"""`--where cloud`: submit, poll, collect and apply against a fake `codex cloud` CLI. Nothing here touches the network."""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import git_fixtures
from test_jobs import JobsBase, meter_with
from twinlane import cloud, common, firewall, jobs

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_CLOUD = os.path.join(HERE, 'fake_codex_cloud.py')
FAKE_MIRROR = os.path.join(HERE, 'fake_mirror.py')


class CloudBase(JobsBase):
    CLOUD_KEYS = ('TWINLANE_CODEX_CLOUD_BIN', 'FAKE_CLOUD_DIR', 'FAKE_CLOUD_MODE', 'FAKE_CLOUD_POLLS_RUNNING',
                  'FAKE_MIRROR_FAIL')

    def setUp(self):
        super().setUp()
        self._cloud_env = {k: os.environ.get(k) for k in self.CLOUD_KEYS}
        self.state = os.path.join(self.tmp, 'cloud-state')
        os.environ.update({'TWINLANE_CODEX_CLOUD_BIN': FAKE_CLOUD, 'FAKE_CLOUD_DIR': self.state})
        for k in ('FAKE_CLOUD_MODE', 'FAKE_CLOUD_POLLS_RUNNING', 'FAKE_MIRROR_FAIL'):
            os.environ.pop(k, None)
        self.ws = git_fixtures.Workspace()
        self.addCleanup(self.ws.cleanup)
        self.repo = self.ws.repo('app', {'src/a.txt': 'one\n'})
        self.lane = self.ws.worktree(self.repo, 'lane')
        self.config({})

    def tearDown(self):
        for k, v in self._cloud_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        super().tearDown()

    def config_body(self, enabled=True):
        mirror = {'argv': [sys.executable, FAKE_MIRROR, '--ref', '{sha}', '--branch', '{branch}', '{adds}'],
                  'success_re': 'push rc 0', 'branch': 'cloud/{job}', 'lock': False}
        return {'enabled': enabled, 'env': 'org/store-cloud', 'poll_s': 0.05, 'poll_fail_max': 2, 'timeout_s': 60,
                'mirror': mirror}

    def config(self, cloud_extra, enabled=True):
        body = self.config_body(enabled)
        body.update(cloud_extra)
        self.write_config({'cloud': body})

    def run_cloud(self, *extra, cwd=None, kind='review'):
        return self.cli('run', '--brief', self.brief, '--cd', cwd or self.repo, '--kind', kind, '--where', 'cloud', *extra)

    def exec_records(self):
        return sorted(f for f in os.listdir(self.state) if f.startswith('exec-') and f.endswith('.json')) \
            if os.path.isdir(self.state) else []

    def mirror_records(self):
        return sorted(f for f in os.listdir(self.state) if f.startswith('mirror-')) if os.path.isdir(self.state) else []

    def read_state(self, name):
        return self.read_json_file(os.path.join(self.state, name))


class CloudRuns(CloudBase):
    def test_a_review_runs_in_the_cloud_and_never_starts_a_local_codex(self):
        rc, out, err = self.run_cloud()
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(self.only_job())
        self.assertEqual((r['status'], r['where'], r['engine']), ('done', 'cloud', 'codex'))
        c = r['cloud']
        self.assertEqual((c['task'], c['branch']), ('task_e_fake1', f"cloud/{r['id']}"))
        self.assertTrue(c['url'].endswith('/task_e_fake1'))
        self.assertEqual(c['firewall']['ok'], True)
        with open(r['final_path'], encoding='utf-8') as h:
            self.assertIn('No findings', h.read())
        sent = self.read_state('exec-1.json')
        self.assertEqual((sent['env'], sent['branch'], sent['attempts']), ('org/store-cloud', c['branch'], '1'))
        self.assertIn('Investigate the thing.', sent['prompt'])
        self.assertFalse(os.path.exists(self.codex_argv_out), 'a local codex process was started')
        pushed = self.read_state('mirror-1.json')
        self.assertEqual(pushed[pushed.index('--ref') + 1], git_fixtures.git(self.repo, 'rev-parse', 'HEAD'))

    def test_cloud_slots_are_not_local_codex_slots(self):
        jid = '20261027-000000-codex-ab12'
        jd = os.path.join(common.JOBS, jid)
        os.makedirs(jd)
        common.write_json(os.path.join(jd, 'status.json'), {
            'status': 'running', 'engine': 'cloud', 'pid': os.getpid(), 'pid_created': jobs._self_created()})
        common.write_json(os.path.join(jd, 'job.json'), {'kind': 'review', 'primary': {'engine': 'codex'}})
        self.assertEqual(jobs.running_count('codex'), 0)
        self.assertEqual(jobs.running_count('cloud'), 1)

    def test_a_long_brief_rides_as_an_attachment_not_on_the_command_line(self):
        with open(self.brief, 'w', encoding='utf-8') as h:
            h.write('Review the change.\n' + ('line of context\n' * 800))
        rc, out, err = self.run_cloud()
        self.assertEqual(rc, 0, out + err)
        sent = self.read_state('exec-1.json')
        self.assertIn('.cloud/brief.md', sent['prompt'])
        self.assertLess(len(sent['prompt']), 400)
        pushed = self.read_state('mirror-1.json')
        self.assertTrue(any(a.endswith('=.cloud/brief.md') for a in pushed), pushed)

    def test_the_mirrors_list_of_left_out_files_reaches_the_prompt(self):
        note = os.path.join(self.tmp, 'dropped.txt')
        with open(note, 'w', encoding='utf-8') as h:
            h.write('config/private_notes.md\n')
        cfg = self.config_body()
        cfg['mirror']['dropped_note_file'] = note
        self.write_config({'cloud': cfg})
        self.assertEqual(self.run_cloud()[0], 0)
        prompt = self.read_state('exec-1.json')['prompt']
        self.assertIn('Files absent from this snapshot', prompt)
        self.assertIn('config/private_notes.md', prompt)

    def test_attachments_ride_the_snapshot(self):
        note = os.path.join(self.tmp, 'report.md')
        with open(note, 'w', encoding='utf-8') as h:
            h.write('PRE: 3 failures, POST: 0 failures\n')
        rc, out, err = self.run_cloud('--attach', note + '=.cloud/verify-r1.md')
        self.assertEqual(rc, 0, out + err)
        pushed = self.read_state('mirror-1.json')
        sent = [a for a in pushed if a.endswith('=.cloud/verify-r1.md')]
        self.assertEqual(len(sent), 1, pushed)
        self.assertIn('stage', sent[0])  # the staged copy that was scanned, not the original path
        with open(sent[0].split('=')[0], encoding='utf-8') as h:
            self.assertEqual(h.read(), 'PRE: 3 failures, POST: 0 failures\n')

    def test_follow_up_carries_the_earlier_final_report(self):
        self.assertEqual(self.run_cloud()[0], 0)
        first = self.only_job()
        rc, out, err = self.run_cloud('--follow-up', first)
        self.assertEqual(rc, 0, out + err)
        second = [j for j in jobs._job_ids() if j != first][0]
        spec = self.read_json_file(os.path.join(common.JOBS, second, 'job.json'))
        self.assertIn('continues twinlane job ' + first, spec['brief_text'])
        self.assertIn('No findings', spec['brief_text'])
        self.assertEqual(self.receipt(second)['cloud']['task'], 'task_e_fake2')


class CloudRefusals(CloudBase):
    def test_disabled_cloud_is_refused_before_anything_is_created(self):
        self.config({}, enabled=False)
        rc, out, err = self.run_cloud()
        self.assertEqual(rc, common.EXIT_REFUSED, out + err)
        self.assertIn('cloud is not enabled', out + err)
        self.assertEqual(jobs._job_ids(), [])

    def test_cloud_flags_without_cloud_are_a_usage_error(self):
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.repo, '--kind', 'review', '--ref', 'main')
        self.assertEqual(rc, common.EXIT_USAGE, out + err)
        self.assertIn('need a cloud run', out + err)

    def test_cloud_cannot_be_combined_with_claude(self):
        rc, out, err = self.run_cloud('--engine', 'claude')
        self.assertEqual(rc, common.EXIT_USAGE, out + err)

    def test_the_firewall_blocks_the_send_and_never_echoes_the_secret(self):
        secret = 'ghp_' + 'a1B2c3D4e5' * 4
        leak = os.path.join(self.tmp, 'leak.md')
        with open(leak, 'w', encoding='utf-8') as h:
            h.write(f'note {secret}\n')
        rc, out, err = self.run_cloud('--attach', leak)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertTrue(r['error'].startswith('FIREWALL BLOCK'), r['error'])
        self.assertNotIn(secret, json.dumps(r))
        self.assertEqual(self.exec_records(), [])
        self.assertEqual(self.mirror_records(), [])

    def leaky(self, files):
        self.leaky_n = getattr(self, 'leaky_n', 0) + 1
        return self.ws.repo(f'leaky{self.leaky_n}', dict({'src/a.txt': 'one\n'}, **files))

    def test_a_secret_in_the_snapshot_blocks_before_the_mirror_and_is_never_echoed(self):
        secret = 'ghp_' + 'Z9y8X7w6V5' * 4
        repo = self.leaky({'src/config.txt': f'note {secret}\n'})
        self.run_cloud(cwd=repo)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertTrue(r['error'].startswith('FIREWALL BLOCK'), r['error'])
        self.assertIn('src/config.txt', r['error'])
        self.assertNotIn(secret, json.dumps(r))
        self.assertEqual(self.mirror_records(), [])
        self.assertEqual(self.exec_records(), [])

    def test_a_tracked_env_file_blocks_by_name_but_a_template_does_not(self):
        repo = self.leaky({'.env': 'A=1\n'})
        self.run_cloud(cwd=repo)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('.env', r['error'])
        self.assertEqual(self.mirror_records(), [])
        cfg = firewall.settings({})
        ok = self.leaky({'.env.example': 'A=\n'})
        tree = firewall.scan_tree(ok, git_fixtures.git(ok, 'rev-parse', 'HEAD'), cfg)
        self.assertTrue(tree['ok'], tree)

    def test_snapshot_exclude_and_the_scan_switch_are_explicit_and_visible(self):
        repo = self.leaky({'vendor/blob.txt': 'x = "' + 'ghp_' + 'Q1w2E3r4T5' * 4 + '"\n'})
        sha = git_fixtures.git(repo, 'rev-parse', 'HEAD')
        self.assertFalse(firewall.scan_tree(repo, sha, firewall.settings({}))['ok'])
        skipped = firewall.settings({'cloud': {'firewall': {'snapshot_exclude': ['vendor/*']}}})
        self.assertTrue(firewall.scan_tree(repo, sha, skipped)['ok'])
        self.config({'firewall': {'snapshot_scan': False}})
        self.assertEqual(self.run_cloud(cwd=repo)[0], 0)
        r = self.receipt(self.only_job())
        self.assertIn('snapshot_scan', r['cloud']['firewall']['snapshot']['skipped'])

    def tree_cfg(self, **extra):
        return firewall.settings({'cloud': {'firewall': extra}})

    def test_files_that_cannot_be_read_as_text_block_unless_excluded_or_media(self):
        repo = self.leaky({'data/big.txt': 'x' * 300, 'bin/blob.dat': 'abc\0def', 'img/logo.png': 'abc\0def',
                           'notes/utf16.txt': 'ok\0'})
        sha = git_fixtures.git(repo, 'rev-parse', 'HEAD')
        cfg = self.tree_cfg(max_bytes=100)
        res = firewall.scan_tree(repo, sha, cfg)
        files = {b['file'] for b in res['blocked']}
        self.assertEqual(files, {'data/big.txt', 'bin/blob.dat', 'notes/utf16.txt'}, res)
        self.assertEqual(res['skipped_media'], 1)
        cfg = self.tree_cfg(max_bytes=100, snapshot_exclude=['data/*', 'bin/*', 'notes/*'])
        self.assertTrue(firewall.scan_tree(repo, sha, cfg)['ok'])

    def test_a_denied_literal_hidden_in_a_large_or_binary_file_does_not_pass(self):
        repo = self.leaky({'big.txt': 'Contoso ' + 'x' * 300, 'weird.txt': 'Contoso\0'})
        sha = git_fixtures.git(repo, 'rev-parse', 'HEAD')
        res = firewall.scan_tree(repo, sha, self.tree_cfg(deny_literals=['Contoso'], max_bytes=100))
        self.assertFalse(res['ok'])
        self.assertEqual({b['file'] for b in res['blocked']}, {'big.txt', 'weird.txt'})

    def test_a_media_name_never_exempts_text_content(self):
        secret = 'ghp_' + 'M4n5B6v7C8' * 4
        repo = self.leaky({'img/fake.png': f'note {secret}\n', 'img/real.png': 'PNG\0data'})
        res = firewall.scan_tree(repo, git_fixtures.git(repo, 'rev-parse', 'HEAD'), self.tree_cfg())
        self.assertEqual({b['file'] for b in res['blocked']}, {'img/fake.png'}, res)
        self.assertEqual(res['skipped_media'], 1)
        self.assertNotIn(secret, json.dumps(res))

    def plumb(self, files):
        # a commit holding exact bytes and modes: {path: (bytes, mode)}
        repo = self.leaky({})
        for path, (data, mode) in files.items():
            oid = subprocess.run(['git', '-C', repo, 'hash-object', '-w', '--stdin'], input=data,
                                 capture_output=True, check=True).stdout.decode().strip()
            git_fixtures.git(repo, 'update-index', '--add', '--cacheinfo', f'{mode},{oid},{path}')
        git_fixtures.git(repo, 'commit', '-q', '-m', 'plumbing')
        return repo, git_fixtures.git(repo, 'rev-parse', 'HEAD')

    def test_oversized_text_behind_a_media_name_is_blocked(self):
        secret = 'ghp_' + 'Q1w2E3r4T5' * 4
        repo = self.leaky({'img/leak.png': f'note {secret}\n' + 'x' * 400})
        res = firewall.scan_tree(repo, git_fixtures.git(repo, 'rev-parse', 'HEAD'), self.tree_cfg(max_bytes=100))
        self.assertEqual({b['file'] for b in res['blocked']}, {'img/leak.png'}, res)
        self.assertNotIn(secret, json.dumps(res))
        real = self.leaky({'img/big.png': 'PNG\0' + 'x' * 400})
        ok = firewall.scan_tree(real, git_fixtures.git(real, 'rev-parse', 'HEAD'), self.tree_cfg(max_bytes=100))
        self.assertTrue(ok['ok'], ok)
        self.assertEqual(ok['skipped_media'], 1)

    def test_a_symlink_target_is_scanned_like_any_file(self):
        secret = 'ghp_' + 'A1s2D3f4G5' * 4
        repo, sha = self.plumb({'link': (secret.encode(), '120000')})
        res = firewall.scan_tree(repo, sha, self.tree_cfg())
        self.assertEqual({b['file'] for b in res['blocked']}, {'link'}, res)
        self.assertNotIn(secret, json.dumps(res))

    def test_invalid_utf8_is_blocked_unless_it_is_media(self):
        repo, sha = self.plumb({'payload.dat': (b'\xff\xfeABC', '100644'), 'img/odd.png': (b'\xff\xfeABC', '100644')})
        res = firewall.scan_tree(repo, sha, self.tree_cfg())
        self.assertEqual({b['file'] for b in res['blocked']}, {'payload.dat'}, res)

    def test_a_media_name_with_a_stray_byte_is_still_scanned_for_text(self):
        secret = 'ghp_' + 'Z1x2C3v4B5' * 4
        repo, sha = self.plumb({'img/credentials.jpg': (f'note {secret}\n'.encode() + b'\xff', '100644')})
        res = firewall.scan_tree(repo, sha, self.tree_cfg())
        self.assertEqual({b['file'] for b in res['blocked']}, {'img/credentials.jpg'}, res)
        self.assertNotIn(secret, json.dumps(res))

    def test_the_command_hook_runs_on_snapshot_files(self):
        repo = self.leaky({'src/secret_name.txt': 'the project is called Falcon\n'})
        sha = git_fixtures.git(repo, 'rev-parse', 'HEAD')
        script = os.path.join(self.tmp, 'tree_checker.py')
        with open(script, 'w', encoding='utf-8') as h:
            h.write("import sys\nsys.exit(3 if 'Falcon' in open(sys.argv[1], encoding='utf-8').read() else 0)\n")
        res = firewall.scan_tree(repo, sha, self.tree_cfg(command=[sys.executable, script, '{file}']))
        self.assertFalse(res['ok'])
        self.assertEqual(res['blocked'][0]['file'], 'src/secret_name.txt')
        self.assertNotIn('Falcon', json.dumps(res))

    def test_the_scan_cache_follows_every_setting_that_changes_the_verdict(self):
        repo = self.leaky({'a.txt': 'x' * 50})
        sha = git_fixtures.git(repo, 'rev-parse', 'HEAD')
        cache = os.path.join(self.tmp, 'scan-cache.json')
        self.assertTrue(firewall.scan_tree(repo, sha, self.tree_cfg(), cache_path=cache)['ok'])
        self.assertTrue(firewall.scan_tree(repo, sha, self.tree_cfg(), cache_path=cache).get('cached'))
        tight = firewall.scan_tree(repo, sha, self.tree_cfg(snapshot_max_total_bytes=10), cache_path=cache)
        self.assertFalse(tight['ok'])
        self.assertFalse(tight.get('cached'))
        small = firewall.scan_tree(repo, sha, self.tree_cfg(max_bytes=10), cache_path=cache)
        self.assertFalse(small['ok'])

    def test_a_clean_snapshot_scan_is_cached_per_commit(self):
        self.assertEqual(self.run_cloud()[0], 0)
        r = self.receipt(self.only_job())
        self.assertEqual(r['cloud']['firewall']['snapshot']['ok'], True)
        self.assertGreaterEqual(r['cloud']['firewall']['snapshot']['checked'], 1)
        cache = common.read_json(os.path.join(common.STATE, 'cloud-snapshot-scan.json'), {})
        self.assertEqual(len(cache), 1)
        again = firewall.scan_tree(self.repo, git_fixtures.git(self.repo, 'rev-parse', 'HEAD'), firewall.settings({}),
                                   cache_path=os.path.join(common.STATE, 'cloud-snapshot-scan.json'))
        self.assertTrue(again.get('cached'))

    def test_the_firewall_command_output_is_never_copied_into_a_reason(self):
        f = os.path.join(self.tmp, 'doc.md')
        with open(f, 'w', encoding='utf-8') as h:
            h.write('harmless\n')
        script = os.path.join(self.tmp, 'checker.py')
        with open(script, 'w', encoding='utf-8') as h:
            h.write("print('Rejected: swordfish-real-value')\nraise SystemExit(1)\n")
        cfg = firewall.settings({'cloud': {'firewall': {'command': [sys.executable, script, '{file}']}}})
        reasons = firewall.scan_file(f, cfg)
        self.assertEqual(reasons, ['firewall command refused (rc 1)'])

    def test_an_unsafe_attachment_destination_is_refused(self):
        note = os.path.join(self.tmp, 'n.md')
        with open(note, 'w', encoding='utf-8') as h:
            h.write('ok\n')
        self.run_cloud('--attach', note + '=../escape.md')
        self.assertIn('destination', self.receipt(self.only_job())['error'])
        self.assertEqual(self.exec_records(), [])


class CloudFailures(CloudBase):
    def failed_receipt(self, mode, **cfg):
        os.environ['FAKE_CLOUD_MODE'] = mode
        self.config(cfg)
        self.run_cloud()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed', r)
        self.assertFalse(os.path.exists(self.codex_argv_out), 'a failed cloud job was re-run locally')
        return r

    def test_a_failed_task_stays_failed_and_is_never_rerun_locally(self):
        r = self.failed_receipt('fail')
        self.assertIn('cloud task failed', r['error'])
        self.assertEqual(r['where'], 'cloud')

    def test_a_submit_error_is_a_failure_with_the_cli_message(self):
        r = self.failed_receipt('exec_fail')
        self.assertIn('submit failed', r['error'])
        self.assertIn('environment not found', r['error'])

    def test_a_snapshot_push_failure_stops_before_submit(self):
        os.environ['FAKE_MIRROR_FAIL'] = '1'
        self.run_cloud()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('snapshot push failed', r['error'])
        self.assertEqual(self.exec_records(), [])

    def test_an_unreachable_cloud_fails_after_the_poll_limit(self):
        r = self.failed_receipt('status_fail')
        self.assertIn('cloud unavailable', r['error'])

    def test_a_task_that_never_finishes_times_out(self):
        os.environ['FAKE_CLOUD_MODE'] = 'slow'
        self.config({'timeout_s': 1})
        self.run_cloud('--timeout', '1')
        self.assertEqual(self.receipt(self.only_job())['status'], 'timeout')


class CloudWriters(CloudBase):
    def run_code(self, *extra):
        return self.run_cloud('--write', *extra, cwd=self.lane, kind='code')

    def test_a_writing_job_applies_the_patch_inside_its_linked_worktree(self):
        rc, out, err = self.run_code()
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'done', r)
        self.assertEqual(r['cloud']['applied'], 'applied')
        with open(os.path.join(self.lane, 'cloud_result.txt'), encoding='utf-8') as h:
            self.assertEqual(h.read(), 'made in the cloud\n')
        self.assertFalse(os.path.exists(os.path.join(self.repo, 'cloud_result.txt')))
        with open(r['cloud']['patch_path'], 'rb') as h:
            self.assertEqual(hashlib.sha256(h.read()).hexdigest(), r['cloud']['patch_sha256'])

    def test_a_patch_that_does_not_apply_is_a_failure_and_keeps_the_patch(self):
        with open(os.path.join(self.lane, 'cloud_result.txt'), 'w', encoding='utf-8') as h:
            h.write('already here\n')
        self.run_code()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('apply-conflict', r['error'])
        self.assertTrue(os.path.exists(os.path.join(common.JOBS, r['id'], 'result.patch')))

    def test_a_patch_touching_the_context_folder_is_refused(self):
        os.environ['FAKE_CLOUD_MODE'] = 'cloudpath'
        self.run_code()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('.cloud/', r['error'])
        self.assertFalse(os.path.exists(os.path.join(self.lane, '.cloud')))

    def test_a_rename_into_the_context_folder_is_refused(self):
        os.environ['FAKE_CLOUD_MODE'] = 'renamecloud'
        self.run_code()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('.cloud/', r['error'])
        self.assertFalse(os.path.exists(os.path.join(self.lane, '.cloud')))

    @staticmethod
    def newfile(path, quoted=False):
        q = '"' if quoted else ''
        a, b = f'{q}a/{path}{q}', f'{q}b/{path}{q}'
        return (f'diff --git {a} {b}\nnew file mode 100644\nindex 0000000..e69de29\n--- /dev/null\n+++ {b}\n'
                '@@ -0,0 +1 @@\n+x\n').encode()

    def test_patch_paths_come_from_git_so_no_spelling_gets_through(self):
        octal = '.cl' + chr(92) + '157ud/b.md'  # git decodes \157 to "o"
        bad = {
            'new file in .cloud': self.newfile('.cloud/x'),
            'upper case': self.newfile('.CLOUD/x'),
            'trailing dot': self.newfile('.cloud./x'),
            'git dir': self.newfile('.git/hooks/pre-commit'),
            'octal quoted': self.newfile(octal, quoted=True),
            'rename into .cloud': b'diff --git a/src/a.txt b/.cloud/a.txt\nsimilarity index 100%\nrename from src/a.txt\n'
                                  b'rename to .cloud/a.txt\n',
            'copy into .cloud': b'diff --git a/src/a.txt b/.cloud/c.txt\nsimilarity index 100%\ncopy from src/a.txt\n'
                                b'copy to .cloud/c.txt\n',
            'parent dir': self.newfile('../x'),
            'short name': self.newfile('CLOUD~1/x'),
            'symlink': b'diff --git a/lnk b/lnk\nnew file mode 120000\nindex 0000000..e69de29\n--- /dev/null\n+++ b/lnk\n'
                       b'@@ -0,0 +1 @@\n+../../outside\n\\ No newline at end of file\n',
            'garbage': b'this is not a patch\n',
        }
        for label, raw in bad.items():
            self.assertIsNotNone(cloud.bad_patch_path(raw, self.repo), label)
        good = {
            'new file': self.newfile('src/b.txt'),
            'rename': b'diff --git a/src/a.txt b/src/b.txt\nsimilarity index 100%\nrename from src/a.txt\nrename to src/b.txt\n',
            '.github': self.newfile('.github/x'),
            'similar name': self.newfile('docs/.cloudy/x'),
        }
        for label, raw in good.items():
            self.assertIsNone(cloud.bad_patch_path(raw, self.repo), label)

    def test_a_submit_that_outlives_its_caller_is_never_submitted_again(self):
        os.environ['FAKE_CLOUD_MODE'] = 'exec_slow'
        with mock.patch.object(cloud, 'SUBMIT_TIMEOUT_S', 1):
            self.run_cloud()
            jid = self.only_job()
            r = self.receipt(jid)
            self.assertEqual(r['status'], 'failed')
            self.assertIn('not submitted again', r['error'])
            path = os.path.join(common.JOBS, jid, 'cloud.json')
            self.assertEqual(self.read_json_file(path)['state'], 'submitting')
            self.assertEqual(len(self.exec_records()), 1)
            spec = self.read_json_file(os.path.join(common.JOBS, jid, 'job.json'))
            again = cloud.run_attempt(jid, 1, {'profile': 'review', 'timeout_s': 30}, spec, common.load_policy())
            self.assertIn('not submitted again', again['error'])
            self.assertEqual(self.read_json_file(path)['state'], 'submitting')  # the marker survives a second look
            self.assertEqual(len(self.exec_records()), 1)

    def test_an_exec_that_prints_no_task_id_is_never_submitted_again(self):
        os.environ['FAKE_CLOUD_MODE'] = 'exec_noid'
        self.run_cloud()
        jid = self.only_job()
        r = self.receipt(jid)
        self.assertEqual(r['status'], 'failed')
        self.assertIn('not submitted again', r['error'])
        path = os.path.join(common.JOBS, jid, 'cloud.json')
        self.assertEqual(self.read_json_file(path)['state'], 'submitting')
        spec = self.read_json_file(os.path.join(common.JOBS, jid, 'job.json'))
        again = cloud.run_attempt(jid, 1, {'profile': 'review', 'timeout_s': 30}, spec, common.load_policy())
        self.assertIn('not submitted again', again['error'])
        self.assertEqual(len(self.exec_records()), 1)

    def test_a_task_that_outlives_its_runner_keeps_its_slot_until_it_finishes(self):
        os.environ['FAKE_CLOUD_MODE'] = 'slow'
        with mock.patch.object(cloud, '_poll_phase', return_value=('timeout', '')):
            self.run_cloud()
        jid = self.only_job()
        self.assertEqual(self.receipt(jid)['status'], 'timeout')
        self.assertEqual(cloud.occupied_count(), 1)
        self.assertEqual(jobs.running_count('cloud'), 1)
        self.assertEqual(jobs.running_count('cloud', exclude=jid), 0)  # its own later attempt is not blocked by it
        cloud.refresh_occupied(common.load_policy())
        self.assertEqual(cloud.occupied_count(), 1)  # still running remotely
        os.environ['FAKE_CLOUD_MODE'] = 'ok'
        os.environ['FAKE_CLOUD_POLLS_RUNNING'] = '0'
        time.sleep(0.2)  # past the shared poll cache
        cloud.refresh_occupied(common.load_policy())
        self.assertEqual(cloud.occupied_count(), 0)
        self.assertEqual(jobs.running_count('cloud'), 0)

    def test_an_occupied_entry_expires_after_its_ttl(self):
        cloud.hold('some-job', 'task_gone')
        self.assertEqual(cloud.occupied_count(), 1)
        self.config({'occupancy_ttl_s': 0.01})
        time.sleep(0.05)
        self.assertEqual(cloud.occupied_count(), 0)

    def test_a_collected_attempt_is_final_and_collects_nothing_twice(self):
        self.assertEqual(self.run_cloud()[0], 0)
        jid = self.only_job()
        path = os.path.join(common.JOBS, jid, 'cloud.json')
        saved = self.read_json_file(path)
        self.assertEqual(saved['state'], 'collected')
        saved['final'] = 'THE SAVED REPORT'
        common.write_json(path, saved)
        before = sorted(os.listdir(self.state))
        spec = self.read_json_file(os.path.join(common.JOBS, jid, 'job.json'))
        again = cloud.run_attempt(jid, 1, {'profile': 'review', 'timeout_s': 30}, spec, common.load_policy())
        self.assertEqual((again['status'], again['final_text']), ('done', 'THE SAVED REPORT'))
        self.assertEqual(sorted(os.listdir(self.state)), before)  # no new status or diff call

    def test_an_interrupted_apply_is_not_applied_a_second_time(self):
        block = ''.join(f'line {i}\n' for i in range(12))
        wt = self.ws.worktree(self.repo, 'repeated-context')
        with open(os.path.join(wt, 'dup.txt'), 'w', encoding='utf-8', newline='') as h:
            h.write(block * 3)
        git_fixtures.git(wt, 'add', 'dup.txt')
        git_fixtures.git(wt, 'commit', '-q', '-m', 'dup')
        with open(os.path.join(wt, 'dup.txt'), encoding='utf-8', newline='') as h:
            original = h.read()
        patch = os.path.join(self.tmp, 'dup.patch')
        with open(patch, 'w', encoding='utf-8', newline='') as h:
            h.write('diff --git a/dup.txt b/dup.txt\n--- a/dup.txt\n+++ b/dup.txt\n@@ -4,6 +4,7 @@\n line 3\n line 4\n'
                    ' line 5\n+inserted\n line 6\n line 7\n line 8\n')
        pre = cloud.touched_state(wt, patch)
        self.assertEqual(cloud._apply(wt, patch), 'applied')
        with open(os.path.join(wt, 'dup.txt'), encoding='utf-8', newline='') as h:
            once = h.read()
        self.assertEqual(once.count('inserted'), 1, once)
        self.assertNotEqual(cloud.touched_state(wt, patch), pre)
        self.assertNotEqual(once, original)
        post = cloud.touched_state(wt, patch)
        self.assertEqual(cloud._apply(wt, patch, recovering=True, pre_state=pre, post_state=post), 'applied')  # applies nothing
        with open(os.path.join(wt, 'dup.txt'), encoding='utf-8', newline='') as h:
            self.assertEqual(h.read().count('inserted'), 1)

    def test_recovery_refuses_when_the_patch_leaves_no_content_trace(self):
        wt = self.ws.worktree(self.repo, 'mode-only')
        patch = os.path.join(self.tmp, 'mode.patch')
        with open(patch, 'w', encoding='utf-8', newline='') as h:
            h.write('diff --git a/README.md b/README.md\nold mode 100644\nnew mode 100755\n')
        state = cloud.touched_state(wt, patch)
        self.assertIsNotNone(state)
        with self.assertRaises(cloud.CloudError):
            cloud._apply(wt, patch, recovering=True, pre_state=state, post_state=state)

    def test_recovery_does_not_mistake_a_repeated_block_for_an_applied_patch(self):
        block = ''.join(f'line {i}\n' for i in range(12))
        wt = self.ws.worktree(self.repo, 'repeat-recover')
        with open(os.path.join(wt, 'dup.txt'), 'w', encoding='utf-8', newline='') as h:
            h.write(block * 2)
        git_fixtures.git(wt, 'add', 'dup.txt')
        git_fixtures.git(wt, 'commit', '-q', '-m', 'dup')
        patch = os.path.join(self.tmp, 'dup2.patch')
        with open(patch, 'w', encoding='utf-8', newline='') as h:
            h.write('diff --git a/dup.txt b/dup.txt\n--- a/dup.txt\n+++ b/dup.txt\n@@ -4,6 +4,7 @@\n line 3\n line 4\n'
                    ' line 5\n+inserted\n line 6\n line 7\n line 8\n')
        # the second block already carries the change, the first (the patch's real target) does not: the patch is
        # reverse-applicable at the second block although this attempt never applied it
        with open(os.path.join(wt, 'dup.txt'), encoding='utf-8', newline='') as h:
            text = h.read()
        second = text.index('line 0', 5)
        edited = text[:second] + text[second:].replace('line 5\n', 'line 5\ninserted\n', 1)
        with open(os.path.join(wt, 'dup.txt'), 'w', encoding='utf-8', newline='') as h:
            h.write(edited)
        pre = cloud.touched_state(wt, patch)  # recorded before the interrupted apply began
        post = cloud.expected_state(wt, patch)
        self.assertNotIn(post, (None, pre))
        self.assertEqual(cloud._apply(wt, patch, recovering=True, pre_state=pre, post_state=post), 'applied')  # applies now
        with open(os.path.join(wt, 'dup.txt'), encoding='utf-8', newline='') as h:
            self.assertEqual(h.read().count('inserted'), 2)  # the first block got it, which a reverse check would have skipped
        self.assertEqual(cloud._apply(wt, patch, recovering=True, pre_state=pre, post_state=post), 'applied')  # no third copy
        with open(os.path.join(wt, 'dup.txt'), encoding='utf-8', newline='') as h:
            self.assertEqual(h.read().count('inserted'), 2)
        with self.assertRaises(cloud.CloudError):
            cloud._apply(wt, patch, recovering=True, pre_state=None, post_state=None)  # nothing recorded: never guess
        with open(os.path.join(wt, 'dup.txt'), 'a', encoding='utf-8', newline='') as h:
            h.write('unrelated edit\\n')
        with self.assertRaises(cloud.CloudError):  # matches neither recorded state: fail closed, whatever git could reverse
            cloud._apply(wt, patch, recovering=True, pre_state=pre, post_state=post)

    def test_a_ledger_that_cannot_be_written_is_reported_not_swallowed(self):
        os.environ['FAKE_CLOUD_MODE'] = 'slow'
        with mock.patch.object(cloud, '_poll_phase', return_value=('timeout', '')), \
                mock.patch.object(cloud, 'hold', return_value=False):
            self.run_cloud()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'timeout')
        self.assertIn('could not be recorded', r['error'])

    def test_redact_strips_secrets_and_contact_data_so_the_result_passes_the_scan(self):
        secret = 'ghp_' + 'R7t8Y9u0I1' * 4
        text = (f'verify: 3 failures, note {secret}\ncall +1' + '5550100123 or 05550100123, mail a@example.com b@example.com\n'
                'safe@example.org stays\n')
        cfg = firewall.settings({'cloud': {'firewall': {'deny_literals': ['Falcon'], 'email_allow': ['@example.org']}}})
        out, counts = firewall.redact(text + 'Falcon\n', cfg)
        self.assertNotIn(secret, out)
        self.assertNotIn('05550100123', out)
        self.assertNotIn('a@example.com', out)
        self.assertNotIn('Falcon', out)
        self.assertIn('safe@example.org', out)
        self.assertIn('verify: 3 failures', out)
        self.assertEqual(firewall.scan_text(out, cfg), [])
        self.assertEqual((counts.get('github-token'), counts.get('phone'), counts.get('email'), counts.get('literal')),
                         (1, 2, 2, 1))

    def test_the_redact_command_writes_a_copy_that_may_be_attached(self):
        secret = 'ghp_' + 'P3o4I5u6Y7' * 4
        src = os.path.join(self.tmp, 'report.md')
        with open(src, 'w', encoding='utf-8') as h:
            h.write(f'PRE 3 / POST 0, note {secret}\n')
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = firewall.cli([src])
        out = buf.getvalue()
        self.assertEqual(rc, 0, out)
        with open(src + '.redacted', encoding='utf-8') as h:
            body = h.read()
        self.assertIn('PRE 3 / POST 0', body)
        self.assertNotIn(secret, body + out)
        self.assertEqual(firewall.scan_file(src + '.redacted', firewall.settings(common.load_policy())), [])

    def test_a_patch_that_is_already_applied_counts_as_applied(self):
        wt = self.ws.worktree(self.repo, 'collect-twice')
        patch = os.path.join(self.tmp, 'twice.patch')
        with open(patch, 'wb') as h:
            h.write(self.newfile('twice.txt'))
        self.assertEqual(cloud._apply(wt, patch), 'applied')
        self.assertEqual(cloud._apply(wt, patch), 'applied')  # the second look finds it already there
        other = os.path.join(self.tmp, 'conflict.patch')
        with open(other, 'wb') as h:
            h.write(self.newfile('twice.txt').replace(b'+x', b'+y'))
        with self.assertRaises(cloud.CloudError):
            cloud._apply(wt, other)

    def test_a_runner_that_died_while_submitting_never_submits_again(self):
        self.assertEqual(self.run_cloud()[0], 0)
        jid = self.only_job()
        self.assertEqual(len(self.exec_records()), 1)
        common.write_json(os.path.join(common.JOBS, jid, 'cloud.json'),
                          {'attempt': 1, 'state': 'submitting', 'task': None, 'branch': 'cloud/lost'})
        spec = self.read_json_file(os.path.join(common.JOBS, jid, 'job.json'))
        a = cloud.run_attempt(jid, 1, {'profile': 'review', 'timeout_s': 30}, spec, common.load_policy())
        self.assertEqual(a['status'], 'failed')
        self.assertIn('not submitted again', a['error'])
        self.assertEqual(len(self.exec_records()), 1)

    def test_no_diff_is_done_with_a_note_for_a_review(self):
        os.environ['FAKE_CLOUD_MODE'] = 'nodiff'
        self.run_cloud()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'done')
        self.assertIsNone(r['cloud']['patch_sha256'])
        with open(r['final_path'], encoding='utf-8') as h:
            self.assertIn('no diff', h.read())

    def test_the_shared_checkout_is_never_a_cloud_writing_target(self):
        rc, out, err = self.run_cloud('--write', cwd=self.repo, kind='code')
        self.assertEqual(rc, common.EXIT_REFUSED, out + err)
        self.assertIn('LINKED worktree', out + err)

class CloudFirst(CloudBase):
    def run_parallel(self, *extra):
        return self.cli('run', '--brief', self.brief, '--cd', self.repo, '--kind', 'parallel', *extra)

    def test_default_where_cloud_sends_a_codex_job_to_the_cloud_with_a_note(self):
        self.config({'default_where': 'cloud'})
        rc, out, err = self.run_parallel()
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(self.only_job())
        self.assertEqual((r['where'], r['status']), ('cloud', 'done'))
        self.assertIn('cloud-first', ' '.join(r['notes']))
        self.assertFalse(os.path.exists(self.codex_argv_out))

    def test_a_job_routed_to_claude_stays_on_claude(self):
        self.config({'default_where': 'cloud'})
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.repo, '--kind', 'review')
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(self.only_job())
        self.assertEqual((r['engine'], r['where']), ('claude', 'local'))
        self.assertEqual(self.exec_records(), [])

    def test_a_brief_that_needs_this_machine_runs_locally_with_a_note(self):
        self.config({'default_where': 'cloud', 'needs_local': ['ECHO:']})
        rc, out, err = self.run_parallel()
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(self.only_job())
        self.assertEqual((r['engine'], r['where']), ('codex', 'local'))
        self.assertIn('needs this machine', ' '.join(r['notes']))
        self.assertEqual(self.exec_records(), [])

    def test_where_local_wins_over_the_default(self):
        self.config({'default_where': 'cloud'})
        rc, out, err = self.run_parallel('--where', 'local')
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.receipt(self.only_job())['where'], 'local')
        self.assertEqual(self.exec_records(), [])

    def test_a_misconfigured_cloud_first_runs_locally_and_says_so(self):
        self.config({'default_where': 'cloud', 'env': None})
        rc, out, err = self.run_parallel()
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(self.only_job())
        self.assertEqual(r['where'], 'local')
        self.assertIn('unusable', ' '.join(r['notes']))


class PollCache(unittest.TestCase):
    def test_parse_state(self):
        self.assertEqual(cloud.parse_state('[READY] done\nbody')[0], 'ready')
        self.assertEqual(cloud.parse_state('\n[ERROR] x')[0], 'failed')
        self.assertEqual(cloud.parse_state('[CANCELLED] x')[0], 'cancelled')
        self.assertEqual(cloud.parse_state('[PENDING] x')[0], 'running')
        self.assertEqual(cloud.parse_state('[Some New State] x')[0], 'running')
        self.assertEqual(cloud.parse_state('no brackets here')[0], 'unknown')
        self.assertEqual(cloud.parse_state('')[0], 'unknown')

    def test_settings_merge_keeps_nested_defaults(self):
        cfg = cloud.settings({'cloud': {'enabled': True, 'mirror': {'argv': ['x']}}})
        self.assertTrue(cfg['enabled'])
        self.assertEqual(cfg['mirror']['argv'], ['x'])
        self.assertEqual(cfg['mirror']['branch'], 'cloud/{job}')
        self.assertFalse(cloud.settings({})['enabled'])


class FirewallRules(unittest.TestCase):
    CFG = firewall.settings({})

    def test_built_in_token_patterns_block(self):
        for text in ('key AKIA' + 'ABCDEFGHIJKLMNOP', '-----BEGIN RSA ' + 'PRIVATE KEY-----',
                     'sk-' + 'abcdefghijklmnopqrstuvwxyz', 'password = "' + 'hunter2hunter2' + '"'):
            self.assertTrue(firewall.scan_text(text, self.CFG), text)
        self.assertEqual(firewall.scan_text('plain prose about a password policy', self.CFG), [])

    def test_bulk_contact_data_blocks_but_a_few_do_not(self):
        phones = '\n'.join(f'+2010{n:08d}' for n in range(1, 6))
        self.assertTrue(firewall.scan_text(phones, self.CFG))
        self.assertEqual(firewall.scan_text('call +44' + '2079460001 or +44' + '2079460002', self.CFG), [])
        mails = '\n'.join(f'user{n}@company.org' for n in range(8))
        self.assertTrue(firewall.scan_text(mails, self.CFG))
        self.assertEqual(firewall.scan_text('\n'.join(f'a{n}@example.com' for n in range(20)), self.CFG), [])

    def test_owner_rules_extend_the_built_ins(self):
        cfg = firewall.settings({'cloud': {'firewall': {'patterns': {'order-id': r'ORD-\d{6}'},
                                                       'deny_literals': ['Contoso Ltd']}}})
        self.assertTrue(firewall.scan_text('see ORD-123456', cfg))
        self.assertTrue(firewall.scan_text('for Contoso Ltd only', cfg))

    def test_a_reason_names_the_rule_and_line_never_the_text(self):
        secret = 'ghp_' + 'Zz9Yy8Xx7W' * 4
        reasons = firewall.scan_text(f'a\nb {secret}\n', self.CFG)
        self.assertEqual(reasons, ['line 2: github-token'])

    def test_files_binary_and_oversize_are_refused(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            binary = os.path.join(d, 'b.bin')
            with open(binary, 'wb') as h:
                h.write(b'\xff\xfe\x00\x01')
            self.assertIn('not UTF-8', firewall.scan_file(binary, self.CFG)[0])
            big = os.path.join(d, 'big.txt')
            with open(big, 'w') as h:
                h.write('x' * 50)
            self.assertIn('larger than', firewall.scan_file(big, dict(self.CFG, max_bytes=10))[0])

    def test_the_command_hook_can_block(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            f = os.path.join(d, 'a.txt')
            with open(f, 'w') as h:
                h.write('hello\n')
            code = "import sys; print('internal name found'); sys.exit(3)"
            cfg = dict(self.CFG, command=[sys.executable, '-c', code, '{file}'])
            self.assertEqual(firewall.scan_file(f, cfg), ['firewall command refused (rc 3)'])
            cfg = dict(self.CFG, command=[sys.executable, '-c', 'pass', '{file}'])
            self.assertEqual(firewall.scan_file(f, cfg), [])

    def test_destinations_must_be_relative_and_inside(self):
        for bad in ('/etc/x', 'C:/x', '../x', 'a/../../x', ''):
            self.assertIsNotNone(firewall.safe_dest(bad), bad)
        self.assertIsNone(firewall.safe_dest('.cloud/report.md'))


if __name__ == '__main__':
    unittest.main()
