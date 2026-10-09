"""common.py helpers that other modules lean on. No network, no model calls."""
import contextlib
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common  # noqa: E402


class PidAliveNeverKills(unittest.TestCase):
    def test_live_child_survives_the_probe(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            with mock.patch.object(os, 'kill', side_effect=AssertionError('os.kill must not be called')) \
                    if common.IS_WIN else mock.patch.object(os, 'getpid', os.getpid):
                for _ in range(3):
                    self.assertTrue(common.pid_alive(child.pid))
            self.assertIsNone(child.poll(), 'the probe killed the process')
        finally:
            child.kill()
            child.wait(timeout=10)
        self.assertFalse(common.pid_alive(child.pid))

    def test_bad_pids(self):
        for pid in (None, '', 'x', 0, -5):
            self.assertFalse(common.pid_alive(pid))


@unittest.skipUnless(shutil.which('git'), 'git not installed')
class GitKeepsPorcelainLeadingSpace(unittest.TestCase):
    def setUp(self):
        self.repo = tempfile.mkdtemp(prefix='twinlane-git-')
        self.addCleanup(shutil.rmtree, self.repo, ignore_errors=True)
        for args in (['init', '-q'], ['config', 'user.email', 'user@example.com'], ['config', 'user.name', 't']):
            subprocess.run(['git', '-C', self.repo, *args], check=True, capture_output=True)
        with open(os.path.join(self.repo, 'a.txt'), 'w', encoding='utf-8') as h:
            h.write('one\n')
        subprocess.run(['git', '-C', self.repo, 'add', 'a.txt'], check=True, capture_output=True)
        subprocess.run(['git', '-C', self.repo, 'commit', '-q', '-m', 'init'], check=True, capture_output=True)
        with open(os.path.join(self.repo, 'a.txt'), 'a', encoding='utf-8') as h:
            h.write('two\n')

    def test_first_entry_keeps_its_status_column(self):
        rc, out = common.git(self.repo, 'status', '--porcelain=v1')
        self.assertEqual(rc, 0)
        self.assertEqual(out.splitlines()[0], ' M a.txt')
        self.assertEqual(out.splitlines()[0][3:], 'a.txt')

    def test_git_state_dirty_is_parseable(self):
        st = common.git_state(self.repo)
        self.assertEqual(st['dirty'], [' M a.txt'])
        self.assertFalse(st['linked_worktree'])


def _harness(code, *args, **kw):
    """Run a python snippet with the twinlane package importable; returns CompletedProcess."""
    prog = 'import sys, json, time, subprocess; sys.path.insert(0, sys.argv[1]); from twinlane import common\n' + code
    return subprocess.run([sys.executable, '-c', prog, ROOT, *args], capture_output=True, text=True,
                          timeout=kw.pop('timeout', 60), **kw)


class ProcessIdentity(unittest.TestCase):
    """a recycled pid must never read as our runner, and must never be killed."""

    def test_created_stamp_is_checked(self):
        me = common.process_created(os.getpid())
        self.assertIsNotNone(me)
        self.assertTrue(common.pid_alive(os.getpid(), me))
        self.assertFalse(common.pid_alive(os.getpid(), me + 1))

    def test_kill_tree_refuses_a_reused_pid(self):
        if not common.IS_WIN and common.process_created(os.getpid()) is None:
            self.skipTest('platform provides no process start time')
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            created = common.process_created(child.pid)
            self.assertFalse(common.kill_tree(child.pid, created + 1))
            time.sleep(0.3)
            self.assertIsNone(child.poll(), 'kill_tree killed a process whose identity did not match')
            self.assertTrue(common.kill_tree(child.pid, created))
            child.wait(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    @unittest.skipIf(common.IS_WIN, 'POSIX process groups only')
    def test_kill_tree_plain_child_does_not_signal_own_group(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            self.assertEqual(os.getpgid(child.pid), os.getpgrp())
            self.assertTrue(common.kill_tree(child.pid))
            child.wait(timeout=10)
            self.assertIsNotNone(child.returncode)
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_parse_time_accepts_long_fraction_z(self):
        self.assertEqual(common.parse_time('2026-01-02T03:04:05.1234567Z').microsecond, 123456)

    def test_macos_start_time_uses_c_locale_ps_fallback(self):
        response = mock.Mock(returncode=0, stdout='Fri Oct  9 15:00:00 2026\n')
        with mock.patch.object(common.sys, 'platform', 'darwin'), \
                mock.patch('builtins.open', side_effect=FileNotFoundError), \
                mock.patch.object(common.subprocess, 'run', return_value=response) as ps:
            self.assertIsInstance(common._posix_start_time(123), int)
        self.assertEqual(ps.call_args.args[0], ['ps', '-o', 'lstart=', '-p', '123'])
        self.assertEqual(ps.call_args.kwargs['env']['LC_ALL'], 'C')

    def test_config_dir_overrides_and_install_preview(self):
        with tempfile.TemporaryDirectory() as tmp:
            claude = os.path.join(tmp, 'claude-settings')
            codex = os.path.join(tmp, 'codex-settings')
            env = dict(os.environ, CLAUDE_CONFIG_DIR=claude, CODEX_HOME=codex)
            code = 'from twinlane import common; print(common.CLAUDE_DIR); print(common.CODEX_DIR)'
            result = subprocess.run([sys.executable, '-c', code], cwd=ROOT, env=env,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.splitlines(), [claude, codex])
            preview = subprocess.run([sys.executable, '-m', 'twinlane', 'install'], cwd=ROOT, env=env,
                                     capture_output=True, text=True)
            self.assertEqual(preview.returncode, 0, preview.stderr)
            self.assertIn(os.path.join(claude, 'settings.json'), preview.stdout)
            self.assertFalse(os.path.exists(claude))


@unittest.skipUnless(common.IS_WIN, 'Windows job objects')
class JobObjects(unittest.TestCase):
    def test_detach_refuses_inside_a_kill_on_close_job(self):
        # inside a kill-on-close job that forbids breakaway, --bg must refuse, not
        # start a runner that dies with the shell.
        r = _harness(
            'assert common.contain_children()\n'
            'print(json.dumps(common.job_info()))\n'
            'try:' '\n'
            '    common.spawn_detached_ex([sys.executable, "-c", "pass"], require_survival=True)\n'
            '    print("started")\n'
            'except common.DetachError as e:' '\n'
            '    print("refused")\n'
            'print(common.spawn_detached_ex([sys.executable, "-c", "pass"])["mode"])\n')
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        lines = r.stdout.split()
        info = json.loads(r.stdout.splitlines()[0])
        self.assertTrue(info['kill_on_close'])
        self.assertFalse(info['breakaway_ok'])
        self.assertIn('refused', lines)
        self.assertEqual(lines[-1], 'in-job')

    def test_contained_children_die_with_the_runner(self):
        # a runner killed without /T must not leave its engine child running.
        runner = subprocess.Popen(
            [sys.executable, '-c',
             'import sys, subprocess, time; sys.path.insert(0, sys.argv[1]); from twinlane import common\n'
             'assert common.contain_children()\n'
             'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])\n'
             'print(c.pid, common.process_created(c.pid), flush=True); time.sleep(60)', ROOT],
            stdout=subprocess.PIPE, text=True)
        try:
            pid, created = (int(x) for x in runner.stdout.readline().split())
            self.assertTrue(common.pid_alive(pid, created))
            runner.kill()  # TerminateProcess on the runner only
            runner.wait(timeout=10)
            deadline = time.time() + 10
            while common.pid_alive(pid, created) and time.time() < deadline:
                time.sleep(0.1)
            self.assertFalse(common.pid_alive(pid, created), 'the engine child outlived its runner')
        finally:
            if runner.poll() is None:
                runner.kill()
            runner.stdout.close()

    def test_a_consoleless_process_starts_children_without_a_window(self):
        # a detached runner has no console; its console children must not open one.
        r = subprocess.run([sys.executable, '-c',
                            'import sys; sys.path.insert(0, sys.argv[1]); from twinlane import common; '
                            'print(common.no_window_flags())', ROOT],
                           capture_output=True, text=True, timeout=60, creationflags=common.DETACHED_PROCESS)
        self.assertEqual(int(r.stdout.strip()), common.CREATE_NO_WINDOW)


class ReadDuringReplace(unittest.TestCase):
    def test_reader_never_sees_missing_while_a_writer_replaces(self):
        # os.replace over an open-for-read target is a transient PermissionError on
        # Windows; a reader must not mistake it for a missing file.
        import threading
        d = tempfile.mkdtemp(prefix='twinlane-rw-')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        path = os.path.join(d, 'status.json')
        common.write_json(path, {'n': 0})
        stop = threading.Event()
        errors = []

        def writer():
            n = 0
            while not stop.is_set():
                n += 1
                try:
                    common.write_json(path, {'n': n, 'pad': 'x' * 2000})
                except OSError as e:
                    errors.append(e)

        t = threading.Thread(target=writer)
        t.start()
        try:
            misses = sum(1 for _ in range(400) if common.read_json(path) is None)
        finally:
            stop.set()
            t.join()
        self.assertEqual(misses, 0)
        self.assertEqual(errors, [])


class FileLock(unittest.TestCase):
    def test_lock_is_exclusive(self):
        d = tempfile.mkdtemp(prefix='twinlane-lock-')
        self.addCleanup(shutil.rmtree, d, ignore_errors=True)
        lock = os.path.join(d, 'x.lock')
        with common.file_lock(lock):
            r = _harness('try:' '\n    with common.file_lock(sys.argv[2], timeout_s=0.3):\n        print("got")\n'
                         'except TimeoutError:' '\n    print("busy")', lock)
            self.assertEqual(r.stdout.strip(), 'busy', r.stderr[-1000:])
        r = _harness('with common.file_lock(sys.argv[2], timeout_s=2):\n    print("got")', lock)
        self.assertEqual(r.stdout.strip(), 'got', r.stderr[-1000:])


class FmtAge(unittest.TestCase):
    def test_units(self):
        self.assertEqual(common.fmt_age(None), '?')
        self.assertEqual(common.fmt_age(45), '45s')
        self.assertEqual(common.fmt_age(720), '12m')
        self.assertEqual(common.fmt_age(3 * 3600 + 5), '3h')
        self.assertEqual(common.fmt_age(2 * 86400), '2d')


PING = os.path.join(os.environ.get('SystemRoot', 'C' + r':\Windows'), 'System32', 'PING.EXE')


class CodexBinaryPortable(unittest.TestCase):
    def test_version_order(self):
        key = common.codex_version_key
        self.assertLess(key('0.162.0-alpha.2'), key('0.162.0'))
        self.assertLess(key('0.162.0-alpha.2'), key('0.162.0-alpha.10'))
        self.assertLess(key('0.162.0-alpha.10'), key('0.162.0-beta.1'))
        self.assertLess(key('0.9.0'), key('0.10.0'))
        self.assertLess(key('0.161.9'), key('codex-cli 0.162.0-alpha.1'))
        self.assertIsNone(key('codex dev build'))

    @unittest.skipIf(common.IS_WIN, 'POSIX binary selection only')
    def test_posix_uses_only_an_explicit_binary_without_a_copy(self):
        with tempfile.TemporaryDirectory(prefix='twinlane-codex-') as tmp:
            local = os.path.join(tmp, 'local')
            source = os.path.join(local, 'OpenAI', 'Codex', 'bin', 'build', 'codex.exe')
            os.makedirs(os.path.dirname(source))
            with open(source, 'wb') as h:
                h.write(b'app fixture')
            for name in common.CODEX_HELPERS:
                with open(os.path.join(os.path.dirname(source), name), 'wb') as h:
                    h.write(b'helper fixture')
            override = os.path.join(tmp, 'codex')
            with open(override, 'wb') as h:
                h.write(b'override fixture')
            bin_dir = os.path.join(tmp, 'bin')
            with mock.patch.dict(os.environ, {'TWINLANE_CODEX_BIN': '', 'LOCALAPPDATA': local}, clear=False), \
                    mock.patch.object(common, 'BIN', bin_dir):
                self.assertEqual(common.acquire_codex()[::2], (None, None))
                self.assertIsNone(common.codex_bin())
                self.assertFalse(os.path.exists(bin_dir))
                os.environ['TWINLANE_CODEX_BIN'] = override
                self.assertEqual(common.acquire_codex()[::2], (override, None))
                self.assertEqual(common.codex_bin(), override)
                self.assertFalse(os.path.exists(bin_dir))


@unittest.skipUnless(common.IS_WIN, 'the desktop-app copy is Windows-only')
class CodexBinary(unittest.TestCase):
    """The desktop-app CLI copy is Windows-only and fails closed rather than run an unverified binary."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.local = os.path.join(self.tmp, 'local')
        self.codex_dir = os.path.join(self.tmp, 'dotcodex')
        self.bin = os.path.join(self.tmp, 'bin')
        env = {k: v for k, v in os.environ.items() if k != 'TWINLANE_CODEX_BIN'}
        env.update(LOCALAPPDATA=self.local, TWINLANE_TEST_ISOLATION='1')
        for patch in (mock.patch.dict(os.environ, env, clear=True),
                      mock.patch.object(common, 'CODEX_DIR', self.codex_dir),
                      mock.patch.object(common, 'BIN', self.bin),
                      mock.patch.object(common.shutil, 'which', return_value=os.path.join(self.tmp, 'path-codex')),
                      # fake exes cannot run: the version is unreadable unless a test says otherwise
                      mock.patch.object(common, '_read_codex_version', return_value=None, create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def _exe(self, folder, body=b'codex', mtime=None, helpers=True):
        os.makedirs(folder, exist_ok=True)
        path = os.path.join(folder, 'codex.exe')
        with open(path, 'wb') as h:
            h.write(body)
        if helpers:
            for name in common.CODEX_HELPERS:
                with open(os.path.join(folder, name), 'wb') as h:
                    h.write(b'helper')
        if mtime:
            os.utime(path, (mtime, mtime))
        return path

    def _age(self, copy_dir, seconds):
        """Make a private copy look last used `seconds` ago."""
        t = time.time() - seconds
        marker = os.path.join(copy_dir, 'last-used')
        if os.path.exists(marker):
            os.utime(marker, (t, t))
        os.utime(copy_dir, (t, t))

    def _copies(self):
        if not os.path.isdir(self.bin):
            return []
        return sorted(d for d in os.listdir(self.bin) if d.startswith('codex-') and not d.endswith('.del'))

    def _appserver(self):
        return os.path.join(self.codex_dir, 'plugins', '.plugin-appserver')

    def _bundle(self, name):
        return os.path.join(self.local, 'OpenAI', 'Codex', 'bin', name)

    def _versions(self, by_body):
        def read(path):
            with open(path, 'rb') as h:
                return by_body.get(h.read())
        reader = mock.Mock(side_effect=read)
        patch = mock.patch.object(common, '_read_codex_version', reader, create=True)
        patch.start()
        self.addCleanup(patch.stop)
        return reader

    def test_app_closed_uses_the_app_server_copy_not_path(self):
        os.makedirs(self._bundle('abc'))  # emptied bundle
        src = self._exe(self._appserver())
        self.assertEqual(common.codex_source_bin(), (src, 'app'))
        got = common.codex_bin()
        self.assertTrue(got.startswith(self.bin), got)
        for name in common.CODEX_HELPERS:
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(got), name)), name)

    def test_running_app_bundle_wins_when_newer(self):
        self._exe(self._appserver(), mtime=time.time() - 3600)
        src = self._exe(self._bundle('abc'), body=b'newer')
        self.assertEqual(common.codex_source_bin(), (src, 'app'))
        with open(common.codex_bin(), 'rb') as h:
            self.assertEqual(h.read(), b'newer')

    def test_twinlane_codex_bin_override_is_used_as_is(self):
        src = self._exe(os.path.join(self.tmp, 'custom'))
        os.environ['TWINLANE_CODEX_BIN'] = src
        self.assertEqual(common.codex_bin(), src)

    # a lease, not an exe lock, keeps a copy alive while it runs.

    def test_leased_copy_survives_prune_even_idle_and_over_budget(self):
        self._exe(self._appserver(), body=b'leased', mtime=time.time() - 600)
        binary, _, lease = common.acquire_codex()
        self.addCleanup(common.release_codex, lease)
        folder = os.path.dirname(binary)
        self._age(folder, 3 * 3600)
        for i in range(3):
            self._exe(self._bundle(f'b{i}'), body=b'other%d' % i, mtime=time.time() + i)
            last = common.codex_bin()
        common._prune_codex_copies(os.path.dirname(last), budget=1)
        self.assertEqual(self._copies(), sorted([os.path.basename(folder), os.path.basename(os.path.dirname(last))]))
        for name in common.CODEX_FILES:
            self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)
        common.release_codex(lease)
        common._prune_codex_copies(os.path.dirname(last), budget=1)
        self.assertTrue(os.path.isdir(folder))  # release makes it the newest valid fallback

    def test_touch_stamps_the_precise_time_not_the_coarse_tick(self):
        # utime(path, None) on Windows lags up to a clock tick: a release then tied an earlier one in _last_used.
        marker = os.path.join(self.tmp, 'last-used')
        before = time.time_ns() // 100 * 100  # NTFS keeps 100 ns units
        common._touch(marker)
        self.assertGreaterEqual(os.stat(marker).st_mtime_ns, before)

    def test_dead_holders_lease_does_not_protect(self):
        self._exe(self._appserver(), body=b'old', mtime=time.time() - 600)
        folder = os.path.dirname(common.codex_bin())
        stale = os.path.join(folder, 'leases', '999999999-1-deadbeef.json')
        os.makedirs(os.path.dirname(stale), exist_ok=True)
        with open(stale, 'w', encoding='utf-8') as h:
            h.write('{}')
        self._exe(self._bundle('new'), body=b'new')
        keep = os.path.dirname(common.codex_bin())
        common._prune_codex_copies(keep, budget=1)
        self.assertFalse(os.path.isdir(folder))

    def test_copy_pruned_before_its_lease_is_resolved_again(self):
        # Lease and prune share bin/.lock: a copy discarded between resolution and lease is never run.
        self._exe(self._appserver())
        common.codex_bin()
        real_take = common._take_lease
        pruned = []

        def prune_first(folder):
            if not pruned:
                pruned.append(folder)
                self.assertTrue(common._discard(folder))
            return real_take(folder)
        with mock.patch.object(common, '_take_lease', side_effect=prune_first):
            binary, _, lease = common.acquire_codex()
        self.addCleanup(common.release_codex, lease)
        self.assertEqual(len(pruned), 1)
        self.assertTrue(os.path.isfile(binary))
        self.assertTrue(os.path.isfile(lease))

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_real_running_exe_in_a_leased_copy_survives_prune(self):
        # Reproduced by a running exe did not stop its folder being renamed and its helpers deleted.
        app = self._appserver()
        os.makedirs(app)
        shutil.copy2(PING, os.path.join(app, 'codex.exe'))
        for name in common.CODEX_HELPERS:
            with open(os.path.join(app, name), 'wb') as h:
                h.write(b'helper')
        binary, _, lease = common.acquire_codex()
        self.addCleanup(common.release_codex, lease)
        proc = subprocess.Popen([binary, '-n', '30', '127.0.0.1'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=0x08000000)

        def stop():
            proc.kill()
            proc.wait(10)
        self.addCleanup(stop)
        folder = os.path.dirname(binary)
        self._age(folder, 3 * 3600)
        for i in range(3):
            self._exe(self._bundle(f'b{i}'), body=b'other%d' % i, mtime=time.time() + i)
            last = common.codex_bin()
        common._prune_codex_copies(os.path.dirname(last), budget=1)
        self.assertIsNone(proc.poll(), 'the leased exe should still be running')
        for name in common.CODEX_FILES:
            self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)

    # fail closed, never the app's own exe or the PATH CLI.

    def test_no_app_build_and_no_copy_is_none_not_path(self):
        binary, why = common.resolve_codex()
        self.assertIsNone(binary)
        self.assertIn('no complete Codex app build', why)
        self.assertIsNone(common.codex_bin())
        self.assertFalse(os.path.isdir(self.bin))

    def test_app_build_gone_uses_the_newest_valid_copy(self):
        self._exe(self._appserver())
        good = common.codex_bin()
        shutil.rmtree(self._appserver())  # the app removed its build
        self.assertEqual(common.codex_bin(), good)

    def test_incomplete_source_is_never_cached_or_run(self):
        self._exe(self._appserver(), helpers=False)  # the app mid-extract: codex.exe, no helpers yet
        self.assertIsNone(common.codex_bin())
        self.assertEqual(self._copies(), [])
        self._exe(self._appserver())  # helpers arrive
        got = common.codex_bin()
        self.assertTrue(got.startswith(self.bin), got)
        for name in common.CODEX_HELPERS:
            self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(got), name)), name)

    def test_failed_first_copy_is_none_not_the_app_exe(self):
        self._exe(self._appserver())
        with mock.patch.object(common.shutil, 'copy2', side_effect=OSError(28, 'disk full')):
            binary, why = common.resolve_codex()
        self.assertIsNone(binary)
        self.assertIn('disk full', why)

    def test_failed_copy_reuses_the_last_good_copy(self):
        self._exe(self._appserver(), body=b'v0', mtime=time.time() - 600)
        good = common.codex_bin()
        src = self._exe(self._appserver(), body=b'v1')
        with mock.patch.object(common.shutil, 'copy2', side_effect=OSError(28, 'disk full')):
            got = common.codex_bin()
        self.assertNotEqual(got, src)
        self.assertEqual(got, good)

    def test_incomplete_bundle_loses_to_a_complete_older_build(self):
        complete = self._exe(self._appserver(), mtime=time.time() - 3600)
        self._exe(self._bundle('abc'), body=b'newer', helpers=False)
        self.assertEqual(common.codex_source_bin(), (complete, 'app'))

    def test_source_changed_during_copy_is_discarded(self):
        self._exe(self._appserver(), body=b'v1', mtime=time.time() - 600)
        real_copy2 = shutil.copy2
        changed = []

        def copy_then_update(a, b, *args, **kw):
            r = real_copy2(a, b, *args, **kw)
            if not changed and os.path.basename(a) == 'codex.exe':
                changed.append(a)
                self._exe(self._appserver(), body=b'v2-longer')  # the app updates mid-copy
            return r
        with mock.patch.object(common.shutil, 'copy2', side_effect=copy_then_update):
            self.assertIsNone(common.codex_bin())
        self.assertEqual(self._copies(), [])
        with open(common.codex_bin(), 'rb') as h:  # the next call copies the settled build
            self.assertEqual(h.read(), b'v2-longer')

    def test_damaged_copy_is_rebuilt(self):
        self._exe(self._appserver())
        got = common.codex_bin()
        helper = os.path.join(os.path.dirname(got), common.CODEX_HELPERS[0])
        os.remove(helper)  # e.g. quarantined by antivirus
        self.assertEqual(common.codex_bin(), got)
        self.assertTrue(os.path.isfile(helper))
        self.assertTrue(os.path.isfile(os.path.join(os.path.dirname(got), 'manifest.json')))
        self.assertIsNotNone(common._valid_copy(os.path.dirname(got)))

    # a copy is bound to the bytes of its build, not just a version name.

    def test_changed_bytes_with_the_same_version_get_a_new_copy(self):
        self._versions({b'AAAA': '0.162.0', b'BBBB': '0.162.0'})
        self._exe(self._appserver(), body=b'AAAA', mtime=time.time() - 600)
        first = common.codex_bin()
        self._exe(self._appserver(), body=b'BBBB', mtime=time.time() - 60)
        second = common.codex_bin()
        self.assertNotEqual(os.path.dirname(first), os.path.dirname(second))
        with open(second, 'rb') as h:
            self.assertEqual(h.read(), b'BBBB')

    def test_same_size_edit_of_a_copy_invalidates_it(self):
        self._exe(self._appserver(), body=b'AAAA', mtime=time.time() - 600)
        got = common.codex_bin()
        with open(got, 'wb') as h:
            h.write(b'XXXX')  # same size, different bytes
        self.assertIsNone(common._valid_copy(os.path.dirname(got)))
        with open(common.codex_bin(), 'rb') as h:
            self.assertEqual(h.read(), b'AAAA')

    def test_copy_records_hashes_and_hashes_each_build_once(self):
        import hashlib
        src = self._exe(self._appserver(), body=b'AAAA', mtime=time.time() - 600)
        calls = []
        real = common._sha256_file

        def counting(path):
            calls.append(os.path.normcase(path))
            return real(path)
        with mock.patch.object(common, '_sha256_file', side_effect=counting, create=True):
            for _ in range(3):
                got = common.codex_bin()
        self.assertEqual(calls.count(os.path.normcase(src)), 1)
        files = common.read_json(os.path.join(os.path.dirname(got), 'manifest.json'))['files']
        self.assertEqual(files['codex.exe']['sha256'], hashlib.sha256(b'AAAA').hexdigest())
        self.assertEqual(set(files), set(common.CODEX_FILES))

    def test_one_lookup_hashes_one_copy_once(self):
        # Review: lookup and prune use stat checks; only the lease hashes, once, the copy it hands out.
        for i in range(common.CODEX_COPY_BUDGET):
            self._exe(self._bundle(f'b{i}'), body=b'v%d' % i, mtime=time.time() + i)
            common.codex_bin()
        self.assertEqual(len(self._copies()), 3)
        calls = []
        real = common._sha256_file

        def counting(path):
            calls.append(os.path.normcase(path))
            return real(path)
        with mock.patch.object(common, '_sha256_file', side_effect=counting, create=True):
            binary, _, lease = common.acquire_codex()
        common.release_codex(lease)
        folder = os.path.normcase(os.path.dirname(binary))
        self.assertEqual(sorted(calls), sorted(os.path.join(folder, n) for n in common.CODEX_FILES))

    def test_a_lease_refuses_a_copy_whose_bytes_changed_in_place(self):
        # Stat checks cannot see a same-size edit that restores the mtime; the lease's hash does, and the build
        # is copied again.
        self._exe(self._appserver(), body=b'AAAA', mtime=time.time() - 600)
        got = common.codex_bin()
        st = os.stat(got)
        with open(got, 'r+b') as h:
            h.write(b'XXXX')
        os.utime(got, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertIsNotNone(common._valid_copy(os.path.dirname(got)))  # the stat check passes
        self.assertEqual(common.codex_bin(), got)
        with open(got, 'rb') as h:
            self.assertEqual(h.read(), b'AAAA')

    # a hard budget of published copies.

    def test_budget_caps_published_copies(self):
        made = []
        for i in range(5):
            self._exe(self._bundle(f'b{i}'), body=b'v%d' % i, mtime=time.time() + i)
            made.append(os.path.basename(os.path.dirname(common.codex_bin())))
            self.assertLessEqual(len(self._copies()), common.CODEX_COPY_BUDGET, self._copies())
        self.assertEqual(self._copies(), sorted(made[-common.CODEX_COPY_BUDGET:]))

    def test_all_copies_leased_reuses_the_newest_instead_of_a_fourth(self):
        held = []
        for i in range(common.CODEX_COPY_BUDGET):
            self._exe(self._bundle(f'b{i}'), body=b'v%d' % i, mtime=time.time() + i)
            binary, _, lease = common.acquire_codex()
            self.addCleanup(common.release_codex, lease)
            held.append(binary)
        self._exe(self._bundle('b9'), body=b'v9', mtime=time.time() + 9)
        binary, why = common.resolve_codex()
        self.assertEqual(binary, held[-1])
        self.assertIn('budget', why)
        self.assertEqual(len(self._copies()), common.CODEX_COPY_BUDGET)

    # the mechanism refuses a test that would write to a non-temp bin/.

    def test_isolation_guard_refuses_a_bin_outside_temp(self):
        probe = os.path.join(os.path.abspath(os.sep), 'twinlane-isolation-' + uuid.uuid4().hex)
        self._exe(self._appserver())
        with mock.patch.object(common, 'BIN', probe):
            with self.assertRaises(RuntimeError):
                common.codex_bin()
            with self.assertRaises(RuntimeError):
                common._prune_codex_copies(os.path.join(probe, 'codex-x'))
        self.assertFalse(os.path.exists(probe))

    def test_each_lookup_marks_the_copy_used(self):
        self._exe(self._appserver())
        d = os.path.dirname(common.codex_bin())
        self._age(d, 2 * 3600)
        common.codex_bin()
        self.assertLess(time.time() - os.path.getmtime(os.path.join(d, 'last-used')), 60)

    # Review: builds are chosen and keyed by version; mtime only when the version cannot be read.

    def test_newer_version_beats_newer_mtime(self):
        self._versions({b'old': '0.150.0', b'new': '0.162.0-alpha.2'})
        self._exe(self._appserver(), body=b'old')  # rewritten later, older build
        bundle = self._exe(self._bundle('abc'), body=b'new', mtime=time.time() - 3600)
        self.assertEqual(common.codex_source_bin(), (bundle, 'app'))
        self.assertTrue(os.path.basename(os.path.dirname(common.codex_bin())).startswith('codex-v0.162.0-alpha.2-'))

    def test_release_beats_its_prerelease(self):
        self._versions({b'pre': '0.162.0-alpha.9', b'rel': '0.162.0'})
        self._exe(self._appserver(), body=b'pre')
        bundle = self._exe(self._bundle('abc'), body=b'rel', mtime=time.time() - 3600)
        self.assertEqual(common.codex_source_bin(), (bundle, 'app'))

    def test_re_extracted_same_build_shares_one_copy(self):
        self._versions({b'same': '0.162.0-alpha.2'})
        self._exe(self._appserver(), body=b'same', mtime=time.time() - 600)
        first = common.codex_bin()
        self._exe(self._appserver(), body=b'same', mtime=time.time() - 60)  # the app re-extracted it
        self.assertEqual(common.codex_bin(), first)
        self.assertEqual(len(self._copies()), 1, self._copies())

    def test_version_is_read_once_per_build(self):
        reader = self._versions({b'a': '0.162.0'})
        self._exe(self._appserver(), body=b'a', mtime=time.time() - 600)
        for _ in range(3):
            common.codex_bin()
        self.assertEqual(reader.call_count, 1)
        self._exe(self._appserver(), body=b'a', mtime=time.time() - 60)
        common.codex_bin()
        self.assertEqual(reader.call_count, 2)

    # Review: leftovers of a killed copy or a blocked delete are cleared.

    def test_staging_lives_with_its_creator_and_blocked_deletes_are_cleared(self):
        # no age rule; a staging folder goes only when the process that made it is dead.
        pid = os.getpid()
        live = os.path.join(self.bin, f'.copy-{pid}-{common.process_created(pid) or 0}-busy')
        dead = os.path.join(self.bin, '.copy-999999999-1-gone')
        legacy = os.path.join(self.bin, '.copy-abcd1234')  # an older twinlane: its creator is unknown
        gone = os.path.join(self.bin, 'codex-1-2.9-9.del')
        for d in (live, dead, legacy, gone):
            os.makedirs(d)
            with open(os.path.join(d, 'codex.exe'), 'wb') as h:
                h.write(b'x')
        for d in (live, legacy):
            self._age(d, 30 * 3600)
        self._exe(self._appserver())
        common.codex_bin()
        self.assertTrue(os.path.isdir(live))  # still copying, however old
        self.assertFalse(os.path.exists(dead))
        self.assertFalse(os.path.isdir(legacy))  # unknown creator expires after the policy bound
        self.assertFalse(os.path.exists(gone))

    # Review: races never raise.

    def test_bundle_vanishing_mid_lookup_does_not_raise(self):
        src = self._exe(self._appserver())
        ghost = os.path.join(self._bundle('gone'), 'codex.exe')
        real_glob = glob.glob
        with mock.patch.object(common.glob, 'glob',
                               side_effect=lambda p, **kw: [ghost] if 'OpenAI' in p else real_glob(p, **kw)):
            self.assertEqual(common.codex_source_bin(), (src, 'app'))
            self.assertTrue(common.codex_bin().startswith(self.bin))

    def test_prune_failure_keeps_the_copy(self):
        src = self._exe(self._appserver())
        with mock.patch.object(common, '_prune_codex_copies', side_effect=PermissionError('busy')):
            got = common.codex_bin()
        self.assertNotEqual(got, src)
        self.assertTrue(got.startswith(self.bin), got)

    # a lease covers lookup to launch; after that the OS is the lock. Windows refuses to delete a
    # running image, so prune deletes codex.exe first and leaves the folder whole when that is refused.

    def _ping_build(self, folder):
        os.makedirs(folder)
        shutil.copy2(PING, os.path.join(folder, 'codex.exe'))
        for name in common.CODEX_HELPERS:
            with open(os.path.join(folder, name), 'wb') as h:
                h.write(b'helper')

    def _run_ping(self, exe):
        proc = subprocess.Popen([exe, '-n', '60', '127.0.0.1'], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL, creationflags=0x08000000)

        def stop():
            if proc.poll() is None:
                proc.kill()
            proc.wait(10)
        self.addCleanup(stop)
        return proc, stop

    def _after_exit(self, attempt, timeout=10.0):
        """Windows releases a killed process's image a moment after wait() returns, so a delete right after can
        still be refused. Production retries on the next lookup; a test retries here."""
        deadline = time.monotonic() + timeout
        while not attempt() and time.monotonic() < deadline:
            time.sleep(0.1)
        return attempt()

    def _newer_copy(self, name='newer'):
        self._exe(self._bundle(name), body=name.encode(), mtime=time.time() + 60)
        return os.path.dirname(common.codex_bin())

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_windows_refuses_to_delete_a_running_image(self):
        # The premise prune rests on: deleting a running exe fails, deleting it after the process exits works.
        folder = os.path.join(self.tmp, 'premise')
        os.makedirs(folder)
        exe = os.path.join(folder, 'codex.exe')
        shutil.copy2(PING, exe)
        proc, stop = self._run_ping(exe)
        with self.assertRaises(OSError):
            os.remove(exe)
        with self.assertRaises(OSError):
            with open(exe, 'r+b'):
                pass
        self.assertIsNone(proc.poll())
        stop()
        self.assertTrue(self._after_exit(lambda: common._remove(exe)))
        self.assertFalse(os.path.exists(exe))

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_orphan_exe_keeps_its_copy_after_the_lease_is_gone(self):
        # The lease holder died (or released after a failed kill) while codex.exe still runs from the copy, and
        # no process list sees it (another user's token, or one that cannot be opened): the OS still refuses.
        self._ping_build(self._appserver())
        binary, _, lease = common.acquire_codex()
        proc, stop = self._run_ping(binary)
        common.release_codex(lease)
        folder = os.path.dirname(binary)
        self._age(folder, 3 * 3600)
        keep = self._newer_copy()
        with mock.patch.object(common, '_running_images', return_value={}, create=True):
            common._prune_codex_copies(keep, budget=1)
        self.assertIsNone(proc.poll(), 'the orphan should still be running')
        for name in common.CODEX_FILES:
            self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)
        stop()
        self.assertTrue(self._after_exit(lambda: common._prune_codex_copies(keep, budget=1) or
                                         not os.path.isdir(folder)))  # nothing runs from it any more

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_codex_exe_started_after_the_lease_check_survives(self):
        # a launch between the checks and the delete (old-format or external) is refused too.
        self._ping_build(self._appserver())
        binary = common.codex_bin()
        folder = os.path.dirname(binary)
        self._age(folder, 3 * 3600)
        keep = self._newer_copy()
        real_discard = common._discard
        started = []

        def launch_then_discard(d):
            if os.path.normcase(d) == os.path.normcase(folder) and not started:
                started.append(self._run_ping(binary)[0])
            return real_discard(d)
        with mock.patch.object(common, '_discard', side_effect=launch_then_discard):
            common._prune_codex_copies(keep, budget=1)
        self.assertEqual(len(started), 1)
        self.assertIsNone(started[0].poll())
        for name in common.CODEX_FILES:
            self.assertTrue(os.path.isfile(os.path.join(folder, name)), name)

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_orphan_helper_is_left_running_and_its_copy_is_never_selected_again(self):
        app = self._appserver()
        self._exe(app)
        shutil.copy2(PING, os.path.join(app, common.CODEX_HELPERS[0]))
        binary = common.codex_bin()
        folder = os.path.dirname(binary)
        helper = os.path.join(folder, common.CODEX_HELPERS[0])
        proc, stop = self._run_ping(helper)  # codex.exe has exited; its helper has not
        self._age(folder, 3 * 3600)
        keep = self._newer_copy()
        common._prune_codex_copies(keep, budget=1)
        self.assertIsNone(proc.poll())
        self.assertTrue(os.path.exists(binary))  # preflight keeps the entire copy intact
        self.assertTrue(os.path.isfile(helper))
        self.assertIsNotNone(common._valid_copy(folder))
        shutil.rmtree(app)
        self.assertNotEqual(common.resolve_codex()[0], binary)
        stop()
        self.assertTrue(self._after_exit(lambda: common._prune_codex_copies(keep, budget=1) or
                                         not os.path.isdir(folder)))  # retried once the helper exited

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_running_older_helper_cannot_cost_the_last_valid_fallback(self):
        app = self._appserver()
        self._exe(app, body=b'old')
        shutil.copy2(PING, os.path.join(app, common.CODEX_HELPERS[0]))
        old = common.codex_bin()
        proc, stop = self._run_ping(os.path.join(os.path.dirname(old), common.CODEX_HELPERS[0]))
        self._age(os.path.dirname(old), 3600)
        newer = self._newer_copy()
        self.assertFalse(common._make_room(1, budget=2))
        self.assertIsNone(proc.poll())
        self.assertEqual(common._newest_valid_copy(), os.path.join(newer, 'codex.exe'))
        self.assertIsNotNone(common._valid_copy(os.path.dirname(old)))
        for folder in (os.path.dirname(old), newer):
            self.assertTrue(all(os.path.isfile(os.path.join(folder, n)) for n in common.CODEX_FILES))
        stop()

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_discard_refuses_extra_file_and_both_hard_link_locations(self):
        self._ping_build(self._appserver())
        folder = os.path.dirname(common.codex_bin())
        exe = os.path.join(folder, 'codex.exe')
        alias = os.path.join(folder, 'alias.exe')
        os.link(exe, alias)
        proc, stop = self._run_ping(alias)
        self.assertFalse(common._discard(folder))
        self.assertTrue(all(os.path.isfile(os.path.join(folder, n)) for n in common.CODEX_FILES))
        stop()
        self.assertTrue(self._after_exit(lambda: common._remove(alias)))
        outside = os.path.join(self.tmp, 'outside.exe')
        os.link(exe, outside)
        self.assertFalse(common._discard(folder))
        self.assertTrue(all(os.path.isfile(os.path.join(folder, n)) for n in common.CODEX_FILES))
        os.remove(outside)
        with open(os.path.join(folder, 'alias.exe'), 'wb') as h:
            h.write(b'extra')
        self.assertFalse(common._discard(folder))
        self.assertTrue(all(os.path.isfile(os.path.join(folder, n)) for n in common.CODEX_FILES))

    def test_unconfirmed_holders_and_legacy_staging_expire_at_policy_bound(self):
        self._exe(self._appserver())
        folder = os.path.dirname(common.codex_bin())
        bound = common._uncertain_liveness_bound()
        self.assertEqual(bound, max(7200, 3600) + 600)
        leases = os.path.join(folder, 'leases')
        os.makedirs(leases, exist_ok=True)
        stamped = os.path.join(leases, '123-456-abcdef12.json')
        zero = os.path.join(leases, '123-0-abcdef12.json')
        for path in (stamped, zero):
            with open(path, 'w') as h:
                h.write('{}')
        creator = os.path.join(self.bin, '.copy-123-456-live')
        legacy = os.path.join(self.bin, '.copy-legacy')
        os.makedirs(creator)
        os.makedirs(legacy)
        with mock.patch.object(common, '_win_process_info', return_value=(True, None)):
            self.assertEqual(common._live_leases(folder), 2)
            common._clear_leftovers()
            self.assertTrue(os.path.isdir(creator))
            self.assertTrue(os.path.isdir(legacy))
            old = time.time() - bound - 1
            for path in (stamped, zero, creator, legacy):
                os.utime(path, (old, old))
            self.assertEqual(common._live_leases(folder), 0)
            common._clear_leftovers()
        self.assertFalse(os.path.isdir(creator))
        self.assertFalse(os.path.isdir(legacy))

    @unittest.skipUnless(os.name == 'nt', 'needs Windows delete semantics')
    def test_late_delete_failure_keeps_the_garbage_marker_until_last(self):
        # Review: a real failed delete after codex.exe is gone. Python opens files without delete sharing,
        # so an open reader makes manifest.json undeletable; the marker must outlive it and the build re-copies.
        self._exe(self._appserver())
        got = common.codex_bin()
        folder = os.path.dirname(got)
        with open(os.path.join(folder, 'manifest.json'), 'rb'):
            self.assertFalse(common._discard(folder))
            self.assertFalse(os.path.exists(got))
            self.assertTrue(os.path.isfile(os.path.join(folder, 'manifest.json')))
            self.assertTrue(os.path.isfile(os.path.join(folder, '.garbage')))
            self.assertIsNone(common._valid_copy(folder))
            self.assertIsNone(common._newest_valid_copy())
        self.assertEqual(common.codex_bin(), got)  # retried, then re-copied
        self.assertIsNotNone(common._valid_copy(folder))
        self.assertFalse(os.path.exists(os.path.join(folder, '.garbage')))

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_copy_missing_a_file_with_a_running_helper_stays_refused(self):
        app = self._appserver()
        self._exe(app, body=b'old')
        shutil.copy2(PING, os.path.join(app, common.CODEX_HELPERS[0]))
        old = os.path.dirname(common.codex_bin())
        proc, stop = self._run_ping(os.path.join(old, common.CODEX_HELPERS[0]))
        os.remove(os.path.join(old, common.CODEX_HELPERS[1]))
        self._age(old, 3600)
        newer = self._newer_copy()
        self.assertFalse(common._discard(old))
        self.assertFalse(common._make_room(1, budget=2))  # not counted as freed
        self.assertIsNone(proc.poll())
        self.assertTrue(os.path.isfile(os.path.join(old, 'codex.exe')))
        self.assertTrue(os.path.isfile(os.path.join(old, common.CODEX_HELPERS[0])))
        self.assertEqual(common._newest_valid_copy(), os.path.join(newer, 'codex.exe'))
        stop()
        self.assertTrue(self._after_exit(lambda: common._make_room(1, budget=2)))
        self.assertFalse(os.path.isdir(old))

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_blocked_delete_with_a_running_codex_exe_is_left_whole(self):
        gone = os.path.join(self.bin, 'codex-333357008-1791375985.4242-1.del')  # renamed by an older twinlane
        self._ping_build(gone)
        proc, stop = self._run_ping(os.path.join(gone, 'codex.exe'))
        self._exe(self._appserver())
        with mock.patch.object(common, '_running_images', return_value={}, create=True):
            common.codex_bin()
        self.assertIsNone(proc.poll())
        for name in common.CODEX_FILES:
            self.assertTrue(os.path.isfile(os.path.join(gone, name)), name)
        stop()
        self.assertTrue(self._after_exit(lambda: common.codex_bin() and not os.path.exists(gone)))

    @unittest.skipUnless(os.name == 'nt' and os.path.isfile(PING), 'needs Windows PING.EXE')
    def test_running_old_format_copy_is_never_pruned_by_age(self):
        # A copy from before manifests/leases, still run by a job started before the upgrade.
        old = os.path.join(self.bin, 'codex-333357008-1791375985')
        self._ping_build(old)
        proc, stop = self._run_ping(os.path.join(old, 'codex.exe'))
        self._age(old, 30 * 3600)
        self._exe(self._appserver())
        common.codex_bin()
        self.assertIsNone(proc.poll())
        for name in common.CODEX_FILES:
            self.assertTrue(os.path.isfile(os.path.join(old, name)), name)
        stop()
        self.assertTrue(self._after_exit(lambda: common.codex_bin() and not os.path.isdir(old)))

    def test_lease_without_a_creation_stamp_protects_while_its_pid_lives(self):
        # fail closed; a reused pid only keeps a copy longer, and the budget bounds that.
        self._exe(self._appserver(), body=b'a', mtime=time.time() - 600)
        held = os.path.dirname(common.codex_bin())
        self._exe(self._bundle('b'), body=b'b', mtime=time.time() - 300)
        stale = os.path.dirname(common.codex_bin())
        for folder, pid in ((held, os.getpid()), (stale, 999999999)):
            os.makedirs(os.path.join(folder, 'leases'), exist_ok=True)
            with open(os.path.join(folder, 'leases', f'{pid}-0-abcdef12.json'), 'w', encoding='utf-8') as h:
                h.write('{}')
        keep = self._newer_copy()
        common._prune_codex_copies(keep, budget=1)
        self.assertTrue(os.path.isfile(os.path.join(held, 'codex.exe')))
        self.assertFalse(os.path.isdir(stale))

    def test_replaced_source_file_with_restored_size_and_mtime_gets_a_new_copy(self):
        src = self._exe(self._appserver(), body=b'AAAA', mtime=time.time() - 600)
        first = common.codex_bin()
        st = os.stat(src)
        with open(src + '.new', 'wb') as h:
            h.write(b'BBBB')
        os.replace(src + '.new', src)  # an update that kept size and mtime: a new file, a new file index
        os.utime(src, ns=(st.st_atime_ns, st.st_mtime_ns))
        self.assertEqual((os.stat(src).st_size, os.stat(src).st_mtime_ns), (st.st_size, st.st_mtime_ns))
        second = common.codex_bin()
        self.assertNotEqual(os.path.dirname(first), os.path.dirname(second))
        with open(second, 'rb') as h:
            self.assertEqual(h.read(), b'BBBB')

    def test_changed_helper_gets_a_new_copy(self):
        self._exe(self._appserver(), mtime=time.time() - 600)
        first = common.codex_bin()
        helper = os.path.join(self._appserver(), common.CODEX_HELPERS[0])
        with open(helper, 'wb') as h:
            h.write(b'helper v2')  # codex.exe unchanged
        second = common.codex_bin()
        self.assertNotEqual(os.path.dirname(first), os.path.dirname(second))
        with open(os.path.join(os.path.dirname(second), common.CODEX_HELPERS[0]), 'rb') as h:
            self.assertEqual(h.read(), b'helper v2')

    def test_staging_and_undeleted_folders_count_toward_the_budget(self):
        self._exe(self._appserver(), body=b'a', mtime=time.time() - 600)
        good = common.codex_bin()
        held = []
        for name in ('codex-x.1-1.del', 'codex-y.2-2.del', '.copy-busy'):  # blocked deletes, another copy
            os.makedirs(os.path.join(self.bin, name))
            if name.endswith('.del'):  # an open reader (no delete sharing) blocks the real delete
                path = os.path.join(self.bin, name, 'data.bin')
                with open(path, 'wb') as h:
                    h.write(b'x')
                held.append(open(path, 'rb'))
                self.addCleanup(held[-1].close)
        real_rmtree = shutil.rmtree

        def blocked(path, *a, **kw):
            if '.copy-' in os.path.basename(str(path)):
                return None
            return real_rmtree(path, *a, **kw)
        real_copy2 = shutil.copy2
        copied = []

        def counting(a, b, *args, **kw):
            copied.append(a)
            return real_copy2(a, b, *args, **kw)
        self._exe(self._bundle('new'), body=b'new', mtime=time.time() + 60)
        with mock.patch.object(common.shutil, 'rmtree', side_effect=blocked), \
                mock.patch.object(common.shutil, 'copy2', side_effect=counting):
            binary, why = common.resolve_codex()
        self.assertEqual(binary, good)
        self.assertIn('budget', why)
        self.assertEqual(copied, [])  # nothing staged: the ceiling holds before copying

    def test_make_room_without_a_newest_copy_evicts_nothing_and_does_not_raise(self):
        # Review: a copy can turn invalid between the eviction list and the newest-copy lookup.
        self._exe(self._appserver())
        folder = os.path.dirname(common.codex_bin())
        with mock.patch.object(common, '_newest_valid_copy', return_value=None):
            self.assertFalse(common._make_room(0, budget=0))
        self.assertIsNotNone(common._valid_copy(folder))

    def test_corrupt_state_files_raise_nothing_but_oserror(self):
        src = self._exe(self._appserver())
        got = common.codex_bin()
        cache_path = os.path.join(self.bin, 'versions.json')
        cache = common.read_json(cache_path)
        common.write_json(cache_path, {k: {'version': 7, 'sha256': 9} for k in cache})
        self.assertEqual(common.codex_bin(), got)  # wrong types are dropped and recomputed
        cache = common.read_json(cache_path)
        common.write_json(cache_path, {k: {'sha256': 'é' * 64} for k in cache})
        self.assertEqual(common.codex_bin(), got)
        manifest = os.path.join(os.path.dirname(got), 'manifest.json')
        m = common.read_json(manifest)
        m['version'] = 5
        common.write_json(manifest, m)
        shutil.rmtree(os.path.dirname(src))
        self.assertEqual(common.codex_bin(), got)  # the fallback ranks a non-string version as unknown
        policy = os.path.join(self.tmp, 'policy.json')
        lease = os.path.join(os.path.dirname(got), 'leases', '123-0-abcdef12.json')
        os.makedirs(os.path.dirname(lease), exist_ok=True)
        common.write_json(lease, {})
        for bad in ([1], {'codex': [1]}, {'codex': {'timeout_s_by_profile': [1]}},
                    {'codex': {'timeout_s': float('inf')}}, {'codex': {'timeout_s_by_profile': {'code': float('nan')}}},
                    {'codex': {'timeout_s': -5}}, {'codex': {'timeout_s': True}}):
            common.write_json(policy, bad)
            with mock.patch.object(common, 'POLICY', policy), \
                    mock.patch.object(common, '_win_process_info', return_value=(True, None)), \
                    mock.patch.object(common, '_posix_start_time', return_value=None):
                self.assertEqual(common._uncertain_liveness_bound(), 600)
                self.assertEqual(common.codex_bin(), got)

    def test_codex_bin_inside_the_app_folders_is_copied_not_run(self):
        for folder in (self._bundle('abc'), self._appserver()):
            src = self._exe(folder)
            os.environ['TWINLANE_CODEX_BIN'] = src
            got = common.codex_bin()
            self.assertTrue(got.startswith(self.bin), (src, got))

    def test_fallback_reason_names_the_copy(self):
        self._exe(self._appserver())
        good = common.codex_bin()
        shutil.rmtree(self._appserver())
        binary, why = common.resolve_codex()
        self.assertEqual(binary, good)
        self.assertIn('earlier private copy', why)
        self.assertIn(good, why)


if __name__ == '__main__':
    unittest.main()
