"""Isolated stop-ledger and explicit main-adoption safety checks."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import recoveryctl as recovery


OWNER = '00000000-0000-0000-0000-000000000011'
PRIOR = '00000000-0000-0000-0000-000000000012'
HOST = '00000000-0000-0000-0000-000000000013'


class RecoveryIdentitySafety(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.main = self.root / 'main.json'
        self.policy = self.root / 'policy.json'
        self.state = self.root / 'coordination.json'
        self.ledger = self.root / 'recovery.json'
        self.slots = {f'{n:02d}': {'owner': f'00000000-0000-0000-0000-{n:012d}',
                                  'work': str(self.root / f'work/{n}')} for n in range(1, 5)}
        recovery.save_json(self.main, {'main_thread': PRIOR, 'host_parent_thread': HOST,
                                      'paused': True, 'slots': self.slots})
        recovery.save_json(self.policy, {'main_thread': PRIOR, 'host_parent_thread': HOST})
        recovery.save_json(self.state, {'version': 1, 'collection_paused': True})
        recovery.save_json(self.ledger, {'version': 1, 'stopped': True, 'stop_complete': True,
            'cycle_started_at': '2026-01-01T00:00:00Z',
            'incidents': {'01': {'first_failed_at': '2026-01-01T00:00:00Z', 'attempts': 9}}})
        self.patch = patch.multiple(recovery, PROJECT=self.root, MAIN=self.main,
            POLICY=self.policy, STATE=self.state, RECOVERY=self.ledger)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.env = patch.dict(os.environ, {'CODEX_THREAD_ID': OWNER})
        self.env.start()
        self.addCleanup(self.env.stop)
        outer = self

        class RPC:
            goal = None

            def __enter__(inner):
                return inner

            def __exit__(inner, *args):
                pass

            def call(inner, method, args):
                if method == 'thread/read':
                    return {'thread': {'id': args['threadId'], 'cwd': str(outer.root)}}
                if method == 'thread/goal/get':
                    return {'goal': inner.goal}
                if method == 'thread/goal/set':
                    return {}  # Acceptance must not be mistaken for actual resumption.
                raise AssertionError(method)

        self.rpc_type = RPC
        self.native = patch('app_server_control.RPC', RPC)
        self.native.start()
        self.addCleanup(self.native.stop)

    def test_adoption_rejects_another_thread_and_absent_authorization(self):
        for owner, authorized in [(PRIOR, True), (OWNER, False)]:
            with self.assertRaises(ValueError):
                recovery.adopt_main(caller_thread=owner, request_id='user-adopt', authorized=authorized)
        self.assertEqual(json.loads(self.main.read_text())['main_thread'], PRIOR)

    def test_explicit_adoption_and_resume_preserve_original_slots_and_old_faults(self):
        recovery.adopt_main(caller_thread=OWNER, request_id='user-adopt', authorized=True)
        self.assertTrue(json.loads(self.state.read_text())['collection_paused'])
        result = recovery.resume_project(caller_thread=OWNER, request_id='user-resume', authorized=True)
        self.assertIsNone(result['native_goal_status'])
        self.assertEqual(json.loads(self.main.read_text())['slots'], self.slots)
        ledger = json.loads(self.ledger.read_text())
        self.assertEqual(ledger['previous_cycles'][0]['state']['incidents']['01']['attempts'], 9)
        self.assertFalse(json.loads(self.state.read_text())['collection_paused'])

    def test_failed_native_goal_resume_does_not_open_gate_or_reset_incidents(self):
        recovery.adopt_main(caller_thread=OWNER, request_id='user-adopt', authorized=True)
        self.rpc_type.goal = {'status': 'paused'}
        with self.assertRaises(ValueError):
            recovery.resume_project(caller_thread=OWNER, request_id='user-resume', authorized=True)
        self.assertTrue(json.loads(self.state.read_text())['collection_paused'])
        ledger = json.loads(self.ledger.read_text())
        self.assertTrue(ledger['stopped'])
        self.assertEqual(ledger['incidents']['01']['attempts'], 9)


if __name__ == '__main__':
    unittest.main()
