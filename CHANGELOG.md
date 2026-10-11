# Changelog

## 0.1.1 - 2026-10-11

### Background workers
- On Windows, every `--bg` runner is created through WMI (`detach=wmi`), outside every job object of the launching app and outside its process tree. When WMI cannot start it, `--bg` is refused (exit 3) instead of falling back to a start that could die with the app. macOS and Linux keep a new process session.
- Real quit-and-reopen tests of the Claude and Codex desktop apps on one tested Windows machine and build kept the runners and engines running. That is survival on that setup, not a general guarantee, and not task success. Sign-out, shutdown, sleep and reboot are untested.

### Liveness and stop
- Liveness is three-valued: alive, dead, unknown. A runner whose identity cannot be read is `unknown`, never `lost`: it keeps its slot, stays visible, blocks `--continue-job`, and `twinlane stop` will not kill it.
- After a timeout or interrupt kill, the runner checks the process it started. One that outlives its kill is recorded in `survivor.json` and the job stays `orphaned` or `unknown` until `twinlane stop` ends it.
- `twinlane run --resume SESSION` is refused while the tracked job that owns the session is live or unknown.
- Write jobs reserve their worktree, so two writers cannot run in one worktree.

### Money guard
- Codex: before every attempt, paid credits are refused for every model when the reading is missing, unknown, stale (over 300 s), on credits, past included usage or spend-capped, unless `codex.allow_credits` is true.
- Claude: extra usage must be known OFF; an unknown state is held as `unknown` with the reason shown. The optional automatic token refresh (off by default) runs only after a recorded OFF reading.
- Still a start-of-attempt check: nothing stops a running Codex job that crosses into credits partway.

### Guard
- Wider coverage of git writes (pull, revert, tag, update-ref, stash, notes, replace, config writes, `worktree add`, `branch -f`, `submodule foreach`, send-pack and other push or mail paths, program options such as `--upload-pack`), outbound sends, wrappers, `env -S`/`env -C`, git config passed through the environment, and inline interpreter code.
- Fails closed inside a job: an import failure exits 2, and a command too long or too complex to inspect is denied.
- Code workers start without secret-named environment variables; the receipt lists the stripped names only.
- On Windows, `/X` after `del`, `rd`, `move` or `copy` is read as a switch only when it is one of that verb's cmd switches and no PowerShell parameter is present, so `del /outside` is a path.
- `claude.extra_usage_allowed` takes effect only when it is the JSON value `true`.
- Archive, transfer and output tools (`unzip`, `tar`, `7z`, `cpio`, `jar`, `dd`, `rsync`, `scp`, `patch`, `wget`, `sort`, `split`, `sqlite3`, `python -m zipfile`, `Expand-Archive`) count as writes, and every `..`, home, variable or root path a writing command names is resolved, including attached option values (`-d../x`, `-xvC../x`, `of=../x`, `-Destination:..\x`), quoted ones and a POSIX-escaped dot (`.\./x`).
- Outside paths fail closed: a `..` or home path named by any command not on a short read-only list is denied, so in-place editors and file-mode tools (`sed --in-place`, `perl -pi`, `chmod`, `icacls`, `ed`, ...) need no listing; output options (`-o`, `--output`, `--target-dir`, `--prefix`, ...) are write targets for every command; `git apply --directory` is checked.
- Archive extractors are allowlisted (only options known to keep member names inside the destination, in any getopt abbreviation and position); extraction by `cpio`, `jar`, `pax -r` and `python -m tarfile -e` is denied, and so is setting `TAR_OPTIONS`/`UNZIP`/`UNZIPOPT`/`ZIPOPT`/`ZIP`.
- `twinlane stop` gives a killed survivor its grace period before judging it alive (on Linux, SIGKILL lands after `kill()` returns).
- Still a heuristic over command text, not a sandbox.

### Board, meter and batons
- Every usage window is shown with its name and the binding one marked; refusals name the window that caused them. Caution routing prefers the other engine when it is below 75% and usable.
- Lost jobs that ended together are grouped on one board line without asserting a cause.
- Baton caps never drop unfinished jobs; handoffs list every unfinished job id or end with an exact `+N more` count; worker receipts never change a baton's owner.

## 0.1.0 - 2026-10-09

First public release.
