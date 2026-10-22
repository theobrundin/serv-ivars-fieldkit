"""Exercise the incremental updater in a temporary filesystem, never a live Pi."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fieldkit import update_operator as updater


class OperatorUpdateTests(unittest.TestCase):
    def exercise(self, fail_manifest=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def mapped(value):
                value = str(value)
                if value.startswith(('/etc/', '/opt/', '/home/serv/', '/run/', '/var/backups')):
                    return root / value.lstrip('/')
                return Path(value)
            def write(name, data):
                p = mapped(name); p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data); return p
            release = 'a' * 40
            initial = {'release': updater.BASE, 'device_id': 'serv-ivars-001',
                       'location_id': 'ivars-001',
                       'machine_fingerprint': hashlib.sha256(b'test-machine').hexdigest()}
            manifest = write('/etc/serv-field/unit.json', json.dumps(initial).encode())
            write('/etc/machine-id', b'test-machine\n')
            config = write('/home/serv/plate_reader/config.json', b'{"backend_writes_enabled":false}')
            write('/run/serv-field/control.lock', b'')
            mapped('/var/backups').mkdir(parents=True)
            old = {'fieldkit/control.py': b'VALUE = 1\n', 'plate_reader/__init__.py': b''}
            new = {'fieldkit/control.py': b'VALUE = 2\n', 'fieldkit/new.py': b'NEW = True\n'}
            write('/opt/serv-field/fieldkit/control.py', old['fieldkit/control.py'])
            write('/opt/serv-reolink/plate_reader/__init__.py', b'')

            def command(args):
                if args[0] == 'tailscale': return '{"TailscaleIPs":["100.77.200.119"]}'
                if 'diff' in args: return ''
                if 'ls-tree' in args:
                    blobs = old if updater.BASE in args else new
                    return '\n'.join(k for k in blobs if k.startswith(args[-1]+'/'))
                if 'hash-object' in args: return 'verified-object'
                if 'rev-parse' in args:
                    return release if args[-1] == 'HEAD' else 'verified-object'
                raise AssertionError(args)

            def blob(args):
                ref, name = args[-1].split(':', 1)
                return (old if ref == updater.BASE else new)[name]

            original_stat = Path.stat
            class RootStat:
                st_uid = 0
                def __init__(self, value): self.value = value
                def __getattr__(self, name): return getattr(self.value, name)
            def root_stat(path, *args, **kwargs):
                return RootStat(original_stat(path, *args, **kwargs))

            real_atomic = updater.atomic
            failed = False
            def atomic(path, data, mode):
                nonlocal failed
                if fail_manifest and path == manifest and not failed:
                    failed = True
                    raise OSError('simulated manifest write failure')
                return real_atomic(path, data, mode)

            with patch.object(updater, 'Path', side_effect=mapped), \
                    patch.object(updater, '__file__', str(root/'source/fieldkit/update_operator.py')), \
                    patch.object(updater.os, 'geteuid', return_value=0), \
                    patch.object(updater.socket, 'gethostname', return_value='servTest'), \
                    patch.object(Path, 'stat', root_stat), \
                    patch.object(updater, 'run', side_effect=command), \
                    patch.object(updater.subprocess, 'check_output', side_effect=blob), \
                    patch.object(updater, 'atomic', side_effect=atomic):
                if fail_manifest:
                    with self.assertRaises(OSError): updater.update(release, True)
                else:
                    updater.update(release, True)
            actual = json.loads(manifest.read_text())
            if fail_manifest:
                self.assertTrue(failed, 'the simulated write failure must be reached')
                self.assertEqual(actual, initial)
                self.assertEqual(mapped('/opt/serv-field/fieldkit/control.py').read_bytes(), old['fieldkit/control.py'])
                self.assertFalse(mapped('/opt/serv-field/fieldkit/new.py').exists())
            else:
                self.assertEqual(actual['release'], release)
                self.assertTrue(actual['allow_open_camera_wifi'])
                self.assertEqual(mapped('/opt/serv-field/fieldkit/control.py').read_bytes(), new['fieldkit/control.py'])
                self.assertEqual(mapped('/opt/serv-field/fieldkit/new.py').read_bytes(), new['fieldkit/new.py'])
            self.assertEqual(config.read_bytes(), b'{"backend_writes_enabled":false}')
            self.assertEqual(mapped('/opt/serv-reolink/plate_reader/__init__.py').read_bytes(), b'')

    def test_approved_operator_update_preserves_application_and_records_policy(self):
        self.exercise()

    def test_failed_manifest_restores_old_operator_files_and_removes_new_files(self):
        self.exercise(fail_manifest=True)


if __name__ == '__main__':
    unittest.main()
