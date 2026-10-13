"""Command dispatch for land, ship, wt, resume, compare and run --best-of (their modules are stubs
that exit 2 until implemented); existing commands keep their routes."""
import importlib
import io
import os
import subprocess
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
import twinlane  # noqa: E402
from twinlane import cli, common  # noqa: E402

STUBS = {'land': 'land', 'ship': 'ship', 'wt': 'worktrees', 'resume': 'resume', 'compare': 'bestof'}


def still_stub(module):
    """True while twinlane.<module> is the placeholder that exits 2; implemented modules drop out."""
    path = os.path.join(ROOT, 'twinlane', module + '.py')
    with open(path, encoding='utf-8') as handle:
        return 'not implemented yet' in handle.read()


def run_main(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = twinlane.main(list(argv))
    return rc, out.getvalue(), err.getvalue()


class Dispatch(unittest.TestCase):
    def test_each_new_command_reaches_its_module_with_the_remaining_arguments(self):
        for command, module in STUBS.items():
            mod = importlib.import_module('twinlane.' + module)
            with self.subTest(command=command), mock.patch.object(mod, 'cli', return_value=41) as fake:
                self.assertEqual(twinlane.main([command, 'sub', '--flag', 'x']), 41)
                expected = ['compare', 'sub', '--flag', 'x'] if command == 'compare' else ['sub', '--flag', 'x']
                fake.assert_called_once_with(expected)

    def test_run_best_of_goes_to_bestof_and_plain_run_goes_to_jobs(self):
        from twinlane import bestof, jobs
        for flag in (['--best-of', 'claude,codex'], ['--best-of=claude,codex']):
            with self.subTest(flag=flag), mock.patch.object(bestof, 'cli', return_value=42) as fake, \
                    mock.patch.object(jobs, 'cli', return_value=0) as plain:
                self.assertEqual(twinlane.main(['run', '--brief', 'b.md', '--cd', '.', *flag]), 42)
                fake.assert_called_once_with(['run', '--brief', 'b.md', '--cd', '.', *flag])
                plain.assert_not_called()
        with mock.patch.object(bestof, 'cli', return_value=42) as fake, \
                mock.patch.object(jobs, 'cli', return_value=7) as plain:
            self.assertEqual(twinlane.main(['run', '--brief', 'b.md', '--cd', '.', '--engine', 'codex']), 7)
            plain.assert_called_once_with('run', ['--brief', 'b.md', '--cd', '.', '--engine', 'codex'])
            fake.assert_not_called()
            # only the exact option counts: a longer name or a value is not the flag
            self.assertFalse(cli.wants_best_of(['--best-of-x', 'y', '--name', 'best-of']))
            self.assertTrue(cli.wants_best_of(['--cd', '.', '--best-of=claude']))

    def test_stub_modules_return_usage_and_say_not_implemented_yet(self):
        for module in ('land', 'ship', 'worktrees', 'bestof', 'resume', 'host'):
            if not still_stub(module):
                continue
            mod = importlib.import_module('twinlane.' + module)
            err = io.StringIO()
            with self.subTest(module=module), redirect_stderr(err):
                self.assertEqual(mod.cli([]), common.EXIT_USAGE)
                self.assertEqual(mod.cli(['anything', '--x']), 2)
            self.assertIn('not implemented yet', err.getvalue())

    def test_new_commands_through_main_print_the_message_and_exit_2(self):
        for argv in (['land', 'status'], ['ship', 'plan', 'x'], ['wt', 'doctor'], ['resume'], ['compare', 'g1'],
                     ['run', '--best-of', 'claude,codex', '--brief', 'b.md', '--cd', '.']):
            module = 'bestof' if argv[0] in ('run', 'compare') else STUBS[argv[0]]
            if not still_stub(module):
                continue
            with self.subTest(argv=argv):
                rc, out, err = run_main(*argv)
                self.assertEqual(rc, 2)
                self.assertIn('not implemented yet', err)
                self.assertEqual(out, '')

    def test_help_lists_the_new_commands(self):
        rc, out, _ = run_main('--help')
        self.assertEqual(rc, 0)
        for text in ('twinlane land ', 'twinlane ship ', 'twinlane wt ', 'twinlane resume', 'twinlane compare',
                     '--best-of'):
            self.assertIn(text, out)

    def test_existing_commands_and_unknown_commands_keep_their_routes(self):
        from twinlane import baton, board, jobs, meter, route, sessions
        for argv, mod, name in ((['route', 'code'], route, 'cli'), (['meter'], meter, 'cli'),
                                (['sessions'], sessions, 'cli'), (['board'], board, 'cli'),
                                (['jobs'], jobs, 'cli'), (['baton', 'list'], baton, 'cli')):
            with self.subTest(argv=argv), mock.patch.object(mod, name, return_value=33) as fake:
                self.assertEqual(twinlane.main(argv), 33)
                fake.assert_called_once()
        rc, _, err = run_main('frobnicate')
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('unknown command', err)

    def test_a_new_command_runs_from_the_script_and_from_python_dash_m(self):
        env = dict(os.environ)
        for argv in ([sys.executable, os.path.join(ROOT, 'twinlane', 'cli.py'), 'land', 'status'],
                     [sys.executable, '-m', 'twinlane', 'wt', 'doctor']):
            with self.subTest(argv=argv[1:3]):
                proc = subprocess.run(argv, cwd=ROOT, env=env, capture_output=True, text=True,
                                      encoding='utf-8', errors='replace', timeout=60)
                self.assertNotIn('Traceback', proc.stderr)
                if still_stub(STUBS[argv[-2]]):
                    self.assertEqual(proc.returncode, 2, proc.stderr)
                    self.assertIn('not implemented yet', proc.stderr)
                else:
                    self.assertNotIn('unknown command', proc.stderr)


if __name__ == '__main__':
    unittest.main()
