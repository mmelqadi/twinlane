"""Codex Cloud backend: `twinlane run --engine codex --where cloud`.

A cloud job runs on OpenAI's servers, so it costs the laptop one sleeping Python process and a status call every
few seconds instead of a Codex engine with its shells. The runner:

  1. scans the brief and every attachment with the data firewall (firewall.py);
  2. makes sure a snapshot branch exists in the cloud repo (a configurable command, so company rules stay in the
     private config) and submits `codex cloud exec --env ENV --branch BRANCH`;
  3. polls `codex cloud status TASK` through a cache shared by every runner;
  4. collects `codex cloud diff TASK` as result.patch, never with `codex cloud apply`, and for a writing job runs
     `git apply --check` and then `git apply` inside the job's own linked worktree only.

A cloud failure is recorded as a failure. Nothing here ever re-runs a job locally; the lead passes `--where local`.

Settings live in the user config under "cloud" (defaults below; nothing leaves the machine until enabled+env):
  enabled, env, remote, clone_dir, binary, max_parallel, attempts, poll_s, poll_fail_max, timeout_s,
  default_ref, inline_prompt_max, apply_to_worktree, task_url_re, mirror {...}, firewall {...} (see firewall.py)
  default_where  'cloud' sends every Codex job there unless --where local, a resume/continue, or the brief matches
                 one of needs_local (regexes for what only this machine can do: the database, a live dry run, ...)
                 The small local Codex pool is the ordinary codex.max_parallel.
mirror: {argv, success_re, branch, lock, add_flag, reuse {ref, branch, receipts_glob, match, sha_key, order_key}}
  argv        command that publishes the snapshot; placeholders {job} {ref} {sha} {branch}; the element "{adds}"
              expands to the attachments as repeated `add_flag SRC=DEST`
  branch      the branch name to run in, default "cloud/{job}"
  reuse       when ref is reuse.ref, no attachments, and the newest matching receipt already holds this sha,
              run on reuse.branch and skip the push
  dropped_note_file  a text file the mirror command writes: the paths it left out. Appended (scanned) to the prompt so
              the worker says which one it needs instead of inventing it
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

from . import common, firewall

DEFAULTS = {
    'enabled': False, 'env': None, 'remote': None, 'clone_dir': None, 'binary': None,
    'max_parallel': 20, 'attempts': 1, 'poll_s': 30, 'poll_fail_max': 8, 'timeout_s': 5400,
    'default_ref': None, 'inline_prompt_max': 6000, 'apply_to_worktree': True,
    'task_url_re': r'https://chatgpt\.com/codex/tasks/([\w-]+)',
    'default_where': 'local', 'needs_local': [], 'occupancy_ttl_s': 86400,
    'mirror': {'argv': None, 'success_re': None, 'branch': 'cloud/{job}', 'lock': True, 'add_flag': '--add',
               'timeout_s': 1800, 'reuse': None, 'dropped_note_file': None},
    'firewall': {},
}
STATE_WORDS = {'ready': 'ready', 'completed': 'ready', 'done': 'ready', 'succeeded': 'ready',
               'error': 'failed', 'failed': 'failed', 'cancelled': 'cancelled', 'canceled': 'cancelled'}
SUBMIT_TIMEOUT_S = 180
STATUS_TIMEOUT_S = 90
DIFF_TIMEOUT_S = 180
BRIEF_ATTACH = '.cloud/brief.md'
NO_WINDOW = getattr(subprocess, 'CREATE_NO_WINDOW', 0)


class CloudError(Exception):
    """A step failed in a way the job reports as its error."""


class SubmitUnknown(CloudError):
    """The cloud task may exist although its id was never recorded. The durable `submitting` marker stays so no
    later runner submits again."""


def settings(policy: dict | None) -> dict:
    cfg = {k: (dict(v) if isinstance(v, dict) else v) for k, v in DEFAULTS.items()}
    user = (policy or {}).get('cloud') or {}
    for key, value in user.items():
        if isinstance(value, dict) and isinstance(cfg.get(key), dict):
            cfg[key].update(value)
        else:
            cfg[key] = value
    return cfg


def refusal(policy: dict | None) -> str | None:
    """Why --where cloud cannot be used at all, or None."""
    cfg = settings(policy)
    if not cfg['enabled']:
        return 'cloud is not enabled: set cloud.enabled and cloud.env in the twinlane config'
    if not cfg['env']:
        return 'cloud.env is not set: it names the Codex Cloud environment (a label) to run in'
    if not binary_argv(cfg):
        return 'the codex CLI was not found (cloud.binary, TWINLANE_CODEX_CLOUD_BIN or PATH)'
    return None


def implicit(policy: dict | None, brief_text: str, eligible: bool) -> tuple[bool, str | None]:
    """(use cloud, note) for a job that did not say --where and routed to Codex."""
    cfg = settings(policy)
    if cfg.get('default_where') != 'cloud' or not eligible:
        return False, None
    why = refusal(policy)
    if why:
        return False, f'cloud-first is configured but unusable ({why}); running locally'
    for rx in cfg.get('needs_local') or []:
        try:
            hit = re.search(rx, brief_text, re.I)
        except re.error:
            continue
        if hit:
            return False, 'the brief needs this machine (cloud.needs_local matched); running locally'
    return True, 'cloud-first: cloud.default_where is cloud'


def binary_argv(cfg: dict) -> list[str] | None:
    configured = cfg.get('binary') or os.environ.get('TWINLANE_CODEX_CLOUD_BIN')
    if isinstance(configured, list) and configured:
        return [str(a) for a in configured]
    if isinstance(configured, str) and configured:
        return [sys.executable, configured] if configured.lower().endswith('.py') else [configured]
    found = shutil.which('codex')
    return [found] if found else None


def _run(argv: list[str], timeout_s: float, cwd: str | None = None) -> tuple[int, bytes, str]:
    try:
        p = subprocess.run(argv, capture_output=True, cwd=cwd, timeout=timeout_s, creationflags=NO_WINDOW)
    except subprocess.TimeoutExpired:
        return 124, b'', f'timed out after {int(timeout_s)} s'
    except OSError as e:
        return 127, b'', f'{type(e).__name__}: {e}'
    return p.returncode, p.stdout, p.stderr.decode('utf-8', 'replace')


def _text(raw: bytes) -> str:
    return raw.decode('utf-8', 'replace')


# ---------------------------------------------------------------- the codex cloud CLI

def parse_state(output: str) -> tuple[str, str]:
    """(state, head line): state is running, ready, failed, cancelled or unknown. The CLI prints a bracketed
    state such as [READY] first; an unfamiliar bracket word counts as running so a new state never fails a job."""
    for line in output.splitlines():
        if not line.strip():
            continue
        m = re.search(r'\[([A-Za-z_ -]+)\]', line)
        if not m:
            return 'unknown', line.strip()[:120]
        word = m.group(1).strip().lower().replace(' ', '_')
        return STATE_WORDS.get(word, 'running'), line.strip()[:120]
    return 'unknown', ''


def poll_cache_path() -> str:
    return os.path.join(common.STATE, 'cloud-poll.json')


def status(cfg: dict, task: str, now: float | None = None) -> tuple[str, str, str]:
    """(state, head, body). One status call per task per cfg poll_s across every runner: a fresh cache entry is
    returned without a process."""
    now = time.time() if now is None else now
    path = poll_cache_path()
    cache = common.read_json(path, {}) or {}
    entry = cache.get(task) if isinstance(cache, dict) else None
    if isinstance(entry, dict) and now - float(entry.get('at') or 0) < cfg["poll_s"] / 2:
        return entry.get('state', 'unknown'), entry.get('head', ''), entry.get('body', '')
    rc, out, err = _run(binary_argv(cfg) + ['cloud', 'status', task], STATUS_TIMEOUT_S)
    if rc:
        raise CloudError(f'status call failed rc {rc}: {common.clip(err or _text(out), 200)}')
    body = _text(out)
    state, head = parse_state(body)
    try:
        with common.file_lock(path + '.lock', timeout_s=5):
            cache = common.read_json(path, {}) or {}
            if not isinstance(cache, dict):
                cache = {}
            cache = {k: v for k, v in cache.items() if isinstance(v, dict) and now - float(v.get('at') or 0) < 86400}
            cache[task] = {'state': state, 'head': head, 'body': body[-4000:], 'at': now}
            common.write_json(path, cache)
    except (OSError, TimeoutError, ValueError):
        pass  # the cache only saves processes
    return state, head, body


def _ledger_path() -> str:
    return os.path.join(common.STATE, 'cloud-occupied.json')


def hold(job_id: str, task: str) -> bool:
    # A task still running in the cloud after its runner gave up (timeout, stop, lost status) keeps counting against
    # cloud.max_parallel until a status call shows it finished or occupancy_ttl_s passes.
    path = _ledger_path()
    for _ in range(3):
        try:
            with common.file_lock(path + '.lock', timeout_s=5):
                ledger = common.read_json(path, {}) or {}
                ledger = ledger if isinstance(ledger, dict) else {}
                ledger[task] = {'job': job_id, 'at': time.time()}
                common.write_json(path, ledger)
            return True
        except (OSError, TimeoutError, ValueError):
            time.sleep(0.2)
    return False


def _held(job_id: str, task: str) -> str:
    if hold(job_id, task):
        return ''
    return (f' WARNING: the occupancy of cloud task {task} could not be recorded, so a new cloud job may start '
            'beyond cloud.max_parallel until it finishes')


def occupied_count(exclude: str | None = None) -> int:
    ledger = common.read_json(_ledger_path(), {}) or {}
    if not isinstance(ledger, dict):
        return 0
    ttl = float(settings(common.load_policy()).get('occupancy_ttl_s') or 86400)
    now = time.time()
    return sum(1 for e in ledger.values() if isinstance(e, dict) and e.get('job') != exclude
               and now - float(e.get('at') or 0) < ttl)


def refresh_occupied(policy: dict | None) -> None:
    # Called by a job that waits for a cloud slot, outside the slot lock: forget tasks that have finished. A failing
    # status call keeps the entry, because an unknown task is treated as still running.
    path = _ledger_path()
    ledger = common.read_json(path, {}) or {}
    if not isinstance(ledger, dict) or not ledger:
        return
    cfg = settings(policy)
    done = []
    for task, entry in list(ledger.items()):
        if not isinstance(entry, dict) or time.time() - float(entry.get('at') or 0) >= float(cfg['occupancy_ttl_s']):
            done.append(task)
            continue
        if not binary_argv(cfg):
            continue
        try:
            state = status(cfg, task)[0]
        except (CloudError, OSError):
            continue
        if state in ('ready', 'failed', 'cancelled'):
            done.append(task)
    if done:
        try:
            with common.file_lock(path + '.lock', timeout_s=5):
                ledger = common.read_json(path, {}) or {}
                for task in done:
                    ledger.pop(task, None)
                common.write_json(path, ledger)
        except (OSError, TimeoutError, ValueError):
            pass


def submit(cfg: dict, branch: str, prompt: str, cwd: str | None) -> tuple[str, str]:
    argv = binary_argv(cfg) + ['cloud', 'exec', '--env', str(cfg['env']), '--branch', branch,
                                '--attempts', str(int(cfg.get('attempts') or 1)), prompt]
    rc, out, err = _run(argv, SUBMIT_TIMEOUT_S, cwd=cwd)
    if rc == 124:
        raise SubmitUnknown(f'submit timed out after {SUBMIT_TIMEOUT_S} s: the task may exist in Codex Cloud on branch '
                         f'{branch}; it is not submitted again')
    text = _text(out) + '\n' + err
    m = re.search(cfg['task_url_re'], text)
    if rc and not m:
        raise CloudError(f'submit failed rc {rc}: {common.clip(err or text, 240)}')
    if rc or not m:
        seen = 'with' if m else 'without'
        raise SubmitUnknown(f'submit returned rc {rc} {seen} a task id on branch {branch}: the task may exist in '
                            f'Codex Cloud, so it is not submitted again ({common.clip(err or text, 160)})')
    return (m.group(1) if m.groups() else m.group(0)), m.group(0)


def diff(cfg: dict, task: str, attempt: int | None = None) -> tuple[int, bytes, str]:
    argv = binary_argv(cfg) + ['cloud', 'diff', task] + (['--attempt', str(attempt)] if attempt else [])
    return _run(argv, DIFF_TIMEOUT_S)


# ---------------------------------------------------------------- snapshot

def resolve_sha(cwd: str, ref: str) -> str:
    rc, out, err = _run(['git', '-C', cwd, 'rev-parse', '--verify', ref + '^{commit}'], 30)
    sha = _text(out).strip()
    if rc or not re.fullmatch(r'[0-9a-f]{40}', sha):
        raise CloudError(f'unknown ref {ref!r} in {cwd}')
    return sha


def _reuse_branch(mirror: dict, ref: str, sha: str, adds: list) -> str | None:
    reuse = mirror.get('reuse')
    if not reuse or adds or ref != reuse.get('ref') or not reuse.get('receipts_glob'):
        return None
    best = None
    for path in glob.glob(reuse['receipts_glob']):
        r = common.read_json(path)
        if not isinstance(r, dict) or any(r.get(k) != v for k, v in (reuse.get('match') or {}).items()):
            continue
        if best is None or str(r.get(reuse.get('order_key', 'at')) or '') > str(best.get(reuse.get('order_key', 'at')) or ''):
            best = r
    if best and best.get(reuse.get('sha_key', 'source_sha')) == sha:
        return reuse.get('branch')
    return None


def ensure_branch(cfg: dict, job_id: str, ref: str, sha: str, adds: list) -> tuple[str, bool]:
    """(branch, pushed). A snapshot with the owner's own command; with no command the ref's branch name itself."""
    mirror = cfg['mirror']
    reused = _reuse_branch(mirror, ref, sha, adds)
    if reused:
        return reused, False
    argv = mirror.get('argv')
    branch = (mirror.get('branch') or 'cloud/{job}').format(job=job_id, ref=ref, sha=sha[:12])
    if not argv:
        raise CloudError('cloud.mirror.argv is not set: no way to publish the snapshot the cloud task runs on')
    flag = mirror.get('add_flag') or '--add'
    full = []
    for part in argv:
        if part == '{adds}':
            for src, dest in adds:
                full += [flag, f'{src}={dest}']
        else:
            full.append(str(part).format(job=job_id, ref=ref, sha=sha, branch=branch))
    lock_path = os.path.join(common.STATE, 'cloud-mirror.lock')
    if mirror.get('lock', True):
        with common.file_lock(lock_path, timeout_s=7200):
            rc, out, err = _run(full, float(mirror.get('timeout_s') or 1800))
    else:
        rc, out, err = _run(full, float(mirror.get('timeout_s') or 1800))
    text = _text(out)
    ok_re = mirror.get('success_re')
    if rc or (ok_re and not re.search(ok_re, text)):
        first = (text.splitlines() or [err or ''])[0]
        raise CloudError(f'snapshot push failed rc {rc}: {common.clip(first or err, 240)}')
    return branch, True


# ---------------------------------------------------------------- one attempt

def attachments(spec: dict) -> list[tuple[str, str]]:
    return [(a['src'], a['dest']) for a in (spec.get('cloud') or {}).get('attach') or []]


def _job_file(job_id: str, name: str) -> str:
    from . import jobs
    return os.path.join(jobs.job_dir(job_id), name)


def _save(job_id: str, name: str, data) -> str:
    from . import jobs
    if isinstance(data, (dict, list)):
        jobs._write_job_json(job_id, name, data)
    else:
        jobs._write_job_text(job_id, name, data)
    return _job_file(job_id, name)


def prompt_for(brief_text: str, cfg: dict, dropped_note: str | None) -> tuple[str, bool]:
    """(prompt, brief_is_attached). A long brief rides as .cloud/brief.md so no command line limit applies."""
    text = brief_text + (('\n\n' + dropped_note) if dropped_note else '')
    if len(text) <= int(cfg['inline_prompt_max']):
        return text, False
    return (f'Your task is in {BRIEF_ATTACH} in the repository root. Read it fully first and follow it exactly. '
            'Make your report your final message.'), True


def run_attempt(job_id: str, n: int, params: dict, spec: dict, policy: dict, stop_requested=lambda: False,
                sleep=time.sleep, clock=time.time) -> dict:
    """One cloud attempt. Returns the attempt dict the receipt expects (engine 'codex', where 'cloud')."""
    from . import jobs
    cfg = settings(policy)
    started = clock()
    a = {'n': n, 'engine': 'codex', 'where': 'cloud', 'model': params.get('model'), 'tier': params.get('tier'),
         'effort': params.get('effort'), 'profile': params['profile'], 'timeout_s': params['timeout_s'],
         'served_models': [params['model']] if params.get('model') else [],
         'served_models_note': 'requested model; the Codex Cloud task does not report the served model',
         'limit_hit': False, 'num_turns': None, 'session_id': None, 'exit_code': None}
    info = {'env': cfg['env'], 'task': None, 'url': None, 'branch': None, 'state': 'preparing', 'patch_sha256': None,
            'firewall': None, 'applied': None, 'source_sha': None}
    a['cloud'] = info

    def finish(status_name: str, error: str | None = None, final: str | None = None) -> dict:
        a['status'] = status_name
        if error:
            a['error'] = error
        if final:
            a['final_text'] = final
        a['exit_code'] = 0 if status_name == 'done' else 1
        return a

    try:
        resumed = common.read_json(_job_file(job_id, 'cloud.json'), None)
        if isinstance(resumed, dict) and resumed.get('state') == 'collected' and resumed.get('attempt') == n:
            info.update({k: resumed.get(k) for k in info if k in resumed})
            return finish('done', final=resumed.get('final') or f"Codex Cloud task finished: {info['url']}")
        if isinstance(resumed, dict) and resumed.get('task') and resumed.get('attempt') == n:
            info.update({k: resumed.get(k) for k in info if k in resumed})  # a restarted runner re-attaches
        elif isinstance(resumed, dict) and resumed.get('state') == 'submitting' and resumed.get('attempt') == n:
            raise SubmitUnknown(f"a runner stopped while submitting this job's cloud task on branch {resumed.get('branch')}: "
                             'the task may exist in Codex Cloud, so it is not submitted again. Look for it there, '
                             'then start a new job if it is missing')
        else:
            _submit_phase(job_id, n, cfg, spec, policy, info)
        _save(job_id, 'cloud.json', dict(info, attempt=n))
        jobs._merge_status(job_id, where='cloud', cloud_task=info['task'], cloud_url=info['url'],
                           cloud_state='submitted')
        state, body = _poll_phase(job_id, n, cfg, info, params['timeout_s'], started, stop_requested, sleep, clock)
        if state == 'stopped':
            return finish('stopped', 'stop requested; the cloud task keeps running in the cloud (the CLI has no cancel)'
                          + _held(job_id, info['task']))
        if state == 'timeout':
            return finish('timeout', f"timed out after {params['timeout_s']} s waiting for the cloud task"
                          + _held(job_id, info['task']))
        if state in ('failed', 'cancelled'):
            return finish('failed', f'cloud task {state}: {info["url"]}', final=body)
        final = _collect_phase(job_id, cfg, spec, info, n, body, a)
        return finish('done', final=final)
    except SubmitUnknown as e:
        return finish('failed', str(e))  # cloud.json keeps state `submitting`: nothing may submit this attempt again
    except CloudError as e:
        note = ''
        if info.get('task') and info.get('state') in ('submitted', 'running', 'unknown'):
            note = _held(job_id, info['task'])  # the poll gave up, the task did not
        info['state'] = 'failed'
        _save(job_id, 'cloud.json', dict(info, attempt=n, error=str(e)))
        return finish('failed', str(e) + note)
    finally:
        a['duration_s'] = round(clock() - started, 1)


def _submit_phase(job_id: str, n: int, cfg: dict, spec: dict, policy: dict, info: dict) -> None:
    from . import jobs
    if cfg.get('remote') and cfg.get('clone_dir') and not os.path.isdir(cfg['clone_dir']):
        rc, _, err = _run(['git', 'clone', '-q', '--depth', '1', cfg['remote'], cfg['clone_dir']], 600)
        if rc:
            raise CloudError(f'cannot clone the cloud repo: {common.clip(err, 200)}')
    fw = firewall.settings(policy)
    adds = attachments(spec)
    for src, dest in adds:
        bad = firewall.safe_dest(dest)
        if bad:
            raise CloudError(f'attachment {dest!r}: {bad}')
    # Copy once, scan the copy, send the copy: what was checked is exactly what leaves, whatever changes on disk meanwhile.
    stage = os.path.join(jobs.job_dir(job_id), 'stage')
    os.makedirs(stage, exist_ok=True)
    try:
        brief_stage = os.path.join(stage, 'brief.md')
        shutil.copyfile(jobs._p(job_id, 'brief.md'), brief_stage)
        staged = []
        for k, (src, dest) in enumerate(adds):
            copy = os.path.join(stage, f'attach-{k}')
            shutil.copyfile(src, copy)
            staged.append((copy, dest))
    except OSError as e:
        raise CloudError(f'cannot stage the files to send: {type(e).__name__}')
    adds = staged
    brief_text = jobs._read_text(brief_stage)
    dropped = None
    prompt, attach_brief = prompt_for(brief_text, cfg, dropped)
    items = [('brief', brief_stage)] + [(dest, src) for src, dest in adds]
    result = firewall.scan(items, fw)
    info['firewall'] = {'ok': result['ok'], 'checked': result['checked'],
                        'blocked': [{'file': b['file'], 'reasons': b['reasons'][:5]} for b in result['blocked']]}
    if not result['ok']:
        raise CloudError(firewall.verdict_line(result))
    if attach_brief:
        adds = adds + [(brief_stage, BRIEF_ATTACH)]
    ref = (spec.get('cloud') or {}).get('ref') or cfg.get('default_ref') or 'HEAD'
    sha = resolve_sha(spec['cwd'], ref)
    info['source_sha'] = sha
    tree = firewall.scan_tree(spec['cwd'], sha, fw, cache_path=os.path.join(common.STATE, 'cloud-snapshot-scan.json'))
    info['firewall']['snapshot'] = {k: tree.get(k) for k in ('ok', 'checked', 'skipped_large', 'skipped_binary',
                                                              'cached', 'skipped') if tree.get(k) is not None}
    if not tree['ok']:
        raise CloudError(firewall.verdict_line(tree))
    jobs._merge_status(job_id, where='cloud', cloud_state='snapshot')
    branch, _pushed = ensure_branch(cfg, job_id, ref, sha, adds)
    info['branch'] = branch
    note_file = (cfg.get('mirror') or {}).get('dropped_note_file')
    if note_file and os.path.isfile(note_file):
        names = jobs._read_text(note_file)[:4000]
        extra = ('\n\n## Files absent from this snapshot (data policy). If you need one, stop and say which; '
                 'never re-create it:\n' + names)
        if firewall.scan_text(extra, fw):
            raise CloudError('FIREWALL BLOCK: the mirror\'s list of left-out files failed the scan')
        prompt += extra
    jobs._merge_status(job_id, cloud_state='submitting')
    _save(job_id, 'cloud.json', dict(info, attempt=n, state='submitting'))  # a crash from here on must not resubmit
    task, url = submit(cfg, branch, prompt, cfg.get('clone_dir') if cfg.get('clone_dir') and os.path.isdir(cfg['clone_dir']) else None)
    info.update(task=task, url=url, state='submitted')


def _poll_phase(job_id, n, cfg, info, timeout_s, started, stop_requested, sleep, clock):
    from . import jobs
    fails = unknown = 0
    last = None
    while True:
        if stop_requested():
            return 'stopped', ''
        if clock() - started > timeout_s:
            return 'timeout', ''
        try:
            state, head, body = status(cfg, info['task'], now=clock())
            fails = 0
        except CloudError as e:
            fails += 1
            if fails >= int(cfg['poll_fail_max']):
                raise CloudError(f'cloud unavailable: {fails} status calls in a row failed ({e})')
            sleep(max(5.0, cfg['poll_s']))
            continue
        unknown = unknown + 1 if state == 'unknown' else 0
        if unknown >= 20:
            raise CloudError('cloud status output has no readable state after 20 polls')
        if state != last:
            info['state'] = state
            jobs._merge_status(job_id, cloud_state=state)
            _save(job_id, 'cloud.json', dict(info, attempt=n))
            last = state
        if state in ('ready', 'failed', 'cancelled'):
            return state, body
        sleep(cfg['poll_s'])


def _collect_phase(job_id, cfg, spec, info, n, body, a) -> str:
    from . import jobs
    rc, raw, err = diff(cfg, info['task'], n if int(cfg.get('attempts') or 1) > 1 else None)
    note = None
    if rc or not raw.strip():
        note = 'the cloud task produced no diff' if not rc else f'diff call failed rc {rc}: {common.clip(err, 160)}'
        if rc and spec.get('profile') == 'code':
            raise CloudError(f'cannot read the cloud diff: {note}')
    if raw.strip():
        why = bad_patch_path(raw, spec['cwd'])
        if why:
            raise CloudError(f'the cloud patch is refused: {why}')
        patch = _job_file(job_id, 'result.patch')
        os.makedirs(os.path.dirname(patch), exist_ok=True)
        with open(patch, 'wb') as f:
            f.write(raw)
        info['patch_sha256'] = hashlib.sha256(raw).hexdigest()
        info['patch_path'] = patch
        if spec.get('profile') == 'code' and cfg.get('apply_to_worktree', True):
            prior = common.read_json(_job_file(job_id, 'cloud.json'), None)
            recovering = isinstance(prior, dict) and prior.get('state') == 'applying' and prior.get('attempt') == n
            pre = prior.get('pre_state') if recovering else touched_state(spec['cwd'], patch)
            post = prior.get('post_state') if recovering else expected_state(spec['cwd'], patch)
            _save(job_id, 'cloud.json', dict(info, attempt=n, state='applying', pre_state=pre, post_state=post))
            info['applied'] = _apply(spec['cwd'], patch, recovering, pre, post)
    elif spec.get('profile') == 'code':
        info['applied'] = 'no diff'
    final = body.split('\n', 1)[1].strip() if '\n' in body else ''
    final = final or f"Codex Cloud task finished: {info['url']}"
    if note:
        final = f'({note})\n\n{final}'
    _save(job_id, 'cloud.json', dict(info, attempt=n, state='collected', final=common.clip(final, 20000)))
    jobs._merge_status(job_id, cloud_state='collected')
    return final


def _unsafe_path(path: str) -> str | None:
    raw = path.replace('\\', '/')
    parts = [p for p in raw.split('/') if p not in ('', '.')]
    if raw.startswith('/') or re.match(r'^[A-Za-z]:', raw) or '..' in parts:
        return 'a path leaves the worktree'
    if any(re.search(r'~\d+(?:\.|$)', p) for p in parts):
        return 'a path uses a short-name alias (name~1)'
    names = [p.rstrip('. ').lower().split(':')[0] for p in parts]
    if names and names[0] == '.cloud':
        return 'it touches .cloud/ context files'
    if '.git' in names:
        return 'a path leaves the worktree'
    return None


def bad_patch_path(raw: bytes, cwd: str) -> str | None:
    """Why a patch may not be applied, or None. The paths are the ones git itself reports (`git apply --numstat -z`
    strips the same leading component and decodes the same quoting as the real apply), both sides of every rename
    and copy included, so nothing can be moved into .cloud/ or out of the worktree by spelling."""
    # a symlink is mode 120000 (octal; leading zeros and any spacing count). Reject on the header text AND on what git
    # itself reports in --summary, so a spelling the regex misses still shows up in git's own reading of the patch.
    if re.search(rb'(?m)^(?:new file mode|new mode|old mode|deleted file mode|index \S+)\s+0*12\d{4}\b', raw):
        return 'it creates or changes a symbolic link'
    try:
        q = subprocess.run(['git', '-C', cwd, 'apply', '--summary'], input=raw, capture_output=True, timeout=120,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return 'git could not read the patch'
    if q.returncode:
        return 'git could not read the patch'
    if re.search(rb'(?m)(?:\bmode\s+(?:change\s+\d+\s+=>\s+)?0*12\d{4}\b|=>\s*0*12\d{4}\b)', q.stdout):
        return 'it creates or changes a symbolic link'
    try:
        p = subprocess.run(['git', '-C', cwd, 'apply', '--numstat', '-z'], input=raw, capture_output=True, timeout=120,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return 'git could not read the patch'
    if p.returncode:
        return 'git could not read the patch'
    items = p.stdout.split(b'\0')
    i = 0
    while i < len(items):
        item = items[i]
        if b'\t' not in item:
            i += 1
            continue
        path = item.split(b'\t', 2)[2]
        names = items[i + 1:i + 3] if not path else [path]
        i += 3 if not path else 1
        for name in names:
            why = _unsafe_path(name.decode('utf-8', 'replace'))
            if why:
                return why
    return None


def _patch_paths(raw: bytes, cwd: str) -> list[str] | None:
    # every path the patch names (both sides of renames), as git itself reads them; None when git cannot read it
    try:
        p = subprocess.run(['git', '-C', cwd, 'apply', '--numstat', '-z'], input=raw, capture_output=True, timeout=120,
                           creationflags=NO_WINDOW)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode:
        return None
    paths, items, i = [], p.stdout.split(b'\0'), 0
    while i < len(items):
        if b'\t' not in items[i]:
            i += 1
            continue
        path = items[i].split(b'\t', 2)[2]
        names = items[i + 1:i + 3] if not path else [path]
        i += 3 if not path else 1
        paths += [n.decode('utf-8', 'replace') for n in names]
    return paths


def _fingerprint(root: str, paths: list[str]) -> str | None:
    h = hashlib.sha256()
    for rel in sorted(set(paths)):
        full = os.path.join(root, *rel.split('/'))
        h.update(rel.encode('utf-8') + b'\0')
        try:
            with open(full, 'rb') as f:
                h.update(hashlib.sha256(f.read()).digest())
        except FileNotFoundError:
            h.update(b'absent')
        except OSError:
            return None
    return h.hexdigest()


def touched_state(cwd: str, patch: str) -> str | None:
    # A fingerprint of the files the patch touches as they are now. Recorded before the apply, with the fingerprint the
    # patch must produce, so a restart can tell "not applied yet" from "applied" from "something else".
    with open(patch, 'rb') as f:
        paths = _patch_paths(f.read(), cwd)
    return None if paths is None else _fingerprint(cwd, paths)


def expected_state(cwd: str, patch: str) -> str | None:
    # The fingerprint after the patch, found by applying it to a scratch copy of the touched files.
    with open(patch, 'rb') as f:
        paths = _patch_paths(f.read(), cwd)
    if paths is None:
        return None
    with tempfile.TemporaryDirectory(prefix='twinlane-post-') as tmp:
        for rel in set(paths):
            src = os.path.join(cwd, *rel.split('/'))
            if os.path.isfile(src):
                dest = os.path.join(tmp, *rel.split('/'))
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                shutil.copyfile(src, dest)
        rc, _, _ = _run(['git', 'apply', os.path.abspath(patch)], 120, cwd=tmp)
        return None if rc else _fingerprint(tmp, paths)


def _apply(cwd: str, patch: str, recovering: bool = False, pre_state: str | None = None,
           post_state: str | None = None) -> str:
    if not common.is_linked_worktree(cwd):
        raise CloudError('refusing to apply a cloud patch outside a linked worktree')
    if recovering:  # an earlier run stopped while applying: the files must match what was recorded, or we stop
        now = touched_state(cwd, patch)
        if not pre_state or not post_state or now is None:
            raise CloudError('an earlier run stopped while applying this patch and its states were not recorded: check '
                             'the worktree and apply result.patch by hand if it is missing')
        if pre_state == post_state:  # a patch whose effect the content fingerprint cannot see (a mode change)
            raise CloudError('an earlier run stopped while applying a patch whose effect cannot be told from its starting '
                             'state (a mode-only change): check the worktree')
        if now == post_state:
            return 'applied'
        if now != pre_state:
            raise CloudError('an earlier run stopped while applying this patch and the files now match neither the '
                             'state before nor the state after it: check the worktree')
    rc, _, err = _run(['git', '-C', cwd, 'apply', '--check', patch], 120)
    if rc:
        back, _, _ = _run(['git', '-C', cwd, 'apply', '--reverse', '--check', patch], 120)
        if not back:
            return 'applied'  # an earlier run of this attempt already applied it before it could record that
        raise CloudError('apply-conflict: git apply --check failed: ' + common.clip(err, 300))
    rc, _, err = _run(['git', '-C', cwd, 'apply', patch], 120)
    if rc:
        raise CloudError('git apply failed after a clean check: ' + common.clip(err, 300))
    return 'applied'
