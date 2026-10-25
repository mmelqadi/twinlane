"""Run unittest modules in parallel and triage failures in serial (stdlib only)."""

import argparse
import concurrent.futures
import json
import math
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time


def read_results(path, module):
    """Read only the driver's dedicated JSONL channel, never diagnostic output."""
    rows = []
    loaded = None
    finished = None
    protocol_error = None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            record = json.loads(line)
            event = record["event"]
            if event == "loaded":
                if loaded is not None:
                    raise ValueError("duplicate loader record")
                loaded = record["tests"]
                rows = [{"test": name, "status": "unresolved", "classification": "real",
                         "import_error": name.startswith("unittest.loader._FailedTest.")}
                        for name in loaded]
            elif event == "outcome":
                row = next((row for row in rows if row["test"] == record["test"]
                            and row["status"] == "unresolved"), None)
                if row is None or record["status"] not in {
                        "ok", "fail", "error", "skipped", "expected_failure", "unexpected_success"}:
                    raise ValueError("unknown or duplicate test outcome")
                row["status"] = record["status"]
                row["classification"] = ("real" if row["status"] in
                                         ("fail", "error", "unexpected_success") else None)
            elif event == "fixture_error":
                rows.append({"test": record["test"], "status": "error",
                             "classification": "real", "import_error": True})
            elif event == "finished":
                finished = record
            else:
                raise ValueError("unknown result record")
    except (OSError, ValueError, KeyError, TypeError) as error:
        protocol_error = str(error)
    for row in rows:
        if row["import_error"] and row["test"].startswith("unittest.loader._FailedTest."):
            row["test"] = module
    return {
        "tests": rows,
        "tests_ran": finished["tests_ran"] if finished else sum(
            row["status"] != "unresolved" for row in rows),
        "tests_loaded": len(loaded) if loaded is not None else None,
        "unittest_seconds": finished["seconds"] if finished else None,
        "unittest_result": ("OK" if finished["success"] else "FAILED") if finished else None,
        "unittest_details": None, "protocol_error": protocol_error,
    }


class _WindowsJob:
    """Retain a tree handle when a restricted Windows token cannot use taskkill."""

    def __init__(self, process):
        import ctypes
        from ctypes import wintypes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        self.kernel.CreateJobObjectW.restype = wintypes.HANDLE
        self.kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self.kernel.AssignProcessToJobObject.restype = wintypes.BOOL
        self.kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self.kernel.TerminateJobObject.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.handle = self.kernel.CreateJobObjectW(None, None)
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        # Popen retains the handle even when OpenProcess is denied by the host.
        if not self.kernel.AssignProcessToJobObject(self.handle, int(process._handle)):
            error = ctypes.WinError(ctypes.get_last_error())
            self.close()
            raise error

    def active_processes(self):
        import ctypes
        from ctypes import wintypes

        class Accounting(ctypes.Structure):
            _fields_ = [(name, ctypes.c_longlong) for name in
                        ("user", "kernel", "period_user", "period_kernel")] + [
                        (name, wintypes.DWORD) for name in
                        ("faults", "total", "active", "terminated")]

        info = Accounting()
        query = self.kernel.QueryInformationJobObject
        query.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p,
                          wintypes.DWORD, ctypes.c_void_p]
        query.restype = wintypes.BOOL
        if not query(self.handle, 1, ctypes.byref(info), ctypes.sizeof(info), None):
            raise ctypes.WinError(ctypes.get_last_error())
        return info.active

    def verify_empty(self):
        deadline = time.monotonic() + 3
        while self.active_processes():
            if time.monotonic() >= deadline:
                raise RuntimeError("descendants survived Job Object termination")
            time.sleep(0.02)

    def terminate(self):
        if not self.kernel.TerminateJobObject(self.handle, 1):
            import ctypes
            raise ctypes.WinError(ctypes.get_last_error())

    def close(self):
        if self.handle:
            self.kernel.CloseHandle(self.handle)
            self.handle = None


def _resume_windows_process(process):
    """Resume the primary thread only after its suspended process is in the job."""
    import ctypes
    from ctypes import wintypes

    class ThreadEntry(ctypes.Structure):
        _fields_ = [("size", wintypes.DWORD), ("usage", wintypes.DWORD),
                    ("tid", wintypes.DWORD), ("owner", wintypes.DWORD),
                    ("base", wintypes.LONG), ("delta", wintypes.LONG),
                    ("flags", wintypes.DWORD)]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel.Thread32First.argtypes = [wintypes.HANDLE, ctypes.POINTER(ThreadEntry)]
    kernel.Thread32Next.argtypes = kernel.Thread32First.argtypes
    kernel.OpenThread.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenThread.restype = wintypes.HANDLE
    kernel.ResumeThread.argtypes = [wintypes.HANDLE]
    kernel.ResumeThread.restype = wintypes.DWORD
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel.CreateToolhelp32Snapshot(4, 0)  # TH32CS_SNAPTHREAD
    if snapshot == wintypes.HANDLE(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    entry = ThreadEntry()
    entry.size = ctypes.sizeof(entry)
    try:
        found = kernel.Thread32First(snapshot, ctypes.byref(entry))
        while found:
            if entry.owner == process.pid:
                thread = kernel.OpenThread(0x0002, False, entry.tid)  # SUSPEND_RESUME
                if not thread:
                    raise ctypes.WinError(ctypes.get_last_error())
                try:
                    if kernel.ResumeThread(thread) == 0xFFFFFFFF:
                        raise ctypes.WinError(ctypes.get_last_error())
                    return
                finally:
                    kernel.CloseHandle(thread)
            found = kernel.Thread32Next(snapshot, ctypes.byref(entry))
        raise RuntimeError("suspended child has no resumable thread")
    finally:
        kernel.CloseHandle(snapshot)


def _process_table():
    """PID -> (parent PID, birth identity, state), including detached sessions."""
    if sys.platform.startswith("linux"):
        table = {}
        for path in Path("/proc").glob("[0-9]*/stat"):
            try:
                fields = path.read_text().rsplit(")", 1)[1].split()
                table[int(path.parent.name)] = (int(fields[1]), fields[19], fields[0])
            except (FileNotFoundError, ProcessLookupError):
                pass
        return table
    result = subprocess.run(["ps", "-axo", "pid=,ppid=,stat=,lstart="],
                            capture_output=True, text=True, timeout=5, check=True)
    table = {}
    for line in result.stdout.splitlines():
        pid, parent, state, birth = line.split(None, 3)
        table[int(pid)] = (int(parent), birth, state)
    return table


def _kill_posix_tree(process):
    # Freeze before walking again so a detached child cannot fork between the
    # final snapshot and the kill. Birth identities protect against PID reuse.
    known = {}
    errors = []
    try:
        table = _process_table()
        if process.pid not in table:
            raise RuntimeError("root exited before descendant ownership could be established")
        known[process.pid] = table[process.pid][1]
        for _ in range(20):
            expanded = True
            while expanded:
                expanded = False
                for pid, (parent, birth, state) in table.items():
                    if (parent in known and pid not in known and parent in table
                            and table[parent][1] == known[parent]):
                        known[pid] = birth
                        expanded = True
            for pid, birth in known.items():
                if pid in table and table[pid][1] == birth and not table[pid][2].startswith("Z"):
                    try:
                        os.kill(pid, signal.SIGSTOP)
                    except (ProcessLookupError, PermissionError):
                        pass  # macOS answers EPERM for a zombie; the stabilization check below is the arbiter
            newer = _process_table()
            unseen = any(parent in known and pid not in known and parent in newer
                         and newer[parent][1] == known[parent]
                         for pid, (parent, _, _) in newer.items())
            stopped = all(pid not in newer or newer[pid][1] != birth
                          or newer[pid][2].startswith(("T", "Z"))
                          for pid, birth in known.items())
            if not unseen and stopped:
                break
            table = newer
            time.sleep(0.01)
        else:
            raise RuntimeError("descendant snapshot did not stabilize")
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    # Always attempt the original group even if the process table is unavailable.
    # Never signal a reused PID just because it appeared in an earlier snapshot.
    try:
        table = _process_table()
        for pid, birth in reversed(list(known.items())):
            if pid in table and table[pid][1] == birth:
                try:
                    os.kill(pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass  # EPERM on a zombie (macOS); a live survivor is reported by the check below
                except OSError as error:
                    errors.append(str(error))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        errors.append(str(error))
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    deadline = time.monotonic() + 3
    while True:
        try:
            table = _process_table()
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            errors.append(str(error))
            break
        survivors = [pid for pid, birth in known.items() if pid in table
                     and table[pid][1] == birth and not table[pid][2].startswith("Z")]
        if not survivors:
            break
        if time.monotonic() >= deadline:
            errors.append("descendants survived timeout cleanup: " + str(survivors))
            break
        time.sleep(0.02)
    if errors:
        raise RuntimeError("descendant termination unverified: " + "; ".join(errors))


def kill_tree(process, job=None):
    """Terminate only this invocation's tree; raise if cleanup cannot be verified."""
    if os.name != "nt":
        return _kill_posix_tree(process)
    try:
        subprocess.run(["taskkill", "/T", "/F", "/PID", str(process.pid)],
                       stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    except (OSError, subprocess.TimeoutExpired):
        pass
    if job is None:
        process.kill()
        raise RuntimeError("no Job Object; descendant termination is unverified")
    job.terminate()
    job.verify_empty()


def run_command(target, tests_dir, log, timeout):
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join((str(tests_dir.parent), str(tests_dir)))
    env["PYTHONFAULTHANDLER"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000004} if os.name == "nt"
               else {"start_new_session": True})
    start = time.monotonic()
    timed_out = False
    termination_error = None
    with log.open("w", encoding="utf-8") as stream:
        process = subprocess.Popen(
            [sys.executable, "-B", "-X", "faulthandler",
             str(Path(__file__).with_name("suite_driver.py")), target, str(log.with_suffix(".jsonl"))],
            cwd=str(tests_dir.parent), env=env, stdin=subprocess.DEVNULL,
            # A runner started with an open, never-closed stdin pipe must not hand it to every test and child:
            # a test child that reads or probes stdin would otherwise block for the whole module timeout.
            stdout=stream, stderr=subprocess.STDOUT, **options,
        )
        job = None
        try:
            if os.name == "nt":
                # Fail closed if association fails: no test code has executed.
                job = _WindowsJob(process)
                _resume_windows_process(process)
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            try:
                kill_tree(process, job)
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                termination_error = str(error)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                termination_error = (termination_error or "") + "; root survived timeout cleanup"
        except BaseException:
            try:
                if job is not None:
                    job.terminate()
                    job.verify_empty()
                elif os.name != "nt":
                    kill_tree(process)
                else:
                    process.kill()
            finally:
                process.wait(timeout=20)
            raise
        finally:
            if job is not None:
                try:
                    job.terminate()
                    job.verify_empty()
                except (OSError, RuntimeError) as error:
                    termination_error = str(error)
                finally:
                    job.close()
    if termination_error:
        timed_out = True
    return {
        "returncode": process.returncode, "seconds": time.monotonic() - start,
        "timeout": timed_out, "log": str(log), "termination_error": termination_error,
    }


def run_module(module, tests_dir, log_dir, timeout, suffix=""):
    log = log_dir / (module + suffix + ".log")
    result = {"module": module, **run_command(module, tests_dir, log, timeout)}
    result.update(read_results(log.with_suffix(".jsonl"), module))
    result["status"] = "timeout" if result["timeout"] else "ok"
    failures = [row for row in result["tests"] if row["status"] in ("fail", "error", "unexpected_success", "unresolved")]
    empty_module = (result["tests_ran"] == 0 and not result["tests"]
                    and result["tests_loaded"] == 0 and result["unittest_result"] == "OK"
                    and result["returncode"] == 0)
    # An import-time crash (including SystemExit), a broken runner, or an abnormal
    # exit after successful tests must never turn into an empty successful result.
    if (not result["timeout"] and not empty_module
            and (result["returncode"] != 0 or result["unittest_result"] != "OK"
                 or result["protocol_error"] or failures)):
        result["status"] = "failed"
        # The driver exits 0 after a clean run and 1 after reporting failures. Any other
        # shape (a protocol error, an exit after `finished`, a kill) is a module-level
        # failure of its own: reruns of the individual failing tests must not clear it.
        expected_rc = 0 if result["unittest_result"] == "OK" else 1
        abnormal = (result["protocol_error"] or result["unittest_result"] is None
                    or result["returncode"] != expected_rc)
        if ((not failures or abnormal)
                and not any(row["import_error"] for row in failures)):
            result["tests"].append({"test": module, "status": "error", "classification": "real",
                                    "import_error": True})
            if not result["tests_ran"]:
                result["tests_ran"] = 1
    for row in result["tests"]:
        if row["status"] in ("fail", "error", "unexpected_success", "unresolved"):
            row["classification"] = "real"
    if result["termination_error"]:
        result["tests"].append({"test": module, "status": "error", "classification": "real",
                                "import_error": False, "runner_error": result["termination_error"]})
    return result


def rerun_failures(modules, tests_dir, log_dir, timeout, module_timeout):
    for module in modules:
        candidates = []
        for index, row in enumerate(module["tests"]):
            if row["status"] not in ("fail", "error") or row["import_error"] or row.get("runner_error"):
                continue
            log = log_dir / (module["module"] + ".rerun-" + str(index + 1) + ".log")
            print("Rerun: " + row["test"], flush=True)
            try:
                rerun = run_command(row["test"], tests_dir, log, timeout)
                rerun.update(read_results(log.with_suffix(".jsonl"), module["module"]))
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                row["rerun"] = {"returncode": None, "timeout": False, "log": str(log),
                                "runner_error": str(error)}
                continue
            row["rerun"] = rerun
            if passed_test(rerun, row["test"]):
                candidates.append(row)
        if candidates:
            print("Serial module rerun: " + module["module"], flush=True)
            try:
                serial = run_module(module["module"], tests_dir, log_dir,
                                    module_timeout, suffix=".serial")
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                serial = {"returncode": None, "timeout": False, "runner_error": str(error)}
            module["serial_rerun"] = serial
            for row in candidates:
                if passed_test(serial, row["test"]):
                    row["classification"] = "load-sensitive"


def passed_test(result, name):
    return (result["returncode"] == 0 and not result["timeout"]
            and result.get("unittest_result") == "OK" and not result.get("protocol_error")
            and not any(row["status"] == "unresolved" for row in result.get("tests", []))
            and any(row["test"] == name and row["status"] == "ok"
                    for row in result.get("tests", [])))


def make_report(modules, wall_seconds, log_dir, strict):
    rows = [dict(row, module=module["module"], log=module["log"])
            for module in modules for row in module["tests"]]
    real = [row for row in rows if row["classification"] == "real"]
    sensitive = [row for row in rows if row["classification"] == "load-sensitive"]
    timeouts = [{"module": module["module"], "test": None, "log": module["log"]}
                for module in modules if module["timeout"]]
    timeouts.extend({"module": row["module"], "test": row["test"], "log": row["rerun"]["log"]}
                    for row in rows if row.get("rerun", {}).get("timeout"))
    timeouts.extend({"module": module["module"], "test": None,
                     "log": module["serial_rerun"]["log"]}
                    for module in modules if module.get("serial_rerun", {}).get("timeout"))
    summary = {
        "modules": len(modules), "tests_ran": sum(module["tests_ran"] for module in modules),
        "ok": sum(row["status"] == "ok" for row in rows),
        "skipped": sum(row["status"] == "skipped" for row in rows),
        "expected_failures": sum(row["status"] == "expected_failure" for row in rows),
        "unexpected_successes": sum(row["status"] == "unexpected_success" for row in rows),
        "real_failures": len(real), "load_sensitive": len(sensitive), "timeouts": len(timeouts),
    }
    return {
        "summary": summary, "modules": modules, "real_failures": real,
        "load_sensitive": sensitive, "timeouts": timeouts,
        "slowest_modules": [{"module": module["module"], "seconds": module["seconds"]}
                            for module in sorted(modules, key=lambda item: item["seconds"], reverse=True)[:10]],
        "wall_seconds": wall_seconds, "log_dir": str(log_dir), "strict": strict,
        "exit_code": int(bool(real or timeouts or (strict and sensitive))),
    }


def print_report(report):
    counts = report["summary"]
    print("\nModules: {modules}; tests ran: {tests_ran}; ok: {ok}; skipped: {skipped}; "
          "expected failures: {expected_failures}; unexpected successes: {unexpected_successes}".format(**counts))
    for title, key in (("Real failures", "real_failures"), ("Load-sensitive", "load_sensitive"),
                       ("Timeouts", "timeouts")):
        print(title + ": " + str(len(report[key])))
        for row in report[key]:
            label = " [UNRESOLVED]" if row.get("status") == "unresolved" else ""
            print("  " + (row.get("test") or row["module"]) + label + " (" + row["log"] + ")")
    for module in report["modules"]:
        for attempt in (module, module.get("serial_rerun", {}),
                        *(row.get("rerun", {}) for row in module["tests"])):
            if attempt.get("termination_error"):
                print("WARNING: timeout cleanup incomplete: " + attempt["termination_error"])
    print("Slowest modules:")
    for module in report["slowest_modules"]:
        print("  {module}: {seconds:.2f}s".format(**module))
    print("Total wall time: {:.2f}s".format(report["wall_seconds"]))
    print("Logs: " + report["log_dir"])


def positive_int(value):
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return number


def positive_seconds(value):
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite positive number of seconds")
    return number


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-j", type=positive_int, default=max(2, (os.cpu_count() or 1) // 2))
    parser.add_argument("--module-timeout", type=positive_seconds, default=1800)
    parser.add_argument("--test-timeout", type=positive_seconds, default=600)
    parser.add_argument("--tests-dir", type=Path, default=Path(__file__).resolve().parent)
    parser.add_argument("-k", action="append", default=[], metavar="SUBSTR")
    parser.add_argument("--no-rerun", action="store_true")
    parser.add_argument("--strict", action="store_true")
    parser.add_argument("--log-dir", type=Path)
    parser.add_argument("--json", type=Path)
    args = parser.parse_args(argv)
    tests_dir = args.tests_dir.resolve()
    if not tests_dir.is_dir():
        parser.error("--tests-dir must be a directory")
    names = sorted(path.stem for path in tests_dir.glob("test_*.py")
                   if path.is_file() and (not args.k or any(key in path.stem for key in args.k)))
    if not names:
        parser.error("no test modules matched")
    try:
        log_dir = args.log_dir.resolve() if args.log_dir else Path(tempfile.mkdtemp(prefix="suite-logs-"))
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        parser.error(str(error))
    start = time.monotonic()
    modules = []
    print("Running {} modules with {} workers".format(len(names), args.j), flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.j) as pool:
        futures = {pool.submit(run_module, name, tests_dir, log_dir, args.module_timeout): name for name in names}
        for future in concurrent.futures.as_completed(futures):
            name = futures[future]
            try:
                module = future.result()
            except (OSError, RuntimeError, subprocess.SubprocessError) as error:
                # Keep infrastructure failures review-visible alongside test failures.
                module = {"module": name, "status": "error", "returncode": None, "seconds": 0,
                          "timeout": False, "log": str(log_dir / (name + ".log")), "tests_ran": 0,
                          "tests": [{"test": name, "status": "error", "classification": "real",
                                     "import_error": True}], "runner_error": str(error)}
            modules.append(module)
            print("{module}: {status} ({seconds:.2f}s)".format(**module), flush=True)
    modules.sort(key=lambda module: module["module"])
    if all(module.get("tests_loaded") == 0 and module["status"] == "ok" for module in modules):
        parser.error("no tests were loaded (overall zero-test selection)")
    if not args.no_rerun:
        rerun_failures(modules, tests_dir, log_dir, args.test_timeout, args.module_timeout)
    report = make_report(modules, time.monotonic() - start, log_dir, args.strict)
    print_report(report)
    if args.json:
        try:
            args.json.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
        except OSError as error:
            parser.error(str(error))
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
