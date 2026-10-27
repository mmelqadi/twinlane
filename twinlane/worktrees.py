"""twinlane wt: doctor and prune linked worktrees, and the worktree helpers board and best-of share.

  twinlane wt doctor [--repo DIR] [--all] [--json | --md]
  twinlane wt prune  [--repo DIR] [--yes] [--include-empty] [--json]

doctor is read-only. It reports, per linked worktree: its state against the project's target branch
(merged, unlanded, empty, detached, missing), whether it is stale, uncommitted changes, what uses it
(a live job, a baton, a land queue entry, the current directory), configured dependency folders
(wt.deps_dirs) that are missing, empty or dangling links, and the Windows long-path risk.

prune is a dry run unless --yes. It removes a worktree only when ALL of these hold:
  - its branch is merged into the target AND it had commits of its own (or `twinlane land` recorded
    landing it); a branch with no commits of its own is "empty" and needs --include-empty;
  - it is clean: no staged, modified or untracked files (ignored files go with it, as with git);
  - nothing uses it: no live job runs at or under it, no baton names it, no land queue entry names it
    or its branch, and it is not (and does not contain) the current directory;
  - its last git activity is older than wt.prune_min_age_s;
  - it is not itself a link, its path does not resolve elsewhere, and no link under wt.link_roots
    points into it;
  - it holds no submodules, no tracked links and no reparse point of an unknown kind.
Removal: (1) every link inside it is unlinked WITHOUT being followed, after recording where it points
(Git for Windows `worktree remove` descends into a junction and deletes what it points at); a link that
will not go, or a target outside the worktree that vanishes, stops the prune; (2) the project's
wt.remove_command if configured, else (3) `git worktree remove <path>` (never --force); (4)
`git branch -d` (never -D: git refuses a branch it does not see as merged, and the branch is kept).
prune refuses to run inside a twinlane worker job.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import stat
import sys
import time

from twinlane import common, projects

BASE_BRANCHES = ('master', 'main')
GIT_TIMEOUT_S = 8
REMOVE_TIMEOUT_S = 600
LONG_PATH = 260                    # MAX_PATH: a longer absolute path fails in tools without long-path support
LIVE_JOB = ('queued', 'running', 'orphaned', 'unknown')
IO_REPARSE_TAG_MOUNT_POINT = 0xA0000003   # a junction
IO_REPARSE_TAG_SYMLINK = 0xA000000C
FILE_ATTRIBUTE_REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
FILE_ATTRIBUTE_DIRECTORY = getattr(stat, 'FILE_ATTRIBUTE_DIRECTORY', 0x10)
_MISSING = object()


class WorktreeError(RuntimeError):
    """A worktree operation that could not be done safely."""


# ---------------------------------------------------------------- shared helpers (board uses these)

def _read_head(path, limit: int = 4096) -> str:
    try:
        with open(path, 'rb') as h:
            return h.read(limit).decode('utf-8', errors='replace')
    except (OSError, TypeError, ValueError):
        return ''


def _parse_worktrees(text: str) -> list[dict]:
    """Records of `git worktree list --porcelain` (NUL-separated with -z, else one field per line)."""
    lines = text.split('\0') if '\0' in text else text.splitlines()
    out, cur = [], {}
    for line in lines + ['']:
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
        elif line.strip() == 'bare':
            cur['bare'] = True
        elif line.startswith('prunable'):
            cur['prunable'] = True
        elif line.startswith('locked'):
            cur['locked'] = line[len('locked'):].strip() or True
    return out


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
    """ahead/behind vs base, dirty count and merged flag for one worktree (two git calls). Board's quick
    view: here `merged` means only "0 ahead"; prune uses its own stricter test (see classify)."""
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


# ---------------------------------------------------------------- paths and links

def _canon(path: str) -> str:
    return common.canon_path(path)


def _canon_link(path: str) -> str:
    """Canonical path of a link ITSELF: its folder resolved, its own name kept (canon_path would follow it)."""
    head, name = os.path.split(os.path.normpath(os.path.abspath(path)))
    return os.path.join(_canon(head), os.path.normcase(name))


def _under(child: str, parent: str) -> bool:
    """Both canonical: `child` is `parent` or inside it."""
    if child == parent:
        return True
    return child.startswith(parent.rstrip('\\/') + os.sep)


def link_kind(st) -> str | None:
    """'symlink', 'junction' or 'reparse' (another kind of reparse point) for an lstat result, else None."""
    if stat.S_ISLNK(st.st_mode):
        return 'symlink'
    if getattr(st, 'st_file_attributes', 0) & FILE_ATTRIBUTE_REPARSE_POINT:
        tag = getattr(st, 'st_reparse_tag', 0)
        if tag == IO_REPARSE_TAG_MOUNT_POINT:
            return 'junction'
        if tag == IO_REPARSE_TAG_SYMLINK:
            return 'symlink'
        return 'reparse'
    return None


def _link_record(path: str, st) -> dict:
    kind = link_kind(st)
    if common.IS_WIN:
        is_dir = bool(getattr(st, 'st_file_attributes', 0) & FILE_ATTRIBUTE_DIRECTORY)
    else:
        is_dir = os.path.isdir(path)
    try:
        target = os.readlink(path)
    except (OSError, ValueError, AttributeError):
        target = None
    exists = os.path.exists(path)  # follows the link: does what it points at exist?
    resolved = os.path.realpath(path) if exists else None
    return {'path': path, 'kind': kind, 'is_dir': is_dir, 'target': target, 'target_exists': exists,
            'resolved': resolved}


def links_inside(root: str) -> list[dict]:
    """Every link (symlink, junction, other reparse point) at any depth under `root`, never followed.

    Each is {path, kind, is_dir, target (as stored), target_exists, resolved (real path, or None when
    it dangles)}. A directory that cannot be read raises OSError: an unread folder is not proof that it
    holds no link."""
    found = []
    stack = [root]
    while stack:
        here = stack.pop()
        with os.scandir(here) as it:
            entries = list(it)
        for entry in entries:
            st = os.lstat(entry.path)
            if link_kind(st):
                found.append(_link_record(entry.path, st))
            elif stat.S_ISDIR(st.st_mode):
                stack.append(entry.path)
    found.sort(key=lambda r: r['path'])
    return found


def _unlink(link: dict) -> None:
    """Remove the link itself; never what it points at."""
    if common.IS_WIN and link['is_dir']:
        os.rmdir(link['path'])  # a junction or a directory symlink: RemoveDirectory removes the link only
    else:
        os.unlink(link['path'])


def _relink(link: dict, worktree_root=None) -> bool:
    """Best effort: put back a link `_unlink` removed (used when a removal fails half-way)."""
    if os.path.lexists(link['path']):
        return True
    try:
        if worktree_root:
            if not os.path.isdir(worktree_root):
                return False
            os.makedirs(os.path.dirname(link['path']), exist_ok=True)
        if link['kind'] == 'junction' and common.IS_WIN:
            import _winapi
            if not link.get('resolved'):
                return False
            _winapi.CreateJunction(link['resolved'], link['path'])
        else:
            os.symlink(link['target'] or link['resolved'], link['path'], target_is_directory=link['is_dir'])
        return True
    except (OSError, TypeError, AttributeError, ImportError):
        return False


def _resolves_elsewhere(path: str) -> str | None:
    """Why a listed worktree path is (or passes through) a link, or None."""
    try:
        st = os.lstat(path)
    except OSError:
        return None
    kind = link_kind(st)
    if kind:
        return f'the worktree folder is itself a {kind}'
    listed = os.path.normcase(os.path.normpath(os.path.abspath(path)))
    real = common.real_path(path)
    if listed != os.path.normcase(real):
        return f'its path resolves elsewhere ({real}); a link is on the way'
    return None


# ---------------------------------------------------------------- git facts

def add(repo: str, branch: str, path: str, base: str | None = None) -> str:
    """Create a linked worktree at `path` on a NEW branch `branch` from `base` (default HEAD). Returns
    its real path. Refuses an existing path; never --force."""
    if os.path.lexists(path):
        raise WorktreeError(f'{path} already exists')
    from . import wip
    with wip.admission(path, 'worktree', branch, new_worktree=True):
        args = ['worktree', 'add', '-b', branch, '--', path] + ([base] if base else [])
        rc, msg = common.git(repo, *args, timeout=REMOVE_TIMEOUT_S)
        if rc != 0:
            raise WorktreeError(f'git worktree add failed: {msg}')
        return common.real_path(path)



def worktree_list(main_root: str) -> list[dict]:
    rc, text = common.git(main_root, 'worktree', 'list', '--porcelain', '-z', timeout=GIT_TIMEOUT_S * 4)
    if rc != 0:
        rc, text = common.git(main_root, 'worktree', 'list', '--porcelain', timeout=GIT_TIMEOUT_S * 4)
    if rc != 0:
        raise WorktreeError(f'git worktree list failed in {main_root}: {text}')
    return _parse_worktrees(text)


def target_branch(main_root: str, cfg: dict) -> str | None:
    """land.target, else the branch the main checkout is on (None when it is detached)."""
    target = (cfg.get('land') or {}).get('target')
    if target:
        return target
    rc, out = common.git(main_root, 'symbolic-ref', '-q', '--short', 'HEAD', timeout=GIT_TIMEOUT_S)
    return out.strip() if rc == 0 and out.strip() else None


def _tip(main_root: str, branch: str) -> str | None:
    rc, out = common.git(main_root, 'rev-parse', '--verify', '-q', f'refs/heads/{branch}^{{commit}}',
                         timeout=GIT_TIMEOUT_S)
    return out.strip() if rc == 0 and out.strip() else None


def _own_commit(subject: str) -> bool:
    """A branch reflog subject that records a commit made ON the branch (not a fast-forward or reset)."""
    s = subject.strip()
    if s.startswith(('commit', 'cherry-pick', 'revert', 'am:')):
        return True
    return s.startswith(('merge ', 'pull')) and 'Merge made by' in s


def own_commits(main_root: str, branch: str) -> bool | None:
    """Did the branch ever get a commit of its own? From its reflog: True/False, or None with no reflog."""
    rc, out = common.git(main_root, 'reflog', 'show', '--format=%H%x09%gs', f'refs/heads/{branch}',
                         timeout=GIT_TIMEOUT_S * 2)
    lines = [ln for ln in out.splitlines() if ln.strip()] if rc == 0 else []
    if not lines:
        return None
    return any(_own_commit(ln.split('\t', 1)[1] if '\t' in ln else '') for ln in lines)


def _is_ancestor(main_root: str, tip: str, target: str) -> bool | None:
    rc, _ = common.git(main_root, 'merge-base', '--is-ancestor', tip, f'refs/heads/{target}',
                       timeout=GIT_TIMEOUT_S * 2)
    return True if rc == 0 else False if rc == 1 else None


def landed(main_root: str, branch: str, tip: str) -> bool:
    """`twinlane land` recorded landing this branch at this tip (state/land/<key>/ledger.jsonl)."""
    path = os.path.join(common.STATE, 'land', projects.repo_key(main_root), 'ledger.jsonl')
    for line in common.tail_lines(path):
        try:
            rec = json.loads(line)
        except ValueError:
            continue
        if not isinstance(rec, dict) or rec.get('event') != 'landed' or rec.get('branch') != branch:
            continue
        commits = rec.get('commits') if isinstance(rec.get('commits'), list) else []
        if tip == rec.get('to') or tip in commits:
            return True
    return False


def _ignored(path: str) -> int | None:
    """Ignored paths in a worktree: `git worktree remove` deletes them with it (shown in the dry run)."""
    rc, out = common.git(path, 'status', '--porcelain=v1', '--ignored=traditional', '--untracked-files=normal',
                         timeout=GIT_TIMEOUT_S * 4)
    return len([x for x in out.splitlines() if x.startswith('!! ')]) if rc == 0 else None


def _dirty(path: str) -> int | None:
    rc, out = common.git(path, 'status', '--porcelain=v1', '--untracked-files=all', '--ignore-submodules=none',
                         timeout=GIT_TIMEOUT_S * 4)
    return len([x for x in out.splitlines() if x.strip()]) if rc == 0 else None


# ---------------------------------------------------------------- who uses a worktree

def usage_index(main_root: str, skip_baton_states=(), policy=None, *, fresh=False) -> dict:
    """Everything that may be using a worktree: live jobs (by cwd), batons, land queue entries and the
    current directory. Anything that cannot be read goes to `problems`, and prune then removes nothing:
    an unread record is not proof that nobody uses a worktree. `fresh` bypasses cached supervisor evidence."""
    from twinlane import baton as baton_mod
    from twinlane import jobs as jobs_mod
    from twinlane import sessions
    u = {'jobs': [], 'batons': [], 'queue': [], 'queue_branches': [], 'problems': [], 'cwd': None}
    try:
        interactive = sessions.collect_sessions(since_s=((policy or common.load_policy()).get('wip') or {}).get('session_active_s', 1800), limit=0, raw_paths=True, policy=policy, fresh=fresh)['rows']
        for row in interactive:
            state = row.get('state')
            if state not in ('job', 'parked', 'ended'):  # an idle session is alive between turns: it still uses its cwd
                cwd = row.get('_writer_cwd') or row.get('cwd')
                if isinstance(cwd, str) and cwd.strip():
                    status = state if state in ('active', 'mid-turn', 'working', 'idle', 'waiting-on-user', 'needs-you') else 'unknown interactive session state'
                    u['jobs'].append((_canon(cwd), 'session:' + str(row.get('id') or '?'), status))
                else:
                    u['problems'].append('interactive session has no readable cwd; ownership unknown')
    except Exception:
        u['problems'].append('interactive session usage unreadable')
    try:
        u['cwd'] = _canon(os.getcwd())
    except OSError:
        u['cwd'] = None
    if os.path.isdir(common.JOBS):
        from .observations import JOB_DIRECTORY_RE
        try:
            ids = sorted(jid for jid in os.listdir(common.JOBS) if jid != 'archive')
        except OSError as e:
            ids = []
            u['problems'].append(f'cannot list jobs ({e})')
        for jid in ids:
            if not os.path.isdir(os.path.join(common.JOBS, jid)):
                continue
            if not JOB_DIRECTORY_RE.fullmatch(jid):
                u['problems'].append(f'nonmatching job folder {jid}; ownership unknown')
            try:
                status = jobs_mod.job_status(jid)
            except Exception:  # noqa: BLE001 - an unreadable job counts as live
                status = 'unknown'
            if status not in LIVE_JOB:
                continue
            spec = common.read_json(os.path.join(common.JOBS, jid, 'job.json'), _MISSING)
            cwd = spec.get('cwd') if isinstance(spec, dict) else None
            if not isinstance(cwd, str) or not cwd.strip():
                u['problems'].append(f'live job {jid} ({status}) has no readable cwd')
                continue
            u['jobs'].append((_canon(cwd), jid, status))
    if os.path.isdir(common.BATONS):
        for name in sorted(os.listdir(common.BATONS)):
            if not name.endswith('.json'):
                continue
            slug = name[: -len('.json')]
            raw = common.read_json(os.path.join(common.BATONS, name), _MISSING)
            if not isinstance(raw, dict):
                u['problems'].append(f'baton {slug} is unreadable')
                continue
            if raw.get('state') in skip_baton_states:
                continue  # historical references do not keep a verified landed lane alive
            repo = raw.get('repo')
            if repo is None:
                continue
            if not isinstance(repo, dict):
                u['problems'].append(f'baton {slug} has an unreadable repo field')
                continue
            for key in ('worktree', 'root'):
                value = repo.get(key)
                if isinstance(value, str) and value.strip():
                    u['batons'].append((_canon(baton_mod.norm_dir(value) or value), slug))
                elif value is not None and value != '':
                    u['problems'].append(f'baton {slug} has an unreadable repo.{key}')
    queue = os.path.join(common.STATE, 'land', projects.repo_key(main_root), 'queue.json')
    if os.path.exists(queue):
        raw = common.read_json(queue, _MISSING)
        entries = raw.get('entries') if isinstance(raw, dict) else None
        if not isinstance(entries, list):
            u['problems'].append('the land queue is unreadable')
        else:
            for e in entries:
                if not isinstance(e, dict):
                    u['problems'].append('the land queue holds an unreadable entry')
                    continue
                slug = e.get('slug') if isinstance(e.get('slug'), str) else '?'
                if isinstance(e.get('worktree'), str) and e['worktree'].strip():
                    u['queue'].append((_canon(e['worktree']), slug))
                if isinstance(e.get('branch'), str) and e['branch'].strip():
                    u['queue_branches'].append((e['branch'], slug))
    return u


def in_use(path: str, branch: str | None, usage: dict) -> list[str]:
    c = _canon(path)
    reasons = []
    for cwd, jid, status in usage['jobs']:
        if _under(cwd, c):
            reasons.append(f'job {jid} ({status}) runs there')
    for wt, slug in usage['batons']:
        if _under(wt, c):
            reasons.append(f'baton {slug} names it')
    for wt, slug in usage['queue']:
        if _under(wt, c):
            reasons.append(f'land queue entry {slug} names it')
    for br, slug in usage['queue_branches']:
        if branch and br == branch:
            reasons.append(f'land queue entry {slug} names its branch')
    if usage['cwd'] and _under(usage['cwd'], c):
        reasons.append('it is the current directory')
    return list(dict.fromkeys(reasons))


# ---------------------------------------------------------------- doctor checks

def deps_report(path: str, deps_dirs) -> list[dict]:
    """Configured dependency folders that are missing, empty, not folders, or dangling/empty links."""
    out = []
    for rel in deps_dirs or []:
        full = os.path.join(path, *[p for p in re.split(r'[\\/]+', rel) if p])
        try:
            st = os.lstat(full)
        except OSError:
            out.append({'dir': rel, 'problem': 'missing'})
            continue
        kind = link_kind(st)
        if kind and not os.path.exists(full):
            out.append({'dir': rel, 'problem': f'dangling {kind}'})
            continue
        if not os.path.isdir(full):
            out.append({'dir': rel, 'problem': 'not a folder'})
            continue
        try:
            with os.scandir(full) as it:
                empty = next(it, None) is None
        except OSError as e:
            out.append({'dir': rel, 'problem': f'unreadable ({e.strerror or e})'})
            continue
        if empty:
            out.append({'dir': rel, 'problem': f'empty {kind} target' if kind else 'empty'})
    return out


def preflight(path, cfg):
    path = common.worktree_top(path) or path
    wt = cfg.get('wt') or {}
    problems = [f"{p['dir']}: {p['problem']}" for p in deps_report(path, wt.get('deps_dirs'))]
    main_root = projects.main_checkout(path) if wt.get('link_roots') else path
    roots = [os.path.realpath(_link_root(main_root, r)) for r in wt.get('link_roots') or []]
    roots.append(os.path.realpath(path))
    for rel in wt.get('deps_dirs') or []:
        full = os.path.join(path, rel)
        try:
            islink = link_kind(os.lstat(full))
        except OSError:
            continue
        if islink and os.path.exists(full):
            target = os.path.realpath(full)
            if not any(_under(_canon(target), _canon(root)) for root in roots):
                problems.append(f'{rel}: link resolves outside allowed roots')
    return problems


def _registry_long_paths() -> int | None:
    """HKLM ...\\FileSystem LongPathsEnabled (Windows only), or None when unknown."""
    if not common.IS_WIN:
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r'SYSTEM\CurrentControlSet\Control\FileSystem') as key:
            value, _ = winreg.QueryValueEx(key, 'LongPathsEnabled')
        return int(value)
    except (OSError, ValueError, TypeError, ImportError):
        return None


def _git_bool(main_root: str, *scope: str) -> bool | None:
    rc, out = common.git(main_root, 'config', *scope, '--type=bool', '--get', 'core.longpaths', timeout=GIT_TIMEOUT_S)
    if rc != 0:
        return None
    return out.strip() == 'true'


def longpath_report(main_root: str, paths: list[str]) -> dict:
    """core.longpaths (effective for the repo, and global), the registry switch, and per worktree the
    number of tracked files whose absolute path is LONG_PATH characters or more (from the main
    checkout's file list)."""
    rc, out = common.git(main_root, 'ls-files', '-z', timeout=GIT_TIMEOUT_S * 8)
    files = [f for f in out.split('\0') if f] if rc == 0 else None
    counts = {}
    for p in paths:
        if files is None:
            counts[p] = None
            continue
        prefix = len(os.path.abspath(p)) + 1
        counts[p] = sum(1 for f in files if prefix + len(f) >= LONG_PATH)
    repo, glob_ = _git_bool(main_root), _git_bool(main_root, '--global')
    registry = _registry_long_paths()
    worst = max((c for c in counts.values() if c), default=0)
    risk = bool(worst) and common.IS_WIN and not (repo and registry == 1)
    return {'core_longpaths': repo, 'core_longpaths_global': glob_, 'registry_long_paths': registry,
            'threshold': LONG_PATH, 'long_paths': counts, 'risk': risk}


# ---------------------------------------------------------------- classification

def _settings(cfg: dict) -> dict:
    wt = cfg.get('wt') or {}
    return {'stale_s': float(wt.get('stale_days') if wt.get('stale_days') is not None else 7) * 86400,
            'min_age_s': float(wt.get('prune_min_age_s') if wt.get('prune_min_age_s') is not None else 86400),
            'deps_dirs': wt.get('deps_dirs') or [], 'link_roots': wt.get('link_roots') or [],
            'remove_command': wt.get('remove_command'),
            'merged_min_age_s': float(wt.get('merged_min_age_s') if wt.get('merged_min_age_s') is not None else 3600),
            'behind_max': float(wt.get('behind_max') if wt.get('behind_max') is not None else 50)}


def classify(main_root: str, cfg: dict, w: dict, target: str | None, usage: dict, now: float | None = None) -> dict:
    """One worktree's row: state (main, missing, detached, target, merged, empty, unlanded, unknown),
    stale, age, dirty count, what uses it, locked. Read-only."""
    s = _settings(cfg)
    now = time.time() if now is None else now
    path = w['path']
    row = {'path': os.path.normpath(path), 'branch': w.get('branch') or '(detached)', 'head': w.get('head'),
           'main': _canon(path) == _canon(main_root), 'state': 'unknown', 'stale': False, 'age_s': None,
           'dirty': None, 'in_use': [], 'locked': w.get('locked'), 'tip': None, 'own_commits': None,
           'landed': False, 'notes': []}
    if w.get('bare'):
        row['state'] = 'bare'
        return row
    if not os.path.isdir(path) or w.get('prunable'):
        row['state'] = 'missing'
        return row
    activity = _activity(path) if not row['main'] else 0.0
    if activity:
        row['age_s'] = max(0.0, now - activity)
        row['stale'] = row['age_s'] > s['stale_s']
    row['dirty'] = _dirty(path)
    row['in_use'] = in_use(path, w.get('branch'), usage)
    if row['main']:
        row['state'] = 'main'
        return row
    branch = w.get('branch')
    if not branch or branch == '(detached)':
        row['state'] = 'detached'
        return row
    if target and branch == target:
        row['state'] = 'target'
        return row
    tip = _tip(main_root, branch)
    row['tip'] = tip
    if not tip:
        row['notes'].append('branch tip unreadable')
        return row
    if not target:
        row['notes'].append('no target branch (set land.target, or check out a branch in the main checkout)')
        return row
    anc = _is_ancestor(main_root, tip, target)
    if anc is None:
        row['notes'].append(f'cannot compare with {target}')
        return row
    if not anc:
        row['state'] = 'unlanded'
        return row
    row['own_commits'] = own_commits(main_root, branch)
    row['landed'] = landed(main_root, branch, tip)
    if row['own_commits'] is None and not row['landed']:
        row['notes'].append('no branch reflog: commits of its own cannot be proven')
    row['state'] = 'merged' if (row['own_commits'] or row['landed']) else 'empty'
    return row


def _link_scan(root: str) -> tuple[list[dict], str | None]:
    try:
        return links_inside(root), None
    except OSError as e:
        return [], f'cannot scan {root} for links ({e.strerror or e})'


def _link_root(main_root, root):
    full = os.path.expanduser(root)
    return full if os.path.isabs(full) else os.path.join(main_root, full)


def inbound_index(main_root: str, link_roots) -> dict:
    """Links found under wt.link_roots (relative roots resolve against the main checkout)."""
    links, problems = [], []
    for root in link_roots or []:
        full = _link_root(main_root, root)
        if not os.path.lexists(full):
            continue
        found, err = _link_scan(full)
        if err:
            problems.append(err)
        links.extend(found)
    return {'links': links, 'problems': problems}


def blockers(row: dict, include_empty: bool, settings: dict, usage: dict) -> list[str]:
    """Why `row` must not be pruned (shallow checks: no folder scan). Empty means eligible so far."""
    st = row['state']
    if st == 'main':
        return ['the main checkout']
    if st == 'missing':
        return ['missing on disk (git worktree prune clears its record)']
    if st == 'bare':
        return ['a bare repository entry']
    out = []
    if st == 'detached':
        out.append('detached HEAD: no branch to judge')
    elif st == 'target':
        out.append('it is on the target branch')
    elif st == 'unlanded':
        out.append('not merged into the target')
    elif st == 'empty' and not include_empty:
        out.append('no commits of its own (empty branch): needs --include-empty')
    elif st == 'unknown':
        out.append('state unknown: ' + ('; '.join(row['notes']) or 'git failed'))
    if row['locked']:
        out.append('locked' + (f' ({row["locked"]})' if isinstance(row['locked'], str) else ''))
    if row['dirty'] is None:
        out.append('git status failed')
    elif row['dirty']:
        out.append(f'{row["dirty"]} uncommitted change(s)')
    out.extend(row['in_use'])
    if usage['problems']:
        out.append('in-use check incomplete: ' + '; '.join(usage['problems']))
    if row['age_s'] is None:
        out.append('last activity unknown')
    elif row['age_s'] < settings['min_age_s']:
        out.append(f'active {common.fmt_age(row["age_s"])} ago (minimum {common.fmt_age(settings["min_age_s"])})')
    return out


def deep_blockers(row: dict, inbound: dict, others=()) -> tuple[list[str], list[dict]]:
    """Folder-level checks for a candidate: is it a link, other checkouts nested in it, links inside it,
    submodules, inbound links. `others` are the paths of every other checkout of the repository."""
    path = row['path']
    out = []
    why = _resolves_elsewhere(path)
    if why:
        return [why], []
    here = _canon(path)
    nested = [o for o in others if _canon(o) != here and _under(_canon(o), here)]
    if nested:
        return ['another checkout lives inside it: ' + ', '.join(nested[:3])], []
    if os.path.exists(os.path.join(path, '.gitmodules')):
        out.append('holds submodules (git refuses to remove those without --force)')
    links, err = _link_scan(path)
    if err:
        return out + [err], []
    c = _canon(path)
    for link in links:
        rel = os.path.relpath(link['path'], path)
        if link['kind'] == 'reparse':
            out.append(f'{rel} is a reparse point of an unknown kind')
        elif link['target_exists'] and not link['resolved']:
            out.append(f'cannot resolve where {rel} points')
        link['protect'] = bool(link['resolved']) and not _under(_canon(link['resolved']), c)
    if links:
        tracked = _tracked(path, [os.path.relpath(link['path'], path) for link in links])
        if tracked is None:
            out.append('cannot tell whether its links are tracked')
        elif tracked:
            out.append('holds tracked link(s): ' + ', '.join(tracked[:5]))
    out.extend(inbound['problems'])
    for link in inbound['links']:
        if link['resolved'] and _under(_canon(link['resolved']), c) and not _under(_canon_link(link['path']), c):
            out.append(f'inbound link {link["path"]} points into it')
    return out, links


def _tracked(path: str, rels: list[str]) -> list[str] | None:
    env = dict(common.git_env(), GIT_LITERAL_PATHSPECS='1')
    rc, out, _ = common.run(['git', '-C', path, 'ls-files', '-z', '--'] + [r.replace(os.sep, '/') for r in rels],
                            timeout=GIT_TIMEOUT_S * 4, env=env)
    if rc != 0:
        return None
    return [f for f in out.split('\0') if f]


# ---------------------------------------------------------------- doctor

def doctor(repo_dir: str | None = None, policy: dict | None = None, now: float | None = None) -> dict:
    main_root, cfg = projects.resolve(repo_dir, policy)
    s = _settings(cfg)
    target = target_branch(main_root, cfg)
    usage = usage_index(main_root)
    rows = []
    for w in worktree_list(main_root):
        row = classify(main_root, cfg, w, target, usage, now)
        row['deps'] = deps_report(row['path'], s['deps_dirs']) if row['state'] not in ('missing', 'bare') else []
        row['prune_blockers'] = blockers(row, False, s, usage)
        rows.append(row)
    lp = longpath_report(main_root, [r['path'] for r in rows if r['state'] not in ('missing', 'bare')])
    for r in rows:
        r['long_paths'] = lp['long_paths'].get(r['path'])
    counts = {}
    for r in rows:
        counts[r['state']] = counts.get(r['state'], 0) + 1
    counts['stale'] = sum(1 for r in rows if r['stale'] and not r['main'])
    return {'repo': main_root, 'target': target, 'configured': cfg['configured'], 'worktrees': rows,
            'counts': counts, 'long_paths': lp, 'deps_dirs': s['deps_dirs'], 'usage_problems': usage['problems']}


def _row_text(r: dict) -> str:
    bits = [r['state'] + (' stale' if r['stale'] and not r['main'] else ''), r['branch'], r['path']]
    if r['dirty']:
        bits.append(f'dirty {r["dirty"]}')
    if r['age_s'] is not None:
        bits.append(f'idle {common.fmt_age(r["age_s"])}')
    if r['in_use']:
        bits.append('in use: ' + '; '.join(r['in_use']))
    if r.get('deps'):
        bits.append('deps: ' + ', '.join(f'{d["dir"]} {d["problem"]}' for d in r['deps']))
    if r.get('long_paths'):
        bits.append(f'long paths: {r["long_paths"]}')
    if r['notes']:
        bits.append('; '.join(r['notes']))
    return '  ' + '  '.join(bits)


def _flag(v) -> str:
    return 'unset' if v is None else str(v).lower()


def render_doctor(d: dict) -> str:
    c = d['counts']
    order = ('merged', 'unlanded', 'empty', 'stale', 'missing', 'detached', 'unknown')
    lines = [f'twinlane wt doctor: {d["repo"]}  target {d["target"] or "(none)"}  '
             + ', '.join(f'{k} {c[k]}' for k in order if c.get(k))]
    lp = d['long_paths']
    lines.append(f'long paths: core.longpaths repo={_flag(lp["core_longpaths"])} global={_flag(lp["core_longpaths_global"])}'
                 f'  registry LongPathsEnabled={_flag(lp["registry_long_paths"])}'
                 f'  risk={"YES" if lp["risk"] else "no"}')
    if d['usage_problems']:
        lines.append('in-use check incomplete: ' + '; '.join(d['usage_problems']))
    for r in d['worktrees']:
        lines.append(_row_text(r))
    if not d['deps_dirs']:
        lines.append('deps: no wt.deps_dirs configured for this project')
    return '\n'.join(lines)


def render_doctor_md(d: dict) -> str:
    lines = [f'## wt doctor: `{d["repo"]}`', '', f'target: `{d["target"] or "(none)"}`', '',
             '| state | branch | path | dirty | idle | in use | deps | long paths |',
             '|---|---|---|---|---|---|---|---|']
    for r in d['worktrees']:
        cells = [r['state'] + (' (stale)' if r['stale'] and not r['main'] else ''), r['branch'], r['path'],
                 r['dirty'], common.fmt_age(r['age_s']) if r['age_s'] is not None else '',
                 '; '.join(r['in_use']), ', '.join(f'{x["dir"]} {x["problem"]}' for x in r.get('deps', [])),
                 r.get('long_paths') or '']
        lines.append('| ' + ' | '.join(str(x if x is not None else '').replace('|', '\\|') for x in cells) + ' |')
    lp = d['long_paths']
    lines += ['', f'core.longpaths repo={_flag(lp["core_longpaths"])} global={_flag(lp["core_longpaths_global"])}, '
              f'registry LongPathsEnabled={_flag(lp["registry_long_paths"])}, risk={"yes" if lp["risk"] else "no"}']
    return '\n'.join(lines)


# ---------------------------------------------------------------- prune

def _remove(main_root: str, settings: dict, row: dict, links: list[dict], target: str | None) -> dict:
    """Unlink inner links, remove the worktree, delete its branch with -d. Returns the outcome."""
    path, branch = row['path'], row['branch']
    res = {'unlinked': [], 'removed': False, 'branch_deleted': False, 'error': None, 'abort': False,
           'branch_note': None}
    protected = [lk for lk in links if lk.get('protect')]

    def vanished():
        return [lk['resolved'] for lk in protected if not os.path.exists(lk['resolved'])]

    def restore():
        back = [lk['path'] for lk in res['unlinked'] if _relink(lk, path)]
        return f'; links put back: {len(back)}/{len(res["unlinked"])}' if res['unlinked'] else ''

    for lk in links:
        try:
            _unlink(lk)
        except OSError as e:
            res['error'] = f'cannot unlink {lk["path"]} ({e.strerror or e})' + restore()
            return res
        if os.path.lexists(lk['path']):
            res['error'] = f'{lk["path"]} is still there after unlinking' + restore()
            return res
        res['unlinked'].append(lk)
        gone = vanished()
        if gone:
            res['error'] = 'a link target VANISHED while unlinking: ' + ', '.join(gone) + restore()
            res['abort'] = True
            return res
    command = settings['remove_command']
    if command:
        values = {'repo': main_root, 'worktree': path, 'branch': branch, 'slug': os.path.basename(path)}
        if target:
            values['target'] = target
        try:
            argv = projects.argv(command, **values)
        except projects.ProjectError as e:
            res['error'] = f'wt.remove_command: {e}' + restore()
            return res
        rc, out, err = common.run(argv, cwd=main_root, timeout=REMOVE_TIMEOUT_S)
        msg = (err or out).strip()
    else:
        rc, msg = common.git(main_root, 'worktree', 'remove', path, timeout=REMOVE_TIMEOUT_S)
    gone = vanished()
    if gone:
        res['error'] = 'a link target VANISHED during the removal: ' + ', '.join(gone)
        res['abort'] = True
        return res
    if rc != 0:
        res['error'] = f'{"wt.remove_command" if command else "git worktree remove"} failed (rc {rc}): ' \
                       f'{common.clip(msg, 300)}' + (restore() if os.path.isdir(path) else '')
        return res
    if os.path.lexists(path):
        res['error'] = 'the folder is still there after the removal' + restore()
        return res
    res['removed'] = True
    if _tip(main_root, branch):
        rc, msg = common.git(main_root, 'branch', '-d', '--', branch, timeout=GIT_TIMEOUT_S * 4)
        if rc == 0:
            res['branch_deleted'] = True
        else:
            res['branch_note'] = f'branch kept: {common.clip(msg, 200)}'
    else:
        res['branch_note'] = 'branch already gone'
    return res


def _ledger(main_root: str, record: dict) -> None:
    d = projects.state_dir('wt', main_root, create=True)
    with open(os.path.join(d, 'prune.jsonl'), 'a', encoding='utf-8', newline='\n') as h:
        h.write(json.dumps(record, ensure_ascii=False) + '\n')


def _evaluate(main_root, cfg, w, target, usage, inbound, include_empty, now=None, others=(), merged_only=False):
    s = _settings(cfg)
    row = classify(main_root, cfg, w, target, usage, now)
    if merged_only:
        s['min_age_s'] = s['merged_min_age_s']
        if row['state'] in ('merged', 'empty', 'unlanded') and target:
            rc, text = common.git(main_root, 'cherry', target, row['branch'], timeout=GIT_TIMEOUT_S)
            rm, merges = common.git(main_root, 'rev-list', '--merges', '--count', target + '..' + row['branch'], timeout=GIT_TIMEOUT_S)
            rb, behind = common.git(main_root, 'rev-list', '--count', row['branch'] + '..' + target, timeout=GIT_TIMEOUT_S)
            if rc or rm or rb or not merges.strip().isdigit() or not behind.strip().isdigit() or any(
                    line and line[0] not in '+-' for line in text.splitlines()):
                row['state'] = 'unknown'
                row['notes'].append('git cherry failed: unique commits or behind unknown')
            else:
                row['unique'] = 0 if row.get('landed') else sum(line.startswith('+') for line in text.splitlines()) + int(merges.strip())
                row['behind'] = int(behind.strip())
                row['verdict'] = 'merged' if row['unique'] == 0 else 'rebase or park' if row['behind'] > s['behind_max'] else 'keep'
                row['rebase_or_park'] = row['verdict'] == 'rebase or park'
                row['state'] = 'merged' if row['unique'] == 0 else 'unlanded'
    row['blockers'] = blockers(row, include_empty, s, usage)
    if row.get('unique'):
        row['blockers'].append(f"{row['unique']} unique commit(s), {row['behind']} behind {target}: {row['verdict']}")
    links = []
    if not row['blockers']:
        deep, links = deep_blockers(row, inbound, others)
        row['blockers'] = deep
    row['links'] = [lk['path'] for lk in links]
    row['ignored'] = _ignored(row['path']) if not row['blockers'] else None
    return row, links


def prune(repo_dir: str | None = None, yes: bool = False, include_empty: bool = False,
          policy: dict | None = None, now: float | None = None, merged_only: bool = False, only=None, archive=False) -> dict:
    """Dry run unless `yes`. Returns {repo, target, dry_run, rows, removed, failed, aborted}."""
    main_root, cfg = projects.resolve(repo_dir, policy)
    s = _settings(cfg)
    if archive:
        cfg = dict(cfg, wt=dict(cfg.get('wt') or {}, merged_min_age_s=0))
    target = target_branch(main_root, cfg)
    result = {'repo': main_root, 'target': target, 'dry_run': not yes, 'rows': [], 'removed': 0,
              'failed': 0, 'aborted': False, 'merged_only': merged_only}

    def run_all():
        usage = usage_index(main_root, ('landed', 'closed') if merged_only else (), policy)
        inbound = inbound_index(main_root, s['link_roots'])
        listed = worktree_list(main_root)
        others = [x['path'] for x in listed]
        for w in listed:
            if only and _canon(w['path']) not in {_canon(p) for p in only}:
                continue
            row, links = _evaluate(main_root, cfg, w, target, usage, inbound, include_empty, now, others, merged_only)
            if row['main']:
                continue
            result['rows'].append(row)
            if row['blockers']:
                row['action'] = 'rebase or park' if row.get('rebase_or_park') else 'skip'
                continue
            if not yes:
                row['action'] = 'would remove'
                continue
            if result['aborted']:
                row['action'] = 'skip'
                row['blockers'] = ['an earlier removal lost a link target; stopped']
                continue
            # Re-check everything immediately before acting: a job or an edit may have arrived meanwhile.
            relisted = worktree_list(main_root)
            fresh = next((x for x in relisted if _canon(x['path']) == _canon(w['path'])), None)
            if fresh is None:
                row['action'], row['blockers'] = 'skip', ['no longer listed by git']
                continue
            row2, links = _evaluate(main_root, cfg, fresh, target, usage_index(main_root, ('landed', 'closed') if merged_only else (), policy, fresh=True),
                                    inbound_index(main_root, s['link_roots']), include_empty, now,
                                    [x['path'] for x in relisted], merged_only)
            if row2['blockers']:
                row2['action'] = 'skip'
                result['rows'][-1] = row2
                continue
            row = row2
            result['rows'][-1] = row
            res = _remove(main_root, s, row, links, target)
            row['action'] = 'removed' if res['removed'] else 'failed'
            row['error'] = res['error']
            row['branch_deleted'] = res['branch_deleted']
            row['branch_note'] = res['branch_note']
            row['unlinked'] = [{'path': lk['path'], 'target': lk['resolved'] or lk['target']} for lk in res['unlinked']]
            if res['removed']:
                result['removed'] += 1
            else:
                result['failed'] += 1
            if res['abort']:
                result['aborted'] = True
            _ledger(main_root, {'event': row['action'], 'at': common.iso(common.now()), 'path': row['path'],
                                'branch': row['branch'], 'tip': row['tip'], 'target': target,
                                'branch_deleted': res['branch_deleted'], 'unlinked': row['unlinked'],
                                'error': res['error'], 'via': 'remove_command' if s['remove_command'] else 'git'})

    if yes:
        with common.file_lock(os.path.join(projects.state_dir('wt', main_root, create=True), '.lock'), 30):
            run_all()
    else:
        run_all()
    return result


def render_prune(r: dict, command='prune') -> str:
    lines = [f'twinlane wt {command}: {r["repo"]}  target {r["target"] or "(none)"}'
             + ('  (dry run)' if r['dry_run'] else '')]
    for row in r['rows']:
        head = f'  {row["action"]:<12}  {row["branch"]}  {row["path"]}'
        if r.get('merged_only'):
            head += f"  UNIQUE {row.get('unique', '?')} BEHIND {row.get('behind', '?')}"
        if row['action'] in ('skip', 'rebase or park'):
            head += ': ' + '; '.join(row['blockers'])
            if row.get('rebase_or_park'):
                head += f"; rebase or park ({row['behind']} commits behind, {row['unique']} unique)"
        elif row['action'] == 'would remove':
            head += f'  (state {row["state"]}, tip {(row["tip"] or "")[:12]}'
            if row['links']:
                head += f', {len(row["links"])} inner link(s) unlinked first'
            if row.get('ignored'):
                head += f', {row["ignored"]} ignored path(s) go with it'
            head += ')'
        elif row['action'] == 'removed':
            head += f'  tip {(row["tip"] or "")[:12]}' + ('  branch deleted' if row.get('branch_deleted') else
                                                         f'  {row.get("branch_note") or ""}')
        else:
            head += f': {row.get("error")}'
        lines.append(head)
    would = sum(1 for row in r['rows'] if row['action'] == 'would remove')
    if r['dry_run']:
        lines.append(f'dry run: {would} would be removed; nothing changed.'
                     + (f' Rerun with {"--apply" if command == "gc" else "--yes"} to remove them.' if would else ''))
    else:
        lines.append(f'removed {r["removed"]}, failed {r["failed"]}'
                     + ('; STOPPED: a link target vanished, check the unlinked list above' if r['aborted'] else ''))
    return '\n'.join(lines)


# ---------------------------------------------------------------- cli

def _public(obj):
    if isinstance(obj, dict):
        return {k: _public(v) for k, v in obj.items() if not k.startswith('_')}
    if isinstance(obj, list):
        return [_public(v) for v in obj]
    return obj


def _project_dirs(policy: dict, repo: str | None) -> list[str]:
    dirs = [repo or os.getcwd()]
    for key in (policy.get('projects') or {}):
        if isinstance(key, str) and os.path.isdir(key):
            dirs.append(key)
    seen, out = set(), []
    for d in dirs:
        try:
            main = projects.main_checkout(d)
        except projects.ProjectError:
            if d == dirs[0] and not repo:
                continue
            out.append(d)
            continue
        if _canon(main) not in seen:
            seen.add(_canon(main))
            out.append(main)
    return out


def cli(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane wt', description='Inspect (doctor) and prune linked worktrees.')
    sub = ap.add_subparsers(dest='cmd')
    d = sub.add_parser('doctor', help='report; --fix also releases stale writer claims')
    d.add_argument('--repo')
    d.add_argument('--all', action='store_true', help='also every project in the user config')
    d.add_argument('--fix', action='store_true',
                   help='release stale writer claims (finished jobs whose runner is gone); never touches a worktree')
    fmt = d.add_mutually_exclusive_group()
    fmt.add_argument('--json', action='store_true')
    fmt.add_argument('--md', action='store_true')
    p = sub.add_parser('prune', help='remove merged, clean, unused, old worktrees (dry run unless --yes)')
    p.add_argument('--repo')
    p.add_argument('--yes', '--apply', action='store_true')
    p.add_argument('--only', action='append')
    p.add_argument('--include-empty', action='store_true')
    p.add_argument('--merged-only', action='store_true', help='require zero unique patches via git cherry')
    p.add_argument('--json', action='store_true')
    p.add_argument('--group')
    p.add_argument('--discard-unmerged', action='store_true')
    g = sub.add_parser('gc', help='prune zero-unique clean unused worktrees (dry run unless --apply)')
    g.add_argument('--repo')
    g.add_argument('--apply', '--yes', action='store_true')
    g.add_argument('--only', action='append')
    g.add_argument('--json', action='store_true')
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    if args.cmd is None:
        ap.print_help()
        return common.EXIT_USAGE
    if args.cmd in ('prune', 'gc'):
        gc = args.cmd == 'gc'
        apply = args.apply if gc else args.yes
        refused = projects.refuse_in_worker('wt ' + args.cmd) if not gc or apply else None
        if refused is not None:
            return refused
        if not gc and (args.group or args.discard_unmerged):
            print('twinlane wt prune: --group/--discard-unmerged is not available: discarding an unmerged branch '
                  'needs git branch -D, which wt prune never runs', file=sys.stderr)
            return common.EXIT_USAGE
        try:
            res = prune(args.repo, yes=apply, include_empty=gc or args.include_empty,
                        merged_only=gc or args.merged_only, only=args.only)
        except (projects.ProjectError, WorktreeError) as e:
            print(f'twinlane wt prune: {e}', file=sys.stderr)
            return common.EXIT_USAGE
        except TimeoutError as e:
            print(f'twinlane wt prune: another prune holds the lock ({e})', file=sys.stderr)
            return common.EXIT_RUNNING
        if args.json:
            common.emit(_public(res), True)
        else:
            print(render_prune(res, command=args.cmd))
        return common.EXIT_FAILED if res['failed'] or res['aborted'] else common.EXIT_OK
    if args.fix:
        refused = projects.refuse_in_worker('wt doctor --fix')
        if refused is not None:
            return refused
    policy = common.load_policy()
    targets = _project_dirs(policy, args.repo) if args.all else [args.repo]
    reports, rc = [], common.EXIT_OK
    for repo in targets:
        try:
            reports.append(doctor(repo, policy))
        except (projects.ProjectError, WorktreeError) as e:
            print(f'twinlane wt doctor: {e}', file=sys.stderr)
            rc = common.EXIT_USAGE
    from . import jobs
    claims = jobs.release_stale_writers(dry_run=not args.fix)
    if args.json:
        out = reports if args.all else (reports[0] if reports else {})
        if claims and not args.all and isinstance(out, dict):
            out = dict(out, stale_writer_claims=claims)
        common.emit(_public(out), True)
    else:
        render = render_doctor_md if args.md else render_doctor
        if reports:
            print('\n\n'.join(render(r) for r in reports))
        if claims and args.fix:
            released = [c for c in claims if c['released']]
            print(f"released {len(released)} stale writer claim{'' if len(released) == 1 else 's'}"
                  + ''.join(f"\n  {c['id']}  {c['cwd']}  ({c['why']})" for c in released)
                  + ''.join(f"\n  NOT released {c['id']}: {c.get('error')}" for c in claims if not c['released']))
        elif claims:
            print(f"{len(claims)} stale writer claim{'' if len(claims) == 1 else 's'} (the job finished and its runner "
                  'is gone); `twinlane wt doctor --fix` releases them'
                  + ''.join(f"\n  {c['id']}  {c['cwd']}" for c in claims))
    return rc
