"""Advisory weekly pace. This never admits a writer or overrides routing policy."""
from __future__ import annotations

import math

from . import common


def weekly_pace(section, engine, now):
    section = section if isinstance(section, dict) else {}
    if section.get('status') == 'unknown':
        return None
    name = 'seven_day' if engine == 'claude' else 'weekly'
    for window in section.get('windows') or []:
        if not isinstance(window, dict) or window.get('name') != name:
            continue
        pct = window.get('pct')
        reset = common.parse_time(window.get('resets_at'))
        if type(pct) not in (int, float) or not math.isfinite(pct) or pct < 0 or reset is None:
            continue
        elapsed = 1 - (reset - now).total_seconds() / (7 * 86400)
        if 0 < elapsed <= 1:
            return pct / elapsed
    return None


def advice(meters, now):
    """Pure routing table: Codex first only on a measured lower weekly pace."""
    paces = {e: weekly_pace(meters.get(e), e, now) for e in ('claude', 'codex')}
    missing = [e for e, pace in paces.items() if pace is None]
    if missing:
        return {'engine': 'claude', 'reason': ' and '.join(missing) + ' weekly meter unknown', 'paces': paces}
    engine = 'codex' if paces['codex'] < paces['claude'] else 'claude'
    return {'engine': engine, 'reason': f'{engine} weekly pace ' +
            ('lower' if paces['codex'] != paces['claude'] else 'tied'), 'paces': paces}


def line(writers, maximum, queued, meters, now):
    route = advice(meters, now)
    return f"writers {writers}/{maximum} · queued {queued} · route: {route['engine']}-first ({route['reason']})"
