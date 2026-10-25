"""File-only, resumable event delivery. No engine, network or meter refresh."""
from __future__ import annotations

import argparse
import copy
import glob
import json
import math
import os
import sys
import time

from . import baton, common, observations


def cursor_path(name):
    baton.require_slug(name)
    return os.path.join(common.STATE, 'watch-' + name + '.json')


def initial_cursor():
    return {'version': 1, 'seen': [], 'levels': {}}


def _level(state, key, pct, thresholds, events, **fields):
    if type(pct) not in (int, float) or not math.isfinite(pct):
        return
    old = state['levels'].get(key)
    if old is not None:
        for threshold in thresholds:
            if old < threshold <= pct:
                events.append(dict(fields, threshold=threshold, pct=pct))
    # High-water per window: a decrease must not re-arm the same threshold.
    state['levels'][key] = max(old, pct) if old is not None else pct


def bound_contexts(batons):
    """Only bound transcripts, with a bounded tail and an already identifiable engine."""
    from . import context
    for b in batons:
        for binding in b.get('bound_sessions') or []:
            engine, sid = binding.get('engine'), binding.get('id')
            if engine not in ('claude', 'codex') or not isinstance(sid, str):
                continue
            path = observations.once(('bound-path', engine, sid), lambda: (
                baton.find_claude_session(sid) if engine == 'claude' else baton.find_codex_session(sid)))
            if path:
                estimate = observations.once(('bound-context', engine, sid), lambda: context.estimate(
                    engine, common.tail_lines(path, max_bytes=256_000)))
                if estimate.get('pct') is not None:
                    yield {'slug': b['slug'], 'session_id': sid, **estimate}


@observations.command
def pending(cursor):
    from . import jobs, meter
    state = copy.deepcopy(cursor)
    seen = set(state['seen'])
    events = []

    def event(key, **data):
        if key not in seen:
            seen.add(key)
            events.append(data)

    for jid in sorted(observations.job_ids()):
        path = jobs._p(jid, 'result.json')
        receipt = observations.job_json(jid, 'result.json')
        if not isinstance(receipt, dict) or receipt.get('status') not in jobs.TERMINAL or not receipt.get('finished_at'):
            continue
        jid = os.path.basename(os.path.dirname(path))
        event('job:' + jid + ':' + str(receipt['finished_at']), type='job_finished', job_id=jid,
              status=receipt['status'], exit_code=receipt.get('exit_code'), at=receipt['finished_at'], receipt_path=path)
    batons = []
    for path in sorted(glob.glob(os.path.join(common.BATONS, '*.json'))):
        slug = os.path.basename(path)[:-5]
        if not baton.valid_slug(slug):
            continue
        b = baton.load(slug)
        if not b:
            continue
        batons.append(b)
        for row in baton.history(slug) + [b]:
            if row.get('waiting_on') == 'lead' and row.get('waiting_since'):
                event('lead:' + slug + ':' + row['waiting_since'], type='waiting_on_lead', slug=slug,
                      at=row['waiting_since'], next_action=common.clip((row.get('progress') or {}).get('next_action'), 120))
    cached = common.read_json(meter.meter_path(), {})
    cached = cached if isinstance(cached, dict) else {}
    for engine, names in (('claude', ('five_hour', 'seven_day')), ('codex', ('weekly',))):
        section = cached.get(engine)
        if not isinstance(section, dict) or section.get('status') == 'unknown':
            continue
        for window in section.get('windows') or []:
            if not isinstance(window, dict) or window.get('name') not in names:
                continue
            reset = window.get('resets_at')
            if common.parse_time(reset) is None or common.parse_time(reset) <= common.now():
                continue
            key = f"meter:{engine}:{window['name']}:{reset}"
            _level(state, key, window.get('pct'), (80, 85), events, type='meter_crossing',
                   engine=engine, window=window['name'], resets_at=reset)
    for ctx in bound_contexts(batons):
        _level(state, 'context:' + ctx['slug'] + ':' + ctx['session_id'], ctx['pct'], (70,), events,
               type='context_crossing', slug=ctx['slug'], session_id=ctx['session_id'], estimate=True)
    state['seen'] = sorted(seen)
    return events, state


def compact(event):
    if event['type'] == 'job_finished':
        return f"watch: job {event['job_id']} {event['status']} (exit {event.get('exit_code')})"
    if event['type'] == 'waiting_on_lead':
        return f"watch: {event['slug']} waiting on lead: {event.get('next_action') or '-'}"
    if event['type'] == 'meter_crossing':
        return f"watch: {event['engine']} {event['window']} crossed {event['threshold']}% ({event['pct']:g}%)"
    return f"watch: {event['slug']} context crossed 70% ({event['pct']:g}%, estimate)"


def run(name='lead', timeout=3300, interval=5, as_json=False):
    path = cursor_path(name)
    cursor = common.read_json(path) if os.path.exists(path) else initial_cursor()
    if not isinstance(cursor, dict) or cursor.get('version') != 1 or not isinstance(cursor.get('seen'), list) or \
            not isinstance(cursor.get('levels'), dict):
        raise ValueError('watch cursor unreadable; refusing to overwrite it')
    deadline = time.monotonic() + timeout
    while True:
        events, next_cursor = pending(cursor)
        if events:
            for event in events:
                print(json.dumps(baton.redact_obj(event), ensure_ascii=False) if as_json else baton.redact(compact(event)), flush=True)
            # Printing and flushing precede acknowledgement: a crash may duplicate, never swallow a wake.
            common.write_json(path, next_cursor)
            return 0
        cursor = next_cursor
        # Baseline observations acknowledge no events. Keep them durable so a
        # later crossing remains pending even if the watcher exits before output.
        common.write_json(path, cursor)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            print(f'watch: no events in {timeout:g} s', flush=True)
            return 0
        time.sleep(min(interval, remaining))


def cli(argv):
    from . import projects
    refused = projects.refuse_in_worker('watch')
    if refused is not None:
        return refused
    ap = argparse.ArgumentParser(prog='twinlane watch')
    ap.add_argument('--timeout', type=float, default=3300)
    ap.add_argument('--interval', type=float, default=5)
    ap.add_argument('--name', default='lead')
    ap.add_argument('--json', action='store_true')
    a = ap.parse_args(argv)
    if not math.isfinite(a.timeout) or a.timeout < 0 or not math.isfinite(a.interval) or a.interval <= 0:
        print('watch: timeout must be nonnegative and interval positive', file=sys.stderr)
        return common.EXIT_USAGE
    try:
        return run(a.name, a.timeout, a.interval, a.json)
    except (ValueError, baton.BatonError, OSError) as e:
        print(f'watch: {e}', file=sys.stderr)
        return common.EXIT_USAGE
