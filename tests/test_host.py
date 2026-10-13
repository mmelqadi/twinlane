"""twinlane.host: boot time and the lost-job cause. Platform readers are mocked; nothing is probed twice."""
import io
import os
import sys
import time
import unittest
from contextlib import redirect_stdout
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import test_support  # noqa: E402,F401; isolated temp home before importing twinlane
from twinlane import common, host  # noqa: E402


class BootTimeTests(unittest.TestCase):
    def test_boot_time_past_or_none(self):
        value = host.boot_time()
        self.assertTrue(value is None or (isinstance(value, float) and value <= time.time()), value)
        if value is not None:
            self.assertGreater(value, 86400 * 365)  # a plausible epoch, not an uptime counter
        # Anything that is not in the past, or not an epoch, is unknown rather than believed.
        future = time.time() + 3600
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'linux'), \
                mock.patch.object(host, '_linux_boot', return_value=future):
            self.assertIsNone(host.boot_time())
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'linux'), \
                mock.patch.object(host, '_linux_boot', return_value=12345.0):
            self.assertIsNone(host.boot_time())
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'linux'), \
                mock.patch.object(host, '_linux_boot', side_effect=OSError('no /proc')):
            self.assertIsNone(host.boot_time())
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'plan9'):
            self.assertIsNone(host.boot_time())

    def test_platform_readers(self):
        past = time.time() - 7200
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'linux'), \
                mock.patch.object(host, '_linux_boot', return_value=past):
            self.assertEqual(host.boot_time(), past)
        with mock.patch.object(host.common, 'IS_WIN', True), \
                mock.patch.object(host, '_windows_boot', return_value=past):
            self.assertEqual(host.boot_time(), past)
        out = '{ sec = %d, usec = 5 } Tue Oct 13 10:00:00 2026\n' % int(past)
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'darwin'), \
                mock.patch.object(host.subprocess, 'run', return_value=mock.Mock(stdout=out)):
            self.assertEqual(host.boot_time(), float(int(past)))
        with mock.patch.object(host.common, 'IS_WIN', False), mock.patch.object(host.sys, 'platform', 'darwin'), \
                mock.patch.object(host.subprocess, 'run', return_value=mock.Mock(stdout='garbage')):
            self.assertIsNone(host.boot_time())


class LostCauseTests(unittest.TestCase):
    BOOT = 1_800_000_000.0

    def at(self, offset_s):
        return common.iso(common.parse_time(self.BOOT + offset_s))

    def test_restart_runner_and_unknown(self):
        cause = host.lost_cause
        self.assertEqual(cause({'started_at': self.at(-3600)}, self.BOOT), host.HOST_RESTARTED)
        self.assertEqual(cause({'created_at': self.at(-3600)}, self.BOOT), host.HOST_RESTARTED)
        self.assertEqual(cause({'started_at': self.at(600)}, self.BOOT), host.RUNNER_DIED)
        # Inside the clock-skew band around the boot a restart is not claimed.
        self.assertEqual(cause({'started_at': self.at(-30)}, self.BOOT), host.RUNNER_DIED)
        self.assertEqual(cause({'started_at': self.at(-host.SKEW_S - 5)}, self.BOOT), host.HOST_RESTARTED)
        # No boot time, or no start time: say so, never guess.
        self.assertEqual(cause({'started_at': self.at(-3600)}, None), host.UNKNOWN)
        self.assertEqual(cause({}, self.BOOT), host.UNKNOWN)
        self.assertEqual(cause({'started_at': 'not a time'}, self.BOOT), host.UNKNOWN)

    def test_default_boot_is_read_from_the_host(self):
        with mock.patch.object(host, 'boot_time', return_value=self.BOOT) as bt:
            self.assertEqual(host.lost_cause({'started_at': self.at(-3600)}), host.HOST_RESTARTED)
            bt.assert_called_once_with()
        with mock.patch.object(host, 'boot_time', return_value=None):
            self.assertEqual(host.lost_cause({'started_at': self.at(-3600)}), host.UNKNOWN)


class CliTests(unittest.TestCase):
    def test_cli_prints_boot_time_or_says_unknown(self):
        buf = io.StringIO()
        with mock.patch.object(host, 'boot_time', return_value=1_800_000_000.0), redirect_stdout(buf):
            self.assertEqual(host.cli(['--json']), common.EXIT_OK)
        self.assertIn('"boot_time": 1800000000.0', buf.getvalue())
        buf = io.StringIO()
        with mock.patch.object(host, 'boot_time', return_value=None), redirect_stdout(buf):
            self.assertEqual(host.cli([]), common.EXIT_MISSING)
        self.assertIn('unknown', buf.getvalue())


if __name__ == '__main__':
    unittest.main()
