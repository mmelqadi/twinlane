#!/usr/bin/env python3
"""twinlane - use Claude Code and Codex together: meter, route, run, baton, handoff.

  twinlane status                       both meters + running jobs + batons (one screen)
  twinlane status JOB                   job state, exit code, completion time and receipt
  twinlane watch [--name lead] [--timeout 3300] [--interval 5] [--json]   wait for coordination events
  twinlane patrol [--fix] [--json] [--loop SECONDS] [--max-runs N] [--quiet]   file-only work checks
  twinlane --version                    development version and best-effort source commit
  twinlane board [--json | --md] [--all] [--finished N] [--watch SECONDS]   one read-only screen for a lead session
  twinlane meter [--json] [--fresh]
  twinlane route <kind> [--urgent] [--engine claude|codex] [--json]
  twinlane run --brief F --cd DIR [--kind K] [--engine auto|claude|codex] [--write] [--bg] ...
  twinlane run --best-of ENGINE[,ENGINE...] --brief F --cd DIR [--kind K] ...   (same brief on several engines)
  twinlane compare GROUP [--wait] [--test] [--json|--md]                        (compare a --best-of group)
  twinlane land card|enqueue|status|next|run|release|dequeue|log ...   (land finished worktree branches, one at a time)
  twinlane ship plan|run ...                                      (release manifest for landed work)
  twinlane wt doctor [--fix]|prune ...                            (inspect, release stale claims, prune worktrees)
  twinlane wt gc [--repo DIR] [--apply]                            (zero-unique cleanup; dry run by default)
  twinlane wip [next] [--json]                                    (writer lanes and manual FIFO admission queue)
  twinlane inbox [--all] [--json] | inbox answer ID --grant WORDS | --deny REASON
  twinlane resume [JOB ...] [--all-lost] [--yes]                  (restart jobs lost to a host restart)
  twinlane jobs | wait JOB... [--any] [--timeout S] | result JOB [--full] | stop JOB
  twinlane jobs archive [--older-than DAYS] [--include-unread] [--apply] [--json]
  twinlane jobs reindex [--dry-run] [--json]
  twinlane ack JOB... | ack --older-than DAYS [--owner SLUG|--name GLOB] [--dry-run]
  twinlane completions [--since ISO|--unacked] [--json]          (read the optional completion log)
  twinlane trust list|add NAME PATH|remove NAME
  twinlane explain-writer-admission --cwd DIR [--job-id ID] [--json]   (read-only; a proposal, never a grant)
  twinlane baton new|set|snap|show|list ...
  twinlane handoff (SLUG | --cd DIR | --session ID) [--to claude|codex] [--out F]
  twinlane handoff --session ID [--session ID ...] [--to claude|codex] --out-dir DIR   (one file per session)
  twinlane sessions [--engine claude|codex|all] [--since 48h] [--cwd DIR] [--limit N] [--jobs] [--json|--md]
  twinlane hook                          (UserPromptSubmit/PreCompact/lane state hook; reads JSON on stdin)
  twinlane guard                         (PreToolUse guard inside twinlane jobs only; reads JSON on stdin)

Never blocked: ask one question. An inbox yes is not a live-write approval.

kinds: code intel plan review (Claude first) | parallel sweep integrate verify
Policy: packaged defaults merged with TWINLANE_CONFIG or <home>/config.json
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# A `_job` runner started through WMI (detach=wmi) has the user's default environment: load the
# caller's from its hand-off file before any twinlane module reads os.environ at import.
if __name__ == '__main__' and sys.argv[1:2] == ['_job']:
    from twinlane import envhandoff
    sys.argv[1:], _handoff = envhandoff.split(sys.argv[1:])
    if _handoff:
        try:
            envhandoff.load(_handoff)
        except (OSError, ValueError):
            sys.exit(5)  # EXIT_FAILED: never run a job with the wrong environment

try:
    from twinlane import common, observations  # noqa: E402
except BaseException:
    if sys.argv[1:2] == ['guard'] and os.environ.get('TWINLANE_ENGINE_JOB'):
        sys.exit(2)  # guard bootstrap cannot silently bypass enforcement on an import failure
    raise


@observations.command
def status(argv):
    from twinlane import baton, jobs, meter
    as_json = '--json' in argv
    ids = [arg for arg in argv if not arg.startswith('-')]
    if ids:
        try:
            jid = jobs.resolve_job(ids[0])
        except LookupError as e:
            print(f'twinlane status: {e}', file=sys.stderr)
            return common.EXIT_USAGE
        receipt = common.read_json(jobs._p(jid, 'result.json'), {}) or {}
        data = {'id': jid, 'status': jobs.job_status(jid), 'receipt_status': receipt.get('status'),
                'exit_code': receipt.get('exit_code'), 'finished_at': receipt.get('finished_at'),
                'receipt_path': jobs._p(jid, 'result.json') if receipt else None}
        common.emit(data, as_json, f"{jid}: {data['status']} exit={data['exit_code']} "
                    f"finished_at={data['finished_at']} receipt_status={data['receipt_status']} receipt={data['receipt_path']}")
        return 0
    m = meter.cached_observation()
    policy = common.load_policy()
    js = [j for j in jobs.list_jobs(limit=50, live_only=True)
          if j.get('status') in ('queued', 'running', 'orphaned', 'unknown')]
    bs = baton.list_batons()[:10]
    from twinlane import capacity, wip
    try:
        writers = len(wip.collect())
    except wip.WipError as e:  # status still reports; only writer admission fails closed
        writers = '?'
        print(f'twinlane: writer count unknown: {e}', file=sys.stderr)
    cap = capacity.line(writers, (policy.get('wip') or {}).get('max_writers', 5),
                        len(wip.queue()), m, common.now())
    if as_json:
        common.emit({'meter': m, 'running_jobs': js, 'batons': bs, 'capacity': cap}, True)
        return 0
    print(meter.format_line(m))
    print(cap)
    print(f'running jobs: {len(js)}' + ''.join(f"\n  {j.get('id')}  {j.get('engine')}  {j.get('name') or ''}  "
                                               f"{j.get('status')}  {j.get('cwd') or ''}" for j in js))
    print(f'batons: {len(bs)}' + ''.join(f"\n  {b.get('slug')}  owner={b.get('owner')}  age={b.get('age')}  "
                                         f"next: {common.clip(b.get('next_action'), 90)}" for b in bs))
    return 0


def fail_open(name, argv=None):
    """Advisory hooks fail open; a worker's enforcement guard must block on bootstrap failure too."""
    try:
        common.utf8_stdio()
        if name != 'hook':
            common.ensure_dirs()
        if name == 'hook':
            from twinlane import hook
            return hook.main(argv=argv)
        from twinlane import guard
        return guard.main()
    except BaseException:  # noqa: BLE001 - preserve the enforcement boundary before guard.main loads
        if name == 'guard' and os.environ.get('TWINLANE_ENGINE_JOB'):
            return 2  # PreToolUse blocking exit; does not depend on imports or writable stdout
        return 0


def wants_best_of(args):
    """True when a `run` argument list asks for the multi-engine form (`--best-of X` or `--best-of=X`)."""
    return any(a == '--best-of' or a.startswith('--best-of=') for a in args)


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('hook', 'guard'):
        return fail_open(argv[0], argv[1:])
    common.utf8_stdio()
    if argv == ['--version']:
        from twinlane import __version__
        import subprocess
        suffix = ''
        try:
            proc = subprocess.run(['git', '-C', os.path.dirname(os.path.dirname(__file__)),
                                   'rev-parse', '--short', 'HEAD'], capture_output=True, text=True, timeout=1)
            if proc.returncode == 0 and proc.stdout.strip():
                suffix = ' (' + proc.stdout.strip() + ')'
        except (OSError, subprocess.TimeoutExpired):
            pass
        print('twinlane ' + __version__ + suffix)
        return 0
    if argv and argv[0] == 'status':  # read-only, including an absent home
        return status(argv[1:])
    if argv and argv[0] == 'watch':
        from twinlane import watch
        return watch.cli(argv[1:])
    if argv and argv[0] == 'patrol':
        from twinlane import patrol
        return patrol.cli(argv[1:])
    if argv and argv[0] == 'completions':  # read-only, even with an absent home
        from twinlane import completions
        return completions.cli(argv[1:])
    if argv and argv[0] == 'board':  # read-only: dispatched before ensure_dirs, so it creates no directory
        from twinlane import board
        return board.cli(argv[1:])
    if argv and argv[0] == 'explain-writer-admission':  # read-only too: no lock, no write, no directory
        from twinlane import writer_admission
        return writer_admission.cli(argv[1:])
    if argv and argv[0] == 'seats':  # status and bench --dry-run must not create a home
        from twinlane import seats
        return seats.cli(argv[1:])
    common.ensure_dirs()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.strip())
        from twinlane import seats
        if seats.enabled():
            print('\n  twinlane seats status|start N|stop|bench --n K --dry-run '
                  '(experimental custody scaffolding; execution held)')
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd in ('wip', 'inbox', 'ack', 'trust'):
        from importlib import import_module
        return import_module('twinlane.' + cmd).cli(rest)
    if cmd == 'meter':
        from twinlane import meter
        return meter.cli(rest)
    if cmd == 'route':
        from twinlane import route
        return route.cli(rest)
    if cmd == 'run' and wants_best_of(rest):
        from twinlane import bestof
        return bestof.cli(['run'] + rest)
    if cmd in ('run', 'jobs', 'wait', 'result', 'stop', '_job'):
        from twinlane import jobs
        return jobs.cli(cmd, rest)
    if cmd == 'compare':
        from twinlane import bestof
        return bestof.cli(['compare'] + rest)
    if cmd == 'land':
        from twinlane import land
        return land.cli(rest)
    if cmd == 'ship':
        from twinlane import ship
        return ship.cli(rest)
    if cmd == 'wt':
        from twinlane import worktrees
        return worktrees.cli(rest)
    if cmd == 'resume':
        from twinlane import resume
        return resume.cli(rest)
    if cmd == 'sessions':
        from twinlane import sessions
        return sessions.cli(rest)
    if cmd == 'install':
        from twinlane import install
        return install.main(rest)
    if cmd in ('baton', 'handoff'):
        from twinlane import baton
        return baton.cli(cmd, rest)
    print(f'twinlane: unknown command {cmd!r}; see twinlane --help', file=sys.stderr)
    return common.EXIT_USAGE


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
