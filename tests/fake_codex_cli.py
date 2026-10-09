"""Fixture only: emulate the documented codex exec JSONL surface."""
import json
import os
import subprocess
import sys
import time


def emit(value):
    print(json.dumps(value), flush=True)


args = sys.argv[1:]
assert 'exec' in args and '--json' in args
assert '--sandbox' in args and '--model' in args and '--cd' in args
assert '--allow-credits' not in args
prompt = sys.stdin.read()
assert prompt
mode = os.environ.get('FAKE_CODEX_MODE', 'success')
if mode == 'hang':
    marker = os.environ['FAKE_CODEX_MARKER']
    subprocess.Popen([sys.executable, '-c',
                      'import pathlib,time,sys; time.sleep(2); pathlib.Path(sys.argv[1]).write_text("survived")',
                      marker])
    time.sleep(30)
emit({'type': 'thread.started', 'thread_id': '00000000-0000-4000-8000-000000000001'})
if mode in ('success', 'parts', 'no_usage'):
    final = 'Fixture task completed.'
    path = args[args.index('--output-last-message') + 1]
    with open(path, 'w', encoding='utf-8') as out:
        out.write(final)
    emit({'type': 'item.completed', 'item': {'type': 'agent_message', 'text': final}})
    usage = {'input_tokens': 12, 'output_tokens': 4}
    if mode not in ('parts', 'no_usage'):
        usage['total_tokens'] = 16
    emit({'type': 'turn.completed', 'usage': {} if mode == 'no_usage' else usage})
    sys.exit(0)
if mode == 'limit':
    emit({'type': 'turn.failed', 'error': {'code': 'usage_limit_reached', 'message': 'usage limit reached'}})
    sys.exit(1)
emit({'type': 'error', 'message': 'unexpected crash'})
sys.exit(5)
