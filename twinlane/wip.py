"""Global writer admission. One worktree is one lane, across engines and batons.

The FIFO stores requests, never executable prompts or approvals. Popping a request
does not launch anything; the caller must rerun admission with current authority.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import shlex
import subprocess
from contextlib import contextmanager

from . import common, observations


class WipError(ValueError):
    pass


def lock(timeout_s=30):
    return common.file_lock(os.path.join(common.STATE, 'wip.lock'), timeout_s)


def queue_path():
    return os.path.join(common.STATE, 'wip-queue.json')


def queue():
    path = queue_path()
    data = common.read_json(path, {'version': 1, 'queue': []})
    if not isinstance(data, dict) or data.get('version') != 1 or not isinstance(data.get('queue'), list) or any(
            not isinstance(x, dict) for x in data['queue']):
        raise WipError('writer queue unreadable; no admission or queue overwrite')
    if os.path.exists(path) and common.read_json(path) is None:
        raise WipError('writer queue unreadable; no admission or queue overwrite')
    return data['queue']


def save_queue(rows):
    common.write_json(queue_path(), {'version': 1, 'queue': rows})


CLAIMS_MAX_BYTES = 1 << 20  # provider stdout cap; claims for a handful of worktrees are tiny


def _run_provider(argv, timeout):
    """Return (stdout bytes, None) or (None, failure label), always within the timeout plus cleanup.

    No pipes: stdout goes to a temporary file and stderr is discarded, so a descendant that inherits the
    handles cannot stall a pipe drain while admission holds the WIP lock, and the read is capped in size.
    On timeout the provider's process tree is killed and the wait for it is bounded too."""
    import tempfile
    extra = {'creationflags': common.no_window_flags()} if common.IS_WIN else {'start_new_session': True}
    try:
        out = tempfile.TemporaryFile()
    except OSError:  # an unwritable temp dir: custody unknown, never a raw OSError out of board or status
        return None, 'spawn error'
    with out:
        try:
            proc = subprocess.Popen(argv, shell=False, stdin=subprocess.DEVNULL, stdout=out,
                                    stderr=subprocess.DEVNULL, **extra)
        except (OSError, ValueError, OverflowError):
            return None, 'spawn error'
        try:
            code = proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            common.kill_tree(proc.pid)  # still unreaped, so the pid cannot have been reused
            try:
                proc.kill()
                proc.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                pass
            return None, 'timeout'
        if code:
            return None, 'nonzero exit'
        try:
            out.seek(0)
            data = out.read(CLAIMS_MAX_BYTES + 1)
        except OSError:
            return None, 'read error'
    if len(data) > CLAIMS_MAX_BYTES:
        return None, 'output too large'
    return data, None


def claims(policy):
    """Read authoritative claims once per observation; None selects activity mode."""
    config = policy.get('wip') or {}
    argv = config.get('claims_command')
    if argv is None or argv == []:
        return None

    def failed(reason):
        # Reasons are fixed labels: provider output and argv may contain secrets.
        config_path = os.environ.get('TWINLANE_CONFIG') or os.path.join(common.data_home(), 'config.json')
        raise WipError(f'writer custody unknown: claims provider failed: {reason}; writer admission stays blocked '
                       f'until the provider is fixed or wip.claims_command is removed from {config_path}') from None

    if not isinstance(argv, list) or not all(isinstance(x, str) for x in argv):
        failed('invalid command')
    timeout = config.get('claims_timeout_s', 10)
    if type(timeout) not in (int, float) or (type(timeout) is float and not math.isfinite(timeout)) or timeout <= 0:
        failed('invalid timeout')

    def read():
        stdout, reason = _run_provider(argv, timeout)
        if reason:
            failed(reason)
        try:
            data = json.loads(stdout)
        except (ValueError, UnicodeError, RecursionError):
            failed('invalid JSON')
        if not isinstance(data, dict) or type(data.get('version')) is not int or data['version'] != 1:
            failed('unsupported version')
        rows = data.get('claims')
        if not isinstance(rows, list):
            failed('invalid claims')
        for row in rows:
            if not isinstance(row, dict) or not isinstance(row.get('worktree'), str) \
                    or not isinstance(row.get('id'), str) or 'age_s' not in row:
                failed('invalid claim')
            path, age = row['worktree'], row['age_s']
            if not os.path.isabs(path) or '\x00' in path:
                failed('non-absolute worktree')
            if age is not None and (type(age) not in (int, float) or (type(age) is float and not math.isfinite(age))):
                failed('invalid claim age')
        return rows

    return observations.once(('writer-claims', tuple(argv), timeout), read)


def _finished_writer(job_id):
    """Terminal receipt and no live or unknown engine (the writer-dir admission rule)."""
    from . import jobs
    if not job_id:
        return False
    receipt = common.read_json(jobs._p(job_id, 'result.json'))
    return isinstance(receipt, dict) and receipt.get('status') in jobs.TERMINAL and not jobs._writer_job_busy(job_id)


@observations.command
def lanes_from(job_rows, baton_rows, session_rows, policy=None):
    policy = policy or common.load_policy()
    claim_rows = claims(policy)
    merged = {}
    def add(path, kind, value, age=None, unknown=None):
        if not path and not unknown:
            return
        key = common.writer_key(path) if path else unknown
        if path and not (policy.get('wip') or {}).get('count_main_checkout', True):
            top = observations.worktree_top(path)
            if top and os.path.isdir(os.path.join(top, '.git')):
                return
        row = merged.setdefault(key, {'key': key, 'worktree': key, 'kinds': [], 'jobs': [],
                                      'batons': [], 'sessions': [], 'claims': [], 'age_s': None, 'sources': []})
        if kind not in row['kinds']:
            row['kinds'].append(kind)
        row[{'job': 'jobs', 'baton': 'batons', 'session': 'sessions', 'claim': 'claims'}[kind]].append(value)
        source = f"session:{value['engine']}:{value['id']}" if kind == 'session' else \
            f"claim:{value['id']}" if kind == 'claim' else f'{kind}:{value}'
        row['sources'].append(source)
        if age is not None:
            row['age_s'] = min(row['age_s'], age) if row['age_s'] is not None else age
    from . import jobs
    for r in job_rows:
        # Writer jobs as admission classifies them. A just-admitted job has no pid yet and must count;
        # only a terminal receipt whose engine is provably gone releases the lane.
        if r.get('status') in jobs.TERMINAL or r.get('profile') == 'intel' and not r.get('write') \
                or _finished_writer(r.get('id')):
            continue
        add(r.get('writer_root') or r.get('cwd') or r.get('worktree'), 'job', r.get('id'), r.get('age_s'),
            unknown='job:' + str(r.get('id')))
    if claim_rows is not None:
        for r in claim_rows:
            add(r['worktree'], 'claim', {'id': r['id'], 'age_s': r['age_s']}, r['age_s'])
        baton_rows, session_rows = [], []
    for r in baton_rows:
        path = r.get('worktree') or (r.get('repo') or {}).get('worktree')
        if r.get('state', 'active') in ('active', 'review') and path and os.path.isdir(path):
            add(path, 'baton', r.get('slug'), r.get('age_s'))
    active_s = (policy.get('wip') or {}).get('session_active_s', 1800)
    parked = {common.writer_key(r.get('worktree') or (r.get('repo') or {}).get('worktree'))
              for r in baton_rows if r.get('state') == 'parked' and (r.get('worktree') or (r.get('repo') or {}).get('worktree'))}
    for r in session_rows:
        age = r.get('age_s')
        path = r.get('_writer_cwd') or r.get('cwd')
        if path and common.writer_key(path) in parked:
            continue
        if r.get('state') in ('job', 'parked', 'unknown'):
            continue
        if r.get('state') not in ('working', 'active', 'mid-turn') and (age is None or age > active_s):
            continue
        add(r.get('_writer_cwd') or r.get('cwd'), 'session',
            {'engine': r.get('engine'), 'id': r.get('id'), 'age_s': age}, age)
    for row in merged.values():
        row['kinds'].sort()
        row['age'] = common.fmt_age(row['age_s'])
    return sorted(merged.values(), key=lambda r: r['key'])


@observations.command
def collect(policy=None):
    from . import baton, jobs, sessions, lanes
    policy = policy or common.load_policy()
    claim_rows = claims(policy)
    active_s = (policy.get('wip') or {}).get('session_active_s', 1800)
    try:
        session_rows = [] if claim_rows is not None else sessions.collect_sessions(
            since_s=active_s, limit=0, include_jobs=False, raw_paths=True, policy=policy)['rows']
    except Exception as e:
        raise WipError('writer custody unknown: interactive sessions unreadable') from e
    job_rows, baton_rows = [], []
    for jid in observations.job_ids():
        try:
            status = jobs.job_status(jid)
        except Exception:
            status = 'unknown'
        if status in jobs.TERMINAL:
            continue
        spec = observations.job_json(jid)
        row = dict(spec) if isinstance(spec, dict) else {}
        row.update(id=jid, status=status)
        row['age_s'] = common.age_s(row.get('created_at'))
        job_rows.append(row)
    for path in ([] if claim_rows is not None else glob.glob(os.path.join(common.BATONS, '*.json'))):
        slug = os.path.basename(path)[:-5]
        if not baton.valid_slug(slug):
            continue
        try:
            b = baton.load(slug)
        except Exception as e:
            raise WipError(f'writer custody unknown: unreadable baton {slug}') from e
        if not b or not isinstance(b.get('repo'), dict):
            raise WipError(f'writer custody unknown: unreadable baton {slug}')
        if b.get('state', 'active') not in baton.STATES + ('stale',):
            raise WipError(f'writer custody unknown: malformed baton {slug}')
        baton_rows.append({'slug': slug, 'state': lanes.effective_state(b, policy, session_rows),
                           'worktree': b['repo'].get('worktree'), 'age_s': common.age_s(b.get('progress_at') or b.get('updated_at'))})
    return lanes_from(job_rows, baton_rows, session_rows, policy)


current_lanes = collect


def check(cwd, kind, ident, policy=None, argv=None, preserve=False, slots=1, new_worktree=False):
    """Call while holding lock(), and publish the admitted job/baton before release."""
    policy = policy or common.load_policy()
    maximum = (policy.get('wip') or {}).get('max_writers', 5)
    if type(maximum) is not int or maximum < 1:
        raise WipError('wip.max_writers must be a positive integer')
    if kind == 'baton' and (policy.get('wip') or {}).get('claims_command') not in (None, []):
        return  # claims mode: a baton holds no writer lane, so it needs no admission (and no provider run)
    lanes = collect(policy)
    # A not-yet-created nested checkout still resolves to its parent's writer key.
    # Creation always consumes new capacity, including every requested contestant.
    key = os.path.normcase(os.path.realpath(os.path.abspath(cwd))) if new_worktree else common.writer_key(cwd)
    if (not new_worktree and slots == 1 and any(x['worktree'] == key for x in lanes)) or len(lanes) + slots <= maximum:
        return
    pending = queue()
    import json
    request_id = hashlib.sha256(json.dumps([kind, key, argv or ident], sort_keys=True).encode()).hexdigest()[:16]
    if not preserve and not any(x.get('id') == request_id for x in pending):
        pending.append({'id': request_id, 'kind': kind, 'name': ident, 'worktree': key,
                        'queued_at': common.iso(common.now()), 'argv': list(argv or (['baton', 'new', ident, '--goal', '<goal>', '--cd', key] if kind == 'baton' else ['run', '--write', '--cd', key, '--brief', '<brief>']))})
        save_queue(pending)
    raise WipError(f'queued: {len(lanes)}/{maximum} writer lanes busy; twinlane wip next')


@contextmanager
def admission(cwd, kind, ident, policy=None, argv=None, preserve=False, new_worktree=False):
    with lock():
        check(cwd, kind, ident, policy, argv=argv, preserve=preserve, new_worktree=new_worktree)
        yield


@observations.command
def cli(argv):
    from . import baton
    ap = argparse.ArgumentParser(prog='twinlane wip')
    ap.add_argument('action', nargs='?', choices=['next'])
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--force', action='store_true')
    a = ap.parse_args(argv)
    try:
        if a.action == 'next':
            from . import projects
            refused = projects.refuse_in_worker('wip next')
            if refused is not None:
                return refused
            with lock():
                pending = queue()
                policy = common.load_policy()
                busy = len(collect(policy))
                maximum = (policy.get('wip') or {}).get('max_writers', 5)
                if busy >= maximum and not a.force:
                    raise WipError(f'queued: {busy}/{maximum} writer lanes busy; queue unchanged')
                row = pending.pop(0) if pending else None
                if row:
                    save_queue(pending)
                    row['command'] = shlex.join(['twinlane'] + row.get('argv', []))
            common.emit(baton.redact_obj(row), a.json,
                        baton.redact(f"next: {row['command']}; rerun admission; nothing launched") if row else 'queue empty')
        else:
            policy = common.load_policy()
            rows, pending = collect(policy), queue()
            rule = 'running write jobs + claims count' if claims(policy) is not None else 'sessions active within 30m count'
            common.emit(baton.redact_obj({'lanes': rows, 'queue': pending}), a.json,
                        baton.redact('\n'.join([f"wip: {len(rows)}/{(policy.get('wip') or {}).get('max_writers', 5)} writer lanes busy (policy wip.max_writers; {rule})"] +
                            [f"  {'+'.join(r['kinds']):<16} {','.join(r['batons']) or '-'} {','.join(str(j) for j in r['jobs']) or '-'} {len(r['sessions'])} sessions  {r['age']}  {r['worktree']}" for r in rows] +
                            [f"queued {r['id']} {r.get('name')} {r.get('worktree')}" for r in pending])))
    except (WipError, TimeoutError) as e:
        print(baton.redact(str(e)))
        return common.EXIT_REFUSED
    return common.EXIT_OK
