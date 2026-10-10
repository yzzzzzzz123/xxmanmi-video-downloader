"""Isolated supervision checks: no live queue, database or production access."""
import json
from pathlib import Path
import runpy
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import operationsctl as operations


class OperationsTests(unittest.TestCase):
    def setUp(self):
        self.sync = SimpleNamespace(
            status=Mock(return_value={'verified_events': 12, 'verified_releases': 3,
                'last_verified_at': '2026-10-08T00:00:00Z', 'pending': 2,
                'executor_live': True}),
            reconcile_dirty=Mock(), kick=Mock(return_value={'started': False}))
        fake_modules = patch.dict(sys.modules, {'production_live': self.sync})
        fake_modules.start()
        self.addCleanup(fake_modules.stop)
        self.current = Mock()
        self.current.read_text.return_value = json.dumps({
            'slots': {'01': {'owner': 'existing-owner'}},
            'main_discovery_checkpoint': {'round': 'historic-round'},
            'current_automation': {'keep': 'existing-field'}})
        self.recovery = operations.recoveryctl
        patched = patch.multiple(self.recovery,
            MAIN=self.current,
            policy=Mock(return_value={'main_thread': 'main-owner'}),
            check=Mock(return_value={'stop_required': False, 'incidents': []}),
            stop_project=Mock(return_value={'ok': True}), save_json=Mock())
        patched.start()
        self.addCleanup(patched.stop)
        lock = patch.object(self.recovery.coordctl, 'StateLock')
        self.lock = lock.start()
        self.addCleanup(lock.stop)

    def test_checkpoint_updates_live_sync_and_preserves_history_and_owners(self):
        self.assertTrue(operations.checkpoint('main-owner')['atomic_checkpoint'])
        self.lock.assert_called_once_with(self.current, timeout=5)
        path, saved = self.recovery.save_json.call_args.args
        self.assertIs(path, self.current)
        self.assertEqual(saved['slots'], {'01': {'owner': 'existing-owner'}})
        self.assertEqual(saved['main_discovery_checkpoint'], {'round': 'historic-round'})
        self.assertEqual(saved['current_automation']['keep'], 'existing-field')
        self.assertEqual(saved['current_automation']['actual_verified_event_versions'], 12)
        self.assertEqual(saved['current_automation']['sync_pending'], 2)
        self.assertEqual(saved['current_automation']['sync_mode'], 'event_delta')
        self.assertEqual(saved['hourly_report_seconds'], 0)
        self.assertIsNone(saved['next_report_at'])
        self.sync.kick.assert_not_called()

    def test_foreign_owner_cannot_checkpoint_or_pulse(self):
        for action in (operations.checkpoint, operations.pulse):
            with self.assertRaises(ValueError):
                action('foreign-owner')
        self.lock.assert_not_called()
        self.recovery.check.assert_not_called()
        self.recovery.save_json.assert_not_called()
        self.sync.status.assert_not_called()

    def test_normal_pulse_preserves_event_sync_and_silent_recovery(self):
        result = operations.pulse('main-owner')
        self.assertEqual(result['status'], 'running')
        self.assertFalse(result['report'])
        self.assertEqual(result['sync_pending'], 2)
        self.sync.reconcile_dirty.assert_called_once_with()
        self.sync.kick.assert_called_once_with()
        self.recovery.stop_project.assert_not_called()

    def test_terminal_recovery_stops_without_starting_sync(self):
        self.recovery.check.return_value = {'stop_required': True}
        self.assertEqual(operations.pulse('main-owner')['status'], 'stopped')
        self.recovery.stop_project.assert_called_once_with(caller_thread='main-owner')
        self.sync.reconcile_dirty.assert_not_called()
        self.sync.kick.assert_not_called()

    def test_retired_discovery_options_are_rejected_without_state_writes(self):
        for option in ('--discovery-round', '--browser-tab'):
            with self.subTest(option=option), patch.object(sys, 'argv', [
                    'operationsctl.py', 'checkpoint', '--thread', 'main-owner', option, 'old']), \
                    patch.object(sys, 'stderr'), self.assertRaises(SystemExit) as stopped:
                runpy.run_path(str(Path(operations.__file__)), run_name='__main__')
            self.assertEqual(stopped.exception.code, 2)
        self.recovery.save_json.assert_not_called()
        self.sync.status.assert_not_called()


if __name__ == '__main__':
    unittest.main()
