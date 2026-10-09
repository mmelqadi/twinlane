"""`twinlane sessions`: the user's open interactive sessions on both engines, newest first.

Claude Code transcripts (~/.claude/projects/*/*.jsonl) and Codex rollouts (~/.codex/sessions/**/rollout-*.jsonl)
are black boxes read through baton.py's readers. Candidates are chosen by mtime first (a directory scan, no file
opens), then each is read through a bounded head (for the title) and a bounded tail (for the state), so the Codex
tree (gigabytes) is never read whole. Rollouts that are not someone's open session are left out: Codex subagent
threads, import-only rollouts, `codex exec` runs and `claude -p`/SDK runs (marked `job`, shown only with --jobs),
and any session a `twinlane run` job recorded in its receipt.

Never spends model tokens. Every text field is redacted at the source, so callers (the CLI, a future
`twinlane board`) can print rows as they are.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time

from twinlane import baton, common

ACTIVE_S = 120                 # modified this recently: the session is probably running now
DEFAULT_SINCE_S = 48 * 3600
DEFAULT_LIMIT = 30
HEAD_BYTES = 3_000_000         # Codex: enough to read past AGENTS.md/instruction wrappers (up to ~100 KB each) to the first user turn
CLAUDE_HEAD_BYTES = 600_000
TAIL_BYTES = 192_000           # first tail read; widened once when it holds no turn marker
TAIL_WIDE_BYTES = 2_000_000
TITLE_CHARS = 70
LAST_CHARS = 90
INDEX_BYTES = 2_000_000        # Codex session_index.jsonl: last bytes read, once per run
CUT_COUNT_BUDGET_S = 1.5       # time spent counting the rows a --limit cut off; past it the count is "at least"
JOB_SCAN_MAX = 3000           # newest job dirs whose receipts are read for session ids
FORWARD_MAX = 10               # sessions named in one printed forward command

STATES = ('active', 'waiting-on-user', 'mid-turn', 'unknown')
ENGINE_CHOICES = ('claude', 'codex', 'all')

_IDE_TAG = re.compile(r'<(ide_[a-z_]+)>.*?</\1>|</?pasted_content[^>]*>', re.S)
_LOCAL_NOISE = ('<local-command', 'Caveat: The messages below')  # echoes of local commands: not a turn
_ROLLOUT_ID = re.compile(r'rollout-\d{4}-\d\d-\d\dT[\d-]+-([0-9a-f]{8}-[0-9a-f-]{27})')


# ---------------------------------------------------------------- small helpers

def parse_since(text) -> float:
    """'48h' / '2d' / '90m' / '30s' / '1w' -> seconds. A bare number is hours."""
    m = re.match(r'^\s*(\d+(?:\.\d+)?)\s*([smhdw]?)\s*$', str(text))
    if not m:
        raise ValueError(f'bad --since {text!r}: use 90m, 48h, 2d or 1w')
    return float(m.group(1)) * {'s': 1, 'm': 60, 'h': 3600, 'd': 86400, 'w': 604800}[m.group(2) or 'h']


def _one_line(text, limit: int) -> str:
    """Redact first, then flatten and clip: a clip can never cut a secret in half and let a prefix through."""
    text = baton.redact(str(text or ''))
    return common.clip(' '.join(text.split()), limit)


def _last_line(text: str | None) -> str:
    """The last line with words in it (a closing code fence or a rule is not what the assistant said)."""
    lines = [ln.strip() for ln in (text or '').splitlines()
             if re.search(r'[A-Za-z0-9]', ln) and not ln.strip().startswith('```')]
    return lines[-1] if lines else ''


def _heartbeat(text: str) -> tuple[str | None, str]:
    """(automation id, message) of a Codex automation heartbeat wrapper; (None, text) for any other text."""
    if not text.lstrip().startswith('<heartbeat'):
        return None, text
    auto = re.search(r'<automation_id>(.*?)</automation_id>', text, re.S)
    msg = re.search(r'<message>(.*?)</message>', text, re.S)
    return (auto.group(1).strip() if auto else 'unnamed'), (msg.group(1).strip() if msg else '')


def _head_lines(path: str, max_bytes: int = HEAD_BYTES, keep=None) -> list[str]:
    """Whole lines from the first max_bytes of a (possibly huge) file; a cut-off last line is dropped.

    `keep` filters on the first 400 characters of a line, so an instruction wrapper of 100 KB is read past
    without being decoded into the result."""
    out, used = [], 0
    try:
        with open(path, 'rb') as h:
            while used < max_bytes:
                raw = h.readline(max_bytes - used + 1)
                if not raw:
                    break
                used += len(raw)
                if not raw.endswith(b'\n') and used > max_bytes:
                    break
                line = raw.decode('utf-8', errors='replace').rstrip('\r\n')
                if keep is None or keep(line[:400]):
                    out.append(line)
    except OSError:
        return out
    return out


def _file_size(path: str) -> int:
    try:
        return os.path.getsize(path)
    except OSError:
        return 0


# ---------------------------------------------------------------- twinlane job sessions

def job_session_ids() -> set[str]:
    """Session ids recorded by `twinlane run` jobs (receipts and attempt lists): those sessions are workers."""
    ids = set()
    try:
        names = sorted(os.listdir(common.JOBS), reverse=True)[:JOB_SCAN_MAX]
    except OSError:
        return ids

    def take(obj):
        if isinstance(obj, dict) and isinstance(obj.get('session_id'), str) and obj['session_id']:
            ids.add(obj['session_id'])

    for name in names:
        for fname in ('result.json', 'status.json'):
            doc = common.read_json(os.path.join(common.JOBS, name, fname), None)
            take(doc)
            if isinstance(doc, dict) and isinstance(doc.get('attempts'), list):
                for a in doc['attempts']:
                    take(a)
    return ids


# ---------------------------------------------------------------- Claude transcripts

def _claude_user_text(obj: dict) -> str:
    return baton._clean_user_text(baton._claude_text((obj.get('message') or {}).get('content')))


def scan_claude(lines: list[str]) -> dict:
    """One pass over transcript lines: entrypoint, last cwd, model, the last assistant text and who spoke last.

    `last_turn` is 'assistant' only for a finished assistant message (text, no tool call pending);
    a pending tool call, a tool result and a user message are all 'busy' (the assistant owes the next move)."""
    out = {'entrypoint': None, 'cwd': None, 'model': None, 'last_text': None, 'last_turn': None,
           'first_user': None}
    for ln in lines:
        if '"message"' not in ln and '"entrypoint"' not in ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(obj, dict):
            continue
        out['entrypoint'] = obj.get('entrypoint') or out['entrypoint']
        kind = obj.get('type')
        if kind not in ('user', 'assistant') or obj.get('isSidechain'):
            continue
        out['cwd'] = obj.get('cwd') or out['cwd']
        msg = obj.get('message') if isinstance(obj.get('message'), dict) else {}
        content = msg.get('content')
        if kind == 'assistant':
            if msg.get('model') and msg['model'] != '<synthetic>':
                out['model'] = msg['model']
            blocks = [b for b in content if isinstance(b, dict)] if isinstance(content, list) else []
            text = baton._claude_text(content).strip()
            if text:
                out['last_text'] = text
            if blocks and blocks[-1].get('type') == 'tool_use':
                out['last_turn'] = 'busy'
            elif blocks and blocks[-1].get('type') in ('thinking', 'redacted_thinking'):
                out['last_turn'] = 'busy'  # a streamed assistant message whose text is still to come
            elif 'stop_reason' in msg and msg['stop_reason'] in (None, 'tool_use'):
                out['last_turn'] = 'busy'
            else:
                out['last_turn'] = 'assistant'
        elif not obj.get('isMeta'):
            text = _claude_user_text(obj)
            if text.startswith(_LOCAL_NOISE):
                continue
            out['last_turn'] = 'busy'
            if text and not text.startswith(baton.USER_NOISE_PREFIXES) and baton._is_real_claude_user(obj):
                out['first_user'] = out['first_user'] or text
    return out


def _claude_title(lines: list[str]) -> str | None:
    """The first real user message of the session (an IDE-context tag block is not the user's words)."""
    for ln in lines:
        if '"user"' not in ln or '"message"' not in ln:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(obj, dict) or obj.get('type') != 'user' or not baton._is_real_claude_user(obj):
            continue
        text = _IDE_TAG.sub('', _claude_user_text(obj)).strip()
        if text and not text.startswith(baton.USER_NOISE_PREFIXES):
            return text
    return None


def _claude_names(lines: list[str]) -> tuple[str | None, str | None]:
    """(last `custom-title`, last `agent-name`) in these lines: the names the Claude app shows."""
    custom = agent = None
    for ln in lines:
        key = 'customTitle' if '"customTitle"' in ln else 'agentName' if '"agentName"' in ln else None
        if not key:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        val = obj.get(key) if isinstance(obj, dict) else None
        if isinstance(val, str) and val.strip():
            if key == 'customTitle':
                custom = val.strip()
            else:
                agent = val.strip()
    return custom, agent


def _describe_claude(path: str, ctx: dict) -> dict | None:
    jobs = ctx['jobs']
    sid = os.path.splitext(os.path.basename(path))[0]
    size = _file_size(path)
    tail = common.tail_lines(path, TAIL_BYTES)
    info = scan_claude(tail)
    head = tail if size <= TAIL_BYTES else _head_lines(path, CLAUDE_HEAD_BYTES)
    if not info['entrypoint'] and head is not tail:
        info['entrypoint'] = scan_claude(head)['entrypoint']
    names = [_claude_names(tail)] + ([_claude_names(head)] if head is not tail else [])
    # A custom title anywhere in the reads beats any agent name (the tail's newest first in each class).
    title = next((n[0] for n in names if n[0]), None) or next((n[1] for n in names if n[1]), None) \
        or _claude_title(head) or info['first_user']
    is_job = sid in jobs or str(info['entrypoint'] or '').startswith('sdk')
    if info['last_turn'] is None and size > TAIL_BYTES:  # one huge last turn filled the tail
        wide = scan_claude(common.tail_lines(path, TAIL_WIDE_BYTES))
        info.update({k: wide[k] for k in ('cwd', 'model', 'last_text', 'last_turn')})
    return {'engine': 'claude', 'id': sid, 'cwd': info['cwd'], 'model': info['model'], 'title': title,
            'last_text': info['last_text'], 'turn': {'assistant': 'assistant', 'busy': 'busy'}.get(info['last_turn']),
            'job': is_job, 'path': path}


# ---------------------------------------------------------------- Codex rollouts

def scan_codex(lines: list[str]) -> dict:
    """One pass over rollout lines: who spoke last, the last agent text, the model, native vs imported turns."""
    out = {'model': None, 'last_text': None, 'last_turn': None, 'native': 0, 'external': 0, 'automation': None}
    turn = None
    for ln in lines:
        if '"payload"' not in ln:
            continue
        head = ln[:300]
        if '"task_started"' not in head and '"task_complete"' not in head and '"turn_aborted"' not in head \
                and '"turn_context"' not in head and '"user_message"' not in head and '"agent_message"' not in head:
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        p = obj.get('payload') if isinstance(obj, dict) and isinstance(obj.get('payload'), dict) else None
        if p is None:
            continue
        ptype = p.get('type')
        if p.get('turn_id'):
            turn = p['turn_id']
        external = baton._is_external_turn(turn)
        if obj.get('type') == 'turn_context':
            if isinstance(p.get('model'), str) and not external:
                out['model'] = p['model']
            continue
        if obj.get('type') != 'event_msg':
            continue
        if ptype == 'task_started':
            out['external' if external else 'native'] += 1
            if not external:
                out['last_turn'] = 'busy'
        elif external:
            continue
        elif ptype == 'user_message':
            out['last_turn'] = 'busy'
        elif ptype == 'agent_message':
            _take_text(out, p.get('message'))
        elif ptype in ('task_complete', 'turn_aborted'):
            if ptype == 'task_complete':
                _take_text(out, p.get('last_agent_message'))
            out['last_turn'] = 'assistant'
    return out


def _take_text(out: dict, text) -> None:
    text = text.strip() if isinstance(text, str) else ''
    auto, text = _heartbeat(text)
    if auto:
        out['automation'] = auto
    if text and not text.startswith(baton.AGENT_NOISE_PREFIXES):
        out['last_text'] = text


def _codex_title(lines: list[str]) -> str | None:
    """The first real user message of a native turn (instruction wrappers and imported turns skipped)."""
    turn = None
    for ln in lines:
        if not baton.USER_TURN_LINE.search(ln[:400]):
            continue
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        p = obj.get('payload') if isinstance(obj, dict) and isinstance(obj.get('payload'), dict) else {}
        if p.get('turn_id'):
            turn = p['turn_id']
        if baton._is_external_turn(turn):
            continue
        text = None
        if obj.get('type') == 'event_msg' and p.get('type') == 'user_message':
            text = p.get('message')
        elif obj.get('type') == 'response_item' and p.get('type') == 'message' and p.get('role') == 'user':
            text = baton._codex_user_text(p)
        text = text.strip() if isinstance(text, str) else ''
        text = _IDE_TAG.sub('', text).strip()
        if text and not text.startswith(baton.USER_NOISE_PREFIXES):
            return text
    return None


def _codex_id(path: str, meta: dict) -> str:
    sid = meta.get('id') or meta.get('session_id')
    if isinstance(sid, str) and sid:
        return sid
    m = _ROLLOUT_ID.search(os.path.basename(path))
    return m.group(1) if m else os.path.splitext(os.path.basename(path))[0]


def codex_index_path() -> str:
    return os.path.join(os.path.dirname(baton.CODEX_SESSIONS), 'session_index.jsonl')


def codex_thread_names() -> dict[str, str]:
    """id -> the thread name the Codex app shows, from session_index.jsonl (last INDEX_BYTES only).

    Several lines for one id: the newest `updated_at` wins; equal or unparseable stamps go to the later line."""
    best: dict[str, tuple] = {}
    for n, ln in enumerate(common.tail_lines(codex_index_path(), INDEX_BYTES)):
        try:
            obj = json.loads(ln)
        except ValueError:
            continue
        if not isinstance(obj, dict) or not isinstance(obj.get('id'), str):
            continue
        name = obj.get('thread_name')
        if not isinstance(name, str) or not name.strip():
            continue
        when = common.parse_time(obj.get('updated_at'))
        key = (when.timestamp() if when else 0.0, n)
        if obj['id'] not in best or key >= best[obj['id']][0]:
            best[obj['id']] = (key, name.strip())
    return {k: v[1] for k, v in best.items()}


def _describe_codex(path: str, ctx: dict) -> dict | None:
    jobs, names = ctx['jobs'], ctx['codex_names']
    meta = baton.rollout_meta(path)
    if baton._is_subagent(meta):
        return None
    sid = _codex_id(path, meta)
    size = _file_size(path)
    lines = common.tail_lines(path, TAIL_BYTES)
    info = scan_codex(lines)
    if info['last_turn'] is None and size > TAIL_BYTES:  # no turn marker in the tail: look further back once
        info = scan_codex(common.tail_lines(path, TAIL_WIDE_BYTES))
    if info['external'] and not info['native']:
        return None  # only an import of another tool's history
    title = names.get(sid)  # what the Codex sidebar shows
    if not title or not info['model']:
        head = lines if size <= TAIL_BYTES else _head_lines(
            path, keep=lambda ln: bool(baton.USER_TURN_LINE.search(ln)) or '"turn_context"' in ln)
        title = title or _codex_title(head) or (f"automation: {info['automation']}" if info['automation'] else None)
        if not info['model']:  # no turn context in the tail (one long turn): the first turn's model is the best guess
            info['model'] = scan_codex([ln for ln in head if '"turn_context"' in ln[:300]][:1])['model']
    return {'engine': 'codex', 'id': sid, 'cwd': meta.get('cwd'), 'model': info['model'], 'title': title,
            'last_text': info['last_text'], 'turn': {'assistant': 'assistant', 'busy': 'busy'}.get(info['last_turn']),
            'job': sid in jobs or baton._is_exec(meta), 'path': path}


# ---------------------------------------------------------------- listing

def _candidates(engine: str, cutoff: float) -> list[tuple[float, str, str]]:
    """(mtime, engine, path) for every transcript/rollout modified after the cutoff: directory scans only."""
    found = []
    if engine in ('claude', 'all'):
        try:
            projects = [e.path for e in os.scandir(baton.CLAUDE_PROJECTS) if e.is_dir()]
        except OSError:
            projects = []
        for d in projects:
            try:
                entries = list(os.scandir(d))
            except OSError:
                continue
            for e in entries:
                if e.name.endswith('.jsonl'):
                    _add(found, 'claude', e, cutoff)
    if engine in ('codex', 'all'):
        stack = [baton.CODEX_SESSIONS]
        while stack:
            try:
                entries = list(os.scandir(stack.pop()))
            except OSError:
                continue
            for e in entries:
                try:
                    if e.is_dir():
                        stack.append(e.path)
                    elif e.name.startswith('rollout-') and e.name.endswith('.jsonl'):
                        _add(found, 'codex', e, cutoff)
                except OSError:
                    continue
    return sorted(found, reverse=True)


def _add(found: list, engine: str, entry, cutoff: float) -> None:
    try:
        mtime = entry.stat().st_mtime
    except OSError:
        return
    if mtime >= cutoff:
        found.append((mtime, engine, entry.path))


def classify(turn: str | None, age_s: float) -> str:
    if age_s < ACTIVE_S:
        return 'active'
    return {'assistant': 'waiting-on-user', 'busy': 'mid-turn'}.get(turn or '', 'unknown')


def _safe(value, limit: int) -> str | None:
    """A printable field: redacted, one line, clipped; None when empty."""
    return _one_line(value, limit) or None


def _row(raw: dict, mtime: float) -> dict:
    """A session row. Every printable field (id, cwd, path, model, title, last) goes through baton.redact."""
    age = max(0.0, common.now().timestamp() - mtime)
    last = _last_line(raw['last_text'])
    return {'engine': raw['engine'], 'id': _safe(raw['id'], 120), 'cwd': _safe(raw['cwd'], 300),
            'modified': common.iso(common.parse_time(mtime)), 'age_s': int(age), 'age': common.fmt_age(age),
            'model': _safe(raw['model'], 60),
            'title': _safe(raw['title'], TITLE_CHARS) or '(no user message in the head read)',
            'last': _safe(last, LAST_CHARS),
            'state': 'job' if raw['job'] else classify(raw['turn'], age), 'path': _safe(raw['path'], 500)}


def _context() -> dict:
    """Read once per run: job session ids and the Codex app's thread names."""
    return {'jobs': job_session_ids(), 'codex_names': codex_thread_names()}


def describe_session(engine: str, path: str) -> dict | None:
    """One transcript/rollout as a row (same shape as list_sessions); None for a subagent or import-only rollout."""
    raw = (_describe_claude if engine == 'claude' else _describe_codex)(path, _context())
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return None
    return _row(raw, mtime) if raw else None


def collect_sessions(engine: str = 'all', since_s: float = DEFAULT_SINCE_S, cwd: str | None = None,
                     limit: int = DEFAULT_LIMIT, include_jobs: bool = False, states=None) -> dict:
    """{'rows': [...], 'cut': N, 'cut_waiting': M, 'cut_exact': bool}: the newest `limit` matching rows, and how
    many further matching rows the limit cut off (counted for up to CUT_COUNT_BUDGET_S; then `cut_exact` is False
    and the counts are lower bounds). `states` keeps only rows in those states ('job' implies include_jobs)."""
    if engine not in ENGINE_CHOICES:
        raise ValueError(f'engine must be one of {ENGINE_CHOICES}')
    states = set(states or ())
    bad = states - set(STATES) - {'job'}
    if bad:
        raise ValueError(f'unknown --state {sorted(bad)}: use {", ".join(STATES + ("job",))}')
    include_jobs = include_jobs or 'job' in states
    want = baton.norm_dir(cwd) if cwd else None
    ctx = _context()
    rows, cut, cut_waiting, cut_exact, started = [], 0, 0, True, time.monotonic()
    for mtime, eng, path in _candidates(engine, common.now().timestamp() - since_s):
        full = bool(limit) and len(rows) >= limit
        if full and time.monotonic() - started > CUT_COUNT_BUDGET_S:
            cut_exact = False
            break
        raw = (_describe_claude if eng == 'claude' else _describe_codex)(path, ctx)
        if raw is None or (raw['job'] and not include_jobs):
            continue
        if want and not baton.under_dir(baton.norm_dir(raw['cwd']), want):
            continue
        row = _row(raw, mtime)
        if states and row['state'] not in states:
            continue
        if full:
            cut += 1
            cut_waiting += row['state'] == 'waiting-on-user'
        else:
            rows.append(row)
    return {'rows': rows, 'cut': cut, 'cut_waiting': cut_waiting, 'cut_exact': cut_exact}


def list_sessions(engine: str = 'all', since_s: float = DEFAULT_SINCE_S, cwd: str | None = None,
                  limit: int = DEFAULT_LIMIT, include_jobs: bool = False, states=None) -> list[dict]:
    """Recent interactive sessions on both engines, newest first (rows are redacted; see _row).

    `twinlane run` workers, SDK runs and `codex exec` runs are left out unless include_jobs (state 'job').
    Codex subagent threads and import-only rollouts are never listed. Use collect_sessions for the cut count."""
    return collect_sessions(engine, since_s, cwd, limit, include_jobs, states)['rows']


# ---------------------------------------------------------------- forwarding

def forward_commands(rows: list[dict], out_dir: str | None = None) -> list[str]:
    """Ready-to-run `twinlane handoff` commands that move the waiting-on-user sessions to the other engine."""
    out_dir = out_dir or os.path.join(common.STATE, 'forward')
    cmds = []
    for engine, target in (('claude', 'codex'), ('codex', 'claude')):
        ids = [r['id'] for r in rows if r['engine'] == engine and r['state'] == 'waiting-on-user']
        for i in range(0, len(ids), FORWARD_MAX):  # every listed session is named, FORWARD_MAX per command
            cmds.append(baton.redact('twinlane handoff ' + ' '.join(f'--session {s}' for s in ids[i:i + FORWARD_MAX])
                                     + f' --to {target} --out-dir "{out_dir}"'))
    return cmds


def cut_notes(res: dict, limit: int, flags: str = '') -> list[str]:
    """Lines saying how many matching rows --limit cut off, and how many of those wait on the user."""
    if not res.get('cut'):
        if res.get('cut_exact', True):
            return []
        flags = (flags + ' ').lstrip()  # the count ran out of time before any cut row: say so, never stay silent
        return [f"more rows may exist (limit {limit}; the count ran out of time); run: twinlane sessions {flags}--limit 0"]
    plus = '' if res.get('cut_exact', True) else '+'  # a lower bound once the count ran out of time
    flags = (flags + ' ').lstrip()
    notes = [f"+{res['cut']}{plus} more rows not shown (limit {limit}); run: twinlane sessions {flags}--limit 0"]
    if res.get('cut_waiting'):
        notes.append(f"+{res['cut_waiting']}{plus} more waiting-on-user "
                     f"(run: twinlane sessions {flags}--state waiting-on-user --limit 0 --md)")
    return notes


def format_text(rows: list[dict], notes: list[str] | None = None) -> str:
    if not rows:
        return 'no recent interactive sessions'
    out = []
    for r in rows:
        out.append(f"{r['engine']:<6} {r['id']}  {r['age']:>4} ago  {r['state']:<15} {r['model'] or '-'}  {r['cwd'] or '-'}")
        out.append(f"       title: {r['title']}")
        if r['last']:
            out.append(f"       last:  {r['last']}")
    return baton.redact('\n'.join(out + list(notes or [])))


def _cell(text) -> str:
    return str(text if text is not None else '-').replace('|', '/').replace('\n', ' ')


def format_md(rows: list[dict], out_dir: str | None = None, notes: list[str] | None = None) -> str:
    if not rows:
        return 'No recent interactive sessions.'
    out = ['| Engine | Id | Age | State | Model | Cwd | Title | Last assistant line |', '|---|---|---|---|---|---|---|---|']
    for r in rows:
        out.append('| ' + ' | '.join(_cell(v) for v in (r['engine'], '`' + r['id'] + '`', r['age'], r['state'], r['model'],
                                                          '`' + str(r['cwd'] or '-') + '`', r['title'], r['last'])) + ' |')
    waiting_cut = [n for n in notes or [] if 'waiting-on-user' in n]
    if [n for n in notes or [] if n not in waiting_cut]:
        out += [''] + [f'- {n}' for n in notes if n not in waiting_cut]
    cmds = forward_commands(rows, out_dir)
    out += ['', '### Forward', '']
    if cmds:
        out += ['Sessions waiting on you can move to the other engine, at most '
                f'{FORWARD_MAX} per command. Each command writes one handoff file per session and prints '
                '`Continue from <path>` for it:', '', '```']
        out += cmds + ['```']
    else:
        out.append('No listed session is `waiting-on-user`: nothing to forward from this list.')
    out += [f'- {n}' for n in waiting_cut]
    return baton.redact('\n'.join(out))


# ---------------------------------------------------------------- CLI

def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog='twinlane sessions', description='recent interactive sessions on both engines')
    ap.add_argument('--engine', choices=ENGINE_CHOICES, default='all')
    ap.add_argument('--since', default='48h', help='look-back window: 90m, 48h, 2d, 1w (default 48h)')
    ap.add_argument('--cwd', help='only sessions whose cwd is DIR or under it')
    ap.add_argument('--limit', type=int, default=DEFAULT_LIMIT, help=f'0 = no limit (default {DEFAULT_LIMIT})')
    ap.add_argument('--jobs', action='store_true', help='also list twinlane workers and SDK runs (state job)')
    ap.add_argument('--state', action='append', metavar='STATE',
                    help='only these states (active, waiting-on-user, mid-turn, unknown, job); repeat or comma-separate')
    fmt = ap.add_mutually_exclusive_group()
    fmt.add_argument('--json', action='store_true')
    fmt.add_argument('--md', action='store_true')
    return ap


def cli(argv: list[str]) -> int:
    try:
        a = _parser().parse_args(argv)
    except SystemExit as e:
        return common.EXIT_OK if e.code in (0, None) else common.EXIT_USAGE
    states = [s.strip() for item in a.state or [] for s in item.split(',') if s.strip()]
    limit = max(a.limit, 0)
    try:
        res = collect_sessions(a.engine, parse_since(a.since), a.cwd, limit, a.jobs, states)
    except ValueError as e:
        print(f'twinlane sessions: {e}', file=sys.stderr)
        return common.EXIT_USAGE
    flags = ' '.join(filter(None, [f'--engine {a.engine}' if a.engine != 'all' else '',
                                   f'--since {a.since}' if a.since != '48h' else '',
                                   f'--cwd "{a.cwd}"' if a.cwd else '', '--jobs' if a.jobs else '']))
    notes = cut_notes(res, limit, flags)
    if a.json:
        common.emit(res['rows'], True)  # stays a plain list; the cut count goes to stderr
        for n in notes:
            print(baton.redact(f'twinlane sessions: {n}'), file=sys.stderr)
    else:
        print(format_md(res['rows'], None, notes) if a.md else format_text(res['rows'], notes))
    return common.EXIT_OK
