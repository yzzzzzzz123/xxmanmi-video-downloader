import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import workflowctl


class RequestedSearchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.work = self.root / "work"
        self.discovery = self.root / "discovery"
        self.taskbooks = self.root / "books"
        self.requests = self.root / "requests"
        self.request = self.requests / "nominations.json"
        workflowctl.coordctl.init_state(self.state, "owner", str(self.work))
        workflowctl.atomic_json(self.request, {
            "kind": "user_requested_titles", "user_authorized": True,
            "requested_at": "2026-10-03T10:00:00+08:00",
            "items": [{"title": "龙与虎", "category": "动漫"}],
        })
        self.sync = mock.patch.object(workflowctl.dbctl, "sync", return_value={"imported_records": 1})
        self.sync_mock = self.sync.start()
        self.addCleanup(self.sync.stop)

    def admit(self, **kwargs):
        return workflowctl.admit_requested_search(
            self.state, kwargs.pop("owner", "owner"), kwargs.pop("request", self.request),
            kwargs.pop("query", "龙与虎"), kwargs.pop("channel", "量子"),
            discovery_root=self.discovery, taskbooks=self.taskbooks,
            request_root=self.requests, **kwargs,
        )

    def test_exact_nomination_uses_existing_claim_and_taskbook_format(self):
        result = self.admit(task_id="request-001")
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "admitted")
        self.assertEqual(result["next_action"]["kind"], "browser_search")
        self.assertTrue(result["claim_sha256"])
        book = json.loads(Path(result["taskbook"]).read_text(encoding="utf-8"))
        self.assertEqual(book["scope"]["queries"], ["龙与虎"])
        self.assertIn(str(self.request.resolve()), book["inputs"])
        resumed = self.admit()
        self.assertEqual(resumed["task_id"], "request-001")

    def test_unrequested_title_and_unauthorized_evidence_rejected_before_claim(self):
        with self.assertRaisesRegex(ValueError, "exact user-requested"):
            self.admit(query="不存在的提名")
        data = json.loads(self.request.read_text(encoding="utf-8"))
        data["user_authorized"] = False
        workflowctl.atomic_json(self.request, data)
        with self.assertRaisesRegex(ValueError, "explicit user"):
            self.admit()
        self.sync_mock.assert_not_called()

    def test_evidence_outside_allowed_root_rejected(self):
        with self.assertRaisesRegex(ValueError, "user-priority"):
            self.admit(request=self.root / "outside.json")

    def test_foreign_claim_is_not_stolen(self):
        workflowctl.shared_discovery.claim_search(self.discovery, "龙与虎", "量子", "other", "other-001")
        result = self.admit()
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "already_claimed")
        self.sync_mock.assert_not_called()

    def test_active_detail_batch_rejects_search_admission(self):
        workflowctl.coordctl.enqueue_items(self.state, "owner", [{
            "url": "https://www.xxmanmi.com/detail/test", "title": "测试",
            "source": "量子", "category": "动漫", "query": "测试",
        }])
        workflowctl.coordctl.claim_batch(self.state, "owner", 1)
        with self.assertRaisesRegex(ValueError, "active detail batch"):
            self.admit()

    def test_prepared_commit_resumes_without_search_or_sync(self):
        first = self.admit(task_id="request-002")
        workflowctl.atomic_json(self.work / "search-commit-request-002.json", {
            "owner": "owner", "task_id": "request-002", "phase": "prepared",
            "evidence": str(self.work / "request-002-evidence.json"),
        })
        self.sync_mock.reset_mock()
        result = self.admit()
        self.assertEqual(result["status"], "resumed_commit")
        self.assertEqual(result["task_id"], first["task_id"])
        self.assertEqual(result["next_action"]["kind"], "commit_search")
        self.sync_mock.assert_not_called()

    def test_completed_search_does_not_replay_browser(self):
        self.admit(task_id="request-003")
        workflowctl.atomic_json(self.work / "search-commit-request-003.json", {
            "owner": "owner", "task_id": "request-003", "phase": "complete",
            "evidence": str(self.work / "request-003-evidence.json"),
        })
        self.sync_mock.reset_mock()
        result = self.admit()
        self.assertEqual(result["status"], "already_complete")
        self.assertEqual(result["next_action"]["kind"], "none")
        self.sync_mock.assert_not_called()

    def test_saved_evidence_is_committed_not_researched(self):
        self.admit(task_id="request-004")
        workflowctl.atomic_json(self.work / "request-004-evidence.json", {"test": True})
        self.sync_mock.reset_mock()
        self.assertEqual(self.admit()["next_action"]["kind"], "commit_search")
        self.sync_mock.assert_not_called()

    def test_resume_cannot_change_task_identity(self):
        self.admit(task_id="request-005")
        with self.assertRaisesRegex(ValueError, "existing task_id"):
            self.admit(task_id="request-other")

    def test_requested_title_scope_retains_seasons_not_fuzzy_movies_or_commentary(self):
        for title, query, expected in [
            ("龙与虎", "龙与虎", True),
            ("广东十虎与后五虎", "龙与虎", False),
            ("怒虎狂龙", "龙与虎", False),
            ("辉夜大小姐想让我告白：天才们的恋爱头脑战 OVA", "辉夜大小姐想让我告白", True),
            ("月刊少女野崎君[电影解说]", "月刊少女野崎君", False),
            ("开画！少女漫", "月刊少女野崎君", False),
            ("夏洛特", "Charlotte/夏洛特", True),
            ("CLANNAD", "cl", False),
        ]:
            with self.subTest(title=title):
                self.assertEqual(workflowctl._requested_title_matches(title, query), expected)

    def test_commit_preserves_all_observations_but_enqueues_requested_program_only(self):
        claim = self.admit(task_id="request-filter")
        links = [
            {"url": "https://www.xxmanmi.com/video2/111.html", "title": "龙与虎", "status": "已完结"},
            {"url": "https://www.xxmanmi.com/video2/222.html", "title": "广东十虎与后五虎", "status": "HD"},
        ]
        evidence = self.work / "request-filter-evidence.json"
        workflowctl.atomic_json(evidence, {
            "task_id": claim["task_id"], "owner": "owner", "query": "龙与虎", "channel": "量子",
            "category": "动漫", "collected_at": "2026-10-03T03:00:00Z",
            "input_value_after_action": "龙与虎", "selected_channels": ["量子"],
            "visible_detail_links": links, "result_count": 2,
        })
        with mock.patch.object(workflowctl.dbctl, "lookup", return_value=[
            {"url": item["url"], "exists": False} for item in links
        ]):
            result = workflowctl.commit_search(self.state, "owner", evidence,
                                               discovery_root=self.discovery, taskbooks=self.taskbooks)
        self.assertEqual(result["result_count"], 2)
        self.assertEqual(result["enqueued"], 1)
        self.assertEqual(result["excluded_out_of_scope"], 1)
        state = workflowctl.read_state(self.state)
        self.assertEqual(set(state["items"]), {links[0]["url"]})
        journal = json.loads((self.work / "searches.ndjson").read_text(encoding="utf-8"))
        self.assertEqual(journal["links"], links)

    def test_request_evidence_change_is_not_silently_accepted(self):
        claim = self.admit(task_id="request-bound")
        book = json.loads(Path(claim["taskbook"]).read_text(encoding="utf-8"))
        workflowctl.atomic_json(self.request, {"changed": True})
        with self.assertRaisesRegex(ValueError, "changed after"):
            workflowctl._requested_query_for_book(book)

    def test_anime_request_excludes_explicit_other_format_but_tv_request_does_not(self):
        self.assertFalse(workflowctl._requested_title_matches("四月是你的谎言短剧", "四月是你的谎言", "动漫"))
        self.assertTrue(workflowctl._requested_title_matches("四月是你的谎言短剧", "四月是你的谎言", "电视剧"))
        self.assertTrue(workflowctl._requested_title_matches("四月是你的谎言真人版", "四月是你的谎言真人版", "动漫"))


if __name__ == "__main__":
    unittest.main()
