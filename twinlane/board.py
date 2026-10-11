"""twinlane board: one compact screen for a lead session (README 'twinlane board', DESIGN.md 'board.py').

Read-only by construction: it reads the cached meter file (never probes an engine, never calls a model),
the jobs/ and batons/ directories and `git worktree list`/`rev-list`/`status`. Nothing is written.
collect() returns plain data; render_text() and render_md() only format it. Every file it reads may hold
any JSON type in any field: a field of the wrong type is ignored or labelled, never trusted, and a record
that cannot be read is reported in ATTENTION instead of raising.
"""
from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import glob
import os
import re
import sys
import time

from . import common
from . import meter as meter_mod

ENGINES = ('claude', 'codex')
LIVE = ('queued', 'running', 'orphaned', 'unknown')
PROBLEM = ('failed', 'timeout', 'lost', 'limit_hit', 'interrupted', 'orphaned')
TERMINAL = ('done', 'failed', 'timeout', 'limit_hit', 'refused', 'stopped', 'lost', 'interrupted')
ATTENTION_WINDOW_S = 24 * 3600     # finished jobs older than this drop off ATTENTION (--all keeps them)
NEW_DIR_GRACE_S = 60               # a job dir this young may still be half-created: not "unreadable" yet
LONG_RUN_FRACTION = 0.8
OLD_JOB_S = ATTENTION_WINDOW_S + 4 * 3600  # a job created this long ago cannot have finished inside the window
FINISHED_DEFAULT = 5
CAPS = {'attention': 6, 'live': 6, 'batons': 5, 'worktrees': 6}
BASE_BRANCHES = ('master', 'main')
GIT_TIMEOUT_S = 8
GIT_WORKERS = 8
PROBE_LIMIT = 12                   # worktrees git is asked about by default (--all: every one)
VERDICT_RE = re.compile(r'^[\s*#>_`-]*(MERGE AFTER FIXES|DO NOT MERGE|MERGE)\b[^\n]*')
CREDIT_KEYS = ('free_resets', 'free_reset', 'reset_credits', 'reset_credit')  # another branch may add one
SHORT = {**meter_mod.SHORT_NAMES, 'weekly': '7d'}
# ATTENTION priority: lower is shown first and P0 is never hidden by the cap.
P_MUST, P_RISK, P_FAIL, P_INFO = 0, 1, 2, 3
LOST_TOGETHER_S = 60               # lost jobs that went quiet within this span share one cause (sleep, sign-out)


# ---------------------------------------------------------------- defensive helpers

def _d(x) -> dict:
    return x if isinstance(x, dict) else {}


def _l(x) -> list:
    return x if isinstance(x, list) else []


def _s(x) -> str | None:
    return x if isinstance(x, str) and x else None


def _num(x):
    if isinstance(x, bool) or not isinstance(x, (int, float)) or x != x or x in (float('inf'), float('-inf')):
        return None
    return float(x)


def _clip(text, n: int) -> str:
    text = '' if text is None else str(text)
    return text if len(text) <= n else text[: n - 3] + '...'


def _redact(text) -> str:
    """The one secret filter (baton.redact, shared with handoff)."""
    from . import baton
    return baton.redact(text)


def _txt(text, n: int) -> str:
    """One redacted, whitespace-collapsed, clipped line. Redact first: clipping first could cut a token
    in half and leave a prefix the patterns no longer match."""
    return _clip(' '.join(_redact(text).split()), n)


def _scrub(obj):
    """Redact every string in a data tree (keys are ours). Applied to the whole output, so no field
    (a name, a path, a branch, a note) needs to remember to filter itself."""
    if isinstance(obj, str):
        return _redact(obj)
    if isinstance(obj, dict):
        return {k: _scrub(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v) for v in obj]
    return obj


def _short_model(m) -> str:
    return re.sub(r'^(claude|gpt)-', '', str(m)) if m else '-'


def _pct(p) -> str:
    p = _num(p)
    if p is None:
        return '?'
    return f'{p:.0f}%' if abs(p - round(p)) < 0.05 else f'{p:.1f}%'


def _until(v, now) -> str:
    t = common.parse_time(v)
    if t is None:
        return '?'
    s = (t - now).total_seconds()
    if s <= 0:
        return 'now'
    s = int(s)
    if s < 3600:
        return f'{max(1, s // 60)}m'
    if s < 86400:
        return f'{s // 3600}h{(s % 3600) // 60:02d}m'
    return f'{s // 86400}d{(s % 86400) // 3600}h'


# ---------------------------------------------------------------- meters + route

def _credit_extra(raw) -> str | None:
    if raw is None or raw == '':
        return None
    if isinstance(raw, bool):
        return 'yes' if raw else 'no'
    if isinstance(raw, (int, float)):
        return f'{raw:g}'
    if isinstance(raw, dict):
        for k in ('count', 'available', 'remaining', 'has'):
            if k in raw:
                return _credit_extra(raw[k])
        return None
    if isinstance(raw, list):
        return str(len(raw))
    return _txt(raw, 20)


def _tri(x):
    return x if isinstance(x, bool) else None


def _engine_view(name: str, sec, policy: dict, now) -> dict:
    sec = _d(sec)
    th = _d(policy.get('thresholds'))
    max_age = _d(policy.get(name)).get('stale_reading_max_age_s', 1800)
    age = common.age_s(sec.get('reading_at'))
    status = _s(sec.get('status')) or 'unknown'
    note = _txt(sec.get('note'), 120)
    stale = age is not None and _num(max_age) is not None and age > max_age
    if stale and status != 'unknown':
        status = 'unknown'
        note = '; '.join(x for x in (note, f'reading is {common.fmt_age(age)} old: stale') if x)
    windows = []
    for w in _l(sec.get('windows')):
        if not isinstance(w, dict):
            continue
        wname = meter_mod.safe_window_name(w.get('name'))
        reset = w.get('resets_at')
        windows.append({'name': wname, 'label': SHORT.get(wname, wname) if wname else meter_mod.INVALID_WINDOW,
                        'limit': meter_mod.safe_window_name(w.get('limit')), 'pct': _num(w.get('pct')),
                        'resets_at': reset if isinstance(reset, (str, int, float)) else None,
                        'resets_in': _until(reset, now)})
    bi = meter_mod.binding_index(windows)
    for i, w in enumerate(windows):  # judged on the cleaned values, the ones route.decide also receives
        w['recognised'], w['binding'] = meter_mod.is_recognised(w), i == bi
    credits = _d(sec.get('credits'))
    nxt = sec.get('next_reset')
    view = {'status': status, 'source': _s(sec.get('source')) or 'none', 'age_s': None if age is None else int(age),
            'max_pct': _num(sec.get('max_pct')), 'next_reset': nxt if isinstance(nxt, (str, int, float)) else None,
            'windows': windows, 'binding': next((w for w in windows if w['binding']), None), 'note': note,
            'stop_pct': _num(th.get('stop_pct')) or 85}
    if name == 'claude':
        view['extra_usage_enabled'] = _tri(sec.get('extra_usage_enabled'))
        view['hold'] = _txt(sec.get('hold'), 60) or None
    else:
        view['credits'] = {'has': _tri(credits.get('has')), 'unlimited': _tri(credits.get('unlimited'))} \
            if credits else None
        view['on_credits'] = sec.get('on_credits') is True
        view['limit_reached_type'] = _txt(sec.get('limit_reached_type'), 40) or None
        view['plan'] = _txt(sec.get('plan'), 40) or None
        view['free_resets'] = next((_credit_extra(sec[k]) for k in CREDIT_KEYS if sec.get(k) not in (None, '')), None)
    return view


def _blockers(d: dict) -> list[dict]:
    """The engines a refusal names, each with its status, binding window (name as shown) and hold."""
    return [{'engine': _s(b.get('engine')), 'status': _s(b.get('status')), 'pct': _num(b.get('pct')),
             'window': _txt(b.get('window_label'), 60) or None, 'hold': _txt(b.get('hold'), 60) or None}
            for b in _l(d.get('blockers')) if isinstance(b, dict)] if d.get('refused') else []


def _routes(meter, policy: dict) -> dict:
    """What each kind would take right now. Uses route.decide on the cached meter: no model call."""
    from . import route
    if meter is None:
        return {}
    out = {}
    for kind in _d(policy.get('routing')):
        try:
            d = route.decide(kind, meter, policy)
        except (ValueError, TypeError, AttributeError):
            continue
        out[kind] = {'engine': d.get('engine'), 'refused': bool(d.get('refused')),
                     'fallback_used': bool(d.get('fallback_used')), 'reason': _txt(d.get('reason'), 100),
                     'model': d.get('model') or d.get('tier'), 'blockers': _blockers(d)}
    return out


def collect_meters(policy: dict, now) -> dict:
    raw = common.read_json(os.path.join(common.STATE, 'meter.json'))
    if not isinstance(raw, dict) or not any(isinstance(raw.get(e), dict) for e in ENGINES):
        return {'cached': False, 'read_at': None, 'age_s': None, 'claude': None, 'codex': None, 'routes': {}}
    views = {e: _engine_view(e, raw.get(e), policy, now) for e in ENGINES}
    # route.decide gets a clean meter: only the fields it reads, each checked, the stale-adjusted status.
    clean = {e: {'status': views[e]['status'], 'max_pct': views[e]['max_pct'], 'next_reset': views[e]['next_reset'],
                 'extra_usage_enabled': views[e].get('extra_usage_enabled'), 'hold': views[e].get('hold'),
                 'windows': [{k: w[k] for k in ('name', 'pct', 'limit')} for w in views[e]['windows']]}
             for e in ENGINES}
    age = common.age_s(raw.get('read_at'))
    return {'cached': True, 'read_at': _s(raw.get('read_at')), 'age_s': None if age is None else int(age),
            **views, 'routes': _routes(clean, policy)}


# ---------------------------------------------------------------- jobs

def _read_head(path, limit: int = 4096) -> str:
    try:
        with open(path, 'rb') as h:
            return h.read(limit).decode('utf-8', errors='replace')
    except (OSError, TypeError, ValueError):
        return ''


def verdict_parts(final_text: str) -> tuple[str | None, str | None]:
    """(first line, keyword) when the first non-blank line starts with MERGE / MERGE AFTER FIXES / DO NOT MERGE."""
    for line in final_text.splitlines()[:30]:
        if not line.strip():
            continue
        m = VERDICT_RE.match(line)
        return (_txt(m.group(0).strip(' *#>_`-'), 90), m.group(1)) if m else (None, None)
    return None, None


def verdict_of(final_text: str) -> str | None:
    return verdict_parts(final_text)[0]


def _id_age_s(jid: str) -> float | None:
    """Age from the `YYYYMMDD-HHMMSS` prefix of a job id (UTC), or None when the name is not a job id."""
    try:
        t = dt.datetime.strptime(jid[:15], '%Y%m%d-%H%M%S').replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None
    return max(0.0, (common.now() - t).total_seconds())


def _file(path: str):
    """(exists, parsed). Parsed is None for a missing or invalid file; `exists` tells the two apart."""
    if not os.path.isfile(path):
        return False, None
    return True, common.read_json(path)


def _last_activity(jid: str) -> float | None:
    """Newest mtime (epoch s) among the job folder's files and those one folder down (codex/, ...), or None.
    Stats only, never reads a log. The `collected` marker is skipped: `twinlane result` rewrites it later."""
    from . import jobs
    newest = None

    def visit(path: str, depth: int) -> None:
        nonlocal newest
        try:
            with os.scandir(path) as it:
                for e in it:
                    if e.is_dir(follow_symlinks=False):
                        if depth == 0:
                            visit(e.path, 1)
                    elif e.name != jobs.COLLECTED or depth:
                        t = e.stat(follow_symlinks=False).st_mtime
                        newest = t if newest is None or t > newest else newest
        except OSError:
            return

    visit(jobs.job_dir(jid), 0)
    return newest


def _blank_row(jid: str) -> dict:
    return {'id': jid, 'name': None, 'kind': None, 'engine': None, 'requested': None, 'served': None,
            'status': 'unreadable', 'cwd': None, 'worktree': None, 'age_s': None, 'age': '?', 'timeout_s': None,
            'timeout_pct': None, 'ended_age_s': None, 'read_only_violation': False, 'limit_hit': False,
            'last_activity_at': None, 'baton': None, 'verdict': None, 'verdict_kind': None, 'collected': False,
            'problems': []}


def _job_row(jid: str, now) -> dict | None:
    from . import jobs
    row = _blank_row(jid)
    has_spec, spec = _file(jobs._p(jid, 'job.json'))
    has_st, st = _file(jobs._p(jid, 'status.json'))
    has_res, res = _file(jobs._p(jid, 'result.json'))
    problems = row['problems']
    if not has_spec:
        young = _id_age_s(jid)
        if young is not None and young < NEW_DIR_GRACE_S:
            return None  # `twinlane run` is still creating this job dir
        problems.append('job.json missing')
    elif not isinstance(spec, dict):
        problems.append('job.json invalid')
    if has_st and not isinstance(st, dict):
        problems.append('status.json invalid')
    if has_res and not isinstance(res, dict):
        problems.append('result.json invalid')
    if has_spec and isinstance(spec, dict) and not has_st and not has_res:
        age = _id_age_s(jid)
        if age is None or age >= NEW_DIR_GRACE_S:  # a started job always has status.json; before that it is new
            problems.append('status.json missing')
    spec, st, receipt = _d(spec), _d(st), _d(res)
    prim = _d(spec.get('primary'))
    req = _d(receipt.get('requested')) or prim
    served = [x for x in _l(receipt.get('served_models')) if isinstance(x, str)]
    started = receipt.get('started_at') or st.get('started_at') or spec.get('created_at')
    elapsed = common.age_s(started)
    cwd = _s(spec.get('cwd'))
    row.update({'name': _txt(spec.get('name') or receipt.get('name'), 40) or None,
                'kind': _s(spec.get('kind')) or _s(receipt.get('kind')),
                'engine': _s(receipt.get('engine')) or _s(st.get('engine')) or _s(prim.get('engine')),
                'requested': _s(req.get('model')) or _s(req.get('tier')) or _s(prim.get('model'))
                or _s(prim.get('tier')),
                'served': served[0] if served else None, 'cwd': cwd,
                'worktree': os.path.basename(cwd.rstrip('\\/')) if cwd else None,
                'age_s': elapsed, 'age': common.fmt_age(elapsed), 'timeout_s': _num(prim.get('timeout_s')),
                'ended_age_s': common.age_s(receipt.get('ended_at')) if receipt.get('ended_at') else None,
                'read_only_violation': receipt.get('read_only_violation') is True,
                'baton': _s(spec.get('baton')),
                'collected': os.path.isfile(jobs._p(jid, jobs.COLLECTED))})
    if problems:
        return row
    try:
        status = jobs.job_status(jid)
    except Exception:  # noqa: BLE001 - a field of a type the runner never writes
        status = None
    if not isinstance(status, str) or status not in LIVE + TERMINAL:
        problems.append('status unreadable')
        return row
    row['status'] = status
    row['limit_hit'] = status == 'limit_hit' or receipt.get('limit_hit') is True
    if status == 'lost':  # when the runner went quiet: jobs lost together share one cause
        last = _last_activity(jid)
        row['last_activity_at'] = common.iso(dt.datetime.fromtimestamp(last, dt.timezone.utc)) if last else None
    if status in LIVE and row['timeout_s'] and elapsed is not None:
        row['timeout_pct'] = round(100 * elapsed / row['timeout_s'])
    if status in TERMINAL and receipt:
        fp = receipt.get('final_path')
        row['verdict'], row['verdict_kind'] = verdict_parts(
            _read_head(fp if isinstance(fp, str) and os.path.isfile(fp) else jobs._p(jid, 'final.md')))
    return row


def collect_jobs(now, show_all: bool, finished_n: int) -> dict:
    """Every job dir is classified, so ATTENTION covers all recent jobs; only the DISPLAYED finished rows are
    capped (finished_n, newest first). The one shortcut: a job whose id says it was created more than
    OLD_JOB_S ago AND whose receipt says it ended outside the attention window, once enough finished rows are
    collected, cannot matter to the screen and is not read in full (creation age alone proves nothing: a long
    job created yesterday can fail an hour ago). Liveness and unreadable jobs are still found at any age;
    --all reads all. A job dir that cannot be read is `unreadable`."""
    from . import jobs
    live, finished, unreadable, scanned = [], [], [], 0
    for jid in jobs._job_ids():
        if not show_all and len(finished) >= finished_n and not os.path.exists(jobs._p(jid, jobs.SURVIVOR)):
            age = _id_age_s(jid)  # a receipt whose engine outlived its kill is live at any age: always read
            if age is not None and age > OLD_JOB_S:
                receipt = common.read_json(jobs._p(jid, 'result.json'))
                ended = common.age_s(receipt.get('ended_at')) if isinstance(receipt, dict) else None
                if ended is not None and ended > ATTENTION_WINDOW_S:
                    continue
        try:
            row = _job_row(jid, now)
        except Exception:  # noqa: BLE001 - one damaged job dir must not blank the board
            row = dict(_blank_row(jid), problems=['job files unreadable'])
        if row is None:
            continue
        scanned += 1
        if row['status'] == 'unreadable':
            unreadable.append(row)
        elif row['status'] in LIVE:
            live.append(row)
        else:
            finished.append(row)
    return {'live': live, 'finished': finished[: (len(finished) if show_all else finished_n)],
            'unreadable': unreadable, 'scanned': scanned, '_all_finished': finished}


# ---------------------------------------------------------------- batons

def _mtime_ns(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _baton_row(slug: str, raw) -> dict:
    from . import baton
    row = {'slug': slug, 'owner': None, 'age': '?', 'age_s': None, 'updated_at': None, 'next_action': None,
           'in_flight': [], 'approvals_pending': [], 'root': None, 'worktree': None, 'problems': []}
    bad = row['problems']
    if not isinstance(raw, dict):
        bad.append('not a JSON object')
        return row

    def sub(parent: dict, key: str, label: str) -> dict:
        v = parent.get(key)
        if v is None or isinstance(v, dict):
            return v or {}
        bad.append(label)
        return {}

    owner, prog = sub(raw, 'owner', 'owner'), sub(raw, 'progress', 'progress')
    holds, repo = sub(raw, 'holds', 'holds'), sub(raw, 'repo', 'repo')
    eng = owner.get('engine')
    if eng in ENGINES:
        row['owner'] = eng
    elif eng is not None:
        bad.append('owner.engine')
    upd = raw.get('updated_at')
    if isinstance(upd, str):
        row['updated_at'], row['age_s'] = upd, common.age_s(upd)
        row['age'] = common.fmt_age(row['age_s'])
    elif upd is not None:
        bad.append('updated_at')
    na = prog.get('next_action')
    if isinstance(na, str):
        row['next_action'] = _txt(na, 100) or None
    elif na is not None:
        bad.append('progress.next_action')
    flights = prog.get('in_flight')
    if isinstance(flights, list):
        row['in_flight'] = [_txt(f['job'], 40) for f in flights if isinstance(f, dict) and isinstance(f.get('job'), str)]
    elif flights is not None:
        bad.append('progress.in_flight')
    jobs = raw.get('jobs')
    if isinstance(jobs, list):  # a size-trimmed baton keeps only a few pointers: jobs[] is the full live list
        # Each unfinished job once: drop a pointer whose job jobs[] records as finished, and repeated pointers.
        finished = {j['id'] for j in jobs if baton._valid_job_entry(j) and not baton._is_live_job(j)}
        live = [j['id'] for j in jobs if baton._is_live_job(j)]
        row['in_flight'] = list(dict.fromkeys([f for f in row['in_flight'] if f not in finished] + live))
    elif jobs is not None:
        bad.append('jobs')
    else:
        row['in_flight'] = list(dict.fromkeys(row['in_flight']))
    pend = holds.get('approvals_pending')
    if isinstance(pend, list):
        for h in pend:
            action = h.get('action') if isinstance(h, dict) else h
            if isinstance(action, str) and action.strip():
                row['approvals_pending'].append(_txt(action, 100))
            elif action is not None and action != '':
                bad.append('holds.approvals_pending')
    elif pend is not None:
        bad.append('holds.approvals_pending')
    for key in ('root', 'worktree'):
        v = repo.get(key)
        if isinstance(v, str) and v:
            row[key] = v
        elif v is not None:
            bad.append(f'repo.{key}')
    row['problems'] = list(dict.fromkeys(bad))
    return row


def collect_batons() -> list[dict]:
    """Read every baton file directly (not through baton.list_batons) so one damaged file becomes a row with
    `problems` instead of an exception."""
    from . import baton
    rows = []
    for path in glob.glob(os.path.join(common.BATONS, '*.json')):
        slug = os.path.basename(path)[: -len('.json')]
        if not baton.valid_slug(slug):
            continue
        try:
            row = _baton_row(slug, common.read_json(path))
        except Exception:  # noqa: BLE001
            row = dict(_baton_row(slug, None), problems=['unreadable'])
        row['_mtime'] = _mtime_ns(path)
        rows.append(row)
    rows.sort(key=lambda r: (r['updated_at'] or '', r['_mtime'], r['slug']), reverse=True)
    for r in rows:
        r.pop('_mtime')
    return rows


# ---------------------------------------------------------------- worktrees

def _parse_worktrees(text: str) -> list[dict]:
    out, cur = [], {}
    for line in text.splitlines() + ['']:
        if not line.strip():
            if cur.get('path'):
                out.append(cur)
            cur = {}
        elif line.startswith('worktree '):
            cur['path'] = line[len('worktree '):].strip()
        elif line.startswith('branch '):
            cur['branch'] = re.sub(r'^refs/heads/', '', line[len('branch '):].strip())
        elif line.startswith('HEAD '):
            cur['head_full'] = line[5:].strip()
            cur['head'] = cur['head_full'][:7]
        elif line.strip() == 'detached':
            cur['branch'] = '(detached)'
        elif line.startswith('prunable'):
            cur['prunable'] = True
    return out


def _norm(path) -> str:
    return common.canon_path(path)


def _gitdir(path: str) -> str | None:
    """A linked worktree's private git dir, from the `gitdir:` line of its .git file (no git call)."""
    head = _read_head(os.path.join(path, '.git'), 1024)
    if head.startswith('gitdir:'):
        d = head[len('gitdir:'):].strip().splitlines()[0].strip() if head[len('gitdir:'):].strip() else ''
        return d if os.path.isabs(d) else os.path.join(path, d)
    return None


def _activity(path: str) -> float:
    """Last-activity stamp from file times alone (reflog and index of the worktree): git over hundreds of
    branches takes seconds, a stat does not."""
    gd = _gitdir(path)
    stamps = [0.0]
    for rel in (('logs', 'HEAD'), ('index',), ('HEAD',)):
        try:
            stamps.append(os.stat(os.path.join(gd, *rel)).st_mtime)
        except (OSError, TypeError):
            pass
    return max(stamps)


def _base_branch(root: str) -> str | None:
    for b in BASE_BRANCHES:
        rc, _ = common.git(root, 'rev-parse', '--verify', '-q', f'refs/heads/{b}', timeout=GIT_TIMEOUT_S)
        if rc == 0:
            return b
    return None


def _probe(w: dict, base: str | None) -> None:
    """ahead/behind vs base, dirty count and merged flag for one worktree (two git calls)."""
    w['checked'] = True
    if base:
        rc, counts = common.git(w['path'], 'rev-list', '--left-right', '--count', f'{base}...HEAD',
                                timeout=GIT_TIMEOUT_S)
        parts = counts.split()
        if rc == 0 and len(parts) == 2 and all(p.isdigit() for p in parts):
            w['behind'], w['ahead'] = int(parts[0]), int(parts[1])
            w['merged'] = w['ahead'] == 0 and w['branch'] != base
    rc, porcelain = common.git(w['path'], 'status', '--porcelain=v1', '--untracked-files=normal',
                               timeout=GIT_TIMEOUT_S)
    if rc == 0:
        w['dirty'] = len([x for x in porcelain.splitlines() if x.strip()])


def rank_worktree(w: dict):
    """Display order: checked-and-unmerged first, then merged; a worktree a baton names comes first in its
    group, then the most recently active."""
    return (not w['checked'], w['merged'], not w['mine'], -w['activity'], w['path'])


def collect_worktrees(batons: list[dict], show_all: bool = False) -> dict:
    """Linked worktrees of every repo a baton points at (the main checkout is not listed). git runs only for
    the PROBE_LIMIT most recently active worktrees (every one with --all): in a repo with hundreds of
    branches even one batched ref listing takes seconds."""
    from . import baton as baton_mod
    mine = {_norm(b['worktree']) for b in batons if isinstance(b.get('worktree'), str)}
    seen, starts = set(), []
    for b in batons:
        for key in ('root', 'worktree'):
            p = b.get(key)
            if isinstance(p, str) and os.path.isdir(p):
                if _norm(p) not in seen:
                    seen.add(_norm(p))
                    starts.append(p)
                break
    repos = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=GIT_WORKERS) as pool:
        listed = list(pool.map(lambda p: common.git(p, 'worktree', 'list', '--porcelain', timeout=GIT_TIMEOUT_S),
                               starts))
        todo, seen_roots = [], set()
        for rc, text in listed:
            wts = _parse_worktrees(text) if rc == 0 else []
            if wts and _norm(wts[0]['path']) not in seen_roots:
                seen_roots.add(_norm(wts[0]['path']))
                todo.append(wts)
        bases = list(pool.map(lambda wts: _base_branch(wts[0]['path']), todo))
        for wts, base in zip(todo, bases):
            rows = [{'path': w['path'], 'branch': w.get('branch') or '-', 'head': w.get('head'), 'ahead': None,
                     'behind': None, 'dirty': None, 'merged': False, 'checked': False,
                     'missing': not os.path.isdir(w['path']) or bool(w.get('prunable')),
                     'mine': _norm(w['path']) in mine} for w in wts[1:]]
            for r in rows:
                r['activity'] = 0.0 if r['missing'] else _activity(r['path'])
            repos.append({'root': baton_mod.norm_dir(wts[0]['path']) or wts[0]['path'], 'base': base,
                          'name': os.path.basename(wts[0]['path'].rstrip('\\/')), 'worktrees': rows})
        order = sorted((w for r in repos for w in r['worktrees'] if not w['missing']),
                       key=lambda w: (not w['mine'], -w['activity']))
        picked = order if show_all else order[:PROBE_LIMIT]
        base_of = {id(w): r['base'] for r in repos for w in r['worktrees']}
        list(pool.map(lambda w: _probe(w, base_of[id(w)]), picked))
    for r in repos:
        r['worktrees'].sort(key=rank_worktree)
    return {'repos': repos, 'probe_limit': None if show_all else PROBE_LIMIT}


# ---------------------------------------------------------------- attention

def _item(priority: int, kind: str, ident: str, text: str, **extra) -> dict:
    return {'priority': priority, 'kind': kind, 'id': ident, 'text': text, **extra}


def _lost_groups(rows: list[dict]) -> list[list[dict]]:
    """Lost rows whose last activity falls within LOST_TOGETHER_S of the group's first, in time order. Only
    groups of two or more matter; a row with no known last activity is never grouped."""
    lost = [r for r in rows if r['status'] == 'lost' and r.get('last_activity_at') and not r['read_only_violation']]
    stamped = sorted(((common.parse_time(r['last_activity_at']), r) for r in lost), key=lambda x: (x[0], x[1]['id']))
    groups, start = [], None
    for t, r in stamped:
        if start is None or (t - start).total_seconds() > LOST_TOGETHER_S:
            groups.append([])
            start = t
        groups[-1].append(r)
    return [g for g in groups if len(g) > 1]


def build_attention(meters: dict, jobs_data: dict, batons: list[dict], show_all: bool) -> list[dict]:
    """Ordered by what the lead must not miss: pending approvals and engines at their stop line (P0), then
    live jobs at risk, unreadable records and engines near the line (P1), then failures (P2), then finished
    work not yet read (P3). Within a priority the order is the order found."""
    items = []
    done = jobs_data['_all_finished']

    def recent(r):
        return r.get('ended_age_s') if r.get('ended_age_s') is not None else r.get('age_s')

    for b in batons:
        for a in b['approvals_pending']:
            items.append(_item(P_MUST, 'approval', b['slug'], f"approval pending [{b['slug']}]: {a}"))
    for name in ENGINES:
        v = meters.get(name)
        if not isinstance(v, dict):
            continue
        s = v['status']
        if s != 'ok':
            bw = v.get('binding')
            bw = bw if bw and bw['pct'] == v['max_pct'] else None  # never blame a window the max did not come from
            tail = (f" ({_pct(v['max_pct'])}" + (f" in {_window_name(bw)}" if bw else '')
                    + f", stop at {v['stop_pct']:g}%)") if v['max_pct'] is not None else ''
            prio = P_MUST if s in ('stop', 'out') else P_INFO if s == 'caution' else P_RISK
            items.append(_item(prio, 'engine', name, f"{name} {s.upper()}{tail}"
                               + (f": {v['note']}" if s == 'unknown' and v['note'] else '')))
        if name == 'claude' and v.get('extra_usage_enabled'):
            items.append(_item(P_MUST, 'engine', name, 'claude extra usage is ON (policy forbids it)'))
        if name == 'codex' and v.get('on_credits'):
            items.append(_item(P_MUST, 'engine', name, 'codex would run on paid credits'))
    if not meters.get('cached'):
        items.append(_item(P_RISK, 'engine', 'meter', 'no cached meter: run twinlane meter'))
    for r in jobs_data['live']:
        if r['status'] == 'orphaned':
            items.append(_item(P_RISK, 'problem', r['id'], f"orphaned: {r['id']} {r['engine'] or ''}".rstrip()
                               + f" (engine still running: twinlane stop {r['id']})"))
        elif r['status'] == 'unknown':  # exists, identity unreadable from here: not lost, and twinlane stop will not kill it
            items.append(_item(P_RISK, 'problem', r['id'], f"unknown: {r['id']} {r['engine'] or ''}".rstrip()
                               + ' (process exists, identity unreadable here: not lost, do not continue or redo it; '
                               'check from the host)'))
        if r['timeout_pct'] is not None and r['timeout_pct'] >= LONG_RUN_FRACTION * 100:
            items.append(_item(P_RISK, 'long', r['id'], f"{r['id']} at {r['timeout_pct']}% of its timeout "
                               f"({r['age']} of {common.fmt_age(r['timeout_s'])})"))
    for r in jobs_data['unreadable']:
        age = _id_age_s(r['id'])
        if show_all or age is None or age <= ATTENTION_WINDOW_S:
            items.append(_item(P_RISK, 'unreadable', r['id'],
                               f"unreadable job {r['id']}: {', '.join(r['problems']) or 'unknown'}"))
    for b in batons:
        if b['problems']:
            items.append(_item(P_RISK, 'malformed', b['slug'],
                               f"malformed baton [{b['slug']}]: {', '.join(b['problems'])}"))
    shown = [r for r in done if show_all or recent(r) is not None and recent(r) <= ATTENTION_WINDOW_S]
    together = {r['id']: g for g in _lost_groups(shown) for r in g}
    for r in shown:
        age = recent(r)
        if r['id'] in together:
            group = together[r['id']]
            if r is group[0]:  # one line for the group, where its first job would have been
                at = common.parse_time(group[-1]['last_activity_at']).strftime('%H:%MZ')
                ids = ', '.join(x['id'] for x in group)
                items.append(_item(P_FAIL, 'problem', r['id'], f"{len(group)} jobs lost together at {at} ({ids}) - "
                                   'a sign-out, shutdown or sleep can end every runner; rerun each with '
                                   'twinlane run --continue-job ID', ids=[x['id'] for x in group]))
            continue
        if r['status'] in PROBLEM or r['limit_hit']:
            what = 'limit_hit' if r['limit_hit'] else r['status']
            items.append(_item(P_FAIL, 'problem', r['id'], f"{what}: {r['id']} {r['engine'] or ''} "
                               f"{r['name'] or ''}".rstrip() + f" ({common.fmt_age(age)} ago)"))
        elif r['read_only_violation']:
            items.append(_item(P_FAIL, 'problem', r['id'], f"read-only violation: {r['id']} "
                               f"(twinlane result {r['id']})"))
    unread = [r for r in done if r['status'] == 'done' and not r['collected'] and r.get('ended_age_s') is not None
              and (show_all or r['ended_age_s'] <= ATTENTION_WINDOW_S)]
    if unread:  # `twinlane result` writes jobs/<id>/collected; jobs finished before that existed show up once
        verdicts = {}
        for r in unread:
            if r.get('verdict_kind'):
                verdicts[r['verdict_kind']] = verdicts.get(r['verdict_kind'], 0) + 1
        newest = min(unread, key=lambda r: r['ended_age_s'])
        items.append(_item(P_INFO, 'unread', newest['id'],
                           f"{len(unread)} finished, not yet read" + (': ' + ', '.join(f'{v} x{n}' for v, n in
                                                                                    verdicts.items()) if verdicts else '')
                           + f"; newest: twinlane result {newest['id']}", ids=[r['id'] for r in unread]))
    return sorted(items, key=lambda i: i['priority'])


# ---------------------------------------------------------------- collect

def collect(show_all: bool = False, finished_n: int = FINISHED_DEFAULT, now=None, policy: dict | None = None) -> dict:
    now = now or common.now()
    policy = policy or common.load_policy()
    meters = collect_meters(policy, now)
    jobs_data = collect_jobs(now, show_all, finished_n)
    batons = collect_batons()
    attention = build_attention(meters, jobs_data, batons, show_all)
    worktrees = collect_worktrees(batons, show_all)
    jobs_data.pop('_all_finished')
    for b in batons:
        b.pop('root', None)
    return _scrub({'generated_at': common.iso(now), 'all': show_all, 'meters': meters, 'attention': attention,
                   'jobs': jobs_data, 'batons': batons, 'worktrees': worktrees})


# ---------------------------------------------------------------- rendering

def _cap(items: list, key: str, show_all: bool, floor: int = 0) -> tuple[list, int]:
    limit = max(CAPS[key], floor)
    if show_all or len(items) <= limit:
        return items, 0
    return items[:limit], len(items) - limit


def _cap_attention(items: list, show_all: bool) -> tuple[list, int]:
    """P0 items (approvals, engines at their stop line) are never hidden by the cap."""
    return _cap(items, 'attention', show_all, floor=sum(1 for i in items if i['priority'] == P_MUST))


def _window_name(w: dict) -> str:
    """A window's name as shown: an unfamiliar one keeps its own name and says so."""
    return w['label'] if w['recognised'] or not w['name'] else f"{w['label']} {meter_mod.UNRECOGNISED_WINDOW}"


def _window_text(w: dict) -> str:
    return (f"{_window_name(w)} {_pct(w['pct'])} (resets in {w['resets_in']})"
            + (' [binding]' if w['binding'] else ''))


def _engine_line(name: str, v, now_none: str = 'no reading') -> str:
    if not isinstance(v, dict):
        return f'{name}: {now_none}'
    parts = [_window_text(w) for w in v['windows']] or ['no windows']
    if name == 'claude' and v.get('extra_usage_enabled') is not None:
        parts.append('extra usage ' + ('ON' if v['extra_usage_enabled'] else 'off'))
    if name == 'codex':
        c = v.get('credits')
        if c:
            parts.append('credits ' + ('unlimited' if c.get('unlimited') else 'yes' if c.get('has') else 'no'))
        if v.get('free_resets') is not None:
            parts.append(f"free resets {v['free_resets']}")
        if v.get('on_credits'):
            parts.append('ON CREDITS')
    tag = f"[{v['source']}, {common.fmt_age(v['age_s'])} old]" if v['source'] != 'none' else '[none]'
    return f"{name}: {v['status']} " + ' | '.join(parts) + f' {tag}'


def _route_groups(routes: dict) -> list[tuple[str, list[str]]]:
    groups: dict[str, list[str]] = {}
    for kind, r in routes.items():
        if r['refused']:
            groups.setdefault('REFUSED', []).append(kind)
        else:
            groups.setdefault(str(r['engine']), []).append(kind + ('*' if r['fallback_used'] else ''))
    return list(groups.items())


def _blocker_text(routes: dict) -> str:
    """Who stopped the refused kinds, once per engine: 'claude stop at 7d-sonnet 90%, codex out at weekly 100%'."""
    seen: dict = {}
    for r in routes.values():
        for b in r['blockers']:
            seen.setdefault(b['engine'], b)
    return ', '.join(f"{b['engine']} {b['status']}"
                     + (f" at {b['window']} {_pct(b['pct'])}" if b['window'] and b['pct'] is not None else '')
                     + (f" [{b['hold']}]" if b['hold'] else '') for b in seen.values())


def _route_text(m: dict) -> str:
    if not m.get('cached'):
        return 'route now: n/a (no cached meter)'
    groups = _route_groups(m['routes'])
    if not groups:
        return 'route now: n/a'
    text = 'route now: ' + '; '.join(f"{('->' + e) if e != 'REFUSED' else e} {' '.join(ks)}" for e, ks in groups)
    blocked = _blocker_text(m['routes'])
    if blocked:
        text += f'  [refused by {blocked}]'
    return text + ('  (* = fallback)' if any(r['fallback_used'] for r in m['routes'].values()) else '')


def _model_text(r: dict) -> str:
    return f"{_short_model(r['requested'])}/{_short_model(r['served']) if r['served'] else '?'}"


def _live_text(r: dict) -> str:
    pct = f" {r['timeout_pct']}%t" if r['timeout_pct'] is not None else ''
    return (f"{r['id']}  {r['engine'] or '-':6} {r['kind'] or '-':9} {r['status']:8} {r['age']:>4}{pct}  "
            f"{_model_text(r)}  {r['name'] or ''}  wt={r['worktree'] or '-'}").rstrip()


def _unread(r: dict) -> bool:
    return r['status'] == 'done' and not r['collected']


def _done_text(r: dict) -> str:
    tail = f"  [{r['verdict']}]" if r.get('verdict') else ''
    return (f"{r['id']}  {r['engine'] or '-':6} {r['status']:9} {common.fmt_age(r.get('ended_age_s')):>4} ago  "
            f"{_model_text(r)}  {r['name'] or ''}{tail}" + ('  (unread)' if _unread(r) else '')).rstrip()


def _baton_text(b: dict) -> str:
    bits = [f"{b['slug']}  owner={b['owner'] or '-'}  {b['age']}"]
    if b['in_flight']:
        bits.append(f"in-flight {len(b['in_flight'])}")
    if b['approvals_pending']:
        bits.append(f"approvals {len(b['approvals_pending'])}")
    if b['problems']:
        bits.append('[malformed: ' + ', '.join(b['problems']) + ']')
    bits.append('next: ' + _clip(b['next_action'] or '-', 70))
    return '  '.join(bits)


def _wt_flat(data: dict) -> list[dict]:
    out = [dict(w, repo=repo['name'], base=repo['base']) for repo in data['worktrees']['repos']
           for w in repo['worktrees']]
    return sorted(out, key=rank_worktree)


def _wt_title(wts: list[dict]) -> str:
    checked = sum(1 for w in wts if w['checked'])
    return (f"{len(wts)} linked, {checked} checked" + ('' if checked == len(wts) else ' (most recently active first)')
            + f", {sum(1 for w in wts if w['merged'])} merged")


def _wt_text(w: dict) -> str:
    if w['missing']:
        return f"{w['repo']}  {w['branch']}  MISSING on disk  {w['path']}"
    ab = '?' if w['ahead'] is None else f"+{w['ahead']}/-{w['behind']} vs {w['base']}"
    dirty = 'not checked' if w['dirty'] is None else str(w['dirty'])
    return f"{w['repo']}  {w['branch']}  {ab}  dirty {dirty}" + ('  MERGED' if w['merged'] else '')


def _more(n: int, hint: str = '--all') -> str:
    return f'  ... +{n} more ({hint})'


def _jobs_title(j: dict) -> str:
    return f"running/queued {len(j['live'])}" + (f", unreadable {len(j['unreadable'])}" if j['unreadable'] else '')


def render_text(data: dict) -> str:
    data = _scrub(data)
    show_all = data['all']
    m = data['meters']
    L = [f"twinlane board  {data['generated_at']}  (read-only; cached meters" +
         (f", {common.fmt_age(m['age_s'])} old)" if m.get('age_s') is not None else ')')]
    L.append('== METERS')
    L.append(_engine_line('claude', m.get('claude')))
    L.append(_engine_line('codex', m.get('codex')))
    for e in ENGINES:
        v = m.get(e)
        if isinstance(v, dict) and v['note'] and v['status'] not in ('ok', 'caution'):
            L.append(f"  note {e}: {v['note']}")
    L.append(_route_text(m))
    att, more = _cap_attention(data['attention'], show_all)
    L.append(f"== ATTENTION ({len(data['attention'])})")
    L += [f"! {a['text']}" for a in att] or ['  nothing needs you']
    if more:
        L.append(_more(more))
    live, more = _cap(data['jobs']['live'], 'live', show_all)
    L.append(f"== JOBS  {_jobs_title(data['jobs'])}")
    L += ['  ' + _live_text(r) for r in live] or ['  none running']
    if more:
        L.append(_more(more))
    L.append(f"  last finished ({len(data['jobs']['finished'])}):")
    L += ['  ' + _done_text(r) for r in data['jobs']['finished']] or ['  none']
    bs, more = _cap(data['batons'], 'batons', show_all)
    L.append(f"== BATONS ({len(data['batons'])})")
    L += ['  ' + _baton_text(b) for b in bs] or ['  none']
    if more:
        L.append(_more(more))
    wts = _wt_flat(data)
    shown, more = _cap(wts, 'worktrees', show_all)
    L.append(f"== WORKTREES ({_wt_title(wts)})")
    L += ['  ' + _wt_text(w) for w in shown] or ['  none (batons point at no linked worktree)']
    if more:
        L.append(_more(more))
    return '\n'.join(L)


def _cell(x) -> str:
    """One table cell. Backslashes are doubled first so that a backslash already in the text cannot
    combine with the escape we add before a pipe (`a\\|b` would otherwise end the cell at the pipe)."""
    text = '-' if x in (None, '') else str(x)
    text = text.replace('\\', '\\\\')
    text = text.replace('|', '\\|')
    return text.replace('\r', ' ').replace('\n', ' ')


def _code(x) -> str:
    return '`' + ('-' if x in (None, '') else str(x)).replace('`', "'").replace('\n', ' ') + '`'


def _table(head: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return []
    return (['| ' + ' | '.join(head) + ' |', '|' + '---|' * len(head)]
            + ['| ' + ' | '.join(_cell(c) for c in r) + ' |' for r in rows])


def render_md(data: dict) -> str:
    data = _scrub(data)
    show_all = data['all']
    m = data['meters']
    L = [f"### twinlane board - {data['generated_at']}", '*read-only; cached meters*' +
         (f" ({common.fmt_age(m['age_s'])} old)" if m.get('age_s') is not None else ''), '', '**Meters**']
    L.append('- ' + _engine_line('claude', m.get('claude')))
    L.append('- ' + _engine_line('codex', m.get('codex')))
    L.append('- ' + _route_text(m))
    att, more = _cap_attention(data['attention'], show_all)
    L += ['', f"**Attention ({len(data['attention'])})**"]
    L += [f"- ! {a['text']}" for a in att] or ['- nothing needs you']
    if more:
        L.append(f'- ... +{more} more (`--all`)')
    live, more = _cap(data['jobs']['live'], 'live', show_all)
    L += ['', f"**Jobs** - {_jobs_title(data['jobs'])}"]
    L += _table(['job', 'engine', 'kind', 'status', 'age', 'model req/served', 'name', 'worktree'],
                [[_code(r['id']), r['engine'], r['kind'], r['status'],
                  r['age'] + (f" ({r['timeout_pct']}% of timeout)" if r['timeout_pct'] is not None else ''),
                  _model_text(r), r['name'], r['worktree']] for r in live]) or ['- none running']
    if more:
        L.append(f'- ... +{more} more (`--all`)')
    L += ['', f"Last finished ({len(data['jobs']['finished'])})"]
    L += _table(['job', 'engine', 'status', 'ended', 'model req/served', 'name', 'verdict'],
                [[_code(r['id']), r['engine'], r['status'] + (' (unread)' if _unread(r) else ''),
                  common.fmt_age(r.get('ended_age_s')) + ' ago', _model_text(r), r['name'], r['verdict']]
                 for r in data['jobs']['finished']]) or ['- none']
    bs, more = _cap(data['batons'], 'batons', show_all)
    L += ['', f"**Batons ({len(data['batons'])})**"]
    L += _table(['baton', 'owner', 'age', 'in-flight', 'approvals', 'next action'],
                [[_code(b['slug']), b['owner'], b['age'], len(b['in_flight']), len(b['approvals_pending']),
                  (f"[malformed: {', '.join(b['problems'])}] " if b['problems'] else '') + (b['next_action'] or '-')]
                 for b in bs]) or ['- none']
    if more:
        L.append(f'- ... +{more} more (`--all`)')
    wts = _wt_flat(data)
    shown, more = _cap(wts, 'worktrees', show_all)
    L += ['', f"**Worktrees ({_wt_title(wts)})**"]
    L += _table(['repo', 'branch', 'ahead/behind', 'dirty', 'state'],
                [[w['repo'], _code(w['branch']),
                  'missing' if w['missing'] else '?' if w['ahead'] is None else f"+{w['ahead']}/-{w['behind']} {w['base']}",
                  w['dirty'], 'MERGED' if w['merged'] else 'missing' if w['missing'] else ''] for w in shown]
                ) or ['- none (batons point at no linked worktree)']
    if more:
        L.append(f'- ... +{more} more (`--all`)')
    return '\n'.join(L)


# ---------------------------------------------------------------- CLI

def frames(render, interval: float, out=None, sleep=time.sleep, max_frames: int | None = None) -> None:
    """--watch: redraw every interval seconds. Clears the screen only on a terminal."""
    out = out or sys.stdout
    n = 0
    while max_frames is None or n < max_frames:
        text = render()
        if getattr(out, 'isatty', lambda: False)():
            out.write('\x1b[H\x1b[2J')
        elif n:
            out.write('\n')
        out.write(text + '\n')
        out.flush()
        n += 1
        if max_frames is not None and n >= max_frames:
            break
        sleep(interval)


def cli(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane board', description='One compact, read-only screen: meters, attention, '
                                 'jobs, batons, worktrees.')
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument('--json', action='store_true')
    fmt.add_argument('--md', action='store_true', help='Markdown for a chat UI')
    ap.add_argument('--all', action='store_true', help='lift the line caps and time windows')
    ap.add_argument('--finished', type=int, default=FINISHED_DEFAULT, metavar='N', help='finished jobs to list')
    ap.add_argument('--watch', type=float, metavar='SECONDS', help='redraw every SECONDS (Ctrl-C stops)')
    try:
        a = ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_OK if e.code == 0 else common.EXIT_USAGE
    if a.watch is not None and a.watch <= 0:
        print('twinlane board: --watch needs a positive number of seconds', file=sys.stderr)
        return common.EXIT_USAGE
    if a.finished < 0:
        print('twinlane board: --finished must be >= 0', file=sys.stderr)
        return common.EXIT_USAGE

    def render() -> str:
        data = collect(show_all=a.all, finished_n=a.finished)
        if a.json:
            import json
            return json.dumps(data, ensure_ascii=False, indent=2)
        return render_md(data) if a.md else render_text(data)

    if a.watch is None:
        print(render())
        return common.EXIT_OK
    try:
        frames(render, a.watch)
    except KeyboardInterrupt:
        pass
    return common.EXIT_OK
