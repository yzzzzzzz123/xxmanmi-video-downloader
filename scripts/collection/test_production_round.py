import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from production_round import accept_receipt, accumulate, checksum, exclude_content, freeze, freeze_early, initialize, package, valid_release_size
from production_sync import sync_once


class RoundGateTests(unittest.TestCase):
    def test_package_refuses_subthreshold_without_reading_database(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(299))
        with mock.patch('production_round.psycopg.connect') as database:
            with self.assertRaisesRegex(ValueError, 'Fewer than 300'):
                package(ledger)
            database.assert_not_called()

    def test_restart_preserves_pending_and_rejects_replacing_baseline(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = Path(directory)
            baseline = folder / 'baseline.json'
            payload = {'captured_baseline': [{'id': 1, 'source_url': 'old'}],
                       'captured_count': 1, 'production_captured_count': 1,
                       'production_identity_baseline_verified': True,
                       'resumed_at': '2026-09-30T07:22:19Z'}
            baseline.write_text(json.dumps(payload), encoding='utf-8')
            with mock.patch('production_round.LEDGER', folder / 'ledger.json'):
                ledger = initialize(baseline)
                accumulate(ledger, self.rows(99))
                (folder / 'ledger.json').write_text(json.dumps(ledger), encoding='utf-8')
                self.assertEqual(len(initialize(baseline)['first_success']), 99)
                payload['resumed_at'] = '2026-10-01T00:00:00Z'
                baseline.write_text(json.dumps(payload), encoding='utf-8')
                with self.assertRaisesRegex(ValueError, 'never reset'):
                    initialize(baseline)

    def test_legacy_publisher_cannot_bypass_round_gate(self):
        with mock.patch('production_sync.run_command') as transport:
            with self.assertRaisesRegex(RuntimeError, 'Per-batch production publishing is retired'):
                sync_once(100)
            transport.assert_not_called()

    def ledger(self):
        return {"baseline": [{"id": 1, "source_url": "old"}], "first_success": {}, "rounds": []}

    def rows(self, count):
        return [{"program_id": i + 2, "source_url": f"new-{i}", "batch_id": f"batch-{i:06d}",
                 "acknowledged_at": "2026-09-30T07:40:00Z", "cover_sha256": "a" * 64}
                for i in range(count)]

    def test_history_duplicates_and_subthreshold_do_not_publish(self):
        ledger = self.ledger()
        rows = self.rows(99)
        accumulate(ledger, rows + rows + [{**rows[0], "program_id": 1, "source_url": "old"}])
        self.assertEqual(len(ledger["first_success"]), 99)
        self.assertIsNone(freeze(ledger))

    def test_explicit_early_freeze_requires_complete_pending_set(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(3))
        with self.assertRaisesRegex(ValueError, 'complete current pending'):
            freeze_early(ledger, 2, 'user requested early release')
        release = freeze_early(ledger, 3, 'user requested early release')
        self.assertEqual(release['release_mode'], 'user_authorized_early')
        self.assertEqual(len(release['programmes']), 3)
        self.assertEqual(freeze(ledger), release)

    def test_exact_three_hundred_and_retry_never_mix_new_items(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(303))
        first = copy.deepcopy(freeze(ledger))
        self.assertEqual(len(first["programmes"]), 300)
        self.assertEqual(first['programme_threshold'], 300)
        accumulate(ledger, self.rows(605))
        self.assertEqual(freeze(ledger), first)
        ledger["rounds"][0]["state"] = "verified"
        second = freeze(ledger)
        self.assertEqual(len(second["programmes"]), 300)
        self.assertTrue({row["source_url"] for row in first["programmes"]}.isdisjoint(
            row["source_url"] for row in second["programmes"]))

    def test_identity_conflict_and_manifest_mutation_are_rejected(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(300))
        with self.assertRaises(ValueError):
            accumulate(ledger, [{**self.rows(1)[0], "source_url": "conflict"}])
        freeze(ledger)["programmes"][0]["source_url"] = "changed"
        with self.assertRaises(ValueError):
            freeze(ledger)

    def exclusion(self, release):
        return {'version': 1, 'release_id': release['release_id'],
                'boundary': 'unverified_sexual_content_version', 'remote_write_started': False,
                'program_ids': [release['programmes'][0]['program_id']],
                'basis': 'Resource version has not been verified against non-explicit edition',
                'metadata_source': 'https://example.test/official-editions',
                'checked_at': '2026-10-03T04:25:00Z'}

    def test_content_exclusion_preserves_old_manifest_and_replaces_with_exact_hundred(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(304))
        release = freeze(ledger)
        before = copy.deepcopy(release)
        exclude_content(ledger, self.exclusion(release))
        self.assertEqual(release['programmes'], before['programmes'])
        self.assertEqual(release['manifest_sha256'], before['manifest_sha256'])
        self.assertEqual(len(ledger['first_success']), 304)
        replacement = freeze(ledger)
        self.assertEqual(len(replacement['programmes']), 300)
        self.assertNotEqual(replacement['release_id'], release['release_id'])
        self.assertNotIn(before['programmes'][0]['source_url'],
                         {r['source_url'] for r in replacement['programmes']})
        with self.assertRaisesRegex(ValueError, 'cannot be accepted'):
            accept_receipt(ledger, self.receipt(release))

    def test_content_exclusion_never_bypasses_threshold_or_cancels_attempted_release(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(300))
        release = freeze(ledger)
        release['publish_started_at'] = '2026-10-03T04:24:00Z'
        with self.assertRaisesRegex(ValueError, 'unattempted'):
            exclude_content(ledger, self.exclusion(release))
        del release['publish_started_at']
        bad = self.exclusion(release)
        bad['program_ids'] = [99999]
        with self.assertRaisesRegex(ValueError, 'identify programmes'):
            exclude_content(ledger, bad)
        exclude_content(ledger, self.exclusion(release))
        self.assertIsNone(freeze(ledger))

    def receipt(self, release):
        return {'ok': True, 'verified': True, 'release_id': release['release_id'],
                'bundle_sha256': 'b' * 64,
                'identity_manifest_sha256': release['manifest_sha256'],
                'package_manifest_sha256': 'c' * 64, 'resource_rows': len(release['programmes']),
                'target': 'ubuntu@43.134.92.99', 'database': 'manying',
                'verified_at': '2026-09-30T10:00:00Z',
                'database_backup': '/opt/manying/backups/example/manying.dump',
                'application_backup': '/opt/manying/backups/example/app.tar.gz',
                'checks': {name: True for name in (
                    'database_backup_readable', 'application_backup_readable',
                    'stable_programme_identities', 'resource_identities', 'cover_hashes',
                    'health_api', 'programme_and_playback_api', 'user_data_protected')},
                'programmes': [{'program_id': row['program_id'],
                                'source_url': row['source_url'],
                                'cover_sha256': row['cover_sha256']}
                               for row in release['programmes']]}

    def prepared_release(self, ledger):
        accumulate(ledger, self.rows(305))
        release = freeze(ledger)
        release.update(bundle_sha256='b' * 64, package_manifest_sha256='c' * 64,
                       resource_rows=300)
        return release

    def test_legacy_frozen_hundred_is_unchanged_then_new_round_uses_three_hundred(self):
        ledger = self.ledger()
        accumulate(ledger, self.rows(405))
        rows = self.rows(100)
        old = {'release_id': 'round-000001-legacy', 'state': 'frozen',
               'programmes': rows, 'manifest_sha256': checksum(rows),
               'bundle_sha256': 'b' * 64, 'package_manifest_sha256': 'c' * 64,
               'resource_rows': 100, 'receipt': None}
        ledger['rounds'].append(old)
        before = copy.deepcopy(old)
        self.assertEqual(freeze(ledger), before)
        self.assertTrue(valid_release_size(old))
        accept_receipt(ledger, self.receipt(old))
        self.assertEqual(old['programmes'], before['programmes'])
        new = freeze(ledger)
        self.assertEqual(len(new['programmes']), 300)
        self.assertEqual(new['programme_threshold'], 300)
        self.assertTrue({row['source_url'] for row in rows}.isdisjoint(
            row['source_url'] for row in new['programmes']))

    def test_100_and_299_new_items_do_not_freeze(self):
        for count in (100, 299):
            ledger = self.ledger()
            accumulate(ledger, self.rows(count))
            self.assertIsNone(freeze(ledger))
        ledger = self.ledger()
        accumulate(ledger, self.rows(300))
        self.assertEqual(len(freeze(ledger)['programmes']), 300)

    def test_partial_or_mismatched_receipt_never_consumes_pending_programmes(self):
        ledger = self.ledger()
        release = self.prepared_release(ledger)
        valid = self.receipt(release)
        cases = []
        partial = copy.deepcopy(valid)
        partial['programmes'].pop()
        cases.append(partial)
        wrong_cover = copy.deepcopy(valid)
        wrong_cover['programmes'][0]['cover_sha256'] = 'd' * 64
        cases.append(wrong_cover)
        missing_backup = copy.deepcopy(valid)
        missing_backup['checks']['database_backup_readable'] = False
        cases.append(missing_backup)
        wrong_package = copy.deepcopy(valid)
        wrong_package['bundle_sha256'] = 'e' * 64
        cases.append(wrong_package)
        for receipt in cases:
            with self.subTest(receipt=receipt):
                before = copy.deepcopy(ledger)
                with self.assertRaises(ValueError):
                    accept_receipt(ledger, receipt)
                self.assertEqual(ledger, before)

    def test_complete_receipt_is_idempotent_and_leaves_extra_programmes_pending(self):
        ledger = self.ledger()
        release = self.prepared_release(ledger)
        receipt = self.receipt(release)
        accept_receipt(ledger, receipt)
        self.assertEqual(release['state'], 'verified')
        self.assertIsNone(freeze(ledger))
        before = copy.deepcopy(ledger)
        accept_receipt(ledger, receipt)
        self.assertEqual(ledger, before)
        receipt['resource_rows'] += 1
        with self.assertRaisesRegex(ValueError, 'receipt changed'):
            accept_receipt(ledger, receipt)


if __name__ == "__main__":
    unittest.main()
