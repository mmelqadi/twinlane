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
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from contextlib import contextmanager

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


def _atomic_write(path: str, write, deadline: float | None = None) -> None:
    """Temp file in the same directory, then os.replace (retried while a reader holds the target).

    With `deadline` (a time.monotonic() value) the retries are bounded by it: no replace is attempted and no
    sleep runs past it, TimeoutError is raised, the temp file is removed and `path` keeps its old content
    (the replace is atomic, so `path` is only ever the old file or the complete new one)."""
    d = os.path.dirname(os.path.abspath(path))
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


def write_json(path: str, data, deadline: float | None = None) -> None:
    _atomic_write(path, lambda h: json.dump(data, h, ensure_ascii=False, indent=2), deadline)


def write_text(path: str, text: str) -> None:
    _atomic_write(path, lambda h: h.write(text))


@contextmanager
def file_lock(path: str, timeout_s: float = 120, poll_s: float = 0.05):
    """Exclusive OS lock on one byte of `path` (LockFileEx / flock).

    The OS drops the lock when its holder dies, so there is no staleness guess and no
    check-then-remove race. The lock file itself stays in place."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        deadline = time.monotonic() + timeout_s
        while not _try_lock_fd(fd):
            if time.monotonic() > deadline:
                raise TimeoutError(f'lock busy: {path}')
            time.sleep(poll_s)
        try:
            yield
        finally:
            _unlock_fd(fd)
    finally:
        os.close(fd)


def _try_lock_fd(fd: int) -> bool:
    try:
        if IS_WIN:
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
        if IS_WIN:
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
    if not os.path.isfile(config):
        return p
    user = read_json(config)
    if not isinstance(user, dict):
        raise SystemExit(f'twinlane: invalid config {config}')
    return _merge_config(p, user)


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


def _app_owned(path: str) -> bool:
    p = _norm(path)
    return any(p.startswith(_norm(d) + os.sep) for d in _app_bin_dirs())


def _pick_codex_source() -> tuple[str | None, str, str | None]:
    """(path, origin, version): TWINLANE_CODEX_BIN as set ('env'); else the best complete app build ('app'); else
    (None, 'none', None). An explicit binary inside the app's own folders is an app source like any other: it is
    copied, never run in place. A build without every helper (the app mid-extract) is never a source;
    complete builds rank by version, then mtime, so an older build rewritten later does not beat a newer one."""
    env = os.environ.get('TWINLANE_CODEX_BIN')
    if not IS_WIN:
        return (env, 'env', None) if env and os.path.isfile(env) else (None, 'none', None)
    if env and os.path.isfile(env) and not _app_owned(env):
        return env, 'env', None
    found = [env] if env and os.path.isfile(env) else (
        glob.glob(os.path.join(_app_bin_dirs()[0], '*', 'codex.exe')) + [os.path.join(_app_bin_dirs()[1], 'codex.exe')])
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
            return _private_codex_copy(src, version), ''
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
    """A confirmed holder lives until exit. Unconfirmed identity expires after the policy bound."""
    if IS_WIN:
        alive, actual = _win_process_info(pid)
    else:
        alive = pid_alive(pid)
        actual = _posix_start_time(pid) if alive else None
    if not alive:
        return False
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


def strip_secret_env(env: dict) -> tuple[dict, list[str]]:
    """(env without secret-named variables, the stripped NAMES sorted). Values are never returned."""
    names = sorted(k for k in env if SECRET_ENV_NAME.search(k) and k.upper() not in SECRET_ENV_KEEP)
    return {k: v for k, v in env.items() if k not in names}, names


# ---------------------------------------------------------------- processes

# Windows process-creation and job-object constants.
DETACHED_PROCESS, CREATE_NEW_PROCESS_GROUP, CREATE_NO_WINDOW = 0x8, 0x200, 0x08000000
CREATE_BREAKAWAY_FROM_JOB = 0x01000000
JOB_LIMIT_BREAKAWAY_OK, JOB_LIMIT_SILENT_BREAKAWAY_OK, JOB_LIMIT_KILL_ON_JOB_CLOSE = 0x800, 0x1000, 0x2000
_PROCESS_QUERY_LIMITED = 0x1000
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
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CreateJobObjectW.restype = wintypes.HANDLE
    k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
    k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
    k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
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


def _win_process_info(pid: int) -> tuple[bool, int | None]:
    """(exists, creation FILETIME) for a pid; creation is None when the process cannot be opened."""
    import ctypes
    from ctypes import wintypes
    k32 = _k32()
    h = k32.OpenProcess(_PROCESS_QUERY_LIMITED, False, pid)
    if not h:
        return ctypes.get_last_error() == 5, None  # access denied => it exists, identity unknown
    try:
        code = wintypes.DWORD()
        if not k32.GetExitCodeProcess(h, ctypes.byref(code)) or code.value != 259:  # STILL_ACTIVE
            return False, None
        t = [ctypes.c_uint64() for _ in range(4)]
        if not k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return True, None
        return True, int(t[0].value)
    finally:
        k32.CloseHandle(h)


def _posix_start_time(pid: int) -> int | None:
    try:
        with open(f'/proc/{pid}/stat', encoding='ascii', errors='replace') as h:
            return int(h.read().rsplit(')', 1)[1].split()[19])
    except (OSError, IndexError, ValueError):
        pass
    if sys.platform != 'darwin':
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


def pid_state(pid, created=None) -> str:
    """ALIVE, DEAD or UNKNOWN for a pid. With `created`, ALIVE means it is that same process (not a reused
    pid) and a readable creation time that differs is DEAD. UNKNOWN is a process that exists but whose
    creation time cannot be read (a restricted-token sandbox cannot open its host's runners): it is neither
    provably ours nor provably gone, so callers must not call it dead and must not kill it. NEVER uses
    os.kill on Windows (signal 0 would TerminateProcess)."""
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
        actual = _posix_start_time(pid) if alive else None
    if not alive:
        return DEAD
    if created in (None, ''):
        return ALIVE
    if actual is None:
        return UNKNOWN
    try:
        return ALIVE if int(created) == actual else DEAD
    except (TypeError, ValueError):
        return DEAD


def pid_alive(pid, created=None) -> bool:
    """True only when the process is PROVABLY alive (and, with `created`, is that same process). An unreadable
    identity is False here: use pid_maybe_alive when "could still be running" is the safe answer."""
    return pid_state(pid, created) == ALIVE


def pid_maybe_alive(pid, created=None) -> bool:
    """False only when the process is provably gone or provably another process; an unreadable identity
    counts as possibly alive. For slot counts, continuation guards and kill-failure checks."""
    return pid_state(pid, created) != DEAD


def kill_tree(pid, created=None) -> bool:
    """Kill a process tree. With `created`, refuses unless the pid is still that same process.
    True only when the kill command reported success."""
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if created not in (None, '') and not pid_alive(pid, created):
        return False  # a reused pid, or an identity that cannot be read: never kill what is not provably it
    if IS_WIN:
        r = subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                           creationflags=no_window_flags())
        if r.returncode == 0:
            return True
        try:
            os.kill(pid, 9)
            return True
        except OSError:
            return False
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
