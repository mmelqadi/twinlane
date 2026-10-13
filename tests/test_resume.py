"""twinlane resume: lists only lost jobs, resumes on --yes, never twice. No engine is ever started.

Job folders are written by hand. `resume.launch` (the in-process `twinlane run`) is replaced by a recorder, except in
the tests that check the real dry-run route and the caller brief recorded by the real planner."""
import datetime as dt
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
from twinlane import common, host, jobs, resume  # noqa: E402


def meter_with():
    return {'read_at': common.iso(common.now()),
            'claude': {'status': 'ok', 'max_pct': 10, 'next_reset': '2026-10-08T20:00:00Z', 'age_s': 5,
                       'extra_usage_enabled': False},
            'codex': {'status': 'ok', 'max_pct': 10, 'next_reset': '2026-10-09T08:00:00Z', 'age_s': 5}}


def fake_decide(kind, meter, policy, urgent, engine):
    primary = engine or policy['routing'][kind]['primary']
    return {'engine': primary, 'refused': False, 'reason': 'test route', 'fallback_used': False,
            'model': policy['claude']['models']['default'] if primary == 'claude' else None,
            'effort': policy['claude']['effort_by_kind'].get(kind) if primary == 'claude' else None,
            'tier': policy['codex']['tier_by_kind'].get(kind) if primary == 'codex' else None,
            'profile': 'code' if kind == 'code' else 'intel', 'wait_until': None}


class Launcher:
    """Stands in for resume.launch: records every call, answers from a script."""

    def __init__(self):
        self.calls = []
        self.refuse_at = None   # index (among real, non-dry-run calls) that is refused
        self.real = 0

    def __call__(self, argv):
        self.calls.append(list(argv))
        if '--dry-run' in argv:
            return 0, {'dry_run': True, 'engine': 'claude', 'profile': 'intel'}, ''
        n = self.real
        self.real += 1
        if self.refuse_at is not None and n == self.refuse_at:
            return common.EXIT_REFUSED, {'refused': True, 'exit': 3, 'reason': 'refused by policy: claude limit'}, ''
        return 0, {'id': '20261013-000000-claude-%04x' % n}, ''

    def real_calls(self):
        return [c for c in self.calls if '--dry-run' not in c]


class ResumeCase(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='twinlane-resume-'))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        saved = (common.STATE, common.JOBS, common.BATONS, jobs._read_meter, jobs._decide)

        def restore():
            common.STATE, common.JOBS, common.BATONS, jobs._read_meter, jobs._decide = saved
        self.addCleanup(restore)
        home = os.path.join(self.tmp, 'home')
        common.STATE, common.JOBS, common.BATONS = (os.path.join(home, d) for d in ('state', 'jobs', 'batons'))
        for d in (common.STATE, common.JOBS, common.BATONS):
            os.makedirs(d)
        jobs._read_meter = lambda policy: meter_with()
        jobs._decide = fake_decide
        self.cwd = os.path.join(self.tmp, 'work')
        os.makedirs(self.cwd)
        with open(os.path.join(self.cwd, 'README.txt'), 'w', encoding='utf-8') as h:
            h.write('a non-empty working directory')  # an empty one reads as a leftover worktree
        self.counter = 0
        env = mock.patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop('TWINLANE_ENGINE_JOB', None)
        # The host boot time is fixed by each test that cares; by default it is unknown.
        patcher = mock.patch.object(host, 'boot_time', return_value=None)
        self.boot = patcher.start()
        self.addCleanup(patcher.stop)

    # ---- fixtures

    def make_job(self, age_s=3 * 3600, name='task', brief_text='do the thing', caller_brief=None, kind='intel',
                 write=False, baton=None, continue_job=None, cwd=None, pid_state='lost', receipt=None):
        self.counter += 1
        when = common.now() - dt.timedelta(seconds=age_s)
        jid = '%s-claude-%04x' % (when.strftime('%Y%m%d-%H%M%S'), self.counter)
        os.makedirs(os.path.join(common.JOBS, jid))
        created = common.iso(when)
        spec = {'id': jid, 'kind': kind, 'cwd': cwd or self.cwd, 'name': name, 'write': write, 'baton': baton,
                'engine': 'claude', 'primary': {'engine': 'claude'}, 'profile': 'intel', 'brief_text': brief_text,
                'continue_job': continue_job, 'created_at': created}
        if caller_brief is not None:
            spec['caller_brief'] = caller_brief
        common.write_json(os.path.join(common.JOBS, jid, 'job.json'), spec)
        # queued, never got a runner pid, older than the grace period: job_status reads it as lost
        common.write_json(os.path.join(common.JOBS, jid, 'status.json'),
                          {'status': 'queued', 'pid': None, 'engine': 'claude', 'created_at': created})
        if receipt is not None:
            common.write_json(os.path.join(common.JOBS, jid, 'result.json'), receipt)
        return jid

    def path(self, jid, name):
        return os.path.join(common.JOBS, jid, name)

    def run_cli(self, *argv, launcher=None):
        launcher = launcher or Launcher()
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(resume, 'launch', launcher), redirect_stdout(out), redirect_stderr(err):
            rc = resume.cli(list(argv))
        return rc, out.getvalue(), err.getvalue(), launcher

    def run_json(self, *argv, launcher=None):
        rc, out, err, launcher = self.run_cli(*argv, '--json', launcher=launcher)
        return rc, json.loads(out), err, launcher


class ListingTests(ResumeCase):
    def test_dry_run_lists_only_lost(self):
        lost = self.make_job(name='lost-one')
        self.make_job(name='finished', receipt={'status': 'done', 'id': 'x'})
        self.make_job(name='failed', receipt={'status': 'failed'})
        rc, data, _, launcher = self.run_json()
        self.assertEqual(rc, common.EXIT_OK)
        self.assertTrue(data['dry_run'])
        self.assertEqual([c['id'] for c in data['candidates']], [lost])
        self.assertEqual(data['candidates'][0]['route'], 'claude (profile intel)')
        self.assertNotIn('brief_text', data['candidates'][0])
        # A dry run starts nothing and writes nothing into a job folder.
        self.assertEqual(launcher.real_calls(), [])
        self.assertFalse(os.path.exists(self.path(lost, 'resumed.json')))
        self.assertFalse(os.path.exists(self.path(lost, 'caller-brief.md')))
        # Human output says it is a dry run and names the next command.
        rc, out, _, _ = self.run_cli()
        self.assertIn('dry run', out)
        self.assertIn('twinlane resume --yes ' + lost, out)

    def test_unknown_and_orphaned_never_resumed(self):
        unknown = self.make_job(name='unknown')
        orphaned = self.make_job(name='orphaned')
        running = self.make_job(name='running')
        lost = self.make_job(name='lost')
        real_status = jobs.job_status
        forced = {unknown: 'unknown', orphaned: 'orphaned', running: 'running'}

        def status(jid):
            return forced.get(jid) or real_status(jid)
        with mock.patch.object(jobs, 'job_status', side_effect=status):
            rc, data, _, launcher = self.run_json()
            self.assertEqual([c['id'] for c in data['candidates']], [lost])
            # Naming them does not help: nothing starts, and the reason is the status.
            rc, out, err, launcher = self.run_cli('--yes', unknown, orphaned, running)
            self.assertEqual(rc, common.EXIT_REFUSED)
            self.assertEqual(launcher.real_calls(), [])
            for jid, word in ((unknown, 'unknown'), (orphaned, 'orphaned'), (running, 'running')):
                self.assertIn('%s: status is %s, not lost' % (jid, word), err)
            # --all-lost resumes only the lost one.
            rc, data, _, launcher = self.run_json('--yes', '--all-lost')
            self.assertEqual([s['id'] for s in data['started']], [lost])
        for jid in (unknown, orphaned, running):
            self.assertFalse(os.path.exists(self.path(jid, 'resumed.json')))

    def test_already_continued_skipped(self):
        continued = self.make_job(name='continued')
        self.make_job(name='its-continuation', continue_job=continued, receipt={'status': 'done', 'id': 'c'})
        refused_cont = self.make_job(name='retry-me')
        self.make_job(name='refused-continuation', continue_job=refused_cont, receipt={'status': 'refused'})
        marked = self.make_job(name='marked')
        common.write_json(self.path(marked, 'resumed.json'), {'resumed_as': 'elsewhere'})
        fresh = self.make_job(name='fresh')
        rc, data, _, launcher = self.run_json()
        ids = [c['id'] for c in data['candidates']]
        self.assertEqual(sorted(ids), sorted([refused_cont, fresh]))  # a refused continuation started nothing
        reasons = {s['id']: s['reason'] for s in data['skipped']}
        self.assertIn('already continued by', reasons[continued])
        self.assertIn('already resumed as elsewhere', reasons[marked])
        # Named explicitly with --yes: refused outright, nothing started.
        rc, out, err, launcher = self.run_cli('--yes', continued)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(launcher.real_calls(), [])
        self.assertIn('already continued', err)

    def test_window_and_named_jobs(self):
        old = self.make_job(age_s=5 * 86400, name='old')
        recent = self.make_job(name='recent')
        rc, data, _, _ = self.run_json()
        self.assertEqual([c['id'] for c in data['candidates']], [recent])  # default window is 48h
        rc, data, _, _ = self.run_json('--since', '30d')
        self.assertEqual({c['id'] for c in data['candidates']}, {old, recent})
        rc, data, _, _ = self.run_json(old)  # a named job ignores the window
        self.assertEqual([c['id'] for c in data['candidates']], [old])
        rc, _, err, _ = self.run_cli('--since', 'soon')
        self.assertEqual(rc, common.EXIT_USAGE)

    def test_cwd_gone_and_no_brief_are_skipped_with_a_reason(self):
        gone = self.make_job(name='gone', cwd=os.path.join(self.tmp, 'deleted'))
        nobrief = self.make_job(name='nobrief', brief_text='')
        rc, data, _, _ = self.run_json()
        self.assertEqual(data['candidates'], [])
        reasons = {s['id']: s['reason'] for s in data['skipped']}
        self.assertIn('working directory is gone', reasons[gone])
        self.assertIn('no brief was recorded', reasons[nobrief])

    def test_dry_run_shows_a_routing_refusal(self):
        self.make_job()

        def refusing(argv):
            return common.EXIT_REFUSED, {'refused': True, 'reason': 'refused by policy: claude limit'}, ''
        rc, out, _, _ = self.run_cli(launcher=refusing)
        self.assertEqual(rc, common.EXIT_OK)
        self.assertIn('WOULD BE REFUSED: refused by policy: claude limit', out)

    def test_real_dry_run_route_for_a_legacy_job(self):
        """No mocked launcher: the dry run goes through the real planner, via a scratch brief."""
        jid = self.make_job(brief_text='legacy text')
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = resume.cli(['--json'])
        self.assertEqual(rc, common.EXIT_OK, err.getvalue())
        data = json.loads(out.getvalue())
        self.assertEqual(data['candidates'][0]['id'], jid)
        self.assertTrue(data['candidates'][0]['route'].startswith('claude'), data['candidates'][0]['route'])
        self.assertFalse(os.path.exists(self.path(jid, 'caller-brief.md')))


class CauseTests(ResumeCase):
    def test_host_restart_cause(self):
        before_boot = self.make_job(age_s=5 * 3600, name='before')
        after_boot = self.make_job(age_s=1800, name='after')
        boot = (common.now() - dt.timedelta(hours=2)).timestamp()
        self.boot.return_value = boot
        rc, data, _, _ = self.run_json()
        causes = {c['id']: c['cause'] for c in data['candidates']}
        self.assertEqual(causes, {before_boot: 'host-restarted', after_boot: 'runner-died'})
        # No boot time: both are still listed, and the cause is said to be unknown.
        self.boot.return_value = None
        rc, data, _, _ = self.run_json()
        self.assertEqual({c['cause'] for c in data['candidates']}, {'unknown'})
        rc, out, _, _ = self.run_cli()
        self.assertIn('cause: unknown (the host boot time or the job start is unavailable)', out)
        # The marker records the cause it acted on.
        self.boot.return_value = boot
        rc, data, _, _ = self.run_json('--yes', before_boot)
        self.assertEqual(common.read_json(self.path(before_boot, 'resumed.json'))['cause'], 'host-restarted')


class ArgvTests(ResumeCase):
    def test_argv_from_caller_brief(self):
        # The job was a continuation: brief_text carries appended context, caller_brief is what the user wrote.
        jid = self.make_job(name='feature', kind='code', write=True, baton='my-baton',
                            brief_text='original\n\n---\n\ntwinlane job X did not finish ...',
                            caller_brief='original request\n')
        rc, data, _, launcher = self.run_json('--yes', jid)
        self.assertEqual(rc, common.EXIT_OK)
        (argv,) = launcher.real_calls()
        brief = self.path(jid, 'caller-brief.md')
        self.assertEqual(argv, ['--continue-job', jid, '--brief', brief, '--cd', self.cwd, '--kind', 'code',
                                '--write', '--engine', 'auto', '--bg', '--name', 'feature-resume',
                                '--baton', 'my-baton'])
        with open(brief, encoding='utf-8', newline='') as h:
            self.assertEqual(h.read(), 'original request\n')
        self.assertEqual(data['started'][0]['id'], jid)

    def test_argv_omits_write_and_baton_when_absent(self):
        jid = self.make_job(name='review', kind='review', caller_brief='look at it')
        self.run_cli('--yes', jid)
        argv = self.last_argv
        self.assertNotIn('--write', argv)
        self.assertNotIn('--baton', argv)
        self.assertEqual(argv[argv.index('--kind') + 1], 'review')

    @property
    def last_argv(self):
        return self._launcher.real_calls()[-1]

    def run_cli(self, *argv, launcher=None):
        self._launcher = launcher or Launcher()
        return super().run_cli(*argv, launcher=self._launcher)

    def test_legacy_job_uses_brief_text(self):
        jid = self.make_job(name='old-style', brief_text='the only text this job kept')  # no caller_brief
        rc, out, _, launcher = self.run_cli()
        self.assertIn('brief: brief_text', out)
        self.assertIn('recorded before caller briefs were kept', out)
        rc, data, _, launcher = self.run_json('--yes', jid)
        self.assertEqual(rc, common.EXIT_OK)
        with open(self.path(jid, 'caller-brief.md'), encoding='utf-8', newline='') as h:
            self.assertEqual(h.read(), 'the only text this job kept')
        self.assertEqual(launcher.real_calls()[0][launcher.real_calls()[0].index('--brief') + 1],
                         self.path(jid, 'caller-brief.md'))


class RunTests(ResumeCase):
    def test_stops_on_route_refusal(self):
        first = self.make_job(age_s=6 * 3600, name='a')
        second = self.make_job(age_s=5 * 3600, name='b')
        third = self.make_job(age_s=4 * 3600, name='c')
        launcher = Launcher()
        launcher.refuse_at = 1
        rc, data, err, launcher = self.run_json('--yes', '--all-lost', launcher=launcher)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual([s['id'] for s in data['started']], [first])
        self.assertEqual(data['stopped']['id'], second)
        self.assertIn('claude limit', data['stopped']['reason'])
        self.assertEqual(data['not_attempted'], [third])
        self.assertEqual(len(launcher.real_calls()), 2)  # oldest first; the third was never tried
        self.assertTrue(os.path.exists(self.path(first, 'resumed.json')))
        self.assertFalse(os.path.exists(self.path(second, 'resumed.json')))
        self.assertFalse(os.path.exists(self.path(third, 'resumed.json')))
        # Human output names where it stopped.
        rc, out, err, _ = self.run_cli('--yes', second, third, launcher=_Refuse())
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('stopped at %s' % second, err)
        self.assertIn('not attempted: %s' % third, err)

    def test_resumed_marker_written(self):
        jid = self.make_job()
        rc, data, _, launcher = self.run_json('--yes', '--all-lost')
        self.assertEqual(rc, common.EXIT_OK)
        new_id = data['started'][0]['resumed_as']
        marker = common.read_json(self.path(jid, 'resumed.json'))
        self.assertEqual(marker['resumed_as'], new_id)
        self.assertTrue(marker['resumed_at'])
        self.assertEqual(marker['argv'], launcher.real_calls()[0])
        # Never twice: the same request again starts nothing.
        rc, data, err, launcher2 = self.run_json('--yes', '--all-lost')
        self.assertEqual(rc, common.EXIT_OK)
        self.assertEqual(launcher2.real_calls(), [])
        self.assertEqual(data['started'], [])
        rc, out, err, launcher3 = self.run_cli('--yes', jid)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(launcher3.real_calls(), [])
        self.assertIn('already resumed as ' + new_id, err)

    def test_marker_is_not_written_for_a_refused_start(self):
        jid = self.make_job()
        launcher = Launcher()
        launcher.refuse_at = 0
        rc, data, _, _ = self.run_json('--yes', jid, launcher=launcher)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertFalse(os.path.exists(self.path(jid, 'resumed.json')))
        # It can be retried later.
        rc, data, _, launcher = self.run_json('--yes', jid)
        self.assertEqual(rc, common.EXIT_OK)
        self.assertTrue(os.path.exists(self.path(jid, 'resumed.json')))

    def test_batch_cap_from_policy(self):
        ids = [self.make_job(age_s=(9 - i) * 3600, name='j%d' % i) for i in range(4)]
        policy = common.load_policy()
        policy['resume'] = dict(policy['resume'], max_batch=2)
        with mock.patch.object(common, 'load_policy', return_value=policy):
            rc, data, _, _ = self.run_json()
            self.assertEqual([c['id'] for c in data['candidates']], ids[:2])
            self.assertEqual(data['deferred'], ids[2:])
            rc, data, _, launcher = self.run_json('--yes', '--all-lost')
        self.assertEqual([s['id'] for s in data['started']], ids[:2])
        self.assertEqual(data['not_attempted'], ids[2:])
        self.assertEqual(len(launcher.real_calls()), 2)

    def test_yes_needs_a_selection(self):
        self.make_job()
        rc, out, err, launcher = self.run_cli('--yes')
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertEqual(launcher.real_calls(), [])
        self.assertIn('--yes needs JOB ids or --all-lost', err)

    def test_refused_inside_a_worker(self):
        jid = self.make_job()
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': '20261013-120000-codex-0001'}):
            rc, out, err, launcher = self.run_cli('--yes', jid)
            self.assertEqual(rc, common.EXIT_REFUSED)
            self.assertIn('refused inside a twinlane worker job', err)
            self.assertEqual(launcher.real_calls(), [])
            self.assertFalse(os.path.exists(self.path(jid, 'resumed.json')))
            # Listing changes nothing, so it stays available.
            rc, out, err, launcher = self.run_cli()
            self.assertEqual(rc, common.EXIT_OK)

    def test_a_lock_held_elsewhere_stops_the_batch(self):
        jid = self.make_job()
        with mock.patch.object(common, 'file_lock', side_effect=TimeoutError('busy')):
            rc, data, _, launcher = self.run_json('--yes', jid)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(launcher.real_calls(), [])
        self.assertIn('another resume holds this job', data['stopped']['reason'])


class _Refuse:
    def __call__(self, argv):
        if '--dry-run' in argv:
            return 0, {'engine': 'claude', 'profile': 'intel'}, ''
        return common.EXIT_REFUSED, {'refused': True, 'reason': 'refused by policy: claude limit'}, ''

    def real_calls(self):
        return []


class PlannerRecordsTheCallerBrief(ResumeCase):
    """The real planner keeps the brief as the caller wrote it, apart from continue-job context."""

    def plan(self, brief_text, *extra):
        brief = os.path.join(self.tmp, 'brief-%d.md' % self.counter)
        self.counter += 1
        with open(brief, 'w', encoding='utf-8') as h:
            h.write(brief_text)
        args = jobs._run_parser().parse_args(['--brief', brief, '--cd', self.cwd, '--engine', 'claude', *extra])
        return jobs.plan_run(args, common.load_policy())

    def test_caller_brief_recorded_and_written(self):
        plan = self.plan('Investigate the cache.')
        self.assertEqual(plan['caller_brief'], 'Investigate the cache.')
        jid = jobs.create_job(plan)
        self.assertEqual(common.read_json(self.path(jid, 'job.json'))['caller_brief'], 'Investigate the cache.')
        with open(self.path(jid, 'caller-brief.md'), encoding='utf-8', newline='') as h:
            self.assertEqual(h.read(), 'Investigate the cache.')
        # The brief the engine reads still has the contract.
        with open(self.path(jid, 'brief.md'), encoding='utf-8') as h:
            self.assertIn('Investigate the cache.', h.read())

    def test_continuation_keeps_the_unmodified_caller_brief(self):
        prior = self.make_job(brief_text='first')
        plan = self.plan('Second try.', '--continue-job', prior)
        self.assertEqual(plan['caller_brief'], 'Second try.')
        self.assertIn('did not finish', plan['brief_text'])  # the context appended for the engine only
        self.assertNotIn('did not finish', plan['caller_brief'])

    def test_resume_end_to_end_with_the_real_planner(self):
        """A lost job created by the real planner resumes from its caller brief through the real `run`."""
        plan = self.plan('Summarise the repo layout.', '--name', 'layout')
        jid = jobs.create_job(plan)
        spec_path = self.path(jid, 'job.json')
        old = common.iso(common.now() - dt.timedelta(hours=3))
        spec = common.read_json(spec_path)
        spec['created_at'] = old
        common.write_json(spec_path, spec)
        common.write_json(self.path(jid, 'status.json'), {'status': 'queued', 'pid': None, 'created_at': old})
        started = []

        def no_runner(new_id, plan, as_json):  # jobs._start_background: no process may start
            started.append(new_id)
            common.emit({'id': new_id}, as_json)
            return common.EXIT_OK
        out = io.StringIO()
        with mock.patch.object(jobs, '_start_background', side_effect=no_runner), redirect_stdout(out):
            rc = resume.cli(['--yes', jid])
        self.assertEqual(rc, common.EXIT_OK)
        self.assertEqual(len(started), 1)
        new_spec = common.read_json(self.path(started[0], 'job.json'))
        self.assertEqual(new_spec['continue_job'], jid)
        self.assertEqual(new_spec['caller_brief'], 'Summarise the repo layout.')
        self.assertEqual(new_spec['name'], 'layout-resume')
        self.assertEqual(common.read_json(self.path(jid, 'resumed.json'))['resumed_as'], started[0])


class WorkerLineageEnv(ResumeCase):
    """A twinlane worker carries exactly one lineage id: its own job."""

    def test_claude_env_carries_the_new_lineage_id(self):
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'other-parent'}):
            for profile in ('intel', 'code'):
                env = jobs.claude_env('new-job', profile=profile, cwd=self.cwd)
                self.assertEqual(env['TWINLANE_ENGINE_JOB'], 'new-job')
            # The parent process is left alone.
            self.assertEqual(os.environ['TWINLANE_ENGINE_JOB'], 'other-parent')

    def test_codex_env_carries_the_new_lineage_id(self):
        seen = {}

        def popen(argv, **kw):
            seen.update(kw.get('env') or {})
            raise OSError('stop before starting anything')
        jid = 'new-job'
        os.makedirs(self.path(jid, 'codex'))
        files = {'codex_log': self.path(jid, 'codex.log'), 'codex_out': self.path(jid, 'codex')}
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'other-parent'}), \
                mock.patch.object(common, 'acquire_codex_binary', return_value=('codex-stub', None, None)), \
                mock.patch.object(common, 'codex_version_refusal', return_value=None), \
                mock.patch.object(jobs.subprocess, 'Popen', side_effect=popen):
            result = jobs._run_codex(jid, ['codex-stub'], self.cwd, 5, files, {'profile': 'intel'},
                                     common.load_policy())
        self.assertEqual(result['status'], 'failed')
        self.assertEqual(seen.get('TWINLANE_ENGINE_JOB'), jid)

    def test_worker_env_helper(self):
        env = jobs.worker_env({'TWINLANE_ENGINE_JOB': 'b', 'PATH': 'p'}, 'c')
        self.assertEqual(env, {'TWINLANE_ENGINE_JOB': 'c', 'PATH': 'p'})


if __name__ == '__main__':
    unittest.main()
