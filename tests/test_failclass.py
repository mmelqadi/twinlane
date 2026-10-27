"""Synthetic failure-class, git preflight, readiness and retry regression checks."""
import copy
import io
import json
import os
import stat
import subprocess
import unittest
from unittest import mock
import test_support  # noqa: F401
import test_jobs as tj
from twinlane import common, failclass, jobs, retry, watchdog


class Classifier(unittest.TestCase):
    def test_table(self):
        rows = [
            ('transient_start', {'error': '--bg refused: the WMI start failed (WMI unavailable before Create: x)'}),
            ('transient_start', {'error': 'codex not usable at start: forced codex unknown is not usable; no swap'}),
            ('transient_start', {'error': 'meter/route unavailable at start: OSError'}),
            ('task', {'status': 'failed', 'ready': False, 'engine_started': True, 'exit_code': 1}),
            ('task', {'status': 'failed', 'ready': False, 'engine_started': True,
                      'error': 'invalid CLI argument --unknown; exited rc 2'}),
            ('task', {'status': 'failed', 'ready': False, 'engine_started': True,
                      'error': 'invalid configuration', 'exit_code': 1}),
            ('transient_engine', {'error': 'Selected model is at capacity. Please try a different model.'}),
            ('transient_engine', {'error': "the Codex sandbox could not set up ('setup refresh had errors')"}),
            ('preflight', {'error': '--cd X is not a git work tree; codex refuses it.'}),
            ('lock', {'error': 'slots.lock busy'}), ('lock', {'error': 'writers.lock busy'}),
            ('custody', {'error': 'writer directory is owned by other (live or unknown)'}),
            ('stall', {'status': 'stalled'}), ('timeout', {'status': 'timeout'}),
            ('limit', {'status': 'limit_hit'}), ('policy', {'status': 'refused', 'error': 'credits forbidden'}),
            ('policy', {'status': 'refused', 'error': 'sandbox could not set up'}),
            ('policy', {'error': 'refused by policy: model is at capacity'}),
            ('policy', {'error': 'policy refused: meter/route unavailable at start'}),
            ('policy', {'error': 'broke its guard: WMI start failed'}),
            ('transient_start', {'status': 'refused', 'error': 'codex not usable at start: unknown'}),
            ('task', {'status': 'failed', 'exit_code': 1, 'ready': True}),
            ('task', {'status': 'failed', 'error': 'ordinary error'})]
        for expected, attempt in rows:
            with self.subTest(attempt=attempt):
                before = dict(attempt)
                self.assertEqual(failclass.classify(attempt), expected)
                self.assertEqual(attempt, before)


class PreflightReady(tj.JobsBase):
    def setUp(self):
        super().setUp()
        parent = mock.patch.object(jobs, '_coordination_parent', return_value=None)
        parent.start(); self.addCleanup(parent.stop)

    def test_non_git_refused_before_job_and_slot(self):
        plain = os.path.join(self.tmp, 'plain'); os.makedirs(plain)
        common.write_text(os.path.join(plain, 'seed'), 'plain')
        with mock.patch.object(jobs, 'create_job') as create, mock.patch.object(jobs, 'acquire_slot') as slot:
            rc, out, err = self.cli('run', '--brief', self.brief, '--cd', plain, '--engine', 'codex', '--json')
        self.assertEqual(rc, common.EXIT_REFUSED, err)
        self.assertIn('git init', out)
        self.assertEqual(json.loads(out)['failure_class'], 'preflight')
        create.assert_not_called(); slot.assert_not_called()
        self.assertEqual(jobs._job_ids(), [])

    def test_git_allowed(self):
        self.assertIsNone(jobs.git_cwd_preflight('codex', self.work))

    def test_git_preflight_starts_no_process(self):
        with mock.patch.object(jobs.subprocess, 'run', side_effect=AssertionError('process')) as run, \
                mock.patch.object(jobs.subprocess, 'Popen', side_effect=AssertionError('process')) as popen:
            self.assertIsNone(jobs.git_cwd_preflight('codex', self.work))
            plain = os.path.join(self.tmp, 'plain-np'); os.makedirs(plain)
            with self.assertRaises(jobs.Refusal):
                jobs.git_cwd_preflight('codex', plain)
        run.assert_not_called(); popen.assert_not_called()

    def test_linked_worktree_gitdir_file_is_allowed(self):
        linked = os.path.join(self.tmp, 'linked'); real = os.path.join(self.tmp, 'gitdirs', 'wt'); os.makedirs(linked); os.makedirs(real)
        common.write_text(os.path.join(linked, '.git'), 'gitdir: ' + real + '\n')
        self.assertIsNone(jobs.git_cwd_preflight('codex', linked))

    def test_git_unknown_with_note(self):
        broken = os.path.join(self.tmp, 'broken'); os.makedirs(broken)
        common.write_text(os.path.join(broken, '.git'), 'gitdir: ' + os.path.join(self.tmp, 'missing') + '\n')
        self.assertIn('unknown', jobs.git_cwd_preflight('codex', broken))
        self.assertIn('unknown', jobs.git_cwd_preflight('codex', os.path.join(self.tmp, 'absent')))
        # Fail at the stat itself (os.path.isdir would swallow this and answer False): a .git that cannot be
        # read is Unknown, never a definite non-repository refusal, even where no readable .git exists anywhere.
        plain = os.path.join(self.tmp, 'plain-denied'); os.makedirs(plain)
        real_stat = os.stat
        def denied(path, *a, **kw):
            if os.path.basename(str(path)) == '.git':
                raise PermissionError('denied')
            return real_stat(path, *a, **kw)
        with mock.patch.object(jobs.os, 'stat', side_effect=denied):
            self.assertIn('unknown', jobs.git_cwd_preflight('codex', self.work))
            self.assertIn('unknown', jobs.git_cwd_preflight('codex', plain))

    def test_nonregular_git_marker_is_unknown_and_never_opened(self):
        fifo = os.stat_result((stat.S_IFIFO | 0o644, 0, 0, 1, 0, 0, 0, 0, 0, 0))
        real_stat, real_open = os.stat, open
        def fake_stat(path, *a, **kw):
            return fifo if os.path.basename(str(path)) == '.git' else real_stat(path, *a, **kw)
        def no_marker_open(path, *a, **kw):
            if os.path.basename(str(path)) == '.git':
                raise AssertionError('a non-regular .git was opened')
            return real_open(path, *a, **kw)
        with mock.patch.object(jobs.os, 'stat', side_effect=fake_stat),                 mock.patch('builtins.open', side_effect=no_marker_open):
            self.assertIn('not a regular file', jobs.git_cwd_preflight('codex', self.work))

    def test_watchdog_never_extends_an_absolute_deadline(self):
        clock = [100.0]
        with mock.patch.object(watchdog.time, 'monotonic', side_effect=lambda: clock[0]):
            proc = mock.Mock(); proc.wait.return_value = 0
            watchdog.wait(proc, 7, None)
            proc.wait.assert_called_once_with(timeout=7)  # no deadline: exactly the relative timeout
            proc = mock.Mock(); proc.wait.return_value = 0
            watchdog.wait(proc, 7, None, deadline=102.5)
            proc.wait.assert_called_once_with(timeout=2.5)
            proc = mock.Mock(); proc.wait.side_effect = subprocess.TimeoutExpired('engine', 0)
            with self.assertRaises(subprocess.TimeoutExpired):  # spent before the call: no new window
                watchdog.wait(proc, 7, None, deadline=99.0)
            proc.wait.assert_called_once_with(timeout=0.0)
            proc = mock.Mock()
            with self.assertRaises(subprocess.TimeoutExpired):  # the watched path never waits at all
                watchdog.wait(proc, 7, watchdog.Stall([], 60), progress=lambda: None, deadline=99.0)
            proc.wait.assert_not_called()

    def test_queue_and_launch_budgets_are_the_job_deadline(self):
        with mock.patch.object(jobs.time, 'monotonic', return_value=100.0),                 mock.patch.object(jobs, 'slot_attempt', side_effect=AssertionError('admission ran')):
            with self.assertRaises(jobs.JobDeadline):  # exhausted while queued: no admission attempt at all
                jobs.acquire_slot('20261026-100009-codex-aaaa', 'codex', 1, copy.deepcopy(tj.POLICY), deadline=100.0)
        with mock.patch.object(jobs.time, 'monotonic', return_value=100.0):
            self.assertEqual(jobs._launch_budget({}, 7), 7)
            self.assertEqual(jobs._launch_budget({'deadline_monotonic': 103.0, 'launch_min_s': 0}, 7), 3.0)
            with self.assertRaises(jobs.JobDeadline):  # preflight spent it: no launch
                jobs._launch_budget({'deadline_monotonic': 100.0, 'launch_min_s': 0}, 7)
            with self.assertRaises(jobs.JobDeadline):  # a retry needs its floor
                jobs._launch_budget({'deadline_monotonic': 100.5}, 7)

    def test_queue_contention_that_spends_the_budget_stops_admission(self):
        jid = '20261026-100010-codex-aaaa'; os.makedirs(jobs.job_dir(jid))
        jobs._write_status(jid, 'queued', engine='codex')
        clock, calls = [100.0], []
        def full(*a, **k):
            calls.append(clock[0]); clock[0] += 3  # each admission attempt costs 3 s of the job's budget
            return False, 'engine slots full: 1/1'
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]),                 mock.patch.object(jobs.time, 'sleep', side_effect=lambda s: clock.__setitem__(0, clock[0] + s)),                 mock.patch.object(jobs, 'slot_attempt', side_effect=full):
            with self.assertRaises(jobs.JobDeadline):
                jobs.acquire_slot(jid, 'codex', 1, copy.deepcopy(tj.POLICY), deadline=105.0)
        self.assertEqual(len(calls), 2)  # 100 s, then ~103 s after the jittered poll: none starts after 105 s
        self.assertTrue(all(t < 105.0 for t in calls), calls)

    def test_lock_wait_classification_and_internal_wait_report(self):
        jid = '20261026-100011-codex-aaaa'; os.makedirs(jobs.job_dir(jid))
        jobs._write_status(jid, 'queued', engine='codex')
        policy = copy.deepcopy(tj.POLICY)
        clock = [100.0]
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]):
            # Unrelated TimeoutErrors are not lock waits: they propagate unchanged.
            with mock.patch.object(jobs, 'slot_attempt', side_effect=TimeoutError('something else')):
                with self.assertRaisesRegex(TimeoutError, 'something else'):
                    jobs.acquire_slot(jid, 'codex', 1, policy, deadline=200.0)
            # A lock-wait bound inside the job budget stays LockWaitTimeout ...
            with mock.patch.object(jobs, 'slot_attempt', side_effect=common.LockWaitTimeout('bound')):
                with self.assertRaises(common.LockWaitTimeout) as raised:
                    jobs.acquire_slot(jid, 'codex', 1, policy, deadline=200.0)
                self.assertNotIsInstance(raised.exception, jobs.JobDeadline)
            # ... and becomes the job's timeout once the job deadline has also passed.
            def late(*a, **k):
                clock[0] = 300.0
                raise common.LockWaitTimeout('bound')
            with mock.patch.object(jobs, 'slot_attempt', side_effect=late):
                with self.assertRaises(jobs.JobDeadline):
                    jobs.acquire_slot(jid, 'codex', 1, policy, deadline=200.0)
            # Admitted on the first attempt after an internal lock wait: reported as waited, with a reason.
            rec = {}
            with mock.patch.object(jobs, 'slot_attempt', return_value=(True, 'waited for slot lock')):
                self.assertTrue(jobs.acquire_slot(jid, 'codex', 1, policy, record=rec, deadline=400.0))
            self.assertEqual(rec['slot_wait_reasons'], ['waiting for slot lock'])

    def test_runner_hands_its_absolute_deadline_to_the_watchdog(self):
        jid = '20261026-100008-claude-aaaa'; os.makedirs(jobs.job_dir(jid))
        files = jobs._attempt_files(jid, 1)
        deadline = jobs.time.monotonic() + 50
        params = {'profile': 'code', 'timeout_s': 10, 'deadline_monotonic': deadline, 'launch_min_s': 0}
        proc = mock.Mock(pid=123, stdout=io.BytesIO(b''))
        with mock.patch.object(jobs.subprocess, 'Popen', return_value=proc),                 mock.patch.object(common, 'contain_engine'),                 mock.patch.object(common, 'process_created', return_value=456),                 mock.patch.object(jobs, '_merge_status'),                 mock.patch.object(jobs.watchdog, 'wait', return_value=1) as wait,                 mock.patch.object(jobs, '_note_unresolved_tree', return_value=True),                 mock.patch.object(jobs, '_close_attempt_job', return_value=(False, None)),                 mock.patch.object(jobs, '_clear_child_starting'):
            jobs._run_claude(jid, ['fixture'], {'cwd': self.work}, params, self.brief, files, copy.deepcopy(tj.POLICY))
        self.assertEqual(wait.call_args.kwargs['deadline'], deadline)

    def test_claude_ignores_git(self):
        with mock.patch.object(jobs.subprocess, 'run') as run:
            self.assertIsNone(jobs.git_cwd_preflight('claude', self.tmp)); run.assert_not_called()

    def test_ready_file_phase_and_first_evidence(self):
        for engine in ('codex', 'claude'):
            with self.subTest(engine=engine):
                jid = '20261026-100000-' + engine + '-aaaa'; os.makedirs(jobs.job_dir(jid))
                events = jobs._p(jid, 'events.jsonl')
                jobs._write_status(jid, 'running', engine=engine)
                self.assertEqual(common.read_json(jobs._p(jid, 'status.json'))['phase'], 'starting')
                proc = mock.Mock(pid=123)
                def finish(**kwargs):
                    event = {'type': 'thread.started'} if engine == 'codex' else {'type': 'system', 'subtype': 'init'}
                    common.write_text(events, json.dumps(event) + '\n')
                    return 0
                proc.wait.side_effect = finish
                ready = jobs.EngineReady(jid, engine, [events], proc, 456)
                watchdog.wait(proc, 10, None, progress=ready)
                self.assertTrue(ready.ready)
                evidence = common.read_json(jobs._p(jid, 'engine-ready.json'))
                self.assertEqual((evidence['engine'], evidence['pid'], evidence['pid_created']), (engine, 123, 456))
                self.assertEqual(common.read_json(jobs._p(jid, 'status.json'))['phase'], 'working')
                common.write_text(events, json.dumps({'type': 'turn.started'}) + '\n'); ready()
                self.assertEqual(common.read_json(jobs._p(jid, 'engine-ready.json')), evidence)

    def test_exit_before_ready(self):
        proc = mock.Mock(pid=123); proc.wait.return_value = 1
        ready = jobs.EngineReady('unused', 'codex', [], proc, 456)
        watchdog.wait(proc, 10, None, progress=ready)
        self.assertFalse(ready.ready)
        self.assertEqual(failclass.classify({'status': 'failed', 'ready': ready.ready,
                                           'engine_started': True, 'exit_code': 1}), 'task')

    def test_error_only_startup_is_not_ready(self):
        for engine in ('codex', 'claude'):
            with self.subTest(engine=engine):
                jid = '20261026-100001-' + engine + '-aaaa'; os.makedirs(jobs.job_dir(jid))
                events = jobs._p(jid, 'events.jsonl')
                jobs._write_status(jid, 'running', engine=engine)
                error = {'type': 'error', 'message': 'sandbox could not set up'}
                common.write_text(events, json.dumps(error) + '\n')
                ready = jobs.EngineReady(jid, engine, [events], mock.Mock(pid=123), 456)
                ready()
                self.assertFalse(ready.ready)
                self.assertFalse(os.path.exists(jobs._p(jid, 'engine-ready.json')))
                self.assertEqual(common.read_json(jobs._p(jid, 'status.json'))['phase'], 'starting')
                self.assertTrue(jobs._work_free({'write': True}, {
                    'ready': ready.ready, 'engine_started': True, 'events_path': events,
                    'event_capture_started': True, 'event_capture_complete': True}))

    def test_only_engine_specific_success_events_establish_readiness(self):
        for engine, rejected, accepted in (
                ('codex', [{'type': 'system', 'subtype': 'init'}, {'type': 'turn.failed'},
                           {'type': 'arbitrary'}, {'type': 'item.completed', 'item': {'type': 'reasoning'}}],
                 [{'type': 'thread.started'}, {'type': 'turn.started'},
                  {'type': 'item.completed', 'item': {'type': 'agent_message'}}]),
                ('claude', [{'type': 'thread.started'}, {'type': 'system'},
                           {'type': 'system', 'subtype': 'error'}, {'type': 'result'}],
                 [{'type': 'system', 'subtype': 'init'}, {'type': 'assistant'}])):
            ready = jobs.EngineReady('unused', engine, [], mock.Mock(), 456)
            for event in rejected:
                self.assertFalse(ready._is_ready(event), (engine, event))
            for event in accepted:
                self.assertTrue(ready._is_ready(event), (engine, event))


class CaptureEvidence(unittest.TestCase):
    def test_pump_records_clean_eof(self):
        capture, errors = {}, []
        proc = mock.Mock(stdout=io.BytesIO(b'{"type":"error","message":"sandbox could not set up"}\n'))
        sink = io.BytesIO()
        jobs._pump(proc, sink, jobs.ClaudeStream(), errors, capture)
        self.assertEqual(capture, {'started': True, 'drained': True, 'clean': True})
        self.assertEqual(errors, [])
        self.assertTrue(sink.getvalue().endswith(b'\n'))

    def test_pump_read_failure_never_proves_eof(self):
        capture, errors = {}, []
        proc = mock.Mock(); proc.stdout.readline.side_effect = OSError('read failed')
        jobs._pump(proc, io.BytesIO(), jobs.ClaudeStream(), errors, capture)
        self.assertTrue(capture['started'])
        self.assertFalse(capture.get('drained'))
        self.assertTrue(errors)

    def test_pump_sink_failure_drains_without_claiming_clean_capture(self):
        capture, errors = {}, []
        proc = mock.Mock(stdout=io.BytesIO(b'{"type":"error"}\n'))
        sink = mock.Mock(); sink.write.side_effect = OSError('write failed')
        jobs._pump(proc, sink, jobs.ClaudeStream(), errors, capture)
        self.assertEqual(capture, {'started': True, 'drained': True, 'clean': False})
        self.assertTrue(errors)


class Retry(tj.JobsBase):
    def setUp(self):
        super().setUp()
        parent = mock.patch.object(jobs, '_coordination_parent', return_value=None)
        parent.start(); self.addCleanup(parent.stop)
        self.policy = copy.deepcopy(tj.POLICY)
        self.policy['retry'] = {'max': 2, 'backoff_s': [0, 0]}
        self.policy['watchdog'] = {'enabled': False}
        self.write_config(self.policy)

    def failure(self, **extra):
        return dict({'status': 'failed', 'engine': 'codex', 'ready': True, 'engine_started': True,
                     'error': 'Selected model is at capacity. Please try a different model.', 'exit_code': 1}, **extra)

    def run_attempts(self, answers, *, unresolved=False, stopped=False, state=common.DEAD, failover=False,
                     timeout=None, on_attempt=None):
        args = jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work, '--engine', 'codex'])
        plan = jobs.plan_run(args, self.policy); plan['failover'] = failover
        if timeout is not None:
            plan['primary']['timeout_s'] = timeout
        jid = jobs.create_job(plan); calls = []
        def attempt(job_id, n, params, *rest):
            calls.append((n, dict(params)))
            if on_attempt:
                on_attempt(n)
            answer = dict(answers[min(n - 1, len(answers) - 1)], n=n)
            if stopped: common.write_text(jobs._p(jid, jobs.STOP_REQUESTED), 'stop')
            return answer
        with mock.patch.object(jobs, 'run_attempt', side_effect=attempt), \
                mock.patch.object(jobs, '_tree_unresolved', return_value=unresolved), \
                mock.patch.object(jobs, '_engine_state', return_value=state), \
                mock.patch.object(jobs, '_job_usage', return_value=(None, None)):
            return jobs.run_job(jid), calls

    def test_capacity_same_engine_and_recorded_retry(self):
        r, calls = self.run_attempts([self.failure(), {'status': 'done', 'engine': 'codex', 'ready': True}])
        self.assertEqual(r['status'], 'done')
        self.assertEqual([a['n'] for a in r['attempts']], [1, 2])
        self.assertEqual(r['attempts'][1]['retry_of'], 1)
        self.assertEqual(r['attempts'][1]['retry_failure_class'], 'transient_engine')
        self.assertEqual(r['failure_class'], 'done')
        for key in ('engine', 'model', 'tier', 'effort', 'profile'):
            self.assertEqual(calls[0][1][key], calls[1][1][key])

    def test_no_retry_unresolved_tree(self):
        r, calls = self.run_attempts([self.failure()], unresolved=True)
        self.assertEqual(len(calls), 1); self.assertIn('not proven DEAD', ' '.join(r['notes']))

    def test_no_retry_unknown_engine(self):
        _, calls = self.run_attempts([self.failure()], state=common.UNKNOWN)
        self.assertEqual(len(calls), 1)

    def test_no_retry_stopped(self):
        r, calls = self.run_attempts([self.failure()], stopped=True)
        self.assertEqual((len(calls), r['status']), (1, 'stopped'))

    def test_no_retry_task(self):
        r, calls = self.run_attempts([{'status': 'failed', 'engine': 'codex', 'ready': True, 'exit_code': 1}])
        self.assertEqual(len(calls), 1); self.assertEqual(r['failure_class'], 'task')

    def test_deterministic_startup_and_policy_failures_are_not_retried(self):
        for answer in (
                {'status': 'failed', 'ready': False, 'engine_started': True,
                 'error': 'invalid CLI arguments', 'exit_code': 2},
                {'status': 'failed', 'ready': False, 'engine_started': True,
                 'error': 'invalid configuration', 'exit_code': 1},
                {'status': 'refused', 'error': 'refused by policy: model is at capacity'}):
            with self.subTest(answer=answer):
                r, calls = self.run_attempts([dict(answer, engine='codex')])
                self.assertEqual(len(calls), 1)
                self.assertIn(r['failure_class'], ('task', 'policy'))

    def test_retry_max(self):
        r, calls = self.run_attempts([self.failure()])
        self.assertEqual(len(calls), 3)
        self.assertEqual([a.get('retry_of') for a in r['attempts']], [None, 1, 2])

    def test_no_retry_writer_patch_event(self):
        path = os.path.join(self.tmp, 'events.jsonl')
        common.write_text(path, json.dumps({'type': 'item.completed', 'item': {'type': 'file_change',
                          'changes': [{'kind': 'apply_patch'}]}}) + '\n')
        real = jobs._work_free
        with mock.patch.object(jobs, '_work_free', side_effect=lambda spec, a: real(dict(spec, write=True), a)):
            r, calls = self.run_attempts([self.failure(ready=False, events_path=path,
                event_capture_started=True, event_capture_complete=True)])
        self.assertEqual(len(calls), 1)
        self.assertIn('not retried: could not prove the attempt did no work', r['notes'])

    def test_missing_or_malformed_log_is_not_proof(self):
        attempt = {'ready': False, 'engine_started': True,
                   'event_capture_started': True, 'event_capture_complete': True}
        self.assertFalse(jobs._work_free({'write': True}, dict(attempt, events_path='missing')))
        path = os.path.join(self.tmp, 'broken.jsonl'); common.write_text(path, '{bad}\n')
        self.assertFalse(jobs._work_free({'write': True}, dict(attempt, events_path=path)))

    def test_slot_released_during_backoff_then_reacquired(self):
        self.policy['retry']['backoff_s'] = [1]; self.write_config(self.policy)
        order = []; real_acquire = jobs.acquire_slot
        def acquire(*a, **k):
            order.append('acquire'); return real_acquire(*a, **k)
        def sleep(_):
            st = common.read_json(jobs._p(self.only_job(), 'status.json'))
            self.assertEqual(st['status'], 'queued'); self.assertFalse(st.get('waiting_slot'))
            self.assertEqual(jobs.running_count('codex'), 0); order.append('sleep')
        with mock.patch.object(jobs, 'acquire_slot', side_effect=acquire), mock.patch.object(jobs.time, 'sleep', side_effect=sleep):
            r, _ = self.run_attempts([self.failure(), {'status': 'done', 'engine': 'codex', 'ready': True}])
        self.assertEqual(r['status'], 'done'); self.assertEqual(order, ['acquire', 'sleep', 'acquire'])

    def test_meter_unknown_rereads_route_without_launch(self):
        reads = []
        def meter(policy):
            reads.append(1); return tj.meter_with(codex='unknown' if len(reads) == 2 else 'ok')
        jobs._read_meter = meter
        r, launched = self.run_attempts([{'status': 'done', 'engine': 'codex', 'ready': True}])
        self.assertEqual(r['attempts'][0]['failure_class'], 'transient_start')
        self.assertEqual(len(launched), 1); self.assertEqual(r['status'], 'done')
        self.assertGreaterEqual(len(reads), 3)

    def test_detach_retries_wmi_never_ambiguous(self):
        plan = jobs.plan_run(jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work,
                                                          '--engine', 'codex']), self.policy)
        jid = jobs.create_job(plan); result = {'pid': 123, 'created': 456, 'mode': 'wmi', 'job': {}}
        with mock.patch.object(common, 'spawn_detached_ex', side_effect=[common.DetachError('the WMI start failed (WMI unavailable before Create: x)'), result]) as spawn:
            self.assertEqual(jobs._detach_with_retry(jid, plan, ['runner']), result)
            self.assertEqual(spawn.call_count, 2)
        with mock.patch.object(common, 'spawn_detached_ex', side_effect=common.DetachAmbiguous('maybe live')) as spawn:
            with self.assertRaises(common.DetachAmbiguous): jobs._detach_with_retry(jid, plan, ['runner'])
            self.assertEqual(spawn.call_count, 1)

    def test_retry_does_not_consume_failover_attempt(self):
        answers = [self.failure(), {'status': 'limit_hit', 'engine': 'codex', 'limit_hit': True},
                   {'status': 'done', 'engine': 'claude', 'ready': True}]
        r, calls = self.run_attempts(answers, failover=True)
        self.assertEqual(r['status'], 'done')
        self.assertEqual([p['engine'] for _, p in calls], ['codex', 'codex', 'claude'])
        self.assertEqual(r['failover'], {'from': 'codex', 'to': 'claude', 'reason': 'limit_hit'})
        self.assertEqual(r['attempts'][1]['retry_of'], 1)
        self.assertNotIn('retry_of', r['attempts'][2])

    def test_failover_retries_stay_on_the_new_engine(self):
        answers = [{'status': 'limit_hit', 'engine': 'codex', 'limit_hit': True},
                   self.failure(engine='claude'), {'status': 'done', 'engine': 'claude', 'ready': True}]
        r, calls = self.run_attempts(answers, failover=True)
        self.assertEqual(r['status'], 'done')
        self.assertEqual([p['engine'] for _, p in calls], ['codex', 'claude', 'claude'])

    def test_retry_budget_is_shared_across_failover(self):
        answers = [self.failure(), self.failure(), {'status': 'limit_hit', 'engine': 'codex', 'limit_hit': True},
                   self.failure(engine='claude')]
        r, calls = self.run_attempts(answers, failover=True)
        self.assertEqual(len(calls), 4)
        self.assertEqual(r['failure_class'], 'transient_engine')

    def test_backoff_must_fit_timeout_budget(self):
        self.policy['retry']['backoff_s'] = [100000]; self.write_config(self.policy)
        r, calls = self.run_attempts([self.failure()])
        self.assertEqual(len(calls), 1)
        self.assertIn('not retried: timeout budget does not cover backoff', r['notes'])

    def test_retry_settings_defaults_and_malformed_values(self):
        self.assertEqual(retry.settings({}), (2, [30, 90]))
        for value in ({'max': True}, {'max': -1}, {'backoff_s': []}, {'backoff_s': [float('nan')]}):
            self.assertEqual(retry.settings({'retry': value})[0], 0)

    def test_retry_readmission_refusal_is_recorded(self):
        with mock.patch.object(jobs, '_retry_admission', side_effect=jobs.Refusal(common.EXIT_REFUSED, 'writer directory owned')):
            r, calls = self.run_attempts([self.failure()])
        self.assertEqual(len(calls), 1)
        self.assertEqual(r['status'], 'refused')
        self.assertEqual(r['attempts'][1]['retry_of'], 1)
        self.assertEqual(r['failure_class'], 'custody')

    def test_retry_attempts_are_recoverable_beyond_two(self):
        r, _ = self.run_attempts([self.failure(), self.failure(), {'status': 'done', 'engine': 'codex', 'ready': True}])
        jid = r['id']
        common.write_text(jobs._p(jid, 'events-a3.jsonl'), '{}\n')
        common.write_text(jobs._p(jid, 'events-a99.jsonl'), '{}\n')
        seen, ignored = jobs._attempt_file_numbers(jid)
        self.assertIn(3, seen); self.assertIn('events-a99.jsonl', ignored)
        self.assertIsNone(jobs._receipt_inconsistency(jid, r, 3, 3))

    def test_writer_command_events_are_not_work_free(self):
        for item in ('command_execution', 'exec', 'apply_patch', 'mcp_tool_call'):
            path = os.path.join(self.tmp, 'tools.jsonl')
            common.write_text(path, json.dumps({'type': 'item.completed', 'item': {'type': item}}) + '\n')
            self.assertFalse(jobs._work_free({'write': True}, {'ready': False, 'engine_started': True, 'events_path': path,
                'event_capture_started': True, 'event_capture_complete': True}))

    def test_writer_without_ready_needs_positive_complete_capture(self):
        path = os.path.join(self.tmp, 'empty.jsonl'); common.write_text(path, '')
        attempt = {'ready': False, 'engine_started': True, 'events_path': path}
        self.assertFalse(jobs._work_free({'write': True}, attempt))
        self.assertIn('clean EOF not proven', attempt['retry_blocked_reason'])
        for started, complete in ((False, False), (True, False), (False, True)):
            self.assertFalse(jobs._work_free({'write': True}, dict(attempt,
                event_capture_started=started, event_capture_complete=complete)))
        proven = dict(attempt, event_capture_started=True, event_capture_complete=True)
        self.assertTrue(jobs._work_free({'write': True}, proven))
        self.assertFalse(jobs._work_free({'write': True}, dict(proven, engine_started=None)))
        self.assertFalse(jobs._work_free({'write': True}, dict(proven, ready=True)))
        self.assertFalse(jobs._work_free({'write': True}, dict(proven, stream_errors=['read failed'])))
        self.assertFalse(jobs._work_free({'write': True}, dict(proven, event_capture_error='reader killed')))

    def test_partial_or_missing_capture_is_not_proof_even_with_flags(self):
        path = os.path.join(self.tmp, 'partial.jsonl')
        attempt = {'ready': False, 'engine_started': True, 'events_path': path,
                   'event_capture_started': True, 'event_capture_complete': True}
        self.assertFalse(jobs._work_free({'write': True}, attempt))
        for raw in ('{"type":"error"}', '{bad}\n'):
            common.write_text(path, raw)
            self.assertFalse(jobs._work_free({'write': True}, attempt))

    def test_writer_with_complete_error_only_capture_can_retry(self):
        path = os.path.join(self.tmp, 'errors.jsonl')
        common.write_text(path, '{"type":"error","message":"sandbox could not set up"}\n')
        real = jobs._work_free
        with mock.patch.object(jobs, '_work_free', side_effect=lambda spec, a: real(dict(spec, write=True), a)), \
                mock.patch.object(jobs, '_retry_admission'):
            r, calls = self.run_attempts([self.failure(ready=False, events_path=path,
                event_capture_started=True, event_capture_complete=True),
                {'status': 'done', 'engine': 'codex'}])
        self.assertEqual(len(calls), 2)
        self.assertEqual(r['status'], 'done')

    def test_reader_still_alive_after_join_does_not_prove_capture(self):
        jid = '20261026-100004-claude-aaaa'; os.makedirs(jobs.job_dir(jid))
        files = jobs._attempt_files(jid, 1)
        params = {'profile': 'code', 'timeout_s': 10}
        proc = mock.Mock(pid=123)
        reader = mock.Mock(); reader.is_alive.return_value = True
        def start_reader(*a, **k):
            # Even alleged EOF flags cannot override a reader that is still running.
            k['args'][-1].update(started=True, drained=True, clean=True)
            return reader
        with mock.patch.object(jobs.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(common, 'process_created', return_value=456), \
                mock.patch.object(jobs, '_merge_status'), \
                mock.patch.object(jobs.threading, 'Thread', side_effect=start_reader), \
                mock.patch.object(jobs.watchdog, 'wait', return_value=1), \
                mock.patch.object(jobs, '_note_unresolved_tree', return_value=True), \
                mock.patch.object(jobs, '_close_attempt_job', return_value=(False, None)), \
                mock.patch.object(jobs, '_clear_child_starting'):
            outcome = jobs._run_claude(jid, ['fixture'], {'cwd': self.work}, params,
                                       self.brief, files, self.policy)
        reader.join.assert_called_once_with(timeout=15)
        self.assertTrue(outcome['event_capture_started'])
        self.assertFalse(outcome['event_capture_complete'])
        self.assertFalse(jobs._work_free({'write': True}, outcome))

    def test_clean_reader_at_eof_records_positive_capture(self):
        jid = '20261026-100005-claude-aaaa'; os.makedirs(jobs.job_dir(jid))
        files = jobs._attempt_files(jid, 1)
        params = {'profile': 'code', 'timeout_s': 10}
        proc = mock.Mock(pid=123, stdout=io.BytesIO(b'{"type":"error"}\n'))
        with mock.patch.object(jobs.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(common, 'process_created', return_value=456), \
                mock.patch.object(jobs, '_merge_status'), \
                mock.patch.object(jobs.watchdog, 'wait', return_value=1), \
                mock.patch.object(jobs, '_note_unresolved_tree', return_value=True), \
                mock.patch.object(jobs, '_close_attempt_job', return_value=(False, None)), \
                mock.patch.object(jobs, '_clear_child_starting'):
            outcome = jobs._run_claude(jid, ['fixture'], {'cwd': self.work}, params,
                                       self.brief, files, self.policy)
        self.assertTrue(outcome['event_capture_started'])
        self.assertTrue(outcome['event_capture_complete'])
        self.assertFalse(outcome['ready'])
        self.assertTrue(jobs._work_free({'write': True}, outcome))

    def test_postlaunch_bookkeeping_failure_with_empty_log_blocks_writer_retry(self):
        jid = '20261026-100002-claude-aaaa'; os.makedirs(jobs.job_dir(jid))
        files = jobs._attempt_files(jid, 1)
        params = {'profile': 'code', 'timeout_s': 10}
        proc = mock.Mock(pid=123)
        with mock.patch.object(jobs.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(common, 'process_created', return_value=456), \
                mock.patch.object(jobs, '_merge_status', side_effect=OSError('sandbox could not set up')), \
                mock.patch.object(jobs.threading, 'Thread') as reader, \
                mock.patch.object(jobs, '_cleanup_started_child', return_value=False), \
                mock.patch.object(jobs, '_close_attempt_job', return_value=(False, None)), \
                mock.patch.object(jobs, '_clear_child_starting'):
            outcome = jobs._run_claude(jid, ['fixture'], {'cwd': self.work}, params,
                                       self.brief, files, self.policy)
        reader.assert_not_called()
        self.assertEqual(os.path.getsize(files['events']), 0)
        self.assertTrue(outcome['engine_started'])
        self.assertFalse(outcome['ready'])
        self.assertFalse(outcome['event_capture_started'])
        self.assertFalse(outcome['event_capture_complete'])
        self.assertEqual(failclass.classify(outcome), 'transient_engine')
        real = jobs._work_free
        with mock.patch.object(jobs, '_work_free', side_effect=lambda spec, a: real(dict(spec, write=True), a)):
            r, calls = self.run_attempts([dict(outcome, engine='claude')])
        self.assertEqual(len(calls), 1)
        self.assertIn('clean EOF not proven', r['attempts'][0]['retry_blocked_reason'])

    def test_retry_preflight_exhausts_deadline_without_launch(self):
        clock = [100.0]; checks = []
        def preflight(*a, **k):
            checks.append(1)
            if len(checks) == 2:
                clock[0] += 3
            return None, None
        def attempt(n):
            if n == 1: clock[0] += 8
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(jobs, 'preflight', side_effect=preflight):
            r, calls = self.run_attempts([self.failure()], timeout=10, on_attempt=attempt)
        self.assertEqual(len(calls), 1)
        self.assertIn('not launched: timeout budget exhausted after preflight', r['notes'])

    def test_retry_preflight_is_subtracted_from_launch_timeout(self):
        clock = [100.0]; checks = []
        def preflight(*a, **k):
            checks.append(1)
            if len(checks) == 2: clock[0] += 2
            return None, None
        def attempt(n):
            if n == 1: clock[0] += 5
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(jobs, 'preflight', side_effect=preflight):
            r, calls = self.run_attempts([self.failure(), {'status': 'done', 'engine': 'codex'}],
                                         timeout=10, on_attempt=attempt)
        self.assertEqual(r['status'], 'done')
        self.assertEqual(calls[1][1]['timeout_s'], 3)
        self.assertEqual(calls[0][1]['deadline_monotonic'], calls[1][1]['deadline_monotonic'])

    def test_retry_slot_wait_is_bounded_by_job_deadline(self):
        clock = [100.0]; checks = []
        def slot(*a, **k):
            checks.append(k['deadline'])
            return (len(checks) == 1), 'engine slots full'
        def sleep(delay): clock[0] += delay
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(jobs.time, 'sleep', side_effect=sleep), \
                mock.patch.object(jobs, 'slot_attempt', side_effect=slot), \
                mock.patch.object(jobs, 'preflight', return_value=(None, None)):
            r, calls = self.run_attempts([self.failure()], timeout=2)
        self.assertEqual(len(calls), 1)
        self.assertEqual(clock[0], 102)
        self.assertTrue(all(deadline == 102 for deadline in checks))
        self.assertIn('not launched: timeout budget exhausted while waiting for a slot', r['notes'])

    def test_slot_lock_timeout_uses_remaining_budget(self):
        jid = '20261026-100003-codex-aaaa'; os.makedirs(jobs.job_dir(jid))
        with mock.patch.object(jobs.time, 'monotonic', return_value=100), \
                mock.patch.object(jobs, 'slot_lock', side_effect=TimeoutError('busy')) as lock:
            with self.assertRaises(TimeoutError):
                jobs.slot_attempt(jid, 'codex', 1, 1, {}, deadline=102)
        lock.assert_called_once_with(2)

    def test_launch_rechecks_deadline_after_bookkeeping(self):
        with mock.patch.object(jobs.time, 'monotonic', return_value=102):
            with self.assertRaises(jobs.JobDeadline):
                jobs._launch_budget({'deadline_monotonic': 102}, 2)
            self.assertEqual(jobs._launch_budget({'deadline_monotonic': 104}, 10), 2)

    def test_prelaunch_deadline_prevents_popen(self):
        jid = '20261026-100006-claude-aaaa'; os.makedirs(jobs.job_dir(jid))
        params = {'profile': 'code', 'timeout_s': 10, 'deadline_monotonic': 102}
        with mock.patch.object(jobs.time, 'monotonic', return_value=102), \
                mock.patch.object(jobs.subprocess, 'Popen') as popen, \
                mock.patch.object(jobs, '_close_attempt_job', return_value=(False, None)), \
                mock.patch.object(jobs, '_clear_child_starting'):
            outcome = jobs._run_claude(jid, ['fixture'], {'cwd': self.work}, params,
                                       self.brief, jobs._attempt_files(jid, 1), self.policy)
        popen.assert_not_called()
        self.assertEqual(outcome['status'], 'timeout')
        self.assertFalse(outcome['engine_started'])

    def test_backoff_oversleep_cannot_launch_another_attempt(self):
        self.policy['retry']['backoff_s'] = [1]; self.write_config(self.policy)
        clock = [100.0]
        def sleep(delay): clock[0] += delay + 10
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(jobs.time, 'sleep', side_effect=sleep):
            r, calls = self.run_attempts([self.failure()], timeout=10)
        self.assertEqual(len(calls), 1)
        self.assertIn('not retried: timeout budget exhausted during backoff', r['notes'])

    def test_background_retry_preflight_exhaustion_and_shared_deadline(self):
        plan = jobs.plan_run(jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work,
                                                          '--engine', 'codex']), self.policy)
        plan['primary']['timeout_s'] = 10
        jid = jobs.create_job(plan); clock = [100.0]
        def fail(*a, **k):
            clock[0] += 8
            raise common.DetachError('the WMI start failed')
        def preflight(*a, **k):
            clock[0] += 3
            return None, None
        with mock.patch.object(jobs.time, 'monotonic', side_effect=lambda: clock[0]), \
                mock.patch.object(jobs, 'preflight', side_effect=preflight), \
                mock.patch.object(common, 'spawn_detached_ex', side_effect=fail) as spawn:
            with self.assertRaisesRegex(common.DetachError, 'budget exhausted before background launch'):
                jobs._detach_with_retry(jid, plan, ['runner'])
        self.assertEqual(spawn.call_count, 1)
        spec = common.read_json(jobs._p(jid, 'job.json'))
        self.assertEqual(spec['deadline_monotonic'], 110)
        with mock.patch.object(jobs.time, 'monotonic', return_value=105):
            self.assertEqual(jobs._job_deadline(spec) - jobs.time.monotonic(), 5)

    def test_detach_retry_max_and_stop(self):
        plan = jobs.plan_run(jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work,
                                                          '--engine', 'codex']), self.policy)
        jid = jobs.create_job(plan)
        with mock.patch.object(common, 'spawn_detached_ex', side_effect=common.DetachError('the WMI start failed')) as spawn:
            with self.assertRaises(common.DetachError): jobs._detach_with_retry(jid, plan, ['runner'])
            self.assertEqual(spawn.call_count, 3)
        def fail(*a, **k):
            common.write_text(jobs._p(jid, jobs.STOP_REQUESTED), 'stop')
            raise common.DetachError('the WMI start failed')
        with mock.patch.object(common, 'spawn_detached_ex', side_effect=fail) as spawn:
            with self.assertRaises(common.DetachError): jobs._detach_with_retry(jid, plan, ['runner'])
            self.assertEqual(spawn.call_count, 1)

    def test_policy_can_disable_background_retry_during_backoff(self):
        plan = jobs.plan_run(jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work,
                                                          '--engine', 'codex']), self.policy)
        jid = jobs.create_job(plan)
        def fail(*a, **k):
            self.policy['retry']['max'] = 0; self.write_config(self.policy)
            raise common.DetachError('the WMI start failed')
        with mock.patch.object(common, 'spawn_detached_ex', side_effect=fail) as spawn:
            with self.assertRaisesRegex(common.DetachError, 'current retry policy'):
                jobs._detach_with_retry(jid, plan, ['runner'])
            self.assertEqual(spawn.call_count, 1)


if __name__ == '__main__': unittest.main()
