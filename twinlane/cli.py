#!/usr/bin/env python3
"""twinlane - use Claude Code and Codex together: meter, route, run, baton, handoff.

  twinlane status                       both meters + running jobs + batons (one screen)
  twinlane board [--json | --md] [--all] [--finished N] [--watch SECONDS]   one read-only screen for a lead session
  twinlane meter [--json] [--fresh]
  twinlane route <kind> [--urgent] [--engine claude|codex] [--json]
  twinlane run --brief F --cd DIR [--kind K] [--engine auto|claude|codex] [--write] [--bg] ...
  twinlane jobs | wait JOB... [--any] [--timeout S] | result JOB [--full] | stop JOB
  twinlane baton new|set|snap|show|list ...
  twinlane handoff (SLUG | --cd DIR | --session ID) [--to claude|codex] [--out F]
  twinlane handoff --session ID [--session ID ...] [--to claude|codex] --out-dir DIR   (one file per session)
  twinlane sessions [--engine claude|codex|all] [--since 48h] [--cwd DIR] [--limit N] [--jobs] [--json|--md]
  twinlane hook                          (Claude Code UserPromptSubmit hook; reads JSON on stdin)
  twinlane guard                         (PreToolUse guard inside twinlane jobs only; reads JSON on stdin)

kinds: code intel plan review (Claude first) | parallel sweep integrate verify (Codex first)
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

from twinlane import common  # noqa: E402


def status(argv):
    from twinlane import baton, jobs, meter
    as_json = '--json' in argv
    m = meter.read(fresh='--fresh' in argv, auto_refresh=True)
    js = [j for j in jobs.list_jobs(limit=50) if j.get('status') in ('queued', 'running', 'orphaned')]
    bs = baton.list_batons()[:10]
    if as_json:
        common.emit({'meter': m, 'running_jobs': js, 'batons': bs}, True)
        return 0
    print(meter.format_line(m))
    print(f'running jobs: {len(js)}' + ''.join(f"\n  {j.get('id')}  {j.get('engine')}  {j.get('name') or ''}  "
                                               f"{j.get('status')}  {j.get('cwd') or ''}" for j in js))
    print(f'batons: {len(bs)}' + ''.join(f"\n  {b.get('slug')}  owner={b.get('owner')}  age={b.get('age')}  "
                                         f"next: {common.clip(b.get('next_action'), 90)}" for b in bs))
    return 0


def fail_open(name):
    """Hooks run on every prompt or tool call: any failure (a bad TWINLANE_HOME, an unwritable
    state dir, a broken module) must exit 0 silently, never block or print a traceback."""
    try:
        common.utf8_stdio()
        common.ensure_dirs()
        if name == 'hook':
            from twinlane import hook
            return hook.main()
        from twinlane import guard
        return guard.main()
    except BaseException:  # noqa: BLE001 - fail-open by contract
        return 0


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv and argv[0] in ('hook', 'guard'):
        return fail_open(argv[0])
    common.utf8_stdio()
    if argv and argv[0] == 'board':  # read-only: dispatched before ensure_dirs, so it creates no directory
        from twinlane import board
        return board.cli(argv[1:])
    common.ensure_dirs()
    if not argv or argv[0] in ('-h', '--help', 'help'):
        print(__doc__.strip())
        return 0
    cmd, rest = argv[0], argv[1:]
    if cmd == 'status':
        return status(rest)
    if cmd == 'meter':
        from twinlane import meter
        return meter.cli(rest)
    if cmd == 'route':
        from twinlane import route
        return route.cli(rest)
    if cmd in ('run', 'jobs', 'wait', 'result', 'stop', '_job'):
        from twinlane import jobs
        return jobs.cli(cmd, rest)
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
