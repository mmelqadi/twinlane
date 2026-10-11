"""Usage meters for Claude Code and Codex (DESIGN.md, section 'meter.py').

read() returns one dict with a 'claude' and a 'codex' section, each carrying a status
(ok|caution|handoff|stop|out|unknown). Readers are injectable so tests never touch the network,
the real credentials or a real engine.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import glob
import json
import math
import os
import queue
import re
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request

from . import common

OAUTH_URL = 'https://api.anthropic.com/api/oauth/usage'
OAUTH_TIMEOUT_S = 6
APP_SERVER_TIMEOUT_S = 15
ROLLOUT_FILES = 40
STATUS_RANK = {'ok': 0, 'caution': 1, 'handoff': 2, 'stop': 3, 'out': 4}
USABLE_STATUSES = ('ok', 'caution', 'handoff')
HOLD_EXTRA_UNKNOWN = 'extra usage state unknown'
HOLD_EXTRA_ON = 'extra usage is ON'
HOLD_EXTRA_UNKNOWN_NOTE = ('Claude held: extra usage state unknown (a call could bill it); record a reading '
                           'showing it OFF: twinlane meter --record-claude <get_usage json>')
SHORT_NAMES = {'five_hour': '5h', 'seven_day': '7d', 'seven_day_opus': '7d-opus',
               'seven_day_sonnet': '7d-sonnet', 'seven_day_oauth_apps': '7d-apps'}
CORE_WINDOWS = {'five_hour', 'seven_day', 'weekly'}
UNRECOGNISED_WINDOW = '(unrecognised window)'
INVALID_WINDOW = '(invalid window name)'
MINUTES_WINDOW = re.compile(r'\d+min')  # the name window_name_from_minutes gives an unfamiliar length
FUTURE_SKEW_S = 60  # a reading stamped further ahead than this is invalid, not fresh


def meter_path() -> str:
    return os.path.join(common.STATE, 'meter.json')


def claude_cache_path() -> str:
    return os.path.join(common.STATE, 'claude-usage.json')


# ---------------------------------------------------------------- cache writes: one rule

CACHE_LOCK_S = 5
_EPOCH = dt.datetime(1970, 1, 1, tzinfo=dt.timezone.utc)
_deferral = threading.local()


def _us(t) -> int | None:
    """A time as whole microseconds since the epoch (the unit cache stamps are compared in), or None."""
    t = common.parse_time(t) if not isinstance(t, dt.datetime) else t
    return None if t is None else (t - _EPOCH) // dt.timedelta(microseconds=1)


def _older(new, cur) -> bool:
    """True when both times are known and `new` is strictly earlier than `cur`. An unknown time never blocks."""
    a, b = _us(new), _us(cur)
    return a is not None and b is not None and a < b


def _locked_update(path: str, decide, timeout_s: float = CACHE_LOCK_S, deadline: float | None = None) -> bool:
    """Read-decide-write `path` under a cross-process lock. decide(current) returns the new content, or None
    to leave the file alone. True when it wrote. A lock that stays busy for `timeout_s` writes nothing (a
    cache write is never worth waiting for). With `deadline` (a time.monotonic() value) the read and the
    replace retries stop at it too: a write that cannot finish by then writes nothing (the file stays the old
    one or becomes the complete new one, never a partial)."""
    try:
        with common.file_lock(path + '.lock', timeout_s=timeout_s, poll_s=min(0.05, max(0.001, timeout_s / 4))):
            new = decide(common.read_json(path, deadline=deadline))
            if new is None:
                return False
            common.write_json(path, new, deadline=deadline)
            return True
    except TimeoutError:
        return False


def _cache_update(path: str, decide) -> bool:
    """Every write to a meter cache file goes through here and is monotonic in reading time: `decide` must
    return None rather than a reading older than the one in the file. Inside deferred_cache_writes() the
    write is queued instead of done (True means queued)."""
    queue_ = getattr(_deferral, 'queue', None)
    if queue_ is not None:
        queue_.append(lambda timeout_s=CACHE_LOCK_S, deadline=None: _locked_update(path, decide, timeout_s, deadline))
        return True
    return _locked_update(path, decide)


@contextlib.contextmanager
def deferred_cache_writes():
    """Cache writes made by THIS thread inside the block are queued, not performed. The caller runs the
    yielded queue with run_deferred() only while it is still inside its own deadline; a reader that ran
    past the deadline therefore leaves no side effect at all."""
    prev = getattr(_deferral, 'queue', None)
    queue_: list = []
    _deferral.queue = queue_
    try:
        yield queue_
    finally:
        _deferral.queue = prev


def run_deferred(queue_: list, deadline: float | None = None) -> int:
    """Run queued cache writes. With `deadline` (a time.monotonic() value) each write waits for the cache
    lock, read the file and retry the replace only for the time left, and the writes still queued when it
    passes are dropped (a skipped cache write only means the next read refreshes). The time one write spends
    inside the filesystem cannot be interrupted from here: a caller that must return at the deadline runs this
    in a thread (jobs._bounded_after_meter). -> number of writes skipped or refused by a busy lock."""
    skipped = 0
    for write in queue_:
        if deadline is None:
            skipped += not write()
            continue
        left = deadline - time.monotonic()
        if left <= 0:
            skipped += 1
            continue
        skipped += not write(min(CACHE_LOCK_S, left), deadline)
    return skipped


# ---------------------------------------------------------------- windows the router counts

def safe_window_name(name) -> str | None:
    """A window name safe to print on one line (baton.WINDOW_NAME: no control characters, 1-40 chars), or None."""
    from . import baton  # lazy: baton is the owner of the rule and needs nothing from here at import time
    return name if isinstance(name, str) and baton.WINDOW_NAME.fullmatch(name) else None


def is_recognised(w) -> bool:
    """True when the code knows what the window is. A window of another Codex limit id carries that id."""
    if not isinstance(w, dict):
        return False
    name = safe_window_name(w.get('name'))
    if name is None:
        return False
    return bool(w.get('limit') or name in SHORT_NAMES or name in CORE_WINDOWS
                or name in SHORT_NAMES.values() or MINUTES_WINDOW.fullmatch(name))


def binding_index(windows) -> int | None:
    """Index of the window the router's max_pct comes from: the highest usable percent, the first on a tie."""
    best = None
    for i, w in enumerate(windows if isinstance(windows, list) else []):
        p = w.get('pct') if isinstance(w, dict) else None
        if isinstance(p, bool) or not isinstance(p, (int, float)) or p != p:
            continue
        if best is None or p > windows[best]['pct']:
            best = i
    return best


def binding_window(windows) -> dict | None:
    """{name, pct, recognised} of the binding window; name is None when it is not printable."""
    i = binding_index(windows)
    if i is None:
        return None
    w = windows[i]
    return {'name': safe_window_name(w.get('name')), 'pct': w['pct'], 'recognised': is_recognised(w)}


def window_label(w: dict) -> str:
    """How a window is named in output: the short name when known, the raw name with an explicit
    `(unrecognised window)` mark when not, `(invalid window name)` when the name cannot be printed."""
    name = safe_window_name((w or {}).get('name'))
    if name is None:
        return INVALID_WINDOW
    if not is_recognised(w):
        return f'{name} {UNRECOGNISED_WINDOW}'
    return SHORT_NAMES.get(name, name)


# ---------------------------------------------------------------- shared shaping

def window_name_from_minutes(mins) -> str:
    try:
        m = int(mins)
    except (TypeError, ValueError):
        return 'window'
    if m >= 10000:
        return 'weekly'
    if m == 300:
        return 'five_hour'
    return f'{m}min'


def _window(name: str, pct, resets_at) -> dict | None:
    try:
        p = float(pct)
    except (TypeError, ValueError):
        p = None
    if isinstance(pct, bool) or p is None or not math.isfinite(p) or p < 0:
        # Keep the invalid window visible: dropping it would make a partial reading look usable.
        return {'name': name, 'pct': None, 'resets_at': common.iso(common.parse_time(resets_at)),
                'invalid_pct': True}
    return {'name': name, 'pct': p, 'resets_at': common.iso(common.parse_time(resets_at))}


def effective_windows(windows: list[dict], now=None) -> list[dict]:
    """A window whose reset time has passed has rolled over, so it counts as 0 %."""
    now = now or common.now()
    out = []
    for w in windows or []:
        w = dict(w)
        t = common.parse_time(w.get('resets_at'))
        if t is not None and t <= now and (w.get('pct') or 0) > 0:
            w['pct'], w['rolled_over'] = 0.0, True
        out.append(w)
    return out


def status_for_pct(pct, thresholds: dict) -> str:
    if pct is None:
        return 'unknown'
    if pct >= thresholds.get('hard_pct', 100):
        return 'out'
    if pct >= thresholds.get('stop_pct', 85):
        return 'stop'
    if pct >= thresholds.get('handoff_pct', 80):
        return 'handoff'
    if pct >= thresholds.get('caution_pct', 75):
        return 'caution'
    return 'ok'


def worse(a: str, b: str) -> str:
    return a if STATUS_RANK.get(a, 5) >= STATUS_RANK.get(b, 5) else b


def next_reset(windows: list[dict], thresholds: dict) -> str | None:
    """When the engine becomes usable again: the LATEST reset among the windows at or over the stop
    line (all of them must clear); when none binds, simply the earliest reset."""
    stop = thresholds.get('stop_pct', 85)
    timed = [w for w in windows if common.parse_time(w.get('resets_at'))]
    binding = [w for w in timed if (w.get('pct') or 0) >= stop]
    pool = binding or timed
    if not pool:
        return None
    pick = max if binding else min
    return common.iso(pick(common.parse_time(w['resets_at']) for w in pool))


def _finish_common(raw: dict, engine_policy: dict, thresholds: dict, now) -> dict:
    """Fields both engines share: source, age, windows, max_pct, status before engine rules."""
    raw = raw or {}
    out = {'status': 'unknown', 'source': raw.get('source') or 'none', 'reading_at': raw.get('reading_at'),
           'age_s': None, 'windows': [], 'binding_window': None, 'max_pct': None, 'next_reset': None,
           'note': raw.get('note') or ''}
    if out['source'] == 'none' or not raw.get('reading_at'):
        out['note'] = out['note'] or 'no reading'
        return out
    stamp = common.parse_time(raw.get('reading_at'))
    if stamp is not None and (stamp - now).total_seconds() > FUTURE_SKEW_S:
        # age_s clamps to 0, so a future stamp would never go stale and could hide exhaustion.
        out['note'] = _join(out['note'], f'reading is dated in the future ({common.iso(stamp)}): invalid')
        return out
    age = common.age_s(raw.get('reading_at'))
    out['age_s'] = None if age is None else int(age)
    windows = []
    for w in raw.get('windows') or []:
        w = w if isinstance(w, dict) else {'name': None}
        checked = _window(w.get('name'), w.get('pct'), w.get('resets_at'))
        windows.append({**w, **checked})
    out['windows'] = effective_windows(windows, now)
    bi = binding_index(out['windows'])
    for i, w in enumerate(out['windows']):
        w['recognised'] = is_recognised(w)
        if i == bi:
            w['binding'] = True
    out['binding_window'] = binding_window(out['windows'])
    pcts = [w['pct'] for w in out['windows'] if w.get('pct') is not None]
    out['max_pct'] = max(pcts) if pcts else None
    out['next_reset'] = next_reset(out['windows'], thresholds)
    if any(w.get('invalid_pct') for w in out['windows']):
        out['note'] = _join(out['note'], 'invalid usage percentage: reading is incomplete')
        return out
    max_age = engine_policy.get('stale_reading_max_age_s', 1800)
    if age is None or age > max_age:
        out['note'] = _join(out['note'], f'reading is {out["age_s"]}s old (> {max_age}s): stale')
        return out
    if out['max_pct'] is None:
        out['note'] = _join(out['note'], 'reading has no usage windows')
        return out
    out['status'] = status_for_pct(out['max_pct'], thresholds)
    return out


def _join(*parts) -> str:
    return '; '.join(p for p in parts if p)


def finish_claude(raw: dict, policy: dict, now=None) -> dict:
    th, cp = policy.get('thresholds', {}), policy.get('claude', {})
    out = _finish_common(raw, cp, th, now or common.now())
    out['extra_usage_enabled'] = (raw or {}).get('extra_usage_enabled')
    out['token_expired_at'] = (raw or {}).get('token_expired_at')
    if (raw or {}).get('refresh'):
        out['refresh'] = raw['refresh']
    if out['status'] != 'unknown' and out['extra_usage_enabled']:
        if cp.get('extra_usage_allowed') is True:
            out['note'] = _join(out['note'], 'WARNING: extra usage is ON; policy allows dispatch and charges may apply')
        else:
            out['status'] = worse(out['status'], 'stop')
            out['note'] = _join(out['note'], 'extra usage is ON; twinlane refuses dispatch')
            out['hold'] = HOLD_EXTRA_ON  # a window reset does not clear it, so no `wait until`
    elif out['status'] in USABLE_STATUSES and out['extra_usage_enabled'] is None and cp.get('extra_usage_allowed') is not True:
        # Unknown is not OFF: any Claude call could bill extra usage, so the reading is held as `unknown`
        # (never routed) with the reason in `hold`, which route, the meter line and the board show.
        out['status'] = 'unknown'
        out['hold'] = HOLD_EXTRA_UNKNOWN
        out['note'] = _join(out['note'], HOLD_EXTRA_UNKNOWN_NOTE)
    elif out['status'] == 'stop' and out['extra_usage_enabled'] is None:
        # route.usable refuses the urgent stop path unless extra usage is known to be OFF.
        out['note'] = _join(out['note'], 'extra usage state unknown: no urgent dispatch past the stop line')
    return out


def finish_codex(raw: dict, policy: dict, now=None) -> dict:
    th, xp = policy.get('thresholds', {}), policy.get('codex', {})
    raw = raw or {}
    out = _finish_common(raw, xp, th, now or common.now())
    credits = raw.get('credits') or {}
    out['credits'] = credits
    out['limit_reached_type'] = raw.get('limit_reached_type')
    out['ordinary_usage_allowed'] = raw.get('ordinary_usage_allowed')
    out['plan'] = raw.get('plan')
    out['limits'] = raw.get('limits') or []
    out['spend_control_reached'] = raw.get('spend_control_reached')
    out['individual_limit'] = raw.get('individual_limit')
    out['free_resets'] = _live_resets(raw.get('free_resets'), now or common.now())
    limited = bool(out['limit_reached_type']) or out['ordinary_usage_allowed'] is False
    at_cap = out['max_pct'] is not None and out['max_pct'] >= th.get('hard_pct', 100)
    out['on_credits'] = bool(credits.get('has')) and (at_cap or limited)
    if out['status'] == 'unknown':
        return out
    if limited and not (xp.get('allow_credits') and credits.get('has')):
        out['status'] = 'out'
        out['note'] = _join(out['note'], f'limit reached ({out["limit_reached_type"] or "ordinary usage not allowed"})')
    if out['on_credits']:
        if xp.get('allow_credits'):
            out['status'] = 'caution'
            out['note'] = _join(out['note'], 'WARNING: policy allows paid credits; charges may apply')
        else:
            out['status'] = 'out'
            out['note'] = _join(out['note'], 'would run on paid credits; twinlane refuses this')
    if out['spend_control_reached'] is True:
        # A reached spend cap stops Codex whatever the windows and credits say; allow_credits cannot lift it.
        out['status'] = 'out'
        out['note'] = _join(out['note'], 'spend control reached')
    return out


def _live_resets(resets, now) -> dict | None:
    """The free full resets with the expiries that already passed dropped, plus the soonest one."""
    if not isinstance(resets, dict):
        return None
    expires = [t for t in resets.get('expires') or [] if (common.parse_time(t) or now) > now]
    return {'available': resets.get('available') or 0, 'expires': expires, 'soonest_expiry': expires[0] if expires else None}


# ---------------------------------------------------------------- Claude: payload normalization

def _desktop_window_name(label: str) -> str:
    s = (label or '').lower()
    if '5-hour' in s or '5 hour' in s or 'five' in s or 'session' in s:
        return 'five_hour'
    if 'week' in s or '7-day' in s or '7 day' in s:
        for model in ('opus', 'sonnet'):
            if model in s:
                return f'seven_day_{model}'
        # "Weekly · all models" is the plain weekly window; any other qualifier is its own window.
        parts = re.split(r'\s[-–]\s|[·:(|]', s, maxsplit=1)
        slug = _slug(parts[1] if len(parts) > 1 else '')
        return 'seven_day' if not slug or slug in ('all_models', 'all', 'limit') else f'seven_day_{slug}'
    return _slug(s) or 'window'


def _slug(s: str) -> str:
    return re.sub(r'[^a-z0-9]+', '_', (s or '').lower()).strip('_')


def _unique_names(windows: list[dict]) -> list[dict]:
    """Two windows must never share a name: later duplicates get _2, _3 ..."""
    seen: dict[str, int] = {}
    for w in windows:
        n = seen.get(w['name'], 0) + 1
        seen[w['name']] = n
        if n > 1:
            w['name'] = f"{w['name']}_{n}"
    return windows


def _oauth_windows(obj: dict) -> list[dict]:
    wins = []
    for key, val in obj.items():
        if key == 'extra_usage' or not isinstance(val, dict) or 'utilization' not in val:
            continue
        if val.get('utilization') is None and val.get('resets_at') is None:
            continue  # OAuth uses a wholly null optional window for a model with no allowance window.
        w = _window(key, val.get('utilization'), val.get('resets_at'))
        if w:
            wins.append(w)
    return wins


def normalize_claude_payload(obj) -> tuple[list[dict], bool | None, str]:
    """OAuth usage response, desktop get_usage output, or an already normalized reading ->
    (windows, extra_usage_enabled, shape). Raises ValueError when nothing is recognized."""
    if isinstance(obj, str):
        obj = json.loads(obj)
    if not isinstance(obj, dict):
        raise ValueError('usage payload must be a JSON object')
    if isinstance(obj.get('windows'), list) and all(isinstance(w, dict) and 'name' in w for w in obj['windows']):
        wins = [w for w in (_window(w['name'], w.get('pct'), w.get('resets_at')) for w in obj['windows']) if w]
        return _unique_names(wins), obj.get('extra_usage_enabled'), 'normalized'
    plan = obj.get('plan') if isinstance(obj.get('plan'), dict) else obj
    if isinstance(plan.get('windows'), list):
        wins = []
        for w in plan['windows']:
            if isinstance(w, dict):
                win = _window(_desktop_window_name(w.get('label', '')), w.get('percentUsed'), w.get('resetsAt'))
                if win:
                    wins.append(win)
        extra = plan.get('extraUsage') or obj.get('extraUsage') or {}
        enabled = extra.get('enabled', extra.get('isEnabled')) if isinstance(extra, dict) else None
        if wins:
            return _unique_names(wins), enabled, 'desktop'
    wins = _oauth_windows(obj)
    if wins:
        extra = obj.get('extra_usage') if isinstance(obj.get('extra_usage'), dict) else {}
        return wins, extra.get('is_enabled'), 'oauth'
    raise ValueError('no usage windows recognized (expected the OAuth or desktop get_usage shape)')


# ---------------------------------------------------------------- Claude: sources

def _credentials_path() -> str:
    return os.path.join(common.CLAUDE_DIR, '.credentials.json')


def _oauth_token(creds_path: str | None = None) -> tuple[str | None, str]:
    """(token, why-not). The token never leaves this module's call stack."""
    d = common.read_json(creds_path or _credentials_path(), {}) or {}
    o = d.get('claudeAiOauth') or {}
    tok, exp = o.get('accessToken'), common.parse_time(o.get('expiresAt'))
    if not tok:
        return None, 'no CLI OAuth token'
    if exp is not None and exp <= common.now():
        return None, f'CLI OAuth token expired {common.iso(exp)}'
    return tok, ''


def _http_get(url: str, headers: dict, timeout: float) -> tuple[int, str]:
    req = urllib.request.Request(url, method='GET')
    for name, value in headers.items():
        req.add_unredirected_header(name, value)
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, msg, response_headers, newurl):
            return None
    opener = urllib.request.build_opener(NoRedirect)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode('utf-8', errors='replace')
    except urllib.error.HTTPError as e:
        code = e.code
        try:
            e.close()
        except (KeyError, OSError):
            pass
        return code, ''


def _try_oauth(creds_path=None, _http=None) -> tuple[dict | None, str]:
    token, why = _oauth_token(creds_path)
    if not token:
        return None, why
    headers = {'Authorization': f'Bearer {token}', 'anthropic-beta': 'oauth-2025-04-20',
               'Accept': 'application/json', 'User-Agent': 'twinlane/1'}
    try:
        code, body = (_http or _http_get)(OAUTH_URL, headers, OAUTH_TIMEOUT_S)
    except Exception as e:  # network errors never carry the request headers
        return None, f'oauth unreachable ({type(e).__name__})'
    if code != 200:
        return None, f'oauth HTTP {code}'
    try:
        wins, extra, _ = normalize_claude_payload(body)
    except ValueError as e:
        return None, f'oauth response unreadable: {e}'
    return {'source': 'oauth', 'reading_at': common.iso(common.now()), 'windows': wins,
            'extra_usage_enabled': extra, 'note': ''}, ''


def save_claude_cache(reading: dict, source: str) -> None:
    data = {'reading_at': reading.get('reading_at') or common.iso(common.now()), 'source': source,
            'windows': reading.get('windows') or [], 'extra_usage_enabled': reading.get('extra_usage_enabled')}

    def decide(cur):
        return None if isinstance(cur, dict) and _older(data['reading_at'], cur.get('reading_at')) else data

    _cache_update(claude_cache_path(), decide)


def load_claude_cache() -> dict | None:
    d = common.read_json(claude_cache_path())
    if not isinstance(d, dict):
        return None
    try:
        wins, extra, _ = normalize_claude_payload(d)
    except ValueError:
        return None
    if extra is None:
        extra = d.get('extra_usage_enabled')
    writer = d.get('source') or 'unknown'
    source = writer if writer in ('desktop', 'statusline') else 'cache'
    return {'source': source, 'reading_at': d.get('reading_at'), 'windows': wins,
            'extra_usage_enabled': extra, 'note': f'from cache (written by {writer})'}


def claude_refresh_argv() -> list[str]:
    """The cheapest real Claude call; it makes the CLI refresh its expired OAuth token."""
    return [common.claude_bin() or 'claude', '-p', '--model', 'claude-haiku-4-5', '--max-turns', '1',
            '--tools', '', '--strict-mcp-config', '--mcp-config', '{"mcpServers":{}}',
            '--settings', '{"disableAllHooks":true}', '--disable-slash-commands',
            '--output-format', 'json', 'Reply OK']


def _refresh_token(_run=None) -> str:
    """Run the refresh call (spends a tiny haiku call). Returns a note with its token usage."""
    argv = claude_refresh_argv()
    rc, out, _err = (_run or common.run)(argv, cwd=common.ROOT, timeout=180, env=common.clean_claude_env())
    usage = {}
    try:
        payload = json.loads(out or '{}')
        if isinstance(payload, dict) and isinstance(payload.get('usage'), dict):
            usage = payload['usage']
    except ValueError:
        pass
    toks = ', '.join(f'{k}={usage[k]}' for k in ('input_tokens', 'output_tokens', 'cache_read_input_tokens',
                                                  'cache_creation_input_tokens') if k in usage)
    return f'refresh call rc={rc}' + (f' ({toks})' if toks else '')


def token_expired_at(creds_path: str | None = None) -> str | None:
    """When the CLI OAuth token expired (ISO), or None when there is no token or it is still valid."""
    d = common.read_json(creds_path or _credentials_path(), {}) or {}
    o = d.get('claudeAiOauth') or {}
    exp = common.parse_time(o.get('expiresAt'))
    return common.iso(exp) if o.get('accessToken') and exp is not None and exp <= common.now() else None


def refresh_blocker() -> str | None:
    """Why the refresh call must not run, or None. It is a real model call, and an expired token means
    the live usage endpoint cannot say whether extra usage is on, so the last recorded state decides:
    only a recorded OFF allows it (ON and unknown both block). twinlane has no refresh without a model call."""
    cached = load_claude_cache()
    extra = None if cached is None else cached.get('extra_usage_enabled')
    if extra is False:
        return None
    state = 'extra usage is ON' if extra else 'extra usage state unknown'
    return (f'{state}: no model call to renew the token; record a reading that shows extra usage OFF '
            '(twinlane meter --record-claude <get_usage json>), or renew the CLI login yourself')


def read_claude_raw(refresh: bool = False, creds_path=None, _http=None, _run=None) -> dict:
    reading, why = _try_oauth(creds_path, _http)
    if reading:
        save_claude_cache(reading, 'oauth')
        return reading
    notes = [why]
    blocked = refresh_blocker() if refresh else None
    if blocked:
        notes.append(f'refresh not run: {blocked}')
    elif refresh:
        notes.append(_refresh_token(_run))
        reading, why2 = _try_oauth(creds_path, _http)
        if reading:
            reading['note'] = _join(*notes, 'token refreshed')
            save_claude_cache(reading, 'oauth')
            return reading
        notes.append(f'after refresh: {why2}')
    # An expired token is what an auto refresh keys on; it is recorded whichever fallback answers.
    expired = token_expired_at(creds_path)
    cached = load_claude_cache()
    if cached:
        cached['note'] = _join(*notes, cached['note'])
        cached['token_expired_at'] = expired
        return cached
    return {'source': 'none', 'token_expired_at': expired,
            'note': _join(*notes, 'no cached reading; renew the Claude CLI login if it has expired, '
                                  'then run twinlane meter --fresh, '
                                  'or record a reading with '
                                  'twinlane meter --record-claude <get_usage json>')}


# ---------------------------------------------------------------- Claude: automatic token refresh

def auto_refresh_enabled(policy: dict | None) -> bool:
    """Optional token renewal; OFF unless a user explicitly enables it."""
    return bool(((policy or {}).get('meter') or {}).get('auto_refresh_claude', False))


REFRESH_CLAIMS_KEEP = 50  # how many claimed expiries the claims file lists; older ones fold into its floor


def _claims_path() -> str:
    return os.path.join(common.STATE, 'claude-refresh-claims.json')


def _expiry_key(expired_at) -> int | None:
    t = common.parse_time(expired_at)
    return None if t is None else int(t.timestamp())


def _claims_doc(cur) -> dict | None:
    """The claims file as {'floor': int|None, 'claimed': [int]}; None when it exists but cannot be trusted
    (every caller then fails closed: nothing is claimable and nothing is pending, so no model call)."""
    if cur is None and not os.path.exists(_claims_path()):
        return {'floor': None, 'claimed': []}
    if not isinstance(cur, dict) or not isinstance(cur.get('claimed'), list)             or not all(isinstance(k, int) and not isinstance(k, bool) for k in cur['claimed'])             or not (cur.get('floor') is None or (isinstance(cur['floor'], int) and not isinstance(cur['floor'], bool))):
        return None
    return {'floor': cur.get('floor'), 'claimed': list(cur['claimed'])}


def _is_claimed(doc: dict, key: int) -> bool:
    return key in doc['claimed'] or (doc['floor'] is not None and key <= doc['floor'])


def refresh_pending(expired_at) -> bool:
    """True while no automatic refresh has been tried for the token that expired at `expired_at`."""
    key = _expiry_key(expired_at)
    doc = _claims_doc(common.read_json(_claims_path()))
    return key is not None and doc is not None and not _is_claimed(doc, key)


def _claim_refresh(expired_at) -> str | None:
    """Take the one automatic refresh allowed per expiry. The claims live in ONE file updated under a lock
    (two commands starting together spend one call). A claim is permanent: the file keeps the last
    REFRESH_CLAIMS_KEEP claimed expiries and, when a claim pushes the oldest out, raises `floor` to it;
    every expiry at or below the floor counts as claimed. The expiry being claimed is never the one dropped,
    and claiming B can never make A pending again. Returns the file path, or None when it is already
    claimed, cannot be trusted or cannot be written (a broken state dir never loops on model calls)."""
    key = _expiry_key(expired_at)
    if key is None:
        return None
    won: list = []

    def decide(cur):
        doc = _claims_doc(cur)
        if doc is None or _is_claimed(doc, key):
            return None
        claimed = sorted(set(doc['claimed']) | {key})
        floor = doc['floor']
        while len(claimed) > REFRESH_CLAIMS_KEEP:
            gone = next(k for k in claimed if k != key)
            claimed.remove(gone)
            floor = gone if floor is None else max(floor, gone)
        won.append(1)
        return {**{k: v for k, v in (cur or {}).items() if k == 'last'}, 'floor': floor, 'claimed': claimed}

    try:
        _locked_update(_claims_path(), decide)
    except OSError:
        return None
    return _claims_path() if won else None


def _record_refresh_outcome(outcome: dict) -> None:
    """Keep the last attempt's result in the claims file (informational; the claim itself already stands)."""
    try:
        _locked_update(_claims_path(), lambda cur: dict(cur, last=outcome) if isinstance(cur, dict) else None)
    except OSError:
        pass


def read_claude_auto(policy: dict | None = None, creds_path=None, _http=None, _run=None) -> dict:
    """read_claude_raw, plus ONE automatic refresh per token expiry when the reading would be unknown
    because the CLI token expired (the overnight case). The hook and `twinlane meter` never come here."""
    raw = read_claude_raw(False, creds_path, _http, _run)
    expired = raw.get('token_expired_at')
    if not expired or not auto_refresh_enabled(policy) or finish_claude(raw, policy or {})['status'] != 'unknown':
        return raw
    blocked = refresh_blocker()
    if blocked:  # checked before the claim, so recording an OFF reading later still allows this expiry its try
        raw['note'] = _join(raw.get('note'), f'automatic refresh not run: {blocked}')
        return raw
    marker = _claim_refresh(expired)
    if marker is None:
        raw['note'] = _join(raw.get('note'), 'automatic refresh already tried for this expiry '
                                             '(or cannot be recorded): renew the Claude CLI login, '
                                             'then run twinlane meter --fresh')
        return raw
    out = read_claude_raw(True, creds_path, _http, _run)
    out['refresh'] = {'auto': True, 'at': common.iso(common.now()), 'expired_at': expired,
                      'ok': out.get('source') == 'oauth'}
    _record_refresh_outcome(dict(out['refresh'], claimed_at=common.iso(common.now())))
    return out


# ---------------------------------------------------------------- Codex: sources

DEFAULT_LIMIT_ID = 'codex'
# Keys that identify an account or a reset credit: never copied into a reading, which twinlane prints and caches.
_ID_KEYS = {'id', 'accountid', 'account_id', 'userid', 'user_id', 'creditid', 'credit_id'}
# Reset credits that are spent or void; anything else (even a status twinlane has never seen) still counts as owned.
_DEAD_RESET_STATUSES = {'consumed', 'used', 'expired', 'revoked', 'redeemed', 'cancelled', 'canceled', 'void'}


def _number(v) -> float | None:
    """A JSON number or a numeric string ("12.50", "$12.50") as a float; anything else None."""
    if isinstance(v, bool):
        return None
    if isinstance(v, str):
        v = v.strip().lstrip('$').replace(',', '')
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x and x not in (float('inf'), float('-inf')) else None


def _plain(v, depth: int = 3):
    """A JSON-safe, id-free, size-bounded copy of an unknown value (for individualLimit)."""
    if isinstance(v, (bool, int, float)) or v is None:
        return v
    if isinstance(v, str):
        return common.clip(v, 80)
    if depth <= 0:
        return None
    if isinstance(v, dict):
        return {str(k): _plain(x, depth - 1) for k, x in list(v.items())[:12] if str(k).lower() not in _ID_KEYS}
    if isinstance(v, list):
        return [_plain(x, depth - 1) for x in v[:12]]
    return None


def _app_windows(rl: dict, limit_id: str | None = None, limit_name=None) -> list[dict]:
    """primary/secondary of one rate-limit object. Windows of a limit other than the default carry
    their limit id and are named '<id>:<window>' so two limits never share a window name."""
    wins = []
    for key in ('primary', 'secondary'):
        w = rl.get(key)
        if isinstance(w, dict):
            name = window_name_from_minutes(w.get('windowDurationMins'))
            win = _window(f'{limit_id}:{name}' if limit_id else name, w.get('usedPercent'), w.get('resetsAt'))
            if win:
                if limit_id:
                    win['limit'], win['limit_name'] = limit_id, limit_name
                wins.append(win)
    return wins


def _reset_credits(obj) -> dict | None:
    """rateLimitResetCredits -> {available, expires[]} (read-only: twinlane never consumes a reset, and the
    credit ids are dropped here)."""
    if not isinstance(obj, dict):
        return None
    items = [c for c in obj.get('credits') or [] if isinstance(c, dict)]
    live = [c for c in items if str(c.get('status') or '').lower() not in _DEAD_RESET_STATUSES]
    expires = sorted(t for t in (common.parse_time(c.get('expiresAt')) for c in live) if t is not None)
    n = obj.get('availableCount')
    count = int(n) if isinstance(n, (int, float)) and not isinstance(n, bool) and n >= 0 else len(live)
    return {'available': count, 'expires': [common.iso(t) for t in expires]}


def parse_app_server(result: dict) -> dict:
    """account/rateLimits/read result -> raw codex reading: every window of every limit id, the plan,
    credits (balance as a number), spend control, the individual limit and the free full resets.
    The account id, the reset-credit ids and the upsell are never copied."""
    result = result or {}
    by_id = result.get('rateLimitsByLimitId')
    by_id = by_id if isinstance(by_id, dict) else {}
    rl = result.get('rateLimits')
    if not isinstance(rl, dict):
        rl = by_id.get(DEFAULT_LIMIT_ID) if isinstance(by_id.get(DEFAULT_LIMIT_ID), dict) else \
            next((v for v in by_id.values() if isinstance(v, dict)), {})
    default_id = rl.get('limitId') or DEFAULT_LIMIT_ID
    wins = _app_windows(rl)
    limits = [{'id': default_id, 'name': rl.get('limitName'), 'model': rl.get('normalModelSlug'), 'default': True}]
    for key, other in by_id.items():
        if not isinstance(other, dict):
            continue
        oid = str(other.get('limitId') or key)
        if oid == default_id or key == default_id:  # the default limit is listed under both
            continue
        wins += _app_windows(other, oid, other.get('limitName'))
        limits.append({'id': oid, 'name': other.get('limitName'), 'model': other.get('normalModelSlug'),
                       'default': False})
    cr = rl.get('credits') or {}
    ordinary = result.get('ordinaryUsageAllowed', rl.get('ordinaryUsageAllowed'))
    spend = result.get('spendControlReached', rl.get('spendControlReached'))
    individual = result.get('individualLimit', rl.get('individualLimit'))
    resets = _reset_credits(result.get('rateLimitResetCredits') or rl.get('rateLimitResetCredits'))
    return {'source': 'app-server', 'reading_at': common.iso(common.now()), 'windows': wins, 'limits': limits,
            'credits': {'has': bool(cr.get('hasCredits')), 'unlimited': bool(cr.get('unlimited')),
                        'balance': _number(cr.get('balance'))},
            'limit_reached_type': rl.get('rateLimitReachedType'), 'ordinary_usage_allowed': ordinary,
            'spend_control_reached': spend if isinstance(spend, bool) else None,
            'individual_limit': _plain(individual), 'free_resets': resets,
            'plan': rl.get('planType'), 'note': ''}


class AppServerError(RuntimeError):
    """A JSON-RPC error reply; `code` is the numeric error code (None when absent or not a number)."""

    def __init__(self, code, message):
        try:
            self.code = int(code)
        except (TypeError, ValueError):
            self.code = None
        super().__init__(f'app-server error {code}: {common.clip(message, 120)}')


SHAPE_ERRORS = (-32600, -32602)  # invalid request / invalid params: the server wants another params shape


def _send(proc, msg: dict) -> None:
    proc.stdin.write((json.dumps(msg) + '\n').encode('utf-8'))
    proc.stdin.flush()


def _await_id(q: queue.Queue, want_id: int, deadline: float) -> dict:
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise TimeoutError(f'no reply to request {want_id} within {APP_SERVER_TIMEOUT_S}s')
        try:
            line = q.get(timeout=left)
        except queue.Empty:
            continue
        if line is None:
            raise ConnectionError('app-server closed its output')
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        if isinstance(msg, dict) and msg.get('id') == want_id and ('result' in msg or 'error' in msg):
            if 'error' in msg:
                err = msg['error'] if isinstance(msg['error'], dict) else {}
                raise AppServerError(err.get('code'), err.get('message'))
            return msg['result']


def _stop_process(proc) -> None:
    """Always leave nothing behind: close stdin, give it a moment, then kill the tree."""
    try:
        proc.stdin.close()
    except Exception:
        pass
    try:
        proc.wait(timeout=1)
    except subprocess.TimeoutExpired:
        common.kill_tree(proc.pid)
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    try:
        proc.stdout.close()
    except Exception:
        pass


def app_server_exchange(argv: list[str], timeout: float = APP_SERVER_TIMEOUT_S) -> dict:
    """initialize -> initialized -> account/rateLimits/read over stdio JSON lines; returns the result.
    Only these read-only methods are ever sent."""
    flags = 0x08000000 if common.IS_WIN else 0  # CREATE_NO_WINDOW: no console flash
    proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            creationflags=flags, start_new_session=not common.IS_WIN)
    q: queue.Queue = queue.Queue()

    def pump():
        try:
            for raw in iter(proc.stdout.readline, b''):
                q.put(raw.decode('utf-8', errors='replace'))
        finally:
            q.put(None)

    threading.Thread(target=pump, daemon=True).start()
    deadline = time.monotonic() + timeout
    try:
        _send(proc, {'method': 'initialize', 'id': 1, 'params': {
            'clientInfo': {'name': 'twinlane', 'title': 'twinlane', 'version': '1'}}})
        _await_id(q, 1, deadline)
        _send(proc, {'method': 'initialized'})
        # Newer app-servers take no params ("invalid type: map, expected unit"); older ones accepted
        # excludeResetCreditDetails. Ask the current shape first, the old one only on a shape refusal.
        _send(proc, {'method': 'account/rateLimits/read', 'id': 2})
        try:
            return _await_id(q, 2, deadline)
        except AppServerError as e:
            if e.code not in SHAPE_ERRORS:
                raise
            first = e
        # A server that exits after refusing the shape breaks the retry: report its refusal, not the pipe.
        try:
            _send(proc, {'method': 'account/rateLimits/read', 'id': 3, 'params': {'excludeResetCreditDetails': True}})
        except (OSError, ValueError):
            raise first from None
        try:
            return _await_id(q, 3, deadline)
        except ConnectionError:
            raise first from None
    finally:
        _stop_process(proc)


def _exchange_reading(argv: list[str], timeout: float) -> tuple[dict | None, str]:
    try:
        return parse_app_server(app_server_exchange(argv, timeout)), ''
    except (OSError, TimeoutError, ConnectionError, RuntimeError, ValueError) as e:
        return None, f'app-server failed: {common.clip(str(e) or type(e).__name__, 160)}'


def read_app_server(_argv=None, timeout: float = APP_SERVER_TIMEOUT_S) -> tuple[dict | None, str]:
    if _argv is not None:
        return _exchange_reading(_argv, timeout)
    # The private copy is leased for the exchange, so no prune removes it mid-read. No usable codex (twinlane never
    # runs the app's own exe or the PATH one) or a failed lookup leaves the caller to the rollouts.
    try:
        with common.codex_lease() as (binary, why):
            if not binary:
                return None, f'codex binary not found: {why}'
            reading, err = _exchange_reading([binary, 'app-server'], timeout)
            if why:  # an older copy or explicit binary: show it in `twinlane status` via the reading's note
                if reading:
                    reading['note'] = _join(reading.get('note'), f'codex CLI: {why}')
                else:
                    err = _join(err, f'codex CLI: {why}')
            return reading, err
    except OSError as e:
        return None, f'codex binary lookup failed: {common.clip(str(e) or type(e).__name__, 140)}'


def _rollout_line_reading(line: str):
    if '"rate_limits"' not in line or '"primary"' not in line:
        return None
    try:
        d = json.loads(line)
    except ValueError:
        return None
    p = d.get('payload') or {}
    rl = p.get('rate_limits') or (p.get('info') or {}).get('rate_limits')
    return (d.get('timestamp') or '', rl) if rl else None


def rollout_reading(sessions_root: str | None = None, max_files: int = ROLLOUT_FILES) -> dict | None:
    """Newest rate-limit snapshot from the newest rollouts (the codex_quota.py logic), tail-read."""
    root = sessions_root or os.path.join(common.CODEX_DIR, 'sessions')
    files = glob.glob(os.path.join(root, '**', 'rollout-*.jsonl'), recursive=True)
    files = sorted(files, key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0)[-max_files:]
    best = None
    for f in files:
        for line in common.tail_lines(f):
            r = _rollout_line_reading(line)
            if r and (best is None or r[0] >= best[0]):
                best = r
    if not best:
        return None
    ts, rl = best
    wins = []
    for key in ('primary', 'secondary'):
        w = rl.get(key)
        if isinstance(w, dict):
            win = _window(window_name_from_minutes(w.get('window_minutes')), w.get('used_percent'),
                          w.get('resets_at'))
            if win:
                wins.append(win)
    cr = rl.get('credits') or {}
    return {'source': 'rollout', 'reading_at': common.iso(common.parse_time(ts)), 'windows': wins,
            'credits': {'has': bool(cr.get('has_credits')), 'unlimited': bool(cr.get('unlimited')),
                        'balance': _number(cr.get('balance'))},
            'limit_reached_type': rl.get('rate_limit_reached_type'), 'ordinary_usage_allowed': None,
            'plan': rl.get('plan_type'), 'note': ''}


TOKEN_KEYS = ('input_tokens', 'cached_input_tokens', 'cache_write_input_tokens', 'output_tokens',
              'reasoning_output_tokens', 'total_tokens')
_SESSION_ID = re.compile(r'[0-9A-Za-z-]{8,64}')


def int_fields(d, keys) -> dict:
    return {k: int(d[k]) for k in keys if isinstance(d.get(k), (int, float)) and not isinstance(d.get(k), bool)}


def rollout_token_usage(session_id, sessions_root: str | None = None) -> dict | None:
    """Cumulative token use of one Codex session: the last `token_count` event of its rollout file
    (`payload.info.total_token_usage`). None when the session has no rollout or no such event yet."""
    if not isinstance(session_id, str) or not _SESSION_ID.fullmatch(session_id):
        return None
    root = sessions_root or os.path.join(common.CODEX_DIR, 'sessions')
    files = glob.glob(os.path.join(root, '*', '*', '*', f'rollout-*{session_id}.jsonl'))
    if not files:
        return None
    path = max(files, key=lambda f: os.path.getmtime(f) if os.path.exists(f) else 0)
    for line in reversed(common.tail_lines(path, 600_000)):
        if '"token_count"' not in line or '"total_token_usage"' not in line:
            continue
        try:
            d = json.loads(line)
        except ValueError:
            continue
        info = ((d.get('payload') or {}).get('info')) if isinstance(d, dict) else None
        total = info.get('total_token_usage') if isinstance(info, dict) else None
        if isinstance(total, dict) and int_fields(total, TOKEN_KEYS):
            return {'source': 'rollout', **int_fields(total, TOKEN_KEYS)}
    return None


def read_codex_raw(_argv=None, sessions_root=None, timeout: float = APP_SERVER_TIMEOUT_S) -> dict:
    reading, why = read_app_server(_argv, timeout)
    if reading:
        return reading
    fallback = rollout_reading(sessions_root)
    if fallback:
        fallback['note'] = _join(why, 'using the newest rollout reading')
        return fallback
    return {'source': 'none', 'note': _join(why, 'no rollout reading')}


# ---------------------------------------------------------------- read / cache

def _safe(reader) -> dict:
    try:
        r = reader()
        return r if isinstance(r, dict) else {'source': 'none', 'note': 'reader returned nothing'}
    except Exception as e:
        return {'source': 'none', 'note': f'reader error: {type(e).__name__}: {common.clip(str(e), 120)}'}


def _cached_meter(policy: dict) -> dict | None:
    m = common.read_json(meter_path())
    if not isinstance(m, dict) or not isinstance(m.get('claude'), dict) or not isinstance(m.get('codex'), dict):
        return None
    age = common.age_s(m.get('read_at'))
    if age is None or age > (policy.get('meter') or {}).get('cache_ttl_s', 120):
        return None
    for eng in ('claude', 'codex'):
        a = common.age_s(m[eng].get('reading_at'))
        m[eng]['age_s'] = None if a is None else int(a)
    return m


def _cached_needs_refresh(cached: dict) -> bool:
    """The cached Claude reading is unknown because the token expired, and no automatic refresh was tried yet."""
    c = cached.get('claude') or {}
    return c.get('status') == 'unknown' and bool(c.get('token_expired_at')) and refresh_pending(c['token_expired_at'])


def read(fresh: bool = False, refresh_claude: bool = False, policy: dict | None = None,
         auto_refresh: bool = False, _claude_reader=None, _codex_reader=None, _run=None, _http=None) -> dict:
    """Both meters. Uses state/meter.json when younger than the TTL unless fresh/refresh_claude.

    auto_refresh (status, route, run; never the hook or plain `twinlane meter`): when the Claude reading is
    unknown because the CLI token expired, spend ONE tiny Haiku call per expiry to renew it
    (policy.meter.auto_refresh_claude, default off). The result then carries `refreshed_now: ['claude']`."""
    policy = policy or common.load_policy()
    started = common.now()  # the cache stamp is when this read began: no reading in it is older than that
    auto = auto_refresh and not refresh_claude and auto_refresh_enabled(policy)
    if not fresh and not refresh_claude:
        cached = _cached_meter(policy)
        if cached and not (auto and _cached_needs_refresh(cached)):
            return cached
    if _claude_reader:
        claude_reader = _claude_reader
    elif auto:
        claude_reader = lambda: read_claude_auto(policy, _http=_http, _run=_run)  # noqa: E731
    else:
        claude_reader = lambda: read_claude_raw(refresh=refresh_claude, _http=_http, _run=_run)  # noqa: E731
    readers = {'claude': claude_reader, 'codex': _codex_reader or read_codex_raw}
    raw: dict = {}

    queue_ = getattr(_deferral, 'queue', None)  # the reader threads inherit this thread's deferral

    def run_one(name):
        _deferral.queue = queue_
        raw[name] = _safe(readers[name])

    threads = [threading.Thread(target=run_one, args=(n,), daemon=True) for n in readers]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    now = common.now()
    m = {'read_at': common.iso(started), 'read_us': _us(started), 'claude': finish_claude(raw.get('claude'), policy, now),
         'codex': finish_codex(raw.get('codex'), policy, now)}
    _cache_update(meter_path(), lambda cur: _merge_meter(cur, m))
    if (raw.get('claude') or {}).get('refresh'):
        m['refreshed_now'] = ['claude']  # this call's own news; never written to the cache
    return m


def _merge_meter(cur, m: dict) -> dict:
    """The new meter.json content: `m`, except that an engine section keeps the cached one when the cached
    reading is newer (strictly later reading_at, or, with no later reading_at on our side, a read that began
    after ours), and read_at never moves backwards."""
    if not isinstance(cur, dict):
        return m
    out = dict(m)
    stale = isinstance(cur.get('read_us'), int) and cur['read_us'] > m['read_us']  # we began before the cached read
    for eng in ('claude', 'codex'):
        have = cur.get(eng)
        if not isinstance(have, dict):
            continue
        ours_at, have_at = (m.get(eng) or {}).get('reading_at'), have.get('reading_at')
        if _older(ours_at, have_at) or (stale and not _older(have_at, ours_at)):
            out[eng] = have
    if stale:
        out['read_at'], out['read_us'] = cur.get('read_at'), cur['read_us']
    return out


def refresh_notice(m: dict) -> str | None:
    """One plain sentence when this very read renewed the Claude token on its own, else None."""
    if 'claude' not in (m.get('refreshed_now') or []):
        return None
    ok = ((m.get('claude') or {}).get('refresh') or {}).get('ok')
    return ('Claude CLI token had expired: refreshed automatically with one tiny Haiku call'
            if ok else 'Claude CLI token had expired: the automatic refresh (one tiny Haiku call) did not restore '
                       'a reading; it will not retry for this expiry. Renew the Claude CLI login, '
                       'then run twinlane meter --fresh')


# ---------------------------------------------------------------- per-job usage (receipt `usage`)

RESET_JITTER_S = 120  # a window whose reset moved by more than this rolled over between two readings


def usage_point(sec) -> dict | None:
    """The part of one engine's meter section a receipt keeps: when it was read and each window's percent."""
    if not isinstance(sec, dict) or sec.get('source') in (None, 'none') or not sec.get('reading_at'):
        return None
    wins = {w['name']: {'pct': w['pct'], 'resets_at': w.get('resets_at'), 'rolled_over': bool(w.get('rolled_over'))}
            for w in sec.get('windows') or [] if w.get('name') and w.get('pct') is not None}
    if not wins:
        return None
    return {'reading_at': sec['reading_at'], 'source': sec['source'], 'status': sec.get('status'), 'windows': wins}


def usage_delta(before: dict | None, after: dict | None) -> dict:
    """Percentage-point change per window between two usage_points: {'delta': {...}|None, 'why': ...}.
    Nothing is invented: no delta without two different readings, and a window that reset between them is skipped."""
    if before is None or after is None:
        return {'delta': None, 'why': f'no {"before" if before is None else "after"} reading'}
    tb, ta = common.parse_time(before['reading_at']), common.parse_time(after['reading_at'])
    if tb is None or ta is None or ta <= tb:
        return {'delta': None, 'why': 'no newer reading after the job than before it'}
    delta, skipped = {}, {}
    for name, b in before['windows'].items():
        a = after['windows'].get(name)
        if a is None:
            continue
        rb, ra = common.parse_time(b.get('resets_at')), common.parse_time(a.get('resets_at'))
        if b.get('rolled_over') or a.get('rolled_over') or (rb and ra and abs((ra - rb).total_seconds()) > RESET_JITTER_S):
            skipped[name] = 'window reset during the job'
            continue
        delta[name] = round(a['pct'] - b['pct'], 2)
    return {'delta': delta, 'skipped': skipped or None, 'why': None}


def record_claude(payload, policy: dict | None = None, source: str | None = None) -> dict:
    """Store a Claude reading fed from outside (desktop get_usage, statusline) and patch the meter cache."""
    policy = policy or common.load_policy()
    obj = json.loads(payload) if isinstance(payload, str) else payload
    wins, extra, shape = normalize_claude_payload(obj)
    when = None
    if isinstance(obj, dict):
        when = common.parse_time(obj.get('reading_at') or obj.get('readAt') or obj.get('fetchedAt'))
        if when is not None and when > common.now():
            when = common.now()  # a writer's clock ahead of ours must not make the reading look fresh forever
    reading = {'reading_at': common.iso(when or common.now()), 'windows': wins, 'extra_usage_enabled': extra}
    src = source or ('desktop' if shape == 'desktop' else 'statusline')
    save_claude_cache(reading, src)
    section = finish_claude(load_claude_cache(), policy)
    def patch(cur):
        if not (isinstance(cur, dict) and isinstance(cur.get('codex'), dict)):
            return None
        if isinstance(cur.get('claude'), dict) and _older(section.get('reading_at'), cur['claude'].get('reading_at')):
            return None
        return dict(cur, claude=section)

    _cache_update(meter_path(), patch)
    return section


# ---------------------------------------------------------------- human output

def _fmt_pct(p) -> str:
    if p is None:
        return '?'
    return f'{p:.0f}%' if abs(p - round(p)) < 0.05 else f'{p:.1f}%'


def _fmt_reset(v, now) -> str:
    t = common.parse_time(v)
    if t is None:
        return '?'
    return t.strftime('%H:%MZ') if t.date() == now.date() else t.strftime('%m-%d %H:%MZ')


def _fmt_age(s) -> str:
    if s is None:
        return '?'
    s = int(s)
    if s < 90:
        return f'{s}s'
    if s < 5400:
        return f'{s // 60}m'
    if s < 172800:
        return f'{s // 3600}h'
    return f'{s // 86400}d'


def _fmt_windows(sec: dict, now, say_resets: bool) -> list[str]:
    """Every window of the reading except another limit id's (listed on its own line by _detail_lines);
    nothing the router counts is left out, and the binding one is marked."""
    parts = []
    wins = sec.get('windows') or []
    bi = binding_index(wins)
    for i, w in enumerate(wins):
        if w.get('limit'):
            continue
        reset = _fmt_reset(w.get('resets_at'), now)
        when = f'resets {reset}' if say_resets and not parts else reset
        parts.append(f'{window_label(w)} {_fmt_pct(w.get("pct"))} ({when})' + (' [binding]' if i == bi else ''))
    return parts


def _fmt_engine(name: str, sec: dict, now, say_resets: bool = True) -> str:
    """One engine's segment; only the line's first window spells out 'resets'."""
    sec = sec or {}
    parts = _fmt_windows(sec, now, say_resets)
    if name == 'claude' and sec.get('extra_usage_enabled') is not None:
        parts.append(f'extra {"on" if sec.get("extra_usage_enabled") else "off"}')
    if name == 'codex' and sec.get('credits'):
        cr = sec['credits']
        bal = cr.get('balance')
        parts.append(f'credits {"yes" if cr.get("has") else "no"}'
                     + (' unlimited' if cr.get('unlimited') else '')
                     + (f' ({bal:g})' if isinstance(bal, (int, float)) and not isinstance(bal, bool) else ''))
        if sec.get('on_credits'):
            parts.append('ON CREDITS')
    if name == 'codex' and sec.get('spend_control_reached'):
        parts.append('SPEND CONTROL REACHED')
    body = ' · '.join(parts)
    src = sec.get('source') or 'none'
    tag = f'[{src}, {_fmt_age(sec.get("age_s"))} old]' if src != 'none' else '[none]'
    return f'{name}: {sec.get("status", "unknown")} ' + (f'{body} ' if body else '') + tag


def _fmt_day(v) -> str:
    t = common.parse_time(v)
    return '?' if t is None else t.strftime('%m-%d')


def _resets_text(fr: dict) -> str | None:
    """'2 free full resets (soonest expires 10-28; also 11-02)'. Read-only: twinlane never consumes a reset."""
    n = int((fr or {}).get('available') or 0)
    if n <= 0:
        return None
    exp = (fr or {}).get('expires') or []
    text = f'{n} free full reset{"s" if n != 1 else ""}'
    if not exp:
        return text
    if n == 1 or len(exp) == 1:
        return f'{text} (expires {_fmt_day(exp[0])})'
    return f'{text} (soonest expires {_fmt_day(exp[0])}; also {", ".join(_fmt_day(e) for e in exp[1:])})'


def _detail_lines(m: dict, now) -> list[str]:
    """Codex facts beyond the first line: plan, individual limit, free resets, every other limit id."""
    sec = m.get('codex') or {}
    out, bits = [], []
    default = next((lim for lim in sec.get('limits') or [] if lim.get('default')), None) or {}
    if sec.get('plan'):
        bits.append(f'plan {sec["plan"]}')
    if default.get('name'):
        bits.append(f'limit {default["name"]}')
    if sec.get('individual_limit') is not None:
        bits.append(f'individual limit {json.dumps(sec["individual_limit"], ensure_ascii=False)}')
    resets = _resets_text(sec.get('free_resets'))
    if resets:
        bits.append(resets)
    if bits:
        out.append('  codex: ' + ' · '.join(bits))
    names = {lim.get('id'): lim.get('name') for lim in sec.get('limits') or []}
    groups: dict = {}
    all_wins = sec.get('windows') or []
    bi = binding_index(all_wins)
    for i, w in enumerate(all_wins):
        if w.get('limit'):
            groups.setdefault(w['limit'], []).append((i, w))
    for lid, wins in groups.items():
        # names here come from the provider's reply, so each passes safe_window_name too
        shown = ' · '.join(f'{(safe_window_name(w.get("name")) or INVALID_WINDOW).split(":", 1)[-1]} '
                           f'{_fmt_pct(w.get("pct"))} ({_fmt_reset(w.get("resets_at"), now)})'
                           + (' [binding]' if i == bi else '') for i, w in wins)
        safe_lid = safe_window_name(lid) or INVALID_WINDOW
        raw = names.get(lid)  # a display name ('GPT-6 Astra') may hold spaces, never control characters
        safe_name = raw if isinstance(raw, str) and 0 < len(raw) <= 60 and raw.isprintable() else None
        label = f'{safe_name} [{safe_lid}]' if safe_name else safe_lid
        out.append(f'  codex limit {label}: {shown}')
    refresh = (m.get('claude') or {}).get('refresh')
    if refresh:
        when = _fmt_reset(refresh.get('at'), now)
        out.append(f'  claude: CLI token had expired; refreshed automatically {when} with one tiny Haiku call'
                   + ('' if refresh.get('ok') else ' (it did not restore a reading)'))
    return out


def format_line(m: dict) -> str:
    now = common.now()
    claude_shows_windows = bool(_fmt_windows(m.get('claude') or {}, now, False))
    line = (f'{_fmt_engine("claude", m.get("claude"), now)} | '
            f'{_fmt_engine("codex", m.get("codex"), now, not claude_shows_windows)}')
    notes = [f'{n}: {m[n]["note"]}' for n in ('claude', 'codex')
             if isinstance(m.get(n), dict) and m[n].get('note')
             and m[n].get('status') not in ('ok', 'caution')]
    return line + ''.join('\n' + e for e in _detail_lines(m, now)) + ('\n  note: ' + ' | '.join(notes) if notes else '')


# ---------------------------------------------------------------- CLI

def _payload_arg(v: str) -> str:
    if v == '-':
        return sys.stdin.read()
    if v.startswith('@'):
        with open(v[1:], encoding='utf-8') as h:
            return h.read()
    return v


def cli(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane meter', description='Usage meters for Claude Code and Codex.')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--fresh', action='store_true', help='ignore the 120 s meter cache')
    ap.add_argument('--record-claude', metavar='JSON',
                    help='store a Claude reading: OAuth or desktop get_usage JSON, @file, or - for stdin')
    ap.add_argument('--source', choices=('desktop', 'statusline'), help='writer name for --record-claude')
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_OK if e.code == 0 else common.EXIT_USAGE
    policy = common.load_policy()
    if a.record_claude is not None:
        try:
            sec = record_claude(_payload_arg(a.record_claude), policy, a.source)
        except (ValueError, OSError) as e:
            print(f'twinlane meter: cannot record Claude usage: {e}', file=sys.stderr)
            return common.EXIT_USAGE
        common.emit(sec, a.json, _fmt_engine('claude', sec, common.now()))
        return common.EXIT_OK
    m = read(fresh=a.fresh, policy=policy)
    common.emit(m, a.json, format_line(m))
    return common.EXIT_OK
