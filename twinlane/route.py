"""Routing decision (DESIGN.md, section 'route.py').

decide() is pure: it reads only its arguments, so the whole decision table is unit-testable.
"""
from __future__ import annotations

import argparse
import sys

from . import common

ENGINES = ('claude', 'codex')
USABLE = ('ok', 'caution', 'handoff')
BATON_ADVICE = 'write/refresh the baton: twinlane baton snap <slug>'
NEAR_LIMIT_ADVICE = 'near limit: keep the baton current'


def usable(engine: str, section: dict | None, policy: dict, urgent: bool) -> bool:
    """ok/caution/handoff always; stop only when urgent, under 100 % and (Claude) extra usage known off."""
    sec = section or {}
    status = sec.get('status', 'unknown')
    if status in USABLE:
        return True
    if status != 'stop' or not urgent:
        return False
    pct = sec.get('max_pct')
    hard = policy.get('thresholds', {}).get('hard_pct', 100)
    if pct is None or pct >= hard:
        return False
    # Past the stop line an urgent Claude job could cross 100 % and bill extra usage, so that path
    # needs extra usage known to be OFF; unknown (None) is never treated as off.
    return not (engine == 'claude' and sec.get('extra_usage_enabled') is not False
                and not policy.get('claude', {}).get('extra_usage_allowed'))


def _describe(engine: str, sec: dict | None) -> str:
    sec = sec or {}
    pct = sec.get('max_pct')
    return f'{engine} {sec.get("status", "unknown")}' + ('' if pct is None else f' ({pct:.0f}%)')


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
    out = {'engine': engine, **_assignment(engine, kind, policy), 'reason': reason, 'wait_until': None,
           'fallback_used': fallback_used, 'refused': False, 'advice': None}
    if (sec or {}).get('status') == 'handoff':
        out['advice'] = NEAR_LIMIT_ADVICE
    return out


def _refused(reason: str, wait_until: str | None) -> dict:
    return {'engine': None, 'model': None, 'effort': None, 'tier': None, 'profile': None,
            'reason': reason, 'wait_until': wait_until, 'fallback_used': False, 'refused': True,
            'advice': BATON_ADVICE}


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
    meter = meter or {}
    tag = ' (urgent)' if urgent else ''
    if engine:
        sec = meter.get(engine)
        if usable(engine, sec, policy, urgent):
            return _chosen(engine, kind, policy, sec, f'forced {_describe(engine, sec)}{tag}', False)
        return _refused(f'forced {_describe(engine, sec)} is not usable{tag}; no swap',
                        (sec or {}).get('next_reset'))
    primary, fallback = routing[kind].get('primary'), routing[kind].get('fallback')
    psec, fsec = meter.get(primary), meter.get(fallback)
    if usable(primary, psec, policy, urgent):
        return _chosen(primary, kind, policy, psec, f'primary {_describe(primary, psec)}{tag}', False)
    if fallback and usable(fallback, fsec, policy, urgent):
        return _chosen(fallback, kind, policy, fsec,
                       f'fallback {_describe(fallback, fsec)}: primary {_describe(primary, psec)}{tag}', True)
    return _refused(f'no usable engine: {_describe(primary, psec)}, {_describe(fallback, fsec)}{tag}',
                    _earliest((psec or {}).get('next_reset'), (fsec or {}).get('next_reset')))


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
    m = meter_mod.read(fresh=a.fresh, policy=policy, auto_refresh=a.engine != 'codex')
    d = decide(a.kind, m, policy, urgent=a.urgent, engine=a.engine)
    notice = meter_mod.refresh_notice(m)
    out = {'kind': a.kind, **d}
    if notice:
        out['meter_refresh'] = notice
    common.emit(out, a.json, format_decision(a.kind, d) + (f'\n  note: {notice}' if notice else ''))
    return common.EXIT_REFUSED if d['refused'] else common.EXIT_OK
