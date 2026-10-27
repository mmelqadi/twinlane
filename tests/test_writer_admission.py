"""writer_admission: read-only snapshot, pure evaluator, and parity with jobs._claim_writer.

Synthetic temp stores only: no real job, reservation or process of this machine is read or observed.
Process observations are injected; the only real probe in this file is of the test process itself.
"""
import builtins
import copy
import io
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from contextlib import contextmanager, redirect_stdout

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
import git_fixtures  # noqa: E402
from twinlane import common, jobs, writer_admission as wa  # noqa: E402

ALIVE, DEAD, UNKNOWN = common.ALIVE, common.DEAD, common.UNKNOWN
TARGET = '20261013-000000-claude-self'
SELF_CREATED = 4_242_424_242


def tree(root):
    """Every directory listing, file byte and mtime under root: an unchanged tree is an unchanged store."""
    out = {}
    for dirpath, dirnames, filenames in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        out[rel] = ('dir', sorted(dirnames), sorted(filenames))
        for name in filenames:
            path = os.path.join(dirpath, name)
            with open(path, 'rb') as h:
                out[os.path.join(rel, name)] = ('file', h.read(), os.stat(path).st_mtime_ns)
    return out


class Store:
    """A throwaway jobs/ + state/ pair and a repo folder, written directly (never through jobs.py)."""

    def __init__(self, test):
        tmp = tempfile.mkdtemp(prefix='twinlane-wa-')
        test.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        self.root = pathlib.Path(tmp)
        self.jobs, self.state = self.root / 'jobs', self.root / 'state'
        self.repo, self.other = self.root / 'repo', self.root / 'other-repo'
        for d in (self.jobs, self.state, self.repo, self.other):
            d.mkdir()
        self.cwd = wa.writer_cwd(str(self.repo))

    def job(self, jid, cwd=None, profile='code', version=1, spec=True, **files):
        d = self.jobs / jid
        d.mkdir(exist_ok=True)
        if spec is True:
            body = {'id': jid, 'profile': profile, 'cwd': str(cwd or self.repo), 'engine': 'claude',
                    'brief_text': 'SECRET-BRIEF-PAYLOAD do not print'}
            if version is not None:
                body['writer_reservation_version'] = version
            self.put(d / 'job.json', body)
        elif spec is not None:
            self.put(d / 'job.json', spec)
        for name, value in files.items():
            self.put(d / {'status': 'status.json', 'result': 'result.json', 'survivor': 'survivor.json',
                          'pending': 'child-starting.json', 'runner_pid': 'runner.pid'}[name], value)
        return jid

    def reservation(self, value, cwd=None):
        path = self.state / 'writers' / wa.reservation_name(cwd or self.cwd)
        path.parent.mkdir(exist_ok=True)
        self.put(path, value)

    @staticmethod
    def put(path, value):
        path = pathlib.Path(path)
        if isinstance(value, bytes):
            path.write_bytes(value)
        elif isinstance(value, str):
            path.write_text(value, encoding='utf-8')
        else:
            path.write_text(json.dumps(value), encoding='utf-8')


def table_observer(table, calls=None):
    def observe(pid, created):
        if calls is not None:
            calls.append((pid, created))
        return {'state': table.get((pid, created), UNKNOWN), 'reason': 'synthetic', 'pid': pid,
                'expected_created': None if created is None else str(created), 'stages': []}
    return observe


class Base(unittest.TestCase):
    def setUp(self):
        self.s = Store(self)

    def explain(self, table=None, target=TARGET, observer=None):
        snap = wa.capture_snapshot(str(self.s.repo), str(self.s.jobs), str(self.s.state),
                                   observer=observer or table_observer(table or {}), target_job_id=target)
        return wa.evaluate(snap)

    def reasons(self, report):
        out = [f['reason'] for f in report['findings']]
        out += [r['reason'] for r in report['reservation'].get('reasons', [])]
        for j in report['jobs']:
            out += [r['reason'] for r in j['reasons']]
        out += [u['reason'] for u in report['unclassified']]
        return out


class ReportShape(Base):
    def test_empty_store_has_no_observed_blocker_and_never_grants(self):
        report = self.explain()
        self.assertEqual(report['decision'], wa.NO_OBSERVED_BLOCKER)
        self.assertTrue(report['proposal_only'])
        self.assertFalse(report['grants_admission'])
        self.assertNotIn('allow', json.dumps(report).lower())
        self.assertTrue(report['snapshot']['stable'])
        self.assertEqual(report['snapshot']['generation'], report['snapshot']['second_generation'])

    def test_report_is_allowlisted(self):
        self.s.job('a', status={'status': 'running', 'pid': 11, 'pid_created': 1, 'prompt': 'SECRET-PROMPT'},
                   survivor={'pid': 12, 'created': 2, 'why': 'SECRET-WHY'},
                   result={'status': 'done', 'final': 'SECRET-FINAL'})
        text = json.dumps(self.explain({(11, 1): ALIVE, (12, 2): DEAD}))
        for secret in ('SECRET-BRIEF-PAYLOAD', 'SECRET-PROMPT', 'SECRET-WHY', 'SECRET-FINAL'):
            self.assertNotIn(secret, text)

    def test_all_same_cwd_code_jobs_are_reported_not_just_the_first_blocker(self):
        for jid in ('a', 'b', 'c'):
            self.s.job(jid, status={'status': 'running', 'pid': 100 + ord(jid), 'pid_created': 7})
        self.s.job('elsewhere', cwd=self.s.other, status={'status': 'running', 'pid': 5, 'pid_created': 7})
        self.s.job('intel', profile='intel', status={'status': 'running', 'pid': 6, 'pid_created': 7})
        report = self.explain({(197, 7): ALIVE, (198, 7): UNKNOWN, (199, 7): DEAD, (5, 7): ALIVE, (6, 7): ALIVE})
        self.assertEqual([j['id'] for j in report['jobs']], ['a', 'b', 'c'])
        self.assertEqual([j['effect'] for j in report['jobs']], [wa.BLOCKED, wa.UNKNOWN, None])
        self.assertEqual(report['skipped']['other_profile_or_cwd'], 2)
        self.assertEqual(report['decision'], wa.BLOCKED)

    def test_unrelated_cwd_does_not_block(self):
        self.s.job('elsewhere', cwd=self.s.other, status={'status': 'running', 'pid': 5, 'pid_created': 7})
        self.assertEqual(self.explain({(5, 7): ALIVE})['decision'], wa.NO_OBSERVED_BLOCKER)

    def test_target_job_is_skipped_like_the_claimant(self):
        self.s.job(TARGET, status={'status': 'running', 'pid': 5, 'pid_created': 7})
        report = self.explain({(5, 7): ALIVE})
        self.assertEqual((report['decision'], report['skipped']['target_job']), (wa.NO_OBSERVED_BLOCKER, 1))
        self.assertEqual(self.explain({(5, 7): ALIVE}, target=None)['decision'], wa.BLOCKED)


class Reservations(Base):
    def owner(self, **over):
        return dict({'id': 'owner', 'cwd': self.s.cwd, 'pid': 50, 'created': 500}, **over)

    def test_malformed_reservations_block(self):
        for value in ('not json', b'\xef\xbb\xbf{}', [], {'id': 'owner'}, self.owner(cwd='elsewhere'),
                      self.owner(id=5), self.owner(pid='50'), self.owner(created=None), self.owner(created=1.5)):
            with self.subTest(value=value):
                self.s.reservation(value)
                report = self.explain({(50, 500): DEAD})
                self.assertEqual(report['decision'], wa.BLOCKED)
                self.assertIn('reservation_unreadable_or_incomplete', self.reasons(report))

    def test_bool_identity_is_unknown_not_dead(self):
        self.s.reservation(self.owner(pid=True))
        report = self.explain({(1, 500): DEAD})
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertEqual(report['reservation']['owner']['reason'], 'invalid_identity')

    def test_owner_states(self):
        for state, decision in ((ALIVE, wa.BLOCKED), (UNKNOWN, wa.UNKNOWN), (DEAD, wa.NO_OBSERVED_BLOCKER)):
            with self.subTest(state=state):
                self.s.reservation(self.owner())
                self.assertEqual(self.explain({(50, 500): state})['decision'], decision)

    def test_dead_owner_with_busy_job_still_holds(self):
        self.s.reservation(self.owner())
        self.s.job('owner', cwd=self.s.other, status={'status': 'done', 'child_pid': 60, 'child_created': 600},
                   result={'status': 'done'})
        report = self.explain({(50, 500): DEAD, (60, 600): ALIVE})
        self.assertEqual(report['decision'], wa.BLOCKED)
        self.assertIn('reservation_owner_job_engine_alive', self.reasons(report))

    def test_unreadable_reservation_is_unknown(self):
        path = self.s.state / 'writers' / wa.reservation_name(self.s.cwd)
        path.mkdir(parents=True)  # a folder where the file should be: it exists but cannot be read
        report = self.explain()
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertIn('reservation_unreadable', self.reasons(report))


class JobFiles(Base):
    def test_unreadable_spec_and_status_are_unknown(self):
        d = self.s.jobs / 'a'
        d.mkdir()
        (d / 'job.json').mkdir()
        report = self.explain()
        self.assertEqual((report['decision'], self.reasons(report)), (wa.UNKNOWN, ['job_spec_unreadable']))
        shutil.rmtree(d)
        self.s.job('b')
        (self.s.jobs / 'b' / 'status.json').mkdir()
        report = self.explain()
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertIn('status_unreadable', self.reasons(report))

    def test_invalid_or_non_object_files_block(self):
        cases = {'spec': dict(spec='broken'), 'spec-list': dict(spec=[1]), 'status': dict(status='broken'),
                 'status-null': dict(status=None), 'status-list': dict(status=[])}
        for name, kw in cases.items():
            with self.subTest(name=name):
                self.s.job(name, **kw)
                report = self.explain()
                self.assertEqual(report['decision'], wa.BLOCKED, self.reasons(report))
                shutil.rmtree(self.s.jobs / name)

    def test_missing_spec_with_runtime_evidence_blocks_but_empty_folder_does_not(self):
        (self.s.jobs / 'empty').mkdir()
        self.assertEqual(self.explain()['decision'], wa.NO_OBSERVED_BLOCKER)
        self.s.job('evidence', spec=None, status={'status': 'running'})
        self.assertEqual(self.explain()['decision'], wa.BLOCKED)

    def test_legacy_queued_needs_a_receipt_to_qualify(self):
        self.s.job('legacy', version=None, status={'status': 'queued'})
        report = self.explain()
        self.assertEqual(report['decision'], wa.BLOCKED)
        self.assertIn('legacy_unqualified_prelaunch', self.reasons(report))
        self.s.put(self.s.jobs / 'legacy' / 'result.json', {'status': 'refused'})
        self.assertEqual(self.explain()['decision'], wa.NO_OBSERVED_BLOCKER)
        self.s.put(self.s.jobs / 'legacy' / 'result.json', {'final': 'no status'})
        self.assertEqual(self.explain()['decision'], wa.BLOCKED)
        (self.s.jobs / 'legacy' / 'result.json').unlink()
        (self.s.jobs / 'legacy' / 'result.json').mkdir()
        self.assertIn('receipt_unreadable', self.reasons(self.explain()))

    def test_legacy_running_without_pid_is_unqualified(self):
        self.s.job('legacy', version=None, status={'status': 'running'})
        self.assertIn('legacy_unqualified_prelaunch', self.reasons(self.explain()))

    def test_modern_queued_without_attempt_does_not_block(self):
        self.s.job('modern', status={'status': 'queued'})
        report = self.explain()
        self.assertEqual(report['decision'], wa.NO_OBSERVED_BLOCKER)
        self.assertEqual(report['jobs'][0]['reasons'][0]['reason'], 'modern_queued_without_attempt')
        self.s.job('modern', status={'status': 'queued', 'attempt': 1, 'pid': 9, 'pid_created': 90})
        self.assertEqual(self.explain({(9, 90): ALIVE})['decision'], wa.BLOCKED)

    def test_ambiguous_version_is_not_treated_as_modern(self):
        self.s.job('odd', version=True, status={'status': 'queued'})
        report = self.explain()
        self.assertEqual(report['decision'], wa.BLOCKED)  # read as legacy: queued with no receipt
        self.assertIn('reservation_version_ambiguous', self.reasons(report))

    def test_runner_pid_file_fallback(self):
        self.s.job('old', version=None, status={'status': 'running'}, runner_pid='77 770\n')
        self.assertEqual(self.explain({(77, 770): ALIVE})['decision'], wa.BLOCKED)
        self.assertEqual(self.explain({(77, 770): DEAD})['decision'], wa.NO_OBSERVED_BLOCKER)
        self.s.put(self.s.jobs / 'old' / 'runner.pid', '﻿77')
        self.assertEqual(self.explain({(77, None): ALIVE})['decision'], wa.BLOCKED)

    def test_malformed_runner_identity_is_unknown(self):
        self.s.job('a', status={'status': 'running', 'pid': '77', 'pid_created': 770})
        report = self.explain({(77, 770): DEAD})
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertEqual(report['jobs'][0]['runner']['reason'], 'invalid_identity')


class EngineEvidence(Base):
    def test_child_and_survivor_disagreement_takes_the_stronger(self):
        cases = [((ALIVE, DEAD), wa.BLOCKED), ((DEAD, ALIVE), wa.BLOCKED), ((DEAD, UNKNOWN), wa.UNKNOWN),
                 ((UNKNOWN, DEAD), wa.UNKNOWN), ((DEAD, DEAD), wa.NO_OBSERVED_BLOCKER)]
        for (child, survivor), decision in cases:
            with self.subTest(child=child, survivor=survivor):
                self.s.job('a', status={'status': 'done', 'child_pid': 61, 'child_created': 610},
                           survivor={'pid': 62, 'created': 620}, result={'status': 'done'})
                report = self.explain({(61, 610): child, (62, 620): survivor})
                self.assertEqual(report['decision'], decision)
                engine = report['jobs'][0]['engine']
                self.assertEqual((engine['child']['state'], engine['survivor']['state']), (child, survivor))

    def test_stampless_survivor_is_never_alive_only_unknown_or_dead(self):
        self.s.job('a', status={'status': 'done'}, survivor={'pid': 62, 'created': None}, result={'status': 'done'})
        report = self.explain({(62, None): ALIVE})
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertEqual(report['jobs'][0]['engine']['survivor']['effective_state'], UNKNOWN)
        self.assertEqual(self.explain({(62, None): DEAD})['decision'], wa.NO_OBSERVED_BLOCKER)

    def test_malformed_survivor_blocks(self):
        self.s.job('a', status={'status': 'done'}, survivor={'pid': '62'}, result={'status': 'done'})
        self.assertIn('survivor_malformed', self.reasons(self.explain()))

    def test_receipt_done_never_outweighs_an_unknown_child(self):
        self.s.job('a', status={'status': 'running', 'pid': 70, 'pid_created': 700, 'child_pid': 71,
                                'child_created': 710}, result={'status': 'done'})
        report = self.explain({(70, 700): DEAD, (71, 710): UNKNOWN})
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertIn('engine_unknown', self.reasons(report))

    def test_pending_launch_is_always_unknown(self):
        cases = [({'engine': 'codex'}, None, 'pending_launch'),
                 ('broken', None, 'pending_launch'),
                 ({'engine': 'codex', 'pid': 80, 'created': 800}, UNKNOWN, 'pending_launch'),
                 ({'engine': 'codex', 'pid': 80, 'created': 800}, ALIVE, 'pending_launch'),
                 ({'engine': 'codex', 'pid': 80, 'created': 800}, DEAD, 'pending_launch_cleanup_dependent'),
                 ({'engine': 'codex', 'pid': 80, 'created': 8.0e2}, DEAD, 'pending_launch')]
        for marker, state, reason in cases:
            with self.subTest(marker=marker, state=state):
                self.s.job('a', status={'status': 'done'}, pending=marker, result={'status': 'done'})
                report = self.explain({(80, 800): state} if state else {})
                self.assertEqual(report['decision'], wa.UNKNOWN)
                self.assertIn(reason, self.reasons(report))
                self.assertEqual(report['jobs'][0]['engine']['engine_state'], UNKNOWN)
                self.assertTrue((self.s.jobs / 'a' / 'child-starting.json').exists(), 'the marker was removed')

    def test_malformed_child_identity_is_unknown(self):
        for child in ('71', True, 7.1, 0):
            with self.subTest(child=child):
                self.s.job('a', status={'status': 'done', 'child_pid': child, 'child_created': 710},
                           result={'status': 'done'})
                self.assertEqual(self.explain({(71, 710): DEAD})['decision'], wa.UNKNOWN)


class SnapshotSafety(Base):
    def test_race_between_reads_is_unknown(self):
        self.s.job('a', status={'status': 'running', 'pid': 5, 'pid_created': 7})

        def racing(pid, created):
            self.s.put(self.s.jobs / 'a' / 'status.json', {'status': 'running', 'pid': 5, 'pid_created': 8})
            return table_observer({(5, 7): DEAD})(pid, created)
        report = self.explain(observer=racing)
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertFalse(report['snapshot']['stable'])
        self.assertIn('file:a/status.json', report['snapshot']['races'])
        self.assertIn('snapshot_changed_between_reads', self.reasons(report))

    def test_new_job_folder_between_reads_is_a_race(self):
        self.s.job('a', status={'status': 'running', 'pid': 5, 'pid_created': 7})

        def racing(pid, created):
            self.s.job('late')
            return table_observer({(5, 7): DEAD})(pid, created)
        report = self.explain(observer=racing)
        self.assertIn('entry:late', report['snapshot']['races'])
        self.assertEqual(report['decision'], wa.UNKNOWN)

    def test_jobs_root_failures_are_unknown(self):
        shutil.rmtree(self.s.jobs)
        report = self.explain()
        self.assertEqual((report['decision'], report['snapshot']['jobs_root']), (wa.UNKNOWN, 'absent'))
        self.s.jobs.write_text('not a folder')
        report = self.explain()
        self.assertEqual((report['decision'], report['snapshot']['jobs_root']), (wa.UNKNOWN, 'unreadable'))

    def test_observer_failure_is_unknown(self):
        self.s.job('a', status={'status': 'running', 'pid': 5, 'pid_created': 7})

        def broken(pid, created):
            raise OSError('synthetic')
        self.assertEqual(self.explain(observer=broken)['decision'], wa.UNKNOWN)

    def test_capture_changes_nothing_on_disk(self):
        self.s.reservation({'id': 'a', 'cwd': self.s.cwd, 'pid': 5, 'created': 7})
        self.s.job('a', status={'status': 'done', 'child_pid': 6, 'child_created': 8}, result={'status': 'done'},
                   pending={'engine': 'codex', 'pid': 6, 'created': 8}, survivor={'pid': 9, 'created': 10})
        (self.s.jobs / 'empty').mkdir()
        before = tree(self.s.root)
        self.explain({(5, 7): DEAD, (6, 8): DEAD, (9, 10): DEAD})
        self.assertEqual(tree(self.s.root), before)

    def test_only_valid_identities_reach_the_observer(self):
        self.s.reservation({'id': 'a', 'cwd': self.s.cwd, 'pid': True, 'created': 7})
        self.s.job('a', status={'status': 'running', 'pid': 5, 'pid_created': 7.0, 'child_pid': 6, 'child_created': ''},
                   survivor={'pid': 9, 'created': 2 ** 70})
        calls = []
        self.explain(observer=table_observer({}, calls))
        self.assertEqual(calls, [(6, None)])

    def test_evaluate_is_pure(self):
        self.s.reservation({'id': 'a', 'cwd': self.s.cwd, 'pid': 5, 'created': 7})
        self.s.job('a', status={'status': 'running', 'pid': 5, 'pid_created': 7, 'child_pid': 6, 'child_created': 8},
                   pending={'engine': 'codex', 'pid': 6, 'created': 8}, survivor={'pid': 9, 'created': None},
                   runner_pid='5 7')
        self.s.job('legacy', version=None, status={'status': 'queued'})
        snap = wa.capture_snapshot(str(self.s.repo), str(self.s.jobs), str(self.s.state),
                                   observer=table_observer({(5, 7): DEAD, (6, 8): DEAD, (9, None): ALIVE}))
        frozen = copy.deepcopy(snap)
        boom = AssertionError('the evaluator touched the OS')
        with mock.patch.object(builtins, 'open', side_effect=boom), \
                mock.patch.object(os, 'scandir', side_effect=boom), \
                mock.patch.object(os, 'stat', side_effect=boom), \
                mock.patch.object(os, 'listdir', side_effect=boom), \
                mock.patch.object(os.path, 'exists', side_effect=boom), \
                mock.patch.object(os.path, 'realpath', side_effect=boom), \
                mock.patch.object(os, 'remove', side_effect=boom), \
                mock.patch.object(common, 'observe_process', side_effect=boom), \
                mock.patch.object(common, '_win_observe', side_effect=boom), \
                mock.patch.object(common, 'pid_state', side_effect=boom), \
                mock.patch.object(common, 'read_json', side_effect=boom), \
                mock.patch.object(subprocess, 'Popen', side_effect=boom):
            first = wa.evaluate(snap)
            second = wa.evaluate(snap)
        self.assertEqual(first, second)
        self.assertEqual(snap, frozen)
        self.assertEqual(first['decision'], wa.BLOCKED)
        self.assertIn('pending_launch_cleanup_dependent', self.reasons(first))

    def test_import_creates_no_directory(self):
        home = os.path.join(tempfile.mkdtemp(prefix='twinlane-wa-home-'), 'never-created')
        self.addCleanup(shutil.rmtree, os.path.dirname(home), ignore_errors=True)
        result = subprocess.run([sys.executable, '-B', '-c', 'import twinlane.writer_admission'], cwd=ROOT,
                                env=dict(os.environ, TWINLANE_HOME=home), capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(os.path.exists(home))

    def test_names_and_canonical_cwd_match_jobs(self):
        (self.s.repo / '.git').mkdir()
        (self.s.repo / 'sub').mkdir()
        self.assertEqual(wa.writer_cwd(self.s.repo / 'sub'), wa.writer_cwd(self.s.repo))
        self.assertEqual(wa.writer_cwd(self.s.repo / 'sub'), jobs._writer_cwd(self.s.repo / 'sub'))
        self.assertEqual((wa.SURVIVOR, wa.CHILD_STARTING, wa.RUNNER_LOCK), (jobs.SURVIVOR, jobs.CHILD_STARTING, jobs.RUNNER_LOCK))
        for path in (str(self.s.repo), str(self.s.repo) + os.sep, str(self.s.repo / '..' / 'repo')):
            self.assertEqual(wa.writer_cwd(path), jobs._writer_cwd(path))
            with mock.patch.object(common, 'STATE', str(self.s.state)):
                self.assertEqual(os.path.join(str(self.s.state), 'writers', wa.reservation_name(wa.writer_cwd(path))),
                                 jobs._writer_path(path))


class ClaimantParity(Base):
    """Same synthetic fixture, same injected process states: the real claimant admits exactly when the
    explanation finds no observed blocker. Fixtures exclude child-starting markers (the claimant deletes a
    proven-dead one, so there the explanation is deliberately cleanup-dependent unknown)."""

    def claimant(self, table):
        def pid_state(pid, created):
            if not pid:
                return DEAD
            return table.get((pid, None if created in (None, '') else created), UNKNOWN)
        spec = {'id': TARGET, 'profile': 'code', 'cwd': str(self.s.repo)}
        with mock.patch.object(common, 'JOBS', str(self.s.jobs)), \
                mock.patch.object(common, 'STATE', str(self.s.state)), \
                mock.patch.object(jobs, '_pid_state', side_effect=pid_state), \
                mock.patch.object(jobs, '_self_created', return_value=SELF_CREATED):
            try:
                return bool(jobs._claim_writer(spec))
            except jobs.Refusal:
                return False

    @unittest.skipUnless(git_fixtures.git_available(), 'git is required')
    def test_worktree_root_subfolder_and_windows_case_share_writer_admission(self):
        ws = git_fixtures.Workspace()
        self.addCleanup(ws.cleanup)
        repo = ws.repo()
        wt = ws.worktree(repo, 'feature')
        sub = os.path.join(wt, 'subfolder')
        os.makedirs(sub)
        spellings = [wt, sub]
        if common.IS_WIN:
            spellings.extend([wt.swapcase(), sub.swapcase()])
        for first in spellings:
            for second in spellings:
                with self.subTest(first=first, second=second), \
                        mock.patch.object(common, 'JOBS', str(self.s.jobs)), \
                        mock.patch.object(common, 'STATE', str(self.s.state)), \
                        mock.patch.object(jobs, '_pid_state', return_value=ALIVE), \
                        mock.patch.object(jobs, '_self_created', return_value=SELF_CREATED):
                    path = jobs._claim_writer({'id': 'first', 'profile': 'code', 'cwd': first})
                    try:
                        with self.assertRaisesRegex(jobs.Refusal, 'owned by first'):
                            jobs._claim_writer({'id': 'second', 'profile': 'code', 'cwd': second})
                        report = wa.evaluate(wa.capture_snapshot(
                            second, str(self.s.jobs), str(self.s.state),
                            observer=table_observer({(os.getpid(), SELF_CREATED): ALIVE}), target_job_id=TARGET))
                        self.assertEqual(report['decision'], wa.BLOCKED)
                    finally:
                        os.remove(path)

    def fixtures(self):
        s = self.s
        # Valid job directory names keep parity focused on lifecycle evidence.
        # Nonmatching folders have their own fail-closed admission regressions.
        owner = {'id': '20261013-000001-claude-dcba', 'cwd': s.cwd, 'pid': 50, 'created': 500}
        yield 'empty', lambda: None, {}
        yield 'reservation alive', lambda: s.reservation(owner), {(50, 500): ALIVE}
        yield 'reservation unknown', lambda: s.reservation(owner), {(50, 500): UNKNOWN}
        yield 'reservation dead, no owner job', lambda: s.reservation(owner), {(50, 500): DEAD}
        yield 'reservation dead, owner child alive', lambda: (
            s.reservation(owner), s.job('20261013-000001-claude-dcba', status={'status': 'done', 'child_pid': 60, 'child_created': 600},
                                        result={'status': 'done'})), {(50, 500): DEAD, (60, 600): ALIVE}
        yield 'reservation malformed', lambda: s.reservation('broken'), {}
        yield 'reservation other cwd', lambda: s.reservation(dict(owner, cwd='x')), {(50, 500): DEAD}
        yield 'running alive', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'running', 'pid': 5, 'pid_created': 7}), \
            {(5, 7): ALIVE}
        yield 'running unknown', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'running', 'pid': 5, 'pid_created': 7}), \
            {(5, 7): UNKNOWN}
        yield 'finished clean', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'done', 'pid': 5, 'pid_created': 7,
                                                           'child_pid': 6, 'child_created': 8},
                                              result={'status': 'done'}), {(5, 7): DEAD, (6, 8): DEAD}
        yield 'done receipt, child unknown', lambda: s.job(
            '20261013-000001-claude-abcd', status={'status': 'done', 'pid': 5, 'pid_created': 7, 'child_pid': 6, 'child_created': 8},
            result={'status': 'done'}), {(5, 7): DEAD, (6, 8): UNKNOWN}
        yield 'legacy queued, no receipt', lambda: s.job('20261013-000001-claude-abcd', version=None, status={'status': 'queued'}), {}
        yield 'legacy queued, receipt', lambda: s.job('20261013-000001-claude-abcd', version=None, status={'status': 'queued'},
                                                      result={'status': 'refused'}), {}
        yield 'legacy no status, receipt', lambda: s.job('20261013-000001-claude-abcd', version=None, result={'status': 'done'}), {}
        yield 'modern queued no attempt', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'queued'}), {}
        yield 'modern queued attempt dead', lambda: s.job(
            '20261013-000001-claude-abcd', status={'status': 'queued', 'attempt': 1, 'pid': 5, 'pid_created': 7}), {(5, 7): DEAD}
        yield 'unrelated cwd alive', lambda: s.job('20261013-000001-claude-abcd', cwd=s.other, status={'status': 'running', 'pid': 5,
                                                                             'pid_created': 7}), {(5, 7): ALIVE}
        yield 'other profile alive', lambda: s.job('20261013-000001-claude-abcd', profile='intel', status={'status': 'running', 'pid': 5,
                                                                                 'pid_created': 7}), {(5, 7): ALIVE}
        yield 'spec invalid', lambda: s.job('20261013-000001-claude-abcd', spec='broken'), {}
        yield 'status invalid', lambda: s.job('20261013-000001-claude-abcd', status='broken'), {}
        yield 'status null', lambda: s.job('20261013-000001-claude-abcd', status=None), {}
        yield 'survivor alive', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'done'}, survivor={'pid': 9, 'created': 90},
                                              result={'status': 'done'}), {(9, 90): ALIVE}
        yield 'survivor stampless alive', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'done'}, survivor={'pid': 9},
                                                        result={'status': 'done'}), {(9, None): ALIVE}
        yield 'survivor stampless dead', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'done'}, survivor={'pid': 9},
                                                       result={'status': 'done'}), {(9, None): DEAD}
        yield 'survivor malformed', lambda: s.job('20261013-000001-claude-abcd', status={'status': 'done'}, survivor={'pid': '9'},
                                                  result={'status': 'done'}), {}
        yield 'empty folder', lambda: (s.jobs / '20261013-000001-claude-abcd').mkdir(), {}
        yield 'runner.pid alive', lambda: s.job('20261013-000001-claude-abcd', version=None, status={'status': 'running'},
                                                runner_pid='77 770'), {(77, 770): ALIVE}
        yield 'runner.pid dead', lambda: s.job('20261013-000001-claude-abcd', version=None, status={'status': 'running'},
                                               runner_pid='77 770'), {(77, 770): DEAD}
        yield 'target itself alive', lambda: s.job(TARGET, status={'status': 'running', 'pid': 5,
                                                                  'pid_created': 7}), {(5, 7): ALIVE}

    def test_differential_parity(self):
        names = [name for name, _, _ in self.fixtures()]
        self.assertGreaterEqual(len(names), 25)
        for name in names:
            with self.subTest(fixture=name):
                self.s = Store(self)
                build, table = next((b, t) for n, b, t in self.fixtures() if n == name)
                build()
                report = self.explain(table)  # before the claimant, which writes a reservation when it admits
                admitted = self.claimant(table)
                self.assertEqual(report['decision'] == wa.NO_OBSERVED_BLOCKER, admitted,
                                 (name, report['decision'], self.reasons(report)))
                if not admitted:
                    self.assertIn(report['decision'], (wa.BLOCKED, wa.UNKNOWN))

    def test_proven_dead_marker_is_where_parity_deliberately_stops(self):
        self.s.job('20261013-000001-claude-abcd', status={'status': 'done'}, result={'status': 'done'},
                   pending={'engine': 'codex', 'pid': 80, 'created': 800})
        report = self.explain({(80, 800): DEAD})
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertTrue((self.s.jobs / '20261013-000001-claude-abcd' / 'child-starting.json').exists())
        self.assertTrue(self.claimant({(80, 800): DEAD}))  # the claimant deleted the marker, then admitted
        self.assertFalse((self.s.jobs / '20261013-000001-claude-abcd' / 'child-starting.json').exists())

    def test_r8_unresolved_tree_forms_match_writer_busy_and_claimant(self):
        jid = '20261013-000001-claude-abcd'
        table = {(5, 7): DEAD, (6, 8): DEAD}
        for marker in (jobs.TREE_UNRESOLVED, jobs.CHILD_STARTING):
            with self.subTest(marker=marker):
                self.s = Store(self)
                self.s.job(jid, status={'status': 'failed', 'pid': 5, 'pid_created': 7,
                                       'child_pid': 6, 'child_created': 8}, result={'status': 'failed'})
                self.s.put(self.s.jobs / jid / marker, {'engine': 'claude', 'unresolved_tree': True})
                report = self.explain(table)
                with mock.patch.object(common, 'JOBS', str(self.s.jobs)), \
                        mock.patch.object(jobs, '_pid_state', return_value=DEAD):
                    self.assertTrue(jobs._writer_job_busy(jid))
                self.assertFalse(self.claimant(table))
                self.assertEqual(report['decision'], wa.BLOCKED)
                self.assertEqual(report['jobs'][0]['engine']['engine_state'], UNKNOWN)
                reason = ' '.join(self.reasons(report))
                self.assertIn(marker, reason)
                self.assertIn('a person must run twinlane stop JOB --tree-gone', reason)

    def test_r8_tree_marker_only_folder_is_not_unpublished(self):
        jid = '20261013-000001-claude-abcd'
        self.s.job(jid, spec=None)
        self.s.put(self.s.jobs / jid / jobs.TREE_UNRESOLVED, {})
        self.assertEqual(self.explain()['decision'], wa.BLOCKED)
        self.assertEqual(self.explain()['skipped']['unpublished'], 0)

    def test_r8_tree_marker_appearance_changes_snapshot_generation(self):
        jid = '20261013-000001-claude-abcd'
        self.s.job(jid, status={'status': 'failed', 'pid': 5, 'pid_created': 7})
        observe = table_observer({(5, 7): DEAD})

        def marker_appears(pid, created):
            self.s.put(self.s.jobs / jid / jobs.TREE_UNRESOLVED, {})
            return observe(pid, created)

        report = self.explain(observer=marker_appears)
        self.assertFalse(report['snapshot']['stable'])
        self.assertIn('file:' + jid + '/' + jobs.TREE_UNRESOLVED, report['snapshot']['races'])

    def test_real_self_observation_blocks_on_a_live_owner(self):
        me = common.process_created(os.getpid())
        self.assertIsNotNone(me)
        self.s.reservation({'id': 'owner', 'cwd': self.s.cwd, 'pid': os.getpid(), 'created': me})
        report = self.explain(observer=wa.default_observer)
        self.assertEqual(report['decision'], wa.BLOCKED)
        self.assertEqual(report['reservation']['owner']['actual_created'], str(me))
        self.assertEqual(report['reservation']['owner']['observer'], 'current-process-token')


class FinishedWriterRecovery(Base):
    OLD = '20261017-000001-codex-old'

    def finished_writer(self, reserved=True, receipt=None, **extra):
        self.s.job(self.OLD, status={'status': 'running', 'pid': 50, 'pid_created': 500,
                                    'child_pid': 60, 'child_created': 600, 'attempt': 1, 'engine': 'codex'},
                   result=receipt if receipt is not None else {
                       'id': self.OLD, 'status': 'done', 'finished_at': '2026-10-17T00:00:00Z'}, **extra)
        if reserved:
            self.s.reservation({'id': self.OLD, 'cwd': self.s.cwd, 'pid': 50, 'created': 500})

    @contextmanager
    def runtime(self, child=DEAD, runner=DEAD, survivor=UNKNOWN, raw=None):
        states = {(50, 500): runner, (60, 600): child, (70, 700): survivor}
        def probe(pid, created):
            return states.get((pid, created), UNKNOWN) if pid else DEAD
        with mock.patch.object(common, 'JOBS', str(self.s.jobs)), \
                mock.patch.object(common, 'STATE', str(self.s.state)), \
                mock.patch.object(common, 'BATONS', str(self.s.root / 'batons')), \
                mock.patch.object(common, 'git', return_value=(1, '')), \
                mock.patch.object(common, 'IS_WIN', True), \
                mock.patch.object(common, '_win_boot_created', return_value=None), \
                mock.patch.object(common, '_win_observe', return_value=raw or {
                    'exists': None, 'created': None, 'reason': 'open_failed'}), \
                mock.patch.object(jobs, '_pid_state', side_effect=probe), \
                mock.patch.object(jobs, '_self_created', return_value=SELF_CREATED):
            yield

    def claim(self):
        return jobs._claim_writer({'id': TARGET, 'profile': 'code', 'cwd': str(self.s.repo)})

    def exit_receipt(self, status='done'):
        rc = 0 if status == 'done' else 1
        return {'id': self.OLD, 'status': status, 'exit_code': rc,
                'attempts': [{'n': 1, 'engine': 'codex', 'status': status, 'exit_code': rc,
                              'events_path': 'engine/events.jsonl'}]}

    def test_exited_child_with_reused_inaccessible_pid_admits_and_repairs(self):
        for reserved in (True, False):
            for status in ('done', 'failed'):
                for reason in ('access_denied', 'times_failed', 'times_raised'):
                    with self.subTest(reserved=reserved, status=status, reason=reason):
                        self.s = Store(self)
                        self.finished_writer(reserved=reserved, receipt=self.exit_receipt(status))
                        def observe(pid, created):
                            obs = table_observer({(50, 500): DEAD, (60, 600): UNKNOWN})(pid, created)
                            if pid == 60:
                                obs.update(exists=True, actual_created=None, reason=reason)
                            return obs
                        before = tree(self.s.root)
                        self.assertEqual(self.explain(observer=observe)['decision'], wa.NO_OBSERVED_BLOCKER)
                        self.assertEqual(tree(self.s.root), before)
                        with self.runtime(child=UNKNOWN, raw={'exists': True, 'created': None, 'reason': reason}):
                            self.assertEqual(jobs.job_status(self.OLD), status)
                            path = self.claim()
                        self.assertEqual(common.read_json(path)['id'], TARGET)
                        repaired = common.read_json(str(self.s.jobs / self.OLD / 'status.json'))
                        self.assertEqual((repaired['status'], repaired['pid'], repaired['child_pid']), (status, 50, 60))

    def test_timed_out_job_whose_child_pid_was_recycled_across_a_restart_releases_the_slot(self):
        """The dead-job custody bug: a timeout receipt (a synthetic exit, so no observed child exit), a dead
        runner and a status.json stuck on running, with the child's pid now held by a process this token
        cannot read. Only a recorded stamp older than the boot proves the child gone; without that proof the
        job keeps its slot."""
        timeout = {'id': self.OLD, 'status': 'timeout', 'exit_code': 124,
                   'attempts': [{'n': 1, 'engine': 'codex', 'status': 'timeout', 'exit_code': 124,
                                 'events_path': 'engine/events.jsonl'}]}
        for boot, released in ((1000, True), (600, False), (None, False)):
            with self.subTest(boot=boot):
                self.s = Store(self)
                self.finished_writer(receipt=timeout)
                with mock.patch.object(common, 'IS_WIN', True), \
                        mock.patch.object(common, '_win_process_info', return_value=(True, None)), \
                        mock.patch.object(common, '_win_boot_created', return_value=boot):
                    child = common.pid_state(60, 600)
                self.assertEqual(child, DEAD if released else UNKNOWN)
                raw = {'exists': True, 'created': None, 'reason': 'access_denied'}
                with self.runtime(child=child, raw=raw):
                    self.assertEqual(jobs._writer_job_busy(self.OLD), not released)
                    if released:
                        self.assertEqual(jobs.job_status(self.OLD), 'timeout')
                        self.assertEqual(jobs._repair_finished_status(self.OLD), 'repaired')
                        self.assertEqual(common.read_json(str(self.s.jobs / self.OLD / 'status.json'))['status'],
                                         'timeout')
                        self.assertEqual(common.read_json(self.claim())['id'], TARGET)
                    else:
                        self.assertEqual(jobs.job_status(self.OLD), 'unknown')
                        self.assertEqual(jobs._repair_finished_status(self.OLD), 'rejected')
                        with self.assertRaises(jobs.Refusal):
                            self.claim()

    def test_reused_pid_without_child_exit_evidence_keeps_blocking(self):
        good = self.exit_receipt()
        cases = [
            {'status': 'done'}, {'status': 'done', 'exit_code': None},
            {'status': 'done', 'exit_code': True},
            {'status': 'failed', 'exit_code': 127, 'error': 'launch failed'},
            dict(good, attempts=[]), dict(good, attempts='unreadable'),
            dict(good, attempts=[{'status': 'done', 'exit_code': 0}]),
            dict(good, attempts=[{'status': 'failed', 'exit_code': 127, 'error': 'bookkeeping failed'}]),
            dict(good, attempts=[{'status': 'done', 'exit_code': 1, 'events_path': 'engine/events.jsonl'}]),
            dict(good, status='stopped', notes=['kill failed']),
            dict(good, status='orphaned'), dict(good, status='interrupted'),
            dict(good, survivor={'pid': 60, 'created': 600}),
        ]
        for receipt in cases:
            with self.subTest(receipt=receipt):
                self.s = Store(self)
                self.finished_writer(receipt=receipt)
                before = tree(self.s.jobs), tree(self.s.state / 'writers')
                with self.runtime(child=UNKNOWN, raw={'exists': True, 'created': None, 'reason': 'access_denied'}):
                    self.assertIn(jobs.job_status(self.OLD), ('unknown', 'orphaned'))
                    with self.assertRaises(jobs.Refusal):
                        self.claim()
                    jobs._repair_finished_status(self.OLD)
                self.assertEqual((tree(self.s.jobs), tree(self.s.state / 'writers')), before)

    def test_reused_pid_exception_requires_dead_runner_and_only_unreadable_child_identity(self):
        cases = [
            (UNKNOWN, UNKNOWN, {'exists': True, 'created': None, 'reason': 'access_denied'}, {}),
            (ALIVE, UNKNOWN, {'exists': True, 'created': None, 'reason': 'access_denied'}, {}),
            (DEAD, ALIVE, {'exists': True, 'created': 600, 'reason': 'running'}, {}),
            (DEAD, UNKNOWN, {'exists': None, 'created': None, 'reason': 'open_failed'}, {}),
            (DEAD, UNKNOWN, {'exists': None, 'created': None, 'reason': 'exit_code_failed'}, {}),
            (DEAD, UNKNOWN, {'exists': True, 'created': 600, 'reason': 'running'}, {}),
            (DEAD, UNKNOWN, {'exists': True, 'created': None, 'reason': 'access_denied'},
             {'survivor': {'pid': 70, 'created': 700}}),
            (DEAD, UNKNOWN, {'exists': True, 'created': None, 'reason': 'access_denied'}, {'survivor': {}}),
            (DEAD, UNKNOWN, {'exists': True, 'created': None, 'reason': 'access_denied'},
             {'pending': {'engine': 'codex'}}),
        ]
        for runner, child, raw, extra in cases:
            with self.subTest(runner=runner, child=child, raw=raw, extra=extra):
                self.s = Store(self)
                self.finished_writer(receipt=self.exit_receipt(), **extra)
                before = tree(self.s.jobs), tree(self.s.state / 'writers')
                with self.runtime(child=child, runner=runner, raw=raw):
                    with self.assertRaises(jobs.Refusal):
                        self.claim()
                    jobs._repair_finished_status(self.OLD)
                self.assertEqual((tree(self.s.jobs), tree(self.s.state / 'writers')), before)

    def test_reused_pid_with_dead_survivor_marker_still_blocks(self):
        self.finished_writer(receipt=self.exit_receipt(), survivor={'pid': 70, 'created': 700})
        before = tree(self.s.jobs), tree(self.s.state / 'writers')
        with self.runtime(child=UNKNOWN, survivor=DEAD,
                          raw={'exists': True, 'created': None, 'reason': 'access_denied'}):
            with self.assertRaises(jobs.Refusal):
                self.claim()
            jobs._repair_finished_status(self.OLD)
        self.assertEqual((tree(self.s.jobs), tree(self.s.state / 'writers')), before)

    def test_reused_pid_board_status_and_watch_write_nothing(self):
        from twinlane import board, cli, watch
        self.finished_writer(receipt=self.exit_receipt())
        before = tree(self.s.root)
        with self.runtime(child=UNKNOWN, raw={'exists': True, 'created': None, 'reason': 'access_denied'}), \
                redirect_stdout(io.StringIO()), \
                mock.patch.object(common, 'write_json', side_effect=AssertionError('read path wrote')), \
                mock.patch.object(common, 'write_text', side_effect=AssertionError('read path wrote')):
            self.assertEqual(jobs.job_status(self.OLD), 'done')
            self.assertEqual(jobs.list_jobs()[0]['status'], 'done')
            self.assertEqual(cli.status([self.OLD, '--json']), 0)
            self.assertEqual(board.collect_jobs(common.now(), show_all=True, finished_n=20)['finished'][0]['status'], 'done')
            watch.pending(watch.initial_cursor())
        self.assertEqual(tree(self.s.root), before)

    def assert_exit_evidence_blocks(self, change_receipt=lambda r: None, change_status=lambda s: None):
        for reserved in (True, False):
            with self.subTest(reserved=reserved):
                self.s = Store(self)
                receipt = self.exit_receipt()
                change_receipt(receipt)
                self.finished_writer(reserved=reserved, receipt=receipt)
                path = self.s.jobs / self.OLD / 'status.json'
                st = common.read_json(str(path))
                change_status(st)
                Store.put(path, st)
                before = tree(self.s.root)
                def observe(pid, created):
                    obs = table_observer({(50, 500): DEAD, (60, 600): UNKNOWN})(pid, created)
                    if pid == 60:
                        obs.update(exists=True, actual_created=None, reason='access_denied')
                    return obs
                self.assertNotEqual(self.explain(observer=observe)['decision'], wa.NO_OBSERVED_BLOCKER)
                with self.runtime(child=UNKNOWN, raw={'exists': True, 'created': None, 'reason': 'access_denied'}):
                    self.assertTrue(jobs._writer_job_busy(self.OLD))
                    self.assertEqual(jobs._receipt_status(self.OLD, receipt), 'unknown')
                    with self.assertRaises(jobs.Refusal) as refused:
                        self.claim()
                    self.assertEqual(refused.exception.code, common.EXIT_REFUSED)
                after = tree(self.s.root)
                # The admission mutex may be allocated; no reservation or job evidence changes.
                after.pop(os.path.join('state', 'writers.lock'), None)
                if 'writers.lock' in after['state'][2]:
                    after['state'][2].remove('writers.lock')
                self.assertEqual(after, before)

    def test_reused_pid_missing_attempts_blocks_and_explains(self):
        self.assert_exit_evidence_blocks(lambda r: r.pop('attempts'))

    def test_reused_pid_empty_attempts_blocks_and_explains(self):
        self.assert_exit_evidence_blocks(lambda r: r.update(attempts=[]))

    def test_reused_pid_null_attempts_blocks_and_explains(self):
        self.assert_exit_evidence_blocks(lambda r: r.update(attempts=None))

    def test_reused_pid_attempt_mismatch_blocks_and_explains(self):
        self.assert_exit_evidence_blocks(lambda r: r['attempts'][-1].update(n=2))

    def test_reused_pid_missing_status_attempt_blocks_and_explains(self):
        self.assert_exit_evidence_blocks(change_status=lambda s: s.pop('attempt'))

    def test_reused_pid_engine_mismatch_blocks_and_explains(self):
        self.assert_exit_evidence_blocks(lambda r: r['attempts'][-1].update(engine='claude'))

    def test_claim_refuses_when_finished_recheck_rejects(self):
        for reserved in (True, False):
            for initial in (DEAD, UNKNOWN):
                for changed in ('alive', 'unknown-existence', 'matching-identity', 'survivor', 'starting', 'runner-alive'):
                    with self.subTest(reserved=reserved, initial=initial, changed=changed):
                        self.s = Store(self)
                        self.finished_writer(reserved=reserved, receipt=self.exit_receipt())
                        status = self.s.jobs / self.OLD / 'status.json'
                        result = self.s.jobs / self.OLD / 'result.json'
                        before = [(p.read_bytes(), p.stat().st_mtime_ns) for p in (status, result)]
                        reservation = self.s.state / 'writers' / wa.reservation_name(self.s.cwd)
                        old_reservation = reservation.read_bytes() if reserved else None
                        lock = common.file_lock
                        locked = False
                        writers_held = False
                        def probe(pid, created):
                            if pid == 50 and locked and changed == 'runner-alive':
                                return ALIVE
                            if not pid or pid == 50:
                                return DEAD
                            if pid == 60:
                                if changed == 'runner-alive':
                                    return initial
                                return (ALIVE if changed == 'alive' else UNKNOWN) if locked else initial
                            return UNKNOWN
                        def raw(pid, k32=None):
                            if locked and changed == 'unknown-existence':
                                return {'exists': None, 'created': None, 'reason': 'open_failed'}
                            return {'exists': True, 'created': 600 if locked and changed == 'matching-identity' else None,
                                    'reason': 'access_denied'}
                        @contextmanager
                        def rechecking_lock(path, *args, **kw):
                            nonlocal locked, writers_held
                            with lock(path, *args, **kw):
                                if os.path.basename(path) == 'writers.lock':
                                    writers_held = True
                                if os.path.basename(path) == 'status.lock':
                                    self.assertTrue(writers_held)
                                    locked = True
                                    if changed == 'survivor':
                                        Store.put(self.s.jobs / self.OLD / jobs.SURVIVOR, {'pid': 70, 'created': 700})
                                    elif changed == 'starting':
                                        Store.put(self.s.jobs / self.OLD / jobs.CHILD_STARTING, {'engine': 'codex'})
                                yield
                        with self.runtime(child=initial), \
                                mock.patch.object(jobs, '_pid_state', side_effect=probe), \
                                mock.patch.object(common, '_win_observe', side_effect=raw), \
                                mock.patch.object(common, 'file_lock', side_effect=rechecking_lock), \
                                mock.patch.object(jobs, '_repair_finished_status', wraps=jobs._repair_finished_status) as repair:
                            with self.assertRaises(jobs.Refusal) as refused:
                                self.claim()
                            self.assertEqual(refused.exception.code, common.EXIT_REFUSED)
                            repair.assert_called_once_with(self.OLD, require_dead_runner=True)
                        self.assertTrue(locked)
                        self.assertEqual([(p.read_bytes(), p.stat().st_mtime_ns) for p in (status, result)], before)
                        self.assertEqual(reservation.read_bytes() if reservation.exists() else None, old_reservation)
                        def observe(pid, created):
                            state = (ALIVE if changed == 'runner-alive' else DEAD) if pid == 50 else (
                                ALIVE if pid == 60 and changed == 'alive' else UNKNOWN)
                            obs = table_observer({(pid, created): state})(pid, created)
                            if pid == 60:
                                evidence = raw(pid)
                                obs.update(exists=evidence['exists'], actual_created=(
                                    str(evidence['created']) if evidence['created'] is not None else None))
                            return obs
                        self.assertNotEqual(self.explain(observer=observe)['decision'], wa.NO_OBSERVED_BLOCKER)

    def test_finished_repair_returns_explicit_outcomes(self):
        self.finished_writer(receipt=self.exit_receipt())
        with self.runtime():
            self.assertEqual(jobs._repair_finished_status(self.OLD), 'repaired')
            self.assertEqual(jobs._repair_finished_status(self.OLD), 'already-final')
        with self.runtime(child=ALIVE):
            self.assertEqual(jobs._repair_finished_status(self.OLD), 'rejected')

    def test_preserving_claim_with_finished_past_job_never_changes_its_tree(self):
        for status in ('done', 'running'):
            for child in (DEAD, ALIVE, UNKNOWN):
                with self.subTest(status=status, child=child):
                    self.s = Store(self)
                    self.finished_writer(reserved=False, receipt=self.exit_receipt())
                    old = self.s.jobs / self.OLD
                    st = common.read_json(str(old / 'status.json'))
                    Store.put(old / 'status.json', dict(st, status=status))
                    before = tree(old)
                    spec = {'id': TARGET, 'profile': 'code', 'cwd': str(self.s.repo),
                            'preserve_existing_metadata': True}
                    with self.runtime(child=child), jobs.preserving_metadata(TARGET), \
                            mock.patch.object(common, 'file_lock', wraps=common.file_lock) as locks:
                        if child == DEAD:
                            path = jobs._claim_writer(spec)
                            self.assertEqual(common.read_json(path)['id'], TARGET)
                        else:
                            with self.assertRaises(jobs.Refusal) as refused:
                                jobs._claim_writer(spec)
                            self.assertEqual(refused.exception.code, common.EXIT_REFUSED)
                    self.assertEqual(tree(old), before)
                    self.assertEqual([os.path.basename(c.args[0]) for c in locks.call_args_list], ['writers.lock'])

    def test_read_only_finished_repair_evaluates_evidence_without_lock_or_write(self):
        from twinlane import observations
        for status in ('done', 'running'):
            for mode in ('preserving', 'protected', 'observing'):
                for runner, child, want in ((DEAD, DEAD, 'unrepaired'),
                                            (DEAD, ALIVE, 'rejected'), (DEAD, UNKNOWN, 'rejected'),
                                            (ALIVE, DEAD, 'rejected'), (UNKNOWN, DEAD, 'rejected')):
                    with self.subTest(status=status, mode=mode, runner=runner, child=child):
                        self.s = Store(self)
                        self.finished_writer(reserved=False, receipt=self.exit_receipt())
                        old = self.s.jobs / self.OLD
                        st = common.read_json(str(old / 'status.json'))
                        Store.put(old / 'status.json', dict(st, status=status))
                        before = tree(self.s.root)
                        scope = (jobs.preserving_metadata(TARGET) if mode == 'preserving' else
                                 mock.patch.object(jobs, '_preserve_job', return_value=True) if mode == 'protected' else
                                 observations.scope())
                        with self.runtime(runner=runner, child=child), scope, \
                                mock.patch.object(common, 'file_lock', side_effect=AssertionError('read-only lock')), \
                                mock.patch.object(common, 'write_json', side_effect=AssertionError('read-only write')):
                            self.assertEqual(jobs._repair_finished_status(self.OLD, require_dead_runner=True), want)
                        self.assertEqual(tree(self.s.root), before)

    def test_wait_result_stop_do_not_report_rejected_repair_as_success(self):
        for command, args in ((jobs.cmd_wait, ['--timeout', '0', '--json']),
                              (jobs.cmd_result, ['--json']), (jobs.cmd_stop, ['--json'])):
            with self.subTest(command=command.__name__):
                self.s = Store(self)
                self.finished_writer(receipt=self.exit_receipt())
                with self.runtime(), mock.patch.object(jobs, '_repair_finished_status', return_value='rejected'), \
                        redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(command([self.OLD, *args]), common.EXIT_RUNNING)
                self.assertIn('unknown', output.getvalue())
                self.assertFalse((self.s.jobs / self.OLD / jobs.COLLECTED).exists())

    def test_wait_terminal_receipt_with_live_child_is_unfinished(self):
        for status in sorted(jobs.TERMINAL):
            with self.subTest(status=status):
                self.s = Store(self)
                self.finished_writer(receipt=self.exit_receipt(status))
                old = self.s.jobs / self.OLD
                st = common.read_json(str(old / 'status.json'))
                Store.put(old / 'status.json', dict(st, status=status))
                before = tree(old)
                with self.runtime(child=ALIVE), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(jobs._repair_finished_status(self.OLD), 'rejected')
                    self.assertEqual(jobs.cmd_wait([self.OLD, '--timeout', '0', '--json']), common.EXIT_RUNNING)
                data = json.loads(output.getvalue())
                self.assertEqual(data['jobs'], {self.OLD: 'unknown'})
                self.assertEqual(data['finished'], [])
                self.assertEqual(data['remaining'], [self.OLD])
                self.assertTrue(data['timed_out'])
                self.assertEqual(tree(old), before)

    def test_runner_finalization_requires_its_own_child_exit_evidence(self):
        self.finished_writer()
        old = self.s.jobs / self.OLD
        status = old / 'status.json'
        before = status.read_bytes(), status.stat().st_mtime_ns
        with self.runtime(child=ALIVE):
            jobs._write_runner_receipt(self.OLD, self.exit_receipt())
            self.assertEqual(jobs.job_status(self.OLD), 'orphaned')
        self.assertEqual((status.read_bytes(), status.stat().st_mtime_ns), before)

    def test_runner_finalized_receipt_does_not_bypass_writer_custody_recheck(self):
        for child in (ALIVE, UNKNOWN):
            with self.subTest(child=child):
                self.s = Store(self)
                self.finished_writer(reserved=False)
                old = self.s.jobs / self.OLD
                st = common.read_json(str(old / 'status.json'))
                Store.put(old / 'status.json', dict(st, status='done', runner_finalized=True))
                before = tree(old)
                with self.runtime(child=child):
                    with self.assertRaises(jobs.Refusal):
                        self.claim()
                    self.assertEqual(jobs._repair_finished_status(self.OLD, require_dead_runner=True), 'rejected')
                self.assertEqual(tree(old), before)

    def test_wait_any_returns_only_clean_completion_beside_live_child(self):
        for status in ('done', 'failed', 'timeout', 'stopped'):
            with self.subTest(status=status):
                self.s = Store(self)
                self.finished_writer(receipt=self.exit_receipt(status))
                old = self.s.jobs / self.OLD
                st = common.read_json(str(old / 'status.json'))
                Store.put(old / 'status.json', dict(st, status=status))
                clean = self.s.job('20261013-000002-claude-abcd', status={'status': 'done'}, result={'status': 'done'})
                with self.runtime(child=ALIVE), redirect_stdout(io.StringIO()) as output:
                    self.assertEqual(jobs.cmd_wait([self.OLD, clean, '--any', '--timeout', '0', '--json']),
                                     common.EXIT_OK)
                data = json.loads(output.getvalue())
                self.assertEqual(data['jobs'], {self.OLD: 'unknown', clean: 'done'})
                self.assertEqual(data['finished'], [clean])
                self.assertEqual(data['remaining'], [self.OLD])
                self.assertFalse(data['timed_out'])

    def test_preserving_claim_rechecks_dead_child_without_writing_old_job(self):
        for status in ('running', 'done'):
            with self.subTest(status=status):
                self.s = Store(self)
                self.finished_writer(reserved=False, receipt=self.exit_receipt())
                old = self.s.jobs / self.OLD
                st = common.read_json(str(old / 'status.json'))
                Store.put(old / 'status.json', dict(st, status=status))
                before = tree(old)
                child_checks = []
                def probe(pid, created):
                    if pid == 60:
                        child_checks.append((pid, created))
                        return DEAD if len(child_checks) == 1 else ALIVE
                    return DEAD if not pid or pid == 50 else UNKNOWN
                spec = {'id': TARGET, 'profile': 'code', 'cwd': str(self.s.repo),
                        'preserve_existing_metadata': True}
                with self.runtime(), jobs.preserving_metadata(TARGET), \
                        mock.patch.object(jobs, '_pid_state', side_effect=probe), \
                        mock.patch.object(jobs, '_repair_finished_status', wraps=jobs._repair_finished_status) as repair:
                    with self.assertRaises(jobs.Refusal) as refused:
                        jobs._claim_writer(spec)
                    self.assertEqual(refused.exception.code, common.EXIT_REFUSED)
                    repair.assert_called_once_with(self.OLD, require_dead_runner=True)
                self.assertGreaterEqual(len(child_checks), 2)
                self.assertEqual(tree(old), before)
                self.assertFalse((self.s.state / 'writers' / wa.reservation_name(self.s.cwd)).exists())

    def test_reused_pid_wait_result_and_stop_repair_finished_status(self):
        for command, args in ((jobs.cmd_wait, ['--timeout', '0', '--json']),
                              (jobs.cmd_result, ['--json']), (jobs.cmd_stop, ['--json'])):
            with self.subTest(command=command.__name__):
                self.s = Store(self)
                self.finished_writer(receipt=self.exit_receipt())
                with self.runtime(child=UNKNOWN, raw={'exists': True, 'created': None, 'reason': 'access_denied'}), \
                        redirect_stdout(io.StringIO()):
                    self.assertEqual(command([self.OLD, *args]), common.EXIT_OK)
                self.assertEqual(common.read_json(str(self.s.jobs / self.OLD / 'status.json'))['status'], 'done')

    def test_reused_pid_repair_rechecks_identity_under_status_lock(self):
        self.finished_writer(receipt=self.exit_receipt())
        before = tree(self.s.jobs)
        unreadable = {'exists': True, 'created': None, 'reason': 'access_denied'}
        unknown = {'exists': None, 'created': None, 'reason': 'open_failed'}
        with self.runtime(child=UNKNOWN), mock.patch.object(common, '_win_observe', side_effect=[unreadable, unknown]):
            jobs._repair_finished_status(self.OLD)
        # Acquiring status.lock is allowed, but the status and receipt must remain intact.
        after = tree(self.s.jobs)
        after.pop(os.path.join(self.OLD, 'status.lock'), None)
        after[self.OLD][2].remove('status.lock')
        self.assertEqual(after, before)

    def test_reused_pid_protected_or_observing_repair_never_writes(self):
        from twinlane import observations
        self.finished_writer(receipt=self.exit_receipt())
        before = tree(self.s.root)
        with self.runtime(child=UNKNOWN, raw={'exists': True, 'created': None, 'reason': 'access_denied'}):
            with mock.patch.object(jobs, '_preserve_job', return_value=True):
                jobs._repair_finished_status(self.OLD)
            with observations.scope():
                jobs._repair_finished_status(self.OLD)
        self.assertEqual(tree(self.s.root), before)

    def test_terminal_receipt_dead_runner_dead_child_admits_and_repairs_under_writer_lock(self):
        for reserved in (True, False):
            with self.subTest(reserved=reserved):
                self.s = Store(self)
                self.finished_writer(reserved=reserved)
                write = common.write_json
                lock = common.file_lock
                held = set()
                @contextmanager
                def tracked_lock(path, *args, **kwargs):
                    with lock(path, *args, **kwargs):
                        held.add(os.path.basename(path))
                        try:
                            yield
                        finally:
                            held.remove(os.path.basename(path))
                def checked_write(path, value, **kwargs):
                    if path == str(self.s.jobs / self.OLD / 'status.json'):
                        self.assertTrue({'writers.lock', 'status.lock'} <= held)
                    return write(path, value, **kwargs)
                with self.runtime(), mock.patch.object(common, 'file_lock', side_effect=tracked_lock), \
                        mock.patch.object(common, 'write_json', side_effect=checked_write):
                    path = self.claim()
                self.assertEqual(common.read_json(path)['id'], TARGET)
                repaired = common.read_json(str(self.s.jobs / self.OLD / 'status.json'))
                self.assertEqual((repaired['status'], repaired['pid'], repaired['child_pid']), ('done', 50, 60))

    def test_dead_pid_with_done_receipt_reads_stale_not_unknown(self):
        # 0.2 regression: status.json stuck at "running", result.json "done", runner and child gone.
        # The job must read as finished (its claim is stale), never as an unknown live writer.
        reused = {'exists': True, 'created': None, 'reason': 'access_denied'}
        cases = (('child gone', None, DEAD, None, 'done', False),
                 ('child pid reused', self.exit_receipt(), UNKNOWN, reused, 'done', False),
                 ('child existence unknown', None, UNKNOWN, None, 'unknown', True))
        for name, receipt, child, raw, want_status, want_busy in cases:
            with self.subTest(name):
                self.s = Store(self)
                self.finished_writer(receipt=receipt)
                with self.runtime(child=child, raw=raw):
                    got = jobs._receipt_status(self.OLD, common.read_json(
                        str(self.s.jobs / self.OLD / 'result.json')))
                    self.assertEqual(got, want_status)
                    self.assertIs(jobs._writer_job_busy(self.OLD), want_busy)

    def test_live_or_unknown_runner_or_child_still_refuses_without_repair(self):
        self.finished_writer()
        before = tree(self.s.jobs)
        for runner, child, status in ((DEAD, UNKNOWN, 'unknown'), (DEAD, ALIVE, 'orphaned'),
                                      (UNKNOWN, DEAD, 'unknown'), (ALIVE, DEAD, 'done')):
            with self.subTest(runner=runner, child=child), self.runtime(child=child, runner=runner):
                self.assertEqual(jobs.job_status(self.OLD), status)
                with self.assertRaises(jobs.Refusal):
                    self.claim()
                jobs._repair_finished_status(self.OLD)
                self.assertEqual(tree(self.s.jobs), before)

    def test_survivor_present_still_refuses_without_repair(self):
        self.finished_writer(survivor={'pid': 70, 'created': 700})
        before = tree(self.s.jobs)
        for state in (ALIVE, UNKNOWN):
            with self.subTest(survivor=state), self.runtime(survivor=state):
                with self.assertRaises(jobs.Refusal):
                    self.claim()
                jobs._repair_finished_status(self.OLD)
                self.assertEqual(tree(self.s.jobs), before)

    def test_board_status_watch_and_resolver_do_not_write(self):
        from twinlane import board, cli, watch
        self.finished_writer()
        before = tree(self.s.root)
        with self.runtime(), redirect_stdout(io.StringIO()), \
                mock.patch.object(common, 'write_json', side_effect=AssertionError('read path wrote')), \
                mock.patch.object(common, 'write_text', side_effect=AssertionError('read path wrote')):
            self.assertEqual(jobs.job_status(self.OLD), 'done')
            self.assertEqual(jobs.list_jobs()[0]['status'], 'done')
            self.assertEqual(cli.status([self.OLD, '--json']), 0)
            rows = board.collect_jobs(common.now(), show_all=True, finished_n=20)
            self.assertEqual(rows['finished'][0]['status'], 'done')
            watch.pending(watch.initial_cursor())
        self.assertEqual(tree(self.s.root), before)

    def test_wait_result_and_stop_repair_finished_status(self):
        for command, args in ((jobs.cmd_wait, ['--timeout', '0', '--json']),
                              (jobs.cmd_result, ['--json']), (jobs.cmd_stop, ['--json'])):
            with self.subTest(command=command.__name__):
                self.s = Store(self)
                self.finished_writer()
                with self.runtime(), redirect_stdout(io.StringIO()):
                    self.assertEqual(command([self.OLD, *args]), common.EXIT_OK)
                self.assertEqual(common.read_json(str(self.s.jobs / self.OLD / 'status.json'))['status'], 'done')

    def test_status_does_not_reclaim_a_dead_launch_marker(self):
        self.finished_writer(pending={'engine': 'codex', 'pid': 60, 'created': 600})
        before = tree(self.s.root)
        with self.runtime():
            self.assertEqual(jobs.job_status(self.OLD), 'done')
        self.assertEqual(tree(self.s.root), before)

    def test_repair_rechecks_process_evidence_under_status_lock(self):
        self.finished_writer()
        path = self.s.jobs / self.OLD / 'status.json'
        before = path.read_bytes(), path.stat().st_mtime_ns
        with self.runtime(), mock.patch.object(jobs, '_pid_state', side_effect=[DEAD, DEAD, DEAD, DEAD, DEAD, UNKNOWN]):
            jobs._repair_finished_status(self.OLD)
        self.assertEqual((path.read_bytes(), path.stat().st_mtime_ns), before)

    def test_protected_or_observing_repair_never_writes(self):
        from twinlane import observations
        self.finished_writer()
        before = tree(self.s.root)
        with self.runtime(), mock.patch.object(jobs, '_preserve_job', return_value=True):
            jobs._repair_finished_status(self.OLD)
        with self.runtime(), observations.scope():
            jobs._repair_finished_status(self.OLD)
        self.assertEqual(tree(self.s.root), before)

    def test_empty_status_still_checks_runner_pid_file(self):
        self.finished_writer()
        Store.put(self.s.jobs / self.OLD / 'status.json', {})
        Store.put(self.s.jobs / self.OLD / 'runner.pid', '50 500')
        before = tree(self.s.root)
        with self.runtime(runner=UNKNOWN):
            self.assertEqual(jobs.job_status(self.OLD), 'unknown')
            jobs._repair_finished_status(self.OLD)
        self.assertEqual(tree(self.s.root), before)


class ObservationMustEchoItsIdentity(Base):
    """An observation counts only when it names exactly the requested pid and stamp; otherwise UNKNOWN."""

    def respond(self, response):
        def observe(pid, created):
            return response(pid, created)
        return observe

    def test_malformed_or_mismatched_responses_are_unknown_through_capture(self):
        self.s.reservation({'id': 'owner', 'cwd': self.s.cwd, 'pid': 1, 'created': 500})
        cases = {
            'none': (lambda p, c: None, 'observation_not_an_object'),
            'list': (lambda p, c: [DEAD], 'observation_not_an_object'),
            'string': (lambda p, c: DEAD, 'observation_not_an_object'),
            'invalid state': (lambda p, c: {'state': 'gone', 'pid': p, 'expected_created': str(c)},
                              'observation_state_invalid'),
            'no state': (lambda p, c: {'pid': p, 'expected_created': str(c)}, 'observation_state_invalid'),
            'missing pid': (lambda p, c: {'state': DEAD, 'expected_created': str(c)}, 'observation_identity_missing'),
            'missing stamp': (lambda p, c: {'state': DEAD, 'pid': p}, 'observation_identity_missing'),
            'state only': (lambda p, c: {'state': DEAD}, 'observation_identity_missing'),
            'wrong pid': (lambda p, c: {'state': DEAD, 'pid': p + 1, 'expected_created': str(c)},
                          'observation_identity_mismatch'),
            'bool pid': (lambda p, c: {'state': DEAD, 'pid': True, 'expected_created': str(c)},
                         'observation_identity_mismatch'),
            'string pid': (lambda p, c: {'state': DEAD, 'pid': str(p), 'expected_created': str(c)},
                           'observation_identity_mismatch'),
            'float pid': (lambda p, c: {'state': DEAD, 'pid': float(p), 'expected_created': str(c)},
                          'observation_identity_mismatch'),
            'wrong stamp': (lambda p, c: {'state': DEAD, 'pid': p, 'expected_created': str(c + 1)},
                            'observation_identity_mismatch'),
            'int stamp': (lambda p, c: {'state': DEAD, 'pid': p, 'expected_created': c},
                          'observation_identity_mismatch'),
            'padded stamp': (lambda p, c: {'state': DEAD, 'pid': p, 'expected_created': '0' + str(c)},
                             'observation_identity_mismatch'),
            'none stamp': (lambda p, c: {'state': DEAD, 'pid': p, 'expected_created': None},
                           'observation_identity_mismatch'),
        }
        for name, (response, reason) in cases.items():
            with self.subTest(name):
                report = self.explain(observer=self.respond(response))
                self.assertEqual(report['decision'], wa.UNKNOWN)
                self.assertEqual(report['reservation']['owner']['reason'], reason)
                self.assertEqual(report['reservation']['reasons'][0]['reason'], 'reservation_owner_unknown')
        echo = self.explain(observer=self.respond(lambda p, c: {'state': DEAD, 'pid': p, 'expected_created': str(c)}))
        self.assertEqual(echo['decision'], wa.NO_OBSERVED_BLOCKER)  # the control: an exact echo is trusted

    def test_stampless_identity_needs_a_none_echo(self):
        self.s.job('a', status={'status': 'done', 'child_pid': 61}, result={'status': 'done'})
        for expected, decision in (('610', wa.UNKNOWN), ('', wa.UNKNOWN), (None, wa.NO_OBSERVED_BLOCKER)):
            with self.subTest(expected=expected):
                report = self.explain(observer=self.respond(
                    lambda p, c: {'state': DEAD, 'pid': p, 'expected_created': expected}))
                self.assertEqual(report['decision'], decision)

    def test_hand_built_snapshot_with_a_mismatched_observation(self):
        self.s.reservation({'id': 'owner', 'cwd': self.s.cwd, 'pid': 50, 'created': 500})
        snap = wa.capture_snapshot(str(self.s.repo), str(self.s.jobs), str(self.s.state),
                                   observer=table_observer({(50, 500): DEAD}))
        self.assertEqual(wa.evaluate(snap)['decision'], wa.NO_OBSERVED_BLOCKER)
        for bad in ({'state': DEAD, 'pid': 51, 'expected_created': '500'}, {'state': DEAD}, 'dead', None):
            with self.subTest(bad=bad):
                forged = copy.deepcopy(snap)
                forged['observations'][wa.identity_key(50, 500)] = bad
                self.assertEqual(wa.evaluate(forged)['decision'], wa.UNKNOWN)
        forged = copy.deepcopy(snap)
        del forged['observations'][wa.identity_key(50, 500)]
        report = wa.evaluate(forged)
        self.assertEqual((report['decision'], report['reservation']['owner']['reason']), (wa.UNKNOWN, 'not_observed'))
        forged['observations'] = ['not', 'a', 'mapping']
        self.assertEqual(wa.evaluate(forged)['decision'], wa.UNKNOWN)

    def test_observer_strings_are_allowlisted(self):
        self.s.reservation({'id': 'owner', 'cwd': self.s.cwd, 'pid': 50, 'created': 500})
        report = self.explain(observer=lambda p, c: {
            'state': ALIVE, 'pid': p, 'expected_created': str(c), 'reason': 'SECRET\nPAYLOAD',
            'actual_created': 'SECRET-500', 'observer': 'token=SECRET', 'platform': 'win32',
            'stages': [{'stage': 'open', 'ok': True, 'last_error': 'SECRET TEXT', 'extra': 'SECRET'}]})
        text = json.dumps(report)
        self.assertNotIn('SECRET', text)
        owner = report['reservation']['owner']
        self.assertEqual((owner['reason'], owner['actual_created'], owner['observer'], owner['platform']),
                         (None, None, None, 'win32'))
        self.assertEqual(owner['stages'], [{'stage': 'open', 'ok': True, 'last_error': None}])


class UnsafeNamesNeverPrinted(Base):
    EVIL = ('EVILNAME\nforged line', 'EVILNAME\x1b[31mred', 'EVILNAME\rcarriage', 'EVILNAME\x07bell')

    def test_fabricated_control_character_names_are_placeholders(self):
        # Windows cannot create these folder names, so the snapshot is fabricated from a captured one.
        self.s.job('a', status={'status': 'running', 'pid': 5, 'pid_created': 7})
        self.s.job('b', spec='broken')
        snap = wa.capture_snapshot(str(self.s.repo), str(self.s.jobs), str(self.s.state),
                                   observer=table_observer({(5, 7): ALIVE}), target_job_id=None)
        snap['entries'][self.EVIL[0]] = snap['entries'].pop('a')
        snap['entries'][self.EVIL[1]] = snap['entries'].pop('b')
        snap['entries'][self.EVIL[2]] = {'is_dir': None, 'error': 'OSError', 'files': {}, 'canonical_cwd': None,
                                          'cwd_problem': None}
        snap['races'] = [{'kind': 'entry', 'entry': self.EVIL[3]},
                         {'kind': 'file', 'entry': self.EVIL[0], 'file': 'status.json'},
                         {'kind': 'file', 'entry': 'a', 'file': 'EVILNAME\n../x'},
                         'EVILNAME raw label\n', {'kind': 'EVILNAME'}]
        snap['stable'] = False
        snap['target_job_id'] = 'EVILNAME\ttarget'
        report = wa.evaluate(snap)
        text, human = json.dumps(report), wa._human(report)
        self.assertNotIn('EVILNAME', text)
        self.assertNotIn('EVILNAME', human)
        self.assertTrue(all(line.isprintable() for line in human.split('\n')))
        self.assertEqual([j['id'] for j in report['jobs']], [wa.display_id(self.EVIL[0])])
        self.assertTrue(report['jobs'][0]['id'].startswith('unsafe-'))
        self.assertEqual(report['jobs'][0]['effect'], wa.BLOCKED)  # the raw name still keyed its evidence
        self.assertEqual(sorted(u['id'] for u in report['unclassified']),
                         sorted(wa.display_id(n) for n in self.EVIL[1:3]))
        self.assertEqual(report['snapshot']['races'][:3], [f'entry:{wa.display_id(self.EVIL[3])}',
                                                           f'file:{wa.display_id(self.EVIL[0])}/status.json',
                                                           'file:a/unrecognized'])
        self.assertEqual(report['snapshot']['races'][3:], ['unrecognized', 'unrecognized'])
        self.assertEqual(report['target_job_id'], wa.display_id('EVILNAME\ttarget'))
        self.assertEqual(wa.display_id(self.EVIL[0]), wa.display_id(self.EVIL[0]))  # deterministic
        self.assertNotEqual(wa.display_id(self.EVIL[0]), wa.display_id(self.EVIL[1]))

    def test_real_folder_with_a_non_plain_name(self):
        name = 'job with space é'
        self.s.job(name, status={'status': 'running', 'pid': 5, 'pid_created': 7})
        report = self.explain({(5, 7): ALIVE})
        self.assertEqual(report['jobs'][0]['id'], wa.display_id(name))
        self.assertNotIn('with space', json.dumps(report) + wa._human(report))


class OnlyConsultedIdentitiesAreObserved(Base):
    def test_exact_call_list(self):
        s = self.s
        s.reservation({'id': 'owner', 'cwd': s.cwd, 'pid': 50, 'created': 500})
        s.job('owner', cwd=s.other, status={'status': 'done', 'pid': 61, 'pid_created': 610, 'child_pid': 60,
                                             'child_created': 600}, runner_pid='62 620')
        s.job(TARGET, status={'status': 'running', 'pid': 70, 'pid_created': 700, 'child_pid': 71,
                              'child_created': 710})
        s.job('rel', status={'status': 'running', 'pid': 80, 'pid_created': 800, 'child_pid': 81},
              survivor={'pid': 82, 'created': 820}, pending={'engine': 'codex', 'pid': 83, 'created': 830})
        s.job('far', cwd=s.other, status={'status': 'running', 'pid': 90, 'pid_created': 900, 'child_pid': 94},
              survivor={'pid': 95, 'created': 950})
        s.job('intel', profile='intel', status={'status': 'running', 'pid': 91, 'pid_created': 910})
        (s.jobs / 'empty').mkdir()
        s.job('broken', spec='broken', runner_pid='92 920', status={'status': 'running', 'pid': 96})
        s.job('mal', status={'status': 'running', 'pid': '93', 'pid_created': 930, 'child_pid': True},
              pending={'engine': 'codex', 'pid': 97})
        calls = []
        report = self.explain(observer=table_observer({(50, 500): DEAD, (60, 600): DEAD}, calls))
        self.assertEqual(calls, [(50, 500), (60, 600), (80, 800), (81, None), (83, 830), (82, 820)])
        self.assertEqual(report['decision'], wa.BLOCKED)  # 'broken' is unreadable: no probe, still a blocker
        mal = next(j for j in report['jobs'] if j['id'] == 'mal')
        self.assertEqual(mal['effect'], wa.UNKNOWN)  # malformed identities stay uncertain without a probe

    def test_target_owner_engine_evidence_is_still_observed(self):
        self.s.reservation({'id': TARGET, 'cwd': self.s.cwd, 'pid': 50, 'created': 500})
        self.s.job(TARGET, status={'status': 'running', 'pid': 70, 'pid_created': 700, 'child_pid': 71,
                                   'child_created': 710})
        calls = []
        report = self.explain(observer=table_observer({(50, 500): DEAD, (71, 710): ALIVE}, calls))
        self.assertEqual(calls, [(50, 500), (71, 710)])  # the reservation check reads the owner job by id
        self.assertEqual(report['decision'], wa.BLOCKED)
        self.assertIn('reservation_owner_job_engine_alive', self.reasons(report))

    def test_invalid_reservation_probes_nothing(self):
        for owner in ({'id': 'owner', 'cwd': 'elsewhere', 'pid': 50, 'created': 500},
                      {'id': 'owner', 'cwd': self.s.cwd, 'pid': 50}, 'broken'):
            with self.subTest(owner=owner):
                self.s.reservation(owner)
                self.s.job('owner', cwd=self.s.other, status={'status': 'done', 'child_pid': 60, 'child_created': 600})
                calls = []
                self.explain(observer=table_observer({}, calls))
                self.assertEqual(calls, [])


class OverlongNumbers(Base):
    """A 5000-digit pid or stamp never reaches int(): relevant records are uncertain, unrelated ones ignored."""
    HUGE = '9' * 5000

    def test_relevant_runner_pid_with_huge_pid_or_stamp_is_unknown(self):
        for text in (self.HUGE, f'5 {self.HUGE}', f'{"1" * 11} 7', f'5 {"1" * 21}'):
            with self.subTest(text=text[:24]):
                self.s.job('a', status={'status': 'running'}, runner_pid=text)
                calls = []
                report = self.explain(observer=table_observer({}, calls))
                self.assertEqual(report['decision'], wa.UNKNOWN)
                self.assertIn('runner_pid_malformed', self.reasons(report))
                self.assertEqual(calls, [])
                self.assertNotIn(self.HUGE, json.dumps(report))

    def test_bounded_but_out_of_range_tokens_are_unknown_not_probed(self):
        self.s.job('a', status={'status': 'running'}, runner_pid=f'{2 ** 32} 7')
        calls = []
        report = self.explain(observer=table_observer({}, calls))
        self.assertEqual((report['decision'], calls), (wa.UNKNOWN, []))
        self.assertEqual(report['jobs'][0]['runner']['reason'], 'invalid_identity')

    def test_unrelated_records_with_huge_numbers_neither_probe_nor_block(self):
        self.s.job('far', cwd=self.s.other, status={'status': 'running'}, runner_pid=f'{self.HUGE} {self.HUGE}')
        self.s.job('far2', cwd=self.s.other, status=f'{{"status": "running", "pid": {self.HUGE}}}')
        self.s.job('intel', profile='intel', status={'status': 'running'}, runner_pid=self.HUGE)
        calls = []
        report = self.explain(observer=table_observer({}, calls))
        self.assertEqual((report['decision'], calls), (wa.NO_OBSERVED_BLOCKER, []))

    def test_relevant_status_with_a_huge_integer_literal_blocks_like_the_claimant(self):
        self.s.job('a', status=f'{{"status": "running", "pid": {self.HUGE}}}')
        calls = []
        report = self.explain(observer=table_observer({}, calls))
        # Decoder integer limits vary by Python version/configuration. Rejected JSON blocks;
        # a successfully parsed but out-of-range identity is UNKNOWN. Neither can admit or probe.
        self.assertIn(report['decision'], (wa.BLOCKED, wa.UNKNOWN))
        self.assertFalse(report['grants_admission'])
        self.assertEqual(calls, [])


class DeeplyNestedJson(Base):
    DEEP = '[' * 10000 + '0' + ']' * 10000

    def decoder_depth_failure(self):
        # Decoder depth behavior varies across supported Python versions. Inject this API failure
        # for the deeply nested document only; all other fixture JSON still uses the real decoder.
        real = json.loads

        def decode(text, *args, **kwargs):
            if text == self.DEEP:
                raise RecursionError('synthetic decoder depth limit')
            return real(text, *args, **kwargs)

        return mock.patch.object(wa.json, 'loads', side_effect=decode)

    def test_relevant_nested_status_is_unknown_without_a_probe(self):
        self.s.job('deep', status=self.DEEP)
        calls = []
        with self.decoder_depth_failure():
            report = self.explain(observer=table_observer({}, calls))
        self.assertEqual(report['decision'], wa.UNKNOWN)
        self.assertIn('status_unreadable', self.reasons(report))
        self.assertEqual(calls, [])

    def test_unrelated_nested_result_does_not_abort_or_probe(self):
        self.s.job('other', cwd=self.s.other, result=self.DEEP)
        calls = []
        with self.decoder_depth_failure():
            report = self.explain(observer=table_observer({}, calls))
        self.assertEqual(report['decision'], wa.NO_OBSERVED_BLOCKER)
        self.assertFalse(report['grants_admission'])
        self.assertEqual(calls, [])


if __name__ == '__main__':
    unittest.main()
