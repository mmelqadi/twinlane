"""Synthetic regression checks for worker admission and post-launch failure boundaries."""
import os
import pathlib
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from twinlane import common, jobs, meter


class AdmissionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='twinlane-admission-')
        self.addCleanup(self.tmp.cleanup)
        self.root = pathlib.Path(self.tmp.name)
        for key in ('STATE', 'JOBS'):
            path = self.root / key.lower()
            path.mkdir()
            patch = mock.patch.object(common, key, str(path))
            patch.start()
            self.addCleanup(patch.stop)
        self.jid = '20261011-000000-codex-abcd'
        pathlib.Path(jobs._p(self.jid, '')).mkdir()

    def spec(self, jid=None):
        return {'id': jid or self.jid, 'profile': 'code', 'cwd': str(self.root)}

    def test_invalid_percentage_never_admits_a_partial_reading(self):
        policy = common.load_policy()
        for value in (True, False, None, 'bad', -1, float('nan'), float('inf')):
            for engine in ('claude', 'codex'):
                with self.subTest(value=value, engine=engine):
                    raw = {'source': 'test', 'reading_at': common.iso(common.now()),
                           'extra_usage_enabled': False,
                           'windows': [{'name': 'weekly', 'pct': 10},
                                       {'name': 'five_hour', 'pct': value}]}
                    finished = getattr(meter, 'finish_' + engine)(raw, policy)
                    self.assertEqual(finished['status'], 'unknown')
                    self.assertTrue(finished['windows'][1]['invalid_pct'])

    def test_worker_guard_blocks_bootstrap_failure_but_advisory_hook_does_not(self):
        twinlane = runpy.run_path(str(pathlib.Path(__file__).resolve().parents[1] / 'twinlane/cli.py'))
        with mock.patch.object(common, 'utf8_stdio'), \
                mock.patch.object(common, 'ensure_dirs', side_effect=OSError('unwritable')), \
                mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': self.jid}):
            self.assertEqual(twinlane['fail_open']('guard'), 2)
            self.assertEqual(twinlane['fail_open']('hook'), 0)

    def test_oauth_null_optional_window_is_absent_but_active_null_is_invalid(self):
        payload = {'weekly': {'utilization': 10, 'resets_at': None},
                   'optional': {'utilization': None, 'resets_at': None}}
        wins, _, _ = meter.normalize_claude_payload(payload)
        self.assertEqual([w['name'] for w in wins], ['weekly'])
        payload['optional']['resets_at'] = '2099-01-01T00:00:00Z'
        wins, _, _ = meter.normalize_claude_payload(payload)
        self.assertTrue(wins[1]['invalid_pct'])

    def test_worker_guard_blocks_initial_import_failure(self):
        target = str(pathlib.Path(__file__).resolve().parents[1] / 'twinlane/cli.py')
        code = """import builtins,runpy,sys
real=builtins.__import__
def blocked(name,*args,**kwargs):
    if name=='twinlane': raise ImportError('synthetic bootstrap failure')
    return real(name,*args,**kwargs)
builtins.__import__=blocked
sys.argv=[sys.argv[1],'guard']
runpy.run_path(sys.argv[0],run_name='__main__')
"""
        result = subprocess.run([sys.executable, '-B', '-c', code, target],
                                env=dict(os.environ, TWINLANE_ENGINE_JOB=self.jid), capture_output=True)
        self.assertEqual(result.returncode, 2, result.stderr)

    def test_unresolved_child_reserves_slot_even_with_terminal_receipt(self):
        common.write_json(jobs._p(self.jid, jobs.CHILD_STARTING), {'engine': 'codex'})
        common.write_json(jobs._p(self.jid, 'result.json'), {'status': 'failed'})
        self.assertEqual(jobs.job_status(self.jid), 'unknown')
        self.assertEqual(jobs.running_count('codex'), 1)
        self.assertEqual(jobs.running_count('claude'), 0)
        self.assertIn('unresolved', jobs._prior_live(self.jid, {'status': 'failed'}))

    def test_unreadable_child_marker_reserves_both_engines(self):
        pathlib.Path(jobs._p(self.jid, jobs.CHILD_STARTING)).write_text('broken')
        for engine in ('codex', 'claude'):
            self.assertEqual(jobs.running_count(engine), 1)

    def test_proven_dead_marker_child_releases_slot_after_runner_crash_or_stop(self):
        common.write_json(jobs._p(self.jid, jobs.CHILD_STARTING),
                          {'engine': 'codex', 'pid': 12345, 'created': 123})
        common.write_json(jobs._p(self.jid, 'result.json'), {'status': 'stopped'})
        with mock.patch.object(jobs, '_pid_state', return_value=common.DEAD):
            self.assertEqual(jobs.job_status(self.jid), 'stopped')
            self.assertEqual(jobs.running_count('codex'), 0)
        self.assertFalse(os.path.exists(jobs._p(self.jid, jobs.CHILD_STARTING)))

    def test_unknown_marker_child_identity_never_reclaims_slot(self):
        common.write_json(jobs._p(self.jid, jobs.CHILD_STARTING),
                          {'engine': 'codex', 'pid': 12345, 'created': 123})
        with mock.patch.object(jobs, '_pid_state', return_value=common.UNKNOWN):
            self.assertTrue(jobs._child_pending(self.jid))
            self.assertEqual(jobs.running_count('codex'), 1)

    def test_child_marker_only_clears_after_child_exits(self):
        path = jobs._p(self.jid, jobs.CHILD_STARTING)
        common.write_json(path, {'engine': 'codex'})
        proc = mock.Mock()
        proc.poll.return_value = None
        jobs._clear_child_starting(self.jid, proc)
        self.assertTrue(os.path.exists(path))
        proc.poll.return_value = 0
        jobs._clear_child_starting(self.jid, proc)
        self.assertFalse(os.path.exists(path))

    def test_live_writer_owner_blocks_another_job(self):
        with mock.patch.object(jobs, '_self_created', return_value=123), \
                mock.patch.object(jobs, '_pid_state', return_value=common.ALIVE):
            path = jobs._claim_writer(self.spec())
            self.assertTrue(os.path.exists(path))
            with self.assertRaises(jobs.Refusal):
                jobs._claim_writer(self.spec('20261011-000001-claude-abcd'))

    def test_empty_allocated_job_does_not_block_writer_admission(self):
        pathlib.Path(jobs._p('20261011-000001-claude-abcd', '')).mkdir()
        with mock.patch.object(jobs, '_self_created', return_value=123):
            self.assertTrue(os.path.exists(jobs._claim_writer(self.spec())))

    def test_missing_spec_with_runtime_evidence_still_blocks_admission(self):
        common.write_json(jobs._p('20261011-000001-claude-abcd', 'status.json'), {'status': 'running'})
        with self.assertRaises(jobs.Refusal):
            jobs._claim_writer(self.spec())

    def test_corrupt_existing_spec_still_blocks_admission(self):
        path = pathlib.Path(jobs._p('20261011-000001-claude-abcd', 'job.json'))
        path.parent.mkdir()
        path.write_text('broken')
        with self.assertRaises(jobs.Refusal):
            jobs._claim_writer(self.spec())

    def test_unresolved_child_prevents_writer_release(self):
        with mock.patch.object(jobs, '_self_created', return_value=123):
            path = jobs._claim_writer(self.spec())
        common.write_json(jobs._p(self.jid, jobs.CHILD_STARTING), {'engine': 'codex'})
        jobs._release_writer(path, self.jid)
        self.assertTrue(os.path.exists(path))
        jobs._clear_child_starting(self.jid)
        jobs._release_writer(path, self.jid)
        self.assertFalse(os.path.exists(path))

    def test_legacy_queued_writer_blocks_but_versioned_queued_writer_does_not(self):
        other = '20261011-000001-claude-abcd'
        for version in (None, 1):
            common.write_json(jobs._p(other, 'job.json'), dict(self.spec(other), writer_reservation_version=version))
            common.write_json(jobs._p(other, 'status.json'), {'status': 'queued'})
            with mock.patch.object(jobs, '_self_created', return_value=123):
                if version is None:
                    with self.assertRaises(jobs.Refusal):
                        jobs._claim_writer(self.spec())
                else:
                    self.assertTrue(os.path.exists(jobs._claim_writer(self.spec())))

    def test_codex_bookkeeping_failure_kills_child_and_preserves_live_lease(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired('synthetic', 0)
        with mock.patch.dict(os.environ, {'TWINLANE_CODEX_BIN': 'synthetic'}), \
                mock.patch.object(common, 'acquire_codex', return_value=('synthetic.exe', None, 'lease')), \
                mock.patch.object(common, 'release_codex') as release, \
                mock.patch.object(jobs.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(common, 'process_created', side_effect=OSError('identity unavailable')), \
                mock.patch.object(common, 'kill_tree', return_value=False) as kill:
            out = jobs._run_codex(self.jid, ['synthetic'], str(self.root), 1,
                                  {'codex_log': str(self.root / 'log')}, {'profile': 'intel'})
        self.assertEqual(out['status'], 'failed')
        kill.assert_called_once_with(proc.pid)
        release.assert_not_called()
        self.assertEqual(jobs.job_status(self.jid), 'unknown')
        self.assertTrue(os.path.exists(jobs._p(self.jid, jobs.SURVIVOR)))

    def test_claude_bookkeeping_failure_kills_child_and_preserves_uncertainty(self):
        proc = mock.Mock(pid=12345)
        proc.poll.return_value = None
        proc.wait.side_effect = subprocess.TimeoutExpired('synthetic', 0)
        brief = self.root / 'brief.md'
        brief.write_text('synthetic')
        files = {k: str(self.root / k) for k in ('stderr', 'events')}
        with mock.patch.object(jobs, 'claude_env', return_value={}), \
                mock.patch.object(jobs.subprocess, 'Popen', return_value=proc), \
                mock.patch.object(common, 'process_created', side_effect=OSError('identity unavailable')), \
                mock.patch.object(common, 'kill_tree', return_value=False) as kill:
            out = jobs._run_claude(self.jid, ['synthetic'], {'cwd': str(self.root)},
                                   {'profile': 'intel', 'timeout_s': 1}, str(brief), files, {})
        self.assertEqual(out['status'], 'failed')
        kill.assert_called_once_with(proc.pid)
        self.assertTrue(jobs._child_pending(self.jid))


if __name__ == '__main__':
    unittest.main()
