"""Invocation-local file and path observations. Never writes or survives a poll."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import os
import re

from . import common

_current = ContextVar('twinlane_observations', default=None)

# Readers historically accepted arbitrary slug suffixes. Keep those jobs visible
# (including unreadable/live records); only the archive requires canonical hex ids.
# The timestamp/engine shape excludes archive and unrelated directories entirely.
JOB_DIRECTORY_RE = re.compile(r'^\d{8}-\d{6}-(?:claude|codex)-[A-Za-z0-9-]+$')


class Snapshot:
    def __init__(self):
        self.cache = {}

    def once(self, key, read):
        if key not in self.cache:
            self.cache[key] = read()
        return self.cache[key]

    def directories(self):
        def scan():
            try:
                with os.scandir(common.JOBS) as entries:
                    return {e.name: e for e in entries if JOB_DIRECTORY_RE.fullmatch(e.name) and e.is_dir()}
            except FileNotFoundError:
                return {}
        return self.once(('directories', common.JOBS), scan)


@contextmanager
def scope():
    """Nested collectors share a snapshot; the next command/poll gets a new one."""
    if _current.get() is not None:
        yield _current.get()
        return
    token = _current.set(Snapshot())
    try:
        yield _current.get()
    finally:
        _current.reset(token)


def command(fn):
    @wraps(fn)
    def wrapped(*args, **kwargs):
        with scope():
            return fn(*args, **kwargs)
    return wrapped


def once(key, read):
    snapshot = _current.get()
    return snapshot.once(key, read) if snapshot is not None else read()


def active():
    return _current.get() is not None


def job_ids():
    snapshot = _current.get()
    if snapshot is None:
        with scope():
            return job_ids()
    return snapshot.once(('job-ids', common.JOBS), lambda: sorted(snapshot.directories(), reverse=True))


def job_json(jid, name='job.json', default=None):
    value = once(('job-file', common.JOBS, jid, name),
                 lambda: common.read_json(os.path.join(common.JOBS, jid, name)))
    return default if value is None else value


def worktree_top(path):
    # No realpath in the cache key: canonicalising is precisely the expensive work.
    return once(('top', os.fspath(path)), lambda: common.worktree_top(path))


def writer_key(path):
    return once(('writer', os.fspath(path)), lambda: os.path.normcase(
        os.path.realpath(worktree_top(path) or os.path.abspath(path))))


def activity(policy):
    """One recent-job index, keyed by baton and writer, shared by every lane.

    Creating/replacing status/result files updates the directory mtime. Old folders
    need only cheap lifecycle-file stats to also catch in-place status updates.
    Liveness/admission scans never use this age filter.
    """
    stale_s = (policy.get('lanes') or {}).get('stale_after_s', 86400)
    def build():
        cutoff = common.now().timestamp() - stale_s
        by_slug, by_writer = {}, {}
        snapshot = _current.get()
        for jid, entry in snapshot.directories().items():
            try:
                recent = entry.stat().st_mtime >= cutoff
            except OSError:
                continue
            # Stat before opening a spec; old lifecycle updates must still count.
            stamps = []
            for name in ('job.json', 'status.json', 'result.json'):
                try:
                    stamp = os.path.getmtime(os.path.join(entry.path, name))
                    stamps.append(stamp)
                    recent = recent or stamp >= cutoff
                except OSError:
                    pass
            # Directory mtime is a discovery hint, never activity evidence:
            # collecting a result or touching a notice also changes it.
            if not recent:
                continue
            stamp = max(stamps, default=0)
            if stamp < cutoff:
                continue
            spec = job_json(jid)
            if not isinstance(spec, dict):
                continue
            slug, cwd = spec.get('baton'), spec.get('writer_root') or spec.get('cwd')
            if isinstance(slug, str):
                by_slug[slug] = max(by_slug.get(slug, stamp), stamp)
            if isinstance(cwd, str) and cwd:
                key = common.writer_key(cwd)
                by_writer[key] = max(by_writer.get(key, stamp), stamp)
        return by_slug, by_writer
    return once(('activity', common.JOBS, stale_s), build)
