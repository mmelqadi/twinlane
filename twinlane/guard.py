"""PreToolUse guard for twinlane workers, plus the matcher the runner reuses after a job.

`twinlane guard` runs as a Claude Code PreToolUse hook that `twinlane run` passes to its own workers via
--settings. It acts ONLY when TWINLANE_ENGINE_JOB is set (a twinlane worker); anywhere else it is silent.
Permission rules alone are prefix matches that `git -C . commit`, a full binary path or PowerShell
get around, and allow rules inherited from project settings can widen the intel profile. This hook
parses the command instead:
  - every profile: no git commit/push/pull/reset/clean/rebase/merge/revert/tag/update-ref/worktree
    add|remove/branch -f|-D, and no git stash except list/show (git global options are skipped to find
    the subcommand; `-c alias.*` and config keys that run programs are refused, also written `-ckey=v`),
    no nested engine launch, no
    outbound sends (curl/wget/Invoke-WebRequest with a body or a non-GET method or to a non-local host,
    mail clients, ssh/scp/sftp/rsync to a host, gh writes including `gh api` with fields, az/aws/gcloud,
    package publishing);
  - words are split as a POSIX shell joins quoted pieces (`-c"code"` is `-c` plus code), and wrappers
    (env, sudo, timeout, xargs, nice, exec, `&`, `.`, ...) are dropped together with their option values;
    `env -S STRING` is read as the command it runs and `env -C DIR` moves that command's cwd;
  - GIT_CONFIG_PARAMETERS and GIT_CONFIG_KEY_<n> (`git -c` through the environment) get the same alias
    and program-key checks, wherever they are set;
  - the same rules apply to code given inline to an interpreter (`python -c`/`-cCODE`, `node -e`,
    `php -r`, `bash -c`, `powershell -Command` or a prefix of it, `cmd /c`, Start-Process -ArgumentList
    as a string or an @() array, a heredoc or here-string), including a subprocess argument list such as
    ['git', 'commit'] and an http.client request other than GET/HEAD; a URL in inline code counts only
    next to an HTTP or socket client call; code piped into an interpreter, encoded commands and a
    decode-then-exec payload are refused because they cannot be read;
  - intel: only the read-only tools and the read-only Bash forms of policy.claude.intel_allow;
  - code: Edit/Write/MultiEdit/NotebookEdit only inside the job cwd, and the write targets of a shell
    command are resolved against the shell's cwd (following `cd`): every path of mv/rm/Set-Content/...,
    only the destination of cp/copy/xcopy/robocopy, redirect targets, curl/wget/git output options,
    open(..., 'w') literals in inline code and GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG* values;
    `..`, `~` and `$VAR`/`%VAR%` count as outside unless they resolve inside.
A shell command over MAX_COMMAND_CHARS characters, or one whose scan exceeds the MAX_WORK budget, is
denied unread; so is inline code whose bounded window (CODE_WINDOW words, the open(/urlopen( argument
window) ends before what decides it. A scan that cannot finish denies; it never allows the unread rest.
This is a heuristic over the command TEXT, not a sandbox: a script file (`python send.py`) is not read,
and an obfuscated command can still pass. The mechanism-level part is that twinlane strips secret-named
variables from a code worker's environment (common.strip_secret_env).
Fail-closed inside a twinlane job: any internal error denies the call with the reason "guard error".
"""
from __future__ import annotations

import bisect
import functools
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
DENY_GIT = {'commit', 'push', 'pull', 'clean', 'rebase', 'merge', 'cherry-pick', 'revert', 'am', 'filter-branch',
            'update-ref', 'symbolic-ref', 'send-email', 'send-pack', 'http-push', 'imap-send', 'replace', 'svn',
            'p4', 'fast-import'}
# `git config` forms that only read; any other form writes a config file (a new alias, hook path or program).
GIT_CONFIG_READ = {'--get', '--get-all', '--get-regexp', '--get-urlmatch', '--get-color', '--get-colorbool',
                   '--list', '-l', 'get', 'list'}
GIT_CONFIG_WRITE = {'--add', '--unset', '--unset-all', '--replace-all', '--rename-section', '--remove-section',
                    '--edit', '-e', 'set', 'unset', 'rename-section', 'remove-section', 'edit'}
GIT_CONFIG_VALUE_OPTS = {'--file', '-f', '--blob', '--type', '--default', '--comment', '--value'}
# Options that name a program git runs (fetch, clone, ls-remote, archive, push).
GIT_PROGRAM_OPTS = re.compile(r'^(--upload-pack|--receive-pack|--exec)(=|$)')
# The only git subcommands a code worker may point at a tree outside its worktree (-C, --work-tree).
GIT_READ = {'status', 'log', 'diff', 'show', 'rev-parse', 'ls-files', 'ls-tree', 'blame', 'grep', 'cat-file',
            'describe', 'shortlog', 'merge-base', 'rev-list', 'name-rev', 'for-each-ref', 'show-ref', 'help',
            'version'}
# `git -c key=value` keys that rename a subcommand or run a program.
GIT_EXEC_CONFIG = re.compile(r'^(alias\..+|pager\..+|core\.(pager|editor|sshcommand|fsmonitor|hookspath|gitproxy)|'
                             r'diff\.external|sequence\.editor|gpg\.program|credential\.helper|'
                             r'protocol\.(.+\.)?allow|uploadpack\..+|remote\..+\.(uploadpack|receivepack|vcs)|'
                             r'.+\.(textconv|command|clean|smudge|process))$', re.I)
ENGINE_WORDS = {'claude', 'codex', 'claude.exe', 'codex.exe', 'claude.cmd', 'codex.cmd'}
# Flags that make an otherwise read-only command write a file or run a program.
WRITE_FLAGS = re.compile(r'(^|\s)(--output(=|\s|$)|--ext-diff|--pre(=|\s|$))')
WRITE_VERBS = re.compile(r'(^|[\s;&|(])(cp|mv|rm|rmdir|del|erase|copy|move|ren|rename|robocopy|xcopy|mkdir|md|touch|'
                         r'tee|truncate|ln|install|sed\s+-i|perl\s+-i|copy-item|move-item|remove-item|'
                         r'set-content|add-content|out-file|new-item|rename-item|clear-content|'
                         r'dd|unzip|tar|7z|rsync|scp|patch|wget|expand-archive)(?=\s|$)', re.I)
# Commands whose every path argument is a write target when they lead a command.
WRITE_VERB_NAMES = {'cp', 'mv', 'rm', 'rmdir', 'rd', 'del', 'erase', 'copy', 'move', 'ren', 'rename', 'robocopy',
                    'xcopy', 'mkdir', 'md', 'touch', 'tee', 'truncate', 'ln', 'install', 'copy-item', 'cpi',
                    'move-item', 'mi', 'remove-item', 'ri', 'set-content', 'add-content', 'ac', 'out-file',
                    'new-item', 'ni', 'rename-item', 'rni', 'clear-content', 'clc', 'tee-object', 'rsync', 'scp'}
# On Windows a bare /X is usually a cmd switch (del /Q, robocopy /MIR), so only Git Bash drive paths (/c/...) count.
_SLASH_ROOT = r'/[a-zA-Z]/[^\s"\'|;&<>]*' if os.name == 'nt' else r'/[^\s"\'|;&<>]*'
ABS_PATH = re.compile(r'(?<![\w/.])([A-Za-z]:[\\/][^\s"\'|;&<>]*|' + _SLASH_ROOT + r'|'
                      r'\\' r'\\' r'[^\s"\'|;&<>]+)')
DOTDOT = re.compile(r'(^|[\\/])\.\.([\\/]|$)')  # a `..` path component
SEGMENT_SPLIT = re.compile(r'&&|\|\||[;|&\r\n]')  # a lone & (background) starts a new command too
REDIRECT = re.compile(r'"[^"]*"|\'[^\']*\'|(?:\d|&|\*)?>>?\|?\s*("[^"]*"|\'[^\']*\'|[^\s;&|<>]+)')
NULL_TARGETS = {'/dev/null', 'nul', 'nul:', '$null', '/dev/stdout', '/dev/stderr'}
VAR_REF = re.compile(r'\$\{?[A-Za-z_]\w*\}?|\$env' r':\w+|%[A-Za-z_]\w*%|\$\(|`', re.I)
CD_WORDS = {'cd', 'chdir', 'pushd', 'popd', 'set-location', 'sl', 'push-location', 'pop-location'}
# Wrappers that run the command after them: `sudo git push`, `xargs -n1 rm`, `timeout 5 curl ...`, and
# PowerShell's call operator `&` and dot-sourcing `. git commit`.
WRAPPERS = {'env', 'command', 'exec', 'nohup', 'time', 'start', 'sudo', 'nice', 'ionice', 'stdbuf', 'builtin',
            'call', 'xargs', 'timeout', 'watch', '&', '@', '.'}
# Wrapper options that take a value (`env -u NAME git ...`): the value is never the program. Spelled
# separately, attached (`-uNAME`, `-n1`) or as `--opt=value`.
WRAPPER_VALUE_OPTS = {
    'env': {'-u', '--unset', '-C', '--chdir', '-S', '--split-string'},
    'timeout': {'-s', '--signal', '-k', '--kill-after'},
    'nice': {'-n', '--adjustment'},
    'ionice': {'-c', '--class', '-n', '--classdata', '-p', '--pid'},
    'xargs': {'-I', '-n', '--max-args', '-P', '--max-procs', '-d', '--delimiter', '-a', '--arg-file', '-L',
              '--max-lines', '-s', '--max-chars', '-E'},
    'exec': {'-a'},
    'sudo': {'-u', '--user', '-g', '--group', '-U', '--other-user', '-C', '--close-from', '-D', '--chdir',
             '-p', '--prompt', '-r', '--role', '-t', '--type', '-T', '--command-timeout', '-h', '--host'},
    'stdbuf': {'-i', '--input', '-o', '--output', '-e', '--error'},
    'watch': {'-n', '--interval'},
    'time': {'-f', '--format', '-o', '--output'},
}
MAX_DEPTH = 4
MAX_COMMAND_CHARS = 32 * 1024  # longer shell commands are denied unread, which bounds the parse time
# Work budget per call: one unit per segment and four per (nested) scan, plus one per 256 characters.
# Inline code, string literals and scriptblocks are scanned again, so a pathological command exhausts it
# and is denied as too complex; ordinary commands use well under a tenth of it.
MAX_WORK = 2000
TOO_COMPLEX = 'command too complex to inspect in a twinlane job'
# Inline code: a command word (git, curl, ...) is judged on itself and the words after it, at most this many.
# A command cut off by the window is denied as too complex unless it was judged on what decides it.
CODE_WINDOW = 16
GIT_ARG_RULES = {'branch', 'tag', 'stash', 'worktree', 'checkout', 'switch'}  # git_hazard reads their arguments

# Interpreters. Code interpreters map to the short options that take inline code (a cluster such as
# `-Bc` ends in one) and to their options whose value is a separate word.
SHELLS = {'bash', 'sh', 'zsh', 'dash', 'cmd', 'powershell', 'pwsh'}
CODE_INLINE = {'python': 'c', 'python3': 'c', 'py': 'c', 'node': 'ep', 'php': 'rBRE', 'ruby': 'e', 'perl': 'eE'}
LONG_INLINE = {'--eval', '--print'}
VALUE_OPTS = {'python': {'-W', '-X'}, 'python3': {'-W', '-X'}, 'py': {'-W', '-X'},
              'node': {'-r', '--require', '--import', '--loader'}, 'php': {'-c', '-d', '-z'},
              'ruby': {'-I', '-r', '-E'}, 'perl': {'-I', '-M', '-m'}}
PS_VALUE_PARAMS = ('-executionpolicy', '-windowstyle', '-version', '-outputformat', '-inputformat',
                   '-configurationname', '-workingdirectory', '-psconsolefile')
INTERPRETERS = SHELLS | set(CODE_INLINE)

# Outbound sends.
MAIL_CLIENTS = {'send-mailmessage', 'sendmail', 'mail', 'mailx', 'mutt', 'swaks', 'blat', 'msmtp'}
RAW_NET = {'ssh', 'sftp', 'telnet', 'nc', 'ncat', 'netcat', 'socat', 'ftp', 'tftp', 'plink', 'pscp', 'psftp',
           'start-bitstransfer'}
CLOUD_CLIS = {'az', 'aws', 'gcloud', 'gsutil', 'azcopy'}
HTTP_CLIENTS = {'curl', 'wget', 'invoke-webrequest', 'invoke-restmethod', 'iwr', 'irm'}
PUBLISH = {'npm': 'publish', 'pnpm': 'publish', 'yarn': 'publish', 'bun': 'publish', 'twine': 'upload',
           'docker': 'push', 'podman': 'push', 'gem': 'push', 'cargo': 'publish'}
GH_READ = {'view', 'list', 'diff', 'checks', 'status', 'watch'}
CURL_DATA = ('--data', '--form', '--json', '--upload-file', '-d', '-F', '-T')
CURL_ROUTING = {'-K', '--config', '--connect-to', '--resolve', '-x', '--proxy', '--preproxy', '--socks4',
                '--socks4a', '--socks5', '--socks5-hostname'}
WGET_SEND = ('--post-data', '--post-file', '--body-data', '--body-file', '--method', '-i', '--input-file', '-e',
             '--execute', '--config')
LOOPBACK = re.compile(r'^(localhost|.+\.localhost|127(\.\d{1,3}){3}|\[?::1\]?)$', re.I)
NET_HEADS = MAIL_CLIENTS | RAW_NET | CLOUD_CLIS | HTTP_CLIENTS | {'scp', 'rsync', 'gh'} | set(PUBLISH)

# Inline code: library calls that send, decode-then-exec payloads, process launches and file writes.
NET_CODE = re.compile(r'\b(smtplib|SMTP_SSL|Net::SMTP|PHPMailer|nodemailer|SmtpClient|send_?mail\s*\(|mail\s*\('
                      r'|requests\.(post|put|patch|delete|request)|httpx\.(post|put|patch|delete|request)'
                      r'|CURLOPT_(POST|CUSTOMREQUEST|POSTFIELDS|UPLOAD)'
                      r'|Net::HTTP::(Post|Put|Patch|Delete)|paramiko|ftplib|telnetlib'
                      r'|Net\.WebClient|Invoke-(WebRequest|RestMethod))'
                      r'|\.Upload(String|File|Data|Values)(Async)?\s*\('
                      r'|method[\'"]?\s*(=>|:|=)\s*[\'"]?(post|put|patch|delete)\b', re.I)
DECODE_CODE = re.compile(r'b64decode|frombase64string|base64_decode|atob\s*\(|unhexlify|fromhex|codecs\.decode'
                         r'|(\x5cx[0-9a-f]{2}){4}|(chr\(\d+\)\s*[+.]\s*){3}', re.I)
# Code that starts a process. An underscore counts as a separator (create_subprocess_exec, posix_spawn).
EXEC_CODE = re.compile(r'(?<![A-Za-z])(system|popen|spawn\w*|subprocess|exec\w*|eval|shell_exec|passthru|'
                       r'proc_open|child_process|Process\.Start|Start-Process|Invoke-Expression|iex|'
                       r'Runtime\.getRuntime)(?![A-Za-z])|`', re.I)
# (open(..., 'w') and open(..., mode='w') are read by _opens_to_write.)
WRITE_CODE = re.compile(r'write_text|write_bytes|shutil\.|'
                        r'os\.(remove|unlink|rename|replace|makedirs|mkdir|rmdir|link|symlink)|'
                        r'\.(unlink|mkdir|rename|replace|touch|rmdir)\s*\(|file_put_contents|fopen\s*\(|'
                        r'\b(unlink|rename|copy|mkdir|rmdir|symlink)\s*\(|writeFile|appendFile|\bfs\.\w+|'
                        r'File\.(write|open|delete|rename)|FileUtils|IO\.write|copyFile|cpSync|rmSync|'
                        r'Set-Content|Out-File|Copy-Item|Move-Item|Remove-Item|New-Item', re.I)
URL_LITERAL = re.compile(r'\b(?!file:)[A-Za-z][A-Za-z0-9+.-]{0,30}://([^/\s\'"?#]+)')
# An HTTP or socket client call: only then does a URL literal in inline code count as a request.
NET_CLIENT_CODE = re.compile(r'urlopen|urllib|requests\.|httpx|aiohttp|http\.client|HTTPS?Connection|socket'
                             r'|\bfetch\s*\(|XMLHttpRequest|axios|\bhttps?\.(get|request)\b|WebSocket'
                             r'|file_get_contents|fopen\s*\(|fsockopen|stream_socket_client|curl_'
                             r'|Net::HTTP|LWP|HTTP::Tiny|open-uri|URI\.open|WebClient|HttpClient|WebRequest'
                             r'|Invoke-(WebRequest|RestMethod)|\b(iwr|irm)\b', re.I)
# http.client: the connection's host and the method of each .request(...).
HTTP_CONN_HOST = re.compile(r'HTTPS?Connection\s*\(\s*[\'"]([^\'"]+)')
HTTP_REQUEST_METHOD = re.compile(r'\.request\s*\(\s*([^,)]{0,60})')
# A string literal that is one word without shell syntax: not worth scanning as a command of its own.
PLAIN_WORD = re.compile(r'^[\w./:@%+=,-]*$')


# ---------------------------------------------------------------- command parsing

def segments(command: str) -> list[str]:
    return [s.strip() for s in SEGMENT_SPLIT.split(command or '') if s.strip()]


# One token of a command line: a double-quoted string (a `\` or backtick escapes the next character; an
# unclosed one runs to the end), a single-quoted string, an operator, or plain text.
CMD_TOKEN = re.compile(r'"(?:[^"\x5c`]|[\x5c`][\s\S])*(?:"|[\x5c`]?\Z)|\'[^\']*(?:\'|\Z)|&&|\|\||[;|&\r\n]'
                       r'|[^"\';|&\r\n]+')
CMD_OPS = {'&&', '||', ';', '|', '&', '\r', '\n'}


def commands(command: str) -> list[tuple[str, str]]:
    """Quote-aware split into (operator before, command) pairs: a `;` or `|` inside quotes does not split,
    and neither does the `&` of `2>&1`. `segments` (quote-blind) is run as well, so an unbalanced quote
    cannot hide a command from both."""
    text, out, buf, op = command or '', [], [], ''
    for m in CMD_TOKEN.finditer(text):
        tok = m.group(0)
        if tok not in CMD_OPS or (tok == '&' and (text[m.start() - 1:m.start()] == '>' or
                                                  text[m.end():m.end() + 1] == '>')):
            buf.append(tok)
            continue
        seg = ''.join(buf).strip()
        if seg:
            out.append((op, seg))
        buf, op = [], tok
    seg = ''.join(buf).strip()
    if seg:
        out.append((op, seg))
    return out


HEREDOC = re.compile(r'(?<!<)<<(?!<)(-?)[ \t]*([\'"]?)([A-Za-z_][\w.-]*)\2')


def heredocs(command: str) -> tuple[str, list[str]]:
    """Cut heredoc bodies out: (command with `<<HEREDOC#n` markers, bodies). A body runs to its delimiter
    line; an opener without one is left in place (so its lines are still checked as commands)."""
    lines, out, bodies, i = (command or '').split('\n'), [], [], 0
    at = {}  # stripped line -> the indexes of the lines that hold it, ascending
    if '<<' in (command or ''):
        for n, cand in enumerate(lines):
            at.setdefault(cand.strip(), []).append(n)
    while i < len(lines):
        line, pos = lines[i], 0
        i += 1
        while True:
            m = HEREDOC.search(line, pos)
            if not m:
                break
            idx = at.get(m.group(3), [])
            k = bisect.bisect_left(idx, i)
            if k == len(idx):
                break
            j = idx[k]
            bodies.append('\n'.join(lines[i:j]))
            marker = f'<<HEREDOC#{len(bodies) - 1}'
            line = line[:m.start()] + marker + line[m.end():]
            pos, i = m.start() + len(marker), j + 1
        out.append(line)
    return '\n'.join(out), bodies


WORD = re.compile(r'(?:[^\s"\']|"[^"]*"|\'[^\']*\'|["\'])+')
QUOTED = re.compile(r'"([^"]*)"|\'([^\']*)\'')


def words(segment: str) -> list[str]:
    """Shell-ish split that keeps Windows backslashes (shlex would eat them). Quoted pieces next to each
    other or to bare text join into one word, as in a POSIX shell: `-c"code"` is `-ccode` and `g"it"` is
    `git`. A lone unbalanced quote stays part of its word."""
    return [QUOTED.sub(_unquote, w).strip('"\'') if '"' in w or "'" in w else w
            for w in WORD.findall(segment or '')]


def _unquote(m: re.Match) -> str:
    return m.group(1) if m.group(1) is not None else m.group(2)


@functools.lru_cache(maxsize=1024)
def _base(word: str) -> str:
    if '/' not in word and '\\' not in word and ':' not in word:
        return word.lower()
    return os.path.basename(word.replace('\\', '/')).lower()


@functools.lru_cache(maxsize=1024)
def _cmd_name(word: str) -> str:
    """A full path such as `.../Git/cmd/git.exe` -> `git`, `python3.12` -> `python`."""
    name = _base(word)
    for ext in ('.exe', '.cmd', '.bat', '.com'):
        if name.endswith(ext):
            name = name[:-len(ext)]
            break
    m = re.match(r'^(python|ruby|perl|php|node)w?[\d.]*$', name)
    return m.group(1) if m else name


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


def _drop_wrappers(ws: list[str], chdirs: list | None = None, split: list | None = None) -> list[str]:
    """The command a segment really runs: no VAR=value or `$x =` assignments, no sudo/xargs/timeout/...
    wrappers (nor their options and counts), no heredoc markers. `env -S STRING` is split into words and
    read as env's own arguments (and its words appended to `split`, so assignments inside it are checked like
    any other); the directories of `env -C DIR` and `sudo -D DIR` are appended to chdirs."""
    ws, i = [w for w in ws if w and not w.startswith('<<HEREDOC#')], 0
    while i < len(ws):
        if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', ws[i]):
            i += 1
        elif re.match(r'^\$[\w:]+$', ws[i]) and i + 1 < len(ws) and ws[i + 1] in ('=', '+=', '-='):
            i += 2
        elif re.match(r'^\$[\w:]+=.', ws[i]):
            ws[i] = ws[i].split('=', 1)[1]
        elif _cmd_name(ws[i]) in WRAPPERS:
            name = _cmd_name(ws[i])
            vopts = WRAPPER_VALUE_OPTS.get(name, set())
            i += 1
            while i < len(ws):
                w = ws[i]
                if w == '--':
                    i += 1
                    break
                if w.startswith('-'):
                    opt, value, i = _wrapper_opt(ws, i, vopts)
                    if name == 'env' and opt in ('-S', '--split-string'):
                        ws[i:i] = words(value)
                        if split is not None:
                            split.extend(words(value))
                    elif name in ('env', 'sudo') and opt in ('-C' if name == 'env' else '-D', '--chdir') \
                            and chdirs is not None:
                        chdirs.append(value)
                elif re.match(r'^\d+[smhd]?$', w):  # timeout 5, watch 2
                    i += 1
                else:
                    break
        else:
            break
    return ws[i:]


def _wrapper_opt(ws: list[str], i: int, vopts: set) -> tuple[str, str, int]:
    """(option, its value or '', index after it) for the wrapper option ws[i]. `--unset NAME` takes the
    next word and `--unset=NAME` does not; a short cluster takes the rest of the word after its first value
    option (`-uNAME`), or the next word when the value option ends it (`-iu NAME`)."""
    w, nxt = ws[i], ws[i + 1] if i + 1 < len(ws) else ''
    if w.startswith('--'):
        key, eq, value = w.partition('=')
        if eq:
            return key, value, i + 1
        return (w, nxt, i + 2) if w in vopts else (w, '', i + 1)
    for j in range(1, len(w)):
        if '-' + w[j] in vopts:
            return ('-' + w[j], w[j + 1:], i + 1) if w[j + 1:] else ('-' + w[j], nxt, i + 2)
    return w, '', i + 1


# ---------------------------------------------------------------- git and outbound rules

def _git_config_writes(args: list[str]) -> bool:
    """True unless `git config ARGS` only reads: a read form, or a lone key with no value."""
    flat = [a.lower() for a in args]
    if any(a in GIT_CONFIG_WRITE for a in flat):
        return True
    if any(a in GIT_CONFIG_READ for a in flat):
        return False
    positional, i = [], 0
    while i < len(args):
        if args[i] in GIT_CONFIG_VALUE_OPTS:
            i += 2
            continue
        if not args[i].startswith('-'):
            positional.append(args[i])
        i += 1
    return len(positional) != 1


def git_hazard(ws: list[str]) -> str | None:
    """A reason when a git call writes history, refs or another worktree's state, else None."""
    if not ws or _cmd_name(ws[0]) != 'git':
        return None
    for i, w in enumerate(ws[1:], 1):
        if w in ('-c', '--config-env') and i + 1 < len(ws):
            value = ws[i + 1]
        elif w.startswith('--config-env='):
            value = w.split('=', 1)[1]
        elif w.startswith('-c') and len(w) > 2:  # compact `-calias.x=commit`
            value = w[2:]
        else:
            continue
        if GIT_EXEC_CONFIG.match(value.split('=', 1)[0]):
            return f'git -c {common.clip(value.split("=", 1)[0], 60)} (an alias or a program) is not allowed in a twinlane job'
    sub, args = git_subcommand(ws)
    if sub is None:
        return None
    flat = [a.lower() for a in args]
    if sub in DENY_GIT:
        return f'git {sub} is not allowed in a twinlane job'
    program = next((a for a in args if GIT_PROGRAM_OPTS.match(a)), None)
    if program or (sub in ('clone', 'fetch', 'ls-remote', 'archive') and '-u' in args):
        return f'git {sub} {common.clip(program or "-u", 20)} runs a program; not allowed in a twinlane job'
    if sub == 'config' and _git_config_writes(args):
        return 'git config writes a config file; not allowed in a twinlane job (git config --get/--list are)'
    if sub == 'notes' and flat[:1] not in ([], ['list'], ['show']):
        return 'git notes writes shared refs; not allowed in a twinlane job (git notes list/show are)'
    if sub == 'submodule' and 'foreach' in flat:
        return 'git submodule foreach runs a command in every submodule; not allowed in a twinlane job'
    if sub == 'reset':
        hard = any(a in ('--hard', '--merge', '--keep') for a in flat)
        return 'git reset --hard is not allowed in a twinlane job' if hard else \
            'git reset moves HEAD or the index; not allowed in a twinlane job (use git restore --staged)'
    if sub == 'stash':  # the stash stack is shared by every worktree of the repo
        action = next((a for a in flat if not a.startswith('-')), 'push')
        if action not in ('list', 'show'):
            return f'git stash {common.clip(action, 20)} changes the shared stash; not allowed in a twinlane job ' \
                   '(stash list/show are)'
    if sub == 'worktree' and flat[:1] in (['remove'], ['prune'], ['move'], ['add']):
        return f'git worktree {flat[0]} is not allowed in a twinlane job'
    if sub == 'branch' and any(a in ('-d', '--delete', '-m', '--move', '-f', '--force', '-c', '--copy')
                               for a in flat):
        return 'deleting, forcing or renaming branches is not allowed in a twinlane job'
    if sub in ('checkout', 'switch') and any(a in ('-B', '-C', '--force-create') for a in args):
        return f'git {sub} {next(a for a in args if a in ("-B", "-C", "--force-create"))} resets a branch; ' \
               'not allowed in a twinlane job'
    if sub == 'tag' and flat and not ({'-l', '--list'} & set(flat) and
                                      not {'-d', '--delete', '-a', '-s', '-f', '--force', '-m'} & set(flat)):
        return 'git tag is not allowed in a twinlane job (git tag --list is)'
    return None


def _host(url: str) -> str:
    m = re.match(r'^(?:[a-z][a-z0-9+.-]*://)?(?:[^@/?#\s]*@)?(\[[^\]]*\]|[^:/?#\s]+)', url or '', re.I)
    return m.group(1).lower() if m else ''


def _local(url: str) -> bool:
    return bool(LOOPBACK.match(_host(url)))


def _http_reason(head: str, args: list[str]) -> str | None:
    """curl/wget (also as PowerShell aliases) and Invoke-WebRequest/Invoke-RestMethod: only a GET to
    localhost. A body, a non-GET method, a routing option or a non-local host is refused (a GET can
    send through its query string)."""
    urls, ps_uri = [], False
    for i, a in enumerate(args):
        low, nxt = a.lower(), (args[i + 1] if i + 1 < len(args) else '')
        # PowerShell parameters: Invoke-WebRequest, and curl/wget as its Windows PowerShell aliases.
        if low.startswith('-') and len(low) >= 3 and 'method'.startswith(low[1:]):
            if nxt.lower() not in ('get', 'head'):
                return f'{head} -Method {common.clip(nxt, 20)} sends data; not allowed in a twinlane job'
            continue
        if any(len(low) >= 3 and p.startswith(low[1:]) for p in ('body', 'infile', 'form')) and low[:1] == '-' \
                and low not in ('-f', '-fo'):
            return f'{head} {a} sends data; not allowed in a twinlane job'
        if low.startswith('-') and len(low) >= 3 and 'uri'.startswith(low[1:]):
            urls.append(nxt)
            ps_uri = True
            continue
        if head == 'curl':
            if a in ('-X', '--request') or a.startswith(('-X', '--request=')):
                method = nxt if a in ('-X', '--request') else a.split('=', 1)[1] if '=' in a else a[2:]
                if method.lower() not in ('get', 'head'):
                    return f'curl -X {common.clip(method, 20)} sends data; not allowed in a twinlane job'
            if a.startswith(CURL_DATA) or (re.match(r'^-[a-zA-Z]{1,4}$', a) and re.search(r'[dFT]', a)):
                return f'curl {common.clip(a, 30)} sends data; not allowed in a twinlane job'
            if a.split('=', 1)[0] in CURL_ROUTING or a.startswith('--socks'):
                return f'curl {a.split("=", 1)[0]} can redirect or hide the request; not allowed in a twinlane job'
        if head == 'wget' and low.startswith(WGET_SEND):
            if not (low.startswith('--method') and (low.split('=', 1)[1:] or [nxt.lower()])[0] in ('get', 'head')):
                return f'wget {common.clip(a, 30)} can send data; not allowed in a twinlane job'
        if '://' in a:
            urls.append(a)
    if not urls and not ps_uri:
        urls = [a for a in args if not a.startswith('-') and
                (re.match(r'^[\w-]+(\.[\w-]+)+(:\d+)?(/|$)', a) or re.match(r'^localhost(:\d+)?(/|$)', a, re.I))]
    for url in urls:
        if not _local(url):
            return f'{head} to a non-local host ({common.clip(_host(url), 60)}) is not allowed in a twinlane job; ' \
                   'use WebFetch to read the web'
    return None


def net_hazard(ws: list[str]) -> str | None:
    """A reason when a command sends something off this machine, else None."""
    if not ws:
        return None
    head, args = _cmd_name(ws[0]), ws[1:]
    low = [a.lower() for a in args] if head in PUBLISH or head == 'gh' else []
    if head in MAIL_CLIENTS:
        return f'sending mail ({head}) is not allowed in a twinlane job'
    if head in RAW_NET:
        return f'remote connections ({head}) are not allowed in a twinlane job'
    if head in CLOUD_CLIS:
        return f'cloud CLIs ({head}) are not allowed in a twinlane job'
    if head in PUBLISH and PUBLISH[head] in low:
        return f'{head} {PUBLISH[head]} is not allowed in a twinlane job'
    if head in ('scp', 'rsync') and any(re.match(r'^(?:[^@\s/\x5c]+@)?[\w.-]{2,}:', a) for a in args
                                        if not a.startswith('-')):
        return f'{head} to a remote host is not allowed in a twinlane job'
    if head == 'gh':
        pos = [a for a in low if not a.startswith('-')]
        if not pos or pos[0] in ('help', 'status', 'search', 'version'):
            return None
        if pos[0] == 'api':
            for i, a in enumerate(low):
                # -f/-F (lowered to -f) also attached as `-ftitle=x`
                if a.startswith('-f') or a in ('--field', '--raw-field', '--input') or \
                        a.startswith(('--field=', '--raw-field=', '--input=')):
                    return 'gh api with fields sends a write request; not allowed in a twinlane job'
                method = (low[i + 1] if i + 1 < len(low) else '') if a in ('-x', '--method') else \
                    a[len('--method='):] if a.startswith('--method=') else a[2:] if a.startswith('-x') else None
                if method is not None and method not in ('get', 'head'):
                    return f'gh api --method {common.clip(method, 20)} is not allowed in a twinlane job'
            return None
        if len(pos) >= 2 and pos[1] in GH_READ:
            return None
        return f'gh {" ".join(pos[:2])} is not allowed in a twinlane job (only view/list/diff/checks/status)'
    if head in HTTP_CLIENTS:
        return _http_reason(head, args)
    return None


def head_hazard(ws: list[str]) -> str | None:
    """Rules on the command a segment runs: engines, git writes, outbound sends."""
    if not ws:
        return None
    head = _base(ws[0])
    if head in ENGINE_WORDS or (head in ('twinlane', 'twinlane.cmd') and len(ws) > 1 and ws[1] in ('run', '_job')):
        return f'nested engine launch ({head}) is not allowed in a twinlane job'
    return git_hazard(ws) or net_hazard(ws)


def _text_hazard(text: str) -> str | None:
    low = (text or '').lower()
    if re.search(r'twinlane[\\/]cli\.py["\']?\s+(run|_job)\b', low):
        return 'nested twinlane run is not allowed in a twinlane job'
    if re.search(r'new-object\s+(system\.)?net\.(webclient|mail\.|sockets\.)|\[(system\.)?net\.(webclient|mail\.'
                 r'|sockets\.|http\.httpclient|webrequest)', low):
        return '.NET network clients are not allowed in a twinlane job'
    return None


# ---------------------------------------------------------------- interpreters

def _script_arg(ws: list[str]) -> str | None:
    """The script an interpreter runs (`-` for stdin), or None when it reads its program from stdin."""
    head, vopts = _cmd_name(ws[0]), VALUE_OPTS.get(_cmd_name(ws[0]), set())
    i = 1
    while i < len(ws):
        a = ws[i]
        if a in vopts:
            i += 2
            continue
        if a == '-m' or a.lower() in ('-file', '-f') and head in ('powershell', 'pwsh'):
            return ws[i + 1] if i + 1 < len(ws) else None
        if a == '-' or not a.startswith('-'):
            return a
        i += 1
    return None


def inline_code(ws: list[str]) -> tuple[str, str] | None:
    """(kind, text) for code given on the command line: kind 'shell' (bash -c, cmd /c, powershell
    -Command, eval, Start-Process, find -exec), 'code' (python -c, node -e, php -r, ...) or 'opaque'
    (an encoded or run-time-built command). None for a script file, a module or a plain command."""
    if not ws:
        return None
    head, args = _cmd_name(ws[0]), ws[1:]
    if '<<<' in args and args.index('<<<') + 1 < len(args) and head in INTERPRETERS:
        return ('shell' if head in SHELLS else 'code'), args[args.index('<<<') + 1]
    if head == 'cmd':
        for i, a in enumerate(args):
            if a.lower() in ('/c', '//c', '/k', '//k'):
                return 'shell', ' '.join(args[i + 1:])
        return None
    if head in ('powershell', 'pwsh'):
        low = [a.lower() for a in args]
        if any(a in ('-file', '-f') for a in low):
            return None
        rest, i = [], 0
        while i < len(args):
            a = low[i]
            if a in ('-e', '-ec') or (len(a) >= 3 and 'encodedcommand'.startswith(a[1:])):
                return 'opaque', ''
            if a.startswith('-c') and 'command'.startswith(a[1:]):
                return 'shell', ' '.join(args[i + 1:])
            if a.startswith(PS_VALUE_PARAMS) or any(p.startswith(a) and len(a) > 3 for p in PS_VALUE_PARAMS):
                i += 2
                continue
            if not a.startswith('-'):
                rest.append(args[i])
            i += 1
        return ('shell', ' '.join(rest)) if rest else None
    if head in ('bash', 'sh', 'zsh', 'dash'):
        for i, a in enumerate(args):
            if a.startswith('-') and not a.startswith('--') and 'c' in a[1:]:
                return 'shell', next((x for x in args[i + 1:] if not x.startswith('-')), '')
            if not a.startswith('-'):
                return None
        return None
    if head in CODE_INLINE:
        letters, vopts, i = CODE_INLINE[head], VALUE_OPTS.get(head, set()), 0
        while i < len(args):
            a = args[i]
            nxt = args[i + 1] if i + 1 < len(args) else ''
            if a in vopts:
                i += 2
                continue
            if a in LONG_INLINE:
                return 'code', nxt
            if a.startswith(('--eval=', '--print=')):
                return 'code', a.split('=', 1)[1]
            if a == '-' or a == '-m' or not a.startswith('-'):
                return None
            if not a.startswith('--'):
                # A short cluster: `-Bc CODE`, or the code attached as in `-cCODE` / `-Bc"CODE"`. An option
                # that takes a value ends the cluster (`-W ignore`, `-Wignore`); `-m` names a module.
                for j in range(1, len(a)):
                    if a[j] in letters:
                        return 'code', a[j + 1:] or nxt
                    if a[j] == 'm' and head in ('python', 'py'):
                        return None
                    if '-' + a[j] in vopts:
                        if not a[j + 1:]:
                            i += 1  # its value is the next word
                        break
            i += 1
        return None
    if head in ('eval', 'invoke-expression', 'iex'):
        text = ' '.join(args)
        return ('opaque', '') if re.search(r'[$`]', text) else ('shell', text)
    if head in ('start-process', 'saps'):
        low = [a.lower() for a in args]
        prog = next((args[i + 1] for i, a in enumerate(low) if a.startswith('-filep') and i + 1 < len(args)), None)
        prog = prog or next((a for a in args if not a.startswith('-')), '')
        at = next((i + 1 for i, a in enumerate(low) if a.startswith(('-arg', '-a')) and
                   a not in ('-a',) and i + 1 < len(args)), None)
        argl = args[at] if at is not None else ''
        if argl.startswith('@('):  # array form @('commit','-m','x'), possibly split at spaces
            j = at
            while not args[j].endswith(')') and j + 1 < len(args):
                j += 1
                argl += ' ' + args[j]
            argl = argl[2:-1] if argl.endswith(')') else argl[2:]
        if at is not None and ',' in argl:  # 'commit','-m','x' (quotes already removed): one word each
            argl = argl.replace(',', ' ')
        return 'shell', f'{prog} {argl}'.strip()
    if head == 'find':
        for i, a in enumerate(args):
            if a in ('-exec', '-execdir', '-ok', '-okdir'):
                inner = [x for x in args[i + 1:] if x not in ('{}', ';', '\\;', '+', '\\')]
                return 'shell', ' '.join(inner)
    return None


# ---------------------------------------------------------------- paths (code profile)

def _native(path: str) -> str:
    p = path.replace('\\', '/')
    m = re.match(r'^/([a-zA-Z])/(.*)$', p)
    return f'{m.group(1)}:/{m.group(2)}' if m else p


def _inside(path: str, root: str) -> bool:
    if not path or not root:
        return False
    p = os.path.normcase(os.path.normpath(os.path.join(root, _native(path))))
    r = os.path.normcase(os.path.normpath(root))
    return p == r or p.startswith(r.rstrip('\\/') + os.sep)


def _expand(word: str) -> str | None:
    """`~` and $VAR/%VAR% expanded, or None when something stays unresolved."""
    w = (word or '').strip().strip('"\'')
    if w.startswith('~'):
        w = os.path.expanduser(w)
        if w.startswith('~'):
            return None
    if VAR_REF.search(w):
        w = os.path.expandvars(w)
        if VAR_REF.search(w):
            return None
    return w


def _path_candidates(word: str) -> list:
    """Every path a word can name, quotes removed: the word itself, a value after `=` or `:`, a short option's
    attached value (`-d../x`), and every value a flag cluster could carry (`-xvC../x`)."""
    w = (word or '').replace('"', '').replace("'", '')
    if not w.startswith('-'):
        return [w] + ([w.split('=', 1)[1]] if '=' in w else [])  # dd of=../x
    out = [w.split(sep, 1)[1] for sep in '=:' if sep in w]
    if not out and not w.startswith('--'):  # a flag cluster: the value may start after any of its letters
        i = 2
        while i < len(w) and w[i - 1].isalpha():
            if w[i] not in '/\\':
                out.append(w[i:])
            i += 1
    return out


def _outside(word: str, root: str, base: str | None) -> bool:
    """True when a path word resolves outside root. Relative paths resolve against base (the shell's cwd,
    None when a `cd` could not be followed); `..`, `~` and variables count as outside unless they resolve
    inside."""
    raw = (word or '').strip().strip('"\'')
    if not raw or raw.lower() in NULL_TARGETS:
        return False
    w = _expand(raw)
    if w is None:
        return True
    w = _native(w)
    if not os.path.isabs(w) and not re.match(r'^[A-Za-z]:', w):
        if base is None:
            return True
        w = os.path.join(base, w)
    return not _inside(w, root)


def _resolve_dir(word: str, base: str | None) -> str | None:
    w = _expand(word or '~')
    if w is None or w == '-':
        return None
    w = _native(w)
    if base is None and not os.path.isabs(w):
        return None
    new = os.path.normpath(os.path.join(base or '', w))
    return new if len(new) <= 1024 else None  # a cd chain this long is not followed (it would grow without end)


def _redirect_targets(seg: str) -> list[str]:
    return [m.group(1) for m in REDIRECT.finditer(seg or '') if m.group(1)]


def path_hazard(ws: list[str], seg: str, root: str, base: str | None, stack: list,
                raw: list[str] | None = None) -> tuple[str | None, str | None]:
    """(new base, reason): follows cd/pushd/popd, then checks the write targets of one command. raw is
    words(seg) before wrappers were dropped."""
    head = _cmd_name(ws[0]) if ws else ''
    if head in CD_WORDS:
        if head in ('popd', 'pop-location'):
            return (stack.pop() if stack else None), None
        if head in ('pushd', 'push-location'):
            stack.append(base)
        target = next((w for w in ws[1:] if not w.startswith('-') and w.lower() != '/d'), '~')
        return _resolve_dir(target, base), None
    for t in _git_env_targets(words(seg) if raw is None else raw):
        if _outside(t, root, base):
            return base, f'a GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG* value points outside the job ' \
                         f'worktree ({common.clip(t, 120)})'
    targets, sources = _redirect_targets(seg) + _output_targets(ws), []
    if head in WRITE_VERB_NAMES or (head in ('sed', 'perl') and any(w.startswith('-i') for w in ws[1:])):
        written, sources = _verb_targets(head, ws)
        targets += written
    sub, _args = git_subcommand(ws)
    if sub is not None and sub not in GIT_READ:
        for i, w in enumerate(ws[1:], 1):
            if w in ('-C', '--git-dir', '--work-tree') and i + 1 < len(ws):
                targets.append(ws[i + 1])
            elif w.startswith(('--git-dir=', '--work-tree=')):
                targets.append(w.split('=', 1)[1])
    for t in targets:
        if _outside(t, root, base):
            return base, f'a writing command names a path outside the job worktree ({common.clip(t, 120)})'
    writing = '>' in seg or WRITE_VERBS.search(seg)
    for path in ABS_PATH.findall(seg):
        if not _inside(path, root) and not path.lower().startswith(('/dev/null', 'nul')) \
                and not any(path in s for s in sources) and writing:
            return base, f'a writing command names a path outside the job worktree ({common.clip(path, 120)})'
    if writing:  # a command it does not model (unzip -d, tar -C) still names `..` paths: resolve each one
        for w in (words(seg) if raw is None else raw)[1:]:
            for cand in _path_candidates(w):
                if DOTDOT.search(cand) and cand not in sources and _outside(cand, root, base):
                    return base, f'a writing command names a path outside the job worktree ({common.clip(cand, 120)})'
    return base, None


GIT_ENV_NAMES = r'(GIT_DIR|GIT_WORK_TREE|GIT_INDEX_FILE|GIT_CONFIG\w*)'
GIT_ENV_SET = re.compile(r'^(?:\$env:)?' + GIT_ENV_NAMES + r'=(.*)$', re.I)
GIT_ENV_VAR = re.compile(r'^\$env:' + GIT_ENV_NAMES + r'$', re.I)


def _git_env(raw: list[str]) -> list[tuple[str, str]]:
    """(name, value) of each GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE/GIT_CONFIG* set anywhere in a command,
    before wrappers are dropped: `GIT_DIR=x git add`, `env GIT_DIR=x ...`, `export`/`set GIT_DIR=x`,
    `$env:GIT_DIR = 'x'`."""
    out = []
    for i, w in enumerate(raw):
        if 'GIT_' not in w.upper():
            continue
        m = GIT_ENV_SET.match(w)
        if m:
            out.append((m.group(1).upper(), m.group(2)))
        elif GIT_ENV_VAR.match(w) and i + 2 < len(raw) and raw[i + 1] == '=':
            out.append((GIT_ENV_VAR.match(w).group(1).upper(), raw[i + 2]))
    return out


def _git_env_targets(raw: list[str]) -> list[str]:
    """The values of _git_env: they point git at a repository or file, so they are paths to check."""
    return [value for _name, value in _git_env(raw)]


def git_env_config_hazard(raw: list[str]) -> str | None:
    """GIT_CONFIG_PARAMETERS ('key=value' ...) and GIT_CONFIG_KEY_<n> are `git -c` given through the
    environment: the same alias and program keys are refused, wherever the variable is set."""
    for name, value in _git_env(raw):
        if name == 'GIT_CONFIG_PARAMETERS':
            keys = re.findall(r"([A-Za-z][\w.-]*)'?\s*=", value)
        elif re.match(r'^GIT_CONFIG_KEY_\d+$', name):
            keys = [value.strip().strip('"\'')]
        else:
            continue
        key = next((k for k in keys if GIT_EXEC_CONFIG.match(k)), None)
        if key is not None:
            return f'{name} sets {common.clip(key, 60)} (an alias or a program); not allowed in a twinlane job'
    return None


def _output_targets(ws: list[str]) -> list[str]:
    """Files written through an output option: curl/wget `-o`/`-O`/`--output[-document]` (also attached,
    `-o../x`, `--output=../x`), Invoke-WebRequest `-OutFile`, git `--output` and `git archive|format-patch
    -o`."""
    out, head = [], _cmd_name(ws[0]) if ws else ''
    sub = git_subcommand(ws)[0]
    if head not in HTTP_CLIENTS and sub is None:
        return out
    for i, w in enumerate(ws[1:], 1):
        nxt = ws[i + 1] if i + 1 < len(ws) else None
        if head in HTTP_CLIENTS:
            if w in ('-o', '-O', '--output', '--output-document') or w.lower().startswith('-outf'):
                out += [nxt] if nxt is not None else []
            elif w.startswith(('--output=', '--output-document=')):
                out.append(w.split('=', 1)[1])
            elif w.startswith(('-o', '-O')) and not w.startswith('--'):
                out.append(w[2:])
        elif sub is not None:
            short = sub in ('archive', 'format-patch')
            if w == '--output' or (short and w == '-o'):
                out += [nxt] if nxt is not None else []
            elif w.startswith('--output='):
                out.append(w.split('=', 1)[1])
            elif short and w.startswith('-o') and len(w) > 2 and not w.startswith('--'):
                out.append(w[2:])
    return out


COPY_VERBS = {'cp', 'copy', 'xcopy', 'robocopy'}
CMD_SWITCH = re.compile(r'^/[A-Za-z0-9?+-]+(?::(.*))?$')  # /Y, /MIR, /LOG:file
# xcopy and robocopy are programs, so every /X is theirs. copy, del, rd, move and ren are also PowerShell
# aliases (Copy-Item, Remove-Item, ...) where /x is a root path on the current drive: only cmd's own switches of that verb count,
# and none once a PowerShell parameter (-Recurse, -Path) shows the line is PowerShell. On POSIX /x is a path.
CMD_ANY_SWITCH = {'xcopy', 'robocopy'}
CMD_VERB_SWITCHES = {'copy': {'a', 'b', 'd', 'l', 'n', 'v', 'y', '-y', 'z'}}
if os.name == 'nt':
    CMD_VERB_SWITCHES.update({'del': {'p', 'f', 's', 'q', 'a'}, 'erase': {'p', 'f', 's', 'q', 'a'},
                              'rd': {'s', 'q'}, 'rmdir': {'s', 'q'}, 'move': {'y', '-y'}})


def _cmd_switch(head: str, w: str, powershell: bool):
    """The CMD_SWITCH match when `w` is a cmd switch of `head`, else None (then it is a path)."""
    m = CMD_SWITCH.match(w)
    if not m or head in CMD_ANY_SWITCH:
        return m
    known = CMD_VERB_SWITCHES.get(head)
    if known is None or powershell:
        return None
    return m if w[1:].split(':', 1)[0].lower() in known else None
# Copy-Item parameters (cp and copy are its PowerShell aliases): a command using them is read the old way.
PS_COPY_PARAMS = ('path', 'literalpath', 'destination', 'container', 'force', 'filter', 'include', 'exclude',
                  'recurse', 'passthru', 'credential', 'fromsession', 'tosession')


def _verb_targets(head: str, ws: list[str]) -> tuple[list[str], list[str]]:
    """(written paths, read-only sources) of a leading writing verb; option values count as written.
    A copy-type verb writes only its destination: the last path of cp (or its -t directory), every path
    after the first of copy/xcopy/robocopy. mv/move/ren, ln (a link reaches its source) and every other
    verb write all their paths, and so does cp with -s/-l/--link or Copy-Item style parameters."""
    written, pos, tdir, whole, i = [], [], None, head not in COPY_VERBS, 1
    powershell = any(re.match(r'^-[A-Za-z]{2,}', w) for w in ws[1:])
    while i < len(ws):
        w, nxt = ws[i], ws[i + 1] if i + 1 < len(ws) else None
        m = _cmd_switch(head, w, powershell)
        if m:
            written += [m.group(1)] if m.group(1) else []
        elif not w.startswith('-') or w == '-':
            pos.append(w)
        elif '=' in w or re.match(r'^-\w+:.', w):
            key, val = re.split(r'[=:]', w, maxsplit=1)
            if key == '--target-directory':
                tdir = val
            else:
                written.append(val)
        elif w.startswith('--'):
            if w == '--target-directory' and nxt is not None:
                tdir, i = nxt, i + 1
            whole = whole or w in ('--link', '--symbolic-link')
        elif len(w) > 2 and any(p.startswith(w[1:].lower()) for p in PS_COPY_PARAMS):
            whole = True
        elif head == 'cp':  # a short cluster; -t DIR and -S SUFFIX take a value, -s/-l make links
            for j in range(1, len(w)):
                whole = whole or w[j] in 'sl'
                if w[j] in 'tS':
                    val = w[j + 1:]
                    if not val and nxt is not None:
                        val, i = nxt, i + 1
                    if w[j] == 't':
                        tdir = val
                    else:
                        written.append(val)
                    break
        i += 1
    if whole:
        return written + pos + ([tdir] if tdir is not None else []), []
    if head == 'cp':
        return (written + [tdir], pos) if tdir is not None else (written + pos[-1:], pos[:-1])
    return written + pos[1:], pos[:1]


# ---------------------------------------------------------------- scanning

def _code_reason(code: str, ctx: dict, depth: int) -> str | None:
    """Inline interpreter code: git writes and sends named as words or argument lists, network libraries,
    non-local URLs, decode-then-exec payloads, command strings it runs and (code profile) file writes
    outside the worktree."""
    runs = EXEC_CODE.search(code or '')
    # Words only count as a command when the code starts a process: `d['claude']` is not a launch, but
    # subprocess.run(['git', 'commit']) is.
    toks = re.findall(r'[^\s\'"`\[\](){},;=]+', code or '') if runs else []
    for i, t in enumerate(toks):
        name = _cmd_name(t)
        if name == 'git' or name in NET_HEADS or _base(t) in ENGINE_WORDS:
            if not _spend(ctx, '', 4):
                return TOO_COMPLEX
            window = toks[i:i + CODE_WINDOW]  # a command and its first arguments
            reason = head_hazard(window)
            if reason:
                return f'{reason} (inline code)'
            if i + CODE_WINDOW < len(toks) and not _window_judged(window):
                return TOO_COMPLEX  # the window ends before the arguments that decide
    m = NET_CODE.search(code or '')
    data = None
    for args in _call_args(code or '', r'urlopen\('):
        if args is None:
            return TOO_COMPLEX
        data = data or re.search(r'\bdata\s*=', args)
    if m or data:
        what = m.group(0) if m else 'urlopen(data=)'
        return f'inline code uses a network or mail client ({common.clip(what, 40)}); not allowed in a twinlane job'
    reason = _http_client_send(code or '') or _url_send(code or '')
    if reason:
        return reason
    if runs and DECODE_CODE.search(code or ''):
        return 'inline code decodes and runs a payload that cannot be inspected; not allowed in a twinlane job'
    literals = [a or b for a, b in re.findall(r"'([^'\n]*)'|\"([^\"\n]*)\"", code or '')]
    if runs:
        for lit in literals:
            if PLAIN_WORD.match(lit):  # one word, no shell syntax: the word checks above already saw it
                continue
            reason = _scan(lit, ctx, depth + 1)
            if reason:
                return reason
    if ctx['profile'] == 'code' and ctx['root']:
        pathish = [lit for lit in literals if '://' not in lit and
                   ('/' in lit or '\\' in lit or lit.startswith(('~', '..')))]
        # A path literal of 400+ characters was skipped, so a padded `./././../out` was never judged. It is
        # not resolved (that keeps the scan bounded); it counts as a possible outside target instead.
        outside = next((lit for lit in pathish if len(lit) >= 400 or _outside(lit, ctx['root'], ctx['base'])),
                       None)
        if outside is not None:
            writes = bool(WRITE_CODE.search(code or '')) or _opens_to_write(code or '')
            if writes is None or (writes and len(outside) >= 400):
                return TOO_COMPLEX
            if writes:
                return f'inline code writes a path outside the job worktree ({common.clip(outside, 120)})'
    return None


def _window_judged(window: list[str]) -> bool:
    """True when a command cut to its first CODE_WINDOW words was judged on what decides it: a git
    subcommand whose verdict does not depend on its arguments. Any other cut command was not."""
    sub = git_subcommand(window)[0] if _cmd_name(window[0]) == 'git' else None
    return sub is not None and sub not in GIT_ARG_RULES


def _call_args(code: str, call: str, window: int = 300):
    """The argument text of each call matching the regex `call` (such as r'open\\s*\\(') up to its first
    `)`, read at most `window` characters ahead, which keeps a 32 K-character command linear. None for a
    call whose `)` is not within the window while the code goes on: its arguments were not all read.
    Nested parentheses end the arguments early."""
    for m in re.finditer(call, code):
        args = code[m.end():m.end() + window]
        close = args.find(')')
        if close == -1 and m.end() + window < len(code):
            yield None
            return
        yield args if close == -1 else args[:close]


def _opens_to_write(code: str) -> bool | None:
    """open(path, 'w'|'a'|'x'|'+...') positionally or as mode='w'; None when an open( call's arguments
    run past the window, so its mode was not read."""
    for args in _call_args(code, r'open\s*\('):
        if args is None:
            return None
        comma = args.find(',')
        if comma != -1 and re.match(r'\s*[\'"][rbt]*[wax+]', args[comma + 1:]) or \
                'mode' in args and re.search(r'\bmode\s*=\s*[\'"][rbt]*[wax+]', args):
            return True
    return False


def _http_client_send(code: str) -> str | None:
    """Http.client: a connection to a non-local host, or a .request(...) whose method is not a GET/HEAD
    literal, is an outbound send."""
    if not re.search(r'http\.client|HTTPS?Connection', code):
        return None
    for host in HTTP_CONN_HOST.findall(code):
        if not _local(host):
            return f'inline code opens an HTTP connection to a non-local host ({common.clip(_host(host), 60)}); ' \
                   'not allowed in a twinlane job'
    for method in HTTP_REQUEST_METHOD.findall(code):
        if method.strip().strip('\'"').upper() not in ('GET', 'HEAD') or not re.match(r'^\s*[\'"]', method):
            return f'inline code sends an HTTP {common.clip(method.strip(), 20)} request; not allowed in a twinlane job'
    return None


def _url_send(code: str) -> str | None:
    """A non-local URL literal counts only when the code also calls an HTTP or socket client: printing a
    URL sends nothing."""
    host = next((h for h in URL_LITERAL.findall(code) if not _local(h)), None)
    if host is not None and NET_CLIENT_CODE.search(code):
        return f'inline code requests a non-local URL ({common.clip(_host(host), 60)}); not allowed in a twinlane job'
    return None


def _command_reason(ws: list[str], seg: str, op: str, body: str | None, ctx: dict, depth: int) -> str | None:
    head = _cmd_name(ws[0])
    # `$(echo git) push`, `& $exe commit`, %COMSPEC% /c ...: the program is only known at run time. A bare
    # `$LASTEXITCODE` (PowerShell prints it) and the `%` alias of ForEach-Object stay allowed.
    if ws[0][:1] == '`' or (ws[0][:1] in ('$', '%') and ws[0] != '%' and (len(ws) > 1 or op == '&')):
        return f'a command name built at run time ({common.clip(ws[0], 40)}) cannot be inspected in a twinlane job'
    bare = re.sub(r'"[^"]*"|\'[^\']*\'', '""', seg)  # braces inside quotes are data (JSON, awk)
    if '{' in bare:  # a PowerShell scriptblock or a shell group: its body is commands too
        inner = bare.split('{', 1)[1]
        reason = _scan(inner.rsplit('}', 1)[0] if '}' in inner else inner, ctx, depth + 1)
        if reason:
            return reason
    inline = inline_code(ws)
    if inline is None and body is not None and head in INTERPRETERS:
        inline = ('shell' if head in SHELLS else 'code'), body
    if op == '|' and head in INTERPRETERS and (inline is None and _script_arg(ws) in (None, '-')
                                               or inline is not None and inline[1].strip() == '-'):
        return f'code piped into {head} cannot be inspected in a twinlane job'
    if inline is not None:
        kind, text = inline
        if kind == 'opaque':
            return f'{head}: an encoded or run-time-built command cannot be inspected in a twinlane job'
        reason = _scan(text, ctx, depth + 1) if kind == 'shell' else _code_reason(text, ctx, depth + 1)
        if reason:
            return reason
    return head_hazard(ws) or head_hazard(_strip_env_prefix(ws))


def _spend(ctx: dict, text: str, units: int = 1) -> bool:
    """Charge `units` plus one per 256 characters to this call's work budget; False once it is spent."""
    ctx['budget'][0] -= units + len(text) // 256
    return ctx['budget'][0] >= 0


def _scan_split(split: list[tuple[str, str]], bodies: list[str], ctx: dict, depth: int) -> str | None:
    base, stack, memo = ctx['base'], [], ctx['memo']
    for op, seg in split:
        seg = seg.strip().lstrip('({').rstrip(')}').strip()
        m = re.search(r'<<HEREDOC#(\d+)', seg)
        body = bodies[int(m.group(1))] if m and int(m.group(1)) < len(bodies) else None
        # Both splits mostly yield the same segments: a (segment, cwd, depth) is judged once per call, except
        # pushd/popd, whose result depends on the directory stack.
        key = ('seg', op if op in ('|', '&') else '', seg, body, base, depth)
        if key not in memo:
            if not _spend(ctx, seg):
                return TOO_COMPLEX
            raw, chdirs, split = words(seg), [], []
            ws = _drop_wrappers(raw, chdirs, split)
            raw = raw + split  # `env -S "GIT_CONFIG_...=... git ..."` sets the same environment
            # `env -C DIR cmd` (or `sudo -D DIR cmd`) runs cmd in DIR while the shell's redirects stay in base: the path checks run
            # against both, and the shell's cwd does not change.
            cmd_base = base
            for d in chdirs:
                cmd_base = _resolve_dir(d, cmd_base)
            reason, new_base = git_env_config_hazard(raw), base
            if ws and not reason:
                reason = _command_reason(ws, seg, op, body, dict(ctx, base=cmd_base), depth)
                if not reason and ctx['profile'] == 'code' and ctx['root']:
                    new_base, reason = path_hazard(ws, seg, ctx['root'], base, list(stack) if chdirs else stack,
                                                   raw)
                    if chdirs:
                        new_base = base
                        reason = reason or path_hazard(ws, seg, ctx['root'], cmd_base, [], raw)[1]
            if ws and _cmd_name(ws[0]) in ('pushd', 'popd', 'push-location', 'pop-location'):
                base = new_base
                if reason:
                    return reason
                continue
            memo[key] = (reason, new_base)
        reason, base = memo[key]
        if reason:
            return reason
    return None


def _scan(command: str, ctx: dict, depth: int = 0) -> str | None:
    """Every rule on one shell command, recursing into inline code. Both splits run: quote-aware (to read
    `python -c "a; b"` whole) and quote-blind (so an unbalanced quote cannot hide a command)."""
    if depth > MAX_DEPTH:
        return 'commands nested this deeply cannot be inspected in a twinlane job'
    key = ('scan', command, ctx['base'], depth)
    if key in ctx['memo']:
        return ctx['memo'][key]
    if not _spend(ctx, command, 4):  # a scan has a fixed cost of about four segments
        return TOO_COMPLEX
    reason = _text_hazard(command)
    if not reason:
        text, bodies = heredocs(command)
        for split in (commands(text), [('', s) for s in segments(text)]):
            reason = _scan_split(split, bodies, ctx, depth)
            if reason:
                break
    ctx['memo'][key] = reason
    return reason


def _ctx(profile: str | None, root: str | None, base: str | None) -> dict:
    return {'profile': profile, 'root': root, 'base': base, 'memo': {}, 'budget': [MAX_WORK]}


def shell_hazard(command: str) -> str | None:
    """A reason when a shell command does something no twinlane worker may do, else None."""
    return _scan(command or '', _ctx(None, None, None))


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

def code_violation(tool: str, tool_input: dict, cwd: str | None, shell_cwd: str | None = None) -> str | None:
    tool_input = tool_input or {}
    if tool in FILE_WRITE_TOOLS:
        path = tool_input.get('file_path') or tool_input.get('notebook_path') or ''
        if cwd and path and not _inside(path, cwd):
            return f'{tool} outside the job worktree ({common.clip(path, 120)})'
        return None
    if tool in SHELL_TOOLS and cwd:
        return _scan(str(tool_input.get('command') or ''), _ctx('code', cwd, shell_cwd or cwd))
    return None


# ---------------------------------------------------------------- decision

def evaluate(tool: str, tool_input: dict, profile: str, cwd: str | None, allow: list[str],
             shell_cwd: str | None = None) -> str | None:
    """Deny reason for one tool call inside a twinlane job, or None to let the normal rules decide.
    shell_cwd is the shell's current directory when the hook knows it (relative paths resolve there)."""
    tool_input = tool_input if isinstance(tool_input, dict) else {}
    if tool in ('Agent', 'Task'):
        return 'nested agent launch is not allowed in a twinlane job'
    if tool in SHELL_TOOLS:
        if len(str(tool_input.get('command') or '')) > MAX_COMMAND_CHARS:
            return f'command too long to inspect (over {MAX_COMMAND_CHARS // 1024} KB); not allowed in a twinlane job'
        # The code-profile scan below runs every shell rule too, so a code job with a cwd scans once.
        reason = None if profile == 'code' and cwd else shell_hazard(str(tool_input.get('command') or ''))
        if reason:
            return reason
    if profile == 'intel':
        return intel_violation(tool, tool_input, allow)
    return code_violation(tool, tool_input, cwd, shell_cwd)


def _deny(reason: str) -> None:
    print(json.dumps({'hookSpecificOutput': {'hookEventName': 'PreToolUse', 'permissionDecision': 'deny',
                                             'permissionDecisionReason': f'[twinlane guard] {reason}'}}))


def main(stdin=None) -> int:
    """Always returns 0; a deny is reported through hookSpecificOutput. Inside a twinlane job any internal
    error denies the call ("guard error"); outside one the hook stays silent."""
    in_job = bool(os.environ.get('TWINLANE_ENGINE_JOB'))
    try:
        if not in_job:
            return 0
        data = json.loads((stdin or sys.stdin).read() or '{}')
        if not isinstance(data, dict):
            raise ValueError('hook input is not a JSON object')
        profile = os.environ.get('TWINLANE_ENGINE_PROFILE') or 'intel'
        cwd = os.environ.get('TWINLANE_ENGINE_CWD') or data.get('cwd')
        shell_cwd = data.get('cwd') if isinstance(data.get('cwd'), str) else None
        allow = (common.load_policy().get('claude') or {}).get('intel_allow') or []
        reason = evaluate(str(data.get('tool_name') or ''), data.get('tool_input') or {}, profile, cwd, allow,
                          shell_cwd)
        if reason:
            _deny(reason)
    except BaseException as e:  # noqa: BLE001 - fail closed inside a twinlane job
        if in_job:
            try:
                _deny(f'guard error ({type(e).__name__}); the call is denied')
            except BaseException:  # noqa: BLE001 - stdout gone: nothing more to say
                pass
    return 0
