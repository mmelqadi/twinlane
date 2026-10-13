"""Per-project settings: main-checkout identity, the user-config-only rule, argv templates."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
import git_fixtures  # noqa: E402
from twinlane import common, projects  # noqa: E402


@unittest.skipUnless(git_fixtures.git_available(), 'git is required')
class ProjectConfig(unittest.TestCase):
    def setUp(self):
        self.ws = git_fixtures.Workspace()
        self.addCleanup(self.ws.cleanup)
        self.repo = self.ws.repo('app')
        self.config_dir = tempfile.mkdtemp(prefix='projects-cfg-', dir=os.environ['TWINLANE_HOME'])

    def policy(self, projects_map=None, **sections):
        """Packaged defaults with a user config merged in, the way load_policy does it."""
        user = dict(sections)
        if projects_map is not None:
            user['projects'] = projects_map
        path = os.path.join(self.config_dir, 'config.json')
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(user, handle)
        with mock.patch.dict(os.environ, {'TWINLANE_CONFIG': path}):
            return common.load_policy()

    def test_every_worktree_of_a_repository_resolves_to_its_main_checkout(self):
        wt = self.ws.worktree(self.repo, 'feature-x')
        sub = os.path.join(wt, 'docs')
        os.makedirs(sub)
        root_a, cfg_a = projects.resolve(self.repo, self.policy())
        root_b, cfg_b = projects.resolve(wt, self.policy())
        root_c, _ = projects.resolve(sub, self.policy())
        self.assertEqual(common.canon_path(root_a), common.canon_path(self.repo))
        self.assertEqual(common.canon_path(root_b), common.canon_path(root_a))
        self.assertEqual(common.canon_path(root_c), common.canon_path(root_a))
        self.assertEqual(cfg_a['key'], cfg_b['key'])
        self.assertEqual(cfg_a['key'], projects.repo_key(self.repo))
        self.assertRegex(cfg_a['key'], r'^[0-9a-f]{16}$')
        other = self.ws.repo('other')
        self.assertNotEqual(projects.repo_key(other), cfg_a['key'])
        with self.assertRaises(projects.ProjectError):
            projects.resolve(self.ws.path('not-a-repo-' + 'x'), self.policy())
        self.assertEqual(projects.state_dir('land', self.repo),
                         os.path.join(common.STATE, 'land', cfg_a['key']))

    def test_the_project_is_found_by_canonical_path_through_a_link(self):
        link = self.ws.link('app-link', self.repo)
        policy = self.policy({link: {'land': {'target': 'trunk'}}})
        _, cfg = projects.resolve(self.repo, policy)
        self.assertTrue(cfg['configured'])
        self.assertEqual(cfg['land']['target'], 'trunk')
        dup = self.policy({link: {}, self.repo: {}})
        with self.assertRaises(projects.ProjectError):
            projects.resolve(self.repo, dup)

    def test_an_unconfigured_project_gets_packaged_defaults_and_no_commands(self):
        _, cfg = projects.resolve(self.repo, self.policy())
        self.assertFalse(cfg['configured'])
        self.assertEqual((cfg['land']['heartbeat_s'], cfg['land']['heartbeat_stale_s'],
                          cfg['land']['gate_timeout_s']), (15, 120, 1800))
        self.assertEqual(cfg['ship']['dry_run_max_age_s'], 1800)
        self.assertEqual((cfg['wt']['stale_days'], cfg['wt']['prune_min_age_s']), (7, 86400))
        self.assertEqual((cfg['wt']['deps_dirs'], cfg['wt']['link_roots']), ([], []))
        self.assertEqual((cfg['best_of']['max_contestants'], cfg['resume']['max_batch']), (3, 10))
        for section, key in (('land', 'target'), ('land', 'gate'), ('land', 'command'), ('ship', 'command'),
                             ('ship', 'dry_run_command'), ('wt', 'root'), ('wt', 'setup_command'),
                             ('wt', 'remove_command')):
            self.assertIsNone(cfg[section][key], section + '.' + key)
        self.assertIs(cfg['land']['command_rebases'], False)
        self.assertIsNone(cfg['test'])

    def test_a_project_entry_overrides_one_key_and_keeps_the_other_defaults(self):
        entry = {'land': {'target': 'trunk', 'gate': ['make', 'check', '{worktree}'], 'command_rebases': True,
                          'gate_timeout_s': 60},
                 'ship': {'dry_run_command': ['plan-it', '{manifest}']},
                 'wt': {'deps_dirs': ['node_modules'], 'root': '../trees'},
                 'test': ['make', 'test']}
        _, cfg = projects.resolve(self.repo, self.policy({self.repo: entry}, land={'heartbeat_s': 20}))
        self.assertTrue(cfg['configured'])
        self.assertEqual(cfg['land']['gate'], ['make', 'check', '{worktree}'])
        self.assertEqual((cfg['land']['gate_timeout_s'], cfg['land']['heartbeat_s']), (60, 20))
        self.assertEqual(cfg['land']['heartbeat_stale_s'], 120)
        self.assertIs(cfg['land']['command_rebases'], True)
        self.assertEqual(cfg['ship']['dry_run_command'], ['plan-it', '{manifest}'])
        self.assertIsNone(cfg['ship']['command'])
        self.assertEqual(cfg['wt']['deps_dirs'], ['node_modules'])
        self.assertEqual(cfg['wt']['stale_days'], 7)
        self.assertEqual(cfg['test'], ['make', 'test'])
        for bad in ({'land': {'gat': ['x']}}, {'lnad': {}}, {'land': {'gate': 'make check'}},
                    {'land': {'gate': ['x', '{nope}']}}, {'wt': {'deps_dirs': ['../up']}},
                    {'wt': {'deps_dirs': [os.path.abspath(os.sep)]}}, {'land': {'gate_timeout_s': 0}},
                    {'land': {'command_rebases': 'yes'}}, {'test': 'make test'}, {'ship': {'command': []}}):
            with self.subTest(entry=bad), self.assertRaises(projects.ProjectError):
                projects.resolve(self.repo, self.policy({self.repo: bad}))

    def test_repository_content_never_configures_a_project(self):
        # A worker can edit anything in its checkout. None of it may name a gate or a ship command.
        for name in ('twinlane.json', '.twinlane.json', 'twinlane-config.json', 'config.json'):
            self.ws.write(self.repo, name, json.dumps({'projects': {self.repo: {
                'land': {'gate': ['true']}, 'ship': {'command': ['true']}}}}))
        os.makedirs(os.path.join(self.repo, '.twinlane'))
        self.ws.write(self.repo, '.twinlane/config.json', json.dumps({'land': {'gate': ['true']}}))
        wt = self.ws.worktree(self.repo, 'feature')
        self.ws.write(wt, '.twinlane.json', json.dumps({'land': {'gate': ['true']}}))
        for where in (self.repo, wt):
            _, cfg = projects.resolve(where, self.policy())
            self.assertFalse(cfg['configured'])
            self.assertIsNone(cfg['land']['gate'])
            self.assertIsNone(cfg['ship']['command'])

    def test_commands_are_argv_lists_with_exact_single_pass_placeholders(self):
        template = ['tool', '--at={worktree}', '{branch}', 'plain {slug} text', '$HOME', '{0}', '{a b}']
        got = projects.argv(template, worktree='/w x', branch='feat/a&b', slug='s')
        self.assertEqual(got, ['tool', '--at=/w x', 'feat/a&b', 'plain s text', '$HOME', '{0}', '{a b}'])
        # A value that contains a placeholder or a shell metacharacter is passed through untouched.
        got = projects.argv(['x', '{branch}', '{slug}'], branch='{slug}', slug='a b; rm -rf c')
        self.assertEqual(got, ['x', '{slug}', 'a b; rm -rf c'])
        for call in (lambda: projects.argv('make check'),                        # a string
                     lambda: projects.argv([]),                                  # empty
                     lambda: projects.argv(['x', '{nope}']),                     # unknown token
                     lambda: projects.argv(['x', '{branch}']),                   # no value given
                     lambda: projects.argv(['x'], nope='1'),                     # unknown value name
                     lambda: projects.argv(['x', 3])):                           # not a string
            with self.assertRaises(projects.ProjectError):
                call()
        # On Windows a .ps1 is started through powershell; elsewhere the argv is left alone.
        with mock.patch.object(common, 'IS_WIN', True), \
                mock.patch.object(common, '_powershell', return_value='powershell'):
            got = projects.argv(['{worktree}/check.PS1', '-Quick'], worktree='\x43:/w')
        self.assertEqual(got[0], 'powershell')
        self.assertEqual(got[-2:], ['\x43:/w/check.PS1', '-Quick'])
        self.assertIn('-File', got)
        with mock.patch.object(common, 'IS_WIN', False):
            self.assertEqual(projects.argv(['check.ps1']), ['check.ps1'])
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'job-1'}):
            self.assertTrue(projects.in_worker())
            err = io.StringIO()
            with redirect_stderr(err):
                self.assertEqual(projects.refuse_in_worker('land run'), common.EXIT_REFUSED)
            self.assertIn('refused inside a twinlane worker', err.getvalue())
        with mock.patch.dict(os.environ):
            os.environ.pop('TWINLANE_ENGINE_JOB', None)
            self.assertFalse(projects.in_worker())
            self.assertIsNone(projects.refuse_in_worker('land run'))

    def test_a_worktree_that_rewrites_its_git_file_cannot_pick_another_project(self):
        victim = self.ws.repo('victim')
        wt = self.ws.worktree(self.repo, 'feature-y')
        with open(os.path.join(wt, '.git'), 'r+', encoding='utf-8') as handle:  # r+: Git for Windows hides it
            handle.truncate(0)
            handle.write('gitdir: ' + os.path.join(victim, '.git').replace('\\', '/') + '\n')
        policy = self.policy({victim: {'land': {'target': 'main'}}})
        with self.assertRaises(projects.ProjectError):
            projects.resolve(wt, policy)

    def test_placeholder_values_with_line_breaks_are_refused(self):
        for bad in ('a\nb', 'a\rb', 'a\x00b'):
            with self.assertRaises(projects.ProjectError):
                projects.argv(['tool', '{slug}'], slug=bad)

    @unittest.skipUnless(common.IS_WIN, 'cmd.exe argument re-parsing is Windows-only')
    def test_a_batch_command_only_receives_plain_values(self):
        script = os.path.join(self.config_dir, 'land.cmd')
        with open(script, 'w', encoding='utf-8') as handle:
            handle.write('@echo off\r\n')
        self.assertEqual(projects.argv([script, '{slug}'], slug='feature-1.2'), [script, 'feature-1.2'])
        for bad in ('a&calc', 'a"b', 'a%PATH%', 'a^b', 'a|b', 'a>b', 'a!b', 'a(b)'):
            with self.assertRaises(projects.ProjectError):
                projects.argv([script, '{slug}'], slug=bad)
        self.assertEqual(projects.argv(['git', '{slug}'], slug='a&b'), ['git', 'a&b'])


if __name__ == '__main__':
    unittest.main()
