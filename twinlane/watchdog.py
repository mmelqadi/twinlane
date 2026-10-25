"""Hung-job watchdog: a worker that makes no progress for too long is killed so it can be requeued.

Progress is any growth or touch of the files a worker writes (event stream, stderr, runner log). A Codex
shell that hangs on its first command, or a model call that never returns, leaves them still. When nothing
has moved for `stall_s` the process tree is killed through `common.kill_tree` with the recorded creation
stamp, so a reused pid is never touched, and the attempt is reported as `stalled`. The runner (not this
module) decides what a stall means for the job: requeue once, then fail with "host overload". A stalled
attempt never carries a verdict, so a review that did not run reads as failed, never as PASS or BLOCK.

Config, in the user config under "watchdog": enabled (true), stall_s (900), poll_s (15).
"""
from __future__ import annotations

import math
import os
import subprocess
import time

from . import common

DEFAULTS = {'enabled': True, 'stall_s': 900, 'poll_s': 15}
STALL_REQUEUES = 1  # a first stall requeues the job; the second one fails it


def settings(policy: dict | None) -> dict:
    out = dict(DEFAULTS)
    raw = (policy or {}).get('watchdog')
    if isinstance(raw, dict):
        if type(raw.get('enabled')) is bool:
            out['enabled'] = raw['enabled']
        for key in ('stall_s', 'poll_s'):
            v = raw.get(key)
            if type(v) in (int, float) and math.isfinite(v) and v > 0:
                out[key] = v
    return out


def _signature(paths) -> tuple:
    sig = []
    for path in paths:
        try:
            st = os.stat(path)
            sig.append((st.st_size, st.st_mtime_ns))
        except OSError:
            sig.append(None)
    return tuple(sig)


class Stall:
    """Tracks one attempt's progress. `check()` is True once nothing has changed for `stall_s`."""

    def __init__(self, paths, stall_s: float, clock=time.monotonic):
        self.paths, self.stall_s, self.clock = list(paths), float(stall_s), clock
        self._sig = _signature(self.paths)
        self._moved = clock()
        self.tripped = False
        self.kill_confirmed = None  # None: no kill yet; True only when the tree kill itself reported success
        self.idle_s = 0.0

    def check(self) -> bool:
        sig, now = _signature(self.paths), self.clock()
        if sig != self._sig:
            self._sig, self._moved = sig, now
        self.idle_s = now - self._moved
        if self.idle_s >= self.stall_s:
            self.tripped = True
        return self.tripped

    def reason(self) -> str:
        return f'no output for {int(self.idle_s // 60)} min (limit {int(self.stall_s // 60)} min)'


def make(paths, policy: dict | None, clock=time.monotonic) -> Stall | None:
    cfg = settings(policy)
    return Stall(paths, cfg['stall_s'], clock) if cfg['enabled'] else None


def wait(proc, timeout_s: float, stall: Stall | None, poll_s: float = 15, kill=None, grace_s: float = 10,
         child_created=None):
    """`proc.wait(timeout_s)` that also kills the tree when `stall` trips.

    Returns the exit code. Raises subprocess.TimeoutExpired at the deadline, exactly like proc.wait, so the
    caller's timeout path is unchanged. On a stall the tree is killed, `stall.tripped` is set and the code
    is returned (None if the process outlived the kill; the caller records a survivor in that case).
    `stall.kill_confirmed` tells whether the tree kill reported success; the runner never retries on False."""
    if stall is None:
        return proc.wait(timeout=timeout_s)
    kill = kill or common.kill_tree
    deadline = time.monotonic() + timeout_s
    while True:
        left = deadline - time.monotonic()
        if left <= 0:
            raise subprocess.TimeoutExpired(getattr(proc, 'args', None), timeout_s)
        chunk, began = min(poll_s, left), time.monotonic()
        try:
            return proc.wait(timeout=chunk)
        except subprocess.TimeoutExpired:
            if time.monotonic() - began < chunk / 2:
                raise  # it gave up long before its time: it cannot be waited on in slices, so this is its deadline
        if stall.check():
            done = kill(proc.pid, child_created) if child_created is not None else kill(proc.pid)
            # The root exiting proves nothing about its children: only the kill's own report does.
            stall.kill_confirmed = done is True
            try:
                return proc.wait(timeout=grace_s)
            except subprocess.TimeoutExpired:
                return None
