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


if __name__ == '__main__':
    unittest.main()
