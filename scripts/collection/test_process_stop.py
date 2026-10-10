"""Safety checks for destructive process stopping; no collection or DB writes."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

import process_stop as stop


MAIN = '00000000-0000-0000-0000-000000000001'
HOST = '00000000-0000-0000-0000-000000000002'
OWNERS = [f'00000000-0000-0000-0000-{n:012d}' for n in range(3, 7)]


class ProcessStopSafety(unittest.TestCase):
    def setUp(self):
        self.project = Path(__file__).resolve().parents[2]
        self.config = {'main_thread': MAIN, 'host_parent_thread': HOST,
                       'stop_authorized': True, 'force_process_stop_authorized': True}
        self.main = {'main_thread': MAIN, 'host_parent_thread': HOST, 'paused': True,
            'slots': {f'{n:02d}': {'owner': owner, 'work': str(self.project / f'work/{n}')}
                      for n, owner in enumerate(OWNERS, 1)}}
        self.coordination = {'collection_paused': True, 'threads': {
            owner: {'role': 'collector', 'work': binding['work']}
            for owner, binding in ((b['owner'], b) for b in self.main['slots'].values())}}
        self.recovery = {'stopped': True}

    def test_authorization_and_both_gates_are_required(self):
        for target, key in [(self.config, 'force_process_stop_authorized'),
                            (self.main, 'paused'), (self.coordination, 'collection_paused'),
                            (self.recovery, 'stopped')]:
            with patch.dict(target, {key: False}), self.assertRaises(ValueError):
                stop.targets_for(self.config, self.main, self.coordination, self.recovery, self.project)

    def test_binding_mismatch_is_rejected(self):
        self.coordination['threads'][OWNERS[0]]['work'] = str(self.project.parent)
        with self.assertRaises(ValueError):
            stop.targets_for(self.config, self.main, self.coordination, self.recovery, self.project)

    def test_shared_app_server_is_never_a_resume_target(self):
        owners = {MAIN, HOST, *OWNERS}
        for argv in [['codex.exe', 'app-server', '--thread', MAIN],
                     ['codex.exe', 'exec', 'app-server', 'resume', MAIN],
                     ['codex.exe', 'resume', 'unregistered'],
                     ['python.exe', '-c', f'print("resume {MAIN}")']]:
            self.assertIsNone(stop.dedicated_owner(argv, argv[0], owners, self.project))
        self.assertEqual(stop.dedicated_owner(['codex.exe', 'resume', MAIN],
            'codex.exe', owners, self.project), MAIN)

    def test_python_requires_absolute_approved_entry_and_exact_owner(self):
        entry = str(self.project / 'scripts/collection/workflowctl.py')
        self.assertEqual(stop.dedicated_owner(['python.exe', entry, '--thread', OWNERS[0]],
            'python.exe', set(OWNERS), self.project), OWNERS[0])
        for entry in ['scripts/collection/workflowctl.py', str(self.project / 'web/server.py')]:
            self.assertIsNone(stop.dedicated_owner(['python.exe', entry, '--thread', OWNERS[0]],
                'python.exe', set(OWNERS), self.project))

    def test_control_thread_web_database_and_unknown_cwd_are_preserved(self):
        for command in ['node web/server.mjs', 'docker start database', 'python other/workflowctl.py']:
            self.assertFalse(stop.collection_terminal({'cwd': str(self.project), 'command': command},
                                                     None, self.project))
        self.assertFalse(stop.collection_terminal({'cwd': str(self.project.parent),
            'command': 'python scripts/collection/workflowctl.py'}, None, self.project))
        self.assertTrue(stop.collection_terminal({'cwd': str(self.project),
            'command': 'python scripts/collection/workflowctl.py current'}, None, self.project))

    def test_native_terminal_removal_requires_actual_absence(self):
        # API acceptance alone must not be reported as successful termination.
        class RPC:
            def call(inner, method, args):
                owner = args['threadId']
                if method == 'thread/read':
                    return {'thread': {'id': owner, 'cwd': str(self.project),
                            'parentThreadId': HOST, 'status': {'type': 'idle'}}}
                if method == 'thread/backgroundTerminals/terminate':
                    return {'terminated': True}
                return {'data': [{'processId': 'still-running', 'cwd': str(self.project),
                                 'command': 'python scripts/collection/workflowctl.py current'}]}
        with patch.object(stop, 'process_snapshot', return_value=[]):
            result = stop.stop_owned_processes(self.config, self.main, self.coordination,
                                              self.recovery, self.project, RPC())
        self.assertFalse(result['ok'])
        self.assertEqual(result['thread_terminals'][OWNERS[0]]['remaining'], ['still-running'])

    @unittest.skipUnless(os.name == 'nt', 'Windows handle and creation-time verification')
    def test_pid_reuse_and_image_mismatch_cannot_terminate_a_live_process(self):
        process = subprocess.Popen([sys.executable, '-c', 'import time;time.sleep(120)'],
                                   creationflags=subprocess.CREATE_NO_WINDOW)
        try:
            row = next(r for r in stop.process_snapshot() if r['ProcessId'] == process.pid)
            with self.assertRaises(ValueError):
                stop.terminate_exact({**row, 'CreationDate': '/Date(0)/'})
            self.assertIsNone(process.poll())
            with self.assertRaises(ValueError):
                stop.terminate_exact({**row, 'ExecutablePath': str(self.project / 'wrong.exe')})
            self.assertIsNone(process.poll())
            self.assertTrue(stop.terminate_exact(row)['exit_verified'])
            process.wait(timeout=3)
        finally:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=3)


if __name__ == '__main__':
    unittest.main()
