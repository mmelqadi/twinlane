"""Opt-in CLI task coordination. Trusted host receipts are not worker assertions.

A coordinated `twinlane run` names a task, a dispatch, its sender (actor) and its receiver (recipient).
The task registry lives outside every worker checkout, at <state>/coordination/<sha256(task)>.json,
format version 1. That format, its path scheme and the dispatch fingerprint are frozen: registries the
predecessor tool wrote (and the jobs they name) replay unchanged, and both tools take the same lock.

The registry validates ordinary twinlane dispatch. It does not authenticate desktop messages or replace
OS/worker write confinement. Anything unreadable, unknown or mismatched is refused, never repaired or
guessed: a refusal leaves the registry as it was, for its owner.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets

from . import common

# Worker lineage: the variable twinlane sets on every engine it starts.
WORKER_ENV = ('TWINLANE_ENGINE_JOB',)
VERSION = 1
MAX_REVISION = 32
# Executable intent of a plan without caller_intent. Frozen: a changed key set changes every fingerprint.
FINGERPRINT_KEYS = ('engine', 'kind', 'profile', 'cwd', 'brief_text', 'primary', 'failover',
                    'resume', 'resume_jobs', 'continue_job', 'coordination')
ENGINES = ('claude', 'codex')

_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9_.:-]{0,159}')
_JOB = re.compile(r'[0-9]{8}-[0-9]{6}-(?:claude|codex)-[0-9a-f]{4}')
_HEX64 = re.compile(r'[0-9a-f]{64}')


class Refusal(ValueError):
    pass


def _is_id(value) -> bool:
    return isinstance(value, str) and _ID.fullmatch(value) is not None


def _identifier(value):
    if not _is_id(value):
        raise Refusal('coordination identity is missing or invalid')
    return value


def _path(task):
    return os.path.join(common.STATE, 'coordination', hashlib.sha256(task.encode()).hexdigest() + '.json')


def _count(value) -> bool:
    return type(value) is int and value >= 0


def _flag(value) -> bool:
    return type(value) is bool


def _text_or_none(value) -> bool:
    return value is None or isinstance(value, str)


def _dispatch_problem(key, e, epoch) -> str | None:
    if not _is_id(key) or not isinstance(e, dict):
        return 'dispatch identity'
    if not isinstance(e.get('job'), str) or not _JOB.fullmatch(e['job']):
        return 'dispatch job'
    if not isinstance(e.get('fingerprint'), str) or not _HEX64.fullmatch(e['fingerprint']):
        return 'dispatch fingerprint'
    if not (_is_id(e.get('actor')) and _is_id(e.get('recipient'))):
        return 'dispatch sender or recipient'
    lineage = e.get('lineage')
    if not isinstance(lineage, list) or not lineage or not all(_is_id(name) for name in lineage):
        return 'dispatch lineage'
    if not (_count(e.get('epoch')) and e['epoch'] <= epoch and _count(e.get('revision'))):
        return 'dispatch epoch or revision'
    if not (_flag(e.get('ready')) and _flag(e.get('launch_admitted'))):
        return 'dispatch admission flags'
    return None


def _receipt_problem(key, r, epoch) -> str | None:
    if not _is_id(key) or not isinstance(r, dict):
        return 'receipt identity'
    if r.get('kind') not in ('review', 'handoff'):
        return 'receipt kind'
    if not all(_is_id(r.get(name)) for name in ('actor', 'recipient', 'predecessor')):
        return 'receipt parties'
    if not isinstance(r.get('evidence'), str) or not r['evidence'].strip():
        return 'receipt evidence'
    if not (_text_or_none(r.get('finding')) and _text_or_none(r.get('accepted_by'))):
        return 'receipt finding or acceptance'
    if not (_count(r.get('epoch')) and r['epoch'] <= epoch):
        return 'receipt epoch'
    if r.get('used_by') is not None and not _is_id(r['used_by']):
        return 'receipt use'
    return None


def registry_problem(data, task) -> str | None:
    """What makes `data` not a version-1 registry of `task`, or None. Unknown extra keys are kept."""
    if not isinstance(data, dict):
        return 'not an object'
    if type(data.get('version')) is not int or data['version'] != VERSION:
        return 'unknown version'
    if data.get('task') != task:
        return 'task mismatch'
    if not all(_is_id(data.get(name)) for name in ('coordinator', 'owner', 'original_owner')):
        return 'coordinator or owner'
    if not (_count(data.get('epoch')) and _count(data.get('revision')) and data['revision'] <= MAX_REVISION):
        return 'epoch or revision'
    if not isinstance(data.get('dispatches'), dict) or not isinstance(data.get('receipts'), dict):
        return 'dispatches or receipts'
    for key, entry in data['dispatches'].items():
        problem = _dispatch_problem(key, entry, data['epoch'])
        if problem:
            return problem
    for key, entry in data['receipts'].items():
        problem = _receipt_problem(key, entry, data['epoch'])
        if problem:
            return problem
    return None


def _read(task):
    path = _path(task)
    try:
        os.stat(path)
    except FileNotFoundError:
        return None
    except OSError as e:  # present but unreachable is not absent: never start a fresh registry over it
        raise Refusal('coordination registry unreadable; preserve it for its owner') from e
    data = common.read_json(path)
    if registry_problem(data, task):
        raise Refusal('coordination registry unreadable; preserve it for its owner')
    return data


def _save(data):
    common.write_json(_path(data['task']), data)


def _lock(task):
    return common.file_lock(_path(task) + '.lock', 30)


def _fingerprint(plan):
    # Meter timestamps and routing narration vary on replay; executable intent must not.
    payload = plan.get('caller_intent') or {key: plan.get(key) for key in FINGERPRINT_KEYS}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()


def _envelope(plan):
    e = plan.get('coordination')
    if not isinstance(e, dict):
        raise Refusal('coordination envelope must be an object')
    for key in ('task', 'dispatch', 'actor', 'recipient'):
        _identifier(e.get(key))
    for key in ('parent', 'receipt'):
        if e.get(key) is not None:
            _identifier(e[key])
    return e


# ---------------------------------------------------------------- worker lineage

def worker_ids() -> list:
    """The distinct non-empty job ids the worker-lineage variables name (none outside a worker)."""
    ids = []
    for name in WORKER_ENV:
        value = os.environ.get(name)
        if value and value not in ids:
            ids.append(value)
    return ids


def worker_parents() -> list:
    """(id, job.json) of every job the lineage variables name. Any unreadable one: custody is unknown."""
    parents = []
    for jid in worker_ids():
        if not _JOB.fullmatch(jid):
            raise Refusal('worker job identity is invalid; custody is unknown')
        spec = common.read_json(os.path.join(common.JOBS, jid, 'job.json'))
        if not isinstance(spec, dict) or spec.get('id') != jid:
            raise Refusal('actual parent metadata missing/unreadable; custody is unknown')
        parents.append((jid, spec))
    return parents


def actual_parent():
    """job.json of the one job this process works for, or None outside a worker. Never a guess: an
    unreadable parent leaves the custody unknown (worker_parents refuses)."""
    parents = worker_parents()
    return parents[0][1] if parents else None


def _check_lineage(e, message):
    """Inside a worker, a dispatch must continue that worker's own delegation."""
    if not worker_ids():
        return
    parent = actual_parent()
    inherited = parent.get('coordination')
    if not isinstance(inherited, dict) or e['task'] != inherited.get('task') \
            or e.get('parent') != inherited.get('dispatch') or e['actor'] != inherited.get('recipient'):
        raise Refusal(message)


# ---------------------------------------------------------------- host and dispatch API

def record_transition(task, receipt, *, authority, kind, actor, recipient,
                      predecessor, evidence, finding=None, accepted_by=None):
    """Trusted host coordinator records already-obtained acceptance, never a brief flag.

    No CLI worker path calls this API. Host access and existing write confinement
    remain its trust boundary; names/evidence strings are not cryptographic identity.
    """
    if worker_ids():
        raise Refusal('workers cannot issue coordination acceptance receipts')
    for value in (task, receipt, authority, actor, recipient, predecessor):
        _identifier(value)
    if kind not in ('review', 'handoff') or not isinstance(evidence, str) or not evidence.strip():
        raise Refusal('transition requires review/handoff and recorded acceptance evidence')
    if kind == 'review' and (not isinstance(finding, str) or not finding.strip()):
        raise Refusal('review rework requires a concrete finding')
    if kind == 'handoff' and (accepted_by != recipient or actor == recipient):
        raise Refusal('handoff requires recipient acceptance')
    if not (_text_or_none(finding) and _text_or_none(accepted_by)):
        raise Refusal('finding and acceptance must be text')
    value = dict(kind=kind, actor=actor, recipient=recipient, predecessor=predecessor,
                 evidence=evidence, finding=finding, accepted_by=accepted_by)
    with _lock(task):
        data = _read(task)
        if data is None or authority != data['coordinator']:
            raise Refusal('only the recorded host coordinator may record acceptance')
        old = data['receipts'].get(receipt)
        if old is not None:
            if {k: old.get(k) for k in value} != value:
                raise Refusal('acceptance identity conflicts with its recorded payload')
            return
        parent = data['dispatches'].get(predecessor)
        if not parent or not parent['ready'] or parent['epoch'] != data['epoch']:
            raise Refusal('transition predecessor is not current admitted work')
        if kind == 'review' and recipient != data['owner']:
            raise Refusal('review rework must return to the current implementation owner')
        if kind == 'handoff' and actor != data['owner']:
            raise Refusal('only the current owner can offer custody')
        data['receipts'][receipt] = dict(value, epoch=data['epoch'], used_by=None)
        _save(data)


def allocate(plan, factory):
    """Validate before job allocation; reserve identity before any job files are written."""
    e = _envelope(plan)
    task, request = e['task'], e['dispatch']
    if plan.get('engine') not in ENGINES:
        raise Refusal('dispatch engine is unknown; no allocation')
    fingerprint = _fingerprint(plan)
    _check_lineage(e, 'worker dispatch must preserve its actual parent task and sender lineage')
    with _lock(task):
        data = _read(task)
        if data and request in data['dispatches']:
            entry = data['dispatches'][request]
            if entry['fingerprint'] != fingerprint:
                raise Refusal('duplicate dispatch identity has a conflicting payload')
            if not entry['ready']:
                raise Refusal('dispatch allocation is uncertain; owner recovery required, no relaunch')
            spec = common.read_json(os.path.join(common.JOBS, entry['job'], 'job.json'))
            if not isinstance(spec, dict) or _fingerprint(spec) != fingerprint:
                raise Refusal('existing dispatch job is missing or changed; no replacement launch')
            return entry['job']
        if data is None:
            if e.get('parent') or e.get('receipt') or e['actor'] == e['recipient']:
                raise Refusal('initial dispatch requires distinct coordinator and owner')
            data = dict(version=VERSION, task=task, coordinator=e['actor'], owner=e['recipient'],
                        original_owner=e['recipient'], epoch=0, revision=0, dispatches={}, receipts={})
            lineage = [e['actor'], e['recipient']]
        else:
            parent = data['dispatches'].get(e.get('parent'))
            receipt = data['receipts'].get(e.get('receipt')) if e.get('receipt') else None
            if not parent or not parent['ready'] or parent['epoch'] != data['epoch']:
                raise Refusal('dispatch requires a current admitted predecessor')
            if e['actor'] != parent['recipient']:
                raise Refusal('sender does not own the predecessor delegation')
            lineage = list(parent['lineage'])
            if receipt:
                if receipt['epoch'] != data['epoch'] or receipt['used_by'] is not None \
                        or receipt['actor'] != e['actor'] or receipt['recipient'] != e['recipient'] \
                        or receipt['predecessor'] != e['parent']:
                    raise Refusal('acceptance receipt is stale, consumed or mismatched')
                if receipt['kind'] == 'review':
                    if e['recipient'] != data['owner'] or data['revision'] >= MAX_REVISION:
                        raise Refusal('review owner or bounded revision is invalid')
                    if data['owner'] not in lineage:
                        raise Refusal('review return has no owner in its recorded lineage')
                    data['revision'] += 1
                    # Return to the owner's pre-review ancestry. Historical dispatch
                    # entries retain the complete review branch; another same-reviewer
                    # round may start, but every return still needs a fresh receipt.
                    last_owner = len(lineage) - 1 - lineage[::-1].index(data['owner'])
                    lineage = lineage[:last_owner]
                else:
                    if e['actor'] != data['owner'] or receipt['accepted_by'] != e['recipient']:
                        raise Refusal('custody transfer is not accepted by the new owner')
                    data['owner'] = e['recipient']
                    data['epoch'] += 1
                receipt['used_by'] = request
            elif e.get('receipt'):
                raise Refusal('no trusted host acceptance receipt exists')
            elif e['recipient'] in lineage:
                raise Refusal('ancestor delegation cycle refused; report result to coordinator')
            lineage.append(e['recipient'])
        for _ in range(20):
            jid = f'{common.stamp()}-{plan["engine"]}-{secrets.token_hex(2)}'
            # Existing baton/job readers require four hex digits. Never overwrite
            # another task's folder or this task's uncertain reservation.
            if not os.path.exists(os.path.join(common.JOBS, jid)) and not any(
                    item['job'] == jid for item in data['dispatches'].values()):
                break
        else:
            raise Refusal('could not reserve a compatible job identity; no allocation')
        entry = dict(job=jid, fingerprint=fingerprint, actor=e['actor'], recipient=e['recipient'],
                     lineage=lineage, epoch=data['epoch'], revision=data['revision'], ready=False,
                     launch_admitted=False)
        data['dispatches'][request] = entry
        _save(data)  # A crash/factory failure is durable uncertainty, never a fresh dispatch.
        factory(jid)
        entry['ready'] = True
        _save(data)
        return jid


def claim_launch(plan, jid):
    """Only one CLI invocation may submit the runner, including before runner.claim exists."""
    e = _envelope(plan)
    with _lock(e['task']):
        data = _read(e['task'])
        if data is None:
            raise Refusal('coordination registry is missing')
        entry = data['dispatches'].get(e['dispatch'])
        if not entry or not entry['ready'] or entry['job'] != jid or entry['fingerprint'] != _fingerprint(plan):
            raise Refusal('dispatch launch does not match its admitted identity')
        if entry['epoch'] != data['epoch']:
            raise Refusal('old custody cannot launch after accepted handoff')
        if entry['launch_admitted']:
            return False
        entry['launch_admitted'] = True
        _save(data)  # Unknown launch outcomes stay reserved; never retry under this identity.
        return True


def validate_runner(plan, jid):
    """Internal _job may consume only an admitted current-epoch CLI launch."""
    e = _envelope(plan)
    with _lock(e['task']):
        data = _read(e['task'])
        entry = data['dispatches'].get(e['dispatch']) if data else None
        if not entry or not entry['ready'] or not entry['launch_admitted'] \
                or entry['epoch'] != data['epoch'] or entry['job'] != jid \
                or entry['fingerprint'] != _fingerprint(plan):
            raise Refusal('runner lacks a current admitted coordination launch')


def replay(plan):
    """Read an existing immutable intent before automatic routing, never admit a launch."""
    e = _envelope(plan)
    _check_lineage(e, 'replay must preserve actual worker lineage')
    with _lock(e['task']):
        data = _read(e['task'])
        entry = data['dispatches'].get(e['dispatch']) if data else None
        if entry is None:
            return None
        if entry['fingerprint'] != _fingerprint(plan):
            raise Refusal('duplicate dispatch identity has a conflicting caller intent')
        if not entry['ready']:
            raise Refusal('dispatch allocation is uncertain; no relaunch')
        spec = common.read_json(os.path.join(common.JOBS, entry['job'], 'job.json'))
        if not isinstance(spec, dict) or _fingerprint(spec) != entry['fingerprint']:
            raise Refusal('existing dispatch job is missing or changed; no replacement launch')
        return entry['job']
