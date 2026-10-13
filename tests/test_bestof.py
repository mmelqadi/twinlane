"""twinlane run --best-of / compare against real temporary git repositories.

The job launcher is replaced: no engine is ever started. Each fake launch writes a finished job folder, so
`compare` reads a real receipt."""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
import git_fixtures as gf  # noqa: E402
from twinlane import bestof, common, jobs  # noqa: E402

REAL_LOAD_POLICY = common.load_policy


def meter_with(claude='ok', codex='ok'):
    return {'read_at': common.iso(common.now()),
            'claude': {'status': claude, 'max_pct': 10, 'next_reset': '2026-10-08T20:00:00Z', 'age_s': 5,
                       'extra_usage_enabled': False},
            'codex': {'status': codex, 'max_pct': 10, 'next_reset': '2026-10-09T08:00:00Z', 'age_s': 5}}


def fake_decide(kind, meter, policy, urgent, engine):
    """A stand-in for route.decide: the forced engine, or a refusal when its meter is not usable."""
    if meter[engine]['status'] not in ('ok', 'caution', 'handoff'):
        return {'engine': None, 'refused': True, 'reason': f'forced {engine} is {meter[engine]["status"]}; no swap',
                'wait_until': '2026-10-08T20:00:00Z'}
    return {'engine': engine, 'refused': False, 'reason': 'test route', 'fallback_used': False,
            'model': policy['claude']['models']['default'] if engine == 'claude' else None,
            'effort': policy['claude']['effort_by_kind'].get(kind) if engine == 'claude' else None,
            'tier': policy['codex']['tier_by_kind'].get(kind) if engine == 'codex' else None,
            'profile': 'code' if kind == 'code' else 'intel', 'wait_until': None}


@unittest.skipUnless(gf.git_available(), 'git is not installed')
class BestOfBase(unittest.TestCase):
    def setUp(self):
        self.ws = gf.Workspace(prefix='twinlane-bo-')
        self.addCleanup(self.ws.cleanup)
        saved = (common.STATE, common.JOBS, common.BATONS)
        hooks = (jobs._read_meter, jobs._decide)

        def restore():
            common.STATE, common.JOBS, common.BATONS = saved
            jobs._read_meter, jobs._decide = hooks
        self.addCleanup(restore)
        home = self.ws.path('home')
        common.STATE = os.path.join(home, 'state')
        common.JOBS = os.path.join(home, 'jobs')
        common.BATONS = os.path.join(home, 'batons')
        for d in (common.STATE, common.JOBS, common.BATONS):
            os.makedirs(d)
        self.repo = self.ws.repo('app')
        self.main = common.real_path(self.repo)
        self.first = gf.git(self.repo, 'rev-parse', 'HEAD')
        self.ws.commit(self.repo, 'later.txt', 'moved on\n')   # HEAD is now past `first`
        self.meter = meter_with()
        jobs._read_meter = lambda policy: self.meter
        jobs._decide = fake_decide
        self.brief = self.ws.write(self.ws.root, 'brief.md', 'Add a greeting function.\n')
        self.test_cmd = None
        self.launched = []
        self.job_seq = 0
        self.launch_rc = 0
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)
        patcher = mock.patch.object(bestof, 'launch', self.fake_launch)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.object(common, 'load_policy', self.policy)
        patcher.start()
        self.addCleanup(patcher.stop)
        patcher = mock.patch.dict(os.environ)
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('TWINLANE_ENGINE_JOB', None)

    # -- fixtures
    def policy(self):
        policy = REAL_LOAD_POLICY()
        entry = {}
        if self.test_cmd:
            entry['test'] = self.test_cmd
        policy['projects'] = {self.repo: entry}
        return policy

    def fake_launch(self, argv):
        """Record the argv and leave a finished job behind, as a real background job eventually would."""
        self.launched.append(list(argv))
        if self.launch_rc:
            return self.launch_rc, {'refused': True, 'reason': 'fake refusal'}, ''
        self.job_seq += 1
        jid = f'20261013-120000-{argv[argv.index("--engine") + 1]}-{self.job_seq:04d}'
        os.makedirs(os.path.join(common.JOBS, jid))
        engine = argv[argv.index('--engine') + 1]
        common.write_json(os.path.join(common.JOBS, jid, 'job.json'), {'id': jid, 'cwd': argv[argv.index('--cd') + 1]})
        common.write_json(os.path.join(common.JOBS, jid, 'result.json'), {
            'id': jid, 'status': 'done', 'engine': engine, 'served_models': [f'{engine}-model'],
            'requested': {'model': f'{engine}-model'}, 'duration_s': 12.5, 'num_turns': 3,
            'touched_files': ['greeting.txt'], 'read_only_violation': None})
        return 0, {'id': jid}, ''

    def run_cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = bestof.cli(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def best_of(self, engines='claude,codex', *extra, write=True, kind=None, json_out=True):
        argv = ['run', '--best-of', engines, '--brief', self.brief, '--cd', self.repo]
        if write:
            argv.append('--write')
        if kind:
            argv += ['--kind', kind]
        if json_out:
            argv.append('--json')
        return self.run_cli(*argv, *extra)

    def worktree_count(self):
        return len([ln for ln in gf.git(self.repo, 'worktree', 'list', '--porcelain').splitlines()
                    if ln.startswith('worktree ')])

    def wt_root(self):
        return os.path.join(os.path.dirname(self.main), 'app-twinlane-wt')


class TestParse(unittest.TestCase):
    def test_parse_contestants_and_cap(self):
        got = bestof.parse_contestants('claude,codex@sol-high', 3)
        self.assertEqual([(c['engine'], c['model'], c['tier'], c['label']) for c in got],
                         [('claude', None, None, 'claude'), ('codex', None, 'sol-high', 'codex')])
        got = bestof.parse_contestants('claude:model-a, claude:model-b ,codex:gpt-x', 3)
        self.assertEqual([c['label'] for c in got], ['claude', 'claude-2', 'codex'])
        self.assertEqual(got[1]['model'], 'model-b')
        self.assertEqual(got[2]['model'], 'gpt-x')
        for bad in ('claude', 'claude,claude', 'claude,codex,codex@luna,claude:x', 'claude,gemini', 'claude@fast,codex',
                    'claude,', ',codex', 'claude:,codex', 'codex@,claude'):
            with self.assertRaises(bestof.BestOfError, msg=bad):
                bestof.parse_contestants(bad, 3)
        with self.assertRaises(bestof.BestOfError):
            bestof.parse_contestants('claude,codex,codex@luna', 2)   # over the cap
        self.assertEqual(len(bestof.parse_contestants('claude,codex,codex@luna', 3)), 3)
        self.assertEqual(bestof.max_contestants({'best_of': {'max_contestants': 2}}), 2)
        self.assertEqual(bestof.max_contestants({}), 3)
        for bad in (0, True, 'x', 1.5):
            with self.assertRaises(bestof.BestOfError):
                bestof.max_contestants({'best_of': {'max_contestants': bad}})


class TestRun(BestOfBase):
    def test_dry_run_creates_nothing(self):
        rc, out, err = self.best_of('claude,codex', '--dry-run', '--base', self.first)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertTrue(data['dry_run'])
        self.assertEqual([c['label'] for c in data['contestants']], ['claude', 'codex'])
        self.assertTrue(all(c.get('argv') for c in data['contestants']))
        self.assertEqual(self.launched, [])
        self.assertEqual(self.worktree_count(), 1)
        self.assertFalse(os.path.exists(self.wt_root()))
        self.assertFalse(os.path.exists(bestof.groups_dir()))
        self.assertEqual(os.listdir(common.JOBS), [])
        rc, out, err = self.best_of('claude,codex', '--dry-run', json_out=False)
        self.assertEqual(rc, 0, err)
        self.assertIn('nothing created', out)
        self.assertIn('N times the usage', out)

    def test_refused_contestant_reported_others_run(self):
        self.meter = meter_with(codex='stop')
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        by = {c['label']: c for c in data['contestants']}
        self.assertTrue(by['claude']['job'])
        self.assertIsNone(by['claude']['refused'])
        self.assertIn('codex is stop', by['codex']['refused'])
        self.assertNotIn('job', by['codex'])
        self.assertEqual(len(self.launched), 1)
        self.assertEqual(self.worktree_count(), 2)   # only the started contestant got a worktree
        # the money guard: Codex on paid credits is refused too, even though its status reads ok
        self.meter = meter_with()
        self.meter['codex']['on_credits'] = True
        self.launched.clear()
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 0, err)
        by = {c['label']: c for c in json.loads(out)['contestants']}
        self.assertIn('paid credits', by['codex']['refused'])
        self.assertEqual(len(self.launched), 1)
        # a launcher refusal after preflight is reported the same way
        self.meter = meter_with()
        self.launch_rc = 3
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 3)

    def test_all_refused_exits_3(self):
        self.meter = meter_with(claude='stop', codex='stop')
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 3)
        data = json.loads(out)
        self.assertEqual(data['started'], 0)
        self.assertTrue(all(c['refused'] for c in data['contestants']))
        self.assertEqual(self.launched, [])
        self.assertEqual(self.worktree_count(), 1)
        self.assertFalse(os.path.exists(bestof.groups_dir()))
        rc, out, err = self.best_of('claude,codex', json_out=False)
        self.assertEqual(rc, 3)
        self.assertIn('every contestant was refused', err)

    def test_one_worktree_per_contestant_same_base(self):
        rc, out, err = self.best_of('claude,codex', '--base', self.first)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data['base'], self.first)
        self.assertEqual(self.worktree_count(), 3)
        paths = set()
        for c in data['contestants']:
            self.assertEqual(c['branch'], f"twinlane/bo-{data['id']}-{c['label']}")
            self.assertTrue(os.path.isdir(c['worktree']))
            self.assertEqual(os.path.dirname(c['worktree']), self.wt_root())
            self.assertEqual(gf.git(c['worktree'], 'rev-parse', 'HEAD'), self.first)
            self.assertEqual(gf.git(c['worktree'], 'rev-parse', '--abbrev-ref', 'HEAD'), c['branch'])
            self.assertFalse(c['shared'])
            paths.add(os.path.normcase(c['worktree']))
        self.assertEqual(len(paths), 2)
        # each job was started in its own worktree, as a write job, from the one brief
        for argv in self.launched:
            self.assertIn('--write', argv)
            self.assertIn('--bg', argv)
            self.assertEqual(argv[argv.index('--brief') + 1], os.path.abspath(self.brief))
        self.assertEqual({os.path.normcase(a[a.index('--cd') + 1]) for a in self.launched}, paths)
        # with no --base the base is the current HEAD
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 0, err)
        self.assertEqual(json.loads(out)['base'], gf.git(self.repo, 'rev-parse', 'HEAD'))

    def test_intel_shares_cwd(self):
        rc, out, err = self.best_of('claude,codex', write=False)
        self.assertEqual(rc, 0, err)
        data = json.loads(out)
        self.assertEqual(data['kind'], 'intel')
        self.assertEqual(self.worktree_count(), 1)
        self.assertFalse(os.path.exists(self.wt_root()))
        for c in data['contestants']:
            self.assertTrue(c['shared'])
            self.assertIsNone(c['worktree'])
            self.assertIsNone(c['branch'])
        for argv in self.launched:
            self.assertEqual(os.path.normcase(argv[argv.index('--cd') + 1]), os.path.normcase(os.path.abspath(self.repo)))
            self.assertNotIn('--write', argv)
        # a code kind without --write is refused per contestant rather than silently made read-only
        self.launched.clear()
        rc, out, err = self.best_of('claude,codex', write=False, kind='code')
        self.assertEqual(rc, 3)
        self.assertIn('needs --write', json.loads(out)['contestants'][0]['refused'])

    def test_contestants_never_fail_over(self):
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 0, err)
        engines = []
        for argv in self.launched:
            self.assertIn('--no-failover', argv)
            self.assertNotIn('--failover', argv)
            engines.append(argv[argv.index('--engine') + 1])
        self.assertEqual(engines, ['claude', 'codex'])
        # a router that answers with the OTHER engine is a refusal, never a swap
        self.launched.clear()

        def swapping(kind, meter, policy, urgent, engine):
            d = fake_decide(kind, meter, policy, urgent, 'codex')
            return d if engine == 'codex' else dict(d, engine='codex')
        jobs._decide = swapping
        rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, 0, err)
        by = {c['label']: c for c in json.loads(out)['contestants']}
        self.assertIn('claude not usable', by['claude']['refused'])
        self.assertEqual([a[a.index('--engine') + 1] for a in self.launched], ['codex'])

    def test_refused_inside_worker(self):
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': '20261013-120000-codex-x'}):
            rc, out, err = self.best_of('claude,codex')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(self.launched, [])
        self.assertEqual(self.worktree_count(), 1)


class TestCompare(BestOfBase):
    def started(self, *extra):
        rc, out, err = self.best_of('claude,codex', '--base', self.first, *extra)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_compare_diffstat_receipts_tests(self):
        marker_cmd = [sys.executable, '-c', 'import os, sys; sys.exit(0 if os.path.exists("greeting.txt") else 1)']
        self.test_cmd = marker_cmd
        group = self.started()
        by = {c['label']: c for c in group['contestants']}
        # claude edits a tracked file and adds a new one; codex does nothing
        self.ws.write(by['claude']['worktree'], 'README.md', 'fixture\nhello\n')
        self.ws.write(by['claude']['worktree'], 'greeting.txt', 'hi\n')
        rc, out, err = self.run_cli('compare', group['id'], '--json', '--test')
        self.assertEqual(rc, 0, err)
        rep = json.loads(out)
        rows = {r['label']: r for r in rep['contestants']}
        self.assertEqual(rows['claude']['status'], 'done')
        self.assertEqual(rows['claude']['served_models'], ['claude-model'])
        self.assertEqual(rows['claude']['duration_s'], 12.5)
        self.assertEqual(rows['claude']['turns'], 3)
        self.assertEqual(rows['claude']['touched_files'], ['greeting.txt'])
        d = rows['claude']['diff']
        self.assertEqual((d['files'], d['added'], d['deleted'], d['untracked']), (1, 1, 0, 1))
        d = rows['codex']['diff']
        self.assertEqual((d['files'], d['added'], d['deleted'], d['untracked']), (0, 0, 0, 0))
        self.assertTrue(rows['claude']['test']['ran'])
        self.assertEqual(rows['claude']['test']['rc'], 0)
        self.assertEqual(rows['codex']['test']['rc'], 1)
        # the text and markdown renderings carry the same facts, and nothing is picked
        rc, out, err = self.run_cli('compare', group['id'])
        self.assertEqual(rc, 0, err)
        self.assertIn('+1 -0, 1 untracked', out)
        self.assertIn('nothing was picked for you', out)
        self.assertIn(f"twinlane compare {group['id']} --discard", out)
        rc, md, err = self.run_cli('compare', group['id'], '--md')
        self.assertIn('## claude (claude)', md)
        # a unique part of the id finds the group
        rc, out, err = self.run_cli('compare', group['id'][-4:], '--json')
        self.assertEqual(rc, 0, err)
        # --wait returns at once when every job is finished
        rc, out, err = self.run_cli('compare', group['id'], '--wait', '--timeout', '5', '--json')
        self.assertEqual(rc, 0, err)

    def test_compare_test_only_with_flag(self):
        marker = self.ws.path('ran-marker.txt')
        self.test_cmd = [sys.executable, '-c', f'open({marker!r}, "w").close()']
        group = self.started()
        rc, out, err = self.run_cli('compare', group['id'])
        self.assertEqual(rc, 0, err)
        self.assertFalse(os.path.exists(marker))
        self.assertIn('tests were not run', out)
        rc, out, err = self.run_cli('compare', group['id'], '--test')
        self.assertEqual(rc, 0, err)
        self.assertTrue(os.path.exists(marker))
        # no configured command: --test says so and runs nothing
        os.remove(marker)
        self.test_cmd = None
        rc, out, err = self.run_cli('compare', group['id'], '--test', '--json')
        rows = json.loads(out)['contestants']
        self.assertFalse(rows[0]['test']['ran'])
        self.assertIn('no test command configured', rows[0]['test']['error'])
        self.assertFalse(os.path.exists(marker))
        # a worker may read a comparison but not run tests or discard
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': '20261013-120000-codex-x'}):
            self.assertEqual(self.run_cli('compare', group['id'])[0], 0)
            self.assertEqual(self.run_cli('compare', group['id'], '--test')[0], common.EXIT_REFUSED)
            self.assertEqual(self.run_cli('compare', group['id'], '--discard')[0], common.EXIT_REFUSED)

    def test_group_record_round_trip(self):
        group = self.started()
        stored = bestof.load_group(group['id'])
        self.assertEqual(stored['id'], group['id'])
        self.assertEqual(stored['schema'], bestof.SCHEMA)
        self.assertEqual(stored['base'], self.first)
        self.assertEqual(stored['kind'], 'code')
        self.assertEqual(stored['repo'], self.main)
        self.assertEqual(stored['brief_src'], os.path.abspath(self.brief))
        import hashlib
        with open(self.brief, 'rb') as h:
            self.assertEqual(stored['brief_sha256'], hashlib.sha256(h.read()).hexdigest())
        self.assertEqual([c['label'] for c in stored['contestants']], ['claude', 'codex'])
        for c in stored['contestants']:
            for key in ('engine', 'model', 'job', 'worktree', 'branch', 'refused'):
                self.assertIn(key, c)
        self.assertEqual(stored, json.loads(json.dumps(stored)))
        bestof.save_group(stored)
        self.assertEqual(bestof.load_group(group['id']), stored)
        self.assertEqual(bestof.load_group(group['id'][-4:])['id'], group['id'])
        for bad in ('../x', '', 'no-such-group', 'a b'):
            with self.assertRaises(bestof.BestOfError, msg=bad):
                bestof.load_group(bad)
        two = dict(stored, id=stored['id'][:-1] + 'z')
        bestof.save_group(two)
        with self.assertRaises(bestof.BestOfError):
            bestof.load_group(stored['id'][:-1])   # ambiguous
        self.assertEqual(bestof.load_group(stored['id'])['id'], stored['id'])   # an exact id always wins

    def test_discard_unmerged_only_group_branches(self):
        group = self.started()
        by = {c['label']: c for c in group['contestants']}
        other = self.ws.worktree(self.repo, 'unrelated-feature', parent=self.ws.path('elsewhere'))
        # codex made a commit of its own (unmerged); claude produced nothing
        self.ws.commit(by['codex']['worktree'], 'greeting.txt', 'hi\n', 'codex greeting')
        # dry run first: nothing changes
        rc, out, err = self.run_cli('compare', group['id'], '--discard')
        self.assertEqual(rc, 0, err)
        self.assertIn('dry run: 1 would be removed', out)
        self.assertTrue(os.path.isdir(by['claude']['worktree']))
        # now for real
        rc, out, err = self.run_cli('compare', group['id'], '--discard', '--yes')
        self.assertEqual(rc, 0, err)
        branches = gf.git(self.repo, 'branch', '--format=%(refname:short)').split()
        self.assertFalse(os.path.exists(by['claude']['worktree']))
        self.assertNotIn(by['claude']['branch'], branches)
        # the unmerged contestant is untouched, and the exact manual commands are printed (never run)
        self.assertTrue(os.path.isdir(by['codex']['worktree']))
        self.assertIn(by['codex']['branch'], branches)
        self.assertIn(f'branch -D {by["codex"]["branch"]}', out)
        self.assertIn('worktree remove', out)
        self.assertNotIn(f'branch -D {by["claude"]["branch"]}', out)
        # a branch that is not the group's is never looked at
        self.assertTrue(os.path.isdir(other))
        self.assertIn('unrelated-feature', branches)
        self.assertNotIn('unrelated-feature', out)
        self.assertEqual(gf.git(by['codex']['worktree'], 'log', '-1', '--format=%s'), 'codex greeting')
        # a worktree that moved to another branch is left alone, even when it holds nothing
        gf.git(other, 'branch', 'swapped')
        gf.git(by['codex']['worktree'], 'checkout', '-q', 'swapped')
        rc, out, err = self.run_cli('compare', group['id'], '--discard', '--yes')
        self.assertTrue(os.path.isdir(by['codex']['worktree']))
        self.assertIn('not the group', out)


if __name__ == '__main__':
    unittest.main()
