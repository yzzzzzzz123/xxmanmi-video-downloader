import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import workflowctl
from search_terminal_quarantine import quarantine, REASON


class TerminalSearchQuarantineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state, self.work = self.root / 'state.json', self.root / 'work'
        self.discovery, self.books = self.root / 'discovery', self.root / 'books'
        workflowctl.coordctl.init_state(self.state, 'owner', str(self.work))
        with mock.patch.object(workflowctl.dbctl, 'searches', return_value=[
                {'query': '测试查询', 'source': '量子', 'category': '动漫'}]):
            self.claim = workflowctl.claim_search_task(
                self.state, 'owner', '量子', query='测试查询', task_id='search-terminal-test',
                discovery_root=self.discovery, taskbooks=self.books)
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        self.evidence = {'version': 1, 'reason': REASON, 'owner': 'owner',
            'task_id': self.claim['task_id'], 'query': '测试查询', 'channel': '量子',
            'claim_sha256': self.claim['claim_sha256'], 'logs_stopped': True,
            'main_authorized': True, 'browser_action_inflight': False,
            'original_action_proof_available': False,
            'terminal_actions': [{'sequence': i, 'operation': operation, 'tool_terminal': True}
                                 for i, operation in enumerate(['Enter (search effect unknown)', 'search view control', 'channel click'], 1)],
            'terminal_action_count': 3,
            'browser_host': {'session_id': 'actual-parent', 'thread_id': 'owner',
                'terminated': False, 'tools_terminal': True, 'checked_at': '2026-10-04T04:45:00Z',
                'method': 'actual requestMeta and terminal tool receipts'},
            'browser_tab': {'tab_id': '123', 'exists': True,
                'checked_at': '2026-10-04T04:45:00Z', 'method': 'approved readonly DOM'},
            'dom_summary': {'input_value_after_action': '测试查询', 'selected_channels': ['量子'],
                'result_count': 1, 'result_container_visible': True, 'pagination_pending': False},
            'local_state': {'browser_evidence_exists': False, 'commit_receipt_exists': False,
                'search_journal_contains_task': False},
            'journals_before': journals, 'journals_after': journals,
            'summary': 'All tool calls settled, search proof absent; do not replay or qualify results'}
        self.path = self.work / 'terminal-quarantine.json'
        self.original_claim = Path(self.claim['claim_path']).read_bytes() if 'claim_path' in self.claim else None

    def run_quarantine(self, *, authorized=True):
        workflowctl.atomic_json(self.path, self.evidence)
        return quarantine(self.state, 'owner', '测试查询', '量子', self.path, True, authorized,
                          discovery_root=self.discovery, taskbooks=self.books, workflow=workflowctl)

    def test_multiple_terminal_interactions_are_manual_review_not_success(self):
        state_raw = self.state.read_bytes()
        current = workflowctl.shared_discovery.inspect_claim(self.discovery, '测试查询', '量子')
        claim_raw = Path(current['path']).read_bytes()
        receipt = self.run_quarantine()
        self.assertFalse(receipt['search_success'])
        self.assertFalse(receipt['replay_authorized'])
        self.assertTrue(receipt['manual_review_required'])
        self.assertEqual(self.state.read_bytes(), state_raw)
        self.assertEqual(Path(current['path']).read_bytes(), claim_raw)
        self.assertFalse((self.work / 'searches.ndjson').exists())
        result = workflowctl.shared_discovery.inspect_claim(self.discovery, '测试查询', '量子')
        self.assertEqual(result['reason'], REASON)
        self.assertEqual(result['abandonment']['terminal_action_count'], 3)

    def test_missing_tab_foreign_parent_session_does_not_need_fabricated_action_id(self):
        self.evidence['browser_tab'].update(exists=False, inventory_tab_ids=['other-owned-tab'])
        self.evidence['terminal_actions'] = [{'sequence': 1, 'operation': 'one-shot helper raised after returned click', 'tool_terminal': True}]
        self.evidence['terminal_action_count'] = 1
        self.evidence.pop('dom_summary')
        self.assertTrue(self.run_quarantine()['ok'])

    def test_unknown_inflight_action_is_rejected(self):
        self.evidence['terminal_actions'][0]['tool_terminal'] = False
        with self.assertRaises(ValueError):
            self.run_quarantine()

    def test_reset_fill_keeps_unknown_outcome_until_confirmed_tab_loss(self):
        self.evidence['terminal_actions'] = [{
            'sequence': 1, 'operation': 'input fill timed out; kernel reset; fill outcome unknown',
            'tool_terminal': True, 'fill_outcome': 'unknown', 'search_click_attempts': 0}]
        self.evidence['terminal_action_count'] = 1
        self.evidence['dom_summary']['input_value_after_action'] = 'old input'
        with self.assertRaises(ValueError):
            self.run_quarantine()
        self.assertEqual(workflowctl.shared_discovery.inspect_claim(
            self.discovery, '测试查询', '量子')['reason'], 'claimed')
        self.evidence['browser_tab'].update(exists=False, inventory_tab_ids=['other-owned-tab'])
        self.evidence['historical_readonly_observation'] = self.evidence.pop('dom_summary')
        before_state = self.state.read_bytes()
        result = self.run_quarantine()
        audit = json.loads(Path(result['ledger']).read_bytes())['abandonments'][0]
        self.assertEqual(audit['terminal_actions'][0]['fill_outcome'], 'unknown')
        self.assertEqual(audit['terminal_actions'][0]['search_click_attempts'], 0)
        self.assertFalse(audit['browser_tab_exists'])
        self.assertFalse(result['search_success'])
        self.assertFalse(result['replay_authorized'])
        self.assertEqual(self.state.read_bytes(), before_state)
        self.assertFalse((self.work / 'searches.ndjson').exists())
        self.assertFalse((self.work / 'search-terminal-test-evidence.json').exists())

    def test_main_authorization_is_required(self):
        with self.assertRaises(ValueError):
            self.run_quarantine(authorized=False)

    def test_exact_claim_hash_is_required(self):
        self.evidence['claim_sha256'] = '0' * 64
        with self.assertRaises(ValueError):
            self.run_quarantine()

    def test_existing_commit_is_protected(self):
        (self.work / 'search-commit-search-terminal-test.json').write_text('{}')
        with self.assertRaises(RuntimeError):
            self.run_quarantine()

    def test_existing_search_journal_is_protected(self):
        (self.work / 'searches.ndjson').write_text(json.dumps({'task_id': self.claim['task_id']}) + '\n')
        journals = workflowctl._journal_snapshot(self.work)
        self.evidence.update(journals_before=journals, journals_after=journals)
        with self.assertRaises(RuntimeError):
            self.run_quarantine()

    def test_changed_logs_are_rejected(self):
        (self.work / 'details.ndjson').write_text('{}\n')
        with self.assertRaises(ValueError):
            self.run_quarantine()

    def test_dom_for_different_query_is_rejected(self):
        self.evidence['dom_summary']['input_value_after_action'] = 'another query'
        with self.assertRaises(ValueError):
            self.run_quarantine()

    def test_missing_inventory_cannot_contain_original_tab(self):
        self.evidence['browser_tab'].update(exists=False, inventory_tab_ids=['123'])
        with self.assertRaises(ValueError):
            self.run_quarantine()

    def test_original_evidence_reentry_is_idempotent(self):
        first = self.run_quarantine()
        second = self.run_quarantine()
        self.assertEqual(first['abandonment_id'], second['abandonment_id'])
        ledger = json.loads(Path(first['ledger']).read_bytes())
        self.assertEqual(len(ledger['abandonments']), 1)

    def test_tampered_terminal_ledger_fails_closed(self):
        receipt = self.run_quarantine()
        path = Path(receipt['ledger'])
        ledger = json.loads(path.read_bytes())
        ledger['abandonments'][0]['replay_authorized'] = True
        path.write_text(json.dumps(ledger))
        with self.assertRaises(ValueError):
            workflowctl.shared_discovery.inspect_claim(self.discovery, '测试查询', '量子')

    def test_user_pause_is_preserved(self):
        state = json.loads(self.state.read_bytes())
        state['collection_paused'] = True
        self.state.write_text(json.dumps(state))
        with self.assertRaises(ValueError):
            self.run_quarantine()


if __name__ == '__main__':
    unittest.main()
