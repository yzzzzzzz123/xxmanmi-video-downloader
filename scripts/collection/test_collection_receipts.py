import hashlib
import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from collection_receipts import cover_receipt_bytes


class ReceiptArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = Path(self.temporary.name)
        self.work = self.project / 'Datebase/xxmanmi-resource-db/.runtime/collection-workers/owner-a'
        self.work.mkdir(parents=True)
        self.batch_id = 'batch-000001'
        self.path = self.work / f'{self.batch_id}-covers.json'
        self.data = b'{"batch_id":"batch-000001","complete":true}'
        self.path.write_bytes(self.data)
        self.batch = {'thread': 'owner-a', 'work': str(self.work), 'verification': {
            'work': str(self.work), 'cover_receipt': str(self.path),
            'cover_receipt_sha256': hashlib.sha256(self.data).hexdigest()}}
        self.cleanup = self.project / 'Datebase/xxmanmi-resource-db/.runtime/cleanup'
        self.index = self.cleanup / 'work-owner-a.json'

    def archive(self, data=None):
        archive = self.cleanup / 'archives/owner-a-proof.zip'
        archive.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr(self.path.name, self.data if data is None else data)
        self.descriptor = {'owner': 'owner-a', 'original_work': str(self.work),
                           'archive': str(archive), 'verified': True, 'removed': True,
                           'files': [{'name': self.path.name, 'size': len(self.data),
                                      'sha256': hashlib.sha256(self.data).hexdigest()}]}
        self.write_index()
        self.path.unlink()
        return archive

    def write_index(self):
        self.index.write_text(json.dumps(self.descriptor), 'utf-8')

    def read(self):
        return cover_receipt_bytes(self.project, self.batch_id, self.batch)

    def test_live_receipt_still_requires_original_bound_hash(self):
        self.assertEqual(self.read(), self.data)
        self.path.write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'bound verification hash'):
            self.read()

    def test_archived_receipt_is_read_without_recreating_retired_work(self):
        self.archive()
        self.assertEqual(self.read(), self.data)
        self.assertFalse(self.path.exists())

    def test_missing_or_invalid_receipt_hash_is_rejected(self):
        for digest in [None, 'wrong', '0' * 64]:
            with self.subTest(digest=digest):
                self.batch['verification']['cover_receipt_sha256'] = digest
                with self.assertRaises(ValueError):
                    self.read()

    def test_changed_live_receipt_cannot_fall_back_to_archive(self):
        self.archive()
        self.path.write_bytes(b'changed')
        with self.assertRaises(ValueError):
            self.read()

    def test_wrong_owner_work_or_unverified_archive_is_rejected(self):
        self.archive()
        original = dict(self.descriptor)
        for field, value in [('owner', 'other'), ('original_work', str(self.project)),
                             ('verified', False), ('removed', False)]:
            with self.subTest(field=field):
                self.descriptor = {**original, field: value}
                self.write_index()
                with self.assertRaises(ValueError):
                    self.read()

    def test_archive_member_bytes_size_and_index_hash_must_match(self):
        archive = self.archive(data=b'changed')
        with self.assertRaises(ValueError):
            self.read()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr(self.path.name, b'x' * len(self.data))
        with self.assertRaisesRegex(ValueError, 'bound verification hash'):
            self.read()
        self.descriptor['files'][0]['sha256'] = '0' * 64
        self.write_index()
        with self.assertRaisesRegex(ValueError, 'index differs'):
            self.read()

    def test_archive_path_escape_and_wrong_member_name_are_rejected(self):
        archive = self.archive()
        self.descriptor['archive'] = str(self.project / archive.name)
        self.write_index()
        with self.assertRaisesRegex(ValueError, 'approved directory'):
            self.read()
        self.descriptor['archive'] = str(archive)
        self.write_index()
        with zipfile.ZipFile(archive, 'w') as bundle:
            bundle.writestr('../' + self.path.name, self.data)
        with self.assertRaisesRegex(ValueError, 'member is invalid'):
            self.read()

    def test_receipt_must_be_exact_file_in_bound_worker_directory(self):
        other = self.project / self.path.name
        other.write_bytes(self.data)
        self.batch['verification']['cover_receipt'] = str(other)
        with self.assertRaisesRegex(ValueError, 'bound collection workspace'):
            self.read()


if __name__ == '__main__':
    unittest.main()
