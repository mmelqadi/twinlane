"""twinlane ship: a release manifest for landed work, and the project's ship command behind a dry run.

  twinlane ship plan SLUG [--landing N] [--baseline REF] [--allow-foreign] [--allow-superseded] [--out F]
                          [--repo DIR] [--json]
  twinlane ship plan FROM..TO [--allow-foreign] [--allow-superseded] [--out F] [--repo DIR] [--json]
  twinlane ship run [SLUG] --manifest F [--repo DIR] [--json]
  twinlane ship run [SLUG] --manifest F --execute --confirm SHA8 [--repo DIR] [--json]

`plan` reads the exact range of a landing from the land ledger (the latest one of SLUG, or --landing N:
1 is the first, -1 the last) or takes an explicit FROM..TO range, and writes a manifest:
  files     every path added or modified in the range, with the sha256 and size of the bytes a checkout
            of the source commit produces on this machine (`git cat-file --filters`, attributes read
            from the source commit itself, so .gitattributes eol and filters apply as they were there)
  deleted   every path the range deletes
  commits   the commits in the range, each with the slug the ledger attributes it to
  excluded_commits
            commits on the target that are not the slug's and are not meant to ship: those after the
            source commit, those inside the range that belong to someone else, and (with --baseline)
            those between the baseline and the range
  flags     foreign_in_range: commits whose changes are inside the shipped bytes but are not the
            slug's (with a FROM..TO range: commits no landing recorded). The plan is refused unless
            --allow-foreign is given. superseded_paths: manifest paths that later commits on the target
            changed again, so the shipped bytes are older than the target's; each names the newer
            commits. The plan is refused unless --allow-superseded is given, and the manifest records
            allow_superseded. --execute checks again: any supersession the manifest does not list
            (a newer commit since the plan) refuses even with the flag.
It is written to STATE/ship/<repo_key>/<label>-<stamp>.json (or --out F) and its sha256 is printed.

`run` never ships by default. Without --execute it runs the project's ship.dry_run_command, writes a
dry-run receipt, and prints (never runs) the ship.command argv. With no ship.command configured it
only prints the manifest path and exits 3. `--execute` needs ship.command, a passing dry-run receipt
for the same manifest sha256 that is younger than ship.dry_run_max_age_s and not used yet, the same
command argv as that dry run, --confirm equal to the first 8 hex digits of the sha256 (or the same
answer typed at an interactive prompt), a manifest that still matches the repository, and a final
re-hash of the manifest just before the command starts. A receipt is good for one execute.
`run` is refused inside a twinlane worker job.

The commands receive a private copy of the manifest (STATE/ship/<repo_key>/manifests/<sha256>.json)
as {manifest}, and TWINLANE_SHIP_* environment variables. Placeholders are those of
twinlane.projects; any other name is refused. Nothing runs through a shell.
Exit codes: 0 ok, 2 usage, 3 refused or not configured, 4 another execute holds the lock, 5 failed.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

from twinlane import common, land, projects

SCHEMA = 'twinlane-ship-manifest-v1'
RECEIPT_SCHEMA = 'twinlane-ship-dry-run-v1'
LOCK_TIMEOUT_S = 30
EXECUTE_LOCK_TIMEOUT_S = 2
GIT_TIMEOUT_S = 120
FILTER_TIMEOUT_S = 600
DRY_RUN_TIMEOUT_S = 1800
FUTURE_SKEW_S = 120
MAX_MANIFEST_BYTES = 64 * 1024 * 1024
MIN_GIT = (2, 40)  # --attr-source
COMMIT_RE = re.compile(r'^[0-9a-f]{40}(?:[0-9a-f]{24})?$')
SHA256_RE = re.compile(r'^[0-9a-f]{64}$')
_MISSING = object()


class ShipError(Exception):
    """A refusal or failure with its exit code; `details` go to --json output."""

    def __init__(self, message: str, code: int = common.EXIT_REFUSED, details: dict | None = None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


# ---------------------------------------------------------------- files

class Paths:
    def __init__(self, main_root: str):
        self.main_root = main_root
        self.dir = projects.state_dir('ship', main_root)
        self.lock = os.path.join(self.dir, '.lock')
        self.execute_lock = os.path.join(self.dir, '.execute.lock')
        self.ledger = os.path.join(self.dir, 'ledger.jsonl')
        self.logs = os.path.join(self.dir, 'logs')
        self.manifests = os.path.join(self.dir, 'manifests')
        self.dryruns = os.path.join(self.dir, 'dryruns')

    def locked(self):
        return common.file_lock(self.lock, LOCK_TIMEOUT_S)

    def copy(self, sha: str) -> str:
        return os.path.join(self.manifests, sha + '.json')

    def receipt(self, sha: str) -> str:
        return os.path.join(self.dryruns, sha + '.json')


def _append_ledger(paths: Paths, event: dict) -> None:
    """Append one event under the project lock."""
    event = dict(event, at=common.iso(common.now()))
    with paths.locked():
        os.makedirs(paths.dir, exist_ok=True)
        with open(paths.ledger, 'a', encoding='utf-8', newline='\n') as handle:
            handle.write(json.dumps(event, ensure_ascii=False, sort_keys=True) + '\n')
            handle.flush()
            os.fsync(handle.fileno())


def _write_bytes(path: str, data: bytes, exclusive: bool = False) -> None:
    """Write `data` to `path`: exclusively (fails if it exists) or atomically (temp file, then replace)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    if exclusive:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o644)
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        return
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def manifest_bytes(manifest: dict) -> bytes:
    return (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + '\n').encode('utf-8')


# ---------------------------------------------------------------- git

def _git(cwd: str, *args: str, timeout: float = GIT_TIMEOUT_S) -> str:
    rc, out = common.git(cwd, *args, timeout=timeout)
    if rc != 0:
        raise ShipError(f"git {' '.join(args)} failed in {cwd}: {common.clip(out, 400)}")
    return out


def _commit(cwd: str, ref: str) -> str:
    rc, out = common.git(cwd, 'rev-parse', '--verify', '-q', '--end-of-options', ref + '^{commit}')
    if rc != 0 or not COMMIT_RE.match(out.strip()):
        raise ShipError(f'{ref!r} is not a commit in {cwd}')
    return out.strip()


def _is_ancestor(cwd: str, older: str, newer: str) -> bool:
    rc, out = common.git(cwd, 'merge-base', '--is-ancestor', older, newer, timeout=GIT_TIMEOUT_S)
    if rc not in (0, 1):
        raise ShipError(f'git merge-base --is-ancestor failed in {cwd}: {common.clip(out, 400)}')
    return rc == 0


def _rev_list(cwd: str, base: str, tip: str) -> list:
    return [line for line in _git(cwd, 'rev-list', '--reverse', f'{base}..{tip}').splitlines() if line.strip()]


def _log(cwd: str, base: str, tip: str) -> list:
    """[{sha, subject, paths}] for base..tip, oldest first; a merge's paths are its first-parent diff."""
    out = _git(cwd, 'log', '--reverse', '--no-color', '--no-show-signature', '--no-renames',
               '--diff-merges=first-parent', '--name-only', '-z', '--format=%x01%H%x02%s', f'{base}..{tip}')
    commits, current, fresh = [], None, False
    header = re.compile(r'^\n?\x01([0-9a-f]{40}(?:[0-9a-f]{24})?)\x02(.*)$', re.S)
    for chunk in out.split('\0'):
        match = header.match(chunk)
        if match:
            current = {'sha': match.group(1), 'subject': match.group(2), 'paths': []}
            commits.append(current)
            fresh = True
            continue
        if fresh and chunk.startswith('\n'):
            chunk = chunk[1:]
        fresh = False
        if chunk and current is not None:
            current['paths'].append(chunk)
    return commits


def _git_version(cwd: str) -> tuple:
    rc, out = common.git(cwd, 'version')
    match = re.search(r'(\d+)\.(\d+)', out) if rc == 0 else None
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _require_git(cwd: str) -> None:
    if _git_version(cwd) < MIN_GIT:
        raise ShipError('ship needs git {}.{} or newer (it reads .gitattributes from the source commit with '
                        '--attr-source)'.format(*MIN_GIT), common.EXIT_USAGE)


def _hash_filtered(cwd: str, commit: str, path: str) -> tuple:
    """(sha256, size) of the bytes a checkout of `commit` writes for `path` on this machine."""
    argv = ['git', '-C', cwd, '--attr-source=' + commit, 'cat-file', '--filters', f'{commit}:{path}']
    h, size = hashlib.sha256(), 0
    with tempfile.TemporaryFile() as err:
        try:
            proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=err,
                                    env=common.git_env(), creationflags=common.no_window_flags())
        except OSError as e:
            raise ShipError(f'cannot run git: {e}', common.EXIT_FAILED)
        timer = threading.Timer(FILTER_TIMEOUT_S, proc.kill)
        timer.start()
        try:
            for chunk in iter(lambda: proc.stdout.read(1 << 20), b''):
                h.update(chunk)
                size += len(chunk)
            rc = proc.wait()
        finally:
            timer.cancel()
            proc.stdout.close()
        if rc != 0:
            err.seek(0)
            text = err.read().decode('utf-8', 'replace')
            raise ShipError(f'git cat-file --filters {commit[:12]}:{path} failed: {common.clip(text.strip(), 400)}',
                            common.EXIT_FAILED)
    return h.hexdigest(), size


def collect_files(cwd: str, base: str, source: str) -> tuple:
    """(files, deleted) for base..source from `git diff-tree --raw` (plumbing: no user diff config)."""
    out = _git(cwd, 'diff-tree', '-r', '-z', '--no-renames', '--no-abbrev', base, source)
    chunks = out.split('\0')
    files, deleted = [], []
    i = 0
    while i < len(chunks):
        meta = chunks[i].lstrip('\n')
        if not meta.startswith(':'):
            i += 1
            continue
        if i + 1 >= len(chunks):
            raise ShipError('unexpected git diff-tree output')
        path = chunks[i + 1]
        i += 2
        parts = meta[1:].split()
        if len(parts) != 5:
            raise ShipError(f'unexpected git diff-tree line {meta!r}')
        _old_mode, new_mode, old_blob, new_blob, status = parts
        if status == 'D':
            deleted.append({'path': path, 'blob': old_blob})
            continue
        if status not in ('A', 'M', 'T'):
            raise ShipError(f'unexpected change {status} for {path}')
        entry = {'path': path, 'status': status, 'mode': new_mode, 'blob': new_blob}
        if new_mode == '160000':  # a submodule: no bytes in this repository
            entry.update(sha256=None, size=None, gitlink=True)
        else:
            entry['sha256'], entry['size'] = _hash_filtered(cwd, source, path)
        files.append(entry)
    files.sort(key=lambda e: e['path'])
    deleted.sort(key=lambda e: e['path'])
    return files, deleted


# ---------------------------------------------------------------- project

def _project(repo: str | None):
    try:
        return projects.resolve(repo or os.getcwd())
    except projects.ProjectError as e:
        raise ShipError(str(e), common.EXIT_USAGE)


def _target(main_root: str, cfg: dict) -> str:
    rc, out = common.git(main_root, 'symbolic-ref', '-q', '--short', 'HEAD')
    target = cfg['land'].get('target') or (out.strip() if rc == 0 else '')
    if not target:
        raise ShipError(f'the main checkout {main_root} has a detached HEAD and land.target is not set')
    return target


def _owners(main_root: str) -> dict:
    """commit -> sorted slugs the land ledger attributes it to."""
    owners = {}
    for event in land.landings(main_root):
        for sha in event.get('commits') or []:
            if isinstance(sha, str) and isinstance(event.get('slug'), str):
                owners.setdefault(sha, set()).add(event['slug'])
    return {sha: sorted(slugs) for sha, slugs in owners.items()}


def _superseded(log_after: list, manifest_paths: set) -> list:
    """[{path, by: [sha], subjects: [subject]}]: manifest paths that commits after the source changed again."""
    by = {}
    for commit in log_after:
        for path in commit['paths']:
            if path in manifest_paths:
                entry = by.setdefault(path, {'path': path, 'by': [], 'subjects': []})
                entry['by'].append(commit['sha'])
                entry['subjects'].append(commit['subject'])
    return [by[path] for path in sorted(by)]


def describe_superseded(entries: list, limit: int = 10) -> str:
    """'path (by sha12 subject, ...); ...' for messages."""
    parts = []
    for entry in entries[:limit]:
        subjects = entry.get('subjects') or [''] * len(entry.get('by') or [])
        newer = ', '.join(f'{sha[:12]} {common.clip(subject, 50)}'.strip()
                          for sha, subject in zip(entry.get('by') or [], subjects))
        parts.append(f"{entry.get('path')} (superseded by {newer})")
    more = len(entries) - limit
    return '; '.join(parts) + (f'; and {more} more path(s)' if more > 0 else '')


# ---------------------------------------------------------------- plan

def _pick_landing(main_root: str, slug: str, number: int | None) -> tuple:
    found = land.landings(main_root, slug)
    if not found:
        raise ShipError(f'the land ledger has no landing for {slug!r}')
    n = -1 if number is None else number
    if n == 0 or not -len(found) <= n <= len(found):
        raise ShipError(f'{slug} has {len(found)} landing(s); --landing takes 1..{len(found)} or -1..-{len(found)}',
                        common.EXIT_USAGE)
    index = n - 1 if n > 0 else len(found) + n
    return index + 1, found[index]


def build_manifest(target_arg: str, repo: str | None = None, landing_no: int | None = None,
                   baseline: str | None = None, allow_foreign: bool = False,
                   allow_superseded: bool = False) -> dict:
    """The manifest dict for a slug's landing or a FROM..TO range. Writes nothing."""
    main_root, cfg = _project(repo)
    _require_git(main_root)
    landing, slug, label, number = None, None, None, None
    if '..' in target_arg:
        if landing_no is not None or baseline is not None:
            raise ShipError('--landing and --baseline only go with a SLUG, not a FROM..TO range', common.EXIT_USAGE)
        ends = target_arg.split('..')
        if len(ends) != 2 or not ends[0] or not ends[1] or ends[1].startswith('.'):
            raise ShipError(f'{target_arg!r} is not a FROM..TO range', common.EXIT_USAGE)
        base, source = _commit(main_root, ends[0]), _commit(main_root, ends[1])
        target = _target(main_root, cfg)
        label = f'range-{base[:8]}-{source[:8]}'
    else:
        if not land.SLUG_RE.match(target_arg):
            raise ShipError(f'invalid slug {target_arg!r}', common.EXIT_USAGE)
        slug = label = target_arg
        number, landing = _pick_landing(main_root, slug, landing_no)
        base, source = landing.get('from'), landing.get('to')
        if not (isinstance(base, str) and COMMIT_RE.match(base) and isinstance(source, str)
                and COMMIT_RE.match(source)):
            raise ShipError(f'landing {number} of {slug} has no exact from/to range in the ledger')
        base, source = _commit(main_root, base), _commit(main_root, source)
        target = landing.get('target') or _target(main_root, cfg)
    head = _commit(main_root, 'refs/heads/' + target)
    if base == source:
        raise ShipError(f'the range {base[:12]}..{source[:12]} is empty')
    if not _is_ancestor(main_root, base, source):
        raise ShipError(f'{base[:12]} is not an ancestor of {source[:12]}')
    if not _is_ancestor(main_root, source, head):
        raise ShipError(f'{source[:12]} is not on {target} (head {head[:12]}): only landed work ships')
    in_range = _rev_list(main_root, base, source)
    if landing is not None and sorted(in_range) != sorted(landing.get('commits') or []):
        raise ShipError(f'the ledger range of {slug} does not match the repository: '
                        f'{len(landing.get("commits") or [])} recorded, {len(in_range)} now in {base[:12]}..'
                        f'{source[:12]}')
    base_ref = base
    if baseline is not None:
        baseline_commit = _commit(main_root, baseline)
        if not _is_ancestor(main_root, baseline_commit, base):
            raise ShipError(f'the baseline {baseline_commit[:12]} is not an ancestor of the landing start '
                            f'{base[:12]}')
        base_ref = baseline_commit

    files, deleted = collect_files(main_root, base, source)
    manifest_paths = {e['path'] for e in files} | {e['path'] for e in deleted}
    owners = _owners(main_root)
    in_range_set = set(in_range)
    log_all = _log(main_root, base_ref, head)
    before_set = set(_rev_list(main_root, base_ref, base)) if base_ref != base else set()
    before = [c for c in log_all if c['sha'] in before_set]
    after = [c for c in log_all if c['sha'] not in in_range_set and c['sha'] not in before_set]

    def describe(c, where, shipped):
        return {'sha': c['sha'], 'subject': c['subject'], 'slugs': owners.get(c['sha'], []), 'where': where,
                'in_shipped_bytes': shipped, 'manifest_paths': sorted(set(c['paths']) & manifest_paths)}

    def own(sha):  # the slug's only when the ledger gives the commit to this slug alone
        return slug is not None and owners.get(sha) == [slug]

    commits, excluded, foreign = [], [], []
    for c in log_all:
        if c['sha'] not in in_range_set:
            continue
        mine = own(c['sha'])
        commits.append({'sha': c['sha'], 'subject': c['subject'], 'slugs': owners.get(c['sha'], [])})
        if slug is not None and not mine:
            item = describe(c, 'in_range', True)
            excluded.append(item)
            foreign.append(item)
        elif slug is None and not owners.get(c['sha']):
            foreign.append(describe(c, 'in_range', True))
    for c in before:
        if own(c['sha']):
            continue
        item = describe(c, 'before', bool(set(c['paths']) & manifest_paths))
        excluded.append(item)
        if item['in_shipped_bytes']:
            foreign.append(item)
    for c in after:
        if own(c['sha']):
            continue
        excluded.append(describe(c, 'after', False))
    flags = {'foreign_in_range': foreign, 'allow_foreign': bool(allow_foreign),
             'superseded_paths': _superseded(after, manifest_paths),
             'allow_superseded': bool(allow_superseded),
             'gitlinks': [e['path'] for e in files if e.get('gitlink')],
             'baseline': base_ref if baseline is not None else None,
             'ungated_paths': list((landing or {}).get('differs_from_gated') or [])}
    rc, autocrlf = common.git(main_root, 'config', '--get', 'core.autocrlf')
    rc2, eol = common.git(main_root, 'config', '--get', 'core.eol')
    manifest = {
        'schema': SCHEMA, 'repo': main_root, 'repo_key': projects.repo_key(main_root), 'target': target,
        'target_head': head, 'slug': slug, 'label': label, 'base_commit': base, 'source_commit': source,
        'created_at': common.iso(common.now()),
        'landing': None if landing is None else {
            'number': number, 'at': landing.get('at'), 'branch': landing.get('branch'),
            'worktree': landing.get('worktree'), 'gated_commit': landing.get('gated_commit'),
            'mode': landing.get('mode')},
        'filters': {'attr_source': source, 'core.autocrlf': autocrlf.strip() if rc == 0 else None,
                    'core.eol': eol.strip() if rc2 == 0 else None},
        'files': files, 'deleted': deleted, 'commits': commits, 'excluded_commits': excluded, 'flags': flags,
    }
    return manifest


def plan(target_arg: str, repo: str | None = None, landing_no: int | None = None, baseline: str | None = None,
         allow_foreign: bool = False, out: str | None = None, allow_superseded: bool = False) -> dict:
    """Build, check and write a manifest. Refused (nothing written) when foreign work is inside the bytes,
    or when later commits changed manifest paths again (shipping would put older bytes over theirs),
    unless the matching --allow-* flag is given."""
    manifest = build_manifest(target_arg, repo, landing_no, baseline, allow_foreign, allow_superseded)
    paths = Paths(manifest['repo'])
    foreign = manifest['flags']['foreign_in_range']
    superseded = manifest['flags']['superseded_paths']
    reasons, details = [], {}
    if foreign and not allow_foreign:
        reasons.append(f'{len(foreign)} commit(s) inside the shipped bytes are not '
                       + (f"{manifest['slug']}'s" if manifest['slug'] else 'recorded by any landing')
                       + ': ' + ', '.join(f"{c['sha'][:12]} {common.clip(c['subject'], 60)}"
                                          f" ({','.join(c['slugs']) or 'no landing'})" for c in foreign[:10])
                       + '; review them, then plan again with --allow-foreign to ship them too')
        details['foreign_in_range'] = foreign
    if superseded and not allow_superseded:
        reasons.append(f'{len(superseded)} path(s) were changed again on {manifest["target"]} after '
                       f'{manifest["source_commit"][:12]}, so these bytes are older than the target\'s: '
                       + describe_superseded(superseded)
                       + '; ship the newer landing, or plan again with --allow-superseded to ship the older bytes')
        details['superseded_paths'] = superseded
    if reasons:
        _append_ledger(paths, {'event': 'plan-refused', 'slug': manifest['slug'], 'label': manifest['label'],
                               'base_commit': manifest['base_commit'], 'source_commit': manifest['source_commit'],
                               'foreign': [c['sha'] for c in details.get('foreign_in_range', [])],
                               'superseded': [s['path'] for s in details.get('superseded_paths', [])]})
        raise ShipError(' AND '.join(reasons), details=details)
    data = manifest_bytes(manifest)
    if out:
        path = os.path.abspath(out)
        _write_bytes(path, data)
    else:
        os.makedirs(paths.dir, exist_ok=True)
        stem = os.path.join(paths.dir, f"{manifest['label']}-{common.stamp()}")
        for n in range(1, 1000):
            path = stem + ('.json' if n == 1 else f'-{n}.json')
            try:
                _write_bytes(path, data, exclusive=True)
                break
            except FileExistsError:
                continue
        else:
            raise ShipError(f'cannot find a free manifest name at {stem}', common.EXIT_FAILED)
    sha = hashlib.sha256(data).hexdigest()
    _append_ledger(paths, {'event': 'planned', 'slug': manifest['slug'], 'label': manifest['label'],
                           'manifest': path, 'manifest_sha256': sha, 'base_commit': manifest['base_commit'],
                           'source_commit': manifest['source_commit'], 'files': len(manifest['files']),
                           'deleted': len(manifest['deleted']), 'allow_foreign': bool(allow_foreign),
                           'allow_superseded': bool(allow_superseded),
                           'superseded': [s['path'] for s in superseded]})
    return {'manifest': path, 'sha256': sha, 'summary': _summary(manifest)}


def _summary(m: dict) -> dict:
    flags = m['flags']
    return {'slug': m['slug'], 'label': m['label'], 'target': m['target'], 'base_commit': m['base_commit'],
            'source_commit': m['source_commit'], 'files': len(m['files']), 'deleted': len(m['deleted']),
            'commits': len(m['commits']), 'excluded_commits': len(m['excluded_commits']),
            'foreign_in_range': len(flags['foreign_in_range']), 'allow_foreign': flags['allow_foreign'],
            'superseded_paths': [s['path'] for s in flags['superseded_paths']],
            'superseded': describe_superseded(flags['superseded_paths']) if flags['superseded_paths'] else '',
            'allow_superseded': bool(flags.get('allow_superseded')), 'gitlinks': flags['gitlinks'],
            'ungated_paths': flags['ungated_paths']}


# ---------------------------------------------------------------- run

def load_manifest(path: str) -> tuple:
    """(bytes, sha256, manifest) for a manifest file, or ShipError."""
    try:
        size = os.path.getsize(path)
        if size > MAX_MANIFEST_BYTES:
            raise ShipError(f'{path} is larger than {MAX_MANIFEST_BYTES} bytes; not a manifest', common.EXIT_USAGE)
        with open(path, 'rb') as handle:
            data = handle.read()
    except OSError as e:
        raise ShipError(f'cannot read the manifest {path}: {e}', common.EXIT_USAGE)
    def no_duplicates(pairs):
        keys = [k for k, _ in pairs]
        if len(keys) != len(set(keys)):
            raise ValueError('duplicate key ' + repr(next(k for k in keys if keys.count(k) > 1)))
        return dict(pairs)

    def no_constant(name):
        raise ValueError(name + ' is not JSON')

    try:
        m = json.loads(data.decode('utf-8'), object_pairs_hook=no_duplicates, parse_constant=no_constant)
    except (UnicodeError, ValueError) as e:
        raise ShipError(f'{path} is not JSON: {e}', common.EXIT_USAGE)
    # The ship command reads these same bytes with its own JSON parser. Accept only the exact form
    # `plan` writes, so no reader can see a different manifest from the one verified here.
    if isinstance(m, dict) and data != manifest_bytes(m):
        raise ShipError(f'{path} is not in the canonical form twinlane writes; plan again', common.EXIT_USAGE)
    if not isinstance(m, dict) or m.get('schema') != SCHEMA:
        raise ShipError(f'{path} is not a {SCHEMA} manifest', common.EXIT_USAGE)
    for key in ('base_commit', 'source_commit', 'target_head'):
        if not isinstance(m.get(key), str) or not COMMIT_RE.match(m[key]):
            raise ShipError(f'{path}: {key} is not a commit id', common.EXIT_USAGE)
    for key in ('repo', 'target', 'label'):
        if not isinstance(m.get(key), str) or not m[key]:
            raise ShipError(f'{path}: {key} is missing', common.EXIT_USAGE)
    if m.get('slug') is not None and not (isinstance(m['slug'], str) and land.SLUG_RE.match(m['slug'])):
        raise ShipError(f'{path}: slug is not a valid slug', common.EXIT_USAGE)
    for key in ('files', 'deleted', 'commits', 'excluded_commits'):
        if not isinstance(m.get(key), list):
            raise ShipError(f'{path}: {key} is not a list', common.EXIT_USAGE)
    if not isinstance(m.get('flags'), dict) or not isinstance(m['flags'].get('superseded_paths'), list):
        raise ShipError(f'{path}: flags are missing', common.EXIT_USAGE)
    return data, hashlib.sha256(data).hexdigest(), m


def verify_manifest(main_root: str, m: dict) -> dict:
    """Re-derive the manifest's files from git; ShipError unless they match exactly.

    Returns {'target_head', 'superseded_paths', 'new_superseded'} for the target as it is now;
    new_superseded holds only the (path, commit) pairs the manifest does not already record."""
    _require_git(main_root)
    base, source = m['base_commit'], m['source_commit']
    for sha in (base, source):
        _commit(main_root, sha)
    head = _commit(main_root, 'refs/heads/' + m['target'])
    if not _is_ancestor(main_root, source, head):
        raise ShipError(f"{source[:12]} is no longer on {m['target']} (head {head[:12]}); plan again")
    files, deleted = collect_files(main_root, base, source)
    if files != m['files'] or deleted != m['deleted']:
        want = {e['path']: e for e in files}
        have = {e.get('path') if isinstance(e, dict) else None: e for e in m['files']}
        differ = sorted(p for p in set(want) | set(have) if want.get(p) != have.get(p) and p is not None)
        raise ShipError('the manifest does not match the repository at ' + source[:12] + ': '
                        + (', '.join(differ[:10]) or 'deleted paths differ') + '; plan again',
                        details={'differ': differ})
    manifest_paths = {e['path'] for e in files} | {e['path'] for e in deleted}
    now = _superseded(_log(main_root, source, head), manifest_paths)
    known = set()
    for entry in m['flags']['superseded_paths']:
        if isinstance(entry, dict) and isinstance(entry.get('by'), list):
            known.update((entry.get('path'), sha) for sha in entry['by'])
    new = []
    for entry in now:
        pairs = [(sha, subject) for sha, subject in zip(entry['by'], entry['subjects'])
                 if (entry['path'], sha) not in known]
        if pairs:
            new.append({'path': entry['path'], 'by': [p[0] for p in pairs], 'subjects': [p[1] for p in pairs]})
    return {'target_head': head, 'superseded_paths': now, 'new_superseded': new}


def _values(m: dict, copy_path: str, main_root: str) -> dict:
    values = {'repo': main_root, 'target': m['target'], 'manifest': copy_path,
              'source_commit': m['source_commit'], 'base': m['base_commit']}
    if m.get('slug'):
        values['slug'] = m['slug']
    landing = m.get('landing') if isinstance(m.get('landing'), dict) else {}
    for key in ('branch', 'worktree'):
        if isinstance(landing.get(key), str) and landing[key]:
            values[key] = landing[key]
    return values


def _env(m: dict, sha: str, copy_path: str, main_root: str, mode: str) -> dict:
    env = dict(os.environ, TWINLANE_SHIP_MANIFEST=copy_path, TWINLANE_SHIP_SHA256=sha,
               TWINLANE_SHIP_SOURCE=m['source_commit'], TWINLANE_SHIP_BASE=m['base_commit'],
               TWINLANE_SHIP_TARGET=m['target'], TWINLANE_SHIP_REPO=main_root, TWINLANE_SHIP_MODE=mode)
    env['TWINLANE_SHIP_SLUG'] = m.get('slug') or ''
    return env


def _ensure_copy(paths: Paths, data: bytes, sha: str) -> str:
    """The private copy the commands receive; rewritten when missing or not these exact bytes."""
    copy = paths.copy(sha)
    try:
        current = _sha256_file(copy)
    except OSError:
        current = None
    if current != sha:
        _write_bytes(copy, data)
    return copy


def _run_command(paths: Paths, argv: list, what: str, label: str, env: dict, cwd: str,
                 timeout: float | None, interactive: bool, echo) -> dict:
    """Run argv, copying its output to a log and to `echo`. No shell."""
    os.makedirs(paths.logs, exist_ok=True)
    log = os.path.join(paths.logs, f'{common.stamp()}-{label}-{what}.log')
    started = time.monotonic()
    rc, timed_out = None, False
    with open(log, 'wb') as handle:
        handle.write((f'# twinlane ship {what}: ' + json.dumps(argv) + '\n').encode('utf-8'))
        handle.flush()
        try:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=None if interactive else subprocess.DEVNULL,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=common.no_window_flags())
        except OSError as e:
            handle.write(f'# could not start: {e}\n'.encode('utf-8'))
            return {'argv': argv, 'rc': 127, 'seconds': 0.0, 'log': log, 'error': str(e)}
        created = common.process_created(proc.pid)
        state = {'timed_out': False}

        def expire():
            state['timed_out'] = True
            common.kill_tree(proc.pid, created)

        timer = threading.Timer(timeout, expire) if timeout else None
        if timer:
            timer.start()
        try:
            for chunk in iter(lambda: proc.stdout.read1(65536) if hasattr(proc.stdout, 'read1')
                              else proc.stdout.read(65536), b''):
                handle.write(chunk)
                handle.flush()
                if echo is not None:
                    try:
                        echo.write(chunk.decode('utf-8', 'replace'))
                        echo.flush()
                    except (OSError, ValueError):
                        pass
            rc = proc.wait()
        finally:
            if timer:
                timer.cancel()
            proc.stdout.close()
        timed_out = state['timed_out']
        if timed_out:
            handle.write(f'\n# timed out after {timeout} s\n'.encode('utf-8'))
            rc = 124
    return {'argv': argv, 'rc': rc, 'seconds': round(time.monotonic() - started, 3), 'log': log,
            'timed_out': timed_out}


def _read_receipt(paths: Paths, sha: str):
    path = paths.receipt(sha)
    if not os.path.exists(path):
        return None
    data = common.read_json(path, _MISSING)
    return data if isinstance(data, dict) else {'invalid': True}


def _check_receipt(receipt, sha: str, ship_argv: list, dry_argv, max_age_s: float) -> None:
    """ShipError unless the receipt is a passing, fresh, unused dry run of this manifest and command."""
    hint = 'run `twinlane ship run --manifest ...` without --execute first'
    if receipt is None:
        raise ShipError(f'no dry run of manifest {sha[:8]} on record; {hint}')
    if receipt.get('invalid') or receipt.get('schema') != RECEIPT_SCHEMA:
        raise ShipError(f'the dry-run receipt of {sha[:8]} cannot be read; {hint}')
    if receipt.get('manifest_sha256') != sha:
        raise ShipError(f'the dry-run receipt is for another manifest; {hint}')
    if receipt.get('rc') != 0:
        raise ShipError(f"the dry run of {sha[:8]} failed (rc {receipt.get('rc')}); fix it and dry-run again")
    if receipt.get('consumed_at'):
        raise ShipError(f"the dry run of {sha[:8]} was already used by an execute at {receipt['consumed_at']}; "
                        'a dry run is good for one execute: dry-run again')
    at = common.parse_time(receipt.get('at'))
    if at is None:
        raise ShipError(f'the dry-run receipt of {sha[:8]} has no time; {hint}')
    age = (common.now() - at).total_seconds()
    if age < -FUTURE_SKEW_S:
        raise ShipError(f"the dry-run receipt of {sha[:8]} is dated in the future ({receipt.get('at')}); {hint}")
    if age > max_age_s:
        raise ShipError(f'the dry run of {sha[:8]} is {int(age)} s old, older than ship.dry_run_max_age_s '
                        f'({int(max_age_s)} s); dry-run again')
    if receipt.get('ship_argv') != ship_argv or receipt.get('argv') != dry_argv:
        raise ShipError('the ship commands changed since the dry run; dry-run again')


def _confirmed(confirm: str | None, sha: str, ask) -> bool:
    if confirm is None and ask is not None:
        confirm = ask(f'Type {sha[:8]} (the first 8 hex digits of the manifest sha256) to ship: ')
    return isinstance(confirm, str) and confirm.strip().lower() == sha[:8]


def _interactive() -> bool:
    try:
        return sys.stdin.isatty() and sys.stdout.isatty()
    except (AttributeError, ValueError):
        return False


def run(manifest_path: str, slug: str | None = None, repo: str | None = None, execute: bool = False,
        confirm: str | None = None, echo=None, ask=None) -> tuple:
    """(exit code, result). Never runs ship.command without execute=True and every check passing."""
    if projects.in_worker():
        raise ShipError('ship run is refused inside a twinlane worker job; run it from the lead session')
    data, sha, m = load_manifest(manifest_path)
    if slug is not None and slug != m.get('slug'):
        raise ShipError(f"the manifest is for {m.get('slug') or m['label']!r}, not {slug!r}", common.EXIT_USAGE)
    main_root, cfg = _project(repo or m['repo'])
    if not (common.canon_path(main_root) == common.canon_path(m['repo'])):
        raise ShipError(f"the manifest is for {m['repo']}, not {main_root}")
    paths = Paths(main_root)
    copy_path = paths.copy(sha)
    ship_cfg = cfg['ship']
    values = _values(m, copy_path, main_root)
    try:
        ship_argv = projects.argv(ship_cfg['command'], **values) if ship_cfg.get('command') else None
        dry_argv = projects.argv(ship_cfg['dry_run_command'], **values) if ship_cfg.get('dry_run_command') else None
    except projects.ProjectError as e:
        raise ShipError(f'ship command: {e}', common.EXIT_USAGE)
    result = {'manifest': os.path.abspath(manifest_path), 'sha256': sha, 'repo': main_root,
              'ship_argv': ship_argv, 'dry_run_argv': dry_argv, 'executed': False}
    if ship_argv is None:
        result.update(configured=False, reason='no ship.command is configured for this project')
        return common.EXIT_REFUSED, result
    if not execute:
        return _dry_run(paths, m, data, sha, ship_argv, dry_argv, result, echo)
    return _execute(paths, m, data, sha, manifest_path, ship_argv, dry_argv, ship_cfg, result, confirm, echo, ask)


def superseded_problem(m: dict, check: dict) -> str | None:
    """Why --execute must refuse over superseded paths, or None.

    Any supersession the manifest does not record refuses: --allow-superseded at plan time covers
    only the commits that manifest lists. Without that flag, any supersession at all refuses."""
    if check['new_superseded']:
        return ('commits on ' + m['target'] + ' changed manifest paths again after the plan: '
                + describe_superseded(check['new_superseded'])
                + '; shipping would put older bytes over theirs: plan again')
    if check['superseded_paths'] and m['flags'].get('allow_superseded') is not True:
        return ('manifest paths were changed again on ' + m['target'] + ' after the source commit: '
                + describe_superseded(check['superseded_paths'])
                + '; plan again (with --allow-superseded to ship the older bytes)')
    return None


def _dry_run(paths, m, data, sha, ship_argv, dry_argv, result, echo) -> tuple:
    check = verify_manifest(paths.main_root, m)
    result.update(superseded_paths=check['superseded_paths'], new_superseded=check['new_superseded'],
                  superseded_problem=superseded_problem(m, check))
    if dry_argv is None:
        result.update(configured=True, reason='no ship.dry_run_command is configured, so --execute will refuse')
        return common.EXIT_REFUSED, result
    copy = _ensure_copy(paths, data, sha)
    cmd = _run_command(paths, dry_argv, 'dryrun', m['label'], _env(m, sha, copy, paths.main_root, 'dry-run'),
                       paths.main_root, DRY_RUN_TIMEOUT_S, False, echo)
    receipt = {'schema': RECEIPT_SCHEMA, 'manifest_sha256': sha, 'manifest': result['manifest'],
               'manifest_copy': copy, 'repo': paths.main_root, 'source_commit': m['source_commit'],
               'argv': dry_argv, 'ship_argv': ship_argv, 'rc': cmd['rc'], 'seconds': cmd['seconds'],
               'log': cmd['log'], 'at': common.iso(common.now()), 'target_head': check['target_head'],
               'allow_superseded': m['flags'].get('allow_superseded') is True,
               'superseded_paths': check['superseded_paths'], 'new_superseded': check['new_superseded']}
    with paths.locked():
        common.write_json(paths.receipt(sha), receipt)
    _append_ledger(paths, {'event': 'dry-run', 'slug': m.get('slug'), 'label': m['label'],
                           'manifest_sha256': sha, 'argv': dry_argv, 'rc': cmd['rc'], 'log': cmd['log']})
    try:  # a readable copy next to the manifest; the authoritative receipt is the one in the state dir
        common.write_json(result['manifest'] + '.dryrun.json', receipt)
    except OSError:
        pass
    result.update(dry_run=cmd, receipt=paths.receipt(sha))
    return (common.EXIT_OK if cmd['rc'] == 0 else common.EXIT_FAILED), result


def _execute(paths, m, data, sha, manifest_path, ship_argv, dry_argv, ship_cfg, result, confirm, echo, ask) -> tuple:
    def refuse(message, code=common.EXIT_REFUSED):
        try:
            _append_ledger(paths, {'event': 'execute-refused', 'slug': m.get('slug'), 'label': m['label'],
                                   'manifest_sha256': sha, 'reason': message})
        except (OSError, TimeoutError):
            pass
        raise ShipError(message, code)

    if not _confirmed(confirm, sha, ask):
        refuse(f'--execute needs --confirm {sha[:8]} (the first 8 hex digits of the manifest sha256 {sha})')
    try:
        execute_lock = common.file_lock(paths.execute_lock, EXECUTE_LOCK_TIMEOUT_S)
        execute_lock.__enter__()
    except TimeoutError:
        raise ShipError('another ship --execute is running for this project', common.EXIT_RUNNING)
    try:
        with paths.locked():
            receipt = _read_receipt(paths, sha)
        try:
            _check_receipt(receipt, sha, ship_argv, dry_argv, ship_cfg['dry_run_max_age_s'])
            check = verify_manifest(paths.main_root, m)
        except ShipError as e:
            refuse(str(e))
        problem = superseded_problem(m, check)
        if problem:
            refuse(problem)
        copy = _ensure_copy(paths, data, sha)
        with paths.locked():
            receipt = _read_receipt(paths, sha)
            try:
                _check_receipt(receipt, sha, ship_argv, dry_argv, ship_cfg['dry_run_max_age_s'])
            except ShipError as e:
                refuse(str(e))
            receipt.update(consumed_at=common.iso(common.now()), consumed_by={'pid': os.getpid()})
            common.write_json(paths.receipt(sha), receipt)
        # The last step before the command: the bytes it will read are the bytes that were confirmed.
        for path in (copy, manifest_path):
            try:
                now_sha = _sha256_file(path)
            except OSError as e:
                refuse(f'cannot re-hash {path}: {e}')
            if now_sha != sha:
                refuse(f'{path} changed after it was checked (sha256 {now_sha[:8]}, confirmed {sha[:8]}); '
                       'nothing ran')
        _append_ledger(paths, {'event': 'executing', 'slug': m.get('slug'), 'label': m['label'],
                               'manifest_sha256': sha, 'argv': ship_argv})
        cmd = None
        try:
            cmd = _run_command(paths, ship_argv, 'execute', m['label'], _env(m, sha, copy, paths.main_root, 'execute'),
                               paths.main_root, None, True, echo)
        finally:
            _append_ledger(paths, {'event': 'executed', 'slug': m.get('slug'), 'label': m['label'],
                                   'manifest_sha256': sha, 'argv': ship_argv,
                                   'rc': None if cmd is None else cmd['rc'],
                                   'log': None if cmd is None else cmd['log'], 'interrupted': cmd is None})
    finally:
        execute_lock.__exit__(None, None, None)
    result.update(executed=True, execute=cmd)
    return (common.EXIT_OK if cmd['rc'] == 0 else common.EXIT_FAILED), result


# ---------------------------------------------------------------- cli

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane ship',
                                 description='release manifest for landed work; never ships by default')
    sub = ap.add_subparsers(dest='cmd')
    p = sub.add_parser('plan', help='write a manifest for a landing or a FROM..TO range')
    p.add_argument('target', metavar='SLUG|FROM..TO')
    p.add_argument('--landing', type=int, help='which landing of SLUG: 1 the first, -1 the last (default)')
    p.add_argument('--baseline', help='the commit already shipped; work between it and the landing is checked')
    p.add_argument('--allow-foreign', action='store_true', help='ship commits that are not the slug\'s too')
    p.add_argument('--allow-superseded', action='store_true',
                   help='ship paths that later commits changed again (older bytes over newer)')
    p.add_argument('--out', help='write the manifest here instead of the state directory')
    p.add_argument('--repo')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('run', help='dry-run the ship command; --execute --confirm SHA8 ships')
    p.add_argument('slug', nargs='?')
    p.add_argument('--manifest', required=True)
    p.add_argument('--execute', action='store_true')
    p.add_argument('--confirm', metavar='SHA8')
    p.add_argument('--repo')
    p.add_argument('--json', action='store_true')
    return ap


def _human_plan(res: dict) -> str:
    s = res['summary']
    lines = [f"manifest {res['manifest']}", f"sha256   {res['sha256']}",
             f"{s['label']}: {s['base_commit'][:12]}..{s['source_commit'][:12]} on {s['target']}: "
             f"{s['files']} file(s), {s['deleted']} deleted, {s['commits']} commit(s); "
             f"{s['excluded_commits']} excluded commit(s) listed"]
    if s['foreign_in_range']:
        lines.append(f"FOREIGN: {s['foreign_in_range']} commit(s) of other work ship too (--allow-foreign)")
    if s['superseded_paths']:
        lines.append('SUPERSEDED (allowed by --allow-superseded): these bytes are older than the target\'s: '
                     + s['superseded'])
    if s['gitlinks']:
        lines.append('submodules (no bytes listed): ' + ', '.join(s['gitlinks'][:10]))
    if s['ungated_paths']:
        lines.append('changed by the land command, not gated: ' + ', '.join(s['ungated_paths'][:10]))
    lines.append(f"next: twinlane ship run --manifest \"{res['manifest']}\"   (dry run; ships nothing)")
    return '\n'.join(lines)


def _human_run(code: int, res: dict) -> str:
    sha8 = res['sha256'][:8]
    lines = [f"manifest {res['manifest']}", f"sha256   {res['sha256']}"]
    if res['ship_argv'] is None:
        lines.append('no ship.command is configured for this project: nothing ran. Ship the files the '
                     'manifest lists yourself.')
        return '\n'.join(lines)
    if res.get('executed'):
        cmd = res['execute']
        lines.append(f"ship command {'finished' if cmd['rc'] == 0 else 'FAILED (rc ' + str(cmd['rc']) + ')'}: "
                     + json.dumps(cmd['argv']))
        lines.append(f"log {cmd['log']}")
        return '\n'.join(lines)
    if res.get('superseded_problem'):
        lines.append('warning: ' + res['superseded_problem'] + '; --execute will refuse')
    elif res.get('superseded_paths'):
        lines.append('superseded, allowed by the plan\'s --allow-superseded: '
                     + describe_superseded(res['superseded_paths']))
    if res.get('dry_run'):
        cmd = res['dry_run']
        lines.append(f"dry run {'passed' if cmd['rc'] == 0 else 'FAILED (rc ' + str(cmd['rc']) + ')'}: "
                     + json.dumps(cmd['argv']) + f"  log {cmd['log']}")
    else:
        lines.append('no ship.dry_run_command is configured: --execute will refuse')
    lines.append('ship command (NOT run): ' + json.dumps(res['ship_argv']))
    if code == common.EXIT_OK:
        lines.append(f"to ship: twinlane ship run --manifest \"{res['manifest']}\" --execute --confirm {sha8}")
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
    if args.cmd == 'run':
        refused = projects.refuse_in_worker('ship run')
        if refused is not None:
            return refused
    try:
        if args.cmd == 'plan':
            res = plan(args.target, args.repo, args.landing, args.baseline, args.allow_foreign, args.out,
                       args.allow_superseded)
            common.emit(res, args.json, _human_plan(res))
            return common.EXIT_OK
        echo = sys.stderr if args.json else sys.stdout
        ask = None
        if args.execute and args.confirm is None and _interactive():
            ask = input
        code, res = run(args.manifest, args.slug, args.repo, args.execute, args.confirm, echo, ask)
        common.emit(res, args.json, _human_run(code, res))
        return code
    except ShipError as e:
        if args.json:
            common.emit({'ok': False, 'code': e.code, 'reason': str(e), 'details': e.details}, True)
        print(f'twinlane ship {args.cmd}: ' + ('refused' if e.code in (common.EXIT_REFUSED, common.EXIT_USAGE)
                                                else 'busy' if e.code == common.EXIT_RUNNING else 'failed')
              + f': {e}', file=sys.stderr)
        return e.code
    except TimeoutError as e:
        print(f'twinlane ship {args.cmd}: the state lock stayed busy ({e}); try again', file=sys.stderr)
        return common.EXIT_RUNNING
