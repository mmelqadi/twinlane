"""twinlane board: fake meters, jobs, batons and real temp git worktrees; every path points at a temp dir."""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import twinlane  # noqa: E402
import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import baton, board, common, meter  # noqa: E402

SECRET = 'sk-' + 'ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX'
DEAD_PID = 4_000_000


def iso_ago(seconds):
    return common.iso(common.now() - dt.timedelta(seconds=seconds))


def iso_in(seconds):
    return common.iso(common.now() + dt.timedelta(seconds=seconds))


def jid_ago(seconds, tag, engine='claude'):
    return (common.now() - dt.timedelta(seconds=seconds)).strftime('%Y%m%d-%H%M%S') + f'-{engine}-{tag}'


def git(cwd, *args):
    r = subprocess.run(['git', '-c', 'user.name=t', '-c', 'user.email=user@example.com', *args], cwd=cwd,
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    return r.stdout


def tree_state(top):
    out = {}
    for d, _, files in os.walk(top):
        for f in files:
            p = os.path.join(d, f)
            st = os.stat(p)
            out[p] = (st.st_size, st.st_mtime_ns)
    return out


class BoardBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-board-')
        self._saved = (common.STATE, common.JOBS, common.BATONS)
        common.STATE = os.path.join(self.tmp, 'state')
        common.JOBS = os.path.join(self.tmp, 'jobs')
        common.BATONS = os.path.join(self.tmp, 'batons')
        common.ensure_dirs()
        config = mock.patch.dict(os.environ, {'TWINLANE_CONFIG': os.path.join(self.tmp, 'config.json')})
        config.start()
        self.addCleanup(config.stop)
        for name, path in (('CLAUDE_PROJECTS', 'claude'), ('CODEX_SESSIONS', 'codex')):
            session_dirs = mock.patch.object(baton, name, os.path.join(self.tmp, path))
            session_dirs.start()
            self.addCleanup(session_dirs.stop)
        self.policy = common.load_policy()

    def tearDown(self):
        common.STATE, common.JOBS, common.BATONS = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- fixtures
    def write_meter(self, claude=None, codex=None, age=30):
        at = iso_ago(age)
        c = {'status': 'ok', 'source': 'oauth', 'reading_at': at, 'max_pct': 50.0, 'next_reset': iso_in(3600),
             'extra_usage_enabled': False,
             'windows': [{'name': 'five_hour', 'pct': 50.0, 'resets_at': iso_in(3600)},
                         {'name': 'seven_day', 'pct': 20.0, 'resets_at': iso_in(86400 * 3)}]}
        x = {'status': 'ok', 'source': 'app-server', 'reading_at': at, 'max_pct': 30.0, 'next_reset': iso_in(7200),
             'credits': {'has': True, 'unlimited': False, 'balance': 'redacted'}, 'on_credits': False,
             'windows': [{'name': '5h', 'pct': 10.0, 'resets_at': iso_in(7200)},
                         {'name': '7d', 'pct': 30.0, 'resets_at': iso_in(86400 * 2)}]}
        c.update(claude or {})
        x.update(codex or {})
        common.write_json(os.path.join(common.STATE, 'meter.json'), {'read_at': at, 'claude': c, 'codex': x})

    def add_job(self, jid, status, engine='claude', kind='code', name=None, started_ago=300, timeout=3600,
                final=None, cwd=None, model='claude-sonnet-5-5', served=None, ended_ago=None, extra=None):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d)
        prim = {'engine': engine, 'model': model if engine == 'claude' else None,
                'tier': None if engine == 'claude' else 'sol-high', 'profile': 'code', 'timeout_s': timeout}
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'name': name or jid[-4:], 'kind': kind,
                                                       'cwd': cwd or os.path.join(self.tmp, 'wt-' + jid[-4:]),
                                                       'primary': prim, 'created_at': iso_ago(started_ago)})
        if status in ('running', 'queued', 'lost'):
            pid = os.getpid() if status != 'lost' else DEAD_PID
            common.write_json(os.path.join(d, 'status.json'), {'status': 'running', 'pid': pid, 'engine': engine,
                                                              'started_at': iso_ago(started_ago),
                                                              'created_at': iso_ago(started_ago)})
            return d
        receipt = {'id': jid, 'status': status, 'engine': engine, 'kind': kind, 'name': name,
                   'requested': {k: prim[k] for k in ('model', 'tier', 'profile')},
                   'served_models': [served or model] if engine == 'claude' else [],
                   'started_at': iso_ago(started_ago),
                   'ended_at': iso_ago(started_ago if ended_ago is None else ended_ago)}
        receipt.update(extra or {})
        if final is not None:
            with open(os.path.join(d, 'final.md'), 'w', encoding='utf-8') as h:
                h.write(final)
            receipt['final_path'] = os.path.join(d, 'final.md')
        common.write_json(os.path.join(d, 'result.json'), receipt)
        return d

    def add_baton(self, slug, owner='claude', next_action='do the thing', approvals=(), in_flight=(),
                  root=None, worktree=None):
        b = baton.empty_baton(slug)
        b['updated_at'] = iso_ago(120)
        b['owner']['engine'] = owner
        b['progress']['next_action'] = next_action
        b['progress']['in_flight'] = [{'what': 'w', 'job': j, 'check': f'twinlane result {j}'} for j in in_flight]
        b['holds']['approvals_pending'] = [{'action': a, 'ask_text': 'ok?'} for a in approvals]
        b['repo'].update({'root': root, 'worktree': worktree})
        common.write_json(baton.baton_path(slug), b)

    def run_board(self, *argv):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = twinlane.main(['board', *argv])
        return rc, out.getvalue()


class VerdictTests(unittest.TestCase):
    def test_verdict_first_line(self):
        v = board.verdict_of
        self.assertEqual(v('MERGE AFTER FIXES\nbody'), 'MERGE AFTER FIXES')
        self.assertEqual(v('DO NOT MERGE: tests fail\nx'), 'DO NOT MERGE: tests fail')
        self.assertEqual(v('\n\n**MERGE**\nx'), 'MERGE')
        self.assertEqual(v('# MERGE - ok'), 'MERGE - ok')
        self.assertIsNone(v('MERGED already\nx'))
        self.assertIsNone(v('Report\nMERGE'))      # only the first non-blank line counts
        self.assertIsNone(v(''))


class CollectTests(BoardBase):
    def test_meters_routes_and_defensive_fields(self):
        self.write_meter(codex={'free_resets': 2, 'future_field': {'x': 1}, 'windows': [
            {'name': '5h', 'pct': 10.0, 'resets_at': iso_in(60)}, None, 'junk', {'name': 7, 'pct': 'bad'},
            {'name': 'weekly', 'pct': 31.0, 'resets_at': iso_in(86400)}]})
        m = board.collect(policy=self.policy)['meters']
        self.assertTrue(m['cached'])
        self.assertEqual([w['label'] for w in m['claude']['windows']], ['5h', '7d'])
        self.assertEqual(m['codex']['free_resets'], '2')
        self.assertEqual(len(m['codex']['windows']), 3)          # non-dict entries skipped, a bad pct kept as None
        self.assertEqual(m['routes']['code']['engine'], 'claude')
        self.assertEqual(m['routes']['sweep']['engine'], 'codex')
        self.assertFalse(m['routes']['code']['fallback_used'])

    def test_missing_and_malformed_meter_sections_do_not_crash(self):
        for raw in ({'claude': {}, 'codex': {}}, {'claude': 'x', 'codex': []},
                    {'claude': {'windows': 'no', 'status': 5, 'reading_at': 'never'}, 'codex': {'credits': 'odd'}}):
            common.write_json(os.path.join(common.STATE, 'meter.json'), dict(raw, read_at=iso_ago(1)))
            with self.subTest(raw=raw):
                data = board.collect(policy=self.policy)
                board.render_text(data), board.render_md(data), json.dumps(data)

    def test_no_cached_meter(self):
        data = board.collect(policy=self.policy)
        self.assertFalse(data['meters']['cached'])
        self.assertEqual(data['meters']['routes'], {})
        self.assertIn('no cached meter', ' '.join(a['text'] for a in data['signals']))
        self.assertIn('route now: n/a', board.render_text(data))

    def test_stale_reading_and_stop_line_are_attention_and_change_routes(self):
        self.write_meter(claude={'status': 'stop', 'max_pct': 87.0}, codex={'reading_at': iso_ago(99999)})
        data = board.collect(policy=self.policy)
        m = data['meters']
        self.assertEqual(m['codex']['status'], 'unknown')       # stale reading cannot keep reading "ok"
        text = ' '.join(a['text'] for a in data['signals'])
        self.assertIn('claude STOP (87%, stop at 85%)', text)
        self.assertIn('codex UNKNOWN', text)
        self.assertTrue(m['routes']['code']['refused'])           # claude stop + codex unknown

    def test_extra_usage_and_credits_flags(self):
        self.write_meter(claude={'extra_usage_enabled': True}, codex={'on_credits': True})
        text = ' '.join(a['text'] for a in board.collect(policy=self.policy)['signals'])
        self.assertIn('extra usage is ON', text)
        self.assertIn('paid credits', text)

    def test_claude_held_for_unknown_extra_usage_says_why(self):
        # The board shows the hold in the attention list, the meter view and the route reasons.
        raw = {'source': 'oauth', 'reading_at': iso_ago(30), 'extra_usage_enabled': None,
               'windows': [{'name': 'five_hour', 'pct': 20.0, 'resets_at': iso_in(3600)}]}
        held = meter.finish_claude(raw, self.policy)
        self.assertEqual(held['status'], 'unknown')
        self.write_meter(claude=held, codex={'status': 'out', 'max_pct': 100.0})
        data = board.collect(policy=self.policy)
        self.assertEqual(data['meters']['claude']['hold'], 'extra usage state unknown')
        text = ' '.join(a['text'] for a in data['signals'])
        self.assertIn('claude UNKNOWN', text)
        self.assertIn('extra usage state unknown', text)
        self.assertTrue(data['meters']['routes']['code']['refused'])
        self.assertIn('[extra usage state unknown]', data['meters']['routes']['code']['reason'])

    def sonnet_stop(self):
        """Seven_day_sonnet alone stops Claude while 5h and 7d look fine."""
        return {'status': 'stop', 'max_pct': 90.0,
                'windows': [{'name': 'five_hour', 'pct': 5.0, 'resets_at': iso_in(3600)},
                            {'name': 'seven_day', 'pct': 15.0, 'resets_at': iso_in(86400 * 3)},
                            {'name': 'seven_day_sonnet', 'pct': 90.0, 'resets_at': iso_in(86400 * 2)}]}

    def test_a_window_that_controls_routing_is_shown_marked_and_named_in_the_refusal(self):
        self.write_meter(claude=self.sonnet_stop(), codex={'status': 'out', 'max_pct': 100.0})
        for flag in ('', '--md'):
            with self.subTest(flag=flag):
                rc, out = self.run_board(*([flag] if flag else []))
                self.assertEqual(rc, 0)
                self.assertIn('7d-sonnet 90% (resets in 1d', out)
                self.assertEqual(out.count('[binding]'), 2)                  # one per engine
                self.assertIn('[refused by claude stop at 7d-sonnet 90%, codex out]', out)
                self.assertIn('claude STOP (90% in 7d-sonnet, stop at 85%)', out)
        rc, out = self.run_board('--json')
        m = json.loads(out)['meters']
        wins = {w['name']: w for w in m['claude']['windows']}
        self.assertEqual([n for n, w in wins.items() if w['binding']], ['seven_day_sonnet'])
        self.assertEqual((wins['seven_day_sonnet']['pct'], wins['seven_day_sonnet']['recognised']), (90.0, True))
        self.assertEqual(m['claude']['binding']['name'], 'seven_day_sonnet')
        self.assertTrue(m['routes']['code']['refused'])
        blockers = m['routes']['code']['blockers']
        self.assertEqual([b['engine'] for b in blockers], ['claude', 'codex'])
        self.assertEqual((blockers[0]['window'], blockers[0]['status'], blockers[0]['pct']), ('7d-sonnet', 'stop', 90.0))

    def test_an_unrecognised_window_is_shown_with_its_label_in_text_markdown_and_json(self):
        self.write_meter(claude={'max_pct': 28.7, 'windows': [
            {'name': 'five_hour', 'pct': 20.0, 'resets_at': iso_in(3600)},
            {'name': 'seven_day', 'pct': 18.0, 'resets_at': iso_in(86400 * 3)},
            {'name': 'iguana_necktie', 'pct': 28.728186, 'resets_at': iso_in(86400 * 27)}]})
        for flag in ('', '--md'):
            with self.subTest(flag=flag):
                _, out = self.run_board(*([flag] if flag else []))
                self.assertIn('iguana_necktie (unrecognised window) 28.7% (resets in 26d', out)
                self.assertIn('[binding]', out)
                self.assertIn('5h 20% (resets in', out)
        _, out = self.run_board('--json')
        wins = {w['name']: w for w in json.loads(out)['meters']['claude']['windows']}
        self.assertEqual((wins['iguana_necktie']['recognised'], wins['iguana_necktie']['binding'],
                          wins['iguana_necktie']['label']), (False, True, 'iguana_necktie'))  # never renamed
        self.assertTrue(wins['five_hour']['recognised'] and not wins['five_hour']['binding'])

    def test_an_unprintable_window_name_is_not_printed_and_still_counted(self):
        self.write_meter(claude={'max_pct': 60.0, 'windows': [
            {'name': 'five_hour', 'pct': 20.0, 'resets_at': iso_in(3600)},
            {'name': 'x\nINJECT', 'pct': 60.0, 'resets_at': iso_in(3600)},
            {'name': 'z' * 41, 'pct': 10.0, 'resets_at': iso_in(3600)}]})
        _, out = self.run_board()
        self.assertNotIn('INJECT', out)
        self.assertNotIn('z' * 41, out)
        self.assertIn('(invalid window name) 60% (resets in', out)
        self.assertEqual(out.count('(invalid window name)'), 2)
        _, out = self.run_board('--json')
        bad = [w for w in json.loads(out)['meters']['claude']['windows'] if w['name'] is None]
        self.assertEqual(len(bad), 2)
        self.assertEqual([w['binding'] for w in bad], [True, False])

    def test_another_codex_limit_id_is_listed_and_counts_as_known(self):
        self.write_meter(codex={'max_pct': 70.0, 'windows': [
            {'name': 'weekly', 'pct': 30.0, 'resets_at': iso_in(7200)},
            {'name': 'codex_astra:weekly', 'pct': 70.0, 'resets_at': iso_in(7200), 'limit': 'codex_astra'}]})
        _, out = self.run_board()
        self.assertIn('codex_astra:weekly 70% (resets in', out)
        self.assertNotIn('codex_astra:weekly (unrecognised window)', out)   # another limit id is a known kind

    def test_jobs_attention_and_verdicts(self):
        self.write_meter()
        self.add_job('20261009-100000-claude-aaaa', 'running', name='live-one', started_ago=100)
        self.add_job('20261009-100001-codex-bbbb', 'running', engine='codex', kind='parallel', started_ago=3000,
                     timeout=3600)                                      # 83% of timeout
        self.add_job('20261009-090000-claude-cccc', 'done', final='MERGE AFTER FIXES\nbody', started_ago=900,
                     ended_ago=600, name='review-a')
        self.add_job('20261009-090001-claude-dddd', 'failed', started_ago=1200, ended_ago=1000)
        self.add_job('20261009-090002-codex-eeee', 'lost', engine='codex', started_ago=900)
        self.add_job('20261009-090003-claude-ffff', 'limit_hit', started_ago=900, ended_ago=800,
                     extra={'limit_hit': True})
        self.add_job('20261008-010000-claude-gggg', 'failed', started_ago=200000, ended_ago=199000)  # > 24 h
        data = board.collect(policy=self.policy)
        by_status = {r['id'][-4:]: r for r in data['jobs']['live']}
        self.assertEqual(sorted(by_status), ['aaaa', 'bbbb'])
        self.assertEqual(by_status['bbbb']['timeout_pct'], 83)
        done = {r['id'][-4:]: r for r in data['jobs']['finished']}
        self.assertEqual(done['cccc']['verdict'], 'MERGE AFTER FIXES')
        self.assertEqual(done['cccc']['served'], 'claude-sonnet-5-5')
        self.assertEqual(done['cccc']['requested'], 'claude-sonnet-5-5')
        texts = [a['text'] for a in data['signals']]
        joined = '\n'.join(texts)
        self.assertIn('1 finished, not yet read: MERGE AFTER FIXES x1', joined)
        self.assertIn('failed: 20261009-090001-claude-dddd', joined)
        self.assertIn('lost: 20261009-090002-codex-eeee', joined)
        self.assertIn('limit_hit: 20261009-090003-claude-ffff', joined)
        self.assertIn('20261009-100001-codex-bbbb at 83% of its timeout', joined)
        self.assertNotIn('gggg', joined)                           # older than the 24 h window
        self.assertIn('gggg', '\n'.join(a['text'] for a in board.collect(show_all=True, policy=self.policy)['signals']))

    def test_baton_live_count_uses_unfinished_jobs_not_only_pointers(self):
        # A size-trimmed baton keeps 10 in-flight pointers but lists all 90 live jobs in jobs[]: the board counts 90.
        self.add_baton('busy')
        b = baton.load('busy')
        ids = [f'20261009-143941-codex-{i:04x}' for i in range(90)]
        b['jobs'] = [{'id': i, 'engine': 'codex', 'status': 'running'} for i in ids]
        b['jobs'] += [{'id': f'20261009-143941-claude-{i:04x}', 'engine': 'claude', 'status': 'done'} for i in range(5)]
        b['progress']['in_flight'] = [{'what': 'w', 'job': i, 'check': f'twinlane result {i}'} for i in ids[-10:]]
        common.write_json(baton.baton_path('busy'), b)
        row = {r['slug']: r for r in board.collect(policy=self.policy)['batons']}['busy']
        self.assertEqual(len(row['in_flight']), 90)          # each live job once, finished ones not counted
        self.assertEqual(set(row['in_flight']), set(ids))
        self.assertIn('in-flight 90', board._baton_text(row))

    def test_baton_live_count_skips_finished_pointers_and_counts_a_job_once(self):
        # A stale pointer to a finished job counted as live, and two pointers to one job as two.
        self.add_baton('stale')
        b = baton.load('stale')
        live, done = '20261009-143941-codex-0001', '20261009-143941-claude-0002'
        b['jobs'] = [{'id': live, 'engine': 'codex', 'status': 'running'},
                     {'id': done, 'engine': 'claude', 'status': 'done'}]
        b['progress']['in_flight'] = [{'what': 'w', 'job': j, 'check': f'twinlane result {j}'} for j in (live, live, done)]
        common.write_json(baton.baton_path('stale'), b)
        row = {r['slug']: r for r in board.collect(policy=self.policy)['batons']}['stale']
        self.assertEqual(row['in_flight'], [live])

    def test_a_job_with_an_unreadable_identity_is_live_unknown_and_in_attention(self):
        # The board must not call a running job `lost` just because this view cannot read it.
        self.write_meter()
        self.add_job('20261009-100000-claude-uuuu', 'running', started_ago=100)
        self.add_job('20261009-100001-claude-llll', 'lost', started_ago=100)
        with mock.patch.object(common, 'pid_state', side_effect=lambda pid, created=None:
                               common.DEAD if pid == DEAD_PID else common.UNKNOWN):
            data = board.collect(policy=self.policy)
        self.assertEqual({r['id'][-4:]: r['status'] for r in data['jobs']['live']}, {'uuuu': 'unknown'})
        self.assertEqual({r['id'][-4:]: r['status'] for r in data['jobs']['finished']}, {'llll': 'lost'})
        self.assertEqual(data['jobs']['unreadable'], [])
        text = '\n'.join(a['text'] for a in data['signals'])
        self.assertIn('unknown: 20261009-100000-claude-uuuu', text)
        self.assertIn('check from the host', text.split('unknown: 20261009-100000-claude-uuuu')[1].splitlines()[0])
        self.assertNotIn('lost: 20261009-100000-claude-uuuu', text)

    def test_batons_and_pending_approvals(self):
        self.write_meter()
        self.add_baton('alpha', owner='codex', approvals=['send the SMS blast'], in_flight=['j1', 'j2'])
        self.add_baton('beta', next_action='x' * 500)
        data = board.collect(policy=self.policy)
        rows = {b['slug']: b for b in data['batons']}
        self.assertEqual(rows['alpha']['owner'], 'codex')
        self.assertEqual(rows['alpha']['in_flight'], ['j1', 'j2'])
        self.assertEqual(rows['alpha']['approvals_pending'], ['send the SMS blast'])
        self.assertLessEqual(len(rows['beta']['next_action']), 100)
        self.assertTrue(any(a['kind'] == 'inbox' and 'alpha:H1' in a['id'] and 'send the SMS blast' in a['text']
                            for a in data['attention']))


class RenderTests(BoardBase):
    def populate(self, live=9):
        self.write_meter()
        for i in range(live):
            self.add_job(f'20261009-10{i:02d}00-claude-{i:04x}', 'running', started_ago=60 + i)
        for i in range(8):
            self.add_job(f'20261009-09{i:02d}00-codex-{i:04x}', 'done', engine='codex', ended_ago=3000 + i,
                         started_ago=3600, final='MERGE\nok' if i == 0 else 'text')
        for i in range(8):
            self.add_baton(f'b{i}', approvals=['a1'] if i < 3 else ())

    def test_default_is_about_forty_lines_and_all_lifts_caps(self):
        self.populate()
        rc, out = self.run_board()
        self.assertEqual(rc, 0)
        lines = out.splitlines()
        self.assertLessEqual(len(lines), 45, out)
        for header in ('== METERS', '== ATTENTION', '== JOBS', '== BATONS', '== WORKTREES'):
            self.assertIn(header, out)
        self.assertLess(out.index('== METERS'), out.index('== ATTENTION'))
        self.assertLess(out.index('== ATTENTION'), out.index('== JOBS'))
        self.assertLess(out.index('== JOBS'), out.index('== BATONS'))
        self.assertLess(out.index('== BATONS'), out.index('== WORKTREES'))
        self.assertIn(f"+{9 - board.CAPS['live']} more (--all)", out)  # compact cap; --all exposes all 9
        rc, full = self.run_board('--all')
        self.assertGreater(len(full.splitlines()), len(lines))
        self.assertNotIn('more (--all)', full)
        self.assertEqual(sum(1 for line in full.splitlines() if 'running' in line and 'claude-' in line), 9)

    def test_markdown_tables_escape_pipes_and_backslashes(self):
        self.populate(live=2)
        self.add_job('20261009-110000-claude-abcd', 'done', name='a|b', final='DO NOT MERGE\nx', ended_ago=60)
        self.add_job('20261009-110001-claude-abce', 'done', name='c\\|d', final='MERGE\nx', ended_ago=59)
        rc, out = self.run_board('--md')
        self.assertEqual(rc, 0)
        self.assertTrue(out.startswith('### twinlane board'))
        self.assertIn('**Meters**', out)
        self.assertIn('| job | engine | kind |', out)
        self.assertIn('a\\|b', out)
        self.assertIn('DO NOT MERGE', out)
        row = [line for line in out.splitlines() if line.startswith('| `') and 'abce' in line][0]
        # a backslash already in the text is doubled, so the pipe after it is still escaped, not a delimiter
        self.assertIn('c\\\\\\|d', row)
        self.assertEqual(board._cell('x\\|y'), 'x\\\\\\|y')
        self.assertEqual(board._cell('a|b\nc'), 'a\\|b c')

    def test_markdown_adds_no_non_ascii_of_its_own(self):
        self.write_meter()
        self.add_baton('long', next_action='word ' * 80)
        self.add_baton('arabic', next_action='\u0645\u0631\u062d\u0628\u0627')
        for flag in ([], ['--md']):
            _, out = self.run_board(*flag)
            with self.subTest(flag=flag):
                self.assertIn('...', out)                             # clipped with ASCII dots, not an ellipsis char
                self.assertNotIn('\u2026', out)
                self.assertIn('\u0645\u0631\u062d\u0628\u0627', out)   # text from batons passes through as is
                # The capacity line deliberately uses middle-dot separators.
                self.assertEqual(out.replace('\u0645\u0631\u062d\u0628\u0627', '').replace('·', '').isascii(), True)

    def test_json_has_every_section(self):
        self.populate(live=2)
        rc, out = self.run_board('--json')
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(set(data), {'generated_at', 'all', 'meters', 'attention', 'signals', 'sessions', 'lanes', 'jobs', 'batons', 'worktrees', 'waiting', 'capacity', 'host', 'queue'})
        self.assertEqual(set(data['jobs']), {'live', 'finished', 'unreadable', 'scanned'})
        self.assertNotIn('_all_finished', data['jobs'])

    def test_finished_count_option(self):
        self.populate(live=1)
        _, out = self.run_board('--json', '--finished', '2')
        self.assertEqual(len(json.loads(out)['jobs']['finished']), 2)

    def test_usage_errors(self):
        for argv in (['--watch', '0'], ['--finished', '-1'], ['--json', '--md'], ['--bogus']):
            with self.subTest(argv=argv), redirect_stdout(io.StringIO()), mock.patch('sys.stderr', io.StringIO()):
                self.assertEqual(twinlane.main(['board', *argv]), common.EXIT_USAGE)


class SafetyTests(BoardBase):
    def test_reads_only_never_writes_never_probes_an_engine(self):
        self.write_meter()
        self.add_job('20261009-100000-claude-aaaa', 'running')
        self.add_job('20261009-090000-claude-cccc', 'done', final='MERGE\nx', ended_ago=60)
        self.add_baton('alpha', approvals=['x'])
        before = tree_state(self.tmp)

        def boom(*a, **k):
            raise AssertionError('board must not probe an engine or start a process of its own')

        with mock.patch.object(meter, 'read', boom), mock.patch.object(meter, 'read_claude_raw', boom), \
                mock.patch.object(meter, 'read_codex_raw', boom), mock.patch.object(meter, 'read_app_server', boom), \
                mock.patch.object(meter, '_http_get', boom), mock.patch.object(meter, 'record_claude', boom):
            for flag in ([], ['--md'], ['--json'], ['--all']):
                self.assertEqual(self.run_board(*flag)[0], 0)
        self.assertEqual(tree_state(self.tmp), before)

    def test_board_on_an_empty_home_creates_no_directory(self):
        # Round-3 finding: `twinlane board` ran ensure_dirs() first and created state/, jobs/, batons/.
        empty = os.path.join(self.tmp, 'empty-home')
        os.makedirs(empty)
        saved = (common.STATE, common.JOBS, common.BATONS)
        common.STATE, common.JOBS, common.BATONS = (os.path.join(empty, d) for d in ('state', 'jobs', 'batons'))
        try:
            for flag in ([], ['--json'], ['--md']):
                with self.subTest(flag=flag):
                    self.assertEqual(self.run_board(*flag)[0], 0)
                    self.assertEqual(os.listdir(empty), [])
        finally:
            common.STATE, common.JOBS, common.BATONS = saved

    def test_cached_meter_older_than_ttl_is_still_only_a_cache_read(self):
        self.write_meter(age=99999)
        before = tree_state(self.tmp)
        with mock.patch.object(meter, 'read', side_effect=AssertionError('no refresh')):
            data = board.collect(policy=self.policy)
        self.assertEqual(data['meters']['claude']['status'], 'unknown')
        self.assertEqual(tree_state(self.tmp), before)


class WatchTests(unittest.TestCase):
    def test_frames_redraw_every_interval(self):
        out, sleeps, n = io.StringIO(), [], iter(range(10))
        board.frames(lambda: f'frame {next(n)}', 2.5, out=out, sleep=sleeps.append, max_frames=3)
        self.assertEqual(out.getvalue().count('frame'), 3)
        self.assertEqual(sleeps, [2.5, 2.5])
        self.assertNotIn('\x1b', out.getvalue())                  # no escape codes into a pipe

    def test_tty_is_cleared_between_frames(self):
        class Tty(io.StringIO):
            def isatty(self):
                return True
        out = Tty()
        board.frames(lambda: 'x', 1, out=out, sleep=lambda s: None, max_frames=2)
        self.assertEqual(out.getvalue().count('\x1b[H\x1b[2J'), 2)

    def test_cli_watch_stops_on_ctrl_c(self):
        with mock.patch.object(board, 'frames', side_effect=KeyboardInterrupt), \
                mock.patch.object(board, 'collect', return_value={}):
            self.assertEqual(board.cli(['--watch', '1']), common.EXIT_OK)


class WorktreeTests(BoardBase):
    def setUp(self):
        super().setUp()
        self.main = os.path.join(self.tmp, 'repo')
        os.makedirs(self.main)
        git(self.main, 'init', '-q', '-b', 'master')
        with open(os.path.join(self.main, 'a.txt'), 'w', encoding='utf-8') as h:
            h.write('a\n')
        git(self.main, 'add', 'a.txt')
        git(self.main, 'commit', '-q', '-m', 'init')
        self.feat = os.path.join(self.tmp, 'wt-feat')
        git(self.main, 'worktree', 'add', '-q', '-b', 'feat', self.feat)
        with open(os.path.join(self.feat, 'f.txt'), 'w', encoding='utf-8') as h:
            h.write('f\n')
        git(self.feat, 'add', 'f.txt')
        git(self.feat, 'commit', '-q', '-m', 'feat work')
        with open(os.path.join(self.feat, 'dirty1.txt'), 'w', encoding='utf-8') as h:
            h.write('d\n')
        with open(os.path.join(self.feat, 'dirty2.txt'), 'w', encoding='utf-8') as h:
            h.write('d\n')
        self.done = os.path.join(self.tmp, 'wt-done')
        git(self.main, 'worktree', 'add', '-q', '-b', 'shipped', self.done)
        with open(os.path.join(self.main, 'b.txt'), 'w', encoding='utf-8') as h:
            h.write('b\n')
        git(self.main, 'add', 'b.txt')
        git(self.main, 'commit', '-q', '-m', 'master moves on')       # shipped is now 0 ahead / 1 behind
        self.gone = os.path.join(self.tmp, 'wt-gone')
        git(self.main, 'worktree', 'add', '-q', '-b', 'vanished', self.gone)
        shutil.rmtree(self.gone)
        self.add_baton('wt', root=baton.norm_dir(self.main), worktree=baton.norm_dir(self.feat))

    def test_ahead_behind_dirty_merged_and_missing(self):
        data = board.collect(policy=self.policy)
        (repo,) = data['worktrees']['repos']
        self.assertEqual(repo['base'], 'master')
        rows = {w['branch']: w for w in repo['worktrees']}
        self.assertEqual((rows['feat']['ahead'], rows['feat']['behind'], rows['feat']['dirty']), (1, 1, 2))
        self.assertFalse(rows['feat']['merged'])
        self.assertTrue(rows['feat']['mine'])
        self.assertEqual((rows['shipped']['ahead'], rows['shipped']['behind'], rows['shipped']['dirty']), (0, 1, 0))
        self.assertTrue(rows['shipped']['merged'])
        self.assertTrue(rows['vanished']['missing'])
        order = [w['branch'] for w in repo['worktrees']]
        self.assertEqual(order[0], 'feat')                           # unmerged and named by a baton: first
        self.assertEqual(order.index('shipped') < order.index('vanished'), True)
        self.assertNotIn('master', order)                            # the main checkout is not listed
        text = board.render_text(data)
        self.assertIn('feat  +1/-1 vs master  dirty 2', text)
        self.assertIn('shipped  +0/-1 vs master  dirty 0  MERGED', text)
        self.assertIn('MISSING on disk', text)
        self.assertIn('1 merged', text)

    def test_default_probes_only_the_most_recent_worktrees(self):
        with mock.patch.object(board, 'PROBE_LIMIT', 1):
            data = board.collect(policy=self.policy)
        rows = data['worktrees']['repos'][0]['worktrees']
        self.assertEqual(sum(1 for w in rows if w['checked']), 1)
        self.assertEqual(rows[0]['branch'], 'feat')                  # the baton's own worktree is probed first
        text = board.render_text(data)
        self.assertIn('1 checked', text)
        with mock.patch.object(board, 'PROBE_LIMIT', 1):
            full = board.collect(show_all=True, policy=self.policy)
        self.assertEqual(sum(1 for w in full['worktrees']['repos'][0]['worktrees'] if w['checked']), 2)  # missing: none

    def test_git_state_is_unchanged(self):
        before = tree_state(self.tmp)
        board.collect(policy=self.policy)
        self.assertEqual(tree_state(self.tmp), before)


# Synthetic tokens, assembled from parts so no scanner mistakes this file for a leak. (text, the part that must vanish)
SHAPES = {
    'openai-sk': ('sk-' + 'proj1234567890ABCDEFGHIJKLMN',) * 2,
    'anthropic-sk-ant': ('sk-ant-' + 'api03-ABCDEFGHIJKLMNOPQRSTUVWX',) * 2,
    'github-ghp': ('ghp_' + 'AbCdEf0123456789AbCdEf0123456789',) * 2,
    'github-gho': ('gho_' + 'AbCdEf0123456789AbCdEf0123456789',) * 2,
    'github-pat': ('github_pat_' + '11ABCDEFG0123456789_abcdefghijklmnop',) * 2,
    'slack-xoxb': ('xoxb-' + '123456789012-abcdefghijkl',) * 2,
    'slack-xoxp': ('xoxp-' + '123456789012-abcdefghijkl',) * 2,
    'slack-xoxa': ('xoxa-' + '123456789012-abcdefghijkl',) * 2,
    'slack-xoxs': ('xoxs-' + '123456789012-abcdefghijkl',) * 2,
    'aws': ('AKIA' + 'ABCDEFGHIJKLMNOP',) * 2,
    'jwt': ('eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r',) * 2,
    'bearer': ('Authorization: Bearer abcdefGHIJKL0123456789xyz', 'abcdefGHIJKL0123456789xyz'),
    'token-kv': ('token=hunter2hunter2', 'hunter2hunter2'),
    'password-kv': ('password: correct-horse-battery', 'correct-horse-battery'),
    'secret-json': ('{"client_secret": "s3cr3t-value-123"}', 's3cr3t-value-123'),
    'key-kv': ('see https://x.example/api?key=AbC123zzTop', 'AbC123zzTop'),
    'api-key-env': ('EXAMPLE_ACCESS_TOKEN=plainvalue987', 'plainvalue987'),
    'url-credentials': ('clone https://deploy:user@example.com/x.git', 'pa55w0rd99'),
    'hex-run': ('hash ' + '0123456789abcdef' * 3, '0123456789abcdef' * 3),
    'base64-run': ('blob Zm9vYmFyQmF6MTIzNDU2Nzg5MGFiY2RlZkdISUpLTE1O', 'Zm9vYmFyQmF6MTIzNDU2Nzg5MGFiY2RlZkdISUpLTE1O'),
}


class ShapeTests(BoardBase):
    """One filter (baton.redact) covers every token shape in every field of every format, and in a handoff."""

    def test_redact_each_shape(self):
        for name, (text, secret) in SHAPES.items():
            with self.subTest(shape=name):
                self.assertNotIn(secret, baton.redact(f'before {text} after'))

    def test_redact_keeps_paths_ids_and_prose(self):
        for keep in ('\x43:\x2fUsers/alice/.twinlane-wt/demo-board/twinlane/board.py', '20000101-000000-claude-demo',
                     'land commit 21adb35 on master', 'the key insight: retry later', 'example-review-doc'):
            with self.subTest(keep=keep):
                self.assertEqual(baton.redact(keep), keep)

    def test_real_path_shapes_survive_redact_unchanged(self):
        keep = [
            '\x43:\x2fUsers/alice/Documents/example-review',
            '\x43:\\Users\\alice\\.twinlane-wt\\rv-blboard',
            '\x43:\\Users\\alice\\Documents\\example-review\\plan.md',
            '//fileserver/Share/ACME/reports/summary-v2.xlsx',
            '\\\\fileserver\\Share\\ACME\\reports\\summary-v2.xlsx',
            '/srv/build/Projects/ExampleOps/release-candidate/twinlane/board.py',
            '/mnt/c\x2fUsers/alice/.twinlane-wt/DemoReviewWorktree',
            'worktree \x43:/work/example-repo/.claude/worktrees/example-repo-feature 27e7e22 [demo-board]',
            '20000101-000001-claude-demo', 'example job is running',
            '27e7e22', '27e7e22a5b8d1c3', '2026-10-09T11:38:24Z',
            'https://example.com/ACME/example-repo2026/pull/412/files?diff=split',
            'branch feature/ExampleFeature',
            'example-review-doc-Phase2',
            'Alice asked: \u0645\u0631\u062d\u0628\u0627 \u0628\u0627\u0644\u0639\u0627\u0644\u0645 '
            '\u0627\u0644\u062a\u0642\u0631\u064a\u0631 \u0627\u0644\u0646\u0647\u0627\u0626\u064a 2026',
        ]
        for text in keep:
            with self.subTest(text=text):
                self.assertEqual(baton.redact(text), text)
                self.assertEqual(baton.redact(f'see {text} now'), f'see {text} now')

    def test_a_secret_next_to_a_path_is_still_masked(self):
        blob = 'Zx9Qw3Er7Ty1Ui5Op2As6Df0Gh4Jk8Lm3Nb7Vc1Xz5'
        for text in (f'path=\x43:\\x\\{blob}', f'/{blob}'):
            with self.subTest(text=text):
                self.assertNotIn(blob, baton.redact(text))

    def test_macos_temp_paths_are_kept(self):
        # /var is a symlink to /private/var, so a resolved temp path starts with /private/var
        for path in ('/var/folders/36/tjdph2t965j8snz9_vkdnw0r0000gn/T/tmpk3x9q2/forward/claude-a.md',
                     '/private/var/folders/36/tjdph2t965j8snz9_vkdnw0r0000gn/T/tmpk3x9q2/forward/claude-a.md'):
            with self.subTest(path=path):
                self.assertEqual(baton.redact(f'Continue from {path}'), f'Continue from {path}')
        blob = 'Zx9Qw3Er7Ty1Ui5Op2As6Df0Gh4Jk8Lm3Nb7Vc1Xz5'
        self.assertNotIn(blob, baton.redact(f'/var/folders/36/tjdph2t965j8snz9_vkdnw0r0000gn/T/{blob}/x.md'))

    def test_base64_secrets_containing_slash_and_plus_are_masked(self):
        import base64
        import random
        rng = random.Random(20261009)
        secrets = ['wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY', 'Zx9Qw3Er7Ty1Ui5Op2As6Df0Gh4Jk8Lm3Nb7Vc1Xz5/+Q=']
        while len(secrets) < 300:
            token = base64.b64encode(bytes(rng.randrange(256) for _ in range(rng.choice((24, 30, 33)))))
            secrets.append(token.decode())
        for sec in secrets:
            if not (any(c.islower() for c in sec) and any(c.isupper() for c in sec) and any(c.isdigit() for c in sec)):
                continue
            for text in (sec, f'key is {sec} ok', f'aws_secret {sec}', f'/srv/{sec}'):
                with self.subTest(text=text):
                    self.assertNotIn(sec, baton.redact(text))
                    self.assertNotIn(sec[8:30], baton.redact(text))

    def test_word_shaped_slash_secret_outside_a_path_is_masked(self):
        # Round-3 bypass: word-like parts joined by `/` read as a path by shape; only context makes a path now.
        import random
        rng = random.Random(1009)
        words = ['Alpha', 'Bravo', 'Charlie', 'Delta', 'Echo', 'Foxtrot', 'Golf', 'Hotel', 'India', 'Juliet']
        tokens = ['Alpha1/Bravo2/Charlie3/Delta4/Echo5/Foxtrot6']
        while len(tokens) < 40:
            tokens.append('/'.join(rng.choice(words) + str(rng.randrange(10, 99)) for _ in range(6)))
        for sec in tokens:
            for text in (sec, f'key is {sec} ok', f'({sec})', f'"{sec}"'):
                with self.subTest(text=text):
                    self.assertNotIn(sec, baton.redact(text))

    def test_full_git_sha_survives_only_when_named_as_one(self):
        sha = '0123456789abcdef0123456789abcdef01234567'
        self.assertEqual(len(sha), 40)
        for keep in (f'commit {sha}', f'land commit {sha} on master', f'HEAD {sha}', f'sha: {sha}',
                     f'merge {sha}', f'{sha}..{sha}', f'git diff {sha[:7]}..{sha}', f'revert {sha}'):
            with self.subTest(keep=keep):
                self.assertEqual(baton.redact(keep), keep)
        for mask in (sha, f'token {sha}', f'value {sha} here', 'commit ' + sha[:39] + 'ab'):  # 41 hex: no SHA
            with self.subTest(mask=mask):
                self.assertIn('[REDACTED-HEX]', baton.redact(mask))

    def test_no_real_worktree_path_is_masked(self):
        # A disposable worktree path must come out of the filter unchanged.
        repo = os.path.join(self.tmp, 'example-repo')
        subprocess.run(['git', 'init', '-q', repo], check=True, capture_output=True)
        r = subprocess.run(['git', 'worktree', 'list', '--porcelain'], cwd=repo, capture_output=True, text=True)
        paths = [ln[9:] for ln in r.stdout.splitlines() if ln.startswith('worktree ')]
        self.assertTrue(paths)
        self.assertEqual([p for p in paths if baton.redact(p) != p], [])

    def test_every_shape_in_every_field_and_format(self):
        self.write_meter()
        for name, (text, secret) in SHAPES.items():
            shutil.rmtree(common.BATONS)
            shutil.rmtree(common.JOBS)
            common.ensure_dirs()
            self.add_baton('leaky', next_action=f'go {text} now', approvals=[f'ask {text}'], in_flight=[text.replace(' ', '')])
            self.add_job('20261009-090000-claude-cccc', 'done', name=f'job {text}', final=f'MERGE {text}\nx',
                         ended_ago=60, cwd=os.path.join(self.tmp, 'wt ' + text))
            for flag in ([], ['--md'], ['--json'], ['--all'], ['--all', '--md'], ['--all', '--json']):
                rc, out = self.run_board(*flag)
                with self.subTest(shape=name, flag=flag):
                    self.assertEqual(rc, 0)
                    self.assertNotIn(secret, out)

    def test_handoff_redacts_baton_and_job_fields(self):
        for name, (text, secret) in SHAPES.items():
            b = baton.empty_baton('leaky')
            b['task'].update({'title': f't {text}', 'goal': f'g {text}'})
            b['progress']['next_action'] = f'n {text}'
            b['holds']['approvals_pending'] = [{'action': f'a {text}', 'ask_text': f'q {text}'}]
            ctx = {'slug': 'leaky', 'cwd': self.tmp, 'generated_at': '2026-10-09T00:00:00Z', 'baton': b,
                   'jobs': [{'id': 'j1', 'engine': 'claude', 'status': 'running', 'name': f'job {text}'}],
                   'meter': {}, 'git': {}, 'verify': []}
            with self.subTest(shape=name):
                self.assertNotIn(secret, baton.render_handoff(ctx))
                self.assertNotIn(secret, baton.fit_handoff(ctx))


class RobustnessTests(BoardBase):
    def test_live_job_is_found_behind_many_finished_jobs(self):
        self.write_meter()
        self.add_job('20261001-000000-claude-old0', 'running', started_ago=100)
        for i in range(105):
            self.add_job(f'20261009-{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}-claude-{i:04x}', 'done',
                         started_ago=500, ended_ago=400)
        data = board.collect(policy=self.policy)
        self.assertEqual([r['id'] for r in data['jobs']['live']], ['20261001-000000-claude-old0'])
        self.assertEqual(len(data['jobs']['finished']), board.FINISHED_DEFAULT)

    def test_an_old_timeout_whose_engine_outlived_its_kill_is_live_behind_many_finished_jobs(self):
        self.write_meter()
        old = '20261001-000000-claude-surv'
        d = self.add_job(old, 'timeout', started_ago=200000, ended_ago=190000)
        common.write_json(os.path.join(d, 'survivor.json'), {'engine': 'claude', 'pid': os.getpid(),
                                                             'created': common.process_created(os.getpid()),
                                                             'why': 'engine process survived its kill'})
        for i in range(105):
            self.add_job(f'20261009-{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}-claude-{i:04x}', 'done',
                         started_ago=500, ended_ago=400)
        data = board.collect(policy=self.policy)
        self.assertEqual([(r['id'], r['status']) for r in data['jobs']['live']], [(old, 'orphaned')])
        self.assertIn(f'orphaned: {old}', ' '.join(a['text'] for a in data['signals']))

    def test_a_long_job_created_long_ago_that_failed_recently_is_in_attention(self):
        # Round-3 finding: the shortcut skipped by CREATION age; a 29 h-old job that failed 1 h ago vanished.
        self.write_meter()
        old = jid_ago(29 * 3600, 'long')
        self.add_job(old, 'failed', started_ago=29 * 3600, ended_ago=3600, timeout=40 * 3600)
        for i in range(board.FINISHED_DEFAULT + 2):
            self.add_job(jid_ago(600 - i, f'n{i:03d}'), 'done', started_ago=600, ended_ago=500, final='MERGE')
        data = board.collect(policy=self.policy)
        self.assertIn(old, ' '.join(a['text'] for a in data['signals']))
        # a job that both started and ended outside the window is still skipped (the shortcut still works)
        older = jid_ago(60 * 3600, 'gone')
        self.add_job(older, 'failed', started_ago=60 * 3600, ended_ago=59 * 3600)
        data = board.collect(policy=self.policy)
        self.assertNotIn(older, ' '.join(a['text'] for a in data['signals']))

    def test_pending_approval_is_never_pushed_off_the_screen(self):
        self.write_meter()
        for i in range(8):
            self.add_job(f'20261009-09{i:02d}00-claude-f{i:03x}', 'failed', started_ago=900, ended_ago=800)
        self.add_baton('needs-ok', approvals=['email the mailing list'])
        data = board.collect(policy=self.policy)
        self.assertEqual(data['attention'][0]['kind'], 'inbox')
        rc, out = self.run_board()
        self.assertIn('needs-ok:H1: ok? [records: email the mailing list]', out)
        self.assertIn('more (--all)', out)                                    # the capped rest says how many
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith('! ')), 1)
        rc, md = self.run_board('--md')
        self.assertIn('email the mailing list', md)
        self.assertIn('more (`--all`)', md)

    def test_engine_stop_stays_in_signals_and_human_questions_are_capped(self):
        self.write_meter(claude={'status': 'stop', 'max_pct': 90.0})
        for i in range(3):
            self.add_baton(f'b{i}', approvals=[f'ok{i}a', f'ok{i}b', f'ok{i}c'])      # 9 approvals > cap of 6
        for i in range(4):
            self.add_job(f'20261009-09{i:02d}00-claude-f{i:03x}', 'failed', started_ago=900, ended_ago=800)
        data = board.collect(policy=self.policy)
        kinds = [a['kind'] for a in data['signals']]
        self.assertEqual(kinds[0], 'engine')
        self.assertEqual({a['kind'] for a in data['attention']}, {'inbox'})
        _, out = self.run_board()
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith('! ')), 5)
        self.assertIn('claude STOP', out)
        self.assertIn('failed:', out)  # job problems remain visible in signals
        self.assertIn('+4 hidden (twinlane inbox --all)', out)

    def test_malformed_baton_is_labelled_not_fatal(self):
        self.write_meter()
        self.add_baton('good', approvals=['ok?'])
        b = baton.empty_baton('bad')
        b['owner'] = ['claude']
        b['progress'] = 'oops'
        b['holds']['approvals_pending'] = 'nope'
        common.write_json(baton.baton_path('bad'), b)
        with open(baton.baton_path('worse'), 'w', encoding='utf-8') as h:
            h.write('[1, 2, 3]')
        with open(baton.baton_path('broken'), 'w', encoding='utf-8') as h:
            h.write('{not json')
        for flag in ([], ['--md'], ['--json'], ['--all']):
            rc, out = self.run_board(*flag)
            self.assertEqual(rc, 0, flag)
        data = board.collect(policy=self.policy)
        rows = {b['slug']: b for b in data['batons']}
        self.assertEqual(set(rows), {'good', 'bad', 'worse', 'broken'})
        self.assertEqual(rows['bad']['problems'], ['owner', 'progress', 'holds.approvals_pending'])
        self.assertEqual(rows['worse']['problems'], ['not a JSON object'])
        text = '\n'.join(a['text'] for a in data['signals'])
        self.assertIn('malformed baton [bad]: owner, progress, holds.approvals_pending', text)
        self.assertIn('malformed baton [worse]', text)
        self.assertIn('malformed baton [broken]', text)

    def test_list_batons_survives_wrong_types(self):
        b = baton.empty_baton('bad')
        b['owner'] = ['x']
        b['updated_at'] = 12
        b['progress'] = []
        b['repo'] = 'r'
        common.write_json(baton.baton_path('bad'), b)
        (row,) = baton.list_batons()
        self.assertEqual((row['slug'], row['owner'], row['next_action']), ('bad', None, None))

    JUNK = [None, 0, 1.5, True, 'x', '', [], [1, 'a'], {}, {'a': 1}, [[]], '2026-10-09T00:00:00Z']

    def _paths(self, obj, prefix=()):
        yield prefix
        if isinstance(obj, dict):
            for k, v in obj.items():
                yield from self._paths(v, prefix + (k,))
        elif isinstance(obj, list) and obj:
            yield from self._paths(obj[0], prefix + (0,))

    @staticmethod
    def _set(obj, path, value):
        import copy
        obj = copy.deepcopy(obj)
        cur = obj
        for k in path[:-1]:
            cur = cur[k]
        cur[path[-1]] = value
        return obj

    def test_fuzz_baton_fields_with_wrong_types(self):
        self.write_meter()
        base = baton.empty_baton('base')
        base['progress']['in_flight'] = [{'what': 'w', 'job': 'j1', 'check': 'c'}]
        base['holds']['approvals_pending'] = [{'action': 'a', 'ask_text': 'q'}]
        base['jobs'] = [{'id': 'j1', 'engine': 'claude', 'status': 'running'}]
        base['evidence'] = [{'kind': 'test', 'path': 'p', 'result': 'ok'}]
        n = 0
        for path in self._paths(base):
            if not path:
                continue
            for junk in self.JUNK:
                n += 1
                common.write_json(os.path.join(common.BATONS, f'f{n}.json'), self._set(base, path, junk))
        # whole-file junk too, and slug-in-file mismatches
        for i, text in enumerate(['null', '"str"', '7', '[]', '{}', '']):
            with open(os.path.join(common.BATONS, f'w{i}.json'), 'w', encoding='utf-8') as h:
                h.write(text)
        for flag in ([], ['--md'], ['--json'], ['--all']):
            rc, out = self.run_board(*flag)
            with self.subTest(flag=flag):
                self.assertEqual(rc, 0)
        data = board.collect(policy=self.policy)
        self.assertGreaterEqual(len(data['batons']), n)

    def test_fuzz_job_files_with_wrong_types(self):
        self.write_meter()
        self.add_job('20261009-090000-claude-aaaa', 'running')
        self.add_job('20261009-090001-claude-bbbb', 'done', final='MERGE\nx', ended_ago=60)
        n = 0
        for source in ('20261009-090000-claude-aaaa', '20261009-090001-claude-bbbb'):
            for fname in ('job.json', 'status.json', 'result.json'):
                src = os.path.join(common.JOBS, source, fname)
                if not os.path.isfile(src):
                    continue
                base = common.read_json(src)
                for path in self._paths(base):
                    if not path:
                        continue
                    for junk in self.JUNK:
                        n += 1
                        dst = os.path.join(common.JOBS, f'20261009-1{n:05d}-claude-z{n:03x}')
                        shutil.copytree(os.path.join(common.JOBS, source), dst)
                        common.write_json(os.path.join(dst, fname), self._set(base, path, junk))
        for flag in ([], ['--md'], ['--json'], ['--all']):
            rc, out = self.run_board(*flag)
            with self.subTest(flag=flag):
                self.assertEqual(rc, 0)
        data = board.collect(show_all=True, policy=self.policy)
        every = data['jobs']['live'] + data['jobs']['unreadable'] + [r for r in data['jobs']['finished']]
        self.assertGreaterEqual(len(every), n)                   # nothing vanished

    def test_fuzz_meter_fields_with_wrong_types(self):
        base = {'status': 'ok', 'source': 'oauth', 'reading_at': iso_ago(5), 'max_pct': 50.0,
                'next_reset': iso_in(60), 'extra_usage_enabled': False, 'on_credits': False, 'plan': 'p',
                'credits': {'has': True, 'unlimited': False}, 'note': 'n', 'free_resets': 2,
                'windows': [{'name': '5h', 'pct': 10.0, 'resets_at': iso_in(60)}]}
        for path in self._paths(base):
            if not path:
                continue
            for junk in self.JUNK:
                for engine in ('claude', 'codex'):
                    common.write_json(os.path.join(common.STATE, 'meter.json'),
                                      {'read_at': iso_ago(1), 'claude': base, 'codex': base,
                                       engine: self._set(base, path, junk)})
                    data = board.collect(policy=self.policy)
                    with self.subTest(path=path, junk=junk, engine=engine):
                        board.render_text(data), board.render_md(data), json.dumps(data)

    def test_corrupt_job_files_are_unreadable_not_queued_or_missing(self):
        self.write_meter()
        cases = [('job.json', '{not json', 'job.json invalid'), ('status.json', '[1]', 'status.json invalid'),
                 ('result.json', '[1]', 'result.json invalid'), ('job.json', '"x"', 'job.json invalid')]
        want = {}
        for i, (fname, text, problem) in enumerate(cases):
            jid = jid_ago(600 + i * 10, f'c{i:03d}')
            self.add_job(jid, 'running')
            with open(os.path.join(common.JOBS, jid, fname), 'w', encoding='utf-8') as h:
                h.write(text)
            want[jid] = [problem]
        nospec = jid_ago(900, 'nosp')
        os.makedirs(os.path.join(common.JOBS, nospec))
        want[nospec] = ['job.json missing']
        data = board.collect(policy=self.policy)
        self.assertEqual(data['jobs']['live'], [])
        self.assertEqual({r['id']: r['problems'] for r in data['jobs']['unreadable']}, want)
        text = '\n'.join(a['text'] for a in data['signals'])
        for jid, problems in want.items():
            self.assertIn(f'unreadable job {jid}: {problems[0]}', text)
        _, out = self.run_board()
        self.assertIn('unreadable 5', out)
        self.assertNotIn(' queued ', '\n'.join(line for line in out.splitlines() if not line.startswith('writers ')))

    def test_attention_covers_an_old_failure_behind_many_newer_finished_jobs(self):
        self.write_meter()
        old = jid_ago(5 * 3600, 'fail')
        self.add_job(old, 'failed', started_ago=5 * 3600, ended_ago=5 * 3600 - 60)
        for i in range(101):
            self.add_job(jid_ago(600 + i, f'd{i:03x}'), 'done', started_ago=600 + i, ended_ago=300, final='MERGE\nx')
        data = board.collect(policy=self.policy)
        self.assertEqual(len(data['jobs']['finished']), board.FINISHED_DEFAULT)      # only the shown rows are capped
        text = '\n'.join(a['text'] for a in data['signals'])
        self.assertIn(old, text)
        self.assertIn('failed', text)
        self.assertEqual(board.collect(policy=self.policy, show_all=True)['jobs']['scanned'], 102)

    def test_a_job_created_long_ago_with_a_receipt_is_not_read_in_full(self):
        self.write_meter()
        for i in range(board.FINISHED_DEFAULT + 3):
            self.add_job(jid_ago(600 + i, f'n{i:03x}'), 'done', started_ago=600, ended_ago=300)
        self.add_job(jid_ago(board.OLD_JOB_S + 3600, 'ancient'), 'done', started_ago=board.OLD_JOB_S + 3600,
                     ended_ago=board.OLD_JOB_S)
        data = board.collect(policy=self.policy)
        self.assertEqual(data['jobs']['scanned'], board.FINISHED_DEFAULT + 3)

    def test_a_partial_job_past_the_grace_period_is_unreadable_not_queued(self):
        self.write_meter()
        jid = jid_ago(board.NEW_DIR_GRACE_S + 120, 'part')
        self.add_job(jid, 'running')
        os.remove(os.path.join(common.JOBS, jid, 'status.json'))               # job.json only
        fresh = jid_ago(5, 'frsh')
        self.add_job(fresh, 'running')
        os.remove(os.path.join(common.JOBS, fresh, 'status.json'))             # still being created
        data = board.collect(policy=self.policy)
        self.assertEqual([(r['id'], r['problems']) for r in data['jobs']['unreadable']],
                         [(jid, ['status.json missing'])])
        self.assertEqual([r['id'] for r in data['jobs']['live']], [fresh])      # inside the grace period: queued
        self.assertIn(f'unreadable job {jid}: status.json missing', '\n'.join(a['text'] for a in data['signals']))
        self.assertNotIn(fresh, '\n'.join(a['text'] for a in data['signals']))

    def test_a_job_dir_still_being_created_is_not_flagged(self):
        self.write_meter()
        os.makedirs(os.path.join(common.JOBS, common.stamp() + '-claude-new0'))
        self.assertEqual(board.collect(policy=self.policy)['jobs']['unreadable'], [])


class CollectedMarkerTests(BoardBase):
    def test_result_writes_the_marker_and_the_board_only_reads_it(self):
        from twinlane import jobs
        self.write_meter()
        jid = '20261009-090000-claude-cccc'
        self.add_job(jid, 'done', final='MERGE\nbody', started_ago=900, ended_ago=600)
        data = board.collect(policy=self.policy)
        self.assertIn('1 finished, not yet read', '\n'.join(a['text'] for a in data['signals']))
        self.assertFalse(data['jobs']['finished'][0]['collected'])
        self.assertFalse(os.path.exists(os.path.join(common.JOBS, jid, 'collected')))   # the board never writes it
        self.assertIn('(unread)', self.run_board()[1])
        with redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.cli('result', [jid]), 0)
        marker = os.path.join(common.JOBS, jid, 'collected')
        self.assertTrue(os.path.isfile(marker))
        with open(marker, encoding='utf-8') as h:
            self.assertIsNotNone(common.parse_time(h.read().strip()))
        data = board.collect(policy=self.policy)
        self.assertTrue(data['jobs']['finished'][0]['collected'])
        self.assertNotIn('not yet read', '\n'.join(a['text'] for a in data['signals']))
        self.assertNotIn('(unread)', self.run_board()[1])

    def test_result_without_a_receipt_does_not_mark(self):
        from twinlane import jobs
        jid = '20261009-090000-claude-aaaa'
        self.add_job(jid, 'running')
        with redirect_stdout(io.StringIO()):
            jobs.cli('result', [jid])
        self.assertFalse(os.path.exists(os.path.join(common.JOBS, jid, 'collected')))

    def test_a_running_receipt_is_not_marked_read(self):
        from twinlane import jobs
        jid = '20261009-090000-claude-rrrr'
        d = self.add_job(jid, 'done', final='MERGE\nbody')
        receipt = common.read_json(os.path.join(d, 'result.json'))
        receipt['status'] = 'running'
        common.write_json(os.path.join(d, 'result.json'), receipt)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.cli('result', [jid]), 0)
        self.assertFalse(os.path.exists(os.path.join(d, 'collected')))
        for status in jobs.TERMINAL:
            receipt['status'] = status
            common.write_json(os.path.join(d, 'result.json'), receipt)
            with redirect_stdout(io.StringIO()):
                jobs.cli('result', [jid])
            with self.subTest(status=status):
                self.assertTrue(os.path.isfile(os.path.join(d, 'collected')))
            os.remove(os.path.join(d, 'collected'))

    def test_marker_failure_does_not_fail_result(self):
        from twinlane import jobs
        jid = '20261009-090000-claude-cccc'
        self.add_job(jid, 'done', final='MERGE\nbody')
        with mock.patch.object(common, 'write_text', side_effect=OSError('read-only')), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.cli('result', [jid]), 0)


class LostTogetherTests(BoardBase):
    """Jobs that went quiet within a minute of each other share one cause: ATTENTION shows them as one line."""
    T0 = dt.datetime(2026, 10, 9, 12, 52, 0, tzinfo=dt.timezone.utc)

    def add_lost(self, tag, at_s, started_ago=900, codex_dir=False):
        """A lost job whose files are all dated T0 + at_s seconds (the runner went quiet then)."""
        jid = jid_ago(started_ago, tag, 'codex')
        d = self.add_job(jid, 'lost', engine='codex', started_ago=started_ago)
        if codex_dir:  # the Codex runner writes its events one folder down
            os.makedirs(os.path.join(d, 'codex'))
            with open(os.path.join(d, 'codex', 'events.jsonl'), 'w', encoding='utf-8') as h:
                h.write('{}\n')
        with open(os.path.join(d, 'runner.log'), 'w', encoding='utf-8') as h:
            h.write('')
        self.date_files(d, at_s)
        return jid, d

    def date_files(self, d, at_s):
        stamp = (self.T0 + dt.timedelta(seconds=at_s)).timestamp()
        for top, _, files in os.walk(d):
            for f in files:
                os.utime(os.path.join(top, f), (stamp, stamp))

    def texts(self):
        return [a['text'] for a in board.collect(policy=self.policy)['signals']]

    def test_four_jobs_lost_within_15_seconds_are_one_line(self):
        self.write_meter()
        ids = [self.add_lost(f'l{i}00', i * 5, started_ago=900 - i)[0] for i in range(4)]
        data = board.collect(policy=self.policy)
        texts = [a['text'] for a in data['signals']]
        lost_lines = [t for t in texts if 'lost' in t]
        self.assertEqual(len(lost_lines), 1, lost_lines)
        line = lost_lines[0]
        self.assertTrue(line.startswith('4 jobs lost together at 12:52Z ('), line)
        for jid in ids:
            self.assertIn(jid, line)
        self.assertIn('a sign-out, shutdown or sleep can end every runner; rerun each with twinlane run --continue-job ID',
                      line)
        item = [a for a in data['signals'] if 'lost together' in a['text']][0]
        self.assertEqual((item['priority'], item['kind']), (board.P_FAIL, 'problem'))
        self.assertEqual(sorted(item['ids']), sorted(ids))
        # grouping never hides a job: every row stays in the data and in --json
        rc, out = self.run_board('--json')
        self.assertEqual(rc, 0)
        rows = [r for r in json.loads(out)['jobs']['finished'] if r['status'] == 'lost']
        self.assertEqual(sorted(r['id'] for r in rows), sorted(ids))
        self.assertTrue(all(r['last_activity_at'] for r in rows))
        rc, text = self.run_board()
        self.assertEqual(sum('lost together' in line for line in text.splitlines()), 1)
        rc, md = self.run_board('--md')
        self.assertIn('4 jobs lost together at 12:52Z', md)

    def test_two_jobs_lost_ten_minutes_apart_keep_their_own_lines(self):
        self.write_meter()
        a, _ = self.add_lost('aaaa', 0)
        b, _ = self.add_lost('bbbb', 600, started_ago=800)
        texts = self.texts()
        lost = [t for t in texts if t.startswith('lost: ')]
        self.assertEqual(len(lost), 2, texts)
        self.assertTrue(any(a in t for t in lost) and any(b in t for t in lost))
        self.assertFalse(any('lost together' in t for t in texts))

    def test_a_single_lost_job_keeps_todays_line(self):
        self.write_meter()
        jid, _ = self.add_lost('eeee', 0)
        self.assertEqual([t for t in self.texts() if 'lost' in t], [f'lost: {jid} codex eeee (15m ago)'])

    def test_the_window_is_60_seconds_from_the_first_job_of_a_group(self):
        self.write_meter()
        first, _ = self.add_lost('f001', 0, started_ago=900)
        second, _ = self.add_lost('f002', 55, started_ago=899)
        third, _ = self.add_lost('f003', 110, started_ago=898)   # 55 s after the second, 110 s after the first
        texts = self.texts()
        grouped = [t for t in texts if 'lost together' in t]
        self.assertEqual(len(grouped), 1)
        self.assertIn('2 jobs lost together', grouped[0])
        self.assertIn(first, grouped[0])
        self.assertIn(second, grouped[0])
        self.assertNotIn(third, grouped[0])
        self.assertTrue(any(t.startswith(f'lost: {third}') for t in texts))

    def test_the_newest_file_one_folder_down_counts_as_activity(self):
        self.write_meter()
        a, da = self.add_lost('c001', 0, codex_dir=True, started_ago=900)
        stamp = (self.T0 + dt.timedelta(seconds=300)).timestamp()
        os.utime(os.path.join(da, 'codex', 'events.jsonl'), (stamp, stamp))
        self.add_lost('c002', 300, started_ago=899)
        self.assertTrue(any('2 jobs lost together at 12:57Z' in t for t in self.texts()), self.texts())

    def test_the_collected_marker_does_not_move_last_activity(self):
        self.write_meter()
        a, da = self.add_lost('m001', 0, started_ago=900)
        self.add_lost('m002', 3, started_ago=899)
        with open(os.path.join(da, 'collected'), 'w', encoding='utf-8') as h:
            h.write(common.iso(common.now()) + '\n')                     # written long after the loss
        self.assertTrue(any('2 jobs lost together at 12:52Z' in t for t in self.texts()), self.texts())

    def test_a_group_counts_once_toward_the_cap_and_keeps_its_priority(self):
        self.write_meter(claude={'status': 'stop', 'max_pct': 90.0})
        for i in range(4):
            self.add_lost(f'g{i}00', i, started_ago=900 - i)
        for i in range(5):
            self.add_job(f'20261009-09{i:02d}00-claude-f{i:03x}', 'failed', started_ago=900, ended_ago=800)
        texts = self.texts()
        self.assertEqual(len(texts), 1 + 1 + 5)                         # STOP, one group line, five failures
        self.assertTrue(texts[0].startswith('claude STOP'))             # P0 first; the group is P2, after it
        _, out = self.run_board()
        self.assertEqual(sum(1 for line in out.splitlines() if line.startswith('! ')), 0)
        self.assertIn('== SIGNALS (7)', out)
        self.assertIn('4 jobs lost together', out)

    def test_a_lost_job_with_no_dated_files_is_not_grouped(self):
        self.write_meter()
        self.add_lost('n001', 0, started_ago=900)
        self.add_lost('n002', 1, started_ago=899)
        with mock.patch.object(board, '_last_activity', return_value=None):
            texts = self.texts()
        self.assertEqual(sum(t.startswith('lost: ') for t in texts), 2)
        self.assertFalse(any('lost together' in t for t in texts))

    def test_dating_the_jobs_changes_nothing_on_disk(self):
        self.write_meter()
        for i in range(3):
            self.add_lost(f'r{i}00', i, started_ago=900 - i)
        before = tree_state(self.tmp)
        board.collect(policy=self.policy)
        self.run_board('--md')
        self.assertEqual(tree_state(self.tmp), before)


if __name__ == '__main__':
    unittest.main()


class FailureErrorTests(BoardBase):
    def test_failed_job_attention_includes_redacted_error(self):
        self.write_meter()
        jid = jid_ago(120, 'fail', 'codex')
        self.add_job(jid, 'failed', engine='codex', extra={'error': 'model X requires a newer version of Codex ' + SECRET})
        with mock.patch.object(baton, 'CLAUDE_PROJECTS', os.path.join(self.tmp, 'claude')), \
                mock.patch.object(baton, 'CODEX_SESSIONS', os.path.join(self.tmp, 'codex')):
            data = board.collect(policy=self.policy)
        text = '\n'.join(a['text'] for a in data['signals'])
        self.assertIn('- model X requires a newer version of Codex', text)
        self.assertNotIn(SECRET, text)
