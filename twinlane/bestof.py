"""twinlane run --best-of / compare: run the same brief on several engines and compare the results.

    twinlane run --best-of claude,codex@sol-high --brief F --cd REPO [--kind K] [--write] [--base REF]
                 [--dry-run] [--json]
    twinlane compare GROUP [--wait] [--test] [--json | --md]
    twinlane compare GROUP --discard [--yes]

Each contestant is an ordinary background job started through `twinlane run --bg`, so the meters, the
routing refusal and the paid-credits guard apply to every contestant on its own. A contestant that is
refused is reported and the others still run; when every contestant is refused nothing is created and the
exit code is 3. A contestant never fails over to the other engine: it is pinned with `--engine X
--no-failover`. N contestants spend N times the usage.

A write contestant gets its own linked worktree on its own branch, all cut from the same base commit. A
read-only contestant shares the directory given with --cd. Nothing is picked for you: `compare` shows each
contestant's receipt and diff, and (with --test) runs the project's configured test command in each
worktree. `compare --discard` removes only this group's worktrees that hold nothing (through the same
safety checks as `twinlane wt prune`); it never deletes a branch with work on it, it prints the commands
for you to run instead.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import secrets
import sys
import time
from contextlib import redirect_stderr, redirect_stdout

from twinlane import common, jobs, projects, worktrees

SCHEMA = 'twinlane-bestof-v1'
ENGINES = ('claude', 'codex')
TEST_TIMEOUT_S = 1800
DIFF_TIMEOUT_S = 60
GROUP_ID = re.compile(r'^[0-9A-Za-z][0-9A-Za-z._-]{0,79}$')
COST_NOTE = ('N contestants spend N times the usage of one run; a contestant never fails over to the other '
             'engine.')


class BestOfError(ValueError):
    """A usage problem with the command line or the group."""


# ---------------------------------------------------------------- contestants

def parse_contestants(spec: str, cap: int) -> list[dict]:
    """`engine[:model][@tier],...` -> [{engine, model, tier, label}], at least two, at most `cap`.

    `@tier` is a Codex tier name; `:model` is a model id. Two entries for one engine need different
    model or tier settings: the same entry twice would only spend the usage twice."""
    parts = [p.strip() for p in str(spec or '').split(',')]
    if not any(parts):
        raise BestOfError('--best-of needs engines, e.g. --best-of claude,codex')
    out, seen, per_engine = [], set(), {}
    for part in parts:
        if not part:
            raise BestOfError(f'--best-of {spec!r} has an empty entry')
        rest, tier = (part.split('@', 1) + [None])[:2] if '@' in part else (part, None)
        engine, model = (rest.split(':', 1) + [None])[:2] if ':' in rest else (rest, None)
        engine = engine.strip().lower()
        model = model.strip() if model is not None else None
        tier = tier.strip() if tier is not None else None
        if engine not in ENGINES:
            raise BestOfError(f'unknown engine {engine!r} in --best-of; engines: {", ".join(ENGINES)}')
        if model == '' or tier == '':
            raise BestOfError(f'--best-of entry {part!r} has an empty model or tier')
        if tier is not None and engine != 'codex':
            raise BestOfError(f'--best-of entry {part!r}: @tier is a Codex setting; use engine:model for Claude')
        key = (engine, model, tier)
        if key in seen:
            raise BestOfError(f'--best-of lists {part!r} twice; the same engine twice needs different models')
        seen.add(key)
        per_engine[engine] = per_engine.get(engine, 0) + 1
        label = engine if per_engine[engine] == 1 else f'{engine}-{per_engine[engine]}'
        out.append({'engine': engine, 'model': model, 'tier': tier, 'label': label})
    if len(out) < 2:
        raise BestOfError('--best-of needs at least two contestants')
    if len(out) > cap:
        raise BestOfError(f'--best-of lists {len(out)} contestants; best_of.max_contestants allows {cap}')
    return out


def max_contestants(policy: dict) -> int:
    value = (policy.get('best_of') or {}).get('max_contestants', 3)
    if not isinstance(value, int) or isinstance(value, bool) or value < 1:
        raise BestOfError('config best_of.max_contestants must be a whole number >= 1')
    return value


def new_group_id() -> str:
    return f'{common.stamp()}-{secrets.token_hex(2)}'


def _preflight(c: dict, kind: str, args, meter: dict | None, meter_error: str | None, policy: dict) -> dict:
    """Route and money checks for one contestant, without creating anything.

    Returns the contestant dict extended with `refused` (a reason or None), `profile`, `wait_until`."""
    c = dict(c, refused=None, profile=None, wait_until=None, reason=None)
    if meter_error:
        c['refused'] = meter_error
        return c
    try:
        d = jobs._route(kind, meter, policy, args.urgent, c['engine'])
    except Exception as e:  # noqa: BLE001 - an unreadable route is a refusal, not a crash
        c['refused'] = f'routing failed: {type(e).__name__}: {common.clip(e, 160)}'
        return c
    if d.get('refused') or d.get('engine') != c['engine']:
        # A decision for another engine is a refusal: contestants never swap engines.
        c['refused'] = f"{c['engine']} not usable: {d.get('reason')}"
        c['wait_until'] = d.get('wait_until')
        return c
    c['reason'] = d.get('reason')
    c['profile'] = jobs.resolve_profile(kind, c['engine'], d, policy, args.write)
    if c['profile'] == 'code' and not args.write:
        c['refused'] = f'kind {kind} uses the code profile, which needs --write'
        return c
    if c['engine'] == 'codex' and c['model']:
        tier_name = jobs.codex_tier_model(c['model'], policy)
        if tier_name:
            c['refused'] = f"model {c['model']} is a Codex tier; write it as codex@{tier_name}"
            return c
    params = jobs.engine_params(c['engine'], kind, d, policy, c['profile'], c['model'], args.effort, c['tier'],
                                args.timeout, args.max_turns)
    if c['engine'] == 'codex':
        why = jobs.codex_credit_refusal(meter, params, policy)
        if why:
            c['refused'] = why
            return c
    c['params'] = {k: params.get(k) for k in ('model', 'effort', 'tier')}
    return c


# ---------------------------------------------------------------- storage

def groups_dir() -> str:
    return os.path.join(common.STATE, 'groups')


def group_path(group_id: str) -> str:
    if not GROUP_ID.match(group_id or ''):
        raise BestOfError(f'not a group id: {group_id!r}')
    return os.path.join(groups_dir(), group_id + '.json')


def save_group(group: dict) -> None:
    os.makedirs(groups_dir(), exist_ok=True)
    common.write_json(group_path(group['id']), group)


def load_group(ref: str) -> dict:
    """The group whose id is `ref`, or the only one that contains it. Raises BestOfError."""
    if not GROUP_ID.match(ref or ''):
        raise BestOfError(f'not a group id: {ref!r}')
    try:
        names = sorted(n[:-5] for n in os.listdir(groups_dir()) if n.endswith('.json'))
    except OSError:
        names = []
    if ref in names:
        hit = ref
    else:
        hits = [n for n in names if ref in n]
        if not hits:
            raise BestOfError(f'no best-of group matches {ref!r}')
        if len(hits) > 1:
            raise BestOfError(f'{ref!r} is ambiguous: {", ".join(hits[:5])}')
        hit = hits[0]
    group = common.read_json(group_path(hit))
    if not isinstance(group, dict) or not isinstance(group.get('contestants'), list):
        raise BestOfError(f'group {hit} is unreadable')
    return group


# ---------------------------------------------------------------- launching

def launch(argv: list) -> tuple[int, dict, str]:
    """Start one background job through `twinlane run`. Returns (exit code, JSON payload or {}, stderr text).

    Everything goes through jobs.cli so the meter check, the routing refusal, the credit guard and the
    writer admission apply exactly as they do to a hand-typed `twinlane run`. Tests replace this."""
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = jobs.cli('run', argv)
    try:
        payload = json.loads(out.getvalue())
    except ValueError:
        payload = {}
    return rc, payload if isinstance(payload, dict) else {}, err.getvalue().strip()


def job_argv(c: dict, cwd: str, name: str, kind: str, args) -> list:
    argv = ['--brief', os.path.abspath(args.brief), '--cd', cwd, '--kind', kind, '--engine', c['engine'],
            '--no-failover', '--bg', '--json', '--name', name]
    if args.write:
        argv.append('--write')
    if c['model']:
        argv += ['--model', c['model']]
    if c['tier']:
        argv += ['--tier', c['tier']]
    if args.effort:
        argv += ['--effort', args.effort]
    if args.timeout:
        argv += ['--timeout', str(args.timeout)]
    if args.max_turns:
        argv += ['--max-turns', str(args.max_turns)]
    if args.urgent:
        argv.append('--urgent')
    return argv


def worktree_root(main_root: str, cfg: dict) -> str:
    configured = (cfg.get('wt') or {}).get('root')
    if configured:
        path = os.path.expanduser(configured)
        return path if os.path.isabs(path) else os.path.join(main_root, path)
    return os.path.join(os.path.dirname(main_root), os.path.basename(main_root.rstrip('/\\')) + '-twinlane-wt')


def _resolve_base(cwd: str, ref: str) -> str:
    rc, out = common.git(cwd, 'rev-parse', '--verify', '-q', f'{ref}^{{commit}}')
    if rc != 0 or not out.strip():
        raise BestOfError(f'--base {ref!r} is not a commit in {cwd}')
    return out.strip().splitlines()[0]


# ---------------------------------------------------------------- run

def _run_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane run --best-of',
                                 description='Run one brief on several engines, each in its own worktree.')
    ap.add_argument('--best-of', dest='best_of', required=True, metavar='ENGINE[:MODEL][@TIER],...')
    ap.add_argument('--brief', required=True)
    ap.add_argument('--cd', required=True)
    ap.add_argument('--kind')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--base', default='HEAD')
    ap.add_argument('--effort')
    ap.add_argument('--timeout', type=int)
    ap.add_argument('--max-turns', dest='max_turns', type=int)
    ap.add_argument('--urgent', action='store_true')
    ap.add_argument('--dry-run', dest='dry_run', action='store_true')
    ap.add_argument('--json', action='store_true')
    return ap


def _read_brief(path: str) -> tuple[str, str]:
    if not os.path.isfile(path):
        raise BestOfError(f'brief not found: {path}')
    with open(path, 'rb') as h:
        raw = h.read()
    text = jobs.decode_text(raw)
    if not text.strip():
        raise BestOfError(f'brief is empty: {path}')
    problem = jobs.brief_problem(text)
    if problem:
        raise BestOfError(f'brief {path} is not readable text: {problem}')
    return text, hashlib.sha256(raw).hexdigest()


def _contestant_public(c: dict) -> dict:
    return {k: c.get(k) for k in ('label', 'engine', 'model', 'tier', 'profile', 'refused', 'wait_until',
                                  'job', 'worktree', 'branch', 'cwd', 'shared', 'note') if k in c}


def plan_group(args, policy: dict) -> dict:
    """Everything `run --best-of` decides before it creates anything (the whole of --dry-run)."""
    contestants = parse_contestants(args.best_of, max_contestants(policy))
    kind = args.kind or ('code' if args.write else 'intel')
    if kind not in policy.get('routing', {}):
        raise BestOfError(f"unknown kind {kind!r}; kinds: {', '.join(policy.get('routing', {}))}")
    cwd = os.path.abspath(args.cd)
    if not os.path.isdir(cwd):
        raise BestOfError(f'--cd must be an existing directory (got {args.cd!r})')
    leftover = common.leftover_dir_problem(cwd)
    if leftover:
        raise BestOfError(f'--cd {cwd} {leftover}; pass an existing repo or worktree')
    _, sha = _read_brief(args.brief)
    meter, meter_error = None, None
    try:
        meter = jobs._meter(policy, auto_refresh=not args.dry_run and any(c['engine'] == 'claude' for c in contestants))
    except Exception as e:  # noqa: BLE001 - an unknown meter is never OK
        meter_error = f'meter unavailable: {type(e).__name__}: {common.clip(e, 160)}'
    checked = [_preflight(c, kind, args, meter, meter_error, policy) for c in contestants]
    needs_tree = any(c['profile'] == 'code' for c in checked if not c['refused'])
    try:
        main_root, cfg = projects.resolve(cwd, policy)
    except projects.ProjectError as e:
        if needs_tree:
            raise BestOfError(f'a write contestant needs a git repository: {e}')
        main_root, cfg = None, None
    base = base_ref = None
    if main_root:
        base_ref = args.base
        base = _resolve_base(cwd, args.base)
    return {'kind': kind, 'cwd': cwd, 'repo': main_root or cwd, 'cfg': cfg, 'base': base, 'base_ref': base_ref,
            'brief_sha256': sha, 'contestants': checked, 'needs_tree': needs_tree, 'meter': meter}


def _name(group_id: str, c: dict) -> str:
    return f"bo-{group_id}-{c['label']}"


def _assign_places(plan: dict, group_id: str) -> None:
    """Fill in where each live contestant will work (nothing is created here)."""
    root = worktree_root(plan['repo'], plan['cfg']) if plan['needs_tree'] else None
    for c in plan['contestants']:
        if c['refused']:
            continue
        if c['profile'] == 'code':
            c['branch'] = f'twinlane/{_name(group_id, c)}'
            c['worktree'] = os.path.join(root, _name(group_id, c))
            c['cwd'], c['shared'] = c['worktree'], False
        else:
            c['branch'], c['worktree'] = None, None
            c['cwd'], c['shared'] = plan['cwd'], True


def _setup_worktree(plan: dict, group_id: str, c: dict) -> str | None:
    """Create the contestant's worktree (and run wt.setup_command). Returns an error text or None."""
    try:
        path = worktrees.add(plan['repo'], c['branch'], c['worktree'], plan['base'])
    except worktrees.WorktreeError as e:
        return f'worktree: {e}'
    c['worktree'] = c['cwd'] = path
    command = ((plan['cfg'] or {}).get('wt') or {}).get('setup_command')
    if command:
        try:
            argv = projects.argv(command, repo=plan['repo'], worktree=path, branch=c['branch'],
                                 slug=_name(group_id, c), base=plan['base'])
        except projects.ProjectError as e:
            return f'wt.setup_command: {e}'
        rc, out, err = common.run(argv, cwd=path, timeout=worktrees.REMOVE_TIMEOUT_S)
        if rc != 0:
            return f'wt.setup_command failed (rc {rc}): {common.clip((err or out).strip(), 200)}'
    return None


def render_run(group: dict, dry_run: bool) -> str:
    started = [c for c in group['contestants'] if not c.get('refused')]
    lines = [f"twinlane best-of {group['id']}{'  (dry run: nothing created)' if dry_run else ''}",
             f"kind {group['kind']}  base {(group.get('base') or '-')[:12]}"
             + (f" ({group['base_ref']})" if group.get('base_ref') else '')]
    for c in group['contestants']:
        model = c.get('model') or c.get('tier') or 'default'
        if c.get('refused'):
            lines.append(f"  refused   {c['label']:<10} {c['refused']}")
        else:
            place = 'shared, read-only: ' + c['cwd'] if c.get('shared') else c['worktree'] + '  ' + c['branch']
            what = c.get('job') or 'would start'
            lines.append(f"  {'would run' if dry_run else 'started':<9} {c['label']:<10} {model}  {what}  {place}")
            if c.get('note'):
                lines.append(f"            note: {c['note']}")
    lines.append(f"note: {COST_NOTE}")
    if started and not dry_run:
        lines.append(f"next: twinlane compare {group['id']} --wait")
    return '\n'.join(lines)


def cmd_run(argv) -> int:
    try:
        args = _run_parser().parse_args(list(argv))
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    refused = projects.refuse_in_worker('run --best-of')
    if refused is not None:
        return refused
    policy = common.load_policy()
    try:
        plan = plan_group(args, policy)
    except BestOfError as e:
        print(f'twinlane run --best-of: {e}', file=sys.stderr)
        return common.EXIT_USAGE
    group_id = new_group_id()
    _assign_places(plan, group_id)
    group = {'id': group_id, 'schema': SCHEMA, 'created_at': common.iso(common.now()),
             'brief_src': os.path.abspath(args.brief), 'brief_sha256': plan['brief_sha256'], 'kind': plan['kind'],
             'write': bool(args.write), 'repo': plan['repo'], 'cwd': plan['cwd'], 'base': plan['base'],
             'base_ref': plan['base_ref'], 'contestants': plan['contestants']}
    live = [c for c in group['contestants'] if not c['refused']]
    if args.dry_run:
        for c in live:
            c['argv'] = job_argv(c, c['cwd'], _name(group_id, c), plan['kind'], args)
        out = dict(group, dry_run=True, contestants=[dict(_contestant_public(c), argv=c.get('argv'))
                                                     for c in group['contestants']])
        if args.json:
            common.emit(out, True)
        else:
            print(render_run(group, True))
        return common.EXIT_OK if live else common.EXIT_REFUSED
    if not live:
        group['contestants'] = [_contestant_public(c) for c in group['contestants']]
        if args.json:
            common.emit(dict(group, dry_run=False, started=0), True)
        else:
            print(render_run(group, False), file=sys.stderr)
            print('twinlane run --best-of: every contestant was refused; nothing was started', file=sys.stderr)
        return common.EXIT_REFUSED
    group['contestants'] = [_contestant_public(c) for c in group['contestants']]
    save_group(group)
    for c in group['contestants']:
        if c.get('refused'):
            continue
        if not c['shared']:
            err = _setup_worktree(plan, group_id, c)
            if err:
                c['refused'] = err
                c['note'] = 'the worktree and branch (if created) are left; `twinlane compare --discard` clears empty ones'
                save_group(group)
                continue
        rc, payload, errtext = launch(job_argv(c, c['cwd'], _name(group_id, c), plan['kind'], args))
        if rc != 0 or not payload.get('id'):
            reason = payload.get('reason') or errtext or f'twinlane run exited {rc}'
            c['refused'] = f'job not started: {common.clip(reason, 300)}'
            if not c['shared']:
                c['note'] = 'its empty worktree is left; `twinlane compare --discard` clears it'
        else:
            c['job'] = payload['id']
        save_group(group)
    started = [c for c in group['contestants'] if c.get('job')]
    if args.json:
        common.emit(dict(group, dry_run=False, started=len(started)), True)
    else:
        print(render_run(group, False))
    return common.EXIT_OK if started else common.EXIT_REFUSED


# ---------------------------------------------------------------- compare

def _job_state(job: str | None) -> str:
    if not job:
        return 'none'
    if not os.path.isdir(jobs.job_dir(job)):
        return 'missing'
    try:
        return jobs.job_status(job)
    except Exception:  # noqa: BLE001 - an unreadable job is not finished
        return 'unknown'


def _finished(state: str) -> bool:
    return state in jobs.TERMINAL or state in ('missing', 'none')


def diff_stats(worktree: str, base: str | None) -> dict:
    """Changes in a worktree against the group's base: `git diff --numstat <base>` plus the untracked files."""
    if not base:
        return {'error': 'no base commit recorded'}
    if not os.path.isdir(worktree):
        return {'error': 'worktree is gone'}
    rc, out = common.git(worktree, 'diff', '--numstat', base, timeout=DIFF_TIMEOUT_S)
    if rc != 0:
        return {'error': common.clip(out, 200)}
    files = added = deleted = 0
    binary = []
    for line in out.splitlines():
        parts = line.split('\t', 2)
        if len(parts) != 3:
            continue
        files += 1
        if parts[0] == '-' or parts[1] == '-':
            binary.append(parts[2])
            continue
        added += int(parts[0])
        deleted += int(parts[1])
    rc, untracked = common.git(worktree, 'ls-files', '--others', '--exclude-standard', timeout=DIFF_TIMEOUT_S)
    names = [x for x in untracked.splitlines() if x.strip()] if rc == 0 else []
    return {'files': files, 'added': added, 'deleted': deleted, 'binary': binary, 'untracked': len(names),
            'untracked_sample': names[:5], 'error': None}


def _receipt(job: str | None) -> dict | None:
    if not job:
        return None
    r = common.read_json(os.path.join(jobs.job_dir(job), 'result.json'))
    return r if isinstance(r, dict) else None


def run_tests(command, worktree: str, repo: str, branch: str, label: str, base: str | None) -> dict:
    started = time.monotonic()
    try:
        values = {'repo': repo, 'worktree': worktree, 'branch': branch, 'slug': label}
        if base:
            values['base'] = base
        argv = projects.argv(command, **values)
    except projects.ProjectError as e:
        return {'ran': False, 'error': str(e)}
    rc, out, err = common.run(argv, cwd=worktree, timeout=TEST_TIMEOUT_S)
    text = (out + ('\n' if out and err else '') + err).strip()
    return {'ran': True, 'rc': rc, 'timed_out': rc == 124 and err == 'timeout',
            'seconds': round(time.monotonic() - started, 1), 'tail': text[-600:]}


def gather(group: dict, policy: dict, do_test: bool) -> dict:
    cfg, cfg_error = None, None
    try:
        _, cfg = projects.resolve(group.get('repo'), policy)
    except projects.ProjectError as e:
        cfg_error = str(e)
    command = (cfg or {}).get('test')
    rows = []
    for c in group['contestants']:
        row = {'label': c.get('label'), 'engine': c.get('engine'), 'branch': c.get('branch'),
               'worktree': c.get('worktree'), 'shared': bool(c.get('shared')), 'job': c.get('job'),
               'refused': c.get('refused'), 'status': None, 'served_models': [], 'requested_model': None,
               'duration_s': None, 'turns': None, 'usage': [], 'touched_files': None,
               'read_only_violation': None, 'diff': None, 'test': None}
        if c.get('refused') or not c.get('job'):
            rows.append(row)
            continue
        row['status'] = _job_state(c['job'])
        r = _receipt(c['job'])
        if r:
            row.update(served_models=r.get('served_models') or [], duration_s=r.get('duration_s'),
                       turns=r.get('num_turns'), touched_files=r.get('touched_files'),
                       read_only_violation=r.get('read_only_violation'),
                       requested_model=(r.get('requested') or {}).get('model') or (r.get('requested') or {}).get('tier'))
            try:
                row['usage'] = jobs.usage_lines(r)
            except Exception as e:  # noqa: BLE001 - a usage line never fails a compare
                row['usage'] = [f'usage: unreadable ({type(e).__name__})']
        if not row['shared'] and c.get('worktree'):
            row['diff'] = diff_stats(c['worktree'], group.get('base'))
        if do_test and not row['shared'] and c.get('worktree'):
            if cfg_error:
                row['test'] = {'ran': False, 'error': f'project settings unusable: {cfg_error}'}
            elif command is None:
                row['test'] = {'ran': False, 'error': 'no test command configured (projects[<repo>].test)'}
            elif not _finished(row['status']):
                row['test'] = {'ran': False, 'error': f"job is still {row['status']}; not tested"}
            else:
                row['test'] = run_tests(command, c['worktree'], group['repo'], c.get('branch') or '', c['label'],
                                        group.get('base'))
        rows.append(row)
    return {'group': group['id'], 'kind': group.get('kind'), 'repo': group.get('repo'), 'base': group.get('base'),
            'created_at': group.get('created_at'), 'brief_sha256': group.get('brief_sha256'),
            'tests_requested': bool(do_test), 'contestants': rows, 'next': next_commands(group, rows)}


def next_commands(group: dict, rows: list) -> list[str]:
    """What you can do next. Nothing is chosen for you."""
    out = []
    base = (group.get('base') or '')[:12]
    for row in rows:
        if row['refused'] or row['shared'] or not row['worktree'] or not _finished(row['status'] or 'none'):
            continue
        diff = row['diff'] or {}
        if not diff.get('files') and not diff.get('untracked'):
            continue
        out.append(f"review {row['label']}: git -C {row['worktree']} diff {base}")
        out.append(f"keep {row['label']}: commit it in {row['worktree']}, then "
                   f"twinlane land enqueue {_name(group['id'], {'label': row['label']})} --worktree {row['worktree']}")
    out.append(f"clear the empty losers: twinlane compare {group['id']} --discard")
    return out


def _diff_text(d: dict | None) -> str:
    if d is None:
        return 'shared directory, read-only'
    if d.get('error'):
        return f"diff unavailable ({d['error']})"
    text = f"{d['files']} file(s), +{d['added']} -{d['deleted']}, {d['untracked']} untracked"
    return text + (f", {len(d['binary'])} binary" if d['binary'] else '')


def render_compare(rep: dict, md: bool = False) -> str:
    lines = []
    if md:
        lines += [f"# Best-of {rep['group']}", '',
                  f"kind `{rep['kind']}`, base `{(rep['base'] or '-')[:12]}`, repo `{rep['repo']}`", '']
    else:
        lines.append(f"twinlane compare {rep['group']}  kind {rep['kind']}  base {(rep['base'] or '-')[:12]}")
    for row in rep['contestants']:
        head = f"{row['label']} ({row['engine']})"
        if md:
            lines.append(f'## {head}')
        else:
            lines.append(head)
        bullet = '- ' if md else '  '
        if row['refused'] or not row['job']:
            lines.append(f"{bullet}refused: {row['refused'] or 'no job'}")
            continue
        served = ','.join(row['served_models']) or row['requested_model'] or '-'
        lines.append(f"{bullet}job {row['job']}  status {row['status']}  model {served}  "
                     f"duration {row['duration_s'] if row['duration_s'] is not None else '-'}s  "
                     f"turns {row['turns'] if row['turns'] is not None else '-'}")
        for u in row['usage']:
            lines.append(f'{bullet}{u}')
        lines.append(f"{bullet}diff vs base: {_diff_text(row['diff'])}")
        if row['touched_files'] is not None:
            lines.append(f"{bullet}receipt touched_files: {len(row['touched_files'])}")
        if row['read_only_violation']:
            lines.append(f'{bullet}READ-ONLY VIOLATION recorded in the receipt')
        if row['worktree']:
            lines.append(f"{bullet}{row['worktree']}  branch {row['branch']}")
        t = row['test']
        if t is not None:
            if not t.get('ran'):
                lines.append(f"{bullet}test: not run: {t.get('error')}")
            else:
                verdict = 'TIMED OUT' if t.get('timed_out') else ('passed' if t['rc'] == 0 else f"FAILED (rc {t['rc']})")
                lines.append(f"{bullet}test: {verdict} in {t['seconds']}s")
                if t['rc'] != 0 and t.get('tail'):
                    lines.append(f"{bullet}  " + t['tail'].replace('\n', '\n' + bullet + '  '))
    if not rep['tests_requested']:
        lines.append('tests were not run (add --test to run the configured test command in each worktree)')
    lines.append('nothing was picked for you.')
    lines += (['', '## Next', ''] if md else ['next:']) + [(f'- {n}' if md else f'  {n}') for n in rep['next']]
    return '\n'.join(lines)


def wait_for(group: dict, timeout: float | None) -> bool:
    """Block until every started job is finished. True when they all are, False on timeout."""
    deadline = None if timeout is None else time.monotonic() + timeout
    while True:
        states = [_job_state(c.get('job')) for c in group['contestants'] if c.get('job')]
        if all(_finished(s) for s in states):
            return True
        if deadline is not None and time.monotonic() >= deadline:
            return False
        time.sleep(jobs.POLL_S if deadline is None else max(0.0, min(jobs.POLL_S, deadline - time.monotonic())))


# ---------------------------------------------------------------- discard

def _manual_commands(repo: str, path: str, branch: str, dirty: int | None) -> list[str]:
    force = ' --force' if dirty else ''
    return [f'git -C "{repo}" worktree remove{force} "{path}"', f'git -C "{repo}" branch -D {branch}']


def discard(group: dict, policy: dict, yes: bool) -> dict:
    """Remove this group's worktrees that hold nothing, with the same checks as `twinlane wt prune`.

    Only the recorded worktrees on the recorded branches are looked at. A worktree that is unmerged,
    dirty, in use or otherwise blocked is left alone and listed with the commands to remove it by hand.
    The branch of a removed worktree is deleted with `git branch -d` (merged only), never -D."""
    main_root, cfg = projects.resolve(group.get('repo'), policy)
    cfg = dict(cfg, wt=dict(cfg.get('wt') or {}, prune_min_age_s=0))  # our own, just-made worktrees
    settings = worktrees._settings(cfg)
    target = worktrees.target_branch(main_root, cfg)
    result = {'group': group['id'], 'repo': main_root, 'dry_run': not yes, 'rows': [], 'removed': 0, 'failed': 0}

    def run_all():
        for c in group['contestants']:
            if c.get('shared') or not c.get('worktree') or not c.get('branch'):
                continue
            row = {'label': c.get('label'), 'path': c['worktree'], 'branch': c['branch'], 'action': 'skip',
                   'blockers': [], 'manual': [], 'error': None, 'branch_deleted': False}
            result['rows'].append(row)
            state = _job_state(c.get('job'))
            if not _finished(state):
                row['blockers'] = [f'job {c.get("job")} is still {state}']
                row['manual'] = _manual_commands(main_root, c['worktree'], c['branch'], None)
                continue
            listed = worktrees.worktree_list(main_root)
            found = next((w for w in listed if worktrees._canon(w['path']) == worktrees._canon(c['worktree'])), None)
            if found is None:
                row['action'], row['blockers'] = 'gone', ['no longer listed by git']
                continue
            if found.get('branch') != c['branch']:
                # Never touch a worktree that now holds some other branch.
                row['blockers'] = [f"the worktree is on {found.get('branch')!r} now, not the group's branch"]
                continue
            usage = worktrees.usage_index(main_root)
            inbound = worktrees.inbound_index(main_root, settings['link_roots'])
            ev, links = worktrees._evaluate(main_root, cfg, found, target, usage, inbound, True, None,
                                            [w['path'] for w in listed])
            row['state'] = ev['state']
            if ev['blockers']:
                row['blockers'] = ev['blockers']
                row['manual'] = _manual_commands(main_root, c['worktree'], c['branch'], ev.get('dirty'))
                continue
            if not yes:
                row['action'] = 'would remove'
                continue
            res = worktrees._remove(main_root, settings, ev, links, target)
            row['action'] = 'removed' if res['removed'] else 'failed'
            row['error'], row['branch_deleted'] = res['error'], res['branch_deleted']
            row['branch_note'] = res['branch_note']
            result['removed' if res['removed'] else 'failed'] += 1
            worktrees._ledger(main_root, {'event': row['action'], 'at': common.iso(common.now()),
                                          'path': ev['path'], 'branch': c['branch'], 'tip': ev['tip'],
                                          'target': target, 'branch_deleted': res['branch_deleted'],
                                          'error': res['error'], 'via': f"best-of {group['id']}"})
            if res['abort']:
                break

    if yes:
        with common.file_lock(os.path.join(projects.state_dir('wt', main_root, create=True), '.lock'), 30):
            run_all()
    else:
        run_all()
    return result


def render_discard(r: dict) -> str:
    lines = [f"twinlane compare {r['group']} --discard" + ('  (dry run)' if r['dry_run'] else '')]
    manual = []
    for row in r['rows']:
        text = f"  {row['action']:<12} {row['label']}  {row['branch']}  {row['path']}"
        if row['action'] == 'failed':
            text += f": {row['error']}"
        elif row['blockers']:
            text += ': ' + '; '.join(row['blockers'])
        elif row['action'] == 'removed':
            text += '  branch deleted' if row.get('branch_deleted') else f"  {row.get('branch_note') or ''}"
        lines.append(text)
        manual += row['manual']
    if r['dry_run']:
        would = sum(1 for row in r['rows'] if row['action'] == 'would remove')
        lines.append(f'dry run: {would} would be removed; nothing changed.' + (' Rerun with --yes.' if would else ''))
    else:
        lines.append(f"removed {r['removed']}, failed {r['failed']}")
    if manual:
        lines.append('left in place (unmerged, changed, in use or blocked); twinlane never deletes these. '
                     'To discard one yourself, run:')
        lines += [f'  {m}' for m in manual]
    return '\n'.join(lines)


# ---------------------------------------------------------------- cli

def _compare_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane compare', description='Compare the contestants of a best-of group.')
    ap.add_argument('group')
    ap.add_argument('--wait', action='store_true', help='wait for every job to finish first')
    ap.add_argument('--timeout', type=float, help='with --wait: give up after this many seconds')
    ap.add_argument('--test', action='store_true', help='run the configured test command in each worktree')
    ap.add_argument('--discard', action='store_true', help='remove this group\'s empty worktrees (dry run unless --yes)')
    ap.add_argument('--yes', action='store_true')
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument('--json', action='store_true')
    fmt.add_argument('--md', action='store_true')
    return ap


def cmd_compare(argv) -> int:
    try:
        args = _compare_parser().parse_args(list(argv))
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    if args.discard and (args.test or args.wait):
        print('twinlane compare: --discard cannot be combined with --test or --wait', file=sys.stderr)
        return common.EXIT_USAGE
    if args.yes and not args.discard:
        print('twinlane compare: --yes only goes with --discard', file=sys.stderr)
        return common.EXIT_USAGE
    if args.test or args.discard:
        refused = projects.refuse_in_worker('compare --test' if args.test else 'compare --discard')
        if refused is not None:
            return refused
    try:
        group = load_group(args.group)
    except BestOfError as e:
        print(f'twinlane compare: {e}', file=sys.stderr)
        return common.EXIT_USAGE
    policy = common.load_policy()
    if args.discard:
        try:
            res = discard(group, policy, args.yes)
        except (projects.ProjectError, worktrees.WorktreeError) as e:
            print(f'twinlane compare --discard: {e}', file=sys.stderr)
            return common.EXIT_USAGE
        except TimeoutError as e:
            print(f'twinlane compare --discard: another prune holds the lock ({e})', file=sys.stderr)
            return common.EXIT_RUNNING
        if args.json:
            common.emit(res, True)
        else:
            print(render_discard(res))
        return common.EXIT_FAILED if res['failed'] else common.EXIT_OK
    finished = wait_for(group, args.timeout) if args.wait else True
    rep = gather(group, policy, args.test)
    if args.json:
        common.emit(rep, True)
    else:
        print(render_compare(rep, md=args.md))
        if not finished:
            print('timed out waiting: some jobs are still running', file=sys.stderr)
    return common.EXIT_OK if finished else common.EXIT_RUNNING


def cli(argv) -> int:
    argv = list(argv)
    if argv and argv[0] == 'run':
        return cmd_run(argv[1:])
    if argv and argv[0] == 'compare':
        return cmd_compare(argv[1:])
    print('usage: twinlane run --best-of ENGINE,ENGINE... --brief F --cd DIR | twinlane compare GROUP',
          file=sys.stderr)
    return common.EXIT_USAGE
