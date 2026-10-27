"""Stable, pure failure categories shared by attempts and terminal receipts."""
from __future__ import annotations


def classify(attempt: dict) -> str:
    text = ' '.join(str(attempt.get(k) or '') for k in ('error', 'reason', 'codex_error')).lower()
    status = attempt.get('status')
    if status == 'done':
        return 'done'
    if any(s in text for s in ('policy refused', 'refused by policy', 'broke its guard')):
        return 'policy'  # a refusal may quote transient engine text
    if status == 'stalled' or attempt.get('stalled') or 'host overload:' in text:
        return 'stall'
    if status == 'limit_hit' or attempt.get('limit_hit'):
        return 'limit'
    if 'not a git work tree' in text or 'worktree preflight' in text or 'worktree not ready' in text:
        return 'preflight'
    if any(s in text for s in ('slots.lock', 'writers.lock', 'slot lock busy', 'waiting for slot lock',
                                     'slot admission stayed contended')):
        return 'lock'
    if status == 'timeout' or 'timed out' in text:
        return 'timeout'
    if 'writer directory' in text or 'writer reservation' in text or 'writer ownership' in text:
        return 'custody'
    meter_unknown = ('meter/route unavailable at start' in text or (
        'not usable at start' in text and 'unknown' in text))
    if status == 'refused' and not meter_unknown:
        return 'policy'
    if 'model is at capacity' in text or 'setup refresh had errors' in text or 'sandbox could not set up' in text:
        return 'transient_engine'
    if 'wmi start failed' in text or meter_unknown:
        return 'transient_start'
    return 'task'
