"""Keep every imported test module and child process in a disposable home."""
import atexit
import os
import shutil
import tempfile


_HOME = tempfile.mkdtemp(prefix='twinlane-tests-')
atexit.register(shutil.rmtree, _HOME, ignore_errors=True)
os.environ['TWINLANE_HOME'] = _HOME
os.environ['HOME'] = _HOME
os.environ['USERPROFILE'] = _HOME
os.environ['XDG_DATA_HOME'] = _HOME
os.environ['LOCALAPPDATA'] = _HOME
assert os.environ['TWINLANE_HOME'] == _HOME
