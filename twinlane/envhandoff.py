"""Env hand-off for a process started through WMI (common.spawn_detached_ex mode 'wmi').

Win32_Process.Create cannot pass the caller's environment or redirect stdio: the new process gets
the user's default environment and no log. The launcher writes the env, working directory and log
path to a JSON file in the job folder and appends `--env-handoff <file>` to the child's argv; the
child loads it first thing (cli.py, before any twinlane module reads os.environ at import).

Who gets the hand-off is decided by ONE arbiter file next to it, `runner.decided`, created with
O_CREAT|O_EXCL by whichever side decides first (the only single-winner primitive here: a rename
racing a delete can report success on both sides on Windows):
- the child creates it as `child <pid> <created>` BEFORE reading anything; if it already exists the
  launcher has decided, and the child exits 5 without reading the hand-off;
- the launcher, when it abandons the start (timeout, error, no claim in time), creates it as
  `launcher <time>`; if that succeeds no runner can ever start and it deletes the hand-off; if it
  already exists a child won, and that child (pid + creation stamp from the file) is the runner.
The file holds only what the caller's environment already held, gets an owner-only DACL (no
inherited ACEs) before any content is written, and is never printed. A launcher killed after
writing it leaves it on disk until the next `twinlane run`/`twinlane jobs` sweeps it (older than
SWEEP_AGE_S); there is no deletion deadline, only the owner-only DACL.

Standard library only, and nothing here reads the environment at import.
"""
from __future__ import annotations

import glob
import json
import os
import sys
import time

ARG = '--env-handoff'
DECIDED = 'runner.decided'
SWEEP_AGE_S = 120


class Lost(OSError):
    """The launcher decided first: this child must not read the hand-off or run."""


def decided_path(handoff: str) -> str:
    return os.path.join(os.path.dirname(os.path.abspath(handoff)), DECIDED)


def write(path: str, env, log_path: str | None = None, cwd: str | None = None) -> None:
    """Create the hand-off file (replacing a stale one and a stale decision): empty, then owner-only,
    then the content. Raises OSError (and leaves no file) when the permissions cannot be set."""
    for p in (path, decided_path(path)):
        try:
            os.remove(p)
        except FileNotFoundError:
            pass
    data = json.dumps({'v': 1, 'env': {str(k): str(v) for k, v in dict(env).items()},
                       'log_path': log_path, 'cwd': cwd})
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0), 0o600)
    try:
        if os.name == 'nt':
            owner_only(path)
        with os.fdopen(fd, 'w', encoding='utf-8') as h:
            fd = None
            h.write(data)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.remove(path)
        except OSError:
            pass
        raise


def split(argv: list[str]) -> tuple[list[str], str | None]:
    """(argv without the hand-off flag, hand-off path or None). The flag is always the last two args."""
    if len(argv) >= 2 and argv[-2] == ARG:
        return list(argv[:-2]), argv[-1]
    return list(argv), None


def decide(handoff: str, who: str) -> bool:
    """Create the arbiter with `who` as content. True: this side decided. False: the other side had."""
    try:
        fd = os.open(decided_path(handoff), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_BINARY', 0),
                     0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, 'w', encoding='ascii') as h:
        h.write(who)
    return True


def read_decision(handoff: str, wait_s: float = 2.0) -> list[str]:
    """The arbiter's words (['child', pid, created] or ['launcher', ...]); waits briefly for a
    winner that created the file but has not written it yet. [] when unreadable."""
    deadline = time.time() + wait_s
    while True:
        try:
            with open(decided_path(handoff), encoding='ascii') as h:
                words = h.read().split()
        except OSError:
            words = []
        if words or time.time() >= deadline:
            return words
        time.sleep(0.02)


def claim_and_read(path: str) -> dict:
    """The child's side: decide first, then read and delete the hand-off. Raises Lost (without
    opening the hand-off) when the launcher decided first."""
    created = _self_created()
    if created is None:
        raise Lost('the runner creation stamp is unavailable; not reading the env hand-off')
    if not decide(path, f'child {os.getpid()} {created}'):
        raise Lost('the launcher abandoned this start; not reading the env hand-off')
    return _read(path)


def _read(path: str) -> dict:
    try:
        with open(path, encoding='utf-8') as h:
            return json.load(h)
    finally:
        try:
            os.remove(path)
        except OSError:
            pass


def load(path: str) -> dict:
    """claim_and_read, then apply: replace os.environ, put its PYTHONPATH on sys.path (twinlane itself is
    stdlib only; this keeps parity with a normal start), chdir, and point stdin at NUL and
    stdout/stderr at the log. Raises OSError (Lost included) / ValueError when unusable."""
    data = claim_and_read(path)
    env = data.get('env') if isinstance(data, dict) else None
    if not isinstance(env, dict) or not env:
        raise ValueError('env hand-off file has no environment')
    os.environ.clear()
    os.environ.update({str(k): str(v) for k, v in env.items()})
    extra = [p for p in os.environ.get('PYTHONPATH', '').split(os.pathsep) if p and p not in sys.path]
    sys.path[1:1] = extra  # where the interpreter puts PYTHONPATH: after the script's directory
    if data.get('cwd'):
        os.chdir(data['cwd'])
    redirect_stdio(data.get('log_path'))
    return data


def sweep(jobs_dir: str, name: str, max_age_s: float = SWEEP_AGE_S) -> int:
    """Delete hand-off files older than max_age_s under jobs_dir/*/; returns the count. The launcher
    deletes its own on return, so only a killed launcher or a crashed child leaves one."""
    n, cutoff = 0, time.time() - max_age_s
    for p in glob.glob(os.path.join(glob.escape(jobs_dir), '*', name)):
        try:
            if os.path.getmtime(p) < cutoff:
                os.remove(p)
                n += 1
        except OSError:
            pass
    return n


def _self_created() -> int | None:
    """This process's creation stamp, in common.process_created's format (common is not importable
    yet: it reads the environment at import)."""
    try:
        if os.name == 'nt':
            import ctypes
            from ctypes import wintypes
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.GetCurrentProcess.restype = wintypes.HANDLE
            k32.GetProcessTimes.argtypes = (wintypes.HANDLE,) + (ctypes.POINTER(ctypes.c_uint64),) * 4
            t = [ctypes.c_uint64() for _ in range(4)]
            if not k32.GetProcessTimes(k32.GetCurrentProcess(), *(ctypes.byref(x) for x in t)):
                return None
            return int(t[0].value)
        with open(f'/proc/{os.getpid()}/stat', encoding='ascii', errors='replace') as h:
            return int(h.read().rsplit(')', 1)[1].split()[19])
    except Exception:
        return None


def redirect_stdio(log_path: str | None) -> None:
    """What Popen(stdin=DEVNULL, stdout=log, stderr=STDOUT) gives a child, done from inside it,
    including the Win32 std handles that this process's own children inherit."""
    for s in (sys.stdout, sys.stderr):
        try:
            s.flush()
        except Exception:
            pass
    binary = getattr(os, 'O_BINARY', 0)
    null = os.open(os.devnull, os.O_RDONLY | binary)
    out = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND | binary, 0o600) if log_path \
        else os.open(os.devnull, os.O_WRONLY | binary)
    for src, dst in ((null, 0), (out, 1), (out, 2)):
        os.dup2(src, dst)
    os.close(null)
    os.close(out)
    sys.stdin = open(0, 'r', encoding='utf-8', closefd=False)
    sys.stdout = open(1, 'w', encoding='utf-8', errors='replace', buffering=1, closefd=False)
    sys.stderr = open(2, 'w', encoding='utf-8', errors='replace', buffering=1, closefd=False)
    if os.name == 'nt':
        try:
            import ctypes
            import msvcrt
            k32 = ctypes.WinDLL('kernel32', use_last_error=True)
            k32.SetStdHandle.argtypes = (ctypes.c_uint32, ctypes.c_void_p)
            for fd, std in ((0, -10), (1, -11), (2, -12)):
                k32.SetStdHandle(ctypes.c_uint32(std & 0xFFFFFFFF), msvcrt.get_osfhandle(fd))
        except Exception:
            pass


# ---------------------------------------------------------------- Windows owner-only DACL

def _advapi():
    import ctypes
    from ctypes import wintypes
    adv = ctypes.WinDLL('advapi32', use_last_error=True)
    k32 = ctypes.WinDLL('kernel32', use_last_error=True)
    k32.GetCurrentProcess.restype = wintypes.HANDLE
    k32.CloseHandle.argtypes = (wintypes.HANDLE,)
    k32.LocalFree.argtypes = (ctypes.c_void_p,)
    adv.OpenProcessToken.argtypes = (wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE))
    adv.GetTokenInformation.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
                                        ctypes.POINTER(wintypes.DWORD))
    adv.ConvertSidToStringSidW.argtypes = (ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p))
    adv.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    adv.SetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p)
    adv.GetFileSecurityW.argtypes = (wintypes.LPCWSTR, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD,
                                     ctypes.POINTER(wintypes.DWORD))
    adv.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = (
        ctypes.c_void_p, wintypes.DWORD, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p), ctypes.c_void_p)
    return ctypes, wintypes, adv, k32


def _check(ok, ctypes):
    if not ok:
        raise ctypes.WinError(ctypes.get_last_error())


def user_sid() -> str:
    """The current process token's user SID as a string (S-1-5-21-...)."""
    ctypes, wintypes, adv, k32 = _advapi()
    tok = wintypes.HANDLE()
    _check(adv.OpenProcessToken(k32.GetCurrentProcess(), 0x8, ctypes.byref(tok)), ctypes)  # TOKEN_QUERY
    try:
        n = wintypes.DWORD()
        adv.GetTokenInformation(tok, 1, None, 0, ctypes.byref(n))  # TokenUser; sizes the buffer
        buf = ctypes.create_string_buffer(n.value)
        _check(adv.GetTokenInformation(tok, 1, buf, n, ctypes.byref(n)), ctypes)
        psid = ctypes.cast(buf, ctypes.POINTER(ctypes.c_void_p))[0]  # TOKEN_USER.User.Sid
        s = ctypes.c_void_p()
        _check(adv.ConvertSidToStringSidW(psid, ctypes.byref(s)), ctypes)
        try:
            return ctypes.wstring_at(s.value)
        finally:
            k32.LocalFree(s)
    finally:
        k32.CloseHandle(tok)


def owner_only(path: str) -> None:
    """Replace the file's DACL with one explicit ACE (current user, full control), protected so no
    ACE is inherited from the job folder."""
    ctypes, wintypes, adv, k32 = _advapi()
    sd = ctypes.c_void_p()
    _check(adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(f'D:P(A;;FA;;;{user_sid()})', 1,
                                                                    ctypes.byref(sd), None), ctypes)
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        _check(adv.SetFileSecurityW(path, 0x4 | 0x80000000, sd), ctypes)
    finally:
        k32.LocalFree(sd)


def dacl_sddl(path: str) -> str:
    """The file's DACL as SDDL (for checks and tests)."""
    ctypes, wintypes, adv, k32 = _advapi()
    n = wintypes.DWORD()
    adv.GetFileSecurityW(path, 0x4, None, 0, ctypes.byref(n))
    buf = ctypes.create_string_buffer(n.value)
    _check(adv.GetFileSecurityW(path, 0x4, buf, n, ctypes.byref(n)), ctypes)
    s = ctypes.c_void_p()
    _check(adv.ConvertSecurityDescriptorToStringSecurityDescriptorW(buf, 1, 0x4, ctypes.byref(s), None), ctypes)
    try:
        return ctypes.wstring_at(s.value)
    finally:
        k32.LocalFree(s)
