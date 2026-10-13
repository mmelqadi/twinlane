"""twinlane land: queue finished worktree branches and land them on the target branch, one at a time.

  twinlane land enqueue SLUG [--worktree DIR] [--repo DIR] [--session ID] [--json]
  twinlane land status [--repo DIR] [--json]
  twinlane land next [--repo DIR] [--json]
  twinlane land run [SLUG] [--rebase] [--dry-run] [--break --reason TEXT] [--repo DIR] [--json]
  twinlane land release [--break --reason TEXT] [--repo DIR] [--json]
  twinlane land dequeue SLUG [--repo DIR] [--json]
  twinlane land log [SLUG] [--repo DIR] [--json]

One project (a repository, identified by its main checkout) has one FIFO queue and one integrator
lock, under STATE/land/<repo_key>/:
  queue.json    {version, repo, entries: [{slug, worktree, branch, enqueued_at, by}]}
  holder.json   the integrator lock: {slug, pid, created, session, host, token, acquired_at,
                heartbeat_at, beats, phase, child}
  ledger.jsonl  append-only: landed | refused | failed | broken-lock events, each with its exact range
  logs/         one log per gate or project land command run
  .lock         an OS file lock serialising every read-modify-write of the files above

The holder is a lease kept by the running `land run` process. A holder whose process is alive is
never taken, even when its heartbeat is stale. A holder whose process is provably dead is broken only
by an explicit `--break --reason`. A holder whose liveness cannot be judged (another host, an
unreadable process identity, a gate child that outlived its parent) is never broken.

Landing is fast-forward only. A branch whose target moved is refused unless rebasing is allowed
(--rebase, or the project's land.command_rebases), and a rebase is refused when the range holds merge
commits, so merges are never flattened. The project's gate runs in the slug's worktree on the tree that
will land; a failing gate lands nothing, and a gate that moves HEAD or changes the tree is refused.
Exit codes: 0 ok, 2 usage, 3 refused, 4 lock held, 5 gate or land failed.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import uuid

from twinlane import common, projects

QUEUE_VERSION = 1
LOCK_TIMEOUT_S = 30
GIT_TIMEOUT_S = 60
REBASE_TIMEOUT_S = 600
SLUG_RE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._-]{0,79}$')
BREAKABLE = ('dead', 'invalid')
_MISSING = object()


class LandError(Exception):
    """A refusal or failure with its exit code; `details` go to --json output and the ledger."""

    def __init__(self, message: str, code: int = common.EXIT_REFUSED, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# ---------------------------------------------------------------- identity

def host_name() -> str:
    try:
        return socket.gethostname() or 'unknown-host'
    except OSError:
        return 'unknown-host'


def _session(explicit: str | None = None) -> str | None:
    return explicit or os.environ.get('TWINLANE_SESSION') or None


def _me(session: str | None = None) -> dict:
    pid = os.getpid()
    return {'pid': pid, 'created': common.process_created(pid), 'session': _session(session), 'host': host_name()}


# ---------------------------------------------------------------- files

class Paths:
    def __init__(self, main_root: str):
        self.main_root = main_root
        self.dir = projects.state_dir('land', main_root)
        self.lock = os.path.join(self.dir, '.lock')
        self.queue = os.path.join(self.dir, 'queue.json')
        self.holder = os.path.join(self.dir, 'holder.json')
        self.ledger = os.path.join(self.dir, 'ledger.jsonl')
        self.logs = os.path.join(self.dir, 'logs')

    def locked(self):
        return common.file_lock(self.lock, LOCK_TIMEOUT_S)


def _load(path: str):
    """Parsed JSON, None when the file does not exist, LandError when it exists but cannot be read.

    Never "missing" for an unreadable file: a queue read as empty and written back would lose entries."""
    if not os.path.exists(path):
        return None
    data = common.read_json(path, _MISSING)
    if data is _MISSING:
        if not os.path.exists(path):
            return None
        raise LandError(f'cannot read {path}; inspect or repair it by hand', common.EXIT_FAILED)
    return data


def _remove(path: str) -> None:
    """Delete a file, retrying a Windows sharing violation (a reader holding it open). Raises if it stays."""
    for attempt in range(40):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if attempt == 39:
                raise
            time.sleep(0.05)


def read_queue(paths: Paths) -> dict:
    data = _load(paths.queue)
    if data is None:
        return {'version': QUEUE_VERSION, 'repo': paths.main_root, 'entries': []}
    entries = data.get('entries') if isinstance(data, dict) else None
    if not isinstance(data, dict) or data.get('version') != QUEUE_VERSION or not isinstance(entries, list) \
            or not all(isinstance(e, dict) and isinstance(e.get('slug'), str) and isinstance(e.get('worktree'), str)
                       and isinstance(e.get('branch'), str) for e in entries):
        raise LandError(f'{paths.queue} is not a version-{QUEUE_VERSION} land queue; inspect or repair it by hand',
                        common.EXIT_FAILED)
    return data


def read_holder(paths: Paths) -> dict | None:
    """The holder record, None when the lock is free, or {'invalid': reason} for an unusable file."""
    if not os.path.exists(paths.holder):
        return None
    data = common.read_json(paths.holder, _MISSING)
    if data is _MISSING:
        if not os.path.exists(paths.holder):
            return None
        return {'invalid': 'holder.json cannot be parsed'}
    if not isinstance(data, dict):
        return {'invalid': 'holder.json is not an object'}
    pid = data.get('pid')
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        return dict(data, invalid='holder.json has no valid pid')
    return data


def read_ledger(paths_or_root) -> list:
    """Every parseable ledger event, oldest first. Ship reads the landed ranges from here."""
    paths = paths_or_root if isinstance(paths_or_root, Paths) else Paths(paths_or_root)
    events = []
    try:
        with open(paths.ledger, encoding='utf-8') as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    events.append(event)
    except FileNotFoundError:
        pass
    return events


def landings(main_root: str, slug: str | None = None) -> list:
    """The `landed` events (exact from/to/commits per slug), oldest first."""
    return [e for e in read_ledger(main_root) if e.get('event') == 'landed' and (slug is None or e.get('slug') == slug)]


def _append_ledger(paths: Paths, event: dict) -> None:
    """Append one event. Callers hold the project lock."""
    os.makedirs(paths.dir, exist_ok=True)
    line = json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n'
    with open(paths.ledger, 'a', encoding='utf-8', newline='\n') as handle:
        handle.write(line)
        handle.flush()
        os.fsync(handle.fileno())


# ---------------------------------------------------------------- holder judgement

def judge(holder: dict | None, stale_s: float) -> dict:
    """{'state': free|alive|unresponsive|dead|unknown|invalid, 'why': text, 'heartbeat_age_s': n}.

    Only dead and invalid may be broken, and only with an explicit reason."""
    if holder is None:
        return {'state': 'free', 'why': 'no holder'}
    if holder.get('invalid'):
        return {'state': 'invalid', 'why': holder['invalid']}
    age = common.age_s(holder.get('heartbeat_at'))
    out = {'heartbeat_age_s': None if age is None else round(age, 1)}
    host = holder.get('host')
    if not isinstance(host, str) or host.lower() != host_name().lower():
        return dict(out, state='unknown', why=f'held from another host ({host}); its process cannot be checked here')
    state = common.pid_state(holder.get('pid'), holder.get('created'))
    child = holder.get('child') if isinstance(holder.get('child'), dict) else None
    child_state = common.pid_state(child.get('pid'), child.get('created')) if child else common.DEAD
    if state == common.DEAD:
        if child_state != common.DEAD:
            return dict(out, state='unknown', why=f"the holder exited but its {child.get('what') or 'child'} "
                                                  f"process {child.get('pid')} may still run")
        return dict(out, state='dead', why=f"process {holder.get('pid')} is gone")
    if state == common.UNKNOWN:
        return dict(out, state='unknown', why=f"process {holder.get('pid')} exists but its identity cannot be read")
    if age is None or age > stale_s:
        return dict(out, state='unresponsive',
                    why=f"process {holder.get('pid')} is alive but its heartbeat is {common.fmt_age(age)} old")
    return dict(out, state='alive', why=f"process {holder.get('pid')} is alive")


def describe_holder(holder: dict, verdict: dict) -> str:
    if verdict['state'] == 'invalid':
        return f"lock file is unusable ({verdict['why']})"
    return (f"held for {holder.get('slug')} by pid {holder.get('pid')} on {holder.get('host')}"
            f"{' session ' + str(holder.get('session')) if holder.get('session') else ''}"
            f" since {holder.get('acquired_at')}, phase {holder.get('phase')}, heartbeat "
            f"{common.fmt_age(verdict.get('heartbeat_age_s'))} ago: {verdict['state']} ({verdict['why']})")


def _holder_brief(holder: dict) -> dict:
    return {k: holder.get(k) for k in ('slug', 'pid', 'created', 'session', 'host', 'acquired_at', 'heartbeat_at',
                                       'phase', 'invalid') if k in holder}


def _break_locked(paths: Paths, holder: dict, verdict: dict, reason: str, session: str | None) -> dict:
    """Break a dead or invalid holder. Callers hold the project lock and checked the verdict."""
    event = {'event': 'broken-lock', 'slug': holder.get('slug'), 'at': common.iso(common.now()),
             'reason': reason, 'state': verdict['state'], 'why': verdict['why'],
             'broken_holder': _holder_brief(holder), 'holder': _me(session)}
    _append_ledger(paths, event)
    _remove(paths.holder)
    return event


class Holding:
    """The integrator lock while `land run` works: heartbeat thread, phase updates, safe release."""

    def __init__(self, paths: Paths, record: dict, interval_s: float):
        self.paths = paths
        self.record = record
        self.interval_s = max(0.2, float(interval_s))
        self.lost = False
        self._stop = threading.Event()
        self._thread = None

    def _ours(self, holder) -> bool:
        return isinstance(holder, dict) and not holder.get('invalid') and all(
            holder.get(k) == self.record[k] for k in ('token', 'pid', 'created'))

    def update(self, **fields) -> bool:
        """Refresh the heartbeat (and set `fields`) if holder.json still names this run. False when lost."""
        try:
            with self.paths.locked():
                holder = read_holder(self.paths)
                if not self._ours(holder):
                    self.lost = True
                    return False
                holder.update(fields)
                holder['heartbeat_at'] = common.iso(common.now())
                holder['beats'] = int(holder.get('beats') or 0) + 1
                common.write_json(self.paths.holder, holder)
                self.record.update(fields)
                return True
        except (TimeoutError, OSError, LandError):
            return not self.lost  # a busy lock skips one beat; the next one retries

    def _beat(self):
        while not self._stop.wait(self.interval_s):
            self.update()

    def start(self):
        self._thread = threading.Thread(target=self._beat, name='twinlane-land-heartbeat', daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=LOCK_TIMEOUT_S + 5)

    def still_held(self) -> bool:
        with self.paths.locked():
            ok = self._ours(read_holder(self.paths))
        if not ok:
            self.lost = True
        return ok

    def release(self) -> bool:
        """Remove holder.json only if it still names this pid, creation stamp and token."""
        self.stop()
        with self.paths.locked():
            if not self._ours(read_holder(self.paths)):
                return False
            _remove(self.paths.holder)
            return True


# ---------------------------------------------------------------- git probes

def _git(cwd: str, *args: str, timeout: float = GIT_TIMEOUT_S, code: int = common.EXIT_REFUSED) -> str:
    rc, out = common.git(cwd, *args, timeout=timeout)
    if rc != 0:
        raise LandError(f"git {' '.join(args)} failed in {cwd}: {common.clip(out, 400)}", code)
    return out


def _rev(cwd: str, ref: str) -> str:
    return _git(cwd, 'rev-parse', '--verify', '-q', ref + '^{commit}').strip()


def _is_ancestor(cwd: str, older: str, newer: str) -> bool:
    rc, out = common.git(cwd, 'merge-base', '--is-ancestor', older, newer, timeout=GIT_TIMEOUT_S)
    if rc not in (0, 1):
        raise LandError(f'git merge-base --is-ancestor failed in {cwd}: {common.clip(out, 400)}')
    return rc == 0


def _branch_of(cwd: str) -> str | None:
    rc, out = common.git(cwd, 'symbolic-ref', '-q', '--short', 'HEAD')
    return out.strip() if rc == 0 and out.strip() else None


def _toplevel(cwd: str) -> str:
    if not os.path.isdir(cwd):
        raise LandError(f'not a directory: {cwd}')
    return common.real_path(_git(cwd, 'rev-parse', '--show-toplevel').strip())


def _commits(cwd: str, base: str, tip: str, merges_only: bool = False) -> list:
    args = ['rev-list', '--reverse'] + (['--merges'] if merges_only else []) + [f'{base}..{tip}']
    return [line for line in _git(cwd, *args).splitlines() if line.strip()]


def _paths_between(cwd: str, base: str, tip: str) -> list:
    out = _git(cwd, 'diff', '--name-only', '-z', '--no-renames', base, tip)
    return [p for p in out.split('\0') if p]


def _status(cwd: str, untracked: bool = True) -> list:
    """`git status --porcelain -z` entries as (XY, path); no renames, so each entry is one path."""
    out = _git(cwd, 'status', '--porcelain=v1', '-z', '--no-renames',
               '--untracked-files=' + ('all' if untracked else 'no'))
    entries = []
    for item in out.split('\0'):
        if len(item) > 3:
            entries.append((item[:2], item[3:]))
    return entries


def _norm_rel(path: str) -> str:
    path = path.replace('\\', '/').strip('/')
    return path.lower() if common.IS_WIN or sys.platform == 'darwin' else path


def _overlaps(a: str, b: str) -> bool:
    a, b = _norm_rel(a), _norm_rel(b)
    return a == b or a.startswith(b + '/') or b.startswith(a + '/')


def _same_path(a: str, b: str) -> bool:
    return common.canon_path(a) == common.canon_path(b)


def _main_of(path: str) -> str:
    try:
        return projects.main_checkout(path)
    except projects.ProjectError as e:
        raise LandError(str(e))


# ---------------------------------------------------------------- project

def _project(repo: str | None):
    try:
        return projects.resolve(repo or os.getcwd())
    except projects.ProjectError as e:
        raise LandError(str(e), common.EXIT_USAGE)


def _target(main_root: str, cfg: dict) -> tuple:
    """(target branch, branch the main checkout is on)."""
    on = _branch_of(main_root)
    target = cfg['land'].get('target') or on
    if not target:
        raise LandError(f'the main checkout {main_root} has a detached HEAD and land.target is not set')
    return target, on


# ---------------------------------------------------------------- queue commands

def enqueue(slug: str, worktree: str | None = None, repo: str | None = None, session: str | None = None) -> dict:
    """Append SLUG (a worktree and its branch) to the project's queue. Idempotent per slug."""
    if not isinstance(slug, str) or not SLUG_RE.match(slug):
        raise LandError(f'invalid slug {slug!r}: use letters, digits, ".", "_" or "-" (max 80)', common.EXIT_USAGE)
    wt = _toplevel(worktree or os.getcwd())
    main_root, cfg = _project(repo or wt)
    if repo and not _same_path(_main_of(wt), main_root):
        raise LandError(f'{wt} is not a worktree of {main_root}')
    if _same_path(wt, main_root):
        raise LandError('the main checkout itself cannot be queued; queue a linked worktree')
    branch = _branch_of(wt)
    if not branch:
        raise LandError(f'{wt} has a detached HEAD; check out the branch to land')
    target, _ = _target(main_root, cfg)
    if branch == target:
        raise LandError(f'{wt} is on the target branch {target}')
    entry = {'slug': slug, 'worktree': wt, 'branch': branch, 'enqueued_at': common.iso(common.now()),
             'by': _me(session)}
    paths = Paths(main_root)
    with paths.locked():
        queue = read_queue(paths)
        for position, existing in enumerate(queue['entries'], 1):
            same_wt = _same_path(existing['worktree'], wt) if os.path.exists(existing['worktree']) \
                else common.canon_path(existing['worktree']) == common.canon_path(wt)
            if existing['slug'] == slug:
                if same_wt and existing['branch'] == branch:
                    return {'queued': False, 'position': position, 'entry': existing, 'repo': main_root}
                raise LandError(f"slug {slug} is already queued for {existing['worktree']} ({existing['branch']})")
            if existing['branch'] == branch or same_wt:
                raise LandError(f"{wt} ({branch}) is already queued as {existing['slug']}")
        queue['entries'].append(entry)
        queue['repo'] = main_root
        common.write_json(paths.queue, queue)
        return {'queued': True, 'position': len(queue['entries']), 'entry': entry, 'repo': main_root}


def dequeue(slug: str, repo: str | None = None) -> dict:
    main_root, cfg = _project(repo)
    paths = Paths(main_root)
    if not os.path.exists(paths.queue):
        return {'removed': False, 'slug': slug}
    with paths.locked():
        holder = read_holder(paths)
        if holder is not None and holder.get('slug') == slug:
            verdict = judge(holder, cfg['land']['heartbeat_stale_s'])
            if verdict['state'] not in BREAKABLE:
                raise LandError(f'{slug} is being landed: ' + describe_holder(holder, verdict), common.EXIT_RUNNING)
        queue = read_queue(paths)
        kept = [e for e in queue['entries'] if e['slug'] != slug]
        if len(kept) == len(queue['entries']):
            return {'removed': False, 'slug': slug}
        queue['entries'] = kept
        common.write_json(paths.queue, queue)
        return {'removed': True, 'slug': slug}


def status(repo: str | None = None) -> dict:
    """Read-only: queue, holder and its verdict. Creates nothing."""
    main_root, cfg = _project(repo)
    paths = Paths(main_root)
    holder = read_holder(paths)
    verdict = judge(holder, cfg['land']['heartbeat_stale_s'])
    target, on = _target(main_root, cfg)
    return {'repo': main_root, 'target': target, 'main_branch': on, 'state_dir': paths.dir,
            'holder': holder, 'holder_state': verdict, 'queue': read_queue(paths)['entries']}


def release(repo: str | None = None, do_break: bool = False, reason: str | None = None,
            session: str | None = None) -> dict:
    """Free the lock. Only a dead or invalid holder is ever removed, and only with --break --reason."""
    if do_break and not (reason or '').strip():
        raise LandError('--break needs --reason TEXT (recorded in the ledger)', common.EXIT_USAGE)
    main_root, cfg = _project(repo)
    paths = Paths(main_root)
    if not os.path.exists(paths.holder):
        return {'released': False, 'why': 'the lock is free'}
    with paths.locked():
        holder = read_holder(paths)
        verdict = judge(holder, cfg['land']['heartbeat_stale_s'])
        if holder is None:
            return {'released': False, 'why': 'the lock is free'}
        if verdict['state'] in BREAKABLE and do_break:
            event = _break_locked(paths, holder, verdict, reason.strip(), session)
            return {'released': True, 'event': event}
        if verdict['state'] in BREAKABLE:
            raise LandError(describe_holder(holder, verdict) + '; break it with --break --reason TEXT',
                            common.EXIT_RUNNING, {'holder': holder, 'holder_state': verdict})
        raise LandError(describe_holder(holder, verdict) + '; a holder that may be alive is never broken',
                        common.EXIT_RUNNING, {'holder': holder, 'holder_state': verdict})


# ---------------------------------------------------------------- run

class Lander:
    """One `land run`: checks, optional rebase, gate, land, ledger. With dry_run nothing is written."""

    def __init__(self, slug=None, repo=None, rebase=False, dry_run=False, break_reason=None, session=None):
        self.slug_wanted = slug
        self.rebase = rebase
        self.dry_run = dry_run
        self.break_reason = (break_reason or '').strip() or None
        self.session = session
        self.main_root, self.cfg = _project(repo)
        self.land_cfg = self.cfg['land']
        self.paths = Paths(self.main_root)
        self.holding = None
        self.notes = []
        self.facts = {}

    # --- facts ---------------------------------------------------------------

    def _head_entry(self, queue: dict) -> dict:
        entries = queue['entries']
        if not entries:
            raise LandError('the land queue is empty')
        head = entries[0]
        if self.slug_wanted and self.slug_wanted != head['slug']:
            where = next((i for i, e in enumerate(entries, 1) if e['slug'] == self.slug_wanted), None)
            raise LandError(f"{self.slug_wanted} is not the queue head ({head['slug']} is)"
                            + (f'; it is number {where}' if where else '; it is not queued'))
        return head

    def _values(self, head: str, base: str) -> dict:
        f = self.facts
        return {'repo': self.main_root, 'worktree': f['worktree'], 'branch': f['branch'], 'slug': f['slug'],
                'target': f['target'], 'base': base, 'source_commit': head}

    def _command(self, key: str, head: str, base: str):
        template = self.land_cfg.get(key)
        if template is None:
            return None
        try:
            return projects.argv(template, **self._values(head, base))
        except projects.ProjectError as e:
            raise LandError(f'land.{key}: {e}', common.EXIT_USAGE)

    def _main_checkout_problems(self, landing_paths: list) -> list:
        """Staged changes, or peer edits and untracked files on the paths this landing writes."""
        problems = []
        for xy, path in _status(self.main_root):
            if xy == '??':
                if any(_overlaps(path, p) for p in landing_paths):
                    problems.append(f'untracked {path} would be overwritten')
            elif xy[0] not in (' ', '?', '!'):
                problems.append(f'staged change {xy} {path}')
            elif any(_overlaps(path, p) for p in landing_paths):
                problems.append(f'peer edit {xy.strip()} {path} is on a landing path')
        return problems

    def check(self, entry: dict) -> dict:
        """Every read-only check, in order; raises LandError on the first refusal."""
        f = self.facts
        f.update(slug=entry['slug'], branch=entry['branch'], worktree=entry['worktree'])
        target, on = _target(self.main_root, self.cfg)
        f['target'] = target
        if on != target:
            raise LandError(f'the main checkout {self.main_root} is on {on or "a detached HEAD"}, not the target '
                            f'{target}')
        wt = entry['worktree']
        if not os.path.isdir(wt):
            raise LandError(f'the worktree {wt} is missing')
        if not _same_path(_main_of(wt), self.main_root):
            raise LandError(f'{wt} is no longer a worktree of {self.main_root}')
        on_branch = _branch_of(wt)
        if on_branch != entry['branch']:
            raise LandError(f"{wt} is not on the queued branch {entry['branch']} (HEAD: {on_branch or 'detached'})")
        dirty = _status(wt, untracked=False)
        if dirty:
            raise LandError(f'{wt} has staged or modified tracked files: '
                            + ', '.join(f'{xy.strip()} {p}' for xy, p in dirty[:10]))
        head = _rev(wt, 'HEAD')
        if head != _rev(self.main_root, 'refs/heads/' + entry['branch']):
            raise LandError(f"{wt} HEAD does not match refs/heads/{entry['branch']}")
        base = _rev(self.main_root, 'refs/heads/' + target)
        f.update(head=head, base=base)
        if _is_ancestor(self.main_root, head, base):
            raise LandError(f"nothing to land: {entry['branch']} ({head[:12]}) is already in {target}; dequeue it")
        f['ff'] = _is_ancestor(self.main_root, base, head)
        f['range_merges'] = _commits(self.main_root, base, head, merges_only=True)
        command_rebases = bool(self.land_cfg.get('command_rebases')) and self.land_cfg.get('command') is not None
        f['command_rebases'] = command_rebases
        if command_rebases and f['range_merges']:
            raise LandError(f"the range {base[:12]}..{head[:12]} holds merge commits "
                            f"({', '.join(m[:12] for m in f['range_merges'])}) and land.command rebases: "
                            'a rebase would flatten them')
        if not f['ff']:
            if not (self.rebase or command_rebases):
                raise LandError(f'{target} moved since {entry["branch"]} forked: not a fast-forward; '
                                'rebase the branch onto the target, or pass --rebase')
            if f['range_merges']:
                raise LandError(f"the range holds merge commits ({', '.join(m[:12] for m in f['range_merges'])}); "
                                'a rebase would flatten them, so it is refused')
        f['will_rebase'] = (not f['ff']) and self.rebase and not command_rebases
        merge_base = _git(self.main_root, 'merge-base', base, head).strip()
        f['landing_paths'] = _paths_between(self.main_root, merge_base, head)
        problems = self._main_checkout_problems(f['landing_paths'])
        if problems:
            raise LandError(f'the main checkout {self.main_root} has changes that landing would touch: '
                            + '; '.join(problems[:10]), details={'problems': problems})
        f['gate_argv'] = self._command('gate', head, base)
        f['command_argv'] = self._command('command', head, base)
        f['commits'] = _commits(self.main_root, base if f['ff'] else merge_base, head)
        return f

    # --- dry run -------------------------------------------------------------

    def dry(self) -> dict:
        queue = read_queue(self.paths)
        entry = self._head_entry(queue)
        holder = read_holder(self.paths)
        verdict = judge(holder, self.land_cfg['heartbeat_stale_s'])
        if holder is not None and not (verdict['state'] in BREAKABLE and self.break_reason):
            raise LandError(describe_holder(holder, verdict), common.EXIT_RUNNING,
                            {'holder': holder, 'holder_state': verdict})
        f = self.check(entry)
        plan = {'dry_run': True, 'slug': f['slug'], 'branch': f['branch'], 'target': f['target'],
                'from': f['base'], 'head': f['head'], 'commits': f['commits'], 'merges': f['range_merges'],
                'mode': self._mode(), 'gate_argv': f['gate_argv'], 'command_argv': f['command_argv'],
                'would_break': holder is not None, 'holder_state': verdict}
        return plan

    def _mode(self) -> str:
        f = self.facts
        if f.get('command_argv'):
            return ('rebase+' if f.get('will_rebase') else '') + 'command'
        return 'rebase+ff' if f.get('will_rebase') else 'ff'

    # --- real run ------------------------------------------------------------

    def _acquire(self) -> dict:
        os.makedirs(self.paths.dir, exist_ok=True)
        with self.paths.locked():
            entry = self._head_entry(read_queue(self.paths))
            holder = read_holder(self.paths)
            if holder is not None:
                verdict = judge(holder, self.land_cfg['heartbeat_stale_s'])
                if verdict['state'] in BREAKABLE and self.break_reason:
                    _break_locked(self.paths, holder, verdict, self.break_reason, self.session)
                else:
                    hint = '; break it with --break --reason TEXT' if verdict['state'] in BREAKABLE else ''
                    raise LandError(describe_holder(holder, verdict) + hint, common.EXIT_RUNNING,
                                    {'holder': holder, 'holder_state': verdict})
            stamp = common.iso(common.now())
            record = dict(_me(self.session), slug=entry['slug'], token=uuid.uuid4().hex, acquired_at=stamp,
                          heartbeat_at=stamp, beats=0, phase='checks', child=None)
            common.write_json(self.paths.holder, record)
        self.holding = Holding(self.paths, record, self.land_cfg['heartbeat_s'])
        self.holding.start()
        return entry

    def _event(self, event: str, **extra) -> dict:
        f = self.facts
        rec = {'event': event, 'slug': f.get('slug'), 'branch': f.get('branch'), 'target': f.get('target'),
               'worktree': f.get('worktree'), 'at': common.iso(common.now()),
               'holder': {k: self.holding.record.get(k) for k in ('pid', 'created', 'session', 'host')}
               if self.holding else _me(self.session)}
        rec.update(extra)
        return rec

    def _record(self, event: dict, dequeue_slug: bool = False) -> None:
        with self.paths.locked():
            _append_ledger(self.paths, event)
            if dequeue_slug:
                queue = read_queue(self.paths)
                queue['entries'] = [e for e in queue['entries'] if e['slug'] != event['slug']]
                common.write_json(self.paths.queue, queue)

    def _run_logged(self, argv: list, what: str, env: dict) -> dict:
        os.makedirs(self.paths.logs, exist_ok=True)
        log = os.path.join(self.paths.logs, f"{common.stamp()}-{self.facts['slug']}-{what}.log")
        started = time.monotonic()
        rc = None
        with open(log, 'wb') as handle:
            handle.write((f'# twinlane land {what}: ' + json.dumps(argv) + '\n').encode('utf-8'))
            handle.flush()
            try:
                proc = subprocess.Popen(argv, cwd=self.facts['worktree'], env=env, stdin=subprocess.DEVNULL,
                                        stdout=handle, stderr=subprocess.STDOUT,
                                        creationflags=common.no_window_flags())
            except OSError as e:
                handle.write(f'# could not start: {e}\n'.encode('utf-8'))
                rc = 127
            if rc is None:
                created = common.process_created(proc.pid)
                self.holding.update(phase=what, child={'pid': proc.pid, 'created': created, 'what': what})
                try:
                    rc = proc.wait(timeout=self.land_cfg['gate_timeout_s'])
                except subprocess.TimeoutExpired:
                    common.kill_tree(proc.pid, created)
                    try:
                        proc.wait(timeout=30)
                    except subprocess.TimeoutExpired:
                        pass
                    handle.write(f"\n# timed out after {self.land_cfg['gate_timeout_s']} s\n".encode('utf-8'))
                    rc = 124
                except BaseException:
                    common.kill_tree(proc.pid, created)
                    raise
                finally:
                    self.holding.update(child=None)
        return {'argv': argv, 'rc': rc, 'seconds': round(time.monotonic() - started, 3), 'log': log}

    def _env(self) -> dict:
        f = self.facts
        return dict(os.environ, TWINLANE_LAND_SLUG=f['slug'], TWINLANE_LAND_BASE=f['base'],
                    TWINLANE_LAND_HEAD=f['head'], TWINLANE_LAND_TARGET=f['target'],
                    TWINLANE_LAND_REPO=self.main_root)

    def _do_rebase(self) -> None:
        f = self.facts
        wt, old = f['worktree'], f['head']
        self.holding.update(phase='rebase')
        rc, out = common.git(wt, 'rebase', '--no-autostash', f['base'], timeout=REBASE_TIMEOUT_S)
        if rc != 0:
            common.git(wt, 'rebase', '--abort', timeout=REBASE_TIMEOUT_S)
            restored = common.git(wt, 'rev-parse', 'HEAD')[1].strip() == old and _branch_of(wt) == f['branch']
            raise LandError(f"rebase of {f['branch']} onto {f['target']} stopped: {common.clip(out, 400)}; "
                            + ('the branch is unchanged' if restored else 'CHECK THE WORKTREE: the abort did not '
                               'restore the branch'), common.EXIT_FAILED)
        new = _rev(wt, 'HEAD')
        if _branch_of(wt) != f['branch'] or not _is_ancestor(wt, f['base'], new):
            raise LandError('the rebase did not leave the branch on top of the target', common.EXIT_FAILED)
        f.update(rebased_from=old, head=new, ff=True)
        f['landing_paths'] = _paths_between(self.main_root, f['base'], new)
        f['commits'] = _commits(self.main_root, f['base'], new)
        f['gate_argv'] = self._command('gate', new, f['base'])
        f['command_argv'] = self._command('command', new, f['base'])
        self.notes.append(f'rebased {len(f["commits"])} commit(s) from {old[:12]} onto {f["base"][:12]}')

    def _gate(self) -> dict | None:
        f = self.facts
        if not f['gate_argv']:
            self.notes.append('no land.gate configured for this project: nothing was gated')
            return None
        wt = f['worktree']
        before = _status(wt)
        result = self._run_logged(f['gate_argv'], 'gate', self._env())
        if result['rc'] != 0:
            raise LandError(f"the gate failed (rc {result['rc']}); see {result['log']}", common.EXIT_FAILED,
                            {'gate': result})
        after_head = common.git(wt, 'rev-parse', 'HEAD')[1].strip()
        moved = []
        if after_head != f['head'] or _branch_of(wt) != f['branch'] \
                or _rev(self.main_root, 'refs/heads/' + f['branch']) != f['head']:
            moved.append(f'HEAD moved from {f["head"][:12]} to {after_head[:12]}')
        after = _status(wt)
        if after != before:
            changed = sorted(set(after) ^ set(before))
            moved.append('the tree changed: ' + ', '.join(f'{xy.strip()} {p}' for xy, p in changed[:10]))
        if moved:
            raise LandError('the gate changed what it judged, so its verdict does not apply: ' + '; '.join(moved),
                            details={'gate': result})
        return result

    def _land(self) -> dict:
        f = self.facts
        main = self.main_root
        if not self.holding.still_held():
            raise LandError('the integrator lock was taken from this run; nothing landed', common.EXIT_FAILED)
        self.holding.update(phase='landing')
        target_now = _rev(main, 'refs/heads/' + f['target'])
        if target_now != f['base']:
            raise LandError(f"{f['target']} moved during the run ({f['base'][:12]} -> {target_now[:12]}); "
                            'run again', details={'moved_to': target_now})
        if _branch_of(main) != f['target']:
            raise LandError(f'the main checkout left {f["target"]}')
        problems = self._main_checkout_problems(f['landing_paths'])
        if problems:
            raise LandError('the main checkout changed during the run: ' + '; '.join(problems[:10]),
                            details={'problems': problems})
        if f['command_argv']:
            result = self._run_logged(f['command_argv'], 'land', self._env())
            to = _rev(main, 'refs/heads/' + f['target'])
            info = {'command': result}
            if to == f['base']:
                raise LandError(f"land.command {'failed (rc ' + str(result['rc']) + ')' if result['rc'] else 'ran'}"
                                f" and {f['target']} did not move; nothing landed", common.EXIT_FAILED, info)
            info.update(self._range(f['base'], to))
            if result['rc'] != 0:
                raise LandError(f"land.command failed (rc {result['rc']}) AFTER moving {f['target']} to {to[:12]}; "
                                'the range is in the ledger: inspect it', common.EXIT_FAILED, info)
            return info
        rc, out = common.git(main, 'merge', '--ff-only', '--no-edit', f['head'], timeout=REBASE_TIMEOUT_S)
        to = _rev(main, 'refs/heads/' + f['target'])
        if rc != 0 or to != f['head']:
            info = self._range(f['base'], to) if to != f['base'] else {}
            raise LandError(f'git merge --ff-only failed: {common.clip(out, 400)}', common.EXIT_FAILED, info)
        return self._range(f['base'], to)

    def _range(self, base: str, to: str) -> dict:
        f = self.facts
        main = self.main_root
        info = {'from': base, 'to': to, 'commits': _commits(main, base, to),
                'merges': _commits(main, base, to, merges_only=True)}
        gated_tree = _git(main, 'rev-parse', f['head'] + '^{tree}').strip()
        landed_tree = _git(main, 'rev-parse', to + '^{tree}').strip()
        info.update(gated_commit=f['head'], gated_tree=gated_tree, landed_tree=landed_tree,
                    tree_matches=gated_tree == landed_tree, contains_gated_commit=_is_ancestor(main, f['head'], to))
        if gated_tree != landed_tree:
            info['differs_from_gated'] = _paths_between(main, f['head'], to)
            self.notes.append(f"the landed tree differs from the gated tree in {len(info['differs_from_gated'])} "
                              'path(s): ' + ', '.join(info['differs_from_gated'][:10]))
        return info

    def run(self) -> dict:
        if self.dry_run:
            return self.dry()
        entry = self._acquire()
        gate = None
        try:
            try:
                self.check(entry)
                if self.facts['will_rebase']:
                    self._do_rebase()
                gate = self._gate()
                landed = self._land()
            except LandError as e:
                event = 'failed' if e.code == common.EXIT_FAILED else 'refused'
                extra = {'reason': str(e), 'from': self.facts.get('base'), 'head': self.facts.get('head'),
                         'gate': e.details.get('gate', gate)}
                extra.update({k: v for k, v in e.details.items() if k != 'gate'})
                e.details['event'] = self._event(event, **extra)
                try:
                    self._record(e.details['event'])
                except Exception as err:  # noqa: BLE001 - keep the refusal; note the lost ledger line
                    print(f'twinlane land: could not write the ledger ({err})', file=sys.stderr)
                raise
            self.holding.update(phase='recording')
            event = self._event('landed', gate=gate, mode=self._mode(), notes=self.notes,
                                rebased_from=self.facts.get('rebased_from'), **landed)
            try:
                self._record(event, dequeue_slug=True)
            except Exception as e:  # noqa: BLE001 - the land happened; say so with its exact range
                raise LandError(f"LANDED {event['slug']} {event['from']}..{event['to']} but the ledger or queue "
                                f'write failed ({e}); record it by hand: ' + json.dumps(event), common.EXIT_FAILED,
                                {'event': event})
            return event
        finally:
            try:
                released = self.holding.release()
            except Exception as e:  # noqa: BLE001 - never mask the run's own outcome
                released = False
                print(f'twinlane land: could not release the lock ({e}); see land status', file=sys.stderr)
            if not released:
                print('twinlane land: the lock was not released by this run; see land status', file=sys.stderr)


# ---------------------------------------------------------------- cli

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane land', description='queue and land worktree branches one at a time')
    sub = ap.add_subparsers(dest='cmd')

    def add(name, help_text):
        p = sub.add_parser(name, help=help_text)
        p.add_argument('--repo', help='any directory inside the repository (default: the current directory)')
        p.add_argument('--json', action='store_true')
        return p

    p = add('enqueue', 'append a worktree branch to the queue')
    p.add_argument('slug')
    p.add_argument('--worktree', help='the worktree to land (default: the current directory)')
    p.add_argument('--session')
    add('status', 'queue and lock holder')
    add('next', 'the queue head')
    p = add('run', 'land the queue head')
    p.add_argument('slug', nargs='?')
    p.add_argument('--rebase', action='store_true', help='rebase a linear branch onto a moved target first')
    p.add_argument('--dry-run', action='store_true', help='print the checks; take no lock and write nothing')
    p.add_argument('--break', dest='do_break', action='store_true', help='break a DEAD holder first')
    p.add_argument('--reason')
    p.add_argument('--session')
    p = add('release', 'free the lock (a dead holder only with --break --reason)')
    p.add_argument('--break', dest='do_break', action='store_true')
    p.add_argument('--reason')
    p.add_argument('--session')
    p = add('dequeue', 'remove a slug from the queue')
    p.add_argument('slug')
    p = add('log', 'ledger events')
    p.add_argument('slug', nargs='?')
    return ap


def _human_status(st: dict) -> str:
    lines = [f"repo {st['repo']}  target {st['target']}"]
    holder, verdict = st['holder'], st['holder_state']
    lines.append('lock: free' if holder is None else 'lock: ' + describe_holder(holder, verdict))
    if not st['queue']:
        lines.append('queue: empty')
    for i, e in enumerate(st['queue'], 1):
        lines.append(f"  {i}. {e['slug']}  {e['branch']}  {e['worktree']}  queued {e.get('enqueued_at')}")
    return '\n'.join(lines)


def _human_run(res: dict) -> str:
    if res.get('dry_run'):
        lines = [f"dry run: would land {res['slug']} ({res['branch']}) on {res['target']} by {res['mode']}: "
                 f"{len(res['commits'])} commit(s) after {res['from'][:12]}"]
        if res.get('would_break'):
            lines.append('  would first break the dead holder: ' + res['holder_state']['why'])
        lines.append('  gate: ' + (json.dumps(res['gate_argv']) if res['gate_argv'] else 'none configured'))
        if res['command_argv']:
            lines.append('  land command: ' + json.dumps(res['command_argv']))
        return '\n'.join(lines)
    lines = [f"landed {res['slug']} on {res['target']}: {res['from'][:12]}..{res['to'][:12]} "
             f"({len(res['commits'])} commit(s), {res['mode']})"]
    lines += ['  note: ' + n for n in res.get('notes') or []]
    return '\n'.join(lines)


def cli(argv) -> int:
    ap = _parser()
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_USAGE if e.code not in (0, None) else common.EXIT_OK
    if not args.cmd:
        ap.print_help(sys.stderr)
        return common.EXIT_USAGE
    if args.cmd in ('run', 'release', 'dequeue'):
        refused = projects.refuse_in_worker('land ' + args.cmd)
        if refused is not None:
            return refused
    try:
        if args.cmd == 'enqueue':
            res = enqueue(args.slug, args.worktree, args.repo, args.session)
            human = (f"{'queued' if res['queued'] else 'already queued'} {args.slug} "
                     f"({res['entry']['branch']}) at position {res['position']}")
        elif args.cmd == 'status':
            res = status(args.repo)
            human = _human_status(res)
        elif args.cmd == 'next':
            st = status(args.repo)
            res = {'head': st['queue'][0] if st['queue'] else None, 'holder_state': st['holder_state']}
            human = 'queue empty' if res['head'] is None else \
                f"{res['head']['slug']}  {res['head']['branch']}  {res['head']['worktree']}"
        elif args.cmd == 'run':
            if args.do_break and not (args.reason or '').strip():
                raise LandError('--break needs --reason TEXT (recorded in the ledger)', common.EXIT_USAGE)
            reason = args.reason if args.do_break else None
            res = Lander(args.slug, args.repo, args.rebase, args.dry_run, reason, args.session).run()
            human = _human_run(res)
        elif args.cmd == 'release':
            res = release(args.repo, args.do_break, args.reason, args.session)
            human = ('broke the lock: ' + res['event']['why']) if res.get('event') else \
                ('released' if res['released'] else res['why'])
        elif args.cmd == 'dequeue':
            res = dequeue(args.slug, args.repo)
            human = ('removed ' if res['removed'] else 'not queued: ') + args.slug
        else:  # log
            main_root, _ = _project(args.repo)
            events = [e for e in read_ledger(main_root) if not args.slug or e.get('slug') == args.slug]
            res = {'events': events}
            human = '\n'.join(f"{e.get('at')}  {e.get('event'):<11} {e.get('slug')}  "
                              f"{(e.get('from') or '')[:12]}..{(e.get('to') or '')[:12]}  "
                              f"{common.clip(e.get('reason') or '', 100)}" for e in events) or 'no events'
    except LandError as e:
        if args.json:
            common.emit({'ok': False, 'code': e.code, 'reason': str(e), 'details': e.details}, True)
        print(f'twinlane land {args.cmd}: ' + ('refused' if e.code in (common.EXIT_REFUSED, common.EXIT_USAGE)
                                                else 'lock held' if e.code == common.EXIT_RUNNING else 'failed')
              + f': {e}', file=sys.stderr)
        return e.code
    except TimeoutError as e:
        print(f'twinlane land {args.cmd}: the state lock stayed busy ({e}); try again', file=sys.stderr)
        return common.EXIT_RUNNING
    common.emit(res, args.json, human)
    return common.EXIT_OK
