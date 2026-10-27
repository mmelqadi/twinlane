"""Disposable folder/SQLite fixtures; no engines, live home, or custody changes."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from contextlib import closing, redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_support  # noqa: E402
from twinlane import board, common, guard, jobs, jobs_index  # noqa: E402


class JobsIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='twinlane-index-')
        self.addCleanup(test_support.cleanup_tempdir, self.tmp)
        for attr in ('STATE', 'JOBS', 'JOBS_INDEX_ENABLED'):
            old = getattr(common, attr)
            self.addCleanup(setattr, common, attr, old)
        common.STATE = os.path.join(self.tmp.name, 'state')
        common.JOBS = os.path.join(self.tmp.name, 'jobs')
        os.makedirs(common.STATE)
        os.makedirs(common.JOBS)
        common.JOBS_INDEX_ENABLED = True
        self.now = common.now()
        clock = mock.patch.object(common, 'now', return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)
        pid = mock.patch.object(common, 'pid_state', side_effect=lambda p, c=None:
                                {101: common.ALIVE, 102: common.UNKNOWN}.get(p, common.DEAD))
        pid.start()
        self.addCleanup(pid.stop)

    def add_job(self, tag, status='running', engine='codex', pid=101, receipt=False, age=20):
        at = common.iso(self.now - dt.timedelta(seconds=age))
        jid = (self.now - dt.timedelta(seconds=age)).strftime('%Y%m%d-%H%M%S') + f'-{engine}-{tag}'
        folder = Path(common.JOBS, jid)
        folder.mkdir()
        spec = dict(id=jid, name=tag, kind='code', engine=engine, cwd=self.tmp.name,
                    writer_root=self.tmp.name, brief_text='same brief', profile='intel', created_at=at,
                    primary=dict(engine=engine, model='fixture-model', tier=None, timeout_s=3600))
        common.write_json(str(folder / 'job.json'), spec)
        common.write_json(str(folder / 'status.json'), dict(status=status, engine=engine, pid=pid,
                          pid_created=1, started_at=at, updated_at=at))
        if receipt:
            common.write_json(str(folder / 'result.json'), dict(id=jid, status=status, engine=engine,
                              started_at=at, ended_at=at, duration_s=0))
        return jid, spec

    def build(self):
        # Test only the storage algorithm here. Public API/CLI worker refusal is
        # independently asserted below; inherited worker markers remain intact.
        return jobs_index._rebuild()

    def views(self):
        compact = board.collect_jobs(self.now, False, 1)
        # An untrusted cache scans old history fully. Compare the displayed
        # rows; private history/scanned counters may conservatively increase.
        compact = {k: compact[k] for k in ('live', 'finished', 'unreadable')}
        return (jobs.running_count('codex'), jobs.running_count('claude'),
                jobs.live_duplicate(self.plan), board.collect_jobs(self.now, True, 5),
                compact,
                [r for r in jobs.list_jobs(limit=50, live_only=True)
                 if r['status'] in board.LIVE])

    def mixed(self):
        self.live, self.plan = self.add_job('live')
        self.add_job('unknown', pid=102)
        self.add_job('queued', status='queued')
        self.add_job('lost', pid=999)
        self.add_job('claude-live', engine='claude')
        self.add_job('finished', status='done', receipt=True)
        self.add_job('failed', status='failed', receipt=True)
        self.add_job('old', status='done', receipt=True, age=86400 * 10)
        surv, _ = self.add_job('surviving', status='failed', receipt=True)
        common.write_json(str(Path(common.JOBS, surv, jobs.SURVIVOR)),
                          dict(engine='codex', pid=102, created=1))
        pending, _ = self.add_job('pending', status='failed', receipt=True)
        common.write_json(str(Path(common.JOBS, pending, jobs.TREE_UNRESOLVED)), dict(engine='codex'))
        self.build()
        common.JOBS_INDEX_ENABLED = False
        expected = self.views()
        common.JOBS_INDEX_ENABLED = True
        return expected

    def sql(self, statement, params=()):
        with closing(sqlite3.connect(jobs_index.path())) as conn, conn:
            conn.execute(statement, params)

    def test_off_no_sqlite_or_database_for_reads_writes(self):
        jid, self.plan = self.add_job('off')
        for policy in ({}, {'jobs_index': {'enabled': False}},
                       {'jobs_index': {'enabled': 1}}, {'jobs_index': {'enabled': 'true'}}):
            with self.subTest(policy=policy), mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('SQLite OFF')):
                common.configure_jobs_index(policy)
                before = set(os.listdir(common.STATE))
                jobs._write_job_json(jid, 'status.json', dict(status='running', engine='codex', pid=101))
                self.views()
                jobs_index.update(jid)
                self.assertIsNone(jobs_index.read())
                # Existing archive/status locks are folder behavior, not new index files.
                self.assertFalse(any(name.startswith('jobs.db') for name in os.listdir(common.STATE)))
                self.assertEqual(before & {'jobs.db'}, set())
        common.configure_jobs_index({'jobs_index': {'enabled': True}})
        self.assertTrue(common.JOBS_INDEX_ENABLED)

    def test_missing_database_read_does_not_create_it(self):
        self.add_job('live')
        with mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('missing DB')):
            self.assertEqual(jobs.running_count('codex'), 1)
        self.assertFalse(Path(jobs_index.path()).exists())
        self.assertFalse(Path(jobs_index.path() + '.lock').exists())

    def test_fresh_index_matches_all_folder_views(self):
        expected = self.mixed()
        self.assertEqual(self.views(), expected)
        indexed_board = board.collect_jobs(self.now, False, 1)
        common.JOBS_INDEX_ENABLED = False
        self.assertEqual(indexed_board, board.collect_jobs(self.now, False, 1))
        common.JOBS_INDEX_ENABLED = True
        with closing(sqlite3.connect(jobs_index.path())) as conn:
            self.assertEqual(conn.execute('PRAGMA journal_mode').fetchone()[0], 'wal')
            self.assertEqual(conn.execute('SELECT count(*) FROM jobs').fetchone()[0], 10)

    def test_missing_live_row_reads_folder(self):
        expected = self.mixed()
        self.sql('DELETE FROM jobs WHERE id=?', (self.live,))
        self.assertEqual(self.views(), expected)

    def test_stale_nonterminal_row_reads_folder(self):
        expected = self.mixed()
        self.sql("UPDATE jobs SET engine='claude', pid=999, status='queued'")
        self.assertEqual(self.views(), expected)

    def test_dirty_falls_back(self):
        expected = self.mixed()
        jobs_index._mark_dirty()
        with mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('dirty DB opened')):
            self.assertEqual(self.views(), expected)

    def test_corrupt_falls_back(self):
        expected = self.mixed()
        Path(jobs_index.path()).write_bytes(b'not SQLite')
        self.assertIsNone(jobs_index.read())
        self.assertEqual(self.views(), expected)

    def test_locked_database_falls_back(self):
        expected = self.mixed()
        with closing(sqlite3.connect(jobs_index.path())) as conn:
            conn.execute('PRAGMA locking_mode=EXCLUSIVE')
            conn.execute('BEGIN EXCLUSIVE')
            conn.execute('UPDATE jobs SET pid=pid')
            self.assertIsNone(jobs_index.read())
            self.assertEqual(self.views(), expected)

    def test_schema_version_falls_back(self):
        expected = self.mixed()
        self.sql('PRAGMA user_version=999')
        self.assertIsNone(jobs_index.read())
        self.assertEqual(self.views(), expected)

    def test_schema_columns_fall_back(self):
        expected = self.mixed()
        self.sql('ALTER TABLE jobs ADD COLUMN foreign_column TEXT')
        self.assertIsNone(jobs_index.read())
        self.assertEqual(self.views(), expected)

    def test_unreadable_index_falls_back(self):
        expected = self.mixed()
        with mock.patch.object(jobs_index, '_connect', side_effect=PermissionError('fixture')):
            self.assertEqual(self.views(), expected)

    def test_index_lock_busy_falls_back(self):
        expected = self.mixed()
        with mock.patch.object(jobs_index, '_lock', side_effect=TimeoutError('fixture lock')):
            self.assertEqual(self.views(), expected)

    def test_terminal_without_result_is_read(self):
        jid, _ = self.add_job('terminal', status='done', receipt=True)
        self.build()
        Path(common.JOBS, jid, 'result.json').unlink()
        common.write_json(str(Path(common.JOBS, jid, 'status.json')),
                          dict(status='running', engine='codex', pid=102, pid_created=1))
        with mock.patch.object(common, 'read_json', wraps=common.read_json) as read:
            self.assertEqual(jobs.running_count('codex'), 1)
            self.assertTrue(any(str(c.args[0]).endswith('status.json') for c in read.call_args_list))

    def test_terminal_status_changed_after_index_is_read(self):
        jid, _ = self.add_job('terminal', status='done', receipt=True)
        self.build()
        common.write_json(str(Path(common.JOBS, jid, 'status.json')),
                          dict(status='running', engine='codex', pid=102, pid_created=1, child_pid=102))
        self.assertIsNone(jobs_index.terminal(jid, jobs_index.read()))
        self.assertEqual(board.collect_jobs(self.now, True, 5)['live'][0]['status'], 'unknown')

    def test_terminal_result_changed_after_index_is_read(self):
        jid, _ = self.add_job('terminal', status='done', receipt=True)
        self.build()
        common.write_json(str(Path(common.JOBS, jid, 'result.json')), dict(status='failed'))
        self.assertIsNone(jobs_index.terminal(jid, jobs_index.read()))

    def test_terminal_markers_always_read_even_if_added_later(self):
        for marker in jobs_index.MARKERS:
            with self.subTest(marker=marker):
                jid, _ = self.add_job(marker.replace('.', '-'), status='done', receipt=True)
                self.build()
                common.write_json(str(Path(common.JOBS, jid, marker)), dict(engine='codex', pid=102, created=1))
                self.assertIsNone(jobs_index.terminal(jid, jobs_index.read()))
        self.assertEqual(jobs.running_count('codex'), 3)

    def test_terminal_nonterminal_status_is_not_skip_evidence(self):
        jid, _ = self.add_job('terminal', status='done', receipt=True)
        common.write_json(str(Path(common.JOBS, jid, 'status.json')),
                          dict(status='running', engine='codex', pid=102, child_pid=102))
        self.build()
        self.assertIsNone(jobs_index.terminal(jid, jobs_index.read()))

    def test_failed_index_write_dirties_but_receipt_succeeds(self):
        jid, _ = self.add_job('publication')
        with mock.patch.object(jobs_index, '_connect', side_effect=sqlite3.OperationalError('locked')):
            jobs._write_job_json(jid, 'status.json', dict(status='done', engine='codex'))
            jobs._write_job_json(jid, 'result.json', dict(id=jid, status='done'))
        self.assertEqual(common.read_json(str(Path(common.JOBS, jid, 'result.json')))['status'], 'done')
        self.assertEqual(jobs.job_status(jid), 'done')
        self.assertTrue(jobs_index._dirty())
        self.assertIsNone(jobs_index.read())

    def test_folder_write_precedes_index_write(self):
        jid, _ = self.add_job('order')
        seen = []
        def update(job):
            seen.append(common.read_json(str(Path(common.JOBS, job, 'status.json')))['status'])
        with mock.patch.object(jobs_index, 'update', side_effect=update):
            jobs._write_job_json(jid, 'status.json', dict(status='done'))
        self.assertEqual(seen, ['done'])

    def test_status_transitions_update_index(self):
        jid, _ = self.add_job('transition')
        jobs._write_job_json(jid, 'status.json', dict(status='running', engine='codex', phase='engine', pid=101))
        row = jobs_index.read()[jid]
        self.assertEqual((row['status'], row['phase'], row['pid']), ('running', 'engine', 101))
        self.assertIsNotNone(row['dedupe_key'])
        jobs._write_job_json(jid, 'result.json', dict(status='done'))
        self.assertIsNone(jobs_index.terminal(jid, jobs_index.read()))
        jobs._write_job_json(jid, 'status.json', dict(status='done', engine='codex'))
        self.assertIsNotNone(jobs_index.terminal(jid, jobs_index.read()))

    def test_rebuild_replaces_corrupt_index_and_clears_dirty(self):
        jid, _ = self.add_job('rebuild')
        Path(jobs_index.path()).write_bytes(b'corrupt')
        jobs_index._mark_dirty()
        data = self.build()
        self.assertFalse(data['dirty'])
        self.assertEqual(set(jobs_index.read()), {jid})
        self.assertFalse(Path(jobs_index.dirty_path()).exists())

    def test_rebuild_failure_preserves_old_database(self):
        self.add_job('rebuild')
        self.build()
        before = Path(jobs_index.path()).read_bytes()
        with mock.patch.object(jobs_index, '_snapshot', side_effect=OSError('fixture')):
            with self.assertRaises(OSError):
                self.build()
        self.assertEqual(Path(jobs_index.path()).read_bytes(), before)
        self.assertTrue(jobs_index._dirty())
        self.assertFalse(list(Path(common.STATE).glob('jobs-index-*')))

    def test_concurrent_failure_stays_dirty_after_rebuild(self):
        self.add_job('rebuild')
        jobs_index._mark_dirty()
        original = jobs_index._snapshot
        def snapshot(jid):
            jobs_index._mark_dirty()
            return original(jid)
        with mock.patch.object(jobs_index, '_snapshot', side_effect=snapshot):
            self.assertTrue(self.build()['dirty'])
        self.assertIsNone(jobs_index.read())
        self.assertFalse(self.build()['dirty'])

    def test_new_folder_during_rebuild_is_not_hidden(self):
        self.add_job('terminal', status='done', receipt=True)
        original = jobs_index._snapshot
        def snapshot(jid):
            self.add_job('late')
            return original(jid)
        with mock.patch.object(jobs_index, '_snapshot', side_effect=snapshot):
            self.build()
        self.assertEqual(jobs.running_count('codex'), 1)

    def test_dry_rebuild_creates_no_index_files_or_sqlite_calls(self):
        self.add_job('dry')
        with mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('dry run')):
            self.assertEqual(jobs_index._rebuild(dry_run=True), dict(dry_run=True, jobs=1))
        self.assertFalse(list(Path(common.STATE).glob('jobs.db*')))

    def test_worker_cannot_reindex_api_cli_or_shell(self):
        config = Path(self.tmp.name, 'config.json')
        config.write_text(json.dumps({'jobs_index': {'enabled': True}}))
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'fixture-parent', 'TWINLANE_CONFIG': str(config)}):
            with self.assertRaisesRegex(ValueError, 'not allowed'):
                jobs_index.rebuild()
            with redirect_stdout(io.StringIO()):
                self.assertEqual(jobs.cmd_jobs(['reindex', '--dry-run', '--json']), common.EXIT_REFUSED)
                self.assertEqual(jobs.cmd_jobs(['reindex', '--json']), common.EXIT_REFUSED)
        for command in ('twinlane jobs reindex', 'dual jobs reindex --dry-run',
                        'python -m twinlane jobs reindex --json'):
            config = {'self_aliases': ['dual']} if command.startswith('dual ') else None
            self.assertIsNotNone(guard.shell_hazard(command, config), command)
        self.assertFalse(list(Path(common.STATE).glob('jobs.db*')))

    def test_reindex_off_refuses_without_sqlite(self):
        common.JOBS_INDEX_ENABLED = False
        with mock.patch.object(sqlite3, 'connect', side_effect=AssertionError('OFF')):
            with self.assertRaisesRegex(ValueError, 'literal true'):
                jobs_index.rebuild()
        self.assertFalse(list(Path(common.STATE).glob('jobs.db*')))

    def test_removed_folder_index_row_is_ignored(self):
        jid, _ = self.add_job('removed')
        self.build()
        Path(common.JOBS, jid).rename(Path(self.tmp.name, 'archived-fixture'))
        self.assertEqual(jobs.running_count('codex'), 0)
        self.assertEqual(jobs._indexed_jobs()[0], [])

    def test_terminal_skips_all_folder_json_reads(self):
        self.add_job('terminal', status='done', receipt=True)
        self.build()
        with mock.patch.object(common, 'read_json', side_effect=AssertionError('terminal folder JSON read')):
            self.assertEqual(jobs.running_count('codex'), 0)

    def _collect_guarding(self, old, allowed=()):
        original = common.read_json
        def read(path, *args, **kwargs):
            if old in str(path) and os.path.basename(str(path)) not in allowed:
                raise AssertionError('old terminal folder JSON was read: ' + os.path.basename(str(path)))
            return original(path, *args, **kwargs)
        with mock.patch.object(common, 'read_json', side_effect=read):
            return board.collect_jobs(self.now, False, 1)

    def test_board_old_history_uses_cached_receipt(self):
        self.add_job('recent', status='done', receipt=True)
        old, _ = self.add_job('old', status='done', receipt=True, age=86400 * 10)
        Path(common.JOBS, old, jobs.COLLECTED).write_text('{}', encoding='utf-8')
        self.build()
        data = self._collect_guarding(old)  # a collected old receipt opens no folder JSON at all
        self.assertEqual(len(data['finished']), 1)
        self.assertEqual(data['unreadable'], [])
        self.assertEqual(data['receipts']['by_owner'], {'none': 1})

    def test_board_old_uncollected_history_reads_only_its_owner(self):
        self.add_job('recent', status='done', receipt=True)
        old, _ = self.add_job('old', status='done', receipt=True, age=86400 * 10)
        self.build()
        # The owner is not indexed, so only job.json may be opened; never the receipt or status.
        data = self._collect_guarding(old, allowed=('job.json',))
        self.assertEqual(len(data['finished']), 1)
        self.assertEqual(data['receipts']['by_owner'], {'none': 2})

    def test_status_live_collection_skips_terminal_folder_reads(self):
        self.add_job('terminal', status='done', receipt=True)
        self.build()
        with mock.patch.object(common, 'read_json', side_effect=AssertionError('terminal folder read')):
            self.assertEqual(jobs.list_jobs(live_only=True), [])

    def test_waiter_order_matches_folder_path(self):
        for tag, priority in [('code', 1), ('review', 0), ('unknown', 0)]:
            jid, _ = self.add_job(tag, status='queued', pid=102 if tag == 'unknown' else 101)
            common.write_json(str(Path(common.JOBS, jid, 'status.json')),
                              dict(status='queued', engine='codex', pid=102 if tag == 'unknown' else 101,
                                   waiting_slot=True, queue_priority=priority, waiting_since='2026-01-01T00:00:00Z'))
        self.add_job('done', status='done', receipt=True)
        self.build()
        common.JOBS_INDEX_ENABLED = False
        expected = jobs._waiters('codex', 'none')
        common.JOBS_INDEX_ENABLED = True
        self.assertEqual(jobs._waiters('codex', 'none'), expected)
        self.assertEqual([row[0] for row in expected], [0, 1])

    def test_old_terminal_with_unknown_child_is_not_hidden_by_history_shortcut(self):
        self.add_job('recent', status='done', receipt=True)
        jid, _ = self.add_job('old-pending', status='done', receipt=True, age=86400 * 10)
        self.build()
        common.write_json(str(Path(common.JOBS, jid, jobs.TREE_UNRESOLVED)), dict(engine='codex'))
        data = board.collect_jobs(self.now, False, 1)
        self.assertEqual(data['live'][0]['id'], jid)
        self.assertEqual(data['live'][0]['status'], 'unknown')

    def test_rebuild_after_readonly_snapshot_cleans_sidecars(self):
        self.add_job('terminal', status='done', receipt=True)
        self.build()
        self.assertIsNotNone(jobs_index.read())
        self.assertFalse(self.build()['dirty'])
        self.assertIsNotNone(jobs_index.read())

    def test_rebuild_refuses_foreign_open_wal_connection(self):
        self.add_job('terminal', status='done', receipt=True)
        self.build()
        before = Path(jobs_index.path()).read_bytes()
        with closing(sqlite3.connect(jobs_index.path())) as foreign:
            foreign.execute('SELECT * FROM jobs').fetchall()
            with self.assertRaises(OSError):
                self.build()
            self.assertEqual(Path(jobs_index.path()).read_bytes(), before)
        self.assertTrue(jobs_index._dirty())
        self.assertFalse(self.build()['dirty'])

    def test_timing_3000_folders(self):
        for i in range(2998):
            self.add_job(f'timing-{i}', status='done', receipt=True)
        self.add_job('live')
        self.add_job('unknown', pid=102)
        self.build()
        common.JOBS_INDEX_ENABLED = False
        start = time.perf_counter()
        baseline = jobs.running_count('codex')
        folder_s = time.perf_counter() - start
        common.JOBS_INDEX_ENABLED = True
        start = time.perf_counter()
        indexed = jobs.running_count('codex')
        index_s = time.perf_counter() - start
        self.assertEqual((baseline, indexed), (2, 2))
        print(f'jobs-index timing (3000 folders): folder={folder_s:.6f}s index={index_s:.6f}s count={indexed}', flush=True)


if __name__ == '__main__':
    unittest.main()
