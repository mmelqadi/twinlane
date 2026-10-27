"""P0c coordination events and papercuts; disposable homes, no engines or network."""
import contextlib
import datetime as dt
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import threading
import unittest
from unittest import mock

import test_support  # noqa: F401; isolate before importing runtime modules
import twinlane
from twinlane import baton, board, capacity, cli, common, guard, inbox, jobs, meter, observations, projects, sessions, watch, wip


def jid(n=1):
    return f'20261017-120000-codex-{n:04x}'


class Fixture(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix='twinlane-p0c-')
        self.addCleanup(test_support.cleanup_tempdir, temp)
        self.tmp = Path(os.path.realpath(temp.name))  # the code under test resolves links and short names
        for name in ('STATE', 'JOBS', 'BATONS', 'BIN'):
            patch = mock.patch.object(common, name, str(self.tmp / name.lower()))
            patch.start()
            self.addCleanup(patch.stop)
        common.ensure_dirs()
        self.real_bound_contexts = watch.bound_contexts
        for patch in (mock.patch.object(sessions, 'collect_sessions', return_value={'rows': []}),
                      mock.patch.object(watch, 'bound_contexts', return_value=[]),
                      mock.patch.dict(os.environ, {'TWINLANE_CODEX_BIN': ''})):
            patch.start()
            self.addCleanup(patch.stop)

    def set_baton(self, slug='task', **fields):
        b = baton.load(slug) or baton.empty_baton(slug)
        b.update(fields)
        return baton.save(b)

    def spec(self, n=1, owner='task'):
        spec = {'id': jid(n), 'engine': 'codex', 'owner': owner, 'profile': 'intel',
                'primary': {'engine': 'codex'}, 'cwd': str(self.tmp)}
        common.write_json(jobs._p(jid(n), 'job.json'), spec)
        return spec

    def finish(self, n=1, status='done'):
        receipt = {'id': jid(n), 'engine': 'codex', 'status': status, 'exit_code': 0 if status == 'done' else 5}
        jobs._write_receipt(jid(n), receipt)
        return receipt

    def capture_watch(self, name='lead', **kw):
        with contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(watch.run(name=name, timeout=0, **kw), 0)
        return out.getvalue()


class Waiting(Fixture):
    def test_validation_legacy_load_and_cli_errors(self):
        for value in ('lead', 'user', 'none', 'job:' + jid(), 'lane:task-2'):
            self.assertEqual(baton.require_waiting_on(value), value)
        for value in ('', 'job:no', 'lane:../bad', 'lane:UPPER', 'lane:task\n', 'lead\n', None, 1):
            with self.subTest(value=value), self.assertRaises(baton.BatonError):
                baton.require_waiting_on(value)
        old = baton.empty_baton('old')
        old.pop('waiting_on')
        old.pop('waiting_since')
        common.write_json(baton.baton_path('old'), old)
        self.assertEqual(baton.load('old')['waiting_on'], 'none')
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['set', 'old', '--waiting-on', 'job:no']), 2)
        old['waiting_on'] = 'invalid'
        common.write_json(baton.baton_path('old'), old)
        with self.assertRaises(baton.BatonError):
            baton.load('old')

    def test_new_and_set_preserve_timestamp_until_value_changes(self):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['new', 'task', '--goal', 'g', '--waiting-on', 'lead']), 0)
            first = baton.load('task')['waiting_since']
            self.assertEqual(baton.cli('baton', ['set', 'task', '--waiting-on', 'lead', '--next', 'step']), 0)
            self.assertEqual(baton.load('task')['waiting_since'], first)
            self.assertEqual(baton.cli('baton', ['set', 'task', '--waiting-on', 'none']), 0)
        self.assertNotEqual(baton.load('task')['waiting_since'], first)

    def test_user_reuses_question_rule_and_answer_is_not_approval(self):
        for action in (['new', 'task', '--goal', 'g'], ['set', 'task']):
            if action[0] == 'set':
                self.set_baton()
            with contextlib.redirect_stderr(io.StringIO()):
                self.assertEqual(baton.cli('baton', action + ['--waiting-on', 'user']), 2)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['set', 'task', '--waiting-on', 'user', '--ask', 'Proceed']), 2)
            self.assertEqual(baton.cli('baton', ['set', 'task', '--waiting-on', 'user', '--ask', 'Review one record?']), 0)
        row = inbox.items(session_rows=[])[0]
        self.assertFalse(row['live_write_approval'])
        b = inbox.answer(row['id'], grant='Yes, review it')
        self.assertEqual(b['waiting_on'], 'lead')
        self.assertFalse(b['holds']['answers'][-1]['live_write_approval'])

    def test_board_order_idle_capacity_and_no_writes_in_all_formats(self):
        for slug, who, when in [('later', 'lead', '2026-10-17T10:00:00Z'),
                                ('first', 'lead', '2026-10-17T09:00:00Z'),
                                ('person', 'user', '2026-10-17T08:00:00Z')]:
            b = self.set_baton(slug, waiting_on=who)
            b['waiting_since'] = when
            common.write_json(baton.baton_path(slug), b)
        rows = [{'engine': 'codex', 'id': 'session-1', 'state': 'idle', 'age_s': 10}]
        def files():
            return {str(p): (p.read_bytes(), p.stat().st_mtime_ns) for p in self.tmp.rglob('*') if p.is_file()}
        before = files()
        with mock.patch.object(sessions, 'collect_sessions', return_value={'rows': rows}):
            data = board.collect()
        self.assertEqual([b['slug'] for b in data['waiting']['lead']], ['first', 'later'])
        self.assertEqual(data['lanes']['idle'], 1)
        for render in (board.render_text, board.render_md):
            text = render(data)
            self.assertIn('Waiting on lead', text)
            self.assertIn('Waiting on user', text)
            self.assertIn('writers 0/5', text)
            self.assertIn('idle lanes: 1', text)
        self.assertIn('capacity', json.loads(json.dumps(data)))
        self.assertEqual(before, files())


class Owner(Fixture):
    def test_unknown_owner_rejected_before_job_allocation(self):
        before = list(Path(common.JOBS).iterdir())
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(jobs.cmd_run(['--owner', 'missing', '--brief', 'absent', '--cd', str(self.tmp)]), 2)
        self.assertEqual(before, list(Path(common.JOBS).iterdir()))
        with self.assertRaises(jobs.Refusal) as error:
            jobs.create_job({'owner': '../bad'})
        self.assertEqual(error.exception.code, 2)

    def test_create_registers_owner_job_without_engine(self):
        self.set_baton()
        plan = {'owner': 'task', 'engine': 'codex', 'profile': 'intel', 'brief_text': 'review',
                'cwd': str(self.tmp), 'primary': {'engine': 'codex'}}
        with mock.patch.object(jobs, '_coordination_parent', return_value=None):
            job = jobs.create_job(plan)
        b = baton.load('task')
        self.assertEqual(b['waiting_on'], 'job:' + job)
        self.assertEqual(b['jobs'][0]['id'], job)
        self.assertIsNone(b['owner']['engine'])

    def test_terminal_done_failed_exactly_once_and_clears_flight(self):
        for n, status in enumerate(('done', 'failed'), 1):
            self.set_baton(waiting_on='job:' + jid(n))
            self.spec(n)
            receipt = self.finish(n, status)
            jobs._write_receipt(jid(n), receipt)
            events = [e for e in baton.history('task') if e.get('event') == 'job_done' and e.get('job_id') == jid(n)]
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]['status'], status)
            self.assertEqual(events[0]['exit_code'], receipt['exit_code'])
            self.assertEqual(events[0]['receipt_path'], jobs._p(jid(n), 'result.json'))
            b = baton.load('task')
            self.assertEqual(b['waiting_on'], 'lead')
            self.assertNotIn(jid(n), [f.get('job') for f in b['progress']['in_flight']])

    def test_terminal_transitions_and_never_overwrites_user_or_other_job(self):
        for n, (waiting, bound, expected) in enumerate([
                ('none', [], 'lead'), ('job:' + jid(2), [{'id': 'session-2', 'engine': 'codex'}], 'lane:task'),
                ('user', [], 'user'), ('job:' + jid(99), [], 'job:' + jid(99)),
                ('lane:other', [], 'lane:other')], 1):
            self.set_baton(waiting_on=waiting, bound_sessions=bound)
            self.spec(n)
            self.finish(n)
            self.assertEqual(baton.load('task')['waiting_on'], expected)

    def test_all_terminal_receipts_notify(self):
        for n, status in enumerate(sorted(jobs.TERMINAL), 1):
            self.set_baton(waiting_on='job:' + jid(n))
            self.spec(n)
            self.finish(n, status)
            self.assertTrue(Path(jobs._p(jid(n), 'owner-notified.json')).exists(), status)

    def test_notification_failure_does_not_change_result(self):
        self.spec()
        with mock.patch.object(baton, 'owner_job', side_effect=OSError('fixture failure')), \
                contextlib.redirect_stderr(io.StringIO()) as err:
            receipt = self.finish(status='failed')
        self.assertEqual(common.read_json(jobs._p(jid(), 'result.json')), receipt)
        self.assertEqual(receipt['exit_code'], 5)
        self.assertIn('notification failed', err.getvalue())

    def test_collect_item_clears_on_result_answer_or_waiting_change(self):
        for n, how in enumerate(('result', 'answer', 'waiting'), 1):
            self.set_baton(waiting_on='job:' + jid(n))
            self.spec(n)
            self.finish(n)
            row = next(r for r in inbox.items(session_rows=[]) if r.get('job_id') == jid(n))
            self.assertEqual(row['question'], f'collect {jid(n)} for task')
            if how == 'result':
                with contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(jobs.cmd_result([jid(n), '--json']), 0)
            elif how == 'answer':
                inbox.answer(row['id'], grant='Collected')
            else:
                self.set_baton(waiting_on='none')
            self.assertFalse(any(r.get('job_id') == jid(n) for r in inbox.items(session_rows=[])))


class Watch(Fixture):
    def test_each_poll_rebuilds_observations_and_sees_new_completion(self):
        self.spec(owner=None)
        polls = []
        pending = watch.pending
        def observed_pending(cursor):
            events, next_cursor = pending(cursor)
            polls.append(events)
            return events, next_cursor
        def finish_between_polls(_delay):
            self.assertEqual(polls, [[]])
            self.finish()
        scripted = iter([0, 0])
        real_clock = watch.time.monotonic  # lock helpers share this clock: script only watch's first two reads
        def scripted_clock():
            for value in scripted:
                return value
            return real_clock()
        with mock.patch.object(watch, 'pending', side_effect=observed_pending), \
                mock.patch.object(watch.time, 'monotonic', side_effect=scripted_clock), \
                mock.patch.object(watch.time, 'sleep', side_effect=finish_between_polls) as sleep, \
                contextlib.redirect_stdout(io.StringIO()) as output:
            self.assertEqual(watch.run(timeout=10, interval=1, as_json=True), 0)
        sleep.assert_called_once()
        self.assertEqual(len(polls), 2)
        self.assertEqual([(e['type'], e['job_id']) for e in polls[1]], [('job_finished', jid())])
        self.assertEqual(json.loads(output.getvalue())['job_id'], jid())

    def test_pending_events_replay_and_two_names_are_independent(self):
        self.spec(owner=None)
        self.finish()
        self.set_baton(waiting_on='lead')
        for name in ('lead', 'other'):
            output = self.capture_watch(name=name, as_json=True)
            events = [json.loads(line) for line in output.splitlines()]
            self.assertEqual({e['type'] for e in events}, {'job_finished', 'waiting_on_lead'})
            self.assertEqual(self.capture_watch(name=name), 'watch: no events in 0 s\n')

    def test_blocks_then_wakes_on_new_receipt(self):
        ready = threading.Event()
        original = watch.pending
        def pending(cursor):
            result = original(cursor)
            ready.set()
            return result
        result = []
        with mock.patch.object(watch, 'pending', side_effect=pending), contextlib.redirect_stdout(io.StringIO()) as out:
            thread = threading.Thread(target=lambda: result.append(watch.run(timeout=2, interval=.01)), daemon=True)
            thread.start()
            self.assertTrue(ready.wait(1))
            self.assertTrue(thread.is_alive())
            self.spec(owner=None)
            self.finish()
            thread.join(3)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result, [0])
        self.assertIn(jid(), out.getvalue())

    def test_timeout_and_file_only_no_refresh_or_subprocess(self):
        with mock.patch.object(meter, 'read', side_effect=AssertionError('refresh')), \
                mock.patch.object(subprocess, 'run', side_effect=AssertionError('subprocess')):
            self.assertEqual(self.capture_watch(), 'watch: no events in 0 s\n')
        self.assertEqual({p.name for p in Path(common.STATE).iterdir()}, {'watch-lead.json'})

    def test_meter_crossings_once_per_reset_and_no_decrease_event(self):
        reset = common.iso(common.now() + dt.timedelta(days=2))
        for engine, name in (('claude', 'five_hour'), ('claude', 'seven_day'), ('codex', 'weekly')):
            for pct, expected in ((79, None), (81, 80), (81, None), (84, None), (86, 85), (70, None), (86, None)):
                common.write_json(meter.meter_path(), {engine: {'windows': [{'name': name, 'pct': pct, 'resets_at': reset}]}})
                output = self.capture_watch(name=engine + '-' + name.replace('_', '-'), as_json=True)
                if expected is None:
                    self.assertIn('no events', output)
                else:
                    event = json.loads(output)
                    self.assertEqual((event['type'], event['threshold']), ('meter_crossing', expected))

    def test_context_crossing_and_unknown_skip(self):
        cursor = watch.initial_cursor()
        for pct, expected in ((None, 0), (69, 0), (71, 1), (50, 0), (72, 0)):
            with mock.patch.object(watch, 'bound_contexts', return_value=[{'slug': 'task', 'session_id': 'session-1', 'pct': pct}]):
                events, cursor = watch.pending(cursor)
            self.assertEqual(len(events), expected)
            if events:
                self.assertTrue(events[0]['estimate'])

    def test_cursor_advances_only_after_printing(self):
        self.spec(owner=None)
        self.finish()
        with mock.patch('builtins.print', side_effect=OSError('output failed')):
            with self.assertRaises(OSError):
                watch.run(timeout=0)
        self.assertFalse(Path(watch.cursor_path('lead')).exists())
        self.assertIn(jid(), self.capture_watch())

    def test_engine_worker_is_refused_by_cli_and_guard(self):
        with mock.patch.object(projects, 'in_worker', return_value=True), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(cli.main(['watch', '--timeout', '0']), 3)
        for command in ('twinlane watch', 'python -m twinlane watch', 'dual watch --name worker'):
            self.assertIsNotNone(guard.shell_hazard(command, config={'self_aliases': ['dual']}), command)
        self.assertFalse(Path(watch.cursor_path('lead')).exists())


class Papercuts(Fixture):
    def test_status_capacity_line_all_formats(self):
        with mock.patch.object(meter, 'cached_observation', return_value={}), mock.patch.object(jobs, 'list_jobs', return_value=[]), \
                mock.patch.object(wip, 'collect', return_value=[{}, {}]), \
                mock.patch.object(wip, 'queue', return_value=[{}]):
            for args in ([], ['--json']):
                with contextlib.redirect_stdout(io.StringIO()) as out:
                    self.assertEqual(cli.status(args), 0)
                value = json.loads(out.getvalue())['capacity'] if args else out.getvalue()
                self.assertIn('writers 2/5 · queued 1 · route: claude-first', value)

    def test_plain_and_structured_evidence(self):
        self.set_baton()
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['set', 'task', '--evidence', 'fixture passed',
                                               '--evidence', 'test|tests/example.py|passed',
                                               '--evidence', 'a note | with a separator']), 0)
        evidence = baton.load('task')['evidence']
        self.assertEqual(evidence[0]['text'], 'fixture passed')
        self.assertEqual(evidence[1], {'kind': 'test', 'path': 'tests/example.py', 'result': 'passed'})
        self.assertEqual(evidence[2]['text'], 'a note | with a separator')
        self.assertIn('fixture passed', '\n'.join(baton.render_baton_section(baton.load('task'))))

    def test_capped_text_warns_and_full_write_is_in_history(self):
        text = 'Long fixture prose ' * 30
        self.set_baton()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(baton.cli('baton', ['set', 'task', '--next', text, '--decision', text,
                                               '--done', text, '--evidence', text]), 0)
        b = baton.load('task')
        for value in (b['progress']['next_action'], b['decisions'][0]['what'], b['progress']['done'][0], b['evidence'][0]['text']):
            self.assertEqual(len(value), 300)
            self.assertTrue(value.endswith('\u2026'))
        self.assertIn('next_action: ' + str(len(text)), err.getvalue())
        full = baton.history('task')[-1]['full_text']
        self.assertEqual(full['baton.decisions[0].what'], text)
        self.assertEqual(full['baton.progress.next_action'], text)
        source = self.tmp / 'import.json'
        b['progress']['next_action'] = text
        common.write_json(str(source), b)
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(baton.cli('baton', ['set', 'task', '--file', str(source)]), 0)
        self.assertEqual(baton.history('task')[-1]['full_text']['baton.progress.next_action'], text)

    def test_meter_uses_best_complete_app_without_exporting_binary_to_run(self):
        root = self.tmp / 'app-bin'
        for name in ('older', 'newest', 'incomplete'):
            folder = root / name
            folder.mkdir(parents=True)
            for filename in (common.CODEX_FILES if name != 'incomplete' else ('codex.exe',)):
                (folder / filename).write_text('fixture', encoding='utf-8')
        # No environment changes: the discovery helper itself must never export a path.
        env = dict(os.environ)
        env.pop('TWINLANE_CODEX_BIN', None)
        versions = lambda path, *args: '2.0.0' if 'newest' in path else '1.0.0'
        def acquire(source):
            binary, _, _ = common._pick_codex_source()
            return binary, '', None
        with mock.patch.dict(os.environ, env, clear=True), mock.patch.object(common, 'IS_WIN', True), \
                mock.patch.object(common, '_app_bin_dirs', return_value=[str(root)]), \
                mock.patch.object(common, 'codex_version', side_effect=versions), \
                mock.patch.object(common, 'acquire_codex_binary', side_effect=acquire), \
                mock.patch.object(meter, '_exchange_reading', return_value=({'windows': []}, '')) as exchange:
            meter.read_app_server()
            self.assertEqual(exchange.call_args.args[0][0], str(root / 'newest' / 'codex.exe'))
            self.assertNotIn('TWINLANE_CODEX_BIN', os.environ)
            self.assertNotIn('TWINLANE_CODEX_BIN', jobs.claude_env(jid()))
            brief = self.tmp / 'brief.md'
            brief.write_text('Review the fixture', encoding='utf-8')
            args = jobs._run_parser().parse_args(['--brief', str(brief), '--cd', str(self.tmp),
                                                  '--engine', 'codex', '--kind', 'intel', '--dry-run'])
            with mock.patch.object(jobs, '_meter', return_value={}), \
                    mock.patch.object(jobs, '_route', return_value={'engine': 'codex', 'profile': 'intel'}), \
                    mock.patch.object(common, 'codex_preflight', return_value={
                        'binary': 'independent-job-choice', 'refusal': None, 'version': '3.0.0', 'origin': 'app', 'note': None}) as preflight:
                plan = jobs.plan_run(args, common.load_policy())
            preflight.assert_called_once()
            self.assertNotIn('codex_binary', plan)
            self.assertNotIn('TWINLANE_CODEX_BIN', jobs.worker_env(os.environ, jid()))

    def test_meter_falls_back_to_path_without_env_write(self):
        binary = str(self.tmp / 'external-bin' / 'codex.exe')
        with mock.patch.object(common, 'acquire_codex', return_value=(None, 'no app', None)), \
                mock.patch.object(common, 'load_policy', return_value={'codex': {'binary_source': 'auto'}}), \
                mock.patch.object(common.shutil, 'which', return_value=binary), \
                mock.patch.object(meter, '_exchange_reading', return_value=({'windows': []}, '')) as exchange:
            before = dict(os.environ)
            meter.read_app_server()
            self.assertEqual(exchange.call_args.args[0], [binary, 'app-server'])
            self.assertEqual(dict(os.environ), before)

    def test_version_has_commit_and_survives_missing_git_and_timeout(self):
        for response, suffix in ((subprocess.CompletedProcess([], 0, 'abc1234\n', ''), ' (abc1234)'),
                                 (OSError('missing'), ''), (subprocess.TimeoutExpired('git', 1), '')):
            with mock.patch.object(subprocess, 'run', **({'side_effect': response} if isinstance(response, Exception)
                                                       else {'return_value': response})), \
                    mock.patch.object(common, 'ensure_dirs', side_effect=AssertionError('write')), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['--version']), 0)
            self.assertEqual(out.getvalue(), 'twinlane ' + twinlane.__version__ + suffix + '\n')

    def test_finished_job_status_and_unknown(self):
        self.spec(owner=None)
        receipt = self.finish(status='failed')
        with mock.patch.object(meter, 'read', side_effect=AssertionError('meter')), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.status([jid()]), 0)
        for value in ('failed', 'exit=5', receipt['finished_at'], jobs._p(jid(), 'result.json')):
            self.assertIn(value, out.getvalue())
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(cli.status(['unknown-job']), 2)
        self.assertIn('unknown-job', err.getvalue())


class Recovery(Fixture):
    def pending_completion(self, where):
        self.set_baton(waiting_on='job:' + jid())
        self.spec()
        original_write = common.write_json
        def write(path, value, *args, **kwargs):
            if where == 'after-baton' and path == jobs._p(jid(), jobs.OWNER_PENDING) and 'event' in value:
                raise OSError('crash after baton save')
            if where == 'after-history' and path == jobs._p(jid(), jobs.OWNER_NOTIFIED):
                raise OSError('crash after history append')
            return original_write(path, value, *args, **kwargs)
        with contextlib.redirect_stderr(io.StringIO()):
            if where == 'after-receipt':
                with mock.patch.object(jobs, '_notify_owner', side_effect=RuntimeError('crash')):
                    with self.assertRaises(RuntimeError):
                        self.finish()
            else:
                with mock.patch.object(common, 'write_json', side_effect=write):
                    self.finish()
        self.assertEqual(common.read_json(jobs._p(jid(), jobs.OWNER_PENDING))['owner'], 'task')
        self.assertEqual(common.read_json(jobs._p(jid(), 'result.json'))['status'], 'done')

    def test_pending_intent_precedes_terminal_receipt(self):
        self.set_baton()
        self.spec()
        original = common.write_json
        def write(path, value, *args, **kwargs):
            if path == jobs._p(jid(), 'result.json'):
                self.assertTrue(Path(jobs._p(jid(), jobs.OWNER_PENDING)).is_file())
                raise OSError('receipt publication failed')
            return original(path, value, *args, **kwargs)
        with mock.patch.object(common, 'write_json', side_effect=write), self.assertRaises(OSError):
            self.finish()
        jobs.repair_owner(jid())
        self.assertFalse(any(e.get('event') == 'job_done' for e in baton.history('task')))

    def test_crashes_repair_once_on_wait_result_inbox_answer_and_runner_reentry(self):
        for crash in ('after-receipt', 'after-baton', 'after-history'):
            for route in ('wait', 'result', 'inbox', 'answer', 'runner'):
                with self.subTest(crash=crash, route=route):
                    # Each case has a separate owner and job home.
                    case = self.tmp / (crash + '-' + route)
                    with mock.patch.object(common, 'JOBS', str(case / 'jobs')), \
                            mock.patch.object(common, 'BATONS', str(case / 'batons')):
                        self.pending_completion(crash)
                        with contextlib.redirect_stdout(io.StringIO()), mock.patch.object(projects, 'in_worker', return_value=False):
                            if route == 'wait':
                                self.assertEqual(jobs.cmd_wait([jid(), '--timeout', '0']), 0)
                            elif route == 'result':
                                self.assertEqual(jobs.cmd_result([jid()]), 0)
                            elif route == 'inbox':
                                self.assertEqual(inbox.cli(['--json']), 0)
                            elif route == 'answer':
                                self.assertEqual(inbox.cli(['answer', 'task:' + jid(), '--grant', 'Collected']), 0)
                            else:
                                with mock.patch.object(jobs, '_claim_runner', return_value=None), \
                                        mock.patch.object(jobs, '_release_runner'), \
                                        mock.patch.object(jobs, '_claim_writer', side_effect=AssertionError('engine admission')):
                                    self.assertTrue(jobs.run_job(jid())['duplicate_runner'])
                        jobs.repair_owner(jid())
                        self.assertFalse(Path(jobs._p(jid(), jobs.OWNER_PENDING)).exists())
                        self.assertTrue(Path(jobs._p(jid(), jobs.OWNER_NOTIFIED)).exists())
                        done = [e for e in baton.history('task') if e.get('event') == 'job_done']
                        self.assertEqual(len(done), 1)
                        self.assertEqual(baton.load('task')['waiting_on'], 'lead')

    def test_more_than_twenty_collect_notices_survive_baton_cap(self):
        self.set_baton()
        for n in range(25):
            self.spec(n)
            self.finish(n)
        self.assertEqual(len(baton.load('task')['jobs']), 20)
        rows = inbox.items(session_rows=[])
        self.assertEqual({r['job_id'] for r in rows}, {jid(n) for n in range(25)})
        inbox.answer('task:' + jid(0), grant='Collected')
        self.assertEqual(len(inbox.items(session_rows=[])), 24)


class ReadOnlyStatus(Fixture):
    def test_effective_survivor_state_wins_over_terminal_receipt(self):
        self.spec(owner=None)
        self.finish()
        common.write_json(jobs._p(jid(), jobs.SURVIVOR), {'pid': 9876, 'created': 123})
        for state, expected in ((common.ALIVE, 'orphaned'), (common.UNKNOWN, 'unknown')):
            with mock.patch.object(jobs, '_survivor_state', return_value=state), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                self.assertEqual(cli.main(['status', jid(), '--json']), 0)
            data = json.loads(out.getvalue())
            self.assertEqual(data['status'], expected)
            self.assertEqual(data['receipt_status'], 'done')

    def test_status_main_never_initializes_refreshes_or_repairs(self):
        def snapshot():
            return {str(p): (p.read_bytes() if p.is_file() else None, p.stat().st_mtime_ns)
                    for p in self.tmp.rglob('*')}
        for populated in (False, True):
            case = self.tmp / ('existing' if populated else 'absent')
            with mock.patch.object(common, 'STATE', str(case / 'state')), \
                    mock.patch.object(common, 'JOBS', str(case / 'jobs')), \
                    mock.patch.object(common, 'BATONS', str(case / 'batons')):
                if populated:
                    self.set_baton()
                    self.spec()
                    with mock.patch.object(jobs, '_notify_owner'):
                        self.finish()
                    common.write_json(jobs._p(jid(), jobs.CHILD_STARTING), {'pid': 9876, 'created': 123})
                before = snapshot()
                with mock.patch.object(common, 'ensure_dirs', side_effect=AssertionError('mkdir')), \
                        mock.patch.object(common, 'write_json', side_effect=AssertionError('write')), \
                        mock.patch.object(meter, 'read', side_effect=AssertionError('refresh')), \
                        mock.patch.object(jobs, 'repair_owner', side_effect=AssertionError('repair')), \
                        mock.patch.object(jobs, '_pid_state', return_value=common.DEAD), \
                        contextlib.redirect_stdout(io.StringIO()):
                    for options in ([], ['--json'], ['--fresh']):
                        self.assertEqual(cli.main(['status'] + options), 0)
                    if populated:
                        self.assertEqual(cli.main(['status', jid()]), 0)
                self.assertEqual(before, snapshot())


class MeterBinaryPolicy(Fixture):
    def read(self, source, app, path):
        with mock.patch.object(common, 'load_policy', return_value={'codex': {'binary_source': source}}), \
                mock.patch.object(common, 'acquire_codex', return_value=(app, 'app fixture', None)) as acquire, \
                mock.patch.object(common.shutil, 'which', return_value=path) as which, \
                mock.patch.object(meter, '_exchange_reading', return_value=({'windows': []}, '')) as exchange:
            before = dict(os.environ)
            result = meter.read_app_server()
            self.assertEqual(dict(os.environ), before)
            return result, acquire, which, exchange

    def test_app_copy_has_no_path_fallback(self):
        result, _, which, exchange = self.read('app-copy', None, str(self.tmp / 'codex.exe'))
        self.assertIsNone(result[0])
        which.assert_not_called()
        exchange.assert_not_called()

    def test_auto_prefers_app_and_path_policy_skips_app(self):
        app, path = str(self.tmp / 'app' / 'codex.exe'), str(self.tmp / 'bin' / 'codex.exe')
        for source, expected in (('auto', app), ('app-copy', app), ('path', path)):
            with self.subTest(source=source):
                result, acquire, which, exchange = self.read(source, app, path)
                self.assertIsNotNone(result[0])
                self.assertEqual(exchange.call_args.args[0], [expected, 'app-server'])
                if source == 'path':
                    acquire.assert_not_called()
                else:
                    which.assert_not_called()

    def test_auto_fallback_and_relative_path_resolve_absolutely(self):
        here = self.tmp / 'cwd'  # relpath cannot cross drives, so make the temp dir the current directory's neighbour
        here.mkdir()
        previous = os.getcwd()
        os.chdir(here)
        self.addCleanup(os.chdir, previous)
        relative = os.path.relpath(self.tmp / 'external' / 'codex.exe')
        for source in ('auto', 'path'):
            result, _, _, exchange = self.read(source, None, relative)
            self.assertIsNotNone(result[0])
            self.assertEqual(exchange.call_args.args[0][0], os.path.realpath(relative))
            self.assertTrue(os.path.isabs(exchange.call_args.args[0][0]))

    def test_path_in_current_directory_and_descendants_is_refused(self):
        for path in ('codex.exe', os.path.join(os.getcwd(), 'tools', 'codex.exe')):
            result, _, _, exchange = self.read('path', None, path)
            self.assertIsNone(result[0])
            self.assertIn('current directory', result[1])
            exchange.assert_not_called()

    def test_explicit_binary_and_lease_are_preserved(self):
        explicit = str(self.tmp / 'explicit' / 'codex.exe')
        with mock.patch.dict(os.environ, {'TWINLANE_CODEX_BIN': explicit}):
            result, _, which, exchange = self.read('path', None, None)
            self.assertIsNotNone(result[0])
            self.assertEqual(exchange.call_args.args[0][0], explicit)
            which.assert_not_called()
        with mock.patch.object(common, 'acquire_codex_binary', return_value=(explicit, 'fixture', 'lease')), \
                mock.patch.object(common, 'release_codex') as release, \
                mock.patch.object(meter, '_exchange_reading', side_effect=OSError('exchange failed')):
            self.assertIsNone(meter.read_app_server()[0])
        release.assert_called_once_with('lease')


class ObservationCost(Fixture):
    def test_large_home_reads_specs_and_resolves_worktrees_once_per_command(self):
        roots = []
        for n in range(20):
            root = self.tmp / ('checkout-' + str(n))
            (root / '.git').mkdir(parents=True)
            roots.append(str(root))
        for n in range(30):
            b = baton.empty_baton('lane-' + str(n))
            b['repo']['worktree'] = roots[n % 20]
            b['progress_at'] = b['updated_at'] = common.iso(common.now() - dt.timedelta(days=3))
            b['state'] = 'waiting-on-you' if n % 2 else 'active'
            b['holds']['approvals_pending'] = [{'id': 'H1', 'kind': 'question', 'ask_text': 'Collect one record?', 'count': 1}]
            common.write_json(baton.baton_path(b['slug']), b)
        for n in range(1500):
            common.write_json(jobs._p(jid(n), 'job.json'), {
                'id': jid(n), 'cwd': roots[n % 20], 'profile': 'code', 'baton': 'lane-' + str(n % 30)})
            common.write_json(jobs._p(jid(n), 'result.json'), {
                'id': jid(n), 'status': 'done', 'finished_at': common.iso(common.now())})
        commands = {'status': lambda: cli.main(['status', '--json']),
                    'board': lambda: cli.main(['board', '--json']),
                    'inbox': lambda: cli.main(['inbox', '--json']),
                    'wip': lambda: cli.main(['wip', '--json']),
                    'watch': lambda: watch.pending(watch.initial_cursor())}
        for name, command in commands.items():
            with self.subTest(command=name), \
                    mock.patch.object(common, 'read_json', wraps=common.read_json) as reads, \
                    mock.patch.object(common, 'worktree_top', wraps=common.worktree_top) as tops, \
                    mock.patch.object(os, 'scandir', wraps=os.scandir) as scans, \
                    mock.patch.object(common, 'git', return_value=(1, '')), \
                    contextlib.redirect_stdout(io.StringIO()) as out:
                result = command()
                if name != 'watch':
                    self.assertEqual(result, 0, out.getvalue())
            from collections import Counter
            spec_reads = Counter(os.fspath(c.args[0]) for c in reads.call_args_list
                                 if os.path.basename(os.fspath(c.args[0])) == 'job.json')
            resolutions = Counter(os.fspath(c.args[0]) for c in tops.call_args_list)
            self.assertLessEqual(max(spec_reads.values(), default=0), 1, (name, spec_reads.most_common(2)))
            self.assertLessEqual(max(resolutions.values(), default=0), 1, (name, resolutions.most_common(2)))
            self.assertLessEqual(len(resolutions), 20)
            self.assertEqual(sum(os.fspath(c.args[0]) == common.JOBS for c in scans.call_args_list), 1)

    def test_old_job_activity_prefilter_does_not_open_specs(self):
        self.spec(owner=None)
        old = (common.now() - dt.timedelta(days=4)).timestamp()
        os.utime(jobs._p(jid(), 'job.json'), (old, old))
        os.utime(jobs.job_dir(jid()), (old, old))
        with observations.scope(), mock.patch.object(common, 'read_json', wraps=common.read_json) as reads:
            self.assertEqual(observations.activity(common.load_policy()), ({}, {}))
        self.assertFalse(any(os.path.basename(os.fspath(c.args[0])) == 'job.json' for c in reads.call_args_list))

    def test_snapshot_expires_between_commands(self):
        self.spec(owner=None)
        self.assertEqual(jobs.list_jobs()[0]['status'], 'queued')
        self.finish()
        self.assertEqual(jobs.list_jobs()[0]['status'], 'done')


class SharedContext(Fixture):
    def test_shared_bound_session_is_resolved_and_estimated_once_per_poll(self):
        from twinlane import context
        binding = {'id': 'shared-session', 'engine': 'codex'}
        batons = [{'slug': slug, 'bound_sessions': [binding]} for slug in ('one', 'two')]
        with observations.scope(), \
                mock.patch.object(baton, 'find_codex_session', return_value='fixture-rollout') as find, \
                mock.patch.object(common, 'tail_lines', return_value=[]) as tail, \
                mock.patch.object(context, 'estimate', return_value={'pct': 71}) as estimate:
            self.assertEqual(len(list(self.real_bound_contexts(batons))), 2)
        find.assert_called_once()
        tail.assert_called_once()
        estimate.assert_called_once()

    def test_shared_session_has_a_separate_crossing_for_each_bound_lane(self):
        cursor = watch.initial_cursor()
        for pct, expected in ((69, 0), (71, 2), (72, 0)):
            rows = [{'slug': slug, 'session_id': 'shared-session', 'pct': pct} for slug in ('one', 'two')]
            with mock.patch.object(watch, 'bound_contexts', return_value=rows):
                events, cursor = watch.pending(cursor)
            self.assertEqual(len(events), expected)
            if events:
                self.assertEqual({e['slug'] for e in events}, {'one', 'two'})


class Capacity(unittest.TestCase):
    def test_weekly_pace_routing_table_including_elapsed_fraction(self):
        now = dt.datetime(2026, 10, 17, tzinfo=dt.timezone.utc)
        def section(engine, pct, elapsed):
            return {'windows': [{'name': 'seven_day' if engine == 'claude' else 'weekly', 'pct': pct,
                                 'resets_at': common.iso(now + dt.timedelta(days=7 * (1 - elapsed)))}]}
        for claude, codex, expected, reason in [((60, .5), (40, .5), 'codex', 'lower'),
                                               ((40, .5), (60, .5), 'claude', 'lower'),
                                               ((50, .5), (50, .5), 'claude', 'tied'),
                                               ((40, .25), (60, .75), 'codex', 'lower'),
                                               (None, (10, .5), 'claude', 'claude weekly meter unknown'),
                                               ((10, .5), None, 'claude', 'codex weekly meter unknown'),
                                               (None, None, 'claude', 'claude and codex weekly meter unknown')]:
            data = {e: section(e, *v) if v else {} for e, v in (('claude', claude), ('codex', codex))}
            advice = capacity.advice(data, now)
            self.assertEqual(advice['engine'], expected)
            self.assertIn(reason, advice['reason'])
            self.assertIn(f'writers 2/5 · queued 3 · route: {expected}-first', capacity.line(2, 5, 3, data, now))

    def test_unknown_invalid_or_expired_weekly_windows(self):
        now = common.now()
        for sec in ({'status': 'unknown'}, {'windows': [{'name': 'weekly', 'pct': float('nan')}]},
                    {'windows': [{'name': 'weekly', 'pct': 50, 'resets_at': common.iso(now - dt.timedelta(seconds=1))}]}):
            self.assertIsNone(capacity.weekly_pace(sec, 'codex', now))
