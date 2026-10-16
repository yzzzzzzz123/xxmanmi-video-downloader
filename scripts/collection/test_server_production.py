import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import collection_runtime as runtime
import server_production as production
import collector_host
import recoveryctl

class ServerProductionTests(unittest.TestCase):
    def test_local_retirement_blocks_writes(self):
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'disabled.json';marker.write_text('{}')
            with patch.dict(os.environ,{'MANYING_COLLECTION_MODE':'local'}),patch.object(runtime,'LOCAL_RETIRED',marker):
                with self.assertRaisesRegex(RuntimeError,'retired'):runtime.require_write()

    def test_production_mode_rejects_windows(self):
        with patch.dict(os.environ,{'MANYING_COLLECTION_MODE':'production'}),patch.object(runtime.sys,'platform','win32'):
            with self.assertRaisesRegex(RuntimeError,'server'):runtime.production()

    def test_wrong_production_database_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'config.json';path.write_text(json.dumps({'database':'resources'}))
            with patch.object(runtime,'production',return_value=True),patch.dict(os.environ,{'MANYING_COLLECTION_CONFIG':str(path)}):
                with self.assertRaisesRegex(ValueError,'target'):runtime.configuration()

    def test_unknown_mode_is_rejected(self):
        with patch.dict(os.environ,{'MANYING_COLLECTION_MODE':'staging'}):
            with self.assertRaises(ValueError):runtime.production()

    def test_retired_local_catalogue_cannot_prepare_or_publish_stale_data(self):
        import catalogue_align
        with tempfile.TemporaryDirectory() as directory:
            marker=Path(directory)/'disabled.json';marker.write_text('{}')
            with patch.dict(os.environ,{'MANYING_COLLECTION_MODE':'local'}),patch.object(runtime,'LOCAL_RETIRED',marker),patch.object(catalogue_align,'remote') as remote:
                for operation in [catalogue_align.prepare,catalogue_align.publish]:
                    with self.assertRaisesRegex(RuntimeError,'retired'):operation()
                remote.assert_not_called()

    def test_no_public_promotion_before_ack(self):
        with tempfile.TemporaryDirectory() as directory:
            current=Path(directory)/'stage.json';current.write_text(json.dumps({'owner':'owner','batch':'batch-000001','phase':'prepared'}))
            with patch.object(production,'CURRENT',current),patch.object(coordctl:=production.coordctl,'_read_state',return_value={'batches':{'batch-000001':{'state':'ready'}}}),patch.object(production.psycopg,'connect') as connect:
                with self.assertRaisesRegex(ValueError,'ACK'):production.finalize('owner','batch-000001')
                connect.assert_not_called()

    def test_pending_cover_never_opens_production_connection(self):
        with tempfile.TemporaryDirectory() as directory:
            current=Path(directory)/'stage.json';current.write_text(json.dumps({'owner':'owner','batch':'batch-000001','phase':'prepared'}))
            state={'batches':{'batch-000001':{'state':'acknowledged','results':[{'outcome':'captured'}],
                    'verification':{'cover_pending_items':['https://example.test/item']}}}}
            with patch.object(production,'CURRENT',current),patch.object(production.coordctl,'_read_state',return_value=state),patch.object(production.psycopg,'connect') as connect:
                result=production.finalize('owner','batch-000001')
                self.assertEqual(result['phase'],'cover_pending');connect.assert_not_called()

    def test_stage_cannot_replace_unresolved_batch(self):
        with tempfile.TemporaryDirectory() as directory:
            current=Path(directory)/'stage.json';current.write_text(json.dumps({'owner':'old','batch':'batch-000001','phase':'prepared'}))
            state={'collection_active_owner':'owner','threads':{},'batches':{'batch-000002':{'thread':'owner'}}}
            with patch.object(production,'CURRENT',current),patch.object(production.coordctl,'_read_state',return_value=state),patch.object(production.psycopg,'connect') as connect:
                with self.assertRaisesRegex(ValueError,'original staging'):production._prepare('owner','batch-000002')
                connect.assert_not_called()

    def test_changed_cover_never_replaces_production_asset(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory);source=folder/'stage.jpg';target=folder/'public.jpg'
            source.write_bytes(b'changed');target.write_bytes(b'original')
            with self.assertRaisesRegex(ValueError,'changed'):
                production._install_cover(source,target,'0'*64,folder/'backup.jpg')
            self.assertEqual(target.read_bytes(),b'original')

    def test_bootstrap_preserves_original_history_and_closes_path_dependency(self):
        with tempfile.TemporaryDirectory() as directory:
            project=Path(directory);root=project/'.runtime/collection-server';archive=root/'migration';work=archive/'original-work';work.mkdir(parents=True)
            old='original-owner';owner='server-owner'
            original={'collection_paused':True,'collection_active_owner':old,
                'threads':{old:{'mode':'idle','active_batch':None,'work':'C:/old/work'}},
                'batches':{'batch-000001':{'state':'acknowledged','thread':old,'work':'C:/old/work'}},
                'items':{},'queue':[],'next_batch':2}
            checkpoint={'owner':old,'action_inflight':None,'seen':{'key':'C:/old/work/source.json'},'pending':[{'controls':['newNum1']}],'sources':['天堂']}
            (work/'source.json').write_bytes(b'{"actual_evidence":true}')
            for path,value in [(archive/'coordination.json',original),(archive/'recovery.json',{'stopped':True,'stop_complete':True,'incidents':{}}),
                    (archive/'original-native-goal.json',{'goal':{'status':'paused'}}),(archive/'main.json',{}),(archive/'policy.json',{}),(work/'playwright-discovery.json',checkpoint)]:
                path.write_text(json.dumps(value))
            locations={name:project/(name+'.json') for name in ['POLICY','MAIN','RECOVERY','STATE']}
            with patch.object(runtime,'PROJECT',project),patch.object(collector_host,'ROOT',root),patch.object(collector_host,'HOST',root/'host.json'),patch.multiple(recoveryctl,**locations):
                collector_host.bootstrap(owner)
            actual=json.loads(locations['STATE'].read_text())
            self.assertEqual(actual['batches'],original['batches'])
            self.assertEqual(actual['next_batch'],2)
            self.assertEqual(json.loads((archive/'coordination.json').read_text()),original)
            new_work=Path(actual['threads'][owner]['work'])
            transferred=json.loads((new_work/'playwright-discovery.json').read_text())
            self.assertEqual(transferred['owner'],owner)
            retained=Path(transferred['seen']['key'])
            self.assertTrue(retained.is_relative_to(new_work))
            self.assertEqual(retained.read_bytes(),(work/'source.json').read_bytes())

if __name__=='__main__':unittest.main()
