import contextlib
import io
import unittest
from unittest.mock import patch

import production_round_publish as publisher


class PublisherCliTests(unittest.TestCase):
    def test_help_unknown_args_and_missing_authorization_cannot_publish(self):
        for arguments, code in [(['--help'], 0), (['--unknown'], 2), ([], 2)]:
            with self.subTest(arguments=arguments), patch.object(publisher, 'publish') as publish, \
                    patch.object(publisher, 'WorkerLock') as lock, \
                    patch.object(publisher.Path, 'read_text') as read, \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as raised:
                    publisher.main(arguments)
                self.assertEqual(raised.exception.code, code)
                publish.assert_not_called()
                lock.assert_not_called()
                read.assert_not_called()

    def test_authorized_main_agent_uses_existing_ledger_and_writer_lock(self):
        with patch.object(publisher, 'publish', return_value={'ok': True}) as publish, \
                patch.object(publisher, 'WorkerLock') as lock, \
                patch.object(publisher.Path, 'read_text', return_value='{"rounds": []}'), \
                contextlib.redirect_stdout(io.StringIO()):
            publisher.main(['--authorized'])
            lock.return_value.__enter__.assert_called_once()
            publish.assert_called_once_with({'rounds': []})


if __name__ == '__main__':
    unittest.main()
