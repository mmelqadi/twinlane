"""Test bootstrap ordering and marker preservation using credential-free synthetic child homes.

No existing job, private vendor directory or process is read. Default-path operations trip before IO.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import test_support  # disposable home before candidate imports

ROOT = Path(__file__).resolve().parents[1]
CONTEXT_PREFIXES = ('TWINLANE_ENGINE_', 'LEGACY_ENGINE_')
PROBE = r'''
import contextlib, ctypes, importlib, io, json, os, sys
from unittest import mock
sys.path[:0] = [sys.argv[1], os.path.join(sys.argv[1], 'tests')]
context = {k: v for k, v in os.environ.items() if k.startswith(('TWINLANE_ENGINE_', 'LEGACY_ENGINE_'))}
with mock.patch.object(ctypes, 'WinDLL', side_effect=AssertionError('native API forbidden'), create=True):
    if sys.argv[2] == 'common-first':
        from twinlane import common
        before = dict(os.environ)
        refused = False
        with mock.patch('tempfile.mkdtemp', side_effect=AssertionError('late bootstrap must not create a home')):
            try:
                importlib.import_module('test_support')
            except RuntimeError:
                refused = True
        print(json.dumps({'refused': refused, 'environment_unchanged': dict(os.environ) == before}))
        sys.exit(0)
    if sys.argv[2] == 'admission-first':
        importlib.import_module('test_admission')
        early = 'test_support' in sys.modules
    else:
        early = True
    support = importlib.import_module('test_support')
    from twinlane import common, cli
    isolated = os.path.normcase(os.path.abspath(support._HOME))
    def within(path):
        return os.path.commonpath([isolated, os.path.normcase(os.path.abspath(path))]) == isolated
    paths_match = common.HOME_DIR == support._HOME and all(within(getattr(common, key))
                        for key in ('STATE', 'JOBS', 'BATONS', 'BIN'))
    paths_match = paths_match and all(within(os.environ[key]) for key in
                                     ('TWINLANE_CONFIG', 'CLAUDE_CONFIG_DIR', 'CODEX_HOME'))
    calls = []
    def mkdir_tripwire(path, *args, **kwargs):
        if not within(path):
            raise AssertionError('default path is outside the disposable bootstrap home')
        calls.append(path)
    with mock.patch.object(os, 'makedirs', side_effect=mkdir_tripwire), contextlib.redirect_stdout(io.StringIO()):
        cli.main(['--help'])
    preserved = context == {k: v for k, v in os.environ.items()
                           if k.startswith(('TWINLANE_ENGINE_', 'LEGACY_ENGINE_'))}
    def inert_config(key):
        path = os.environ.get(key, '')
        if not path or not within(path) or not os.path.isfile(path):
            return False
        with open(path, encoding='utf-8') as handle:
            return handle.read() == ''
    template = os.environ.get('GIT_TEMPLATE_DIR', '')
    inert_git = all(inert_config(key) for key in ('GIT_CONFIG_SYSTEM', 'GIT_CONFIG_GLOBAL')) \
        and bool(template) and within(template) and os.path.isdir(template) and not os.listdir(template)
    print(json.dumps({'early_bootstrap': early, 'paths_match': paths_match,
                      'markers_preserved': preserved, 'mkdir_calls': len(calls),
                      'inert_git': inert_git,
                      'child_bytecode_disabled': os.environ.get('PYTHONDONTWRITEBYTECODE') == '1'}))
'''


class BootstrapIsolation(unittest.TestCase):
    def probe(self, entry):
        with tempfile.TemporaryDirectory(prefix='bootstrap-probe-', dir=test_support._HOME) as tmp:
            # Whitelist interpreter necessities; no inherited API credentials, proxy or vendor config.
            env = {k: v for k, v in os.environ.items()
                   if k.upper() in ('PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'PATHEXT')
                   or k.startswith(CONTEXT_PREFIXES)}
            env.update({k: tmp for k in ('HOME', 'USERPROFILE', 'LOCALAPPDATA', 'XDG_DATA_HOME',
                                         'TEMP', 'TMP', 'TWINLANE_HOME')})
            env.update({'CLAUDE_CONFIG_DIR': os.path.join(tmp, 'fake-claude'),
                        'CODEX_HOME': os.path.join(tmp, 'fake-codex'), 'PYTHONIOENCODING': 'utf-8'})
            # Synthetic byte-preservation sentinels; retain any real inherited markers unchanged.
            env.setdefault('TWINLANE_ENGINE_JOB', 'synthetic-bootstrap-sentinel')
            env.setdefault('LEGACY_ENGINE_JOB', 'synthetic-legacy-sentinel')
            env.setdefault('TWINLANE_ENGINE_CWD', tmp)
            proc = subprocess.run([sys.executable, '-B', '-c', PROBE, str(ROOT), entry],
                                  cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            return json.loads(proc.stdout)

    def test_earliest_admission_import_bootstraps_cached_paths_before_io(self):
        result = self.probe('admission-first')
        self.assertTrue(result['early_bootstrap'])
        self.assertTrue(result['paths_match'])
        self.assertTrue(result['markers_preserved'])
        self.assertEqual(result['mkdir_calls'], 4)

    def test_bootstrap_preserves_worker_markers(self):
        result = self.probe('support-first')
        self.assertTrue(result['paths_match'])
        self.assertTrue(result['markers_preserved'])
        self.assertEqual(result['mkdir_calls'], 4)

    def test_late_bootstrap_refuses_before_creating_home_or_changing_context(self):
        result = self.probe('common-first')
        self.assertTrue(result['refused'])
        self.assertTrue(result['environment_unchanged'])

    def test_child_git_configuration_is_owned_and_inert(self):
        self.assertTrue(self.probe('support-first')['inert_git'])

    def test_child_interpreters_inherit_no_bytecode_intent(self):
        self.assertTrue(self.probe('support-first')['child_bytecode_disabled'])


if __name__ == '__main__':
    unittest.main()
