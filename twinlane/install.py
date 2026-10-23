"""Install helpers: the advisory Claude Code hook, command aliases and the agent skill.

  twinlane install                       preview adding the UserPromptSubmit hook
  twinlane install --yes                 apply it (timestamped backup first; idempotent)
  twinlane install --check [--json]      read-only report: hook count (exactly one expected), shims, skills
  twinlane install --unhook [--yes]      remove our hook entry only (backup first)
  twinlane install --hook-command CMD    register (or, with --unhook/--check, recognise) a custom hook command
  twinlane install --alias NAME[,NAME]   write NAME.cmd (CRLF) and NAME (LF) shims into --bin-dir
  twinlane install --skill DIR           write the packaged SKILL.md to DIR/SKILL.md (repeat for several dirs)

Every write previews by default and needs --yes. A file that is replaced is backed up first, and a shim
that twinlane did not write is never overwritten unless --replace is given. Every other settings key and
every other hook is preserved.
"""
from __future__ import annotations

import argparse
import copy
import difflib
import json
import os
import re
import shlex
import shutil
import sys
import tempfile
from pathlib import Path

from . import common, baton

HOOK_EVENT = 'UserPromptSubmit'
HOOK_TIMEOUT_S = 5
EVENT_TIMEOUTS = {'UserPromptSubmit': 5, 'PreCompact': 30, 'Notification': 5, 'Stop': 5}
# Exit 2 from a UserPromptSubmit hook BLOCKS the prompt, and python itself exits 2 when the script is
# missing (folder moved, or mid-reinstall). `|| exit 0` turns every failure into a pass.
FAIL_OPEN = '|| exit 0'
_FAIL_OPEN_TAIL = re.compile(r'\s*\|\|\s*exit\s+0\s*$', re.I)
# Any spelling of "twinlane hook" / "<...>/twinlane/cli.py hook" (quotes, .exe/.cmd, python -m) is ours.
HOOK_PATTERN = re.compile(r'\btwinlane(?:\.(?:exe|cmd))?"?(?:[\\/]cli\.py"?)?\s+hook\b', re.I)

SHIM_MARK = 'twinlane alias shim'
ALIAS_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]{0,31}$')
SKILL_SOURCE = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'skill', 'SKILL.md')
MAX_SHIM_BYTES = 4096


# ---------------------------------------------------------------- hook command and matching

def command(custom: str | None = None) -> str:
    """The hook command to register. A custom command always fails open (`|| exit 0` is appended)."""
    if custom is None:
        return 'twinlane hook ' + FAIL_OPEN
    text = str(custom).strip()
    if not text or '\n' in text or '\r' in text:
        raise ValueError('--hook-command must be one non-empty line')
    return text if _FAIL_OPEN_TAIL.search(text) else text + ' ' + FAIL_OPEN


def _norm(cmd) -> str:
    text = _FAIL_OPEN_TAIL.sub('', str(cmd or '').strip()).replace('\\', '/')
    return re.sub(r'\s+', ' ', text).casefold()


def default_settings_path() -> str:
    return os.environ.get('TWINLANE_SETTINGS_PATH') or os.path.join(common.CLAUDE_DIR, 'settings.json')


def default_bin_dir() -> str:
    return os.path.join(common.HOME, '.local', 'bin')


def is_our_hook(item, custom: str | None = None) -> bool:
    if not isinstance(item, dict):
        return False
    text = str(item.get('command') or '')
    if HOOK_PATTERN.search(text):
        return True
    return bool(custom) and _norm(text) == _norm(custom)


def _groups(settings: dict, event=HOOK_EVENT) -> list:
    hooks = settings.get('hooks')
    groups = hooks.get(event) if isinstance(hooks, dict) else None
    return groups if isinstance(groups, list) else []


def count_our_hooks(settings: dict, custom: str | None = None, event=HOOK_EVENT) -> int:
    return sum(1 for g in _groups(settings, event) if isinstance(g, dict) and isinstance(g.get('hooks'), list)
               for h in g['hooks'] if is_our_hook(h, custom))


def add_hook(settings: dict, custom: str | None = None, event=HOOK_EVENT) -> bool:
    """Append our hook as its own matcher group. Returns False when it is already present."""
    if count_our_hooks(settings, custom, event):
        return False
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('settings "hooks" is not an object; refusing to edit')
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise ValueError(f'settings hooks.{event} is not a list; refusing to edit')
    groups.append({'hooks': [{'type': 'command', 'command': command(custom), 'timeout': EVENT_TIMEOUTS.get(event, HOOK_TIMEOUT_S)}]})
    return True


def remove_hook(settings: dict, custom: str | None = None, event=HOOK_EVENT) -> int:
    """Drop only our hook entries; a group (and the event key) emptied by that removal goes too."""
    hooks = settings.get('hooks')
    if not isinstance(hooks, dict) or not isinstance(hooks.get(event), list):
        return 0
    removed, kept = 0, []
    for group in hooks[event]:
        if not isinstance(group, dict) or not isinstance(group.get('hooks'), list):
            kept.append(group)
            continue
        inner = [h for h in group['hooks'] if not is_our_hook(h, custom)]
        dropped = len(group['hooks']) - len(inner)
        removed += dropped
        if dropped and not inner:
            continue
        group['hooks'] = inner
        kept.append(group)
    if removed:
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    return removed


# ---------------------------------------------------------------- file io

def load_settings(path) -> dict:
    """Missing file -> {}. Invalid JSON raises: never overwrite a file we could not parse."""
    path = Path(path)
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError(f'{path} is not a JSON object')
    return value


def backup_file(path) -> str | None:
    """Copy `path` to a timestamped sibling that never overwrites an earlier backup."""
    path = str(path)
    if not os.path.exists(path):
        return None
    dest = f'{path}.twinlane-backup-{common.stamp()}'
    n = 1
    while os.path.exists(dest):
        dest = f'{path}.twinlane-backup-{common.stamp()}-{n}'
        n += 1
    shutil.copy2(path, dest)
    return dest


def write_bytes(path: str, data: bytes, mode: int | None = None) -> None:
    """Temp file in the same directory, then os.replace; bytes are written as given (no newline translation)."""
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.tmp-', dir=folder)
    try:
        with os.fdopen(fd, 'wb') as handle:
            handle.write(data)
        if mode is not None:
            os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ---------------------------------------------------------------- aliases

def parse_alias_names(values) -> list:
    names = []
    for value in values or []:
        for raw in str(value).split(','):
            name = raw.strip()
            if not name:
                continue
            if not ALIAS_NAME.match(name):
                raise ValueError(f'invalid alias name {name!r}: use a letter, then letters, digits, "-" or "_" (max 32)')
            if name not in names:
                names.append(name)
    return names


def shim_target() -> tuple:
    """(python, cli.py) the shims run: this interpreter and this package's entry script."""
    py, cli = sys.executable or '', common.TWINLANE_PY
    if not py:
        raise ValueError('cannot find the running Python interpreter to point the shims at')
    return py, cli


def shim_bytes(kind: str, py: str, cli: str) -> bytes:
    """kind 'cmd' -> CRLF batch file; kind 'sh' -> LF POSIX shell script."""
    if kind == 'cmd':
        for part in (py, cli):
            if any(c in part for c in '"%\r\n'):
                raise ValueError(f'path {part!r} has a character a .cmd shim cannot quote (" or %)')
        text = f'@rem {SHIM_MARK}\r\n@"{py}" "{cli}" %*\r\n'
        return text.encode('utf-8')
    for part in (py, cli):
        if '\n' in part or '\r' in part:
            raise ValueError(f'path {part!r} contains a line break')
    text = '#!/bin/sh\n# ' + SHIM_MARK + '\nexec ' + shlex.quote(py.replace('\\', '/')) + ' ' + \
        shlex.quote(cli.replace('\\', '/')) + ' "$@"\n'
    return text.encode('utf-8')


def alias_files(bin_dir: str, name: str, py: str, cli: str) -> list:
    return [(os.path.join(bin_dir, name + '.cmd'), shim_bytes('cmd', py, cli), None),
            (os.path.join(bin_dir, name), shim_bytes('sh', py, cli), 0o755)]


def classify_shim(path: str, desired: bytes | None = None) -> str:
    """missing | dir | same | ours (a twinlane shim with other content) | foreign."""
    if os.path.isdir(path):
        return 'dir'
    if not os.path.lexists(path):
        return 'missing'
    try:
        if os.path.getsize(path) > MAX_SHIM_BYTES:
            return 'foreign'
        with open(path, 'rb') as handle:
            raw = handle.read()
    except OSError:
        return 'foreign'
    if desired is not None and raw == desired:
        return 'same'
    return 'ours' if SHIM_MARK.encode('utf-8') in raw else 'foreign'


def find_shim_names(bin_dir: str, limit: int = 2000) -> list:
    """Alias names in `bin_dir` whose files carry our marker (read-only; small files only)."""
    found = set()
    try:
        entries = list(os.scandir(bin_dir))
    except OSError:
        return []
    for entry in entries[:limit]:
        stem = entry.name[:-4] if entry.name.lower().endswith('.cmd') else entry.name
        if not ALIAS_NAME.match(stem):
            continue
        try:
            if entry.is_file() and entry.stat().st_size <= MAX_SHIM_BYTES and \
                    classify_shim(entry.path) == 'ours':
                found.add(stem)
        except OSError:
            continue
    return sorted(found)


# ---------------------------------------------------------------- check (read-only)

def _on_path(folder: str) -> bool:
    want = os.path.normcase(os.path.normpath(os.path.abspath(folder)))
    for entry in os.environ.get('PATH', '').split(os.pathsep):
        if entry and os.path.normcase(os.path.normpath(os.path.abspath(entry))) == want:
            return True
    return False


def check(settings_path: str, custom: str | None = None, bin_dir: str | None = None, names=None,
          skill_dirs=None) -> dict:
    bin_dir = bin_dir or default_bin_dir()
    py, cli = shim_target()
    hook = {'settings': settings_path, 'command': command(custom)}
    try:
        count = count_our_hooks(load_settings(settings_path), custom)
        hook.update(registered=count, exactly_one=count == 1)
    except (OSError, ValueError) as exc:
        hook['error'] = str(exc)
    aliases = {}
    for name in sorted(set(names or []) | set(find_shim_names(bin_dir))):
        entry = {}
        for path, desired, _ in alias_files(bin_dir, name, py, cli):
            state = classify_shim(path, desired)
            entry[os.path.basename(path)] = {'path': path, 'state': state, 'ok': state == 'same'}
        aliases[name] = entry
    if skill_dirs is None:
        skill_dirs = [os.path.join(common.CLAUDE_DIR, 'skills', 'twinlane'),
                      os.path.join(common.CODEX_DIR, 'skills', 'twinlane')]
    try:
        with open(SKILL_SOURCE, 'rb') as handle:
            source = handle.read()
    except OSError:
        source = None
    skills = {}
    for folder in skill_dirs:
        target = os.path.join(folder, 'SKILL.md')
        exists = os.path.isfile(target)
        current = False
        if exists and source is not None:
            try:
                with open(target, 'rb') as handle:
                    current = handle.read() == source
            except OSError:
                pass
        skills[folder] = {'path': target, 'exists': exists, 'current': current}
    events = {}
    try:
        settings = load_settings(settings_path)
        for event in ('PreCompact', 'Notification', 'Stop'):
            n = count_our_hooks(settings, custom, event)
            events[event] = {'registered': n, 'exactly_one': n == 1}
    except (OSError, ValueError) as exc:
        events['error'] = str(exc)
    return {'hook': hook, 'events': events, 'bin_dir': bin_dir, 'bin_dir_on_path': _on_path(bin_dir),
            'aliases': aliases, 'skills': skills}


def format_check(report: dict) -> str:
    lines = []
    hook = report['hook']
    if 'error' in hook:
        lines.append(f"hook   settings unreadable: {hook['error']}")
    else:
        n = hook['registered']
        state = 'registered once (ok)' if n == 1 else ('NOT registered' if n == 0 else f'registered {n} times (expected exactly one)')
        lines.append(f"hook   {HOOK_EVENT} {state} in {hook['settings']}")
        if n == 0:
            lines.append('       add it (preview, then --yes): twinlane install')
        elif n > 1:
            lines.append('       fix it: twinlane install --unhook --yes, then twinlane install --yes')
    for event, state in report.get('events', {}).items():
        if isinstance(state, dict):
            lines.append(f"hook   {event} registered {state['registered']} (expected exactly one)")
    lines.append(f"bin    {report['bin_dir']} " + ('is on PATH' if report['bin_dir_on_path'] else 'is NOT on PATH'))
    if not report['aliases']:
        lines.append('alias  none found (write one: twinlane install --alias NAME)')
    for name, files in report['aliases'].items():
        for fname, info in files.items():
            state = {'same': 'ok', 'missing': 'missing', 'ours': 'out of date', 'foreign': 'foreign file',
                     'dir': 'is a folder'}.get(info['state'], info['state'])
            lines.append(f"alias  {fname:14} {state:14} {info['path']}")
    for folder, info in report['skills'].items():
        state = 'ok' if info['current'] else ('out of date' if info['exists'] else 'missing')
        lines.append(f"skill  {state:14} {info['path']}")
    return '\n'.join(lines)


# ---------------------------------------------------------------- planning (nothing is written here)

def plan_hook(path: Path, custom: str | None, remove: bool, event=HOOK_EVENT) -> dict:
    settings = load_settings(path)
    proposed = copy.deepcopy(settings)
    shown = command(custom)
    if remove:
        changed = remove_hook(proposed, custom, event) > 0
        plan = {'kind': 'unhook', 'target': str(path), 'changed': changed,
                'would': f'Would remove the {event} hook from {path}',
                'doing': f'Removing the {event} hook from {path}',
                'noop': f'Hook not registered in {path}; nothing changed.'}
    else:
        existing = count_our_hooks(settings, custom, event)
        changed = add_hook(proposed, custom, event)
        plan = {'kind': 'hook', 'target': str(path), 'changed': changed,
                'would': f'Would add {event} hook to {path}',
                'doing': f'Adding {event} hook to {path}',
                'detail': 'Command: ' + shown,
                'noop': 'Hook already registered; no write needed.' if existing == 1 else
                        f'Hook already registered {existing} times; run `twinlane install --unhook --yes` '
                        'then install again to leave exactly one.'}

    def apply() -> tuple:
        messages = []
        path.parent.mkdir(parents=True, exist_ok=True)
        saved = backup_file(path) if path.exists() else None
        if saved:
            messages.append('Backup: ' + saved)
        current = load_settings(path)
        if remove:
            remove_hook(current, custom, event)
        else:
            add_hook(current, custom, event)
        common.write_json(str(path), current)
        n = count_our_hooks(load_settings(path), custom, event)
        if remove:
            if n:
                return common.EXIT_FAILED, messages, 'verification failed: hook still present', saved
            messages.append('Verified no twinlane hook remains.')
        else:
            if n != 1:
                return common.EXIT_FAILED, messages, 'verification failed', saved
            messages.append('Verified one hook.')
        return common.EXIT_OK, messages, None, saved

    plan['diff'] = ''.join(difflib.unified_diff(
        json.dumps(settings, ensure_ascii=False, indent=2).splitlines(True),
        json.dumps(proposed, ensure_ascii=False, indent=2).splitlines(True),
        fromfile=str(path), tofile=str(path) + ' (proposed)'))
    plan['event'] = event
    plan['apply'] = apply
    return plan


def plan_aliases(names, bin_dir: str, replace: bool) -> list:
    py, cli = shim_target()
    plans = []
    for name in names:
        for path, desired, mode in alias_files(bin_dir, name, py, cli):
            state = classify_shim(path, desired)
            if state == 'dir':
                raise ValueError(f'{path} is a folder; refusing to replace it')
            if state == 'foreign' and not replace:
                raise ValueError(f'{path} exists and is not a twinlane shim; refusing to overwrite it '
                                 '(use --replace to back it up and replace it)')
            changed = state != 'same'
            note = {'missing': 'new', 'same': 'up to date', 'ours': 'update (backup first)',
                    'foreign': 'REPLACE a foreign file (backup first)'}[state]

            def apply(path=path, desired=desired, mode=mode, state=state):
                messages = []
                saved = backup_file(path) if state in ('ours', 'foreign') else None
                if saved:
                    messages.append('Backup: ' + saved)
                write_bytes(path, desired, mode)
                with open(path, 'rb') as handle:
                    if handle.read() != desired:
                        return common.EXIT_FAILED, messages, f'verification failed: {path}', saved
                return common.EXIT_OK, messages, None, saved

            plans.append({'kind': 'alias', 'target': path, 'changed': changed,
                          'would': f'Would write {path} ({note})', 'doing': f'Writing {path} ({note})',
                          'noop': f'{path} is up to date; no write needed.', 'apply': apply})
    return plans


def plan_skill(folder: str) -> dict:
    folder = os.path.abspath(os.path.expanduser(folder))
    target = os.path.join(folder, 'SKILL.md')
    try:
        with open(SKILL_SOURCE, 'rb') as handle:
            source = handle.read()
    except OSError as exc:
        raise ValueError(f'the packaged skill file is missing: {exc}')
    if os.path.isfile(folder):
        raise ValueError(f'{folder} is a file, not a folder')
    if os.path.isdir(target):
        raise ValueError(f'{target} is a folder; refusing to replace it')
    state = 'missing'
    if os.path.isfile(target):
        with open(target, 'rb') as handle:
            state = 'same' if handle.read() == source else 'differs'
    note = {'missing': 'new', 'same': 'up to date', 'differs': 'replace (backup first)'}[state]

    def apply() -> tuple:
        messages = []
        saved = backup_file(target) if state == 'differs' else None
        if saved:
            messages.append('Backup: ' + saved)
        write_bytes(target, source)
        with open(target, 'rb') as handle:
            if handle.read() != source:
                return common.EXIT_FAILED, messages, f'verification failed: {target}', saved
        return common.EXIT_OK, messages, None, saved

    return {'kind': 'skill', 'target': target, 'changed': state != 'same',
            'would': f'Would write {target} ({note})', 'doing': f'Writing {target} ({note})',
            'noop': f'{target} is up to date; no write needed.', 'apply': apply}


# ---------------------------------------------------------------- cli

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog='twinlane install', description=__doc__.splitlines()[0])
    p.add_argument('--settings', default=None, help='Claude settings.json to inspect or edit')
    p.add_argument('--yes', action='store_true', help='apply the previewed changes')
    p.add_argument('--json', action='store_true')
    p.add_argument('--check', action='store_true', help='read-only report; changes nothing')
    p.add_argument('--hook', action='store_true', help='add the hook (the default when no other action is given)')
    p.add_argument('--unhook', action='store_true', help='remove our hook entry')
    p.add_argument('--lane-state', action='store_true', help='preview Notification/Stop hooks and the Codex notify line')
    p.add_argument('--precompact', action='store_true', help='preview the session-bound PreCompact checkpoint hook')
    p.add_argument('--hook-command', metavar='CMD', help='custom hook command to register or recognise')
    p.add_argument('--alias', action='append', metavar='NAME[,NAME]', help='write command shims called NAME')
    p.add_argument('--bin-dir', default=None, help='folder for the shims (default ~/.local/bin)')
    p.add_argument('--replace', action='store_true', help='back up and replace a shim twinlane did not write')
    p.add_argument('--skill', action='append', metavar='DIR', help='write the packaged SKILL.md into DIR')
    return p


def main(argv=None) -> int:
    p = build_parser()
    a = p.parse_args(argv)
    if a.hook and a.unhook:
        p.error('--hook and --unhook cannot be combined')
    if a.check and (a.hook or a.unhook or a.yes or a.replace or a.precompact or a.lane_state):
        p.error('--check is read-only and cannot be combined with --hook, --unhook, --replace or --yes')
    if a.replace and not a.alias:
        p.error('--replace needs --alias')
    do_hook = a.hook or (a.unhook and not a.precompact) or not (a.alias or a.skill or a.check or a.precompact or a.lane_state)
    if a.hook and a.precompact:
        p.error('preview/install each hook separately: --hook or --precompact')
    if a.hook_command and not (do_hook or a.check or a.precompact or a.lane_state):
        p.error('--hook-command needs --hook, --unhook, --check or the default hook action')
    path = Path(a.settings or default_settings_path())
    bin_dir = os.path.abspath(os.path.expanduser(a.bin_dir)) if a.bin_dir else default_bin_dir()
    try:
        names = parse_alias_names(a.alias)
        if a.hook_command:
            command(a.hook_command)  # validate before anything is planned
        if a.check:
            skill_dirs = [os.path.abspath(os.path.expanduser(d)) for d in a.skill] if a.skill else None
            report = check(str(path), a.hook_command, bin_dir, names, skill_dirs)
            common.emit(report, a.json, format_check(report))
            return common.EXIT_OK
        plans = []
        if do_hook:
            plans.append(plan_hook(path, a.hook_command, a.unhook))
        if a.precompact:
            plans.append(plan_hook(path, a.hook_command, a.unhook, 'PreCompact'))
        if a.lane_state:
            plans.extend(plan_hook(path, a.hook_command, a.unhook, event) for event in ('Notification', 'Stop'))
        if names:
            plans.extend(plan_aliases(names, bin_dir, a.replace))
        for folder in a.skill or []:
            plans.append(plan_skill(folder))
    except (OSError, ValueError) as exc:
        print('twinlane install: ' + str(exc), file=sys.stderr)
        return common.EXIT_REFUSED

    say = (lambda *_args: None) if a.json else print
    results = []
    for plan in plans:
        say(plan['doing'] if a.yes and plan['changed'] else plan['would'])
        if plan.get('detail'):
            say(plan['detail'])
        if plan.get('diff'):
            say(baton.redact(plan['diff']))
        if not plan['changed']:
            say(plan['noop'])
        results.append({'kind': plan['kind'], 'target': plan['target'], 'changed': plan['changed'],
                        'applied': False, 'backup': None,
                        **({'event': plan['event'], 'diff': baton.redact(plan['diff'])} if 'event' in plan else {})})
    pending = [(plan, res) for plan, res in zip(plans, results) if plan['changed']]
    if names and not _on_path(bin_dir):
        say(f'Note: {bin_dir} is not on PATH.')
    if pending and not a.yes:
        say('Run with --yes to apply.')
    rc = common.EXIT_OK
    if pending and a.yes:
        for plan, res in pending:
            try:
                code, messages, error, saved = plan['apply']()
            except (OSError, ValueError) as exc:
                code, messages, error, saved = common.EXIT_FAILED, [], str(exc), None
            for line in messages:
                say(line)
            res['backup'] = saved
            if error:
                print('twinlane install: ' + error, file=sys.stderr)
                rc = code
                break
            res['applied'] = True
    notify = [sys.executable, common.TWINLANE_PY, 'hook', '--codex-notify'] if a.lane_state else None
    if notify:
        say('Codex config (add manually; never edited here): notify = ' + json.dumps(notify))
    if a.json:
        common.emit({'yes': a.yes, 'actions': results, **({'codex_notify': notify} if notify else {})}, True)
    return rc


if __name__ == '__main__':
    sys.exit(main())
