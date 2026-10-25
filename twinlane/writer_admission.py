"""twinlane explain-writer-admission: why a code job in DIR would (not) pass the writer check, read-only.

The real check is jobs._claim_writer, run by the job's own runner under state/writers.lock with its own
process token. This module only explains it, in three separate steps:

  capture_snapshot  reads the cwd's reservation and every job folder twice (before and after the process
                    observations), with no lock, no write, no delete and no directory creation. A difference
                    between the two reads, a scan failure or an unreadable relevant file makes the answer unknown.
                    Only identities the check can consult are observed: the reservation owner and its job's engine
                    evidence, and the runner/engine evidence of same-cwd code jobs other than --job-id.
  evaluate          a pure function of that snapshot (and the observations captured in it): no filesystem,
                    process or native call. It mirrors _claim_writer's rules, including legacy prelaunch jobs.
                    An observation counts only when it names exactly the pid and stamp that was asked for.
  cli               `explain-writer-admission --cwd DIR [--job-id ID] [--json]`.

The answer is a proposal, never a grant: `blocked`, `unknown` or `no-observed-blocker`. It never says
"allowed". A proven-dead child-starting marker is reported unknown, because the claimant must delete it and
that delete can fail; a receipt never outweighs a live child or survivor. A qualified child exit and a DEAD
runner can resolve an unreadable child creation stamp as PID reuse. The observations
are made by the current process only, so the claimant (with its own token) can see more or less. Output is
allowlisted: no briefs, prompts, payloads, environment or tokens, and a job-folder name that is not a plain
id is printed as a stable placeholder, never raw.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time

from . import common, jobs

SCHEMA = 'twinlane.writer-admission-explain/1'
BLOCKED, UNKNOWN, NO_OBSERVED_BLOCKER = 'blocked', 'unknown', 'no-observed-blocker'
_RANK = {None: 0, UNKNOWN: 1, BLOCKED: 2}

# File names as jobs.py writes them (tests check they stay equal).
SURVIVOR, CHILD_STARTING, RUNNER_LOCK, RUNNER_PID = 'survivor.json', 'child-starting.json', 'runner.lock', 'runner.pid'
TREE_UNRESOLVED = jobs.TREE_UNRESOLVED
JOB_FILES = ('job.json', 'status.json', 'result.json', SURVIVOR, CHILD_STARTING, RUNNER_PID, RUNNER_LOCK,
             TREE_UNRESOLVED)
PRESENCE_MARKERS = ('status.json', RUNNER_PID, RUNNER_LOCK, SURVIVOR, CHILD_STARTING, TREE_UNRESOLVED)
KNOWN_STATUSES = ('queued', 'running', 'done', 'failed', 'timeout', 'limit_hit', 'refused', 'stopped', 'lost',
                  'interrupted')
READ_RETRIES = 8
MAX_FILE_BYTES = 64 * 1024 * 1024
PID_DIGITS, STAMP_DIGITS = 10, 20  # 2**32-1 and 2**64-1: longer runner.pid tokens are malformed, never int()-ed
_SAFE_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}')
_WORD = re.compile(r'[A-Za-z0-9_.-]{1,64}')
_DECIMAL = re.compile(r'[0-9]{1,20}')

NOTES = (
    'proposal only: this report never grants, reserves or records writer admission',
    'the real claimant re-checks everything under writers.lock with its own current process token',
    'process observations are by this process only and never carry over to a later admission',
    'a proven-dead child-starting marker stays unknown: the claimant must delete it, and that can fail',
)


# ---------------------------------------------------------------- canonical paths (same as jobs._writer_cwd)

def writer_cwd(cwd: str) -> str:
    return common.writer_key(cwd)


def reservation_name(canonical_cwd: str) -> str:
    return hashlib.sha256(canonical_cwd.encode('utf-8')).hexdigest() + '.json'


# ---------------------------------------------------------------- snapshot (read-only IO)

def _read_file(path: str) -> dict:
    """{'state': absent|ok|unreadable, 'sha256', 'size', 'error', 'data'}. Only FileNotFoundError is absence;
    a busy file (Windows replace) is retried like common.read_json, then unreadable."""
    for i in range(READ_RETRIES):
        try:
            with open(path, 'rb') as h:
                data = h.read(MAX_FILE_BYTES + 1)
        except FileNotFoundError:
            return {'state': 'absent', 'sha256': None, 'size': None, 'error': None, 'data': None}
        except PermissionError as e:
            if i + 1 < READ_RETRIES:
                time.sleep(0.01 * (i + 1))
                continue
            return {'state': 'unreadable', 'sha256': None, 'size': None, 'error': type(e).__name__, 'data': None}
        except OSError as e:
            return {'state': 'unreadable', 'sha256': None, 'size': None, 'error': type(e).__name__, 'data': None}
        if len(data) > MAX_FILE_BYTES:
            return {'state': 'unreadable', 'sha256': None, 'size': None, 'error': 'too_large', 'data': None}
        return {'state': 'ok', 'sha256': hashlib.sha256(data).hexdigest(), 'size': len(data), 'error': None,
                'data': data}
    return {'state': 'unreadable', 'sha256': None, 'size': None, 'error': 'retries', 'data': None}


def _parse(record: dict, name: str) -> dict:
    """Attach the parsed value the way jobs.py would read it, then drop the raw bytes."""
    data = record.pop('data', None)
    record['json'] = None
    record['value'] = None
    if record['state'] != 'ok':
        return record
    if name == RUNNER_PID:  # jobs._read_text -> decode_text
        if data.startswith((b'\xff\xfe', b'\xfe\xff')):
            record['value'] = data.decode('utf-16', errors='replace')
        else:
            record['value'] = data.decode('utf-8-sig', errors='replace')
        return record
    try:  # common.read_json: UTF-8 text (a BOM, bad bytes or an over-long integer literal is invalid)
        record['value'] = json.loads(data.decode('utf-8'))
        record['json'] = 'ok'
    except ValueError:
        record['json'] = 'invalid'
    except RecursionError:
        # Valid JSON may exceed a supported decoder's nesting limit. It is unreadable evidence,
        # not proof of absence; unrelated files can still be skipped by the pure classifier.
        record['state'], record['error'] = 'unreadable', 'RecursionError'
    return record


def _read_pass(cwd: str, jobs_root: str, state_root: str) -> dict:
    snap = {'cwd': cwd, 'case_insensitive': os.path.normcase('A') == 'a', 'jobs_root': {'state': 'ok', 'error': None},
            'entries': {}}
    snap['reservation'] = _parse(_read_file(os.path.join(state_root, 'writers', reservation_name(cwd))), 'reservation')
    try:
        with os.scandir(jobs_root) as it:
            listing = list(it)
    except FileNotFoundError:
        snap['jobs_root'] = {'state': 'absent', 'error': None}
        listing = []
    except OSError as e:
        snap['jobs_root'] = {'state': 'unreadable', 'error': type(e).__name__}
        listing = []
    for entry in sorted(listing, key=lambda e: e.name):
        item = {'is_dir': None, 'error': None, 'files': {}, 'canonical_cwd': None, 'cwd_problem': None}
        snap['entries'][entry.name] = item
        try:
            item['is_dir'] = entry.is_dir()  # follows links, as _claim_writer's scan does
        except OSError as e:
            item['error'] = type(e).__name__
            continue
        if not item['is_dir']:
            continue
        for name in JOB_FILES:
            item['files'][name] = _parse(_read_file(os.path.join(entry.path, name)), name)
        spec = item['files']['job.json']
        if spec['json'] == 'ok' and isinstance(spec['value'], dict) and spec['value'].get('cwd'):
            raw = spec['value']['cwd']
            if not isinstance(raw, str):
                item['cwd_problem'] = 'not_a_string'
            else:
                try:
                    item['canonical_cwd'] = writer_cwd(raw)
                except (OSError, ValueError) as e:
                    item['cwd_problem'] = type(e).__name__
    return snap


def _digest(snap: dict) -> str:
    """Generation of one read: names, kinds, file hashes and canonical cwds (never content)."""
    def rec(r):
        return [r['state'], r['sha256'], r['size'], r['error']]
    shape = {'reservation': rec(snap['reservation']), 'jobs_root': snap['jobs_root'],
             'entries': {n: [e['is_dir'], e['error'], e['canonical_cwd'], e['cwd_problem'],
                             {f: rec(r) for f, r in sorted(e['files'].items())}]
                         for n, e in sorted(snap['entries'].items())}}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode('utf-8', 'surrogatepass')).hexdigest()


def _races(a: dict, b: dict) -> list[dict]:
    """What differs between two reads, as {'kind', 'entry', 'file'} (raw names: rendered safely on output)."""
    out = []
    if a['reservation']['sha256'] != b['reservation']['sha256'] or a['reservation']['state'] != b['reservation']['state']:
        out.append({'kind': 'reservation'})
    if a['jobs_root'] != b['jobs_root']:
        out.append({'kind': 'jobs_root'})
    for name in sorted(set(a['entries']) | set(b['entries'])):
        ea, eb = a['entries'].get(name), b['entries'].get(name)
        if ea is None or eb is None:
            out.append({'kind': 'entry', 'entry': name})
            continue
        if (ea['is_dir'], ea['error'], ea['canonical_cwd'], ea['cwd_problem']) != \
                (eb['is_dir'], eb['error'], eb['canonical_cwd'], eb['cwd_problem']):
            out.append({'kind': 'entry', 'entry': name})
        for f in sorted(set(ea['files']) | set(eb['files'])):
            ra, rb = ea['files'].get(f), eb['files'].get(f)
            if ra is None or rb is None or (ra['state'], ra['sha256'], ra['error']) != (rb['state'], rb['sha256'], rb['error']):
                out.append({'kind': 'file', 'entry': name, 'file': f})
    return out


def default_observer(pid, created) -> dict:
    return common.observe_process(pid, created)


def capture_snapshot(cwd: str, jobs_root: str, state_root: str, observer=None,
                     target_job_id: str | None = None) -> dict:
    """Read, observe every identity the check can consult in the first read, read again. Read-only: no lock,
    write or mkdir. `observer(pid, created)` defaults to common.observe_process (the current process token)
    and must echo the identity it observed (`pid`, `expected_created`); see _observed."""
    observer = observer or default_observer
    canonical = writer_cwd(cwd)
    first = _read_pass(canonical, jobs_root, state_root)
    first['target_job_id'] = target_job_id  # before deriving identities: the target's own evidence is not probed
    observations = {}
    for key, (pid, created) in required_identities(first).items():
        try:
            observations[key] = observer(pid, created)
        except Exception as e:  # noqa: BLE001 - an observer failure proves nothing
            observations[key] = {'pid': pid, 'expected_created': None if created is None else str(created),
                                 'state': common.UNKNOWN, 'reason': 'observer_raised', 'exception': type(e).__name__}
    second = _read_pass(canonical, jobs_root, state_root)
    races = _races(first, second)
    first.update(observations=observations, generation=_digest(first), second_generation=_digest(second),
                 races=races)
    first['stable'] = not races and first['generation'] == first['second_generation']
    return first


# ---------------------------------------------------------------- pure evaluation (no IO below this line)

def identity_key(pid, created) -> str:
    return f'{pid}:{"-" if created is None else created}'


def _no_stamp(created) -> bool:
    return created is None or created == ''  # pid_state treats '' as "no stamp"


def _identity(pid, created):
    """(pid, created-or-None) when strictly valid, else None. '' is no stamp, as in common.pid_state."""
    if not common._valid_pid(pid):
        return None
    if _no_stamp(created):
        return pid, None
    return (pid, created) if common._valid_stamp(created) else None


def display_id(name) -> str:
    """A job-folder name as printed: itself when it is a plain id, else a stable placeholder (never raw)."""
    safe = _safe_id(name)
    if safe is not None:
        return safe
    raw = name.encode('utf-8', 'surrogatepass') if isinstance(name, str) else repr(name).encode('utf-8', 'replace')
    return 'unsafe-' + hashlib.sha256(raw).hexdigest()[:16]


def _word(value):
    return value if isinstance(value, str) and _WORD.fullmatch(value) else None


def _printable(text: str) -> str:
    return ''.join(c if c.isprintable() else f'\\x{ord(c):02x}' if ord(c) < 256 else f'\\u{ord(c):04x}'
                   for c in str(text))


def _json(files: dict, name: str):
    """(kind, value) for a job file: absent | unreadable | invalid | ok (value may be JSON null)."""
    r = files.get(name) or {'state': 'absent'}
    if r['state'] != 'ok':
        return r['state'], None
    if name == RUNNER_PID:
        return 'ok', r.get('value')
    return ('ok', r['value']) if r.get('json') == 'ok' else ('invalid', None)


def _reservation_value(snap: dict):
    r = snap['reservation']
    if r['state'] != 'ok':
        return r['state'], None
    return ('ok', r['value']) if r.get('json') == 'ok' else ('invalid', None)


def _valid_owner(snap: dict):
    """The reservation as _claim_writer accepts it ({id, cwd, pid, created}), else None."""
    kind, owner = _reservation_value(snap)
    if kind != 'ok' or not isinstance(owner, dict) or owner.get('cwd') != snap['cwd'] \
            or not isinstance(owner.get('id'), str) or not isinstance(owner.get('pid'), int) \
            or not isinstance(owner.get('created'), int):
        return None
    return owner


def _runner_pid_of(files: dict):
    """jobs._runner_pid: ("<pid> <created>"; older files hold only the pid). Third item: problem or None.
    Tokens longer than a DWORD pid or a uint64 stamp are malformed and never converted (int() of thousands
    of digits raises)."""
    kind, text = _json(files, RUNNER_PID)
    if kind == 'absent':
        return None, None, None
    if kind != 'ok':
        return None, None, 'runner_pid_unreadable'
    parts = (text or '').split()
    if not parts or not parts[0].isdigit():
        return None, None, None
    if not parts[0].isascii() or len(parts[0]) > PID_DIGITS:
        return None, None, 'runner_pid_malformed'
    created = None
    if len(parts) > 1 and parts[1].isdigit():
        if not parts[1].isascii() or len(parts[1]) > STAMP_DIGITS:
            return None, None, 'runner_pid_malformed'
        try:
            created = int(parts[1])
        except ValueError:
            return None, None, 'runner_pid_malformed'
    try:
        pid = int(parts[0])
    except ValueError:
        return None, None, 'runner_pid_malformed'
    return pid, created, None


def _runner_identity(files: dict):
    """(pid, created, problem) as _claim_writer picks it: status.json's pid, else runner.pid."""
    kind, st = _json(files, 'status.json')
    st = st if kind == 'ok' and isinstance(st, dict) else None
    if (st or {}).get('pid'):
        return st['pid'], st.get('pid_created'), None
    return _runner_pid_of(files)


def _engine_identities(files: dict) -> list:
    """The child, launch marker, survivor, and runner needed to qualify a child exit."""
    out = []
    kind, st = _json(files, 'status.json')
    if kind == 'ok' and isinstance(st, dict) and st.get('child_pid') is not None:
        out.append((st.get('child_pid'), st.get('child_created')))
    kind, receipt = _json(files, 'result.json')
    if kind == 'ok' and jobs._receipt_observed_child_exit(receipt, st):
        pid, created, problem = _runner_identity(files)
        if not problem:
            out.append((pid, created))
    kind, pending = _json(files, CHILD_STARTING)
    if kind == 'ok' and isinstance(pending, dict) and common._valid_pid(pending.get('pid')) \
            and common._valid_stamp(pending.get('created')):
        out.append((pending['pid'], pending['created']))
    kind, sv = _json(files, SURVIVOR)
    if kind == 'ok' and isinstance(sv, dict) and isinstance(sv.get('pid'), int):
        out.append((sv['pid'], None if _no_stamp(sv.get('created')) else sv.get('created')))
    return out


def _classify(snap: dict):
    """Each job-folder entry, as _claim_writer's scan sees it: (raw name, entry, verdict, spec). Verdicts:
    scan_failed, not_a_folder, target, unpublished, spec_unreadable, spec_invalid, other, cwd_malformed, relevant."""
    target = snap.get('target_job_id')
    for name in sorted(snap['entries']):
        entry = snap['entries'][name]
        if entry.get('error'):
            yield name, entry, 'scan_failed', None
            continue
        if not entry.get('is_dir'):
            yield name, entry, 'not_a_folder', None
            continue
        if target is not None and name == target:
            yield name, entry, 'target', None
            continue
        files = entry['files']
        if files['job.json']['state'] == 'absent' and all(files[m]['state'] == 'absent' for m in PRESENCE_MARKERS):
            yield name, entry, 'unpublished', None
            continue
        kind, spec = _json(files, 'job.json')
        if kind == 'unreadable':
            yield name, entry, 'spec_unreadable', None
        elif kind != 'ok' or not isinstance(spec, dict):
            yield name, entry, 'spec_invalid', None
        elif spec.get('profile') != 'code' or not spec.get('cwd'):
            yield name, entry, 'other', spec
        elif entry.get('cwd_problem') or entry.get('canonical_cwd') is None:
            yield name, entry, 'cwd_malformed', spec
        elif entry['canonical_cwd'] != snap['cwd']:
            yield name, entry, 'other', spec
        else:
            yield name, entry, 'relevant', spec


def required_identities(snap: dict) -> dict:
    """The strictly valid identities the check can consult: {identity_key: (pid, created-or-None)}.

    The reservation owner and its job's engine evidence (whatever that job's cwd, and even when it is the
    target: the real reservation check looks it up by id), plus runner and engine evidence of same-cwd code
    jobs other than the target. Unrelated, unpublished or unreadable jobs are never probed; a malformed
    identity is never probed either (it is reported unknown)."""
    raw = []
    owner = _valid_owner(snap)
    if owner is not None:
        raw.append((owner['pid'], owner['created']))
        if _safe_id(owner['id']) is not None:
            entry = _entry_for(snap, owner['id'])
            if entry is not None and not entry.get('error'):
                raw.extend(_engine_identities(entry.get('files') or {}))
    for _name, entry, verdict, _spec in _classify(snap):
        if verdict != 'relevant':
            continue
        pid, created, problem = _runner_identity(entry['files'])
        if pid and not problem:
            raw.append((pid, created))
        raw.extend(_engine_identities(entry['files']))
    out = {}
    for pid, created in raw:
        ident = _identity(pid, created)
        if ident:
            out[identity_key(*ident)] = ident
    return out


class _Findings:
    def __init__(self):
        self.items = []

    def add(self, effect, reason, **extra):
        self.items.append(dict({'effect': effect, 'reason': reason}, **extra))

    def effect(self):
        return max((i['effect'] for i in self.items), key=lambda e: _RANK[e], default=None)


def _observation_problem(obs, ident) -> str | None:
    """Why an observation cannot be trusted for this identity, or None. It must be an object with a valid
    state that names exactly the requested pid (a real int) and stamp (an exact decimal string, or None for
    no stamp). A missing echoed identity is never filled in from the request."""
    if not isinstance(obs, dict):
        return 'observation_not_an_object'
    if obs.get('state') not in (common.ALIVE, common.DEAD, common.UNKNOWN):
        return 'observation_state_invalid'
    if 'pid' not in obs or 'expected_created' not in obs:
        return 'observation_identity_missing'
    if type(obs['pid']) is not int or obs['pid'] != ident[0]:
        return 'observation_identity_mismatch'
    expected = obs['expected_created']
    if ident[1] is None:
        return None if expected is None else 'observation_identity_mismatch'
    if not isinstance(expected, str) or not _DECIMAL.fullmatch(expected) or expected != str(ident[1]):
        return 'observation_identity_mismatch'
    return None


def _observed(snap: dict, pid, created):
    """(state, public observation) for a recorded identity. A malformed identity, a missing observation or one
    that does not echo exactly this identity is UNKNOWN."""
    ident = _identity(pid, created)
    if ident is None:
        return common.UNKNOWN, {'state': common.UNKNOWN, 'reason': 'invalid_identity',
                                'pid_problem': common._malformed(pid, common._valid_pid(pid)) or 'missing'}
    observations = snap.get('observations')
    observations = observations if isinstance(observations, dict) else {}
    key = identity_key(*ident)
    obs = observations.get(key)
    problem = _observation_problem(obs, ident) if key in observations else 'not_observed'
    if problem:
        return common.UNKNOWN, {'pid': ident[0], 'expected_created': None if ident[1] is None else str(ident[1]),
                                'state': common.UNKNOWN, 'reason': problem}
    return obs['state'], _public_observation(obs, ident)


def _public_observation(obs: dict, ident) -> dict:
    out = {'pid': ident[0], 'expected_created': None if ident[1] is None else str(ident[1]),
           'state': obs['state'], 'reason': _word(obs.get('reason'))}
    actual = obs.get('actual_created')
    if 'actual_created' in obs:
        out['actual_created'] = actual if isinstance(actual, str) and _DECIMAL.fullmatch(actual) else None
    if 'exists' in obs and obs['exists'] in (True, False, None):
        out['exists'] = obs['exists']
    for k in ('observer', 'platform'):
        if k in obs:
            out[k] = _word(obs[k])
    stages = obs.get('stages')
    if isinstance(stages, list):
        clean = []
        for s in stages:
            if not isinstance(s, dict):
                continue
            item = {}
            for k in ('stage', 'exception'):
                if k in s:
                    item[k] = _word(s[k])
            for k in ('attempted', 'ok'):
                if k in s:
                    item[k] = s[k] if isinstance(s[k], bool) else None
            for k in ('last_error', 'exit_code'):
                if k in s:
                    v = s[k]
                    item[k] = v if (type(v) is int and 0 <= v <= 2 ** 32) else _word(v)
            clean.append(item)
        out['stages'] = clean
    return out


def _state_effect(state: str):
    return BLOCKED if state == common.ALIVE else UNKNOWN if state == common.UNKNOWN else None


def _entry_for(snap: dict, job_id: str):
    entries = snap['entries']
    if job_id in entries:
        return entries[job_id]
    if snap.get('case_insensitive'):
        for name, entry in entries.items():
            if name.lower() == job_id.lower():
                return entry
    return None


def _busy(snap: dict, job_id: str, entry: dict | None) -> tuple:
    """jobs._writer_job_busy, including the qualified post-exit PID-reuse exception.
    Returns (_Findings, detail) where detail is the allowlisted per-process evidence."""
    f = _Findings()
    detail = {}
    files = (entry or {}).get('files') or {}
    if entry is not None and entry.get('error'):
        f.add(UNKNOWN, 'job_folder_unreadable')
    kind, st = _json(files, 'status.json')
    if kind == 'unreadable':
        f.add(UNKNOWN, 'status_unreadable')
    elif kind == 'invalid' or (kind == 'ok' and st is None):
        f.add(BLOCKED, 'status_invalid')
    elif kind == 'ok' and not isinstance(st, dict):
        f.add(BLOCKED, 'status_not_an_object')
    st = st if isinstance(st, dict) else {}
    states = []
    tree_kind, _ = _json(files, TREE_UNRESOLVED)
    pending_kind, pending_tree = _json(files, CHILD_STARTING)
    tree_marker = TREE_UNRESOLVED if tree_kind != 'absent' else CHILD_STARTING \
        if pending_kind == 'ok' and isinstance(pending_tree, dict) and pending_tree.get('unresolved_tree') is True \
        else None
    if tree_marker:
        f.add(BLOCKED, tree_marker + ': unresolved tree; a person must run twinlane stop JOB --tree-gone')
        detail['unresolved_tree'] = {'marker': tree_marker}
        states.append(common.UNKNOWN)
    # the engine child recorded in status.json (None/absent: none was recorded)
    if st.get('child_pid') is None:
        detail['child'] = {'recorded': False}
        states.append(common.DEAD)
    else:
        state, obs = _observed(snap, st.get('child_pid'), st.get('child_created'))
        detail['child'] = dict(obs, recorded=True)
        receipt_kind, receipt = _json(files, 'result.json')
        if state == common.UNKNOWN and obs.get('exists') is True and obs.get('actual_created') is None \
                and common._valid_stamp(st.get('child_created')) \
                and receipt_kind == 'ok' and jobs._receipt_observed_child_exit(receipt, st) \
                and _json(files, SURVIVOR)[0] == 'absent' and _json(files, CHILD_STARTING)[0] == 'absent':
            pid, created, problem = _runner_identity(files)
            runner, _ = _observed(snap, pid, created)
            if not problem and common._valid_stamp(created) and runner == common.DEAD:
                state = common.DEAD
                detail['child'].update(effective_state=state, reason_code='finished_child_pid_reused')
        states.append(state)
    # child-starting.json: any marker is launch uncertainty; a proven-dead one only clears if its delete works
    kind, pending = _json(files, CHILD_STARTING)
    if kind != 'absent':
        qualified = kind == 'ok' and isinstance(pending, dict) and common._valid_pid(pending.get('pid')) \
            and common._valid_stamp(pending.get('created'))
        if qualified:
            state, obs = _observed(snap, pending['pid'], pending['created'])
        else:
            state, obs = common.UNKNOWN, {'state': common.UNKNOWN, 'reason': 'marker_without_identity'}
        reason = 'pending_launch_cleanup_dependent' if state == common.DEAD else 'pending_launch'
        detail['pending_launch'] = dict(obs, marker=kind if kind != 'ok' else 'present', reason_code=reason)
        f.add(UNKNOWN, reason)
        states.append(common.UNKNOWN)
    # survivor.json: an engine that outlived its kill
    kind, sv = _json(files, SURVIVOR)
    if kind == 'unreadable':
        f.add(UNKNOWN, 'survivor_unreadable')
    elif kind != 'absent':
        if kind != 'ok' or not isinstance(sv, dict) or not isinstance(sv.get('pid'), int):
            f.add(BLOCKED, 'survivor_malformed')  # _writer_job_busy: a survivor file it cannot read is busy
        elif _no_stamp(sv.get('created')):
            state, obs = _observed(snap, sv['pid'], None)
            sv_state = common.DEAD if state == common.DEAD else common.UNKNOWN  # stampless: never ALIVE
            detail['survivor'] = dict(obs, stampless=True, effective_state=sv_state)
            states.append(sv_state)
        else:
            state, obs = _observed(snap, sv['pid'], sv['created'])
            detail['survivor'] = dict(obs, stampless=False, effective_state=state)
            states.append(state)
    engine = common.ALIVE if common.ALIVE in states else common.UNKNOWN if common.UNKNOWN in states else common.DEAD
    detail['engine_state'] = engine
    if engine == common.ALIVE:
        f.add(BLOCKED, 'engine_alive')
    elif engine == common.UNKNOWN:
        f.add(UNKNOWN, 'engine_unknown')
    return f, detail


def _status_word(value):
    return value if value in KNOWN_STATUSES else (None if value in (None, '') else 'other')


def _safe_id(value):
    return value if isinstance(value, str) and _SAFE_ID.fullmatch(value) and value not in ('.', '..') else None


def _evaluate_reservation(snap: dict) -> tuple:
    f = _Findings()
    out = {'present': False}
    kind, _ = _reservation_value(snap)
    if kind == 'absent':
        return f, out
    out['present'] = True
    if kind == 'unreadable':
        f.add(UNKNOWN, 'reservation_unreadable')
        return f, out
    owner = _valid_owner(snap)
    if owner is None:
        f.add(BLOCKED, 'reservation_unreadable_or_incomplete')
        return f, out
    out['owner_id'] = _safe_id(owner['id'])
    state, obs = _observed(snap, owner['pid'], owner['created'])
    out['owner'] = obs
    if state != common.DEAD:
        f.add(_state_effect(state) or UNKNOWN, 'reservation_owner_' + state)
        return f, out
    if out['owner_id'] is None:
        f.add(UNKNOWN, 'reservation_owner_id_unsafe')
        return f, out
    busy, detail = _busy(snap, owner['id'], _entry_for(snap, owner['id']))
    out['owner_job'] = detail
    for item in busy.items:
        f.add(item['effect'], 'reservation_owner_job_' + item['reason'])
    return f, out


def _evaluate_job(snap: dict, name: str, entry: dict, spec: dict) -> dict:
    """One same-canonical-cwd code job, following _claim_writer's order."""
    files = entry['files']
    f = _Findings()
    version = spec.get('writer_reservation_version')
    modern = type(version) is int and version == 1
    if not modern and version == 1:
        f.add(UNKNOWN, 'reservation_version_ambiguous')  # True or 1.0: the claimant reads it as 1
    item = {'id': display_id(name), 'writer_reservation_version': 1 if modern else None if version is None else 'other'}
    kind, st = _json(files, 'status.json')
    if kind == 'unreadable':
        f.add(UNKNOWN, 'status_unreadable')
    elif kind == 'invalid' or (kind == 'ok' and not isinstance(st, dict)):
        f.add(BLOCKED, 'status_unreadable')
    st = st if isinstance(st, dict) else None
    status = (st or {}).get('status')
    item['status'] = _status_word(status)
    item['attempt_recorded'] = bool((st or {}).get('attempt'))
    busy, detail = _busy(snap, name, entry)
    item['engine'] = detail
    # Only a versioned runner acquires this reservation before launching; queued with no attempt and no
    # busy evidence, it does not block (an older queued runner does not honor it and keeps blocking).
    if st is not None and modern and status == 'queued' and not st.get('attempt') and busy.effect() is None:
        item['effect'], item['reasons'] = None, [{'effect': None, 'reason': 'modern_queued_without_attempt'}]
        return item
    pid, created, problem = _runner_identity(files)
    if problem:
        f.add(UNKNOWN, problem)
    if not modern and (not status or status == 'queued' or not pid):
        rkind, receipt = _json(files, 'result.json')
        item['receipt_status'] = _status_word(receipt.get('status')) if isinstance(receipt, dict) else None
        if rkind == 'unreadable':
            f.add(UNKNOWN, 'receipt_unreadable')
        elif not isinstance(receipt, dict) or not receipt.get('status'):
            f.add(BLOCKED, 'legacy_unqualified_prelaunch')
    if pid:
        state, obs = _observed(snap, pid, created)
        item['runner'] = obs
        effect = _state_effect(state)
        if effect:
            f.add(effect, 'runner_' + state)
    else:
        item['runner'] = {'recorded': False}
    for b in busy.items:
        f.add(b['effect'], b['reason'])
    item['effect'], item['reasons'] = f.effect(), f.items
    return item


def _race_label(race) -> str:
    if not isinstance(race, dict) or race.get('kind') not in ('reservation', 'jobs_root', 'entry', 'file'):
        return 'unrecognized'
    if race['kind'] in ('reservation', 'jobs_root'):
        return race['kind']
    label = f"{race['kind']}:{display_id(race.get('entry'))}"
    if race['kind'] == 'file':
        label += '/' + (race.get('file') if race.get('file') in JOB_FILES else 'unrecognized')
    return label


_UNCLASSIFIED = {'scan_failed': (UNKNOWN, 'entry_scan_failed'), 'spec_unreadable': (UNKNOWN, 'job_spec_unreadable'),
                 'spec_invalid': (BLOCKED, 'job_spec_invalid_or_missing'), 'cwd_malformed': (UNKNOWN, 'job_cwd_malformed')}


def evaluate(snap: dict) -> dict:
    """Pure: the allowlisted explanation for one captured snapshot. Never 'allowed'."""
    top = _Findings()
    if not snap.get('stable'):
        top.add(UNKNOWN, 'snapshot_changed_between_reads')
    root = snap['jobs_root']['state']
    if root != 'ok':
        top.add(UNKNOWN, 'jobs_root_' + (root if root in ('absent', 'unreadable') else 'unrecognized'))
    reservation_f, reservation = _evaluate_reservation(snap)
    reservation['effect'] = reservation_f.effect()
    reservation['reasons'] = reservation_f.items
    jobs_out, unclassified = [], []
    skipped = {'not_a_folder': 0, 'unpublished': 0, 'other_profile_or_cwd': 0, 'target_job': 0}
    for name, entry, verdict, spec in _classify(snap):
        if verdict in _UNCLASSIFIED:
            effect, reason = _UNCLASSIFIED[verdict]
            unclassified.append({'id': display_id(name), 'effect': effect, 'reason': reason})
        elif verdict == 'relevant':
            jobs_out.append(_evaluate_job(snap, name, entry, spec))
        else:
            skipped[{'not_a_folder': 'not_a_folder', 'target': 'target_job', 'unpublished': 'unpublished',
                     'other': 'other_profile_or_cwd'}[verdict]] += 1
    effects = [top.effect(), reservation['effect']] + [j['effect'] for j in jobs_out] + \
              [u['effect'] for u in unclassified]
    worst = max(effects, key=lambda e: _RANK[e], default=None)
    decision = BLOCKED if worst == BLOCKED else UNKNOWN if worst == UNKNOWN else NO_OBSERVED_BLOCKER
    target = snap.get('target_job_id')
    return {
        'schema': SCHEMA, 'proposal_only': True, 'grants_admission': False, 'decision': decision,
        'cwd': snap['cwd'], 'target_job_id': None if target is None else display_id(target),
        'snapshot': {'stable': bool(snap.get('stable')), 'generation': snap.get('generation'),
                     'second_generation': snap.get('second_generation'),
                     'races': [_race_label(r) for r in snap.get('races') or []], 'jobs_root': root},
        'findings': top.items, 'reservation': reservation, 'jobs': jobs_out, 'unclassified': unclassified,
        'skipped': skipped, 'notes': list(NOTES),
    }


# ---------------------------------------------------------------- command

def _human(report: dict) -> str:
    lines = [f"writer admission (proposal only, never a grant): {report['decision']}",
             f"cwd: {_printable(report['cwd'])}",
             f"snapshot: {'stable' if report['snapshot']['stable'] else 'CHANGED between reads'} "
             f"generation {str(report['snapshot']['generation'])[:12]}"]
    lines.extend(f'  race: {r}' for r in report['snapshot']['races'])
    for item in report['findings']:
        lines.append(f"  {item['effect']}: {item['reason']}")
    res = report['reservation']
    if not res['present']:
        lines.append('reservation: none')
    else:
        lines.append(f"reservation: owner {res.get('owner_id') or '?'} -> {res['effect'] or 'no observed blocker'}")
        lines.extend(f"  {r['effect']}: {r['reason']}" for r in res['reasons'])
    lines.append(f"same-cwd code jobs: {len(report['jobs'])}")
    for job in report['jobs']:
        lines.append(f"  {job['id']}  status={job['status']}  -> {job['effect'] or 'no observed blocker'}")
        lines.extend(f"    {r['effect'] or 'none'}: {r['reason']}" for r in job['reasons'])
    for u in report['unclassified']:
        lines.append(f"  {u['id']}  {u['effect']}: {u['reason']}")
    lines.extend(f'note: {n}' for n in report['notes'])
    return '\n'.join(lines)


def cli(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane explain-writer-admission',
                                 description='Read-only, proposal-only explanation of the writer-directory check.')
    ap.add_argument('--cwd', required=True)
    ap.add_argument('--job-id')
    ap.add_argument('--json', action='store_true')
    try:
        args = ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    if args.job_id is not None and _safe_id(args.job_id) is None:
        print('twinlane: --job-id must be a plain job id', file=sys.stderr)
        return common.EXIT_USAGE
    if not args.cwd or '\x00' in args.cwd:  # checked before any path call: a NUL never reaches the filesystem
        print('twinlane: --cwd must be a non-empty path without NUL characters', file=sys.stderr)
        return common.EXIT_USAGE
    try:
        writer_cwd(args.cwd)
    except (OSError, ValueError):
        print('twinlane: --cwd cannot be canonicalized', file=sys.stderr)
        return common.EXIT_USAGE
    snap = capture_snapshot(args.cwd, common.JOBS, common.STATE, target_job_id=args.job_id)
    report = evaluate(snap)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(_human(report))
    return common.EXIT_OK
