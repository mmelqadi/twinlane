"""Fixed Windows toast and its windowless timeout supervisor (stdlib only)."""
import base64
import json
import os
import re
import subprocess
import sys
import unicodedata

TIMEOUT_S = 5
NO_WINDOW = 0x08000000
JOB_ID = re.compile(r'[0-9]{8}-[0-9]{6}-(?:claude|codex)-[0-9a-f]{4}')
STATUSES = {'done', 'failed', 'timeout', 'limit_hit', 'refused', 'stopped', 'lost', 'interrupted'}

# Input is JSON data on stdin. No job data is ever interpolated into PowerShell code,
# XML markup, a script path, executable name or an invocation expression.
SCRIPT = r'''
$ErrorActionPreference = 'Stop'
try {
    $data = [Console]::In.ReadToEnd() | ConvertFrom-Json
    [Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType = WindowsRuntime] > $null
    [Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime] > $null
    $xml = New-Object Windows.Data.Xml.Dom.XmlDocument
    $xml.LoadXml('<toast><visual><binding template="ToastGeneric"><text/><text/><text/></binding></visual></toast>')
    $nodes = $xml.GetElementsByTagName('text')
    $nodes.Item(0).InnerText = [string]$data.id
    $nodes.Item(1).InnerText = [string]$data.name
    $nodes.Item(2).InnerText = [string]$data.status
    $toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
    [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('twinlane').Show($toast)
} catch { exit 1 }
'''


def payload(jid, name, status):
    if not isinstance(jid, str) or not JOB_ID.fullmatch(jid) or status not in STATUSES:
        raise ValueError('invalid toast identity/status')
    if name is None:
        name = ''
    if not isinstance(name, str):
        raise ValueError('invalid toast name')
    # Bound OS input and normalize control characters to display text. Quotes, dollars,
    # backticks and markup remain inert data, including when supplied in a job name.
    name = ''.join(' ' if unicodedata.category(c) in ('Cc', 'Zl', 'Zp') else c for c in name[:200])
    return json.dumps(dict(id=jid, name=name, status=status), ensure_ascii=True).encode('ascii')


def run(data, log_path):
    """Only this disposable helper waits; the receipt publisher never waits for PowerShell."""
    message = None
    try:
        row = json.loads(data)
        data = payload(row['id'], row['name'], row['status'])
        if os.path.basename(log_path) != 'runner.log' or os.path.basename(os.path.dirname(log_path)) != row['id']:
            return
        system_root = os.environ.get('SystemRoot', r'C:\Windows')
        powershell = os.path.join(system_root, 'System32', 'WindowsPowerShell', 'v1.0', 'powershell.exe')
        result = subprocess.run([powershell, '-NoLogo', '-NoProfile', '-NonInteractive',
                                 '-WindowStyle', 'Hidden', '-EncodedCommand',
                                 base64.b64encode(SCRIPT.encode('utf-16-le')).decode('ascii')],
                                input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=TIMEOUT_S, creationflags=NO_WINDOW)
        if result.returncode:
            message = 'toast failed; skipped'
    except subprocess.TimeoutExpired:
        message = 'toast timed out; skipped'
    except Exception:
        message = 'toast unavailable; skipped'
    if message:
        try:
            with open(log_path, 'a', encoding='utf-8') as handle:
                handle.write('completion sink: ' + message + '\n')
        except OSError:
            pass


if __name__ == '__main__' and os.name == 'nt' and len(sys.argv) == 3:
    try:
        run(base64.b64decode(sys.argv[1], validate=True), sys.argv[2])
    except Exception:
        pass
