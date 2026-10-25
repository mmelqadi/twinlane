"""Preserving start with an explicitly supplied standalone Codex binary.

Synthetic only: disposable homes, a synthetic binary file that is never executed, mocked children. Every
Codex discovery, copy, lease, prune, version probe, rollout read, provider read and network path trips.
"""
import builtins
import contextlib
import datetime as dt
import hashlib
import io
import json
import os
import shutil
import socket
import subprocess
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # disposable home before importing runtime
from twinlane import codex_runner, common, jobs, meter

RUNNER = os.path.join(os.path.dirname(os.path.abspath(jobs.__file__)), 'codex_runner.py')
QUALIFIED = 'TWINLANE_CODEX_BIN_QUALIFIED'
TRIPPED = ('_pick_codex_source', 'codex_source_bin', 'resolve_codex', 'acquire_codex', 'acquire_codex_binary',
           'codex_bin', 'codex_lease', '_private_codex_copy', '_take_lease', '_prune_codex_copies', '_make_room',
           '_newest_valid_copy', '_read_codex_version', '_build_info', '_bundle_sha256')


class PreservingCodexStartTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=test_support._HOME)
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.root = Path(self.temp.name)
        for name in ('jobs', 'state', 'local', 'codex-home', 'twinlane-bin', 'work', 'standalone', 'other'):
            (self.root / name).mkdir()
        self.store, self.state = self.root / 'jobs', self.root / 'state'
        self.cwd = str(self.root / 'work')
        (self.root / 'work' / 'README.md').write_text('synthetic work tree', encoding='utf-8')
        self.brief = self.root / 'brief.md'
        self.brief.write_text('Synthetic source-only task', encoding='utf-8')
        self.binary = self.root / 'local' / 'Programs' / 'OpenAI' / 'Codex' / 'bin' / 'codex.exe'
        self.binary.parent.mkdir(parents=True)
        self.binary.write_bytes(b'MZ synthetic standalone codex, never executed\n' * 8)
        self.other_binary = self.root / 'other' / 'codex.exe'
        self.other_binary.write_bytes(b'MZ a different synthetic standalone codex\n' * 8)
        # Protected roots: the desktop app's own folders and twinlane's shared private-copy store.
        self.app = self.root / 'local' / 'OpenAI' / 'Codex' / 'bin' / 'abc123' / 'codex.exe'
        self.plugin = self.root / 'codex-home' / 'plugins' / '.plugin-appserver' / 'codex.exe'
        self.shared = self.root / 'twinlane-bin' / 'codex-0.1.0-deadbeef' / 'codex.exe'
        for path in (self.app, self.plugin, self.shared):
            path.parent.mkdir(parents=True)
            path.write_bytes(b'MZ protected synthetic codex\n' * 8)

        self.stack = contextlib.ExitStack()
        self.addCleanup(self.stack.close)
        env = self.stack.enter_context(mock.patch.dict(os.environ, {
            'LOCALAPPDATA': str(self.root / 'local'), 'TWINLANE_CONFIG': str(self.root / 'absent-config.json')}))
        for name in ('TWINLANE_CODEX_BIN', 'TWINLANE_CODEX_RUN', QUALIFIED):
            env.pop(name, None)
        for name, value in (('JOBS', str(self.store)), ('STATE', str(self.state)), ('CODEX_RUN', None),
                            ('CODEX_DIR', str(self.root / 'codex-home')), ('BIN', str(self.root / 'twinlane-bin'))):
            self.stack.enter_context(mock.patch.object(common, name, value))
        self.tripped = []
        for name in TRIPPED:
            self.stack.enter_context(mock.patch.object(common, name, side_effect=self.trip(name)))
        self.stack.enter_context(mock.patch.object(shutil, 'which', side_effect=self.trip('PATH discovery')))
        self.stack.enter_context(mock.patch.object(meter, 'read', side_effect=self.trip('provider meter read')))
        self.stack.enter_context(mock.patch.object(meter, 'rollout_token_usage', side_effect=self.trip('rollout read')))
        self.stack.enter_context(mock.patch.object(common, '_k32', side_effect=self.trip('native probe')))
        self.stack.enter_context(mock.patch.object(socket.socket, 'connect', side_effect=self.trip('network')))
        self.stack.enter_context(mock.patch.object(subprocess, 'Popen', side_effect=self.trip('engine process')))
        self.stack.enter_context(mock.patch.object(common, 'pid_state', return_value=common.DEAD))
        self.stack.enter_context(mock.patch.object(jobs, '_pid_state', return_value=common.DEAD))
        self.stack.enter_context(mock.patch.object(jobs, '_self_created', return_value=12345))
        self.stack.enter_context(mock.patch.object(jobs, 'CONTAIN', False))
        self.policy = common.load_policy()

        # Foreign jobs: one running with a pending child marker and an env hand-off, one finished.
        self.running = self.store / '20260101-000000-codex-abcd'
        self.finished = self.store / '20260101-000001-claude-dcba'
        self.running.mkdir()
        self.finished.mkdir()
        self.put(self.running, 'job.json', {'id': self.running.name, 'cwd': self.cwd, 'profile': 'intel',
                                            'engine': 'codex', 'primary': {'engine': 'codex'}})
        self.put(self.running, 'status.json', {'status': 'running', 'engine': 'codex', 'pid': 101, 'pid_created': 9001})
        self.put(self.running, jobs.CHILD_STARTING, {'pid': 102, 'created': 9002, 'engine': 'codex'})
        self.put(self.running, jobs.HANDOFF, {'env': {'SYNTHETIC': 'only'}})
        os.utime(self.running / jobs.HANDOFF, (1, 1))
        self.put(self.finished, 'job.json', {'id': self.finished.name, 'cwd': self.cwd, 'engine': 'claude'})
        self.put(self.finished, 'status.json', {'status': 'done', 'engine': 'claude'})
        self.put(self.finished, 'result.json', {'id': self.finished.name, 'status': 'done'})

    # ------------------------------------------------------------ helpers

    def trip(self, name):
        def raise_(*args, **kwargs):
            self.tripped.append(name)
            raise AssertionError(f'tripwire: {name}')
        return raise_

    def put(self, folder, name, data):
        (folder / name).write_text(json.dumps(data), encoding='utf-8')

    def inventory(self, *roots):
        """Bytes and mtime of every file below the roots, and every directory entry: no lease/version/copy appears."""
        out = {}
        for root in roots:
            for path in sorted(Path(root).rglob('*')):
                out[str(path)] = (path.read_bytes(), path.stat().st_mtime_ns) if path.is_file() else 'dir'
        return out

    def foreign(self):
        return self.inventory(self.running, self.finished)

    def protected(self):
        # Programs/OpenAI/Codex/bin is the intentionally mutable standalone fixture,
        # distinct from the protected desktop cache in local/OpenAI.
        return self.inventory(self.root / 'local' / 'OpenAI', self.root / 'codex-home', self.root / 'twinlane-bin')

    @contextlib.contextmanager
    def protect_foreign(self):
        """Attempted mutations of foreign job folders trip even when runtime code would swallow the error."""
        attempted = []
        folders = (self.running.resolve(), self.finished.resolve())

        def foreign(path):
            return not isinstance(path, int) and any(Path(path).resolve().is_relative_to(f) for f in folders)

        def mutation(fn):
            def checked(*args, **kwargs):
                if any(foreign(p) for p in args[:2]):
                    attempted.append(args)
                    raise AssertionError('foreign metadata mutation')
                return fn(*args, **kwargs)
            return checked
        original_open = builtins.open

        def checked_open(path, mode='r', *args, **kwargs):
            if any(c in mode for c in 'wax+') and foreign(path):
                attempted.append((path, mode))
                raise AssertionError('foreign metadata write')
            return original_open(path, mode, *args, **kwargs)
        with mock.patch('builtins.open', side_effect=checked_open), \
                mock.patch.object(os, 'remove', side_effect=mutation(os.remove)), \
                mock.patch.object(os, 'unlink', side_effect=mutation(os.unlink)), \
                mock.patch.object(os, 'replace', side_effect=mutation(os.replace)), \
                mock.patch.object(os, 'rename', side_effect=mutation(os.rename)):
            yield
        self.assertEqual(attempted, [])

    def args(self, *extra, engine='codex'):
        return jobs._run_parser().parse_args(['--brief', str(self.brief), '--cd', self.cwd, '--kind', 'intel',
                                              '--engine', engine, '--preserve-existing-metadata', *extra])

    def plan(self, *extra, args=None):
        """`args`: arguments parsed beforehand, for a test that traps os.stat (argparse reads locale files)."""
        decision = {'engine': 'codex', 'refused': False, 'profile': 'intel', 'reason': 'synthetic'}
        with mock.patch.object(jobs, '_meter', return_value={}), \
                mock.patch.object(jobs, '_route', return_value=decision), \
                mock.patch.object(jobs, '_refresh_notice', return_value=None):
            return jobs.plan_run(args or self.args(*extra), self.policy)

    def explicit(self, value):
        return mock.patch.dict(os.environ, {'TWINLANE_CODEX_BIN': value})

    def cache(self, codex=None, read_age_s=0):
        now = common.now()
        payload = {'read_at': common.iso(now - dt.timedelta(seconds=read_age_s)),
                   'claude': {'status': 'ok', 'source': 'synthetic', 'max_pct': 10, 'reading_at': common.iso(now),
                              'extra_usage_enabled': False},
                   'codex': dict({'status': 'ok', 'source': 'synthetic', 'max_pct': 10, 'reading_at': common.iso(now),
                                  'on_credits': False}, **(codex or {}))}
        path = self.state / 'meter.json'
        path.write_text(json.dumps(payload), encoding='utf-8')
        return path

    def tamper_in_place(self, path):
        """Same size, same mtime, different bytes: only the content hash can see it."""
        st = path.stat()
        data = bytearray(path.read_bytes())
        data[-2] ^= 1
        path.write_bytes(bytes(data))
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))

    def fake_child(self, record):
        """A mocked child that leaves the runner's evidence; nothing is executed."""
        # This Popen has no kernel process/thread handle. Containment belongs to
        # the real-child tests; keep the native-probe tripwire active elsewhere.
        self.stack.enter_context(mock.patch.object(common, 'contain_engine'))
        def popen(argv, cwd=None, env=None, **kwargs):
            # Only the two hand-off variables: a failure message must never echo the rest of the environment.
            record.append({'argv': list(argv), 'env': {k: v for k, v in (env or {}).items()
                                                       if k in ('TWINLANE_CODEX_BIN', QUALIFIED)}})
            out = Path(argv[argv.index('--out') + 1])
            out.mkdir(parents=True, exist_ok=True)
            (out / 'last-message.md').write_text('synthetic final message', encoding='utf-8')
            (out / 'result.txt').write_text('exit=0 session=unknown model=m effort=e sandbox=read-only tokens=7\n',
                                            encoding='utf-8')
            proc = mock.Mock(pid=4242)
            proc.wait.return_value = 0
            proc.poll.return_value = 0
            return proc
        return popen

    def created_job(self):
        with self.explicit(str(self.binary)):
            plan = self.plan()
        jid = jobs._create_job(plan)
        return jid, common.read_json(jobs._p(jid, 'job.json'))

    # ------------------------------------------------------------ planning

    def test_explicit_standalone_binary_is_planned_with_its_recorded_identity(self):
        before, shielded = self.foreign(), self.protected()
        with self.explicit(str(self.binary)), self.protect_foreign():
            plan = self.plan()
        identity = plan['codex_binary']
        self.assertEqual(plan['engine'], 'codex')
        self.assertIs(plan['preserve_existing_metadata'], True)
        self.assertEqual(os.path.normcase(identity['path']), os.path.normcase(str(self.binary)))
        self.assertEqual(identity['sha256'], hashlib.sha256(self.binary.read_bytes()).hexdigest())
        self.assertEqual(identity['size'], self.binary.stat().st_size)
        self.assertIs(plan['failover'], False, 'a preserving Codex start must not fall back to another engine')
        self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))

    def test_arbitrary_explicit_binary_is_outside_the_approved_standalone_location(self):
        with self.explicit(str(self.other_binary)):
            with self.assertRaises(ValueError):common.qualify_explicit_codex_bin(str(self.other_binary))

    def test_approved_location_cannot_overlap_a_configured_shared_root(self):
        for key in ('BIN','CODEX_DIR'):
            with self.subTest(root=key), mock.patch.object(common,key,str(self.binary.parent)):
                with self.assertRaises(ValueError):common.qualify_explicit_codex_bin(str(self.binary))

    def test_absent_or_malformed_explicit_binary_refuses_without_discovery(self):
        missing = str(self.root / 'standalone' / 'missing.exe')
        values = [None, '', '   ', 'codex', 'codex.exe', os.path.join('standalone', 'codex.exe'),
                  str(self.binary) + '\n', str(self.binary) + '\t', '"' + str(self.binary) + '"',
                  ' ' + str(self.binary), str(self.root / 'standalone'), missing]
        before, shielded = self.foreign(), self.protected()
        for value in values:
            with self.subTest(value=value), self.protect_foreign():
                with mock.patch.dict(os.environ):
                    if value is None:
                        os.environ.pop('TWINLANE_CODEX_BIN', None)
                    else:
                        os.environ['TWINLANE_CODEX_BIN'] = value
                    with self.assertRaises(jobs.Refusal) as caught:
                        self.plan()
                self.assertIn('TWINLANE_CODEX_BIN', caught.exception.message)
        self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))

    def test_network_path_is_refused_before_any_filesystem_access(self):
        def no_fs(*args, **kwargs):
            raise AssertionError('filesystem access to a network path')
        args = self.args()
        with self.explicit(r'\\server.invalid\share\codex.exe'), \
                mock.patch.object(os, 'stat', side_effect=no_fs), mock.patch.object(os, 'lstat', side_effect=no_fs), \
                mock.patch.object(common, 'canon_path', side_effect=no_fs), \
                mock.patch.object(common, 'real_path', side_effect=no_fs):
            with self.assertRaises(jobs.Refusal) as caught:
                self.plan(args=args)
        self.assertIn('TWINLANE_CODEX_BIN', caught.exception.message)
        self.assertEqual(self.tripped, [])

    def test_protected_app_plugin_and_shared_bin_binaries_are_refused_untouched(self):
        before, shielded = self.foreign(), self.protected()
        for path in (self.app, self.plugin, self.shared):
            with self.subTest(path=path.parent.name), self.explicit(str(path)), self.protect_foreign():
                with self.assertRaises(jobs.Refusal) as caught:
                    self.plan()
                self.assertIn('protected', caught.exception.message)
        self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))

    def test_hard_link_to_a_protected_binary_is_refused(self):
        link = self.root / 'standalone' / 'hard-codex.exe'
        try:
            os.link(self.app, link)
        except OSError as e:
            self.skipTest(f'hard links unavailable here: {e}')
        shielded = self.protected()
        with self.explicit(str(link)):
            with self.assertRaises(jobs.Refusal) as caught:
                self.plan()
        self.assertIn('link', caught.exception.message)
        self.assertEqual((shielded, self.tripped), (self.protected(), []))

    def test_symlink_or_junction_into_a_protected_root_is_refused(self):
        cases = []
        if common.IS_WIN:
            import _winapi
            junction = self.root / 'standalone' / 'junction'
            _winapi.CreateJunction(str(self.app.parent), str(junction))
            self.addCleanup(os.rmdir, junction)  # removes the junction itself, before the temp tree goes
            cases.append(junction / 'codex.exe')
        symlink = self.root / 'standalone' / 'symlink-codex.exe'
        try:
            os.symlink(self.shared, symlink)
            cases.append(symlink)
        except (OSError, NotImplementedError):
            pass
        if not cases:
            self.skipTest('neither junctions nor symlinks can be created here')
        shielded = self.protected()
        for path in cases:
            with self.subTest(path=path.name), self.explicit(str(path)):
                with self.assertRaises(jobs.Refusal) as caught:
                    self.plan()
                self.assertIn('TWINLANE_CODEX_BIN', caught.exception.message)
        self.assertEqual((shielded, self.tripped), (self.protected(), []))

    def test_explicit_failover_or_adapter_runner_is_refused_at_plan(self):
        with self.explicit(str(self.binary)):
            with self.assertRaises(jobs.Refusal) as caught:
                self.plan('--failover')
            self.assertIn('fail', caught.exception.message)
            with mock.patch.object(common, 'CODEX_RUN', str(self.root / 'adapter.py')):
                with self.assertRaises(jobs.Refusal) as caught:
                    self.plan()
            self.assertIn('TWINLANE_CODEX_RUN', caught.exception.message)
        self.assertEqual(self.tripped, [])

    # ------------------------------------------------------------ preflight

    def test_changed_binary_environment_or_recorded_identity_refuses_at_preflight(self):
        jid, spec = self.created_job()
        params = spec['primary']
        tamper = self.binary.read_bytes()
        cases = {
            'bytes changed in place': lambda s: self.tamper_in_place(self.binary),
            'environment points elsewhere': lambda s: os.environ.__setitem__('TWINLANE_CODEX_BIN', str(self.other_binary)),
            'environment removed': lambda s: os.environ.pop('TWINLANE_CODEX_BIN'),
            'recorded identity missing': lambda s: s.pop('codex_binary'),
            'recorded identity malformed': lambda s: s.__setitem__('codex_binary', str(self.binary)),
            'recorded identity extra key': lambda s: s['codex_binary'].__setitem__('note', 'x'),
            'recorded hash altered': lambda s: s['codex_binary'].__setitem__('sha256', '0' * 64),
        }
        before = self.foreign()
        for name, change in cases.items():
            with self.subTest(case=name), self.explicit(str(self.binary)), self.protect_foreign():
                self.binary.write_bytes(tamper)
                spec_now = json.loads(json.dumps(spec))
                with mock.patch.object(jobs, '_meter') as metered:
                    change(spec_now)
                    with jobs.preserving_metadata(jid):
                        reason, _ = jobs.preflight(spec_now, params, self.policy, False)
                self.assertIsNotNone(reason)
                self.assertIn('preserving Codex', reason)
                metered.assert_not_called()
        self.assertEqual((before, self.tripped), (self.foreign(), []))

    def test_cross_engine_failover_is_refused_in_both_directions_before_meter(self):
        jid, spec = self.created_job()
        with mock.patch.object(jobs, '_meter') as metered, jobs.preserving_metadata(jid):
            reason, _ = jobs.preflight(spec, dict(spec['primary'], engine='claude'), self.policy, False)
            self.assertIn('fail', reason)
            claude = dict(spec, engine='claude', primary={'engine': 'claude'})
            claude.pop('codex_binary')
            reason, _ = jobs.preflight(claude, {'engine': 'codex'}, self.policy, False)
            self.assertIn('Claude only', reason)
        metered.assert_not_called()

    def test_cached_meter_freshness_unknown_and_paid_guards_are_unchanged(self):
        jid, spec = self.created_job()
        cases = {
            'fresh included usage': ({}, 0, None),
            'underlying reading stale': ({'reading_at': common.iso(common.now() - dt.timedelta(seconds=4000))}, 0, 'x'),
            'reading unknown': ({'status': 'unknown'}, 0, 'x'),
            'on paid credits': ({'on_credits': True}, 0, 'credits'),
            'cache envelope expired': ({}, 600, 'x'),
        }
        for name, (codex, read_age, expected) in cases.items():
            with self.subTest(case=name), self.explicit(str(self.binary)), mock.patch.object(jobs, '_read_meter', None):
                path = self.cache(codex, read_age)
                cached = path.read_bytes(), path.stat().st_mtime_ns
                with jobs.preserving_metadata(jid):
                    reason, _ = jobs.preflight(spec, spec['primary'], self.policy, fresh=True)
                if expected is None:
                    self.assertIsNone(reason)
                else:
                    self.assertIsNotNone(reason)
                    if expected != 'x':
                        self.assertIn(expected, reason)
                self.assertEqual(cached, (path.read_bytes(), path.stat().st_mtime_ns))
        self.assertEqual(self.tripped, [])

    # ------------------------------------------------------------ launch

    def test_actual_attempt_launches_only_the_qualified_binary_through_the_built_in_runner(self):
        jid, spec = self.created_job()
        executable_before=(self.binary.read_bytes(),self.binary.stat().st_mtime_ns)
        before, shielded = self.foreign(), self.protected()
        children = []
        with self.explicit(str(self.binary)), self.protect_foreign(), jobs.preserving_metadata(jid), \
                mock.patch.object(subprocess, 'Popen', side_effect=self.fake_child(children)), \
                mock.patch.object(common, 'process_created', return_value=777):
            attempt = jobs.run_attempt(jid, 1, spec['primary'], jobs._p(jid, 'brief.md'), spec, self.policy, None)
        self.assertEqual(attempt['status'], 'done', attempt)
        self.assertEqual(len(children), 1)
        argv, env = children[0]['argv'], children[0]['env']
        self.assertEqual(argv[:2], [sys.executable, RUNNER])
        self.assertIn('--preserve-existing-metadata',argv)
        self.assertEqual(attempt['argv'],argv)
        self.assertEqual(attempt['codex_bin'], spec['codex_binary']['path'])
        self.assertEqual(env['TWINLANE_CODEX_BIN'], spec['codex_binary']['path'])
        self.assertEqual(json.loads(env[QUALIFIED]), spec['codex_binary'])
        self.assertEqual(attempt['token_usage']['source'], 'built-in-runner')
        self.assertFalse(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))
        self.assertEqual(executable_before,(self.binary.read_bytes(),self.binary.stat().st_mtime_ns))
        self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))

    def test_attempt_refuses_tampered_binary_adapter_or_missing_identity_before_any_child(self):
        cases = ('tampered', 'adapter', 'missing identity', 'environment swapped')
        before, shielded = self.foreign(), self.protected()
        for name in cases:
            with self.subTest(case=name), self.explicit(str(self.binary)):
                self.binary.write_bytes(b'MZ synthetic standalone codex, never executed\n' * 8)
                jid, spec = self.created_job()
                patches = contextlib.ExitStack()
                if name == 'tampered':
                    self.tamper_in_place(self.binary)
                elif name == 'adapter':
                    patches.enter_context(mock.patch.object(common, 'CODEX_RUN', str(self.root / 'adapter.py')))
                elif name == 'missing identity':
                    spec.pop('codex_binary')
                else:
                    os.environ['TWINLANE_CODEX_BIN'] = str(self.other_binary)
                with patches, self.protect_foreign(), jobs.preserving_metadata(jid):
                    attempt = jobs.run_attempt(jid, 1, spec['primary'], jobs._p(jid, 'brief.md'), spec,
                                               self.policy, None)
                self.assertEqual(attempt['status'], 'refused', attempt)
                self.assertIn('preserving Codex', attempt['error'])
                self.assertFalse(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))
        self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))

    def test_built_in_runner_verifies_the_handoff_and_never_falls_back_to_path(self):
        jid, spec = self.created_job()
        identity = spec['codex_binary']
        out = self.root / 'runner-out'
        argv = ['--brief', str(self.brief), '--cd', self.cwd, '--model', 'm', '--effort', 'e',
                '--out', str(out), '--timeout', '5', '--preserve-existing-metadata']

        def run(env):
            launched = []

            def popen(args, **kwargs):
                launched.append(list(args))
                proc = mock.Mock(returncode=0, pid=4243)
                proc.communicate.return_value = (b'', b'')
                return proc
            with mock.patch.dict(os.environ, {k: v for k, v in env.items() if v is not None}), \
                    mock.patch.object(codex_runner.subprocess, 'Popen', side_effect=popen):
                for name in ('TWINLANE_CODEX_BIN', QUALIFIED):
                    if env.get(name) is None:
                        os.environ.pop(name, None)
                return codex_runner.run(argv), launched
        good = {'TWINLANE_CODEX_BIN': identity['path'], QUALIFIED: json.dumps(identity)}
        rc, launched = run(good)
        self.assertEqual(rc, 0)
        self.assertEqual([args[0] for args in launched], [identity['path']])
        refusals = {
            'handoff absent': dict(good, **{QUALIFIED: None}),
            'handoff and binary absent': {'TWINLANE_CODEX_BIN':None,QUALIFIED:None},
            'binary unset': dict(good, TWINLANE_CODEX_BIN=None),
            'binary elsewhere': dict(good, TWINLANE_CODEX_BIN=str(self.other_binary)),
            'malformed handoff': dict(good, **{QUALIFIED: '{not json'}),
            'handoff for another file': dict(good, **{QUALIFIED: json.dumps(dict(identity, sha256='0' * 64))}),
        }
        for name, env in refusals.items():
            with self.subTest(case=name):
                rc, launched = run(env)
                self.assertEqual((rc, launched), (common.EXIT_REFUSED, []))
        self.tamper_in_place(self.binary)
        rc, launched = run(good)
        self.assertEqual((rc, launched), (common.EXIT_REFUSED, []))
        self.assertEqual(self.tripped, [])

    def test_ordinary_codex_launch_still_acquires_normally_and_drops_a_stray_handoff(self):
        jid = '20260101-000009-codex-0001'
        os.makedirs(jobs.job_dir(jid))
        files = jobs._attempt_files(jid, 1)
        children = []
        with mock.patch.dict(os.environ, {QUALIFIED: '{"stray": true}'}), \
                mock.patch.object(common, 'acquire_codex_binary', return_value=(str(self.binary), '', None)) as acquire, \
                mock.patch.object(subprocess, 'Popen', side_effect=self.fake_child(children)), \
                mock.patch.object(common, 'process_created', return_value=777), \
                mock.patch.object(meter, 'rollout_token_usage', return_value=None) as rollout:
            argv = jobs.build_argv({'engine': 'codex', 'profile': 'intel', 'timeout_s': 5}, self.policy,
                                   str(self.brief), self.cwd, files['codex_out'])
            out = jobs._run_codex(jid, argv, self.cwd, 5, files, {'profile': 'intel'}, self.policy)
        self.assertEqual(out['status'], 'done', out)
        acquire.assert_called_once()
        rollout.assert_called_once()
        self.assertNotIn(QUALIFIED, children[0]['env'])
        self.assertEqual(children[0]['env']['TWINLANE_CODEX_BIN'], str(self.binary))

    def test_preserving_token_accounting_skips_rollouts_but_ordinary_reads_them(self):
        events = self.root / 'events.jsonl'
        events.write_text(json.dumps({'type': 'turn.completed', 'usage': {'input_tokens': 3, 'output_tokens': 4}}) + '\n')
        with jobs.preserving_metadata('20260101-000010-codex-0002'):
            got = jobs._codex_tokens('11111111-1111-1111-1111-111111111111', str(events), None)
        self.assertEqual(got, {'source': 'codex events', 'input_tokens': 3, 'output_tokens': 4})
        self.assertEqual(self.tripped, [])
        with mock.patch.object(meter, 'rollout_token_usage', return_value={'source': 'rollout'}) as rollout:
            self.assertEqual(jobs._codex_tokens('sid', str(events), None), {'source': 'rollout'})
        rollout.assert_called_once()

    # ------------------------------------------------------------ whole lifecycle

    def test_inline_cli_lifecycle_runs_only_the_new_job_and_preserves_foreign_metadata(self):
        self.cache()
        before, shielded = self.foreign(), self.protected()
        children = []
        with self.explicit(str(self.binary)), self.protect_foreign(), \
                mock.patch.object(jobs, '_read_meter', None), \
                mock.patch.object(jobs, '_coordination_parent', return_value=None), \
                mock.patch.object(jobs, 'tripwire_snapshot', return_value={}), \
                mock.patch.object(jobs, 'tripwire_diff', return_value={'touched_files': None, 'head_moved': None}), \
                mock.patch.object(common, 'is_linked_worktree', return_value=False), \
                mock.patch.object(common, 'process_created', return_value=777), \
                mock.patch.object(subprocess, 'Popen', side_effect=self.fake_child(children)):
            with contextlib.redirect_stdout(io.StringIO()) as shown, contextlib.redirect_stderr(io.StringIO()) as err:
                rc = jobs.cmd_run(['--brief', str(self.brief), '--cd', self.cwd, '--kind', 'intel', '--engine', 'codex',
                                   '--preserve-existing-metadata', '--json'])
        self.assertEqual(rc, common.EXIT_OK, shown.getvalue() + err.getvalue())
        self.assertEqual(len(children), 1)
        new = [p for p in self.store.iterdir() if p not in (self.running, self.finished)]
        self.assertEqual(len(new), 1)
        receipt = json.loads((new[0] / 'result.json').read_text(encoding='utf-8'))
        self.assertEqual(receipt['status'], 'done')
        spec = json.loads((new[0] / 'job.json').read_text(encoding='utf-8'))
        self.assertEqual(json.loads(children[0]['env'][QUALIFIED]), spec['codex_binary'])
        self.assertFalse((new[0] / jobs.RUNNER_LOCK).exists())
        self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))

    def test_detached_runner_entry_requires_the_planned_binary_in_its_handed_over_environment(self):
        """`twinlane _job JOB --preserve-existing-metadata` is what a --bg start runs after envhandoff.load()
        replaced os.environ with the caller's: the spec identity and that environment must still agree."""
        self.cache()
        for case in ('same binary', 'environment swapped after planning'):
            with self.subTest(case=case):
                jid, spec = self.created_job()
                common.write_json(jobs._p(jid, 'status.json'), {'status': 'queued', 'engine': 'codex'})
                before, shielded = self.foreign(), self.protected()
                children = []
                handed_over = str(self.binary) if case == 'same binary' else str(self.other_binary)
                with self.explicit(handed_over), self.protect_foreign(), \
                        mock.patch.object(jobs, '_read_meter', None), \
                        mock.patch.object(jobs, 'tripwire_snapshot', return_value={}), \
                        mock.patch.object(jobs, 'tripwire_diff', return_value={'touched_files': None, 'head_moved': None}), \
                        mock.patch.object(common, 'is_linked_worktree', return_value=False), \
                        mock.patch.object(common, 'process_created', return_value=777), \
                        mock.patch.object(subprocess, 'Popen', side_effect=self.fake_child(children)):
                    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                        rc = jobs.cmd_job([jid, '--preserve-existing-metadata'])
                receipt = common.read_json(jobs._p(jid, 'result.json'))
                if case == 'same binary':
                    self.assertEqual((rc, receipt['status'], len(children)), (common.EXIT_OK, 'done', 1))
                    self.assertEqual(json.loads(children[0]['env'][QUALIFIED]), spec['codex_binary'])
                else:
                    self.assertEqual((rc, receipt['status'], children), (common.EXIT_REFUSED, 'refused', []))
                    self.assertIn('preserving Codex', json.dumps(receipt))
                self.assertFalse(os.path.exists(jobs._p(jid, jobs.RUNNER_LOCK)))
                self.assertEqual((before, shielded, self.tripped), (self.foreign(), self.protected(), []))


if __name__ == '__main__':
    unittest.main()
