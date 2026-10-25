"""Writer lanes, human questions, context and checkpoints: synthetic/offline only."""
import contextlib
import datetime as dt
import io
import json
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

import test_support  # noqa: F401; isolate home before twinlane imports
import test_baton
import test_sessions
import test_worktrees
from twinlane import baton, board, common, context, guard, hook, inbox, install, jobs, projects, sessions, wip, worktrees, lanes, land


class Lanes(test_baton.BatonTestBase):
    def setUp(self):
        super().setUp()
        self.policy = common.load_policy()
        self.policy['wip'] = {'max_writers': 2}
        for patcher in (mock.patch.object(common, 'load_policy', return_value=self.policy),
                        mock.patch.object(sessions, 'collect_sessions', return_value={'rows': []})):
            patcher.start()
            self.addCleanup(patcher.stop)

    def lane(self, n, state='active'):
        path = os.path.join(self.tmp, f'lane-{n}')
        os.makedirs(path, exist_ok=True)
        b = baton.empty_baton(f'lane-{n}')
        b['repo']['worktree'] = path
        b['state'] = state
        baton.save(b)
        return path

    def test_same_worktree_and_subdirectory_count_once(self):
        path = self.lane(1)
        self.lane(2, 'review')
        b = baton.empty_baton('alias')
        b['repo']['worktree'] = path
        baton.save(b)
        self.assertEqual(len(wip.collect()), 2)
        with wip.admission(path, 'run', 'again'):
            pass
        with self.assertRaisesRegex(wip.WipError, 'queued: 2/2'):
            with wip.admission(self.repo, 'run', 'three'):
                self.fail('admitted over cap')

    def test_baton_new_queues_fifo_and_deduplicates(self):
        self.lane(1)
        self.lane(2)
        for slug in ('third', 'fourth', 'third'):
            with self.assertRaisesRegex(wip.WipError, 'queued: 2/2'):
                baton.new(slug, 'goal', cwd=self.repo)
        self.assertIsNone(baton.load('third'))
        self.assertEqual([x['name'] for x in wip.queue()], ['third', 'fourth'])
        with contextlib.redirect_stdout(io.StringIO()) as out, mock.patch.object(projects, 'in_worker', return_value=False):
            self.assertEqual(wip.cli(['next', '--force', '--json']), 0)
        self.assertEqual(json.loads(out.getvalue())['name'], 'third')
        self.assertEqual([x['name'] for x in wip.queue()], ['fourth'])

    def test_full_queue_next_preserves_requests_and_uses_versioned_envelope(self):
        self.lane(1)
        self.lane(2)
        with self.assertRaises(wip.WipError):
            with wip.lock():
                wip.check(self.repo, 'run', 'third', self.policy, argv=['run', '--write', '--cd', self.repo])
        before = Path(wip.queue_path()).read_bytes()
        with mock.patch.object(projects, 'in_worker', return_value=False), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(wip.cli(['next']), 3)
        self.assertEqual(before, Path(wip.queue_path()).read_bytes())
        self.assertEqual(common.read_json(wip.queue_path())['version'], 1)

    def test_recent_idle_session_counts_and_old_session_does_not(self):
        rows = [{'engine': 'codex', 'id': 'recent', 'state': 'idle', 'cwd': self.repo, 'age_s': 10},
                {'engine': 'codex', 'id': 'old', 'state': 'idle', 'cwd': self.tmp, 'age_s': 7200}]
        self.assertEqual(len(wip.lanes_from([], [], rows, self.policy)), 1)
        self.assertEqual(wip.lanes_from([], [{'slug': 'park', 'state': 'parked', 'worktree': self.repo}], rows, self.policy), [])
        self.policy['wip']['count_main_checkout'] = False
        self.assertEqual(wip.lanes_from([], [], rows, self.policy), [])

    def test_cli_over_cap_is_exit_three(self):
        self.lane(1)
        self.lane(2)
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['new', 'third', '--goal', 'g', '--cd', self.repo]), 3)

    def test_read_only_jobs_are_not_writers(self):
        self.lane(1)
        for n, profile in ((1, 'code'), (2, 'intel')):
            path = os.path.join(common.JOBS, test_baton.jid(n))
            common.write_json(os.path.join(path, 'job.json'), {'profile': profile, 'cwd': self.repo})
        with mock.patch.object(jobs, 'job_status', return_value='unknown'):
            rows = wip.collect()
        self.assertEqual(len(rows), 2)
        self.assertEqual(sum(len(r['sources']) for r in rows), 2)

    def test_run_rejects_before_job_allocation(self):
        self.lane(1)
        self.lane(2)
        with mock.patch.object(jobs, '_admitted_job') as allocate:
            with self.assertRaisesRegex(jobs.Refusal, 'queued: 2/2'):
                jobs.create_job({'write': True, 'cwd': self.repo, 'name': 'third'})
            allocate.assert_not_called()

    def test_interactive_main_checkout_counts(self):
        with mock.patch.object(sessions, 'collect_sessions', return_value={'rows': [
                {'engine': 'claude', 'id': 'one', 'state': 'active', 'cwd': self.repo},
                {'engine': 'codex', 'id': 'two', 'state': 'mid-turn', 'cwd': self.repo},
                {'engine': 'codex', 'id': 'idle', 'state': 'waiting-on-user', 'cwd': self.tmp}]}):
            rows = wip.collect()
        self.assertEqual(len(rows), 1)
        self.assertEqual(len(rows[0]['sources']), 2)

    def test_terminal_batons_release_slot(self):
        for n, state in enumerate(('landed', 'closed', 'waiting-on-you', 'waiting-external', 'parked')):
            self.lane(n, state)
        self.assertEqual(wip.collect(), [])

    def test_unreadable_custody_and_queue_fail_closed(self):
        common.write_text(baton.baton_path('bad'), '{')
        with self.assertRaisesRegex(wip.WipError, 'unreadable baton'):
            wip.collect()
        common.write_text(wip.queue_path(), '{')
        with self.assertRaisesRegex(wip.WipError, 'queue unreadable'):
            wip.queue()

    def test_concurrent_admissions_never_overbook(self):
        self.policy['wip']['max_writers'] = 1
        results = []
        barrier = threading.Barrier(2)

        def create(n):
            barrier.wait()
            cwd = os.path.join(self.tmp, f'thread-{n}')
            os.makedirs(cwd)
            try:
                baton.new(f'thread-{n}', 'g', cwd=cwd)
                results.append('accepted')
            except wip.WipError:
                results.append('queued')
        threads = [threading.Thread(target=create, args=(i,)) for i in range(2)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(20)
        self.assertEqual(sorted(results), ['accepted', 'queued'])

    def test_resume_cannot_bypass_cap(self):
        self.lane(1)
        self.lane(2)
        path = self.lane(3, 'parked')
        before = Path(baton.baton_path('lane-3')).read_bytes()
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['set', 'lane-3', '--state', 'active']), 3)
        self.assertEqual(Path(baton.baton_path('lane-3')).read_bytes(), before)
        with self.assertRaises(wip.WipError):
            baton.snap('new-snap', path)
        self.assertIsNone(baton.load('new-snap'))

    def test_snap_move_at_cap_queues_without_changing_active_or_review_baton(self):
        self.lane(1)
        self.lane(2)
        for state in ('active', 'review'):
            b = baton.load('lane-1')
            b['state'] = state
            baton.save(b)
            before = Path(baton.baton_path('lane-1')).read_bytes()
            with contextlib.redirect_stderr(io.StringIO()) as err:
                rc = baton.cli('baton', ['snap', 'lane-1', '--cd', self.repo])
            self.assertEqual(rc, 3)
            self.assertIn('queued: 2/2 writer lanes busy', err.getvalue())
            self.assertEqual(Path(baton.baton_path('lane-1')).read_bytes(), before)
        self.assertEqual(len(wip.queue()), 1)
        self.assertEqual(wip.queue()[0]['argv'], ['baton', 'snap', 'lane-1', '--cd', self.repo])

    def test_snap_move_to_counted_toplevel_and_same_toplevel_is_admitted(self):
        self.lane(1)
        b = baton.empty_baton('destination')
        b['repo']['worktree'] = self.repo
        baton.save(b)
        subdir = os.path.join(self.repo, 'src')
        os.makedirs(subdir)
        baton.snap('lane-1', subdir)
        self.assertEqual(common.writer_key(baton.load('lane-1')['repo']['worktree']), common.writer_key(self.repo))
        with mock.patch.object(wip, 'check') as check:
            baton.snap('lane-1', self.repo)
        check.assert_not_called()
        self.assertEqual(wip.queue(), [])

    def test_snap_move_parked_at_cap_does_not_take_a_slot(self):
        self.lane(1)
        self.lane(2)
        self.lane(3, 'parked')
        with mock.patch.object(wip, 'check') as check:
            moved = baton.snap('lane-3', self.repo)
        check.assert_not_called()
        self.assertEqual(moved['state'], 'parked')
        self.assertEqual(common.writer_key(moved['repo']['worktree']), common.writer_key(self.repo))
        self.assertEqual(len(wip.collect()), 2)

    def test_nested_worktree_creation_and_multiple_slots_are_new_capacity(self):
        self.policy['wip']['max_writers'] = 1
        baton.new('occupied', 'goal', cwd=self.repo)
        destination = os.path.join(self.repo, 'nested')
        for slots in (1, 2):
            with self.subTest(slots=slots), self.assertRaisesRegex(wip.WipError, 'queued: 1/1'):
                with wip.lock():
                    wip.check(destination, 'best-of', 'pair', self.policy, slots=slots, new_worktree=True)
        self.assertEqual(wip.queue()[0]['worktree'], os.path.normcase(os.path.realpath(destination)))
        with self.assertRaisesRegex(wip.WipError, 'queued: 1/1'):
            worktrees.add(self.repo, 'nested', destination)
        self.assertFalse(os.path.lexists(destination))
        self.assertNotEqual(common.git(self.repo, 'rev-parse', '--verify', 'refs/heads/nested')[0], 0)

    def test_existing_lane_shortcut_does_not_ignore_additional_slots(self):
        self.policy['wip']['max_writers'] = 1
        baton.new('occupied', 'goal', cwd=self.repo)
        with wip.lock(), self.assertRaisesRegex(wip.WipError, 'queued: 1/1'):
            wip.check(self.repo, 'best-of', 'pair', self.policy, slots=2)

    def test_bestof_nested_contestants_refuse_before_creating_worktrees(self):
        from twinlane import bestof
        baton.new('occupied', 'goal', cwd=self.repo)
        brief = Path(self.tmp) / 'brief.md'
        brief.write_text('Inspect synthetic source.', encoding='utf-8')
        destinations = [os.path.join(self.repo, 'nested', str(n)) for n in range(2)]
        plan = {'brief_sha256': 'fixture', 'kind': 'code', 'repo': self.repo, 'cwd': self.repo,
                'base': 'HEAD', 'base_ref': 'HEAD',
                'contestants': [{'refused': None, 'shared': False, 'worktree': p} for p in destinations]}
        for maximum in (1, 2):
            self.policy['wip']['max_writers'] = maximum
            with self.subTest(maximum=maximum), mock.patch.object(projects, 'in_worker', return_value=False), \
                    mock.patch.object(bestof, 'plan_group', return_value=plan), \
                    mock.patch.object(bestof, '_assign_places'), mock.patch.object(bestof, 'save_group') as save, \
                    mock.patch.object(worktrees, 'add') as add, mock.patch.object(bestof, 'launch') as launch, \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                rc = bestof.cmd_run(['--best-of', 'claude,codex', '--write', '--cd', self.repo,
                                     '--brief', str(brief), '--json'])
            self.assertEqual(rc, 3)
            self.assertIn(f'queued: 1/{maximum}', json.loads(out.getvalue())['reason'])
            save.assert_not_called()
            add.assert_not_called()
            launch.assert_not_called()
        self.assertTrue(all(not os.path.lexists(p) for p in destinations))

    def test_board_and_admission_agree_on_read_only_and_write_jobs(self):
        subdir = os.path.join(self.repo, 'src')
        os.makedirs(subdir)
        for profile, write, expected in (('intel', False, 0), ('code', True, 1), ('intel', True, 1)):
            with self.subTest(profile=profile, write=write):
                jid = test_baton.jid(1)
                common.write_json(os.path.join(common.JOBS, jid, 'job.json'),
                                  {'profile': profile, 'write': write, 'cwd': subdir,
                                   'writer_root': self.repo, 'created_at': common.iso(common.now())})
                common.write_json(os.path.join(common.JOBS, jid, 'status.json'), {'status': 'running'})
                with mock.patch.object(jobs, 'job_status', return_value='running'):
                    row = board._job_row(jid, common.now())
                    data = board.collect(policy=self.policy)
                    admitted = wip.collect(self.policy)
                self.assertEqual((row['profile'], row['write'], row['writer_root']), (profile, write, self.repo))
                self.assertEqual(data['lanes']['busy'], expected)
                self.assertEqual(len(admitted), expected)

    def test_cap_checked_before_route_or_engine_probe(self):
        self.lane(1)
        self.lane(2)
        brief = Path(self.tmp) / 'brief.md'
        brief.write_text('Inspect synthetic source.', encoding='utf-8')
        args = jobs._run_parser().parse_args(['--write', '--cd', self.repo, '--brief', str(brief)])
        with mock.patch.object(jobs, '_meter') as meter:
            with self.assertRaisesRegex(jobs.Refusal, 'queued: 2/2'):
                jobs.plan_run(args, self.policy)
            meter.assert_not_called()

    def test_incomplete_job_refuses_admission(self):
        os.makedirs(os.path.join(common.JOBS, test_baton.jid(1)))
        self.assertEqual(wip.collect()[0]['key'], 'job:' + test_baton.jid(1))


class StatesAndInbox(test_baton.BatonTestBase):
    def test_parked_timeout_derives_stale_without_writing(self):
        b = baton.empty_baton('parked')
        baton.apply_set(b, test_baton._set_args(state='parked', park_for='8h'))
        self.assertEqual(baton.effective_state(b), 'parked')
        b['state_at'] = common.iso(common.now() - dt.timedelta(hours=9))
        self.assertEqual(baton.effective_state(b), 'stale')
        self.assertEqual(b['state'], 'parked')

    def test_inbox_rank_duplicates_and_hidden_count(self):
        waiting = baton.empty_baton('waiting')
        baton.apply_set(waiting, test_baton._set_args(ask='Archive 1 branch?'))
        legacy = baton.empty_baton('legacy')
        legacy['holds']['approvals_pending'] = [{'id': 'H1', 'action': 'Review it?'},
                                                 {'id': 'H2', 'action': 'Review it?'}]
        rows = inbox.items([legacy, waiting], session_rows=[])
        self.assertEqual([r['rank'] for r in rows], [1, 2])
        self.assertEqual(rows[-1]['duplicates'], 2)
        result = inbox.projection(rows, policy={'inbox': {'show': 1}})
        self.assertEqual((result['total'], result['shown'], result['hidden']), (2, 1, 1))
        self.assertIn('1 hidden', inbox.header(result))

    def test_session_bindings_capped_validated_and_removed(self):
        b = baton.empty_baton('bindings')
        with mock.patch.object(baton, 'find_claude_session', return_value=None), mock.patch.object(baton, 'find_codex_session', return_value=None):
            baton.bind_sessions(b, [f'session-{n}' for n in range(9)])
        self.assertEqual(len(b['bound_sessions']), 8)
        self.assertEqual(b['bound_sessions'][0]['id'], 'session-1')
        baton.bind_sessions(b, remove=['session-8'])
        self.assertEqual(len(b['bound_sessions']), 7)
        with self.assertRaises(baton.BatonError):
            baton.bind_sessions(b, ['../bad'])
    def test_blocked_requires_question_or_external(self):
        b = baton.empty_baton('a')
        with self.assertRaisesRegex(baton.BatonError, 'blocked needs'):
            baton.apply_set(b, test_baton._set_args(state='blocked'))
        baton.apply_set(b, test_baton._set_args(ask='Archive these 2 branches?'))
        self.assertEqual(b['state'], 'waiting-on-you')
        self.assertEqual(inbox.items([b])[0]['question'], 'Archive these 2 branches?')
        c = baton.empty_baton('c')
        baton.apply_set(c, test_baton._set_args(external='https://example.com/issue/3'))
        self.assertEqual(c['state'], 'waiting-external')

    def test_invalid_question_and_external_are_rejected(self):
        for kwargs in ({'ask': 'do it'}, {'ask': 'first?\nsecond?'}, {'external': 'local.txt'},
                       {'ask': 'Proceed?', 'external': 'https://example.com'},
                       {'state': 'waiting-on-you'}, {'state': 'waiting-external'}):
            with self.subTest(kwargs=kwargs), self.assertRaises(baton.BatonError):
                baton.apply_set(baton.empty_baton('a'), test_baton._set_args(**kwargs))

    def test_legacy_holds_get_stable_ids_and_answer_records_context_only(self):
        b = baton.empty_baton('legacy')
        b.pop('state')
        b['owner']['engine'] = 'claude'
        b['holds']['approvals_pending'] = [{'action': 'archive 2 branches', 'ask_text': 'Archive 2 branches?'},
                                           {'id': 'H5', 'action': 'archive 3 branches', 'ask_text': 'Archive 3 branches?'}]
        common.write_json(baton.baton_path('legacy'), b)
        before = Path(baton.baton_path('legacy')).read_bytes()
        rows = inbox.items()
        self.assertEqual([r['id'] for r in rows], ['legacy:H5', 'legacy:H6'])
        self.assertEqual(Path(baton.baton_path('legacy')).read_bytes(), before)
        saved = inbox.answer('legacy:H6', grant='Yes, archive those 2 branches')
        answer = saved['holds']['answers'][-1]
        self.assertEqual(answer['words'], 'Yes, archive those 2 branches')
        self.assertFalse(answer['live_write_approval'])
        self.assertEqual(answer['via'], 'inbox')
        self.assertEqual(answer['engine'], 'claude')
        self.assertEqual(len(inbox.items()), 1)

    def test_denial_does_not_resume_or_grant(self):
        b = baton.empty_baton('a')
        baton.apply_set(b, test_baton._set_args(ask='Archive 1 branch?'))
        baton.save(b)
        answered = inbox.answer('a:H1', deny='Keep it for review')
        self.assertEqual(answered['state'], 'parked')
        self.assertEqual(answered['holds']['approvals_granted'], [])
        self.assertEqual(answered['holds']['answers'][0]['reason'], 'Keep it for review')
        with self.assertRaises(baton.BatonError):
            inbox.answer('a:H1', grant='yes')

    def test_inbox_default_five_all_seven_and_oldest_first(self):
        for n in range(7):
            b = baton.empty_baton(f'q-{n}')
            baton.apply_set(b, test_baton._set_args(ask=f'Archive branch {n}?'))
            baton.save(b)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(inbox.cli(['--json']), 0)
        self.assertEqual(len(json.loads(out.getvalue())['items']), 5)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(inbox.cli(['--all', '--json']), 0)
        self.assertEqual(len(json.loads(out.getvalue())['items']), 7)

    def test_unreadable_inbox_is_unknown_not_empty(self):
        common.write_text(baton.baton_path('broken'), '{')
        with self.assertRaisesRegex(baton.BatonError, 'inbox incomplete'):
            inbox.items()

    def test_answer_keeps_original_engine_and_session_after_owner_change(self):
        b = baton.empty_baton('a')
        b['owner']['engine'], b['bound_session'] = 'claude', 'original'
        baton.apply_set(b, test_baton._set_args(ask='Archive 1 branch?'))
        b['owner']['engine'], b['bound_session'] = 'codex', 'successor'
        baton.save(b)
        answer = inbox.answer('a:H1', grant='Yes, archive that branch')['holds']['answers'][-1]
        self.assertEqual((answer['engine'], answer['session_id']), ('claude', 'original'))
        self.assertEqual(answer['via'], 'inbox')

    def test_stale_uses_progress_and_does_not_persist(self):
        b = baton.empty_baton('old')
        b['progress_at'] = common.iso(common.now() - dt.timedelta(days=2))
        b['updated_at'] = common.iso(common.now())
        self.assertEqual(baton.effective_state(b), 'stale')
        self.assertEqual(b['state'], 'active')
        b['progress_at'] = common.iso(common.now())
        self.assertEqual(baton.effective_state(b), 'active')

    def test_landing_never_closes_matching_all_pass_baton(self):
        b = baton.empty_baton('done')
        b['repo'].update(worktree=self.repo, branch='feature')
        b['task']['acceptance'] = [{'id': 'A1', 'criterion': 'works', 'status': 'pass'}]
        baton.save(b)
        common.write_text(baton.baton_path('unrelated-broken'), '{')
        event = {'event': 'landed', 'tree_matches': True, 'branch': 'feature', 'worktree': self.repo}
        baton.record_landing(dict(event, event='landed-unverified'))
        self.assertEqual(baton.load('done')['state'], 'landed')
        self.assertFalse(baton.load('done')['landed']['verified'])
        baton.record_landing(dict(event, worktree=self.tmp))
        self.assertEqual(baton.load('done')['landed']['count'], 1)
        baton.record_landing(event)
        self.assertEqual(baton.load('done')['state'], 'landed')
        self.assertEqual(baton.load('done')['landed']['count'], 2)
        self.assertTrue(baton.load('done')['landed']['verified'])

    def test_verified_landing_with_pending_acceptance_is_landed(self):
        b = baton.empty_baton('done')
        b['repo'].update(worktree=self.repo, branch='feature')
        baton.save(b)
        baton.record_landing({'event': 'landed', 'tree_matches': True, 'branch': 'feature', 'worktree': self.repo})
        self.assertEqual(baton.load('done')['state'], 'landed')

    def test_board_attention_is_only_inbox_then_stale(self):
        b = baton.empty_baton('question')
        baton.apply_set(b, test_baton._set_args(ask='Archive 1 branch?'))
        rows = [board._baton_row('question', b), {'slug': 'old', 'state': 'stale'}]
        attention = board.build_attention({}, {'live': [{'status': 'failed'}]}, rows, False)
        self.assertEqual([r['kind'] for r in attention], ['inbox', 'stale'])
        self.assertIn('close or resume?', attention[-1]['text'])
        many = attention * 10
        self.assertEqual(len(board._cap_attention(many, False)[0]), 10)


class Context(unittest.TestCase):
    def test_synthetic_usage_ignored_and_policy_model_precedence(self):
        records = [json.dumps({'type': 'assistant', 'timestamp': '2026-01-01T00:00:00Z',
                   'message': {'model': 'demo', 'usage': {'input_tokens': 80}}}),
                   json.dumps({'type': 'assistant', 'message': {'model': '<synthetic>', 'usage': {'input_tokens': 999}}})]
        ctx = context.estimate('claude', records, model='demo', policy={'context': {
            'window_tokens': {'claude': 1000, 'models': {'demo': 100}}, 'checkpoint_pct': 50}})
        self.assertEqual((ctx['tokens'], ctx['window'], ctx['flag']), (80, 100, 'checkpoint'))
        self.assertTrue(ctx['estimate'])
        self.assertEqual(ctx['window_source'], 'policy')
        self.assertIn('~80%', ctx['text'])
    def estimate(self, engine, records, windows=None):
        return context.estimate(engine, [json.dumps(x) for x in records], model='test-model',
                                policy={'context': {'windows': windows or {}}})

    def test_claude_latest_input_and_cache_only(self):
        records = [{'type': 'assistant', 'message': {'usage': {'input_tokens': 5}}},
                   {'type': 'assistant', 'message': {'usage': {'input_tokens': 10,
                    'cache_read_input_tokens': 50, 'cache_creation_input_tokens': 10, 'output_tokens': 999}}}]
        ctx = self.estimate('claude', records, {'test-model': 100})
        self.assertEqual((ctx['tokens'], ctx['pct'], ctx['flag']), (70, 70.0, 'checkpoint'))

    def test_codex_last_usage_not_cumulative_and_cached_not_added_twice(self):
        records = [{'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'total_token_usage': {'total_tokens': 50000}, 'last_token_usage': {
            'input_tokens': 80, 'output_tokens': 5, 'cached_input_tokens': 50}, 'model_context_window': 100}}}]
        ctx = self.estimate('codex', records)
        self.assertEqual((ctx['tokens'], ctx['pct'], ctx['flag']), (85, 85.0, 'rotate'))

    def test_unknown_window_stays_unknown_and_zero_is_real(self):
        ctx = self.estimate('claude', [{'type': 'assistant', 'message': {'usage': {'input_tokens': 0}}}])
        self.assertEqual(ctx['tokens'], 0)
        self.assertIsNone(ctx['pct'])
        self.assertIn('window unknown', context.label(ctx))
        self.assertEqual(context.label(self.estimate('codex', [])), 'ctx ?')

    def test_window_policy_override_and_bad_usage(self):
        row = {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
            'last_token_usage': {'total_tokens': 85}, 'model_context_window': 100}}}
        self.assertEqual(self.estimate('codex', [row], {'codex': 200})['pct'], 42.5)
        for value in (-1, True, '80', float('nan')):
            row['payload']['info']['last_token_usage']['total_tokens'] = value
            self.assertIsNone(self.estimate('codex', [row])['tokens'])

    def test_external_import_token_counts_do_not_replace_native_context(self):
        records = [{'type': 'event_msg', 'payload': {'type': 'token_count', 'turn_id': 't1', 'info': {
            'last_token_usage': {'total_tokens': 10}, 'model_context_window': 100}}},
            {'type': 'turn_context', 'payload': {'turn_id': 'external-import-turn-1'}},
            {'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                'last_token_usage': {'total_tokens': 95}, 'model_context_window': 100}}}]
        self.assertEqual(self.estimate('codex', records)['tokens'], 10)


class Supervisor(unittest.TestCase):
    def setUp(self):
        lanes._supervisor.cache_clear()

    def test_supervisor_success_is_memoized_and_optional_fields_preserved(self):
        row = {'sessionId': 'session-1', 'cwd': '/work/demo', 'status': 'busy', 'kind': 'interactive',
               'pid': 10, 'startedAt': 1, 'name': 'demo', 'waitingFor': 'one answer', 'extra': 'discard'}
        with mock.patch.object(common, 'run', return_value=(0, json.dumps([row]), '')) as run:
            data = lanes._supervisor('fake-cli', 1)
            self.assertEqual(lanes._supervisor('fake-cli', 1), data)
            self.assertEqual(run.call_count, 1)
        self.assertNotIn('extra', data[0])
        self.assertEqual(lanes.health(data[0]), ('needs-you', 'one answer'))
        self.assertEqual(lanes.health({'status': 'busy'}), ('working', None))
        self.assertEqual(lanes.health({'status': 'idle'}), ('idle', None))

    def test_supervisor_failures_fall_back(self):
        for result in ((1, '', ''), (0, '{', ''), (0, '{}', '')):
            lanes._supervisor.cache_clear()
            with mock.patch.object(common, 'run', return_value=result):
                self.assertIsNone(lanes._supervisor('fake-cli', 1))
        lanes._supervisor.cache_clear()
        with mock.patch.object(common, 'run', side_effect=TimeoutError):
            self.assertIsNone(lanes._supervisor('fake-cli', 1))
        self.assertIsNone(lanes._supervisor(None, 1))

    def test_notify_records_no_message_content_and_rejects_path_ids(self):
        with mock.patch.object(common, 'write_json') as write:
            hook.lane_state({'type': 'agent-turn-complete', 'thread-id': 'session-1', 'cwd': '/work/demo',
                             'last-assistant-message': 'must not be stored'}, 'codex')
            row = write.call_args.args[1]
            self.assertEqual(row['state'], 'idle')
            self.assertNotIn('must not be stored', json.dumps(row))
            write.reset_mock()
            hook.lane_state({'session_id': '../escape', 'hook_event_name': 'Stop'})
            write.assert_not_called()


class SessionHealth(test_sessions.Fakes):
    def test_observed_idle_filter_precedes_pagination_and_hidden_counts(self):
        self.codex_done(test_sessions.CODEX_A, age_s=300)
        self.codex_done(test_sessions.CODEX_B, age_s=600)
        for sid in (test_sessions.CODEX_A, test_sessions.CODEX_B):
            common.write_json(os.path.join(common.STATE, 'lanes', 'codex-' + sid + '.json'),
                              {'engine': 'codex', 'session_id': sid, 'state': 'idle',
                               'since': common.iso(common.now())})
        result = sessions.collect_sessions(states=['idle'], limit=1)
        self.assertEqual([r['id'] for r in result['rows']], [test_sessions.CODEX_A])
        self.assertEqual(result['rows'][0]['state'], 'idle')
        self.assertEqual((result['cut'], result['cut_waiting'], result['cut_exact']), (1, 0, True))
        result = sessions.collect_sessions(states=['waiting-on-user'], limit=1)
        self.assertEqual((result['rows'], result['cut'], result['cut_waiting']), ([], 0, 0))

    def test_supervisor_state_filter_and_hidden_waiting_count_use_observations(self):
        for sid, age in ((test_sessions.CLAUDE_A, 300), (test_sessions.CLAUDE_B, 600)):
            self.put_claude(sid, [self.c_user('inspect', sid=sid), self.c_asst('busy', sid=sid, tool=True)], age_s=age)
        observed = [{'sessionId': sid, 'kind': 'interactive', 'cwd': self.repo, 'status': 'idle'}
                    for sid in (test_sessions.CLAUDE_A, test_sessions.CLAUDE_B)]
        with mock.patch.object(lanes, 'supervisor_rows', return_value=observed):
            result = sessions.collect_sessions(states=['idle'], limit=1)
        self.assertEqual([r['id'] for r in result['rows']], [test_sessions.CLAUDE_A])
        self.assertEqual((result['cut'], result['cut_waiting']), (1, 0))
        for row in observed:
            row['waitingFor'] = 'Review these changes?'
        with mock.patch.object(lanes, 'supervisor_rows', return_value=observed):
            result = sessions.collect_sessions(states=['needs-you'], limit=1)
        self.assertEqual((len(result['rows']), result['cut'], result['cut_waiting']), (1, 1, 1))

    def test_count_budget_starts_after_observed_matching_rows_fill_limit(self):
        self.codex_done(test_sessions.CODEX_A, age_s=300)
        self.codex_done(test_sessions.CODEX_B, age_s=600)
        common.write_json(os.path.join(common.STATE, 'lanes', 'codex-' + test_sessions.CODEX_B + '.json'),
                          {'engine': 'codex', 'session_id': test_sessions.CODEX_B, 'state': 'idle',
                           'since': common.iso(common.now())})
        with mock.patch.object(sessions, 'CUT_COUNT_BUDGET_S', -1):
            result = sessions.collect_sessions(states=['idle'], limit=1)
        self.assertEqual([r['id'] for r in result['rows']], [test_sessions.CODEX_B])
        self.assertEqual((result['cut'], result['cut_waiting']), (0, 0))

    def test_context_and_creator_survive_session_and_board_rendering(self):
        user = json.loads(self.c_user('Inspect 1 parser', startedBy='lead-session'))
        assistant = json.loads(self.c_asst('Ready.'))
        assistant['message']['usage'] = {'input_tokens': 10, 'cache_read_input_tokens': 60}
        self.put_claude(test_sessions.CLAUDE_A, [json.dumps(user), json.dumps(assistant)])
        policy = common.load_policy()
        policy['context']['window_tokens'] = {'claude': 100}
        rows = sessions.collect_sessions(limit=0, policy=policy)['rows']
        self.assertEqual(rows[0]['context']['flag'], 'checkpoint')
        self.assertEqual(rows[0]['started_by'], 'lead-session')
        self.assertTrue(rows[0]['unmanaged'])
        policy['sessions']['lead_family'] = [test_sessions.CLAUDE_A]
        rows = sessions.collect_sessions(limit=0, policy=policy)['rows']
        self.assertFalse(rows[0]['unmanaged'])
        data = board.collect(policy=policy)
        for rendered in (sessions.format_md(rows), board.render_md(data), board.render_text(data)):
            self.assertIn('checkpoint', rendered)
            self.assertIn('lead-session', rendered)

    def test_claude_child_is_visible_without_inheriting_parent_custody(self):
        parent = self.put_claude(test_sessions.CLAUDE_A, [self.c_user('parent'), self.c_asst('finished')])
        child = Path(parent).parent / test_sessions.CLAUDE_A / 'subagents' / 'agent-child.jsonl'
        child.parent.mkdir(parents=True)
        records = [json.loads(self.c_user('child task')), json.loads(self.c_asst('child result'))]
        for record in records:
            record['isSidechain'] = True
        child.write_text('\n'.join(json.dumps(x) for x in records) + '\n', encoding='utf-8')
        policy = common.load_policy()
        policy['sessions']['lead_family'] = [test_sessions.CLAUDE_A]
        rows = sessions.collect_sessions(limit=0, policy=policy)['rows']
        children = [r for r in rows if r['id'] == 'agent-child']
        self.assertEqual(len(children), 1)
        self.assertEqual(children[0]['started_by'], test_sessions.CLAUDE_A)
        self.assertTrue(children[0]['unmanaged'])
        self.assertEqual(children[0]['last'], 'child result')


class WorkerControls(unittest.TestCase):
    def test_worker_cannot_answer_or_pop_but_can_read(self):
        for prefix in ('twinlane', 'python -m twinlane', 'python twinlane/cli.py'):
            for suffix in ('inbox answer task:H1 --grant yes', 'wip next'):
                self.assertIsNotNone(guard.evaluate('Bash', {'command': prefix + ' ' + suffix}, 'code', '.', []))
            for suffix in ('inbox --json', 'wip --json'):
                self.assertIsNone(guard.evaluate('Bash', {'command': prefix + ' ' + suffix}, 'code', '.', []))
        with mock.patch.object(projects, 'in_worker', return_value=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(wip.cli(['next']), 3)
            self.assertEqual(inbox.cli(['answer', 'task:H1', '--grant', 'yes']), 3)


class Checkpoints(test_baton.BatonTestBase):
    def test_checkpoint_preserves_progress_and_no_guess_by_cwd(self):
        self.create('one', 'session-1')
        b = baton.load('one')
        b['progress_at'] = common.iso(common.now() - dt.timedelta(days=2))
        common.write_json(baton.baton_path('one'), b)
        before = Path(baton.baton_path('one')).read_bytes()
        hook.precompact({'session_id': 'unbound-session', 'cwd': self.repo})
        self.assertEqual(before, Path(baton.baton_path('one')).read_bytes())
        hook.precompact({'session_id': 'session-1', 'trigger': 'manual'})
        saved = baton.load('one')
        self.assertEqual(saved['progress_at'], b['progress_at'])
        self.assertEqual(saved['last_precompact']['session_id'], 'session-1')

    def create(self, slug, sid):
        b = baton.empty_baton(slug)
        b['repo']['worktree'] = self.repo
        b['bound_session'] = sid
        baton.save(b)

    def test_precompact_exact_binding_snap_no_refresh_and_worker_skips(self):
        self.create('one', 'session-1')
        self.create('two', 'session-2')
        payload = {'hook_event_name': 'PreCompact', 'session_id': 'session-1', 'cwd': self.repo}
        with mock.patch.object(baton, 'snap') as snap, mock.patch.object(hook, 'spawn_refresh') as refresh:
            self.assertEqual(hook.run(stdin=io.StringIO(json.dumps(payload)),
                                      env={'TWINLANE_ENGINE_JOB': 'fixture'}), 0)
            snap.assert_not_called()
            self.assertEqual(hook.run(stdin=io.StringIO(json.dumps(payload)), env={}), 0)
            self.assertEqual(snap.call_args.args, ('one', None, 'checkpoint'))
            self.assertFalse(snap.call_args.kwargs['create'])
            refresh.assert_not_called()

    def test_missing_wrong_bindings_never_snap_and_shared_binding_snaps_both(self):
        self.create('one', 'session-1')
        with mock.patch.object(baton, 'snap') as snap:
            hook.checkpoint({'cwd': self.repo})
            hook.checkpoint({'session_id': 'wrong', 'cwd': self.repo})
            snap.assert_not_called()
            self.create('duplicate', 'session-1')
            hook.checkpoint({'session_id': 'session-1', 'cwd': self.tmp})
            self.assertEqual({c.args[0] for c in snap.call_args_list}, {'one', 'duplicate'})

    def test_install_precompact_diff_is_dry_and_preserves_other_events(self):
        path = Path(self.tmp) / 'settings.json'
        original = {'unrelated': 'kept', 'hooks': {'Stop': [{'hooks': [{'type': 'command', 'command': 'other'}]}]}}
        common.write_json(str(path), original)
        before = path.read_bytes()
        with contextlib.redirect_stdout(io.StringIO()) as out:
            rc = install.main(['--precompact', '--settings', str(path), '--json'])
        self.assertEqual(rc, 0)
        self.assertEqual(path.read_bytes(), before)
        action = json.loads(out.getvalue())['actions'][0]
        self.assertIn('PreCompact', action['diff'])
        self.assertFalse(action['applied'])
        settings = dict(original)
        settings['hooks'] = dict(original['hooks'])
        self.assertTrue(install.add_hook(settings, event='PreCompact'))
        self.assertFalse(install.add_hook(settings, event='PreCompact'))
        self.assertEqual(settings['hooks']['Stop'], original['hooks']['Stop'])

    def test_precompact_persists_snapshot_without_touching_peer_baton(self):
        self.create('one', 'session-1')
        self.create('two', 'session-2')
        before = Path(baton.baton_path('two')).read_bytes()
        hook.checkpoint({'session_id': 'session-1', 'cwd': self.repo})
        self.assertEqual(baton.load('one')['repo']['head'], common.git(self.repo, 'rev-parse', 'HEAD')[1].strip())
        self.assertEqual(Path(baton.baton_path('two')).read_bytes(), before)

    def test_precompact_snap_never_waits_longer_on_the_wip_lock_than_its_own_lock(self):
        self.create('one', 'session-1')
        real = wip.lock
        with mock.patch.object(wip, 'lock', side_effect=lambda *a: real(*a)) as lock:
            hook.checkpoint({'session_id': 'session-1', 'cwd': self.repo})
        self.assertEqual(lock.call_args.args, (hook.PRECOMPACT_LOCK_S,))


class GarbageCollection(test_worktrees.WtBase):
    def test_dependencies_preflight_uses_worktree_root_from_subdirectory(self):
        wt = self.ws.worktree(self.repo, 'deps-root')
        subdir = os.path.join(wt, 'src')
        os.makedirs(subdir)
        policy = self.policy(deps_dirs=['vendor'])
        _, cfg = projects.resolve(subdir, policy)
        self.assertIn('vendor: missing', jobs.writer_readiness(subdir, policy))
        self.ws.write(wt, 'vendor/loader.txt', 'fixture')
        self.assertEqual(worktrees.preflight(subdir, cfg), [])
        self.assertIsNone(jobs.writer_readiness(subdir, policy))
        os.unlink(os.path.join(wt, 'vendor', 'loader.txt'))
        os.rmdir(os.path.join(wt, 'vendor'))
        self.assertIn('vendor: missing', jobs.writer_readiness(subdir, policy))

    def test_relative_link_roots_resolve_against_main_checkout(self):
        wt = self.ws.worktree(self.repo, 'deps-link')
        subdir = os.path.join(wt, 'src')
        os.makedirs(subdir)
        target = os.path.join(self.repo, 'shared-deps')
        self.ws.write(self.repo, 'shared-deps/loader.txt', 'fixture')
        link = os.path.join(wt, 'vendor')
        try:
            test_worktrees.gf.make_link(link, target)
        except OSError as e:
            self.skipTest(f'directory links unavailable: {e}')
        self.addCleanup(test_worktrees.gf.remove_link, link)
        policy = self.policy(deps_dirs=['vendor'], link_roots=['shared-deps'])
        _, cfg = projects.resolve(subdir, policy)
        os.chdir(self.ws.root)
        self.assertEqual(worktrees.preflight(subdir, cfg), [])
        self.assertIsNone(jobs.writer_readiness(subdir, policy))
        self.assertEqual(len(worktrees.inbound_index(self.repo, ['shared-deps'])['problems']), 0)
        cfg['wt']['link_roots'] = ['elsewhere']
        self.assertIn('vendor: link resolves outside allowed roots', worktrees.preflight(subdir, cfg))

    def test_fresh_merged_worktree_keeps_one_hour_floor_and_only_limits_scope(self):
        fresh = self.merged('fresh', age=10)
        old = self.merged('old', age=7200)
        result = worktrees.prune(self.repo, policy=self.policy(), merged_only=True, only=[fresh])
        self.assertEqual(len(result['rows']), 1)
        self.assertEqual(result['rows'][0]['action'], 'skip')
        self.assertIn('minimum 1h', '; '.join(result['rows'][0]['blockers']))
        self.assertTrue(os.path.isdir(old))

    def test_review_card_is_read_only_and_archive_failure_preserves_result(self):
        wt = self.merged('review-card')
        b = baton.empty_baton('review-card')
        b['repo'].update(worktree=wt, branch='review-card')
        baton.apply_set(b, test_baton._set_args(ask='Archive 1 worktree?'))
        baton.save(b)
        before = Path(baton.baton_path('review-card')).read_bytes()
        with mock.patch.object(common, 'load_policy', return_value=self.policy()):
            card = land.review_card(slug='review-card', repo=self.repo)
        self.assertIn('open inbox item', card['verdict'])
        self.assertLessEqual(len(land._human_card(card).splitlines()), 30)
        self.assertEqual(before, Path(baton.baton_path('review-card')).read_bytes())
        from types import SimpleNamespace
        event = {'event': 'landed', 'worktree': wt, 'branch': 'review-card'}
        runner = SimpleNamespace(main_root=self.repo, cfg={'land': {'archive_worktree': 'apply'}})
        with mock.patch.object(worktrees, 'prune', side_effect=RuntimeError('fixture archive failure')):
            land._after_land(event, runner)
        self.assertEqual(event['archive_error'], 'fixture archive failure')
        self.assertEqual(event['event'], 'landed')
        self.assertEqual(baton.load('review-card')['state'], 'landed')

    def test_gc_cli_defaults_to_dry_run_and_worker_cannot_apply(self):
        wt = self.merged('gc-preview', age=2 * 86400)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(worktrees.cli(['gc', '--repo', self.repo, '--json']), 0)
        result = json.loads(out.getvalue())
        self.assertTrue(result['dry_run'])
        self.assertEqual(self.rows(result)['gc-preview']['action'], 'would remove')
        self.assertTrue(os.path.isdir(wt))
        with mock.patch.object(projects, 'in_worker', return_value=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(worktrees.cli(['gc', '--repo', self.repo, '--apply']), 3)
        self.assertTrue(os.path.isdir(wt))

    def test_missing_or_empty_dependencies_refuse_before_runtime_meter(self):
        wt = self.ws.worktree(self.repo, 'dependencies')
        policy = self.policy(deps_dirs=['vendor'])
        spec = {'cwd': wt, 'profile': 'code', 'kind': 'code'}
        with mock.patch.object(jobs, '_meter') as meter:
            why, _ = jobs.preflight(spec, {'engine': 'claude'}, policy, fresh=False)
            self.assertIn('vendor: missing', why)
            meter.assert_not_called()
        os.makedirs(os.path.join(wt, 'vendor'))
        self.assertIn('vendor: empty', jobs.writer_readiness(wt, policy))
        self.ws.write(wt, 'vendor/loader.txt', 'fixture')
        self.assertIsNone(jobs.writer_readiness(wt, policy))

    def test_gc_reuses_prune_and_keeps_unique_dirty_and_live_worktrees(self):
        merged = self.merged('merged')
        dirty = self.merged('dirty')
        self.ws.write(dirty, 'untracked.txt', 'keep')
        unique = self.ws.worktree(self.repo, 'unique')
        self.ws.write(unique, 'unique.txt', 'keep')
        self.ws.commit(unique, 'unique.txt', 'keep')
        live = self.merged('live')
        self.write_job(test_baton.jid(1), live)
        result = worktrees.prune(self.repo, policy=self.policy(), merged_only=True, include_empty=True)
        rows = self.rows(result)
        self.assertEqual(rows['merged']['action'], 'would remove')
        self.assertEqual(rows['merged']['unique'], 0)
        for branch in ('dirty', 'unique', 'live'):
            self.assertEqual(rows[branch]['action'], 'skip')
        self.assertTrue(os.path.isdir(merged))

    def test_gc_equivalent_cherry_pick_is_zero_unique_but_keeps_branch_if_git_refuses_delete(self):
        wt = self.ws.worktree(self.repo, 'patch')
        self.ws.write(wt, 'patch.txt', 'patch')
        self.ws.commit(wt, 'patch.txt', 'patch')
        self.ws.write(self.repo, 'main.txt', 'main')
        self.ws.commit(self.repo, 'main.txt', 'main')
        test_worktrees.gf.git(self.repo, 'cherry-pick', 'patch')
        self.age(wt, 7200)
        result = worktrees.prune(self.repo, policy=self.policy(), merged_only=True, include_empty=True)
        self.assertEqual(self.rows(result)['patch']['action'], 'would remove')
        self.assertTrue(os.path.isdir(wt))

    def test_unique_far_behind_is_rebase_or_park(self):
        wt = self.ws.worktree(self.repo, 'unique')
        self.ws.write(wt, 'unique.txt', 'unique')
        self.ws.commit(wt, 'unique.txt', 'unique')
        for n in range(2):
            self.ws.write(self.repo, 'main.txt', str(n))
            self.ws.commit(self.repo, 'main.txt', str(n))
        result = worktrees.prune(self.repo, policy=self.policy(behind_max=1), merged_only=True)
        row = self.rows(result)['unique']
        self.assertTrue(row['rebase_or_park'])
        self.assertIn('rebase or park', worktrees.render_prune(result))

    def test_closed_baton_no_longer_pins_worktree(self):
        wt = self.merged('closed')
        b = baton.empty_baton('closed')
        b['state'] = 'closed'
        b['repo']['worktree'] = wt
        baton.save(b)
        result = worktrees.prune(self.repo, policy=self.policy(), merged_only=True)
        self.assertEqual(self.rows(result)['closed']['action'], 'would remove')

    def test_apply_removes_only_eligible_worktree_and_rechecks(self):
        removable = self.merged('eligible')
        protected = self.merged('dirty')
        self.ws.write(protected, 'keep.txt', 'keep')
        result = worktrees.prune(self.repo, yes=True, policy=self.policy(), merged_only=True)
        self.assertEqual(result['removed'], 1)
        self.assertFalse(os.path.exists(removable))
        self.assertTrue(os.path.isdir(protected))
        self.assertTrue(os.path.isdir(self.repo))

    def test_custom_removal_success_with_leftover_folder_restores_links(self):
        wt = self.merged('leftover')
        target = self.ws.path('dependency')
        os.makedirs(target)
        self.ws.write(target, 'marker.txt', 'keep')
        link = os.path.join(wt, 'vendor')
        try:
            test_worktrees.gf.make_link(link, target)
        except OSError:
            self.skipTest('directory link fixture unavailable')
        links = worktrees.links_inside(wt)
        for lk in links:
            lk['protect'] = True
        with mock.patch.object(common, 'run', return_value=(0, '', '')):
            res = worktrees._remove(self.repo, {'remove_command': ['echo', 'fixture']},
                                    {'path': wt, 'branch': 'leftover'}, links, 'main')
        self.assertFalse(res['removed'])
        self.assertIn('links put back: 1/1', res['error'])
        self.assertTrue(os.path.isdir(link))
        self.assertTrue(os.path.isfile(os.path.join(target, 'marker.txt')))


if __name__ == '__main__':
    unittest.main()
