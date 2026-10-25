"""Overload handling: host-load admission, FIFO slot order, the hung-job watchdog and stale writer custody."""
import json
import os
import subprocess
import sys
import threading
import time
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate before importing runtime modules
import test_board as tb
import test_jobs as tj
from twinlane import common, hostload, jobs, watchdog, wip

LOW = {'cpu_pct': 20.0, 'ram_free_mb': 9000, 'engine_procs': 3}


def busy(cpu=97.0, ram=9000, procs=3):
    return {'cpu_pct': cpu, 'ram_free_mb': ram, 'engine_procs': procs}


class HostLoad(unittest.TestCase):
    def test_idle_host_keeps_every_slot(self):
        self.assertEqual(hostload.effective_parallel(14, LOW), (14, None))

    def test_unreadable_sensors_never_throttle(self):
        for reading in (None, {}, {'cpu_pct': None, 'ram_free_mb': None, 'engine_procs': None}):
            self.assertEqual(hostload.effective_parallel(14, reading), (14, None))

    def test_cpu_saturation_lowers_slots_and_names_the_reason(self):
        usable, why = hostload.effective_parallel(14, busy(cpu=97))
        self.assertLess(usable, 14)
        self.assertGreaterEqual(usable, 2)
        self.assertEqual(why, 'host busy: cpu 97%')

    def test_slots_fall_as_load_rises_and_recover_as_it_falls(self):
        seen = [hostload.effective_parallel(14, busy(cpu=c))[0] for c in (50, 80, 85, 90, 95, 99, 90, 85, 80, 50)]
        self.assertEqual(seen[0], 14)
        self.assertEqual(seen[:6], sorted(seen[:6], reverse=True))
        self.assertEqual(seen[5], 2)  # the floor, never zero
        self.assertEqual(seen[6:], sorted(seen[6:]))
        self.assertEqual(seen[-1], 14)

    def test_min_parallel_is_a_floor_even_at_total_saturation(self):
        for cpu in (100, 100.0):
            self.assertEqual(hostload.effective_parallel(14, busy(cpu=cpu, ram=50, procs=99))[0], 2)
        policy = {'hostload': {'min_parallel': 5}}
        self.assertEqual(hostload.effective_parallel(14, busy(cpu=100), policy)[0], 5)
        self.assertEqual(hostload.effective_parallel(3, busy(cpu=100), {'hostload': {'min_parallel': 5}})[0], 3)

    def test_low_ram_and_many_engine_processes_are_reasons_too(self):
        usable, why = hostload.effective_parallel(14, busy(cpu=10, ram=700))
        self.assertLess(usable, 14)
        self.assertEqual(why, 'host busy: free RAM 700 MB')
        usable, why = hostload.effective_parallel(14, busy(cpu=10, procs=28))
        self.assertLess(usable, 14)
        self.assertEqual(why, 'host busy: 28 codex/sandbox processes')

    def test_thresholds_come_from_the_config(self):
        policy = {'hostload': {'cpu_soft': 30, 'cpu_hard': 40, 'min_parallel': 1}}
        self.assertEqual(hostload.effective_parallel(14, busy(cpu=45), policy)[0], 1)
        self.assertEqual(hostload.effective_parallel(14, busy(cpu=45), {'hostload': {'enabled': False}})[0], 14)

    def test_bad_config_values_fall_back_to_the_defaults(self):
        policy = {'hostload': {'cpu_soft': 'x', 'cpu_hard': -5, 'min_parallel': True, 'enabled': 'no'}}
        self.assertEqual(hostload.settings(policy), hostload.settings(None))

    def test_disabled_or_isolated_sampling_reads_nothing(self):
        self.assertIsNone(hostload.sample({'hostload': {'enabled': False}}))
        self.assertIsNone(hostload.sample({}))  # test isolation: nothing is measured unless a test installs a sampler

    def test_installed_sampler_is_used_and_cleared(self):
        hostload.set_sampler(lambda: busy())
        try:
            self.assertEqual(hostload.sample({})['cpu_pct'], 97.0)
        finally:
            hostload.set_sampler(None)
        self.assertIsNone(hostload.sample({}))

    def test_a_zero_ram_threshold_cannot_reach_a_division_by_zero(self):
        for soft in (0, -1, 0.5):
            cfg = hostload.settings({'hostload': {'ram_free_soft_mb': soft, 'ram_free_hard_mb': 0}})
            self.assertGreater(cfg['ram_free_soft_mb'], cfg['ram_free_hard_mb'])
            usable, _ = hostload.effective_parallel(14, busy(cpu=10, ram=100), {'hostload': {'ram_free_soft_mb': soft}})
            self.assertGreaterEqual(usable, 2)

    def test_disabled_admission_ignores_even_a_saturated_reading(self):
        off = {'hostload': {'enabled': False}}
        self.assertEqual(hostload.effective_parallel(14, busy(cpu=100, ram=10, procs=99), off), (14, None))

    def test_hosts_without_proc_get_sensors_from_loadavg_vm_stat_and_ps(self):
        vm = ('Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free:      1000.\n'
              'Pages active:    5000.\nPages inactive:  3000.\nPages speculative: 200.\n')
        ps = '/usr/bin/login\n/Applications/Codex.app/Contents/MacOS/codex\ncodex\n-zsh\n'

        def run(argv):
            return vm if argv[0] == 'vm_stat' else ps

        def no_proc(*a, **k):
            raise OSError('no /proc here')

        with mock.patch.object(hostload, '_run', run), mock.patch.object(hostload.common, 'IS_WIN', False), \
                mock.patch.object(hostload, '_posix_times', no_proc), \
                mock.patch.object(hostload, '_posix_free_mb', no_proc), \
                mock.patch.object(hostload, '_posix_engine_procs', no_proc), \
                mock.patch.object(hostload.sys, 'platform', 'darwin'), \
                mock.patch('os.getloadavg', create=True, return_value=(3.0, 2.0, 1.0)), \
                mock.patch.object(hostload.os, 'cpu_count', return_value=4):
            got = hostload.measure()
        self.assertEqual(got['cpu_pct'], 75.0)
        self.assertEqual(got['ram_free_mb'], (1000 + 3000 + 200) * 16384 // (1024 * 1024))
        self.assertEqual(got['engine_procs'], 2)

    def test_board_line(self):
        text = hostload.line(busy(cpu=97, ram=1500), 14)
        self.assertIn('cpu 97%', text)
        self.assertIn('free RAM 1.5 GB', text)
        self.assertRegex(text, r'slots \d+/14 \(host busy: cpu 97%\)')
        self.assertIn('unknown', hostload.line(None, 14))
        self.assertIn('slots 14/14', hostload.line(LOW, 14))


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


class Watchdog(unittest.TestCase):
    def setUp(self):
        self.dir = test_support._HOME
        self.path = os.path.join(self.dir, f'wd-{time.monotonic_ns()}.log')
        with open(self.path, 'w') as h:
            h.write('start')

    def test_no_progress_trips_after_the_limit_and_names_the_idle_time(self):
        clock = Clock()
        stall = watchdog.Stall([self.path], 300, clock)
        clock.t = 299
        self.assertFalse(stall.check())
        clock.t = 301
        self.assertTrue(stall.check())
        self.assertTrue(stall.tripped)
        self.assertEqual(stall.reason(), 'no output for 5 min (limit 5 min)')

    def test_output_resets_the_clock(self):
        clock = Clock()
        stall = watchdog.Stall([self.path], 300, clock)
        clock.t = 250
        with open(self.path, 'a') as h:
            h.write('more')
        self.assertFalse(stall.check())
        clock.t = 540
        self.assertFalse(stall.check())  # 290 s since the last growth
        clock.t = 560
        self.assertTrue(stall.check())

    def test_a_missing_file_is_no_progress_not_an_error(self):
        clock = Clock()
        stall = watchdog.Stall([self.path + '.none'], 10, clock)
        clock.t = 11
        self.assertTrue(stall.check())

    def test_settings_validate_and_can_disable(self):
        self.assertEqual(watchdog.settings(None), watchdog.DEFAULTS)
        self.assertEqual(watchdog.settings({'watchdog': {'stall_s': 60, 'poll_s': 'x'}})['stall_s'], 60)
        self.assertEqual(watchdog.settings({'watchdog': {'stall_s': -1}})['stall_s'], watchdog.DEFAULTS['stall_s'])
        self.assertIsNone(watchdog.make([self.path], {'watchdog': {'enabled': False}}))

    def test_wait_without_a_watchdog_is_proc_wait(self):
        proc = mock.Mock()
        proc.wait.return_value = 7
        self.assertEqual(watchdog.wait(proc, 5, None), 7)
        proc.wait.assert_called_once_with(timeout=5)

    def test_wait_kills_the_tree_on_a_stall_and_marks_it(self):
        clock = Clock()
        stall = watchdog.Stall([self.path], 1, clock)
        proc = mock.Mock(pid=4242)
        calls = []

        def wait(timeout):
            calls.append(timeout)
            if len(calls) == 1:
                time.sleep(timeout)  # a real wait blocks for its slice
                clock.t = 5
                raise subprocess.TimeoutExpired('x', timeout)
            return -9

        proc.wait.side_effect = wait
        killed = []
        rc = watchdog.wait(proc, 60, stall, poll_s=0.01, kill=lambda *a: killed.append(a) or True, child_created=77)
        self.assertEqual(rc, -9)
        self.assertTrue(stall.tripped)
        self.assertEqual(killed, [(4242, 77)])

    def test_a_kill_that_does_not_report_success_is_recorded_whatever_the_root_does(self):
        for report, expected in ((False, False), (None, False), (True, True)):
            clock = Clock()
            stall = watchdog.Stall([self.path], 1, clock)
            proc = mock.Mock(pid=9)
            seen = []

            def wait(timeout, clock=clock, seen=seen):
                seen.append(timeout)
                if len(seen) == 1:
                    time.sleep(timeout)
                    clock.t = 5
                    raise subprocess.TimeoutExpired('x', timeout)
                return 0  # the root exits cleanly; its children are another matter

            proc.wait.side_effect = wait
            self.assertEqual(watchdog.wait(proc, 60, stall, poll_s=0.01, kill=lambda *a, r=report: r), 0)
            self.assertIs(stall.kill_confirmed, expected)

    def test_a_wait_that_gives_up_early_is_its_deadline_not_a_slice(self):
        proc = mock.Mock(pid=1)
        proc.wait.side_effect = subprocess.TimeoutExpired('x', 15)  # raises at once, whatever it was asked
        stall = watchdog.Stall([self.path], 3600, Clock())
        began = time.monotonic()
        with self.assertRaises(subprocess.TimeoutExpired):
            watchdog.wait(proc, 3600, stall, poll_s=15)
        self.assertLess(time.monotonic() - began, 5)  # no spinning until the real deadline
        self.assertEqual(proc.wait.call_count, 1)

    def test_wait_still_raises_timeout_at_the_deadline(self):
        proc = mock.Mock(pid=1)
        proc.wait.side_effect = subprocess.TimeoutExpired('x', 0.01)
        stall = watchdog.Stall([self.path], 3600, Clock())
        with self.assertRaises(subprocess.TimeoutExpired):
            watchdog.wait(proc, 0.05, stall, poll_s=0.01)
        self.assertFalse(stall.tripped)


class SlotQueue(tj.JobsBase):
    """Slot admission: host load lowers the usable slots; waiting jobs start first-in-first-out, reviews first."""

    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)

    def waiter(self, jid, kind='code', since='2026-10-24T10:00:00Z', engine='codex'):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d, exist_ok=True)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'name': jid, 'kind': kind, 'cwd': self.work,
                                                        'primary': {'engine': engine, 'model': 'm'}})
        common.write_json(os.path.join(d, 'status.json'), {
            'status': 'queued', 'waiting_slot': True, 'engine': engine, 'pid': os.getpid(),
            'pid_created': jobs._self_created(), 'waiting_since': since,
            'queue_priority': jobs.queue_priority(kind)})
        return jid

    def runner(self, jid, engine='codex'):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d, exist_ok=True)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'name': jid, 'kind': 'code', 'cwd': self.work,
                                                        'primary': {'engine': engine, 'model': 'm'}})
        common.write_json(os.path.join(d, 'status.json'), {'status': 'running', 'engine': engine,
                                                           'pid': os.getpid(), 'pid_created': jobs._self_created()})
        return jid

    def test_load_lowers_the_usable_slots_and_the_job_queues_with_a_reason(self):
        hostload.set_sampler(lambda: busy(cpu=99))
        for n in range(2):
            self.runner(f'20261024-100000-codex-r{n}')
        me = self.waiter('20261024-100001-codex-me')
        got, why = jobs.slot_attempt(me, 'codex', 1, 14, {})
        self.assertFalse(got)
        self.assertEqual(why, 'host busy: cpu 99%')

    def test_the_same_job_starts_once_the_load_falls(self):
        for n in range(2):
            self.runner(f'20261024-100000-codex-r{n}')
        me = self.waiter('20261024-100001-codex-me')
        hostload.set_sampler(lambda: busy(cpu=99))
        self.assertFalse(jobs.slot_attempt(me, 'codex', 1, 14, {})[0])
        hostload.set_sampler(lambda: LOW)
        got, why = jobs.slot_attempt(me, 'codex', 1, 14, {})
        self.assertTrue(got, why)
        self.assertEqual(common.read_json(os.path.join(common.JOBS, me, 'status.json'))['status'], 'running')

    def test_a_busy_host_never_blocks_an_idle_engine(self):
        hostload.set_sampler(lambda: busy(cpu=100, ram=10, procs=99))
        me = self.waiter('20261024-100001-codex-me')
        self.assertTrue(jobs.slot_attempt(me, 'codex', 1, 14, {})[0])

    def test_no_load_reading_changes_nothing(self):
        hostload.set_sampler(lambda: None)
        for n in range(3):
            self.runner(f'20261024-100000-codex-r{n}')
        me = self.waiter('20261024-100001-codex-me')
        self.assertTrue(jobs.slot_attempt(me, 'codex', 1, 4, {})[0])
        self.assertEqual(jobs.slot_attempt('20261024-100002-codex-x', 'codex', 1, 4, {}), (False, 'engine slots full: 4/4'))

    def test_waiters_start_in_arrival_order(self):
        a = self.waiter('20261024-100000-codex-aaaa', since='2026-10-24T10:00:00Z')
        b = self.waiter('20261024-100001-codex-bbbb', since='2026-10-24T10:00:05Z')
        got, why = jobs.slot_attempt(b, 'codex', 1, 1, {})
        self.assertFalse(got)
        self.assertEqual(why, 'queued behind 1 job')
        self.assertTrue(jobs.slot_attempt(a, 'codex', 1, 1, {})[0])

    def test_a_review_goes_ahead_of_earlier_work(self):
        a = self.waiter('20261024-100000-codex-aaaa', kind='code', since='2026-10-24T10:00:00Z')
        r = self.waiter('20261024-100009-codex-rrrr', kind='review', since='2026-10-24T10:00:09Z')
        self.assertFalse(jobs.slot_attempt(a, 'codex', 1, 1, {})[0])
        self.assertTrue(jobs.slot_attempt(r, 'codex', 1, 1, {})[0])

    def test_a_dead_waiter_does_not_hold_the_line(self):
        a = self.waiter('20261024-100000-codex-aaaa', since='2026-10-24T10:00:00Z')
        st = common.read_json(os.path.join(common.JOBS, a, 'status.json'))
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        st.update(pid=p.pid, pid_created=None)
        common.write_json(os.path.join(common.JOBS, a, 'status.json'), st)
        b = self.waiter('20261024-100001-codex-bbbb', since='2026-10-24T10:00:05Z')
        self.assertTrue(jobs.slot_attempt(b, 'codex', 1, 1, {})[0])

    def test_other_engines_queues_are_independent(self):
        self.waiter('20261024-100000-claude-aaaa', engine='claude', since='2026-10-24T09:00:00Z')
        b = self.waiter('20261024-100001-codex-bbbb')
        self.assertTrue(jobs.slot_attempt(b, 'codex', 1, 1, {})[0])

    def test_acquire_slot_waits_then_starts_when_the_load_falls(self):
        for n in range(2):
            self.runner(f'20261024-100000-codex-r{n}')
        me = self.waiter('20261024-100001-codex-me')
        load = {'v': busy(cpu=99)}
        hostload.set_sampler(lambda: load['v'])
        out = {}
        t = threading.Thread(target=lambda: out.setdefault('waited', jobs.acquire_slot(me, 'codex', 1, {'codex': {'max_parallel': 14}})))
        t.start()
        deadline = time.monotonic() + 10
        st = {}
        while time.monotonic() < deadline:
            st = common.read_json(os.path.join(common.JOBS, me, 'status.json'), {}) or {}
            if st.get('queue_reason'):
                break
            time.sleep(0.05)
        self.assertEqual(st.get('status'), 'queued')
        self.assertEqual(st.get('queue_reason'), 'host busy: cpu 99%')
        load['v'] = LOW
        t.join(15)
        self.assertFalse(t.is_alive())
        self.assertTrue(out['waited'])
        self.assertEqual(common.read_json(os.path.join(common.JOBS, me, 'status.json'))['status'], 'running')


class StallFlow(tj.JobsBase):
    """A hung worker is killed, requeued once, then failed as host overload; it never reads as a verdict."""

    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)
        os.environ['TWINLANE_CONFIG'] = os.path.join(self.tmp, 'config.json')
        common.write_json(os.environ['TWINLANE_CONFIG'], {'watchdog': {'stall_s': 1.5, 'poll_s': 0.2}})

    def confirmed_kill(self):
        """The retry flow needs a stall kill that is reported confirmed; POSIX never reports one (round 13), so the
        retry tests state it explicitly instead of inheriting it from the platform."""
        patcher = mock.patch.object(jobs, '_watchdog_kill', return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_two_stalls_fail_the_job_as_host_overload(self):
        self.confirmed_kill()
        os.environ['FAKE_SLEEP'] = '120'
        t = time.monotonic()
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        self.assertLess(time.monotonic() - t, 60)
        self.assertEqual(rc, common.EXIT_FAILED, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('host overload', r['error'])
        self.assertEqual([a['status'] for a in r['attempts']], ['stalled', 'failed'])
        self.assertTrue(r['attempts'][0]['requeued'])
        self.assertTrue(any('stalled' in n and 'requeued' in n for n in r['notes']), r['notes'])
        self.assertIsNone(r.get('final_path'))
        self.assertNotIn(r['status'], ('done', 'pass', 'block'))

    def _unsure_run(self):
        os.environ['FAKE_SLEEP'] = '120'
        real = common.kill_tree

        def unsure(*a, **k):
            real(*a, **k)
            return False  # the kill did not report that the whole tree is gone

        with mock.patch.object(common, 'kill_tree', unsure):
            rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        return rc, out, self.only_job()

    def test_an_unwritable_unresolved_marker_keeps_the_launch_marker_and_the_claim(self):
        real = jobs._write_job_json

        def refuse(job_id, name, data):
            if name == jobs.TREE_UNRESOLVED:
                raise PermissionError('read-only')
            return real(job_id, name, data)

        with mock.patch.object(jobs, '_write_job_json', refuse), mock.patch.object(jobs.time, 'sleep', lambda s: None):
            rc, out, jid = self._unsure_run()
        self.assertFalse(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))  # the earlier uncertainty is not cleared
        self.assertTrue(jobs._tree_unresolved(jid))
        self.assertNotIn('pid', common.read_json(jobs._p(jid, jobs.CHILD_STARTING)))  # nothing a root-pid check can reap
        self.assertTrue(jobs._writer_job_busy(jid))
        self.assertIsNone(jobs._custody_stale(jid))
        jobs.running_count('claude')  # another runner's slot scan, and a continuation check, reap dead-root markers
        jobs.running_count('codex')
        jobs._child_pending(jid)
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))
        self.assertTrue(jobs._writer_job_busy(jid))
        rc, out, _ = self.cli('stop', jid, '--tree-gone')
        self.assertEqual(rc, 0, out)
        self.assertFalse(jobs._writer_job_busy(jid))

    def test_a_tree_kill_that_is_not_confirmed_is_never_retried(self):
        rc, out, jid = self._unsure_run()
        r = self.receipt(jid)
        self.assertEqual(rc, common.EXIT_FAILED, out)
        self.assertEqual([a['status'] for a in r['attempts']], ['failed'])  # one attempt: nothing started on top
        self.assertIn('not proven stopped', r['attempts'][0]['error'])
        self.assertIn('host overload', r['attempts'][0]['error'])
        self.assertNotIn(r['status'], ('done', 'pass', 'block'))

    def test_an_unconfirmed_kill_keeps_the_worktree_claim_until_a_person_clears_it(self):
        rc, out, jid = self._unsure_run()
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertTrue(jobs._writer_job_busy(jid))  # the next writer on this worktree is refused
        self.assertIsNone(jobs._custody_stale(jid))  # and doctor --fix will not release it
        rc, out, _ = self.cli('stop', jid, '--tree-gone')
        self.assertEqual(rc, 0, out)
        self.assertFalse(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertFalse(jobs._writer_job_busy(jid))
        log = os.path.join(common.STATE, 'custody-releases.jsonl')
        with open(log, encoding='utf-8') as handle:
            self.assertIn(jid, handle.read())

    def test_an_unresolved_tree_reads_as_live_to_every_consumer(self):
        rc, out, jid = self._unsure_run()
        st = common.read_json(jobs._p(jid, 'status.json')) or {}
        self.assertTrue(jobs._child_pending(jid))
        self.assertNotEqual(jobs._engine_state(jid, st), common.DEAD)  # archive and worktree checks stop here
        self.assertEqual(jobs._receipt_status(jid, self.receipt(jid)), 'unknown')
        engine = self.receipt(jid)['attempts'][0]['engine']
        self.assertGreaterEqual(jobs.running_count(engine), 1)  # it keeps spending a slot
        self.assertEqual(jobs.running_count('codex' if engine == 'claude' else 'claude'), 0)
        rc, out, _ = self.cli('stop', jid, '--tree-gone')
        self.assertEqual(rc, 0, out)
        self.assertFalse(jobs._child_pending(jid))
        self.assertEqual(jobs.running_count(engine), 0)

    def test_a_retry_that_cannot_start_ends_the_job_failed_not_stalled(self):
        self.confirmed_kill()
        os.environ['FAKE_SLEEP'] = '120'
        real = jobs.acquire_slot
        calls = []

        def second_fails(job_id, engine, attempt, policy):
            calls.append(attempt)
            if attempt == 2:
                raise OSError('state disk went away')
            return real(job_id, engine, attempt, policy)

        with mock.patch.object(jobs, 'acquire_slot', second_fails):
            rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        r = self.receipt(self.only_job())
        self.assertEqual(calls, [1, 2])
        self.assertEqual(r['status'], 'failed')
        self.assertIn(r['status'], jobs.TERMINAL)
        self.assertEqual([a['status'] for a in r['attempts']], ['failed'])
        self.assertIn('stalled', r['attempts'][0]['error'])  # the stall evidence is kept
        self.assertIn('retry could not start', r['attempts'][0]['error'])

    def test_an_interrupt_after_an_unconfirmed_stall_kill_still_holds_the_claim(self):
        os.environ['FAKE_SLEEP'] = '120'

        def interrupted_in_the_grace_wait(proc, timeout_s, stall, *a, **k):
            common.kill_tree(proc.pid)  # the root is gone; the tree kill did not report success; then Ctrl+C
            stall.tripped, stall.kill_confirmed = True, False
            raise KeyboardInterrupt()

        with mock.patch.object(watchdog, 'wait', interrupted_in_the_grace_wait):
            self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        jid = self.only_job()
        self.assertTrue(jobs._tree_unresolved(jid))
        self.assertTrue(jobs._writer_job_busy(jid))
        self.assertIsNone(jobs._custody_stale(jid))
        self.assertTrue(jobs._child_pending(jid))

    def test_the_second_admission_names_the_request_the_first_one_did(self):
        argv = ['run', '--write', '--cd', self.work, '--brief', self.brief]
        policy = {'wip': {'max_writers': 1}}
        row = dict(self.row_for(argv), pumping_at=common.iso(common.now()), claim='tok')
        with mock.patch.object(wip, 'collect', return_value=[]):
            rid = wip.check(self.work, 'run', 'x', policy, argv=argv)  # plan_run's check admits it
        row['id'] = rid
        wip.save_queue([row])
        jobs._WIP_REQUEST.set((rid, argv))
        # another run fills the only lane before create_job's own check
        with mock.patch.object(wip, 'collect', return_value=[{'worktree': 'elsewhere'}]), \
                mock.patch.object(common, 'load_policy', return_value=policy), \
                mock.patch.dict(os.environ, {wip.CLAIM_ENV: 'tok'}):
            with self.assertRaises(jobs.Refusal):
                jobs.create_job({'write': True, 'cwd': self.work, 'name': 'x'})
        rows = wip.queue()
        self.assertEqual([r['id'] for r in rows], [rid])  # no second, placeholder row
        self.assertEqual(rows[0]['argv'], argv)
        self.assertNotIn('pumping_at', rows[0])  # the claim went back with the request

    def row_for(self, argv):
        return {'kind': 'run', 'name': 'x', 'worktree': common.writer_key(self.work), 'argv': argv,
                'queued_at': common.iso(common.now())}

    def test_a_worker_cannot_release_custody_with_tree_gone(self):
        rc, out, jid = self._unsure_run()
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'x'}):
            rc, out, _ = self.cli('stop', jid, '--tree-gone')
        self.assertEqual(rc, common.EXIT_REFUSED, out)
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))  # the claim is still held

    def test_tree_gone_with_nothing_to_clear_changes_nothing(self):
        os.environ['FAKE_SLEEP'] = '0'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        jid = self.only_job()
        rc, out, _ = self.cli('stop', jid, '--tree-gone', '--json')
        self.assertEqual(rc, 0, out)
        self.assertFalse(json.loads(out)['cleared'])

    def test_a_stall_never_makes_a_third_attempt(self):
        starts = []

        def attempt(job_id, n, *a, **k):
            starts.append(n)
            return {'n': n, 'engine': 'claude', 'status': 'stalled',
                    'error': 'stalled: no output for 1 min (limit 1 min)', 'stall_kill_confirmed': True,
                    'started_at': common.iso(common.now()), 'exit_code': None}

        with mock.patch.object(jobs, 'run_attempt', attempt):
            self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        self.assertEqual(starts, [1, 2])
        self.assertLessEqual(max(starts), jobs.MAX_ATTEMPTS)

    def test_a_survivor_after_a_stall_kill_blocks_the_retry(self):
        starts = []

        def attempt(job_id, n, *a, **k):
            starts.append(n)
            return {'n': n, 'engine': 'claude', 'status': 'stalled',
                    'error': 'stalled: no output for 1 min (limit 1 min)', 'stall_kill_confirmed': True,
                    'started_at': common.iso(common.now()), 'exit_code': None}

        alive = {'engine': 'claude', 'pid': os.getpid(), 'created': common.process_created(os.getpid()),
                 'why': 'survived', 'at': common.iso(common.now())}
        with mock.patch.object(jobs, 'run_attempt', attempt), mock.patch.object(jobs, '_survivor', return_value=alive):
            self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        self.assertEqual(starts, [1])

    @unittest.skipIf(common.IS_WIN, 'POSIX never reports a stall kill confirmed')
    def test_r14_posix_stall_ends_after_one_attempt_with_the_claim_held(self):
        os.environ['FAKE_SLEEP'] = '120'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        jid = self.only_job()
        r = self.receipt(jid)
        self.assertEqual(rc, common.EXIT_FAILED, out)
        self.assertEqual([a['status'] for a in r['attempts']], ['failed'])  # no automatic retry
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertTrue(jobs._writer_job_busy(jid))

    def test_a_stall_then_a_healthy_retry_finishes(self):
        self.confirmed_kill()
        marker = os.path.join(test_support._HOME, 'stall-once')
        os.environ['FAKE_SLEEP_ONCE'] = marker
        os.environ['FAKE_SLEEP'] = '120'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review')
        self.assertEqual(rc, 0, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'done')
        self.assertEqual([a['status'] for a in r['attempts']], ['stalled', 'done'])

    def test_the_watchdog_can_be_switched_off(self):
        common.write_json(os.environ['TWINLANE_CONFIG'], {'watchdog': {'enabled': False}})
        os.environ['FAKE_SLEEP'] = '4'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'review', '--timeout', '30')
        self.assertEqual(rc, 0, out)


class StaleCustody(tj.JobsBase):
    """A finished job must not keep its worktree: the claim is released, with a record, once it is provably stale."""

    JID = '20261024-163508-codex-251f'

    def finished_job(self, jid=None, ended_ago_s=600, finalized=True, status='done', child=True):
        jid = jid or self.JID
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d, exist_ok=True)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'name': jid, 'kind': 'code', 'cwd': self.work,
                                                        'profile': 'code', 'primary': {'engine': 'codex'}})
        ended = common.iso(common.now() - __import__('datetime').timedelta(seconds=ended_ago_s))
        st = {'status': status, 'pid': 76640, 'pid_created': 134373333099975784, 'updated_at': ended,
              'engine': 'codex', 'runner_finalized': finalized}
        if child:
            st.update(child_pid=70884, child_created=134373333330823142)
        common.write_json(os.path.join(d, 'status.json'), st)
        common.write_json(os.path.join(d, 'result.json'), {'id': jid, 'status': status, 'ended_at': ended})
        return jid

    def claim_file(self, jid):
        path = jobs._writer_path(self.work)
        common.write_json(path, {'id': jid, 'cwd': jobs._writer_cwd(self.work), 'pid': 76640,
                                 'created': 134373333099975784})
        return path

    def spec(self, jid='20261024-190000-codex-new1'):
        return {'id': jid, 'profile': 'code', 'cwd': self.work}

    def states(self, runner, child):
        order = iter([runner, child])
        return mock.patch.object(jobs, '_pid_state', side_effect=lambda *a: next(order, common.DEAD))

    def test_unreadable_pids_of_a_long_finished_job_no_longer_block_the_next_writer(self):
        self.claim_file(self.finished_job())
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            path = jobs._claim_writer(self.spec())
        self.assertEqual(common.read_json(path)['id'], '20261024-190000-codex-new1')
        with open(os.path.join(common.STATE, 'custody-releases.jsonl'), encoding='utf-8') as h:
            log = [json.loads(x) for x in h]
        self.assertEqual(log[0]['job'], self.JID)
        self.assertIn('finalized', log[0]['why'])

    def test_a_job_that_finished_moments_ago_still_holds_the_claim(self):
        self.claim_file(self.finished_job(ended_ago_s=5))
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            with self.assertRaises(jobs.Refusal) as ctx:
                jobs._claim_writer(self.spec())
        self.assertIn('live or unknown', ctx.exception.message)

    def test_a_job_that_never_finalized_is_not_released_on_unknown_pids(self):
        self.claim_file(self.finished_job(finalized=False))
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(self.spec())

    def test_a_provably_alive_owner_is_never_released(self):
        self.claim_file(self.finished_job())
        with mock.patch.object(jobs, '_pid_state', return_value=common.ALIVE):
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(self.spec())

    def test_a_job_without_a_terminal_receipt_is_never_released(self):
        jid = self.finished_job()
        os.remove(os.path.join(common.JOBS, jid, 'result.json'))
        self.claim_file(jid)
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(self.spec())

    def test_a_survivor_marker_keeps_the_claim(self):
        jid = self.finished_job()
        common.write_json(os.path.join(common.JOBS, jid, jobs.SURVIVOR), {'engine': 'codex', 'pid': 1, 'created': 1})
        self.claim_file(jid)
        self.assertIsNone(jobs._custody_stale(jid))

    def test_release_stale_writers_dry_run_then_apply(self):
        path = self.claim_file(self.finished_job())
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            rows = jobs.release_stale_writers(dry_run=True)
            self.assertEqual([(r['id'], r['released']) for r in rows], [(self.JID, False)])
            self.assertTrue(os.path.exists(path))
            rows = jobs.release_stale_writers()
        self.assertEqual([(r['id'], r['released']) for r in rows], [(self.JID, True)])
        self.assertFalse(os.path.exists(path))
        self.assertTrue(os.path.exists(os.path.join(common.STATE, 'custody-releases.jsonl')))

    def test_a_claim_of_a_vanished_job_with_a_dead_runner_is_released(self):
        path = jobs._writer_path(self.work)
        common.write_json(path, {'id': '20261001-000000-codex-gone', 'cwd': jobs._writer_cwd(self.work),
                                 'pid': 76640, 'created': 1})
        with mock.patch.object(jobs, '_pid_state', return_value=common.DEAD):
            self.assertEqual(len(jobs.release_stale_writers()), 1)
        self.assertFalse(os.path.exists(path))
        common.write_json(path, {'id': '20261001-000000-codex-gone', 'cwd': jobs._writer_cwd(self.work),
                                 'pid': 76640, 'created': 1})
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            self.assertEqual(jobs.release_stale_writers(), [])
        self.assertTrue(os.path.exists(path))

    def test_wt_doctor_fix_releases_and_plain_doctor_only_reports(self):
        import io
        from twinlane import worktrees
        path = self.claim_file(self.finished_job())
        buf = io.StringIO()
        report = {'repo': 'r'}
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN), \
                mock.patch.object(worktrees, 'doctor', return_value=report), \
                mock.patch.object(worktrees, 'render_doctor', return_value='REPORT'), \
                mock.patch('sys.stdout', buf):
            self.assertEqual(worktrees.cli(['doctor']), 0)
            self.assertTrue(os.path.exists(path))
            self.assertIn('1 stale writer claim', buf.getvalue())
            self.assertIn('wt doctor --fix', buf.getvalue())
            self.assertEqual(worktrees.cli(['doctor', '--fix']), 0)
        self.assertFalse(os.path.exists(path))
        self.assertIn('released 1 stale writer claim', buf.getvalue())
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'x'}):
            self.assertEqual(worktrees.cli(['doctor', '--fix']), common.EXIT_REFUSED)



class BoardQueue(tb.BoardBase):
    """The board shows the host load and every queued job with the reason it waits."""

    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)
        self.write_meter()

    def queued(self, jid, reason, since_ago, kind='code', priority=1):
        d = self.add_job(jid, 'queued', engine='codex', kind=kind)
        common.write_json(os.path.join(d, 'status.json'), {
            'status': 'queued', 'waiting_slot': True, 'engine': 'codex', 'pid': os.getpid(),
            'queue_reason': reason, 'queue_priority': priority,
            'waiting_since': tb.iso_ago(since_ago)})

    def test_json_text_and_markdown_carry_the_host_line_and_the_queue(self):
        hostload.set_sampler(lambda: busy(cpu=97))
        self.queued('20261024-100000-codex-aaaa', 'host busy: cpu 97%', 600)
        self.queued('20261024-100100-codex-bbbb', 'queued behind 1 job', 300, kind='review', priority=0)
        rc, out = self.run_board('--json')
        self.assertEqual(rc, 0)
        data = json.loads(out)
        self.assertRegex(data['host'], r'^host load: cpu 97% .*slots \d+/\d+ \(host busy: cpu 97%\)$')
        self.assertEqual([w['id'] for w in data['queue']], ['20261024-100100-codex-bbbb', '20261024-100000-codex-aaaa'])
        self.assertEqual([w['reason'] for w in data['queue']], ['queued behind 1 job', 'host busy: cpu 97%'])
        rc, text = self.run_board()
        self.assertIn('host load: cpu 97%', text)
        self.assertIn('== QUEUE (2 waiting for a slot)', text)
        self.assertRegex(text, r'1\. 20261024-100100-codex-bbbb .*queued behind 1 job')
        self.assertRegex(text, r'2\. 20261024-100000-codex-aaaa .*host busy: cpu 97%')
        rc, md = self.run_board('--md')
        self.assertIn('host load: cpu 97%', md)
        self.assertIn('**Queue (2 waiting for a slot)**', md)

    def test_no_queue_and_unreadable_load_stay_quiet(self):
        hostload.set_sampler(lambda: None)
        rc, out = self.run_board('--json')
        data = json.loads(out)
        self.assertEqual(data['queue'], [])
        self.assertIn('unknown', data['host'])
        self.assertNotIn('QUEUE', self.run_board()[1])


class WipPump(tj.JobsBase):
    """A freed writer lane starts the next queued request: FIFO, reviews first, never past the host's limit."""

    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)
        self.started = []

    def row(self, rid, ago=60, kind='code', argv=None, brief=None):
        argv = argv or ['run', '--write', '--cd', self.work, '--brief', brief or self.brief, '--kind', kind]
        return {'id': rid, 'kind': 'run', 'name': rid, 'worktree': self.work, 'argv': argv,
                'queued_at': common.iso(common.now() - __import__('datetime').timedelta(seconds=ago))}

    def spawn(self, row):
        self.started.append(row['id'])

    def pump(self, rows, busy=0, **kw):
        wip.save_queue(rows)
        return wip.pump({}, busy=busy, spawn=self.spawn, **kw)

    def test_a_free_lane_launches_the_oldest_request_and_removes_it_from_the_queue(self):
        res = self.pump([self.row('older', 300), self.row('newer', 100)], busy=4)
        self.assertEqual(self.started, ['older'])
        self.assertEqual([r['id'] for r in wip.queue()], ['older', 'newer'])  # 'older' stays claimed until admitted
        self.assertIsNotNone(wip.queue()[0].get('pumping_at'))
        self.assertNotIn('pumping_at', wip.queue()[1])
        self.assertEqual([r['id'] for r in res['launched']], ['older'])

    def test_every_free_lane_is_filled(self):
        self.pump([self.row('a', 300), self.row('b', 200), self.row('c', 100)], busy=3)
        self.assertEqual(self.started, ['a', 'b'])
        self.assertEqual({r['id'] for r in wip.queue() if r.get('pumping_at')}, {'a', 'b'})
        self.assertNotIn('pumping_at', [r for r in wip.queue() if r['id'] == 'c'][0])

    def test_a_review_goes_first(self):
        self.pump([self.row('work', 300), self.row('rev', 10, kind='review')], busy=4)
        self.assertEqual(self.started, ['rev'])

    def test_full_lanes_launch_nothing(self):
        res = self.pump([self.row('a')], busy=5)
        self.assertEqual(self.started, [])
        self.assertEqual(len(wip.queue()), 1)
        self.assertIn('5/5', res['note'])

    def test_a_busy_host_lowers_the_lane_limit(self):
        res = self.pump([self.row('a')], busy=2, reading=busy(cpu=99))
        self.assertEqual(self.started, [])
        self.assertIn('host busy: cpu 99%', res['note'])
        self.pump([self.row('a')], busy=1, reading=busy(cpu=99))
        self.assertEqual(self.started, ['a'])

    def test_requests_that_cannot_be_rerun_as_typed_stay_queued(self):
        placeholder = self.row('ph', argv=['run', '--write', '--cd', self.work, '--brief', '<brief>'])
        missing = self.row('gone', brief=os.path.join(self.tmp, 'no-such-brief.md'))
        old = self.row('old', ago=3 * 86400)
        baton_row = dict(self.row('b'), kind='baton', argv=['baton', 'new', 'x'])
        res = self.pump([placeholder, missing, old, baton_row, self.row('ok')], busy=0)
        self.assertEqual(self.started, ['ok'])
        self.assertEqual(sorted(x['id'] for x in res['skipped']), ['b', 'gone', 'old', 'ph'])
        self.assertEqual(sorted(r['id'] for r in wip.queue()), ['b', 'gone', 'ok', 'old', 'ph'])
        self.assertEqual([r['id'] for r in wip.queue() if r.get('pumping_at')], ['ok'])

    def test_dry_run_changes_nothing(self):
        res = self.pump([self.row('a')], dry_run=True)
        self.assertEqual(self.started, [])
        self.assertEqual(len(wip.queue()), 1)
        self.assertEqual([r['launched'] for r in res['launched']], [False])

    def test_a_request_that_fails_to_start_goes_back_to_the_front(self):
        wip.save_queue([self.row('a')])

        def boom(row):
            raise OSError('no process')

        res = wip.pump({}, busy=0, spawn=boom)
        self.assertEqual([r['id'] for r in wip.queue()], ['a'])
        self.assertEqual(res['launched'], [])
        self.assertIn('could not start', res['skipped'][0]['why'])

    def test_can_be_switched_off_and_an_empty_queue_is_quiet(self):
        wip.save_queue([self.row('a')])
        self.assertEqual(wip.pump({'wip': {'autopump': False}}, busy=0, spawn=self.spawn)['launched'], [])
        wip.save_queue([])
        self.assertEqual(wip.pump({}, busy=0, spawn=self.spawn)['note'], 'queue empty')

    def test_a_pump_that_dies_after_claiming_leaves_the_request_queued(self):
        wip.save_queue([self.row('a')])

        def die(row):
            raise KeyboardInterrupt()

        with self.assertRaises(KeyboardInterrupt):
            wip.pump({}, busy=0, spawn=die)
        left = wip.queue()
        self.assertEqual([r['id'] for r in left], ['a'])
        self.assertIsNotNone(left[0].get('pumping_at'))
        res = wip.pump({}, busy=0, spawn=self.spawn)  # the next pump does not start it a second time
        self.assertEqual(self.started, [])
        self.assertEqual(res['skipped'][0]['why'], 'a pump is starting it')

    def test_a_claim_from_a_pump_that_never_finished_waits_for_a_human(self):
        old = dict(self.row('a'), pumping_at=common.iso(common.now() - __import__('datetime').timedelta(minutes=30)))
        res = self.pump([old])
        self.assertEqual(self.started, [])
        self.assertIn('did not finish', res['skipped'][0]['why'])
        self.assertEqual([r['id'] for r in wip.queue()], ['a'])

    def test_the_request_is_claimed_while_it_starts_and_removed_once_it_has(self):
        seen = []
        wip.save_queue([self.row('a')])
        wip.pump({}, busy=0, spawn=lambda row: seen.append([r.get('pumping_at') for r in wip.queue()]))
        self.assertTrue(seen[0][0])
        self.assertEqual([r['id'] for r in wip.queue()], ['a'])  # still claimed: the run it started was not admitted yet
        self.assertTrue(wip.queue()[0].get('pumping_at'))

    def _claimed(self, rid='rid'):
        wip.save_queue([dict(self.row(rid), pumping_at=common.iso(common.now()), claim='tok')])

    def test_the_run_acknowledges_its_request_only_once_its_job_exists(self):
        self._claimed()
        jobs._WIP_REQUEST.set(('rid', None))
        with mock.patch.object(wip, 'collect', return_value=[]), mock.patch.dict(os.environ, {wip.CLAIM_ENV: 'tok'}), \
                mock.patch.object(jobs, '_admitted_job', side_effect=lambda plan: wip.queue() and 'job-1') as made:
            jid = jobs.create_job({'write': True, 'cwd': self.work, 'name': 'x'})
        self.assertEqual(jid, 'job-1')  # the request was still queued while the job was being made
        made.assert_called_once()
        self.assertEqual(wip.queue(), [])

    def test_a_job_that_cannot_be_made_leaves_the_request_claimed(self):
        self._claimed()
        jobs._WIP_REQUEST.set(('rid', None))
        with mock.patch.object(wip, 'collect', return_value=[]), mock.patch.dict(os.environ, {wip.CLAIM_ENV: 'tok'}), \
                mock.patch.object(jobs, '_admitted_job', side_effect=OSError('disk')):
            with self.assertRaises(OSError):
                jobs.create_job({'write': True, 'cwd': self.work, 'name': 'x'})
        self.assertEqual([r['id'] for r in wip.queue()], ['rid'])
        self.assertTrue(wip.queue()[0].get('pumping_at'))

    def test_admission_in_check_alone_does_not_remove_a_claimed_request(self):
        argv = ['run', '--write', '--cd', self.work, '--brief', self.brief]
        with mock.patch.object(wip, 'collect', return_value=[]):
            rid = wip.check(self.work, 'run', 'x', {}, argv=argv)
            wip.save_queue([dict(self.row('x', argv=argv), id=rid, pumping_at=common.iso(common.now()))])
            self.assertEqual(wip.check(self.work, 'run', 'x', {}, argv=argv), rid)
        self.assertEqual([r['id'] for r in wip.queue()], [rid])  # a route refusal after this point loses nothing

    def test_an_unclaimed_request_is_satisfied_by_a_hand_typed_repeat(self):
        wip.save_queue([self.row('x'), self.row('y')])
        wip.acknowledge('x', None)  # the repeat ran before any pump claimed the request
        self.assertEqual([r['id'] for r in wip.queue()], ['y'])  # so the pump cannot run it a second time

    def test_a_claimed_request_without_a_matching_token_is_not_acknowledged(self):
        self._claimed('x')
        wip.acknowledge('x', None)
        wip.acknowledge('x', 'someone-else')
        self.assertEqual([r['id'] for r in wip.queue()], ['x'])
        wip.save_queue([dict(self.row('z'), pumping_at=common.iso(common.now()))])  # claimed by an older pump
        wip.acknowledge('z', None)
        self.assertEqual([r['id'] for r in wip.queue()], ['z'])

    def test_only_the_run_that_carries_the_claim_token_acknowledges(self):
        self._claimed('x')
        wip.acknowledge('x', None)
        wip.acknowledge('x', 'someone-else')
        self.assertEqual([r['id'] for r in wip.queue()], ['x'])
        wip.acknowledge('x', 'tok')
        self.assertEqual(wip.queue(), [])

    def test_a_pumped_row_carries_a_token_and_the_spawn_gets_it(self):
        wip.save_queue([self.row('a')])
        seen = []
        wip.pump({}, busy=0, spawn=lambda row: seen.append(row.get('claim')))
        self.assertTrue(seen[0])
        self.assertEqual(wip.queue()[0].get('claim'), seen[0])
        with mock.patch.object(common, 'spawn_detached_ex') as spawn:
            wip._spawn(dict(self.row('b'), claim='tok'))
        self.assertEqual(spawn.call_args.kwargs['env'][wip.CLAIM_ENV], 'tok')

    def test_a_run_that_did_not_start_gives_the_claim_back(self):
        wip.save_queue([self.row('a')])

        def boom(row):
            raise OSError('no process')

        wip.pump({}, busy=0, spawn=boom)
        self.assertNotIn('pumping_at', wip.queue()[0])

    def test_a_run_that_is_refused_again_keeps_its_request(self):
        wip.save_queue([self.row('a')])

        def refused_again(row):  # the started run found the lanes full and queued itself: that clears the claim
            rows = wip.queue()
            for r in rows:
                r.pop('pumping_at', None)
            wip.save_queue(rows)

        wip.pump({}, busy=0, spawn=refused_again)
        self.assertEqual([r['id'] for r in wip.queue()], ['a'])
        self.assertNotIn('pumping_at', wip.queue()[0])

    def test_check_rearms_a_claimed_request_and_records_the_invocation_directory(self):
        argv = ['run', '--write', '--cd', self.work, '--brief', self.brief]
        policy = {'wip': {'max_writers': 1}}
        with mock.patch.object(wip, 'collect', return_value=[{'worktree': 'elsewhere'}]):
            with self.assertRaises(wip.WipError):
                wip.check(self.work, 'run', 'x', policy, argv=argv)
            first = wip.queue()
            self.assertEqual(first[0]['cwd'], os.getcwd())
            first[0]['pumping_at'] = common.iso(common.now())
            first[0]['claim'] = 'tok'
            wip.save_queue(first)
            with self.assertRaises(wip.WipError):  # an identical command typed by hand while the pump starts the request
                wip.check(self.work, 'run', 'x', policy, argv=argv)
            self.assertIn('pumping_at', wip.queue()[0])  # does not take the pump's claim away
            with mock.patch.dict(os.environ, {wip.CLAIM_ENV: 'tok'}):
                with self.assertRaises(wip.WipError):  # the run the pump started, refused again, hands it back
                    wip.check(self.work, 'run', 'x', policy, argv=argv)
        again = wip.queue()
        self.assertEqual(len(again), 1)
        self.assertNotIn('pumping_at', again[0])
        self.assertNotIn('claim', again[0])

    def test_a_queued_run_is_replayed_from_the_directory_it_was_typed_in(self):
        base = os.path.join(self.tmp, 'typed-here')
        os.makedirs(base)
        brief = os.path.join(base, 'rel-brief.md')
        with open(brief, 'w') as h:
            h.write('x')
        row = self.row('r', argv=['run', '--write', '--cd', 'wt', '--brief', 'rel-brief.md'])
        self.assertIsNotNone(wip.launchable(row))  # relative paths and no recorded directory: not safe to replay
        row['cwd'] = base
        self.assertIsNone(wip.launchable(row))
        self.assertIn('gone', wip.launchable(dict(row, cwd=os.path.join(base, 'missing'))))
        free = {'in_job': False, 'kill_on_close': False, 'breakaway_ok': True}
        with mock.patch.object(common, 'job_info', return_value=free), \
                mock.patch.object(common, 'spawn_detached_ex') as spawn:
            wip._spawn(row)
        self.assertEqual(spawn.call_args.kwargs['cwd'], base)

    def test_pump_is_refused_inside_a_worker_and_by_the_guard(self):
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'x'}):
            self.assertEqual(wip.cli(['pump']), common.EXIT_REFUSED)
        from twinlane import guard
        self.assertIn('wip pump', guard._self_reason(['wip', 'pump'], 'twinlane') or '')
        self.assertIsNone(guard._self_reason(['wip'], 'twinlane'))
        self.assertIn('wt doctor', guard._self_reason(['wt', 'doctor', '--fix'], 'twinlane') or '')
        self.assertIsNone(guard._self_reason(['wt', 'doctor'], 'twinlane'))

    def test_pump_quietly_never_raises(self):
        with mock.patch.object(wip, 'pump', side_effect=RuntimeError('x')):
            self.assertIsNone(wip.pump_quietly({}))



class WinKillWait(unittest.TestCase):
    """Snapshot targets can be waited on, but snapshots cannot confirm whole-tree exit."""

    def run_gone(self, wait_result, before, after):
        k32 = mock.Mock()
        k32.WaitForSingleObject.return_value = wait_result
        with mock.patch.object(common, '_win_process_tree_snapshot', return_value=after):
            return common._win_tree_gone([1, 2], {10, 11}, before, k32)

    def test_signalled_snapshot_targets_do_not_prove_tree_exit(self):
        rows = [(10, 1, 100), (11, 10, 101)]
        self.assertFalse(self.run_gone(0, rows, list(rows)))

    def test_a_process_that_has_not_exited_is_not_gone(self):
        rows = [(10, 1, 100)]
        self.assertFalse(self.run_gone(258, rows, list(rows)))  # WAIT_TIMEOUT

    def test_a_child_born_after_the_snapshot_is_not_gone(self):
        before = [(10, 1, 100)]
        self.assertFalse(self.run_gone(0, before, before + [(12, 10, 150)]))

    def test_an_unreadable_second_snapshot_is_not_gone(self):
        self.assertFalse(self.run_gone(0, [(10, 1, 100)], None))

    def test_a_kernel_without_the_wait_call_cannot_confirm_exit(self):
        class Bare:
            pass

        self.assertFalse(common._win_tree_gone([1], {10}, [(10, 1, 100)], Bare()))


class R8JobKernel:
    """A synthetic job still owns G=103 after its intermediate parent C=102 exited."""

    def __init__(self, active=(0,), assign=True):
        self.active, self.assign = list(active), assign
        self.events, self.flags, self.queries = [], None, 0
        self.query_ok, self.resume_result = True, 1

    def CreateJobObjectW(self, *_):
        self.events.append('create')
        return 501

    def SetInformationJobObject(self, handle, kind, limits, size):
        self.events.append('limits')
        self.flags = limits._obj.Basic.LimitFlags
        return True

    def AssignProcessToJobObject(self, *_):
        self.events.append('assign')
        return self.assign

    def CreateToolhelp32Snapshot(self, *_):
        self.events.append('threads')
        return 502

    def Thread32First(self, snapshot, entry):
        entry._obj.th32ThreadID, entry._obj.th32OwnerProcessID = 91, 101
        return True

    def Thread32Next(self, *_):
        return False

    def OpenThread(self, *_):
        return 503

    def ResumeThread(self, *_):
        self.events.append('resume')
        return self.resume_result

    def CloseHandle(self, handle):
        self.events.append(('close', handle))
        return True

    def TerminateJobObject(self, *_):
        self.events.append('terminate-job')
        return True

    def QueryInformationJobObject(self, handle, kind, info, size, returned):
        assert kind == 1  # JobObjectBasicAccountingInformation
        self.queries += 1
        info._obj.ActiveProcesses = self.active[0]
        if len(self.active) > 1:
            self.active.pop(0)
        return self.query_ok

    def WaitForSingleObject(self, *_):
        return 0

    def OpenProcess(self, *_):
        return 601

    def GetProcessTimes(self, handle, created, *_):
        created._obj.value = 100
        return True

    def TerminateProcess(self, *_):
        self.events.append('terminate-process')
        return True

    def process_identity(self, handle):
        return 101, 1, 100


class WinAttemptJobs(unittest.TestCase):
    def setUp(self):
        self.kernel = R8JobKernel()
        self.proc = mock.Mock(pid=101, _handle=601)
        for patch in (mock.patch.object(common, 'IS_WIN', True),
                      mock.patch.object(common, '_k32', return_value=self.kernel),
                      mock.patch.object(common, '_ENGINE_JOBS', {}, create=True),
                      mock.patch.object(common, '_UNCONTAINED', set(), create=True)):
            patch.start()
            self.addCleanup(patch.stop)

    def test_r8_suspend_assign_resume_and_close_order(self):
        common.contain_engine(self.proc)
        self.assertEqual(self.kernel.flags, common.JOB_LIMIT_KILL_ON_JOB_CLOSE)
        self.assertLess(self.kernel.events.index('assign'), self.kernel.events.index('resume'))
        self.assertIn(101, common._ENGINE_JOBS)
        common.close_engine_job(self.proc)
        self.assertNotIn(101, common._ENGINE_JOBS)
        self.assertEqual(self.kernel.events[-1], ('close', 501))

    def test_r8_zero_active_processes_confirms_after_polling(self):
        common.contain_engine(self.proc)
        self.kernel.active = [3, 1, 0]
        with mock.patch.object(common.time, 'sleep'), \
                mock.patch.object(common, '_win_kill_tree', side_effect=AssertionError('snapshot fallback')):
            self.assertTrue(common.kill_tree(101, 100))
        self.assertEqual(self.kernel.queries, 3)
        self.assertIn('terminate-job', self.kernel.events)

    def test_r8_surviving_grandchild_with_dead_parent_is_unconfirmed(self):
        common.contain_engine(self.proc)
        self.kernel.active = [1]  # G remains in the job although C no longer exists
        with mock.patch.object(common.time, 'monotonic', side_effect=[0, 6]), \
                mock.patch.object(common, '_win_process_tree_snapshot', return_value=[(103, 102, 102)]):
            self.assertFalse(common.kill_tree(101, 100))

    def test_r8_snapshot_with_transient_parent_cannot_confirm(self):
        before, after = [(101, 1, 100)], [(103, 102, 102)]
        with mock.patch.object(common, '_win_process_tree_snapshot', return_value=after):
            self.assertFalse(common._win_tree_gone([601], {101}, before, self.kernel))

    def test_r8_assignment_failure_falls_back_without_confirmation(self):
        self.kernel.assign = False
        common.contain_engine(self.proc)
        self.assertNotIn(101, common._ENGINE_JOBS)
        self.assertIn('resume', self.kernel.events)
        self.assertIn(('close', 501), self.kernel.events)
        with mock.patch.object(common, '_win_process_tree_snapshot', return_value=[(101, 1, 100)]):
            self.assertFalse(common.kill_tree(101, 100))
        self.assertIn('terminate-process', self.kernel.events)

    def test_r8_accounting_failure_never_confirms_even_if_buffer_is_zero(self):
        common.contain_engine(self.proc)
        self.kernel.query_ok = False
        self.assertFalse(common.kill_tree(101, 100))

    def test_r8_job_kill_still_works_after_root_exit(self):
        common.contain_engine(self.proc)
        self.kernel.active = [1, 0]
        with mock.patch.object(common.time, 'sleep'), \
                mock.patch.object(common, 'pid_alive', return_value=False):
            self.assertTrue(common.kill_tree(101, 100))

    def test_r8_mismatching_creation_stamp_never_terminates_job(self):
        common.contain_engine(self.proc)
        self.assertFalse(common.kill_tree(101, 99))
        self.assertNotIn('terminate-job', self.kernel.events)

    def test_r8_resume_failure_keeps_job_for_exception_cleanup(self):
        self.kernel.resume_result = 0xFFFFFFFF
        with self.assertRaises(OSError):
            common.contain_engine(self.proc)
        self.assertIn(101, common._ENGINE_JOBS)
        self.assertTrue(common.kill_tree(101, 100))
        common.close_engine_job(self.proc)

    def test_r9_close_reports_a_descendant_that_survives_the_root(self):
        common.contain_engine(self.proc)
        self.kernel.active = [1]  # the root exited, a descendant is still in the job
        with mock.patch.object(common.time, 'monotonic', side_effect=[0, 6]):
            self.assertFalse(common.close_engine_job(self.proc))
        self.assertIn('terminate-job', self.kernel.events)
        self.assertNotIn(101, common._ENGINE_JOBS)

    def test_r9_close_confirms_an_empty_job_and_a_missing_one(self):
        common.contain_engine(self.proc)
        self.kernel.active = [0]
        self.assertTrue(common.close_engine_job(self.proc))
        self.assertTrue(common.close_engine_job(self.proc))  # already closed: nothing left to confirm
        self.assertTrue(common.close_engine_job(None))

    def test_r10_failed_containment_never_reads_as_confirmed_cleanup(self):
        self.kernel.assign = False
        common.contain_engine(self.proc)  # the engine still resumes, but no job holds it
        self.assertNotIn(101, common._ENGINE_JOBS)
        self.assertIn('resume', self.kernel.events)
        self.assertFalse(common.close_engine_job(self.proc))
        self.assertTrue(common.close_engine_job(self.proc))  # the state is consumed once

    def test_r11_an_interrupted_drain_still_lets_go_of_the_job(self):
        common.contain_engine(self.proc)
        self.kernel.active = [1]
        with mock.patch.object(common, '_win_kill_job', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                common.close_engine_job(self.proc)
        self.assertNotIn(101, common._ENGINE_JOBS)  # the handle closed: KILL_ON_JOB_CLOSE reaps the descendants
        self.assertEqual(self.kernel.events[-1], ('close', 501))

    def test_r12_the_hook_runs_while_the_engine_is_suspended_and_can_veto_the_resume(self):
        seen = []
        common.contain_engine(self.proc, before_resume=lambda contained: seen.append(
            (contained, 'resume' in self.kernel.events)))
        self.assertEqual(seen, [(True, False)])  # contained, and not yet resumed
        self.assertIn('resume', self.kernel.events)
        common._ENGINE_JOBS.clear()
        self.kernel.events.clear()
        self.kernel.assign = False
        seen.clear()
        common.contain_engine(self.proc, before_resume=lambda contained: seen.append(contained))
        self.assertEqual(seen, [False])
        self.kernel.events.clear()

        def veto(contained):
            raise OSError('custody could not be recorded')

        with self.assertRaises(OSError):
            common.contain_engine(self.proc, before_resume=veto)
        self.assertNotIn('resume', self.kernel.events)  # an uncontained engine without custody never runs

    def test_r8_posix_never_calls_the_job_api(self):
        with mock.patch.object(common, 'IS_WIN', False), mock.patch.object(common, '_k32') as native:
            common.contain_engine(self.proc)
            common.close_engine_job(self.proc)
        native.assert_not_called()


class R8RunnerCustody(tj.JobsBase):
    def fixture(self, engine='claude'):
        # Exercise runner internals using disposable fake engines. Do not change
        # inherited worker lineage to make a lead-only CLI dispatch pass.
        jid = '20261025-000000-' + engine + '-f8aa'
        params = {'engine': engine, 'profile': 'intel', 'model': 'fixture',
                  'effort': 'low', 'tier': 'sol', 'max_turns': 5, 'timeout_s': 30}
        spec = {'id': jid, 'name': 'fixture', 'kind': 'review', 'engine': engine,
                'profile': 'intel', 'cwd': self.work, 'primary': params,
                'brief_text': 'synthetic fixture', 'created_at': common.iso(common.now()),
                'failover': False, 'notes': []}
        os.makedirs(jobs.job_dir(jid))
        common.write_json(jobs._p(jid, 'job.json'), spec)
        common.write_text(jobs._p(jid, 'brief.md'), 'synthetic fixture')
        return jid, spec, params

    def test_r8_surviving_grandchild_leaves_marker_and_no_retry(self):
        os.environ['FAKE_SLEEP'] = '120'
        os.environ['TWINLANE_CONFIG'] = os.path.join(self.tmp, 'watchdog.json')
        common.write_json(os.environ['TWINLANE_CONFIG'], {'watchdog': {'stall_s': 1.5, 'poll_s': 0.2}})
        real_kill = common.kill_tree
        kernel = R8JobKernel(active=(1,))  # G survives C in the fake job's accounting

        def kill(pid, created=None):
            real_kill(pid, created)  # reap the real fixture engine; fake G is never a host process
            with mock.patch.object(common.time, 'monotonic', side_effect=[0, 6]):
                return common._win_kill_job((kernel, 501))

        jid, spec, _ = self.fixture()
        with mock.patch.object(common, 'kill_tree', side_effect=kill):
            receipt = jobs.run_job(jid)
        self.assertEqual(receipt['status'], 'failed', receipt)
        self.assertEqual(len(self.receipt(jid)['attempts']), 1)
        self.assertFalse(self.receipt(jid)['attempts'][0]['stall_kill_confirmed'])
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertTrue(jobs._writer_job_busy(jid))

    def test_r9_normal_exit_with_a_surviving_descendant_keeps_the_claim(self):
        jid, spec, params = self.fixture()
        spec['profile'] = 'code'
        with mock.patch.object(common, 'close_engine_job', return_value=False):
            attempt = jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'), spec, common.load_policy(), None)
        self.assertEqual(attempt['status'], 'done', attempt)
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertTrue(jobs._writer_job_busy(jid))

    def test_r10_unresolved_tree_blocks_a_limit_hit_failover(self):
        jid, spec, params = self.fixture()
        spec['failover'] = True
        common.write_json(jobs._p(jid, 'job.json'), spec)
        calls = []

        def limit_hit(job_id, n, *a, **k):
            calls.append(n)
            common.write_json(jobs._p(job_id, jobs.TREE_UNRESOLVED), {'engine': 'claude'})
            return {'n': n, 'engine': 'claude', 'status': 'limit_hit', 'error': 'usage limit'}

        with mock.patch.object(jobs, 'run_attempt', side_effect=limit_hit):
            receipt = jobs.run_job(jid)
        self.assertEqual(calls, [1])
        self.assertTrue(any('no failover' in note for note in receipt['notes']), receipt['notes'])
        self.assertTrue(jobs._writer_job_busy(jid))

    def test_r11_interrupt_during_the_final_drain_records_custody_before_propagating(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                jid, spec, params = self.fixture(engine)
                spec['profile'] = 'code'
                with mock.patch.object(common, 'close_engine_job', side_effect=KeyboardInterrupt):
                    with self.assertRaises(KeyboardInterrupt):
                        jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'), spec, common.load_policy(), None)
                self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
                self.assertTrue(jobs._writer_job_busy(jid))

    def test_r11_interrupt_with_an_unwritable_marker_keeps_the_pidless_launch_marker(self):
        jid, spec, params = self.fixture()
        spec['profile'] = 'code'
        real = jobs._write_job_json

        def refuse(job_id, name, data):
            if name == jobs.TREE_UNRESOLVED:
                raise OSError('disk')
            return real(job_id, name, data)

        with mock.patch.object(common, 'close_engine_job', side_effect=KeyboardInterrupt), \
                mock.patch.object(jobs, '_write_job_json', side_effect=refuse), \
                mock.patch.object(jobs.time, 'sleep'):
            with self.assertRaises(KeyboardInterrupt):
                jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'), spec, common.load_policy(), None)
        self.assertTrue(jobs._tree_unresolved(jid))
        self.assertNotIn('pid', common.read_json(jobs._p(jid, jobs.CHILD_STARTING)))
        self.assertTrue(jobs._writer_job_busy(jid))

    def test_r12_an_uncontained_engine_gets_durable_custody_before_it_resumes(self):
        jid, spec, params = self.fixture('claude')
        jobs._hold_uncontained(jid, 'claude')
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
        self.assertTrue(jobs._writer_job_busy(jid))
        # a `stop` that then kills the runner and the root cannot clear it: only a person does
        self.assertIsNone(jobs._custody_stale(jid))

    def test_r12_an_unwritable_marker_refuses_to_start_the_uncontained_engine(self):
        jid, spec, params = self.fixture('claude')
        real = jobs._write_job_json

        def refuse(job_id, name, data):
            if name == jobs.TREE_UNRESOLVED:
                raise OSError('disk')
            return real(job_id, name, data)

        with mock.patch.object(jobs, '_write_job_json', side_effect=refuse), mock.patch.object(jobs.time, 'sleep'):
            with self.assertRaises(OSError):
                jobs._hold_uncontained(jid, 'claude')

    def test_r12_both_runners_pass_the_custody_hook_before_the_resume(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                jid, spec, params = self.fixture(engine)
                spec['profile'] = 'code'
                seen = {}

                def contain(proc, before_resume=None):
                    seen['hook'] = before_resume
                    try:
                        before_resume(False)  # the engine could not be put in a job object
                    finally:
                        common.kill_tree(proc.pid)  # the real suspended-or-running fixture engine
                    raise KeyboardInterrupt

                with mock.patch.object(common, 'contain_engine', side_effect=contain):
                    try:
                        jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'), spec, common.load_policy(), None)
                    except KeyboardInterrupt:
                        pass
                self.assertIsNotNone(seen.get('hook'))
                self.assertTrue(os.path.exists(jobs._p(jid, jobs.TREE_UNRESOLVED)))
                self.assertTrue(jobs._writer_job_busy(jid))

    def test_r9_marker_only_folder_refuses_the_actual_claimant(self):
        marker_only = os.path.join(common.JOBS, '20261025-000001-claude-aaaa')
        os.makedirs(marker_only)
        common.write_json(os.path.join(marker_only, jobs.TREE_UNRESOLVED), {})
        with self.assertRaisesRegex(jobs.Refusal, 'unreadable'):
            jobs._claim_writer({'profile': 'code', 'cwd': self.work, 'id': '20261025-000002-claude-bbbb'})

    def test_r9_posix_codex_wrapper_kill_is_never_reported_confirmed(self):
        with mock.patch.object(common, 'kill_tree', return_value=True) as kill:
            self.assertIs(jobs._kill_wrapper_unconfirmed(4242, 7), False)
            self.assertIs(jobs._kill_wrapper_unconfirmed(4243), False)
        self.assertEqual([c.args for c in kill.call_args_list], [(4242, 7), (4243,)])

    def test_r13_posix_claude_runner_hands_the_watchdog_the_unconfirmed_kill(self):
        jid, spec, params = self.fixture('claude')
        seen = {}

        def stop_here(proc, timeout_s, stall, poll_s, **kw):
            seen.update(kw)
            raise KeyboardInterrupt

        sentinel = object()
        with mock.patch.object(jobs, '_watchdog_kill', return_value=sentinel), \
                mock.patch.object(common, 'kill_tree', return_value=False), \
                mock.patch.object(jobs.watchdog, 'wait', side_effect=stop_here):
            try:
                jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'), spec, common.load_policy(), None)
            except KeyboardInterrupt:
                pass
        self.assertIs(seen.get('kill'), sentinel)

    def test_r9_posix_codex_runner_hands_the_watchdog_the_unconfirmed_kill(self):
        jid, spec, params = self.fixture('codex')
        seen = {}

        def stop_here(proc, timeout_s, stall, poll_s, **kw):
            seen.update(kw)
            raise KeyboardInterrupt

        sentinel = object()
        with mock.patch.object(jobs, '_watchdog_kill', return_value=sentinel),                 mock.patch.object(common, 'kill_tree', return_value=False),                 mock.patch.object(jobs.watchdog, 'wait', side_effect=stop_here):
            try:
                jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'), spec, common.load_policy(), None)
            except KeyboardInterrupt:
                pass
        self.assertIs(seen.get('kill'), sentinel)
        with mock.patch.object(common, 'IS_WIN', False):
            self.assertIs(jobs._watchdog_kill(), jobs._kill_wrapper_unconfirmed)
        with mock.patch.object(common, 'IS_WIN', True):
            self.assertIsNone(jobs._watchdog_kill())

    @unittest.skipUnless(common.IS_WIN, 'Windows suspended engine creation')
    def test_r8_both_engine_spawn_sites_suspend_before_containment(self):
        popen, contain = subprocess.Popen, common.contain_engine
        events = []

        def spawn(argv, **kw):
            proc = popen(argv, **kw)
            if kw.get('cwd') == self.work and kw.get('start_new_session') is False:
                events.append(('spawn', proc.pid, kw['creationflags']))
            return proc

        def assign(proc, **kw):
            events.append(('contain', proc.pid))
            return contain(proc, **kw)

        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine), mock.patch.object(subprocess, 'Popen', side_effect=spawn), \
                    mock.patch.object(common, 'contain_engine', side_effect=assign):
                events.clear()
                jid, spec, params = self.fixture(engine)
                attempt = jobs.run_attempt(jid, 1, params, jobs._p(jid, 'brief.md'),
                                           spec, common.load_policy(), None)
                self.assertEqual(attempt['status'], 'done', attempt)
                self.assertEqual([e[0] for e in events], ['spawn', 'contain'])
                self.assertEqual(events[0][1], events[1][1])
                self.assertTrue(events[0][2] & common.CREATE_SUSPENDED)
                self.assertNotIn(events[0][1], common._ENGINE_JOBS)


if __name__ == '__main__':
    unittest.main()
