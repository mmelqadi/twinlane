"""Routing decision (DESIGN.md, section 'route.py').

decide() is pure: it reads only its arguments, so the whole decision table is unit-testable.
"""
from __future__ import annotations

import argparse
import sys

from . import common
from . import meter as meter_mod

ENGINES = ('claude', 'codex')
USABLE = ('ok', 'caution', 'handoff')
BATON_ADVICE = 'write/refresh the baton: twinlane baton snap <slug>'
NEAR_LIMIT_ADVICE = 'near limit: keep the baton current'


def claude_billing_hold(section: dict | None, policy: dict) -> str | None:
    """Why Claude may take no call at all, or None: any call could bill extra usage unless the reading
    shows it OFF. Checked here as well as in the meter, because a cache written before the meter learned
    the rule (or edited by hand) can still say `ok` with extra usage ON or unknown."""
    if (policy.get('claude') or {}).get('extra_usage_allowed') is True:
        return None
    extra = (section or {}).get('extra_usage_enabled')
    if extra is False:
        return None
    return 'extra usage is ON' if extra else 'extra usage state unknown'


def _with_hold(engine: str, section: dict | None, policy: dict) -> dict | None:
    """The section with Claude's billing hold named in `hold`, so the reason and the (absent) wait show it."""
    if engine != 'claude' or not isinstance(section, dict) or section.get('hold'):
        return section
    why = claude_billing_hold(section, policy)
    return {**section, 'hold': why} if why else section


def usable(engine: str, section: dict | None, policy: dict, urgent: bool) -> bool:
    """Claude never while extra usage is not known OFF; otherwise ok/caution/handoff always, and stop
    only when urgent and under 100 %."""
    sec = section or {}
    if engine == 'claude' and claude_billing_hold(sec, policy):
        return False
    status = sec.get('status', 'unknown')
    if status in USABLE:
        return True
    if status != 'stop' or not urgent:
        return False
    pct = sec.get('max_pct')
    hard = policy.get('thresholds', {}).get('hard_pct', 100)
    if pct is None or pct >= hard:
        return False
    # Past the stop line an urgent Claude job could cross 100 % and bill extra usage: claude_billing_hold
    # (checked on entry) already refused it unless extra usage is known OFF or the policy allows it.
    return True


def _window_text(sec: dict | None) -> str | None:
    """The binding window's name as the output shows it (unfamiliar ones say so), or None without windows or
    when the reading's max_pct is not that window's (a hand-edited or inconsistent cache must not be blamed on it)."""
    wins = (sec or {}).get('windows')
    i = meter_mod.binding_index(wins)
    if i is None or wins[i]['pct'] != (sec or {}).get('max_pct'):
        return None
    return meter_mod.window_label(wins[i])


def _describe(engine: str, sec: dict | None) -> str:
    sec = sec or {}
    pct = sec.get('max_pct')
    window = _window_text(sec)
    shown = '' if pct is None else f' ({pct:.0f}%' + (f' in {window}' if window else '') + ')'
    return f'{engine} {sec.get("status", "unknown")}{shown}' + (f' [{sec["hold"]}]' if sec.get('hold') else '')


def _blocker(engine: str, sec: dict | None) -> dict:
    """Why one engine could not take the work, as data: its status, the window that binds it, the hold."""
    sec = sec or {}
    bw = meter_mod.binding_window(sec.get('windows'))
    return {'engine': engine, 'status': sec.get('status', 'unknown'), 'pct': sec.get('max_pct'),
            'window': bw['name'] if bw else None, 'window_label': _window_text(sec),
            'recognised': bw['recognised'] if bw else None, 'hold': sec.get('hold') or None}


def _resets_at(sec: dict | None) -> str | None:
    """When waiting would help: a held engine (extra usage unknown) does not clear at a window reset."""
    sec = sec or {}
    return None if sec.get('hold') else sec.get('next_reset')


def _assignment(engine: str, kind: str, policy: dict) -> dict:
    if engine == 'claude':
        cp = policy.get('claude', {})
        models = cp.get('models', {})
        fast = kind in (cp.get('fast_kinds') or [])
        return {'model': models.get('fast' if fast else 'default'),
                'effort': (cp.get('effort_by_kind') or {}).get(kind, 'high'), 'tier': None,
                'profile': 'code' if kind == 'code' else 'intel'}
    xp = policy.get('codex', {})
    return {'model': None, 'effort': None, 'tier': (xp.get('tier_by_kind') or {}).get(kind, 'sol'),
            'profile': 'code' if kind in (xp.get('write_kinds') or ['code', 'integrate']) else 'intel'}


def _earliest(*times) -> str | None:
    parsed = [t for t in (common.parse_time(x) for x in times) if t is not None]
    return common.iso(min(parsed)) if parsed else None


def _chosen(engine: str, kind: str, policy: dict, sec: dict, reason: str, fallback_used: bool) -> dict:
    if (sec or {}).get('stale_ok'):
        reason += f'; meter stale-ok (age {int(sec["stale_ok_age_s"]) // 60}m)'
    out = {'engine': engine, **_assignment(engine, kind, policy), 'reason': reason, 'wait_until': None,
           'fallback_used': fallback_used, 'refused': False, 'advice': None, 'blockers': []}
    if (sec or {}).get('status') == 'handoff':
        out['advice'] = NEAR_LIMIT_ADVICE
    return out


def _refused(reason: str, wait_until: str | None, blockers: list[dict]) -> dict:
    """`blockers` names, per engine that could not take the work, the window (or hold) that stopped it."""
    return {'engine': None, 'model': None, 'effort': None, 'tier': None, 'profile': None,
            'reason': reason, 'wait_until': wait_until, 'fallback_used': False, 'refused': True,
            'advice': BATON_ADVICE, 'blockers': blockers}


def _prefers_other(sec: dict | None, other_sec: dict | None, other: str | None, policy: dict, urgent: bool) -> bool:
    """Caution routing, 75-85 %: the first choice is in caution or handoff and the other engine is below
    caution and usable, so the new work goes to the other one. Urgency does not change it."""
    return bool(other and (sec or {}).get('status') in ('caution', 'handoff')
                and (other_sec or {}).get('status') == 'ok' and usable(other, other_sec, policy, urgent))


def decide(kind: str, meter: dict, policy: dict, urgent: bool = False, engine: str | None = None) -> dict:
    """-> {engine, model, effort, tier, profile, reason, wait_until, fallback_used, refused, advice}.
    Raises ValueError for an unknown kind or engine."""
    routing = policy.get('routing') or {}
    if kind not in routing:
        raise ValueError(f'unknown kind {kind!r}; known: {", ".join(routing)}')
    if engine == 'auto':
        engine = None
    if engine is not None and engine not in ENGINES:
        raise ValueError(f'unknown engine {engine!r}')
    meter = {e: _with_hold(e, s, policy) for e, s in (meter or {}).items()}
    tag = ' (urgent)' if urgent else ''
    if engine:
        sec = meter.get(engine)
        if usable(engine, sec, policy, urgent):
            return _chosen(engine, kind, policy, sec, f'forced {_describe(engine, sec)}{tag}', False)
        return _refused(f'forced {_describe(engine, sec)} is not usable{tag}; no swap',
                        _resets_at(sec), [_blocker(engine, sec)])
    primary, fallback = routing[kind].get('primary'), routing[kind].get('fallback')
    psec, fsec = meter.get(primary), meter.get(fallback)
    if usable(primary, psec, policy, urgent):
        if _prefers_other(psec, fsec, fallback, policy, urgent):
            window, pct = _window_text(psec), psec.get('max_pct')
            why = (f'{primary} at {"?" if pct is None else f"{pct:.0f}"}% '
                   f'({psec["status"] + ", " + window if window else psec["status"]}): prefer {fallback}')
            return _chosen(fallback, kind, policy, fsec, why + tag, True)
        return _chosen(primary, kind, policy, psec, f'primary {_describe(primary, psec)}{tag}', False)
    if fallback and usable(fallback, fsec, policy, urgent):
        return _chosen(fallback, kind, policy, fsec,
                       f'fallback {_describe(fallback, fsec)}: primary {_describe(primary, psec)}{tag}', True)
    return _refused(f'no usable engine: {_describe(primary, psec)}, {_describe(fallback, fsec)}{tag}',
                    _earliest(_resets_at(psec), _resets_at(fsec)),
                    [_blocker(primary, psec)] + ([_blocker(fallback, fsec)] if fallback else []))


def format_decision(kind: str, d: dict) -> str:
    if d.get('refused'):
        line = f'route {kind}: REFUSED - {d["reason"]}'
        if d.get('wait_until'):
            line += f'; wait until {d["wait_until"]}'
        return line + f'\n  {d["advice"]}'
    spec = ' '.join(f'{k} {d[k]}' for k in ('model', 'effort', 'tier', 'profile') if d.get(k))
    line = f'route {kind} -> {d["engine"]} {spec} ({d["reason"]})'
    return line + (f'\n  {d["advice"]}' if d.get('advice') else '')


def cli(argv) -> int:
    from . import meter as meter_mod
    ap = argparse.ArgumentParser(prog='twinlane route', description='Pick the engine for a kind of work.')
    ap.add_argument('kind')
    ap.add_argument('--urgent', action='store_true')
    ap.add_argument('--engine', choices=('auto', 'claude', 'codex'), default='auto')
    ap.add_argument('--fresh', action='store_true', help='ignore the meter cache')
    ap.add_argument('--json', action='store_true')
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_OK if e.code == 0 else common.EXIT_USAGE
    policy = common.load_policy()
    if a.kind not in (policy.get('routing') or {}):
        print(f'twinlane route: unknown kind {a.kind!r}; known: {", ".join(policy.get("routing") or {})}',
              file=sys.stderr)
        return common.EXIT_USAGE
    # A forced Codex route never needs Claude, so it never spends the automatic token refresh.
    m = meter_mod.read(fresh=a.fresh, policy=policy, auto_refresh=a.engine != 'codex', admission=True)
    d = decide(a.kind, m, policy, urgent=a.urgent, engine=a.engine)
    notice = meter_mod.refresh_notice(m)
    out = {'kind': a.kind, **d}
    if notice:
        out['meter_refresh'] = notice
    common.emit(out, a.json, format_decision(a.kind, d) + (f'\n  note: {notice}' if notice else ''))
    return common.EXIT_REFUSED if d['refused'] else common.EXIT_OK
