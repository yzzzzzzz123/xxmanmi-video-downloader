import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import workflowctl
from search_parent_host_quarantine import SELECTOR_ERROR


class ParentHostQuarantineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.work = self.root / 'work'
        self.state = self.root / 'state.json'
        self.discovery = self.root / 'discovery'
        self.books = self.root / 'taskbooks'
        workflowctl.coordctl.init_state(self.state, 'owner', str(self.work))
        with mock.patch.object(workflowctl.dbctl, 'searches', return_value=[
                {'query': '测试查询', 'source': '量子', 'category': '动漫'}]):
            claim = workflowctl.claim_search_task(self.state, 'owner', '量子',
                query='测试查询', task_id='search-parent-test',
                discovery_root=self.discovery, taskbooks=self.books)
        self.config = self.root / '.runtime/supervisor/config.json'
        workflowctl.atomic_json(self.config, {'main_thread': 'parent-session',
            'slots': {'01': {'owner': 'owner', 'work': str(self.work)}}})
        self.evidence = self.work / 'terminal.json'
        self.work.mkdir(exist_ok=True)
        self.value = {'version': 1, 'reason': 'quarantined_unverifiable_result',
            'owner': 'owner', 'task_id': claim['task_id'], 'query': '测试查询',
            'channel': '量子', 'claim_sha256': claim['claim_sha256'],
            'logs_stopped': True, 'main_authorized': True, 'browser_action_inflight': False,
            'journals': workflowctl._journal_snapshot(self.work),
            'browser_host': {'session_id': 'parent-session', 'thread_id': 'owner',
                'terminated': False, 'tools_terminal': True,
                'checked_at': '2026-10-04T05:00:00Z', 'method': 'actual host metadata'},
            'browser_tab': {'session_id': 'parent-session', 'owner_thread_id': 'owner',
                'exists': True, 'readable': True, 'tab_id': '123',
                'url': 'https://www.xxmanmi.com/',
                'checked_at': '2026-10-04T05:00:00Z', 'method': 'actual approved tab DOM'},
            'search_action': {'state': 'completed_unverifiable', 'action_attempts': 1,
                'replay_authorized': False, 'original_module_instance_available': True,
                'action_id': '56516473-6513-4c6f-be0d-b19dde6aef3e',
                'execute_next_action': 'finalize_search_action', 'execute_returned': True,
                'finalize_returned': True, 'finalize_attempts': 1,
                'private_action_record_exportable': False, 'result_error': SELECTOR_ERROR},
            'dom_summary': {'input_value_after_action': '测试查询', 'selected_channels': ['量子'],
                'page_url': 'https://www.xxmanmi.com/',
                'result_count': 2, 'visible_detail_links': [
                    {'url': 'https://www.xxmanmi.com/video2/1.html', 'title': '片一', 'status': '已完结'},
                    {'url': 'https://www.xxmanmi.com/video2/2.html', 'title': '片二', 'status': '已完结'}],
                'observed_at': '2026-10-04T05:00:00Z', 'result_container_visible': True,
                'pagination_pending': False, 'empty_result_visible': False},
            'local_state': {'browser_evidence_exists': False, 'commit_receipt_exists': False,
                'search_journal_contains_task': False},
            'summary': 'Returned original action is stranded by a frozen wrong selector; preserve proof and never replay.'}

    def run_quarantine(self, authorized=True):
        workflowctl.atomic_json(self.evidence, self.value)
        with mock.patch.object(workflowctl, 'PROJECT_ROOT', self.root):
            return workflowctl.quarantine_search_unverifiable_result(
                self.state, 'owner', '测试查询', '量子', self.evidence, True,
                discovery_root=self.discovery, taskbooks=self.books,
                authorized_parent_host=authorized)

    def test_true_parent_preserves_single_action_id_without_enqueue_or_success(self):
        before = self.state.read_bytes()
        result = self.run_quarantine()
        ledger = json.loads(Path(result['ledger']).read_bytes())['abandonments'][0]
        self.assertEqual(ledger['action_id'], self.value['search_action']['action_id'])
        self.assertFalse(ledger['search_success'])
        self.assertFalse(ledger['replay_authorized'])
        self.assertEqual(ledger['browser_host_session_id'], 'parent-session')
        self.assertEqual(ledger['browser_host_thread_id'], 'owner')
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse((self.work / 'searches.ndjson').exists())
        self.assertFalse((self.work / 'search-parent-test-evidence.json').exists())
        self.assertEqual(ledger['evidence_sha256'], hashlib.sha256(self.evidence.read_bytes()).hexdigest())

    def test_foreign_parent_without_explicit_authorization_still_rejected(self):
        with self.assertRaisesRegex(ValueError, 'live original browser host'):
            self.run_quarantine(False)

    def test_wrong_parent_or_owner_slot_rejected(self):
        self.value['browser_host']['session_id'] = 'unrelated-session'
        with self.assertRaisesRegex(ValueError, 'configured real main session'):
            self.run_quarantine()

    def test_unknown_execute_cannot_be_quarantined_as_completed(self):
        self.value['search_action']['execute_next_action'] = 'inspect_search_action'
        with self.assertRaisesRegex(ValueError, 'exact returned prepared action'):
            self.run_quarantine()

    def test_wrong_current_dom_query_preserves_claim(self):
        self.value['dom_summary']['input_value_after_action'] = '别的查询'
        with self.assertRaisesRegex(ValueError, 'actual complete read-only'):
            self.run_quarantine()

    def test_boolean_action_count_rejected(self):
        self.value['search_action']['action_attempts'] = True
        with self.assertRaisesRegex(ValueError, 'exact returned prepared action'):
            self.run_quarantine()

    def test_pause_gate_preserves_prepared_action(self):
        state = json.loads(self.state.read_bytes()); state['collection_paused'] = True
        workflowctl.atomic_json(self.state, state)
        with self.assertRaisesRegex(workflowctl.coordctl.CoordinationError, 'collection paused'):
            self.run_quarantine()

    def test_parent_binding_changed_during_stopped_log_check_preserves_claim(self):
        def change(_):
            workflowctl.atomic_json(self.config, {'main_thread': 'replacement-parent', 'slots': {}})
        with mock.patch.object(workflowctl.time, 'sleep', side_effect=change):
            with self.assertRaisesRegex(RuntimeError, 'parent binding, pause gate'):
                self.run_quarantine()

    def missing_tab(self):
        tab = self.value['browser_tab']
        tab.update({'exists': False, 'readable': False, 'missing_error': 'No tab with id: 123.',
                    'inventory_tab_ids': ['other-owned-tab']})
        self.value['historical_readonly_observation'] = self.value.pop('dom_summary')

    def test_confirmed_missing_tab_retains_true_past_dom_and_exact_action(self):
        self.missing_tab()
        result = self.run_quarantine()
        r = json.loads(Path(result['ledger']).read_bytes())['abandonments'][0]
        self.assertFalse(r['browser_tab_exists'])
        self.assertIsNone(r['dom_summary'])
        self.assertEqual(r['historical_readonly_observation']['result_count'], 2)
        self.assertEqual(r['action_id'], self.value['search_action']['action_id'])
        self.assertFalse(r['search_success'])

    def test_tab_gone_requires_actual_missing_error(self):
        self.missing_tab(); self.value['browser_tab']['missing_error'] = 'timeout'
        with self.assertRaisesRegex(ValueError, 'exact error'):
            self.run_quarantine()

    def test_tab_still_in_inventory_cannot_be_declared_missing(self):
        self.missing_tab(); self.value['browser_tab']['inventory_tab_ids'] = ['123']
        with self.assertRaisesRegex(ValueError, 'confirming inventory'):
            self.run_quarantine()

    def test_missing_tab_cannot_supply_historical_dom_as_current(self):
        self.missing_tab(); self.value['dom_summary'] = self.value['historical_readonly_observation']
        with self.assertRaisesRegex(ValueError, 'historical DOM as current'):
                self.run_quarantine()

    def test_missing_tab_terminal_ledger_rejects_replay_or_unbound_private_action(self):
        self.missing_tab()
        result = self.run_quarantine()
        ledger_path = Path(result['ledger'])
        original = json.loads(ledger_path.read_bytes())
        for key, bad in [('replay_authorized', True), ('action_id', ''),
                         ('parent_config_sha256', ''), ('execute_returned', False)]:
            with self.subTest(key=key):
                altered = json.loads(json.dumps(original))
                altered['abandonments'][0][key] = bad
                workflowctl.atomic_json(ledger_path, altered)
                with self.assertRaisesRegex(ValueError, 'inconsistent evidence'):
                    workflowctl.shared_discovery.load_abandonment_ledger(self.discovery)
        workflowctl.atomic_json(ledger_path, original)


class RetainedActionQuarantineTests(unittest.TestCase):
    def setUp(self):
        ParentHostQuarantineTests.setUp(self)
        self.config = self.root / '.runtime/main-agent/state.json'
        self.binding = {'main_thread': 'business-main', 'host_parent_thread': 'parent-session',
            'slots': {'01': {'owner': 'owner', 'work': str(self.work)}},
            'fault_repair_authorization': {'authorized': True, 'basis': 'direct_user',
                'scope': 'existing_workflow_fault_source_and_tests'}}
        workflowctl.atomic_json(self.config, self.binding)
        self.value['search_action']['result_error'] = 'Zero results require a visible empty-result indicator'
        self.value['dom_summary'].update(result_count=0, visible_detail_links=[],
            result_container_visible=False, empty_result_visible=False)

    def run_quarantine(self, *, candidate=False):
        workflowctl.atomic_json(self.evidence, self.value)
        with mock.patch.object(workflowctl, 'PROJECT_ROOT', self.root):
            return workflowctl.quarantine_search_unverifiable_result(
                self.state, 'owner', '测试查询', '量子', self.evidence, True, candidate,
                discovery_root=self.discovery, taskbooks=self.books,
                authorized_retained_action=True)

    def missing_tab(self):
        self.value['historical_readonly_observation'] = self.value.pop('dom_summary')
        self.value['browser_tab'].update(exists=False, readable=False,
            missing_error='No tab with id: 123.', inventory_tab_ids=['456'])

    def saved_candidate(self):
        candidate = self.work / 'search-parent-test-evidence.json'
        workflowctl.atomic_json(candidate, {'owner': 'owner', 'task_id': 'search-parent-test',
            'channel': '量子', 'selected_channels': ['量子'], 'query': '测试 查询',
            'input_value_after_action': '测试 查询', 'result_count': 1,
            'visible_detail_links': [{'url': 'https://www.xxmanmi.com/video2/1.html'}]})
        self.value['local_state'].update(browser_evidence_exists=True,
            saved_candidate_sha256=hashlib.sha256(candidate.read_bytes()).hexdigest())
        return candidate

    def test_absent_loading_indicator_is_valid_not_search_success(self):
        self.value['dom_summary']['loading_indicator'] = None
        self.run_quarantine()
        item = workflowctl.shared_discovery.load_abandonment_ledger(self.discovery)[1]['abandonments'][0]
        self.assertIsNone(item['dom_summary']['loading_indicator'])
        self.assertFalse(item['search_success'])
        self.assertFalse(item['replay_authorized'])

    def test_visible_or_malformed_loading_indicator_is_rejected(self):
        for loading in ({'visible_count': 1}, {'visible_count': -1},
                        {'visible_count': True}, {'visible_count': '0'}, False, []):
            with self.subTest(loading=loading):
                self.value['dom_summary']['loading_indicator'] = loading
                with self.assertRaisesRegex(ValueError, 'complete read-only'):
                    self.run_quarantine()

    def test_hidden_container_is_manual_review_never_success(self):
        before = self.state.read_bytes()
        result = self.run_quarantine()
        item = workflowctl.shared_discovery.load_abandonment_ledger(self.discovery)[1]['abandonments'][0]
        self.assertTrue(item['authorized_retained_action'])
        self.assertFalse(item['dom_summary']['result_container_visible'])
        self.assertFalse(item['search_success'])
        self.assertFalse(item['replay_authorized'])
        self.assertEqual(before, self.state.read_bytes())
        self.assertEqual(result['status'], 'quarantined_unverifiable_result')
        self.assertFalse((self.work / 'searches.ndjson').exists())

    def test_missing_tab_preserves_historical_mismatch_without_current_dom(self):
        self.value['dom_summary']['input_value_after_action'] = '测试 查询'
        self.missing_tab()
        self.run_quarantine()
        item = workflowctl.shared_discovery.load_abandonment_ledger(self.discovery)[1]['abandonments'][0]
        self.assertFalse(item['browser_tab_exists'])
        self.assertIsNone(item['dom_summary'])
        self.assertEqual(item['historical_readonly_observation']['input_value_after_action'], '测试 查询')

    def test_saved_mismatched_candidate_bytes_preserved(self):
        self.missing_tab()
        candidate = self.saved_candidate()
        before = candidate.read_bytes()
        self.run_quarantine(candidate=True)
        item = workflowctl.shared_discovery.load_abandonment_ledger(self.discovery)[1]['abandonments'][0]
        self.assertEqual(candidate.read_bytes(), before)
        self.assertEqual(item['saved_candidate_evidence_sha256'], hashlib.sha256(before).hexdigest())
        self.assertTrue(item['browser_evidence_exists'])

    def test_valid_candidate_must_resume_commit_not_quarantine(self):
        candidate = self.saved_candidate()
        value = json.loads(candidate.read_bytes())
        value.update(query='测试查询', input_value_after_action='测试查询')
        workflowctl.atomic_json(candidate, value)
        self.value['local_state']['saved_candidate_sha256'] = hashlib.sha256(candidate.read_bytes()).hexdigest()
        with self.assertRaisesRegex(RuntimeError, 'query/input mismatch'):
            self.run_quarantine(candidate=True)

    def test_foreign_candidate_and_changed_bytes_rejected(self):
        candidate = self.saved_candidate()
        original = candidate.read_bytes()
        for change in ({'owner': 'other'}, {'task_id': 'other'}, {'channel': '非凡'}):
            with self.subTest(change=change):
                value = json.loads(original); value.update(change)
                workflowctl.atomic_json(candidate, value)
                self.value['local_state']['saved_candidate_sha256'] = hashlib.sha256(candidate.read_bytes()).hexdigest()
                with self.assertRaisesRegex(RuntimeError, 'query/input mismatch'):
                    self.run_quarantine(candidate=True)
        candidate.write_bytes(original)
        self.value['local_state']['saved_candidate_sha256'] = '0' * 64
        with self.assertRaisesRegex(RuntimeError, 'unchanged bytes'):
            self.run_quarantine(candidate=True)

    def test_original_identity_and_settled_single_execution_required(self):
        import copy
        original = copy.deepcopy(self.value)
        cases = [('browser_host', 'session_id', 'other'), ('browser_host', 'thread_id', 'other'),
            ('browser_host', 'tools_terminal', False), ('search_action', 'action_id', ''),
            ('search_action', 'execute_returned', False), ('search_action', 'action_attempts', True),
            ('search_action', 'action_attempts', 2), ('search_action', 'finalize_returned', False),
            ('search_action', 'original_module_instance_available', False),
            ('search_action', 'replay_authorized', True), ('search_action', 'result_error', '')]
        for group, key, bad in cases:
            with self.subTest(group=group, key=key):
                self.value = copy.deepcopy(original); self.value[group][key] = bad
                with self.assertRaises(ValueError):
                    self.run_quarantine()
        self.value = copy.deepcopy(original); self.value['browser_action_inflight'] = True
        with self.assertRaises(ValueError):
            self.run_quarantine()

    def test_authorization_pause_and_slot_binding_required(self):
        import copy
        original = copy.deepcopy(self.binding)
        for kind in ('authorization', 'pause', 'work', 'parent'):
            with self.subTest(kind=kind):
                value = copy.deepcopy(original)
                if kind == 'authorization': value['fault_repair_authorization']['authorized'] = False
                if kind == 'pause': value['paused'] = True
                if kind == 'work': value['slots']['01']['work'] = str(self.root / 'foreign')
                if kind == 'parent': value['host_parent_thread'] = 'other'
                workflowctl.atomic_json(self.config, value)
                with self.assertRaises(ValueError):
                    self.run_quarantine()

    def test_missing_tab_requires_exact_error_actual_inventory_and_no_current_dom(self):
        import copy
        self.missing_tab(); original = copy.deepcopy(self.value)
        for key, bad in [('missing_error', 'timeout'), ('inventory_tab_ids', ['123']),
                         ('inventory_tab_ids', None)]:
            with self.subTest(key=key, bad=bad):
                self.value = copy.deepcopy(original); self.value['browser_tab'][key] = bad
                with self.assertRaises(ValueError): self.run_quarantine()
        self.value = copy.deepcopy(original)
        self.value['dom_summary'] = self.value['historical_readonly_observation']
        with self.assertRaises(ValueError): self.run_quarantine()

    def test_rechecks_authorization_evidence_and_candidate_under_stopped_log_gate(self):
        for target in ('config', 'evidence', 'candidate', 'commit', 'logs'):
            with self.subTest(target=target):
                candidate = self.saved_candidate()
                original_config = self.config.read_bytes()
                def mutate(_):
                    if target == 'config': workflowctl.atomic_json(self.config, {**self.binding, 'paused': True})
                    if target == 'evidence': workflowctl.atomic_json(self.evidence, {**self.value, 'summary': 'changed'})
                    if target == 'candidate': candidate.write_bytes(candidate.read_bytes() + b' ')
                    if target == 'commit': workflowctl.atomic_json(self.work / 'search-commit-search-parent-test.json', {})
                    if target == 'logs': (self.work / 'searches.ndjson').write_text('{}\n', encoding='utf-8')
                with mock.patch.object(workflowctl.time, 'sleep', side_effect=mutate):
                    with self.assertRaises(RuntimeError): self.run_quarantine(candidate=True)
                self.config.write_bytes(original_config)
                for name in ('search-commit-search-parent-test.json', 'searches.ndjson'):
                    (self.work / name).unlink(missing_ok=True)


class ParentUnsubmittedInputTests(unittest.TestCase):
    def setUp(self):
        ParentHostQuarantineTests.setUp(self)
        self.config = self.root / '.runtime/main-agent/state.json'
        self.proof = {'owner': 'owner', 'task_id': 'search-parent-test',
            'query': '测试查询', 'channel': '量子', 'tab_id': '123',
            'proof_verified': True, 'source_verified_no_submit': True,
            'all_tools_terminal': True, 'runtime_reset_verified': True,
            'terminal_tool_ids': ['fill-tool', 'readonly-tool', 'inventory-tool'],
            'reset_error': 'js execution timed out; kernel reset, rerun your request'}
        self.binding = {'host_parent_thread': 'parent-session',
            'slots': {'03': {'owner': 'owner', 'work': str(self.work)}},
            'fault_repair_authorization': {'authorized': True, 'basis': 'direct_user',
                'scope': 'existing_workflow_fault_source_and_tests'},
            'unsubmitted_input_recoveries': [self.proof]}
        workflowctl.atomic_json(self.config, self.binding)
        self.value.update(reason='abandoned_uninspectable_input_action', dom_summary=None)
        self.value['browser_host']['session_reset'] = True
        self.value['browser_tab'].update(readable=False, owned_by_original_session=True,
                                        inventory_tab_ids=['123'])
        self.value['search_action'] = {'state': 'input_fill_unknown_unsubmitted',
            'action_attempts': 1, 'input_fill_attempts': 1, 'submit_attempts': 0,
            'input_result': 'unknown', 'prepare_result': 'unknown',
            'original_module_instance_available': False, 'execute_receipt': False,
            'inspect_receipt': False, 'finalize_receipt': False, 'replay_authorized': False}
        self.value['terminal_actions'] = [
            {'sequence': i + 1, 'tool_id': tool, 'tool_terminal': True,
             'operation': 'actual original source operation',
             'status': 'failed' if i < 2 else 'completed',
             'error': self.proof['reset_error'] if i < 2 else None}
            for i, tool in enumerate(self.proof['terminal_tool_ids'])]

    def abandon(self, authorized=True):
        workflowctl.atomic_json(self.evidence, self.value)
        with mock.patch.object(workflowctl, 'PROJECT_ROOT', self.root):
            return workflowctl.abandon_uninspectable_input_action(self.state, 'owner',
                '测试查询', '量子', self.evidence, True, discovery_root=self.discovery,
                taskbooks=self.books, authorized_parent_input=authorized)

    def test_verified_no_submit_is_manual_review_without_replay_or_fake_dom(self):
        before = self.state.read_bytes()
        result = self.abandon()
        item = workflowctl.shared_discovery.load_abandonment_ledger(self.discovery)[1]['abandonments'][0]
        self.assertEqual(result['status'], 'abandoned_uninspectable_input_action')
        self.assertTrue(item['manual_review_required'])
        self.assertFalse(item['search_success'])
        self.assertFalse(item['replay_authorized'])
        self.assertEqual(item['submit_attempts'], 0)
        self.assertEqual(item['prepare_result'], 'unknown')
        self.assertEqual(item['browser_host_session_id'], 'parent-session')
        self.assertEqual(before, self.state.read_bytes())
        self.assertFalse((self.work / 'searches.ndjson').exists())

    def test_wrong_host_known_action_click_or_nonterminal_proof_is_rejected(self):
        import copy
        original = copy.deepcopy(self.value)
        cases = [('browser_host', 'session_id', 'other'), ('browser_host', 'tools_terminal', False),
            ('browser_tab', 'inventory_tab_ids', []), ('browser_tab', 'readable', True),
            ('search_action', 'submit_attempts', 1), ('search_action', 'submit_attempts', False),
            ('search_action', 'action_id', 'known'), ('search_action', 'input_result', 'success'),
            ('search_action', 'original_module_instance_available', True)]
        for group, key, value in cases:
            with self.subTest(group=group, key=key):
                self.value = copy.deepcopy(original); self.value[group][key] = value
                with self.assertRaises(ValueError): self.abandon()
        self.value = copy.deepcopy(original)
        self.value['terminal_actions'][0]['tool_terminal'] = False
        with self.assertRaises(ValueError): self.abandon()
        self.value = copy.deepcopy(original); self.value['dom_summary'] = {'result_count': 0}
        with self.assertRaises(ValueError): self.abandon()
        self.value = copy.deepcopy(original)
        with self.assertRaises(ValueError): self.abandon(False)

    def test_main_source_verification_pause_and_stopped_state_are_rechecked(self):
        import copy
        original = copy.deepcopy(self.binding)
        for key in ('proof_verified', 'source_verified_no_submit', 'all_tools_terminal', 'runtime_reset_verified'):
            with self.subTest(key=key):
                config = copy.deepcopy(original); config['unsubmitted_input_recoveries'][0][key] = False
                workflowctl.atomic_json(self.config, config)
                with self.assertRaises(ValueError): self.abandon()
        workflowctl.atomic_json(self.config, original)
        for target in ('config', 'evidence', 'commit', 'logs'):
            with self.subTest(target=target):
                def mutate(_):
                    if target == 'config': workflowctl.atomic_json(self.config, {**original, 'paused': True})
                    if target == 'evidence': workflowctl.atomic_json(self.evidence, {**self.value, 'summary': 'changed'})
                    if target == 'commit': workflowctl.atomic_json(self.work / 'search-commit-search-parent-test.json', {})
                    if target == 'logs': (self.work / 'searches.ndjson').write_text('{}\n', encoding='utf-8')
                with mock.patch.object(workflowctl.time, 'sleep', side_effect=mutate):
                    with self.assertRaises(RuntimeError): self.abandon()
                workflowctl.atomic_json(self.config, original)
                for name in ('search-commit-search-parent-test.json', 'searches.ndjson'):
                    (self.work / name).unlink(missing_ok=True)


class MisboundResumeTests(unittest.TestCase):
    run_quarantine = RetainedActionQuarantineTests.run_quarantine
    missing_tab = RetainedActionQuarantineTests.missing_tab

    def setUp(self):
        RetainedActionQuarantineTests.setUp(self)
        self.value['dom_summary']['input_value_after_action'] = '测试 查询'
        self.missing_tab()
        self.terminal = self.run_quarantine()
        self.binding['retained_misbound_actions'] = [{'owner': 'owner', 'task_id': 'search-parent-test',
            'action_id': self.value['search_action']['action_id'], 'query': '测试查询',
            'prepared_query': '测试 查询', 'channel': '量子', 'proof_verified': True,
            'all_tools_terminal': True, 'original_prepare_tool': 'fixture-prepare',
            'original_execute_tool': 'fixture-execute',
            'release_error': 'Release requires a matching workflow terminal receipt from the runtime ledger'}]
        workflowctl.atomic_json(self.config, self.binding)

    def resume(self):
        with mock.patch.object(workflowctl, 'PROJECT_ROOT', self.root):
            return workflowctl.resume_quarantined_misbound_search(self.state, 'owner',
                '测试查询', '量子', self.evidence, True, discovery_root=self.discovery, taskbooks=self.books)

    def test_resume_is_idempotent_preserves_terminal_and_never_claims_release(self):
        ledger = Path(self.terminal['ledger']); before = ledger.read_bytes()
        state_before = self.state.read_bytes()
        first = self.resume(); second = self.resume()
        self.assertEqual(first, second)
        self.assertFalse(first['released']); self.assertFalse(first['search_success'])
        self.assertFalse(first['replay_authorized'])
        self.assertEqual(first['next_action']['kind'], 'claim_other_search')
        self.assertEqual(before, ledger.read_bytes()); self.assertEqual(state_before, self.state.read_bytes())

    def test_unverified_unknown_or_matching_actions_cannot_resume(self):
        import copy
        original = copy.deepcopy(self.binding)
        for key, bad in [('proof_verified', False), ('all_tools_terminal', False),
            ('prepared_query', '测试查询'), ('action_id', 'another'), ('channel', '非凡'),
            ('original_prepare_tool', ''), ('original_execute_tool', ''), ('release_error', 'timeout')]:
            with self.subTest(key=key):
                changed = copy.deepcopy(original); changed['retained_misbound_actions'][0][key] = bad
                workflowctl.atomic_json(self.config, changed)
                with self.assertRaises(ValueError): self.resume()

    def test_saved_evidence_changed_logs_pause_or_changed_authority_rejects_resume(self):
        for case in ('candidate', 'commit', 'logs', 'pause', 'config-change'):
            with self.subTest(case=case):
                if case == 'candidate': workflowctl.atomic_json(self.work / 'search-parent-test-evidence.json', {})
                if case == 'commit': workflowctl.atomic_json(self.work / 'search-commit-search-parent-test.json', {})
                if case == 'logs': (self.work / 'searches.ndjson').write_text('{}\n', encoding='utf-8')
                if case == 'pause': workflowctl.atomic_json(self.state, {**json.loads(self.state.read_bytes()), 'collection_paused': True})
                with mock.patch.object(workflowctl.time, 'sleep', side_effect=(
                        lambda _: workflowctl.atomic_json(self.config, {**self.binding, 'paused': True})
                        ) if case == 'config-change' else None):
                    with self.assertRaises(RuntimeError): self.resume()
                for name in ('search-parent-test-evidence.json', 'search-commit-search-parent-test.json', 'searches.ndjson'):
                    (self.work / name).unlink(missing_ok=True)
                state = json.loads(self.state.read_bytes()); state['collection_paused'] = False
                workflowctl.atomic_json(self.state, state)


if __name__ == '__main__':
    unittest.main()
