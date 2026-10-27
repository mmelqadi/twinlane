"""Patrol uses disposable files, cached meters and synthetic process identities only."""
import io
import json
import os
import threading
import time
import unittest
from contextlib import ExitStack, contextmanager, nullcontext, redirect_stdout
from unittest import mock
from types import SimpleNamespace

import test_support  # noqa: F401; isolation precedes runtime imports
import test_board as tb
from twinlane import baton, board, common, hostload, jobs, patrol, projects, wip
import twinlane

LOW = {'cpu_pct': 20.0, 'ram_free_mb': 9000, 'engine_procs': 3}
REAL_BOUNDED = patrol._bounded


class PatrolTests(tb.BoardBase):
    def setUp(self):
        super().setUp()
        self.write_meter()
        self.host_reading = dict(LOW)
        self.write_host(LOW)
        sampler = mock.patch.object(hostload, '_SAMPLER', lambda: dict(self.host_reading))
        sampler.start()
        self.addCleanup(sampler.stop)
        self.config = mock.patch.object(common, 'load_policy', return_value=self.policy)
        self.config.start()
        self.addCleanup(self.config.stop)
        self.refuse = mock.patch.object(projects, 'refuse_in_worker', return_value=None)
        self.refuse.start()
        self.addCleanup(self.refuse.stop)
        self.brief = os.path.join(self.tmp, 'brief.md')
        common.write_text(self.brief, 'Review this fixture.')
        # Functional tests exercise readers and persistence without OS scheduling deadlines.
        # The two bounded-reader tests below exercise the real thread/timeout mechanism.
        reader = mock.patch.object(patrol, '_bounded', side_effect=self.read_now)
        reader.start()
        self.addCleanup(reader.stop)

    @staticmethod
    def read_now(name, read, deadline, budget=patrol.READ_BUDGET_S, spawn=False):
        if deadline <= time.monotonic():
            raise TimeoutError('pass budget exhausted')
        from twinlane import observations
        with observations.scope(), (nullcontext() if spawn else common.nonspawning_liveness()):
            try:
                return read()
            except SystemExit as error:
                raise ValueError(str(error)) from error

    def write_host(self, reading, age=0):
        self.host_reading = dict(reading)
        common.write_json(os.path.join(common.STATE, 'hostload.json'), dict(reading, at=time.time() - age))

    def queued(self, ident='request-a', ago=700, engine='codex'):
        row = {'id': ident, 'kind': 'run', 'cwd': self.tmp, 'worktree': self.tmp, 'queued_at': tb.iso_ago(ago),
               'argv': ['run', '--engine', engine, '--cd', self.tmp, '--brief', self.brief]}
        wip.save_queue(wip.queue() + [row])
        return row

    def runtime_wait(self):
        jid = tb.jid_ago(300, 'runtimewait')
        self.add_job(jid, 'queued')
        path = jobs._p(jid, 'status.json')
        status = common.read_json(path)
        status.update(status='queued', waiting_slot=True, waiting_since=tb.iso_ago(700),
                      queue_reason='host busy: cpu 99%')
        common.write_json(path, status)
        return jid

    def run_patrol(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            rc = twinlane.main(['patrol', *args])
        self.assertEqual(rc, 0, out.getvalue())
        return out.getvalue()

    def data(self, *args):
        return json.loads(self.run_patrol('--json', *args))

    def kinds(self, data):
        return {r['kind'] for r in data['findings']}

    def test_empty_home_writes_only_patrol_state_and_no_findings(self):
        os.remove(os.path.join(common.STATE, 'meter.json'))
        before = tb.tree_state(self.tmp)
        with mock.patch('subprocess.Popen', side_effect=AssertionError('no process')), \
                mock.patch.object(board, 'collect_worktrees', side_effect=AssertionError('no git')), \
                mock.patch.object(wip, 'claims', side_effect=AssertionError('no provider')):
            self.assertEqual(self.data()['findings'], [])
        added = set(tb.tree_state(self.tmp)) - set(before)
        self.assertEqual(added, {os.path.join(common.STATE, 'patrol.json')})

    def test_regression_live_job_never_spawns_even_without_proc(self):
        jid = tb.jid_ago(300, 'live')
        self.add_job(jid, 'running')  # the actual test process, never an invented live PID
        path = jobs._p(jid, 'status.json')
        status = common.read_json(path)
        status['pid_created'] = common.process_created(os.getpid()) or 12345
        common.write_json(path, status)
        self.policy['claude']['max_parallel'] = 1
        self.queued(engine='claude')
        real_open = open

        def no_proc(path, *args, **kwargs):
            if os.fspath(path).startswith('/proc/'):
                raise FileNotFoundError('no proc filesystem')
            return real_open(path, *args, **kwargs)

        def signal_zero(pid, sig):
            # Emulate POSIX on every host; os.kill(pid, 0) must NEVER run on Windows.
            self.assertEqual((pid, sig), (os.getpid(), 0))

        with ExitStack() as stack:
            for target in ('subprocess.Popen', 'subprocess.run', 'os.popen'):
                stack.enter_context(mock.patch(target, side_effect=AssertionError('no process')))
            # Also exercise the native platform before simulating macOS's absent /proc.
            native = self.data()
            self.assertNotIn('patrol_error', self.kinds(native))
            stack.enter_context(mock.patch.object(common, 'IS_WIN', False))
            stack.enter_context(mock.patch.object(common.sys, 'platform', 'darwin'))
            stack.enter_context(mock.patch('builtins.open', side_effect=no_proc))
            stack.enter_context(mock.patch.object(common.os, 'kill', side_effect=signal_zero))
            data = self.data()
            self.assertNotIn('patrol_error', self.kinds(data))
            self.assertIn('job_unknown', self.kinds(data))
            wait = next(r for r in data['findings'] if r['id'] == 'request-a')
            self.assertIn('slots full', wait['text'])  # UNKNOWN still occupies a slot
            view = board.collect(policy=self.policy, file_only=True)
            self.assertEqual(view['jobs']['unreadable'], [])
            self.assertEqual(view['jobs']['live'][0]['status'], 'unknown')
            self.assertEqual(common.pid_state(os.getpid(), status['pid_created'], spawn=False), common.UNKNOWN)

    def _seed_wait_clock(self, ident):
        path = os.path.join(common.STATE, 'patrol.json')
        state = common.read_json(path)
        row = next(r for r in state['findings'] if r['kind'] == 'queue_waiting' and r['id'] == ident)
        key = patrol._key(row)
        record = state['seen'][key]
        record['first_at'] = tb.iso_ago(1000)
        record['reminded_at'] = tb.iso_ago(100)
        common.write_json(path, state)
        return key, dict(record)

    def _assert_wait_clock(self, data, ident, key, record, detail):
        row = next(r for r in data['findings'] if r['kind'] == 'queue_waiting' and r['id'] == ident)
        self.assertFalse(row['new'])
        self.assertEqual(patrol._key(row), key)
        self.assertEqual(row['detail'], detail)
        state = common.read_json(os.path.join(common.STATE, 'patrol.json'))
        self.assertEqual(state['seen'][key]['first_at'], record['first_at'])
        self.assertEqual(state['seen'][key]['reminded_at'], record['reminded_at'])
        with open(os.path.join(common.STATE, 'patrol.jsonl'), encoding='utf-8') as handle:
            history = [json.loads(line) for line in handle]
        events = [r['event'] for r in history if r.get('kind') == 'queue_waiting' and r.get('id') == ident]
        self.assertEqual(events, ['opened'])

    def test_regression_runtime_wait_measurements_preserve_reminder(self):
        jid = tb.jid_ago(300, 'waiting')
        self.add_job(jid, 'queued')
        path = jobs._p(jid, 'status.json')
        status = common.read_json(path)
        status.update(status='queued', waiting_slot=True, waiting_since=tb.iso_ago(700),
                      queue_reason='host busy: cpu 99%')
        common.write_json(path, status)
        self.data()
        key, record = self._seed_wait_clock(jid)
        status['queue_reason'] = 'host busy: cpu 100%'
        common.write_json(path, status)
        self._assert_wait_clock(self.data(), jid, key, record, status['queue_reason'])

    def _assert_stale_finding(self, data, prior, source):
        row = next((r for r in data['findings'] if patrol._key(r) == patrol._key(prior)), None)
        self.assertIsNotNone(row, 'prior finding disappeared')
        self.assertTrue(row.get('stale', False), 'incomplete finding not stale')
        self.assertFalse(row['safe_fix'])
        self.assertTrue(any(r['kind'] == 'patrol_error' and r['source'] == source for r in data['findings']))
        with open(os.path.join(common.STATE, 'patrol.jsonl'), encoding='utf-8') as handle:
            self.assertFalse(any(r['event'] == 'resolved' and r['kind'] == prior['kind'] and r['id'] == prior['id']
                                 for r in map(json.loads, handle)))
        return row

    def test_regression_runtime_wait_keeps_board_failure_stale(self):
        jid = self.runtime_wait()
        prior = next(r for r in self.data()['findings'] if r['kind'] == 'queue_waiting' and r['id'] == jid)
        key, record = self._seed_wait_clock(jid)
        with self.broken_evidence(os.path.join(common.STATE, 'meter.json'), 'malformed'), \
                mock.patch.object(wip, 'pump_quietly') as pump:
            data = self.data('--fix')
            pump.assert_not_called()
        self.assertEqual({r['source'] for r in data['findings'] if r['kind'] == 'patrol_error'}, {'board'})
        self._assert_stale_finding(data, prior, 'board')
        self._assert_wait_clock(data, jid, key, record, prior['detail'])
        self.assertFalse(next(r for r in self.data()['findings'] if r['id'] == jid).get('stale', False))

    def test_regression_legacy_request_brief_denied_keeps_prior_stale(self):
        request = self.queued()
        request.pop('cwd')
        wip.save_queue([request])
        self.assertIsNone(wip.launchable(request))  # absolute --brief and --cd still replay safely
        prior = next(r for r in self.data()['findings'] if r['id'] == request['id'])
        self.assertEqual(prior['kind'], 'queue_ready')
        with self.broken_evidence(self.brief, 'stat'), mock.patch.object(wip, 'pump_quietly') as pump:
            data = self.data('--fix')
            pump.assert_not_called()
        self._assert_stale_finding(data, prior, 'queue')
        self.assertFalse(next(r for r in self.data()['findings'] if r['id'] == request['id']).get('stale', False))

    def test_regression_invalid_meter_timestamps_never_enable_pump(self):
        self.queued()
        self.queued('request-b', engine='claude')
        self.write_meter(age=1900)  # stored ok statuses are too old: both engines are unknown
        baseline = self.data()
        self.assertEqual(sum(r['kind'] == 'engine_blocked' for r in baseline['findings']), 2)
        path = os.path.join(common.STATE, 'meter.json')
        with open(path, encoding='utf-8') as handle:
            original = handle.read()
        for engine in ('claude', 'codex', None):
            for value in (None, 'not a timestamp'):
                with self.subTest(engine=engine, value=value):
                    raw = json.loads(original)
                    target, key = (raw[engine], 'reading_at') if engine else (raw, 'read_at')
                    if value is None:
                        target.pop(key)
                    else:
                        target[key] = value
                    common.write_json(path, raw)
                    with mock.patch.object(wip, 'pump_quietly') as pump:
                        data = self.data('--fix')
                        pump.assert_not_called()
                    for prior in baseline['findings']:
                        self._assert_stale_finding(data, prior, 'board')
                    self.assertNotIn('queue_ready', self.kinds(data))
        self.write_meter()
        self.assertEqual(self.kinds(self.data()), {'queue_ready'})  # valid freshness evidence recovers

    def test_an_unsupported_meter_status_never_resolves_a_block_or_readies_a_request(self):
        self.queued('request-a', engine='codex')
        self.write_meter(age=1900)  # the stored ok status is too old: unknown, so the request is blocked
        baseline = self.data()
        self.assertIn('engine_blocked', self.kinds(baseline))
        path = os.path.join(common.STATE, 'meter.json')
        with open(path, encoding='utf-8') as handle:
            original = handle.read()
        for engine in ('claude', 'codex'):
            for value in ('garbled', '', None, 5):
                with self.subTest(engine=engine, status=value):
                    raw = json.loads(original)
                    raw[engine]['status'] = value
                    common.write_json(path, raw)
                    with mock.patch.object(wip, 'pump_quietly') as pump:
                        data = self.data('--fix')
                        pump.assert_not_called()
                    for prior in baseline['findings']:
                        self._assert_stale_finding(data, prior, 'board')
                    self.assertNotIn('queue_ready', self.kinds(data))
        self.write_meter()
        self.assertEqual(self.kinds(self.data()), {'queue_ready'})

    def test_regression_pump_wait_measurements_preserve_reminder(self):
        self.queued()
        for reason in ('host busy: cpu 99%', 'host busy: cpu 100%'):
            with mock.patch.object(wip, 'pump_quietly', return_value={
                    'launched': [], 'skipped': [{'id': 'request-a', 'why': reason}]}):
                data = self.data('--fix')
            if reason.endswith('99%'):
                key, record = self._seed_wait_clock('request-a')
            else:
                self._assert_wait_clock(data, 'request-a', key, record, reason)
                # A report without --fix retains the same categorized pump refusal.
                self._assert_wait_clock(self.data(), 'request-a', key, record, reason)

    def _assert_custody_incomplete(self, data, jid):
        self.assertIn('custody_stale', self.kinds(data))
        row = next(r for r in data['findings'] if r['kind'] == 'custody_stale' and r['id'] == jid)
        self.assertTrue(row['stale'])
        self.assertFalse(row['safe_fix'])
        self.assertTrue(any(r['kind'] == 'patrol_error' and r['source'] == 'custody' for r in data['findings']))
        with open(os.path.join(common.STATE, 'patrol.jsonl'), encoding='utf-8') as handle:
            self.assertFalse(any(r.get('event') == 'resolved' and r.get('kind') == 'custody_stale'
                                 for r in map(json.loads, handle)))

    def test_regression_unreadable_custody_folder_keeps_warning_stale(self):
        jid, path = self.stale_writer()
        self.assertIn('custody_stale', self.kinds(self.data()))
        folder, real_listdir = os.path.dirname(path), os.listdir

        def unreadable(target):
            if os.fspath(target) == folder:
                raise PermissionError('fixture folder unreadable')
            return real_listdir(target)

        with mock.patch.object(os, 'listdir', side_effect=unreadable):
            self._assert_custody_incomplete(self.data('--fix'), jid)
        self.assertTrue(os.path.exists(path))
        self.assertNotIn('stale', next(r for r in self.data()['findings'] if r['kind'] == 'custody_stale'))

    def test_regression_unreadable_custody_claim_keeps_warning_stale(self):
        jid, path = self.stale_writer()
        self.assertIn('custody_stale', self.kinds(self.data()))
        real_open = open

        def unreadable(target, *args, **kwargs):
            if os.fspath(target) == path:
                raise PermissionError('fixture claim unreadable')
            return real_open(target, *args, **kwargs)

        with mock.patch('builtins.open', side_effect=unreadable):
            self._assert_custody_incomplete(self.data('--fix'), jid)
        self.assertTrue(os.path.exists(path))
        os.remove(path)
        self.assertNotIn('custody_stale', self.kinds(self.data()))  # a complete empty read resolves

    def test_regression_read_only_custody_creates_no_lock(self):
        self.data()  # establish patrol's own state files before comparing directory entries
        os.mkdir(os.path.join(common.STATE, 'writers'))
        before = set(os.listdir(common.STATE))
        self.assertNotIn('writers.lock', before)
        self.assertEqual(self.data()['findings'], [])
        self.assertEqual(set(os.listdir(common.STATE)), before)

    def test_bound_baton_does_not_scan_transcripts_or_run_a_provider(self):
        from twinlane import sessions, lanes
        b = baton.empty_baton('bound')
        b['bound_sessions'] = [{'engine': 'codex', 'id': 'fixture-session'}]
        b['repo']['worktree'] = self.tmp
        common.write_json(baton.baton_path('bound'), b)
        with mock.patch.object(sessions, '_candidates', side_effect=AssertionError('no transcripts')), \
                mock.patch.object(lanes, 'effective_state', side_effect=AssertionError('no activity scan')), \
                mock.patch.object(wip, 'claims', side_effect=AssertionError('no provider')):
            self.assertEqual(self.data()['findings'], [])

    def test_stalled_queue_pumps_once_only_with_fix(self):
        self.queued()
        self.queued('request-b')
        with mock.patch.object(wip, 'pump_quietly', return_value={'launched': [
                {'id': 'request-a', 'launched': True}]}) as pump:
            data = self.data()
            self.assertEqual(self.kinds(data), {'queue_ready'})
            self.assertTrue(all(r['safe_fix'] for r in data['findings']))
            pump.assert_not_called()
            data = self.data('--fix')
            pump.assert_called_once()
            self.assertEqual(data['fixed'][0]['id'], 'request-a')
        with open(os.path.join(common.STATE, 'patrol.jsonl'), encoding='utf-8') as h:
            self.assertIn('fixed', h.read())

    def test_queue_waiting_oldest_first_with_host_reason(self):
        self.queued('young', 70)
        self.queued('old', 900)
        self.write_host(dict(LOW, cpu_pct=99))
        with mock.patch.object(jobs, 'running_count', return_value=2):
            data = self.data()
        rows = [r for r in data['findings'] if r['kind'] == 'queue_waiting']
        self.assertEqual([r['id'] for r in rows], ['old', 'young'])
        self.assertIn('host busy', rows[0]['text'])
        self.assertGreater(rows[0]['age_s'], rows[1]['age_s'])
        self.assertFalse(any(r['safe_fix'] for r in rows))

    def test_full_engine_slots_wait_without_pump(self):
        self.queued()
        with mock.patch.object(jobs, 'running_count', return_value=4):
            self.assertIn('slots full', self.data()['findings'][0]['text'])

    def test_unlaunchable_and_unknown_host_are_waits(self):
        row = self.queued()
        os.remove(self.brief)
        self.assertIn('brief file is gone', self.data()['findings'][0]['text'])
        common.write_text(self.brief, 'fixture')
        os.remove(os.path.join(common.STATE, 'hostload.json'))
        with mock.patch.object(hostload, '_SAMPLER', return_value=None):
            self.assertIn('host load unknown', self.data()['findings'][0]['text'])

    def test_queue_stall_config(self):
        self.policy['patrol'] = {'queue_stall_s': 1000}
        self.queued()
        data = self.data()
        self.assertEqual(self.kinds(data), {'queue_ready'})
        self.assertFalse(data['findings'][0]['safe_fix'])

    def stale_writer(self):
        jid = tb.jid_ago(1200, 'cust')
        self.add_job(jid, 'done', started_ago=1200, ended_ago=900)
        path = os.path.join(common.STATE, 'writers', 'fixture.json')
        common.write_json(path, {'id': jid, 'cwd': self.tmp, 'pid': tb.DEAD_PID})
        return jid, path

    def test_stale_custody_release_only_with_fix(self):
        jid, path = self.stale_writer()
        self.assertIn('custody_stale', self.kinds(self.data()))
        self.assertTrue(os.path.exists(path))
        self.data('--fix')
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(common.STATE, 'custody-releases.jsonl')))

    def test_both_unresolved_markers_are_never_released(self):
        for marker in (jobs.TREE_UNRESOLVED, jobs.CHILD_STARTING):
            jid, path = self.stale_writer() if marker == jobs.TREE_UNRESOLVED else (tb.jid_ago(1300, 'pend'), None)
            if path is None:
                self.add_job(jid, 'done', started_ago=1300)
            common.write_json(jobs._p(jid, marker), {'unresolved_tree': True})
        with mock.patch.object(jobs, 'release_stale_writers', wraps=jobs.release_stale_writers) as release, \
                mock.patch.object(wip, 'pump_quietly') as pump:
            data = self.data('--fix')
            rows = [r for r in data['findings'] if r['kind'] == 'custody_unresolved']
            self.assertEqual(len(rows), 2)
            self.assertTrue(all('--tree-gone' in r['remedy'] and 'worktree' in r['text'] for r in rows))
            self.assertTrue(all(not r['safe_fix'] for r in rows))
            release.assert_not_called()  # reporting uses the lock-free snapshot
            pump.assert_not_called()
        self.assertTrue(os.path.exists(path or os.path.join(common.STATE, 'writers', 'fixture.json')))

    def test_orphaned_unknown_and_lost_reuse_board_classification(self):
        ids = [tb.jid_ago(700 + i, f'job{i}') for i in range(3)]
        for jid in ids:
            self.add_job(jid, 'lost')
        statuses = dict(zip(ids, ('orphaned', 'unknown', 'lost')))
        with mock.patch.object(jobs, 'job_status', side_effect=lambda jid: statuses[jid]):
            data = self.data('--fix')
        self.assertTrue({'job_orphaned', 'job_unknown', 'job_lost'} <= self.kinds(data))
        self.assertEqual(next(r for r in data['findings'] if r['kind'] == 'job_lost')['remedy'], 'twinlane resume')
        self.assertTrue(all(not r['safe_fix'] for r in data['findings']))

    def test_job_long_timeout_and_silent_output(self):
        a, b = tb.jid_ago(850, 'long'), tb.jid_ago(1500, 'mute')
        self.add_job(a, 'running', started_ago=850, timeout=1000)
        self.add_job(b, 'running', started_ago=1500, timeout=10000)
        self.policy['watchdog'] = {'stall_s': 1000}
        rows = [r for r in self.data()['findings'] if r['kind'] == 'job_long']
        self.assertEqual({r['id'] for r in rows}, {a, b})
        self.assertTrue(any('80%' in r['text'] for r in rows))
        self.assertTrue(any('no output' in r['text'] for r in rows))
        common.write_text(jobs._p(b, 'events.jsonl'), 'fresh')
        self.assertNotIn(b, [r['id'] for r in self.data()['findings'] if r['kind'] == 'job_long'])

    def test_engine_blocked_lists_requests_and_handoff(self):
        self.write_meter(codex={'status': 'stop', 'max_pct': 95})
        self.queued('one')
        self.queued('two')
        row = next(r for r in self.data()['findings'] if r['kind'] == 'engine_blocked')
        self.assertIn('one, two', row['text'])
        self.assertIn('handoff --cd ', row['remedy'])
        self.assertIn('--to claude', row['remedy'])
        self.assertIn(self.tmp, row['remedy'])
        self.assertFalse(row['safe_fix'])

    def test_slot_leak_is_explicitly_skipped(self):
        data = self.data()
        self.assertTrue(any('slot_leak' in x for x in data['skipped']))
        self.assertNotIn('slot_leak', self.kinds(data))

    def test_waiting_on_person_questions_and_legacy_approvals(self):
        b = baton.empty_baton('waiting')
        b['updated_at'] = tb.iso_ago(4000)
        b['progress_at'] = tb.iso_ago(4000)
        b['holds']['approvals_pending'] = [
            {'id': 'H1', 'action': 'review fixture', 'ask_text': 'May it run?', 'asked_at': tb.iso_ago(3600), 'kind': 'question'},
            {'id': 'H2', 'action': 'approve fixture', 'ask_text': 'Approve?', 'asked_at': tb.iso_ago(1900)}]
        common.write_json(baton.baton_path('waiting'), b)
        rows = [r for r in self.data()['findings'] if r['kind'] == 'waiting_on_person']
        self.assertEqual([r['id'] for r in rows], ['waiting:H1', 'waiting:H2'])
        self.assertTrue(all(r['age_s'] >= 1800 and not r['safe_fix'] for r in rows))

    def test_host_overload_requires_three_consecutive_passes_and_resets(self):
        self.write_host(dict(LOW, cpu_pct=99))
        self.assertNotIn('host_overload', self.kinds(self.data()))
        self.assertNotIn('host_overload', self.kinds(self.data()))
        self.assertIn('host_overload', self.kinds(self.data()))
        self.write_host(LOW)
        self.assertNotIn('host_overload', self.kinds(self.data()))
        self.assertEqual(self.data()['overload_passes'], 0)

    def test_unchanged_suppressed_resolved_printed_and_json_full(self):
        self.queued()
        self.assertIn('queue_ready', self.run_patrol())
        self.assertEqual(self.run_patrol('--quiet'), '')
        data = self.data()
        self.assertFalse(data['findings'][0]['new'])
        wip.save_queue([])
        self.assertIn('resolved: queue_ready', self.run_patrol('--quiet'))

    def test_reminder_after_one_hour(self):
        self.queued()
        self.run_patrol()
        path = os.path.join(common.STATE, 'patrol.json')
        state = common.read_json(path)
        for value in state['seen'].values():
            value['first_at'] = value['reminded_at'] = tb.iso_ago(3601)
        common.write_json(path, state)
        self.assertIn('reminder:', self.run_patrol('--quiet'))
        self.assertEqual(self.run_patrol('--quiet'), '')

    def test_unreadable_state_is_preserved_for_recovery(self):
        path = os.path.join(common.STATE, 'patrol.json')
        for value in ('bad json', '[]', '{"version": 1, "seen": {"x": 0}, "findings": [], "overload_passes": 0}'):
            with self.subTest(value=value):
                common.write_text(path, value)
                self.assertTrue(any(r['kind'] == 'patrol_error' and r['source'] == 'state'
                                    for r in self.data()['findings']))
                with open(path, encoding='utf-8') as handle:
                    self.assertEqual(handle.read(), value)

    def test_failing_collector_reports_error_without_resolving_unknowns(self):
        self.queued()
        self.run_patrol()
        with mock.patch.object(wip, 'queue', side_effect=OSError('unreadable')):
            data = self.data()
        self.assertIn('patrol_error', self.kinds(data))
        self.assertIn('queue_ready', self.kinds(data))
        self.assertTrue(next(r for r in data['findings'] if r['kind'] == 'queue_ready')['stale'])

    def test_each_collector_failure_keeps_pass_running(self):
        for target, name in ((board, 'collect'), (hostload, 'cached'), (jobs, 'running_count'),
                             (jobs, 'stale_writers_snapshot'), (patrol, '_details'), (common, 'load_policy')):
            with self.subTest(name=name), mock.patch.object(target, name, side_effect=ValueError('broken')):
                self.assertIn('patrol_error', self.kinds(self.data()))

    def test_bounded_reader_does_not_accumulate_threads(self):
        gate, done = threading.Event(), threading.Event()
        done.wait = mock.Mock(return_value=False)  # deterministic overdue read
        worker = None
        try:
            with mock.patch.object(patrol, 'threading', SimpleNamespace(Event=lambda: done, Thread=threading.Thread)):
                with self.assertRaises(TimeoutError):
                    REAL_BOUNDED('blocked-fixture', gate.wait, time.monotonic() + 60)
                worker = patrol._READERS[(common.STATE, 'blocked-fixture')]
                with self.assertRaises(TimeoutError):
                    REAL_BOUNDED('blocked-fixture', lambda: None, time.monotonic() + 60)
                self.assertIs(patrol._READERS[(common.STATE, 'blocked-fixture')], worker)
                done.wait.assert_called_once()
        finally:
            gate.set()
            if worker is not None:
                worker.join()

    def test_loop_runs_twice_and_worker_refusal_only_for_fix(self):
        with mock.patch.object(patrol.time, 'sleep') as sleep:
            self.run_patrol('--loop', '30', '--max-runs', '2', '--quiet')
            sleep.assert_called_once_with(30)
        with mock.patch.object(projects, 'refuse_in_worker', return_value=3) as refuse, redirect_stdout(io.StringIO()):
            self.assertEqual(patrol.cli([]), 0)
            refuse.assert_not_called()
            self.assertEqual(patrol.cli(['--fix']), 3)
            refuse.assert_called_once_with('patrol --fix')

    def test_unreadable_baton_cannot_resolve_a_person_wait(self):
        b = baton.empty_baton('question')
        b['updated_at'] = tb.iso_ago(4000)
        b['progress_at'] = tb.iso_ago(4000)
        b['holds']['approvals_pending'] = [{'id': 'H1', 'action': 'review fixture', 'ask_text': 'Review?'}]
        common.write_json(baton.baton_path('question'), b)
        self.assertIn('waiting_on_person', self.kinds(self.data()))
        common.write_text(baton.baton_path('question'), '{bad json')
        data = self.data()
        self.assertIn('patrol_error', self.kinds(data))
        row = next(r for r in data['findings'] if r['kind'] == 'waiting_on_person')
        self.assertTrue(row['stale'])

    def test_redacts_text_json_state_and_history(self):
        with mock.patch.object(board, 'collect', side_effect=OSError(tb.SECRET)):
            out = self.run_patrol('--json')
        self.assertNotIn(tb.SECRET, out)
        for name in ('patrol.json', 'patrol.jsonl'):
            with open(os.path.join(common.STATE, name), encoding='utf-8') as h:
                self.assertNotIn(tb.SECRET, h.read())

    def test_failed_queue_evidence_never_triggers_a_fix(self):
        self.queued()
        self.run_patrol()
        with mock.patch.object(wip, 'queue', side_effect=OSError('unreadable')), \
                mock.patch.object(wip, 'pump_quietly') as pump:
            self.data('--fix')
            pump.assert_not_called()

    def test_runtime_slot_wait_has_age_reason_and_engine_block(self):
        jid = tb.jid_ago(700, 'wait')
        self.add_job(jid, 'queued', engine='codex')
        common.write_json(jobs._p(jid, 'status.json'), {'status': 'queued', 'pid': os.getpid(),
                          'engine': 'codex', 'waiting_since': tb.iso_ago(700), 'queue_reason': 'slots full'})
        self.write_meter(codex={'status': 'out', 'max_pct': 100})
        data = self.data()
        waiting = next(r for r in data['findings'] if r['kind'] == 'queue_waiting')
        self.assertEqual(waiting['id'], jid)
        self.assertIn('slots full', waiting['text'])
        self.assertGreater(waiting['age_s'], 600)
        self.assertIn('engine_blocked', self.kinds(data))

    def test_overload_identity_does_not_change_with_job_age(self):
        jid = tb.jid_ago(700, 'busy')
        self.add_job(jid, 'running', engine='codex')
        self.write_host(dict(LOW, cpu_pct=99))
        with mock.patch.object(common, 'fmt_age', wraps=common.fmt_age):
            for i in range(3):
                self.data()
            with mock.patch.object(common, 'fmt_age', return_value='different age'):
                data = self.data()
            row = next(r for r in data['findings'] if r['kind'] == 'host_overload')
            self.assertFalse(row['new'])
            self.assertEqual(row['running_jobs'][0]['id'], jid)

    def test_entire_collect_is_bounded_and_report_is_still_written(self):
        gate, done = threading.Event(), threading.Event()
        done.wait = mock.Mock(return_value=False)
        def bounded(name, read, deadline, budget=patrol.READ_BUDGET_S, spawn=False):
            if name == 'collect':
                with mock.patch.object(patrol, 'threading', SimpleNamespace(Event=lambda: done, Thread=threading.Thread)):
                    return REAL_BOUNDED(name, read, deadline, budget, spawn)
            return self.read_now(name, read, deadline, budget, spawn)
        try:
            with mock.patch.object(patrol, '_bounded', side_effect=bounded), \
                    mock.patch.object(patrol, 'collect', side_effect=lambda *a: gate.wait()):
                data = self.data()
            self.assertIn('patrol_error', self.kinds(data))
            self.assertTrue(os.path.exists(os.path.join(common.STATE, 'patrol.json')))
            done.wait.assert_called_once()
        finally:
            gate.set()
            worker = patrol._READERS.get((common.STATE, 'collect'))
            if worker is not None:
                worker.join()

    def test_history_is_capped(self):
        path = os.path.join(common.STATE, 'patrol.jsonl')
        common.write_text(path, '{}\n' * 2100)
        self.queued()
        self.run_patrol()
        with open(path, encoding='utf-8') as h:
            self.assertEqual(len(h.readlines()), 2000)

    def test_remedies_obey_expired_deadlines(self):
        jid, path = self.stale_writer()
        with self.assertRaises(TimeoutError):
            jobs.release_stale_writers(deadline=time.monotonic() - 1)
        self.assertTrue(os.path.exists(path))
        self.queued()
        before = common.read_json(wip.queue_path())
        with mock.patch.object(wip, '_spawn') as spawn:
            with self.assertRaises(TimeoutError):
                wip.pump(self.policy, busy=0, reading=LOW, deadline=time.monotonic() - 1)
            spawn.assert_not_called()
        self.assertEqual(common.read_json(wip.queue_path()), before)

    def test_regression_pressure_allows_minimum_slots_and_pump(self):
        self.policy['hostload'] = {'min_parallel': 2}
        self.write_host(dict(LOW, cpu_pct=99))
        self.queued()
        with mock.patch.object(jobs, 'running_count', return_value=0), \
                mock.patch.object(wip, 'pump_quietly', return_value={
                    'launched': [{'id': 'request-a', 'launched': True}]}) as pump:
            data = self.data('--fix')
        pump.assert_called_once()
        self.assertIn('queue_stalled', self.kinds(data))
        self.assertEqual(data['fixed'][0]['id'], 'request-a')

    def test_regression_unusable_counters_keep_overload_stale(self):
        self.write_host(dict(LOW, cpu_pct=99))
        for _ in range(3):
            self.data()
        self.queued()
        unreadable = {'cpu_pct': None, 'ram_free_mb': None, 'engine_procs': None, 'heap': None}
        for reading in (unreadable, dict(unreadable, cpu_pct='bad', ram_free_mb=-1, engine_procs=float('inf'))):
            with self.subTest(reading=reading), mock.patch.object(wip, 'pump_quietly') as pump:
                self.write_host(reading)
                data = self.data('--fix')
                pump.assert_not_called()
                self.assertEqual(data['overload_passes'], 3)
                self.assertEqual(data['host_status'], 'unknown')
                self.assertNotIn('queue_stalled', self.kinds(data))
                self.assertTrue(next(r for r in data['findings'] if r['kind'] == 'host_overload')['stale'])
                self.assertIn('host load unknown', next(r for r in data['findings'] if r['id'] == 'request-a')['text'])

    def test_regression_writer_capacity_and_reservations_defer_to_pump(self):
        self.policy['wip'] = {'max_writers': 5}
        self.queued()
        with mock.patch.object(jobs, 'running_count', return_value=0), \
                mock.patch.object(wip, 'collect', return_value=[{'worktree': str(i)} for i in range(5)]), \
                mock.patch.object(wip, '_spawn') as spawn:
            data = self.data()
            self.assertEqual(self.kinds(data), {'queue_ready'})
            self.assertEqual(data['findings'][0]['severity'], 'info')
            self.assertIn('writer capacity is decided by the pump', data['findings'][0]['text'])
            with mock.patch.object(hostload, 'sample', return_value=LOW):
                data = self.data('--fix')
            spawn.assert_not_called()
            self.assertEqual(self.kinds(data), {'queue_waiting'})
            self.assertEqual(data['findings'][0]['pump_reason'], '5/5 writer lanes busy')
            data = self.data()
            self.assertEqual(self.kinds(data), {'queue_waiting'})
            self.assertFalse(data['findings'][0]['new'])
            self.assertEqual(data['findings'][0]['pump_reason'], '5/5 writer lanes busy')
            # A changed authoritative reason updates the wait, without inventing a stall.
            with mock.patch.object(wip, 'pump_quietly', return_value={
                    'launched': [], 'skipped': [{'id': 'request-a', 'why': 'a pump is starting it'}]}):
                data = self.data('--fix')
            self.assertEqual(data['findings'][0]['pump_reason'], 'a pump is starting it')
            self.assertTrue(data['findings'][0]['new'])
        row = self.queued()
        reserved = dict(self.queued('reserved'), pumping_at=tb.iso_ago(60), claim='fixture')
        wip.save_queue([row, reserved])
        with mock.patch.object(wip, 'collect', return_value=[{}] * 4), \
                mock.patch.object(hostload, 'sample', return_value=LOW), \
                mock.patch.object(wip, '_spawn') as spawn:
            data = self.data('--fix')
            spawn.assert_not_called()
        self.assertNotIn('queue_stalled', self.kinds(data))
        self.assertEqual(next(r for r in data['findings'] if r['id'] == 'request-a')['pump_reason'],
                         'a pump is starting it')
        self.assertEqual(next(r for r in data['findings'] if r['id'] == 'reserved')['kind'], 'queue_claimed')
        for claim, needs_person in (({'pumping_at': tb.iso_ago(60), 'claim': 'fixture'}, False),
                                    ({'pumping_at': tb.iso_ago(601), 'claim': 'fixture'}, True),
                                    ({'claim': 'fixture'}, True)):
            with self.subTest(claim=claim), mock.patch.object(wip, 'pump_quietly') as pump:
                row = self.queued()
                wip.save_queue([dict(row, **claim)])
                data = self.data('--fix')
                pump.assert_not_called()
                self.assertEqual(self.kinds(data), {'queue_claimed'})
                finding = data['findings'][0]
                self.assertFalse(finding['safe_fix'])
                self.assertEqual(finding['needs_person'], needs_person)
        self.queued('unclaimed')
        with mock.patch.object(wip, 'pump_quietly') as pump:
            self.data('--fix')
            pump.assert_not_called()  # a claim without its timestamp defers the whole pump

    def test_regression_moving_load_keeps_identity_and_reminder(self):
        path = os.path.join(common.STATE, 'patrol.json')
        self.write_host(dict(LOW, cpu_pct=99))
        for _ in range(3):
            self.data()
        before = common.read_json(path)
        key = next(k for k in before['seen'] if k.startswith('host_overload:'))
        self.write_host(dict(LOW, cpu_pct=100))
        self.assertEqual(self.run_patrol('--quiet'), '')
        after = common.read_json(path)
        row = next(r for r in after['findings'] if r['kind'] == 'host_overload')
        self.assertFalse(row['new'])
        self.assertIn('cpu 100%', row['detail'])
        self.assertEqual(set(after['seen']), set(before['seen']))
        self.assertEqual(after['seen'][key]['first_at'], before['seen'][key]['first_at'])
        self.assertEqual(after['seen'][key]['reminded_at'], before['seen'][key]['reminded_at'])
        after['seen'][key]['reminded_at'] = tb.iso_ago(3601)
        common.write_json(path, after)
        self.assertIn('reminder: host_overload', self.run_patrol('--quiet'))
        with open(os.path.join(common.STATE, 'patrol.jsonl'), encoding='utf-8') as h:
            events = [json.loads(line) for line in h]
        self.assertEqual([e['event'] for e in events if e['kind'] == 'host_overload'], ['opened'])

    def test_regression_whole_pass_never_refreshes_host_cache(self):
        path = os.path.join(common.STATE, 'hostload.json')
        # Real sampler path, including macOS fallbacks; no replacement sampler hides a spawn.
        with mock.patch.object(hostload, '_SAMPLER', None), \
                mock.patch.dict(os.environ), mock.patch.object(common, 'IS_WIN', False), \
                mock.patch.object(hostload.sys, 'platform', 'darwin'), \
                mock.patch.object(hostload, '_posix_times', side_effect=OSError('no proc')), \
                mock.patch.object(hostload, '_posix_free_mb', side_effect=OSError('no proc')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('no Popen')) as popen, \
                mock.patch('subprocess.run', side_effect=AssertionError('no run')) as run, \
                mock.patch('os.popen', side_effect=AssertionError('no os.popen')) as os_popen:
            os.environ.pop('TWINLANE_TEST_ISOLATION', None)
            self.write_host(LOW, age=301)
            before = common.read_json(path)
            data = self.data()
            popen.assert_not_called()
            run.assert_not_called()
            os_popen.assert_not_called()
            self.assertEqual(data['host_status'], 'unknown')
            self.assertEqual(common.read_json(path), before)
            self.write_host(LOW, age=60)  # older than sample_ttl_s, still valid for patrol
            self.assertEqual(self.data()['host_status'], 'known')
            os.remove(path)
            self.assertEqual(self.data()['host_status'], 'unknown')
            self.assertFalse(os.path.exists(path))

    def test_regression_quiet_job_respects_watchdog_enabled(self):
        jid = tb.jid_ago(1500, 'quiet')
        self.add_job(jid, 'running', started_ago=1500, timeout=10000)
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                self.policy['watchdog'] = {'enabled': enabled, 'stall_s': 1000}
                row = next(r for r in self.data()['findings'] if r['kind'] == 'job_long')
                if enabled:
                    self.assertIn('watchdog handles recovery', row['text'])
                    self.assertEqual(row['remedy'], 'twinlane status ' + jid)
                else:
                    self.assertIn('inspect it; the watchdog is off', row['text'])
                    self.assertEqual(row['remedy'], 'twinlane stop ' + jid)
                    self.assertNotIn('watchdog handles recovery', row['text'])


    @contextmanager
    def broken_evidence(self, path, mode):
        """Path-specific failures; every unrelated operation uses its real implementation."""
        real_open, real_stat, real_listdir, real_scandir = open, os.stat, os.listdir, os.scandir
        if mode in ('malformed', 'timestamp', 'partial_host', 'queue_identity',
                    'meter_reading_missing', 'meter_reading_timestamp', 'meter_read_missing', 'meter_read_timestamp'):
            with real_open(path, 'rb') as handle:
                before = handle.read()
            if mode == 'malformed':
                common.write_text(path, '{broken json')
            else:
                value = json.loads(before)
                if mode == 'timestamp':
                    value['waiting_since'] = 'not a timestamp'
                elif mode == 'partial_host':
                    value['cpu_pct'] = None
                elif mode.startswith('meter_'):
                    target, key = (value['codex'], 'reading_at') if mode.startswith('meter_reading_') else (value, 'read_at')
                    if mode.endswith('_missing'):
                        target.pop(key)
                    else:
                        target[key] = 'not a timestamp'
                else:
                    value['queue'][0].pop('id')
                common.write_json(path, value)
            try:
                yield
            finally:
                with real_open(path, 'wb') as handle:
                    handle.write(before)
            return

        def failure(real):
            def call(target, *args, **kwargs):
                if isinstance(target, (str, bytes, os.PathLike)) and os.fspath(target) == path:
                    raise PermissionError('fixture evidence unreadable')
                return real(target, *args, **kwargs)
            return call

        target, name, real = {'open': ('builtins', 'open', real_open),
                              'stat': (os, 'stat', real_stat),
                              'listdir': (os, 'listdir', real_listdir),
                              'scandir': (os, 'scandir', real_scandir)}[mode]
        patch = mock.patch('builtins.open', side_effect=failure(real)) if target == 'builtins' else \
            mock.patch.object(target, name, side_effect=failure(real))
        with patch:
            yield

    def test_regression_damaged_real_custody_receipt_keeps_warning(self):
        jid, claim = self.stale_writer()
        self.assertIn('custody_stale', self.kinds(self.data()))
        for mode in ('malformed', 'open'):
            with self.subTest(mode=mode), self.broken_evidence(jobs._p(jid, 'result.json'), mode), \
                    mock.patch.object(jobs, 'release_stale_writers') as release:
                self._assert_custody_incomplete(self.data('--fix'), jid)
                release.assert_not_called()
            self.assertTrue(os.path.exists(claim))
            self.assertFalse(next(r for r in self.data()['findings'] if r['kind'] == 'custody_stale').get('stale', False))

    def test_regression_unenumerable_batons_keep_person_wait(self):
        b = baton.empty_baton('denied')
        b.update(waiting_on='user', waiting_since=tb.iso_ago(4000))
        common.write_json(baton.baton_path('denied'), b)
        self.assertIn('waiting_on_person', self.kinds(self.data()))
        # glob.glob uses scandir; patrol's explicit enumeration uses listdir.
        with self.broken_evidence(common.BATONS, 'scandir'), self.broken_evidence(common.BATONS, 'listdir'):
            data = self.data()
        row = next((r for r in data['findings'] if r['kind'] == 'waiting_on_person'), None)
        self.assertIsNotNone(row)
        self.assertTrue(row.get('stale', False))
        self.assertFalse(row['safe_fix'])
        self.assertTrue(any(r['kind'] == 'patrol_error' and r['source'] == 'board' for r in data['findings']))
        with open(os.path.join(common.STATE, 'patrol.jsonl'), encoding='utf-8') as handle:
            self.assertFalse(any(r['event'] == 'resolved' and r['kind'] == 'waiting_on_person' for r in map(json.loads, handle)))
        os.remove(baton.baton_path('denied'))
        os.rmdir(common.BATONS)
        self.assertNotIn('waiting_on_person', self.kinds(self.data()))  # genuinely absent is complete

    def test_regression_each_reader_keeps_prior_findings_without_resolved_events(self):
        """One matrix covers every collect reader, including already guarded controls."""
        jid, claim = self.stale_writer()
        quiet = tb.jid_ago(1500, 'quietmatrix')
        self.add_job(quiet, 'running', started_ago=1500, timeout=10000)
        b = baton.empty_baton('matrix')
        b.update(waiting_on='user', waiting_since=tb.iso_ago(700))
        self.policy['patrol'] = {'person_wait_s': 100}
        common.write_json(baton.baton_path('matrix'), b)
        self.queued()
        legacy = self.queued('legacy-request')
        legacy.pop('cwd')
        wip.save_queue([r for r in wip.queue() if r['id'] != legacy['id']] + [legacy])
        runtime = self.runtime_wait()
        # Owner notifications are a separate, tolerant inbox reader inside board.collect.
        notice_job = tb.jid_ago(1300, 'cafe')
        self.add_job(notice_job, 'done', started_ago=1300)
        common.write_json(jobs._p(notice_job, jobs.OWNER_NOTIFIED),
                          {'owner': 'matrix', 'waiting_on': 'user', 'waiting_since': b['waiting_since'],
                           'at': tb.iso_ago(700), 'engine': 'codex'})
        marker = jobs._p(notice_job, jobs.CHILD_STARTING)
        common.write_json(marker, {'unresolved_tree': True})
        self.write_host(dict(LOW, cpu_pct=99))
        for _ in range(3):
            baseline = self.data()
        self.assertNotIn('patrol_error', self.kinds(baseline))
        self.assertIn('host_overload', self.kinds(baseline), str(baseline))
        state_path, history_path = (os.path.join(common.STATE, n) for n in ('patrol.json', 'patrol.jsonl'))
        before_state = common.read_json(state_path)
        with open(history_path, encoding='utf-8') as handle:
            before_history = handle.read()
        cases = [
            ('meters', 'board', 'waiting_on_person', 'matrix', os.path.join(common.STATE, 'meter.json'), 'malformed'),
            ('meters denied', 'board', 'waiting_on_person', 'matrix', os.path.join(common.STATE, 'meter.json'), 'open'),
            ('meter reading missing', 'board', 'queue_ready', 'request-a', os.path.join(common.STATE, 'meter.json'), 'meter_reading_missing'),
            ('meter reading timestamp', 'board', 'queue_ready', 'request-a', os.path.join(common.STATE, 'meter.json'), 'meter_reading_timestamp'),
            ('meter read missing', 'board', 'queue_ready', 'request-a', os.path.join(common.STATE, 'meter.json'), 'meter_read_missing'),
            ('meter read timestamp', 'board', 'queue_ready', 'request-a', os.path.join(common.STATE, 'meter.json'), 'meter_read_timestamp'),
            ('runtime wait board', 'board', 'queue_waiting', runtime, os.path.join(common.STATE, 'meter.json'), 'malformed'),
            ('job directory', 'board', 'job_long', quiet, common.JOBS, 'scandir'),
            ('job row', 'board', 'job_long', quiet, jobs._p(quiet, 'status.json'), 'malformed'),
            ('job stat', 'board', 'job_long', quiet, jobs._p(quiet, 'status.json'), 'stat'),
            ('baton directory', 'board', 'waiting_on_person', 'matrix', common.BATONS, 'listdir'),
            ('baton timestamp', 'board', 'waiting_on_person', 'matrix', baton.baton_path('matrix'), 'timestamp'),
            ('baton file', 'board', 'waiting_on_person', 'matrix', baton.baton_path('matrix'), 'malformed'),
            ('inbox notice', 'board', 'waiting_on_person', 'matrix:' + notice_job, jobs._p(notice_job, jobs.OWNER_NOTIFIED), 'malformed'),
            ('queue', 'queue', 'queue_ready', 'request-a', wip.queue_path(), 'malformed'),
            ('queue identity', 'queue', 'queue_ready', 'request-a', wip.queue_path(), 'queue_identity'),
            ('queue denied', 'queue', 'queue_ready', 'request-a', wip.queue_path(), 'open'),
            ('queue brief stat', 'queue', 'queue_ready', 'request-a', self.brief, 'stat'),
            ('legacy queue brief stat', 'queue', 'queue_ready', 'legacy-request', self.brief, 'stat'),
            ('runtime wait queue', 'queue', 'queue_waiting', runtime, wip.queue_path(), 'malformed'),
            ('runtime wait details', 'details', 'queue_waiting', runtime, jobs._p(runtime, 'status.json'), 'malformed'),
            ('board overload dependency', 'board', 'host_overload', 'host', os.path.join(common.STATE, 'meter.json'), 'malformed'),
            ('board queue dependency', 'board', 'queue_ready', 'request-a', os.path.join(common.STATE, 'meter.json'), 'malformed'),
            ('details overload dependency', 'details', 'host_overload', 'host', jobs._attempt_files(quiet, 1)['events'], 'stat'),
            ('details queue dependency', 'details', 'queue_ready', 'request-a', jobs._attempt_files(quiet, 1)['events'], 'stat'),
            ('host queue dependency', 'hostload', 'queue_ready', 'request-a', os.path.join(common.STATE, 'hostload.json'), 'malformed'),
            ('runtime wait host dependency', 'hostload', 'queue_waiting', runtime, os.path.join(common.STATE, 'hostload.json'), 'malformed'),
            ('runtime wait slots dependency', 'slots', 'queue_waiting', runtime, None, 'slots'),
            ('policy queue dependency', 'policy', 'queue_ready', 'request-a', None, 'policy'),
            ('policy overload dependency', 'policy', 'host_overload', 'host', None, 'policy'),
            ('policy custody dependency', 'policy', 'custody_stale', jid, None, 'policy'),
            ('policy details dependency', 'policy', 'custody_unresolved', notice_job, None, 'policy'),
            ('policy runtime wait dependency', 'policy', 'queue_waiting', runtime, None, 'policy'),
            ('host cache', 'hostload', 'host_overload', 'host', os.path.join(common.STATE, 'hostload.json'), 'malformed'),
            ('partial host', 'hostload', 'host_overload', 'host', os.path.join(common.STATE, 'hostload.json'), 'partial_host'),
            ('host denied', 'hostload', 'host_overload', 'host', os.path.join(common.STATE, 'hostload.json'), 'open'),
            ('custody receipt', 'custody', 'custody_stale', jid, jobs._p(jid, 'result.json'), 'malformed'),
            ('custody claim', 'custody', 'custody_stale', jid, claim, 'malformed'),
            ('custody directory', 'custody', 'custody_stale', jid, os.path.dirname(claim), 'listdir'),
            ('detail marker', 'details', 'custody_unresolved', notice_job, marker, 'malformed'),
            ('detail output', 'details', 'job_long', quiet, jobs._attempt_files(quiet, 1)['events'], 'stat'),
            ('slots', 'slots', 'queue_ready', 'request-a', None, 'slots'),
            ('policy/watchdog settings', 'policy', 'waiting_on_person', 'matrix', None, 'policy'),
        ]
        for reader, source, kind, ident, path, mode in cases:
            with self.subTest(reader=reader):
                common.write_json(state_path, before_state)
                common.write_text(history_path, before_history)
                old = next(r for r in baseline['findings'] if r['kind'] == kind and r['id'] == ident)
                if mode == 'slots':
                    broken = mock.patch.object(jobs, 'running_count', return_value=None)
                elif mode == 'policy':
                    broken = mock.patch.object(common, 'load_policy', side_effect=SystemExit('fixture policy unreadable'))
                else:
                    broken = self.broken_evidence(path, mode)
                with broken:
                    data = self.data()
                self._assert_stale_finding(data, old, source)

    def test_regression_unreadable_state_does_not_overwrite_prior_observation(self):
        self.queued()
        self.data()
        path = os.path.join(common.STATE, 'patrol.json')
        with open(path, 'rb') as handle:
            before = handle.read()
        history = os.path.join(common.STATE, 'patrol.jsonl')
        with open(history, 'rb') as handle:
            old_history = handle.read()
        with self.broken_evidence(path, 'open'), mock.patch.object(wip, 'pump_quietly') as pump:
            data = self.data('--fix')
            pump.assert_not_called()
        self.assertTrue(any(r['kind'] == 'patrol_error' and r['source'] == 'state' for r in data['findings']))
        with open(path, 'rb') as handle:
            self.assertEqual(handle.read(), before)
        with open(history, 'rb') as handle:
            self.assertEqual(handle.read(), old_history)
        self.assertFalse(next(r for r in self.data()['findings'] if r['kind'] == 'queue_ready')['new'])


if __name__ == '__main__':
    unittest.main()
