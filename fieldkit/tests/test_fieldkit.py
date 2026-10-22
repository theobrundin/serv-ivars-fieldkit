import contextlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from datetime import datetime
from xml.etree import ElementTree as E
from unittest.mock import patch

from fieldkit import host, provision, control, install
from fieldkit import image
from fieldkit.cloud_timezone import merge_mapping


class FieldTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_preview_requires_cellular_consent_even_with_wifi_preferred(self):
        routes = [{'dst': 'default', 'dev': 'wlan1', 'metric': 10},
                  {'dst': 'default', 'dev': 'wwan0', 'metric': 1600}]
        with self.assertRaises(ValueError):
            host.preview_permitted(routes, False)
        host.preview_permitted(routes, True)

    def test_preview_conservatively_detects_cellular_routes_in_other_tables(self):
        with self.assertRaises(ValueError):
            host.preview_permitted([{'dev': 'wwan0', 'table': 52}], False)

    def test_route_check_includes_ipv6_and_fails_closed(self):
        with patch.object(host, 'run', side_effect=[(0, '[]'), (0, '[{"dev":"wwan0","dst":"::/0"}]')]):
            self.assertTrue(host.metered_possible(host.routes()))
        with patch.object(host, 'run', side_effect=[(0, '[]'), (1, '')]):
            with self.assertRaises(ValueError): host.routes()

    def test_wifi_preview_no_cellular_route(self):
        host.preview_permitted([{'dev': 'wlan1'}, {'dev': 'tailscale0'}], False)

    def test_preview_requires_overlay_ssh_and_bounded_time(self):
        with patch.dict(os.environ, {'SSH_CONNECTION': '192.168.1.20 123 192.168.1.10 22'}):
            with self.assertRaises(ValueError): host.preview(20, False)
        for duration in (0, 301):
            with self.assertRaises(ValueError): host.preview(duration, True)

    def test_private_credentials_refuse_public_file_and_symlink(self):
        p = self.root / 'secret.json'; p.write_text('{}'); p.chmod(0o644)
        with self.assertRaises(ValueError): host.private_json(p)
        p.chmod(0o600)
        link = self.root / 'link'; link.symlink_to(p)
        with self.assertRaises(ValueError): host.private_json(link)
        self.assertEqual(host.private_json(p), {})

    def test_provision_is_private_idempotent_and_never_prints_key(self):
        directory = self.root / 'private'
        with contextlib.redirect_stdout(io.StringIO()) as output:
            one = provision.provision(directory, 'sandbox')
            two = provision.provision(directory, 'sandbox')
        self.assertEqual(one, two)
        self.assertNotIn(one['api_key'], output.getvalue())
        self.assertEqual((directory / 'sandbox-identity.json').stat().st_mode & 0o777, 0o600)

    def test_provision_cannot_overwrite_existing_cloud_identity(self):
        with patch.object(provision, 'aws', return_value={'Item': {'device_id': {'S': 'different'}}}) as aws:
            with self.assertRaises(ValueError): provision.provision(self.root / 'private', 'production', apply=True)
            self.assertEqual(aws.call_count, 1)

    def test_provision_creation_is_conditional(self):
        with patch.object(provision, 'aws', side_effect=[{}, {}]) as aws, contextlib.redirect_stdout(io.StringIO()):
            provision.provision(self.root / 'private', 'production', apply=True)
        self.assertEqual(aws.call_args.args[1]['ConditionExpression'], 'attribute_not_exists(device_id)')

    def test_modes_require_empty_lane_before_reading_secrets_or_stopping_app(self):
        with patch.object(control, 'command') as command, patch.object(host, 'private_json') as read:
            with self.assertRaises(ValueError): control.mode('production', False)
            command.assert_not_called(); read.assert_not_called()

    def test_network_wifi_cannot_take_camera_radio(self):
        with patch.object(host, 'routes', return_value=[{'dst': 'default', 'dev': 'wlan0'}]), patch.object(control, 'command') as command:
            with self.assertRaises(ValueError): control.network('wifi')
            command.assert_not_called()

    def test_install_never_proceeds_without_empty_lane_confirmation(self):
        with patch.object(install, 'command') as command:
            with self.assertRaises(ValueError): install.install('a' * 40, self.root, 'cellular')
            command.assert_not_called()

    def test_atomic_update_keeps_owner_permissions_and_no_partial_file(self):
        p = self.root / 'state.json'
        control.atomic(p, b'{}', os.getuid(), os.getgid())
        self.assertEqual(p.read_bytes(), b'{}')
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        self.assertFalse(p.with_name(p.name + '.new').exists())

    def test_existing_staging_symlink_is_never_followed(self):
        p = self.root / 'state.json'; victim = self.root / 'victim'; victim.write_text('original')
        p.with_name(p.name + '.new').symlink_to(victim)
        with self.assertRaises(FileExistsError): control.atomic(p, b'changed')
        self.assertEqual(victim.read_text(), 'original')

    def test_metrics_do_not_print_raw_text_or_count_as_delivered(self):
        audit = self.root / 'audit_cases'; audit.mkdir()
        (audit / 'one.json').write_text(json.dumps({'outcome': 'confirmed', 'plate': 'SECRET123', 'trafficDecision': 'visit_queued'}))
        with patch.object(host, 'CONFIG', self.root / 'config.json'):
            out = host.metrics(10)
        self.assertEqual(out['outcomes'], {'confirmed': 1})
        self.assertNotIn('SECRET123', json.dumps(out))
        self.assertIn('not unique', out['limitation'])

    def test_image_range_rejected_before_any_request(self):
        with patch.object(image, 'call') as call:
            for value in (-1, 101, float('nan')):
                with self.assertRaises(ValueError): image.adjust({'brightness': value})
            call.assert_not_called()

    def test_image_rejected_readback_restores_original(self):
        sources = E.fromstring('<x xmlns:t="http://www.onvif.org/ver10/media/wsdl"><t:VideoSources token="source"/></x>')
        xml = E.fromstring('<t:ImagingSettings xmlns:t="http://www.onvif.org/ver20/imaging/wsdl" xmlns:s="http://www.onvif.org/ver10/schema"><s:Brightness>50</s:Brightness></t:ImagingSettings>')
        with patch.object(image, 'call', return_value=sources) as call, patch.object(image, 'settings', return_value=xml):
            with self.assertRaises(ValueError): image.adjust({'brightness': 60})
        setters = [c for c in call.call_args_list if c.args[1] == 'SetImagingSettings']
        self.assertEqual(len(setters), 2)
        self.assertIn('>60<', setters[0].args[2]); self.assertIn('>50<', setters[1].args[2])

    def test_image_read_only_does_not_write(self):
        sources = E.fromstring('<x xmlns:t="http://www.onvif.org/ver10/media/wsdl"><t:VideoSources token="source"/></x>')
        xml = E.fromstring('<t:ImagingSettings xmlns:t="http://www.onvif.org/ver20/imaging/wsdl" xmlns:s="http://www.onvif.org/ver10/schema"><s:Brightness>50</s:Brightness></t:ImagingSettings>')
        with patch.object(image, 'call', return_value=sources) as call, patch.object(image, 'settings', return_value=xml):
            self.assertEqual(image.adjust({}), {'brightness': 50})
        self.assertEqual(call.call_count, 1)

    def test_actual_hours_parser_handles_warmup_close_and_overnight(self):
        from plate_reader.config import parse_hours
        hours = parse_hours({'timezone': 'America/Los_Angeles', 'open': '10:00', 'close': '22:00', 'warmup_minutes': 15})
        early = datetime.fromisoformat('2026-10-21T09:45:00-07:00')
        self.assertTrue(hours.capture_should_run(early)); self.assertFalse(hours.is_open(early))
        self.assertTrue(hours.is_open(datetime.fromisoformat('2026-10-21T10:00:00-07:00')))
        self.assertFalse(hours.capture_should_run(datetime.fromisoformat('2026-10-21T22:00:00-07:00')))
        overnight = parse_hours({'timezone': 'America/Los_Angeles', 'open': '18:00', 'close': '02:00'})
        self.assertTrue(overnight.is_open(datetime.fromisoformat('2026-10-22T01:59:00-07:00')))
        self.assertFalse(overnight.is_open(datetime.fromisoformat('2026-10-22T02:00:00-07:00')))

    def test_timezone_mapping_preserves_other_sites(self):
        self.assertEqual(merge_mapping('ljs-louisville-001=America/New_York,ivars-001=UTC', 'America/Los_Angeles'),
                         'ljs-louisville-001=America/New_York,ivars-001=America/Los_Angeles')
        with self.assertRaises(ValueError): merge_mapping('malformed', 'UTC')


if __name__ == '__main__':
    unittest.main()
