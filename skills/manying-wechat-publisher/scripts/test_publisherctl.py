"""Offline behavioral tests: no WeChat calls and no publication side effects."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("publisherctl", Path(__file__).with_name("publisherctl.py"))
ctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ctl)
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a2ioAAAAASUVORK5CYII=")


class PublicResponse:
    url = "https://example.org/poster.png"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def read(self, amount):
        return PNG[:amount]


class PublisherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        result = ctl.new_article(argparse.Namespace(root=str(self.root), title="补片指南", topic="补片角度", account="漫映Manying"))
        self.directory = Path(result["dir"])

    def tearDown(self):
        self.temp.cleanup()

    def prepare(self, native=True):
        with patch.object(ctl, "urlopen", return_value=PublicResponse()):
            ctl.fetch_image(argparse.Namespace(dir=str(self.directory), url=PublicResponse.url, file="poster.png", credit="Synthetic offline test fixture"))
        ctl.write_json(self.directory / "content.json", {"summary": "读者可以获得的具体信息。", "sources": [{"title": "Offline fixture source", "url": "https://example.org/news", "checked_at": "2026-10-09", "claims": ["Offline test input, not a real verified claim"]}], "blocks": [{"type": "p", "text": "第一段<&>不能注入HTML。"}, {"type": "image", "file": "poster.png", "caption": "测试图"}, {"type": "h2", "text": "小标题"}, {"type": "p", "text": "第二段"}], "disclosure": "仅为离线测试"})
        if native:
            ctl.write_json(self.directory / "native-images.json", {"poster.png": '<img src="https://mmbiz.qpic.cn/test/640?wx_fmt=png" data-w="1" alt="">'})
        ctl.render(argparse.Namespace(dir=str(self.directory)))

    def mark_saved(self):
        for field in ctl.CHECK_FIELDS:
            ctl.verify(argparse.Namespace(dir=str(self.directory), field=field, note="Synthetic observation; offline state-machine test"))
        self.event("draft-saved")

    def event(self, name, url=None):
        return ctl.record(argparse.Namespace(dir=str(self.directory), event=name, url=url, note="Synthetic observation; not a real WeChat action"))

    def test_new_deduplicates_without_rewriting_workspace(self):
        result = ctl.new_article(argparse.Namespace(root=str(self.root), title="补片 指南！", topic="another", account="漫映Manying"))
        self.assertTrue(result["duplicate"])
        self.assertEqual(result["dir"], str(self.directory))
        self.assertEqual(ctl.read_json(self.directory / "manifest.json")["topic"], "补片角度")

    def test_prepare_escapes_text_and_preserves_image_position(self):
        self.prepare()
        self.assertTrue(ctl.check(self.directory)["ok"])
        paste = (self.directory / "paste.html").read_text(encoding="utf-8")
        self.assertIn("&lt;&amp;&gt;", paste)
        self.assertLess(paste.index("第一段"), paste.index("mmbiz.qpic.cn"))
        self.assertLess(paste.index("mmbiz.qpic.cn"), paste.index("第二段"))

    def test_native_mapping_required_before_publication(self):
        self.prepare(native=False)
        self.mark_saved()
        self.assertFalse(ctl.check(self.directory, "publish")["ok"])
        with self.assertRaises(ValueError):
            self.event("submit-requested")

    def test_cannot_claim_draft_published(self):
        with self.assertRaises(ValueError):
            self.event("published", "https://mp.weixin.qq.com/s/test")

    def test_actual_state_sequence_and_immutable_published(self):
        self.prepare()
        self.mark_saved()
        self.assertTrue(ctl.check(self.directory, "publish")["ok"])
        self.event("submit-requested")
        self.event("awaiting-admin")
        with self.assertRaises(ValueError):
            ctl.render(argparse.Namespace(dir=str(self.directory)))
        with self.assertRaises(ValueError):
            self.event("submit-requested")
        self.event("uncertain")
        self.event("published", "https://mp.weixin.qq.com/s/test")
        self.assertEqual(ctl.load_manifest(self.directory)["status"], "published")
        with self.assertRaises(ValueError):
            self.event("published", "https://mp.weixin.qq.com/s/test")

    def test_modifications_invalidate_old_gates(self):
        self.prepare()
        self.mark_saved()
        content = ctl.read_json(self.directory / "content.json")
        content["blocks"][0]["text"] = "发生了正文修改"
        ctl.write_json(self.directory / "content.json", content)
        self.assertFalse(ctl.check(self.directory, "publish")["ok"])
        ctl.render(argparse.Namespace(dir=str(self.directory)))
        self.assertEqual(ctl.load_manifest(self.directory)["checks"], {})

    def test_asset_tampering_fails_integrity(self):
        self.prepare()
        with (self.directory / "poster.png").open("ab") as stream:
            stream.write(b"changed")
        self.assertFalse(ctl.check(self.directory)["ok"])

    def test_lock_excludes_other_writer(self):
        with ctl.locked(self.directory):
            with self.assertRaises(ValueError):
                with ctl.locked(self.directory):
                    pass
        self.assertFalse((self.directory / ".publisher.lock").exists())

    def test_auth_and_local_urls_rejected(self):
        for url in ("https://mp.weixin.qq.com/s?appmsg_token=secret", "https://localhost/image.jpg", "https://127.0.0.1/image.jpg", "https://user:password@example.org/image.jpg"):
            with self.assertRaises(ValueError):
                ctl.public_url(url)
        with self.assertRaises(ValueError):
            ctl.public_url("https://mp.weixin.qq.com/cgi-bin/appmsg", published=True)

    def test_native_html_rejects_script_and_wrong_cdn(self):
        for markup in ('<img src="https://example.org/image.jpg">', '<img src="https://mmbiz.qpic.cn/a" onerror="alert(1)">', '<img src="https://mmbiz.qpic.cn/a"><script>alert(1)</script>'):
            with self.assertRaises(ValueError):
                ctl.native_html(markup)

    def test_import_authorized_local_asset_and_do_not_overwrite(self):
        source = self.root / "own.png"
        source.write_bytes(PNG)
        args = argparse.Namespace(dir=str(self.directory), path=str(source), file="own.png", credit="Own offline fixture")
        result = ctl.register_image(args)
        self.assertEqual(result["width"], 1)
        self.assertEqual((self.directory / "own.png").read_bytes(), PNG)
        with self.assertRaises(ValueError):
            ctl.register_image(args)

    def test_gif_registration_preserves_bytes_and_rejects_truncated_header(self):
        gif = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAAAAAAALAAAAAABAAEAAAICRAEAOw==')
        source = self.root / 'own.gif'
        source.write_bytes(gif)
        ctl.register_image(argparse.Namespace(dir=str(self.directory), path=str(source),
                                             file='motion.gif', credit='Own GIF fixture'))
        self.assertEqual((self.directory / 'motion.gif').read_bytes(), gif)
        self.assertEqual(ctl.load_manifest(self.directory)['images'][0]['format'], 'gif')
        with self.assertRaises(ValueError):
            ctl.image_size(gif[:10])

    def test_realistic_native_image_style_and_self_closing_tag(self):
        markup = '<img src="https://mmbiz.qpic.cn/a" style="width:100%;height:auto;" />'
        self.assertEqual(ctl.native_html(markup), markup)
        with self.assertRaises(ValueError):
            ctl.native_html('<img src="https://mmbiz.qpic.cn/a" style="background:url(https://example.org)">')

    def test_publication_url_requires_identifier_but_allows_public_tracking(self):
        with self.assertRaises(ValueError):
            ctl.public_url("https://mp.weixin.qq.com/s", published=True)
        with self.assertRaises(ValueError):
            ctl.public_url("https://mp.weixin.qq.com/s/test?tempkey=preview", published=True)
        real_shape = "https://mp.weixin.qq.com/s?__biz=fixture&mid=1&idx=1&sn=fixture&scene=21"
        self.assertEqual(ctl.public_url(real_shape, published=True), real_shape)
        self.assertEqual(ctl.public_url("https://mp.weixin.qq.com/s/test#rd", published=True), "https://mp.weixin.qq.com/s/test")

    def test_path_escape_rejected(self):
        with self.assertRaises(ValueError):
            ctl.local_file(self.directory, "../outside.png")

    def test_inflight_assets_are_immutable(self):
        self.prepare()
        self.mark_saved()
        self.event("submit-requested")
        local = self.root / "other.png"
        local.write_bytes(PNG)
        with self.assertRaises(ValueError):
            ctl.register_image(argparse.Namespace(dir=str(self.directory), path=str(local), file="other.png", credit="Offline test"))
        with patch.object(ctl, "urlopen") as request:
            with self.assertRaises(ValueError):
                ctl.fetch_image(argparse.Namespace(dir=str(self.directory), url=PublicResponse.url, file="other.png", credit="Offline test"))
            request.assert_not_called()

    def test_legacy_status_read_only(self):
        legacy = self.root / "legacy"
        legacy.mkdir()
        ctl.write_json(legacy / "manifest.json", {"title": "旧稿", "status": "waiting_admin", "evidence_checked_at": "2026-10-08"})
        listing = ctl.status(argparse.Namespace(root=str(self.root), limit=5))
        self.assertEqual(listing["total"], 2)
        self.assertTrue(next(row for row in listing["articles"] if row.get("title") == "旧稿")["legacy"])
        with self.assertRaises(ValueError):
            ctl.load_manifest(legacy, mutable=True)

    def test_status_keeps_old_pending_work_ahead_of_new_archives(self):
        manifest = ctl.load_manifest(self.directory)
        manifest["status"] = "awaiting_admin"
        manifest["updated_at"] = "2026-01-01"
        ctl.write_json(self.directory / "manifest.json", manifest)
        archive = self.root / "newer-archive"
        archive.mkdir()
        ctl.write_json(archive / "manifest.json", {"title": "Newer completed article", "status": "published", "updated_at": "2026-10-09"})
        listing = ctl.status(argparse.Namespace(root=str(self.root), limit=1))
        self.assertEqual(listing["articles"][0]["status"], "awaiting_admin")
        self.assertEqual(listing["attention_required"], 1)


if __name__ == "__main__":
    unittest.main()
