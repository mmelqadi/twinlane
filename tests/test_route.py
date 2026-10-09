"""route.decide: the full decision table from DESIGN.md. Pure function, no IO."""
import datetime as dt
import io
import itertools
import os
import sys
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, route  # noqa: E402

STATUSES = ('ok', 'caution', 'handoff', 'stop', 'out', 'unknown')
PCT_FOR = {'ok': 20.0, 'caution': 77.0, 'handoff': 82.0, 'stop': 90.0, 'out': 100.0, 'unknown': None}
CLAUDE_KINDS = ('code', 'intel', 'plan', 'review')
CODEX_KINDS = ('parallel', 'sweep', 'integrate', 'verify')
ALL_KINDS = CLAUDE_KINDS + CODEX_KINDS
POLICY = common.load_policy()


def iso_in(seconds):
    return common.iso(common.now() + dt.timedelta(seconds=seconds))


def sec(status, pct='default', reset_in=3600, extra=False):
    return {'status': status, 'max_pct': PCT_FOR[status] if pct == 'default' else pct,
            'next_reset': iso_in(reset_in), 'extra_usage_enabled': extra}


def meter(claude, codex):
    return {'read_at': iso_in(0), 'claude': claude, 'codex': codex}


def other(engine):
    return 'codex' if engine == 'claude' else 'claude'


class TableTests(unittest.TestCase):
    def test_policy_routing_kinds(self):
        for k in CLAUDE_KINDS:
            self.assertEqual(POLICY['routing'][k], {'primary': 'claude', 'fallback': 'codex'})
        for k in CODEX_KINDS:
            self.assertEqual(POLICY['routing'][k], {'primary': 'codex', 'fallback': 'claude'})

    def test_full_table_non_urgent(self):
        """Every kind x primary status x fallback status."""
        usable = {'ok', 'caution', 'handoff'}
        for kind, ps, fs in itertools.product(ALL_KINDS, STATUSES, STATUSES):
            primary = POLICY['routing'][kind]['primary']
            fallback = other(primary)
            m = meter(**{primary: sec(ps, reset_in=7200), fallback: sec(fs, reset_in=3600)})
            d = route.decide(kind, m, POLICY)
            with self.subTest(kind=kind, primary=ps, fallback=fs):
                if ps in usable:
                    self.assertEqual((d['engine'], d['fallback_used'], d['refused']), (primary, False, False))
                    self.assertIn(f'primary {primary} {ps}', d['reason'])
                elif fs in usable:
                    self.assertEqual((d['engine'], d['fallback_used'], d['refused']), (fallback, True, False))
                    self.assertIn(f'primary {primary} {ps}', d['reason'])  # names the primary's status
                else:
                    self.assertTrue(d['refused'])
                    self.assertIsNone(d['engine'])
                    self.assertEqual(d['wait_until'], m[fallback]['next_reset'])  # the earlier reset
                    self.assertIn('twinlane baton snap', d['advice'])

    def test_both_usable_takes_primary(self):
        for kind in ALL_KINDS:
            d = route.decide(kind, meter(sec('ok'), sec('ok')), POLICY)
            self.assertEqual(d['engine'], POLICY['routing'][kind]['primary'])
            self.assertIsNone(d['wait_until'])

    def test_primary_stop_out_unknown_falls_back(self):
        for ps in ('stop', 'out', 'unknown'):
            d = route.decide('code', meter(sec(ps), sec('ok')), POLICY)
            self.assertEqual(d['engine'], 'codex')
            self.assertTrue(d['fallback_used'])
            self.assertIn(f'primary claude {ps}', d['reason'])
            d = route.decide('sweep', meter(sec('ok'), sec(ps)), POLICY)
            self.assertEqual(d['engine'], 'claude')
            self.assertTrue(d['fallback_used'])


class UrgentTests(unittest.TestCase):
    def test_urgent_stop_between_85_and_100_is_usable(self):
        for pct in (85.0, 90.0, 99.9):
            d = route.decide('code', meter(sec('stop', pct), sec('out')), POLICY, urgent=True)
            self.assertEqual((d['engine'], d['fallback_used']), ('claude', False))
            self.assertIn('urgent', d['reason'])

    def test_urgent_prefers_primary_stop_over_fallback(self):
        d = route.decide('code', meter(sec('stop', 88.0), sec('ok')), POLICY, urgent=True)
        self.assertEqual(d['engine'], 'claude')

    def test_non_urgent_stop_is_not_usable(self):
        d = route.decide('code', meter(sec('stop', 88.0), sec('out')), POLICY)
        self.assertTrue(d['refused'])

    def test_urgent_stop_at_or_over_100_is_not_usable(self):
        d = route.decide('code', meter(sec('stop', 100.0), sec('out')), POLICY, urgent=True)
        self.assertTrue(d['refused'])

    def test_urgent_stop_with_unknown_pct_is_not_usable(self):
        d = route.decide('code', meter(sec('stop', None), sec('out')), POLICY, urgent=True)
        self.assertTrue(d['refused'])

    def test_urgent_never_rescues_out_or_unknown(self):
        for s in ('out', 'unknown'):
            d = route.decide('code', meter(sec(s), sec(s)), POLICY, urgent=True)
            self.assertTrue(d['refused'], s)

    def test_urgent_claude_stop_with_extra_usage_on_is_not_usable(self):
        d = route.decide('code', meter(sec('stop', 40.0, extra=True), sec('ok')), POLICY, urgent=True)
        self.assertEqual(d['engine'], 'codex')
        self.assertTrue(d['fallback_used'])

    def test_urgent_claude_stop_with_unknown_extra_usage_is_not_usable(self):
        # past the stop line Claude would spend extra usage if it is ON; unknown is not OFF.
        d = route.decide('code', meter(sec('stop', 90.0, extra=None), sec('out')), POLICY, urgent=True)
        self.assertTrue(d['refused'])
        d = route.decide('code', meter(sec('stop', 90.0, extra=False), sec('out')), POLICY, urgent=True)
        self.assertEqual(d['engine'], 'claude')

    def test_urgent_codex_stop_ignores_extra_usage_flag(self):
        d = route.decide('sweep', meter(sec('out'), sec('stop', 90.0, extra=True)), POLICY, urgent=True)
        self.assertEqual(d['engine'], 'codex')

    def test_urgent_falls_back_to_stop_engine(self):
        d = route.decide('code', meter(sec('out'), sec('stop', 90.0)), POLICY, urgent=True)
        self.assertEqual((d['engine'], d['fallback_used']), ('codex', True))


class ForcedEngineTests(unittest.TestCase):
    def test_policy_money_overrides_dispatch_with_warning(self):
        from twinlane import meter as meter_mod
        allowed = common.load_policy()
        allowed['claude']['extra_usage_allowed'] = True
        allowed['codex']['allow_credits'] = True
        claude_raw = {'source': 'oauth', 'reading_at': iso_in(-5), 'extra_usage_enabled': True,
                      'windows': [{'name': 'five_hour', 'pct': 20, 'resets_at': iso_in(3600)}]}
        codex_raw = {'source': 'app-server', 'reading_at': iso_in(-5),
                     'windows': [{'name': 'weekly', 'pct': 100, 'resets_at': iso_in(3600)}],
                     'credits': {'has': True}, 'ordinary_usage_allowed': False}
        c = meter_mod.finish_claude(claude_raw, allowed)
        x = meter_mod.finish_codex(codex_raw, allowed)
        self.assertIn('WARNING', c['note'])
        self.assertIn('WARNING', x['note'])
        self.assertEqual(route.decide('code', {'claude': c, 'codex': x}, allowed)['engine'], 'claude')
        self.assertEqual(route.decide('parallel', {'claude': c, 'codex': x}, allowed)['engine'], 'codex')

    def test_forced_usable_engine_is_used_even_off_primary(self):
        d = route.decide('code', meter(sec('ok'), sec('ok')), POLICY, engine='codex')
        self.assertEqual((d['engine'], d['fallback_used'], d['refused']), ('codex', False, False))
        self.assertIn('forced', d['reason'])

    def test_forced_unusable_is_refused_without_swap(self):
        for s in ('stop', 'out', 'unknown'):
            m = meter(sec(s, reset_in=500), sec('ok'))
            d = route.decide('code', m, POLICY, engine='claude')
            self.assertTrue(d['refused'], s)
            self.assertIsNone(d['engine'])
            self.assertFalse(d['fallback_used'])
            self.assertIn('no swap', d['reason'])
            self.assertEqual(d['wait_until'], m['claude']['next_reset'])

    def test_forced_stop_urgent_is_usable(self):
        d = route.decide('sweep', meter(sec('ok'), sec('stop', 86.0)), POLICY, engine='codex', urgent=True)
        self.assertEqual(d['engine'], 'codex')

    def test_auto_means_no_force(self):
        d = route.decide('code', meter(sec('out'), sec('ok')), POLICY, engine='auto')
        self.assertEqual((d['engine'], d['fallback_used']), ('codex', True))

    def test_unknown_engine_and_kind_raise(self):
        with self.assertRaises(ValueError):
            route.decide('code', meter(sec('ok'), sec('ok')), POLICY, engine='gemini')
        with self.assertRaises(ValueError):
            route.decide('dance', meter(sec('ok'), sec('ok')), POLICY)


class RefusalTests(unittest.TestCase):
    def test_both_out_wait_until_is_earliest_reset(self):
        m = meter(sec('out', reset_in=9000), sec('out', reset_in=600))
        d = route.decide('code', m, POLICY)
        self.assertTrue(d['refused'])
        self.assertEqual(d['wait_until'], m['codex']['next_reset'])
        self.assertEqual(d['advice'], 'write/refresh the baton: twinlane baton snap <slug>')
        m = meter(sec('out', reset_in=60), sec('unknown', reset_in=600))
        self.assertEqual(route.decide('verify', m, POLICY)['wait_until'], m['claude']['next_reset'])

    def test_both_unknown_without_resets_has_no_wait_until(self):
        m = meter({'status': 'unknown'}, {'status': 'unknown'})
        d = route.decide('code', m, POLICY)
        self.assertTrue(d['refused'])
        self.assertIsNone(d['wait_until'])

    def test_missing_sections_are_unknown(self):
        self.assertTrue(route.decide('code', {}, POLICY)['refused'])
        self.assertTrue(route.decide('code', None, POLICY)['refused'])

    def test_codex_on_credits_allow_false_is_out_and_refused(self):
        """The meter turns on_credits into status out; route must then refuse codex."""
        from twinlane import meter as meter_mod
        raw = {'source': 'app-server', 'reading_at': iso_in(-5),
               'windows': [{'name': 'weekly', 'pct': 100.0, 'resets_at': iso_in(3600)}],
               'credits': {'has': True}, 'limit_reached_type': 'primary', 'ordinary_usage_allowed': False}
        codex = meter_mod.finish_codex(raw, POLICY)
        self.assertEqual(codex['status'], 'out')
        self.assertTrue(codex['on_credits'])
        d = route.decide('parallel', meter(sec('out'), codex), POLICY)
        self.assertTrue(d['refused'])
        d = route.decide('parallel', meter(sec('ok'), codex), POLICY)
        self.assertEqual((d['engine'], d['fallback_used']), ('claude', True))
        d = route.decide('parallel', meter(sec('ok'), codex), POLICY, engine='codex', urgent=True)
        self.assertTrue(d['refused'])


class AssignmentTests(unittest.TestCase):
    def test_claude_model_effort_profile(self):
        for kind in ALL_KINDS:
            m = meter(sec('ok'), sec('out'))
            d = route.decide(kind, m, POLICY)
            self.assertEqual(d['engine'], 'claude')
            self.assertEqual(d['model'], POLICY['claude']['models']['default'])
            self.assertEqual(d['effort'], POLICY['claude']['effort_by_kind'][kind])
            self.assertEqual(d['profile'], 'code' if kind == 'code' else 'intel')
            self.assertIsNone(d['tier'])

    def test_claude_fast_kinds_use_fast_model(self):
        p = dict(POLICY, claude=dict(POLICY['claude'], fast_kinds=['sweep']))
        d = route.decide('sweep', meter(sec('ok'), sec('out')), p)
        self.assertEqual(d['model'], POLICY['claude']['models']['fast'])
        d = route.decide('code', meter(sec('ok'), sec('out')), p)
        self.assertEqual(d['model'], POLICY['claude']['models']['default'])

    def test_codex_tier_and_profile(self):
        for kind in ALL_KINDS:
            d = route.decide(kind, meter(sec('out'), sec('ok')), POLICY)
            self.assertEqual(d['engine'], 'codex')
            self.assertEqual(d['tier'], POLICY['codex']['tier_by_kind'][kind])
            self.assertEqual(d['profile'], 'code' if kind in ('code', 'integrate') else 'intel')
            self.assertIsNone(d['model'])

    def test_handoff_band_adds_advice(self):
        d = route.decide('code', meter(sec('handoff'), sec('ok')), POLICY)
        self.assertEqual(d['advice'], 'near limit: keep the baton current')
        d = route.decide('code', meter(sec('ok'), sec('handoff')), POLICY)
        self.assertIsNone(d['advice'])
        d = route.decide('code', meter(sec('out'), sec('handoff')), POLICY)
        self.assertEqual(d['advice'], 'near limit: keep the baton current')

    def test_result_keys(self):
        want = {'engine', 'model', 'effort', 'tier', 'profile', 'reason', 'wait_until', 'fallback_used', 'refused'}
        for m in (meter(sec('ok'), sec('ok')), meter(sec('out'), sec('out'))):
            self.assertTrue(want <= set(route.decide('code', m, POLICY)))

    def test_decide_is_pure(self):
        m = meter(sec('stop', 90.0), sec('ok'))
        before = repr(m)
        with mock.patch('builtins.open', side_effect=AssertionError('IO in decide')):
            route.decide('code', m, POLICY, urgent=True)
        self.assertEqual(repr(m), before)


class CliTests(unittest.TestCase):
    def run_cli(self, argv, m):
        buf = io.StringIO()
        with mock.patch('twinlane.meter.read', return_value=m), redirect_stdout(buf), \
                mock.patch('sys.stderr', io.StringIO()):
            rc = route.cli(argv)
        return rc, buf.getvalue()

    def test_cli_ok_and_refused_exit_codes(self):
        rc, out = self.run_cli(['code'], meter(sec('ok'), sec('ok')))
        self.assertEqual(rc, 0)
        self.assertIn('route code -> claude', out)
        rc, out = self.run_cli(['code', '--json'], meter(sec('out'), sec('out')))
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('"refused": true', out)

    def test_cli_unknown_kind_is_usage_error(self):
        rc, _ = self.run_cli(['dance'], meter(sec('ok'), sec('ok')))
        self.assertEqual(rc, common.EXIT_USAGE)


if __name__ == '__main__':
    unittest.main()
