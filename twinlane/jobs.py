"""twinlane run / _job / jobs / wait / result / stop (DESIGN.md "jobs.py").

A job is a folder jobs/<YYYYmmdd-HHMMSS>-<engine>-<slug4>/ holding job.json (the spec), brief.md
(contract + brief), status.json (queued/running, pid), the engine's raw output and, at the end,
result.json (the receipt). The runner never reruns a timed-out job; it fails over to the other
engine at most once, and only after a usage-limit hit.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from contextvars import ContextVar, copy_context
import hashlib
import json
import os
import random
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time

from . import common, envhandoff, guard, hostload, observations, watchdog

# Test hooks: meter.py / route.py / baton.py are imported lazily, and tests replace these.
_read_meter = None      # callable(policy) -> meter dict
_decide = None          # callable(kind, meter, policy, urgent, engine) -> decision dict
_record_baton = None    # callable(slug, receipt) -> None
_PRESERVING_METADATA = ContextVar('twinlane_preserving_metadata', default=False)
_CURRENT_JOB = ContextVar('twinlane_current_metadata_job', default=None)
_REAPED_CHILDREN = ContextVar('twinlane_reaped_children', default=None)
_JOB_LOCK_DEADLINE = ContextVar('twinlane_job_lock_deadline', default=None)


@contextmanager
def preserving_metadata(job_id=None):
    """Observations retain other jobs' metadata; only this new job owns its lifecycle files.

    Context-local rather than an environment flag: persist the option in job.json for the
    detached runner, and explicitly copy it into the usage scan thread.
    """
    mode = _PRESERVING_METADATA.set(True)
    owner = _CURRENT_JOB.set(job_id)
    try:
        yield
    finally:
        _CURRENT_JOB.reset(owner)
        _PRESERVING_METADATA.reset(mode)


def _preserve_job(job_id):
    return _PRESERVING_METADATA.get() and job_id != _CURRENT_JOB.get()
CONTAIN = True          # runner joins a kill-on-close job object (tests running in-process turn it off)

POLL_S = 2.0
SLOT_POLL_S = 2.0
CODEX_GRACE_S = 120     # the runner has its own timeout; ours is a backstop
FINAL_LINES = 120
MAX_TOOL_USES = 2000    # tool calls kept per attempt for the after-job policy scan

ENGINES = ('claude', 'codex')
CODEX_TIERS = ('luna', 'sol', 'sol-high', 'astra')  # the Codex runner's --tier names; the policy may add more
MAX_ATTEMPTS = 2  # the runner makes one primary attempt and at most one failover
WRITE_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash', 'PowerShell'}
LIMIT_TEXT = re.compile(r'usage limit|hit your limit|rate limit', re.I)
CODEX_LIMIT_MARKERS = ('usage_limit_exceeded', "You've hit your usage limit", 'rate_limit_reached')
# Only these Codex events can report the run's own limit; item.* events carry tool output and agent
# text, which may quote the markers (a job that greps Codex logs).
CODEX_ERROR_EVENTS = {'error', 'turn.failed', 'thread.error', 'stream_error'}
GUARD_MATCHER = 'Bash|PowerShell|Edit|Write|MultiEdit|NotebookEdit|mcp__.*'
TERMINAL = {'done', 'failed', 'timeout', 'limit_hit', 'refused', 'stopped', 'lost', 'interrupted'}
EXIT_BY_STATUS = {'done': common.EXIT_OK, 'failed': common.EXIT_FAILED, 'timeout': common.EXIT_FAILED,
                  'stopped': common.EXIT_FAILED, 'lost': common.EXIT_FAILED, 'limit_hit': common.EXIT_LIMIT,
                  'refused': common.EXIT_REFUSED, 'interrupted': common.EXIT_FAILED}
QUEUED_WITHOUT_PID_LOST_S = 120
# jobs/<id>/survivor.json: written by the runner when an engine process outlives its kill. A receipt that
# ended cleanly has no such file, so liveness is never probed for it.
SURVIVOR = 'survivor.json'
TREE_UNRESOLVED = 'tree-unresolved.json'  # a stall kill that was not proven complete: the claim stays until a person clears it
CHILD_STARTING = 'child-starting.json'  # durable uncertainty before Popen, until child identity is recorded
KILL_GRACE_S = 15          # how long a killed engine gets to be reaped before it counts as a survivor
SESSION_SCAN_S = 3.0       # wall-clock budget of the --resume session-owner lookup
SESSION_EVENTS_BYTES = 65536  # how much of each events file the --resume lookup reads for session ids
NO_WINDOW = common.CREATE_NO_WINDOW if common.IS_WIN else 0
CALLER_BRIEF = 'caller-brief.md'  # jobs/<id>/: the brief as the caller wrote it, before contract or continue-job context


# ---------------------------------------------------------------- paths / small io

def job_dir(job_id: str) -> str:
    active = os.path.join(common.JOBS, job_id)
    if os.path.isfile(os.path.join(active, 'job.json')):
        return active
    from . import archive, baton
    if baton.JOB_ID_RE.fullmatch(job_id):
        return archive.archived_ids().get(job_id, active)
    return active


def _p(job_id: str, name: str) -> str:
    return os.path.join(job_dir(job_id), name)


def lock_paths(job_id: str, name: str | None = None) -> tuple[str, str | None]:
    """Outer archive coordination, then the canonical pre-upgrade lifecycle lock."""
    return (os.path.join(common.STATE, 'job-locks', job_id + '.archive'), _p(job_id, name) if name else None)


_ARCHIVE_LOCKS = threading.local()


@contextmanager
def archive_lock(job_id: str, timeout_s=30):
    # Reentrant only within this thread. A copied context in another thread must
    # still take the OS lock. Never retain a resolved evidence path across it.
    path, _ = lock_paths(job_id)
    held = getattr(_ARCHIVE_LOCKS, 'held', set())
    if path in held:
        yield
        return
    with common.file_lock(path, timeout_s, wait_until=_JOB_LOCK_DEADLINE.get()):
        _ARCHIVE_LOCKS.held = held | {path}
        try:
            yield
        finally:
            _ARCHIVE_LOCKS.held = held


@contextmanager
def lifecycle_lock(job_id: str, name: str, timeout_s=30):
    with archive_lock(job_id, timeout_s):
        _, inner = lock_paths(job_id, name)
        with common.file_lock(inner, timeout_s, create_parent=False, wait_until=_JOB_LOCK_DEADLINE.get()):
            yield


def _write_job_file(job_id, name, write):
    """Resolve and write without creating evidence directories, even on retry."""
    with archive_lock(job_id):
        for attempt in range(2):
            path = _p(job_id, name)
            try:
                write(path)
                return
            except FileNotFoundError:
                if attempt:
                    raise


def _write_job_json(job_id, name, data):
    _write_job_file(job_id, name, lambda path: common.write_json(path, data, create_parent=False))


def _write_job_text(job_id, name, text):
    _write_job_file(job_id, name, lambda path: common.write_text(path, text, create_parent=False))


def _read_text(path: str) -> str:
    try:
        with open(path, 'rb') as h:
            return decode_text(h.read())
    except OSError:
        return ''


def decode_text(raw: bytes) -> str:
    """UTF-8 (with or without BOM), or UTF-16 when it carries a BOM: PowerShell 5.1's Out-File and
    `>` write UTF-16LE, and decoding that as UTF-8 gives NUL-interleaved text."""
    if raw.startswith((b'\xff\xfe', b'\xfe\xff')):
        return raw.decode('utf-16', errors='replace')
    return raw.decode('utf-8-sig', errors='replace')


def brief_problem(text: str) -> str | None:
    """Why a decoded brief is unusable (wrong encoding), or None."""
    if '\x00' in text:
        return 'it contains NUL characters (UTF-16 without a BOM?); save it as UTF-8'
    bad = text.count('�')
    if bad > max(8, len(text) // 20):
        return f'{bad} undecodable characters; save it as UTF-8'
    return None


_human_age = common.fmt_age


def _other(engine: str) -> str:
    return 'codex' if engine == 'claude' else 'claude'


# ---------------------------------------------------------------- meter / route / baton access

def _meter(policy: dict, fresh: bool = False, auto_refresh: bool = True, admission: bool = True) -> dict:
    """auto_refresh renews an expired Claude token once per expiry (policy.meter.auto_refresh_claude)."""
    if _read_meter is not None:
        return _read_meter(policy)
    from . import meter
    if _PRESERVING_METADATA.get():
        # A fresh read of BOTH accounts can lease/prune Codex binaries even for Claude.
        # Only reuse the ordinary TTL-qualified cache; never renew tokens, run a model,
        # acquire shared binaries or write cache state to obtain admission evidence.
        cached = meter._cached_meter(policy)
        if cached is None:
            raise Refusal(common.EXIT_REFUSED, 'preserving start requires existing fresh meter evidence; no refresh or shared binary-cache acquisition')
        # The envelope TTL is not the lifetime of each underlying allowance reading.
        # _cached_meter recalculates age but does not reclassify its old status. Only
        # downgrade stale/unreadable/future-dated evidence; never invent a fresh read.
        cached = dict(cached)
        now = common.now()
        for engine in ENGINES:
            section = dict(cached[engine])
            stamp = common.parse_time(section.get('reading_at'))
            age = common.age_s(section.get('reading_at'))
            limit = (policy.get(engine) or {}).get('stale_reading_max_age_s', 1800)
            if type(limit) is not int or limit < 0:
                raise Refusal(common.EXIT_REFUSED, f'{engine} freshness policy is invalid')
            if stamp is None or age is None or age > limit or (stamp - now).total_seconds() > meter.FUTURE_SKEW_S:
                section['status'] = 'unknown'
                section['note'] = '; '.join(filter(None, (section.get('note'),
                    'cached underlying allowance is stale, unreadable or future-dated; no refresh performed')))
            cached[engine] = section
        return cached
    return meter.read(fresh=fresh, policy=policy, auto_refresh=auto_refresh, admission=admission)


def _cached_meter() -> dict | None:
    """meter_after: the cached reading only, so a finishing job never probes the network."""
    return common.read_json(os.path.join(common.STATE, 'meter.json'))


def _refresh_notice(meter: dict) -> str | None:
    """The sentence for a read that just renewed the Claude token (None for cached or test meters)."""
    from . import meter as meter_mod
    return meter_mod.refresh_notice(meter) if isinstance(meter, dict) else None


def _dry_run_refresh_note(meter: dict, policy: dict) -> str | None:
    """For --dry-run: the Claude meter is unknown only because the token expired, and a real run would renew it."""
    from . import meter as meter_mod
    c = (meter or {}).get('claude') if isinstance(meter, dict) else None
    c = c if isinstance(c, dict) else {}
    if (c.get('status') == 'unknown' and c.get('token_expired_at') and meter_mod.auto_refresh_enabled(policy)
            and meter_mod.refresh_pending(c['token_expired_at'])):
        blocked = meter_mod.refresh_blocker()
        if blocked:
            return f'dry run: the Claude meter is unknown because the CLI token expired; a real run would NOT refresh it ({blocked})'
        return ('dry run: the Claude meter is unknown because the CLI token expired; a real run would refresh it '
                'once with one tiny Haiku call. This dry run made no model call.')
    return None


def _route(kind: str, meter: dict, policy: dict, urgent: bool, engine: str | None) -> dict:
    if _decide is not None:
        return _decide(kind, meter, policy, urgent, engine)
    from . import route
    return route.decide(kind, meter, policy, urgent=urgent, engine=engine)


def _baton(slug: str | None, receipt: dict) -> str | None:
    """Fail-open: a baton problem never fails the job; the error goes into the receipt."""
    if not slug:
        return None
    try:
        if _record_baton is not None:
            _record_baton(slug, receipt)
        else:
            from . import baton
            baton.record_job(slug, receipt)
    except Exception as e:  # noqa: BLE001 - recorded, never raised
        return f'{type(e).__name__}: {common.clip(e, 200)}'
    return None


# ---------------------------------------------------------------- contract / profiles / argv

def contract(job_id: str, engine: str, profile: str) -> str:
    scope = ('for profile intel do not modify files. ' if profile == 'intel'
             else 'for profile code edit files only inside this worktree. ')
    return (f'You are a delegated worker run by twinlane (job {job_id}, engine {engine}, profile {profile}). '
            'Rules: do NOT git commit, push, merge, rebase or deploy; no live data writes, recipient sends, '
            'or migrations; use dry runs for external actions; never print secrets; '
            + scope +
            'End with a report: what you did, files changed, commands run with results, what was NOT done, '
            'open questions, and the exact next step.')


def compose_brief(job_id: str, engine: str, profile: str, brief: str) -> str:
    return contract(job_id, engine, profile) + '\n\n---\n\n' + brief.rstrip() + '\n'


def resolve_profile(kind: str, engine: str, decision: dict, policy: dict, write: bool) -> str:
    """'code' (writes) or 'intel' (read-only). An explicit --write asks for the write profile."""
    prof = decision.get('profile')
    if prof not in ('code', 'intel'):
        if engine == 'codex':
            prof = 'code' if kind in policy.get('codex', {}).get('write_kinds', ['code', 'integrate']) else 'intel'
        else:
            prof = 'code' if kind == 'code' else 'intel'
    return 'code' if write else prof


def codex_tier_model(model, policy: dict) -> str | None:
    """The tier name when a Codex --model value is one (a tier is passed as --tier, never as a model id)."""
    tiers = set(CODEX_TIERS) | {t for t in policy.get('codex', {}).get('tier_by_kind', {}).values() if isinstance(t, str)}
    name = str(model or '').strip().lower()
    return name if name in tiers else None


def engine_params(engine: str, kind: str, decision: dict, policy: dict, profile: str,
                  model=None, effort=None, tier=None, timeout=None, max_turns=None) -> dict:
    """Concrete model/effort/tier/limits for one attempt. Explicit overrides win over the decision."""
    if engine == 'claude':
        c = policy.get('claude', {})
        return {'engine': 'claude', 'profile': profile, 'tier': None,
                'model': model or decision.get('model') or c.get('models', {}).get('default'),
                'effort': effort or decision.get('effort') or c.get('effort_by_kind', {}).get(kind),
                'max_turns': int(max_turns or c.get('max_turns_by_profile', {}).get(profile) or 150),
                'timeout_s': int(timeout or c.get('timeout_s_by_profile', {}).get(profile) or 3600)}
    x = policy.get('codex', {})
    selected_tier = tier or decision.get('tier') or x.get('tier_by_kind', {}).get(kind) or 'luna'
    return {'engine': 'codex', 'profile': profile,
            'model': model or x.get('models', {}).get(selected_tier),
            'effort': effort or x.get('efforts', {}).get(selected_tier, 'medium'), 'max_turns': None,
            'tier': selected_tier,
            'timeout_s': int(timeout or x.get('timeout_s') or 3600)}


def claude_launcher(binary: str) -> list[str]:
    """A .py bin (the test fake) runs under this interpreter; anything else runs directly."""
    return [sys.executable, binary] if binary.lower().endswith('.py') else [binary]


def claude_args(params: dict, policy: dict, resume: str | None = None, cwd: str | None = None,
                settings_path: str | None = None) -> list[str]:
    c = policy.get('claude', {})
    args = ['-p', '--model', params['model'], '--effort', params['effort'], '--output-format', 'stream-json',
            '--verbose', '--max-turns', str(params['max_turns']), '--permission-prompts', 'none']
    if resume:
        # A supplied session may still be open in an interactive Claude window.
        args += ['--resume', resume, '--fork-session']
    deny = list(c.get('deny', []))
    if params['profile'] == 'code':
        args += ['--permission-mode', c.get('code_permission_mode') or 'acceptEdits',
                 '--allowedTools', *c.get('code_allow', [])]
    else:
        # The guard lives here, not in the policy file: intel never receives a write tool.
        allow = [t for t in c.get('intel_allow', []) if t not in WRITE_TOOLS]
        args += ['--permission-mode', 'dontAsk', '--allowedTools', *allow]
        # Claude merges allow rules from the user's and the project's settings on top of
        # --allowedTools, and a deny rule beats any allow rule. The inherited rules themselves go
        # into the --settings file (intel_settings_deny): hundreds of them overflow the 32 KB
        # Windows command line.
        deny += [r for r in c.get('intel_deny', []) if r not in deny]
    args += ['--disallowedTools', *deny]
    if settings_path:
        args += ['--settings', settings_path]
    return args


def _settings_allow(path: str) -> list[str]:
    try:
        with open(path, encoding='utf-8-sig') as h:
            data = json.load(h)
    except (OSError, ValueError):
        return []
    allow = ((data.get('permissions') or {}).get('allow') or []) if isinstance(data, dict) else []
    return [r for r in allow if isinstance(r, str)]


def inherited_write_rules(cwd: str | None, intel_allow: list[str]) -> list[str]:
    """Allow rules a worker would inherit from user/project/local settings that the read-only
    intel profile must not have (e.g. `Bash(python *)`, browser MCP tools), to pass as deny rules.

    Bare read-only tools (Read, WebFetch(...)) stay allowed. A bare `Bash` allow becomes a bare `Bash`
    deny, which costs intel its git log/diff forms but keeps it read-only."""
    paths = [os.path.join(common.CLAUDE_DIR, 'settings.json'), os.path.join(common.CLAUDE_DIR, 'settings.local.json')]
    roots = []
    if cwd:
        roots.append(cwd)
        rc, top = common.git(cwd, 'rev-parse', '--show-toplevel')
        if rc == 0 and top:
            roots.append(top)
    for root in roots:
        paths += [os.path.join(root, '.claude', 'settings.json'), os.path.join(root, '.claude', 'settings.local.json')]
    out = []
    for p in dict.fromkeys(os.path.normcase(os.path.abspath(x)) for x in paths):
        for rule in _settings_allow(p):
            if not _intel_safe_rule(rule, intel_allow) and rule not in out:
                out.append(rule)
    return out


def _intel_safe_rule(rule: str, intel_allow: list[str]) -> bool:
    """True when an inherited allow rule grants nothing beyond the intel profile."""
    if rule in intel_allow:
        return True
    tool, _, inner = rule.partition('(')
    tool = tool.strip()
    if tool == 'Bash':
        pattern = inner.rstrip(')').replace(':*', '').replace('*', '').strip()
        # Deny-listing `Bash(git status)` would also block intel's own `git status`.
        return bool(pattern) and guard.intel_shell_ok(pattern, intel_allow) is None
    return tool in guard.READ_ONLY_TOOLS


def intel_settings_deny(params: dict, policy: dict, cwd: str | None) -> list[str]:
    """Deny rules for the intel worker's --settings file: every inherited allow rule that grants more
    than the read-only profile (empty for the code profile)."""
    if params.get('profile') == 'code':
        return []
    allow = [t for t in (policy.get('claude') or {}).get('intel_allow', []) if t not in WRITE_TOOLS]
    return inherited_write_rules(cwd, allow)


def guard_settings(job_dir_path: str, deny: list[str] | None = None) -> str:
    """Write the --settings file for this worker: `twinlane guard` as a PreToolUse hook, plus the
    profile's extra deny rules (kept off the command line, which Windows caps at 32 KB)."""
    py = sys.executable.replace('\\', '/')
    command = f'"{py}" "{common.TWINLANE_PY.replace(chr(92), "/")}" guard'
    job_id = os.path.basename(job_dir_path)
    data = {'hooks': {'PreToolUse': [
        {'matcher': GUARD_MATCHER, 'hooks': [{'type': 'command', 'command': command, 'timeout': 15}]}]}}
    if deny:
        data['permissions'] = {'deny': list(deny)}
    _write_job_json(job_id, 'guard-settings.json', data)
    return _p(job_id, 'guard-settings.json')


def worker_env(env: dict, job_id: str) -> dict:
    """`env` with this worker's lineage: TWINLANE_ENGINE_JOB is the new job."""
    env = dict(env)
    env['TWINLANE_ENGINE_JOB'] = job_id
    return env


def claude_env(job_id: str, profile: str | None = None, cwd: str | None = None,
               stripped: list | None = None, extra_secret_patterns: list | None = None) -> dict:
    """The worker's env. A code-profile worker also loses every secret-named variable (and any that match
    policy secrets.extra_env_name_patterns); their NAMES are appended to `stripped` for the receipt."""
    extra = {'TWINLANE_ENGINE_JOB': job_id}
    if profile:
        extra['TWINLANE_ENGINE_PROFILE'] = profile
    if cwd:
        extra['TWINLANE_ENGINE_CWD'] = cwd
    env = worker_env(common.clean_claude_env(extra), job_id)
    if profile == 'code':
        env, names = common.strip_secret_env(env, extra_secret_patterns)
        if stripped is not None:
            stripped.extend(names)
    return env


def codex_launcher() -> list[str]:
    runner = common.CODEX_RUN
    if runner:
        if runner.lower().endswith('.py'):
            return [sys.executable, runner]
        return [common.bash_bin() or 'bash', runner]
    return [sys.executable, os.path.join(os.path.dirname(__file__), 'codex_runner.py')]


def codex_args(params: dict, policy: dict, brief_path: str, cwd: str, out_dir: str) -> list[str]:
    args = ['--brief', brief_path, '--cd', cwd]
    if common.CODEX_RUN:
        args += ['--tier', params['tier']]
    if params.get('model'):
        args += ['--model', params['model']]
    if params.get('effort'):
        args += ['--effort', params['effort']]
    if params['profile'] == 'code':
        args.append('--write')
    if not common.CODEX_RUN:  # an adapter gets codex.exec_config from the environment instead (see _run_codex)
        try:
            for item in common.codex_exec_config(policy):
                args += ['--exec-config', item]
        except ValueError:  # _run_codex refuses the attempt with the reason before anything starts
            pass
    return args + ['--out', out_dir, '--timeout', str(params['timeout_s'])]


def build_argv(params: dict, policy: dict, brief_path: str, cwd: str, out_dir: str,
               resume: str | None = None, settings_path: str | None = None) -> list[str] | None:
    if params['engine'] == 'claude':
        binary = common.claude_bin()
        if not binary:
            return None
        return claude_launcher(binary) + claude_args(params, policy, resume, cwd, settings_path)
    return codex_launcher() + codex_args(params, policy, brief_path, cwd, out_dir)


# ---------------------------------------------------------------- stream parsing

TURNS_NOTE = ('model_calls counts distinct main-loop model responses (subagent_model_calls those of subagents); '
              'num_turns is the Claude CLI result counter, a different unit from the --max-turns limit the CLI '
              'enforces itself. A run the CLI stopped at that limit ends with subtype error_max_turns.')


class ClaudeStream:
    """Incremental parser for `claude -p --output-format stream-json` lines."""

    def __init__(self):
        self.session_id = None
        self.init_model = None
        self.permission_mode = None
        self.tools_count = None
        self.limit_hit = False
        self.result = None
        self.last_text = ''
        self.bad_lines = 0
        self.tool_uses = []
        self.errors = []
        # Model responses, counted by distinct message id: the CLI splits one response across several
        # `assistant` events, one per content block. An event without an id counts once on its own.
        self.main_calls: set = set()
        self.sub_calls: set = set()
        self.unkeyed_main = 0
        self.unkeyed_sub = 0

    def feed(self, line: str) -> None:
        """Never raises: a malformed event is counted, and the caller keeps draining the pipe."""
        try:
            self._feed(line)
        except Exception as e:  # noqa: BLE001 - one odd event must not stop the stdout pump
            self.bad_lines += 1
            if len(self.errors) < 5:
                self.errors.append(f'{type(e).__name__}: {common.clip(e, 160)}')

    def _feed(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError:
            self.bad_lines += 1
            return
        if not isinstance(ev, dict):
            return
        kind = ev.get('type')
        if ev.get('session_id'):
            self.session_id = self.session_id or ev['session_id']
        if kind == 'system' and ev.get('subtype') == 'init':
            self.session_id = ev.get('session_id') or self.session_id
            self.init_model = ev.get('model')
            self.permission_mode = ev.get('permissionMode')
            tools = ev.get('tools')
            self.tools_count = len(tools) if isinstance(tools, list) else None
        elif kind == 'rate_limit_event':
            info = ev.get('rate_limit_info')
            if isinstance(info, dict) and str(info.get('status', '')).lower() == 'rejected':
                self.limit_hit = True
        elif kind == 'assistant':
            self._count_call(ev)
            text = _message_text(ev.get('message'))
            if text:
                self.last_text = text
            self._record_tool_uses(ev.get('message'))
        elif kind == 'result':
            self.result = ev
            if ev.get('session_id'):
                self.session_id = ev['session_id']
            if ev.get('is_error') and LIMIT_TEXT.search(str(ev.get('result') or '')):
                self.limit_hit = True

    def _count_call(self, ev: dict) -> None:
        msg = ev.get('message') if isinstance(ev.get('message'), dict) else {}
        sub = bool(ev.get('parent_tool_use_id'))
        mid = msg.get('id')
        if isinstance(mid, str) and mid:
            (self.sub_calls if sub else self.main_calls).add(mid)
        elif sub:
            self.unkeyed_sub += 1
        else:
            self.unkeyed_main += 1

    def _record_tool_uses(self, msg) -> None:
        for c in _content_items(msg):
            if c.get('type') == 'tool_use' and len(self.tool_uses) < MAX_TOOL_USES:
                inp = c.get('input') if isinstance(c.get('input'), dict) else {}
                keep = {k: common.clip(inp[k], 2000) for k in ('command', 'file_path', 'notebook_path') if k in inp}
                self.tool_uses.append({'id': c.get('id'), 'name': str(c.get('name') or ''), 'input': keep})

    def summary(self) -> dict:
        r = self.result or {}
        usage = r.get('modelUsage')
        served = list(usage) if isinstance(usage, dict) else ([self.init_model] if self.init_model else [])
        denials = r.get('permission_denials')
        denied_ids = {d.get('tool_use_id') for d in denials if isinstance(d, dict)} if isinstance(denials, list) else set()
        return {'tool_uses': self.tool_uses, 'denied_tool_use_ids': denied_ids, 'parse_errors': self.errors,
                'session_id': self.session_id, 'init_model': self.init_model,
                'permission_mode': self.permission_mode, 'tools_count': self.tools_count,
                'limit_hit': self.limit_hit, 'has_result': self.result is not None,
                'subtype': r.get('subtype'), 'is_error': r.get('is_error'), 'num_turns': r.get('num_turns'),
                'model_calls': len(self.main_calls) + self.unkeyed_main,
                'subagent_model_calls': len(self.sub_calls) + self.unkeyed_sub,
                'duration_ms': r.get('duration_ms'), 'duration_api_ms': r.get('duration_api_ms'),
                'total_cost_usd': r.get('total_cost_usd'), 'served_models': served,
                'usage': r.get('usage') if isinstance(r.get('usage'), dict) else None,
                'model_usage': usage if isinstance(usage, dict) else None,
                'served_models_note': None if isinstance(usage, dict) else
                'Claude init model fallback; no result modelUsage, so served model is unverified',
                'permission_denials': len(denials) if isinstance(denials, list) else (denials or 0),
                'final_text': r.get('result') if isinstance(r.get('result'), str) else self.last_text,
                'bad_lines': self.bad_lines}


def _content_items(msg) -> list[dict]:
    content = msg.get('content') if isinstance(msg, dict) else None
    return [c for c in content if isinstance(c, dict)] if isinstance(content, list) else []


def _message_text(msg) -> str:
    parts = [c.get('text') for c in _content_items(msg) if c.get('type') == 'text']
    return '\n'.join(p for p in parts if isinstance(p, str) and p).strip()


def policy_violations(tool_uses: list[dict], denied_ids: set, profile: str, cwd: str | None,
                      policy: dict) -> list[dict]:
    """Tool calls that ran (were not denied) yet break the job's profile: the after-the-fact check
    that also covers writes the git tripwire cannot see (outside the repo, ignored files, the DB)."""
    allow = (policy.get('claude') or {}).get('intel_allow') or []
    out = []
    for u in tool_uses or []:
        if u.get('id') in denied_ids:
            continue
        try:
            reason = guard.evaluate(u.get('name') or '', u.get('input') or {}, profile, cwd, allow)
        except Exception as e:  # noqa: BLE001 - fail closed, as the hook does
            reason = f'guard error ({type(e).__name__})'
        if reason:
            out.append({'tool': u.get('name'), 'input': common.clip(json.dumps(u.get('input'), ensure_ascii=False), 200),
                        'reason': reason})
    return out


def parse_stream_lines(lines) -> dict:
    s = ClaudeStream()
    for line in lines:
        s.feed(line)
    return s.summary()


_KV = re.compile(r'(\w+)=(\S+)')


def _codex_limit_event(line: str) -> bool:
    """A structured Codex error event reporting this run's own usage/rate limit. Free text inside
    item.* events (command output, agent messages) never counts."""
    if not any(m in line for m in CODEX_LIMIT_MARKERS):
        return False
    try:
        ev = json.loads(line)
    except ValueError:
        return False
    if not isinstance(ev, dict) or ev.get('type') not in CODEX_ERROR_EVENTS:
        return False
    fields = json.dumps({k: ev.get(k) for k in ('message', 'error', 'code', 'codex_error_info')}, ensure_ascii=False)
    return any(m in fields for m in CODEX_LIMIT_MARKERS)


def _error_text(text: str) -> str:
    from . import baton
    return common.clip(baton.redact(' '.join(text.split())), 500)


def codex_error_text(lines) -> str | None:
    """Newest structured error only; agent messages and command output are not error events."""
    for line in reversed(list(lines)):
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict) or ev.get('type') not in CODEX_ERROR_EVENTS:
            continue
        error = ev.get('error')
        for text in (ev.get('message'), error.get('message') if isinstance(error, dict) else error):
            if isinstance(text, str) and text.strip():
                return _error_text(text)
    return None


def parse_codex_out(out_dir: str) -> dict:
    """Codex runner evidence: last-message.md, result.txt, events.jsonl."""
    first = common.first_line(os.path.join(out_dir, 'result.txt')).strip()
    kv = dict(_KV.findall(first))
    events = common.tail_lines(os.path.join(out_dir, 'events.jsonl'))
    stderr = common.tail_lines(os.path.join(out_dir, 'stderr.log'), 200_000)
    limit = any(_codex_limit_event(line) for line in events) or any(
        'error' in line.lower() and any(m in line for m in CODEX_LIMIT_MARKERS) for line in stderr)
    sid = kv.get('session') if kv.get('session') not in (None, 'unknown') else None
    if not sid:
        for line in events:
            m = re.search(r'"(?:session_id|thread_id)":\s*"([0-9a-f-]{36})"', line)
            if m:
                sid = m.group(1)
                break
    error = codex_error_text(events) or next((_error_text(s) for s in stderr if 'error' in s.lower()), None)
    return {'error_text': error, 'session_id': sid, 'model': kv.get('model'), 'effort': kv.get('effort'), 'sandbox': kv.get('sandbox'),
            'tokens': kv.get('tokens'), 'reported_exit': kv.get('exit'), 'limit_hit': limit,
            'final_text': _read_text(os.path.join(out_dir, 'last-message.md')), 'result_line': first}


# ---------------------------------------------------------------- git tripwire

def _porcelain(cwd: str) -> dict | None:
    """{path: (status, mtime_ns, size)} for every dirty path, or None outside a repo / on error.

    Mtime and size catch further edits to a file that was already dirty before the job."""
    rc, top = common.git(cwd, 'rev-parse', '--show-toplevel')
    if rc != 0:
        return None
    # -z output: NUL-separated, unquoted paths and an explicit rename source field.
    rc, out, _ = common.run(['git', '-C', cwd, '-c', 'core.quotePath=false', 'status', '--porcelain=v1',
                             '--untracked-files=all', '-z'], timeout=60, env=common.git_env())
    if rc != 0:
        return None
    files, parts, i = {}, out.split('\0'), 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        code, path = entry[:2], entry[3:]
        if code[0] in 'RC':
            i += 1  # -z puts the rename source in the next field
        try:
            st = os.stat(os.path.join(top, path))
            files[path] = (code, st.st_mtime_ns, st.st_size)
        except OSError:
            files[path] = (code, None, None)
    return files


def tripwire_snapshot(cwd: str) -> dict:
    files = _porcelain(cwd)
    if files is None:
        return {'repo': False}
    _, head = common.git(cwd, 'rev-parse', 'HEAD')
    return {'repo': True, 'head': head, 'files': files}


def tripwire_diff(before: dict, after: dict, cwd: str) -> dict:
    if not before.get('repo') or not after.get('repo'):
        return {'touched_files': None, 'head_moved': None, 'note': 'not a git repo: tripwire off'}
    b, a = before['files'], after['files']
    touched = {p for p in set(b) | set(a) if b.get(p) != a.get(p)}
    moved = before.get('head') != after.get('head')
    if moved:
        rc, names = common.git(cwd, 'diff', '--name-only', before['head'], after['head'])
        if rc == 0:
            touched |= {n for n in names.splitlines() if n}
    return {'touched_files': sorted(touched), 'head_moved': moved,
            'head_before': before.get('head'), 'head_after': after.get('head')}


# ---------------------------------------------------------------- slots

def slot_lock(timeout_s: float = 120, *, job_id=None, policy=None, deadline=None):
    """state/slots.lock held with an OS file lock: released by the OS if its holder dies, so there is
    no staleness guess (two waiters could each remove the other's fresh lock)."""
    if job_id is not None:
        return _job_start_lock('slots', job_id, policy, deadline)
    return common.file_lock(os.path.join(common.STATE, 'slots.lock'), timeout_s)


def _lock_wait_seconds(policy=None):
    policy = common.load_policy() if policy is None else policy
    value = (policy.get('jobs') or {}).get('lock_wait_s', 1800)
    try:
        value = float(value)
        if 0 < value < float('inf'):
            return value
    except (TypeError, ValueError):
        pass
    return 1800.0


@contextmanager
def _job_start_lock(name, job_id=None, policy=None, deadline=None, report=True):
    """Extended waits only for job admission; owner sidecars are diagnostic, never leases."""
    deadline = time.monotonic() + _lock_wait_seconds(policy) if deadline is None else deadline
    noted = [False]

    def waiting():
        cell = _SLOT_STUCK.get()
        if cell is not None and name == 'slots' and cell[0] is None:
            cell[0] = time.monotonic()  # a native extended wait counts towards the same overall bound
        if noted[0] or job_id is None or not report:
            return
        noted[0] = True  # the reason is a courtesy: a missing job folder or a failed write never fails the wait
        if not os.path.isdir(_p(job_id, '')):
            return
        try:
            st = common.read_json(_p(job_id, 'status.json'), {}) or {}
            spec = common.read_json(_p(job_id, 'job.json'), {}) or {}
            fields = {k: v for k, v in st.items() if k not in ('status', 'pid', 'pid_created', 'updated_at')}
            fields.update(queue_reason=f"waiting for {'slot' if name == 'slots' else 'writer'} lock",
                          waiting_lock=name, waiting_since=st.get('waiting_since') or common.iso(common.now()))
            if name == 'slots':
                fields.update(waiting_slot=True, queue_priority=queue_priority(spec.get('kind')))
            _write_status(job_id, 'queued', **fields)
        except OSError:
            pass

    _self_created()  # do not make the first process-identity query while holding the global lock
    token = _JOB_LOCK_DEADLINE.set(deadline)
    try:
        with common.file_lock(os.path.join(common.STATE, name + '.lock'),
                              wait_until=deadline, on_wait=waiting):
            yield
    finally:
        _JOB_LOCK_DEADLINE.reset(token)


_SLOT_PROBES = ContextVar('twinlane_slot_probes', default=None)


class _AdmissionChanged(Exception):
    pass


@contextmanager
def _slot_snapshot(engine, job_id, freeze=True):
    """Probe processes outside slots.lock; reread ALL admission files under the lock.

    An identity not observed in this attempt forces a new outside scan. Thus a concurrently
    admitted runner is counted, never hidden by an old running-count snapshot. Nothing here
    persists across attempts, and DEAD observations are tied to the recorded creation stamp.
    """
    probes = {'values': {}, 'frozen': False}
    token = _SLOT_PROBES.set(probes)
    try:
        _self_created()
        running_count(engine, exclude=job_id)
        _waiters(engine, job_id)
        probes['frozen'] = freeze  # the in-lock fallback probes only identities that appeared after the scan
        yield
    finally:
        _SLOT_PROBES.reset(token)


_SELF_CREATED = []


def _self_created():
    if not _SELF_CREATED:
        _SELF_CREATED.append(common.process_created(os.getpid()))
    return _SELF_CREATED[0]


def _inlock_budget() -> None:
    """Work done while holding slots.lock (the in-lock probes of the fallback) stops at the shared bound and unwinds it."""
    probes = _SLOT_PROBES.get()
    # the streak's deadline, else the one this very lock wait ran under (a first attempt has no streak yet)
    deadline = _SLOT_DEADLINE.get() or _JOB_LOCK_DEADLINE.get()
    if probes is not None and probes.get('locked') and deadline is not None and time.monotonic() > deadline:
        raise common.LockWaitTimeout('slot admission scan exceeded the lock-wait bound while holding slots.lock')


def _pid_state(pid, created) -> str:
    _inlock_budget()
    probes = _SLOT_PROBES.get()
    if probes is not None:
        key = (repr(pid), repr(created))
        if key not in probes['values']:
            if probes['frozen']:
                raise _AdmissionChanged()
            probes['values'][key] = _probe_pid_state(pid, created)
        return probes['values'][key]
    return _probe_pid_state(pid, created)


def _probe_pid_state(pid, created) -> str:
    if _PRESERVING_METADATA.get() and not (pid is None and created is None):
        if not common._valid_pid(pid) or not common._valid_stamp(created):
            return common.UNKNOWN
    return common.pid_state(pid, created) if pid else common.DEAD


def _alive(pid, created) -> bool:
    """Could still be running: alive, or exists with an identity this process cannot read (UNKNOWN).
    Slots, continuation guards and kill checks lean to alive; only a provably gone process is dead."""
    return _pid_state(pid, created) != common.DEAD


def _child_state(st: dict) -> str:
    return _pid_state(st.get('child_pid'), st.get('child_created'))


def _child_alive(st: dict) -> bool:
    return _child_state(st) != common.DEAD


def _survivor(job_id: str) -> dict | None:
    """The engine process that outlived its kill, from jobs/<id>/survivor.json, or None. The only cost for a
    job that ended cleanly is one existence check."""
    if not os.path.exists(_p(job_id, SURVIVOR)):
        return None
    s = common.read_json(_p(job_id, SURVIVOR))
    if not isinstance(s, dict) or not isinstance(s.get('pid'), int):
        return None
    return s


def _survivor_state(s: dict) -> str:
    """A survivor recorded without a creation stamp has no identity: it is never DEAD while its pid exists
    (counted, blocks a continue) and never ALIVE (never killed)."""
    if s.get('created') in (None, ''):
        return common.DEAD if _pid_state(s['pid'], None) == common.DEAD else common.UNKNOWN
    return _pid_state(s['pid'], s['created'])


def _child_pending(job_id: str) -> bool:
    """Reclaim launch uncertainty only when its own recorded child identity is proven gone. An unresolved stalled
    tree is uncertainty no pid can settle: it reads as pending for every liveness, slot and archive consumer."""
    if os.path.exists(_p(job_id, TREE_UNRESOLVED)):
        return True
    path = _p(job_id, CHILD_STARTING)
    if not os.path.exists(path):
        return False
    if _preserve_job(job_id) or observations.active() or _SLOT_PROBES.get() is not None:
        # Observe without reclaim. Pin bytes around the native check: a changed/unreadable
        # marker or invalid recorded identity holds; only definitive exact-stamp DEAD clears
        # the logical uncertainty. The file remains untouched, even across other tasks.
        try:
            with open(path, 'rb') as handle:
                before = handle.read()
            pending = json.loads(before)
            if not isinstance(pending, dict) or not common._valid_pid(pending.get('pid')) \
                    or not common._valid_stamp(pending.get('created')):
                return True
            state = _pid_state(pending['pid'], pending['created'])
            with open(path, 'rb') as handle:
                after = handle.read()
            return before != after or state != common.DEAD
        except (OSError, ValueError, RecursionError):
            return True
    with archive_lock(job_id):
        path = _p(job_id, CHILD_STARTING)
        pending = common.read_json(path)
        if isinstance(pending, dict) and isinstance(pending.get('pid'), int) \
                and isinstance(pending.get('created'), int) \
                and _pid_state(pending['pid'], pending['created']) == common.DEAD:
            try:
                os.remove(path)
            except OSError:
                return True
            return False
        return True

def _engine_state(job_id: str, st: dict) -> str:
    """The strongest evidence that a job's engine process still runs: the child recorded in status.json or the
    survivor recorded in survivor.json (a crash between the two writes and the receipt leaves only the latter).
    ALIVE beats UNKNOWN beats DEAD."""
    states = [_child_state(st)]
    if _child_pending(job_id):
        states.append(common.UNKNOWN)
    s = _survivor(job_id)
    if s:
        states.append(_survivor_state(s))
    return common.ALIVE if common.ALIVE in states else common.UNKNOWN if common.UNKNOWN in states else common.DEAD


def _engine_gone(proc, grace_s: float = KILL_GRACE_S) -> bool:
    """After a kill, whatever kill_tree answered: is the engine really reaped once its grace has run? A
    "successful" kill command proves nothing, and a second Ctrl+C during the wait is judged by the process."""
    try:
        proc.wait(timeout=grace_s)
    except BaseException:  # noqa: BLE001 - TimeoutExpired, or an interrupt of the grace wait itself
        pass
    return proc.poll() is not None


def _pid_gone(pid, created, grace_s: float = KILL_GRACE_S) -> bool:
    """After a kill this process did not start: wait up to the grace for the pid to stop being that process."""
    deadline = time.monotonic() + grace_s
    while common.pid_maybe_alive(pid, created):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def _note_survivor(job_id: str, engine: str, proc, created, why: str) -> None:
    """Record that `proc` outlived its kill, BEFORE any receipt says the job ended: the receipt then cannot hide
    it from job_status, running_count or `twinlane stop`. Best effort: a read-only disk must not hide it from the
    receipt's own note either."""
    if not created:
        try:
            created = common.process_created(proc.pid)
        except OSError:
            created = None
    try:
        _write_job_json(job_id, SURVIVOR, {'engine': engine, 'pid': proc.pid, 'created': created, 'why': why,
                                                 'at': common.iso(common.now())})
    except OSError:
        pass


def _cleanup_started_child(job_id: str, engine: str, proc, created, why: str) -> bool:
    """Clean up every failure after Popen, including failures before status/pump initialization.

    Return whether the child may still be alive. A failed bookkeeping write must never release its lease.
    """
    killed = False
    try:
        killed = common.kill_tree(proc.pid)
    except BaseException:
        pass
    if common.IS_WIN and killed is not True:
        _note_unresolved_tree(job_id, engine, None, kill_confirmed=False)
    alive = not _engine_gone(proc)
    if alive:
        _note_survivor(job_id, engine, proc, created, why)
    return alive


def _clear_child_starting(job_id: str, proc=None, keep: bool = False) -> None:
    """Remove uncertainty only after proving the child reaped/not started.

    Persisting identity is insufficient: later cleanup and survivor persistence can both fail.
    Keep the durable marker for the entire child lifetime so a terminal receipt cannot hide it.
    `keep` holds it when a stalled tree is unresolved and its own marker could not be written.
    """
    if keep or _preserve_job(job_id) or (common.IS_WIN and _tree_unresolved(job_id)):
        return
    try:
        if proc is None or proc.poll() is not None:
            reaped = _REAPED_CHILDREN.get()
            if proc is not None and reaped is not None:
                reaped[job_id] = proc.pid
            with archive_lock(job_id):
                os.remove(_p(job_id, CHILD_STARTING))
    except OSError:
        pass  # retained uncertainty is safer than silently allowing another worker


def running_count(engine: str, exclude: str | None = None) -> int:
    n = 0
    for jid in _job_ids():
        if jid == exclude:
            continue
        # Launch uncertainty survives receipt/status write failures. If even its engine is
        # unreadable, reserve both engines conservatively rather than guessing one free.
        if _child_pending(jid):
            pending = common.read_json(_p(jid, CHILD_STARTING)) or common.read_json(_p(jid, TREE_UNRESOLVED))
            pending_engine = pending.get('engine') if isinstance(pending, dict) else None
            if pending_engine not in ('claude', 'codex') or pending_engine == engine:
                n += 1
            continue
        if os.path.exists(_p(jid, 'result.json')):
            s = _survivor(jid)  # a receipt ends the job, unless its engine outlived the kill
            if s and s.get('engine') == engine and _survivor_state(s) != common.DEAD:
                n += 1
            continue
        st = common.read_json(_p(jid, 'status.json'))
        if not isinstance(st, dict):
            # Unreadable mid-write: a live runner still holds a slot (over-counting only delays a job).
            spec = common.read_json(_p(jid, 'job.json'), {}) or {}
            if _PRESERVING_METADATA.get():
                primary = spec.get('primary') if isinstance(spec, dict) else None
                declared = primary.get('engine') if isinstance(primary, dict) else None
                pid, created = _runner_pid(jid)
                if declared not in ENGINES or (declared == engine and (
                        not common._valid_pid(pid) or not common._valid_stamp(created))):
                    n += 1
                    continue
            surv = _survivor(jid)
            if (spec.get('primary') or {}).get('engine') == engine and (
                    _alive(*_runner_pid(jid)) or surv and _survivor_state(surv) != common.DEAD):
                n += 1
            continue
        if _PRESERVING_METADATA.get() and st.get('engine') not in ENGINES:
            # An object-shaped status can still omit/mangle its engine. Neither
            # engine may assume capacity is free while its recorded runner/child
            # is live, UNKNOWN or lacks a qualified identity.
            pid, created = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(jid)
            if not common._valid_pid(pid) or not common._valid_stamp(created) \
                    or _alive(pid, created) or _engine_state(jid, st) != common.DEAD:
                n += 1
            continue
        if st.get('engine') != engine:
            continue
        if _PRESERVING_METADATA.get() and st.get('status') == 'running' and (
                not common._valid_pid(st.get('pid')) or not common._valid_stamp(st.get('pid_created'))):
            n += 1
            continue
        if st.get('status') == 'running' and _alive(st.get('pid'), st.get('pid_created')):
            n += 1
        elif _engine_state(jid, st) != common.DEAD:  # an orphaned engine process still spends quota
            n += 1
    return n


def try_acquire_slot(job_id: str, engine: str, attempt: int, max_parallel: int) -> bool:
    try:
        with _slot_snapshot(engine, job_id), slot_lock(job_id=job_id):
            _SLOT_PROBES.get()['locked'] = True
            if running_count(engine, exclude=job_id) >= max_parallel:
                return False
            _inlock_budget()
            _write_status(job_id, 'running', engine=engine, attempt=attempt)
            return True
    except _AdmissionChanged:
        return False


def queue_priority(kind) -> int:
    """0 starts before 1: a review unblocks a land, so it goes ahead of other waiting work."""
    return 0 if kind == 'review' else 1


def _waiters(engine: str, exclude: str) -> list:
    """Live jobs waiting for a slot of this engine as (priority, waiting_since, id), in service order.
    A waiter whose runner is not provably alive is ignored: it must not hold the line."""
    rows = []
    for jid in _job_ids():
        if jid == exclude or os.path.exists(_p(jid, 'result.json')):
            continue
        st = common.read_json(_p(jid, 'status.json'))
        live = isinstance(st, dict) and st.get('status') == 'queued' and st.get('waiting_slot') \
            and st.get('engine') == engine
        if not live or _pid_state(st.get('pid'), st.get('pid_created')) != common.ALIVE:
            continue
        rows.append((st.get('queue_priority', 1), str(st.get('waiting_since') or ''), jid))
    return sorted(rows)


def _own_place(job_id: str) -> tuple:
    st = common.read_json(_p(job_id, 'status.json'))
    if isinstance(st, dict) and st.get('waiting_slot') and st.get('waiting_since'):
        return st.get('queue_priority', 1), str(st['waiting_since']), job_id
    spec = common.read_json(_p(job_id, 'job.json'), {}) or {}
    return queue_priority(spec.get('kind') if isinstance(spec, dict) else None), common.iso(common.now()), job_id


def slot_attempt(job_id: str, engine: str, attempt: int, configured: int, policy: dict) -> tuple:
    """(started, why not). The usable slots shrink as the host gets busy (hostload), and waiting jobs are
    served first-in-first-out with reviews first. A job is queued with a reason, never refused."""
    usable, host_why = hostload.effective_parallel(configured, hostload.sample(policy), policy)
    # After SLOT_INLOCK_AFTER restarts in a row the snapshot is not frozen: under the lock only the identities that
    # appeared since the outside scan are probed (a few, never the whole store), so that attempt cannot be
    # invalidated and the oldest job always decides.
    snapshot = _slot_snapshot(engine, job_id, freeze=not _SLOT_INLOCK.get())
    try:
        with snapshot, slot_lock(job_id=job_id, policy=policy, deadline=_SLOT_DEADLINE.get()):
            _SLOT_PROBES.get()['locked'] = True
            running = running_count(engine, exclude=job_id)
            if running >= usable:
                return False, (f'engine slots full: {running}/{configured}' if running >= configured
                               else host_why or f'engine slots full: {running}/{configured}')
            ahead = [w for w in _waiters(engine, job_id) if w < _own_place(job_id)]
            if len(ahead) >= usable - running:
                return False, f"queued behind {len(ahead)} job{'' if len(ahead) == 1 else 's'}"
            waited = (common.read_json(_p(job_id, 'status.json'), {}) or {}).get('waiting_lock')
            _inlock_budget()
            _write_status(job_id, 'running', engine=engine, attempt=attempt)
            return True, 'waited for slot lock' if waited else None
    except _AdmissionChanged:
        return False, ADMISSION_CHANGED


ADMISSION_CHANGED = 'slot observations changed; retrying admission'
SLOT_INLOCK_AFTER = 3
WRITER_SWEEP_WAIT_S = 30  # release_stale_writers is a lead command: it fails fast, only job runners wait long
_SLOT_DEADLINE = ContextVar('twinlane_slot_deadline', default=None)
_SLOT_STUCK = ContextVar('twinlane_slot_stuck', default=None)
_SLOT_INLOCK = ContextVar('twinlane_slot_inlock', default=False)


def acquire_slot(job_id: str, engine: str, attempt: int, policy: dict) -> bool:
    """Blocks until a slot is free. Returns True when it had to wait. The reason it waits is in status.json."""
    configured = int(policy.get(engine, {}).get('max_parallel') or 4)
    waiting, last, since = False, None, None
    stuck = [None]  # monotonic start of the current streak of lock waits / restarted scans; ONE bound covers it
    changed = 0     # consecutive restarted scans
    bound = _lock_wait_seconds(policy)
    while True:
        attempt_started = time.monotonic()
        tokens = (_SLOT_DEADLINE.set(None if stuck[0] is None else stuck[0] + bound), _SLOT_STUCK.set(stuck),
                  _SLOT_INLOCK.set(changed >= SLOT_INLOCK_AFTER))
        try:
            started, why = slot_attempt(job_id, engine, attempt, configured, policy)
        except common.LockWaitTimeout:
            raise
        except TimeoutError as error:
            if 'lock busy:' not in str(error):
                raise
            stuck[0] = attempt_started if stuck[0] is None else stuck[0]
            if time.monotonic() - stuck[0] >= bound:
                raise common.LockWaitTimeout(f'lock stayed busy for {bound / 60:.3f} '
                                             'minutes: slots.lock') from error
            started, why = False, 'waiting for slot lock'
        finally:
            for var, token in zip((_SLOT_DEADLINE, _SLOT_STUCK, _SLOT_INLOCK), tokens):
                var.reset(token)
        if started:
            return waiting or why == 'waited for slot lock'
        if why == ADMISSION_CHANGED:
            changed += 1
            stuck[0] = attempt_started if stuck[0] is None else stuck[0]
            if time.monotonic() - stuck[0] >= bound:
                raise common.LockWaitTimeout(f'slot admission stayed contended for {bound / 60:.3f} minutes')
        else:
            changed = 0
            if why != 'waiting for slot lock':
                stuck[0] = None  # a normal answer (slots full, queued behind, host busy) ends the streak
        if not waiting or why != last:
            since = since or (common.read_json(_p(job_id, 'status.json'), {}) or {}).get('waiting_since') \
                or common.iso(common.now())
            _write_status(job_id, 'queued', engine=engine, attempt=attempt, waiting_slot=True, queue_reason=why,
                          waiting_since=since, queue_priority=_own_place(job_id)[0])
            waiting, last = True, why
        time.sleep(SLOT_POLL_S * random.uniform(0.75, 1.25)
                   * (2.5 if (why or '').startswith('host busy') else 1))


def _write_status(job_id: str, status: str, **extra) -> None:
    with lifecycle_lock(job_id, 'status.lock'):
        st = {'status': status, 'pid': os.getpid(), 'pid_created': _self_created(),
              'updated_at': common.iso(common.now())}
        old = common.read_json(_p(job_id, 'status.json'), {}) or {}
        st['started_at'] = old.get('started_at') or (st['updated_at'] if status == 'running' else None)
        st.update(extra)
        _write_job_json(job_id, 'status.json', st)


def _merge_status(job_id: str, **fields) -> None:
    with lifecycle_lock(job_id, 'status.lock'):
        st = common.read_json(_p(job_id, 'status.json'), {}) or {}
        st.update(fields)
        _write_job_json(job_id, 'status.json', st)


# ---------------------------------------------------------------- preflight

# What the Codex transport runs for each tier (its own table); an explicit `model` overrides the tier.
CODEX_TIER_MODELS = {'luna': 'gpt-6-luna', 'sol': 'gpt-6.1-sol', 'sol-high': 'gpt-6.1-sol', 'astra': 'gpt-6-astra'}
CREDITS_OK_HOW = ('only the user can OK paid credits, by setting codex.allow_credits to true in policy.json '
                  '(twinlane has no per-job credits flag)')


def codex_model(params: dict) -> str:
    """The model a Codex attempt will actually run: the explicit override, else the tier's model."""
    return params.get('model') or CODEX_TIER_MODELS.get(params.get('tier')) or str(params.get('tier') or 'unknown')


def codex_credit_refusal(meter: dict, params: dict, policy: dict) -> str | None:
    """Why a Codex attempt must not start, or None. Paid credits are refused for EVERY model (luna too)
    unless policy.codex.allow_credits is true. twinlane does not rely on the transport for this: the reading
    must be present and fresh, and show Codex neither on credits nor past its included usage. Unknown
    is refused. Judged on the effective model, so a tier override cannot dodge it."""
    xp = policy.get('codex') or {}
    if xp.get('allow_credits') is True:
        return None
    model, sec = codex_model(params), (meter or {}).get('codex') or {}
    age = sec.get('age_s')
    lag = common.age_s((meter or {}).get('read_at')) or 0
    max_age = int(xp.get('preflight_max_age_s') or 300)
    if age is None or sec.get('status') == 'unknown' or age + lag > max_age:
        seen = 'unknown' if age is None or sec.get('status') == 'unknown' else f'{int(age + lag)}s old (> {max_age}s)'
        return (f'codex reading is {seen}: {model} could spend paid credits unseen; run `twinlane meter --fresh` '
                f'and retry; {CREDITS_OK_HOW}')
    if (sec.get('on_credits') or sec.get('spend_control_reached') or sec.get('ordinary_usage_allowed') is False
            or sec.get('limit_reached_type')):
        return f'codex is on or past paid credits: {model} would spend them; {CREDITS_OK_HOW}'
    return None


def _preserving_engine_refusal(spec: dict, engine: str) -> str | None:
    """A preserving start runs only its own primary engine; Codex only with the binary recorded at planning.
    Checked before any meter read, so a refusal has no binary-cache or provider effect."""
    primary = (spec.get('primary') or {}).get('engine')
    if engine != primary:
        return (f'preserving {primary} start never fails over to {engine}'
                + ('; a Claude preserving start stays Claude only' if primary == 'claude' else ''))
    if engine == 'claude':
        return None
    if engine != 'codex':
        return f'preserving start does not support engine {engine!r}'
    if common.CODEX_RUN:
        return 'preserving Codex start refused: a TWINLANE_CODEX_RUN adapter cannot verify the qualified binary hand-off'
    problem = common.codex_bin_identity_problem(spec.get('codex_binary'))
    return f'preserving Codex start refused at preflight: {problem}' if problem else None


def preflight(spec: dict, params: dict, policy: dict, fresh: bool,
              seen: dict | None = None) -> tuple[str | None, str | None]:
    """Re-check the meter right before an attempt starts: a queued job may have waited while the
    engine crossed its stop line. Returns (refusal reason, wait_until) or (None, None).
    `seen` (optional) keeps the first meter read here: the job's 'before' usage reading."""
    engine = params['engine']
    if spec.get('write') or spec.get('profile') == 'code':
        why = writer_readiness(spec.get('cwd'), policy)
        if why:
            return why, None
    if spec.get('preserve_existing_metadata') is True:
        why = _preserving_engine_refusal(spec, engine)
        if why:
            return why, None
    try:
        meter = _meter(policy, fresh=fresh, auto_refresh=engine != 'codex')
        if seen is not None and 'meter' not in seen:
            seen['meter'], seen['at'] = meter, common.now()
        d = _route(spec['kind'], meter, policy, spec.get('urgent', False), engine)
    except Exception as e:  # noqa: BLE001 - an unknown meter is never OK
        return f'meter/route unavailable at start: {type(e).__name__}: {common.clip(e, 160)}', None
    if d.get('refused') or d.get('engine') != engine:
        return f"{engine} not usable at start: {d.get('reason')}", d.get('wait_until')
    if engine == 'codex':
        why = codex_credit_refusal(meter, params, policy)
        if why:
            return why, None
    return None, None


def writer_readiness(cwd, policy):
    from . import worktrees, projects
    # This check qualifies configured dependencies. Existing writer admission owns
    # Git/worktree qualification; a project with no dependencies needs no probe.
    entries = policy.get('projects') or {}
    configured = (policy.get('wt') or {}).get('deps_dirs') or any(
        isinstance(entry, dict) and isinstance(entry.get('wt'), dict) and entry['wt'].get('deps_dirs')
        for entry in entries.values())
    if not configured:
        return None
    try:
        _, project = projects.resolve(cwd, policy)
        mode = (project.get('wt') or {}).get('preflight', 'refuse')
        if mode == 'off':
            return None
        problems = worktrees.preflight(common.worktree_top(cwd) or cwd, project)
    except projects.ProjectError as e:
        return f'writer worktree preflight: {e}'
    if problems:
        message = 'worktree not ready: ' + '; '.join(problems)
        setup = (project.get('wt') or {}).get('setup_command')
        if setup:
            message += '; run: ' + str(setup)
        if mode == 'warn':
            print(message, file=sys.stderr)
        else:
            return message

    return None


# ---------------------------------------------------------------- attempts

def _attempt_files(job_id: str, n: int) -> dict:
    sfx = '' if n == 1 else f'-a{n}'
    return {'events': _p(job_id, f'events{sfx}.jsonl'), 'stderr': _p(job_id, f'stderr{sfx}.log'),
            'final': _p(job_id, f'final-a{n}.md'), 'codex_out': _p(job_id, f'codex{sfx}'),
        'codex_log': _p(job_id, f'codex-adapter{sfx}.log'),
        'codex_log_legacy': _p(job_id, f'codex-run{sfx}.log')}  # the name an earlier tool gave the same log


def run_attempt(job_id: str, n: int, params: dict, brief_path: str, spec: dict, policy: dict,
                resume: str | None) -> dict:
    files = _attempt_files(job_id, n)
    started = common.now()
    a = {'n': n, 'engine': params['engine'], 'model': params.get('model'), 'effort': params.get('effort'),
         'tier': params.get('tier'), 'profile': params['profile'], 'max_turns': params.get('max_turns'),
         'timeout_s': params['timeout_s'], 'resume': resume, 'brief_path': brief_path,
         'started_at': common.iso(started)}
    settings = guard_settings(job_dir(job_id), intel_settings_deny(params, policy, spec['cwd'])) \
        if params['engine'] == 'claude' else None
    argv = build_argv(params, policy, brief_path, spec['cwd'], files['codex_out'], resume, settings)
    if argv is None:
        a.update(status='failed', exit_code=common.EXIT_MISSING, error='claude binary not found')
    elif params['engine'] == 'claude':
        a['argv'] = argv
        a.update(_run_claude(job_id, argv, spec, params, brief_path, files, policy))
    else:
        a['argv'] = argv
        preserving = spec.get('preserve_existing_metadata') is True or _PRESERVING_METADATA.get()
        a.update(_run_codex(job_id, argv, spec['cwd'], params['timeout_s'] + CODEX_GRACE_S, files, params, policy,
                            **({'qualified': spec.get('codex_binary')} if preserving else {})))
    a['ended_at'] = common.iso(common.now())
    a['duration_s'] = round((common.now() - started).total_seconds(), 1)
    text = a.pop('final_text', '') or ''
    if text:
        _write_job_text(job_id, os.path.basename(files['final']), text)
        a['final_path'] = files['final']
    return a


def _pump(proc, fev, stream: ClaudeStream, errors: list) -> None:
    """Copy claude's stdout to events.jsonl and the parser. Whatever fails, it keeps reading, so the
    child can never block on a full pipe and sit idle until the timeout."""
    sink = fev
    try:
        for raw in iter(proc.stdout.readline, b''):
            if sink is not None:
                try:
                    sink.write(raw)
                    sink.flush()
                except OSError as e:
                    errors.append(f'events.jsonl write failed, still draining: {e}')
                    sink = None
            stream.feed(raw.decode('utf-8', errors='replace'))
    except (OSError, ValueError) as e:
        errors.append(f'stdout read stopped: {e}')


def _kill_wrapper_unconfirmed(pid, created=None) -> bool:
    """On macOS and Linux a process-group kill proves nothing about the whole tree: the packaged Codex runner starts
    `codex exec` in its OWN session, and a Claude worker may start a helper with its own session. Without a handle
    that contains every descendant the kill is reported unconfirmed (no retry, custody held); the group is still
    killed."""
    if created is None:
        common.kill_tree(pid)
    else:
        common.kill_tree(pid, created)
    return False


def _close_attempt_job(job_id: str, engine: str, proc, unresolved: bool) -> tuple:
    """Close the attempt's job object and record what could not be proven. -> (keep_launch_marker, interruption).
    An interrupt during the drain is held until the unresolved-tree custody is persisted (the PID-less fallback
    included); the caller re-raises it once its own cleanup has run."""
    interrupt = None
    try:
        confirmed = common.close_engine_job(proc)
    except BaseException as e:
        confirmed, interrupt = False, e
    if not confirmed and not _note_unresolved_tree(job_id, engine, None, kill_confirmed=False):
        unresolved = True  # the marker could not be written: the rewritten launch marker is the custody
    return unresolved, interrupt


def _hold_uncontained(job_id: str, engine: str):
    """Runs while an engine that no job object holds is still suspended: its descendants can never be proven gone,
    so unresolved-tree custody is made durable BEFORE it runs (a killed runner or a `stop` then leaves the claim
    held). When the marker file cannot be written the engine is not started."""
    if not _note_unresolved_tree(job_id, engine, None, kill_confirmed=False) or not _tree_unresolved(job_id):
        raise OSError('the engine could not be contained in a job object and the unresolved-tree marker could '
                      'not be written; it was not started')


def _watchdog_kill():
    """The kill both watchdogs use: the default on Windows (the attempt job object answers), the unconfirmed
    process-group kill elsewhere."""
    return None if common.IS_WIN else _kill_wrapper_unconfirmed


def _note_unresolved_tree(job_id: str, engine: str, stall, kill_confirmed=None) -> bool:
    """A stall kill that did not report success may have left children running although the root is gone. Leave a
    marker that keeps the worktree claim until a person has looked (`twinlane stop JOB --tree-gone`): no pid in it
    can be trusted to identify what survived, so nothing here ever kills anything. Returns False when the marker
    was needed and could not be written: the launch marker is then rewritten without a pid and the caller keeps it."""
    if kill_confirmed is not False and (stall is None or not stall.tripped or stall.kill_confirmed is True):
        return True
    for _ in range(3):
        try:
            _write_job_json(job_id, TREE_UNRESOLVED, {'engine': engine, 'at': common.iso(common.now()),
                                                      'why': 'the stalled engine tree was not proven stopped'
                                                      if stall is not None and stall.tripped
                                                      else 'the engine tree was not proven stopped'})
            return True
        except OSError:
            time.sleep(0.2)
    try:  # the launch marker names the dead root, which a slot scan would reap: rewrite it with no pid to find
        _write_job_json(job_id, CHILD_STARTING, {'engine': engine, 'unresolved_tree': True})
    except OSError:
        pass  # even that failed: the old marker is kept, and a dead-root scan can still reap it
    return False  # either way the launch marker stays: it is now the only record


def _mark_stalled(out: dict, stall) -> dict:
    """A killed hung worker: whatever it printed is not a result, so no final text and no verdict survive."""
    if stall is not None and stall.tripped:
        out.update(status='stalled', final_text='', limit_hit=False, error='stalled: ' + stall.reason(),
                   stall_kill_confirmed=stall.kill_confirmed is True)
    return out


SURVIVED_TIMEOUT_KILL = 'engine process (pid {pid}) survived its kill after the timeout (kill {kill})'
SURVIVED_INTERRUPT_KILL = ('engine process (pid {pid}) survived its kill after the runner was interrupted '
                           '(kill {kill})')


def _run_claude(job_id, argv, spec, params, brief_path, files, policy) -> dict:
    stream, errors, stripped = ClaudeStream(), [], []
    timed_out = False
    cwd, timeout_s = spec['cwd'], params['timeout_s']
    env = claude_env(job_id, params['profile'], cwd, stripped, common.extra_secret_patterns(policy))
    proc = None
    stall = watchdog.make([files['events'], files['stderr']], policy)
    unresolved = False
    try:
        _write_job_json(job_id, CHILD_STARTING, {'engine': 'claude'})
        with open(brief_path, 'rb') as fin, open(files['stderr'], 'wb') as ferr, open(files['events'], 'wb') as fev:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=fin,
                                    stdout=subprocess.PIPE, stderr=ferr,
                                    creationflags=NO_WINDOW | (common.CREATE_SUSPENDED if common.IS_WIN else 0),
                                    start_new_session=not common.IS_WIN)
            child_created = None
            try:
                common.contain_engine(proc, before_resume=lambda contained: contained
                                      or _hold_uncontained(job_id, 'claude'))
                child_created = common.process_created(proc.pid)
                _write_job_json(job_id, CHILD_STARTING,
                                  {'engine': 'claude', 'pid': proc.pid, 'created': child_created})
                _merge_status(job_id, child_pid=proc.pid if child_created else None, child_created=child_created)
                reader = threading.Thread(target=_pump, args=(proc, fev, stream, errors), daemon=True)
                reader.start()
                try:
                    rc = watchdog.wait(proc, timeout_s, stall, watchdog.settings(policy)['poll_s'],
                                       child_created=child_created,
                                       kill=_watchdog_kill())
                    unresolved = not _note_unresolved_tree(job_id, 'claude', stall)
                    if rc is None:  # only a stall kill returns None: the tree outlived it
                        raise subprocess.TimeoutExpired(argv, KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    killed = common.kill_tree(proc.pid)
                    if common.IS_WIN and killed is not True:
                        unresolved = not _note_unresolved_tree(job_id, 'claude', stall, kill_confirmed=False)
                    try:
                        rc = proc.wait(timeout=KILL_GRACE_S)
                    except subprocess.TimeoutExpired:
                        rc = None
                        _note_survivor(job_id, 'claude', proc, child_created, SURVIVED_TIMEOUT_KILL.format(
                            pid=proc.pid, kill='reported success' if killed else 'failed'))
                reader.join(timeout=15)
            except BaseException:
                # An interrupt during the grace wait after a stall kill skips the note above: make it here, before
                # the launch marker is cleared, or surviving children lose their custody protection.
                unresolved = not _note_unresolved_tree(job_id, 'claude', stall)
                _cleanup_started_child(job_id, 'claude', proc, child_created,
                                       'engine survived its kill after a post-start failure')
                raise
    except OSError as e:
        return {'status': 'failed', 'exit_code': common.EXIT_MISSING, 'error': f'claude launch or bookkeeping failed: {e}'}
    finally:
        # the stream reader has consumed the result, but a descendant may outlive the root
        unresolved, interrupt = _close_attempt_job(job_id, 'claude', proc, unresolved)
        _clear_child_starting(job_id, proc, keep=unresolved)
        if interrupt is not None:
            raise interrupt
    s = stream.summary()
    out = {k: s[k] for k in ('session_id', 'served_models', 'served_models_note', 'num_turns', 'model_calls',
                             'subagent_model_calls', 'subtype', 'is_error', 'duration_ms',
                             'duration_api_ms', 'total_cost_usd', 'permission_denials', 'permission_mode',
                             'tools_count', 'limit_hit', 'final_text', 'bad_lines')}
    out.update(exit_code=rc, events_path=files['events'], stderr_path=files['stderr'])
    if params['profile'] == 'code':
        out['env_stripped'] = stripped  # names only, never values
    tokens = _claude_tokens(s.get('usage'), s.get('model_usage'))
    if tokens:
        out['token_usage'] = tokens
    out['tool_uses'] = len(s['tool_uses'])
    out['disallowed_tool_uses'] = policy_violations(s['tool_uses'], s['denied_tool_use_ids'], params['profile'],
                                                    cwd, policy)[:20]
    if errors or s['parse_errors']:
        out['stream_errors'] = (errors + s['parse_errors'])[:10]
    out['status'] = _claude_status(s, rc, timed_out)
    if out['status'] == 'failed' and s['has_result'] and not s['is_error'] and s['subtype'] == 'success' and rc != 0:
        out['error'] = f'claude reported success but exited rc {rc}'
    return _mark_stalled(out, stall)


CLAUDE_TOKEN_KEYS = {'input_tokens': 'input_tokens', 'output_tokens': 'output_tokens',
                     'cache_read_input_tokens': 'cached_input_tokens',
                     'cache_creation_input_tokens': 'cache_write_input_tokens'}


CLAUDE_MODEL_TOKEN_KEYS = {'inputTokens': 'input_tokens', 'outputTokens': 'output_tokens',
                           'cacheReadInputTokens': 'cached_input_tokens',
                           'cacheCreationInputTokens': 'cache_write_input_tokens'}


def _claude_tokens(usage, model_usage=None) -> dict | None:
    """The result event's own token counts, under the names the receipt shares with Codex. Without a
    top-level `usage`, the per-model `modelUsage` entries are summed."""
    def num(v):
        return isinstance(v, (int, float)) and not isinstance(v, bool)

    if isinstance(usage, dict):
        got = {new: int(usage[old]) for old, new in CLAUDE_TOKEN_KEYS.items() if num(usage.get(old))}
        if got:
            return {'source': 'claude result', **got}
    got = {}
    for entry in (model_usage or {}).values() if isinstance(model_usage, dict) else ():
        for old, new in CLAUDE_MODEL_TOKEN_KEYS.items():
            if isinstance(entry, dict) and num(entry.get(old)):
                got[new] = got.get(new, 0) + int(entry[old])
    return {'source': 'claude modelUsage', **got} if got else None


def _codex_events_tokens(events_path: str) -> dict | None:
    """Fallback when no rollout is found: the sum of the `turn.completed` usage events of codex exec --json."""
    total: dict = {}
    try:
        with open(events_path, 'rb') as h:
            for raw in h:
                if b'turn.completed' not in raw:
                    continue
                try:
                    ev = json.loads(raw)
                except ValueError:
                    continue
                u = ev.get('usage') if isinstance(ev, dict) and ev.get('type') == 'turn.completed' else None
                if isinstance(u, dict):
                    for k in ('input_tokens', 'cached_input_tokens', 'output_tokens', 'reasoning_output_tokens'):
                        if isinstance(u.get(k), (int, float)) and not isinstance(u.get(k), bool):
                            total[k] = total.get(k, 0) + int(u[k])
    except OSError:
        return None
    return {'source': 'codex events', **total} if total else None


def _codex_tokens(session_id, events_path: str, run_tokens) -> dict | None:
    """Per-job tokens for a Codex attempt: the session rollout's last token_count, else the exec events,
    else the runner's total. None when none of them has one."""
    from . import meter
    got = None
    if not _PRESERVING_METADATA.get():  # a preserving start never reads the private session rollouts
        try:
            got = meter.rollout_token_usage(session_id)
        except OSError:
            pass
    got = got or _codex_events_tokens(events_path)
    if not got and isinstance(run_tokens, str) and run_tokens.isdigit():
        got = {'source': 'built-in-runner', 'total_tokens': int(run_tokens)}
    return got


def _claude_status(s: dict, rc, timed_out: bool) -> str:
    if timed_out:
        return 'timeout'
    ok = s['has_result'] and not s['is_error'] and s['subtype'] == 'success'
    if ok:
        return 'done' if rc == 0 else 'failed'
    if s['limit_hit']:
        return 'limit_hit'
    return 'failed'


# What Codex prints when its sandbox helper could not set up: its commands then fail while the run still
# ends with a normal-looking message.
SANDBOX_SETUP_MARKER = 'setup refresh had errors'


def _sandbox_setup_failed(files: dict) -> bool:
    paths = [os.path.join(files['codex_out'], 'events.jsonl'), os.path.join(files['codex_out'], 'stderr.log'),
             files['codex_log']]
    for path in paths:
        try:
            with open(path, encoding='utf-8', errors='replace') as stream:
                if any(SANDBOX_SETUP_MARKER in line for line in stream):
                    return True
        except OSError:
            continue
    return False


_ORDINARY = object()  # _run_codex(qualified=...) default: an ordinary start acquires its binary as usual


def _preserving_launch_refusal(argv, qualified) -> str | None:
    """Why a preserving Codex attempt must not launch: only the built-in runner verifies the hand-off, and the
    explicit binary must still be the one recorded at planning."""
    if common.CODEX_RUN or list(argv[:2]) != codex_launcher():
        return 'preserving Codex start refused: only the built-in runner verifies the qualified binary hand-off'
    problem = common.codex_bin_identity_problem(qualified)
    return f'preserving Codex start refused at launch: {problem}' if problem else None


def _run_codex(job_id, argv, cwd, timeout_s, files, params, policy=None, qualified=_ORDINARY) -> dict:
    """`qualified` (a preserving start): the binary identity recorded at planning, the only binary it may run."""
    timed_out = False
    preserving = qualified is not _ORDINARY or _PRESERVING_METADATA.get()
    qualified = None if qualified is _ORDINARY else qualified
    # preflight() (codex_credit_refusal) is what keeps credits from being spent; the adapter flag is belt and braces.
    env = worker_env(os.environ, job_id)
    if common.CODEX_RUN:
        env['TWINLANE_CODEX_ADAPTER_STRICT_CREDITS'] = '1'
    cx = (policy or {}).get('codex') or {}
    try:
        exec_config = common.codex_exec_config(policy)
        extra_secrets = common.extra_secret_patterns(policy)
        if common.CODEX_RUN and exec_config:
            env['TWINLANE_CODEX_EXEC_CONFIG'] = json.dumps(exec_config)
    except ValueError as e:
        return {'status': 'failed', 'exit_code': common.EXIT_USAGE, 'error': f'codex policy refused: {e}'}
    stripped = None
    if params.get('profile') == 'code':  # a writing worker gets no secret-named variables (names go in the receipt)
        env, stripped = common.strip_secret_env(env, extra_secrets)
    # Fail closed: never "whatever codex is first on PATH" unless codex.binary_source allows it. The lease covers
    # lookup to child start; after that Windows refuses to delete the running codex.exe. A child whose kill
    # failed keeps its lease. A preserving start never looks up, copies or leases: it runs the qualified binary.
    if preserving:
        refused = _preserving_launch_refusal(argv, qualified)
        if refused:
            return {'status': 'refused', 'exit_code': common.EXIT_REFUSED, 'error': refused}
        binary, why, lease = qualified['path'], f"TWINLANE_CODEX_BIN as set (qualified standalone): {qualified['path']}", None
    else:
        try:
            binary, why, lease = common.acquire_codex_binary(cx.get('binary_source') or 'auto')
        except (OSError, RuntimeError, ValueError) as e:
            binary, why, lease = None, f'lookup failed: {common.clip(str(e) or type(e).__name__, 200)}', None
    if not binary:
        return {'status': 'failed', 'exit_code': common.EXIT_MISSING, 'error': f'no usable codex CLI: {why}'}
    too_old = common.codex_version_refusal(binary, cx.get('min_cli_version'))
    if too_old:
        common.release_codex(lease)
        return {'status': 'refused', 'exit_code': common.EXIT_REFUSED, 'error': f'codex CLI refused: {too_old}'}
    if preserving:
        if cx.get('min_cli_version') not in (None, ''):  # the version probe ran the file: still the recorded bytes?
            refused = _preserving_launch_refusal(argv, qualified)
            if refused:
                return {'status': 'refused', 'exit_code': common.EXIT_REFUSED, 'error': refused}
        env[common.CODEX_BIN_QUALIFIED] = json.dumps(qualified, sort_keys=True)
        # Mode is explicit on argv as well: losing the environment hand-off must never
        # downgrade a preserving runner to ordinary binary/PATH lookup.
        if '--preserve-existing-metadata' not in argv:
            argv.append('--preserve-existing-metadata')  # retain the actual child argv in the attempt receipt
    else:
        env.pop(common.CODEX_BIN_QUALIFIED, None)  # a stray hand-off never constrains an ordinary start
    env['TWINLANE_CODEX_BIN'] = binary
    child_alive = False
    proc = None
    stall = watchdog.make([files['codex_log']] + ([os.path.join(files['codex_out'], 'events.jsonl')]
                                                   if files.get('codex_out') else []), policy)
    unresolved = False
    try:
        _write_job_json(job_id, CHILD_STARTING, {'engine': 'codex'})
        with open(files['codex_log'], 'wb') as log:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=log,
                                    stderr=subprocess.STDOUT,
                                    creationflags=NO_WINDOW | (common.CREATE_SUSPENDED if common.IS_WIN else 0),
                                    start_new_session=not common.IS_WIN)
            child_created = None
            try:
                common.contain_engine(proc, before_resume=lambda contained: contained
                                      or _hold_uncontained(job_id, 'codex'))
                child_created = common.process_created(proc.pid)
                _write_job_json(job_id, CHILD_STARTING,
                                  {'engine': 'codex', 'pid': proc.pid, 'created': child_created})
                _merge_status(job_id, child_pid=proc.pid if child_created else None, child_created=child_created)
                try:
                    rc = watchdog.wait(proc, timeout_s, stall, watchdog.settings(policy)['poll_s'],
                                       child_created=child_created,
                                       kill=_watchdog_kill())
                    unresolved = not _note_unresolved_tree(job_id, 'codex', stall)
                    if rc is None:  # only a stall kill returns None: the tree outlived it
                        raise subprocess.TimeoutExpired(argv, KILL_GRACE_S)
                except subprocess.TimeoutExpired:
                    timed_out = True
                    killed = common.kill_tree(proc.pid)
                    if common.IS_WIN and killed is not True:
                        unresolved = not _note_unresolved_tree(job_id, 'codex', stall, kill_confirmed=False)
                    try:
                        rc = proc.wait(timeout=KILL_GRACE_S)
                    except subprocess.TimeoutExpired:
                        rc, child_alive = None, True
                        _note_survivor(job_id, 'codex', proc, child_created, SURVIVED_TIMEOUT_KILL.format(
                            pid=proc.pid, kill='reported success' if killed else 'failed'))
            except BaseException:
                unresolved = not _note_unresolved_tree(job_id, 'codex', stall)  # see the claude runner
                child_alive = _cleanup_started_child(job_id, 'codex', proc, child_created,
                                                    'engine survived its kill after a post-start failure')
                raise
        c = parse_codex_out(files['codex_out'])  # consume the result before closing the attempt job
    except OSError as e:
        return {'status': 'failed', 'exit_code': common.EXIT_MISSING, 'error': f'codex launch or bookkeeping failed: {e}'}
    finally:
        unresolved, interrupt = _close_attempt_job(job_id, 'codex', proc, unresolved)
        _clear_child_starting(job_id, proc, keep=unresolved)
        if not child_alive:
            common.release_codex(lease)
        if interrupt is not None:
            raise interrupt
    served = [c['model']] if c['model'] else ([params['model']] if params.get('model') else [])
    out = {'session_id': c['session_id'], 'served_models': served, 'served_models_note':
           'requested model; Codex CLI may not report the served model',
           'num_turns': None, 'limit_hit': False, 'final_text': c['final_text'], 'exit_code': rc,
           'events_path': os.path.join(files['codex_out'], 'events.jsonl'), 'stderr_path': files['codex_log'],
           'result_line': c['result_line'], 'tokens': c['tokens'], 'codex_bin': binary}
    if stripped is not None:
        out['env_stripped'] = stripped
    tokens = _codex_tokens(c['session_id'], out['events_path'], c['tokens'])
    if tokens:
        out['token_usage'] = tokens
    if why:  # an older copy, the PATH, or an explicit binary instead of the current app build
        out['codex_note'] = f'codex CLI: {why}'
    sandbox_failed = _sandbox_setup_failed(files)
    if sandbox_failed:
        out['codex_note'] = common.clip(f"{out.get('codex_note', 'codex CLI:')} | the Codex sandbox could not set up "
                                        f"('{SANDBOX_SETUP_MARKER}'): its commands may not have run, so treat the "
                                        'result as unverified', 600)
    if child_alive:
        out['codex_note'] = common.clip(f"{out.get('codex_note', 'codex CLI:')} | the child survived its kill; its "
                                        'copy stays leased', 600)
    if timed_out or rc == 124:
        out['status'] = 'timeout'
    elif rc == 3:
        out['status'] = 'refused'
        out['error'] = 'Codex runner refused'
    elif rc == 0 and c['final_text'].strip():
        out['status'] = 'done'  # a finished run is never a limit hit, whatever its output mentions
    elif c['limit_hit']:
        out['status'], out['limit_hit'] = 'limit_hit', True
    else:
        out['status'] = 'failed'
    out['codex_error'] = c.get('error_text')
    if sandbox_failed and out['status'] == 'done':
        out['status'] = 'failed'
        out['error'] = ("the Codex sandbox could not set up ('setup refresh had errors'); "
                        'its commands may not have run: result unverified')
    if out['status'] in ('failed', 'limit_hit', 'timeout') and not out.get('error'):
        out['error'] = c.get('error_text') or f'codex exited rc {rc} with no error event'
        if out['status'] == 'timeout':
            out['error'] = f'timed out after {timeout_s} s' + (f": {c['error_text']}" if c.get('error_text') else '')
    return _mark_stalled(out, stall)


# ---------------------------------------------------------------- failover

def continuation_brief(job_id: str, to_engine: str, profile: str, original: str, prev: dict) -> str:
    turns = prev.get('num_turns')
    last = common.clip((prev.get('final_path') and _read_text(prev['final_path'])) or '(no message captured)', 4000)
    note = (f"The previous engine ({prev['engine']}) stopped at its usage limit after "
            f"{turns if turns is not None else 'an unknown number of'} turns. Its partial work is in this worktree "
            f"(see git diff); its last message follows:" + '\n\n' + last)
    return compose_brief(job_id, to_engine, profile, original + '\n\n---\n\n' + note)


def plan_failover(spec: dict, prev: dict, policy: dict) -> tuple[dict | None, str]:
    """Params for the other engine, or (None, reason). Routing still gates it: no swap into an engine
    that is out, stopped or unknown."""
    other = _other(prev['engine'])
    try:
        meter = _meter(policy, auto_refresh=other != 'codex')
        d = _route(spec['kind'], meter, policy, spec.get('urgent', False), other)
    except Exception as e:  # noqa: BLE001 - routing unavailable means no failover, never a blind one
        return None, f'routing unavailable: {type(e).__name__}: {common.clip(e, 160)}'
    if d.get('refused') or d.get('engine') != other:
        return None, f"route refused {other}: {d.get('reason')}"
    return engine_params(other, spec['kind'], d, policy, spec['profile'], timeout=spec.get('timeout_override')), ''


# ---------------------------------------------------------------- runner

def _main_checkout(cwd: str) -> str | None:
    """The main checkout behind a linked worktree (peers work there), or None."""
    rc, common_dir = common.git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if rc == 0 and os.path.basename(common_dir.rstrip('\\/')) == '.git':
        return os.path.dirname(common_dir.rstrip('\\/'))
    return None


HANDOFF = 'runner.env.json'   # env hand-off of a WMI-started runner (envhandoff)
RUNNER_LOCK = 'runner.lock'   # "<pid> <created>" of the one runner executing the job
STOP_REQUESTED = 'stop.requested'  # written by `twinlane stop` first; a runner launches nothing after it
UNSTAMPED_LOCK_GRACE_S = 600  # an unstamped lock is stale after the job's timeout plus this
UNSTAMPED_LOCK_DEFAULT_TIMEOUT_S = 7200  # when job.json has no timeout


def _unstamped_lock_stale(job_id: str, path: str) -> bool:
    """twinlane always stamps its locks, so an unstamped one is legacy or corrupt and its pid may have
    been reused: stale once older than the job's timeout + UNSTAMPED_LOCK_GRACE_S, held before."""
    spec = common.read_json(_p(job_id, 'job.json'), {}) or {}
    try:
        timeout = int((spec.get('primary') or {}).get('timeout_s') or UNSTAMPED_LOCK_DEFAULT_TIMEOUT_S)
    except (TypeError, ValueError):
        timeout = UNSTAMPED_LOCK_DEFAULT_TIMEOUT_S
    try:
        return time.time() - os.path.getmtime(path) > timeout + UNSTAMPED_LOCK_GRACE_S
    except OSError:
        return False


def _claim_runner(job_id: str) -> tuple[int, int | None] | None:
    """Become the job's only runner: create runner.lock with O_EXCL. Returns None when claimed, or
    the (pid, created) of the LIVE runner that holds it. A lock naming a dead process is stale and
    taken over; the claim runs under a short OS lock so two runners cannot both take it over."""
    me = (os.getpid(), _self_created())
    if me[1] is None:
        raise OSError('runner creation stamp is unavailable; refusing an unstamped lock')
    with lifecycle_lock(job_id, 'runner.claim'):
        path = _p(job_id, RUNNER_LOCK)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        except FileExistsError:
            parts = _read_text(path).split()
            if _PRESERVING_METADATA.get():
                # A prior/ambiguous runner claim is protected, even if now DEAD or ours.
                if len(parts) == 2 and len(parts[0]) <= 10 and len(parts[1]) <= 20 \
                        and all(p.isascii() and p.isdigit() for p in parts):
                    return int(parts[0]), int(parts[1])
                return 0, None
            pid = int(parts[0]) if parts and parts[0].isdigit() else None
            created = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
            # Unknown (exists, identity unreadable) holds the lock too: two runners on one job is the worse failure.
            if pid and (pid, created) != me and common.pid_maybe_alive(pid, created) and \
                    (created is not None or not _unstamped_lock_stale(job_id, path)):
                return pid, created
            os.remove(path)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, 'w', encoding='ascii') as h:
            h.write(f'{me[0]} {me[1] or ""}'.strip())
    return None


def _release_runner(job_id: str) -> None:
    with lifecycle_lock(job_id, 'runner.claim'):
        path = _p(job_id, RUNNER_LOCK)
        if _PRESERVING_METADATA.get() and _read_text(path).split() != [str(os.getpid()), str(_self_created())]:
            return
        if _read_text(path).split()[:1] == [str(os.getpid())]:
            try:
                os.remove(path)
            except OSError:
                pass


def _writer_cwd(cwd: str) -> str:
    return common.writer_key(cwd)


def _writer_path(cwd: str) -> str:
    key = hashlib.sha256(_writer_cwd(cwd).encode('utf-8')).hexdigest()
    return os.path.join(common.STATE, 'writers', key + '.json')


def _tree_unresolved(job_id: str) -> bool:
    """A stalled engine tree that was not proven stopped holds the claim: either its own marker, or (when that could
    not be written) the launch marker rewritten without a pid, which no check of the dead root can reap."""
    if os.path.exists(_p(job_id, TREE_UNRESOLVED)):
        return True
    pending = common.read_json(_p(job_id, CHILD_STARTING))
    return isinstance(pending, dict) and pending.get('unresolved_tree') is True


def _writer_job_busy(job_id: str) -> bool:
    """Live/unknown engines hold the directory, except qualified post-exit child PID reuse."""
    path = _p(job_id, 'status.json')
    st = common.read_json(path)
    if st is None and os.path.exists(path):
        return True
    if st is not None and not isinstance(st, dict):
        return True
    if os.path.exists(_p(job_id, SURVIVOR)) and _survivor(job_id) is None:
        return True
    if _tree_unresolved(job_id):
        return True
    engine = _engine_state(job_id, st or {})
    return engine != common.DEAD and not (engine == common.UNKNOWN and
                                         _finished_child_pid_reused(job_id, st or {}))


CUSTODY_GRACE_S = 120  # a finished runner exits within seconds; this long after its receipt, an unreadable pid is stale


def _custody_stale(job_id: str) -> str | None:
    """Why the writer claim held for `job_id` is provably stale, or None when it must keep blocking.

    Only a finished job qualifies: a terminal receipt, no survivor or unresolved child marker, and no process
    that is provably still the job's runner or engine. A runner whose pid cannot be read (a reused pid, or a
    probe failing under host overload) is accepted as gone once its receipt is older than CUSTODY_GRACE_S and
    the runner marked itself finalized: that is the case that left a finished job holding its worktree."""
    receipt = common.read_json(_p(job_id, 'result.json'))
    st = common.read_json(_p(job_id, 'status.json'))
    if not isinstance(receipt, dict) or receipt.get('status') not in TERMINAL \
            or isinstance(receipt.get('survivor'), dict) or (st is not None and not isinstance(st, dict)):
        return None
    if any(os.path.exists(_p(job_id, marker)) for marker in (CHILD_STARTING, SURVIVOR, TREE_UNRESOLVED)):
        return None
    st = st or {}
    pid, created = ((st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(job_id))
    states = [_pid_state(pid, created), _child_state(st)]
    if common.ALIVE in states:
        return None
    if common.UNKNOWN not in states:
        return 'job finished and its runner and engine are gone'
    ended = common.parse_time(receipt.get('ended_at')) or common.parse_time(st.get('updated_at'))
    if st.get('runner_finalized') is True and ended is not None \
            and (common.now() - ended).total_seconds() >= CUSTODY_GRACE_S:
        return (f'job finished {int((common.now() - ended).total_seconds())} s ago and marked itself finalized; '
                'its pid identity cannot be read (reused pid or failing probe), so it is taken as gone')
    return None


def _log_custody_release(job_id: str, cwd, why: str) -> None:
    """One JSON line per automatic release, so a claim never disappears without a record."""
    try:
        with open(os.path.join(common.STATE, 'custody-releases.jsonl'), 'a', encoding='utf-8') as h:
            h.write(json.dumps({'at': common.iso(common.now()), 'job': job_id, 'cwd': cwd, 'why': why},
                               sort_keys=True) + '\n')
    except OSError:
        pass


def stale_writers_snapshot(deadline: float | None = None) -> list:
    """Read-only custody evidence, without locks or writes. Any incomplete claim read raises.

    A missing writers folder is complete and empty; other listing failures, vanished/unreadable
    claims and malformed owners cannot prove earlier custody warnings resolved. Release still
    takes its own lock and revalidates each claim through release_stale_writers.
    """
    folder = os.path.join(common.STATE, 'writers')
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.json'))
    except FileNotFoundError:
        return []
    out = []
    def object_file(path):
        try:
            with open(path, encoding='utf-8') as handle:
                value = json.load(handle)
        except FileNotFoundError:
            return None
        if not isinstance(value, dict):
            raise ValueError('incomplete custody evidence: ' + os.path.basename(path))
        return value

    def present(path):
        try:
            os.stat(path)
            return True
        except FileNotFoundError:
            return False

    with common.nonspawning_liveness(), observations.scope():
        for name in names:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('custody scan budget exhausted')
            with open(os.path.join(folder, name), encoding='utf-8') as handle:
                owner = json.load(handle)
            if not isinstance(owner, dict) or not isinstance(owner.get('id'), str):
                raise ValueError('unreadable writer claim: ' + name)
            jid = owner['id']
            try:
                os.stat(_p(jid, ''))
            except FileNotFoundError:
                why = 'owner job folder is gone and its runner pid is dead' \
                    if _pid_state(owner.get('pid'), owner.get('created')) == common.DEAD else None
            else:
                # Read once, strictly. _custody_stale's tolerant None is also used by
                # admission/release and cannot distinguish an unreadable receipt.
                receipt = object_file(_p(jid, 'result.json'))
                st = object_file(_p(jid, 'status.json')) or {}
                if receipt is None and st.get('status') not in ('queued', 'running', 'waiting'):
                    raise ValueError('incomplete custody evidence: missing receipt/status')
                why = None
                markers = [present(_p(jid, m)) for m in (CHILD_STARTING, SURVIVOR, TREE_UNRESOLVED)]
                if receipt is not None and receipt.get('status') not in TERMINAL:
                    raise ValueError('incomplete custody evidence: invalid receipt status')
                if receipt is not None and not isinstance(receipt.get('survivor'), dict) and not any(markers):
                    pid, created = st.get('pid'), st.get('pid_created')
                    if not pid:
                        try:
                            with open(_p(jid, 'runner.pid'), encoding='utf-8') as handle:
                                parts = handle.read().split()
                        except FileNotFoundError:
                            parts = []
                        if parts and (len(parts) not in (1, 2) or not all(p.isascii() and p.isdigit() for p in parts)):
                            raise ValueError('incomplete custody evidence: invalid runner identity')
                        pid = int(parts[0]) if parts else None
                        created = int(parts[1]) if len(parts) == 2 else None
                    states = [_pid_state(pid, created), _child_state(st)]
                    if common.ALIVE not in states:
                        if common.UNKNOWN not in states:
                            why = 'job finished and its runner and engine are gone'
                        else:
                            ended = common.parse_time(receipt.get('ended_at')) or common.parse_time(st.get('updated_at'))
                            if st.get('runner_finalized') is True and ended is not None and \
                                    (common.now() - ended).total_seconds() >= CUSTODY_GRACE_S:
                                why = 'job finished past custody grace and marked itself finalized; pid identity unreadable'
                            else:
                                raise ValueError('incomplete custody evidence: process identity unreadable')
            if why:
                out.append({'id': jid, 'cwd': owner.get('cwd'), 'why': why, 'released': False})
    return out


def release_stale_writers(dry_run: bool = False, deadline: float | None = None) -> list:
    """Remove writer claims whose job is finished and gone (see _custody_stale). -> rows with `released`."""
    out = []
    folder = os.path.join(common.STATE, 'writers')
    try:
        names = sorted(n for n in os.listdir(folder) if n.endswith('.json'))
    except OSError:
        return out
    # a lead command (patrol, wt doctor --fix): it fails fast, only job runners wait on the long bound
    wait_until = time.monotonic() + WRITER_SWEEP_WAIT_S if deadline is None else deadline
    with _job_start_lock('writers', deadline=wait_until):
        for name in names:
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError('custody scan budget exhausted')
            path = os.path.join(folder, name)
            owner = common.read_json(path)
            if not isinstance(owner, dict) or not isinstance(owner.get('id'), str):
                continue
            jid = owner['id']
            if not os.path.isdir(_p(jid, '')):
                # No job folder (archived or deleted): stale only if its runner is provably gone.
                why = 'owner job folder is gone and its runner pid is dead' \
                    if _pid_state(owner.get('pid'), owner.get('created')) == common.DEAD else None
            else:
                why = _custody_stale(jid)
            if not why:
                continue
            row = {'id': jid, 'cwd': owner.get('cwd'), 'why': why, 'released': False}
            if not dry_run:
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError('custody release budget exhausted')
                try:
                    os.remove(path)
                    row['released'] = True
                    _log_custody_release(jid, owner.get('cwd'), why)
                except OSError as e:
                    row['error'] = str(e)
            out.append(row)
    return out


def _claim_writer(spec: dict) -> str | None:
    """Reserve the canonical writer directory across job IDs and continuation lineages.

    The OS lock serializes claim/reclaim. The durable owner remains if a child survives or its
    identity cannot be established; no age-based eviction can silently admit a second writer.
    """
    if spec.get('profile') != 'code':
        return None
    cwd, jid = _writer_cwd(spec['cwd']), spec['id']
    rc, top = common.git(spec['cwd'], 'rev-parse', '--show-toplevel')
    if rc == 0 and os.path.normcase(os.path.realpath(top.strip())) != cwd:
        raise Refusal(common.EXIT_REFUSED,
                      f'cannot establish the worktree root: git says {top.strip()}, the folder walk says {cwd}')
    path = _writer_path(cwd)
    with _job_start_lock('writers', jid):
        if os.path.exists(path):
            if _PRESERVING_METADATA.get():
                raise Refusal(common.EXIT_REFUSED, 'existing writer reservation is protected; no replacement or cleanup')
            owner = common.read_json(path)
            if not isinstance(owner, dict) or owner.get('cwd') != cwd or not isinstance(owner.get('id'), str) \
                    or not isinstance(owner.get('pid'), int) or not isinstance(owner.get('created'), int):
                raise Refusal(common.EXIT_REFUSED, 'writer reservation is unreadable or incomplete; ownership is unknown')
            blocked = f"writer directory is owned by {owner['id']} (live or unknown); wait for its owner"
            stale = None
            if _pid_state(owner['pid'], owner['created']) != common.DEAD or _writer_job_busy(owner['id']):
                stale = _custody_stale(owner['id'])
                if not stale:
                    raise Refusal(common.EXIT_REFUSED, blocked)
            else:
                receipt = common.read_json(_p(owner['id'], 'result.json'))
                if isinstance(receipt, dict) and receipt.get('status') in TERMINAL \
                        and _repair_finished_status(owner['id'], require_dead_runner=True) == 'rejected':
                    stale = _custody_stale(owner['id'])
                    if not stale:
                        raise Refusal(common.EXIT_REFUSED, blocked)
            if stale:
                _log_custody_release(owner['id'], cwd, stale)
        # Include pre-reservation runners, which can still exist when an installation is upgraded.
        with os.scandir(common.JOBS) as entries:
            existing = list(entries)
        for entry in existing:
            if entry.name == 'archive' or not entry.is_dir() or entry.name == jid:
                continue
            if not observations.JOB_DIRECTORY_RE.fullmatch(entry.name):
                raise Refusal(common.EXIT_REFUSED, f'cannot verify writer ownership: nonmatching job folder {entry.name}')
            spec_path = os.path.join(entry.path, 'job.json')
            if not os.path.exists(spec_path) and not any(os.path.exists(os.path.join(entry.path, f))
                    for f in ('status.json', 'runner.pid', RUNNER_LOCK, SURVIVOR, CHILD_STARTING, TREE_UNRESOLVED)):
                continue  # create_job allocated a folder but has not published its launchable spec yet.
            other = common.read_json(spec_path)
            if not isinstance(other, dict):
                raise Refusal(common.EXIT_REFUSED, f'cannot verify writer ownership: job {entry.name} is unreadable')
            if other.get('profile') != 'code' or not other.get('cwd') or _writer_cwd(other['cwd']) != cwd:
                continue
            st = common.read_json(os.path.join(entry.path, 'status.json'))
            if st is not None and not isinstance(st, dict):
                raise Refusal(common.EXIT_REFUSED, f'cannot verify writer ownership: status of {entry.name} is unreadable')
            if st is None and os.path.exists(os.path.join(entry.path, 'status.json')):
                raise Refusal(common.EXIT_REFUSED, f'cannot verify writer ownership: status of {entry.name} is unreadable')
            # Only newly versioned runners must acquire this same reservation before launching.
            # An older queued runner does not honor it and must conservatively keep blocking.
            if other.get('writer_reservation_version') == 1 and (st or {}).get('status') == 'queued' \
                    and not st.get('attempt') and not _writer_job_busy(entry.name):
                continue
            pid, created = ((st['pid'], st.get('pid_created')) if (st or {}).get('pid') else _runner_pid(entry.name))
            if _PRESERVING_METADATA.get() and (
                    not common._valid_pid(pid) or not common._valid_stamp(created)):
                raise Refusal(common.EXIT_REFUSED, f'writer identity of {entry.name} is incomplete or malformed; ownership is unknown')
            if other.get('writer_reservation_version') != 1 and (
                    not (st or {}).get('status') or (st or {}).get('status') == 'queued' or not pid):
                receipt = common.read_json(os.path.join(entry.path, 'result.json'))
                if not isinstance(receipt, dict) or not receipt.get('status'):
                    raise Refusal(common.EXIT_REFUSED,
                                  f'legacy writer {entry.name} has unqualified prelaunch ownership; recovery required')
            blocked = f'writer directory is owned by {entry.name} (live or unknown); wait for its owner'
            if _alive(pid, created) or _writer_job_busy(entry.name):
                stale = _custody_stale(entry.name)
                if not stale:
                    raise Refusal(common.EXIT_REFUSED, blocked)
                _log_custody_release(entry.name, cwd, stale)
                continue
            receipt = common.read_json(_p(entry.name, 'result.json'))
            if isinstance(receipt, dict) and receipt.get('status') in TERMINAL \
                    and _repair_finished_status(entry.name, require_dead_runner=True) == 'rejected':
                stale = _custody_stale(entry.name)
                if not stale:
                    raise Refusal(common.EXIT_REFUSED, blocked)
                _log_custody_release(entry.name, cwd, stale)
        created = _self_created()
        if created is None:
            raise Refusal(common.EXIT_REFUSED, 'writer identity unavailable; cannot reserve its directory')
        common.write_json(path, {'id': jid, 'cwd': cwd, 'pid': os.getpid(), 'created': created})
    return path


def _release_writer(path: str, job_id: str) -> None:
    try:
        with _job_start_lock('writers', job_id, report=False):
            owner = common.read_json(path)
            if isinstance(owner, dict) and owner.get('id') == job_id and not _writer_job_busy(job_id):
                os.remove(path)
    except OSError:
        pass  # keep an uncertain owner; another claimant must establish it is gone


def run_job(job_id: str, *, preserve_existing_metadata=False) -> dict:
    spec = common.read_json(_p(job_id, 'job.json'))
    required = preserve_existing_metadata or _PRESERVING_METADATA.get()
    if (required and not isinstance(spec, dict)) or (isinstance(spec, dict) and (
            ('preserve_existing_metadata' in spec and type(spec['preserve_existing_metadata']) is not bool)
            or (required and spec.get('preserve_existing_metadata') is not True))):
        return {'id': job_id, 'status': 'refused', 'exit_code': common.EXIT_REFUSED,
                'notes': ['metadata preservation mode missing or malformed; no runner claimed']}
    token = _REAPED_CHILDREN.set({})
    try:
        if isinstance(spec, dict) and spec.get('preserve_existing_metadata') is True:
            with preserving_metadata(job_id):
                return _run_job(job_id)
        return _run_job(job_id)
    finally:
        _REAPED_CHILDREN.reset(token)


def _run_job(job_id: str) -> dict:
    spec = common.read_json(_p(job_id, 'job.json'))
    if not isinstance(spec, dict):
        raise SystemExit(f'twinlane: no job {job_id}')
    if _PRESERVING_METADATA.get() and spec.get('preserve_existing_metadata') is not True:
        return {'id': job_id, 'status': 'refused', 'exit_code': common.EXIT_REFUSED,
                'notes': ['metadata preservation mode changed; no runner claimed']}
    if _PRESERVING_METADATA.get() and os.path.exists(_p(job_id, 'result.json')):
        return {'id': job_id, 'status': 'refused', 'exit_code': common.EXIT_REFUSED,
                'notes': ['existing terminal job is protected; no replay or runner claimed']}
    holder = _claim_runner(job_id)
    if holder:
        # Another live runner owns this job: touch nothing (no status, no receipt, no engine).
        return {'id': job_id, 'status': 'running', 'duplicate_runner': True, 'holder_pid': holder[0],
                'notes': [f'job {job_id} already has a live runner (pid {holder[0]}); this one exits']}
    writer = None
    try:
        done = common.read_json(_p(job_id, 'result.json'))
        if isinstance(done, dict):
            repair_owner(job_id)
            # Finished (or stopped/refused before this runner started): never run it again.
            return {**done, 'duplicate_runner': True,
                    'notes': [f'job {job_id} already has a receipt ({done.get("status")}); this runner exits']}
        if spec.get('coordination'):
            from . import coordination
            try:
                coordination.validate_runner(spec, job_id)
            except coordination.Refusal as e:
                status = common.read_json(_p(job_id, 'status.json'), {}) or {}
                if _engine_state(job_id, status) != common.DEAD \
                        or os.path.exists(_p(job_id, 'result.json')):
                    return {'id': job_id, 'status': 'running', 'duplicate_runner': True,
                            'notes': ['coordination refused; existing/unknown engine or receipt preserved']}
                # A dead engine is not proof that no attempt ever started. Preserve
                # its lost/recoverable evidence rather than inventing a new receipt.
                if any(status.get(key) for key in ('attempt', 'started_at', 'child_pid', 'child_created')) \
                        or os.path.exists(_p(job_id, SURVIVOR)):
                    return {'id': job_id, 'status': status.get('status') or 'lost',
                            'duplicate_runner': True, 'coordination_refused': True,
                            'notes': ['coordination refused; prior attempt evidence preserved']}
                receipt = {'id': job_id, 'engine': spec['engine'], 'status': 'refused',
                           'exit_code': common.EXIT_REFUSED, 'error': str(e),
                           'notes': ['no engine started; coordination admission refused']}
                _write_receipt(job_id, receipt)
                _write_status(job_id, 'refused', engine=spec['engine'])
                return receipt
        try:
            writer = _claim_writer(spec)
        except (Refusal, OSError) as error:
            receipt = build_receipt(spec, [{'n': 1, 'engine': spec['primary']['engine'], 'status': 'refused',
                                            'error': str(error)}], {}, common.now(), None,
                                    ['writer admission refused; no engine started'], meter_after=None)
            _write_receipt(job_id, receipt)
            return receipt
        return _run_claimed(job_id, spec)
    finally:
        if writer:
            _release_writer(writer, job_id)
        _release_runner(job_id)
        if writer and not _PRESERVING_METADATA.get():
            # A writer lane just freed up: start the next queued writer request, if one is ready.
            from . import wip
            wip.pump_quietly()


def _run_claimed(job_id: str, spec: dict) -> dict:
    t0 = common.now()
    attempts, notes, failover = [], list(spec.get('notes') or []), None
    if CONTAIN and common.IS_WIN and not common.contain_children():
        notes.append('engine children are not in a kill-on-close job object (the OS refused)')
    linked = common.is_linked_worktree(spec['cwd'])
    main_root = _main_checkout(spec['cwd']) if linked and spec['profile'] == 'code' else None
    before = main_before = None
    trip = {'touched_files': None, 'head_moved': None}
    if _stop_requested(job_id):
        # `twinlane stop` ran before this runner could be seen (no pid yet): launch nothing.
        notes.append('stop requested before the runner started; nothing launched')
        receipt = build_receipt(spec, attempts, trip, t0, failover, notes, linked)
        receipt['status'] = 'stopped'
        _write_receipt(job_id, receipt)  # nothing launched: status.json stays as the queue left it
        return receipt
    stop_hit = interrupted = False
    policy, seen = None, {}
    try:
        policy = common.load_policy()
        err = _baton(spec.get('baton'), _start_receipt(spec))
        if err:
            notes.append(f'baton start: {err}')
        params, brief_path, resume = spec['primary'], _p(job_id, 'brief.md'), spec.get('resume')
        stalls = 0
        for n in range(1, MAX_ATTEMPTS + 1):
            waited = acquire_slot(job_id, params['engine'], n, policy)
            if before is None:
                # After the slot wait, so queue time does not count as the job's own window.
                before = tripwire_snapshot(spec['cwd'])
                main_before = tripwire_snapshot(main_root) if main_root else None
            why, wait_until = preflight(spec, params, policy, fresh=waited, seen=seen)
            if not why and n == 1:
                why = _continue_blocked(spec.get('continue_job')) or next(
                    filter(None, (_continue_blocked(j, f'--resume (session of job {j})')
                                  for j in spec.get('resume_jobs') or ())), None)
            if why:
                attempts.append({'n': n, 'engine': params['engine'], 'status': 'refused', 'error': why,
                                 'wait_until': wait_until, 'started_at': common.iso(common.now())})
                break
            if _stop_requested(job_id):
                notes.append(f'stop requested; attempt {n} was not launched')
                stop_hit = True
                break
            a = run_attempt(job_id, n, params, brief_path, spec, policy, resume)
            attempts.append(a)
            if a['status'] == 'stalled':
                # A hung worker was killed. Requeue once (the slot wait applies the host-load limit again);
                # a second stall means the host cannot run it: fail, with no verdict of any kind.
                stalls += 1
                alive = _survivor(job_id)
                if a.get('stall_kill_confirmed') is not True or alive and _survivor_state(alive) != common.DEAD:
                    # The kill of the tree is unproven, or an engine outlived it: a second attempt could write
                    # into the same worktree while the first still runs. No retry; custody stays with the job.
                    a['status'] = 'failed'
                    a['error'] = ('host overload: ' + a['error'] + '; its process tree was not proven stopped, '
                                  'so the job was not retried')
                    notes.append(f"attempt {n}: {a['error']}")
                    break
                if stalls <= watchdog.STALL_REQUEUES and n < MAX_ATTEMPTS and not _stop_requested(job_id):
                    a['requeued'] = True
                    notes.append(f"attempt {n} stalled ({a['error']}); its process tree was killed and the job "
                                 'was requeued once')
                    continue
                a['status'] = 'failed'
                if _stop_requested(job_id):
                    a['error'] = 'stalled and a stop was requested: ' + a['error']
                elif stalls <= watchdog.STALL_REQUEUES:
                    a['error'] = 'host overload: ' + a['error']
                else:
                    a['error'] = f"host overload: stalled {stalls} times ({a['error']})"
                notes.append(f"attempt {n}: {a['error']}; the work did not run to a result")
                break
            if a['status'] == 'limit_hit' and n < MAX_ATTEMPTS and spec.get('failover') and _tree_unresolved(job_id):
                # The attempt's tree is not proven gone: a second engine could write beside a survivor.
                notes.append(f'attempt {n}: its process tree was not proven stopped, so no failover was started')
                break
            if a['status'] != 'limit_hit' or n >= MAX_ATTEMPTS or not spec.get('failover') \
                    or _stop_requested(job_id):
                break
            nxt, why = plan_failover(spec, a, policy)
            if nxt and nxt['engine'] == 'codex':
                pf = common.codex_preflight(policy)
                if pf['refusal']:
                    nxt, why = None, pf['refusal']
            if nxt is None:
                a['failover_refused'] = why
                break
            brief_path = _p(job_id, 'brief-a2.md')
            _write_job_text(job_id, os.path.basename(brief_path), continuation_brief(job_id, nxt['engine'], spec['profile'],
                                                             spec['brief_text'], a))
            failover = {'from': a['engine'], 'to': nxt['engine'], 'reason': 'limit_hit'}
            params, resume = nxt, None
        if before is not None:
            trip = tripwire_diff(before, tripwire_snapshot(spec['cwd']), spec['cwd'])
        if main_before is not None:
            mt = tripwire_diff(main_before, tripwire_snapshot(main_root), main_root)
            trip['main_checkout'] = {'path': main_root, 'changes': mt.get('touched_files'),
                                     'head_moved': mt.get('head_moved')}
    except BaseException as e:  # noqa: BLE001 - a crashed or interrupted runner must still leave a receipt
        interrupted = isinstance(e, (KeyboardInterrupt, SystemExit))
        notes.append(f"runner {'interrupted' if interrupted else 'error'}: {type(e).__name__}: {common.clip(e, 300)}")
        if not attempts or attempts[-1].get('status') in (None, 'running'):
            attempts.append({'n': len(attempts) + 1, 'engine': spec['primary']['engine'],
                             'status': 'interrupted' if interrupted else 'failed', 'error': str(e)})
        elif interrupted:
            attempts[-1]['status'] = 'interrupted'
        elif attempts[-1].get('status') == 'stalled':
            # The retry of a stalled attempt could not start. `stalled` is not terminal: fail it, stall evidence kept.
            attempts[-1]['status'] = 'failed'
            attempts[-1]['error'] = (f"host overload: {attempts[-1].get('error')}; "
                                     f'the retry could not start ({type(e).__name__})')
    usage, meter_after = _job_usage(job_id, spec, attempts, seen, t0, policy, quick=interrupted)
    if usage and usage.get('after_note'):
        notes.append(usage['after_note'])
    if usage and usage.get('cache_note'):
        notes.append(usage['cache_note'])
    receipt = build_receipt(spec, attempts, trip, t0, failover, notes, linked, usage=usage, meter_after=meter_after)
    # `twinlane stop` never writes the receipt while this runner lives: the request alone decides here.
    if stop_hit or _stop_requested(job_id) and receipt['status'] != 'done':
        receipt['status'] = 'stopped'
    elif _stop_requested(job_id):
        receipt['notes'].append('stop requested after the work finished; the receipt stays done')
    _write_runner_receipt(job_id, receipt)
    err = _baton(spec.get('baton'), receipt)
    if err:
        receipt['notes'].append(f'baton end: {err}')
        _write_runner_receipt(job_id, receipt)
    return receipt


def _stop_requested(job_id: str) -> bool:
    return os.path.exists(_p(job_id, STOP_REQUESTED))


def _start_receipt(spec: dict) -> dict:
    p = spec['primary']
    return {'id': spec['id'], 'name': spec.get('name'), 'status': 'running', 'engine': p['engine'],
            'requested': {k: p.get(k) for k in ('model', 'effort', 'tier', 'profile')}, 'session_id': None,
            'cwd': spec['cwd'], 'started_at': spec.get('created_at')}


# ---------------------------------------------------------------- per-job usage

USAGE_BEFORE_LAG_S = 120   # a 'before' reading older than this at the start makes the delta approximate
USAGE_AFTER_LAG_S = 45     # an 'after' reading taken more than this before the end is re-read once
AFTER_READ_TIMEOUT_S = 10  # default for policy meter.after_read_timeout_s: the whole end-of-job meter read
OVERLAP_SCAN_S = 5.0       # default for policy meter.overlap_scan_timeout_s: time budget of the overlap scan
AFTER_TIMEOUT_NOTE = 'usage after: meter read timed out'
# A replace that began before the deadline may still land after it, so the note
# says the write was pending, never that the cache was left unchanged.
CACHE_DEADLINE_NOTE = 'meter cache write still pending at the deadline'
USAGE_NOTE = ('percentage points of each account window; the meters resolve about 1 point, and any other use '
              'of the account during the job is included')


def _job_created(jid: str):
    """The creation time encoded in a job id (<YYYYmmdd-HHMMSS>-<engine>-<slug>, UTC), or None."""
    m = re.match(r'(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})-', jid)
    return common.parse_time('%s-%s-%sT%s:%s:%sZ' % m.groups()) if m else None


def _overlapping_jobs(job_id: str, start, end, budget_s: float = OVERLAP_SCAN_S) -> tuple:
    """-> ({engine: [job ids]}, truncated). Every OTHER job folder is checked: one created after `end` cannot
    have run in the window, any other is judged by its receipt's attempt times (or, while it still runs, its
    start). The scan runs in a daemon worker and the caller waits at most `budget_s` of wall-clock time for it
    (one slow folder read cannot stretch that); `truncated` is True when anything stopped the scan early (the
    budget, an unreadable folder or listing), so the caller must label the delta approximate instead of
    trusting an empty answer. After a timeout the answer holds what the worker had found so far."""
    if budget_s <= 0:
        return {}, True  # no time at all: nothing was scanned
    out: dict = {}
    state = {'truncated': False, 'done': False}
    guard_ = threading.Lock()
    stop = threading.Event()

    def add(eng, jid, s, e):
        if eng in ENGINES and s is not None and s < end and (e is None or e > start):
            with guard_:
                ids = out.setdefault(eng, [])
                if jid not in ids:
                    ids.append(jid)

    def scan():
        try:
            ids = sorted((jid for jid in os.listdir(common.JOBS)
                          if jid != 'archive'), reverse=True)
        except FileNotFoundError:
            ids = []
        except OSError:
            state['truncated'] = True
            return
        for jid in ids:
            if stop.is_set():
                return
            if jid == job_id or not os.path.isdir(_p(jid, '')):
                continue
            if not observations.JOB_DIRECTORY_RE.fullmatch(jid):
                state['truncated'] = True
            created = _job_created(jid)
            if created is not None and created > end:
                continue  # created after the window closed: it cannot have run inside it
            try:
                r = common.read_json(_p(jid, 'result.json'))
                if isinstance(r, dict):
                    for a in r.get('attempts') or []:
                        if isinstance(a, dict) and a.get('started_at') and (a.get('argv') or a.get('session_id')):
                            add(a.get('engine'), jid, common.parse_time(a['started_at']),
                                common.parse_time(a.get('ended_at') or r.get('ended_at')))
                    continue
                if job_status(jid) not in ('running', 'orphaned', 'unknown'):
                    continue
                spec = common.read_json(_p(jid, 'job.json'), {}) or {}
                st = common.read_json(_p(jid, 'status.json'), {}) or {}
                add((spec.get('primary') or {}).get('engine'), jid,
                    common.parse_time(st.get('started_at') or spec.get('created_at')), None)
            except Exception:  # noqa: BLE001 - an unreadable folder means the scan is incomplete, not that it is clear
                state['truncated'] = True

    def run():
        try:
            scan()
        except BaseException:  # noqa: BLE001 - a scan that died is an incomplete scan
            state['truncated'] = True
        finally:
            state['done'] = True

    context = copy_context()
    t = threading.Thread(target=context.run, args=(run,), daemon=True, name='twinlane-overlap-scan')
    t.start()
    t.join(max(0.0, budget_s))
    stop.set()
    with guard_:
        return {k: list(v) for k, v in out.items()}, state['truncated'] or not state['done']


def sum_tokens(attempts: list, engine: str) -> dict | None:
    """Token counts of one engine over all its attempts (sources named, never mixed silently)."""
    total: dict = {}
    sources = []
    for a in attempts:
        tu = a.get('token_usage')
        if a.get('engine') != engine or not isinstance(tu, dict):
            continue
        if tu.get('source') and tu['source'] not in sources:
            sources.append(tu['source'])
        for k, v in tu.items():
            if k != 'source' and isinstance(v, int):
                total[k] = total.get(k, 0) + v
    return {'source': ', '.join(sources), **total} if total else None


def _bounded_after_meter(policy: dict, timeout_s: float, notes: list | None = None) -> tuple:
    """-> (meter or None, timed_out). The cached reading when it was taken near the job's end, else one plain
    re-read, all inside ONE deadline: the read runs in a daemon thread, so a hung reader delays the receipt by
    at most `timeout_s` and never keeps the runner alive. The reader only RETURNS its reading: its cache
    writes are queued and performed in a second daemon thread under the SAME deadline, so the receipt never
    waits for the filesystem (a slow write, a busy cache lock, Windows sharing-violation retries). A write
    still running at the deadline is left pending: no replace STARTS after the deadline, but one already started
    may land later, and the cache file is only ever the old one or the complete new one. CACHE_DEADLINE_NOTE
    is appended to `notes`. A process that exits mid-write can leave a `.tmp-` file beside the cache.
    A reader that outlives the deadline changes nothing on disk. An exception in the read is re-raised."""
    from . import meter as meter_mod
    box: dict = {}
    deadline = time.monotonic() + max(0.0, timeout_s)

    def work():
        try:
            with meter_mod.deferred_cache_writes() as pending:
                m = _meter(policy, auto_refresh=False, admission=False)
                if any(((common.age_s((m.get(e) or {}).get('reading_at')) or 0) > USAGE_AFTER_LAG_S) for e in ENGINES):
                    m = _meter(policy, fresh=True, auto_refresh=False, admission=False)
            box['pending'], box['m'] = pending, m
        except BaseException as e:  # noqa: BLE001 - handed to the caller
            box['err'] = e

    context = copy_context()
    t = threading.Thread(target=context.run, args=(work,), daemon=True, name='twinlane-after-meter')
    t.start()
    t.join(max(0.0, timeout_s))
    if t.is_alive():
        return None, True
    if 'err' in box:
        raise box['err']
    if box['pending']:
        persisted: dict = {}

        def persist():
            try:
                persisted['skipped'] = meter_mod.run_deferred(box['pending'], deadline)
            except BaseException as e:  # noqa: BLE001 - the reading is already in hand; the cache is optional
                persisted['error'] = e

        w = threading.Thread(target=persist, daemon=True, name='twinlane-after-meter-cache')
        w.start()
        w.join(max(0.0, deadline - time.monotonic()))
        missed = w.is_alive() or (persisted.get('skipped') and time.monotonic() >= deadline)
        if notes is not None and (missed or 'error' in persisted):
            notes.append(CACHE_DEADLINE_NOTE if missed else
                         f"meter cache not updated ({type(persisted['error']).__name__})")
    return box['m'], False


def _job_usage(job_id: str, spec: dict, attempts: list, seen: dict, t0, policy, quick: bool) -> tuple:
    """-> (receipt `usage` block or None, the 'after' meter). The 'before' reading is the one the first
    attempt's preflight used; the 'after' reading is the cached one when it was taken near the job's
    end, else one non-model re-read (an interrupted job never waits for it), bounded by policy
    meter.after_read_timeout_s: on a timeout the block has no after reading and says so. No model call."""
    after = _cached_meter()
    ran = [a for a in attempts if a.get('argv') or a.get('session_id')]
    if not ran:
        return None, after
    from . import meter as meter_mod
    ended = common.now()
    try:
        timed_out = False
        cache_notes: list = []
        policy = policy or common.load_policy()
        mp = policy.get('meter') or {}
        if not quick:
            after, timed_out = _bounded_after_meter(policy, float(mp.get('after_read_timeout_s', AFTER_READ_TIMEOUT_S)),
                                                    cache_notes)
        before = seen.get('meter') or spec.get('meter_before') or {}
        started = common.parse_time(ran[0].get('started_at')) or t0
        overlaps, scan_cut = _overlapping_jobs(job_id, started, ended,
                                               float(mp.get('overlap_scan_timeout_s', OVERLAP_SCAN_S)))
        block = {'engines': list(dict.fromkeys(a['engine'] for a in ran)), 'started_at': common.iso(started),
                 'ended_at': common.iso(ended), 'note': USAGE_NOTE}
        if timed_out:
            block['after_note'] = AFTER_TIMEOUT_NOTE
        if cache_notes:
            block['cache_note'] = cache_notes[0]
        for eng in ENGINES:
            b = meter_mod.usage_point((before or {}).get(eng))
            a = meter_mod.usage_point((after or {}).get(eng))
            d = meter_mod.usage_delta(b, a)
            reasons = []
            if d['delta'] is not None:
                lag_b = (started - common.parse_time(b['reading_at'])).total_seconds()
                lag_a = (ended - common.parse_time(a['reading_at'])).total_seconds()
                if lag_b > USAGE_BEFORE_LAG_S:
                    reasons.append(f'the before reading was {int(lag_b)}s old when the job started')
                if lag_a > USAGE_AFTER_LAG_S:
                    reasons.append(f'the after reading was taken {int(lag_a)}s before the job ended')
            if scan_cut:
                reasons.append('overlap scan truncated')
            if overlaps.get(eng):
                reasons.append(f'{len(overlaps[eng])} other {eng} job(s) overlapped: ' + ', '.join(overlaps[eng][:5]))
            block[eng] = {'before': b, 'after': a, **d, 'approximate': bool(reasons), 'reasons': reasons,
                          'overlapping_jobs': overlaps.get(eng, [])}
        block['tokens'] = {e: t for e in ENGINES if (t := sum_tokens(attempts, e))}
        return block, after
    except Exception as e:  # noqa: BLE001 - usage is evidence, never a reason to lose the receipt
        return {'error': f'{type(e).__name__}: {common.clip(e, 200)}', 'note': USAGE_NOTE}, after


TRIPWIRE_SCOPE = 'tracked and untracked non-ignored paths inside the repo only'


_UNSET = object()


def _receipt_error(receipt: dict) -> str | None:
    attempts = receipt.get('attempts') or []
    return attempts[-1].get('error') if attempts else None


def _finalize(receipt: dict) -> dict:
    """Stamp every terminal receipt, including refusal and stop paths without attempts."""
    if receipt.get('status') not in ('running', 'queued'):
        receipt['finished_at'] = receipt.get('finished_at') or receipt.get('ended_at') or common.iso(common.now())
        receipt['error'] = (receipt.get('error') or _receipt_error(receipt)) if receipt.get('status') != 'done' else None
    return receipt


def _validate_owner(slug):
    if slug is None:
        return
    from . import baton
    try:
        if baton.load(baton.require_slug(slug)) is None:
            raise baton.BatonError(f'no owner baton {slug}')
    except baton.BatonError as e:
        raise Refusal(common.EXIT_USAGE, str(e)) from e


def _notify_owner(spec, receipt):
    if not isinstance(spec, dict) or not spec.get('owner'):
        return
    try:
        from . import baton
        baton.owner_job(spec['owner'], receipt, terminal=receipt.get('status') in TERMINAL)
    except Exception as e:
        # A notification is advisory; the worker's result and exit code stay intact.
        from . import baton
        print(baton.redact(f'twinlane: owner notification failed: {type(e).__name__}: {common.clip(e, 200)}'), file=sys.stderr)


OWNER_PENDING = 'owner-pending.json'
OWNER_NOTIFIED = 'owner-notified.json'


def repair_owner(jid):
    """Retry a durable completion on authorized write paths only; never rerun an engine."""
    if _preserve_job(jid):
        return
    pending = common.read_json(_p(jid, OWNER_PENDING))
    receipt = common.read_json(_p(jid, 'result.json'))
    if isinstance(pending, dict) and pending.get('job_id') == jid and isinstance(receipt, dict) \
            and receipt.get('id') == jid and receipt.get('status') in TERMINAL:
        owner = pending.get('owner')
        if isinstance(owner, str):
            _notify_owner({'owner': owner}, receipt)


def repair_owners():
    for jid in _job_ids():
        if os.path.isfile(_p(jid, OWNER_PENDING)):
            repair_owner(jid)


def _write_receipt(jid, receipt):
    receipt = _finalize(receipt)
    spec = common.read_json(_p(jid, 'job.json'), {}) or {}
    if spec.get('owner') and receipt.get('status') in TERMINAL and not os.path.exists(_p(jid, OWNER_NOTIFIED)):
        # Write-ahead intent: receipt publication must never strand the owner's wake.
        if not os.path.exists(_p(jid, OWNER_PENDING)):
            _write_job_json(jid, OWNER_PENDING, {'owner': spec['owner'], 'job_id': jid,
                              'engine': receipt.get('engine'), 'at': receipt.get('finished_at')})
    _write_job_json(jid, 'result.json', receipt)
    _notify_owner(spec, receipt)


def _write_runner_receipt(jid, receipt):
    """Publish, then finalize only the current runner's clean completion.

    CHILD_STARTING stays durable until Popen's child is reaped (or never
    started). Require this invocation's reaping evidence for a recorded child;
    legacy receipts alone do not acquire that authority. Keep every recorded
    identity, including the last attempt's child. Admission still probes them.
    """
    _write_receipt(jid, receipt)
    if _preserve_job(jid) or receipt.get('status') not in TERMINAL or 'survivor' in receipt:
        return
    try:
        with lifecycle_lock(jid, 'status.lock'):
            if any(os.path.exists(_p(jid, marker)) for marker in (CHILD_STARTING, SURVIVOR, TREE_UNRESOLVED)):
                return
            st = common.read_json(_p(jid, 'status.json'))
            if not isinstance(st, dict):
                return
            if st.get('child_pid') and (_REAPED_CHILDREN.get() or {}).get(jid) != st['child_pid']:
                return
            st.update(status=receipt['status'], runner_finalized=True, updated_at=common.iso(common.now()))
            _write_job_json(jid, 'status.json', st)
    except OSError:
        pass  # the published receipt and unfinished status retain the probed path


def build_receipt(spec: dict, attempts: list, trip: dict, t0, failover, notes, linked: bool = True,
                  usage: dict | None = None, meter_after=_UNSET) -> dict:
    last = attempts[-1] if attempts else {}
    p = spec['primary']
    served = []
    for a in attempts:
        for m in a.get('served_models') or []:
            if m and m not in served:
                served.append(m)
    disallowed = [v for a in attempts for v in a.get('disallowed_tool_uses') or []][:20]
    changed = trip.get('touched_files')
    notes = list(notes) + [a['codex_note'] for a in attempts if a.get('codex_note')] + (
        [trip['note']] if trip.get('note') else [])
    # Only a linked worktree the job owns can attribute its changes; in a shared checkout a peer's
            # edit or another tool moving HEAD would read as the worker's.
    touched = changed if linked else None
    external = None if linked else changed
    if not linked and changed is not None:
        notes.append('shared checkout: changes observed during the job are listed as external_changes '
                     '(not attributed to the worker)')
    violation = None
    if spec['profile'] == 'intel':
        tripped = linked and (bool(touched) or bool(trip.get('head_moved')))
        scanned = any('disallowed_tool_uses' in a for a in attempts)
        if tripped or disallowed:
            violation = True
        elif (linked and touched is not None) or scanned:
            violation = False
    final_src = last.get('final_path')
    final_path = None
    if final_src:
        final_path = _p(spec['id'], 'final.md')
        _write_job_text(spec['id'], 'final.md', _read_text(final_src))
    survivor = _survivor(spec['id'])
    if survivor:
        notes.append(f"{survivor.get('why') or 'engine process survived its kill'}; the job stays orphaned and holds "
                     f"its slot until it is gone: twinlane stop {spec['id']}")
    receipt = {
        'id': spec['id'], 'name': spec.get('name'), 'kind': spec['kind'], 'cwd': spec['cwd'],
        'status': last.get('status') or 'failed', 'engine': last.get('engine') or p['engine'],
        'requested': {k: p.get(k) for k in ('model', 'effort', 'tier', 'profile')},
        'served_models': served,
        'served_models_note': '; '.join(dict.fromkeys(a['served_models_note'] for a in attempts
                                                  if a.get('served_models_note'))) or None,
        'session_id': last.get('session_id'),
        'session_engine': last.get('engine'), 'num_turns': last.get('num_turns'),
        'model_calls': last.get('model_calls'), 'subagent_model_calls': last.get('subagent_model_calls'),
        'turns_note': TURNS_NOTE if last.get('engine') == 'claude' else None,
        'duration_s': round((common.now() - t0).total_seconds(), 1), 'subtype': last.get('subtype'),
        'is_error': last.get('is_error'),
        'permission_denials': sum(int(a.get('permission_denials') or 0) for a in attempts),
        'touched_files': touched, 'external_changes': external, 'tripwire_scope': TRIPWIRE_SCOPE,
        'head_moved': trip.get('head_moved'), 'main_checkout': trip.get('main_checkout'),
        'read_only_violation': violation, 'disallowed_tool_uses': disallowed,
        'limit_hit': bool(last.get('limit_hit')), 'failover': failover, 'attempts': attempts,
        'meter_before': spec.get('meter_before'),
        'meter_after': _cached_meter() if meter_after is _UNSET else meter_after, 'usage': usage,
        'final_path': final_path, 'events_path': last.get('events_path'), 'exit_code': last.get('exit_code'),
        'wait_until': last.get('wait_until'),
        'started_at': common.iso(t0), 'ended_at': common.iso(common.now()), 'baton': spec.get('baton'),
        'notes': notes,
    }
    if survivor:  # only a receipt whose engine outlived the kill carries it; job_status and `twinlane stop` read it
        receipt['survivor'] = {k: survivor.get(k) for k in ('engine', 'pid', 'created', 'at')}
    if violation is True and receipt['status'] == 'done':
        receipt['status'] = 'failed'
        reason = ', '.join(touched[:10]) if touched else ('HEAD moved' if trip.get('head_moved') else
                                                         'disallowed tools: ' + common.clip(str(disallowed), 500))
        receipt['error'] = 'read-only job broke its rule: ' + reason
    if disallowed and receipt['status'] == 'done':
        receipt['status'] = 'failed'
        receipt['error'] = 'job broke its guard: disallowed tools: ' + common.clip(str(disallowed), 500)
    if receipt['status'] == 'done' and receipt['exit_code'] not in (None, 0):
        receipt['status'] = 'failed'
        receipt['error'] = f"{receipt['engine']} exited rc {receipt['exit_code']}"
    return _finalize(receipt)


# ---------------------------------------------------------------- planning a run

class Refusal(Exception):
    def __init__(self, code: int, message: str, extra: dict | None = None):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra or {}


def _run_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane run', description='Dispatch a brief to Claude or Codex.')
    ap.add_argument('--brief', required=True)
    ap.add_argument('--cd')
    ap.add_argument('--kind', help='default: code with --write, else intel')
    ap.add_argument('--engine', choices=('auto', 'claude', 'codex'), default='auto')
    ap.add_argument('--write', action='store_true')
    ap.add_argument('--preserve-existing-metadata', action='store_true',
                    help='retain pre-existing job handoffs and launch markers; uncertainty keeps holding')
    ap.add_argument('--bg', action='store_true')
    ap.add_argument('--model')
    ap.add_argument('--effort')
    ap.add_argument('--tier')
    g = ap.add_mutually_exclusive_group()
    g.add_argument('--resume', metavar='SESSION')
    g.add_argument('--continue-job', metavar='JOB')
    ap.add_argument('--timeout', type=int)
    ap.add_argument('--max-turns', type=int)
    ap.add_argument('--urgent', action='store_true')
    ap.add_argument('--name')
    ap.add_argument('--baton')
    ap.add_argument('--owner', help='existing baton that collects this job')
    ap.add_argument('--task-id', help='opt-in stable CLI coordination task identity')
    ap.add_argument('--dispatch-id', help='idempotent request identity within --task-id')
    ap.add_argument('--actor', help='sender identity in the recorded delegation lineage')
    ap.add_argument('--recipient', help='implementation/worker identity receiving this dispatch')
    ap.add_argument('--parent-dispatch', help='recorded predecessor within the same task')
    ap.add_argument('--transition-receipt', help='host-recorded accepted review/handoff receipt identity')
    ap.add_argument('--resume-owner', action='store_true',
                    help="coordinator continues the current owner's own finished latest job (--continue-job)")
    ap.add_argument('--failover', action=argparse.BooleanOptionalAction, default=None)
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--json', action='store_true')
    return ap


def _preserving_codex_binary(args) -> dict:
    """The recorded identity of the one Codex binary a preserving start may run: TWINLANE_CODEX_BIN exactly as
    the caller supplied it, qualified as a standalone file. No PATH or app discovery, private copy, lease,
    prune or version cache, and no other engine to fall back to."""
    if args.failover:
        raise Refusal(common.EXIT_REFUSED, 'preserving Codex start never fails over to another engine; drop --failover')
    if common.CODEX_RUN:
        raise Refusal(common.EXIT_REFUSED, 'preserving Codex start refuses a TWINLANE_CODEX_RUN adapter: only the '
                      'built-in runner verifies the qualified binary hand-off')
    try:
        return common.qualify_explicit_codex_bin(os.environ.get('TWINLANE_CODEX_BIN'))
    except ValueError as e:
        raise Refusal(common.EXIT_REFUSED, f'preserving Codex start refused: {e}. It needs TWINLANE_CODEX_BIN set to '
                      'a standalone codex binary outside the app and twinlane bin folders') from e


def plan_run(args, policy: dict) -> dict:
    """Everything `run` decides before creating anything (also the whole of --dry-run)."""
    owner = getattr(args, 'owner', None)
    _validate_owner(owner)
    preserving = args.preserve_existing_metadata or _PRESERVING_METADATA.get()
    if preserving and (args.continue_job or args.resume or args.baton or owner):
        raise Refusal(common.EXIT_REFUSED, 'preserving start needs a fresh job without resume, continuation or baton updates')
    kind = args.kind or ('code' if args.write else 'intel')
    if kind not in policy.get('routing', {}):
        raise Refusal(common.EXIT_USAGE, f"unknown kind {kind!r}; kinds: {', '.join(policy.get('routing', {}))}")
    prior = _prior_job(args.continue_job) if args.continue_job else None
    if prior and prior.get('live'):
        raise Refusal(common.EXIT_REFUSED, _continue_refusal(prior['id'], prior['live']))
    cwd = args.cd or (prior or {}).get('cwd')
    if not cwd or not os.path.isdir(cwd):
        raise Refusal(common.EXIT_USAGE, f'--cd must be an existing directory (got {cwd!r})')
    cwd = os.path.abspath(cwd)
    leftover = common.leftover_dir_problem(cwd)
    if leftover:
        raise Refusal(common.EXIT_USAGE, f'--cd {cwd} {leftover}; pass an existing repo or worktree')
    if not os.path.isfile(args.brief):
        raise Refusal(common.EXIT_USAGE, f'brief not found: {args.brief}')
    # Coordinated CLI admission pins the caller text before replay/routing.
    # Continue-job transforms this same snapshot below, without rereading its source.
    brief = getattr(args, '_coordination_brief_snapshot', None)
    if brief is None:
        brief = _read_text(args.brief)
    if not brief.strip():
        raise Refusal(common.EXIT_USAGE, f'brief is empty: {args.brief}')
    problem = brief_problem(brief)
    if problem:
        raise Refusal(common.EXIT_USAGE, f'brief {args.brief} is not readable text: {problem}')
    if args.write and not args.dry_run:
        from . import wip
        try:
            with wip.lock():
                request = wip.check(cwd, 'run', args.name or common.clip(os.path.splitext(os.path.basename(args.brief))[0], 40),
                                    policy, argv=getattr(args, '_requeue_argv', []), preserve=preserving)
            # The second admission (create_job) must name the same request: same argv, so same id.
            _WIP_REQUEST.set((None if preserving else request, getattr(args, '_requeue_argv', [])))
        except (wip.WipError, TimeoutError) as e:
            raise Refusal(common.EXIT_REFUSED, str(e)) from e
    forced = None if args.engine == 'auto' else args.engine
    # A dry run costs nothing: it never takes the automatic token refresh (a model call), it only says so.
    meter = _meter(policy, auto_refresh=forced != 'codex' and not args.dry_run)
    d = _route(kind, meter, policy, args.urgent, forced)
    dry_note = _dry_run_refresh_note(meter, policy) if args.dry_run else None
    if d.get('refused') or d.get('engine') not in ENGINES:
        raise Refusal(common.EXIT_REFUSED, f"refused by policy: {d.get('reason')}" + (f'. {dry_note}' if dry_note else ''),
                      {'decision': d, 'wait_until': d.get('wait_until')})
    engine = d['engine']
    codex_binary = _preserving_codex_binary(args) if preserving and engine == 'codex' else None
    # A preserving start never looks up or probes app builds: its qualified binary has its own checks.
    pf = common.codex_preflight(policy) if engine == 'codex' and not preserving else None
    if pf and pf['refusal']:
        raise Refusal(common.EXIT_REFUSED, 'codex CLI refused before dispatch: ' + pf['refusal'], {'codex': pf})
    profile = resolve_profile(kind, engine, d, policy, args.write)
    if profile == 'code':
        if not args.write:
            raise Refusal(common.EXIT_REFUSED, f'kind {kind} uses the code profile, which needs --write')
        if not common.is_linked_worktree(cwd):
            raise Refusal(common.EXIT_REFUSED, f'--write needs a LINKED worktree (git worktree add); {cwd} is not one')
        why = writer_readiness(cwd, policy)
        if why:
            raise Refusal(common.EXIT_REFUSED, why)
    tier_name = codex_tier_model(args.model, policy) if engine == 'codex' else None
    if tier_name:
        raise Refusal(common.EXIT_USAGE, f'--model {args.model} is a Codex tier; use --tier {tier_name}')
    params = engine_params(engine, kind, d, policy, profile, args.model, args.effort, args.tier, args.timeout,
                           args.max_turns)
    notes, resume, resume_jobs = [], args.resume, []
    if pf:
        notes.append(f"codex CLI {pf['version'] or 'unknown'} ({pf['origin']})")
        if pf['note']:
            notes.append(pf['note'])
    caller_brief = brief  # before any continue-job context is appended: `twinlane resume` re-runs from this
    if resume and engine == 'claude':  # a codex run ignores --resume, so only a Claude resume needs the guard
        resume_jobs, truncated = _session_owners(resume)
        for jid in resume_jobs:
            why = _prior_live(jid, _receipt(jid))
            if why:
                raise Refusal(common.EXIT_REFUSED, _continue_refusal(jid, why, f'--resume (session of job {jid})'))
        if truncated:  # fail closed: an owner beyond the scan would be resumed twice
            raise Refusal(common.EXIT_REFUSED, '--resume: the session lookup stopped before every tracked job was '
                          'checked, so a running job may own this session. Retry; to pick up a tracked job use '
                          '`twinlane run --continue-job <id>`')
    notice = _refresh_notice(meter)
    if notice:
        notes.append(notice)
    if dry_note:
        notes.append(dry_note)
    if prior and prior.get('ignored_attempt_files'):
        notes.append('continue-job: ignored unrelated or out-of-range attempt files: '
                     + ', '.join(prior['ignored_attempt_files']))
    if prior:
        # Only Claude can resume through this path, and only the last attempt's session. Every
        # attempt before the resumed one (all of them when nothing resumes) is embedded as context.
        if prior.get('session_engine') == engine == 'claude' and prior.get('session_id'):
            resume = prior['session_id']
        block, parts, trimmed = _context_block(prior, resumed=bool(resume))
        if prior.get('lost'):
            notes.append(_lost_note(prior, parts, trimmed))
        if prior.get('session_id') and not resume:
            notes.append(f"continue-job: session of {prior.get('session_engine')} cannot resume on {engine}; "
                         'its attempts are embedded as context instead')
        if prior.get('lost'):
            brief = brief.rstrip() + '\n\n---\n\n' + _lost_notice(prior) + block
        elif block:
            brief = brief.rstrip() + '\n\n---\n\nContinuing twinlane job ' + prior['id'] + '.' + block
    if resume and engine != 'claude':
        # Notes reach receipts and the --bg output: name the source, never echo a caller-supplied value.
        src = '--resume value' if resume == args.resume else f"continue-job session of {prior['id']}"
        notes.append(f'{src} ignored: resume is Claude-only and the engine is {engine}')
        resume = None
    failover = args.failover if args.failover is not None else args.engine == 'auto'
    if codex_binary is not None:
        failover = False  # a preserving Codex start never falls back to another engine
    return {'kind': kind, 'cwd': cwd, **({'writer_root': common.writer_key(cwd)} if profile == 'code' else {}),
            'brief_src': os.path.abspath(args.brief), 'brief_text': brief,
            'caller_brief': caller_brief, 'decision': d, 'meter_before': meter, 'engine': engine, 'profile': profile, 'primary': params,
            'resume': resume, 'resume_jobs': resume_jobs, 'continue_job': prior['id'] if prior else None,
            'failover': failover,
            'write': bool(args.write), 'urgent': bool(args.urgent), 'baton': args.baton, 'owner': owner,
            **({'preserve_existing_metadata': True}
               if args.preserve_existing_metadata or _PRESERVING_METADATA.get() else {}),
            **({'codex_binary': codex_binary} if codex_binary is not None else {}),
            'timeout_override': args.timeout, 'notes': notes,
            'name': args.name or common.clip(os.path.splitext(os.path.basename(args.brief))[0], 40)}


LOST_NOTES = 5             # progress messages kept from one attempt
LOST_NOTES_CHARS = 4000    # their embedded total (newest kept); also the cap on one final message
CONTEXT_CHARS = 8000       # all embedded attempt context together; the oldest content is trimmed first
# A final message that is only the engine's limit notice ("You've hit your session limit · resets ...")
# is not a usable report: the attempt's progress notes are its context instead.
LIMIT_NOTICE = re.compile(r'(?:usage|session|rate|weekly)\s+limit|hit your (?:\w+\s+)?limit|limit (?:reached|exceeded)',
                          re.I)
LIMIT_NOTICE_CHARS = 400
# Receipts that can end without a usable session or final message while the attempt's raw output
# still holds the run's progress (a Codex limit hit whose thread.started fell outside the parser's tail).
RECOVERABLE_RECEIPTS = ('stopped', 'interrupted', 'timeout', 'limit_hit', 'failed')


def _prior_job(ref: str) -> dict:
    jid = resolve_job(ref)
    r = common.read_json(_p(jid, 'result.json'))
    r = r if isinstance(r, dict) else None
    spec = common.read_json(_p(jid, 'job.json')) or {}
    final = _read_text(r['final_path']) if r and r.get('final_path') else ''
    prior = {'id': jid, 'cwd': (r or {}).get('cwd') or spec.get('cwd'), 'session_id': (r or {}).get('session_id'),
             'session_engine': (r or {}).get('session_engine') or (r or {}).get('engine'), 'final_text': final,
             'live': _prior_live(jid, r), 'lost': None, 'ignored_attempt_files': _ignored_attempt_files(jid)}
    if prior['live']:
        return prior
    # Reconcile attempts before trusting ANY receipt: a runner that crashed after a failover's attempt
    # took its slot leaves a complete-looking receipt (session + message) of the attempt before it.
    k = max(_receipt_attempts(r) or [1])
    last_attempt, last_engine = _last_attempt(jid, r)
    superseded = k if r and last_attempt > k else None
    inconsistency = _receipt_inconsistency(jid, r, k, last_attempt)
    ctx = _attempt_context(jid, spec, r, final, k, last_attempt, last_engine)
    prior.update(attempt=last_attempt, context=ctx)
    if r is None:
        lost = job_status(jid)  # lost: a dead runner, or a queue entry that never started
    elif superseded or inconsistency or (r.get('status') in RECOVERABLE_RECEIPTS and not (r.get('session_id') and final)):
        lost = r['status'] if _status_ok(r) else 'unknown'  # a malformed status never reads as finished
    else:
        lost = None
    if lost:
        # Only the LAST attempt's session is ever resumed: after a failover, attempt 1's session belongs to
        # the other engine's earlier work. The receipt's own session wins over one read back from raw
        # output, but only when it belongs to that attempt.
        if not (prior['session_id'] and k == last_attempt and not inconsistency):
            prior.update(session_id=ctx[-1]['session_id'], session_engine=ctx[-1]['engine'])
        if inconsistency:
            prior['session_id'] = None
        prior.update(lost=lost, superseded=superseded, inconsistency=inconsistency, prepared=_prepared_attempt(jid))
    return prior


def _receipt_attempts(r: dict | None) -> list[int]:
    return [a['n'] for a in (r or {}).get('attempts') or []
            if isinstance(a, dict) and isinstance(a.get('n'), int) and a['n'] >= 1]


def _runner_status(v) -> bool:
    """A status the runner writes (a JSON list or object is malformed, never a TypeError)."""
    return isinstance(v, str) and v in TERMINAL


def _status_ok(r: dict) -> bool:
    """Only a status the runner writes is a status; anything else (garbage, blank, missing) is malformed."""
    return _runner_status(r.get('status'))


def _receipt_inconsistency(job_id: str, r: dict | None, claimed: int, latest: int) -> str | None:
    if not r:
        return None
    if not _status_ok(r):
        return 'receipt status is missing, empty or not a runner status'
    atts = r.get('attempts') or []
    numbers = _receipt_attempts(r)
    if numbers and (numbers != sorted(set(numbers)) or any(n > MAX_ATTEMPTS for n in numbers)):
        return 'receipt attempts are out of order, duplicated, or beyond the runner limit'
    st = common.read_json(_p(job_id, 'status.json'), {}) or {}
    status_n = st.get('attempt')
    if isinstance(status_n, int) and status_n < claimed:
        return f'status.json attempt {status_n} is lower than receipt attempt {claimed}'
    sid = r.get('session_id')
    if sid and claimed == latest and claimed > 1:
        matching = [a for a in atts if isinstance(a, dict) and a.get('n') == claimed]
        if len(matching) != 1 or matching[0].get('session_id') != sid or matching[0].get('engine') != r.get('session_engine'):
            return f'receipt session cannot be tied to attempt {claimed}'
    return None


def _prior_live(job_id: str, r: dict | None) -> str | None:
    """Why the prior job may still be writing to its worktree, or None. Pids are identity-checked
    (pid + creation time), so a recycled pid never counts."""
    st = common.read_json(_p(job_id, 'status.json'), {}) or {}
    # Any receipt status: a receipt is a claim, a live runner is the fact (a failed `twinlane stop`, or a
    # runner still finishing after it wrote the receipt). This process itself is never the prior's
    # runner still at work: an inline job it ran has already returned.
    pid, created = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(job_id)
    if _alive(pid, created) and int(pid) != os.getpid():
        return _still_running('its runner', pid, _pid_state(pid, created))
    if _child_alive(st):
        return _still_running('its engine process', st['child_pid'], _child_state(st))
    s = _survivor(job_id)  # an engine that outlived its kill, whether or not status.json recorded it
    if s and _survivor_state(s) != common.DEAD:
        return _still_running('its engine process', s['pid'], _survivor_state(s))
    if _child_pending(job_id):
        return 'its engine launch has unresolved bookkeeping; child exit is unproven'
    if r is None and job_status(job_id) in ('running', 'queued'):
        return 'it is still queued (its runner may be starting)'
    return None


IDENTITY_UNREADABLE = 'its process identity cannot be read from here'


def _still_running(what: str, pid, state: str) -> str:
    """Why a prior's process blocks a continue. UNKNOWN (it exists, this process cannot read its identity) is
    not "lost": a sandbox sees every host runner this way."""
    if state == common.UNKNOWN:
        return f'{what} (pid {pid}) may still be running: {IDENTITY_UNREADABLE}'
    return f'{what} (pid {pid}) is still running'


def _continue_refusal(prior_id: str, why: str, flag: str | None = None) -> str:
    flag = flag or f'--continue-job {prior_id}'
    if IDENTITY_UNREADABLE in why:  # twinlane stop would refuse too: it kills only what it can identify
        return (f'{flag}: {why}; it is not lost, so a second worker could edit the same tree: '
                f'check it from the host (twinlane board) or wait for it (twinlane wait {prior_id}) first')
    return (f'{flag}: {why}; wait for it (twinlane wait {prior_id}) or stop it '
            f'(twinlane stop {prior_id}) first, so two workers never edit the same tree')


def _receipt(job_id: str) -> dict | None:
    r = common.read_json(_p(job_id, 'result.json'))
    return r if isinstance(r, dict) else None


def _continue_blocked(prior_id: str | None, flag: str | None = None) -> str | None:
    """Re-check, right before the new worker starts, that the prior job has not come (back) to life."""
    if not prior_id or not os.path.isdir(job_dir(prior_id)):
        return None
    why = _prior_live(prior_id, _receipt(prior_id))
    return _continue_refusal(prior_id, why, flag) if why else None


_SESSION_IN_LINE = re.compile(r'"session_id":\s*"([^"]+)"')


def _job_sessions(job_id: str) -> set:
    """Every session id a tracked job is known by: the receipt (and its attempts), status.json, and the first
    SESSION_EVENTS_BYTES of each events file (a job that has not written a receipt yet has only that; its session
    may sit after other events, not only on line one)."""
    ids = set()
    r = _receipt(job_id) or {}
    st = common.read_json(_p(job_id, 'status.json'), {}) or {}
    for src in (r, st, *(a for a in r.get('attempts') or [] if isinstance(a, dict))):
        if isinstance(src.get('session_id'), str):
            ids.add(src['session_id'])
    try:
        names = [n for n in os.listdir(job_dir(job_id)) if re.fullmatch(r'events(-a\d+)?\.jsonl', n)]
    except OSError:
        names = []
    for n in names[:MAX_ATTEMPTS + 1]:
        try:
            with open(_p(job_id, n), 'rb') as f:
                head = f.read(SESSION_EVENTS_BYTES).decode('utf-8', 'replace')
        except FileNotFoundError:  # gone since the listing: nothing to own. Any other error is an incomplete scan
            continue
        ids.update(_SESSION_IN_LINE.findall(head))
    return ids


def _session_owners(session_id: str, budget_s: float | None = None) -> tuple[list, bool]:
    """-> (tracked job ids that own this session, truncated). Newest job first, inside a wall-clock budget
    like the other job-folder scans; `truncated` says the budget (or an unreadable listing) stopped it early. A
    session no tracked job owns (the user's own interactive one) returns []."""
    budget_s = SESSION_SCAN_S if budget_s is None else budget_s
    deadline = time.monotonic() + max(0.0, budget_s)
    owners = []
    for jid in _job_ids():
        if time.monotonic() >= deadline:
            return owners, True
        try:
            if session_id in _job_sessions(jid):
                owners.append(jid)
        except Exception:  # noqa: BLE001 - an unreadable folder is an incomplete scan, not a clear one
            return owners, True
    return owners, False


# Output of an attempt that ran: events-a2.jsonl, codex-a2, final-a1.md. brief-aN.md is written
# BEFORE attempt N takes its slot, so it only shows that attempt N was prepared.
_ATTEMPT_FILE = re.compile(r'^(.+)-a(\d+)(?:\.\w+)?$')
_BRIEF_FILE = re.compile(r'^brief-a(\d+)\.md$')


def _attempt_file_numbers(job_id: str) -> tuple[list[int], list[str]]:
    """Recognize only output names produced by run_attempt, within the runner's attempt limit."""
    try:
        names = os.listdir(job_dir(job_id))
    except OSError:
        return [], []
    seen, ignored = [], []
    for name in names:
        match = _ATTEMPT_FILE.match(name)
        if not match or name.startswith('brief-'):
            continue
        n = int(match.group(2))
        files = _attempt_files(job_id, n)
        allowed = {os.path.basename(files[k]) for k in ('events', 'stderr', 'final', 'codex_out', 'codex_log',
                                                        'codex_log_legacy')}
        if 1 <= n <= MAX_ATTEMPTS and name in allowed:
            seen.append(n)
        else:
            ignored.append(name)
    return seen, sorted(ignored)


def _ignored_attempt_files(job_id: str) -> list[str]:
    return _attempt_file_numbers(job_id)[1]


def _prepared_attempt(job_id: str) -> int | None:
    try:
        found = [int(m.group(1)) for m in map(_BRIEF_FILE.match, os.listdir(job_dir(job_id))) if m]
    except OSError:
        return None
    return max(found) if found else None


def _last_attempt(job_id: str, r: dict | None) -> tuple[int, str | None]:
    """(n, engine) of the job's last attempt that RAN: the HIGHEST number any source proves (receipt
    attempts, status.json `attempt`, attempt output files). status.json is written when an attempt
    takes its slot, before it has any output, so a failover that died before writing still counts."""
    st = common.read_json(_p(job_id, 'status.json'), {}) or {}
    seen = _receipt_attempts(r) + _attempt_file_numbers(job_id)[0]
    if isinstance(st.get('attempt'), int) and st['attempt'] >= 1:
        seen.append(st['attempt'])
    if any(os.path.exists(p) for p in _attempt_files(job_id, 1).values()):
        seen.append(1)  # attempt 1's raw files carry no -a suffix
    n = max(seen or [1])
    # A runner crash appends its attempt with the primary engine; status.json names the real one.
    engine = st.get('engine') if st.get('attempt') == n else None
    atts = [a for a in (r or {}).get('attempts') or [] if isinstance(a, dict)]
    return n, engine or next((a.get('engine') for a in reversed(atts) if a.get('n') == n), None)


def _attempt_events(job_id: str, n: int, engine: str | None) -> tuple[str | None, str | None]:
    files = _attempt_files(job_id, n)
    for e, path in (('codex', os.path.join(files['codex_out'], 'events.jsonl')), ('claude', files['events'])):
        if engine in (None, e) and os.path.exists(path):
            return e, path
    return None, None


def _attempt_output(job_id: str, n: int) -> bool:
    """Whether attempt n left ANY non-empty runner file: events, stderr, final, or the Codex logs."""
    files = _attempt_files(job_id, n)
    paths = [files[k] for k in ('events', 'stderr', 'final', 'codex_log', 'codex_log_legacy')]
    for root, _, names in os.walk(files['codex_out']):
        paths += [os.path.join(root, x) for x in names]
    for path in paths:
        try:
            if os.path.getsize(path) > 0:
                return True
        except OSError:
            continue
    return False


def _usable_final(text: str | None, status: str | None = None) -> str:
    """The final message, unless it is only the engine's limit notice. An attempt whose recorded status is
    known decides by that status (a done report mentioning a rate limit is a report); only an attempt of
    unknown status falls back to the short-text pattern."""
    text = (text or '').strip()
    if status is not None and status != 'limit_hit':
        return text
    return '' if len(text) <= LIMIT_NOTICE_CHARS and LIMIT_NOTICE.search(text) else text


def _attempt_context(job_id: str, spec: dict, r: dict | None, receipt_final: str, k: int, last: int,
                     last_engine: str | None) -> list[dict]:
    """One entry per attempt 1..last: its engine, whether it left output, its session (from its raw
    events) and its context, which is its usable final message (final-aN.md, else the receipt's final
    for the receipt's attempt k) or else its last progress notes."""
    atts = [a for a in (r or {}).get('attempts') or [] if isinstance(a, dict)]
    primary = spec.get('engine') or (spec.get('primary') or {}).get('engine')
    out = []
    for n in range(1, last + 1):
        got = _read_progress(job_id, n, last_engine if n == last else None) or {}
        entry = next((a for a in reversed(atts) if a.get('n') == n), {})
        # A failover always swaps engines, so attempt 2 of an unknown engine ran on the other one.
        guess = primary if n % 2 else (primary and _other(primary))
        engine = got.get('session_engine') or (last_engine if n == last else None) or entry.get('engine') or guess
        final = _read_text(_attempt_files(job_id, n)['final'])
        if not final.strip() and r and n == k:
            final = receipt_final
        status = entry.get('status') if _runner_status(entry.get('status')) else None
        if status is None and r and n == k and _status_ok(r):
            status = r['status']
        item = {'n': n, 'engine': engine, 'output': _attempt_output(job_id, n) or bool(final.strip()),
                'session_id': got.get('session_id'), 'kind': None, 'text': '', 'notes': 0}
        if _usable_final(final, status):
            item.update(kind='message', text=common.clip(_usable_final(final, status), LOST_NOTES_CHARS))
        elif got.get('progress_notes'):
            item.update(kind='notes', text=got['progress'], notes=got['progress_notes'])
        out.append(item)
    return out


def _read_progress(job_id: str, n: int, engine: str | None) -> dict | None:
    """Session id and last progress notes from one attempt's raw output (tail only: events files can
    be many MB), or None when that attempt wrote none."""
    engine, path = _attempt_events(job_id, n, engine)
    if not path:
        return None
    lines = common.tail_lines(path)
    notes = []
    if engine == 'codex':
        sid = parse_codex_out(os.path.dirname(path))['session_id']
        if not sid:  # thread.started is the first line, which a tail of a huge file drops
            m = re.search(r'"(?:session_id|thread_id)":\s*"([0-9a-f-]{36})"', common.first_line(path))
            sid = m.group(1) if m else None
        for line in lines:
            if '"agent_message"' not in line:
                continue
            try:
                item = json.loads(line).get('item')
            except (ValueError, AttributeError):
                continue
            if isinstance(item, dict) and item.get('type') == 'agent_message' and isinstance(item.get('text'), str):
                notes.append(item['text'].strip())
    else:
        sid = parse_stream_lines(lines)['session_id']
        for line in lines:
            if '"assistant"' not in line:
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if isinstance(ev, dict) and ev.get('type') == 'assistant':
                notes.append(_message_text(ev.get('message')))
    notes = [x for x in notes if x][-LOST_NOTES:]
    text = '\n\n'.join(notes)
    if len(text) > LOST_NOTES_CHARS:
        text = '…' + text[-(LOST_NOTES_CHARS - 1):]
    return {'session_id': sid, 'session_engine': engine, 'progress': text, 'progress_notes': len(notes),
            'progress_from': n, 'progress_engine': engine}


def _lost_reason(prior: dict) -> str:
    if prior.get('inconsistency'):
        why = f"inconsistent receipt: {prior['inconsistency']}; its session cannot be safely resumed"
    elif prior.get('superseded'):
        why = (f"after attempt {prior['superseded']} it failed over to attempt {prior.get('attempt')} "
               f"({prior.get('session_engine')}), which left no receipt; the receipt's session and message "
               f"belong to attempt {prior['superseded']} only")
    else:
        gaps = [g for g, ok in (('final message', prior.get('final_text')), ('session id', prior.get('session_id')))
                if not ok]
        why = ('it left no ' + ' and no '.join(gaps)) if gaps else 'its receipt was incomplete'
    if prior.get('prepared') and prior['prepared'] > (prior.get('attempt') or 1):
        why += f"; attempt {prior['prepared']} was prepared (its brief was written) but never took its slot"
    return why


def _lost_note(prior: dict, parts: list[str], trimmed: bool) -> str:
    return (f"continue-job: prior job {prior['id']} did not finish (status: {prior['lost']}; {_lost_reason(prior)}); "
            f"recovered attempt {prior.get('attempt')} ({prior.get('session_engine')}): session "
            f"{prior.get('session_id') or 'none'}; context embedded: {', '.join(parts) or 'none'}"
            f"{' (trimmed, oldest first)' if trimmed else ''}; "
            'its working tree may hold an uncommitted partial diff to review, not discard')


def _lost_notice(prior: dict) -> str:
    return (f"twinlane job {prior['id']} did not finish (status: {prior['lost']}): {_lost_reason(prior)}. Its working "
            f"tree ({prior.get('cwd')}) may hold an uncommitted partial diff: review it (git status, git diff) "
            'and build on it; do not discard it.')


def _context_block(prior: dict, resumed: bool) -> tuple[str, list[str], bool]:
    """(brief text, note parts, trimmed) for the attempts before the resumed one, or for all of them
    when nothing resumes: each attempt's usable final message, else its progress notes, labelled with
    its attempt and engine, oldest first. Over CONTEXT_CHARS, the oldest content is cut first."""
    ctx = prior.get('context') or []
    last = ctx[-1]['n'] if ctx else 0
    shown = [c for c in ctx if c['n'] < last or not resumed]
    texts = [c['text'] for c in shown if c['kind']]
    excess, cut, gone = sum(map(len, texts)) - CONTEXT_CHARS, [], '(trimmed away: over the size limit)'
    for t in texts:
        if excess <= 0 or len(t) <= len(gone):
            cut.append(t)
        elif excess >= len(t) - len(gone):
            excess -= len(t) - len(gone)
            cut.append(gone)
        else:
            cut.append('…' + t[excess + 1:])
            excess = 0
    trimmed = cut != texts
    out, parts, texts = '', [], iter(cut)
    for c in shown:
        who = f"attempt {c['n']}, {c['engine']}"
        if c['kind'] == 'message':
            out += f'\n\nIts last message ({who}; context only):\n\n' + next(texts)
            parts.append(f"attempt {c['n']} ({c['engine']}) last message")
        elif c['kind'] == 'notes':
            out += f'\n\nIts last progress notes ({who}; newest last):\n\n' + next(texts)
            parts.append(f"attempt {c['n']} ({c['engine']}) {c['notes']} progress note(s)")
        elif len(ctx) > 1:
            # Say what an attempt left; "wrote no output" only when it has no runner file at all.
            if c['n'] == last and not c['output']:
                out = (f" Its last attempt ({last}, {c['engine']}) wrote no output before it ended, so there is no "
                       'session to resume.') + out
            elif c['n'] == last:
                out = (f" Its last attempt ({last}, {c['engine']}) left no final message or progress notes"
                       f"{'' if c['session_id'] else ' and no session id'}.") + out
            else:
                out += (f"\n\nAttempt {c['n']} ({c['engine']}) "
                        + ('left no final message or progress notes.' if c['output'] else 'wrote no output.'))
    if not parts and not resumed:
        why = ('its engine output holds no usable final message or progress notes'
               if any(c['output'] for c in ctx) else 'the job folder holds no engine output')
        out += f'\n\nContext from its attempts: (none recovered: {why})'
    if trimmed:
        out += f'\n\n(Context trimmed to {CONTEXT_CHARS} characters: the oldest content was cut first.)'
    return out, parts, trimmed


def _coordination_parent():
    """job.json of the job this process works for (None outside a worker); refuses when custody is unknown.

    Both the current and the predecessor lineage variable are read. When they name two different jobs
    the parent is unknown; that is tolerated only when neither job is coordinated (the answer would be
    the same whichever is the parent), and a coordinated dispatch still refuses in coordination.py."""
    from . import coordination
    try:
        parents = coordination.worker_parents()
    except coordination.Refusal as e:
        raise Refusal(common.EXIT_REFUSED, str(e)) from e
    if not parents:
        return None
    if len(parents) > 1 and any(spec.get('coordination') is not None for _, spec in parents):
        raise Refusal(common.EXIT_REFUSED, 'worker lineage is ambiguous: the current and the predecessor '
                      'job variables name different jobs; custody is unknown')
    return parents[0][1]


_WIP_REQUEST = ContextVar('twinlane_wip_request', default=None)  # the queued request this run replays


def create_job(plan: dict) -> str:
    _validate_owner(plan.get('owner'))
    request, requeue_argv = _WIP_REQUEST.get() or (None, None)
    _WIP_REQUEST.set(None)
    if plan.get('write') or plan.get('profile') == 'code':
        from . import wip
        try:
            with wip.admission(plan['cwd'], 'run', plan.get('name') or plan.get('baton') or 'writer',
                               argv=requeue_argv, preserve=plan.get('preserve_existing_metadata') is True):
                jid = _admitted_job(plan)
                if request:  # the job is on disk now: only here is a pumped request really done
                    try:
                        wip.acknowledge(request, os.environ.get(wip.CLAIM_ENV))
                    except OSError:
                        pass  # the claim stays; a person sees a claimed request next to a running job
                return jid
        except (wip.WipError, TimeoutError) as e:
            raise Refusal(common.EXIT_REFUSED, str(e)) from e
    return _admitted_job(plan)


def _admitted_job(plan: dict) -> str:
    parent_spec = _coordination_parent()
    if plan.get('coordination') is None:
        if parent_spec is not None and parent_spec.get('coordination') is not None:
            raise Refusal(common.EXIT_REFUSED, 'coordinated workers cannot drop task identity on child dispatch')
        return _create_job(plan)
    from . import coordination
    try:
        return coordination.allocate(plan, lambda jid: _create_job(plan, jid))
    except coordination.Refusal as e:
        raise Refusal(common.EXIT_REFUSED, str(e)) from e


def _create_job(plan: dict, reserved_id: str | None = None) -> str:
    os.makedirs(common.JOBS, exist_ok=True)
    for _ in range(1 if reserved_id else 20):
        if reserved_id:
            jid = reserved_id
        else:
            jid = f"{common.stamp()}-{plan['engine']}-{secrets.token_hex(2)}"
        try:
            os.makedirs(job_dir(jid))
            break
        except FileExistsError:
            continue
    else:
        raise RuntimeError('could not allocate a job id')
    spec = dict(plan, id=jid, created_at=common.iso(common.now()), writer_reservation_version=1)
    _write_job_text(jid, 'brief.md', compose_brief(jid, plan['engine'], plan['profile'], plan['brief_text']))
    if isinstance(plan.get('caller_brief'), str):
        _write_job_text(jid, CALLER_BRIEF, plan['caller_brief'])
    _write_job_json(jid, 'job.json', spec)
    _write_job_json(jid, 'status.json', {'status': 'queued', 'pid': None, 'engine': plan['engine'],
                                               'created_at': spec['created_at']})
    _notify_owner(spec, dict(id=jid, engine=plan['engine'], status='queued'))
    return jid


def dry_run_view(plan: dict, policy: dict) -> dict:
    jid = '<job-id>'
    jd = os.path.join(common.JOBS, jid)
    params = plan['primary']
    argv = build_argv(params, policy, os.path.join(jd, 'brief.md'), plan['cwd'], os.path.join(jd, 'codex'),
                      plan['resume'], os.path.join(jd, 'guard-settings.json'))
    settings_deny = intel_settings_deny(params, policy, plan['cwd']) if params['engine'] == 'claude' else []
    return {'dry_run': True, 'engine': plan['engine'], 'kind': plan['kind'], 'profile': plan['profile'],
            'params': params, 'decision': plan['decision'], 'cwd': plan['cwd'], 'argv': argv,
            'settings_deny_rules': len(settings_deny),
            'stdin': f'{jd}/brief.md (contract + brief)' if params['engine'] == 'claude' else None,
            'failover': plan['failover'], 'resume': plan['resume'], 'notes': plan['notes']}


# ---------------------------------------------------------------- CLI commands

# What a coordinated caller asked for, fingerprinted before routing. Frozen with the version-1 registry:
# a changed field set would make every recorded dispatch refuse its own replay.
CALLER_INTENT_FIELDS = ('kind', 'engine', 'model', 'effort', 'tier', 'timeout', 'max_turns',
                        'resume', 'continue_job', 'write', 'urgent', 'failover', 'baton')


def _coordination_intent(args, envelope):
    if not os.path.isfile(args.brief):
        raise Refusal(common.EXIT_USAGE, f'brief not found: {args.brief}')
    intent = {name: getattr(args, name) for name in CALLER_INTENT_FIELDS}
    if getattr(args, 'owner', None) is not None:
        intent['owner'] = args.owner
    if args.preserve_existing_metadata or _PRESERVING_METADATA.get():
        intent['preserve_existing_metadata'] = True
    intent.update(cwd=os.path.abspath(args.cd) if args.cd else None,
                  brief_text=_read_text(args.brief), coordination=envelope)
    return intent


def _coordination_replay_result(jid, as_json):
    receipt = common.read_json(_p(jid, 'result.json'))
    status = receipt.get('status') if isinstance(receipt, dict) else job_status(jid)
    common.emit({'id': jid, 'replayed': True, 'status': status, 'receipt': receipt,
                 'notes': ['existing dispatch preserved; no runner submitted or admission bypassed']}, as_json)
    return EXIT_BY_STATUS.get(status, common.EXIT_RUNNING)


def cmd_run(argv) -> int:
    # Enter before planning/replay: neither a refusal nor dry-run may sweep old handoffs.
    mode = _PRESERVING_METADATA.set(_PRESERVING_METADATA.get() or '--preserve-existing-metadata' in argv)
    owner = _CURRENT_JOB.set(None)
    try:
        return _cmd_run(argv)
    finally:
        _CURRENT_JOB.reset(owner)
        _PRESERVING_METADATA.reset(mode)


def _cmd_run(argv) -> int:
    args = _parse(_run_parser(), argv)
    if isinstance(args, int):
        return args
    args._requeue_argv = ['run'] + list(argv)
    # argparse accepts unambiguous long-option abbreviations. Activate the parsed
    # mode before parent/replay/sweep/planning effects, not just the raw spelling.
    if args.preserve_existing_metadata:
        _PRESERVING_METADATA.set(True)
    try:
        _validate_owner(args.owner)
        parent = _coordination_parent()
        if parent is not None and 'preserve_existing_metadata' in parent \
                and type(parent['preserve_existing_metadata']) is not bool:
            raise Refusal(common.EXIT_REFUSED, 'parent metadata preservation mode is malformed; no sweep or launch')
        if parent is not None and parent.get('preserve_existing_metadata') is True:
            _PRESERVING_METADATA.set(True)
        values = (args.task_id, args.dispatch_id, args.actor, args.recipient,
                  args.parent_dispatch, args.transition_receipt)
        envelope = intent = None
        if args.resume_owner and args.task_id is None:
            raise Refusal(common.EXIT_REFUSED, '--resume-owner needs a coordination task (--task-id ...)')
        if any(value is not None for value in values):
            if not all(values[:4]):
                raise Refusal(common.EXIT_REFUSED, 'coordination requires task, dispatch, actor and recipient')
            envelope = dict(task=args.task_id, dispatch=args.dispatch_id, actor=args.actor,
                            recipient=args.recipient, parent=args.parent_dispatch, receipt=args.transition_receipt)
            if args.resume_owner:  # added only when asked, so existing envelopes keep their fingerprints
                envelope['resume_owner'] = True
            intent = _coordination_intent(args, envelope)
            args._coordination_brief_snapshot = intent['brief_text']
            if not args.dry_run:
                from . import coordination
                existing = coordination.replay({'coordination': envelope, 'caller_intent': intent})
                if existing:
                    if _PRESERVING_METADATA.get():
                        raise Refusal(common.EXIT_REFUSED, 'existing dispatch is protected; preserving start will not replay it')
                    return _coordination_replay_result(existing, args.json)
        elif parent is not None and parent.get('coordination') is not None:
            raise Refusal(common.EXIT_REFUSED, 'coordinated workers cannot drop task identity')
        if not _PRESERVING_METADATA.get():
            envhandoff.sweep(common.JOBS, HANDOFF)
        policy = common.load_policy()
        plan = plan_run(args, policy)
        if envelope is not None:
            plan.update(coordination=envelope, caller_intent=intent)
    except ValueError as e:
        common.emit({'refused': True, 'exit': common.EXIT_REFUSED, 'reason': str(e)}, args.json)
        if not args.json:
            print(f'twinlane run: {e}', file=sys.stderr)
        return common.EXIT_REFUSED
    except Refusal as r:
        common.emit({'refused': True, 'exit': r.code, 'reason': r.message, **r.extra}, args.json)
        if not args.json:
            print(f'twinlane run: {r.message}', file=sys.stderr)
            if r.extra.get('wait_until'):
                print(f"wait until {r.extra['wait_until']}; write/refresh the baton: twinlane baton snap <slug>",
                      file=sys.stderr)
        return r.code
    if args.dry_run:
        view = dry_run_view(plan, policy)
        if args.json:
            common.emit(view, True)
        else:
            d = plan['decision']
            print(f"route: {plan['engine']} profile={plan['profile']} model={plan['primary'].get('model')} "
                  f"effort={plan['primary'].get('effort')} tier={plan['primary'].get('tier')} "
                  f"failover={plan['failover']} reason={d.get('reason')}")
            print('argv: ' + (subprocess.list2cmdline(view['argv']) if view['argv'] else '(engine binary missing)'))
            for n in plan['notes']:
                print('note: ' + n)
        return common.EXIT_OK if view['argv'] else common.EXIT_MISSING
    try:
        jid = create_job(plan)
        if plan.get('coordination'):
            from . import coordination
            if not coordination.claim_launch(plan, jid):
                return _coordination_replay_result(jid, args.json)
    except (Refusal, ValueError) as e:
        common.emit({'refused': True, 'exit': common.EXIT_REFUSED, 'reason': str(e)}, args.json)
        if not args.json:
            print(f'twinlane run: {e}', file=sys.stderr)
        return common.EXIT_REFUSED
    if args.bg:
        return _start_background(jid, plan, args.json)
    _write_status(jid, 'queued', engine=plan['engine'])
    receipt = run_job(jid)
    _print_result(receipt, args.json, full=False, max_lines=40)
    return EXIT_BY_STATUS.get(receipt['status'], common.EXIT_FAILED)


def _start_background(jid: str, plan: dict, as_json: bool) -> int:
    try:
        runner_argv = [sys.executable, common.TWINLANE_PY, '_job', jid]
        if plan.get('preserve_existing_metadata') is True:
            runner_argv.append('--preserve-existing-metadata')
        d = common.spawn_detached_ex(runner_argv, cwd=common.ROOT,
                                     log_path=_p(jid, 'runner.log'), require_survival=True,
                                     handoff=_p(jid, HANDOFF))
        if not d.get('created'):
            raise common.DetachAmbiguous('runner started but its creation stamp is unavailable')
    except common.DetachAmbiguous as e:
        if e.runner_may_run:
            # A runner may be executing: no terminal receipt (it would hide that runner); it records itself.
            reason = (f'--bg start is ambiguous: {e}. A runner may be running: check `twinlane jobs` and '
                      f'`twinlane wait {jid}` before starting this work again.')
            spec = common.read_json(_p(jid, 'job.json'), {}) or {}
            spec['detach'] = {'mode': 'ambiguous', 'error': str(e)}
            _write_job_json(jid, 'job.json', spec)
            if e.pid and e.created:  # never an unstamped pid: `twinlane stop` could kill a reused one
                _write_job_text(jid, 'runner.pid', f'{e.pid} {e.created}')
            common.emit({'id': jid, 'refused': True, 'status': 'unknown', 'reason': reason}, as_json)
            if not as_json:
                print(f'twinlane run: {reason}', file=sys.stderr)
            return common.EXIT_REFUSED
        return _refuse_background(jid, plan, as_json, e)
    except common.DetachError as e:
        return _refuse_background(jid, plan, as_json, e)
    _write_job_text(jid, 'runner.pid', f"{d['pid']} {d['created']}")
    spec = common.read_json(_p(jid, 'job.json'), {}) or {}
    spec['detach'] = {'mode': d['mode'], 'job': d['job']}
    _write_job_json(jid, 'job.json', spec)
    note = '' if d['mode'] in ('wmi', 'posix') else \
        '\nnote: started by the caller, maybe still inside one of its job objects; it can die with that job'
    note += ''.join(f'\nnote: {n}' for n in plan['notes'])
    common.emit({'id': jid, 'pid': d['pid'], 'engine': plan['engine'], 'detach': d['mode'],
                 'wait': f'twinlane wait {jid}', 'notes': plan['notes']}, as_json,
                f"{jid}  ({plan['engine']}, {plan['profile']}, detach={d['mode']})\nnext: twinlane wait {jid}{note}")
    return common.EXIT_OK


def _refuse_background(jid: str, plan: dict, as_json: bool, e: Exception) -> int:
    """No runner exists and none can start: a terminal 'refused' receipt."""
    advice = ('Launch identity retained; report this refusal to the task coordinator, do not replay inline.'
              if plan.get('coordination') else 'Run without --bg (inline), or start twinlane from a normal terminal.')
    reason = f'--bg refused: {e}. {advice}'
    receipt = {'id': jid, 'status': 'refused', 'engine': plan['engine'], 'error': reason, 'attempts': [],
               'notes': [reason]}
    # No runner started, but the receipt still has one writer: the runner role, which a concurrent
    # `twinlane stop` takes too. A role that cannot be taken, or is held, means no receipt (fail closed).
    try:
        holder = _claim_runner(jid)
    except (OSError, TimeoutError) as error:
        holder, unwritten = False, f'the runner role could not be taken ({type(error).__name__})'
    else:
        unwritten = f'runner pid {holder[0]} holds the job' if holder else None
    if holder is None:
        try:
            if common.read_json(_p(jid, 'result.json')) is None:
                _write_receipt(jid, receipt)
        finally:
            _release_runner(jid)
    payload = {'id': jid, 'refused': True, 'reason': reason}
    if unwritten:
        payload['receipt'] = f'not written: {unwritten}'
    common.emit(payload, as_json)
    if not as_json:
        print(f'twinlane run: {reason}' + (f' No receipt written: {unwritten}.' if unwritten else ''),
              file=sys.stderr)
    return common.EXIT_REFUSED


def cmd_job(argv) -> int:
    preserve = len(argv) == 2 and argv[-1] == '--preserve-existing-metadata'
    if preserve:
        argv = argv[:-1]
    if len(argv) != 1:
        print('usage: twinlane _job JOB', file=sys.stderr)
        return common.EXIT_USAGE
    receipt = run_job(argv[0], preserve_existing_metadata=True) if preserve else run_job(argv[0])
    if receipt.get('duplicate_runner'):
        print(f"twinlane _job: {receipt['notes'][0]}", file=sys.stderr)
        return common.EXIT_REFUSED
    return EXIT_BY_STATUS.get(receipt['status'], common.EXIT_FAILED)


def cmd_jobs(argv) -> int:
    if argv[:1] == ['archive']:
        from . import archive
        return archive.cli(argv[1:])
    as_json, show_all = '--json' in argv, '--all' in argv
    envhandoff.sweep(common.JOBS, HANDOFF)
    rows = list_jobs(limit=None if show_all else 20)
    if as_json:
        common.emit(rows, True)
        return common.EXIT_OK
    if not rows:
        print('no jobs')
    for j in rows:
        dur = f"{j['duration_s']}s" if j.get('duration_s') is not None else '-'
        print(f"{j['id']}  {j.get('name') or ''}  {j.get('engine')}  {j.get('model') or '-'}  {j['status']}  "
              f"age={j['age']}  dur={dur}  {j.get('cwd') or ''}")
    return common.EXIT_OK


def cmd_wait(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane wait')
    ap.add_argument('jobs', nargs='+')
    ap.add_argument('--any', action='store_true')
    ap.add_argument('--timeout', type=float)
    ap.add_argument('--json', action='store_true')
    args = _parse(ap, argv)
    if isinstance(args, int):
        return args
    try:
        ids = list(dict.fromkeys(resolve_job(j) for j in args.jobs))
    except LookupError as e:
        print(f'twinlane wait: {e}', file=sys.stderr)
        return common.EXIT_USAGE
    deadline = None if args.timeout is None else time.monotonic() + args.timeout
    while True:
        repairs = {}
        for j in ids:
            repair_owner(j)
            # Only a terminal receipt has a recheck to reject; a job without one (lost) keeps its own status.
            receipt = common.read_json(_p(j, 'result.json'))
            repairs[j] = (_repair_finished_status(j) if isinstance(receipt, dict)
                          and receipt.get('status') in TERMINAL else None)
        statuses = {j: job_status(j) for j in ids}
        statuses = {j: 'unknown' if repairs[j] == 'rejected' and s in TERMINAL else s
                    for j, s in statuses.items()}
        finished = [j for j, s in statuses.items() if s in TERMINAL]
        if (args.any and finished) or len(finished) == len(ids):
            _print_statuses(statuses, args.json, timed_out=False, finished=finished, any_mode=args.any, timeout=args.timeout)
            return max((EXIT_BY_STATUS.get(statuses[j], common.EXIT_FAILED) for j in finished), default=0)
        if deadline is not None and time.monotonic() >= deadline:
            _print_statuses(statuses, args.json, timed_out=True, finished=finished, any_mode=args.any, timeout=args.timeout)
            return common.EXIT_RUNNING
        wait_s = POLL_S if deadline is None else max(0.0, min(POLL_S, deadline - time.monotonic()))
        time.sleep(wait_s)


COLLECTED = 'collected'    # jobs/<id>/collected: the time `twinlane result` last printed this receipt (only it writes it)


def mark_collected(job_id: str) -> None:
    """Record that the receipt was read, so `twinlane board` can tell "finished, not yet read". Best effort: a
    read-only disk must not make `twinlane result` fail after it has printed."""
    try:
        _write_job_text(job_id, COLLECTED, common.iso(common.now()) + '\n')
    except OSError:
        pass


def cmd_result(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane result')
    ap.add_argument('job')
    ap.add_argument('--full', action='store_true')
    ap.add_argument('--json', action='store_true')
    args = _parse(ap, argv)
    if isinstance(args, int):
        return args
    try:
        jid = resolve_job(args.job)
    except LookupError as e:
        print(f'twinlane result: {e}', file=sys.stderr)
        return common.EXIT_USAGE
    receipt = common.read_json(_p(jid, 'result.json'))
    if not isinstance(receipt, dict):
        status = job_status(jid)
        common.emit({'id': jid, 'status': status}, args.json, f'{jid}: {status} (no receipt yet; twinlane wait {jid})')
        return common.EXIT_RUNNING if status not in TERMINAL else common.EXIT_FAILED
    repair_owner(jid)
    # Only a terminal receipt has a recheck to reject; a running receipt prints as before.
    repair = _repair_finished_status(jid) if receipt.get('status') in TERMINAL else None
    if repair == 'rejected':
        status = job_status(jid)
        receipt = dict(receipt, effective_status='unknown' if status in TERMINAL else status,
                       status_repair='rejected')
        _print_result(receipt, args.json, full=args.full)
        if not args.json:
            print(f'{jid}: finished-status recheck rejected; custody remains unresolved', file=sys.stderr)
        return common.EXIT_RUNNING
    _print_result(receipt, args.json, full=args.full)
    if receipt.get('status') in TERMINAL:  # a running receipt will be replaced by the real result
        mark_collected(jid)
    return common.EXIT_OK


def _request_stop(jid):
    # First, so a runner that has not recorded its pid still launches nothing.
    def write(path):
        with open(path, 'x', encoding='utf-8') as handle:
            handle.write(common.iso(common.now()) or '')
    try:
        _write_job_file(jid, STOP_REQUESTED, write)
    except FileExistsError:
        pass


def _clear_tree_unresolved(jid: str, as_json: bool) -> int:
    """The person's word that the stalled tree is gone: remove the marker, with a record of it."""
    if _preserve_job(jid):
        print(f'twinlane stop: {jid} is being observed; nothing was changed', file=sys.stderr)
        return common.EXIT_REFUSED
    held = [m for m in (TREE_UNRESOLVED, CHILD_STARTING) if os.path.exists(_p(jid, m))]
    if not held or not os.path.exists(_p(jid, 'result.json')):
        common.emit({'id': jid, 'cleared': False}, as_json, f'{jid}: no unresolved process tree to clear')
        return common.EXIT_OK
    try:
        for marker in held:
            os.remove(_p(jid, marker))
    except OSError as e:
        print(f'twinlane stop: could not clear {jid}: {e}', file=sys.stderr)
        return common.EXIT_FAILED
    _log_custody_release(jid, None, 'a person confirmed the stalled engine tree is gone (stop --tree-gone)')
    common.emit({'id': jid, 'cleared': True}, as_json, f'{jid}: unresolved tree cleared; its worktree claim can now be released')
    return common.EXIT_OK


def cmd_stop(argv) -> int:
    ap = argparse.ArgumentParser(prog='twinlane stop')
    ap.add_argument('job')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--tree-gone', action='store_true',
                    help='after checking by hand that nothing of the stalled engine tree still runs, release its worktree claim')
    args = _parse(ap, argv)
    if isinstance(args, int):
        return args
    try:
        jid = resolve_job(args.job)
    except LookupError as e:
        print(f'twinlane stop: {e}', file=sys.stderr)
        return common.EXIT_USAGE
    if args.tree_gone:
        from . import projects
        refused = projects.refuse_in_worker('stop --tree-gone')  # custody is released by a person, never a worker
        if refused is not None:
            return refused
        return _clear_tree_unresolved(jid, args.json)
    _request_stop(jid)
    spec = common.read_json(_p(jid, 'job.json'), {}) or {}
    existing = common.read_json(_p(jid, 'result.json'))
    # Only a terminal receipt means finished; a running receipt is stopped like any live job.
    if isinstance(existing, dict) and existing.get('status') in TERMINAL:
        repair = _repair_finished_status(jid)
        survivor = _survivor(jid)
        if survivor and _survivor_state(survivor) != common.DEAD:
            return _stop_survivor(jid, existing, survivor, args.json)
        if repair == 'rejected':
            status = job_status(jid)
            common.emit({'id': jid, 'status': 'unknown' if status in TERMINAL else status,
                         'already_finished': False, 'status_repair': 'rejected'}, args.json,
                        f'{jid}: finished-status recheck rejected; custody remains unresolved')
            return common.EXIT_RUNNING
        _baton(spec.get('baton'), existing)  # its baton may still list it as running: sync from the receipt
        common.emit({'id': jid, 'status': existing.get('status'), 'already_finished': True}, args.json,
                    f"{jid}: already finished ({existing.get('status')})")
        return common.EXIT_OK
    st = common.read_json(_p(jid, 'status.json'), {}) or {}
    runner = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(jid)
    killed, failed, unknown = [], [], []
    # Runner first: its tree (and its job object) takes the engine child with it. Every pid is
    # identity-checked, so a reused pid of an unrelated process is never killed; a pid without a
    # creation stamp has no identity and is never killed at all.
    targets = [runner, (st.get('child_pid'), st.get('child_created'))]
    surv = _survivor(jid)  # a survivor recorded before a crash that left no receipt (a stampless one is never killed)
    if surv and surv['pid'] not in (st.get('child_pid'), runner[0]):
        targets.append((surv['pid'], surv.get('created')))
    for pid, created in targets:
        if not pid or int(pid) == os.getpid():
            continue
        # No stamp to compare, or one this process cannot read (a sandbox): the identity is unknown either way.
        if created in (None, '') and common.pid_alive(pid) or common.pid_state(pid, created) == common.UNKNOWN:
            unknown.append(int(pid))
    if unknown:
        msg = (f'{jid}: runner identity unknown (pid {", ".join(map(str, unknown))} has no creation stamp or its '
               f'identity cannot be read from here; on macOS, ps must provide a parseable C-locale process start '
               f'time); not killed, job left as is. {STOP_REQUESTED} is set, so a twinlane runner launches no '
               'further attempt.')
        common.emit({'id': jid, 'status': 'unknown', 'identity_unknown_pids': unknown, 'killed_pids': killed},
                    args.json, msg)
        return common.EXIT_FAILED
    for pid, created in targets:
        if not pid or int(pid) == os.getpid() or created in (None, '') or not common.pid_alive(pid, created):
            continue
        if common.kill_tree(pid, created):
            killed.append(int(pid))
        else:
            failed.append(int(pid))
        # A kill is asynchronous, and on Windows a cross-process kill is never reported as confirmed (the attempt's
        # job handle lives in the runner), so the grace applies either way before the liveness check below.
        _pid_gone(pid, created)
    alive = [int(pid) for pid, created in targets
             if pid and int(pid) != os.getpid() and common.pid_maybe_alive(pid, created)]
    if runner[0] and int(runner[0]) in alive:
        return _stop_requested_only(jid, f'runner pid {runner[0]} is still alive after the kill', killed, failed,
                                    alive, args.json)
    if alive:  # the runner is gone but its engine is not: a stopped receipt now would be a lie
        msg = (f'{jid}: stop FAILED: pid {", ".join(map(str, alive))} is still alive after the kill; no receipt '
               f'written, job left running. {STOP_REQUESTED} is set, so a twinlane runner launches no further '
               f'attempt. Retry `twinlane stop {jid}` or end the process by hand.')
        common.emit({'id': jid, 'status': 'stop_failed', 'kill_failed_pids': failed, 'alive_pids': alive,
                     'killed_pids': killed}, args.json, msg)
        return common.EXIT_FAILED
    # Single-writer receipt: only the holder of the runner role writes result.json. Take it the way a runner does
    # (a lock naming a dead runner is stale), so no runner can publish while this writes, and a runner that starts
    # later finds the receipt and exits.
    try:
        holder = _claim_runner(jid)
    except (OSError, TimeoutError) as e:
        return _stop_requested_only(jid, f'the runner role could not be taken ({type(e).__name__})', killed,
                                    failed, [], args.json)
    if holder:
        return _stop_requested_only(jid, f'runner pid {holder[0]} holds the job', killed, failed, [holder[0]],
                                    args.json)
    try:
        existing = common.read_json(_p(jid, 'result.json'))
        if isinstance(existing, dict) and existing.get('status') in TERMINAL:  # finished while we were stopping it
            _baton(spec.get('baton'), existing)
            common.emit({'id': jid, 'status': existing.get('status'), 'already_finished': True,
                         'killed_pids': killed}, args.json,
                        f"{jid}: finished before the stop took effect ({existing.get('status')})")
            return common.EXIT_OK
        receipt = {'id': jid, 'name': spec.get('name'), 'kind': spec.get('kind'), 'cwd': spec.get('cwd'),
                   'status': 'stopped', 'engine': st.get('engine') or (spec.get('primary') or {}).get('engine'),
                   'requested': {k: (spec.get('primary') or {}).get(k)
                                 for k in ('model', 'effort', 'tier', 'profile')},
                   'served_models': [], 'session_id': None, 'attempts': [], 'killed_pids': killed,
                   'kill_failed_pids': failed, 'stopped_at': common.iso(common.now()), 'final_path': None,
                   'events_path': None, 'baton': spec.get('baton'),
                   'notes': ['stopped by twinlane stop; partial output may be in the job folder']}
        _write_receipt(jid, receipt)
        err = _baton(spec.get('baton'), receipt)  # the runner is gone, so nothing else clears its in-flight item
        if err:
            receipt['notes'].append(f'baton end: {err}')
            _write_receipt(jid, receipt)
    finally:
        _release_runner(jid)
    common.emit(receipt, args.json, f"{jid}: stopped (killed {killed or 'nothing alive'})")
    return common.EXIT_OK


def _stop_requested_only(jid: str, why: str, killed: list, failed: list, alive: list, as_json: bool) -> int:
    """`twinlane stop` while a runner may still be alive: it writes no receipt (only the runner role does). The
    request stays set, so that runner launches nothing further and records the outcome itself."""
    msg = (f'{jid}: stop REQUESTED, not confirmed: {why}; no receipt written. {STOP_REQUESTED} is set, so the '
           f'runner launches no further attempt and records the outcome itself. Check `twinlane wait {jid}`, or '
           f'retry `twinlane stop {jid}`.')
    common.emit({'id': jid, 'status': 'stop_requested', 'killed_pids': killed, 'kill_failed_pids': failed,
                 'alive_pids': alive}, as_json, msg)
    return common.EXIT_RUNNING


def _stop_survivor(jid: str, receipt: dict, survivor: dict, as_json: bool) -> int:
    """`twinlane stop` on a job whose receipt exists but whose engine outlived the kill: end that process, keep the
    receipt as history. Same identity rule as everywhere else: only a process proven to be the recorded one dies."""
    pid, created = survivor['pid'], survivor.get('created')
    state = _survivor_state(survivor)
    if state != common.ALIVE:
        msg = (f'{jid}: engine identity unknown (pid {pid} has no creation stamp or its identity cannot be read '
               f'from here); not killed, job left as is. Stop it from the host or end the process by hand.')
        common.emit({'id': jid, 'status': 'unknown', 'identity_unknown_pids': [pid], 'killed_pids': []}, as_json, msg)
        return common.EXIT_FAILED
    killed = common.kill_tree(pid, created)
    _pid_gone(pid, created)  # a kill is asynchronous, and on Windows a cross-process kill is never reported as confirmed
    if common.pid_maybe_alive(pid, created):
        msg = (f'{jid}: stop FAILED: engine pid {pid} is still alive after the kill; the job stays orphaned and '
               f'holds its slot. Retry `twinlane stop {jid}` or end the process by hand.')
        common.emit({'id': jid, 'status': 'stop_failed', 'kill_failed_pids': [pid], 'alive_pids': [pid],
                     'killed_pids': []}, as_json, msg)
        return common.EXIT_FAILED
    common.emit({'id': jid, 'status': receipt.get('status'), 'already_finished': True, 'killed_pids': [pid] if killed
                 else []}, as_json, f"{jid}: stopped the engine process (pid {pid}) that outlived its kill; "
                 f"the receipt stays {receipt.get('status')}")
    return common.EXIT_OK


def _parse(ap: argparse.ArgumentParser, argv):
    """argparse exits on --help (0) and on bad input (2); return that code instead of exiting."""
    try:
        return ap.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_USAGE if e.code else common.EXIT_OK


# ---------------------------------------------------------------- listing / status

def _job_ids() -> list[str]:
    if _SLOT_PROBES.get() is not None:
        # An enclosing board/command snapshot must not hide a newly admitted job.
        return sorted(observations.Snapshot().directories(), reverse=True)
    return observations.job_ids()


def resolve_job(ref: str) -> str:
    ids = _job_ids()
    if ref in ids:
        return ref
    from . import archive
    archived = archive.archived_ids()
    if ref in archived:
        return ref
    ids += sorted(archived, reverse=True)
    hits = [j for j in ids if ref in j]
    if len(hits) == 1:
        return hits[0]
    raise LookupError(f'no job matches {ref!r}' if not hits else f'{ref!r} is ambiguous: {", ".join(hits[:5])}')


def _runner_pid(job_id: str) -> tuple[int | None, int | None]:
    """(pid, creation stamp) from runner.pid ("<pid> <created>"; older files hold only the pid)."""
    parts = _read_text(_p(job_id, 'runner.pid')).split()
    if _PRESERVING_METADATA.get():
        if len(parts) != 2 or len(parts[0]) > 10 or len(parts[1]) > 20 \
                or not all(p.isascii() and p.isdigit() for p in parts):
            return None, None
        pid, created = int(parts[0]), int(parts[1])
        return (pid, created) if common._valid_pid(pid) and common._valid_stamp(created) else (None, None)
    pid = int(parts[0]) if parts and parts[0].isdigit() else None
    created = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
    return pid, created


def _receipt_observed_child_exit(receipt, st) -> bool:
    """Recognize an observed engine exit, never a synthetic launch/stop exit code.

    Both done and failed receipts need the final attempt's post-wait evidence tied
    to the recorded child's attempt. This proves only that child's exit;
    descendants remain subject to the survivor marker.
    """
    if not isinstance(receipt, dict) or receipt.get('status') not in ('done', 'failed') \
            or type(receipt.get('exit_code')) is not int or 'survivor' in receipt:
        return False
    if receipt['status'] == 'done' and receipt['exit_code'] != 0:
        return False
    attempts = receipt.get('attempts')
    if not isinstance(attempts, list) or not attempts or not isinstance(attempts[-1], dict):
        return False
    last = attempts[-1]
    return isinstance(st, dict) and type(st.get('attempt')) is int and st['attempt'] > 0 \
        and type(last.get('n')) is int and last['n'] == st['attempt'] \
        and (last.get('engine') is None or st.get('engine') is None or last['engine'] == st['engine']) \
        and last.get('status') in ('done', 'failed') and type(last.get('exit_code')) is int \
        and last['exit_code'] == receipt['exit_code'] and isinstance(last.get('events_path'), str) \
        and bool(last['events_path']) and 'survivor' not in last


def _finished_child_pid_reused(job_id: str, st: dict, receipt=None) -> bool:
    """An exited child cannot own a later inaccessible process with its old pid.

    This exception applies only to unreadable creation identity, with a qualified
    completion and a DEAD runner. Unknown existence, launch uncertainty, survivors
    and a readable matching identity retain the ordinary blocking rules.
    """
    receipt = common.read_json(_p(job_id, 'result.json')) if receipt is None else receipt
    if not _receipt_observed_child_exit(receipt, st) or os.path.exists(_p(job_id, SURVIVOR)) \
            or os.path.exists(_p(job_id, CHILD_STARTING)) or os.path.exists(_p(job_id, TREE_UNRESOLVED)):
        return False
    pid, created = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(job_id)
    child, stamp = st.get('child_pid'), st.get('child_created')
    if not all((common._valid_pid(pid), common._valid_stamp(created),
                common._valid_pid(child), common._valid_stamp(stamp))) \
            or _pid_state(pid, created) != common.DEAD:
        return False
    obs = common.observe_process(child, stamp)
    return obs.get('state') == common.UNKNOWN and obs.get('exists') is True \
        and obs.get('actual_created') is None and obs.get('pid') == child \
        and obs.get('expected_created') == str(stamp)


def _receipt_status(job_id: str, receipt: dict) -> str:
    """A receipt cannot hide a child left in an unfinished status record.

    Cleanly finalized jobs keep their cheap receipt path. An unfinished record needs
    proof that the engine is gone; a runner may still be finishing receipt cleanup.
    Call under observations.scope() so launch markers are observed, never reclaimed.
    """
    if os.path.exists(_p(job_id, SURVIVOR)):
        survivor = _survivor(job_id)
        if survivor is None:
            return 'unknown'
        state = _survivor_state(survivor)
        if state != common.DEAD:
            return 'orphaned' if state == common.ALIVE else 'unknown'
    if _child_pending(job_id):
        return 'unknown'
    path = _p(job_id, 'status.json')
    st = common.read_json(path)
    if (st is None and os.path.exists(path)) or (st is not None and not isinstance(st, dict)):
        return 'unknown'
    if receipt.get('status') in TERMINAL and st is not None and st.get('status') not in TERMINAL:
        pid, created = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(job_id)
        runner = _pid_state(pid, created)
        if runner == common.UNKNOWN:
            return 'unknown'
        engine = _engine_state(job_id, st)
        if engine != common.DEAD:
            if engine == common.UNKNOWN and runner == common.DEAD \
                    and _finished_child_pid_reused(job_id, st, receipt):
                return receipt['status']
            if engine == common.UNKNOWN:
                return 'unknown'
            return (st.get('status') or 'running') if runner == common.ALIVE else 'orphaned'
    return receipt.get('status') or 'failed'


def _repair_finished_status(job_id: str, *, require_dead_runner=False) -> str:
    """Repair only on claim/wait/result/stop, after rechecking under status.lock.

    Preserve the recorded identities: the caller is collecting a finished job, not
    becoming its runner. Observation commands and protected metadata never write.
    Return repaired, already-final (final receipt, no repair needed or runner
    still finishing its own cleanup), unrepaired (acceptable evidence evaluated
    read-only in preserving/observing mode), or rejected. Rejection
    is never writer clearance, including when evidence changes under status.lock.
    Writer admission requires a DEAD runner even when receipt collection can
    already complete while that runner finishes its own cleanup.
    """
    read_only = _preserve_job(job_id) or observations.active()
    def outcome(receipt, st):
        if not isinstance(receipt, dict) or receipt.get('status') not in TERMINAL \
                or (st is not None and not isinstance(st, dict)):
            return 'rejected'
        if st is None or st.get('status') in TERMINAL:
            if not require_dead_runner and st and st.get('runner_finalized') is True \
                    and st.get('status') == receipt['status'] and 'survivor' not in receipt \
                    and not any(os.path.exists(_p(job_id, marker))
                                for marker in (CHILD_STARTING, SURVIVOR, TREE_UNRESOLVED)):
                return 'already-final'
            with observations.scope():
                if _writer_job_busy(job_id):
                    return 'rejected'
                if require_dead_runner:
                    current = st or {}
                    pid, created = ((current.get('pid'), current.get('pid_created'))
                                    if current.get('pid') else _runner_pid(job_id))
                    if _pid_state(pid, created) != common.DEAD:
                        return 'rejected'
            return 'already-final'
        pid, created = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(job_id)
        with observations.scope():
            runner = _pid_state(pid, created)
            if (runner == common.DEAD or (runner == common.ALIVE and not require_dead_runner)) \
                    and _receipt_status(job_id, receipt) == receipt['status']:
                return 'repairable' if runner == common.DEAD else 'already-final'
        return 'rejected'
    receipt = common.read_json(_p(job_id, 'result.json'))
    st = common.read_json(_p(job_id, 'status.json'))
    decision = outcome(receipt, st)
    if read_only:
        return 'rejected' if decision == 'rejected' else 'unrepaired'
    if decision != 'repairable':
        return decision
    with lifecycle_lock(job_id, 'status.lock'):
        receipt = common.read_json(_p(job_id, 'result.json'))
        st = common.read_json(_p(job_id, 'status.json'))
        decision = outcome(receipt, st)
        if decision != 'repairable':
            return decision
        st.update(status=receipt['status'], updated_at=common.iso(common.now()))
        _write_job_json(job_id, 'status.json', st)
        return 'repaired'


@observations.command
def job_status(job_id: str) -> str:
    """The job's state. alive -> its recorded status; dead -> the receipt, else `orphaned` (runner gone, engine
    child alive) or `lost`; exists but identity unreadable -> `unknown`, never `lost` (a sandbox cannot read
    the host's runners, and a false `lost` invites a duplicate worker). A receipt whose engine outlived its
    kill reads `orphaned` until that process is gone."""
    r = common.read_json(_p(job_id, 'result.json'))
    if isinstance(r, dict):
        return _receipt_status(job_id, r)
    st = common.read_json(_p(job_id, 'status.json'), {}) or {}
    if not isinstance(st, dict) and _child_pending(job_id):
        return 'unknown'
    status = st.get('status') or 'queued'
    pid, created = (st.get('pid'), st.get('pid_created')) if st.get('pid') else _runner_pid(job_id)
    if pid:
        state = common.pid_state(pid, created)
        if state == common.ALIVE:
            return status
        if state == common.UNKNOWN:
            return 'unknown'
        # The runner may have just written result.json and exited.
        r = common.read_json(_p(job_id, 'result.json')) or {}
        if r.get('status'):
            return _receipt_status(job_id, r)
        # Runner gone but its engine child still running: not finished, and not safe to redo.
        engine = _engine_state(job_id, st)  # the child in status.json, or a survivor.json written before a crash
        return 'orphaned' if engine == common.ALIVE else 'unknown' if engine == common.UNKNOWN else 'lost'
    if _child_pending(job_id):
        return 'unknown'
    age = common.age_s(st.get('created_at'))
    return 'lost' if age is not None and age > QUEUED_WITHOUT_PID_LOST_S else status


@observations.command
def list_jobs(limit: int | None = 20) -> list[dict]:
    rows = []
    for jid in _job_ids()[:limit] if limit else _job_ids():
        spec = observations.job_json(jid, default={}) or {}
        r = observations.job_json(jid, 'result.json')
        st = observations.job_json(jid, 'status.json', {}) or {}
        prim = spec.get('primary') or {}
        started = (r or {}).get('started_at') or st.get('started_at') or spec.get('created_at')
        served = (r or {}).get('served_models') or []
        rows.append({'id': jid, 'name': spec.get('name'), 'kind': spec.get('kind'),
                     'engine': (r or {}).get('engine') or st.get('engine') or prim.get('engine'),
                     'model': served[0] if served else prim.get('model') or prim.get('tier'),
                     'status': job_status(jid), 'cwd': spec.get('cwd'), 'started_at': started,
                     'age': _human_age(common.age_s(started)), 'age_s': common.age_s(started),
                     'duration_s': (r or {}).get('duration_s')})
    return rows


# ---------------------------------------------------------------- printing

def _print_statuses(statuses: dict, as_json: bool, timed_out: bool, finished: list[str],
                    any_mode: bool = False, timeout: float | None = None) -> None:
    remaining = [j for j in statuses if j not in finished]
    if as_json:
        common.emit({'timed_out': timed_out, 'jobs': statuses, 'finished': finished, 'remaining': remaining}, True)
        return
    for j, s in statuses.items():
        print(f'{j}: {s}')
    if finished:
        print('returned: ' + ' '.join(finished))
        if remaining:
            print('wait on remaining: twinlane wait ' + ' '.join(remaining) + (' --any' if any_mode else '')
                  + (f' --timeout {timeout:g}' if timeout is not None else ''))
    if timed_out:
        print('still running: wait again or twinlane stop <id>')


def _attempt_models(receipt: dict) -> str:
    """Requested and served model per attempt, so a failover never reads as one engine serving another's
    model. A receipt without attempts (a start receipt, an older one) shows its top-level fields as a1."""
    attempts = [a for a in receipt.get('attempts') or [] if isinstance(a, dict)]
    if not attempts:
        req = receipt.get('requested') or {}
        attempts = [{'n': 1, 'engine': receipt.get('engine'), 'model': req.get('model'), 'tier': req.get('tier'),
                     'served_models': receipt.get('served_models')}]
    parts = []
    for a in attempts:
        served = ','.join(str(m) for m in a.get('served_models') or []) or '-'
        note = ''
        if a.get('engine') == 'codex':
            note = ' (as requested; no served evidence)'
        elif a.get('served_models_note'):
            note = ' (unverified: init-model fallback)'
        parts.append(f"a{a.get('n')} {a.get('engine')} requested={a.get('model') or a.get('tier') or '-'} "
                     f"served={served}{note}")
    return ' | '.join(parts)


def _fmt_delta(v: float) -> str:
    return f'{v:+.1f}'.replace('.0', '') + 'pp'


def _fmt_tokens(t: dict) -> str:
    names = (('input_tokens', 'in'), ('cached_input_tokens', 'cached'), ('cache_write_input_tokens', 'cache-write'),
             ('output_tokens', 'out'), ('reasoning_output_tokens', 'reasoning'), ('total_tokens', 'total'))
    return ' '.join(f'{label} {t[key]:,}' for key, label in names if key in t) + f" ({t.get('source')})"


def usage_lines(receipt: dict) -> list[str]:
    """The receipt's usage block as plain lines: the account-window delta per engine the job ran on
    (labelled APPROXIMATE with the reasons) and the job's own tokens."""
    from . import meter as meter_mod
    u = receipt.get('usage')
    if not isinstance(u, dict):
        return []
    if u.get('error'):
        return [f"usage: unavailable ({u['error']})"]
    out = []
    for eng in u.get('engines') or []:
        e = u.get(eng) or {}
        if e.get('delta') is None:
            out.append(f"usage {eng}: no delta ({e.get('why')})")
            continue
        parts = [f"{meter_mod.SHORT_NAMES.get(n, n)} {_fmt_delta(v)}" for n, v in e['delta'].items()
                 if n in meter_mod.CORE_WINDOWS or v]
        line = f'usage {eng}: ' + (', '.join(parts) or 'no shared window')
        if e.get('skipped'):
            line += ' (skipped: ' + ', '.join(f'{n} reset during the job' for n in e['skipped']) + ')'
        if e.get('approximate'):
            reasons = list(e.get('reasons') or [])
            cut = 'overlap scan truncated' in reasons
            rest = [r for r in reasons if r != 'overlap scan truncated']
            line += (' APPROXIMATE (overlap scan truncated)' if cut else ' APPROXIMATE') \
                + ((': ' if not cut else '; ') + '; '.join(rest) if rest else '')
        out.append(line)
    if u.get('after_note'):
        out.append(u['after_note'])
    if u.get('cache_note'):
        out.append(u['cache_note'])
    for eng, t in (u.get('tokens') or {}).items():
        out.append(f'tokens {eng}: ' + _fmt_tokens(t))
    return out


def _print_result(receipt: dict, as_json: bool, full: bool, max_lines: int = FINAL_LINES) -> None:
    path = receipt.get('final_path')
    if path:
        old_root = os.path.join(common.JOBS, receipt['id'])
        try:
            relative = os.path.relpath(path, old_root)
        except ValueError:  # an external final path on another Windows drive
            relative = None
        if relative is not None and relative != os.pardir \
                and not relative.startswith(os.pardir + os.sep) and not os.path.isabs(relative):
            path = os.path.join(job_dir(receipt['id']), relative)
    final = _read_text(path) if path else ''
    if as_json:
        common.emit(dict(receipt, final_text=final), True)
        return
    print(f"{receipt['id']}  status={receipt['status']}  engine={receipt.get('engine')}")
    if receipt.get('error'):
        print(f"error: {receipt['error']}")
    if receipt.get('finished_at'):
        print(f"finished: {receipt['finished_at']}")
    print('models: ' + _attempt_models(receipt))
    print(f"session={receipt.get('session_id') or '-'}  model_calls={receipt.get('model_calls')}  "
          f"cli_turns={receipt.get('num_turns')}  "
          f"duration={receipt.get('duration_s')}s  denials={receipt.get('permission_denials')}  "
          f"touched={len(receipt['touched_files']) if receipt.get('touched_files') is not None else '?'}  "
          f"read_only_violation={receipt.get('read_only_violation')}")
    if receipt.get('failover'):
        fo = receipt['failover']
        print(f"failover: {fo['from']} -> {fo['to']} ({fo['reason']})")
    for a in receipt.get('attempts') or []:
        if a.get('failover_refused'):
            print(f"failover refused: {a['failover_refused']}")
    for n in receipt.get('notes') or []:
        print(f'note: {n}')
    if receipt.get('touched_files'):
        print('touched: ' + ', '.join(receipt['touched_files'][:20]))
    for line in usage_lines(receipt):
        print(line)
    if final:
        lines = final.splitlines()
        print('----- final message -----')
        shown = lines if full else lines[:max_lines]
        print('\n'.join(shown))
        if len(lines) > len(shown):
            print(f'... {len(lines) - len(shown)} more lines: twinlane result {receipt["id"]} --full')
    print(f"evidence: {job_dir(receipt['id'])}")


# ---------------------------------------------------------------- entry

COMMANDS = {'run': cmd_run, 'jobs': cmd_jobs, 'wait': cmd_wait, 'result': cmd_result, 'stop': cmd_stop,
            '_job': cmd_job}


def cli(cmd: str, argv) -> int:
    fn = COMMANDS.get(cmd)
    if fn is None:
        print(f'twinlane: unknown jobs command {cmd!r}', file=sys.stderr)
        return common.EXIT_USAGE
    return fn(list(argv))
