"""Experimental seat custody scaffolding; no app-server or inference is launched.

See docs/seats.md for the protocol/guard qualification hold. A reservation is a
durable process identity, not a TTL. Only DEAD permits replacement or removal.
The short OS lock serializes changes to that durable reservation.
"""
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from pathlib import Path

from . import common


HOLD = ('warm-seat execution is not qualified: per-job guard context and '
        'post-turn process custody need proof; see docs/seats.md')
PROMPT = 'Reply with exactly: twinlane seat benchmark. Do not use tools.'


class SeatError(RuntimeError):
    pass


class SeatBusy(SeatError):
    pass


def enabled(policy=None) -> bool:
    section = (common.load_policy() if policy is None else policy).get('seats')
    return isinstance(section, dict) and section.get('enabled') is True


def _number(value) -> int:
    if type(value) is not int or value < 1:
        raise ValueError('seat number/count must be a positive integer')
    return value


def _root() -> Path:
    root = Path(common.STATE) / 'seats'
    if root.is_symlink():
        raise SeatError('seat directory is a symbolic link; custody is unknown')
    return root


def _path(number: int) -> Path:
    return _root() / f'{_number(number)}.json'


def _read(path: Path) -> dict | None:
    """None means absent; unreadable/malformed/non-regular means UNKNOWN."""
    unknown = {'state': common.UNKNOWN, 'pid': None, 'pid_created': None, 'started_at': None}
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        return unknown
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        return unknown
    try:
        record = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError, UnicodeError):
        return unknown
    if not isinstance(record, dict) or not common._valid_pid(record.get('pid')) \
            or not common._valid_stamp(record.get('pid_created')) \
            or not isinstance(record.get('started_at'), str) \
            or common.parse_time(record['started_at']) is None:
        return unknown
    try:
        state = common.pid_state(record['pid'], record['pid_created'])
    except OSError:
        state = common.UNKNOWN
    if state not in (common.ALIVE, common.DEAD, common.UNKNOWN):
        state = common.UNKNOWN
    return {key: record[key] for key in ('pid', 'pid_created', 'started_at')} | {'state': state}


def status() -> list[dict]:
    """Observation only: never mkdir, lock, reclaim, start or refresh a meter."""
    root = _root()
    try:
        paths = list(root.iterdir())
    except FileNotFoundError:
        return []
    except OSError as exc:
        raise SeatError('seat directory cannot be read; custody is unknown') from exc
    rows = []
    for path in paths:
        name = path.stem
        if path.suffix != '.json' or not name.isascii() or not name.isdigit() \
                or name != str(int(name)) or int(name) < 1:
            continue
        record = _read(path)
        if record is not None:
            rows.append({'seat_id': int(name), **record})
    return sorted(rows, key=lambda row: row['seat_id'])


def claim(number: int, pid: int, pid_created: int, *, policy=None) -> dict:
    """Reserve a verified process identity (scaffolding API, not an engine start).

    A future qualified supervisor must publish all launch/descendant custody
    before using this API. This record alone cannot account for a process tree.
    Even the same live owner cannot claim twice. No age-based recovery exists.
    """
    if not enabled(policy):
        raise SeatError('seats.enabled is false')
    if not common._valid_pid(pid) or not common._valid_stamp(pid_created):
        raise SeatError('seat owner needs a valid pid and creation stamp')
    path = _path(number)
    try:
        with common.file_lock(str(path) + '.lock', timeout_s=0):
            current = _read(path)
            if current is not None and current['state'] != common.DEAD:
                raise SeatBusy(f'seat {number} is {current["state"]}; reservation retained')
            if common.pid_state(pid, pid_created) != common.ALIVE:
                raise SeatError('new seat owner identity is not ALIVE; reservation unchanged')
            record = {'pid': pid, 'pid_created': pid_created, 'started_at': common.iso(common.now())}
            common.write_json(str(path), record)
            return record
    except TimeoutError as exc:
        raise SeatBusy(f'seat {number} custody lock is busy') from exc


def release_dead(number: int) -> bool:
    """Recheck under the same lock; UNKNOWN never frees a reservation."""
    path = _path(number)
    if not path.parent.exists():
        return False
    try:
        with common.file_lock(str(path) + '.lock', timeout_s=0, create_parent=False):
            current = _read(path)
            if current is None or current['state'] != common.DEAD:
                return False
            path.unlink()
            return True
    except TimeoutError as exc:
        raise SeatBusy(f'seat {number} custody lock is busy') from exc


def bench_plan(count: int, *, policy=None) -> dict:
    """Prospective plan only. Null measurements must never look like zero failures."""
    _number(count)
    return {'dry_run': True, 'enabled': enabled(policy), 'runtime_ready': False, 'hold': HOLD,
            'n': count, 'prompt': PROMPT, 'kind': 'review', 'sandbox': 'read-only',
            'prospective_model_calls': 2 * count, 'cold_runs': count, 'seat_runs': count,
            'fresh_thread_per_job': True, 'fallback': False, 'measurements': None,
            'metrics': ['start_latency_s', 'failure_count'],
            'writes': [], 'launches': 0}


def _positive(text: str) -> int:
    try:
        return _number(int(text))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cli(argv) -> int:
    parser = argparse.ArgumentParser(prog='twinlane seats', description=HOLD)
    subs = parser.add_subparsers(dest='command', required=True)
    observe = subs.add_parser('status', help='read reservation identities; no engine health claim')
    observe.add_argument('--json', action='store_true')
    start = subs.add_parser('start', help='refused until execution is qualified')
    start.add_argument('n', type=_positive)
    start.add_argument('--json', action='store_true')
    stop = subs.add_parser('stop', help='scaffolding: reap DEAD reservations only; never kill a process')
    stop.add_argument('--json', action='store_true')
    stop.add_argument('--dry-run', action='store_true')
    bench = subs.add_parser('bench', help='print a plan; execution is not qualified')
    bench.add_argument('--n', type=_positive, default=1)
    bench.add_argument('--dry-run', action='store_true')
    bench.add_argument('--json', action='store_true')
    try:
        args = parser.parse_args(list(argv))
    except SystemExit as exc:
        return common.EXIT_USAGE if exc.code else common.EXIT_OK
    try:
        policy = common.load_policy()
        if args.command == 'bench' and args.dry_run:
            common.emit(bench_plan(args.n, policy=policy), True)
            return common.EXIT_OK
        if args.command in ('start', 'bench'):
            why = HOLD if enabled(policy) else 'seats.enabled is false; ' + HOLD
            common.emit({'status': 'refused', 'error': why, 'runtime_ready': False}, args.json, why)
            return common.EXIT_REFUSED
        rows = status()
        if args.command == 'status':
            data = {'enabled': enabled(policy), 'runtime_ready': False, 'hold': HOLD, 'seats': rows}
            lines = [f'seats: enabled={data["enabled"]}; reservation scaffolding only; {HOLD}']
            lines += [f'seat {r["seat_id"]}: {r["state"]} pid={r["pid"]} created={r["pid_created"]}'
                      for r in rows]
            common.emit(data, args.json, '\n'.join(lines))
            return common.EXIT_OK
        reaped, retained = [], []
        for row in rows:
            number = row['seat_id']
            if args.dry_run:
                (reaped if row['state'] == common.DEAD else retained).append(number)
            elif release_dead(number):
                reaped.append(number)
            else:
                retained.append(number)
        data = {'dry_run': args.dry_run, 'reaped' if not args.dry_run else 'would_reap': reaped,
                'retained': retained, 'processes_killed': 0}
        common.emit(data, args.json, f'seats: DEAD-only cleanup {data}; no processes killed')
        return common.EXIT_RUNNING if retained else common.EXIT_OK
    except (SeatError, OSError) as exc:
        common.emit({'status': 'unknown', 'error': str(exc)}, args.json,
                    f'twinlane seats: {exc}')
        return common.EXIT_FAILED


if __name__ == '__main__':
    sys.exit(cli(sys.argv[1:]))
