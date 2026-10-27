"""Opt-in, fixed completion sinks. No command, plugin or trust-registry execution."""
import argparse
import base64
import json
import os
import subprocess
import sys

from . import common

# Long enough for a loaded host (four 4 KB appends missed 1 s at ~130 console hosts), and still a bound:
# notify() runs after finalization and is never retried, so a lock timeout loses the row.
APPEND_LOCK_S = 5


def _path():
    return os.path.join(common.STATE, 'completions.jsonl')


def _note(jid, message):
    from . import jobs
    try:
        # Never recreate a job moved by archival, or rewrite its evidence.
        with open(jobs._p(jid, 'runner.log'), 'a', encoding='utf-8') as handle:
            handle.write('completion sink: ' + message + '\n')
    except OSError:
        pass


def _append(row):
    line = (json.dumps(row, ensure_ascii=True, separators=(',', ':')) + '\n').encode('utf-8')
    with common.file_lock(os.path.join(common.STATE, 'completions.lock'), APPEND_LOCK_S, poll_s=0.01):
        with open(_path(), 'a+b') as handle:
            # A previous failed append may have left a partial tail. Preserve it, but do not
            # join the next record onto it. Readers ignore that incomplete JSON line.
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b'\n':
                    handle.write(b'\n')
            handle.write(line)
            handle.flush()


def _toast(row):
    if not common.IS_WIN:
        return False
    from . import completion_toast
    payload = completion_toast.payload(row['id'], row['name'], row['status'])
    python = sys.executable
    windowless = os.path.join(os.path.dirname(python), 'pythonw.exe')
    if os.path.isfile(windowless):
        python = windowless
    from . import jobs
    # A detached, windowless supervisor owns the hard timeout, even after the runner exits.
    # If Windows refuses breakaway from containment, skip; never change containment policy.
    subprocess.Popen([python, os.path.abspath(completion_toast.__file__),
                      base64.b64encode(payload).decode('ascii'), jobs._p(row['id'], 'runner.log')],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True, creationflags=common.CREATE_NO_WINDOW | 0x01000000)
    return True


def notify(jid, receipt, policy=None):
    """Called once by the publishing runner, after final receipt writes and lock release.

    Duplicate runners/readers/owner repairs never call this. There is no replay or retry of
    a completed job to recover a failed sink, and no notification work when disabled.
    """
    from . import jobs
    if not isinstance(receipt, dict) or receipt.get('id') != jid \
            or not isinstance(receipt.get('status'), str) or receipt['status'] not in jobs.TERMINAL:
        return
    try:
        if policy is None:
            policy = common.load_policy()
    except (Exception, SystemExit):
        _note(jid, 'settings unreadable; skipped')
        return
    settings = policy.get('notify')
    if not isinstance(settings, dict):
        return
    log, toast = settings.get('completions_log') is True, settings.get('toast') is True
    if not log and not toast:
        return
    # Read the published bytes, never a pending in-memory receipt or any worker output.
    final = common.read_json(jobs._p(jid, 'result.json'))
    if not isinstance(final, dict) or final.get('id') != jid or final.get('status') != receipt['status']:
        _note(jid, 'matching final receipt unavailable; skipped')
        return
    # A terminal receipt is not a finished job: a surviving or UNKNOWN engine reads orphaned/unknown here.
    from . import observations
    try:
        with observations.scope():
            custody = jobs._receipt_status(jid, final)
    except Exception:
        custody = 'unknown'
    if custody != final['status']:
        _note(jid, f'engine custody reads {custody}; skipped')
        return
    spec = common.read_json(jobs._p(jid, 'job.json'), {}) or {}
    row = {key: final.get(key, spec.get(key)) for key in ('id', 'name', 'engine', 'kind')}
    row.update(status=final['status'], owner=spec.get('owner'),
               ended_at=final.get('ended_at') or final.get('finished_at'))
    if 'failure_class' in final:
        row['failure_class'] = final['failure_class']
    if log:
        try:
            _append(row)
        except Exception:
            _note(jid, 'completions log append failed; skipped')
    if toast:
        try:
            if not _toast(row):
                _note(jid, 'toast unavailable on this platform; skipped')
        except Exception:
            _note(jid, 'toast launch failed; skipped')


def cli(argv):
    from . import ack, baton, jobs
    ap = argparse.ArgumentParser(prog='twinlane completions')
    filters = ap.add_mutually_exclusive_group()
    filters.add_argument('--since')
    filters.add_argument('--unacked', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = jobs._parse(ap, argv)
    if isinstance(args, int):
        return args
    since = common.parse_time(args.since) if args.since is not None else None
    if args.since is not None and since is None:
        print('twinlane completions: --since requires an ISO timestamp', file=sys.stderr)
        return common.EXIT_USAGE
    rows = []
    try:
        try:
            handle = open(_path(), encoding='utf-8')
        except FileNotFoundError:
            handle = None
        if handle is not None:
            with handle:
                for line in handle:
                    try:
                        row = json.loads(line)
                    except ValueError:
                        continue  # includes a concurrently written partial tail
                    if not isinstance(row, dict) or not isinstance(row.get('id'), str) \
                            or not baton.JOB_ID_RE.fullmatch(row['id']) \
                            or not isinstance(row.get('status'), str) or row['status'] not in jobs.TERMINAL:
                        continue
                    ended = common.parse_time(row.get('ended_at'))
                    if since is not None and (ended is None or ended < since):
                        continue
                    if args.unacked and ack.collected(row['id'], row):
                        continue
                    rows.append(row)
    except (OSError, UnicodeError):
        print('twinlane completions: log unreadable', file=sys.stderr)
        return common.EXIT_FAILED
    if args.json:
        common.emit(rows, True)
    else:
        for row in rows:
            print(f"{row['id']}  {row['status']}  {row.get('engine') or ''}  "
                  + str(row.get('name') or '').encode('unicode_escape').decode('ascii'))
    return common.EXIT_OK
