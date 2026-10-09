"""Register twinlane's advisory Claude Code hook in a chosen settings file."""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
from pathlib import Path

from . import common

HOOK_EVENT = 'UserPromptSubmit'
HOOK_PATTERN = re.compile(r'\btwinlane(?:[\\/]cli\.py)?\s+hook\b', re.I)


def command() -> str:
    return 'twinlane hook || exit 0'


def default_settings_path() -> str:
    return os.environ.get('TWINLANE_SETTINGS_PATH') or os.path.join(common.CLAUDE_DIR, 'settings.json')


def is_our_hook(item) -> bool:
    return isinstance(item, dict) and bool(HOOK_PATTERN.search(str(item.get('command', ''))))


def count_our_hooks(settings: dict) -> int:
    groups = (settings.get('hooks') or {}).get(HOOK_EVENT) or []
    return sum(is_our_hook(h) for g in groups if isinstance(g, dict)
               for h in (g.get('hooks') or []))


def add_hook(settings: dict) -> bool:
    if count_our_hooks(settings):
        return False
    hooks = settings.setdefault('hooks', {})
    if not isinstance(hooks, dict):
        raise ValueError('hooks must be an object')
    groups = hooks.setdefault(HOOK_EVENT, [])
    if not isinstance(groups, list):
        raise ValueError('UserPromptSubmit must be a list')
    groups.append({'hooks': [{'type': 'command', 'command': command(), 'timeout': 5}]})
    return True


def load_settings(path: Path) -> dict:
    if not path.exists():
        return {}
    value = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(value, dict):
        raise ValueError('settings must be an object')
    return value


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog='twinlane install')
    p.add_argument('--settings', default=default_settings_path())
    p.add_argument('--yes', action='store_true')
    a = p.parse_args(argv)
    path = Path(a.settings)
    try:
        settings = load_settings(path)
        proposed = json.loads(json.dumps(settings))
        changed = add_hook(proposed)
    except (OSError, ValueError) as exc:
        print('twinlane install: ' + str(exc), file=sys.stderr)
        return common.EXIT_REFUSED
    print(('Would add' if not a.yes else 'Adding') + ' UserPromptSubmit hook to ' + str(path))
    print('Command: ' + command())
    if not changed:
        print('Hook already registered; no write needed.')
        return 0
    if not a.yes:
        print('Run with --yes to apply.')
        return 0
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        backup = path.with_name(path.name + '.twinlane-backup-' + common.stamp())
        shutil.copy2(path, backup)
        print('Backup: ' + str(backup))
    common.write_json(str(path), proposed)
    if count_our_hooks(load_settings(path)) != 1:
        print('twinlane install: verification failed', file=sys.stderr)
        return common.EXIT_FAILED
    print('Verified one hook.')
    return 0
