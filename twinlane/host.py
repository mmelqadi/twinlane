"""Host facts: when the machine booted, and so whether a lost job died with a restart.

`boot_time()` is the only platform code. `lost_cause()` compares a job's start with it. Both are
advisory: a missing boot time makes the cause "unknown", never a guess.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time

from . import common

HOST_RESTARTED = 'host-restarted'
RUNNER_DIED = 'runner-died'
UNKNOWN = 'unknown'
# A job that started within this many seconds of the boot time is not called a restart victim: the boot
# time is computed from an uptime counter (and the job's start from another clock), so they only agree
# to about this much.
SKEW_S = 60
_UNSET = object()
_SYSCTL_BOOT = re.compile(r'sec\s*=\s*(\d+)')


def _windows_boot(now: float) -> float | None:
    import ctypes
    fn = ctypes.windll.kernel32.GetTickCount64
    fn.restype = ctypes.c_ulonglong
    return now - fn() / 1000.0


def _linux_boot() -> float | None:
    with open('/proc/stat', encoding='utf-8') as h:
        for line in h:
            if line.startswith('btime '):
                return float(line.split()[1])
    return None


def _macos_boot() -> float | None:
    out = subprocess.run(['sysctl', '-n', 'kern.boottime'], capture_output=True, text=True, timeout=5).stdout
    m = _SYSCTL_BOOT.search(out)
    return float(m.group(1)) if m else None


def boot_time() -> float | None:
    """Epoch seconds of the last boot, or None when this platform cannot say.

    Windows reads GetTickCount64, Linux /proc/stat, macOS sysctl kern.boottime. A value that is not in the
    past (or not plausibly an epoch) is rejected: None means "unknown", and callers must not infer a cause.
    Windows Fast Startup keeps the tick counter across a shutdown, so a "restart" that was a shutdown on
    such a host can read as an older boot; the cause is then an underestimate, never an overstatement."""
    now = time.time()
    try:
        if common.IS_WIN:
            value = _windows_boot(now)
        elif sys.platform == 'darwin':
            value = _macos_boot()
        elif sys.platform.startswith('linux'):
            value = _linux_boot()
        else:
            return None
    except (OSError, ValueError, AttributeError, ImportError, subprocess.SubprocessError):
        return None
    if value is None or not (86400 * 365 < value <= now):
        return None
    return float(value)


def _started(job) -> float | None:
    """Epoch seconds a job started: a mapping with started_at / created_at, or a time value."""
    if isinstance(job, dict):
        for key in ('started_at', 'created_at'):
            t = common.parse_time(job.get(key))
            if t is not None:
                return t.timestamp()
        return None
    t = common.parse_time(job)
    return None if t is None else t.timestamp()


def lost_cause(job, boot=_UNSET) -> str:
    """Why a lost job was lost: `host-restarted` when it started before the boot time minus SKEW_S,
    `runner-died` when it started after the host came up (so the runner itself died), `unknown` when the
    boot time or the job's start time cannot be read. `boot` is injected by tests; default boot_time()."""
    boot = boot_time() if boot is _UNSET else boot
    started = _started(job)
    if boot is None or started is None:
        return UNKNOWN
    return HOST_RESTARTED if started < boot - SKEW_S else RUNNER_DIED


def cli(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane host', description='Print this host\'s last boot time.')
    ap.add_argument('--json', action='store_true')
    try:
        args = ap.parse_args(list(argv))
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK
    boot = boot_time()
    iso = common.iso(common.parse_time(boot)) if boot is not None else None
    common.emit({'boot_time': boot, 'boot_time_iso': iso}, args.json,
                f'boot time: {iso}' if iso else 'boot time: unknown on this platform')
    return common.EXIT_OK if boot is not None else common.EXIT_MISSING
