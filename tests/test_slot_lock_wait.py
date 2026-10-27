"""Admission lock contention: isolated fake jobs only, no engines or real job state."""
import os
import threading
import time
from contextlib import contextmanager
from unittest import mock

import test_support  # noqa: F401; isolate before importing runtime modules
from test_overload import SlotQueue, LOW
from twinlane import common, hostload, jobs, observations


class LockWaiting(SlotQueue):
    def setUp(self):
        super().setUp()
        hostload.set_sampler(lambda: LOW)

    def status(self, jid):
        return common.read_json(os.path.join(common.JOBS, jid, 'status.json'), {}) or {}

    def test_legacy_timeout_stays_queued_then_proceeds(self):
        jid = self.waiter('20261026-100000-codex-legacy')
        real = jobs.slot_attempt
        seen = []

        def attempt(*args):
            if not seen:
                seen.append('timeout')
                raise TimeoutError('lock busy: slots.lock')
            self.assertEqual(self.status(jid)['queue_reason'], 'waiting for slot lock')
            return real(*args)

        with mock.patch.object(jobs, 'slot_attempt', side_effect=attempt):
            self.assertTrue(jobs.acquire_slot(jid, 'codex', 1, {}))
        self.assertEqual(self.status(jid)['status'], 'running')

    def test_wait_past_old_timeout_is_visible_and_then_starts(self):
        jid = self.waiter('20261026-100000-codex-wait')
        path = os.path.join(common.STATE, 'slots.lock')
        original = common.file_lock
        results, errors = [], []

        def shorter(path, timeout_s=120, *args, **kwargs):
            return original(path, 0.02, *args, **kwargs)

        def runner():
            try:
                results.append(jobs.acquire_slot(jid, 'codex', 1, {'jobs': {'lock_wait_s': 5}}))
            except Exception as error:
                errors.append(error)

        with original(path), mock.patch.object(common, 'file_lock', side_effect=shorter):
            thread = threading.Thread(target=runner)
            thread.start()
            deadline = time.monotonic() + 3
            while not self.status(jid).get('waiting_lock') and time.monotonic() < deadline:
                time.sleep(0.01)
            time.sleep(0.08)  # longer than the old (scaled) 20ms timeout
            seen = self.status(jid)
        thread.join(10)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(seen['status'], 'queued')
        self.assertEqual(seen['queue_reason'], 'waiting for slot lock')
        self.assertEqual(results, [True])
        self.assertEqual(self.status(jid)['status'], 'running')

    def test_overall_bound_reports_holder_and_age(self):
        jid = self.waiter('20261026-100000-codex-bound')
        path = os.path.join(common.STATE, 'slots.lock')
        with common.file_lock(path, wait_until=time.monotonic() + 5):
            with self.assertRaisesRegex(common.LockWaitTimeout, r'lock stayed busy for .* minutes: .*pid .*age'):
                jobs.acquire_slot(jid, 'codex', 1, {'jobs': {'lock_wait_s': 0.06}})
        self.assertEqual(self.status(jid)['queue_reason'], 'waiting for slot lock')
        self.assertFalse(os.path.exists(path + '.owner.json'))

    def test_writer_wait_is_visible_and_bound_is_configurable(self):
        jid = self.waiter('20261026-100000-codex-writer')
        self.write_config({'jobs': {'lock_wait_s': 0.06}})
        spec = common.read_json(os.path.join(common.JOBS, jid, 'job.json'))
        spec['profile'] = 'code'
        path = os.path.join(common.STATE, 'writers.lock')
        with common.file_lock(path):
            with self.assertRaises(common.LockWaitTimeout):
                jobs._claim_writer(spec)
        self.assertEqual(self.status(jid)['queue_reason'], 'waiting for writer lock')
        self.assertIsNotNone(jobs._claim_writer(spec))

    def test_new_identity_under_lock_retries_instead_of_overbooking(self):
        jid = self.waiter('20261026-100000-codex-candidate')
        original = jobs.slot_lock
        inserted = []

        @contextmanager
        def racing(*args, **kwargs):
            with original(*args, **kwargs):
                if not inserted:
                    other = self.runner('20261026-100001-codex-new')
                    st = self.status(other)
                    st.update(pid=987654, pid_created=123456)
                    common.write_json(os.path.join(common.JOBS, other, 'status.json'), st)
                    inserted.append(other)
                yield

        with mock.patch.object(jobs, 'slot_lock', racing), \
                mock.patch.object(jobs, '_probe_pid_state', return_value=common.ALIVE), observations.scope():
            self.assertEqual(jobs.slot_attempt(jid, 'codex', 1, 1, {}),
                             (False, 'slot observations changed; retrying admission'))
            self.assertFalse(jobs.slot_attempt(jid, 'codex', 1, 1, {})[0])
        self.assertEqual(self.status(jid)['status'], 'queued')

    def test_fourteen_runners_slow_liveness_fifo_and_priority(self):
        # Thirty folders: 16 dead former runners plus 14 live waiters. Each distinct
        # process observation costs 0.05s. All this work must happen OUTSIDE slots.lock.
        for n in range(16):
            jid = self.runner(f'20261026-090000-codex-dead{n}')
            st = self.status(jid)
            st.update(pid=100000 + n, pid_created=100000 + n)
            common.write_json(os.path.join(common.JOBS, jid, 'status.json'), st)
        ids = [self.waiter(f'20261026-1000{n:02d}-codex-w{n}',
                           kind='review' if n in (5, 11) else 'code',
                           since=f'2026-10-26T10:00:{n:02d}Z') for n in range(14)]
        expected = sorted(ids, key=jobs._own_place)
        order, errors, counts = [], [], []
        barrier, stop = threading.Barrier(14), threading.Event()
        original_write = jobs._write_status

        def probe(pid, created):
            if stop.is_set():
                raise RuntimeError('test deadline')
            self.assertFalse(jobs._SLOT_PROBES.get()['frozen'], 'liveness ran under slots.lock')
            if pid:
                time.sleep(0.05)
            return common.ALIVE if pid == os.getpid() else common.DEAD

        def write(jid, status, **extra):
            original_write(jid, status, **extra)
            if status == 'running':
                order.append(jid)  # called under the OS lock
                counts.append(sum(self.status(other).get('status') == 'running'
                                  and not os.path.exists(os.path.join(common.JOBS, other, 'result.json'))
                                  for other in ids))

        def runner(jid):
            try:
                barrier.wait(10)
                jobs.acquire_slot(jid, 'codex', 1, {'codex': {'max_parallel': 1}, 'jobs': {'lock_wait_s': 240}})
                common.write_json(os.path.join(common.JOBS, jid, 'result.json'), {'status': 'done'})
                original_write(jid, 'done', engine='codex')
            except Exception as error:
                errors.append((jid, repr(error)))

        started = time.monotonic()
        with mock.patch.object(jobs, '_probe_pid_state', side_effect=probe), \
                mock.patch.object(jobs, '_write_status', side_effect=write):
            threads = [threading.Thread(target=runner, args=(jid,)) for jid in reversed(ids)]
            for thread in threads:
                thread.start()
            deadline = started + 420
            for thread in threads:
                thread.join(max(0, deadline - time.monotonic()))
            stop.set()
            for thread in threads:
                thread.join(10)
            self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(order, expected)
        self.assertEqual(max(counts), 1)
        self.assertLess(time.monotonic() - started, 420)


    def fake_clock(self):
        """jobs.time with a patched monotonic/sleep so the bound can be crossed without waiting."""
        import types
        now = [0.0]
        fake = types.SimpleNamespace(**{k: getattr(time, k) for k in dir(time) if not k.startswith('_')})
        fake.monotonic = lambda: now[0]
        fake.sleep = lambda seconds: now.__setitem__(0, now[0] + seconds)
        return now, mock.patch.object(jobs, 'time', fake)

    def test_the_overall_bound_is_not_reset_by_restarted_scans(self):
        # bound 100 s. A lock wait begins at t=0 (a native extended wait sets the shared cell), the scan is then
        # invalidated at t=70, and the next attempt must still end at t=100, not at 70 + 100.
        jid = self.waiter('20261026-100000-codex-noreset')
        now, patched = self.fake_clock()
        deadlines = []

        def attempt(*args):
            deadlines.append(jobs._SLOT_DEADLINE.get())
            if len(deadlines) == 1:
                jobs._SLOT_STUCK.get()[0] = now[0]  # what _job_start_lock's on_wait does when the wait begins
                now[0] = 70.0
                return False, jobs.ADMISSION_CHANGED
            now[0] += 40.0
            return False, jobs.ADMISSION_CHANGED

        with patched, mock.patch.object(jobs, 'slot_attempt', side_effect=attempt):
            with self.assertRaisesRegex(common.LockWaitTimeout, 'slot admission stayed contended'):
                jobs.acquire_slot(jid, 'codex', 1, {'jobs': {'lock_wait_s': 100}})
        self.assertEqual(deadlines[0], None)
        self.assertEqual(deadlines[1:], [100.0] * (len(deadlines) - 1))  # one deadline from the FIRST stuck moment

    def test_a_normal_answer_ends_the_streak(self):
        jid = self.waiter('20261026-100000-codex-streak')
        now, patched = self.fake_clock()
        deadlines, answers = [], iter([TimeoutError('lock busy: slots.lock'), (False, 'engine slots full: 1/1'),
                                       (False, jobs.ADMISSION_CHANGED), (True, None)])

        def attempt(*args):
            deadlines.append(jobs._SLOT_DEADLINE.get())
            now[0] += 10.0
            answer = next(answers)
            if isinstance(answer, Exception):
                raise answer
            return answer

        with patched, mock.patch.object(jobs, 'slot_attempt', side_effect=attempt),                 mock.patch.object(jobs, 'SLOT_POLL_S', 0.0):
            jobs.acquire_slot(jid, 'codex', 1, {'jobs': {'lock_wait_s': 100}})
        # attempt 2 inherits the lock-busy streak from t=0; 'slots full' ends it; the restart at t=20 begins a new one
        self.assertEqual(deadlines, [None, 100.0, None, 120.0])

    def test_repeated_invalidation_ends_with_an_in_lock_attempt_that_admits(self):
        jid = self.waiter('20261026-100000-codex-starved')
        original, real_attempt = jobs.slot_lock, jobs.slot_attempt
        inserted, modes = [], []

        @contextmanager
        def racing(*args, **kwargs):
            with original(*args, **kwargs):
                if len(inserted) < 4:  # a new unseen identity appears between every outside scan and the lock
                    other = self.runner(f'20261026-10010{len(inserted)}-codex-new')
                    st = self.status(other)
                    st.update(pid=987650 + len(inserted), pid_created=123456 + len(inserted))
                    common.write_json(os.path.join(common.JOBS, other, 'status.json'), st)
                    inserted.append(other)
                yield

        def attempt(*args):
            modes.append(jobs._SLOT_INLOCK.get())
            return real_attempt(*args)

        policy = {'codex': {'max_parallel': 5}}  # four inserted runners plus this job: exactly at capacity
        with mock.patch.object(jobs, 'slot_lock', racing), mock.patch.object(jobs, 'slot_attempt', attempt),                 mock.patch.object(jobs, '_probe_pid_state', return_value=common.ALIVE),                 mock.patch.object(jobs, 'SLOT_POLL_S', 0.0), observations.scope():
            jobs.acquire_slot(jid, 'codex', 1, policy)
        self.assertEqual(modes, [False, False, False, True])
        self.assertEqual(self.status(jid)['status'], 'running')
        running = [j for j in os.listdir(common.JOBS) if self.status(j).get('status') == 'running']
        self.assertEqual(len(running), 5)

    def test_the_in_lock_fallback_stops_at_the_shared_bound_and_releases_the_lock(self):
        jid = self.waiter('20261026-100000-codex-slowfallback')
        now, patched = self.fake_clock()
        original = jobs.slot_lock
        inserted = []

        @contextmanager
        def racing(*args, **kwargs):
            with original(*args, **kwargs):
                if len(inserted) < 4:  # also inside the fallback attempt: it must probe that one identity under the lock
                    other = self.runner(f'20261026-10020{len(inserted)}-codex-new')
                    st = self.status(other)
                    st.update(pid=987600 + len(inserted), pid_created=123400 + len(inserted))
                    common.write_json(os.path.join(common.JOBS, other, 'status.json'), st)
                    inserted.append(other)
                yield

        def probe(pid, created):
            if (jobs._SLOT_PROBES.get() or {}).get('locked'):
                now[0] += 150.0  # a probe inside the lock is slow: the budget runs out while the lock is held
            return common.ALIVE

        with patched, mock.patch.object(jobs, 'slot_lock', racing), mock.patch.object(jobs, '_probe_pid_state', probe),                 mock.patch.object(jobs, 'SLOT_POLL_S', 0.0), observations.scope():
            with self.assertRaisesRegex(common.LockWaitTimeout, 'exceeded the lock-wait bound'):
                jobs.acquire_slot(jid, 'codex', 1, {'codex': {'max_parallel': 8}, 'jobs': {'lock_wait_s': 100}})
        self.assertNotEqual(self.status(jid)['status'], 'running')
        with common.file_lock(os.path.join(common.STATE, 'slots.lock'), 1):  # the failed attempt unwound the lock
            pass

    def test_the_in_lock_fallback_lists_jobs_fresh_inside_a_stale_observation_snapshot(self):
        jid = self.waiter('20261026-100000-codex-stalesnap')
        with mock.patch.object(jobs, '_probe_pid_state', return_value=common.ALIVE), observations.scope():
            jobs._job_ids()  # an enclosing command snapshot lists the folders now ...
            other = self.runner('20261026-100001-codex-admitted-later')  # ... and another runner is admitted after it
            st = self.status(other)
            st.update(pid=987700, pid_created=123700)
            common.write_json(os.path.join(common.JOBS, other, 'status.json'), st)
            restore = jobs._SLOT_INLOCK.set(True)
            try:
                started, why = jobs.slot_attempt(jid, 'codex', 1, 1, {})
            finally:
                jobs._SLOT_INLOCK.reset(restore)
        self.assertFalse(started)  # one slot, already taken by the runner the stale snapshot did not list
        self.assertEqual(why, 'engine slots full: 1/1')

    def test_a_first_attempt_is_bounded_by_its_own_lock_wait_deadline_too(self):
        jid = self.waiter('20261026-100000-codex-firstattempt')
        now, patched = self.fake_clock()
        other = self.runner('20261026-100003-codex-late')
        st = self.status(other)
        st.update(pid=987800, pid_created=123800)
        common.write_json(os.path.join(common.JOBS, other, 'status.json'), st)
        original = jobs.slot_lock

        @contextmanager
        def racing(*args, **kwargs):
            with original(*args, **kwargs):
                os.makedirs(os.path.join(common.JOBS, other), exist_ok=True)
                st2 = self.status(other)
                st2.update(pid=987801, pid_created=123801)  # appeared after the outside scan
                common.write_json(os.path.join(common.JOBS, other, 'status.json'), st2)
                yield

        def probe(pid, created):
            if (jobs._SLOT_PROBES.get() or {}).get('locked'):
                now[0] += 150.0
            return common.ALIVE

        self.assertIsNone(jobs._SLOT_DEADLINE.get())  # no streak deadline: only the lock wait's own deadline exists
        restore = jobs._SLOT_INLOCK.set(True)  # unfrozen snapshot: the unseen identity is probed under the lock
        try:
            with patched, mock.patch.object(jobs, 'slot_lock', racing), mock.patch.object(jobs, '_probe_pid_state', probe),                     mock.patch.object(jobs, '_lock_wait_seconds', return_value=100.0), observations.scope():
                with self.assertRaisesRegex(common.LockWaitTimeout, 'exceeded the lock-wait bound'):
                    jobs.slot_attempt(jid, 'codex', 1, 8, {})
        finally:
            jobs._SLOT_INLOCK.reset(restore)
        self.assertNotEqual(self.status(jid)['status'], 'running')

    def test_the_try_helper_also_stops_at_the_bound_before_writing_running(self):
        jid = self.waiter('20261026-100000-codex-tryhelper')
        now, patched = self.fake_clock()
        original = jobs.slot_lock

        @contextmanager
        def slow(*args, **kwargs):
            with original(*args, **kwargs):
                now[0] += 150.0  # the lock was held past the bound before the running status is written
                yield

        restore = jobs._SLOT_DEADLINE.set(now[0] + 100.0)
        try:
            with patched, mock.patch.object(jobs, 'slot_lock', slow), mock.patch.object(jobs, '_probe_pid_state', return_value=common.ALIVE), observations.scope():
                with self.assertRaisesRegex(common.LockWaitTimeout, 'exceeded the lock-wait bound'):
                    jobs.try_acquire_slot(jid, 'codex', 1, 8)
        finally:
            jobs._SLOT_DEADLINE.reset(restore)
        self.assertNotEqual(self.status(jid)['status'], 'running')

    def test_the_stale_writer_sweep_fails_fast_not_after_the_job_bound(self):
        self.write_config({'jobs': {'lock_wait_s': 600}})
        folder = os.path.join(common.STATE, 'writers')
        os.makedirs(folder, exist_ok=True)
        common.write_json(os.path.join(folder, 'x.json'), {'id': '20261026-100000-codex-gone'})
        self.assertEqual(jobs.WRITER_SWEEP_WAIT_S, 30)
        started = time.monotonic()
        with common.file_lock(os.path.join(common.STATE, 'writers.lock')),                 mock.patch.object(jobs, 'WRITER_SWEEP_WAIT_S', 0.1):
            with self.assertRaises(common.LockWaitTimeout):
                jobs.release_stale_writers(dry_run=True)
        self.assertLess(time.monotonic() - started, 10)


if __name__ == '__main__':
    import unittest
    unittest.main()
