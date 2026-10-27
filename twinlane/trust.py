"""Inert trust records for future completion sinks. No imports or execution of sinks."""
import argparse
import getpass
import hashlib
import json
import os
import re
import sys

from . import common, jobs

NAME = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,79}$')


def _path():
    return os.path.join(common.STATE, 'trusted-sinks.json')


def _read():
    try:
        with open(_path(), encoding='utf-8') as handle:
            data = json.load(handle)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError('invalid trust registry')
    for name, row in data.items():
        if not NAME.fullmatch(name) or not isinstance(row, dict) or row.get('name') != name \
                or not isinstance(row.get('path'), str) or not os.path.isabs(row['path']) \
                or not isinstance(row.get('sha256'), str) or not re.fullmatch(r'[0-9a-f]{64}', row['sha256']) \
                or common.parse_time(row.get('trusted_at')) is None or not isinstance(row.get('by'), str):
            raise ValueError('invalid trust registry entry')
    return data


def _sha(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(65536), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _verified(row):
    try:
        return _sha(row['path']) == row['sha256']
    except OSError:
        return False


def trusted_sink(name):
    """Return a verified path, never execute it. Consumers must address use-time races themselves."""
    try:
        row = _read().get(name)
        if row is None:
            return None
        if _verified(row):
            return row['path']
        print(f'twinlane trust: {name}: file changed or unreadable; untrusted', file=sys.stderr)
    except (OSError, ValueError, TypeError, RecursionError):
        print('twinlane trust: registry unreadable; untrusted', file=sys.stderr)
    return None


def cli(argv):
    ap = argparse.ArgumentParser(prog='twinlane trust')
    sub = ap.add_subparsers(dest='action', required=True)
    sub.add_parser('list')
    add = sub.add_parser('add')
    add.add_argument('name')
    add.add_argument('path')
    remove = sub.add_parser('remove')
    remove.add_argument('name')
    args = jobs._parse(ap, argv)
    if isinstance(args, int):
        return args
    if args.action != 'list' and not NAME.fullmatch(args.name):
        print('twinlane trust: invalid name', file=sys.stderr)
        return common.EXIT_USAGE
    try:
        if args.action == 'list':
            data = _read()
            if not data:
                print('no trusted sinks')
            for name, row in sorted(data.items()):
                state = 'trusted' if _verified(row) else 'UNTRUSTED (file changed or unreadable)'
                print(f"{name}: {state} {row['path']} sha256={row['sha256']} trusted_at={row['trusted_at']} by={row['by']}")
            return common.EXIT_OK
        with common.file_lock(os.path.join(common.STATE, 'trusted-sinks.lock')):
            data = _read()  # never replace an unreadable registry with an empty one
            if args.action == 'add':
                path = os.path.abspath(os.path.expanduser(args.path))
                data[args.name] = dict(name=args.name, path=path, sha256=_sha(path),
                                       trusted_at=common.iso(common.now()), by=getpass.getuser())
            elif data.pop(args.name, None) is None:
                print(f'{args.name}: not registered')
                return common.EXIT_OK
            common.write_json(_path(), data)
        print(f'{args.name}: ' + ('trusted' if args.action == 'add' else 'removed'))
        return common.EXIT_OK
    except (OSError, ValueError, TypeError, RecursionError, TimeoutError):
        print('twinlane trust: cannot safely read or update registry', file=sys.stderr)
        return common.EXIT_FAILED
