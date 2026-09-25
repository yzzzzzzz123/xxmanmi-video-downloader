import copy
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('download', Path(__file__).with_name('download.py'))
d = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(d)

PAYLOAD = {'pages': [{'page_url': 'https://www.xxmanmi.com/video2/119740.html', 'title': '百家讲坛',
                      'episodes': [{'label': '20251202', 'line': '量子', 'url': 'https://example.com/a.m3u8'}]}]}


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)
        self.state = d.new_state()
        d.ingest(self.state, copy.deepcopy(PAYLOAD))
        self.entry = next(iter(self.state['entries'].values()))

    def test_default_site_merge_and_fallback(self):
        other = copy.deepcopy(PAYLOAD)
        other['pages'][0]['episodes'][0].update(line='非凡', url='https://example.com/b.m3u8')
        d.ingest(self.state, other)
        d.ingest(self.state, other)
        self.assertEqual(self.state['scope']['mode'], 'site')
        self.assertEqual(len(self.state['entries']), 1)
        self.assertEqual(len(self.entry['sources']), 2)
        self.assertEqual(self.entry['sources'][0]['line'], '量子')

    def test_specified_scope_is_explicit_and_preserves_catalog(self):
        with self.assertRaises(ValueError):
            d.ingest(self.state, {'scope': {'mode': 'specified'}})
        payload = {'scope': {'mode': 'specified', 'selection': 'only this program'}}
        with self.assertRaises(d.Conflict):
            d.ingest(self.state, payload)
        d.ingest(self.state, payload, reset=True)
        self.assertEqual(self.state['active'], [])
        self.assertEqual(len(self.state['entries']), 1)

    def test_coverage_cannot_finish_with_pending_or_failed(self):
        page = PAYLOAD['pages'][0]['page_url']
        d.ingest(self.state, {'coverage': {'discovered': [page], 'exhausted': True}})
        self.assertFalse(self.state['coverage']['exhausted'])
        d.ingest(self.state, {'coverage': {'visited': [page], 'failed': {page: 'challenge'}, 'exhausted': True}})
        self.assertFalse(self.state['coverage']['exhausted'])
        d.ingest(self.state, {})
        self.assertIn(page, self.state['coverage']['failed'])
        d.ingest(self.state, {'coverage': {'visited': [page], 'resolved': [page], 'exhausted': True}})
        self.assertTrue(self.state['coverage']['exhausted'])
        self.assertFalse(d.summary(self.state)['scope_complete'])

    def test_atomic_manifest_and_corruption_preserved(self):
        d.save(self.root, self.state)
        self.assertEqual(d.load_state(self.root)['active'], self.state['active'])
        with patch.object(d.os, 'replace', side_effect=OSError('interrupted')):
            with self.assertRaises(OSError):
                d.save(self.root, d.new_state())
        self.assertEqual(d.load_state(self.root)['active'], self.state['active'])

    def test_paths_symlinks_and_url_validation(self):
        for value in ['../outside', '/tmp/file']:
            with self.assertRaises(d.Conflict):
                d.safe_path(self.root, value)
        (self.root / 'escape').symlink_to('/tmp', target_is_directory=True)
        with self.assertRaises(d.Conflict):
            d.safe_path(self.root, 'escape/out.mp4')
        self.assertNotIn('/', d.slug('../a/b'))
        for value in ['file:///etc/passwd', 'https://u:p@example.com/a', 'https://example.com/a\n']:
            with self.assertRaises(ValueError):
                d.url(value)
        with self.assertRaises(ValueError):
            d.public_url('http://127.0.0.1/a')

    def test_single_writer(self):
        with d.writer(self.root):
            with self.assertRaises(d.Conflict):
                with d.writer(self.root):
                    pass

    def test_low_disk_pauses_before_network_or_process(self):
        with patch.object(d, 'space', side_effect=d.Paused('low disk')), patch.object(d, 'playlist') as remote:
            self.assertEqual(d.run(self.root, self.state, 'unused', d.GIB, 1), 2)
            remote.assert_not_called()
        self.assertEqual(d.load_state(self.root)['entries'][self.entry['id']]['status'], 'paused')

    def test_existing_file_is_not_overwritten(self):
        final = d.safe_path(self.root, self.entry['file'])
        final.parent.mkdir()
        with final.open('xb') as stream:
            stream.write(b'belongs to user')
        with self.assertRaises(d.Conflict):
            d.handle_entry(self.root, self.state, self.entry, 'unused', 1)
        self.assertEqual(final.read_bytes(), b'belongs to user')

    def test_completed_file_skipped_and_changed_file_preserved(self):
        final = d.safe_path(self.root, self.entry['file'])
        final.parent.mkdir()
        with final.open('xb') as stream:
            stream.write(b'fixture')
        self.entry.update(status='complete', verified={'size': 7, 'sha256': d.digest(final)})
        with patch.object(d, 'playlist') as remote:
            self.assertEqual(d.handle_entry(self.root, self.state, self.entry, 'unused', 1), 'skipped')
            remote.assert_not_called()
        self.entry['verified']['sha256'] = 'changed'
        with self.assertRaises(d.Conflict):
            d.handle_entry(self.root, self.state, self.entry, 'unused', 1)
        self.assertEqual(final.read_bytes(), b'fixture')

    def test_crash_after_promotion_recovers_without_network(self):
        final = d.safe_path(self.root, self.entry['file'])
        final.parent.mkdir()
        part = final.with_suffix('.part.mp4')
        with part.open('xb') as stream:
            stream.write(b'fixture')
        self.entry.update(status='promoting', owned_part=True,
                          verified={'size': 7, 'sha256': d.digest(part), 'full_decode': True})
        d.os.link(part, final)
        with patch.object(d, 'playlist') as remote:
            self.assertEqual(d.handle_entry(self.root, self.state, self.entry, 'unused', 1), 'skipped')
            remote.assert_not_called()
        self.assertTrue(self.state['first_decode_verified'])
        self.assertFalse(part.exists())
        self.assertEqual(self.entry['status'], 'complete')

    def test_retry_budget(self):
        with patch.object(d, 'playlist', side_effect=ValueError('bad remote')) as remote, patch.object(d.time, 'sleep'):
            self.assertEqual(d.handle_entry(self.root, self.state, self.entry, 'unused', 1), 'failed')
            self.assertEqual(remote.call_count, 3)
        self.assertEqual(self.entry['status'], 'failed')

    def test_proxy_bypass_is_process_only(self):
        with patch.dict(d.os.environ, {'HTTPS_PROXY': 'http://localhost:9999', 'http_proxy': 'http://localhost:9999'}):
            env = d.direct_env()
            self.assertNotIn('HTTPS_PROXY', env)
            self.assertNotIn('http_proxy', env)
            self.assertEqual(env['NO_PROXY'], '*')
            self.assertIn('HTTPS_PROXY', d.os.environ)

    def test_disk_drop_stops_running_process(self):
        with patch.object(d, 'space', side_effect=d.Paused('disk dropped')):
            with self.assertRaises(d.Paused):
                d.process([sys.executable, '-c', 'import time; time.sleep(30)'], self.root, 1, 'test')

    def test_unknown_partial_is_preserved(self):
        part = d.safe_path(self.root, self.entry['file'][:-4] + '.part.mp4')
        part.parent.mkdir()
        with part.open('xb') as stream:
            stream.write(b'unknown partial')
        with self.assertRaises(d.Conflict):
            d.handle_entry(self.root, self.state, self.entry, 'unused', 1)
        self.assertEqual(part.read_bytes(), b'unknown partial')

    def test_same_media_url_is_alias_not_second_download(self):
        final = d.safe_path(self.root, self.entry['file'])
        final.parent.mkdir()
        with final.open('xb') as stream:
            stream.write(b'fixture')
        self.entry.update(status='complete', verified={'size': 7, 'sha256': d.digest(final)})
        another = copy.deepcopy(PAYLOAD)
        another['pages'][0].update(page_url='https://www.xxmanmi.com/video2/1234.html', title='第二页')
        d.ingest(self.state, another)
        entry = self.state['entries'][self.state['active'][-1]]
        with patch.object(d, 'playlist') as remote:
            self.assertEqual(d.handle_entry(self.root, self.state, entry, 'unused', 1), 'duplicate')
            remote.assert_not_called()
        self.assertEqual(entry['duplicate_of'], self.entry['id'])

    def test_retry_failed_requires_flag(self):
        self.entry.update(status='failed')
        with patch.object(d, 'handle_entry') as handle:
            self.assertEqual(d.run(self.root, self.state, 'unused', 1, 0), 3)
            handle.assert_not_called()

    def test_cli_ingest_status_and_bad_input_leave_manifest_unchanged(self):
        command = [sys.executable, str(Path(d.__file__)), '--output', str(self.root)]
        good = subprocess.run(command + ['ingest'], input=json.dumps(PAYLOAD), capture_output=True, text=True)
        self.assertEqual(good.returncode, 0, good.stdout)
        before = (self.root / d.MANIFEST).read_bytes()
        bad = subprocess.run(command + ['ingest'], input='{"scope": {"mode": "wrong"}}', capture_output=True, text=True)
        self.assertEqual(bad.returncode, 1)
        self.assertEqual((self.root / d.MANIFEST).read_bytes(), before)
        status = subprocess.run(command + ['status'], capture_output=True, text=True)
        self.assertEqual(status.returncode, 0)
        self.assertEqual(json.loads(status.stdout)['scope']['mode'], 'site')


class PlaylistTests(unittest.TestCase):
    def check_body(self, body):
        return d.playlist('https://example.com/index.m3u8', fetch=lambda _: (body, 'https://example.com/index.m3u8'), check=d.url)

    def test_master_relative_segments_duration(self):
        mapping = {
            'https://example.com/index.m3u8': '#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nhls/mixed.m3u8',
            'https://example.com/hls/mixed.m3u8': '#EXTM3U\n#EXTINF:4.1,\na.ts\n#EXTINF:2.2,\nb.ts\n#EXT-X-ENDLIST',
        }
        result = d.playlist('https://example.com/index.m3u8', fetch=lambda u: (mapping[u], u), check=d.url)
        self.assertEqual(result['segments'], 2)
        self.assertAlmostEqual(result['duration'], 6.3)
        self.assertTrue(result['url'].endswith('hls/mixed.m3u8'))

    def test_encryption_live_audio_and_bad_duration_refused(self):
        base = '#EXTM3U\n#EXTINF:4,\na.ts\n#EXT-X-ENDLIST'
        bad = [base.replace('#EXT-X-ENDLIST', ''),
               base.replace('#EXTINF', '#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF'),
               base.replace('#EXTINF', '#EXT-X-MEDIA:TYPE=AUDIO,URI="audio.m3u8"\n#EXTINF'),
               base.replace('4,', 'nan,'), base.replace('4,', '-1,'), 'html']
        for body in bad:
            with self.subTest(body=body), self.assertRaises(ValueError):
                self.check_body(body)

    def test_cycle_refused(self):
        with self.assertRaises(ValueError):
            self.check_body('#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nindex.m3u8')


if __name__ == '__main__':
    unittest.main()
