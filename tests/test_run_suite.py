"""Exercise the suite runner against disposable modules, never the real suite."""

import test_support  # noqa: F401 -- isolate this module and inherited child environments

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import textwrap
import time
import unittest
from unittest import mock


RUNNER = Path(__file__).with_name("run_suite.py")
_SPEC = importlib.util.spec_from_file_location("suite_runner", RUNNER)
suite_runner = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(suite_runner)


class SuiteRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="suite-fixtures-")
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.root = Path(self.temp.name)
        self.tests = self.root / "tests"
        self.tests.mkdir()
        self.marker = self.tests / "marker"

    def module(self, name, source):
        (self.tests / (name + ".py")).write_text(textwrap.dedent(source), encoding="utf-8")

    def run_suite(self, *args):
        report_path = self.root / "report.json"
        result = subprocess.run(
            [sys.executable, "-B", str(RUNNER), "-j", "2", "--tests-dir", str(self.tests),
             "--log-dir", str(self.root / "logs"), "--json", str(report_path), *args],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8", timeout=20,
        )
        report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else None
        return result, report

    def test_modules_and_their_children_do_not_inherit_the_runner_stdin(self):
        self.module("test_stdin", """
            import subprocess
            import sys
            import unittest
            class Case(unittest.TestCase):
                def test_module_stdin_is_empty(self):
                    self.assertEqual(sys.stdin.read(), '')
                def test_child_reading_stdin_gets_eof(self):
                    done = subprocess.run([sys.executable, '-c', 'import sys; sys.stdin.read()'], timeout=15)
                    self.assertEqual(done.returncode, 0)
        """)
        report_path = self.root / "report.json"
        # The pipe stays open and silent for the whole run, like the stdin of a detached background shell.
        runner = subprocess.Popen(
            [sys.executable, "-B", str(RUNNER), "-j", "1", "--tests-dir", str(self.tests),
             "--log-dir", str(self.root / "logs"), "--json", str(report_path), "--module-timeout", "40"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding="utf-8")
        try:
            output = runner.stdout.read()
            runner.wait(timeout=90)
        finally:
            runner.stdin.close()
            runner.stdout.close()
        self.assertEqual(runner.returncode, 0, output)
        report = json.loads(report_path.read_text(encoding="utf-8"))
        self.assertEqual(report["real_failures"], [])
        self.assertEqual(report["timeouts"], [])

    def flaky_module(self):
        self.module("test_flaky", """
            from pathlib import Path
            import unittest
            class Case(unittest.TestCase):
                def test_once(self):
                    marker = Path(__file__).with_name('marker')
                    existed = marker.exists()
                    marker.unlink(missing_ok=True)
                    self.assertFalse(existed)
        """)
        self.marker.touch()

    def test_fail_error_import_skip_and_rerun(self):
        self.module("test_mixed", """
            import unittest
            class Case(unittest.TestCase):
                def test_pass(self):
                    print('output between name and verdict')
                def test_fail(self):
                    self.fail('synthetic failure')
                def test_error(self):
                    raise ValueError('synthetic error')
                @unittest.skip('synthetic skip')
                def test_skip(self):
                    pass
        """)
        self.module("test_import", "raise RuntimeError('synthetic import error')\n")
        self.flaky_module()
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["exit_code"], 1)
        self.assertEqual(report["summary"], {
            "modules": 3, "tests_ran": 6, "ok": 1, "skipped": 1,
            "expected_failures": 0, "unexpected_successes": 0,
            "real_failures": 3, "load_sensitive": 1, "timeouts": 0,
        })
        self.assertEqual({row["test"] for row in report["real_failures"]}, {
            "test_import", "test_mixed.Case.test_fail", "test_mixed.Case.test_error"})
        self.assertEqual(report["load_sensitive"][0]["classification"], "load-sensitive")
        imported = next(module for module in report["modules"] if module["module"] == "test_import")
        self.assertTrue(imported["tests"][0]["import_error"])
        self.assertNotIn("rerun", imported["tests"][0])
        self.assertEqual(len(report["slowest_modules"]), 3)
        self.assertGreater(report["wall_seconds"], 0)
        self.assertEqual(Path(report["log_dir"]), self.root / "logs")
        for module in report["modules"]:
            self.assertTrue(Path(module["log"]).is_file())
            if module["module"] != "test_import":
                self.assertIn(module["unittest_result"], ("OK", "FAILED"))
        self.assertIn("Real failures: 3", result.stdout)
        self.assertIn("Load-sensitive: 1", result.stdout)

    def test_load_sensitive_exit_modes(self):
        self.flaky_module()
        for flags, code, real, sensitive in (((), 0, 0, 1), (("--strict",), 1, 0, 1),
                                             (("--no-rerun",), 1, 1, 0)):
            with self.subTest(flags=flags):
                self.marker.touch()
                result, report = self.run_suite(*flags)
                self.assertEqual(result.returncode, code, result.stdout)
                self.assertEqual(report["summary"]["real_failures"], real)
                self.assertEqual(report["summary"]["load_sensitive"], sensitive)
                if flags == ("--no-rerun",):
                    self.assertNotIn("rerun", report["modules"][0]["tests"][0])

    def test_filters_pass_and_expected_failure(self):
        self.module("test_good", """
            import os
            from pathlib import Path
            import unittest
            class Case(unittest.TestCase):
                def test_context(self):
                    expected = Path(__file__).resolve().parent
                    self.assertEqual(Path.cwd(), expected.parent)
                    self.assertEqual(os.environ['PYTHONPATH'].split(os.pathsep),
                                     [str(expected.parent), str(expected)])
                    self.assertEqual(os.environ['PYTHONFAULTHANDLER'], '1')
        """)
        self.module("test_expected", """
            import unittest
            class Case(unittest.TestCase):
                @unittest.expectedFailure
                def test_expected(self):
                    self.fail('expected')
        """)
        self.module("test_excluded", "raise RuntimeError('must not import')\n")
        self.module("test_empty", "HELPER = True\n")
        result, report = self.run_suite("-k", "good", "-k", "expected", "-k", "empty")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report["summary"]["modules"], 3)
        self.assertEqual(report["summary"]["tests_ran"], 2)
        self.assertEqual(report["summary"]["ok"], 1)
        self.assertEqual(report["summary"]["expected_failures"], 1)
        empty = next(module for module in report["modules"] if module["module"] == "test_empty")
        self.assertEqual(empty["status"], "ok")
        self.assertEqual(empty["tests_ran"], 0)

    def test_timeout_preserves_completed_tests_and_kills_descendants(self):
        self.module("test_hang", """
            from pathlib import Path
            import subprocess
            import sys
            import time
            import unittest
            class Case(unittest.TestCase):
                def test_a_pass(self):
                    pass
                def test_z_hang(self):
                    pid_file = Path(__file__).with_name('child.pid')
                    child = subprocess.Popen([sys.executable, '-B', '-c',
                        'import os, pathlib, sys, time; '
                        'pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(60)',
                        str(pid_file)])
                    time.sleep(60)
        """)
        result, report = self.run_suite("--module-timeout", "2.5")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["modules"][0]["status"], "timeout")
        self.assertEqual(report["summary"]["timeouts"], 1)
        self.assertEqual(report["summary"]["tests_ran"], 1)
        self.assertEqual(report["summary"]["ok"], 1)
        pid_path = self.tests / "child.pid"
        self.assertTrue(pid_path.exists(), result.stdout)
        pid = int(pid_path.read_text())
        deadline = time.monotonic() + 3
        while self.pid_running(pid) and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertFalse(self.pid_running(pid), "timed-out module left its child running")

    @staticmethod
    def pid_running(pid):
        if os.name == "nt":
            import ctypes
            from ctypes import wintypes
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
            kernel.OpenProcess.restype = wintypes.HANDLE
            kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
            kernel.WaitForSingleObject.restype = wintypes.DWORD
            kernel.CloseHandle.argtypes = [wintypes.HANDLE]
            handle = kernel.OpenProcess(0x00100000, False, pid)
            if not handle:
                return ctypes.get_last_error() != 87  # ERROR_INVALID_PARAMETER: no such PID
            try:
                return kernel.WaitForSingleObject(handle, 0) != 0
            finally:
                kernel.CloseHandle(handle)
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        # An orphaned zombie on Linux is already dead, pending the host's reaper.
        stat = Path("/proc") / str(pid) / "stat"
        try:
            return stat.read_text().rsplit(")", 1)[1].split()[0] != "Z"
        except FileNotFoundError:
            return stat.parent.exists() if Path("/proc").is_dir() else True

    def test_rerun_timeout_is_a_real_failure_and_timeout(self):
        self.module("test_retry_hang", """
            from pathlib import Path
            import time
            import unittest
            class Case(unittest.TestCase):
                def test_retry(self):
                    marker = Path(__file__).with_name('retry')
                    if marker.exists():
                        time.sleep(60)
                    marker.touch()
                    self.fail('first attempt')
        """)
        result, report = self.run_suite("--test-timeout", "0.5")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["summary"]["real_failures"], 1)
        self.assertEqual(report["summary"]["timeouts"], 1)
        self.assertEqual(report["timeouts"][0]["test"], "test_retry_hang.Case.test_retry")

    def test_abrupt_import_crash_and_unexpected_success_gate(self):
        self.module("test_crash", "raise SystemExit(0)\n")
        self.module("test_unexpected", """
            import unittest
            class Case(unittest.TestCase):
                @unittest.expectedFailure
                def test_unexpected(self):
                    pass
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["summary"]["real_failures"], 2)
        self.assertEqual(report["summary"]["unexpected_successes"], 1)

    def test_diagnostics_cannot_redirect_failure(self):
        self.module("test_demo", r"""
            import unittest
            class Case(unittest.TestCase):
                def test_bad(self):
                    print('\ntest_good (test_demo.Case.test_good) ... ', end='')
                    self.fail('always fails, including alone')
                def test_good(self):
                    pass
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["summary"]["load_sensitive"], 0)
        self.assertEqual([row["test"] for row in report["real_failures"]],
                         ["test_demo.Case.test_bad"])
        self.assertEqual(report["summary"]["ok"], 1)

    def test_serial_module_failure_stays_real(self):
        self.module("test_order", """
            import unittest
            changed = False
            class Case(unittest.TestCase):
                def test_a(self):
                    global changed
                    changed = True
                def test_b(self):
                    self.assertFalse(changed, 'deterministic module-order failure')
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["summary"]["real_failures"], 1)
        self.assertEqual(report["summary"]["load_sensitive"], 0)
        row = report["real_failures"][0]
        self.assertEqual(row["rerun"]["returncode"], 0)
        self.assertEqual(report["modules"][0]["serial_rerun"]["returncode"], 1)

    def test_abnormal_exit_after_a_failure_survives_passing_reruns(self):
        # The test fails once, the driver reports it, then an atexit hook exits 9. The isolated
        # and serial reruns both pass (the markers are gone), but the abnormal exit must still gate.
        self.module("test_exits", """
            import atexit, os
            from pathlib import Path
            import unittest
            here = Path(__file__).parent
            if (here / 'exit_marker').exists():
                (here / 'exit_marker').unlink()
                atexit.register(os._exit, 9)
            class Case(unittest.TestCase):
                def test_once(self):
                    marker = here / 'fail_marker'
                    existed = marker.exists()
                    marker.unlink(missing_ok=True)
                    self.assertFalse(existed)
        """)
        (self.tests / "fail_marker").touch()
        (self.tests / "exit_marker").touch()
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertGreaterEqual(report["summary"]["real_failures"], 1, report["summary"])
        module = report["modules"][0]
        self.assertEqual(module["returncode"], 9)
        self.assertTrue(any(row["classification"] == "real" and row["test"] == "test_exits"
                            for row in module["tests"]), module["tests"])

    def test_crash_after_loading_leaves_unresolved_tests(self):
        self.module("test_crash", """
            import os
            import unittest
            class Case(unittest.TestCase):
                def test_a(self):
                    print('ok\\nRan 2 tests in 0.001s\\n\\nOK', flush=True)
                    os._exit(0)
                def test_b(self):
                    pass
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        unresolved = [row for row in report["real_failures"] if row["status"] == "unresolved"]
        self.assertEqual({row["test"] for row in unresolved},
                         {"test_crash.Case.test_a", "test_crash.Case.test_b"})
        self.assertIn("UNRESOLVED", result.stdout)

    def test_stop_without_a_verdict_stays_unresolved(self):
        self.module("test_missing_verdict", """
            import unittest
            class Case(unittest.TestCase):
                def run(self, result):
                    result.startTest(self)
                    result.stopTest(self)
                def test_no_verdict(self):
                    pass
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["real_failures"][0]["status"], "unresolved")
        self.assertEqual(report["summary"]["tests_ran"], 1)

    def test_overall_zero_tests_is_usage_error_in_all_modes(self):
        self.module("test_helper", "HELPER = True\n")
        for flags in ((), ("--strict",), ("--no-rerun",)):
            with self.subTest(flags=flags):
                result, report = self.run_suite(*flags)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("zero-test", result.stdout)
                self.assertIsNone(report)

    def test_structured_docstrings_and_subtests(self):
        self.module("test_subtests", """
            import unittest
            class Case(unittest.TestCase):
                def test_doc(self):
                    'A description ... FAIL'
                    print('unexpected success')
                def test_sub(self):
                    with self.subTest(value=1):
                        self.fail('synthetic subtest failure')
                    with self.subTest(value=2):
                        raise ValueError('synthetic subtest error')
                @unittest.skip('reason')
                def test_skip(self):
                    pass
        """)
        result, report = self.run_suite("--no-rerun")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual([row["status"] for row in report["modules"][0]["tests"]],
                         ["ok", "skipped", "error"])
        self.assertEqual(report["summary"]["tests_ran"], 3)

    def test_skipped_class_fixture_has_explicit_outcomes(self):
        self.module("test_fixture_skip", """
            import unittest
            class Case(unittest.TestCase):
                @classmethod
                def setUpClass(cls):
                    raise unittest.SkipTest('fixture unavailable')
                def test_one(self):
                    pass
                def test_two(self):
                    pass
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report["summary"]["skipped"], 2)
        self.assertEqual(report["summary"]["real_failures"], 0)

    def test_tests_dir_wins_over_driver_directory(self):
        self.module("test_run_suite", """
            import unittest
            class SyntheticCase(unittest.TestCase):
                def test_selected(self):
                    pass
        """)
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(report["summary"]["tests_ran"], 1)
        self.assertEqual(report["modules"][0]["tests"][0]["test"],
                         "test_run_suite.SyntheticCase.test_selected")

    def test_skipped_subtest_on_rerun_is_not_a_pass(self):
        self.module("test_subtest_skip", """
            from pathlib import Path
            import unittest
            class Case(unittest.TestCase):
                def test_once(self):
                    marker = Path(__file__).with_name('marker')
                    if marker.exists():
                        marker.unlink()
                        self.fail('original failure')
                    with self.subTest(value=1):
                        self.skipTest('unavailable on rerun')
        """)
        self.marker.touch()
        result, report = self.run_suite()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(report["summary"]["real_failures"], 1)
        self.assertEqual(report["summary"]["load_sensitive"], 0)
        self.assertEqual(report["real_failures"][0]["rerun"]["tests"][0]["status"], "skipped")

    def test_posix_snapshot_failure_still_kills_group_and_warns(self):
        with mock.patch.object(suite_runner.os, "name", "posix"), \
                mock.patch.object(suite_runner, "_process_table", create=True,
                                  side_effect=PermissionError("snapshot denied")), \
                mock.patch.object(suite_runner.os, "killpg", create=True) as kill_group, \
                mock.patch.object(suite_runner.signal, "SIGKILL", 9, create=True):
            with self.assertRaisesRegex(RuntimeError, "termination unverified"):
                suite_runner.kill_tree(mock.Mock(pid=450001))
        kill_group.assert_called_once_with(450001, 9)

    def test_posix_cleanup_reaches_detached_descendant(self):
        # Fake OS calls only: these PIDs are never sent to the real kernel.
        root, child = 450001, 450002
        table = {root: (1, "root-birth", "S"), child: (root, "child-birth", "S")}
        killed = []

        def send(pid, sig):
            if sig == 9:
                killed.append(pid)
                table.pop(pid, None)
            elif sig == 19:
                parent, birth, _ = table[pid]
                table[pid] = (parent, birth, "T")

        def group(pid, sig):
            # The child's detached session is outside this process group.
            send(root, sig)

        with mock.patch.object(suite_runner.os, "name", "posix"), \
                mock.patch.object(suite_runner, "_process_table", lambda: dict(table), create=True), \
                mock.patch.object(suite_runner.os, "kill", side_effect=send), \
                mock.patch.object(suite_runner.os, "killpg", side_effect=group, create=True), \
                mock.patch.object(suite_runner.signal, "SIGSTOP", 19, create=True), \
                mock.patch.object(suite_runner.signal, "SIGKILL", 9, create=True):
            suite_runner.kill_tree(mock.Mock(pid=root))
        self.assertIn(child, killed, "detached child was outside killpg and survived")

    def test_posix_reused_pid_does_not_admit_unrelated_descendants(self):
        root, child, stranger = 450001, 450002, 450003
        initial = {root: (1, "root", "T"), child: (root, "child", "T")}
        reused = {root: (1, "root", "T"), child: (1, "replacement", "T"),
                  stranger: (child, "stranger", "T")}
        snapshots = iter((initial, reused, reused, reused))
        with mock.patch.object(suite_runner.os, "name", "posix"), \
                mock.patch.object(suite_runner, "_process_table", create=True,
                                  side_effect=lambda: next(snapshots)), \
                mock.patch.object(suite_runner.os, "kill") as send, \
                mock.patch.object(suite_runner.os, "killpg", create=True), \
                mock.patch.object(suite_runner.signal, "SIGSTOP", 19, create=True), \
                mock.patch.object(suite_runner.signal, "SIGKILL", 9, create=True):
            # Remove the owned root after its kill, so verification sees no survivors.
            def kill_owned(pid, sig):
                if sig == 9:
                    reused.pop(pid, None)
            send.side_effect = kill_owned
            suite_runner.kill_tree(mock.Mock(pid=root))
        killed = [call.args[0] for call in send.call_args_list if call.args[1] == 9]
        self.assertEqual(killed, [root])

    def test_posix_surviving_descendants_are_reported(self):
        table = {450001: (1, "root", "T"), 450002: (450001, "child", "T")}
        with mock.patch.object(suite_runner.os, "name", "posix"), \
                mock.patch.object(suite_runner, "_process_table", create=True, return_value=table), \
                mock.patch.object(suite_runner.os, "kill"), \
                mock.patch.object(suite_runner.os, "killpg", create=True), \
                mock.patch.object(suite_runner.signal, "SIGSTOP", 19, create=True), \
                mock.patch.object(suite_runner.signal, "SIGKILL", 9, create=True), \
                mock.patch.object(suite_runner.time, "monotonic", side_effect=(0, 4)):
            with self.assertRaisesRegex(RuntimeError, "descendants survived"):
                suite_runner.kill_tree(mock.Mock(pid=450001))

    @unittest.skipIf(os.name == "nt", "requires native POSIX sessions")
    def test_posix_timeout_kills_native_detached_child(self):
        self.module("test_detached", """
            from pathlib import Path
            import subprocess
            import sys
            import time
            import unittest
            class Case(unittest.TestCase):
                def test_hang(self):
                    child = subprocess.Popen([sys.executable, '-B', '-c',
                        'import time; time.sleep(60)'], start_new_session=True)
                    Path(__file__).with_name('child.pid').write_text(str(child.pid))
                    time.sleep(60)
        """)
        self.addCleanup(self.cleanup_fixture_child)
        result, report = self.run_suite("--module-timeout", "1.5")
        self.assertEqual(result.returncode, 1, result.stdout)
        pid = int((self.tests / "child.pid").read_text())
        self.assertFalse(self.pid_running(pid), "detached child survived timeout")
        self.assertIsNone(report["modules"][0]["termination_error"])

    def cleanup_fixture_child(self):
        path = self.tests / "child.pid"
        if path.exists():
            pid = int(path.read_text())
            if self.pid_running(pid):
                if os.name == "nt":
                    import ctypes
                    from ctypes import wintypes
                    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
                    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
                    kernel.OpenProcess.restype = wintypes.HANDLE
                    kernel.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
                    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
                    handle = kernel.OpenProcess(1, False, pid)
                    if handle:
                        try:
                            kernel.TerminateProcess(handle, 1)
                        finally:
                            kernel.CloseHandle(handle)
                else:
                    import signal
                    os.kill(pid, signal.SIGKILL)
            deadline = time.monotonic() + 3
            while self.pid_running(pid) and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertFalse(self.pid_running(pid), "fixture cleanup left its own child running")

    @unittest.skipUnless(os.name == "nt", "requires native Windows Job Objects")
    def test_windows_job_contains_child_before_execution(self):
        self.module("test_early_child", """
            from pathlib import Path
            import subprocess
            import sys
            import time
            Path(__file__).with_name('started').touch()
            child = subprocess.Popen([sys.executable, '-B', '-c',
                'import time; time.sleep(60)'])
            Path(__file__).with_name('child.pid').write_text(str(child.pid))
            time.sleep(60)
        """)
        self.addCleanup(self.cleanup_fixture_child)
        real_job = suite_runner._WindowsJob
        executed_before_assignment = []

        def assign(process):
            # Deliberately widen the old start/assign race to a reproducible window.
            deadline = time.monotonic() + 0.8
            while not (self.tests / "child.pid").exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            executed_before_assignment.append((self.tests / "started").exists())
            return real_job(process)

        with mock.patch.object(suite_runner, "_WindowsJob", side_effect=assign), \
                mock.patch.object(suite_runner.subprocess, "run", side_effect=PermissionError("denied")):
            result = suite_runner.run_command("test_early_child", self.tests,
                                              self.root / "early.log", 1.5)
        self.assertEqual(executed_before_assignment, [False], "test ran before job assignment")
        self.assertTrue(result["timeout"])
        self.assertIsNone(result["termination_error"])
        self.assertFalse(self.pid_running(int((self.tests / "child.pid").read_text())),
                         "descendant survived with taskkill denied")

    @unittest.skipUnless(os.name == "nt", "requires native Windows Job Objects")
    def test_windows_reports_unverified_cleanup(self):
        self.module("test_hang", "import time; time.sleep(60)\n")
        with mock.patch.object(suite_runner._WindowsJob, "verify_empty", create=True,
                               side_effect=RuntimeError("descendant verification failed")), \
                mock.patch.object(suite_runner.subprocess, "run", side_effect=PermissionError("denied")):
            result = suite_runner.run_module("test_hang", self.tests, self.root, 0.3)
        self.assertTrue(result["timeout"])
        self.assertIn("descendant verification failed", result["termination_error"] or "")
        report = suite_runner.make_report([result], 1, self.root, False)
        import io
        from contextlib import redirect_stdout
        output = io.StringIO()
        with redirect_stdout(output):
            suite_runner.print_report(report)
        self.assertIn("WARNING: timeout cleanup incomplete", output.getvalue())
        self.assertEqual(report["exit_code"], 1)

    def test_usage_errors(self):
        for flags in (("-j", "0"), ("--module-timeout", "nan"), ("--test-timeout", "-1"),
                      ("-k", "absent")):
            with self.subTest(flags=flags):
                result, report = self.run_suite(*flags)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIsNone(report)


if __name__ == "__main__":
    unittest.main()
