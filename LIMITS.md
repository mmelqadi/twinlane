# Limits

- Claude's OAuth usage endpoint is undocumented and may change. The meter sends the locally stored token only to `api.anthropic.com` and refuses redirects. Unknown or stale readings block dispatch for that engine.
- Codex usage comes from its app-server interface and local session evidence. These formats may change; unknown readings stay unknown.
- Served-model reporting differs by vendor. A Codex receipt can only show the requested model when the CLI does not report the served one.
- Windows has the most complete detach behaviour. POSIX uses a new process session and process-group termination; Windows job-object details are unavailable there.
- The Codex app-server meter currently finds only the Windows Codex desktop app. It copies the app-server build into `<home>/bin`, keeping at most 3 copies of about 434 MB each (about 1.3 GB total). On Linux/macOS, set `TWINLANE_CODEX_BIN` or rely on rollout files; otherwise the Codex meter reads **unknown**, and parallel/sweep routes fail safe to Claude or refuse.
- `twinlane install` registers an advisory hook for interactive sessions. The blocking guard only acts inside twinlane-dispatched workers; interactive sessions are NOT guarded. The guard is a heuristic over shell text, not a sandbox. Code workers must still run in worktrees with the vendor's own permissions and sandbox.
- Built and tested against the `codex exec --json` interface (flags `--json --model --config model_reasoning_effort=... --sandbox --cd --output-last-message`); older Codex CLI releases may not accept current model names; run `codex --version` and set your models in config.
- Model ids are defaults as of 2026-10; set your own in config.
- Credits and extra usage are checked only when a job starts. A long Codex job can still reach paid credits if your account allows it; twinlane passes no option that enables credits. Check the receipt and meter before continuing.
