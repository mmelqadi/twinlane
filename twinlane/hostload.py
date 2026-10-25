"""Host-load admission: measure how busy this machine is and lower the usable slot count as it rises.

A job is never refused for load. It waits in the queue with a reason ("host busy: cpu 97%") and starts when
the load falls, because `effective_parallel()` is recomputed on every slot poll. At least `min_parallel`
slots stay open, so an idle engine always makes progress and a busy host cannot deadlock the queue.

Measuring is advisory and cheap: kernel counters only (no WMI, no subprocess). Any reading that cannot be
taken is None, and None never throttles: an unreadable sensor must not stop work. Under test isolation
nothing is measured unless a test installs a sampler, so a loaded CI machine cannot change a test.

Thresholds live in the user config under "hostload" (all optional):
  enabled            true
  cpu_soft/cpu_hard  80 / 98   percent busy: slots shrink linearly between the two
  ram_free_soft_mb   2048      free physical RAM: slots shrink below this ...
  ram_free_hard_mb   512       ... down to min_parallel at this
  procs_soft/hard    16 / 32   live codex / sandbox processes
  min_parallel       2         slots that stay open per engine at full load
  sample_ttl_s       3         one sample is shared by every runner for this long
"""
from __future__ import annotations

import math
import os
import subprocess
import sys
import time

from . import common

DEFAULTS = {'enabled': True, 'cpu_soft': 80, 'cpu_hard': 98, 'ram_free_soft_mb': 2048, 'ram_free_hard_mb': 512,
            'procs_soft': 16, 'procs_hard': 32, 'min_parallel': 2, 'sample_ttl_s': 3}
# Image names that count as engine load: the Codex CLI and the sandbox helpers it spawns.
ENGINE_PROCESS_NAMES = ('codex.exe', 'codex', 'codex-command-runner.exe', 'codex-windows-sandbox-setup.exe')

_SAMPLER = None  # tests install a callable returning a sample dict


def set_sampler(fn) -> None:
    """Install (or clear, with None) a replacement for the real measurement. Tests only."""
    global _SAMPLER
    _SAMPLER = fn


def settings(policy: dict | None) -> dict:
    out = dict(DEFAULTS)
    raw = (policy or {}).get('hostload')
    if isinstance(raw, dict):
        for key, default in DEFAULTS.items():
            value = raw.get(key)
            if key == 'enabled':
                if type(value) is bool:
                    out[key] = value
            elif type(value) in (int, float) and math.isfinite(value) and value >= 0:
                out[key] = value
    if out['cpu_hard'] <= out['cpu_soft']:
        out['cpu_hard'] = out['cpu_soft'] + 1
    if out['ram_free_soft_mb'] < 1:  # the span below would be zero or negative: both limits fall back together
        out['ram_free_soft_mb'], out['ram_free_hard_mb'] = DEFAULTS['ram_free_soft_mb'], DEFAULTS['ram_free_hard_mb']
    if out['ram_free_hard_mb'] >= out['ram_free_soft_mb']:
        out['ram_free_hard_mb'] = max(0, out['ram_free_soft_mb'] - 1)
    if out['procs_hard'] <= out['procs_soft']:
        out['procs_hard'] = out['procs_soft'] + 1
    out['min_parallel'] = max(1, int(out['min_parallel']))
    return out


# ---------------------------------------------------------------- measuring

def _win_times():
    import ctypes
    from ctypes import wintypes

    class FT(ctypes.Structure):
        _fields_ = [('lo', wintypes.DWORD), ('hi', wintypes.DWORD)]

    idle, kernel, user = FT(), FT(), FT()
    if not ctypes.windll.kernel32.GetSystemTimes(ctypes.byref(idle), ctypes.byref(kernel), ctypes.byref(user)):
        return None
    idle, kernel, user = ((f.hi << 32) | f.lo for f in (idle, kernel, user))
    return idle, kernel + user  # kernel time includes idle time


def _win_free_mb():
    import ctypes

    class MEM(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong), ('total', ctypes.c_ulonglong),
                    ('avail', ctypes.c_ulonglong), ('tpf', ctypes.c_ulonglong), ('apf', ctypes.c_ulonglong),
                    ('tv', ctypes.c_ulonglong), ('av', ctypes.c_ulonglong), ('aex', ctypes.c_ulonglong)]

    m = MEM()
    m.length = ctypes.sizeof(MEM)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m)):
        return None
    return int(m.avail // (1024 * 1024))


def _win_engine_procs():
    """Count live engine processes from a Toolhelp snapshot (no WMI, no subprocess)."""
    import ctypes
    from ctypes import wintypes

    class PE(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('usage', wintypes.DWORD), ('pid', wintypes.DWORD),
                    ('heap', ctypes.c_void_p), ('module', wintypes.DWORD), ('threads', wintypes.DWORD),
                    ('parent', wintypes.DWORD), ('pri', ctypes.c_long), ('flags', wintypes.DWORD),
                    ('exe', ctypes.c_wchar * 260)]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
    snap = k32.CreateToolhelp32Snapshot(0x2, 0)
    if snap in (None, ctypes.c_void_p(-1).value):
        return None
    names = {n.lower() for n in ENGINE_PROCESS_NAMES}
    count = 0
    try:
        entry = PE()
        entry.size = ctypes.sizeof(PE)
        ok = k32.Process32FirstW(ctypes.c_void_p(snap), ctypes.byref(entry))
        while ok:
            if entry.exe.lower() in names:
                count += 1
            ok = k32.Process32NextW(ctypes.c_void_p(snap), ctypes.byref(entry))
    finally:
        k32.CloseHandle(ctypes.c_void_p(snap))
    return count


def _posix_times():
    with open('/proc/stat', encoding='ascii') as h:
        parts = [int(x) for x in h.readline().split()[1:]]
    return parts[3] + (parts[4] if len(parts) > 4 else 0), sum(parts)


def _posix_free_mb():
    with open('/proc/meminfo', encoding='ascii') as h:
        for line in h:
            if line.startswith('MemAvailable:'):
                return int(line.split()[1]) // 1024
    return None


def _posix_engine_procs():
    names = {n.lower() for n in ENGINE_PROCESS_NAMES}
    count = 0
    for entry in os.listdir('/proc'):
        if entry.isdigit():
            try:
                with open(f'/proc/{entry}/comm', encoding='utf-8', errors='replace') as h:
                    if h.read().strip().lower() in names:
                        count += 1
            except OSError:
                continue
    return count


def _run(argv) -> str | None:
    try:
        done = subprocess.run(argv, capture_output=True, text=True, timeout=3, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    return done.stdout if done.returncode == 0 else None


def _loadavg_pct() -> float | None:
    """CPU pressure from the 1-minute load average per core, for hosts with no /proc/stat (macOS)."""
    try:
        return round(max(0.0, min(100.0, 100.0 * os.getloadavg()[0] / (os.cpu_count() or 1))), 1)
    except (OSError, AttributeError, ValueError):
        return None


def _vm_stat_free_mb() -> int | None:
    """Free + inactive + speculative pages from `vm_stat` (macOS has no /proc/meminfo)."""
    text = _run(['vm_stat'])
    if not text:
        return None
    size, pages = 4096, 0
    for line in text.splitlines():
        if 'page size of' in line:
            digits = ''.join(c if c.isdigit() else ' ' for c in line.split('page size of', 1)[1]).split()
            size = int(digits[0]) if digits else size
        elif line.split(':')[0] in ('Pages free', 'Pages inactive', 'Pages speculative'):
            pages += int(line.split(':')[1].strip().rstrip('.'))
    return int(pages * size // (1024 * 1024)) if pages else None


def _ps_engine_procs() -> int | None:
    text = _run(['ps', '-A', '-o', 'comm='])
    if text is None:
        return None
    names = {n.lower() for n in ENGINE_PROCESS_NAMES}
    return sum(1 for line in text.splitlines() if os.path.basename(line.strip()).lower() in names)


def measure() -> dict:
    """One real reading. Every field is None when it could not be read."""
    out = {'cpu_pct': None, 'ram_free_mb': None, 'engine_procs': None, 'heap': None}
    try:
        times = _win_times if common.IS_WIN else _posix_times
        first = times()
        time.sleep(0.25)
        second = times()
        if first and second:
            idle, total = second[0] - first[0], second[1] - first[1]
            if total > 0:
                out['cpu_pct'] = round(max(0.0, min(100.0, 100.0 * (total - idle) / total)), 1)
    except (OSError, ValueError, AttributeError, ImportError, IndexError):
        pass
    if out['cpu_pct'] is None and not common.IS_WIN:
        out['cpu_pct'] = _loadavg_pct()
    try:
        out['ram_free_mb'] = _win_free_mb() if common.IS_WIN else _posix_free_mb()
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    if out['ram_free_mb'] is None and sys.platform == 'darwin':
        try:
            out['ram_free_mb'] = _vm_stat_free_mb()
        except (ValueError, IndexError):
            pass
    try:
        out['engine_procs'] = _win_engine_procs() if common.IS_WIN else _posix_engine_procs()
    except (OSError, ValueError, AttributeError, ImportError):
        pass
    if out['engine_procs'] is None and not common.IS_WIN:
        out['engine_procs'] = _ps_engine_procs()
    # Desktop-heap exhaustion has no cheap counter; its symptom (0xc0000142 on a new console process)
    # shows up as a job whose shell never answers, which the stall watchdog handles.
    return out


def _cache_path() -> str:
    return os.path.join(common.STATE, 'hostload.json')


def sample(policy: dict | None = None, now: float | None = None) -> dict | None:
    """The shared host reading, at most `sample_ttl_s` old; None when disabled or nothing could be read."""
    cfg = settings(policy)
    if not cfg['enabled']:
        return None
    if _SAMPLER is not None:
        return _SAMPLER()
    if os.environ.get('TWINLANE_TEST_ISOLATION'):
        return None
    now = time.time() if now is None else now
    cached = common.read_json(_cache_path(), None)
    if isinstance(cached, dict) and type(cached.get('at')) in (int, float) \
            and 0 <= now - cached['at'] <= cfg['sample_ttl_s']:
        return cached
    reading = measure()
    reading['at'] = now
    try:
        common.write_json(_cache_path(), reading)
    except OSError:
        pass
    return reading


# ---------------------------------------------------------------- judging

def pressure(reading: dict | None, cfg: dict) -> tuple[float, str | None]:
    """(0..1, why). 0 means no pressure; 1 means every metric at its hard limit. `why` names the worst one."""
    if not isinstance(reading, dict):
        return 0.0, None
    worst, why = 0.0, None

    def consider(p, text):
        nonlocal worst, why
        if p > worst:
            worst, why = p, text

    cpu = reading.get('cpu_pct')
    if type(cpu) in (int, float):
        consider((cpu - cfg['cpu_soft']) / (cfg['cpu_hard'] - cfg['cpu_soft']), f'cpu {cpu:.0f}%')
    free = reading.get('ram_free_mb')
    if type(free) in (int, float):
        consider((cfg['ram_free_soft_mb'] - free) / (cfg['ram_free_soft_mb'] - cfg['ram_free_hard_mb']),
                 f'free RAM {free:.0f} MB')
    procs = reading.get('engine_procs')
    if type(procs) in (int, float):
        consider((procs - cfg['procs_soft']) / (cfg['procs_hard'] - cfg['procs_soft']),
                 f'{int(procs)} codex/sandbox processes')
    return min(1.0, worst), why


def effective_parallel(configured: int, reading: dict | None, policy: dict | None = None) -> tuple[int, str | None]:
    """(slots usable now, why fewer than configured). Never below min_parallel, never above `configured`."""
    cfg = settings(policy)
    configured = max(1, int(configured))
    if not cfg['enabled']:
        return configured, None
    p, why = pressure(reading, cfg)
    if p <= 0:
        return configured, None
    usable = min(configured, max(cfg['min_parallel'], math.floor(configured * (1 - p) + 1e-9)))
    if usable >= configured:
        return configured, None
    return usable, f'host busy: {why}'


def line(reading: dict | None, configured: int, policy: dict | None = None) -> str:
    """One board line: the reading, and the slot count it allows."""
    cfg = settings(policy)
    if not cfg['enabled']:
        return 'host load: admission off (hostload.enabled=false)'
    if not isinstance(reading, dict):
        return 'host load: unknown (no reading; slots not reduced)'
    bits = []
    if type(reading.get('cpu_pct')) in (int, float):
        bits.append(f"cpu {reading['cpu_pct']:.0f}%")
    if type(reading.get('ram_free_mb')) in (int, float):
        bits.append(f"free RAM {reading['ram_free_mb'] / 1024:.1f} GB")
    if type(reading.get('engine_procs')) in (int, float):
        bits.append(f"{int(reading['engine_procs'])} codex/sandbox procs")
    usable, why = effective_parallel(configured, reading, policy)
    tail = f'slots {usable}/{configured}' + (f' ({why})' if why else '')
    return 'host load: ' + (' · '.join(bits) or 'no counters readable') + ' · ' + tail
