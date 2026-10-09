"""Print a real twinlane board from disposable fixture data; never contact a vendor."""
from __future__ import annotations

import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def iso(offset=0):
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=offset)).isoformat().replace('+00:00', 'Z')


def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2), encoding='utf-8')


def main():
    with tempfile.TemporaryDirectory(prefix='twinlane-demo-') as folder:
        root = Path(folder)
        home = root / 'home'
        env = dict(os.environ, TWINLANE_HOME=str(home), HOME=str(root), USERPROFILE=str(root),
                   LOCALAPPDATA=str(root), XDG_DATA_HOME=str(root))
        meter = {
            'read_at': iso(-10),
            'claude': {'status': 'caution', 'source': 'fixture', 'reading_at': iso(-10),
                       'max_pct': 77, 'next_reset': iso(7200), 'extra_usage_enabled': False,
                       'windows': [{'name': 'five_hour', 'pct': 77, 'resets_at': iso(7200)},
                                   {'name': 'seven_day', 'pct': 31, 'resets_at': iso(200000)}]},
            'codex': {'status': 'ok', 'source': 'fixture', 'reading_at': iso(-10),
                      'max_pct': 42, 'next_reset': iso(10800), 'on_credits': False,
                      'windows': [{'name': '5h', 'pct': 42, 'resets_at': iso(10800)},
                                  {'name': '7d', 'pct': 18, 'resets_at': iso(260000)}]}}
        write(home / 'state' / 'meter.json', meter)
        stamp = dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')
        for suffix, engine, status, age in [('a1', 'claude', 'done', 300),
                                             ('b2', 'codex', 'done', 180),
                                             ('c3', 'codex', 'running', 60)]:
            jid = '{}-{}-{}'.format(stamp, engine, suffix)
            job = home / 'jobs' / jid
            write(job / 'job.json', {'id': jid, 'name': 'demo-' + suffix, 'kind': 'code',
                                     'cwd': str(root / 'example-repo'),
                                     'primary': {'engine': engine, 'model': 'claude-sonnet-5-5' if engine == 'claude' else 'gpt-6.1-sol',
                                                 'tier': 'sol', 'profile': 'code', 'timeout_s': 3600},
                                     'created_at': iso(-age)})
            if status == 'running':
                write(job / 'status.json', {'status': 'running', 'pid': os.getpid(), 'engine': engine,
                                            'started_at': iso(-age), 'created_at': iso(-age)})
            else:
                (job / 'final.md').write_text('Completed demo task.\n', encoding='utf-8')
                write(job / 'result.json', {'id': jid, 'status': status, 'engine': engine,
                                            'kind': 'code', 'name': 'demo-' + suffix,
                                            'requested': {'model': 'claude-sonnet-5-5' if engine == 'claude' else 'gpt-6.1-sol',
                                                          'tier': 'sol', 'profile': 'code'},
                                            'served_models': ['claude-sonnet-5-5'] if engine == 'claude' else [],
                                            'started_at': iso(-age), 'ended_at': iso(-age + 30),
                                            'final_path': str(job / 'final.md')})
        write(home / 'batons' / 'demo.json', {'baton_version': 1, 'slug': 'demo',
                                              'updated_at': iso(-20), 'owner': {'engine': 'codex'},
                                              'progress': {'next_action': 'Review the latest receipt', 'in_flight': []},
                                              'holds': {'approvals_pending': []}, 'repo': {}})
        binary = shutil.which('twinlane')
        cmd = [binary, 'board'] if binary else [sys.executable, '-m', 'twinlane.cli', 'board']
        result = subprocess.run(cmd, env=env, cwd=Path(__file__).resolve().parents[2],
                                capture_output=True, text=True, check=False)
        print(result.stdout, end='')
        if result.returncode:
            print(result.stderr, file=sys.stderr, end='')
        return result.returncode


if __name__ == '__main__':
    sys.exit(main())
