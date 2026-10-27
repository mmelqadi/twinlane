"""Opt-in, disposable job index. Folders and native PID probes remain authoritative.

Only unchanged terminal evidence can avoid folder reads. Never use indexed PIDs,
engine or dedupe keys to admit work. SQLite is imported lazily, only when enabled.
"""
from __future__ import annotations

from contextlib import closing
import json
import os
from pathlib import Path
import stat
import tempfile

from . import common, observations

VERSION = 1
BUSY_S = 0.05
COLUMNS = ('id', 'engine', 'kind', 'status', 'phase', 'pid', 'pid_created', 'cwd',
           'dedupe_key', 'updated_at', 'ended_at', 'status_stamp', 'result_stamp')
MARKERS = ('survivor.json', 'child-starting.json', 'tree-unresolved.json')


def path():
    return os.path.join(common.STATE, 'jobs.db')


def dirty_path():
    return path() + '.dirty'


def names():
    """Names only: the index can never hide a folder it has not seen."""
    try:
        return sorted((n for n in os.listdir(common.JOBS)
                       if observations.JOB_DIRECTORY_RE.fullmatch(n)), reverse=True)
    except FileNotFoundError:
        return []


def _stamp(file):
    try:
        info = os.stat(file)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise OSError('job evidence is not a regular file')
    return json.dumps((info.st_mtime_ns, info.st_size, info.st_ino))


def _dirty():
    try:
        os.lstat(dirty_path())
        return True
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _mark_dirty():
    # One token per failure. Rebuild removes only tokens that preceded its scan;
    # a concurrent failed writer must never have its warning cleared by rebuild.
    for _ in range(2):
        try:
            os.makedirs(dirty_path(), exist_ok=True)
            fd, _ = tempfile.mkstemp(prefix='failure-', dir=dirty_path())
            os.close(fd)
            return
        except FileExistsError:
            return  # an existing plain-file dirty marker already blocks all reads
        except FileNotFoundError:
            continue  # rebuild removed the empty directory between mkdir and mkstemp
        except OSError:
            return  # an unwritable state directory cannot be repaired by the cache


def _connect(file, *, readonly=False):
    import sqlite3
    uri = Path(os.path.abspath(file)).as_uri() + ('?mode=ro' if readonly else '?mode=rwc')
    conn = sqlite3.connect(uri, uri=True, timeout=BUSY_S)
    try:
        conn.execute(f'PRAGMA busy_timeout={int(BUSY_S * 1000)}')
        return conn
    except Exception:
        conn.close()
        raise


def _schema(conn, *, create=False):
    if create:
        conn.execute('CREATE TABLE jobs ('
                     'id TEXT PRIMARY KEY, engine TEXT, kind TEXT, status TEXT, phase TEXT, '
                     'pid INTEGER, pid_created INTEGER, cwd TEXT, dedupe_key TEXT, updated_at TEXT, '
                     'ended_at TEXT, status_stamp TEXT, result_stamp TEXT)')
        conn.execute(f'PRAGMA user_version={VERSION}')
    if conn.execute('PRAGMA user_version').fetchone()[0] != VERSION:
        raise ValueError('jobs index schema version mismatch')
    if tuple(r[1] for r in conn.execute('PRAGMA table_info(jobs)')) != COLUMNS:
        raise ValueError('jobs index schema mismatch')


def _lock():
    return common.file_lock(path() + '.lock', BUSY_S, poll_s=0.01, create_parent=False)


def _snapshot(jid):
    from . import jobs
    folder = os.path.join(common.JOBS, jid)
    status_path, result_path = (os.path.join(folder, f) for f in ('status.json', 'result.json'))
    before = (_stamp(status_path), _stamp(result_path))
    spec = common.read_json(os.path.join(folder, 'job.json'))
    st, result = common.read_json(status_path), common.read_json(result_path)
    after = (_stamp(status_path), _stamp(result_path))
    if before != after:
        raise OSError('job changed during index snapshot')
    spec = spec if isinstance(spec, dict) else {}
    primary = spec.get('primary')
    primary = primary if isinstance(primary, dict) else {}
    current = st if isinstance(st, dict) else {}
    receipt = result if isinstance(result, dict) else {}
    status = current.get('status') or 'unknown'
    # A terminal receipt does not override unfinished/invalid lifecycle evidence.
    if receipt.get('status') in jobs.TERMINAL and (before[0] is None or status in jobs.TERMINAL):
        status = receipt['status']
    elif status in jobs.TERMINAL:
        status = 'unknown'
    key = jobs._dedupe_key(spec)
    row = dict(id=jid, engine=current.get('engine') or primary.get('engine'), kind=spec.get('kind'),
               status=status, phase=current.get('phase'), pid=current.get('pid'),
               pid_created=current.get('pid_created'), cwd=spec.get('cwd'),
               dedupe_key=json.dumps(key) if key is not None else None,
               updated_at=current.get('updated_at') or receipt.get('ended_at'),
               ended_at=receipt.get('ended_at'), status_stamp=before[0], result_stamp=before[1])
    return tuple(row[k] for k in COLUMNS)


def _insert(conn, row):
    conn.execute('INSERT OR REPLACE INTO jobs (' + ','.join(COLUMNS) + ') VALUES ('
                 + ','.join('?' for _ in COLUMNS) + ')', row)


def update(jid):
    """Called after status/result publication. Failure never changes job success."""
    if not common.JOBS_INDEX_ENABLED:
        return
    try:
        with _lock():
            new = not os.path.exists(path())
            with closing(_connect(path())) as conn:
                conn.execute('PRAGMA journal_mode=WAL')
                with conn:
                    _schema(conn, create=new)
                    _insert(conn, _snapshot(jid))
    except Exception:
        _mark_dirty()


def read():
    """A missing, dirty, busy, damaged or foreign index means full folder reads."""
    if not common.JOBS_INDEX_ENABLED or _dirty():
        return None
    try:
        # Readers do not create a missing database or coordination lock.
        if not os.path.isfile(path()) or not os.path.isfile(path() + '.lock'):
            return None
        with _lock(), closing(_connect(path(), readonly=True)) as conn:
            _schema(conn)
            rows = {r[0]: dict(zip(COLUMNS, r)) for r in conn.execute('SELECT ' + ','.join(COLUMNS) + ' FROM jobs')}
        return None if _dirty() else rows
    except Exception:
        return None


def terminal(jid, rows):
    """Return cached terminal metadata only if its folder still proves it safe to skip."""
    from . import jobs
    row = rows.get(jid) if rows is not None else None
    if not row or row['status'] not in jobs.TERMINAL or row['result_stamp'] is None:
        return None
    folder = os.path.join(common.JOBS, jid)
    try:
        # Even terminal receipts can own a surviving or UNKNOWN child/tree.
        for marker in MARKERS:
            try:
                os.lstat(os.path.join(folder, marker))
                return None
            except FileNotFoundError:
                pass
        if _stamp(os.path.join(folder, 'result.json')) != row['result_stamp']:
            return None
        if _stamp(os.path.join(folder, 'status.json')) != row['status_stamp']:
            return None
        return row
    except (OSError, ValueError, TypeError):
        return None


def rebuild(*, dry_run=False):
    """Build separately, close/checkpoint every connection, then replace under the cache lock."""
    if not common.JOBS_INDEX_ENABLED:
        raise ValueError('jobs_index.enabled must be literal true before reindex')
    if os.environ.get('TWINLANE_ENGINE_JOB'):
        raise ValueError('jobs reindex changes shared state; not allowed in a twinlane job')
    return _rebuild(dry_run=dry_run)


def _rebuild(*, dry_run=False):
    # Internal algorithm, separately unit-tested against disposable folders.
    ids = names()
    if dry_run:
        return dict(dry_run=True, jobs=len(ids))
    os.makedirs(common.STATE, exist_ok=True)
    # Normalize an externally supplied plain-file dirty flag to token form before scanning.
    with _lock():
        if os.path.isfile(dirty_path()):
            os.remove(dirty_path())
            _mark_dirty()
        failures = set(os.listdir(dirty_path())) if _dirty() else set()
    fd, temp = tempfile.mkstemp(prefix='jobs-index-', suffix='.db', dir=common.STATE)
    os.close(fd)
    try:
        with closing(_connect(temp)) as conn:
            conn.execute('PRAGMA journal_mode=WAL')
            with conn:
                _schema(conn, create=True)
                for jid in ids:
                    _insert(conn, _snapshot(jid))
            conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
        with _lock():
            # All our readers/writers close under this lock. No old WAL handles
            # or sidecars can be carried across the replacement (Windows too).
            if any(os.path.exists(path() + suffix) for suffix in ('-wal', '-shm')):
                # A final read-only connection can leave empty sidecars behind.
                # A writable open/checkpoint/close cleans those up; a foreign
                # connection that still owns WAL must prevent replacement.
                with closing(_connect(path())) as old:
                    checkpoint = old.execute('PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    if checkpoint[0] != 0:
                        raise OSError('jobs index WAL checkpoint is busy')
                if any(os.path.exists(path() + suffix) for suffix in ('-wal', '-shm')):
                    raise OSError('jobs index still has open WAL sidecars; close other SQLite users first')
            os.replace(temp, path())
            for name in failures:
                try:
                    os.remove(os.path.join(dirty_path(), name))
                except FileNotFoundError:
                    pass
            try:
                os.rmdir(dirty_path())
            except FileNotFoundError:
                pass
            except OSError:
                if not _dirty():
                    raise
        return dict(dry_run=False, jobs=len(ids), dirty=_dirty())
    except Exception:
        _mark_dirty()
        raise
    finally:
        for file in (temp, temp + '-wal', temp + '-shm'):
            try:
                os.remove(file)
            except FileNotFoundError:
                pass


def cli(argv):
    import argparse
    from . import jobs
    parser = argparse.ArgumentParser(prog='twinlane jobs reindex')
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--json', action='store_true')
    args = jobs._parse(parser, argv)
    if isinstance(args, int):
        return args
    common.load_policy()
    try:
        data = rebuild(dry_run=args.dry_run)
    except (OSError, ValueError, TimeoutError) as error:
        common.emit({'error': str(error)}, args.json, f'twinlane jobs reindex: {error}')
        return common.EXIT_REFUSED
    except Exception as error:
        common.emit({'error': type(error).__name__}, args.json, 'twinlane jobs reindex: index rebuild failed')
        return common.EXIT_FAILED
    common.emit(data, args.json, f"{'Dry run' if args.dry_run else 'Rebuilt'}: {data['jobs']} jobs")
    return common.EXIT_OK
