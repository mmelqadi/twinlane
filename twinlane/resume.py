"""twinlane resume: restart jobs that were lost, typically to a host restart.

Never automatic: there is no logon task and nothing calls this on its own. The default is a dry run that
lists the jobs whose status is exactly `lost` (never unknown, orphaned or running: those may still be
alive, and resuming them would start a second worker on the same work). `--yes` re-runs each selected
job's original request as a continuation (`twinlane run --continue-job ID`), one at a time, and stops at
the first refusal. Each resumed job gets jobs/<id>/resumed.json, so it is never resumed twice.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import subprocess
import sys
import tempfile
from contextlib import redirect_stderr, redirect_stdout

from . import common, host, jobs, projects, sessions

DEFAULT_SINCE = '48h'
DEFAULT_MAX_BATCH = 10
MARKER = 'resumed.json'
LOCK = 'resume.lock'


# ---------------------------------------------------------------- reading jobs

def _p(job_id: str, name: str) -> str:
    return jobs._p(job_id, name)


def max_batch(policy: dict) -> int:
    value = (policy.get('resume') or {}).get('max_batch')
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 1 else DEFAULT_MAX_BATCH


def _start(spec: dict, status: dict, receipt) -> object:
    """When the job started, as the listing sees it: the receipt, then the status file, then the spec."""
    return (receipt or {}).get('started_at') or (status or {}).get('started_at') or spec.get('created_at')


def _caller_brief(spec: dict) -> tuple:
    """(text, source). The brief as the caller wrote it; legacy jobs only kept brief_text, which for a job
    that was itself a continuation also holds the earlier context."""
    text = spec.get('caller_brief')
    if isinstance(text, str) and text.strip():
        return text, 'caller_brief'
    text = spec.get('brief_text')
    if isinstance(text, str) and text.strip():
        return text, 'brief_text'
    return None, None


def continuations(min_id: str = '') -> dict:
    """{job id: id of a job that continues it}. A continuation that ended `refused` never started any
    work and does not count. Only jobs newer than `min_id` are read: a continuation is created later."""
    found = {}
    for jid in jobs._job_ids():
        if jid <= min_id:
            continue
        spec = common.read_json(_p(jid, 'job.json'), {})
        prior = spec.get('continue_job') if isinstance(spec, dict) else None
        if not prior:
            continue
        receipt = common.read_json(_p(jid, 'result.json'))
        if isinstance(receipt, dict) and receipt.get('status') == 'refused':
            continue
        found.setdefault(prior, jid)
    return found


def build_argv(jid: str, spec: dict) -> list:
    """The `twinlane run` arguments that continue job `jid` from its recorded request."""
    argv = ['--continue-job', jid, '--brief', _p(jid, jobs.CALLER_BRIEF), '--cd', spec['cwd'],
            '--kind', spec.get('kind') or ('code' if spec.get('write') else 'intel')]
    if spec.get('write'):
        argv.append('--write')
    argv += ['--engine', 'auto', '--bg', '--name', common.clip('%s-resume' % (spec.get('name') or jid), 60)]
    if spec.get('baton'):
        argv += ['--baton', spec['baton']]
    return argv


def inspect(jid: str, boot=host._UNSET, continued=None) -> dict:
    """One job's resume verdict: {'id', 'eligible', 'reason' (when not), and the row fields when eligible}."""
    spec = common.read_json(_p(jid, 'job.json'), {})
    if not isinstance(spec, dict) or spec.get('id') != jid:
        return {'id': jid, 'eligible': False, 'reason': 'no readable job.json'}
    status = jobs.job_status(jid)
    if status != 'lost':
        return {'id': jid, 'eligible': False, 'status': status,
                'reason': 'status is %s, not lost; only a lost job is ever resumed' % status}
    marker = common.read_json(_p(jid, MARKER))
    if isinstance(marker, dict):
        return {'id': jid, 'eligible': False, 'status': status,
                'reason': 'already resumed as %s' % (marker.get('resumed_as') or '?')}
    if continued is None:
        continued = continuations(jid)
    if jid in continued:
        return {'id': jid, 'eligible': False, 'status': status,
                'reason': 'already continued by %s' % continued[jid]}
    cwd = spec.get('cwd')
    if not cwd or not os.path.isdir(cwd):
        return {'id': jid, 'eligible': False, 'status': status, 'reason': 'working directory is gone: %s' % cwd}
    text, source = _caller_brief(spec)
    if text is None:
        return {'id': jid, 'eligible': False, 'status': status, 'reason': 'no brief was recorded'}
    st = common.read_json(_p(jid, 'status.json'), {}) or {}
    started = _start(spec, st, common.read_json(_p(jid, 'result.json')))
    cause = host.lost_cause({'started_at': started}, boot)
    dirty = jobs._porcelain(cwd)
    return {'id': jid, 'eligible': True, 'status': status, 'name': spec.get('name'), 'kind': spec.get('kind'),
            'engine': (spec.get('primary') or {}).get('engine') or spec.get('engine'), 'cwd': cwd,
            'write': bool(spec.get('write')), 'baton': spec.get('baton'), 'started_at': started,
            'cause': cause, 'dirty_files': None if dirty is None else len(dirty), 'brief_source': source,
            'brief_text': text, 'argv': build_argv(jid, spec)}


def select(refs: list, since_s, boot=host._UNSET) -> tuple:
    """(eligible rows oldest first, skipped [{'id', 'reason'}]). Named jobs ignore the window."""
    if boot is host._UNSET:
        boot = host.boot_time()
    skipped, ids = [], []
    if refs:
        for ref in refs:
            try:
                ids.append(jobs.resolve_job(ref))
            except LookupError as e:
                skipped.append({'id': ref, 'reason': str(e), 'named': True})
    else:
        ids = list(jobs._job_ids())
    continued = continuations(min(ids)) if ids else {}
    rows = []
    for jid in sorted(set(ids)):
        if not refs and since_s is not None:
            spec = common.read_json(_p(jid, 'job.json'), {}) or {}
            age = common.age_s(_start(spec, common.read_json(_p(jid, 'status.json'), {}) or {},
                                      common.read_json(_p(jid, 'result.json'))))
            if age is None or age > since_s:
                continue
        verdict = inspect(jid, boot, continued)
        if verdict['eligible']:
            rows.append(verdict)
        elif refs:
            skipped.append(dict(verdict, named=True))
        elif verdict.get('status') == 'lost':  # a lost job we will not resume is worth saying why
            skipped.append(dict(verdict, named=False))
    return rows, skipped


# ---------------------------------------------------------------- launching

def launch(argv: list) -> tuple:
    """Run `twinlane run ARGV --json` in this process: (exit code, JSON payload or {}, stderr text).

    Going through jobs.cli keeps the meter check, the routing refusal, the credit guard and the writer
    admission exactly as for a hand-typed run. Tests replace this."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = jobs.cli('run', list(argv) + ['--json'])
    try:
        payload = json.loads(out.getvalue())
    except ValueError:
        payload = {}
    return rc, payload if isinstance(payload, dict) else {}, err.getvalue().strip()


def _route_line(row: dict) -> str:
    """What the router would do now, from a dry run. The brief goes through a scratch file, so a listing
    writes nothing into a job folder (a legacy job has no caller-brief.md yet)."""
    fd, scratch = tempfile.mkstemp(prefix='twinlane-resume-', suffix='.md')
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='') as h:
            h.write(row['brief_text'])
        argv = list(row['argv'])
        argv[argv.index('--brief') + 1] = scratch
        rc, payload, err = launch(argv + ['--dry-run'])
    finally:
        try:
            os.unlink(scratch)
        except OSError:
            pass
    if payload.get('refused') or rc not in (common.EXIT_OK, common.EXIT_MISSING):
        why = payload.get('reason') or err or 'twinlane run exited %s' % rc
        return 'WOULD BE REFUSED: ' + common.clip(why, 300)
    return '%s (profile %s)' % (payload.get('engine'), payload.get('profile'))


def _ensure_brief_file(row: dict) -> None:
    path = row['argv'][row['argv'].index('--brief') + 1]
    try:
        with open(path, 'rb') as h:
            if jobs.decode_text(h.read()) == row['brief_text']:
                return
    except OSError:
        pass
    common.write_text(path, row['brief_text'])


def resume_one(row: dict) -> tuple:
    """Continue one job. (started, detail). Re-checks under the job's lock that nobody else did."""
    jid = row['id']
    try:
        with common.file_lock(_p(jid, LOCK), 30):
            again = inspect(jid)
            if not again['eligible']:
                return False, {'id': jid, 'reason': 'no longer resumable: ' + again['reason']}
            _ensure_brief_file(again)
            rc, payload, err = launch(again['argv'])
            new_id = payload.get('id')
            if rc != 0 or not new_id or payload.get('refused'):
                reason = payload.get('reason') or err or 'twinlane run exited %s' % rc
                return False, {'id': jid, 'exit': rc, 'reason': common.clip(reason, 400)}
            common.write_json(_p(jid, MARKER), {'resumed_as': new_id, 'resumed_at': common.iso(common.now()),
                                                'cause': again['cause'], 'argv': again['argv']})
            return True, {'id': jid, 'resumed_as': new_id}
    except TimeoutError:
        return False, {'id': jid, 'reason': 'another resume holds this job; try again'}


# ---------------------------------------------------------------- command

def _public(row: dict) -> dict:
    return {k: v for k, v in row.items() if k != 'brief_text'}


def _cause_text(row: dict) -> str:
    if row['cause'] == host.UNKNOWN:
        return 'unknown (the host boot time or the job start is unavailable)'
    return row['cause']


def _render(rows: list, skipped: list, deferred: list, since: str, window: bool, batch: int) -> str:
    scope = 'started in the last %s' % since if window else 'named'
    lines = ['%d lost job(s) %s; nothing was started (dry run)' % (len(rows), scope)]
    for r in rows:
        dirty = 'not a git repo' if r['dirty_files'] is None else '%d changed path(s)' % r['dirty_files']
        lines += ['  %s  %s  %s  %s' % (r['id'], r.get('engine') or '?', r.get('kind') or '?', r.get('name') or ''),
                  '      cause: %s   worktree: %s   brief: %s' % (_cause_text(r), dirty, r['brief_source']),
                  '      cwd: %s' % r['cwd'], '      route: %s' % r['route'],
                  '      argv: ' + subprocess.list2cmdline(['twinlane', 'run'] + r['argv'])]
        if r['brief_source'] == 'brief_text':
            lines.append('      note: recorded before caller briefs were kept; brief_text is used (a job that '
                         'was itself a continuation also carries its earlier context)')
    if deferred:
        lines.append('%d more beyond resume.max_batch=%d: %s' % (len(deferred), batch, ' '.join(deferred)))
    for s in skipped:
        lines.append('skipped %s: %s' % (s['id'], s['reason']))
    if rows:
        lines.append('next: twinlane resume --yes ' + ' '.join(r['id'] for r in rows[:batch]))
    return '\n'.join(lines)


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane resume', description='Resume jobs whose status is lost. '
                                 'Dry run unless --yes. Never automatic.')
    ap.add_argument('jobs', nargs='*', metavar='JOB',
                    help='job id or unique fragment (the --since window does not apply to named jobs)')
    ap.add_argument('--all-lost', action='store_true', help='select every lost job inside the --since window')
    ap.add_argument('--since', default=DEFAULT_SINCE,
                    help='look-back window: 90m, 24h, 2d, 1w (default %s)' % DEFAULT_SINCE)
    ap.add_argument('--yes', action='store_true', help='start the continuations; without it, list only')
    ap.add_argument('--json', action='store_true')
    return ap


def cli(argv) -> int:
    try:
        args = _parser().parse_args(list(argv))
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    if args.yes:
        refused = projects.refuse_in_worker('resume --yes')
        if refused is not None:
            return refused
        if not args.jobs and not args.all_lost:
            print('twinlane resume: --yes needs JOB ids or --all-lost; run without --yes to list first',
                  file=sys.stderr)
            return common.EXIT_USAGE
    try:
        since_s = sessions.parse_since(args.since)
    except ValueError as e:
        print('twinlane resume: %s' % e, file=sys.stderr)
        return common.EXIT_USAGE
    policy = common.load_policy()
    batch = max_batch(policy)
    rows, skipped = select(args.jobs, since_s)
    named_bad = [s for s in skipped if s.get('named')]
    if args.yes and named_bad:
        for s in named_bad:
            print('twinlane resume: %s: %s' % (s['id'], s['reason']), file=sys.stderr)
        print('twinlane resume: nothing was started; fix the selection or drop the job', file=sys.stderr)
        return common.EXIT_REFUSED
    take, deferred = rows[:batch], [r['id'] for r in rows[batch:]]
    window = not args.jobs
    if not args.yes:
        for r in take:
            r['route'] = _route_line(r)
        out = {'dry_run': True, 'max_batch': batch, 'candidates': [_public(r) for r in take],
               'deferred': deferred, 'skipped': skipped}
        if args.json:
            common.emit(out, True)
        else:
            print(_render(take, skipped, deferred, args.since, window, batch))
        return common.EXIT_OK
    started, stopped, untouched = [], None, []
    for i, r in enumerate(take):
        ok, detail = resume_one(r)
        if ok:
            started.append(detail)
            continue
        stopped = detail
        untouched = [x['id'] for x in take[i + 1:]]
        break
    out = {'dry_run': False, 'started': started, 'stopped': stopped, 'not_attempted': untouched + deferred,
           'skipped': skipped}
    if args.json:
        common.emit(out, True)
    else:
        for s in started:
            print('resumed %s as %s   next: twinlane wait %s' % (s['id'], s['resumed_as'], s['resumed_as']))
        if stopped:
            print('stopped at %s: %s' % (stopped['id'], stopped['reason']), file=sys.stderr)
            if out['not_attempted']:
                print('not attempted: ' + ' '.join(out['not_attempted']), file=sys.stderr)
        elif deferred:
            print('%d more beyond resume.max_batch=%d; run again to continue: %s'
                  % (len(deferred), batch, ' '.join(deferred)))
        if not started and not stopped:
            print('nothing to resume')
    return common.EXIT_REFUSED if stopped else common.EXIT_OK
