"""Bounded Codex admission evidence; fixed clocks and fake readers, never a real engine."""
import copy
import datetime as dt
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_support  # noqa: E402; isolate vendor homes before importing common
from twinlane import board, common, jobs, meter, route  # noqa: E402


NOW = dt.datetime(2026, 1, 10, 12, tzinfo=dt.timezone.utc)


class StaleOkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='twinlane-stale-ok-')
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.policy = copy.deepcopy(common.load_policy())
        self.state = os.path.join(self.temp.name, 'state')
        os.mkdir(self.state)
        self.failed = {'source': 'none', 'note': 'app-server failed: no reply'}
        self.claude = {'source': 'oauth', 'reading_at': common.iso(NOW), 'extra_usage_enabled': False,
                       'windows': [{'name': 'five_hour', 'pct': 3, 'resets_at': common.iso(NOW + dt.timedelta(hours=2))}]}
        for patch in (mock.patch.object(common, 'STATE', self.state),
                      mock.patch.object(common, 'now', return_value=NOW),
                      mock.patch.object(common, 'load_policy', return_value=self.policy),
                      mock.patch.object(meter, 'read_claude_raw', return_value=self.claude),
                      mock.patch.object(meter, 'read_codex_raw', return_value=self.failed),
                      mock.patch.object(common, 'acquire_codex_binary', side_effect=AssertionError('real binary lookup'))):
            patch.start()
            self.addCleanup(patch.stop)
        self.cache = Path(meter.meter_path())

    def seed(self, age=300, pct=3, **changes):
        raw = {'source': 'app-server', 'reading_at': common.iso(NOW - dt.timedelta(seconds=age)),
               'windows': [{'name': 'five_hour', 'pct': pct, 'resets_at': common.iso(NOW + dt.timedelta(hours=1))}],
               'credits': {'has': False}, 'ordinary_usage_allowed': True}
        section = meter.finish_codex(raw, self.policy)
        section.update(changes)
        data = {'read_at': raw['reading_at'], 'read_us': meter._us(raw['reading_at']),
                'claude': meter.finish_claude(self.claude, self.policy), 'codex': section}
        self.cache.write_text(json.dumps(data), encoding='utf-8')
        return data

    def read(self, **kw):
        args = dict(fresh=True, policy=self.policy, admission=True,
                    _claude_reader=lambda: self.claude, _codex_reader=lambda: self.failed)
        args.update(kw)
        return meter.read(**args)

    def decision(self, m):
        return route.decide('verify', m, self.policy, engine='codex')

    def assert_refused(self, m):
        self.assertTrue(self.decision(m)['refused'])
        self.assertEqual(self.decision(m)['reason'], 'forced codex unknown is not usable; no swap')
        self.assertNotIn('stale_ok', m['codex'])

    def test_five_minute_three_percent_reading_admitted_without_changing_cache(self):
        original = self.seed()
        before = self.cache.read_bytes()
        m = self.read()
        self.assertFalse(self.decision(m)['refused'])
        self.assertTrue(m['codex']['stale_ok'])
        self.assertEqual(m['codex']['stale_ok_age_s'], 300)
        self.assertEqual(m['codex']['reading_at'], original['codex']['reading_at'])
        self.assertIn('meter stale-ok (age 5m)', self.decision(m)['reason'])
        self.assertEqual(self.cache.read_bytes(), before)

    def test_thirty_one_minutes_refused(self):
        self.seed(age=1860)
        self.assert_refused(self.read())

    def test_exact_age_limit_refused(self):
        self.seed(age=1800)
        self.assert_refused(self.read())

    def test_handoff_threshold_refused(self):
        self.seed(pct=80)
        self.assert_refused(self.read())

    def test_caution_below_handoff_admitted(self):
        self.seed(pct=79)
        self.assertFalse(self.decision(self.read())['refused'])

    def test_current_policy_handoff_threshold_rechecked(self):
        self.seed(pct=70)
        self.policy['thresholds']['handoff_pct'] = 70
        self.assert_refused(self.read())

    def test_reset_in_past_refused_even_if_it_would_roll_over_to_zero(self):
        data = self.seed()
        data['codex']['windows'][0]['resets_at'] = common.iso(NOW - dt.timedelta(seconds=1))
        self.cache.write_text(json.dumps(data), encoding='utf-8')
        self.assert_refused(self.read())

    def test_every_counted_window_needs_future_reset(self):
        for reset in (None, 'invalid', common.iso(NOW)):
            with self.subTest(reset=reset):
                data = self.seed()
                data['codex']['windows'].append({'name': 'weekly', 'pct': 0, 'resets_at': reset})
                self.cache.write_text(json.dumps(data), encoding='utf-8')
                self.assert_refused(self.read())

    def test_no_cache_refused(self):
        self.assert_refused(self.read())

    def test_malformed_cache_refused(self):
        for body in ('not json', '[]', '{}', '{"codex": 4}', '{"codex": {"status": "ok"}, "claude": {}}'):
            with self.subTest(body=body):
                self.cache.write_text(body, encoding='utf-8')
                self.assert_refused(self.read())

    def test_unreadable_cache_refused(self):
        self.cache.mkdir()
        self.assert_refused(self.read())

    def test_on_credits_refused_even_if_status_says_ok(self):
        self.seed(on_credits=True)
        self.assert_refused(self.read())

    def test_credit_state_recomputed_with_allow_credits(self):
        self.policy['codex']['allow_credits'] = True
        self.seed(credits={'has': True}, ordinary_usage_allowed=False)
        self.assert_refused(self.read())

    def test_unknown_or_inconsistent_cached_percent_refused(self):
        for pct in (None, True, '3', float('nan'), float('inf'), 4):
            with self.subTest(pct=pct):
                self.seed(max_pct=pct)
                self.assert_refused(self.read())

    def test_only_real_sources_are_allowed(self):
        for source in ('none', 'unknown', 'desktop'):
            with self.subTest(source=source):
                self.seed(source=source)
                self.assert_refused(self.read())
        self.seed(source='rollout')
        self.assertFalse(self.decision(self.read())['refused'])

    def test_bad_cached_statuses_refused(self):
        for status in ('handoff', 'stop', 'out', 'unknown'):
            with self.subTest(status=status):
                self.seed(status=status)
                self.assert_refused(self.read())

    def test_future_timestamp_refused(self):
        self.seed(age=-1)
        self.assert_refused(self.read())

    def test_policy_bound_cannot_exceed_existing_stale_reading_limit(self):
        self.policy['meter']['stale_ok_max_age_s'] = 3600
        self.seed(age=1860)
        self.assert_refused(self.read())

    def test_both_configured_bounds_raised_still_hit_the_thirty_minute_ceiling(self):
        self.policy['meter']['stale_ok_max_age_s'] = 3600
        self.policy.setdefault('codex', {})['stale_reading_max_age_s'] = 3600
        self.seed(age=1860)
        self.assert_refused(self.read())

    def test_shorter_configured_bound_is_enforced(self):
        self.policy['meter']['stale_ok_max_age_s'] = 240
        self.seed()
        self.assert_refused(self.read())

    def test_invalid_age_policy_fails_closed(self):
        for bound in (None, True, '1800', -1, float('inf')):
            with self.subTest(bound=bound):
                self.policy['meter']['stale_ok_max_age_s'] = bound
                self.seed()
                self.assert_refused(self.read())

    def test_repeated_failure_never_extends_original_thirty_minute_bound(self):
        original = self.seed()
        before = self.cache.read_bytes()
        self.assertFalse(self.decision(self.read())['refused'])
        later = NOW + dt.timedelta(seconds=1501)
        with mock.patch.object(common, 'now', return_value=later):
            self.assert_refused(self.read())
        self.assertEqual(self.cache.read_bytes(), before)
        self.assertEqual(json.loads(before)['codex']['reading_at'], original['codex']['reading_at'])

    def test_cached_carry_forward_is_never_eligible(self):
        self.seed(stale_ok=True, stale_ok_age_s=0)
        self.assert_refused(self.read())

    def test_fresh_meter_shows_actual_failure_and_labelled_note(self):
        self.seed()
        before = self.cache.read_bytes()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(meter.cli(['--fresh', '--json']), common.EXIT_OK)
        sec = json.loads(out.getvalue())['codex']
        self.assertEqual(sec['source'], 'none')
        self.assertEqual(sec['status'], 'unknown')
        self.assertIsNone(sec['max_pct'])
        self.assertNotIn('stale_ok', sec)
        self.assertIn('meter stale-ok (age 5m) available for dispatch', sec['note'])
        self.assertEqual(self.cache.read_bytes(), before)

    def test_board_shows_failure_with_note_after_admission(self):
        self.seed()
        self.read()
        sec = board.collect_meters(self.policy, NOW)['codex']
        self.assertEqual(sec['status'], 'unknown')
        self.assertEqual(sec['source'], 'none')
        self.assertIsNone(sec['max_pct'])
        self.assertIn('meter stale-ok (age 5m)', sec['note'])

    def test_route_output_includes_stale_ok_note(self):
        self.seed()
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(route.cli(['verify', '--engine', 'codex']), common.EXIT_OK)
        self.assertIn('meter stale-ok (age 5m)', out.getvalue())

    def test_run_admission_and_decision_text_include_stale_ok_note(self):
        self.seed()
        with mock.patch.object(jobs, '_read_meter', None):
            m = jobs._meter(self.policy, auto_refresh=False)
        d = jobs._route('verify', m, self.policy, False, 'codex')
        self.assertFalse(d['refused'])
        self.assertIn('meter stale-ok (age 5m)', d['reason'])

    def test_auto_refresh_admission_also_uses_fallback(self):
        self.seed()
        m = self.read(admission=None, auto_refresh=True)
        self.assertTrue(m['codex']['stale_ok'])

    def test_claude_has_no_stale_ok_fallback(self):
        self.seed()
        m = self.read(_claude_reader=lambda: self.failed)
        self.assertEqual(m['claude']['status'], 'unknown')
        self.assertEqual(m['claude']['source'], 'none')
        self.assertNotIn('stale_ok', m['claude'])

    def test_actual_live_handoff_reading_is_never_overridden(self):
        self.seed()
        raw = {'source': 'app-server', 'reading_at': common.iso(NOW),
               'windows': [{'name': 'weekly', 'pct': 80, 'resets_at': common.iso(NOW + dt.timedelta(days=1))}]}
        m = self.read(_codex_reader=lambda: raw)
        self.assertEqual(m['codex']['status'], 'handoff')
        self.assertNotIn('stale_ok', m['codex'])
        self.assertEqual(json.loads(self.cache.read_text(encoding='utf-8'))['codex']['max_pct'], 80)

    def test_cache_ttl_and_fresh_semantics_are_unchanged(self):
        self.seed()
        reader = mock.Mock(return_value=self.failed)
        self.read(_codex_reader=reader)
        self.read(fresh=False, _codex_reader=reader)
        self.assertEqual(reader.call_count, 1)
        self.read(fresh=True, _codex_reader=reader)
        self.assertEqual(reader.call_count, 2)
        with mock.patch.object(common, 'now', return_value=NOW + dt.timedelta(seconds=121)):
            self.read(fresh=False, _codex_reader=reader)
        self.assertEqual(reader.call_count, 3)


class AppServerRetryTests(unittest.TestCase):
    def result(self):
        return {'rateLimits': {'primary': {'usedPercent': 3, 'windowDurationMins': 300,
                                          'resetsAt': common.iso(NOW + dt.timedelta(hours=2))}}}

    def read(self, side_effect):
        argv = ['fake-codex', 'app-server']
        with mock.patch.object(meter, 'app_server_exchange', side_effect=side_effect) as exchange:
            reading = meter.read_codex_raw(_argv=argv, rollout_fallback=False)
        return reading, exchange

    def test_timeout_retries_once_with_longer_timeout_and_succeeds(self):
        reading, exchange = self.read([TimeoutError('no reply'), self.result()])
        self.assertEqual(reading['source'], 'app-server')
        self.assertEqual(reading['windows'][0]['pct'], 3)
        self.assertEqual([c.args[1] for c in exchange.call_args_list],
                         [meter.APP_SERVER_TIMEOUT_S, meter.APP_SERVER_RETRY_TIMEOUT_S])
        self.assertEqual(meter.APP_SERVER_RETRY_TIMEOUT_S, 30)

    def test_closed_output_retries_once(self):
        reading, exchange = self.read([ConnectionError('app-server closed its output'), self.result()])
        self.assertEqual(reading['source'], 'app-server')
        self.assertEqual(exchange.call_count, 2)

    def test_two_timeouts_give_up_without_third_attempt(self):
        reading, exchange = self.read([TimeoutError('no reply'), TimeoutError('no reply')])
        self.assertEqual(reading['source'], 'none')
        self.assertEqual(exchange.call_count, 2)

    def test_missing_binary_is_not_retried(self):
        with mock.patch.object(common, 'acquire_codex_binary', return_value=(None, 'missing', None)) as acquire, \
                mock.patch.object(common, 'release_codex'), \
                mock.patch.object(meter, 'app_server_exchange') as exchange:
            reading = meter.read_codex_raw(rollout_fallback=False)
        self.assertEqual(reading['source'], 'none')
        self.assertEqual(acquire.call_count, 1)
        exchange.assert_not_called()

    def test_protocol_and_unsupported_errors_are_not_retried(self):
        for failure in (FileNotFoundError('missing'), meter.AppServerError(-32601, 'unsupported'),
                        ValueError('protocol error')):
            with self.subTest(failure=failure):
                reading, exchange = self.read([failure])
                self.assertEqual(reading['source'], 'none')
                self.assertEqual(exchange.call_count, 1)


if __name__ == '__main__':
    unittest.main()
