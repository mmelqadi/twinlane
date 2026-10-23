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

    def test_restart_survival_is_not_overclaimed(self):
        readme, limits, changes = doc('README.md'), doc('LIMITS.md'), doc('CHANGELOG.md')
        for body in (readme, limits, changes):
            self.assertIn('not a general guarantee', body)
        self.assertIn('Sign-out, shutdown, sleep and reboot were not tested', readme)
        self.assertIn('`detach=wmi`', readme)
        self.assertIn('`--bg` is refused (exit 3)', readme)
        for body in (readme, limits, changes):
            self.assertNotIn('always survive', body.lower())
            self.assertNotIn('guaranteed to survive', body.lower())

    def test_guard_and_credit_limits_are_stated(self):
        readme, limits, design = doc('README.md'), doc('LIMITS.md'), doc('DESIGN.md')
        for body in (readme, limits, design):
            self.assertIn('not a sandbox', body)
        self.assertIn('a command obfuscated in a way it does not model can still pass', readme)
        self.assertIn('it exits 2', design)
        self.assertIn('Nothing in twinlane stops a running Codex job that crosses into credits partway', limits)
        self.assertIn('There is no per-job credits flag', readme)
        self.assertIn('never their values', readme)

    def test_liveness_and_batons(self):
        readme = doc('README.md')
        self.assertIn('never `lost`', readme)
        self.assertIn('survivor.json', readme)
        self.assertIn('exact count', readme)
        self.assertIn('never its `owner`', readme)

    def test_config_and_install_preview(self):
        readme = doc('README.md')
        self.assertIn('twinlane install --yes', readme)
        self.assertIn('TWINLANE_HOME', readme)
        self.assertIn('TWINLANE_CONFIG', readme)
        self.assertIn('policy.json', readme)
        self.assertIn('CLAUDE_CONFIG_DIR', readme)
        self.assertIn('CODEX_HOME', readme)
        self.assertIn('TWINLANE_CODEX_ADAPTER_STRICT_CREDITS', readme)


    def test_new_subcommands_are_documented(self):
        readme = doc('README.md')
        for command in ('twinlane land enqueue', 'twinlane land run', 'twinlane land release --break --reason',
                        'twinlane ship plan', 'twinlane ship run', '--execute --confirm',
                        'twinlane wt doctor', 'twinlane wt prune', 'twinlane run --best-of',
                        'twinlane compare GROUP', '`twinlane resume`', '`projects`'):
            self.assertIn(command, readme)
        for heading in ('## Landing finished work', '## Shipping landed work', '## Worktree health',
                        '## Best-of runs', '## Resuming lost jobs', '## Per-project configuration'):
            self.assertIn(heading, readme)

    def test_project_config_boundary_and_placeholders(self):
        readme, design = doc('README.md'), doc('DESIGN.md')
        self.assertIn('user** config', readme)
        self.assertIn('never read from the repository itself', readme)
        self.assertIn('never from repository content', design)
        for placeholder in ('{repo}', '{worktree}', '{branch}', '{slug}', '{target}', '{manifest}',
                            '{source_commit}', '{base}'):
            self.assertIn('`' + placeholder + '`', readme)

    def test_new_commands_do_not_overclaim(self):
        readme, limits, design, method = doc('README.md'), doc('LIMITS.md'), doc('DESIGN.md'), doc('METHODOLOGY.md')
        self.assertIn('`twinlane ship` never ships by default', readme)
        self.assertIn('never deploys by itself', limits)
        self.assertIn('never deletes an unmerged branch', limits)
        self.assertIn('`git branch -d`', design)
        self.assertIn('a deny-list and defense in depth, not a sandbox', limits)
        self.assertIn('Real confinement comes from', limits)
        self.assertIn('twinlane land run', method)
        self.assertIn('twinlane ship plan', method)

    def test_changelog_has_current_release(self):
        changes = doc('CHANGELOG.md')
        self.assertIn('## 0.2.0', changes)
        for token in ('`land`', '`ship plan`', '`wt doctor`', '`run --best-of`', '`resume`'):
            self.assertIn(token, changes)
        self.assertLess(changes.index('## 0.2.0'), changes.index('## 0.1.2'))

    def test_common_tasks_and_skill_name_real_commands(self):
        import re
        readme = (ROOT / 'README.md').read_text(encoding='utf-8')
        skill = (ROOT / 'skills' / 'twinlane' / 'SKILL.md').read_text(encoding='utf-8')
        self.assertIn('## Common tasks', readme)
        self.assertIn('skills/twinlane/SKILL.md', readme)
        self.assertTrue(skill.startswith('---\nname: twinlane\n'))
        cli = (ROOT / 'twinlane' / 'cli.py').read_text(encoding='utf-8')
        known = set(re.findall(r"cmd == '([a-z-]+)'", cli)) | set(re.findall(r"argv\[0\] == '([a-z-]+)'", cli))
        for group in re.findall(r"cmd in \(([^)]*)\)", cli):
            known |= set(re.findall(r"'([a-z-]+)'", group))
        section = readme.split('## Common tasks', 1)[1].split('\n## ', 1)[0]
        for body, name in ((section, 'README Common tasks'), (skill, 'skills/twinlane/SKILL.md')):
            used = set(re.findall(r'`twinlane ([a-z-]+)', body))
            self.assertTrue(used, name)
            self.assertEqual(used - known, set(), f'{name} names commands the CLI does not have')

    def test_fresh_readings_are_documented(self):
        readme, limits, skill = doc('README.md'), doc('LIMITS.md'), doc('skills/twinlane/SKILL.md')
        for body in (readme, limits):
            self.assertIn('--no-rollout', body)
            self.assertIn('meter.codex_rollout_fallback', body)
        self.assertIn('### Fresh readings', readme)
        self.assertIn('TWINLANE_CODEX_BIN', skill)
        self.assertIn('--no-rollout', skill)
        self.assertIn('never dispatch on a guess', skill)

    def test_version_files_agree(self):
        import re
        init = (ROOT / 'twinlane' / '__init__.py').read_text(encoding='utf-8')
        toml = (ROOT / 'pyproject.toml').read_text(encoding='utf-8')
        version = re.search(r"__version__ = '([^']+)'", init).group(1)
        self.assertEqual(version, re.search(r'(?m)^version = "([^"]+)"', toml).group(1))
        # CITATION.cff is what citing readers see: it names the same version, released the day
        # the newest CHANGELOG section gives.
        cff = (ROOT / 'CITATION.cff').read_text(encoding='utf-8')
        top = re.search(r'(?m)^## (\S+) - (\d{4}-\d{2}-\d{2})', (ROOT / 'CHANGELOG.md').read_text(encoding='utf-8'))
        self.assertEqual(re.search(r'(?m)^version: (\S+)', cff).group(1), top.group(1))
        # Unreleased development builds keep the citation for the latest actual release.
        if '.dev' not in version:
            self.assertEqual(top.group(1), version)
        self.assertEqual(re.search(r'(?m)^date-released: (\S+)', cff).group(1).strip('"\''), top.group(2))


if __name__ == '__main__':
    unittest.main()
