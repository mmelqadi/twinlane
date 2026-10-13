"""Built-in Codex runner evidence and failure tests; no vendor calls."""
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

import test_support  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
FAKE = Path(__file__).with_name('fake_codex_cli.py')
sys.path.insert(0, str(ROOT))
from twinlane.codex_runner import command as runner_command  # noqa: E402


class Runner(unittest.TestCase):
    def test_success_limit_and_crash(self):
        for mode, expected in [('success', 0), ('parts', 0), ('no_usage', 0), ('limit', 6), ('crash', 5)]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory(prefix='runner-', dir=os.environ['TWINLANE_HOME']) as tmp:
                base = Path(tmp)
                brief = base / 'brief.md'
                brief.write_text('Review the fixture.', encoding='utf-8')
                out = base / 'evidence'
                env = dict(os.environ, TWINLANE_CODEX_BIN=str(FAKE), FAKE_CODEX_MODE=mode)
                proc = subprocess.run([sys.executable, str(ROOT / 'twinlane' / 'codex_runner.py'),
                                       '--brief', str(brief), '--cd', str(base), '--model', 'gpt-6.1-sol',
                                       '--effort', 'high', '--out', str(out), '--timeout', '10'],
                                      env=env, capture_output=True, text=True, timeout=20)
                self.assertEqual(proc.returncode, expected, proc.stderr)
                self.assertTrue((out / 'events.jsonl').is_file())
                self.assertTrue((out / 'result.txt').is_file())
                events = [json.loads(line) for line in (out / 'events.jsonl').read_text(encoding='utf-8').splitlines()]
                self.assertEqual(events[0]['type'], 'thread.started')
                if mode in ('success', 'parts', 'no_usage'):
                    self.assertEqual((out / 'last-message.md').read_text(encoding='utf-8'), 'Fixture task completed.')
                    self.assertIn('tokens=' + ('unknown' if mode == 'no_usage' else '16'),
                                  (out / 'result.txt').read_text(encoding='utf-8'))

    @unittest.skipIf(os.name == 'nt', 'POSIX process groups only')
    def test_timeout_kills_codex_child_group(self):
        with tempfile.TemporaryDirectory(prefix='runner-timeout-') as tmp:
            base = Path(tmp)
            brief = base / 'brief.md'
            brief.write_text('Fixture.', encoding='utf-8')
            marker = base / 'survived.txt'
            env = dict(os.environ, TWINLANE_CODEX_BIN=str(FAKE), FAKE_CODEX_MODE='hang',
                       FAKE_CODEX_MARKER=str(marker))
            proc = subprocess.run([sys.executable, str(ROOT / 'twinlane' / 'codex_runner.py'),
                                   '--brief', str(brief), '--cd', str(base), '--model', 'gpt-6.1-sol',
                                   '--effort', 'low', '--out', str(base / 'evidence'), '--timeout', '0.3'],
                                  env=env, capture_output=True, text=True, timeout=8)
            self.assertEqual(proc.returncode, 124, proc.stderr)
            time.sleep(2.5)
            self.assertFalse(marker.exists(), 'the child outlived its timed-out process group')

    def test_exec_config_reaches_codex_exec(self):
        config = ['features.apps=false', 'mcp_servers.demo.enabled=false', 'notify=["a", "b=c"]']
        # The argv the Codex CLI is started with: each entry is its own "-c key=value" pair, and the model,
        # effort, sandbox and --cd flags twinlane owns are still there.
        argv = runner_command('codex', 'b.md', 'W', 'gpt-6.1-sol', 'high', False, 'F', config)
        pairs = [(argv[i], argv[i + 1]) for i in range(len(argv) - 1) if argv[i] == '-c']
        self.assertEqual([v for _, v in pairs], config)
        self.assertEqual(argv[argv.index('--sandbox') + 1], 'read-only')
        self.assertIn('model_reasoning_effort="high"', argv)
        self.assertNotIn('-c', runner_command('codex', 'b.md', 'W', 'm', 'low', True, 'F'))
        # And through the runner process: --exec-config (repeatable) reaches the child codex exec.
        with tempfile.TemporaryDirectory(prefix='runner-config-', dir=os.environ['TWINLANE_HOME']) as tmp:
            base = Path(tmp)
            record = base / 'argv.json'
            recorder = base / 'recorder.py'
            recorder.write_text(
                'import json, os, runpy, sys\n'
                'open(os.environ["RECORD_ARGV"], "w").write(json.dumps(sys.argv[1:]))\n'
                'runpy.run_path(os.environ["REAL_FAKE"], run_name="__main__")\n', encoding='utf-8')
            brief = base / 'brief.md'
            brief.write_text('Review the fixture.', encoding='utf-8')
            env = dict(os.environ, TWINLANE_CODEX_BIN=str(recorder), RECORD_ARGV=str(record), REAL_FAKE=str(FAKE),
                       FAKE_CODEX_MODE='success')
            cmd = [sys.executable, str(ROOT / 'twinlane' / 'codex_runner.py'), '--brief', str(brief), '--cd', str(base),
                   '--model', 'gpt-6.1-sol', '--effort', 'high', '--out', str(base / 'evidence'), '--timeout', '10']
            for item in config:
                cmd += ['--exec-config', item]
            proc = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=20)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            seen = json.loads(record.read_text(encoding='utf-8'))
            self.assertEqual([seen[i + 1] for i in range(len(seen) - 1) if seen[i] == '-c'], config)
            self.assertEqual(seen[0], 'exec')

    def test_module_help(self):
        proc = subprocess.run([sys.executable, '-m', 'twinlane', '--help'], cwd=ROOT,
                              capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


if __name__ == '__main__':
    unittest.main()
