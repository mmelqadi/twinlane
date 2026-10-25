"""Bounded snapshot parsing and handle-pinned Windows kills, with no host process probes in fakes."""
import ctypes
import os
import struct
import subprocess
import sys
import time
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate homes before importing twinlane
from twinlane import common


def snapshot(rows, width=8):
    result = bytearray()
    pid_at, fmt = (0x50, '<Q') if width == 8 else (0x44, '<I')
    for index, (pid, parent, stamp) in enumerate(rows):
        entry = bytearray(0x100)
        struct.pack_into('<I', entry, 0, 0x100 if index + 1 < len(rows) else 0)
        struct.pack_into('<q', entry, 0x20, stamp)
        struct.pack_into(fmt, entry, pid_at, pid)
        struct.pack_into(fmt, entry, pid_at + width, parent)
        result.extend(entry)
    return bytes(result)


class SnapshotBounds(unittest.TestCase):
    def test_impossible_thread_count_invalidates_the_whole_snapshot(self):
        for width in (4, 8):
            for index in (0, 1):
                with self.subTest(width=width, entry=index):
                    bad = bytearray(snapshot([(42, 1, 100), (43, 42, 101)], width))
                    struct.pack_into('<I', bad, index * 0x100 + 4, 0xFFFFFFFF)
                    self.assertIsNone(common._snapshot_created_from(bytes(bad), 42, width))

    def test_thread_payload_must_fit_its_entry_in_both_pointer_widths(self):
        for width, header, stride in ((4, 0xB8, 0x40), (8, 0x100, 0x50)):
            for terminal in (False, True):
                with self.subTest(width=width, terminal=terminal):
                    size = header + stride
                    entry = bytearray(snapshot([(42, 1, 100)], width))
                    entry = entry[:size] + bytearray(max(0, size - len(entry)))
                    struct.pack_into('<I', entry, 4, 1)
                    if not terminal:
                        struct.pack_into('<I', entry, 0, size)
                        entry.extend(snapshot([(43, 42, 101)], width))
                    self.assertEqual(common._snapshot_created_from(bytes(entry), 42, width), 100)
                    struct.pack_into('<I', entry, 4, 2)
                    self.assertIsNone(common._snapshot_created_from(bytes(entry), 42, width))

    def test_full_fixed_header_is_required_even_with_zero_threads(self):
        for width, header in ((4, 0xB8), (8, 0x100)):
            with self.subTest(width=width):
                data = snapshot([(42, 1, 100)], width)
                self.assertEqual(common._snapshot_created_from(data[:header], 42, width), 100)
                self.assertIsNone(common._snapshot_created_from(data[:header - 1], 42, width))

    def test_matching_entry_must_validate_its_offset_and_header(self):
        for width in (4, 8):
            good = snapshot([(42, 1, 100)], width)
            for name, step in [('backwards', 0xFFFFFF00), ('past end', 0x200),
                               ('header overlap', 1), ('next header truncated', 0x100)]:
                with self.subTest(width=width, case=name):
                    bad = bytearray(good)
                    struct.pack_into('<I', bad, 0, step)
                    self.assertIsNone(common._snapshot_created_from(bytes(bad), 42, width))
            with self.subTest(width=width, case='truncated header'):
                self.assertIsNone(common._snapshot_created_from(good[:0x48 if width == 8 else 0x44], 42, width))

    def test_a_later_malformed_entry_invalidates_an_earlier_match(self):
        bad = bytearray(snapshot([(42, 1, 100), (43, 42, 101)]))
        struct.pack_into('<I', bad, 0x100, 0xFFFFFF00)
        self.assertIsNone(common._snapshot_created_from(bytes(bad), 42, 8))

    def test_zero_offset_terminates_instead_of_looping_or_reading_trailing_entries(self):
        bad = bytearray(snapshot([(41, 1, 99), (42, 41, 100)]))
        struct.pack_into('<I', bad, 0, 0)
        self.assertIsNone(common._snapshot_created_from(bytes(bad), 42, 8))
        self.assertEqual(common._snapshot_created_from(bytes(bad), 41, 8), 99)

    def test_dense_forward_walk_is_bounded_and_requires_a_terminator(self):
        rows = [(pid, 1, 100) for pid in range(1, 100)]
        bad = bytearray(snapshot(rows))
        struct.pack_into('<I', bad, len(bad) - 0x100, 0x100)
        self.assertIsNone(common._snapshot_created_from(bytes(bad), 1, 8))


class SnapshotQueryGrowth(unittest.TestCase):
    def test_growth_then_success_preserves_bytes(self):
        data = snapshot([(42, 1, 100)])
        sizes = []

        def query(info, buf, size, needed):
            self.assertEqual(info, 5)
            sizes.append(size)
            if len(sizes) == 1:
                needed._obj.value = size + 4096
                return common._STATUS_INFO_LENGTH_MISMATCH
            ctypes.memmove(buf, data, len(data))
            needed._obj.value = len(data)
            return 0

        self.assertEqual(common._win_process_snapshot(query), data)
        self.assertEqual(sizes, [1 << 20, 2 << 20])

    def test_repeated_mismatch_hits_attempt_cap(self):
        query = mock.Mock(return_value=common._STATUS_INFO_LENGTH_MISMATCH)
        with mock.patch.object(common, '_SNAPSHOT_MAX_ATTEMPTS', 3):
            self.assertIsNone(common._win_process_snapshot(query))
        self.assertEqual(query.call_count, 3)

    def test_repeated_mismatch_hits_size_cap(self):
        query = mock.Mock(return_value=common._STATUS_INFO_LENGTH_MISMATCH)
        self.assertIsNone(common._win_process_snapshot(query))
        self.assertEqual(query.call_count, 7)
        self.assertLessEqual(max(call.args[2] for call in query.call_args_list), common._SNAPSHOT_MAX_BYTES)

    def test_oversize_request_and_other_ntstatus_give_no_reading(self):
        def too_large(info, buf, size, needed):
            needed._obj.value = common._SNAPSHOT_MAX_BYTES + 1
            return common._STATUS_INFO_LENGTH_MISMATCH

        for query in (too_large, mock.Mock(return_value=0xC0000022), mock.Mock(side_effect=OSError)):
            with self.subTest(query=query):
                self.assertIsNone(common._win_process_snapshot(query))

    def test_success_cannot_claim_bytes_outside_the_buffer(self):
        def query(info, buf, size, needed):
            needed._obj.value = size + 1
            return 0
        self.assertIsNone(common._win_process_snapshot(query))

    def test_tree_snapshot_requires_the_current_process_self_check(self):
        data = snapshot([(os.getpid(), 1, 100), (42, 1, 101)], ctypes.sizeof(ctypes.c_void_p))
        with mock.patch.object(common, '_win_process_snapshot', return_value=data), \
                mock.patch.object(common, '_win_own_created', return_value=100):
            self.assertEqual(common._win_process_tree_snapshot(), [(os.getpid(), 1, 100), (42, 1, 101)])
        with mock.patch.object(common, '_win_process_snapshot', return_value=data), \
                mock.patch.object(common, '_win_own_created', return_value=99):
            self.assertIsNone(common._win_process_tree_snapshot())


class ProcessHandleIdentity(unittest.TestCase):
    def test_native_queries_read_pid_parent_and_sequence_from_the_same_handle(self):
        handle, calls = object(), []

        def query(target, info, out, size, needed):
            self.assertIs(target, handle)
            calls.append(info)
            needed._obj.value = size
            if info == 0:
                self.assertEqual(size, 6 * ctypes.sizeof(ctypes.c_void_p))
                out._obj[4], out._obj[5] = 43, 42
            elif info == 92:
                self.assertEqual(size, 8)
                out._obj.value = 11
            else:
                self.fail('unexpected query')
            return 0

        self.assertEqual(common._win_process_identity(handle, object(), query), (43, 42, 11))
        self.assertEqual(calls, [0, 92])

    def test_failed_or_truncated_native_queries_give_no_identity(self):
        for failed_class in (0, 92):
            for failure in ('status', 'short', 'oversize', 'raise'):
                with self.subTest(info=failed_class, failure=failure):
                    def query(handle, info, out, size, needed):
                        if info == failed_class and failure == 'raise':
                            raise OSError('query')
                        needed._obj.value = size
                        if info == failed_class and failure in ('short', 'oversize'):
                            needed._obj.value += -1 if failure == 'short' else 1
                        if info == 0:
                            out._obj[4], out._obj[5] = 43, 42
                        else:
                            out._obj.value = 11
                        return 0xC0000003 if info == failed_class and failure == 'status' else 0
                    self.assertIsNone(common._win_process_identity(object(), object(), query))

    def test_fake_kernel_without_identity_reader_never_uses_native_queries(self):
        with mock.patch.object(ctypes, 'WinDLL', side_effect=AssertionError('native API'), create=True):
            self.assertIsNone(common._win_process_identity(object(), object()))


class KillKernel:
    """Each open returns a distinct object, which is the only accepted termination target."""
    def __init__(self, stamps=None, fail=None, identities=None):
        self.stamps = stamps or {42: 100, 43: 101, 44: 102}
        self.identities = identities if identities is not None else {
            42: (42, 1, 10), 43: (43, 42, 11), 44: (44, 43, 12)}
        self.fail = fail
        self.opened, self.closed, self.terminated, self.access = [], [], [], []

    def OpenProcess(self, access, inherit, pid):
        self.access.append((pid, access, inherit))
        if self.fail == ('open_raise', pid):
            raise OSError('open')
        if self.fail == ('open', pid):
            return None
        handle = (pid, self.stamps[pid], object(), self.identities.get(pid))
        self.opened.append(handle)
        return handle

    def GetProcessTimes(self, handle, created, *unused):
        if self.fail == ('times_raise', handle[0]):
            raise OSError('times')
        created._obj.value = handle[1]
        return self.fail != ('times', handle[0])

    def process_identity(self, handle):
        assert handle in self.opened and handle not in self.closed
        if self.fail == ('identity_raise', handle[0]):
            raise OSError('identity')
        return None if self.fail == ('identity', handle[0]) else handle[3]

    def TerminateProcess(self, handle, code):
        assert handle in self.opened and handle not in self.closed
        # A numeric pid now names something else. Only the pinned object may be terminated.
        self.stamps[handle[0]] += 1000
        self.terminated.append(handle)
        if self.fail == ('terminate_raise', handle[0]):
            raise OSError('terminate')
        return self.fail != ('terminate', handle[0])

    def CloseHandle(self, handle):
        self.closed.append(handle)
        if self.fail == ('close_raise', handle[0]):
            raise OSError('close')
        return True


class HandlePinnedKill(unittest.TestCase):
    DEFAULT_ROWS = object()

    def test_clock_rollback_does_not_admit_an_orphan_of_a_reused_root_pid(self):
        # A (sequence 10, pid 42) created B at wall stamp 200, then exited.
        # After rollback, unrelated C (sequence 30) reuses pid 42 at stamp 100.
        kernel = KillKernel(stamps={42: 100, 43: 200},
                            identities={42: (42, 1, 30), 43: (43, 42, 20)})
        result = self.kill(kernel, rows=[(42, 1, 100), (43, 42, 200)])
        self.assertEqual([h[0] for h in kernel.terminated], [42])
        self.assertFalse(result)

    def test_a_genuine_child_born_after_rollback_uses_sequence_order(self):
        kernel = KillKernel(stamps={42: 100, 43: 99})
        self.assertFalse(self.kill(kernel, rows=[(42, 1, 100), (43, 42, 99)]))
        self.assertEqual([h[0] for h in kernel.terminated], [43, 42])

    def test_unknown_root_sequence_kills_only_verified_root(self):
        for failure in ('identity', 'identity_raise'):
            with self.subTest(failure=failure):
                kernel = KillKernel(fail=(failure, 42))
                self.assertFalse(self.kill(kernel))
                self.assertEqual([h[0] for h in kernel.terminated], [42])

    def test_child_handle_parent_and_sequence_must_prove_membership(self):
        for identity in ((43, 99, 11), (99, 42, 11), (43, 42, 10),
                         (43, 42, 0), (43, 42, True), None):
            with self.subTest(identity=identity):
                kernel = KillKernel(identities={42: (42, 1, 10), 43: identity})
                self.assertFalse(self.kill(kernel))
                self.assertEqual([h[0] for h in kernel.terminated], [42])

    def test_unknown_child_creation_stamp_refuses_its_subtree(self):
        kernel = KillKernel()
        self.assertFalse(self.kill(kernel, rows=[(42, 1, 100), (43, 42, 0), (44, 43, 102)]))
        self.assertEqual([h[0] for h in kernel.terminated], [42])

    def kill(self, kernel, stamp=100, rows=DEFAULT_ROWS, snapshot_error=False, before_snapshot=None):
        if rows is self.DEFAULT_ROWS:
            rows = [(42, 1, 100), (43, 42, 101), (44, 43, 102)]

        def read_snapshot():
            if snapshot_error:
                raise OSError('snapshot')
            if before_snapshot:
                before_snapshot()
            return rows

        with mock.patch.object(common, 'IS_WIN', True), \
                mock.patch.object(common, '_k32', return_value=kernel), \
                mock.patch.object(common, 'pid_alive', return_value=True), \
                mock.patch.object(common, '_win_process_tree_snapshot', side_effect=read_snapshot, create=True), \
                mock.patch.object(common.subprocess, 'run', return_value=mock.Mock(returncode=0)) as by_pid, \
                mock.patch.object(common.os, 'kill', side_effect=AssertionError('numeric pid kill')):
            result = common.kill_tree(42, stamp)
        by_pid.assert_not_called()
        self.assertCountEqual(kernel.closed, kernel.opened)
        self.assertEqual(len(kernel.closed), len(kernel.opened))
        return result

    def test_kill_uses_the_same_handle_that_supplied_the_identity(self):
        kernel = KillKernel()
        self.assertFalse(self.kill(kernel))
        self.assertEqual([h[0] for h in kernel.terminated], [44, 43, 42])
        self.assertEqual(kernel.access, [(p, 0x101001, False) for p in (42, 43, 44)])

    def test_pid_reuse_between_verification_and_termination_cannot_change_the_target(self):
        kernel = KillKernel()
        def replace_pid():
            kernel.stamps[42] = 900
        self.assertFalse(self.kill(kernel, before_snapshot=replace_pid))
        self.assertEqual(kernel.terminated[-1], kernel.opened[0])
        self.assertEqual(kernel.terminated[-1][1], 100)

    def test_root_identity_mismatch_kills_nothing(self):
        kernel = KillKernel()
        self.assertFalse(self.kill(kernel, 99))
        self.assertEqual(kernel.terminated, [])
        self.assertEqual([h[0] for h in kernel.opened], [42])

    def test_unopenable_or_unreadable_root_kills_nothing(self):
        for failure in ('open', 'open_raise', 'times', 'times_raise'):
            with self.subTest(failure=failure):
                kernel = KillKernel(fail=(failure, 42))
                self.assertFalse(self.kill(kernel))
                self.assertEqual(kernel.terminated, [])

    def test_reused_parent_pid_does_not_admit_an_older_child_or_its_subtree(self):
        kernel = KillKernel(stamps={42: 100, 43: 99, 44: 102},
                            identities={42: (42, 1, 10), 43: (43, 42, 9), 44: (44, 43, 12)})
        rows = [(42, 1, 100), (43, 42, 99), (44, 43, 102)]
        self.assertFalse(self.kill(kernel, rows=rows))
        self.assertEqual([h[0] for h in kernel.terminated], [42])

    def test_each_generation_checks_its_parent_sequence(self):
        kernel = KillKernel(stamps={42: 100, 43: 101, 44: 100},
                            identities={42: (42, 1, 10), 43: (43, 42, 11), 44: (44, 43, 10)})
        rows = [(42, 1, 100), (43, 42, 101), (44, 43, 100)]
        self.assertFalse(self.kill(kernel, rows=rows))
        self.assertEqual([h[0] for h in kernel.terminated], [43, 42])

    def test_recycled_child_after_snapshot_is_skipped_and_reports_failure(self):
        kernel = KillKernel(stamps={42: 100, 43: 999, 44: 102})
        self.assertFalse(self.kill(kernel))
        self.assertEqual([h[0] for h in kernel.terminated], [42])

    def test_an_inaccessible_child_refuses_its_unproven_subtree(self):
        kernel = KillKernel(fail=('open', 43))
        self.assertFalse(self.kill(kernel))
        self.assertEqual([h[0] for h in kernel.terminated], [42])

    def test_child_failures_close_every_handle_and_still_terminate_root(self):
        for failure in ('open', 'open_raise', 'times', 'times_raise', 'identity', 'identity_raise',
                        'terminate', 'terminate_raise'):
            with self.subTest(failure=failure):
                kernel = KillKernel(fail=(failure, 43))
                self.assertFalse(self.kill(kernel))
                self.assertIn(42, [h[0] for h in kernel.terminated])

    def test_close_exception_does_not_leak_remaining_handles(self):
        self.assertFalse(self.kill(KillKernel(fail=('close_raise', 44))))

    def test_root_termination_failures_still_close_every_handle(self):
        for failure in ('terminate', 'terminate_raise'):
            with self.subTest(failure=failure):
                self.assertFalse(self.kill(KillKernel(fail=(failure, 42))))

    def test_a_failed_child_does_not_block_its_sibling(self):
        kernel = KillKernel(fail=('open', 43),
                            identities={42: (42, 1, 10), 43: (43, 42, 11), 44: (44, 42, 12)})
        self.assertFalse(self.kill(kernel, rows=[(42, 1, 100), (43, 42, 101), (44, 42, 102)]))
        self.assertEqual([h[0] for h in kernel.terminated], [44, 42])

    def test_snapshot_exception_closes_root_without_killing_anything(self):
        kernel = KillKernel()
        self.assertFalse(self.kill(kernel, snapshot_error=True))
        self.assertEqual(kernel.terminated, [])

    def test_untrusted_or_stale_snapshot_kills_only_verified_root_and_reports_failure(self):
        for rows in (None, [], [(42, 1, 99), (43, 42, 101)]):
            with self.subTest(rows=rows):
                kernel = KillKernel()
                self.assertFalse(self.kill(kernel, rows=rows))
                self.assertEqual([h[0] for h in kernel.terminated], [42])


@unittest.skipUnless(common.IS_WIN, 'Windows handle-based tree termination')
class RealWindowsTreeKill(unittest.TestCase):
    def start_tree(self):
        # The root launches its own child and prints the child's pid for an independently pinned observer.
        code = ('import subprocess, sys, time; '
                'p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"]); '
                'print(p.pid, flush=True); time.sleep(60)')
        root = subprocess.Popen([sys.executable, '-u', '-c', code], stdout=subprocess.PIPE, text=True)
        self.addCleanup(root.stdout.close)
        self.addCleanup(lambda: root.wait(timeout=10))
        self.addCleanup(lambda: root.kill() if root.poll() is None else None)
        child_pid = int(root.stdout.readline())
        kernel = common._k32()
        handle = kernel.OpenProcess(0x101001, False, child_pid)
        self.assertTrue(handle)

        def cleanup_child():
            try:
                kernel.TerminateProcess(handle, 1)
            finally:
                kernel.CloseHandle(handle)
        self.addCleanup(cleanup_child)
        return root, child_pid, common.process_created(root.pid), common.process_created(child_pid)

    def test_right_identity_terminates_root_and_child(self):
        root, child, stamp, child_stamp = self.start_tree()
        self.assertFalse(common.kill_tree(root.pid, stamp))  # snapshot-only: unconfirmed
        root.wait(timeout=10)
        deadline = time.monotonic() + 10
        while common.pid_maybe_alive(child, child_stamp) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(common.pid_maybe_alive(child, child_stamp))

    def test_wrong_identity_leaves_root_and_child_alive(self):
        root, child, stamp, child_stamp = self.start_tree()
        self.assertFalse(common.kill_tree(root.pid, stamp + 1))
        self.assertIsNone(root.poll())
        self.assertTrue(common.pid_alive(child, child_stamp))
