"""Run a Codex CLI job and write the same evidence contract as an optional adapter.

Built and tested against the `codex exec --json` interface (flags `--json --model
--config model_reasoning_effort=... --sandbox --cd --output-last-message`). Older
Codex CLI releases may not accept current model names; run `codex --version` and
set your models in config. Usage is checked by the caller when a job starts.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import stat
import subprocess
import sys
from pathlib import Path


LIMIT_MARKERS = ('usage_limit', 'rate_limit', 'quota', 'limit reached', 'usage limit')
# Set by a preserving start (twinlane common.CODEX_BIN_QUALIFIED): the identity of the one binary it may run.
QUALIFIED = 'TWINLANE_CODEX_BIN_QUALIFIED'
IDENTITY_KEYS = ('path', 'size', 'mtime_ns', 'file_id', 'dev', 'sha256')
EXIT_REFUSED = 3


def handoff_problem(binary: str | None, handoff: str) -> str | None:
    """Why `binary` is not the qualified binary handed over (None when it is). twinlane checked it before
    starting this runner; checking again here, right before the child starts, narrows the window between."""
    try:
        want = json.loads(handoff)
    except ValueError:
        return 'the qualified binary hand-off is malformed'
    if not isinstance(want, dict) or set(want) != set(IDENTITY_KEYS):
        return 'the qualified binary hand-off is malformed'
    if not binary or binary != want['path']:
        return 'TWINLANE_CODEX_BIN is not the qualified binary'
    try:
        st = os.lstat(binary)
        key = (st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev)
        if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or \
                key != (want['size'], want['mtime_ns'], want['file_id'], want['dev']):
            return 'the qualified binary changed or is no longer a standalone file'
        digest = hashlib.sha256()
        with open(binary, 'rb') as h:
            for chunk in iter(lambda: h.read(1 << 20), b''):
                digest.update(chunk)
        after = os.lstat(binary)
    except OSError as exc:
        return f'the qualified binary cannot be verified ({type(exc).__name__})'
    if (after.st_size, after.st_mtime_ns, after.st_ino, after.st_dev) != key or digest.hexdigest() != want['sha256']:
        return 'the qualified binary bytes changed'
    return None


def command(binary: str, brief: str, cwd: str, model: str, effort: str, write: bool,
            final_path: str, exec_config=()) -> list[str]:
    launch = [sys.executable, binary] if binary.lower().endswith('.py') else [binary]
    extra = [part for item in exec_config for part in ('-c', item)]
    return launch + ['exec', '--json', '--model', model,
            '--config', 'model_reasoning_effort=' + json.dumps(effort)] + extra + [
            '--sandbox', 'workspace-write' if write else 'read-only',
            '--cd', cwd, '--output-last-message', final_path, '-']


def run(argv=None) -> int:
    p = argparse.ArgumentParser(prog='twinlane codex runner')
    p.add_argument('--brief', required=True)
    p.add_argument('--cd', required=True)
    p.add_argument('--model', required=True)
    p.add_argument('--effort', required=True)
    p.add_argument('--out', required=True)
    p.add_argument('--timeout', type=float, required=True)
    p.add_argument('--write', action='store_true')
    p.add_argument('--preserve-existing-metadata', action='store_true')
    p.add_argument('--exec-config', action='append', default=[], metavar='KEY=VALUE',
                   help='passed to codex exec as -c KEY=VALUE (repeatable)')
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    events_path = out / 'events.jsonl'
    final_path = out / 'last-message.md'
    result_path = out / 'result.txt'
    stderr_path = out / 'stderr.log'
    binary = os.environ.get('TWINLANE_CODEX_BIN')
    handoff = os.environ.get(QUALIFIED)
    if a.preserve_existing_metadata and handoff is None:
        stderr_path.write_text('twinlane codex runner refused: preserving mode requires the qualified binary hand-off\n',encoding='utf-8')
        return EXIT_REFUSED
    if handoff is not None:  # a preserving start: this exact binary or nothing, never the codex on PATH
        problem = handoff_problem(binary, handoff)
        if problem:
            stderr_path.write_text(f'twinlane codex runner refused: {problem}\n', encoding='utf-8')
            return EXIT_REFUSED
    binary = binary or 'codex'
    args = command(binary, a.brief, a.cd, a.model, a.effort, a.write, str(final_path), a.exec_config)
    try:
        prompt = Path(a.brief).read_text(encoding='utf-8')
        with events_path.open('wb') as events, stderr_path.open('wb') as errors:
            proc = subprocess.Popen(args, cwd=a.cd, stdin=subprocess.PIPE, stdout=events,
                                    stderr=errors, start_new_session=os.name != 'nt')
            try:
                proc.communicate(prompt.encode('utf-8'), timeout=a.timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired:
                if os.name != 'nt':
                    try:
                        os.killpg(proc.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    proc.kill()
                proc.communicate()
                rc = 124
    except (OSError, ValueError) as exc:
        stderr_path.write_text(str(exc), encoding='utf-8')
        rc = 127
    session = 'unknown'
    tokens = None
    limit = False
    final_event = ''
    try:
        with events_path.open(encoding='utf-8', errors='replace') as stream:
            for line in stream:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                typ = event.get('type')
                if typ in ('thread.started', 'session.started'):
                    session = event.get('thread_id') or event.get('session_id') or session
                if typ in ('turn.completed', 'turn.failed'):
                    usage = event.get('usage') or {}
                    total = usage.get('total_tokens')
                    parts = (usage.get('input_tokens'), usage.get('output_tokens'))
                    tokens = total if isinstance(total, (int, float)) else (
                        sum(parts) if all(isinstance(part, (int, float)) for part in parts) else None)
                if typ in ('error', 'turn.failed'):
                    payload = json.dumps({k: event.get(k) for k in ('message', 'error', 'code')}).lower()
                    limit = limit or any(marker in payload for marker in LIMIT_MARKERS)
                item = event.get('item') or {}
                if typ == 'item.completed' and item.get('type') == 'agent_message':
                    final_event = item.get('text') or final_event
    except OSError:
        pass
    if not final_path.is_file() and final_event:
        final_path.write_text(final_event, encoding='utf-8')
    result_path.write_text('exit={0} session={1} model={2} effort={3} sandbox={4} tokens={5}\n'.format(
        rc, session, a.model, a.effort, 'workspace-write' if a.write else 'read-only',
        tokens if tokens is not None else 'unknown'),
        encoding='utf-8')
    if limit:
        return 6
    return rc


if __name__ == '__main__':
    sys.exit(run())
