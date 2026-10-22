"""Create isolated Ivar's identities. Secrets only enter private files / AWS.

Run on an administrator's computer with AWS CLI credentials. Existing items
must match exactly; this tool never rotates an existing device's key.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat
import subprocess
import tempfile
from zoneinfo import ZoneInfo

API = 'https://sb13iibr7f.execute-api.us-east-1.amazonaws.com'
IDENTITIES = {'production': ('serv-ivars-001', 'ivars-001'),
              'sandbox': ('serv-ivars-001-sandbox', 'ivars-001-sandbox')}


def aws(operation, payload):
    with tempfile.NamedTemporaryFile(mode='w', suffix='.json') as f:
        json.dump(payload, f); f.flush()
        result = subprocess.run(['aws', 'dynamodb', operation, '--region', 'us-east-1',
            '--cli-input-json', 'file://' + f.name, '--output', 'json', '--no-cli-pager'],
            text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError('AWS operation failed; private response suppressed')
    return json.loads(result.stdout or '{}')


def private_directory(path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    s = path.lstat()
    if not stat.S_ISDIR(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
        raise ValueError('Use an owned, real, mode-700 private directory')


def provision(directory, mode, timezone=None, apply=False):
    private_directory(directory)
    if timezone:
        ZoneInfo(timezone)
    device, location = IDENTITIES[mode]
    path = directory / (mode + '-identity.json')
    if path.exists() or path.is_symlink():
        s = path.lstat()
        if not stat.S_ISREG(s.st_mode) or s.st_mode & 0o077 or s.st_uid != os.getuid():
            raise ValueError('Existing identity file is not private')
        identity = json.loads(path.read_text())
        if (identity['device_id'], identity['location_id']) != (device, location):
            raise ValueError('Existing identity mismatch')
        if timezone and identity.get('timezone') != timezone:
            raise ValueError('Existing time zone differs; review before changing it')
    else:
        identity = dict(device_id=device, location_id=location, api_key=secrets.token_hex(32),
                        api_base=API, restaurant_name="Ivar's", location_number='001')
        if timezone:
            identity['timezone'] = timezone
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(identity, f, indent=2); f.write('\n'); f.flush(); os.fsync(f.fileno())
    if apply:
        item = {k: {'S': identity[k]} for k in
                ('device_id', 'location_id', 'api_key', 'restaurant_name', 'location_number')}
        item['status'] = {'S': 'active'}
        item['environment'] = {'S': mode}
        if identity.get('timezone'):
            item['timezone'] = {'S': identity['timezone']}
        old = aws('get-item', {'TableName': 'Devices', 'ConsistentRead': True,
                              'Key': {'device_id': {'S': device}}}).get('Item')
        if old and any(old.get(k) != v for k, v in item.items()):
            raise ValueError('AWS item differs; refusing to overwrite or rotate it')
        if not old:
            aws('put-item', {'TableName': 'Devices', 'Item': item,
                            'ConditionExpression': 'attribute_not_exists(device_id)'})
    print(json.dumps({'device': device, 'location': location, 'aws_verified': apply,
                      'private_identity_file': str(path)}))
    return identity


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--mode', choices=IDENTITIES, required=True)
    p.add_argument('--timezone')
    p.add_argument('--apply', action='store_true')
    a = p.parse_args()
    provision(a.directory, a.mode, a.timezone, a.apply)
