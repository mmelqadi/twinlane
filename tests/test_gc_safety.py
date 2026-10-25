"""GC session protection with synthetic worktrees and mocked removal; no engines or network."""
from contextlib import ExitStack, nullcontext
import json
import os
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate before importing runtime modules
from twinlane import common, lanes, projects, sessions, worktrees


class GcSessionSafety(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        lanes._supervisor.cache_clear()
        self.addCleanup(lanes._supervisor.cache_clear)
        self.main = os.path.join(test_support._HOME, 'gc-main')
        self.path = os.path.join(test_support._HOME, 'gc-lane')
        self.policy = {'lanes': {'claude_agents': True}}
        self.worktree = {'path': self.path, 'branch': 'feature', 'head': 'abc'}

        def patch(obj, name, **kw):
            return self.stack.enter_context(mock.patch.object(obj, name, **kw))

        # All source and lifecycle evidence is synthetic. The real session collector,
        # supervisor cache, usage scan, eligibility checks and destructive recheck run.
        for name in ('STATE', 'JOBS', 'BATONS'):
            patch(common, name, new=os.path.join(test_support._HOME, 'gc-unused-' + name.lower()))
        self.stack.enter_context(mock.patch.dict(os.environ, {'TWINLANE_TEST_ISOLATION': '0'}))
        patch(common, 'claude_bin', return_value='fixture-cli')
        patch(common, 'file_lock', side_effect=lambda *a, **k: nullcontext())
        patch(common, 'git', side_effect=lambda root, *args, **kw: (0, '' if args[0] == 'cherry' else '0'))
        patch(sessions, '_candidates', return_value=[])
        patch(sessions, '_context', return_value={'policy': self.policy, 'managed': set()})
        patch(lanes, 'state_rows', return_value=[])
        patch(projects, 'resolve', return_value=(self.main, {}))
        patch(projects, 'state_dir', return_value=os.path.join(test_support._HOME, 'gc-lock'))
        patch(worktrees, 'target_branch', return_value='main')
        patch(worktrees, 'worktree_list', side_effect=lambda root: [dict(self.worktree)])
        patch(worktrees, 'classify', side_effect=self.classify)
        patch(worktrees, 'inbound_index', return_value={'links': [], 'problems': []})
        patch(worktrees, 'deep_blockers', return_value=([], []))
        patch(worktrees, '_ignored', return_value=0)
        patch(worktrees, '_ledger')
        self.remove = patch(worktrees, '_remove', return_value={
            'removed': True, 'error': None, 'branch_deleted': True, 'branch_note': None,
            'unlinked': [], 'abort': False})
        self.supervisor = patch(common, 'run')

    def classify(self, root, cfg, wt, target, usage, now):
        return dict(wt, main=False, state='merged', age_s=86400, dirty=0, locked=False,
                    tip='abc', landed=True, notes=[],
                    in_use=worktrees.in_use(wt['path'], wt['branch'], usage))

    def session(self, status='busy', **kw):
        return dict(sessionId='session-1', kind='interactive', cwd=self.path, status=status, **kw)

    def snapshots(self, *rows):
        self.supervisor.side_effect = [(0, json.dumps(snapshot), '') for snapshot in rows]

    def gc(self, yes=True):
        return worktrees.prune(self.main, yes=yes, policy=self.policy, merged_only=True)

    def assert_blocked(self, result, reason):
        self.remove.assert_not_called()
        self.assertEqual(result['removed'], 0)
        self.assertEqual(result['rows'][0]['action'], 'skip')
        self.assertIn(reason, '; '.join(result['rows'][0]['blockers']))

    def test_gc_refreshes_supervisor_before_removal(self):
        self.snapshots([], [self.session()])
        self.assert_blocked(self.gc(), 'session:session-1 (working)')
        self.assertEqual(self.supervisor.call_count, 2)

    def test_starting_session_protects_known_worktree(self):
        self.snapshots([self.session('starting')])
        self.assert_blocked(self.gc(), 'unknown interactive session state')

    def test_missing_status_protects_known_worktree(self):
        row = self.session()
        row.pop('status')
        self.snapshots([row])
        self.assert_blocked(self.gc(), 'unknown interactive session state')

    def test_unknown_session_without_cwd_blocks_cleanup(self):
        self.snapshots([dict(self.session('starting'), cwd=None)])
        self.assert_blocked(self.gc(), 'ownership unknown')

    def test_unknown_session_with_unreadable_cwd_blocks_cleanup(self):
        self.snapshots([dict(self.session('starting'), cwd=42)])
        self.assert_blocked(self.gc(), 'ownership unknown')

    def test_unrecognised_collected_state_protects_known_worktree(self):
        with mock.patch.object(sessions, 'collect_sessions', return_value={
                'rows': [{'id': 'session-1', 'state': 'starting', 'cwd': self.path}]}):
            self.assert_blocked(self.gc(), 'unknown interactive session state')

    def test_idle_session_protects_known_worktree(self):
        self.snapshots([self.session('idle')], [self.session('idle')])
        self.assert_blocked(self.gc(), 'session:session-1 (idle)')

    def test_session_turning_idle_before_the_recheck_still_blocks(self):
        self.snapshots([], [self.session('idle')])
        self.assert_blocked(self.gc(), 'session:session-1 (idle)')
        self.assertEqual(self.supervisor.call_count, 2)

    def test_ended_session_allows_mocked_removal(self):
        self.snapshots([self.session('ended')], [self.session('ended')])
        self.assertEqual(self.gc()['removed'], 1)
        self.remove.assert_called_once()
        self.assertEqual(self.supervisor.call_count, 2)

    def test_listing_keeps_supervisor_cache(self):
        self.snapshots([])
        for _ in range(2):
            self.assertEqual(self.gc(yes=False)['rows'][0]['action'], 'would remove')
        self.assertEqual(self.supervisor.call_count, 1)
        self.remove.assert_not_called()


if __name__ == '__main__':
    unittest.main()
