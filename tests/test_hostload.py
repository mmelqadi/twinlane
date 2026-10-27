"""Cache-only host observations never invoke the measurement path."""
import os
import unittest
from unittest import mock

import test_support  # noqa: F401
import test_board as tb
from twinlane import common, hostload


class CachedHostloadTests(tb.BoardBase):
    def test_cached_returns_sample_and_age_without_refreshing(self):
        path = os.path.join(common.STATE, 'hostload.json')
        with mock.patch.object(hostload, 'measure', side_effect=AssertionError('no measurement')), \
                mock.patch('subprocess.Popen', side_effect=AssertionError('no process')), \
                mock.patch('subprocess.run', side_effect=AssertionError('no process')), \
                mock.patch('os.popen', side_effect=AssertionError('no process')):
            self.assertIsNone(hostload.cached(now=1000))
            for at, expected in ((900, 100), (0, 1000), (1000, 0)):
                reading = {'at': at, 'cpu_pct': None, 'ram_free_mb': None, 'engine_procs': None, 'heap': None}
                common.write_json(path, reading)
                self.assertEqual(hostload.cached(now=1000), (reading, expected))
                self.assertEqual(common.read_json(path), reading)
            for at in (None, True, '900', -1, 1001, float('nan'), float('inf')):
                common.write_json(path, {'at': at})
                self.assertIsNone(hostload.cached(now=1000))


class ConsoleHostGauge(unittest.TestCase):
    CALM = {'cpu_pct': 10.0, 'ram_free_mb': 9000, 'engine_procs': 2}

    def test_settings_validate_the_pair_like_the_process_pair(self):
        cfg = hostload.settings({'hostload': {'conhost_soft': 100, 'conhost_hard': 50}})
        self.assertEqual((cfg['conhost_soft'], cfg['conhost_hard']), (100, 101))
        cfg = hostload.settings(None)
        self.assertEqual((cfg['conhost_soft'], cfg['conhost_hard']), (80, 120))

    def test_console_hosts_shrink_the_slots_and_name_themselves(self):
        low = dict(self.CALM, console_hosts=40)
        self.assertEqual(hostload.effective_parallel(14, low), (14, None))
        mid = dict(self.CALM, console_hosts=100)  # halfway between soft and hard
        usable, why = hostload.effective_parallel(14, mid)
        self.assertEqual((usable, why), (7, 'host busy: 100 console hosts'))
        high = dict(self.CALM, console_hosts=158)  # the field incident: past the hard limit, never below min_parallel
        usable, why = hostload.effective_parallel(14, high)
        self.assertEqual(usable, hostload.settings(None)['min_parallel'])
        self.assertIn('158 console hosts', why)
        self.assertIn('158 console hosts', hostload.line(high, 14))

    def test_a_missing_count_never_throttles(self):
        for reading in (self.CALM, dict(self.CALM, console_hosts=None)):
            self.assertEqual(hostload.effective_parallel(14, reading), (14, None))


if __name__ == '__main__':
    unittest.main()
