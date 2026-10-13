# Changelog

## 0.2.0 - 2026-10-13

### New commands
- `land`: a per-repository first-in-first-out queue and one integrator lock (token, heartbeat; a dead holder is broken only with `--break --reason`). Landing is fast-forward only, runs the project's `land.gate` on the tree that will land, and appends the exact landed range to a ledger. `--rebase` rebases a linear branch; `--dry-run` takes no lock.
- `ship plan` / `ship run`: a manifest of the exact files, deletions and commits of a landed range, with superseded and foreign commits flagged. `ship run` is a dry run by default; `--execute` needs a fresh matching dry-run receipt and a confirm code (the first 8 hex digits of the manifest sha256), and manifests are accepted only in the exact canonical bytes `plan` writes. twinlane runs only the command you configure; it never deploys by itself.
- `wt doctor` / `wt prune`: a health report for linked worktrees; prune is a dry run by default, unlinks junctions and symlinks first without following them, deletes branches with `git branch -d` only, and records `prune.jsonl`.
- `run --best-of` and `compare`: the same brief on several engines in separate worktrees, then a side-by-side comparison, optionally running the project's test command.
- `resume`: lists jobs lost to a reboot or app quit with the likely cause, and with `--yes` continues each once. Never automatic.

### Configuration
- Per-project settings in the `projects` map of the user config only, never read from repository content: argv-list commands with the placeholders `{repo}`, `{worktree}`, `{branch}`, `{slug}`, `{target}`, `{manifest}`, `{source_commit}` and `{base}`. Values with line breaks are refused; `.bat` and `.cmd` commands receive only plain values.
- Codex: `codex.binary_source` (`auto`, `app-copy`, `path`), `codex.min_cli_version` and `codex.exec_config`.
- Installer: `install --check`, `--unhook`, `--hook-command`, `--alias` and `--skill`; the packaged `SKILL.md` ships with the package.

### Guard
- Denies database clients, configurable `guard.deny_commands` and `guard.deny_text`, program runners and nested shells, git ref writes, PowerShell execution and persistence commands, and twinlane commands that dispatch or mutate; `guard.allow_subagents` controls sub-agents. A `guard` policy that is not an object fails closed.
- A job cannot write its worktree's `.git`, and file-tool paths must be literal (`~`, `$VAR` and URLs are refused).
- Still a deny-list and defense in depth, not a sandbox; see LIMITS.md.

## 0.1.2 - 2026-10-11

### Guard
- Fail closed where the guard cannot judge: in a code job, any tool it does not know (MCP tools included) is denied, a write or shell call without a known job worktree is denied, and a call whose path or command is missing or not a string is denied.
- The parser reads what the shell runs: bash `$'...'` quoting (`$'\x2e\x2e/x'`) is decoded as bash decodes it; a command substitution (`$(...)`, backticks, `<(...)`) or an operator parameter expansion (`${X:-..}`, `${PWD%/*}`) in a path of a command that may write is denied, because its value is unknown until it runs.
- Resource caps deny instead of cutting: a brace expansion over 256 words is denied rather than checked in part.

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
- Archive extractors are allowlisted (only options known to keep member names inside the destination, in any getopt abbreviation and position); extraction by `cpio`, `jar`, `pax -r` and `python -m tarfile -e` is denied, and so is setting `TAR_OPTIONS`/`TAR_READER_OPTIONS`/`TAR_WRITER_OPTIONS`/`TAPE`/`UNZIP`/`UNZIPOPT`/`ZIPOPT`/`ZIP` (also through quoted or joined names, or a name built at run time) and a tar `-T` list without `--verbatim-files-from`.
- Read-only is judged per call: `git` only for read subcommands without `--output`/`grep -O`, `fd` without `-x`/`-X` in any cluster, `rg` without `--pre`, `xxd` with one file operand (its second operand is a write target), `tree` without `-o`/`-R`, `file` without `-C`; `less`, `ack` and `ag` are no longer read-only. A leading assignment's value (`TAPE=../x tar ...`) is path-checked, brace lists are expanded (`{..,b}/x`) and a glob component that can match `..` (`.?`, `.*`, `.[.]`) is read as `..`.
- `twinlane stop` gives a killed survivor its grace period before judging it alive (on Linux, SIGKILL lands after `kill()` returns).
- Still a heuristic over command text, not a sandbox.

### Board, meter and batons
- Every usage window is shown with its name and the binding one marked; refusals name the window that caused them. Caution routing prefers the other engine when it is below 75% and usable.
- Lost jobs that ended together are grouped on one board line without asserting a cause.
- Baton caps never drop unfinished jobs; handoffs list every unfinished job id or end with an exact `+N more` count; worker receipts never change a baton's owner.

## 0.1.0 - 2026-10-09

First public release.
