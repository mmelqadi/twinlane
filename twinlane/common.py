"""Shared helpers for twinlane: paths, atomic JSON, time, processes, engine binaries.

Standard library only. Windows first (the host), POSIX tolerated.
"""
from __future__ import annotations

import datetime as dt
import glob
import locale
import hashlib
import json
import math
import random
import os
import re
import shutil
import stat
import struct
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

_PREFLIGHT_VERSIONS = ContextVar('codex_preflight_versions', default=None)

HOME = os.path.expanduser('~')
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def data_home() -> str:
    explicit = os.environ.get('TWINLANE_HOME')
    if explicit:
        return os.path.abspath(os.path.expanduser(explicit))
    if os.name == 'nt':
        base = os.environ.get('LOCALAPPDATA') or os.path.join(HOME, 'AppData', 'Local')
    else:
        base = os.environ.get('XDG_DATA_HOME') or os.path.join(HOME, '.local', 'share')
    return os.path.join(base, 'twinlane')


HOME_DIR = data_home()
STATE = os.path.join(HOME_DIR, 'state')
JOBS = os.path.join(HOME_DIR, 'jobs')
BATONS = os.path.join(HOME_DIR, 'batons')
BIN = os.path.join(HOME_DIR, 'bin')
POLICY = os.path.join(os.path.dirname(__file__), 'policy.json')
JOBS_INDEX_ENABLED = False  # refreshed by load_policy; OFF paths do no extra policy or SQLite I/O
TWINLANE_PY = os.path.join(os.path.dirname(__file__), 'cli.py')
CLAUDE_DIR = os.path.abspath(os.path.expanduser(os.environ.get('CLAUDE_CONFIG_DIR') or os.path.join(HOME, '.claude')))
CODEX_DIR = os.path.abspath(os.path.expanduser(os.environ.get('CODEX_HOME') or os.path.join(HOME, '.codex')))
CODEX_RUN = os.environ.get('TWINLANE_CODEX_RUN')

IS_WIN = os.name == 'nt'


def real_path(path: str) -> str:
    """An existing path with symlinks, junctions and Windows short names resolved; case is kept for display."""
    result = os.path.realpath(os.path.abspath(path))
    if IS_WIN:
        import ctypes
        from ctypes import wintypes
        get_long = ctypes.WinDLL('kernel32', use_last_error=True).GetLongPathNameW
        get_long.argtypes = (wintypes.LPCWSTR, wintypes.LPWSTR, wintypes.DWORD)
        get_long.restype = wintypes.DWORD
        size = get_long(result, None, 0)
        if size:
            buf = ctypes.create_unicode_buffer(size)
            if get_long(result, buf, size):
                result = os.path.normpath(buf.value)
        if len(result) > 1 and result[1] == ':':
            result = result[0].upper() + result[1:]
    return result


def canon_path(path: str) -> str:
    """One identity for comparing paths: `real_path` folded to the platform's case rules."""
    return os.path.normcase(real_path(path))

# Exit codes (DESIGN.md)
EXIT_OK, EXIT_USAGE, EXIT_REFUSED, EXIT_RUNNING, EXIT_FAILED, EXIT_LIMIT, EXIT_MISSING = 0, 2, 3, 4, 5, 6, 127


def ensure_dirs():
    for d in (STATE, JOBS, BATONS, BIN):
        os.makedirs(d, exist_ok=True)


def utf8_stdio():
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding='utf-8', errors='replace')
        except Exception:
            pass


# ---------------------------------------------------------------- time

def now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None) -> str | None:
    if t is None:
        return None
    return t.astimezone(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def parse_time(v) -> dt.datetime | None:
    """ISO string, epoch seconds or epoch milliseconds -> aware UTC datetime (None if unparseable)."""
    if v is None or v == '':
        return None
    if isinstance(v, (int, float)):
        x = float(v)
        if x > 1e12:  # milliseconds
            x /= 1000.0
        try:
            return dt.datetime.fromtimestamp(x, dt.timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(v).strip()
    if s.isdigit():
        return parse_time(int(s))
    try:
        s = re.sub(r'(\.\d{6})\d+(?=Z$|[+-]\d\d:?\d\d$|$)', r'\1', s)
        t = dt.datetime.fromisoformat(s.replace('Z', '+00:00'))
    except ValueError:
        return None
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


def age_s(v) -> float | None:
    t = parse_time(v)
    return None if t is None else max(0.0, (now() - t).total_seconds())


def fmt_age(seconds) -> str:
    """Compact age for listings: 45s, 12m, 3h, 2d ('?' when unknown)."""
    if seconds is None:
        return '?'
    s = int(seconds)
    for unit, size in (('d', 86400), ('h', 3600), ('m', 60)):
        if s >= size:
            return f'{s // size}{unit}'
    return f'{s}s'


def stamp() -> str:
    return now().strftime('%Y%m%d-%H%M%S')


# ---------------------------------------------------------------- json io

READ_RETRIES = 8


def read_json(path: str, default=None, deadline: float | None = None):
    """Parsed JSON, or default when the file is missing or invalid.

    On Windows, opening a file while another process os.replace()s it raises PermissionError for a
    moment; that is a busy file, not a missing one, so it is retried before giving up. With `deadline`
    (a time.monotonic() value) no retry sleeps past it: a busy file then reads as `default`."""
    for i in range(READ_RETRIES):
        try:
            with open(path, encoding='utf-8') as h:
                return json.load(h)
        except PermissionError:
            time.sleep(0.01 * (i + 1) if deadline is None else max(0.0, min(0.01 * (i + 1), deadline - time.monotonic())))
            if deadline is not None and time.monotonic() >= deadline:
                return default
        except (OSError, ValueError):
            return default
    return default


def _atomic_write(path: str, write, deadline: float | None = None, *, create_parent=True) -> None:
    """Temp file in the same directory, then os.replace (retried while a reader holds the target).

    With `deadline` (a time.monotonic() value) the retries are bounded by it: no replace is attempted and no
    sleep runs past it, TimeoutError is raised, the temp file is removed and `path` keeps its old content
    (the replace is atomic, so `path` is only ever the old file or the complete new one).
    `create_parent=False` never recreates a directory moved by another process."""
    d = os.path.dirname(os.path.abspath(path))
    if create_parent:
        os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=d)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as h:
            write(h)
        for i in range(20):
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f'write deadline passed: {path}')
            try:
                os.replace(tmp, path)
                return
            except PermissionError:  # reader holds the file on Windows
                nap = 0.05 * (i + 1)
                time.sleep(nap if deadline is None else max(0.0, min(nap, deadline - time.monotonic())))
        if deadline is not None and time.monotonic() >= deadline:
            raise TimeoutError(f'write deadline passed: {path}')
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass


def write_json(path: str, data, deadline: float | None = None, *, create_parent=True) -> None:
    _atomic_write(path, lambda h: json.dump(data, h, ensure_ascii=False, indent=2), deadline,
                  create_parent=create_parent)


def write_text(path: str, text: str, *, create_parent=True) -> None:
    _atomic_write(path, lambda h: h.write(text), create_parent=create_parent)


class LockWaitTimeout(TimeoutError):
    """An explicitly extended lock wait exhausted its overall budget."""


@contextmanager
def file_lock(path: str, timeout_s: float = 120, poll_s: float = 0.05, *, create_parent=True,
              wait_until=None, on_wait=None):
    """Exclusive OS lock on one byte of `path` (LockFileEx / flock).

    The OS drops the lock when its holder dies, so there is no staleness guess and no
    check-then-remove race. The lock file itself stays in place. `create_parent=False`
    requires an existing parent, for in-folder lifecycle locks during archival. Job admission
    may supply an absolute monotonic `wait_until`: jittered backoff, an `on_wait` callback,
    and best-effort holder diagnostics then replace the ordinary short timeout."""
    if create_parent:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        started = time.monotonic()
        deadline = started + timeout_s if wait_until is None else wait_until
        delay = poll_s
        while not _try_lock_fd(fd):
            if on_wait is not None:
                on_wait()
            if time.monotonic() > deadline:
                if wait_until is not None:
                    owner = read_json(path + '.owner.json', {}) or {}
                    detail = ''
                    if isinstance(owner, dict) and type(owner.get('pid')) is int:
                        age = age_s(owner.get('at'))
                        detail = f"; last observed holder pid {owner['pid']}"
                        if age is not None:
                            detail += f', age {max(0, age):.1f}s'
                    raise LockWaitTimeout(f'lock stayed busy for {(time.monotonic() - started) / 60:.3f} '
                                          f'minutes: {path}{detail}')
                raise TimeoutError(f'lock busy: {path}')
            time.sleep(poll_s if wait_until is None else
                       min(max(0, deadline - time.monotonic()), random.uniform(0.5, 1.5) * delay))
            delay = min(1.0, delay * 1.6)
        if wait_until is not None:
            try:
                write_json(path + '.owner.json', {'pid': os.getpid(), 'at': iso(now())})
            except OSError:
                pass  # diagnostics only; never authority to reclaim a lock
        try:
            yield
        finally:
            if wait_until is not None:
                try:
                    os.remove(path + '.owner.json')
                except OSError:
                    pass
            _unlock_fd(fd)
    finally:
        os.close(fd)


def _try_lock_fd(fd: int) -> bool:
    try:
        if os.name == 'nt':  # the real OS, not the IS_WIN test seam: these are native lock calls
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unlock_fd(fd: int) -> None:
    try:
        if os.name == 'nt':
            import msvcrt
            os.lseek(fd, 0, 0)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(fd, fcntl.LOCK_UN)
    except OSError:
        pass


def tail_lines(path: str, max_bytes: int = 2_000_000) -> list[str]:
    """Last max_bytes of a (possibly huge) text file as lines; the first partial line is dropped."""
    try:
        size = os.path.getsize(path)
        with open(path, 'rb') as h:
            if size > max_bytes:
                h.seek(size - max_bytes)
                data = h.read()
                data = data[data.find(b'\n') + 1:]
            else:
                data = h.read()
    except OSError:
        return []
    return data.decode('utf-8', errors='replace').splitlines()


def first_line(path: str, max_bytes: int = 200_000) -> str:
    try:
        with open(path, 'rb') as h:
            return h.readline(max_bytes).decode('utf-8', errors='replace')
    except OSError:
        return ''


def load_policy() -> dict:
    p = read_json(POLICY)
    if not isinstance(p, dict):
        raise SystemExit(f'twinlane: cannot read policy {POLICY}')
    config = os.environ.get('TWINLANE_CONFIG') or os.path.join(data_home(), 'config.json')
    if os.path.isfile(config):
        user = read_json(config)
        if not isinstance(user, dict):
            raise SystemExit(f'twinlane: invalid config {config}')
        p = _merge_config(p, user)
    configure_jobs_index(p)
    return p


def configure_jobs_index(policy):
    global JOBS_INDEX_ENABLED
    section = policy.get('jobs_index')
    JOBS_INDEX_ENABLED = isinstance(section, dict) and section.get('enabled') is True


def _merge_config(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        else:
            merged[key] = value
    return merged


def clip(s, n: int) -> str:
    s = '' if s is None else str(s)
    return s if len(s) <= n else s[: n - 1] + '…'


# ---------------------------------------------------------------- binaries / env

def claude_bin() -> str | None:
    """TWINLANE_CLAUDE_BIN overrides (tests point it at a fake that replays stream-json fixtures)."""
    env = os.environ.get('TWINLANE_CLAUDE_BIN')
    if env:
        return env
    cand = os.path.join(HOME, '.local', 'bin', 'claude.exe' if IS_WIN else 'claude')
    if os.path.isfile(cand):
        return cand
    return shutil.which('claude')


CODEX_HELPERS = ('codex-command-runner.exe', 'codex-windows-sandbox-setup.exe', 'codex-code-mode-host.exe')
CODEX_FILES = ('codex.exe',) + CODEX_HELPERS
# Disk ceiling: bin/ holds at most this many copy-sized folders (~434 MB each), counting published copies,
# staging folders (.copy-*) and deletes a lock blocked (*.del).
CODEX_COPY_BUDGET = 3
_MANIFEST, _LAST_USED, _VERSIONS, _LEASES, _BIN_LOCK = 'manifest.json', 'last-used', 'versions.json', 'leases', '.lock'
_LEASE_RE = re.compile(r'(\d+)-(\d+)-[0-9a-f]+\.json')
_STAGING_RE = re.compile(r'\.copy-(\d+)-(\d+)-.+')  # .copy-<creator pid>-<its creation stamp>-<random>
_GARBAGE = '.garbage'
_VERSION_RE = re.compile(r'(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.]+))?')


class CopyBudgetFull(OSError):
    """bin/ is at CODEX_COPY_BUDGET and nothing in it can be evicted: no new copy is staged."""


def _guard_bin_writes() -> None:
    """Tests set TWINLANE_TEST_ISOLATION=1: then every write to bin/ (copies, leases, the version cache, pruning)
    refuses unless bin/ is under the system temp dir, so no test can copy into or prune the real bin/."""
    if os.environ.get('TWINLANE_TEST_ISOLATION') != '1':
        return
    tmp = os.path.normcase(os.path.realpath(tempfile.gettempdir()))
    if not os.path.normcase(os.path.realpath(BIN)).startswith(tmp + os.sep):
        raise RuntimeError(f'TWINLANE_TEST_ISOLATION=1 but bin/ is not under {tmp}: {BIN}')


def _mtime(path: str) -> float:
    """mtime, 0 for a file that vanished: the app empties its bundle on exit and other twinlane processes prune."""
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def _norm(path: str) -> str:
    return os.path.normcase(os.path.realpath(path))


def codex_version_key(version: str | None) -> tuple | None:
    """Sortable key for '0.162.0' / 'codex-cli 0.162.0-alpha.2': numeric parts, a pre-release below its
    release, pre-release identifiers numeric below alphanumeric (semver)."""
    m = _VERSION_RE.search(version) if isinstance(version, str) else None  # a manifest or cache may hold anything
    if not m:
        return None
    core = tuple(int(x) for x in m.group(1, 2, 3))
    if not m.group(4):
        return core + (1, ())
    return core + (0, tuple((0, int(p), '') if p.isdigit() else (1, 0, p) for p in m.group(4).split('.')))


def _read_codex_version(path: str) -> str | None:
    """'X.Y.Z[-pre]' from `codex.exe --version`, None when it cannot be read (tests patch this)."""
    try:
        r = subprocess.run([path, '--version'], capture_output=True, text=True, encoding='utf-8', errors='replace',
                           stdin=subprocess.DEVNULL, timeout=15, creationflags=0x08000000 if IS_WIN else 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    m = _VERSION_RE.search(r.stdout or '')
    return m.group(0) if r.returncode == 0 and m else None


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def _build_info(path: str, st: os.stat_result, want_version: bool = True, want_sha: bool = False) -> dict:
    """{'version', 'sha256'} of an app-owned file, cached in bin/versions.json per (path, size, mtime_ns, file
    index): a codex.exe is run for its version, and any file hashed, once per build. The file index (st_ino)
    makes a replaced file re-hash even when its size and mtime were restored. Out of scope: an in-place,
    same-size edit that restores the mtime (same file index); catching it means hashing ~434 MB per lookup.
    A failed version read is not cached; a hash is cached only when the file did not change while it was read."""
    key = f'{os.path.normcase(path)}|{st.st_size}|{st.st_mtime_ns}|{st.st_ino}'
    cache_path = os.path.join(BIN, _VERSIONS)
    cache = read_json(cache_path, {})
    cache = cache if isinstance(cache, dict) else {}
    entry = cache.get(key)
    entry = dict(entry) if isinstance(entry, dict) else ({'version': entry} if isinstance(entry, str) else {})
    if not isinstance(entry.get('version'), str):
        entry.pop('version', None)
    if not (isinstance(entry.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', entry['sha256'])):
        entry.pop('sha256', None)
    changed = False
    if want_version and not entry.get('version'):
        version = _read_codex_version(path)
        if version:
            entry['version'], changed = version, True
    if want_sha and not entry.get('sha256'):
        digest = _sha256_file(path)
        after = os.stat(path)
        if (after.st_size, after.st_mtime_ns, after.st_ino) != (st.st_size, st.st_mtime_ns, st.st_ino):
            raise OSError(f'codex build changed while it was hashed: {path}')
        entry['sha256'], changed = digest, True
    if changed:
        _guard_bin_writes()
        cache = {k: v for k, v in cache.items() if os.path.isfile(k.split('|')[0])}
        cache[key] = entry
        try:
            write_json(cache_path, cache)
        except OSError:
            pass
    return entry


def codex_version(path: str, st: os.stat_result) -> str | None:
    reads = _PREFLIGHT_VERSIONS.get()
    if reads is not None:
        key = f'{os.path.normcase(path)}|{st.st_size}|{st.st_mtime_ns}|{st.st_ino}'
        if key not in reads:
            cached = read_json(os.path.join(BIN, _VERSIONS), {})
            entry = cached.get(key) if isinstance(cached, dict) else None
            version = entry.get('version') if isinstance(entry, dict) else entry
            reads[key] = version if isinstance(version, str) else _read_codex_version(path)
        return reads[key]
    return _build_info(path, st).get('version')


def _bundle_sha256(src_dir: str, stats: dict) -> tuple[str, dict]:
    """(bundle id, {file: sha256}): the sha256 over the four files' sha256 in CODEX_FILES order, so a changed
    helper is a new bundle even when codex.exe is unchanged."""
    shas = {n: _build_info(os.path.join(src_dir, n), stats[n], want_version=False, want_sha=True)['sha256']
            for n in CODEX_FILES}
    return hashlib.sha256(''.join(shas[n] for n in CODEX_FILES).encode('ascii')).hexdigest(), shas


def _codex_complete(folder: str) -> bool:
    return all(os.path.isfile(os.path.join(folder, n)) for n in CODEX_FILES)


def _app_bin_dirs() -> list[str]:
    base = os.environ.get('LOCALAPPDATA') or os.path.join(HOME, 'AppData', 'Local')
    return [os.path.join(base, 'OpenAI', 'Codex', 'bin'), os.path.join(CODEX_DIR, 'plugins', '.plugin-appserver')]


def _standalone_codex(path: str) -> bool:
    """True for the standalone Codex CLI install (LOCALAPPDATA/Programs/OpenAI/Codex/bin): a separately updated CLI,
    not one of the app's own builds, so it is never copied as an app source; preflight names it when it is too old."""
    base = os.environ.get('LOCALAPPDATA') or os.path.join(HOME, 'AppData', 'Local')
    return _norm(path).startswith(_norm(os.path.join(base, 'Programs', 'OpenAI', 'Codex', 'bin')) + os.sep)


def _app_owned(path: str) -> bool:
    p = _norm(path)
    return any(p.startswith(_norm(d) + os.sep) for d in _app_bin_dirs())


def _pick_codex_source(*, ignore_env: bool = False) -> tuple[str | None, str, str | None]:
    """(path, origin, version): TWINLANE_CODEX_BIN as set ('env'); else the best complete app build ('app'); else
    (None, 'none', None). An explicit binary inside the app's own folders is an app source like any other: it is
    copied, never run in place. A build without every helper (the app mid-extract) is never a source;
    complete builds rank by version, then mtime, so an older build rewritten later does not beat a newer one."""
    env = None if ignore_env else os.environ.get('TWINLANE_CODEX_BIN')
    if not IS_WIN:
        return (env, 'env', None) if env and os.path.isfile(env) else (None, 'none', None)
    if env and os.path.isfile(env) and not _app_owned(env):
        return env, 'env', None
    found = [env] if env and os.path.isfile(env) else []
    for folder in _app_bin_dirs():
        found.extend(glob.glob(os.path.join(folder, '*', 'codex.exe')))
        found.append(os.path.join(folder, 'codex.exe'))
    ranked = []
    for path in found:
        try:
            st = os.stat(path)
        except OSError:  # absent, or the app emptied its bundle meanwhile
            continue
        if not _codex_complete(os.path.dirname(path)):
            continue
        version = codex_version(path, st)
        vkey = codex_version_key(version)
        ranked.append(((vkey is not None, vkey or (), st.st_mtime), path, version))
    if ranked:
        _, path, version = max(ranked, key=lambda r: r[0])
        return path, 'app', version
    return None, 'none', None


def codex_source_bin() -> tuple[str | None, str]:
    """(path, origin) of the codex build twinlane would copy. The desktop app keeps its CLI in
    OpenAI/Codex/bin/<hash>/ only while it runs (the folder is emptied when it exits);
    ~/.codex/plugins/.plugin-appserver keeps the same build with its helper exes."""
    path, origin, _ = _pick_codex_source()
    return path, origin


def resolve_codex() -> tuple[str | None, str]:
    """(binary, why), failing closed: TWINLANE_CODEX_BIN as set (the user's explicit choice, unless it points into the
    app's own folders); else a private copy of the best complete app build, made if needed; else, when that
    build is gone or cannot be copied, the newest valid private copy; else None. Never the app's own exe (it
    is locked while it runs and deleted when the app exits). `why` is '' for the normal case;
    anything else is worth showing (job notes, twinlane status).
    Run the result only under a lease: see acquire_codex()."""
    src, origin, version = _pick_codex_source()
    if not IS_WIN:
        return (src, f'TWINLANE_CODEX_BIN as set: {src}') if src else (
            None, 'no Codex binary configured (set TWINLANE_CODEX_BIN)')
    if origin == 'env':
        return src, f'TWINLANE_CODEX_BIN as set: {src}'
    if src:
        try:
            note = ''
            env = os.environ.get('TWINLANE_CODEX_BIN')
            if env and _app_owned(env) and _norm(env) != _norm(src) and os.path.isfile(env):
                old_version = codex_version(env, os.stat(env))
                note = (f'TWINLANE_CODEX_BIN names app build {old_version}; '
                        f'using the newer app build {version}')
            return _private_codex_copy(src, version), note
        except OSError as e:
            why = f'cannot copy {src}: {clip(str(e) or type(e).__name__, 160)}'
    else:
        why = 'no complete Codex app build found (open the Codex app once, or set TWINLANE_CODEX_BIN)'
    old = _newest_valid_copy()
    if old:
        m = _valid_copy(os.path.dirname(old)) or {}
        return old, f'{why}; using an earlier private copy ({m.get("version") or "version unknown"}): {old}'
    return None, why


def acquire_codex() -> tuple[str | None, str, str | None]:
    """(binary, why, lease). The lease covers the window between lookup and the child's start; after that the
    OS protects the copy (Windows refuses to delete a running codex.exe, and _discard() deletes it first).
    Lease and prune both run under bin/.lock, so a copy pruned between resolution and lease is resolved again
    instead of being run half-deleted."""
    if not IS_WIN:
        binary, why = resolve_codex()
        return binary, why, None
    for _ in range(3):
        binary, why = resolve_codex()
        folder = _private_folder(binary)
        if not folder:
            return binary, why, None
        lease = _take_lease(folder)
        if lease:
            _touch(os.path.join(folder, _LAST_USED))
            try:
                _prune_codex_copies(folder)
            except OSError:  # housekeeping: never lose a good copy over it
                pass
            return binary, why, lease
    raise OSError('the private codex copy was pruned, or failed its hash check, before it could be leased, three times')


def release_codex(lease: str | None) -> None:
    if not lease:
        return
    _touch(os.path.join(os.path.dirname(os.path.dirname(lease)), _LAST_USED))
    try:
        os.remove(lease)
    except OSError:
        pass


@contextmanager
def codex_lease():
    """with codex_lease() as (binary, why): run binary (None when there is no usable codex) under a lease."""
    binary, why, lease = acquire_codex()
    try:
        yield binary, why
    finally:
        release_codex(lease)


CODEX_SOURCES = ('auto', 'app-copy', 'path')


def acquire_codex_binary(source: str = 'auto') -> tuple[str | None, str, str | None]:
    """(binary, why, lease) for policy codex.binary_source, failing closed (binary None) when nothing qualifies.
    TWINLANE_CODEX_BIN, when set, is the user's explicit choice in every mode. Otherwise:
      app-copy  a leased private copy of the desktop app's newer CLI (acquire_codex); never the PATH.
      path      the `codex` on PATH.
      auto      the app copy when there is one, else the `codex` on PATH (named in `why`).
    Run the result under its lease and release_codex() it after the child exits."""
    if source not in CODEX_SOURCES:
        raise ValueError(f'codex.binary_source must be one of {", ".join(CODEX_SOURCES)}, not {source!r}')
    env = os.environ.get('TWINLANE_CODEX_BIN')
    why = ''
    if source != 'path' or env and _app_owned(env):
        binary, why, lease = acquire_codex()
        if binary or source == 'app-copy':
            return binary, why, lease
    if env:
        return env, f'TWINLANE_CODEX_BIN as set: {env}', None
    found = shutil.which('codex')
    if not found:
        return None, (f'{why}; ' if why else '') + 'no codex on PATH', None
    found = os.path.realpath(os.path.abspath(found))
    cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    resolved = os.path.normcase(found)
    if resolved == cwd or resolved.startswith(cwd.rstrip(os.sep) + os.sep):
        return None, 'refusing codex on PATH inside the current directory', None
    return found, (f'{why}; using the codex on PATH: {found}' if why else f'binary_source=path: {found}'), None


# A preserving start (jobs.py) runs only an explicitly supplied standalone TWINLANE_CODEX_BIN: no PATH or app
# lookup, private copy, lease, prune or version cache. Its identity is recorded at planning, checked again at
# preflight and launch, and handed to the built-in runner, which checks it once more before the child starts.
CODEX_BIN_QUALIFIED = 'TWINLANE_CODEX_BIN_QUALIFIED'
CODEX_IDENTITY_KEYS = ('path', 'size', 'mtime_ns', 'file_id', 'dev', 'sha256')


def codex_protected_roots() -> list[str]:
    """Folders a preserving start never runs Codex from: the desktop app's builds, install and data, the
    Codex home, and twinlane's shared private-copy store (bin/)."""
    base = os.environ.get('LOCALAPPDATA') or os.path.join(HOME, 'AppData', 'Local')
    roots = _app_bin_dirs() + [os.path.join(base, 'OpenAI'), CODEX_DIR, BIN]
    if IS_WIN:
        roots.append(os.path.join(os.environ.get('ProgramFiles') or r'C:\Program Files', 'WindowsApps'))
    return roots


def _codex_stat_key(st: os.stat_result) -> tuple:
    return st.st_size, st.st_mtime_ns, st.st_ino, st.st_dev


def qualify_explicit_codex_bin(value) -> dict:
    """{path, size, mtime_ns, file_id, dev, sha256} of `value` (TWINLANE_CODEX_BIN) as an explicitly supplied
    STANDALONE binary at LOCALAPPDATA/Programs/OpenAI/Codex/bin/codex.exe, or ValueError naming why it does not
    qualify. Reads and hashes the file; never runs,
    copies, leases or caches it. Qualifies only an absolute local path to a regular file with one hard link,
    reached without any link, junction or short name, outside every codex_protected_roots() folder."""
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError('TWINLANE_CODEX_BIN is absent, empty or padded with spaces')
    if any(ord(c) < 32 or c in '"<>|*?' for c in value) or (IS_WIN and ':' in value[2:]):
        raise ValueError('TWINLANE_CODEX_BIN holds control, quoting, wildcard or stream characters')
    if value.startswith(('\\\\', '//')):
        raise ValueError('TWINLANE_CODEX_BIN is a network or device path; give a local standalone file')
    if not os.path.isabs(value) or (IS_WIN and not re.match(r'[A-Za-z]:[\\/]', value)):
        raise ValueError('TWINLANE_CODEX_BIN must be an absolute path to the binary (no PATH lookup)')
    path = os.path.normpath(value)
    try:
        st = os.lstat(path)
        canon = canon_path(path)
    except OSError as e:
        raise ValueError(f'TWINLANE_CODEX_BIN cannot be read ({type(e).__name__})') from e
    if not stat.S_ISREG(st.st_mode):
        raise ValueError('TWINLANE_CODEX_BIN is not a regular file (a link, folder or device)')
    if os.path.normcase(path) != canon:
        raise ValueError('TWINLANE_CODEX_BIN resolves through a link, junction or short name; give its final path')
    if st.st_nlink != 1:
        raise ValueError('TWINLANE_CODEX_BIN has other hard links; give a standalone copy')
    for root in codex_protected_roots():
        top = canon_path(root).rstrip(os.sep)
        if canon == top or canon.startswith(top + os.sep):
            raise ValueError(f'TWINLANE_CODEX_BIN is inside a protected Codex binary root ({root}): app-owned and '
                             'shared copies are never run by a preserving start')
    base = os.environ.get('LOCALAPPDATA') or os.path.join(HOME, 'AppData', 'Local')
    approved = os.path.normcase(os.path.normpath(os.path.join(base,'Programs','OpenAI','Codex','bin','codex.exe')))
    if canon != approved:
        raise ValueError('preserving Codex starts require the explicit standalone Programs/OpenAI/Codex/bin/codex.exe location')
    try:
        digest = _sha256_file(path)
        after = os.lstat(path)
    except OSError as e:
        raise ValueError(f'TWINLANE_CODEX_BIN cannot be hashed ({type(e).__name__})') from e
    if _codex_stat_key(after) != _codex_stat_key(st):
        raise ValueError('TWINLANE_CODEX_BIN changed while it was hashed')
    return {'path': path, 'size': st.st_size, 'mtime_ns': st.st_mtime_ns, 'file_id': st.st_ino,
            'dev': st.st_dev, 'sha256': digest}


def codex_bin_identity_problem(recorded) -> str | None:
    """Why TWINLANE_CODEX_BIN is no longer the binary a preserving start recorded (None when it still is):
    the current value is qualified again from scratch and must match every recorded field."""
    if not (isinstance(recorded, dict) and set(recorded) == set(CODEX_IDENTITY_KEYS)
            and isinstance(recorded['path'], str) and isinstance(recorded['sha256'], str)
            and re.fullmatch(r'[0-9a-f]{64}', recorded['sha256'])
            and all(type(recorded[k]) is int for k in ('size', 'mtime_ns', 'file_id', 'dev'))):
        return 'the job carries no well-formed qualified binary identity (runner hand-off unqualified)'
    try:
        current = qualify_explicit_codex_bin(os.environ.get('TWINLANE_CODEX_BIN'))
    except ValueError as e:
        return str(e)
    if current != recorded:
        return 'TWINLANE_CODEX_BIN is not the binary recorded when this job was planned (path, file or bytes changed)'
    return None


# Per-job settings twinlane passes itself: exec_config may not override them.
CODEX_OWNED_CONFIG = ('model', 'model_reasoning_effort', 'sandbox_mode', 'approval_policy')
_CONFIG_KEY = re.compile(r'[A-Za-z_][A-Za-z0-9_.\-]*')


def codex_exec_config(policy: dict | None) -> list[str]:
    """policy codex.exec_config: 'key=value' strings handed to `codex exec` as -c key=value pairs, for example
    'features.apps=false'. ValueError when an entry is malformed or sets something twinlane owns per job (the
    model, its effort, the sandbox, the approval policy)."""
    raw = ((policy or {}).get('codex') or {}).get('exec_config')
    if raw is None:
        return []
    if not isinstance(raw, list):
        raise ValueError('codex.exec_config must be a list of "key=value" strings')
    for item in raw:
        key, sep, value = item.partition('=') if isinstance(item, str) else ('', '', '')
        if not sep or not _CONFIG_KEY.fullmatch(key.strip()) or not value.strip()                 or any(ord(c) < 32 for c in item):
            raise ValueError(f'codex.exec_config entry {item!r} is not "key=value"')
        if key.strip().lower() in CODEX_OWNED_CONFIG:
            raise ValueError(f'codex.exec_config may not set {key.strip()}: twinlane sets it for each job')
    return list(raw)


def codex_version_refusal(binary: str, minimum) -> str | None:
    """Why `binary` is older than policy codex.min_cli_version (None when it is new enough or no minimum is
    set). A version that cannot be read is a refusal too: the minimum could not be shown to hold."""
    if minimum is None or minimum == '':
        return None
    want = codex_version_key(minimum) if isinstance(minimum, str) else None
    if want is None:
        return f'codex.min_cli_version {minimum!r} is not a version number (use X.Y.Z)'
    try:
        version = codex_version(binary, os.stat(binary))
    except OSError:
        version = _read_codex_version(binary)
    got = codex_version_key(version)
    if got is None:
        return f'cannot read the version of {binary} (codex --version), so codex.min_cli_version {minimum} is unproven'
    if got < want:
        return (f'codex CLI {version} at {binary} is older than codex.min_cli_version {minimum}; '
                'point TWINLANE_CODEX_BIN at a newer build or set codex.binary_source to app-copy')
    return None


def codex_preflight(policy: dict) -> dict:
    """Inspect dispatch's CLI choice without copying a build or taking a lease."""
    token = _PREFLIGHT_VERSIONS.set({})
    try:
        return _codex_preflight(policy)
    except OSError as e:
        return dict(binary=None, origin='none', version=None, newest_app=None, note=None,
                    refusal=f'no usable codex CLI: lookup failed: {e}')
    finally:
        _PREFLIGHT_VERSIONS.reset(token)


def _codex_preflight(policy: dict) -> dict:
    cx = policy.get('codex') or {}
    out = dict(binary=None, origin='none', version=None, newest_app=None, refusal=None, note=None)
    source = cx.get('binary_source') or 'auto'
    if source not in CODEX_SOURCES:
        out['refusal'] = f'codex.binary_source must be one of {", ".join(CODEX_SOURCES)}, not {source!r}'
        return out
    env = os.environ.get('TWINLANE_CODEX_BIN')
    if env and not os.path.isfile(env):
        out['refusal'] = f'TWINLANE_CODEX_BIN points at a missing file: {env}'
        return out
    newest, origin, newest_version = _pick_codex_source(ignore_env=True)
    if origin == 'app':
        out['newest_app'] = newest_version
    if env and not _app_owned(env):
        binary, origin, version = env, 'env', codex_version(env, os.stat(env))
    elif source == 'path' and not env:
        binary, origin, version = shutil.which('codex'), 'path', None
    else:
        binary, origin, version = _pick_codex_source()
        if not binary:
            # An existing verified private copy is also usable when the app is closed.
            binary = _newest_valid_copy() if IS_WIN and source != 'path' else None
            origin = 'app' if binary else 'none'
        if not binary and source != 'app-copy':
            binary, origin = shutil.which('codex'), 'path'
    if not binary:
        out['refusal'] = 'no usable codex CLI: no complete Codex app build or allowed CLI found'
        return out
    version = version or codex_version(binary, os.stat(binary))
    out.update(binary=binary, origin=origin, version=version)
    out['refusal'] = codex_version_refusal(binary, cx.get('min_cli_version'))
    if out['refusal']:
        return out
    if env and not _app_owned(env):
        got, best = codex_version_key(version), codex_version_key(newest_version)
        if got is not None and best is not None and got < best and cx.get('allow_older_env_bin') is not True:
            kind = ' (the standalone Codex CLI)' if _standalone_codex(binary) else ''
            out['refusal'] = (f'TWINLANE_CODEX_BIN is codex {version} at {binary}{kind}, older than the Codex app build '
                              f'{newest_version}; unset TWINLANE_CODEX_BIN (the app build is then copied and used), '
                              'point it at a newer CLI, or set codex.allow_older_env_bin to true')
        elif got is None:
            out['note'] = f'cannot read the version of {binary}; codex.min_cli_version is not set'
    return out


def worktree_top(path: str) -> str | None:
    """Find the nearest checkout root by stat only (including linked and nested checkouts)."""
    folder = os.path.realpath(os.path.abspath(path))
    while True:
        if os.path.exists(os.path.join(folder, '.git')):
            return folder
        parent = os.path.dirname(folder)
        if parent == folder:
            return None
        folder = parent


def writer_key(path: str) -> str:
    from . import observations
    return observations.writer_key(path)


def codex_bin() -> str | None:
    """The codex CLI resolve_codex() picks, with the lookup's housekeeping. Unleased: code that RUNS it must
    use acquire_codex() / codex_lease() instead."""
    binary, _, lease = acquire_codex()
    release_codex(lease)
    return binary


def _private_folder(binary: str | None) -> str | None:
    if not binary:
        return None
    folder = os.path.dirname(binary)
    in_bin = os.path.normcase(os.path.dirname(folder)) == os.path.normcase(os.path.abspath(BIN))
    return folder if in_bin and os.path.basename(folder).startswith('codex-') else None


def _bin_lock(timeout_s: float):
    os.makedirs(BIN, exist_ok=True)
    return file_lock(os.path.join(BIN, _BIN_LOCK), timeout_s=timeout_s)


def _take_lease(folder: str) -> str | None:
    """A lease file <copy>/leases/<pid>-<created>-<n>.json naming this process; None if the copy is gone or its
    bytes no longer match the manifest. The lease is taken under bin/.lock; the copy is then hashed outside it
    (the lease already keeps prune away), so parallel lookups do not queue behind ~434 MB of hashing. A copy
    that fails the hash is marked garbage: never selected again, and discarded once nothing runs from it."""
    _guard_bin_writes()
    pid = os.getpid()
    name = f'{pid}-{process_created(pid) or 0}-{uuid.uuid4().hex[:8]}.json'
    with _bin_lock(60):
        if not _valid_copy(folder):
            return None
        path = os.path.join(folder, _LEASES, name)
        write_json(path, {'pid': pid, 'at': iso(now())})
    if _valid_copy(folder, verify=True):
        return path
    with _bin_lock(60):
        try:
            with open(os.path.join(folder, _GARBAGE), 'xb'):
                pass
        except OSError:  # already marked, or unmarkable: then each retry fails this same hash
            pass
        finally:
            _remove(path)
    return None


def _finite_seconds(v) -> bool:
    """A usable policy timeout: a finite, non-negative number (not a bool, inf or nan)."""
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) and v >= 0


def _uncertain_liveness_bound() -> int:
    """Oldest unconfirmed holder/staging item allowed by the current job policy."""
    policy = read_json(POLICY, {})
    limits = []
    for engine in ('claude', 'codex'):
        config = policy.get(engine) if isinstance(policy, dict) else None
        config = config if isinstance(config, dict) else {}
        by_profile = config.get('timeout_s_by_profile')
        by_profile = by_profile if isinstance(by_profile, dict) else {}
        limits.extend(v for v in by_profile.values() if _finite_seconds(v))
        if _finite_seconds(config.get('timeout_s')):
            limits.append(config['timeout_s'])
    return int(max(limits, default=0)) + 600


def _holder_alive(pid: int, created: int, item: str | None = None) -> bool:
    """A confirmed holder lives until exit. Unconfirmed identity expires after the policy bound.

    This age bound is a legacy policy for Codex binary-copy leases and staging folders only. It is not the
    writer-directory rule: a writer reservation (jobs._claim_writer) is never evicted by age, and an
    unknown or unreadable owner keeps holding it until its owner is proven gone."""
    if IS_WIN:
        alive, actual = _win_process_info(pid)
    else:
        alive = pid_alive(pid)
        actual = _posix_start_time(pid) if alive else None
    if alive is False:
        return False  # None (the probe failed) is unconfirmed, like an unreadable identity: bounded below
    if created and actual is not None:
        return actual == created
    if item and time.time() - _mtime(item) >= _uncertain_liveness_bound():
        return False
    return True


def _live_leases(folder: str) -> int:
    """Leases on a copy whose holder still runs; a dead holder's lease is removed. Call under bin/.lock."""
    n = 0
    for f in glob.glob(os.path.join(folder, _LEASES, '*.json')):
        m = _LEASE_RE.fullmatch(os.path.basename(f))
        if m and _holder_alive(int(m.group(1)), int(m.group(2)), f):
            n += 1
            continue
        try:
            os.remove(f)
        except OSError:
            n += 1  # cannot clear it: treat it as held
    return n


def _touch(path: str) -> None:
    """Stamp `path` with the precise clock. utime(path, None) takes Windows' coarse system time (one clock tick,
    up to ~15.6 ms), so two copies touched within a tick tied in _last_used and the name order, not the later
    release, picked the fallback."""
    try:
        with open(path, 'a', encoding='utf-8'):
            pass
        t = time.time_ns()
        os.utime(path, ns=(t, t))
    except OSError:
        pass


def _valid_copy(folder: str, verify: bool = False) -> dict | None:
    """The manifest of a complete private copy whose files keep their recorded size, mtime_ns and file index.
    `verify` also hashes every file against its recorded sha256 (~434 MB): only _take_lease does, once per lease."""
    if os.path.exists(os.path.join(folder, _GARBAGE)) or folder.endswith('.del'):
        return None
    m = read_json(os.path.join(folder, _MANIFEST))
    files = m.get('files') if isinstance(m, dict) else None
    if not isinstance(files, dict) or not m.get('bundle_sha256') or any(n not in files for n in CODEX_FILES):
        return None
    for name, rec in files.items():
        try:
            st = os.stat(os.path.join(folder, name))
            if not isinstance(rec, dict) or ((st.st_size, st.st_mtime_ns, st.st_ino) !=
                                             (rec.get('size'), rec.get('mtime_ns'), rec.get('ino'))):
                return None
            if verify and _sha256_file(os.path.join(folder, name)) != rec.get('sha256'):
                return None
        except OSError:
            return None
    return m


def _copy_dirs() -> list[str]:
    return [d for d in glob.glob(os.path.join(BIN, 'codex-*')) if not d.endswith('.del') and os.path.isdir(d)]


def _leftover_dirs() -> list[str]:
    """Copy-sized folders that are not published copies: staging (.copy-*) and blocked deletes (*.del)."""
    return [d for d in glob.glob(os.path.join(BIN, '.copy-*')) + glob.glob(os.path.join(BIN, 'codex-*.del'))
            if os.path.isdir(d)]


def _last_used(folder: str) -> float:
    return max(_mtime(os.path.join(folder, _LAST_USED)), _mtime(folder))


def _newest_valid_copy() -> str | None:
    best = None
    for d in _copy_dirs():
        m = _valid_copy(d)
        if m:
            vkey = codex_version_key(m.get('version'))
            rank = (vkey is not None, vkey or (), _last_used(d))
            if best is None or rank > best[0]:
                best = (rank, d)
    return os.path.join(best[1], 'codex.exe') if best else None


def _remove(path: str) -> bool:
    try:
        os.remove(path)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _discard(folder: str) -> bool:
    """Preflight every file before deleting; partial deletion is durable garbage, never a fallback. Files the
    manifest lists may be missing (quarantined, or a partial delete): what remains passes the same preflight."""
    if not IS_WIN:
        return False
    if not os.path.isdir(folder):
        return True
    manifest = read_json(os.path.join(folder, _MANIFEST))
    expected = manifest.get('files') if isinstance(manifest, dict) else None
    try:
        entries = set(os.listdir(folder))
        if isinstance(expected, dict):
            if entries - (set(expected) | {_MANIFEST, _LAST_USED, _LEASES, _GARBAGE}):
                return False
        files = []
        for root, dirs, names in os.walk(folder):
            if dirs and (root != folder or dirs != [_LEASES]):
                return False
            files.extend(os.path.join(root, name) for name in names)
        for path in files:
            if os.stat(path).st_nlink != 1:
                return False
            if path.lower().endswith(('.exe', '.dll')):
                with open(path, 'r+b'):
                    pass  # Windows denies a write-open on a running/mapped image.
    except OSError:
        return False
    # Mark before the first delete. If any later delete fails, this marker excludes the partial copy, so it is
    # removed last of all (shutil.rmtree's order is the directory's, which puts '.garbage' first on NTFS).
    marker = os.path.join(folder, _GARBAGE)
    try:
        if _GARBAGE not in entries:
            with open(marker, 'xb'):
                pass
    except OSError:
        return not os.path.isdir(folder)
    if not _remove(os.path.join(folder, 'codex.exe')):
        if _GARBAGE not in entries:  # the copy is still whole: unmark only what this call marked
            _remove(marker)
        return False
    ok = True
    for root, _dirs, names in os.walk(folder, topdown=False):
        for name in names:
            path = os.path.join(root, name)
            if path != marker:
                ok = _remove(path) and ok
        if root != folder:
            try:
                os.rmdir(root)
            except FileNotFoundError:
                pass
            except OSError:
                ok = False
    if not ok or not _remove(marker):
        return False
    try:
        os.rmdir(folder)
    except FileNotFoundError:
        pass
    except OSError:
        return False
    return True


def _clear_leftovers() -> None:
    """Retry blocked deletes (*.del, left by older versions) in the same codex.exe-first order, and drop
    staging folders whose creator is dead (whatever their age). A staging folder that does not name its
    creator (an older twinlane) is left alone: it may still be copying."""
    for d in glob.glob(os.path.join(BIN, 'codex-*.del')):
        _discard(d)
    for d in glob.glob(os.path.join(BIN, '.copy-*')):
        m = _STAGING_RE.fullmatch(os.path.basename(d))
        if ((m and not _holder_alive(int(m.group(1)), int(m.group(2)), d)) or
                (not m and time.time() - _mtime(d) >= _uncertain_liveness_bound())):
            shutil.rmtree(d, ignore_errors=True)  # never published, so nothing runs from it


def _make_room(extra: int, keep: str | None = None, budget: int = CODEX_COPY_BUDGET) -> bool:
    """Under bin/.lock. Copies no live lease names go: invalid ones (older formats, damaged, half-deleted)
    always, valid ones least recently used first while the folders in bin/ (published, staging and undeleted)
    plus `extra` new ones exceed the budget. _discard() leaves any copy whose codex.exe is running. `keep`
    stays. True when `extra` more folders fit."""
    _clear_leftovers()
    dirs = [d for d in _copy_dirs() if keep is None or os.path.normcase(d) != os.path.normcase(keep)]
    free = [d for d in dirs if not _live_leases(d)]
    for d in free:
        if not _valid_copy(d):
            _discard(d)
    free = sorted((d for d in free if _valid_copy(d)), key=_last_used)
    newest = _newest_valid_copy()
    if not newest:  # copies turned invalid meanwhile (e.g. quarantine): no fallback to reserve, so evict nothing
        free = []
    else:
        free = [d for d in free if os.path.normcase(d) != os.path.normcase(os.path.dirname(newest))]

    def total():
        return len(_copy_dirs()) + len(_leftover_dirs())
    # A prune frees what it can; a new copy evicts only when that can make room for it (a copy whose codex.exe
    # turns out to be running is refused, and stays valid, so a fallback copy always remains).
    for d in free:
        if total() + extra <= budget:
            break
        _discard(d)
    return total() + extra <= budget


def _private_codex_copy(src: str, version: str | None = None, budget: int = CODEX_COPY_BUDGET) -> str:
    """Copy codex.exe and its helpers to bin/codex-v<version>-<bundle12>/ (codex-<bundle12>/ when the version
    is unreadable), keyed by the bundle hash of all four files so changed bytes never reuse an old copy. The
    staging folder is reserved under bin/.lock within the disk budget BEFORE anything is copied; a copy is
    made only from a complete source that did not change while it was copied, every copied file's sha256 must
    match its source, and it is published under bin/.lock."""
    _guard_bin_writes()
    src_dir = os.path.dirname(src)
    if not _codex_complete(src_dir):
        raise FileNotFoundError(f'incomplete codex build in {src_dir}')
    before = {n: os.stat(os.path.join(src_dir, n)) for n in CODEX_FILES}
    bundle, shas = _bundle_sha256(src_dir, before)
    tag = f'v{version}-' if codex_version_key(version) else ''
    dest_dir = os.path.join(BIN, f'codex-{tag}{bundle[:12]}')
    dest = os.path.join(dest_dir, 'codex.exe')
    if _valid_copy(dest_dir):
        return dest
    with _bin_lock(60):
        if _valid_copy(dest_dir):
            return dest
        if os.path.isdir(dest_dir) and (_live_leases(dest_dir) or not _discard(dest_dir)):
            raise OSError(f'a damaged copy is still in use: {dest_dir}')
        if not _make_room(1, budget=budget):
            raise CopyBudgetFull(f'bin/ is at its budget of {budget} copy folders (in use, staging or undeleted); '
                                 'not staging another copy')
        pid = os.getpid()  # reserved, and named for its creator: counted by every other process from now
        tmp = tempfile.mkdtemp(prefix=f'.copy-{pid}-{process_created(pid) or 0}-', dir=BIN)
    try:
        for n in CODEX_FILES:
            shutil.copy2(os.path.join(src_dir, n), os.path.join(tmp, n))
        for n, b in before.items():
            a = os.stat(os.path.join(src_dir, n))
            if (a.st_size, a.st_mtime_ns, a.st_ino) != (b.st_size, b.st_mtime_ns, b.st_ino):
                raise OSError(f'codex build changed while it was copied: {n}')
        files = {}
        for n in CODEX_FILES:
            digest = _sha256_file(os.path.join(tmp, n))
            if digest != shas[n]:
                raise OSError(f'copied {n} does not match its source')
            c = os.stat(os.path.join(tmp, n))  # the rename below keeps the file index
            files[n] = {'size': c.st_size, 'mtime_ns': c.st_mtime_ns, 'ino': c.st_ino, 'sha256': digest}
        write_json(os.path.join(tmp, _MANIFEST), {
            'version': version, 'bundle_sha256': bundle, 'source': src, 'copied_at': iso(now()), 'files': files})
        _touch(os.path.join(tmp, _LAST_USED))
        with _bin_lock(60):
            if _valid_copy(dest_dir):  # a concurrent caller published it first
                return dest
            if os.path.isdir(dest_dir) and (_live_leases(dest_dir) or not _discard(dest_dir)):
                raise OSError(f'a damaged copy is still in use: {dest_dir}')
            os.rename(tmp, dest_dir)
    finally:
        if os.path.isdir(tmp):
            shutil.rmtree(tmp, ignore_errors=True)
    return dest


def _prune_codex_copies(keep: str, budget: int = CODEX_COPY_BUDGET) -> None:
    """Fit the budget around `keep`: a leased copy, or one whose codex.exe is running, always stays."""
    _guard_bin_writes()
    try:
        with _bin_lock(10):
            _make_room(0, keep, budget)
    except TimeoutError:  # another process holds the lock for a while: it prunes too
        pass


def bash_bin() -> str | None:
    for c in ('C' + r':\Program Files\Git\bin\bash.exe',
              'C' + r':\Program Files\Git\usr\bin\bash.exe'):
        if os.path.isfile(c):
            return c
    return shutil.which('bash')


def clean_claude_env(extra: dict | None = None) -> dict:
    """Env for a child claude: subscription login only (an ANTHROPIC_API_KEY would bill the API, and the
    desktop app's ANTHROPIC_BASE_URL breaks the CLI), and no nested-session markers."""
    env = dict(os.environ)
    for k in list(env):
        if k.startswith('ANTHROPIC_') or k in ('CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT', 'CLAUDE_CODE_USE_BEDROCK',
                                               'CLAUDE_CODE_USE_VERTEX', 'CLAUDE_CODE_USE_FOUNDRY',
                                               'CLAUDE_CODE_SAFE_MODE', 'CLAUDE_CODE_SSE_PORT'):
            env.pop(k, None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


# Environment variable NAMES that look like a secret: a code-profile worker never receives them, so a
# script it runs (python send.py) has no API key, token or password to send with.
SECRET_ENV_NAME = re.compile(r'TOKEN|SECRET|PASSWORD|PASSWD|PASSPHRASE|API_?KEY|PRIVATE_?KEY|CREDENTIAL|_KEY$|'
                             r'_PAT$|_DSN$|CONNECTION_?STRING|DATABASE_URL|^AWS_|^AZURE_|^GH_|^GITHUB_TOKEN$|'
                             r'^OPENAI_|^ANTHROPIC_|^SSH_AUTH_SOCK$', re.I)
# Secret-looking names an engine needs to log in. Empty: the Claude CLI logs in from
# ~/.claude/.credentials.json (twinlane already drops every ANTHROPIC_* variable, see clean_claude_env) and
# The Codex runner reads only CODEX_HOME and TWINLANE_CODEX_BIN, which are not secret-looking.
SECRET_ENV_KEEP: frozenset = frozenset()


def extra_secret_patterns(policy: dict | None) -> list[str]:
    """policy secrets.extra_env_name_patterns: regexes (matched case-insensitively with search) for variable
    names a code worker must also lose, for example a store's API prefix. ValueError when it is not a list of
    valid patterns: a pattern that silently matched nothing would leak the variable it was meant to hide."""
    raw = ((policy or {}).get('secrets') or {}).get('extra_env_name_patterns')
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(p, str) and p for p in raw):
        raise ValueError('secrets.extra_env_name_patterns must be a list of non-empty regex strings')
    for pattern in raw:
        try:
            re.compile(pattern, re.I)
        except re.error as e:
            raise ValueError(f'secrets.extra_env_name_patterns: invalid pattern {pattern!r}: {e}') from None
    return list(raw)


def strip_secret_env(env: dict, extra_patterns: list[str] | None = None) -> tuple[dict, list[str]]:
    """(env without secret-named variables, the stripped NAMES sorted). Values are never returned.
    `extra_patterns` (see extra_secret_patterns) add to the built-in name pattern."""
    extra = [re.compile(p, re.I) for p in extra_patterns or ()]
    names = sorted(k for k in env if k.upper() not in SECRET_ENV_KEEP
                   and (SECRET_ENV_NAME.search(k) or any(x.search(k) for x in extra)))
    return {k: v for k, v in env.items() if k not in names}, names


# ---------------------------------------------------------------- processes

# Windows process-creation and job-object constants.
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x8, 0x200, 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
CREATE_SUSPENDED = 0x00000004
JOB_LIMIT_BREAKAWAY_OK, JOB_LIMIT_SILENT_BREAKAWAY_OK, JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x800, 0x1000, 0x2000
_PROCESS_QUERY_LIMITED = 0x1000
_PROCESS_TERMINATE, _SYNCHRONIZE = 0x0001, 0x00100000
_JOB_EXTENDED_LIMITS = 9


class DetachError(OSError):
    """A background process cannot be guaranteed to outlive its caller."""


class DetachAmbiguous(DetachError):
    """A WMI start got far enough that a process may exist: never fall back to another mode.
    runner_may_run is False when the launcher won the hand-off arbiter (no child can run), True when
    that cannot be ruled out. pid/created identify the WMI-created process when known (both or none)."""

    def __init__(self, message: str, runner_may_run: bool = True, pid: int | None = None,
                 created: int | None = None):
        super().__init__(message)
        self.runner_may_run, self.pid, self.created = runner_may_run, pid, created


def _k32():
    import ctypes
    from ctypes import wintypes
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.GetExitCodeProcess.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
    k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(ctypes.c_uint64),) * 4
    k32.TerminateProcess.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.TerminateProcess.restype = wintypes.BOOL
    k32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    k32.WaitForSingleObject.restype = wintypes.DWORD
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
    k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
    k32.TerminateJobObject.restype = wintypes.BOOL
    k32.CreateToolhelp32Snapshot.argtypes = (wintypes.DWORD, wintypes.DWORD)
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.Thread32First.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    k32.Thread32Next.argtypes = (wintypes.HANDLE, ctypes.c_void_p)
    k32.OpenThread.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    k32.OpenThread.restype = wintypes.HANDLE
    k32.ResumeThread.argtypes = (wintypes.HANDLE,)
    k32.ResumeThread.restype = wintypes.DWORD
    k32.IsProcessInJob.argtypes = (wintypes.HANDLE, wintypes.HANDLE, ctypes.POINTER(wintypes.BOOL))
    k32.QueryInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD))
    k32.GetConsoleWindow.restype = wintypes.HWND
    return k32


def _job_limits_struct():
    """JOBOBJECT_EXTENDED_LIMIT_INFORMATION (only LimitFlags is used)."""
    import ctypes
    from ctypes import wintypes

    class Basic(ctypes.Structure):
        _fields_ = [('a', ctypes.c_int64), ('b', ctypes.c_int64), ('LimitFlags', wintypes.DWORD),
                    ('c', ctypes.c_size_t), ('d', ctypes.c_size_t), ('e', wintypes.DWORD), ('f', ctypes.c_size_t),
                    ('g', wintypes.DWORD), ('h', wintypes.DWORD)]

    class Extended(ctypes.Structure):
        _fields_ = [('Basic', Basic), ('Io', ctypes.c_uint64 * 6), ('m', ctypes.c_size_t * 4)]

    return Extended()


# Private to this runner process. A handle names the attempt's kernel object even
# after its root exits; descendants cannot escape by losing an intermediate parent.
_ENGINE_JOBS = {}
_UNCONTAINED = set()  # engine pids resumed without a job: nothing can later prove their descendants gone


def _win_resume_engine(proc, k32):
    """Popen closes the primary thread handle. Find that still-suspended thread
    through Toolhelp and resume it only after job assignment has been attempted."""
    import ctypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [('dwSize', ctypes.c_uint32), ('cntUsage', ctypes.c_uint32),
                    ('th32ThreadID', ctypes.c_uint32), ('th32OwnerProcessID', ctypes.c_uint32),
                    ('tpBasePri', ctypes.c_int32), ('tpDeltaPri', ctypes.c_int32),
                    ('dwFlags', ctypes.c_uint32)]

    snapshot = k32.CreateToolhelp32Snapshot(0x4, 0)  # TH32CS_SNAPTHREAD
    if snapshot in (None, 0, -1, ctypes.c_void_p(-1).value):
        raise OSError('cannot enumerate the suspended engine thread')
    try:
        entry = ThreadEntry()
        entry.dwSize = ctypes.sizeof(entry)
        found = k32.Thread32First(snapshot, ctypes.byref(entry))
        for _ in range(1_000_000):  # bounded even if a broken API never ends the enumeration
            if not found:
                break
            if entry.th32OwnerProcessID == proc.pid:
                thread = k32.OpenThread(0x2, False, entry.th32ThreadID)  # THREAD_SUSPEND_RESUME
                if not thread:
                    raise OSError('cannot open the suspended engine thread')
                try:
                    if k32.ResumeThread(thread) != 1:
                        raise OSError('cannot resume the suspended engine thread')
                    return
                finally:
                    k32.CloseHandle(thread)
            entry.dwSize = ctypes.sizeof(entry)
            found = k32.Thread32Next(snapshot, ctypes.byref(entry))
        raise OSError('suspended engine thread not found')
    finally:
        k32.CloseHandle(snapshot)


def contain_engine(proc, before_resume=None):
    """Called immediately after a CREATE_SUSPENDED Popen, before any engine code
    runs. Nesting/assignment failure permits a snapshot-only, unconfirmed kill.
    `before_resume(contained)` runs while the engine is still suspended; if it raises, the engine is never
    resumed (the runner's post-start cleanup kills it). Resume failure raises into the same cleanup path."""
    if not IS_WIN:
        return
    import ctypes
    k32 = _k32()
    handle = None
    try:
        try:
            handle = k32.CreateJobObjectW(None, None)
            if handle:
                limits = _job_limits_struct()
                limits.Basic.LimitFlags = JOB_LIMIT_KILL_ON_JOB_CLOSE
                if k32.SetInformationJobObject(handle, _JOB_EXTENDED_LIMITS, ctypes.byref(limits),
                                               ctypes.sizeof(limits)) \
                        and k32.AssignProcessToJobObject(handle, int(proc._handle)):
                    _ENGINE_JOBS[proc.pid] = (k32, handle, proc)
                    handle = None  # registry owns it until the attempt's result has been read
        except Exception:  # unavailable job APIs/nesting: resume with conservative snapshot cleanup
            pass
    finally:
        if handle:
            k32.CloseHandle(handle)
    contained = proc.pid in _ENGINE_JOBS
    if not contained:
        _UNCONTAINED.add(proc.pid)
    if before_resume is not None:
        before_resume(contained)
    _win_resume_engine(proc, k32)


def close_engine_job(proc) -> bool:
    """After reading the exited engine's output (or failed-attempt cleanup): end whatever the root left in the
    job and let go of it. True when there was no job or it reported zero active processes; False means a
    descendant may still run, and the caller must keep the worktree claim. Never closes an unrelated handle."""
    if proc is None:
        return True
    if proc.pid in _UNCONTAINED:  # no job ever held it: the root's exit proves nothing about its descendants
        _UNCONTAINED.discard(proc.pid)
        return False
    entry = _ENGINE_JOBS.get(proc.pid)
    if not entry:
        return True
    k32, handle = entry[:2]
    try:
        return _win_kill_job(entry)
    finally:  # an interrupt during the drain still lets go: KILL_ON_JOB_CLOSE then reaps the descendants
        if k32.CloseHandle(handle):
            _ENGINE_JOBS.pop(proc.pid, None)


def _win_kill_job(entry) -> bool:
    """Only a successful accounting query with ActiveProcesses == 0 proves exit.
    TerminateJobObject itself merely requests termination, like TerminateProcess."""
    import ctypes

    class Accounting(ctypes.Structure):
        _fields_ = [('times', ctypes.c_int64 * 4), ('TotalPageFaultCount', ctypes.c_uint32),
                    ('TotalProcesses', ctypes.c_uint32), ('ActiveProcesses', ctypes.c_uint32),
                    ('TotalTerminatedProcesses', ctypes.c_uint32)]

    k32, handle = entry[:2]
    try:
        k32.TerminateJobObject(handle, 1)
        deadline = time.monotonic() + 5
        while True:
            counts = Accounting()
            if not k32.QueryInformationJobObject(handle, 1, ctypes.byref(counts), ctypes.sizeof(counts), None):
                return False  # JobObjectBasicAccountingInformation unavailable: retain custody
            if counts.ActiveProcesses == 0:
                return True
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            time.sleep(min(0.05, left))
    except Exception:
        return False


_ERROR_ACCESS_DENIED, _ERROR_INVALID_PARAMETER, _STILL_ACTIVE = 5, 87, 259
_WIN_PID_MAX = 0xFFFFFFFF   # a Windows pid is a DWORD; a larger value would be silently truncated by ctypes
_STAMP_MAX = 2 ** 64 - 1    # a creation FILETIME (or POSIX start time) is an unsigned 64-bit value


def _valid_pid(pid, maximum: int = _WIN_PID_MAX) -> bool:
    """A recorded pid that can name exactly one process: a real int (not a bool) in 1..maximum."""
    return type(pid) is int and 0 < pid <= maximum


def _valid_stamp(created) -> bool:
    """A recorded creation stamp: a real int (not a bool, float or string) in 1..2**64-1."""
    return type(created) is int and 0 < created <= _STAMP_MAX


def _win_observe(pid, k32=None) -> dict:
    """Raw OpenProcess / GetExitCodeProcess / GetProcessTimes evidence for one pid, failing closed.

    Each call's last error is read immediately after it returns, before anything else can overwrite it, and
    an opened handle is always closed. `exists` is True (STILL_ACTIVE), False only for qualified absence
    (OpenProcess ERROR_INVALID_PARAMETER for a valid pid, or a readable exit code other than STILL_ACTIVE),
    and None when the probe cannot establish existence (for example an unexpected open error or a failed
    exit-code query). Access denial and a times failure after STILL_ACTIVE retain True with no identity.
    `created` is the creation FILETIME when readable. `k32` is injectable for tests.

    How callers (pid_state, observe_process) read the outcomes:
      - an unexpected OpenProcess error, or a failed or raising exit-code query: existence unknown, so
        UNKNOWN with or without a recorded stamp;
      - OpenProcess access denied (5), or failed process times after STILL_ACTIVE: it exists but opening it
        cannot read its identity. A process snapshot (_snapshot_identity) then supplies the creation stamp when
        it can, and that stamp is compared like any other. Without one the identity is unreadable: UNKNOWN
        against a recorded stamp and ALIVE when no stamp was recorded. ALIVE there still blocks a writer; it
        never claims a matching identity or any context equivalence."""
    import ctypes
    from ctypes import wintypes
    obs = {'exists': None, 'created': None, 'reason': 'not_attempted', 'stages': []}

    def stage(name, ok, error=None, **extra):
        obs['stages'].append(dict({'stage': name, 'attempted': True, 'ok': ok, 'last_error': error}, **extra))

    if not _valid_pid(pid):
        obs['reason'] = 'invalid_pid'
        return obs
    try:
        k32 = k32 or _k32()
    except Exception as e:  # noqa: BLE001 - no kernel32: nothing is proven
        stage('kernel32', False, exception=type(e).__name__)
        obs['reason'] = 'kernel_unavailable'
        return obs
    handle = None
    try:
        try:
            ctypes.set_last_error(0)
            handle = k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
            error = ctypes.get_last_error()
        except Exception as e:  # noqa: BLE001
            stage('open', False, exception=type(e).__name__)
            obs['reason'] = 'open_raised'
            handle = None
            return obs
        if not handle:
            stage('open', False, error)
            if error == _ERROR_ACCESS_DENIED:
                obs.update(exists=True, reason='access_denied')  # it exists; opening it cannot read its identity
                _snapshot_identity(obs, pid, k32, stage)
            elif error == _ERROR_INVALID_PARAMETER:
                obs.update(exists=False, reason='no_such_process')
            else:
                obs['reason'] = 'open_failed'
            return obs
        stage('open', True)
        code = wintypes.DWORD()
        try:
            ctypes.set_last_error(0)
            ok = k32.GetExitCodeProcess(handle, ctypes.byref(code))
            error = ctypes.get_last_error()
        except Exception as e:  # noqa: BLE001
            stage('exit_code', False, exception=type(e).__name__)
            obs['reason'] = 'exit_code_raised'
            return obs
        if not ok:
            stage('exit_code', False, error)
            obs['reason'] = 'exit_code_failed'
            return obs
        stage('exit_code', True, exit_code=int(code.value))
        if code.value != _STILL_ACTIVE:
            obs.update(exists=False, reason='exited')
            return obs
        times = [ctypes.c_uint64() for _ in range(4)]
        try:
            ctypes.set_last_error(0)
            ok = k32.GetProcessTimes(handle, *(ctypes.byref(x) for x in times))
            error = ctypes.get_last_error()
        except Exception as e:  # noqa: BLE001
            stage('times', False, exception=type(e).__name__)
            obs.update(exists=True, reason='times_raised')
            return obs
        if not ok:
            stage('times', False, error)
            obs.update(exists=True, reason='times_failed')  # STILL_ACTIVE: it runs, identity unreadable
            _snapshot_identity(obs, pid, k32, stage)
            return obs
        stage('times', True)
        obs.update(exists=True, created=int(times[0].value), reason='running')
        return obs
    finally:
        if handle:
            try:
                ctypes.set_last_error(0)
                closed = k32.CloseHandle(handle)
                error = ctypes.get_last_error()
                stage('close', bool(closed), None if closed else error)
            except Exception as e:  # noqa: BLE001 - a failed close never changes what was observed
                stage('close', False, exception=type(e).__name__)


_SYSTEM_PROCESS_INFORMATION = 5
_STATUS_INFO_LENGTH_MISMATCH = 0xC0000004
_SNAPSHOT_MAX_BYTES = 64 * 1024 * 1024
_SNAPSHOT_MAX_ATTEMPTS = 8


def _snapshot_processes_from(buf: bytes, ptr_size: int) -> list | None:
    """Validated (pid, parent pid, creation FILETIME) rows, or None for a malformed snapshot.

    Validate the entire chain before exposing any row, including a matching last row's offset.
    NextEntryOffset is unsigned: a wrapped backwards offset is rejected by the upper bound.
    Zero denotes the final entry, never a step back to the same entry."""
    if ptr_size not in (4, 8):
        return None
    pid_at, pid_fmt = (0x50, '<Q') if ptr_size == 8 else (0x44, '<I')
    # SYSTEM_PROCESS_INFORMATION's full fixed header and SYSTEM_THREAD_INFORMATION stride.
    # The consumed pid prefix alone cannot bound the trailing NumberOfThreads payload.
    need, thread_size = (0x100, 0x50) if ptr_size == 8 else (0xB8, 0x40)
    offset, rows = 0, []
    for _ in range(len(buf) // need):
        if offset + need > len(buf):
            return None
        (step,) = struct.unpack_from('<I', buf, offset)
        if step and (step < need or offset + step + need > len(buf)):
            return None
        entry_size = step or len(buf) - offset
        (threads,) = struct.unpack_from('<I', buf, offset + 4)
        if threads > (entry_size - need) // thread_size:
            return None
        entry = struct.unpack_from(pid_fmt, buf, offset + pid_at)[0]
        parent = struct.unpack_from(pid_fmt, buf, offset + pid_at + ptr_size)[0]
        created = struct.unpack_from('<q', buf, offset + 0x20)[0]
        rows.append((entry, parent, created))
        if step == 0:
            return rows
        offset += step
    return None


def _snapshot_created_from(buf: bytes, pid: int, ptr_size: int) -> int | None:
    """Creation FILETIME of `pid`, or None for an absent pid or any malformed entry."""
    if not _valid_pid(pid):
        return None
    rows = _snapshot_processes_from(buf, ptr_size)
    for entry, _, created in rows or ():
        if entry == pid:
            return created if _valid_stamp(created) else None
    return None


def _win_own_created() -> int | None:
    """This process's creation FILETIME from GetProcessTimes, or None."""
    import ctypes
    k32 = _k32()
    times = [ctypes.c_uint64() for _ in range(4)]
    if not k32.GetProcessTimes(k32.GetCurrentProcess(), *(ctypes.byref(x) for x in times)):
        return None
    return int(times[0].value) or None


def _win_process_snapshot(query=None) -> bytes | None:
    """Bounded NT query; an injected query has the same signature as NtQuerySystemInformation."""
    import ctypes
    try:
        if query is None:
            query = ctypes.WinDLL('ntdll').NtQuerySystemInformation
            query.restype = ctypes.c_ulong
            query.argtypes = (ctypes.c_ulong, ctypes.c_void_p, ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong))
        size = min(1 << 20, _SNAPSHOT_MAX_BYTES)
        for _ in range(_SNAPSHOT_MAX_ATTEMPTS):
            buf, needed = ctypes.create_string_buffer(size), ctypes.c_ulong(0)
            status = query(_SYSTEM_PROCESS_INFORMATION, buf, size, ctypes.byref(needed))
            if status == 0:
                if needed.value > size:
                    return None
                return buf.raw[:needed.value] if needed.value else buf.raw
            if status != _STATUS_INFO_LENGTH_MISMATCH:
                return None
            size = max(size * 2, needed.value + (1 << 16))
            if size > _SNAPSHOT_MAX_BYTES:
                return None
    except Exception:  # noqa: BLE001 - a failed query proves nothing
        pass
    return None


def _win_snapshot_data():
    """(buffer, pointer size) of one SystemProcessInformation snapshot, or None.

    The snapshot is trusted only when it gives THIS process the creation time GetProcessTimes gives it, which
    checks the entry layout on this machine and bitness. Any failure is None."""
    import ctypes
    try:
        data = _win_process_snapshot()
        if data is None:
            return None
        ptr = ctypes.sizeof(ctypes.c_void_p)
        own = _snapshot_created_from(data, os.getpid(), ptr)
        if own is None or own != _win_own_created():
            return None
        return data, ptr
    except Exception:  # noqa: BLE001 - no snapshot proves nothing
        return None


def _win_snapshot_created(pid: int) -> int | None:
    """The creation FILETIME of `pid` from one NtQuerySystemInformation(SystemProcessInformation) snapshot.

    The snapshot lists every process without opening any, so a process this token may not open (for example a
    SYSTEM service that now holds a recycled pid) still has a readable creation time. Any failure is None:
    the identity stays unreadable."""
    snap = _win_snapshot_data()
    return _snapshot_created_from(snap[0], pid, snap[1]) if snap else None


_WIN_SYSTEM_PID = 4


def _win_boot_created() -> int | None:
    """When this boot began, as the creation FILETIME of the System process (pid 4) from the process snapshot,
    or None. Every other process is created after it, so a recorded stamp EARLIER than this belongs to a
    process that cannot still exist. Any failure is None: no boot proof."""
    return _win_snapshot_created(_WIN_SYSTEM_PID)


def _predates_boot(created, k32=None) -> bool:
    """True only when the recorded creation stamp is provably older than this boot, so the process it names
    is gone even though its pid now belongs to a process whose identity this token cannot read. A real kernel32
    reads the real boot time; a scripted test kernel opts in with a `boot_created()` method. Not a Windows
    probe, or any failure to read the boot time: False (the identity stays unreadable, never DEAD)."""
    import ctypes
    if not IS_WIN or not _valid_stamp(created):
        return False
    reader = _win_boot_created if k32 is None or isinstance(k32, ctypes.CDLL) else getattr(k32, 'boot_created', None)
    if reader is None:
        return False
    try:
        boot = reader()
    except Exception:  # noqa: BLE001 - no boot time proves nothing
        return False
    return _valid_stamp(boot) and created < boot


def _snapshot_identity(obs: dict, pid: int, k32, stage) -> None:
    """Fill obs['created'] from a process snapshot when opening the process could not read it.

    A real kernel32 uses the real snapshot; a scripted test kernel opts in with a `snapshot_created(pid)`
    method, so a test never reads a real process by accident. Found: the stamp, compared by the caller
    like any other. Not found or failed: obs is unchanged (exists, identity unreadable)."""
    import ctypes
    reader = _win_snapshot_created if isinstance(k32, ctypes.CDLL) else getattr(k32, 'snapshot_created', None)
    if reader is None:
        return
    try:
        created = reader(pid)
    except Exception as e:  # noqa: BLE001 - a failed snapshot never changes what was observed
        stage('snapshot', False, exception=type(e).__name__)
        return
    if _valid_stamp(created):
        stage('snapshot', True)
        obs['created'] = created
    else:
        stage('snapshot', False)


def _win_process_info(pid: int, k32=None) -> tuple[bool | None, int | None]:
    """(exists, creation FILETIME) for a pid. exists is None when it cannot be established (an API failure);
    creation is None when the process cannot be opened or its times cannot be read."""
    obs = _win_observe(pid, k32)
    return obs['exists'], obs['created']


_NO_SPAWN_LIVENESS = ContextVar('twinlane_no_spawn_liveness', default=False)


@contextmanager
def nonspawning_liveness():
    """Observe identities without ps; unreadable identities remain UNKNOWN. Local to this context."""
    token = _NO_SPAWN_LIVENESS.set(True)
    try:
        yield
    finally:
        _NO_SPAWN_LIVENESS.reset(token)


def _posix_zombie(pid: int) -> bool:
    """True when the pid is a zombie: killed or exited and only waiting for its parent to reap it."""
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii', errors='replace') as h:
            return h.read().rsplit(')', 1)[1].split()[0] == 'Z'
    except (OSError, IndexError):
        pass
    if sys.platform != 'darwin' or _NO_SPAWN_LIVENESS.get():
        return False
    try:
        result = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True, timeout=3)
        return result.returncode == 0 and result.stdout.strip().startswith('Z')
    except (OSError, subprocess.TimeoutExpired):
        return False


def _posix_start_time(pid: int) -> int | None:
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii', errors='replace') as h:
            return int(h.read().rsplit(')', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        pass
    if sys.platform != 'darwin' or _NO_SPAWN_LIVENESS.get():
        return None
    try:
        result = subprocess.run(['ps', '-o', 'lstart=', '-p', str(pid)], capture_output=True,
                                text=True, timeout=3, env=dict(os.environ, LC_ALL='C'))
        if result.returncode:
            return None
        with _c_locale_time():
            return int(time.mktime(time.strptime(result.stdout.strip(), '%a %b %d %H:%M:%S %Y')))
    except (OSError, ValueError, locale.Error, subprocess.TimeoutExpired):
        return None


@contextmanager
def _c_locale_time():
    previous = locale.setlocale(locale.LC_TIME)
    try:
        locale.setlocale(locale.LC_TIME, 'C')
        yield
    finally:
        locale.setlocale(locale.LC_TIME, previous)


def process_created(pid) -> int | None:
    """An opaque creation stamp for a live pid (None if dead or unknown). Store it with the pid so a
    recycled pid is never mistaken for the original process."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if IS_WIN:
        alive, created = _win_process_info(pid)
        return created if alive else None
    return _posix_start_time(pid)


ALIVE, DEAD, UNKNOWN = 'alive', 'dead', 'unknown'


def pid_state(pid, created=None, *, spawn=True) -> str:
    """ALIVE, DEAD or UNKNOWN for a pid. With `created`, ALIVE means it is that same process (not a reused
    pid) and a readable creation time that differs is DEAD. On Windows a process this token cannot open still
    has its creation time read from a process snapshot, so a pid recycled by a SYSTEM service reads DEAD.
    UNKNOWN is a process that exists but whose creation time cannot be read by either route, or a pid whose
    existence the OS probe could not establish (with or without a stamp): it is neither provably ours nor
    provably gone, so callers must not call it dead and must not kill it. NEVER uses os.kill on Windows
    (signal 0 would TerminateProcess). With spawn=False, POSIX never falls back to ps;
    a live pid with an unreadable recorded identity returns UNKNOWN."""
    if not spawn:
        with nonspawning_liveness():
            return pid_state(pid, created)
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return DEAD
    if pid <= 0:
        return DEAD
    if IS_WIN:
        alive, actual = _win_process_info(pid)
    else:
        try:
            os.kill(pid, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if alive and _posix_zombie(pid):
            return DEAD
        actual = _posix_start_time(pid) if alive else None
    if alive is None:
        return UNKNOWN  # the probe itself failed: neither its existence nor its absence is proven
    if not alive:
        return DEAD
    if created in (None, ''):
        return ALIVE
    if actual is None:
        # Exists, identity unreadable: a recorded stamp older than this boot still names a gone process
        # (the pid was recycled by something this token cannot read after a restart).
        return DEAD if IS_WIN and _predates_boot(_stamp_or_none(created)) else UNKNOWN
    try:
        return ALIVE if int(created) == actual else DEAD
    except (TypeError, ValueError):
        return DEAD


def _stamp_or_none(created):
    """`created` as a valid stamp, or None (a malformed recorded value is never boot proof)."""
    try:
        value = int(created)
    except (TypeError, ValueError):
        return None
    return value if _valid_stamp(value) else None


def pid_alive(pid, created=None) -> bool:
    """True only when the process is PROVABLY alive (and, with `created`, is that same process). An unreadable
    identity is False here: use pid_maybe_alive when "could still be running" is the safe answer."""
    return pid_state(pid, created) == ALIVE


def pid_maybe_alive(pid, created=None) -> bool:
    """False only when the process is provably gone or provably another process; an unreadable identity
    counts as possibly alive. For slot counts, continuation guards and kill-failure checks."""
    return pid_state(pid, created) != DEAD


_POSIX_PID_MAX = 2 ** 31 - 1


def _malformed(value, valid: bool) -> str | None:
    """Why a recorded pid or stamp is unusable, as a fixed word (never the value itself)."""
    if valid or value is None:
        return None
    if isinstance(value, bool):
        return 'bool'
    if isinstance(value, int):
        return 'out_of_range'
    if isinstance(value, float):
        return 'float'
    return 'not_an_int'


def _posix_observe(pid: int) -> dict:
    obs = {'exists': None, 'created': None, 'reason': 'not_attempted', 'stages': []}
    try:
        os.kill(pid, 0)
        obs['stages'].append({'stage': 'signal0', 'attempted': True, 'ok': True, 'last_error': None})
        obs['exists'] = True
    except ProcessLookupError:
        obs['stages'].append({'stage': 'signal0', 'attempted': True, 'ok': False, 'last_error': 'ESRCH'})
        obs.update(exists=False, reason='no_such_process')
        return obs
    except PermissionError:
        obs['stages'].append({'stage': 'signal0', 'attempted': True, 'ok': False, 'last_error': 'EPERM'})
        obs['exists'] = True
    except (OSError, OverflowError) as e:
        obs['stages'].append({'stage': 'signal0', 'attempted': True, 'ok': False,
                              'last_error': getattr(e, 'errno', None), 'exception': type(e).__name__})
        obs['reason'] = 'signal0_failed'
        return obs
    if _posix_zombie(pid):
        obs.update(exists=False, reason='exited')
        return obs
    created = _posix_start_time(pid)
    obs['stages'].append({'stage': 'start_time', 'attempted': True, 'ok': created is not None, 'last_error': None})
    obs.update(created=created, reason='running' if created is not None else 'times_failed')
    return obs


def observe_process(pid, expected_created=None, k32=None) -> dict:
    """One JSON-safe, allowlisted observation of a recorded (pid, creation stamp) identity.

    It is evidence seen by THIS process's token at this moment only: another process (for example the
    real writer claimant) may see more or less, and nothing here claims context equivalence or grants
    anything. Stamps are exact decimal strings (a FILETIME exceeds 2**53 and must not pass through a float).
    The identity is validated strictly first: a bool, float, string or out-of-range pid or stamp is never
    probed and never DEAD (`state` UNKNOWN). With a valid identity, DEAD needs qualified absence, a readable
    exit, or a readable creation stamp that differs. Unknown existence (unexpected open errors or failed
    exit-code queries) is UNKNOWN with or without a stamp.
    A process that cannot be opened (access denied, or failed times while STILL_ACTIVE) is compared by its
    snapshot creation stamp when one is readable (stage 'snapshot'); otherwise it is UNKNOWN against a stamp
    and ALIVE without one: a conservative "it runs", never a matching identity.
    No environment, token or command line is read or reported."""
    pid_max = _WIN_PID_MAX if IS_WIN else _POSIX_PID_MAX
    pid_ok = _valid_pid(pid, pid_max)
    stamp_ok = expected_created is None or _valid_stamp(expected_created)
    out = {'platform': 'win32' if IS_WIN else 'posix', 'observer': 'current-process-token',
           'pid': pid if pid_ok else None, 'pid_problem': _malformed(pid, pid_ok) or (None if pid_ok else 'missing'),
           'expected_created': str(expected_created) if stamp_ok and expected_created is not None else None,
           'expected_created_problem': _malformed(expected_created, stamp_ok),
           'actual_created': None, 'exists': None, 'state': UNKNOWN, 'reason': None, 'stages': []}
    if not pid_ok or not stamp_ok:
        out['reason'] = 'invalid_identity'
        return out
    raw = _win_observe(pid, k32) if IS_WIN or k32 is not None else _posix_observe(pid)
    stages = []
    for s in raw.get('stages') or []:
        clean = {k: s.get(k) for k in ('stage', 'attempted', 'ok', 'last_error') if k in s}
        if s.get('exit_code') is not None:
            clean['exit_code'] = int(s['exit_code'])
        if s.get('exception'):
            clean['exception'] = str(s['exception'])
        stages.append(clean)
    actual = raw.get('created')
    out.update(stages=stages, exists=raw.get('exists'), reason=raw.get('reason'),
               actual_created=str(actual) if type(actual) is int else None)
    if raw.get('exists') is None:
        out['state'] = UNKNOWN
    elif raw.get('exists') is False:
        out['state'] = DEAD
    elif expected_created is None:
        out['state'] = ALIVE
    elif type(actual) is not int:
        if _predates_boot(expected_created, k32):
            out['state'], out['reason'] = DEAD, 'predates_boot'
        else:
            out['state'], out['reason'] = UNKNOWN, out['reason'] or 'identity_unreadable'
    elif actual == expected_created:
        out['state'] = ALIVE
    else:
        out['state'], out['reason'] = DEAD, 'creation_mismatch'
    return out


def _win_process_tree_snapshot() -> list | None:
    """Parent links from the same bounded, self-checked snapshot used by the identity fallback."""
    import ctypes
    try:
        data = _win_process_snapshot()
        if data is None:
            return None
        ptr = ctypes.sizeof(ctypes.c_void_p)
        own = _snapshot_created_from(data, os.getpid(), ptr)
        if own is None or own != _win_own_created():
            return None
        return _snapshot_processes_from(data, ptr)
    except Exception:  # noqa: BLE001 - unknown tree membership is never permission to kill
        return None


def _win_process_identity(handle, k32, query=None) -> tuple | None:
    """(pid, recorded parent pid, monotonic process sequence) from this process handle.

    NtQueryInformationProcess classes 0 and 92 return PROCESS_BASIC_INFORMATION and
    ProcessSequenceNumber (ULONGLONG). Unsupported queries refuse descendant membership;
    a scripted kernel must opt in explicitly and never falls through to native APIs.
    Layouts: https://github.com/winsiderss/phnt/blob/master/ntpsapi.h
    """
    import ctypes
    try:
        if query is None and not isinstance(k32, ctypes.CDLL):
            reader = getattr(k32, 'process_identity', None)
            identity = reader(handle) if reader else None
        else:
            if query is None:
                query = ctypes.WinDLL('ntdll').NtQueryInformationProcess
                query.restype = ctypes.c_long
                query.argtypes = (ctypes.c_void_p, ctypes.c_ulong, ctypes.c_void_p,
                                  ctypes.c_ulong, ctypes.POINTER(ctypes.c_ulong))
            # The first four pointer-sized slots cover ExitStatus/padding, PEB,
            # affinity and priority/padding; pid and inherited pid are slots 4 and 5.
            basic = (ctypes.c_void_p * 6)()
            size = ctypes.c_ulong()
            if query(handle, 0, ctypes.byref(basic), ctypes.sizeof(basic), ctypes.byref(size)) != 0 \
                    or size.value != ctypes.sizeof(basic):
                return None
            sequence, size = ctypes.c_uint64(), ctypes.c_ulong()
            if query(handle, 92, ctypes.byref(sequence), ctypes.sizeof(sequence), ctypes.byref(size)) != 0 \
                    or size.value != ctypes.sizeof(sequence):
                return None
            identity = (int(basic[4] or 0), int(basic[5] or 0), int(sequence.value))
        if not isinstance(identity, tuple) or len(identity) != 3:
            return None
        pid, parent, sequence = identity
        if _valid_pid(pid) and type(parent) is int and 0 <= parent <= _WIN_PID_MAX and _valid_stamp(sequence):
            return identity
    except Exception:  # noqa: BLE001 - no ordering proof means no descendant permission
        pass
    return None


def _win_tree_gone(handles, members, before, k32) -> bool:
    """Best-effort wait for snapshot targets. Snapshots cannot prove tree exit:
    an intermediate parent can exit between them, hiding its surviving children.
    Only a runner-owned job's zero ActiveProcesses can confirm a Windows kill."""
    wait = getattr(k32, 'WaitForSingleObject', None)
    if wait is None:
        return False
    deadline = time.monotonic() + 5
    for handle in handles:
        if wait(handle, max(0, int((deadline - time.monotonic()) * 1000))) != 0:  # WAIT_OBJECT_0
            return False
    after = _win_process_tree_snapshot()
    if after is None or before is None:
        return False
    return False  # even no new direct child is not proof about transient parents


def _win_kill_tree(pid: int, created=None, k32=None) -> bool:
    """Pin each verified process object until termination; never kill by numeric pid.

    A failed root verification kills nothing. Unverifiable descendants are skipped individually;
    False reports incomplete enumeration, verification or termination to callers.
    The snapshot bounds membership in time: children born after it may survive.
    Snapshot-only cleanup therefore always reports False."""
    import ctypes
    handles = []
    try:
        if not _valid_pid(pid):
            return False
        k32 = k32 or _k32()

        def acquire(target, expected=None):
            handle = k32.OpenProcess(_PROCESS_TERMINATE | _PROCESS_QUERY_LIMITED | _SYNCHRONIZE, False, target)
            if not handle:
                return None
            handles.append(handle)  # even failed/raising identity queries must close it
            times = [ctypes.c_uint64() for _ in range(4)]
            if not k32.GetProcessTimes(handle, *(ctypes.byref(x) for x in times)):
                return None
            stamp = int(times[0].value)
            if not _valid_stamp(stamp) or (expected is not None and stamp != expected):
                return None
            return handle, stamp

        expected = None if created in (None, '') else int(created)
        root = acquire(pid, expected)
        if root is None:
            return False
        rows = _win_process_tree_snapshot()
        complete = rows is not None and (pid, root[1]) in {(p, c) for p, _, c in rows}
        children = {}
        if complete:
            for child, parent, stamp in rows:
                if _valid_pid(child):
                    children.setdefault(parent, []).append((child, stamp))
        root_identity = _win_process_identity(root[0], k32)
        if root_identity is None or root_identity[0] != pid:
            complete = False
        targets, queue, seen = [root[0]], [], {pid}
        if complete:
            queue.append((pid, root_identity[2]))
        for parent, parent_sequence in queue:
            for child, stamp in children.get(parent, ()):
                if child in seen:
                    continue
                seen.add(child)
                if not _valid_stamp(stamp):
                    complete = False
                    continue
                try:
                    verified = acquire(child, stamp)
                except Exception:  # noqa: BLE001 - one inaccessible child must not prevent the other kills
                    verified = None
                if verified is None:
                    complete = False
                    continue
                identity = _win_process_identity(verified[0], k32)
                if identity is None or identity[:2] != (child, parent) or identity[2] <= parent_sequence:
                    complete = False
                    continue
                # Every ancestor stays pinned. An older orphan cannot belong to a
                # later incarnation of its recorded parent pid, even across clock rollback.
                queue.append((child, identity[2]))
                targets.append(verified[0])
        for handle in reversed(targets):
            try:
                if not k32.TerminateProcess(handle, 1):
                    complete = False
            except Exception:  # noqa: BLE001
                complete = False
        if complete and not _win_tree_gone(targets, seen, rows, k32):
            complete = False
        return complete
    except Exception:  # noqa: BLE001 - no pid-only fallback on any failure
        return False
    finally:
        for handle in reversed(handles):
            try:
                k32.CloseHandle(handle)
            except Exception:  # noqa: BLE001 - still close the remaining handles
                pass


def kill_tree(pid, created=None) -> bool:
    """Kill a process tree. With `created`, refuses unless the pid is still that same process.
    On Windows only an owned job's zero ActiveProcesses confirms the whole tree;
    snapshot-only cleanup is best effort and always reports False."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if IS_WIN:
        entry = _ENGINE_JOBS.get(pid)
        if entry is not None:
            if created not in (None, ''):
                import ctypes
                try:
                    times = [ctypes.c_uint64() for _ in range(4)]
                    if not entry[0].GetProcessTimes(int(entry[2]._handle), *(ctypes.byref(x) for x in times)) \
                            or times[0].value != int(created):
                        return False
                except Exception:
                    return False
            return _win_kill_job(entry)
        return _win_kill_tree(pid, created)
    if created not in (None, '') and not pid_alive(pid, created):
        return False  # a reused pid, or an identity that cannot be read: never kill what is not provably it
    try:
        group = os.getpgid(pid)
        if group == pid and group != os.getpgrp():
            os.killpg(group, 9)
        else:
            os.kill(pid, 9)
        return True
    except ProcessLookupError:
        return False
    except OSError:
        return False


def no_window_flags() -> int:
    """CREATE_NO_WINDOW when this process has no console window. A console child of a process with
    no console (a detached runner) would otherwise get a new VISIBLE console, and closing that window
    kills the child."""
    if not IS_WIN:
        return 0
    try:
        return 0 if _k32().GetConsoleWindow() else CREATE_NO_WINDOW
    except Exception:
        return CREATE_NO_WINDOW


def job_info() -> dict:
    """The Windows job object this process runs in: {in_job, kill_on_close, breakaway_ok}.

    Only the innermost job is visible; limits of enclosing jobs are not."""
    if not IS_WIN:
        return {'in_job': False, 'kill_on_close': False, 'breakaway_ok': True}
    import ctypes
    from ctypes import wintypes
    try:
        k32 = _k32()
        inj = wintypes.BOOL()
        if not k32.IsProcessInJob(k32.GetCurrentProcess(), None, ctypes.byref(inj)):
            return {'in_job': None, 'kill_on_close': None, 'breakaway_ok': None}
        if not inj.value:
            return {'in_job': False, 'kill_on_close': False, 'breakaway_ok': True}
        ext, n = _job_limits_struct(), wintypes.DWORD()
        if not k32.QueryInformationJobObject(None, _JOB_EXTENDED_LIMITS, ctypes.byref(ext), ctypes.sizeof(ext),
                                             ctypes.byref(n)):
            return {'in_job': True, 'kill_on_close': None, 'breakaway_ok': None}
        flags = ext.Basic.LimitFlags
        return {'in_job': True, 'kill_on_close': bool(flags & JOB_LIMIT_KILL_ON_JOB_CLOSE),
                'breakaway_ok': bool(flags & (JOB_LIMIT_BREAKAWAY_OK | JOB_LIMIT_SILENT_BREAKAWAY_OK))}
    except Exception:
        return {'in_job': None, 'kill_on_close': None, 'breakaway_ok': None}


_CONTAINER = None


def contain_children() -> bool:
    """Put THIS process in a new kill-on-close job object, so every child it starts dies with it.

    A runner killed without /T (or crashing) then cannot leave claude.exe or codex running with no
    timeout and no receipt. False when the OS refuses (non-Windows, or a parent job that forbids
    nesting)."""
    global _CONTAINER
    if not IS_WIN:
        return False
    if _CONTAINER is not None:
        return True
    import ctypes
    try:
        k32 = _k32()
        job = k32.CreateJobObjectW(None, None)
        if not job:
            return False
        ext = _job_limits_struct()
        ext.Basic.LimitFlags = JOB_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, _JOB_EXTENDED_LIMITS, ctypes.byref(ext), ctypes.sizeof(ext)) \
                or not k32.AssignProcessToJobObject(job, k32.GetCurrentProcess()):
            k32.CloseHandle(job)
            return False
        _CONTAINER = job  # never closed here: the handle dies with this process and the job kills the rest
        return True
    except Exception:
        return False


# Win32_Process.Create through Windows PowerShell's System.Management ([wmiclass]; ~3 s here, the
# CIM cmdlets take ~10 s). The command line and directory travel in env vars, so nothing is quoted
# into the script. CreateFlags must be non-zero (0 is rejected with 21, invalid parameter).
# Everything that can fail before Create is in the try: its catch prints WMI_PRECREATE and exits 3,
# the only outcome proven to have started nothing (the caller may fall back only then).
WMI_PRECREATE = 'twinlane-WMI-PRECREATE'
_WMI_CREATE_PS = (
    "$ErrorActionPreference = 'Stop'\n"
    "try {\n"
    "  $cls = [wmiclass]'Win32_Process'\n"
    "  $si = ([wmiclass]'Win32_ProcessStartup').CreateInstance()\n"
    "  $si.ShowWindow = 0\n"
    "  $si.CreateFlags = [uint32]$env:TWINLANE_WMI_FLAGS\n"
    f"}} catch {{ '{WMI_PRECREATE} ' + ($_.Exception.Message -replace '\\s+', ' '); exit 3 }}\n"
    "$r = $cls.Create($env:TWINLANE_WMI_CMD, $env:TWINLANE_WMI_CWD, $si)\n"
    '"$($r.ReturnValue) $($r.ProcessId)"\n')
WMI_TIMEOUT_S = 60
HANDOFF_WAIT_S = 20


def _filetime_now() -> int:
    return int((time.time() + 11644473600) * 10 ** 7)


def _powershell() -> str:
    exe = os.path.join(os.environ.get('SystemRoot') or ('C' + r':\Windows'), 'System32', 'WindowsPowerShell', 'v1.0',
                       'powershell.exe')
    return exe if os.path.isfile(exe) else 'powershell'


def _remove_retry(path: str) -> None:
    """Delete a file, retrying a transient sharing violation (an antivirus scan); the sweeper gets the rest."""
    for n in range(20):
        try:
            os.remove(path)
            return
        except FileNotFoundError:
            return
        except PermissionError:
            if n == 19:
                return
            time.sleep(0.1)


def _spawn_wmi(argv: list[str], cwd: str | None, env: dict | None, log_path: str | None,
               handoff: str) -> dict:
    """Start argv + [--env-handoff, handoff] through WMI, outside the caller's job object (its parent
    is WmiPrvSE; its own job neither kills on close nor forbids breakaway). Returns {pid, created}
    of the child that won the hand-off arbiter (envhandoff: `runner.decided`, O_EXCL).

    Fallback safety: plain DetachError only when nothing can have started (the hand-off could not
    be written, PowerShell could not be launched, or the script failed before Create). Every later
    failure raises DetachAmbiguous, which callers must not fall back from. Whenever it gives up
    (any failure, or no claim within HANDOFF_WAIT_S) the launcher decides `launcher` on the arbiter:
    if that wins, no child can ever run and the hand-off is deleted; if a child had decided first,
    that child is the runner and is returned, whatever PowerShell printed."""
    import re
    from . import envhandoff
    try:
        envhandoff.write(handoff, os.environ if env is None else env, log_path, cwd)
    except OSError as e:
        raise DetachError(f'could not write the env hand-off: {type(e).__name__}') from None
    decided = envhandoff.decided_path(handoff)
    pid = created = None
    error = None
    try:
        t0 = _filetime_now()
        ps_env = dict(os.environ, TWINLANE_WMI_CMD=subprocess.list2cmdline(list(argv) + [envhandoff.ARG, handoff]),
                      TWINLANE_WMI_CWD=cwd or os.getcwd(), TWINLANE_WMI_FLAGS=str(CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW))
        try:
            r = subprocess.run([_powershell(), '-NoProfile', '-NonInteractive', '-Command', _WMI_CREATE_PS],
                               env=ps_env, capture_output=True, text=True, encoding='utf-8', errors='replace',
                               timeout=WMI_TIMEOUT_S, stdin=subprocess.DEVNULL, creationflags=CREATE_NO_WINDOW)
        except OSError as e:
            raise DetachError(f'could not launch PowerShell for WMI: {type(e).__name__}') from None
        except subprocess.TimeoutExpired:
            raise DetachAmbiguous(f'PowerShell did not answer within {WMI_TIMEOUT_S} s; Create may have run') \
                from None
        lines = [x.strip() for x in (r.stdout or '').splitlines() if x.strip()]
        pre = [x for x in lines if x.startswith(WMI_PRECREATE)]
        if r.returncode == 3 and pre:
            raise DetachError('WMI unavailable before Create: ' + pre[-1][len(WMI_PRECREATE):].strip()[:200])
        found = [m for m in (re.match(r'^(\d+) (\d+)$', x) for x in lines) if m]
        if not found:
            codes = [x for x in lines if x.isdigit()]
            err = ' '.join((r.stderr or '').split())[:200]
            raise DetachAmbiguous(f'Win32_Process.Create returned {codes[-1]}' if codes else
                                  f'no ProcessId from WMI (rc {r.returncode}): {err or "no output"}')
        rv, pid = int(found[-1].group(1)), int(found[-1].group(2))
        if rv != 0 or pid == 0:
            pid = None
            raise DetachAmbiguous(f'Win32_Process.Create returned {rv}')
        created = process_created(pid)  # the stamp, right away: every pid twinlane records carries one
        if created is not None and created < t0 - 2 * 10 ** 7:
            pid = created = None  # not ours: never kill or record it
            raise DetachAmbiguous('WMI returned a pid that belongs to an older process')
        deadline = time.time() + HANDOFF_WAIT_S
        while not os.path.exists(decided) and pid_alive(pid, created) and time.time() < deadline:
            time.sleep(0.05)
    except BaseException as e:  # noqa: BLE001 - the arbiter is settled on every path, then re-raised
        error = e
    try:
        won = envhandoff.decide(handoff, f'launcher {int(time.time())}')
    except OSError:
        won = None
    if won is False:  # a child decided first: it is the runner
        words = envhandoff.read_decision(handoff)
        if error is not None and not isinstance(error, Exception):
            raise error  # KeyboardInterrupt and the like
        if len(words) == 3 and words[0] == 'child' and words[1].isdigit() and words[2].isdigit():
            cpid = int(words[1])
            cstamp = int(words[2])
            deadline = time.time() + 5  # it deletes the hand-off right after reading it
            while os.path.exists(handoff) and time.time() < deadline:
                time.sleep(0.05)
            return {'pid': cpid, 'created': cstamp}
        raise DetachAmbiguous('the hand-off decision is unreadable; a runner may exist', pid=pid, created=created)
    if won is None:  # could not record a decision
        if isinstance(error, DetachError) and not isinstance(error, DetachAmbiguous):
            _remove_retry(handoff)  # failed before Create: no child exists
            raise error
        raise DetachAmbiguous('could not record the launcher decision; a runner may exist', pid=pid, created=created)
    _remove_retry(handoff)  # the launcher decided: no child can ever read it
    if isinstance(error, DetachAmbiguous):
        error.runner_may_run = False
        error.pid, error.created = error.pid or pid, error.created or created
        error.args = (str(error) + '; the launcher decided first, so no runner can start',)
        raise error
    if error is not None:
        raise error
    # Started but did not claim the hand-off in time: it can no longer (it exits 5); a failed start.
    if pid and created and pid_alive(pid, created):
        kill_tree(pid, created)
    raise DetachAmbiguous(f'the WMI-started process did not claim its env hand-off within {HANDOFF_WAIT_S} s',
                          runner_may_run=False, pid=pid, created=created)


def spawn_detached_ex(argv: list[str], cwd: str | None = None, env: dict | None = None,
                      log_path: str | None = None, require_survival: bool = False,
                      handoff: str | None = None) -> dict:
    """Start a process that outlives the caller. Returns {pid, created, mode, job}.

    mode: 'wmi' (created by WMI: parent WmiPrvSE, outside every job of the caller), 'breakaway'
    (CREATE_BREAKAWAY_FROM_JOB), 'in-job' (still in the caller's job), 'no-job' (the caller is in
    no job object) or 'posix'. wmi needs `handoff` (a file path in the job folder) and a child that
    loads it (envhandoff.load; cli.py does for `_job`), since WMI passes neither env nor stdio. A
    wmi attempt that may have created a process raises DetachAmbiguous and is never followed by
    another mode.

    With require_survival (`--bg`), every Windows caller gets wmi or DetachError, regardless of
    job membership. job_info sees only the innermost job, and breakaway may leave an enclosing
    job attached. A later probe reproduced that hazard after a real app quit killed breakaway runners;
    that quit's exact termination mechanism was not traced. Direct starts also keep
    the caller as parent even outside all jobs, so a tree kill while the caller lives reaches them.

    Without require_survival (the hook's best-effort refresh) the order is breakaway, then wmi
    (only with a hand-off), then in-job.
    On Windows the child gets its own hidden console (CREATE_NO_WINDOW), which its console children
    share, so no window ever appears."""
    info = job_info()
    out = open(log_path, 'ab') if log_path else subprocess.DEVNULL
    try:
        if IS_WIN:
            base = CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
            if require_survival:
                if not handoff:
                    raise DetachError('Windows background survival requires a WMI start outside the caller\'s '
                                      'job and process tree, and it needs an env hand-off')
                try:
                    return {**_spawn_wmi(argv, cwd, env, log_path, handoff), 'mode': 'wmi', 'job': info}
                except DetachAmbiguous:
                    raise  # a process may exist: a fallback could run the job twice
                except DetachError as e:
                    raise DetachError(f'the WMI start failed ({e}); direct starts can die with the caller\'s '
                                      'job or process tree, so none was tried') from None
            wmi_error = None
            order = [(base | CREATE_BREAKAWAY_FROM_JOB, 'breakaway'), (None, 'wmi'), (base, 'in-job')] \
                if info.get('in_job') is not False else [(base, 'no-job')]
            for flags, mode in order:
                if mode == 'wmi':
                    if not handoff:
                        continue
                    try:
                        return {**_spawn_wmi(argv, cwd, env, log_path, handoff), 'mode': mode, 'job': info}
                    except DetachAmbiguous:
                        raise  # a process may exist: a fallback could run the job twice
                    except DetachError as e:
                        wmi_error = str(e)
                        continue
                try:
                    p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                                         stderr=subprocess.STDOUT, creationflags=flags, close_fds=True)
                except OSError:
                    continue
                return {'pid': p.pid, 'created': process_created(p.pid), 'mode': mode, 'job': info}
            raise DetachError('could not start a detached process'
                              + (f' (WMI start failed: {wmi_error})' if wmi_error else ''))
        p = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out,
                             stderr=subprocess.STDOUT, start_new_session=True, close_fds=True)
        return {'pid': p.pid, 'created': process_created(p.pid), 'mode': 'posix', 'job': info}
    finally:
        if log_path:
            out.close()


def spawn_detached(argv: list[str], cwd: str | None = None, env: dict | None = None,
                   log_path: str | None = None) -> int:
    """Best-effort detached start (the hook's meter refresh); spawn_detached_ex has the guarantees."""
    return spawn_detached_ex(argv, cwd, env, log_path)['pid']


def run(argv: list[str], cwd: str | None = None, timeout: float = 30, env: dict | None = None,
        input_text: str | None = None) -> tuple[int, str, str]:
    """Run a short command; returns (rc, stdout, stderr). rc 124 on timeout, 127 if missing."""
    try:
        p = subprocess.run(argv, cwd=cwd, env=env, input=input_text, capture_output=True, text=True,
                           encoding='utf-8', errors='replace', timeout=timeout,
                           creationflags=no_window_flags())
        return p.returncode, p.stdout or '', p.stderr or ''
    except subprocess.TimeoutExpired as e:
        return 124, (e.stdout or '') if isinstance(e.stdout, str) else '', 'timeout'
    except FileNotFoundError as e:
        return 127, '', str(e)


# ---------------------------------------------------------------- git

def git_env() -> dict:
    """Probes run in repos peers are working in: never take the optional index lock (git status
    would otherwise rewrite .git/index to refresh stat data)."""
    return dict(os.environ, GIT_OPTIONAL_LOCKS='0')


def git(cwd: str, *args: str, timeout: float = 20) -> tuple[int, str]:
    rc, out, err = run(['git', '-C', cwd, *args], timeout=timeout, env=git_env())
    # rstrip only: porcelain output starts with a meaningful space (' M path').
    return rc, (out if rc == 0 else err).rstrip()


def git_state(cwd: str) -> dict:
    """Mechanical repo state for receipts and batons. Empty dict when cwd is not in a repo."""
    rc, top = git(cwd, 'rev-parse', '--show-toplevel')
    if rc != 0:
        return {}
    st = {'root': top}
    _, st['branch'] = git(cwd, 'rev-parse', '--abbrev-ref', 'HEAD')
    _, st['head'] = git(cwd, 'rev-parse', 'HEAD')
    rc1, gd = git(cwd, 'rev-parse', '--absolute-git-dir')
    rc2, cd = git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    st['linked_worktree'] = bool(rc1 == 0 and rc2 == 0 and os.path.normcase(os.path.normpath(gd))
                                 != os.path.normcase(os.path.normpath(cd)))
    rc, up = git(cwd, 'rev-parse', '--abbrev-ref', '--symbolic-full-name', '@{u}')
    st['upstream'] = up if rc == 0 else None
    rc, porcelain = git(cwd, 'status', '--porcelain=v1', '--untracked-files=all')
    st['dirty'] = porcelain.splitlines()[:200] if rc == 0 else []
    rc, log = git(cwd, 'log', '--oneline', '-5')
    st['recent'] = log.splitlines() if rc == 0 else []
    return st


def is_linked_worktree(cwd: str) -> bool:
    return bool(git_state(cwd).get('linked_worktree'))


def leftover_dir_problem(cwd: str) -> str | None:
    """Why an existing directory is a leftover (removed-worktree) folder rather than a place to work,
    or None. Only two shapes count: nothing in it at all, or a `.git` file whose gitdir is gone. A
    plain non-repo folder is fine (black-box handoffs start from those), including one holding an
    ordinary file named `.git` that is not a gitdir pointer."""
    try:
        names = os.listdir(cwd)
    except OSError as e:
        return f'cannot be read ({e.strerror or e})'
    if not names:
        return 'is empty (a leftover worktree folder?)'
    marker = os.path.join(cwd, '.git')
    if not os.path.isfile(marker):
        return None
    try:
        with open(marker, encoding='utf-8', errors='replace') as h:
            first = h.readline().strip()
    except OSError as e:
        return f'has an unreadable .git file ({e.strerror or e})'
    if not first.lower().startswith('gitdir:'):
        return None  # an ordinary file named .git, not a worktree pointer: a plain folder
    target = first[len('gitdir:'):].strip()
    if not os.path.isdir(os.path.join(cwd, target)):  # a relative pointer resolves against cwd
        return f'has a .git file pointing at a missing gitdir ({target}); the worktree was removed'
    return None


def emit(obj, as_json: bool, human: str | None = None) -> None:
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2))
    elif human is not None:
        print(human)
