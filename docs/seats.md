# Warm Codex review seats: qualification hold

Experimental, OFF by default. This change takes the brief's **design note plus
lock/status scaffolding** fallback. It does not implement or claim a working warm
runner. Enabling `seats.enabled` makes the experimental command visible in help;
it does not lift the execution hold.

## What the protocol establishes

The [official app-server lifecycle](https://developers.openai.com/codex/app-server)
supports `thread/start` for a fresh conversation on an initialized connection,
followed by `turn/start`. Turn completion has an explicit outcome; cancellation
is a request followed by a completion event. The protocol also has background
terminal management and permits per-turn permission overrides. These facts
establish fresh-thread capability, **not safe reuse under Twinlane custody**.
Checked 2026-10-26; no account-backed turn or benchmark was run.

## Why execution stops here

The inspected base is `a0367bc`. Relevant existing boundaries:

* `codex_runner.command` selects the cold process's read-only sandbox; its `run`
  owns that child's stdin, event stream and exit.
* `meter.app_server_exchange` performs only initialization and a rate-limit read,
  then stops its server. `_await_id` discards unrelated notifications. Reusing it
  for jobs would lose evidence, including tool events, rather than preserve it.
* `jobs._run_codex` constructs `worker_env(..., job_id)` before process launch,
  qualifies/leases the binary, publishes child-start custody, contains the
  engine, and drains its attempt process tree. `_engine_state` observes the
  recorded child and survivors. A long-lived server has a different lifetime
  from that attempt, and keeps its process environment across fresh threads.

There is currently no qualified bridge from a shared server's thread to each
job's guard context, durable launch markers and descendant cleanup. A client can
lose its reply after sending `turn/start`, and absence of received tool events
does not prove the server did nothing. A thread result also cannot be substituted
for DEAD process evidence. Simply treating the warm server as the attempt child
would either hold every completed job forever or release custody while that
server/tools may still run. Neither is acceptable.

This is an integration/qualification blocker, not a claim that app-server lacks
fresh conversations. No schema or fake is presented as proof that the installed
binary safely supports this worker lifecycle. The cold runner and its admission,
archive, writer, retry, billing and guard mechanisms are unchanged.

## Delivered scaffold

* Packaged policy: `"seats": {"enabled": false}`. Only literal `true` opts in.
* `twinlane seats status [--json]` observes `state/seats/<n>.json` without making
  directories, taking locks, reclaiming records, refreshing meters or starting an
  engine. It explicitly says runtime readiness is false.
* `seats.claim(n, pid, pid_created)` is a reservation primitive for a **future
  qualified supervisor**, not an engine launcher. A positive, readable process
  identity is required. The record contains `pid`, `pid_created`, `started_at`.
  Mutation uses `common.file_lock` on `<n>.json.lock`; the durable JSON reservation
  remains exclusive after that short OS lock closes. Even the same ALIVE owner
  cannot claim a second time. Only a DEAD prior identity permits replacement.
* `twinlane seats stop [--dry-run] [--json]` only reaps reservations rechecked DEAD
  under that same OS lock. It kills nothing and retains ALIVE/UNKNOWN records,
  returning exit 4 when reservations remain. Malformed, unstamped and unreadable
  identities remain UNKNOWN. There is no TTL or force override. This is not a
  general-purpose process-tree cleanup primitive.
* `twinlane seats start N` and non-dry `bench` refuse (exit 3), including when
  enabled. `bench --n K --dry-run` prints a prospective JSON plan with 2K model
  calls, zero launches/writes and `measurements: null`. It writes no comparison
  and reports no synthetic speed or failure result. Existing `run --seat` remains
  unsupported; it cannot silently take the cold path or bypass admission.

No daemon, app-server request, real seat claim, lease, live job/state/baton write,
configuration change or account reset is performed by this scaffolding's CLI.
Only the explicit DEAD-reservation cleanup command can mutate seat state.

## Evidence required before implementing execution

1. Qualify the selected binary's schema offline and retain the exact identity.
   Confirm fresh threads, explicit read-only/never-approve settings and effective
   permissions; deny MCP, write-capable tools, external processes and permission
   overrides in the mechanism. Reject unsupported or refused policies.
2. Design an enforced per-job guard context that cannot inherit a previous job's
   identity/authority from the server environment. Keep one active job per seat;
   a fresh thread must contain no prior conversation, tools or instruction drift.
3. Persist each accepted job's seat/server identities and request phase **before**
   sending work. Teach status, continue, stop, archive, writer/slot custody and
   crash recovery about that obligation. A dead client is not a dead engine;
   UNKNOWN must preserve all affected reservations and admitted requests.
4. Attribute every event to thread/turn/request, including malformed messages,
   out-of-order replies, server requests, disconnects and tool children. Require
   an explicit successful terminal outcome plus proven descendant cleanup before
   reuse. An uncertain job ends `transient_start`, never `done`. A reservation
   still needs DEAD before process-based reclamation.
5. Permit cold fallback only with positive no-work proof (for example, work was
   never sent and there are no tool events). A dropped response or empty observed
   stream after sending work is insufficient. Do not rerun an admitted request
   based on a timeout, policy refusal or a missing receipt.
6. Reuse normal admission, fresh included-allowance checks, binary qualification,
   slot limits and read-only kind/profile validation **for every job**; only
   `review`/`intel`, explicit `--seat`, and enabled policy may enter. No credits,
   free reset, API fallback or policy-refusal bypass. Receipts retain the cold
   contract plus seat identity, fresh thread, correlated events and start latency.

The fake-backed execution qualification must include two sequential distinct
threads, concurrent requests, client/server crashes at every dispatch boundary,
unreadable process identity, late tool events, permission refusals, cancellation,
surviving descendants, archive during work and no-work-only cold fallback.

## Future benchmark

After those proofs and lead review, `bench --n K` should measure identical tiny
prompts with the same model, effort, cwd, policy and read-only sandbox. Alternate
cold/seat order; report seat startup separately from per-job dispatch-to-start
latency, raw samples and failure counts/types. Preserve missing/unknown samples.
Count the seat initialization cost rather than hiding it. Write a JSON comparison
only from actual receipts. This scaffold's dry-run plan is not that comparison.

Run from `tests/`:

```text
python -m unittest test_seats test_channels test_jobs
```

`test_support.py` selects a disposable home before imports. Seat tests inject
process liveness and forbid subprocess launch; no real Codex account is used.
The lead's next step is to review this hold and choose the per-job guard/custody
bridge before commissioning a runner or a real benchmark.
