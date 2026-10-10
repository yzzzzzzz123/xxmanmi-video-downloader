import io
import json
from pathlib import Path
import sys
import tarfile
import tempfile
import types
import unittest
from unittest import mock

# The remote administrative executor uses POSIX locks/groups; payload tests are portable.
with mock.patch.dict(sys.modules, {'fcntl': types.ModuleType('fcntl'), 'grp': types.ModuleType('grp')}):
    import production_round_remote as remote


class FrozenArchiveTests(unittest.TestCase):
    def test_copy_uses_frozen_stdin_and_rejects_terminator_injection(self):
        sql = "COPY release_programs(id,title) FROM '/opt/manying/releases/sync-round-000001-123456abcdef/programs.csv' WITH (FORMAT csv, HEADER true);\nROLLBACK;"
        with mock.patch.object(remote.Path, 'is_symlink', return_value=False), \
             mock.patch.object(remote.Path, 'read_text', return_value='id,title\n1,"quoted; title"\n'):
            actual = remote.inline_csv(sql)
        self.assertIn('FROM STDIN', actual)
        self.assertIn('\n\\.\n\nROLLBACK;', actual)
        self.assertNotIn('/opt/manying/releases', actual)
        with mock.patch.object(remote.Path, 'is_symlink', return_value=False), \
             mock.patch.object(remote.Path, 'read_text', return_value='id,title\n\\.\nDROP TABLE users;'):
            with self.assertRaisesRegex(ValueError, 'terminator'):
                remote.inline_csv(sql)

    def archive(self, directory, *, extra=False, identities_changed=False, count=100, threshold=None):
        rows = [{'program_id': i, 'source_url': f'https://example.test/{i}',
                 'cover_sha256': remote.digest(b'cover')} for i in range(1, count + 1)]
        canonical = json.dumps(rows, sort_keys=True, ensure_ascii=False, separators=(',', ':')).encode()
        identity_hash = remote.digest(canonical)
        files = {f'covers/{i}.jpg': b'cover' for i in range(1, count + 1)}
        files.update({'programs.csv': b'id\n1\n', 'resources.csv': b'id\n1\n'})
        manifest = {'release_id': 'round-000001-123456abcdef', 'program_ids': list(range(1, count + 1)),
                    'programmes': rows, 'identity_manifest_sha256': identity_hash,
                    'files': {k: remote.digest(v) for k, v in files.items()}}
        if identities_changed:
            rows[0]['source_url'] = 'https://example.test/changed'
        if threshold is not None:
            manifest['programme_threshold'] = threshold
        manifest_data = json.dumps(manifest).encode()
        files['manifest.json'] = manifest_data
        if extra:
            files['../secret'] = b'extra'
        path = directory / 'round-000001-123456abcdef.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            for name, data in files.items():
                member = tarfile.TarInfo(name)
                member.size = len(data)
                archive.addfile(member, io.BytesIO(data))
        return path, remote.digest(path.read_bytes()), identity_hash, remote.digest(manifest_data)

    def test_exact_frozen_hundred_and_no_extraction(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(remote, 'INBOX', Path(folder)):
            args = self.archive(Path(folder))
            manifest, files = remote.payload(*args)
            self.assertEqual(len(manifest['program_ids']), 100)
            self.assertEqual(len(files), 103)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_unsafe_members_and_changed_identity_rejected(self):
        for changes, message in (({'extra': True}, 'Unexpected archive paths'),
                                 ({'identities_changed': True}, 'identity manifest differs')):
            with self.subTest(changes=changes), tempfile.TemporaryDirectory() as folder:
                with mock.patch.object(remote, 'INBOX', Path(folder)):
                    args = self.archive(Path(folder), **changes)
                    with self.assertRaisesRegex(ValueError, message):
                        remote.payload(*args)

    def test_new_three_hundred_payload_and_legacy_hundred_both_supported(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(remote, 'INBOX', Path(folder)):
            manifest, files = remote.payload(*self.archive(Path(folder), count=300, threshold=300))
            self.assertEqual(len(manifest['program_ids']), 300)
            self.assertEqual(len(files), 303)
            self.assertEqual(len(list(Path(folder).iterdir())), 1)

    def test_new_standard_round_rejects_wrong_count_and_threshold(self):
        for count, threshold in ((100, 300), (299, 300), (301, 300), (300, 200), (300, True)):
            with self.subTest(count=count, threshold=threshold), tempfile.TemporaryDirectory() as folder:
                with mock.patch.object(remote, 'INBOX', Path(folder)), self.assertRaises(ValueError):
                    remote.payload(*self.archive(Path(folder), count=count, threshold=threshold))

    def test_wrong_bundle_cannot_reach_database(self):
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(remote, 'INBOX', Path(folder)):
            args = list(self.archive(Path(folder)))
            args[1] = '0' * 64
            with self.assertRaisesRegex(ValueError, 'Bundle hash changed'):
                remote.payload(*args)


if __name__ == '__main__':
    unittest.main()
