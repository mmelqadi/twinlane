"""Fail if public source carries an author's private identifiers."""
import os
import re
import subprocess
import unittest
from pathlib import Path


def _extra_terms():
    # Maintainers can add their own private words (one per line) without publishing them.
    path = os.environ.get('TWINLANE_PRIVACY_TERMS')
    if not path:
        return ()
    with open(path, encoding='utf-8') as handle:
        return tuple(line.strip().lower() for line in handle if line.strip() and not line.startswith('#'))


PRIVATE_TERMS = _extra_terms()
EMAIL = re.compile(r'\b[A-Z0-9._%+-]+@([A-Z0-9.-]+\.[A-Z]{2,})\b', re.I)
WIN_SLASH = re.compile('C' + r':/Users/(?!alice(?:/|\b))', re.I)
POSIX_HOME = re.compile(r'/(?:home|Users)/(?!alice(?:/|\b))[^/\s]+/', re.I)


def private_path(line):
    normalized = re.sub(r'\\(?:x43|u0043)', 'C', line, flags=re.I)
    normalized = re.sub(r'\\(?:x5c|u005c)', '/', normalized, flags=re.I)
    normalized = re.sub(r'\\+', '/', normalized)
    return bool(WIN_SLASH.search(normalized) or POSIX_HOME.search(normalized))


class Privacy(unittest.TestCase):
    def test_public_files(self):
        root = Path(__file__).resolve().parents[1]
        proc = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard', '-z'],
                              cwd=root, capture_output=True, check=False)
        if proc.returncode == 0:
            names = [name for name in proc.stdout.decode('utf-8').split('\0') if name]
        else:
            names = [str(path.relative_to(root)) for path in root.rglob('*')
                     if path.is_file() and not any(part in ('.git', '__pycache__') for part in path.parts)]
        failures = []
        for name in names:
            if name.replace('\\', '/').endswith('tests/test_no_private_strings.py'):
                continue
            path = root / name
            if not path.is_file():
                continue
            try:
                value = path.read_text(encoding='utf-8')
            except UnicodeError:
                continue
            for number, line in enumerate(value.splitlines(), 1):
                low = line.lower()
                if any(term in low for term in PRIVATE_TERMS) or private_path(line):
                    failures.append('{}:{}'.format(name, number))
                for match in EMAIL.finditer(line):
                    address = match.group(0).lower()
                    if address.startswith('noreply@') or match.group(1).lower() in ('example.com', 'example.org'):
                        continue
                    failures.append('{}:{}'.format(name, number))
        self.assertEqual(failures, [], 'private identifiers at ' + ', '.join(failures))

    def test_private_path_forms(self):
        for value in ('/' + 'home/bob/project/', '/' + 'Users/bob/project/',
                      r'\x43:' + '/' + 'Users/bob/project/',
                      r'\u0043:' + '/' + 'Users/bob/project/'):
            self.assertTrue(private_path(value), value)
        for value in ('/' + 'home/alice/project/', '/' + 'Users/alice/project/',
                      'C:' + '/' + 'Users/alice/project/'):
            self.assertFalse(private_path(value), value)


if __name__ == '__main__':
    unittest.main()
