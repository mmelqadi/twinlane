# Design

twinlane keeps its state under a configurable data directory. `state/` stores cached usage and locks, `jobs/` stores per-attempt inputs and receipts, `batons/` stores task context, and `bin/` is reserved for local helpers. The packaged policy is merged with a user JSON override at read time.

`meter.py` normalizes Claude OAuth usage and Codex app-server limits. Each reading carries source and age; missing evidence remains unknown. `route.py` is a pure decision function over a task kind, meter snapshot, and policy. `jobs.py` launches workers, records JSON events and final text, and tries the other engine only after a usage-limit result. `codex_runner.py` calls `codex exec --json` directly and writes the adapter evidence contract.

`baton.py` records goals, worktree state, decisions, holds, and next actions. `board.py` reads cached records without starting workers. `sessions.py` discovers vendor session files for forwarding. `hook.py` warns at the limit boundary; `guard.py` checks tool calls inside Claude workers. The advisory hook fails open, so a hook failure leaves the user's prompt usable. The guard fails closed inside a job: if it cannot load it exits 2 (a blocking PreToolUse result), and a scan that cannot finish denies. It is a heuristic over command text, not a sandbox, and an obfuscated command can still pass, so the worker's permission profile and the Codex sandbox remain necessary.

Liveness is three-valued: `common.pid_state` returns alive, dead or unknown, and a process whose creation time cannot be read is unknown, never lost. A job keeps a `child-starting.json` marker from just before its engine starts until the engine is reaped, and a `survivor.json` when an engine outlives its kill, so a crash between those writes and the receipt still reads `orphaned` or `unknown`. On Windows, `--bg` runners start through WMI with the caller's environment handed over in an owner-only file, and `--bg` is refused when that start is impossible; POSIX runners use a new session.

`jobs.codex_credit_refusal` runs before every Codex attempt and refuses paid credits for every model unless `codex.allow_credits` is true; a write job also reserves its worktree so two writers cannot share one.

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
