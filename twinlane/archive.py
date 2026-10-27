"""Conservative, reversible archival of collected job evidence."""
from contextlib import ExitStack
import argparse
import json
import math
import os
import re
from collections import Counter

from . import baton, common, jobs, observations

TERMINAL = {'done', 'failed', 'timeout', 'stopped', 'refused'}
MONTH = re.compile(r'^\d{4}-(?:0[1-9]|1[0-2])$')


def archived_ids():
    """The directory layout also resolves jobs if an index append was interrupted."""
    root = os.path.join(common.JOBS, 'archive')
    try:
        months = os.listdir(root)
    except FileNotFoundError:
        return {}
    found = {}
    for month in sorted(months):
        if not MONTH.fullmatch(month):
            continue
        directory = os.path.join(root, month)
        for jid in os.listdir(directory):
            path = os.path.join(directory, jid)
            if baton.JOB_ID_RE.fullmatch(jid) and os.path.isdir(path):
                found[jid] = path
    return found


def _json(path, optional=False):
    try:
        with open(path, encoding='utf-8') as handle:
            value = json.load(handle)
    except FileNotFoundError:
        if optional:
            return {}
        raise ValueError('missing ' + os.path.basename(path)) from None
    except (OSError, ValueError, RecursionError):
        raise ValueError('unreadable ' + os.path.basename(path)) from None
    if not isinstance(value, dict):
        raise ValueError('invalid ' + os.path.basename(path))
    return value


def _baton_slugs():
    try:
        names = os.listdir(common.BATONS)
    except FileNotFoundError:
        return []
    return sorted(name[:-5] for name in names if name.endswith('.json'))


def _baton_check(jid, spec):
    owners = set()
    for slug in _baton_slugs():
        if not baton.valid_slug(slug):
            raise ValueError('invalid baton filename')
        try:
            record = baton.load(slug)
        except (OSError, ValueError, TypeError, AttributeError, baton.BatonError, RecursionError):
            raise ValueError('unreadable baton ' + slug) from None
        if record is None:
            raise ValueError('unreadable baton ' + slug)
        progress = record.get('progress')
        entries = record.get('jobs')
        if not isinstance(progress, dict) or not isinstance(progress.get('in_flight'), list) \
                or not isinstance(entries, list) or any(not baton._valid_job_entry(j) for j in entries):
            raise ValueError('invalid baton ' + slug)
        flights = progress['in_flight']
        if any(not isinstance(f, dict) or (f.get('job') is not None and not isinstance(f['job'], str))
               for f in flights):
            raise ValueError('invalid baton ' + slug)
        if any(f.get('job') == jid for f in flights) \
                or any(j['id'] == jid and baton._is_live_job(j) for j in entries):
            return 'baton in flight: ' + slug
        if record.get('waiting_on') == 'job:' + jid:
            return 'owner baton waiting: ' + slug
        owners.add(slug)
    owner = spec.get('owner')
    if os.path.lexists(jobs._p(jid, jobs.OWNER_PENDING)):
        return 'owner completion pending'
    if owner:
        if not isinstance(owner, str) or owner not in owners:
            return 'owner baton missing or unreadable'
        notified = _json(jobs._p(jid, jobs.OWNER_NOTIFIED))
        if notified.get('owner') != owner or notified.get('job_id') != jid:
            return 'owner completion unverified'
    return None


def _dead_evidence(jid, st):
    # Even a stale lock is retained. Archival never takes over a runner role.
    if os.path.lexists(jobs._p(jid, jobs.RUNNER_LOCK)):
        raise ValueError('runner lock present')
    for pid_key, stamp_key in (('pid', 'pid_created'), ('child_pid', 'child_created')):
        if st.get(pid_key) is not None or st.get(stamp_key) is not None:
            if not common._valid_pid(st.get(pid_key)) or not common._valid_stamp(st.get(stamp_key)):
                raise ValueError('invalid process identity: ' + pid_key)
    runner = jobs._pid_state(st.get('pid'), st.get('pid_created'))
    pid_path = jobs._p(jid, 'runner.pid')
    try:
        with open(pid_path, encoding='ascii') as handle:
            parts = handle.read().split()
    except FileNotFoundError:
        parts = None
    if parts is not None:
        if len(parts) != 2 or not all(p.isascii() and p.isdigit() for p in parts) \
                or not common._valid_pid(int(parts[0])) or not common._valid_stamp(int(parts[1])):
            raise ValueError('invalid runner identity')
        pid, created = map(int, parts)
        state = jobs._pid_state(pid, created)
        if state != common.DEAD:
            raise ValueError('runner ' + state)
    if runner != common.DEAD:
        raise ValueError('runner ' + runner)
    if os.path.lexists(jobs._p(jid, jobs.CHILD_STARTING)):
        _json(jobs._p(jid, jobs.CHILD_STARTING))
        raise ValueError('engine starting')
    for marker in (jobs.SURVIVOR,):
        if os.path.lexists(jobs._p(jid, marker)):
            identity = _json(jobs._p(jid, marker))
            if not common._valid_pid(identity.get('pid')) or not common._valid_stamp(identity.get('created')):
                raise ValueError('invalid ' + marker)
    with observations.scope():
        engine = jobs._engine_state(jid, st)
    if engine != common.DEAD:
        raise ValueError('engine ' + engine)


def quiescence():
    """All live-folder evidence must be terminal and dead, including legacy jobs."""
    blocked = []
    try:
        ids = sorted(name for name in os.listdir(common.JOBS)
                     if name != 'archive' and os.path.isdir(os.path.join(common.JOBS, name)))
    except OSError:
        return [{'reason': 'cannot read jobs directory'}]
    for jid in ids:
        try:
            if not observations.JOB_DIRECTORY_RE.fullmatch(jid):
                raise ValueError('nonmatching job folder; ownership unknown')
            live = os.path.join(common.JOBS, jid)
            _json(os.path.join(live, 'job.json'))
            receipt = _json(os.path.join(live, 'result.json'))
            st = _json(os.path.join(live, 'status.json'), optional=True)
            if receipt.get('status') not in jobs.TERMINAL:
                raise ValueError('job is not terminal')
            if st.get('status') in ('orphaned', 'unknown', 'starting'):
                raise ValueError('job status ' + st['status'])
            # A terminal receipt next to a status.json still reading queued/running is the normal leftover of
            # older runners (they never rewrote status.json after the receipt). It is quiescent only on the
            # same proof as any other job: runner, runner.pid, launch marker, survivor and engine all DEAD.
            _dead_evidence(jid, st)
        except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
            blocked.append({'id': jid, 'reason': str(error) if isinstance(error, ValueError)
                            else 'unreadable job evidence'})
    return blocked


def eligibility(jid, days, include_unread):
    row = {'id': jid}
    try:
        if not baton.JOB_ID_RE.fullmatch(jid):
            raise ValueError('noncanonical job id; retained in active listings')
        if os.path.islink(os.path.join(common.JOBS, jid)):
            raise ValueError('job directory is a symlink')
        receipt = _json(jobs._p(jid, 'result.json'))
        spec = _json(jobs._p(jid, 'job.json'))
        st = _json(jobs._p(jid, 'status.json'), optional=True)
        ended = receipt.get('ended_at') or receipt.get('finished_at')
        when = common.parse_time(ended)
        row.update(name=receipt.get('name') or spec.get('name'), status=receipt.get('status'), ended_at=ended)
        if receipt.get('id') != jid:
            raise ValueError('receipt id mismatch')
        if receipt.get('status') not in TERMINAL:
            raise ValueError('no archivable terminal receipt')
        if when is None:
            raise ValueError('invalid completion time')
        age = (common.now() - when).total_seconds() / 86400
        row['age_days'] = round(age, 3)
        if age <= days:
            raise ValueError('recent')
        # Owner completion notices stay in the inbox until result collects them,
        # even after owner_job changes waiting_on from job:<id> to lead.
        if not include_unread or spec.get('owner'):
            try:
                with open(jobs._p(jid, jobs.COLLECTED), encoding='utf-8') as handle:
                    collected = common.parse_time(handle.read().strip())
            except FileNotFoundError:
                raise ValueError('owner receipt not collected' if include_unread and spec.get('owner')
                                 else 'unread receipt') from None
            if collected is None or collected < when:
                raise ValueError('invalid or stale read marker')
        _dead_evidence(jid, st)
        if isinstance(spec.get('wip_claim'), str):
            from . import wip
            if wip.outstanding_claim(spec['wip_claim']):
                raise ValueError('writer-queue request not yet acknowledged')  # the pump's only admission proof
        why = _baton_check(jid, spec)
        if why:
            raise ValueError(why)
    except (OSError, ValueError, TypeError, OverflowError, RecursionError) as error:
        row['reason'] = str(error) if isinstance(error, ValueError) else 'unreadable job evidence'
    return row


def _append_index(row):
    index = os.path.join(common.JOBS, 'archive', 'index.jsonl')
    # The global archive lock serializes dedupe and append. A complete but not
    # fsynced line is retained; scanning the layout remains authoritative.
    try:
        with open(index, encoding='utf-8') as handle:
            for line in handle:
                try:
                    if json.loads(line).get('id') == row['id']:
                        return
                except (ValueError, AttributeError):
                    continue
    except FileNotFoundError:
        pass
    entry = {k: row[k] for k in ('id', 'name', 'status', 'ended_at')}
    entry['archived_at'] = common.iso(common.now())
    with open(index, 'a', encoding='utf-8') as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
        handle.flush()
        os.fsync(handle.fileno())


def _move(jid, days, include_unread):
    with common.file_lock(os.path.join(common.STATE, 'jobs-archive.lock'), 0), jobs.archive_lock(jid, 0):
        active = os.path.join(common.JOBS, jid)
        if not os.path.isdir(active) and jid in archived_ids():
            return {'id': jid, 'archived': True, 'already_archived': True}
        blocked = quiescence()
        if blocked:
            return {'id': jid, 'reason': 'apply blocked: live jobs are not terminal and dead',
                    'apply_blocked': blocked}
        # Old runners take only these inner locks. Probe both while holding the
        # outer lock, then CLOSE their handles before rename (required on Windows).
        with ExitStack() as locks:
            for name in ('runner.claim', 'status.lock'):
                _, inner = jobs.lock_paths(jid, name)
                locks.enter_context(common.file_lock(inner, 0, create_parent=False))
            row = eligibility(jid, days, include_unread)
            if row.get('reason'):
                return row
        month = common.parse_time(row['ended_at']).strftime('%Y-%m')
        destination = os.path.join(common.JOBS, 'archive', month, jid)
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        if os.path.lexists(destination):
            return dict(row, reason='archive destination exists')
        # Dependency checks and replace share the outer lock with baton saves.
        # Never take baton locks here: their writers take those before job locks.
        blocked = quiescence()
        if blocked:
            return dict(row, reason='apply blocked: live jobs are not terminal and dead', apply_blocked=blocked)
        row = eligibility(jid, days, include_unread)
        if row.get('reason'):
            return row
        # A straggling old-code inner holder makes this fail on Windows: keep the
        # folder in place and report the failure, never remove locks to force it.
        os.replace(active, destination)
        try:
            _append_index(row)
        except (OSError, ValueError):
            return dict(row, archived=True, warning='index entry uncertain')
        return dict(row, archived=True)


def cli(argv):
    parser = argparse.ArgumentParser(prog='twinlane jobs archive')
    parser.add_argument('--older-than', type=float)
    parser.add_argument('--include-unread', action='store_true')
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = jobs._parse(parser, argv)
    if isinstance(args, int):
        return args
    days = args.older_than
    if days is None:
        days = common.load_policy().get('jobs', {}).get('archive_after_days', 14)
    if isinstance(days, bool) or not isinstance(days, (int, float)) or not math.isfinite(days) or days < 0:
        print('twinlane jobs archive: days must be a finite nonnegative number')
        return common.EXIT_USAGE
    candidates, skipped, moved, errors = [], [], [], []
    try:
        ids = jobs._job_ids()
    except OSError:
        ids = []
        errors.append({'reason': 'cannot read jobs directory'})
    blocked = quiescence()
    for jid in ids:
        row = eligibility(jid, days, args.include_unread)
        if row.get('reason'):
            skipped.append(row)
            continue
        candidates.append(row)
    if args.apply and not blocked and not errors:
        for candidate in candidates:
            jid = candidate['id']
            try:
                row = _move(jid, days, args.include_unread)
                (skipped if row.get('reason') else moved).append(row)
                if row.get('apply_blocked'):
                    blocked = row['apply_blocked']
                    break
            except (OSError, ValueError) as error:
                errors.append({'id': jid, 'reason': 'archive failed; folder retained (possibly busy): '
                               + type(error).__name__})
    counts = dict(Counter(row['reason'] for row in skipped))
    data = dict(apply=args.apply, older_than_days=days, candidates=candidates, skipped=skipped,
                skipped_counts=counts, moved=moved, errors=errors, apply_blocked=blocked)
    if args.json:
        common.emit(data, True)
    else:
        print(f"{'Apply' if args.apply else 'Dry run'}: {len(candidates)} candidates, {len(moved)} archived, "
              f'{len(skipped)} skipped, {len(errors)} errors')
        if blocked:
            print('Apply blocked: all live jobs must be terminal and dead')
            for item in blocked[:5]:
                print(f"{item.get('id', 'jobs')}: {item['reason']}")
        for row in candidates:
            print(f"{row['id']}  {row['name'] or ''}  {row['status']}  ended_at={row['ended_at']}  age={row['age_days']}d")
        for reason, count in counts.items():
            examples = [row['id'] for row in skipped if row['reason'] == reason][:3]
            print(f"skip {reason}: {count} ({', '.join(examples)})")
        for row in moved:
            if row.get('warning'):
                print(f"{row['id']}: {row['warning']}")
        for error in errors:
            print(f"{error.get('id', 'archive')}: {error['reason']}")
    if args.apply and blocked:
        return common.EXIT_REFUSED
    return common.EXIT_FAILED if errors or any(r.get('warning') for r in moved) else common.EXIT_OK
