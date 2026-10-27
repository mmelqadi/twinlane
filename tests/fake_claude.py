#!/usr/bin/env python3
"""Fake `claude -p ... --output-format stream-json` for twinlane tests. It NEVER calls a model.

Reads the brief on stdin and prints stream-json events: system/init, assistant, an optional
rejected rate_limit_event, then result. Exits 0.

Env knobs:
  FAKE_LIMIT=1            emit a rejected rate_limit_event and an is_error "hit your limit" result
  FAKE_SERVED_MODEL=M     model reported in result.modelUsage (default: the --model argument)
  FAKE_SESSION_ID=S       session id (default: --resume value, else a new uuid); a fork always gets a new id
  FAKE_SLEEP=SECONDS      sleep after init (timeout tests)
  FAKE_SLEEP_ONCE=PATH    with FAKE_SLEEP, sleep only while PATH does not exist (the first run creates it)
  FAKE_TOUCH=NAME         create NAME in the cwd and report it as a Write tool_use (tripwire tests)
  FAKE_TOUCH_NO_TOOL_USE=1  create FAKE_TOUCH without a tool_use (a peer's write, as far as the job can tell)
  FAKE_TOOL_USE=JSON      emit one assistant tool_use {"name": ..., "input": {...}}
  FAKE_BAD_EVENTS=1       emit malformed events first (content 5, rate_limit_info a string, broken JSON)
  FAKE_ARGV_OUT=PATH      write {argv, anthropic_env, env_names, job, brief_head, cwd} as JSON to PATH
"""
import json
import os
import sys
import time
import uuid


def emit(obj):
    sys.stdout.buffer.write((json.dumps(obj, ensure_ascii=False) + '\n').encode('utf-8'))
    sys.stdout.flush()


def arg(name):
    argv = sys.argv[1:]
    if name in argv and argv.index(name) + 1 < len(argv):
        return argv[argv.index(name) + 1]
    return None


def echo_line(brief):
    """The first 'ECHO:' line of the brief, so tests can prove UTF-8 survives the round trip."""
    for line in brief.splitlines():
        if line.startswith('ECHO:'):
            return line[5:].strip()
    return ''


def main():
    brief = sys.stdin.buffer.read().decode('utf-8', errors='replace')
    out = os.environ.get('FAKE_ARGV_OUT')
    if out:
        with open(out, 'w', encoding='utf-8') as h:
            json.dump({'argv': sys.argv[1:], 'anthropic_env': [k for k in os.environ if k.startswith('ANTHROPIC_')],
                       'env_names': sorted(os.environ), 'job': os.environ.get('TWINLANE_ENGINE_JOB'),
                       'brief_head': brief[:3000], 'cwd': os.getcwd(),
                       'pid': os.getpid(), 'pgid': os.getpgid(0) if os.name != 'nt' else None},
                      h, ensure_ascii=False)
    model = arg('--model') or 'claude-opus-5-5'
    served = os.environ.get('FAKE_SERVED_MODEL') or model
    sid = (str(uuid.uuid4()) if '--fork-session' in sys.argv[1:] else
           os.environ.get('FAKE_SESSION_ID') or arg('--resume') or str(uuid.uuid4()))
    emit({'type': 'system', 'subtype': 'init', 'session_id': sid, 'model': served,
          'permissionMode': arg('--permission-mode') or 'default', 'tools': ['Read', 'Glob', 'Grep'],
          'cwd': os.getcwd()})
    if os.environ.get('FAKE_BAD_EVENTS') == '1':
        emit({'type': 'assistant', 'session_id': sid, 'message': {'content': 5}})
        emit({'type': 'rate_limit_event', 'session_id': sid, 'rate_limit_info': 'rejected?'})
        sys.stdout.buffer.write(b'{not json\n')
        sys.stdout.flush()
    if os.environ.get('FAKE_TOUCH'):
        target = os.path.join(os.getcwd(), os.environ['FAKE_TOUCH'])
        if os.environ.get('FAKE_TOUCH_NO_TOOL_USE') != '1':
            emit({'type': 'assistant', 'session_id': sid, 'message': {'content': [
                {'type': 'tool_use', 'id': 'toolu_touch', 'name': 'Write', 'input': {'file_path': target}}]}})
        with open(target, 'w', encoding='utf-8') as h:
            h.write('touched by fake claude\n')
    if os.environ.get('FAKE_TOOL_USE'):
        use = json.loads(os.environ['FAKE_TOOL_USE'])
        emit({'type': 'assistant', 'session_id': sid, 'message': {'content': [
            {'type': 'tool_use', 'id': 'toolu_x', 'name': use['name'], 'input': use.get('input') or {}}]}})
    if os.environ.get('FAKE_SLEEP'):
        once = os.environ.get('FAKE_SLEEP_ONCE')  # sleep only on the first run: the marker file records it
        if not once or not os.path.exists(once):
            if once:
                open(once, 'w').close()
            time.sleep(float(os.environ['FAKE_SLEEP']))
    echo = echo_line(brief)
    emit({'type': 'assistant', 'session_id': sid,
          'message': {'model': served, 'content': [{'type': 'text', 'text': f'working on it {echo}'.strip()}]}})
    usage = {served: {'inputTokens': 10, 'outputTokens': 5}}
    if os.environ.get('FAKE_LIMIT') == '1':
        emit({'type': 'rate_limit_event', 'session_id': sid,
              'rate_limit_info': {'status': 'rejected', 'rateLimitType': 'five_hour', 'resetsAt': 1791460800}})
        emit({'type': 'result', 'subtype': 'success', 'is_error': True, 'num_turns': 2, 'duration_ms': 1200,
              'duration_api_ms': 900, 'total_cost_usd': 0.01, 'session_id': sid, 'modelUsage': usage,
              'permission_denials': [], 'result': "You've hit your limit · resets 3pm (UTC)"})
        return 0
    emit({'type': 'result', 'subtype': 'success', 'is_error': False, 'num_turns': 3, 'duration_ms': 2100,
          'duration_api_ms': 1500, 'total_cost_usd': 0.02, 'session_id': sid, 'modelUsage': usage,
          'usage': {'input_tokens': 120, 'output_tokens': 30, 'cache_read_input_tokens': 900,
                    'cache_creation_input_tokens': 50},
          'permission_denials': [{'tool_name': 'Bash'}],
          'result': f'Report: fake claude finished. {echo}\nfiles changed: none\nnext step: none'.strip()})
    return 0


if __name__ == '__main__':
    sys.exit(main())
