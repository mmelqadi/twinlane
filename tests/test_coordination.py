"""Disconnected task admission tests; no engines, live jobs or account access."""
import contextlib
import copy
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
from twinlane import baton, common, coordination, jobs  # noqa: E402

LINEAGE_ENV = ('TWINLANE_ENGINE_JOB',)

# A version-1 registry and its two jobs exactly as the predecessor tool writes them (its own code wrote
# these bytes: `run --task-id task-live-1` twice, then one host review receipt). Live registries in this
# format must replay unchanged.
V1_TASK = 'task-live-1'
V1_REGISTRY_NAME = 'c2ad1369a2ac40c971aeafc5ea81aab9f1bdee65ee2a5b2b07d16a35274aba32.json'
V1_BRIEF = b'Review the parser change and report findings.\n'
V1_REGISTRY = '''{
  "version": 1,
  "task": "task-live-1",
  "coordinator": "lead",
  "owner": "worker-a",
  "original_owner": "worker-a",
  "epoch": 0,
  "revision": 0,
  "dispatches": {
    "impl-1": {
      "job": "20261011-115400-claude-3f2a",
      "fingerprint": "89e7e96997791ec54d5d6bd139d896648d6d0eedd4d2c22b1ed2d422d3dcea54",
      "actor": "lead",
      "recipient": "worker-a",
      "lineage": [
        "lead",
        "worker-a"
      ],
      "epoch": 0,
      "revision": 0,
      "ready": true,
      "launch_admitted": true
    },
    "review-1": {
      "job": "20261011-115500-claude-9c01",
      "fingerprint": "a5cadbb82a762fa0ccafb32d98536bce52b3361b3c8a934b43e582377648591b",
      "actor": "worker-a",
      "recipient": "reviewer-r",
      "lineage": [
        "lead",
        "worker-a",
        "reviewer-r"
      ],
      "epoch": 0,
      "revision": 0,
      "ready": true,
      "launch_admitted": true
    }
  },
  "receipts": {
    "finding-1": {
      "kind": "review",
      "actor": "reviewer-r",
      "recipient": "worker-a",
      "predecessor": "review-1",
      "evidence": "host review receipt",
      "finding": "F1: unchecked empty input",
      "accepted_by": null,
      "epoch": 0,
      "used_by": null
    }
  }
}'''
V1_JOBS = {
    '20261011-115400-claude-3f2a': '''{
  "engine": "claude",
  "kind": "intel",
  "profile": "intel",
  "cwd": "/fixture/repo",
  "brief_text": "Review the parser change and report findings.\\n",
  "primary": {
    "engine": "claude",
    "model": "fixture"
  },
  "resume": null,
  "failover": true,
  "notes": [],
  "coordination": {
    "task": "task-live-1",
    "dispatch": "impl-1",
    "actor": "lead",
    "recipient": "worker-a",
    "parent": null,
    "receipt": null
  },
  "caller_intent": {
    "kind": "intel",
    "engine": "auto",
    "model": null,
    "effort": null,
    "tier": null,
    "timeout": null,
    "max_turns": null,
    "resume": null,
    "continue_job": null,
    "write": false,
    "urgent": false,
    "failover": null,
    "baton": null,
    "cwd": null,
    "brief_text": "Review the parser change and report findings.\\n",
    "coordination": {
      "task": "task-live-1",
      "dispatch": "impl-1",
      "actor": "lead",
      "recipient": "worker-a",
      "parent": null,
      "receipt": null
    }
  },
  "id": "20261011-115400-claude-3f2a",
  "created_at": "2026-10-11T11:54:00Z",
  "writer_reservation_version": 1
}''',
    '20261011-115500-claude-9c01': '''{
  "engine": "claude",
  "kind": "intel",
  "profile": "intel",
  "cwd": "/fixture/repo",
  "brief_text": "Review the parser change and report findings.\\n",
  "primary": {
    "engine": "claude",
    "model": "fixture"
  },
  "resume": null,
  "failover": true,
  "notes": [],
  "coordination": {
    "task": "task-live-1",
    "dispatch": "review-1",
    "actor": "worker-a",
    "recipient": "reviewer-r",
    "parent": "impl-1",
    "receipt": null
  },
  "caller_intent": {
    "kind": "intel",
    "engine": "auto",
    "model": "fixture-model",
    "effort": null,
    "tier": null,
    "timeout": 900,
    "max_turns": null,
    "resume": null,
    "continue_job": null,
    "write": false,
    "urgent": false,
    "failover": null,
    "baton": null,
    "cwd": null,
    "brief_text": "Review the parser change and report findings.\\n",
    "coordination": {
      "task": "task-live-1",
      "dispatch": "review-1",
      "actor": "worker-a",
      "recipient": "reviewer-r",
      "parent": "impl-1",
      "receipt": null
    }
  },
  "id": "20261011-115500-claude-9c01",
  "created_at": "2026-10-11T11:54:00Z",
  "writer_reservation_version": 1
}''',
}
# The predecessor's fingerprint of a plan without caller_intent (direct create_job), for the key set.
V1_PLAN_FINGERPRINT = '0d1569269341b08fbc0f6d93b7355c90b7ebcaca5a1716914dac6552574fa0bc'


class CoordinationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='twinlane-coordination-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        for name in ('STATE', 'JOBS', 'BATONS'):
            path = self.root / name.lower()
            path.mkdir()
            patch = mock.patch.object(common, name, str(path))
            patch.start(); self.addCleanup(patch.stop)
        patch = mock.patch.dict(os.environ)
        patch.start(); self.addCleanup(patch.stop)
        for name in LINEAGE_ENV:
            os.environ.pop(name, None)
        self.base = dict(engine='claude', kind='code', profile='code', cwd=str(self.root),
                         brief_text='owned fixture', primary={'engine': 'claude', 'model': 'fixture'},
                         resume=None, failover=False, notes=[])
        self.submissions = []
        self.brief = self.root / 'fixture.md'
        self.brief.write_text(self.base['brief_text'])

    def plan(self, request='first', actor='A', recipient='B', parent=None, receipt=None):
        plan = copy.deepcopy(self.base)
        plan['coordination'] = dict(task='task-X', dispatch=request, actor=actor,
                                    recipient=recipient, parent=parent, receipt=receipt)
        return plan

    def run_cli(self, args):
        def submit(jid, plan, as_json):
            self.submissions.append(jid)
            common.write_json(jobs._p(jid, 'result.json'), {'id': jid, 'status': 'done', 'exit_code': 0,
                              'notes': ['terminal synthetic submission; no real worker started']})
            return common.EXIT_OK
        def route(args, policy):
            if self.base is None:
                raise AssertionError('this run must not route')
            return copy.deepcopy(self.base)
        with mock.patch.object(common, 'load_policy', return_value={}), \
                mock.patch.object(jobs, 'plan_run', side_effect=route), \
                mock.patch.object(jobs, '_start_background', side_effect=submit), \
                contextlib.redirect_stdout(io.StringIO()) as output, contextlib.redirect_stderr(io.StringIO()):
            result = jobs.cmd_run(args)
        return result, output.getvalue()

    def cli(self, request='first', actor='A', recipient='B', parent=None, receipt=None, labelled=True):
        self.brief.write_text(self.base['brief_text'])
        args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json']
        if labelled:
            args += ['--task-id', 'task-X', '--dispatch-id', request,
                     '--actor', actor, '--recipient', recipient]
            if parent: args += ['--parent-dispatch', parent]
            if receipt: args += ['--transition-receipt', receipt]
        return self.run_cli(args)

    def ids(self):
        return {p.name for p in Path(common.JOBS).iterdir() if p.is_dir()}

    # ------------------------------------------------------------ ported from the predecessor (34)

    def test_cli_refuses_A_B_A_before_another_job_exists(self):
        self.assertEqual(self.cli()[0], 0)
        before = self.ids()
        rc, text = self.cli('back', 'B', 'A', 'first')
        self.assertEqual(rc, 3); self.assertIn('ancestor', text)
        self.assertEqual(self.ids(), before)

    def test_cli_refuses_any_ancestor_after_longer_chain(self):
        self.cli(); self.cli('child', 'B', 'C', 'first')
        before = self.ids()
        self.assertEqual(self.cli('cycle', 'C', 'A', 'child')[0], 3)
        self.assertEqual(self.ids(), before)

    def test_cli_duplicate_returns_same_job_without_second_runner_submission(self):
        self.assertEqual(self.cli()[0], 0)
        rc, text = self.cli()
        self.assertEqual(rc, 0); self.assertIn('replayed', text)
        self.assertEqual(len(self.ids()), 1); self.assertEqual(len(self.submissions), 1)

    def test_direct_create_duplicate_payload_conflict_refuses(self):
        jid = jobs.create_job(self.plan())
        self.assertEqual(jobs.create_job(self.plan()), jid)
        changed = self.plan(); changed['brief_text'] = 'different deliverable'
        with self.assertRaises(jobs.Refusal): jobs.create_job(changed)
        self.assertEqual(len(self.ids()), 1)

    def test_cli_payload_conflict_refuses_second_runner(self):
        self.cli()
        self.base['brief_text'] = 'changed executable request'
        self.assertEqual(self.cli()[0], 3)
        self.assertEqual(len(self.ids()), 1); self.assertEqual(len(self.submissions), 1)

    def test_inline_cli_replay_does_not_execute_existing_dispatch_again(self):
        args = ['--brief', str(self.brief), '--cd', str(self.root), '--json', '--task-id', 'task-X',
                '--dispatch-id', 'first', '--actor', 'A', '--recipient', 'B']
        def finished(jid):
            value = {'id': jid, 'status': 'done', 'exit_code': 0}
            common.write_json(jobs._p(jid, 'result.json'), value)
            return value
        with mock.patch.object(common, 'load_policy', return_value={}), \
                mock.patch.object(jobs, 'plan_run', side_effect=lambda a, p: copy.deepcopy(self.base)), \
                mock.patch.object(jobs, 'run_job', side_effect=finished) as run, \
                mock.patch.object(jobs, '_print_result'), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.cmd_run(args), 0)
            self.assertEqual(jobs.cmd_run(args), 0)
            self.assertEqual(run.call_count, 1)

    def test_concurrent_create_and_launch_admission_are_idempotent(self):
        barrier = threading.Barrier(4)
        results = []; errors = []
        def admit():
            try:
                barrier.wait(timeout=5)
                plan = self.plan()
                jid = jobs.create_job(plan)
                results.append((jid, coordination.claim_launch(plan, jid)))
            except BaseException as e: errors.append(e)
        threads = [threading.Thread(target=admit) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(timeout=10)
        self.assertFalse(any(thread.is_alive() for thread in threads)); self.assertEqual(errors, [])
        self.assertEqual(len(results), 4)
        self.assertEqual(len({jid for jid, _ in results}), 1)
        self.assertEqual(sum(launched for _, launched in results), 1)

    def test_cli_review_defect_rework_stays_same_owner_task_and_is_idempotent(self):
        self.cli(); self.cli('review', 'B', 'R', 'first')
        self.assertEqual(self.cli('fix', 'R', 'B', 'review')[0], 3)
        coordination.record_transition('task-X', 'finding-F1', authority='A', kind='review',
            actor='R', recipient='B', predecessor='review', finding='F1: missing failure guard',
            evidence='owned reviewed fixture receipt')
        self.assertEqual(self.cli('fix', 'R', 'B', 'review', 'finding-F1')[0], 0)
        self.assertEqual(self.cli('fix', 'R', 'B', 'review', 'finding-F1')[0], 0)
        self.assertEqual(len(self.submissions), 3)
        data = coordination._read('task-X')
        self.assertEqual(data['owner'], 'B'); self.assertEqual(data['revision'], 1)
        self.assertEqual(data['receipts']['finding-F1']['used_by'], 'fix')

    def test_cli_accepted_handoff_is_atomic_replayable_and_old_owner_refused(self):
        self.cli()
        kwargs = dict(authority='A', kind='handoff', actor='B', recipient='C',
                      predecessor='first', accepted_by='C', evidence='explicit host custody receipt')
        coordination.record_transition('task-X', 'custody-1', **kwargs)
        self.assertEqual(self.cli('handoff', 'B', 'C', 'first', 'custody-1')[0], 0)
        coordination.record_transition('task-X', 'custody-1', **kwargs)
        self.assertEqual(self.cli('handoff', 'B', 'C', 'first', 'custody-1')[0], 0)
        data = coordination._read('task-X')
        self.assertEqual(data['owner'], 'C'); self.assertEqual(data['epoch'], 1)
        before = self.ids()
        self.assertEqual(self.cli('old-owner', 'B', 'D', 'first')[0], 3)
        self.assertEqual(self.ids(), before); self.assertEqual(len(self.submissions), 2)

    def test_self_declared_handoff_and_fake_receipt_refused(self):
        self.cli()
        self.assertEqual(self.cli('handoff', 'B', 'C', 'first', 'invented')[0], 3)
        with self.assertRaises(coordination.Refusal):
            coordination.record_transition('task-X', 'no-acceptance', authority='A', kind='handoff',
                actor='B', recipient='C', predecessor='first', evidence='a brief', accepted_by=None)
        with self.assertRaises(coordination.Refusal):
            coordination.record_transition('task-X', 'no-custody-change', authority='A', kind='handoff',
                actor='B', recipient='B', predecessor='first', evidence='a brief', accepted_by='B')

    def test_worker_cannot_issue_acceptance_or_drop_actual_parent_identity(self):
        parent = jobs.create_job(self.plan())
        with mock.patch.dict(os.environ, TWINLANE_ENGINE_JOB=parent):
            with self.assertRaises(coordination.Refusal):
                coordination.record_transition('task-X', 'fake', authority='A', kind='review',
                    actor='R', recipient='B', predecessor='first', evidence='brief', finding='self assertion')
            with self.assertRaises(jobs.Refusal): jobs.create_job(copy.deepcopy(self.base))
            with self.assertRaises(jobs.Refusal): jobs.create_job(self.plan('new-root'))
            self.assertEqual(self.cli(labelled=False)[0], 3)
            self.assertEqual(self.cli('new-root')[0], 3)
            child = jobs.create_job(self.plan('child', 'B', 'C', 'first'))
            self.assertNotEqual(child, parent)

    def test_unlabelled_root_legacy_is_compatible_and_explicitly_unprotected(self):
        self.assertEqual(self.cli(labelled=False)[0], 0)
        self.assertEqual(self.cli(labelled=False)[0], 0)
        self.assertEqual(len(self.ids()), 2)

    def test_invalid_partial_envelope_cli_refused_before_create(self):
        with mock.patch.object(common, 'load_policy', return_value={}), \
                mock.patch.object(jobs, 'plan_run', return_value=copy.deepcopy(self.base)), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jobs.cmd_run(['--brief', 'fixture', '--task-id', 'X']), 3)
        self.assertEqual(self.ids(), set())

    def test_allocation_failure_is_durable_uncertainty_not_a_new_request(self):
        with mock.patch.object(jobs, '_create_job', side_effect=OSError('owned fixture write failure')):
            with self.assertRaises(OSError): jobs.create_job(self.plan())
        with self.assertRaises(jobs.Refusal): jobs.create_job(self.plan())
        self.assertEqual(self.ids(), set())

    def test_missing_existing_job_refuses_replacement(self):
        jid = jobs.create_job(self.plan())
        Path(jobs._p(jid, 'job.json')).unlink()
        with self.assertRaises(jobs.Refusal): jobs.create_job(self.plan())

    def test_internal_runner_cannot_bypass_launch_admission(self):
        jid = jobs.create_job(self.plan())
        with mock.patch.object(jobs, '_run_claimed') as engine:
            result = jobs.run_job(jid)
        self.assertEqual(result['status'], 'refused'); engine.assert_not_called()

    def test_review_receipt_cannot_be_reused_for_another_revision(self):
        self.cli(); self.cli('review', 'B', 'R', 'first')
        coordination.record_transition('task-X', 'F1', authority='A', kind='review', actor='R',
            recipient='B', predecessor='review', finding='actual F1', evidence='host finding receipt')
        self.cli('fix', 'R', 'B', 'review', 'F1')
        self.assertEqual(self.cli('fix-again', 'R', 'B', 'review', 'F1')[0], 3)

    def test_handoff_invalidates_unlaunched_old_owner_request(self):
        first = self.plan(); old_job = jobs.create_job(first)
        coordination.record_transition('task-X', 'H1', authority='A', kind='handoff', actor='B',
            recipient='C', predecessor='first', accepted_by='C', evidence='explicit acceptance')
        jobs.create_job(self.plan('handoff', 'B', 'C', 'first', 'H1'))
        with self.assertRaises(coordination.Refusal): coordination.claim_launch(first, old_job)

    def test_unreadable_registry_never_allocates_replacement_work(self):
        self.cli()
        Path(coordination._path('task-X')).write_text('{bad json')
        before = self.ids()
        self.assertEqual(self.cli()[0], 3)
        self.assertEqual(self.ids(), before)

    def test_F1_unknown_actual_parent_cannot_allocate_unlabelled_child(self):
        parent = jobs.create_job(self.plan())
        path = Path(jobs._p(parent, 'job.json'))
        for content in (None, '{broken', '[]'):
            if content is None:
                if path.exists(): path.unlink()
            else: path.write_text(content)
            before = self.ids()
            with mock.patch.dict(os.environ, TWINLANE_ENGINE_JOB=parent):
                with self.assertRaises(jobs.Refusal): jobs.create_job(copy.deepcopy(self.base))
                self.assertEqual(self.cli(labelled=False)[0], 3)
            self.assertEqual(self.ids(), before)

    def test_R1_coordinated_job_is_visible_to_existing_baton(self):
        jid = jobs.create_job(self.plan())
        self.assertRegex(jid, baton.JOB_ID_RE)
        baton.record_job('owned-fixture', {'id': jid, 'engine': 'claude', 'status': 'queued'})
        value = common.read_json(str(Path(common.BATONS) / 'owned-fixture.json'))
        self.assertIn(jid, [entry['id'] for entry in value['jobs']])

    def test_R2_same_reviewer_two_rounds_still_requires_fresh_findings(self):
        self.cli(); self.cli('review', 'B', 'R', 'first')
        for n, predecessor in ((1, 'review'), (2, 'review-2')):
            self.assertEqual(self.cli(f'plain-return-{n}', 'R', 'B', predecessor)[0], 3)
            coordination.record_transition('task-X', f'F{n}', authority='A', kind='review',
                actor='R', recipient='B', predecessor=predecessor, finding=f'concrete F{n}', evidence='host review')
            self.assertEqual(self.cli(f'fix-{n}', 'R', 'B', predecessor, f'F{n}')[0], 0)
            if n == 1: self.assertEqual(self.cli('review-2', 'B', 'R', 'fix-1')[0], 0)
        self.assertEqual(coordination._read('task-X')['revision'], 2)

    def test_R3_refused_replay_returns_refusal_and_no_inline_retry_advice(self):
        self.cli(); jid = self.submissions[0]
        common.write_json(jobs._p(jid, 'result.json'), {'id': jid, 'status': 'refused', 'exit_code': 3})
        rc, output = self.cli()
        self.assertEqual(rc, 3); self.assertIn('refused', output)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            jobs._refuse_background(jid, common.read_json(jobs._p(jid, 'job.json')), False,
                                    OSError('owned detach refusal'))
        self.assertNotIn('Run without --bg', err.getvalue())

    def test_R4_replay_keeps_identity_when_auto_route_changes(self):
        self.cli(); jid = self.submissions[0]
        self.base['engine'] = 'codex'; self.base['primary'] = {'engine': 'codex', 'model': 'different-automatic-route'}
        rc, output = self.cli()
        self.assertEqual(rc, 0); self.assertIn(jid, output)
        self.assertEqual(len(self.submissions), 1)

    def test_R5_refused_internal_runner_persists_terminal_receipt(self):
        plan = self.plan(); jid = jobs.create_job(plan)
        result = jobs.run_job(jid)
        self.assertEqual(result['status'], 'refused')
        saved = common.read_json(jobs._p(jid, 'result.json'))
        self.assertEqual(saved['status'], 'refused')
        self.assertEqual(jobs.job_status(jid), 'refused')

    def test_replay_is_read_only_before_unavailable_routing_and_meter(self):
        self.cli()
        args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json',
                '--task-id', 'task-X', '--dispatch-id', 'first', '--actor', 'A', '--recipient', 'B']
        with mock.patch.object(jobs, 'plan_run', side_effect=jobs.Refusal(3, 'included allowance unavailable')) as route, \
                mock.patch.object(jobs, '_meter', side_effect=AssertionError('no meter on replay')) as meter, \
                mock.patch.object(jobs, '_start_background') as spawn, contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jobs.cmd_run(args), 0)
            route.assert_not_called(); meter.assert_not_called(); spawn.assert_not_called()
            self.assertEqual(jobs.cmd_run(args + ['--model', 'explicitly-different-model']), 3)
            route.assert_not_called(); spawn.assert_not_called()

    def test_replay_of_unproven_launch_reports_pending_not_success(self):
        self.cli(); jid = self.submissions[0]
        Path(jobs._p(jid, 'result.json')).unlink()
        rc, text = self.cli()
        self.assertEqual(rc, common.EXIT_RUNNING); self.assertIn('queued', text)
        self.assertEqual(len(self.submissions), 1)

    def test_compatible_id_collision_is_bounded_and_preserves_existing_folder(self):
        taken = Path(common.JOBS) / '20261011-000000-claude-aaaa'; taken.mkdir()
        (taken / 'keep.txt').write_text('peer fixture')
        # An unreadable live record occupies its own WIP lane; allocation still avoids its id.
        with mock.patch.object(common, 'stamp', return_value='20261011-000000'), \
                mock.patch.object(coordination.secrets, 'token_hex', side_effect=['aaaa', 'bbbb']):
            self.assertTrue(jobs.create_job(self.plan()).endswith('-bbbb'))
        self.assertEqual((taken / 'keep.txt').read_text(), 'peer fixture')

    def test_runner_refusal_preserves_existing_terminal_receipt(self):
        jid = jobs.create_job(self.plan())
        receipt = {'id': jid, 'status': 'done', 'exit_code': 0, 'owned_marker': 'preserve'}
        common.write_json(jobs._p(jid, 'result.json'), receipt)
        self.assertEqual(jobs.run_job(jid)['status'], 'done')
        self.assertEqual(common.read_json(jobs._p(jid, 'result.json')), receipt)

    def test_runner_refusal_preserves_other_running_or_unknown_engine(self):
        jid = jobs.create_job(self.plan())
        with mock.patch.object(jobs, '_claim_runner', return_value=(123, 'owned-fixture')):
            self.assertEqual(jobs.run_job(jid)['status'], 'running')
        self.assertFalse(Path(jobs._p(jid, 'result.json')).exists())
        with mock.patch.object(jobs, '_engine_state', return_value=common.UNKNOWN):
            self.assertEqual(jobs.run_job(jid)['status'], 'running')
        self.assertFalse(Path(jobs._p(jid, 'result.json')).exists())

    def test_readable_actual_legacy_parent_retains_compatibility(self):
        parent = jobs.create_job(copy.deepcopy(self.base))
        with mock.patch.dict(os.environ, TWINLANE_ENGINE_JOB=parent):
            self.assertEqual(self.cli(labelled=False)[0], 0)
        self.assertEqual(len(self.ids()), 2)

    def test_note1_brief_snapshot_survives_edit_before_planning(self):
        args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json',
                '--task-id', 'task-X', '--dispatch-id', 'first', '--actor', 'A', '--recipient', 'B']
        original_read = jobs._read_text
        def edited_read(path):
            text = original_read(path)
            self.brief.write_text('edited after caller snapshot')
            return text
        seen = []
        with mock.patch.object(jobs, '_read_text', side_effect=edited_read) as read, \
                mock.patch.object(common, 'load_policy', return_value={'routing': {'intel': {}}}), \
                mock.patch.object(jobs, 'brief_problem', side_effect=lambda text: seen.append(text)), \
                mock.patch.object(jobs, '_meter', side_effect=jobs.Refusal(3, 'fixture stop before routing')), \
                contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jobs.cmd_run(args), 3)
        self.assertEqual(seen, ['owned fixture'])
        self.assertEqual(read.call_count, 1)
        self.assertEqual(self.ids(), set())

    def test_note1_continue_context_keeps_caller_snapshot_separate(self):
        args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json',
                '--continue-job', 'fixture-prior', '--task-id', 'task-X', '--dispatch-id', 'first',
                '--actor', 'A', '--recipient', 'B']
        def transform(args, policy):
            self.assertEqual(args._coordination_brief_snapshot, 'owned fixture')
            plan = copy.deepcopy(self.base)
            plan['brief_text'] = 'owned fixture\n\nprior job context'
            plan['continue_job'] = args.continue_job
            return plan
        with mock.patch.object(common, 'load_policy', return_value={}), \
                mock.patch.object(jobs, 'plan_run', side_effect=transform), \
                mock.patch.object(jobs, '_start_background', return_value=0), \
                contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.cmd_run(args), 0)
        jid = next(iter(self.ids()))
        spec = common.read_json(jobs._p(jid, 'job.json'))
        self.assertEqual(spec['caller_intent']['brief_text'], 'owned fixture')
        self.assertIn('prior job context', spec['brief_text'])

    def test_note3_refused_stale_runner_preserves_lost_attempt_evidence(self):
        jid = jobs.create_job(self.plan())
        for evidence in ({'attempt': 1}, {'started_at': '2026-10-11T00:00:00Z'},
                         {'child_pid': 123, 'child_created': 'old stamp'}):
            status = {'id': jid, 'status': 'lost', **evidence}
            common.write_json(jobs._p(jid, 'status.json'), status)
            with mock.patch.object(jobs, '_engine_state', return_value=common.DEAD), \
                    mock.patch.object(jobs, '_run_claimed') as engine:
                result = jobs.run_job(jid)
            self.assertEqual(result['status'], 'lost')
            self.assertFalse(Path(jobs._p(jid, 'result.json')).exists())
            self.assertEqual(common.read_json(jobs._p(jid, 'status.json')), status)
            engine.assert_not_called()

    # ------------------------------------------------------------ predecessor registries and lineage names

    def install_v1_fixture(self):
        registry = Path(common.STATE) / 'coordination' / V1_REGISTRY_NAME
        registry.parent.mkdir(parents=True)
        registry.write_bytes(V1_REGISTRY.encode('utf-8'))
        for jid, text in V1_JOBS.items():
            folder = Path(common.JOBS) / jid
            folder.mkdir()
            (folder / 'job.json').write_bytes(text.encode('utf-8'))
            common.write_json(str(folder / 'result.json'), {'id': jid, 'status': 'done', 'exit_code': 0})
        self.brief.write_bytes(V1_BRIEF)
        return registry

    def v1_cli(self, request, actor, recipient, parent=None, receipt=None, extra=()):
        args = ['--brief', str(self.brief), '--bg', '--json', '--kind', 'intel', '--task-id', V1_TASK,
                '--dispatch-id', request, '--actor', actor, '--recipient', recipient] + list(extra)
        if parent: args += ['--parent-dispatch', parent]
        if receipt: args += ['--transition-receipt', receipt]
        return self.run_cli(args)

    def snapshot(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}

    def test_v1_registry_replays_unchanged(self):
        registry = self.install_v1_fixture()
        self.assertEqual(hashlib.sha256(V1_TASK.encode()).hexdigest() + '.json', V1_REGISTRY_NAME)
        self.assertEqual(Path(coordination._path(V1_TASK)), registry)
        self.assertEqual(coordination.FINGERPRINT_KEYS,
                         ('engine', 'kind', 'profile', 'cwd', 'brief_text', 'primary', 'failover',
                          'resume', 'resume_jobs', 'continue_job', 'coordination'))
        self.assertEqual(jobs.CALLER_INTENT_FIELDS,
                         ('kind', 'engine', 'model', 'effort', 'tier', 'timeout', 'max_turns',
                          'resume', 'continue_job', 'write', 'urgent', 'failover', 'baton'))
        before = self.snapshot()
        # A replay never routes: the route and meter are unavailable here and must not be consulted.
        self.base = None
        cases = (('impl-1', 'lead', 'worker-a', None, (), '20261011-115400-claude-3f2a'),
                 ('review-1', 'worker-a', 'reviewer-r', 'impl-1', ('--model', 'fixture-model', '--timeout', '900'),
                  '20261011-115500-claude-9c01'))
        with mock.patch.object(jobs, '_meter', side_effect=AssertionError('no meter on replay')):
            for request, actor, recipient, parent, extra, jid in cases:
                with self.subTest(request=request):
                    rc, out = self.v1_cli(request, actor, recipient, parent, extra=extra)
                    self.assertEqual(rc, 0, out)
                    value = json.loads(out)
                    self.assertEqual((value['id'], value['replayed'], value['status']), (jid, True, 'done'))
            # Changed executable intent under a recorded identity: refused, still nothing written.
            self.assertEqual(self.v1_cli('impl-1', 'lead', 'worker-a', extra=('--model', 'other'))[0], 3)
        self.assertEqual(self.submissions, [])
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(registry.read_bytes(), V1_REGISTRY.encode('utf-8'))

    def test_v1_registry_continues_with_its_recorded_receipt(self):
        registry = self.install_v1_fixture()
        old = json.loads(V1_REGISTRY)
        self.base = dict(engine='claude', kind='intel', profile='intel', cwd=str(self.root),
                         brief_text=V1_BRIEF.decode(), primary={'engine': 'claude', 'model': 'fixture'},
                         resume=None, failover=True, notes=[])
        rc, out = self.v1_cli('fix-1', 'reviewer-r', 'worker-a', 'review-1', 'finding-1')
        self.assertEqual(rc, 0, out); self.assertEqual(len(self.submissions), 1)
        data = json.loads(registry.read_text(encoding='utf-8'))
        self.assertIsNone(coordination.registry_problem(data, V1_TASK))
        self.assertEqual(registry.read_bytes(), json.dumps(data, ensure_ascii=False, indent=2).encode('utf-8'))
        for request in ('impl-1', 'review-1'):
            self.assertEqual(data['dispatches'][request], old['dispatches'][request])
        self.assertEqual((data['revision'], data['owner'], data['epoch']), (1, 'worker-a', 0))
        self.assertEqual(data['receipts']['finding-1'], dict(old['receipts']['finding-1'], used_by='fix-1'))
        self.assertEqual(data['dispatches']['fix-1']['lineage'], ['lead', 'worker-a'])
        self.assertEqual(data['dispatches']['fix-1']['job'], self.submissions[0])

    def test_fingerprint_without_caller_intent_matches_predecessor(self):
        plan = dict(engine='codex', kind='code', profile='code', cwd='/fixture/wt', brief_text='Fix the parser.\n',
                    primary={'engine': 'codex', 'model': 'fixture', 'tier': None}, failover=False, resume=None,
                    resume_jobs=[], continue_job=None, notes=['varies'], meter_before={'at': 'varies'},
                    coordination=dict(task='task-live-2', dispatch='d1', actor='lead', recipient='worker-b',
                                      parent=None, receipt=None))
        self.assertEqual(coordination._fingerprint(plan), V1_PLAN_FINGERPRINT)
        plan['notes'] = ['other narration']; plan['meter_before'] = {'at': 'later'}
        self.assertEqual(coordination._fingerprint(plan), V1_PLAN_FINGERPRINT)

    def test_lineage_variable_names_the_parent(self):
        parent = jobs.create_job(self.plan())
        with mock.patch.dict(os.environ, TWINLANE_ENGINE_JOB=parent):
            with self.assertRaises(coordination.Refusal):
                coordination.record_transition('task-X', 'fake', authority='A', kind='review',
                    actor='R', recipient='B', predecessor='first', evidence='brief', finding='self assertion')
            self.assertEqual(self.cli(labelled=False)[0], 3)
            self.assertEqual(self.cli('new-root')[0], 3)
            self.assertEqual(self.cli('child', 'B', 'C', 'first')[0], 0)
            self.assertEqual(self.cli('child-2', 'B', 'D', 'first')[0], 0)
        self.assertEqual(len(self.submissions), 2)

    def test_invalid_lineage_job_identity_refuses(self):
        for value in ('../escape', '20261011-000000-claude-aaaa\n', 'not-a-job'):
            with self.subTest(value=value), mock.patch.dict(os.environ, TWINLANE_ENGINE_JOB=value):
                with self.assertRaises(jobs.Refusal): jobs.create_job(copy.deepcopy(self.base))
                self.assertEqual(self.cli(labelled=False)[0], 3)
        self.assertEqual(self.ids(), set())

    def test_malformed_registry_fields_refuse_and_stay_untouched(self):
        self.cli()
        path = Path(coordination._path('task-X'))
        good = json.loads(path.read_text(encoding='utf-8'))
        def first_entry(d): return d['dispatches']['first']
        mutations = {
            'job escapes the jobs folder': lambda d: first_entry(d).update(job='../outside'),
            'version is a boolean': lambda d: d.update(version=True),
            'unknown version': lambda d: d.update(version=2),
            'missing admission flag': lambda d: first_entry(d).pop('ready'),
            'negative epoch': lambda d: d.update(epoch=-1),
            'entry from a future epoch': lambda d: first_entry(d).update(epoch=5),
            'receipts not an object': lambda d: d.update(receipts=[]),
            'other task': lambda d: d.update(task='task-Y'),
            'short fingerprint': lambda d: first_entry(d).update(fingerprint='abc'),
            'lineage with a bad name': lambda d: first_entry(d).update(lineage=['A', '../B']),
        }
        for label, mutate in mutations.items():
            with self.subTest(label):
                data = copy.deepcopy(good); mutate(data)
                path.write_text(json.dumps(data, indent=2), encoding='utf-8')
                stored, before = path.read_bytes(), self.ids()
                self.assertEqual(self.cli()[0], 3)
                self.assertEqual(self.cli('child', 'B', 'C', 'first')[0], 3)
                with self.assertRaises(coordination.Refusal):
                    coordination.record_transition('task-X', 'R1', authority='A', kind='review', actor='B',
                        recipient='B', predecessor='first', finding='x', evidence='host')
                self.assertEqual(path.read_bytes(), stored); self.assertEqual(self.ids(), before)
        self.assertEqual(len(self.submissions), 1)

    def test_registry_present_but_unreachable_is_not_absent(self):
        path = Path(coordination._path('task-X'))
        path.parent.mkdir(parents=True)
        with mock.patch.object(coordination.os, 'stat', side_effect=PermissionError('fixture denial')):
            with self.assertRaises(coordination.Refusal):
                coordination._read('task-X')
        self.assertFalse(path.exists())

    # ------------------------------------------------------------ owner resume

    def resume(self, request, parent, actor='A', recipient='B', continue_job=None, flag=True):
        job = continue_job or coordination._read('task-X')['dispatches'][parent]['job']
        self.base['continue_job'] = job
        try:
            args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json',
                    '--continue-job', job, '--task-id', 'task-X', '--dispatch-id', request,
                    '--actor', actor, '--recipient', recipient, '--parent-dispatch', parent]
            return self.run_cli(args + (['--resume-owner'] if flag else []))
        finally:
            self.base.pop('continue_job')

    def reviewed_chain(self):
        """first A->B, review B->R, accepted fix R->B, re-review B->R: all done, B still owns."""
        self.cli(); self.cli('review', 'B', 'R', 'first')
        coordination.record_transition('task-X', 'finding-F1', authority='A', kind='review',
            actor='R', recipient='B', predecessor='review', finding='F1: fixture finding',
            evidence='owned reviewed fixture receipt')
        self.assertEqual(self.cli('fix', 'R', 'B', 'review', 'finding-F1')[0], 0)
        self.assertEqual(self.cli('rereview', 'B', 'R', 'fix')[0], 0)

    def test_coordinator_resumes_finished_owner_without_new_ancestry(self):
        self.reviewed_chain()
        rc, text = self.resume('resume-1', 'fix')
        self.assertEqual(rc, 0, text)
        data = coordination._read('task-X')
        entry, fix = data['dispatches']['resume-1'], data['dispatches']['fix']
        self.assertEqual(entry['lineage'], fix['lineage'])
        self.assertEqual((data['owner'], data['epoch'], data['revision']), ('B', 0, 1))
        self.assertEqual(self.resume('resume-1', 'fix')[0], 0)  # replay: same job, no second runner
        self.assertEqual(len(self.submissions), 5)

    def test_resume_can_chain_from_the_owners_latest_resume(self):
        self.cli()
        self.assertEqual(self.resume('resume-1', 'first')[0], 0)
        self.assertEqual(self.resume('resume-2', 'resume-1')[0], 0)
        before = self.ids()
        self.assertEqual(self.resume('resume-3', 'first')[0], 3)  # no longer the latest
        self.assertEqual(self.ids(), before)

    def test_resume_without_the_flag_still_hits_ownership_and_cycle_rules(self):
        self.reviewed_chain()
        before = self.ids()
        rc, text = self.resume('resume-1', 'fix', flag=False)
        self.assertEqual(rc, 3); self.assertIn('does not own the predecessor', text)
        rc, text = self.cli('again', 'B', 'B', 'fix')
        self.assertEqual(rc, 3); self.assertIn('ancestor', text)
        self.assertEqual(self.ids(), before)

    def test_resume_refusals_leave_no_job(self):
        self.reviewed_chain()
        before = self.ids()
        cases = (dict(request='r1', parent='fix', actor='R'),                 # not the coordinator
                 dict(request='r2', parent='fix', actor='B'),                 # owner cannot self-resume
                 dict(request='r3', parent='fix', recipient='R'),             # not the owner
                 dict(request='r4', parent='rereview', recipient='R'),        # predecessor is not the owner's
                 dict(request='r5', parent='first'),                          # not the owner's latest
                 dict(request='r6', parent='fix', continue_job='20261014-000000-claude-ffff'))
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(self.resume(**case)[0], 3)
        self.assertEqual(self.ids(), before)

    def test_resume_refuses_unless_the_owner_finished_done_and_nothing_runs(self):
        self.reviewed_chain()
        data = coordination._read('task-X')
        fix, rereview = data['dispatches']['fix']['job'], data['dispatches']['rereview']['job']
        real = jobs.job_status
        before = self.ids()
        for target, status in ((fix, 'failed'), (fix, 'timeout'), (rereview, 'running'),
                               (rereview, 'unknown'), (rereview, 'orphaned')):
            with self.subTest(target=target, status=status),                     mock.patch.object(jobs, 'job_status', side_effect=lambda j: status if j == target else real(j)):
                rc, text = self.resume('r-' + status, 'fix')
                self.assertEqual(rc, 3, text)
        self.assertEqual(self.ids(), before)

    def test_resume_owner_must_be_exactly_true_and_takes_no_receipt(self):
        self.cli()
        job = coordination._read('task-X')['dispatches']['first']['job']
        for value in (False, 1, 'yes'):
            plan = self.plan('r1', 'A', 'B', 'first')
            plan['coordination']['resume_owner'] = value
            plan['continue_job'] = job
            with self.subTest(value=value), self.assertRaises(coordination.Refusal):
                coordination.allocate(plan, lambda jid: None)
        coordination.record_transition('task-X', 'finding-F1', authority='A', kind='review',
            actor='A', recipient='B', predecessor='first', finding='F1: fixture finding',
            evidence='owned reviewed fixture receipt')  # real and unused: only the resume rule refuses it
        plan = self.plan('r2', 'A', 'B', 'first', receipt='finding-F1')
        plan['coordination']['resume_owner'] = True
        plan['continue_job'] = job
        with self.assertRaises(coordination.Refusal) as caught:
            coordination.allocate(plan, lambda jid: None)
        self.assertIn('takes no transition receipt', str(caught.exception))
        self.assertIsNone(coordination._read('task-X')['receipts']['finding-F1']['used_by'])

    def test_resume_without_a_task_registry_allocates_nothing(self):
        for parent in (None, 'first'):
            with self.subTest(parent=parent):
                args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json',
                        '--task-id', 'task-X', '--dispatch-id', 'r1', '--actor', 'A', '--recipient', 'B',
                        '--resume-owner']
                rc, text = self.run_cli(args + (['--parent-dispatch', parent] if parent else []))
                self.assertEqual(rc, 3, text)
                self.assertEqual(self.ids(), set())
                self.assertIsNone(coordination._read('task-X'))
        plan = self.plan('r2', 'A', 'B')
        plan['coordination']['resume_owner'] = True
        with self.assertRaises(coordination.Refusal) as caught:
            coordination.allocate(plan, lambda jid: None)
        self.assertIn('existing task registry', str(caught.exception))
        self.assertIsNone(coordination._read('task-X'))

    def test_resume_needs_a_predecessor_in_an_existing_registry(self):
        self.cli()
        before = self.ids()
        args = ['--brief', str(self.brief), '--cd', str(self.root), '--bg', '--json',
                '--task-id', 'task-X', '--dispatch-id', 'r1', '--actor', 'A', '--recipient', 'B',
                '--resume-owner']
        self.assertEqual(self.run_cli(args)[0], 3)
        self.assertEqual(self.ids(), before)

    def test_worker_cannot_resume_an_owner(self):
        self.cli()
        plan = self.plan('r1', 'A', 'B', 'first')
        plan['coordination']['resume_owner'] = True
        plan['continue_job'] = coordination._read('task-X')['dispatches']['first']['job']
        with mock.patch.object(coordination, 'worker_ids', return_value=['20261014-000000-claude-abcd']),                 mock.patch.object(coordination, '_check_lineage'):
            with self.assertRaises(coordination.Refusal) as caught:
                coordination.allocate(plan, lambda jid: None)
        self.assertIn('workers cannot resume', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
