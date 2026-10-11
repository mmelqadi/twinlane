# Design

twinlane keeps its state under a configurable data directory. `state/` stores cached usage and locks, `jobs/` stores per-attempt inputs and receipts, `batons/` stores task context, and `bin/` is reserved for local helpers. The packaged policy is merged with a user JSON override at read time.

`meter.py` normalizes Claude OAuth usage and Codex app-server limits. Each reading carries source and age; missing evidence remains unknown. `route.py` is a pure decision function over a task kind, meter snapshot, and policy. `jobs.py` launches workers, records JSON events and final text, and tries the other engine only after a usage-limit result. `codex_runner.py` calls `codex exec --json` directly and writes the adapter evidence contract.

`baton.py` records goals, worktree state, decisions, holds, and next actions. `board.py` reads cached records without starting workers. `sessions.py` discovers vendor session files for forwarding. `hook.py` warns at the limit boundary; `guard.py` checks tool calls inside Claude workers. The advisory hook fails open, so a hook failure leaves the user's prompt usable. The guard fails closed inside a job: if it cannot load it exits 2 (a blocking PreToolUse result), and a scan that cannot finish denies. It is a heuristic over command text, not a sandbox, and an obfuscated command can still pass, so the worker's permission profile and the Codex sandbox remain necessary.

Liveness is three-valued: `common.pid_state` returns alive, dead or unknown, and a process whose creation time cannot be read is unknown, never lost. A job keeps a `child-starting.json` marker from just before its engine starts until the engine is reaped, and a `survivor.json` when an engine outlives its kill, so a crash between those writes and the receipt still reads `orphaned` or `unknown`. On Windows, `--bg` runners start through WMI with the caller's environment handed over in an owner-only file, and `--bg` is refused when that start is impossible; POSIX runners use a new session.

`jobs.codex_credit_refusal` runs before every Codex attempt and refuses paid credits for every model unless `codex.allow_credits` is true; a write job also reserves its worktree so two writers cannot share one.

Exit codes: 0 success, 2 command usage, 3 refused, 4 still running, 5 failed, 6 usage limit, 127 missing executable.
