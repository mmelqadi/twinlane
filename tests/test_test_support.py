"""The suite's own teardown helper rides out transient Windows delete errors and nothing else."""
import errno
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # disposable home before importing runtime


def _transient(winerror):
    e = OSError(errno.ENOTEMPTY, 'directory is not empty')
    e.winerror = winerror
    return e


class RmtreeRetry(unittest.TestCase):
    def test_retries_directory_not_empty_then_removes(self):
        path = tempfile.mkdtemp(dir=test_support._HOME)
        Path(path, 'f.txt').write_text('x', encoding='utf-8')
        real = test_support.shutil.rmtree
        calls = []

        def flaky(p, **kw):
            calls.append(p)
            if len(calls) < 3:
                raise _transient(145)
            return real(p, **kw)

        with mock.patch.object(test_support.shutil, 'rmtree', side_effect=flaky), \
                mock.patch.object(test_support.time, 'sleep'):
            test_support.rmtree_retry(path)
        self.assertEqual(len(calls), 3)
        self.assertFalse(os.path.exists(path))

    def test_gives_up_after_the_last_attempt(self):
        with mock.patch.object(test_support.shutil, 'rmtree', side_effect=_transient(32)), \
                mock.patch.object(test_support.os.path, 'lexists', return_value=True), \
                mock.patch.object(test_support.time, 'sleep'):
            with self.assertRaises(OSError):
                test_support.rmtree_retry('unused', attempts=3)

    def test_other_errors_are_not_retried(self):
        boom = OSError(errno.EINVAL, 'bad name')
        with mock.patch.object(test_support.shutil, 'rmtree', side_effect=boom) as rm, \
                mock.patch.object(test_support.os.path, 'lexists', return_value=True):
            with self.assertRaises(OSError):
                test_support.rmtree_retry('unused')
        self.assertEqual(rm.call_count, 1)

    def test_cleanup_tempdir_falls_back_to_retry(self):
        temp = tempfile.TemporaryDirectory(dir=test_support._HOME)
        with mock.patch.object(temp, 'cleanup', side_effect=_transient(145)), \
                mock.patch.object(test_support, 'rmtree_retry') as retry:
            test_support.cleanup_tempdir(temp)
        retry.assert_called_once_with(temp.name)
        temp.cleanup()


if __name__ == '__main__':
    unittest.main()
