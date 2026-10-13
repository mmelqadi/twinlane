# Operating method

Keep one lead for a task. The lead writes a small goal and owns the worktree, chooses a task kind, and checks the board before dispatch. Workers on either engine return evidence and changes; the lead reviews the diff and runs the final checks.

Use a read-only worker for investigation and a code worker only where edits are intended. Cross-vendor review is useful for risky changes and disagreements. Stop when the evidence converges on the stated goal; do not repeat a completed review just to alternate vendors.

At 75% usage, narrow new work; between 75% and 85%, `twinlane route` sends new work to the other engine when it is below 75% and usable. At 80%, update the baton and prepare a handoff brief. At 85%, stop new nonurgent work on that engine and move it to the other engine if its reading is usable. Never treat unknown usage as free allowance, turn on paid extra usage, or spend Codex credits. A handoff transfers context, not an approval for live actions.

## Lead and integrator lanes

Workers do not land or ship. Each worker finishes in its own worktree on its own branch and reports evidence; the lead reviews the diff and reruns the project's gate itself rather than trusting a worker's "done".

One session is the integrator for a repository. It enqueues finished branches with `twinlane land enqueue SLUG`, checks `twinlane land status`, and lands them one at a time with `twinlane land run`, after a `--dry-run` if anything looks off. Because the gate runs on the tree that will land and a landing is fast-forward only, a branch that falls behind goes back to its worker or is rebased with `--rebase` when its range is linear. Two integrators never run at once: the lock refuses the second, and a stale lock is broken only when its holder is provably dead, with a reason on record.

Release is a separate, targeted step. After landing, `twinlane ship plan SLUG` writes a manifest of exactly that landing's files; read its foreign and superseded flags before passing `--allow-foreign` or `--allow-superseded`. Run `twinlane ship run --manifest PATH` as a dry run, review it, and only then `--execute --confirm CODE`. Ship the landing you reviewed, not everything on the branch. Nothing in this lane needs a worker to hold deploy credentials.

Keep the tree tidy at task boundaries: `twinlane wt doctor`, then `twinlane wt prune` as a dry run before `--yes`. After a reboot or an app quit, `twinlane resume` lists lost jobs; continue one only if its work is still wanted. Use `run --best-of` and `compare` for a decision that deserves two independent attempts, and accept that it costs the usage of both.
