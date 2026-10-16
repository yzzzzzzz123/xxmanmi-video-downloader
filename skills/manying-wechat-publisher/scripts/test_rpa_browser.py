"""RPA behavior tests use an isolated browser and loopback-only synthetic editor."""
import argparse
from contextlib import contextmanager
import copy
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import rpa_browser as rpa
import test_publisherctl as publisher_tests

PLAN = {"schema_version": 1, "account": {"selector": "#account", "text": "漫映Manying"},
        "title_selector": "#title", "editor_selector": "#body",
        "steps": [{"action": "fill", "selector": "#title", "field": "title"},
                  {"action": "fill", "selector": "#summary", "field": "summary"},
                  {"action": "paste-html", "selector": "#body"},
                  {"action": "save", "selector": "#save", "text": "保存草稿",
                   "receipt_selector": "#receipt", "receipt_text": "保存成功"}]}
EDITOR = """<!doctype html><html><head><meta charset="utf-8"><title>Offline RPA fixture</title></head><body>
<div id="account">漫映Manying</div><div id="title" contenteditable="true" role="textbox"></div><textarea id="summary"></textarea>
<div id="body" contenteditable="true"></div><input id="upload" type="file" style="display:none">
<button id="save">保存草稿</button><button id="publish">发表</button><div id="receipt" style="display:none"></div>
<script>document.getElementById('save').onclick=()=>{const el=document.getElementById('receipt');
el.style.display='block';el.textContent='保存成功';};</script></body></html>"""


class PlanTests(unittest.TestCase):
    def test_no_publication_or_authentication_actions(self):
        for text in ["发表", "继续发表", "保存并群发", "登录", "扫码", "删除", "Publish"]:
            plan = copy.deepcopy(PLAN)
            plan["steps"] = [{"action": "click", "selector": "button", "text": text, "next_selector": "body"}]
            with self.assertRaises(ValueError):
                rpa.validate_plan(plan)

    def test_no_arbitrary_code_or_fill_payload(self):
        for step in [{"action": "evaluate", "selector": "body"},
                     {"action": "fill", "selector": "input", "field": "password"}]:
            plan = copy.deepcopy(PLAN)
            plan["steps"] = [step]
            with self.assertRaises(ValueError):
                rpa.validate_plan(plan)

    def test_authenticated_urls_are_redacted(self):
        value = rpa.sanitized("Failed at https://mp.weixin.qq.com/cgi-bin/appmsg?token=private#x")
        self.assertNotIn("private", value)
        self.assertNotIn("token=", value)

    def test_skill_edit_is_not_draft_authorization(self):
        with self.assertRaises(ValueError):
            rpa.run_article(argparse.Namespace(authorized_draft=False))


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.fixture = publisher_tests.PublisherTests()
        self.fixture.setUp()
        self.fixture.prepare(native=True)
        self.directory = self.fixture.directory
        rpa.ctl.write_json(self.directory / "plan.json", PLAN)
        self.args = argparse.Namespace(authorized_draft=True, dir=str(self.directory), plan="plan.json",
                                       channel="chromium", headed=False, timeout_ms=1000)

    def tearDown(self):
        self.fixture.tearDown()

    def test_unknown_actions_are_not_replayed(self):
        for state in ["pending", "uncertain", "running"]:
            rpa.ctl.write_json(self.directory / "rpa-state.json", {"state": state})
            with patch.object(rpa, "browser_session") as launch:
                with self.assertRaises(ValueError):
                    rpa.run_article(self.args)
                launch.assert_not_called()

    def test_completed_plan_has_no_repeat_side_effect(self):
        manifest = rpa.ctl.load_manifest(self.directory)
        plan_hash = hashlib.sha256(json.dumps(PLAN, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
        rpa.ctl.write_json(self.directory / "rpa-state.json", {"state": "completed", "plan_hash": plan_hash,
                                                            "render_digest": manifest["render_digest"]})
        with patch.object(rpa, "browser_session") as launch:
            self.assertTrue(rpa.run_article(self.args)["ok"])
            launch.assert_not_called()

    def test_published_and_inflight_articles_are_read_only(self):
        original = rpa.ctl.load_manifest(self.directory)
        for status in ["published", "submitted", "awaiting_admin", "publication_unknown"]:
            rpa.ctl.write_json(self.directory / "manifest.json", dict(original, status=status))
            with patch.object(rpa, "browser_session") as launch:
                with self.assertRaises(ValueError):
                    rpa.run_article(self.args)
                launch.assert_not_called()

    def test_failed_action_records_uncertainty_before_any_replay(self):
        class Page:
            def goto(self, *args, **kwargs):
                pass
        class Context:
            pages = [Page()]
        @contextmanager
        def session(*args, **kwargs):
            yield Context()
        def fail(page, plan, values, directory, progress):
            progress(0, "pending")
            raise RuntimeError("simulated lost action acknowledgement")
        with patch.object(rpa, "browser_session", session), patch.object(rpa, "execute_steps", fail):
            with self.assertRaises(RuntimeError):
                rpa.run_article(self.args)
        state = rpa.ctl.read_json(self.directory / "rpa-state.json")
        self.assertEqual(state["state"], "uncertain")
        with patch.object(rpa, "browser_session") as launch:
            with self.assertRaises(ValueError):
                rpa.run_article(self.args)
            launch.assert_not_called()


class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                data = EDITOR.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            def log_message(self, *args):
                pass
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.origin = "http://127.0.0.1:" + str(cls.server.server_address[1])
        cls.worker = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.worker.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.worker.join(timeout=3)

    @contextmanager
    def page(self):
        with rpa.browser_session(channel="chromium", temporary=True) as context:
            page = context.new_page()
            page.goto(self.origin, wait_until="domcontentloaded")
            yield page

    def test_real_fill_html_paste_and_draft_receipt(self):
        values = {"title": "本地测试稿", "summary": "不是实际公众号文章", "assets": set(),
                  "html": "<section><h2>测试标题</h2><p>RPA正文测试</p></section>", "body_proof": "RPA正文测试"}
        with tempfile.TemporaryDirectory(prefix="manying-rpa-test-") as folder, self.page() as page:
            page, saved = rpa.execute_steps(page, PLAN, values, Path(folder), origin=self.origin)
            self.assertTrue(saved)
            self.assertEqual(page.locator("#title").inner_text(), values["title"])
            self.assertEqual(page.locator("#summary").input_value(), values["summary"])
            self.assertEqual(page.locator("#body h2").inner_text(), "测试标题")
            self.assertIn(values["body_proof"], page.locator("#body").inner_text())

    def test_wrong_account_stops_before_input(self):
        plan = copy.deepcopy(PLAN)
        plan["account"]["text"] = "另一个公众号"
        with self.page() as page, self.assertRaises(ValueError):
            rpa.execute_steps(page, plan, {}, Path("."), origin=self.origin)

    def test_another_draft_title_is_not_overwritten(self):
        with self.page() as page:
            page.locator("#title").fill("另一个草稿")
            with self.assertRaisesRegex(ValueError, "Another draft title"):
                rpa.execute_steps(page, PLAN, {"title": "本次文章"}, Path("."), origin=self.origin)
            self.assertEqual(page.locator("#title").inner_text(), "另一个草稿")

    def test_changed_publish_label_cannot_be_clicked(self):
        plan = copy.deepcopy(PLAN)
        plan["steps"] = [{"action": "save", "selector": "#publish", "text": "保存草稿",
                          "receipt_selector": "#receipt", "receipt_text": "保存成功"}]
        with self.page() as page, self.assertRaises(ValueError):
            rpa.execute_steps(page, plan, {}, Path("."), origin=self.origin)

    def test_old_receipt_is_not_counted_as_a_new_save(self):
        plan = copy.deepcopy(PLAN)
        plan["steps"] = [plan["steps"][-1]]
        with self.page() as page:
            page.get_by_role("button", name="保存草稿", exact=True).click()
            with self.assertRaises(ValueError):
                rpa.execute_steps(page, plan, {}, Path("."), origin=self.origin)


if __name__ == "__main__":
    unittest.main()
