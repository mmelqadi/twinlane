"""Guard parity with the private predecessor: database clients, configured deny rules, an invalid guard
config, twinlane's own dispatching commands, subagents, and the allow/deny corpora. Pure parsing; no model
calls, no network."""
import io
import json
import os
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
from twinlane import common, guard  # noqa: E402

ALLOW = common.load_policy()['claude']['intel_allow']
WT = ('\x43:\x2fUsers/alice/worktrees/task-a' if os.name == 'nt' else
      os.path.join(tempfile.gettempdir(), 'twinlane-guard-worktree', 'task-a'))
BS = chr(92)
# A project's own rules, as a user config would set them; twinlane ships none of these names.
CONFIG = {'self_aliases': ['tl'],
          'deny_commands': ['release-prod.sh', 'kubectl delete', 'terraform apply'],
          'deny_text': [r'release-prod\.sh', r'ops/rotate_\w+\.py']}


def judge(cmd, tool='Bash', config=None, profile='code'):
    return guard.evaluate(tool, {'command': cmd}, profile, WT, ALLOW, config=CONFIG if config is None else config)


def spellings(prog, args):
    """(label, tool, command): one program and its arguments in every spelling the guard must see through."""
    a = ' '.join(args)
    sq = ' '.join("'%s'" % x for x in args)
    listed = ', '.join(repr(x) for x in [prog] + list(args))
    hexed = "$'" + BS + 'x%02x' % ord(prog[0]) + "'" + prog[1:]
    return [
        ('plain', 'Bash', f'{prog} {a}'),
        ('double quotes', 'Bash', f'"{prog}" {a}'),
        ('split quotes', 'Bash', f"{prog[0]}'{prog[1:]}' {a}"),
        ('ANSI-C', 'Bash', f'{hexed} {a}'),
        ('brace', 'Bash', f'{{{prog},}} {a}'),
        ('env prefix', 'Bash', f'env FOO=1 {prog} {a}'),
        ('sudo prefix', 'Bash', f'sudo -u root {prog} {a}'),
        ('timeout nohup', 'Bash', f'timeout 5 nohup {prog} {a}'),
        ('full path', 'Bash', f'/usr/local/bin/{prog} {a}'),
        ('windows path .exe', 'Bash', f'"\x43:/Program Files/tools/{prog}.exe" {a}'),
        ('.exe', 'Bash', f'{prog}.exe {a}'),
        ('.CMD', 'Bash', f'{prog.upper()}.CMD {a}'),
        ('call operator', 'PowerShell', f"& '{prog}' {sq}"),
        ('attached call operator', 'PowerShell', f'&"{prog}" {a}'),
        ('call operator path', 'PowerShell', f"& 'C:{BS}tools{BS}{prog}.exe' {sq}"),
        ('python subprocess list', 'Bash', f'python -c "import subprocess; subprocess.run([{listed}])"'),
        ('bash -c', 'Bash', f'bash -c "{prog} {a}"'),
        ("sh -c ''", 'Bash', f"sh -c '{prog} {a}'"),
        ('cmd /c', 'Bash', f'cmd /c {prog} {a}'),
        ('powershell -Command', 'Bash', f'powershell -NoProfile -Command "{prog} {a}"'),
        ('xargs', 'Bash', f'echo x | xargs {prog} {a}'),
        ('$(...)', 'Bash', f'echo $({prog} {a})'),
        ('backticks', 'Bash', f'echo `{prog} {a}`'),
        ('"$(...)"', 'Bash', f'echo "result: $({prog} {a})"'),
        ('<(...)', 'Bash', f'cat <({prog} {a})'),
        ('PowerShell $(...)', 'PowerShell', f'Write-Output "$({prog} {a})"'),
        ('backslash in name', 'Bash', f'{prog[0]}{BS}{prog[1:]} {a}'),
    ]


DB_PROGRAMS = [('psql', ['-h', 'localhost', '-c', 'select 1']), ('mysql', ['-e', 'select 1']),
               ('sqlcmd', ['-Q', 'select 1']), ('mongosh', ['--eval', 'db.x.drop()']), ('redis-cli', ['flushall']),
               ('sqlplus', ['-s', 'u/p']), ('invoke-sqlcmd', ['-Query', 'select 1'])]
SELF_VERBS = [['run', '--brief', 'b.md'], ['_job', 'j1'], ['land', 't1'], ['ship', 't1'], ['wt', 'prune'],
              ['resume', 'j1'], ['install', '--yes'], ['--best-of', '2', '--brief', 'b.md'], ['run', '--best-of=3']]
DENY_PROGRAMS = DB_PROGRAMS + [
    ('release-prod.sh', ['--now']), ('kubectl', ['delete', 'pod', 'web-1']), ('terraform', ['apply', '-auto-approve']),
    ('tl', ['run', '--brief', 'b.md']), ('tl', ['land', 't1']), ('git', ['push', 'origin', 'main']),
] + [('twinlane', verb) for verb in SELF_VERBS]
DENY_EXTRA = [
    'python -m twinlane run --brief b.md', 'python -mtwinlane land t1', 'py -3 -m twinlane.cli ship t1',
    'python -I -m twinlane wt prune', 'python /opt/src/twinlane/cli.py resume j1',
    'python3 ../twinlane/__main__.py install --yes', 'python src/twinlane/cli.py --best-of 3 run',
    'uvx twinlane run --brief b', 'npx tl run', 'twinlane-script.py run', 'twinlane wt doctor --prune', 'twinlane $VERB',
    "python -c \"from twinlane import cli; cli.main(['land', 't1'])\"",
    "python -c \"import subprocess,sys; subprocess.run([sys.executable, '-m', 'twinlane', 'run'])\"",
    'docker exec db psql -c "drop table x"', 'kubectl exec -it pg-0 -- psql', 'uv run mysql -e "select 1"',
    'wsl psql', 'psql16 -l', 'mysql8.0 -e x', "python -c \"import psycopg2; psycopg2.connect('dbname=x')\"",
    "node -e \"require('child_process').execSync('psql -l')\"", 'kubectl -n prod delete deploy web',
    'bash scripts/release-prod.sh', 'bash scripts/"release-prod".sh', "bash scripts/release-prod$'" + BS + "x2e'sh",
    'bash scripts/release-pro{d,x}.sh', 'python ops/rotate_keys.py', 'python "ops"/rotate_keys.py',
    'git.cmd push', 'git.bat commit -m x', 'cmd /c g^it push', 'g' + BS + 'it push', 'git $SUB', 'git ${IFS}push',
    'g$x push', 'git${IFS}push', 'echo $(cd .. && rm -rf x)', 'echo $(cd ..; touch x)', 'cat <<EOF\n$(git push)\nEOF',
    'echo $(echo $(echo $(echo $(echo $(echo hi)))))', 'echo "$(case x in a) git push;; esac)"',
    'case x in a|b) git push;; esac', 'bash <(echo git push)', 'source <(echo psql)', 'python /dev/stdin',
    'exec $c', '. $c', 'claude -p hi', 'npx codex exec hi', 'git log $(git merge-base HEAD main)..HEAD --output=../x',
]
DENY_EXTRA_POWERSHELL = [
    'New-Object System.Data.SqlClient.SqlConnection', 'Write-Output (git push)', 'Write-Output @(git push)',
    '& (Get-Command psql) -l', '&("ps"+"ql")', '. (Get-Command psql)', 'Start-Process psql', 'iex "psql"',
]
ALLOW_CORPUS = [
    'git status', 'git diff --stat', 'git log --oneline -10', 'git add twinlane/guard.py', 'git show HEAD~1:README.md',
    'git rev-parse HEAD', 'git branch --show-current', 'git restore --staged notes.txt', 'ls -la', 'cat README.md',
    'grep -rn "twinlane run" docs', 'grep -rn psql docs/', 'rg -n deny_text twinlane', 'python -m pytest tests -q',
    'python -B -m unittest discover -s tests -p "test_guard*.py"', 'python -B -c "print(1)"', 'pip install -e .',
    'pip show twinlane', 'twinlane status', 'twinlane jobs', 'twinlane result abc', 'twinlane wt doctor',
    'python -m twinlane jobs', 'twinlane --help', 'tl jobs', 'npm test', 'npm run build', 'npx eslint .',
    'node scripts/build.js', 'mkdir -p build/out', 'touch notes.txt', 'cp a.txt b.txt', 'mv old.txt new.txt',
    'rm -f build/tmp.txt', 'echo hello > out.txt', 'echo $(date) > stamp.txt', "printf '%s" + BS + "n' a b",
    'sed -n 1,5p README.md', 'head -n 20 twinlane/guard.py', 'wc -l tests/*.py', 'find . -name "*.py" -newer setup.cfg',
    'jq .version package.json', "python -c \"import json;print(json.dumps({'a': 1}))\"", 'pytest -k mysql_url_parsing',
    "cat <<'EOF' > notes.md\nwrite $(git push) literally\nEOF", 'docker build -t app .', 'docker ps',
    'kubectl get pods', 'terraform plan', 'make test', 'cargo test', 'go test ./...', 'echo "release notes" > notes.txt',
    'cd tests && python -m unittest test_guard', 'for f in a b; do echo $f; done', '(cd sub && make)',
    '{ echo a; echo b; } > both.txt', 'ls {src,tests}/*.py', 'sqlite3 build/test.db .tables',
    'sleep 1 & echo hi', 'echo $((1 + 2))',
]
ALLOW_CORPUS_POWERSHELL = [
    'Get-ChildItem -Recurse', 'Get-Content README.md | Select-Object -First 5', 'Write-Host (Get-Date)',
    '$n = (Get-ChildItem tests).Count; Write-Output $n', 'git status --short', '$x = @(1, 2); $x.Count',
]


def run_main(payload, env):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env, clear=False), redirect_stdout(out):
        rc = guard.main(io.StringIO(json.dumps(payload)))
    text = out.getvalue()
    reason = json.loads(text)['hookSpecificOutput']['permissionDecisionReason'] if text.strip() else None
    return rc, reason


class ConfigFile:
    """A user config file named by TWINLANE_CONFIG for the duration of a with block."""

    def __init__(self, content):
        self.content = content

    def __enter__(self):
        fd, self.path = tempfile.mkstemp(suffix='.json')
        with os.fdopen(fd, 'w', encoding='utf-8') as handle:
            handle.write(self.content if isinstance(self.content, str) else json.dumps(self.content))
        self.patch = mock.patch.dict(os.environ, {'TWINLANE_CONFIG': self.path})
        self.patch.start()
        return self.path

    def __exit__(self, *exc):
        self.patch.stop()
        os.unlink(self.path)


JOB = {'TWINLANE_ENGINE_JOB': 'j1', 'TWINLANE_ENGINE_PROFILE': 'code', 'TWINLANE_ENGINE_CWD': WT}


class GuardParity(unittest.TestCase):
    def test_review_shell_prefixes_and_trap(self):
        for cmd in ('then git push', 'do psql -l', 'else git push', 'elif psql -l',
                    '! git push', 'coproc git push', 'time git push', "trap 'git push' EXIT"):
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(judge(cmd), cmd)
        for cmd in ('if true; then echo hi; fi', 'for x in a b; do echo $x; done'):
            self.assertIsNone(judge(cmd), cmd)

    def test_review_program_after_prefix_and_data_mentions(self):
        for cmd in ('strace git push', 'setsid psql -l', 'doas release-prod.sh',
                    'nsenter -t 1 git push', 'time -p git push'):
            self.assertIsNotNone(judge(cmd), cmd)
        for cmd in ('grep -rn "git push" docs', 'rg -n psql docs', 'echo git push',
                    "printf '%s' psql", 'cat git', 'pytest -k psql_parser'):
            self.assertIsNone(judge(cmd), cmd)

    def test_review_runners_and_nested_shells(self):
        for cmd in ('docker-compose exec db psql', 'docker compose exec db psql',
                    'npm exec psql', 'pnpm exec psql', 'pnpm dlx psql', 'yarn dlx psql',
                    'bundle exec psql', 'pipenv run psql', 'poetry run psql', 'mise exec -- psql',
                    'nix run nixpkgs#postgresql -- psql', 'nix-shell --run psql',
                    'kubectl exec pg-0 -- bash -c "psql -l"'):
            self.assertIsNotNone(judge(cmd), cmd)

    def test_review_database_clients_and_libraries(self):
        for prog in ('pg_dump', 'pg_dumpall', 'mysqldump', 'mongodump', 'mongoexport',
                     'mongoimport', 'pgcli', 'mycli', 'usql', 'mssql-cli', 'sqlpackage', 'bcp'):
            self.assertIsNotNone(judge(prog + ' x'), prog)
        for mod in ('pg8000', 'pymongo', 'asyncpg', 'aiomysql', 'pyodbc', 'pymssql'):
            self.assertIsNotNone(judge(f'python -c "import {mod}"'), mod)
        for mod in ('pg', 'mysql2', 'mssql', 'tedious', 'mongodb'):
            self.assertIsNotNone(judge(f'node -e "require(\'{mod}\')"'), mod)
        for cmd in ('New-Object Data.SqlClient.SqlConnection',
                    'New-Object Microsoft.Data.SqlClient.SqlConnection',
                    'New-Object -ComObject ADODB.Connection'):
            self.assertIsNotNone(judge(cmd, 'PowerShell'), cmd)

    def test_review_git_refs_tools_and_environment(self):
        for cmd in ('git fetch origin +HEAD:refs/heads/main', 'git fetch . HEAD:main',
                    'git gc --prune=now', 'git reflog expire --all', 'git bisect run git push',
                    "git difftool --extcmd='git push'", "GIT_PAGER='psql -l' git log",
                    "GIT_EXTERNAL_DIFF=psql git diff", "GIT_SSH_COMMAND=psql git fetch",
                    "EDITOR=psql git add -p", "VISUAL=psql git add -p", "PAGER=psql git log"):
            self.assertIsNotNone(judge(cmd), cmd)

    def test_review_inline_powershell_paths_and_persistence(self):
        cases = [('Bash', "awk 'BEGIN{system(\"git push\")}'"),
                 ('Bash', "sed 's/x/git push/e' README.md"), ('Bash', "vim -c ':!git push'"),
                 ('Bash', "perl -e 'qx(git push)'"), ('Bash', "ruby -e '%x(git push)'"),
                 ('PowerShell', "[Diagnostics.Process]::Start('git','push')"),
                 ('PowerShell', "[scriptblock]::Create('git push')"),
                 ('PowerShell', "[IO.File]::WriteAllText('\x43:\x5cx.txt','x')"),
                 ('PowerShell', "[IO.File]::Copy('inside.txt','\x43:\x5cx.txt')"),
                 ('PowerShell', "[IO.File]::Move('inside.txt','..\\x.txt')"),
                 ('PowerShell', "Set-Content -LiteralPath ('..'+'\\x') x"),
                 ('PowerShell', 'Export-Csv -Path \x43:\x5cx.csv'),
                 ('PowerShell', 'Start-Process notepad'),
                 ('PowerShell', "Invoke-Expression 'echo hi'"),
                 ('Bash', 'schtasks /create /tn x /tr x'),
                 ('PowerShell', 'Register-ScheduledTask x'),
                 ('Bash', 'crontab x'), ('Bash', 'at x'), ('Bash', 'sc create x'),
                 ('PowerShell', 'New-Service x'), ('Bash', 'reg add x')]
        for tool, cmd in cases:
            with self.subTest(cmd=cmd):
                self.assertIsNotNone(judge(cmd, tool), cmd)
        self.assertIsNone(judge("[IO.File]::Copy('\x43:\x5csource.txt','inside.txt')", 'PowerShell'))

    def test_review_self_stop_and_handoff(self):
        for cmd in ('twinlane stop j1', 'twinlane handoff j1',
                    'python -m twinlane stop j1', 'tl handoff j1'):
            self.assertIsNotNone(judge(cmd), cmd)

    def test_git_entry_and_unexpanded_paths_are_not_writable(self):
        # The worktree's .git names its repository: rewriting it points later git commands elsewhere.
        def write(path, tool='Write'):
            return guard.evaluate(tool, {'file_path': path, 'content': 'x'}, 'code', WT, ALLOW, config=CONFIG)
        for rel in ('.git', '.git/hooks/pre-commit', '.GIT', '.git.', '.git ', 'GIT~1', '.git::$DATA',
                    'sub/../.git', 'sub/.git/config'):
            for tool in ('Write', 'Edit'):
                self.assertIsNotNone(write(WT + '/' + rel, tool), rel)
        for path in ('~/notes.txt', '$HOME/notes.txt', '%USERPROFILE%/notes.txt', 'C:notes.txt',
                     'file:///tmp/notes.txt'):
            self.assertIsNotNone(write(path), path)
        for rel in ('notes.txt', '.gitignore', '.github/workflows/ci.yml', 'docs/.gitkeep', 'git/readme'):
            self.assertIsNone(write(WT + '/' + rel), rel)
        for cmd in ('echo x > .git', 'cp hook .git/hooks/pre-commit', 'mv a .git.', 'echo x > GIT~1',
                    'cd sub && echo x > ../.git', 'Set-Content -Path .git -Value x'):
            for tool in ('Bash', 'PowerShell'):
                self.assertIsNotNone(judge(cmd, tool), (tool, cmd))
        for cmd in ('echo x > .gitignore', 'cat .git', 'git status', 'echo hi > notes.txt'):
            self.assertIsNone(judge(cmd), cmd)

    def test_review_non_object_policy_fails_closed(self):
        with mock.patch.object(common, 'load_policy', return_value=[]):
            rc, reason = run_main({'tool_name': 'Read', 'tool_input': {'file_path': 'README.md'}}, JOB)
        self.assertEqual(rc, 0)
        self.assertIn('policy is not an object', reason or '')

    def test_review_regex_complexity_and_timing(self):
        for pattern in ('(a+)+', '(a*)*', '(.*)+', 'x' * 513):
            with self.subTest(pattern=pattern[:20]):
                with self.assertRaises(guard.GuardConfigError):
                    guard.load_config({'deny_text': [pattern]})
        command = 'echo ' + 'a' * (32 * 1024 - 6)
        start = time.perf_counter()
        guard.shell_hazard(command, {'deny_text': [r'^blocked$']})
        self.assertLess(time.perf_counter() - start, 1.0)
        command = 'echo ' + ('a ' * (16 * 1024 - 4))
        start = time.perf_counter()
        self.assertIsNotNone(guard.shell_hazard(command))
        self.assertLess(time.perf_counter() - start, 1.0)

    def test_review_batch_corpus(self):
        for batch in ('b1', 'b2', 'b3', 'b4', 'b5'):
            path = os.path.join(os.path.dirname(__file__), 'guard_review_' + batch + '.txt')
            with open(path, encoding='utf-8') as handle:
                for line in handle:
                    line = line.rstrip('\n')
                    if line.startswith('#') or line.count('\t') < 2:
                        continue
                    tool, expected, cmd = line.split('\t', 2)
                    if cmd.startswith('E:'):
                        cmd = cmd[2:].encode('latin-1', 'backslashreplace').decode('unicode_escape')
                    with self.subTest(batch=batch, cmd=cmd):
                        self.assertEqual(judge(cmd, tool) is not None, expected == 'D', cmd)

    def test_db_clients_denied_every_spelling(self):
        for prog, args in DB_PROGRAMS:
            for label, tool, cmd in spellings(prog, args):
                with self.subTest(program=prog, spelling=label):
                    self.assertIn('database', judge(cmd, tool, config={}) or '', cmd)
            self.assertIn('database', guard.shell_hazard(f'{prog} {" ".join(args)}') or '')
            self.assertIn('database', judge(f'{prog} -l', profile='intel') or '')
        for cmd in ('docker exec db psql -c "drop table x"', 'podman run --rm pg psql', 'kubectl exec -it pg-0 -- psql',
                    'uv run mysql -e "select 1"', 'poetry run psql', 'wsl psql', 'psql16 -l', 'mysql8.0 -e x',
                    'pg_restore -d x dump', 'mongo admin', 'cqlsh host', 'clickhouse-client -q x', 'osql -Q x',
                    'bcp t out f', 'Invoke-Sqlcmd -Query x', 'find . -exec psql {} ;', 'env -S "psql -l"',
                    "python -c \"import psycopg2; psycopg2.connect('dbname=x')\"", "python -c \"import pyodbc\"",
                    "python -c \"import mysql.connector\"", "php -r \"new PDO('mysql:host=x');\"",
                    "node -e \"require('child_process').execSync('psql -l')\""):
            self.assertIsNotNone(judge(cmd, config={}), cmd)
        self.assertIsNotNone(judge('New-Object System.Data.SqlClient.SqlConnection', 'PowerShell', config={}))
        for cmd in ('grep -rn psql docs/', 'pytest -k mysql_url_parsing', 'cat sql/schema.sql',
                    'sqlite3 build/test.db .tables', "python -c \"import sqlite3; sqlite3.connect('t.db')\"",
                    'docker build -t pg .', 'pip show psycopg2'):
            self.assertIsNone(judge(cmd, config={}), cmd)

    def test_configured_deny_commands_and_text(self):
        denied = ('release-prod.sh --now', './release-prod.sh', 'bash release-prod.sh', 'sh ./release-prod.sh -y',
                  'kubectl delete pod x', 'kubectl -n prod delete deploy web', 'terraform apply',
                  'terraform -chdir=infra apply -auto-approve', 'python ops/rotate_keys.py',
                  'bash -c "python ops/rotate_db.py"', 'python ops/rot{ate,x}_keys.py', 'python "ops"/rotate_keys.py',
                  "python ops$'" + BS + "x2f'rotate_keys.py", "python -c \"import os; os.system('release-prod.sh')\"")
        for cmd in denied:
            self.assertIsNotNone(judge(cmd), cmd)
            self.assertIsNone(judge(cmd, config={}), cmd)  # none of it is denied without the configuration
        self.assertIn('guard.deny_commands', judge('kubectl delete pod x'))
        self.assertIn('guard.deny_text', judge('python ops/rotate_keys.py'))
        self.assertIsNotNone(guard.shell_hazard('terraform apply', CONFIG))
        for cmd in ('kubectl get pods', 'terraform plan', 'cat release-notes.md', 'python ops/report.py',
                    'kubectl describe pod delete-me-not'):
            self.assertIsNone(judge(cmd), cmd)
        # From the user config file, both through evaluate() and through the hook.
        with ConfigFile({'guard': CONFIG}):
            self.assertIsNotNone(guard.evaluate('Bash', {'command': 'terraform apply'}, 'code', WT, ALLOW))
            self.assertIsNone(guard.evaluate('Bash', {'command': 'terraform plan'}, 'code', WT, ALLOW))
            rc, reason = run_main({'tool_name': 'Bash', 'tool_input': {'command': 'bash release-prod.sh'}}, JOB)
            self.assertEqual(rc, 0)
            self.assertIn('release-prod', reason or '')

    def test_invalid_guard_config_fails_closed(self):
        for bad in ('deny everything', [], {'allow_subagents': 'yes'}, {'deny_text': ['(unclosed']},
                    {'deny_text': 'one-string'}, {'deny_text': [1]}, {'deny_commands': ['']}, {'deny_commands': [' ']},
                    {'self_aliases': ['two words']}, {'self_aliases': ['/']}, {'unknown_key': True},
                    {'deny_text': ['x'] * 300}, {'deny_commands': ['x' * 600]}):
            with self.subTest(config=str(bad)[:60]):
                with self.assertRaises(guard.GuardConfigError):
                    guard.load_config(bad)
                for tool, tool_input in (('Read', {'file_path': 'README.md'}), ('Bash', {'command': 'git status'}),
                                         ('Edit', {'file_path': WT + '/a.py'})):
                    for profile in ('code', 'intel'):
                        reason = guard.evaluate(tool, tool_input, profile, WT, ALLOW, config=bad)
                        self.assertIn('guard configuration is not valid', reason or '', (tool, profile))
        self.assertEqual(guard.load_config(None)['allow_subagents'], False)  # absent section: the defaults
        # Through the user config file: an invalid guard section, and a file that is not JSON at all.
        call = {'tool_name': 'Read', 'tool_input': {'file_path': 'README.md'}}
        for content in ({'guard': {'deny_text': ['[']}}, {'guard': None}, '{not json'):
            with self.subTest(content=str(content)[:40]), ConfigFile(content):
                self.assertIsNotNone(guard.evaluate('Read', {'file_path': 'README.md'}, 'code', WT, ALLOW))
                rc, reason = run_main(call, JOB)
                self.assertEqual(rc, 0)
                self.assertIsNotNone(reason)
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop('TWINLANE_ENGINE_JOB', None)
                    self.assertEqual(run_main(call, {}), (0, None))  # outside a job the hook stays silent

    def test_self_commands_denied_in_job(self):
        invocations = ['twinlane', 'twinlane.cmd', 'twinlane.exe', 'TWINLANE.CMD', '/usr/local/bin/twinlane',
                       '"\x43:/Program Files/py/Scripts/twinlane.exe"', 'python -m twinlane', 'python -mtwinlane',
                       'py -3 -m twinlane.cli', 'python -I -m twinlane.__main__', 'python src/twinlane/cli.py',
                       'python3 ../twinlane/__main__.py', 'tl', 'tl.cmd', 'uvx twinlane', 'env X=1 twinlane',
                       'sudo twinlane', 't"winlane"', "$'" + BS + "x74'winlane"]
        for prefix in invocations:
            for verb in SELF_VERBS:
                cmd = f'{prefix} {" ".join(verb)}'
                with self.subTest(command=cmd):
                    self.assertIsNotNone(judge(cmd), cmd)
        for cmd in ('bash -c "twinlane land t1"', "python -c \"import subprocess; subprocess.run(['twinlane', 'ship'])\"",
                    "python -c \"from twinlane import cli; cli.main(['run', '--brief', 'b'])\"",
                    'echo $(twinlane resume j1)', 'twinlane $VERB', 'twinlane wt doctor --prune', 'twinlane wt'):
            self.assertIsNotNone(judge(cmd), cmd)
        for cmd in ("& 'twinlane' land t1", "& 'C:" + BS + "tools" + BS + "twinlane.exe' ship t1"):
            self.assertIsNotNone(judge(cmd, 'PowerShell'), cmd)
        self.assertIsNone(judge('tl run', config={}))  # an alias only counts once it is configured
        for cmd in ('twinlane jobs', 'twinlane status', 'twinlane result j1', 'twinlane wait j1', 'twinlane wt doctor',
                    'python -m twinlane jobs', 'tl jobs', 'pip show twinlane', 'cat twinlane/cli.py', 'twinlane --help',
                    'grep -rn "twinlane land" docs'):
            self.assertIsNone(judge(cmd), cmd)
        rc, reason = run_main({'tool_name': 'Bash', 'tool_input': {'command': 'python -m twinlane land t1'}}, JOB)
        self.assertEqual(rc, 0)
        self.assertIn('land', reason or '')

    def test_subagents_denied_by_default_allowed_by_config(self):
        for tool in sorted(guard.SUBAGENT_TOOLS):
            prompt = {'prompt': 'look around', 'description': 'x'}
            self.assertIn('allow_subagents', guard.evaluate(tool, prompt, 'code', WT, ALLOW) or '')
            for profile in ('code', 'intel'):
                self.assertIsNotNone(guard.evaluate(tool, prompt, profile, WT, ALLOW, config={}))
            allowed = {'allow_subagents': True}
            self.assertIsNone(guard.evaluate(tool, prompt, 'code', WT, ALLOW, config=allowed))
            # intel stays an allow-list: the tool must be in intel_allow too
            self.assertIsNotNone(guard.evaluate(tool, prompt, 'intel', WT, [], config=allowed))
            self.assertIsNone(guard.evaluate(tool, prompt, 'intel', WT, [tool], config=allowed))
            call = {'tool_name': tool, 'tool_input': prompt}
            self.assertIsNotNone(run_main(call, JOB)[1])
            with ConfigFile({'guard': allowed}):
                self.assertEqual(run_main(call, JOB), (0, None))

    def test_allow_corpus_passes(self):
        corpus = [(cmd, 'Bash') for cmd in ALLOW_CORPUS] + [(cmd, 'PowerShell') for cmd in ALLOW_CORPUS_POWERSHELL]
        self.assertGreaterEqual(len(corpus), 40)
        for cmd, tool in corpus:
            with self.subTest(command=cmd):
                self.assertIsNone(judge(cmd, tool), cmd)

    def test_deny_corpus_matrix(self):
        corpus = [(f'{prog} [{label}]', tool, cmd) for prog, args in DENY_PROGRAMS
                  for label, tool, cmd in spellings(prog, args)]
        corpus += [(cmd, 'Bash', cmd) for cmd in DENY_EXTRA] + [(cmd, 'PowerShell', cmd) for cmd in DENY_EXTRA_POWERSHELL]
        self.assertGreaterEqual(len(corpus), 500)
        for name, tool, cmd in corpus:
            with self.subTest(case=name):
                self.assertIsNotNone(judge(cmd, tool), cmd)


if __name__ == '__main__':
    unittest.main()
