# Design

## Observation cost and read-only status

Each command/poll shares one invocation-local job index from a single directory scan.
Specs and writer identities are resolved once per distinct job/path and released at the
end of the invocation. Lane activity is indexed by baton and writer once, with lifecycle
mtimes checked before opening old specs. No age filter weakens live-worker custody.
Bound transcript lookup/context estimates and session provenance are shared within a poll.
Status dispatches before directory initialization and uses only cached meter evidence;
effective job state is reported separately from the persisted receipt status.

## Waiting, owners and wake delivery

Batons store `waiting_on` (`lead`, `user`, `job:<id>`, `lane:<slug>`, `none`) and
`waiting_since`, an ISO timestamp changed only when the dependency changes. Legacy
batons default to `none`. Job ids and lane slugs use the existing validators. A user wait
reuses the yes/no `--ask` and inbox path; answers are evidence, never execution authority.
Board reads rank lead/user waits oldest first and count observed idle lanes without writes.

`run --owner SLUG` validates an existing baton before allocation, merges the new job into
its job index and sets its dependency. Every terminal receipt persistence path notifies the
owner. Under the baton lock, completion records `job_done` with id, state, exit code and
receipt path. It changes a matching job dependency (or `none`) to its bound lane, otherwise
lead. Other dependencies are preserved. A per-job notification marker and history check
make repeated finalization idempotent; notification failures log without changing the job.
Before publishing a terminal receipt, the runner writes `owner-pending.json` with the
owner slug. Completion writes `owner-notified.json` and removes the pending intent.
Runner re-entry, wait, result and the writable inbox CLI repair pending completions;
metadata-preserving re-entry still refuses terminal-job mutation. Status, board and watch
never repair. Collection discovers per-job notices independently of the capped job index.
WIP admission is acquired before baton locks. Collection notices compare the dependency
and its timestamp with the completion marker and the existing result-collected marker.

`watch` reads finalized receipts, baton/history snapshots, cached meter windows and bounded
tails of identifiable bound sessions. There are no per-poll subprocesses, network requests,
model calls or refreshes. It watches Claude 5h/7d and Codex weekly 80%/85% crossings and
estimated bound context crossing 70%; missing estimates are skipped. Meter thresholds use
a high-water value per reset window, so decreases do not trigger or re-arm a wake.
Each poll sees only the latest meter/context observation. A threshold excursion that
rises and falls between observations is not detected or reconstructed. Context crossing
keys include the baton slug and session id, so shared sessions wake each bound lane.

Each validated watcher name owns `state/watch-<name>.json`. On startup it delivers retained
events absent from that cursor, including events predating the first invocation. Names
are independent. Output is printed and flushed before the atomic cursor write: a crash
can replay output, so delivery is at least once. No-event polls persist observation
baselines but acknowledge no events. Only the cursor file is written. Retention is bounded
by available receipts and baton history; this is a local wake mechanism, not an event broker.
Coordinating sessions run watch in the background and re-arm after each wake; worker guards
refuse it before cursor access. Defaults are a 5-second interval and a 3300-second timeout.

`capacity.py` provides pure weekly-pace advice: percent used divided by elapsed fraction
of a seven-day window. Only a measured lower Codex pace advises Codex first; ties and
unknown readings advise Claude first with the reason, naming missing meters. This line
is advisory and never replaces policy admission.

## Lanes, human questions and checkpoints

`wip.py` merges jobs, batons and recent interactive sessions by `common.writer_key`, the checkout toplevel.
Count and creation are serialized under the WIP lock. Writer rows without a known identity retain their lane.
The versioned `state/wip-queue.json` contains retry argv, never prompt bodies or approvals. A next operation
prints the retry command; it never dispatches and leaves the queue intact while full unless forced.

Jobs count as admission classifies them (`write`, or any profile but `intel`). A job releases its lane
only when its receipt is terminal and `jobs._writer_job_busy(id)` is false, the writer-directory
admission rule: a provably dead engine frees it even when the runner's identity is unreadable and its
status file is unfinished. A just-admitted job (no pid, no receipt) and a row without an id retain their lane.

Setting `wip.claims_command` to a non-empty argv list selects claims mode: writer jobs and claimed
worktrees count; batons and activity-window sessions do not. Because a baton holds no lane there, baton
commands (`baton new|set|snap`, `handoff`) skip writer admission and never run the provider. A null or empty
command keeps the default activity mode. Claims merge by `common.writer_key` and respect `wip.count_main_checkout`.

The provider prints `{"version": 1, "claims": [{"worktree": "<absolute path>", "id": "<opaque string>",
"age_s": null}]}`. Ages may also be finite numbers in seconds. Collection runs the argv
without a shell, stdin or pipes: stdout goes to a temporary file read up to 1 MiB (`output too large`
beyond), stderr is discarded, and on timeout the provider's process tree is killed with a bounded wait, so
a descendant holding inherited handles cannot stall admission under the WIP lock. Claims are fetched once
per command observation.

`wip.claims_timeout_s` defaults to 10. Spawn errors, timeouts, nonzero exits and invalid JSON, versions,
claim shapes or paths fail closed with `writer custody unknown: claims provider failed: <reason>`,
which names the config file to remove `wip.claims_command` from. Only writer admission fails: `board`
and `status` show the writer count as `?` with a signal, and read-only (intel) runs never enter admission.
Reasons are short fixed labels; provider output and arguments are never included in the error.

`lanes.py` derives stale from progress, local git activity, job activity and bound sessions. Mechanical
snapshots preserve the progress clock. Parked batons release their slot until explicitly resumed; after the
park timeout the board asks whether to close or resume. Only an explicit human/lead close writes closed.
Landing records landed, the count and verified status outside the integrator lock, including unverified landings.

The inbox ranks blocking questions, waiting questions and legacy approvals, collapsing duplicate legacy text.
Its header reports every hidden item. Answers take only the baton lock and append to `holds.answers`, never
`approvals_granted`; an inbox yes is never a live-write approval. ATTENTION contains inbox and stale work.
SIGNALS retains the P0 usage and process warnings. Board collection takes no locks and writes no state.

Supervisor discovery is memoized and bounded; failure falls back to transcript observations. Notification,
Stop and Codex notify write atomic local lane-state files, never contact a service and never store Codex
message bodies. Supervisor observations take precedence over hook state. Provenance never confers authority.

`wt prune --merged-only` and its thin `wt gc` alias retain shallow/deep blockers, interactive usage,
locks and immediate rechecks. Unique patches plus unique merge commits keep a branch; landed ledger tips
can identify squash landings. The merged minimum age defaults to one hour; `wt.behind_max` defaults to 50.
Removal unlinks dependencies first, restores links if a folder remains, and uses `git branch -d` only.
Worktree creation is gated by WIP. Configured dependencies must be ready and links resolve within allowed roots.
The optional post-land archive applies these same checks to one just-landed path, outside the integrator lock.

`context.py` uses the latest per-request usage, never cumulative billing totals. Window precedence is exact
model policy, engine policy, then the Codex rollout window. Every display carries `~` and `estimate: true`;
missing denominators remain unknown. PreCompact binds by session id, checkpoints up to three batons with
bounded lock time, preserves `progress_at`, and never guesses by cwd. Worker callbacks return first.
Install previews a redacted unified settings diff, preserves unrelated keys/events and requires `--yes`.
Codex notify configuration is printed for manual installation, never edited.

twinlane keeps its state under a configurable data directory. `state/` stores cached usage and locks, `jobs/` stores per-attempt inputs and receipts, `batons/` stores task context, and `bin/` is reserved for local helpers. The packaged policy is merged with a user JSON override at read time.

`meter.py` normalizes Claude OAuth usage and Codex app-server limits. Each reading carries source and age; missing evidence remains unknown. `route.py` is a pure decision function over a task kind, meter snapshot, and policy. `jobs.py` launches workers, records JSON events and final text, and tries the other engine only after a usage-limit result. `codex_runner.py` calls `codex exec --json` directly and writes the adapter evidence contract.

`baton.py` records goals, worktree state, decisions, holds, and next actions. `board.py` reads cached records without starting workers. `sessions.py` discovers vendor session files for forwarding. `hook.py` warns at the limit boundary; `guard.py` checks tool calls inside Claude workers. The advisory hook fails open, so a hook failure leaves the user's prompt usable. The guard fails closed inside a job: if it cannot load it exits 2 (a blocking PreToolUse result), and a scan that cannot finish denies. It is a heuristic over command text, not a sandbox, and an obfuscated command can still pass, so the worker's permission profile and the Codex sandbox remain necessary.

Liveness is three-valued: `common.pid_state` returns alive, dead or unknown, and a process whose creation time cannot be read is unknown, never lost. On Windows a process this token cannot open (for example a SYSTEM service that now holds a recycled pid) has its creation time read from a `NtQuerySystemInformation` process snapshot, trusted only when the same snapshot gives the current process the creation time `GetProcessTimes` gives it; a different time is dead, and without a trusted snapshot it stays unknown. A job keeps a `child-starting.json` marker from just before its engine starts until the engine is reaped, and a `survivor.json` when an engine outlives its kill, so a crash between those writes and the receipt still reads `orphaned` or `unknown`. On Windows, `--bg` runners start through WMI with the caller's environment handed over in an owner-only file, and `--bg` is refused when that start is impossible; POSIX runners use a new session. Windows tree kills verify creation times and terminate through the same pinned handles; snapshot parsing bounds the full fixed header and declared thread payload within each entry, and query growth is bounded.

Windows descendant membership uses each child's handle to read its recorded parent PID and monotonic process sequence number (`NtQueryInformationProcess`, classes 0 and 92), requiring the sequence to exceed its verified parent's while retaining all ancestor handles until termination. An orphan predating a reused parent PID fails this rule regardless of wall-clock rollback or recovery; creation FILETIMEs remain identity checks, never ancestry ordering evidence. An unavailable sequence query, mismatched parent, or inaccessible intermediate refuses that branch and returns `False` after attempting only the verified root and independently proven branches.

A job's `result.json` has one writer: the holder of the runner role (`runner.lock`, taken with O_EXCL; a lock naming a dead process is stale). `twinlane stop` writes `stop.requested` and kills the recorded tree, but writes `stopped` only after it takes the runner role itself, which a live runner never yields; it re-reads the receipt inside the role and keeps any terminal receipt. While a runner may still be alive, stop exits 4 (`stop_requested`) and that runner records the outcome itself: `stopped`, or `done` when the work had already finished. A background refusal takes the same role, and a runner that starts later finds the receipt and exits.

`jobs.codex_credit_refusal` runs before every Codex attempt and refuses paid credits for every model unless `codex.allow_credits` is true; a write job also reserves its worktree so two writers cannot share one.

A stale writer claim with a terminal receipt can be released when the runner and recorded child are provably DEAD, without attempt exit evidence. For an existing process whose creation identity is unreadable, the PID-reuse exception instead requires a DEAD runner and a `done` or `failed` receipt with a nonempty attempts list: the terminal final attempt has a matching integer exit code, a nonempty post-wait events path, no survivor, and an integer `n` equal to the recorded `status.json` `attempt`. Engines must match when both are present. Missing attempt numbers, unknown existence, a matching live child, survivors and launch markers block this exception. The exit evidence covers only the recorded child; descendants are covered by the survivor marker. Admission holds `writers.lock`, rechecks a pending status repair under `status.lock`, and refuses if that recheck is rejected. Repair returns `repaired`, `already-final` or `rejected`; unsuccessful repair never grants writer clearance.

## Project commands and the trust boundary

`projects.py` resolves a repository to its main checkout (the first `git worktree list` entry, confirmed by asking that checkout which worktrees it owns, because a linked worktree's `.git` file is writable by whoever works in it). Settings for `land`, `ship`, `wt` and `test` come only from the `projects` map of the user config, keyed by that path, and never from repository content. A worker in a checkout therefore cannot rewrite the gate, land command or ship command that judges its own work. Commands are argv lists with named placeholders replaced in one pass; there is no shell, unknown keys and placeholders are errors, and values with line breaks are refused. A `.bat` or `.cmd` target receives only plain values, because `cmd.exe` re-parses its arguments. State for each project lives under `state/<feature>/<repo_key>/`.

## Land queue and lock

`land.py` keeps `queue.json`, `holder.json` and an append-only `ledger.jsonl` per project, and serialises every read-modify-write behind one OS file lock. The integrator lock is a lease held by the running `land run` process (a token plus a heartbeat). Staleness alone never takes it: a live holder is respected, a provably dead one is broken only by an explicit `--break --reason`, and an unjudgeable one (another host, an unreadable process identity) is never broken. Landing is fast-forward only; the gate runs on the tree that will land, and a gate that changes `HEAD` or the tree is refused. The ledger records each landing's exact range, which is what `ship plan` later reads. A file that exists but cannot be parsed is an error, never an empty queue, so a write cannot silently drop entries.

## Ship manifests and receipts

`ship plan` hashes the bytes a checkout of the source commit would produce (git attributes are read from that commit) and records files, deletions, commits, excluded commits and flags for foreign and superseded changes. The manifest is accepted by `ship run` only in the exact canonical bytes `plan` writes, and `run` works on a private copy keyed by its sha256. `ship run` is a dry run that writes a receipt; `--execute` crosses a different boundary than the plan: it needs a fresh, unused receipt for the same manifest hash and command, a confirm code (the first 8 hex digits of the hash), a manifest that still matches the repository, and a re-hash just before the command starts. twinlane runs only the configured command; it has no deploy logic of its own.

## Prune safety

`wt prune` is a dry run first. It keeps a worktree if anything uses it, if it is dirty, if its branch is not merged, if it holds submodules or links it cannot classify, or if it is itself a link. Junctions and symlinks inside a worktree are unlinked first, after recording their targets and without following them, because some Windows Git builds descend into a junction and delete what it points at; a link that will not go, or a vanished target outside the worktree, stops the prune. Branches are deleted with `git branch -d`, never `-D`, so git itself refuses an unmerged branch. Each action is appended to `prune.jsonl`.

## Resume

`resume.py` re-runs a lost job's request as a continuation. It never runs on its own, selects only jobs whose status is exactly `lost`, and stops at the first refusal. `host.py` compares the job's start with the machine's boot time to name a restart as the likely cause; a missing boot time gives `unknown`, never a guess. A `resumed.json` marker prevents a second resume.

Exit codes: 0 success, 2 command usage, 3 refused, 4 still running, 5 failed, 6 usage limit, 127 missing executable.
