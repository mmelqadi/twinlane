"""Strict Windows detach cannot trust an absent or unreadable job membership result."""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from twinlane import common


@unittest.skipUnless(common.IS_WIN, 'Windows')
class StrictWindowsDetach(unittest.TestCase):
    def test_failed_job_membership_query_is_unknown(self):
        api = mock.Mock()
        api.IsProcessInJob.return_value = 0
        with mock.patch.object(common, '_k32', return_value=api):
            self.assertEqual(common.job_info(),
                             {'in_job': None, 'kill_on_close': None, 'breakaway_ok': None})

    def test_strict_no_job_uses_broker(self):
        info = {'in_job': False, 'kill_on_close': False, 'breakaway_ok': True}
        with mock.patch.object(common, 'job_info', return_value=info), \
                mock.patch.object(common, '_spawn_wmi', return_value={'pid': 7, 'created': 8}) as broker, \
                mock.patch.object(common.subprocess, 'Popen') as direct:
            result = common.spawn_detached_ex(['fake-python'], require_survival=True, handoff='env.json')
        self.assertEqual((result['mode'], result['pid'], result['created']), ('wmi', 7, 8))
        broker.assert_called_once()
        direct.assert_not_called()

    def test_strict_no_job_refuses_without_handoff(self):
        info = {'in_job': False, 'kill_on_close': False, 'breakaway_ok': True}
        with mock.patch.object(common, 'job_info', return_value=info), \
                mock.patch.object(common.subprocess, 'Popen') as direct:
            with self.assertRaises(common.DetachError):
                common.spawn_detached_ex(['fake-python'], require_survival=True)
        direct.assert_not_called()

    def test_strict_no_job_refuses_broker_failure(self):
        info = {'in_job': False, 'kill_on_close': False, 'breakaway_ok': True}
        with mock.patch.object(common, 'job_info', return_value=info), \
                mock.patch.object(common, '_spawn_wmi', side_effect=common.DetachError('pre-create failure')), \
                mock.patch.object(common.subprocess, 'Popen') as direct:
            with self.assertRaises(common.DetachError):
                common.spawn_detached_ex(['fake-python'], require_survival=True, handoff='env.json')
        direct.assert_not_called()


if __name__ == '__main__':
    unittest.main(verbosity=2)
