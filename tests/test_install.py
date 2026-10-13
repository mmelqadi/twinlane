"""install.py: hook edits, --check, --unhook, --hook-command, --alias and --skill. Temp directories only."""
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, install  # noqa: E402

CLI = os.path.join(ROOT, 'twinlane', 'cli.py')


class InstallCase(unittest.TestCase):
    ORIGINAL = {
        'permissions': {'allow': ['Read'], 'deny': []},
        'model': 'opus',
        'hooks': {
            'UserPromptSubmit': [{'hooks': [{'type': 'command', 'command': 'python "/x/other_guard.py"',
                                             'timeout': 10}]}],
            'PreToolUse': [{'matcher': 'Bash', 'hooks': [{'type': 'command', 'command': 'python g.py'}]}],
        },
        'theme': 'dark',
        'note': 'ملاحظة عربية',
    }

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-install-', dir=os.environ['TWINLANE_HOME'])
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = os.path.join(self.tmp, 'settings.json')
        self.bin = os.path.join(self.tmp, 'bin')
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(self.ORIGINAL, handle, ensure_ascii=False, indent=2)

    def load(self):
        with open(self.path, encoding='utf-8') as handle:
            return json.load(handle)

    def backups(self, folder=None, marker='.twinlane-backup-'):
        folder = folder or self.tmp
        return sorted(f for f in os.listdir(folder) if marker in f)

    def cli(self, *args, settings=True):
        out, err = io.StringIO(), io.StringIO()
        argv = list(args) + (['--settings', self.path] if settings else [])
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = install.main(argv)
            except SystemExit as exc:  # argparse usage errors
                rc = exc.code
        self.last_err = err.getvalue()
        return rc, out.getvalue()


class TestInstallSettings(InstallCase):
    def test_hook_adds_entry_with_backup_and_preserves_everything(self):
        rc, _ = self.cli('--yes')
        self.assertEqual(rc, 0)
        s = self.load()
        ups = s['hooks']['UserPromptSubmit']
        self.assertEqual(ups[0], self.ORIGINAL['hooks']['UserPromptSubmit'][0])
        self.assertEqual(ups[1], {'hooks': [{'type': 'command', 'command': install.command(), 'timeout': 5}]})
        self.assertEqual(install.command(), 'twinlane hook || exit 0')
        for key in ('permissions', 'model', 'theme', 'note'):
            self.assertEqual(s[key], self.ORIGINAL[key])
        self.assertEqual(s['hooks']['PreToolUse'], self.ORIGINAL['hooks']['PreToolUse'])
        self.assertEqual(list(s), list(self.ORIGINAL))  # key order kept
        self.assertEqual(len(self.backups()), 1)
        with open(os.path.join(self.tmp, self.backups()[0]), encoding='utf-8') as handle:
            self.assertEqual(json.load(handle), self.ORIGINAL)

    def test_hook_is_idempotent(self):
        self.cli('--yes')
        rc, out = self.cli('--yes')
        self.assertEqual(rc, 0)
        self.assertIn('already registered', out)
        self.assertEqual(install.count_our_hooks(self.load()), 1)
        self.assertEqual(len(self.backups()), 1)  # a no-op writes no new backup

    def test_unhook_round_trip_restores_original(self):
        self.cli('--yes')
        rc, out = self.cli('--unhook', '--yes')
        self.assertEqual(rc, 0)
        self.assertIn('Verified no twinlane hook remains', out)
        self.assertEqual(self.load(), self.ORIGINAL)

    def test_unhook_removes_only_our_entry_from_a_shared_group(self):
        s = json.loads(json.dumps(self.ORIGINAL))
        s['hooks']['UserPromptSubmit'][0]['hooks'].append(
            {'type': 'command', 'command': 'python "/opt/twinlane/twinlane/cli.py" hook'})
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(s, handle)
        rc, _ = self.cli('--unhook', '--yes')
        self.assertEqual(rc, 0)
        self.assertEqual(self.load(), self.ORIGINAL)

    def test_unhook_when_absent_changes_nothing(self):
        rc, out = self.cli('--unhook', '--yes')
        self.assertEqual(rc, 0)
        self.assertIn('not registered', out)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.load(), self.ORIGINAL)

    def test_hook_on_settings_without_hooks_then_unhook_drops_the_key(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump({'model': 'opus', 'hooks': {}}, handle)
        self.cli('--yes')
        self.assertEqual(install.count_our_hooks(self.load()), 1)
        self.cli('--unhook', '--yes')
        self.assertEqual(self.load(), {'model': 'opus', 'hooks': {}})

    def test_non_list_event_is_refused(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump({'hooks': {'UserPromptSubmit': {'oops': 1}}}, handle)
        rc, _ = self.cli('--yes')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(self.load(), {'hooks': {'UserPromptSubmit': {'oops': 1}}})

    def test_invalid_json_is_refused_untouched(self):
        with open(self.path, 'w', encoding='utf-8') as handle:
            handle.write('{"model": "opus",')
        for flag in ('--yes', '--unhook'):
            rc, _ = self.cli(flag)
            self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(Path(self.path).read_text(encoding='utf-8'), '{"model": "opus",')
        self.assertEqual(self.backups(), [])

    def test_env_var_selects_settings_path(self):
        with mock.patch.dict(os.environ, {'TWINLANE_SETTINGS_PATH': self.path}):
            self.assertEqual(install.default_settings_path(), self.path)

    def test_preview_writes_nothing(self):
        before = Path(self.path).read_text(encoding='utf-8')
        rc, out = self.cli()
        self.assertEqual(rc, 0)
        self.assertIn('Run with --yes', out)
        self.assertEqual(Path(self.path).read_text(encoding='utf-8'), before)
        self.assertEqual(os.listdir(self.tmp), ['settings.json'])

    def test_hook_pattern_matches_spellings(self):
        for cmd in ('twinlane hook',
                    'twinlane hook || exit 0',
                    'python /opt/twinlane/twinlane/cli.py hook',
                    'python "\x43:\x5ctools\x5ctwinlane\x5ccli.py" hook || exit 0',
                    '"\x43:/Program Files/Python/python.exe" -m twinlane hook ',
                    'twinlane.exe hook'):
            self.assertTrue(install.is_our_hook({'command': cmd}), cmd)
        for cmd in ('twinlane meter', 'other hook', 'mytwinlane hook', 'twinlane hooks', None, ''):
            self.assertFalse(install.is_our_hook({'command': cmd}), cmd)
        self.assertFalse(install.is_our_hook('twinlane hook'))


class TestCheck(InstallCase):
    def test_check_is_read_only_and_reports(self):
        before = os.path.getmtime(self.path)
        skill = os.path.join(self.tmp, 'skills', 'twinlane')
        rc, out = self.cli('--check', '--json', '--bin-dir', self.bin, '--alias', 'tl', '--skill', skill)
        self.assertEqual(rc, 0)
        report = json.loads(out)
        self.assertEqual(report['hook']['registered'], 0)
        self.assertFalse(report['hook']['exactly_one'])
        self.assertEqual(report['aliases']['tl']['tl.cmd']['state'], 'missing')
        self.assertFalse(report['skills'][skill]['exists'])
        self.assertEqual(os.path.getmtime(self.path), before)
        self.assertEqual(self.backups(), [])
        self.assertFalse(os.path.exists(self.bin))
        self.assertFalse(os.path.exists(os.path.dirname(skill)))

    def test_check_counts_exactly_one_and_duplicates(self):
        self.cli('--yes')
        report = json.loads(self.cli('--check', '--json', '--bin-dir', self.bin, '--skill', self.tmp)[1])
        self.assertEqual((report['hook']['registered'], report['hook']['exactly_one']), (1, True))
        s = self.load()
        s['hooks']['UserPromptSubmit'].append({'hooks': [{'type': 'command', 'command': 'twinlane hook'}]})
        with open(self.path, 'w', encoding='utf-8') as handle:
            json.dump(s, handle)
        rc, text = self.cli('--check', '--bin-dir', self.bin, '--skill', self.tmp)
        self.assertEqual(rc, 0)
        self.assertIn('registered 2 times', text)

    def test_check_finds_our_shims_and_rejects_mixed_flags(self):
        self.cli('--alias', 'tl', '--bin-dir', self.bin, '--yes')
        report = json.loads(self.cli('--check', '--json', '--bin-dir', self.bin, '--skill', self.tmp)[1])
        self.assertEqual(sorted(report['aliases']), ['tl'])
        self.assertTrue(all(f['ok'] for f in report['aliases']['tl'].values()))
        rc, _ = self.cli('--check', '--yes')
        self.assertEqual(rc, common.EXIT_USAGE)


class TestHookCommand(InstallCase):
    def test_hook_command_never_exits_2_when_the_script_is_missing(self):
        # python exits 2 for a missing script, and exit 2 from UserPromptSubmit blocks every prompt.
        bash = common.bash_bin()
        if not bash:
            self.skipTest('no bash')
        gone = os.path.join(self.tmp, 'gone', 'cli.py').replace('\\', '/')
        py = sys.executable.replace('\\', '/')
        rc, _ = self.cli('--hook-command', f'"{py}" "{gone}" hook', '--yes')
        self.assertEqual(rc, 0)
        registered = self.load()['hooks']['UserPromptSubmit'][-1]['hooks'][0]['command']
        self.assertTrue(registered.endswith('|| exit 0'))
        bare = registered.split(' || ')[0]
        self.assertEqual(subprocess.run([bash, '-c', bare], capture_output=True).returncode, 2)
        self.assertEqual(subprocess.run([bash, '-c', registered], capture_output=True).returncode, 0)

    def test_custom_hook_command_is_recognised_for_unhook(self):
        custom = 'mytool fire --prompt-hook'
        rc, out = self.cli('--hook-command', custom, '--yes')
        self.assertEqual(rc, 0)
        self.assertIn('mytool fire --prompt-hook || exit 0', out)
        s = self.load()
        self.assertEqual(install.count_our_hooks(s, custom), 1)
        self.assertEqual(install.count_our_hooks(s), 0)  # the default pattern does not know it
        # a second install with the same (or an already fail-open) spelling adds nothing
        rc, out = self.cli('--hook-command', custom + ' || exit 0', '--yes')
        self.assertIn('already registered', out)
        self.assertEqual(install.count_our_hooks(self.load(), custom), 1)
        rc, out = self.cli('--unhook', '--yes')  # without the command: not ours, kept
        self.assertIn('not registered', out)
        self.assertEqual(install.count_our_hooks(self.load(), custom), 1)
        rc, _ = self.cli('--unhook', '--hook-command', custom, '--yes')
        self.assertEqual(rc, 0)
        self.assertEqual(self.load(), self.ORIGINAL)

    def test_bad_hook_command_is_refused(self):
        rc, _ = self.cli('--hook-command', '   ', '--yes')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(self.load(), self.ORIGINAL)

    def test_hook_fails_open_before_setup(self):
        blocker = os.path.join(self.tmp, 'a-file')
        Path(blocker).write_text('x', encoding='utf-8')
        env = dict(os.environ, TWINLANE_HOME=os.path.join(blocker, 'not-a-dir'))  # a FILE as parent
        env.pop('TWINLANE_ENGINE_JOB', None)
        r = subprocess.run([sys.executable, CLI, 'hook'], input='{}', capture_output=True, text=True,
                           env=env, timeout=60)
        self.assertEqual((r.returncode, r.stdout, r.stderr), (0, '', ''))

    def test_job_env_is_silent(self):
        env = dict(os.environ, TWINLANE_HOME=os.path.join(self.tmp, 'home'), TWINLANE_ENGINE_JOB='job-1')
        r = subprocess.run([sys.executable, CLI, 'hook'], input=json.dumps({'hook_event_name': 'UserPromptSubmit'}),
                           capture_output=True, text=True, env=env, timeout=60)
        self.assertEqual((r.returncode, r.stdout), (0, ''))


class TestAlias(InstallCase):
    def read(self, name):
        with open(os.path.join(self.bin, name), 'rb') as handle:
            return handle.read()

    def test_alias_writes_crlf_cmd_and_lf_shell(self):
        rc, out = self.cli('--alias', 'tl,twl', '--bin-dir', self.bin)
        self.assertEqual(rc, 0)
        self.assertIn('Run with --yes', out)
        self.assertFalse(os.path.exists(self.bin))  # preview writes nothing
        self.assertEqual(self.load(), self.ORIGINAL)  # an alias action leaves the hook alone
        rc, out = self.cli('--alias', 'tl,twl', '--bin-dir', self.bin, '--yes')
        self.assertEqual(rc, 0)
        self.assertEqual(sorted(os.listdir(self.bin)), ['tl', 'tl.cmd', 'twl', 'twl.cmd'])
        self.assertEqual(self.load(), self.ORIGINAL)
        cmd, sh = self.read('tl.cmd'), self.read('tl')
        self.assertTrue(cmd.endswith(b'\r\n'))
        self.assertEqual(cmd.count(b'\n'), cmd.count(b'\r\n'))  # every line ends CRLF
        self.assertIn(b'%*', cmd)
        self.assertIn(os.path.abspath(common.TWINLANE_PY).encode('utf-8'), cmd)
        self.assertNotIn(b'\r', sh)  # bash rejects CRLF scripts
        self.assertTrue(sh.startswith(b'#!/bin/sh\n'))
        self.assertTrue(sh.endswith(b' "$@"\n'))
        self.assertIn(b'exec ', sh)
        if os.name != 'nt':
            self.assertTrue(os.access(os.path.join(self.bin, 'tl'), os.X_OK))
        rc, out = self.cli('--alias', 'tl', '--bin-dir', self.bin, '--yes')  # idempotent: no backups
        self.assertEqual(rc, 0)
        self.assertIn('up to date', out)
        self.assertEqual(self.backups(self.bin), [])

    def test_alias_shell_shim_runs_under_bash(self):
        bash = common.bash_bin()
        if not bash:
            self.skipTest('no bash')
        self.cli('--alias', 'tl', '--bin-dir', self.bin, '--yes')
        shim = os.path.join(self.bin, 'tl').replace('\\', '/')
        r = subprocess.run([bash, shim, '--help'], capture_output=True, text=True, encoding='utf-8', timeout=60)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn('twinlane', r.stdout)

    def test_alias_refuses_foreign_file_without_replace(self):
        os.makedirs(self.bin)
        foreign = os.path.join(self.bin, 'tl')
        Path(foreign).write_bytes(b'#!/bin/sh\necho mine\n')
        rc, _ = self.cli('--alias', 'tl', '--bin-dir', self.bin, '--yes')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('--replace', self.last_err)
        self.assertEqual(Path(foreign).read_bytes(), b'#!/bin/sh\necho mine\n')
        self.assertEqual(os.listdir(self.bin), ['tl'])  # nothing else was written either
        rc, _ = self.cli('--alias', 'tl', '--bin-dir', self.bin)  # a preview refuses too
        self.assertEqual(rc, common.EXIT_REFUSED)

    def test_alias_refuses_a_folder_even_with_replace(self):
        os.makedirs(os.path.join(self.bin, 'tl'))
        rc, _ = self.cli('--alias', 'tl', '--bin-dir', self.bin, '--replace', '--yes')
        self.assertEqual(rc, common.EXIT_REFUSED)

    def test_alias_replace_backs_up(self):
        os.makedirs(self.bin)
        foreign = os.path.join(self.bin, 'tl')
        Path(foreign).write_bytes(b'#!/bin/sh\necho mine\n')
        rc, out = self.cli('--alias', 'tl', '--bin-dir', self.bin, '--replace')  # preview
        self.assertEqual(rc, 0)
        self.assertEqual(Path(foreign).read_bytes(), b'#!/bin/sh\necho mine\n')
        rc, out = self.cli('--alias', 'tl', '--bin-dir', self.bin, '--replace', '--yes')
        self.assertEqual(rc, 0)
        self.assertIn('Backup:', out)
        backups = self.backups(self.bin)
        self.assertEqual(len(backups), 1)
        self.assertTrue(backups[0].startswith('tl.twinlane-backup-'))
        self.assertEqual(Path(self.bin, backups[0]).read_bytes(), b'#!/bin/sh\necho mine\n')
        self.assertIn(b'twinlane alias shim', Path(foreign).read_bytes())
        self.assertTrue(os.path.isfile(os.path.join(self.bin, 'tl.cmd')))

    def test_our_stale_shim_is_updated_with_a_backup_and_no_replace_flag(self):
        os.makedirs(self.bin)
        stale = os.path.join(self.bin, 'tl')
        Path(stale).write_bytes(b'#!/bin/sh\n# twinlane alias shim\nexec old-python old.py "$@"\n')
        rc, _ = self.cli('--alias', 'tl', '--bin-dir', self.bin, '--yes')
        self.assertEqual(rc, 0)
        self.assertEqual(len(self.backups(self.bin)), 1)
        self.assertNotIn(b'old-python', Path(stale).read_bytes())

    def test_invalid_alias_name_and_replace_without_alias(self):
        for bad in ('../x', 'a b', '1x', ''.join('a' for _ in range(40))):
            rc, _ = self.cli('--alias', bad, '--bin-dir', self.bin, '--yes')
            self.assertEqual(rc, common.EXIT_REFUSED, bad)
        self.assertFalse(os.path.exists(self.bin))
        rc, _ = self.cli('--replace')
        self.assertEqual(rc, common.EXIT_USAGE)


class TestSkill(InstallCase):
    def test_skill_preview_then_yes_with_backup(self):
        folder = os.path.join(self.tmp, 'skills', 'twinlane')
        os.makedirs(folder)
        target = os.path.join(folder, 'SKILL.md')
        Path(target).write_bytes(b'---\nname: old\n---\nold text\n')
        source = Path(install.SKILL_SOURCE).read_bytes()
        rc, out = self.cli('--skill', folder)
        self.assertEqual(rc, 0)
        self.assertIn('Run with --yes', out)
        self.assertEqual(Path(target).read_bytes(), b'---\nname: old\n---\nold text\n')
        self.assertEqual(self.backups(folder), [])
        rc, out = self.cli('--skill', folder, '--yes')
        self.assertEqual(rc, 0)
        self.assertIn('Backup:', out)
        self.assertEqual(Path(target).read_bytes(), source)
        backups = self.backups(folder)
        self.assertEqual(len(backups), 1)
        self.assertEqual(Path(folder, backups[0]).read_bytes(), b'---\nname: old\n---\nold text\n')
        self.assertEqual(self.load(), self.ORIGINAL)  # a skill action leaves the hook alone
        rc, out = self.cli('--skill', folder, '--yes')  # idempotent
        self.assertIn('up to date', out)
        self.assertEqual(len(self.backups(folder)), 1)

    def test_skill_into_a_new_folder_needs_no_backup(self):
        folder = os.path.join(self.tmp, 'new', 'deeper')
        rc, _ = self.cli('--skill', folder, '--yes')
        self.assertEqual(rc, 0)
        self.assertTrue(os.path.isfile(os.path.join(folder, 'SKILL.md')))
        self.assertEqual(self.backups(folder), [])

    def test_packaged_skill_is_generic_and_declared(self):
        text = Path(install.SKILL_SOURCE).read_text(encoding='utf-8')
        self.assertTrue(text.startswith('---\nname: twinlane\n'))
        self.assertNotIn('\r', text)
        for command in ('twinlane route', 'twinlane run', 'twinlane baton', 'twinlane handoff', 'twinlane board'):
            self.assertIn(command, text)
        pyproject = Path(ROOT, 'pyproject.toml').read_text(encoding='utf-8')
        self.assertIn('skill/SKILL.md', pyproject)

    def test_skill_and_alias_and_hook_combine_under_one_yes(self):
        folder = os.path.join(self.tmp, 'skill')
        rc, out = self.cli('--hook', '--alias', 'tl', '--skill', folder, '--bin-dir', self.bin, '--yes')
        self.assertEqual(rc, 0)
        self.assertEqual(install.count_our_hooks(self.load()), 1)
        self.assertTrue(os.path.isfile(os.path.join(self.bin, 'tl.cmd')))
        self.assertTrue(os.path.isfile(os.path.join(folder, 'SKILL.md')))


if __name__ == '__main__':
    unittest.main()
