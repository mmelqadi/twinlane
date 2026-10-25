"""Claude Code UserPromptSubmit warning and session-bound PreCompact checkpoint hook (DESIGN.md "hook.py").

Fail-open by construction: it reads only the cached meter (state/meter.json), never blocks for more
than about a second, prints nothing unless an engine is at handoff/stop/out, and always returns 0.
A stale cache is refreshed by a detached `twinlane meter --fresh` (no model call) for the NEXT prompt.
"""
from __future__ import annotations

import json
import os
import sys
import threading

from twinlane import common

WARN_STATUSES = ('handoff', 'stop', 'out')
DEFAULT_CACHE_MAX_AGE_S = 300
REFRESH_THROTTLE_S = 120  # at most one background refresh per 2 min, however fast prompts arrive
STDIN_WAIT_S = 0.5
WINDOW_LABELS = {'five_hour': '5h', 'seven_day': '7d', 'seven_day_opus': '7d-opus',
                 'seven_day_sonnet': '7d-sonnet', 'weekly': 'weekly'}


# ---------------------------------------------------------------- inputs

def read_stdin_json(stream=None, wait_s: float = STDIN_WAIT_S) -> dict:
    """Hook payload, or {} if stdin is a tty, empty, invalid or slow (a reader thread bounds the wait)."""
    stream = stream if stream is not None else sys.stdin
    try:
        if stream is None or stream.isatty():
            return {}
    except (AttributeError, ValueError, OSError):
        return {}
    box: list[str] = []
    reader = threading.Thread(target=lambda: box.append(stream.read()), daemon=True)
    reader.start()
    reader.join(wait_s)
    if not box:
        return {}
    try:
        data = json.loads(box[0] or '{}')
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def cache_path() -> str:
    """meter.meter_path() when meter.py provides it; otherwise the spec path state/meter.json."""
    try:
        from twinlane import meter  # lazy: the hook must work even if meter.py is broken or missing
        p = meter.meter_path()
        if isinstance(p, str) and p:
            return p
    except Exception:
        pass
    return os.path.join(common.STATE, 'meter.json')


def cache_max_age_s(policy: dict | None) -> float:
    try:
        return float((policy or {}).get('meter', {}).get('hook_cache_max_age_s', DEFAULT_CACHE_MAX_AGE_S))
    except (TypeError, ValueError):
        return DEFAULT_CACHE_MAX_AGE_S


def safe_policy() -> dict:
    try:
        return common.load_policy()
    except BaseException:  # load_policy raises SystemExit on a missing policy
        return {}


# ---------------------------------------------------------------- refresh

def refresh_due(meter: dict | None, max_age_s: float) -> bool:
    if not isinstance(meter, dict):
        return True
    age = common.age_s(meter.get('read_at'))
    return age is None or age > max_age_s


def spawn_refresh(state_dir: str, spawn=None) -> bool:
    """Start `twinlane meter --fresh --json` detached unless one started within REFRESH_THROTTLE_S.

    --fresh rereads usage without making a model call.
    """
    marker = os.path.join(state_dir, 'hook-refresh.json')
    last = common.read_json(marker, {}) or {}
    age = common.age_s(last.get('spawned_at'))
    if age is not None and age < REFRESH_THROTTLE_S:
        return False
    spawn = spawn or common.spawn_detached
    argv = [sys.executable or 'python', common.TWINLANE_PY, 'meter', '--fresh', '--json']
    env = dict(os.environ)
    env.pop('TWINLANE_ENGINE_JOB', None)
    pid = spawn(argv, cwd=common.ROOT, env=env)
    common.write_json(marker, {'spawned_at': common.iso(common.now()), 'pid': pid})
    return True


# ---------------------------------------------------------------- message

def engine_status(eng: dict | None) -> str:
    """Cached status, except a reading whose next reset already passed says nothing about now."""
    if not isinstance(eng, dict):
        return 'unknown'
    status = str(eng.get('status') or 'unknown')
    reset = common.parse_time(eng.get('next_reset'))
    window_has_reset = reset is not None and reset <= common.now()
    # Credits use and a reached spend cap are billing states, not windows, so a passed reset does not clear them.
    if status in WARN_STATUSES and window_has_reset and not eng.get('on_credits') \
            and not eng.get('spend_control_reached'):
        return 'unknown'
    return status


def top_window(eng: dict) -> dict | None:
    wins = [w for w in (eng.get('windows') or []) if isinstance(w, dict) and w.get('pct') is not None]
    if not wins:
        return None
    return max(wins, key=lambda w: float(w.get('pct') or 0))


def hhmm(value) -> str | None:
    t = common.parse_time(value)
    return None if t is None else t.strftime('%H:%MZ') if t.date() == common.now().date() \
        else t.strftime('%m-%d %H:%MZ')


def describe(name: str, eng: dict | None) -> str:
    status = engine_status(eng)
    if not isinstance(eng, dict):
        return f'{name} unknown'
    win = top_window(eng)
    if win is None:
        part = f'{name} {status}'
    else:
        label = WINDOW_LABELS.get(str(win.get('name')), str(win.get('name')))
        part = f"{name} {label} {float(win.get('pct') or 0):.0f}% ({status})" if status != 'ok' \
            else f"{name} {label} {float(win.get('pct') or 0):.0f}% ok"
    reset = hhmm((win or {}).get('resets_at') or eng.get('next_reset'))
    if status in WARN_STATUSES and reset:
        part += f' resets {reset}'
    if eng.get('extra_usage_enabled'):
        part += ' [extra usage ON: policy forbids]'
    if eng.get('on_credits'):
        part += ' [on paid credits]'
    if eng.get('spend_control_reached'):
        part += ' [spend control reached]'
    return part


def build_message(meter: dict | None, max_age_s: float = DEFAULT_CACHE_MAX_AGE_S) -> str | None:
    """The additionalContext text, or None when neither engine is at handoff/stop/out."""
    if not isinstance(meter, dict):
        return None
    claude, codex = meter.get('claude'), meter.get('codex')
    statuses = {engine_status(claude), engine_status(codex)}
    if not statuses & set(WARN_STATUSES):
        return None
    text = f"[twinlane] {describe('Claude', claude)}; {describe('Codex', codex)}."
    age = common.age_s(meter.get('read_at'))
    if age is not None and age > max_age_s:
        text += f' (reading {int(age // 60)} min old; refreshing)'
    text += (' Route new work with `twinlane route <kind>`; keep the baton current: `twinlane baton snap <slug>`.')
    if engine_status(claude) in ('stop', 'out') or engine_status(codex) in ('stop', 'out'):
        text += ' At stop/out: write the baton, then `twinlane handoff <slug> --to <other engine>`.'
    return text


def emit(text: str, out=None) -> None:
    out = out if out is not None else sys.stdout
    payload = {'hookSpecificOutput': {'hookEventName': 'UserPromptSubmit', 'additionalContext': text}}
    out.write(json.dumps(payload, ensure_ascii=True) + '\n')
    out.flush()


# ---------------------------------------------------------------- entry

def run(stdin=None, out=None, meter: dict | None = None, policy: dict | None = None,
        state_dir: str | None = None, spawn=None, env=None) -> int:
    """Testable core. `meter` injects a cached reading; otherwise state/meter.json is read."""
    env = env if env is not None else os.environ
    payload = read_stdin_json(stdin)
    if env.get('TWINLANE_ENGINE_JOB'):
        return 0  # a twinlane-dispatched worker: never nag it, never spawn refreshes from it
    if payload.get('hook_event_name') == 'PreCompact':
        precompact(payload)
        return 0
    if payload.get('hook_event_name') in ('Notification', 'Stop'):
        lane_state(payload)
        return 0
    event = payload.get('hook_event_name')
    if event and event != 'UserPromptSubmit':
        return 0
    policy = policy if policy is not None else safe_policy()
    max_age = cache_max_age_s(policy)
    state_dir = state_dir or common.STATE
    if meter is None:
        meter = common.read_json(cache_path(), None)
    if refresh_due(meter, max_age):
        try:
            spawn_refresh(state_dir, spawn)
        except Exception:
            pass
    text = build_message(meter, max_age)
    if text:
        emit(text, out)
    return 0


PRECOMPACT_LOCK_S = 5


def precompact(payload):
    """Checkpoint up to three batons explicitly bound to this session; never infer by cwd."""
    from . import baton
    import glob
    from . import lanes
    sid = payload.get('session_id')
    if not isinstance(sid, str) or not baton.SESSION_RE.fullmatch(sid):
        return
    matches = []
    for path in glob.glob(os.path.join(common.BATONS, '*.json')):
        slug = os.path.basename(path)[:-5]
        if not baton.valid_slug(slug):
            continue
        b = common.read_json(path)
        if isinstance(b, dict) and sid in lanes.bound_ids(b) and b.get('state') not in ('landed', 'closed'):
            matches.append((b.get('updated_at') or '', slug))
    for _, slug in sorted(matches, reverse=True)[:3]:
        try:
            baton.snap(slug, None, 'checkpoint', lock_timeout_s=PRECOMPACT_LOCK_S,
                       extra={'last_precompact': {'at': common.iso(common.now()), 'session_id': sid,
                              'trigger': payload.get('trigger') if payload.get('trigger') in ('auto', 'manual') else None}},
                       create=False)
        except Exception:
            continue


checkpoint = precompact


def lane_state(payload, engine='claude'):
    from . import baton
    sid = payload.get('session_id') if engine == 'claude' else payload.get('thread-id')
    if not isinstance(sid, str) or not baton.SESSION_RE.fullmatch(sid):
        return
    if engine == 'codex' and payload.get('type') != 'agent-turn-complete':
        return
    row = {'engine': engine, 'session_id': sid, 'cwd': payload.get('cwd'),
           'state': 'needs-you' if payload.get('hook_event_name') == 'Notification' else 'idle',
           'since': common.iso(common.now())}
    if engine == 'claude' and row['state'] == 'needs-you':
        row['reason'] = baton.redact(str(payload.get('message') or ''))[:160]
    common.write_json(os.path.join(common.STATE, 'lanes', f'{engine}-{sid}.json'), row)


def main(argv=None, **kwargs) -> int:
    """Contract entry (`twinlane hook`). Any failure exits 0 silently: a hook must never break a prompt."""
    try:
        args = sys.argv[2:] if argv is None else argv
        if len(args) == 2 and args[0] == '--codex-notify':
            lane_state(json.loads(args[1]), 'codex')
        else:
            run(**kwargs)
    except BaseException:
        pass
    return 0
