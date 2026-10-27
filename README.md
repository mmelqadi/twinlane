# twinlane

**Claude Code + Codex. One workflow.**

Run Claude Code and Codex together on your included subscriptions, with usage-aware routing, saved handoff briefs, and background jobs with receipts.

When one engine approaches its usage limit, the other may still have allowance. twinlane brings both readings into one view, helps route the next task, and keeps a local record of the work.

Works with Anthropic's Claude Code CLI and the OpenAI Codex CLI on Windows, macOS and Linux. Use it when you keep hitting Claude Code usage limits or Codex rate limits mid-task, when you want parallel AI coding agents in separate git worktrees, or when you need a guardrail hook that keeps an autonomous coding agent inside its worktree.

[![CI](https://github.com/mmelqadi/twinlane/actions/workflows/ci.yml/badge.svg)](https://github.com/mmelqadi/twinlane/actions/workflows/ci.yml)
[MIT license](LICENSE) · [Latest release](https://github.com/mmelqadi/twinlane/releases/latest) · Python 3.9+ · Standard-library runtime

## What you can do

Job admission waits through contention on `slots.lock` and `writers.lock`, with jittered retries
and a visible `waiting for slot lock` or `waiting for writer lock` reason in `status.json` and the
board. The default lock-wait bound is 30 minutes; set `jobs.lock_wait_s` in your config to change it.
Process checks run outside the slot lock; admission rechecks job metadata under the lock before
claiming capacity. Reviews retain priority and other waiters retain FIFO order.

- **Wake the coordinating session.** Use `twinlane watch --name lead` to wait for completed jobs,
  batons waiting on lead, cached usage crossings and estimated context crossings.

- **See both usage limits.** Check Claude Code and Codex windows together, including the window currently limiting each engine.
- **Route new work.** Choose an engine by task type, fresh usage readings, and your configured thresholds.
- **Prepare a handoff.** Save the task's goal, progress, decisions, and recent evidence in a brief for the next engine.
- **Inspect delegated work.** Background jobs leave event logs, final results, and receipts you can review locally.
- **Land and ship deliberately.** A single-integrator land queue with a gate, a release manifest for exactly what landed, worktree health checks, best-of runs across engines, and restart recovery for lost jobs (see the sections below).

If a dispatched worker ends with a recognized usage-limit result, twinlane can make one policy-checked attempt on the other engine, carrying the original brief, available partial output, and worktree changes. This is a new attempt; it does not migrate a running conversation.

### Coordination commands

```sh
twinlane baton new example --goal "Review the change" --waiting-on lead
twinlane baton set example --waiting-on user --ask "Review this one change?"
twinlane run --owner example --brief brief.md --cd . --kind review --bg
twinlane watch --name lead --timeout 3300 --interval 5 --json
twinlane result JOB
twinlane status JOB
twinlane --version
```

`waiting_on` accepts `lead`, `user`, `job:JOB`, `lane:SLUG`, or `none`. A user wait needs
`--ask`, just like the existing question inbox; an answer never grants a live-write approval.
The board ranks **Waiting on lead** and **Waiting on user** by oldest wait first, shows idle
lanes, and includes `writers N/MAX · queued Q · route: ENGINE-first (reason)`. This routing
advice compares weekly pace (usage divided by the fraction of the week elapsed); dispatch
still checks the configured policy and fresh meters.

`--owner SLUG` requires an existing baton. Job completion records a history event and wakes
its bound lane, or lead when no session is bound, unless the baton is waiting on a user or
another dependency. The inbox's `collect JOB for SLUG` item clears when anyone runs
`twinlane result JOB`, answers that inbox item, or changes the baton's wait.

A coordinating session should run `twinlane watch` in the background and re-arm it after
each wake. It exits after printing pending events, or a timeout line. Each `--name` has an
independent cursor; delivery is at least once, with acknowledgement only after output.
Watch polls files only and never refreshes meters or calls a model. Engine jobs cannot run it.

`baton set --evidence "plain text"` stores a text evidence item; `kind|path|result` still works.
Capped baton text is truncated with an ellipsis and a stderr warning; history retains the
full text for that write. `meter` discovers the newest complete app CLI build, then PATH,
without exporting its choice into worker environments. `status JOB` shows completion
state, exit code, time and receipt path; `--version` includes a best-effort short source commit.

### Preview the board

The image and runnable demo below use **simulated usage and job data**. They show the board without contacting a vendor.

![twinlane board](docs/demo.svg)

## Try the demo

With Git and Python 3.9+ installed:

```sh
git clone https://github.com/mmelqadi/twinlane.git
cd twinlane
python examples/demo/run.py
```

The demo creates disposable fixture data and removes it when finished. You do not need subscriptions, installed agent CLIs, or a pipx installation to preview the board. Use `python3` if that is your Python command.

## Quickstart

### 1. Install for live work

From the source checkout above, with pipx installed:

```sh
pipx install .
```

For an installation without a source checkout, use `pipx install git+https://github.com/mmelqadi/twinlane`. The example briefs below are in the source checkout.

Live workers need the installed `claude` and `codex` CLIs, signed into the accounts you intend to use. Check your installed CLI versions and configure model names for your accounts.

Refresh the usage readings and inspect the routing decision:

```sh
twinlane meter --fresh
twinlane route intel
```

If an engine reads `unknown`, follow the displayed reason before starting work. On macOS/Linux, set `TWINLANE_CODEX_BIN` to your Codex binary or use available rollout evidence (turn that off with `--no-rollout`, see [Fresh readings](#fresh-readings)); automatic Codex desktop discovery is Windows-specific. See [Limits](LIMITS.md) for platform and authentication details.

If the Claude CLI login has expired, renew that login and run `twinlane meter --fresh` again. `--fresh` rereads usage; it does not renew credentials or make a model call. You can also supply a usage reading with `twinlane meter --record-claude <get_usage json>`.

### 2. Run a first read-only task

From the twinlane source checkout, use the included brief to inspect this repository:

```sh
twinlane run --kind intel --cd . --brief examples/briefs/first-review.md
twinlane board
```

The `intel` profile requests read-only work. Inspect the returned job ID with `twinlane result JOB`, replacing `JOB` with that ID.

To inspect your own repository, change `--cd` to its path and supply a brief file describing your task.

### 3. Try a writing task in a linked worktree

Writing jobs require a **linked Git worktree**. From the twinlane source checkout:

```sh
git worktree add ../twinlane-first-task -b twinlane-first-task
twinlane run --kind code --cd ../twinlane-first-task --brief examples/briefs/first-change.md --write --bg
```

The included brief asks for one small documentation improvement. Use a different branch and directory name if those already exist. After the job starts, inspect its ID with `twinlane wait JOB` and `twinlane result JOB`, then review the worktree diff yourself.

### Optional: interactive usage warnings

The advisory Claude hook is optional for dispatching workers. Preview it before applying it:

```sh
twinlane install              # preview the hook change
twinlane install --yes        # apply it to your Claude settings file, with backup
```

Credits and extra usage are checked only when a job starts. A long Codex job can still reach paid credits if your account allows it. See [Money guard](#money-guard) and [Limits](LIMITS.md) before running live jobs.

## Common tasks

Transient Codex meter failure: dispatch retries once, then may use a labelled `meter stale-ok (age Nm)` reading for up to 30 minutes; `twinlane meter --fresh` and the board still show the actual failure (see [LIMITS.md](LIMITS.md)).

| I want to | Run |
| --- | --- |
| See both engines' usage, jobs and batons on one screen | `twinlane board` (`--md` for chat, `--watch 30` in a terminal) |
| Get fresh usage readings | `twinlane meter --fresh` (see [Fresh readings](#fresh-readings)) |
| Know which engine should take a task | `twinlane route KIND` (exit 3 = refused, with the reason) |
| Run a read-only task in the background | `twinlane run --kind intel --cd DIR --brief F --bg` |
| Let a worker edit code | `twinlane run --kind code --write --cd LINKED_WORKTREE --brief F --bg` |
| Wait for workers without polling | `twinlane wait JOB1 JOB2 --any --timeout 60` |
| Read what a worker did | `twinlane result JOB` (`--json` for the receipt) |
| Hand a task to the other engine | `twinlane baton set SLUG --next "..."`, then `twinlane handoff SLUG --to codex` |
| Land finished work, one branch at a time | `twinlane land enqueue SLUG`, then `twinlane land run SLUG` |
| List exactly what landed, for a release | `twinlane ship plan` |
| Check or clean up worktrees | `twinlane wt doctor`, then `twinlane wt prune` (dry run unless asked) |
| See writer capacity and queued requests | `twinlane wip`; `twinlane wip next` pops one request without launching it; `twinlane wip pump` starts ready ones |
| Find stalled work and safe recovery commands | `twinlane patrol`; `--fix` pumps ready requests and releases stale custody; `--loop 120 --quiet` repeats |
| See why a job is waiting, and how busy the host is | `twinlane board` (host-load line and QUEUE section) |
| See questions that need a person | `twinlane inbox` (ranked questions and hidden count; `--all` shows every question) |
| Record a question's answer | `twinlane inbox answer task:H1 --grant "verbatim words"` or `--deny "reason"` |
| Clean up worktrees with no unique patches | `twinlane wt gc` (preview), then `twinlane wt gc --apply` |
| Preview a compaction checkpoint hook | `twinlane install --precompact` (settings diff; no install without `--yes`) |
| Continue jobs lost to a reboot | `twinlane resume`, then `twinlane resume --all-lost --yes` |

Every command takes `--help`. Exit codes: 0 ok, 2 usage, 3 refused by policy, 4 still running or lock held, 5 failed, 6 engine limit hit.

Patrol's host reader is cache-only: it never samples the machine or starts a process. Missing samples,
samples older than five minutes, and samples with no usable counters give host status `unknown`; they
preserve the overload count and keep an existing overload finding as stale. `queue_ready` means engine
slots and host allow the request; writer capacity is decided by the authoritative pump. With `--fix`,
patrol records the pump's refusal as `queue_waiting`, retaining that reason on later reports until new
evidence changes it. A `queue_stalled` finding requires a confirmed pump launch. `queue_claimed` marks an
outstanding pump reservation, which patrol never replays; after ten minutes it asks a person to inspect
it. Changing host measurements update `detail` without reopening a finding or resetting its hourly reminder.

Patrol and `board.collect(file_only=True)` observe process liveness without spawning `ps`.
On POSIX they use `/proc` when available or signal zero; an unreadable recorded identity stays
`unknown` and occupies its slot. Runtime and pump queue waits use stable reason categories
(host busy, engine stop, slots full, writer capacity, claimed, other); measurements stay in `detail`.
Custody reporting takes no lock and creates no file. Failed folder or claim reads produce
`patrol_error` and keep previous custody warnings stale until a complete snapshot is available.

To teach a coding agent these commands, copy [`skills/twinlane/SKILL.md`](skills/twinlane/SKILL.md) into its skills folder (for example `~/.claude/skills/twinlane/` or `~/.codex/skills/twinlane/`).

## Usage and routing

| Kind | First | Fallback |
| --- | --- | --- |
| `code`, `intel`, `plan`, `review` | Claude Code | Codex CLI |
| `parallel`, `sweep`, `integrate`, `verify` | Codex CLI | Claude Code |

The default thresholds are caution at 75%, prepare a handoff at 80%, stop new nonurgent work at 85%, and hard stop at 100%. An unknown or stale reading is **unknown**, never 0%; it cannot authorize that engine.

Every usage window of both engines counts: Claude's 5-hour, weekly and per-model windows, and each Codex limit. The meter and the board list each window and mark the one that sets the engine's maximum `[binding]`; a window twinlane does not know keeps its own name with `(unrecognised window)`. Every refusal names the window that caused it, for example `claude stop (90% in 7d-sonnet)`, and `--json` carries the same facts as `blockers`. When the first-choice engine is at 75-85% and the other is below 75% and usable, `twinlane route` picks the other one and says so.

### Fresh readings

Dispatch trusts a reading only while the cache is younger than `meter.cache_ttl_s` (120 s); the board may show older readings, so a route the board shows is not yet a dispatch admission. `twinlane meter --fresh` takes a new reading of both engines and writes the cache: one OAuth GET for Claude (no model call, no token renewal; add `--refresh` only if you accept one tiny model call to renew an expired token) and one read-only app-server exchange for Codex.

Two settings keep that refresh narrow:

- `TWINLANE_CODEX_BIN=PATH` runs that Codex binary as given. On Windows, twinlane otherwise copies the desktop app's build into `<home>/bin`, leases the copy and prunes older copies; an explicit binary outside the app's own folders skips all three.
- `--no-rollout` (or `meter.codex_rollout_fallback: false` in your config) never reads local Codex rollout session files. A failed app-server exchange then leaves Codex **unknown** instead of falling back to the newest rollout reading.

```sh
TWINLANE_CODEX_BIN=/path/to/codex twinlane meter --fresh --no-rollout --json
```

Check `codex.source` (`app-server`), each engine's `status` and `reading_at`, then dispatch within the cache TTL.

### Money guard

Credits and extra usage are checked only when a job starts. A long Codex job can still reach paid credits if your account allows it; twinlane passes no option that enables credits.

- Claude: dispatch is refused while paid extra usage is ON, and also while its state is unknown. An unknown state is held as `unknown` with the reason shown by `twinlane route`, `twinlane meter` and the board, and `twinlane meter --record-claude <get_usage json>` clears it. The optional automatic token refresh (off by default, `meter.auto_refresh_claude`) makes its one small model call only when the last recorded reading shows extra usage OFF.
- Codex: before every attempt, a failover included, twinlane refuses for every model when the Codex reading is missing, unknown, older than `codex.preflight_max_age_s` (300 s), on credits, past its included usage or spend-capped. The refusal names the model.
- Setting `claude.extra_usage_allowed` or `codex.allow_credits` to true in config allows those jobs and prints a warning that charges may apply. There is no per-job credits flag.

## Workers and receipts

`twinlane run` accepts `--engine`, `--kind`, `--cd`, `--brief`, `--bg`, `--write`, `--name`, `--baton`, `--effort`, and `--timeout`. A code worker can edit its selected worktree; an intel worker gets a read-only profile. Background jobs have an ID, an evidence directory, event stream, final message, usage information where reported, and a receipt. Use `twinlane wait JOB --any`, `twinlane result JOB`, or `twinlane stop JOB`.

Codex uses the installed CLI through `codex exec --json`. `TWINLANE_CODEX_RUN` can name an optional compatible adapter. No adapter is required. Evidence includes `events.jsonl`, `last-message.md`, and `result.txt`.

A code worker, Claude or Codex, starts without any environment variable whose name looks like a secret (TOKEN, SECRET, PASSWORD, API_KEY, `*_KEY`, AWS_*, AZURE_*, GH_*, OPENAI_*, ...). The receipt's `env_stripped` lists the stripped names, never their values. A secret a script reads from a file is not removed.

Job liveness has three values: alive, dead and unknown. A process that exists but whose creation time cannot be read (a restricted-token sandbox cannot open the host's runners) makes its job `unknown`, never `lost`. On Windows a process that cannot be opened is still identified by its creation time in a system process snapshot, so a pid that a system service took over after the job ended reads dead and frees the job's slot. An `unknown` job keeps its engine's slot, stays on the board and in the baton, blocks `--continue-job`, and `twinlane stop` will not kill it; check it from the host. After a kill at a timeout or an interrupt, the runner checks the process it started (the Claude CLI, or the Codex runner) instead of trusting the kill command. A process that is still running is recorded in `jobs/<id>/survivor.json` and the job stays `orphaned` (or `unknown`) until `twinlane stop` ends it. `twinlane run --resume SESSION` is refused while the tracked job that owns that session is live or unknown.

`twinlane jobs archive [--older-than DAYS] [--include-unread] [--apply] [--json]` previews old finished jobs, with candidate details and skip reasons. The default age is 14 days (`jobs.archive_after_days` in policy). Only receipts with status `done`, `failed`, `timeout`, `stopped` or `refused` qualify. Receipts must have been read with `result` unless `--include-unread` is given. Live or unknown processes, runner locks, unreadable evidence and pending baton work prevent archival. `--apply` refuses with exit 3 unless every job in the live folder is terminal and dead; dry runs still list candidates and explain any apply block. New lifecycle writers take an outer archive lock before the original in-folder locks, preserving exclusion with older runners. Archival probes and closes the in-folder locks before renaming while retaining the outer lock. `--apply` rechecks each job and moves it to `<home>/jobs/archive/YYYY-MM/<id>`, then appends an audit entry to `index.jsonl`; it never deletes evidence. An index write or sync failure reports `index entry uncertain` and keeps the job moved; lookups scan the archive layout and retries do not duplicate entries. `result`, `status` and `wait` still resolve archived ids, including when the index is unavailable. Normal `jobs` and `board` listings show active folders only. Workers may preview but cannot apply archival. Archival cannot exclude a twinlane release older than 0.3.3: run `--apply` only once every process using the home runs this release, because an older `stop` or refusal aimed at an already archived id could recreate a live folder next to the archived evidence.

On Windows, every `--bg` runner is created through WMI (`detach=wmi`), outside every job object of the launching app and outside its process tree. If WMI cannot start it, `--bg` is refused (exit 3) rather than falling back to a start that could die with the app. macOS and Linux keep their own path: a new process session and process-group stop.

## Batons, board, and sessions

`twinlane baton new SLUG --goal "..."` starts a local task record. `baton set`, `baton snap`, and `baton show` maintain it. `twinlane handoff SLUG --to codex` writes a brief using the task record and recent evidence. Approvals never transfer with a brief.

Writer admission counts each worktree toplevel once across both engines. Live writer jobs, active/review
batons and interactive sessions active within `wip.session_active_s` (default 1800 seconds) occupy a lane.
The main checkout counts by default (`wip.count_main_checkout`). `wip.max_writers` defaults to 5.
At capacity a new lane exits 3 with a deduplicated FIFO request. `twinlane wip` shows the lanes and queue;
`twinlane wip next` prints the next retry command and pops it only when capacity exists (`--force` overrides
the pop check). It never starts a worker. Read-only jobs do not count.

Stored baton states are `active`, `review`, `landed`, `closed`, `waiting-on-you`, `waiting-external`, and `parked`.
`stale` is derived after `lanes.stale_after_s` without progress, local git activity, jobs or bound session activity.
Checkpoint snaps do not reset the progress clock. Batons are never auto-closed, even when every acceptance item
passes: landing marks `landed` and records its count and verified state. Close explicitly with
`twinlane baton set SLUG --state closed`. Resume with `--state active`.
Use `--ask "Archive these 2 branches?" --record branches --count 2` or `--external https://example.com/issue/1`.
`--state blocked` is refused: ask one question or name an external dependency.
`--state parked --park-for 8h` releases the baton slot; the default park timeout is 3 days. A park past its
timeout becomes derived stale with "parked past timeout: close or resume?" on the board.

`twinlane inbox` ranks blocking questions, waiting questions and legacy holds across projects, oldest first
within each rank. It always shows the open, shown and hidden counts; `--all` reveals every item.
`twinlane inbox answer SLUG:H1 --grant "yes, go ahead"` (or `--deny "reason"`) writes `holds.answers`.
An inbox yes is an answer, never a live-write approval. Live writes still need the user's confirmation naming
the action and its records, given to the session that performs them. Explicit baton approval resolution remains separate.
Board ATTENTION contains inbox items and stale lanes; SIGNALS retains usage, process and receipt warnings.

Sessions and board show a `~` context estimate from the newest usage record, with advisory checkpoint/rotate
flags at configurable thresholds (70%/85% by default). Set `context.window_tokens.models`, then per-engine
`context.window_tokens.claude`/`codex` when verified; Codex can fall back to its rollout window. Unknown sizes stay unknown.
Claude supervisor `agents --json` supplies working/idle/needs-you states when available, with transcript and
local hook-state fallback. `startedBy` and `UNMANAGED` describe provenance, not authorization.

Bind sessions explicitly using `baton new ... --session SESSION_ID` or
`baton set SLUG --bind-session SESSION_ID` (repeatable, at most 8; `--unbind-session` removes one).
`twinlane install --precompact` previews a settings diff; the callback checkpoints up to three batons bound
to that session id, never guesses by cwd and preserves `progress_at`. Worker callbacks do nothing.
`twinlane install --lane-state` previews Notification/Stop hooks and prints a Codex notify line for manual
configuration. Installation is opt-in with `--yes`; no Codex configuration file is edited.

A baton is capped at 8 KB by trimming the oldest finished history. An unfinished job is never trimmed, so many live jobs can leave a baton over 8 KB, and a handoff lists every unfinished job id or ends with an exact count of the ones it left out. Worker receipts update a baton's job and in-flight fields but never its `owner` (the lead); only `twinlane baton set --owner` changes that.

`twinlane board --md` is a read-only view of cached usage, routing, live and finished jobs, batons, and linked worktrees. `twinlane sessions` lists open or recent Claude Code and Codex CLI sessions available for forwarding. The demo below is generated from a disposable fake home by `python examples/demo/run.py`:

```text
twinlane board  2026-10-11T10:15:33Z  (read-only; cached meters, 10s old)
== METERS
claude: caution 5h 77% (resets in 1h59m) [binding] | 7d 31% (resets in 2d7h) | extra usage off [fixture, 10s old]
codex: ok 5h 42% (resets in 2h59m) [binding] | 7d 18% (resets in 3d0h) [fixture, 10s old]
route now: ->codex code* intel* plan* review* parallel sweep integrate verify  (* = fallback)
lanes: 1/5 writer lanes busy (twinlane wip)
== ATTENTION (0)
  nothing needs you
== SIGNALS (2)
  claude CAUTION (77% in 5h, stop at 85%)
  2 finished, not yet read; newest: twinlane result 20261011-101533-codex-b2
== JOBS  running/queued 1
  20261011-101533-codex-c3  codex  code      running    1m 2%t  6.1-sol/?  demo-c3  wt=example-repo
  last finished (2):
  20261011-101533-codex-b2  codex  done        2m ago  6.1-sol/?  demo-b2  (unread)
  20261011-101533-claude-a1  claude done        4m ago  sonnet-5-5/sonnet-5-5  demo-a1  (unread)
== BATONS (1)
  demo  owner=codex  20s  next: Review the latest receipt
== WORKTREES (0 linked, 0 checked, 0 merged)
  none (batons point at no linked worktree)
```

## Landing finished work

`twinlane land` lets one integrator land worktree branches into the target branch one at a time. Each repository (identified by its main checkout) has one first-in-first-out queue and one integrator lock.

```sh
twinlane land enqueue my-task --worktree ../app-my-task    # append the branch to the queue
twinlane land status                                       # queue and lock holder
twinlane land run --dry-run                                # print the checks; no lock, no writes
twinlane land run                                          # land the head of the queue
```

`land run` runs the project's `land.gate` in the slug's worktree on the tree that will land, then fast-forwards the target branch. A failing gate lands nothing, and a gate that moves `HEAD` or changes the tree is refused. If the target moved, `--rebase` rebases a linear branch first; a range that holds merge commits is never rebased. Every outcome (landed, refused, failed, broken lock) is appended to a ledger that records the exact commit range.

The lock is held by the running `land run` process and carries a token and a heartbeat. A live holder is never taken, even with a stale heartbeat. A provably dead holder is cleared only with `twinlane land release --break --reason "..."` (or `land run --break --reason "..."`); a holder whose liveness cannot be judged is never broken. Other subcommands: `next`, `dequeue SLUG`, `log [SLUG]`. Add `--json` for machine-readable output. Exit codes: 0 ok, 2 usage, 3 refused, 4 lock held, 5 gate or land failed. Workers cannot run `land`; run it from the lead session.

## Shipping landed work

`twinlane ship` never ships by default. It turns a landing into a manifest and runs the command you configured, behind a dry run.

```sh
twinlane ship plan my-task                         # manifest of the slug's latest landing
twinlane ship plan abc1234..def5678                # or an explicit commit range
twinlane ship run --manifest PATH                  # dry run: runs ship.dry_run_command, writes a receipt
twinlane ship run --manifest PATH --execute --confirm 1a2b3c4d
```

`ship plan` lists the exact files of the range (path, sha256 and size of the bytes a checkout of the source commit produces), the deleted paths and the commits. Commits that are not the slug's are flagged as foreign, and paths that later commits changed again are flagged as superseded; the plan is refused unless you pass `--allow-foreign` or `--allow-superseded`. `--baseline REF` names what is already shipped; `--landing N` picks which landing of the slug (1 is the first, -1 the last).

`ship run --execute` needs a configured `ship.command`, a passing dry-run receipt for the same manifest that is younger than `ship.dry_run_max_age_s` and not used yet, and `--confirm` equal to the first 8 hex digits of the manifest sha256. A receipt is good for one execute. A manifest is accepted only in the exact canonical bytes `ship plan` writes. Without `ship.command`, `ship run` only prints the manifest path and exits 3. Workers cannot run `ship run`.

## Overload handling

On a busy machine twinlane queues work instead of piling it on. Nothing is ever refused for load.

- **Host-load admission.** Each job waits for a slot, and the number of usable slots per engine falls as the host gets busy and recovers as it clears (never below `hostload.min_parallel`). CPU, free RAM and the count of live Codex and sandbox processes are read from kernel counters only (no WMI): Windows system calls, Linux `/proc`, and on macOS the load average, `vm_stat` and `ps`. A waiting job shows `host busy: cpu 97%` (or the RAM or process reason) in `twinlane board`.
- **Order.** Waiting jobs start first-in-first-out per engine, reviews first. The board's QUEUE section shows the order and each reason.
- **Hung-worker watchdog.** A worker whose output files do not change for `watchdog.stall_s` (default 900 s) has its process tree killed through its recorded creation stamp. The job is requeued once; a second stall fails it with `host overload`. A stalled attempt never carries a verdict, so a review that did not run is `failed`, never a pass or a block. The retry happens only when the kill of the whole tree reported success; on Windows the runner starts each engine suspended, assigns it to an attempt Job Object with kill-on-close, then resumes its main thread through Toolhelp (Python closes the original thread handle). TerminateJobObject is confirmed only when its accounting reports zero active processes within five seconds, including descendants whose parents already exited. Assignment failure uses snapshot cleanup and cannot confirm a kill. The handle closes after the engine result is read, reaping strays. Cross-process stops have no attempt handle and cannot confirm tree exit from snapshots. Otherwise the tree may still hold running children, so the job fails with `host overload`, is not retried, and leaves a `tree-unresolved.json` marker that keeps the worktree claim (and refuses a second writer there) until a person has looked and runs `twinlane stop JOB --tree-gone`, which logs the release. If that marker cannot be written, the job's launch marker is rewritten without a pid and kept instead; it holds the claim the same way, and no check of the dead root removes it. An unresolved tree counts as live uncertainty everywhere a pid would: job status, slot counts, continuation, archive and worktree cleanup checks. A retry that cannot even start (for example the state directory becomes unwritable) ends the job `failed` with the stall evidence kept, never `stalled`. The marker is also written when the runner is interrupted after an unconfirmed stall kill, and both admission checks of a pumped run name the same queued request, so a lane that fills in between returns the original request to the queue instead of adding a placeholder. Nothing is killed from that marker. A stall never causes a third attempt: a job runs at most twice.
- **Queued writer requests.** `twinlane wip pump` (also run when a writer job ends) re-runs the oldest ready request that `wip` queued, when a lane is free and the host can take it. It runs the stored command exactly as typed, from the directory it was typed in, through full admission again; the request stays claimed until the run's job exists on disk, so a run that dies at once, or is refused after it was admitted (routing, binary checks), leaves a claimed request for a person instead of losing it. Each claim carries a token that the pump hands to the run it starts; only that run may acknowledge or give the claim back, so an identical command typed by hand cannot take it. It leaves anything with a placeholder, a missing brief file or an age over a day in the queue. Set `wip.autopump` to false to turn it off.

Settings, all optional, in the user config:

```json
{"hostload": {"enabled": true, "cpu_soft": 80, "cpu_hard": 98, "ram_free_soft_mb": 2048,
              "ram_free_hard_mb": 512, "procs_soft": 16, "procs_hard": 32, "min_parallel": 2, "cpu_window_s": 20},
 "watchdog": {"enabled": true, "stall_s": 900, "poll_s": 15},
 "wip": {"autopump": true}}
```

Between `soft` and `hard` the usable slots shrink linearly from `max_parallel` to `min_parallel`. A reading that cannot be taken never throttles. The CPU figure is the average over the last `cpu_window_s` seconds (5 to 120, default 20) from a short history of kernel counters kept in `state/hostload-cpu.json`, so a burst of short spikes does not read as an overload while a sustained one still does; with under 5 s of history it takes the lowest of three short samples.

## Codex Cloud (`--where cloud`)

When the bottleneck is the laptop, run Codex jobs in Codex Cloud and keep a small local pool for what only this machine can do (a database, a dry run). Cloud is off until you configure it.

```
twinlane run --where cloud --kind review --brief review.md --cd REPO --bg
twinlane run --where cloud --kind code --write --cd LINKED_WORKTREE --brief b.md --bg   # patch is applied to that worktree
twinlane run --where cloud --follow-up JOB --attach report.md --brief next.md --cd REPO --bg
```

- **Same job, other place.** A cloud job has the same fields, receipt, board row and rules as a local one; the receipt adds `where: cloud` and the task id, URL and patch hash. It is Codex only (`--engine claude`, `--resume` and `--continue-job` are refused with `--where cloud`; use `--follow-up JOB` to continue).
- **One data firewall.** Everything that leaves the machine (the composed brief, every `--attach`, the snapshot) is scanned first: token patterns, bulk phone numbers and e-mail addresses, your own `deny_literals` and patterns, size and UTF-8 limits, and an optional `command` hook. A hit refuses the job and names the rule and line, never the matched text.
- **Snapshot.** The task runs on a branch of a code-only mirror that your `cloud.mirror` command publishes for each job (`{job} {ref} {sha} {branch}`; one lock so two jobs never publish at once; an unchanged ref can reuse the last branch). twinlane ships no mirror tool and no company name.
- **Results come back as patches.** The runner polls `codex cloud status` through one shared cache (many jobs, one poll), fetches `codex cloud diff` into `result.patch` with its sha256, refuses a patch that touches `.cloud/`, and for a writing job runs `git apply --check` and `git apply` only inside the linked worktree. It never runs `codex cloud apply` in a shared checkout.
- **Failure stays failure.** A cloud task that fails, or an unreachable service, is recorded as failed. It is never silently re-run locally; the lead decides. The CLI cannot cancel a task, so `twinlane stop` ends the job and leaves the task running remotely.
- **Cloud-first.** `cloud.default_where: "cloud"` sends every Codex job there unless you pass `--where local`, it is a resume/continue, or the brief matches one of `cloud.needs_local` (regexes). A misconfigured cloud runs locally with a visible note. `--where local` always wins. The ordinary `codex.max_parallel` stays the size of the local pool; `cloud.max_parallel` (default 20) caps cloud jobs, which do not shrink with host load.
- **Chained work.** Build in the cloud, verify locally, send a sanitized report back with `--follow-up JOB --attach report.md`; the earlier job's final report is added to the brief (clipped) and is scanned like everything else. `twinlane redact report.md` writes `report.md.redacted` with secrets, every phone number and non-allow-listed e-mail address replaced by `[redacted:RULE]`, and refuses to write it if the text still fails the scan.
- **Left-out files and leaving slots.** `cloud.mirror.dropped_note_file` names a text file your mirror command writes (the paths it left out); it is scanned and appended to the prompt so the worker asks for a file instead of inventing it. A task that is still running remotely when its runner times out or is stopped keeps counting against `cloud.max_parallel` until a status call shows it finished or `cloud.occupancy_ttl_s` (default 24 h) passes.

```json
{"cloud": {"enabled": true, "env": "<codex cloud environment id>", "default_where": "cloud",
           "needs_local": ["\\bsqlsrv\\b", "dry run"],
           "mirror": {"argv": ["python", "mirror.py", "{job}", "{ref}", "{sha}", "{branch}"], "branch": "cloud/{job}"},
           "firewall": {"deny_literals": ["internal-hostname"]}}}
```

`twinlane board` shows cloud jobs as engine `cloud` and adds a `cloud N running, M queued (max 20)` line next to the local pool.

## Worktree health

```sh
twinlane wt doctor [--all] [--md | --json]    # report (and stale writer claims)
twinlane wt doctor --fix                       # release stale writer claims
twinlane wt prune                              # dry run
twinlane wt prune --yes [--include-empty]      # remove what passed every check
```

`wt doctor` reports each linked worktree: merged, unlanded, empty, detached or missing; stale; uncommitted changes; what uses it (a live job, a baton, a land queue entry); missing or dangling dependency folders; and the Windows long-path risk. `--all` also covers every project in your config.

`wt doctor` also lists stale writer claims: a claim whose job has finished (terminal receipt, no survivor marker) and whose runner and engine are gone, or whose pid can no longer be read because the host is overloaded, once the receipt is older than two minutes. `wt doctor --fix` releases them and logs each release to `custody-releases.jsonl`; the same check runs automatically when a new writer is refused by such a claim. Workers cannot run `--fix`.

`wt prune` is a dry run unless `--yes`. It removes a worktree only when its branch is merged and had commits of its own, it is clean, nothing uses it, and its last activity is older than `wt.prune_min_age_s`. Links (junctions and symlinks) inside it are unlinked first, without being followed. The branch is deleted with `git branch -d` only; an unmerged branch is never deleted. Every action is recorded in `prune.jsonl`. Workers cannot run `prune`.

`wt gc [--apply]` uses the same prune checks with zero unique patches determined by `git cherry`.
`wt prune --merged-only` enables that comparison too. Patch-equivalent cherry-picks can therefore be
eligible even without ancestry; Git may keep their branch when `branch -d` refuses it. Dirty, busy, locked,
too-recent or uncertain worktrees stay in place. In merged-only mode, landed/closed batons are historical references and no
longer pin their worktrees; active interactive sessions still protect theirs. Unmerged work more than
`wt.behind_max` commits behind (default 50) is labelled `rebase or park` and never removed. Archive defaults to a suggestion; user configuration can opt into pruning only the just-landed worktree. Configured dependency folders are checked
before writer dispatch and again before each attempt; missing, empty or dangling folders refuse the writer.

## Best-of runs

```sh
twinlane run --best-of claude,codex@sol-high --brief task.md --cd . --write
twinlane compare GROUP --wait --test
```

`run --best-of` starts the same brief on several engines (at most `best_of.max_contestants`, default 3). Each contestant is an ordinary background job, so the meters and the money guard apply to every one, and none fails over to the other engine. N contestants spend N times the usage. A write contestant gets its own worktree and branch cut from `--base` (default `HEAD`); read-only contestants share `--cd`. `--dry-run` shows the plan.

`twinlane compare GROUP` lists each contestant's receipt and diff; `--wait` waits for the jobs, `--test` runs the project's configured `test` command in each worktree. Nothing is picked or merged for you. `compare GROUP --discard` (dry run unless `--yes`) removes only that group's empty worktrees, through the same checks as `wt prune`.

## Resuming lost jobs

`twinlane resume` lists background jobs whose status is exactly `lost`, typically after a reboot or an app quit, with the likely cause when the host's boot time shows one. It never runs on its own. `twinlane resume JOB...` or `--all-lost [--since 48h]` selects jobs; `--yes` continues each one once (`run --continue-job`), at most `resume.max_batch` (default 10), and stops at the first refusal. A job that is `unknown`, `orphaned` or running is never resumed, because it may still be alive. A resumed job is marked in `jobs/<id>/resumed.json` and is not resumed twice.

## Per-project configuration

Land, ship, worktree and best-of behaviour is set per repository in the `projects` map of your **user** config, keyed by the main checkout's path. It is never read from the repository itself, so a worker inside a checkout cannot rewrite the gate or ship command that judges its work.

```json
{
  "projects": {
    "/srv/app": {
      "land": {"target": "main", "gate": ["make", "check"]},
      "ship": {"dry_run_command": ["make", "release-dry-run", "MANIFEST={manifest}"],
               "command": ["make", "release", "MANIFEST={manifest}"]},
      "wt": {"deps_dirs": ["node_modules"], "stale_days": 7},
      "test": ["make", "test"]
    }
  }
}
```

Commands are argv lists, never strings; nothing runs through a shell. Placeholders are `{repo}`, `{worktree}`, `{branch}`, `{slug}`, `{target}`, `{manifest}`, `{source_commit}` and `{base}`; an unknown placeholder is an error, and a value with a line break is refused. A `.bat` or `.cmd` command only receives plain values. Other keys: `land.command`, `land.command_rebases`, `land.heartbeat_s`, `land.heartbeat_stale_s`, `land.gate_timeout_s`, `ship.dry_run_max_age_s`, `wt.root`, `wt.setup_command`, `wt.remove_command`, `wt.link_roots`, `wt.prune_min_age_s`. Unknown keys are refused. With `land.require_gate: true`, a missing gate refuses landing. Otherwise an ungated landing is labelled explicitly.

## Configuration

`TWINLANE_HOME` overrides the data directory. Otherwise Windows uses `%LOCALAPPDATA%/twinlane`; macOS and Linux use `$XDG_DATA_HOME/twinlane` or `~/.local/share/twinlane`. The directory holds `state/`, `jobs/`, `batons/`, and `bin/`. `TWINLANE_CONFIG` selects a user JSON file; otherwise twinlane reads `<home>/config.json`. User values merge recursively over the packaged `policy.json`.

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` select the Claude and Codex configuration directories. `TWINLANE_CODEX_BIN` selects the Codex binary for the runner and the meter. `twinlane install` previews the resolved Claude settings path before `--yes` writes it.

The optional adapter named by `TWINLANE_CODEX_RUN` receives `--brief PATH --cd DIR --tier TIER --out DIR --timeout SECONDS`, optional `--model MODEL --effort EFFORT`, and `--write` for a code job. It receives `TWINLANE_CODEX_ADAPTER_STRICT_CREDITS=1` and `TWINLANE_CODEX_BIN` when available. It must write `<out>/events.jsonl`, `<out>/last-message.md`, and `<out>/result.txt`; it must exit nonzero on refusal or failure. The strict credits variable asks the adapter to refuse paid credits. The packaged runner does not need this adapter.

Add local deny rules in your own config, for example:

```json
{"claude": {"deny": ["Bash(my-release-command:*)", "PowerShell(my-release-command*)"]}}
```

Arrays replace the packaged array; include the generic rules you still need. Do not put credentials in the config file.

`jobs.archive_after_days` sets the default minimum completion age for `jobs archive` (14 days). `--older-than` overrides it for one invocation; ages must be finite, nonnegative numbers, and a job must be strictly older than the threshold.

`--include-unread` preserves unread `--owner` completion notices: those jobs remain active until their receipt is collected with `result`.

Guard settings go in the `guard` section: `deny_commands` (program names, optionally with leading arguments), `deny_text` (regular expressions matched against the command text), `self_aliases` (extra names for the twinlane command) and `allow_subagents` (default false). A `guard` section that is not an object, or cannot be read, denies every call of the job.

Codex settings: `codex.binary_source` is `auto` (default: the desktop app's private copy when there is one, else `codex` on PATH), `app-copy` or `path`; `TWINLANE_CODEX_BIN` overrides it in every mode. `codex.min_cli_version` (`X.Y.Z`) refuses a binary that is older or whose version cannot be read. `codex.exec_config` is a list of `"key=value"` strings passed to `codex exec` as `-c`; it may not set the model, its effort, the sandbox or the approval policy, which twinlane sets per job.

Installer options: `twinlane install --check` is a read-only report (one hook expected, shims, skills). `--unhook` removes only twinlane's hook entry. `--hook-command CMD` registers or recognises a custom hook command. `--alias NAME[,NAME]` writes command shims into `--bin-dir` (default `~/.local/bin`); `--replace` backs up and replaces a shim twinlane did not write. `--skill DIR` writes the packaged `SKILL.md` into `DIR`. Every write is a preview until `--yes`.

## Platform and security

Windows has detached background workers and process-tree stop support. POSIX workers detach with a new session and stop their process group. Windows job-object details have no POSIX equivalent. The WMI start was tested with synthetic job-close and tree-kill tests and with real quit-and-reopen tests of the Claude and Codex desktop apps on one Windows machine and build: the runners and engines kept running. That shows survival on that tested setup only; it is not a general guarantee, and it says nothing about whether the task itself succeeds. Sign-out, shutdown, sleep and reboot were not tested. The Codex app-server meter currently finds only the Windows Codex desktop app. It copies a complete app-server build into `<home>/bin`, keeping at most 3 copies of about 434 MB each (about 1.3 GB total). On Linux/macOS, set `TWINLANE_CODEX_BIN` or rely on rollout files. Otherwise the Codex meter reads **unknown**, and parallel/sweep routes fail safe to Claude or refuse.

`twinlane install` registers an advisory hook for interactive sessions. The blocking guard only acts inside twinlane-dispatched workers; interactive sessions are NOT guarded. The guard is a heuristic over shell text, not a sandbox. It parses each shell command a worker runs and denies git history, ref and config writes (commit, push, pull, merge, rebase, reset, revert, tag, `stash` other than list/show, `notes` writes, `config` writes, `submodule foreach`, program options such as `--upload-pack`), outbound sends (curl/wget/Invoke-WebRequest with a body, a non-GET method or a non-local host, mail clients, ssh/scp/rsync to a host, `gh` writes, cloud CLIs, package publishing), nested engine launches, and in the code profile paths outside the worktree: any path named by a write command, a redirect or an output option (`-o`, `--output`, `--target-dir`, ...), and a `..` or home path named by any command that is not on a short read-only list (so `chmod`, `sed --in-place` or `perl -pi` on `../x` are denied without being listed). Archive extractors run only with options known to keep member names inside the destination. It follows wrappers (`env`, `sudo`, `timeout`, `xargs`), `env -S`/`env -C`, git config passed through the environment, and code given inline to an interpreter. It does not read script files, and a command obfuscated in a way it does not model can still pass: quoting beyond simple concatenation, aliases and functions, a bare `$CMD`, a script written and then run, lookalike Unicode in a command name. It also denies database clients, program runners and nested shells, git ref writes, PowerShell execution and persistence commands (scheduled tasks, crontab), twinlane commands that dispatch or mutate, and any write to the worktree's `.git`; file-tool paths must be literal (`~`, `$VAR` and URLs are refused). The guard is defense in depth, not confinement: see [Limits](LIMITS.md). A command too long or too complex to inspect is denied, and if the guard itself cannot load inside a job it blocks the call (exit 2). The meter reads local CLI credentials and vendor usage information. Its Claude endpoint is undocumented and may change; redirects are refused so the token stays on the original host. No API key router is used.

## What this is not

twinlane is not an API-key router or a multi-model chat UI. It coordinates two installed CLIs and stores evidence locally.

## Related work

- `amElnagdy/delegate-skills`: discovery and setup for 17 CLIs.
- Claude/Codex orchestrator, delegate, and handoff projects: single-direction delegation or file handoffs.
- `ccusage`: Claude token and cost reports.

## Tests and license

Run `python -m unittest discover -s tests -v`, or use the faster parallel option
`python -B tests/run_suite.py -j 4` with serial failure triage (see
[Running the tests](CONTRIBUTING.md#running-the-tests)). Tests use fixtures and temporary homes; they do not call vendor networks. MIT licensed; see [LICENSE](LICENSE).

`land card BRANCH` (or `land card --baton SLUG`) is a read-only, one-screen review of the worktree,
ahead/behind, unique commits, diffstat, gate evidence, acceptance and inbox. Enqueue prints its verdict.
After landing, `land.archive_worktree` is `suggest` by default (`off`/`apply` are opt-in alternatives);
the suggestion uses `wt prune --merged-only --apply --only PATH`. Automatic archive keeps every prune
safety and skips only the one-hour minimum age for that just-landed worktree. Archive failure preserves the landing result.
