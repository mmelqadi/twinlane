"""Checks for user-facing boundaries that are easy to overstate in the docs."""
from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


def doc(name):
    return ' '.join((ROOT / name).read_text(encoding='utf-8').split())


class DocumentedBoundaries(unittest.TestCase):
    def test_tagline_and_demo(self):
        readme = doc('README.md')
        self.assertIn('Run Claude Code and Codex together on your included subscriptions', readme)
        self.assertIn('![twinlane board](docs/demo.svg)', readme)
        self.assertIn('== METERS', readme)

    def test_routing_and_thresholds(self):
        readme = doc('README.md')
        for token in ('75%', '80%', '85%', '100%', '`code`', '`parallel`'):
            self.assertIn(token, readme)
        self.assertIn('unknown', readme.lower())

    def test_money_guard(self):
        readme = doc('README.md')
        self.assertIn('paid extra usage is ON', readme)
        for name in ('README.md', 'LIMITS.md'):
            body = doc(name)
            self.assertIn('Credits and extra usage are checked only when a job starts', body)
            self.assertIn('A long Codex job can still reach paid credits', body)
            self.assertIn('passes no option that enables credits', body)

    def test_worker_evidence(self):
        readme = doc('README.md')
        for token in ('`twinlane run`', '`twinlane result JOB`', '`events.jsonl`', '`last-message.md`'):
            self.assertIn(token, readme)

    def test_baton_and_sessions(self):
        readme = doc('README.md')
        self.assertIn('`twinlane baton new', readme)
        self.assertIn('`twinlane sessions`', readme)
        self.assertIn('Approvals never transfer', readme)

    def test_read_only_meter_limits(self):
        readme = doc('README.md')
        limits = doc('LIMITS.md')
        self.assertIn('read-only', readme)
        self.assertIn('undocumented', limits)
        self.assertIn('api.anthropic.com', limits)

    def test_platform_and_hook_limits(self):
        readme = doc('README.md')
        limits = doc('LIMITS.md')
        self.assertIn('POSIX', readme)
        self.assertIn('not a sandbox', limits)
        for phrase in ('`codex exec --json`', '`codex --version`', 'model ids are defaults as of 2026-10',
                       'interactive sessions are NOT guarded', 'heuristic over shell text',
                       'TWINLANE_CODEX_BIN', 'at most 3 copies'):
            self.assertIn(phrase.lower(), limits.lower())

    def test_config_and_install_preview(self):
        readme = doc('README.md')
        self.assertIn('twinlane install --yes', readme)
        self.assertIn('TWINLANE_HOME', readme)
        self.assertIn('TWINLANE_CONFIG', readme)
        self.assertIn('policy.json', readme)
        self.assertIn('CLAUDE_CONFIG_DIR', readme)
        self.assertIn('CODEX_HOME', readme)
        self.assertIn('TWINLANE_CODEX_ADAPTER_STRICT_CREDITS', readme)


if __name__ == '__main__':
    unittest.main()
