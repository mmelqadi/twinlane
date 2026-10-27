"""Read-stamp behavior in isolated homes; no runners or external actions."""
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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # noqa: E402
from twinlane import ack, archive, baton, board, cli, common, guard, inbox, jobs  # noqa: E402


class AckTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='twinlane-ack-')
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.home = Path(self.temp.name)
        for key in ('JOBS', 'STATE', 'BATONS'):
            p = self.home / key.lower()
            p.mkdir()
            patch = mock.patch.object(common, key, str(p))
            patch.start()
            self.addCleanup(patch.stop)
        self.now = common.parse_time(common.iso(common.now()))
        patch = mock.patch.object(common, 'now', return_value=self.now)
        patch.start()
        self.addCleanup(patch.stop)
        self.count = 0

    def job(self, status='done', days=30, owner=None, name='review task'):
        self.count += 1
        jid = f'20260901-120000-codex-{self.count:04x}'
        folder = Path(common.JOBS) / jid
        folder.mkdir()
        common.write_json(str(folder / 'job.json'), dict(id=jid, name=name, owner=owner))
        common.write_json(str(folder / 'result.json'), dict(id=jid, status=status,
                          ended_at=common.iso(self.now - dt.timedelta(days=days))))
        return jid, folder

    def call(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ack.cli(list(args))
        return code, out.getvalue()

    def test_explicit_multiple_idempotent_and_cli_dispatch(self):
        a, folder = self.job()
        b, other = self.job(status='failed')
        with mock.patch.object(jobs, 'mark_collected', wraps=jobs.mark_collected) as stamp:
            code, out = self.call(a, b, a)
            self.assertEqual(code, 0)
            self.assertEqual(stamp.call_count, 2)
            self.assertEqual(out.count(': stamped'), 2)
        for p in (folder, other):
            self.assertEqual(common.parse_time((p / jobs.COLLECTED).read_text()), self.now)
        with redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['ack', a]), 0)
        self.assertIn('already collected', self.call(a)[1])

    def test_requires_matching_terminal_receipt(self):
        for content in (None, '{', '[]', '{"status": "done"}',
                        json.dumps({'id': 'wrong', 'status': 'done'})):
            jid, folder = self.job()
            result = folder / 'result.json'
            if content is None:
                result.unlink()
            else:
                result.write_text(content, encoding='utf-8')
            self.assertIn('no matching terminal receipt', self.call(jid)[1])
            self.assertFalse((folder / jobs.COLLECTED).exists())
        for status in ('running', 'queued', 'unknown', 'orphaned'):
            jid, folder = self.job(status=status)
            self.assertIn('skipped', self.call(jid)[1])
            self.assertFalse((folder / jobs.COLLECTED).exists())

    def test_malformed_terminal_status_is_skipped(self):
        jid, folder = self.job()
        common.write_json(str(folder / 'result.json'), {'id': jid, 'status': []})
        self.assertIn('skipped', self.call(jid)[1])
        self.assertFalse((folder / jobs.COLLECTED).exists())

    def test_worker_guard_blocks_ack(self):
        for words in (['twinlane', 'ack', '20260901-120000-codex-0001'], ['twinlane', 'ack', '--older-than', '14'],
                      ['python', '-m', 'twinlane', 'ack', '20260901-120000-codex-0001']):
            self.assertIsNotNone(guard.self_hazard(words))
        self.assertIsNotNone(guard.self_hazard(['dual', 'ack', '--older-than', '14'],
                                              {'self_names': ('dual',)}))
        self.assertIsNone(guard.self_hazard(['twinlane', 'result', '20260901-120000-codex-0001']))

    def test_bulk_age_and_name_filter_and_dry_run(self):
        old, folder = self.job()
        recent, recent_folder = self.job(days=2)
        boundary, boundary_folder = self.job(days=14)
        mismatch, mismatch_folder = self.job(name='build task')
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        code, out = self.call('--older-than', '14', '--name', 'review*', '--dry-run')
        self.assertEqual(code, 0)
        self.assertIn(old + ': would stamp', out)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        self.call('--older-than', '14', '--name', 'review*')
        self.assertTrue((folder / jobs.COLLECTED).exists())
        for p in (recent_folder, boundary_folder, mismatch_folder):
            self.assertFalse((p / jobs.COLLECTED).exists())

    def owner_notice(self, jid, folder):
        b = baton.empty_baton('owner-lane')
        b.update(waiting_on='lead', waiting_since=common.iso(self.now))
        common.write_json(baton.baton_path('owner-lane'), b)
        common.write_json(str(folder / jobs.OWNER_NOTIFIED),
                          dict(owner='owner-lane', job_id=jid, waiting_on=b['waiting_on'],
                               waiting_since=b['waiting_since'], at=common.iso(self.now)))
        return b

    def test_owner_inbox_notice_is_not_dismissed(self):
        jid, folder = self.job(owner='owner-lane')
        b = self.owner_notice(jid, folder)
        before = inbox.items(batons=[b], session_rows=[])
        self.assertTrue(any(r.get('job_id') == jid for r in before))
        self.assertIn('owner notice still in inbox', self.call(jid)[1])
        self.assertFalse((folder / jobs.COLLECTED).exists())
        self.assertEqual(before, inbox.items(batons=[b], session_rows=[]))
        self.assertEqual(archive.eligibility(jid, 14, True)['reason'], 'owner receipt not collected')

    def test_owner_filter_dismissed_notice_and_pending_keep(self):
        jid, folder = self.job(owner='owner-lane')
        other, other_folder = self.job()
        b = self.owner_notice(jid, folder)
        b['waiting_since'] = common.iso(self.now + dt.timedelta(seconds=1))
        common.write_json(baton.baton_path('owner-lane'), b)
        (folder / jobs.OWNER_PENDING).write_text('{}', encoding='utf-8')
        self.assertIn('owner completion pending', self.call('--older-than', '14', '--owner', 'owner-lane')[1])
        (folder / jobs.OWNER_PENDING).unlink()
        self.call('--older-than', '14', '--owner', 'owner-lane')
        self.assertTrue((folder / jobs.COLLECTED).exists())
        self.assertFalse((other_folder / jobs.COLLECTED).exists())
        self.assertNotIn('reason', archive.eligibility(jid, 14, False))

    def test_unverified_owner_and_failed_stamp(self):
        jid, folder = self.job(owner='missing-owner')
        self.assertIn('owner completion unverified', self.call(jid)[1])
        self.assertFalse((folder / jobs.COLLECTED).exists())
        jid, folder = self.job()
        with mock.patch.object(jobs, 'mark_collected', return_value=False):
            code, out = self.call(jid)
        self.assertEqual(code, common.EXIT_FAILED)
        self.assertIn('skipped', out)
        self.assertFalse((folder / jobs.COLLECTED).exists())

    def test_stamping_never_makes_unknown_custody_archivable(self):
        jid, folder = self.job()
        common.write_json(str(folder / 'status.json'), {'pid': 1234, 'pid_created': 123})
        with mock.patch.object(common, 'pid_state', return_value=common.UNKNOWN):
            self.assertIn(': stamped', self.call(jid)[1])
            self.assertEqual(archive.eligibility(jid, 14, False)['reason'], 'runner unknown')
        self.assertTrue(folder.is_dir())

    def test_invalid_arguments(self):
        for args in ([], ['--older-than', '-1'], ['--older-than', 'nan'], ['--older-than', 'inf'],
                     ['--name', '*'], ['a', '--older-than', '1'], ['--older-than', '1', '--owner', '../x']):
            self.assertEqual(self.call(*args)[0], common.EXIT_USAGE, args)

    def test_board_counts_old_receipts_with_capped_finished_rows(self):
        for owner, count in ((None, 4), ('a', 3), ('b', 2)):
            for _ in range(count):
                self.job(owner=owner)
        stamped, folder = self.job()
        self.call(stamped)
        self.job(status='running')
        with mock.patch.object(jobs, '_job_ids', wraps=jobs._job_ids) as ids:
            data = board.collect_jobs(self.now, False, 1)
        self.assertEqual(ids.call_count, 1)
        self.assertEqual(data['receipts'], {'uncollected': 9, 'by_owner': {'a': 3, 'b': 2, 'none': 4}})
        self.assertEqual(len(data['finished']), 1)
        self.assertEqual(board._receipts_line(data), 'receipts: 9 uncollected (by owner: a 3, b 2, none 4)')
        for renderer in (board.render_text, board.render_md):
            # Full board construction is already covered in test_board; use it with external probes stubbed.
            with mock.patch.object(board, 'collect_worktrees', return_value={'repos': [], 'errors': []}), \
                    mock.patch('twinlane.sessions.collect_sessions', return_value={'rows': []}), \
                    mock.patch.object(board, 'host_and_queue', return_value=('', [], '')):
                full = board.collect(policy=common.load_policy())
            self.assertIn('receipts: 9 uncollected', renderer(full))
        self.assertEqual(json.loads(json.dumps(data))['receipts']['uncollected'], 9)


if __name__ == '__main__':
    unittest.main()
