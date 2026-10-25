"""Tests for twinlane.jobs. No network, no model calls: fake claude / fake optional adapter only."""
import datetime as dt
import glob
import io
import itertools
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
from contextlib import contextmanager, redirect_stderr, redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))

import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import common, jobs  # noqa: E402

FAKE_CLAUDE = os.path.join(HERE, 'fake_claude.py')
FAKE_CODEX = os.path.join(HERE, 'fake_codex_run.py')
FIXTURES = os.path.join(HERE, 'fixtures')
POLICY = common.read_json(common.POLICY)
WRITE_TOOLS = {'Edit', 'Write', 'MultiEdit', 'NotebookEdit', 'Bash', 'PowerShell'}
ARABIC = 'مرحبا بالعالم'


def meter_with(claude='ok', codex='ok', codex_age_s=5):
    return {'read_at': common.iso(common.now()),
            'claude': {'status': claude, 'max_pct': 10, 'next_reset': '2026-10-08T20:00:00Z', 'age_s': 5,
                       'extra_usage_enabled': False},
            'codex': {'status': codex, 'max_pct': 10, 'next_reset': '2026-10-09T08:00:00Z', 'age_s': codex_age_s}}


def fake_decide(kind, meter, policy, urgent, engine):
    """A stand-in for route.decide with the same contract (route.py is built in parallel)."""
    usable = ('ok', 'caution', 'handoff')
    primary = engine or policy['routing'][kind]['primary']
    chosen = primary if meter[primary]['status'] in usable else None
    if chosen is None and engine is None:
        fb = policy['routing'][kind]['fallback']
        chosen = fb if meter[fb]['status'] in usable else None
    if chosen is None:
        return {'engine': None, 'refused': True, 'reason': f"{primary} {meter[primary]['status']}",
                'wait_until': '2026-10-08T20:00:00Z'}
    return {'engine': chosen, 'refused': False, 'reason': 'test route', 'fallback_used': chosen != primary,
            'model': policy['claude']['models']['default'] if chosen == 'claude' else None,
            'effort': policy['claude']['effort_by_kind'].get(kind) if chosen == 'claude' else None,
            'tier': policy['codex']['tier_by_kind'].get(kind) if chosen == 'codex' else None,
            'profile': 'code' if kind == 'code' else 'intel', 'wait_until': None}


def git(cwd, *args):
    return subprocess.run(['git', '-C', cwd, '-c', 'user.name=t', '-c', 'user.email=user@example.com',
                           '-c', 'commit.gpgsign=false', '-c', 'core.hooksPath=', *args],
                          capture_output=True, text=True, encoding='utf-8')


def make_repo(base):
    repo = os.path.join(base, 'repo')
    os.makedirs(repo)
    git(repo, 'init', '-q', '-b', 'main')
    with open(os.path.join(repo, 'a.txt'), 'w', encoding='utf-8') as h:
        h.write('one\n')
    git(repo, 'add', 'a.txt')
    git(repo, 'commit', '-q', '--no-verify', '-m', 'init')
    return repo


def add_worktree(repo, base):
    wt = os.path.join(base, 'wt')
    r = git(repo, 'worktree', 'add', '-q', '-b', 'wt-branch', wt)
    assert r.returncode == 0, r.stderr
    return wt


def unreadable_identity(*pids):
    """These pids exist but their creation time cannot be read (a restricted-token sandbox);
    other pids are probed for real. No pids = every pid."""
    if common.IS_WIN:
        real = common._win_process_info
        return mock.patch.object(common, '_win_process_info',
                                 side_effect=lambda pid: (True, None) if not pids or pid in pids else real(pid))
    real = common._posix_start_time
    return mock.patch.object(common, '_posix_start_time',
                             side_effect=lambda pid: None if not pids or pid in pids else real(pid))


class JobsBase(unittest.TestCase):
    ENV_KEYS = ('TWINLANE_HOME', 'TWINLANE_CONFIG', 'TWINLANE_CLAUDE_BIN', 'TWINLANE_CODEX_RUN', 'ANTHROPIC_API_KEY', 'ANTHROPIC_BASE_URL',
                'FAKE_LIMIT', 'FAKE_SERVED_MODEL', 'FAKE_SESSION_ID', 'FAKE_SLEEP', 'FAKE_SLEEP_ONCE', 'FAKE_TOUCH', 'FAKE_ARGV_OUT',
                'FAKE_CODEX_LIMIT', 'FAKE_CODEX_EXIT', 'FAKE_CODEX_ARGV_OUT', 'FAKE_TOUCH_NO_TOOL_USE',
                'FAKE_TOOL_USE', 'FAKE_BAD_EVENTS', 'FAKE_CODEX_FINAL', 'TWINLANE_CODEX_ADAPTER_STRICT_CREDITS',
                'TWINLANE_CODEX_BIN', 'LOCALAPPDATA', 'TWINLANE_TEST_ISOLATION', 'FAKE_CODEX_USAGE', 'FAKE_CODEX_ROLLOUT_ROOT',
                'TWINLANE_FAKE_API_TOKEN')

    def fresh_jobs(self):
        """A new empty jobs dir per subtest. Deleting and recreating one dir raced Windows' delayed
        delete (WinError 145, directory is not empty); tearDown removes them all."""
        common.JOBS = tempfile.mkdtemp(prefix='jobs-', dir=self.home)

    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='twinlane-jobs-'))
        self.home = os.path.join(self.tmp, 'home')
        self._saved = {k: getattr(common, k) for k in ('STATE', 'JOBS', 'BATONS', 'CODEX_RUN', 'BIN', 'CODEX_DIR')}
        # Keep all test state and vendor file lookups in disposable paths.
        common.BIN = os.path.join(self.home, 'bin')
        common.CODEX_DIR = os.path.join(self.tmp, 'dotcodex')
        self._env = {k: os.environ.get(k) for k in self.ENV_KEYS}
        self._hooks = (jobs._read_meter, jobs._decide, jobs._record_baton, jobs.POLL_S, jobs.SLOT_POLL_S,
                       jobs.CONTAIN)
        jobs.CONTAIN = False  # never put the test runner itself in a kill-on-close job object
        common.STATE, common.JOBS, common.BATONS = (os.path.join(self.home, d) for d in ('state', 'jobs', 'batons'))
        common.CODEX_RUN = FAKE_CODEX
        common.ensure_dirs()
        for k in self.ENV_KEYS:
            os.environ.pop(k, None)
        os.environ.update({'TWINLANE_HOME': self.home, 'TWINLANE_CLAUDE_BIN': FAKE_CLAUDE,
                           'TWINLANE_CODEX_RUN': FAKE_CODEX, 'ANTHROPIC_API_KEY': 'not-a-real-key',
                           'ANTHROPIC_BASE_URL': 'http://127.0.0.1:9',
                           'LOCALAPPDATA': os.path.join(self.tmp, 'localappdata'), 'TWINLANE_TEST_ISOLATION': '1'})
        # The optional adapter receives only fake values.
        self.fake_codex_bin = os.path.join(self.tmp, 'fake-codex.exe')
        with open(self.fake_codex_bin, 'wb') as h:
            h.write(b'fixture executable placeholder')
        os.environ['TWINLANE_CODEX_BIN'] = self.fake_codex_bin
        self.meter = meter_with()
        jobs._read_meter = lambda policy: self.meter
        jobs._decide = fake_decide
        self.baton_calls = []
        jobs._record_baton = lambda slug, receipt: self.baton_calls.append((slug, receipt.get('status')))
        jobs.POLL_S, jobs.SLOT_POLL_S = 0.1, 0.1
        self.work = os.path.join(self.tmp, 'work')
        os.makedirs(self.work)
        with open(os.path.join(self.work, 'seed.txt'), 'w', encoding='utf-8') as h:
            h.write('a plain, non-repo working folder; an empty one is refused as a leftover\n')
        self.brief = os.path.join(self.tmp, 'task-brief.md')
        with open(self.brief, 'w', encoding='utf-8') as h:
            h.write(f'Investigate the thing.\nECHO: {ARABIC}\n')
        self.argv_out = os.path.join(self.tmp, 'claude-argv.json')
        self.codex_argv_out = os.path.join(self.tmp, 'codex-argv.json')
        os.environ['FAKE_ARGV_OUT'] = self.argv_out
        os.environ['FAKE_CODEX_ARGV_OUT'] = self.codex_argv_out

    def tearDown(self):
        for k, v in self._saved.items():
            setattr(common, k, v)
        for k, v in self._env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        (jobs._read_meter, jobs._decide, jobs._record_baton, jobs.POLL_S, jobs.SLOT_POLL_S,
         jobs.CONTAIN) = self._hooks
        for _ in range(10):
            shutil.rmtree(self.tmp, ignore_errors=True)
            if not os.path.exists(self.tmp):
                break
            time.sleep(0.3)

    def cli(self, cmd, *argv):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            rc = jobs.cli(cmd, list(argv))
        return rc, out.getvalue(), err.getvalue()

    def write_config(self, config):
        common.write_json(os.path.join(self.home, 'config.json'), config)

    def only_job(self):
        ids = jobs._job_ids()
        self.assertEqual(len(ids), 1, ids)
        return ids[0]

    def receipt(self, jid):
        return common.read_json(os.path.join(common.JOBS, jid, 'result.json'))

    def read_json_file(self, path):
        with open(path, encoding='utf-8') as h:
            return json.load(h)


class TruthfulErrors(JobsBase):
    def run_codex(self, *extra):
        return self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'parallel', '--engine', 'codex', *extra)

    def test_codex_error_events_are_structured_latest_and_redacted(self):
        lines = [json.dumps({'type': 'turn.failed', 'error': {'message': 'model X requires a newer version of Codex'}})]
        self.assertIn('requires a newer', jobs.codex_error_text(lines))
        lines.append(json.dumps({'type': 'error', 'message': 'newest\n failure'}))
        self.assertEqual(jobs.codex_error_text(lines), 'newest failure')
        self.assertIsNone(jobs.codex_error_text(['{}', 'bad json', json.dumps({'type': 'item.completed', 'message': 'error'})]))
        secret = 'sk-' + 'abcdefghijklmnopqrstuvwxyz1234567890'
        self.assertNotIn(secret, jobs.codex_error_text([json.dumps({'type': 'error', 'error': secret})]))

    def test_codex_failure_receipt_and_result_keep_error_and_finish(self):
        # The fixture has only a special rc=3 knob; wrap its real run to exit 1 afterwards.
        common.CODEX_RUN = os.path.join(self.tmp, 'failed-adapter.py')
        common.write_text(common.CODEX_RUN, f"import runpy, sys\nrunpy.run_path({FAKE_CODEX!r})['main']()\nsys.exit(1)\n")
        real_parse = jobs.parse_codex_out
        message = 'model X requires a newer version of Codex'
        def parse(out_dir):
            os.makedirs(out_dir, exist_ok=True)
            common.write_text(os.path.join(out_dir, 'events.jsonl'), json.dumps({'type': 'turn.failed', 'error': {'message': message}}))
            return real_parse(out_dir)
        with mock.patch.object(jobs, 'parse_codex_out', parse), \
                mock.patch.object(jobs, '_coordination_parent', return_value=None):
            rc, _, _ = self.run_codex()
        self.assertEqual(rc, common.EXIT_FAILED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(r['error'], message)
        self.assertEqual(r['finished_at'], r['ended_at'])
        self.assertTrue(r['finished_at'])
        self.assertEqual(r['attempts'][0]['codex_error'], message)
        self.assertIn('error: ' + message, self.cli('result', r['id'])[1])
        self.assertEqual(json.loads(self.cli('result', r['id'], '--json')[1])['error'], message)

    def test_stderr_error_fallback_and_terminal_finalization(self):
        common.write_text(os.path.join(self.tmp, 'stderr.log'), 'ordinary output\nERROR: broken setup\n')
        self.assertEqual(jobs.parse_codex_out(self.tmp)['error_text'], 'ERROR: broken setup')
        for status in ('failed', 'refused', 'stopped', 'interrupted', 'timeout', 'limit_hit', 'lost'):
            r = jobs._finalize({'status': status, 'attempts': [{'error': 'reason'}]})
            self.assertTrue(r['finished_at'])
            self.assertEqual(r['error'], 'reason')
        self.assertNotIn('finished_at', jobs._finalize({'status': 'running'}))

    def test_claude_success_requires_zero_exit(self):
        s = dict(has_result=True, is_error=False, subtype='success', limit_hit=False)
        for rc, expected in ((0, 'done'), (1, 'failed'), (127, 'failed')):
            self.assertEqual(jobs._claude_status(s, rc, False), expected)
        self.assertEqual(jobs._claude_status(dict(s, has_result=False, limit_hit=True), 1, False), 'limit_hit')
        self.assertEqual(jobs._claude_status(s, None, True), 'timeout')

    def test_receipt_cannot_complete_after_guard_violation_or_nonzero_exit(self):
        spec = dict(id='job-demo', primary={'engine': 'claude'}, profile='code', kind='code', cwd=self.work)
        for extra in ({'disallowed_tool_uses': [{'tool': 'Bash', 'reason': 'commit refused'}]}, {'exit_code': 1}):
            receipt = jobs.build_receipt(spec, [dict(status='done', engine='claude', **extra)], {}, common.now(), None, [])
            self.assertEqual(receipt['status'], 'failed')
            self.assertTrue(receipt['error'])

    def test_runtime_version_refusal_is_policy_refusal(self):
        with mock.patch.object(common, 'acquire_codex_binary', return_value=(self.fake_codex_bin, '', None)), \
                mock.patch.object(common, 'codex_version_refusal', return_value='too old'):
            r = jobs._run_codex('job-demo', [], self.work, 1, {}, {'profile': 'intel'}, POLICY)
        self.assertEqual((r['status'], r['exit_code']), ('refused', common.EXIT_REFUSED))
        self.assertIn('too old', r['error'])

    def test_claude_success_event_with_nonzero_process_exit_fails(self):
        binary = os.path.join(self.tmp, 'failed-claude.py')
        common.write_text(binary, f"import runpy, sys\nrunpy.run_path({FAKE_CLAUDE!r})['main']()\nsys.exit(1)\n")
        os.environ['TWINLANE_CLAUDE_BIN'] = binary
        with mock.patch.object(jobs, '_coordination_parent', return_value=None):
            rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, common.EXIT_FAILED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertEqual(r['error'], 'claude reported success but exited rc 1')

    @unittest.skipUnless(common.IS_WIN, 'Windows app builds')
    def test_older_env_binary_dispatch_uses_stub_versions_and_explicit_opt_in(self):
        folder = os.path.join(os.environ['LOCALAPPDATA'], 'OpenAI', 'Codex', 'bin', 'build-demo')
        os.makedirs(folder)
        for filename in common.CODEX_FILES:
            common.write_text(os.path.join(folder, filename), 'print("codex-cli 0.162.0")\n')
        override = os.path.join(self.tmp, 'old-codex.py')
        common.write_text(override, 'print("codex-cli 0.149.1")\n')
        os.environ['TWINLANE_CODEX_BIN'] = override
        def version(path):
            # The app fixture is a Python script named codex.exe: no installed CLI runs.
            r = subprocess.run([sys.executable, path, '--version'], capture_output=True, text=True)
            match = common._VERSION_RE.search(r.stdout)
            return match.group(0) if r.returncode == 0 and match else None
        with mock.patch.object(common, '_read_codex_version', side_effect=version), \
                mock.patch.object(jobs, '_coordination_parent', return_value=None):
            rc, out, err = self.run_codex('--json')
            self.assertEqual(rc, common.EXIT_REFUSED)
            self.assertIn('0.149.1', out + err)
            self.assertIn('0.162.0', out + err)
            self.assertEqual(jobs._job_ids(), [])
            self.write_config({'codex': {'allow_older_env_bin': True}})
            self.assertEqual(self.run_codex()[0], 0)
            self.fresh_jobs()
            shutil.rmtree(folder)
            self.write_config({'codex': {'min_cli_version': '0.150.0'}})
            self.assertEqual(self.run_codex()[0], common.EXIT_REFUSED)
            self.assertEqual(jobs._job_ids(), [])

    def test_preflight_refuses_before_job_allocation_including_dry_run(self):
        for extra in ((), ('--dry-run',), ('--json',)):
            with mock.patch.object(common, 'codex_preflight', return_value=dict(refusal='CLI 0.149.1 older than app 0.162.0')), \
                    mock.patch.object(jobs, '_coordination_parent', return_value=None):
                rc, out, err = self.run_codex(*extra)
            self.assertEqual(rc, common.EXIT_REFUSED)
            self.assertIn('0.149.1', out + err)
            self.assertIn('0.162.0', out + err)
            self.assertEqual(jobs._job_ids(), [])

    def test_failover_records_codex_preflight_refusal_without_attempt(self):
        os.environ['FAKE_LIMIT'] = '1'
        with mock.patch.object(common, 'codex_preflight', return_value={'refusal': 'old CLI'}), \
                mock.patch.object(jobs, '_coordination_parent', return_value=None):
            self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'intel')
        attempts = self.receipt(self.only_job())['attempts']
        self.assertEqual(len(attempts), 1)
        self.assertEqual(attempts[0]['failover_refused'], 'old CLI')

    def test_writer_claims_share_worktree_root_and_cross_check_git(self):
        repo = make_repo(self.tmp)
        wt = add_worktree(repo, self.tmp)
        sub = os.path.join(wt, 'sub')
        os.makedirs(sub)
        first = dict(id='writer-one', cwd=sub, profile='code')
        path = jobs._claim_writer(first)
        with self.assertRaisesRegex(jobs.Refusal, 'writer directory is owned by writer-one'):
            jobs._claim_writer(dict(first, id='writer-two', cwd=wt))
        other = os.path.join(self.tmp, 'second-wt')
        self.assertEqual(git(repo, 'worktree', 'add', '-q', '-b', 'second', other).returncode, 0)
        jobs._claim_writer(dict(first, id='writer-three', cwd=other))
        self.assertEqual(common.read_json(path)['cwd'], common.writer_key(wt))
        with mock.patch.object(common, 'git', return_value=(0, other)):
            with self.assertRaisesRegex(jobs.Refusal, 'cannot establish the worktree root'):
                jobs._claim_writer(dict(first, id='writer-four'))


class StreamParsing(unittest.TestCase):
    def fixture(self, name):
        with open(os.path.join(FIXTURES, name), encoding='utf-8') as h:
            return h.read().splitlines()

    def test_model_calls_count_distinct_responses_not_cli_turns(self):
        # One response split over two content-block events counts once; a subagent's responses count
        # apart; an event without an id counts on its own; num_turns stays the CLI's own counter.
        ev = [
            {'type': 'assistant', 'message': {'id': 'm1', 'content': [{'type': 'text', 'text': 'a'}]}},
            {'type': 'assistant', 'message': {'id': 'm1', 'content': [{'type': 'tool_use', 'id': 't1', 'name': 'Read', 'input': {}}]}},
            {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 't1'}]}},
            {'type': 'assistant', 'message': {'id': 'm2', 'content': [{'type': 'text', 'text': 'b'}]}},
            {'type': 'assistant', 'parent_tool_use_id': 't9', 'message': {'id': 's1', 'content': []}},
            {'type': 'assistant', 'parent_tool_use_id': 't9', 'message': {'id': 's1', 'content': []}},
            {'type': 'assistant', 'message': {'content': [{'type': 'text', 'text': 'c'}]}},
            {'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 14, 'result': 'done'},
        ]
        s = jobs.parse_stream_lines([json.dumps(e) for e in ev])
        self.assertEqual(s['model_calls'], 3)
        self.assertEqual(s['subagent_model_calls'], 1)
        self.assertEqual(s['num_turns'], 14)

    def test_init_and_result(self):
        s = jobs.parse_stream_lines(self.fixture('jobs_stream_ok.jsonl'))
        self.assertEqual(s['session_id'], '11111111-2222-3333-4444-555555555555')
        self.assertEqual(s['init_model'], 'claude-opus-5-5')
        self.assertEqual(s['permission_mode'], 'dontAsk')
        self.assertEqual(s['tools_count'], 4)
        self.assertEqual(s['num_turns'], 7)
        self.assertEqual(s['subtype'], 'success')
        self.assertFalse(s['is_error'])
        self.assertEqual(s['served_models'], ['claude-opus-5-5', 'claude-haiku-4-5'])
        self.assertEqual(s['permission_denials'], 2)
        self.assertEqual(s['duration_api_ms'], 8000)
        self.assertIn(ARABIC, s['final_text'])
        self.assertFalse(s['limit_hit'])
        self.assertEqual(s['bad_lines'], 1)

    def test_rate_limit_event_rejected(self):
        s = jobs.parse_stream_lines(self.fixture('jobs_stream_limit.jsonl'))
        self.assertTrue(s['limit_hit'])
        self.assertEqual(jobs._claude_status(s, 0, False), 'limit_hit')

    def test_allowed_rate_limit_event_is_not_a_hit(self):
        s = jobs.parse_stream_lines(['{"type":"rate_limit_event","rate_limit_info":{"status":"allowed_warning"}}'])
        self.assertFalse(s['limit_hit'])

    def test_limit_text_needs_is_error(self):
        hit = jobs.parse_stream_lines(['{"type":"result","subtype":"success","is_error":true,'
                                       '"result":"Claude usage limit reached"}'])
        self.assertTrue(hit['limit_hit'])
        benign = jobs.parse_stream_lines(['{"type":"result","subtype":"success","is_error":false,'
                                          '"result":"I documented the rate limit handling"}'])
        self.assertFalse(benign['limit_hit'])
        self.assertEqual(jobs._claude_status(benign, 0, False), 'done')

    def test_no_result_is_failed_and_timeout_wins(self):
        s = jobs.parse_stream_lines(['{"type":"system","subtype":"init","session_id":"x","model":"m"}'])
        self.assertEqual(jobs._claude_status(s, 1, False), 'failed')
        self.assertEqual(jobs._claude_status(s, None, True), 'timeout')
        self.assertEqual(s['served_models'], ['m'])  # falls back to the init model
        self.assertIn('unverified', s['served_models_note'])

    def test_codex_out_parsing(self):
        c = jobs.parse_codex_out(os.path.join(FIXTURES, 'jobs_codex_limit'))
        self.assertTrue(c['limit_hit'])
        self.assertEqual(c['session_id'], 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee')
        self.assertEqual(c['model'], 'gpt-6-sol')


class ArgvAndEnv(JobsBase):
    def params(self, profile):
        return jobs.engine_params('claude', 'code' if profile == 'code' else 'intel', {}, POLICY, profile)

    def allowed(self, argv):
        i, j = argv.index('--allowedTools'), argv.index('--disallowedTools')
        return argv[i + 1:j], argv[j + 1:]

    def test_intel_profile_is_read_only(self):
        argv = jobs.claude_args(self.params('intel'), POLICY)
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'dontAsk')
        allow, deny = self.allowed(argv)
        for tool in POLICY['claude']['intel_allow']:
            self.assertIn(tool, allow)
        self.assertFalse(WRITE_TOOLS & set(allow), allow)
        self.assertEqual(deny[:len(POLICY['claude']['deny'])], POLICY['claude']['deny'])
        for rule in ('Edit', 'Write', 'PowerShell', 'Bash(git diff --output*)',
                     'Bash(rg --pre*)'):
            self.assertIn(rule, deny)
        for flag in ('-p', '--verbose'):
            self.assertIn(flag, argv)
        self.assertEqual(argv[argv.index('--output-format') + 1], 'stream-json')
        self.assertEqual(argv[argv.index('--permission-prompts') + 1], 'none')
        self.assertEqual(argv[argv.index('--max-turns') + 1], str(POLICY['claude']['max_turns_by_profile']['intel']))
        for banned in ('--bare', '--restricted', '--strict-mcp-config'):
            self.assertNotIn(banned, argv)

    def test_intel_filters_write_tools_even_if_policy_lists_them(self):
        bad = json.loads(json.dumps(POLICY))
        bad['claude']['intel_allow'] += ['Edit', 'Write', 'Bash']
        allow, _ = self.allowed(jobs.claude_args(self.params('intel'), bad))
        self.assertFalse(WRITE_TOOLS & set(allow))

    def test_inherited_allow_rules_are_denied_through_the_settings_file(self):
        # project/user allow rules merge on top of --allowedTools; intel must deny the
        # ones that grant more than read-only, and hundreds of them must not overflow the command line.
        os.makedirs(os.path.join(self.work, '.claude'))
        wide = ['Bash(python *)', 'mcp__claude-in-chrome__navigate', 'Read', 'Bash(git log:*)', 'WebFetch(domain:x)']
        wide += [f'Bash(php scripts/_some_long_script_name_number_{i}.php --with --many --flags)' for i in range(600)]
        common.write_json(os.path.join(self.work, '.claude', 'settings.local.json'), {'permissions': {'allow': wide}})
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, 0, out)
        argv = self.read_json_file(self.argv_out)['argv']
        self.assertLess(len(subprocess.list2cmdline(argv)), 30000)
        settings = self.read_json_file(argv[argv.index('--settings') + 1])
        deny = settings['permissions']['deny']
        for rule in ('Bash(python *)', 'mcp__claude-in-chrome__navigate', wide[-1]):
            self.assertIn(rule, deny)
        for rule in ('Read', 'Bash(git log:*)', 'WebFetch(domain:x)'):
            self.assertNotIn(rule, deny)
        self.assertEqual(settings['hooks']['PreToolUse'][0]['matcher'], jobs.GUARD_MATCHER)

    def test_code_profile(self):
        argv = jobs.claude_args(self.params('code'), POLICY, resume='sid-1')
        self.assertEqual(argv[argv.index('--permission-mode') + 1], 'acceptEdits')
        allow, deny = self.allowed(argv)
        self.assertEqual(allow, POLICY['claude']['code_allow'])
        self.assertEqual(deny, POLICY['claude']['deny'])
        self.assertIn('Bash(git commit:*)', deny)
        self.assertEqual(argv[argv.index('--resume') + 1], 'sid-1')
        self.assertIn('--fork-session', argv)

    def test_env_drops_anthropic(self):
        env = jobs.claude_env('job-x')
        self.assertFalse([k for k in env if k.startswith('ANTHROPIC_')])
        self.assertEqual(env['TWINLANE_ENGINE_JOB'], 'job-x')

    def test_py_bin_runs_under_this_python(self):
        self.assertEqual(jobs.claude_launcher(FAKE_CLAUDE), [sys.executable, FAKE_CLAUDE])
        executable = os.path.join(self.tmp, 'claude.exe')
        self.assertEqual(jobs.claude_launcher(executable), [executable])

    def test_codex_args_never_add_credits_by_default(self):
        p = jobs.engine_params('codex', 'code', {}, POLICY, 'code')
        args = jobs.codex_args(p, POLICY, 'b.md', self.work, os.path.join(self.tmp, 'out'))
        self.assertNotIn('--allow-credits', args)
        self.assertIn('--write', args)
        self.assertEqual(args[args.index('--tier') + 1], POLICY['codex']['tier_by_kind']['code'])
        lenient = json.loads(json.dumps(POLICY))
        lenient['codex']['allow_credits'] = True
        self.assertNotIn('--allow-credits', jobs.codex_args(p, lenient, 'b.md', self.work, os.path.join(self.tmp, 'out')))
        intel = jobs.engine_params('codex', 'sweep', {}, POLICY, 'intel')
        self.assertNotIn('--write', jobs.codex_args(intel, POLICY, 'b.md', self.work, os.path.join(self.tmp, 'out')))
        self.assertEqual(jobs.codex_launcher(), [sys.executable, FAKE_CODEX])

    def test_contract_is_profile_specific(self):
        intel, code = jobs.contract('J', 'claude', 'intel'), jobs.contract('J', 'codex', 'code')
        self.assertIn('job J, engine claude, profile intel', intel)
        self.assertIn('do not modify files', intel)
        self.assertNotIn('do not modify files', code)
        for c in (intel, code):
            self.assertIn('do NOT git commit, push, merge, rebase or deploy', c)
            self.assertIn('the exact next step', c)


class RunPlanning(JobsBase):
    def test_code_profile_needs_write(self):
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'code', '--dry-run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('--write', err)

    def test_code_profile_refuses_main_checkout_and_accepts_linked_worktree(self):
        repo = make_repo(self.tmp)
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', repo, '--write', '--dry-run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('LINKED worktree', err)
        wt = add_worktree(repo, self.tmp)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', wt, '--write', '--dry-run', '--json')
        self.assertEqual(rc, 0, out)
        view = json.loads(out)
        self.assertEqual(view['profile'], 'code')
        self.assertIn('acceptEdits', view['argv'])
        self.assertEqual(jobs._job_ids(), [])

    def test_dry_run_creates_nothing(self):
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--dry-run', '--json')
        self.assertEqual(rc, 0)
        view = json.loads(out)
        self.assertTrue(view['dry_run'])
        self.assertEqual(view['engine'], 'claude')
        self.assertEqual(view['argv'][:2], [sys.executable, FAKE_CLAUDE])
        self.assertIn('dontAsk', view['argv'])
        self.assertEqual(os.listdir(common.JOBS), [])
        self.assertFalse(os.path.exists(self.argv_out))
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--dry-run')
        self.assertIn('route: claude profile=intel', out)
        self.assertIn('argv: ', out)

    def test_the_exit_3_refusal_names_the_window_that_caused_it(self):
        # With the real route.decide: seven_day_sonnet alone refuses Claude.
        jobs._decide = None
        m = meter_with(claude='stop', codex='out')
        m['claude'].update(max_pct=90.0, windows=[{'name': 'five_hour', 'pct': 5.0}, {'name': 'seven_day', 'pct': 15.0},
                                                  {'name': 'seven_day_sonnet', 'pct': 90.0}])
        self.meter = m
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('claude stop (90% in 7d-sonnet)', err)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--json')
        self.assertEqual(rc, common.EXIT_REFUSED)
        view = json.loads(out)
        self.assertIn('claude stop (90% in 7d-sonnet)', view['reason'])
        self.assertEqual(view['decision']['blockers'][0]['window'], 'seven_day_sonnet')
        self.assertEqual(os.listdir(common.JOBS), [])

    def test_refused_route_exits_3_and_creates_nothing(self):
        self.meter = meter_with(claude='out', codex='unknown')
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--json')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertTrue(json.loads(out)['refused'])
        self.assertEqual(os.listdir(common.JOBS), [])

    def test_refuses_leftover_worktree_folders_but_not_plain_non_repo_dirs(self):
        empty = os.path.join(self.tmp, 'empty-leftover')
        os.makedirs(empty)
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', empty, '--dry-run')
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('is empty', err)
        dangling = os.path.join(self.tmp, 'dangling-leftover')
        os.makedirs(dangling)
        for pointer in (f'gitdir: {self.tmp}/gone/.git/worktrees/x', 'gitdir: ../gone/.git/worktrees/x'):
            with open(os.path.join(dangling, '.git'), 'w', encoding='utf-8') as h:
                h.write(pointer + '\n')
            rc, _, err = self.cli('run', '--brief', self.brief, '--cd', dangling, '--dry-run')
            self.assertEqual(rc, common.EXIT_USAGE, pointer)
            self.assertIn('missing gitdir', err)
        with open(os.path.join(dangling, '.git'), 'w', encoding='utf-8') as h:
            h.write('not a pointer\n')  # an ordinary file named .git is a plain folder, not a leftover
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', dangling, '--dry-run')
        self.assertEqual(rc, 0, err)
        self.assertEqual(jobs._job_ids(), [])
        # A folder that is not a repo at all stays a legitimate place to run from.
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--dry-run')
        self.assertEqual(rc, 0, out)

    def test_a_codex_tier_name_passed_as_model_is_refused(self):
        for tier in ('luna', 'sol', 'sol-high', 'astra'):
            rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex',
                                    '--model', tier, '--dry-run')
            self.assertEqual(rc, common.EXIT_USAGE, tier)
            self.assertIn(f'--model {tier} is a Codex tier; use --tier {tier}', err)
            self.assertEqual(out, '')
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex', '--model', 'Astra',
                              '--dry-run', '--json')
        self.assertEqual(rc, common.EXIT_USAGE)
        refusal = json.loads(out)
        self.assertTrue(refusal['refused'])
        self.assertEqual(refusal['reason'], '--model Astra is a Codex tier; use --tier astra')
        self.assertEqual(jobs._job_ids(), [])  # a real run is refused the same way, before anything is created
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex', '--model', 'astra')
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertEqual(jobs._job_ids(), [])

    def test_a_codex_model_id_is_still_accepted_and_the_tier_goes_by_tier(self):
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex',
                                '--model', 'gpt-6-astra', '--dry-run', '--json')
        self.assertEqual(rc, 0, err)
        argv = json.loads(out)['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'gpt-6-astra')
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex',
                                '--tier', 'astra', '--dry-run', '--json')
        self.assertEqual(rc, 0, err)
        argv = json.loads(out)['argv']
        self.assertEqual(argv[argv.index('--tier') + 1], 'astra')
        # twinlane resolves a tier to the policy's model for it, so the runner gets that model too.
        self.assertEqual(argv[argv.index('--model') + 1], POLICY['codex']['models']['astra'])

    def test_the_tier_name_check_covers_a_route_that_lands_on_codex(self):
        self.meter = meter_with(claude='out')
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'intel', '--model', 'sol',
                              '--dry-run')
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('--model sol is a Codex tier; use --tier sol', err)
        # Failover during a run never carries the caller's --model to the other engine.
        spec = {'kind': 'intel', 'urgent': False, 'profile': 'intel'}
        params, why = jobs.plan_failover(spec, {'engine': 'claude'}, POLICY)
        self.assertEqual(why, '')
        self.assertEqual(params['model'], POLICY['codex']['models'][params['tier']])  # the tier's model, not --model

    def test_the_claude_engine_is_unaffected_by_the_tier_name_check(self):
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude',
                                '--model', 'sol', '--dry-run', '--json')
        self.assertEqual(rc, 0, err)
        argv = json.loads(out)['argv']
        self.assertEqual(argv[argv.index('--model') + 1], 'sol')
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude',
                                '--model', 'claude-opus-5-5', '--dry-run', '--json')
        self.assertEqual(rc, 0, err)

    def test_bad_usage(self):
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', os.path.join(self.tmp, 'nope'))
        self.assertEqual(rc, common.EXIT_USAGE)
        rc, _, _ = self.cli('run', '--cd', self.work)
        self.assertEqual(rc, common.EXIT_USAGE)
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'dance')
        self.assertEqual(rc, common.EXIT_USAGE)


class RunnerFinalization(JobsBase):
    """Exercise runner completion directly with inert child processes, without dispatch."""

    def run_fixture(self, engine, outcome='done', kill_result=True, survivor=False, stop_requested=None):
        jid = '20261017-000001-' + engine + '-fixture'
        os.makedirs(jobs.job_dir(jid))
        params = {'engine': engine, 'profile': 'intel', 'timeout_s': 2,
                  'max_turns': 2, 'model': 'fixture-model', 'effort': 'low', 'tier': 'luna'}
        spec = {'id': jid, 'engine': engine, 'primary': params, 'profile': 'intel',
                'kind': 'intel', 'cwd': self.work, 'failover': False}
        common.write_json(jobs._p(jid, 'job.json'), spec)
        common.write_text(jobs._p(jid, 'brief.md'), 'fixture brief')
        jobs._write_status(jid, 'queued', engine=engine)
        proc = InlineRuns.EngineProc(os.getpid(), stuck=99 if survivor else int(outcome == 'timeout'),
                                     rc=3 if outcome == 'refused' else 1 if outcome in ('failed', 'limit_hit', 'stopped') else 0,
                                     raises=KeyboardInterrupt if outcome == 'interrupted' else None)
        event = {'type': 'result', 'subtype': 'success' if outcome != 'limit_hit' else 'error_during_execution',
                 'is_error': outcome == 'limit_hit',
                 'result': 'hit your limit' if outcome == 'limit_hit' else 'fixture report'}
        proc.stdout = io.BytesIO((json.dumps(event) + '\n').encode('utf-8'))
        parsed = jobs.parse_codex_out(jobs._p(jid, 'absent'))
        parsed.update(final_text='fixture report', limit_hit=outcome == 'limit_hit')
        # Only the engine launch is inert; the real runner owns its lock, status,
        # launch marker, child cleanup, receipt publication and finalization.
        real_popen = subprocess.Popen
        launched = []
        def popen(argv, *args, **kw):
            if kw.get('env', {}).get('TWINLANE_ENGINE_JOB') == jid:
                launched.append(proc)
                return proc
            return real_popen(argv, *args, **kw)
        # `twinlane stop` while the engine runs leaves only its request (the runner alone writes the receipt).
        requested = outcome == 'stopped' if stop_requested is None else stop_requested
        with mock.patch.object(jobs, 'preflight', return_value=(None, None)), \
                mock.patch.object(jobs, '_job_usage', return_value=(None, None)), \
                mock.patch.object(jobs, 'parse_codex_out', return_value=parsed), \
                mock.patch.object(jobs, '_stop_requested', side_effect=lambda job_id: requested and bool(launched)), \
                mock.patch.object(common, '_read_codex_version', return_value=None), \
                mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(jobs.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common, 'kill_tree', return_value=kill_result):
            receipt = jobs.run_job(jid)
        return jid, receipt

    def test_clean_runner_exit_paths_finalize_and_need_zero_process_probes(self):
        for engine in ('claude', 'codex'):
            outcomes = ('done', 'failed', 'limit_hit', 'stopped', 'interrupted', 'timeout')
            if engine == 'codex':
                outcomes += ('refused',)
            for outcome in outcomes:
                for kill_result in ((True, False) if outcome == 'timeout' else (True,)):
                    with self.subTest(engine=engine, outcome=outcome, kill=kill_result):
                        self.fresh_jobs()
                        states = []
                        write, lock = common.write_json, common.file_lock
                        held = []
                        @contextmanager
                        def locked(path, *args, **kw):
                            with lock(path, *args, **kw):
                                held.append(os.path.basename(path))
                                try:
                                    yield
                                finally:
                                    held.pop()
                        def record(path, value, *args, **kw):
                            if os.path.basename(path) == 'status.json':
                                states.append(dict(value))
                                if value.get('runner_finalized'):
                                    self.assertIn('status.lock', held)
                                    result = self.read_json_file(os.path.join(os.path.dirname(path), 'result.json'))
                                    self.assertEqual(result['status'], outcome)
                            return write(path, value, *args, **kw)
                        with mock.patch.object(common, 'write_json', side_effect=record), \
                                mock.patch.object(common, 'file_lock', side_effect=locked):
                            jid, receipt = self.run_fixture(engine, outcome, kill_result)
                        self.assertEqual(receipt['status'], outcome)
                        st = common.read_json(jobs._p(jid, 'status.json'))
                        if common.IS_WIN and outcome == 'timeout' and kill_result is False:
                            # Reaping the synthetic root cannot resolve an unconfirmed tree.
                            self.assertEqual(st['status'], 'running')
                            self.assertNotIn('runner_finalized', st)
                            self.assertTrue(jobs._tree_unresolved(jid))
                            self.assertTrue(jobs._child_pending(jid))
                            self.assertEqual(jobs.job_status(jid), 'unknown')
                            self.assertEqual(jobs.running_count(engine), 1)
                            continue
                        self.assertEqual(st['status'], outcome)
                        self.assertTrue(st['runner_finalized'])
                        self.assertFalse(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))
                        for key in ('pid', 'pid_created', 'child_pid', 'child_created', 'attempt', 'engine'):
                            self.assertEqual(st[key], states[-2][key], key)
                        self.assertIsNotNone(st['child_pid'])
                        with mock.patch.object(common, 'pid_state', side_effect=AssertionError('process probe')) as probe, \
                                mock.patch.object(common, 'kill_tree') as kill:
                            self.assertEqual(jobs.job_status(jid), outcome)
                            self.assertEqual([j['status'] for j in jobs.list_jobs()], [outcome])
                            self.assertEqual(jobs.running_count(engine), 0)
                            self.assertEqual(jobs.running_count('codex' if engine == 'claude' else 'claude'), 0)
                            self.assertIn('already finished', self.cli('stop', jid)[1])
                        probe.assert_not_called()
                        kill.assert_not_called()

    def test_stop_requested_after_the_work_finished_keeps_the_done_receipt(self):
        jid, receipt = self.run_fixture('claude', 'done', stop_requested=True)
        self.assertEqual((receipt['status'], self.receipt(jid)['status']), ('done', 'done'))
        self.assertIn('stop requested after the work finished; the receipt stays done', receipt['notes'])

    def test_stop_requested_while_the_engine_runs_is_recorded_by_the_runner(self):
        jid, receipt = self.run_fixture('codex', 'failed', stop_requested=True)
        self.assertEqual((receipt['status'], self.receipt(jid)['status']), ('stopped', 'stopped'))

    def test_receipt_with_surviving_child_never_finalizes(self):
        for engine in ('claude', 'codex'):
            for outcome in ('timeout', 'interrupted'):
                with self.subTest(engine=engine, outcome=outcome):
                    self.fresh_jobs()
                    jid, receipt = self.run_fixture(engine, outcome, kill_result=False, survivor=True)
                    self.assertEqual(receipt['status'], outcome)
                    self.assertIn('survivor', receipt)
                    self.assertTrue(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))
                    st = common.read_json(jobs._p(jid, 'status.json'))
                    self.assertEqual(st['status'], 'running')
                    self.assertNotIn('runner_finalized', st)

    def test_failed_survivor_write_still_cannot_finalize_a_live_child(self):
        write = common.write_json
        def write_survivor(path, value, *args, **kw):
            if os.path.basename(path) == jobs.SURVIVOR:
                raise OSError('fixture survivor write failure')
            return write(path, value, *args, **kw)
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                self.fresh_jobs()
                with mock.patch.object(common, 'write_json', side_effect=write_survivor):
                    jid, receipt = self.run_fixture(engine, 'timeout', survivor=True)
                self.assertNotIn('survivor', receipt)
                self.assertTrue(os.path.exists(jobs._p(jid, jobs.CHILD_STARTING)))
                self.assertEqual(common.read_json(jobs._p(jid, 'status.json'))['status'], 'running')

    def test_failed_finalization_keeps_receipt_and_legacy_probed_path(self):
        for failure in ('lock', 'write'):
            with self.subTest(failure=failure):
                self.fresh_jobs()
                write, lock = common.write_json, common.file_lock
                def write_status(path, value, *args, **kw):
                    if failure == 'write' and os.path.basename(path) == 'status.json' and value.get('runner_finalized'):
                        raise OSError('fixture finalize failure')
                    return write(path, value, *args, **kw)
                def lock_status(path, *args, **kw):
                    # Canonical lifecycle locks stay in the job folder for old-runner compatibility.
                    if failure == 'lock' and os.path.basename(path) == 'status.lock' \
                            and os.path.exists(jobs._p(os.path.basename(os.path.dirname(path)), 'result.json')):
                        raise TimeoutError('fixture finalize lock failure')
                    return lock(path, *args, **kw)
                with mock.patch.object(common, 'write_json', side_effect=write_status), \
                        mock.patch.object(common, 'file_lock', side_effect=lock_status):
                    jid, receipt = self.run_fixture('claude')
                self.assertEqual(receipt['status'], 'done')
                self.assertEqual(self.receipt(jid)['status'], 'done')
                st = common.read_json(jobs._p(jid, 'status.json'))
                self.assertEqual(st['status'], 'running')
                self.assertNotIn('runner_finalized', st)
                with mock.patch.object(common, 'pid_state', return_value=common.UNKNOWN) as probe:
                    self.assertEqual(jobs.job_status(jid), 'unknown')
                probe.assert_called()


class InlineRuns(JobsBase):
    @unittest.skipIf(common.IS_WIN, 'POSIX sessions only')
    def test_engine_child_starts_in_own_session(self):
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude')
        self.assertEqual(rc, 0, out + err)
        child = self.read_json_file(self.argv_out)
        self.assertEqual(child['pgid'], child['pid'])
        self.assertNotEqual(child['pgid'], os.getpgrp())

    def test_inline_intel_run_receipt(self):
        os.environ['FAKE_SERVED_MODEL'] = 'claude-opus-5-5-served'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--baton', 'my-task')
        self.assertEqual(rc, 0, out)
        jid = self.only_job()
        self.assertIn('-claude-', jid)
        r = self.receipt(jid)
        self.assertEqual(r['status'], 'done')
        self.assertEqual(r['served_models'], ['claude-opus-5-5-served'])
        self.assertIsNone(r['served_models_note'])
        self.assertEqual(r['requested']['model'], 'claude-opus-5-5')
        self.assertEqual(r['requested']['profile'], 'intel')
        self.assertEqual(r['num_turns'], 3)
        self.assertIsInstance(r['model_calls'], int)
        self.assertEqual(r['subagent_model_calls'], 0)
        self.assertIn('error_max_turns', r['turns_note'])
        self.assertEqual(r['permission_denials'], 1)
        self.assertEqual(r['meter_before'], self.meter)
        self.assertEqual(len(r['attempts']), 1)
        self.assertIsNone(r['touched_files'])  # not a repo: tripwire off, and the receipt says so
        with open(r['final_path'], encoding='utf-8') as h:
            self.assertIn(ARABIC, h.read())
        seen = self.read_json_file(self.argv_out)
        self.assertEqual(seen['anthropic_env'], [])
        self.assertEqual(seen['job'], jid)
        self.assertTrue(seen['brief_head'].startswith('You are a delegated worker run by twinlane (job ' + jid))
        self.assertIn(ARABIC, seen['brief_head'])
        self.assertEqual(os.path.normcase(seen['cwd']), os.path.normcase(self.work))
        self.assertEqual(self.baton_calls, [('my-task', 'running'), ('my-task', 'done')])
        self.assertIn(ARABIC, out)

    def test_timeout_kills_and_never_reruns(self):
        os.environ['FAKE_SLEEP'] = '60'
        t = time.monotonic()
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--timeout', '2')
        self.assertLess(time.monotonic() - t, 40)
        self.assertEqual(rc, common.EXIT_FAILED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'timeout')
        self.assertEqual(len(r['attempts']), 1)
        self.assertIsNone(r['failover'])

    def test_failover_claude_limit_to_codex(self):
        os.environ['FAKE_LIMIT'] = '1'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'intel')
        self.assertEqual(rc, 0, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'done')
        self.assertEqual(r['engine'], 'codex')
        self.assertEqual(r['failover'], {'from': 'claude', 'to': 'codex', 'reason': 'limit_hit'})
        a1, a2 = r['attempts']
        self.assertEqual((a1['n'], a1['engine'], a1['status'], a1['limit_hit']), (1, 'claude', 'limit_hit', True))
        self.assertEqual((a2['n'], a2['engine'], a2['status']), (2, 'codex', 'done'))
        self.assertEqual(r['served_models'], ['claude-opus-5-5', 'gpt-6.1-sol'])
        self.assertIn('Codex CLI may not report', r['served_models_note'])
        text = self.cli('result', self.only_job())[1]
        self.assertRegex(text, r'models: a1 claude requested=\S+ served=claude-opus-5-5 \| '
                               r'a2 codex requested=\S+ served=gpt-6\.1-sol \(as requested; no served evidence\)')
        seen = self.read_json_file(self.codex_argv_out)
        self.assertNotIn('--allow-credits', seen['argv'])
        self.assertNotIn('--write', seen['argv'])
        self.assertIn('The previous engine (claude) stopped at its usage limit after 2 turns', seen['brief_head'])
        self.assertIn('engine codex, profile intel', seen['brief_head'])
        self.assertIn("hit your limit", seen['brief_head'])
        self.assertIn(ARABIC, seen['brief_head'])

    def test_explicit_engine_does_not_fail_over(self):
        os.environ['FAKE_LIMIT'] = '1'
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude')
        self.assertEqual(rc, common.EXIT_LIMIT)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'limit_hit')
        self.assertTrue(r['limit_hit'])
        self.assertEqual(len(r['attempts']), 1)
        self.assertFalse(os.path.exists(self.codex_argv_out))

    def test_failover_refused_when_other_engine_is_out(self):
        os.environ['FAKE_LIMIT'] = '1'
        self.meter = meter_with(codex='out')
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, common.EXIT_LIMIT)
        r = self.receipt(self.only_job())
        self.assertEqual(len(r['attempts']), 1)
        self.assertIn('route refused codex', r['attempts'][0]['failover_refused'])

    def test_codex_limit_and_refusal(self):
        os.environ['FAKE_CODEX_LIMIT'] = '1'
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'sweep', '--engine', 'codex')
        self.assertEqual(rc, common.EXIT_LIMIT)
        self.assertEqual(self.receipt(jobs._job_ids()[0])['status'], 'limit_hit')
        os.environ.pop('FAKE_CODEX_LIMIT')
        os.environ['FAKE_CODEX_EXIT'] = '3'
        time.sleep(1.1)  # distinct job-id timestamp keeps _job_ids()[0] the newest
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'sweep', '--engine', 'codex')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(self.receipt(jobs._job_ids()[0])['status'], 'refused')

    def test_codex_code_profile_in_linked_worktree(self):
        wt = add_worktree(make_repo(self.tmp), self.tmp)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', wt, '--kind', 'integrate', '--write')
        self.assertEqual(rc, 0, out)
        seen = self.read_json_file(self.codex_argv_out)
        self.assertIn('--write', seen['argv'])
        self.assertEqual(seen['argv'][seen['argv'].index('--tier') + 1], 'sol')
        r = self.receipt(self.only_job())
        self.assertEqual(r['touched_files'], [])
        self.assertIsNone(r['read_only_violation'])  # only the intel profile can violate read-only

    def test_codex_success_that_mentions_limit_strings_is_done(self):
        # tool output and stderr may quote limit strings (grepping the codebase for them);
        # only a structured error event is a limit hit.
        os.environ['FAKE_CODEX_NOISY'] = '1'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'parallel', '--engine', 'codex')
        self.assertEqual(rc, 0, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'done')
        self.assertFalse(r['limit_hit'])
        self.assertEqual(self.read_json_file(self.codex_argv_out)['strict_credits'], '1')

    def test_codex_adapter_needs_no_private_binary_copy(self):
        with mock.patch.object(jobs.shutil, 'which', return_value=None):
            rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work,
                                  '--kind', 'parallel', '--engine', 'codex')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.receipt(self.only_job())['status'], 'done')
        self.assertEqual(os.listdir(common.BIN), [])

    def test_codex_lookup_is_isolated_from_the_real_app(self):
        # a codex job test must never copy or prune the real bin/, ~/.codex or %LOCALAPPDATA%.
        for path in (common.BIN, common.CODEX_DIR, os.environ['LOCALAPPDATA']):
            self.assertTrue(os.path.normcase(path).startswith(os.path.normcase(self.tmp)), path)
        self.assertEqual(os.environ['TWINLANE_TEST_ISOLATION'], '1')

    def test_codex_lookup_failure_fails_the_job_closed(self):
        # The built-in runner needs an installed CLI; an unknown executable fails closed.
        common.CODEX_RUN = None
        os.environ.pop('TWINLANE_CODEX_BIN', None)
        with mock.patch.object(jobs.shutil, 'which', return_value=None):
            rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work,
                                  '--kind', 'parallel', '--engine', 'codex')
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(jobs._job_ids(), [])
        self.assertIn('no usable codex CLI', out + err)
        self.assertFalse(os.path.exists(self.codex_argv_out))

    # ---- which Codex binary a job runs (codex.binary_source), and the checks on it

    def _fake_app_build(self):
        os.environ.pop('TWINLANE_CODEX_BIN', None)
        bundle = os.path.join(os.environ['LOCALAPPDATA'], 'OpenAI', 'Codex', 'bin', 'abc')
        os.makedirs(bundle)
        for name in common.CODEX_FILES:
            with open(os.path.join(bundle, name), 'wb') as h:
                h.write(b'codex')
        return bundle

    def run_codex(self, *extra):
        return self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'parallel', '--engine', 'codex',
                        *extra)

    @unittest.skipUnless(common.IS_WIN, 'the desktop-app copy is Windows-only')
    def test_codex_job_gets_the_private_cli_copy(self):
        # The desktop app empties its CLI bundle when it exits, and the PATH codex can be an old release the
        # account refuses for new models, so a job runs a leased copy of the app's own build.
        self._fake_app_build()
        with mock.patch.object(common, '_read_codex_version', return_value=None):  # a fake exe cannot run
            rc, out, _ = self.run_codex()
        self.assertEqual(rc, 0, out)
        seen = self.read_json_file(self.codex_argv_out)
        got = seen['codex_bin']
        self.assertTrue(got and got.startswith(common.BIN), got)
        # the copy is leased while the child runs, and released when it exits
        self.assertEqual(len(seen['leases']), 1, seen['leases'])
        self.assertEqual(os.listdir(os.path.join(os.path.dirname(got), 'leases')), [])

    def test_no_codex_anywhere_fails_the_job_instead_of_using_path(self):
        self.write_config({'codex': {'binary_source': 'app-copy'}})
        os.environ.pop('TWINLANE_CODEX_BIN')  # no app build, no private copy: a codex on PATH must not be used
        on_path = os.path.join(self.tmp, 'path-codex')
        with mock.patch.object(jobs.shutil, 'which', return_value=on_path):
            rc, out, err = self.run_codex()
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(jobs._job_ids(), [])
        self.assertIn('no usable codex CLI', out + err)
        self.assertNotIn('path-codex', out)
        self.assertFalse(os.path.exists(self.codex_argv_out))  # the transport never started

    def test_auto_falls_back_to_the_path_and_the_receipt_says_so(self):
        os.environ.pop('TWINLANE_CODEX_BIN')
        with mock.patch.object(jobs.shutil, 'which', return_value=self.fake_codex_bin):
            rc, out, _ = self.run_codex()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_json_file(self.codex_argv_out)['codex_bin'], self.fake_codex_bin)
        self.assertIn('using the codex on PATH', ' '.join(self.receipt(self.only_job())['notes']))

    def test_binary_source_path_ignores_the_app_copy(self):
        self.write_config({'codex': {'binary_source': 'path'}})
        self._fake_app_build()
        with mock.patch.object(jobs.shutil, 'which', return_value=self.fake_codex_bin):
            rc, out, _ = self.run_codex()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_json_file(self.codex_argv_out)['codex_bin'], self.fake_codex_bin)
        self.assertEqual(os.listdir(common.BIN), [])  # no private copy was made

    def test_an_unknown_binary_source_fails_the_job(self):
        self.write_config({'codex': {'binary_source': 'somewhere'}})
        rc, out, err = self.run_codex()
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(jobs._job_ids(), [])
        self.assertIn('binary_source must be one of', out + err)
        self.assertFalse(os.path.exists(self.codex_argv_out))

    @unittest.skipUnless(common.IS_WIN, 'the desktop-app copy is Windows-only')
    def test_a_child_whose_kill_failed_keeps_its_lease(self):
        # a timed-out child that survived kill_tree may still run from the copy
        self._fake_app_build()

        class Unkillable:
            pid = os.getpid()

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired('runner', timeout)

        real_popen = subprocess.Popen

        def popen(argv, *a, **kw):  # git and friends run for real; only the Codex transport is faked
            if kw.get('env', {}).get('TWINLANE_CODEX_BIN'):
                return Unkillable()
            return real_popen(argv, *a, **kw)

        with mock.patch.object(common, '_read_codex_version', return_value=None), \
                mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(jobs.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common, 'kill_tree', return_value=False):
            self.run_codex()
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'timeout')
        leases = glob.glob(os.path.join(common.BIN, 'codex-*', 'leases', '*.json'))
        self.assertEqual(len(leases), 1, leases)
        self.assertIn('survived its kill', json.dumps(r))

    @unittest.skipUnless(common.IS_WIN, 'the desktop-app copy is Windows-only')
    def test_an_earlier_copy_is_named_in_the_receipt(self):
        # reusing an older copy is allowed but must be visible
        bundle = self._fake_app_build()
        with mock.patch.object(common, '_read_codex_version', return_value=None):
            first = common.codex_bin()
        shutil.rmtree(bundle)  # the app emptied its bundle on exit
        rc, out, _ = self.run_codex()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.read_json_file(self.codex_argv_out)['codex_bin'], first)
        notes = ' '.join(self.receipt(self.only_job())['notes'])
        self.assertIn('earlier private copy', notes)
        self.assertIn(first, notes)

    def test_min_cli_version_refuses_old_binary(self):
        self.write_config({'codex': {'min_cli_version': '0.162.0'}})
        with mock.patch.object(common, '_read_codex_version', return_value='0.149.1'):
            rc, out, err = self.run_codex()
        self.assertNotEqual(rc, 0, out)
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertEqual(jobs._job_ids(), [])
        self.assertIn('0.149.1', out + err)
        self.assertIn('older than codex.min_cli_version 0.162.0', out + err)
        self.assertFalse(os.path.exists(self.codex_argv_out))  # the transport never started
        self.fresh_jobs()
        with mock.patch.object(common, '_read_codex_version', return_value='0.162.1'):
            rc, out, err = self.run_codex()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.receipt(self.only_job())['status'], 'done')

    def test_the_money_guard_runs_before_every_attempt_and_before_the_binary_lookup(self):
        calls = []
        real_preflight, real_acquire = jobs.preflight, common.acquire_codex_binary

        def preflight(spec, params, *a, **kw):
            calls.append('preflight:' + params['engine'])
            return real_preflight(spec, params, *a, **kw)

        def acquire(*a, **kw):
            calls.append('binary')
            return real_acquire(*a, **kw)

        with mock.patch.object(jobs, 'preflight', preflight), mock.patch.object(common, 'acquire_codex_binary', acquire):
            self.assertEqual(self.run_codex()[0], 0)
            self.assertEqual(calls, ['preflight:codex', 'binary'])
            del calls[:]
            self.fresh_jobs()
            os.environ['FAKE_LIMIT'] = '1'  # claude hits its limit, the job fails over to codex
            rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'intel')
            self.assertEqual(rc, 0, out)
            self.assertEqual(calls, ['preflight:claude', 'preflight:codex', 'binary'])
            # a refusal from the guard means no binary is looked up at all
            del calls[:]
            self.fresh_jobs()
            self.meter['codex'].update(on_credits=True)
            self.meter['claude'].update(status='ok')
            with mock.patch.object(jobs, '_decide', lambda *a: dict(fake_decide(*a), refused=False, engine='codex')):
                self.run_codex()
            self.assertEqual(calls, ['preflight:codex'])
            r = self.receipt(self.only_job())
            self.assertEqual(r['status'], 'refused')
            self.assertIn('paid credits', r['attempts'][0]['error'])

    def _recording_codex(self):
        """A fake `codex` that records its argv, then behaves like tests/fake_codex_cli.py."""
        record = os.path.join(self.tmp, 'codex-cli-argv.json')
        recorder = os.path.join(self.tmp, 'recording-codex.py')
        with open(recorder, 'w', encoding='utf-8') as h:
            h.write('import json, os, runpy, sys\n'
                    'open(os.environ["RECORD_ARGV"], "w").write(json.dumps(sys.argv[1:]))\n'
                    'runpy.run_path(os.environ["REAL_FAKE"], run_name="__main__")\n')
        os.environ.update(TWINLANE_CODEX_BIN=recorder, RECORD_ARGV=record,
                          REAL_FAKE=os.path.join(HERE, 'fake_codex_cli.py'))
        self.addCleanup(lambda: [os.environ.pop(k, None) for k in ('RECORD_ARGV', 'REAL_FAKE')])
        common.CODEX_RUN = None  # the built-in runner, which starts `codex exec`
        return record

    def test_exec_config_reaches_codex_exec(self):
        config = ['features.apps=false', 'mcp_servers.demo.enabled=false']
        self.write_config({'codex': {'exec_config': config}})
        record = self._recording_codex()
        rc, out, _ = self.run_codex()
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.receipt(self.only_job())['status'], 'done')
        argv = self.read_json_file(record)
        self.assertEqual([argv[i + 1] for i in range(len(argv) - 1) if argv[i] == '-c'], config)
        self.assertEqual(argv[0], 'exec')
        self.assertIn('--sandbox', argv)
        # a transport adapter gets the same list in its environment
        common.CODEX_RUN = FAKE_CODEX
        seen_env = []
        real_popen = subprocess.Popen

        def popen(argv, *a, **kw):
            seen_env.append(kw.get('env') or {})
            return real_popen(argv, *a, **kw)

        os.environ['TWINLANE_CODEX_BIN'] = self.fake_codex_bin
        self.fresh_jobs()
        with mock.patch.object(jobs.subprocess, 'Popen', side_effect=popen):
            self.assertEqual(self.run_codex()[0], 0)
        adapter_env = [e for e in seen_env if e.get('TWINLANE_CODEX_EXEC_CONFIG')][0]
        self.assertEqual(json.loads(adapter_env['TWINLANE_CODEX_EXEC_CONFIG']), config)

    def test_a_bad_exec_config_fails_the_job_before_anything_starts(self):
        for bad in (['sandbox_mode="danger-full-access"'], ['no equals sign'], 'features.apps=false'):
            with self.subTest(bad=bad):
                self.fresh_jobs()
                self.write_config({'codex': {'exec_config': bad}})
                record = self._recording_codex()
                rc, out, _ = self.run_codex()
                self.assertNotEqual(rc, 0, out)
                r = self.receipt(self.only_job())
                self.assertEqual(r['status'], 'failed')
                self.assertIn('codex policy refused', r['attempts'][0]['error'])
                self.assertFalse(os.path.exists(record))

    def test_secret_env_extra_patterns_stripped_from_worker_envs(self):
        self.write_config({'secrets': {'extra_env_name_patterns': ['^ACME_']}})
        wt = add_worktree(make_repo(self.tmp), self.tmp)
        with mock.patch.dict(os.environ, {'ACME_STORE_URL': 'u', 'ACME_NOTE': 'n', 'GH_TOKEN': 't', 'KEEP_ME': 'k'}):
            rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', wt, '--kind', 'integrate', '--write')
            self.assertEqual(rc, 0, out)
            names = self.read_json_file(self.codex_argv_out)['env_names']
            for gone in ('ACME_STORE_URL', 'ACME_NOTE', 'GH_TOKEN'):
                self.assertNotIn(gone, names)
            self.assertIn('KEEP_ME', names)
            stripped = self.receipt(self.only_job())['attempts'][0]['env_stripped']
            self.assertEqual(sorted(set(stripped) & {'ACME_STORE_URL', 'ACME_NOTE', 'GH_TOKEN', 'KEEP_ME'}),
                             ['ACME_NOTE', 'ACME_STORE_URL', 'GH_TOKEN'])
            # the Claude worker env follows the same rule (a code profile only)
            gone = []
            env = jobs.claude_env('job-x', 'code', None, gone, ['^ACME_'])
            self.assertNotIn('ACME_STORE_URL', env)
            self.assertIn('ACME_STORE_URL', gone)
            self.assertIn('ACME_STORE_URL', jobs.claude_env('job-x', 'intel', None, None, ['^ACME_']))

    def test_a_sandbox_setup_failure_is_noted_in_the_receipt(self):
        # Codex can end a run that looks normal while its sandbox helper never started, so no command ran.
        files = {'codex_out': os.path.join(self.tmp, 'out'), 'codex_log': os.path.join(self.tmp, 'adapter.log')}
        os.makedirs(files['codex_out'])
        self.assertFalse(jobs._sandbox_setup_failed(files))
        for name, text in (('stderr.log', 'ERROR exec_command failed: helper_unknown_error: setup refresh had errors\n'),
                           ('events.jsonl', '{"aggregated_output": "setup refresh had errors"}\n')):
            with self.subTest(file=name):
                path = os.path.join(files['codex_out'], name)
                with open(path, 'w', encoding='utf-8') as h:
                    h.write('fine\n' + text)
                self.assertTrue(jobs._sandbox_setup_failed(files))
                os.remove(path)
        # end to end: the real CLI writes the marker to the evidence folder's stderr.log; here it is appended
        # after the adapter fixture ran
        real_parse = jobs.parse_codex_out

        def parse(out_dir):
            with open(os.path.join(out_dir, 'stderr.log'), 'a', encoding='utf-8') as h:
                h.write('Failed to create unified exec process: helper_unknown_error: setup refresh had errors\n')
            return real_parse(out_dir)

        with mock.patch.object(jobs, 'parse_codex_out', parse):
            rc, out, _ = self.run_codex()
        self.assertEqual(rc, common.EXIT_FAILED, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('unverified', r['error'])
        notes = ' '.join(r['notes'])
        self.assertIn('sandbox could not set up', notes)
        self.assertIn('unverified', notes)

    def test_explicit_codex_binary_is_recorded(self):
        os.environ['TWINLANE_CODEX_BIN'] = self.fake_codex_bin
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work,
                              '--kind', 'parallel', '--engine', 'codex')
        self.assertEqual(rc, 0, out)
        self.assertEqual(self.receipt(self.only_job())['attempts'][0]['codex_bin'], self.fake_codex_bin)

    def test_a_child_whose_kill_failed_is_reported(self):
        # A timed-out process that survives termination must not be described as complete.
        class Unkillable:
            pid = os.getpid()

            def poll(self):
                return None

            def wait(self, timeout=None):
                raise subprocess.TimeoutExpired('runner', timeout)
        real_popen = subprocess.Popen
        def popen(argv, *a, **kw):
            if isinstance(argv, list) and FAKE_CODEX in argv:
                return Unkillable()
            return real_popen(argv, *a, **kw)
        with mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(jobs.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common, 'kill_tree', return_value=False):
            self.cli('run', '--brief', self.brief, '--cd', self.work,
                     '--kind', 'parallel', '--engine', 'codex')
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'timeout')
        self.assertIn('survived its kill', json.dumps(r))

    # ---- a timeout receipt must not hide an engine whose kill failed

    class EngineProc:
        """Popen stand-in: wait() raises TimeoutExpired for the first `stuck` calls, then returns rc."""

        def __init__(self, pid, stuck, rc=-9, raises=None):
            self.pid, self.stuck, self.rc, self.raises, self.stdout = pid, stuck, rc, raises, io.BytesIO(b'')

        def wait(self, timeout=None):
            if self.raises:  # Ctrl+C while the runner waits (once; the grace wait after the kill is a fresh call)
                exc, self.raises = self.raises, None
                raise exc()
            if self.stuck > 0:
                self.stuck -= 1
                raise subprocess.TimeoutExpired('engine', timeout)
            return self.rc

        def poll(self):
            return None if self.stuck > 0 else self.rc

    @staticmethod
    def _kill_diagnosis(out, real_kill, child, stamp):
        """What the stop saw, for a failure on a platform the author cannot run."""
        pgid = None
        if os.name != 'nt':
            try:
                pgid = (os.getpgid(child.pid), os.getpgrp())
            except OSError as e:
                pgid = repr(e)
        return (f'{out}\nkill_tree calls: {real_kill.call_args_list}\nchild poll={child.poll()} stamp={stamp} '
                f'now={common.process_created(child.pid)} state={common.pid_state(child.pid, stamp)} pgid={pgid}')

    def _stuck_engine(self):
        """A real sleeping process standing in for an engine child that outlives its kill: (process, stamp)."""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child, common.process_created(child.pid)

    def _run_timing_out(self, engine, proc, kill_result):
        """One inline job whose engine process is `proc`, timed out, with kill_tree answering `kill_result`."""
        real_popen = subprocess.Popen

        def popen(argv, *a, **kw):  # git and friends run for real; only the engine is faked
            is_claude = isinstance(argv, list) and any(os.path.basename(str(x)) == 'fake_claude.py' for x in argv)
            if (engine == 'claude' and is_claude) or (engine == 'codex' and isinstance(argv, list) and any(os.path.basename(str(x)) == 'fake_codex_run.py' for x in argv)):
                return proc
            return real_popen(argv, *a, **kw)

        # Test lifecycle behavior with an inert engine, independently of lead-only dispatch.
        jid = '20261025-000001-' + engine + '-fixture'
        params = {'engine': engine, 'profile': 'intel', 'timeout_s': 2, 'max_turns': 2,
                  'model': 'fixture-model', 'effort': 'low', 'tier': 'luna'}
        spec = {'id': jid, 'engine': engine, 'primary': params, 'profile': 'intel',
                'kind': 'intel', 'cwd': self.work, 'failover': False}
        os.makedirs(jobs.job_dir(jid))
        common.write_json(jobs._p(jid, 'job.json'), spec)
        common.write_text(jobs._p(jid, 'brief.md'), 'fixture brief')
        jobs._write_status(jid, 'queued', engine=engine)
        with mock.patch.object(common, '_read_codex_version', return_value=None), \
                mock.patch.object(common, 'contain_engine'), \
                mock.patch.object(jobs.subprocess, 'Popen', side_effect=popen), \
                mock.patch.object(common, 'kill_tree', return_value=kill_result) as kill:
            jobs.run_job(jid)
        return self.only_job(), kill

    def _check_a_surviving_engine_stays_visible_and_stoppable(self, engine):
        child, stamp = self._stuck_engine()
        other = 'codex' if engine == 'claude' else 'claude'
        jid, kill = self._run_timing_out(engine, self.EngineProc(child.pid, stuck=99), False)
        kill.assert_any_call(child.pid)  # the timeout path did try to kill it
        r = self.receipt(jid)
        self.assertEqual(r['status'], 'timeout')  # the receipt keeps saying what happened ...
        self.assertEqual((r['survivor']['pid'], r['survivor']['created']), (child.pid, stamp))
        self.assertIn('survived its kill', json.dumps(r['notes']))
        # ... but the job is not finished: it stays visible, holds its engine's slot and can be stopped.
        self.assertEqual(jobs.job_status(jid), 'orphaned')
        self.assertEqual(jobs.running_count(engine), 1)
        self.assertEqual(jobs.running_count(other), 0)
        self.assertEqual([j['status'] for j in jobs.list_jobs()], ['orphaned'])
        # A stop whose kill fails again reports it and leaves the job orphaned
        with mock.patch.object(common, 'kill_tree', return_value=False):
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, common.EXIT_FAILED)
        data = json.loads(out)
        self.assertEqual((data['status'], data['alive_pids']), ('stop_failed', [child.pid]))
        self.assertEqual(jobs.job_status(jid), 'orphaned')
        # A stop whose kill works ends the engine and frees the slot; the receipt stays the history
        with mock.patch.object(common, 'kill_tree', side_effect=common.kill_tree) as real_kill:
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, 0, self._kill_diagnosis(out, real_kill, child, stamp))
        real_kill.assert_called_once_with(child.pid, stamp)
        child.wait(timeout=15)
        if common.IS_WIN:
            self.assertTrue(jobs._tree_unresolved(jid))
            self.assertEqual(jobs.job_status(jid), 'unknown')
            self.assertEqual(jobs.running_count(engine), 1)
            self.assertEqual(self.cli('stop', jid, '--tree-gone')[0], 0)
        self.assertEqual(jobs.job_status(jid), 'timeout')
        self.assertEqual(jobs.running_count(engine), 0)
        self.assertEqual(self.receipt(jid)['status'], 'timeout')
        with mock.patch.object(common, 'kill_tree') as again:  # nothing left to kill
            self.assertIn('already finished', self.cli('stop', jid)[1])
        again.assert_not_called()

    def test_a_claude_engine_that_survives_the_timeout_kill_stays_visible_and_stoppable(self):
        self._check_a_surviving_engine_stays_visible_and_stoppable('claude')

    def test_a_codex_engine_that_survives_the_timeout_kill_stays_visible_and_stoppable(self):
        self._check_a_surviving_engine_stays_visible_and_stoppable('codex')

    def test_an_engine_that_survives_the_kill_after_an_interrupt_stays_visible(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                self.fresh_jobs()
                child, stamp = self._stuck_engine()
                jid, kill = self._run_timing_out(engine, self.EngineProc(child.pid, stuck=99, raises=KeyboardInterrupt),
                                                 False)
                kill.assert_any_call(child.pid)
                r = self.receipt(jid)
                self.assertEqual(r['status'], 'interrupted')
                self.assertEqual((r['survivor']['pid'], r['survivor']['created']), (child.pid, stamp))
                self.assertEqual((jobs.job_status(jid), jobs.running_count(engine)), ('orphaned', 1))
                self.assertEqual(self.cli('stop', jid)[0], 0)
                child.wait(timeout=15)
                if common.IS_WIN:
                    self.assertTrue(jobs._tree_unresolved(jid))
                    self.assertEqual(self.cli('stop', jid, '--tree-gone')[0], 0)
                self.assertEqual((jobs.job_status(jid), jobs.running_count(engine)), ('interrupted', 0))

    def test_an_interrupt_whose_kill_worked_leaves_no_survivor(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                self.fresh_jobs()
                # The kill worked and the engine is reaped by the time the grace wait asks (stuck=0)
                jid, _ = self._run_timing_out(engine, self.EngineProc(os.getpid(), stuck=0, raises=KeyboardInterrupt),
                                              True)
                r = self.receipt(jid)
                self.assertEqual(r['status'], 'interrupted')
                self.assertNotIn('survivor', r)
                self.assertFalse(os.path.exists(os.path.join(common.JOBS, jid, jobs.SURVIVOR)))

    # ---- a kill that reports success proves nothing: the process is checked afterwards

    def test_a_kill_that_reported_success_but_left_the_engine_running_is_a_survivor(self):
        for engine in ('claude', 'codex'):
            for how, raises in (('timeout', None), ('interrupt', KeyboardInterrupt)):
                with self.subTest(engine=engine, path=how):
                    self.fresh_jobs()
                    child, stamp = self._stuck_engine()
                    jid, kill = self._run_timing_out(engine, self.EngineProc(child.pid, stuck=99, raises=raises), True)
                    kill.assert_any_call(child.pid)
                    r = self.receipt(jid)
                    self.assertEqual(r['status'], 'timeout' if how == 'timeout' else 'interrupted')
                    self.assertEqual((r['survivor']['pid'], r['survivor']['created']), (child.pid, stamp))
                    self.assertIn('survived its kill', json.dumps(r['notes']))
                    self.assertEqual((jobs.job_status(jid), jobs.running_count(engine)), ('orphaned', 1))

    def test_a_survivor_is_recorded_with_its_pid_even_when_no_creation_stamp_can_be_read(self):
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                self.fresh_jobs()
                child, _ = self._stuck_engine()
                created = common.process_created
                with mock.patch.object(common, 'process_created',
                                       side_effect=lambda pid: None if pid == child.pid else created(pid)):
                    jid, _ = self._run_timing_out(engine, self.EngineProc(child.pid, stuck=99), True)
                saved = common.read_json(os.path.join(common.JOBS, jid, jobs.SURVIVOR))
                self.assertEqual((saved['pid'], saved['created']), (child.pid, None))
                # without a stamp the identity is unknown: held and visible, never killed by twinlane stop
                self.assertEqual((jobs.job_status(jid), jobs.running_count(engine)), ('unknown', 1))
                with mock.patch.object(common, 'kill_tree') as kill:
                    self.assertEqual(self.cli('stop', jid)[0], common.EXIT_FAILED)
                kill.assert_not_called()

    def test_a_clean_timeout_is_unchanged_and_needs_no_process_probe(self):
        for engine in ('claude', 'codex'):
            for kill_result in (True, False):  # a dead root cannot resolve an unconfirmed Windows tree
                with self.subTest(engine=engine, kill=kill_result):
                    self.fresh_jobs()
                    jid, _ = self._run_timing_out(engine, self.EngineProc(os.getpid(), stuck=1), kill_result)
                    r = self.receipt(jid)
                    self.assertEqual(r['status'], 'timeout')
                    self.assertNotIn('survivor', r)
                    self.assertNotIn('survived', json.dumps(r['notes']))
                    self.assertFalse(os.path.exists(os.path.join(common.JOBS, jid, jobs.SURVIVOR)))
                    unresolved = common.IS_WIN and not kill_result
                    expected = 'unknown' if unresolved else 'timeout'
                    self.assertEqual(jobs._tree_unresolved(jid), unresolved)
                    # Both a qualified exit and an unresolved marker need no root-pid probe.
                    with mock.patch.object(common, 'pid_state', side_effect=AssertionError('process probe')), \
                            mock.patch.object(common, 'kill_tree') as kill:
                        self.assertEqual(jobs.job_status(jid), expected)
                        self.assertEqual(jobs.running_count(engine), int(unresolved))
                        self.assertEqual([j['status'] for j in jobs.list_jobs()], [expected])
                        if not unresolved:
                            self.assertIn('already finished', self.cli('stop', jid)[1])
                    kill.assert_not_called()
                    if unresolved:
                        # Stop must recheck the runner; the unresolved marker cannot authorize release.
                        self.assertEqual(self.cli('stop', jid)[0], common.EXIT_RUNNING)
                        self.assertTrue(jobs._tree_unresolved(jid))

    def test_requested_codex_model_is_not_claimed_as_verified(self):
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work,
                              '--kind', 'parallel', '--engine', 'codex')
        self.assertEqual(rc, 0, out)
        r = self.receipt(self.only_job())
        self.assertIn('requested model', r['served_models_note'])
        self.assertEqual(r['served_models'], ['gpt-6.1-sol'])

    def test_luna_refuses_on_a_stale_codex_reading(self):
        # A small model can still cross into paid credits, so a fresh quota reading is required.
        self.meter = meter_with(codex_age_s=900)
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'sweep', '--engine', 'codex')
        self.assertEqual(rc, common.EXIT_REFUSED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'refused')
        self.assertIn('luna', r['attempts'][0]['error'])
        self.assertFalse(os.path.exists(self.codex_argv_out))  # the optional adapter was never started

    def test_a_job_rechecks_the_meter_when_it_starts(self):
        # a queued job may wait while its engine crosses the stop line.
        readings = [meter_with(), meter_with(claude='stop')]
        jobs._read_meter = lambda policy: readings.pop(0) if len(readings) > 1 else readings[0]
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude')
        self.assertEqual(rc, common.EXIT_REFUSED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'refused')
        self.assertIn('not usable at start', r['attempts'][0]['error'])
        self.assertFalse(os.path.exists(self.argv_out))  # claude was never started

    def test_utf16_brief_is_decoded(self):
        # PowerShell 5.1 `>` writes UTF-16LE with a BOM.
        with open(self.brief, 'w', encoding='utf-16') as h:
            h.write(f'Investigate the thing.\nECHO: {ARABIC}\n')
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, 0, out)
        self.assertIn(ARABIC, self.read_json_file(self.argv_out)['brief_head'])

    def test_binary_brief_is_refused(self):
        with open(self.brief, 'wb') as h:
            h.write(b'abc\x00\x00def' * 50)
        rc, _, err = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertEqual(jobs._job_ids(), [])

    def test_malformed_events_do_not_stall_the_run(self):
        # a bad event must not kill the pump thread (which would block the child on a full pipe).
        os.environ['FAKE_BAD_EVENTS'] = '1'
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, 0, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'done')
        with open(r['final_path'], encoding='utf-8') as h:
            self.assertIn(ARABIC, h.read())

    def test_code_profile_edit_outside_the_worktree_is_recorded(self):
        # the code profile's confinement is checked against the worker's own tool calls.
        wt = add_worktree(make_repo(self.tmp), self.tmp)
        outside = os.path.join(self.tmp, 'elsewhere', 'x.php')
        os.environ['FAKE_TOOL_USE'] = json.dumps({'name': 'Edit', 'input': {'file_path': outside}})
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', wt, '--kind', 'code', '--write')
        self.assertEqual(rc, common.EXIT_FAILED, out)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('job broke its guard', r['error'])
        self.assertEqual(r['disallowed_tool_uses'][0]['tool'], 'Edit')
        self.assertIn('outside the job worktree', r['disallowed_tool_uses'][0]['reason'])

    def assert_secret_stripped(self, jid, seen):
        self.assertNotIn('TWINLANE_FAKE_API_TOKEN', seen['env_names'])
        self.assertIn('TWINLANE_ENGINE_JOB', seen['env_names'])
        self.assertIn('TWINLANE_FAKE_API_TOKEN', self.receipt(jid)['attempts'][0]['env_stripped'])
        for path in glob.glob(os.path.join(jobs.job_dir(jid), '**', '*'), recursive=True):
            if os.path.isfile(path):
                with open(path, 'rb') as h:
                    self.assertNotIn(b'fake-secret-value-123', h.read(), path)

    def test_code_worker_env_has_no_secret_named_variables(self):
        # A script the code worker runs (php send.php) finds no token to send with. The
        # receipt lists the stripped NAMES, never a value.
        os.environ['TWINLANE_FAKE_API_TOKEN'] = 'fake-secret-value-123'
        wt = add_worktree(make_repo(self.tmp), self.tmp)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', wt, '--kind', 'code', '--write')
        self.assertEqual(rc, 0, out)
        self.assert_secret_stripped(self.only_job(), self.read_json_file(self.argv_out))

    def test_codex_code_worker_env_has_no_secret_named_variables(self):
        os.environ['TWINLANE_FAKE_API_TOKEN'] = 'fake-secret-value-123'
        wt = add_worktree(make_repo(self.tmp), self.tmp)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', wt, '--kind', 'integrate', '--write')
        self.assertEqual(rc, 0, out)
        seen = self.read_json_file(self.codex_argv_out)
        self.assertEqual(seen['strict_credits'], '1')
        self.assertIn('TWINLANE_CODEX_BIN', seen['env_names'])
        self.assert_secret_stripped(self.only_job(), seen)

    def test_intel_receives_the_guard_hook(self):
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        self.assertEqual(rc, 0, out)
        argv = self.read_json_file(self.argv_out)['argv']
        settings = argv[argv.index('--settings') + 1]
        hooks = self.read_json_file(settings)['hooks']['PreToolUse']
        self.assertIn('guard', hooks[0]['hooks'][0]['command'])

    def test_interrupted_runner_still_writes_a_receipt(self):
        # Ctrl+C on an inline run leaves an 'interrupted' receipt, not a ghost.
        real = jobs.run_attempt

        def boom(*a, **k):
            raise KeyboardInterrupt()
        jobs.run_attempt = boom
        try:
            rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work)
        finally:
            jobs.run_attempt = real
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'interrupted')
        self.assertNotEqual(rc, 0)

    def test_intel_write_trips_the_tripwire(self):
        wt = add_worktree(make_repo(self.tmp), self.tmp)
        os.environ['FAKE_TOUCH'] = 'new-file.txt'
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', wt)
        self.assertEqual(rc, common.EXIT_FAILED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertIn('new-file.txt', r['error'])
        self.assertEqual(r['touched_files'], ['new-file.txt'])
        self.assertIsNone(r.get('external_changes'))
        self.assertTrue(r['read_only_violation'])
        self.assertFalse(r['head_moved'])

    def test_shared_checkout_changes_are_not_attributed(self):
        # In a shared checkout a peer's edit looks the same as the worker's; only the worker's own
        # tool calls (the events scan) can convict it.
        repo = make_repo(self.tmp)
        os.environ['FAKE_TOUCH'] = 'new-file.txt'
        os.environ['FAKE_TOUCH_NO_TOOL_USE'] = '1'
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', repo)
        self.assertEqual(rc, 0)
        r = self.receipt(self.only_job())
        self.assertIsNone(r['touched_files'])
        self.assertEqual(r['external_changes'], ['new-file.txt'])
        self.assertFalse(r['read_only_violation'])

    def test_intel_write_tool_use_is_a_violation_in_a_shared_checkout(self):
        repo = make_repo(self.tmp)
        os.environ['FAKE_TOUCH'] = 'new-file.txt'
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', repo)
        self.assertEqual(rc, common.EXIT_FAILED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'failed')
        self.assertTrue(r['read_only_violation'])
        self.assertEqual(r['disallowed_tool_uses'][0]['tool'], 'Write')

    def test_continue_job_resumes_the_claude_session(self):
        os.environ['FAKE_SESSION_ID'] = 'sess-continue-1'
        self.assertEqual(self.cli('run', '--brief', self.brief, '--cd', self.work)[0], 0)
        first = self.only_job()
        os.environ.pop('FAKE_SESSION_ID')
        time.sleep(1.1)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--continue-job', first)
        self.assertEqual(rc, 0, out)
        seen = self.read_json_file(self.argv_out)
        self.assertEqual(seen['argv'][seen['argv'].index('--resume') + 1], 'sess-continue-1')
        self.assertIn('--fork-session', seen['argv'])
        self.assertEqual(os.path.normcase(seen['cwd']), os.path.normcase(self.work))
        second = jobs._job_ids()[0]
        forked_id = self.receipt(second)['session_id']
        self.assertNotEqual(forked_id, 'sess-continue-1')
        rc, out, _ = self.cli('run', '--brief', self.brief, '--continue-job', second)
        self.assertEqual(rc, 0, out)
        seen = self.read_json_file(self.argv_out)
        self.assertEqual(seen['argv'][seen['argv'].index('--resume') + 1], forked_id)


class ContinueLostJob(JobsBase):
    """--continue-job on a prior that left no receipt (its runner died): recover from the job folder."""
    CODEX_SID = '00000000-0000-4000-8000-000000000023'

    def prior(self, engine, alive=False, result=None, files=None, attempt=1, attempt_engine=None,
              child_alive=False, tag='43c7'):
        jid = f'20261008-132349-{engine}-{tag}'
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'name': 'prior', 'kind': 'code', 'engine': engine,
                                                        'cwd': self.work, 'primary': {'engine': engine}})
        # A sleeping child process stands in for a live runner / engine child (this process never counts as
        # the prior's runner); a wrong creation stamp on our own pid makes a dead one.
        dead = (os.getpid(), 12345)
        runner = self.live_pid() if alive else dead
        child = self.live_pid() if child_alive else dead
        common.write_json(os.path.join(d, 'status.json'), {
            'status': 'running', 'pid': runner[0], 'pid_created': runner[1],
            'child_pid': child[0], 'child_created': child[1],
            'engine': attempt_engine or engine, 'attempt': attempt, 'started_at': common.iso(common.now())})
        if result:
            common.write_json(os.path.join(d, 'result.json'), result)
        for name, events in (files or {}).items():
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), 'w', encoding='utf-8') as h:
                h.writelines(json.dumps(e, ensure_ascii=False) + '\n' for e in events)
        return jid

    def live_pid(self):
        """(pid, creation stamp) of a sleeping process that lives until the test ends."""
        if not getattr(self, '_sleeper', None):
            self._sleeper = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])
            self.addCleanup(self._sleeper.wait)
            self.addCleanup(self._sleeper.kill)
        return self._sleeper.pid, common.process_created(self._sleeper.pid)

    def codex_events(self, *texts):
        return ([{'type': 'thread.started', 'thread_id': self.CODEX_SID}, {'type': 'turn.started'}]
                + [{'type': 'item.completed', 'item': {'id': f'item_{i}', 'type': 'agent_message', 'text': t}}
                   for i, t in enumerate(texts)]
                + [{'type': 'item.completed', 'item': {'type': 'command_execution', 'command': 'rg x',
                                                       'aggregated_output': 'not progress'}}])

    @staticmethod
    def claude_events(sid, *texts):
        return ([{'type': 'system', 'subtype': 'init', 'session_id': sid, 'model': 'claude-opus-5-5'}]
                + [{'type': 'assistant', 'session_id': sid,
                    'message': {'content': [{'type': 'text', 'text': t}, {'type': 'tool_use', 'name': 'Read'}]}}
                   for t in texts])

    def plan(self, jid, *extra):
        args = jobs._run_parser().parse_args(['--brief', self.brief, '--continue-job', jid, *extra])
        return jobs.plan_run(args, POLICY)

    # ---- --resume <session> must not bypass the liveness guard of the job that owns the session

    def plan_resume(self, session, *extra):
        args = jobs._run_parser().parse_args(['--brief', self.brief, '--cd', self.work, '--resume', session, *extra])
        return jobs.plan_run(args, POLICY)

    def receipt_with_session(self, status, sid):
        return {'id': 'x', 'status': status, 'session_id': sid, 'session_engine': 'claude', 'final_path': None,
                'cwd': self.work, 'attempts': [{'n': 1, 'engine': 'claude', 'status': status, 'session_id': sid}]}

    def test_resume_of_the_session_of_a_live_tracked_job_is_refused_like_continue_job(self):
        sources = {
            'receipt': dict(result=self.receipt_with_session('timeout', 'sess-live-r')),   # receipt, runner alive
            'raw events': dict(files={'events.jsonl': self.claude_events('sess-live-r', 'working')}),  # no receipt yet
        }
        for name, kw in sources.items():
            with self.subTest(source=name):
                self.fresh_jobs()
                jid = self.prior('claude', alive=True, **kw)
                with self.assertRaises(jobs.Refusal) as ctx:
                    self.plan_resume('sess-live-r')
                self.assertEqual(ctx.exception.code, common.EXIT_REFUSED)
                self.assertIn(jid, ctx.exception.message)
                self.assertIn('is still running', ctx.exception.message)
                self.assertIn(f'twinlane wait {jid}', ctx.exception.message)
                self.assertIn(f'twinlane stop {jid}', ctx.exception.message)
                self.assertNotIn('sess-live-r', ctx.exception.message)  # never echo a caller-supplied value

    def test_resume_of_the_session_of_an_unknown_state_job_is_refused(self):
        jid = self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('sess-unk', 'working')})
        with unreadable_identity(self.live_pid()[0]):
            with self.assertRaises(jobs.Refusal) as ctx:
                self.plan_resume('sess-unk')
        self.assertIn(jid, ctx.exception.message)
        self.assertIn(jobs.IDENTITY_UNREADABLE, ctx.exception.message)

    def test_resume_of_the_session_of_a_survivor_job_is_refused(self):
        jid = self.prior('claude', result=self.receipt_with_session('timeout', 'sess-surv'))
        common.write_json(os.path.join(common.JOBS, jid, jobs.SURVIVOR),
                          {'engine': 'claude', 'pid': self.live_pid()[0], 'created': self.live_pid()[1], 'why': 't'})
        with self.assertRaises(jobs.Refusal):
            self.plan_resume('sess-surv')

    def test_resume_of_the_session_of_a_finished_tracked_job_is_allowed(self):
        self.prior('claude', result=self.receipt_with_session('done', 'sess-done'),
                   files={'events.jsonl': self.claude_events('sess-done', 'finished')})
        plan = self.plan_resume('sess-done')
        self.assertEqual(plan['resume'], 'sess-done')

    def test_resume_of_an_untracked_session_is_allowed(self):
        self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('sess-other', 'working')})
        plan = self.plan_resume('sess-interactive-user')  # the user's own session: no tracked job owns it
        self.assertEqual(plan['resume'], 'sess-interactive-user')
        self.assertFalse([n for n in plan['notes'] if 'session lookup' in n])

    def test_resume_session_lookup_that_did_not_finish_refuses(self):
        # An owner beyond the scan budget was resumed with only a note. Fail closed.
        self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('sess-slow', 'working')})
        with mock.patch.object(jobs, 'SESSION_SCAN_S', 0):  # no time at all: nothing could be scanned
            with self.assertRaises(jobs.Refusal) as ctx:
                self.plan_resume('sess-slow')
        self.assertEqual(ctx.exception.code, common.EXIT_REFUSED)
        self.assertIn('session lookup stopped', ctx.exception.message)
        self.assertNotIn('sess-slow', ctx.exception.message)

    def test_resume_finds_a_session_that_is_not_on_the_first_event_line(self):
        # A valid first event without the session hid the owner on line two.
        events = [{'type': 'system', 'subtype': 'hook_started'}] + self.claude_events('sess-line2', 'working')
        jid = self.prior('claude', alive=True, files={'events.jsonl': events})
        with self.assertRaises(jobs.Refusal) as ctx:
            self.plan_resume('sess-line2')
        self.assertIn(jid, ctx.exception.message)

    def test_resume_owner_that_came_to_life_after_planning_is_refused_at_start(self):
        owner = self.prior('claude', result=self.receipt_with_session('done', 'sess-race'))
        plan = self.plan_resume('sess-race')
        self.assertEqual(plan['resume_jobs'], [owner])
        new = jobs.create_job(plan)
        st_path = os.path.join(common.JOBS, owner, 'status.json')
        st = common.read_json(st_path)
        st['pid'], st['pid_created'] = self.live_pid()  # its runner is alive again
        common.write_json(st_path, st)
        r = jobs.run_job(new)
        self.assertEqual(r['status'], 'refused')
        self.assertIn(f'--resume (session of job {owner}): its runner', r['attempts'][0]['error'])
        self.assertFalse(os.path.exists(self.argv_out))  # no engine started

    def test_finished_codex_prior_on_codex_embeds_its_final_message(self):
        # Codex cannot resume, so a done Codex prior continued on Codex must not lose both session and message.
        d = os.path.join(common.JOBS, '20261008-132349-codex-43c7')
        final = os.path.join(d, 'final.txt')
        jid = self.prior('codex', result={'id': '20261008-132349-codex-43c7', 'status': 'done', 'engine': 'codex',
                                          'session_id': self.CODEX_SID, 'final_path': final, 'cwd': self.work})
        with open(final, 'w', encoding='utf-8') as h:
            h.write(f'round 2 done: B1 fixed {ARABIC}')
        plan = self.plan(jid, '--engine', 'codex')
        self.assertIsNone(plan['resume'])
        self.assertIn(f'round 2 done: B1 fixed {ARABIC}', plan['brief_text'])
        self.assertTrue(any('embedded as context instead' in n for n in plan['notes']), plan['notes'])
        self.assertFalse(any('ignored' in n for n in plan['notes']), plan['notes'])

    def test_lost_codex_prior_embeds_its_progress_and_a_lost_note(self):
        jid = self.prior('codex', files={'codex/events.jsonl': self.codex_events(
            *(f'note {i}' for i in range(1, 8)), f'last: tests half written {ARABIC}')})
        plan = self.plan(jid, '--engine', 'codex')
        self.assertIsNone(plan['resume'])
        self.assertEqual(os.path.normcase(plan['cwd']), os.path.normcase(self.work))
        brief = plan['brief_text']
        self.assertIn('did not finish (status: lost)', brief)
        self.assertIn('do not discard it', brief)
        self.assertIn(f'last: tests half written {ARABIC}', brief)
        self.assertIn('note 4', brief)
        self.assertNotIn('note 3', brief)  # only the last LOST_NOTES messages
        self.assertNotIn('not progress', brief)
        self.assertNotIn('(none)', brief)
        joined = '\n'.join(plan['notes'])
        self.assertIn('did not finish (status: lost', joined)
        self.assertIn(self.CODEX_SID, joined)
        self.assertIn('context embedded: attempt 1 (codex) 5 progress note(s)', joined)
        self.assertIn('partial diff', joined)
        # Routed to Claude instead, the Codex thread cannot resume: the notes are embedded the same way.
        plan = self.plan(jid, '--engine', 'claude')
        self.assertIsNone(plan['resume'])
        self.assertIn(f'last: tests half written {ARABIC}', plan['brief_text'])

    def test_lost_recovery_reads_tails_and_bounds_the_block(self):
        filler = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'x' * 1000}}
                  for _ in range(2500)]  # ~2.6 MB: the tail drops thread.started
        events = self.codex_events()[:1] + filler + self.codex_events('y' * 9000, 'final note')[2:]
        jid = self.prior('codex', files={'codex/events.jsonl': events})
        with mock.patch.object(common, 'tail_lines', wraps=common.tail_lines) as tail:
            got = jobs._prior_job(jid)
        self.assertTrue(all(c.kwargs.get('max_bytes', c.args[1] if len(c.args) > 1 else 2_000_000) <= 2_000_000
                            for c in tail.call_args_list))
        self.assertEqual(got['session_id'], self.CODEX_SID)  # from the first line alone
        self.assertLessEqual(len(got['context'][-1]['text']), jobs.LOST_NOTES_CHARS)
        self.assertTrue(got['context'][-1]['text'].endswith('final note'))

    def test_lost_claude_prior_resumes_its_recovered_session(self):
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-old', 'attempt one'),
                                          'events-a2.jsonl': self.claude_events('sess-lost-2', 'halfway there')},
                         attempt=2)
        plan = self.plan(jid)
        self.assertEqual(plan['resume'], 'sess-lost-2')  # the last attempt's session
        self.assertIn('did not finish (status: lost)', plan['brief_text'])
        self.assertIn('do not discard it', plan['brief_text'])
        self.assertIn('did not finish (status: lost', '\n'.join(plan['notes']))
        rc, out, _ = self.cli('run', '--brief', self.brief, '--continue-job', jid)
        self.assertEqual(rc, 0, out)
        seen = self.read_json_file(self.argv_out)
        self.assertEqual(seen['argv'][seen['argv'].index('--resume') + 1], 'sess-lost-2')
        self.assertEqual(os.path.normcase(seen['cwd']), os.path.normcase(self.work))
        second = [j for j in jobs._job_ids() if j != jid][0]
        self.assertIn('did not finish (status: lost)', jobs._read_text(os.path.join(common.JOBS, second, 'brief.md')))

    def test_stopped_receipt_without_a_session_is_recovered(self):
        jid = self.prior('claude', result={'id': 'x', 'status': 'stopped', 'session_id': None, 'final_path': None,
                                           'cwd': self.work},
                         files={'events.jsonl': self.claude_events('sess-stopped', 'was editing a.py')})
        plan = self.plan(jid)
        self.assertEqual(plan['resume'], 'sess-stopped')
        self.assertIn('did not finish (status: stopped)', plan['brief_text'])

    def test_lost_prior_with_no_evidence_says_so(self):
        jid = self.prior('codex')
        plan = self.plan(jid, '--engine', 'codex')
        self.assertIsNone(plan['resume'])
        self.assertIn('did not finish (status: lost)', plan['brief_text'])
        self.assertIn('(none recovered', plan['brief_text'])
        joined = '\n'.join(plan['notes'])
        self.assertIn('recovered attempt 1 (codex): session none; context embedded: none', joined)

    def assert_refused(self, jid, what):
        with self.assertRaises(jobs.Refusal) as cm:
            self.plan(jid)
        self.assertEqual(cm.exception.code, common.EXIT_REFUSED)
        self.assertIn(f'{what} (pid {self.live_pid()[0]}) is still running', cm.exception.message)
        self.assertIn(f'twinlane stop {jid}', cm.exception.message)
        rc, _, err = self.cli('run', '--brief', self.brief, '--continue-job', jid, '--dry-run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn(f'twinlane stop {jid}', err)
        self.assertEqual(jobs._job_ids(), [jid])  # nothing created

    def test_any_receipt_with_a_live_runner_is_refused(self):
        # The re-verify probe: a timeout receipt while its runner (pid + creation time) still runs.
        for status in ('timeout', 'limit_hit', 'failed', 'interrupted', 'done', 'refused'):
            with self.subTest(status=status):
                self.fresh_jobs()  # each status starts clean, even after a failed one
                if os.path.exists(self.argv_out):
                    os.remove(self.argv_out)
                jid = self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('s-live', 'busy')},
                                 result={'id': 'x', 'status': status, 'session_id': 's-live', 'final_path': None,
                                         'cwd': self.work, 'attempts': [{'n': 1, 'engine': 'claude'}]})
                self.assert_refused(jid, 'its runner')
                rc, _, err = self.cli('run', '--brief', self.brief, '--continue-job', jid)  # not a dry run
                self.assertEqual(rc, common.EXIT_REFUSED, err)
                self.assertFalse(os.path.exists(self.argv_out))  # no engine started
                self.assertEqual(jobs._job_ids(), [jid])

    def test_receipt_prior_whose_runner_comes_back_is_refused_at_start(self):
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('s-t', 'halfway')},
                         result={'id': 'x', 'status': 'timeout', 'session_id': 's-t', 'final_path': None,
                                 'cwd': self.work, 'attempts': [{'n': 1, 'engine': 'claude'}]})
        new = jobs.create_job(self.plan(jid))
        st_path = os.path.join(common.JOBS, jid, 'status.json')
        st = common.read_json(st_path)
        st['pid'], st['pid_created'] = self.live_pid()
        common.write_json(st_path, st)
        r = jobs.run_job(new)
        self.assertEqual(r['status'], 'refused')
        self.assertIn(f'--continue-job {jid}: its runner', r['attempts'][0]['error'])
        self.assertFalse(os.path.exists(self.argv_out))

    def test_live_running_prior_is_refused(self):
        jid = self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('sess-live', 'busy')})
        self.assert_refused(jid, 'its runner')

    def test_orphaned_prior_is_refused(self):
        jid = self.prior('codex', child_alive=True, files={'codex/events.jsonl': self.codex_events('busy')})
        self.assertEqual(jobs.job_status(jid), 'orphaned')
        self.assert_refused(jid, 'its engine process')

    def assert_refused_unreadable(self, jid, what):
        with self.assertRaises(jobs.Refusal) as cm:
            self.plan(jid)
        self.assertEqual(cm.exception.code, common.EXIT_REFUSED)
        self.assertIn(f'{what} (pid {self.live_pid()[0]}) may still be running', cm.exception.message)
        self.assertIn('cannot be read', cm.exception.message)
        self.assertIn(f'twinlane wait {jid}', cm.exception.message)
        rc, _, err = self.cli('run', '--brief', self.brief, '--continue-job', jid, '--dry-run')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIn('cannot be read', err)
        self.assertEqual(jobs._job_ids(), [jid])  # nothing created

    def test_prior_whose_runner_identity_is_unreadable_is_refused_not_continued(self):
        # The sandbox sees `process_info=(True, None)`. That must block --continue-job.
        jid = self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('sess-u', 'busy')})
        with unreadable_identity(self.live_pid()[0]):
            self.assertEqual(jobs.job_status(jid), 'unknown')
            self.assert_refused_unreadable(jid, 'its runner')
            self.assertIn('cannot be read', jobs._continue_blocked(jid))

    def test_prior_whose_engine_identity_is_unreadable_is_refused_not_continued(self):
        jid = self.prior('codex', child_alive=True, files={'codex/events.jsonl': self.codex_events('busy')})
        with unreadable_identity(self.live_pid()[0]):
            self.assert_refused_unreadable(jid, 'its engine process')

    def test_prior_with_a_dead_runner_and_a_readable_mismatch_is_still_lost(self):
        # pid-reuse protection is unchanged: a readable identity that differs is another process.
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-d', 'halfway')})
        self.assertEqual(jobs.job_status(jid), 'lost')
        self.assertIsNone(jobs._continue_blocked(jid))

    def test_stopped_prior_whose_kill_failed_is_refused(self):
        jid = self.prior('claude', alive=True, files={'events.jsonl': self.claude_events('sess-stop', 'busy')},
                         result={'id': 'x', 'status': 'stopped', 'session_id': None, 'final_path': None,
                                 'cwd': self.work, 'killed_pids': [], 'kill_failed_pids': [self.live_pid()[0]]})
        self.assert_refused(jid, 'its runner')

    def test_prior_that_comes_back_to_life_is_refused_at_start(self):
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-x', 'halfway')})
        plan = self.plan(jid)
        new = jobs.create_job(plan)
        st_path = os.path.join(common.JOBS, jid, 'status.json')
        st = common.read_json(st_path)
        st['pid'], st['pid_created'] = self.live_pid()  # its runner is alive again
        common.write_json(st_path, st)
        r = jobs.run_job(new)
        self.assertEqual(r['status'], 'refused')
        self.assertIn(f'--continue-job {jid}: its runner', r['attempts'][0]['error'])
        self.assertFalse(os.path.exists(self.argv_out))  # no engine started

    def test_failover_before_attempt_2_output_resumes_nothing(self):
        # Attempt 1 (claude) hit its limit; attempt 2 (codex) took its slot and died before writing.
        jid = self.prior('claude', attempt=2, attempt_engine='codex',
                         files={'events.jsonl': self.claude_events('sess-first', 'claude progress')})
        plan = self.plan(jid, '--engine', 'claude')
        self.assertIsNone(plan['resume'])  # never attempt 1's session
        brief = plan['brief_text']
        self.assertIn('last attempt (2, codex) wrote no output', brief)
        self.assertIn('progress notes (attempt 1, claude', brief)
        self.assertIn('claude progress', brief)
        joined = '\n'.join(plan['notes'])
        self.assertIn('recovered attempt 2 (codex): session none; context embedded: attempt 1 (claude) 1 progress note(s)',
                      joined)

    def test_plan_notes_reach_the_receipt_and_the_bg_output(self):
        jid = self.prior('codex', files={'codex/events.jsonl': self.codex_events('half done')})
        rc, out, _ = self.cli('run', '--brief', self.brief, '--continue-job', jid, '--engine', 'claude')
        self.assertEqual(rc, 0, out)
        second = [j for j in jobs._job_ids() if j != jid][0]
        notes = self.receipt(second)['notes']
        self.assertTrue(any(f'prior job {jid} did not finish (status: lost' in n for n in notes), notes)
        self.assertIn(f'note: continue-job: prior job {jid} did not finish', out)
        fake = {'pid': 4242, 'created': 133000000000000000, 'mode': 'no-job', 'job': None}  # stamped: detach refuses an unstamped spawn
        with mock.patch.object(common, 'spawn_detached_ex', return_value=fake) as spawn:
            rc, out, _ = self.cli('run', '--brief', self.brief, '--continue-job', jid, '--engine', 'claude', '--bg')
        self.assertEqual(rc, 0, out)
        spawn.assert_called_once()
        self.assertIn(f'note: continue-job: prior job {jid} did not finish (status: lost', out)
        rc, out, _ = self.cli('run', '--brief', self.brief, '--continue-job', jid, '--engine', 'claude', '--dry-run')
        self.assertIn(f'note: continue-job: prior job {jid} did not finish', out)

    def test_limit_hit_receipt_recovers_a_thread_id_beyond_the_tail(self):
        filler = [{'type': 'item.completed', 'item': {'type': 'command_execution', 'aggregated_output': 'x' * 1000}}
                  for _ in range(2500)]  # ~2.6 MB: parse_codex_out's tail misses thread.started
        events = self.codex_events()[:1] + filler + self.codex_events('was fixing b.py')[2:]
        jid = self.prior('codex', files={'codex/events.jsonl': events},
                         result={'id': 'x', 'status': 'limit_hit', 'session_id': None, 'final_path': None,
                                 'cwd': self.work, 'attempts': [{'n': 1, 'engine': 'codex', 'status': 'limit_hit'}]})
        plan = self.plan(jid, '--engine', 'codex')
        self.assertIn('did not finish (status: limit_hit)', plan['brief_text'])
        self.assertIn('was fixing b.py', plan['brief_text'])
        self.assertNotIn('(none)', plan['brief_text'])
        self.assertIn(self.CODEX_SID, '\n'.join(plan['notes']))

    def test_timeout_receipt_with_a_session_but_no_message_resumes_it_with_the_notice(self):
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-raw', 'mid-way')},
                         result={'id': 'x', 'status': 'timeout', 'session_id': 'sess-receipt', 'session_engine': 'claude',
                                 'final_path': None, 'cwd': self.work,
                                 'attempts': [{'n': 1, 'engine': 'claude', 'status': 'timeout'}]})
        plan = self.plan(jid)
        self.assertEqual(plan['resume'], 'sess-receipt')  # the receipt's session wins
        self.assertIn('did not finish (status: timeout): it left no final message', plan['brief_text'])

    def test_newer_status_attempt_beats_the_receipt_attempt(self):
        # The receipt claims attempt 1 (claude, sess-a1); status.json proves attempt 2 (codex) took its slot.
        jid = self.prior('claude', attempt=2, attempt_engine='codex',
                         files={'events.jsonl': self.claude_events('sess-a1', 'a1 progress')},
                         result={'id': 'x', 'status': 'interrupted', 'session_id': 'sess-a1',
                                 'session_engine': 'claude', 'final_path': None, 'cwd': self.work,
                                 'attempts': [{'n': 1, 'engine': 'claude', 'status': 'interrupted'}]})
        plan = self.plan(jid, '--engine', 'claude')
        self.assertIsNone(plan['resume'])  # never attempt 1's session
        self.assertIn('last attempt (2, codex) wrote no output', plan['brief_text'])
        self.assertIn('progress notes (attempt 1, claude', plan['brief_text'])
        self.assertIn('recovered attempt 2 (codex): session none', '\n'.join(plan['notes']))

    def test_stray_attempt_files_do_not_create_a_failover(self):
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-a1', 'a1 progress'),
                                          'events-a9.jsonl': self.claude_events('sess-a9', 'stray'),
                                          'other-a2.log': [], 'codex-a2x/events.jsonl': self.codex_events('stray')})
        plan = self.plan(jid)
        self.assertEqual(plan['resume'], 'sess-a1')
        self.assertIn('ignored unrelated or out-of-range attempt files', '\n'.join(plan['notes']))
        self.assertIn('events-a9.jsonl', '\n'.join(plan['notes']))
        self.assertIn('other-a2.log', '\n'.join(plan['notes']))
        self.assertNotIn('failed over', plan['brief_text'])

    def test_ignored_resume_value_is_never_recorded(self):
        marker = 'SYNTHETIC_TOKEN_MARKER_7f3a'
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex',
                                '--resume', marker)
        self.assertEqual(rc, 0, out + err)
        self.assertIn('note: --resume value ignored: resume is Claude-only', out)
        fake = {'pid': 4242, 'created': 133000000000000000, 'mode': 'no-job', 'job': None}  # stamped: detach refuses an unstamped spawn
        with mock.patch.object(common, 'spawn_detached_ex', return_value=fake):
            rc, bg_out, bg_err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex',
                                          '--resume', marker, '--bg')
        self.assertEqual(rc, 0, bg_out)
        self.assertIn('note: --resume value ignored', bg_out)
        for text in (out, err, bg_out, bg_err):
            self.assertNotIn(marker, text)
        for jid in jobs._job_ids():
            for root, _, names in os.walk(os.path.join(common.JOBS, jid)):
                for name in names:
                    with open(os.path.join(root, name), 'rb') as h:
                        self.assertNotIn(marker.encode(), h.read(), os.path.join(root, name))

    def failover_crash_prior(self, a2_events=None):
        """Codex's probe: attempt 1 (claude) hit its limit, attempt 2 took the codex slot, the runner crashed
        before recording attempt 2. The receipt looks complete: attempt 1's session and final message."""
        files = {'events.jsonl': self.claude_events('sess-a1', 'a1 raw note')}
        if a2_events:
            files['codex-a2/events.jsonl'] = a2_events
        jid = self.prior('claude', attempt=2, attempt_engine='codex', files=files,
                         result={'id': 'x', 'status': 'limit_hit', 'session_id': 'sess-a1', 'session_engine': 'claude',
                                 'engine': 'claude', 'cwd': self.work, 'final_path': None,
                                 'attempts': [{'n': 1, 'engine': 'claude', 'status': 'limit_hit'}]})
        final = os.path.join(common.JOBS, jid, 'final.md')
        common.write_text(final, 'A1 FINAL REPORT')
        r = common.read_json(os.path.join(common.JOBS, jid, 'result.json'))
        r['final_path'] = final
        common.write_json(os.path.join(common.JOBS, jid, 'result.json'), r)
        common.write_text(os.path.join(common.JOBS, jid, 'brief-a2.md'), 'continuation brief')
        return jid

    def test_complete_receipt_superseded_by_a_failover_attempt_resumes_nothing(self):
        jid = self.failover_crash_prior()
        plan = self.plan(jid)  # auto routes to claude: the old claude session must not resume
        self.assertEqual(plan['engine'], 'claude')
        self.assertIsNone(plan['resume'])
        brief = plan['brief_text']
        self.assertIn('did not finish (status: limit_hit): after attempt 1 it failed over to attempt 2 (codex)', brief)
        self.assertIn('Its last message (attempt 1, claude; context only):\n\nA1 FINAL REPORT', brief)
        self.assertNotIn('a1 raw note', brief)
        joined = '\n'.join(plan['notes'])
        self.assertIn('failed over to attempt 2 (codex)', joined)
        self.assertIn('recovered attempt 2 (codex): session none', joined)
        self.assertNotIn('ignored unrelated', joined)  # final-a1.md is a runner output

    def test_complete_receipt_superseded_embeds_the_newer_attempts_progress(self):
        jid = self.failover_crash_prior(a2_events=self.codex_events('codex a2 was fixing c.py'))
        plan = self.plan(jid)
        self.assertIsNone(plan['resume'])
        brief = plan['brief_text']
        self.assertIn('failed over to attempt 2 (codex)', brief)
        # Every attempt is context: attempt 1's report, then attempt 2's own notes, each labelled.
        self.assertIn('Its last message (attempt 1, claude; context only):\n\nA1 FINAL REPORT\n\n'
                      'Its last progress notes (attempt 2, codex; newest last):\n\ncodex a2 was fixing c.py', brief)
        self.assertIn(f'recovered attempt 2 (codex): session {self.CODEX_SID}', '\n'.join(plan['notes']))

    def test_complete_receipt_superseded_with_zero_notes_keeps_older_final(self):
        jid = self.failover_crash_prior(a2_events=self.codex_events()[:1])
        plan = self.plan(jid)
        self.assertIsNone(plan['resume'])
        self.assertIn('Its last message (attempt 1, claude; context only):\n\nA1 FINAL REPORT', plan['brief_text'])
        self.assertNotIn('wrote no output', plan['brief_text'])
        self.assertIn('context embedded: attempt 1 (claude) last message', '\n'.join(plan['notes']))

    def test_zero_note_last_attempt_keeps_its_session_and_embeds_earlier_notes(self):
        # Double limit: claude hit its limit, codex took attempt 2 and hit its own right after thread.started.
        files = {'events.jsonl': self.claude_events('sess-a1', 'a1 was fixing c.py'),
                 'codex-a2/events.jsonl': self.codex_events()[:1]}
        receipt = {'id': 'x', 'status': 'limit_hit', 'engine': 'codex', 'session_engine': 'codex',
                   'session_id': self.CODEX_SID, 'cwd': self.work, 'final_path': None,
                   'attempts': [{'n': 1, 'engine': 'claude', 'status': 'limit_hit', 'session_id': 'sess-a1'},
                                {'n': 2, 'engine': 'codex', 'status': 'limit_hit', 'session_id': self.CODEX_SID}]}
        for result, status in ((receipt, 'limit_hit'), (None, 'lost')):
            with self.subTest(receipt=bool(result)):
                self.fresh_jobs()
                jid = self.prior('claude', attempt=2, attempt_engine='codex', files=files, result=result)
                plan = self.plan(jid)
                self.assertIsNone(plan['resume'])  # neither the codex thread nor attempt 1's claude session
                brief = plan['brief_text']
                self.assertIn(f'did not finish (status: {status})', brief)
                self.assertIn('Its last attempt (2, codex) left no final message or progress notes.', brief)
                self.assertIn('progress notes (attempt 1, claude', brief)
                self.assertIn('a1 was fixing c.py', brief)
                self.assertNotIn('no engine output', brief)
                self.assertNotIn('wrote no output', brief)
                self.assertIn(f'recovered attempt 2 (codex): session {self.CODEX_SID}; context embedded: '
                              'attempt 1 (claude) 1 progress note(s)', '\n'.join(plan['notes']))

    def test_zero_note_output_is_not_called_no_output(self):
        jid = self.prior('codex', files={'codex/events.jsonl': self.codex_events()[:1]})
        plan = self.plan(jid, '--engine', 'codex')
        self.assertIn('(none recovered', plan['brief_text'])
        self.assertNotIn('no engine output', plan['brief_text'])
        self.assertIn(f'recovered attempt 1 (codex): session {self.CODEX_SID}; context embedded: none',
                      '\n'.join(plan['notes']))

    def test_receipt_without_a_status_fails_closed(self):
        for status in (None, ''):
            with self.subTest(status=status):
                self.fresh_jobs()
                jid = self.failover_crash_prior()
                path = os.path.join(common.JOBS, jid, 'result.json')
                r = common.read_json(path)
                r['status'] = status
                common.write_json(path, r)
                plan = self.plan(jid)
                self.assertEqual(plan['engine'], 'claude')
                self.assertIsNone(plan['resume'])  # never the superseded receipt's sess-a1
                self.assertIn('did not finish (status: unknown)', plan['brief_text'])

    def test_finished_failover_receipt_continues_without_a_lost_notice(self):
        # Shaped like 20261008-185302-claude-d25d: claude hit its limit, codex finished attempt 2.
        jid = self.prior('claude', attempt=2, attempt_engine='codex',
                         files={'events.jsonl': self.claude_events('sess-a1', 'a1 progress'),
                                'codex-a2/events.jsonl': self.codex_events('a2 progress')})
        d = os.path.join(common.JOBS, jid)
        for name, text in (('final-a1.md', 'You have hit your limit'), ('final-a2.md', 'A2 FINAL REPORT'),
                           ('final.md', 'A2 FINAL REPORT'), ('brief-a2.md', 'continuation brief'),
                           ('codex-adapter-a2.log', 'log'), ('stderr.log', ''), ('brief.md', 'brief'),
                           ('codex-a2/last-message.md', 'A2 FINAL REPORT'), ('codex-a2/result.txt', 'ok'),
                           ('codex-a2/stderr.log', ''), ('guard-settings.json', '{}'), ('runner.log', '')):
            common.write_text(os.path.join(d, name), text)
        common.write_json(os.path.join(d, 'result.json'), {
            'id': jid, 'kind': 'code', 'cwd': self.work, 'status': 'done', 'engine': 'codex',
            'session_id': self.CODEX_SID, 'session_engine': 'codex', 'limit_hit': False,
            'failover': {'from': 'claude', 'to': 'codex', 'reason': 'limit_hit'},
            'final_path': os.path.join(d, 'final.md'), 'events_path': os.path.join(d, 'codex-a2', 'events.jsonl'),
            'attempts': [
                {'n': 1, 'engine': 'claude', 'session_id': 'sess-a1', 'subtype': 'success', 'is_error': True,
                 'limit_hit': True, 'status': 'limit_hit', 'events_path': os.path.join(d, 'events.jsonl'),
                 'final_path': os.path.join(d, 'final-a1.md')},
                {'n': 2, 'engine': 'codex', 'session_id': self.CODEX_SID, 'limit_hit': False, 'status': 'done',
                 'events_path': os.path.join(d, 'codex-a2', 'events.jsonl'),
                 'final_path': os.path.join(d, 'final-a2.md')}]})
        for engine in ('claude', 'codex'):
            with self.subTest(engine=engine):
                plan = self.plan(jid, '--engine', engine)
                self.assertIsNone(plan['resume'])  # a codex thread never resumes
                # Nothing resumes (a codex thread), so both attempts are context; final-a1.md is the
                # limit notice, so attempt 1's notes stand in for it.
                self.assertIn('Continuing twinlane job ' + jid + '.\n\nIts last progress notes (attempt 1, claude; '
                              'newest last):\n\na1 progress\n\nIts last message (attempt 2, codex; context only):\n\n'
                              'A2 FINAL REPORT', plan['brief_text'])
                self.assertNotIn('hit your', plan['brief_text'])
                self.assertNotIn('did not finish', plan['brief_text'])
                joined = '\n'.join(plan['notes'])
                self.assertIn(f'session of codex cannot resume on {engine}; its attempts are embedded as context', joined)
                self.assertNotIn('inconsistent receipt', joined + plan['brief_text'])
                self.assertNotIn('ignored', joined)
                self.assertNotIn('did not finish', joined)

    def test_attempt_3_known_only_from_status_fails_closed(self):
        files = {'events.jsonl': self.claude_events('sess-a1', 'a1 progress'),
                 'events-a2.jsonl': self.claude_events('sess-a2', 'a2 progress')}
        receipt = {'id': 'x', 'status': 'done', 'engine': 'claude', 'session_engine': 'claude',
                   'session_id': 'sess-a2', 'cwd': self.work, 'final_path': None,
                   'attempts': [{'n': 1, 'engine': 'claude', 'session_id': 'sess-a1'},
                                {'n': 2, 'engine': 'claude', 'session_id': 'sess-a2'}]}
        for result in (None, receipt):
            with self.subTest(receipt=bool(result)):
                self.fresh_jobs()
                jid = self.prior('claude', attempt=3, files=files, result=result)
                if result:
                    final = os.path.join(common.JOBS, jid, 'final.md')
                    common.write_text(final, 'A2 FINAL REPORT')
                    result = dict(result, final_path=final)
                    common.write_json(os.path.join(common.JOBS, jid, 'result.json'), result)
                plan = self.plan(jid)
                self.assertIsNone(plan['resume'])  # neither sess-a2 nor sess-a1
                self.assertIn('last attempt (3, claude) wrote no output', plan['brief_text'])
                self.assertIn('recovered attempt 3 (claude): session none', '\n'.join(plan['notes']))
                if result:
                    self.assertIn('Its last message (attempt 2, claude; context only):\n\nA2 FINAL REPORT',
                                  plan['brief_text'])
                else:
                    self.assertIn('progress notes (attempt 2, claude', plan['brief_text'])
                    self.assertIn('a2 progress', plan['brief_text'])

    CASES = ('zero_notes', 'final_only', 'notes_only', 'nothing')

    def attempt_case(self, n, engine, case):
        """(event files, text files, session id) for attempt n on engine in one of CASES."""
        sid = self.CODEX_SID if engine == 'codex' else f'sess-a{n}'
        sfx = '' if n == 1 else f'-a{n}'
        name = f'codex{sfx}/events.jsonl' if engine == 'codex' else f'events{sfx}.jsonl'
        notes = (f'A{n} NOTE',) if case == 'notes_only' else ()
        events = (self.codex_events(*notes) if notes else self.codex_events()[:1]) if engine == 'codex' \
            else self.claude_events(sid, *notes)
        files = {name: events} if case in ('zero_notes', 'notes_only') else {}
        texts = {f'final-a{n}.md': f'A{n} FINAL REPORT'} if case == 'final_only' else {}
        return files, texts, sid if files else None

    def test_every_attempt_before_the_resumed_one_is_context_in_either_engine_order(self):
        for i, ((a, b), c1, c2, with_receipt, engine) in enumerate(itertools.product(
                (('claude', 'codex'), ('codex', 'claude')), self.CASES, self.CASES, (False, True), jobs.ENGINES)):
            with self.subTest(order=f'{a}->{b}', a1=c1, a2=c2, receipt=with_receipt, engine=engine):
                # A fresh job id per case: deleting 128 folders in a row trips Windows (WinError 145).
                f1, t1, s1 = self.attempt_case(1, a, c1)
                f2, t2, s2 = self.attempt_case(2, b, c2)
                jid = self.prior(a, attempt=2, attempt_engine=b, files={**f1, **f2}, tag=f'{i:04x}')
                d = os.path.join(common.JOBS, jid)
                for name, text in {**t1, **t2}.items():
                    common.write_text(os.path.join(d, name), text)
                if with_receipt:
                    final = None
                    if t2:
                        final = os.path.join(d, 'final.md')
                        common.write_text(final, t2['final-a2.md'])
                    common.write_json(os.path.join(d, 'result.json'), {
                        'id': jid, 'status': 'limit_hit', 'engine': b, 'session_engine': b, 'session_id': s2,
                        'cwd': self.work, 'final_path': final,
                        'attempts': [{'n': 1, 'engine': a, 'status': 'limit_hit', 'session_id': s1},
                                     {'n': 2, 'engine': b, 'status': 'limit_hit', 'session_id': s2}]})
                plan = self.plan(jid, '--engine', engine)
                brief = plan['brief_text']
                resumed = engine == b == 'claude' and s2 is not None
                self.assertEqual(plan['resume'], s2 if resumed else None)
                self.assertIn(f'did not finish (status: {"limit_hit" if with_receipt else "lost"})', brief)
                for n, eng, case in ((1, a, c1), (2, b, c2)):
                    if n == 2 and resumed:
                        self.assertNotIn('A2 ', brief)  # the resumed session already holds its own work
                        continue
                    label = f'(attempt {n}, {eng}'
                    want = {'final_only': f'Its last message {label}; context only):\n\nA{n} FINAL REPORT',
                            'notes_only': f'Its last progress notes {label}; newest last):\n\nA{n} NOTE',
                            'zero_notes': (f'Attempt 1 ({eng}) left no final message or progress notes.' if n == 1
                                           else f'Its last attempt (2, {eng}) left no final message or progress notes.'),
                            'nothing': (f'Attempt 1 ({eng}) wrote no output.' if n == 1
                                        else f'Its last attempt (2, {eng}) wrote no output before it ended')}[case]
                    self.assertIn(want, brief)
                    if case != 'nothing':
                        self.assertNotIn(f'Attempt {n} ({eng}) wrote no output', brief)
                        self.assertNotIn(f'last attempt ({n}, {eng}) wrote no output', brief)
                shown = (c1,) if resumed else (c1, c2)
                if not resumed and not set(shown) & {'final_only', 'notes_only'}:
                    self.assertIn('(none recovered', brief)
                else:
                    self.assertNotIn('(none recovered', brief)
                self.assertNotIn('no engine output' if {c1, c2} != {'nothing'} else 'holds no usable', brief)

    def test_reverse_double_limit_resumes_claude_and_embeds_the_codex_attempt(self):
        # Codex attempt 1 left a note and hit its limit; Claude attempt 2 hit its own after init.
        files = {'codex/events.jsonl': self.codex_events('A1 CODEX CONTEXT'),
                 'events-a2.jsonl': self.claude_events('sess-a2')}
        for status, final in (('limit_hit', None), ('done', 'A2 CLAUDE REPORT')):
            with self.subTest(status=status):
                self.fresh_jobs()
                jid = self.prior('codex', attempt=2, attempt_engine='claude', files=files)
                path = None
                if final:
                    path = os.path.join(common.JOBS, jid, 'final.md')
                    common.write_text(path, final)
                common.write_json(os.path.join(common.JOBS, jid, 'result.json'), {
                    'id': jid, 'status': status, 'engine': 'claude', 'session_engine': 'claude',
                    'session_id': 'sess-a2', 'cwd': self.work, 'final_path': path,
                    'attempts': [{'n': 1, 'engine': 'codex', 'status': 'limit_hit', 'session_id': self.CODEX_SID},
                                 {'n': 2, 'engine': 'claude', 'status': status, 'session_id': 'sess-a2'}]})
                plan = self.plan(jid, '--engine', 'claude')
                self.assertEqual(plan['resume'], 'sess-a2')
                self.assertIn('Its last progress notes (attempt 1, codex; newest last):\n\nA1 CODEX CONTEXT',
                              plan['brief_text'])
                self.assertNotIn('A2 CLAUDE REPORT', plan['brief_text'])  # resumed, not embedded
                self.assertEqual('did not finish' in plan['brief_text'], status == 'limit_hit')
                if status == 'limit_hit':
                    self.assertIn('session sess-a2; context embedded: attempt 1 (codex) 1 progress note(s)',
                                  '\n'.join(plan['notes']))

    def test_limit_notice_is_not_a_usable_final_message(self):
        files = {'events.jsonl': self.claude_events('sess-a1', 'A1 NOTE'),
                 'codex-a2/events.jsonl': self.codex_events()[:1]}
        for text, used in (("You've hit your session limit · resets 11:40pm (America/New_York)", False),
                           ('Report: fixed the rate limit retry in b.py. ' + 'details ' * 60, True)):
            with self.subTest(used=used):
                self.fresh_jobs()
                jid = self.prior('claude', attempt=2, attempt_engine='codex', files=files)
                common.write_text(os.path.join(common.JOBS, jid, 'final-a1.md'), text)
                brief = self.plan(jid, '--engine', 'claude')['brief_text']
                self.assertEqual('Its last message (attempt 1, claude; context only):\n\nReport:' in brief, used)
                self.assertEqual('A1 NOTE' in brief, not used)
                self.assertNotIn('hit your session limit', brief)

    def test_a_finished_short_report_mentioning_a_limit_is_a_report(self):
        # A done attempt's final is its report whatever it says; only a limit_hit (or unknown) attempt's
        # short limit text is a notice.
        text = 'Fixed retry when the rate limit is exceeded; tests pass.'
        for status, used in (('done', True), ('limit_hit', False), ([], False)):
            with self.subTest(status=status):
                self.fresh_jobs()
                jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-a1', 'A1 NOTE')})
                final = os.path.join(common.JOBS, jid, 'final.md')
                common.write_text(final, text)
                common.write_json(os.path.join(common.JOBS, jid, 'result.json'), {
                    'id': jid, 'status': status, 'engine': 'claude', 'session_engine': 'claude',
                    'session_id': 'sess-a1', 'cwd': self.work, 'final_path': final,
                    'attempts': [{'n': 1, 'engine': 'claude', 'status': status, 'session_id': 'sess-a1'}]})
                brief = self.plan(jid, '--engine', 'codex')['brief_text']
                self.assertEqual(text in brief, used)
                self.assertEqual('A1 NOTE' in brief, not used)

    def test_context_over_the_limit_trims_the_oldest_first_and_says_so(self):
        files = {'events.jsonl': self.claude_events('sess-a1', 'A1 HEAD ' + 'x' * 3000 + ' A1 TAIL')}
        jid = self.prior('claude', attempt=2, attempt_engine='codex', files=files)
        common.write_text(os.path.join(common.JOBS, jid, 'final-a2.md'), 'A2 FINAL REPORT')
        for budget, cut_whole in ((1000, False), (40, True)):
            with self.subTest(budget=budget), mock.patch.object(jobs, 'CONTEXT_CHARS', budget):
                plan = self.plan(jid, '--engine', 'claude')
                brief = plan['brief_text']
                self.assertIn('Its last message (attempt 2, codex; context only):\n\nA2 FINAL REPORT', brief)  # newest kept
                self.assertNotIn('A1 HEAD', brief)
                self.assertEqual('A1 TAIL' in brief, not cut_whole)
                self.assertEqual('(trimmed away: over the size limit)' in brief, cut_whole)
                self.assertIn(f'(Context trimmed to {budget} characters: the oldest content was cut first.)', brief)
                self.assertIn('(trimmed, oldest first)', '\n'.join(plan['notes']))
                text, _, _ = jobs._context_block(jobs._prior_job(jid), resumed=False)
                if not cut_whole:
                    self.assertLessEqual(len(text.split('newest last):\n\n')[1].split('\n\nIts last message')[0])
                                         + len('A2 FINAL REPORT'), budget)

    def test_malformed_status_on_a_consistent_single_attempt_receipt_fails_closed(self):
        for status in (None, '', ' ', 'missing', 'garbage', 7, [], {}):
            with self.subTest(status=status):
                self.fresh_jobs()
                jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-a1', 'a1 note')})
                final = os.path.join(common.JOBS, jid, 'final.md')
                common.write_text(final, 'A1 FINAL REPORT')
                r = {'id': jid, 'status': status, 'engine': 'claude', 'session_engine': 'claude',
                     'session_id': 'sess-a1', 'cwd': self.work, 'final_path': final,
                     'attempts': [{'n': 1, 'engine': 'claude', 'status': 'done', 'session_id': 'sess-a1'}]}
                if status == 'missing':
                    r.pop('status')
                common.write_json(os.path.join(common.JOBS, jid, 'result.json'), r)
                plan = self.plan(jid, '--engine', 'claude')
                self.assertIsNone(plan['resume'])
                brief = plan['brief_text']
                self.assertIn('did not finish (status: unknown)', brief)
                self.assertIn('inconsistent receipt: receipt status is missing, empty or not a runner status', brief)
                self.assertIn('do not discard it', brief)
                self.assertIn('Its last message (attempt 1, claude; context only):\n\nA1 FINAL REPORT', brief)

    def test_any_runner_file_is_output(self):
        for name in ('stderr-a2.log', 'codex-adapter-a2.log', 'codex-a2/stderr.log'):
            with self.subTest(file=name):
                self.fresh_jobs()
                jid = self.prior('claude', attempt=2, attempt_engine='codex',
                                 files={'events.jsonl': self.claude_events('sess-a1', 'a1 note')})
                path = os.path.join(common.JOBS, jid, name)
                os.makedirs(os.path.dirname(path), exist_ok=True)
                common.write_text(path, 'codex: error: auth refused')
                brief = self.plan(jid, '--engine', 'claude')['brief_text']
                self.assertNotIn('wrote no output', brief)
                self.assertIn('Its last attempt (2, codex) left no final message or progress notes and no session id.',
                              brief)
                self.assertIn('a1 note', brief)
        # An empty runner file is not output.
        common.write_text(path, '')
        self.assertIn('Its last attempt (2, codex) wrote no output', self.plan(jid, '--engine', 'claude')['brief_text'])

    def test_legacy_codex_run_log_counts_as_attempt_file(self):
        # An earlier tool named the Codex transport log codex-run[-aN].log; its jobs must not read as "ignored".
        jid = self.prior('claude', attempt=2, attempt_engine='codex',
                         files={'events.jsonl': self.claude_events('sess-a1', 'a1 note')})
        common.write_text(os.path.join(common.JOBS, jid, 'codex-run-a2.log'), 'codex: error: auth refused')
        self.assertEqual(jobs._attempt_file_numbers(jid), ([2], []))
        plan = self.plan(jid, '--engine', 'claude')
        self.assertNotIn('codex-run-a2.log', '\n'.join(plan['notes']))
        self.assertNotIn('ignored unrelated or out-of-range attempt files', '\n'.join(plan['notes']))
        self.assertIn('Its last attempt (2, codex) left no final message or progress notes and no session id.',
                      plan['brief_text'])
        # an unrelated name beside it is still ignored, and so is a legacy name for an out-of-range attempt
        common.write_text(os.path.join(common.JOBS, jid, 'codex-run-a9.log'), 'x')
        common.write_text(os.path.join(common.JOBS, jid, 'codex-runner-a2.log'), 'x')
        self.assertEqual(jobs._attempt_file_numbers(jid), ([2], ['codex-run-a9.log', 'codex-runner-a2.log']))
        # attempt 1 carries no suffix: codex-run.log is its raw output, and an empty one is not output
        self.fresh_jobs()
        jid = self.prior('claude', attempt=1, attempt_engine='codex')
        path = os.path.join(common.JOBS, jid, 'codex-run.log')
        common.write_text(path, '')
        self.assertFalse(jobs._attempt_output(jid, 1))
        common.write_text(path, 'codex: error: auth refused')
        self.assertTrue(jobs._attempt_output(jid, 1))
        self.assertEqual(jobs._last_attempt(jid, None)[0], 1)

    def test_contradictory_receipt_never_resumes_older_session(self):
        for attempts, status_attempt in (
            ([{'n': 2, 'engine': 'claude'}, {'n': 1, 'engine': 'claude', 'session_id': 'sess-a1'}], 2),
            ([{'n': 1, 'engine': 'claude', 'session_id': 'sess-a1'},
              {'n': 2, 'engine': 'claude'}, {'n': 2, 'engine': 'claude'}], 2),
            ([{'n': 1, 'engine': 'claude', 'session_id': 'sess-a1'}, {'n': 2, 'engine': 'claude'}], 1),
        ):
            with self.subTest(attempts=attempts, status_attempt=status_attempt):
                self.fresh_jobs()
                jid = self.prior('claude', attempt=status_attempt,
                                 result={'id': 'x', 'status': 'done', 'engine': 'claude',
                                         'session_engine': 'claude', 'session_id': 'sess-a1',
                                         'cwd': self.work, 'attempts': attempts})
                final = os.path.join(common.JOBS, jid, 'final.md')
                common.write_text(final, 'older final report')
                receipt = common.read_json(os.path.join(common.JOBS, jid, 'result.json'))
                receipt['final_path'] = final
                common.write_json(os.path.join(common.JOBS, jid, 'result.json'), receipt)
                plan = self.plan(jid)
                self.assertIsNone(plan['resume'])
                self.assertIn('inconsistent receipt:', plan['brief_text'])
                self.assertIn('older final report', plan['brief_text'])

    def test_prepared_but_unstarted_attempt_resumes_the_last_attempt_that_ran(self):
        # brief-a2.md written, but status.json still says attempt 1 and attempt 2 has no output.
        jid = self.prior('claude', files={'events.jsonl': self.claude_events('sess-a1', 'a1 progress')})
        common.write_text(os.path.join(common.JOBS, jid, 'brief-a2.md'), 'continuation brief')
        plan = self.plan(jid)
        self.assertEqual(plan['resume'], 'sess-a1')
        self.assertIn('attempt 2 was prepared (its brief was written) but never took its slot', plan['brief_text'])
        self.assertIn('recovered attempt 1 (claude): session sess-a1', '\n'.join(plan['notes']))

    def test_prepared_attempt_that_took_its_slot_resumes_nothing(self):
        jid = self.prior('claude', attempt=2, attempt_engine='codex',
                         files={'events.jsonl': self.claude_events('sess-a1', 'a1 progress')})
        common.write_text(os.path.join(common.JOBS, jid, 'brief-a2.md'), 'continuation brief')
        plan = self.plan(jid)
        self.assertIsNone(plan['resume'])
        self.assertIn('last attempt (2, codex) wrote no output', plan['brief_text'])
        self.assertNotIn('was prepared', plan['brief_text'])


class Tripwire(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp(prefix='twinlane-trip-'))
        self.repo = make_repo(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_further_edit_of_a_dirty_file_and_arabic_names(self):
        path = os.path.join(self.repo, 'a.txt')
        with open(path, 'a', encoding='utf-8') as h:
            h.write('dirty before\n')
        before = jobs.tripwire_snapshot(self.repo)
        time.sleep(0.05)
        with open(path, 'a', encoding='utf-8') as h:
            h.write('edited during the job\n')
        with open(os.path.join(self.repo, 'ملف.txt'), 'w', encoding='utf-8') as h:
            h.write('x')
        d = jobs.tripwire_diff(before, jobs.tripwire_snapshot(self.repo), self.repo)
        self.assertEqual(d['touched_files'], sorted(['a.txt', 'ملف.txt']))
        self.assertFalse(d['head_moved'])

    def test_commit_moves_head(self):
        before = jobs.tripwire_snapshot(self.repo)
        with open(os.path.join(self.repo, 'b.txt'), 'w', encoding='utf-8') as h:
            h.write('b')
        git(self.repo, 'add', 'b.txt')
        git(self.repo, 'commit', '-q', '--no-verify', '-m', 'sneaky')
        d = jobs.tripwire_diff(before, jobs.tripwire_snapshot(self.repo), self.repo)
        self.assertTrue(d['head_moved'])
        self.assertIn('b.txt', d['touched_files'])

    def test_not_a_repo(self):
        plain = os.path.join(self.tmp, 'plain')
        os.makedirs(plain)
        d = jobs.tripwire_diff(jobs.tripwire_snapshot(plain), jobs.tripwire_snapshot(plain), plain)
        self.assertIsNone(d['touched_files'])


class SlotsAndLifecycle(JobsBase):
    def fake_job(self, jid, status='running', pid=None, engine='claude', result=None, **extra):
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d, exist_ok=True)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'name': jid, 'kind': 'intel', 'cwd': self.work,
                                                        'primary': {'engine': engine, 'model': 'm'}})
        common.write_json(os.path.join(d, 'status.json'), dict({'status': status, 'pid': pid, 'engine': engine,
                                                                 'created_at': common.iso(common.now())}, **extra))
        if result:
            common.write_json(os.path.join(d, 'result.json'), result)
        return jid

    def dead_pid(self):
        p = subprocess.Popen([sys.executable, '-c', 'pass'])
        p.wait()
        return p.pid

    def test_lock_is_exclusive_and_dies_with_its_holder(self):
        with jobs.slot_lock():
            with self.assertRaises(TimeoutError):
                with jobs.slot_lock(timeout_s=0.3):
                    pass
        lock = os.path.join(common.STATE, 'slots.lock')
        with jobs.slot_lock(timeout_s=1):  # a leftover lock FILE is not a held lock
            self.assertTrue(os.path.exists(lock))
        code = ('import sys,time; sys.path.insert(0, sys.argv[1]); from twinlane import common\n'
                'with common.file_lock(sys.argv[2]):\n    print("held", flush=True); time.sleep(60)')
        holder = subprocess.Popen([sys.executable, '-c', code, os.path.dirname(HERE), lock],
                                  stdout=subprocess.PIPE)
        try:
            self.assertEqual(holder.stdout.readline().strip(), b'held')
            with self.assertRaises(TimeoutError):
                with jobs.slot_lock(timeout_s=0.3):
                    pass
            holder.kill()  # TerminateProcess: no cleanup runs, the OS releases the lock
            holder.wait()
            with jobs.slot_lock(timeout_s=5):
                pass
        finally:
            if holder.poll() is None:
                holder.kill()
            holder.stdout.close()

    def test_job_status_ignores_a_reused_pid(self):
        # The runner pid is alive (it is us) but its creation stamp does not match: a recycled pid.
        jid = self.fake_job('20261008-000009-claude-ghst', pid=os.getpid(), pid_created=12345)
        with open(os.path.join(common.JOBS, jid, 'runner.pid'), 'w') as h:
            h.write(f'{os.getpid()} 12345')
        self.assertEqual(jobs.job_status(jid), 'lost')
        self.assertEqual(jobs.running_count('claude'), 0)

    # ---- alive / dead / unknown

    def stamped_job(self, jid, engine='claude', **extra):
        """A job whose runner is a real sleeping process, recorded with its creation stamp: (jid, process)."""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return self.fake_job(jid, pid=child.pid, pid_created=common.process_created(child.pid), engine=engine,
                             **extra), child

    def test_unreadable_runner_identity_is_unknown_not_lost_and_holds_its_slot(self):
        # The sandbox view: the runner exists, its creation time cannot be read. That used to read `lost`.
        jid, child = self.stamped_job('20261008-000001-claude-aaaa')
        with unreadable_identity(child.pid):
            self.assertEqual(jobs.job_status(jid), 'unknown')
            self.assertEqual([j['status'] for j in jobs.list_jobs()], ['unknown'])
            self.assertEqual(jobs.running_count('claude'), 1)
            self.assertEqual(jobs.running_count('codex'), 0)
            mine = self.fake_job('20261008-000002-claude-bbbb', status='queued', pid=os.getpid())
            self.assertFalse(jobs.try_acquire_slot(mine, 'claude', 1, max_parallel=1))
            self.assertTrue(jobs.try_acquire_slot(mine, 'claude', 1, max_parallel=2))
        self.assertEqual(jobs.job_status(jid), 'running')  # readable again: it was alive all along

    def test_liveness_matrix_unknown_dead_and_mismatch(self):
        jid, child = self.stamped_job('20261008-000001-claude-aaaa')
        with self.subTest('exists, identity unreadable'), unreadable_identity(child.pid):
            self.assertEqual((jobs.job_status(jid), jobs.running_count('claude')), ('unknown', 1))
        if common.IS_WIN:
            with self.subTest('does not exist'), mock.patch.object(common, '_win_process_info',
                                                                   return_value=(False, None)):
                self.assertEqual((jobs.job_status(jid), jobs.running_count('claude')), ('lost', 0))
        with self.subTest('readable identity that does not match is a reused pid'):
            wrong = self.fake_job('20261008-000002-claude-bbbb', pid=child.pid,
                                  pid_created=common.process_created(child.pid) + 1)
            self.assertEqual(jobs.job_status(wrong), 'lost')
            self.assertEqual(jobs.running_count('claude'), 1)  # only the real runner
        with self.subTest('exited'):
            gone = self.fake_job('20261008-000003-claude-cccc', pid=self.dead_pid(), pid_created=12345)
            self.assertEqual(jobs.job_status(gone), 'lost')

    def test_unreadable_engine_child_of_a_dead_runner_is_unknown_and_holds_its_slot(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid(), pid_created=12345,
                            child_pid=child.pid, child_created=common.process_created(child.pid))
        self.assertEqual(jobs.job_status(jid), 'orphaned')  # readable: certainly alive
        with unreadable_identity(child.pid):
            self.assertEqual(jobs.job_status(jid), 'unknown')
            self.assertEqual(jobs.running_count('claude'), 1)

    def test_stop_refuses_to_kill_an_unreadable_identity(self):
        jid, child = self.stamped_job('20261008-000001-claude-aaaa')
        with unreadable_identity(child.pid), mock.patch.object(common, 'kill_tree') as kill:
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, common.EXIT_FAILED)
        data = json.loads(out)
        self.assertEqual((data['status'], data['identity_unknown_pids']), ('unknown', [child.pid]))
        kill.assert_not_called()
        self.assertIsNone(child.poll())
        self.assertIsNone(common.read_json(os.path.join(common.JOBS, jid, 'result.json')), 'no false stopped receipt')

    # ---- a crash between survivor.json and the receipt

    def _crashed_runner_with_survivor(self, jid, created):
        """Survivor.json written, the runner then died before any receipt: the engine (a real sleeping
        process) is still alive. `created` None is a survivor recorded without a stamp. -> (jid, engine process)"""
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        stamp = common.process_created(child.pid) if created else None
        jid = self.fake_job(jid, pid=self.dead_pid(), pid_created=12345)  # runner gone; no child_pid in status
        common.write_json(os.path.join(common.JOBS, jid, jobs.SURVIVOR),
                          {'engine': 'claude', 'pid': child.pid, 'created': stamp, 'why': 'test'})
        return jid, child

    def test_survivor_without_a_receipt_is_not_lost_and_holds_its_slot(self):
        for created, expected in ((True, 'orphaned'), (False, 'unknown')):
            with self.subTest(stamped=created):
                self.fresh_jobs()
                jid, child = self._crashed_runner_with_survivor('20261008-000001-claude-aaaa', created)
                self.assertEqual(jobs.job_status(jid), expected)
                self.assertEqual([j['status'] for j in jobs.list_jobs()], [expected])
                self.assertEqual((jobs.running_count('claude'), jobs.running_count('codex')), (1, 0))
                mine = self.fake_job('20261008-000002-claude-bbbb', status='queued', pid=os.getpid())
                self.assertFalse(jobs.try_acquire_slot(mine, 'claude', 1, max_parallel=1))
                self.assertIsNotNone(jobs._continue_blocked(jid))
                child.kill()
                child.wait()
                self.assertEqual(jobs.job_status(jid), 'lost')  # the survivor is gone: nothing left to protect
                self.assertEqual(jobs.running_count('claude'), 0)

    def test_stop_refuses_to_write_stopped_over_a_stampless_survivor_without_a_receipt(self):
        jid, child = self._crashed_runner_with_survivor('20261008-000001-claude-aaaa', created=False)
        with mock.patch.object(common, 'kill_tree') as kill:
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, common.EXIT_FAILED)
        data = json.loads(out)
        self.assertEqual((data['status'], data['identity_unknown_pids']), ('unknown', [child.pid]))
        kill.assert_not_called()
        self.assertIsNone(child.poll())
        self.assertIsNone(common.read_json(os.path.join(common.JOBS, jid, 'result.json')), 'no false stopped receipt')

    def test_wait_does_not_return_early_on_unknown(self):
        jid, child = self.stamped_job('20261008-000001-claude-aaaa')
        other = self.fake_job('20261008-000002-claude-bbbb', pid=os.getpid())
        with unreadable_identity(child.pid):
            for extra in ((), ('--any',)):
                with self.subTest(extra=extra):
                    rc, out, _ = self.cli('wait', jid, other, *extra, '--timeout', '0.3', '--json')
                    self.assertEqual(rc, common.EXIT_RUNNING)
                    data = json.loads(out)
                    self.assertEqual((data['timed_out'], data['finished'], data['jobs'][jid]), (True, [], 'unknown'))

    def test_stop_keeps_an_existing_receipt(self):
        receipt = {'status': 'done', 'final_text': 'kept'}
        jid = self.fake_job('20261008-000010-claude-done', pid=self.dead_pid(), result=receipt)
        self.cli('stop', jid)
        self.assertEqual(self.receipt(jid)['final_text'], 'kept')

    def test_running_count_and_slot_limit(self):
        self.fake_job('20261008-000001-claude-aaaa', pid=os.getpid())
        self.fake_job('20261008-000002-claude-bbbb', pid=self.dead_pid())
        self.fake_job('20261008-000003-codex-cccc', pid=os.getpid(), engine='codex')
        self.fake_job('20261008-000004-claude-dddd', pid=os.getpid(), result={'status': 'done'})
        self.assertEqual(jobs.running_count('claude'), 1)
        mine = self.fake_job('20261008-000005-claude-eeee', status='queued', pid=os.getpid())
        self.assertFalse(jobs.try_acquire_slot(mine, 'claude', 1, max_parallel=1))
        self.assertTrue(jobs.try_acquire_slot(mine, 'claude', 1, max_parallel=2))
        st = common.read_json(os.path.join(common.JOBS, mine, 'status.json'))
        self.assertEqual((st['status'], st['pid'], st['engine']), ('running', os.getpid(), 'claude'))

    def test_wait_timeout_exits_4_and_any(self):
        running = self.fake_job('20261008-000001-claude-aaaa', pid=os.getpid())
        rc, out, _ = self.cli('wait', running, '--timeout', '0.3')
        self.assertEqual(rc, common.EXIT_RUNNING)
        self.assertIn('running', out)
        done = self.fake_job('20261008-000002-claude-bbbb', pid=os.getpid(), result={'status': 'done'})
        rc, out, _ = self.cli('wait', running, done, '--any', '--timeout', '5')
        self.assertEqual(rc, 0, out)
        self.assertIn('returned: ' + done, out)
        self.assertIn('wait on remaining: twinlane wait ' + running, out)
        self.assertEqual(self.cli('wait', done)[0], 0)

    def test_wait_any_reports_all_finished_and_worst_status_in_any_order(self):
        running = self.fake_job('20261008-000001-claude-aaaa', pid=os.getpid())
        done = self.fake_job('20261008-000002-claude-bbbb', result={'status': 'done'})
        limit = self.fake_job('20261008-000003-claude-cccc', result={'status': 'limit_hit'})
        for order in ((done, limit, running), (limit, done, running)):
            rc, out, _ = self.cli('wait', *order, '--any', '--json', '--timeout', '0.1')
            self.assertEqual(rc, common.EXIT_LIMIT)
            data = json.loads(out)
            self.assertEqual(set(data['finished']), {done, limit})
            self.assertEqual(data['remaining'], [running])
        rc, out, _ = self.cli('wait', running, '--any', '--timeout', '0.1')
        self.assertEqual(rc, common.EXIT_RUNNING)
        self.assertNotIn('returned:', out)

    def test_wait_hint_keeps_any_and_timeout_and_timeout_json_is_truthful(self):
        running = self.fake_job('20261008-000001-claude-aaaa', pid=os.getpid())
        other = self.fake_job('20261008-000004-claude-dddd', pid=os.getpid())
        done = self.fake_job('20261008-000002-claude-bbbb', result={'status': 'done'})
        rc, out, _ = self.cli('wait', running, other, done, '--any', '--timeout', '5')
        self.assertEqual(rc, 0)
        self.assertIn(f'wait on remaining: twinlane wait {running} {other} --any --timeout 5', out)
        rc, out, _ = self.cli('wait', running, done, '--json', '--timeout', '0.1')
        self.assertEqual(rc, common.EXIT_RUNNING)
        data = json.loads(out)
        self.assertEqual((data['finished'], data['remaining']), ([done], [running]))
        self.assertEqual(self.cli('wait', done, 'bbbb', '--timeout', '1')[0], 0)  # same job named twice

    def test_dead_runner_is_lost(self):
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        self.assertEqual(jobs.job_status(jid), 'lost')
        self.assertEqual(self.cli('wait', jid, '--timeout', '2')[0], common.EXIT_FAILED)

    def test_stop_kills_tree_and_records_stopped(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            # As runners record it: every pid carries its creation stamp (an unstamped one is never killed).
            jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid(), child_pid=child.pid,
                                child_created=common.process_created(child.pid))
            rc, out, _ = self.cli('stop', jid, '--json')
            self.assertEqual(rc, 0)
            self.assertIn(child.pid, json.loads(out)['kill_failed_pids' if common.IS_WIN else 'killed_pids'])
            child.wait(timeout=15)
            self.assertEqual(self.receipt(jid)['status'], 'stopped')
            self.assertIn('already finished', self.cli('stop', jid)[1])
        finally:
            if child.poll() is None:
                child.kill()

    def test_stop_with_a_running_receipt_still_kills_the_live_child(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        try:
            # A non-terminal receipt is not completion: stop must not answer "already finished".
            jid = self.fake_job('20261008-000002-claude-bbbb', pid=self.dead_pid(), child_pid=child.pid,
                                child_created=common.process_created(child.pid),
                                result={'id': '20261008-000002-claude-bbbb', 'status': 'running'})
            rc, out, _ = self.cli('stop', jid, '--json')
            self.assertEqual(rc, 0)
            payload = json.loads(out)
            self.assertIn(child.pid, payload['kill_failed_pids' if common.IS_WIN else 'killed_pids'])
            self.assertNotEqual(payload.get('already_finished'), True)
            child.wait(timeout=15)
            self.assertEqual(self.receipt(jid)['status'], 'stopped')
        finally:
            if child.poll() is None:
                child.kill()

    def test_stop_refuses_unstamped_child_with_start_time_guidance(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
        try:
            jid = self.fake_job('20261008-000002-claude-bbbb', pid=self.dead_pid(), child_pid=child.pid,
                                child_created=None)
            rc, out, _ = self.cli('stop', jid)
            self.assertEqual(rc, common.EXIT_FAILED)
            self.assertIn('parseable C-locale process start time', out)
            self.assertIsNone(child.poll())
            self.assertIsNone(self.receipt(jid))
        finally:
            if child.poll() is None:
                child.kill()
                child.wait(timeout=10)

    def test_stop_clears_the_jobs_baton_in_flight_item(self):
        from twinlane import baton
        jobs._record_baton = baton.record_job  # the real recorder, against the temp batons dir
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        spec_path = os.path.join(common.JOBS, jid, 'job.json')
        spec = common.read_json(spec_path)
        common.write_json(spec_path, dict(spec, baton='stopme'))
        baton.record_job('stopme', {'id': jid, 'engine': 'claude', 'status': 'running', 'name': 'review'})
        self.assertEqual([f['job'] for f in baton.load('stopme')['progress']['in_flight']], [jid])
        self.assertEqual(self.cli('stop', jid)[0], 0)
        b = baton.load('stopme')
        self.assertEqual(self.receipt(jid)['baton'], 'stopme')
        self.assertEqual(b['jobs'][0]['status'], 'stopped')
        self.assertEqual(b['progress']['in_flight'], [])

    def test_stop_whose_kill_fails_on_a_live_target_writes_no_receipt_and_keeps_the_baton(self):
        from twinlane import baton
        jobs._record_baton = baton.record_job
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        orig = common.kill_tree
        try:
            jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid(), child_pid=child.pid,
                                child_created=common.process_created(child.pid))
            spec_path = os.path.join(common.JOBS, jid, 'job.json')
            common.write_json(spec_path, dict(common.read_json(spec_path), baton='stuck'))
            baton.record_job('stuck', {'id': jid, 'engine': 'claude', 'status': 'running', 'name': 'r'})
            common.kill_tree = lambda pid, created=None: False  # the kill fails; the child lives on
            rc, out, _ = self.cli('stop', jid, '--json')
            self.assertEqual(rc, common.EXIT_FAILED)
            data = json.loads(out)
            self.assertEqual((data['status'], data['alive_pids']), ('stop_failed', [child.pid]))
            self.assertIsNone(common.read_json(os.path.join(common.JOBS, jid, 'result.json')))
            self.assertEqual([f['job'] for f in baton.load('stuck')['progress']['in_flight']], [jid])
        finally:
            common.kill_tree = orig
            if child.poll() is None:
                child.kill()

    def test_stop_waits_for_an_unconfirmed_kill_to_land_before_judging_the_target_alive(self):
        # On Windows a cross-process kill is never reported as confirmed, yet the process does die a moment later:
        # the liveness check must not run before the grace, or a stop that worked reads as "still alive".
        import threading
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        orig = common.kill_tree
        try:
            jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid(), child_pid=child.pid,
                                child_created=common.process_created(child.pid))

            def late_kill(pid, created=None):
                threading.Timer(0.6, child.kill).start()
                return False  # the kill was requested but is not confirmed

            common.kill_tree = late_kill
            rc, out, _ = self.cli('stop', jid, '--json')
            self.assertEqual(rc, 0, out)
            child.wait(timeout=15)
            self.assertEqual(self.receipt(jid)['status'], 'stopped')
        finally:
            common.kill_tree = orig
            if child.poll() is None:
                child.kill()

    def test_stop_of_an_already_finished_job_syncs_its_stale_baton(self):
        from twinlane import baton
        jobs._record_baton = baton.record_job
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid(),
                            result={'id': '20261008-000001-claude-aaaa', 'status': 'done', 'engine': 'claude'})
        spec_path = os.path.join(common.JOBS, jid, 'job.json')
        common.write_json(spec_path, dict(common.read_json(spec_path), baton='late'))
        baton.record_job('late', {'id': jid, 'engine': 'claude', 'status': 'running', 'name': 'r'})
        self.assertEqual(self.cli('stop', jid)[0], 0)
        b = baton.load('late')
        self.assertEqual(b['jobs'][0]['status'], 'done')
        self.assertEqual(b['progress']['in_flight'], [])

    def test_stop_records_a_baton_failure_in_the_receipt_and_still_stops(self):
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        spec_path = os.path.join(common.JOBS, jid, 'job.json')
        common.write_json(spec_path, dict(common.read_json(spec_path), baton='x'))

        def boom(slug, receipt):
            raise RuntimeError('baton locked')
        jobs._record_baton = boom
        self.assertEqual(self.cli('stop', jid)[0], 0)
        r = self.receipt(jid)
        self.assertEqual(r['status'], 'stopped')
        self.assertTrue(any('baton end: RuntimeError' in n for n in r['notes']))

    # ---- single-writer receipt: only the holder of the runner role writes result.json

    def _sleeper(self):
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child, common.process_created(child.pid)

    def test_stop_never_writes_while_a_live_runner_holds_the_job(self):
        runner, created = self._sleeper()
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        lock = jobs._p(jid, jobs.RUNNER_LOCK)
        common.write_text(lock, f'{runner.pid} {created}')
        rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, common.EXIT_RUNNING)
        data = json.loads(out)
        self.assertEqual((data['status'], data['alive_pids']), ('stop_requested', [runner.pid]))
        self.assertIsNone(self.receipt(jid))
        self.assertTrue(os.path.exists(jobs._p(jid, jobs.STOP_REQUESTED)))
        self.assertEqual(jobs._read_text(lock).split(), [str(runner.pid), str(created)])
        self.assertIsNone(runner.poll())

    def test_stop_whose_kill_leaves_the_runner_alive_only_requests(self):
        runner, created = self._sleeper()
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=runner.pid, pid_created=created)
        with mock.patch.object(common, 'kill_tree', return_value=False):
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, common.EXIT_RUNNING)
        data = json.loads(out)
        self.assertEqual((data['status'], data['kill_failed_pids'], data['alive_pids']),
                         ('stop_requested', [runner.pid], [runner.pid]))
        self.assertIsNone(self.receipt(jid))

    def test_a_receipt_the_runner_publishes_during_the_stop_is_kept(self):
        # The runner publishes after the kill, before the stop could write: the stop re-reads only once it holds
        # the runner role, so the runner's receipt is never overwritten.
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        claim = jobs._claim_runner

        def publish_then_claim(job_id):
            common.write_json(jobs._p(job_id, 'result.json'), {'id': job_id, 'status': 'done', 'engine': 'claude'})
            return claim(job_id)
        with mock.patch.object(jobs, '_claim_runner', side_effect=publish_then_claim):
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, 0)
        self.assertTrue(json.loads(out)['already_finished'])
        self.assertEqual(self.receipt(jid)['status'], 'done')
        self.assertFalse(os.path.exists(jobs._p(jid, jobs.RUNNER_LOCK)))

    def test_stopped_is_recorded_once_and_a_later_runner_writes_nothing(self):
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        writes, real = [], jobs._write_receipt

        def record(job_id, receipt):
            writes.append(receipt['status'])
            return real(job_id, receipt)
        with mock.patch.object(jobs, '_write_receipt', side_effect=record):
            self.assertEqual(self.cli('stop', jid)[0], 0)
            self.assertIn('already finished', self.cli('stop', jid)[1])
        self.assertEqual(writes, ['stopped'])
        self.assertFalse(os.path.exists(jobs._p(jid, jobs.RUNNER_LOCK)))
        with mock.patch.object(jobs, '_write_receipt', side_effect=AssertionError('a second writer')):
            late = jobs.run_job(jid)
        self.assertTrue(late['duplicate_runner'])
        self.assertEqual(self.receipt(jid)['status'], 'stopped')

    def test_background_refusal_takes_the_runner_role_and_never_overwrites(self):
        plan, err = {'engine': 'claude'}, OSError('spawn failed')
        quiet = lambda: (redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()))  # noqa: E731
        fresh = self.fake_job('20261008-000001-claude-aaaa', status='queued')
        out, errs = quiet()
        with out, errs:
            self.assertEqual(jobs._refuse_background(fresh, plan, True, err), common.EXIT_REFUSED)
        self.assertEqual(self.receipt(fresh)['status'], 'refused')
        self.assertFalse(os.path.exists(jobs._p(fresh, jobs.RUNNER_LOCK)))
        stopped = self.fake_job('20261008-000002-claude-bbbb', status='queued',
                                result={'id': '20261008-000002-claude-bbbb', 'status': 'stopped', 'engine': 'claude'})
        holder, created = self._sleeper()
        held = self.fake_job('20261008-000003-claude-cccc', status='queued')
        common.write_text(jobs._p(held, jobs.RUNNER_LOCK), f'{holder.pid} {created}')
        for jid in (stopped, held):
            out, errs = quiet()
            with out, errs:
                self.assertEqual(jobs._refuse_background(jid, plan, True, err), common.EXIT_REFUSED)
        self.assertEqual(self.receipt(stopped)['status'], 'stopped')
        self.assertIsNone(self.receipt(held))

    def test_background_refusal_that_cannot_take_the_role_writes_nothing(self):
        # Fail closed: without the role a check-then-write could overwrite a receipt the holder publishes meanwhile.
        plan = {'engine': 'claude'}
        for n, error in enumerate((OSError('no creation stamp'), TimeoutError('runner.claim busy')), 1):
            for published in (None, 'done'):
                with self.subTest(error=type(error).__name__, published=published):
                    jid = self.fake_job(f'20261008-00000{n}-claude-{"d" if published else "c"}{n}aa', status='queued')

                    def claim(job_id, error=error, published=published):
                        if published:  # the role holder publishes while the refusal is blocked
                            common.write_json(jobs._p(job_id, 'result.json'),
                                              {'id': job_id, 'status': published, 'engine': 'claude'})
                        raise error
                    out = io.StringIO()
                    with mock.patch.object(jobs, '_claim_runner', side_effect=claim), \
                            redirect_stdout(out), redirect_stderr(io.StringIO()):
                        rc = jobs._refuse_background(jid, plan, True, OSError('spawn failed'))
                    self.assertEqual(rc, common.EXIT_REFUSED)
                    self.assertEqual((self.receipt(jid) or {}).get('status'), published)
                    self.assertIn('not written: the runner role could not be taken', out.getvalue())

    def test_stop_treats_a_role_holder_of_unknown_identity_as_live(self):
        holder, created = self._sleeper()
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=self.dead_pid())
        common.write_text(jobs._p(jid, jobs.RUNNER_LOCK), f'{holder.pid} {created}')
        with unreadable_identity(holder.pid):
            rc, out, _ = self.cli('stop', jid, '--json')
        self.assertEqual(rc, common.EXIT_RUNNING)
        self.assertEqual(json.loads(out)['status'], 'stop_requested')
        self.assertIsNone(self.receipt(jid))
        self.assertIsNone(holder.poll())

    def test_list_jobs_and_bounded_result(self):
        jid = self.fake_job('20261008-000001-claude-aaaa', pid=os.getpid())
        final = os.path.join(common.JOBS, jid, 'final.md')
        common.write_text(final, '\n'.join(f'line {i} {ARABIC}' for i in range(200)))
        common.write_json(os.path.join(common.JOBS, jid, 'result.json'),
                          {'id': jid, 'status': 'done', 'engine': 'claude', 'served_models': ['m-served'],
                           'final_path': final, 'duration_s': 4.2, 'started_at': common.iso(common.now())})
        rows = jobs.list_jobs()
        self.assertEqual(len(rows), 1)
        for key in ('id', 'name', 'engine', 'status', 'cwd', 'started_at', 'age'):
            self.assertIn(key, rows[0])
        self.assertEqual((rows[0]['status'], rows[0]['model']), ('done', 'm-served'))
        rc, out, _ = self.cli('result', jid)
        self.assertEqual(rc, 0)
        self.assertIn('line 119', out)
        self.assertNotIn('line 120 ', out)
        self.assertIn('80 more lines', out)
        self.assertIn('line 199', self.cli('result', jid, '--full')[1])
        self.assertIn(jid, self.cli('jobs')[1])
        self.assertEqual(self.cli('result', 'no-such-job')[0], common.EXIT_USAGE)

    def test_codex_result_labels_requested_model_as_unverified(self):
        jid = self.fake_job('20261008-000001-codex-aaaa', pid=os.getpid())
        common.write_json(os.path.join(common.JOBS, jid, 'result.json'),
                          {'id': jid, 'status': 'done', 'engine': 'codex',
                           'requested': {'model': 'gpt-6-sol'}, 'served_models': ['gpt-6-sol'],
                           'touched_files': [], 'final_path': None})
        rc, out, _ = self.cli('result', jid)
        self.assertEqual(rc, 0)
        self.assertIn('models: a1 codex requested=gpt-6-sol served=gpt-6-sol (as requested; no served evidence)', out)

    def test_failover_result_shows_requested_and_served_per_attempt(self):
        # The shape of example job: Claude hit its limit, Codex finished.
        jid = self.fake_job('20261008-000002-claude-bbbb', pid=os.getpid())
        common.write_json(os.path.join(common.JOBS, jid, 'result.json'), {
            'id': jid, 'status': 'done', 'engine': 'codex',
            'requested': {'model': 'claude-opus-5-5', 'effort': 'high', 'tier': None, 'profile': 'code'},
            'served_models': ['claude-opus-5-5', 'gpt-6-sol'], 'touched_files': [], 'final_path': None,
            'failover': {'from': 'claude', 'to': 'codex', 'reason': 'limit_hit'},
            'attempts': [{'n': 1, 'engine': 'claude', 'model': 'claude-opus-5-5', 'tier': None,
                          'served_models': ['claude-opus-5-5'], 'status': 'limit_hit'},
                         {'n': 2, 'engine': 'codex', 'model': None, 'tier': 'sol-high',
                          'served_models': ['gpt-6-sol'], 'status': 'done',
                          'served_models_note': 'as reported by the optional adapter'}]})
        out = self.cli('result', jid)[1]
        self.assertIn('models: a1 claude requested=claude-opus-5-5 served=claude-opus-5-5 | '
                      'a2 codex requested=sol-high served=gpt-6-sol (as requested; no served evidence)', out)
        self.assertNotIn('served=claude-opus-5-5,gpt-6-sol', out)  # one list read as Codex serving Claude's model
        self.assertNotIn('Codex model=requested-as-served', out)
        data = json.loads(self.cli('result', jid, '--json')[1])
        self.assertEqual(data['served_models'], ['claude-opus-5-5', 'gpt-6-sol'])
        self.assertEqual(data['requested']['model'], 'claude-opus-5-5')


class EndToEndBackground(JobsBase):
    def test_bg_run_wait_result(self):
        os.environ['FAKE_SERVED_MODEL'] = 'claude-opus-5-5-e2e'
        os.environ['FAKE_SESSION_ID'] = 'e2e-session-0001'
        # The detached runner re-checks the meter before it starts; give it a fresh cached reading so
        # the test never reads the real quota.
        common.write_json(os.path.join(common.STATE, 'meter.json'), meter_with())
        rc, out, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--bg', '--json', '--name', 'e2e')
        self.assertEqual(rc, 0, out)
        jid = json.loads(out)['id']
        rc, out, _ = self.cli('wait', jid, '--timeout', '90')
        if rc != 0:
            self.fail(out + jobs._read_text(os.path.join(common.JOBS, jid, 'runner.log')))
        rc, out, _ = self.cli('result', jid, '--json')
        self.assertEqual(rc, 0)
        r = json.loads(out)
        self.assertEqual(r['status'], 'done')
        self.assertEqual(r['served_models'], ['claude-opus-5-5-e2e'])
        self.assertEqual(r['session_id'], 'e2e-session-0001')
        self.assertEqual(r['num_turns'], 3)
        self.assertIn(ARABIC, r['final_text'])
        st = common.read_json(os.path.join(common.JOBS, jid, 'status.json'))
        self.assertNotEqual(st['pid'], os.getpid())  # the detached runner did the work
        mode = common.read_json(os.path.join(common.JOBS, jid, 'job.json'))['detach']['mode']
        if common.IS_WIN:
            # Every Windows background launch uses WMI, including a caller outside job objects.
            self.assertEqual(mode, 'wmi')
        else:
            self.assertEqual(mode, 'posix')
        self.assertFalse(os.path.exists(os.path.join(common.JOBS, jid, 'runner.env.json')))
        self.assertEqual(self.read_json_file(self.argv_out)['anthropic_env'], [])
        rows = {j['id']: j for j in jobs.list_jobs()}
        self.assertEqual(rows[jid]['name'], 'e2e')


def usage_meter(claude=(10.0, 40.0), codex=30.0, at=0, source='oauth'):
    """A meter whose readings carry windows, so a receipt can compute a delta. `at` shifts reading_at (seconds)."""
    when = common.iso(common.now() + dt.timedelta(seconds=at))
    later = common.iso(common.now() + dt.timedelta(days=3))
    return {'read_at': when,
            'claude': {'status': 'ok', 'max_pct': max(claude), 'source': source, 'reading_at': when, 'age_s': 1,
                       'extra_usage_enabled': False, 'next_reset': later,
                       'windows': [{'name': 'five_hour', 'pct': claude[0], 'resets_at': later},
                                   {'name': 'seven_day', 'pct': claude[1], 'resets_at': later}]},
            'codex': {'status': 'ok', 'max_pct': codex, 'source': 'app-server', 'reading_at': when, 'age_s': 1,
                      'next_reset': later, 'windows': [{'name': 'weekly', 'pct': codex, 'resets_at': later}]}}


class UsageReceipts(JobsBase):
    """Each receipt records the meter before and after the job and the delta; both engines, no model call."""

    def two_phase(self, before, after):
        """_read_meter returns `before` for the plan and preflight reads, `after` for the reads after the job."""
        calls = []

        def read(policy):
            calls.append(1)
            return before() if len(calls) <= 2 else after()

        jobs._read_meter = read
        return calls

    def run_claude(self, *extra):
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude', *extra)
        self.assertEqual(rc, 0, out + err)
        return self.receipt(jobs._job_ids()[0]), out  # newest: the test may have planted older peer jobs

    def test_receipt_records_before_after_and_delta_for_both_engines(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(12.5, 41.0), codex=31.0))
        r, out = self.run_claude()
        u = r['usage']
        self.assertEqual(u['engines'], ['claude'])
        self.assertEqual(u['claude']['delta'], {'five_hour': 2.5, 'seven_day': 1.0})
        self.assertEqual(u['codex']['delta'], {'weekly': 1.0})  # recorded for the other engine too
        self.assertEqual(u['claude']['before']['windows']['five_hour']['pct'], 10.0)
        self.assertEqual(u['claude']['after']['windows']['five_hour']['pct'], 12.5)
        self.assertFalse(u['claude']['approximate'])
        self.assertEqual(u['claude']['reasons'], [])
        self.assertEqual(r['meter_after']['claude']['windows'][0]['pct'], 12.5)
        self.assertIn('usage claude: 5h +2.5pp, 7d +1pp', out)
        self.assertNotIn('APPROXIMATE', out)
        self.assertNotIn('usage codex', out)  # only engines the job ran on are printed
        rc, shown, _ = self.cli('result', r['id'])
        self.assertIn('usage claude: 5h +2.5pp, 7d +1pp', shown)

    def test_result_json_carries_the_usage_block(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))
        r, _ = self.run_claude()
        rc, shown, _ = self.cli('result', r['id'], '--json')
        self.assertEqual(json.loads(shown)['usage']['claude']['delta']['five_hour'], 1.0)

    def test_an_overlapping_running_job_on_the_same_engine_makes_it_approximate(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(13.0, 40.0), codex=30.0))
        other = os.path.join(common.JOBS, '20261009-000000-claude-peer')
        os.makedirs(other)
        common.write_json(os.path.join(other, 'job.json'), {'primary': {'engine': 'claude'},
                                                           'created_at': common.iso(common.now())})
        common.write_json(os.path.join(other, 'status.json'), {
            'status': 'running', 'pid': os.getpid(), 'pid_created': jobs._self_created(),
            'started_at': common.iso(common.now())})
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude')
        self.assertEqual(rc, 0, out + err)
        r = self.receipt(jobs._job_ids()[0])
        u = r['usage']
        self.assertTrue(u['claude']['approximate'])
        self.assertEqual(u['claude']['overlapping_jobs'], ['20261009-000000-claude-peer'])
        self.assertIn('1 other claude job(s) overlapped: 20261009-000000-claude-peer', u['claude']['reasons'][0])
        self.assertFalse(u['codex']['approximate'])  # the peer ran on claude only
        self.assertIn('APPROXIMATE: 1 other claude job(s) overlapped', out)

    def test_a_finished_job_that_overlapped_in_time_counts_and_one_that_did_not_does_not(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))
        now = common.now()

        def finished(name, start_s, end_s):
            d = os.path.join(common.JOBS, name)
            os.makedirs(d)
            common.write_json(os.path.join(d, 'result.json'), {
                'status': 'done', 'engine': 'claude', 'started_at': common.iso(now + dt.timedelta(seconds=start_s)),
                'ended_at': common.iso(now + dt.timedelta(seconds=end_s)),
                'attempts': [{'engine': 'claude', 'argv': ['x'], 'started_at': common.iso(
                    now + dt.timedelta(seconds=start_s)), 'ended_at': common.iso(
                    now + dt.timedelta(seconds=end_s))}]})

        finished('20261009-000001-claude-before', -3600, -1800)  # ended long before this job
        finished('20261009-000002-claude-during', -5, 3600)      # ends after: overlaps
        r, _ = self.run_claude()
        self.assertEqual(r['usage']['claude']['overlapping_jobs'], ['20261009-000002-claude-during'])

    def test_a_stale_before_reading_makes_the_delta_approximate(self):
        self.two_phase(lambda: usage_meter(at=-900), lambda: usage_meter(claude=(11.0, 40.0)))
        r, out = self.run_claude()
        self.assertTrue(r['usage']['claude']['approximate'])
        self.assertIn('before reading was', r['usage']['claude']['reasons'][0])
        self.assertIn('APPROXIMATE', out)

    def test_no_newer_reading_means_no_delta_and_says_why(self):
        fixed = usage_meter(at=-5)
        jobs._read_meter = lambda policy: fixed
        r, out = self.run_claude()
        self.assertIsNone(r['usage']['claude']['delta'])
        self.assertIn('no newer reading', r['usage']['claude']['why'])
        self.assertIn('usage claude: no delta (no newer reading', out)

    def test_a_window_that_reset_during_the_job_is_skipped(self):
        def after():
            m = usage_meter(claude=(2.0, 41.0))
            m['claude']['windows'][0]['resets_at'] = common.iso(common.now() + dt.timedelta(hours=5))
            return m

        self.two_phase(lambda: usage_meter(claude=(90.0, 40.0), at=-5), after)
        r, out = self.run_claude()
        self.assertEqual(r['usage']['claude']['delta'], {'seven_day': 1.0})
        self.assertIn('five_hour', r['usage']['claude']['skipped'])
        self.assertIn('skipped: five_hour reset during the job', out)

    def test_a_refused_job_has_no_usage(self):
        readings = [usage_meter(), usage_meter()]
        readings[1]['claude']['status'] = 'stop'
        jobs._read_meter = lambda policy: readings.pop(0) if len(readings) > 1 else readings[0]
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'claude')
        self.assertEqual(rc, common.EXIT_REFUSED)
        self.assertIsNone(self.receipt(self.only_job())['usage'])

    def test_a_usage_failure_never_loses_the_receipt(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))
        with mock.patch.object(jobs, '_overlapping_jobs', side_effect=RuntimeError('boom')):
            r, out = self.run_claude()
        self.assertEqual(r['status'], 'done')
        self.assertIn('RuntimeError: boom', r['usage']['error'])
        self.assertIn('usage: unavailable (RuntimeError: boom)', out)

    def test_the_after_reading_is_re_read_once_when_it_predates_the_end_and_never_by_a_model(self):
        seen = []

        def fake_meter(policy, fresh=False, auto_refresh=True):
            seen.append((fresh, auto_refresh))
            return usage_meter(at=-300) if not fresh else usage_meter(claude=(12.0, 40.0))

        spec = {'meter_before': usage_meter(at=-400), 'cwd': self.work}
        attempts = [{'engine': 'claude', 'argv': ['x'], 'started_at': common.iso(common.now())}]
        with mock.patch.object(jobs, '_meter', fake_meter):
            usage, after = jobs._job_usage('x', spec, attempts, {}, common.now(), None, quick=False)
        self.assertEqual(seen, [(False, False), (True, False)])  # never an auto (model) refresh
        self.assertEqual(after['claude']['windows'][0]['pct'], 12.0)
        self.assertEqual(usage['claude']['delta']['five_hour'], 2.0)

    def test_an_interrupted_job_never_waits_for_a_meter_read(self):
        spec = {'meter_before': usage_meter(at=-400), 'cwd': self.work}
        attempts = [{'engine': 'claude', 'argv': ['x'], 'started_at': common.iso(common.now())}]
        with mock.patch.object(jobs, '_meter', side_effect=AssertionError('must not read')):
            usage, _ = jobs._job_usage('x', spec, attempts, {}, common.now(), None, quick=True)
        self.assertEqual(usage['engines'], ['claude'])

    def test_a_hung_after_reader_cannot_block_the_finish_and_the_receipt_says_so(self):
        release = threading.Event()
        calls = []

        def read(policy):
            calls.append(1)
            if len(calls) <= 2:
                return usage_meter(at=-5)
            release.wait(30)  # the end-of-job reader hangs
            return usage_meter(claude=(11.0, 40.0))

        jobs._read_meter = read
        pol = common.load_policy()
        pol['meter']['after_read_timeout_s'] = 0.3
        t0 = time.monotonic()
        try:
            with mock.patch.object(common, 'load_policy', return_value=pol):
                r, out = self.run_claude()
            took = time.monotonic() - t0
        finally:
            release.set()
        self.assertEqual(r['status'], 'done')
        self.assertLess(took, 20)
        self.assertIn('usage after: meter read timed out', r['notes'])
        self.assertEqual(r['usage']['after_note'], 'usage after: meter read timed out')
        self.assertIsNone(r['usage']['claude']['after'])
        self.assertIsNone(r['usage']['claude']['delta'])
        self.assertIn('usage after: meter read timed out', out)

    def test_the_after_read_deadline_is_a_policy_value(self):
        self.assertEqual(common.load_policy()['meter']['after_read_timeout_s'], 10)
        self.assertEqual(common.load_policy()['meter']['overlap_scan_timeout_s'], 5)

    def test_bounded_after_meter_returns_at_the_deadline_with_a_daemon_thread(self):
        release = threading.Event()
        try:
            with mock.patch.object(jobs, '_meter', lambda policy, fresh=False, auto_refresh=True: release.wait(30)):
                t0 = time.monotonic()
                m, timed_out = jobs._bounded_after_meter({}, 0.2)
            self.assertTrue(timed_out)
            self.assertIsNone(m)
            self.assertLess(time.monotonic() - t0, 5)
            self.assertTrue(all(t.daemon for t in threading.enumerate() if t.name == 'twinlane-after-meter'))
        finally:
            release.set()

    # --- the after-job reader returns its reading; only the caller writes, inside the deadline

    def real_meter_with_fake_readers(self, release):
        """jobs._meter -> the real meter.read, whose two readers wait on `release` and then save/return a reading."""
        from twinlane import meter as meter_mod
        stamp = common.iso(common.now() - dt.timedelta(seconds=30))
        later = common.iso(common.now() + dt.timedelta(days=3))
        claude = {'source': 'oauth', 'reading_at': stamp, 'extra_usage_enabled': False,
                  'windows': [{'name': 'five_hour', 'pct': 20.0, 'resets_at': later},
                              {'name': 'seven_day', 'pct': 20.0, 'resets_at': later}]}
        codex = {'source': 'app-server', 'reading_at': stamp, 'credits': {'has': False, 'unlimited': False},
                 'limit_reached_type': None, 'ordinary_usage_allowed': True, 'plan': 'pro',
                 'windows': [{'name': 'weekly', 'pct': 20.0, 'resets_at': later}]}

        def slow_claude(*a, **k):
            release.wait(15)
            meter_mod.save_claude_cache(claude, 'oauth')  # what the real reader does after its fetch
            return dict(claude)

        def slow_codex(*a, **k):
            release.wait(15)
            return dict(codex)

        jobs._read_meter = None
        return (mock.patch.object(meter_mod, 'read_claude_raw', slow_claude),
                mock.patch.object(meter_mod, 'read_codex_raw', slow_codex))

    @staticmethod
    def after_meter_threads_done():
        for t in [t for t in threading.enumerate() if t.name == 'twinlane-after-meter']:
            t.join(15)
        return not any(t.name == 'twinlane-after-meter' for t in threading.enumerate())

    def state_files(self):
        return sorted(n for n in os.listdir(common.STATE) if n in ('meter.json', 'claude-usage.json'))

    def test_a_reader_past_its_deadline_leaves_no_cache_side_effect(self):
        for existing in (False, True):
            with self.subTest(existing_cache=existing):
                for n in self.state_files():
                    os.remove(os.path.join(common.STATE, n))
                before = {}
                if existing:
                    common.write_json(os.path.join(common.STATE, 'meter.json'), usage_meter(claude=(77.0, 77.0), at=-3000))
                    common.write_json(os.path.join(common.STATE, 'claude-usage.json'),
                                      {'reading_at': common.iso(common.now() - dt.timedelta(hours=2)), 'source': 'old',
                                       'windows': [], 'extra_usage_enabled': False})
                    for n in self.state_files():
                        with open(os.path.join(common.STATE, n), 'rb') as h:
                            before[n] = h.read()
                release = threading.Event()
                p1, p2 = self.real_meter_with_fake_readers(release)
                with p1, p2:
                    m, timed_out = jobs._bounded_after_meter(common.load_policy(), 0.2)
                    self.assertEqual((m, timed_out), (None, True))
                    release.set()  # the abandoned reader now finishes, past the deadline
                    self.assertTrue(self.after_meter_threads_done())
                after = {}
                for n in self.state_files():
                    with open(os.path.join(common.STATE, n), 'rb') as h:
                        after[n] = h.read()
                self.assertEqual(after, before)

    def test_a_reader_inside_its_deadline_is_written_by_the_caller(self):
        release = threading.Event()
        release.set()
        p1, p2 = self.real_meter_with_fake_readers(release)
        with p1, p2:
            m, timed_out = jobs._bounded_after_meter(common.load_policy(), 5)
        self.assertFalse(timed_out)
        self.assertEqual(m['codex']['max_pct'], 20.0)
        self.assertEqual(self.state_files(), ['claude-usage.json', 'meter.json'])

    def test_queued_cache_writes_never_outlast_the_deadline_on_a_busy_lock(self):
        # with the cache lock held, a 50 ms budget took 356 ms, because the queued writes waited
        # for the lock AFTER the reader returned. Each write now waits only for the time left, or is dropped.
        release = threading.Event()
        release.set()
        p1, p2 = self.real_meter_with_fake_readers(release)
        hold, held = threading.Event(), threading.Event()

        def holder():
            with common.file_lock(os.path.join(common.STATE, 'meter.json.lock'), timeout_s=5), \
                    common.file_lock(os.path.join(common.STATE, 'claude-usage.json.lock'), timeout_s=5):
                held.set()
                hold.wait(5)

        t = threading.Thread(target=holder, daemon=True)
        t.start()
        self.assertTrue(held.wait(5))
        try:
            for budget in (0.05, 0.3):
                with self.subTest(budget=budget), p1, p2:
                    t0 = time.monotonic()
                    m, timed_out = jobs._bounded_after_meter(common.load_policy(), budget)
                    took = time.monotonic() - t0
                    self.assertLess(took, budget + 0.15)
                    self.assertNotIn('meter.json', self.state_files())        # refused by the busy lock
                    self.assertNotIn('claude-usage.json', self.state_files())
        finally:
            hold.set()
            t.join(5)

    # --- the after-job bound covers the cache WRITE too, not just the lock (slow write, sharing violations)

    @staticmethod
    def cache_writer_done():
        for t in [t for t in threading.enumerate() if t.name == 'twinlane-after-meter-cache']:
            t.join(15)
        return not any(t.name == 'twinlane-after-meter-cache' for t in threading.enumerate())

    def cache_bytes(self):
        out = {}
        for n in self.state_files():
            with open(os.path.join(common.STATE, n), 'rb') as h:
                out[n] = h.read()
        return out

    def plant_old_cache(self, existing):
        for n in self.state_files():
            os.remove(os.path.join(common.STATE, n))
        if existing:
            common.write_json(os.path.join(common.STATE, 'meter.json'), usage_meter(claude=(77.0, 77.0), at=-3000))
            common.write_json(os.path.join(common.STATE, 'claude-usage.json'),
                              {'reading_at': common.iso(common.now() - dt.timedelta(hours=2)), 'source': 'old',
                               'windows': [], 'extra_usage_enabled': False})
        return self.cache_bytes()

    def test_a_slow_cache_write_does_not_extend_the_after_meter_bound(self):
        # with a 30 ms budget and a 180 ms write the call returned after ~181 ms, timed_out False.
        real_write = common.write_json
        budget, delay = 0.4, 1.5
        for existing in (False, True):
            with self.subTest(existing_cache=existing):
                old = self.plant_old_cache(existing)
                release = threading.Event()
                release.set()
                p1, p2 = self.real_meter_with_fake_readers(release)

                def slow_write(path, data, deadline=None):
                    time.sleep(delay)
                    return real_write(path, data, deadline=deadline)

                notes = []
                with p1, p2, mock.patch.object(common, 'write_json', slow_write):
                    t0 = time.monotonic()
                    m, timed_out = jobs._bounded_after_meter(common.load_policy(), budget, notes)
                    took = time.monotonic() - t0
                    self.assertLess(took, budget + 0.25)
                    self.assertFalse(timed_out)                       # the reading itself arrived in time
                    self.assertEqual(m['codex']['max_pct'], 20.0)
                    self.assertEqual(notes, ['meter cache write still pending at the deadline'])
                    self.assertTrue(self.cache_writer_done())         # the late writer finishes after the deadline...
                self.assertEqual(self.cache_bytes(), old)             # ...and changed nothing: old file or no file
                self.assertEqual([n for n in os.listdir(common.STATE) if n.startswith('.tmp-')], [])

    def test_windows_sharing_violations_on_the_cache_replace_are_bounded_by_the_deadline(self):
        budget = 0.4
        for existing in (False, True):
            with self.subTest(existing_cache=existing):
                old = self.plant_old_cache(existing)
                release = threading.Event()
                release.set()
                p1, p2 = self.real_meter_with_fake_readers(release)
                attempts = []

                def busy(src, dst):
                    attempts.append(time.monotonic())
                    raise PermissionError(13, 'sharing violation')

                notes = []
                with p1, p2, mock.patch('os.replace', busy):
                    t0 = time.monotonic()
                    m, timed_out = jobs._bounded_after_meter(common.load_policy(), budget, notes)
                    took = time.monotonic() - t0
                    self.assertLess(took, budget + 0.25)              # 20 retries used to sleep ~10 s
                    self.assertFalse(timed_out)
                    self.assertEqual(notes, ['meter cache write still pending at the deadline'])
                    self.assertTrue(self.cache_writer_done())
                    self.assertTrue(attempts)                         # it did try, and stopped trying at the deadline
                    self.assertLess(max(attempts) - t0, budget + 0.05)
                self.assertEqual(self.cache_bytes(), old)
                self.assertEqual([n for n in os.listdir(common.STATE) if n.startswith('.tmp-')], [])

    def test_a_replace_started_before_the_deadline_lands_late_and_the_note_says_pending(self):
        # A replace begun before the deadline finished after it, while the note said
        # The cache was not updated. The note now says pending, and the late file is the complete new one.
        budget = 0.4
        old = self.plant_old_cache(True)
        release = threading.Event()
        release.set()
        p1, p2 = self.real_meter_with_fake_readers(release)
        real_replace = os.replace

        def slow(src, dst):
            time.sleep(budget + 0.3)          # starts inside the budget, finishes well after it
            real_replace(src, dst)

        notes = []
        with p1, p2, mock.patch('os.replace', slow):
            t0 = time.monotonic()
            m, timed_out = jobs._bounded_after_meter(common.load_policy(), budget, notes)
            self.assertLess(time.monotonic() - t0, budget + 0.25)
            self.assertFalse(timed_out)
            self.assertEqual(notes, [jobs.CACHE_DEADLINE_NOTE])
            self.assertIn('pending', jobs.CACHE_DEADLINE_NOTE)
            self.assertTrue(self.cache_writer_done())
        now = self.cache_bytes()
        self.assertNotEqual(now, old)                      # the late replace did land
        for blob in now.values():
            json.loads(blob)                               # and every cache file is complete JSON
        self.assertEqual([n for n in os.listdir(common.STATE) if n.startswith('.tmp-')], [])

    def test_a_transient_sharing_violation_inside_the_deadline_still_writes_the_complete_cache(self):
        self.plant_old_cache(False)
        release = threading.Event()
        release.set()
        p1, p2 = self.real_meter_with_fake_readers(release)
        real_replace, fails = os.replace, {}

        def flaky(src, dst):
            fails[dst] = fails.get(dst, 0) + 1
            if fails[dst] <= 2:
                raise PermissionError(13, 'sharing violation')
            return real_replace(src, dst)

        notes = []
        with p1, p2, mock.patch('os.replace', flaky):
            m, timed_out = jobs._bounded_after_meter(common.load_policy(), 5, notes)
        self.assertEqual((timed_out, notes), (False, []))
        self.assertEqual(self.state_files(), ['claude-usage.json', 'meter.json'])
        self.assertEqual(common.read_json(os.path.join(common.STATE, 'meter.json'))['codex']['max_pct'], 20.0)

    def test_a_skipped_cache_write_is_noted_in_the_receipt_and_never_loses_it(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))

        def bounded(policy, timeout_s, notes=None):
            notes.append(jobs.CACHE_DEADLINE_NOTE)
            return usage_meter(claude=(11.0, 40.0)), False

        with mock.patch.object(jobs, '_bounded_after_meter', bounded):
            r, out = self.run_claude()
        self.assertEqual(r['status'], 'done')
        self.assertEqual(r['usage']['claude']['delta']['five_hour'], 1.0)   # the reading is in the receipt anyway
        self.assertEqual(r['usage']['cache_note'], 'meter cache write still pending at the deadline')
        self.assertIn('meter cache write still pending at the deadline', r['notes'])
        self.assertIn('meter cache write still pending at the deadline', out)

    def test_an_in_deadline_reader_that_began_before_a_newer_reading_does_not_overwrite_it(self):
        from twinlane import meter as meter_mod
        release = threading.Event()
        p1, p2 = self.real_meter_with_fake_readers(release)
        out = {}
        with p1, p2:
            t = threading.Thread(target=lambda: out.update(r=jobs._bounded_after_meter(common.load_policy(), 10)))
            t.start()
            time.sleep(0.3)  # the after-reader is blocked in its fetch; a newer reading lands meanwhile
            newer = usage_meter(claude=(90.0, 90.0), codex=90.0)
            meter_mod.save_claude_cache({'reading_at': newer['claude']['reading_at'], 'windows': newer['claude']['windows'],
                                         'extra_usage_enabled': False}, 'newer')
            meter_mod.read(fresh=True, policy=common.load_policy(),
                           _claude_reader=lambda: {'source': 'oauth', 'reading_at': newer['claude']['reading_at'],
                                                   'windows': newer['claude']['windows'], 'extra_usage_enabled': False},
                           _codex_reader=lambda: {'source': 'app-server', 'reading_at': newer['codex']['reading_at'],
                                                  'windows': newer['codex']['windows'],
                                                  'credits': {'has': False, 'unlimited': False},
                                                  'ordinary_usage_allowed': True})
            release.set()
            t.join(15)
        self.assertFalse(out['r'][1])  # it finished inside its deadline and wrote its (older) reading
        cached = common.read_json(os.path.join(common.STATE, 'meter.json'))
        self.assertEqual(cached['claude']['max_pct'], 90.0)
        self.assertEqual(cached['codex']['max_pct'], 90.0)
        self.assertEqual(common.read_json(os.path.join(common.STATE, 'claude-usage.json'))['source'], 'newer')

    def plant_finished(self, name, start_s, end_s, engine='claude'):
        now = common.now()
        d = os.path.join(common.JOBS, name)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'result.json'), {
            'status': 'done', 'engine': engine, 'started_at': common.iso(now + dt.timedelta(seconds=start_s)),
            'ended_at': common.iso(now + dt.timedelta(seconds=end_s)),
            'attempts': [{'engine': engine, 'argv': ['x'], 'started_at': common.iso(now + dt.timedelta(seconds=start_s)),
                          'ended_at': common.iso(now + dt.timedelta(seconds=end_s))}]})

    def test_an_overlap_behind_more_than_100_newer_jobs_is_still_found(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))
        self.plant_finished('20200101-000000-claude-old-but-long', -3600, 3600)  # oldest folder, spans now
        for i in range(120):  # 120 newer folders that ended long ago
            self.plant_finished('20260101-%06d-claude-n%03d' % (i, i), -7200, -7000)
        r, out = self.run_claude()
        u = r['usage']['claude']
        self.assertEqual(u['overlapping_jobs'], ['20200101-000000-claude-old-but-long'])
        self.assertTrue(u['approximate'])
        self.assertNotIn('truncated', ' '.join(u['reasons']))

    def test_a_job_created_after_the_window_is_skipped_without_reading_it(self):
        end = common.now()
        self.plant_finished('29990101-000000-claude-future', -5, 5)
        reads = []
        real = common.read_json
        with mock.patch.object(common, 'read_json', lambda p, *a: reads.append(p) or real(p, *a)):
            out, cut = jobs._overlapping_jobs('me', end - dt.timedelta(seconds=10), end)
        self.assertEqual((out, cut), ({}, False))
        self.assertEqual(reads, [])

    def test_a_job_whose_runner_identity_is_unreadable_still_overlaps(self):
        # `unknown` is a live job, so the usage delta of a job beside it is approximate.
        end = common.now()
        child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        jid = '20200101-000000-codex-unk'
        d = os.path.join(common.JOBS, jid)
        os.makedirs(d)
        common.write_json(os.path.join(d, 'job.json'), {'id': jid, 'primary': {'engine': 'codex'}})
        common.write_json(os.path.join(d, 'status.json'), {
            'status': 'running', 'pid': child.pid, 'pid_created': common.process_created(child.pid),
            'engine': 'codex', 'started_at': common.iso(end - dt.timedelta(seconds=60))})
        with unreadable_identity(child.pid):
            out, cut = jobs._overlapping_jobs('me', end - dt.timedelta(seconds=10), end)
        self.assertEqual((out, cut), ({'codex': [jid]}, False))

    def test_a_cut_short_overlap_scan_is_labelled_approximate(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))
        for i in range(5):
            self.plant_finished('20260101-%06d-claude-n%03d' % (i, i), -7200, -7000)
        pol = common.load_policy()
        pol['meter']['overlap_scan_timeout_s'] = -1  # the budget is gone before the first folder
        with mock.patch.object(common, 'load_policy', return_value=pol):
            r, out = self.run_claude()
        u = r['usage']['claude']
        self.assertTrue(u['approximate'])
        self.assertIn('overlap scan truncated', u['reasons'])
        self.assertIn('APPROXIMATE (overlap scan truncated)', out)
        self.assertTrue(r['usage']['codex']['approximate'])  # the scan covers every engine

    # --- the scan budget is a wall-clock bound, whichever step is slow

    SLOW_S, BUDGET_S = 1.0, 0.05  # a wide gap, so a slow CI runner still tells budget from slow step

    def slow_scan(self, where, running=False):
        """Run _overlapping_jobs with ONE kind of filesystem step made slow; -> (result, seconds it took)."""
        for name in os.listdir(common.JOBS):  # a previous subtest may have failed before cleaning up
            shutil.rmtree(os.path.join(common.JOBS, name), ignore_errors=True)
        for i in range(3):
            self.plant_finished('20260101-%06d-claude-n%03d' % (i, i), -7200, -7000)
        if running:  # no result.json: such a folder is judged by job_status
            os.makedirs(os.path.join(common.JOBS, '20260101-000009-claude-running'))
        now = common.now()
        real = {'read_json': common.read_json, 'isdir': os.path.isdir, 'listdir': os.listdir,
                'status': jobs.job_status}
        slow = self.SLOW_S

        def delay(fn):
            def wrapped(*a, **k):
                time.sleep(slow)
                return fn(*a, **k)
            return wrapped

        patches = {'read_json': mock.patch.object(common, 'read_json', delay(real['read_json'])),
                   'isdir': mock.patch.object(os.path, 'isdir', delay(real['isdir'])),
                   'listdir': mock.patch.object(os, 'listdir', delay(real['listdir'])),
                   'status': mock.patch.object(jobs, 'job_status', delay(real['status']))}
        with patches[where]:
            t0 = time.monotonic()
            res = jobs._overlapping_jobs('me', now - dt.timedelta(seconds=10), now, self.BUDGET_S)
            took = time.monotonic() - t0
        time.sleep(self.SLOW_S * 4)  # let the abandoned worker run out before the patch is gone
        return res, took

    def test_a_slow_step_cannot_stretch_the_overlap_scan_past_its_budget(self):
        for where in ('read_json', 'isdir', 'listdir'):
            with self.subTest(slow_step=where):
                (out, cut), took = self.slow_scan(where)
                self.assertTrue(cut)
                self.assertLess(took, self.SLOW_S * 0.8, 'waited for the slow step instead of the budget')

    def test_a_slow_status_check_of_a_running_job_is_bounded_too(self):
        (out, cut), took = self.slow_scan('status', running=True)
        self.assertTrue(cut)
        self.assertLess(took, self.SLOW_S * 0.8)

    def test_a_scan_inside_its_budget_is_complete_and_not_truncated(self):
        self.plant_finished('20260101-000001-claude-during', -5, 3600)
        now = common.now()
        out, cut = jobs._overlapping_jobs('me', now - dt.timedelta(seconds=10), now, 5.0)
        self.assertEqual((out, cut), ({'claude': ['20260101-000001-claude-during']}, False))

    def test_a_timed_out_scan_keeps_what_it_found_and_is_still_labelled_truncated(self):
        self.plant_finished('20260101-000002-claude-found-first', -5, 3600)   # sorts first (reverse order)
        self.plant_finished('20260101-000001-claude-slow', -7200, -7000)
        now = common.now()
        real = common.read_json

        def slow_for_second(path, *a):
            if 'claude-slow' in path:
                time.sleep(self.SLOW_S)
            return real(path, *a)

        with mock.patch.object(common, 'read_json', slow_for_second):
            out, cut = jobs._overlapping_jobs('me', now - dt.timedelta(seconds=10), now, self.BUDGET_S)
        self.assertTrue(cut)
        self.assertEqual(out, {'claude': ['20260101-000002-claude-found-first']})
        time.sleep(self.SLOW_S * 2)

    def test_a_slow_scan_labels_the_receipt_approximate_within_the_budget(self):
        self.plant_finished('20260101-000001-claude-slow', -7200, -7000)
        real = common.read_json

        def slow_for_planted(path, *a):
            if 'claude-slow' in path:
                time.sleep(self.SLOW_S)
            return real(path, *a)

        pol = common.load_policy()
        pol['meter']['overlap_scan_timeout_s'] = self.BUDGET_S
        spec = {'meter_before': usage_meter(at=-5), 'cwd': self.work}
        attempts = [{'engine': 'claude', 'argv': ['x'], 'started_at': common.iso(common.now())}]
        with mock.patch.object(common, 'read_json', slow_for_planted):
            t0 = time.monotonic()
            usage, _ = jobs._job_usage('x', spec, attempts, {}, common.now(), pol, quick=True)
            took = time.monotonic() - t0
        time.sleep(self.SLOW_S * 2)
        self.assertLess(took, self.SLOW_S * 0.8)
        self.assertIn('overlap scan truncated', usage['claude']['reasons'])
        self.assertTrue(usage['claude']['approximate'])

    def test_an_unreadable_job_folder_also_counts_as_a_truncated_scan(self):
        self.plant_finished('20260101-000001-claude-n', -7200, -7000)
        now = common.now()
        with mock.patch.object(common, 'read_json', side_effect=OSError('locked')):
            out, cut = jobs._overlapping_jobs('me', now - dt.timedelta(seconds=10), now)
        self.assertTrue(cut)

    def test_claude_tokens_come_from_the_result_event(self):
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(claude=(11.0, 40.0)))
        r, out = self.run_claude()
        self.assertEqual(r['attempts'][0]['token_usage'], {
            'source': 'claude result', 'input_tokens': 120, 'output_tokens': 30, 'cached_input_tokens': 900,
            'cache_write_input_tokens': 50})
        self.assertEqual(r['usage']['tokens']['claude']['input_tokens'], 120)
        self.assertIn('tokens claude: in 120 cached 900 cache-write 50 out 30 (claude result)', out)

    def test_claude_tokens_fall_back_to_model_usage_sums(self):
        s = jobs.parse_stream_lines([json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False, 'modelUsage': {
            'a': {'inputTokens': 10, 'outputTokens': 5, 'cacheReadInputTokens': 7},
            'b': {'inputTokens': 1, 'outputTokens': 2}}})])
        self.assertEqual(jobs._claude_tokens(s['usage'], s['model_usage']), {
            'source': 'claude modelUsage', 'input_tokens': 11, 'output_tokens': 7, 'cached_input_tokens': 7})
        self.assertIsNone(jobs._claude_tokens(None, None))

    def run_codex(self):
        rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'parallel',
                                '--engine', 'codex')
        self.assertEqual(rc, 0, out + err)
        return self.receipt(self.only_job()), out

    def test_codex_tokens_come_from_the_session_rollout(self):
        os.environ['FAKE_CODEX_ROLLOUT_ROOT'] = os.path.join(common.CODEX_DIR, 'sessions')
        self.two_phase(lambda: usage_meter(at=-5), lambda: usage_meter(codex=33.0))
        r, out = self.run_codex()
        self.assertEqual(r['attempts'][0]['token_usage'], {
            'source': 'rollout', 'input_tokens': 2500, 'cached_input_tokens': 1250, 'output_tokens': 40,
            'reasoning_output_tokens': 7, 'total_tokens': 2540})
        self.assertEqual(r['usage']['codex']['delta'], {'weekly': 3.0})
        self.assertIn('usage codex: weekly +3pp', out)
        self.assertIn('tokens codex: in 2,500 cached 1,250 out 40 reasoning 7 total 2,540 (rollout)', out)

    def test_codex_tokens_fall_back_to_the_exec_events_then_to_codex_run(self):
        os.environ['FAKE_CODEX_USAGE'] = '1'
        r, _ = self.run_codex()
        self.assertEqual(r['attempts'][0]['token_usage'], {
            'source': 'codex events', 'input_tokens': 300, 'cached_input_tokens': 30, 'output_tokens': 15})
        self.fresh_jobs()
        os.environ.pop('FAKE_CODEX_USAGE')
        r, _ = self.run_codex()
        self.assertEqual(r['attempts'][0]['token_usage'], {'source': 'built-in-runner', 'total_tokens': 123})

    def test_a_failover_sums_tokens_per_engine(self):
        total = jobs.sum_tokens([
            {'engine': 'claude', 'token_usage': {'source': 'claude result', 'input_tokens': 5}},
            {'engine': 'codex', 'token_usage': {'source': 'rollout', 'input_tokens': 7, 'total_tokens': 9}},
            {'engine': 'claude', 'token_usage': {'source': 'claude result', 'input_tokens': 6}},
            {'engine': 'claude'}], 'claude')
        self.assertEqual(total, {'source': 'claude result', 'input_tokens': 11})
        self.assertIsNone(jobs.sum_tokens([], 'codex'))

    def test_old_receipts_without_usage_print_nothing_new(self):
        self.assertEqual(jobs.usage_lines({'id': 'x', 'status': 'done'}), [])


class AutoRefreshPlanning(JobsBase):
    def test_plan_run_asks_for_the_refresh_unless_codex_is_forced_and_notes_it(self):
        seen = []

        def fake_meter(policy, fresh=False, auto_refresh=True):
            seen.append(auto_refresh)
            m = meter_with()
            m['refreshed_now'] = ['claude']
            m['claude']['refresh'] = {'ok': True}
            return m

        with mock.patch.object(jobs, '_meter', fake_meter):
            rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work)
            self.assertEqual(rc, 0, out + err)
            self.assertTrue(seen[0])  # the plan read of a real run may refresh
            seen.clear()
            self.cli('run', '--brief', self.brief, '--cd', self.work, '--engine', 'codex', '--kind', 'parallel')
            self.assertFalse(seen[0])  # a forced Codex run never needs Claude
        self.assertIn('note: Claude CLI token had expired: refreshed automatically', out)

    def test_a_dry_run_never_asks_for_the_refresh(self):
        seen = []

        def fake_meter(policy, fresh=False, auto_refresh=True):
            seen.append(auto_refresh)
            return meter_with()

        with mock.patch.object(jobs, '_meter', fake_meter):
            for extra in ((), ('--engine', 'codex', '--kind', 'parallel')):
                rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--dry-run', *extra)
                self.assertEqual(rc, 0, out + err)
        self.assertEqual(seen, [False, False])

    def test_a_dry_run_says_the_meter_is_unknown_and_a_real_run_would_refresh_once(self):
        self.write_config({'meter': {'auto_refresh_claude': True}})
        m = meter_with()
        m['claude'].update(status='unknown', max_pct=None, windows=[],
                           token_expired_at=common.iso(common.now() - dt.timedelta(minutes=5)))
        # The refresh is a model call: it runs only when the last recorded reading shows extra usage OFF.
        with mock.patch.object(jobs, '_meter', lambda policy, fresh=False, auto_refresh=True: m), \
                mock.patch('twinlane.meter.refresh_blocker', return_value=None):
            rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--dry-run', '--json')
        self.assertEqual(rc, 0, out + err)
        text = json.dumps(json.loads(out))
        self.assertIn('Claude meter is unknown', text)
        self.assertIn('a real run would refresh it once', text)
        self.assertIn('made no model call', text)
        self.assertEqual(jobs._job_ids(), [])  # and it created nothing

    def test_dry_run_routes_away_from_unknown_without_a_model_call(self):
        m = meter_with()
        m['claude'].update(status='unknown', max_pct=None, windows=[],
                           token_expired_at=common.iso(common.now() - dt.timedelta(minutes=5)))
        # The refresh is a model call: it runs only when the last recorded reading shows extra usage OFF.
        with mock.patch.object(jobs, '_meter', lambda policy, fresh=False, auto_refresh=True: m),                 mock.patch('twinlane.meter.refresh_blocker', return_value=None):
            rc, out, err = self.cli('run', '--brief', self.brief, '--cd', self.work, '--dry-run', '--json')
        self.assertEqual(rc, 0, out + err)
        text = json.dumps(json.loads(out))
        self.assertEqual(json.loads(out)['engine'], 'codex')
        self.assertNotIn('would refresh', text)
        self.assertEqual(jobs._job_ids(), [])  # and it created nothing

    def test_preflight_and_failover_refresh_only_for_claude(self):
        seen = []

        def fake_meter(policy, fresh=False, auto_refresh=True):
            seen.append(auto_refresh)
            return meter_with()

        spec = {'kind': 'intel', 'urgent': False}
        with mock.patch.object(jobs, '_meter', fake_meter):
            jobs.preflight(spec, {'engine': 'claude'}, POLICY, False)
            jobs.preflight(spec, {'engine': 'codex', 'tier': 'sol'}, POLICY, False)
            jobs.plan_failover(dict(spec, profile='intel'), {'engine': 'claude'}, POLICY)   # other = codex
            jobs.plan_failover(dict(spec, profile='intel'), {'engine': 'codex'}, POLICY)    # other = claude
        self.assertEqual(seen, [True, False, False, True])


class CodexCreditAdmission(JobsBase):
    """Twinlane refuses Codex paid credits itself, for every model, and does not trust the transport's flag."""

    SPEC = {'kind': 'sweep', 'urgent': False}

    def params(self, tier='luna', model=None):
        return jobs.engine_params('codex', 'sweep', {}, POLICY, 'intel', model=model, tier=tier)

    def pre(self, meter, params, policy=None):
        jobs._read_meter = lambda p: meter
        return jobs.preflight(self.SPEC, params, policy or POLICY, False)[0]

    def meter_codex(self, **over):
        m = meter_with()
        m['codex'].update(over)
        return m

    def test_on_credits_refuses_every_model_luna_included(self):
        for tier, model in (('luna', None), ('sol', None), ('sol-high', None), ('astra', None),
                            ('sol-high', 'gpt-6-luna')):
            why = self.pre(self.meter_codex(on_credits=True), self.params(tier, model))
            self.assertIn('paid credits', why or '', (tier, model))
            self.assertIn('codex.allow_credits', why)  # says how the user can OK it

    def test_past_included_usage_refuses_like_credits(self):
        for over in ({'ordinary_usage_allowed': False}, {'limit_reached_type': 'weekly'},
                     {'spend_control_reached': True}):
            self.assertIn('paid credits', self.pre(self.meter_codex(**over), self.params('luna')) or '', over)

    def test_unknown_reading_refuses_every_model(self):
        for tier in ('luna', 'sol-high'):
            # No age: twinlane's own check refuses. A reading marked unknown is stopped by the route (not usable)
            # first, and by the credit check too if the route were wrong.
            self.assertIn('reading is unknown', self.pre(self.meter_codex(age_s=None), self.params(tier)) or '')
            self.assertIn('unknown', self.pre(self.meter_codex(status='unknown'), self.params(tier)) or '')
            self.assertIn('reading is unknown', jobs.codex_credit_refusal(self.meter_codex(status='unknown'),
                                                                          self.params(tier), POLICY) or '')

    def test_the_effective_model_is_judged_not_the_tier(self):
        # A review probe: a 600 s old reading refused tier=luna but accepted tier=sol-high + model=gpt-6-luna.
        old = self.meter_codex(age_s=600)
        for p in (self.params('luna'), self.params('sol-high', 'gpt-6-luna'), self.params('sol-high')):
            self.assertIn('600s old', self.pre(old, p) or '')
        self.assertEqual(jobs.codex_model(self.params('sol-high', 'gpt-6-luna')), 'gpt-6-luna')
        self.assertIn('gpt-6-luna', self.pre(old, self.params('sol-high', 'gpt-6-luna')))
        self.assertEqual(jobs.codex_model(self.params('astra')), 'gpt-6-astra')

    def test_explicit_credits_ok_passes(self):
        # The one existing switch: policy.codex.allow_credits (the user's OK); it is also what adds --allow-credits.
        lenient = json.loads(json.dumps(POLICY))
        lenient['codex']['allow_credits'] = True
        self.assertIsNone(self.pre(self.meter_codex(on_credits=True, age_s=900), self.params('sol-high', 'gpt-6-luna'),
                                   lenient))

    def test_included_usage_and_a_fresh_reading_pass(self):
        for tier, model in (('luna', None), ('sol', None), ('sol-high', 'gpt-6-luna'), ('astra', None)):
            self.assertIsNone(self.pre(self.meter_codex(), self.params(tier, model)), (tier, model))

    def test_a_refused_codex_job_starts_nothing(self):
        self.meter = self.meter_codex(on_credits=True)
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'sweep', '--engine', 'codex',
                            '--tier', 'sol-high', '--model', 'gpt-6-luna')
        self.assertEqual(rc, common.EXIT_REFUSED)
        r = self.receipt(self.only_job())
        self.assertEqual(r['status'], 'refused')
        self.assertIn('gpt-6-luna would spend them', r['attempts'][0]['error'])
        self.assertFalse(os.path.exists(self.codex_argv_out))

    def test_failover_from_claude_passes_the_same_checks(self):
        os.environ['FAKE_LIMIT'] = '1'
        self.meter = self.meter_codex(on_credits=True)
        rc, _, _ = self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'intel')
        r = self.receipt(self.only_job())
        self.assertEqual([a['status'] for a in r['attempts']], ['limit_hit', 'refused'])
        self.assertEqual(r['attempts'][1]['engine'], 'codex')
        self.assertIn('paid credits', r['attempts'][1]['error'])
        self.assertFalse(os.path.exists(self.codex_argv_out))  # the transport was never started
        self.assertNotEqual(rc, 0)

    def test_failover_with_an_unknown_codex_reading_is_refused(self):
        os.environ['FAKE_LIMIT'] = '1'
        self.meter = self.meter_codex(age_s=None)
        self.cli('run', '--brief', self.brief, '--cd', self.work, '--kind', 'intel')
        r = self.receipt(self.only_job())
        self.assertEqual([a['status'] for a in r['attempts']], ['limit_hit', 'refused'])
        self.assertIn('reading is unknown', r['attempts'][1]['error'])
        self.assertFalse(os.path.exists(self.codex_argv_out))


if __name__ == '__main__':
    unittest.main()
