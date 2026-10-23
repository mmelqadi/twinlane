"""Synthetic standard-start metadata contract; no native probes or engines."""
import contextlib
import builtins
import io
import json
import os
import socket
import subprocess
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # disposable home before importing runtime
from twinlane import common, jobs
REAL_PID_STATE = jobs._pid_state


class PreservingStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=test_support._HOME)
        self.root = Path(self.temp.name)
        self.store = self.root / 'jobs'
        self.store.mkdir()
        self.state = self.root / 'state'
        self.state.mkdir()
        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        # Hermetic against the invoking shell: no inherited explicit binary, adapter or private config.
        env = self.stack.enter_context(mock.patch.dict(os.environ, {'TWINLANE_CONFIG': str(self.root / 'absent.json')}))
        for name in ('TWINLANE_CODEX_BIN', 'TWINLANE_CODEX_RUN', 'TWINLANE_CODEX_BIN_QUALIFIED'):
            env.pop(name, None)
        self.stack.enter_context(mock.patch.object(common, 'CODEX_RUN', None))
        self.stack.enter_context(mock.patch.object(common, 'JOBS', str(self.store)))
        self.stack.enter_context(mock.patch.object(common, 'STATE', str(self.state)))
        self.stack.enter_context(mock.patch.object(common, '_k32', side_effect=AssertionError('native probe')))
        self.stack.enter_context(mock.patch.object(subprocess, 'Popen', side_effect=AssertionError('engine/provider process')))
        self.stack.enter_context(mock.patch.object(socket.socket, 'connect', side_effect=AssertionError('network/provider')))
        self.stack.enter_context(mock.patch.object(common, 'pid_state', return_value=common.DEAD))
        self.stack.enter_context(mock.patch.object(jobs, '_pid_state', return_value=common.DEAD))
        self.stack.enter_context(mock.patch.object(jobs, '_self_created', return_value=12345))
        self.old = '20260101-000000-claude-abcd'
        self.new = '20260101-000001-claude-efab'
        self.folder = self.store / self.old
        self.folder.mkdir()
        self.cwd = str(self.root)
        # These synthetic worktrees have no git process. Mirror the toplevel probe without
        # weakening the existing engine/provider/native-process tripwires.
        git_probe = common.git
        def root_probe(cwd, *args, **kwargs):
            if args == ('rev-parse', '--show-toplevel'):
                return 0, self.cwd
            return git_probe(cwd, *args, **kwargs)
        self.stack.enter_context(mock.patch.object(common, 'git', side_effect=root_probe))
        self.write('job.json', {'id': self.old, 'cwd': self.cwd, 'profile': 'code', 'writer_reservation_version': 1})
        self.write('status.json', {'status': 'running', 'engine': 'claude', 'pid': 101, 'pid_created': 9001})
        self.write(jobs.CHILD_STARTING, {'pid': 102, 'created': 9002, 'engine': 'claude'})
        self.write(jobs.HANDOFF, {'env': {'SYNTHETIC': 'only'}})
        os.utime(self.folder / jobs.HANDOFF, (1, 1))

    def write(self, name, data):
        (self.folder / name).write_text(json.dumps(data), encoding='utf-8')

    def snapshot(self):
        return {p.name: (p.read_bytes(), p.stat().st_mtime_ns) for p in self.folder.iterdir()}

    @contextlib.contextmanager
    def protect_old_store(self):
        """Tripwire attempted mutations too, even when caught and hidden by runtime code."""
        attempted = []
        def old(path):
            return not isinstance(path, int) and Path(path).resolve().is_relative_to(self.folder)
        def mutation(fn):
            def checked(*args, **kwargs):
                if any(old(p) for p in args[:2]):
                    attempted.append(args)
                    raise AssertionError('old metadata mutation')
                return fn(*args, **kwargs)
            return checked
        original_open = builtins.open
        def checked_open(path, mode='r', *args, **kwargs):
            if any(c in mode for c in 'wax+') and old(path):
                attempted.append((path, mode))
                raise AssertionError('old metadata write')
            return original_open(path, mode, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=checked_open), \
                mock.patch.object(os, 'remove', side_effect=mutation(os.remove)), \
                mock.patch.object(os, 'unlink', side_effect=mutation(os.unlink)), \
                mock.patch.object(os, 'replace', side_effect=mutation(os.replace)), \
                mock.patch.object(os, 'rename', side_effect=mutation(os.rename)):
            yield
        self.assertEqual(attempted, [])

    def spec(self):
        return {'id': self.new, 'cwd': self.cwd, 'profile': 'code', 'engine': 'claude',
                'primary': {'engine': 'claude'},
                'kind': 'code', 'name': 'synthetic',
                'preserve_existing_metadata': True}

    def test_actual_runner_admits_exact_dead_marker_without_deletion(self):
        new_folder = self.store / self.new
        new_folder.mkdir()
        (new_folder / 'job.json').write_text(json.dumps(self.spec()))
        before = self.snapshot()
        with self.protect_old_store(), mock.patch.object(jobs, '_run_claimed', return_value={'status': 'done'}):
            result = jobs.run_job(self.new)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(result['status'], 'done')
        self.assertFalse((new_folder / jobs.RUNNER_LOCK).exists())

    def test_malformed_markers_hold_without_native_probe_or_change(self):
        for pending in (None, [], {'pid': True, 'created': 9002}, {'pid': 102, 'created': False},
                        {'pid': 102, 'created': '9002'}, {'pid': 2**40, 'created': 9002},
                        {'pid': 102, 'created': 2**64}, {'engine': 'codex'}):
            with self.subTest(pending=pending):
                self.write(jobs.CHILD_STARTING, pending)
                before = self.snapshot()
                with jobs.preserving_metadata(self.new), \
                        mock.patch.object(jobs, '_pid_state', side_effect=AssertionError('invalid identity probe')):
                    self.assertTrue(jobs._child_pending(self.old))
                self.assertEqual(before, self.snapshot())

    def test_alive_unknown_and_readable_creation_mismatch_preserve_marker(self):
        for state in (common.ALIVE, common.UNKNOWN, common.DEAD):
            with self.subTest(state=state):
                before = self.snapshot()
                with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_pid_state', return_value=state) as probe:
                    self.assertEqual(jobs._child_pending(self.old), state != common.DEAD)
                    probe.assert_called_once_with(102, 9002)
                self.assertEqual(before, self.snapshot())

    def test_marker_change_during_identity_probe_holds(self):
        def race(pid, stamp):
            self.write(jobs.CHILD_STARTING, {'pid': 103, 'created': 9003, 'engine': 'codex'})
            return common.DEAD
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_pid_state', side_effect=race):
            self.assertTrue(jobs._child_pending(self.old))
        self.assertEqual(json.loads((self.folder / jobs.CHILD_STARTING).read_text())['pid'], 103)

    def test_preexisting_dead_writer_reservation_is_not_overwritten(self):
        path = Path(jobs._writer_path(self.cwd))
        path.parent.mkdir(exist_ok=True)
        path.write_text(json.dumps({'id': self.old, 'cwd': jobs._writer_cwd(self.cwd), 'pid': 101, 'created': 9001}))
        before = path.read_bytes(), path.stat().st_mtime_ns
        with jobs.preserving_metadata(self.new):
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(self.spec())
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_mode_cannot_be_removed_or_malformed_on_expected_preserving_runner(self):
        folder = self.store / self.new
        folder.mkdir()
        for mode in (None, False, 'true', 1):
            with self.subTest(mode=mode):
                spec = self.spec()
                spec['preserve_existing_metadata'] = mode
                (folder / 'job.json').write_text(json.dumps(spec))
                with mock.patch.object(jobs, '_claim_runner', side_effect=AssertionError('runner claimed')):
                    result = jobs.run_job(self.new, preserve_existing_metadata=True)
                self.assertEqual(result['status'], 'refused')

    def test_coordination_fingerprint_binds_preserving_mode(self):
        from twinlane import coordination
        plan = {'engine': 'claude', 'preserve_existing_metadata': True,
                'caller_intent': {'kind': 'code', 'preserve_existing_metadata': True}}
        original = coordination._fingerprint(plan)
        plan['preserve_existing_metadata'] = False
        with self.assertRaises(coordination.Refusal):
            coordination._fingerprint(plan)
        self.assertTrue(original)

    def test_child_cli_inherits_parent_preserving_mode_before_sweep(self):
        before = self.snapshot()
        with mock.patch.object(jobs, '_coordination_parent', return_value={'preserve_existing_metadata': True}), \
                mock.patch.object(jobs, 'plan_run', side_effect=jobs.Refusal(common.EXIT_REFUSED, 'synthetic route hold')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jobs.cmd_run(['--brief', 'synthetic'])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(before, self.snapshot())

    def test_cli_preserving_mode_does_not_sweep_old_handoff_before_planning(self):
        before = self.snapshot()
        with mock.patch.object(jobs, '_coordination_parent', return_value=None), \
                mock.patch.object(jobs, 'plan_run', side_effect=jobs.Refusal(common.EXIT_REFUSED, 'synthetic route hold')):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jobs.cmd_run(['--brief', 'synthetic', '--preserve-existing-metadata'])
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(before, self.snapshot())

    def test_slot_scan_keeps_foreign_dead_pending_marker_and_counts_both_engine_scans(self):
        self.write('job.json', {'id': self.old, 'cwd': str(self.root / 'other-task'),
                              'profile': 'intel', 'primary': {'engine': 'codex'}})
        self.write('status.json', {'status': 'running', 'engine': 'codex', 'pid': 101, 'pid_created': 9001})
        before = self.snapshot()
        # Engine/cwd filters must not grant permission to reclaim metadata of another task.
        with jobs.preserving_metadata(self.new), self.protect_old_store():
            self.assertEqual(jobs.running_count('claude'), 0)
            self.assertEqual(jobs.running_count('codex'), 0)
        self.assertEqual(before, self.snapshot())

    def test_slot_wait_preserves_old_metadata_across_alive_unknown_dead(self):
        (self.store / self.new).mkdir()
        before = self.snapshot()
        states = iter((common.ALIVE, common.UNKNOWN, common.DEAD))
        def probe(pid, stamp):
            return next(states, common.DEAD) if pid == 102 else common.DEAD
        with jobs.preserving_metadata(self.new), self.protect_old_store(), \
                mock.patch.object(jobs, '_pid_state', side_effect=probe), mock.patch.object(jobs.time, 'sleep') as sleep:
            self.assertTrue(jobs.acquire_slot(self.new, 'claude', 1, {'claude': {'max_parallel': 1}}))
        self.assertEqual(sleep.call_count, 2)
        self.assertEqual(before, self.snapshot())
        self.assertEqual(json.loads((self.store / self.new / 'status.json').read_text())['status'], 'running')

    def test_threaded_receipt_overlap_scan_propagates_preservation(self):
        before = self.snapshot()
        with jobs.preserving_metadata(self.new), self.protect_old_store():
            jobs._overlapping_jobs(self.new, common.now(), common.now())
        self.assertEqual(before, self.snapshot())

    def test_new_job_own_child_marker_lifecycle_remains_compatible(self):
        new_folder = self.store / self.new
        new_folder.mkdir()
        marker = new_folder / jobs.CHILD_STARTING
        marker.write_text('{"pid": 102, "created": 9002}')
        before = self.snapshot()
        with jobs.preserving_metadata(self.new), self.protect_old_store():
            jobs._clear_child_starting(self.new)
        self.assertFalse(marker.exists())
        self.assertEqual(before, self.snapshot())

    def test_two_concurrent_preserving_claimants_never_replace_one_another(self):
        before = self.snapshot()
        barrier = threading.Barrier(2)
        results = []
        def claim(jid):
            spec = dict(self.spec(), id=jid)
            barrier.wait()
            with jobs.preserving_metadata(jid):
                try:
                    jobs._claim_writer(spec)
                    results.append('admitted')
                except jobs.Refusal:
                    results.append('refused')
        with self.protect_old_store():
            threads = [threading.Thread(target=claim, args=(jid,)) for jid in (self.new, '20260101-000002-codex-abef')]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(5)
                self.assertFalse(thread.is_alive())
        self.assertCountEqual(results, ['admitted', 'refused'])
        self.assertEqual(before, self.snapshot())

    def test_background_argv_requires_persisted_preserving_mode_in_runner(self):
        folder = self.store / self.new
        folder.mkdir()
        (folder / 'job.json').write_text(json.dumps(self.spec()))
        before = self.snapshot()
        with self.protect_old_store(), mock.patch.object(common, 'spawn_detached_ex',
                return_value={'pid': 201, 'created': 9201, 'mode': 'wmi', 'job': False}) as spawn:
            with contextlib.redirect_stdout(io.StringIO()):
                jobs._start_background(self.new, dict(self.spec(), notes=[]), True)
        self.assertIn('--preserve-existing-metadata', spawn.call_args.args[0])
        self.assertEqual(before, self.snapshot())

    def test_legacy_coordination_fingerprint_remains_byte_identical(self):
        import hashlib
        from twinlane import coordination
        plan = {'engine': 'claude', 'caller_intent': {'kind': 'code'}}
        expected = hashlib.sha256(json.dumps(plan['caller_intent'], sort_keys=True, allow_nan=False).encode()).hexdigest()
        self.assertEqual(coordination._fingerprint(plan), expected)

    def test_full_inline_cli_allocates_new_identity_and_preserves_complete_old_folder(self):
        before = self.snapshot()
        plan = dict(self.spec(), brief_text='Synthetic source-only task', caller_brief='Synthetic source-only task',
                    brief_src='synthetic', notes=[], failover=False, resume=None, resume_jobs=[], continue_job=None)
        plan.pop('id')
        def finished(jid, spec):
            result = {'id': jid, 'status': 'done', 'attempts': [], 'notes': []}
            common.write_json(jobs._p(jid, 'result.json'), result)
            return result
        with self.protect_old_store(), mock.patch.object(jobs, '_coordination_parent', return_value=None), \
                mock.patch.object(jobs, 'plan_run', return_value=plan), \
                mock.patch.object(jobs, '_run_claimed', side_effect=finished):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jobs.cmd_run(['--brief', 'synthetic', '--preserve-existing-metadata'])
        self.assertEqual(rc, 0)
        self.assertEqual(before, self.snapshot())
        new_folders = [p for p in self.store.iterdir() if p != self.folder]
        self.assertEqual(len(new_folders), 1)
        spec = json.loads((new_folders[0] / 'job.json').read_text())
        self.assertIs(spec['preserve_existing_metadata'], True)
        self.assertTrue((new_folders[0] / 'result.json').exists())

    def test_real_plan_and_caller_intent_propagate_opt_in_but_legacy_stays_absent(self):
        brief = self.root / 'brief.md'
        brief.write_text('Synthetic task only')
        policy = common.load_policy()
        decision = {'engine': 'claude', 'profile': 'intel', 'reason': 'synthetic', 'refused': False}
        for enabled in (True, False):
            args = jobs._run_parser().parse_args(['--brief', str(brief), '--cd', self.cwd] +
                                                 (['--preserve-existing-metadata'] if enabled else []))
            with mock.patch.object(jobs, '_meter', return_value={}), \
                    mock.patch.object(jobs, '_route', return_value=decision), \
                    mock.patch.object(jobs, '_refresh_notice', return_value=None):
                plan = jobs.plan_run(args, policy)
            self.assertEqual(plan.get('preserve_existing_metadata', False), enabled)
            intent = jobs._coordination_intent(args, {'task': 'synthetic'})
            self.assertEqual(intent.get('preserve_existing_metadata', False), enabled)
            if not enabled:
                self.assertNotIn('preserve_existing_metadata', plan)
                self.assertNotIn('preserve_existing_metadata', intent)

    def test_allocation_collision_does_not_replace_preexisting_folder(self):
        plan = dict(self.spec(), brief_text='Synthetic', notes=[])
        before = self.snapshot()
        with self.protect_old_store():
            with self.assertRaises(RuntimeError):
                jobs._create_job(plan, self.old)
        self.assertEqual(before, self.snapshot())

    def test_ambiguous_background_launch_records_only_new_job_uncertainty(self):
        folder = self.store / self.new
        folder.mkdir()
        (folder / 'job.json').write_text(json.dumps(self.spec()))
        before = self.snapshot()
        error = common.DetachAmbiguous('synthetic ambiguous launch', pid=201, created=9201)
        with self.protect_old_store(), mock.patch.object(common, 'spawn_detached_ex', side_effect=error):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                rc = jobs._start_background(self.new, dict(self.spec(), notes=[]), True)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertFalse((folder / 'result.json').exists())
        self.assertEqual(before, self.snapshot())

    def test_old_handoff_age_is_not_a_new_consume_ttl(self):
        from twinlane import envhandoff
        for age in (1, 2000000000):
            folder = self.root / str(age)
            folder.mkdir()
            path = folder / jobs.HANDOFF
            path.write_text(json.dumps({'v': 1, 'env': {'SYNTHETIC': 'only'}}))
            os.utime(path, (age, age))
            with mock.patch.object(envhandoff, '_self_created', return_value=9201):
                data = envhandoff.claim_and_read(str(path))
            self.assertEqual(data['env'], {'SYNTHETIC': 'only'})
            self.assertFalse(path.exists())
            # Existing single-winner arbitration refuses any second claimant without a probe.
            with mock.patch.object(envhandoff, '_self_created', return_value=9202):
                with self.assertRaises(envhandoff.Lost):
                    envhandoff.claim_and_read(str(path))

    def test_exact_creation_stamp_is_required_for_dead_marker_classification(self):
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_pid_state') as probe:
            probe.side_effect = lambda pid, stamp: common.DEAD if (pid, stamp) == (102, 9002) else common.UNKNOWN
            self.assertFalse(jobs._child_pending(self.old))
            self.write(jobs.CHILD_STARTING, {'pid': 102, 'created': 9003})
            self.assertTrue(jobs._child_pending(self.old))

    def test_base_preserving_start_rejects_continuation_resume_and_baton_before_reads(self):
        for option in (['--continue-job', self.old], ['--resume', 'synthetic-session'], ['--baton', 'old-baton']):
            args = jobs._run_parser().parse_args(['--brief', 'synthetic', '--preserve-existing-metadata'] + option)
            with mock.patch.object(jobs, '_prior_job', side_effect=AssertionError('old job accessed')):
                with self.assertRaises(jobs.Refusal):
                    jobs.plan_run(args, common.load_policy())

    def test_preserving_transition_refused_before_existing_registry_mutation(self):
        from twinlane import coordination
        plan = {'engine': 'claude', 'preserve_existing_metadata': True,
                'coordination': {'task': 'synthetic', 'dispatch': 'new', 'actor': 'owner',
                                 'recipient': 'reviewer', 'parent': 'prior', 'receipt': 'accepted-old'},
                'caller_intent': {'kind': 'code', 'preserve_existing_metadata': True}}
        with mock.patch.object(coordination, '_read', side_effect=AssertionError('registry read')), \
                mock.patch.object(coordination, '_save', side_effect=AssertionError('registry changed')):
            with self.assertRaises(coordination.Refusal):
                coordination.allocate(plan, lambda jid: self.fail('allocated'))

    def test_codex_preserving_start_refuses_before_binary_cache_path(self):
        brief = self.root / 'brief.md'
        brief.write_text('Synthetic task only')
        args = jobs._run_parser().parse_args(['--brief', str(brief), '--cd', self.cwd,
                                             '--preserve-existing-metadata', '--engine', 'codex'])
        with mock.patch.object(jobs, '_meter', return_value={}), \
                mock.patch.object(jobs, '_route', return_value={'engine': 'codex', 'refused': False}), \
                mock.patch.object(jobs, 'engine_params', side_effect=AssertionError('binary path planning')):
            with self.assertRaises(jobs.Refusal):
                jobs.plan_run(args, common.load_policy())

    def test_preserving_native_identity_wrapper_rejects_coerced_or_unstamped_identity(self):
        for pid, stamp in ((True, 9001), ('101', 9001), (101, True), (101, '9001'), (101, None), (0, 9001)):
            with self.subTest(pid=pid, stamp=stamp), jobs.preserving_metadata(self.new), \
                    mock.patch.object(jobs, '_pid_state', REAL_PID_STATE), \
                    mock.patch.object(common, 'pid_state', side_effect=AssertionError('malformed identity probe')):
                self.assertEqual(jobs._pid_state(pid, stamp), common.UNKNOWN)

    def test_actual_coordination_adds_only_new_dispatch_and_claim_without_transition_changes(self):
        import copy
        from twinlane import coordination
        plan = dict(self.spec(), brief_text='Synthetic source-only', notes=[])
        plan.pop('id')
        plan['coordination'] = {'task': 'synthetic-task', 'dispatch': 'initial', 'actor': 'lead',
                                'recipient': 'owner', 'parent': None, 'receipt': None}
        # Disable native lineage only by mocking the read-only resolver, not by modifying env.
        with mock.patch.object(coordination, 'worker_ids', return_value=[]), \
                mock.patch.object(jobs, '_coordination_parent', return_value=None):
            first = coordination.allocate(plan, lambda jid: jobs._create_job(plan, jid))
            self.assertTrue(coordination.claim_launch(plan, first))
            original = copy.deepcopy(coordination._read('synthetic-task'))
            second = dict(plan, coordination={'task': 'synthetic-task', 'dispatch': 'second',
                            'actor': 'owner', 'recipient': 'next-worker', 'parent': 'initial', 'receipt': None})
            before = self.snapshot()
            with self.protect_old_store():
                jid = coordination.allocate(second, lambda jid: jobs._create_job(second, jid))
                self.assertTrue(coordination.claim_launch(second, jid))
                self.assertFalse(coordination.claim_launch(second, jid))
                spec = common.read_json(jobs._p(jid, 'job.json'))
                coordination.validate_runner(spec, jid)
            current = coordination._read('synthetic-task')
        self.assertEqual(before, self.snapshot())
        for field in ('owner', 'original_owner', 'coordinator', 'epoch', 'revision', 'receipts'):
            self.assertEqual(current[field], original[field])
        self.assertEqual(current['dispatches']['initial'], original['dispatches']['initial'])
        self.assertEqual(set(current['dispatches']), {'initial', 'second'})

    def test_preserving_runner_does_not_reclaim_existing_runner_lock(self):
        folder = self.store / self.new
        folder.mkdir()
        lock = folder / jobs.RUNNER_LOCK
        lock.write_text(f'{os.getpid()} 12345')
        before = lock.read_bytes(), lock.stat().st_mtime_ns
        with jobs.preserving_metadata(self.new), mock.patch.object(common, 'pid_maybe_alive', return_value=False):
            self.assertIsNotNone(jobs._claim_runner(self.new))
        self.assertEqual(before, (lock.read_bytes(), lock.stat().st_mtime_ns))

    def test_preserving_terminal_job_is_not_replayed_or_reclaimed(self):
        folder = self.store / self.new
        folder.mkdir()
        (folder / 'job.json').write_text(json.dumps(self.spec()))
        (folder / 'result.json').write_text('{"status":"done"}')
        with mock.patch.object(jobs, '_claim_runner', side_effect=AssertionError('terminal runner claimed')):
            result = jobs.run_job(self.new, preserve_existing_metadata=True)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual((folder / 'result.json').read_text(), '{"status":"done"}')

    def test_release_own_runner_checks_complete_stamp_not_only_pid(self):
        folder = self.store / self.new
        folder.mkdir()
        lock = folder / jobs.RUNNER_LOCK
        lock.write_text(f'{os.getpid()} 98765')
        with jobs.preserving_metadata(self.new):
            jobs._release_runner(self.new)
        self.assertTrue(lock.exists())

    def test_preserving_failover_cannot_reach_unqualified_codex_cache(self):
        with mock.patch.object(jobs, '_meter', side_effect=AssertionError('codex effects before refusal')):
            reason, _ = jobs.preflight(self.spec(), {'engine': 'codex'}, {}, False)
        self.assertIn('Claude only', reason)

    def test_missing_runner_identity_is_unknown_for_admission_and_capacity(self):
        (self.folder / jobs.CHILD_STARTING).unlink()  # fixture setup, not candidate cleanup
        self.write('status.json', {'status': 'running', 'engine': 'claude'})
        before = self.snapshot()
        with jobs.preserving_metadata(self.new), self.protect_old_store():
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(self.spec())
            self.assertEqual(jobs.running_count('claude'), 1)
        self.assertEqual(before, self.snapshot())

    def test_unreadable_status_and_unclassified_engine_hold_both_capacities(self):
        (self.folder / jobs.CHILD_STARTING).unlink()
        self.write('status.json', ['malformed'])
        before = self.snapshot()
        with jobs.preserving_metadata(self.new), self.protect_old_store():
            self.assertEqual(jobs.running_count('claude'), 1)
            self.assertEqual(jobs.running_count('codex'), 1)
        self.assertEqual(before, self.snapshot())

    def test_preserving_runner_pid_parser_is_bounded_and_exact(self):
        path = self.folder / 'runner.pid'
        for text in ('9' * 5000 + ' 9001', '101 ' + '9' * 5000, '101', '101 9001 extra',
                     '4294967296 9001', '101 18446744073709551616'):
            with self.subTest(text_length=len(text)):
                path.write_text(text)
                with jobs.preserving_metadata(self.new):
                    self.assertEqual(jobs._runner_pid(self.old), (None, None))
        path.write_text('101 134999999999999999')
        with jobs.preserving_metadata(self.new):
            self.assertEqual(jobs._runner_pid(self.old), (101, 134999999999999999))

    def test_preserving_meter_requires_existing_fresh_cache_without_refresh_or_binary_lease(self):
        from twinlane import meter
        cached = {'read_at': common.iso(common.now()),
                  'claude': {'status': 'ok', 'reading_at': common.iso(common.now())},
                  'codex': {'status': 'ok', 'reading_at': common.iso(common.now())}}
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_read_meter', None), \
                mock.patch.object(meter, '_cached_meter', return_value=cached), \
                mock.patch.object(meter, 'read', side_effect=AssertionError('refresh/lease')):
            self.assertEqual(jobs._meter({}, fresh=True), cached)
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_read_meter', None), \
                mock.patch.object(meter, '_cached_meter', return_value=None), \
                mock.patch.object(meter, 'read', side_effect=AssertionError('refresh/lease')):
            with self.assertRaises(jobs.Refusal):
                jobs._meter({})

    def test_threaded_after_meter_keeps_preserving_context_and_never_refreshes(self):
        from twinlane import meter
        cached = {'read_at': common.iso(common.now()), 'claude': {'reading_at': common.iso(common.now())},
                  'codex': {'reading_at': common.iso(common.now())}}
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_read_meter', None), \
                mock.patch.object(meter, '_cached_meter', return_value=cached), \
                mock.patch.object(meter, 'read', side_effect=AssertionError('thread lost preserving context')):
            after, timed_out = jobs._bounded_after_meter({}, 2)
        self.assertFalse(timed_out)
        self.assertEqual(after, cached)

    def test_malformed_parent_mode_refuses_before_any_global_sweep(self):
        before = self.snapshot()
        with mock.patch.object(jobs, '_coordination_parent', return_value={'preserve_existing_metadata': 'true'}):
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = jobs.cmd_run(['--brief', 'synthetic'])
        self.assertEqual(result, common.EXIT_REFUSED)
        self.assertEqual(before, self.snapshot())

    def test_expired_real_cache_cannot_launch_or_refresh(self):
        import datetime as dt
        from twinlane import meter
        payload = {'read_at': common.iso(common.now() - dt.timedelta(minutes=5)),
                   'claude': {'status': 'ok', 'reading_at': common.iso(common.now())},
                   'codex': {'status': 'ok', 'reading_at': common.iso(common.now())}}
        path = self.state / 'meter.json'
        path.write_text(json.dumps(payload))
        before = path.read_bytes(), path.stat().st_mtime_ns
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_read_meter', None), \
                mock.patch.object(meter, 'read', side_effect=AssertionError('automatic fresh evidence')):
            with self.assertRaises(jobs.Refusal):
                jobs._meter({'meter': {'cache_ttl_s': 120}})
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_abbreviated_preserving_flag_applies_before_sweep_and_planning_even_dry_run(self):
        for extra in ([], ['--dry-run']):
            with self.subTest(extra=extra):
                before = self.snapshot()
                def route_hold(args, policy):
                    self.assertTrue(jobs._PRESERVING_METADATA.get(), 'parsed preserving flag not active before planning')
                    raise jobs.Refusal(common.EXIT_REFUSED, 'synthetic policy hold')
                with self.protect_old_store(), mock.patch.object(jobs, '_coordination_parent', return_value=None), \
                        mock.patch.object(jobs, 'plan_run', side_effect=route_hold):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        result = jobs.cmd_run(['--brief', 'synthetic', '--preserve-existing-metadat'] + extra)
                self.assertEqual(result, common.EXIT_REFUSED)
                self.assertEqual(before, self.snapshot())

    def test_fresh_envelope_cannot_hide_newly_stale_underlying_allowance_at_queued_preflight(self):
        import datetime as dt
        from twinlane import meter
        now = common.now()
        payload = {'read_at': common.iso(now - dt.timedelta(seconds=30)),
                   'claude': {'status': 'ok', 'source': 'synthetic', 'max_pct': 10,
                              'reading_at': common.iso(now - dt.timedelta(seconds=1820)),
                              'extra_usage_enabled': False},
                   'codex': {'status': 'ok', 'source': 'synthetic', 'max_pct': 10,
                             'reading_at': common.iso(now), 'on_credits': False}}
        path = self.state / 'meter.json'
        path.write_text(json.dumps(payload))
        before = path.read_bytes(), path.stat().st_mtime_ns
        policy = common.load_policy()
        with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_read_meter', None), \
                mock.patch.object(common, 'now', return_value=now), \
                mock.patch.object(meter, 'read', side_effect=AssertionError('provider refresh')):
            reason, _ = jobs.preflight(self.spec(), {'engine': 'claude'}, policy, fresh=True)
        self.assertIsNotNone(reason, 'stale underlying allowance admitted from fresh cache envelope')
        self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_missing_or_unknown_engine_with_live_unknown_runner_holds_both_capacities(self):
        (self.folder / jobs.CHILD_STARTING).unlink()  # isolated fixture setup
        self.write('job.json', {'id': self.old, 'cwd': str(self.root / 'other-task'),
                               'profile': 'intel', 'primary': {'engine': 'claude'}})
        for engine in (None, 'unrecognized'):
            for state in (common.ALIVE, common.UNKNOWN):
                with self.subTest(engine=engine, state=state):
                    st = {'status': 'running', 'pid': 101, 'pid_created': 9001}
                    if engine is not None:
                        st['engine'] = engine
                    self.write('status.json', st)
                    before = self.snapshot()
                    with jobs.preserving_metadata(self.new), self.protect_old_store(), \
                            mock.patch.object(jobs, '_pid_state', return_value=state):
                        self.assertEqual(jobs.running_count('claude'), 1)
                        self.assertEqual(jobs.running_count('codex'), 1)
                    self.assertEqual(before, self.snapshot())

    def test_underlying_cache_age_boundary_uses_configured_limit_without_upgrade_or_write(self):
        import datetime as dt
        from twinlane import meter
        now = common.now().replace(microsecond=0)
        for age, expected in ((90, 'ok'), (91, 'unknown')):
            with self.subTest(age=age):
                payload = {'read_at': common.iso(now),
                    'claude': {'status': 'ok', 'reading_at': common.iso(now - dt.timedelta(seconds=age))},
                    'codex': {'status': 'unknown', 'reading_at': common.iso(now)}}
                path = self.state / 'meter.json'
                path.write_text(json.dumps(payload))
                before = path.read_bytes(), path.stat().st_mtime_ns
                with jobs.preserving_metadata(self.new), mock.patch.object(jobs, '_read_meter', None), \
                        mock.patch.object(common, 'now', return_value=now), \
                        mock.patch.object(meter, 'read', side_effect=AssertionError('provider refresh')):
                    result = jobs._meter({'claude': {'stale_reading_max_age_s': 90}})
                self.assertEqual(result['claude']['status'], expected)
                self.assertEqual(result['codex']['status'], 'unknown')
                self.assertEqual(before, (path.read_bytes(), path.stat().st_mtime_ns))

    def test_unknown_engine_with_definitively_dead_runner_and_child_remains_nonblocking(self):
        (self.folder / jobs.CHILD_STARTING).unlink()
        self.write('status.json', {'status': 'running', 'pid': 101, 'pid_created': 9001})
        before = self.snapshot()
        with jobs.preserving_metadata(self.new), self.protect_old_store():
            self.assertEqual(jobs.running_count('claude'), 0)
            self.assertEqual(jobs.running_count('codex'), 0)
        self.assertEqual(before, self.snapshot())


if __name__ == '__main__':
    unittest.main()
