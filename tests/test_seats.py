"""Seat reservation scaffolding only. Never launch Codex or touch the real home."""
import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate before runtime imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from twinlane import cli, common, seats


class Seats(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='seats-')
        self.addCleanup(test_support.cleanup_tempdir, temp)
        self.root = Path(temp.name)
        self.state = self.root / 'state'
        self.policy = {'seats': {'enabled': True}}
        self.addCleanup(mock.patch.stopall)
        mock.patch.object(common, 'STATE', str(self.state)).start()
        mock.patch.object(common, 'load_policy', side_effect=lambda: self.policy).start()
        self.live = mock.patch.object(common, 'pid_state', return_value=common.ALIVE).start()
        self.launch = mock.patch('subprocess.Popen', side_effect=AssertionError('no engine launches')).start()

    def path(self, number=1):
        return self.state / 'seats' / f'{number}.json'

    def record(self, number=1, **updates):
        record = {'pid': 123, 'pid_created': 456, 'started_at': '2026-10-26T00:00:00Z'}
        record.update(updates)
        path = self.path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record), encoding='utf-8')
        return path

    def command(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = cli.main(['seats', *args])
        self.launch.assert_not_called()
        return rc, out.getvalue(), err.getvalue()

    def test_packaged_default_is_off_and_only_literal_true_enables(self):
        packaged = json.loads(Path(common.POLICY).read_text(encoding='utf-8'))
        self.assertFalse(seats.enabled(packaged))
        for section in (None, [], True, {}, {'enabled': False}, {'enabled': 1}, {'enabled': 'true'}):
            with self.subTest(section=section):
                self.assertFalse(seats.enabled({'seats': section}))
        self.assertTrue(seats.enabled({'seats': {'enabled': True}}))

    def test_top_help_hides_disabled_seats(self):
        for enabled in (False, True):
            self.policy['seats']['enabled'] = enabled
            out = io.StringIO()
            with mock.patch.object(common, 'ensure_dirs'), contextlib.redirect_stdout(out):
                self.assertEqual(cli.main(['--help']), 0)
            self.assertEqual('twinlane seats' in out.getvalue(), enabled)

    def test_status_of_absent_home_creates_nothing(self):
        self.policy['seats']['enabled'] = False
        rc, out, _ = self.command('status', '--json')
        self.assertEqual(rc, common.EXIT_OK)
        data = json.loads(out)
        self.assertEqual(data['seats'], [])
        self.assertFalse(data['enabled'])
        self.assertFalse(data['runtime_ready'])
        self.assertFalse(self.state.exists())

    def test_status_never_reclaims_dead_records_or_takes_a_lock(self):
        path = self.record()
        before = path.read_bytes()
        self.live.return_value = common.DEAD
        with mock.patch.object(common, 'file_lock', side_effect=AssertionError('read-only')):
            rc, out, _ = self.command('status', '--json')
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)['seats'][0]['state'], common.DEAD)
        self.assertEqual(path.read_bytes(), before)
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_claim_records_identity_and_started_time(self):
        record = seats.claim(1, 123, 456)
        self.assertEqual(json.loads(self.path().read_text(encoding='utf-8')), record)
        self.assertEqual((record['pid'], record['pid_created']), (123, 456))
        self.assertIsNotNone(common.parse_time(record['started_at']))
        self.live.assert_called_with(123, 456)

    def test_claim_does_not_repeat_even_for_the_same_live_owner(self):
        seats.claim(1, 123, 456)
        before = self.path().read_bytes()
        with self.assertRaises(seats.SeatBusy):
            seats.claim(1, 123, 456)
        self.assertEqual(self.path().read_bytes(), before)

    def test_status_includes_every_number_accepted_by_claim(self):
        number = 1000000001
        seats.claim(number, 123, 456)
        self.assertEqual(seats.status()[0]['seat_id'], number)

    def test_unknown_never_releases_or_replaces_even_an_ancient_claim(self):
        path = self.record(started_at='2000-01-01T00:00:00Z')
        before = path.read_bytes()
        self.live.return_value = common.UNKNOWN
        self.assertFalse(seats.release_dead(1))
        with self.assertRaises(seats.SeatBusy):
            seats.claim(1, 789, 1000)
        self.assertEqual(path.read_bytes(), before)

    def test_dead_owner_can_be_replaced_with_a_verified_new_identity(self):
        self.record()
        self.live.side_effect = lambda pid, created: common.DEAD if (pid, created) == (123, 456) else common.ALIVE
        seats.claim(1, 789, 1000)
        self.assertEqual(seats.status()[0]['pid'], 789)

    def test_missing_stamp_and_malformed_records_are_unknown_and_retained(self):
        invalid = [None, [], {}, {'pid': 123}, {'pid': 123, 'pid_created': None},
                   {'pid': True, 'pid_created': 456, 'started_at': '2026-10-26T00:00:00Z'},
                   {'pid': 123, 'pid_created': False, 'started_at': '2026-10-26T00:00:00Z'},
                   {'pid': 123, 'pid_created': 456, 'started_at': 'invalid'}]
        for value in invalid:
            with self.subTest(value=value):
                path = self.path()
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(json.dumps(value), encoding='utf-8')
                before = path.read_bytes()
                self.assertEqual(seats.status()[0]['state'], common.UNKNOWN)
                self.assertFalse(seats.release_dead(1))
                with self.assertRaises(seats.SeatBusy):
                    seats.claim(1, 789, 1000)
                self.assertEqual(path.read_bytes(), before)
        self.live.assert_not_called()

    def test_invalid_json_and_invalid_utf8_are_not_absence(self):
        path = self.record()
        for payload in (b'{broken', b'\xff'):
            path.write_bytes(payload)
            self.assertEqual(seats.status()[0]['state'], common.UNKNOWN)
            self.assertFalse(seats.release_dead(1))
            self.assertEqual(path.read_bytes(), payload)

    def test_unreadable_record_is_unknown(self):
        path = self.record()
        with mock.patch.object(Path, 'read_text', side_effect=PermissionError('denied')):
            self.assertEqual(seats.status()[0]['state'], common.UNKNOWN)
            self.assertFalse(seats.release_dead(1))
        self.assertTrue(path.is_file())

    def test_linked_records_holding_a_dead_identity_are_unknown_and_retained(self):
        import os
        self.live.return_value = common.DEAD  # only the link check stands between a link and removal
        target = self.root / 'elsewhere.json'
        target.write_text(json.dumps({'pid': 123, 'pid_created': 456,
                                      'started_at': '2026-10-26T00:00:00Z'}), encoding='utf-8')
        before = target.read_bytes()
        self.path().parent.mkdir(parents=True, exist_ok=True)
        for kind, make in (('hardlink', os.link), ('symlink', os.symlink)):
            with self.subTest(kind=kind):
                try:
                    make(target, self.path())
                except (OSError, NotImplementedError) as exc:
                    self.skipTest(f'{kind} unavailable here: {exc}')
                self.assertEqual(seats.status()[0]['state'], common.UNKNOWN)
                self.assertFalse(seats.release_dead(1))
                with self.assertRaises(seats.SeatBusy):
                    seats.claim(1, 789, 1000)
                self.assertTrue(os.path.lexists(self.path()))
                self.assertEqual(target.read_bytes(), before)
                self.path().unlink()

    def test_unreadable_process_identity_is_unknown(self):
        self.record()
        self.live.side_effect = OSError('probe denied')
        self.assertEqual(seats.status()[0]['state'], common.UNKNOWN)
        self.assertFalse(seats.release_dead(1))

    def test_unreadable_directory_reports_failure_not_an_empty_pool(self):
        self.record()
        with mock.patch.object(Path, 'iterdir', side_effect=PermissionError('denied')):
            rc, out, _ = self.command('status', '--json')
        self.assertEqual(rc, common.EXIT_FAILED)
        self.assertEqual(json.loads(out)['status'], 'unknown')

    def test_invalid_new_identity_or_disabled_policy_never_publishes_a_claim(self):
        for pid, created in ((None, 1), (1, None), (True, 1), (1, False), (0, 1), (1, -1)):
            with self.subTest(pid=pid, created=created), self.assertRaises(seats.SeatError):
                seats.claim(1, pid, created)
        self.policy['seats']['enabled'] = False
        with self.assertRaises(seats.SeatError):
            seats.claim(1, 123, 456)
        self.assertFalse(self.path().exists())

    def test_unknown_new_owner_cannot_replace_dead_record(self):
        path = self.record()
        before = path.read_bytes()
        self.live.side_effect = [common.DEAD, common.UNKNOWN]
        with self.assertRaises(seats.SeatError):
            seats.claim(1, 789, 1000)
        self.assertEqual(path.read_bytes(), before)

    def test_release_rechecks_liveness_inside_lock(self):
        path = self.record()
        self.live.return_value = common.DEAD
        self.assertEqual(seats.status()[0]['state'], common.DEAD)
        self.live.return_value = common.UNKNOWN
        self.assertFalse(seats.release_dead(1))
        self.assertTrue(path.exists())

    def test_dead_only_cleanup_preserves_live_and_unknown_when_disabled(self):
        self.policy['seats']['enabled'] = False
        self.record(1)
        self.record(2, pid=234)
        self.record(3, pid=345)
        self.live.side_effect = lambda pid, stamp: {123: common.DEAD, 234: common.ALIVE, 345: common.UNKNOWN}[pid]
        rc, out, _ = self.command('stop', '--json')
        self.assertEqual(rc, common.EXIT_RUNNING)
        data = json.loads(out)
        self.assertEqual(data['reaped'], [1])
        self.assertEqual(data['retained'], [2, 3])
        self.assertEqual(data['processes_killed'], 0)
        self.assertFalse(self.path(1).exists())
        self.assertTrue(self.path(2).exists())
        self.assertTrue(self.path(3).exists())

    def test_stop_dry_run_does_not_create_lock_or_remove_dead_claim(self):
        path = self.record()
        self.live.return_value = common.DEAD
        rc, out, _ = self.command('stop', '--dry-run', '--json')
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)['would_reap'], [1])
        self.assertEqual(list(path.parent.iterdir()), [path])

    def test_stop_and_release_on_absent_home_create_nothing(self):
        self.assertFalse(seats.release_dead(1))
        rc, _, _ = self.command('stop', '--json')
        self.assertEqual(rc, 0)
        self.assertFalse(self.state.exists())

    def test_start_and_real_bench_refuse_even_when_enabled(self):
        for enabled in (True, False):
            self.policy['seats']['enabled'] = enabled
            for args in (('start', '2', '--json'), ('bench', '--n', '2', '--json')):
                with self.subTest(enabled=enabled, args=args):
                    rc, out, _ = self.command(*args)
                    self.assertEqual(rc, common.EXIT_REFUSED)
                    self.assertEqual(json.loads(out)['status'], 'refused')
                    self.assertIn('docs/seats.md', out)
        self.assertFalse(self.state.exists())

    def test_bench_dry_run_only_prints_plan_even_when_disabled(self):
        self.policy['seats']['enabled'] = False
        rc, out, _ = self.command('bench', '--n', '3', '--dry-run')
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertEqual(data['prospective_model_calls'], 6)
        self.assertEqual((data['cold_runs'], data['seat_runs']), (3, 3))
        self.assertEqual(data['launches'], 0)
        self.assertEqual(data['writes'], [])
        self.assertIsNone(data['measurements'])
        self.assertFalse(data['runtime_ready'])
        self.assertFalse(self.state.exists())

    def test_invalid_numbers_are_rejected_before_writing(self):
        for command in (('start', '0'), ('start', '-1'), ('bench', '--n', 'no'), ('bench', '--n', '0')):
            with self.subTest(command=command):
                self.assertEqual(self.command(*command)[0], common.EXIT_USAGE)
        for number in (0, -1, True, '../1', 1.5):
            with self.subTest(number=number), self.assertRaises(ValueError):
                seats.claim(number, 123, 456)
        self.assertFalse(self.state.exists())

    def test_busy_os_lock_never_replaces_or_releases_record(self):
        path = self.record()
        before = path.read_bytes()
        self.live.return_value = common.DEAD
        with mock.patch.object(common, 'file_lock', side_effect=TimeoutError('held')):
            with self.assertRaises(seats.SeatBusy):
                seats.claim(1, 789, 1000)
            with self.assertRaises(seats.SeatBusy):
                seats.release_dead(1)
        self.assertEqual(path.read_bytes(), before)

    def test_concurrent_claims_admit_exactly_one_owner(self):
        barrier = threading.Barrier(2)
        outcomes = []

        def reserve(pid):
            barrier.wait(timeout=5)
            try:
                seats.claim(1, pid, pid + 100)
                outcomes.append('claimed')
            except seats.SeatBusy:
                outcomes.append('held')

        workers = [threading.Thread(target=reserve, args=(pid,)) for pid in (123, 234)]
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=10)
            self.assertFalse(worker.is_alive())
        self.assertEqual(sorted(outcomes), ['claimed', 'held'])
        self.assertIn(seats.status()[0]['pid'], (123, 234))


if __name__ == '__main__':
    unittest.main()
