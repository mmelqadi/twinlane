"""Throwaway git repositories, linked worktrees and directory links for tests.

Everything lives under one temporary directory that `Workspace.cleanup` removes. A directory
link (a junction on Windows, a symlink elsewhere) is unlinked WITHOUT following it before the tree
is deleted: shutil.rmtree can descend into a junction on some Python versions and delete its target.
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time

IS_WIN = os.name == 'nt'
REPARSE_POINT = getattr(stat, 'FILE_ATTRIBUTE_REPARSE_POINT', 0x400)
IDENTITY = ['-c', 'user.name=Fixture', '-c', 'user.email=fixture@example.org', '-c', 'commit.gpgsign=false',
            '-c', 'core.autocrlf=false', '-c', 'init.defaultBranch=main']


def git_available():
    return shutil.which('git') is not None


def git(cwd, *args, check=True):
    """Run git in `cwd`; returns stdout stripped. Raises AssertionError with git's stderr when check."""
    proc = subprocess.run(['git', *IDENTITY, '-C', cwd, *args], capture_output=True, text=True,
                          encoding='utf-8', errors='replace')
    if check and proc.returncode != 0:
        raise AssertionError('git {} failed in {}: {}'.format(' '.join(args), cwd, proc.stderr.strip()))
    return proc.stdout.strip() if check else (proc.returncode, proc.stdout.strip(), proc.stderr.strip())


def is_link(path):
    """True for a symlink or a Windows reparse point (junction), without following it."""
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, 'st_file_attributes', 0) & REPARSE_POINT)


def make_link(link, target):
    """Make directory `link` point at the existing directory `target` (junction on Windows)."""
    if IS_WIN:
        import _winapi
        _winapi.CreateJunction(os.path.abspath(target), os.path.abspath(link))
    else:
        os.symlink(os.path.abspath(target), link, target_is_directory=True)


def remove_link(path):
    """Remove a link itself; never touches what it points at."""
    if IS_WIN and os.path.isdir(path):
        os.rmdir(path)  # a directory link or junction: removes the link only
    else:
        os.unlink(path)


def unlink_links_under(root):
    """Remove every link inside `root` (scandir, no follow). Returns how many were removed."""
    removed = 0
    stack = [root]
    while stack:
        here = stack.pop()
        try:
            entries = list(os.scandir(here))
        except OSError:
            continue
        for entry in entries:
            if is_link(entry.path):
                try:
                    remove_link(entry.path)
                    removed += 1
                except OSError:
                    pass
            elif entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
    return removed


def _force_remove(function, path, _info):
    os.chmod(path, stat.S_IWRITE)  # git marks object files read-only; Windows refuses to delete them
    function(path)


def rmtree(path):
    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=_force_remove)
    else:
        shutil.rmtree(path, onerror=_force_remove)


class Workspace:
    """A temporary directory with helpers to build repositories, worktrees and links in it.

        ws = Workspace()
        test.addCleanup(ws.cleanup)
        repo = ws.repo('app')                       # a repo with one commit on branch `main`
        wt = ws.worktree(repo, 'feature-x')         # <ws>/app-wt/feature-x on branch feature-x
        link = ws.link('app-link', repo)            # directory link to repo
    """

    def __init__(self, prefix='twinlane-git-'):
        self.root = os.path.realpath(tempfile.mkdtemp(prefix=prefix))

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    def repo(self, name='repo', files=None):
        """A new repository `name` on branch main with one commit (README plus `files` {path: text})."""
        path = self.path(name)
        os.makedirs(path)
        git(path, 'init', '-q')
        git(path, 'symbolic-ref', 'HEAD', 'refs/heads/main')
        files = dict({'README.md': 'fixture\n'}, **(files or {}))
        for rel, text in files.items():
            self.write(path, rel, text)
        git(path, 'add', '-A')
        git(path, 'commit', '-q', '-m', 'initial')
        return path

    def write(self, repo, rel, text):
        full = os.path.join(repo, *rel.split('/'))
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8', newline='') as handle:
            handle.write(text)
        return full

    def commit(self, repo, rel, text, message=None):
        """Write `rel`, commit it, return the new commit id."""
        self.write(repo, rel, text)
        git(repo, 'add', '--', rel)
        git(repo, 'commit', '-q', '-m', message or 'update ' + rel)
        return git(repo, 'rev-parse', 'HEAD')

    def worktree(self, repo, branch, base=None, parent=None):
        """A linked worktree for a NEW branch, under <ws>/<repo>-wt/<branch> (or `parent`)."""
        parent = parent or self.path(os.path.basename(repo) + '-wt')
        os.makedirs(parent, exist_ok=True)
        path = os.path.join(parent, branch.replace('/', '-'))
        args = ['worktree', 'add', '-q', '-b', branch, path]
        if base:
            args.append(base)
        git(repo, *args)
        return os.path.realpath(path)

    def link(self, name, target):
        """A directory link `<ws>/<name>` pointing at `target`; returns the link path."""
        path = self.path(name)
        make_link(path, target)
        return path

    def cleanup(self):
        unlink_links_under(self.root)
        for _ in range(10):
            rmtree(self.root)
            if not os.path.exists(self.root):
                return
            time.sleep(0.3)


def self_check():  # pragma: no cover - a manual smoke test: python tests/git_fixtures.py
    ws = Workspace()
    try:
        repo = ws.repo()
        wt = ws.worktree(repo, 'feature')
        link = ws.link('linked', wt)
        print(repo, wt, link, is_link(link), file=sys.stderr)
    finally:
        ws.cleanup()


if __name__ == '__main__':
    self_check()
