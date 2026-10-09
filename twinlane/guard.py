"""PreToolUse guard for twinlane workers, plus the matcher the runner reuses after a job.

`twinlane guard` runs as a Claude Code PreToolUse hook that `twinlane run` passes to its own workers via
--settings. It acts ONLY when TWINLANE_ENGINE_JOB is set (a twinlane worker); anywhere else it is silent.
Permission rules alone are prefix matches that `git -C . commit`, a full binary path or PowerShell
get around, and allow rules inherited from project settings can widen the intel profile. This hook
parses the command instead:
  - every profile: no git commit/push/reset --hard/clean/rebase/merge/worktree remove/branch -D
    (git global options are skipped to find the subcommand), no nested engine launch;
  - intel: only the read-only tools and the read-only Bash forms of policy.claude.intel_allow;
  - code: Edit/Write/MultiEdit/NotebookEdit only inside the job cwd, and shell commands that write
    (cp, mv, rm, Set-Content, > ...) may not name an absolute path outside it (a heuristic).
Fail-open: any internal error allows the call (the --disallowedTools rules still apply).
"""
from __future__ import annotations

import json
import os
import re
import sys

from twinlane import common

READ_ONLY_TOOLS = {'Read', 'Glob', 'Grep', 'Skill', 'WebSearch', 'WebFetch', 'TodoWrite',
                   'ToolSearch', 'LS', 'NotebookRead', 'TaskOutput', 'BashOutput'}
ALWAYS_READ_ONLY = {'ToolSearch', 'LS', 'NotebookRead', 'TaskOutput', 'BashOutput'}  # harness plumbing
FILE_WRITE_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit'}
SHELL_TOOLS = {'Bash', 'PowerShell'}
# git global options that take a separate value (`git -C dir status`).
GIT_OPTS_WITH_VALUE = {'-C', '-c', '--git-dir', '--work-tree', '--namespace', '--exec-path', '--super-prefix',
                       '--config-env', '--list-cmds'}
DENY_GIT = {'commit', 'push', 'clean', 'rebase', 'merge', 'cherry-pick', 'am', 'filter-branch', 'update-ref'}
ENGINE_WORDS = {'claude', 'codex', 'claude.exe', 'codex.exe', 'claude.cmd', 'codex.cmd'}
# Flags that make an otherwise read-only command write a file or run a program.
WRITE_FLAGS = re.compile(r'(^|\s)(--output(=|\s|$)|--ext-diff|--pre(=|\s|$))')
WRITE_VERBS = re.compile(r'(^|[\s;&|(])(cp|mv|rm|rmdir|del|erase|copy|move|ren|rename|robocopy|xcopy|mkdir|md|touch|'
                         r'tee|truncate|ln|install|sed\s+-i|perl\s+-i|copy-item|move-item|remove-item|'
                         r'set-content|add-content|out-file|new-item|rename-item|clear-content)(?=\s|$)', re.I)
ABS_PATH = re.compile(r'(?<![\w/])([A-Za-z]:[\\/][^\s"\'|;&<>]*|/[a-zA-Z]/[^\s"\'|;&<>]*|'
                      r'\\' r'\\' r'[^\s"\'|;&<>]+)')
SEGMENT_SPLIT = re.compile(r'&&|\|\||[;|&\r\n]')  # a lone & (background) starts a new command too


# ---------------------------------------------------------------- command parsing

def segments(command: str) -> list[str]:
    return [s.strip() for s in SEGMENT_SPLIT.split(command or '') if s.strip()]


def words(segment: str) -> list[str]:
    """Shell-ish split that keeps Windows backslashes (shlex would eat them)."""
    return [w.strip('"\'') for w in re.findall(r'"[^"]*"|\'[^\']*\'|\S+', segment)]


def _base(word: str) -> str:
    return os.path.basename(word.replace('\\', '/')).lower()


def git_subcommand(ws: list[str]) -> tuple[str | None, list[str]]:
    """('commit', [args]) for `git -C x -c k=v commit -m y`; (None, []) when not a git call."""
    if not ws or _base(ws[0]) not in ('git', 'git.exe'):
        return None, []
    i = 1
    while i < len(ws):
        w = ws[i]
        if w in GIT_OPTS_WITH_VALUE:
            i += 2
            continue
        if w.startswith('-'):
            i += 1
            continue
        return w.lower(), ws[i + 1:]
    return None, []


def _strip_env_prefix(ws: list[str]) -> list[str]:
    """Drop leading VAR=value assignments and wrappers like `env`, `command`, `cmd /c`, `&`."""
    while ws and (re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', ws[0]) or _base(ws[0]) in ('env', 'command', 'exec', '&',
                                                                                     'nohup', 'time', 'start')):
        ws = ws[1:]
    if len(ws) >= 2 and _base(ws[0]) in ('cmd', 'cmd.exe') and ws[1].lower() in ('/c', '//c', '/k'):
        return _strip_env_prefix(words(' '.join(ws[2:])))
    if ws and _base(ws[0]) in ('bash', 'sh', 'bash.exe', 'powershell', 'powershell.exe', 'pwsh', 'pwsh.exe'):
        rest = [w for w in ws[1:] if not w.startswith('-')]
        return _strip_env_prefix(words(' '.join(rest))) if rest else ws
    return ws


def shell_hazard(command: str) -> str | None:
    """A reason when a shell command does something no twinlane worker may do, else None."""
    text = command or ''
    low = text.lower()
    if re.search(r'twinlane[\\/]cli\.py["\']?\s+(run|_job)\b', low):
        return 'nested twinlane run is not allowed in a twinlane job'
    for seg in segments(text):
        ws = _strip_env_prefix(words(seg))
        if not ws:
            continue
        head = _base(ws[0])
        if head in ENGINE_WORDS or (head in ('twinlane', 'twinlane.cmd') and len(ws) > 1 and ws[1] in ('run', '_job')):
            return f'nested engine launch ({head}) is not allowed in a twinlane job'
        sub, args = git_subcommand(ws)
        if sub is None:
            continue
        flat = [a.lower() for a in args]
        if sub in DENY_GIT:
            return f'git {sub} is not allowed in a twinlane job'
        if sub == 'reset' and ('--hard' in flat or '--merge' in flat or '--keep' in flat):
            return 'git reset --hard is not allowed in a twinlane job'
        if sub == 'worktree' and flat[:1] in (['remove'], ['prune'], ['move']):
            return f'git worktree {flat[0]} is not allowed in a twinlane job'
        if sub == 'branch' and any(a in ('-d', '--delete', '-m', '--move', '-f', '--force') for a in flat):
            return 'deleting or renaming branches is not allowed in a twinlane job'
    return None


# ---------------------------------------------------------------- intel allow-list

def _bash_prefixes(allow: list[str]) -> list[str]:
    out = []
    for rule in allow or []:
        m = re.match(r'^Bash\((.+?)(?::\*)?\)$', rule)
        if m:
            out.append(m.group(1).strip())
    return out


def intel_shell_ok(command: str, allow: list[str]) -> str | None:
    """None when every segment is an allowed read-only form; else the reason."""
    if re.search(r'[<>`]|\$\(', command or ''):
        return 'redirects and command substitution are not read-only'
    if WRITE_FLAGS.search(command or ''):
        return 'output/exec flags (--output, --ext-diff, --pre) can write files or run programs'
    prefixes = _bash_prefixes(allow)
    for seg in segments(command):
        if not any(seg == p or seg.startswith(p + ' ') for p in prefixes):
            return f'not a read-only command in the intel allow-list: {common.clip(seg, 80)}'
    return None


def intel_violation(tool: str, tool_input: dict, allow: list[str]) -> str | None:
    """Reason a tool call breaks the read-only intel profile, else None."""
    if tool == 'Bash':
        return intel_shell_ok(str((tool_input or {}).get('command') or ''), allow)
    if tool in READ_ONLY_TOOLS and (tool in (allow or []) or tool in ALWAYS_READ_ONLY):
        return None
    return f'{tool} is not allowed in the read-only intel profile'


# ---------------------------------------------------------------- code-profile confinement

def _inside(path: str, root: str) -> bool:
    if not path or not root:
        return False
    p = path.replace('\\', '/')
    m = re.match(r'^/([a-zA-Z])/(.*)$', p)
    if m:
        p = f'{m.group(1)}:/{m.group(2)}'
    p = os.path.normcase(os.path.normpath(os.path.join(root, p)))
    r = os.path.normcase(os.path.normpath(root))
    return p == r or p.startswith(r.rstrip('\\/') + os.sep)


def code_violation(tool: str, tool_input: dict, cwd: str | None) -> str | None:
    tool_input = tool_input or {}
    if tool in FILE_WRITE_TOOLS:
        path = tool_input.get('file_path') or tool_input.get('notebook_path') or ''
        if cwd and path and not _inside(path, cwd):
            return f'{tool} outside the job worktree ({common.clip(path, 120)})'
        return None
    if tool in SHELL_TOOLS and cwd:
        command = str(tool_input.get('command') or '')
        for seg in segments(command):
            if not (WRITE_VERBS.search(seg) or '>' in seg):
                continue
            for path in ABS_PATH.findall(seg):
                if not _inside(path, cwd) and not path.lower().startswith(('/dev/null', 'nul')):
                    return f'a writing command names a path outside the job worktree ({common.clip(path, 120)})'
    return None


# ---------------------------------------------------------------- decision

def evaluate(tool: str, tool_input: dict, profile: str, cwd: str | None, allow: list[str]) -> str | None:
    """Deny reason for one tool call inside a twinlane job, or None to let the normal rules decide."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool in ('Agent', 'Task'):
        return 'nested agent launch is not allowed in a twinlane job'
    if tool in SHELL_TOOLS:
        reason = shell_hazard(str(tool_input.get('command') or ''))
        if reason:
            return reason
    if profile == 'intel':
        return intel_violation(tool, tool_input, allow)
    return code_violation(tool, tool_input, cwd)


def _deny(reason: str) -> None:
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                             'permissionDecisionReason': f'[twinlane guard] {reason}'}}))


def main(stdin=None) -> int:
    """Always returns 0; a deny is reported through hookSpecificOutput."""
    try:
        if not os.environ.get('TWINLANE_ENGINE_JOB'):
            return 0
        data = json.loads((stdin or sys.stdin).read() or '{}')
        if not isinstance(data, dict):
            return 0
        profile = os.environ.get('TWINLANE_ENGINE_PROFILE') or 'intel'
        cwd = os.environ.get('TWINLANE_ENGINE_CWD') or data.get('cwd')
        allow = (common.load_policy().get('claude') or {}).get('intel_allow') or []
        reason = evaluate(str(data.get('tool_name') or ''), data.get('tool_input') or {}, profile, cwd, allow)
        if reason:
            _deny(reason)
    except BaseException:  # noqa: BLE001 - fail-open; --disallowedTools still applies
        return 0
    return 0
