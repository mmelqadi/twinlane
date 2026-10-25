"""Context occupancy from the latest per-request usage, never cumulative billing totals."""
from __future__ import annotations

import json
import math

from . import common


def number(value):
    try:
        return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None
    except OverflowError:
        return None


def claude_usage(obj):
    if obj.get('type') != 'assistant' or obj.get('isSidechain'):
        return None
    msg = obj.get('message')
    if not isinstance(msg, dict) or msg.get('model') == '<synthetic>':
        return None
    usage = msg.get('usage')
    if not isinstance(usage, dict):
        return None
    values = [number(usage.get(k, 0)) for k in
              ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
    return sum(values) if 'input_tokens' in usage and None not in values else None


def codex_usage(payload):
    info = payload.get('info') or {}
    usage = info.get('last_token_usage') or {} if isinstance(info, dict) else {}
    if not isinstance(usage, dict):
        return None, None
    tokens = number(usage.get('total_tokens'))
    if tokens is None:
        values = [number(usage.get(k)) for k in ('input_tokens', 'output_tokens')]
        tokens = sum(values) if None not in values else None
    return tokens, number(info.get('model_context_window')) if isinstance(info, dict) else None


def estimate(engine, lines, model=None, policy=None, include_sidechain=False):
    tokens = observed_window = None
    turn = at = task_window = None
    for line in lines:
        try:
            obj = json.loads(line)
        except (ValueError, TypeError):
            continue
        if not isinstance(obj, dict):
            continue
        if engine == 'codex':
            p = obj.get('payload')
            if isinstance(p, dict) and p.get('turn_id'):
                turn = p['turn_id']
            if isinstance(turn, str) and turn.startswith('external-import-turn'):
                continue
        if engine == 'claude' and obj.get('type') == 'assistant' and (include_sidechain or not obj.get('isSidechain')):
            msg = obj.get('message') or {}
            if not isinstance(msg, dict) or msg.get('model') == '<synthetic>':
                continue
            usage = msg.get('usage') if isinstance(msg, dict) else None
            if not isinstance(usage, dict):
                continue
            values = [number(usage.get(k, 0)) for k in
                      ('input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens')]
            tokens = sum(values) if 'input_tokens' in usage and None not in values else None
            at = obj.get('timestamp')
        elif engine == 'codex' and obj.get('type') == 'event_msg':
            p = obj.get('payload') or {}
            if isinstance(p, dict) and p.get('type') == 'task_started':
                task_window = number(p.get('model_context_window'))
            if not isinstance(p, dict) or p.get('type') != 'token_count':
                continue
            info = p.get('info') or {}
            if not isinstance(info, dict):
                continue
            usage = info.get('last_token_usage') or {}
            if not isinstance(usage, dict):
                continue
            tokens = number(usage.get('total_tokens'))
            if tokens is None:
                values = [number(usage.get(k)) for k in ('input_tokens', 'output_tokens')]
                tokens = sum(values) if None not in values else None
            observed_window = number(info.get('model_context_window'))
            at = obj.get('timestamp')
    cfg = ((policy if policy is not None else common.load_policy()).get('context') or {})
    cfg = cfg if isinstance(cfg, dict) else {}
    windows = cfg.get('window_tokens') or cfg.get('windows') or {}
    windows = windows if isinstance(windows, dict) else {}
    configured = (windows.get('models') or {}).get(model) if model else None
    configured = configured if configured is not None else windows.get(model) if model else configured
    configured = configured if configured is not None else windows.get(engine)
    window = number(configured) if configured is not None else observed_window or task_window
    window = window if window and window > 0 else None
    pct = round(tokens / window * 100, 1) if tokens is not None and window else None
    rotate, checkpoint = number(cfg.get('rotate_pct', 85)), number(cfg.get('checkpoint_pct', 70))
    flag = ('rotate' if rotate is not None and pct >= rotate else
            'checkpoint' if checkpoint is not None and pct >= checkpoint else None) if pct is not None else None
    result = {'tokens': tokens, 'window': window, 'pct': pct, 'flag': flag, 'estimate': True,
              'window_source': ('policy' if configured is not None else 'rollout') if window else None,
              'at': at, 'model': model}
    result['text'] = label(result)
    return result


def label(ctx):
    ctx = ctx or {}
    tokens, pct = ctx.get('tokens'), ctx.get('pct')
    if tokens is None:
        return 'ctx ?'
    def short(n):
        return f'{n / 1000:g}k' if n >= 1000 else f'{n:g}'
    return f"ctx ~{short(tokens)}" + (f"/{short(ctx['window'])} ~{pct:g}%" if pct is not None else ' %? (window unknown)') + \
        (f" {ctx['flag']}" if ctx.get('flag') else '')
