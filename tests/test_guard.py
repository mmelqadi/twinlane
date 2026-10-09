"""twinlane guard: the PreToolUse hook twinlane passes to its own Claude workers. Pure parsing; no model calls."""
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, guard  # noqa: E402

ALLOW = common.load_policy()['claude']['intel_allow']
WT = ('\x43:\x2fUsers/alice/worktrees/task-a' if os.name == 'nt' else
      os.path.join(tempfile.gettempdir(), 'twinlane-guard-worktree', 'task-a'))
OTHER = os.path.join(tempfile.gettempdir(), 'twinlane-guard-other', 'x.py')


def bash(cmd):
    return {'command': cmd}


class ShellHazards(unittest.TestCase):
    """prefix deny rules miss git global options, full paths, PowerShell and wrappers."""

    def test_denied_in_every_profile(self):
        for cmd in ('git commit -m x', 'git -C . commit -m x', 'git -c core.hooksPath=/dev/null commit -m x',
                    'git --git-dir=.git push', '"\x43:/Program Files/Git/cmd/git.exe" push origin master',
                    'cmd //c "git push"', 'cmd /c git push', 'powershell -Command "git reset --hard HEAD~1"',
                    'GIT_DIR=.git git push', 'git reset --hard', 'git clean -fdx', 'git rebase -i HEAD~3',
                    'git worktree remove ../x', 'git branch -D feature', 'git branch --delete feature',
                    'git cherry-pick abc', 'git update-ref -d refs/heads/x',
                    'claude -p hi', '\x43:\x2fUsers/alice/.local/bin/claude.exe -p hi', 'codex exec hi',
                    'python \x43:\x2fUsers/alice/projects/twinlane/twinlane/cli.py run --brief b', 'twinlane run --brief b',
                    'ls && git push'):
            for profile in ('intel', 'code'):
                self.assertIsNotNone(guard.evaluate('Bash', bash(cmd), profile, WT, ALLOW), (profile, cmd))
        self.assertIsNotNone(guard.evaluate('PowerShell', bash('git push origin x'), 'code', WT, ALLOW))

    def test_read_only_git_is_allowed(self):
        for cmd in ('git log --oneline -10', 'git status', 'git log | head -5', 'git diff HEAD~1 -- a.php',
                    'git -C . log -3', 'git branch --list', 'git worktree list'):
            self.assertIsNone(guard.shell_hazard(cmd), cmd)


class IntelProfile(unittest.TestCase):
    """Findings 8 and 13: intel stays read-only whatever the settings inherit."""

    def test_write_capable_forms_of_allowed_commands(self):
        for cmd in ('git diff --output=x.log', 'git log --output x', 'git diff --ext-diff', 'rg --pre cat foo',
                    'rg --pre=sh x', 'git log > out.txt', 'ls `rm -rf x`', 'ls $(touch x)', 'head a.txt >> b'):
            self.assertIsNotNone(guard.evaluate('Bash', bash(cmd), 'intel', WT, ALLOW), cmd)

    def test_a_lone_ampersand_starts_a_new_command(self):
        # `git log & python -c ...` passed the intel guard (no split on a lone &)
        for cmd in ('git log & python -c "open(1,2)"', 'git status&python x.py', 'rg foo &\r\nnpm test',
                    'git log -3 |& python x.py'):
            self.assertIsNotNone(guard.evaluate('Bash', bash(cmd), 'intel', WT, ALLOW), cmd)
        for profile in ('intel', 'code'):
            self.assertIsNotNone(guard.evaluate('Bash', bash('ls & git push'), profile, WT, ALLOW), profile)
            self.assertIsNotNone(guard.evaluate('Bash', bash('git status & claude -p hi'), profile, WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash('git log -3 && git status'), 'intel', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash('php -l a.php > /dev/null 2>&1'), 'code', WT, ALLOW))

    def test_only_the_allow_list(self):
        self.assertIsNone(guard.evaluate('Bash', bash('rg -n foo helpers/'), 'intel', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash('git log -3 | head -2'), 'intel', WT, ALLOW))
        for cmd in ('python x.py', 'php -r "file_put_contents(1,2);"', 'curl -X POST http://x', 'npm test'):
            self.assertIsNotNone(guard.evaluate('Bash', bash(cmd), 'intel', WT, ALLOW), cmd)
        for tool in ('Edit', 'Write', 'PowerShell', 'NotebookEdit', 'mcp__claude-in-chrome__navigate',
                     'mcp__example__write', 'mcp__computer-use__left_click'):
            self.assertIsNotNone(guard.evaluate(tool, {}, 'intel', WT, ALLOW), tool)
        for tool in ('Read', 'Grep', 'Glob', 'WebFetch', 'ToolSearch'):
            self.assertIsNone(guard.evaluate(tool, {}, 'intel', WT, ALLOW), tool)

    def test_nested_agents_are_denied_for_both_profiles(self):
        for profile in ('intel', 'code'):
            for tool in ('Agent', 'Task'):
                self.assertIsNotNone(guard.evaluate(tool, {}, profile, WT, ALLOW))


class CodeProfile(unittest.TestCase):
    """the code profile is confined to its worktree, not only by the prompt."""

    def test_file_tools_inside_and_sibling_worktree(self):
        self.assertIsNone(guard.evaluate('Edit', {'file_path': WT + '/src/x.py'}, 'code', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Write', {'file_path': 'src/new.py'}, 'code', WT, ALLOW))
        path = WT + '/../task-b/x.py'
        self.assertIsNotNone(guard.evaluate('Edit', {'file_path': path}, 'code', WT, ALLOW))

    @unittest.skipUnless(os.name == 'nt', 'Windows drive-letter paths only')
    def test_file_tools_outside_the_windows_worktree(self):
        for path in ('\x43:/work/example-repo/src/x.py', '\x43:\\projects-alt\\a.py',
                     '/c/work/example-repo/x.py'):
            self.assertIsNotNone(guard.evaluate('Edit', {'file_path': path}, 'code', WT, ALLOW), path)

    @unittest.skipIf(os.name == 'nt', 'POSIX absolute paths only')
    def test_file_tools_outside_the_posix_worktree(self):
        self.assertIsNotNone(guard.evaluate('Edit', {'file_path': OTHER}, 'code', WT, ALLOW))

    def test_writing_shell_commands_inside_the_worktree(self):
        self.assertIsNone(guard.evaluate('Bash', bash(f'cp a.py {WT}/b.py'), 'code', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash('python -m py_compile a.py > /dev/null'), 'code', WT, ALLOW))

    @unittest.skipUnless(os.name == 'nt', 'Windows drive-letter paths only')
    def test_writing_shell_commands_outside_the_windows_worktree(self):
        self.assertIsNotNone(guard.evaluate('Bash', bash('cp a.py \x43:/work-alt/a.py'), 'code', WT, ALLOW))
        self.assertIsNotNone(guard.evaluate('PowerShell', bash('Set-Content -Path \x43:\\projects\\x.txt -Value 1'),
                                            'code', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash('cat \x43:/work/example-repo/README.md'), 'code', WT,
                                         ALLOW))  # reading outside is fine

    @unittest.skipUnless(os.name == 'nt', 'cmd switches only exist on Windows')
    def test_cmd_switches_inside_the_windows_worktree_are_not_paths(self):
        for cmd in ('del /Q build\\x.txt', 'rmdir /S /Q build', 'robocopy src dst /MIR', 'xcopy src dst /E /I'):
            self.assertIsNone(guard.evaluate('Bash', bash(cmd), 'code', WT, ALLOW), cmd)
        self.assertIsNotNone(guard.evaluate('Bash', bash('rm -f /c/other/a.py'), 'code', WT, ALLOW))

    @unittest.skipIf(os.name == 'nt', 'POSIX absolute paths only')
    def test_writing_shell_commands_outside_the_posix_worktree(self):
        self.assertIsNotNone(guard.evaluate('Bash', bash(f'cp a.py {OTHER}'), 'code', WT, ALLOW))
        self.assertIsNotNone(guard.evaluate('PowerShell', bash(f'Set-Content -Path {OTHER} -Value 1'),
                                            'code', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash(f'cat {OTHER}'), 'code', WT, ALLOW))


class HookProtocol(unittest.TestCase):
    def run_main(self, payload, env):
        out = io.StringIO()
        with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(out):
            rc = guard.main(io.StringIO(json.dumps(payload)))
        return rc, out.getvalue()

    def test_silent_outside_a_twinlane_job(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TWINLANE_ENGINE_JOB', None)
            out = io.StringIO()
            with redirect_stdout(out):
                rc = guard.main(io.StringIO(json.dumps({'tool_name': 'Bash',
                                                        'tool_input': {'command': 'git push'}})))
        self.assertEqual((rc, out.getvalue()), (0, ''))

    def test_deny_json_inside_a_job(self):
        rc, out = self.run_main({'tool_name': 'Bash', 'tool_input': {'command': 'git -C . push'}},
                                {'TWINLANE_ENGINE_JOB': 'j1', 'TWINLANE_ENGINE_PROFILE': 'code', 'TWINLANE_ENGINE_CWD': WT})
        self.assertEqual(rc, 0)
        h = json.loads(out)['hookSpecificOutput']
        self.assertEqual((h['hookEventName'], h['permissionDecision']), ('PreToolUse', 'deny'))
        self.assertIn('git push', h['permissionDecisionReason'])

    def test_allowed_call_prints_nothing(self):
        rc, out = self.run_main({'tool_name': 'Bash', 'tool_input': {'command': 'git status'}},
                                {'TWINLANE_ENGINE_JOB': 'j1', 'TWINLANE_ENGINE_PROFILE': 'intel'})
        self.assertEqual((rc, out), (0, ''))

    def test_garbage_input_fails_open(self):
        with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'j1'}):
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(guard.main(io.StringIO('not json')), 0)
        self.assertEqual(out.getvalue(), '')


if __name__ == '__main__':
    unittest.main()
