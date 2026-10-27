"""meter.py: parsing, thresholds, staleness, cache, record-claude, app-server exchange. No network, no model."""
import datetime as dt
import io
import http.server
import json
import os
import random
import shutil
import sys
import threading
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, meter  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
FAKE_TOKEN = 'sk-' + 'ant-oat01-FAKE-TOKEN-must-never-be-written'


def fixture(name):
    with open(os.path.join(FIX, name), encoding='utf-8') as h:
        return h.read()


def iso_in(seconds):
    return common.iso(common.now() + dt.timedelta(seconds=seconds))


def policy(**over):
    p = common.load_policy()
    for path, val in over.items():
        sect, key = path.split('__')
        p[sect] = dict(p[sect], **{key: val})
    return p


def claude_raw(pct, extra=False, age=5, source='oauth'):
    return {'source': source, 'reading_at': iso_in(-age), 'extra_usage_enabled': extra,
            'windows': [{'name': 'five_hour', 'pct': pct, 'resets_at': iso_in(3600)},
                        {'name': 'seven_day', 'pct': 10.0, 'resets_at': iso_in(86400)}]}


def codex_raw(pct, has_credits=False, reached=None, ordinary=True, age=5):
    return {'source': 'app-server', 'reading_at': iso_in(-age),
            'windows': [{'name': 'weekly', 'pct': pct, 'resets_at': iso_in(5 * 86400)}],
            'credits': {'has': has_credits, 'unlimited': False, 'balance': None},
            'limit_reached_type': reached, 'ordinary_usage_allowed': ordinary, 'plan': 'pro'}


class TempState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-meter-')
        self.state = os.path.join(self.tmp, 'state')
        self.claude_dir = os.path.join(self.tmp, 'claude')
        os.makedirs(self.state)
        os.makedirs(self.claude_dir)
        # A codex lookup here never reads the real app or copies into the real bin/ ().
        for p in (mock.patch.object(common, 'STATE', self.state),
                  mock.patch.object(common, 'CLAUDE_DIR', self.claude_dir),
                  mock.patch.object(common, 'BIN', os.path.join(self.tmp, 'bin')),
                  mock.patch.object(common, 'CODEX_DIR', os.path.join(self.tmp, 'dotcodex')),
                  mock.patch.dict(os.environ, {'LOCALAPPDATA': os.path.join(self.tmp, 'local'),
                                               'TWINLANE_TEST_ISOLATION': '1', 'TWINLANE_CODEX_BIN': ''})):
            p.start()
            self.addCleanup(p.stop)
        self.policy = common.load_policy()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def write_creds(self, expires_in_s, exp_ms=None):
        exp_ms = int((time.time() + expires_in_s) * 1000) if exp_ms is None else exp_ms
        common.write_json(os.path.join(self.claude_dir, '.credentials.json'),
                          {'claudeAiOauth': {'accessToken': FAKE_TOKEN, 'expiresAt': exp_ms}})
        return exp_ms

    def record_extra_off(self):
        """The last recorded reading shows extra usage OFF (stale, so the status stays unknown)."""
        meter.save_claude_cache(claude_raw(20, extra=False, age=3 * 3600, source='desktop'), 'desktop')

    def state_text(self):
        out = ''
        for name in os.listdir(self.state):
            with open(os.path.join(self.state, name), encoding='utf-8') as h:
                out += h.read()
        return out


class ThresholdTests(unittest.TestCase):
    TH = {'caution_pct': 75, 'handoff_pct': 80, 'stop_pct': 85, 'hard_pct': 100}

    def test_status_bands(self):
        cases = [(0, 'ok'), (74.9, 'ok'), (75, 'caution'), (79.9, 'caution'), (80, 'handoff'),
                 (84.9, 'handoff'), (85, 'stop'), (99.9, 'stop'), (100, 'out'), (130, 'out'), (None, 'unknown')]
        for pct, want in cases:
            self.assertEqual(meter.status_for_pct(pct, self.TH), want, pct)

    def test_window_names_from_minutes(self):
        self.assertEqual(meter.window_name_from_minutes(10080), 'weekly')
        self.assertEqual(meter.window_name_from_minutes(300), 'five_hour')
        self.assertEqual(meter.window_name_from_minutes(60), '60min')

    def test_rolled_over_window_counts_zero(self):
        w = meter.effective_windows([{'name': 'weekly', 'pct': 97.0, 'resets_at': iso_in(-60)}])
        self.assertEqual(w[0]['pct'], 0.0)
        self.assertTrue(w[0]['rolled_over'])

    def test_next_reset_is_latest_binding_window(self):
        wins = [{'name': 'five_hour', 'pct': 90, 'resets_at': iso_in(3600)},
                {'name': 'seven_day', 'pct': 100, 'resets_at': iso_in(86400)},
                {'name': 'seven_day_sonnet', 'pct': 5, 'resets_at': iso_in(60)}]
        self.assertEqual(meter.next_reset(wins, self.TH), wins[1]['resets_at'])

    def test_next_reset_without_binding_window_is_earliest(self):
        wins = [{'name': 'five_hour', 'pct': 10, 'resets_at': iso_in(3600)},
                {'name': 'seven_day', 'pct': 20, 'resets_at': iso_in(86400)}]
        self.assertEqual(meter.next_reset(wins, self.TH), wins[0]['resets_at'])


class FinishTests(unittest.TestCase):
    def setUp(self):
        self.policy = common.load_policy()

    def test_claude_bands(self):
        for pct, want in ((12, 'ok'), (76, 'caution'), (82, 'handoff'), (90, 'stop'), (100, 'out')):
            self.assertEqual(meter.finish_claude(claude_raw(pct), self.policy)['status'], want)

    def test_claude_extra_usage_on_gives_stop(self):
        s = meter.finish_claude(claude_raw(12, extra=True), self.policy)
        self.assertEqual(s['status'], 'stop')
        self.assertIn('extra usage', s['note'])
        self.assertEqual(s['hold'], 'extra usage is ON')  # no `wait until` a window reset

    def test_claude_extra_usage_does_not_soften_out(self):
        self.assertEqual(meter.finish_claude(claude_raw(100, extra=True), self.policy)['status'], 'out')

    def test_claude_extra_usage_allowed_by_policy(self):
        s = meter.finish_claude(claude_raw(12, extra=True), policy(claude__extra_usage_allowed=True))
        self.assertEqual(s['status'], 'ok')
        self.assertIn('WARNING', s['note'])

    def test_only_a_literal_true_allows_extra_usage(self):
        for value in ('false', 'no', 1, 'true'):
            s = meter.finish_claude(claude_raw(12, extra=True), policy(claude__extra_usage_allowed=value))
            self.assertEqual(s['status'], 'stop', value)
            s = meter.finish_claude(claude_raw(12, extra=None), policy(claude__extra_usage_allowed=value))
            self.assertEqual(s['status'], 'unknown', value)

    def test_stale_reading_is_unknown(self):
        s = meter.finish_claude(claude_raw(12, age=1801), self.policy)
        self.assertEqual(s['status'], 'unknown')
        self.assertIn('stale', s['note'])
        self.assertEqual(s['max_pct'], 12)  # the numbers are kept for display

    def test_future_dated_reading_is_unknown(self):
        # a reading stamped in the future (clock skew, bad record) would never go stale.
        s = meter.finish_claude(claude_raw(12, age=-3600), self.policy)
        self.assertEqual(s['status'], 'unknown')
        self.assertIn('future', s['note'])
        self.assertEqual(meter.finish_claude(claude_raw(12, age=-30), self.policy)['status'], 'ok')  # small skew

    def test_unknown_extra_usage_past_the_handoff_line_is_noted(self):
        s = meter.finish_claude(claude_raw(90, extra=None), self.policy)
        self.assertEqual(s['status'], 'stop')
        self.assertIn('extra usage state unknown', s['note'])

    def test_unknown_extra_usage_holds_claude_at_any_usable_level(self):
        # None at 20 % was `ok` and routed to Claude. Unknown is not OFF.
        for pct in (20, 76, 82):
            s = meter.finish_claude(claude_raw(pct, extra=None), self.policy)
            self.assertEqual(s['status'], 'unknown', pct)
            self.assertEqual(s['hold'], meter.HOLD_EXTRA_UNKNOWN)
            self.assertIn('extra usage state unknown', s['note'])
            self.assertIn('--record-claude', s['note'])  # says how to clear it
            self.assertEqual(s['max_pct'], pct)  # the numbers stay visible
        off = meter.finish_claude(claude_raw(20, extra=False), self.policy)
        self.assertEqual(off['status'], 'ok')
        self.assertNotIn('hold', off)

    def test_unknown_extra_usage_does_not_hide_stop_or_out(self):
        for pct, want in ((90, 'stop'), (100, 'out')):
            self.assertEqual(meter.finish_claude(claude_raw(pct, extra=None), self.policy)['status'], want)

    def test_unknown_extra_usage_is_fine_when_policy_allows_extra_usage(self):
        s = meter.finish_claude(claude_raw(20, extra=None), policy(claude__extra_usage_allowed=True))
        self.assertEqual(s['status'], 'ok')

    def test_no_reading_is_unknown(self):
        for raw in (None, {}, {'source': 'none', 'note': 'x'}):
            self.assertEqual(meter.finish_claude(raw, self.policy)['status'], 'unknown')
            self.assertEqual(meter.finish_codex(raw, self.policy)['status'], 'unknown')

    def test_reading_without_windows_is_unknown(self):
        raw = claude_raw(1)
        raw['windows'] = []
        self.assertEqual(meter.finish_claude(raw, self.policy)['status'], 'unknown')

    def test_codex_bands(self):
        for pct, want in ((30, 'ok'), (75, 'caution'), (80, 'handoff'), (85, 'stop'), (100, 'out')):
            self.assertEqual(meter.finish_codex(codex_raw(pct), self.policy)['status'], want)

    def test_codex_on_credits_not_allowed_is_out(self):
        s = meter.finish_codex(codex_raw(100, has_credits=True), self.policy)
        self.assertTrue(s['on_credits'])
        self.assertEqual(s['status'], 'out')
        self.assertIn('twinlane refuses', s['note'])

    def test_codex_limit_reached_with_credits_is_on_credits_and_out(self):
        s = meter.finish_codex(codex_raw(40, has_credits=True, reached='primary'), self.policy)
        self.assertTrue(s['on_credits'])
        self.assertEqual(s['status'], 'out')

    def test_codex_limit_reached_without_credits_is_out(self):
        s = meter.finish_codex(codex_raw(40, reached='primary'), self.policy)
        self.assertFalse(s['on_credits'])
        self.assertEqual(s['status'], 'out')

    def test_codex_ordinary_usage_not_allowed_is_out(self):
        self.assertEqual(meter.finish_codex(codex_raw(40, ordinary=False), self.policy)['status'], 'out')

    def test_ordinary_usage_not_allowed_needs_credits_and_policy(self):
        for allow in (False, True):
            for has_credits in (False, True):
                s = meter.finish_codex(codex_raw(40, ordinary=False, has_credits=has_credits),
                                       policy(codex__allow_credits=allow))
                self.assertEqual(s['status'], 'caution' if allow and has_credits else 'out',
                                 (allow, has_credits))
                self.assertIn('WARNING' if allow and has_credits else 'ordinary usage not allowed', s['note'])
        s = meter.finish_codex(codex_raw(100, ordinary=False, has_credits=True, reached='primary'),
                               policy(codex__allow_credits=True))
        self.assertEqual(s['status'], 'caution')

    def test_codex_credits_present_but_under_limit_is_normal(self):
        s = meter.finish_codex(codex_raw(30, has_credits=True), self.policy)
        self.assertFalse(s['on_credits'])
        self.assertEqual(s['status'], 'ok')

    def test_codex_on_credits_allowed_is_usable(self):
        s = meter.finish_codex(codex_raw(100, has_credits=True), policy(codex__allow_credits=True))
        self.assertEqual(s['status'], 'caution')
        self.assertIn('WARNING', s['note'])

    def test_codex_stale_is_unknown(self):
        self.assertEqual(meter.finish_codex(codex_raw(30, age=4000), self.policy)['status'], 'unknown')


class ClaudePayloadTests(unittest.TestCase):
    def test_oauth_fixture(self):
        wins, extra, shape = meter.normalize_claude_payload(fixture('oauth_usage.json'))
        self.assertEqual(shape, 'oauth')
        self.assertFalse(extra)
        by = {w['name']: w for w in wins}
        self.assertEqual(set(by), {'five_hour', 'seven_day', 'seven_day_sonnet'})  # null and extra_usage skipped
        self.assertEqual(by['five_hour']['pct'], 50.0)
        self.assertEqual(by['five_hour']['resets_at'], '2026-10-08T15:00:00Z')

    def test_desktop_shape(self):
        payload = {'plan': {'windows': [
            {'label': '5-hour limit', 'percentUsed': 12, 'resetsAt': '2026-10-08T15:09:00Z'},
            {'label': 'Weekly · all models', 'percentUsed': 44, 'resetsAt': '2026-10-13T20:59:00Z'},
            {'label': 'Weekly · Opus', 'percentUsed': 9, 'resetsAt': '2026-10-13T20:59:00Z'}],
            'extraUsage': {'enabled': True}}}
        wins, extra, shape = meter.normalize_claude_payload(json.dumps(payload, ensure_ascii=False))
        self.assertEqual(shape, 'desktop')
        self.assertTrue(extra)
        self.assertEqual([w['name'] for w in wins], ['five_hour', 'seven_day', 'seven_day_opus'])
        self.assertEqual(wins[1]['pct'], 44.0)

    def test_desktop_window_names_never_collide(self):
        # Distinct weekly labels must keep separate keys.
        payload = {'plan': {'windows': [
            {'label': 'Weekly · all models', 'percentUsed': 50, 'resetsAt': '2026-10-13T21:00:00Z'},
            {'label': 'Weekly · Claude Design', 'percentUsed': 10, 'resetsAt': '2026-10-13T21:00:00Z'},
            {'label': '7-day limit', 'percentUsed': 20, 'resetsAt': None},
            {'label': 'Weekly', 'percentUsed': 30, 'resetsAt': None}]}}
        wins, _, _ = meter.normalize_claude_payload(payload)
        self.assertEqual([w['name'] for w in wins],
                         ['seven_day', 'seven_day_claude_design', 'seven_day_2', 'seven_day_3'])
        cached = {'windows': [{'name': 'seven_day', 'pct': 50}, {'name': 'seven_day', 'pct': 10}]}
        self.assertEqual([w['name'] for w in meter.normalize_claude_payload(cached)[0]],
                         ['seven_day', 'seven_day_2'])

    def test_unrecognized_payload_raises(self):
        for bad in ('{}', '[]', '{"foo": 1}'):
            with self.assertRaises(ValueError):
                meter.normalize_claude_payload(bad)


class ClaudeSourceTests(TempState):
    def test_oauth_redirect_never_forwards_authorization(self):
        self.write_creds(3600)
        received = []

        class Destination(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                received.append(dict(self.headers))
                self.send_response(200)
                self.end_headers()

            def log_message(self, *_):
                pass

        destination = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Destination)

        class Redirect(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header('Location', 'http://127.0.0.1:{}/usage'.format(destination.server_port))
                self.end_headers()

            def log_message(self, *_):
                pass

        source = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Redirect)
        threads = [threading.Thread(target=server.serve_forever, daemon=True)
                   for server in (source, destination)]
        try:
            for thread in threads:
                thread.start()
            with mock.patch.object(meter, 'OAUTH_URL', 'http://127.0.0.1:{}/usage'.format(source.server_port)):
                raw = meter.read_claude_raw()
            self.assertEqual(meter.finish_claude(raw, self.policy)['status'], 'unknown')
            self.assertFalse(any('Authorization' in headers for headers in received))
        finally:
            for server in (source, destination):
                server.shutdown()
                server.server_close()
            for thread in threads:
                thread.join(timeout=3)

    def test_oauth_success_caches_without_token(self):
        self.write_creds(3600)
        seen = {}

        def fake_http(url, headers, timeout):
            seen.update(url=url, headers=headers, timeout=timeout)
            return 200, fixture('oauth_usage.json')

        r = meter.read_claude_raw(_http=fake_http)
        self.assertEqual(r['source'], 'oauth')
        self.assertEqual(seen['url'], 'https://api.anthropic.com/api/oauth/usage')
        self.assertEqual(seen['headers']['anthropic-beta'], 'oauth-2025-04-20')
        self.assertEqual(seen['headers']['Authorization'], f'Bearer {FAKE_TOKEN}')
        self.assertEqual(seen['timeout'], 6)
        self.assertTrue(os.path.exists(meter.claude_cache_path()))
        self.assertNotIn(FAKE_TOKEN, self.state_text())

    def test_expired_token_skips_the_call_and_uses_cache(self):
        self.write_creds(-60)
        meter.save_claude_cache(claude_raw(33), 'desktop')
        http = mock.Mock()
        r = meter.read_claude_raw(_http=http)
        http.assert_not_called()
        self.assertEqual(r['source'], 'desktop')
        self.assertIn('expired', r['note'])

    def test_http_error_falls_back_to_cache_named_cache(self):
        self.write_creds(3600)
        meter.save_claude_cache(claude_raw(20), 'oauth')
        r = meter.read_claude_raw(_http=lambda *a: (401, ''))
        self.assertEqual(r['source'], 'cache')
        self.assertIn('HTTP 401', r['note'])

    def test_network_error_note_never_contains_token(self):
        self.write_creds(3600)

        def boom(url, headers, timeout):
            raise OSError('connection refused')

        r = meter.read_claude_raw(_http=boom)
        self.assertEqual(r['source'], 'none')
        self.assertNotIn(FAKE_TOKEN, json.dumps(r))

    def test_no_credentials_and_no_cache_is_none(self):
        r = meter.read_claude_raw(_http=mock.Mock())
        self.assertEqual(r['source'], 'none')
        self.assertEqual(meter.finish_claude(r, self.policy)['status'], 'unknown')

    def test_refresh_argv_is_exact(self):
        with mock.patch.object(common, 'claude_bin', return_value='\x43:/bin/claude.exe'):
            argv = meter.claude_refresh_argv()
        self.assertEqual(argv, ['\x43:/bin/claude.exe', '-p', '--model', 'claude-haiku-4-5', '--max-turns', '1',
                                '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
                                '--settings', '{"disableAllHooks":true}', '--disable-slash-commands',
                                '--output-format', 'json', 'Reply OK'])

    def test_refresh_path_runs_injected_runner_then_retries(self):
        """The refresh command is captured by a fake runner: it is never executed."""
        self.write_creds(-60)
        self.record_extra_off()
        calls = []

        def fake_run(argv, cwd=None, timeout=30, env=None, input_text=None):
            calls.append({'argv': argv, 'cwd': cwd, 'env': env})
            self.write_creds(3600)  # what the real CLI would do: refresh the token
            return 0, json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 2}}), ''

        with mock.patch.dict(os.environ, {'ANTHROPIC_API_KEY': 'x', 'CLAUDECODE': '1'}), \
                mock.patch.object(common, 'claude_bin', return_value='\x43:/bin/claude.exe'):
            expected = meter.claude_refresh_argv()
            r = meter.read_claude_raw(refresh=True, _http=lambda *a: (200, fixture('oauth_usage.json')),
                                      _run=fake_run)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]['argv'], expected)
        self.assertEqual(os.path.normcase(calls[0]['cwd']), os.path.normcase(common.ROOT))
        self.assertNotIn('ANTHROPIC_API_KEY', calls[0]['env'])
        self.assertNotIn('CLAUDECODE', calls[0]['env'])
        self.assertEqual(r['source'], 'oauth')
        self.assertIn('input_tokens=9', r['note'])

    def test_no_refresh_without_flag(self):
        self.write_creds(-60)
        run = mock.Mock()
        meter.read_claude_raw(refresh=False, _http=mock.Mock(), _run=run)
        run.assert_not_called()

    def test_malformed_refresh_output_keeps_the_cached_reading(self):
        self.write_creds(-60)
        self.record_extra_off()
        for body in ('not json', '[1]', '"unexpected"', '42', 'null',
                     '{"usage": 1}', '{"usage": "input_tokens"}', '{"usage": ["input_tokens"]}'):
            with self.subTest(body=body), mock.patch.object(common, 'claude_bin', return_value='fake-claude'):
                run = mock.Mock(return_value=(1, body, ''))
                raw = meter.read_claude_raw(refresh=True, _http=mock.Mock(), _run=run)
                run.assert_called_once()
                self.assertEqual(raw['source'], 'desktop')
                self.assertIs(raw['extra_usage_enabled'], False)
                self.assertIn('refresh call rc=1', raw['note'])
                self.assertIn('after refresh:', raw['note'])

    def test_malformed_refresh_output_still_retries_oauth(self):
        self.write_creds(-60)
        self.record_extra_off()

        def run(*args, **kwargs):
            self.write_creds(3600)
            return 0, '["unexpected"]', ''

        with mock.patch.object(common, 'claude_bin', return_value='fake-claude'):
            raw = meter.read_claude_raw(refresh=True, _http=lambda *a: (200, fixture('oauth_usage.json')),
                                       _run=run)
        self.assertEqual(raw['source'], 'oauth')
        self.assertIn('token refreshed', raw['note'])


class RefreshGateTests(TempState):
    """The token refresh is a real (tiny) model call: it may run only when extra usage is known OFF."""

    def setUp(self):
        super().setUp()
        self.calls = []
        # twinlane ships the automatic refresh OFF; these tests turn it on to exercise its extra-usage gate.
        self.policy = json.loads(json.dumps(self.policy))
        self.policy.setdefault('meter', {})['auto_refresh_claude'] = True

    def fake_run(self, argv, cwd=None, timeout=30, env=None, input_text=None):
        self.calls.append(argv)
        self.write_creds(3600)  # what the real CLI does on a refresh
        return 0, json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 2}}), ''

    def seed(self, extra, age=3 * 3600):
        """The last recorded reading, old enough to be stale (so the status is unknown and an auto refresh looks due)."""
        meter.save_claude_cache(claude_raw(20, extra=extra, age=age, source='desktop'), 'desktop')

    def auto(self, **kw):
        return meter.read(fresh=True, policy=self.policy, auto_refresh=True, _codex_reader=lambda: codex_raw(30),
                          _run=self.fake_run, _http=lambda *a: (200, fixture('oauth_usage.json')), **kw)

    def test_expired_token_with_extra_usage_on_makes_no_model_call(self):
        self.write_creds(-60)
        self.seed(True)
        m = self.auto()
        self.assertEqual(self.calls, [])
        self.assertEqual(m['claude']['status'], 'unknown')
        self.assertIn('automatic refresh not run: extra usage is ON', m['claude']['note'])
        self.assertNotIn('refreshed_now', m)

    def test_expired_token_with_extra_usage_unknown_makes_no_model_call(self):
        self.write_creds(-60)
        self.seed(None)
        m = self.auto()
        self.assertEqual(self.calls, [])
        self.assertIn('automatic refresh not run: extra usage state unknown', m['claude']['note'])

    def test_expired_token_with_no_recorded_reading_makes_no_model_call(self):
        self.write_creds(-60)
        self.auto()
        self.assertEqual(self.calls, [])

    def test_dry_run_never_promises_a_refresh_the_gate_would_block(self):
        # --dry-run said a real run "would refresh" while extra usage was not known OFF.
        from twinlane import jobs
        expired = {'claude': {'status': 'unknown', 'token_expired_at': common.iso(common.now())}}
        with mock.patch.object(meter, 'auto_refresh_enabled', return_value=True),                 mock.patch.object(meter, 'refresh_pending', return_value=True):
            for extra in (True, None):
                self.seed(extra)
                with self.subTest(extra=extra):
                    note = jobs._dry_run_refresh_note(expired, self.policy)
                    self.assertIn('would NOT refresh', note)
            self.seed(False)
            self.assertIn('would refresh it once', jobs._dry_run_refresh_note(expired, self.policy))

    def test_known_off_still_refreshes_once(self):
        self.write_creds(-60)
        self.seed(False)
        with mock.patch.object(common, 'claude_bin', return_value='C' + ':/bin/claude.exe'):
            m = self.auto()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(m['refreshed_now'], ['claude'])
        self.assertEqual(m['claude']['source'], 'oauth')

    def test_the_raw_refresh_path_is_gated_too(self):
        # Explicit internal refreshes are gated just like automatic refreshes.
        for extra in (True, None):
            self.write_creds(-60)
            self.seed(extra)
            r = meter.read_claude_raw(True, _http=lambda *a: (500, ''), _run=self.fake_run)
            self.assertEqual(self.calls, [], extra)
            self.assertIn('refresh not run', r['note'])

    def run_cli(self, *args):
        """twinlane meter ARGS with the network, the Codex reader and the model call all faked."""
        refresh = mock.Mock(return_value='refresh call rc=0')
        out = io.StringIO()
        with mock.patch.object(meter, '_refresh_token', refresh), mock.patch.object(
                meter, 'read_codex_raw', return_value=codex_raw(30)), mock.patch.object(
                meter, '_http_get', return_value=(500, '')), redirect_stdout(out):
            rc = meter.cli(list(args))
        return rc, out.getvalue(), refresh

    def test_meter_refresh_flag_needs_extra_usage_known_off(self):
        for extra, want in ((True, 0), (None, 0), (False, 1)):
            with self.subTest(extra=extra):
                self.write_creds(-60)
                self.seed(extra)
                rc, _out, refresh = self.run_cli('--refresh')
                self.assertEqual(rc, 0)
                self.assertEqual(refresh.call_count, want)
        # without the flag the CLI never renews, whatever extra usage says
        self.write_creds(-60)
        self.seed(False)
        _rc, _out, refresh = self.run_cli()
        self.assertEqual(refresh.call_count, 0)

    def test_meter_refresh_flag_dry_reports(self):
        for extra, would in ((True, False), (None, False), (False, True)):
            with self.subTest(extra=extra):
                self.write_creds(-60)
                self.seed(extra)
                rc, out, refresh = self.run_cli('--refresh', '--dry-run', '--json')
                self.assertEqual(rc, 0)
                self.assertEqual(refresh.call_count, 0)  # a dry run never makes the model call
                plan = json.loads(out)['refresh_dry_run']
                self.assertIs(plan['would_refresh'], would)
                self.assertTrue(plan['token_expired_at'])
                self.assertIn('renew it' if would else 'NOT renew', plan['report'])
        self.write_creds(3600)  # a valid token: nothing to renew
        _rc, out, refresh = self.run_cli('--dry-run')
        self.assertEqual(refresh.call_count, 0)
        self.assertIn('not expired', out)

    def test_a_blocked_refresh_keeps_the_expiry_claimable(self):
        self.write_creds(-60)
        self.seed(None)
        self.auto()
        self.assertEqual(self.calls, [])
        self.assertEqual((common.read_json(meter._claims_path(), {}) or {}).get('claimed', []), [])
        self.seed(False)  # the user records a reading that shows extra usage OFF
        with mock.patch.object(common, 'claude_bin', return_value='C' + ':/bin/claude.exe'):
            self.auto()
        self.assertEqual(len(self.calls), 1)

    def test_unknown_extra_usage_with_a_valid_token_needs_no_refresh_and_is_held(self):
        self.write_creds(3600)
        meter.save_claude_cache(claude_raw(20, extra=None, age=5, source='desktop'), 'desktop')
        m = meter.read(fresh=True, policy=self.policy, _codex_reader=lambda: codex_raw(30),
                       _http=lambda *a: (500, ''), _run=self.fake_run)
        self.assertEqual(self.calls, [])
        self.assertEqual(m['claude']['status'], 'unknown')
        self.assertEqual(m['claude']['hold'], meter.HOLD_EXTRA_UNKNOWN)


class CodexSourceTests(TempState):
    def test_parse_app_server_fixture(self):
        r = meter.parse_app_server(json.loads(fixture('app_server_ratelimits.json'))['result'])
        self.assertEqual(r['source'], 'app-server')
        self.assertEqual([w['name'] for w in r['windows']], ['weekly', 'five_hour'])
        self.assertEqual(r['windows'][0]['pct'], 30.0)
        self.assertEqual(r['windows'][0]['resets_at'], common.iso(common.parse_time(1791956220)))
        self.assertEqual(r['credits'], {'has': True, 'unlimited': False, 'balance': 100.0})
        self.assertEqual(r['plan'], 'pro')
        self.assertTrue(r['ordinary_usage_allowed'])

    def test_rollout_reading_skips_null_and_takes_newest(self):
        root = os.path.join(self.tmp, 'sessions', '2026', '10', '07')
        os.makedirs(root)
        shutil.copy(os.path.join(FIX, 'rollout_sample.jsonl'), os.path.join(root, 'rollout-fixture.jsonl'))
        r = meter.rollout_reading(os.path.join(self.tmp, 'sessions'))
        self.assertEqual(r['source'], 'rollout')
        self.assertEqual(r['reading_at'], '2026-10-07T09:03:00Z')
        self.assertEqual(r['windows'][0], {'name': 'weekly', 'pct': 65.0,
                                           'resets_at': common.iso(common.parse_time(1791096217))})
        self.assertEqual(r['windows'][1]['name'], 'five_hour')
        self.assertFalse(r['credits']['has'])
        # the 5h window reset in 2025: it counts as 0 once finished
        fin = meter.effective_windows(r['windows'])
        self.assertEqual(fin[1]['pct'], 0.0)

    def test_rollout_none_when_no_files(self):
        self.assertIsNone(meter.rollout_reading(os.path.join(self.tmp, 'nothing')))

    def test_missing_binary_is_noted_and_falls_back(self):
        # Neither an app build nor a PATH binary: remain unknown without launching.
        with mock.patch.object(common, 'acquire_codex', return_value=(None, 'no app', None)), \
                mock.patch.object(common.shutil, 'which', return_value=None):
            r = meter.read_codex_raw(sessions_root=os.path.join(self.tmp, 'none'))
        self.assertEqual(r['source'], 'none')
        self.assertIn('codex binary not found', r['note'])
        self.assertEqual(meter.finish_codex(r, self.policy)['status'], 'unknown')

    @unittest.skipUnless(os.name == 'nt', 'the desktop-app copy is Windows-only')
    def test_app_server_read_holds_a_lease_on_the_private_copy(self):
        # a prune during the read must not remove the copy the app-server runs from.
        app = os.path.join(self.tmp, 'dotcodex', 'plugins', '.plugin-appserver')
        os.makedirs(app)
        for name in common.CODEX_FILES:
            with open(os.path.join(app, name), 'wb') as h:
                h.write(b'fake ' + name.encode())
        seen = []

        def exchange(argv, timeout):
            leases = os.path.join(os.path.dirname(argv[0]), 'leases')
            seen.append((leases, os.listdir(leases) if os.path.isdir(leases) else []))
            return json.loads(fixture('app_server_ratelimits.json'))['result']
        with mock.patch.object(common, '_read_codex_version', return_value=None, create=True), \
                mock.patch.object(meter, 'app_server_exchange', side_effect=exchange):
            r, why = meter.read_app_server(timeout=5)
        self.assertEqual(why, '')
        leases, held = seen[0]
        self.assertTrue(leases.startswith(common.BIN), leases)
        self.assertEqual(len(held), 1, held)
        self.assertEqual(os.listdir(leases), [])  # released after the read

    @unittest.skipUnless(os.name == 'nt', 'the desktop-app copy is Windows-only')
    def test_app_server_read_names_an_earlier_copy(self):
        # , author's choice 1: reading through an older copy must be visible in the reading.
        app = os.path.join(self.tmp, 'dotcodex', 'plugins', '.plugin-appserver')
        os.makedirs(app)
        for name in common.CODEX_FILES:
            with open(os.path.join(app, name), 'wb') as h:
                h.write(b'fake ' + name.encode())
        with mock.patch.object(common, '_read_codex_version', return_value=None, create=True):
            first = common.codex_bin()
        shutil.rmtree(app)  # the app emptied its folder on exit
        result = json.loads(fixture('app_server_ratelimits.json'))['result']
        with mock.patch.object(meter, 'app_server_exchange', return_value=result):
            r, why = meter.read_app_server(timeout=5)
        self.assertEqual(why, '')
        self.assertIn('earlier private copy', r.get('note') or '')
        self.assertIn(first, r['note'])

    def _fake_server(self, body):
        path = os.path.join(self.tmp, 'fake_app_server.py')
        with open(path, 'w', encoding='utf-8') as h:
            h.write(body)
        return [sys.executable, '-I', path]

    def test_app_server_exchange_with_fake(self):
        log = os.path.join(self.tmp, 'methods.txt')
        result = json.loads(fixture('app_server_ratelimits.json'))['result']
        argv = self._fake_server(f'''
import json, sys
log = open({log!r}, 'w', encoding='utf-8')
for line in sys.stdin:
    msg = json.loads(line)
    log.write(msg.get('method', '') + '\\n'); log.flush()
    if msg.get('id') == 1:
        print(json.dumps({{"method": "some/notification", "params": {{}}}}), flush=True)
        print(json.dumps({{"id": 1, "result": {{"userAgent": "fake"}}}}), flush=True)
    elif msg.get('id') == 2:
        print(json.dumps({{"id": 2, "result": json.loads({json.dumps(result)!r})}}), flush=True)
''')
        r, why = meter.read_app_server(_argv=argv, timeout=10)
        self.assertEqual(why, '')
        self.assertEqual(r['windows'][0]['pct'], 30.0)
        with open(log, encoding='utf-8') as h:
            methods = h.read().split()
        self.assertEqual(methods, ['initialize', 'initialized', 'account/rateLimits/read'])

    def _shape_server(self, log, accept_params):
        """Fake app-server that accepts rateLimits/read only with (old) or only without (new) params."""
        result = json.loads(fixture('app_server_ratelimits.json'))['result']
        return self._fake_server(f'''
import json, sys
log = open({log!r}, 'w', encoding='utf-8')
for line in sys.stdin:
    msg = json.loads(line)
    log.write(msg.get('method', '') + ('+params' if 'params' in msg else '') + '\\n'); log.flush()
    if msg.get('id') == 1:
        print(json.dumps({{"id": 1, "result": {{"userAgent": "fake"}}}}), flush=True)
    elif msg.get('method') == 'account/rateLimits/read':
        if ('params' in msg) == {accept_params!r}:
            print(json.dumps({{"id": msg['id'], "result": json.loads({json.dumps(result)!r})}}), flush=True)
        else:
            print(json.dumps({{"id": msg['id'], "error": {{"code": -32600,
                  "message": "Invalid request: invalid type: map, expected unit"}}}}), flush=True)
''')

    def test_app_server_new_shape_sends_no_params(self):
        log = os.path.join(self.tmp, 'methods.txt')
        r, why = meter.read_app_server(_argv=self._shape_server(log, accept_params=False), timeout=10)
        self.assertEqual(why, '')
        self.assertEqual(r['windows'][0]['pct'], 30.0)
        with open(log, encoding='utf-8') as h:
            self.assertEqual(h.read().split(), ['initialize+params', 'initialized', 'account/rateLimits/read'])

    def test_app_server_old_shape_retries_with_params(self):
        log = os.path.join(self.tmp, 'methods.txt')
        r, why = meter.read_app_server(_argv=self._shape_server(log, accept_params=True), timeout=10)
        self.assertEqual(why, '')
        self.assertEqual(r['windows'][0]['pct'], 30.0)
        with open(log, encoding='utf-8') as h:
            self.assertEqual(h.read().split(), ['initialize+params', 'initialized', 'account/rateLimits/read',
                                                'account/rateLimits/read+params'])

    def test_app_server_other_error_is_not_retried(self):
        log = os.path.join(self.tmp, 'methods.txt')
        argv = self._fake_server(f'''
import json, sys
log = open({log!r}, 'w', encoding='utf-8')
for line in sys.stdin:
    msg = json.loads(line)
    log.write(msg.get('method', '') + '\\n'); log.flush()
    if msg.get('id') == 1:
        print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
    elif 'id' in msg:
        print(json.dumps({{"id": msg['id'], "error": {{"code": -32001, "message": "not signed in"}}}}), flush=True)
''')
        r, why = meter.read_app_server(_argv=argv, timeout=10)
        self.assertIsNone(r)
        self.assertIn('-32001', why)
        with open(log, encoding='utf-8') as h:
            self.assertEqual(h.read().split().count('account/rateLimits/read'), 1)

    def test_app_server_retry_reads_the_error_code_not_its_text(self):
        # A message that merely quotes a shape-error code is not a shape refusal.
        log = os.path.join(self.tmp, 'methods.txt')
        argv = self._fake_server(f'''
import json, sys
log = open({log!r}, 'w', encoding='utf-8')
for line in sys.stdin:
    msg = json.loads(line)
    log.write(msg.get('method', '') + '\\n'); log.flush()
    if msg.get('id') == 1:
        print(json.dumps({{"id": 1, "result": {{}}}}), flush=True)
    elif 'id' in msg:
        print(json.dumps({{"id": msg['id'], "error": {{"code": -32001,
              "message": "upstream said -32602 invalid params"}}}}), flush=True)
''')
        r, why = meter.read_app_server(_argv=argv, timeout=10)
        self.assertIsNone(r)
        self.assertIn('-32001', why)
        with open(log, encoding='utf-8') as h:
            self.assertEqual(h.read().split().count('account/rateLimits/read'), 1)

    def test_app_server_exit_after_shape_error_reports_the_refusal(self):
        # The server refuses the shape and exits: the retry cannot be sent or answered, and the report must
        # keep the refusal instead of a vague broken pipe / closed output.
        argv = self._fake_server('''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if msg.get('id') == 1:
        print(json.dumps({"id": 1, "result": {}}), flush=True)
    elif msg.get('method') == 'account/rateLimits/read':
        print(json.dumps({"id": msg['id'], "error": {"code": -32600, "message": "invalid type: unit"}}), flush=True)
        sys.exit(0)
''')
        r, why = meter.read_app_server(_argv=argv, timeout=10)
        self.assertIsNone(r)
        self.assertIn('-32600', why)

    def test_binary_lookup_race_falls_back_to_rollouts(self):
        # The app can empty its bundle mid-lookup; the meter must still use the rollout reading.
        root = os.path.join(self.tmp, 'sessions', '2026', '10', '07')
        os.makedirs(root)
        shutil.copy(os.path.join(FIX, 'rollout_sample.jsonl'), os.path.join(root, 'rollout-fixture.jsonl'))
        with mock.patch.object(common, 'acquire_codex', side_effect=FileNotFoundError('bundle emptied'), create=True), \
                mock.patch.object(common.shutil, 'which', return_value=os.path.join(self.tmp, 'no', 'codex.exe')):
            r = meter.read_codex_raw(sessions_root=os.path.join(self.tmp, 'sessions'))
        self.assertEqual(r['source'], 'rollout')
        self.assertIn('lookup failed', r['note'])

    def _rollout_fixture(self):
        root = os.path.join(self.tmp, 'sessions', '2026', '10', '07')
        os.makedirs(root)
        shutil.copy(os.path.join(FIX, 'rollout_sample.jsonl'), os.path.join(root, 'rollout-fixture.jsonl'))
        return os.path.join(self.tmp, 'sessions')

    def test_no_rollout_leaves_a_failed_read_unknown(self):
        # rollouts are present and would be used; with the fallback off they are never opened.
        sessions = self._rollout_fixture()
        with mock.patch.object(meter, 'read_app_server', return_value=(None, 'app-server exited 1')),                 mock.patch.object(meter, 'rollout_reading', side_effect=AssertionError('rollouts opened')) as rr:
            r = meter.read_codex_raw(sessions_root=sessions, rollout_fallback=False)
        rr.assert_not_called()
        self.assertEqual(r['source'], 'none')
        self.assertIn('app-server exited 1', r['note'])
        self.assertIn('rollout fallback off', r['note'])
        self.assertEqual(meter.finish_codex(r, self.policy)['status'], 'unknown')

    def test_no_rollout_keeps_a_good_app_server_reading(self):
        good = meter.parse_app_server(json.loads(fixture('app_server_ratelimits.json'))['result'])
        with mock.patch.object(meter, 'read_app_server', return_value=(good, '')):
            r = meter.read_codex_raw(rollout_fallback=False)
        self.assertEqual(r['source'], 'app-server')

    def test_rollout_fallback_policy(self):
        self.assertTrue(meter.rollout_fallback_enabled({}))
        self.assertTrue(meter.rollout_fallback_enabled({'meter': {'codex_rollout_fallback': True}}))
        self.assertFalse(meter.rollout_fallback_enabled({'meter': {'codex_rollout_fallback': False}}))
        # malformed fails closed: no session files
        for bad in ('true', 1, None, 'yes'):
            self.assertFalse(meter.rollout_fallback_enabled({'meter': {'codex_rollout_fallback': bad}}))

    def _read_with_failed_server(self, policy, **kw):
        sessions = self._rollout_fixture()
        real = meter.rollout_reading
        with mock.patch.object(meter, 'read_app_server', return_value=(None, 'app-server exited 1')),                 mock.patch.object(meter, 'rollout_reading', side_effect=lambda root=None, **_: real(sessions)):
            return meter.read(fresh=True, policy=policy, _claude_reader=lambda: claude_raw(12), **kw)

    def test_read_follows_policy_and_the_argument(self):
        self.assertEqual(self._read_with_failed_server(self.policy)['codex']['source'], 'rollout')
        shutil.rmtree(os.path.join(self.tmp, 'sessions'))
        off = dict(self.policy, meter=dict(self.policy.get('meter') or {}, codex_rollout_fallback=False))
        m = self._read_with_failed_server(off)
        self.assertEqual(m['codex']['status'], 'unknown')
        self.assertNotEqual(m['codex'].get('source'), 'rollout')
        shutil.rmtree(os.path.join(self.tmp, 'sessions'))
        m = self._read_with_failed_server(self.policy, rollout_fallback=False)
        self.assertEqual(m['codex']['status'], 'unknown')
        # a failed read never reaches the allowance cache: it keeps the earlier genuine (rollout) reading byte for byte
        # and the failure goes to its own sidecar, so the two later failed reads read back as unknown, not as rollout
        self.assertEqual(common.read_json(meter.meter_path())['codex'].get('source'), 'rollout')
        failed = common.read_json(meter._failed_observation_path())
        self.assertEqual(failed['codex']['status'], 'unknown')
        self.assertNotEqual(failed['codex'].get('source'), 'rollout')

    def test_cli_no_rollout_flag(self):
        sessions = self._rollout_fixture()
        with mock.patch.object(meter, 'read_app_server', return_value=(None, 'app-server exited 1')),                 mock.patch.object(meter, 'rollout_reading', side_effect=AssertionError('rollouts opened')),                 mock.patch.object(meter, 'read_claude_raw', return_value=claude_raw(12)),                 mock.patch('sys.stdout', new_callable=io.StringIO) as out:
            rc = meter.cli(['--fresh', '--no-rollout', '--json'])
        self.assertEqual(rc, common.EXIT_OK)
        self.assertEqual(json.loads(out.getvalue())['codex']['status'], 'unknown')
        self.assertTrue(os.path.isdir(sessions))

    def test_app_server_hang_is_killed_at_deadline(self):
        pidfile = os.path.join(self.tmp, 'pid.txt')
        argv = self._fake_server(f'''
import os, time
open({pidfile!r}, 'w').write(str(os.getpid()))
time.sleep(60)
''')
        t0 = time.monotonic()
        r, why = meter.read_app_server(_argv=argv, timeout=1.5)
        self.assertIsNone(r)
        self.assertIn('app-server failed', why)
        self.assertLess(time.monotonic() - t0, 12)
        with open(pidfile) as h:
            pid = int(h.read())
        self.assertFalse(common.pid_alive(pid))

    def test_app_server_error_reply(self):
        argv = self._fake_server('''
import json, sys
for line in sys.stdin:
    msg = json.loads(line)
    if 'id' in msg:
        print(json.dumps({"id": msg["id"], "error": {"code": -32600, "message": "nope"}}), flush=True)
''')
        r, why = meter.read_app_server(_argv=argv, timeout=5)
        self.assertIsNone(r)
        self.assertIn('-32600', why)


class ReadAndCacheTests(TempState):
    def test_read_uses_injected_readers_and_writes_cache(self):
        m = meter.read(fresh=True, policy=self.policy, _claude_reader=lambda: claude_raw(12),
                       _codex_reader=lambda: codex_raw(30))
        self.assertEqual(m['claude']['status'], 'ok')
        self.assertEqual(m['codex']['status'], 'ok')
        self.assertEqual(common.read_json(meter.meter_path())['claude']['max_pct'], 12)

    def test_cache_within_ttl_skips_readers(self):
        meter.read(fresh=True, policy=self.policy, _claude_reader=lambda: claude_raw(12),
                   _codex_reader=lambda: codex_raw(30))
        rc, rx = mock.Mock(), mock.Mock()
        m = meter.read(policy=self.policy, _claude_reader=rc, _codex_reader=rx)
        rc.assert_not_called()
        rx.assert_not_called()
        self.assertEqual(m['codex']['max_pct'], 30)

    def test_expired_cache_rereads(self):
        meter.read(fresh=True, policy=self.policy, _claude_reader=lambda: claude_raw(12),
                   _codex_reader=lambda: codex_raw(30))
        m = common.read_json(meter.meter_path())
        m['read_at'] = iso_in(-121)
        common.write_json(meter.meter_path(), m)
        m2 = meter.read(policy=self.policy, _claude_reader=lambda: claude_raw(50),
                        _codex_reader=lambda: codex_raw(60))
        self.assertEqual(m2['claude']['max_pct'], 50)

    def test_reader_exception_is_unknown(self):
        def boom():
            raise RuntimeError('kaboom')

        m = meter.read(fresh=True, policy=self.policy, _claude_reader=boom, _codex_reader=lambda: codex_raw(30))
        self.assertEqual(m['claude']['status'], 'unknown')
        self.assertIn('kaboom', m['claude']['note'])
        self.assertEqual(m['codex']['status'], 'ok')


class RecordClaudeTests(TempState):
    DESKTOP = {'plan': {'windows': [{'label': '5-hour limit', 'percentUsed': 81, 'resetsAt': None},
                                    {'label': 'Weekly · all models', 'percentUsed': 44, 'resetsAt': None}],
                        'extraUsage': {'enabled': False}}}

    def test_record_desktop_shape_via_cli(self):
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = meter.cli(['--record-claude', json.dumps(self.DESKTOP, ensure_ascii=False), '--json'])
        self.assertEqual(rc, 0)
        sec = json.loads(buf.getvalue())
        self.assertEqual(sec['status'], 'handoff')
        self.assertEqual(sec['source'], 'desktop')
        cache = common.read_json(meter.claude_cache_path())
        self.assertEqual(cache['source'], 'desktop')
        self.assertEqual([w['name'] for w in cache['windows']], ['five_hour', 'seven_day'])

    def test_record_oauth_shape_from_file(self):
        path = os.path.join(self.tmp, 'usage.json')
        shutil.copy(os.path.join(FIX, 'oauth_usage.json'), path)
        with redirect_stdout(io.StringIO()):
            rc = meter.cli(['--record-claude', '@' + path])
        self.assertEqual(rc, 0)
        self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'statusline')

    def test_record_patches_existing_meter_cache(self):
        meter.read(fresh=True, policy=self.policy, _claude_reader=lambda: {'source': 'none'},
                   _codex_reader=lambda: codex_raw(30))
        meter.record_claude(self.DESKTOP, self.policy)
        m = common.read_json(meter.meter_path())
        self.assertEqual(m['claude']['max_pct'], 81.0)
        self.assertEqual(m['codex']['max_pct'], 30)

    def test_record_bad_json_is_usage_error(self):
        with redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(meter.cli(['--record-claude', '{"nothing": true}']), common.EXIT_USAGE)
            self.assertEqual(meter.cli(['--record-claude', 'not json']), common.EXIT_USAGE)


class FormatTests(unittest.TestCase):
    def test_format_line_example(self):
        now = common.now()
        today = lambda h, mi: common.iso(now.replace(hour=h, minute=mi, second=0, microsecond=0))
        m = {'claude': {'status': 'ok', 'source': 'oauth', 'age_s': 3, 'extra_usage_enabled': False,
                        'windows': [{'name': 'five_hour', 'pct': 12.0, 'resets_at': today(15, 9)},
                                    {'name': 'seven_day', 'pct': 44.0, 'resets_at': today(20, 59)},
                                    {'name': 'seven_day_opus', 'pct': 0.0, 'resets_at': None}]},
             'codex': {'status': 'ok', 'source': 'app-server', 'age_s': 2,
                       'windows': [{'name': 'weekly', 'pct': 30, 'resets_at': '2099-10-14T05:37:00Z'}],
                       'credits': {'has': True}, 'on_credits': False}}
        # Every window the router counts is listed (the 0 % one too) and the binding one is marked.
        self.assertEqual(meter.format_line(m),
                         'claude: ok 5h 12% (resets 15:09Z) · 7d 44% (20:59Z) [binding] · 7d-opus 0% (?) · '
                         'extra off [oauth, 3s old] | '
                         'codex: ok weekly 30% (10-14 05:37Z) [binding] · credits yes [app-server, 2s old]')

    def test_an_unrecognised_window_is_listed_with_its_own_name_and_marked_binding(self):
        # The real cache holds `iguana_necktie`, which sets the maximum.
        sec = {'status': 'ok', 'source': 'oauth', 'age_s': 3, 'extra_usage_enabled': False,
               'windows': [{'name': 'five_hour', 'pct': 20.0, 'resets_at': None},
                           {'name': 'seven_day', 'pct': 18.0, 'resets_at': None},
                           {'name': 'iguana_necktie', 'pct': 28.728186, 'resets_at': None}]}
        line = meter.format_line({'claude': sec, 'codex': None})
        self.assertIn('iguana_necktie (unrecognised window) 28.7% (?) [binding]', line)
        self.assertEqual(line.count('[binding]'), 1)
        self.assertNotIn('5h 20% (?) [binding]', line)
        self.assertIn('7d 18%', line)

    def test_a_seven_day_sonnet_window_that_controls_routing_is_in_the_line(self):
        sec = {'status': 'stop', 'source': 'oauth', 'age_s': 3,
               'windows': [{'name': 'five_hour', 'pct': 5.0}, {'name': 'seven_day', 'pct': 15.0},
                           {'name': 'seven_day_sonnet', 'pct': 90.0}]}
        line = meter.format_line({'claude': sec, 'codex': None})
        self.assertIn('7d-sonnet 90% (?) [binding]', line)
        self.assertNotIn('(unrecognised window)', line)

    def test_an_unprintable_window_name_is_never_printed_and_says_so(self):
        sec = {'status': 'ok', 'source': 'oauth', 'age_s': 3,
               'windows': [{'name': 'five_hour', 'pct': 5.0}, {'name': 'x\nINJECT: ok', 'pct': 50.0},
                           {'name': 'y' * 41, 'pct': 7.0}]}
        line = meter.format_line({'claude': sec, 'codex': None})
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn('(invalid window name) 50% (?) [binding]', line)
        self.assertNotIn('INJECT', line)
        self.assertNotIn('y' * 41, line)

    def test_finish_claude_names_the_binding_window_in_the_section(self):
        policy = common.load_policy()
        raw = {'source': 'oauth', 'reading_at': common.iso(common.now()), 'extra_usage_enabled': False,
               'windows': [{'name': 'five_hour', 'pct': 20.0, 'resets_at': None},
                           {'name': 'iguana_necktie', 'pct': 28.7, 'resets_at': None}]}
        sec = meter.finish_claude(raw, policy)
        self.assertEqual(sec['binding_window'], {'name': 'iguana_necktie', 'pct': 28.7, 'recognised': False})
        self.assertEqual([(w['name'], w['recognised'], w.get('binding', False)) for w in sec['windows']],
                         [('five_hour', True, False), ('iguana_necktie', False, True)])
        self.assertIsNone(meter.finish_claude({'source': 'none'}, policy)['binding_window'])

    def test_binding_ties_take_the_first_window_and_bad_percents_are_skipped(self):
        wins = [{'name': 'a', 'pct': None}, {'name': 'five_hour', 'pct': 40.0}, {'name': 'seven_day', 'pct': 40.0},
                {'name': 'b', 'pct': True}, {'name': 'c', 'pct': float('nan')}, 'junk']
        self.assertEqual(meter.binding_index(wins), 1)
        self.assertIsNone(meter.binding_index([{'name': 'a', 'pct': 'x'}]))
        self.assertIsNone(meter.binding_index(None))

    def test_a_codex_limit_window_is_marked_binding_on_its_detail_line(self):
        r = meter.parse_app_server(json.loads(fixture('app_server_ratelimits_full.json'))['result'])
        sec = dict(r, status='ok', source='app-server', age_s=1)
        sec['windows'] = [dict(w, pct=95.0 if w['name'] == 'codex_astra:weekly' else w['pct']) for w in sec['windows']]
        line = meter.format_line({'claude': None, 'codex': sec})
        self.assertEqual(line.count('[binding]'), 1)
        self.assertRegex(line, r'codex limit [^\n]*weekly 95%[^\n]*\[binding\]')

    def test_a_crafted_codex_limit_name_cannot_break_the_meter_text(self):
        # A newline in a limit window name reached `twinlane meter` text unchanged.
        r = meter.parse_app_server(json.loads(fixture('app_server_ratelimits_full.json'))['result'])
        sec = dict(r, status='ok', source='app-server', age_s=1)
        lid = next(w['limit'] for w in sec['windows'] if w.get('limit'))
        sec['windows'] = [dict(w, name='x:weekly\nFAKE: claude ok') if w.get('limit') == lid else w
                          for w in sec['windows']]
        sec['limits'] = [dict(lim, name='bad\rname') if lim.get('id') == lid else lim for lim in sec['limits'] or []]
        line = meter.format_line({'claude': None, 'codex': sec})
        self.assertNotIn('FAKE', line)
        self.assertNotIn('\r', line)
        self.assertIn(meter.INVALID_WINDOW, line)

    def test_unknown_engine_line_has_note(self):
        m = {'claude': {'status': 'unknown', 'source': 'none', 'windows': [], 'note': 'CLI OAuth token expired'},
             'codex': {'status': 'ok', 'source': 'app-server', 'age_s': 2, 'windows': [], 'credits': {'has': False}}}
        out = meter.format_line(m)
        self.assertTrue(out.startswith('claude: unknown [none] | codex: ok credits no [app-server, 2s old]'))
        self.assertIn('note: claude: CLI OAuth token expired', out)


SECRETS = ('ACCOUNT-ID-SECRET', 'RESET-CREDIT-ID')


class CodexFullReplyTests(TempState):
    """The measured account/rateLimits/read reply: every limit, window, credit and free reset."""

    def full(self):
        return json.loads(fixture('app_server_ratelimits_full.json'))['result']

    def test_every_window_of_every_limit_id_is_kept_and_named(self):
        r = meter.parse_app_server(self.full())
        self.assertEqual([w['name'] for w in r['windows']], ['weekly', 'codex_astra:weekly', 'codex_astra:five_hour'])
        self.assertEqual([w.get('limit') for w in r['windows']], [None, 'codex_astra', 'codex_astra'])
        self.assertEqual([w['pct'] for w in r['windows']], [34.0, 12.0, 3.0])
        self.assertEqual([(x['id'], x['name'], x['default']) for x in r['limits']],
                         [('codex', None, True), ('codex_astra', 'GPT-6 Astra', False)])
        self.assertEqual(r['plan'], 'pro')
        self.assertIs(r['spend_control_reached'], False)
        self.assertIsNone(r['individual_limit'])

    def test_account_and_reset_credit_ids_are_never_copied(self):
        r = meter.parse_app_server(self.full())
        sec = meter.finish_codex(r, self.policy)
        text = json.dumps(r) + json.dumps(sec) + meter.format_line({'claude': {}, 'codex': sec})
        for secret in SECRETS:
            self.assertNotIn(secret, text)

    def test_balance_is_a_number(self):
        for given, want in ((42.5, 42.5), ('125.50', 125.5), ('$1,200.00', 1200.0), (0, 0.0), (None, None),
                            ('n/a', None), (True, None), ({}, None)):
            r = meter.parse_app_server({'rateLimits': {'credits': {'hasCredits': True, 'balance': given}}})
            self.assertEqual(r['credits']['balance'], want, given)

    def test_free_resets_count_each_expiry_and_soonest(self):
        sec = meter.finish_codex(meter.parse_app_server(self.full()), self.policy)
        fr = sec['free_resets']
        self.assertEqual(fr['available'], 2)
        self.assertEqual(fr['expires'], ['2099-10-28T00:00:00Z', '2099-11-02T00:00:00Z'])
        self.assertEqual(fr['soonest_expiry'], '2099-10-28T00:00:00Z')

    def test_spent_and_past_resets_are_not_listed(self):
        res = self.full()
        creds = res['rateLimitResetCredits']['credits']
        creds[0]['status'] = 'consumed'
        creds[1]['expiresAt'] = '2020-01-01T00:00:00Z'
        res['rateLimitResetCredits']['availableCount'] = 1
        sec = meter.finish_codex(meter.parse_app_server(res), self.policy)
        self.assertEqual(sec['free_resets'], {'available': 1, 'expires': [], 'soonest_expiry': None})

    def test_reset_count_without_details_still_shows(self):
        res = self.full()
        res['rateLimitResetCredits'] = {'availableCount': 3}
        sec = meter.finish_codex(meter.parse_app_server(res), self.policy)
        self.assertEqual(sec['free_resets']['available'], 3)
        self.assertIn('  codex: plan pro · 3 free full resets', meter.format_line({'claude': {}, 'codex': sec}))

    def test_spend_control_reached_is_out_even_when_credits_are_allowed(self):
        res = self.full()
        res['rateLimits']['spendControlReached'] = True
        for allow in (False, True):
            p = policy(codex__allow_credits=allow)
            sec = meter.finish_codex(meter.parse_app_server(res), p)
            self.assertEqual(sec['status'], 'out', allow)
            self.assertIn('spend control reached', sec['note'])

    def test_spend_control_and_ordinary_usage_stop_the_router(self):
        from twinlane import route
        pol = self.policy
        for field, value in (('spendControlReached', True), ('ordinaryUsageAllowed', False)):
            res = self.full()
            if field == 'ordinaryUsageAllowed':
                res[field] = value
            else:
                res['rateLimits'][field] = value
            m = {'claude': meter.finish_claude(claude_raw(10), pol),
                 'codex': meter.finish_codex(meter.parse_app_server(res), pol)}
            self.assertFalse(route.usable('codex', m['codex'], pol, urgent=True), field)
            d = route.decide('parallel', m, pol, urgent=True)
            self.assertEqual(d['engine'], 'claude', field)  # failed over: codex never chosen
            self.assertTrue(route.decide('parallel', m, pol, engine='codex')['refused'], field)

    def test_individual_limit_is_kept_when_set_and_ids_dropped(self):
        res = self.full()
        res['rateLimits']['individualLimit'] = {'amount': 50, 'currency': 'USD', 'id': 'RESET-CREDIT-ID-9'}
        sec = meter.finish_codex(meter.parse_app_server(res), self.policy)
        self.assertEqual(sec['individual_limit'], {'amount': 50, 'currency': 'USD'})
        self.assertIn('individual limit {"amount": 50, "currency": "USD"}',
                      meter.format_line({'claude': {}, 'codex': sec}))

    def test_default_limit_taken_from_the_by_id_map_when_rate_limits_is_missing(self):
        res = self.full()
        del res['rateLimits']
        r = meter.parse_app_server(res)
        self.assertEqual([w['name'] for w in r['windows']], ['weekly', 'codex_astra:weekly', 'codex_astra:five_hour'])

    def test_format_shows_everything_usage_relevant(self):
        sec = meter.finish_codex(meter.parse_app_server(self.full()), self.policy)
        text = meter.format_line({'claude': meter.finish_claude(claude_raw(12), self.policy), 'codex': sec})
        lines = text.splitlines()
        self.assertIn('credits yes (42.5)', lines[0])
        self.assertNotIn('codex_astra', lines[0])  # other limit ids live on their own line
        self.assertIn('  codex: plan pro · 2 free full resets (soonest expires 10-28; also 11-02)', lines)
        self.assertTrue(any(l.startswith('  codex limit GPT-6 Astra [codex_astra]: weekly 12% (')
                            and 'five_hour 3%' in l for l in lines), lines)

    def test_one_reset_and_unlimited_credits_wording(self):
        res = self.full()
        res['rateLimitResetCredits'] = {'availableCount': 1, 'credits': [res['rateLimitResetCredits']['credits'][1]]}
        res['rateLimits']['credits'] = {'hasCredits': True, 'unlimited': True, 'balance': None}
        sec = meter.finish_codex(meter.parse_app_server(res), self.policy)
        text = meter.format_line({'claude': {}, 'codex': sec})
        self.assertIn('1 free full reset (expires 10-28)', text)
        self.assertIn('credits yes unlimited', text)

    def test_spend_control_shows_on_the_first_line(self):
        res = self.full()
        res['rateLimits']['spendControlReached'] = True
        sec = meter.finish_codex(meter.parse_app_server(res), self.policy)
        self.assertIn('SPEND CONTROL REACHED', meter.format_line({'claude': {}, 'codex': sec}).splitlines()[0])

    def test_json_output_carries_the_new_fields(self):
        sec = meter.finish_codex(meter.parse_app_server(self.full()), self.policy)
        m = {'read_at': common.iso(common.now()), 'claude': meter.finish_claude(claude_raw(5), self.policy), 'codex': sec}
        common.write_json(meter.meter_path(), m)
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.assertEqual(meter.cli(['--json']), 0)
        out = json.loads(buf.getvalue())['codex']
        for key in ('limits', 'spend_control_reached', 'individual_limit', 'free_resets', 'plan', 'credits'):
            self.assertIn(key, out)
        self.assertEqual(out['credits']['balance'], 42.5)

    def test_the_exchange_asks_only_for_read_only_methods(self):
        log = os.path.join(self.tmp, 'methods.txt')
        body = json.dumps(self.full())
        path = os.path.join(self.tmp, 'fake_full.py')
        with open(path, 'w', encoding='utf-8') as h:
            h.write(f'''
import json, sys
log = open({log!r}, 'w', encoding='utf-8')
for line in sys.stdin:
    msg = json.loads(line)
    log.write(msg.get('method', '') + '\\n'); log.flush()
    if msg.get('id') == 1:
        print(json.dumps({{"id": 1, "result": {{"userAgent": "fake"}}}}), flush=True)
    elif msg.get('id') == 2:
        print(json.dumps({{"id": 2, "result": json.loads({body!r})}}), flush=True)
''')
        r, why = meter.read_app_server(_argv=[sys.executable, '-I', path], timeout=10)
        self.assertEqual(why, '')
        self.assertEqual(len(r['windows']), 3)
        with open(log, encoding='utf-8') as h:
            self.assertEqual(h.read().split(), ['initialize', 'initialized', 'account/rateLimits/read'])

    def test_no_code_path_names_a_credit_spending_method(self):
        import re
        with open(meter.__file__, encoding='utf-8') as h:
            src = h.read()
        self.assertIsNone(re.search(r'''['"]account/(rateLimitResetCredit|sendAddCreditsNudgeEmail)''', src))


class AutoRefreshTests(TempState):
    """An expired CLI token: status/route/run renew it once per expiry; the hook and `twinlane meter` never do."""

    def setUp(self):
        super().setUp()
        self.policy['meter']['auto_refresh_claude'] = True
        self.calls = []
        self.renew = True
        self.record_extra_off()  # the refresh is a model call: it needs a recorded reading that shows extra usage OFF

    def fake_run(self, argv, cwd=None, timeout=30, env=None, input_text=None):
        self.calls.append(argv)
        if self.renew:
            self.write_creds(3600 + len(self.calls))  # what the real CLI does: a fresh token
        return 0, json.dumps({'usage': {'input_tokens': 9, 'output_tokens': 2}}), ''

    def read(self, pol=None, **kw):
        kw.setdefault('auto_refresh', True)
        return meter.read(fresh=kw.pop('fresh', True), policy=pol or self.policy, _codex_reader=lambda: codex_raw(30),
                          _run=self.fake_run, _http=lambda *a: (200, fixture('oauth_usage.json')), **kw)

    def markers(self):
        return (common.read_json(meter._claims_path(), {}) or {}).get('claimed', [])

    def test_expired_token_is_refreshed_automatically_once_and_says_so(self):
        self.write_creds(-60)
        with mock.patch.object(common, 'claude_bin', return_value='\x43:/bin/claude.exe'):
            m = self.read()
        self.assertEqual(len(self.calls), 1)
        self.assertIn('claude-haiku-4-5', self.calls[0])
        self.assertEqual(m['claude']['source'], 'oauth')
        self.assertNotEqual(m['claude']['status'], 'unknown')
        self.assertEqual(m['refreshed_now'], ['claude'])
        self.assertTrue(m['claude']['refresh']['ok'])
        self.assertIn('refreshed automatically', meter.refresh_notice(m))
        self.assertEqual(len(self.markers()), 1)
        self.assertIn('claude: CLI token had expired; refreshed automatically', meter.format_line(m))
        self.assertNotIn('refreshed_now', common.read_json(meter.meter_path()))
        self.assertNotIn(FAKE_TOKEN, self.state_text())

    def test_only_once_per_expiry_even_when_the_refresh_did_not_help(self):
        self.write_creds(-60)
        self.renew = False
        m1 = self.read()
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(m1['claude']['status'], 'unknown')
        self.assertFalse(m1['claude']['refresh']['ok'])
        self.assertIn('did not restore', meter.refresh_notice(m1))
        for _ in range(3):  # repeated status/route/run: no further call
            m = self.read()
            self.assertEqual(len(self.calls), 1)
            self.assertNotIn('refreshed_now', m)
            self.assertIn('already tried', m['claude']['note'])
        self.write_creds(-120)  # a different expiry earns one more try
        self.read()
        self.assertEqual(len(self.calls), 2)
        self.assertEqual(len(self.markers()), 2)  # both expiries stay recorded

    def test_claiming_a_later_expiry_never_re_enables_an_earlier_one(self):
        a, b = '2026-10-09T01:00:00Z', '2026-10-09T02:00:00Z'
        self.assertTrue(meter._claim_refresh(a))
        self.assertTrue(meter._claim_refresh(b))
        self.assertIsNone(meter._claim_refresh(a))  # A is still taken after B was claimed
        self.assertFalse(meter.refresh_pending(a))
        self.assertFalse(meter.refresh_pending(b))
        self.assertEqual(len(self.markers()), 2)

    def test_token_returning_to_an_old_expiry_does_not_spend_a_second_call(self):
        self.renew = False
        self.write_creds(-120)  # expiry A
        credentials = os.path.join(self.claude_dir, '.credentials.json')
        with open(credentials, 'rb') as handle:
            original = handle.read()
        self.read()
        self.write_creds(-60)   # expiry B
        self.read()
        self.assertEqual(len(self.calls), 2)
        with open(credentials, 'wb') as handle:
            handle.write(original)  # exactly A again, as if the old file was restored
        self.read()
        self.assertEqual(len(self.calls), 2)

    # --- a claimed expiry is never pending again, however many later ones are claimed

    @staticmethod
    def expiry(i):
        return common.iso(dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(hours=i))

    def test_the_claims_file_is_bounded_and_no_claim_is_ever_forgotten(self):
        n = meter.REFRESH_CLAIMS_KEEP + 15
        orders = {'ascending': list(range(n)), 'descending': list(range(n))[::-1],
                  'shuffled': random.Random(7).sample(range(n), n)}
        for name, order in orders.items():
            with self.subTest(order=name):
                if os.path.exists(meter._claims_path()):
                    os.remove(meter._claims_path())
                claimed = []
                for i in order:
                    pending = meter.refresh_pending(self.expiry(i))
                    won = bool(meter._claim_refresh(self.expiry(i)))
                    self.assertEqual(won, pending)  # an expiry at or below the floor reads as claimed already
                    claimed.append(i)
                    doc = common.read_json(meter._claims_path())
                    self.assertLessEqual(len(doc['claimed']), meter.REFRESH_CLAIMS_KEEP)
                    if won:
                        self.assertIn(meter._expiry_key(self.expiry(i)), doc['claimed'])  # the current one is kept
                    for j in claimed:  # every earlier claim, pruned from the list or not, is still taken
                        self.assertFalse(meter.refresh_pending(self.expiry(j)), (name, i, j))
                for j in claimed:
                    self.assertIsNone(meter._claim_refresh(self.expiry(j)), (name, j))
                self.assertEqual(len(common.read_json(meter._claims_path())['claimed']), meter.REFRESH_CLAIMS_KEEP)
                if name == 'ascending':  # real expiries only move forward: every one of them was claimable
                    self.assertEqual(common.read_json(meter._claims_path())['floor'],
                                     meter._expiry_key(self.expiry(n - 1 - meter.REFRESH_CLAIMS_KEEP)))

    def test_an_old_expiry_whose_claim_was_pruned_does_not_cost_a_second_model_call(self):
        self.renew = False
        self.write_creds(-3600)  # expiry A, claimed by the real flow
        self.read()
        self.assertEqual(len(self.calls), 1)
        a = meter.token_expired_at()
        for i in range(meter.REFRESH_CLAIMS_KEEP + 5):  # later expiries push A out of the listed claims
            self.assertTrue(meter._claim_refresh(self.expiry(i + 10_000)))
        self.assertNotIn(meter._expiry_key(a), common.read_json(meter._claims_path())['claimed'])
        self.assertFalse(meter.refresh_pending(a))
        self.read()  # the credentials still say A
        self.assertEqual(len(self.calls), 1)

    def test_a_new_later_expiry_is_still_claimable_after_many_claims(self):
        for i in range(meter.REFRESH_CLAIMS_KEEP + 20):
            meter._claim_refresh(self.expiry(i))
        later = self.expiry(meter.REFRESH_CLAIMS_KEEP + 500)
        self.assertTrue(meter.refresh_pending(later))
        self.assertTrue(meter._claim_refresh(later))

    def test_an_unreadable_claims_file_fails_closed(self):
        for body in ('{ not json', '[]', '{"claimed": "x"}', '{"claimed": [1.5], "floor": null}',
                     '{"claimed": [], "floor": "soon"}'):
            with self.subTest(body=body):
                with open(meter._claims_path(), 'w') as h:
                    h.write(body)
                self.assertFalse(meter.refresh_pending(self.expiry(1)))
                self.assertIsNone(meter._claim_refresh(self.expiry(1)))
                with open(meter._claims_path()) as h:
                    self.assertEqual(h.read(), body)  # never rewritten, so no claim is lost

    def test_concurrent_claimers_of_one_expiry_spend_exactly_one_call(self):
        wins, barrier = [], threading.Barrier(8)

        def claim():
            barrier.wait(5)
            wins.append(bool(meter._claim_refresh(self.expiry(3))))

        threads = [threading.Thread(target=claim) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(sorted(wins), [False] * 7 + [True])

    def test_policy_flag_off_never_refreshes(self):
        self.write_creds(-60)
        m = self.read(policy(meter__auto_refresh_claude=False))
        self.assertEqual(self.calls, [])
        self.assertEqual(m['claude']['status'], 'unknown')

    def test_flag_defaults_off_when_the_policy_has_no_key(self):
        pol = self.policy
        pol['meter'] = {k: v for k, v in pol['meter'].items() if k != 'auto_refresh_claude'}
        self.assertFalse(meter.auto_refresh_enabled(pol))
        self.assertFalse(meter.auto_refresh_enabled({}))
        self.assertIs(common.read_json(common.POLICY)['meter']['auto_refresh_claude'], False)

    def test_plain_reads_and_the_meter_cli_never_spend_a_call(self):
        """`twinlane meter --fresh` is what the hook spawns: it must stay model-free."""
        self.write_creds(-60)
        with mock.patch.object(meter, '_refresh_token') as refresh, \
                mock.patch.object(meter, 'read_codex_raw', return_value=codex_raw(30)):
            meter.read(fresh=True, policy=self.policy)
            with redirect_stdout(io.StringIO()):
                meter.cli(['--fresh', '--json'])
                meter.cli([])
        refresh.assert_not_called()
        self.assertEqual(self.markers(), [])

    def test_the_hook_spawn_command_is_model_free(self):
        from twinlane import hook
        spawned = []
        hook.spawn_refresh(self.state, spawn=lambda argv, **kw: spawned.append(argv) or 1)
        self.assertEqual(spawned[0][-3:], ['meter', '--fresh', '--json'])
        self.assertNotIn('--refresh', spawned[0])

    def test_a_valid_cached_reading_needs_no_refresh(self):
        self.write_creds(-60)
        meter.save_claude_cache(claude_raw(20), 'desktop')
        m = self.read()
        self.assertEqual(self.calls, [])
        self.assertEqual(m['claude']['status'], 'ok')
        self.assertEqual(self.markers(), [])

    def test_a_cached_unknown_expired_reading_is_re_read_while_a_refresh_is_pending(self):
        self.write_creds(-60)
        self.renew = False
        meter.read(fresh=True, policy=self.policy, _codex_reader=lambda: codex_raw(30))  # plain read: unknown cached
        self.assertEqual(common.read_json(meter.meter_path())['claude']['status'], 'unknown')
        plain = meter.read(policy=self.policy, _codex_reader=lambda: codex_raw(30), _run=self.fake_run)
        self.assertEqual(self.calls, [])  # within the TTL a plain read keeps the cache
        self.assertEqual(plain['claude']['status'], 'unknown')
        auto = meter.read(policy=self.policy, auto_refresh=True, _codex_reader=lambda: codex_raw(30),
                          _run=self.fake_run)  # no fresh flag: the cache would have answered
        self.assertEqual(len(self.calls), 1)
        self.assertEqual(auto['refreshed_now'], ['claude'])
        meter.read(policy=self.policy, auto_refresh=True, _codex_reader=lambda: codex_raw(30), _run=self.fake_run)
        self.assertEqual(len(self.calls), 1)  # marker taken: the cache answers again

    def test_the_claim_is_atomic(self):
        first = meter._claim_refresh('2026-10-09T01:00:00Z')
        self.assertTrue(first and os.path.exists(first))
        self.assertIsNone(meter._claim_refresh('2026-10-09T01:00:00Z'))
        self.assertFalse(meter.refresh_pending('2026-10-09T01:00:00Z'))
        self.assertTrue(meter.refresh_pending('2026-10-09T02:00:00Z'))

    def test_an_unwritable_marker_means_no_call(self):
        self.write_creds(-60)
        blocker = os.path.join(self.tmp, 'a-file')
        open(blocker, 'w').close()  # the claims file's parent is a regular file: it can never be created
        with mock.patch.object(meter, '_claims_path', return_value=os.path.join(blocker, 'm.json')):
            self.read()
        self.assertEqual(self.calls, [])

    def test_no_token_expiry_means_no_auto_refresh(self):
        # No credentials at all, or a token that is still valid but the call failed: a refresh would not help.
        m = self.read()
        self.assertEqual(self.calls, [])
        self.write_creds(3600)
        m = meter.read(fresh=True, policy=self.policy, auto_refresh=True, _codex_reader=lambda: codex_raw(30),
                       _run=self.fake_run, _http=lambda *a: (500, ''))
        self.assertEqual(self.calls, [])
        self.assertEqual(m['claude']['status'], 'unknown')

    def test_route_uses_auto_refresh_but_a_forced_codex_route_does_not(self):
        from twinlane import route
        seen = []

        def fake_read(**kw):
            seen.append(kw)
            return {'read_at': common.iso(common.now()), 'refreshed_now': ['claude'],
                    'claude': dict(meter.finish_claude(claude_raw(10), self.policy), refresh={'ok': True}),
                    'codex': meter.finish_codex(codex_raw(10), self.policy)}

        out = io.StringIO()
        with mock.patch.object(meter, 'read', fake_read), redirect_stdout(out):
            self.assertEqual(route.cli(['plan']), 0)
            self.assertEqual(route.cli(['plan', '--engine', 'codex']), 0)
            self.assertEqual(route.cli(['plan', '--json']), 0)
        self.assertEqual([k['auto_refresh'] for k in seen], [True, False, True])
        self.assertIn('note: Claude CLI token had expired: refreshed automatically', out.getvalue())
        self.assertIn('"meter_refresh"', out.getvalue())

    def test_status_command_reads_only_cached_observations(self):
        from twinlane import cli
        with mock.patch.object(meter, 'read', side_effect=AssertionError('probe')), \
                mock.patch.object(meter, 'cached_observation', return_value={}) as cached, \
                redirect_stdout(io.StringIO()), \
                mock.patch.object(common, 'JOBS', os.path.join(self.tmp, 'jobs')), \
                mock.patch.object(common, 'BATONS', os.path.join(self.tmp, 'batons')):
            self.assertEqual(cli.status(['--fresh']), 0)
        cached.assert_called_once_with()


class UsageHelperTests(TempState):
    def sec(self, pct, at=0, resets=3600, source='oauth', name='five_hour'):
        return {'source': source, 'status': 'ok', 'reading_at': iso_in(at),
                'windows': [{'name': name, 'pct': pct, 'resets_at': iso_in(resets)}]}

    def test_delta_between_two_readings(self):
        b = meter.usage_point(self.sec(10, -100))
        a = meter.usage_point(self.sec(12.5, 0))
        self.assertEqual(meter.usage_delta(b, a), {'delta': {'five_hour': 2.5}, 'skipped': None, 'why': None})

    def test_no_delta_without_two_different_readings(self):
        p = meter.usage_point(self.sec(10, -5))
        self.assertIn('no newer reading', meter.usage_delta(p, p)['why'])
        self.assertIn('no before reading', meter.usage_delta(None, p)['why'])
        self.assertIn('no after reading', meter.usage_delta(p, None)['why'])
        self.assertIsNone(meter.usage_point({'source': 'none'}))
        self.assertIsNone(meter.usage_point({'source': 'oauth', 'reading_at': iso_in(0), 'windows': []}))

    def test_a_window_that_reset_in_between_is_skipped(self):
        b = meter.usage_point(self.sec(90, -100, resets=10))
        a = meter.usage_point(self.sec(4, 0, resets=18000))
        d = meter.usage_delta(b, a)
        self.assertEqual(d['delta'], {})
        self.assertEqual(list(d['skipped']), ['five_hour'])

    def test_rolled_over_cached_window_is_skipped(self):
        sec = self.sec(80, -100, resets=-10)
        sec['windows'] = meter.effective_windows(sec['windows'])
        d = meter.usage_delta(meter.usage_point(sec), meter.usage_point(self.sec(1, 0)))
        self.assertEqual(d['delta'], {})

    def _rollout(self, sid, lines):
        d = os.path.join(self.tmp, 'sessions', '2026', '10', '09')
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f'rollout-2026-10-09T10-00-00-{sid}.jsonl'), 'w', encoding='utf-8') as h:
            h.write('\n'.join(lines) + '\n')

    def test_rollout_token_usage_takes_the_last_cumulative_count(self):
        sid = '00000000-0000-4000-8000-000000000024'

        def ev(total, out):
            return json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'total_token_usage': {'input_tokens': total, 'cached_input_tokens': 5, 'output_tokens': out,
                                      'reasoning_output_tokens': 1, 'total_tokens': total + out, 'junk': 'x'}}}})

        self._rollout(sid, [ev(100, 10), json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count',
                            'info': None}}), ev(300, 40), '{"bad json'])
        got = meter.rollout_token_usage(sid, os.path.join(self.tmp, 'sessions'))
        self.assertEqual(got, {'source': 'rollout', 'input_tokens': 300, 'cached_input_tokens': 5,
                               'output_tokens': 40, 'reasoning_output_tokens': 1, 'total_tokens': 340})

    def test_rollout_token_usage_none_cases(self):
        root = os.path.join(self.tmp, 'sessions')
        self.assertIsNone(meter.rollout_token_usage('00000000-0000-4000-8000-000000000024', root))
        self.assertIsNone(meter.rollout_token_usage(None, root))
        self.assertIsNone(meter.rollout_token_usage('../../etc/passwd', root))  # never a path
        self._rollout('aaaaaaaa-0000-0000-0000-000000000000', ['{"type":"event_msg","payload":{"type":"token_count"}}'])
        self.assertIsNone(meter.rollout_token_usage('aaaaaaaa-0000-0000-0000-000000000000', root))


class CacheMonotonicTests(TempState):
    """no writer, in any thread or process, replaces a cached reading with an older one."""

    def read_both(self, claude, codex, **kw):
        return meter.read(fresh=True, policy=self.policy, _claude_reader=claude, _codex_reader=codex, **kw)

    def test_claude_usage_file_matrix(self):
        # (existing reading_at age, incoming reading_at age, incoming wins?)  ages in seconds, bigger = older
        cases = [(10, 100, False), (10, 10, True), (100, 10, True), (None, 10, True)]
        for existing, incoming, wins in cases:
            with self.subTest(existing=existing, incoming=incoming):
                data = {'source': 'existing', 'windows': [], 'extra_usage_enabled': False}
                if existing is not None:
                    data['reading_at'] = iso_in(-existing)
                common.write_json(meter.claude_cache_path(), data)
                reading = claude_raw(20)
                reading['reading_at'] = iso_in(-incoming)
                meter.save_claude_cache(reading, 'incoming')
                self.assertEqual(common.read_json(meter.claude_cache_path())['source'],
                                 'incoming' if wins else 'existing')
        with self.subTest('unreadable existing file'):
            with open(meter.claude_cache_path(), 'w') as h:
                h.write('{ not json')
            meter.save_claude_cache(claude_raw(20), 'incoming')
            self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'incoming')

    def test_a_slow_reader_that_finishes_after_a_newer_reading_leaves_the_newer_one(self):
        """Both engines: the slow read began first and finishes last; the cache must keep the newer reading."""
        for pct_new, slow_age, slow_wins in ((90, 60, False), (90, 30, False), (90, -20, True)):  # older, tie, newer
            with self.subTest(slow_reading_age_s=slow_age):
                for name in os.listdir(self.state):
                    os.remove(os.path.join(self.state, name))
                release = threading.Event()
                started = threading.Event()

                def slow_claude():
                    r = claude_raw(20, age=slow_age)  # fetched now, delivered late
                    started.set()
                    release.wait(10)
                    return r

                def slow_codex():
                    r = codex_raw(20, age=slow_age)
                    release.wait(10)
                    return r

                t = threading.Thread(target=lambda: self.read_both(slow_claude, slow_codex))
                t.start()
                self.assertTrue(started.wait(5))
                time.sleep(0.05)  # the newer read begins strictly later (microsecond stamps)
                self.read_both(lambda: claude_raw(pct_new, age=30), lambda: codex_raw(pct_new, age=30))
                release.set()
                t.join(10)
                cached = common.read_json(meter.meter_path())
                want = 20 if slow_wins else pct_new
                self.assertEqual(cached['claude']['max_pct'], want)
                self.assertEqual(cached['codex']['max_pct'], want)

    def test_the_cached_meter_read_at_never_moves_backwards(self):
        self.read_both(lambda: claude_raw(40), lambda: codex_raw(40))
        newer = common.read_json(meter.meter_path())
        older = dict(newer, read_us=newer['read_us'] - 5_000_000, read_at=iso_in(-5))
        older['claude'] = dict(newer['claude'], max_pct=1)
        meter._cache_update(meter.meter_path(), lambda cur: meter._merge_meter(cur, older))
        kept = common.read_json(meter.meter_path())
        self.assertEqual(kept['read_us'], newer['read_us'])
        self.assertEqual(kept['claude']['max_pct'], 40)

    def test_an_older_rollout_fallback_cannot_replace_a_newer_codex_reading(self):
        self.read_both(lambda: claude_raw(10), lambda: codex_raw(90, age=2))
        old_rollout = codex_raw(5, age=3600)
        old_rollout['source'] = 'rollout'
        self.read_both(lambda: claude_raw(11), lambda: old_rollout)
        cached = common.read_json(meter.meter_path())
        self.assertEqual(cached['codex']['max_pct'], 90)   # kept
        self.assertEqual(cached['claude']['max_pct'], 11)  # the other engine still advances

    def test_a_failed_later_read_still_replaces_the_cache_with_unknown(self):
        """Monotonic is about reading time, not about hiding a failure: a LATER read that fails is shown."""
        self.read_both(lambda: claude_raw(10), lambda: codex_raw(90))
        self.read_both(lambda: {'source': 'none'}, lambda: codex_raw(90))
        self.assertEqual(common.read_json(meter.meter_path())['claude']['status'], 'unknown')

    def test_record_claude_with_an_older_reading_keeps_the_newer_one_in_both_files(self):
        meter.save_claude_cache(claude_raw(12, age=1), 'oauth')
        self.read_both(lambda: claude_raw(12, age=1), lambda: codex_raw(30))
        desktop = {'reading_at': iso_in(-3600),
                   'plan': {'windows': [{'label': '5-hour limit', 'percentUsed': 81, 'resetsAt': None}],
                            'extraUsage': {'enabled': False}}}
        section = meter.record_claude(desktop, self.policy)
        self.assertEqual(section['max_pct'], 12)
        self.assertEqual(common.read_json(meter.meter_path())['claude']['max_pct'], 12)
        self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'oauth')

    def test_threads_writing_in_any_order_leave_the_newest(self):
        ages = [5, 300, 40, 1000, 7, 90, 2, 600, 15, 250, 33, 120]
        barrier = threading.Barrier(len(ages))

        def writer(age):
            r = claude_raw(age % 100, age=age)
            barrier.wait(5)
            meter.save_claude_cache(r, 'w%d' % age)

        threads = [threading.Thread(target=writer, args=(a,)) for a in ages]
        for t in threads:
            t.start()
        for t in threads:
            t.join(15)
        self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'w2')  # age 2 s: the newest

    def test_processes_writing_in_any_order_leave_the_newest(self):
        import subprocess
        code = ("import sys; sys.path.insert(0, %r)\n"
                "from twinlane import common, meter\n"
                "import datetime as dt\n"
                "age = int(sys.argv[1])\n"
                "r = {'reading_at': common.iso(common.now() - dt.timedelta(seconds=age)), 'windows': [], "
                "'extra_usage_enabled': False}\n"
                "for _ in range(5): meter.save_claude_cache(r, 'p%%d' %% age)\n") % ROOT
        env = dict(os.environ, TWINLANE_HOME=os.path.dirname(self.state))
        # the state dir of this test is <tmp>/state, so <tmp> is the override
        procs = [subprocess.Popen([sys.executable, '-B', '-c', code, str(a)], env=env) for a in (400, 3, 90, 700, 20, 11)]
        for p in procs:
            self.assertEqual(p.wait(60), 0)
        self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'p3')

    def test_deferred_writes_touch_nothing_until_the_caller_runs_them(self):
        with meter.deferred_cache_writes() as pending:
            self.read_both(lambda: claude_raw(10), lambda: codex_raw(30))
            meter.save_claude_cache(claude_raw(10), 'oauth')
            self.assertEqual([n for n in os.listdir(self.state) if not n.endswith('.lock')], [])
        self.assertEqual(len(pending), 2)
        meter.run_deferred(pending)
        self.assertEqual(common.read_json(meter.meter_path())['codex']['max_pct'], 30)
        self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'oauth')
        meter.save_claude_cache(claude_raw(10), 'direct')  # outside the block: written at once again
        self.assertEqual(common.read_json(meter.claude_cache_path())['source'], 'direct')


if __name__ == '__main__':
    unittest.main()
