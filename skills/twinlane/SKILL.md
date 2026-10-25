---
name: twinlane
description: >-
  Run Claude Code and Codex together with the `twinlane` CLI: one board for both engines' usage, routing by
  task kind, background workers with receipts, handoff batons, a land queue and worktree health. Use when
  the user mentions a usage limit, quota or "running out", asks which engine should do a task, wants to
  delegate to Claude or Codex, run parallel workers, hand a task to the other engine, or land worktree branches.
---

# twinlane

`twinlane` is a local CLI. Every command takes `--help`. Read-only commands (`board`, `meter`, `route`,
`jobs`, `result`, `baton show`, `wt doctor`, `ship plan`) are always safe to run.

## Pick the command

| I want to | Run |
|---|---|
| See usage, jobs and batons for both engines | `twinlane board --md` |
| Get fresh usage readings | `twinlane meter --fresh --json` |
| Ask which engine takes a task | `twinlane route KIND` |
| Run a read-only worker | `twinlane run --kind intel --cd DIR --brief F --bg` |
| Run a worker that edits code | `twinlane run --kind code --write --cd LINKED_WORKTREE --brief F --bg` |
| Wait for workers | `twinlane wait JOB1 JOB2 --any --timeout 60` |
| Read a worker's result | `twinlane result JOB` (`--json` for the receipt) |
| Keep task state | `twinlane baton new SLUG --goal "..."`, `twinlane baton set SLUG --next "..."` |
| Hand the task to the other engine | `twinlane handoff SLUG --to codex` (or `--to claude`) |
| Land a finished branch | `twinlane land enqueue SLUG`, then `twinlane land run SLUG` |
| Check worktrees | `twinlane wt doctor` |
| Continue jobs lost to a reboot | `twinlane resume` |

Kinds: `code`, `intel`, `plan`, `review` go to Claude first; `parallel`, `sweep`, `integrate`, `verify` go to
Codex first. Usage status per engine: `ok` below 75%, `caution`, `handoff` at 80% (write the baton now),
`stop` at 85% (no new non-urgent work), `out` at 100%, `unknown` (never treated as ok).

## Before you dispatch

1. Run `twinlane meter --fresh --json`. Dispatch trusts only a reading younger than 120 seconds; the board
   can show older ones.
2. For a refresh with no side effects beyond the cache, set `TWINLANE_CODEX_BIN` to a Codex binary outside
   the desktop app's folders and add `--no-rollout`: no binary copy or prune, and no local session files.
3. Check `status` and `reading_at` for both engines, and `codex.source` (`app-server`). If either engine is
   `unknown`, read its `note` and fix that; never dispatch on a guess.
4. Run `twinlane route KIND`. Exit 3 means refused; the reason names the window that caused it.

## Write the brief

```markdown
# Goal
The outcome in one or two sentences.
# Acceptance
- A1: a check with its command and expected result
# Context
- path/to/file:function, and why it matters
# Constraints
- files it may edit; no commits, pushes or deploys
# Report format
What changed, commands run with results, what was not done, next step.
```

twinlane adds the worker contract (no commits, no pushes, report shape) on its own.

## After a worker finishes

A worker's "done" is a claim. Check `twinlane result JOB --json`:

- `status`;
- `touched_files`;
- `read_only_violation`, which must be false for intel and review;
- `limit_hit`;
- `permission_denials`.

Then read the diff and rerun the tests yourself. Workers never commit, push, merge or deploy; you do,
in your own worktree. Statuses `orphaned` and `unknown` mean the engine may still be running: never rerun
those, check them first.

## Exit codes

| code | meaning | do |
|---|---|---|
| 0 | ok | continue |
| 2 | usage error | fix the command |
| 3 | refused by policy | read the reason; do not work around it |
| 4 | still running, or lock held | wait again |
| 5 | failed | `twinlane result JOB --full`; fix the brief, never blind-rerun |
| 6 | engine limit hit | hand off, or let `--engine auto` fail over once |

## Rules

- Never raise thresholds, edit the meter cache, or invent readings to get a dispatch through.
- Approvals never transfer in a handoff: the next engine asks the user again.
- Paid extra usage and Codex credits stay off unless the user turns them on in config.

## Lanes and attention

Use `twinlane board`, `twinlane inbox`, `twinlane wip`, then `twinlane wt prune --merged-only` (preview).
Never blocked: ask one question with `baton set SLUG --ask "Archive these 2 branches?" --record branches --count 2`.
An inbox yes is not a live-write approval. Bind your session with `--bind-session SESSION_ID`.
Batons are never auto-closed: landing marks landed; stale is derived from inactivity, and checkpoint snaps are
not progress. Close explicitly or resume with `--state active`; park with `--state parked --park-for 8h`.
`wip next` prints the retry command, never starts it, and keeps a full queue unless `--force` is given.
Context is a `~` estimate. Use `land card --baton SLUG` before landing; archive defaults to a prune suggestion.
`install --precompact` and `install --lane-state` preview settings diffs; Codex notify configuration is manual.

## Wake the coordinating session

Run `twinlane watch --name lead` in the background while coordinating lanes. Re-arm it after
each wake: collect completed jobs with `twinlane result JOB`, inspect `twinlane board`, and
handle the ranked waits before waiting again. Different watcher names have independent
cursors; delivery is at least once. Watch only polls files and cached meters. Worker jobs
are refused and must return their report instead of watching lead events.

Use `twinlane run --owner SLUG` for an existing baton to record the job dependency and its
completion. Use `baton set SLUG --waiting-on lead|user|job:JOB|lane:SLUG|none`; a user wait
requires a yes/no `--ask`. Inbox answers never grant live-write approval. The capacity line
is weekly-pace advice only; dispatch still follows policy and fresh meter checks.
