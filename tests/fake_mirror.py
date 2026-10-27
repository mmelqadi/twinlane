"""Fixture only: a snapshot publisher. Records its argv in FAKE_CLOUD_DIR/mirror-N.json and reports success."""
import json
import os
import sys

state = os.environ['FAKE_CLOUD_DIR']
os.makedirs(state, exist_ok=True)
n = len([f for f in os.listdir(state) if f.startswith('mirror-')]) + 1
with open(os.path.join(state, f'mirror-{n}.json'), 'w', encoding='utf-8') as h:
    json.dump(sys.argv[1:], h)
if os.environ.get('FAKE_MIRROR_FAIL'):
    print('push failed')
    sys.exit(1)
print('push rc 0')
