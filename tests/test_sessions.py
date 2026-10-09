"""`twinlane sessions` and batch `twinlane handoff --session` tests.

Fake Claude transcripts and Codex rollouts are written into temp dirs (the same ones the baton tests redirect
the readers to); the real ~/.claude and ~/.codex are never read. No network, no models."""
import contextlib
import io
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import test_baton  # noqa: E402  (its base class is reached through the module so discovery does not rerun it)
import test_support  # noqa: E402; isolated temp home before importing twinlane
from twinlane import baton, common, sessions  # noqa: E402

SECRET = 'sk-' + 'ant-api03-ABCDEFGHIJKLMNOPQRSTUVWX'
CLAUDE_A = '11111111-aaaa-4bbb-8ccc-000000000001'
CLAUDE_B = '11111111-aaaa-4bbb-8ccc-000000000002'
CODEX_A = '01a10000-0000-7000-8000-00000000000a'
CODEX_B = '01a10000-0000-7000-8000-00000000000b'
OLD = 600  # seconds: well past the 120 s active window


def jl(obj):
    return json.dumps(obj, ensure_ascii=False)


class Fakes(test_baton.BatonTestBase):
    """Builders for fake transcripts/rollouts plus helpers; every file lives under self.tmp."""

    def setUp(self):
        super().setUp()
        for p in (baton.CLAUDE_PROJECTS, baton.CODEX_SESSIONS, common.JOBS):
            self.assertTrue(os.path.abspath(p).startswith(os.path.abspath(self.tmp)), p)  # never the real dirs
        os.makedirs(common.JOBS, exist_ok=True)

    # ---- Claude
    def c_user(self, text, cwd=None, entry='claude-desktop', sid=CLAUDE_A, **extra):
        return jl(dict({'type': 'user', 'sessionId': sid, 'cwd': cwd or self.repo, 'entrypoint': entry,
                        'message': {'role': 'user', 'content': text}}, **extra))

    def c_result(self, cwd=None, sid=CLAUDE_A):
        return jl({'type': 'user', 'sessionId': sid, 'cwd': cwd or self.repo, 'entrypoint': 'claude-desktop',
                   'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 't1',
                                                            'content': 'ok'}]}})

    def c_asst(self, text, cwd=None, stop='end_turn', sid=CLAUDE_A, model='claude-opus-5-5', tool=False,
               entry='claude-desktop'):
        block = {'type': 'tool_use', 'id': 't1', 'name': 'Bash', 'input': {}} if tool \
            else {'type': 'text', 'text': text}
        return jl({'type': 'assistant', 'sessionId': sid, 'cwd': cwd or self.repo, 'entrypoint': entry,
                   'message': {'role': 'assistant', 'model': model, 'content': [block], 'stop_reason': stop}})

    def put_claude(self, sid, lines, cwd=None, age_s=OLD):
        d = os.path.join(baton.CLAUDE_PROJECTS, baton.claude_dir_name(cwd or self.repo))
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, sid + '.jsonl')
        with open(path, 'w', encoding='utf-8', newline='\n') as h:
            h.write('\n'.join(lines) + '\n')
        t = time.time() - age_s
        os.utime(path, (t, t))
        return path

    # ---- Codex
    def x_meta(self, sid, cwd=None, originator='Codex Desktop', source='vscode'):
        return jl({'type': 'session_meta', 'payload': {'id': sid, 'cwd': cwd or self.repo,
                                                       'originator': originator, 'source': source}})

    @staticmethod
    def x_ev(kind, turn='t1', **fields):
        return jl({'type': 'event_msg', 'payload': dict({'type': kind, 'turn_id': turn}, **fields)})

    @staticmethod
    def x_user(text):
        return jl({'type': 'response_item', 'payload': {'type': 'message', 'role': 'user',
                                                        'content': [{'type': 'input_text', 'text': text}]}})

    @staticmethod
    def x_ctx(model='gpt-6.1-sol', turn='t1'):
        return jl({'type': 'turn_context', 'payload': {'turn_id': turn, 'model': model}})

    def put_codex(self, sid, lines, age_s=OLD):
        d = os.path.join(baton.CODEX_SESSIONS, '2026', '10', '08')
        os.makedirs(d, exist_ok=True)
        path = os.path.join(d, f'rollout-2026-10-08T11-00-00-{sid}.jsonl')
        with open(path, 'w', encoding='utf-8', newline='\n') as h:
            h.write('\n'.join(lines) + '\n')
        t = time.time() - age_s
        os.utime(path, (t, t))
        return path

    def codex_done(self, sid, user='do the thing', answer='All done.', age_s=OLD, **meta):
        return self.put_codex(sid, [self.x_meta(sid, **meta), self.x_ev('task_started'), self.x_ctx(),
                                    self.x_user(user), self.x_ev('task_complete', last_agent_message=answer)], age_s)

    def listed(self, **kw):
        return {(r['engine'], r['id']): r for r in sessions.list_sessions(limit=0, **kw)}

    def run_sessions(self, argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = sessions.cli(argv)
        return rc, out.getvalue(), err.getvalue()


class StateTests(Fakes):
    def state_of(self, engine, sid):
        return self.listed()[(engine, sid)]['state']

    def test_claude_states(self):
        cases = {
            'waiting': [self.c_user('hi'), self.c_asst('Done. Anything else?')],
            'user-last': [self.c_user('hi'), self.c_asst('ok'), self.c_user('now do more')],
            'tool-result-last': [self.c_user('hi'), self.c_asst('', tool=True, stop='tool_use'), self.c_result()],
            'tool-pending': [self.c_user('hi'), self.c_asst('', tool=True, stop='tool_use')],
            'text-before-tool': [self.c_user('hi'), self.c_asst('Let me look.', stop='tool_use')],
        }
        want = {'waiting': 'waiting-on-user', 'user-last': 'mid-turn', 'tool-result-last': 'mid-turn',
                'tool-pending': 'mid-turn', 'text-before-tool': 'mid-turn'}
        for name, lines in cases.items():
            sid = f'22222222-aaaa-4bbb-8ccc-{abs(hash(name)) % 10**12:012d}'
            self.put_claude(sid, lines)
            self.assertEqual(self.state_of('claude', sid), want[name], name)

    def test_claude_active_and_unknown(self):
        self.put_claude(CLAUDE_A, [self.c_user('hi'), self.c_asst('Done.')], age_s=5)
        self.put_claude(CLAUDE_B, [jl({'type': 'last-prompt', 'sessionId': CLAUDE_B, 'entrypoint': 'claude-desktop'})])
        rows = self.listed()
        self.assertEqual(rows[('claude', CLAUDE_A)]['state'], 'active')  # even though the last turn is the assistant's
        self.assertEqual(rows[('claude', CLAUDE_B)]['state'], 'unknown')

    def test_claude_ignores_sidechain_and_meta_entries(self):
        side = json.loads(self.c_asst('subagent chatter'))
        side['isSidechain'] = True
        lines = [self.c_user('hi'), self.c_asst('Finished.'), jl(side),
                 self.c_user('<local-command-stdout>x</local-command-stdout>'), self.c_user('meta', isMeta=True)]
        self.put_claude(CLAUDE_A, lines)
        row = self.listed()[('claude', CLAUDE_A)]
        self.assertEqual((row['state'], row['last']), ('waiting-on-user', 'Finished.'))

    def test_codex_states(self):
        sid = lambda n: f'01a10000-0000-7000-8000-0000000000{n:02d}'  # noqa: E731
        base = lambda n: [self.x_meta(sid(n)), self.x_ev('task_started'), self.x_user('q')]  # noqa: E731
        self.put_codex(sid(1), base(1) + [self.x_ev('agent_message', message='thinking'),
                                          self.x_ev('task_complete', last_agent_message='Final answer.')])
        self.put_codex(sid(2), base(2) + [self.x_ev('agent_message', message='working on it')])
        self.put_codex(sid(3), base(3) + [self.x_ev('task_complete', last_agent_message='first answer'),
                                          self.x_ev('user_message', message='and another thing')])
        self.put_codex(sid(4), base(4) + [self.x_ev('task_complete', last_agent_message='first answer'),
                                          self.x_ev('task_started', turn='t2')])
        self.put_codex(sid(5), base(5) + [self.x_ev('turn_aborted')])
        self.put_codex(sid(6), [self.x_meta(sid(6)), self.x_user('only a question')])
        self.put_codex(sid(7), base(7) + [self.x_ev('task_complete', last_agent_message='x')], age_s=3)
        rows = self.listed()
        got = {n: rows[('codex', sid(n))]['state'] for n in range(1, 8)}
        self.assertEqual(got, {1: 'waiting-on-user', 2: 'mid-turn', 3: 'mid-turn', 4: 'mid-turn',
                               5: 'waiting-on-user', 6: 'unknown', 7: 'active'})
        self.assertEqual(rows[('codex', sid(1))]['last'], 'Final answer.')
        self.assertEqual(rows[('codex', sid(2))]['last'], 'working on it')


class FilterTests(Fakes):
    def test_workers_subagents_and_imports_are_left_out(self):
        self.put_claude(CLAUDE_A, [self.c_user('mine'), self.c_asst('ok')])
        self.put_claude(CLAUDE_B, [self.c_user('worker brief', entry='sdk-cli', sid=CLAUDE_B),
                                   self.c_asst('worker report', sid=CLAUDE_B, entry='sdk-cli')])
        self.codex_done(CODEX_A)
        sub = '01a10000-0000-7000-8000-0000000000c1'
        self.put_codex(sub, [self.x_meta(sub, source={'subagent': {'thread_spawn': {'parent_thread_id': CODEX_A}}}),
                             self.x_ev('task_started'), self.x_ev('task_complete', last_agent_message='sub')])
        imp = '01a10000-0000-7000-8000-0000000000c2'
        self.put_codex(imp, [self.x_meta(imp), self.x_ev('task_started', turn='external-import-turn-1'),
                             self.x_user('imported history'),
                             self.x_ev('task_complete', turn='external-import-turn-1', last_agent_message='imp')])
        exe = '01a10000-0000-7000-8000-0000000000c3'
        self.codex_done(exe, originator='codex_exec', source='exec')
        self.assertEqual(sorted(self.listed()), [('claude', CLAUDE_A), ('codex', CODEX_A)])
        everything = {k: r['state'] for k, r in self.listed(include_jobs=True).items()}
        self.assertEqual(everything[('claude', CLAUDE_B)], 'job')
        self.assertEqual(everything[('codex', exe)], 'job')
        self.assertNotIn(('codex', sub), everything)  # subagent threads are never sessions
        self.assertNotIn(('codex', imp), everything)  # import-only rollouts are never sessions

    def test_imported_turns_do_not_change_the_state(self):
        self.put_codex(CODEX_A, [self.x_meta(CODEX_A), self.x_ev('task_started'), self.x_user('native request'),
                                 self.x_ev('task_complete', last_agent_message='Native answer.'),
                                 self.x_ev('task_started', turn='external-import-turn-9'),
                                 self.x_ev('user_message', turn='external-import-turn-9', message='imported'),
                                 self.x_ev('agent_message', turn='external-import-turn-9', message='imported answer')])
        row = self.listed()[('codex', CODEX_A)]
        self.assertEqual((row['state'], row['last'], row['title']), ('waiting-on-user', 'Native answer.', 'native request'))

    def test_a_session_recorded_by_a_twinlane_job_is_a_job(self):
        self.put_claude(CLAUDE_A, [self.c_user('looks interactive'), self.c_asst('ok')])
        self.put_claude(CLAUDE_B, [self.c_user('also', sid=CLAUDE_B), self.c_asst('ok', sid=CLAUDE_B)])
        self.codex_done(CODEX_A)
        self.codex_done(CODEX_B)
        os.makedirs(os.path.join(common.JOBS, '20261009-000000-claude-aaaa'))
        os.makedirs(os.path.join(common.JOBS, '20261009-000001-codex-bbbb'))
        os.makedirs(os.path.join(common.JOBS, '20261009-000002-codex-cccc'))
        common.write_json(os.path.join(common.JOBS, '20261009-000000-claude-aaaa', 'result.json'),
                          {'status': 'ok', 'session_id': CLAUDE_A})
        common.write_json(os.path.join(common.JOBS, '20261009-000001-codex-bbbb', 'result.json'),
                          {'status': 'ok', 'attempts': [{'engine': 'codex', 'session_id': CODEX_A}]})
        common.write_json(os.path.join(common.JOBS, '20261009-000002-codex-cccc', 'status.json'),
                          {'status': 'running', 'session_id': CODEX_B})
        self.assertEqual(sessions.job_session_ids(), {CLAUDE_A, CODEX_A, CODEX_B})
        self.assertEqual(sorted(self.listed()), [('claude', CLAUDE_B)])
        marked = {k: r['state'] for k, r in self.listed(include_jobs=True).items()}
        self.assertEqual(marked[('claude', CLAUDE_A)], 'job')
        self.assertEqual(marked[('codex', CODEX_A)], 'job')

    def test_order_since_engine_cwd_and_limit(self):
        other = baton.norm_dir(os.path.join(self.tmp, 'other'))
        os.makedirs(other)
        self.put_claude(CLAUDE_A, [self.c_user('newest'), self.c_asst('a')], age_s=300)
        self.codex_done(CODEX_A, age_s=900)
        self.put_claude(CLAUDE_B, [self.c_user('elsewhere', cwd=other, sid=CLAUDE_B),
                                   self.c_asst('b', cwd=other, sid=CLAUDE_B)], cwd=other, age_s=1800)
        self.codex_done(CODEX_B, age_s=3 * 86400)
        rows = sessions.list_sessions(limit=0)
        self.assertEqual([r['id'] for r in rows], [CLAUDE_A, CODEX_A, CLAUDE_B])  # newest first; 3-day-old one is out
        self.assertEqual([r['id'] for r in sessions.list_sessions(since_s=7 * 86400, limit=0)][-1], CODEX_B)
        self.assertEqual([r['id'] for r in sessions.list_sessions(since_s=sessions.parse_since('10m'), limit=0)],
                         [CLAUDE_A])
        self.assertEqual([r['engine'] for r in sessions.list_sessions('codex', limit=0)], ['codex'])
        self.assertEqual([r['id'] for r in sessions.list_sessions(cwd=other, limit=0)], [CLAUDE_B])
        self.assertEqual([r['id'] for r in sessions.list_sessions(cwd=self.tmp, limit=0)],
                         [CLAUDE_A, CODEX_A, CLAUDE_B])  # a parent dir covers its children
        self.assertEqual([r['id'] for r in sessions.list_sessions(limit=2)], [CLAUDE_A, CODEX_A])

    def test_parse_since(self):
        self.assertEqual(sessions.parse_since('90m'), 5400)
        self.assertEqual(sessions.parse_since('48h'), 172800)
        self.assertEqual(sessions.parse_since('2d'), 172800)
        self.assertEqual(sessions.parse_since('1w'), 604800)
        self.assertEqual(sessions.parse_since('3'), 10800)  # a bare number is hours
        for bad in ('', 'soon', '-1h', '5x'):
            with self.assertRaises(ValueError):
                sessions.parse_since(bad)


class TextTests(Fakes):
    def test_title_is_the_first_user_message_and_last_is_the_last_assistant_line(self):
        long_first = 'Please build a tool that lists every open session on both engines and forwards them ' * 3
        self.put_claude(CLAUDE_A, [
            self.c_user('<system-reminder>ignore me</system-reminder>\n' + long_first),
            self.c_asst('First answer.'), self.c_user('second question'),
            self.c_asst('Summary line\n\nNext: run the suite.\n```\n```')])
        self.put_codex(CODEX_A, [self.x_meta(CODEX_A), self.x_ev('task_started'), self.x_ctx('gpt-6-astra'),
                                 self.x_user('# AGENTS.md instructions for \x43:\\x' '\n<INSTRUCTIONS>\nrules\n</INSTRUCTIONS>'),
                                 self.x_user('<environment_context><cwd>\x43:\\x</cwd></environment_context>'),
                                 self.x_user('fix the flaky test'), self.x_user('and then the second thing'),
                                 self.x_ev('task_complete', last_agent_message='Fixed.\nNext step: merge it.')])
        rows = self.listed()
        a, x = rows[('claude', CLAUDE_A)], rows[('codex', CODEX_A)]
        self.assertTrue(a['title'].startswith('Please build a tool'))
        self.assertNotIn('ignore me', a['title'])
        self.assertLessEqual(len(a['title']), sessions.TITLE_CHARS)
        self.assertEqual(a['last'], 'Next: run the suite.')  # a closing code fence is not a line
        self.assertEqual(a['model'], 'claude-opus-5-5')
        self.assertEqual((x['title'], x['last'], x['model']), ('fix the flaky test', 'Next step: merge it.', 'gpt-6-astra'))
        self.assertLessEqual(len(self.listed()[('claude', CLAUDE_A)]['last']), sessions.LAST_CHARS)

    def test_last_line_is_clipped_to_90_and_title_without_a_user_message_is_labelled(self):
        self.codex_done(CODEX_A, answer='word ' * 60)
        self.put_codex(CODEX_B, [self.x_meta(CODEX_B), self.x_ev('task_started'),
                                 self.x_ev('task_complete', last_agent_message='<heartbeat>\n'
                                           '<automation_id>nightly-check</automation_id>\n'
                                           '<message>Monitor could not reach the DB.</message>\n</heartbeat>')])
        rows = self.listed()
        self.assertEqual(len(rows[('codex', CODEX_A)]['last']), sessions.LAST_CHARS)
        self.assertEqual(rows[('codex', CODEX_B)]['title'], 'automation: nightly-check')
        self.assertEqual(rows[('codex', CODEX_B)]['last'], 'Monitor could not reach the DB.')

    def test_title_is_found_past_a_huge_instruction_wrapper(self):
        """A rollout whose tail read cannot see the first turn: the title comes from a bounded head read."""
        wrapper = self.x_user('# AGENTS.md instructions for \x43:\\x' '\n<INSTRUCTIONS>\n' + 'rule\n' * 80_000 + '</INSTRUCTIONS>')
        filler = [self.x_ev('agent_message', message=f'step {i} ' + 'z' * 800) for i in range(400)]
        path = self.put_codex(CODEX_A, [self.x_meta(CODEX_A), self.x_ev('task_started'), self.x_ctx('gpt-6-luna'),
                                        wrapper, self.x_user('the real first request'), *filler,
                                        self.x_ev('task_complete', last_agent_message='Finished.')])
        self.assertGreater(os.path.getsize(path), 2 * sessions.TAIL_BYTES)
        row = self.listed()[('codex', CODEX_A)]
        self.assertEqual((row['title'], row['state'], row['last'], row['model']),
                         ('the real first request', 'waiting-on-user', 'Finished.', 'gpt-6-luna'))

    def test_claude_title_is_found_past_a_huge_tail(self):
        lines = [self.c_user('the first request'), self.c_asst('start')]
        lines += [self.c_asst('x' * 900, stop='tool_use') for _ in range(400)]
        lines += [self.c_asst('All finished.')]
        path = self.put_claude(CLAUDE_A, lines)
        self.assertGreater(os.path.getsize(path), 2 * sessions.TAIL_BYTES)
        row = self.listed()[('claude', CLAUDE_A)]
        self.assertEqual((row['title'], row['state'], row['last']), ('the first request', 'waiting-on-user', 'All finished.'))

    def test_secrets_are_redacted_in_every_output(self):
        straddle = 'a' * 50 + ' ' + SECRET  # the 70-char title cut would leave 18 key characters, too few for the key pattern
        self.put_claude(CLAUDE_A, [self.c_user(straddle), self.c_asst('Done.\nuse token=hunter2hunter2 and ' + SECRET)])
        self.codex_done(CODEX_A, user='my password: swordfish ' + SECRET, answer='key sk-ant-' + 'Q' * 30)
        rows = sessions.list_sessions(limit=0)
        blob = json.dumps(rows) + sessions.format_text(rows) + sessions.format_md(rows)
        for needle in (SECRET, 'sk-ant-api03', 'hunter2', 'swordfish', 'sk-ant-QQQ'):
            self.assertNotIn(needle, blob)
        self.assertIn('[REDACTED]', blob)
        rc, out, err = self.run_sessions(['--json'])
        self.assertEqual(rc, 0)
        self.assertNotIn('hunter2', out + err)

    def test_pipes_and_newlines_cannot_break_the_markdown_table(self):
        self.put_claude(CLAUDE_A, [self.c_user('a | b\nc'), self.c_asst('x | y')])
        md = sessions.format_md(sessions.list_sessions(limit=0))
        row = [ln for ln in md.splitlines() if CLAUDE_A in ln][0]
        self.assertEqual(row.count('|'), 9)


class CliTests(Fakes):
    def test_json_text_and_md_output(self):
        self.put_claude(CLAUDE_A, [self.c_user('review the plan'), self.c_asst('Plan reviewed.')], age_s=300)
        self.codex_done(CODEX_A, age_s=900)
        rc, out, _ = self.run_sessions(['--json'])
        rows = json.loads(out)
        self.assertEqual((rc, [r['engine'] for r in rows]), (0, ['claude', 'codex']))
        for key in ('id', 'cwd', 'age', 'age_s', 'model', 'title', 'last', 'state', 'modified', 'path'):
            self.assertIn(key, rows[0])
        rc, out, _ = self.run_sessions([])
        self.assertIn('waiting-on-user', out)
        self.assertIn('title: review the plan', out)
        rc, out, _ = self.run_sessions(['--engine', 'codex', '--md'])
        self.assertIn('| codex |', out)
        self.assertNotIn(CLAUDE_A, out)

    def test_md_forward_block_names_the_other_engine_per_waiting_session(self):
        self.put_claude(CLAUDE_A, [self.c_user('q'), self.c_asst('Waiting on you.')])
        self.put_claude(CLAUDE_B, [self.c_user('q', sid=CLAUDE_B), self.c_asst('', sid=CLAUDE_B, tool=True, stop='tool_use')])
        self.codex_done(CODEX_A)
        _, out, _ = self.run_sessions(['--md'])
        self.assertIn('### Forward', out)
        fwd = [ln for ln in out.splitlines() if ln.startswith('twinlane handoff')]
        self.assertEqual(len(fwd), 2)
        to_codex = [ln for ln in fwd if '--to codex' in ln][0]
        to_claude = [ln for ln in fwd if '--to claude' in ln][0]
        self.assertIn(f'--session {CLAUDE_A}', to_codex)
        self.assertNotIn(CLAUDE_B, out.split('### Forward')[1])  # mid-turn: not offered for forwarding
        self.assertNotIn(CODEX_A, to_codex)
        self.assertIn(f'--session {CODEX_A}', to_claude)
        for ln in fwd:
            self.assertIn('--out-dir', ln)

    def test_md_forward_block_when_nothing_waits(self):
        self.put_claude(CLAUDE_A, [self.c_user('q'), self.c_asst('', tool=True, stop='tool_use')])
        _, out, _ = self.run_sessions(['--md'])
        self.assertIn('nothing to forward', out)
        self.assertEqual(self.run_sessions(['--md', '--cwd', os.path.join(self.tmp, 'none')])[1].strip(),
                         'No recent interactive sessions.')

    def test_forward_commands_name_every_listed_session(self):
        rows = [{'engine': 'claude', 'id': f'id{i}', 'state': 'waiting-on-user'} for i in range(25)]
        cmds = sessions.forward_commands(rows, 'D')
        self.assertEqual([c.count('--session') for c in cmds], [sessions.FORWARD_MAX, sessions.FORWARD_MAX, 5])
        self.assertEqual({i for c in cmds for i in c.split() if i.startswith('id')}, {r['id'] for r in rows})

    def test_bad_arguments(self):
        rc, _, err = self.run_sessions(['--since', 'soon'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('--since', err)
        self.assertEqual(self.run_sessions(['--engine', 'gemini'])[0], common.EXIT_USAGE)
        self.assertEqual(self.run_sessions(['--json', '--md'])[0], common.EXIT_USAGE)

    def test_twinlane_py_routes_sessions(self):
        import twinlane
        self.put_claude(CLAUDE_A, [self.c_user('via cli.py'), self.c_asst('ok')])
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = twinlane.main(['sessions', '--json'])
        self.assertEqual((rc, json.loads(out.getvalue())[0]['id']), (0, CLAUDE_A))


class BatchHandoffTests(Fakes):
    def setUp(self):
        super().setUp()
        self.out_dir = os.path.join(self.tmp, 'forward')
        self.put_claude(CLAUDE_A, [self.c_user('Continue the sessions work please'), self.c_asst('Ready for review.')])
        self.codex_done(CODEX_A, user='Port the thing to codex', answer='Ported. Next: review.')

    def handoff(self, argv):
        return self.run_cli('handoff', argv)

    def test_one_file_per_session_for_both_engines_with_the_continue_line(self):
        rc, out, err = self.handoff(['--session', CLAUDE_A, '--session', CODEX_A, '--out-dir', self.out_dir])
        self.assertEqual((rc, err), (0, ''))
        lines = out.strip().splitlines()
        self.assertEqual(len(lines), 2)
        files = {}
        for ln in lines:
            head, path = ln.split(' -> Continue from ')
            engine, sid, title = head.split(' ', 2)
            files[sid] = (engine, title, path)
        self.assertEqual(files[CLAUDE_A][:2], ('claude', 'Continue the sessions work please'))
        self.assertEqual(files[CODEX_A][:2], ('codex', 'Port the thing to codex'))
        for sid, (engine, _, path) in files.items():
            self.assertEqual(os.path.dirname(path), os.path.abspath(self.out_dir))
            self.assertIn(sid, os.path.basename(path))
            with open(path, encoding='utf-8') as h:
                text = h.read()
            self.assertIn('# Handoff:', text)
            self.assertIn('Approvals do not transfer', text)
            self.assertIn('for codex' if engine == 'claude' else 'for claude', text)  # default: the other engine
        with open(files[CLAUDE_A][2], encoding='utf-8') as h:
            self.assertIn('Ready for review.', h.read())
        with open(files[CODEX_A][2], encoding='utf-8') as h:
            self.assertIn('Ported. Next: review.', h.read())

    def test_explicit_to_applies_to_every_session(self):
        rc, out, _ = self.handoff(['--session', CLAUDE_A, '--session', CODEX_A, '--to', 'claude',
                                   '--out-dir', self.out_dir])
        self.assertEqual(rc, 0)
        for ln in out.strip().splitlines():
            with open(ln.split(' -> Continue from ')[1], encoding='utf-8') as h:
                self.assertIn('for claude', h.read())

    def test_unknown_id_fails_that_session_only(self):
        rc, out, err = self.handoff(['--session', 'no-such-session', '--session', CODEX_A, '--out-dir', self.out_dir])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('no-such-session', err)
        self.assertEqual(len(out.strip().splitlines()), 1)
        self.assertIn(CODEX_A, out)

    def test_a_removed_cwd_fails_that_session_only(self):
        gone = baton.norm_dir(os.path.join(self.tmp, 'gone'))
        os.makedirs(gone)
        self.put_claude(CLAUDE_B, [self.c_user('lost worktree', cwd=gone, sid=CLAUDE_B),
                                   self.c_asst('ok', cwd=gone, sid=CLAUDE_B)], cwd=gone)
        os.rmdir(gone)
        rc, out, err = self.handoff(['--session', CLAUDE_B, '--session', CLAUDE_A, '--out-dir', self.out_dir])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn(CLAUDE_B, err)
        self.assertEqual([ln.split(' ')[1] for ln in out.strip().splitlines()], [CLAUDE_A])

    def test_json_output(self):
        rc, out, _ = self.handoff(['--session', CLAUDE_A, '--session', CODEX_A, '--out-dir', self.out_dir, '--json'])
        rows = json.loads(out)
        self.assertEqual((rc, [r['engine'] for r in rows]), (0, ['claude', 'codex']))
        self.assertTrue(all(os.path.isfile(r['path']) for r in rows))

    def test_batch_titles_never_carry_secrets(self):
        self.put_claude(CLAUDE_B, [self.c_user('use ' + SECRET + ' now', sid=CLAUDE_B), self.c_asst('ok', sid=CLAUDE_B)])
        rc, out, _ = self.handoff(['--session', CLAUDE_B, '--out-dir', self.out_dir])
        self.assertEqual(rc, 0)
        self.assertNotIn(SECRET, out)
        self.assertNotIn('sk-ant', out)

    def test_bad_combinations(self):
        for argv in (['--session', CLAUDE_A, '--session', CODEX_A],                 # several ids, no directory
                     ['--out-dir', self.out_dir],                                    # directory, no id
                     ['--session', CLAUDE_A, '--out-dir', self.out_dir, '--out', 'x.md'],
                     ['--session', CLAUDE_A, '--out-dir', self.out_dir, '--cd', self.repo]):
            rc, out, err = self.handoff(argv)
            self.assertEqual((rc, out), (common.EXIT_USAGE, ''), argv)
            self.assertTrue(err, argv)
        self.assertFalse(os.path.exists(self.out_dir))

    def test_single_session_forms_still_work(self):
        rc, out, _ = self.handoff(['--session', CLAUDE_A])
        self.assertEqual(rc, 0)
        self.assertIn('# Handoff:', out)
        target = os.path.join(self.tmp, 'one.md')
        rc, out, _ = self.handoff(['--session', CODEX_A, '--to', 'claude', '--out', target])
        self.assertEqual(rc, 0)
        self.assertIn('handoff brief:', out)
        self.assertTrue(os.path.isfile(target))

    def test_duplicate_ids_are_written_once(self):
        rc, out, _ = self.handoff(['--session', CLAUDE_A, '--session', CLAUDE_A, '--out-dir', self.out_dir])
        self.assertEqual((rc, len(out.strip().splitlines())), (0, 1))


GH = 'ghp_' + 'Z' * 30
JWT = 'eyJ' + 'a' * 12 + '.' + 'b' * 12 + '.' + 'c' * 12


class RedactionTests(Fakes):
    """every printable field of a row, in every format, goes through baton.redact."""

    def setUp(self):
        super().setUp()
        self.root = os.path.join(self.tmp, 'codex-' + GH)  # a token inside the rollout's path
        baton.CODEX_SESSIONS = self.root
        self.tokcwd = '\x43:\\work\\' + SECRET + '\\repo'
        self.put_codex(CODEX_A, [self.x_meta(CODEX_A, cwd=self.tokcwd), self.x_ctx('gpt-' + GH),
                                 self.x_ev('task_started'), self.x_user('use ' + JWT + ' to log in'),
                                 self.x_ev('task_complete', last_agent_message='key is ' + GH)])
        self.put_codex('ghp_' + 'Q' * 30, [self.x_meta('ghp_' + 'Q' * 30), self.x_user('q'),
                                           self.x_ev('task_complete', last_agent_message='ok')])

    def test_no_format_prints_a_token(self):
        needles = (GH, 'ghp_ZZZ', JWT, SECRET, 'sk-ant-api', 'ghp_QQQ')
        rows = sessions.list_sessions(limit=0)
        self.assertEqual(len(rows), 2)
        outputs = [json.dumps(rows), sessions.format_text(rows), sessions.format_md(rows)]
        for argv in (['--json'], [], ['--md']):
            rc, out, err = self.run_sessions(argv)
            self.assertEqual(rc, 0)
            outputs.append(out + err)
        for blob in outputs:
            for needle in needles:
                self.assertNotIn(needle, blob)
        row = [r for r in rows if r['cwd']][0]
        for key in ('id', 'cwd', 'path', 'title', 'last', 'model'):
            self.assertNotIn('ghp_', row[key] or '', key)
        self.assertIn('[REDACTED', json.dumps(rows))

    def test_batch_handoff_lines_json_and_errors_are_redacted(self):
        self.put_claude(CLAUDE_A, [self.c_user('log in with ' + JWT), self.c_asst('Done.')])
        out_dir = os.path.join(self.tmp, 'out-' + GH)
        rc, out, err = self.run_cli('handoff', ['--session', CLAUDE_A, '--session', 'nope-' + GH, '--out-dir', out_dir])
        self.assertEqual(rc, common.EXIT_USAGE)
        for needle in (GH, JWT):
            self.assertNotIn(needle, out + err)
        self.assertIn('Continue from', out)
        rc, out, err = self.run_cli('handoff', ['--session', CLAUDE_A, '--out-dir', out_dir, '--json'])
        self.assertEqual(rc, 0)
        self.assertNotIn(GH, out)
        self.assertNotIn(JWT, out)

    def test_single_session_handoff_json_and_text_redact_the_out_path(self):
        self.put_claude(CLAUDE_A, [self.c_user('hello'), self.c_asst('Done.')])
        out = os.path.join(self.tmp, 'brief-' + GH + '.md')
        for extra in (['--json'], []):
            rc, stdout, err = self.run_cli('handoff', ['--session', CLAUDE_A, '--out', out] + extra)
            self.assertEqual(rc, 0, err)
            self.assertNotIn(GH, stdout + err)


class SessionHandoffIsolationTests(Fakes):
    """a session handoff renders one session and touches no baton."""

    def setUp(self):
        super().setUp()
        self.out_dir = os.path.join(self.tmp, 'forward')
        self.put_claude(CLAUDE_A, [self.c_user('claude thread alpha'), self.c_asst('CLAUDE-ANSWER-ALPHA.')])
        self.put_claude(CLAUDE_B, [self.c_user('claude thread beta', sid=CLAUDE_B),
                                   self.c_asst('CLAUDE-ANSWER-BETA.', sid=CLAUDE_B)], age_s=2 * OLD)
        self.codex_done(CODEX_A, user='codex thread alpha', answer='CODEX-ANSWER-ALPHA.')
        self.codex_done(CODEX_B, user='codex thread beta', answer='CODEX-ANSWER-BETA.', age_s=2 * OLD)
        baton.new('task-x', 'a task baton that must not change', cwd=self.repo)
        self.task_before = self.baton_files()

    def baton_files(self):
        out = {}
        for name in sorted(os.listdir(common.BATONS)):
            with open(os.path.join(common.BATONS, name), 'rb') as h:
                out[name] = h.read()
        return out

    def read(self, path):
        with open(path, encoding='utf-8') as h:
            return h.read()

    def test_single_session_handoff_offers_the_interactive_lead_first_and_honours_kind(self):
        out = os.path.join(self.tmp, 'one.md')
        rc, stdout, err = self.run_cli('handoff', ['--session', CLAUDE_A, '--to', 'codex', '--out', out])
        self.assertEqual(rc, 0, err)
        lines = stdout.splitlines()
        i = next(n for n, ln in enumerate(lines) if ln.startswith('Interactive (lead): open codex'))
        run = next(n for n, ln in enumerate(lines) if ln.startswith('twinlane run '))
        self.assertLess(i, run)
        self.assertIn('--kind intel', lines[run])
        self.assertNotIn('--baton', lines[run])
        self.assertEqual(self.baton_files(), self.task_before)
        rc, _, err = self.run_cli('handoff', ['--session', CLAUDE_A, '--to', 'codex', '--out', out,
                                              '--kind', 'code'])
        self.assertEqual(rc, common.EXIT_USAGE)  # the fake repo is not a linked worktree
        self.assertIn('LINKED worktree', err)

    def test_batch_renders_only_its_own_thread_and_writes_no_baton(self):
        ids = [CLAUDE_A, CLAUDE_B, CODEX_A, CODEX_B]
        args = [x for i in ids for x in ('--session', i)]
        rc, out, err = self.run_cli('handoff', args + ['--out-dir', self.out_dir])
        self.assertEqual((rc, err), (0, ''))
        self.assertEqual(self.baton_files(), self.task_before)  # no new baton, no .handoff.md, task baton byte-equal
        mine = {CLAUDE_A: 'CLAUDE-ANSWER-ALPHA', CLAUDE_B: 'CLAUDE-ANSWER-BETA',
                CODEX_A: 'CODEX-ANSWER-ALPHA', CODEX_B: 'CODEX-ANSWER-BETA'}
        for sid, marker in mine.items():
            engine = 'claude' if sid.startswith('1111') else 'codex'
            text = self.read(os.path.join(self.out_dir, f'handoff-{engine}-{sid}.md'))
            self.assertIn(marker, text)
            for other in set(mine.values()) - {marker}:
                self.assertNotIn(other, text, f'{sid} carries {other}')
            self.assertIn('not linked', text)
            self.assertNotIn('a task baton that must not change', text)
            self.assertNotIn('task-x', text)

    def test_with_session_links_one_other_engine_session_explicitly(self):
        target = os.path.join(self.tmp, 'one.md')
        rc, out, err = self.run_cli('handoff', ['--session', CLAUDE_A, '--with-session', CODEX_B, '--out', target])
        self.assertEqual((rc, err), (0, ''))
        text = self.read(target)
        self.assertIn('CLAUDE-ANSWER-ALPHA', text)
        self.assertIn('CODEX-ANSWER-BETA', text)       # the linked one
        self.assertNotIn('CODEX-ANSWER-ALPHA', text)   # not the cwd's newest
        self.assertNotIn('not linked', text)
        self.assertIn('no baton written', out)
        self.assertEqual(self.baton_files(), self.task_before)

    def test_with_session_errors(self):
        for argv in (['--session', CLAUDE_A, '--with-session', CLAUDE_B],     # same engine
                     ['--session', CLAUDE_A, '--with-session', 'no-such-id'],
                     ['--with-session', CODEX_A, '--cd', self.repo],           # no --session
                     ['--session', CLAUDE_A, '--with-session', CODEX_A, '--out-dir', self.out_dir]):
            rc, out, err = self.run_cli('handoff', argv)
            self.assertEqual((rc, out), (common.EXIT_USAGE, ''), argv)
            self.assertTrue(err, argv)
        self.assertEqual(self.baton_files(), self.task_before)

    def test_unknown_session_does_not_fall_back_to_the_newest_in_the_cwd(self):
        rc, out, err = self.run_cli('handoff', ['--session', 'ffffffff-0000-4000-8000-000000000000', '--cd', self.repo])
        self.assertEqual((rc, out), (common.EXIT_USAGE, ''))
        self.assertIn('no Claude session or Codex rollout', err)

    def test_session_with_an_explicit_slug_keeps_that_baton_but_not_the_cwd_thread(self):
        target = os.path.join(self.tmp, 'slug.md')
        rc, out, err = self.run_cli('handoff', ['task-x', '--session', CLAUDE_B, '--out', target])
        self.assertEqual((rc, err), (0, ''))
        text = self.read(target)
        self.assertIn('a task baton that must not change', text)  # the explicit task baton is the task
        self.assertIn('CLAUDE-ANSWER-BETA', text)
        self.assertNotIn('CODEX-ANSWER', text)
        self.assertIn('not linked', text)

    def test_session_run_command_has_no_baton_flag(self):
        target = os.path.join(self.tmp, 'cmd.md')
        rc, out, _ = self.run_cli('handoff', ['--session', CODEX_A, '--to', 'claude', '--out', target])
        run = [ln for ln in out.splitlines() if ln.startswith('twinlane run')]
        self.assertEqual(len(run), 1)
        self.assertNotIn('--baton', run[0])
        self.assertIn('--name handoff-session-codex-', run[0])

    def test_single_session_without_out_writes_nothing(self):
        rc, out, _ = self.run_cli('handoff', ['--session', CODEX_A])
        self.assertEqual(rc, 0)
        self.assertIn('CODEX-ANSWER-ALPHA', out)
        self.assertEqual(self.baton_files(), self.task_before)


class NameTests(Fakes):
    """the titles the apps show beat the first user message."""

    def write_index(self, rows):
        with open(sessions.codex_index_path(), 'w', encoding='utf-8', newline='\n') as h:
            h.write('\n'.join(jl(r) if isinstance(r, dict) else r for r in rows) + '\n')

    def test_codex_index_name_is_the_title_and_newest_updated_at_wins(self):
        self.codex_done(CODEX_A, user='first user message A')
        self.codex_done(CODEX_B, user='first user message B')
        self.write_index([
            {'id': CODEX_A, 'thread_name': 'Old name', 'updated_at': '2026-10-08T10:00:00.1234567Z'},
            {'id': CODEX_A, 'thread_name': 'Newest name', 'updated_at': '2026-10-08T12:00:00Z'},
            {'id': CODEX_A, 'thread_name': 'Stale later line', 'updated_at': '2026-10-08T11:00:00Z'},
            'not json at all', jl({'id': CODEX_B, 'thread_name': '   '}), jl({'thread_name': 'no id'})])
        rows = self.listed()
        self.assertEqual(rows[('codex', CODEX_A)]['title'], 'Newest name')
        self.assertEqual(rows[('codex', CODEX_B)]['title'], 'first user message B')  # blank name: fall back
        self.assertEqual(sessions.codex_thread_names(), {CODEX_A: 'Newest name'})

    def test_equal_stamps_go_to_the_later_line_and_names_are_redacted_and_clipped(self):
        self.codex_done(CODEX_A)
        self.write_index([{'id': CODEX_A, 'thread_name': 'first', 'updated_at': '2026-10-08T10:00:00Z'},
                          {'id': CODEX_A, 'thread_name': 'second ' + GH + ' ' + 'w' * 100,
                           'updated_at': '2026-10-08T10:00:00Z'}])
        title = self.listed()[('codex', CODEX_A)]['title']
        self.assertTrue(title.startswith('second'))
        self.assertNotIn('ghp_', title)
        self.assertLessEqual(len(title), sessions.TITLE_CHARS)

    def test_index_is_read_once_per_run_and_only_its_tail(self):
        self.codex_done(CODEX_A, user='fallback title')
        self.write_index([{'id': CODEX_A, 'thread_name': 'in the head', 'updated_at': '2026-10-08T10:00:00Z'}]
                         + [{'id': f'x{i}', 'thread_name': 'filler ' * 20, 'updated_at': '2026-10-08T10:00:00Z'}
                            for i in range(60)])
        saved, calls = sessions.INDEX_BYTES, []
        real = sessions.codex_thread_names
        sessions.codex_thread_names = lambda: calls.append(1) or real()
        try:
            self.codex_done(CODEX_B)
            self.assertEqual(len(self.listed()), 2)
            self.assertEqual(len(calls), 1)  # two sessions, one read
            sessions.INDEX_BYTES = 2000      # the head of the file is beyond the bound
            self.assertNotIn(CODEX_A, real())
            self.assertEqual(self.listed()[('codex', CODEX_A)]['title'], 'fallback title')
        finally:
            sessions.codex_thread_names, sessions.INDEX_BYTES = real, saved

    def test_missing_index_is_fine(self):
        self.codex_done(CODEX_A, user='no index here')
        self.assertFalse(os.path.exists(sessions.codex_index_path()))
        self.assertEqual(self.listed()[('codex', CODEX_A)]['title'], 'no index here')

    @staticmethod
    def c_title(sid, kind, value):
        key = 'customTitle' if kind == 'custom-title' else 'agentName'
        return jl({'type': kind, key: value, 'sessionId': sid})

    def test_claude_custom_title_then_agent_name_then_first_message(self):
        a, b, c = CLAUDE_A, CLAUDE_B, '11111111-aaaa-4bbb-8ccc-000000000003'
        self.put_claude(a, [self.c_user('typed message A'), self.c_title(a, 'custom-title', 'Old'),
                            self.c_asst('x'), self.c_title(a, 'custom-title', 'Renamed in the app'),
                            self.c_title(a, 'agent-name', 'Agent name')])
        self.put_claude(b, [self.c_user('typed message B', sid=b), self.c_title(b, 'agent-name', 'Only agent name'),
                            self.c_asst('x', sid=b)])
        self.put_claude(c, [self.c_user('typed message C', sid=c), self.c_asst('x', sid=c)])
        rows = self.listed()
        self.assertEqual(rows[('claude', a)]['title'], 'Renamed in the app')
        self.assertEqual(rows[('claude', b)]['title'], 'Only agent name')
        self.assertEqual(rows[('claude', c)]['title'], 'typed message C')

    def test_claude_title_found_in_the_head_of_a_huge_transcript(self):
        lines = [self.c_user('typed first'), self.c_title(CLAUDE_A, 'custom-title', 'Head title')]
        lines += [self.c_asst('x' * 900, stop='tool_use') for _ in range(400)] + [self.c_asst('Done.')]
        path = self.put_claude(CLAUDE_A, lines)
        self.assertGreater(os.path.getsize(path), 2 * sessions.TAIL_BYTES)
        self.assertEqual(self.listed()[('claude', CLAUDE_A)]['title'], 'Head title')

    def test_a_head_custom_title_beats_a_tail_agent_name(self):
        lines = [self.c_user('typed first'), self.c_title(CLAUDE_A, 'custom-title', 'Head custom')]
        lines += [self.c_asst('x' * 900, stop='tool_use') for _ in range(400)]
        lines += [self.c_title(CLAUDE_A, 'agent-name', 'Tail agent'), self.c_asst('Done.')]
        self.put_claude(CLAUDE_A, lines)
        self.assertEqual(self.listed()[('claude', CLAUDE_A)]['title'], 'Head custom')


class CutTests(Fakes):
    """a --limit cut is counted and said, and the Forward block never drops listed sessions."""

    def make(self, n_claude_waiting, n_codex_waiting, n_busy=0):
        for i in range(n_claude_waiting):
            sid = f'33333333-aaaa-4bbb-8ccc-{i:012d}'
            self.put_claude(sid, [self.c_user(f'claude q{i}', sid=sid), self.c_asst('Done.', sid=sid)],
                            age_s=OLD + i)
        for i in range(n_codex_waiting):
            self.codex_done(f'01a10000-0000-7000-8000-{i:012d}', user=f'codex q{i}', age_s=OLD + 1000 + i)
        for i in range(n_busy):
            sid = f'44444444-aaaa-4bbb-8ccc-{i:012d}'
            self.put_claude(sid, [self.c_user('busy', sid=sid)], age_s=OLD + 5000 + i)

    def test_default_list_says_how_many_rows_were_cut(self):
        self.make(8, 7)
        rc, out, err = self.run_sessions(['--limit', '10'])
        self.assertEqual(rc, 0)
        self.assertIn('+5 more rows not shown (limit 10); run: twinlane sessions --limit 0', out)
        self.assertIn('+5 more waiting-on-user (run: twinlane sessions --state waiting-on-user --limit 0 --md)', out)
        self.assertEqual(len([ln for ln in out.splitlines() if ln.startswith(('claude ', 'codex '))]), 10)
        self.assertNotIn('more rows not shown', self.run_sessions(['--limit', '0'])[1])
        self.assertNotIn('more rows not shown', self.run_sessions(['--limit', '15'])[1])

    def test_cut_counts_only_waiting_ones_as_waiting(self):
        self.make(3, 0, n_busy=4)  # newest are the 3 waiting; the 4 mid-turn ones are cut
        res = sessions.collect_sessions(limit=3)
        self.assertEqual((len(res['rows']), res['cut'], res['cut_waiting'], res['cut_exact']), (3, 4, 0, True))
        self.assertEqual(sessions.cut_notes(res, 3), ['+4 more rows not shown (limit 3); run: twinlane sessions --limit 0'])

    def test_md_forward_block_names_every_listed_session_and_the_cut_remainder(self):
        self.make(13, 0)
        rc, out, _ = self.run_sessions(['--md', '--limit', '12', '--engine', 'claude'])
        fwd = [ln for ln in out.splitlines() if ln.startswith('twinlane handoff')]
        self.assertEqual([ln.count('--session') for ln in fwd], [10, 2])  # all 12 listed, none dropped
        self.assertIn('+1 more waiting-on-user (run: twinlane sessions --engine claude --state waiting-on-user --limit 0 --md)',
                      out.split('### Forward')[1])
        self.assertIn('+1 more rows not shown', out.split('### Forward')[0])

    def test_state_filter(self):
        self.make(2, 1, n_busy=2)
        self.put_claude(CLAUDE_A, [self.c_user('now')], age_s=5)
        self.assertEqual({r['state'] for r in sessions.list_sessions(limit=0, states=['mid-turn'])}, {'mid-turn'})
        self.assertEqual(len(sessions.list_sessions(limit=0, states=['waiting-on-user'])), 3)
        self.assertEqual(len(sessions.list_sessions(limit=0, states=['waiting-on-user', 'active'])), 4)
        rc, out, _ = self.run_sessions(['--state', 'waiting-on-user,active', '--state', 'unknown', '--json'])
        self.assertEqual((rc, len(json.loads(out))), (0, 4))
        # the limit applies after the filter: 1 waiting row shown, 2 more waiting counted
        res = sessions.collect_sessions(limit=1, states=['waiting-on-user'])
        self.assertEqual((len(res['rows']), res['cut'], res['cut_waiting']), (1, 2, 2))

    def test_state_job_implies_jobs_and_a_bad_state_is_a_usage_error(self):
        self.put_claude(CLAUDE_B, [self.c_user('w', entry='sdk-cli', sid=CLAUDE_B),
                                   self.c_asst('w', sid=CLAUDE_B, entry='sdk-cli')])
        self.assertEqual([r['state'] for r in sessions.list_sessions(limit=0, states=['job'])], ['job'])
        rc, _, err = self.run_sessions(['--state', 'sleeping'])
        self.assertEqual(rc, common.EXIT_USAGE)
        self.assertIn('--state', err)

    def test_json_stays_a_list_and_the_cut_goes_to_stderr(self):
        self.make(5, 0)
        rc, out, err = self.run_sessions(['--json', '--limit', '2'])
        self.assertEqual((rc, len(json.loads(out))), (0, 2))
        self.assertIn('+3 more rows not shown', err)

    def test_an_exhausted_count_budget_gives_a_lower_bound(self):
        self.make(6, 0)
        saved = sessions.CUT_COUNT_BUDGET_S
        sessions.CUT_COUNT_BUDGET_S = -1  # no time to count
        try:
            res = sessions.collect_sessions(limit=2)
        finally:
            sessions.CUT_COUNT_BUDGET_S = saved
        self.assertEqual((len(res['rows']), res['cut'], res['cut_exact']), (2, 0, False))
        self.assertTrue(sessions.cut_notes(dict(res, cut=1), 2)[0].startswith('+1+ more rows not shown'))
        notes = sessions.cut_notes(res, 2)  # nothing counted before time ran out: still never silent
        self.assertEqual(len(notes), 1)
        self.assertIn('more rows may exist', notes[0])
        self.assertEqual(sessions.cut_notes({'cut': 0, 'cut_exact': True}, 2), [])


if __name__ == '__main__':
    unittest.main()
