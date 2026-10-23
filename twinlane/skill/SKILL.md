---
name: twinlane
description: >-
  Run Claude Code and Codex together with the `twinlane` CLI: usage meters for both engines,
  routing by task kind, profile-scoped background workers, and engine-neutral handoff batons.
  Use when the user mentions a usage limit, quota, "running out", "which engine", a handoff,
  taking over from Claude or Codex, delegating to Claude or Codex, or parallel workers; at the
  start of any substantive task; and whenever the twinlane hook warns that an engine is at
  handoff, stop or out.
---

# twinlane

`twinlane` coordinates two installed CLIs (Claude Code and Codex) on their included subscriptions.
It reads both usage meters, picks an engine for each kind of task, runs workers with evidence and
receipts, and keeps a handoff record (a baton) so either engine can continue the other's work.
It never uses an API key and never enables paid extra usage or credits.

If the `twinlane` command is not on PATH, run `twinlane install --alias twinlane` to preview a shim
(add `--yes` to write it), or call `python -m twinlane`.

## 1. At the start of a substantive task

```sh
twinlane status                # both meters, running jobs and batons on one screen
twinlane route code            # engine, model and effort for this kind; exit 3 = refused
```

Task kinds: `code intel plan review` go to Claude Code first; `parallel sweep integrate verify` go to
Codex first. An engine reading is one of `ok`, `caution` (about 75%), `handoff` (about 80%: write the
baton now), `stop` (about 85%: no new non-urgent work), `out`, or `unknown`. `unknown` is never treated
as OK. A refusal names the usage window that caused it.

If a reading is `unknown`, follow the reason shown by `twinlane meter` before starting work.
`twinlane meter --fresh` rereads usage; it does not renew a login or make a model call.

## 2. See everything

```sh
twinlane board --md            # one read-only screen: meters, routes, attention, jobs, batons, worktrees
twinlane board --all --json    # no caps; --watch 30 redraws in a terminal pane
twinlane sessions --md         # open or recent Claude Code and Codex CLI sessions
```

Paste the board's attention list and job table into the reply so the user can see the state in the chat.

## 3. Dispatch a worker

```sh
twinlane run --kind intel --cd DIR --brief brief.md                  # read-only worker, waits for the result
twinlane run --kind code --cd WORKTREE --brief brief.md --write --bg # code worker in a linked git worktree
twinlane wait JOB --any        # block until a job finishes (or --timeout S)
twinlane result JOB            # final message and receipt; marks the job as read
twinlane jobs                  # list jobs
twinlane stop JOB              # stop one job
```

- Write the brief as if the worker sees nothing of this conversation: goal, files, constraints, the
  checks to run, and what to report. Name what it must NOT do.
- A code worker needs a linked git worktree (`git worktree add`), never the main checkout.
- Workers do not commit, push, deploy or send anything outward. Review the diff yourself, rerun the
  checks, and commit by path.
- A job that ends `lost` right after a sleep, shutdown or sign-out can be continued with
  `twinlane run --continue-job JOB`.
- Never run `twinlane run`, `twinlane land` or other mutating twinlane commands from inside a worker.

## 4. Keep one baton per task

```sh
twinlane baton new SLUG --goal "..." --cd WORKTREE --accept "A1: ..." --accept "A2: ..."
twinlane baton set SLUG --next "ONE runnable step" --done "..." --decision "..."
twinlane baton snap SLUG       # refresh git state, live jobs and meters (no model call)
twinlane baton show SLUG
```

Update the baton at every milestone. `snap` is cheap; run it before any risky step. Approvals the user
gave to one engine do not transfer to the other.

## 5. Hand a task to the other engine

When an engine reaches `handoff` or `stop`, write the baton, then:

```sh
twinlane handoff SLUG --to codex     # or --to claude; writes a brief from the baton and recent evidence
twinlane handoff --session ID --to claude --out brief.md
```

The receiving session reads the brief, re-checks git state, and continues. It starts from the baton's
`next` action and the recorded evidence, not from memory.

## 6. Other commands

```sh
twinlane run --best-of claude,codex --brief brief.md --cd DIR   # same brief on several engines
twinlane compare GROUP --wait                                    # compare a --best-of group
twinlane land status | next | run | release                      # land finished worktree branches one at a time
twinlane ship plan | run                                         # release manifest for landed work
twinlane wt doctor | prune                                       # inspect and prune linked worktrees
twinlane resume --all-lost                                       # restart jobs lost to a host restart
```

Commands that change a repository or a release (`land run`, `ship run`, `wt prune`, `resume`) preview
by default and need an explicit confirmation flag or the user's go-ahead. Run `twinlane --help` for the
full list in your installed version.

## Rules

- Do not spend paid credits or extra usage. If a reading shows them on or unknown, stop and tell the user.
- Treat a worker's "done" as a claim. Open the evidence (`twinlane result JOB`), rerun the checks, and
  check the diff before saying it is finished.
- The guard that blocks risky shell commands only acts inside twinlane workers; it is a heuristic, not a
  sandbox. Interactive sessions are not guarded.

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
