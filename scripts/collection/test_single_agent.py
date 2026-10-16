"""Evidence/ownership regression checks; temporary state only, no production."""
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import nullcontext

import coordctl
import playwrightctl
from single_agentctl import transition, handoff_single_execution


class SingleAgentTests(unittest.TestCase):
    def test_new_resource_priority_preserves_fifo_and_active_claims(self):
        import single_agentctl as single
        import workflowctl as workflow
        state={'threads':{'main':{'active_batch':None}},
               'queue':['old-a','new-a','old-b','new-b']}
        def update(_path, action): return action(state)
        with patch.object(single.coordctl,'_read_state',return_value=copy.deepcopy(state)), \
             patch.object(workflow.dbctl,'lookup',return_value=[
                 {'url':'old-a','captured':True},{'url':'new-a','captured':False},
                 {'url':'old-b','captured':True},{'url':'new-b','captured':False}]), \
             patch.object(single.coordctl,'_update',side_effect=update), \
             patch.object(single.coordctl,'require_collection_running'), \
             patch.object(single.coordctl,'_require_peer'):
            single.prioritize_new_resources('main')
        self.assertEqual(state['queue'],['new-a','new-b','old-a','old-b'])
        before=copy.deepcopy(state)
        state['threads']['main']['active_batch']='original-claim'
        with patch.object(single.coordctl,'_read_state',return_value=state), \
             patch.object(workflow.dbctl,'lookup') as lookup, \
             patch.object(single.coordctl,'_update') as mutate:
            single.prioritize_new_resources('main')
            lookup.assert_not_called();mutate.assert_not_called()
        self.assertEqual(state['queue'],before['queue'])

    def test_single_execution_handoff_preserves_queue_history_and_original_owner(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            old = {'role':'peer','work':str(root/'old'),'work_key':str(root/'old'),
                   'mode':'idle','active_batch':None}
            batches = {'old-ack':{'thread':'old','state':'acknowledged','verification':{'verified':True}}}
            state = {'execution_mode':'single','collection_active_owner':'old',
                     'collection_paused':True,'threads':{'old':copy.deepcopy(old)},
                     'batches':copy.deepcopy(batches),'queue':['observed-url'],
                     'single_agent_migration':{'ready_handoffs':[]}}
            receipt = handoff_single_execution(state,'old','new',root/'new',{'request_id':'user'})
            self.assertEqual(state['collection_active_owner'],'new')
            self.assertEqual(state['queue'],['observed-url'])
            self.assertEqual(state['batches'],batches)
            self.assertEqual(state['threads']['old']['work'],old['work'])
            self.assertEqual(state['threads']['old']['execution_handoff_to'],'new')
            self.assertEqual(state['threads']['new']['execution_handoff'],receipt)
            self.assertTrue(state['collection_paused'])
            state['collection_paused'] = False
            with self.assertRaises(coordctl.CoordinationError):
                coordctl._require_peer(state,'old')
            self.assertEqual(coordctl._require_peer(state,'new')['mode'],'idle')

    def test_single_execution_handoff_refuses_open_gate_and_unsettled_claim(self):
        for paused, batch in [(False,None),(True,'inflight')]:
            state = {'execution_mode':'single','collection_active_owner':'old',
                     'collection_paused':paused,'threads':{'old':{'mode':'idle','active_batch':batch}},
                     'batches':{}}
            before = copy.deepcopy(state)
            with self.assertRaises(ValueError):
                handoff_single_execution(state,'old','new',Path('new'),{'request_id':'user'})
            self.assertEqual(state,before)

    def test_single_stop_targets_only_exact_main_and_private_headless_profile(self):
        from process_stop import targets_for,dedicated_owner
        with tempfile.TemporaryDirectory() as tmp:
            project=Path(tmp);work=project/'runtime/main'
            owner='00000000-0000-0000-0000-000000000001'
            config={'main_thread':owner,'force_process_stop_authorized':True,'stop_authorized':True}
            main={'execution_mode':'single','main_thread':owner,'paused':True,
                  'slots':{'main':{'owner':owner,'work':str(work)}}}
            state={'collection_paused':True,'collection_active_owner':owner,
                   'threads':{owner:{'work':str(work)}}}
            owners=targets_for(config,main,state,{'stopped':True},project)
            self.assertEqual(owners,{owner:work.resolve()})
            args=['chrome.exe','--headless','--user-data-dir='+str(work/'playwright-browser-profile')]
            self.assertEqual(dedicated_owner(args,'chrome.exe',owners,project),owner)
            self.assertIsNone(dedicated_owner(['chrome.exe','--headless','--user-data-dir='+str(project/'user-chrome')],
                'chrome.exe',owners,project))
            self.assertIsNone(dedicated_owner([args[0],args[2]],'chrome.exe',owners,project))

    def test_handoff_retains_ready_and_requeues_only_stopped_claim(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            peers={str(i):{'role':'peer','work':str(root/str(i)),
                'work_key':str(root/str(i)),'mode':'idle','active_batch':None} for i in range(4)}
            slots={str(i):{'owner':str(i),'work':peers[str(i)]['work']} for i in range(4)}
            u1='https://www.xxmanmi.com/video1/1.html'
            u2='https://www.xxmanmi.com/video1/2.html'
            ready={'thread':'0','work':peers['0']['work'],'state':'ready',
                'logs_stopped':True,'item_urls':[u1],'verification':None}
            claimed={'thread':'1','work':peers['1']['work'],'state':'claimed','item_urls':[u2]}
            peers['0'].update(mode='awaiting_ack',active_batch='a')
            peers['1'].update(mode='claimed',active_batch='b')
            state={'collection_paused':True,'threads':peers,'queue':[],
                'batches':{'a':copy.deepcopy(ready),'b':claimed},
                'items':{u1:{'state':'ready','batch':'a'},u2:{'state':'claimed','batch':'b'}}}
            receipt=transition(state,'main',root/'main',slots,{'request_id':'user-request'})
            self.assertEqual(state['batches']['a'],ready)
            self.assertEqual(state['queue'],[u2])
            self.assertEqual(state['batches']['b']['state'],'released')
            self.assertNotIn('acknowledged_at',state['batches']['b'])
            self.assertEqual(receipt['ready_handoffs'][0]['batch'],'a')
            state['collection_paused']=False
            with self.assertRaises(coordctl.CoordinationError): coordctl._require_peer(state,'0')
            self.assertEqual(coordctl._require_peer(state,'main')['mode'],'idle')

    def test_migration_rejects_open_gate(self):
        with self.assertRaisesRegex(ValueError,'closed'):
            transition({'collection_paused':False},'main',Path('main'),{}, {})

    def test_refresh_requires_same_dom_and_keeps_previous_acceptance(self):
        with tempfile.TemporaryDirectory() as tmp:
            work=Path(tmp);state_path=work/'state.json'
            coordctl.init_state(state_path,'main',str(work/'worker'))
            peerwork=work/'worker';peerwork.mkdir()
            item={'url':'https://www.xxmanmi.com/video1/1.html','title':'Test',
                  'status':'Episode 1','source':'source','query':'','category':''}
            coordctl.enqueue_items(state_path,'main',[item])
            s=coordctl._read_state(state_path)
            s.update(execution_mode='single',collection_active_owner='main',queue=[])
            s['items'][item['url']].update(state='acknowledged',batch='original',outcome='captured')
            coordctl._atomic_write(state_path,s)
            updated={**item,'status':'Episode 2'}
            proof=peerwork/'proof.json'
            proof.write_text(json.dumps({'status':'observed','snapshot':{'view':'latest',
                'loading':False,'panelVisible':True,'readyState':'complete','items':[updated]}}),encoding='utf-8')
            bad={**updated,'status':'Invented'}
            with self.assertRaises(coordctl.CoordinationError):
                coordctl.enqueue_items(state_path,'main',[bad],refresh_evidence=proof)
            self.assertEqual(coordctl._read_state(state_path)['queue'],[])
            self.assertEqual(coordctl.enqueue_items(state_path,'main',[updated],refresh_evidence=proof)['refreshed'],1)
            row=coordctl._read_state(state_path)['items'][item['url']]
            self.assertEqual(row['update_history'][0]['previous_batch'],'original')
            self.assertEqual(row['item']['status'],'Episode 2')
            self.assertEqual(coordctl.enqueue_items(state_path,'main',[updated],refresh_evidence=proof)['added'],0)

    def test_truncated_segments_rejected(self):
        item={'url':'https://www.xxmanmi.com/video1/1.html'}
        evidence={'schema':'xxmanmi-playwright-detail-evidence-v1','owner':'main','batch':'b',
            'jobId':'a'*32,'segmentSize':100,'expectedUrl':item['url'],
            'metadata':{'observation':{'episodeDomCount':101,'fingerprint':'f'}},'segments':[]}
        with self.assertRaisesRegex(ValueError,'segments'):
            playwrightctl.validate_evidence(evidence,item,'main','b')

    def test_pending_publication_is_rebound_before_independent_acceptance(self):
        import production_live as live
        with tempfile.TemporaryDirectory() as tmp:
            current=Path(tmp)/'current.json'
            frozen={'state':'verified','manifest':{'event_revisions':{'1':3},'program_ids':[1]}}
            current.write_text(json.dumps(frozen),encoding='utf-8')
            state={'pending':{'1':{'revision':3}},'verified_events':0,'verified_releases':0}
            calls=[]
            def mutate(op): return op(state)
            def rebind(*args): calls.append('rebind');return {'release_id':'new'}
            def publish(**kwargs): calls.append('publish')
            def verify(*args):
                calls.append('verify');return {'release_id':'new'}
            with patch.object(live,'CURRENT',current),patch.object(live,'allowed',return_value=True), \
                 patch.object(live,'WorkerLock',return_value=nullcontext()), \
                 patch.object(live,'mutate',side_effect=mutate),patch.object(live,'ledger',return_value=state), \
                 patch.object(live.alignment,'rebind_frozen_delta',side_effect=rebind), \
                 patch.object(live.alignment,'publish',side_effect=publish), \
                 patch.object(live.alignment,'verify_frozen',side_effect=verify):
                result=live.drain(max_passes=1)
            self.assertEqual(calls,['rebind','publish','verify'])
            self.assertTrue(result['ok'])
            self.assertEqual(state['pending'],{})
            self.assertEqual(state['last_release_id'],'new')

    def test_unknown_empty_list_waits_for_real_items(self):
        empty={'url':'https://www.xxmanmi.com/','view':'latest','source':'source',
            'items':[],'radios':[],'loading':False,'explicitEmpty':False,
            'readyState':'complete','panelVisible':True}
        populated={**empty,'items':[{'url':'https://www.xxmanmi.com/video1/1.html'}]}
        class Page:
            def __init__(self): self.reads=0
            def evaluate(self,reader):
                self.reads+=1
                return empty if self.reads<4 else populated
            def wait_for_timeout(self,delay): pass
        page=Page()
        self.assertEqual(len(playwrightctl.settled_list(page,'reader')['items']),1)
        self.assertGreaterEqual(page.reads,5)

    def test_persistent_search_is_restored_to_observed_latest_tab(self):
        search={'view':'search','tabs':[{'value':'like','text':'最新'}],'radios':[]}
        latest={'view':'latest','radios':[{'name':'newNum','text':'酷播'}]}
        clicks=[]
        class Control:
            def count(self): return 1
            def is_visible(self): return True
            def click(self): clicks.append('latest')
        class Page:
            def locator(self,selector):
                self.selector=selector
                return Control()
        page=Page()
        with patch.object(playwrightctl,'settled_list',side_effect=[search,latest]):
            self.assertEqual(playwrightctl.latest_list(page,'reader',['酷播']),latest)
        self.assertEqual(clicks,['latest'])
        self.assertIn('data-id="like"',page.selector)

    def test_missing_latest_source_controls_cannot_complete_a_round(self):
        with patch.object(playwrightctl,'settled_list',return_value={'view':'latest','radios':[]}):
            with self.assertRaisesRegex(ValueError,'round cannot be completed'):
                playwrightctl.latest_list(object(),'reader',['酷播'])


if __name__=='__main__': unittest.main()
