"""Keep every imported test module and child process in a disposable home."""
import atexit
import errno
import os
import shutil
import stat
import sys
import tempfile
import time


if 'twinlane.common' in sys.modules:
    raise RuntimeError('test_support must load before twinlane.common; context was not changed')

_HOME = tempfile.mkdtemp(prefix='twinlane-tests-')
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ['TWINLANE_HOME'] = _HOME
os.environ['TWINLANE_TEST_ISOLATION'] = '1'
os.environ['HOME'] = _HOME
os.environ['USERPROFILE'] = _HOME
os.environ['XDG_DATA_HOME'] = _HOME
os.environ['LOCALAPPDATA'] = _HOME
# Never merge the caller's private policy or read their vendor homes into tests.
# Engine-job markers remain intact below: isolation does not turn a worker into a lead.
os.environ['TWINLANE_CONFIG'] = os.path.join(_HOME, 'config.json')
os.environ['CLAUDE_CONFIG_DIR'] = os.path.join(_HOME, 'claude')
os.environ['CODEX_HOME'] = os.path.join(_HOME, 'codex')
# Child interpreters inherit the host command's -B intent. Fixture Git processes use only owned,
# inert system/global files and a fresh template; real configuration and repo hooks are untouched.
os.environ['PYTHONDONTWRITEBYTECODE'] = '1'
for _scope in ('SYSTEM', 'GLOBAL'):
    _config = os.path.join(_HOME, 'git-' + _scope.lower() + '.config')
    with open(_config, 'w', encoding='utf-8') as _handle:
        _handle.write('')
    os.environ['GIT_CONFIG_' + _scope] = _config
_template = os.path.join(_HOME, 'git-template')
os.mkdir(_template)
os.environ['GIT_TEMPLATE_DIR'] = _template
# Preserve inherited worker context. A guarded caller remains guarded; host qualification must
# establish marker absence rather than removing a marker to make dispatch behavior change.
assert os.environ['TWINLANE_HOME'] == _HOME


_TRANSIENT_WINERRORS = (5, 32, 145)  # access denied, sharing violation, directory not empty (pending delete)


def _force_remove(function, path, _exc):
    try:
        os.chmod(path, stat.S_IWRITE)  # git and some fixtures leave read-only files; Windows refuses to delete them
    except OSError:
        pass
    function(path)


def rmtree_retry(path, attempts: int = 20, delay: float = 0.05) -> None:
    """Remove a test tree, riding out the transient errors Windows raises while a just-exited child, a runner
    thread or a scanner still holds or is deleting something inside it (WinError 5, 32, 145)."""
    for attempt in range(attempts):
        if not os.path.lexists(path):
            return
        try:
            if sys.version_info >= (3, 12):
                shutil.rmtree(path, onexc=_force_remove)
            else:
                shutil.rmtree(path, onerror=_force_remove)
            return
        except FileNotFoundError:
            return
        except OSError as e:
            transient = (getattr(e, 'winerror', None) in _TRANSIENT_WINERRORS
                         or e.errno in (errno.ENOTEMPTY, errno.EACCES, errno.EBUSY))
            if not transient or attempt == attempts - 1:
                raise
            time.sleep(delay * (attempt + 1))


def cleanup_tempdir(temp: tempfile.TemporaryDirectory) -> None:
    """TemporaryDirectory.cleanup with rmtree_retry as the fallback (cleanup detaches its finalizer first)."""
    try:
        temp.cleanup()
    except OSError:
        rmtree_retry(temp.name)
