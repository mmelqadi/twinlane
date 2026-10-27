"""Synthetic receipts only. Every toast process launch is mocked, including on Windows."""
import base64
from concurrent.futures import ThreadPoolExecutor
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import test_support  # noqa: E402
from twinlane import ack, cli, common, completions, completion_toast, guard, jobs  # noqa: E402


class CompletionSinkTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='twinlane-completions-')
        self.addCleanup(test_support.cleanup_tempdir, self.temp)
        self.home = Path(self.temp.name)
        for key in ('STATE', 'JOBS', 'BATONS'):
            path = self.home / key.lower()
            path.mkdir()
            patch = mock.patch.object(common, key, str(path))
            patch.start()
            self.addCleanup(patch.stop)
        self.settings = {'completions_log': True, 'toast': False}
        patch = mock.patch.object(common, 'load_policy', side_effect=lambda: {'notify': self.settings})
        self.policy = patch.start()
        self.addCleanup(patch.stop)
        # Never show a toast, even if a test accidentally reaches the launch boundary.
        patch = mock.patch.object(completions.subprocess, 'Popen')
        self.launch = patch.start()
        self.addCleanup(patch.stop)
        self.count = 0

    def job(self, status='done', **extra):
        self.count += 1
        jid = f'20261026-120000-codex-{self.count:04x}'
        folder = Path(common.JOBS) / jid
        folder.mkdir()
        spec = dict(id=jid, name='bounded task', engine='codex', kind='code', owner=None,
                    profile='intel', cwd=str(self.home), primary={'engine': 'codex'}, notes=[])
        common.write_json(str(folder / 'job.json'), spec)
        receipt = dict(id=jid, name=spec['name'], engine='codex', kind='code', status=status,
                       ended_at='2026-10-26T12:00:00Z', notes=[], **extra)
        common.write_json(str(folder / 'result.json'), receipt)
        return jid, receipt, folder

    def rows(self):
        path = Path(completions._path())
        return [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()] if path.exists() else []

    def call(self, *args):
        output = io.StringIO()
        with redirect_stdout(output):
            code = completions.cli(list(args))
        return code, output.getvalue()

    def test_off_is_inert_and_only_literal_true_enables(self):
        jid, receipt, _ = self.job()
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        for settings in (None, False, {}, {'completions_log': False, 'toast': False},
                         {'completions_log': 1, 'toast': 'true'}):
            self.settings = settings
            with mock.patch.object(completions, '_append') as append, mock.patch.object(completions, '_toast') as toast:
                completions.notify(jid, receipt)
                append.assert_not_called()
                toast.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})

    def test_absent_policy_key_is_inert(self):
        jid, receipt, _ = self.job()
        self.policy.side_effect = None
        self.policy.return_value = {}
        completions.notify(jid, receipt)
        self.assertEqual(self.rows(), [])
        self.assertEqual(list(Path(common.STATE).iterdir()), [])
        self.launch.assert_not_called()

    def test_one_row_for_every_terminal_receipt(self):
        for status in sorted(jobs.TERMINAL):
            jid, receipt, folder = self.job(status, failure_class='fixture')
            # Only the allowlisted metadata goes to the sink.
            receipt.update(brief='PRIVATE BRIEF', final='PRIVATE FINAL', error='PRIVATE ERROR')
            common.write_json(str(folder / 'result.json'), receipt)
            completions.notify(jid, receipt)
        rows = self.rows()
        self.assertEqual(len(rows), len(jobs.TERMINAL))
        self.assertEqual({row['status'] for row in rows}, jobs.TERMINAL)
        self.assertEqual(set(rows[0]), {'id', 'name', 'engine', 'kind', 'status', 'failure_class', 'owner', 'ended_at'})
        self.assertNotIn('PRIVATE', Path(completions._path()).read_text())

    def test_unresolved_engine_custody_publishes_nothing(self):
        # A timeout receipt whose engine outlived the kill, or whose launch/tree is unresolved, is not finished.
        for marker, body in ((jobs.SURVIVOR, {'pid': 'unreadable'}),
                             (jobs.TREE_UNRESOLVED, {}), (jobs.CHILD_STARTING, {'pid': None})):
            jid, receipt, folder = self.job('timeout')
            common.write_json(str(folder / marker), body)
            completions.notify(jid, receipt)
            self.assertEqual(self.rows(), [], marker)
            self.assertIn('engine custody reads', (folder / 'runner.log').read_text(encoding='utf-8'))
        jid, receipt, _ = self.job('timeout')
        completions.notify(jid, receipt)
        self.assertEqual([row['id'] for row in self.rows()], [jid])

    def test_nonterminal_and_invalid_receipts_do_nothing(self):
        for status in ('running', 'queued', 'unknown', 'orphaned', 'stalled', []):
            jid, receipt, _ = self.job(status)
            completions.notify(jid, receipt)
        completions.notify('wrong', {})
        self.policy.assert_not_called()
        self.assertEqual(list(Path(common.STATE).iterdir()), [])
        self.launch.assert_not_called()

    def test_reads_final_bytes_and_spec_fallbacks(self):
        jid, receipt, folder = self.job('refused')
        spec = common.read_json(str(folder / 'job.json'))
        spec.update(name='owner task', owner='lead-lane')
        common.write_json(str(folder / 'job.json'), spec)
        final = dict(id=jid, status='refused', finished_at='2026-10-26T12:01:00Z')
        common.write_json(str(folder / 'result.json'), final)
        completions.notify(jid, receipt)
        self.assertEqual(self.rows(), [dict(id=jid, status='refused', name='owner task', owner='lead-lane',
                                          engine='codex', kind='code', ended_at=final['finished_at'])])

    def test_unpublished_or_mismatching_receipt_is_skipped(self):
        jid, receipt, folder = self.job()
        (folder / 'result.json').unlink()
        completions.notify(jid, receipt)
        self.assertEqual(self.rows(), [])
        self.assertIn('final receipt unavailable', (folder / 'runner.log').read_text())
        common.write_json(str(folder / 'result.json'), dict(receipt, status='running'))
        completions.notify(jid, receipt)
        self.assertEqual(self.rows(), [])

    def test_failed_append_does_not_change_receipt_or_stop_toast(self):
        jid, receipt, folder = self.job()
        before = (folder / 'result.json').read_bytes()
        self.settings['toast'] = True
        for error in (OSError('synthetic'), TimeoutError('busy')):
            with mock.patch.object(completions, '_append', side_effect=error), \
                    mock.patch.object(completions, '_toast', return_value=True) as toast:
                completions.notify(jid, receipt)
                toast.assert_called_once()
        self.assertEqual(before, (folder / 'result.json').read_bytes())
        self.assertIn('append failed', (folder / 'runner.log').read_text())

    def test_failed_toast_does_not_change_receipt_or_log(self):
        jid, receipt, folder = self.job()
        before = (folder / 'result.json').read_bytes()
        self.settings['toast'] = True
        with mock.patch.object(completions, '_toast', side_effect=OSError('synthetic')):
            completions.notify(jid, receipt)
        self.assertEqual(before, (folder / 'result.json').read_bytes())
        self.assertEqual(len(self.rows()), 1)
        self.assertIn('toast launch failed', (folder / 'runner.log').read_text())

    def test_concurrent_appends_keep_lines_whole(self):
        rows = [dict(id=f'20261026-120000-codex-{n:04x}', name='line\n' + 'x' * 4000, status='done')
                for n in range(24)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(completions._append, rows))
        actual = self.rows()
        self.assertEqual(len(actual), 24)
        self.assertEqual({row['id'] for row in actual}, {row['id'] for row in rows})
        before = Path(completions._path()).read_bytes()
        completions._append({'id': 'later'})
        self.assertTrue(Path(completions._path()).read_bytes().startswith(before))

    def test_partial_tail_is_preserved_and_next_row_readable(self):
        path = Path(completions._path())
        path.write_bytes(b'{"partial":')
        jid, receipt, _ = self.job()
        completions.notify(jid, receipt)
        self.assertTrue(path.read_bytes().startswith(b'{"partial":\n'))
        code, output = self.call('--json')
        self.assertEqual(code, 0)
        self.assertEqual([row['id'] for row in json.loads(output)], [jid])

    def test_toast_payload_is_data_and_launch_is_windowless_nonblocking(self):
        jid, receipt, folder = self.job()
        name = "quotes '\" ` $(Remove-Item *)\n<tag>&\r\x00"
        receipt['name'] = name
        common.write_json(str(folder / 'result.json'), receipt)
        self.settings = {'toast': True}
        with mock.patch.object(common, 'IS_WIN', True):
            completions.notify(jid, receipt)
        self.launch.assert_called_once()
        argv = self.launch.call_args.args[0]
        data = json.loads(base64.b64decode(argv[2]))
        self.assertEqual(set(data), {'id', 'name', 'status'})
        self.assertEqual(data['id'], jid)
        self.assertIn('$(Remove-Item *)', data['name'])
        self.assertNotIn('\n', data['name'])
        self.assertNotIn('\x00', data['name'])
        self.assertEqual(argv[1], str(Path(completion_toast.__file__).resolve()))
        opts = self.launch.call_args.kwargs
        self.assertTrue(opts['creationflags'] & completion_toast.NO_WINDOW)
        self.assertTrue(opts['creationflags'] & 0x01000000)
        self.launch.return_value.wait.assert_not_called()
        self.launch.return_value.communicate.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_toast_uses_fixed_script_and_separate_stdin(self):
        jid, _, folder = self.job()
        data = completion_toast.payload(jid, "'`$(bad)\n<xml>", 'done')
        with mock.patch.object(completion_toast.subprocess, 'run', return_value=mock.Mock(returncode=0)) as run:
            completion_toast.run(data, str(folder / 'runner.log'))
        args, opts = run.call_args
        script = base64.b64decode(args[0][-1]).decode('utf-16-le')
        self.assertEqual(script, completion_toast.SCRIPT)
        self.assertNotIn('$(bad)', script)
        self.assertEqual(opts['input'], data)
        self.assertEqual(opts['timeout'], completion_toast.TIMEOUT_S)
        self.assertEqual(opts['creationflags'], completion_toast.NO_WINDOW)
        self.assertIn('.InnerText', script)

    def test_toast_timeout_has_no_receipt_delay_or_mutation(self):
        jid, receipt, folder = self.job()
        before = (folder / 'result.json').read_bytes()
        self.settings = {'toast': True}
        with mock.patch.object(common, 'IS_WIN', True):
            completions.notify(jid, receipt)
        self.launch.return_value.wait.assert_not_called()
        with mock.patch.object(completion_toast.subprocess, 'run',
                               side_effect=subprocess.TimeoutExpired('powershell', completion_toast.TIMEOUT_S)):
            completion_toast.run(completion_toast.payload(jid, '', 'done'), str(folder / 'runner.log'))
        self.assertEqual(before, (folder / 'result.json').read_bytes())
        self.assertIn('toast timed out', (folder / 'runner.log').read_text())

    def test_non_windows_toast_is_skipped(self):
        jid, receipt, folder = self.job()
        self.settings = {'toast': True}
        with mock.patch.object(common, 'IS_WIN', False):
            completions.notify(jid, receipt)
        self.launch.assert_not_called()
        self.assertIn('platform', (folder / 'runner.log').read_text())

    def test_toast_validates_and_bounds_text(self):
        jid, _, _ = self.job()
        for bad in ('../x', jid + "'$(bad)"):
            with self.assertRaises(ValueError):
                completion_toast.payload(bad, '', 'done')
        with self.assertRaises(ValueError):
            completion_toast.payload(jid, '', 'running')
        self.assertEqual(len(json.loads(completion_toast.payload(jid, 'x' * 500, 'done'))['name']), 200)
        # C0, DEL, C1 (NEL U+0085, CSI U+009B) and Unicode line/paragraph separators all become spaces.
        name = json.loads(completion_toast.payload(jid, 'a\x00b\x7fc\x85d\x9be f g', 'done'))['name']
        self.assertEqual(name, 'a b c d e f g')

    def runner(self, jid, claimed):
        held = {'runner': False, 'writer': False}

        def claim_runner(_):
            held['runner'] = True

        def claim_writer(_):
            held['writer'] = True
            return 'owned-writer'

        patches = [mock.patch.object(jobs, '_claim_runner', side_effect=claim_runner),
                   mock.patch.object(jobs, '_claim_writer', side_effect=claim_writer),
                   mock.patch.object(jobs, '_release_runner', side_effect=lambda _: held.update(runner=False)),
                   mock.patch.object(jobs, '_release_writer', side_effect=lambda *a: held.update(writer=False)),
                   mock.patch('twinlane.wip.pump_quietly'),
                   mock.patch.object(jobs, '_run_claimed', side_effect=claimed)]
        for patch in patches:
            patch.start()
            self.addCleanup(patch.stop)
        return held

    def test_runner_emits_once_after_final_rewrite_and_all_locks_release(self):
        jid, receipt, folder = self.job()
        (folder / 'result.json').unlink()

        def claimed(*_):
            jobs._write_runner_receipt(jid, receipt)
            self.assertEqual(self.rows(), [])
            receipt['notes'].append('baton end: fixture')
            jobs._write_runner_receipt(jid, receipt)
            return receipt

        held = self.runner(jid, claimed)
        original = completions._append

        def append(row):
            self.assertFalse(any(held.values()))
            self.assertFalse(getattr(jobs._ARCHIVE_LOCKS, 'held', set()))
            self.assertEqual(common.read_json(str(folder / 'result.json'))['notes'], ['baton end: fixture'])
            original(row)

        with mock.patch.object(completions, '_append', side_effect=append) as sink:
            self.assertEqual(jobs.run_job(jid), receipt)
            self.assertEqual(sink.call_count, 1)
            # An already-published job never runs work or emits a second completion.
            self.assertTrue(jobs.run_job(jid)['duplicate_runner'])
            self.assertEqual(sink.call_count, 1)
        self.assertEqual(len(self.rows()), 1)

    def test_stalled_attempt_is_not_a_new_terminal_status(self):
        jid, receipt, folder = self.job('failed', failure_class='stalled')
        (folder / 'result.json').unlink()
        spec = common.read_json(str(folder / 'job.json'))
        attempt = dict(n=1, engine='codex', status='stalled', error='fixture stall', stall_kill_confirmed=True)
        with mock.patch.object(jobs, 'CONTAIN', False), \
                mock.patch.object(common, 'is_linked_worktree', return_value=False), \
                mock.patch.object(jobs, '_baton', return_value=None), \
                mock.patch.object(jobs, 'acquire_slot', return_value=False), \
                mock.patch.object(jobs, 'preflight', return_value=(None, None)), \
                mock.patch.object(jobs, 'tripwire_snapshot', return_value={}), \
                mock.patch.object(jobs, 'tripwire_diff', return_value={}), \
                mock.patch.object(jobs, 'run_attempt', side_effect=lambda *a: dict(attempt)), \
                mock.patch.object(jobs, '_survivor', return_value=None), \
                mock.patch.object(jobs, '_job_usage', return_value=(None, None)), \
                mock.patch('twinlane.watchdog.STALL_REQUEUES', 0):
            final = jobs._run_claimed(jid, spec)
        self.assertEqual(final['status'], 'failed')
        self.assertEqual(self.rows(), [])  # attempts / receipt helpers are not publication hooks
        completions.notify(jid, final)
        self.assertEqual(len(self.rows()), 1)

    def test_background_refusal_hook_follows_runner_release_once(self):
        jid, _, folder = self.job('refused')
        (folder / 'result.json').unlink()
        held = {'runner': True}

        def append(row):
            self.assertFalse(held['runner'])
            self.assertTrue((folder / 'result.json').is_file())

        with mock.patch.object(jobs, '_claim_runner', return_value=None), \
                mock.patch.object(jobs, '_release_runner', side_effect=lambda _: held.update(runner=False)), \
                mock.patch.object(completions, '_append', side_effect=append) as sink, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(jobs._refuse_background(jid, {'engine': 'codex'}, True, OSError('fixture')), 3)
            self.assertEqual(sink.call_count, 1)
            jobs._refuse_background(jid, {'engine': 'codex'}, True, OSError('fixture'))
            self.assertEqual(sink.call_count, 1)

    def test_stop_hook_follows_final_rewrite_and_runner_release_once(self):
        jid, _, folder = self.job()
        (folder / 'result.json').unlink()
        held = {'runner': True}
        original = completions._append

        def append(row):
            self.assertFalse(held['runner'])
            self.assertFalse(getattr(jobs._ARCHIVE_LOCKS, 'held', set()))
            self.assertEqual(common.read_json(str(folder / 'result.json'))['notes'][-1], 'baton end: fixture')
            original(row)

        with mock.patch.object(jobs, '_runner_pid', return_value=(None, None)), \
                mock.patch.object(jobs, '_claim_runner', return_value=None), \
                mock.patch.object(jobs, '_release_runner', side_effect=lambda _: held.update(runner=False)), \
                mock.patch.object(jobs, '_baton', return_value='fixture'), \
                mock.patch.object(completions, '_append', side_effect=append) as sink, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(jobs.cmd_stop([jid, '--json']), 0)
            self.assertEqual(sink.call_count, 1)
        self.assertEqual(self.rows()[0]['status'], 'stopped')

    def test_off_reuses_runner_policy_without_an_extra_read(self):
        jid, receipt, folder = self.job()
        (folder / 'result.json').unlink()

        def claimed(*_):
            jobs._COMPLETION_POLICY.set({})
            jobs._write_runner_receipt(jid, receipt)
            return receipt

        self.runner(jid, claimed)
        self.assertEqual(jobs.run_job(jid), receipt)
        self.policy.assert_not_called()
        self.launch.assert_not_called()
        self.assertEqual(self.rows(), [])

    def test_since_unacked_and_ack_agree_for_valid_corrupt_and_stale_markers(self):
        jid, receipt, folder = self.job()
        completions.notify(jid, receipt)
        self.assertEqual(self.call('--since', '2026-10-26T12:00:01Z', '--json'), (0, '[]\n'))
        self.assertEqual(len(json.loads(self.call('--since', receipt['ended_at'], '--json')[1])), 1)
        for stamp in (None, 'garbage', '2026-10-25T12:00:00Z', '2026-10-26T12:00:00Z'):
            marker = folder / jobs.COLLECTED
            if stamp is None:
                marker.unlink(missing_ok=True)
            else:
                marker.write_text(stamp, encoding='utf-8')
            expected = not ack.collected(jid, receipt)
            self.assertEqual(bool(json.loads(self.call('--unacked', '--json')[1])), expected)
            output = io.StringIO()
            with redirect_stdout(output):
                self.assertEqual(ack.cli([jid, '--dry-run']), 0)
            self.assertEqual('would stamp' in output.getvalue(), expected)
        (folder / jobs.COLLECTED).unlink()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(ack.cli([jid]), 0)
        self.assertEqual(json.loads(self.call('--unacked', '--json')[1]), [])

    def test_cli_is_read_only_even_with_absent_state(self):
        before = {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()}
        with mock.patch.object(common, 'ensure_dirs', side_effect=AssertionError('write')), \
                redirect_stdout(io.StringIO()):
            self.assertEqual(cli.main(['completions', '--unacked', '--json']), 0)
        self.assertEqual(before, {str(p): p.read_bytes() for p in self.home.rglob('*') if p.is_file()})
        for args in (['--since', 'invalid'], ['--unacked', '--since', '2026-10-26T00:00:00Z'], ['--notify', 'cmd']):
            self.assertEqual(self.call(*args)[0], common.EXIT_USAGE)

    def test_worker_guard_allows_reads_and_blocks_settings_log_writes(self):
        self.assertIsNone(guard.self_hazard(['twinlane', 'completions', '--unacked']))
        root = str(self.home / 'worktree')
        for path in (str(self.home / 'config.json'), completions._path()):
            self.assertIsNotNone(guard.evaluate('Write', {'file_path': path, 'content': '{}'}, 'code', root, []))
        self.assertIsNotNone(guard.self_hazard(['twinlane', '_job', 'job-id']))


if __name__ == '__main__':
    unittest.main()
