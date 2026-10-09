# twinlane

Run Claude Code and Codex together on your included subscriptions: one usage meter, failover before a limit hits, background workers with receipts.

![twinlane board](docs/demo.svg)

## Why

Two subscriptions have two independent limit clocks. When one fills mid-task, rebuilding the context in the other can cost hours. twinlane keeps a small local record of the work, routes new work while allowance remains, and writes a handoff brief before a switch.

## Quickstart

Python 3.9+ and the `claude` and `codex` CLIs are required for live workers. For this source checkout:

```sh
pipx install .
twinlane install              # preview the hook change
twinlane install --yes        # apply it to your Claude settings file, with backup
twinlane status
twinlane route code
twinlane run --kind code --cd ~/projects/app --brief task.md --write --bg
```

Install from Git: `pipx install git+https://github.com/mmelqadi/twinlane`.

## Usage and routing

| Kind | First | Fallback |
| --- | --- | --- |
| `code`, `intel`, `plan`, `review` | Claude Code | Codex CLI |
| `parallel`, `sweep`, `integrate`, `verify` | Codex CLI | Claude Code |

The default thresholds are caution at 75%, prepare a handoff at 80%, stop new nonurgent work at 85%, and hard stop at 100%. An unknown or stale reading is **unknown**, never 0%; it cannot authorize that engine.

### Money guard

Credits and extra usage are checked only when a job starts. A long Codex job can still reach paid credits if your account allows it; twinlane passes no option that enables credits. By default, Claude dispatch is refused while paid extra usage is ON, and Codex dispatch is refused when its reading says it would use credits. Setting `claude.extra_usage_allowed` or `codex.allow_credits` to true in config allows those jobs and prints a warning that charges may apply.

## Workers and receipts

`twinlane run` accepts `--engine`, `--kind`, `--cd`, `--brief`, `--bg`, `--write`, `--name`, `--baton`, `--effort`, and `--timeout`. A code worker can edit its selected worktree; an intel worker gets a read-only profile. Background jobs have an ID, an evidence directory, event stream, final message, usage information where reported, and a receipt. Use `twinlane wait JOB --any`, `twinlane result JOB`, or `twinlane stop JOB`.

Codex uses the installed CLI through `codex exec --json`. `TWINLANE_CODEX_RUN` can name an optional compatible adapter. No adapter is required. Evidence includes `events.jsonl`, `last-message.md`, and `result.txt`.

## Batons, board, and sessions

`twinlane baton new SLUG --goal "..."` starts a local task record. `baton set`, `baton snap`, and `baton show` maintain it. `twinlane handoff SLUG --to codex` writes a brief using the task record and recent evidence. Approvals never transfer with a brief.

`twinlane board --md` is a read-only view of cached usage, routing, live and finished jobs, batons, and linked worktrees. `twinlane sessions` lists open or recent Claude Code and Codex CLI sessions available for forwarding. The demo below is generated from a disposable fake home by `python examples/demo/run.py`:

```text
twinlane board  2026-10-09T13:52:59Z  (read-only; cached meters, 10s old)
== METERS
claude: caution 5h 77% (resets in 1h59m) | 7d 31% (resets in 2d7h) | extra usage off [fixture, 10s old]
codex: ok 5h 42% (resets in 2h59m) | 7d 18% (resets in 3d0h) [fixture, 10s old]
route now: ->claude code intel plan review; ->codex parallel sweep integrate verify
== ATTENTION (2)
! claude CAUTION (77%, stop at 85%)
! 2 finished, not yet read; newest: twinlane result 20261009-135259-codex-b2
== JOBS  running/queued 1
  20261009-135259-codex-c3  codex  code      running    1m 2%t  6.1-sol/?  demo-c3  wt=example-repo
  last finished (2):
  20261009-135259-codex-b2  codex  done        2m ago  6.1-sol/?  demo-b2  (unread)
  20261009-135259-claude-a1  claude done        4m ago  sonnet-5-5/sonnet-5-5  demo-a1  (unread)
== BATONS (1)
  demo  owner=codex  20s  next: Review the latest receipt
== WORKTREES (0 linked, 0 checked, 0 merged)
  none (batons point at no linked worktree)
```

## Configuration

`TWINLANE_HOME` overrides the data directory. Otherwise Windows uses `%LOCALAPPDATA%/twinlane`; macOS and Linux use `$XDG_DATA_HOME/twinlane` or `~/.local/share/twinlane`. The directory holds `state/`, `jobs/`, `batons/`, and `bin/`. `TWINLANE_CONFIG` selects a user JSON file; otherwise twinlane reads `<home>/config.json`. User values merge recursively over the packaged `policy.json`.

`CLAUDE_CONFIG_DIR` and `CODEX_HOME` select the Claude and Codex configuration directories. `TWINLANE_CODEX_BIN` selects the Codex binary for the runner and the meter. `twinlane install` previews the resolved Claude settings path before `--yes` writes it.

The optional adapter named by `TWINLANE_CODEX_RUN` receives `--brief PATH --cd DIR --tier TIER --out DIR --timeout SECONDS`, optional `--model MODEL --effort EFFORT`, and `--write` for a code job. It receives `TWINLANE_CODEX_ADAPTER_STRICT_CREDITS=1` and `TWINLANE_CODEX_BIN` when available. It must write `<out>/events.jsonl`, `<out>/last-message.md`, and `<out>/result.txt`; it must exit nonzero on refusal or failure. The strict credits variable asks the adapter to refuse paid credits. The packaged runner does not need this adapter.

Add local deny rules in your own config, for example:

```json
{"claude": {"deny": ["Bash(my-release-command:*)", "PowerShell(my-release-command*)"]}}
```

Arrays replace the packaged array; include the generic rules you still need. Do not put credentials in the config file.

## Platform and security

Windows has detached background workers and process-tree stop support. POSIX workers detach with a new session and stop their process group. Windows job-object details have no POSIX equivalent. The Codex app-server meter currently finds only the Windows Codex desktop app. It copies a complete app-server build into `<home>/bin`, keeping at most 3 copies of about 434 MB each (about 1.3 GB total). On Linux/macOS, set `TWINLANE_CODEX_BIN` or rely on rollout files. Otherwise the Codex meter reads **unknown**, and parallel/sweep routes fail safe to Claude or refuse.

`twinlane install` registers an advisory hook for interactive sessions. The blocking guard only acts inside twinlane-dispatched workers; interactive sessions are NOT guarded. The guard is a heuristic over shell text, not a sandbox. The meter reads local CLI credentials and vendor usage information. Its Claude endpoint is undocumented and may change; redirects are refused so the token stays on the original host. No API key router is used.

## What this is not

twinlane is not an API-key router or a multi-model chat UI. It coordinates two installed CLIs and stores evidence locally.

## Related work

- `amElnagdy/delegate-skills`: discovery and setup for 17 CLIs.
- Claude/Codex orchestrator, delegate, and handoff projects: single-direction delegation or file handoffs.
- `ccusage`: Claude token and cost reports.

## Tests and license

Run `python -m unittest discover -s tests -v`. Tests use fixtures and temporary homes; they do not call vendor networks. MIT licensed; see [LICENSE](LICENSE).
