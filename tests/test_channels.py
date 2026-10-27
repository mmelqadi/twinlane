"""More parallel engine channels: slot-lock contention waits, slot waits on the receipt, live-duplicate refusal,
dead pump claims released, and the board's per-engine slots and stuck-queue lines."""
import json
import os
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate before importing runtime modules
import test_board as tb
import test_jobs as tj
from twinlane import common, hostload, jobs, wip

LOW = {'cpu_pct': 20.0, 'ram_free_mb': 9000, 'engine_procs': 3}


def ago(seconds):
    return common.iso(common.now() - __import__('datetime').timedelta(seconds=seconds))


def dead_identity():
    """(pid, creation stamp) of a process that has exited."""
    p = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(0.5)'])
    created = common.process_created(p.pid)
    p.wait(10)
    deadline = time.monotonic() + 10
    while common.pid_state(p.pid, created) != common.DEAD and time.monotonic() < deadline:
        time.sleep(0.05)
    return p.pid, created


class SlotWaitRecord(tj.JobsBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)
        hostload.set_sampler(lambda: LOW)
        jobs.SLOT_POLL_S = 0.01
        self.jid = '20261026-100000-codex-aaaa'
        d = os.path.join(common.JOBS, self.jid)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'id': self.jid, 'kind': 'code', 'cwd': self.work,
                                                        'primary': {'engine': 'codex'}})

    def test_slot_lock_contention_is_a_wait_and_the_reasons_are_recorded(self):
        answers = iter([TimeoutError('lock busy: ' + os.path.join(common.STATE, 'slots.lock')), (False, 'engine slots full: 1/1'),
                        (False, 'engine slots full: 1/1'), (True, None)])

        def attempt(*a, **k):
            x = next(answers)
            if isinstance(x, BaseException):
                raise x
            return x

        rec = {}
        with mock.patch.object(jobs, 'slot_attempt', attempt):
            self.assertTrue(jobs.acquire_slot(self.jid, 'codex', 1, {'codex': {'max_parallel': 1}}, record=rec))
        self.assertEqual(rec['slot_wait_reasons'], ['waiting for slot lock', 'engine slots full: 1/1'])
        self.assertGreaterEqual(rec['slot_wait_s'], 0)
        st = common.read_json(os.path.join(common.JOBS, self.jid, 'status.json'))
        self.assertEqual(st['status'], 'queued')  # slot_attempt is mocked: the last write was the queue entry

    def test_no_wait_records_nothing(self):
        rec = {}
        with mock.patch.object(jobs, 'slot_attempt', lambda *a, **k: (True, None)):
            self.assertFalse(jobs.acquire_slot(self.jid, 'codex', 1, {}, record=rec))
        self.assertEqual(rec, {})

    def test_reasons_are_capped(self):
        reasons = [(False, f'r{i}') for i in range(jobs.SLOT_WAIT_REASONS_MAX + 3)] + [(True, None)]
        it = iter(reasons)
        rec = {}
        with mock.patch.object(jobs, 'slot_attempt', lambda *a, **k: next(it)):
            jobs.acquire_slot(self.jid, 'codex', 1, {}, record=rec)
        self.assertEqual(len(rec['slot_wait_reasons']), jobs.SLOT_WAIT_REASONS_MAX)


class LiveDuplicate(tj.JobsBase):
    def job(self, jid, brief='same brief', status='running', pid=None, created=None, result=False, cwd=None,
            tier='astra', engine='codex'):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {
            'id': jid, 'engine': engine, 'cwd': cwd or self.work, 'brief_text': brief,
            'primary': {'engine': engine, 'model': 'gpt-6-astra', 'tier': tier}})
        common.write_json(os.path.join(d, 'status.json'), {
            'status': status, 'engine': engine, 'pid': os.getpid() if pid is None else pid,
            'pid_created': jobs._self_created() if created is None else created})
        if result:
            common.write_json(os.path.join(d, 'result.json'), {'id': jid, 'status': 'done'})
        return jid

    def plan(self, brief='same brief', **extra):
        return dict({'engine': 'codex', 'cwd': self.work, 'brief_text': brief, 'profile': 'intel',
                     'primary': {'engine': 'codex', 'model': 'gpt-6-astra', 'tier': 'astra'}}, **extra)

    def test_a_live_job_with_the_same_brief_folder_and_engine_is_a_duplicate(self):
        jid = self.job('20261026-100000-codex-aaaa')
        self.assertEqual(jobs.live_duplicate(self.plan()), jid)
        self.job('20261026-100001-codex-bbbb', status='queued')
        self.assertIsNotNone(jobs.live_duplicate(self.plan()))

    def test_what_is_not_a_duplicate(self):
        self.job('20261026-100000-codex-done', result=True)
        self.job('20261026-100001-codex-othr', brief='another brief')
        self.job('20261026-100002-codex-tier', tier='sol-high')
        self.job('20261026-100003-codex-cwd1', cwd=self.tmp)
        pid, created = dead_identity()
        self.job('20261026-100004-codex-dead', pid=pid, created=created)
        self.job('20261026-100005-claude-eng', engine='claude')
        self.assertIsNone(jobs.live_duplicate(self.plan()))

    def test_the_job_the_submitter_runs_inside_is_not_its_duplicate(self):
        parent = self.job('20261026-100000-codex-prnt')
        with mock.patch.dict(os.environ, TWINLANE_ENGINE_JOB=parent):
            self.assertIsNone(jobs.live_duplicate(self.plan()))
            other = self.job('20261026-100001-codex-othr')
            self.assertEqual(jobs.live_duplicate(self.plan()), other)

    def test_allow_duplicate_and_coordination_skip_the_check(self):
        self.job('20261026-100000-codex-aaaa')
        self.assertIsNone(jobs.live_duplicate(self.plan(allow_duplicate=True)))
        self.assertIsNone(jobs.live_duplicate(self.plan(coordination={'task': 't'})))

    def test_create_job_refuses_a_live_duplicate_before_writing_anything(self):
        jid = self.job('20261026-100000-codex-aaaa')
        before = sorted(os.listdir(common.JOBS))
        with self.assertRaises(jobs.Refusal) as cm:
            jobs.create_job(self.plan())
        self.assertIn(jid, cm.exception.message)
        self.assertIn('--allow-duplicate', cm.exception.message)
        self.assertEqual(sorted(os.listdir(common.JOBS)), before)

    def test_the_cli_accepts_allow_duplicate(self):
        with open(self.brief, encoding='utf-8') as h:
            self.job('20261026-100000-codex-aaaa', brief=h.read())
        import io
        from contextlib import redirect_stdout
        buf = io.StringIO()
        with redirect_stdout(buf):
            rc = jobs.cli('run', ['--engine', 'codex', '--kind', 'review', '--tier', 'astra', '--cd', self.work,
                                  '--brief', self.brief, '--dry-run', '--json', '--allow-duplicate'])
        self.assertEqual(rc, common.EXIT_OK, buf.getvalue())


class PumpDeadClaims(tj.JobsBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)

    def row(self, rid='r1', claimed_ago=600, pid=None, created=None, tries=1, claim='tok'):
        r = {'id': rid, 'kind': 'run', 'name': rid, 'worktree': self.work, 'cwd': self.work,
             'queued_at': ago((claimed_ago or 0) + 60),
             'argv': ['run', '--write', '--cd', self.work, '--brief', self.brief]}
        if claimed_ago is not None:
            r.update(pumping_at=ago(claimed_ago), claim=claim)
        if pid is not None:
            r.update(pump_pid=pid, pump_created=created, pump_tries=tries, pump_claim=claim)
        return r

    def test_a_stale_claim_whose_child_died_unadmitted_is_released(self):
        pid, created = dead_identity()
        rows = [self.row(pid=pid, created=created)]
        self.assertEqual(wip.release_dead_claims(rows), ['r1'])
        self.assertNotIn('pumping_at', rows[0])
        self.assertNotIn('claim', rows[0])
        self.assertEqual(rows[0]['pump_tries'], 1)  # kept: it caps the retries
        self.assertIsNone(wip.launchable(rows[0]))

    def test_claims_that_stay(self):
        pid, created = dead_identity()
        fresh = self.row('fresh', claimed_ago=10, pid=pid, created=created)
        legacy = self.row('legacy')  # no recorded child: needs a person, as before
        alive = self.row('alive', pid=os.getpid(), created=jobs._self_created())
        spent = self.row('spent', pid=pid, created=created, tries=wip.PUMP_MAX_TRIES)
        admitted = self.row('admitted', pid=pid, created=created, claim='tok-admitted')
        d = os.path.join(common.JOBS, common.stamp() + '-codex-abcd')
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'id': 'x', 'wip_claim': 'tok-admitted'})
        rows = [fresh, legacy, alive, spent, admitted]
        self.assertEqual(wip.release_dead_claims(rows), [])
        self.assertTrue(all(r.get('pumping_at') for r in rows))

    def test_an_unreadable_job_spec_counts_as_admitted(self):
        pid, created = dead_identity()
        row = self.row(pid=pid, created=created)
        d = os.path.join(common.JOBS, common.stamp() + '-codex-abcd')
        os.makedirs(d)
        with open(os.path.join(d, 'job.json'), 'w', encoding='utf-8') as h:
            h.write('{not json')
        self.assertTrue(wip._claimed_job_exists(row))

    def test_the_pump_records_the_child_and_later_retries_it(self):
        pid, created = dead_identity()
        started = []

        def spawn(row):
            started.append(row['id'])
            return {'pid': pid, 'created': created}

        wip.save_queue([self.row(claimed_ago=None)])
        wip.pump({}, busy=0, spawn=spawn, reading=LOW)
        q = wip.queue()
        self.assertEqual((q[0]['pump_pid'], q[0]['pump_created'], q[0]['pump_tries']), (pid, created, 1))
        q[0]['pumping_at'] = ago(wip.PUMP_CLAIM_S + 60)  # the claim has gone stale; its child is dead
        wip.save_queue(q)
        res = wip.pump({}, busy=0, spawn=spawn, reading=LOW)
        self.assertEqual(res.get('released'), ['r1'])
        self.assertEqual(started, ['r1', 'r1'])
        self.assertEqual(wip.queue()[0]['pump_tries'], 2)

    def test_dry_run_releases_nothing(self):
        pid, created = dead_identity()
        wip.save_queue([self.row(pid=pid, created=created)])
        wip.pump({}, busy=0, dry_run=True, reading=LOW)
        self.assertIsNotNone(wip.queue()[0].get('pumping_at'))

    def test_an_admitted_pumped_run_records_its_claim_on_the_job(self):
        os.environ[wip.CLAIM_ENV] = 'tok-123'
        self.addCleanup(os.environ.pop, wip.CLAIM_ENV, None)
        token = jobs._WIP_REQUEST.set(('rid-1', ['run']))
        self.addCleanup(jobs._WIP_REQUEST.reset, token)
        plan = {'engine': 'codex', 'profile': 'code', 'write': True, 'cwd': self.work, 'brief_text': 'x',
                'name': 'n', 'primary': {'engine': 'codex', 'tier': 'sol-high'}}
        jid = jobs.create_job(plan)
        self.assertEqual(common.read_json(os.path.join(common.JOBS, jid, 'job.json'))['wip_claim'], 'tok-123')
        self.assertNotIn('wip_claim', plan)


class ReviewR1(PumpDeadClaims):
    """Astra r1: identity bound to its claim; dedupe serialized with publication; archive keeps unacknowledged jobs."""

    def test_an_identity_recorded_for_another_claim_never_releases(self):
        pid, created = dead_identity()
        row = self.row(pid=pid, created=created, claim='new-token')
        row['pump_claim'] = 'old-token'
        self.assertEqual(wip.release_dead_claims([row]), [])
        self.assertEqual(row['claim'], 'new-token')

    def test_a_new_claim_and_a_rearm_both_drop_the_old_identity(self):
        pid, created = dead_identity()
        old = self.row(claimed_ago=None)
        old.update(pump_pid=pid, pump_created=created, pump_claim='old', pump_tries=1)
        wip.save_queue([old])
        boom = lambda row: (_ for _ in ()).throw(OSError('no process'))  # noqa: E731
        wip.pump({}, busy=0, spawn=lambda row: None, reading=LOW)  # started, but no identity came back
        q = wip.queue()[0]
        self.assertIsNotNone(q.get('claim'))
        self.assertNotIn('pump_pid', q)
        self.assertNotIn('pump_claim', q)
        self.assertEqual(q['pump_tries'], 2)  # counted when the claim was taken
        q['pumping_at'] = ago(wip.PUMP_CLAIM_S + 60)
        self.assertEqual(wip.release_dead_claims([q]), [])  # no identity for this claim: a person decides
        del boom
        argv = ['run', '--write', '--cd', self.work, '--brief', self.brief]
        full = mock.patch.object(wip, 'collect', return_value=[{'worktree': 'other'}] * 99)
        wip.save_queue([])
        with full, wip.lock():
            with self.assertRaises(wip.WipError):
                wip.check(self.work, 'run', 'n', {}, argv=argv)  # queues a row with check's own request id
        rows = wip.queue()
        mine = [x for x in rows if x['argv'] == argv][0]
        mine.update(pumping_at=ago(10), claim='tok-r', pump_pid=pid, pump_created=created, pump_claim='tok-r')
        wip.save_queue(rows)
        os.environ[wip.CLAIM_ENV] = 'tok-r'
        self.addCleanup(os.environ.pop, wip.CLAIM_ENV, None)
        with full, wip.lock():
            with self.assertRaises(wip.WipError):
                wip.check(self.work, 'run', 'n', {}, argv=argv)  # its pumped run is refused again: re-armed
        r = [x for x in wip.queue() if x['id'] == mine['id']][0]
        for k in wip.CLAIM_FIELDS:
            self.assertNotIn(k, r)

    def test_a_job_before_its_runner_exists_is_live_while_its_submitter_is(self):
        d = os.path.join(common.JOBS, '20261026-100000-codex-subm')
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {
            'id': 'x', 'engine': 'codex', 'cwd': self.work, 'brief_text': 'b',
            'primary': {'engine': 'codex', 'model': 'm', 'tier': 'astra'},
            'submitter_pid': os.getpid(), 'submitter_created': jobs._self_created()})
        common.write_json(os.path.join(d, 'status.json'), {'status': 'queued', 'pid': None, 'engine': 'codex'})
        plan = {'engine': 'codex', 'cwd': self.work, 'brief_text': 'b',
                'primary': {'engine': 'codex', 'model': 'm', 'tier': 'astra'}}
        self.assertEqual(jobs.live_duplicate(plan), '20261026-100000-codex-subm')
        pid, created = dead_identity()
        spec = common.read_json(os.path.join(d, 'job.json'))
        spec.update(submitter_pid=pid, submitter_created=created)
        common.write_json(os.path.join(d, 'job.json'), spec)
        self.assertIsNone(jobs.live_duplicate(plan))

    def test_two_identical_submissions_cannot_both_pass(self):
        plan = {'engine': 'codex', 'profile': 'intel', 'cwd': self.work, 'brief_text': 'same',
                'primary': {'engine': 'codex', 'model': 'm', 'tier': 'astra'}, 'notes': []}
        first = jobs.create_job(dict(plan))
        self.assertEqual(common.read_json(os.path.join(common.JOBS, first, 'job.json'))['submitter_pid'], os.getpid())
        with self.assertRaises(jobs.Refusal):
            jobs.create_job(dict(plan))
        self.assertTrue(jobs.create_job(dict(plan, allow_duplicate=True)))

    def test_the_check_runs_under_the_dedupe_lock_and_a_busy_lock_never_refuses(self):
        plan = {'engine': 'codex', 'profile': 'intel', 'cwd': self.work, 'brief_text': 'x',
                'primary': {'engine': 'codex', 'model': 'm', 'tier': 'astra'}, 'notes': []}
        seen = []
        real = jobs.live_duplicate

        def probe(p):
            lock = os.path.join(common.STATE, 'dedupe.lock')
            with self.assertRaises(TimeoutError):
                with common.file_lock(lock, 0):
                    pass
            seen.append(True)
            return real(p)
        with mock.patch.object(jobs, 'live_duplicate', probe):
            jobs.create_job(dict(plan))
        self.assertEqual(seen, [True])
        jobs.DEDUPE_LOCK_S, saved = 0, jobs.DEDUPE_LOCK_S
        self.addCleanup(setattr, jobs, 'DEDUPE_LOCK_S', saved)
        with common.file_lock(os.path.join(common.STATE, 'dedupe.lock'), 0):
            jid = jobs.create_job(dict(plan, brief_text='y'))
        self.assertIn('duplicate check skipped: dedupe.lock busy',
                      common.read_json(os.path.join(common.JOBS, jid, 'job.json'))['notes'])

    def test_archive_keeps_a_job_whose_queue_claim_is_unacknowledged(self):
        from twinlane import archive
        jid = '20260901-120000-codex-abcd'
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'wip_claim': 'tok-out'})
        common.write_json(os.path.join(d, 'status.json'), {'status': 'done'})
        common.write_json(os.path.join(d, 'result.json'), {'id': jid, 'status': 'done',
                                                           'finished_at': ago(30 * 86400)})
        with open(os.path.join(d, jobs.COLLECTED), 'w', encoding='utf-8') as h:
            h.write(common.iso(common.now()))
        wip.save_queue([self.row(claim='tok-out')])
        with mock.patch.object(common, 'pid_state', return_value=common.DEAD):
            self.assertEqual(archive.eligibility(jid, 14, False).get('reason'),
                             'writer-queue request not yet acknowledged')
            wip.save_queue([])
            self.assertIsNone(archive.eligibility(jid, 14, False).get('reason'))


class ReviewR2(PumpDeadClaims):
    """Astra r2: unusable admission evidence never proves non-admission; the start cap holds against fast re-arms."""

    def released_with(self, content):
        pid, created = dead_identity()
        row = self.row(pid=pid, created=created)
        d = os.path.join(common.JOBS, common.stamp() + '-codex-abcd')
        os.makedirs(d)
        if content is not None:
            with open(os.path.join(d, 'job.json'), 'w', encoding='utf-8') as h:
                h.write(content)
        return wip.release_dead_claims([row])

    def test_missing_or_non_object_specs_keep_the_claim(self):
        for content in (None, '[]', 'null', '"text"', '{broken'):
            with self.subTest(content=content):
                self.assertEqual(self.released_with(content), [])
                for n in os.listdir(common.JOBS):
                    import shutil
                    shutil.rmtree(os.path.join(common.JOBS, n))

    def test_a_child_that_rearms_before_settle_still_uses_a_start(self):
        def rearm(row):
            with wip.lock():  # the child is refused again and re-arms before the pump records it
                rows = wip.queue()
                for r in rows:
                    if r['id'] == row['id']:
                        wip._drop_claim(r)
                wip.save_queue(rows)
            return {'pid': 1, 'created': 1}

        wip.save_queue([self.row(claimed_ago=None)])
        for _ in range(wip.PUMP_MAX_TRIES + 2):
            wip.pump({}, busy=0, spawn=rearm, reading=LOW)
        q = wip.queue()[0]
        self.assertEqual(q['pump_tries'], wip.PUMP_MAX_TRIES)
        self.assertIn('the pump started it', wip.launchable(q))
        self.assertEqual(wip.stuck_summary([q]), {'pump start limit reached': 1})


class ReviewR3(PumpDeadClaims):
    """Astra r3: a falsey non-integer start count is unknown, never zero."""

    BAD = (False, None, 0.0, '', [], True, 1.0, '1')

    def test_launchable_refuses_a_malformed_count(self):
        for bad in self.BAD:
            with self.subTest(bad=bad):
                row = self.row(claimed_ago=None)
                row['pump_tries'] = bad
                self.assertIn('the pump started it', wip.launchable(row))
        row = self.row(claimed_ago=None)
        self.assertIsNone(wip.launchable(row))  # absent count: a fresh request

    def test_release_keeps_a_claim_with_a_malformed_count(self):
        pid, created = dead_identity()
        for bad in self.BAD:
            with self.subTest(bad=bad):
                row = self.row(pid=pid, created=created)
                row['pump_tries'] = bad
                self.assertEqual(wip.release_dead_claims([row]), [])

    def test_the_pump_never_starts_a_malformed_count(self):
        started = []
        row = self.row('bad', claimed_ago=None)
        row['pump_tries'] = None
        wip.save_queue([row])
        wip.pump({}, busy=0, spawn=lambda r: started.append(r['id']), reading=LOW)
        self.assertEqual(started, [])


class StuckSummary(unittest.TestCase):
    def test_reasons_are_grouped(self):
        rows = [{'id': 'a', 'kind': 'run', 'argv': ['run', '--brief', '<brief>']},
                {'id': 'b', 'kind': 'baton', 'argv': ['baton']},
                {'id': 'c', 'kind': 'run', 'argv': ['run', '--brief', '<brief>']}]
        self.assertEqual(wip.stuck_summary(rows), {'argv has a placeholder': 2, 'not a run request': 1})


class BoardSlots(tb.BoardBase):
    def setUp(self):
        super().setUp()
        self.addCleanup(hostload.set_sampler, None)
        self.write_meter()

    def test_slots_line_counts_running_and_waiting_per_engine(self):
        hostload.set_sampler(lambda: LOW)
        self.add_job('20261026-100000-codex-aaaa', 'running', engine='codex')
        self.add_job('20261026-100001-codex-bbbb', 'running', engine='codex')
        rc, out = self.run_board('--json')
        data = json.loads(out)
        self.assertRegex(data['slots'], r'^slots: claude 0/\d+ · codex 2/\d+$')
        self.assertIn(data['slots'], self.run_board('--md')[1])
        self.assertIn(data['slots'], self.run_board()[1])

    def test_host_load_shows_the_lower_count(self):
        hostload.set_sampler(lambda: {'cpu_pct': 99.0, 'ram_free_mb': 9000, 'engine_procs': 3})
        live = [{'engine': 'codex', 'status': 'running'}, {'engine': 'codex', 'status': 'queued'}]
        from twinlane import board
        line = board.slots_line(live, {'codex': {'max_parallel': 14}}, hostload.sample({}))
        self.assertIn('codex 1/14 (host allows', line)
        self.assertIn(', 1 waiting', line)

    def test_stuck_requests_are_counted_apart(self):
        from twinlane import board
        rows = [{'id': 'a', 'kind': 'run', 'argv': ['run', '--brief', '<brief>']}]
        self.assertEqual(board.queued_text(rows), '1 (1 stuck: 1 argv has a placeholder)')
        self.assertEqual(board.queued_text([]), '0')


if __name__ == '__main__':
    unittest.main()
