"""Writer WIP custody and optional claims; disposable stores and stdlib providers only."""
import contextlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate before importing runtime modules
from twinlane import board, common, jobs, sessions, wip


WRITER_ID = '20261020-000000-codex-writer'  # a real job-folder name: the archive scan hides nothing else


class Wip(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='twinlane-wip-')
        self.addCleanup(test_support.cleanup_tempdir, temp)
        self.root = Path(temp.name)
        for name in ('STATE', 'JOBS', 'BATONS'):
            patch = mock.patch.object(common, name, str(self.root / name.lower()))
            patch.start()
            self.addCleanup(patch.stop)
        common.ensure_dirs()
        self.worktree = self.root / 'worktree'
        self.worktree.mkdir()
        self.policy = {'wip': {'max_writers': 5}}
        patch = mock.patch.object(sessions, 'collect_sessions', return_value={'rows': []})
        self.session_mock = patch.start()
        self.addCleanup(patch.stop)

    def job(self, jid=WRITER_ID, **fields):
        row = {'id': jid, 'profile': 'code', 'write': True, 'cwd': str(self.worktree),
               'status': 'running'}
        row.update(fields)
        common.write_json(jobs._p(jid, 'job.json'), row)
        common.write_json(jobs._p(jid, 'status.json'), {'status': row['status']})
        return row

    def provider(self, source=None, data=None):
        path = self.root / 'provider.py'
        path.write_text(source if source is not None else 'print(' + repr(json.dumps(data)) + ')\n',
                        encoding='utf-8')
        self.policy['wip']['claims_command'] = [sys.executable, '-B', str(path)]
        return path

    def claim(self, ident='lease', path=None, age=30):
        return {'worktree': str(path or self.worktree), 'id': ident, 'age_s': age}

    def board(self):
        with mock.patch.object(board, 'collect_meters', return_value={}), \
                mock.patch.object(board, 'collect_worktrees', return_value={}), \
                mock.patch.object(board, 'build_attention', return_value=[]), \
                mock.patch.object(board, 'build_signals', return_value=[]):
            return board.collect(policy=self.policy)

    def test_done_receipt_dead_engine_unknown_runner_releases_lane_in_both_paths(self):
        self.job()
        common.write_json(jobs._p(WRITER_ID, 'status.json'),
                          {'status': 'running', 'pid': 100, 'pid_created': 200,
                           'child_pid': 300, 'child_created': 400})
        common.write_json(jobs._p(WRITER_ID, 'result.json'), {'status': 'done'})
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN), \
                mock.patch.object(jobs, '_engine_state', return_value=common.DEAD):
            self.assertEqual(jobs.job_status(WRITER_ID), 'unknown')
            self.assertFalse(jobs._writer_job_busy(WRITER_ID))
            self.assertEqual(wip.collect(self.policy), [])
            row = board._job_row(WRITER_ID, common.now())
            self.assertEqual(wip.lanes_from([row], [], [], self.policy), [])
            self.assertEqual(self.board()['lanes']['busy'], 0)

    @unittest.skipUnless(common.IS_WIN, 'the process snapshot is the Windows identity fallback')
    def test_a_child_pid_recycled_by_a_process_we_cannot_open_frees_the_lane(self):
        """A 0.2-era writer stayed 'running' after its runner died because its child pid was recycled by a
        SYSTEM process this token cannot open: identity UNKNOWN held the lane. The snapshot reads the new
        process's creation time, so the old child is DEAD and the lane frees; nothing is killed."""
        runner, child, recorded = 53268, 15044, 134_365_977_693_017_836

        class Kernel:
            def __init__(self, snapshot):
                self.snapshot = snapshot

            def OpenProcess(self, access, inherit, pid):
                import ctypes
                ctypes.set_last_error(5 if pid == child else 87)  # denied, or no such process
                return None

            def snapshot_created(self, pid):
                return self.snapshot if pid == child else None

        self.job()
        common.write_json(jobs._p(WRITER_ID, 'status.json'),
                          {'status': 'running', 'pid': runner, 'pid_created': 134_365_977_565_978_873,
                           'child_pid': child, 'child_created': recorded})
        common.write_json(jobs._p(WRITER_ID, 'result.json'), {'status': 'timeout'})
        cases = ((134_367_896_988_950_621, False), (recorded, True), (None, True))
        for snapshot, busy in cases:
            with self.subTest(snapshot=snapshot),                     mock.patch.object(common, '_k32', return_value=Kernel(snapshot)),                     mock.patch.object(common, 'kill_tree', side_effect=AssertionError('kill')):
                self.assertIs(jobs._writer_job_busy(WRITER_ID), busy)
                self.assertEqual(len(wip.collect(self.policy)), int(busy))
                self.assertNotEqual(jobs.job_status(WRITER_ID), 'running')

    def test_live_queued_and_running_writers_count_in_both_paths(self):
        for status in ('queued', 'running'):
            with self.subTest(status=status):
                self.job(status=status)
                with mock.patch.object(jobs, 'job_status', return_value=status), \
                        mock.patch.object(jobs, '_engine_state', return_value=common.ALIVE):
                    direct = wip.collect(self.policy)
                    row = board._job_row(WRITER_ID, common.now())
                    self.assertEqual(wip.lanes_from([row], [], [], self.policy), direct)
                    self.assertEqual(len(direct), 1)
                    self.assertEqual(self.board()['lanes']['busy'], 1)

    def test_unknown_engine_still_counts(self):
        row = self.job()
        with mock.patch.object(jobs, '_engine_state', return_value=common.UNKNOWN):
            self.assertEqual(len(wip.lanes_from([row], [], [], self.policy)), 1)

    def test_done_receipt_with_unknown_engine_keeps_its_lane(self):
        row = self.job()
        common.write_json(jobs._p(WRITER_ID, 'result.json'), {'status': 'done'})
        with mock.patch.object(jobs, '_writer_job_busy', return_value=True):
            self.assertEqual(len(wip.lanes_from([row], [], [], self.policy)), 1)

    def test_just_admitted_job_without_pid_or_receipt_counts(self):
        row = self.job(status='queued')  # admission publishes this before its runner records a pid
        with mock.patch.object(jobs, '_writer_job_busy', return_value=False):
            self.assertEqual(len(wip.lanes_from([row], [], [], self.policy)), 1)

    def test_read_only_intel_does_not_count_but_admitted_code_profile_does(self):
        reader = self.job('reader', profile='intel', write=False)
        with mock.patch.object(jobs, '_writer_job_busy') as busy:
            self.assertEqual(wip.lanes_from([reader], [], [], self.policy), [])
        busy.assert_not_called()
        # create_job admits `write or profile == code` as a writer; the count must agree.
        code = self.job('code-no-flag')
        code.pop('write')
        with mock.patch.object(jobs, '_writer_job_busy', return_value=True):
            self.assertEqual(len(wip.lanes_from([code], [], [], self.policy)), 1)

    def test_writer_without_id_retains_unknown_custody(self):
        with mock.patch.object(jobs, '_writer_job_busy', side_effect=AssertionError('no id')):
            rows = wip.lanes_from([{'write': True}], [], [], self.policy)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['key'], 'job:None')

    def test_claims_merge_worktrees_and_jobs_and_ignore_activity(self):
        self.provider(data={'version': 1, 'claims': [self.claim(), self.claim('alias', age=10),
                                                    self.claim('unknown-age', age=None)]})
        job = self.job()
        self.session_mock.return_value = {'rows': [
            {'engine': 'codex', 'id': 'active', 'cwd': str(self.root), 'state': 'active', 'age_s': 0}]}
        # Broken baton data must not affect claims-based WIP admission.
        common.write_text(str(Path(common.BATONS) / 'broken.json'), '{')
        with mock.patch.object(jobs, 'job_status', return_value='running'), \
                mock.patch.object(jobs, '_writer_job_busy', return_value=True):
            rows = wip.collect(self.policy)
            other = [{'worktree': str(self.root), 'state': 'active', 'slug': 'other'}]
            direct = wip.lanes_from([job], other, self.session_mock.return_value['rows'], self.policy)
        self.assertEqual(rows, direct)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['key'], common.writer_key(str(self.worktree)))
        self.assertEqual(rows[0]['kinds'], ['claim', 'job'])
        self.assertEqual([c['id'] for c in rows[0]['claims']], ['lease', 'alias', 'unknown-age'])
        self.assertEqual(rows[0]['age_s'], 10)
        self.assertEqual(rows[0]['batons'], [])
        self.assertEqual(rows[0]['sessions'], [])
        self.session_mock.assert_not_called()

    def test_separate_claimed_worktrees_take_separate_lanes(self):
        self.provider(data={'version': 1, 'claims': [self.claim(), self.claim('other', self.root)]})
        self.assertEqual(len(wip.collect(self.policy)), 2)

    def test_claims_obey_count_main_checkout(self):
        (self.worktree / '.git').mkdir()
        self.policy['wip']['count_main_checkout'] = False
        self.provider(data={'version': 1, 'claims': [self.claim()]})
        self.assertEqual(wip.collect(self.policy), [])

    def test_same_claimed_worktree_is_admitted_at_cap(self):
        self.policy['wip']['max_writers'] = 1
        self.provider(data={'version': 1, 'claims': [self.claim()]})
        with wip.lock():
            wip.check(str(self.worktree), 'run', 'same', self.policy)
            with self.assertRaisesRegex(wip.WipError, 'queued: 1/1'):
                wip.check(str(self.root), 'run', 'other', self.policy)

    def test_claims_ignore_unreadable_sessions(self):
        self.provider(data={'version': 1, 'claims': []})
        self.session_mock.side_effect = OSError('unreadable')
        self.assertEqual(wip.collect(self.policy), [])
        self.session_mock.assert_not_called()

    def test_fetch_once_per_collect_board_and_cli_and_refresh_next_call(self):
        self.provider(data={'version': 1, 'claims': [self.claim()]})
        run = wip._run_provider
        with mock.patch.object(wip, '_run_provider', wraps=run) as provider:
            self.assertEqual(len(wip.collect(self.policy)), 1)
            self.assertEqual(provider.call_count, 1)
            self.assertEqual(self.board()['lanes']['busy'], 1)
            self.assertEqual(provider.call_count, 2)
            with mock.patch.object(common, 'load_policy', return_value=self.policy), \
                    contextlib.redirect_stdout(io.StringIO()) as output:
                self.assertEqual(wip.cli([]), 0)
            self.assertEqual(provider.call_count, 3)
        self.assertIn('(policy wip.max_writers; running write jobs + claims count)', output.getvalue())

    def test_empty_command_keeps_default_activity_mode_and_wording(self):
        self.policy['wip']['claims_command'] = []
        self.session_mock.return_value = {'rows': [
            {'engine': 'codex', 'id': 'active', 'cwd': str(self.worktree), 'state': 'active', 'age_s': 0}]}
        with mock.patch.object(common, 'load_policy', return_value=self.policy), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wip.cli([]), 0)
        self.assertIn('wip: 1/5', output.getvalue())
        self.assertIn('sessions active within 30m count', output.getvalue())

    def test_claims_json_keeps_existing_fields_and_adds_claims(self):
        self.provider(data={'version': 1, 'claims': [self.claim(age=None)]})
        with mock.patch.object(common, 'load_policy', return_value=self.policy), \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(wip.cli(['--json']), 0)
        data = json.loads(output.getvalue())
        row = data['lanes'][0]
        self.assertEqual(row['kinds'], ['claim'])
        self.assertEqual(row['claims'], [{'id': 'lease', 'age_s': None}])
        self.assertTrue({'jobs', 'batons', 'sessions', 'sources', 'worktree', 'key', 'age', 'age_s'} <= row.keys())

    def test_provider_process_failures_are_short_and_never_echo_output(self):
        cases = [('import sys; print("PRIVATE-OUTPUT"); print("PRIVATE-ERROR", file=sys.stderr); sys.exit(7)', 'nonzero exit'),
                 ('print("PRIVATE-BAD-JSON")', 'invalid JSON'),
                 ('import time; time.sleep(3)', 'timeout')]
        for source, reason in cases:
            with self.subTest(reason=reason):
                self.provider(source=source)
                self.policy['wip']['claims_timeout_s'] = .05 if reason == 'timeout' else 10
                with self.assertRaises(wip.WipError) as error:
                    wip.collect(self.policy)
                message = str(error.exception)
                self.assertTrue(message.startswith(f'writer custody unknown: claims provider failed: {reason}; '))
                self.assertIn('wip.claims_command is removed from', message)  # names the fix / rollback
                self.assertNotIn('PRIVATE', message)

    def _descendant(self, tail):
        """A provider that leaves a 30 s descendant holding its inherited stdout, then runs `tail`."""
        pidfile = self.root / 'descendant.pid'
        self.addCleanup(lambda: pidfile.exists() and common.kill_tree(int(pidfile.read_text() or 0)))
        self.provider(source=('import json, subprocess, sys, time\n'
                              'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])\n'
                              f'open({str(pidfile)!r}, "w").write(str(p.pid))\n' + tail))

    def test_descendant_holding_stdout_cannot_stall_a_finished_provider(self):
        self._descendant('print(json.dumps({"version": 1, "claims": []}))\n')
        started = time.monotonic()
        self.assertEqual(wip.collect(self.policy), [])
        self.assertLess(time.monotonic() - started, 8)  # a pipe drain would wait for the 30 s descendant

    def test_timeout_with_a_descendant_is_bounded(self):
        self._descendant('time.sleep(30)\n')
        self.policy['wip']['claims_timeout_s'] = 1
        started = time.monotonic()
        with self.assertRaisesRegex(wip.WipError, 'claims provider failed: timeout;'):
            wip.collect(self.policy)
        self.assertLess(time.monotonic() - started, 10)

    def test_oversized_stdout_fails_closed_and_stderr_is_never_buffered(self):
        self.provider(source=f'import sys; sys.stdout.write(" " * {wip.CLAIMS_MAX_BYTES + 10})\n')
        with self.assertRaisesRegex(wip.WipError, 'claims provider failed: output too large;'):
            wip.collect(self.policy)
        self.provider(source=('import json, sys\n'
                              f'sys.stderr.write("x" * {8 * wip.CLAIMS_MAX_BYTES})\n'
                              'print(json.dumps({"version": 1, "claims": []}))\n'))
        self.assertEqual(wip.collect(self.policy), [])

    def test_provider_spawn_error_fails_closed(self):
        self.policy['wip']['claims_command'] = [str(self.root / 'missing-program')]
        with self.assertRaisesRegex(wip.WipError, 'claims provider failed: spawn error'):
            wip.collect(self.policy)

    def test_unusable_temp_dir_is_a_wip_error_and_the_board_still_renders(self):
        self.provider(data={'version': 1, 'claims': []})
        with mock.patch('tempfile.TemporaryFile', side_effect=PermissionError('read-only temp dir')):
            with self.assertRaisesRegex(wip.WipError, 'claims provider failed: spawn error;'):
                wip.collect(self.policy)
            data = self.board()
        self.assertEqual(data['lanes']['busy'], '?')
        self.assertIn('writers ?/5', data['capacity'])

    def test_unreadable_provider_output_is_a_wip_error(self):
        self.provider(data={'version': 1, 'claims': []})
        real = tempfile.TemporaryFile
        for failing in ('seek', 'read'):
            class Broken:  # a temp file whose read-back fails (a device or quota error)
                def __init__(self):
                    self.handle = real()

                def fileno(self):
                    return self.handle.fileno()

                def seek(self, *a):
                    if failing == 'seek':
                        raise OSError('device error')
                    return self.handle.seek(*a)

                def read(self, *a):
                    if failing == 'read':
                        raise OSError('device error')
                    return self.handle.read(*a)

                def __enter__(self):
                    return self

                def __exit__(self, *exc):
                    self.handle.close()
            with self.subTest(failing=failing), mock.patch('tempfile.TemporaryFile', side_effect=Broken):
                with self.assertRaisesRegex(wip.WipError, 'claims provider failed: read error;'):
                    wip.collect(self.policy)

    def test_invalid_provider_shapes_fail_closed(self):
        claim = self.claim()
        cases = [[], {'version': 2, 'claims': []}, {'version': True, 'claims': []},
                 {'version': 1, 'claims': {}}, {'version': 1, 'claims': [None]},
                 {'version': 1, 'claims': [{**claim, 'worktree': 'relative/path'}]},
                 {'version': 1, 'claims': [{**claim, 'id': 7}]},
                 {'version': 1, 'claims': [{k: v for k, v in claim.items() if k != 'age_s'}]},
                 {'version': 1, 'claims': [{**claim, 'age_s': 'old'}]},
                 {'version': 1, 'claims': [{**claim, 'age_s': True}]},
                 {'version': 1, 'claims': [{**claim, 'age_s': float('nan')}]},
                 {'version': 1, 'claims': [{**claim, 'age_s': float('inf')}]}]
        for data in cases:
            with self.subTest(data=data):
                self.provider(data=data)
                with self.assertRaisesRegex(wip.WipError, 'writer custody unknown: claims provider failed:'):
                    wip.collect(self.policy)

    def test_invalid_command_and_timeout_fail_closed(self):
        for command in ('program', [1]):
            self.policy['wip']['claims_command'] = command
            with self.subTest(command=command), self.assertRaisesRegex(wip.WipError, 'invalid command'):
                wip.collect(self.policy)
        self.provider(data={'version': 1, 'claims': []})
        for timeout in (0, -1, None, '10', True, float('nan'), float('inf')):
            self.policy['wip']['claims_timeout_s'] = timeout
            with self.subTest(timeout=timeout), self.assertRaisesRegex(wip.WipError, 'invalid timeout'):
                wip.collect(self.policy)

    def test_failing_provider_blocks_writer_admission_only(self):
        self.provider(source='print("bad JSON")')
        # Writer admission fails closed and names the fix.
        with self.assertRaisesRegex(wip.WipError, r'claims provider failed: invalid JSON; .*wip\.claims_command'):
            wip.check(str(self.worktree), 'run', 'writer', self.policy)
        with mock.patch.object(common, 'load_policy', return_value=self.policy), \
                mock.patch.object(jobs, '_admitted_job', return_value='admitted') as admitted:
            with self.assertRaises(jobs.Refusal) as refusal:
                jobs.create_job({'cwd': str(self.worktree), 'write': True, 'profile': 'code', 'name': 'w'})
            self.assertEqual(refusal.exception.code, common.EXIT_REFUSED)
            admitted.assert_not_called()
            # A read-only (intel) dispatch never enters writer admission.
            self.assertEqual(jobs.create_job({'cwd': str(self.worktree), 'write': False, 'profile': 'intel'}),
                             'admitted')
        # The board still renders, with an unknown writer count and a signal.
        data = self.board()
        self.assertEqual(data['lanes']['busy'], '?')
        self.assertIn('writers ?/5', data['capacity'])
        self.assertTrue(any('writer count unknown' in str(s) for s in data['signals']))
        # Status still reports and exits 0.
        from twinlane import cli, meter
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(common, 'load_policy', return_value=self.policy), \
                mock.patch.object(meter, 'cached_observation', return_value={}), \
                mock.patch.object(meter, 'format_line', return_value='meter'), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            self.assertEqual(cli.status([]), 0)
        self.assertIn('writers ?/5', out.getvalue())
        self.assertIn('writer count unknown', err.getvalue())

    def test_claims_mode_baton_needs_no_lane_at_cap_or_on_provider_failure(self):
        from twinlane import baton
        others = [self.root / f'other{i}' for i in range(5)]
        self.provider(data={'version': 1, 'claims': [self.claim(f'c{i}', p) for i, p in enumerate(others)]})
        self.assertEqual(len(wip.collect(self.policy)), 5)
        # A writer into a new worktree queues at the cap; a baton op does not (batons hold no lane here).
        with self.assertRaisesRegex(wip.WipError, 'queued: 5/5'):
            wip.check(str(self.worktree), 'run', 'writer', self.policy)
        wip.save_queue([])
        self.assertIsNone(wip.check(str(self.worktree), 'baton', 'b', self.policy))
        with mock.patch.object(common, 'load_policy', return_value=self.policy):
            self.assertEqual(baton.new('b', 'goal', cwd=str(self.worktree))['slug'], 'b')
        self.assertEqual(wip.queue(), [])
        # A failing provider still blocks writers but never baton ops, which do not run it.
        self.provider(source='print("bad JSON")')
        with self.assertRaisesRegex(wip.WipError, 'claims provider failed'):
            wip.check(str(self.worktree), 'run', 'writer', self.policy)
        with mock.patch.object(wip, '_run_provider', side_effect=AssertionError('provider ran')):
            self.assertIsNone(wip.check(str(self.worktree), 'baton', 'b', self.policy))

    def test_activity_mode_baton_still_queues_at_cap(self):
        for i in range(5):
            path = self.root / f'busy{i}'
            path.mkdir()
            self.job(f'20261020-00000{i}-codex-w{i}', cwd=str(path))
        with mock.patch.object(jobs, 'job_status', return_value='running'), \
                mock.patch.object(jobs, '_engine_state', return_value=common.ALIVE):
            self.assertEqual(len(wip.collect(self.policy)), 5)
            with self.assertRaisesRegex(wip.WipError, 'queued: 5/5'):
                wip.check(str(self.worktree), 'baton', 'b', self.policy)


if __name__ == '__main__':
    unittest.main()
