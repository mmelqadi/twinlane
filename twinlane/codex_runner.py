"""Run a Codex CLI job and write the same evidence contract as an optional adapter.

Built and tested against the `codex exec --json` interface (flags `--json --model
--config model_reasoning_effort=... --sandbox --cd --output-last-message`). Older
Codex CLI releases may not accept current model names; run `codex --version` and
set your models in config. Usage is checked by the caller when a job starts.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from pathlib import Path


LIMIT_MARKERS = ('usage_limit', 'rate_limit', 'quota', 'limit reached', 'usage limit')


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
    p.add_argument('--exec-config', action='append', default=[], metavar='KEY=VALUE',
                   help='passed to codex exec as -c KEY=VALUE (repeatable)')
    a = p.parse_args(argv)
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    events_path = out / 'events.jsonl'
    final_path = out / 'last-message.md'
    result_path = out / 'result.txt'
    stderr_path = out / 'stderr.log'
    binary = os.environ.get('TWINLANE_CODEX_BIN') or 'codex'
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
