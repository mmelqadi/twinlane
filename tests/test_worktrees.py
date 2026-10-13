"""twinlane wt doctor / wt prune against real temporary repositories, worktrees and directory links.

Every repository, link target and twinlane state folder lives in a temporary workspace. prune deletes
things, so each test proves what it removed AND what it left alone."""
import io
import json
import os
import sys
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
import git_fixtures as gf  # noqa: E402
from twinlane import common, projects, worktrees  # noqa: E402

HOUR = 3600


def run_cli(*argv):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        rc = worktrees.cli(list(argv))
    return rc, out.getvalue(), err.getvalue()


@unittest.skipUnless(gf.git_available(), 'git is not installed')
class WtBase(unittest.TestCase):
    def setUp(self):
        self.ws = gf.Workspace(prefix='twinlane-wt-')
        self.addCleanup(self.ws.cleanup)
        saved = (common.STATE, common.JOBS, common.BATONS)

        def restore():
            common.STATE, common.JOBS, common.BATONS = saved
        self.addCleanup(restore)
        home = self.ws.path('home')
        common.STATE = os.path.join(home, 'state')
        common.JOBS = os.path.join(home, 'jobs')
        common.BATONS = os.path.join(home, 'batons')
        for d in (common.STATE, common.JOBS, common.BATONS):
            os.makedirs(d)
        self.repo = self.ws.repo('app')
        self.main = common.real_path(self.repo)
        old_cwd = os.getcwd()
        self.addCleanup(os.chdir, old_cwd)  # runs before the workspace cleanup (a cwd inside it blocks rmtree)

    # -- fixtures
    def policy(self, land=None, **wt):
        policy = common.load_policy()
        entry = {'wt': dict({'prune_min_age_s': HOUR}, **wt)}
        if land:
            entry['land'] = land
        policy['projects'] = {self.repo: entry}
        return policy

    def age(self, wt, seconds):
        """Backdate the worktree's last git activity (what prune's minimum age reads)."""
        gd = worktrees._gitdir(wt)
        stamp = time.time() - seconds
        for rel in (('logs', 'HEAD'), ('index',), ('HEAD',)):
            path = os.path.join(gd, *rel)
            if os.path.exists(path):
                os.utime(path, (stamp, stamp))

    def merged(self, branch, age=2 * HOUR, files=None):
        """A worktree with one commit of its own, merged into main, idle for `age` seconds."""
        wt = self.ws.worktree(self.repo, branch)
        for rel, text in (files or {branch + '.txt': branch + '\n'}).items():
            self.ws.write(wt, rel, text)
        gf.git(wt, 'add', '-A')
        gf.git(wt, 'commit', '-q', '-m', 'work on ' + branch)
        gf.git(self.repo, 'merge', '-q', '--no-edit', branch)
        if age:
            self.age(wt, age)
        return wt

    def branch_exists(self, branch):
        rc, _, _ = gf.git(self.repo, 'rev-parse', '--verify', '-q', 'refs/heads/' + branch, check=False)
        return rc == 0

    def rows(self, result):
        return {r['branch']: r for r in (result.get('worktrees') or result.get('rows'))}

    def write_job(self, jid, cwd, status='running', done=False):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d, exist_ok=True)
        if cwd is not None:
            common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'cwd': cwd})
        common.write_json(os.path.join(d, 'status.json'), {'status': status, 'pid': os.getpid()})
        if done:
            common.write_json(os.path.join(d, 'result.json'), {'status': 'done'})


class Doctor(WtBase):
    def test_doctor_reports_missing_and_empty_deps_dirs(self):
        full = self.ws.worktree(self.repo, 'full')
        os.makedirs(os.path.join(full, 'vendor', 'pkg'))
        os.makedirs(os.path.join(full, 'node_modules'))           # present but empty
        bare = self.ws.worktree(self.repo, 'bare')                 # neither folder
        report = worktrees.doctor(self.repo, self.policy(deps_dirs=['vendor', 'node_modules']))
        rows = self.rows(report)
        self.assertEqual(rows['full']['deps'], [{'dir': 'node_modules', 'problem': 'empty'}])
        self.assertEqual(rows['bare']['deps'], [{'dir': 'vendor', 'problem': 'missing'},
                                                {'dir': 'node_modules', 'problem': 'missing'}])
        self.assertEqual(len(rows['main']['deps']), 2)             # the main checkout is checked too
        text = worktrees.render_doctor(report)
        self.assertIn('deps: vendor missing, node_modules missing', text)
        self.assertIn('deps: node_modules empty', text)
        self.assertTrue(os.path.isdir(bare))                       # read-only

    def test_doctor_reports_dangling_link_deps(self):
        wt = self.ws.worktree(self.repo, 'linked')
        shared = self.ws.path('shared-vendor')
        os.makedirs(os.path.join(shared, 'pkg'))
        empty = self.ws.path('empty-modules')
        os.makedirs(empty)
        gf.make_link(os.path.join(wt, 'vendor'), shared)
        gf.make_link(os.path.join(wt, 'node_modules'), empty)
        healthy = worktrees.doctor(self.repo, self.policy(deps_dirs=['vendor', 'node_modules']))
        problems = {d['dir']: d['problem'] for d in self.rows(healthy)['linked']['deps']}
        self.assertNotIn('vendor', problems)                       # a live link to a full folder is fine
        self.assertRegex(problems['node_modules'], r'^empty (junction|symlink) target$')
        gf.rmtree(shared)                                          # the link now dangles
        report = worktrees.doctor(self.repo, self.policy(deps_dirs=['vendor', 'node_modules']))
        problems = {d['dir']: d['problem'] for d in self.rows(report)['linked']['deps']}
        self.assertRegex(problems['vendor'], r'^dangling (junction|symlink)$')
        self.assertTrue(gf.is_link(os.path.join(wt, 'vendor')))   # reported, never touched

    def test_doctor_longpath_report(self):
        rel = 'deep/a-rather-long-file-name.txt'
        self.ws.commit(self.repo, rel, 'x\n')
        wt = self.ws.worktree(self.repo, 'long-feature')
        gf.git(self.repo, 'config', 'core.longpaths', 'true')
        limit = len(os.path.abspath(wt)) + 1 + len(rel)            # exactly this file, in the worktree only
        self.assertGreater(len(os.path.abspath(wt)), len(os.path.abspath(self.repo)))
        with mock.patch.object(worktrees, 'LONG_PATH', limit), \
                mock.patch.object(worktrees, '_registry_long_paths', return_value=0):
            report = worktrees.doctor(self.repo, self.policy())
        lp = report['long_paths']
        self.assertTrue(lp['core_longpaths'])
        self.assertEqual(lp['registry_long_paths'], 0)
        self.assertEqual(lp['threshold'], limit)
        rows = self.rows(report)
        self.assertEqual(rows['long-feature']['long_paths'], 1)
        self.assertEqual(rows['main']['long_paths'], 0)
        self.assertEqual(lp['risk'], common.IS_WIN)                 # registry off: tools without long paths fail
        with mock.patch.object(worktrees, 'LONG_PATH', limit), \
                mock.patch.object(worktrees, '_registry_long_paths', return_value=1):
            self.assertFalse(worktrees.doctor(self.repo, self.policy())['long_paths']['risk'])
        self.assertIn('core.longpaths repo=true', worktrees.render_doctor(report))

    def test_doctor_classifies_stale_unlanded_merged_empty(self):
        self.merged('merged-one')
        old = self.merged('merged-old', age=10 * 86400)
        unlanded = self.ws.worktree(self.repo, 'unlanded')
        self.ws.commit(unlanded, 'u.txt', 'u\n')
        self.ws.worktree(self.repo, 'empty-one')                   # no commit of its own: NOT merged
        landed = self.ws.worktree(self.repo, 'landed-empty')       # no commit, but land recorded it
        tip = gf.git(self.repo, 'rev-parse', 'refs/heads/landed-empty')
        ledger = os.path.join(common.STATE, 'land', projects.repo_key(self.main), 'ledger.jsonl')
        os.makedirs(os.path.dirname(ledger))
        with open(ledger, 'w', encoding='utf-8') as h:
            h.write(json.dumps({'event': 'landed', 'slug': 'x', 'branch': 'landed-empty', 'to': tip,
                                'commits': []}) + '\n')
        gone = self.ws.worktree(self.repo, 'gone')
        gf.rmtree(gone)
        report = worktrees.doctor(self.repo, self.policy())
        rows = self.rows(report)
        self.assertEqual(report['target'], 'main')
        self.assertEqual(rows['main']['state'], 'main')
        self.assertEqual(rows['merged-one']['state'], 'merged')
        self.assertTrue(rows['merged-one']['own_commits'])
        self.assertFalse(rows['merged-one']['stale'])
        self.assertEqual(rows['merged-old']['state'], 'merged')
        self.assertTrue(rows['merged-old']['stale'])
        self.assertEqual(rows['unlanded']['state'], 'unlanded')
        self.assertEqual(rows['empty-one']['state'], 'empty')
        self.assertFalse(rows['empty-one']['own_commits'])
        self.assertEqual(rows['landed-empty']['state'], 'merged')
        self.assertTrue(rows['landed-empty']['landed'])
        self.assertEqual(rows['gone']['state'], 'missing')
        self.assertEqual(report['counts']['merged'], 3)
        self.assertEqual(report['counts']['stale'], 1)
        self.assertTrue(os.path.isdir(old))
        text = worktrees.render_doctor(report)
        self.assertIn('merged stale  merged-old', text)
        self.assertIn('unlanded 1', text)


class Prune(WtBase):
    def test_prune_dry_run_by_default(self):
        wt = self.merged('done', age=2 * 86400)                    # older than the packaged 1-day minimum
        rc, out, err = run_cli('prune', '--repo', self.repo)       # no project config: packaged defaults
        self.assertEqual(rc, 0, err)
        self.assertIn('would remove', out)
        self.assertIn('dry run: 1 would be removed; nothing changed', out)
        res = worktrees.prune(self.repo, policy=self.policy())
        self.assertTrue(res['dry_run'])
        self.assertEqual([r['action'] for r in res['rows']], ['would remove'])
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(self.branch_exists('done'))
        self.assertFalse(os.path.exists(os.path.join(common.STATE, 'wt')))   # a dry run writes nothing

    def test_prune_removes_only_merged_clean_old(self):
        keep_main = gf.git(self.repo, 'rev-parse', 'HEAD')
        old = self.merged('old')
        young = self.merged('young', age=0)
        dirty = self.merged('dirty')
        self.ws.write(dirty, 'scratch.txt', 'not committed\n')     # untracked counts as dirty
        staged = self.merged('staged')
        self.ws.write(staged, 'staged.txt', 's\n')
        gf.git(staged, 'add', 'staged.txt')
        self.age(staged, 2 * HOUR)
        unlanded = self.ws.worktree(self.repo, 'unlanded')
        self.ws.commit(unlanded, 'u.txt', 'u\n')
        self.age(unlanded, 2 * HOUR)
        empty = self.ws.worktree(self.repo, 'empty')               # merged by ancestry, but no commits of its own
        self.age(empty, 2 * HOUR)
        res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        rows = self.rows(res)
        self.assertEqual(rows['old']['action'], 'removed')
        self.assertTrue(rows['old']['branch_deleted'])
        self.assertFalse(os.path.exists(old))
        self.assertFalse(self.branch_exists('old'))
        for branch, path, why in (('young', young, 'active'), ('dirty', dirty, 'uncommitted'),
                                  ('staged', staged, 'uncommitted'), ('unlanded', unlanded, 'not merged'),
                                  ('empty', empty, '--include-empty')):
            with self.subTest(branch=branch):
                self.assertEqual(rows[branch]['action'], 'skip')
                self.assertIn(why, '; '.join(rows[branch]['blockers']))
                self.assertTrue(os.path.isdir(path))
                self.assertTrue(self.branch_exists(branch))
        self.assertTrue(os.path.isfile(os.path.join(dirty, 'scratch.txt')))
        self.assertEqual(res['removed'], 1)
        res = worktrees.prune(self.repo, yes=True, include_empty=True, policy=self.policy())
        self.assertEqual(self.rows(res)['empty']['action'], 'removed')
        self.assertFalse(os.path.exists(empty))
        self.assertEqual(res['removed'], 1)                        # still only that one: nothing else qualified
        self.assertTrue(os.path.isdir(self.repo))
        self.assertNotEqual(gf.git(self.repo, 'rev-parse', 'HEAD'), '')
        gf.git(self.repo, 'merge-base', '--is-ancestor', keep_main, 'HEAD')   # main history intact
        ledger = os.path.join(projects.state_dir('wt', self.main), 'prune.jsonl')
        with open(ledger, encoding='utf-8') as h:
            events = [json.loads(line) for line in h]
        self.assertEqual([(e['event'], e['branch']) for e in events], [('removed', 'old'), ('removed', 'empty')])
        self.assertTrue(all(e['tip'] for e in events))             # the tip is kept: a branch can be recreated

    def test_prune_never_live_job_worktree(self):
        busy = self.merged('busy')
        free = self.merged('free')
        self.write_job('20261013-100000-claude-live', os.path.join(busy, 'src'))
        self.write_job('20261013-090000-codex-done', free, done=True)            # finished: does not hold it
        res = worktrees.prune(self.repo, policy=self.policy())
        rows = self.rows(res)
        self.assertEqual(rows['busy']['action'], 'skip')
        self.assertIn('job 20261013-100000-claude-live (running) runs there', rows['busy']['blockers'])
        self.assertEqual(rows['free']['action'], 'would remove')
        # A live job whose cwd cannot be read could be anywhere: nothing is removed.
        self.write_job('20261013-110000-claude-blind', None)
        res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        self.assertEqual({r['action'] for r in res['rows']}, {'skip'})
        self.assertIn('has no readable cwd', '; '.join(self.rows(res)['free']['blockers']))
        self.assertTrue(os.path.isdir(busy) and os.path.isdir(free))

    def test_prune_never_baton_or_queued_worktree(self):
        held = self.merged('held')
        queued = self.merged('queued')
        by_branch = self.merged('by-branch')
        free = self.merged('free')
        common.write_json(os.path.join(common.BATONS, 'task-a.json'),
                          {'slug': 'task-a', 'repo': {'root': self.repo, 'worktree': held.replace('\\', '/')}})
        queue = os.path.join(projects.state_dir('land', self.main, create=True), 'queue.json')
        common.write_json(queue, {'version': 1, 'repo': self.main, 'entries': [
            {'slug': 'q1', 'worktree': queued, 'branch': 'queued'},
            {'slug': 'q2', 'worktree': self.ws.path('elsewhere'), 'branch': 'by-branch'}]})
        res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        rows = self.rows(res)
        self.assertIn('baton task-a names it', rows['held']['blockers'])
        self.assertIn('land queue entry q1 names it', rows['queued']['blockers'])
        self.assertIn('land queue entry q2 names its branch', rows['by-branch']['blockers'])
        self.assertEqual(rows['free']['action'], 'removed')
        for path in (held, queued, by_branch):
            self.assertTrue(os.path.isdir(path))
        # A baton that cannot be read might name any worktree: nothing more is removed.
        with open(os.path.join(common.BATONS, 'broken.json'), 'w', encoding='utf-8') as h:
            h.write('{not json')
        common.write_json(os.path.join(common.BATONS, 'task-a.json'), {'slug': 'task-a', 'repo': {}})
        res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        self.assertIn('baton broken is unreadable', '; '.join(self.rows(res)['held']['blockers']))
        self.assertEqual(res['removed'], 0)
        self.assertTrue(os.path.isdir(held))

    def test_prune_unlinks_inner_links_first_and_target_survives(self):
        shared = self.ws.path('shared')
        os.makedirs(shared)
        self.ws.write(shared, 'keep.txt', 'precious\n')
        cache = self.ws.path('cache-store')
        os.makedirs(os.path.join(cache, 'objects'))
        self.ws.write(cache, 'objects/blob.bin', 'precious too\n')
        wt = self.merged('with-links', files={'.gitignore': 'vendor\ncache\n', 'lib/code.txt': 'c\n'})
        gf.make_link(os.path.join(wt, 'vendor'), shared)
        gf.make_link(os.path.join(wt, 'lib', 'cache'), cache)
        found = {os.path.relpath(lk['path'], wt) for lk in worktrees.links_inside(wt)}
        self.assertEqual(found, {'vendor', os.path.join('lib', 'cache')})
        dry = worktrees.render_prune(worktrees.prune(self.repo, policy=self.policy()))
        self.assertIn('2 inner link(s) unlinked first', dry)
        self.assertTrue(gf.is_link(os.path.join(wt, 'vendor')))   # the dry run unlinked nothing
        events = []
        real_unlink, real_git = worktrees._unlink, common.git

        def unlink(link):
            events.append(('unlink', os.path.basename(link['path'])))
            return real_unlink(link)

        def git(cwd, *args, **kw):
            if args[:2] == ('worktree', 'remove'):
                events.append(('git worktree remove',))
            return real_git(cwd, *args, **kw)
        with mock.patch.object(worktrees, '_unlink', side_effect=unlink), \
                mock.patch.object(common, 'git', side_effect=git):
            res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        row = self.rows(res)['with-links']
        self.assertEqual(row['action'], 'removed', row.get('error') or row.get('blockers'))
        self.assertEqual(sorted(events[:2]), [('unlink', 'cache'), ('unlink', 'vendor')])
        self.assertEqual(events[2:], [('git worktree remove',)])  # links went before git touched the folder
        self.assertEqual(len(row['unlinked']), 2)
        self.assertFalse(os.path.exists(wt))
        with open(os.path.join(shared, 'keep.txt'), encoding='utf-8') as h:
            self.assertEqual(h.read(), 'precious\n')
        self.assertTrue(os.path.isfile(os.path.join(cache, 'objects', 'blob.bin')))

    def test_prune_refuses_link_worktree(self):
        wt = self.merged('moved')
        elsewhere = self.ws.path('other-disk')
        os.makedirs(elsewhere)
        real = os.path.join(elsewhere, 'moved')
        os.rename(wt, real)
        gf.make_link(wt, real)                                      # git still lists the old path, now a link
        res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        row = self.rows(res)['moved']
        self.assertEqual(row['action'], 'skip')
        self.assertRegex('; '.join(row['blockers']), r'itself a (junction|symlink)')
        self.assertTrue(gf.is_link(wt))
        self.assertTrue(os.path.isfile(os.path.join(real, 'moved.txt')))
        self.assertTrue(self.branch_exists('moved'))

    def test_prune_refuses_inbound_link(self):
        wt = self.merged('target-of-link', files={'data/table.csv': 'a,b\n'})
        links = self.ws.path('links')
        os.makedirs(links)
        gf.make_link(os.path.join(links, 'data'), os.path.join(wt, 'data'))
        res = worktrees.prune(self.repo, yes=True, policy=self.policy(link_roots=[links]))
        row = self.rows(res)['target-of-link']
        self.assertEqual(row['action'], 'skip')
        self.assertIn('inbound link', '; '.join(row['blockers']))
        self.assertTrue(os.path.isfile(os.path.join(wt, 'data', 'table.csv')))
        # Without the configured link root the same worktree goes (the root is what is scanned).
        res = worktrees.prune(self.repo, policy=self.policy())
        self.assertEqual(self.rows(res)['target-of-link']['action'], 'would remove')

    def test_prune_refuses_current_dir(self):
        wt = self.merged('here')
        os.chdir(wt)
        res = worktrees.prune(self.repo, yes=True, policy=self.policy())
        row = self.rows(res)['here']
        self.assertEqual(row['action'], 'skip')
        self.assertIn('it is the current directory', row['blockers'])
        self.assertTrue(os.path.isdir(wt))

    def test_prune_safe_branch_delete_only(self):
        calls = []
        real_run = common.run

        def spy(argv, *a, **kw):
            calls.append(list(argv))
            return real_run(argv, *a, **kw)
        # 1. merged into main (the main checkout's branch): git branch -d deletes it.
        self.merged('ok')
        # 2. target configured as `release`: merged there but not into main, so -d refuses and it is KEPT.
        gf.git(self.repo, 'branch', 'release')
        wt2 = worktrees.add(self.repo, 'only-release', self.ws.path('app-wt', 'only-release'), 'release')
        self.ws.commit(wt2, 'r.txt', 'r\n')
        gf.git(self.repo, 'update-ref', 'refs/heads/release', gf.git(wt2, 'rev-parse', 'HEAD'))
        self.age(wt2, 2 * HOUR)
        with mock.patch.object(common, 'run', side_effect=spy):
            first = worktrees.prune(self.repo, yes=True, policy=self.policy())
            second = worktrees.prune(self.repo, yes=True, policy=self.policy(land={'target': 'release'}))
        self.assertTrue(self.rows(first)['ok']['branch_deleted'])
        self.assertFalse(self.branch_exists('ok'))
        row = self.rows(second)['only-release']
        self.assertEqual(row['action'], 'removed')
        self.assertFalse(row['branch_deleted'])
        self.assertIn('branch kept', row['branch_note'])
        self.assertTrue(self.branch_exists('only-release'))       # never escalated to -D
        git_calls = [c for c in calls if c and c[0] == 'git']
        deletes = [c for c in git_calls if 'branch' in c]
        self.assertEqual([c[c.index('branch'):] for c in deletes],
                         [['branch', '-d', '--', 'ok'], ['branch', '-d', '--', 'only-release']])
        for c in git_calls:
            self.assertFalse({'-D', '--force', '-f', '--delete', '-ff'} & set(c), c)

    def test_prune_uses_project_remove_command(self):
        wt = self.merged('custom')
        record = self.ws.path('remove-calls.jsonl')
        script = self.ws.write(self.ws.root, 'remove_wt.py',
                               'import json, subprocess, sys\n'
                               'with open(sys.argv[1], "a", encoding="utf-8") as h:' '\n'
                               '    h.write(json.dumps(sys.argv[2:]) + "\\' 'n")\n'
                               'sys.exit(subprocess.call(["git", "-C", sys.argv[4], "worktree", "remove", sys.argv[2]]))\n')
        command = [sys.executable, script, record, '{worktree}', '{branch}', '{repo}']
        removes = []
        real_git = common.git

        def git(cwd, *args, **kw):
            if args[:2] == ('worktree', 'remove'):
                removes.append(args)
            return real_git(cwd, *args, **kw)
        with mock.patch.object(common, 'git', side_effect=git):
            res = worktrees.prune(self.repo, yes=True, policy=self.policy(remove_command=command))
        row = self.rows(res)['custom']
        self.assertEqual(row['action'], 'removed', row.get('error'))
        self.assertEqual(removes, [])                              # twinlane did not call git worktree remove
        with open(record, encoding='utf-8') as h:
            (args,) = [json.loads(line) for line in h]
        self.assertEqual(args, [os.path.normpath(wt), 'custom', self.main])
        self.assertFalse(os.path.exists(wt))
        self.assertTrue(row['branch_deleted'])                     # still -d, after the command

    def test_prune_refused_inside_worker(self):
        wt = self.merged('worker-sees', age=2 * 86400)
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': '20261013-120000-codex-x'}):
            for argv in (['prune', '--repo', self.repo, '--yes'], ['prune', '--repo', self.repo]):
                with self.subTest(argv=argv):
                    rc, out, err = run_cli(*argv)
                    self.assertEqual(rc, common.EXIT_REFUSED)
                    self.assertIn('refused inside a twinlane worker job', err)
                    self.assertEqual(out, '')
            rc, out, _ = run_cli('doctor', '--repo', self.repo)   # the read-only report still works
            self.assertEqual(rc, 0)
            self.assertIn('worker-sees', out)
        self.assertTrue(os.path.isdir(wt))
        self.assertTrue(self.branch_exists('worker-sees'))


if __name__ == '__main__':
    unittest.main()
