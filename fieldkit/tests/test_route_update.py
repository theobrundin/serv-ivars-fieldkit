"""Route release gate and recovery tests; never contact the Pi."""
import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fieldkit import update_routes as u


class RouteUpdateTests(unittest.TestCase):
    def config(self):
        return {'device_id':'serv-ivars-001','location_id':'ivars-001',
                'capture':{'width':2304,'height':1296},'backend_writes_enabled':True,
                'api_key':'synthetic-secret','hours':{'timezone':'America/Los_Angeles'},
                'presence':{'shadow_only':True,'board_zone':{},'route_qualification':{
                    'decision_timeout_seconds':300,'terminal_exit_zone':{}}}}

    def test_patch_preserves_identity_hours_and_other_gates(self):
        cfg=self.config()
        for mode in ('shadow','active'):
            proposed=json.loads(u.patch_config(json.dumps(cfg),mode))
            expected=copy.deepcopy(cfg)
            for key in ('board_zone','route_qualification'):
                expected['presence'][key]=proposed['presence'][key]
            self.assertEqual(proposed,expected)
            self.assertEqual(proposed['presence']['route_qualification']['shadow_only'],mode=='shadow')
            self.assertEqual(proposed['presence']['route_qualification']['decision_timeout_seconds'],300)

    def test_other_site_frame_and_sandbox_refused(self):
        for key,value in [('device_id','serv-unit-001'),('location_id','ljs-louisville-001'),
                          ('backend_writes_enabled',False),('capture',{'width':1456,'height':1088})]:
            cfg=self.config();cfg[key]=value
            with self.assertRaises(ValueError):u.patch_config(json.dumps(cfg),'active')
        with self.assertRaises(ValueError):u.patch_config(json.dumps(self.config()),'unknown')

    def test_no_implicit_approval_or_lane_confirmation(self):
        with self.assertRaises(ValueError):u.apply(False)
        with tempfile.TemporaryDirectory() as tmp, patch.object(u,'identity'), patch.object(u,'APPROVAL',Path(tmp)/'absent'):
            with self.assertRaises(FileNotFoundError):u.apply(True)

    def test_reject_non_channel_or_expanded_application_scope(self):
        sha='1'*40
        with patch.object(u,'git',return_value=b'2'*40+b'\n'):
            with self.assertRaises(ValueError):u.release(sha,False)
        changed='\n'.join(u.SCOPE|{'plate_reader/hasher.py'}).encode()
        with patch.object(u,'git',side_effect=[sha.encode()+b'\n',changed]):
            with self.assertRaises(ValueError):u.release(sha,False)
        with self.assertRaises(ValueError):u.release('--bad',False)

    def transaction(self, fail=False):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            paths={'ROOT':root,'APP':root/'live'/'plate_reader','CONFIG':root/'config.json',
                   'PROFILE':root/'production.json','UNIT':root/'unit.json',
                   'SHA_FILE':root/'deployed','APPROVAL':root/'approval.json','STATE':root/'state.json'}
            paths['APP'].mkdir(parents=True)
            (paths['APP']/'old.py').write_text('old code')
            original=json.dumps(self.config()).encode()
            paths['CONFIG'].write_bytes(original);paths['PROFILE'].write_bytes(original)
            paths['UNIT'].write_text('{"release":"old"}')
            paths['SHA_FILE'].write_text('oldsha')
            paths['APPROVAL'].write_text(json.dumps({'sha':'1'*40,'mode':'active','before':{'config':'same'}}))
            queue=root/'queue.db';queue.write_bytes(b'do not rewind')
            stack=__import__('contextlib').ExitStack()
            with stack:
                for key,value in paths.items():stack.enter_context(patch.object(u,key,value))
                for name in ('identity','trusted','validate_config'):
                    stack.enter_context(patch.object(u,name))
                stack.enter_context(patch.object(u.os,'fchown'))
                stack.enter_context(patch.object(u,'baseline',return_value={'config':'same'}))
                stack.enter_context(patch.object(u,'release',return_value={'new.py':b'new code'}))
                commands=[]
                def run(args,**kwargs):
                    commands.append(args)
                    if fail and args==['systemctl','start',u.SERVICE] and len([c for c in commands if c==args])==1:
                        raise ValueError('synthetic startup failure')
                    return b''
                stack.enter_context(patch.object(u,'run',side_effect=run))
                stack.enter_context(patch.object(u,'healthy',return_value=True))
                if fail:
                    with self.assertRaises(ValueError):u.apply(True)
                    self.assertEqual((paths['APP']/'old.py').read_text(),'old code')
                    self.assertEqual(paths['CONFIG'].read_bytes(),original)
                    self.assertEqual(paths['PROFILE'].read_bytes(),original)
                    self.assertEqual(paths['SHA_FILE'].read_text(),'oldsha')
                    self.assertEqual(json.loads(paths['STATE'].read_text())['status'],'rolled_back_after_failure')
                else:
                    u.apply(True)
                    self.assertTrue((paths['APP']/'new.py').exists())
                    self.assertEqual(paths['APP'].stat().st_mode&0o777,0o755)
                    self.assertFalse(json.loads(paths['CONFIG'].read_text())['presence']['route_qualification']['shadow_only'])
                    self.assertEqual(paths['CONFIG'].read_bytes(),paths['PROFILE'].read_bytes())
                    self.assertEqual(paths['SHA_FILE'].read_text(),'1'*40+'\n')
                    u.rollback(True)
                    self.assertEqual((paths['APP']/'old.py').read_text(),'old code')
                    self.assertEqual(paths['CONFIG'].read_bytes(),original)
                self.assertEqual(queue.read_bytes(),b'do not rewind')
                self.assertFalse(paths['APPROVAL'].exists())
                self.assertTrue(all(c[0]=='systemctl' and c[-1]==u.SERVICE for c in commands))

    def test_apply_and_explicit_rollback_preserve_queue(self):
        self.transaction()

    def test_failed_start_restores_previous_code_and_profiles(self):
        self.transaction(fail=True)

    def test_config_drift_denies_apply_before_service_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            approval=Path(tmp)/'approval';approval.write_text(json.dumps({'sha':'1'*40,'mode':'active','before':{'config':'old'}}))
            with patch.object(u,'APPROVAL',approval),patch.object(u,'identity'),patch.object(u,'trusted'),patch.object(u,'release'),patch.object(u,'baseline',return_value={'config':'new'}),patch.object(u,'run') as run:
                with self.assertRaises(ValueError):u.apply(True)
                run.assert_not_called()


if __name__=='__main__':unittest.main()
