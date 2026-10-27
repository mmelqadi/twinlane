"""Fixture only: emulate the `codex cloud` subcommands twinlane uses (exec, status, diff).

State lives in FAKE_CLOUD_DIR. FAKE_CLOUD_MODE: ok (default), exec_fail, fail (task ends [ERROR]),
status_fail (every status call exits 1), nodiff, cloudpath (the patch touches .cloud/), renamecloud (a rename
into .cloud/), exec_slow (exec outlives its caller), exec_noid (exec exits 0 without printing a task id), slow (never ready).
FAKE_CLOUD_POLLS_RUNNING: status calls that answer [RUNNING] before the task is ready (default 1).
"""
import json
import os
import sys
import time

state = os.environ['FAKE_CLOUD_DIR']
mode = os.environ.get('FAKE_CLOUD_MODE', 'ok')
args = sys.argv[1:]
assert args and args[0] == 'cloud', args
sub = args[1]
os.makedirs(state, exist_ok=True)


def counter(name):
    path = os.path.join(state, name)
    n = int(open(path).read()) + 1 if os.path.exists(path) else 1
    with open(path, 'w') as h:
        h.write(str(n))
    return n


if sub == 'exec':
    if mode == 'exec_fail':
        sys.stderr.write('error: environment not found\n')
        sys.exit(2)
    n = counter('exec-count')
    rec = {'env': args[args.index('--env') + 1], 'branch': args[args.index('--branch') + 1],
           'attempts': args[args.index('--attempts') + 1], 'prompt': args[-1], 'cwd': os.getcwd()}
    with open(os.path.join(state, f'exec-{n}.json'), 'w', encoding='utf-8') as h:
        json.dump(rec, h)
    if mode == 'exec_slow':  # accepted remotely, but the caller times out before any task id comes back
        time.sleep(6)
    if mode == 'exec_noid':
        print('Queued.')
        sys.exit(0)
    print(f'Submitted https://chatgpt.com/codex/tasks/task_e_fake{n}')
    sys.exit(0)

task = args[2]
if sub == 'status':
    if mode == 'status_fail':
        sys.stderr.write('network unreachable\n')
        sys.exit(1)
    calls = counter(f'status-{task}')
    if mode == 'slow' or calls <= int(os.environ.get('FAKE_CLOUD_POLLS_RUNNING', '1')):
        print('[RUNNING] fixture task')
    elif mode == 'fail':
        print('[ERROR] fixture task')
    else:
        print('[READY] fixture task\nReview: all good. No findings.')
    sys.exit(0)
if sub == 'diff':
    if mode == 'nodiff':
        sys.exit(0)
    if mode == 'renamecloud':
        sys.stdout.write('diff --git a/src/a.txt b/.cloud/a.txt\nsimilarity index 100%\nrename from src/a.txt\n'
                         'rename to .cloud/a.txt\n')
        sys.exit(0)
    path = '.cloud/leak.txt' if mode == 'cloudpath' else 'cloud_result.txt'
    sys.stdout.write(f'diff --git a/{path} b/{path}\nnew file mode 100644\nindex 0000000..e69de29\n'
                     f'--- /dev/null\n+++ b/{path}\n@@ -0,0 +1 @@\n+made in the cloud\n')
    sys.exit(0)
sys.exit(64)
