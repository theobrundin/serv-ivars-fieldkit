"""Preview/apply Ivar's local-calendar mapping without replacing other settings.

Requires AWS administrator credentials on the operator computer. Does not
change Lambda code. Revision checks reject concurrent environment edits.
"""
import argparse
import json
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo

FUNCTIONS = ('AggregateVisits', 'AggregateDetectionEvents', 'Analytics')


def merge_mapping(value, timezone):
    ZoneInfo(timezone)
    parts = []
    for part in value.split(','):
        if not part.strip(): continue
        if '=' not in part:
            raise ValueError('Malformed existing timezone mapping; review before changing')
        key, val = part.split('=', 1)
        if key.strip() != 'ivars-001': parts.append(key.strip() + '=' + val.strip())
    parts.append('ivars-001=' + timezone)
    return ','.join(parts)


def aws(operation, request):
    with tempfile.NamedTemporaryFile(mode='w') as f:
        json.dump(request, f); f.flush()
        p = subprocess.run(['aws', 'lambda', operation, '--region', 'us-east-1',
            '--cli-input-json', 'file://' + f.name, '--output', 'json', '--no-cli-pager'],
            text=True, capture_output=True, timeout=60)
    if p.returncode: raise RuntimeError('AWS configuration operation failed; private response suppressed')
    return json.loads(p.stdout or '{}')


def configure(timezone, apply):
    ZoneInfo(timezone)
    for name in FUNCTIONS:
        current = aws('get-function-configuration', {'FunctionName': name})
        variables = dict(current.get('Environment', {}).get('Variables', {}))
        before = variables.get('LOCATION_TIMEZONES', '')
        after = merge_mapping(before, timezone)
        if apply and after != before:
            variables['LOCATION_TIMEZONES'] = after
            aws('update-function-configuration', {'FunctionName': name,
                'RevisionId': current['RevisionId'], 'Environment': {'Variables': variables}})
            for _ in range(30):
                actual = aws('get-function-configuration', {'FunctionName': name})
                if actual.get('LastUpdateStatus') == 'Failed':
                    raise RuntimeError('Lambda update failed for ' + name)
                if actual.get('LastUpdateStatus') == 'Successful':
                    if actual.get('Environment', {}).get('Variables', {}).get('LOCATION_TIMEZONES') != after:
                        raise RuntimeError('Timezone readback differed for ' + name)
                    break
                time.sleep(2)
            else:
                raise RuntimeError('Timezone update not yet verified for ' + name)
        print(json.dumps({'function': name, 'location': 'ivars-001', 'timezone': timezone,
                          'changed': apply and after != before, 'preview_only': not apply}))


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--timezone', required=True); p.add_argument('--apply', action='store_true')
    a = p.parse_args(); configure(a.timezone, a.apply)
