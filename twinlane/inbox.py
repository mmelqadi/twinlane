"""Human questions projected from baton holds, including legacy approvals.

Answers are evidence on the originating baton, never execution authority.
"""
from __future__ import annotations

import argparse
import glob
import os
import sys
import copy

from . import baton, common, observations


@observations.command
def items(batons=None, policy=None, session_rows=None):
    policy = policy or common.load_policy()
    if batons is None:
        batons = []
        for path in sorted(glob.glob(os.path.join(common.BATONS, '*.json'))):
            slug = os.path.basename(path)[:-5]
            if baton.valid_slug(slug):
                try:
                    b = baton.load(slug)
                except Exception as e:
                    raise baton.BatonError(f'inbox incomplete: unreadable baton {slug}') from e
                if not b:
                    raise baton.BatonError(f'inbox incomplete: unreadable baton {slug}')
                batons.append(b)
    rows = []
    for b in batons:
        b = baton._merge_defaults(copy.deepcopy(b))
        if not isinstance(b.get('holds'), dict) or not isinstance(b.get('owner'), dict) or \
                not isinstance(b['holds'].get('approvals_pending'), list):
            raise baton.BatonError(f"inbox incomplete: malformed holds/owner in {b.get('slug')}")
        seen = {}
        from . import jobs
        # Durable notices outlive the baton's bounded finished-job index.
        def notices():
            out = {}
            for jid in observations.job_ids():
                if not baton.JOB_ID_RE.fullmatch(jid) or os.path.exists(jobs._p(jid, jobs.COLLECTED)):
                    continue
                notice = observations.job_json(jid, jobs.OWNER_NOTIFIED)
                if isinstance(notice, dict):
                    owner = notice.get('owner')
                    if owner is None:  # legacy markers had no owner; use the spec, never the capped index
                        spec = observations.job_json(jid, default={})
                        owner = spec.get('owner') if isinstance(spec, dict) else None
                    if isinstance(owner, str):
                        out.setdefault(owner, []).append((jid, notice))
            return out
        for jid, notice in observations.once(('owner-notices', common.JOBS), notices).get(b['slug'], []):
            if (b.get('waiting_on'), b.get('waiting_since')) != (notice.get('waiting_on'), notice.get('waiting_since')):
                continue
            rows.append({'id': f"{b['slug']}:{jid}", 'baton': b['slug'], 'hold': jid, 'job_id': jid,
                         'question': f"collect {jid} for {b['slug']}", 'records': jid, 'count': 1,
                         'rank': 1, 'duplicates': 1, 'hold_ids': [], 'project': '-',
                         'at': notice.get('at'), 'engine': notice.get('engine'), 'live_write_approval': False})
        for hold in b['holds'].get('approvals_pending') or []:
            if not isinstance(hold, dict):
                continue
            question = hold.get('ask_text') or hold.get('action')
            if not isinstance(question, str) or not question.strip():
                continue
            question_item = hold.get('kind') == 'question'
            priority = 1 if question_item else 2
            if question_item and b.get('state') == 'waiting-on-you' and live_lane(b, session_rows, policy):
                priority = 0
            duplicate_key = (question, str(hold.get('action')), str(hold.get('count')))
            if not question_item and duplicate_key in seen:
                seen[duplicate_key]['duplicates'] += 1
                seen[duplicate_key]['hold_ids'].append(hold['id'])
                continue
            row = {'id': f"{b['slug']}:{hold['id']}", 'baton': b['slug'], 'hold': hold['id'],
                         'question': question, 'records': hold.get('action'),
                         'count': hold.get('count'), 'rank': priority, 'duplicates': 1, 'hold_ids': [hold['id']],
                         'project': os.path.basename(observations.worktree_top(b['repo'].get('worktree')) or
                                                     b['repo'].get('worktree') or '') if b['repo'].get('worktree') else '-',
                         'at': hold.get('asked_at') or hold.get('at') or b.get('progress_at') or b.get('updated_at'),
                         'engine': hold.get('engine') or b['owner'].get('engine'),
                         'live_write_approval': False}
            if not isinstance(row['at'], str):
                row['at'] = None
            rows.append(row)
            if not question_item:
                seen[duplicate_key] = row
    return rank(rows)


def rank(rows):
    return sorted(rows, key=lambda r: (r.get('rank', 2), r.get('at') or '', r.get('baton', ''), r['id']))


def live_lane(b, session_rows, policy):
    from . import lanes
    cwd = (b.get('repo') or {}).get('worktree')
    if cwd and baton.live_jobs_for(cwd):
        return True
    def live_batons():
        from . import jobs
        owners = set()
        for jid in observations.job_ids():
            spec = observations.job_json(jid)
            if isinstance(spec, dict) and spec.get('baton') and jobs.job_status(jid) not in jobs.TERMINAL:
                owners.add(spec['baton'])
        return owners
    if b['slug'] in observations.once(('live-batons', common.JOBS), live_batons):
        return True
    if session_rows is None:
        from . import sessions
        session_rows = observations.once(('inbox-sessions',), lambda: sessions.collect_sessions(since_s=(policy.get('wip') or {}).get('session_active_s', 1800),
                                                  limit=0, raw_paths=True, policy=policy)['rows'])
    ids = lanes.bound_ids(b)
    for r in session_rows:
        age = r.get('age_s')
        if age is not None and age > (policy.get('wip') or {}).get('session_active_s', 1800):
            continue
        path = r.get('_writer_cwd') or r.get('cwd')
        if r.get('state') in ('working', 'active', 'mid-turn', 'needs-you') and (
                r.get('id') in ids or cwd and path and common.writer_key(path) == common.writer_key(cwd)):
            return True
    return False


def projection(rows, show_all=False, policy=None):
    cfg = (policy or common.load_policy()).get('inbox') or {}
    shown = rows if show_all else rows[:cfg.get('show', 5)]
    return {'items': shown, 'total': len(rows), 'shown': len(shown), 'hidden': len(rows)-len(shown),
            'by_rank': {name: sum(r.get('rank', 2) == i for r in rows)
                        for i, name in enumerate(('blocking', 'waiting', 'legacy'))}}


def header(data):
    counts = data['by_rank']
    return (f"inbox: {data['total']} open ({counts['blocking']} blocking, {counts['waiting']} waiting, "
            f"{counts['legacy']} legacy); showing {data['shown']}, {data['hidden']} hidden: twinlane inbox --all")


def answer(ident, grant=None, deny=None):
    if deny is not None and not deny.strip():
        raise baton.BatonError('--deny needs a nonempty reason')
    slug, sep, hold_id = ident.partition(':')
    if not sep or not hold_id:
        raise baton.BatonError('inbox ID must be BATON:HOLD (from twinlane inbox)')
    baton.require_slug(slug)
    if baton.JOB_ID_RE.fullmatch(hold_id):
        if not any(r['id'] == ident for r in items()):
            raise baton.BatonError('no pending inbox item ' + ident)
        from . import jobs
        jobs.mark_collected(hold_id)
        return baton.load(slug)
    with baton.baton_lock(slug):
        b = baton.load_for_update(slug)
        if b is None:
            raise baton.BatonError(f'no baton {slug}')
        if not __import__('re').fullmatch(r'H[0-9]+', hold_id):
            raise baton.BatonError('invalid inbox hold id')
        holds = b['holds']
        hold = next((h for h in holds['approvals_pending'] if isinstance(h, dict) and h.get('id') == hold_id), None)
        if hold is None:
            raise baton.BatonError('no pending inbox item ' + ident)
        hold.setdefault('engine', b['owner'].get('engine'))
        baton.record_answer(holds, hold, 'yes' if grant is not None else 'no', grant if grant is not None else deny)
        baton.after_answer(b)
        return baton.save(b, 'user', strict=True)


@observations.command
def cli(argv):
    ap = argparse.ArgumentParser(prog='twinlane inbox')
    ap.add_argument('--all', action='store_true')
    ap.add_argument('--json', action='store_true')
    sub = ap.add_subparsers(dest='action')
    p = sub.add_parser('answer')
    p.add_argument('id')
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument('--grant', metavar='VERBATIM_WORDS')
    choice.add_argument('--deny', metavar='REASON')
    p.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    try:
        if a.action == 'answer':
            from . import projects
            refused = projects.refuse_in_worker('inbox answer')
            if refused is not None:
                return refused
            from . import jobs
            jobs.repair_owners()
            b = answer(a.id, a.grant, a.deny)
            decision = 'yes' if a.grant is not None else 'no'
            message = f'answered {a.id} {decision}: recorded as an answer on baton {b["slug"]} (scope: {b["owner"].get("engine") or "the baton owner"}).'
            if a.grant is not None:
                message += ("\nAn inbox answer is never a live-write approval: a live write still needs the user's own "
                            'confirmation naming the action and its records, given to the session that performs it.')
            common.emit(baton.redact_obj(dict(b, live_write_approval=False)), a.json, message)
        else:
            from . import jobs, projects
            if not projects.in_worker():
                jobs.repair_owners()
            rows = items()
            data = projection(rows, a.all)
            common.emit(baton.redact_obj(data), a.json, baton.redact(header(data) + '\n' + '\n'.join(
                f"  [{('blocking', 'waiting', 'legacy')[r['rank']]}] {r['id']} {common.fmt_age(common.age_s(r['at']))} "
                f"{r['engine'] or '-'} [{r['project']}] {r['question']} (record {r['records']}, count {r['count']})"
                + (f" x{r['duplicates']}" if r['duplicates'] > 1 else '') for r in data['items'])))
    except (baton.BatonError, TimeoutError) as e:
        print(baton.redact(str(e)), file=sys.stderr)
        return common.EXIT_USAGE
    return common.EXIT_OK
