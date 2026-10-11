"""route.decide: the full decision table from DESIGN.md. Pure function, no IO."""
import datetime as dt
import io
import itertools
import json
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
                if ps in ('caution', 'handoff') and fs == 'ok':  # caution routing: prefer the other engine
                    self.assertEqual((d['engine'], d['fallback_used'], d['refused']), (fallback, True, False))
                    self.assertIn(f'{primary} at {PCT_FOR[ps]:.0f}% ({ps}): prefer {fallback}', d['reason'])
                elif ps in usable:
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

    def test_policy_allowing_extra_usage_permits_urgent_claude_at_stop(self):
        # The user's explicit policy OK must reach the urgent stop path too; never at 100 %.
        allowed = dict(POLICY, claude=dict(POLICY['claude'], extra_usage_allowed=True))
        d = route.decide('code', meter(sec('stop', 90.0, extra=True), sec('out')), allowed, urgent=True,
                         engine='claude')
        self.assertEqual((d['engine'], d['refused']), ('claude', False))
        self.assertFalse(route.usable('claude', sec('stop', 100.0, extra=True), allowed, urgent=True))
        self.assertFalse(route.usable('claude', sec('stop', 90.0, extra=True), allowed, urgent=False))

    def test_urgent_claude_stop_with_unknown_extra_usage_is_not_usable(self):
        # past the stop line Claude would spend extra usage if it is ON; unknown is not OFF.
        d = route.decide('code', meter(sec('stop', 90.0, extra=None), sec('out')), POLICY, urgent=True)
        self.assertTrue(d['refused'])
        d = route.decide('code', meter(sec('stop', 90.0, extra=False), sec('out')), POLICY, urgent=True)
        self.assertEqual(d['engine'], 'claude')

    def test_a_held_claude_says_why_in_the_reason(self):
        # unknown extra usage holds Claude; the refusal names that, not just "unknown".
        held = dict(sec('unknown', 20.0, extra=None), hold='extra usage state unknown')
        d = route.decide('code', meter(held, sec('out')), POLICY)
        self.assertTrue(d['refused'])
        self.assertIn('claude unknown (20%) [extra usage state unknown]', d['reason'])
        d = route.decide('code', meter(held, sec('ok')), POLICY)
        self.assertEqual((d['engine'], d['fallback_used']), ('codex', True))
        self.assertIn('[extra usage state unknown]', d['reason'])
        d = route.decide('code', meter(held, sec('ok')), POLICY, engine='claude')
        self.assertTrue(d['refused'])
        self.assertIn('[extra usage state unknown]', d['reason'])
        self.assertIsNone(d['wait_until'])  # a window reset does not clear a hold
        d = route.decide('code', meter(held, dict(sec('out'), next_reset=None)), POLICY)
        self.assertIsNone(d['wait_until'])

    def test_a_cached_ok_claude_with_extra_usage_not_off_is_never_routed(self):
        # A cache written before the meter held unknown extra usage (or edited by hand) still
        # says `ok`; route itself must require extra usage known OFF, forced or not, urgent or not.
        for extra, why in ((None, 'extra usage state unknown'), (True, 'extra usage is ON')):
            stale_ok = sec('ok', 20.0, extra=extra)
            with self.subTest(extra=extra):
                self.assertFalse(route.usable('claude', stale_ok, POLICY, urgent=True))
                d = route.decide('code', meter(stale_ok, sec('ok')), POLICY)
                self.assertEqual((d['engine'], d['fallback_used']), ('codex', True))
                self.assertIn(f'[{why}]', d['reason'])
                d = route.decide('code', meter(stale_ok, sec('out')), POLICY, engine='claude', urgent=True)
                self.assertTrue(d['refused'])
                self.assertIsNone(d['wait_until'])  # a window reset does not clear it
        allowed = dict(POLICY, claude=dict(POLICY['claude'], extra_usage_allowed=True))
        self.assertTrue(route.usable('claude', sec('ok', 20.0, extra=None), allowed, urgent=False))
        self.assertTrue(route.usable('codex', sec('ok', 20.0, extra=None), POLICY, urgent=False))

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
        # Codex on allowed paid credits reads `caution`, so caution routing prefers the included engine ...
        d = route.decide('parallel', {'claude': c, 'codex': x}, allowed)
        self.assertEqual(d['engine'], 'claude')
        self.assertIn('prefer claude', d['reason'])
        # ... while Codex itself stays usable: forcing it is not refused.
        d = route.decide('parallel', {'claude': c, 'codex': x}, allowed, engine='codex')
        self.assertEqual((d['engine'], d['refused']), ('codex', False))

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


def wsec(status, pct, windows, **kw):
    """A section whose windows are listed, so the decision can name the one that binds."""
    return dict(sec(status, pct), windows=[{'name': n, 'pct': p, 'resets_at': iso_in(3600)} for n, p in windows], **kw)


class CautionTests(unittest.TestCase):
    """Caution routing, 75-85 %: prefer the other engine for new work."""

    def test_claude_79_codex_20_goes_to_codex_and_says_why(self):
        d = route.decide('code', meter(sec('caution', 79.0), sec('ok', 20.0)), POLICY)
        self.assertEqual((d['engine'], d['refused'], d['fallback_used']), ('codex', False, True))
        self.assertEqual(d['reason'], 'claude at 79% (caution): prefer codex')
        self.assertEqual(d['profile'], 'code')  # still a code job: the write profile follows the kind

    def test_handoff_band_prefers_the_other_engine_too(self):
        # Lead decision on at 80-85 % the first choice must not beat an engine at 20 %.
        d = route.decide('code', meter(sec('handoff', 82.0), sec('ok', 20.0)), POLICY)
        self.assertEqual((d['engine'], d['fallback_used']), ('codex', True))
        self.assertEqual(d['reason'], 'claude at 82% (handoff): prefer codex')

    def test_both_in_caution_keeps_the_first_choice(self):
        d = route.decide('code', meter(sec('caution', 79.0), sec('caution', 78.0)), POLICY)
        self.assertEqual((d['engine'], d['fallback_used']), ('claude', False))
        self.assertIn('primary claude caution (79%)', d['reason'])

    def test_74_percent_stays_where_it_is(self):
        d = route.decide('code', meter(sec('ok', 74.0), sec('ok', 20.0)), POLICY)
        self.assertEqual((d['engine'], d['fallback_used']), ('claude', False))
        self.assertIn('primary claude ok (74%)', d['reason'])

    def test_the_rule_holds_for_codex_first_kinds_too(self):
        d = route.decide('sweep', meter(sec('ok', 20.0), sec('caution', 79.0)), POLICY)
        self.assertEqual((d['engine'], d['reason']), ('claude', 'codex at 79% (caution): prefer claude'))

    def test_other_engine_in_handoff_or_unusable_keeps_the_first_choice(self):
        for status in ('handoff', 'stop', 'out', 'unknown'):
            with self.subTest(other=status):
                d = route.decide('code', meter(sec('caution', 79.0), sec(status)), POLICY)
                self.assertEqual(d['engine'], 'claude')
                self.assertFalse(d['fallback_used'])

    def test_a_held_claude_is_not_a_better_engine(self):
        held = dict(sec('unknown', 20.0, extra=None), hold='extra usage state unknown')
        d = route.decide('sweep', meter(held, sec('caution', 79.0)), POLICY)
        self.assertEqual(d['engine'], 'codex')

    def test_urgent_does_not_change_the_rule(self):
        d = route.decide('code', meter(sec('caution', 79.0), sec('ok', 20.0)), POLICY, urgent=True)
        self.assertEqual(d['engine'], 'codex')
        self.assertIn('claude at 79% (caution): prefer codex (urgent)', d['reason'])

    def test_forced_engine_still_wins(self):
        d = route.decide('code', meter(sec('caution', 79.0), sec('ok', 20.0)), POLICY, engine='claude')
        self.assertEqual((d['engine'], d['fallback_used']), ('claude', False))
        self.assertIn('forced claude caution (79%)', d['reason'])

    def test_a_caution_reading_without_a_percent_does_not_crash(self):
        d = route.decide('code', meter(sec('caution', None), sec('ok', 20.0)), POLICY)
        self.assertEqual((d['engine'], d['reason']), ('codex', 'claude at ?% (caution): prefer codex'))

    def test_the_binding_window_is_named_when_known(self):
        c = wsec('caution', 79.0, [('five_hour', 10.0), ('seven_day_sonnet', 79.0)])
        d = route.decide('code', meter(c, sec('ok', 20.0)), POLICY)
        self.assertEqual(d['reason'], 'claude at 79% (caution, 7d-sonnet): prefer codex')


class RefusalNamesItsWindowTests(unittest.TestCase):
    """Every routing refusal names the window that caused it."""

    def claude_stop(self):
        return wsec('stop', 90.0, [('five_hour', 5.0), ('seven_day', 15.0), ('seven_day_sonnet', 90.0)])

    def test_text_reason_and_blockers_name_a_known_window(self):
        d = route.decide('code', meter(self.claude_stop(), sec('out', 100.0)), POLICY)
        self.assertTrue(d['refused'])
        self.assertIn('claude stop (90% in 7d-sonnet)', d['reason'])
        first = d['blockers'][0]
        self.assertEqual((first['engine'], first['status'], first['pct'], first['window'], first['recognised']),
                         ('claude', 'stop', 90.0, 'seven_day_sonnet', True))
        self.assertEqual([b['engine'] for b in d['blockers']], ['claude', 'codex'])
        self.assertIn('claude stop (90% in 7d-sonnet)', route.format_decision('code', d))

    def test_an_unrecognised_window_is_named_as_such(self):
        c = wsec('stop', 90.0, [('five_hour', 5.0), ('iguana_necktie', 90.0)])
        d = route.decide('code', meter(c, sec('out', 100.0)), POLICY)
        self.assertIn('claude stop (90% in iguana_necktie (unrecognised window))', d['reason'])
        self.assertEqual((d['blockers'][0]['window'], d['blockers'][0]['recognised']), ('iguana_necktie', False))

    def test_a_forced_refusal_names_the_window(self):
        d = route.decide('code', meter(self.claude_stop(), sec('ok')), POLICY, engine='claude')
        self.assertTrue(d['refused'])
        self.assertIn('forced claude stop (90% in 7d-sonnet) is not usable', d['reason'])
        self.assertEqual([(b['engine'], b['window']) for b in d['blockers']], [('claude', 'seven_day_sonnet')])

    def test_a_hold_is_named_instead_of_a_window(self):
        held = dict(wsec('unknown', 20.0, [('five_hour', 20.0)]), hold='extra usage state unknown')
        d = route.decide('code', meter(held, sec('out')), POLICY)
        self.assertEqual(d['blockers'][0]['hold'], 'extra usage state unknown')

    def test_an_inconsistent_reading_does_not_blame_a_window_it_did_not_come_from(self):
        c = wsec('stop', 90.0, [('five_hour', 50.0)])
        d = route.decide('code', meter(c, sec('out')), POLICY)
        self.assertIn('claude stop (90%)', d['reason'])
        self.assertIsNone(d['blockers'][0]['window_label'])

    def test_an_unprintable_window_name_never_reaches_the_reason(self):
        c = wsec('stop', 90.0, [('five_hour', 5.0), ('bad\nname', 90.0)])
        d = route.decide('code', meter(c, sec('out')), POLICY)
        self.assertNotIn('\n', d['reason'])
        self.assertIn('(invalid window name)', d['reason'])
        self.assertIsNone(d['blockers'][0]['window'])

    def test_a_chosen_route_has_no_blockers(self):
        self.assertEqual(route.decide('code', meter(sec('ok'), sec('ok')), POLICY)['blockers'], [])

    def test_cli_json_and_text_carry_the_window(self):
        m = meter(self.claude_stop(), sec('out', 100.0))
        buf = io.StringIO()
        with mock.patch('twinlane.meter.read', return_value=m), redirect_stdout(buf), \
                mock.patch('sys.stderr', io.StringIO()):
            self.assertEqual(route.cli(['code', '--json']), common.EXIT_REFUSED)
        out = json.loads(buf.getvalue())
        self.assertEqual(out['blockers'][0]['window'], 'seven_day_sonnet')
        self.assertIn('7d-sonnet', out['reason'])
        buf = io.StringIO()
        with mock.patch('twinlane.meter.read', return_value=m), redirect_stdout(buf), \
                mock.patch('sys.stderr', io.StringIO()):
            route.cli(['code'])
        self.assertIn('REFUSED - no usable engine: claude stop (90% in 7d-sonnet)', buf.getvalue())


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
        d = route.decide('code', meter(sec('handoff'), sec('caution')), POLICY)  # no ok engine to prefer
        self.assertEqual(d['advice'], 'near limit: keep the baton current')
        d = route.decide('code', meter(sec('ok'), sec('handoff')), POLICY)
        self.assertIsNone(d['advice'])
        d = route.decide('code', meter(sec('out'), sec('handoff')), POLICY)
        self.assertEqual(d['advice'], 'near limit: keep the baton current')

    def test_result_keys(self):
        want = {'engine', 'model', 'effort', 'tier', 'profile', 'reason', 'wait_until', 'fallback_used', 'refused',
                'blockers'}
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
