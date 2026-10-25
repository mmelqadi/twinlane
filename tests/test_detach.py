"""--bg survival: spawn_detached_ex modes (Windows require_survival: wmi or refuse), the WMI env
hand-off, and real WMI starts that outlive a kill-on-close job, a desktop app's nested job chain and a
tree kill (simulations: no real desktop app restart has been run). Every process started here is a
python sleeper or a fake engine, killed before the test ends. No network, no model calls."""
import ctypes
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, envhandoff  # noqa: E402

# A harmless child that loads its hand-off (as cli.py does for _job), proves which env it got, and sleeps.
SLEEPER = ('import os, sys, time; sys.path.insert(0, sys.argv[1]); from twinlane import envhandoff\n'
           'rest, h = envhandoff.split(sys.argv[2:])\n'
           'if h: envhandoff.load(h)\n'
           'print("sleeper cwd=" + os.getcwd() + " mark=" + os.environ.get("TWINLANE_TEST_MARK", "-"), flush=True)\n'
           'open(rest[0], "w").write(os.environ.get("TWINLANE_TEST_MARK", "-"))\n'
           'time.sleep(120)\n')


def _wait(pred, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(0.1)
    return pred()


class _Tmp(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-detach-')
        self.pids = []

        def cleanup():
            for pid, created in self.pids:
                if common.pid_alive(pid, created):
                    common.kill_tree(pid, created)
            for _ in range(10):
                shutil.rmtree(self.tmp, ignore_errors=True)
                if not os.path.exists(self.tmp):
                    break
                time.sleep(0.3)
        self.addCleanup(cleanup)


@unittest.skipUnless(common.IS_WIN, 'Windows job objects and WMI')
class WmiSurvivesAKillOnCloseJob(_Tmp):
    """A1: from inside a kill-on-close job that forbids breakaway, the wmi start is outside that job
    and survives the job being closed. This SIMULATES the job-object close an app exit may cause; it is not
    a real Claude or Codex desktop app restart, which has not been run (see README, `--bg` restart note)."""

    def test_child_outlives_the_closed_job(self):
        mark, log, handoff = (os.path.join(self.tmp, n) for n in ('mark.txt', 'runner.log', 'runner.env.json'))
        launcher = subprocess.Popen(
            [sys.executable, '-c',
             'import ctypes, json, os, subprocess, sys, time; from ctypes import wintypes\n'
             'sys.path.insert(0, sys.argv[1]); from twinlane import common\n'
             'assert common.contain_children()\n'
             'info = common.job_info()\n'
             'd = common.spawn_detached_ex([sys.executable, "-c", sys.argv[2], sys.argv[1], sys.argv[3]],\n'
             '    cwd=sys.argv[6], env=dict(os.environ, TWINLANE_TEST_MARK="from-caller"), log_path=sys.argv[4],\n'
             '    require_survival=True, handoff=sys.argv[5])\n'
             'k = common._k32(); h = k.OpenProcess(0x1000, False, d["pid"]); inj = wintypes.BOOL()\n'
             'ok = k.IsProcessInJob(h, common._CONTAINER, ctypes.byref(inj)); k.CloseHandle(h)\n'
             'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])\n'
             'print(json.dumps({"info": info, "pid": d["pid"], "created": d["created"], "mode": d["mode"],\n'
             '    "in_launcher_job": bool(inj.value) if ok else None, "control": c.pid,\n'
             '    "control_created": common.process_created(c.pid)}), flush=True)\n'
             'time.sleep(120)\n',
             ROOT, SLEEPER, mark, log, handoff, self.tmp],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            env=dict(os.environ, TWINLANE_TEST_MARK='launcher-only'))
        try:
            line = launcher.stdout.readline()
            if not line:
                problem = launcher.stderr.read()[-2000:]
                if 'WMI unavailable' in problem and 'Access denied' in problem:
                    self.skipTest('WMI denied by this Windows environment')
                self.fail('launcher failed: ' + problem)
            r = json.loads(line)
        finally:
            if launcher.poll() is None:
                launcher.kill()  # TerminateProcess: its job handle closes and the job kills its members
            launcher.wait(timeout=30)
            launcher.stdout.close()
            launcher.stderr.close()
        self.pids += [(r['pid'], r['created']), (r['control'], r['control_created'])]
        self.assertTrue(r['info']['kill_on_close'])
        self.assertFalse(r['info']['breakaway_ok'])
        self.assertEqual(r['mode'], 'wmi')
        self.assertIsNotNone(r['created'])
        self.assertIs(r['in_launcher_job'], False, 'the wmi child is a member of the caller job')
        # The job really was closed: its ordinary member died with the launcher.
        self.assertTrue(_wait(lambda: not common.pid_alive(r['control'], r['control_created'])),
                        'the control child outlived the closed job, so the test proves nothing')
        time.sleep(1.0)
        self.assertTrue(common.pid_alive(r['pid'], r['created']), 'the wmi child died with the job')
        # It ran with the caller's env, in the caller's cwd, logging to runner.log, and consumed the hand-off.
        self.assertTrue(_wait(lambda: os.path.exists(mark)))
        with open(mark, encoding='utf-8') as h:
            self.assertEqual(h.read(), 'from-caller')
        self.assertFalse(os.path.exists(handoff))
        with open(envhandoff.decided_path(handoff), encoding='ascii') as h:
            self.assertEqual(h.read(), f"child {r['pid']} {r['created']}", 'the arbiter names the runner')
        with open(log, encoding='utf-8') as h:
            self.assertIn(f'sleeper cwd={self.tmp} mark=from-caller', h.read())
        # The stop path: kill_tree (taskkill /T) works on it.
        self.assertIs(common.kill_tree(r['pid'], r['created']), not common.IS_WIN)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['pid'], r['created'])))


@unittest.skipUnless(common.IS_WIN, 'Windows job objects and WMI')
class TwinlaneStopKillsAWmiRunner(_Tmp):
    """The stop path: `twinlane stop` kills a runner that WMI started, via runner.pid."""

    def test_twinlane_stop(self):
        mark, handoff = os.path.join(self.tmp, 'mark.txt'), os.path.join(self.tmp, 'h.json')
        try:
            d = common._spawn_wmi([sys.executable, '-c', SLEEPER, ROOT, mark], self.tmp,
                                  dict(os.environ, TWINLANE_TEST_MARK='stop'), os.path.join(self.tmp, 'log.txt'), handoff)
        except common.DetachError as exc:
            if 'WMI unavailable' in str(exc) and 'Access denied' in str(exc):
                self.skipTest('WMI denied by this Windows environment')
            raise
        self.pids.append((d['pid'], d['created']))
        self.assertTrue(common.pid_alive(d['pid'], d['created']))
        home = os.path.join(self.tmp, 'home')
        jid = '20261008-000000-claude-test'
        jd = os.path.join(home, 'jobs', jid)
        os.makedirs(jd)
        common.write_json(os.path.join(jd, 'job.json'), {'id': jid, 'primary': {'engine': 'claude'}})
        common.write_text(os.path.join(jd, 'runner.pid'), f"{d['pid']} {d['created']}")
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'twinlane', 'cli.py'), 'stop', jid, '--json'],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TWINLANE_HOME=home, PYTHONIOENCODING='utf-8'))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        receipt = json.loads(r.stdout)
        self.assertEqual(receipt['status'], 'stopped')
        self.assertEqual(receipt['killed_pids'], [] if common.IS_WIN else [d['pid']])
        if common.IS_WIN:
            self.assertEqual(receipt['kill_failed_pids'], [d['pid']])
        self.assertTrue(_wait(lambda: not common.pid_alive(d['pid'], d['created'])))


# A launcher that rebuilds a desktop app's nested job chain around itself, then starts a child. Under the Codex desktop
# app (read-only probe, 2026-10-11) a `--bg` runner recorded as detach=breakaway was still a member of
# An app-held kill-on-close job that forbids breakaway; the innermost job allowed breakaway, and
# job_info sees only that one. Here: outer = kill-on-close (breakaway forbidden, or allowed with
# "allow"), inner = kill-on-close + breakaway allowed.
CHAIN_LAUNCHER = (
    'import ctypes, json, os, subprocess, sys, time; from ctypes import wintypes\n'
    'root, what, outer_mode, tmp = sys.argv[1:5]; sys.path.insert(0, root); from twinlane import common\n'
    'k = common._k32()\n'
    'def job(flags):\n'
    '    j = k.CreateJobObjectW(None, None); e = common._job_limits_struct(); e.Basic.LimitFlags = flags\n'
    '    assert k.SetInformationJobObject(j, 9, ctypes.byref(e), ctypes.sizeof(e))\n'
    '    assert k.AssignProcessToJobObject(j, k.GetCurrentProcess()), ctypes.get_last_error()\n'
    '    return j\n'
    'kill, ok = common.JOB_LIMIT_KILL_ON_JOB_CLOSE, common.JOB_LIMIT_BREAKAWAY_OK\n'
    'outer = job(kill | (ok if outer_mode == "allow" else 0)); inner = job(kill | ok)\n'
    'def member(pid, j):\n'
    '    h = k.OpenProcess(0x1000, False, pid); b = wintypes.BOOL(); r = k.IsProcessInJob(h, j, ctypes.byref(b))\n'
    '    k.CloseHandle(h); return bool(b.value) if r else None\n'
    'out = {"info": common.job_info(), "launcher": os.getpid(), "launcher_created": common.process_created(os.getpid())}\n'
    'if what == "raw":  # the old --bg start: CREATE_BREAKAWAY_FROM_JOB, its parent is this launcher\n'
    '    p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdout=subprocess.DEVNULL,\n'
    '        stderr=subprocess.DEVNULL, creationflags=common.CREATE_NEW_PROCESS_GROUP | common.CREATE_NO_WINDOW\n'
    '        | common.CREATE_BREAKAWAY_FROM_JOB)\n'
    '    out.update(pid=p.pid, created=common.process_created(p.pid), mode="raw-breakaway")\n'
    'elif what == "twinlane":\n'
    '    d = common.spawn_detached_ex([sys.executable, "-c", sys.argv[5], root, os.path.join(tmp, "mark.txt")],\n'
    '        cwd=tmp, env=dict(os.environ, TWINLANE_TEST_MARK="from-caller"), log_path=os.path.join(tmp, "runner.log"),\n'
    '        require_survival=True, handoff=os.path.join(tmp, "runner.env.json"))\n'
    '    out.update(pid=d["pid"], created=d["created"], mode=d["mode"])\n'
    'else:  # bg: the real `twinlane run`, fake engines from the inherited env\n'
    '    r = subprocess.run([sys.executable, os.path.join(root, "twinlane/cli.py"), "run"] + sys.argv[5:],\n'
    '        capture_output=True, text=True, encoding="utf-8", errors="replace")\n'
    '    out.update(rc=r.returncode, stdout=r.stdout, stderr=r.stderr[-2000:])\n'
    'if "pid" in out' ':\n'
    '    out.update(in_outer=member(out["pid"], outer), in_inner=member(out["pid"], inner))\n'
    'c = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], stdout=subprocess.DEVNULL)\n'
    'out.update(control=c.pid, control_created=common.process_created(c.pid))\n'
    'print(json.dumps(out), flush=True)\n'
    'time.sleep(120)\n')

# The app's shell starts the launcher and outlives its exit. The parent keeps stdin open:
# after acknowledging launcher exit the shell blocks on that pipe, independently of its
# child's lifetime. Only the fixture's explicit release/identity-checked kill ends it.
# Otherwise taskkill can kill the launcher first and race the shell's automatic exit.
SHELL = ('import subprocess, sys; subprocess.Popen(sys.argv[1:]).wait(); '
         'print("launcher exited; shell held", flush=True); sys.stdin.buffer.read(1)')


def _launch_chain(test, what, outer_mode, *extra):
    """(shell, launcher report). Every pid it reports is registered for identity-checked cleanup."""
    shell = subprocess.Popen([sys.executable, '-c', SHELL, sys.executable, '-c', CHAIN_LAUNCHER, ROOT, what,
                              outer_mode, test.tmp, *extra],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             text=True, encoding='utf-8')
    shell_created = common.process_created(shell.pid)
    shell.twinlane_created = shell_created

    def stop_shell():
        if shell.poll() is None and common.pid_alive(shell.pid, shell_created):
            common.kill_tree(shell.pid, shell_created)
        shell.wait(timeout=30)
        shell.stdin.close()
        shell.stdout.close()
        shell.stderr.close()
    test.addCleanup(stop_shell)
    line = shell.stdout.readline()
    if not line:
        shell.wait(timeout=30)
        test.fail('launcher failed: ' + shell.stderr.read()[-2000:])
    r = json.loads(line)
    test.pids += [(r['control'], r['control_created']), (r['launcher'], r['launcher_created'])]
    if r.get('pid'):
        test.pids.append((r['pid'], r['created']))
    return shell, r


def _tree_kill(shell):
    """What an app exit can do: terminate its shell's process tree (taskkill /T), launcher still alive.
    The launcher's death then closes its job handles, so the chain's kill-on-close jobs fire too."""
    assert shell.twinlane_created is not None, 'the owned shell has no recorded creation stamp'
    assert common.kill_tree(shell.pid, shell.twinlane_created) is (not common.IS_WIN), 'snapshot-only Windows kill must stay unconfirmed'
    shell.wait(timeout=30)


def _job_close_only(r):
    """Terminate only the identity-proven launcher's exact handle; its job handles close (no /T)."""
    from ctypes import wintypes
    assert r['launcher_created'] is not None, 'the owned launcher has no recorded creation stamp'
    k = common._k32()
    h = k.OpenProcess(0x1000 | 0x0001, False, r['launcher'])  # QUERY_LIMITED_INFORMATION | TERMINATE
    assert h, 'cannot open the owned launcher for identity verification and termination'
    try:
        code = wintypes.DWORD()
        assert k.GetExitCodeProcess(h, ctypes.byref(code)) and code.value == 259, 'launcher is not alive'
        times = [ctypes.c_uint64() for _ in range(4)]
        assert k.GetProcessTimes(h, *(ctypes.byref(t) for t in times)), 'launcher identity is unreadable'
        assert times[0].value == r['launcher_created'], 'launcher pid was reused'
        k.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
        k.TerminateProcess.restype = wintypes.BOOL
        assert k.TerminateProcess(h, 1), 'exact-handle launcher termination failed'
    finally:
        k.CloseHandle(h)
    assert _wait(lambda: not common.pid_alive(r['launcher'], r['launcher_created'])), 'launcher did not exit'


@unittest.skipUnless(common.IS_WIN, 'Windows job objects and WMI')
class NestedJobChain(_Tmp):
    """Nested-job-chain hazards reproduced with python sleepers only (a SIMULATION, not a real app exit):
    the original quit's exact termination trigger remains unknown.
    breakaway from a nested chain leaves only the jobs up to the first that forbids it, and keeps
    the launcher as parent. The --bg start (wmi) must survive both a job close and a tree kill."""

    def test_outer_shell_lifetime_is_independent_of_launcher_exit(self):
        shell, r = _launch_chain(self, 'raw', 'forbid')
        _job_close_only(r)
        # EOF with the old fixture proves that the shell exited with its child.
        # The corrected fixture acknowledges child exit, then blocks on its owned pipe.
        acknowledgement = shell.stdout.readline()
        self.assertTrue(common.pid_alive(shell.pid, shell.twinlane_created),
                        'outer fixture shell exited when its launcher ended')
        self.assertEqual(acknowledgement.strip(), 'launcher exited; shell held')
        self.assertTrue(_wait(lambda: not common.pid_alive(r['control'], r['control_created'])))
        self.assertTrue(_wait(lambda: not common.pid_alive(r['pid'], r['created'])))
        _tree_kill(shell)
        self.assertFalse(common.pid_alive(shell.pid, shell.twinlane_created))

    def test_old_breakaway_stays_in_the_outer_job_and_dies_with_it(self):
        shell, r = _launch_chain(self, 'raw', 'forbid')
        self.assertEqual(r['info'], {'in_job': True, 'kill_on_close': True, 'breakaway_ok': True},
                         'job_info must see what a real app quit recorded: only the innermost job')
        self.assertEqual((r['in_inner'], r['in_outer']), (False, True), 'breakaway left the outer job')
        _job_close_only(r)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['control'], r['control_created'])))
        self.assertTrue(_wait(lambda: not common.pid_alive(r['pid'], r['created'])),
                        'a breakaway child outlived the outer kill-on-close job')

    def test_old_breakaway_dies_with_a_tree_kill_even_outside_every_job(self):
        shell, r = _launch_chain(self, 'raw', 'allow')
        self.assertEqual((r['in_inner'], r['in_outer']), (False, False))
        _tree_kill(shell)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['pid'], r['created'])),
                        'a tree kill with the launcher alive did not reach its breakaway child')

    def test_old_breakaway_orphan_outside_every_job_survives_a_job_close(self):
        # Contrast: with no enclosing kill-on-close job, an orphan (launcher gone) is not killed.
        shell, r = _launch_chain(self, 'raw', 'allow')
        _job_close_only(r)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['control'], r['control_created'])))
        time.sleep(1.0)
        self.assertTrue(common.pid_alive(r['pid'], r['created']))

    def test_bg_start_survives_tree_kill_and_job_close(self):
        shell, r = _launch_chain(self, 'twinlane', 'forbid', SLEEPER)
        self.assertEqual(r['mode'], 'wmi', 'require_survival trusted breakaway inside a job chain')
        self.assertIsNotNone(r['created'])
        self.assertEqual((r['in_inner'], r['in_outer']), (False, False))
        _tree_kill(shell)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['launcher'], r['launcher_created'])))
        self.assertTrue(_wait(lambda: not common.pid_alive(r['control'], r['control_created'])),
                        'the control child outlived the tree kill and job close, so the test proves nothing')
        time.sleep(1.0)
        self.assertTrue(common.pid_alive(r['pid'], r['created']), 'the --bg child died with the app tree')
        mark = os.path.join(self.tmp, 'mark.txt')
        self.assertTrue(_wait(lambda: os.path.exists(mark)))
        with open(mark, encoding='utf-8') as h:
            self.assertEqual(h.read(), 'from-caller')
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'runner.env.json')))
        self.assertIs(common.kill_tree(r['pid'], r['created']), not common.IS_WIN)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['pid'], r['created'])))


class EnvHandoffFile(_Tmp):
    """The hand-off: written, loaded (env, cwd, stdio applied), deleted; never left behind on failure."""

    def test_write_split_load_delete(self):
        path, log, cwd = os.path.join(self.tmp, 'h.json'), os.path.join(self.tmp, 'log.txt'), \
            os.path.join(self.tmp, 'cwd')
        os.makedirs(cwd)
        env = {k: v for k, v in os.environ.items() if k.upper() in ('PATH', 'SYSTEMROOT', 'TEMP', 'TMP')}
        env['TWINLANE_TEST_MARK'] = 'loaded'
        envhandoff.write(path, env, log, cwd)
        with open(path, encoding='utf-8') as h:
            data = json.load(h)
        self.assertEqual((data['env']['TWINLANE_TEST_MARK'], data['log_path'], data['cwd']), ('loaded', log, cwd))
        envhandoff.write(path, env, log, cwd)  # a stale file is replaced, not an error
        self.assertEqual(envhandoff.split(['_job', 'J', envhandoff.ARG, path]), (['_job', 'J'], path))
        self.assertEqual(envhandoff.split(['_job', 'J']), (['_job', 'J'], None))
        r = subprocess.run(
            [sys.executable, '-c',
             'import os, sys; sys.path.insert(0, sys.argv[1]); from twinlane import envhandoff\n'
             'envhandoff.load(sys.argv[2])\n'
             'print("cwd=" + os.getcwd(), "mark=" + os.environ.get("TWINLANE_TEST_MARK", "-"),\n'
             '      "leak=" + os.environ.get("TWINLANE_TEST_LEAK", "-"), "stdin=" + repr(sys.stdin.read()))\n'
             'print("to-stderr", file=sys.stderr)\n',
             ROOT, path],
            capture_output=True, text=True, timeout=60, input='typed',
            env=dict(os.environ, TWINLANE_TEST_LEAK='parent-only'))
        self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        self.assertEqual(r.stdout, '', 'stdout must go to the log, not the inherited pipe')
        self.assertFalse(os.path.exists(path), 'the hand-off file must be deleted once loaded')
        with open(log, encoding='utf-8') as h:
            text = h.read()
        # macOS reports the resolved cwd (/var is a symlink to /private/var)
        self.assertTrue(any(f"cwd={c} mark=loaded leak=- stdin=''" in text
                            for c in (cwd, os.path.realpath(cwd))), text)
        self.assertIn('to-stderr', text)

    def test_unusable_file_is_deleted_and_raises(self):
        path = os.path.join(self.tmp, 'h.json')
        with open(path, 'w', encoding='utf-8') as h:
            h.write('{"v": 1, "env": {}}')
        with self.assertRaises(ValueError):
            envhandoff.load(path)
        self.assertFalse(os.path.exists(path))

    def test_job_entry_refuses_without_its_environment(self):
        # cli.py `_job` with a missing hand-off exits 5 before touching any job.
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'twinlane', 'cli.py'), '_job', 'no-such-job',
                            envhandoff.ARG, os.path.join(self.tmp, 'missing.json')],
                           capture_output=True, text=True, timeout=60,
                           env=dict(os.environ, TWINLANE_HOME=os.path.join(self.tmp, 'home')))
        self.assertEqual(r.returncode, 5, r.stderr[-1000:])
        self.assertFalse(os.path.exists(os.path.join(self.tmp, 'home', 'jobs')))


class _FakeProc:
    pid = 4242


@unittest.skipUnless(common.IS_WIN, 'Windows creation flags')
class DetachOrder(unittest.TestCase):
    """The detach order: with require_survival every Windows caller gets wmi or a refusal; best effort tries
    breakaway -> wmi (only with a hand-off) -> in-job; best effort outside a job starts directly."""

    DOOMED = {'in_job': True, 'kill_on_close': True, 'breakaway_ok': False}
    NESTED_CHAIN = {'in_job': True, 'kill_on_close': True, 'breakaway_ok': True}  # what runners lost in a real app quit recorded

    def run_spawn(self, info, breakaway_ok=False, wmi=None, **kw):
        calls = []

        def popen(argv, **pkw):
            mode = 'breakaway' if pkw['creationflags'] & common.CREATE_BREAKAWAY_FROM_JOB else 'plain'
            calls.append(mode)
            if mode == 'breakaway' and not breakaway_ok:
                raise PermissionError(5, 'Access is denied')
            return _FakeProc()

        def spawn_wmi(*a):
            calls.append('wmi')
            if isinstance(wmi, Exception):
                raise wmi
            return wmi

        with mock.patch.object(common, 'job_info', return_value=info), \
                mock.patch.object(common.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common, '_spawn_wmi', side_effect=spawn_wmi), \
                mock.patch.object(common, 'process_created', return_value=99):
            try:
                return common.spawn_detached_ex(['x'], **kw), calls
            except common.DetachError as e:
                return e, calls

    def test_best_effort_tries_breakaway_first(self):
        d, calls = self.run_spawn(self.DOOMED, breakaway_ok=True, wmi={'pid': 1, 'created': 2}, handoff='h')
        self.assertEqual((d['mode'], calls), ('breakaway', ['breakaway']))

    def test_survival_never_trusts_breakaway(self):
        # A real app quit: the innermost job allowed breakaway, an outer app job did not; 'breakaway' died with the app.
        d, calls = self.run_spawn(self.NESTED_CHAIN, breakaway_ok=True, wmi={'pid': 7, 'created': 8},
                                  require_survival=True, handoff='h')
        self.assertEqual((d['mode'], d['pid'], d['created'], calls), ('wmi', 7, 8, ['wmi']))

    def test_wmi_when_the_job_forbids_breakaway(self):
        d, calls = self.run_spawn(self.DOOMED, wmi={'pid': 7, 'created': 8}, require_survival=True, handoff='h')
        self.assertEqual((d['mode'], d['pid'], d['created'], calls), ('wmi', 7, 8, ['wmi']))

    def test_survival_with_unknown_job_state_uses_wmi(self):
        d, calls = self.run_spawn({'in_job': None, 'kill_on_close': None, 'breakaway_ok': None}, breakaway_ok=True,
                                  wmi={'pid': 7, 'created': 8}, require_survival=True, handoff='h')
        self.assertEqual((d['mode'], calls), ('wmi', ['wmi']))

    def test_wmi_failure_refuses_when_survival_is_required(self):
        e, calls = self.run_spawn(self.DOOMED, wmi=common.DetachError('Win32_Process.Create returned 2'),
                                  require_survival=True, handoff='h')
        self.assertIsInstance(e, common.DetachError)
        self.assertIn('returned 2', str(e))
        self.assertEqual(calls, ['wmi'])

    def test_wmi_unavailable_refuses_even_in_a_job_that_does_not_kill(self):
        # The innermost job not killing on close says nothing about the jobs above it.
        for info in ({'in_job': True, 'kill_on_close': False, 'breakaway_ok': True},
                     {'in_job': True, 'kill_on_close': False, 'breakaway_ok': False}):
            e, calls = self.run_spawn(info, breakaway_ok=True, require_survival=True, handoff='h',
                                      wmi=common.DetachError(f'WMI unavailable before Create: {common.WMI_PRECREATE}'))
            self.assertIsInstance(e, common.DetachError)
            self.assertNotIsInstance(e, common.DetachAmbiguous)
            self.assertIn('WMI unavailable', str(e))
            self.assertEqual(calls, ['wmi'], 'an ancestry-linked start was tried')

    def test_ambiguous_wmi_start_is_raised_without_fallback(self):
        e, calls = self.run_spawn(self.NESTED_CHAIN, breakaway_ok=True, require_survival=True, handoff='h',
                                  wmi=common.DetachAmbiguous('no ProcessId from WMI', runner_may_run=True))
        self.assertIsInstance(e, common.DetachAmbiguous)
        self.assertTrue(e.runner_may_run)
        self.assertEqual(calls, ['wmi'])

    def test_wmi_failure_falls_back_to_in_job(self):
        d, calls = self.run_spawn(self.DOOMED, wmi=common.DetachError('x'), handoff='h')
        self.assertEqual((d['mode'], calls), ('in-job', ['breakaway', 'wmi', 'plain']))

    def test_no_handoff_no_wmi(self):
        # The hook's best-effort refresh has no hand-off: unchanged behaviour, no PowerShell.
        d, calls = self.run_spawn(self.DOOMED)
        self.assertEqual((d['mode'], calls), ('in-job', ['breakaway', 'plain']))
        e, calls = self.run_spawn(self.NESTED_CHAIN, breakaway_ok=True, require_survival=True)
        self.assertIsInstance(e, common.DetachError)
        self.assertIn('hand-off', str(e))
        self.assertEqual(calls, [], 'nothing may start without the WMI broker')

    def test_best_effort_outside_a_job_starts_directly(self):
        d, calls = self.run_spawn({'in_job': False, 'kill_on_close': False, 'breakaway_ok': True},
                                  wmi={'pid': 1, 'created': 2}, handoff='h')
        self.assertEqual((d['mode'], calls), ('no-job', ['plain']))


class PosixDetach(unittest.TestCase):
    """Non-Windows: no job objects; the child gets its own session, so a signal to the caller's
    process group or session does not reach it."""

    def test_posix_new_session(self):
        seen = {}

        def popen(argv, **kw):
            seen.update(kw)
            return _FakeProc()
        with mock.patch.object(common, 'IS_WIN', False), \
                mock.patch.object(common, 'job_info', return_value={'in_job': False}), \
                mock.patch.object(common.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common, '_spawn_wmi') as wmi, \
                mock.patch.object(common, 'process_created', return_value=99):
            d = common.spawn_detached_ex(['x'], require_survival=True, handoff='h')
        wmi.assert_not_called()
        self.assertEqual((d['mode'], d['pid'], d['created']), ('posix', 4242, 99))
        self.assertTrue(seen['start_new_session'])
        self.assertNotIn('creationflags', seen)


@unittest.skipUnless(common.IS_WIN, 'Windows WMI')
class SpawnWmiFailures(_Tmp):
    """A failed WMI start removes the hand-off (so no late child can run with it) and raises."""

    def check(self, completed):
        handoff = os.path.join(self.tmp, 'h.json')
        seen = {}

        def run(argv, **kw):
            seen['cmd'] = kw['env']['TWINLANE_WMI_CMD']
            seen['exists'] = os.path.exists(handoff)
            return completed

        with mock.patch.object(common.subprocess, 'run', side_effect=run):
            with self.assertRaises(common.DetachError) as cm:
                common._spawn_wmi(['py', '-c', 'x y'], self.tmp, {'A': '1'}, None, handoff)
        self.assertTrue(seen['exists'], 'the hand-off must exist before the process is created')
        self.assertTrue(seen['cmd'].endswith(subprocess.list2cmdline([envhandoff.ARG, handoff])))
        self.assertIn('"x y"', seen['cmd'])
        self.assertFalse(os.path.exists(handoff))
        return str(cm.exception)

    def test_create_error_code(self):
        self.assertIn('returned 21', self.check(subprocess.CompletedProcess([], 0, '21 \n', '')))

    def test_powershell_error(self):
        msg = self.check(subprocess.CompletedProcess([], 1, '', 'Exception calling "Create"\n  boom'))
        self.assertIn('rc 1', msg)
        self.assertIn('boom', msg)


AMBIG_INFO = {'in_job': True, 'kill_on_close': False, 'breakaway_ok': False}  # in-job would be available


@unittest.skipUnless(common.IS_WIN, 'Windows WMI')
class WmiNeverFallsBackOnceCreateMayHaveRun(_Tmp):
    """A possible launch: after PowerShell may have reached Create, the only outcomes are the confirmed child or a
    DetachAmbiguous that spawn_detached_ex does not fall back from. Only a proven pre-Create failure
    falls back."""

    def spawn(self, run_effect, **spawn_kw):
        handoff = os.path.join(self.tmp, 'runner.env.json')
        plain = []

        def popen(argv, **kw):
            if kw['creationflags'] & common.CREATE_BREAKAWAY_FROM_JOB:
                raise PermissionError(5, 'Access is denied')
            plain.append(argv)
            return _FakeProc()

        def run(argv, **kw):
            return run_effect(handoff, kw)

        with mock.patch.object(common, 'job_info', return_value=AMBIG_INFO), \
                mock.patch.object(common.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common.subprocess, 'run', side_effect=run):
            try:
                out = common.spawn_detached_ex(['py', '-c', 'pass'], cwd=self.tmp, env={'A': '1'},
                                               handoff=handoff, **spawn_kw)
            except common.DetachError as e:
                out = e
        self.assertFalse(os.path.exists(handoff), 'the hand-off must be gone when the launcher returns')
        return out, plain

    def sleeper(self):
        p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (p.kill(), p.wait(10)))
        return p.pid, common.process_created(p.pid)

    @staticmethod
    def child_wins(handoff, pid, stamp):
        """What a real child does first: decide on the arbiter, then read (and delete) the hand-off."""
        assert envhandoff.decide(handoff, f'child {pid} {stamp}')
        envhandoff._read(handoff)

    def test_unexpected_last_line_still_returns_the_child(self):
        # Codex's repro: Create succeeded, the child claimed the hand-off, then PowerShell printed more.
        pid, stamp = self.sleeper()

        def effect(handoff, kw):
            self.child_wins(handoff, pid, stamp)
            return subprocess.CompletedProcess([], 0, f'0 {pid}\nWARNING: profile noise\n', '')
        out, plain = self.spawn(effect)
        self.assertNotIsInstance(out, Exception, out)
        self.assertEqual((out['mode'], out['pid'], out['created'], plain), ('wmi', pid, stamp, []))

    def test_unparseable_output_after_a_child_won_returns_that_child(self):
        pid, stamp = self.sleeper()

        def effect(handoff, kw):
            self.child_wins(handoff, pid, stamp)  # a child is running the job
            return subprocess.CompletedProcess([], 0, 'something unexpected\n', '')
        out, plain = self.spawn(effect)
        self.assertNotIsInstance(out, Exception, out)
        self.assertEqual((out['mode'], out['pid'], out['created']), ('wmi', pid, stamp))
        self.assertEqual(plain, [], 'fell back to in-job: the job would run twice')

    def test_timeout_after_a_child_won_returns_that_child(self):
        pid, stamp = self.sleeper()

        def effect(handoff, kw):
            self.child_wins(handoff, pid, stamp)
            raise subprocess.TimeoutExpired('powershell', 60)
        out, plain = self.spawn(effect)
        self.assertNotIsInstance(out, Exception, out)
        self.assertEqual((out['pid'], out['created'], plain), (pid, stamp, []))

    def test_timeout_is_ambiguous_and_never_falls_back(self):
        def effect(handoff, kw):
            raise subprocess.TimeoutExpired('powershell', 60)
        out, plain = self.spawn(effect)
        self.assertIsInstance(out, common.DetachAmbiguous)
        self.assertFalse(out.runner_may_run, 'nobody claimed the hand-off, so no runner can start')
        self.assertEqual(plain, [])

    def test_proven_pre_create_failure_falls_back(self):
        def effect(handoff, kw):
            return subprocess.CompletedProcess([], 3, f'{common.WMI_PRECREATE} Access denied\n', '')
        out, plain = self.spawn(effect)
        self.assertNotIsInstance(out, Exception, out)
        self.assertEqual((out['mode'], len(plain)), ('in-job', 1))

    def test_missing_powershell_falls_back(self):
        def effect(handoff, kw):
            raise FileNotFoundError(2, 'powershell.exe')
        out, plain = self.spawn(effect)
        self.assertEqual((out['mode'], len(plain)), ('in-job', 1))

    def test_with_survival_a_pre_create_failure_refuses(self):
        # --bg: WMI unavailable is a refusal, never an ancestry-linked start; nothing ran, so no ambiguity.
        for effect in (lambda h, kw: subprocess.CompletedProcess([], 3, f'{common.WMI_PRECREATE} Access denied\n', ''),
                       lambda h, kw: (_ for _ in ()).throw(FileNotFoundError(2, 'powershell.exe'))):
            out, plain = self.spawn(effect, require_survival=True)
            self.assertIsInstance(out, common.DetachError)
            self.assertNotIsInstance(out, common.DetachAmbiguous)
            self.assertEqual(plain, [], 'fell back to a start that can die with an enclosing job')

    def test_with_survival_a_timeout_stays_ambiguous(self):
        def effect(handoff, kw):
            raise subprocess.TimeoutExpired('powershell', 60)
        out, plain = self.spawn(effect, require_survival=True)
        self.assertIsInstance(out, common.DetachAmbiguous)
        self.assertEqual(plain, [])


class StartBackgroundReportsAmbiguity(_Tmp):
    """A possible launch: _start_background never writes a terminal receipt over a runner that may exist."""

    def start(self, exc):
        from twinlane import jobs
        jid = '20261008-000000-claude-amb1'
        with mock.patch.object(common, 'JOBS', os.path.join(self.tmp, 'jobs')), \
                mock.patch.object(common, 'spawn_detached_ex', side_effect=exc):
            os.makedirs(jobs.job_dir(jid))
            common.write_json(jobs._p(jid, 'job.json'), {'id': jid})
            with mock.patch('sys.stdout'), mock.patch('sys.stderr'):
                rc = jobs._start_background(jid, {'engine': 'claude', 'profile': 'code'}, False)
            return rc, common.read_json(jobs._p(jid, 'result.json')), common.read_json(jobs._p(jid, 'job.json'))

    def test_may_run_writes_no_receipt(self):
        rc, result, spec = self.start(common.DetachAmbiguous('no ProcessId', runner_may_run=True))
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIsNone(result)
        self.assertEqual(spec['detach']['mode'], 'ambiguous')

    def test_cannot_run_is_refused(self):
        rc, result, _ = self.start(common.DetachAmbiguous('timeout', runner_may_run=False))
        self.assertEqual((rc, result['status']), (common.EXIT_REFUSED, 'refused'))

    def runner_pid_after(self, exc):
        from twinlane import jobs
        self.start(exc)
        with mock.patch.object(common, 'JOBS', os.path.join(self.tmp, 'jobs')):
            return jobs._read_text(jobs._p('20261008-000000-claude-amb1', 'runner.pid'))

    def test_an_unstamped_pid_is_never_recorded(self):
        self.assertEqual(self.runner_pid_after(common.DetachAmbiguous('x', pid=4444, created=None)), '')

    def test_a_stamped_pid_is_recorded_with_its_stamp(self):
        self.assertEqual(self.runner_pid_after(common.DetachAmbiguous('x', pid=4444, created=123)), '4444 123')

    def test_success_without_a_stamp_is_ambiguous_and_records_no_pid(self):
        from twinlane import jobs
        jid = '20261008-000000-claude-amb1'
        with mock.patch.object(common, 'JOBS', os.path.join(self.tmp, 'jobs')), \
                mock.patch.object(common, 'spawn_detached_ex', return_value={'pid': 4444, 'created': None}):
            os.makedirs(jobs.job_dir(jid))
            common.write_json(jobs._p(jid, 'job.json'), {'id': jid})
            with mock.patch('sys.stdout'), mock.patch('sys.stderr'):
                rc = jobs._start_background(jid, {'engine': 'claude', 'profile': 'code'}, False)
            self.assertEqual(rc, common.EXIT_REFUSED)
            self.assertEqual(jobs._read_text(jobs._p(jid, 'runner.pid')), '')
            self.assertIsNone(common.read_json(jobs._p(jid, 'result.json')))


@unittest.skipUnless(common.IS_WIN, 'Windows DACL and WMI')
class HandoffNeverStaysOnDisk(_Tmp):
    """The hand-off file: owner-only DACL before content; deleted when the launcher returns; stale ones swept."""

    def test_owner_only_dacl_without_inherited_aces(self):
        path = os.path.join(self.tmp, 'runner.env.json')
        envhandoff.write(path, {'A': '1'})
        sddl = envhandoff.dacl_sddl(path)
        self.assertTrue(sddl.startswith('D:P'), sddl)  # protected: nothing inherited from the folder
        self.assertNotIn(';ID;', sddl)
        self.assertNotIn('ID)', sddl)
        self.assertEqual(sddl.count('('), 1, 'exactly one ACE')
        sid = envhandoff.user_sid()
        expected = f'D:P(A;;FA;;;{sid})'
        if sid.endswith('-500'):
            self.assertIn(sddl, (expected, 'D:P(A;;FA;;;LA)'))
        else:
            self.assertEqual(sddl, expected)

    def test_a_slow_child_never_gets_the_file(self):
        # Real WMI: the child waits 4 s before loading; the launcher gives up after 1 s and deletes the file.
        handoff = os.path.join(self.tmp, 'runner.env.json')
        slow = ('import sys, time; time.sleep(4); sys.path.insert(0, sys.argv[1]); from twinlane import envhandoff\n'
                'envhandoff.load(envhandoff.split(sys.argv[2:])[1]); time.sleep(60)\n')
        with mock.patch.object(common, 'HANDOFF_WAIT_S', 1):
            try:
                with self.assertRaises(common.DetachAmbiguous) as cm:
                    common._spawn_wmi([sys.executable, '-c', slow, ROOT], self.tmp, dict(os.environ), None, handoff)
            except common.DetachError as exc:
                if 'WMI unavailable' in str(exc) and 'Access denied' in str(exc):
                    self.skipTest('WMI denied by this Windows environment')
                raise
        self.assertFalse(cm.exception.runner_may_run)
        self.assertFalse(os.path.exists(handoff))
        with open(envhandoff.decided_path(handoff), encoding='ascii') as h:
            self.assertTrue(h.read().startswith('launcher '), 'the launcher must have decided')
        self.assertIsNotNone(cm.exception.pid)
        self.assertTrue(_wait(lambda: not common.pid_alive(cm.exception.pid)))

    def test_sweeper_removes_only_stale_files(self):
        jobs_dir = os.path.join(self.tmp, 'jobs')
        old = time.time() - 600
        paths = {}
        for jid, suffix, age in (('a', '', old), ('b', '', old), ('c', '', None)):
            os.makedirs(os.path.join(jobs_dir, jid))
            p = paths[jid] = os.path.join(jobs_dir, jid, 'runner.env.json' + suffix)
            with open(p, 'w') as h:
                h.write('{}')
            if age:
                os.utime(p, (age, age))
        self.assertEqual(envhandoff.sweep(jobs_dir, 'runner.env.json'), 2)
        self.assertEqual([os.path.exists(paths[j]) for j in 'abc'], [False, False, True])

    def test_twinlane_jobs_sweeps(self):
        home = os.path.join(self.tmp, 'home')
        p = os.path.join(home, 'jobs', '20261008-000000-claude-swp1', 'runner.env.json')
        os.makedirs(os.path.dirname(p))
        with open(p, 'w') as h:
            h.write('{}')
        os.utime(p, (time.time() - 600,) * 2)
        r = subprocess.run([sys.executable, os.path.join(ROOT, 'twinlane', 'cli.py'), 'jobs'], capture_output=True, text=True,
                           timeout=60, env=dict(os.environ, TWINLANE_HOME=home))
        self.assertEqual(r.returncode, 0, r.stderr[-1000:])
        self.assertFalse(os.path.exists(p))


sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import test_jobs  # noqa: E402  (JobsBase: isolated TWINLANE_HOME, fake engines)


class _JobHelpers(test_jobs.JobsBase):
    def make_job(self):
        from twinlane import jobs
        args = jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work])
        return jobs.create_job(jobs.plan_run(args, common.load_policy()))

    def run_job_cli(self, jid):
        from twinlane import jobs
        return self.cli('_job', jid)[0], jobs


@unittest.skipUnless(common.IS_WIN, 'Windows job objects and WMI')
class NestedJobChainBackgroundRun(test_jobs.JobsBase):
    """The fake-engine `twinlane run --bg` workflow from inside a nested app job chain: the app's tree is
    killed and its jobs close right after the launch, and the job still finishes (a simulation)."""

    def test_bg_job_finishes_after_tree_kill_and_job_close(self):
        self.pids = []
        os.environ['FAKE_SLEEP'] = '3'  # still running when the app tree goes
        common.write_json(os.path.join(common.STATE, 'meter.json'), test_jobs.meter_with())

        def cleanup():
            for pid, created in self.pids:
                if common.pid_alive(pid, created):
                    common.kill_tree(pid, created)
        self.addCleanup(cleanup)
        shell, r = _launch_chain(self, 'bg', 'forbid', '--brief', self.brief, '--cd', self.work, '--bg', '--json')
        self.assertEqual(r['rc'], 0, r['stdout'] + r['stderr'])
        launch = json.loads(r['stdout'])
        jid = launch['id']
        runner = jobs_read_runner(jid)
        self.pids.append(runner)  # before any assertion, so a failure still cleans it up
        self.assertEqual(launch['detach'], 'wmi')
        _tree_kill(shell)
        self.assertTrue(_wait(lambda: not common.pid_alive(r['control'], r['control_created'])),
                        'the control child outlived the tree kill and job close, so the test proves nothing')
        self.assertTrue(common.pid_alive(*runner), 'the runner died with the app tree')
        rc, out, _ = self.cli('wait', jid, '--timeout', '90')
        self.assertEqual(rc, 0, out)
        receipt = self.receipt(jid)
        self.assertEqual((receipt['status'], len(receipt['attempts'])), ('done', 1))
        self.assertEqual(common.read_json(os.path.join(common.JOBS, jid, 'job.json'))['detach']['mode'], 'wmi')
        self.assertFalse(os.path.exists(os.path.join(common.JOBS, jid, 'runner.env.json')))


def jobs_read_runner(jid):
    with open(os.path.join(common.JOBS, jid, 'runner.pid'), encoding='ascii') as h:
        pid, created = h.read().split()
    return int(pid), int(created)


class OneRunnerPerJob(_JobHelpers):
    """The runner lock: runner.lock makes a second runner for the same job exit without touching anything."""
    def test_live_lock_holder_means_exit_untouched(self):
        jid = self.make_job()
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (holder.kill(), holder.wait(10)))
        jd = os.path.join(common.JOBS, jid)
        common.write_text(os.path.join(jd, 'runner.lock'), f'{holder.pid} {common.process_created(holder.pid)}')
        with open(os.path.join(jd, 'status.json'), 'rb') as h:
            status_before = h.read()
        rc, jobs = self.run_job_cli(jid)
        self.assertEqual(rc, common.EXIT_REFUSED)
        with open(os.path.join(jd, 'status.json'), 'rb') as h:
            self.assertEqual(h.read(), status_before, 'the duplicate runner touched status.json')
        self.assertFalse(os.path.exists(os.path.join(jd, 'result.json')))
        self.assertFalse(os.path.exists(self.argv_out), 'the engine ran')
        self.assertTrue(os.path.exists(os.path.join(jd, 'runner.lock')), 'the live holder lost its lock')

    def test_lock_holder_with_an_unreadable_identity_is_not_taken_over(self):
        # access denied is not "dead". A second runner must not take the lock from it.
        jid = self.make_job()
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (holder.kill(), holder.wait(10)))
        jd = os.path.join(common.JOBS, jid)
        common.write_text(os.path.join(jd, 'runner.lock'), f'{holder.pid} {common.process_created(holder.pid)}')
        with test_jobs.unreadable_identity(holder.pid):
            rc, jobs = self.run_job_cli(jid)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertFalse(os.path.exists(self.argv_out), 'the engine ran')
        self.assertFalse(os.path.exists(os.path.join(jd, 'result.json')))
        self.assertTrue(os.path.exists(os.path.join(jd, 'runner.lock')), 'the holder lost its lock')

    def test_stale_lock_is_taken_over(self):
        jid = self.make_job()
        dead = subprocess.Popen([sys.executable, '-c', 'pass'])
        created = common.process_created(dead.pid)
        dead.wait(10)
        jd = os.path.join(common.JOBS, jid)
        common.write_text(os.path.join(jd, 'runner.lock'), f'{dead.pid} {created or 1}')
        rc, jobs = self.run_job_cli(jid)
        self.assertEqual(rc, 0)
        self.assertEqual(self.receipt(jid)['status'], 'done')
        self.assertFalse(os.path.exists(os.path.join(jd, 'runner.lock')), 'the lock is released at the end')

    def test_finished_job_is_never_rerun(self):
        jid = self.make_job()
        rc, jobs = self.run_job_cli(jid)
        self.assertEqual(rc, 0)
        os.remove(self.argv_out)
        rc, _ = self.run_job_cli(jid)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertFalse(os.path.exists(self.argv_out), 'a finished job ran its engine again')

    def test_two_concurrent_runners_one_engine_run(self):
        os.environ['FAKE_SLEEP'] = '3'
        common.write_json(os.path.join(common.STATE, 'meter.json'), test_jobs.meter_with())
        jid = self.make_job()
        argv = [sys.executable, os.path.join(ROOT, 'twinlane', 'cli.py'), '_job', jid]
        procs = [subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                 for _ in range(2)]
        outs = []
        for p in procs:
            try:
                outs.append((p.wait(timeout=120),) + p.communicate())
            finally:
                if p.poll() is None:
                    p.kill()
        rcs = sorted(o[0] for o in outs)
        self.assertEqual(rcs, [0, common.EXIT_REFUSED], outs)
        dup = next(o for o in outs if o[0] == common.EXIT_REFUSED)
        self.assertIn('already has', dup[2])
        r = self.receipt(jid)
        self.assertEqual((r['status'], len(r['attempts'])), ('done', 1))


class UnstampedLocks(_JobHelpers):
    """An unstamped runner.lock (legacy/corrupt: twinlane always stamps) is held while young and stale after
    the job's timeout + 10 min, so a reused pid cannot block the job forever."""

    def lock_live_unstamped(self, jid, age_s):
        holder = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (holder.kill(), holder.wait(10)))
        lock = os.path.join(common.JOBS, jid, 'runner.lock')
        common.write_text(lock, str(holder.pid))
        if age_s:
            t = time.time() - age_s
            os.utime(lock, (t, t))

    def timeout_s(self, jid):
        return common.read_json(os.path.join(common.JOBS, jid, 'job.json'))['primary']['timeout_s']

    def test_young_unstamped_lock_is_held(self):
        jid = self.make_job()
        self.lock_live_unstamped(jid, 0)
        rc, _ = self.run_job_cli(jid)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertFalse(os.path.exists(self.argv_out))

    def test_old_unstamped_lock_is_taken_over(self):
        jid = self.make_job()
        self.lock_live_unstamped(jid, self.timeout_s(jid) + 600 + 60)
        rc, _ = self.run_job_cli(jid)
        self.assertEqual(rc, 0)
        self.assertEqual(self.receipt(jid)['status'], 'done')


class StopIdentityAndEarlyStop(_JobHelpers):
    """`twinlane stop` never kills an unstamped pid, and a stop that lands before the runner is visible
    still launches nothing."""

    def test_unstamped_pid_is_never_killed(self):
        from twinlane import jobs
        jid = self.make_job()
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(lambda: (live.kill(), live.wait(10)))
        common.write_text(os.path.join(common.JOBS, jid, 'runner.pid'), str(live.pid))  # no stamp
        with mock.patch.object(common, 'kill_tree') as kill:
            rc, out, _ = self.cli('stop', jid)
        self.assertEqual(kill.call_count, 0, kill.call_args_list)
        self.assertEqual(rc, common.EXIT_FAILED)
        self.assertIn('identity unknown', out)
        self.assertIsNone(self.receipt(jid), 'the job must be left as is')
        self.assertIsNone(live.poll())
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.STOP_REQUESTED)))

    def test_unknown_runner_prevents_killing_a_stamped_child(self):
        from twinlane import jobs
        jid = self.make_job()
        common.write_text(jobs._p(jid, 'runner.pid'), '4444')
        common.write_json(jobs._p(jid, 'status.json'),
                          {'child_pid': 5555, 'child_created': 123})
        with mock.patch.object(common, 'pid_alive', return_value=True), \
                mock.patch.object(common, 'kill_tree') as kill:
            rc, out, _ = self.cli('stop', jid)
        self.assertEqual(rc, common.EXIT_FAILED)
        self.assertIn('identity unknown', out)
        kill.assert_not_called()
        self.assertIsNone(self.receipt(jid))

    def test_stop_requested_before_the_runner_launches_nothing(self):
        from twinlane import jobs
        jid = self.make_job()
        common.write_text(jobs._p(jid, jobs.STOP_REQUESTED), 'now')
        with mock.patch.object(jobs, 'acquire_slot', wraps=jobs.acquire_slot) as slot:
            rc, _ = self.run_job_cli(jid)
        self.assertEqual(slot.call_count, 0, 'a stopped job took a slot (status went running)')
        self.assertFalse(os.path.exists(self.argv_out), 'an engine started after a stop')
        r = self.receipt(jid)
        self.assertEqual((r['status'], r['attempts']), ('stopped', []))
        self.assertIn('stop requested before the runner started; nothing launched', r['notes'])
        self.assertEqual(jobs.job_status(jid), 'stopped')
        self.assertEqual(common.read_json(jobs._p(jid, 'status.json'))['status'], 'queued')

    def test_stop_requested_while_waiting_for_a_slot_launches_nothing(self):
        from twinlane import jobs
        jid = self.make_job()
        real = jobs.acquire_slot

        def slot_then_stop(job_id, *a, **kw):
            waited = real(job_id, *a, **kw)
            common.write_text(jobs._p(job_id, jobs.STOP_REQUESTED), 'now')  # stop lands after the claim check
            return waited
        with mock.patch.object(jobs, 'acquire_slot', side_effect=slot_then_stop):
            rc, _ = self.run_job_cli(jid)
        self.assertFalse(os.path.exists(self.argv_out), 'an engine started after a stop')
        r = self.receipt(jid)
        self.assertEqual(r['status'], 'stopped')
        self.assertIn('stop requested; attempt 1 was not launched', r['notes'])
        self.assertEqual(jobs.job_status(jid), 'stopped')

    def test_twinlane_stop_writes_the_request_first(self):
        from twinlane import jobs
        jid = self.make_job()
        rc, _, _ = self.cli('stop', jid)
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.STOP_REQUESTED)))


class ArbiterRace(_Tmp):
    """The child's claim and the launcher's abandon race on the O_EXCL arbiter: exactly one winner
    in every trial, and a losing child never reads the hand-off."""

    TRIALS = 300

    def test_child_without_a_creation_stamp_does_not_claim_or_read(self):
        handoff = os.path.join(self.tmp, 'runner.env.json')
        envhandoff.write(handoff, {'TEST_ONLY': 'x'})
        with mock.patch.object(envhandoff, '_self_created', return_value=None), \
                mock.patch.object(envhandoff, '_read') as read:
            with self.assertRaises(envhandoff.Lost):
                envhandoff.claim_and_read(handoff)
        read.assert_not_called()
        self.assertFalse(os.path.exists(envhandoff.decided_path(handoff)))
        self.assertTrue(os.path.exists(handoff))

    def test_exactly_one_winner_and_the_loser_never_reads(self):
        import threading
        reads = []
        real_read = envhandoff._read

        def counting_read(path):
            reads.append(path)
            return real_read(path)
        with mock.patch.object(envhandoff, '_read', side_effect=counting_read):
            for n in range(self.TRIALS):
                d = os.path.join(self.tmp, str(n))
                os.makedirs(d)
                handoff = os.path.join(d, 'runner.env.json')
                envhandoff.write(handoff, {'SECRET_LIKE': 'x'})
                go = threading.Barrier(2)
                res = {}

                def child():
                    go.wait()
                    try:
                        res['child'] = envhandoff.claim_and_read(handoff)
                    except envhandoff.Lost:
                        res['child'] = None

                def launcher():
                    go.wait()
                    res['launcher'] = envhandoff.decide(handoff, 'launcher 0')
                ts = [threading.Thread(target=child), threading.Thread(target=launcher)]
                for t in ts:
                    t.start()
                for t in ts:
                    t.join(10)
                child_won = res['child'] is not None
                self.assertNotEqual(child_won, res['launcher'], f'trial {n}: winners {res}')
                self.assertEqual(reads.count(handoff), 1 if child_won else 0, f'trial {n}')
                with open(envhandoff.decided_path(handoff), encoding='ascii') as h:
                    self.assertEqual(h.read().split()[0], 'child' if child_won else 'launcher')


class PythonPathParity(_Tmp):
    """The hand-off: the hand-off's PYTHONPATH lands on sys.path right after the script directory."""

    def test_pythonpath_entries_on_sys_path(self):
        a, b = os.path.join(self.tmp, 'pa'), os.path.join(self.tmp, 'pb')
        path, log = os.path.join(self.tmp, 'h.json'), os.path.join(self.tmp, 'log.txt')
        envhandoff.write(path, dict(os.environ, PYTHONPATH=os.pathsep.join([a, b])), log)
        r = subprocess.run([sys.executable, '-c',
                            'import sys; sys.path.insert(0, sys.argv[1]); from twinlane import envhandoff\n'
                            'envhandoff.load(sys.argv[2]); print(repr(sys.path[1:3]))', ROOT, path],
                           capture_output=True, text=True, timeout=60,
                           env={k: v for k, v in os.environ.items() if k != 'PYTHONPATH'})
        self.assertEqual(r.returncode, 0, r.stderr[-1000:])
        with open(log, encoding='utf-8') as h:
            self.assertEqual(h.read().strip(), repr([a, b]))


if __name__ == '__main__':
    unittest.main()
