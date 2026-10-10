"""Offline checks for archive boundaries, repeat detection and credential exclusion."""
import json
from pathlib import Path
import tempfile
import unittest

import archive_articles as archive
import publisherctl as ctl


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def article(self, name, title, text, status='published', stamp='2026-10-08T16:01:00+00:00'):
        folder = self.root / name
        folder.mkdir()
        ctl.write_json(folder / 'manifest.json', {
            'publication_id': name, 'account': '漫映Manying', 'title': title,
            'status': status, 'published_at_observed': stamp,
            'public_url': 'https://mp.weixin.qq.com/s/' + name if status == 'published' else None,
            'images': [],
        })
        ctl.write_json(folder / 'content.json', {'sources': [], 'blocks': [
            {'type': 'p', 'text': text},
            {'type': 'p', 'text': '我们的网站是 https://manying.site/，点阅读原文。'},
        ]})
        return folder

    def test_shanghai_day_excludes_drafts_and_preserves_source_files(self):
        old = self.article('old', '旧日稿', '昨天的内容', stamp='2026-10-08T15:59:00+00:00')
        new = self.article('new', '新日稿', '当天的内容')
        self.article('pending', '在途稿', '尚未发表', status='draft')
        before = {p: p.read_bytes() for d in (old, new) for p in d.iterdir()}
        result = archive.archive_day(self.root, '2026-10-09')
        self.assertEqual(result['articles'], 1)
        saved = ctl.read_json(Path(result['file']))
        self.assertEqual(saved['articles'][0]['title'], '新日稿')
        self.assertEqual(saved['articles'][0]['content']['blocks'][0]['text'], '当天的内容')
        self.assertTrue(all(p.read_bytes() == data for p, data in before.items()))

    def test_shared_site_footer_is_allowed_but_reused_body_is_blocked(self):
        self.article('old', '修飞机', '女工站在装配台前，分工完成了飞机修理。')
        new = self.article('new', '看房单', '城堡会走路，看房时需要先跟上它。', status='draft')
        self.assertTrue(archive.check_article(self.root, new)['ok'])
        content = ctl.read_json(new / 'content.json')
        content['blocks'][0]['text'] = '女工站在装配台前，分工完成了飞机修理。'
        ctl.write_json(new / 'content.json', content)
        result = archive.check_article(self.root, new)
        self.assertFalse(result['ok'])
        self.assertIn('same_body', result['duplicates'][0]['reasons'])

    def test_normalized_title_and_long_copied_paragraph_are_detected(self):
        copied = '这一段足够长的主体文字在两篇不同文章中被完整复用，会造成内容重复，必须在发文前发现并改写而不是只换一个标题。'
        self.article('old', '看房单！', copied)
        new = self.article('new', '看 房 单', copied + '额外补充。', status='draft')
        content = ctl.read_json(new / 'content.json')
        content['blocks'].insert(0, {'type': 'p', 'text': copied})
        ctl.write_json(new / 'content.json', content)
        result = archive.check_article(self.root, new)
        self.assertFalse(result['ok'])
        self.assertIn('same_title', result['duplicates'][0]['reasons'])
        self.assertIn('repeated_substantive_paragraph', result['duplicates'][0]['reasons'])

    def test_backend_only_record_is_honest_and_preview_urls_are_rejected(self):
        target = self.root / 'archive'
        target.mkdir()
        record = {'publication_id': 'backend', 'account': '漫映Manying', 'title': '后台旧稿',
                  'published_at': '2026-10-09T01:57:00+08:00',
                  'public_url': 'https://mp.weixin.qq.com/s/backend'}
        ctl.write_json(target / 'backend-only.json', [record])
        result = archive.archive_day(self.root, '2026-10-09')
        self.assertEqual(result['backend_only'], 1)
        self.assertEqual(result['full_text_archived'], 0)
        record['public_url'] += '?tempkey=preview'
        ctl.write_json(target / 'backend-only.json', [record])
        with self.assertRaises(ValueError):
            archive.archive_day(self.root, '2026-10-09')

    def test_archive_keeps_repeat_detection_after_original_directory_moves(self):
        old = self.article('old', '过去的稿件', '主体段落保留在每日存档中，不能通过删除原目录来重复发表。')
        archive.archive_day(self.root, '2026-10-09')
        old.rename(self.root / 'archive' / 'moved-original')
        current = self.article('new', '另一个标题', '主体段落保留在每日存档中，不能通过删除原目录来重复发表。', status='draft')
        self.assertFalse(archive.check_article(self.root, current)['ok'])
        self.assertEqual(archive.archive_day(self.root, '2026-10-09')['articles'], 1)


if __name__ == '__main__':
    unittest.main()
