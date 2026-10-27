# Limits

- Job admission lock contention waits up to `jobs.lock_wait_s` (default 1800 seconds / 30 minutes),
  with jitter and `waiting for slot lock` / `waiting for writer lock` in status and the board.
  Expiry reports how long the lock stayed busy and the last observed holder PID and age, if available.
  One bound covers a whole streak of lock waits and restarted scans (restarts do not reset it); after three restarted scans in a row the next attempt probes inside the lock so the oldest job always decides. The lead command `release_stale_writers` (`wt doctor --fix`, patrol) still fails after 30 s.
  Holder sidecars are best-effort diagnostics, never grounds to remove a lock. OS locks still release
  when the holder dies. Slot admission probes processes outside the lock and rereads admission files
  inside it; new identities cause a retry. Ordinary non-admission lock timeouts remain unchanged.

- Codex meter timeouts or closed output/no reply get one additional app-server exchange with a 30 s timeout (`APP_SERVER_RETRY_TIMEOUT_S`), before the rollout fallback; the default exchange budget is 15 + 30 s, plus bounded process cleanup. Missing binaries, protocol errors and unsupported requests are not retried. Only dispatch admission (`route`, `run`, best-of and start/failover checks) can use the last genuine `app-server` or `rollout` Codex section after an unavailable live read. Its original `reading_at` must be younger than `meter.stale_ok_max_age_s` (default 1800 s / 30 minutes), capped by `codex.stale_reading_max_age_s`. Every counted window needs a known percentage and a future reset; the section must be `ok` or `caution` under current thresholds and never on credits. Missing, unreadable, malformed, expired, reset, handoff/stop/out/unknown or previously carried sections are refused as before. A carried section is labelled `meter stale-ok (age Nm)` with `stale_ok` and `stale_ok_age_s`; it never refreshes its original stamp or replaces `state/meter.json`. Actual failed observations are stored separately in `state/meter-codex-failure.json`, so explicit meter commands and the board show the failure with only a labelled fallback-availability note. The 120 s cache TTL and `--fresh` semantics are unchanged. Claude's OAuth meter has no stale-ok fallback; its failures have a different cause. Post-job usage observations also never substitute stale-ok evidence.

- The writer cap coordinates one twinlane home on one machine. It counts distinct worktrees, not model calls or processes. Interactive activity comes from recent transcript markers and is an estimate; unreadable live job metadata occupies its own lane. Starting a CLI outside twinlane is not mechanically prevented. Queue entries contain metadata, never executable prompts, and `wip next` grants no slot or authority. `wip pump` claims a request (`pumping_at`) before it starts and the request leaves the queue only when the job of the run it started exists on disk; a claim older than five minutes is left for a human (`wip next`), never relaunched blind. A run started inside a job object that ends its children can therefore die with its parent; its request stays claimed rather than being lost. If a stall kill does not report that the whole tree is gone, the job is not retried and its worktree claim is held by `tree-unresolved.json` until a person confirms with `twinlane stop JOB --tree-gone`; twinlane cannot tell which processes survived and kills none of them from that marker. On Windows each runner attempt starts its engine suspended, assigns a kill-on-close Job Object and then resumes it; zero ActiveProcesses after TerminateJobObject is the only confirmation. Descendants stay contained even after an intermediate parent exits. Escaping descendants remain a limit for callers without that job handle (cross-process stop, or assignment failure such as forbidden nesting): snapshot cleanup is best effort and reports tree exit unconfirmed. An unconfirmed runner kill retains custody. The attempt handle closes after the result is read; first the job is terminated and must report zero active processes, otherwise (or if an interrupt cuts the drain short) the job keeps its worktree claim (`tree-unresolved.json`, or the pid-less launch marker when that cannot be written) like an unconfirmed stall kill. If the job object cannot be assigned (for example a host that forbids nesting), the attempt still runs but nothing can prove its descendants gone, so unresolved-tree custody is written BEFORE the engine is resumed (a killed runner or a `stop` cannot lose it) and every such attempt ends with the claim held until a person confirms; if that marker cannot be written the engine is not started; an unresolved tree also stops a usage-limit failover from starting a second engine. On macOS and Linux no stall kill is ever reported confirmed (the packaged Codex runner starts `codex exec` in its own session, and a Claude worker may start a helper in its own session): the process group is still killed, but the job is not retried and the claim is held until a person runs `twinlane stop JOB --tree-gone`, so the automatic stall requeue only works on Windows. It re-runs a queued `run` command exactly as typed (it holds the path of the brief, not its text) through full admission again, only for a request younger than a day whose brief file still exists.
- Codex Cloud (`--where cloud`) depends on the `codex cloud` subcommands of the installed Codex CLI and on a mirror command you supply. The CLI has no cancel, so `stop` cannot end a remote task. The firewall is pattern-based: it catches what it knows (tokens, bulk phones and e-mails, your literals) and cannot prove a brief or a snapshot carries nothing sensitive, so keep data out of the mirror. Status is read by polling, not pushed.
- Host load is advisory. CPU is averaged over `hostload.cpu_window_s` (default 20 s), so a spike shorter than that cannot hold the queue, and a real overload shorter than that is noticed late. It is read from CPU, free-RAM and process counters; desktop-heap exhaustion has no cheap counter, so it shows up only as a stalled worker, which the watchdog handles. The watchdog judges progress by file changes, so a worker that is busy but silent for longer than `watchdog.stall_s` is killed and requeued once.
- Stale is an inactivity projection, not proof of completion. Batons are never auto-closed; landing marks landed and explicit closure remains a human/lead action. Inbox answers are retained as context on that baton and never transfer live-write authority across sessions or engines. A lane resumes only through fresh admission.
- Context is an estimate from the latest request record in a bounded transcript tail. Claude input and cache tokens are summed; Codex uses `last_token_usage`, never cumulative totals. Missing usage or a missing model-window configuration stays unknown. The packaged context-window map is intentionally empty; verified Codex event windows can supply the denominator. The 70/85 flags are advisory and do not stop a model.
- `startedBy` is explicit recorded provenance; missing provenance stays unknown. `UNMANAGED` is a recent session outside direct job receipts, explicit baton bindings and the configured lead family. It does not imply that the session is malicious or still running. Child sessions are visible and are not automatically trusted because their parent is tracked.
- `wt gc` is a dry run by default and uses prune's existing ownership, age, lock, link and recheck protections. A zero-unique-patch result permits cleanup of a cherry-picked equivalent branch; its branch ref is kept if `git branch -d` refuses deletion. Post-land archive defaults to a suggestion; apply is an explicit user-config choice and retains prune safety. Configured dependency checks cannot prove that an application runs correctly.
- PreCompact snaps up to three batons explicitly bound by session id. A matching cwd alone grants no binding. It never calls a model, refreshes usage, creates a Git checkpoint or installs itself. The advisory hook remains fail-open if snapshotting fails.

- Claude's OAuth usage endpoint is undocumented and may change. The meter sends the locally stored token only to `api.anthropic.com` and refuses redirects. Unknown or stale readings block dispatch for that engine.
- Codex usage comes from its app-server interface and local session evidence. These formats may change; unknown readings stay unknown.
- Served-model reporting differs by vendor. A Codex receipt can only show the requested model when the CLI does not report the served one.
- Windows has the most complete detach behaviour. POSIX uses a new process session and process-group termination; Windows job-object details are unavailable there.
- On Windows, `--bg` runners start through WMI. Surviving a desktop app quit was shown with real quit-and-reopen tests on one tested Windows machine and build; that is not a general guarantee. Sign-out, shutdown, sleep and reboot are untested, and survival says nothing about whether the task succeeds.
- A job whose runner cannot be identified (for example from inside a restricted-token sandbox) reads `unknown`; twinlane will not stop it, so check it from the host.
- The Codex app-server meter currently finds only the Windows Codex desktop app. It copies the app-server build into `<home>/bin`, keeping at most 3 copies of about 434 MB each (about 1.3 GB total). On Linux/macOS, set `TWINLANE_CODEX_BIN` or rely on rollout files; otherwise the Codex meter reads **unknown**, and parallel/sweep routes fail safe to Claude or refuse.
- When the Codex app-server exchange fails, the meter falls back to the newest local rollout reading. `--no-rollout` or `meter.codex_rollout_fallback: false` turns that off; Codex then reads **unknown**.
- `twinlane install` registers an advisory hook for interactive sessions. The blocking guard only acts inside twinlane-dispatched workers; interactive sessions are NOT guarded. The guard is a heuristic over shell text, not a sandbox: it does not read script files, and an obfuscated command can still pass. Code workers must still run in worktrees with the vendor's own permissions and sandbox.
- Built and tested against the `codex exec --json` interface (flags `--json --model --config model_reasoning_effort=... --sandbox --cd --output-last-message`); older Codex CLI releases may not accept current model names; run `codex --version` and set your models in config.
- Model ids are defaults as of 2026-10; set your own in config.
- Credits and extra usage are checked only when a job starts. A long Codex job can still reach paid credits if your account allows it; twinlane passes no option that enables credits. Nothing in twinlane stops a running Codex job that crosses into credits partway. Check the receipt and meter before continuing.
- Removing secret-named environment variables from code workers does not remove secrets a script reads from files such as a repository `.env`. It also removes `CLAUDE_CODE_OAUTH_TOKEN`, so a Claude code worker on a setup that logs in only through that variable fails to authenticate; intel workers keep it.
- Outside-path checks fail closed: a `..` or home path named by any command that is not on the guard's short read-only list is denied, even when it is only read (`python ../tools/x.py`, `sort ../x > out.txt`); read or run it from a read-only command or from inside the worktree. A command is read-only per call: one with a write or run option is read-only only without it (`git` read subcommands without `--output` or `grep -O`, `find` without actions, `fd` without `-x`/`-X`, `rg` without `--pre`, `xxd` with one file operand, `tree` without `-o`/`-R`, `file` without `-C`), and `less`, `ack` and `ag` are not read-only at all. A path the shell computes when it runs (`$(...)`, backticks, `${X:-..}`, `${PWD%/*}`) in a command that is not read-only is denied, and so is a brace expansion over 256 words; bash `$'...'` quoting is decoded first. Code jobs allow only known tools: MCP tools, a write or shell call without a known job worktree, and a call without a string path or command are denied. What still passes: an ABSOLUTE path given to a command the guard does not know as a writer, outside an output option (`chmod 777 /abs/x`); a target taken from a config file or an environment variable; a plain `$VAR` given to a command the guard does not know as a writer; and anything a script file does. Codex workers also run under Codex's own `workspace-write` sandbox; Claude workers rely on the guard plus Claude Code's own permissions.
- Archive extractors are allowlisted: tar and bsdtar run only with options known to keep member names inside the destination (unknown options, `-P`, `--absolute-names`, `--transform`, `-s` and command hooks are denied in any getopt abbreviation), unzip only with plain letter options (no `-:`/`-^`), 7z without `-spf`/`-snl`/`-snh`; extraction by `cpio`, `jar`, `pax -r` and `python -m tarfile -e` is denied. A tar `-T`/`--files-from` list is denied unless `--verbatim-files-from` comes first, because GNU tar reads its lines as options. Setting `TAR_OPTIONS`, `TAR_READER_OPTIONS`, `TAR_WRITER_OPTIONS`, `TAPE`, `UNZIP`, `UNZIPOPT`, `ZIPOPT` or `ZIP` is denied in any syntax the guard reads, including names joined from quotes, escapes or `+`; an uppercase `UNZIP`, `ZIP` or `TAPE` anywhere in a command is refused too (`grep UNZIP` needs `grep -i unzip`); and `export`, `declare`, `env`, `setx`, `Set-Item env:` or `SetEnvironmentVariable` with a variable NAME built at run time (`export "$n=x"`, `$'..'`, `[char]`) is denied, because the name cannot be read. `git fetch`, `git clone` and `git ls-remote` may contact any host, so the URL itself can carry data out.
- After a timeout or interrupt kill, twinlane checks the process it started. On macOS and Linux the packaged Codex runner starts `codex exec` in its own session, so a Codex engine that outlives its runner is not detected; on Windows the attempt Job Object contains the tree; if assignment fails, verified snapshot members are killed best effort and tree exit stays unconfirmed.
- The guard hook runs with the Python interpreter recorded at install time. If that interpreter is removed (for example a rebuilt virtual environment), the hook cannot start and Claude Code does not block the call; rerun `twinlane install`.
- The guard is a deny-list and defense in depth, not a sandbox, and a deny-list cannot be complete: a new tool, an unmodelled spelling or a script file can pass. Real confinement comes from elsewhere: workers that hold no push or deploy credentials, a worker that runs only in its own worktree, and a lead who reviews every diff and reruns the gates. Treat a guard denial as a guardrail, and a guard pass as no evidence of safety. `guard.deny_commands` and `guard.deny_text` add your own rules; they do not make the guard complete.
- The guard acts only inside twinlane-dispatched workers. A `guard` policy section that is not an object, or cannot be read, denies every call of the job (fail closed); a typo in `deny_text` therefore stops the job instead of weakening it.
- `twinlane land` serialises landings through one lock per repository and lands fast-forward only. It trusts the project's gate to be a real check; with no `land.gate` it lands ungated and says so. A holder whose liveness cannot be judged (another host, an unreadable process identity) is never broken, so such a lock needs a person to look at it. The queue and ledger are local files: they coordinate sessions on one machine's state directory, not machines.
- `twinlane ship` never deploys by itself. It writes a manifest and runs only the command you configured in your user config, after a fresh dry-run receipt and a confirm code. It cannot tell whether that command is safe, and the manifest lists files and commits, not whether the release is correct. Manifest hashes are computed on the machine that plans, so line-ending or filter settings can make them differ across machines.
- `twinlane wt prune` never deletes an unmerged branch (`git branch -d`, never `-D`) and keeps any worktree that is dirty, in use, linked or unclassifiable; the cost is that some stale worktrees stay until you remove them yourself. Link handling covers Windows junctions and symlinks; an unusual reparse point is kept, not removed.
- `twinlane resume` continues only jobs whose status is exactly `lost`, only on request, once each. A continuation is a new attempt with the original brief and any recorded context; it does not restore a running conversation or undo partial changes. The restart cause is advisory: it compares times and can be `unknown`.
- `run --best-of` spends one run of usage per contestant and never fails over between engines. `compare` shows evidence; it does not choose a winner or merge anything.
- Per-project commands run with your user's rights and no shell. twinlane refuses a string command, an unknown placeholder, a value with a line break and, for `.bat`/`.cmd` targets, any value `cmd.exe` would interpret, but it does not sandbox the command itself.

- WIP sees only jobs, batons and local supervisor/transcript activity in this home. Sessions with no observable activity are invisible. Transcript activity uses file times; `wip.count_main_checkout` controls main-checkout lanes.
- An inbox yes is never a live-write approval. Answers are recorded separately from grants. Context numbers labelled `~` are estimates from the newest bounded usage record.
- Merged-only pruning keeps a one-hour minimum age except the optional archive of a just-landed worktree. It never deletes an unmerged branch.
- A landed-unverified range is never shippable. Re-gating is possible only when the lane worktree still sits on the landed commit. twinlane never deploys by itself.

- `twinlane patrol` is a file-only, one-pass report by default (exit 0 even with findings).
  It reads cached engine meters and host-load samples without refreshing them, and writes only
  `state/patrol.json` and `state/patrol.jsonl`. The cache-only host reader never measures or spawns.
  Missing, older-than-five-minute or unusable host counters mean host status `unknown`: the overload
  count is retained and an existing overload finding stays stale until usable evidence returns.
  Liveness observations never spawn `ps`: POSIX uses `/proc` or signal zero, keeping unreadable
  identities `unknown` and conservatively counted as running. This also applies to the file-only board.
  Custody snapshots take no lock and create no file; only a missing folder is complete and empty.
  Unreadable folders or claim files are incomplete evidence: `patrol_error` retains prior custody
  findings as stale, without authorizing release. Actual release still locks and revalidates claims.
  No model is called.
  `--fix` permits only `wip.pump_quietly()` and `jobs.release_stale_writers()`; pumping can launch stored
  requests through full admission. Engine workers may read patrol, but cannot run `patrol --fix`.
  All other findings are report only: unresolved trees require a person to verify they are gone before
  `twinlane stop JOB --tree-gone`; lost jobs point at `twinlane resume` and are never auto-resumed.
  `queue_ready` reports engine and host eligibility only; file-only readers cannot verify writer capacity.
  Only a confirmed `--fix` pump launch produces `queue_stalled`; a refusal is `queue_waiting` with the
  pump's own reason, retained on subsequent reports until evidence changes. `queue_claimed` is never
  replayed by patrol; a claim older than ten minutes (or with an unreadable timestamp) needs a person.
  A claim missing its timestamp defers the whole pump, since its replay guard cannot recognize it.
  Slot-leak checking is skipped: slots are derived from job liveness, with no separate cheap lease reader.
  `worktrees.doctor` is skipped because repository and dependency probes exceed the file-only pass budget.
  Each collector has a bounded wait within a roughly ten-second pass; slow or failed collectors become
  `patrol_error`, and unknown evidence does not resolve earlier findings. An overdue read is not duplicated
  on the next pass. Remedies check the pass deadline before making changes; an interrupted pump can leave
  a claimed request for manual inspection. The existing remedies retain their own admission and custody rules.
  `--loop SECONDS` requires at least 30 seconds; `--max-runs N` bounds the number of passes.
  `--quiet` suppresses unchanged output; opened, resolved and hourly reminder findings are printed.
  JSON always includes every current finding and its `new` flag. Ages and changing host measurements
  (`detail`) are separate from stable condition identity, preserving quiet output and hourly reminders.
  History retains the last 2000 opened/resolved/fixed events. Unreadable patrol state freezes
  state/history writes and fixes until it can be read or repaired; prior evidence is not overwritten.
  Configure `patrol.queue_stall_s` (600) and `patrol.person_wait_s` (1800); overload needs three consecutive
  measured passes above the configured host-load threshold; unknown evidence does not reset the count.
  A quiet job with the watchdog disabled asks for inspection and offers `twinlane stop JOB`.
  Patrol is advisory and never kills or stops a job.


### Patrol reader completeness

Incomplete evidence never resolves a finding. Each failed source emits `patrol_error`; earlier findings
that depend on it stay open with `stale=True`, `safe_fix=False`, including a finding emitted again with
the same key. Policy failures invalidate all dependent checks; details invalidate board checks, and
board/slots/host failures invalidate queue checks. Runtime waits also depend on the board's live-job
classification and queue evidence, despite their stored source being details. No error authorizes a fix.
A genuinely missing optional file/directory can be complete and empty; a denied parent is an error,
never absence.

The table covers every reader consumed by `patrol.collect()`. "Strict" means only FileNotFoundError
is accepted as absence; other access/I/O failures and invalid JSON/object types raise. The underlying
readers remain tolerant for other callers unless stated otherwise.

| Reader / evidence | Unreadable directory | Unreadable file | Malformed file | Patrol handling |
| --- | --- | --- | --- | --- |
| Policy + optional config (`load_policy`); host/watchdog/patrol settings | Strict open raises (config is checked without isfile) | Strict open raises; loader SystemExit is captured | Strict JSON/object raises; loader failure captured | policy error; all prior sources stale. Settings functions only transform this policy; invalid optional scalar settings keep documented defaults. |
| Board meters cache | Strict open raises | Raw reader returns None; patrol strict open raises | Strict parse requires engine objects and parseable read_at plus each engine's reading_at, including missing timestamps | board error; board, host, queue and runtime-wait findings stale; no pumping. Missing cache means engines unknown. |
| Board job directory scan (`observations.job_ids`) | scandir raises; missing directory is empty | Entry is_dir/stat errors propagate | Non-job directory names ignored | board/details/slots errors; their dependents stale. |
| Board job rows (`job/status/result.json`), runner.pid, survivor/child/tree markers | Strict patrol inputs raise | Raw JSON/text can return None/empty; strict patrol inputs raise | Strict JSON/object/status or runner identity validation raises; board problems also become errors | board error; prior job findings stale; details/slots independently validate these inputs. |
| Board batons (`collect_batons`, file_only) | Explicit listdir raises; missing directory is empty | Row reports problems | Row reports problems; invalid wait/inbox timestamps flag incomplete | board error; prior human waits stale. Non-patrol enumeration retains tolerant glob behavior. |
| Board inbox/attention + durable owner notices | Job scan raises | Raw notice read returns None; patrol strict input raises | Raw notice ignored; patrol strict JSON/object raises; malformed baton holds become row problems | board error; prior human waits stale. Inbox ranking and attention projection perform no extra evidence reads. Collected-marker stat is strict. |
| WIP queue (`wip.queue` equivalent) | Strict open raises | Raw read can default to empty; patrol strict open raises | Strict JSON, object, version, rows, identity and timestamp checks raise | queue error; prior queue and runtime-wait findings stale. Queue absence is complete and empty. |
| Queue launchability (`wip.launchable`) | Strict cwd/brief stat raises, including absolute legacy briefs without cwd | Permission/I/O stat failure raises; no brief contents read | Malformed argv/paths cause reader error or explicit unlaunchable reason | queue error + stale on read failure; genuinely gone brief/cwd is an explicit wait. |
| Host cache (`hostload.cached`) | Strict open raises | Raw reader returns None; patrol strict open raises | Invalid JSON raises; invalid timestamp/counters return None/unusable; any missing counter is partial | hostload error; prior overload and queue findings stale. Missing/stale cache is unknown; no sampling. Disabled host checks need no measurement. |
| Slots (`jobs.running_count`) | Strict job scan/inputs raise | Strict job inputs raise rather than accepting tolerant None | Strict inputs raise; non-integer/negative returned counts raise | slots error; prior queue findings stale. Native liveness stays non-spawning and conservative. |
| Read-only custody snapshot: writers directory, claims, receipt/status, runner identity, marker stats | listdir/stat/open raise; missing writers directory is empty | Strict open raises | Strict JSON/object/receipt status/runner identity raises | custody error; prior custody findings stale, no release. Missing receipt without a live status is incomplete; unknown identity needs existing finalized/grace proof. Release still locks and revalidates independently. |
| Job details: job/status + containment markers, output mtimes | Strict inputs/stat raise | Raw helpers use None/False; patrol uses strict inputs/stat | Strict JSON/object/status raises; log contents are not parsed | details error; prior unresolved/quiet-job/queue findings stale. Missing logs use job start time; watchdog settings come only from policy. |

The dependency audit below includes indirect invalidation. Failure of any named collector keeps
both re-emitted and prior matching findings stale, disables their fixes, and emits no resolved event.
The single parametrized reader-failure matrix exercises every source/dependency pair, including
runtime waits through board AND queue and legacy requests through their absolute brief path.

| Finding source / kinds | Required collect readers |
| --- | --- |
| board: job_orphaned, job_unknown, job_lost, job_long, waiting_on_person | policy, board, details |
| hostload: host_overload | policy, hostload, board, details |
| queue: queue_ready, queue_waiting, queue_claimed, queue_stalled, engine_blocked | policy, queue, board, slots, hostload, details |
| custody: custody_stale | policy, custody |
| details: custody_unresolved | policy, details |
| details: runtime queue_waiting | policy, details, board, queue (also slots and hostload through queue invalidation) |

Patrol errors identify the failed reader in the current pass; they never authorize fixes.
A failed whole-pass collector retains every prior finding as stale.

Patrol's own previous-state reader runs before collect: unreadable or malformed state emits a state
error and freezes state/history writes and fixes. It cannot reconstruct unreadable findings, so it retains
the stored observation for recovery instead of replacing it with an empty one. History is bookkeeping,
not evidence for finding resolution; its existing read/write errors emit a history error. Verdict text,
board signals and display-only activity stats do not decide patrol findings. Worktree/session/provider
readers are excluded from the file-only board.
