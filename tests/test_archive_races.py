"""Mixed-version and archive interleavings, using disposable homes only."""
from contextlib import contextmanager, redirect_stdout
import datetime as dt
import os
import io
from pathlib import Path
import threading
import unittest
from unittest import mock

import test_archive
from twinlane import archive, baton, common, jobs, worktrees


class ArchiveRaceTests(unittest.TestCase):
    setUp = test_archive.ArchiveTests.setUp
    job = test_archive.ArchiveTests.job
    call = test_archive.ArchiveTests.call

    def move_directly(self, jid, folder):
        destination = self.home / 'jobs' / 'archive' / '2026-09' / jid
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.replace(folder, destination)
        return destination

    def test_old_runner_claim_blocks_new_claim(self):
        jid, folder = self.job()
        real_lock = common.file_lock
        def immediate(path, *args, **kwargs):
            return real_lock(path, 0)
        with real_lock(str(folder / 'runner.claim'), 0), \
                mock.patch.object(common, 'file_lock', side_effect=immediate):
            with self.assertRaises(TimeoutError):
                jobs._claim_runner(jid)
        self.assertFalse((folder / jobs.RUNNER_LOCK).exists())

    def test_inner_locks_closed_before_replace_and_order_is_outer_first(self):
        jid, folder = self.job()
        held, seen = [], []
        real_lock, replace = common.file_lock, os.replace
        @contextmanager
        def tracked(path, *args, **kwargs):
            if Path(path).name in ('status.lock', 'runner.claim'):
                self.assertTrue(any(p.endswith(jid + '.archive') for p in held))
                self.assertEqual(Path(path).parent, folder)
            with real_lock(path, *args, **kwargs):
                held.append(path)
                seen.append(path)
                try:
                    yield
                finally:
                    held.remove(path)
        def move(source, target):
            if source == str(folder):
                self.assertTrue(any(p.endswith(jid + '.archive') for p in held))
                self.assertFalse(any(Path(p).name in ('status.lock', 'runner.claim') for p in held))
            return replace(source, target)
        with mock.patch.object(common, 'file_lock', side_effect=tracked), \
                mock.patch.object(archive.os, 'replace', side_effect=move):
            jobs._claim_runner(jid)
            jobs._release_runner(jid)
            jobs._merge_status(jid, checked=True)
            self.assertTrue(archive._move(jid, 14, False)['archived'])
        self.assertTrue(any(Path(p).name == 'status.lock' for p in seen))

    def test_any_nonquiescent_job_refuses_entire_apply(self):
        good, folder = self.job()
        for condition in ('running', 'missing-receipt', 'starting', 'runner-lock', 'unknown'):
            with self.subTest(condition=condition):
                jid, live = self.job(status='running' if condition == 'running' else 'done')
                if condition == 'missing-receipt':
                    (live / 'result.json').unlink()
                elif condition == 'starting':
                    common.write_json(str(live / jobs.CHILD_STARTING), {'engine': 'codex'})
                elif condition == 'runner-lock':
                    (live / jobs.RUNNER_LOCK).write_text('1234 1')
                elif condition == 'unknown':
                    common.write_json(str(live / 'status.json'), {'pid': 1234, 'pid_created': 1})
                with mock.patch.object(common, 'pid_state', return_value=common.UNKNOWN):
                    preview_code, preview = self.call()
                    code, data = self.call('--apply')
                self.assertEqual(preview_code, 0)
                self.assertTrue(preview['apply_blocked'])
                self.assertIn(good, [r['id'] for r in preview['candidates']])
                self.assertEqual(code, 3)
                self.assertEqual(data['moved'], [])
                self.assertTrue(folder.exists())
                for p in live.iterdir():
                    p.unlink()
                live.rmdir()

    def test_a_stale_running_status_beside_a_terminal_receipt_is_quiescent_only_when_dead(self):
        good, folder = self.job()
        jid, live = self.job(state={'status': 'running', 'pid': 1234, 'pid_created': 1})
        with mock.patch.object(common, 'pid_state', return_value=common.DEAD):
            self.assertEqual(archive.quiescence(), [])
        for state in (common.ALIVE, common.UNKNOWN):
            with self.subTest(state=state), mock.patch.object(common, 'pid_state', return_value=state):
                self.assertEqual([r['id'] for r in archive.quiescence()], [jid])
        common.write_json(str(live / 'status.json'), {'status': 'starting'})
        with mock.patch.object(common, 'pid_state', return_value=common.DEAD):
            self.assertEqual([r['reason'] for r in archive.quiescence()], ['job status starting'])

    def test_old_inner_holder_prevents_move(self):
        jid, folder = self.job()
        for name in ('runner.claim', 'status.lock'):
            with self.subTest(name=name), common.file_lock(str(folder / name), 0):
                with self.assertRaises(TimeoutError):
                    archive._move(jid, 14, False)
                self.assertTrue(folder.exists())

    def test_marker_only_live_folder_does_not_shadow_archive(self):
        jid, folder = self.job()
        archived = self.move_directly(jid, folder)
        folder.mkdir()
        (folder / jobs.COLLECTED).write_text('marker')
        self.assertEqual(jobs.job_dir(jid), str(archived))

    def marker_race(self, stop=False):
        jid, folder = self.job(read=False)
        resolve = jobs.job_dir
        moved = []
        def resolve_then_move(job_id):
            path = resolve(job_id)
            if not moved:
                moved.append(self.move_directly(jid, folder))
            return path
        with mock.patch.object(jobs, 'job_dir', side_effect=resolve_then_move):
            if stop:
                with redirect_stdout(io.StringIO()):
                    self.assertEqual(jobs.cmd_stop([jid, '--json']), 0)
            else:
                jobs.mark_collected(jid)
        self.assertFalse(folder.exists())
        self.assertTrue((moved[0] / (jobs.STOP_REQUESTED if stop else jobs.COLLECTED)).is_file())

    def test_collection_during_move_resolves_again_without_mkdir(self):
        self.marker_race()

    def test_stop_during_move_resolves_again_without_mkdir(self):
        self.marker_race(stop=True)

    def test_collection_resolution_holds_outer_lock(self):
        jid, _ = self.job()
        resolve, lock = jobs.job_dir, common.file_lock
        def checked(job_id):
            with self.assertRaises(TimeoutError):
                with lock(str(self.home / 'state' / 'job-locks' / (jid + '.archive')), 0):
                    pass
            return resolve(job_id)
        with mock.patch.object(jobs, 'job_dir', side_effect=checked):
            jobs.mark_collected(jid)
            jobs._request_stop(jid)

    def test_missing_job_is_never_recreated_by_marker_or_lifecycle_write(self):
        jid, folder = self.job()
        for path in folder.iterdir():
            path.unlink()
        folder.rmdir()
        jobs.mark_collected(jid)
        with self.assertRaises(FileNotFoundError):
            jobs._request_stop(jid)
        with self.assertRaises(FileNotFoundError):
            jobs._merge_status(jid, checked=True)
        self.assertFalse(folder.exists())

    @unittest.skipUnless(common.IS_WIN, 'Windows refuses directory rename with open inner locks')
    def test_old_straggler_after_inner_probe_leaves_folder_in_place(self):
        jid, folder = self.job()
        replace = os.replace
        def straggler(source, destination):
            if source == str(folder):
                with common.file_lock(str(folder / 'runner.claim'), 0):
                    return replace(source, destination)
            return replace(source, destination)
        with mock.patch.object(archive.os, 'replace', side_effect=straggler):
            code, data = self.call('--apply')
        self.assertEqual(code, common.EXIT_FAILED)
        self.assertEqual(data['moved'], [])
        self.assertTrue(folder.exists())

    def test_baton_dependency_write_excludes_archive(self):
        for mode in ('waiting', 'flight', 'owner'):
            jid, folder = self.job()
            record = baton.empty_baton('sample-' + mode)
            if mode == 'waiting':
                record['waiting_on'] = 'job:' + jid
            elif mode == 'flight':
                record['progress']['in_flight'] = [{'what': 'sample', 'job': jid}]
            else:
                record['jobs'] = [{'id': jid, 'status': 'running'}]
            write = common.write_json
            def interleave(path, data, *args, **kwargs):
                if path == baton.baton_path(record['slug']):
                    outcome = []
                    def attempt():
                        try:
                            outcome.append(archive._move(jid, 14, False))
                        except TimeoutError:
                            outcome.append('locked')
                    contender = threading.Thread(target=attempt)
                    contender.start()
                    contender.join(2)
                    self.assertFalse(contender.is_alive())
                    self.assertEqual(outcome, ['locked'])
                    self.assertTrue(folder.exists())
                return write(path, data, *args, **kwargs)
            with mock.patch.object(common, 'write_json', side_effect=interleave):
                baton.save(record)

    def test_normalized_job_entry_is_locked_before_publish(self):
        # An entry with an extra field only becomes a live dependency after normalization.
        jid, folder = self.job()
        record = baton.new('normalize-first', 'sample')
        record['jobs'] = [{'id': jid, 'status': 'running', 'extra': 'legacy'}]
        locked = []
        real = jobs.archive_lock
        @contextmanager
        def tracked(job_id, *args, **kwargs):
            with real(job_id, *args, **kwargs):
                locked.append(job_id)
                yield
        with mock.patch.object(jobs, 'archive_lock', side_effect=tracked):
            baton.save(record)
        self.assertIn(jid, locked)

    def test_new_baton_after_move_cannot_admit_dependency(self):
        jid, folder = self.job()
        self.move_directly(jid, folder)
        with self.assertRaises(baton.BatonError):
            baton.new('late-reference', 'sample', waiting_on='job:' + jid)
        self.assertFalse(Path(baton.baton_path('late-reference')).exists())

    def test_index_fsync_failure_keeps_move_and_retry_deduplicates(self):
        jid, folder = self.job()
        with mock.patch.object(archive.os, 'fsync', side_effect=OSError('fixture')):
            row = archive._move(jid, 14, False)
        self.assertFalse(folder.exists())
        self.assertTrue(row['archived'])
        self.assertEqual(row['warning'], 'index entry uncertain')
        self.assertEqual(jobs.job_status(jid), 'done')
        archive._move(jid, 14, False)
        index = self.home / 'jobs' / 'archive' / 'index.jsonl'
        self.assertEqual(len(index.read_text().splitlines()), 1)

    def test_index_open_failure_keeps_moved_evidence_lookup(self):
        jid, folder = self.job()
        read_open = open
        def denied(path, *args, **kwargs):
            if os.fspath(path).endswith('index.jsonl'):
                raise PermissionError('fixture')
            return read_open(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=denied):
            code, data = self.call('--apply')
        self.assertEqual(code, common.EXIT_FAILED)
        self.assertEqual(data['moved'][0]['warning'], 'index entry uncertain')
        self.assertFalse(folder.exists())
        self.assertEqual(jobs.job_status(jid), 'done')

    def test_nonmatching_directory_blocks_writer_and_marks_overlap_uncertain(self):
        (self.home / 'jobs' / 'legacy-worker').mkdir()
        spec = {'id': '20261020-120000-codex-abcd', 'cwd': str(self.home), 'profile': 'code'}
        with mock.patch.object(common, 'git', return_value=(1, '')):
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(spec)
        _, uncertain = jobs._overlapping_jobs(spec['id'], self.now - dt.timedelta(hours=1), self.now)
        self.assertTrue(uncertain)

    def test_nonmatching_directory_is_worktree_usage_problem(self):
        (self.home / 'jobs' / 'legacy-worker').mkdir()
        from twinlane import sessions
        with mock.patch.object(sessions, 'collect_sessions', return_value={'rows': []}):
            usage = worktrees.usage_index(str(self.home))
        self.assertTrue(any('legacy-worker' in p for p in usage['problems']))


if __name__ == '__main__':
    unittest.main()
