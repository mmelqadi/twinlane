"""Batons (one small engine-neutral JSON per task) and handoff briefs.

A baton lives at batons/<slug>.json; each write appends the previous version to
batons/<slug>.history.jsonl (last 50 kept). `handoff` renders a self-contained Markdown
continuation brief from the baton plus a bounded black-box read of the newest Claude
transcript and Codex rollout for the same cwd, fresh git state and live twinlane jobs.

Never spends model tokens: everything here is file and git reads.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import math
import os
import re
import sys
import time

from twinlane import common

# Tests point these at fixture dirs.
CLAUDE_PROJECTS = os.path.join(common.CLAUDE_DIR, 'projects')
CODEX_SESSIONS = os.path.join(common.CODEX_DIR, 'sessions')

SLUG_RE = re.compile(r'^[a-z0-9-]{1,60}$')
HISTORY_KEEP = 50
REASONS = ('checkpoint', 'limit', 'handoff', 'job', 'user')
ENGINES = ('claude', 'codex')
ACCEPT_STATUSES = ('not_run', 'fail', 'pass', 'blocked', 'n/a')
EVIDENCE_KINDS = ('source', 'test', 'review', 'integration', 'deploy', 'prod')
LIVE_JOB_STATUSES = ('queued', 'running', 'orphaned')

TAIL_BYTES = 2_000_000
EXCERPT_CHARS = 1500
CODEX_LOOKBACK_DAYS = 14
CODEX_SCAN_MAX = 12          # rollouts for one cwd whose tails are parsed while choosing
CODEX_BACKSCAN_BYTES = (8_000_000, 24_000_000)  # wider tails tried when the 2 MB tail has no user turn
CLAUDE_SCAN_MAX = 15
CLAUDE_META_TAIL = 256_000
OTHER_SESSION_WINDOW_S = 2 * 3600
WORKER_ENTRYPOINTS = ('sdk-cli',)  # `claude -p` workers: twinlane jobs, Codex->Claude dispatches, subagent SDK runs
HANDOFF_MAX_BYTES = 8192
BATON_MAX_BYTES = 8192
BATON_FIELD_MAX_CHARS = 300
BATON_QUOTE_MAX_CHARS = 600
RESUME_IDLE_S = 300
CLAUDE_DIR_NAME_MAX = 200  # Claude Code truncates longer project-dir names and appends a hash

# List caps bound counts; save() clips text and trims the oldest entries to stay within BATON_MAX_BYTES.
CAPS = {'done': 30, 'then': 10, 'decisions': 30, 'rejected': 20, 'evidence': 30, 'jobs': 20,
        'read_first': 5, 'do_not': 20, 'verify_before_act': 10, 'dirty': 25, 'in_flight': 10}

FOOTER = 'Approvals do not transfer: re-ask for any live write.'

USER_NOISE_PREFIXES = ('<command-', '<local-command', '<task-notification', '<system-reminder', '<heartbeat',
                       '<environment_context', '<user_instructions', '<turn_aborted', 'Caveat: The messages below',
                       '<external_codex_apps_open_page', '<send_user_message_question_reply', '# AGENTS.md instructions',
                       '<INSTRUCTIONS>', 'This session is being continued from a previous conversation')
AGENT_NOISE_PREFIXES = ('[external_agent_tool_call', '[external_agent_tool_result')

_WORD = re.compile(r'(?:[A-Z][a-z]{2,}|[a-z]{3,}|[A-Z]{3,}|\d+)+')


def _wordish(run: str) -> float:
    """Share of the run made of words (Capitalised, lower, UPPER, digits) between `-`/`_`/`+`/`=`: a name
    like `ReviewWorktree` is made of words, random base64 almost never is (about 1 in
    10,000 random 32-character secrets reach 0.7; none of 43 characters or more did in 100,000 trials)."""
    segs = [s for s in re.split(r'[-_+=/]+', run) if s]
    return sum(len(s) for s in segs if _WORD.fullmatch(s)) / len(run)


def _blob(m: 're.Match') -> str:
    """A long base64/base64url-looking run is a secret when it mixes lower, upper and digits at high entropy.
    The run never contains a path separator (the pattern excludes `/` and `\\`), so a path is judged one
    component at a time; a run made of words (names, branches, identifiers) is kept."""
    run = m.group(0).strip('=')
    if len(run) < 32 or not (re.search('[a-z]', run) and re.search('[A-Z]', run) and re.search('[0-9]', run)):
        return m.group(0)
    if _wordish(run) >= 0.7:
        return m.group(0)
    return '[REDACTED-BLOB]' if _entropy(run) >= 4.0 else m.group(0)


def _entropy(run: str) -> float:
    counts = {c: run.count(c) for c in set(run)}
    return -sum(n / len(run) * math.log2(n / len(run)) for n in counts.values())


_PATH_ROOT = re.compile(r'/(?:/|[a-zA-Z]/|home/|Users/|tmp/|mnt/|var/|private/|opt/|etc/|usr/|root/|srv/|workspace)')
# macOS per-user temp: /var/folders/<2>/<random>/T/ (and /private/var/..., what /var resolves to)
_MAC_TEMP = re.compile(r'(?:/private)?/var/folders/[\w+-]{2}/[\w+-]{20,40}/[TCX]/')
_REF_BEFORE = re.compile(r'(?i)(?:\b(?:branch|ref|refs|origin|checkout|switch)\b[\s:=]*|\[)$')
_GIT_REF_BEFORE = re.compile(r'(?i)(?:\b(?:commit|sha1?|head|merge|tree|parent|object|rev|revision|tag|'
                             r'checkout|cherry-pick|reset|revert|rebase)\b[\s:=#@(]*|\.\.\.?)$')


def _path_context(m: 're.Match') -> bool:
    """Shape alone cannot tell `/Word1/Word2...` from a base64 secret built of word-like parts, so a run with
    `/` is a path only by CONTEXT: it follows a drive or URL host (`C:`, `x.com`, `.`), a `~`, starts at a
    known filesystem root (`/home/`, `/Users/`, git-bash `/c/`, UNC `//`, ...), or is named as a git ref
    (`branch feature/X`, `[feature/X]`)."""
    before = m.string[m.start() - 1] if m.start() else ''
    return ((before != '' and before in ':.~') or bool(_PATH_ROOT.match(m.group(0)))
            or bool(_REF_BEFORE.search(m.string[max(0, m.start() - 12):m.start()])))


def _blob_slash(m: 're.Match') -> str:
    """Standard base64 contains `/` (an AWS secret key does), and cutting it at every `/` could leave
    fragments under 32 characters. So a run that contains `/` is judged whole. Fail closed: outside a path
    context (see `_path_context`) a long mixed-case high-entropy run is masked even when its parts read as
    words; inside one it is kept unless under half of its characters are words. Without a `/` the
    per-component rule in `_blob` applies."""
    run = m.group(0).strip('=')
    if '/' not in run:
        return m.group(0)
    temp = _MAC_TEMP.match(run)
    if temp:  # the OS names this folder at random; only what follows it is judged
        return '[REDACTED-BLOB]' if _slash_run_is_secret(run[temp.end():], True) else m.group(0)
    return '[REDACTED-BLOB]' if _slash_run_is_secret(run, _path_context(m)) else m.group(0)


def _slash_run_is_secret(run: str, path_context: bool) -> bool:
    if len(run) < 32 or not (re.search('[a-z]', run) and re.search('[A-Z]', run) and re.search('[0-9]', run)):
        return False
    if _entropy(run) < 4.0:
        return False
    return not (path_context and _wordish(run) >= 0.5)


def _hex(m: 're.Match') -> str:
    """32+ hex characters are masked, except a git object id (exactly 40 or 64) named as one by the text
    just before it (`commit <sha>`, `HEAD at <sha>`, `a..<sha>`): their characters alone cannot tell a SHA
    from a hex secret, the context can."""
    run = m.group(0)
    if len(run) in (40, 64) and _GIT_REF_BEFORE.search(m.string[max(0, m.start() - 24):m.start()]):
        return run
    if len(run) in (40, 64) and m.string[m.end():m.end() + 2] == '..':
        return run
    return '[REDACTED-HEX]'


# One list for every display and handoff path. Specific token shapes first, generic key=value and
# high-entropy runs last. Guarantee: these shapes are masked; nothing else is promised.
SECRET_PATTERNS = [
    (re.compile(r'-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)', re.S),
     '[REDACTED-PEM]'),
    (re.compile(r'(?<![A-Za-z0-9])sk-(?:ant-)?[A-Za-z0-9_\-]{16,}'), 'sk-[REDACTED]'),
    (re.compile(r'(?<![A-Za-z0-9])[sr]k_(?:live|test)_[A-Za-z0-9]{10,}'), '[REDACTED-STRIPE]'),
    (re.compile(r'\b(?:ghp|gho|ghs|ghu|ghr)_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}'), '[REDACTED-GH]'),
    (re.compile(r'\bxox[abposr]-[A-Za-z0-9-]{10,}'), '[REDACTED-SLACK]'),
    (re.compile(r'hooks\.slack\.com/services/[A-Za-z0-9/]+'), 'hooks.slack.com/services/[REDACTED]'),
    (re.compile(r'\b(?:AKIA|ASIA)[0-9A-Z]{16}\b'), '[REDACTED-AWS]'),
    (re.compile(r'\bAIza[0-9A-Za-z_\-]{30,}'), '[REDACTED-GOOGLE]'),
    (re.compile(r'\bnpm_[A-Za-z0-9]{30,}'), '[REDACTED-NPM]'),
    (re.compile(r'\beyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}'), '[REDACTED-JWT]'),
    (re.compile(r'(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=\-]{16,}'), r'\1 [REDACTED]'),
    (re.compile(r'(://[^/\s:@]+:)[^@\s/]+@'), r'\1[REDACTED]@'),
    (re.compile(r'(?i)(["\']?\b[\w.\-]*(?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|'
                r'private[_-]?key)[\w.\-]*["\']?)(\s*[:=]\s*)("[^"]*"|\'[^\']*\'|[^\s,;]+)'), r'\1\2[REDACTED]'),
    (re.compile(r'(?i)(\b[\w.\-]*key)(=)("[^"]*"|\'[^\']*\'|[^\s,;&]+)'), r'\1\2[REDACTED]'),
    (re.compile(r'(?<![0-9A-Za-z])[0-9a-fA-F]{32,}(?![0-9A-Za-z])'), _hex),
    (re.compile(r'(?<![A-Za-z0-9+/_=\-])[A-Za-z0-9+/_=\-]{32,}'), _blob_slash),
    (re.compile(r'(?<![A-Za-z0-9+_=\-])[A-Za-z0-9+_=\-]{32,}'), _blob),
]


class BatonError(Exception):
    """A usage or validation error; the CLI maps it to exit 2."""


# ---------------------------------------------------------------- paths / slugs

def valid_slug(slug) -> bool:
    return isinstance(slug, str) and bool(SLUG_RE.match(slug))


def require_slug(slug) -> str:
    if not valid_slug(slug):
        raise BatonError(f'invalid slug {slug!r}: use 1-60 chars of a-z, 0-9 and -')
    return slug


def baton_path(slug: str) -> str:
    return os.path.join(common.BATONS, require_slug(slug) + '.json')


def history_path(slug: str) -> str:
    return os.path.join(common.BATONS, require_slug(slug) + '.history.jsonl')


def brief_path(slug: str) -> str:
    return os.path.join(common.BATONS, require_slug(slug) + '.handoff.md')


def norm_dir(path: str | None) -> str | None:
    """Absolute, OS-native directory path; accepts Git Bash style /c/x/y on Windows."""
    if not path:
        return None
    p = str(path).strip().strip('"')
    m = re.match(r'^/([a-zA-Z])(/|$)(.*)$', p)
    if common.IS_WIN and m:
        p = m.group(1).upper() + ':\\' + m.group(3)
    p = common.real_path(p)
    return p.rstrip('\\/') if len(p) > 3 else p


def same_dir(a, b) -> bool:
    if not a or not b:
        return False
    return common.canon_path(str(a)) == common.canon_path(str(b))


def under_dir(child, parent) -> bool:
    if not child or not parent:
        return False
    c = common.canon_path(str(child))
    p = common.canon_path(str(parent))
    return c == p or c.startswith(p.rstrip('\\/') + os.sep)


fmt_age = common.fmt_age


def redact(text) -> str:
    text = '' if text is None else str(text)
    for rx, repl in SECRET_PATTERNS:
        text = rx.sub(repl, text)
    return text


# ---------------------------------------------------------------- baton io

def empty_baton(slug: str) -> dict:
    return {
        'baton_version': 1, 'slug': require_slug(slug), 'updated_at': None, 'reason': 'user',
        'owner': {'engine': None, 'session_id': None, 'model': None},
        'task': {'tracker_id': None, 'title': None, 'goal': None, 'acceptance': []},
        'repo': {'root': None, 'worktree': None, 'branch': None, 'head': None, 'base': None, 'pushed': None,
                 'dirty': [], 'captured_at': None},
        'progress': {'done': [], 'in_flight': [], 'next_action': None, 'then': []},
        'decisions': [], 'rejected': [],
        'holds': {'approvals_pending': [], 'approvals_granted': [], 'approvals_rejected': [], 'prohibitions': []},
        'evidence': [], 'jobs': [], 'read_first': [], 'do_not': [], 'verify_before_act': [],
        'meter_at_write': {'claude_max_pct': None, 'codex_max_pct': None},
    }


def _merge_defaults(baton: dict) -> dict:
    """Fill sections missing from an older or hand-written baton, without touching present values."""
    base = empty_baton(baton.get('slug'))
    for k, v in base.items():
        if k not in baton or baton[k] is None:
            baton[k] = v
        elif isinstance(v, dict) and isinstance(baton[k], dict):
            for kk, vv in v.items():
                baton[k].setdefault(kk, vv)
    return baton


def load(slug: str) -> dict | None:
    data = common.read_json(baton_path(slug))
    if not isinstance(data, dict):
        return None
    data.setdefault('slug', slug)
    return _merge_defaults(data)


def baton_lock(slug: str):
    """Per-slug OS lock around every load-modify-save, so a job receipt, a snap and a handoff
    writing the same baton never drop each other's update. Not re-entrant: take it once."""
    return common.file_lock(os.path.join(common.BATONS, f'{slug}.lock'), timeout_s=60)


def load_for_update(slug: str) -> dict | None:
    """load(), but a baton that exists and cannot be read is an error, never an empty start."""
    b = load(slug)
    if b is None and os.path.exists(baton_path(slug)):
        raise BatonError(f'baton {slug} exists but cannot be read ({baton_path(slug)}); not overwriting it')
    return b


def validate(baton) -> list[str]:
    """Schema and storage bounds for every write. Returns problems (empty = valid)."""
    if not isinstance(baton, dict):
        return ['baton must be a JSON object']
    errs = []
    if baton.get('baton_version') != 1:
        errs.append('baton_version must be 1')
    if not valid_slug(baton.get('slug')):
        errs.append('slug must match [a-z0-9-]{1,60}')
    if baton.get('reason') is not None and baton.get('reason') not in REASONS:
        errs.append(f'reason must be one of {REASONS}')
    for key in ('owner', 'task', 'repo', 'progress', 'holds', 'meter_at_write'):
        if key in baton and not isinstance(baton[key], dict):
            errs.append(f'{key} must be an object')
    for key in ('decisions', 'rejected', 'evidence', 'jobs', 'read_first', 'do_not', 'verify_before_act'):
        if key in baton and not isinstance(baton[key], list):
            errs.append(f'{key} must be a list')
    owner = baton.get('owner') if isinstance(baton.get('owner'), dict) else {}
    if owner.get('engine') not in (None,) + ENGINES:
        errs.append(f'owner.engine must be one of {ENGINES}')
    task = baton.get('task') if isinstance(baton.get('task'), dict) else {}
    acc = task.get('acceptance', [])
    if not isinstance(acc, list):
        errs.append('task.acceptance must be a list')
    else:
        for a in acc:
            if not isinstance(a, dict) or not a.get('id') or a.get('status', 'not_run') not in ACCEPT_STATUSES:
                errs.append(f'bad acceptance item {a!r}')
    for e in baton.get('evidence', []) if isinstance(baton.get('evidence'), list) else []:
        if not isinstance(e, dict) or e.get('kind') not in EVIDENCE_KINDS:
            errs.append(f'bad evidence item {e!r}')
    if isinstance(baton.get('read_first'), list) and len(baton['read_first']) > CAPS['read_first']:
        errs.append('read_first holds at most 5 paths')
    def check_text(value, path='baton'):
        if isinstance(value, str):
            limit = BATON_QUOTE_MAX_CHARS if path.endswith('.quote') else BATON_FIELD_MAX_CHARS
            if len(value) > limit:
                errs.append(f'{path} exceeds {limit} characters; keep pointers, not prose')
        elif isinstance(value, dict):
            for key, item in value.items():
                check_text(item, f'{path}.{key}')
        elif isinstance(value, list):
            for index, item in enumerate(value):
                check_text(item, f'{path}[{index}]')
    check_text(baton)
    size = len(json.dumps(baton, ensure_ascii=False, indent=2).encode('utf-8'))
    if size > BATON_MAX_BYTES:
        errs.append(f'baton is {size} bytes; maximum is {BATON_MAX_BYTES} bytes')
    return errs


def _append_history(slug: str, previous: dict) -> None:
    path = history_path(slug)
    try:
        with open(path, encoding='utf-8') as h:
            lines = [ln for ln in h.read().splitlines() if ln.strip()]
    except OSError:
        lines = []
    lines.append(json.dumps(previous, ensure_ascii=False, separators=(',', ':')))
    common.write_text(path, '\n'.join(lines[-HISTORY_KEEP:]) + '\n')


def _apply_caps(baton: dict) -> None:
    prog = baton['progress']
    for key in ('done', 'then', 'in_flight'):
        prog[key] = prog.get(key, [])[-CAPS[key]:]
    for key in ('decisions', 'rejected', 'evidence', 'jobs', 'do_not', 'verify_before_act', 'read_first'):
        baton[key] = baton.get(key, [])[-CAPS[key]:]


def _clip_strings(value, path='baton'):
    """Clip every over-long string in place (system writes never refuse on length)."""
    items = value.items() if isinstance(value, dict) else enumerate(value) if isinstance(value, list) else ()
    for key, item in list(items):
        sub = f'{path}.{key}' if isinstance(value, dict) else f'{path}[{key}]'
        if isinstance(item, str):
            value[key] = common.clip(item, BATON_QUOTE_MAX_CHARS if sub.endswith('.quote') else BATON_FIELD_MAX_CHARS)
        else:
            _clip_strings(item, sub)


def _baton_bytes(baton: dict) -> int:
    return len(json.dumps(baton, ensure_ascii=False, indent=2).encode('utf-8'))


def _fit_size(baton: dict) -> None:
    """Drop the oldest history-like entries until the baton fits BATON_MAX_BYTES.
    Never drops live jobs, in-flight items, holds, do_not, verify_before_act or the task."""
    pools = [baton['progress'].get('done', []), baton.get('evidence', []), baton.get('rejected', []),
             baton.get('decisions', [])]
    while _baton_bytes(baton) > BATON_MAX_BYTES:
        done_jobs = [j for j in baton.get('jobs', [])
                     if not (isinstance(j, dict) and j.get('status') in LIVE_JOB_STATUSES)]
        if done_jobs:
            baton['jobs'].remove(done_jobs[0])
            continue
        pool = next((p for p in pools if p), None)
        if pool is None:
            return
        pool.pop(0)


def save(baton: dict, reason: str = 'user', strict: bool = False) -> dict:
    """Write the baton atomically, moving the previous version into history first.
    strict=True (new/set from user input) refuses over-long text; other writers clip and trim."""
    if reason not in REASONS:
        raise BatonError(f'reason must be one of {REASONS}')
    slug = require_slug(baton.get('slug'))
    common.ensure_dirs()
    _apply_caps(baton)
    baton['reason'] = reason
    baton['updated_at'] = common.iso(common.now())
    if strict:
        errs = [e for e in validate(baton) if 'bytes; maximum' not in e]
        if errs:
            raise BatonError('invalid baton: ' + '; '.join(errs[:5])
                             + ' (shorten it; an old field: edit `twinlane baton show SLUG --json` and reload with --file)')
    _clip_strings(baton)
    _fit_size(baton)
    errs = [e for e in validate(baton) if 'bytes; maximum' not in e or strict]
    if errs:
        raise BatonError('invalid baton: ' + '; '.join(errs[:5]))
    previous = common.read_json(baton_path(slug))
    if isinstance(previous, dict):
        _append_history(slug, previous)
    common.write_json(baton_path(slug), baton)
    try:  # list_batons breaks updated_at (1 s) ties by mtime; Windows' write mtime is one coarse clock tick
        t = time.time_ns()
        os.utime(baton_path(slug), ns=(t, t))
    except OSError:
        pass
    return baton


def history(slug: str) -> list[dict]:
    try:
        with open(history_path(slug), encoding='utf-8') as h:
            out = []
            for ln in h:
                try:
                    out.append(json.loads(ln))
                except ValueError:
                    continue
            return out
    except OSError:
        return []


# ---------------------------------------------------------------- mechanical state (snap)

def _main_root(cwd: str, worktree: str) -> str:
    rc, common_dir = common.git(cwd, 'rev-parse', '--path-format=absolute', '--git-common-dir')
    if rc == 0 and os.path.basename(common_dir.rstrip('\\/')) == '.git':
        return norm_dir(os.path.dirname(common_dir.rstrip('\\/')))
    return worktree


def _pushed_state(cwd: str, upstream: str | None) -> str:
    if not upstream:
        return 'no upstream'
    rc, ahead = common.git(cwd, 'rev-list', '--count', '@{u}..HEAD')
    if rc != 0:
        return f'unknown vs {upstream}'
    return f'in {upstream}' if ahead == '0' else f'{ahead} commit(s) ahead of {upstream}'


def repo_fields(cwd: str, previous: dict | None = None, st: dict | None = None) -> dict:
    previous = previous or {}
    st = common.git_state(cwd) if st is None else st
    if not st:
        return dict(previous, worktree=cwd, note='not a git repo')
    worktree = norm_dir(st['root'])
    return {'root': _main_root(cwd, worktree), 'worktree': worktree, 'branch': st.get('branch'),
            'head': st.get('head'), 'base': previous.get('base'), 'pushed': _pushed_state(cwd, st.get('upstream')),
            'dirty': _shown_dirty(st.get('dirty', [])), 'linked_worktree': st.get('linked_worktree', False),
            'captured_at': common.iso(common.now())}


def _shown_dirty(dirty: list) -> list:
    shown = [common.clip(d, BATON_FIELD_MAX_CHARS) for d in dirty[:CAPS['dirty']]]
    if len(dirty) > len(shown):
        shown.append(f'... (+{len(dirty) - len(shown)} more)')
    return shown


def _porcelain_v2(text: str) -> dict:
    """branch, head, upstream, ahead and v1-style dirty lines from `git status --porcelain=v2 -b`."""
    out = {'branch': None, 'head': None, 'upstream': None, 'ahead': None, 'dirty': []}
    for ln in text.splitlines():
        if ln.startswith('# branch.oid '):
            oid = ln[len('# branch.oid '):]
            out['head'] = None if oid == '(initial)' else oid
        elif ln.startswith('# branch.head '):
            head = ln[len('# branch.head '):]
            out['branch'] = 'HEAD' if head == '(detached)' else head
        elif ln.startswith('# branch.upstream '):
            out['upstream'] = ln[len('# branch.upstream '):]
        elif ln.startswith('# branch.ab '):
            m = re.match(r'# branch\.ab \+(\d+) -\d+', ln)
            out['ahead'] = int(m.group(1)) if m else None
        elif ln.startswith('? '):
            out['dirty'].append('?? ' + ln[2:])
        elif ln[:2] in ('1 ', '2 ', 'u '):
            fields = ln.split(' ', {'1': 8, '2': 9, 'u': 10}[ln[0]])
            xy = fields[1].replace('.', ' ')
            path = fields[-1]
            if ln[0] == '2':
                path, _, orig = path.partition('\t')
                path = f'{orig} -> {path}'
            out['dirty'].append(f'{xy} {path}')
    return out


def refresh_repo(baton: dict) -> bool:
    """Refresh the saved repo block's branch, head, pushed state and dirty list from ONE
    `git status` (read-only, no optional locks; about 50 ms) and stamp repo.captured_at.
    False, with the block left as it was, when no recorded directory exists or git fails."""
    repo = baton['repo']
    cwd = repo.get('worktree') or repo.get('root')
    if not cwd or not os.path.isdir(cwd):
        return False
    rc, text = common.git(cwd, '--no-optional-locks', 'status', '--porcelain=v2', '-b', '--untracked-files=all',
                          timeout=10)
    if rc != 0:
        return False
    st = _porcelain_v2(text)
    if st['upstream'] is None:
        pushed = 'no upstream'
    elif st['ahead'] is None:
        pushed = f"unknown vs {st['upstream']}"
    else:
        pushed = f"in {st['upstream']}" if st['ahead'] == 0 else f"{st['ahead']} commit(s) ahead of {st['upstream']}"
    repo.update(branch=st['branch'], head=st['head'], pushed=pushed, dirty=_shown_dirty(st['dirty'][:200]),
                captured_at=common.iso(common.now()))
    return True


def cached_meter() -> dict:
    """Meter numbers from the cache only: snap and handoff must stay fast and offline."""
    m = common.read_json(os.path.join(common.STATE, 'meter.json'))
    if not isinstance(m, dict):
        return {'claude_max_pct': None, 'codex_max_pct': None, 'note': 'no cached meter; run twinlane meter'}
    claude, codex = m.get('claude') or {}, m.get('codex') or {}
    return {'claude_max_pct': claude.get('max_pct'), 'codex_max_pct': codex.get('max_pct'),
            'claude_status': claude.get('status'), 'codex_status': codex.get('status'),
            'claude_next_reset': claude.get('next_reset'), 'codex_next_reset': codex.get('next_reset'),
            'read_at': m.get('read_at')}


def _scan_jobs_dir(limit: int) -> list[dict]:
    """Fallback when jobs.py is unavailable: read jobs/<id>/{job,status,result}.json directly."""
    try:
        ids = sorted((d for d in os.listdir(common.JOBS) if os.path.isdir(os.path.join(common.JOBS, d))),
                     reverse=True)[:limit]
    except OSError:
        return []
    out = []
    for jid in ids:
        d = os.path.join(common.JOBS, jid)
        spec = common.read_json(os.path.join(d, 'job.json'), {}) or {}
        st = common.read_json(os.path.join(d, 'status.json'), {}) or {}
        res = common.read_json(os.path.join(d, 'result.json'), {}) or {}
        status = res.get('status') or st.get('status') or 'queued'
        if status == 'running' and st.get('pid') and not common.pid_alive(st.get('pid')):
            status = 'stale'
        started = st.get('started_at') or spec.get('created_at')
        out.append({'id': jid, 'name': spec.get('name'), 'engine': res.get('engine') or spec.get('engine'),
                    'status': status, 'cwd': spec.get('cwd'), 'started_at': started,
                    'session_id': res.get('session_id') or st.get('session_id'), 'age': fmt_age(common.age_s(started))})
    return out


def list_all_jobs(limit: int = 50) -> list[dict]:
    try:
        from twinlane import jobs  # written in parallel; may not exist yet
        return list(jobs.list_jobs(limit=limit))
    except Exception:
        return _scan_jobs_dir(limit)


def live_jobs_for(cwd: str | None) -> list[dict]:
    return [j for j in list_all_jobs() if j.get('status') in LIVE_JOB_STATUSES
            and (cwd is None or under_dir(norm_dir(j.get('cwd')), cwd))]


def _merge_live_jobs(baton: dict, live: list[dict]) -> None:
    by_id = {j.get('id'): j for j in baton['jobs'] if isinstance(j, dict)}
    for j in live:
        entry = by_id.setdefault(j['id'], {'id': j['id']})
        entry.update({'engine': j.get('engine'), 'status': j.get('status'),
                      'session_id': j.get('session_id') or entry.get('session_id')})
    baton['jobs'] = list(by_id.values())
    flights = {f.get('job'): f for f in baton['progress']['in_flight'] if isinstance(f, dict)}
    for j in live:
        flights.setdefault(j['id'], {'what': j.get('name') or f"{j.get('engine')} job", 'job': j['id'],
                                     'check': f"twinlane result {j['id']}", 'never_rerun': True})
    baton['progress']['in_flight'] = list(flights.values())


def _actual_job_status(jid) -> str | None:
    """The job's real status from its receipt and runner state; None when the job is not on this host."""
    if not isinstance(jid, str) or not os.path.isdir(os.path.join(common.JOBS, jid)):
        return None
    try:
        from twinlane import jobs
        return jobs.job_status(jid)
    except Exception:
        res = common.read_json(os.path.join(common.JOBS, jid, 'result.json'))
        return res.get('status') if isinstance(res, dict) and isinstance(res.get('status'), str) else None


def reconcile_jobs(baton: dict) -> bool:
    """Bring recorded job states in line with the job folders: a job saved as live that has since
    finished, been stopped or lost its runner takes its real status, and in-flight items for jobs
    that are no longer live are dropped. Jobs not found on this host are left as recorded."""
    changed = False
    status = {}
    for entry in baton['jobs']:
        if isinstance(entry, dict):
            status[entry.get('id')] = entry.get('status')
            if entry.get('status') in LIVE_JOB_STATUSES:
                actual = _actual_job_status(entry.get('id'))
                if actual and actual != entry['status']:
                    entry['status'] = status[entry['id']] = actual
                    changed = True
    flights = baton['progress']['in_flight']
    kept = []
    for f in flights:
        jid = f.get('job') if isinstance(f, dict) else None
        current = status[jid] if jid in status else _actual_job_status(jid) if jid else None
        if current is not None and current not in LIVE_JOB_STATUSES:
            changed = True
            continue
        kept.append(f)
    flights[:] = kept
    return changed


def _refresh_mechanical(baton: dict, cwd: str | None, st: dict | None = None) -> dict:
    cwd = norm_dir(cwd) or baton['repo'].get('worktree') or baton['repo'].get('root') or norm_dir(os.getcwd())
    baton['repo'] = repo_fields(cwd, baton.get('repo'), st)
    reconcile_jobs(baton)
    _merge_live_jobs(baton, live_jobs_for(baton['repo'].get('worktree') or cwd))
    baton['meter_at_write'] = cached_meter()
    return baton


def snap(slug: str, cwd: str | None = None, reason: str = 'checkpoint') -> dict:
    """Refresh repo/git state, live jobs and the cached meter. Creates a skeleton if the baton is new."""
    with baton_lock(require_slug(slug)):
        baton = load_for_update(slug) or empty_baton(slug)
        _refresh_mechanical(baton, cwd)
        return save(baton, reason)


# ---------------------------------------------------------------- new / set / record_job

ACCEPT_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9_]{0,9})\s*[:)]\s*(.+)$', re.S)
ACCEPT_STATUS_RE = re.compile(r'^\s*([A-Za-z][A-Za-z0-9_]{0,9})\s*=\s*([a-z_/]+)\s*(?::(.*))?$', re.S)


def parse_accept(items: list[str]) -> list[dict]:
    out = []
    for i, raw in enumerate(items or [], 1):
        m = ACCEPT_RE.match(raw)
        aid, crit = (m.group(1), m.group(2).strip()) if m else (f'A{i}', raw.strip())
        out.append({'id': aid, 'criterion': crit, 'status': 'not_run', 'evidence': None})
    return out


def _goal_lines(goal: str | None) -> str | None:
    if goal is None:
        return None
    return '\n'.join(goal.strip().splitlines()[:3])


def new(slug: str, goal: str, tracker: str | None = None, title: str | None = None, cwd: str | None = None,
        accept: list[str] | None = None) -> dict:
    require_slug(slug)
    with baton_lock(slug):
        if os.path.exists(baton_path(slug)):
            raise BatonError(f'baton {slug} exists; use `twinlane baton set {slug} ...`')
        baton = empty_baton(slug)
        baton['task'].update({'goal': _goal_lines(goal), 'tracker_id': tracker, 'title': title or slug,
                              'acceptance': parse_accept(accept)})
        if cwd:
            _refresh_mechanical(baton, cwd)
        return save(baton, 'user', strict=True)


def _set_accept_status(baton: dict, spec: str) -> None:
    m = ACCEPT_STATUS_RE.match(spec)
    if not m or m.group(2) not in ACCEPT_STATUSES:
        raise BatonError(f'--accept-status wants ID=STATUS[:evidence] with STATUS in {ACCEPT_STATUSES}')
    for a in baton['task']['acceptance']:
        if a.get('id') == m.group(1):
            a['status'] = m.group(2)
            if m.group(3):
                a['evidence'] = m.group(3).strip()
            return
    raise BatonError(f'no acceptance criterion {m.group(1)}')


def _parse_evidence(spec: str) -> dict:
    parts = [p.strip() for p in spec.split('|', 2)]
    if len(parts) != 3 or parts[0] not in EVIDENCE_KINDS:
        raise BatonError(f'--evidence wants kind|path|result with kind in {EVIDENCE_KINDS}')
    return {'kind': parts[0], 'path': parts[1], 'result': parts[2]}


def _parse_grant(spec: str) -> dict:
    if '|' not in spec:
        raise BatonError('--grant wants "verbatim quote|scope"')
    quote, scope = spec.rsplit('|', 1)
    return {'quote': quote.strip(), 'scope': scope.strip(), 'at': common.iso(common.now())}


def _parse_hold(spec: str) -> dict:
    action, _, ask = spec.partition('|')
    return {'action': action.strip(), 'ask_text': (ask or action).strip()}


def _hold_ids(holds: dict) -> set:
    return {h['id'] for key in ('approvals_pending', 'approvals_granted', 'approvals_rejected')
            for h in holds.get(key) or [] if isinstance(h, dict) and h.get('id')}


def _next_hold_id(holds: dict) -> str:
    used = [int(i[1:]) for i in _hold_ids(holds) if isinstance(i, str) and re.fullmatch(r'H\d+', i)]
    return f'H{max(used, default=0) + 1}'


def _hold_label(h) -> str:
    h = h if isinstance(h, dict) else {}
    return f"{'[' + h['id'] + '] ' if h.get('id') else ''}{h.get('action')}: ask \"{h.get('ask_text')}\""


def resolve_hold(holds: dict, selector: str, grant: str | None, deny: str | None) -> dict:
    """Move one pending approval to granted (with the user's quote, its scope and the time) or to
    rejected. SELECTOR is an id (H1) or a case-insensitive prefix of the action or ask text; no match
    or more than one match changes nothing and lists the candidates."""
    if (grant is None) == (deny is None):
        raise BatonError('--resolve needs exactly one of --grant "<user quote>" or --deny ["<reason>"]')
    if grant is not None and not grant.strip():
        raise BatonError('--grant must quote what the user said; an empty quote is not an approval')
    pending = [h for h in holds['approvals_pending'] if isinstance(h, dict)]
    sel = (selector or '').strip()
    hits = [h for h in pending if sel and h.get('id') == sel] or [
        h for h in pending if sel and any(str(h.get(k) or '').lower().startswith(sel.lower())
                                          for k in ('action', 'ask_text'))]
    if len(hits) != 1:
        shown = '; '.join(_hold_label(h) for h in (hits or pending)[:6]) or 'none pending'
        raise BatonError(f"--resolve {selector!r} matches {'no pending approval' if not hits else 'several'}: "
                         f"{'candidates' if hits else 'pending are'} {shown}")
    hold = hits[0]
    if not hold.get('id'):
        hold['id'] = _next_hold_id(holds)
    holds['approvals_pending'] = [h for h in holds['approvals_pending'] if h is not hold]
    now = common.iso(common.now())
    if grant is not None:
        holds['approvals_granted'].append({'id': hold['id'], 'action': hold.get('action'), 'quote': grant.strip(),
                                           'scope': hold.get('action'), 'at': now})
    else:
        holds.setdefault('approvals_rejected', []).append(
            {'id': hold['id'], 'action': hold.get('action'), 'reason': (deny or '').strip() or None, 'at': now})
    return hold


def _append_unique(lst: list, items) -> None:
    for it in items or []:
        if it not in lst:
            lst.append(it)


def apply_set(baton: dict, a) -> dict:
    """Merge `twinlane baton set` arguments (an argparse Namespace or similar) into the baton."""
    prog, task, holds = baton['progress'], baton['task'], baton['holds']
    now = common.iso(common.now())
    if a.next is not None:
        prog['next_action'] = a.next
    _append_unique(prog['done'], a.done)
    _append_unique(prog['then'], a.then)
    if a.goal is not None:
        task['goal'] = _goal_lines(a.goal)
    if a.title is not None:
        task['title'] = a.title
    if a.tracker is not None:
        task['tracker_id'] = a.tracker
    task['acceptance'].extend(x for x in parse_accept(a.accept)
                              if x['id'] not in {y.get('id') for y in task['acceptance']})
    for spec in a.accept_status or []:
        _set_accept_status(baton, spec)
    if a.decision or a.user_quote:
        by_user = bool(a.user_quote)
        baton['decisions'].append({'what': a.decision or common.clip(a.user_quote, BATON_FIELD_MAX_CHARS), 'why': a.why or '',
                                   'by': 'user' if by_user else 'agent', 'quote': a.user_quote, 'at': now})
    _append_unique(baton['rejected'], a.reject)
    resolve, deny = getattr(a, 'resolve', None), getattr(a, 'deny', None)
    if resolve is not None:
        if len(a.grant or []) > 1:
            raise BatonError('--resolve takes one --grant quote')
        resolve_hold(holds, resolve, (a.grant or [None])[0], deny)  # before --hold: only existing items resolve
    elif deny is not None:
        raise BatonError('--deny needs --resolve <id|text-prefix>')
    for spec in a.hold or []:
        hold = _parse_hold(spec)
        hold['id'] = _next_hold_id(holds)
        holds['approvals_pending'].append(hold)
    if resolve is None:
        holds['approvals_granted'].extend(_parse_grant(g) for g in a.grant or [])
    _append_unique(holds['prohibitions'], a.prohibit)
    baton['evidence'].extend(_parse_evidence(e) for e in a.evidence or [])
    for p in a.read_first or []:
        if p in baton['read_first']:
            baton['read_first'].remove(p)
        baton['read_first'].append(p)
    _append_unique(baton['do_not'], a.do_not)
    _append_unique(baton['verify_before_act'], a.verify)
    if a.owner is not None:
        baton['owner']['engine'] = a.owner
    return baton


def replace_from_file(slug: str, path: str) -> dict:
    data = common.read_json(path)
    errs = validate(data)
    if errs:
        raise BatonError('invalid baton file: ' + '; '.join(errs))
    if data['slug'] != slug:
        raise BatonError(f'file slug {data["slug"]!r} does not match {slug!r}')
    with baton_lock(slug):
        return save(_merge_defaults(data), data.get('reason') or 'user', strict=True)


def _receipt_id(receipt: dict) -> str | None:
    return receipt.get('id') or receipt.get('job_id') or receipt.get('job')


def record_job(slug: str, receipt: dict) -> None:
    """Upsert one job into the baton; a running job makes its engine the owner."""
    jid = _receipt_id(receipt or {})
    if not jid:
        return
    with baton_lock(require_slug(slug)):
        _record_job_locked(slug, jid, receipt)


def _record_job_locked(slug: str, jid: str, receipt: dict) -> None:
    baton = load_for_update(slug) or empty_baton(slug)
    status = receipt.get('status') or 'running'
    served = receipt.get('served_models') or []
    model = served[0] if served else (receipt.get('requested') or {}).get('model') or receipt.get('model')
    reconcile_jobs(baton)
    entry = next((j for j in baton['jobs'] if isinstance(j, dict) and j.get('id') == jid), None)
    if entry is None:
        entry = {'id': jid}
        baton['jobs'].append(entry)
    entry.update({'engine': receipt.get('engine') or entry.get('engine'), 'status': status,
                  'session_id': receipt.get('session_id') or entry.get('session_id')})
    flights = baton['progress']['in_flight']
    flights[:] = [f for f in flights if not (isinstance(f, dict) and f.get('job') == jid)]
    if status in LIVE_JOB_STATUSES:
        baton['owner'] = {'engine': entry['engine'], 'session_id': entry.get('session_id'), 'model': model}
        flights.append({'what': receipt.get('name') or f"{entry['engine']} job", 'job': jid,
                        'check': f'twinlane result {jid}', 'never_rerun': True})
    save(baton, 'job')


# ---------------------------------------------------------------- list / show

def list_batons() -> list[dict]:
    out = []
    for path in glob.glob(os.path.join(common.BATONS, '*.json')):
        b = common.read_json(path)
        if not isinstance(b, dict) or not valid_slug(b.get('slug')):
            continue
        # A hand-edited or damaged baton can hold any JSON type in any field: take only what has the right type.
        sub = lambda key: b.get(key) if isinstance(b.get(key), dict) else {}  # noqa: E731
        owner = sub('owner').get('engine')
        updated = b.get('updated_at') if isinstance(b.get('updated_at'), str) else None
        out.append({'slug': b['slug'], 'owner': owner if owner in ENGINES else None,
                    'age': fmt_age(common.age_s(updated)), 'updated_at': updated,
                    'reason': b.get('reason') if isinstance(b.get('reason'), str) else None,
                    'next_action': sub('progress').get('next_action'),
                    'worktree': sub('repo').get('worktree'), '_mtime': _mtime_ns(path)})
    out.sort(key=lambda r: (r.get('updated_at') or '', r.pop('_mtime')), reverse=True)
    return out


# ---------------------------------------------------------------- black box: Claude transcripts

def claude_dir_name(cwd: str) -> str:
    """Map a worktree path to Claude Code's project directory name."""
    return re.sub(r'[^A-Za-z0-9]', '-', cwd)


def claude_project_dir(cwd: str) -> str | None:
    name = claude_dir_name(cwd)
    exact = os.path.join(CLAUDE_PROJECTS, name)
    if os.path.isdir(exact):
        return exact
    try:
        dirs = os.listdir(CLAUDE_PROJECTS)
    except OSError:
        return None
    low = name.lower()
    for d in dirs:
        if d.lower() == low:
            return os.path.join(CLAUDE_PROJECTS, d)
    if len(name) > CLAUDE_DIR_NAME_MAX:
        prefix = low[:CLAUDE_DIR_NAME_MAX]
        hits = [d for d in dirs if d.lower().startswith(prefix)]
        if hits:
            return os.path.join(CLAUDE_PROJECTS, max(hits, key=lambda d: os.path.getmtime(
                os.path.join(CLAUDE_PROJECTS, d))))
    return None


def newest_file(paths: list[str]) -> str | None:
    best, best_t = None, -1.0
    for p in paths:
        try:
            t = os.path.getmtime(p)
        except OSError:
            continue
        if t > best_t:
            best, best_t = p, t
    return best


def claude_tail_meta(path: str) -> tuple[str | None, str | None]:
    """(entrypoint, last cwd) from the transcript's tail."""
    entry = cwd = None
    for ln in reversed(common.tail_lines(path, CLAUDE_META_TAIL)):
        if '"cwd"' not in ln and '"entrypoint"' not in ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if isinstance(obj, dict):
            entry, cwd = entry or obj.get('entrypoint'), cwd or obj.get('cwd')
        if entry and cwd:
            break
    return entry, cwd


def claude_transcripts_for(cwd: str) -> list[str]:
    """Interactive transcripts for cwd, newest first. Skipped: `claude -p` workers (sdk-cli: twinlane jobs,
    dispatched reviewers) and sessions whose last cwd moved elsewhere (EnterWorktree)."""
    d = claude_project_dir(cwd)
    if not d:
        return []
    paths = sorted(glob.glob(os.path.join(d, '*.jsonl')), key=_mtime_ns, reverse=True)[:CLAUDE_SCAN_MAX]
    out = []
    for p in paths:
        entry, last_cwd = claude_tail_meta(p)
        if entry in WORKER_ENTRYPOINTS or (last_cwd and not same_dir(norm_dir(last_cwd), cwd)):
            continue
        out.append(p)
    return out


def newest_claude_transcript(cwd: str) -> str | None:
    hits = claude_transcripts_for(cwd)
    return hits[0] if hits else None


def _session_ref(path: str) -> dict:
    return {'session_id': os.path.splitext(os.path.basename(path))[0], 'modified': _mtime_iso(path)}


def _recent_others(paths: list[str], chosen: str) -> list[dict]:
    """Other sessions written within OTHER_SESSION_WINDOW_S of the chosen one: parallel sessions
    the reader should know about (the brief reads only one)."""
    t0 = _mtime_ns(chosen) / 1e9
    return [_session_ref(p) for p in paths if p != chosen and t0 - _mtime_ns(p) / 1e9 <= OTHER_SESSION_WINDOW_S]


def find_claude_session(session_id: str) -> str | None:
    if not re.match(r'^[A-Za-z0-9-]{6,80}$', session_id or ''):
        return None
    return newest_file(glob.glob(os.path.join(CLAUDE_PROJECTS, '*', session_id + '.jsonl')))


def _claude_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and b.get('type') == 'text')
    return ''


def _clean_user_text(text: str) -> str:
    return re.sub(r'<system-reminder>.*?</system-reminder>', '', text, flags=re.S).strip()


def _is_real_claude_user(obj: dict) -> bool:
    if obj.get('isMeta') or obj.get('isSidechain'):
        return False
    origin = obj.get('origin')
    if isinstance(origin, dict) and origin.get('kind') not in (None, 'human'):
        return False
    return True


def parse_claude_lines(lines: list[str]) -> dict:
    """Last 3 assistant texts and the last real user message from transcript lines."""
    info = {'assistant': [], 'user': None, 'session_id': None, 'cwd': None, 'model': None, 'branch': None,
            'last_at': None}
    for ln in lines:
        if '"type"' not in ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        kind = obj.get('type')
        if kind not in ('user', 'assistant') or obj.get('isSidechain'):
            continue
        info['session_id'] = obj.get('sessionId') or info['session_id']
        info['cwd'] = obj.get('cwd') or info['cwd']
        info['branch'] = obj.get('gitBranch') or info['branch']
        info['last_at'] = obj.get('timestamp') or info['last_at']
        msg = obj.get('message') or {}
        if kind == 'assistant':
            text = _claude_text(msg.get('content')).strip()
            if msg.get('model') and msg.get('model') != '<synthetic>':
                info['model'] = msg['model']
            if text:
                info['assistant'] = (info['assistant'] + [text])[-3:]
        elif _is_real_claude_user(obj):
            text = _clean_user_text(_claude_text(msg.get('content')))
            if text and not text.startswith(USER_NOISE_PREFIXES):
                info['user'] = text
    return info


def claude_blackbox(cwd: str | None, session_id: str | None = None) -> dict | None:
    path = find_claude_session(session_id) if session_id else None
    candidates = []
    if not path and cwd:
        candidates = claude_transcripts_for(cwd)
        path = candidates[0] if candidates else None
    if not path:
        return None
    info = parse_claude_lines(common.tail_lines(path, TAIL_BYTES))
    info.update(engine='claude', path=path, modified=_mtime_iso(path), others=_recent_others(candidates, path))
    info['session_id'] = info['session_id'] or os.path.splitext(os.path.basename(path))[0]
    return info


def _mtime_ns(path: str) -> int:
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return 0


def _mtime_iso(path: str) -> str | None:
    try:
        return common.iso(common.parse_time(os.path.getmtime(path)))
    except OSError:
        return None


# ---------------------------------------------------------------- black box: Codex rollouts

CWD_RX = re.compile(r'"cwd"\s*:\s*"((?:[^"\\]|\\.)*)"')


def rollout_meta(path: str) -> dict:
    """session_meta payload from the first line; regex fallback when the line exceeds the read cap."""
    line = common.first_line(path)
    try:
        obj = json.loads(line)
        if obj.get('type') == 'session_meta':
            return obj.get('payload') or {}
    except ValueError:
        pass
    m = CWD_RX.search(line)
    if not m:
        return {}
    try:
        return {'cwd': json.loads('"' + m.group(1) + '"')}
    except ValueError:
        return {}


def recent_rollouts(days: int = CODEX_LOOKBACK_DAYS) -> list[str]:
    cutoff = common.now().timestamp() - days * 86400
    found = []
    for p in glob.glob(os.path.join(CODEX_SESSIONS, '**', 'rollout-*.jsonl'), recursive=True):
        try:
            t = os.path.getmtime(p)
        except OSError:
            continue
        if t >= cutoff:
            found.append((t, p))
    return [p for _, p in sorted(found, reverse=True)]


def _is_subagent(meta: dict) -> bool:
    src = meta.get('source')
    return isinstance(src, dict) and 'subagent' in src


def _is_exec(meta: dict) -> bool:
    return meta.get('originator') == 'codex_exec' or meta.get('source') == 'exec'


def pick_codex_rollout(cwd: str) -> tuple[str | None, dict | None]:
    """(path, parsed tail) of the newest interactive rollout for cwd that has content of its own.

    Skipped: subagent threads, rollouts that are only an import of another tool's history
    (external-import turns), and - unless nothing else exists - `codex exec` workers."""
    matching = [p for p in recent_rollouts() if same_dir(norm_dir(rollout_meta(p).get('cwd')), cwd)]
    fallback = (None, None)
    for p in matching[:CODEX_SCAN_MAX]:
        meta = rollout_meta(p)
        if _is_subagent(meta):
            continue
        info = parse_codex_lines(common.tail_lines(p, TAIL_BYTES))
        if info['external_turns'] and not info['native_turns']:
            continue
        if not (info['user'] or info['assistant']):
            continue
        if _is_exec(meta):
            fallback = fallback if fallback[0] else (p, info)
            continue
        return p, info
    return fallback


def newest_codex_rollout(cwd: str) -> str | None:
    return pick_codex_rollout(cwd)[0]


def find_codex_session(session_id: str) -> str | None:
    if not re.match(r'^[A-Za-z0-9-]{6,80}$', session_id or ''):
        return None
    return newest_file(glob.glob(os.path.join(CODEX_SESSIONS, '**', f'rollout-*{session_id}*.jsonl'),
                                 recursive=True))


def _is_external_turn(turn_id) -> bool:
    return isinstance(turn_id, str) and turn_id.startswith('external-import-turn')


def _codex_response_text(payload: dict) -> str:
    return '\n'.join(c.get('text', '') for c in payload.get('content') or []
                     if isinstance(c, dict) and c.get('type') in ('output_text', 'text')).strip()


USER_TURN_LINE = re.compile(r'"role"\s*:\s*"user"|"user_message"|"task_started"|"task_complete"')


def _codex_user_text(payload: dict) -> str:
    return '\n'.join(c.get('text', '') for c in payload.get('content') or []
                     if isinstance(c, dict) and c.get('type') in ('input_text', 'text')).strip()


def _take_user(info: dict, text) -> None:
    text = (text or '').strip() if isinstance(text, str) else ''
    if text and not text.startswith(USER_NOISE_PREFIXES):
        info['user'] = text


def parse_codex_lines(lines: list[str]) -> dict:
    """Last 3 agent messages and the last real user message; skips external-import turns.

    Current rollouts carry the user's words only as response_item role=user (no user_message
    event); older ones carry both. Instruction and context wrappers are skipped."""
    info = {'assistant': [], 'user': None, 'last_at': None, 'native_turns': 0, 'external_turns': 0}
    fallback = []  # response_item assistant messages, used only if no agent_message events exist
    turn = None
    for ln in lines:
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        p = obj.get('payload') if isinstance(obj.get('payload'), dict) else {}
        ptype = p.get('type')
        if p.get('turn_id'):
            turn = p['turn_id']
        if ptype == 'task_started':
            info['external_turns' if _is_external_turn(turn) else 'native_turns'] += 1
        info['last_at'] = obj.get('timestamp') or info['last_at']
        external = _is_external_turn(turn)
        if obj.get('type') == 'event_msg' and not external:
            if ptype == 'agent_message':
                _push_agent(info['assistant'], p.get('message'))
            elif ptype == 'task_complete':
                _push_agent(info['assistant'], p.get('last_agent_message'))
            elif ptype == 'user_message':
                _take_user(info, p.get('message'))
        elif obj.get('type') == 'response_item' and ptype == 'message' and not external:
            if p.get('role') == 'assistant':
                _push_agent(fallback, _codex_response_text(p))
            elif p.get('role') == 'user':
                _take_user(info, _codex_user_text(p))
        if ptype == 'task_complete':
            turn = None
    if not info['assistant']:
        info['assistant'] = fallback[-3:]
    return info


def codex_last_user_wide(path: str) -> str | None:
    """The last real user message from a wider tail, for a long turn that filled the 2 MB tail.
    Only user/turn lines are parsed, so the wider read stays cheap."""
    size = os.path.getsize(path) if os.path.exists(path) else 0
    for nbytes in CODEX_BACKSCAN_BYTES:
        if size <= TAIL_BYTES:
            return None
        lines = [ln for ln in common.tail_lines(path, nbytes) if USER_TURN_LINE.search(ln[:400])]
        user = parse_codex_lines(lines)['user']
        if user or size <= nbytes:
            return user
    return None


def _push_agent(lst: list, text) -> None:
    text = (text or '').strip() if isinstance(text, str) else ''
    if not text or text.startswith(AGENT_NOISE_PREFIXES) or (lst and lst[-1] == text):
        return
    lst.append(text)
    del lst[:-3]


def codex_blackbox(cwd: str | None, session_id: str | None = None) -> dict | None:
    path = find_codex_session(session_id) if session_id else None
    info = None
    if not path and cwd:
        path, info = pick_codex_rollout(cwd)
    if not path:
        return None
    meta = rollout_meta(path)
    info = info or parse_codex_lines(common.tail_lines(path, TAIL_BYTES))
    if not info['user']:
        info['user'] = codex_last_user_wide(path)
    info.update(engine='codex', path=path, modified=_mtime_iso(path), cwd=meta.get('cwd'),
                session_id=meta.get('id') or meta.get('session_id'), model=None,
                originator=meta.get('originator'))
    return info


def session_cwd(session_id: str) -> str | None:
    c = find_claude_session(session_id)
    if c:
        cwd = parse_claude_lines(common.tail_lines(c, TAIL_BYTES)).get('cwd')
        if cwd:
            return norm_dir(cwd)
    x = find_codex_session(session_id)
    return norm_dir(rollout_meta(x).get('cwd')) if x else None


# ---------------------------------------------------------------- handoff rendering

def _quote(text: str | None, limit: int) -> str:
    if not text:
        return '> (none in the tail read)'
    body = common.clip(redact(text.strip()), limit)
    return '\n'.join('> ' + ln if ln.strip() else '>' for ln in body.splitlines())


def _bullets(items, limit: int = 8, width: int = 220, newest: bool = True) -> list[str]:
    """Bullet lines; newest=True keeps the tail of a chronological list, False keeps the head."""
    items = [i for i in (items or []) if i]
    kept = items[-limit:] if newest else items[:limit]
    lines = [f'- {common.clip(redact(str(i)), width)}' for i in kept]
    if len(items) > limit:
        note = f'- ... ({len(items) - limit} {"earlier" if newest else "more"} omitted)'
        if newest:
            lines.insert(0, note)
        else:
            lines.append(note)
    return lines


def render_baton_section(b: dict) -> list[str]:
    task, prog, holds = b['task'], b['progress'], b['holds']
    owner = b.get('owner') or {}
    out = [f"## Task (baton `{b['slug']}`, updated {b.get('updated_at') or '?'}, reason {b.get('reason')}, "
           f"owner {owner.get('engine') or '-'})"]
    if task.get('title'):
        out.append(f"- Title: {task['title']}" + (f" (tracker {task['tracker_id']})" if task.get('tracker_id') else ''))
    if task.get('goal'):
        out.append('- Goal: ' + task['goal'].replace('\n', ' / '))
    out.append(f"- **Next action:** {prog.get('next_action') or '(not set: decide from the evidence below)'}")
    if prog.get('then'):
        out += ['- Then:'] + ['  ' + x for x in _bullets(prog['then'], 5)]
    if task.get('acceptance'):
        out.append('- Acceptance:')
        out += [f"  - {a.get('id')} [{a.get('status')}] {common.clip(a.get('criterion'), 160)}"
                + (f" (evidence: {common.clip(a.get('evidence'), 100)})" if a.get('evidence') else '')
                for a in task['acceptance'][:10]]
    sections = [('Done', prog.get('done'), 8),
                ('In flight (never rerun; check first)',
                 [f"{f.get('what')}: `{f.get('check')}`" for f in prog.get('in_flight') or []], 5),
                ('Decisions', [_fmt_decision(d) for d in b.get('decisions') or []], 6),
                ('Rejected', b.get('rejected'), 5),
                ('Approvals pending', [_hold_label(h) for h in holds.get('approvals_pending') or []], 5),
                ('Prohibitions', holds.get('prohibitions'), 6),
                ('Do not', b.get('do_not'), 6),
                ('Evidence', [f"{e.get('kind')}: {e.get('path')} -> {e.get('result')}"
                              for e in b.get('evidence') or []], 6),
                ('Read first', b.get('read_first'), 5)]
    for title, items, limit in sections:
        if items:
            out += [f'- {title}:'] + ['  ' + x for x in _bullets(items, limit)]
    return out


def _fmt_decision(d) -> str:
    if not isinstance(d, dict):
        return str(d)
    s = f"{d.get('what')} (by {d.get('by')})"
    if d.get('quote'):
        s += f' user said: "{d["quote"]}"'
    return s


def render_repo_section(st: dict, cwd: str | None, title: str = 'Repo state (fresh)') -> list[str]:
    out = [f'## {title}', f'- cwd: `{cwd or "?"}`']
    if not st:
        return out + ['- not a git repo (or git unavailable)']
    out += [f"- branch `{st.get('branch')}` head `{st.get('head')}` upstream `{st.get('upstream') or 'none'}`"
            + (' (linked worktree)' if st.get('linked_worktree') else '')]
    dirty = st.get('dirty') or []
    out.append(f'- dirty files: {len(dirty)}' + (' (peer WIP? never stage/revert what you did not write)'
                                                   if dirty else ''))
    out += ['  ' + x for x in _bullets(dirty, 15, 160, newest=False)]
    if st.get('recent'):
        out += ['- recent commits:'] + ['  ' + x for x in _bullets(st['recent'], 5, 160)]
    return out


def render_jobs_section(jobs: list[dict]) -> list[str]:
    if not jobs:
        return ['## Live twinlane jobs for this cwd', '- none']
    return ['## Live twinlane jobs for this cwd'] + [
        f"- `{j.get('id')}` {j.get('engine')} {j.get('status')} {j.get('name') or ''}: check `twinlane result "
        f"{j.get('id')}`; never rerun" for j in jobs[:8]]


def render_blackbox(bb: dict | None, label: str, limit: int) -> list[str]:
    if not bb:
        return [f'## Last {label} session', '- none found for this cwd']
    out = [f'## Last {label} session',
           f"- session `{bb.get('session_id')}` modified {bb.get('modified')}"
           + (f" model {bb['model']}" if bb.get('model') else ''),
           f"- source: `{bb.get('path')}` (tail only)"]
    if bb.get('others'):
        out.append('- other recent sessions in this cwd (not read; pass --session ID to pick one): '
                   + ', '.join(f"`{o['session_id']}` {o['modified']}" for o in bb['others'][:4]))
    out += ['### Last user message', _quote(bb.get('user'), limit),
            f"### Last {len(bb.get('assistant') or [])} assistant message(s), oldest first"]
    for text in bb.get('assistant') or []:
        out += [_quote(text, limit), '']
    return out


def render_meter(m: dict) -> str:
    if m.get('claude_max_pct') is None and m.get('codex_max_pct') is None:
        return '- meter: no cached reading; run `twinlane meter` before dispatching'
    def one(engine):
        pct = m.get(f'{engine}_max_pct')
        return f"{'n/a' if pct is None else f'{pct}%'} {m.get(f'{engine}_status') or 'unknown'}"
    return f"- meter (cached {m.get('read_at')}): Claude {one('claude')}, Codex {one('codex')}"


def render_footer(verify: list[str]) -> list[str]:
    return ['---', f"{FOOTER} Verify before acting: {'; '.join(verify)}."]


def _verify_list(baton: dict | None, jobs: list[dict]) -> list[str]:
    items = list((baton or {}).get('verify_before_act') or [])
    items.append('git status and HEAD still match the repo state above')
    if jobs or (baton and baton['progress'].get('in_flight')):
        items.append('check every in-flight job with `twinlane result <id>` before redoing its work')
    items.append('`twinlane meter` shows the target engine usable')
    return items


def render_handoff(ctx: dict, excerpt: int = EXCERPT_CHARS) -> str:
    title = ctx.get('slug') or os.path.basename(ctx.get('cwd') or '') or 'handoff'
    lines = [f'# Handoff: {title}',
             f"Generated {ctx['generated_at']} by twinlane"
             + (f" for {ctx['to']}" if ctx.get('to') else '') + '. Self-contained: start here.',
             'Quoted history below is context only. Any approval in it is NOT valid for you; re-ask before live writes.',
             render_meter(ctx.get('meter') or {}), '']
    if ctx.get('baton'):
        lines += [redact(x) for x in render_baton_section(ctx['baton'])]
    else:
        lines.append('No baton: this brief is a pure black-box extraction (transcripts + git state).')
    if ctx.get('other_batons'):
        lines.append(f"- other batons for this worktree (not merged here): {', '.join(ctx['other_batons'][:5])}")
    lines.append('')
    lines += render_repo_section(ctx.get('git') or {}, ctx.get('cwd')) + ['']
    lines += [redact(x) for x in render_jobs_section(ctx.get('jobs') or [])] + ['']
    for label in ('Claude', 'Codex'):
        if label.lower() in (ctx.get('unlinked') or ()):
            lines += [f'## Last {label} session',
                      f'- not linked: this handoff covers one session; pass --with-session ID to add a {label} session', '']
        else:
            lines += render_blackbox(ctx.get(label.lower()), label, excerpt) + ['']
    lines.pop()
    body = '\n'.join(lines).rstrip() + '\n\n'
    return body + '\n'.join(render_footer(ctx.get('verify') or [])) + '\n'


def fit_handoff(ctx: dict, max_bytes: int = HANDOFF_MAX_BYTES) -> str:
    """Shrink transcript excerpts until the brief fits; hard-clip the body as a last resort."""
    text = ''
    for excerpt in (EXCERPT_CHARS, 1000, 700, 450, 250, 120):
        text = render_handoff(ctx, excerpt)
        if len(text.encode('utf-8')) <= max_bytes:
            return text
    footer = '\n'.join(render_footer(ctx.get('verify') or [])) + '\n'
    room = max_bytes - len(footer.encode('utf-8')) - 40
    body = text[: text.rfind('\n---\n')].encode('utf-8')[:max(room, 0)].decode('utf-8', errors='ignore')
    return body.rstrip() + '\n\n(... clipped to fit 8 KB)\n\n' + footer


# ---------------------------------------------------------------- handoff orchestration

def derive_slug(cwd: str) -> str:
    base = re.sub(r'[^a-z0-9-]+', '-', os.path.basename(cwd.rstrip('\\/')).lower()).strip('-')[:60] or 'cwd'
    existing = load(base)
    if (existing is None and not os.path.exists(baton_path(base))) or             (existing and same_dir(existing['repo'].get('worktree'), cwd)):
        return base
    return f"{base[:51]}-{hashlib.sha1(os.path.normcase(cwd).encode('utf-8')).hexdigest()[:8]}"


HANDOFF_KINDS = ('intel', 'code')


def worker_is_write(linked: bool, kind: str | None) -> bool:
    """A delegated worker edits only when asked (--kind code); unasked it is read-only intel, even in a
    linked worktree (a handoff is a takeover, and a takeover belongs to an interactive lead)."""
    return kind == 'code'


def interactive_line(to: str, brief: str, cwd: str) -> str:
    return (f'Interactive (lead): open {to} and say "Continue from {brief}"  '
            f'[recommended; start it in "{cwd}"]')


def delegated_label(write: bool) -> str:
    if write:
        return ('Delegated worker (code --write: edits files in this linked worktree; never commits, merges or '
                'deploys):')
    return ('Delegated worker (read-only intel: cannot edit, commit or merge, so it cannot take over as lead; '
            'for a write worker add --kind code to `twinlane handoff`):')


def run_command(to: str, brief: str, cwd: str, linked: bool, claude_bb: dict | None, slug: str | None,
                kind: str | None = None, name: str | None = None) -> list[str]:
    """The `twinlane run` line; without a slug (a session handoff has no baton) there is no --baton flag."""
    kind = '--kind code --write' if worker_is_write(linked, kind) else '--kind intel'
    cmd = (f'twinlane run --brief "{brief}" --cd "{cwd}" {kind} --engine {to} --bg '
           + (f'--baton {slug} --name handoff-{slug}' if slug else f'--name handoff-{name}'))
    out = [cmd]
    path = (claude_bb or {}).get('path')
    session_mtime = _mtime_ns(path) if path else 0
    if to == 'claude' and claude_bb and claude_bb.get('session_id') and session_mtime and \
            (common.now().timestamp() - session_mtime / 1e9) >= RESUME_IDLE_S:
        out.append(f"# or fork the idle Claude session instead (run ONE of the two): {cmd} --resume {claude_bb['session_id']}")
    return out


def batons_for_dir(cwd: str) -> list[str]:
    """Slugs of batons whose recorded worktree is cwd, newest first."""
    return [r['slug'] for r in list_batons() if r.get('worktree') and same_dir(r['worktree'], cwd)]


def _resolve_target(slug, cwd, session) -> tuple[str, str, list[str]]:
    """(slug, cwd, other batons for that cwd). Without a slug, a baton already recording this
    worktree wins over the derived name, so `handoff --cd` reuses it instead of starting empty."""
    baton = load_for_update(require_slug(slug)) if slug else None
    if slug and baton is None and not cwd and not session:
        raise BatonError(f'no baton {slug}; pass --cd DIR (or --session ID) for a black-box handoff')
    cwd = norm_dir(cwd)
    if not cwd and session:
        cwd = session_cwd(session)
    if not cwd and baton:
        cwd = baton['repo'].get('worktree') or baton['repo'].get('root')
        root = baton['repo'].get('root')
        if cwd and root and os.path.isdir(root) and (
                not os.path.isdir(cwd) or common.leftover_dir_problem(cwd)):
            cwd = norm_dir(root)  # the task worktree was removed (landed): hand off from the main repo
    if not cwd:
        raise BatonError('cannot tell the cwd: pass --cd DIR')
    if not os.path.isdir(cwd):
        raise BatonError(f'directory no longer exists (worktree removed?): {cwd}; no baton or run command written; '
                         'pass --cd <an existing repo or worktree> to hand off from there')
    leftover = common.leftover_dir_problem(cwd)
    if leftover:
        raise BatonError(f'directory {cwd} {leftover}; no baton or run command written; '
                         'pass --cd <an existing repo or worktree> to hand off from there')
    hits = batons_for_dir(cwd)
    if slug:
        return slug, cwd, [h for h in hits if h != slug]
    if hits:
        return hits[0], cwd, hits[1:]
    return derive_slug(cwd), cwd, []


def session_engine(session_id: str) -> tuple[str, str] | None:
    """(engine, transcript or rollout path) for a session id, or None."""
    path = find_claude_session(session_id)
    if path:
        return 'claude', path
    path = find_codex_session(session_id)
    return ('codex', path) if path else None


def _session_blackboxes(session: str, with_session: str | None) -> tuple[dict | None, dict | None, list[str], str]:
    """(claude, codex, unlinked engines, session engine): only the chosen session, plus --with-session if given.
    The cwd is never used to pick a transcript, so a brief cannot carry an unrelated thread."""
    found = session_engine(session)
    if not found:
        raise BatonError(f'no Claude session or Codex rollout with id {session}')
    ids = {found[0]: session}
    if with_session:
        other = session_engine(with_session)
        if not other:
            raise BatonError(f'--with-session: no Claude session or Codex rollout with id {with_session}')
        if other[0] == found[0]:
            raise BatonError(f'--with-session must be a {"codex" if found[0] == "claude" else "claude"} session; '
                             f'{with_session} is a {other[0]} session like {session}')
        ids[other[0]] = with_session
    claude = claude_blackbox(None, ids['claude']) if 'claude' in ids else None
    codex = codex_blackbox(None, ids['codex']) if 'codex' in ids else None
    return claude, codex, [e for e in ENGINES if e not in ids], found[0]


def handoff(slug: str | None = None, cwd: str | None = None, session: str | None = None, to: str | None = None,
            out: str | None = None, kind: str | None = None, with_session: str | None = None) -> dict:
    """A brief for a baton/cwd (SLUG or --cd), or for exactly one session (--session ID without SLUG).

    A session handoff reads only that session (plus `with_session`, an explicit link to one session of the other
    engine) and writes no baton: it touches neither the cwd's task baton nor another session's, and the only file
    it writes is `out`. `kind` picks the printed delegated-worker line (needs `to`)."""
    if kind is not None and kind not in HANDOFF_KINDS:
        raise BatonError(f'--kind must be one of {", ".join(HANDOFF_KINDS)} (got {kind!r})')
    if kind is not None and not to:
        raise BatonError('--kind picks the delegated-worker line, which needs --to claude|codex')
    if session and not slug:
        return _handoff_session(cwd, session, to, out, with_session, kind)
    if with_session and not session:
        raise BatonError('--with-session needs --session ID')
    slug, cwd, others = _resolve_target(slug, cwd, session)
    with baton_lock(slug):
        return _handoff_locked(slug, cwd, others, session, to, out, kind, with_session)


def _handoff_session(cwd, session, to, out, with_session, kind=None) -> dict:
    claude, codex, unlinked, engine = _session_blackboxes(session, with_session)
    cwd = norm_dir(cwd) or session_cwd(session)
    if not cwd:
        raise BatonError(f'cannot tell the cwd of session {session}: pass --cd DIR')
    if not os.path.isdir(cwd):
        raise BatonError(f'directory no longer exists (worktree removed?): {cwd}; no run command written; '
                         'pass --cd <an existing repo or worktree> to hand off from there')
    leftover = common.leftover_dir_problem(cwd)
    if leftover:
        raise BatonError(f'directory {cwd} {leftover}; no run command written; '
                         'pass --cd <an existing repo or worktree> to hand off from there')
    git_now, jobs = common.git_state(cwd), live_jobs_for(cwd)
    linked = bool(git_now.get('linked_worktree'))
    if to and kind == 'code' and not linked:
        raise BatonError(f'--kind code needs a LINKED worktree (git worktree add); {cwd} is not one; '
                         'no run command written')
    label = f'session-{engine}-{session[:8]}'
    ctx = {'slug': label, 'cwd': cwd, 'to': to, 'generated_at': common.iso(common.now()), 'meter': cached_meter(),
           'baton': None, 'git': git_now, 'jobs': jobs, 'claude': claude, 'codex': codex, 'unlinked': unlinked,
           'other_batons': [], 'verify': _verify_list(None, jobs)}
    text = fit_handoff(ctx)
    path = os.path.abspath(out) if out else None
    if path:
        common.write_text(path, text)
    command = run_command(to, path, cwd, linked, claude, None, kind, label) if to and path else []
    write = worker_is_write(linked, kind)
    return {'slug': None, 'cwd': cwd, 'brief': path, 'bytes': len(text.encode('utf-8')), 'markdown': text,
            'command': command, 'interactive': interactive_line(to, path, cwd) if to and path else None,
            'delegated': delegated_label(write) if to and path else None,
            'delegated_write': write if to and path else None,
            'claude_session': (claude or {}).get('session_id'), 'codex_session': (codex or {}).get('session_id')}


def _handoff_locked(slug, cwd, others, session, to, out, kind=None, with_session=None) -> dict:
    baton = load_for_update(slug) or empty_baton(slug)
    git_now = common.git_state(cwd)
    if to and kind == 'code' and not git_now.get('linked_worktree'):
        raise BatonError(f'--kind code needs a LINKED worktree (git worktree add); {cwd} is not one; '
                         'no baton or run command written')
    _refresh_mechanical(baton, cwd, git_now)
    jobs = live_jobs_for(cwd)
    ctx = {'slug': slug, 'cwd': cwd, 'to': to, 'generated_at': common.iso(common.now()),
           'meter': baton['meter_at_write'], 'baton': baton if _has_content(baton) else None,
           'git': git_now, 'jobs': jobs, 'other_batons': others}
    if session:  # SLUG + --session: the baton is the task, the session is the thread; the cwd picks neither
        ctx['claude'], ctx['codex'], ctx['unlinked'], _ = _session_blackboxes(session, with_session)
    else:
        ctx.update(claude=claude_blackbox(cwd, None), codex=codex_blackbox(cwd, None))
    ctx['verify'] = _verify_list(baton, jobs)
    text = fit_handoff(ctx)
    paths = [brief_path(slug)] + ([os.path.abspath(out)] if out else [])
    baton['last_handoff'] = {'at': ctx['generated_at'], 'to': to, 'brief': paths[-1],
                             'claude_transcript': (ctx['claude'] or {}).get('path'),
                             'codex_rollout': (ctx['codex'] or {}).get('path')}
    # The handoff is the escape hatch near a limit: the brief is always written; save() clips and trims.
    for p in paths:
        common.write_text(p, text)
    save(baton, 'handoff')
    linked = bool(ctx['git'].get('linked_worktree'))
    command = run_command(to, paths[-1], cwd, linked, ctx['claude'], slug, kind) if to else []
    write = worker_is_write(linked, kind)
    return {'slug': slug, 'cwd': cwd, 'brief': paths[-1], 'bytes': len(text.encode('utf-8')), 'markdown': text,
            'command': command, 'interactive': interactive_line(to, paths[-1], cwd) if to else None,
            'delegated': delegated_label(write) if to else None, 'delegated_write': write if to else None,
            'claude_session': (ctx['claude'] or {}).get('session_id'),
            'codex_session': (ctx['codex'] or {}).get('session_id')}


def _has_content(b: dict) -> bool:
    t, p = b['task'], b['progress']
    return any([t.get('goal'), t.get('title'), t.get('acceptance'), p.get('next_action'), p.get('done'),
                b.get('decisions'), b.get('evidence'), b.get('do_not'), b['holds'].get('prohibitions'),
                b['holds'].get('approvals_pending')])


# ---------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane baton', description='engine-neutral task batons')
    sub = ap.add_subparsers(dest='sub', required=True)
    p = sub.add_parser('new')
    p.add_argument('slug')
    p.add_argument('--goal', required=True)
    p.add_argument('--tracker')
    p.add_argument('--title')
    p.add_argument('--cd')
    p.add_argument('--accept', action='append')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('set')
    p.add_argument('slug')
    for flag in ('--next', '--goal', '--title', '--tracker', '--decision', '--why', '--user-quote', '--file'):
        p.add_argument(flag)
    p.add_argument('--owner', choices=ENGINES)
    p.add_argument('--resolve', metavar='ID|TEXT', help='resolve one pending approval: with --grant "<user quote>" '
                   'or --deny ["<reason>"]')
    p.add_argument('--deny', nargs='?', const='', metavar='REASON')
    p.add_argument('--reason', choices=REASONS, default='user')
    for flag in ('--done', '--then', '--accept', '--reject', '--hold', '--grant', '--prohibit', '--evidence',
                 '--accept-status', '--read-first', '--do-not', '--verify'):
        p.add_argument(flag, action='append')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('snap')
    p.add_argument('slug')
    p.add_argument('--cd')
    p.add_argument('--reason', choices=REASONS, default='checkpoint')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('show')
    p.add_argument('slug')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('list')
    p.add_argument('--json', action='store_true')
    p = sub.add_parser('history')
    p.add_argument('slug')
    p.add_argument('--json', action='store_true')
    return ap


def _handoff_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane handoff', description='render a self-contained continuation brief')
    ap.add_argument('slug', nargs='?')
    ap.add_argument('--cd')
    ap.add_argument('--session', action='append', help='a Claude session id or Codex rollout id; repeat for a batch')
    ap.add_argument('--to', choices=ENGINES, help='default for a batch: the engine the session is not on')
    ap.add_argument('--out')
    ap.add_argument('--kind', choices=HANDOFF_KINDS,
                    help='kind of the printed delegated-worker line (needs --to); default: code --write inside '
                         'a linked worktree, else read-only intel')
    ap.add_argument('--out-dir', help='batch: write one handoff file per --session here')
    ap.add_argument('--with-session', help='with --session: also quote this session of the other engine')
    ap.add_argument('--json', action='store_true')
    return ap


def _summary(b: dict) -> str:
    return (f"baton {b['slug']} saved ({b.get('reason')}, {b.get('updated_at')}); "
            f"next: {common.clip(b['progress'].get('next_action'), 100) or '-'}")


def _baton_cmd(a) -> int:
    if a.sub == 'new':
        b = new(a.slug, a.goal, a.tracker, a.title, a.cd, a.accept)
        common.emit(b, a.json, _summary(b))
    elif a.sub == 'set':
        if a.file:
            b = replace_from_file(require_slug(a.slug), a.file)
        else:
            with baton_lock(require_slug(a.slug)):
                b = load_for_update(a.slug)
                if b is None:
                    raise BatonError(f'no baton {a.slug}; create it with `twinlane baton new {a.slug} --goal ...`')
                refresh_repo(b)
                reconcile_jobs(b)
                b = save(apply_set(b, a), a.reason, strict=True)
        common.emit(b, a.json, _summary(b))
    elif a.sub == 'snap':
        b = snap(require_slug(a.slug), a.cd, a.reason)
        common.emit(b, a.json, _summary(b) + f" | {b['repo'].get('branch')} {str(b['repo'].get('head'))[:10]} "
                                                f"dirty={len(b['repo'].get('dirty') or [])} jobs={len(b['jobs'])}")
    elif a.sub == 'show':
        b = load(require_slug(a.slug))
        if b is None:
            raise BatonError(f'no baton {a.slug}')
        reconcile_jobs(b)  # the view only: show never writes
        shown = dict(b, approvals_granted_note='Quoted approvals are context only; re-ask before any live write.')
        captured = (b['repo'].get('captured_at') and f"captured {b['repo']['captured_at']}"
                    or f"capture time not recorded; baton updated {b.get('updated_at')}")
        common.emit(shown, a.json, '\n'.join(render_baton_section(b) + [''] + render_repo_section(
            dict(b['repo'], upstream=b['repo'].get('pushed')), b['repo'].get('worktree'),
            f"Repo state ({captured})") + ['', FOOTER]))
    elif a.sub == 'list':
        rows = list_batons()
        common.emit(rows, a.json, '\n'.join(
            f"{r['slug']:<30} {r['owner'] or '-':<7} {r['age']:>4}  next: {common.clip(r['next_action'], 70) or '-'}"
            f"  [{r['worktree'] or '-'}]" for r in rows) or 'no batons')
    elif a.sub == 'history':
        rows = history(require_slug(a.slug))
        common.emit(rows, a.json, '\n'.join(f"{r.get('updated_at')}  {r.get('reason')}  next: "
                                            f"{common.clip((r.get('progress') or {}).get('next_action'), 80)}"
                                            for r in rows) or 'no history')
    return common.EXIT_OK


def redact_obj(value):
    """baton.redact applied to every string inside a JSON-shaped value (keys kept)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, list):
        return [redact_obj(v) for v in value]
    if isinstance(value, dict):
        return {k: redact_obj(v) for k, v in value.items()}
    return value


def handoff_sessions(session_ids: list[str], to: str | None, out_dir: str) -> list[dict]:
    """One handoff file per session id (a Claude session or a Codex rollout), written into out_dir.

    Each entry has engine, id, title, to and either path (the file) or error; a session that cannot be
    handed off does not stop the others. Without `to`, each goes to the engine it is not on."""
    from twinlane import sessions
    os.makedirs(out_dir, exist_ok=True)
    results, seen = [], set()
    for sid in session_ids:
        if sid in seen:
            continue
        seen.add(sid)
        found = session_engine(sid)
        if not found:
            results.append({'id': sid, 'error': 'no Claude session or Codex rollout with this id'})
            continue
        engine, path = found
        target = to or ('codex' if engine == 'claude' else 'claude')
        row = sessions.describe_session(engine, path) or {}
        entry = {'engine': engine, 'id': sid, 'to': target,
                 'title': row.get('title') or '(no user message in the head read)'}
        out = os.path.abspath(os.path.join(out_dir, f'handoff-{engine}-{sid}.md'))
        try:
            r = handoff(session=sid, to=target, out=out)
        except (BatonError, TimeoutError) as e:
            entry['error'] = str(e)
        else:
            entry.update(path=out, bytes=r['bytes'], slug=r['slug'], command=r['command'])
        results.append(entry)
    return results


def _handoff_batch_cmd(a) -> int:
    results = handoff_sessions(a.session, a.to, os.path.abspath(a.out_dir))
    if a.json:
        common.emit(redact_obj(results), True)
    for r in results:  # every printed field is redacted, whatever the transcript or a path held
        if r.get('error'):
            print(redact(f"twinlane handoff: {r['engine'] + ' ' if r.get('engine') else ''}{r['id']}: {r['error']}"),
                  file=sys.stderr)
        elif not a.json:
            print(redact(f"{r['engine']} {r['id']} {r['title']} -> Continue from {r['path']}"))
    return common.EXIT_USAGE if any(r.get('error') for r in results) else common.EXIT_OK


def _handoff_cmd(a) -> int:
    given = a.session or []
    if a.out_dir:
        if not given:
            raise BatonError('--out-dir needs at least one --session ID')
        if a.slug or a.cd or a.out or a.with_session:
            raise BatonError('--out-dir writes one file per --session; do not combine it with SLUG, --cd, --out '
                             'or --with-session')
        return _handoff_batch_cmd(a)
    if len(given) > 1:
        raise BatonError('several --session ids need --out-dir DIR (one handoff file per session)')
    if not (a.slug or a.cd or given):
        raise BatonError('handoff needs SLUG, --cd DIR or --session ID')
    r = handoff(a.slug, a.cd, given[0] if given else None, a.to, a.out, a.kind, a.with_session)
    if a.json:
        common.emit(redact_obj(r), True)  # every emitted field, --out and the brief path included
        return common.EXIT_OK
    if a.out:
        note = f"baton {r['slug']} written (reason handoff)" if r['slug'] else 'no baton written (session handoff)'
        print(redact(f"handoff brief: {r['brief']} ({r['bytes']} bytes); {note}"))
    else:
        print(r['markdown'])
    if r['interactive']:
        print(r['interactive'])
        print(r['delegated'])
    for line in r['command']:
        print(line)
    return common.EXIT_OK


def cli(cmd: str, argv: list[str]) -> int:
    parser = _parser() if cmd == 'baton' else _handoff_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as e:
        return common.EXIT_OK if e.code in (0, None) else common.EXIT_USAGE
    try:
        return _baton_cmd(args) if cmd == 'baton' else _handoff_cmd(args)
    except (BatonError, TimeoutError) as e:
        print(f'twinlane {cmd}: {e}', file=sys.stderr)
        return common.EXIT_USAGE
