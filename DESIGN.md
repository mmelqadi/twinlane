# Design

twinlane keeps its state under a configurable data directory. `state/` stores cached usage and locks, `jobs/` stores per-attempt inputs and receipts, `batons/` stores task context, and `bin/` is reserved for local helpers. The packaged policy is merged with a user JSON override at read time.

`meter.py` normalizes Claude OAuth usage and Codex app-server limits. Each reading carries source and age; missing evidence remains unknown. `route.py` is a pure decision function over a task kind, meter snapshot, and policy. `jobs.py` launches workers, records JSON events and final text, and tries the other engine only after a usage-limit result. `codex_runner.py` calls `codex exec --json` directly and writes the adapter evidence contract.

`baton.py` records goals, worktree state, decisions, holds, and next actions. `board.py` reads cached records without starting workers. `sessions.py` discovers vendor session files for forwarding. `hook.py` warns at the limit boundary; `guard.py` checks tool calls inside Claude workers. Hook failures leave the user's prompt usable, so the worker's permission profile and the Codex sandbox remain necessary.

Exit codes: 0 success, 2 command usage, 3 refused, 4 still running, 5 failed, 6 usage limit, 127 missing executable.
