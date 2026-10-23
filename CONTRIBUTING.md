# Contributing to twinlane

Thanks for helping. Small, focused changes are the easiest to review.

## Before you start

- For a bug, open an issue with the steps to reproduce it.
- For a new feature, open an issue first so we can agree on the approach.

## Making a change

1. Fork the repository and create a branch from `main`.
2. Make your change and add or update tests for it.
3. Run the test suite locally (see the README) and make sure it passes.
4. Keep the pull request to one topic and describe what it changes and why.

## Running the tests

Run `python -B tests/run_suite.py -j 4` for parallel module execution, or use
`python -B -m unittest discover -s tests -v` for a serial run. The parallel runner
reruns failed tests alone, one at a time, then reruns each affected module once
serially. A test must pass both checks to be labelled `load-sensitive`; a failure
that reproduces in module order stays real. The label is evidence for
investigation, not proof of its cause. Results use a dedicated JSON file;
diagnostic output cannot supply verdicts. Real failures, unresolved loader
entries, import errors and timeouts gate the run. An overall zero-test selection
exits 2 (helper modules with no tests are allowed alongside actual tests). Use
`--strict` to gate on load-sensitive tests too, or `--no-rerun` to gate on every
original failure.

Use repeated `-k SUBSTR` options to select module names, `--module-timeout` and
`--test-timeout` to set limits in seconds (defaults: 1800 and 600), and `--json FILE`
for a machine-readable report. Logs include original attempts, isolated reruns
and serial module reruns; their temporary folder is printed at the end, or set
it with `--log-dir`.
Timeouts terminate the module's process tree. Python faulthandler is enabled;
stack output depends on the platform and the signal causing termination.

On Windows, children start suspended and join a Job Object before running; timeout
cleanup verifies that the job is empty, including when `taskkill` is denied. On
POSIX, cleanup walks the process table and freezes owned descendants before
killing them, including children that detached into another session. Processes
that were already reparented before that snapshot cannot be attributed by this
portable ancestry walk. Cleanup errors or surviving tracked processes remain
timeouts and print a warning; they never turn into a successful run.

## Ground rules

- Never commit secrets, tokens, personal data or machine-specific paths.
- New code should work on Windows, macOS and Linux.
- By contributing you agree that your work is released under the MIT License.
- Everyone taking part follows the [Code of Conduct](CODE_OF_CONDUCT.md).
