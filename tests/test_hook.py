"""hook.py (UserPromptSubmit warning) and install.py (settings.json hook edits). No network, no model calls."""
import datetime as dt
import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, hook  # noqa: E402

from twinlane import install  # noqa: E402


def iso_in(seconds: float) -> str:
    return common.iso(common.now() + dt.timedelta(seconds=seconds))


def engine(status, pct, window='five_hour', reset_in=3600, **extra):
    e = {'status': status, 'max_pct': pct, 'next_reset': iso_in(reset_in),
         'windows': [{'name': window, 'pct': pct, 'resets_at': iso_in(reset_in)}]}
    e.update(extra)
    return e


def meter(claude, codex, read_ago=10):
    return {'read_at': iso_in(-read_ago), 'claude': claude, 'codex': codex}


class FakeStdin(io.StringIO):
    def isatty(self):
        return False


class SpawnRecorder:
    def __init__(self):
        self.calls = []

    def __call__(self, argv, cwd=None, env=None, log_path=None):
        self.calls.append({'argv': argv, 'cwd': cwd, 'env': env})
        return 4242


class HookCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-hook-')
        self.spawn = SpawnRecorder()
        # Never read the real state/meter.json: an uninjected meter comes from this temp path.
        self.cache_file = os.path.join(self.tmp, 'meter.json')
        self._orig_cache_path = hook.cache_path
        hook.cache_path = lambda: self.cache_file

    def tearDown(self):
        hook.cache_path = self._orig_cache_path
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, m, payload=None, env=None, policy=None):
        out = io.StringIO()
        stdin = FakeStdin(json.dumps(payload if payload is not None else
                                     {'hook_event_name': 'UserPromptSubmit', 'prompt': 'hi'}))
        rc = hook.main(stdin=stdin, out=out, meter=m, policy=policy if policy is not None else {},
                       state_dir=self.tmp, spawn=self.spawn, env=env if env is not None else {})
        self.assertEqual(rc, 0)
        return out.getvalue()


class TestHookMessages(HookCase):
    def test_silent_when_both_ok_or_caution(self):
        self.assertEqual(self.call(meter(engine('ok', 10), engine('caution', 77, 'weekly'))), '')

    def test_warns_at_stop_with_valid_hook_json(self):
        text = self.call(meter(engine('stop', 86), engine('ok', 30, 'weekly')))
        payload = json.loads(text)
        out = payload['hookSpecificOutput']
        self.assertEqual(out['hookEventName'], 'UserPromptSubmit')
        ctx = out['additionalContext']
        self.assertTrue(ctx.startswith('[twinlane] Claude 5h 86% (stop) resets '))
        self.assertIn('Codex weekly 30% ok', ctx)
        self.assertIn('twinlane route <kind>', ctx)
        self.assertIn('twinlane baton snap <slug>', ctx)
        self.assertIn('twinlane handoff <slug>', ctx)

    def test_warns_at_handoff_without_handoff_command(self):
        ctx = json.loads(self.call(meter(engine('ok', 5), engine('handoff', 82, 'weekly'))))
        ctx = ctx['hookSpecificOutput']['additionalContext']
        self.assertIn('Codex weekly 82% (handoff)', ctx)
        self.assertNotIn('twinlane handoff', ctx)

    def test_out_engine_warns(self):
        self.assertIn('(out)', self.call(meter(engine('out', 100), engine('ok', 1, 'weekly'))))

    def test_top_window_is_reported(self):
        c = engine('stop', 88)
        c['windows'] = [{'name': 'five_hour', 'pct': 12}, {'name': 'seven_day', 'pct': 88}]
        self.assertIn('Claude 7d 88% (stop)', self.call(meter(c, engine('ok', 1, 'weekly'))))

    def test_passed_reset_suppresses_stale_warning(self):
        self.assertEqual(self.call(meter(engine('stop', 90, reset_in=-60), engine('ok', 3, 'weekly'))), '')

    def test_credits_out_survives_passed_reset(self):
        cx = engine('out', 100, 'weekly', reset_in=-60, on_credits=True)
        self.assertIn('[on paid credits]', self.call(meter(engine('ok', 1), cx)))

    def test_spend_control_reached_is_flagged_and_survives_passed_reset(self):
        cx = engine('out', 40, 'weekly', reset_in=-60, spend_control_reached=True)
        self.assertIn('[spend control reached]', self.call(meter(engine('ok', 1), cx)))

    def test_extra_usage_flagged(self):
        text = self.call(meter(engine('stop', 20, extra_usage_enabled=True), engine('ok', 1, 'weekly')))
        self.assertIn('extra usage ON', text)

    def test_unknown_alone_is_silent(self):
        self.assertEqual(self.call(meter({'status': 'unknown', 'windows': []}, None)), '')

    def test_arabic_and_odd_window_names_do_not_break_output(self):
        c = engine('stop', 90, window='نافذة')
        text = self.call(meter(c, engine('ok', 1, 'weekly')))
        self.assertIn('\\u0646', text)  # ascii-escaped JSON stays valid on any console code page
        self.assertIn('نافذة', json.loads(text)['hookSpecificOutput']['additionalContext'])


class TestHookSilenceAndSafety(HookCase):
    def test_twinlane_job_env_is_silent_and_spawns_nothing(self):
        stale = meter(engine('out', 100), engine('out', 100, 'weekly'), read_ago=9999)
        self.assertEqual(self.call(stale, env={'TWINLANE_ENGINE_JOB': '20261008-x'}), '')
        self.assertEqual(self.spawn.calls, [])

    def test_other_event_is_silent(self):
        m = meter(engine('stop', 90), engine('ok', 1, 'weekly'))
        self.assertEqual(self.call(m, payload={'hook_event_name': 'Stop'}), '')

    def test_missing_event_name_still_works(self):
        self.assertIn('(stop)', self.call(meter(engine('stop', 90), engine('ok', 1, 'weekly')), payload={}))

    def test_garbage_meter_and_stdin_exit_zero_silently(self):
        for bad in ('nonsense', 42, [], {'claude': 'x', 'codex': 7}, {'claude': {'windows': 'x', 'status': 'stop'}}):
            out = io.StringIO()
            rc = hook.main(stdin=FakeStdin('{not json'), out=out, meter=bad, policy={}, state_dir=self.tmp,
                           spawn=self.spawn, env={})
            self.assertEqual(rc, 0)

    def test_exception_inside_is_swallowed(self):
        def boom(*a, **k):
            raise RuntimeError('spawn failed')
        out = io.StringIO()
        rc = hook.main(stdin=FakeStdin('{}'), out=out, meter=None, policy={}, state_dir=self.tmp,
                       spawn=boom, env={})
        self.assertEqual((rc, out.getvalue()), (0, ''))

    def test_slow_stdin_does_not_block(self):
        class Hanging(FakeStdin):
            def read(self, *a):
                time.sleep(5)
                return '{}'
        t0 = time.monotonic()
        self.assertEqual(hook.read_stdin_json(Hanging(''), wait_s=0.2), {})
        self.assertLess(time.monotonic() - t0, 1.0)


class TestHookRefresh(HookCase):
    def test_fresh_cache_spawns_nothing(self):
        self.call(meter(engine('ok', 1), engine('ok', 1, 'weekly'), read_ago=30))
        self.assertEqual(self.spawn.calls, [])

    def test_stale_cache_spawns_one_detached_fresh_meter_and_uses_old_value(self):
        m = meter(engine('stop', 90), engine('ok', 1, 'weekly'), read_ago=400)
        text = self.call(m, env={'TWINLANE_ENGINE_JOB': ''})
        self.assertIn('(stop)', text)
        self.assertIn('min old; refreshing', text)
        self.assertEqual(len(self.spawn.calls), 1)
        argv = self.spawn.calls[0]['argv']
        self.assertEqual(argv[1:], [common.TWINLANE_PY, 'meter', '--fresh', '--json'])
        self.assertNotIn('--refresh', argv)  # --refresh would spend a Claude call
        self.assertNotIn('TWINLANE_ENGINE_JOB', self.spawn.calls[0]['env'])

    def test_refresh_is_throttled(self):
        stale = meter(engine('ok', 1), engine('ok', 1, 'weekly'), read_ago=400)
        self.call(stale)
        self.call(stale)
        self.call(None)
        self.assertEqual(len(self.spawn.calls), 1)

    def test_missing_cache_spawns_and_stays_silent(self):
        self.assertEqual(self.call(None), '')
        self.assertEqual(len(self.spawn.calls), 1)

    def test_policy_max_age_is_honoured(self):
        m = meter(engine('ok', 1), engine('ok', 1, 'weekly'), read_ago=100)
        self.call(m, policy={'meter': {'hook_cache_max_age_s': 60}})
        self.assertEqual(len(self.spawn.calls), 1)

    def test_reads_cache_file_when_no_meter_injected(self):
        common.write_json(self.cache_file, meter(engine('handoff', 81), engine('ok', 1, 'weekly')))
        text = self.call(None)
        self.assertIn('Claude 5h 81% (handoff)', text)
        self.assertEqual(self.spawn.calls, [])


class TestHookSubprocess(unittest.TestCase):
    """End to end through cli.py with an isolated TWINLANE_HOME: exit 0, fast, valid output."""

    def run_twinlane(self, stdin_text, extra_env):
        tmp = tempfile.mkdtemp(prefix='twinlane-hook-e2e-')
        try:
            env = dict(os.environ, TWINLANE_HOME=tmp, PYTHONIOENCODING='utf-8', **extra_env)
            env.pop('TWINLANE_ENGINE_JOB', None)
            env.update(extra_env)
            os.makedirs(os.path.join(tmp, 'state'))
            common.write_json(os.path.join(tmp, 'state', 'meter.json'),
                              meter(engine('stop', 87), engine('ok', 2, 'weekly')))
            # The marker makes the hook skip the background refresh, so nothing outlives the test.
            common.write_json(os.path.join(tmp, 'state', 'hook-refresh.json'), {'spawned_at': iso_in(0)})
            t0 = time.monotonic()
            p = subprocess.run([sys.executable, os.path.join(ROOT, 'twinlane', 'cli.py'), 'hook'], input=stdin_text,
                               capture_output=True, text=True, encoding='utf-8', env=env, timeout=20)
            return p, time.monotonic() - t0
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_warns_and_exits_zero(self):
        p, _ = self.run_twinlane(json.dumps({'hook_event_name': 'UserPromptSubmit', 'prompt': 'مرحبا'}), {})
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn('(stop)', json.loads(p.stdout)['hookSpecificOutput']['additionalContext'])

    def test_job_env_prints_nothing(self):
        p, _ = self.run_twinlane('{}', {'TWINLANE_ENGINE_JOB': 'job-1'})
        self.assertEqual((p.returncode, p.stdout), (0, ''))


# ---------------------------------------------------------------- install.py

class TestInstallSettings(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix='twinlane-install-', dir=os.environ['TWINLANE_HOME'])
        self.path = os.path.join(self.tmp, 'settings.json')
        self.original = {'model': 'sonnet', 'hooks': {'PreToolUse': [{'hooks': [{'command': 'other'}]}]}}
        with open(self.path, 'w', encoding='utf-8') as out:
            json.dump(self.original, out)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def call(self, *args):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = install.main(['--settings', self.path, *args])
        return rc, out.getvalue()

    def test_preview_does_not_write(self):
        before = Path(self.path).read_text(encoding='utf-8')
        rc, output = self.call()
        self.assertEqual(rc, 0)
        self.assertIn('Run with --yes', output)
        self.assertEqual(Path(self.path).read_text(encoding='utf-8'), before)
        self.assertEqual(len(os.listdir(self.tmp)), 1)

    def test_yes_adds_hook_and_backup(self):
        rc, output = self.call('--yes')
        self.assertEqual(rc, 0)
        with open(self.path, encoding='utf-8') as source:
            settings = json.load(source)
        self.assertEqual(install.count_our_hooks(settings), 1)
        self.assertEqual(settings['model'], 'sonnet')
        self.assertEqual(settings['hooks']['PreToolUse'], self.original['hooks']['PreToolUse'])
        self.assertIn('Backup:', output)
        self.assertEqual(len(os.listdir(self.tmp)), 2)

    def test_yes_creates_new_settings_without_a_backup(self):
        os.remove(self.path)
        rc, output = self.call('--yes')
        self.assertEqual(rc, 0)
        self.assertEqual(install.count_our_hooks(install.load_settings(Path(self.path))), 1)
        self.assertNotIn('Backup:', output)
        self.assertEqual(len(os.listdir(self.tmp)), 1)

    def test_repeated_yes_is_idempotent(self):
        self.call('--yes')
        rc, output = self.call('--yes')
        self.assertEqual(rc, 0)
        self.assertIn('already registered', output)
        self.assertEqual(len(os.listdir(self.tmp)), 2)

    def test_invalid_json_is_refused_untouched(self):
        with open(self.path, 'w', encoding='utf-8') as out:
            out.write('{bad')
        rc, _ = self.call('--yes')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(Path(self.path).read_text(encoding='utf-8'), '{bad')
        self.assertEqual(len(os.listdir(self.tmp)), 1)

    def test_settings_override(self):
        with mock.patch.dict(os.environ, {'TWINLANE_SETTINGS_PATH': self.path}):
            self.assertEqual(install.default_settings_path(), self.path)

    def test_hook_match_is_specific(self):
        self.assertTrue(install.is_our_hook({'command': 'twinlane hook || exit 0'}))
        self.assertFalse(install.is_our_hook({'command': 'other hook'}))


if __name__ == '__main__':
    unittest.main()
