"""Job archival uses disposable homes and synthetic evidence only."""
import datetime as dt
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import test_support  # noqa: E402
from twinlane import archive, baton, board, cli, common, guard, jobs  # noqa: E402


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='twinlane-archive-')
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.home = Path(self.temp.name)
        for key, folder in (('JOBS', 'jobs'), ('STATE', 'state'), ('BATONS', 'batons')):
            patch = mock.patch.object(common, key, str(self.home / folder))
            patch.start()
            self.addCleanup(patch.stop)
            (self.home / folder).mkdir()
        patch = mock.patch.dict(os.environ, {'TWINLANE_HOME': str(self.home),
                                            'TWINLANE_CONFIG': str(self.home / 'config.json')})
        patch.start()
        self.addCleanup(patch.stop)
        self.now = dt.datetime(2026, 10, 20, 12, tzinfo=dt.timezone.utc)
        patch = mock.patch.object(common, 'now', return_value=self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.count = 0

    def job(self, days=30, status='done', read=True, state=None, owner=None):
        self.count += 1
        jid = f'20260901-120000-codex-{self.count:04x}'
        folder = self.home / 'jobs' / jid
        folder.mkdir()
        ended = common.iso(self.now - dt.timedelta(days=days))
        common.write_json(str(folder / 'job.json'), dict(id=jid, name='sample task', owner=owner))
        common.write_json(str(folder / 'status.json'), state or {'status': status})
        common.write_json(str(folder / 'result.json'), dict(id=jid, name='sample task', status=status,
                          finished_at=ended, final_path=str(folder / 'final.md')))
        (folder / 'final.md').write_text('Saved result text', encoding='utf-8')
        if read:
            (folder / jobs.COLLECTED).write_text(common.iso(self.now), encoding='utf-8')
        return jid, folder

    def call(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = jobs.cli('jobs', ['archive', '--json', *args])
        return code, json.loads(output.getvalue())

    def add_baton(self, jid, waiting=False, entry=False):
        record = baton.empty_baton('sample-lane')
        if waiting:
            record['waiting_on'] = 'job:' + jid
        elif entry:
            record['jobs'] = [{'id': jid, 'status': 'running'}]
        else:
            record['progress']['in_flight'] = [{'what': 'sample', 'job': jid}]
        common.write_json(baton.baton_path('sample-lane'), record)

    def test_dry_run_classifies_states_and_changes_no_bytes(self):
        good, _ = self.job()
        unread, _ = self.job(read=False)
        recent, _ = self.job(days=1)
        live, _ = self.job(state={'status': 'running', 'pid': os.getpid(),
                                  'pid_created': common.process_created(os.getpid())})
        locked, folder = self.job()
        (folder / jobs.RUNNER_LOCK).write_text('99999999 1', encoding='ascii')
        lost, folder = self.job()
        (folder / 'result.json').unlink()
        broken, folder = self.job()
        (folder / 'result.json').write_text('{', encoding='utf-8')
        flight, _ = self.job()
        self.add_baton(flight)
        waiting, _ = self.job(owner='owner-lane')
        record = baton.empty_baton('owner-lane')
        record['waiting_on'] = 'job:' + waiting
        common.write_json(baton.baton_path('owner-lane'), record)
        before = {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        code, data = self.call()
        self.assertEqual(code, 0)
        self.assertEqual([r['id'] for r in data['candidates']], [good])
        reasons = {r['id']: r['reason'] for r in data['skipped']}
        for jid, reason in [(unread, 'unread receipt'), (recent, 'recent'), (live, 'runner alive'),
                            (locked, 'runner lock present'), (lost, 'missing result.json'),
                            (broken, 'unreadable result.json'), (flight, 'baton in flight: sample-lane'),
                            (waiting, 'owner baton waiting: owner-lane')]:
            self.assertEqual(reasons[jid], reason)
        self.assertEqual(data['candidates'][0]['age_days'], 30)
        self.assertEqual(data['moved'], [])
        self.assertEqual(sum(data['skipped_counts'].values()), 8)
        self.assertEqual(before, {p.relative_to(self.home): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_apply_moves_indexes_and_all_lookups_keep_working(self):
        jid, folder = self.job()
        kept, kept_folder = self.job(read=False)
        code, data = self.call('--apply')
        self.assertEqual((code, data['errors']), (0, []))
        self.assertEqual([r['id'] for r in data['moved']], [jid])
        destination = self.home / 'jobs' / 'archive' / '2026-09' / jid
        self.assertTrue(destination.is_dir())
        self.assertFalse(folder.exists())
        self.assertTrue(kept_folder.exists())
        index = self.home / 'jobs' / 'archive' / 'index.jsonl'
        entry = json.loads(index.read_text(encoding='utf-8'))
        self.assertEqual(set(entry), {'id', 'name', 'status', 'ended_at', 'archived_at'})
        self.assertEqual(entry['id'], jid)
        for command, args in [('result', [jid, '--json']), ('wait', [jid, '--timeout', '0', '--json'])]:
            output = io.StringIO()
            with redirect_stdout(output):
                code = jobs.cli(command, args)
            self.assertEqual(code, 0, output.getvalue())
            if command == 'result':
                self.assertEqual(json.loads(output.getvalue())['final_text'], 'Saved result text')
        output = io.StringIO()
        with redirect_stdout(output):
            self.assertEqual(cli.main(['status', jid, '--json']), 0)
        self.assertEqual(json.loads(output.getvalue())['status'], 'done')
        self.assertEqual(jobs.resolve_job(jid[-4:]), jid)
        self.assertEqual([r['id'] for r in jobs.list_jobs(limit=None)], [kept])
        with mock.patch.object(board, '_job_row', wraps=board._job_row) as read_row:
            board.collect_jobs(self.now, True, 20)
            self.assertNotIn(jid, [call.args[0] for call in read_row.call_args_list])
        self.assertEqual(jobs._job_ids(), [kept])
        self.assertEqual([r['id'] for r in baton._scan_jobs_dir(20)], [kept])
        index.unlink()
        self.assertEqual(jobs.resolve_job(jid), jid)
        self.assertEqual(jobs.job_status(jid), 'done')

    def test_age_policy_include_unread_and_strict_boundary(self):
        old, _ = self.job(days=20, read=False)
        boundary, _ = self.job(days=14)
        self.assertEqual(self.call()[1]['candidates'], [])
        self.assertEqual([r['id'] for r in self.call('--include-unread')[1]['candidates']], [old])
        self.assertEqual([r['id'] for r in self.call('--older-than', '13')[1]['candidates']], [boundary])
        common.write_json(str(self.home / 'config.json'), {'jobs': {'archive_after_days': 21}})
        self.assertEqual(self.call('--include-unread')[1]['candidates'], [])
        code, data = self.call('--older-than', '10', '--include-unread', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual({r['id'] for r in data['moved']}, {old, boundary})

    def test_unknown_runner_and_engine_survivor_block(self):
        runner, _ = self.job(state={'pid': 1234, 'pid_created': 123})
        engine, _ = self.job(state={'child_pid': 1234, 'child_created': 123})
        survivor, folder = self.job()
        common.write_json(str(folder / jobs.SURVIVOR), {'pid': 1234, 'created': 123})
        with mock.patch.object(common, 'pid_state', return_value=common.UNKNOWN):
            _, data = self.call()
        self.assertEqual({r['id']: r['reason'] for r in data['skipped']},
                         {runner: 'runner unknown', engine: 'engine unknown', survivor: 'engine unknown'})

    def test_corrupt_optional_evidence_and_baton_fail_closed(self):
        for marker in ('status.json', jobs.SURVIVOR, jobs.CHILD_STARTING):
            with self.subTest(marker=marker):
                jid, folder = self.job()
                (folder / marker).write_text('{', encoding='utf-8')
                self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'unreadable ' + marker)
        jid, _ = self.job()
        Path(baton.baton_path('broken')).write_text('{', encoding='utf-8')
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'unreadable baton broken')

    def test_live_job_entry_and_owner_pending_protect_receipt(self):
        jid, _ = self.job()
        self.add_baton(jid, entry=True)
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'baton in flight: sample-lane')
        Path(baton.baton_path('sample-lane')).unlink()
        jid, folder = self.job(owner='owner-lane')
        common.write_json(baton.baton_path('owner-lane'), baton.empty_baton('owner-lane'))
        common.write_json(str(folder / jobs.OWNER_PENDING), {'owner': 'owner-lane', 'job_id': jid})
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'owner completion pending')

    def test_manual_baton_pointer_is_not_a_job_dependency(self):
        jid, _ = self.job()
        record = baton.empty_baton('sample-lane')
        record['progress']['in_flight'] = [{'what': 'manual review', 'check': 'read the report', 'job': None}]
        common.write_json(baton.baton_path('sample-lane'), record)
        self.assertNotIn('reason', archive.eligibility(jid, 14, False))
        record['progress']['in_flight'][0]['job'] = []
        common.write_json(baton.baton_path('sample-lane'), record)
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'invalid baton sample-lane')

    def test_recheck_blocks_changed_state_under_locks(self):
        jid, folder = self.job()
        original = archive._move
        def change(*args):
            common.write_json(str(folder / 'status.json'), {'pid': os.getpid(),
                              'pid_created': common.process_created(os.getpid())})
            return original(*args)
        with mock.patch.object(archive, '_move', side_effect=change):
            code, data = self.call('--apply')
        self.assertEqual(code, common.EXIT_REFUSED)
        self.assertEqual(data['moved'], [])
        self.assertEqual(data['apply_blocked'][0]['reason'], 'runner alive')
        self.assertTrue(folder.exists())

    def test_busy_lifecycle_locks_prevent_move(self):
        jid, folder = self.job()
        for lock in ('runner.claim', 'status.lock'):
            with self.subTest(lock=lock), common.file_lock(jobs.lock_paths(jid, lock)[1], 0):
                code, data = self.call('--apply')
                self.assertEqual(code, common.EXIT_FAILED)
                self.assertEqual(data['moved'], [])
                self.assertTrue(folder.exists())

    def test_move_failure_continues_and_never_overwrites_destination(self):
        bad, bad_folder = self.job()
        good, _ = self.job()
        replace = os.replace
        def fail(source, destination):
            if source == str(bad_folder):
                raise PermissionError('fixture failure')
            return replace(source, destination)
        with mock.patch.object(archive.os, 'replace', side_effect=fail):
            code, data = self.call('--apply')
        self.assertEqual(code, common.EXIT_FAILED)
        self.assertEqual([r['id'] for r in data['moved']], [good])
        self.assertTrue(bad_folder.exists())
        self.assertEqual(data['errors'][0]['id'], bad)
        collision = self.home / 'jobs' / 'archive' / '2026-09' / bad
        collision.mkdir()
        code, data = self.call('--apply')
        self.assertEqual(code, 0)
        self.assertEqual(data['skipped'][0]['reason'], 'archive destination exists')

    def test_only_requested_terminal_statuses_and_valid_read_markers(self):
        for status in ('running', 'lost', 'limit_hit', 'interrupted'):
            jid, _ = self.job(status=status)
            self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'no archivable terminal receipt')
        for status in archive.TERMINAL:
            jid, _ = self.job(status=status)
            self.assertNotIn('reason', archive.eligibility(jid, 14, False))
        jid, folder = self.job()
        (folder / jobs.COLLECTED).write_text('unreadable time', encoding='utf8')
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'invalid or stale read marker')

    def test_guard_allows_preview_and_denies_apply_forms(self):
        allow = common.load_policy()['claude']['intel_allow']
        for profile in ('intel', 'code'):
            for command in ('twinlane jobs archive --apply', 'python -m twinlane jobs archive --apply',
                            'twinlane jobs archive --app', 'twinlane jobs archive --a',
                            'twinlane jobs archive --apply --json'):
                self.assertIsNotNone(guard.evaluate('Bash', {'command': command}, profile, str(self.home), allow), command)
            self.assertIsNone(guard.evaluate('Bash', {'command': 'twinlane jobs archive --json'},
                                            profile, str(self.home), allow))
            self.assertIsNotNone(guard.evaluate('Bash', {'command': 'custom-lanes jobs archive --a'},
                                               profile, str(self.home), allow,
                                               config={'self_aliases': ['custom-lanes']}))

    def test_unreadable_result_and_malformed_identity_are_not_clearance(self):
        jid, folder = self.job()
        read_open = open
        def denied(path, *args, **kwargs):
            if os.fspath(path) == str(folder / 'result.json'):
                raise PermissionError('fixture denied')
            return read_open(path, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=denied):
            self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'unreadable result.json')
        (folder / 'runner.pid').write_text('', encoding='ascii')
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'invalid runner identity')
        (folder / 'runner.pid').unlink()
        common.write_json(str(folder / 'status.json'), {'pid': 0, 'pid_created': 1})
        self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'invalid process identity: pid')

    def test_external_final_path_remains_readable(self):
        jid, folder = self.job()
        receipt = common.read_json(str(folder / 'result.json'))
        outside = self.home / 'external-result.md'
        outside.write_text('External result', encoding='utf-8')
        receipt['final_path'] = str(outside)
        common.write_json(str(folder / 'result.json'), receipt)
        with mock.patch.object(os.path, 'relpath', side_effect=ValueError('different drives')):
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(jobs.cli('result', [jid, '--json']), 0)
        self.assertEqual(json.loads(output.getvalue())['final_text'], 'External result')

    def test_completed_owner_and_dead_stamped_processes_can_archive(self):
        jid, folder = self.job(owner='owner-lane', state={'pid': 1234, 'pid_created': 123,
                                                        'child_pid': 2345, 'child_created': 234})
        common.write_json(baton.baton_path('owner-lane'), baton.empty_baton('owner-lane'))
        common.write_json(str(folder / jobs.OWNER_NOTIFIED), {'owner': 'owner-lane', 'job_id': jid})
        (folder / 'runner.pid').write_text('1234 123', encoding='ascii')
        with mock.patch.object(common, 'pid_state', return_value=common.DEAD):
            code, data = self.call('--apply')
        self.assertEqual((code, data['errors']), (0, []))
        self.assertEqual([r['id'] for r in data['moved']], [jid])

    def test_include_unread_keeps_owner_completion_notice_visible(self):
        jid, folder = self.job(read=False, owner='owner-lane')
        record = baton.empty_baton('owner-lane')
        record['waiting_on'] = 'lead'
        record['jobs'] = [{'id': jid, 'status': 'done'}]
        common.write_json(baton.baton_path('owner-lane'), record)
        common.write_json(str(folder / jobs.OWNER_NOTIFIED), {'owner': 'owner-lane', 'job_id': jid})
        code, data = self.call('--include-unread', '--apply')
        self.assertEqual(code, 0)
        self.assertEqual(data['moved'], [])
        self.assertEqual(data['skipped'][0]['reason'], 'owner receipt not collected')
        self.assertTrue(folder.exists())


if __name__ == '__main__':
    unittest.main()
