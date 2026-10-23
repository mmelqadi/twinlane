# Writer admission explanation (source candidate)

`twinlane explain-writer-admission --cwd DIR [--job-id ID] [--json]` explains the recorded writer-directory check. This candidate is not installed. A source review or a synthetic test does not authorize observing existing jobs, reserving a writer, recovery or a retry.

The command dispatches before directory initialization. It creates no directories or lock files, records no collection marker, and never reserves, releases, kills or cleans up a job. It does not call the lifecycle/status helpers that can delete a child-starting marker.

## Data flow

The collector canonicalizes the requested directory using the same rule as the claimant. It reads its reservation and job metadata, captures process observations, then reads the metadata again. Directory entries, file hashes, read failures and canonical directory identities contribute to the snapshot comparison. A changed or unreadable snapshot cannot establish admission. Two equal reads are evidence from an interval, not an atomic lock or a guarantee that no transient change occurred.

The evaluator accepts that captured snapshot and performs no filesystem or native calls. It reports the reservation, relevant jobs and uncertainty, including legacy queued writers, versioned queued writers without an attempt, runner identity, child identity, survivors and pending launches. A completed receipt cannot outweigh live engine evidence. A proven-dead pending-launch marker remains cleanup-dependent uncertainty because the real claimant's deletion can fail.

A `done` or `failed` receipt with evidence that the runner observed the recorded child's exit can resolve an unreadable child creation stamp as PID reuse when the runner is DEAD. Both receipts require a nonempty attempts list whose final attempt is `done` or `failed`, has an integer exit code matching the receipt, a nonempty post-wait `events_path`, and no survivor. Its integer `n` must equal `status.json`'s recorded `attempt`; missing attempt numbers do not qualify. Attempt and status engines must match when both are present. A `done` receipt must have exit code zero. The exception requires an existing process with unreadable identity, valid recorded identities, and no survivor or launch marker. Unknown existence, a matching live child, stopped/orphaned receipts, and missing exit evidence continue to block. This evidence covers the recorded child only; descendants are covered by the survivor marker, not by that child's exit. A terminal receipt with a DEAD runner and a provably DEAD child still requires no attempt exit evidence.

Observation commands never repair files. Claim/wait/result/stop paths recheck before repairing unfinished status records and distinguish `repaired`, `already-final`, and `rejected`. Writer admission holds `writers.lock` and refuses when the recheck under `status.lock` rejects; an unsuccessful repair never clears the directory. Wait/result/stop do not report a rejected repair as successful completion.

Every report sets `proposal_only: true` and `grants_admission: false`. Decisions are `blocked`, `unknown` or `no-observed-blocker`. The last means only that the captured evidence supplied no blocker. The real claimant checks again under its own writer lock and current process token, including its own identity before reservation. No equivalence to an earlier launcher's token or environment is asserted.

## What is observed, and what an observation must prove

The collector records `--job-id` in the first snapshot before deriving identities. It observes only identities the real check can consult:

- the reservation owner, when the reservation has the shape the claimant accepts;
- that owner job's engine evidence (recorded child, qualified launch marker, survivor), whatever the owner job's directory, and even when the owner is the `--job-id` job, because the reservation check looks the owner job up by id;
- the runner and engine evidence of same-canonical-directory code jobs other than `--job-id`.

Unrelated directories, other profiles, unpublished folders and unreadable or invalid specifications are never probed. A malformed identity (bool, float, string, out-of-range pid or stamp) is never probed and is reported unknown.

An observation is trusted only when it is an object with a valid state and echoes exactly the requested identity: `pid` as a real integer equal to the requested pid, and `expected_created` as the exact decimal string of the requested stamp (or `null` when no stamp was requested). A missing echo is never filled in from the request. A missing, non-object, invalid-state, wrong-pid, bool-pid, wrong-stamp or wrongly typed response is unknown, never `no-observed-blocker`. Observer-supplied text (reason, observer, stage and error names, actual stamp) is allowlisted to short words or decimal digits; anything else is dropped.

## Identifiers and inputs

Job-folder names stay raw inside the snapshot for lookups. On output (`jobs[].id`, `unclassified[].id`, race labels, `target_job_id`, human text) a name that is not a plain id is replaced by a deterministic `unsafe-<sha256 prefix>` placeholder. Race labels are built from structured race records, and unrecognized records render as `unrecognized`. The human output escapes control characters in the requested directory.

`--cwd` that is empty or contains NUL is a usage error (exit 2) before any path call. A directory that cannot be canonicalized is also a usage error, without a traceback.

`runner.pid` tokens longer than a DWORD pid (10 digits) or a uint64 stamp (20 digits) are malformed and are never converted. A relevant record then reports `runner_pid_malformed` (unknown). In-range-length but out-of-range values are invalid identities (unknown, never probed). Unrelated jobs' runner files are read and decoded in the snapshot but their numeric tokens are not converted. JSON integer literals beyond an enabled Python conversion limit make that file invalid, exactly as `read_json` reads it; versions or configurations accepting the integer instead produce an invalid-identity UNKNOWN. Python's integer limit is not changed. The real claimant can still raise on such a `runner.pid`; this explanation does not claim parity there.

A JSON decoder nesting-limit failure (`RecursionError`) is unreadable evidence. Relevant records remain uncertain; an unrelated job's auxiliary file cannot abort the report. Decoder nesting behavior varies across supported Python versions, so tests inject this failure for a deeply nested document while decoding the other fixture files normally. The host decoder accepted the original nested document; no host crash is claimed.

## Windows observations

Unexpected OpenProcess failures and failed exit-code queries preserve unknown existence. Errors are captured immediately before another API call can overwrite them. Opened handles are closed in a finally block. Qualified absence, a successfully read exit code or a readable creation mismatch can establish DEAD. An unreadable identity cannot establish a matching identity. The legacy tuple helper preserves its return shape, adding `None` for unknown existence; consumers handle that conservatively.

Accepted conservative contract (unchanged logic, clarified in docstrings): an unexpected OpenProcess error, or a failed or raising exit-code query, is unknown with or without a recorded stamp. OpenProcess access denied (5), or failed process times after STILL_ACTIVE, means the process exists but its identity is unreadable: unknown against a recorded stamp, alive when no stamp was recorded. That stampless "alive" still blocks a writer and never claims a matching identity or context equivalence.

The age bound in `_holder_alive` is a legacy policy for Codex binary-copy leases and staging folders only. Writer reservations are never evicted by age: an unknown or unreadable owner keeps holding the directory until it is proven gone.

The structured observer validates recorded identities before probing. Its output uses an allowlist and exact decimal creation-stamp strings, avoiding FILETIME rounding above 2**53. It reports API stages and errors, never environment variables, tokens, briefs or arbitrary job payloads. Observations belong to the current observer at that moment and cannot grant a later claimant admission.

## Verification and boundaries

Tests use fake kernels, disposable stores and injected process evidence. They cover reservation and legacy-scan decisions, malformed records, snapshot races, child/survivor disagreement, receipt limitations, cleanup-dependent uncertainty and parity with the real claimant where cleanup is not required. Command tests check unchanged bytes, modification times and directory listings and tripwire mutating helpers. Self-observation tests concern the test process only.

The original suite bootstrap removes TWINLANE_ENGINE_JOB. Such results do not qualify preserved-context acceptance. The lead's focused fixture preserves all inherited engine variables and runs the candidate tests unchanged. Initial focused verification: 60 tests passed; the lead had no inherited engine-job context and manufactured none.

A later source-correction continuation (observation identity echo, narrowed probes, safe identifiers, NUL/malformed `--cwd`, bounded `runner.pid` parsing, docstring clarification and one additive fake-kernel test) was written with Read/Grep/Edit/Write only and reported zero denials. Lead verification of the corrected candidate passed 84 tests in 53.427 seconds: 16 fake-kernel/strict-identity tests, 50 writer tests, 10 command tests and 8 existing process regressions. It preserved all inherited engine variables; none were present in the lead and none were manufactured. The corrected tests use synthetic stores, injected observers, fabricated snapshots for control-character names that Windows cannot create, and fake kernels. Existing process regressions use newly created owned test children; no existing jobs were observed.

Final parser-depth revision: two injected decoder-failure cases first raised uncaught errors, then passed after `RecursionError` became unreadable evidence. The complete focused selection passed 86 tests in 18.854 seconds (16 fake-kernel/strict identity, 52 writer, 10 command, 8 owned process regressions). The host decoder accepted the original raw nested input; these injected failures qualify the exception boundary, not a reproduced host crash. The large-integer test accepts either BLOCKED or UNKNOWN depending on decoder configuration while requiring no admission and no process probe.

An implementation-worker full-suite command with a temporary log outside the worktree was refused by the guard and was not replayed. The full suite remains NOT_RUN. This separate documentation action does not execute that command or create its denied log. Historical held candidates and their refusal history remain unchanged. Installation, existing-job native observation and retries require separate scope.
