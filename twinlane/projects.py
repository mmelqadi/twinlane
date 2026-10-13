"""Per-project configuration for land, ship, wt and best-of.

A project is a git repository, identified by its MAIN checkout (the first entry of
`git worktree list`), so every linked worktree of one repository resolves to the same project.
Settings come only from the user config's "projects" map, keyed by that checkout's canonical
path. They are never read from repository content, so a worker running inside a checkout cannot
rewrite the gate or the ship command that judges its own work.

Commands are argv lists. A string is refused: nothing here goes through a shell. Placeholders
in an element are replaced by exact token, in one pass, and an unknown placeholder is an error.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import sys

from twinlane import common

PLACEHOLDERS = ('repo', 'worktree', 'branch', 'slug', 'target', 'manifest', 'source_commit', 'base')

# Keys a project may set, per section. Anything else is refused: a typo in a gate or a ship
# command must not silently leave the step unconfigured.
PROJECT_KEYS = {
    'land': ('target', 'gate', 'command', 'command_rebases', 'heartbeat_s', 'heartbeat_stale_s',
             'gate_timeout_s'),
    'ship': ('dry_run_command', 'command', 'dry_run_max_age_s'),
    'wt': ('deps_dirs', 'root', 'setup_command', 'remove_command', 'link_roots', 'stale_days',
           'prune_min_age_s'),
}
TOP_KEYS = tuple(PROJECT_KEYS) + ('test',)
COMMAND_KEYS = {'land': ('gate', 'command'), 'ship': ('dry_run_command', 'command'),
                'wt': ('setup_command', 'remove_command')}
LIST_KEYS = {'wt': ('deps_dirs', 'link_roots')}
# (section, key) -> smallest allowed value; the number must be finite and not a bool.
NUMBER_KEYS = {
    ('land', 'heartbeat_s'): 1, ('land', 'heartbeat_stale_s'): 1, ('land', 'gate_timeout_s'): 1,
    ('ship', 'dry_run_max_age_s'): 1, ('wt', 'stale_days'): 0, ('wt', 'prune_min_age_s'): 0,
    ('best_of', 'max_contestants'): 1, ('resume', 'max_batch'): 1,
}
_PLACEHOLDER = re.compile(r'\{([A-Za-z_][A-Za-z0-9_]*)\}')


class ProjectError(ValueError):
    """The project settings, or the repository they are looked up for, cannot be used."""


# ---------------------------------------------------------------- identity

def main_checkout(repo_dir: str) -> str:
    """The main checkout of the repository containing `repo_dir`: the first `git worktree list` entry."""
    rc, out = common.git(repo_dir, 'worktree', 'list', '--porcelain')
    if rc != 0:
        raise ProjectError(f'not a git repository: {repo_dir}')
    first = []
    for line in out.splitlines():
        if not line.strip():
            break
        first.append(line)
    if not first or not first[0].startswith('worktree '):
        raise ProjectError(f'cannot read the worktree list of {repo_dir}')
    path = first[0][len('worktree '):]
    if path.startswith('"'):
        raise ProjectError(f'the main checkout path of {repo_dir} needs quoting; unsupported')
    if any(line.strip() == 'bare' for line in first):
        raise ProjectError(f'{repo_dir} belongs to a bare repository; there is no main checkout')
    main_root = common.real_path(path)
    _confirm_membership(main_root, repo_dir)
    return main_root


def _confirm_membership(main_root: str, repo_dir: str) -> None:
    """Ask the main checkout itself, not the worktree, which worktrees it owns.

    A linked worktree's `.git` file is writable by whoever works in that worktree, so the first
    `git worktree list` entry it reports could name another repository and pick that project's
    commands. The main checkout must have a real .git directory and must list `repo_dir` (or a
    worktree containing it) before its settings are used."""
    if not os.path.isdir(os.path.join(main_root, '.git')):
        raise ProjectError(f'the main checkout {main_root} has no .git directory; refusing')
    rc, out = common.git(main_root, 'worktree', 'list', '--porcelain')
    if rc != 0:
        raise ProjectError(f'cannot read the worktree list of {main_root}')
    want = common.canon_path(common.real_path(repo_dir))
    for line in out.splitlines():
        if not line.startswith('worktree '):
            continue
        root = common.canon_path(common.real_path(line[len('worktree '):])).rstrip('/\\')
        if want == root or want.startswith(root + '/') or want.startswith(root + '\\'):
            return
    raise ProjectError(f'{repo_dir} is not a worktree the repository at {main_root} lists; refusing')


def repo_key(main_root: str) -> str:
    """Stable 16-hex id of a project, from its canonical main-checkout path."""
    return hashlib.sha256(common.canon_path(main_root).encode('utf-8')).hexdigest()[:16]


def state_dir(feature: str, main_root: str, create: bool = False) -> str:
    """STATE/<feature>/<repo_key>/, the home of a feature's queue, ledger and logs for one project."""
    path = os.path.join(common.STATE, feature, repo_key(main_root))
    if create:
        os.makedirs(path, exist_ok=True)
    return path


# ---------------------------------------------------------------- worker refusal

def in_worker() -> bool:
    """True inside a twinlane worker job."""
    return bool(os.environ.get('TWINLANE_ENGINE_JOB'))


def refuse_in_worker(command: str) -> int | None:
    """None when allowed. Inside a worker: print why and return the refusal exit code."""
    if not in_worker():
        return None
    print(f'twinlane {command}: refused inside a twinlane worker job; run it from the lead session',
          file=sys.stderr)
    return common.EXIT_REFUSED


# ---------------------------------------------------------------- commands

def _tokens(element: str):
    return [m.group(1) for m in _PLACEHOLDER.finditer(element)]


def check_command(value, name: str) -> list:
    """A non-empty list of non-empty strings with only known placeholders, or ProjectError."""
    if isinstance(value, str):
        raise ProjectError(f'{name} must be an argv list, not a string (nothing runs through a shell)')
    if not isinstance(value, (list, tuple)) or not value:
        raise ProjectError(f'{name} must be a non-empty argv list')
    for element in value:
        if not isinstance(element, str) or not element:
            raise ProjectError(f'{name} must hold only non-empty strings')
        for token in _tokens(element):
            if token not in PLACEHOLDERS:
                raise ProjectError(f'{name}: unknown placeholder {{{token}}}; known: '
                                   + ', '.join('{' + p + '}' for p in PLACEHOLDERS))
    return list(value)


def wrap_script(argv: list) -> list:
    """On Windows a .ps1 cannot be started directly: name it through powershell."""
    if common.IS_WIN and argv and argv[0].lower().endswith('.ps1'):
        return [common._powershell(), '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File'] + list(argv)
    return list(argv)


def argv(template, **values) -> list:
    """Expand a command template into the argv to run.

    Each element has its `{name}` tokens replaced by the matching value, in one pass, so a value
    that itself contains braces is never expanded again. A token without a value, an unknown
    token, or a value for an unknown name is a ProjectError. Never str.format, never a shell."""
    check_command(template, 'command')
    for name in values:
        if name not in PLACEHOLDERS:
            raise ProjectError(f'unknown placeholder value {name!r}')

    def fill(match):
        name = match.group(1)
        value = values.get(name)
        if value is None:
            raise ProjectError(f'placeholder {{{name}}} has no value here')
        return str(value)

    for name, value in values.items():
        if any(c in str(value) for c in '\r\n\x00'):
            raise ProjectError(f'placeholder {{{name}}} holds a line break or NUL; refusing')
    filled = [_PLACEHOLDER.sub(fill, element) for element in template]
    if _runs_through_cmd(filled[0]):
        for name, value in values.items():
            if _CMD_UNSAFE.search(str(value)):
                raise ProjectError(f'placeholder {{{name}}} holds a character cmd.exe would interpret '
                                   f'({value!r}); a .bat/.cmd command only receives plain values')
    return wrap_script(filled)


# cmd.exe re-parses the arguments of a .bat/.cmd file (the CVE-2024-24576 class), so values
# passed to one are limited to characters it treats literally.
_CMD_UNSAFE = re.compile(r'[^A-Za-z0-9 _.:/\\@+=,-]')


def _runs_through_cmd(program: str) -> bool:
    """True on Windows when `program` is, or resolves on PATH to, a .bat or .cmd file."""
    if not common.IS_WIN:
        return False
    found = shutil.which(program) or program
    return found.lower().endswith(('.bat', '.cmd'))


# ---------------------------------------------------------------- resolution

def _is_number(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and value == value \
        and value not in (float('inf'), float('-inf'))


def _check_section(name: str, section, allowed=None) -> dict:
    if not isinstance(section, dict):
        raise ProjectError(f'projects: "{name}" must be an object')
    if allowed is not None:
        unknown = sorted(set(section) - set(allowed))
        if unknown:
            raise ProjectError(f'projects: unknown {name} key(s) {", ".join(unknown)}; known: '
                               + ', '.join(allowed))
    return section


def _validate(cfg: dict) -> None:
    for section, keys in COMMAND_KEYS.items():
        for key in keys:
            if cfg[section].get(key) is not None:
                check_command(cfg[section][key], f'{section}.{key}')
    if cfg['test'] is not None:
        check_command(cfg['test'], 'test')
    for section, keys in LIST_KEYS.items():
        for key in keys:
            value = cfg[section].get(key)
            if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
                raise ProjectError(f'{section}.{key} must be a list of non-empty strings')
    for item in cfg['wt']['deps_dirs']:
        parts = re.split(r'[\\/]+', item)
        if os.path.isabs(item) or re.match(r'^[A-Za-z]:', item) or '..' in parts:
            raise ProjectError(f'wt.deps_dirs entries must be relative paths inside the worktree: {item!r}')
    for (section, key), low in NUMBER_KEYS.items():
        value = cfg[section].get(key)
        if not _is_number(value) or value < low:
            raise ProjectError(f'{section}.{key} must be a number >= {low}')
    land = cfg['land']
    if land.get('target') is not None and (not isinstance(land['target'], str) or not land['target'].strip()):
        raise ProjectError('land.target must be a branch name')
    if not isinstance(land.get('command_rebases'), bool):
        raise ProjectError('land.command_rebases must be true or false')
    if cfg['wt'].get('root') is not None and not (isinstance(cfg['wt']['root'], str) and cfg['wt']['root'].strip()):
        raise ProjectError('wt.root must be a directory path')


def find_project(projects, main_root: str):
    """The settings for `main_root` from a "projects" map, or None. Keys compare by canonical path."""
    if not isinstance(projects, dict):
        raise ProjectError('config "projects" must be an object keyed by repository path')
    want = common.canon_path(main_root)
    found = [(key, value) for key, value in projects.items()
             if isinstance(key, str) and common.canon_path(key) == want]
    if len(found) > 1:
        raise ProjectError('config "projects" lists the same repository twice: '
                           + ', '.join(key for key, _ in found))
    return found[0][1] if found else None


def resolve(repo_dir: str | None = None, policy: dict | None = None) -> tuple[str, dict]:
    """(main_root, cfg) for the repository containing `repo_dir` (default: the current directory).

    cfg holds land, ship, wt, best_of and resume (packaged defaults, then the user config, then
    this project's entry), `test` (an argv or None), `key` (repo_key) and `configured` (whether
    the user config has an entry for this repository). Raises ProjectError when the repository
    cannot be found or the settings are invalid; nothing is ever read from the repository itself."""
    main_root = main_checkout(repo_dir or os.getcwd())
    policy = common.load_policy() if policy is None else policy
    entry = find_project(policy.get('projects', {}), main_root)
    configured = entry is not None
    entry = _check_section('project entry', entry if configured else {}, TOP_KEYS)
    cfg = {'key': repo_key(main_root), 'configured': configured}
    for section in ('land', 'ship', 'wt', 'best_of', 'resume'):
        base = policy.get(section, {})
        if not isinstance(base, dict):
            raise ProjectError(f'config "{section}" must be an object')
        if section in PROJECT_KEYS:
            own = _check_section(section, entry.get(section, {}), PROJECT_KEYS[section])
            merged = common._merge_config(base, own)
            for key in PROJECT_KEYS[section]:
                merged.setdefault(key, False if key == 'command_rebases' else None)
            cfg[section] = merged
        else:
            cfg[section] = dict(base)
    cfg['test'] = entry.get('test')
    _validate(cfg)
    return main_root, cfg
