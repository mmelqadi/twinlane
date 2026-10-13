"""twinlane land: FIFO queue, single-integrator lock, gate, fast-forward landing and the exact ledger range."""
import io
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
import git_fixtures  # noqa: E402
from twinlane import common, land  # noqa: E402

PY = sys.executable
GIT_IDENTITY = {'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.org',
                'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.org'}

HEARTBEAT_GATE = r'''
import json, os, sys, time
holder_path, out_path = sys.argv[1], sys.argv[2]

def read():
    for _ in range(100):
        try:
            with open(holder_path, encoding='utf-8') as handle:
                return json.load(handle)
        except (OSError, ValueError):
            time.sleep(0.02)
    raise SystemExit('holder unreadable')

time.sleep(0.5)
first = read()
time.sleep(2.6)
second = read()
with open(out_path, 'w', encoding='utf-8') as handle:
    json.dump({'first': first, 'second': second}, handle)
'''

RECORD_GATE = r'''
import json, os, subprocess, sys
head = subprocess.run(['git', 'rev-parse', 'HEAD'], capture_output=True, text=True).stdout.strip()
with open(sys.argv[1], 'w', encoding='utf-8') as handle:
    json.dump({'head': head, 'env_head': os.environ.get('TWINLANE_LAND_HEAD'),
               'env_base': os.environ.get('TWINLANE_LAND_BASE'), 'cwd': os.getcwd()}, handle)
'''

LAND_COMMAND = r'''
import json, os, subprocess, sys
repo, worktree, branch, slug, target, base, source, out = sys.argv[1:9]
with open(out, 'w', encoding='utf-8') as handle:
    json.dump(sys.argv[1:9], handle)
subprocess.run(['git', '-C', repo, 'merge', '-q', '--ff-only', branch], check=True)
with open(os.path.join(repo, 'carried.txt'), 'w', encoding='utf-8') as handle:
    handle.write('carried by the project land script\n')
subprocess.run(['git', '-C', repo, 'add', 'carried.txt'], check=True)
subprocess.run(['git', '-C', repo, 'commit', '-q', '-m', 'carry'], check=True)
'''


@unittest.skipUnless(git_fixtures.git_available(), 'git is required')
class Land(unittest.TestCase):
    def setUp(self):
        self.ws = git_fixtures.Workspace(prefix='twinlane-land-')
        self.addCleanup(self.ws.cleanup)
        self.repo = self.ws.repo('app', files={'shared.txt': 'shared\n', 'other.txt': 'other\n'})
        cfg_dir = tempfile.mkdtemp(prefix='land-cfg-', dir=os.environ['TWINLANE_HOME'])
        self.config_path = os.path.join(cfg_dir, 'config.json')
        env = dict(GIT_IDENTITY, TWINLANE_CONFIG=self.config_path)
        patcher = mock.patch.dict(os.environ, env)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('TWINLANE_ENGINE_JOB', None)
        os.environ.pop('TWINLANE_SESSION', None)
        self.configure()
        self.paths = land.Paths(self.repo)
        self.scripts = self.ws.path('scripts')
        os.makedirs(self.scripts)

    # --- helpers -------------------------------------------------------------

    def configure(self, **land_cfg):
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump({'projects': {self.repo: {'land': land_cfg}}}, handle)

    def script(self, name, text):
        path = os.path.join(self.scripts, name)
        with open(path, 'w', encoding='utf-8') as handle:
            handle.write(text)
        return path

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = land.cli(list(argv) + ['--repo', self.repo])
        return rc, out.getvalue(), err.getvalue()

    def feature(self, branch, files=None, base=None):
        """A worktree on a new branch with one commit per file; returns (worktree, [commit ids])."""
        wt = self.ws.worktree(self.repo, branch, base=base)
        commits = [self.ws.commit(wt, rel, text) for rel, text in (files or {branch + '.txt': branch + '\n'}).items()]
        return wt, commits

    def enqueue(self, slug, wt):
        rc, out, err = self.cli('enqueue', slug, '--worktree', wt)
        self.assertEqual(rc, 0, err)
        return out

    def head(self, cwd=None, ref='HEAD'):
        return git_fixtures.git(cwd or self.repo, 'rev-parse', ref)

    def queue_slugs(self):
        return [e['slug'] for e in land.read_queue(self.paths)['entries']]

    def events(self):
        return land.read_ledger(self.paths)

    def write_holder(self, **fields):
        stamp = common.iso(common.now())
        record = {'slug': 'held', 'pid': os.getpid(), 'created': common.process_created(os.getpid()),
                  'session': 's-1', 'host': land.host_name(), 'token': 'abc', 'acquired_at': stamp,
                  'heartbeat_at': stamp, 'beats': 3, 'phase': 'gate', 'child': None}
        record.update(fields)
        os.makedirs(self.paths.dir, exist_ok=True)
        common.write_json(self.paths.holder, record)
        with open(self.paths.holder, 'rb') as handle:
            return handle.read()

    def holder_bytes(self):
        with open(self.paths.holder, 'rb') as handle:
            return handle.read()

    def snapshot(self, *worktrees):
        files = []
        for base, _dirs, names in os.walk(os.path.join(common.STATE, 'land')):
            for name in names:
                st = os.stat(os.path.join(base, name))
                files.append((os.path.join(base, name), st.st_size, st.st_mtime_ns))
        repo_state = [git_fixtures.git(self.repo, 'for-each-ref', '--format=%(refname) %(objectname)')]
        for where in (self.repo,) + worktrees:
            repo_state.append(git_fixtures.git(where, 'status', '--porcelain=v1', '--untracked-files=all'))
            repo_state.append(self.head(where))
        return sorted(files), repo_state

    # --- queue ---------------------------------------------------------------

    def test_enqueue_is_fifo_and_idempotent_per_slug(self):
        wt_a, _ = self.feature('feat-a')
        wt_b, _ = self.feature('feat-b')
        self.assertIn('queued a', self.enqueue('a', wt_a))
        self.enqueue('b', wt_b)
        self.assertIn('already queued a', self.enqueue('a', wt_a))
        self.assertEqual(self.queue_slugs(), ['a', 'b'])
        self.assertEqual(self.cli('enqueue', 'a', '--worktree', wt_b)[0], common.EXIT_REFUSED)   # slug taken
        self.assertEqual(self.cli('enqueue', 'c', '--worktree', wt_a)[0], common.EXIT_REFUSED)   # branch queued
        self.assertEqual(self.cli('enqueue', '../x', '--worktree', wt_a)[0], common.EXIT_USAGE)
        self.assertEqual(self.cli('enqueue', 'm', '--worktree', self.repo)[0], common.EXIT_REFUSED)
        self.assertEqual(self.queue_slugs(), ['a', 'b'])
        rc, out, _ = self.cli('next', '--json')
        self.assertEqual((rc, json.loads(out)['head']['slug']), (0, 'a'))
        rc, out, _ = self.cli('status', '--json')
        st = json.loads(out)
        self.assertEqual([e['slug'] for e in st['queue']], ['a', 'b'])
        self.assertEqual(st['queue'][0]['branch'], 'feat-a')
        self.assertEqual(common.canon_path(st['queue'][0]['worktree']), common.canon_path(wt_a))
        self.assertEqual(st['queue'][0]['by']['pid'], os.getpid())
        self.assertEqual(st['holder_state']['state'], 'free')
        self.assertEqual(self.cli('dequeue', 'a')[0], 0)
        self.enqueue('a', wt_a)
        self.assertEqual(self.queue_slugs(), ['b', 'a'])                                         # back of the line

    def test_concurrent_enqueue_threads(self):
        trees = [(f's{i}', self.feature(f'feat-{i}')[0]) for i in range(8)]
        calls = trees + trees                      # every slug twice, all at once
        barrier = threading.Barrier(len(calls))
        results, errors = [], []
        real_read = common.read_json

        def slow_read(*args, **kwargs):            # widen the read-modify-write window
            value = real_read(*args, **kwargs)
            time.sleep(0.01)
            return value

        def worker(slug, wt):
            try:
                barrier.wait(timeout=60)
                results.append(land.enqueue(slug, wt, self.repo))
            except Exception as e:  # noqa: BLE001
                errors.append(repr(e))

        with mock.patch.object(common, 'read_json', side_effect=slow_read):
            threads = [threading.Thread(target=worker, args=call) for call in calls]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=120)
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 16)
        self.assertEqual(sum(1 for r in results if r['queued']), 8)
        slugs = self.queue_slugs()
        self.assertEqual(sorted(slugs), sorted(s for s, _ in trees))
        self.assertEqual(len(slugs), len(set(slugs)))
        with open(self.paths.queue, encoding='utf-8') as handle:
            self.assertEqual(json.load(handle)['version'], 1)

    # --- the lock ------------------------------------------------------------

    def test_run_refuses_when_not_head(self):
        wt_a, _ = self.feature('feat-a')
        wt_b, _ = self.feature('feat-b')
        self.enqueue('a', wt_a)
        self.enqueue('b', wt_b)
        before = self.head()
        rc, _, err = self.cli('run', 'b')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('not the queue head (a is)', err)
        rc, _, err = self.cli('run', 'nope')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('not queued', err)
        self.assertFalse(os.path.exists(self.paths.holder))
        self.assertEqual(self.events(), [])
        self.assertEqual(self.head(), before)
        self.assertEqual(self.queue_slugs(), ['a', 'b'])
        for slug in ('a', 'b'):
            self.cli('dequeue', slug)
        rc, _, err = self.cli('run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('queue is empty', err)

    def test_live_holder_reported_not_stolen(self):
        wt, _ = self.feature('feat-a')
        self.enqueue('a', wt)
        before = self.head()
        held = self.write_holder()
        rc, _, err = self.cli('run')
        self.assertEqual(rc, common.EXIT_RUNNING)
        self.assertIn(f'pid {os.getpid()}', err)
        self.assertIn('alive', err)
        rc, out, _ = self.cli('status', '--json')
        self.assertEqual(json.loads(out)['holder_state']['state'], 'alive')
        # A stale heartbeat on a live process is "unresponsive": still never taken or broken.
        held = self.write_holder(heartbeat_at=common.iso(common.parse_time(time.time() - 3600)))
        rc, out, _ = self.cli('status', '--json')
        self.assertEqual(json.loads(out)['holder_state']['state'], 'unresponsive')
        for argv in (('run',), ('run', '--break', '--reason', 'looks stuck'), ('release',),
                     ('release', '--break', '--reason', 'looks stuck')):
            with self.subTest(argv=argv):
                rc, _, err = self.cli(*argv)
                self.assertEqual(rc, common.EXIT_RUNNING, err)
                self.assertIn('unresponsive', err)
                self.assertEqual(self.holder_bytes(), held)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.events(), [])
        self.assertEqual(self.queue_slugs(), ['a'])

    def test_dead_holder_break_needs_reason(self):
        wt, commits = self.feature('feat-a')
        self.enqueue('a', wt)
        proc = subprocess.Popen([PY, '-c', 'import time; time.sleep(60)'])
        created = common.process_created(proc.pid)
        self.assertIsNotNone(created)
        proc.kill()
        proc.wait()
        held = self.write_holder(pid=proc.pid, created=created, slug='crashed')
        rc, _, err = self.cli('run')
        self.assertEqual(rc, common.EXIT_RUNNING)
        self.assertIn('dead', err)
        self.assertIn('--break --reason', err)
        self.assertEqual(self.cli('release')[0], common.EXIT_RUNNING)
        self.assertEqual(self.cli('release', '--break')[0], common.EXIT_USAGE)
        self.assertEqual(self.cli('release', '--break', '--reason', '   ')[0], common.EXIT_USAGE)
        self.assertEqual(self.cli('run', '--break')[0], common.EXIT_USAGE)
        self.assertEqual(self.holder_bytes(), held)
        self.assertEqual(self.events(), [])
        rc, out, err = self.cli('release', '--break', '--reason', 'the lead crashed')
        self.assertEqual(rc, 0, err)
        self.assertIn('broke the lock', out)
        self.assertFalse(os.path.exists(self.paths.holder))
        (event,) = self.events()
        self.assertEqual((event['event'], event['reason'], event['state']), ('broken-lock', 'the lead crashed', 'dead'))
        self.assertEqual((event['broken_holder']['pid'], event['broken_holder']['slug']), (proc.pid, 'crashed'))
        self.assertEqual(event['holder']['pid'], os.getpid())
        rc, _, err = self.cli('run')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.head(), commits[-1])

    def test_unknown_holder_never_broken(self):
        wt, _ = self.feature('feat-a')
        self.enqueue('a', wt)
        before = self.head()
        cases = {
            'another host': (lambda: self.write_holder(host='some-other-host'), None),
            'unreadable identity': (lambda: self.write_holder(), common.UNKNOWN),
            'orphaned gate child': (lambda: self.write_holder(pid=999999, created=1, child={
                'pid': os.getpid(), 'created': common.process_created(os.getpid()), 'what': 'gate'}), None),
        }
        for name, (make, forced) in cases.items():
            with self.subTest(case=name):
                held = make()
                patch = mock.patch.object(common, 'pid_state', return_value=forced) if forced else mock.patch.object(
                    common, 'pid_state', wraps=common.pid_state)
                with patch:
                    rc, out, _ = self.cli('status', '--json')
                    self.assertEqual(json.loads(out)['holder_state']['state'], 'unknown')
                    for argv in (('release', '--break', '--reason', 'x'), ('run', '--break', '--reason', 'x'),
                                 ('run', '--dry-run', '--break', '--reason', 'x')):
                        rc, _, err = self.cli(*argv)
                        self.assertEqual(rc, common.EXIT_RUNNING, (argv, err))
                        self.assertIn('unknown', err)
                self.assertEqual(self.holder_bytes(), held)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.events(), [])

    # --- the gate ------------------------------------------------------------

    def test_heartbeat_updates_during_gate(self):
        wt, commits = self.feature('feat-a')
        self.enqueue('a', wt)
        out_path = self.ws.path('beats.json')
        gate = self.script('beat_gate.py', HEARTBEAT_GATE)
        self.configure(heartbeat_s=1, gate=[PY, gate, self.paths.holder, out_path])
        rc, _, err = self.cli('run')
        self.assertEqual(rc, 0, err)
        with open(out_path, encoding='utf-8') as handle:
            seen = json.load(handle)
        first, second = seen['first'], seen['second']
        self.assertEqual((first['phase'], second['phase']), ('gate', 'gate'))
        self.assertEqual(first['child']['what'], 'gate')
        self.assertEqual(first['pid'], os.getpid())
        self.assertGreaterEqual(second['beats'] - first['beats'], 2)
        self.assertGreater(common.parse_time(second['heartbeat_at']), common.parse_time(first['acquired_at']))
        self.assertFalse(os.path.exists(self.paths.holder))
        self.assertEqual(self.head(), commits[-1])

    def test_gate_failure_lands_nothing(self):
        wt, _ = self.feature('feat-a')
        self.enqueue('a', wt)
        before = self.head()
        self.configure(gate=[PY, '-c', "import sys; print('gate says no'); sys.exit(3)"])
        rc, _, err = self.cli('run')
        self.assertEqual(rc, common.EXIT_FAILED, err)
        self.assertIn('gate failed (rc 3)', err)
        self.assertEqual(self.head(), before)
        self.assertEqual(self.queue_slugs(), ['a'])
        self.assertFalse(os.path.exists(self.paths.holder))
        (event,) = self.events()
        self.assertEqual((event['event'], event['gate']['rc']), ('failed', 3))
        with open(event['gate']['log'], encoding='utf-8') as handle:
            self.assertIn('gate says no', handle.read())
        self.assertNotIn('to', event)

    def test_gate_that_moves_head_or_dirties_tree_refused(self):
        wt, commits = self.feature('feat-a')
        self.enqueue('a', wt)
        before = self.head()
        gates = {
            'HEAD moved': "import subprocess; subprocess.run(['git', 'commit', '-q', '--allow-empty', '-m', 'x'], "
                          "check=True)",
            'untracked': "open('gate-artifact.txt', 'w').write('x')",
            'modified': "open('feat-a.txt', 'a').write('more')",
        }
        for name, code in gates.items():
            with self.subTest(gate=name):
                self.configure(gate=[PY, '-c', code])
                rc, _, err = self.cli('run')
                self.assertEqual(rc, common.EXIT_REFUSED, err)
                self.assertIn('the gate changed what it judged', err)
                self.assertIn('HEAD moved' if name == 'HEAD moved' else 'the tree changed', err)
                self.assertEqual(self.head(), before)
                self.assertEqual(self.queue_slugs(), ['a'])
                self.assertFalse(os.path.exists(self.paths.holder))
                self.assertEqual(self.events()[-1]['event'], 'refused')
                # put the worktree back for the next case
                git_fixtures.git(wt, 'reset', '-q', '--hard', commits[-1])
                git_fixtures.git(wt, 'clean', '-q', '-f')
        self.assertEqual(len(self.events()), 3)

    # --- landing -------------------------------------------------------------

    def test_ff_only_land_records_exact_range(self):
        wt, commits = self.feature('feat-a', {'a1.txt': 'one\n', 'a2.txt': 'two\n'})
        self.enqueue('a', wt)
        base = self.head()
        self.configure(gate=[PY, '-c', 'pass'])
        rc, out, err = self.cli('run', 'a', '--json')
        self.assertEqual(rc, 0, err)
        event = json.loads(out)
        self.assertEqual((event['event'], event['slug'], event['branch'], event['target']),
                         ('landed', 'a', 'feat-a', 'main'))
        self.assertEqual((event['from'], event['to']), (base, commits[-1]))
        self.assertEqual(event['commits'], commits)
        self.assertEqual((event['merges'], event['mode'], event['tree_matches']), ([], 'ff', True))
        self.assertEqual((event['gate']['rc'], event['gate']['argv']), (0, [PY, '-c', 'pass']))
        self.assertEqual(self.head(), commits[-1])
        self.assertTrue(os.path.isfile(os.path.join(self.repo, 'a2.txt')))
        self.assertEqual(self.queue_slugs(), [])
        self.assertFalse(os.path.exists(self.paths.holder))
        (recorded,) = land.landings(self.repo, 'a')
        self.assertEqual((recorded['from'], recorded['to'], recorded['commits']), (base, commits[-1], commits))
        rc, out, _ = self.cli('log', 'a', '--json')
        self.assertEqual([e['event'] for e in json.loads(out)['events']], ['landed'])

    def test_target_moved_without_rebase_refuses(self):
        wt, commits = self.feature('feat-a')
        self.enqueue('a', wt)
        moved = self.ws.commit(self.repo, 'main-only.txt', 'peer\n')
        rc, _, err = self.cli('run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('not a fast-forward', err)
        self.assertEqual(self.head(), moved)
        self.assertEqual(self.head(wt), commits[-1])
        self.assertEqual(self.queue_slugs(), ['a'])
        self.assertFalse(os.path.exists(self.paths.holder))
        self.assertEqual(self.events()[-1]['event'], 'refused')

    def test_rebase_refused_when_range_has_merges(self):
        wt, _ = self.feature('feat-a')
        git_fixtures.git(wt, 'checkout', '-q', '-b', 'side')
        self.ws.commit(wt, 'side.txt', 'side\n')
        git_fixtures.git(wt, 'checkout', '-q', 'feat-a')
        self.ws.commit(wt, 'feat-a-2.txt', 'more\n')
        git_fixtures.git(wt, 'merge', '-q', '--no-ff', 'side', '-m', 'merge side')
        tip = self.head(wt)
        self.enqueue('a', wt)
        self.ws.commit(self.repo, 'main-only.txt', 'peer\n')
        moved = self.head()
        rc, _, err = self.cli('run', '--rebase')
        self.assertEqual(rc, common.EXIT_REFUSED, err)
        self.assertIn('merge commits', err)
        self.assertEqual((self.head(wt), self.head()), (tip, moved))
        # A project land command that rebases is refused on merges too, even before the target moves.
        git_fixtures.git(self.repo, 'reset', '-q', '--hard', 'HEAD~1')
        self.configure(command=[PY, '-c', 'pass'], command_rebases=True)
        rc, _, err = self.cli('run')
        self.assertEqual(rc, common.EXIT_REFUSED, err)
        self.assertIn('flatten', err)
        self.assertEqual(self.head(wt), tip)
        self.assertEqual(self.queue_slugs(), ['a'])

    def test_rebase_allowed_for_linear_range(self):
        wt, commits = self.feature('feat-a', {'a1.txt': 'one\n', 'a2.txt': 'two\n'})
        self.enqueue('a', wt)
        moved = self.ws.commit(self.repo, 'main-only.txt', 'peer\n')
        seen_path = self.ws.path('gate-saw.json')
        gate = self.script('record_gate.py', RECORD_GATE)
        self.configure(gate=[PY, gate, seen_path])
        rc, out, err = self.cli('run', '--rebase', '--json')
        self.assertEqual(rc, 0, err)
        event = json.loads(out)
        self.assertEqual((event['mode'], event['from'], event['rebased_from']), ('rebase+ff', moved, commits[-1]))
        self.assertEqual(len(event['commits']), 2)
        self.assertNotEqual(event['commits'], commits)
        self.assertEqual(self.head(), event['to'])
        self.assertEqual(self.head(wt), event['to'])
        self.assertEqual(git_fixtures.git(self.repo, 'rev-list', '--reverse', f'{moved}..HEAD').split(),
                         event['commits'])
        with open(seen_path, encoding='utf-8') as handle:
            seen = json.load(handle)
        self.assertEqual((seen['head'], seen['env_head'], seen['env_base']), (event['to'], event['to'], moved))
        self.assertEqual(common.canon_path(seen['cwd']), common.canon_path(wt))
        for name in ('a1.txt', 'a2.txt', 'main-only.txt'):
            self.assertTrue(os.path.isfile(os.path.join(self.repo, name)), name)

    def test_main_checkout_peer_changes_refuse(self):
        wt, commits = self.feature('feat-a', {'shared.txt': 'from the branch\n', 'new.txt': 'new\n'})
        self.enqueue('a', wt)
        before = self.head()
        shared = os.path.join(self.repo, 'shared.txt')
        cases = [
            ('peer edit', lambda: self.ws.write(self.repo, 'shared.txt', 'peer work in progress\n'),
             lambda: git_fixtures.git(self.repo, 'checkout', '-q', '--', 'shared.txt')),
            ('staged change', lambda: (self.ws.write(self.repo, 'staged.txt', 'x\n'),
                                       git_fixtures.git(self.repo, 'add', 'staged.txt')),
             lambda: (git_fixtures.git(self.repo, 'rm', '-q', '--cached', 'staged.txt'),
                      os.remove(os.path.join(self.repo, 'staged.txt')))),
            ('untracked new.txt would be overwritten', lambda: self.ws.write(self.repo, 'new.txt', 'peer\n'),
             lambda: os.remove(os.path.join(self.repo, 'new.txt'))),
        ]
        for name, make, undo in cases:
            with self.subTest(case=name):
                make()
                rc, _, err = self.cli('run')
                self.assertEqual(rc, common.EXIT_REFUSED, err)
                self.assertIn(name, err)
                self.assertEqual(self.head(), before)
                undo()
        with open(shared, encoding='utf-8') as handle:
            self.assertEqual(handle.read(), 'shared\n')
        # A peer edit OFF the landing paths stays where it is and does not block the land.
        self.ws.write(self.repo, 'other.txt', 'peer edit elsewhere\n')
        rc, _, err = self.cli('run')
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.head(), commits[-1])
        with open(os.path.join(self.repo, 'other.txt'), encoding='utf-8') as handle:
            self.assertEqual(handle.read(), 'peer edit elsewhere\n')
        self.assertEqual([e['event'] for e in self.events()], ['refused'] * 3 + ['landed'])

    def test_project_land_command_argv_and_range_recomputed(self):
        wt, commits = self.feature('feat-a')
        self.enqueue('one', wt)
        base = self.head()
        out_path = self.ws.path('argv.json')
        script = self.script('land_cmd.py', LAND_COMMAND)
        self.configure(gate=[PY, '-c', 'pass'], command=[PY, script, '{repo}', '{worktree}', '{branch}', '{slug}',
                                                          '{target}', '{base}', '{source_commit}', out_path])
        rc, out, err = self.cli('run', '--json')
        self.assertEqual(rc, 0, err)
        event = json.loads(out)
        with open(out_path, encoding='utf-8') as handle:
            got = json.load(handle)
        self.assertEqual(common.canon_path(got[0]), common.canon_path(self.repo))
        self.assertEqual(common.canon_path(got[1]), common.canon_path(wt))
        self.assertEqual(got[2:7], ['feat-a', 'one', 'main', base, commits[-1]])
        self.assertEqual(common.canon_path(got[7]), common.canon_path(out_path))
        carry = self.head()
        self.assertNotEqual(carry, commits[-1])
        self.assertEqual((event['mode'], event['from'], event['to']), ('command', base, carry))
        self.assertEqual(event['commits'], [commits[-1], carry])
        self.assertEqual(event['command']['rc'], 0)
        self.assertTrue(event['contains_gated_commit'])
        self.assertFalse(event['tree_matches'])
        self.assertEqual(event['differs_from_gated'], ['carried.txt'])
        self.assertTrue(any('differs from the gated tree' in n for n in event['notes']))
        self.assertEqual(self.queue_slugs(), [])

    # --- refusals and dry run ------------------------------------------------

    def test_land_refused_inside_worker(self):
        wt, _ = self.feature('feat-a')
        self.enqueue('a', wt)
        before = self.snapshot(wt)
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'job-1'}):
            for argv in (('run',), ('run', '--dry-run'), ('release',), ('release', '--break', '--reason', 'x'),
                         ('dequeue', 'a')):
                with self.subTest(argv=argv):
                    rc, _, err = self.cli(*argv)
                    self.assertEqual(rc, common.EXIT_REFUSED)
                    self.assertIn('refused inside a twinlane worker', err)
        self.assertEqual(self.snapshot(wt), before)
        self.assertEqual(self.queue_slugs(), ['a'])
        self.assertFalse(os.path.exists(self.paths.ledger))

    def test_dry_run_writes_nothing(self):
        wt, commits = self.feature('feat-a')
        self.enqueue('a', wt)
        marker = self.ws.path('gate-ran')
        self.configure(gate=[PY, '-c', f'open({marker!r}, "w").write("x")'])
        before = self.snapshot(wt)
        rc, out, err = self.cli('run', '--dry-run')
        self.assertEqual(rc, 0, err)
        self.assertIn('dry run: would land a (feat-a) on main by ff: 1 commit(s)', out)
        self.assertIn('gate: ', out)
        self.assertEqual(self.snapshot(wt), before)
        self.ws.commit(self.repo, 'main-only.txt', 'peer\n')     # the target moves
        before = self.snapshot(wt)
        rc, _, err = self.cli('run', '--dry-run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('not a fast-forward', err)
        rc, out, err = self.cli('run', '--dry-run', '--rebase', '--json')
        self.assertEqual(rc, 0, err)
        plan = json.loads(out)
        self.assertEqual((plan['mode'], plan['commits']), ('rebase+ff', commits))
        self.assertEqual(self.snapshot(wt), before)
        self.assertFalse(os.path.exists(marker))
        self.assertFalse(os.path.exists(self.paths.holder))
        self.assertFalse(os.path.exists(self.paths.ledger))
        # A project that was never used gets no state directory from a dry run.
        fresh = self.ws.repo('fresh')
        fresh_dir = land.Paths(fresh).dir
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = land.cli(['run', '--dry-run', '--repo', fresh])
        self.assertEqual(rc, common.EXIT_REFUSED, err.getvalue())
        self.assertFalse(os.path.exists(fresh_dir))


if __name__ == '__main__':
    unittest.main()
