"""Inert trust registry tests, using only disposable source files and state."""
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # noqa: E402
from twinlane import cli, common, guard, trust  # noqa: E402


class TrustTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='twinlane-trust-')
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.home = Path(self.temp.name)
        patch = mock.patch.object(common, 'STATE', str(self.home / 'state'))
        patch.start()
        self.addCleanup(patch.stop)
        self.source = self.home / 'sink.py'
        self.source.write_text('raise RuntimeError("must never execute")\n', encoding='utf-8')

    def call(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = trust.cli(list(args))
        return code, out.getvalue(), err.getvalue()

    def test_add_list_verify_remove_through_cli(self):
        self.assertEqual(self.call('list')[:2], (0, 'no trusted sinks\n'))
        self.assertFalse(Path(common.STATE).exists())
        with mock.patch.object(common, 'ensure_dirs'), redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['trust', 'add', 'sink', str(self.source)]), 0)
        row = json.loads(Path(trust._path()).read_text())['sink']
        self.assertEqual(set(row), {'name', 'path', 'sha256', 'trusted_at', 'by'})
        self.assertEqual(row['sha256'], hashlib.sha256(self.source.read_bytes()).hexdigest())
        self.assertIsNotNone(common.parse_time(row['trusted_at']))
        self.assertTrue(row['by'])
        self.assertEqual(trust.trusted_sink('sink'), str(self.source))
        self.assertIn('sink: trusted', self.call('list')[1])
        self.assertEqual(self.call('remove', 'sink')[0], 0)
        self.assertIsNone(trust.trusted_sink('sink'))
        self.assertEqual(self.call('remove', 'sink')[0], 0)

    def test_changed_and_missing_files_are_untrusted_until_explicit_retrust(self):
        self.call('add', 'sink', str(self.source))
        self.source.write_text('changed', encoding='utf-8')
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(trust.trusted_sink('sink'))
        self.assertIn('untrusted', err.getvalue())
        self.assertIn('UNTRUSTED', self.call('list')[1])
        self.call('add', 'sink', str(self.source))
        self.assertEqual(trust.trusted_sink('sink'), str(self.source))
        self.source.unlink()
        with redirect_stderr(io.StringIO()):
            self.assertIsNone(trust.trusted_sink('sink'))

    def test_invalid_registry_fails_closed_and_is_not_overwritten(self):
        Path(common.STATE).mkdir()
        for data in ('{', '[]', '{"sink": {}}', '{"../bad": {}}'):
            Path(trust._path()).write_text(data, encoding='utf-8')
            with redirect_stderr(io.StringIO()):
                self.assertIsNone(trust.trusted_sink('sink'))
            self.assertEqual(self.call('add', 'new', str(self.source))[0], common.EXIT_FAILED)
            self.assertEqual(self.call('remove', 'sink')[0], common.EXIT_FAILED)
            self.assertEqual(Path(trust._path()).read_text(), data)

    def test_add_failure_preserves_other_records_and_names_are_validated(self):
        self.call('add', 'sink', str(self.source))
        before = Path(trust._path()).read_bytes()
        self.assertEqual(self.call('add', 'other', str(self.home / 'missing'))[0], common.EXIT_FAILED)
        self.assertEqual(Path(trust._path()).read_bytes(), before)
        for name in ('../x', '', 'space name'):
            self.assertEqual(self.call('add', name, str(self.source))[0], common.EXIT_USAGE)
        self.assertIsNone(trust.trusted_sink('unknown'))

    def test_worker_guard_blocks_trust_mutations(self):
        for args in (['add', 'sink', 'file'], ['remove', 'sink']):
            self.assertIsNotNone(guard.self_hazard(['twinlane', 'trust', *args]))
            self.assertIsNotNone(guard.self_hazard(['python', '-m', 'twinlane', 'trust', *args]))
        self.assertIsNone(guard.self_hazard(['twinlane', 'trust', 'list']))

    def test_sha256_streams_large_files_and_writes_are_locked(self):
        self.source.write_bytes(b'x' * 200000)
        with mock.patch.object(common, 'file_lock', wraps=common.file_lock) as lock:
            self.assertEqual(self.call('add', 'sink', str(self.source))[0], 0)
        self.assertEqual(lock.call_count, 1)
        self.assertEqual(trust.trusted_sink('sink'), str(self.source))
        self.assertEqual(json.loads(Path(trust._path()).read_text())['sink']['sha256'],
                         hashlib.sha256(self.source.read_bytes()).hexdigest())


if __name__ == '__main__':
    unittest.main()
