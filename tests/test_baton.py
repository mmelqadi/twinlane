"""Baton + handoff tests: temp runtime dirs, fixture transcripts, a throwaway git repo. No network, no models."""
import contextlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import baton, common  # noqa: E402

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'fixtures')
CLAUDE_SID = '00000000-0000-4000-8000-000000000020'
CODEX_SID = '00000000-0000-4000-8000-000000000021'


def _git(cwd, *args):
    subprocess.run(['git', '-C', cwd, '-c', 'user.name=t', '-c', 'user.email=user@example.com', *args],
                   check=True, capture_output=True)


def _set_args(**kw):
    """A Namespace shaped like `twinlane baton set` arguments."""
    fields = dict(next=None, goal=None, title=None, tracker=None, decision=None, why=None, user_quote=None,
                  owner=None, done=None, then=None, accept=None, reject=None, hold=None, grant=None, prohibit=None,
                  evidence=None, accept_status=None, read_first=None, do_not=None, verify=None)
    fields.update(kw)
    return types.SimpleNamespace(**fields)


def _raw(path):
    with open(path, 'rb') as h:
        return h.read()


def _read_fixture(name):
    with open(os.path.join(FIX, name), encoding='utf-8') as h:
        return h.read().splitlines()


def jid(n, engine='claude'):
    """A job id in the format jobs.create_job makes (baton only indexes real ones)."""
    return f'20261009-143941-{engine}-{n:04x}'


class BatonTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-baton-')
        self._saved = (common.STATE, common.JOBS, common.BATONS, baton.CLAUDE_PROJECTS, baton.CODEX_SESSIONS)
        common.STATE = os.path.join(self.tmp, 'state')
        common.JOBS = os.path.join(self.tmp, 'jobs')
        common.BATONS = os.path.join(self.tmp, 'batons')
        baton.CLAUDE_PROJECTS = os.path.join(self.tmp, 'claude-projects')
        baton.CODEX_SESSIONS = os.path.join(self.tmp, 'codex-sessions')
        common.ensure_dirs()
        self.repo = baton.norm_dir(os.path.join(self.tmp, 'repo'))
        os.makedirs(self.repo)
        _git(self.repo, 'init', '-q')
        with open(os.path.join(self.repo, 'a.txt'), 'w', encoding='utf-8') as h:
            h.write('a\n')
        _git(self.repo, 'add', 'a.txt')
        _git(self.repo, 'commit', '-q', '-m', 'init')

    def tearDown(self):
        (common.STATE, common.JOBS, common.BATONS, baton.CLAUDE_PROJECTS, baton.CODEX_SESSIONS) = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def install_transcripts(self):
        """Place the fixtures where the extractor looks for this repo's cwd."""
        cdir = self.claude_dir()
        self.write_claude(CLAUDE_SID, _read_fixture('claude_transcript.jsonl'))
        lines = _read_fixture('codex_rollout.jsonl')
        meta = json.loads(lines[0])
        meta['payload']['cwd'] = self.repo
        lines[0] = json.dumps(meta, ensure_ascii=False)
        xdir = os.path.join(baton.CODEX_SESSIONS, '2026', '10', '08')
        os.makedirs(xdir)
        with open(os.path.join(xdir, f'rollout-2026-10-08T10-00-00-{CODEX_SID}.jsonl'), 'w', encoding='utf-8',
                  newline='\n') as h:
            h.write('\n'.join(lines) + '\n')

    def claude_dir(self):
        cdir = os.path.join(baton.CLAUDE_PROJECTS, baton.claude_dir_name(self.repo))
        os.makedirs(cdir, exist_ok=True)
        return cdir

    def write_claude(self, sid, lines, cwd=None, entrypoint=None, age_s=0):
        """A transcript for this repo: the fixture's cwd (and entrypoint) rewritten."""
        out = []
        for ln in lines:
            obj = json.loads(ln)
            if 'cwd' in obj:
                obj['cwd'] = cwd or self.repo
            if entrypoint:
                obj['entrypoint'] = entrypoint
            out.append(json.dumps(obj, ensure_ascii=False))
        path = os.path.join(self.claude_dir(), sid + '.jsonl')
        with open(path, 'w', encoding='utf-8', newline='\n') as h:
            h.write('\n'.join(out) + '\n')
        if age_s:
            t = os.path.getmtime(path) - age_s
            os.utime(path, (t, t))
        return path

    def write_rollout(self, name, fixture, source=None, originator=None, age_s=0):
        lines = _read_fixture(fixture)
        meta = json.loads(lines[0])
        meta['payload']['cwd'] = self.repo
        if source is not None:
            meta['payload']['source'] = source
        if originator is not None:
            meta['payload']['originator'] = originator
        lines[0] = json.dumps(meta, ensure_ascii=False)
        xdir = os.path.join(baton.CODEX_SESSIONS, '2026', '10', '08')
        os.makedirs(xdir, exist_ok=True)
        path = os.path.join(xdir, f'rollout-2026-10-08T11-00-00-{name}.jsonl')
        with open(path, 'w', encoding='utf-8', newline='\n') as h:
            h.write('\n'.join(lines) + '\n')
        if age_s:
            t = os.path.getmtime(path) - age_s
            os.utime(path, (t, t))
        return path

    def run_cli(self, cmd, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = baton.cli(cmd, argv)
        return rc, out.getvalue(), err.getvalue()


class LegacyKeyTests(BatonTestBase):
    """Batons written by the predecessor tool carry task.asana_gid (a tracker id) and may carry keys this
    version does not know. They must keep working, and saving must never drop any of that data."""

    def write_legacy(self, slug='old'):
        b = baton.empty_baton(slug)
        b['task'].pop('tracker_id')
        b['task'].update({'asana_gid': 'LEGACY-100', 'title': 'Old task', 'goal': 'finish it',
                          'future_field': {'keep': ['me']}})
        b['unknown_top'] = {'note': 'written by a newer tool'}
        with open(baton.baton_path(slug), 'w', encoding='utf-8') as h:
            json.dump(b, h)
        return slug

    def test_legacy_asana_gid_shows_as_tracker(self):
        slug = self.write_legacy()
        b = baton.load(slug)
        self.assertEqual(b['task']['tracker_id'], 'LEGACY-100')
        self.assertIn('(tracker LEGACY-100)', ' '.join(baton.render_baton_section(b)))
        # a tracker_id that is already set wins over the legacy key
        b['task']['tracker_id'] = 'NEW-7'
        baton.save(b, 'user')
        self.assertEqual(baton.load(slug)['task']['tracker_id'], 'NEW-7')

    def test_save_keeps_legacy_and_unknown_task_keys(self):
        slug = self.write_legacy()
        b = baton.load(slug)
        baton.save(baton.apply_set(b, _set_args(next='carry on', decision='keep old keys', why='no data loss')), 'user')
        with open(baton.baton_path(slug), encoding='utf-8') as h:
            raw = json.load(h)
        self.assertEqual(raw['task']['asana_gid'], 'LEGACY-100')
        self.assertEqual(raw['task']['future_field'], {'keep': ['me']})
        self.assertEqual(raw['unknown_top'], {'note': 'written by a newer tool'})
        self.assertEqual(raw['task']['tracker_id'], 'LEGACY-100')
        self.assertEqual(raw['progress']['next_action'], 'carry on')
        self.assertEqual(baton.validate(raw), [])


class SlugAndPathTests(unittest.TestCase):
    def test_slug_rules(self):
        for ok in ('a', 'example-repo', 'x' * 60, '2026-10-08-fix'):
            self.assertTrue(baton.valid_slug(ok), ok)
        for bad in ('', 'Upper', 'under_score', '../x', 'a/b', 'x' * 61, 'sp ace', 'مرحبا', None):
            self.assertFalse(baton.valid_slug(bad), bad)
        with self.assertRaises(baton.BatonError):
            baton.baton_path('../escape')

    def test_claude_dir_name_rule(self):
        self.assertEqual(baton.claude_dir_name('\x43:\\work\\example-repo'), 'C--work-example-repo')
        self.assertEqual(baton.claude_dir_name('\x43:\\Users\\alice\\.codex\\state'), 'C--Users-alice--codex-state')
        self.assertEqual(baton.claude_dir_name('\x43:\\Users\\alice\\worktrees\\wt_1'), 'C--Users-alice-worktrees-wt-1')

    @unittest.skipUnless(common.IS_WIN, 'Git Bash path form is Windows-only')
    def test_norm_dir_accepts_git_bash_paths(self):
        self.assertEqual(baton.norm_dir('/c/work/example-repo'), '\x43:\\work\\example-repo')
        self.assertEqual(baton.norm_dir('\x63:/work/'), '\x43:\\work')

    def test_redaction(self):
        text = baton.redact('key sk-' + 'ant-abcdefghijklmnopqrstu and password: hunter2 and Bearer abcdefghijklmnopqrstuvwxyz')
        self.assertNotIn('hunter2', text)
        self.assertNotIn('abcdefghijklmnopqrstu', text)
        self.assertIn('password: [REDACTED]', text)


class RoundTripTests(BatonTestBase):
    def test_new_set_snap_history(self):
        b = baton.new('demo', 'Ship the baton module\nline2\nline3\nline4', tracker='123', cwd=self.repo,
                      accept=['A1: tests pass', 'handoff fits 8 KB'])
        self.assertEqual(b['task']['goal'].count('\n'), 2)
        self.assertEqual([a['id'] for a in b['task']['acceptance']], ['A1', 'A2'])
        self.assertEqual(b['repo']['worktree'], common.real_path(self.repo))
        with self.assertRaises(baton.BatonError):
            baton.new('demo', 'again')

        b = baton.load('demo')
        baton.save(baton.apply_set(b, _set_args(
            next='run the live check', done=['wrote baton.py'], decision='keep cache-only meter',
            user_quote='use both engines', reject=['full transcript parse -> too slow'],
            hold=['release|may I release?'], grant=['yes release it|release.sh once'],
            evidence=['test|tests/test_baton.py|20 pass'], accept_status=['A1=pass:20 pass'],
            read_first=['DESIGN.md'], do_not=['commit'], owner='claude', prohibit=['no live writes'])), 'user')
        b = baton.load('demo')
        self.assertEqual(b['progress']['next_action'], 'run the live check')
        self.assertEqual(b['task']['acceptance'][0]['status'], 'pass')
        self.assertEqual(b['decisions'][0]['by'], 'user')
        self.assertEqual(b['holds']['approvals_granted'][0]['scope'], 'release.sh once')
        self.assertEqual(b['holds']['approvals_pending'][0]['ask_text'], 'may I release?')
        self.assertEqual(b['owner']['engine'], 'claude')
        self.assertEqual(baton.validate(b), [])

        rc, shown, _ = self.run_cli('baton', ['show', 'demo'])
        self.assertEqual(rc, 0)
        self.assertTrue(shown.rstrip().endswith(baton.FOOTER))
        rc, shown_json, _ = self.run_cli('baton', ['show', 'demo', '--json'])
        self.assertEqual(rc, 0)
        self.assertIn('re-ask before any live write', json.loads(shown_json)['approvals_granted_note'])

        with open(os.path.join(self.repo, 'new.txt'), 'w', encoding='utf-8') as h:
            h.write('x')
        s = baton.snap('demo')
        self.assertEqual(s['reason'], 'checkpoint')
        self.assertTrue(any('new.txt' in d for d in s['repo']['dirty']))
        rc, head = common.git(self.repo, 'rev-parse', 'HEAD')
        self.assertEqual(s['repo']['head'], head)
        self.assertEqual(s['progress']['next_action'], 'run the live check')  # snap keeps the narrative
        self.assertEqual(len(baton.history('demo')), 2)

    def test_history_keeps_last_50(self):
        b = baton.new('hist', 'goal')
        for i in range(55):
            b['progress']['next_action'] = f'step {i}'
            baton.save(b, 'checkpoint')
        rows = baton.history('hist')
        self.assertEqual(len(rows), 50)
        self.assertEqual(rows[-1]['progress']['next_action'], 'step 53')

    def test_accept_status_unknown_id(self):
        b = baton.new('acc', 'goal', accept=['A1: x'])
        with self.assertRaises(baton.BatonError):
            baton.apply_set(b, _set_args(accept_status=['A9=pass']))
        with self.assertRaises(baton.BatonError):
            baton.apply_set(b, _set_args(accept_status=['A1=great']))

    def test_file_replace_validates(self):
        baton.new('file', 'goal')
        bad = os.path.join(self.tmp, 'bad.json')
        common.write_json(bad, {'baton_version': 2, 'slug': 'file', 'owner': {'engine': 'gpt'}})
        rc, _, err = self.run_cli('baton', ['set', 'file', '--file', bad])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('baton_version', err)
        good = os.path.join(self.tmp, 'good.json')
        doc = baton.empty_baton('file')
        doc['progress']['next_action'] = 'from file'
        common.write_json(good, doc)
        rc, _, _ = self.run_cli('baton', ['set', 'file', '--file', good])
        self.assertEqual(rc, 0)
        self.assertEqual(baton.load('file')['progress']['next_action'], 'from file')

    def test_save_truncates_oversized_fields_and_preserves_full_history(self):
        b = baton.new('bounded', 'goal')
        before = len(baton.history('bounded'))
        b['progress']['next_action'] = 'x' * 2000
        with contextlib.redirect_stderr(io.StringIO()) as warning:
            baton.save(b, 'user', strict=True)
        self.assertIn('next_action: 2000', warning.getvalue())
        self.assertEqual(len(baton.load('bounded')['progress']['next_action']), 300)
        self.assertGreater(len(baton.history('bounded')), before)
        self.assertEqual(baton.history('bounded')[-1]['full_text']['baton.progress.next_action'], 'x' * 2000)
        b['progress']['next_action'] = 'next'
        b['progress']['done'] = [f'{i:02d}' + 'x' * 298 for i in range(30)]
        baton.save(b, 'job')  # system writes trim the oldest entries instead of refusing
        self.assertLessEqual(os.path.getsize(baton.baton_path('bounded')), baton.BATON_MAX_BYTES)
        done = baton.load('bounded')['progress']['done']
        self.assertTrue(done[-1].startswith('29'))
        self.assertLess(len(done), 30)

    def test_record_job_never_refused_on_a_full_baton(self):
        b = baton.new('full', 'goal')
        b['progress']['done'] = ['d' * 299 for _ in range(30)]
        baton.save(b, 'job')
        for i in range(25):
            baton.record_job('full', {'id': jid(i, 'codex'), 'engine': 'codex', 'status': 'running', 'name': 'n' * 400})
        b = baton.load('full')
        # Changed with the finding-7 fix: a worker job no longer becomes the baton's owner (was 'codex').
        self.assertIsNone(b['owner']['engine'])
        self.assertIn(jid(24, 'codex'), [f['job'] for f in b['progress']['in_flight']])
        # All 25 live jobs stay listed even though the baton is full of finished history.
        self.assertEqual({jid(i, 'codex') for i in range(25)}, {j['id'] for j in b['jobs']})
        # With real 28-character ids the 25 live entries alone take more than the budget; they are the exempt part.
        self.assertLessEqual(baton._baton_bytes(baton._without_live_work(b)), baton.BATON_MAX_BYTES)
        self.assertEqual(baton.validate(b), [])

    def test_record_job_in_flight_and_owner_untouched(self):
        baton.record_job('jobbed', {'id': jid(1, 'codex'), 'engine': 'codex', 'status': 'running', 'name': 'sweep',
                                    'requested': {'model': 'gpt-6.1-sol'}})
        b = baton.load('jobbed')
        # Changed with the finding-7 fix: registering a worker no longer sets the owner (was 'codex').
        self.assertIsNone(b['owner']['engine'])
        self.assertEqual(b['progress']['in_flight'][0]['check'], f"twinlane result {jid(1, 'codex')}")
        self.assertTrue(b['progress']['in_flight'][0]['never_rerun'])
        baton.record_job('jobbed', {'id': jid(1, 'codex'), 'engine': 'codex', 'status': 'done', 'session_id': 's1'})
        b = baton.load('jobbed')
        self.assertEqual(len(b['jobs']), 1)
        self.assertEqual(b['jobs'][0]['status'], 'done')
        self.assertEqual(b['progress']['in_flight'], [])
        self.assertEqual(b['reason'], 'job')

    def test_list_batons(self):
        baton.new('one', 'g1')
        b = baton.new('two', 'g2')
        b['progress']['next_action'] = 'do two'
        baton.save(b, 'user')
        rows = baton.list_batons()
        self.assertEqual([r['slug'] for r in rows][:1], ['two'])
        self.assertEqual({'slug', 'owner', 'age', 'next_action', 'worktree'} - set(rows[0]), set())

    def test_cli_usage_errors(self):
        self.assertEqual(self.run_cli('baton', ['new', 'Bad_Slug', '--goal', 'x'])[0], common.EXIT_USAGE)
        self.assertEqual(self.run_cli('baton', ['show', 'missing'])[0], common.EXIT_USAGE)
        self.assertEqual(self.run_cli('handoff', [])[0], common.EXIT_USAGE)
        self.assertEqual(self.run_cli('handoff', ['nobaton'])[0], common.EXIT_USAGE)
        self.assertEqual(self.run_cli('baton', ['bogus'])[0], common.EXIT_USAGE)

    def test_jobs_dir_fallback(self):
        d = os.path.join(common.JOBS, '20261008-120000-claude-ab12')
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'name': 'review', 'engine': 'claude', 'cwd': self.repo})
        common.write_json(os.path.join(d, 'status.json'), {'status': 'running', 'pid': os.getpid(),
                                                           'started_at': common.iso(common.now())})
        rows = baton._scan_jobs_dir(10)
        self.assertEqual(rows[0]['status'], 'running')
        self.assertEqual(rows[0]['cwd'], self.repo)


class ResolveApprovalTests(BatonTestBase):
    def holds(self, slug='appr'):
        return baton.load(slug)['holds']

    def setUp(self):
        super().setUp()
        baton.new('appr', 'goal')
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--hold', 'deploy site|may I deploy the site?',
                                                '--hold', 'deploy cron|may I deploy the cron?',
                                                '--hold', 'email the mailing list|may I email the list?'])[0], 0)

    def test_new_holds_get_ids(self):
        self.assertEqual([h['id'] for h in self.holds()['approvals_pending']], ['H1', 'H2', 'H3'])

    def test_grant_by_id_moves_pending_to_granted_with_quote_scope_and_time(self):
        rc, out, _ = self.run_cli('baton', ['set', 'appr', '--resolve', 'H3', '--grant', 'yes, email them | once'])
        self.assertEqual(rc, 0)
        h = self.holds()
        self.assertEqual([x['id'] for x in h['approvals_pending']], ['H1', 'H2'])
        g = h['approvals_granted'][0]
        self.assertEqual((g['id'], g['quote'], g['scope']),
                         ('H3', 'yes, email them | once', 'email the mailing list'))
        self.assertTrue(g['at'])
        self.assertEqual(h['approvals_rejected'], [])

    def test_grant_by_text_prefix_is_case_insensitive(self):
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--resolve', 'EMAIL', '--grant', 'ok'])[0], 0)
        self.assertEqual(self.holds()['approvals_granted'][0]['id'], 'H3')
        # the ask text is matched too
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--resolve', 'may i deploy the c', '--grant', 'ok'])[0], 0)
        self.assertEqual(self.holds()['approvals_granted'][1]['id'], 'H2')

    def test_deny_moves_pending_to_rejected(self):
        rc, _, _ = self.run_cli('baton', ['set', 'appr', '--resolve', 'H1', '--deny', 'not today'])
        self.assertEqual(rc, 0)
        h = self.holds()
        self.assertEqual(h['approvals_granted'], [])
        self.assertEqual((h['approvals_rejected'][0]['id'], h['approvals_rejected'][0]['reason']), ('H1', 'not today'))
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--resolve', 'H2', '--deny'])[0], 0)
        self.assertIsNone(self.holds()['approvals_rejected'][1]['reason'])

    def test_ambiguous_prefix_refuses_lists_candidates_and_changes_nothing(self):
        before = _raw(baton.baton_path('appr'))
        rc, _, err = self.run_cli('baton', ['set', 'appr', '--resolve', 'deploy', '--grant', 'yes'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('several', err)
        self.assertIn('[H1] deploy site', err)
        self.assertIn('[H2] deploy cron', err)
        self.assertNotIn('email the mailing list', err)
        self.assertEqual(_raw(baton.baton_path('appr')), before)

    def test_no_match_lists_what_is_pending(self):
        rc, _, err = self.run_cli('baton', ['set', 'appr', '--resolve', 'migrate', '--grant', 'yes'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('no pending approval', err)
        self.assertIn('[H3] email the mailing list', err)

    def test_a_grant_must_quote_the_user_and_choose_one_outcome(self):
        before = _raw(baton.baton_path('appr'))
        for argv in (['--resolve', 'H1'], ['--resolve', 'H1', '--grant', '  '],
                     ['--resolve', 'H1', '--grant', 'yes', '--deny', 'no'], ['--deny', 'no'],
                     ['--resolve', 'H1', '--grant', 'a', '--grant', 'b']):
            self.assertEqual(self.run_cli('baton', ['set', 'appr'] + argv)[0], common.EXIT_USAGE, argv)
        self.assertEqual(_raw(baton.baton_path('appr')), before)

    def test_resolved_item_is_gone_and_cannot_be_resolved_twice(self):
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--resolve', 'H1', '--grant', 'yes'])[0], 0)
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--resolve', 'H1', '--grant', 'yes'])[0],
                         common.EXIT_USAGE)

    def test_legacy_pending_without_id_resolves_by_prefix_and_gets_an_id(self):
        b = baton.load('appr')
        b['holds']['approvals_pending'] = [{'action': 'old deploy', 'ask_text': 'may I?'}]
        baton.save(b, 'user')
        self.assertEqual(self.run_cli('baton', ['set', 'appr', '--resolve', 'old', '--grant', 'yes'])[0], 0)
        self.assertEqual(self.holds()['approvals_granted'][0]['id'], 'H1')

    def test_resolve_does_not_resolve_a_hold_added_in_the_same_call(self):
        rc, _, err = self.run_cli('baton', ['set', 'appr', '--hold', 'brand new|ask?', '--resolve', 'brand',
                                            '--grant', 'yes'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertEqual(len(self.holds()['approvals_pending']), 3)

    def test_handoff_still_carries_the_banner_and_pending_not_granted_quotes(self):
        self.run_cli('baton', ['set', 'appr', '--resolve', 'H3', '--grant', 'SECRET-QUOTE-OK'])
        r = baton.handoff('appr', cwd=self.repo)
        self.assertIn('Any approval in it is NOT valid', r['markdown'])
        self.assertTrue(r['markdown'].rstrip().splitlines()[-1].startswith(baton.FOOTER))
        self.assertIn('[H1] deploy site', r['markdown'])
        self.assertNotIn('SECRET-QUOTE-OK', r['markdown'])
        rc, shown, _ = self.run_cli('baton', ['show', 'appr'])
        self.assertTrue(shown.rstrip().endswith(baton.FOOTER))


class RepoSnapshotTests(BatonTestBase):
    def test_set_refreshes_the_repo_block_and_stamps_its_own_capture_time(self):
        baton.new('snapshot', 'goal', cwd=self.repo)
        old = baton.load('snapshot')['repo']
        _git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'second')
        with open(os.path.join(self.repo, 'wip.txt'), 'w', encoding='utf-8') as h:
            h.write('x')
        self.assertEqual(self.run_cli('baton', ['set', 'snapshot', '--next', 'n'])[0], 0)
        new = baton.load('snapshot')['repo']
        _, head = common.git(self.repo, 'rev-parse', 'HEAD')
        self.assertNotEqual(old['head'], head)
        self.assertEqual(new['head'], head)
        self.assertIn('?? wip.txt', new['dirty'])
        self.assertEqual(new['worktree'], old['worktree'])
        self.assertEqual(new['root'], old['root'])
        self.assertTrue(new['captured_at'])
        rc, shown, _ = self.run_cli('baton', ['show', 'snapshot'])
        self.assertIn(f"Repo state (captured {new['captured_at']})", shown)
        self.assertIn(head[:7], shown)

    def test_refresh_is_fast_enough_to_run_on_every_set(self):
        import time
        baton.new('speed', 'goal', cwd=self.repo)
        b = baton.load('speed')
        t = time.perf_counter()
        for _ in range(3):
            self.assertTrue(baton.refresh_repo(b))
        self.assertLess((time.perf_counter() - t) / 3, 1.0)  # about 50 ms here; the bound only catches a hang

    def test_legacy_baton_without_capture_time_is_labelled_honestly(self):
        b = baton.new('legacy', 'goal')
        b['repo'] = {'head': 'e445ad8', 'branch': 'master'}
        baton.save(b, 'user')
        rc, shown, _ = self.run_cli('baton', ['show', 'legacy'])
        self.assertIn('capture time not recorded; baton updated', shown)
        self.assertNotIn('Repo state (at', shown)

    def test_removed_worktree_keeps_the_block_and_set_still_works(self):
        baton.new('gone', 'goal', cwd=self.repo)
        shutil.rmtree(self.repo, ignore_errors=True)
        before = baton.load('gone')['repo']
        self.assertEqual(self.run_cli('baton', ['set', 'gone', '--next', 'n'])[0], 0)
        self.assertEqual(baton.load('gone')['repo'], before)

    def test_baton_without_a_repo_is_not_given_the_current_directory(self):
        baton.new('norepo', 'goal')
        self.assertEqual(self.run_cli('baton', ['set', 'norepo', '--next', 'n'])[0], 0)
        r = baton.load('norepo')['repo']
        self.assertIsNone(r['head'])
        self.assertIsNone(r['captured_at'])

    def test_porcelain_v2_parsing(self):
        text = '\n'.join(['# branch.oid abc123', '# branch.head main', '# branch.upstream origin/main',
                          '# branch.ab +2 -1', '1 .M N... 100644 100644 100644 h h src/a b.py',
                          '1 A. N... 000000 100644 100644 h h new.py',
                          '2 R. N... 100644 100644 100644 h h R100 new name.py\told.py',
                          'u UU N... 1 2 3 4 h1 h2 h3 conflict.py', '? untracked.txt', '! ignored.txt'])
        st = baton._porcelain_v2(text)
        self.assertEqual((st['head'], st['branch'], st['upstream'], st['ahead']), ('abc123', 'main', 'origin/main', 2))
        self.assertEqual(st['dirty'], [' M src/a b.py', 'A  new.py', 'R  old.py -> new name.py', 'UU conflict.py',
                                       '?? untracked.txt'])
        st = baton._porcelain_v2('# branch.oid (initial)\n# branch.head (detached)')
        self.assertEqual((st['head'], st['branch']), (None, 'HEAD'))

    def test_pushed_state_follows_the_upstream(self):
        baton.new('pushed', 'goal', cwd=self.repo)
        b = baton.load('pushed')
        baton.refresh_repo(b)
        self.assertEqual(b['repo']['pushed'], 'no upstream')
        remote = os.path.join(self.tmp, 'remote.git')
        subprocess.run(['git', 'init', '-q', '--bare', remote], check=True, capture_output=True)
        _, branch = common.git(self.repo, 'rev-parse', '--abbrev-ref', 'HEAD')
        _git(self.repo, 'remote', 'add', 'origin', remote)
        _git(self.repo, 'push', '-q', '-u', 'origin', branch)
        baton.refresh_repo(b)
        self.assertEqual(b['repo']['pushed'], f'in origin/{branch}')
        _git(self.repo, 'commit', '-q', '--allow-empty', '-m', 'ahead')
        baton.refresh_repo(b)
        self.assertEqual(b['repo']['pushed'], f'1 commit(s) ahead of origin/{branch}')
        # the same text the full snap produces
        self.assertEqual(baton.snap('pushed')['repo']['pushed'], b['repo']['pushed'])


def OLD(i):
    return jid(0x1000 + i)   # finished-history ids sort apart from the live ones below


def LIVE(i):
    return jid(0x2000 + i, 'codex')


LIVE_STATUSES = ('queued', 'running', 'orphaned', 'unknown', 'stop_failed')  # every one must survive the caps


class LiveWorkCapTests(BatonTestBase):
    """The list caps trim finished history only; unfinished jobs are never dropped."""

    def live_baton(self, n_live, n_finished):
        b = baton.empty_baton('caps')
        b['jobs'] = [{'id': OLD(i), 'engine': 'claude', 'status': 'done'} for i in range(n_finished)]
        live = [LIVE(i) for i in range(n_live)]
        b['jobs'] += [{'id': jid, 'engine': 'codex', 'status': LIVE_STATUSES[i % len(LIVE_STATUSES)]}
                      for i, jid in enumerate(live)]
        b['progress']['in_flight'] = [{'what': 'w', 'job': jid, 'check': f'twinlane result {jid}', 'never_rerun': True}
                                      for jid in live]
        return b, live

    def test_apply_caps_keeps_every_unfinished_job_and_trims_finished_history(self):
        b, live = self.live_baton(21, 30)
        baton._apply_caps(b)
        ids = [j['id'] for j in b['jobs']]
        self.assertEqual([i for i in ids if i in live], live)
        self.assertEqual([i for i in ids if i not in live], [OLD(i) for i in range(10, 30)])
        self.assertEqual([f['job'] for f in b['progress']['in_flight']], live)

    def test_save_keeps_every_live_job_and_the_size_budget(self):
        b, live = self.live_baton(21, 30)
        b['progress']['done'] = [f'{i:02d}' + 'x' * 100 for i in range(30)]
        baton.save(b, 'job')
        saved = baton.load('caps')
        self.assertTrue(set(live) <= {j['id'] for j in saved['jobs']})
        self.assertLessEqual(os.path.getsize(baton.baton_path('caps')), baton.BATON_MAX_BYTES)
        self.assertLess(len([j for j in saved['jobs'] if j['id'] not in live]), 30)  # history went first

    def test_record_job_25_live_jobs_all_discoverable(self):
        for i in range(25):
            baton.record_job('many', {'id': jid(i), 'engine': 'claude' if i % 2 else 'codex',
                                      'status': 'running', 'name': f'worker {i}'})
        b = baton.load('many')
        want = {jid(i) for i in range(25)}
        self.assertEqual(want, {j['id'] for j in b['jobs']})
        self.assertEqual(want, {f['job'] for f in b['progress']['in_flight']} | {j['id'] for j in b['jobs']})
        text = '\n'.join(baton.render_baton_section(b))
        for want_id in want:
            self.assertIn(want_id, text)  # `baton show` and the handoff list every one, not the newest 5

    def test_live_jobs_alone_over_budget_are_kept_and_set_still_works(self):
        b, live = self.live_baton(90, 0)  # 90 live jobs do not fit in 8 KB
        baton.save(b, 'job')
        saved = baton.load('caps')
        self.assertTrue(set(live) <= {j['id'] for j in saved['jobs']})
        self.assertGreater(os.path.getsize(baton.baton_path('caps')), baton.BATON_MAX_BYTES)
        rc, _, err = self.run_cli('baton', ['set', 'caps', '--next', 'still settable'])
        self.assertEqual((rc, err), (0, ''))
        saved = baton.load('caps')
        self.assertEqual(saved['progress']['next_action'], 'still settable')
        self.assertTrue(set(live) <= {j['id'] for j in saved['jobs']})

    def test_live_exemption_does_not_excuse_other_oversize(self):
        b, _ = self.live_baton(25, 0)
        b['holds']['prohibitions'] = ['p' * 299 for _ in range(30)]  # untrimmable, not live work
        with self.assertRaisesRegex(baton.BatonError, 'bytes; maximum'):
            baton.save(b, 'user', strict=True)

    def test_baton_finished_statuses_match_the_job_runner(self):
        from twinlane import jobs
        self.assertEqual(set(baton.FINISHED_JOB_STATUSES), set(jobs.TERMINAL))


class LeadOwnerTests(BatonTestBase):
    """The owner is the lead; registering or finishing a worker job never changes it."""
    LEAD = {'engine': 'codex', 'session_id': 'lead-session', 'model': 'gpt-6.1-sol'}

    def lead_baton(self, slug='lead'):
        b = baton.new(slug, 'goal')
        b['owner'] = dict(self.LEAD)
        baton.save(b, 'user')

    def test_worker_starts_and_completions_leave_the_lead_alone(self):
        self.lead_baton()
        c1, x1, c2, x2 = jid(1), jid(1, 'codex'), jid(2), jid(2, 'codex')
        steps = [(c1, 'claude', 'running'), (x1, 'codex', 'running'), (c2, 'claude', 'running'),
                 (c1, 'claude', 'done'), (x2, 'codex', 'running'), (x1, 'codex', 'limit_hit'),
                 (c2, 'claude', 'failed'), (x2, 'codex', 'done')]
        for job, engine, status in steps:
            baton.record_job('lead', {'id': job, 'engine': engine, 'status': status, 'name': job,
                                      'session_id': f'sess-{job}', 'served_models': [f'model-{engine}']})
            self.assertEqual(baton.load('lead')['owner'], self.LEAD, (job, status))
        self.assertEqual(len(baton.load('lead')['jobs']), 4)

    def test_snap_and_handoff_leave_the_lead_alone(self):
        self.lead_baton()
        baton.record_job('lead', {'id': jid(1), 'engine': 'claude', 'status': 'running', 'session_id': 's'})
        baton.snap('lead', self.repo)
        self.assertEqual(baton.load('lead')['owner'], self.LEAD)
        baton.handoff('lead', cwd=self.repo, to='claude')
        self.assertEqual(baton.load('lead')['owner'], self.LEAD)

    def test_explicit_owner_change_still_works_with_workers_running(self):
        self.lead_baton()
        baton.record_job('lead', {'id': jid(1), 'engine': 'claude', 'status': 'running', 'session_id': 'w'})
        rc, _, err = self.run_cli('baton', ['set', 'lead', '--owner', 'claude'])
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(baton.load('lead')['owner']['engine'], 'claude')
        baton.record_job('lead', {'id': jid(1, 'codex'), 'engine': 'codex', 'status': 'running'})
        self.assertEqual(baton.load('lead')['owner']['engine'], 'claude')  # and the new lead stays put too
        rc, _, _ = self.run_cli('baton', ['set', 'lead', '--owner', 'codex'])
        self.assertEqual(baton.load('lead')['owner']['engine'], 'codex')

    def test_a_first_job_does_not_set_an_unset_owner(self):
        baton.record_job('nolead', {'id': jid(1), 'engine': 'claude', 'status': 'running', 'session_id': 'w'})
        baton.record_job('nolead', {'id': jid(1), 'engine': 'claude', 'status': 'done'})
        self.assertEqual(baton.load('nolead')['owner'], {'engine': None, 'session_id': None, 'model': None})
        self.assertEqual(baton.list_batons()[0]['owner'], None)

    def test_concurrent_worker_registration_leaves_the_lead_alone(self):
        self.lead_baton('race-lead')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from twinlane import baton, common\n'
                'common.BATONS = sys.argv[2]\n'
                'engine = sys.argv[3]\n'
                'for i in range(4):\n'
                '    jid = f"20261009-143941-{engine}-{i:04x}"\n'
                '    baton.record_job("race-lead", {"id": jid, "engine": engine, "status": "running", "session_id": jid})\n'
                '    baton.record_job("race-lead", {"id": jid, "engine": engine, "status": "done"})')
        env = dict(os.environ, TWINLANE_HOME=os.path.join(self.tmp, 'sub-home'))
        procs = [subprocess.Popen([sys.executable, '-c', code, root, common.BATONS, engine], env=env)
                 for engine in ('claude', 'codex', 'claude', 'codex')]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        b = baton.load('race-lead')
        self.assertEqual(b['owner'], self.LEAD)
        self.assertEqual(len(b['jobs']), 8)  # claude 0..3 and codex 0..3, each registered twice


def _ids_in(text):
    return set(re.findall(r'\d{8}-\d{6}-(?:claude|codex)-[0-9a-f]{4}', text))


class JobIdVisibilityTests(BatonTestBase):
    """A handoff names every unfinished job id, or counts the ones it cannot list
    and says where to find them. A long description or a long list never hides one silently."""

    def ctx_for(self, b, jobs=()):
        return {'slug': b['slug'], 'generated_at': 'now', 'baton': b, 'jobs': list(jobs), 'cwd': self.repo,
                'git': {}, 'verify': ['x']}

    def many_live(self, slug, n):
        b = baton.empty_baton(slug)
        ids = [jid(i, 'codex' if i % 2 else 'claude') for i in range(n)]
        b['jobs'] = [{'id': i, 'engine': 'codex', 'status': 'running'} for i in ids]
        b['progress']['in_flight'] = [{'what': 'worker', 'job': i, 'check': f'twinlane result {i}', 'never_rerun': True}
                                      for i in ids]
        return b, ids

    def test_job_id_regex_is_the_one_the_runner_makes(self):
        from twinlane import jobs
        for engine in baton.ENGINES:
            # Id allocation is independent of the inherited real worker registry.
            with mock.patch.object(jobs, '_coordination_parent', return_value=None):
                made = jobs.create_job({'engine': engine, 'profile': 'intel', 'brief_text': 'x'})
            self.assertRegex(made, baton.JOB_ID_RE)
        for bad in ('j1', 'live-00', '20261009-143941-gemini-abcd', '20261009-143941-codex-abcde',
                    '20261009-143941-codex-ABCD', ''):
            self.assertIsNone(baton.JOB_ID_RE.match(bad), bad)

    def test_long_description_never_hides_the_job_id(self):
        baton.new('longdesc', 'goal', cwd=self.repo)
        baton.record_job('longdesc', {'id': jid(1), 'engine': 'claude', 'status': 'running', 'name': 'w' * 300})
        b = baton.load('longdesc')
        self.assertIn(jid(1), '\n'.join(baton.render_baton_section(b)))
        self.assertIn(f'twinlane result {jid(1)}', '\n'.join(baton.render_baton_section(b)))
        r = baton.handoff('longdesc', cwd=self.repo, to='codex')
        self.assertIn(jid(1), r['markdown'])
        self.assertIn(f'twinlane result {jid(1)}', r['markdown'])
        # A description of any length, in every pointer shown
        b, ids = self.many_live('longmany', 5)
        for f in b['progress']['in_flight']:
            f['what'] = 'x' * 300
        text = '\n'.join(baton.render_baton_section(b))
        for i in ids:
            self.assertIn(f'{i}: ', text)
            self.assertIn(f'twinlane result {i}', text)

    def test_300_unfinished_jobs_every_id_listed_or_counted_with_where_to_find_them(self):
        b, ids = self.many_live('crowd', 300)
        baton.save(b, 'job')
        b = baton.load('crowd')
        self.assertEqual({j['id'] for j in b['jobs']}, set(ids))      # the baton itself keeps all 300
        text = baton.fit_handoff(self.ctx_for(b))
        self.assertLessEqual(len(text.encode('utf-8')), baton.HANDOFF_MAX_BYTES)
        missing = set(ids) - _ids_in(text)
        counts = [int(n) for n in re.findall(r'\+(\d+) more unfinished jobs: `twinlane baton show crowd`', text)]
        self.assertTrue(missing)                                     # 300 ids cannot all fit in 8 KB ...
        self.assertEqual(counts, [len(missing)])                     # ... so the count of those left out is exact
        self.assertNotIn('clipped to fit', text)
        rc, shown, _ = self.run_cli('baton', ['show', 'crowd'])      # and `twinlane baton show` really lists them all
        self.assertEqual((rc, _ids_in(shown)), (0, set(ids)))

    def test_every_id_is_listed_when_they_fit(self):
        b, ids = self.many_live('few', 12)
        text = baton.fit_handoff(self.ctx_for(b))
        self.assertEqual(_ids_in(text), set(ids))
        self.assertNotIn('more unfinished jobs', text)

    def test_also_live_list_is_bounded_with_its_own_overflow(self):
        live = [{'id': jid(i), 'engine': 'claude', 'status': 'running', 'name': 'n'} for i in range(120)]
        ctx = self.ctx_for(baton.empty_baton('livelist'), live)
        ctx['baton'] = None
        text = baton.fit_handoff(ctx)
        self.assertLessEqual(len(text.encode('utf-8')), baton.HANDOFF_MAX_BYTES)
        counts = [int(n) for n in re.findall(r'\+(\d+) more unfinished jobs: `twinlane jobs --all`', text)]
        self.assertEqual(counts, [len(set(jid(i) for i in range(120)) - _ids_in(text))])
        self.assertGreater(counts[0], 0)

    def test_the_last_resort_clip_never_cuts_inside_an_id_list(self):
        b, ids = self.many_live('tiny', 300)
        for budget in (4000, 2500, 1800):
            text = baton.fit_handoff(self.ctx_for(b), max_bytes=budget)
            self.assertLessEqual(len(text.encode('utf-8')), budget)
            missing = set(ids) - _ids_in(text)
            counts = [int(n) for n in re.findall(r'\+(\d+) more unfinished jobs', text)]
            self.assertEqual(counts, [len(missing)], budget)
            for line in text.splitlines():                           # no id is cut in half
                for part in re.findall(r'\d{8}-\d{6}-(?:claude|codex)-\w*', line):
                    self.assertRegex(part, baton.JOB_ID_RE)

    def test_overflow_line_is_present_when_even_zero_ids_must_go(self):
        b, ids = self.many_live('zero', 300)
        old = baton.HANDOFF_TIERS
        try:
            baton.HANDOFF_TIERS = ((120, 0),)
            text = baton.fit_handoff(self.ctx_for(b))
        finally:
            baton.HANDOFF_TIERS = old
        self.assertIn('+295 more unfinished jobs: `twinlane baton show zero`', text)   # 300 minus the 5 pointers shown


class JobEntrySchemaTests(BatonTestBase):
    """Only a real, unfinished job counts as live work for the size exemption."""

    def junk(self, n):
        b = baton.empty_baton('junk')
        b['jobs'] = [{'blob': 'x' * 290} for _ in range(n)]
        return b

    def test_entries_that_are_not_jobs_are_refused_and_get_no_exemption(self):
        for n in (30, 100):
            b = self.junk(n)
            self.assertGreater(baton._baton_bytes(b), baton.BATON_MAX_BYTES)
            errs = baton.validate(b)
            self.assertTrue(any('bad job entry' in e for e in errs), (n, errs[:2]))
            self.assertTrue(any('bytes; maximum' in e for e in errs), (n, errs[-1]))
            with self.assertRaisesRegex(baton.BatonError, 'invalid baton'):
                baton.save(b, 'user', strict=True)
            self.assertFalse(os.path.exists(baton.baton_path('junk')))
        self.assertEqual(baton._without_live_work(self.junk(30))['jobs'], self.junk(30)['jobs'])

    def test_each_kind_of_bad_entry_is_refused(self):
        good = {'id': jid(1), 'engine': 'codex', 'status': 'running'}
        bad = [{'id': 'j1', 'status': 'running'}, {'status': 'running'}, {'id': None}, 'job', 7,
               dict(good, blob='x'), dict(good, session_id='s' * 101), dict(good, session_id=['a']),
               dict(good, status={'a': 1}), {'id': jid(2).upper(), 'status': 'running'}]
        for entry in bad:
            b = baton.empty_baton('bad')
            b['jobs'] = [entry]
            self.assertTrue(any('bad job entry' in e for e in baton.validate(b)), entry)
        b = baton.empty_baton('bad')
        b['jobs'] = [good, dict(good)]
        self.assertTrue(any('listed twice' in e for e in baton.validate(b)))
        b['jobs'] = [good]
        self.assertEqual(baton.validate(b), [])

    def test_90_real_live_jobs_still_pass_and_a_blob_cannot_hide_among_them(self):
        b = baton.empty_baton('real')
        ids = [jid(i, 'codex') for i in range(90)]
        b['jobs'] = [{'id': i, 'engine': 'codex', 'status': 'running', 'session_id': 's' * 100} for i in ids]
        b['progress']['in_flight'] = [{'what': 'w', 'job': i, 'check': f'twinlane result {i}', 'never_rerun': True}
                                      for i in ids]
        self.assertGreater(baton._baton_bytes(b), baton.BATON_MAX_BYTES)
        self.assertEqual(baton.validate(b), [])
        baton.save(b, 'user', strict=True)
        self.assertEqual({j['id'] for j in baton.load('real')['jobs']}, set(ids))
        # An entry with an extra field is not excused, however many real jobs sit beside it
        b['jobs'].append({'id': jid(500), 'engine': 'codex', 'status': 'running', 'blob': 'x' * 290})
        self.assertTrue(any('bad job entry' in e for e in baton.validate(b)))
        # extra fields on an in-flight pointer are counted too (not excused as live work)
        c = baton.load('real')
        c['progress']['in_flight'][0]['blob'] = ['y' * 290] * 40
        self.assertTrue(any('bytes; maximum' in e for e in baton.validate(c)))
        # one pointer per live job is excused: a pointer repeated 100 times is not
        d = baton.load('real')
        d['progress']['in_flight'] = d['progress']['in_flight'][:1] * 100
        self.assertEqual(len(baton._without_live_work(d)['progress']['in_flight']), 99)

    def test_the_exemption_has_a_ceiling(self):
        b = baton.empty_baton('ceiling')
        b['jobs'] = [{'id': jid(i, 'codex'), 'engine': 'codex', 'status': 'running', 'session_id': 's' * 100}
                     for i in range(baton.LIVE_EXEMPT_MAX + 50)]
        self.assertEqual(len(baton._without_live_work(b)['jobs']), 50)
        self.assertTrue(any('bytes; maximum' in e for e in baton.validate(b)))

    def test_finished_and_unknown_statuses(self):
        done, odd = {'id': jid(1), 'status': 'done'}, {'id': jid(2), 'status': 'weird'}
        self.assertFalse(baton._is_live_job(done))
        self.assertTrue(baton._is_live_job(odd))                   # unknown states are unfinished work
        self.assertFalse(baton._is_live_job({'status': 'running'}))

    def test_system_writers_drop_non_jobs_and_keep_real_ones(self):
        b = baton.empty_baton('sys')
        b['jobs'] = [{'blob': 'x'}, 'junk', {'id': jid(3), 'status': 'running', 'blob': 'z', 'session_id': 's' * 500},
                     {'id': jid(3), 'status': 'done'}]
        baton.save(b, 'job')                                       # a system write never refuses
        saved = baton.load('sys')['jobs']
        self.assertEqual([j['id'] for j in saved], [jid(3)])
        self.assertEqual(saved[0]['status'], 'running')             # first entry of a repeated id wins
        self.assertNotIn('blob', saved[0])
        self.assertEqual(len(saved[0]['session_id']), 100)          # cut to the field bound, job kept
        baton.record_job('sys', {'id': 'not-a-job-id', 'engine': 'codex', 'status': 'running'})
        self.assertEqual([j['id'] for j in baton.load('sys')['jobs']], [jid(3)])


class JobReconcileTests(BatonTestBase):
    def job(self, jid, status, pid=None):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'cwd': self.repo, 'engine': 'claude'})
        if status:
            common.write_json(os.path.join(d, 'result.json'), {'id': jid, 'status': status, 'engine': 'claude'})
        else:
            common.write_json(os.path.join(d, 'status.json'), {'status': 'running', 'pid': pid, 'engine': 'claude',
                                                              'created_at': common.iso(common.now())})

    def running_in_baton(self, slug, *jids):
        for jid in jids:
            baton.record_job(slug, {'id': jid, 'engine': 'claude', 'status': 'running', 'name': jid})
        return baton.load(slug)

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        return p.pid

    def check(self, slug, jid, status):
        b = baton.load(slug)
        self.assertEqual({j['id']: j['status'] for j in b['jobs']}[jid], status)
        self.assertNotIn(jid, [f['job'] for f in b['progress']['in_flight']])

    def test_snap_reconciles_finished_stopped_and_lost_jobs(self):
        baton.new('rec', 'goal', cwd=self.repo)
        done, stopped, lost, alive = jid(1), jid(2), jid(3), jid(4)
        self.running_in_baton('rec', done, stopped, lost, alive)
        self.job(done, 'done')
        self.job(stopped, 'stopped')
        self.job(lost, None, pid=self.dead_pid())
        self.job(alive, None, pid=os.getpid())
        baton.snap('rec')
        self.check('rec', done, 'done')
        self.check('rec', stopped, 'stopped')
        self.check('rec', lost, 'lost')
        b = baton.load('rec')
        self.assertEqual([f['job'] for f in b['progress']['in_flight']], [alive])
        self.assertEqual({j['id']: j['status'] for j in b['jobs']}[alive], 'running')

    def test_snap_keeps_an_unknown_job_live_in_flight(self):
        # A runner whose identity cannot be read is not lost, so its baton item stays in flight.
        baton.new('unk', 'goal', cwd=self.repo)
        self.running_in_baton('unk', jid(9))
        self.job(jid(9), None, pid=os.getpid())
        with mock.patch.object(common, 'pid_state', return_value=common.UNKNOWN):
            baton.snap('unk')
        b = baton.load('unk')
        self.assertEqual({j['id']: j['status'] for j in b['jobs']}[jid(9)], 'unknown')
        self.assertEqual([f['job'] for f in b['progress']['in_flight']], [jid(9)])

    def test_set_and_record_job_reconcile_too(self):
        baton.new('rec2', 'goal')
        self.running_in_baton('rec2', jid(1), jid(2))
        self.job(jid(1), 'stopped')
        self.job(jid(2), 'done')
        self.assertEqual(self.run_cli('baton', ['set', 'rec2', '--next', 'n'])[0], 0)
        self.check('rec2', jid(1), 'stopped')
        self.check('rec2', jid(2), 'done')
        self.running_in_baton('rec2', jid(3))
        self.job(jid(3), 'failed')
        baton.record_job('rec2', {'id': jid(4, 'codex'), 'engine': 'codex', 'status': 'running'})
        self.check('rec2', jid(3), 'failed')

    def test_show_reconciles_the_view_without_writing(self):
        baton.new('rec3', 'goal')
        self.running_in_baton('rec3', jid(1))
        self.job(jid(1), 'stopped')
        raw = _raw(baton.baton_path('rec3'))
        rc, out, _ = self.run_cli('baton', ['show', 'rec3', '--json'])
        shown = json.loads(out)
        self.assertEqual(shown['jobs'][0]['status'], 'stopped')
        self.assertEqual(shown['progress']['in_flight'], [])
        self.assertEqual(_raw(baton.baton_path('rec3')), raw)
        rc, text, _ = self.run_cli('baton', ['show', 'rec3'])
        self.assertNotIn('In flight', text)

    def test_unknown_jobs_and_manual_in_flight_items_are_left_alone(self):
        baton.new('rec4', 'goal')
        b = baton.load('rec4')
        elsewhere = jid(77, 'codex')   # a valid id with no job folder on this host
        b['jobs'] = [{'id': elsewhere, 'engine': 'codex', 'status': 'running'}]
        b['progress']['in_flight'] = [{'what': 'manual', 'check': 'ask the user'},
                                      {'what': 'x', 'job': elsewhere, 'check': 'c'}]
        baton.save(b, 'user')
        baton.snap('rec4')
        b = baton.load('rec4')
        self.assertEqual(b['jobs'][0]['status'], 'running')
        self.assertEqual(len(b['progress']['in_flight']), 2)

    def test_handoff_and_snap_keep_every_live_job_of_the_cwd(self):
        # 22 live jobs in the jobs dir + finished history in the baton.
        baton.new('busy', 'goal', cwd=self.repo)
        b = baton.load('busy')
        b['jobs'] = [{'id': OLD(i), 'engine': 'claude', 'status': 'done'} for i in range(25)]
        baton.save(b, 'user')
        live = [f'20261009-1200{i:02d}-claude-ab{i:02d}' for i in range(22)]
        for jid in live:
            self.job(jid, None, pid=os.getpid())
        baton.snap('busy')
        saved = baton.load('busy')
        self.assertTrue(set(live) <= {j['id'] for j in saved['jobs']})
        r = baton.handoff('busy', cwd=self.repo, to='codex')
        for jid in live:
            self.assertIn(jid, r['markdown'])  # the brief names every one, not the first 5 / 8
        self.assertLessEqual(r['bytes'], baton.HANDOFF_MAX_BYTES)
        saved = baton.load('busy')
        self.assertTrue(set(live) <= {j['id'] for j in saved['jobs']})
        self.assertEqual(saved['reason'], 'handoff')
        finished = [j['id'] for j in saved['jobs'] if j['id'] not in live]
        self.assertLess(len(finished), 25)
        self.assertNotIn(OLD(0), finished)  # the oldest finished entries were what got trimmed

    def test_in_flight_item_for_a_job_missing_from_jobs_list_is_dropped_when_finished(self):
        baton.new('rec5', 'goal')
        b = baton.load('rec5')
        b['progress']['in_flight'] = [{'what': 'x', 'job': jid(9), 'check': f'twinlane result {jid(9)}'}]
        baton.save(b, 'user')
        self.job(jid(9), 'done')
        baton.snap('rec5')
        self.assertEqual(baton.load('rec5')['progress']['in_flight'], [])


class ExtractionTests(unittest.TestCase):
    def test_claude_lines(self):
        info = baton.parse_claude_lines(_read_fixture('claude_transcript.jsonl'))
        self.assertEqual(info['assistant'], ['Assistant message two: tests written, 12 pass.',
                                             'Assistant message three: handoff renders under 8 KB.',
                                             'Assistant message four: next step is the live read-only check.'])
        self.assertTrue(info['user'].startswith('Now render the handoff.'))
        self.assertNotIn('system-reminder', info['user'])
        self.assertIn('شكرا', info['user'])
        self.assertEqual(info['session_id'], CLAUDE_SID)
        self.assertEqual(info['model'], 'claude-opus-5-5')

    def test_codex_lines_skip_external_import(self):
        info = baton.parse_codex_lines(_read_fixture('codex_rollout.jsonl'))
        self.assertEqual(info['assistant'], ['Codex progress: running the unit suite.',
                                             'Codex final: 42 tests pass; not committed.',
                                             'Codex closing note: hand back to Claude for review.'])
        self.assertIn('النص العربي', info['user'])
        joined = json.dumps(info, ensure_ascii=False)
        self.assertNotIn('Imported', joined)
        self.assertNotIn('external_agent_tool_call', joined)

    def test_codex_fallback_to_response_items(self):
        lines = [json.dumps({'type': 'response_item', 'payload': {'type': 'message', 'role': 'assistant',
                                                                  'content': [{'type': 'output_text', 'text': 'old'}]}})]
        self.assertEqual(baton.parse_codex_lines(lines)['assistant'], ['old'])

    def test_rollout_meta_regex_fallback(self):
        tmp = tempfile.mkdtemp()
        try:
            p = os.path.join(tmp, 'rollout-x.jsonl')
            with open(p, 'w', encoding='utf-8') as h:  # truncated first line: JSON parse fails
                h.write('{"type":"session_meta","payload":{"cwd":"\x43:\\\\work\\\\x","base_instructions":"' + 'z' * 50)
            self.assertEqual(baton.rollout_meta(p)['cwd'], '\x43:\\work\\x')
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_fit_handoff_bounds_size(self):
        big = 'نص طويل ' * 2000
        ctx = {'slug': 'big', 'cwd': '\x43:\\x', 'generated_at': '2026-10-08T00:00:00Z', 'meter': {},
               'baton': None, 'git': {}, 'jobs': [], 'verify': ['x'],
               'claude': {'assistant': [big, big, big], 'user': big, 'session_id': 's', 'path': 'p'},
               'codex': {'assistant': [big, big, big], 'user': big, 'session_id': 't', 'path': 'q'}}
        text = baton.fit_handoff(ctx)
        self.assertLessEqual(len(text.encode('utf-8')), baton.HANDOFF_MAX_BYTES)
        self.assertIn(baton.FOOTER, text)


class SelectionTests(BatonTestBase):
    def test_current_codex_format_user_from_response_items(self):
        info = baton.parse_codex_lines(_read_fixture('codex_rollout_current.jsonl'))
        self.assertEqual(info['user'], 'continue with the tests')
        self.assertEqual(info['assistant'], ['Current-format reply one.', 'Current-format reply two.'])
        self.assertEqual((info['native_turns'], info['external_turns']), (2, 0))
        lines = _read_fixture('codex_rollout_current.jsonl')[:8]  # first turn only
        self.assertIn('النسخة الحالية', baton.parse_codex_lines(lines)['user'])

    def test_codex_user_found_beyond_the_tail(self):
        lines = _read_fixture('codex_rollout_current.jsonl')
        filler = json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output',
                                                                  'output': 'x' * 5000}})
        path = self.write_rollout('wide', 'codex_rollout_current.jsonl')
        with open(path, 'w', encoding='utf-8', newline='\n') as h:
            h.write('\n'.join(lines[:11]) + '\n')
            for _ in range(500):  # ~2.5 MB of tool output after the last user message
                h.write(filler + '\n')
        old = baton.TAIL_BYTES
        baton.TAIL_BYTES = 1_000_000
        try:
            self.assertIsNone(baton.parse_codex_lines(common.tail_lines(path, baton.TAIL_BYTES))['user'])
            self.assertEqual(baton.codex_last_user_wide(path), 'continue with the tests')
        finally:
            baton.TAIL_BYTES = old

    def test_codex_selection_skips_imports_subagents_and_exec(self):
        good = self.write_rollout('00000000-0000-4000-8000-000000000022', 'codex_rollout_current.jsonl', age_s=600)
        imported = _read_fixture('codex_rollout.jsonl')[:5]  # only the external-import turn
        path = self.write_rollout('import', 'codex_rollout.jsonl', age_s=30)
        with open(path, 'w', encoding='utf-8', newline='\n') as h:
            meta = json.loads(imported[0])
            meta['payload']['cwd'] = self.repo
            h.write('\n'.join([json.dumps(meta)] + imported[1:]) + '\n')
        self.write_rollout('sub', 'codex_rollout_current.jsonl',
                           source={'subagent': {'thread_spawn': {'parent_thread_id': 'x'}}}, age_s=20)
        self.write_rollout('exec', 'codex_rollout_current.jsonl', source='exec', originator='codex_exec', age_s=10)
        chosen, info = baton.pick_codex_rollout(self.repo)
        self.assertEqual(chosen, good)
        self.assertEqual(info['user'], 'continue with the tests')

    def test_codex_exec_is_the_last_resort(self):
        only = self.write_rollout('exec', 'codex_rollout_current.jsonl', source='exec', originator='codex_exec')
        self.assertEqual(baton.newest_codex_rollout(self.repo), only)

    def test_claude_selection_skips_workers_and_moved_sessions(self):
        lines = _read_fixture('claude_transcript.jsonl')
        mine = self.write_claude(CLAUDE_SID, lines, age_s=900)
        self.write_claude('aaaaaaaa-0000-0000-0000-000000000001', lines, entrypoint='sdk-cli', age_s=10)
        self.write_claude('aaaaaaaa-0000-0000-0000-000000000002', lines,
                          cwd=os.path.join(self.repo, '.claude', 'worktrees', 'x'), age_s=5)
        peer = self.write_claude('aaaaaaaa-0000-0000-0000-000000000003', lines, age_s=1800)
        self.assertEqual(baton.newest_claude_transcript(self.repo), mine)
        bb = baton.claude_blackbox(self.repo)
        self.assertEqual(bb['path'], mine)
        self.assertEqual([o['session_id'] for o in bb['others']], [os.path.basename(peer)[:-6]])
        text = '\n'.join(baton.render_blackbox(bb, 'Claude', 200))
        self.assertIn('other recent sessions in this cwd', text)


class ConcurrencyTests(BatonTestBase):
    def test_concurrent_record_job_keeps_every_job(self):
        baton.new('race', 'concurrent writers')
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        code = ('import sys; sys.path.insert(0, sys.argv[1]); from twinlane import baton, common\n'
                'common.BATONS = sys.argv[2]\n'
                'for i in range(5):\n'
                '    baton.record_job("race", {"id": f"20261009-143941-codex-{int(sys.argv[3]) * 16 + i:04x}", '
                '"engine": "codex", "status": "done"})')
        env = dict(os.environ, TWINLANE_HOME=os.path.join(self.tmp, 'sub-home'))
        procs = [subprocess.Popen([sys.executable, '-c', code, root, common.BATONS, str(n)], env=env)
                 for n in range(4)]
        for p in procs:
            self.assertEqual(p.wait(timeout=120), 0)
        ids = {j['id'] for j in baton.load('race')['jobs']}
        self.assertEqual(len(ids), 20)  # 4 writers x 5 jobs, at the 20-job cap: none lost

    def test_unreadable_baton_is_never_replaced_with_an_empty_one(self):
        baton.new('broken', 'keep me')
        with open(baton.baton_path('broken'), 'w', encoding='utf-8') as h:
            h.write('{"slug": "broken", "task": {"goal": "keep me"')  # torn write
        with self.assertRaises(baton.BatonError):
            baton.record_job('broken', {'id': jid(1), 'engine': 'claude', 'status': 'done'})
        with open(baton.baton_path('broken'), encoding='utf-8') as h:
            self.assertIn('keep me', h.read())


class MeterLineTests(BatonTestBase):
    """The handoff meter line: whole-number percents, naming the window that set the max."""

    def write_cached_meter(self, claude=None, codex=None):
        common.write_json(os.path.join(common.STATE, 'meter.json'),
                          {'read_at': '2026-10-09T12:00:00Z', 'claude': claude or {}, 'codex': codex or {}})

    def test_percent_is_rounded_and_the_max_window_is_named(self):
        self.write_cached_meter(
            claude={'status': 'ok', 'max_pct': 28.728186, 'windows': [
                {'name': '5h', 'pct': 4.0}, {'name': '7d', 'pct': 12.5}, {'name': 'iguana_necktie', 'pct': 28.728186}]},
            codex={'status': 'ok', 'max_pct': 36.0, 'windows': [{'name': 'weekly', 'pct': 36.0}]})
        m = baton.cached_meter()
        self.assertEqual(m['claude_max_window'], 'iguana_necktie')
        self.assertEqual(m['claude_max_pct'], 28.728186)  # the stored number keeps its precision
        line = baton.render_meter(m)
        self.assertIn('Claude 29% ok (max: iguana_necktie)', line)
        self.assertIn('Codex 36% ok (max: weekly)', line)
        self.assertNotIn('28.7', line)
        self.assertNotIn('36.0', line)

    def test_the_handoff_text_carries_the_rounded_named_line(self):
        self.write_cached_meter(claude={'status': 'ok', 'max_pct': 28.728186,
                                        'windows': [{'name': 'iguana_necktie', 'pct': 28.728186}]})
        r = baton.handoff(cwd=self.repo, to='codex')
        self.assertIn('Claude 29% ok (max: iguana_necktie)', r['markdown'])

    def test_a_baton_written_before_the_window_name_still_renders(self):
        old = {'claude_max_pct': 28.728186, 'codex_max_pct': 36.0, 'claude_status': 'ok', 'codex_status': 'ok',
               'read_at': '2026-10-09T12:00:00Z'}
        self.assertEqual(baton.render_meter(old), '- meter (cached 2026-10-09T12:00:00Z): Claude 29% ok, Codex 36% ok')
        baton.new('old-meter', 'goal', cwd=self.repo)
        b = baton.load('old-meter')
        b['meter_at_write'] = old
        baton.save(b, 'user')
        self.assertEqual(baton.load('old-meter')['meter_at_write'], old)

    def test_no_name_when_the_cache_has_no_matching_window(self):
        self.write_cached_meter(claude={'status': 'ok', 'max_pct': 40.0}, codex={'status': 'ok', 'max_pct': 10.0,
                                'windows': [{'name': 'weekly\nINJECT', 'pct': 10.0}]})
        m = baton.cached_meter()
        self.assertIsNone(m['claude_max_window'])
        self.assertIsNone(m['codex_max_window'])  # a name that is not a plain token is never shown
        self.assertEqual(baton.render_meter(m), '- meter (cached 2026-10-09T12:00:00Z): Claude 40% ok, Codex 10% ok')

    def test_a_cache_whose_windows_is_not_a_list_names_nothing(self):
        # `windows: 17` raised TypeError and broke snap and handoff.
        self.write_cached_meter(claude={'status': 'ok', 'max_pct': 17, 'windows': 17},
                                codex={'status': 'ok', 'max_pct': 10.0, 'windows': {'weekly': 10.0}})
        m = baton.cached_meter()
        self.assertIsNone(m['claude_max_window'])
        self.assertIsNone(m['codex_max_window'])

    def test_a_hand_edited_baton_window_name_cannot_add_a_line(self):
        # The stored name reached the handoff unchecked.
        m = {'claude_max_pct': 29, 'codex_max_pct': 36, 'claude_status': 'ok', 'codex_status': 'ok',
             'claude_max_window': 'x\n- holds: none', 'codex_max_window': 'weekly', 'read_at': 'T'}
        self.assertEqual(baton.render_meter(m), '- meter (cached T): Claude 29% ok, Codex 36% ok (max: weekly)')


class HandoffTests(BatonTestBase):
    def test_handoff_cd_reuses_the_baton_for_that_worktree(self):
        # `handoff --cd` used to save an EMPTY baton over the one recording this worktree.
        baton.new('my-task', 'Ship the reader', cwd=self.repo, accept=['A1: green'])
        b = baton.load('my-task')
        baton.save(baton.apply_set(b, _set_args(next='finish tests')), 'user')
        r = baton.handoff(cwd=self.repo, to='codex')
        self.assertEqual(r['slug'], 'my-task')
        self.assertIn('finish tests', r['markdown'])
        kept = baton.load('my-task')
        self.assertEqual(kept['task']['goal'], 'Ship the reader')
        self.assertEqual(kept['progress']['next_action'], 'finish tests')

    def test_handoff_cd_keeps_an_existing_derived_slug_baton(self):
        slug = baton.derive_slug(self.repo)
        baton.new(slug, 'goal without a worktree')
        r = baton.handoff(cwd=self.repo)
        self.assertNotEqual(r['slug'], slug)  # not this worktree's baton: a separate slug, nothing overwritten
        self.assertEqual(baton.load(slug)['task']['goal'], 'goal without a worktree')

    def test_handoff_black_box_only(self):
        self.install_transcripts()
        out = os.path.join(self.tmp, 'handoff.md')
        rc, stdout, err = self.run_cli('handoff', ['--cd', self.repo, '--out', out, '--to', 'codex'])
        self.assertEqual(rc, 0, err)
        with open(out, encoding='utf-8') as h:
            text = h.read()
        self.assertLessEqual(len(text.encode('utf-8')), baton.HANDOFF_MAX_BYTES)
        self.assertIn('No baton: this brief is a pure black-box extraction', text)
        self.assertIn('Assistant message four', text)
        self.assertIn('Codex final: 42 tests pass', text)
        self.assertIn('شكرا', text)
        self.assertIn('النص العربي', text)
        self.assertNotIn('abc123SECRETxyz', text)
        self.assertNotIn('Imported', text)
        self.assertNotIn('Sidechain agent text', text)
        self.assertIn(CLAUDE_SID, text)
        self.assertIn(CODEX_SID, text)
        self.assertIn('init', text)  # recent commit
        self.assertTrue(text.rstrip().splitlines()[-1].startswith(baton.FOOTER))
        self.assertIn('--engine codex', stdout)
        self.assertIn(f'--cd "{self.repo}"', stdout)
        slug = baton.derive_slug(self.repo)
        b = baton.load(slug)
        self.assertEqual(b['reason'], 'handoff')
        self.assertEqual(b['last_handoff']['to'], 'codex')
        self.assertTrue(os.path.isfile(baton.brief_path(slug)))

    def test_handoff_from_baton_with_resume_hint(self):
        self.install_transcripts()
        path = os.path.join(self.claude_dir(), CLAUDE_SID + '.jsonl')
        old = os.path.getmtime(path) - baton.RESUME_IDLE_S - 10
        os.utime(path, (old, old))
        baton.new('task-x', 'Finish the baton', cwd=self.repo, accept=['A1: green'])
        b = baton.load('task-x')
        baton.save(baton.apply_set(b, _set_args(next='write the README', do_not=['git push'])), 'user')
        rc, stdout, err = self.run_cli('handoff', ['task-x', '--to', 'claude'])
        self.assertEqual(rc, 0, err)
        self.assertIn('**Next action:** write the README', stdout)
        self.assertIn('A1 [not_run] green', stdout)
        self.assertIn('git push', stdout)
        self.assertIn(f'--resume {CLAUDE_SID}', stdout)
        self.assertIn('--baton task-x --name handoff-task-x', stdout)
        self.assertIn('fork the idle Claude session', stdout)
        runnable = [ln for ln in stdout.splitlines() if ln.startswith('twinlane run ')]
        self.assertEqual(len(runnable), 1, runnable)  # the fork alternative stays a comment
        self.assertNotIn('--resume', runnable[0])
        resume_line = next(ln for ln in stdout.splitlines() if ln.startswith('#') and '--resume' in ln)
        self.assertIn('--baton task-x', resume_line)
        self.assertIn('Approvals do not transfer', stdout)
        self.assertEqual(baton.load('task-x')['reason'], 'handoff')
        self.assertGreaterEqual(len(baton.history('task-x')), 2)

    def test_handoff_does_not_suggest_live_session_resume(self):
        self.install_transcripts()
        r = baton.handoff(cwd=self.repo, to='claude')
        self.assertTrue(r['command'])
        self.assertFalse(any('--resume' in line for line in r['command']))
        self.assertIn('--baton ' + r['slug'], r['command'][0])

    def test_handoff_refuses_removed_worktree_without_writing_baton(self):
        missing = os.path.join(self.tmp, 'removed-worktree')
        rc, out, err = self.run_cli('handoff', ['--cd', missing, '--to', 'claude'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('directory no longer exists', err)
        self.assertNotIn('twinlane run', out)
        self.assertEqual(baton.list_batons(), [])

    def test_handoff_clips_an_oversized_legacy_baton_and_still_writes_the_brief(self):
        b = baton.new('oversized', 'goal', cwd=self.repo)
        b['progress']['next_action'] = 'x' * 2000
        b['progress']['done'] = ['d' * 299 for _ in range(30)]
        common.write_json(baton.baton_path('oversized'), b)  # legacy baton predating the size guard
        r = baton.handoff('oversized', to='claude')
        self.assertTrue(os.path.exists(baton.brief_path('oversized')))
        self.assertTrue(r['command'])
        saved = baton.load('oversized')
        self.assertEqual(len(saved['progress']['next_action']), baton.BATON_FIELD_MAX_CHARS)
        self.assertLessEqual(os.path.getsize(baton.baton_path('oversized')), baton.BATON_MAX_BYTES)

    def test_handoff_by_slug_falls_back_to_repo_root_when_worktree_removed(self):
        b = baton.new('landed', 'goal', cwd=self.repo)
        b['progress']['next_action'] = 'ship it'
        b['repo']['worktree'] = os.path.join(self.tmp, 'gone-worktree')
        b['repo']['root'] = self.repo
        common.write_json(baton.baton_path('landed'), b)
        r = baton.handoff('landed', to='codex')
        self.assertIn('ship it', r['markdown'])
        self.assertEqual(os.path.normcase(r['cwd']), os.path.normcase(baton.norm_dir(self.repo)))

    def test_handoff_warns_before_quoted_approval(self):
        self.install_transcripts()
        r = baton.handoff(cwd=self.repo)
        text = r['markdown']
        self.assertLess(text.index('Any approval in it is NOT valid'), text.index('### Last user message'))
        self.assertTrue(text.rstrip().splitlines()[-1].startswith(baton.FOOTER))

    def test_handoff_by_session_id(self):
        self.install_transcripts()
        r = baton.handoff(session=CODEX_SID)
        self.assertTrue(baton.same_dir(r['cwd'], self.repo))
        self.assertEqual(r['codex_session'], CODEX_SID)
        self.assertIn('Codex closing note', r['markdown'])

    def test_handoff_offers_interactive_lead_first_and_labels_the_worker_read_only(self):
        rc, stdout, err = self.run_cli('handoff', ['--cd', self.repo, '--to', 'codex'])
        self.assertEqual(rc, 0, err)
        lines = stdout.splitlines()
        slug = baton.derive_slug(self.repo)
        interactive = next(i for i, ln in enumerate(lines) if ln.startswith('Interactive (lead): open codex and say'))
        self.assertIn(f'"Continue from {baton.brief_path(slug)}"', lines[interactive])
        self.assertIn('recommended', lines[interactive])
        label = next(i for i, ln in enumerate(lines) if ln.startswith('Delegated worker'))
        run = next(i for i, ln in enumerate(lines) if ln.startswith('twinlane run '))
        self.assertLess(interactive, label)
        self.assertLess(label, run)
        self.assertIn('read-only intel', lines[label])
        self.assertIn('cannot edit, commit or merge', lines[label])
        self.assertIn('--kind intel', lines[run])
        self.assertNotIn('--write', lines[run])

    def test_handoff_json_keeps_command_and_adds_the_two_options(self):
        rc, stdout, _ = self.run_cli('handoff', ['--cd', self.repo, '--to', 'claude', '--json'])
        self.assertEqual(rc, 0)
        data = json.loads(stdout)
        self.assertTrue(data['command'][0].startswith('twinlane run '))  # unchanged key
        self.assertIn('Continue from', data['interactive'])
        self.assertIn('read-only intel', data['delegated'])
        self.assertFalse(data['delegated_write'])
        rc, stdout, _ = self.run_cli('handoff', ['--cd', self.repo, '--json'])
        data = json.loads(stdout)  # without --to there is nobody to hand to: no options
        self.assertEqual((data['command'], data['interactive'], data['delegated']), ([], None, None))

    def test_handoff_kind_code_passes_through_in_a_linked_worktree_only(self):
        rc, _, err = self.run_cli('handoff', ['--cd', self.repo, '--to', 'codex', '--kind', 'code'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('LINKED worktree', err)
        self.assertEqual(baton.list_batons(), [])  # refused before anything was written
        wt = baton.norm_dir(os.path.join(self.tmp, 'linked'))
        _git(self.repo, 'worktree', 'add', '-q', '-b', 'linked-branch', wt)
        rc, stdout, err = self.run_cli('handoff', ['--cd', wt, '--to', 'codex'])
        self.assertEqual(rc, 0, err)  # unasked, even in a linked worktree, the worker is read-only intel
        run = next(ln for ln in stdout.splitlines() if ln.startswith('twinlane run '))
        self.assertIn('--kind intel', run)
        self.assertNotIn('--write', run)
        rc, stdout, err = self.run_cli('handoff', ['--cd', wt, '--to', 'codex', '--kind', 'code'])
        self.assertEqual(rc, 0, err)
        run = next(ln for ln in stdout.splitlines() if ln.startswith('twinlane run '))
        self.assertIn('--kind code --write', run)
        self.assertIn('Delegated worker (code --write', stdout)
        self.assertNotIn('read-only intel', stdout)
        rc, stdout, _ = self.run_cli('handoff', ['--cd', wt, '--to', 'codex', '--kind', 'intel'])
        run = next(ln for ln in stdout.splitlines() if ln.startswith('twinlane run '))
        self.assertIn('--kind intel', run)  # a caller can ask for the read-only worker inside a worktree too
        rc, _, err = self.run_cli('handoff', ['--cd', wt, '--kind', 'code'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('needs --to', err)

    def test_handoff_refuses_leftover_worktree_folders_but_not_plain_dirs(self):
        empty = os.path.join(self.tmp, 'empty-leftover')
        os.makedirs(empty)
        rc, out, err = self.run_cli('handoff', ['--cd', empty, '--to', 'claude'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('is empty', err)
        self.assertNotIn('twinlane run', out)
        dangling = os.path.join(self.tmp, 'dangling-leftover')
        os.makedirs(dangling)
        with open(os.path.join(dangling, '.git'), 'w', encoding='utf-8') as h:
            h.write(f'gitdir: {self.tmp}/gone/.git/worktrees/dangling-leftover\n')
        rc, out, err = self.run_cli('handoff', ['--cd', dangling, '--to', 'claude'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('missing gitdir', err)
        self.assertNotIn('twinlane run', out)
        self.assertEqual(baton.list_batons(), [])  # no baton, brief or command for either
        plain = os.path.join(self.tmp, 'plain-folder')  # a black-box handoff from a non-repo folder stays valid
        os.makedirs(plain)
        with open(os.path.join(plain, 'notes.txt'), 'w', encoding='utf-8') as h:
            h.write('x\n')
        rc, out, err = self.run_cli('handoff', ['--cd', plain, '--to', 'claude'])
        self.assertEqual(rc, 0, err)
        self.assertIn('twinlane run ', out)
        with open(os.path.join(plain, '.git'), 'w', encoding='utf-8') as h:  # ordinary file, not a pointer
            h.write('not a pointer\n')
        rc, out, err = self.run_cli('handoff', ['--cd', plain, '--to', 'claude'])
        self.assertEqual(rc, 0, err)
        self.assertIn('twinlane run ', out)

    def test_handoff_by_slug_falls_back_to_repo_root_when_worktree_folder_is_a_leftover(self):
        b = baton.new('left-over', 'goal', cwd=self.repo)
        leftover = os.path.join(self.tmp, 'leftover-worktree')
        os.makedirs(leftover)
        with open(os.path.join(leftover, '.git'), 'w', encoding='utf-8') as h:
            h.write(f'gitdir: {self.tmp}/gone/.git/worktrees/leftover-worktree\n')
        b['repo']['worktree'] = leftover
        b['repo']['root'] = self.repo
        common.write_json(baton.baton_path('left-over'), b)
        r = baton.handoff('left-over', to='codex')
        self.assertEqual(os.path.normcase(r['cwd']), os.path.normcase(baton.norm_dir(self.repo)))

    def test_handoff_json(self):
        rc, stdout, _ = self.run_cli('handoff', ['--cd', self.repo, '--json'])
        self.assertEqual(rc, 0)
        data = json.loads(stdout)
        self.assertIn('none found for this cwd', data['markdown'])
        self.assertLessEqual(data['bytes'], baton.HANDOFF_MAX_BYTES)


if __name__ == '__main__':
    unittest.main()
