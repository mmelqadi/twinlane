"""Adversarial self-check for the twinlane guard.

Usage: python -B tests/adversarial_check.py <repo root>
Prints DENY n/n for the deny corpus and ALLOW m/m for the allow corpus, then every miss. Exit 1 on a miss.
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, sys.argv[1] if len(sys.argv) > 1 else os.path.dirname(HERE))
from twinlane import guard  # noqa: E402

# Placeholder checkouts, spelled with \x escapes so this source carries no absolute-path text.
WT = '\x43:\x2fUsers/alice/worktrees/task-a' if os.name == 'nt' else '\x2fhome/alice/worktrees/task-a'
CONFIG = {'allow_subagents': False, 'self_aliases': ['tl'],
          'deny_commands': ['release-prod.sh', 'kubectl delete', 'terraform apply'],
          'deny_text': [r'release-prod\.sh', r'ops/rotate_\w+\.py']}


def judge(cmd, tool='Bash'):
    return guard.evaluate(tool, {'command': cmd}, 'code', WT, [], config=CONFIG)


def ansi(word):
    """The first character of word written as bash ANSI-C hex."""
    return "$'\\x%02x'" % ord(word[0]) + word[1:]


def transforms(prog, args):
    """(label, tool, command) for one program and its arguments across every spelling the plan names."""
    a = ' '.join(args)
    sq = ' '.join("'%s'" % x for x in args)
    lst = ', '.join(repr(x) for x in [prog] + list(args))
    return [
        ('plain', 'Bash', f'{prog} {a}'),
        ('dquote', 'Bash', f'"{prog}" {a}'),
        ('squote-split', 'Bash', f"{prog[0]}'{prog[1:]}' {a}"),
        ('ansi-c', 'Bash', f'{ansi(prog)} {a}'),
        ('brace', 'Bash', f'{{{prog},}} {a}'),
        ('env', 'Bash', f'env FOO=1 {prog} {a}'),
        ('sudo', 'Bash', f'sudo -u root {prog} {a}'),
        ('timeout', 'Bash', f'timeout 5 nohup {prog} {a}'),
        ('full-path', 'Bash', f'/usr/local/bin/{prog} {a}'),
        ('win-path', 'Bash', f'"\x43:/Program Files/tools/{prog}.exe" {a}'),
        ('.exe', 'Bash', f'{prog}.exe {a}'),
        ('.cmd', 'Bash', f'{prog.upper()}.CMD {a}'),
        ('ps-call', 'PowerShell', f"& '{prog}' {sq}"),
        ('ps-call-attached', 'PowerShell', f'&"{prog}" {a}'),
        ('ps-call-path', 'PowerShell', f"& '\x43:\x5ctools\x5c{prog}.exe' {sq}"),
        ('py-subprocess', 'Bash', f'python -c "import subprocess; subprocess.run([{lst}])"'),
        ('bash-c', 'Bash', f'bash -c "{prog} {a}"'),
        ('sh-c-squote', 'Bash', f"sh -c '{prog} {a}'"),
        ('cmd-c', 'Bash', f'cmd /c {prog} {a}'),
        ('pwsh-command', 'Bash', f'powershell -NoProfile -Command "{prog} {a}"'),
        ('xargs', 'Bash', f'echo x | xargs {prog} {a}'),
        ('subst', 'Bash', f'echo $({prog} {a})'),
        ('backticks', 'Bash', f'echo `{prog} {a}`'),
        ('dq-subst', 'Bash', f'echo "result: $({prog} {a})"'),
        ('proc-subst', 'Bash', f'cat <({prog} {a})'),
        ('ps-subexpr', 'PowerShell', f'Write-Output "$({prog} {a})"'),
        ('backslash', 'Bash', f'{prog[0]}\\{prog[1:]} {a}'),
    ]


DENY_PROGRAMS = [
    # database clients
    ('psql', ['-h', 'localhost', '-c', 'select 1']),
    ('mysql', ['-e', 'select 1']),
    ('sqlcmd', ['-Q', 'select 1']),
    ('mongosh', ['--eval', 'db.x.drop()']),
    ('redis-cli', ['flushall']),
    ('invoke-sqlcmd', ['-Query', 'select 1']),
    # configured deny_commands
    ('release-prod.sh', ['--now']),
    ('kubectl', ['delete', 'pod', 'web-1']),
    ('terraform', ['apply', '-auto-approve']),
    # twinlane's own dispatching commands, and the configured alias
    ('twinlane', ['run', '--brief', 'b.md']),
    ('twinlane', ['_job', 'j1']),
    ('twinlane', ['land', 't1']),
    ('twinlane', ['ship', 't1']),
    ('twinlane', ['wt', 'prune']),
    ('twinlane', ['resume', 'j1']),
    ('twinlane', ['install', '--yes']),
    ('twinlane', ['--best-of', '2', 'run']),
    ('tl', ['run', '--brief', 'b.md']),
    ('tl', ['land', 't1']),
    # git writes through the spellings the matrix exercises (git.cmd was a gap)
    ('git', ['push', 'origin', 'main']),
]

EXTRA_DENY = [
    ('Bash', 'python -m twinlane run --brief b.md'),
    ('Bash', 'python -mtwinlane land t1'),
    ('Bash', 'py -3 -m twinlane.cli ship t1'),
    ('Bash', 'python -I -m twinlane wt prune'),
    ('Bash', 'python \x43:/src/twinlane/cli.py resume j1'),
    ('Bash', 'python3 ../twinlane/__main__.py install --yes'),
    ('Bash', 'python src/twinlane/cli.py --best-of 3 run'),
    ('Bash', 'uvx ' 'twinlane run --brief b'),
    ('Bash', 'npx tl run'),
    ('Bash', 'twinlane-script.py run'),
    ('Bash', 'twinlane wt doctor --prune'),
    ('Bash', 'twinlane $VERB'),
    ('Bash', 'python -c "from twinlane import cli; cli.main([\'land\', \'t1\'])"'),
    ('Bash', 'python -c "import subprocess,sys; subprocess.run([sys.executable, \'-m\', \'twinlane\', \'run\'])"'),
    ('Bash', 'docker exec db psql -c "drop table x"'),
    ('Bash', 'kubectl exec -it pg-0 -- psql'),
    ('Bash', 'uv run mysql -e "select 1"'),
    ('Bash', 'wsl psql'),
    ('Bash', 'psql16 -l'),
    ('Bash', 'mysql8.0 -e x'),
    ('Bash', 'python -c "import psycopg2; psycopg2.connect(\'dbname=x\')"'),
    ('Bash', 'node -e "require(\'child_process\').execSync(\'psql -l\')"'),
    ('PowerShell', 'New-Object System.Data.SqlClient.SqlConnection'),
    ('Bash', 'kubectl -n prod delete deploy web'),
    ('Bash', 'bash scripts/release-prod.sh'),
    ('Bash', 'bash scripts/"release-prod".sh'),
    ('Bash', "bash scripts/release-prod$'\\x2e'sh"),
    ('Bash', 'bash scripts/release-pro{d,x}.sh'),
    ('Bash', 'python ops/rotate_keys.py'),
    ('Bash', 'git.cmd push'),
    ('Bash', 'git.bat commit -m x'),
    ('Bash', 'cmd /c g^it push'),
    ('Bash', 'g\\it push'),
    ('Bash', 'git $SUB'),
    ('Bash', 'git ${IFS}push'),
    ('Bash', 'g$x push'),
    ('Bash', 'git${IFS}push'),
    ('Bash', 'echo $(cd .. && rm -rf x)'),
    ('Bash', 'echo $(cd ..; touch x)'),
    ('Bash', 'cat <<EOF\n$(git push)\nEOF'),
    ('Bash', 'echo $(echo $(echo $(echo $(echo $(echo hi)))))'),
    ('Bash', 'echo "$(case x in a) git push;; esac)"'),
    ('PowerShell', 'Write-Output (git push)'),
    ('PowerShell', 'Write-Output @(git push)'),
    ('Bash', 'claude -p hi'),
    ('Bash', 'npx codex exec hi'),
    ('Bash', 'git log $(git merge-base HEAD main)..HEAD --output=../x'),
    ('Bash', 'case x in a|b) git push;; esac'),
]

ALLOW = [
    ('Bash', 'git status'), ('Bash', 'git diff --stat'), ('Bash', 'git log --oneline -10'),
    ('Bash', 'git add twinlane/guard.py'), ('Bash', 'git show HEAD~1:README.md'), ('Bash', 'git rev-parse HEAD'),
    ('Bash', 'git branch --show-current'), ('Bash', 'git restore --staged notes.txt'),
    ('Bash', 'ls -la'), ('Bash', 'cat README.md'), ('Bash', 'grep -rn "twinlane run" docs'),
    ('Bash', 'grep -rn psql docs/'), ('Bash', 'rg -n deny_text twinlane'),
    ('Bash', 'python -m pytest tests -q'), ('Bash', 'python -B -m unittest discover -s tests -p "test_guard*.py"'),
    ('Bash', 'python -B -c "print(1)"'), ('Bash', 'pip install -e .'), ('Bash', 'pip show twinlane'),
    ('Bash', 'twinlane status'), ('Bash', 'twinlane jobs'), ('Bash', 'twinlane result abc'),
    ('Bash', 'twinlane wt doctor'), ('Bash', 'python -m twinlane jobs'), ('Bash', 'twinlane --help'),
    ('Bash', 'tl jobs'), ('Bash', 'npm test'), ('Bash', 'npm run build'), ('Bash', 'npx eslint .'),
    ('Bash', 'node scripts/build.js'), ('Bash', 'mkdir -p build/out'), ('Bash', 'touch notes.txt'),
    ('Bash', 'cp a.txt b.txt'), ('Bash', 'mv old.txt new.txt'), ('Bash', 'rm -f build/tmp.txt'),
    ('Bash', 'echo hello > out.txt'), ('Bash', 'echo $(date) > stamp.txt'), ('Bash', "printf '%s\\n' a b"),
    ('Bash', 'sed -n 1,5p README.md'), ('Bash', 'head -n 20 twinlane/guard.py'), ('Bash', 'wc -l tests/*.py'),
    ('Bash', 'find . -name "*.py" -newer setup.cfg'), ('Bash', 'jq .version package.json'),
    ('Bash', 'python -c "import json;print(json.dumps({\'a\': 1}))"'), ('Bash', 'pytest -k mysql_url_parsing'),
    ('Bash', "cat <<'EOF' > notes.md\nwrite $(git push) literally\nEOF"), ('Bash', 'docker build -t app .'),
    ('Bash', 'docker ps'), ('Bash', 'kubectl get pods'), ('Bash', 'terraform plan'), ('Bash', 'make test'),
    ('Bash', 'cargo test'), ('Bash', 'go test ./...'), ('Bash', 'echo "deploy notes" > notes.txt'),
    ('Bash', 'cd tests && python -m unittest test_guard'), ('Bash', 'for f in a b; do echo $f; done'),
    ('Bash', '(cd sub && make)'), ('Bash', '{ echo a; echo b; } > both.txt'), ('Bash', 'ls {src,tests}/*.py'),
    ('PowerShell', 'Get-ChildItem -Recurse'), ('PowerShell', 'Get-Content README.md | Select-Object -First 5'),
    ('PowerShell', 'Write-Host (Get-Date)'), ('PowerShell', '$n = (Get-ChildItem tests).Count; Write-Output $n'),
    ('PowerShell', 'git status --short'),
    ('Bash', 'if true; then echo hi; fi'),
    ('Bash', 'if false; then echo no; else printf yes; fi'),
    ('Bash', 'for x in a b; do echo $x; done'),
    ('Bash', 'grep -rn "git push" docs'),
    ('Bash', 'rg -n psql docs'),
    ('Bash', 'echo psql'),
    ('Bash', "printf '%s' git push"),
    ('Bash', 'cat git'),
    ('Bash', 'pytest -k psql_parser'),
]


def review_probes():
    """The reviewed command table is kept beside the parity tests, independent of a temporary directory."""
    base = HERE
    for batch in ('b1', 'b2', 'b3', 'b4', 'b5'):
        with open(os.path.join(base, 'guard_review_' + batch + '.txt'), encoding='utf-8') as handle:
            for line in handle:
                line = line.rstrip('\n')
                if line.startswith('#') or line.count('\t') < 2:
                    continue
                tool, expected, cmd = line.split('\t', 2)
                if cmd.startswith('E:'):
                    cmd = cmd[2:].encode('latin-1', 'backslashreplace').decode('unicode_escape')
                yield batch, tool, expected, cmd


def main():
    deny = [(f'{prog} [{label}]', tool, cmd) for prog, args in DENY_PROGRAMS for label, tool, cmd in
            transforms(prog, args)] + [(cmd, tool, cmd) for tool, cmd in EXTRA_DENY]
    deny += [(batch, tool, cmd) for batch, tool, expected, cmd in review_probes() if expected == 'D']
    allows = ALLOW + [(tool, cmd) for _, tool, expected, cmd in review_probes() if expected == 'A']
    missed = [(name, tool, cmd) for name, tool, cmd in deny if not judge(cmd, tool)]
    print(f'DENY {len(deny) - len(missed)}/{len(deny)}')
    wrong = [(tool, cmd, judge(cmd, tool)) for tool, cmd in allows if judge(cmd, tool)]
    print(f'ALLOW {len(allows) - len(wrong)}/{len(allows)}')
    for name, tool, cmd in missed:
        print('  MISSED DENY', tool, repr(cmd))
    for tool, cmd, why in wrong:
        print('  WRONG DENY ', tool, repr(cmd), '->', why)
    return 1 if missed or wrong else 0


if __name__ == '__main__':
    sys.exit(main())
