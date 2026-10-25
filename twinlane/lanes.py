"""Read-only lane observations. Activity is evidence; it never closes a baton."""
from __future__ import annotations

import glob
import json
import os
import re
import time
from functools import lru_cache

from . import common, observations


def duration(value=None, policy=None):
    if value is None:
        return (policy or common.load_policy()).get('lanes', {}).get('park_timeout_s', 259200)
    match = re.fullmatch(r'([0-9]+)([smhd])', value)
    if not match or int(match[1]) < 1:
        from .baton import BatonError
        raise BatonError('--park-for needs a positive duration such as 8h or 3d')
    return int(match[1]) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400}[match[2]]


@lru_cache(maxsize=8)
def _supervisor(binary, timeout):
    try:
        from .jobs import claude_launcher
        if not binary:
            return None
        rc, out, _ = common.run(claude_launcher(binary) + ['agents', '--json'], timeout=timeout)
        data = json.loads(out) if rc == 0 else None
        if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
            return None
        keys = ('sessionId', 'cwd', 'status', 'kind', 'pid', 'startedAt', 'name', 'state', 'waitingFor')
        return [{k: r[k] for k in keys if k in r} for r in data]
    except Exception:
        return None


def supervisor_rows(policy=None, *, fresh=False):
    """Use cached listing evidence unless a destructive caller requests a fresh read."""
    cfg = (policy or common.load_policy()).get('lanes') or {}
    if not cfg.get('claude_agents', True):
        return None
    if os.environ.get('TWINLANE_TEST_ISOLATION') == '1':
        return None
    read = _supervisor.__wrapped__ if fresh else _supervisor
    return read(common.claude_bin(), cfg.get('claude_agents_timeout_s', 8))


def health(row):
    waiting = row.get('waitingFor')
    if waiting or row.get('state') in ('waiting', 'waiting-for-input', 'needs-you', 'waiting-on-user', 'waiting-on-you'):
        return 'needs-you', str(waiting or row.get('state'))[:160]
    return {'busy': ('working', None), 'idle': ('idle', None),
            'ended': ('parked', None)}.get(row.get('status'), ('unknown', None))


def state_rows(policy=None):
    cfg = (policy or common.load_policy()).get('lanes') or {}
    out = []
    for path in glob.glob(os.path.join(common.STATE, 'lanes', '*.json')):
        row = common.read_json(path)
        if not isinstance(row, dict) or row.get('engine') not in ('claude', 'codex'):
            continue
        age = common.age_s(row.get('since'))
        if age is not None and 0 <= age <= cfg.get('state_file_ttl_s', 86400):
            out.append(row)
    return out


def observation_sources(policy=None, *, fresh=False):
    """One consistent supervisor/hook snapshot for a session collection."""
    supervisor = supervisor_rows(policy, fresh=True) if fresh else supervisor_rows(policy)
    return supervisor, {(r['engine'], r.get('session_id')): r for r in state_rows(policy)}


def observe(rows, policy=None, *, sources=None, include_missing=True):
    """Supervisor wins over hook files; transcripts provide the fallback."""
    policy = policy or common.load_policy()
    supervisor, hooks = sources if sources is not None else observation_sources(policy)
    live = {r.get('sessionId'): r for r in supervisor or []}
    seen = set()
    result = []
    for original in rows:
        row = dict(original)
        key = (row['engine'], row['id'])
        seen.add(key)
        sup = live.get(row['id']) if row['engine'] == 'claude' else None
        state = hooks.get(key)
        if sup is not None:
            row['state'], row['reason'] = health(sup)
            row['supervisor'] = True
        elif state:
            row['state'], row['reason'] = state.get('state', 'unknown'), state.get('reason')
        elif supervisor is not None and row['engine'] == 'claude' and row.get('state') != 'job':
            row['state'] = 'parked'
        result.append(row)
    for sid, sup in (live.items() if include_missing else ()):
        if not isinstance(sid, str) or ('claude', sid) in seen or sup.get('kind') != 'interactive':
            continue
        stamp = sup.get('startedAt')
        at = common.parse_time(stamp / 1000) if type(stamp) in (int, float) else None
        age = max(0, (common.now() - at).total_seconds()) if at else None
        state, reason = health(sup)
        result.append({'engine': 'claude', 'id': sid, 'cwd': sup.get('cwd'),
                       '_writer_cwd': sup.get('cwd'), 'state': state, 'reason': reason,
                       'modified': common.iso(at) if at else None, 'age_s': age,
                       'age': common.fmt_age(age), 'title': sup.get('name') or '(interactive session)',
                       'context': None, 'supervisor': True, 'model': None, 'last': '', 'path': None})
    return result


def bound_ids(b):
    ids = {e['id'] for e in (b.get('bound_sessions') if isinstance(b.get('bound_sessions'), list) else []) if isinstance(e, dict) and isinstance(e.get('id'), str)}
    for sid in ((b.get('owner') if isinstance(b.get('owner'), dict) else {}).get('session_id'), b.get('bound_session')):
        if isinstance(sid, str):
            ids.add(sid)
    return ids


def _git_activity(cwd):
    # Git reflog timestamps include local commits, checkouts and rebases.
    dotgit = os.path.join(observations.worktree_top(cwd) or cwd, '.git')
    gitdir = dotgit
    if os.path.isfile(dotgit):
        try:
            with open(dotgit, encoding='utf-8') as handle:
                text = handle.read().strip()
            gitdir = os.path.normpath(os.path.join(os.path.dirname(dotgit), text.partition(':')[2].strip()))
        except OSError:
            gitdir = None
    for rel in ('logs/HEAD', 'HEAD'):
        try:
            return os.path.getmtime(os.path.join(gitdir, rel))
        except (OSError, TypeError):
            pass
    return None


@observations.command
def last_activity(b, policy=None, session_rows=None):
    from . import baton, sessions
    policy = policy or common.load_policy()
    times = []
    def add(value):
        stamp = common.parse_time(value)
        if stamp:
            times.append(stamp.timestamp())
    # A null progress clock identifies a pre-progress-clock legacy baton.
    add(b.get('progress_at') or b.get('updated_at'))
    cwd = (b.get('repo') if isinstance(b.get('repo'), dict) else {}).get('worktree')
    if cwd:
        add(observations.once(('git-activity', cwd), lambda: _git_activity(cwd)))
    key = common.writer_key(cwd) if cwd else None
    by_slug, by_writer = observations.activity(policy)
    add(by_slug.get(b.get('slug')))
    add(by_writer.get(key))
    ids = bound_ids(b)
    if ids:
        after = float((policy.get('lanes') or {}).get('stale_after_s', 86400))
        for mtime, engine, path in observations.once(('session-activity', after), lambda: sessions._candidates('all', common.now().timestamp() - after)):
            sid = os.path.basename(path)[:-6] if engine == 'claude' else None
            if engine == 'codex':
                match = sessions._ROLLOUT_ID.search(os.path.basename(path))
                sid = match[1] if match else None
            if sid in ids:
                add(mtime)
    for row in session_rows or []:
        path = row.get('_writer_cwd') or row.get('cwd')
        if row.get('id') in ids or key and path and common.writer_key(path) == key:
            if row.get('state') in ('working', 'active', 'mid-turn'):
                add(common.now().timestamp())
            else:
                add(row.get('modified'))
    if not times:
        try:
            add(os.path.getmtime(baton.baton_path(b['slug'])))
        except OSError:
            pass
    return max(times) if times else None


@observations.command
def effective_state(b, policy=None, session_rows=None):
    policy = policy or common.load_policy()
    state = b.get('state', 'active')
    if state in ('active', 'review') and any(r.get('id') in bound_ids(b) and r.get('state') == 'needs-you' for r in session_rows or []):
        return 'waiting-on-you'
    if state == 'parked':
        age = common.age_s(b.get('state_at') or b.get('progress_at') or b.get('updated_at'))
        return 'stale' if age is not None and age > b.get('park_timeout_s', duration(policy=policy)) else state
    if state not in ('active', 'review', 'waiting-external'):
        return state
    from . import baton
    cwd = (b.get('repo') if isinstance(b.get('repo'), dict) else {}).get('worktree')
    if cwd and baton.live_jobs_for(cwd):
        return state
    stamp = last_activity(b, policy, session_rows)
    stale_s = (policy.get('lanes') or {}).get('stale_after_s', 86400)
    return 'stale' if stamp is not None and common.now().timestamp() - stamp > stale_s else state


def preview_landed(event, main_root=None):
    from . import baton
    rows = []
    for path in glob.glob(os.path.join(common.BATONS, '*.json')):
        b = common.read_json(path)
        repo = b.get('repo') if isinstance(b, dict) else None
        if not isinstance(repo, dict) or b.get('state') == 'closed':
            continue
        if repo.get('branch') == event.get('branch') and baton.same_dir(repo.get('worktree'), event.get('worktree')):
            rows.append({'slug': b['slug'], 'state': 'landed',
                         'count': (b.get('landed') or {}).get('count', 0) + 1,
                         'verified': event.get('event') != 'landed-unverified'})
    return rows


def mark_landed(event, main_root=None):
    from . import baton
    rows = []
    try:
        candidates = preview_landed(event, main_root)
    except Exception as e:
        return [{'error': str(e)}]
    for row in candidates:
        try:
            with baton.baton_lock(row['slug'], timeout_s=10):
                b = baton.load_for_update(row['slug'])
                repo = (b or {}).get('repo') or {}
                if not b or b.get('state') == 'closed' or repo.get('branch') != event.get('branch') or not baton.same_dir(repo.get('worktree'), event.get('worktree')):
                    continue
                b['state'], b['state_at'] = 'landed', common.iso(common.now())
                row['count'] = (b.get('landed') or {}).get('count', 0) + 1
                b['landed'] = dict({k: event.get(k) for k in ('at', 'branch', 'target', 'from', 'to', 'event')},
                                   verified=row['verified'], count=row['count'])
                baton.save(b, 'checkpoint')
        except Exception as e:
            row['error'] = str(e)
        rows.append(row)
    return rows
