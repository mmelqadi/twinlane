"""Bounded, file-only work checks. Only --fix may pump requests or release stale custody."""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import shlex
import threading
import time
from collections import deque
from contextlib import nullcontext

from . import baton, board, common, hostload, jobs, meter, observations, watchdog, wip

PASS_BUDGET_S = 9.0
READ_BUDGET_S = 1.0
REMINDER_S = 3600
HOST_MAX_AGE_S = 300
SKIPPED = ['slot_leak: slots are derived from job liveness; no separate cheap lease reader',
           'worktrees.doctor: repository and dependency probes exceed the file-only budget']
_READERS = {}


def _bounded(name, read, deadline, budget=READ_BUDGET_S, spawn=False):
    """One daemon per collector/home at most; an overdue read never piles up on later polls."""
    key = (common.STATE, name)
    old = _READERS.get(key)
    if old is not None and old.is_alive():
        raise TimeoutError('previous read still pending')
    left = min(budget, deadline - time.monotonic())
    if left <= 0:
        raise TimeoutError('pass budget exhausted')
    done, result = threading.Event(), []

    def run():
        try:
            with observations.scope(), (nullcontext() if spawn else common.nonspawning_liveness()):
                result.append((True, read()))
        except (Exception, SystemExit) as e:  # load_policy exits on unreadable input
            result.append((False, ValueError(str(e)) if isinstance(e, SystemExit) else e))
        finally:
            done.set()

    worker = threading.Thread(target=run, name='patrol-' + name, daemon=True)
    _READERS[key] = worker
    worker.start()
    if not done.wait(left):
        raise TimeoutError('collector budget exhausted')
    if not result[0][0]:
        raise result[0][1]
    return result[0][1]


def _finding(kind, ident, text, remedy, age=None, safe=False, source='board', severity='warning',
             condition=None, detail=None):
    return {'kind': kind, 'id': str(ident), 'severity': severity, 'age_s': age,
            'text': text, 'remedy': remedy, 'safe_fix': safe, 'source': source,
            'condition': condition or text, 'detail': detail}


def _error(name, error):
    return _finding('patrol_error', name, f'{name}: {type(error).__name__}: {error}',
                    'twinlane patrol --json', source=name)


def _reason_category(reason):
    """Measurements belong in detail, never in a queue wait's identity."""
    reason = (reason or '').lower()
    if reason.startswith('host busy'):
        return 'host busy'
    if reason.startswith(('engine stop', 'engine out', 'engine unknown', 'engine at ',
                          'claude at ', 'codex at ', 'claude stop', 'codex stop')):
        return 'engine stop'
    if reason.startswith(('engine slots full', 'slots full', 'claude slots full', 'codex slots full')):
        return 'slots full'
    if reason.startswith('writer capacity') or reason.split(' ', 1)[-1].startswith('writer lanes busy'):
        return 'writer capacity'
    if reason.startswith(('claimed', 'a pump is starting', 'an earlier pump claimed', 'earlier pump claim')):
        return 'claimed'
    return 'other'


def _number(value, default=0):
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else default


def _setting(policy, key, default):
    raw = policy.get('patrol')
    return _number(raw.get(key), default) if isinstance(raw, dict) else default


def _initial():
    return {'version': 1, 'at': None, 'findings': [], 'seen': {}, 'overload_passes': 0}


def _key(row):
    digest = hashlib.sha256((row.get('condition') or row['text']).encode('utf-8')).hexdigest()[:16]
    return row['kind'] + ':' + row['id'] + ':' + digest


def _state():
    raw = _object(os.path.join(common.STATE, 'patrol.json'), _initial())
    if not isinstance(raw, dict) or type(raw.get('version')) is not int or raw.get('version') != 1 or not isinstance(raw.get('seen'), dict) or \
            not isinstance(raw.get('findings'), list) or type(raw.get('overload_passes')) is not int or \
            raw['overload_passes'] < 0:
        raise ValueError('patrol state unreadable')
    for row in raw['findings']:
        if not isinstance(row, dict) or any(not isinstance(row.get(k), str) for k in ('kind', 'id', 'text', 'source', 'severity', 'remedy')) or \
                type(row.get('safe_fix')) is not bool or any(
                    row.get(k) is not None and not isinstance(row[k], str) for k in ('condition', 'detail', 'pump_reason')):
            raise ValueError('patrol state unreadable')
    for key, value in raw['seen'].items():
        if not isinstance(key, str) or not isinstance(value, dict) or any(
                common.parse_time(value.get(k)) is None for k in ('first_at', 'last_at', 'reminded_at')):
            raise ValueError('patrol state unreadable')
    return raw


def _object(path, missing=None):
    """Only ENOENT may mean absent; invalid JSON and access failures are incomplete."""
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        return missing
    if not isinstance(value, dict):
        raise ValueError('not a JSON object: ' + os.path.basename(path))
    return value


def _stat(path):
    try:
        return os.stat(path)
    except FileNotFoundError:
        return None


def _policy():
    _object(common.POLICY)
    _object(os.environ.get('TWINLANE_CONFIG') or os.path.join(common.data_home(), 'config.json'))
    value = common.load_policy()
    if not isinstance(value, dict) or not value:
        raise ValueError('policy unreadable')
    return value


def _job_inputs():
    """Strict inputs for the board/liveness projections; share parsed job files locally."""
    for jid in observations.job_ids():
        for name in ('job.json', 'status.json', 'result.json', jobs.SURVIVOR,
                     jobs.CHILD_STARTING, jobs.TREE_UNRESOLVED, jobs.OWNER_NOTIFIED):
            path = jobs._p(jid, name)
            _stat(path)  # isfile/exists must not hide access failures
            value = _object(path)
            if value is not None and name in ('status.json', 'result.json') and value.get('status') not in board.LIVE + board.TERMINAL:
                raise ValueError('invalid job status: ' + name)
            if value is not None:
                for key in ('created_at', 'started_at', 'ended_at', 'updated_at', 'waiting_since'):
                    if value.get(key) is not None and common.parse_time(value[key]) is None:
                        raise ValueError('invalid job timestamp: ' + name)
                if name == jobs.OWNER_NOTIFIED and common.parse_time(value.get('at')) is None:
                    raise ValueError('owner notice timestamp unreadable')
            observations.once(('job-file', common.JOBS, jid, name), lambda value=value: value)
        _stat(jobs._p(jid, jobs.COLLECTED))
        try:
            with open(jobs._p(jid, 'runner.pid'), encoding='utf-8') as handle:
                parts = handle.read().split()
        except FileNotFoundError:
            continue
        if len(parts) not in (1, 2) or not all(p.isascii() and p.isdigit() for p in parts):
            raise ValueError('runner identity unreadable')


def _board(policy):
    _job_inputs()
    raw = _object(os.path.join(common.STATE, 'meter.json'))
    if raw is not None:
        if not all(isinstance(raw.get(e), dict) for e in board.ENGINES):
            raise ValueError('meter cache unreadable')
        if common.parse_time(raw.get('read_at')) is None or any(
                common.parse_time(raw[e].get('reading_at')) is None for e in board.ENGINES):
            raise ValueError('meter cache timestamp unreadable')
        # an unsupported status is not "available": board keeps any non-empty string, patrol would read it as usable
        known = set(meter.STATUS_RANK) | {'unknown'}
        if any(raw[e].get('status') not in known for e in board.ENGINES):
            raise ValueError('meter cache status unreadable')
    data = board.collect(policy=policy, file_only=True, show_all=True)
    # Use the strictly parsed cache rather than the board's tolerant second read.
    if raw is not None:
        now = common.now()
        views = {e: board._engine_view(e, raw.get(e), policy, now) for e in board.ENGINES}
        data['meters'].update(views)
        data['meters']['routes'] = board._routes(views, policy)
    return data


def _slots():
    _job_inputs()
    counts = {e: jobs.running_count(e) for e in board.ENGINES}
    if any(type(n) is not int or n < 0 for n in counts.values()):
        raise ValueError('slot counts unreadable')
    return counts


def _host():
    _object(os.path.join(common.STATE, 'hostload.json'))
    return hostload.cached()


def _queue():
    raw = _object(wip.queue_path(), {'version': 1, 'queue': []})
    if raw.get('version') != 1 or not isinstance(raw.get('queue'), list) or any(
            not isinstance(row, dict) for row in raw['queue']):
        raise ValueError('writer queue unreadable')
    if wip.queue() != raw['queue']:
        raise ValueError('writer queue changed or became unreadable during observation')
    rows = []
    for row in raw['queue']:
        if not isinstance(row.get('id'), str) or not row['id'] or common.parse_time(row.get('queued_at')) is None:
            raise ValueError('queue identity/timestamp unreadable')
        # launchable uses isfile/isdir, which also erase stat failures.
        brief = wip._brief_path(row)
        if brief:
            if isinstance(row.get('cwd'), str):
                _stat(row['cwd'])
            # Legacy requests can replay absolute paths without a recorded cwd.
            if os.path.isabs(brief):
                _stat(brief)
            elif isinstance(row.get('cwd'), str):
                _stat(os.path.join(row['cwd'], brief))
        rows.append(dict(row, launch_reason=wip.launchable(row)))
    return rows


def _details():
    _job_inputs()
    out = []
    for jid in observations.job_ids():
        st = observations.job_json(jid, 'status.json', {})
        spec = observations.job_json(jid, default={})
        st, spec = st if isinstance(st, dict) else {}, spec if isinstance(spec, dict) else {}
        pending = any(_stat(jobs._p(jid, name)) is not None for name in (jobs.TREE_UNRESOLVED, jobs.CHILD_STARTING))
        age = None
        if st.get('status') == 'running' and st.get('engine') in ('claude', 'codex'):
            attempt = st.get('attempt', 1)
            files = jobs._attempt_files(jid, attempt if type(attempt) is int and attempt > 0 else 1)
            paths = ([files['events'], files['stderr']] if st.get('engine') == 'claude' else
                     [files['codex_log'], os.path.join(files['codex_out'], 'events.jsonl')])
            stamps = [_stat(path) for path in paths]
            latest = max((s.st_mtime for s in stamps if s is not None), default=0)
            began = common.parse_time(st.get('started_at') or spec.get('created_at'))
            latest = max(latest, began.timestamp() if began else 0)
            age = max(0, common.now().timestamp() - latest) if latest else None
        out.append({'id': jid, 'pending': pending, 'cwd': spec.get('writer_root') or spec.get('cwd'),
                    'quiet_s': age, 'waiting_since': st.get('waiting_since'), 'reason': st.get('queue_reason'),
                    'age_s': common.age_s(spec.get('created_at'))})
    return out


def _engine(row, data):
    argv = row.get('argv')
    argv = argv if isinstance(argv, list) else []

    def option(name, default):
        for i, arg in enumerate(argv):
            if not isinstance(arg, str):
                continue
            if arg.startswith(name + '='):
                return arg.split('=', 1)[1]
            if arg == name and i + 1 < len(argv) and isinstance(argv[i + 1], str):
                return argv[i + 1]
        return default
    engine = option('--engine', row.get('engine'))
    if engine in ('claude', 'codex'):
        return engine
    kind = option('--kind', 'code')
    route = (data.get('meters', {}).get('routes') or {}).get(kind) or {}
    return route.get('engine') or ('codex' if kind in ('parallel', 'sweep', 'integrate', 'verify') else 'claude')


def collect(previous, deadline):
    findings, failed, values = [], set(), {}
    def read(name, fn, budget=READ_BUDGET_S):
        try:
            values[name] = _bounded(name, fn, deadline, budget)
            if values[name] is None and name != 'hostload':
                raise ValueError('reader returned no observation')
        except Exception as e:
            failed.add(name)
            findings.append(_error(name, e))
        return values.get(name)

    policy = read('policy', _policy) or {}
    data = read('board', lambda: _board(policy), 2.0) if 'policy' not in failed else None
    pending = read('queue', _queue)
    cached = read('hostload', _host)
    counts = read('slots', _slots)
    stale = read('custody', lambda: jobs.stale_writers_snapshot(deadline=deadline))
    details = read('details', _details)
    cfg = hostload.settings(policy)
    reading = cached[0] if cached is not None and cached[1] <= HOST_MAX_AGE_S else None
    usable_counters = isinstance(reading, dict) and all(
        _number(reading.get(k), None) is not None for k in ('cpu_pct', 'ram_free_mb', 'engine_procs'))
    host_status = 'disabled' if not cfg['enabled'] else 'known' if usable_counters else 'unknown'
    if host_status == 'unknown':
        if 'hostload' not in failed:
            findings.append(_error('hostload', ValueError('host cache missing, stale or unusable')))
        failed.add('hostload')
    if isinstance(reading, dict):
        reading = {k: _number(reading.get(k), None) for k in ('cpu_pct', 'ram_free_mb', 'engine_procs')}
    pressure, why = hostload.pressure(reading, cfg) if cfg['enabled'] else (0, None)
    overload = (previous['overload_passes'] if 'hostload' in failed else
                previous['overload_passes'] + 1 if pressure > 0 else 0)
    for row in stale or []:
        findings.append(_finding('custody_stale', row['id'], f"proven stale writer custody; worktree {row.get('cwd') or '?'}",
                                 'twinlane wt doctor --fix', safe=True, source='custody'))
    for row in details or []:
        if row['pending']:
            findings.append(_finding('custody_unresolved', row['id'],
                                     f"engine tree is not proven gone; blocks worktree {row['cwd'] or '?'}",
                                     f"twinlane stop {row['id']} --tree-gone", row['age_s'], source='details'))
    if data:
        live = data['jobs']['live']
        for row in live + data['jobs']['finished']:
            status, jid = row.get('status'), row['id']
            if status in ('orphaned', 'unknown', 'lost'):
                text = {'orphaned': 'runner gone; engine still running', 'unknown': 'process identity unreadable; check from the host',
                        'lost': 'runner and engine gone; inspect lost group before resuming'}[status]
                remedy = 'twinlane resume' if status == 'lost' else f'twinlane status {jid}'
                findings.append(_finding('job_' + status, jid, text, remedy, row.get('age_s')))
        for row in data['jobs']['unreadable']:
            failed.add('board')
            findings.append(_error('board', ValueError(f"unreadable job {row['id']}: {row.get('problems')}")))
        detail_map = {r['id']: r for r in details or []}
        watchdog_cfg = watchdog.settings(policy)
        stall = watchdog_cfg['stall_s']
        for row in live:
            if row.get('status') != 'running':
                continue
            reasons = []
            age, timeout = _number(row.get('age_s')), _number(row.get('timeout_s'))
            if timeout and age >= timeout * .8:
                reasons.append('past 80% of timeout')
            quiet = detail_map.get(row['id'], {}).get('quiet_s')
            remedy = f"twinlane status {row['id']}"
            if quiet is not None and quiet > stall:
                recovery = 'watchdog handles recovery' if watchdog_cfg['enabled'] else 'inspect it; the watchdog is off'
                reasons.append(f'no output past watchdog stall_s={stall:g}; {recovery}')
                if not watchdog_cfg['enabled']:
                    remedy = f"twinlane stop {row['id']}"
            if reasons:
                findings.append(_finding('job_long', row['id'], '; '.join(reasons),
                                         remedy, age))

        from . import inbox
        for row in inbox.rank([r for b in data['batons'] for r in b.get('inbox', [])]):
            age = common.age_s(row.get('at'))
            if age is None:
                failed.add('board')
                findings.append(_error('board', ValueError('inbox timestamp unreadable')))
            if age is not None and age >= _setting(policy, 'person_wait_s', 1800):
                findings.append(_finding('waiting_on_person', row['id'],
                                         f"{row.get('question')}; records: {row.get('records')}",
                                         'twinlane inbox --all', age, severity='info'))
        for row in data['batons']:
            if row.get('problems'):
                failed.add('board')
                findings.append(_error('board', ValueError(f"unreadable baton {row['slug']}: {row['problems']}")))
            age = common.age_s(row.get('waiting_since'))
            if row.get('waiting_on') == 'user' and age is None:
                failed.add('board')
                findings.append(_error('board', ValueError('baton waiting timestamp unreadable')))
            if row.get('waiting_on') == 'user' and age is not None and age >= _setting(policy, 'person_wait_s', 1800):
                findings.append(_finding('waiting_on_person', row['slug'], row.get('next_action') or 'waiting on user',
                                         f"twinlane baton show {row['slug']}", age, severity='info'))
        if overload >= 3 and 'hostload' not in failed:
            oldest = sorted((r for r in live if r.get('status') == 'running'),
                            key=lambda r: -_number(r.get('age_s')))[:5]
            ids = ', '.join(r['id'] for r in oldest) or 'none'
            finding = _finding('host_overload', 'host', 'host load remains over threshold',
                               'twinlane board', source='hostload', condition='over_threshold',
                               detail=f'{why}; oldest running jobs: ' + ids)
            finding['running_jobs'] = [{'id': r['id'], 'age_s': r.get('age_s')} for r in oldest]
            findings.append(finding)
    if pending is not None and data is not None and counts is not None:
        waiting = []
        previous_queue = {r['id']: r for r in previous['findings'] if r['source'] == 'queue'}
        for row in sorted(pending, key=lambda r: str(r.get('queued_at') or '')):
            jid, engine = row.get('id', '?'), _engine(row, data)
            age = common.age_s(row.get('queued_at'))
            if row.get('pumping_at') is not None or row.get('claim') is not None:
                claim_age = common.age_s(row.get('pumping_at'))
                needs_person = claim_age is None or claim_age > 600
                finding = _finding('queue_claimed', jid,
                                   'earlier pump claim needs a person to inspect it' if needs_person else 'a pump is starting it',
                                   'twinlane jobs; twinlane wip next' if needs_person else 'twinlane wip',
                                   claim_age, source='queue', severity='warning' if needs_person else 'info')
                finding.update(needs_person=needs_person, claim_only=row.get('pumping_at') is None)
                waiting.append(finding)
                continue
            meter = data['meters'].get(engine) or {}
            status = meter.get('status', 'unknown')
            reason = row['launch_reason']
            condition, detail = reason, None
            configured = int((policy.get(engine) or {}).get('max_parallel') or 4)
            usable, host_reason = hostload.effective_parallel(configured, reading, policy)
            if status in ('stop', 'out', 'unknown'):
                reason = f'{engine} at {status}'
                condition = reason
            elif host_status == 'unknown':
                reason, condition = 'host load unknown', 'host_unknown'
            elif counts[engine] >= usable:
                reason = 'host busy: reduced slots full' if host_reason else f'{engine} slots full'
                condition = 'host_slots_full' if host_reason else 'engine_slots_full'
                detail = host_reason or f'{counts[engine]}/{usable}'
            ready = reason is None
            prior = previous_queue.get(str(jid), {})
            pump_reason = prior.get('pump_reason') if ready else None
            finding = _finding('queue_waiting' if reason or pump_reason else 'queue_ready', jid,
                               f'{engine}: ' + (reason or pump_reason or
                                   'engine slots and host allow it; writer capacity is decided by the pump'),
                               'twinlane wip pump' if ready else 'twinlane wip', age,
                               safe=ready and age is not None and age >= _setting(policy, 'queue_stall_s', 600),
                               source='queue', severity='info',
                               condition=condition or (_reason_category(pump_reason) if pump_reason else 'engine_host_ready'),
                               detail=detail or pump_reason)
            if pump_reason:
                finding['pump_reason'] = pump_reason
            waiting.append(finding)
        findings.extend(waiting)
    elif pending:
        failed.add('queue')
    live_map = {r['id']: r for r in (data or {}).get('jobs', {}).get('live', [])}
    if data:
        by_engine = {}
        for row in pending or []:
            by_engine.setdefault(_engine(row, data), []).append(row)
        for row in live_map.values():
            if row.get('status') == 'queued' and row.get('engine') in ('claude', 'codex'):
                by_engine.setdefault(row['engine'], []).append(row)
        for engine, rows in by_engine.items():
            status = (data['meters'].get(engine) or {}).get('status', 'unknown')
            if status in ('stop', 'out', 'unknown'):
                other = 'codex' if engine == 'claude' else 'claude'
                commands = []
                for row in rows:
                    cwd = row.get('worktree')
                    if not isinstance(cwd, str) or not os.path.isabs(cwd):
                        cwd = row.get('cwd')
                    if cwd:
                        commands.append(f'twinlane handoff --cd {shlex.quote(cwd)} --to {other}')
                finding = _finding('engine_blocked', engine,
                                   f"{engine} at {status}; queued: {', '.join(str(r.get('id', '?')) for r in rows)}",
                                   commands[0] if commands else 'twinlane baton list', source='queue')
                finding['commands'] = list(dict.fromkeys(commands))
                findings.append(finding)
    for row in details or []:
        if live_map.get(row['id'], {}).get('status') == 'queued':
            reason = row['reason'] or 'waiting for a slot'
            findings.append(_finding('queue_waiting', row['id'], reason,
                                     f"twinlane status {row['id']}", common.age_s(row['waiting_since']),
                                     source='details', severity='info', condition=_reason_category(reason), detail=reason))
    # Missing evidence cannot resolve last pass's warning.
    if 'policy' in failed:
        failed.update(('board', 'queue', 'details', 'slots', 'hostload', 'custody'))
    if 'details' in failed:
        failed.add('board')
    if 'board' in failed:
        failed.add('hostload')
    if 'board' in failed or 'slots' in failed or 'hostload' in failed:
        failed.add('queue')
    def incomplete(row):
        # Runtime waits need details plus the board's live classification and queue evidence.
        return row['source'] in failed or (row['source'] == 'details' and row['kind'] == 'queue_waiting'
                                           and bool(failed.intersection(('board', 'queue'))))

    findings = [dict(r, stale=True, safe_fix=False) if r['kind'] != 'patrol_error' and incomplete(r) else r
                for r in findings]
    known = {_key(r) for r in findings}
    for row in previous['findings']:
        if row['kind'] != 'patrol_error' and incomplete(row) and _key(row) not in known:
            findings.append(dict(row, stale=True, safe_fix=False))
    findings.sort(key=lambda r: (0 if r['kind'] == 'queue_waiting' else 1,
                                 -_number(r.get('age_s')) if r['kind'] == 'queue_waiting' else 0))
    return findings, overload, policy, host_status


def _fix(findings, policy, deadline):
    events = []
    if any(r['kind'] == 'custody_stale' and not r.get('stale') for r in findings):
        try:
            until = min(deadline, time.monotonic() + READ_BUDGET_S)
            rows = _bounded('release', lambda until=until: jobs.release_stale_writers(deadline=until), until, spawn=True)
            for row in rows:
                if row.get('released'):
                    events.append({'event': 'fixed', 'kind': 'custody_stale', 'id': row['id'], 'text': row['why']})
                elif row.get('error'):
                    findings.append(_error('release', OSError(row['error'])))
        except Exception as e:
            findings.append(_error('release', e))
    candidates = [r for r in findings if r['source'] == 'queue' and r['safe_fix'] and not r.get('stale')]
    # A malformed claim without its timestamp is not recognized by the pump's replay guard.
    # Leave all pumping to a person rather than risk replaying that claimed request.
    if candidates and not any(r['kind'] == 'queue_claimed' and r.get('claim_only') for r in findings):
        try:
            until = min(deadline, time.monotonic() + READ_BUDGET_S)
            result = _bounded('pump', lambda until=until: wip.pump_quietly(policy, deadline=until), until, spawn=True)
            if result is None:
                findings.append(_error('pump', RuntimeError('pump did not complete; inspect queue')))
            if result is not None:
                launched = {r['id'] for r in result.get('launched', []) if r.get('launched')}
                skipped = {r['id']: r.get('why') for r in result.get('skipped', [])}
                for row in candidates:
                    if row['id'] in launched:
                        row.update(kind='queue_stalled', condition='pump_confirmed', severity='warning')
                        row.pop('pump_reason', None)
                        events.append({'event': 'fixed', 'kind': 'queue_stalled', 'id': row['id'], 'text': 'request pumped'})
                    else:
                        reason = (skipped.get(row['id']) or result.get('note') or
                                  '; '.join(str(why) for why in skipped.values() if why) or
                                  'pump launched no request for this row')
                        row.update(kind='queue_waiting', text=reason, condition=_reason_category(reason),
                                   detail=reason, pump_reason=reason)
        except Exception as e:
            findings.append(_error('pump', e))
    return events


def _history(events, deadline):
    import json
    path = os.path.join(common.STATE, 'patrol.jsonl')
    if not events:
        return
    # Stream old history: memory stays bounded even after a previously uncapped history.
    try:
        with open(path, encoding='utf-8') as h:
            lines = deque(maxlen=2000)
            for line in h:
                if time.monotonic() >= deadline:
                    raise TimeoutError('history budget exhausted')
                lines.append(line)
    except FileNotFoundError:
        lines = deque(maxlen=2000)
    with open(path, 'a', encoding='utf-8') as h:
        for event in events:
            h.write(json.dumps(baton.redact_obj(event), ensure_ascii=False) + '\n')
            lines.append(json.dumps(baton.redact_obj(event), ensure_ascii=False) + '\n')
    if len(lines) >= 2000:
        common._atomic_write(path, lambda h: h.write(''.join(lines)), deadline)


def run_once(fix=False, as_json=False, quiet=False):
    deadline = time.monotonic() + PASS_BUDGET_S
    state_error = None
    try:
        previous = _bounded('state', _state, deadline)
    except Exception as e:
        previous, state_error = _initial(), _error('state', e)
    try:
        until = deadline - 3
        findings, overload, policy, host_status = _bounded('collect', lambda: collect(previous, until), until, PASS_BUDGET_S)
    except Exception as e:
        findings, overload, policy = ([_error('collect', e)] + [dict(r, stale=True, safe_fix=False) for r in previous['findings']],
                                     previous['overload_passes'], {})
        host_status = 'unknown'
    if state_error is not None:
        findings = [state_error] + [dict(r, stale=True, safe_fix=False) for r in findings]
    fixed = _fix(findings, policy, deadline - 1) if fix and state_error is None else []
    findings = baton.redact_obj(findings)
    at = common.iso(common.now())
    old = {_key(r): r for r in previous['findings']}
    current, seen, events, printed = {}, {}, list(fixed), []
    for row in findings:
        key = _key(row)
        if key in current:
            continue
        record = dict(previous['seen'].get(key) or {'first_at': at, 'last_at': at, 'reminded_at': at})
        new = key not in old
        reminder = not new and (common.age_s(record['reminded_at']) or 0) >= REMINDER_S
        row['new'] = new
        current[key] = row
        if new or reminder:
            running = ''.join(f"; {j['id']} age {common.fmt_age(j.get('age_s'))}" for j in row.get('running_jobs', []))
            printed.append(('reminder: ' if reminder else '') + f"{row['kind']} {row['id']} ({common.fmt_age(row.get('age_s'))}): "
                           + row['text'] + (('; ' + row['detail']) if row.get('detail') else '')
                           + running + ' -> ' + row['remedy'])
            record['reminded_at'] = at
        if new:
            events.append({'event': 'opened', 'kind': row['kind'], 'id': row['id'], 'text': row['text']})
        record['last_at'] = at
        seen[key] = record
    for key, row in old.items():
        if key not in current:
            printed.append(f"resolved: {row['kind']} {row['id']}: {row['text']}")
            events.append({'event': 'resolved', 'kind': row['kind'], 'id': row['id'], 'text': row['text']})
    for event in fixed:
        printed.append(f"fixed: {event['kind']} {event['id']}: {event['text']}")
    rows = list(current.values())
    state = {'version': 1, 'at': at, 'findings': rows, 'seen': seen, 'overload_passes': overload,
             'host_status': host_status}
    for name, action in (
            ('write', lambda: common.write_json(os.path.join(common.STATE, 'patrol.json'), state, deadline=deadline)),
            ('history', lambda: _history([dict(e, at=at) for e in events], deadline))):
        try:
            if state_error is None:
                _bounded(name, action, deadline)
        except Exception as e:
            error = baton.redact_obj(_error(name, e))
            rows.append(dict(error, new=True))
            printed.append(error['text'])
    payload = {'version': 1, 'at': at, 'findings': rows, 'overload_passes': overload, 'fixed': baton.redact_obj(fixed),
               'skipped': SKIPPED, 'host_status': host_status}
    if as_json:
        common.emit(baton.redact_obj(payload), True)
    elif printed or not quiet:
        common.emit(payload, False, baton.redact('\n'.join(printed) if printed else 'patrol: nothing new'))
    return 0


def cli(argv):
    ap = argparse.ArgumentParser(prog='twinlane patrol')
    ap.add_argument('--fix', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--loop', type=float)
    ap.add_argument('--max-runs', type=int)
    ap.add_argument('--quiet', action='store_true')
    a = ap.parse_args(argv)
    if a.loop is not None and (not math.isfinite(a.loop) or a.loop < 30):
        ap.error('--loop must be at least 30 seconds')
    if a.max_runs is not None and a.max_runs < 1:
        ap.error('--max-runs must be positive')
    if a.fix:
        from . import projects
        refused = projects.refuse_in_worker('patrol --fix')
        if refused is not None:
            return refused
    count = 0
    try:
        while True:
            try:
                run_once(a.fix, a.json, a.quiet)
            except Exception as e:  # one damaged pass must not stop a loop
                error = baton.redact_obj(_error('pass', e))
                common.emit({'findings': [dict(error, new=True)]}, a.json, baton.redact(error['text']))
            count += 1
            if a.loop is None or a.max_runs is not None and count >= a.max_runs:
                return 0
            time.sleep(a.loop)
    except KeyboardInterrupt:
        return 0
