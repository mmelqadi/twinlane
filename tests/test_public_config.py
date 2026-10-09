"""Generic paths, config layering, and policy safety."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_support  # noqa: E402
from twinlane import common


class PublicConfig(unittest.TestCase):
    def test_home_resolution_order(self):
        with tempfile.TemporaryDirectory(prefix='paths-', dir=os.environ['TWINLANE_HOME']) as tmp:
            with mock.patch.dict(os.environ, {'TWINLANE_HOME': str(Path(tmp) / 'explicit'),
                                              'LOCALAPPDATA': str(Path(tmp) / 'local'),
                                              'XDG_DATA_HOME': str(Path(tmp) / 'xdg')}):
                self.assertEqual(common.data_home(), str(Path(tmp) / 'explicit'))
            with mock.patch.dict(os.environ, {'LOCALAPPDATA': str(Path(tmp) / 'local'),
                                              'XDG_DATA_HOME': str(Path(tmp) / 'xdg')}):
                os.environ.pop('TWINLANE_HOME', None)
                expected = Path(tmp) / ('local' if common.IS_WIN else 'xdg') / 'twinlane'
                self.assertEqual(Path(common.data_home()), expected)

    def test_user_config_merges_over_defaults(self):
        with tempfile.TemporaryDirectory(prefix='config-', dir=os.environ['TWINLANE_HOME']) as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text(json.dumps({'thresholds': {'caution_pct': 70},
                                        'claude': {'deny': ['Bash(my-command:*)']}}), encoding='utf-8')
            with mock.patch.dict(os.environ, {'TWINLANE_CONFIG': str(path)}):
                p = common.load_policy()
            self.assertEqual(p['thresholds']['caution_pct'], 70)
            self.assertEqual(p['thresholds']['handoff_pct'], 80)
            self.assertEqual(p['claude']['deny'], ['Bash(my-command:*)'])
            self.assertIn('models', p['claude'])

    def test_home_config_is_used_without_config_override(self):
        with tempfile.TemporaryDirectory(prefix='home-config-', dir=os.environ['TWINLANE_HOME']) as tmp:
            path = Path(tmp) / 'config.json'
            path.write_text('{"thresholds": {"caution_pct": 72}}', encoding='utf-8')
            with mock.patch.dict(os.environ, {'TWINLANE_HOME': tmp}):
                os.environ.pop('TWINLANE_CONFIG', None)
                policy = common.load_policy()
            self.assertEqual(policy['thresholds']['caution_pct'], 72)
            self.assertEqual(policy['thresholds']['stop_pct'], 85)

    def test_default_policy_is_generic(self):
        value = Path(common.POLICY).read_text(encoding='utf-8').lower()
        for term in ('sqlcmd', 'deploy', 'mcp__plugin_', 'c:' + '/users/'):
            self.assertNotIn(term, value)
        p = common.load_policy()
        self.assertFalse(p['claude']['extra_usage_allowed'])
        self.assertFalse(p['codex']['allow_credits'])


if __name__ == '__main__':
    unittest.main()
