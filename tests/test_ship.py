"""twinlane ship: manifests of landed work, and a ship command that never runs by default."""
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
import git_fixtures  # noqa: E402
from twinlane import common, land, projects, ship  # noqa: E402

PY = sys.executable
GIT_IDENTITY = {'GIT_AUTHOR_NAME': 'Fixture', 'GIT_AUTHOR_EMAIL': 'fixture@example.org',
                'GIT_COMMITTER_NAME': 'Fixture', 'GIT_COMMITTER_EMAIL': 'fixture@example.org'}

# Records its arguments, the manifest bytes' sha256 and the TWINLANE_SHIP_* env, then exits with argv[-1].
RECORDER = r'''
import hashlib, json, os, sys
out, code = sys.argv[-2], int(sys.argv[-1])
args = sys.argv[1:-2]
with open(os.environ['TWINLANE_SHIP_MANIFEST'], 'rb') as handle:
    seen = hashlib.sha256(handle.read()).hexdigest()
env = {k: v for k, v in os.environ.items() if k.startswith('TWINLANE_SHIP_')}
with open(out, 'w', encoding='utf-8') as handle:
    json.dump({'args': args, 'manifest_sha256': seen, 'env': env, 'cwd': os.getcwd()}, handle)
print('recorder ran', code)
sys.exit(code)
'''


def sha256(data):
    return hashlib.sha256(data).hexdigest()


@unittest.skipUnless(git_fixtures.git_available(), 'git is required')
class Ship(unittest.TestCase):
    repo_name = 'app'

    def setUp(self):
        self.ws = git_fixtures.Workspace(prefix='twinlane-ship-')
        self.addCleanup(self.ws.cleanup)
        self.repo = self.ws.repo(self.repo_name, files={'shared.txt': 'shared\n', 'other.txt': 'other\n'})
        git_fixtures.git(self.repo, 'config', 'core.autocrlf', 'false')
        git_fixtures.git(self.repo, 'config', 'core.eol', 'lf')
        cfg_dir = tempfile.mkdtemp(prefix='ship-cfg-', dir=os.environ['TWINLANE_HOME'])
        self.config_path = os.path.join(cfg_dir, 'config.json')
        patcher = mock.patch.dict(os.environ, dict(GIT_IDENTITY, TWINLANE_CONFIG=self.config_path))
        patcher.start()
        self.addCleanup(patcher.stop)
        os.environ.pop('TWINLANE_ENGINE_JOB', None)
        self.scripts = self.ws.path('scripts')
        os.makedirs(self.scripts)
        self.recorder = os.path.join(self.scripts, 'recorder.py')
        with open(self.recorder, 'w', encoding='utf-8') as handle:
            handle.write(RECORDER)
        self.dry_marker = self.ws.path('dry.json')
        self.ship_marker = self.ws.path('shipped.json')
        self.configure()

    # --- helpers -------------------------------------------------------------

    def configure(self, ship_cfg=None, **extra):
        entry = dict(extra)
        if ship_cfg is not None:
            entry['ship'] = ship_cfg
        with open(self.config_path, 'w', encoding='utf-8') as handle:
            json.dump({'projects': {self.repo: entry}}, handle)

    def configure_commands(self, dry_rc=0, dry=True, command=True, **ship_extra):
        cfg = dict(ship_extra)
        if dry:
            cfg['dry_run_command'] = [PY, self.recorder, '{manifest}', '{source_commit}', '{slug}', '{base}',
                                      self.dry_marker, str(dry_rc)]
        if command:
            cfg['command'] = [PY, self.recorder, '{manifest}', '{source_commit}', '{slug}', '{target}',
                              self.ship_marker, '0']
        self.configure(cfg)

    def call(self, module, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = module.cli(list(argv))
        return rc, out.getvalue(), err.getvalue()

    def ship(self, *argv):
        return self.call(ship, *(list(argv) + ['--repo', self.repo]))

    def land_slug(self, slug, files=None, delete=()):
        """Land a new worktree branch with one commit per file (and one per deletion); returns the ledger event."""
        wt = self.ws.worktree(self.repo, 'feat-' + slug)
        for rel, text in (files or {slug + '.txt': slug + '\n'}).items():
            self.ws.commit(wt, rel, text)
        for rel in delete:
            git_fixtures.git(wt, 'rm', '-q', '--', rel)
            git_fixtures.git(wt, 'commit', '-q', '-m', 'delete ' + rel)
        for argv in (['enqueue', slug, '--worktree', wt], ['run', slug]):
            rc, _, err = self.call(land, *(argv + ['--repo', self.repo]))
            self.assertEqual(rc, 0, err)
        return land.landings(self.repo, slug)[-1]

    def plan(self, *argv, rc=0):
        code, out, err = self.ship('plan', *argv, '--json')
        self.assertEqual(code, rc, err + out)
        res = json.loads(out)
        if rc == 0:
            with open(res['manifest'], 'rb') as handle:
                res['data'] = json.loads(handle.read().decode('utf-8'))
        return res

    def blob(self, commit, rel):
        return git_fixtures.git(self.repo, 'rev-parse', f'{commit}:{rel}')

    def manifest_files(self):
        folder = projects.state_dir('ship', self.repo)
        if not os.path.isdir(folder):
            return []
        return sorted(n for n in os.listdir(folder) if n.endswith('.json'))

    def receipt_path(self, sha):
        return ship.Paths(self.repo).receipt(sha)

    def ledger(self):
        path = ship.Paths(self.repo).ledger
        if not os.path.exists(path):
            return []
        with open(path, encoding='utf-8') as handle:
            return [json.loads(line) for line in handle if line.strip()]

    def dry_run(self, manifest):
        rc, out, err = self.ship('run', '--manifest', manifest)
        self.assertEqual(rc, 0, err + out)
        return out

    # --- plan ----------------------------------------------------------------

    def test_manifest_hashes_filtered_blobs_at_source_commit(self):
        landed = self.land_slug('a', {'a.txt': 'one\n', 'nested/deep.txt': 'deep\n'})
        later = self.ws.commit(self.repo, 'a.txt', 'two, after the landing\n')   # the target moves on
        res = self.plan('a', '--allow-superseded')
        m = res['data']
        with open(res['manifest'], 'rb') as handle:
            self.assertEqual(res['sha256'], sha256(handle.read()))
        self.assertEqual((m['schema'], m['slug'], m['target']), ('twinlane-ship-manifest-v1', 'a', 'main'))
        self.assertEqual((m['base_commit'], m['source_commit']), (landed['from'], landed['to']))
        self.assertEqual(m['target_head'], later)
        self.assertEqual(m['filters']['attr_source'], landed['to'])
        by_path = {e['path']: e for e in m['files']}
        self.assertEqual(sorted(by_path), ['a.txt', 'nested/deep.txt'])
        self.assertEqual(by_path['a.txt'], {'path': 'a.txt', 'status': 'A', 'mode': '100644',
                                            'blob': self.blob(landed['to'], 'a.txt'),
                                            'sha256': sha256(b'one\n'), 'size': 4})
        self.assertEqual(by_path['nested/deep.txt']['sha256'], sha256(b'deep\n'))
        self.assertEqual(m['deleted'], [])
        self.assertEqual([c['sha'] for c in m['commits']], landed['commits'])
        self.assertTrue(all(c['slugs'] == ['a'] for c in m['commits']))
        self.assertTrue(res['manifest'].startswith(projects.state_dir('ship', self.repo)))
        # a range plan of the same commits hashes the same bytes
        rng = self.plan(f"{landed['from']}..{landed['to']}", '--allow-superseded')['data']
        self.assertEqual(rng['files'], m['files'])
        self.assertIsNone(rng['slug'])
        planned = [e for e in self.ledger() if e['event'] == 'planned']
        self.assertEqual(planned[0]['manifest_sha256'], res['sha256'])

    def test_eol_filter_respected(self):
        landed = self.land_slug('eol', {'.gitattributes': '*.txt text eol=crlf\n*.bin -text\n',
                                        'lines.txt': 'a\nb\n', 'raw.bin': 'x\ny\n'})
        raw = git_fixtures.git(self.repo, 'cat-file', '-p', f"{landed['to']}:lines.txt")
        self.assertNotIn('\r', raw)                                  # the blob itself holds LF
        # the target later drops the eol rule; the source commit's attributes still decide
        self.ws.commit(self.repo, '.gitattributes', '*.txt -text\n')
        m = self.plan('eol', '--allow-superseded')['data']
        by_path = {e['path']: e for e in m['files']}
        self.assertEqual(by_path['lines.txt']['sha256'], sha256(b'a\r\nb\r\n'))
        self.assertEqual(by_path['lines.txt']['size'], 6)
        self.assertNotEqual(by_path['lines.txt']['sha256'], sha256(raw.encode('utf-8') + b'\n'))
        self.assertEqual(by_path['raw.bin']['sha256'], sha256(b'x\ny\n'))
        self.assertEqual(by_path['.gitattributes']['sha256'], sha256(b'*.txt text eol=crlf\n*.bin -text\n'))
        self.assertEqual(m['flags']['superseded_paths'], [{'path': '.gitattributes',
                                                           'by': [git_fixtures.git(self.repo, 'rev-parse', 'HEAD')],
                                                           'subjects': ['update .gitattributes']}])
        # run re-derives the same filtered bytes, so the manifest verifies
        self.configure_commands()
        self.assertEqual(ship.verify_manifest(self.repo, m)['new_superseded'], [])

    def test_deleted_listed(self):
        before = git_fixtures.git(self.repo, 'rev-parse', 'HEAD')
        landed = self.land_slug('del', {'shared.txt': 'changed\n'}, delete=('other.txt',))
        m = self.plan('del')['data']
        self.assertEqual(m['deleted'], [{'path': 'other.txt', 'blob': self.blob(before, 'other.txt')}])
        self.assertEqual([(e['path'], e['status']) for e in m['files']], [('shared.txt', 'M')])
        self.assertEqual(m['files'][0]['sha256'], sha256(b'changed\n'))
        self.assertEqual(len(landed['commits']), 2)

    def test_excluded_and_superseded_flagged(self):
        early = self.land_slug('c', {'c.txt': 'c\n'})
        mine = self.land_slug('a', {'a.txt': 'a1\n'})
        later = self.land_slug('b', {'a.txt': 'b changed a\n', 'b.txt': 'b\n'})
        direct = self.ws.commit(self.repo, 'z.txt', 'no landing\n', message='direct commit')
        m = self.plan('a', '--baseline', early['from'], '--allow-superseded')['data']
        self.assertEqual([e['path'] for e in m['files']], ['a.txt'])
        excluded = {e['sha']: e for e in m['excluded_commits']}
        for sha in later['commits']:
            self.assertEqual((excluded[sha]['where'], excluded[sha]['slugs'], excluded[sha]['in_shipped_bytes']),
                             ('after', ['b'], False))
        self.assertEqual((excluded[direct]['where'], excluded[direct]['slugs']), ('after', []))
        self.assertEqual(excluded[direct]['subject'], 'direct commit')
        (c_sha,) = early['commits']
        self.assertEqual((excluded[c_sha]['where'], excluded[c_sha]['in_shipped_bytes']), ('before', False))
        self.assertFalse(set(mine['commits']) & set(excluded))       # the slug's own commits are not excluded
        self.assertEqual(m['flags']['superseded_paths'], [{'path': 'a.txt', 'by': later['commits'][:1],
                                                           'subjects': ['update a.txt']}])
        self.assertTrue(m['flags']['allow_superseded'])
        self.assertEqual(m['flags']['foreign_in_range'], [])
        self.assertEqual(m['flags']['baseline'], early['from'])
        # without a baseline nothing before the landing is listed
        plain = self.plan('a', '--allow-superseded')['data']
        self.assertNotIn(c_sha, {e['sha'] for e in plain['excluded_commits']})
        # --landing picks an earlier landing of the same slug
        self.assertEqual(self.plan('c', '--landing', '1')['data']['source_commit'], early['to'])
        self.assertEqual(self.ship('plan', 'c', '--landing', '2')[0], common.EXIT_USAGE)

    def test_superseded_refuses_without_flag_and_execute_rechecks(self):
        self.land_slug('a', {'a.txt': 'a1\n'})
        later = self.land_slug('b', {'a.txt': 'b changed a\n'})
        count = len(self.manifest_files())
        # plan: refused by default, naming the path and the newer commit; nothing written
        rc, out, err = self.ship('plan', 'a')
        self.assertEqual((rc, out), (common.EXIT_REFUSED, ''))
        self.assertIn(f"a.txt (superseded by {later['commits'][0][:12]} update a.txt)", err)
        self.assertIn('--allow-superseded', err)
        res = self.plan('a', rc=common.EXIT_REFUSED)
        self.assertEqual(res['details']['superseded_paths'],
                         [{'path': 'a.txt', 'by': later['commits'], 'subjects': ['update a.txt']}])
        self.assertEqual(len(self.manifest_files()), count)
        self.assertEqual((self.ledger()[-1]['event'], self.ledger()[-1]['superseded']), ('plan-refused', ['a.txt']))
        # with the flag: written, recorded in the manifest and the receipt, printed with the newer commit
        rc, out, err = self.ship('plan', 'a', '--allow-superseded')
        self.assertEqual(rc, 0, err)
        self.assertIn('SUPERSEDED (allowed by --allow-superseded)', out)
        self.assertIn(later['commits'][0][:12], out)
        res = self.plan('a', '--allow-superseded')
        self.assertTrue(res['data']['flags']['allow_superseded'])
        check = ship.verify_manifest(self.repo, res['data'])
        self.assertIsNone(ship.superseded_problem(res['data'], check))
        # a hand-edited manifest that lists the supersession but drops the flag is refused at execute
        edited = dict(res['data'], flags=dict(res['data']['flags'], allow_superseded=False))
        self.assertIn('a.txt (superseded by', ship.superseded_problem(edited, check))
        self.configure_commands()
        out = self.dry_run(res['manifest'])
        self.assertIn('superseded, allowed by the plan', out)
        receipt = common.read_json(self.receipt_path(res['sha256']))
        self.assertTrue(receipt['allow_superseded'])
        self.assertEqual([s['path'] for s in receipt['superseded_paths']], ['a.txt'])
        rc, _, err = self.ship('run', '--manifest', res['manifest'], '--execute', '--confirm', res['sha256'][:8])
        self.assertEqual(rc, 0, err)                     # the recorded supersession is what was allowed
        os.remove(self.ship_marker)
        # execute re-checks: a newer commit on the same path since the plan refuses, even with the flag
        self.dry_run(res['manifest'])
        newer = self.ws.commit(self.repo, 'a.txt', 'newer still\n', message='newer still')
        rc, out, err = self.ship('run', '--manifest', res['manifest'], '--execute', '--confirm', res['sha256'][:8])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn(f'a.txt (superseded by {newer[:12]} newer still)', err)
        self.assertNotIn(later['commits'][0][:12], err)  # only the supersession the plan did not record
        self.assertFalse(os.path.exists(self.ship_marker))
        self.assertEqual(self.ledger()[-1]['event'], 'execute-refused')
        # and a manifest planned with nothing superseded refuses once something is
        self.land_slug('x', {'x.txt': 'x\n'})
        clean = self.plan('x')
        self.assertFalse(clean['data']['flags']['allow_superseded'])
        self.dry_run(clean['manifest'])
        over = self.ws.commit(self.repo, 'x.txt', 'over x\n', message='over x')
        rc, out, _ = self.ship('run', '--manifest', clean['manifest'])
        self.assertEqual(rc, 0)                          # the dry run itself still runs, and warns
        self.assertIn('warning: ', out)
        self.assertIn(f'x.txt (superseded by {over[:12]} over x)', out)
        rc, _, err = self.ship('run', '--manifest', clean['manifest'], '--execute', '--confirm',
                               clean['sha256'][:8])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn(f'x.txt (superseded by {over[:12]} over x)', err)
        self.assertFalse(os.path.exists(self.ship_marker))
        self.assertIsNone(common.read_json(self.receipt_path(clean['sha256'])).get('consumed_at'))

    def test_foreign_in_range_refuses_without_flag(self):
        peer = self.land_slug('peer', {'shared.txt': 'peer work\n'})
        mine = self.land_slug('mine', {'shared.txt': 'peer work\nmine\n', 'mine.txt': 'm\n'})
        count = len(self.manifest_files())
        # the baseline is what already shipped: peer's change rides inside shared.txt's bytes
        res = self.plan('mine', '--baseline', peer['from'], rc=common.EXIT_REFUSED)
        self.assertFalse(res['ok'])
        self.assertEqual([c['sha'] for c in res['details']['foreign_in_range']], peer['commits'])
        self.assertEqual(len(self.manifest_files()), count)           # nothing written
        self.assertEqual(self.ledger()[-1]['event'], 'plan-refused')
        allowed = self.plan('mine', '--baseline', peer['from'], '--allow-foreign')['data']
        self.assertTrue(allowed['flags']['allow_foreign'])
        self.assertEqual([c['sha'] for c in allowed['flags']['foreign_in_range']], peer['commits'])
        self.assertEqual(allowed['flags']['foreign_in_range'][0]['where'], 'before')
        self.assertEqual(allowed['flags']['foreign_in_range'][0]['manifest_paths'], ['shared.txt'])
        # a range holding a commit no landing recorded is refused the same way
        direct = self.ws.commit(self.repo, 'direct.txt', 'unlanded\n')
        res = self.plan(f"{mine['from']}..{direct}", rc=common.EXIT_REFUSED)
        self.assertEqual([c['sha'] for c in res['details']['foreign_in_range']], [direct])
        ok = self.plan(f"{mine['from']}..{direct}", '--allow-foreign')['data']
        self.assertEqual(ok['flags']['foreign_in_range'][0]['sha'], direct)
        # a range of landed work only is not foreign
        self.plan(f"{peer['from']}..{mine['to']}")
        # a slug whose ledger range holds another slug's commit is refused too
        events = land.read_ledger(self.repo)
        forged = dict(events[-1], slug='other', commits=mine['commits'][:1])
        with open(land.Paths(self.repo).ledger, 'a', encoding='utf-8') as handle:
            handle.write(json.dumps(forged) + '\n')
        res = self.plan('mine', rc=common.EXIT_REFUSED)
        self.assertEqual([c['sha'] for c in res['details']['foreign_in_range']], mine['commits'][:1])

    # --- run -----------------------------------------------------------------

    def record_unverified(self, event):
        unverified = dict(event, event='landed-unverified', reason='command did not complete')
        paths = land.Paths(self.repo)
        with paths.locked():
            land._append_ledger(paths, unverified)

    def test_unverified_range_refuses_plan_regardless_of_foreign_override(self):
        bad = self.land_slug('unchecked')
        self.record_unverified(bad)
        later = self.land_slug('later')
        for flag in ([], ['--allow-foreign']):
            for target in (f"{bad['from']}..{later['to']}", 'unchecked'):
                res = self.plan(target, *flag, rc=common.EXIT_REFUSED)
                self.assertIn('unchecked', res['reason'])
                self.assertIn(f"{bad['from']}..{bad['to']}", res['reason'])
                self.assertIn('a verified landing or a fix-forward on the target is needed', res['reason'])
            # A baseline cannot silently include the unchecked range either.
            self.plan('later', '--baseline', bad['from'], *flag, rc=common.EXIT_REFUSED)
        # An isolated later range does not overlap the unchecked commits.
        self.plan('later')
        # Giving the same commits another target branch cannot hide the unverified range.
        git_fixtures.git(self.repo, 'branch', 'release', later['to'])
        self.configure(land={'target': 'release'})
        self.plan(f"{bad['from']}..{later['to']}", '--allow-foreign', rc=common.EXIT_REFUSED)

    def test_unverified_range_rechecked_by_dry_run_and_execute(self):
        bad = self.land_slug('unchecked')
        manifests = [self.plan('unchecked', *flag) for flag in ([], ['--allow-foreign'])]
        self.configure_commands()
        for res in manifests:
            self.dry_run(res['manifest'])
        self.record_unverified(bad)  # a manifest/receipt predating the finding cannot bypass it
        for res in manifests:
            for args in ([], ['--execute', '--confirm', res['sha256'][:8]]):
                rc, out, err = self.ship('run', '--manifest', res['manifest'], *args)
                self.assertEqual(rc, common.EXIT_REFUSED, out + err)
                self.assertIn('unchecked', out + err)
                self.assertIn(f"{bad['from']}..{bad['to']}", out + err)
                self.assertIn('a verified landing or a fix-forward on the target is needed', out + err)
        self.assertFalse(os.path.exists(self.ship_marker))

    def test_verified_fix_forward_only_clears_ranges_including_its_result(self):
        bad = self.land_slug('unchecked')
        self.record_unverified(bad)
        self.configure(land={'gate': [PY, '-c', 'pass']})
        fixed = self.land_slug('fixed')
        self.plan(f"{bad['from']}..{fixed['to']}")
        self.plan(f"{bad['from']}..{bad['to']}", rc=common.EXIT_REFUSED)

    def test_run_without_execute_never_calls_ship_command(self):
        landed = self.land_slug('a')
        res = self.plan('a')
        self.configure_commands()
        rc, out, err = self.ship('run', 'a', '--manifest', res['manifest'])
        self.assertEqual(rc, 0, err + out)
        self.assertFalse(os.path.exists(self.ship_marker))
        with open(self.dry_marker, encoding='utf-8') as handle:
            dry = json.load(handle)
        copy = ship.Paths(self.repo).copy(res['sha256'])
        self.assertEqual(dry['args'], [copy, landed['to'], 'a', landed['from']])
        self.assertEqual(dry['manifest_sha256'], res['sha256'])
        self.assertEqual(dry['env']['TWINLANE_SHIP_MODE'], 'dry-run')
        self.assertEqual(common.canon_path(dry['cwd']), common.canon_path(self.repo))
        expected = [PY, self.recorder, copy, landed['to'], 'a', 'main', self.ship_marker, '0']
        self.assertIn('ship command (NOT run): ' + json.dumps(expected), out)
        self.assertIn('--execute --confirm ' + res['sha256'][:8], out)
        receipt = common.read_json(self.receipt_path(res['sha256']))
        self.assertEqual((receipt['rc'], receipt['manifest_sha256'], receipt['ship_argv']),
                         (0, res['sha256'], expected))
        self.assertTrue(os.path.exists(res['manifest'] + '.dryrun.json'))
        # with no dry-run command the ship command is still only printed
        self.configure_commands(dry=False)
        rc, out, _ = self.ship('run', '--manifest', res['manifest'])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('ship command (NOT run)', out)
        self.assertFalse(os.path.exists(self.ship_marker))
        with mock.patch.object(ship, '_run_command', side_effect=AssertionError('nothing may start')):
            self.assertEqual(self.ship('run', '--manifest', res['manifest'])[0], common.EXIT_REFUSED)

    def test_execute_needs_fresh_matching_dry_run_and_confirm(self):
        self.land_slug('a')
        res = self.plan('a')
        sha, man = res['sha256'], res['manifest']
        self.configure_commands()

        def execute(*extra):
            return self.ship('run', '--manifest', man, '--execute', *extra)

        rc, _, err = execute('--confirm', sha[:8])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('no dry run', err)
        self.dry_run(man)
        self.assertEqual(execute()[0], common.EXIT_REFUSED)                      # no confirm, no terminal
        self.assertEqual(execute('--confirm', '00000000')[0], common.EXIT_REFUSED)
        receipt = common.read_json(self.receipt_path(sha))
        common.write_json(self.receipt_path(sha), dict(receipt, at='2001-01-01T00:00:00Z'))
        rc, _, err = execute('--confirm', sha[:8])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('older than ship.dry_run_max_age_s', err)
        self.configure_commands(dry_rc=1)
        self.assertEqual(self.ship('run', '--manifest', man)[0], common.EXIT_FAILED)
        self.assertIn('failed (rc 1)', execute('--confirm', sha[:8])[2])
        self.configure_commands()
        self.dry_run(man)
        self.configure({'dry_run_command': [PY, self.recorder, '{manifest}', self.dry_marker, '0'],
                        'command': [PY, self.recorder, '{manifest}', 'changed', self.ship_marker, '0']})
        rc, _, err = execute('--confirm', sha[:8])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('changed since the dry run', err)
        self.configure_commands()
        os.remove(self.receipt_path(sha))
        other = self.plan('a', '--allow-foreign', '--out', self.ws.path('other.json'))
        self.assertNotEqual(other['sha256'], sha)
        self.dry_run(other['manifest'])                  # a passing dry run of ANOTHER manifest
        self.assertIn('no dry run', execute('--confirm', sha[:8])[2])
        self.assertFalse(os.path.exists(self.ship_marker))
        self.dry_run(man)
        rc, out, err = execute('--confirm', sha[:8].upper())
        self.assertEqual(rc, 0, err + out)
        with open(self.ship_marker, encoding='utf-8') as handle:
            shipped = json.load(handle)
        self.assertEqual((shipped['manifest_sha256'], shipped['env']['TWINLANE_SHIP_MODE']), (sha, 'execute'))
        self.assertEqual(shipped['env']['TWINLANE_SHIP_SHA256'], sha)
        self.assertTrue(common.read_json(self.receipt_path(sha))['consumed_at'])
        os.remove(self.ship_marker)
        rc, _, err = execute('--confirm', sha[:8])                                  # one execute per dry run
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('already used', err)
        self.assertFalse(os.path.exists(self.ship_marker))
        # an interactive answer counts as the confirmation
        self.dry_run(man)
        code, result = ship.run(man, repo=self.repo, execute=True, echo=io.StringIO(), ask=lambda _prompt: sha[:8])
        self.assertEqual((code, result['executed']), (0, True))
        events = [e['event'] for e in self.ledger()]
        self.assertIn('execute-refused', events)
        self.assertEqual(events.count('executed'), 2)

    def test_execute_refused_inside_worker(self):
        self.land_slug('a')
        res = self.plan('a')
        self.configure_commands()
        self.dry_run(res['manifest'])
        os.remove(self.dry_marker)
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'job-1'}):
            rc, _, err = self.ship('run', '--manifest', res['manifest'], '--execute', '--confirm', res['sha256'][:8])
            self.assertEqual(rc, common.EXIT_REFUSED)
            self.assertIn('worker', err)
            self.assertEqual(self.ship('run', '--manifest', res['manifest'])[0], common.EXIT_REFUSED)
            with self.assertRaises(ship.ShipError):
                ship.run(res['manifest'], repo=self.repo, execute=True, confirm=res['sha256'][:8])
        self.assertFalse(os.path.exists(self.ship_marker))
        self.assertFalse(os.path.exists(self.dry_marker))
        self.assertIsNone(common.read_json(self.receipt_path(res['sha256'])).get('consumed_at'))

    def test_execute_rehashes_manifest(self):
        self.land_slug('a')
        res = self.plan('a')
        sha, man = res['sha256'], res['manifest']
        self.configure_commands()
        self.dry_run(man)
        # an edited manifest is another manifest: it has no dry run
        with open(man, 'ab') as handle:
            handle.write(b' ')
        rc, _, err = self.ship('run', '--manifest', man, '--execute', '--confirm', sha[:8])
        self.assertEqual(rc, common.EXIT_USAGE)  # not the canonical bytes plan wrote: refused at load
        self.assertIn('canonical form', err)
        with open(man, 'rb') as handle:
            data = handle.read()
        with open(man, 'wb') as handle:
            handle.write(data[:-1])
        self.assertEqual(sha256(data[:-1]), sha)
        # the bytes change after every check: the final re-hash stops the command
        real = ship._ensure_copy
        copy = ship.Paths(self.repo).copy(sha)
        for victim in (copy, man):
            self.dry_run(man)

            def tamper(*args, victim=victim):
                path = real(*args)
                with open(victim, 'ab') as handle:
                    handle.write(b'\n')
                return path

            with self.subTest(victim=victim), mock.patch.object(ship, '_ensure_copy', side_effect=tamper):
                rc, _, err = self.ship('run', '--manifest', man, '--execute', '--confirm', sha[:8])
                self.assertEqual(rc, common.EXIT_REFUSED)
                self.assertIn('changed after it was checked', err)
                self.assertFalse(os.path.exists(self.ship_marker))
            if victim == man:
                with open(man, 'wb') as handle:
                    handle.write(data[:-1])
        # a manifest edited so it no longer matches the repository is refused even with its own dry run
        forged = json.loads(data.decode('utf-8'))
        forged['files'][0]['sha256'] = '0' * 64
        forged_path = self.ws.path('forged.json')
        with open(forged_path, 'wb') as handle:
            handle.write(ship.manifest_bytes(forged))
        rc, _, err = self.ship('run', '--manifest', forged_path)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('does not match the repository', err)
        self.assertFalse(os.path.exists(self.ship_marker))

    def test_only_the_canonical_manifest_bytes_are_accepted(self):
        # The ship command parses the same bytes with its own JSON reader. A duplicate key, or any
        # form plan never writes, could make that reader see a manifest other than the verified one.
        self.land_slug('a')
        res = self.plan('a')
        self.configure_commands()
        with open(res['manifest'], 'rb') as handle:
            good = handle.read()
        text = good.decode('utf-8')
        decoy = '{\n  "files": [],' + text[1:]  # a first "files" key some parsers keep instead of the last
        variants = {
            'duplicate-key': decoy.encode('utf-8'),
            'bom': b'\xef\xbb\xbf' + good,
            'compact': json.dumps(json.loads(text), sort_keys=True).encode('utf-8'),
            'crlf': good.replace(b'\n', b'\r\n'),
            'nan': text.replace('"schema"', '"x": NaN,\n  "schema"', 1).encode('utf-8'),
        }
        for name, data in variants.items():
            path = self.ws.path(f'variant-{name}.json')
            with open(path, 'wb') as handle:
                handle.write(data)
            with self.subTest(variant=name):
                rc, _, err = self.ship('run', '--manifest', path)
                self.assertEqual(rc, common.EXIT_USAGE, err)
        self.assertFalse(os.path.exists(self.dry_marker))
        self.assertFalse(os.path.exists(self.ship_marker))

    def test_unconfigured_prints_and_exits_3(self):
        self.land_slug('a')
        res = self.plan('a')
        for argv in ([], ['--execute', '--confirm', res['sha256'][:8]]):
            with self.subTest(argv=argv):
                rc, out, _ = self.ship('run', '--manifest', res['manifest'], *argv)
                self.assertEqual(rc, common.EXIT_REFUSED)
                self.assertIn(res['manifest'], out)
                self.assertIn('no ship.command is configured', out)
        # a dry-run command alone is not run either: there is nothing it could lead to
        self.configure_commands(command=False)
        rc, out, _ = self.ship('run', '--manifest', res['manifest'], '--json')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(json.loads(out)['configured'], False)
        self.assertFalse(os.path.exists(self.dry_marker))
        self.assertFalse(os.path.exists(self.receipt_path(res['sha256'])))

    def test_placeholders_known_keys_only(self):
        self.land_slug('a')
        res = self.plan('a')
        self.configure({'dry_run_command': [PY, self.recorder, '{manifest}', self.dry_marker, '0'],
                        'command': [PY, self.recorder, '{sha}', self.ship_marker, '0']})
        rc, _, err = self.ship('run', '--manifest', res['manifest'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('unknown placeholder {sha}', err)
        self.assertFalse(os.path.exists(self.dry_marker))
        # values are filled once: braces in a value are never expanded again
        self.configure_commands()
        self.assertEqual(projects.argv(['x', '{slug}'], slug='{repo}'), ['x', '{repo}'])
        # a manifest value cannot smuggle a line break or a brace token into the argv
        data = res['data']
        for field, value, code in (('target', 'main\nrm', common.EXIT_USAGE), ('slug', '{repo}', common.EXIT_USAGE)):
            bad = dict(data, **{field: value})
            path = self.ws.path(f'bad-{field}.json')
            with open(path, 'wb') as handle:
                handle.write(ship.manifest_bytes(bad))
            with self.subTest(field=field):
                rc, _, err = self.ship('run', '--manifest', path)
                self.assertEqual(rc, code, err)
        self.assertFalse(os.path.exists(self.dry_marker))
        self.assertFalse(os.path.exists(self.ship_marker))


if __name__ == '__main__':
    unittest.main()
