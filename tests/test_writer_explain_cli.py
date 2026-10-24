"""`twinlane explain-writer-admission`: read-only, proposal-only, temp stores only.

Every run here uses an isolated temporary home; process observations are injected except for the test
process itself. Lock, write, cleanup, status and process-spawn helpers are replaced with tripwires.
"""
import builtins
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
from twinlane import cli, common, jobs, writer_admission as wa  # noqa: E402
from test_writer_admission import Store, table_observer, tree  # noqa: E402

ALIVE, DEAD, UNKNOWN = common.ALIVE, common.DEAD, common.UNKNOWN

FORBIDDEN = [
    (jobs, '_claim_writer'), (jobs, '_release_writer'), (jobs, '_writer_job_busy'), (jobs, '_engine_state'),
    (jobs, '_child_pending'), (jobs, '_clear_child_starting'), (jobs, '_claim_runner'), (jobs, '_release_runner'),
    (jobs, 'job_status'), (jobs, 'list_jobs'), (jobs, 'running_count'), (jobs, '_prior_live'),
    (jobs, 'mark_collected'), (jobs, '_write_status'), (jobs, '_merge_status'), (jobs, 'slot_lock'),
    (common, 'file_lock'), (common, 'write_json'), (common, 'write_text'), (common, '_atomic_write'),
    (common, 'ensure_dirs'), (common, 'kill_tree'), (common, 'spawn_detached_ex'), (common, 'run'),
    (os, 'remove'), (os, 'unlink'), (os, 'rmdir'), (os, 'mkdir'), (os, 'makedirs'), (os, 'replace'),
    (os, 'rename'), (shutil, 'rmtree'), (subprocess, 'Popen'),
]


class ExplainCommand(unittest.TestCase):
    def setUp(self):
        self.s = Store(self)
        for key, value in (('JOBS', self.s.jobs), ('STATE', self.s.state), ('HOME_DIR', self.s.root)):
            patch = mock.patch.object(common, key, str(value))
            patch.start()
            self.addCleanup(patch.stop)

    def run_cli(self, *args, table=None, observer=None, allow=()):
        calls = []
        tripwires = [mock.patch.object(mod, name, side_effect=AssertionError(f'{name} must not be called'))
                     for mod, name in FORBIDDEN if name not in allow]
        out, err = io.StringIO(), io.StringIO()
        before = tree(self.s.root)
        with contextlib.ExitStack() as stack:
            for t in tripwires:
                stack.enter_context(t)
            stack.enter_context(mock.patch.object(wa, 'default_observer', observer or table_observer(table or {}, calls)))
            stack.enter_context(contextlib.redirect_stdout(out))
            stack.enter_context(contextlib.redirect_stderr(err))
            code = cli.main(['explain-writer-admission', *args])
        self.assertEqual(tree(self.s.root), before, 'the explanation changed the store')
        return code, out.getvalue(), err.getvalue()

    def test_json_report_blocked_by_a_live_writer(self):
        self.s.reservation({'id': 'owner', 'cwd': self.s.cwd, 'pid': 50, 'created': 500})
        self.s.job('owner', status={'status': 'running', 'pid': 50, 'pid_created': 500, 'prompt': 'SECRET-PROMPT'},
                   result={'final': 'SECRET-FINAL'})
        code, out, _ = self.run_cli('--cwd', str(self.s.repo), '--json', table={(50, 500): ALIVE})
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertEqual(report['decision'], wa.BLOCKED)
        self.assertEqual((report['proposal_only'], report['grants_admission']), (True, False))
        self.assertEqual([j['id'] for j in report['jobs']], ['owner'])
        for secret in ('SECRET-BRIEF-PAYLOAD', 'SECRET-PROMPT', 'SECRET-FINAL'):
            self.assertNotIn(secret, out)

    def test_no_observed_blocker_is_never_called_allowed(self):
        code, out, _ = self.run_cli('--cwd', str(self.s.repo), '--json')
        report = json.loads(out)
        self.assertEqual((code, report['decision']), (0, wa.NO_OBSERVED_BLOCKER))
        self.assertNotIn('allow', out.lower())
        self.assertNotIn('grant', report['decision'])
        self.assertFalse(os.path.exists(self.s.state / 'writers'), 'a reservation folder was created')

    def test_proven_dead_marker_is_reported_cleanup_dependent_and_left_in_place(self):
        self.s.job('a', status={'status': 'done'}, result={'status': 'done'},
                   pending={'engine': 'codex', 'pid': 80, 'created': 800})
        code, out, _ = self.run_cli('--cwd', str(self.s.repo), '--json', table={(80, 800): DEAD})
        report = json.loads(out)
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertEqual(report['jobs'][0]['engine']['pending_launch']['reason_code'], 'pending_launch_cleanup_dependent')
        self.assertTrue((self.s.jobs / 'a' / 'child-starting.json').exists())

    def test_job_id_is_excluded_like_the_claimant(self):
        self.s.job('mine', status={'status': 'running', 'pid': 5, 'pid_created': 7})
        _, out, _ = self.run_cli('--cwd', str(self.s.repo), '--job-id', 'mine', '--json', table={(5, 7): ALIVE})
        self.assertEqual(json.loads(out)['decision'], wa.NO_OBSERVED_BLOCKER)
        _, out, _ = self.run_cli('--cwd', str(self.s.repo), '--json', table={(5, 7): ALIVE})
        self.assertEqual(json.loads(out)['decision'], wa.BLOCKED)

    def test_human_output(self):
        self.s.job('a', version=None, status={'status': 'queued'})
        code, out, _ = self.run_cli('--cwd', str(self.s.repo))
        self.assertEqual(code, 0)
        self.assertIn('proposal only, never a grant): blocked', out)
        self.assertIn('legacy_unqualified_prelaunch', out)
        self.assertNotIn('SECRET-BRIEF-PAYLOAD', out)

    def test_usage_errors(self):
        for args in ([], ['--json'], ['--cwd', str(self.s.repo), '--job-id', '../escape'],
                     ['--cwd', str(self.s.repo), '--job-id', '']):
            with self.subTest(args=args):
                code, out, _ = self.run_cli(*args)
                self.assertEqual(code, common.EXIT_USAGE)
                self.assertEqual(out, '')

    def test_nul_or_empty_cwd_is_usage_without_any_filesystem_call(self):
        boom = AssertionError('a path call was made')
        for cwd in ('repo\x00x', '\x00', ''):
            with self.subTest(cwd=repr(cwd)):
                out, err = io.StringIO(), io.StringIO()
                # argparse reads locale files through os.stat unless the language is plain C
                with mock.patch.dict(os.environ, {'LANGUAGE': 'C'}), \
                        mock.patch.object(os.path, 'realpath', side_effect=boom), \
                        mock.patch.object(os.path, 'abspath', side_effect=boom), \
                        mock.patch.object(os.path, 'normcase', side_effect=boom), \
                        mock.patch.object(os, 'scandir', side_effect=boom), \
                        mock.patch.object(os, 'stat', side_effect=boom), \
                        mock.patch.object(builtins, 'open', side_effect=boom), \
                        mock.patch.object(wa, 'capture_snapshot', side_effect=boom), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(['explain-writer-admission', '--cwd', cwd, '--json'])
                self.assertEqual(code, common.EXIT_USAGE)
                self.assertEqual(out.getvalue(), '')
                self.assertIn('--cwd', err.getvalue())
                self.assertNotIn('Traceback', err.getvalue())

    def test_uncanonicalizable_cwd_is_usage_without_traceback(self):
        for error in (ValueError('synthetic'), OSError('synthetic')):
            with self.subTest(error=type(error).__name__):
                out, err = io.StringIO(), io.StringIO()
                with mock.patch.object(wa, 'writer_cwd', side_effect=error), \
                        mock.patch.object(wa, 'capture_snapshot', side_effect=AssertionError('captured')), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    code = cli.main(['explain-writer-admission', '--cwd', str(self.s.repo)])
                self.assertEqual((code, out.getvalue()), (common.EXIT_USAGE, ''))
                self.assertIn('cannot be canonicalized', err.getvalue())

    def test_real_self_observation_through_the_command(self):
        me = common.process_created(os.getpid())
        self.s.reservation({'id': 'owner', 'cwd': self.s.cwd, 'pid': os.getpid(), 'created': me})
        # macOS reads a start time by running a read-only `ps`; nothing else may spawn a process.
        _, out, _ = self.run_cli('--cwd', str(self.s.repo), '--json', observer=wa.common.observe_process,
                                 allow=('Popen',) if sys.platform == 'darwin' else ())
        report = json.loads(out)
        self.assertEqual(report['decision'], wa.BLOCKED, out)
        self.assertEqual(report['reservation']['owner']['state'], ALIVE)


class ExplainProcess(unittest.TestCase):
    def test_fresh_home_is_never_created(self):
        base = tempfile.mkdtemp(prefix='twinlane-wa-cli-')
        self.addCleanup(shutil.rmtree, base, ignore_errors=True)
        home, repo = os.path.join(base, 'home-never-created'), os.path.join(base, 'repo')
        os.mkdir(repo)
        result = subprocess.run([sys.executable, '-B', '-m', 'twinlane', 'explain-writer-admission', '--cwd', repo,
                                 '--json'], cwd=ROOT, env=dict(os.environ, TWINLANE_HOME=home),
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual((report['decision'], report['snapshot']['jobs_root']), (wa.UNKNOWN, 'absent'))
        self.assertFalse(os.path.exists(home))
        self.assertEqual(sorted(os.listdir(base)), ['repo'])


if __name__ == '__main__':
    unittest.main()
