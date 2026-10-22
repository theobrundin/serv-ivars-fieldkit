"""Prepare private profiles on the Pi without changing running configuration."""
import argparse
import copy
import json
import os
from pathlib import Path

from fieldkit.host import CONFIG, private_json
from fieldkit.provision import private_directory


def prepare(identity_path, directory, timezone=None, opening=None, closing=None, site_file=None):
    from plate_reader.config import load, parse_hours
    identity = private_json(identity_path)
    if (identity['device_id'], identity['location_id']) != ('serv-ivars-001', 'ivars-001'):
        raise ValueError('Unexpected production identity')
    if site_file:
        if any(value is not None for value in (timezone, opening, closing)):
            raise ValueError('Use a site file or explicit daily hours, not both')
        site = json.loads(site_file.read_text())
        if (site['device_id'], site['location_id']) != (identity['device_id'], identity['location_id']):
            raise ValueError('Site and private identity do not match')
        hours = site['hours']
    else:
        hours = {'timezone': timezone, 'open': opening, 'close': closing, 'warmup_minutes': 15}
    parse_hours(hours)
    private_directory(directory)
    existing = private_json(CONFIG)
    production = copy.deepcopy(existing)
    production.update({k: identity[k] for k in ('device_id', 'location_id', 'api_key', 'api_base')})
    production['hours'] = hours
    production['backend_writes_enabled'] = True
    # Keep gates and hashing unchanged. Geometry still requires an Ivar's
    # field review; a new site's production checks cannot inherit LJS approval.
    sandbox = copy.deepcopy(production)
    sandbox.update(device_id='serv-unit-bench-reolink', location_id='bench-reolink-house',
                   api_key='0' * 64, api_base='https://backend-disabled.invalid', backend_writes_enabled=False)
    sandbox.pop('hours', None)
    if any((directory / (name + '.json')).exists() or (directory / (name + '.json')).is_symlink()
           for name in ('sandbox', 'production')):
        raise ValueError('Profiles already exist; review and archive them before regenerating')
    for name, value in (('sandbox', sandbox), ('production', production)):
        path = directory / (name + '.json')
        if path.exists():
            raise ValueError('Profiles already exist; review and archive them before regenerating')
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as f:
            json.dump(value, f, indent=2); f.write('\n')
        load(path)
    print('Both profiles validated and staged privately. Running configuration unchanged.')


if __name__ == '__main__':
    os.umask(0o077)
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--identity', type=Path, required=True); p.add_argument('--directory', type=Path, required=True)
    p.add_argument('--site-file', type=Path)
    p.add_argument('--timezone'); p.add_argument('--open'); p.add_argument('--close')
    a = p.parse_args()
    prepare(a.identity, a.directory, a.timezone, a.open, a.close, a.site_file)
