# Changelog

## Unreleased

## 0.4.0 - 2026-10-27

- Job admission waits on lock contention instead of failing: a runner that cannot take `slots.lock` or `writers.lock` keeps waiting with jittered backoff, shows `waiting for slot lock` (or writer lock) in `status.json`, and fails only after `jobs.lock_wait_s` (default 30 minutes) with the last observed holder. Process liveness is probed outside `slots.lock`, and every admission file is re-read under it, so the critical section stays short and the slot cap is still exact. One `jobs.lock_wait_s` bound covers a whole streak of lock waits and restarted scans; after three restarted scans in a row the next attempt probes inside the lock, so the oldest job always decides; `release_stale_writers` (a lead command) still fails after 30 s.

- Retry transient Codex app-server meter failures once with a 30 s timeout. Dispatch can use a labelled stale-ok reading younger than 30 minutes (or the lower configured bound), only below handoff with known percentages, future resets and no paid credits. Preserve the genuine cache and its original timestamp; explicit meter/board output keeps the actual failure visible. Claude does not receive this fallback.
- Patrol completeness: unreadable job receipts and baton directories cannot resolve open custody
  or human-wait findings. Strict patrol inputs also cover meters, queue/brief stats, owner notices,
  job markers/identity/output stats and policy loading. Missing/stale/unusable host evidence emits
  an error; dependent findings remain stale and cannot authorize fixes. Unreadable previous state
  freezes writes/fixes for recovery. LIMITS documents every reader and its failure behavior; a
  parametrized evidence-failure matrix checks that no prior finding gains a false resolved event.
  Runtime queue waits retain board/queue dependencies; legacy absolute briefs are strictly statted
  without cwd; missing or malformed meter read_at/reading_at timestamps keep findings stale and
  prevent pumping. The reader table and matrix cover every finding-source dependency.

- `patrol`: bounded file-only checks of queued and running work, stale/unresolved custody, human waits
  and sustained host load. Optional `--fix` only pumps ready requests and releases proven stale writers;
  deduplicated reports, hourly reminders, JSON, bounded event history and repeat mode.
  Host reading is cache-only; missing, stale (over five minutes) or unusable counters mean `unknown`
  and preserve overload evidence as stale. `queue_ready` defers writer capacity to the pump;
  `queue_claimed` never replays a reservation and asks for inspection after ten minutes. With `--fix`,
  pump refusals become `queue_waiting` until evidence changes; `queue_stalled` requires a launch.
  Stable condition keys keep changing load measurements in `detail` without reopening findings or
  resetting hourly reminders. Quiet jobs respect `watchdog.enabled` and offer inspection when it is off.

- Patrol review fixes: non-spawning liveness for live jobs and the file-only board (unknown
  identities retain slots); stable runtime/pump wait categories keep changing measurements in
  `detail` and preserve reminders; lock-free custody snapshots report incomplete folder/claim reads
  as `patrol_error`, retaining earlier custody warnings as stale instead of resolving them.

- Overload handling: host-load admission (the usable slots per engine fall with CPU, free RAM and live Codex
  process count and recover as load clears; a job waits with a reason such as `host busy: cpu 97%`, never refused),
  first-in-first-out slot order with reviews first, a hung-worker watchdog (kill the tree, requeue once, then fail
  with `host overload`, never a verdict), automatic release of stale writer claims plus `wt doctor --fix`,
  `wip pump` to start ready queued writer requests, and a host-load line and QUEUE section on `board`.
  A stall kill that is not confirmed is never retried and holds the worktree claim (`tree-unresolved.json`) until
  `twinlane stop JOB --tree-gone`; a job runs at most twice; a pumped request stays claimed until its run is admitted.

- Host-load CPU is an average over `hostload.cpu_window_s` (default 20 s, 5..120) of a rolling counter history, not one 0.25 s window: spikes no longer read as "host busy: cpu 91%" while real use is about 35%, and a sustained overload still throttles. Cold start takes the lowest of three short samples.
- Baton `waiting_on` dependencies and stable `waiting_since`, ranked board waits and idle counts.
- `run --owner` completion history and inbox collection notices; file-only `watch` with independent,
  resumable cursors and cached usage/context crossings. Coordinating sessions re-arm after each wake.
- Weekly-pace capacity advice on board and status; worker guards refuse watch.
- Plain-text evidence, truncation warnings with full history, meter CLI discovery without worker
  environment changes, development version with source commit, and finished-job status output.

### Fixed
- Windows engine attempts start suspended and enter kill-on-close Job Objects before resuming. Only zero active processes confirms termination; snapshot fallback retains unresolved-tree custody.
- On macOS and Linux a stalled Claude worker's kill is reported unconfirmed like Codex's (a helper in its own session survives a process-group kill), so there is no automatic retry beside it. A queued request that a hand-typed identical command just satisfied is removed from the queue, so the pump cannot run it a second time; a claimed request still needs the pump's token.
- An engine that no job object holds gets unresolved-tree custody recorded while it is still suspended, before it runs; if the marker cannot be written it is not started.
- An interrupt during the final job-object drain records unresolved-tree custody before it propagates, and the job handle is always released (kill-on-close then reaps descendants).
- An engine started without a job object (assignment failed) never reads as confirmed cleanup, and an unresolved tree stops a usage-limit failover as well as a stall retry.
- A Windows attempt's job object is terminated and must report zero active processes before it is released; a descendant that outlives the root keeps the worktree claim. On macOS and Linux a stalled Codex runner kill is never confirmed (the engine runs in its own session), so there is no automatic retry. A folder holding only `tree-unresolved.json` now blocks the writer claimant itself, not just its explanation.
- Writer-admission explanations capture unresolved-tree markers and name the manual release command, including the pid-less launch-marker fallback.
- Windows tree termination verifies and kills through the same open handles, rejects stale parent links, and bounds process snapshot parsing and query retries.
- Job archival preserves mixed-version lifecycle locking, refuses nonquiescent homes, coordinates marker and baton writes, and retains moved evidence on uncertain index durability. Unrecognized job folders block writer admission and worktree cleanup.
- A job whose recorded child pid is now held by a process this token cannot read no longer keeps its WIP slot after a restart: a recorded creation stamp older than this boot (the System process's creation time from the process snapshot) proves the process is gone, so `stop`, `status` and `wip` settle a timed-out job whose runner died (`_win_boot_created`, `_predates_boot`). No boot proof keeps the identity UNKNOWN.
- A writer whose child pid was recycled by a process this token cannot open (a SYSTEM service) no longer holds its WIP slot forever: on Windows the creation time comes from a self-checked process snapshot, so the recycled pid reads dead. Nothing is killed.
- Codex failures retain error text and completion time; binary qualification and writer admission happen before dispatch.
- Land requires a configured gate when requested, verifies tree identity, records interrupted landing intent and recovers without duplicate events. Unverified ranges cannot ship.
- Removal restores unlinked dependencies when a custom command leaves the worktree folder behind.
- Single-writer job receipts: `stop` can no longer overwrite a receipt the runner publishes while it stops it. Stop writes
  `stopped` only from the runner role; while a runner may be alive it exits 4 (`stop_requested`) and the runner records
  the outcome. A stop that arrives after the work finished keeps the `done` receipt.
- A WIP claims provider whose temporary file cannot be created or read is a WIP error (writer count unknown), not a
  crash of board or status.
- `wt gc` / `wt prune` re-read the session supervisor, bypassing its cache, at the recheck right before a removal, so a
  session that started after the scan protects its worktree. A session in an unknown or unrecognised state protects
  its worktree, or blocks cleanup when its directory cannot be read. Idle sessions keep protecting theirs.

### New
- Parallel test runner with structured verdicts, isolated and serial-module failure triage, verified process-tree timeouts and zero-test rejection. Test modules get an empty stdin, so a runner started with an open pipe cannot stall their child processes.
- `jobs archive` previews or moves old, collected terminal jobs into a monthly archive while keeping id lookups working.
- Worktree WIP cap, versioned FIFO retry queue, `wip` and `wip next` (print only, no launch).
- Baton progress clock, bound sessions, parked state and derived stale. Landings mark landed and increment a count; batons are never auto-closed.
- Ranked cross-project inbox with hidden counts. Answers never become live-write approvals; board ATTENTION and SIGNALS are separate.
- `wt prune --merged-only`, thin `wt gc` alias, `--only`, minimum age and interactive-session protections.
- Context estimates labelled `~`; supervisor and local hook lane states with transcript fallback.
- Session-bound PreCompact, installer dry-run settings diffs and optional lane-state hooks; Codex notify configuration is printed only.
- Read-only `land card` and optional post-land worktree archive with prune safety.

### Changed
- Sandbox setup failures and read-only violations are failed outcomes. Codex qualification refusals exit 3 before dispatch.
- Preserving Codex starts use a qualified explicit binary and never fall back to another engine.

## 0.2.0 - 2026-10-14

### New commands
- `land`: a per-repository first-in-first-out queue and one integrator lock (token, heartbeat; a dead holder is broken only with `--break --reason`). Landing is fast-forward only, runs the project's `land.gate` on the tree that will land, and appends the exact landed range to a ledger. `--rebase` rebases a linear branch; `--dry-run` takes no lock.
- `ship plan` / `ship run`: a manifest of the exact files, deletions and commits of a landed range, with superseded and foreign commits flagged. `ship run` is a dry run by default; `--execute` needs a fresh matching dry-run receipt and a confirm code (the first 8 hex digits of the manifest sha256), and manifests are accepted only in the exact canonical bytes `plan` writes. twinlane runs only the command you configure; it never deploys by itself.
- `wt doctor` / `wt prune`: a health report for linked worktrees; prune is a dry run by default, unlinks junctions and symlinks first without following them, deletes branches with `git branch -d` only, and records `prune.jsonl`.
- `run --best-of` and `compare`: the same brief on several engines in separate worktrees, then a side-by-side comparison, optionally running the project's test command.
- `resume`: lists jobs lost to a reboot or app quit with the likely cause, and with `--yes` continues each once. Never automatic.

### Workers, meters and coordination
- `meter --no-rollout` and the policy key `meter.codex_rollout_fallback` (default `true`): with the fallback off, a failed Codex app-server read stays `unknown` and no rollout session file is opened. A non-boolean value counts as off.
- `run --preserve-existing-metadata` (Claude only): a start that never alters existing job metadata; it reads only a meter cache that is already fresh and never refreshes it, renews a token or acquires a Codex binary. Design notes: `docs/design/metadata-preserving-start.md`.
- `explain-writer-admission --cwd DIR [--job-id ID] [--json]`: a read-only explanation of the writer-directory check (reservation, relevant jobs, uncertainty). It creates no directories or locks. Design notes: `docs/design/writer-admission-explain.md`.
- Claude receipts carry `model_calls` (distinct main-loop model responses) and `subagent_model_calls` next to `num_turns`, with a `turns_note`: `num_turns` is the Claude CLI's own counter, a different unit from the `--max-turns` limit the CLI enforces. `twinlane result` prints `model_calls=` and `cli_turns=`.
- `run --resume-owner` (with `--task-id ... --continue-job ID`): the recorded coordinator continues the current owner's own finished job. It adds no ancestry and changes no ownership, epoch or revision. It is refused unless the predecessor is the owner's latest dispatch in the epoch, finished `done`, `--continue-job` names that job, no receipt is given, and every dispatch in the epoch is terminal. Workers cannot use it. Without the flag the envelope and fingerprint are unchanged, and the ownership and ancestor-cycle refusals apply as before.
- README "Common tasks" table and "Fresh readings" section; an agent skill in `skills/twinlane/SKILL.md`.

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
