"""Explicit read stamps; never repair jobs, dismiss inbox notices or release custody."""
import argparse
from contextlib import nullcontext
import fnmatch
import math
import os

from . import baton, common, jobs


def _record(jid):
    receipt = common.read_json(jobs._p(jid, 'result.json'))
    if not isinstance(receipt, dict) or receipt.get('id') != jid \
            or not isinstance(receipt.get('status'), str) or receipt['status'] not in jobs.TERMINAL:
        raise ValueError('no matching terminal receipt')
    spec = common.read_json(jobs._p(jid, 'job.json'))
    if not isinstance(spec, dict):
        raise ValueError('unreadable job spec')
    return receipt, spec


def _owner_check(jid, owner):
    if os.path.lexists(jobs._p(jid, jobs.OWNER_PENDING)):
        raise ValueError('owner completion pending')
    if not owner:
        return
    b = baton.load(owner)
    notice = common.read_json(jobs._p(jid, jobs.OWNER_NOTIFIED))
    if not b or not isinstance(notice, dict) or notice.get('owner') not in (None, owner) \
            or notice.get('job_id') != jid:
        raise ValueError('owner completion unverified')
    if (b.get('waiting_on'), b.get('waiting_since')) == (notice.get('waiting_on'), notice.get('waiting_since')):
        raise ValueError('owner notice still in inbox; use twinlane result')


def collected(jid, receipt):
    """A valid read stamp at least as recent as this receipt; shared with completions."""
    try:
        with open(jobs._p(jid, jobs.COLLECTED), encoding='utf-8') as handle:
            stamped = common.parse_time(handle.read().strip())
    except (OSError, UnicodeError):
        stamped = None
    ended = common.parse_time(receipt.get('ended_at') or receipt.get('finished_at'))
    return stamped is not None and (ended is None or stamped >= ended)


def _ack(jid, args):
    _, spec = _record(jid)
    owner = spec.get('owner')
    if owner is not None and (not isinstance(owner, str) or not baton.valid_slug(owner)):
        raise ValueError('invalid owner')
    # Baton writers take the baton lock before job locks. Match that order.
    with baton.baton_lock(owner) if owner and not args.dry_run else nullcontext():
        with nullcontext() if args.dry_run else jobs.archive_lock(jid):
            receipt, current = _record(jid)
            if current.get('owner') != owner:
                raise ValueError('owner changed; retry after review')
            if args.owner and owner != args.owner or args.name and not fnmatch.fnmatchcase(
                    str(current.get('name') or receipt.get('name') or ''), args.name):
                raise ValueError('filter mismatch')
            if args.older_than is not None:
                ended = common.parse_time(receipt.get('ended_at') or receipt.get('finished_at'))
                if ended is None:
                    raise ValueError('invalid completion time')
                if (common.now() - ended).total_seconds() <= args.older_than * 86400:
                    raise ValueError('recent')
            if os.path.lexists(jobs._p(jid, jobs.COLLECTED)):
                # A corrupt/stale marker is repairable, but never dismiss an outstanding owner notice.
                if collected(jid, receipt):
                    return 'skipped: already collected'
            _owner_check(jid, owner)
            if args.dry_run:
                return 'would stamp'
            if not jobs.mark_collected(jid):
                raise OSError('read marker could not be written')
            return 'stamped'


def cli(argv):
    ap = argparse.ArgumentParser(prog='twinlane ack')
    ap.add_argument('ids', nargs='*')
    ap.add_argument('--older-than', type=float)
    filters = ap.add_mutually_exclusive_group()
    filters.add_argument('--owner')
    filters.add_argument('--name')
    ap.add_argument('--dry-run', action='store_true')
    args = jobs._parse(ap, argv)
    if isinstance(args, int):
        return args
    if bool(args.ids) == (args.older_than is not None) or (args.owner or args.name) and args.older_than is None \
            or args.older_than is not None and (not math.isfinite(args.older_than) or args.older_than < 0) \
            or args.owner and not baton.valid_slug(args.owner):
        print('twinlane ack: use JOB... or --older-than finite nonnegative DAYS [--owner SLUG|--name GLOB]')
        return common.EXIT_USAGE
    try:
        ids = list(dict.fromkeys(jobs.resolve_job(j) for j in args.ids)) if args.ids else jobs._job_ids()
    except (LookupError, OSError) as error:
        print(f'twinlane ack: {error}')
        return common.EXIT_USAGE
    code = common.EXIT_OK
    for jid in ids:
        try:
            outcome = _ack(jid, args)
        except (ValueError, baton.BatonError) as error:
            outcome = 'skipped: ' + str(error)
        except (OSError, TimeoutError):
            outcome = 'skipped: cannot safely read or stamp receipt'
            code = common.EXIT_FAILED
        print(f'{jid}: {outcome}')
    return code
