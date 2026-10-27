"""Data firewall for anything that leaves this machine for a cloud worker.

One scan covers a brief, every attachment, a follow-up report and (through the optional `command` hook) whatever
the owner's own tools know about: secret VALUES, company names, customer lists. A hit blocks the send and names the
file and the rule only, never the text that matched.

Rules live in the user config under cloud.firewall (all optional):
  patterns          {name: regex}      extra patterns on top of the built-in token patterns
  deny_literals     [str]              exact strings that must never leave (case-sensitive)
  phone_max         3                  distinct phone-shaped numbers allowed per file
  email_max         5                  distinct addresses allowed per file (example/test addresses never count)
  email_allow       ['.test', ...]     address suffixes that do not count
  max_bytes         2000000            a bigger file, or one that is not UTF-8 text, is refused
  command           [argv, '{file}']   an extra checker: exit 0 passes, anything else blocks. Its output is never
                                       copied into a reason (a checker may echo what it matched)
  snapshot_scan     true               also scan the tracked files of the snapshot commit (scan_tree); false only
                                       when the mirror command is the owner's own filter and is trusted
  snapshot_exclude  [glob]             paths the mirror removes, so they are not scanned
  snapshot_deny_paths [glob]           file names that may not be in a snapshot (keys, .env, ...)
  snapshot_binary_ok  [glob]           names (images, fonts by default) that are not read as text; any other file
                                       that is binary or over max_bytes blocks unless it is in snapshot_exclude
"""
from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time

from . import common

SCANNER_VERSION = 2
MEDIA_OK = ['*.png', '*.jpg', '*.jpeg', '*.gif', '*.webp', '*.ico', '*.woff', '*.woff2', '*.ttf', '*.otf', '*.eot']
DEFAULT_PATTERNS = {
    'private-key': r'-----BEGIN [A-Z ]*PRIVATE KEY-----',
    'aws-access-key': r'\bAKIA[0-9A-Z]{16}\b',
    'github-token': r'\bgh[pousr]_[A-Za-z0-9]{30,}\b',
    'api-key-sk': r'\bsk-[A-Za-z0-9_\-]{20,}',
    'slack-token': r'\bxox[abprs]-[A-Za-z0-9-]{10,}',
    'jwt': r'\beyJ[A-Za-z0-9_-]{10,}\.eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}',
    'bearer-token': r'(?i)\bbearer\s+[A-Za-z0-9._\-]{24,}',
    'secret-assignment': r'''(?i)\b(?:password|passwd|secret|api[_-]?key|access[_-]?token)\b["']?\s*[:=]\s*["'][^"'\s]{12,}["']''',
}
PHONE_RE = re.compile(r'(?<![\w.])(?:\+\d{1,3}[ -]?\d{8,11}|0\d{9,10})(?![\w.])')
EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+(?:\.[\w-]+)+')
DEFAULTS = {'patterns': {}, 'deny_literals': [], 'phone_max': 3, 'email_max': 5,
            'email_allow': ['.test', '@example.com', '@example.org', '@localhost'], 'max_bytes': 2_000_000,
            'command': None, 'command_timeout_s': 60,
            'snapshot_scan': True, 'snapshot_exclude': [], 'snapshot_max_total_bytes': 1_000_000_000,
            'snapshot_timeout_s': 600, 'snapshot_binary_ok': MEDIA_OK,
            'snapshot_deny_paths': ['.env', '.env.*', '*.pem', '*.key', '*.pfx', '*.p12', 'id_rsa*', 'id_ed25519*',
                                    '.npmrc', '.pypirc', '.netrc', '*.kdbx']}
TEMPLATE_SUFFIXES = ('.example', '.sample', '.template', '.dist')


def settings(policy: dict | None) -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(((policy or {}).get('cloud') or {}).get('firewall') or {})
    return cfg


def scan_text(text: str, cfg: dict) -> list[str]:
    """Reasons (rule names with a line number) why this text may not leave; empty when it may."""
    reasons = []
    rules = dict(DEFAULT_PATTERNS)
    rules.update(cfg.get('patterns') or {})
    compiled = []
    for name, rx in rules.items():
        try:
            compiled.append((name, re.compile(rx)))
        except re.error:
            reasons.append(f'rule {name}: invalid pattern in cloud.firewall.patterns')
    for lit in cfg.get('deny_literals') or []:
        if lit and lit in text:
            reasons.append('denied literal from cloud.firewall.deny_literals')
    for n, line in enumerate(text.splitlines(), 1):
        for name, rx in compiled:
            if rx.search(line):
                reasons.append(f'line {n}: {name}')
    phones = set(PHONE_RE.findall(text))
    allow = tuple(a.lower() for a in cfg.get('email_allow') or ())
    emails = {e for e in EMAIL_RE.findall(text) if not e.lower().endswith(allow)}
    if len(phones) > int(cfg.get('phone_max') or 3):
        reasons.append(f'bulk contact data: {len(phones)} phone-shaped numbers')
    if len(emails) > int(cfg.get('email_max') or 5):
        reasons.append(f'bulk contact data: {len(emails)} email addresses')
    return reasons[:20]


def redact(text: str, cfg: dict) -> tuple[str, dict]:
    # Returns (text with everything the scan would object to replaced by [redacted:RULE], {rule: count}). Every
    # phone-shaped number and every e-mail address outside email_allow goes, however few there are: a report that
    # travels back to a cloud job carries conclusions, not contact data.
    counts: dict = {}

    def sub(rx, label, value):
        def hit(_m):
            counts[label] = counts.get(label, 0) + 1
            return f'[redacted:{label}]'
        return rx.sub(hit, value)

    rules = dict(DEFAULT_PATTERNS)
    rules.update(cfg.get('patterns') or {})
    for name, rx in rules.items():
        try:
            text = sub(re.compile(rx), name, text)
        except re.error:
            continue
    for lit in cfg.get('deny_literals') or []:
        if lit and lit in text:
            counts['literal'] = counts.get('literal', 0) + text.count(lit)
            text = text.replace(lit, '[redacted:literal]')
    allow = tuple(a.lower() for a in cfg.get('email_allow') or ())
    text = EMAIL_RE.sub(lambda m: m.group(0) if m.group(0).lower().endswith(allow) else
                        (counts.__setitem__('email', counts.get('email', 0) + 1) or '[redacted:email]'), text)
    text = sub(PHONE_RE, 'phone', text)
    return text, counts


def cli(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog='twinlane redact', description='Redact a text file with the cloud data firewall '
                                 'rules, so a local verify report can go back to a cloud job (--follow-up JOB --attach F).')
    ap.add_argument('file')
    ap.add_argument('--out', help='where to write the redacted copy (default: FILE.redacted)')
    ap.add_argument('--json', action='store_true')
    args = ap.parse_args(argv)
    cfg = settings(common.load_policy())
    try:
        with open(args.file, 'rb') as f:
            text = f.read().decode('utf-8')
    except (OSError, UnicodeDecodeError) as e:
        print(f'twinlane redact: cannot read {args.file}: {type(e).__name__}', file=sys.stderr)
        return common.EXIT_USAGE
    out, counts = redact(text, cfg)
    left = scan_text(out, cfg)
    dest = args.out or args.file + '.redacted'
    if not left:
        with open(dest, 'w', encoding='utf-8', newline='') as f:
            f.write(out)
    common.emit({'out': None if left else dest, 'redacted': counts, 'remaining': left}, args.json,
                (f'{sum(counts.values())} redactions {counts} -> {dest}' if not left else
                 f'NOT written: the text still fails the scan ({"; ".join(left[:3])})'))
    return 0 if not left else common.EXIT_REFUSED


def scan_file(path: str, cfg: dict) -> list[str]:
    try:
        size = os.path.getsize(path)
    except OSError as e:
        return [f'unreadable: {type(e).__name__}']
    if size > int(cfg.get('max_bytes') or 2_000_000):
        return [f'larger than {int(cfg.get("max_bytes") or 2_000_000)} bytes']
    try:
        with open(path, 'rb') as f:
            text = f.read().decode('utf-8')
    except UnicodeDecodeError:
        return ['not UTF-8 text (binary attachments are refused)']
    except OSError as e:
        return [f'unreadable: {type(e).__name__}']
    return scan_text(text, cfg) or _command_reasons(cfg, path)


def _command_reasons(cfg: dict, path: str) -> list[str]:
    """Run the owner's checker on one file. Its output is never copied: it may quote what it matched."""
    cmd = cfg.get('command')
    if not cmd:
        return []
    argv = [str(a).replace('{file}', path) for a in cmd]
    try:
        p = subprocess.run(argv, capture_output=True, timeout=int(cfg.get('command_timeout_s') or 60))
    except (OSError, subprocess.SubprocessError) as e:
        return [f'firewall command failed: {type(e).__name__}']
    return [f'firewall command refused (rc {p.returncode})'] if p.returncode else []


def scan(items: list[tuple[str, str]], cfg: dict) -> dict:
    """items: (label, path). Returns {'ok': bool, 'checked': n, 'blocked': [{'file': label, 'reasons': [...]}]}.
    The label, never the text, is what a report may show."""
    blocked = []
    for label, path in items:
        reasons = scan_file(path, cfg)
        if reasons:
            blocked.append({'file': label, 'reasons': reasons})
    return {'ok': not blocked, 'checked': len(items), 'blocked': blocked}


def _tree_entries(cwd: str, sha: str) -> list[tuple[str, str, int]] | None:
    """(path, object id, size) of every regular tracked file at SHA; None when the tree cannot be listed."""
    try:
        p = subprocess.run(['git', '-C', cwd, 'ls-tree', '-r', '-z', '--long', sha], capture_output=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None
    if p.returncode:
        return None
    out = []
    for raw in p.stdout.split(b'\0'):
        meta, _, path = raw.partition(b'\t')
        parts = meta.split()
        if len(parts) != 4 or parts[1] != b'blob':
            continue  # submodules hold no file content here; a symlink is a blob holding its target, scanned as text
        try:
            size = int(parts[3])
        except ValueError:
            continue
        out.append((path.decode('utf-8', 'replace').replace('\\', '/'), parts[2].decode('ascii'), size))
    return out


def _tree_key(sha: str, cfg: dict) -> str:
    """Everything that can change the verdict: the commit, the built-in rules, every setting that is read, and the
    identity (size and mtime) of any file the command hook names."""
    keys = ('patterns', 'deny_literals', 'phone_max', 'email_max', 'email_allow', 'max_bytes', 'command',
            'command_timeout_s', 'snapshot_exclude', 'snapshot_deny_paths', 'snapshot_binary_ok',
            'snapshot_max_total_bytes')
    files = []
    for part in cfg.get('command') or []:
        try:
            st = os.stat(str(part))
            files.append([str(part), st.st_size, st.st_mtime_ns])
        except (OSError, ValueError):
            pass
    blob = json.dumps([SCANNER_VERSION, DEFAULT_PATTERNS, sha] + [cfg.get(k) for k in keys] + [files],
                      sort_keys=True, default=str)
    return hashlib.sha256(blob.encode('utf-8')).hexdigest()


def _blob_head(cwd: str, obj: str, count: int, deadline: float) -> bytes:
    # the first `count` bytes of one blob; the git process is stopped as soon as they are read
    env = dict(os.environ, GIT_NO_LAZY_FETCH='1')
    proc = subprocess.Popen(['git', '-C', cwd, 'cat-file', 'blob', obj], stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    timer = threading.Timer(max(1.0, deadline - time.time()), proc.kill)
    timer.start()
    try:
        head = proc.stdout.read(count)
    finally:
        timer.cancel()
        proc.kill()
        proc.stdout.close()
        proc.wait()
    if time.time() > deadline:
        raise TimeoutError
    if not head:
        raise LookupError(obj)
    return head


def _blobs(cwd: str, wanted: list[tuple[str, str, int]], deadline: float):
    """Yield (path, bytes) for each wanted blob, in order, reading with `git cat-file --batch` in bounded chunks.
    Every git call has a timeout that kills it, and a missing object is an error, never a skipped file."""
    env = dict(os.environ, GIT_NO_LAZY_FETCH='1')
    i = 0
    while i < len(wanted):
        chunk, size = [], 0
        while i < len(wanted) and (not chunk or size + wanted[i][2] <= 64_000_000):
            chunk.append(wanted[i])
            size += wanted[i][2]
            i += 1
        left = deadline - time.time()
        if left <= 0:
            raise TimeoutError
        request = ('\n'.join(obj for _, obj, _ in chunk) + '\n').encode('ascii')
        p = subprocess.run(['git', '-C', cwd, 'cat-file', '--batch'], input=request, capture_output=True,
                           timeout=left, env=env)
        buf, pos = p.stdout, 0
        for path, _obj, _size in chunk:
            nl = buf.find(b'\n', pos)
            header = buf[pos:nl].split() if nl >= 0 else []
            if len(header) != 3 or header[1] != b'blob':
                raise LookupError(path)
            end = nl + 1 + int(header[2])
            if end > len(buf):
                raise LookupError(path)
            yield path, buf[nl + 1:end]
            pos = end + 1


def scan_tree(cwd: str, sha: str, cfg: dict, cache_path: str | None = None) -> dict:
    """Scan the tracked files of the snapshot commit before the mirror publishes it. Reasons name the path and the
    rule, never the text. Fail closed: a file that cannot be read as text (binary, over max_bytes, a missing
    object) blocks unless the owner lists it in snapshot_exclude (the mirror removes it) or it is a known image or
    font by name (snapshot_binary_ok). The command hook runs on every file, as it does for a brief.
    A passing result is cached per commit and rule set."""
    if not cfg.get('snapshot_scan', True):
        return {'ok': True, 'checked': 0, 'blocked': [], 'skipped': 'cloud.firewall.snapshot_scan is false'}
    key = _tree_key(sha, cfg)
    if cache_path:
        cached = common.read_json(cache_path, {}) or {}
        if isinstance(cached, dict) and key in cached:
            return dict(cached[key], cached=True)
    entries = _tree_entries(cwd, sha)
    if entries is None:
        return {'ok': False, 'checked': 0, 'blocked': [{'file': 'snapshot', 'reasons': ['cannot list the snapshot tree']}]}
    excluded = [g.replace('\\', '/') for g in cfg.get('snapshot_exclude') or []]
    deny = [g.lower() for g in cfg.get('snapshot_deny_paths') or []]
    media = [g.lower() for g in cfg.get('snapshot_binary_ok') or []]
    max_bytes = int(cfg.get('max_bytes') or 2_000_000)
    blocked, readable, skipped_media, total, media_paths, big_media = [], [], 0, 0, set(), []

    def block(path, reason):
        if len(blocked) < 20:
            blocked.append({'file': path, 'reasons': [reason]})

    for path, obj, size in entries:
        if any(fnmatch.fnmatch(path, g) for g in excluded):
            continue
        base = path.rsplit('/', 1)[-1].lower()
        if not base.endswith(TEMPLATE_SUFFIXES) and any(fnmatch.fnmatch(base, g) for g in deny):
            block(path, 'file name may not leave (cloud.firewall.snapshot_deny_paths)')
        elif any(fnmatch.fnmatch(base, g) for g in media):
            if size > max_bytes:
                big_media.append((path, obj))  # too big to read whole: its head must still look binary
            else:  # small enough to read: a name never exempts text, only binary content
                readable.append((path, obj, size))
                media_paths.add(path)
                total += size
        elif size > max_bytes:
            block(path, f'larger than {max_bytes} bytes, so it cannot be scanned (list it in cloud.firewall.snapshot_exclude '
                        'if the mirror removes it)')
        else:
            readable.append((path, obj, size))
            total += size
    if total > int(cfg.get('snapshot_max_total_bytes') or 1_000_000_000):
        return {'ok': False, 'checked': len(readable), 'blocked': [{'file': 'snapshot', 'reasons': [
            f'{total} bytes of text to scan: list generated or vendored paths in cloud.firewall.snapshot_exclude']}]}
    deadline = time.time() + int(cfg.get('snapshot_timeout_s') or 600)
    if len(blocked) < 20:
        try:
            with tempfile.TemporaryDirectory(prefix='twinlane-fw-') as tmp:
                for path, obj in big_media:
                    if len(blocked) >= 20:
                        break
                    if b'\0' in _blob_head(cwd, obj, 65536, deadline):
                        skipped_media += 1
                    else:
                        block(path, f'larger than {max_bytes} bytes and its start is not binary, so a media name '
                                    'cannot vouch for it (list it in cloud.firewall.snapshot_exclude)')
                for path, data in _blobs(cwd, readable, deadline):
                    if len(blocked) >= 20:
                        break
                    if b'\0' in data:
                        if path in media_paths:
                            skipped_media += 1
                        else:
                            block(path, 'binary content cannot be scanned (list it in cloud.firewall.snapshot_exclude '
                                        'or snapshot_binary_ok)')
                        continue
                    try:
                        text = data.decode('utf-8')
                    except UnicodeDecodeError:
                        if path not in media_paths:
                            block(path, 'not valid UTF-8 text, so it cannot be scanned (list it in '
                                        'cloud.firewall.snapshot_exclude or snapshot_binary_ok)')
                            continue
                        text = data.decode('utf-8', 'replace')  # a media name with no NUL: still scanned for what it holds
                    reasons = scan_text(text, cfg)
                    if not reasons and cfg.get('command'):
                        if time.time() > deadline:
                            raise TimeoutError
                        ext = os.path.splitext(path)[1]
                        probe = os.path.join(tmp, 'blob' + (ext if re.fullmatch(r'\.[A-Za-z0-9]{1,10}', ext) else ''))
                        with open(probe, 'wb') as f:
                            f.write(data)
                        reasons = _command_reasons(cfg, probe)
                    if reasons:
                        block(path, '; '.join(reasons[:3]))
        except (TimeoutError, subprocess.TimeoutExpired):
            block('snapshot', 'the snapshot scan timed out')
        except (LookupError, OSError, ValueError, subprocess.SubprocessError):
            block('snapshot', 'a file could not be read from the snapshot (a missing object?)')
    result = {'ok': not blocked, 'checked': len(readable), 'blocked': blocked, 'skipped_media': skipped_media}
    if result['ok'] and cache_path:
        try:
            with common.file_lock(cache_path + '.lock', timeout_s=5):
                cached = common.read_json(cache_path, {}) or {}
                cached = dict(list(cached.items())[-49:]) if isinstance(cached, dict) else {}
                cached[key] = result
                common.write_json(cache_path, cached)
        except (OSError, TimeoutError, ValueError):
            pass
    return result


def scan_brief(text: str, cfg: dict) -> list[str]:
    """The brief text itself (not yet a file)."""
    return scan_text(text, cfg)


def verdict_line(result: dict) -> str:
    if result.get('ok'):
        return f"firewall passed {result.get('checked', 0)} file(s)"
    parts = [f"{b['file']}: {'; '.join(b['reasons'][:3])}" for b in result['blocked'][:5]]
    return 'FIREWALL BLOCK: ' + ' | '.join(parts)


def safe_dest(dest: str) -> str | None:
    """Why an attachment destination inside the cloud snapshot is refused, or None."""
    norm = dest.replace('\\', '/')
    if not norm or norm.startswith('/') or re.match(r'^[A-Za-z]:', norm) or '..' in norm.split('/'):
        return 'destination must be a relative path without ..'
    return None
