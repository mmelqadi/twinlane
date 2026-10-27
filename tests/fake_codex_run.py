#!/usr/bin/env python3
"""Optional adapter fixture for twinlane tests. It NEVER calls a model.

Takes the optional adapter's flags (--brief --cd --tier --out --timeout [--model] [--effort] [--write]
[--allow-credits]) and writes <out>/events.jsonl, <out>/last-message.md, <out>/result.txt.

Env knobs:
  FAKE_CODEX_LIMIT=1     write a usage_limit_exceeded event, no last message, exit 1
  FAKE_CODEX_EXIT=3      exit 3 before running (the credits refusal of the real script)
  FAKE_CODEX_ARGV_OUT=P  write {argv, brief_head, strict_credits, codex_bin, leases on its copy} as JSON to P
  FAKE_CODEX_NOISY=1     succeed, but tool output and stderr mention rate_limit_reached/usage_limit_exceeded
"""
import json
import os
import sys
import uuid

TIER_MODEL = {'luna': 'gpt-6-luna', 'sol': 'gpt-6-sol', 'sol-high': 'gpt-6-sol', 'astra': 'gpt-6-astra'}


def parse(argv):
    opts, i = {'write': False, 'allow_credits': False}, 0
    while i < len(argv):
        a = argv[i]
        if a == '--write':
            opts['write'] = True
            i += 1
        elif a == '--allow-credits':
            opts['allow_credits'] = True
            i += 1
        else:
            opts[a.lstrip('-').replace('-', '_')] = argv[i + 1]
            i += 2
    return opts


def main():
    opts = parse(sys.argv[1:])
    with open(opts['brief'], encoding='utf-8') as h:
        brief = h.read()
    if os.environ.get('FAKE_CODEX_ARGV_OUT'):
        leases = os.path.join(os.path.dirname(os.environ.get('TWINLANE_CODEX_BIN') or '.'), 'leases')
        with open(os.environ['FAKE_CODEX_ARGV_OUT'], 'w', encoding='utf-8') as h:
            json.dump({'argv': sys.argv[1:], 'brief_head': brief[:4000],
                       'strict_credits': os.environ.get('TWINLANE_CODEX_ADAPTER_STRICT_CREDITS'),
                       'codex_bin': os.environ.get('TWINLANE_CODEX_BIN'), 'env_names': sorted(os.environ),
                       'leases': sorted(os.listdir(leases)) if os.path.isdir(leases) else []}, h, ensure_ascii=False)
    if os.environ.get('FAKE_CODEX_EXIT') == '3':
        print('REFUSED: Codex is past its weekly limit', file=sys.stderr)
        return 3
    out = opts['out']
    os.makedirs(out, exist_ok=True)
    sid = str(uuid.uuid4())
    model = opts.get('model') or TIER_MODEL.get(opts.get('tier'), 'gpt-6-luna')
    limit = os.environ.get('FAKE_CODEX_LIMIT') == '1'
    with open(os.path.join(out, 'events.jsonl'), 'w', encoding='utf-8') as h:
        h.write(json.dumps({'type': 'thread.started', 'thread_id': sid}) + '\n')
        if limit:
            h.write(json.dumps({'type': 'error', 'message': "usage_limit_exceeded: You've hit your usage limit"}) + '\n')
        else:
            if os.environ.get('FAKE_CODEX_NOISY') == '1':
                h.write(json.dumps({'type': 'item.completed', 'item': {
                    'type': 'command_execution', 'command': 'rg rate_limit_reached',
                    'aggregated_output': 'src/x.py: usage_limit_exceeded rate_limit_reached'}}) + '\n')
            if os.environ.get('FAKE_CODEX_USAGE') == '1':
                for n in (1, 2):  # two turns: the receipt must sum them
                    h.write(json.dumps({'type': 'turn.completed', 'usage': {
                        'input_tokens': 100 * n, 'cached_input_tokens': 10 * n, 'output_tokens': 5 * n}}) + '\n')
            else:
                h.write(json.dumps({'type': 'turn.completed'}) + '\n')
    rc = 1 if limit else 0
    root = os.environ.get('FAKE_CODEX_ROLLOUT_ROOT')
    if root:  # what the real Codex writes: sessions/YYYY/MM/DD/rollout-<ts>-<session id>.jsonl
        day = os.path.join(root, '2026', '10', '09')
        os.makedirs(day, exist_ok=True)
        with open(os.path.join(day, f'rollout-2026-10-09T10-00-00-{sid}.jsonl'), 'w', encoding='utf-8') as h:
            for total in (1000, 2500):
                h.write(json.dumps({'type': 'event_msg', 'payload': {'type': 'token_count', 'info': {
                    'total_token_usage': {'input_tokens': total, 'cached_input_tokens': total // 2,
                                          'output_tokens': 40, 'reasoning_output_tokens': 7,
                                          'total_tokens': total + 40}}}}) + '\n')
    if not limit:
        with open(os.path.join(out, 'last-message.md'), 'w', encoding='utf-8') as h:
            h.write(os.environ.get('FAKE_CODEX_FINAL') or 'Report: fake codex finished. تم\nfiles changed: none\n')
    sandbox = 'workspace-write' if opts['write'] else 'read-only'
    with open(os.path.join(out, 'result.txt'), 'w', encoding='utf-8') as h:
        h.write(f'exit={rc} seconds=1 model={model} effort=high sandbox={sandbox} session={sid} tokens=123 cli=fake\n')
    with open(os.path.join(out, 'stderr.log'), 'w', encoding='utf-8') as h:
        h.write(f'session id: {sid}\n')
        if os.environ.get('FAKE_CODEX_NOISY') == '1':
            h.write('exec rg usage_limit_exceeded -> 3 matches\n')
    print(f'evidence: {out}')
    return rc


if __name__ == '__main__':
    sys.exit(main())
