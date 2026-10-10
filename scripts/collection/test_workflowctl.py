import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from contextlib import redirect_stdout
from unittest import mock

import workflowctl


def observed(url):
    return {
        "url": url, "title": "测试节目", "status": "已完结",
        "source": "量子", "category": "动漫", "query": "测试查询",
    }


def detail(url):
    return {
        "url": url, "title": "测试节目", "status": "已完结",
        "source": "小小漫迷", "category": "动漫", "query": "测试查询",
        "collectedAt": "2026-09-28T01:00:00+00:00", "episodeDomCount": 1,
        "episodes": [{
            "group": "playList0", "label": "第01集", "line": "量子",
            "url": "https://media.example.invalid/episode-1.m3u8",
        }],
    }


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state.json"
        self.work = self.root / "work"
        self.discovery = self.root / "discovery"
        self.taskbooks = self.root / "taskbooks"
        workflowctl.coordctl.init_state(self.state, "owner", str(self.work))
        # All writer-lock effects stay in this isolated test workspace.
        self.writer_path = mock.patch.object(workflowctl.dbctl, "WRITE_LOCK_PATH", self.root / "database-writer.lock")
        self.writer_path.start()
        self.addCleanup(self.writer_path.stop)
        self.real_cover_gate = workflowctl._ensure_batch_covers
        self.cover_calls = []
        self.cover_gate = mock.patch.object(
            workflowctl, "_ensure_batch_covers", side_effect=self.selected_covers,
        )
        self.cover_gate_mock = self.cover_gate.start()
        self.addCleanup(self.cover_gate.stop)

    def tearDown(self):
        self.temporary.cleanup()

    def selected_covers(self, work, batch_id, source_urls):
        self.assertFalse(workflowctl.dbctl.WRITE_LOCK_PATH.exists())
        self.cover_calls.append((Path(work), batch_id, list(source_urls)))
        path = Path(work) / f"{batch_id}-covers.json"
        workflowctl.atomic_json(path, {
            "version": 1,
            "batch_id": batch_id,
            "complete": True,
            "cover_checked_items": len(source_urls),
            "items": [{
                "source_url": url,
                "program_id": index,
                "status": "selected",
                "selection": "existing",
                "artifact": {
                    "sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                },
            } for index, url in enumerate(source_urls, 1)],
        })
        return {
            "cover_ok": True,
            "cover_checked_items": len(source_urls),
            "cover_receipt": str(path.resolve()),
            "cover_receipt_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }

    def claim_search(self, *, query="测试查询", task_id="search-test-001"):
        rows = [{"query": query, "source": "量子", "category": "动漫"}]
        with mock.patch.object(workflowctl.dbctl, "searches", return_value=rows):
            return workflowctl.claim_search_task(
                self.state, "owner", "量子", query=query, task_id=task_id,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )

    def abandonment_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        value = {
            "version": 1,
            "reason": "abandoned_unknown_action",
            "owner": "owner",
            "task_id": claim["task_id"],
            "query": claim["query"],
            "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"],
            "logs_stopped": True,
            "journals": workflowctl._journal_snapshot(self.work),
            "browser_host": {
                "session_id": "owner", "terminated": True,
                "terminal_status": "interrupted",
                "checked_at": "2026-09-29T15:00:00Z",
                "method": "supported host session status",
            },
            "browser_tab": {
                "exists": False, "checked_at": "2026-09-29T15:00:01Z",
                "method": "browser host tab inventory",
            },
            "search_action": {
                "state": "unknown", "original_module_instance_available": False,
                "active_action_capability_available": False,
                "action_id_recoverable": False, "execute_receipt": False,
                "inspect_receipt": False, "finalize_receipt": False,
                "replay_authorized": False,
            },
            "local_state": {
                "browser_evidence_exists": False,
                "commit_receipt_exists": False,
                "search_journal_contains_task": False,
            },
            "summary": "host terminated and the unknown UI action cannot be safely replayed",
        }
        value.update(overrides)
        path = self.root / "abandon-search.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def host_mismatch_quarantine_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        value = {
            "version": 1, "reason": "quarantined_foreign_host_session",
            "owner": "owner", "task_id": claim["task_id"],
            "query": claim["query"], "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"], "logs_stopped": True,
            "journals": workflowctl._journal_snapshot(self.work),
            "browser_host": {
                "session_id": "foreign-runtime-session", "thread_id": "owner",
                "terminated": False, "checked_at": "2026-10-02T06:00:00Z",
                "method": "host request metadata and browser runtime inventory",
            },
            "browser_tab": {
                "exists": True, "tab_id": "tab-17",
                "checked_at": "2026-10-02T06:00:01Z",
                "method": "original runtime tab inventory and URL read",
            },
            "search_action": {
                "state": "completed_unverifiable", "action_attempts": 1,
                "action_id": "action-once", "click_returned": True,
                "original_module_instance_available": True,
                "replay_authorized": False,
                "result_error": "Zero results require a visible empty-result indicator",
            },
            "dom_summary": {
                "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T06:00:02Z",
                "input_value_after_action": claim["query"],
                "selected_channels": [claim["channel"]],
                "result_count": 0, "visible_detail_links": [],
                "empty_result_visible": False, "pagination_pending": False,
                "result_container_visible": True,
            },
            "local_state": {
                "browser_evidence_exists": False,
                "commit_receipt_exists": False,
                "search_journal_contains_task": False,
            },
            "summary": "one confirmed click; host session differs from owner thread; zero results lack visible empty state",
        }
        value.update(overrides)
        path = self.root / "quarantine-host-mismatch.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def legacy_evidence_gate_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journal_snapshot = workflowctl._journal_snapshot(self.work)
        value = {
            "version": 1, "reason": "quarantined_legacy_evidence_gate_failure",
            "owner": "owner", "task_id": claim["task_id"],
            "query": claim["query"], "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"], "logs_stopped": True,
            "journals_before": journal_snapshot,
            "journals_after": journal_snapshot,
            "browser_host": {
                "session_id": "owner", "thread_id": "owner", "terminated": False,
                "checked_at": "2026-10-02T06:10:00Z",
                "method": "owner browser runtime metadata and live session check",
            },
            "browser_tab": {
                "exists": True, "owner_thread_id": "owner", "tab_id": "tab-21",
                "checked_at": "2026-10-02T06:10:01Z",
                "method": "read-only original owner tab inventory and DOM snapshot",
            },
            "search_action": {
                "state": "completed_unverifiable", "action_attempts": 1,
                "click_returned": True, "action_id": "action-legacy-once",
                "original_module_instance_available": True,
                "private_action_record_exportable": False,
                "old_api_version": 3, "new_api_version": 4,
                "finalize_attempts": 1,
                "finalize_gate_error": "Search results did not visibly change from the pre-action snapshot; inspect stale or identical results",
                "replay_authorized": False,
            },
            "dom_summary": {
                "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T06:10:02Z",
                "input_value_after_action": claim["query"],
                "selected_channels": [claim["channel"]],
                "result_count": 0, "visible_detail_links": [],
                "empty_result_visible": True, "pagination_pending": False,
                "result_container_visible": True,
                "loading_indicator": {
                    "selector": "#searchList .weui-primary-loading",
                    "visible_count": 0, "visible_texts": [],
                },
            },
            "local_state": {
                "browser_evidence_exists": False,
                "commit_receipt_exists": False,
                "search_journal_contains_task": False,
            },
            "summary": "one search click returned; API v3 finalization rejected the exact query/channel empty DOM under the old change gate; private action record cannot be exported; no replay or submission",
        }
        value.update(overrides)
        path = self.root / "quarantine-legacy-evidence-gate.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def foreign_host_stale_results_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        old_links = [{"title": "新葫芦兄弟", "url": "https://www.xxmanmi.com/video2/72363.html"},
                     {"title": "新葫芦兄弟", "url": "https://www.xxmanmi.com/video2/74426.html"}]
        value = {
            "version": 1, "reason": "quarantined_foreign_host_stale_results",
            "owner": "owner", "task_id": claim["task_id"], "query": claim["query"],
            "channel": claim["channel"], "claim_sha256": claim["claim_sha256"],
            "logs_stopped": True, "journals_before": journals, "journals_after": journals,
            "browser_host": {"session_id": "foreign-runtime-session", "thread_id": "owner",
                "terminated": False, "checked_at": "2026-10-02T07:00:00Z", "method": "host metadata"},
            "browser_tab": {"exists": True, "readable": True, "tab_id": "tab-99",
                "session_id": "foreign-runtime-session", "owner_thread_id": "owner",
                "checked_at": "2026-10-02T07:00:01Z", "method": "tab inventory and read-only DOM"},
            "search_action": {"task_id": claim["task_id"], "state": "completed_unverifiable",
                "action_attempts": 1, "click_returned": True, "action_id": "click-99",
                "result_error": "Search results did not visibly change from the pre-action snapshot",
                "replay_authorized": False},
            "pre_action_dom_summary": {"query": "新葫芦兄弟", "visible_detail_links": old_links,
                "page_url": "https://www.xxmanmi.com/", "observed_at": "2026-10-02T07:00:02Z"},
            "dom_summary": {"input_value_after_action": claim["query"],
                "selected_channels": [claim["channel"]], "visible_detail_links": old_links,
                "result_count": 2, "result_container_visible": True, "pagination_pending": False,
                "loading_visible_count": 0, "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T07:00:03Z"},
            "local_state": {"browser_evidence_exists": False, "commit_receipt_exists": False,
                "search_journal_contains_task": False, "capture_lock_exists": False},
            "summary": "single confirmed click; foreign host; stable old non-target results remain; manual review only",
        }
        value.update(overrides)
        path = self.root / "quarantine-foreign-stale-results.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def foreign_host_input_mismatch_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        value = {
            "version": 1, "reason": "quarantined_foreign_host_input_mismatch",
            "owner": "owner", "task_id": claim["task_id"],
            "query": claim["query"], "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"], "logs_stopped": True,
            "journals_before": journals, "journals_after": journals,
            "browser_host": {"session_id": "foreign-runtime-session", "thread_id": "owner",
                "terminated": False, "checked_at": "2026-10-02T08:00:00Z", "method": "host metadata"},
            "browser_tab": {"exists": True, "readable": True, "tab_id": "tab-input-1",
                "session_id": "foreign-runtime-session", "owner_thread_id": "owner",
                "checked_at": "2026-10-02T08:00:01Z", "method": "original tab inventory and DOM read"},
            "search_action": {"task_id": claim["task_id"], "state": "completed_unverifiable",
                "action_attempts": 1, "click_returned": True, "action_id": "click-input-1",
                "result_error": "Zero results require a visible empty-result indicator",
                "original_module_instance_available": True, "replay_authorized": False},
            "dom_summary": {"page_url": "https://www.xxmanmi.com/", "observed_at": "2026-10-02T08:00:02Z",
                "input_value_after_action": claim["query"] + " ",
                "selected_channels": [claim["channel"]], "result_count": 0,
                "visible_detail_links": [], "empty_result_visible": False,
                "pagination_pending": False, "result_container_visible": True,
                "loading_indicator": None},
            "local_state": {"taskbook_scope_matches": True, "browser_evidence_exists": False,
                "commit_receipt_exists": False, "search_journal_contains_task": False,
                "capture_lock_exists": False},
            "summary": "one returned click used an input that differs from the exact claim; manual review only",
        }
        value.update(overrides)
        path = self.root / "quarantine-foreign-input-mismatch.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_foreign_host_input_mismatch_quarantine_is_manual_review_only(self):
        claim = self.claim_search(query="exact claim input", task_id="search-input-mismatch-001")
        path = self.foreign_host_input_mismatch_evidence(claim)
        output = io.StringIO()
        argv = ["quarantine-search-foreign-host-input-mismatch", "--state", str(self.state),
                "--thread", "owner", "--query", claim["query"], "--channel", "量子",
                "--evidence", str(path), "--logs-stopped", "--discovery-root", str(self.discovery),
                "--taskbooks", str(self.taskbooks)]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "quarantined_foreign_host_input_mismatch")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        inspected = workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")
        audit = inspected["abandonment"]
        self.assertEqual(audit["claim_query"], claim["query"])
        self.assertNotEqual(audit["dom_input_value"], claim["query"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())
        self.assertEqual(workflowctl._journal_snapshot(self.work), audit["journals_after"])
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            replay = workflowctl.claim_search_task(self.state, "owner", "量子", query=claim["query"],
                discovery_root=self.discovery, taskbooks=self.taskbooks)
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "quarantined_foreign_host_input_mismatch")
        no_lookup.assert_not_called()

    def test_foreign_host_input_mismatch_rejects_claim_action_tab_dom_and_artifact_failures(self):
        mutations = [
            ("claim_sha256", "f" * 64, "effective claim/task"),
            ("browser_host.session_id", "owner", "live foreign host session"),
            ("search_action.action_attempts", 2, "exactly one returned task click"),
            ("search_action.task_id", "other-task", "exactly one returned task click"),
            ("browser_tab.readable", False, "readable original task tab"),
            ("dom_summary.input_value_after_action", "exact claim input", "mismatched submitted query"),
            ("dom_summary.selected_channels", ["other"], "mismatched submitted query"),
            ("local_state.capture_lock_exists", True, "matching taskbook and absent evidence/commit/journal/lock"),
            ("journals_before", [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}],
             "two input-mismatch journal snapshots must match current worker logs"),
        ]
        for index, (path_part, value, message) in enumerate(mutations, 1):
            claim = self.claim_search(query=f"input mismatch reject {index}", task_id=f"search-input-reject-{index}")
            path = self.foreign_host_input_mismatch_evidence(claim)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            if path_part == "dom_summary.input_value_after_action":
                value = claim["query"]
            target = evidence
            parts = path_part.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            with self.subTest(field=path_part), mock.patch.object(workflowctl.time, "sleep"):
                with self.assertRaisesRegex((ValueError, RuntimeError), message):
                    workflowctl.quarantine_search_foreign_host_input_mismatch(
                        self.state, "owner", claim["query"], "量子", path, True,
                        discovery_root=self.discovery, taskbooks=self.taskbooks)
            self.assertEqual(workflowctl.shared_discovery.inspect_claim(
                self.discovery, claim["query"], "量子")["reason"], "claimed")

    def test_foreign_host_input_mismatch_refuses_existing_official_artifacts(self):
        claim = self.claim_search(query="artifact gate", task_id="search-input-artifact-001")
        path = self.foreign_host_input_mismatch_evidence(claim)
        (self.work / f"search-commit-{claim['task_id']}.json").write_text("{}", encoding="utf-8")
        with mock.patch.object(workflowctl.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "saved search evidence or commit exists"):
                workflowctl.quarantine_search_foreign_host_input_mismatch(
                    self.state, "owner", claim["query"], "量子", path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks)
        self.assertEqual(workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], "量子")["reason"], "claimed")

    def test_foreign_host_stale_results_quarantine_is_manual_review_only(self):
        claim = self.claim_search(query="target stale test", task_id="search-stale-001")
        path = self.foreign_host_stale_results_evidence(claim)
        output = io.StringIO()
        argv = ["quarantine-search-foreign-host-stale-results", "--state", str(self.state),
                "--thread", "owner", "--query", claim["query"], "--channel", "量子",
                "--evidence", str(path), "--logs-stopped", "--discovery-root", str(self.discovery),
                "--taskbooks", str(self.taskbooks)]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "quarantined_foreign_host_stale_results")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["dom_summary"]["result_count"], 2)
        self.assertEqual(audit["pre_action_dom_summary"]["query"], "新葫芦兄弟")
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())

    def test_foreign_host_stale_results_rejects_forged_or_unstable_proof(self):
        mutations = [
            ("browser_host.session_id", "owner", "live foreign host"),
            ("browser_tab.readable", False, "readable original task tab"),
            ("search_action.action_attempts", 2, "exactly one confirmed task click"),
            ("dom_summary.input_value_after_action", "wrong", "exact target input/channel"),
            ("dom_summary.visible_detail_links", [], "unchanged visible non-target results"),
            ("local_state.capture_lock_exists", True, "absent artifacts"),
        ]
        for index, (path_part, value, message) in enumerate(mutations, 1):
            claim = self.claim_search(query=f"stale reject {index}", task_id=f"search-stale-reject-{index}")
            path = self.foreign_host_stale_results_evidence(claim)
            evidence = json.loads(path.read_text(encoding="utf-8"))
            target = evidence
            parts = path_part.split(".")
            for part in parts[:-1]:
                target = target[part]
            target[parts[-1]] = value
            path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
            with self.subTest(field=path_part), mock.patch.object(workflowctl.time, "sleep"):
                with self.assertRaisesRegex((ValueError, RuntimeError), message):
                    workflowctl.quarantine_search_foreign_host_stale_results(
                        self.state, "owner", claim["query"], "量子", path, True,
                        discovery_root=self.discovery, taskbooks=self.taskbooks,
                    )
            self.assertEqual(workflowctl.shared_discovery.inspect_claim(
                self.discovery, claim["query"], "量子")["reason"], "claimed")

    def host_tab_gone_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journal_snapshot = workflowctl._journal_snapshot(self.work)
        value = {
            "version": 1, "reason": "quarantined_foreign_host_tab_gone",
            "owner": "owner", "task_id": claim["task_id"],
            "query": claim["query"], "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"], "logs_stopped": True,
            "journals_before": journal_snapshot, "journals_after": journal_snapshot,
            "browser_host": {
                "session_id": "foreign-runtime-session", "thread_id": "owner",
                "terminated": False, "checked_at": "2026-10-02T06:05:00Z",
                "method": "host request metadata and browser runtime inventory",
            },
            "browser_tab": {
                "exists": False, "tab_id": "tab-17",
                "missing_error": "Error: No tab with id: tab-17.",
                "inventory_tab_ids": [],
                "checked_at": "2026-10-02T06:05:01Z",
                "method": "original tab URL read and browser tab inventory",
            },
            "search_action": {
                "state": "completed_unverifiable", "action_attempts": 1,
                "action_id": "action-once", "click_returned": True,
                "original_module_instance_available": True,
                "replay_authorized": False,
                "result_error": "Zero results require a visible empty-result indicator",
            },
            "dom_summary": {
                "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T06:00:02Z",
                "input_value_after_action": claim["query"],
                "selected_channels": [claim["channel"]],
                "result_count": 0, "visible_detail_links": [],
                "empty_result_visible": False, "pagination_pending": False,
                "result_container_visible": True,
            },
            "local_state": {
                "browser_evidence_exists": False,
                "commit_receipt_exists": False,
                "search_journal_contains_task": False,
            },
            "summary": "one confirmed click; original tab later returned exact missing-tab error; no visible empty state",
        }
        value.update(overrides)
        path = self.root / "quarantine-host-tab-gone.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def confirmed_click_tab_gone_evidence(self, claim, **overrides):
        path = self.legacy_evidence_gate_evidence(claim)
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update({"reason": "quarantined_confirmed_click_tab_gone"})
        value["browser_tab"].update({
            "exists": False, "tab_id": "tab-21",
            "missing_error": "Error: No tab with id: tab-21.",
            "inventory_tab_ids": ["tab-other"],
        })
        value["search_action"].update({
            "state": "completed_unverifiable", "action_attempts": 1,
            "click_returned": True, "action_id": "action-click-once",
            "original_module_instance_available": True,
            "result_error": "Tab disappeared after confirmed click",
            "replay_authorized": False,
        })
        value["dom_summary"].update({
            "recorded_after_action": True, "action_id": "action-click-once",
        })
        value["summary"] = "confirmed click; original task tab later missing; preserve prior after-DOM; manual review only"
        value.update(overrides)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def confirmed_click_no_outcome_evidence(self, claim, **overrides):
        path = self.legacy_evidence_gate_evidence(claim)
        value = json.loads(path.read_text(encoding="utf-8"))
        value.update({"reason": "abandoned_confirmed_click_no_outcome"})
        value.pop("dom_summary")
        value["browser_host"].update({
            "session_id": "foreign-runtime-session", "thread_id": "owner",
        })
        value["browser_tab"].update({
            "exists": False, "tab_id": "tab-21",
            "missing_error": "Error: No tab with id: tab-21.",
            "inventory_tab_ids": ["tab-other"],
        })
        value["search_action"].update({
            "task_id": claim["task_id"], "state": "completed_outcome_unavailable",
            "action_attempts": 1, "click_returned": True,
            "action_id": "action-click-once", "action_outcome": "unavailable",
            "after_dom_exportable": False,
            "original_module_instance_available": True,
            "replay_authorized": False,
        })
        value["action_result"] = {
            "actionId": "action-click-once", "task_id": claim["task_id"],
            "next_action": "finalize_search_action",
        }
        value["summary"] = "confirmed one click; exact task tab absent; old action receipt has no after-DOM; outcome unavailable; manual review only"
        value.update(overrides)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def confirmed_click_stale_non_target_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        value = {
            "version": 1,
            "reason": "abandoned_confirmed_click_stale_non_target_result",
            "owner": "owner", "task_id": claim["task_id"],
            "query": claim["query"], "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"], "logs_stopped": True,
            "browser_host": {
                "session_id": "owner", "thread_id": "owner", "terminated": False,
                "checked_at": "2026-10-02T06:00:00Z", "method": "requestMeta and live host status",
            },
            "browser_tab": {
                "tab_id": "tab-21", "exists": True, "readable": True,
                "session_id": "owner", "owner_thread_id": "owner",
                "checked_at": "2026-10-02T06:00:01Z", "method": "tab inventory and read-only public DOM",
            },
            "search_action": {
                "task_id": claim["task_id"], "state": "completed_outcome_unavailable",
                "action_attempts": 1, "click_returned": True,
                "action_id": "action-click-once", "action_outcome": "unavailable",
                "after_dom_exportable": False, "original_module_instance_available": True,
                "replay_authorized": False,
            },
            "before_dom": {
                "recorded": True, "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T06:00:02Z", "input_value": claim["query"],
                "selected_channels": [claim["channel"]], "result_count": 1,
                "results": [{"title": "另一部旧节目", "url": "https://www.xxmanmi.com/video2/11111.html"}],
                "loading": False, "pagination_pending": False,
            },
            "after_dom": {
                "recorded": True, "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T06:00:03Z", "input_value": claim["query"],
                "selected_channels": [claim["channel"]], "result_count": 1,
                "results": [{"title": "另一部旧节目", "url": "https://www.xxmanmi.com/video2/11111.html"}],
                "loading": False, "pagination_pending": False, "action_id": "action-click-once",
            },
            "local_state": {
                "browser_evidence_exists": False, "commit_receipt_exists": False,
                "search_journal_contains_task": False,
            },
            "journals_before": journals, "journals_after": journals,
            "summary": "single click returned; exact query/channel remained with the same sole non-target result",
        }
        value.update(overrides)
        path = self.root / "abandon-stale-non-target.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def confirmed_click_helper_gate_error_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        helper_error = (
            "Search results did not visibly change from the pre-action snapshot; "
            "inspect stale or identical results"
        )
        value = {
            "version": 1, "reason": "abandoned_confirmed_click_helper_gate_error",
            "owner": "owner", "task_id": claim["task_id"],
            "query": claim["query"], "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"], "logs_stopped": True,
            "helper_error": helper_error,
            "browser_host": {
                "session_id": "owner", "thread_id": "owner-host-thread", "terminated": False,
                "checked_at": "2026-10-02T06:00:00Z", "method": "requestMeta and live host status",
            },
            "browser_tab": {
                "tab_id": "1799482794", "exists": False,
                "missing_error": "No tab with id: 1799482794.",
                "inventory_tab_ids": [], "checked_at": "2026-10-02T06:00:01Z",
                "method": "original task tab read and Chrome tab inventory",
            },
            "search_action": {
                "task_id": claim["task_id"], "state": "completed_outcome_unavailable",
                "action_attempts": 1, "click_returned": True,
                "result_error": helper_error, "original_module_instance_available": True,
                "replay_authorized": False,
            },
            "local_state": {
                "browser_evidence_exists": False, "action_result_receipt_exists": False,
                "commit_receipt_exists": False, "search_journal_contains_task": False,
            },
            "journals_before": journals, "journals_after": journals,
            "summary": "one returned click failed the exact stale-result helper gate; original tab absent; receipt unavailable; manual review only",
        }
        value.update(overrides)
        path = self.root / "abandon-helper-gate-error.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def foreign_host_click_unknown_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        value = {
            "version": 1,
            "reason": "abandoned_foreign_host_click_unknown",
            "owner": "owner",
            "task_id": claim["task_id"],
            "query": claim["query"],
            "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"],
            "logs_stopped": True,
            "browser_host": {
                "session_id": "foreign-runtime-session", "thread_id": "owner",
                "terminated": False, "session_reset": True,
                "checked_at": "2026-10-02T06:00:00Z",
                "method": "host request metadata after execution-context reset",
            },
            "browser_tab": {
                "tab_id": "tab-31", "url": "https://www.xxmanmi.com/",
                "exists": True, "readable": True,
                "session_id": "foreign-runtime-session", "owner_thread_id": "owner",
                "checked_at": "2026-10-02T06:00:01Z",
                "method": "existing host tab inventory and read-only public DOM read",
            },
            "search_action": {
                "state": "search_click_unknown", "action_attempts": 1,
                "search_click_attempted": True, "search_click_completed": "unknown",
                "original_module_instance_available": False,
                "active_action_capability_available": False,
                "action_id_recoverable": False,
                "execute_receipt": False, "inspect_receipt": False,
                "finalize_receipt": False, "replay_authorized": False,
            },
            "dom_summary": {
                "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-02T06:00:02Z",
                "input_value_after_action": claim["query"],
                "selected_channels": [claim["channel"]],
                "result_count": 0, "visible_detail_links": [],
                "empty_result_visible": False, "pagination_pending": False,
                "result_container_visible": True, "loading_indicator": None,
            },
            "local_state": {
                "browser_evidence_exists": False,
                "commit_receipt_exists": False,
                "search_journal_contains_task": False,
            },
            "journals_before": journals,
            "journals_after": journals,
            "summary": "one unknown click in a reset foreign host; task tab remained readable; manual review only",
        }
        value.update(overrides)
        path = self.root / "abandon-foreign-host-click-unknown.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def live_owner_click_unknown_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        journals = workflowctl._journal_snapshot(self.work)
        value = {
            "version": 1,
            "reason": "abandoned_live_owner_click_unknown",
            "owner": "owner",
            "task_id": claim["task_id"],
            "query": claim["query"],
            "channel": claim["channel"],
            "claim_sha256": claim["claim_sha256"],
            "logs_stopped": True,
            "browser_host": {
                "session_id": "owner", "thread_id": "owner-thread",
                "terminated": False, "context_reset": True,
                "checked_at": "2026-10-03T02:15:00Z",
                "method": "requestMeta plus observed Node execution timeout and kernel reset",
            },
            "browser_tab": {
                "tab_id": "tab-owner-1", "url": "https://www.xxmanmi.com/",
                "exists": True, "readable": True, "session_id": "owner",
                "owner_thread_id": "owner-thread", "checked_at": "2026-10-03T02:15:01Z",
                "method": "owner Chrome tab inventory and read-only public DOM snapshot",
            },
            "search_action": {
                "state": "search_click_unknown", "action_attempts": 1,
                "helper_call_attempts": 1, "search_click_attempted": True,
                "search_click_completed": "unknown",
                "input_value_before_call": claim["query"],
                "selected_channels_before_call": [claim["channel"]],
                "call_timed_out": True, "context_reset": True,
                "tool_error": "js execution timed out; kernel reset, rerun your request",
                "original_module_instance_available": False,
                "action_id_recoverable": False,
                "execute_receipt": False, "inspect_receipt": False,
                "finalize_receipt": False, "replay_authorized": False,
            },
            "dom_summary": {
                "page_url": "https://www.xxmanmi.com/",
                "observed_at": "2026-10-03T02:15:02Z",
                "input_value": claim["query"],
                "selected_channels": [claim["channel"]],
                "result_count": 0, "visible_detail_links": [],
                "empty_result_visible": False, "pagination_pending": False,
                "result_container_visible": True, "loading_indicator": None,
            },
            "local_state": {
                "browser_evidence_exists": False,
                "action_result_receipt_exists": False,
                "commit_receipt_exists": False,
                "search_journal_contains_task": False,
                "capture_lock_exists": False,
            },
            "journals_before": journals,
            "journals_after": journals,
            "summary": "one helper call timed out and reset its execution context; exact owner tab remained readable; manual review only",
        }
        value.update(overrides)
        path = self.root / "abandon-live-owner-click-unknown.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_legacy_confirmed_helper_without_action_id_can_be_isolated_not_replayed(self):
        claim = self.claim_search()
        path = self.host_mismatch_quarantine_evidence(claim)
        evidence = json.loads(path.read_text(encoding="utf-8"))
        action = evidence["search_action"]
        del action["action_id"]
        action.update({"helper_entry": "runSearchActionEvidence", "helper_call_attempts": 1,
                       "helper_returned": True, "click_returned": "unknown",
                       "click_completion_receipt_available": False,
                       "action_id_emitted": False, "active_action_capability_available": False})
        evidence["browser_tab"].update({"readable": True, "session_id": "foreign-runtime-session",
                                       "owner_thread_id": "owner"})
        evidence["dom_summary"]["loading_indicator"] = None
        workflowctl.atomic_json(path, evidence)
        with mock.patch.object(workflowctl.dbctl, "sync") as synced:
            result = workflowctl.quarantine_search_foreign_host_session(
                self.state, "owner", claim["query"], claim["channel"], path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
            synced.assert_not_called()
        self.assertTrue(result["manual_review_required"])
        self.assertFalse(result["replay_authorized"])
        inspected = workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], claim["channel"])
        self.assertIsNone(inspected["abandonment"]["action_id"])
        self.assertEqual(inspected["abandonment"]["action_identity_source"], "legacy_helper_no_id")

    def test_legacy_helper_isolation_rejects_invented_or_unconfirmed_action_state(self):
        claim = self.claim_search()
        path = self.host_mismatch_quarantine_evidence(claim)
        original = json.loads(path.read_text(encoding="utf-8"))
        del original["search_action"]["action_id"]
        original["search_action"].update({"helper_entry": "runSearchActionEvidence", "helper_call_attempts": 1,
                                          "helper_returned": True, "click_returned": "unknown",
                                          "click_completion_receipt_available": False,
                                          "action_id_emitted": False, "active_action_capability_available": False})
        original["browser_tab"].update({"readable": True, "session_id": "foreign-runtime-session",
                                        "owner_thread_id": "owner"})
        original["dom_summary"]["loading_indicator"] = None
        for key, value in [("click_returned", False), ("helper_call_attempts", 2),
                           ("action_id_emitted", True), ("result_error", "another error")]:
            evidence = json.loads(json.dumps(original))
            evidence["search_action"][key] = value
            workflowctl.atomic_json(path, evidence)
            with self.subTest(key=key), self.assertRaises(ValueError):
                workflowctl.quarantine_search_foreign_host_session(
                    self.state, "owner", claim["query"], claim["channel"], path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )

    def test_quarantine_search_host_mismatch_cli_is_terminal_and_non_replayable(self):
        claim = self.claim_search()
        evidence = self.host_mismatch_quarantine_evidence(claim)
        output = io.StringIO()
        argv = [
            "quarantine-search-host-mismatch", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "quarantined_foreign_host_session")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        inspected = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )
        audit = inspected["abandonment"]
        self.assertEqual(audit["browser_host_session_id"], "foreign-runtime-session")
        self.assertEqual(audit["browser_host_thread_id"], "owner")
        self.assertEqual(audit["action_id"], "action-once")
        self.assertEqual(audit["dom_summary"]["result_count"], 0)
        self.assertTrue(audit["manual_review_required"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            replay = workflowctl.claim_search_task(
                self.state, "owner", "量子", query=claim["query"],
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "quarantined_foreign_host_session")
        no_lookup.assert_not_called()

    def test_quarantine_search_host_mismatch_rejects_identity_or_dom_mismatch(self):
        claim = self.claim_search()
        path = self.host_mismatch_quarantine_evidence(claim)
        for field, value, message in (
            ("session_id", "owner", "preserve the foreign runtime session"),
            ("thread_id", "somebody-else", "preserve the foreign runtime session"),
        ):
            with self.subTest(field=field):
                evidence = json.loads(path.read_text(encoding="utf-8"))
                evidence["browser_host"][field] = value
                path.write_text(json.dumps(evidence), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, message):
                    workflowctl.quarantine_search_foreign_host_session(
                        self.state, "owner", claim["query"], "量子", path, True,
                        discovery_root=self.discovery, taskbooks=self.taskbooks,
                    )
        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["browser_host"].update({"session_id": "foreign-runtime-session", "thread_id": "owner"})
        evidence["dom_summary"]["empty_result_visible"] = True
        path.write_text(json.dumps(evidence), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "completed zero-result state"):
            workflowctl.quarantine_search_foreign_host_session(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
            "claimed",
        )

    def test_legacy_evidence_gate_quarantine_is_manual_review_only(self):
        claim = self.claim_search()
        evidence = self.legacy_evidence_gate_evidence(claim)
        output = io.StringIO()
        argv = [
            "quarantine-search-legacy-evidence-gate", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "quarantined_legacy_evidence_gate_failure")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["old_api_version"], 3)
        self.assertEqual(audit["new_api_version"], 4)
        self.assertEqual(audit["action_id"], "action-legacy-once")
        self.assertIn("Search results did not visibly change", audit["finalize_gate_error"])
        self.assertTrue(audit["dom_summary"]["empty_result_visible"])
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertTrue(audit["manual_review_required"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            replay = workflowctl.claim_search_task(
                self.state, "owner", "量子", query=claim["query"],
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "quarantined_legacy_evidence_gate_failure")
        no_lookup.assert_not_called()

    def test_legacy_evidence_gate_quarantine_rejects_unproven_cases(self):
        claim = self.claim_search()
        for field, value, message in (
            ("browser_host.session_id", "foreign-runtime", "live owner browser session"),
            ("search_action.old_api_version", 4, "recorded v3-to-v4 gate failure"),
            ("dom_summary.input_value_after_action", "wrong query", "exact query/channel"),
            ("dom_summary.empty_result_visible", False, "visible zero-result state"),
            ("journals_after", [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}],
             "two legacy evidence-gate journal snapshots"),
        ):
            with self.subTest(field=field):
                evidence_path = self.legacy_evidence_gate_evidence(claim)
                evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.quarantine_search_legacy_evidence_gate(
                            self.state, "owner", claim["query"], "量子", evidence_path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
            "claimed",
        )

    def test_confirmed_click_tab_gone_quarantine_is_manual_review_only(self):
        claim = self.claim_search()
        evidence = self.confirmed_click_tab_gone_evidence(claim)
        output = io.StringIO()
        argv = [
            "quarantine-search-confirmed-click-tab-gone", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "quarantined_confirmed_click_tab_gone")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["browser_host_session_id"], "owner")
        self.assertEqual(audit["browser_host_thread_id"], "owner")
        self.assertEqual(audit["browser_tab_missing_error"], "Error: No tab with id: tab-21.")
        self.assertEqual(audit["browser_tab_inventory_ids"], ["tab-other"])
        self.assertEqual(audit["action_id"], "action-click-once")
        self.assertTrue(audit["dom_summary"]["recorded_after_action"])
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertTrue(audit["manual_review_required"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())

    def test_confirmed_click_tab_gone_rejects_fabricated_or_unstable_evidence(self):
        claim = self.claim_search()
        cases = [
            ("browser_tab.exists", True, "original task tab to be absent"),
            ("browser_tab.missing_error", "Error: No tab with id: another-tab.", "exact No tab with id error"),
            ("browser_tab.inventory_tab_ids", ["tab-21"], "inventory must confirm"),
            ("browser_host.session_id", "foreign-session", "live non-foreign owner host"),
            ("browser_host.terminated", True, "live non-foreign owner host"),
            ("search_action.click_returned", False, "one explicitly returned action"),
            ("dom_summary.input_value_after_action", "wrong query", "previously recorded after-DOM summary"),
            ("dom_summary.selected_channels", ["别的渠道"], "previously recorded after-DOM summary"),
            ("dom_summary.action_id", "other-action", "previously recorded after-DOM summary"),
            ("journals_after", [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}], "two confirmed-click journal snapshots"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                path = self.confirmed_click_tab_gone_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.quarantine_search_confirmed_click_tab_gone(
                            self.state, "owner", claim["query"], "量子", path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
            "claimed",
        )

    def test_confirmed_click_no_outcome_is_manual_review_only_without_dom(self):
        claim = self.claim_search()
        evidence = self.confirmed_click_no_outcome_evidence(claim)
        output = io.StringIO()
        argv = [
            "abandon-confirmed-click-no-outcome", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "abandoned_confirmed_click_no_outcome")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["browser_host_session_id"], "foreign-runtime-session")
        self.assertEqual(audit["browser_host_thread_id"], "owner")
        self.assertEqual(audit["action_state"], "completed_outcome_unavailable")
        self.assertEqual(audit["action_outcome"], "unavailable")
        self.assertEqual(audit["action_task_id"], claim["task_id"])
        self.assertEqual(audit["action_id"], "action-click-once")
        self.assertEqual(audit["browser_tab_missing_error"], "Error: No tab with id: tab-21.")
        self.assertEqual(audit["browser_tab_inventory_ids"], ["tab-other"])
        self.assertEqual(audit["action_result_receipt"]["next_action"], "finalize_search_action")
        self.assertFalse(audit["after_dom_exportable"])
        self.assertNotIn("dom_summary", audit)
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertTrue(audit["manual_review_required"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            replay = workflowctl.claim_search_task(
                self.state, "owner", "量子", query=claim["query"],
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "abandoned_confirmed_click_no_outcome")
        no_lookup.assert_not_called()

    def test_confirmed_click_no_outcome_rejects_identity_action_tab_and_log_tampering(self):
        claim = self.claim_search()
        cases = [
            ("browser_host.thread_id", "another-owner", "live browser session and matching owner thread"),
            ("search_action.task_id", "another-task", "one returned action with unavailable outcome"),
            ("action_result.actionId", "another-action", "action result receipt must exactly match"),
            ("browser_tab.missing_error", "Error: No tab with id: fake-tab.", "exact No tab with id error"),
            ("browser_tab.inventory_tab_ids", ["tab-21"], "inventory must confirm"),
            ("journals_after", [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}], "two confirmed-click journal snapshots"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                path = self.confirmed_click_no_outcome_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.abandon_confirmed_click_no_outcome(
                            self.state, "owner", claim["query"], "量子", path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
                self.assertEqual(
                    workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
                    "claimed",
                )

    def test_foreign_host_click_unknown_cli_is_manual_review_only(self):
        claim = self.claim_search(query="foreign host click", task_id="search-foreign-click-001")
        evidence = self.foreign_host_click_unknown_evidence(claim)
        output = io.StringIO()
        argv = [
            "abandon-foreign-host-click-unknown", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "abandoned_foreign_host_click_unknown")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        inspected = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )
        audit = inspected["abandonment"]
        self.assertEqual(audit["browser_host_session_id"], "foreign-runtime-session")
        self.assertEqual(audit["browser_host_thread_id"], "owner")
        self.assertTrue(audit["browser_host_session_reset"])
        self.assertEqual(audit["browser_tab_session_id"], "foreign-runtime-session")
        self.assertTrue(audit["browser_tab_readable"])
        self.assertEqual(audit["action_state"], "search_click_unknown")
        self.assertEqual(audit["action_attempts"], 1)
        self.assertEqual(audit["search_click_completed"], "unknown")
        self.assertEqual(audit["dom_summary"]["input_value_after_action"], claim["query"])
        self.assertEqual(audit["dom_summary"]["selected_channels"], [claim["channel"]])
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertTrue(audit["manual_review_required"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            replay = workflowctl.claim_search_task(
                self.state, "owner", "量子", query=claim["query"],
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "abandoned_foreign_host_click_unknown")
        no_lookup.assert_not_called()

    def test_foreign_host_click_unknown_rejects_identity_action_and_tab_tampering(self):
        cases = [
            ("browser_host.session_id", "owner", "distinct live host session"),
            ("browser_host.thread_id", "another-owner", "distinct live host session"),
            ("browser_host.session_reset", False, "distinct live host session"),
            ("browser_tab.session_id", "another-session", "readable original tab"),
            ("browser_tab.owner_thread_id", "another-owner", "readable original tab"),
            ("browser_tab.exists", False, "readable original tab"),
            ("browser_tab.readable", False, "readable original tab"),
            ("browser_tab.url", "https://example.invalid/", "public xxmanmi.com page"),
            ("search_action.action_attempts", 2, "exactly one unknown click"),
            ("search_action.state", "completed_unverifiable", "exactly one unknown click"),
            ("search_action.search_click_attempted", False, "exactly one unknown click"),
            ("search_action.search_click_completed", "returned", "exactly one unknown click"),
            ("search_action.original_module_instance_available", True, "exactly one unknown click"),
            ("search_action.execute_receipt", True, "exactly one unknown click"),
            ("search_action.replay_authorized", True, "exactly one unknown click"),
            ("dom_summary.input_value_after_action", "wrong query", "exact query/channel"),
            ("dom_summary.selected_channels", ["別的渠道"], "exact query/channel"),
        ]
        for index, (field, value, message) in enumerate(cases, 1):
            with self.subTest(field=field):
                query = f"foreign click reject {index}"
                claim = self.claim_search(query=query, task_id=f"search-foreign-reject-{index}")
                path = self.foreign_host_click_unknown_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.abandon_foreign_host_click_unknown(
                            self.state, "owner", query, "量子", path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
                self.assertEqual(
                    workflowctl.shared_discovery.inspect_claim(self.discovery, query, "量子")["reason"],
                    "claimed",
                )

    def test_foreign_host_click_unknown_rejects_unstopped_or_changed_logs_and_artifacts(self):
        claim = self.claim_search(query="foreign log reject", task_id="search-foreign-log-001")
        path = self.foreign_host_click_unknown_evidence(claim)
        with self.assertRaisesRegex(ValueError, "requires --logs-stopped"):
            workflowctl.abandon_foreign_host_click_unknown(
                self.state, "owner", claim["query"], "量子", path, False,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )

        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["journals_after"] = [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}]
        path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "two foreign-host journal snapshots"):
            with mock.patch.object(workflowctl.time, "sleep"):
                workflowctl.abandon_foreign_host_click_unknown(
                    self.state, "owner", claim["query"], "量子", path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )

        path = self.foreign_host_click_unknown_evidence(claim)
        candidate = self.work / f"{claim['task_id']}-evidence.json"
        candidate.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "saved search evidence or commit exists"):
            workflowctl.abandon_foreign_host_click_unknown(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        candidate.unlink()

        commit = self.work / f"search-commit-{claim['task_id']}.json"
        commit.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "saved search evidence or commit exists"):
            workflowctl.abandon_foreign_host_click_unknown(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        commit.unlink()

        (self.work / ".capture-helper.lock").write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "capture writer is still active"):
            workflowctl.abandon_foreign_host_click_unknown(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        (self.work / ".capture-helper.lock").unlink()

        path = self.foreign_host_click_unknown_evidence(claim)
        journal_path = self.work / "searches.ndjson"
        journal_path.write_text(json.dumps({"task_id": claim["task_id"]}, ensure_ascii=False) + "\n", encoding="utf-8")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        current_journals = workflowctl._journal_snapshot(self.work)
        evidence["journals_before"] = current_journals
        evidence["journals_after"] = current_journals
        path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "search journal already contains this task"):
            with mock.patch.object(workflowctl.time, "sleep"):
                workflowctl.abandon_foreign_host_click_unknown(
                    self.state, "owner", claim["query"], "量子", path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )

        unstable_claim = self.claim_search(
            query="foreign unstable journal", task_id="search-foreign-log-unstable",
        )
        unstable_path = self.foreign_host_click_unknown_evidence(unstable_claim)
        unstable_evidence = json.loads(unstable_path.read_text(encoding="utf-8"))
        first_snapshot = unstable_evidence["journals_before"]
        changed_snapshot = first_snapshot + [{
            "name": "new.ndjson", "size": 0, "sha256": "0" * 64,
        }]
        with self.assertRaisesRegex(RuntimeError, "two foreign-host journal snapshots"):
            with mock.patch.object(workflowctl, "_journal_snapshot",
                                   side_effect=[first_snapshot, changed_snapshot]), \
                    mock.patch.object(workflowctl.time, "sleep"):
                workflowctl.abandon_foreign_host_click_unknown(
                    self.state, "owner", unstable_claim["query"], "量子", unstable_path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )

    def test_live_owner_click_unknown_cli_is_manual_review_only(self):
        claim = self.claim_search(query="owner-matched click timeout", task_id="search-live-click-001")
        evidence = self.live_owner_click_unknown_evidence(claim)
        output = io.StringIO()
        argv = [
            "abandon-live-owner-click-unknown", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "abandoned_live_owner_click_unknown")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["browser_host_session_id"], "owner")
        self.assertEqual(audit["browser_host_thread_id"], "owner-thread")
        self.assertTrue(audit["browser_host_context_reset"])
        self.assertEqual(audit["browser_tab_session_id"], "owner")
        self.assertTrue(audit["browser_tab_readable"])
        self.assertEqual(audit["action_state"], "search_click_unknown")
        self.assertEqual(audit["action_attempts"], 1)
        self.assertEqual(audit["helper_call_attempts"], 1)
        self.assertEqual(audit["search_click_completed"], "unknown")
        self.assertEqual(audit["dom_summary"]["input_value"], claim["query"])
        self.assertEqual(audit["dom_summary"]["selected_channels"], [claim["channel"]])
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertTrue(audit["manual_review_required"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["action_result_receipt_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse(audit["capture_lock_exists"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            replay = workflowctl.claim_search_task(
                self.state, "owner", "量子", query=claim["query"],
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "abandoned_live_owner_click_unknown")
        no_lookup.assert_not_called()

    def test_live_owner_click_unknown_rejects_identity_action_dom_and_receipt_tampering(self):
        cases = [
            ("browser_host.session_id", "foreign-session", "owner-matched live host"),
            ("browser_host.terminated", True, "owner-matched live host"),
            ("browser_host.context_reset", False, "owner-matched live host"),
            ("browser_tab.exists", False, "readable original task tab"),
            ("browser_tab.readable", False, "readable original task tab"),
            ("browser_tab.session_id", "foreign-session", "readable original task tab"),
            ("search_action.action_attempts", 2, "one timed-out search helper call"),
            ("search_action.helper_call_attempts", 2, "one timed-out search helper call"),
            ("search_action.call_timed_out", False, "one timed-out search helper call"),
            ("search_action.tool_error", "other error", "one timed-out search helper call"),
            ("search_action.input_value_before_call", "wrong query", "one timed-out search helper call"),
            ("search_action.selected_channels_before_call", ["别的渠道"], "one timed-out search helper call"),
            ("search_action.execute_receipt", True, "one timed-out search helper call"),
            ("search_action.action_id", "guessed-id", "cannot contain an action ID or receipt"),
            ("dom_summary.input_value", "wrong query", "exact query, channel"),
            ("dom_summary.selected_channels", ["别的渠道"], "exact query, channel"),
            ("local_state.capture_lock_exists", True, "absent action/evidence/commit/journal/lock"),
        ]
        for index, (field, value, message) in enumerate(cases, 1):
            with self.subTest(field=field):
                query = f"live owner click reject {index}"
                claim = self.claim_search(query=query, task_id=f"search-live-reject-{index}")
                path = self.live_owner_click_unknown_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.abandon_live_owner_click_unknown(
                            self.state, "owner", query, "量子", path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
                self.assertEqual(
                    workflowctl.shared_discovery.inspect_claim(self.discovery, query, "量子")["reason"],
                    "claimed",
                )

    def test_live_owner_click_unknown_rejects_unstopped_or_changed_logs_and_artifacts(self):
        claim = self.claim_search(query="live owner click logs", task_id="search-live-logs-001")
        path = self.live_owner_click_unknown_evidence(claim)
        with self.assertRaisesRegex(ValueError, "requires --logs-stopped"):
            workflowctl.abandon_live_owner_click_unknown(
                self.state, "owner", claim["query"], "量子", path, False,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )

        evidence = json.loads(path.read_text(encoding="utf-8"))
        evidence["journals_after"] = [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}]
        path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "two live-owner journal snapshots"):
            with mock.patch.object(workflowctl.time, "sleep"):
                workflowctl.abandon_live_owner_click_unknown(
                    self.state, "owner", claim["query"], "量子", path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )

        path = self.live_owner_click_unknown_evidence(claim)
        candidate = self.work / f"{claim['task_id']}-evidence.json"
        candidate.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "search evidence, action receipt, or commit exists"):
            workflowctl.abandon_live_owner_click_unknown(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        candidate.unlink()

        action_receipt = self.work / f"search-action-{claim['task_id']}.json"
        action_receipt.write_text("{}", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "search evidence, action receipt, or commit exists"):
            workflowctl.abandon_live_owner_click_unknown(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        action_receipt.unlink()

        lock = self.work / ".capture-helper.lock"
        lock.write_text("active", encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "capture writer is still active"):
            workflowctl.abandon_live_owner_click_unknown(
                self.state, "owner", claim["query"], "量子", path, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        lock.unlink()

        path = self.live_owner_click_unknown_evidence(claim)
        journal_path = self.work / "searches.ndjson"
        journal_path.write_text(json.dumps({"task_id": claim["task_id"]}) + "\n", encoding="utf-8")
        evidence = json.loads(path.read_text(encoding="utf-8"))
        current_journals = workflowctl._journal_snapshot(self.work)
        evidence["journals_before"] = current_journals
        evidence["journals_after"] = current_journals
        path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(RuntimeError, "search journal already contains this task"):
            with mock.patch.object(workflowctl.time, "sleep"):
                workflowctl.abandon_live_owner_click_unknown(
                    self.state, "owner", claim["query"], "量子", path, True,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )

    def test_quarantine_search_host_tab_gone_cli_is_terminal_and_non_replayable(self):
        claim = self.claim_search()
        evidence = self.host_tab_gone_evidence(claim)
        output = io.StringIO()
        argv = [
            "quarantine-search-host-tab-gone", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "quarantined_foreign_host_tab_gone")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["browser_host_session_id"], "foreign-runtime-session")
        self.assertEqual(audit["browser_host_thread_id"], "owner")
        self.assertEqual(audit["action_id"], "action-once")
        self.assertEqual(audit["browser_tab_missing_error"], "Error: No tab with id: tab-17.")
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertFalse(audit["replay_authorized"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())

    def test_quarantine_search_host_tab_gone_preserves_transport_error_variants(self):
        for index, error in enumerate((
            "No tab with id: tab-17", "No tab with id: tab-17.",
            "Error: No tab with id: tab-17", "Error: No tab with id: tab-17.",
        )):
            with self.subTest(error=error):
                claim = self.claim_search(query=f"缺失标签{index}", task_id=f"search-gone-{index}")
                path = self.host_tab_gone_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                evidence["browser_tab"]["missing_error"] = error
                path = self.root / f"quarantine-host-tab-gone-{index}.json"
                path.write_text(json.dumps(evidence), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    receipt = workflowctl.quarantine_search_foreign_host_tab_gone(
                        self.state, "owner", claim["query"], "量子", path, True,
                        discovery_root=self.discovery, taskbooks=self.taskbooks,
                    )
                self.assertFalse(receipt["replay_authorized"])
                audit = workflowctl.shared_discovery.inspect_claim(
                    self.discovery, claim["query"], "量子",
                )["abandonment"]
                self.assertEqual(audit["browser_tab_missing_error"], error)
                self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
                self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())

    def test_quarantine_search_host_tab_gone_rejects_fabricated_or_unstable_evidence(self):
        claim = self.claim_search()
        path = self.host_tab_gone_evidence(claim)
        variants = []
        wrong_missing = json.loads(path.read_text(encoding="utf-8"))
        wrong_missing["browser_tab"]["missing_error"] = "tab missing"
        variants.append((wrong_missing, "exact No tab with id error"))
        for error in ("No tab with id: tab-18", "No tab with id: tab-17. extra", None, []):
            wrong_tab = json.loads(path.read_text(encoding="utf-8"))
            wrong_tab["browser_tab"]["missing_error"] = error
            variants.append((wrong_tab, "exact No tab with id error"))
        wrong_identity = json.loads(path.read_text(encoding="utf-8"))
        wrong_identity["browser_host"]["session_id"] = "owner"
        variants.append((wrong_identity, "preserve the foreign runtime session"))
        changed_journal = json.loads(path.read_text(encoding="utf-8"))
        changed_journal["journals_after"] = [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}]
        variants.append((changed_journal, "two host-tab-gone journal snapshots"))
        for evidence, message in variants:
            with self.subTest(message=message):
                path.write_text(json.dumps(evidence), encoding="utf-8")
                with self.assertRaisesRegex((ValueError, RuntimeError), message):
                    workflowctl.quarantine_search_foreign_host_tab_gone(
                        self.state, "owner", claim["query"], "量子", path, True,
                        discovery_root=self.discovery, taskbooks=self.taskbooks,
                    )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
            "claimed",
        )

    def test_confirmed_click_stale_non_target_result_is_manual_review_only(self):
        claim = self.claim_search()
        evidence = self.confirmed_click_stale_non_target_evidence(claim)
        output = io.StringIO()
        argv = [
            "abandon-confirmed-click-stale-non-target-result", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "abandoned_confirmed_click_stale_non_target_result")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["stable_non_target_result"]["url"], "https://www.xxmanmi.com/video2/11111.html")
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())

    def test_confirmed_click_stale_non_target_rejects_missing_or_changed_facts(self):
        claim = self.claim_search()
        cases = [
            ("browser_host.session_id", "foreign", "live owner browser host"),
            ("browser_tab.readable", False, "readable original owner tab"),
            ("search_action.action_attempts", 2, "exactly one returned click"),
            ("after_dom.input_value", "wrong query", "exact query/channel"),
            ("after_dom.results", [{"title": claim["query"], "url": "https://www.xxmanmi.com/video2/11111.html"}], "not a proven non-target"),
            ("after_dom.results", [{"title": "不同旧节目", "url": "https://www.xxmanmi.com/video2/11111.html"}], "same sole old non-target result"),
            ("journals_after", [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}], "two stale-result journal snapshots"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                path = self.confirmed_click_stale_non_target_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.abandon_confirmed_click_stale_non_target_result(
                            self.state, "owner", claim["query"], "量子", path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
            "claimed",
        )

    def test_confirmed_click_helper_gate_error_is_manual_review_only_without_receipt(self):
        claim = self.claim_search()
        evidence = self.confirmed_click_helper_gate_error_evidence(claim)
        output = io.StringIO()
        argv = [
            "abandon-confirmed-click-helper-gate-error", "--state", str(self.state),
            "--thread", "owner", "--query", claim["query"], "--channel", "量子",
            "--evidence", str(evidence), "--logs-stopped",
            "--discovery-root", str(self.discovery), "--taskbooks", str(self.taskbooks),
        ]
        with mock.patch.object(workflowctl.time, "sleep"), redirect_stdout(output):
            self.assertEqual(workflowctl.main(argv), 0)
        receipt = json.loads(output.getvalue())
        self.assertEqual(receipt["status"], "abandoned_confirmed_click_helper_gate_error")
        self.assertTrue(receipt["manual_review_required"])
        self.assertFalse(receipt["replay_authorized"])
        audit = workflowctl.shared_discovery.inspect_claim(
            self.discovery, claim["query"], claim["channel"],
        )["abandonment"]
        self.assertEqual(audit["browser_tab_missing_error"], "No tab with id: 1799482794.")
        self.assertEqual(audit["browser_tab_inventory_ids"], [])
        self.assertEqual(audit["helper_error"], "Search results did not visibly change from the pre-action snapshot; inspect stale or identical results")
        self.assertEqual(audit["browser_host_thread_id"], "owner-host-thread")
        self.assertFalse(audit["action_result_receipt_exists"])
        self.assertEqual(audit["journals_before"], audit["journals_after"])
        self.assertFalse(audit["browser_evidence_exists"])
        self.assertFalse(audit["commit_receipt_exists"])
        self.assertFalse(audit["search_journal_contains_task"])
        self.assertFalse((self.work / f"{claim['task_id']}-evidence.json").exists())
        self.assertFalse((self.work / f"search-commit-{claim['task_id']}.json").exists())

    def test_confirmed_click_helper_gate_error_rejects_any_unknown_fact_or_receipt(self):
        claim = self.claim_search()
        cases = [
            ("helper_error", "different error", "exact claim/task/error"),
            ("search_action.result_error", "different error", "exact recorded gate error"),
            ("browser_host.session_id", "foreign", "live owner browser session"),
            ("search_action.action_attempts", 2, "one returned click"),
            ("browser_tab.missing_error", "Error: No tab with id: wrong.", "exact No tab with id error"),
            ("browser_tab.inventory_tab_ids", ["1799482794"], "inventory must confirm"),
            ("local_state.action_result_receipt_exists", True, "absent receipt/evidence/commit/journal"),
            ("action_result", {"task_id": claim["task_id"], "actionId": "guessed", "next_action": "inspect_search_action"}, "guessed or fabricated action receipt"),
            ("journals_after", [{"name": "searches.ndjson", "size": 1, "sha256": "0" * 64}], "two helper-gate journal snapshots"),
        ]
        for field, value, message in cases:
            with self.subTest(field=field):
                path = self.confirmed_click_helper_gate_error_evidence(claim)
                evidence = json.loads(path.read_text(encoding="utf-8"))
                target = evidence
                parts = field.split(".")
                for part in parts[:-1]:
                    target = target[part]
                target[parts[-1]] = value
                path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
                with mock.patch.object(workflowctl.time, "sleep"):
                    with self.assertRaisesRegex((ValueError, RuntimeError), message):
                        workflowctl.abandon_confirmed_click_helper_gate_error(
                            self.state, "owner", claim["query"], "量子", path, True,
                            discovery_root=self.discovery, taskbooks=self.taskbooks,
                        )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, claim["query"], "量子")["reason"],
            "claimed",
        )

    def pre_search_cancellation_evidence(self, claim, **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        value = {
            "version": 1, "reason": "cancelled_pre_search_invalid_plan",
            "owner": "owner", "task_id": claim["task_id"], "query": claim["query"],
            "channel": claim["channel"], "claim_sha256": claim["claim_sha256"],
            "logs_stopped": True, "journals": workflowctl._journal_snapshot(self.work),
            "browser_host": {"session_id": "owner", "terminated": False,
                             "checked_at": "2026-09-29T15:00:00Z", "method": "supported host session status"},
            "browser_tab": {"exists": True, "checked_at": "2026-09-29T15:00:01Z", "method": "browser host tab inventory"},
            "search_action": {"state": "not_started_invalid_plan", "action_attempts": 0,
                              "original_module_instance_available": True, "execute_receipt": False,
                              "inspect_receipt": False, "finalize_receipt": False, "replay_authorized": False},
            "invalid_plan": {"reason": "channel_not_visible_in_current_search_surface", "visible_channels": ["非凡"]},
            "local_state": {"browser_evidence_exists": False, "commit_receipt_exists": False,
                            "search_journal_contains_task": False},
            "summary": "selected channel is not visible in the current public search surface; no UI action occurred",
        }
        value.update(overrides)
        path = self.root / "cancel-pre-search.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def claim_task_scope_mismatch_evidence(self, claim, task_query="测试 查询", **overrides):
        self.work.mkdir(parents=True, exist_ok=True)
        book_path = Path(claim["taskbook"])
        book = json.loads(book_path.read_text(encoding="utf-8"))
        book["scope"]["queries"] = [task_query]
        book_path.write_text(json.dumps(book, ensure_ascii=False), encoding="utf-8")
        claim_query = claim["query"]
        value = {
            "version": 1, "reason": "quarantined_claim_task_scope_mismatch",
            "owner": "owner", "task_id": claim["task_id"], "query": claim_query,
            "channel": claim["channel"], "claim_sha256": claim["claim_sha256"],
            "taskbook_sha256": hashlib.sha256(book_path.read_bytes()).hexdigest(),
            "claim_query": claim_query, "taskbook_query": task_query,
            "claim_query_codepoints": [ord(char) for char in claim_query],
            "taskbook_query_codepoints": [ord(char) for char in task_query],
            "logs_stopped": True,
            "journals_before": workflowctl._journal_snapshot(self.work),
            "journals_after": workflowctl._journal_snapshot(self.work),
            "search_action": {"state": "not_started_scope_mismatch", "input_fill_attempts": 0,
                              "click_attempts": 0, "replay_authorized": False},
            "local_state": {"taskbook_scope_mismatch": True,
                            "browser_evidence_exists": False, "commit_receipt_exists": False,
                            "search_journal_contains_task": False, "capture_lock_exists": False},
            "summary": "claim and taskbook query strings differ by exact Unicode codepoints; no input or click occurred",
        }
        value.update(overrides)
        path = self.root / "claim-task-scope-mismatch.json"
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def test_claim_search_creates_claim_and_taskbook_together(self):
        result = self.claim_search()
        self.assertTrue(result["ok"])
        book = json.loads(Path(result["taskbook"]).read_text(encoding="utf-8"))
        self.assertEqual(book["owner"], "owner")
        self.assertEqual(book["scope"]["queries"], ["测试查询"])
        self.assertEqual(book["scope"]["channels"], ["量子"])
        claim = workflowctl.shared_discovery.inspect_claim(
            self.discovery, "测试查询", "量子",
        )
        self.assertEqual(claim["claim"]["task_id"], result["task_id"])

    def test_claim_search_rejects_resource_source_name_without_creating_claim(self):
        with mock.patch.object(workflowctl.dbctl, "searches") as lookup:
            with self.assertRaisesRegex(ValueError, "not UI search channels"):
                workflowctl.claim_search_task(
                    self.state, "owner", "小小漫迷", query="测试查询",
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )
        lookup.assert_not_called()
        self.assertFalse((self.discovery / "claims").exists())

    def test_abandon_unknown_search_preserves_audit_and_allows_other_work(self):
        claimed = self.claim_search()
        claim_path = self.discovery / "claims" / (
            workflowctl.shared_discovery.claim_digest("测试查询", "量子") + ".json"
        )
        original = claim_path.read_bytes()
        evidence = self.abandonment_evidence(claimed)
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.abandon_search_unknown_action(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "abandoned_unknown_action")
        self.assertFalse(result["next_action"]["replay_abandoned"])
        self.assertEqual(claim_path.read_bytes(), original)
        inspected = workflowctl.shared_discovery.inspect_claim(
            self.discovery, "测试查询", "量子",
        )
        self.assertEqual(inspected["reason"], "abandoned_unknown_action")
        self.assertFalse(inspected["abandonment"]["replay_authorized"])
        with mock.patch.object(workflowctl.dbctl, "searches") as no_lookup:
            refused = workflowctl.claim_search_task(
                self.state, "owner", "量子", query="测试查询",
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(refused["reason"], "abandoned_unknown_action")
        no_lookup.assert_not_called()
        rows = [{"query": "其他查询", "source": "量子", "category": "动漫"}]
        with mock.patch.object(workflowctl.dbctl, "searches", return_value=rows) as lookup:
            other = workflowctl.claim_search_task(
                self.state, "owner", "量子",
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(other["query"], "其他查询")
        self.assertIn("测试查询", lookup.call_args.kwargs["exclude_queries"])

    def test_abandon_unknown_search_rejects_local_evidence_or_replay(self):
        claimed = self.claim_search()
        unsafe = self.abandonment_evidence(claimed)
        value = json.loads(unsafe.read_text(encoding="utf-8"))
        value["search_action"]["replay_authorized"] = True
        unsafe.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "refuse replay"):
            workflowctl.abandon_search_unknown_action(
                self.state, "owner", "测试查询", "量子", unsafe, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        evidence_file = self.work / f"{claimed['task_id']}-evidence.json"
        evidence_file.write_text("{}", encoding="utf-8")
        safe = self.abandonment_evidence(claimed)
        with self.assertRaisesRegex(RuntimeError, "browser evidence exists"):
            workflowctl.abandon_search_unknown_action(
                self.state, "owner", "测试查询", "量子", safe, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )

    def test_cancel_pre_search_invalid_plan_is_terminal_without_ui_action(self):
        claimed = self.claim_search()
        evidence = self.pre_search_cancellation_evidence(claimed)
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.cancel_pre_search_invalid_plan(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "cancelled_pre_search_invalid_plan")
        inspected = workflowctl.shared_discovery.inspect_claim(self.discovery, "测试查询", "量子")
        self.assertEqual(inspected["reason"], "cancelled_pre_search_invalid_plan")
        unsafe = self.pre_search_cancellation_evidence(claimed)
        value = json.loads(unsafe.read_text(encoding="utf-8"))
        value["search_action"]["action_attempts"] = 1
        unsafe.write_text(json.dumps(value), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "does not match the effective claim/task|not actively owned"):
            workflowctl.cancel_pre_search_invalid_plan(
                self.state, "owner", "测试查询", "量子", unsafe, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )

    def test_claim_task_scope_mismatch_is_terminal_non_replayable_manual_review(self):
        claimed = self.claim_search()
        claim_path = self.discovery / "claims" / (
            workflowctl.shared_discovery.claim_digest("测试查询", "量子") + ".json"
        )
        original_claim = claim_path.read_bytes()
        evidence = self.claim_task_scope_mismatch_evidence(claimed)
        original_taskbook = Path(claimed["taskbook"]).read_bytes()
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.quarantine_claim_task_scope_mismatch(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "quarantined_claim_task_scope_mismatch")
        self.assertTrue(result["manual_review_required"])
        self.assertFalse(result["replay_authorized"])
        self.assertEqual(claim_path.read_bytes(), original_claim)
        self.assertEqual(Path(claimed["taskbook"]).read_bytes(), original_taskbook)
        inspected = workflowctl.shared_discovery.inspect_claim(self.discovery, "测试查询", "量子")
        self.assertEqual(inspected["reason"], "quarantined_claim_task_scope_mismatch")
        self.assertFalse(inspected["abandonment"]["replay_authorized"])
        self.assertEqual(inspected["abandonment"]["claim_query_codepoints"], [ord(c) for c in "测试查询"])
        self.assertEqual(inspected["abandonment"]["taskbook_query_codepoints"], [ord(c) for c in "测试 查询"])

    def test_claim_task_scope_mismatch_refuses_equal_query_or_any_ui_attempt(self):
        claimed = self.claim_search()
        equal_book = self.claim_task_scope_mismatch_evidence(claimed, task_query="测试查询")
        with self.assertRaisesRegex(ValueError, "queries are equal"):
            workflowctl.quarantine_claim_task_scope_mismatch(
                self.state, "owner", "测试查询", "量子", equal_book, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        mismatch = self.claim_task_scope_mismatch_evidence(claimed)
        value = json.loads(mismatch.read_text(encoding="utf-8"))
        value["search_action"]["click_attempts"] = 1
        mismatch.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "no input or search click"):
            workflowctl.quarantine_claim_task_scope_mismatch(
                self.state, "owner", "测试查询", "量子", mismatch, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(
            workflowctl.shared_discovery.inspect_claim(self.discovery, "测试查询", "量子")["reason"],
            "claimed",
        )

    def test_cancel_pre_search_allows_verified_missing_tab_but_requires_reason(self):
        claimed = self.claim_search()
        evidence = self.pre_search_cancellation_evidence(claimed)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value["browser_tab"].update({"exists": False, "missing_reason": "verified absent from live tab inventory"})
        evidence.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.cancel_pre_search_invalid_plan(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "cancelled_pre_search_invalid_plan")

    def test_abandon_uninspectable_input_requires_no_submit_and_is_terminal(self):
        claimed = self.claim_search()
        evidence = self.pre_search_cancellation_evidence(claimed)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value.update({"reason": "abandoned_uninspectable_input_action"})
        value["browser_host"].update({"session_reset": True})
        value["browser_tab"].update({"exists": True, "owned_by_original_session": True})
        value["search_action"] = {"state": "input_fill_unknown_unsubmitted", "action_attempts": 1,
                                  "input_fill_attempts": 1, "submit_attempts": 0,
                                  "original_module_instance_available": False, "execute_receipt": False,
                                  "inspect_receipt": False, "finalize_receipt": False, "replay_authorized": False}
        evidence.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.abandon_uninspectable_input_action(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "abandoned_uninspectable_input_action")

    def test_abandon_uninspectable_click_is_terminal_without_claiming_completion(self):
        claimed = self.claim_search()
        evidence = self.pre_search_cancellation_evidence(claimed)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value.update({"reason": "abandoned_uninspectable_click_action"})
        value["browser_host"].update({"session_reset": True})
        value["browser_tab"].update({"exists": True, "owned_by_original_session": True})
        value["search_action"] = {"state": "search_click_unknown", "action_attempts": 1,
                                  "search_click_attempted": True, "search_click_completed": "unknown",
                                  "original_module_instance_available": False, "execute_receipt": False,
                                  "inspect_receipt": False, "finalize_receipt": False, "replay_authorized": False}
        evidence.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.abandon_uninspectable_input_action(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "abandoned_uninspectable_click_action")

    def test_cancel_pre_search_session_unavailable_is_terminal_before_ui(self):
        claimed = self.claim_search()
        evidence = self.pre_search_cancellation_evidence(claimed)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value.update({"reason": "cancelled_pre_search_session_unavailable"})
        value["browser_host"].update({"session_reset": True})
        value["browser_tab"] = {"exists": False, "missing_reason": "no task tab was created after the verified reset",
                                "checked_at": "2026-09-29T15:00:01Z", "method": "supported browser session inventory"}
        value["search_action"] = {"state": "not_started_session_unavailable", "action_attempts": 0,
                                  "original_module_instance_available": False, "execute_receipt": False,
                                  "inspect_receipt": False, "finalize_receipt": False, "replay_authorized": False}
        evidence.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.cancel_pre_search_invalid_plan(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "cancelled_pre_search_session_unavailable")

    def test_abandon_uninspectable_navigation_is_terminal_without_claiming_completion(self):
        claimed = self.claim_search()
        evidence = self.pre_search_cancellation_evidence(claimed)
        value = json.loads(evidence.read_text(encoding="utf-8"))
        value.update({"reason": "abandoned_uninspectable_navigation_action"})
        value["browser_host"].update({"session_reset": True})
        value["browser_tab"].update({"exists": True, "owned_by_original_session": True})
        value["search_action"] = {"state": "navigation_unknown", "action_attempts": 1,
                                  "navigation_attempted": True, "navigation_completed": "unknown",
                                  "original_module_instance_available": False, "execute_receipt": False,
                                  "inspect_receipt": False, "finalize_receipt": False, "replay_authorized": False}
        evidence.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        with mock.patch.object(workflowctl.time, "sleep"):
            result = workflowctl.abandon_uninspectable_input_action(
                self.state, "owner", "测试查询", "量子", evidence, True,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(result["status"], "abandoned_uninspectable_navigation_action")

    def test_commit_search_prechecks_syncs_and_enqueues_once(self):
        claim = self.claim_search()
        url = "https://www.xxmanmi.com/video2/9.html"
        evidence = {
            "task_id": claim["task_id"], "owner": "owner",
            "query": "测试查询", "channel": "量子", "category": "动漫",
            "collected_at": "2026-09-28T01:02:03+00:00",
            "input_value_after_action": "测试查询",
            "selected_channels": ["量子"], "result_count": 1,
            "visible_detail_links": [{"url": url, "title": "测试节目", "status": "已完结"}],
        }
        evidence_path = self.work / "browser-evidence.json"
        self.work.mkdir(parents=True, exist_ok=True)
        evidence_path.write_text(json.dumps(evidence, ensure_ascii=False), encoding="utf-8")
        lookup = [{
            "url": url, "exists": False, "captured": False,
            "captured_at": None, "episode_dom_count": None,
            "source_name": None, "unresolved_observations": 0,
        }]
        with mock.patch.object(workflowctl.dbctl, "lookup", return_value=lookup) as lookup_call:
            with mock.patch.object(workflowctl.dbctl, "sync", return_value={"imported_records": 1}):
                result = workflowctl.commit_search(
                    self.state, "owner", evidence_path,
                    discovery_root=self.discovery, taskbooks=self.taskbooks,
                )
        self.assertEqual(result["enqueued"], 1)
        lookup_call.assert_called_once_with([url])
        state = workflowctl.read_state(self.state)
        self.assertEqual(state["items"][url]["state"], "queued")
        lines = (self.work / "searches.ndjson").read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        with mock.patch.object(workflowctl.dbctl, "lookup") as no_lookup:
            repeat = workflowctl.commit_search(
                self.state, "owner", evidence_path,
                discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(repeat["status"], "already_complete")
        no_lookup.assert_not_called()
        self.assertEqual(len((self.work / "searches.ndjson").read_text(encoding="utf-8").splitlines()), 1)

    def test_complete_captured_builds_results_finishes_and_acks(self):
        url = "https://www.xxmanmi.com/video2/10.html"
        workflowctl.coordctl.enqueue_items(self.state, "owner", [observed(url)])
        claim = workflowctl.coordctl.claim_batch(self.state, "owner", 1)
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "details.ndjson").write_text(
            json.dumps(detail(url), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        finish = {
            "ok": True, "error_count": 0,
            "audit": {"records": 1, "passed": 1, "resources_checked": 1, "superseded": 0},
        }
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish) as run:
            result = workflowctl.complete_captured(
                self.state, "owner", claim["batch"], True,
            )
        self.assertEqual(result["status"], "acknowledged")
        run.assert_called_once_with("finish", [self.work.resolve()], tail=1)
        state = workflowctl.read_state(self.state)
        self.assertEqual(state["batches"][claim["batch"]]["state"], "acknowledged")
        self.assertEqual(state["items"][url]["state"], "acknowledged")
        self.assertTrue((self.work / f"{claim['batch']}-results.json").exists())
        self.assertTrue((self.work / f"{claim['batch']}-verification.json").exists())
        verification = json.loads(
            (self.work / f"{claim['batch']}-verification.json").read_text(encoding="utf-8")
        )
        self.assertTrue(verification["cover_ok"])
        self.assertEqual(verification["cover_checked_items"], 1)
        self.assertEqual(self.cover_calls, [(self.work.resolve(), claim["batch"], [url])])
        with mock.patch.object(workflowctl.batchctl, "run_batch") as no_finish:
            repeat = workflowctl.complete_captured(
                self.state, "owner", claim["batch"], True,
            )
        self.assertEqual(repeat["status"], "already_complete")
        no_finish.assert_not_called()

    def test_complete_captured_rejects_wrong_journal_tail_without_ready(self):
        claimed = "https://www.xxmanmi.com/video2/11.html"
        other = "https://www.xxmanmi.com/video2/12.html"
        workflowctl.coordctl.enqueue_items(self.state, "owner", [observed(claimed)])
        claim = workflowctl.coordctl.claim_batch(self.state, "owner", 1)
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "details.ndjson").write_text(
            json.dumps(detail(other), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        with self.assertRaisesRegex(ValueError, "does not exactly match"):
            workflowctl.complete_captured(self.state, "owner", claim["batch"], True)
        state = workflowctl.read_state(self.state)
        self.assertEqual(state["batches"][claim["batch"]]["state"], "claimed")

    def test_complete_captured_cover_failure_leaves_ready_for_reentry(self):
        url = "https://www.xxmanmi.com/video2/111.html"
        workflowctl.coordctl.enqueue_items(self.state, "owner", [observed(url)])
        claim = workflowctl.coordctl.claim_batch(self.state, "owner", 1)
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "details.ndjson").write_text(
            json.dumps(detail(url), ensure_ascii=False) + "\n", encoding="utf-8",
        )
        finish = {
            "ok": True, "error_count": 0,
            "audit": {"records": 1, "passed": 1, "resources_checked": 1},
        }
        self.cover_gate_mock.side_effect = RuntimeError("cover unavailable")
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
            with self.assertRaisesRegex(RuntimeError, "cover unavailable"):
                workflowctl.complete_captured(
                    self.state, "owner", claim["batch"], True,
                )
        state = workflowctl.read_state(self.state)
        self.assertEqual(state["batches"][claim["batch"]]["state"], "ready")
        self.assertFalse(
            (self.work / f"{claim['batch']}-verification.json").exists()
        )
        self.cover_gate_mock.side_effect = self.selected_covers
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
            completed = workflowctl.complete_captured(
                self.state, "owner", claim["batch"], True,
            )
        self.assertEqual(completed["status"], "acknowledged")

    def test_snapshot_is_compact_and_includes_all_outstanding_batches(self):
        first = "https://www.xxmanmi.com/video2/13.html"
        second = "https://www.xxmanmi.com/video2/14.html"
        workflowctl.coordctl.enqueue_items(self.state, "owner", [observed(first)])
        own_claim = workflowctl.coordctl.claim_batch(self.state, "owner", 1)
        other_work = self.root / "other-work"
        workflowctl.coordctl.register_thread(self.state, "other", "peer", str(other_work))
        workflowctl.coordctl.enqueue_items(self.state, "other", [observed(second)])
        other_claim = workflowctl.coordctl.claim_batch(self.state, "other", 1)
        db_status = {
            "sources": ["小小漫迷"], "discovered_programs": 2,
            "captured_programs": 0, "pending_programs": 2,
            "resource_links": 0, "m3u8_links": 0, "mp4_links": 0,
        }
        with mock.patch.object(workflowctl.dbctl, "status", return_value=db_status):
            result = workflowctl.snapshot(
                self.state, "小小漫迷", ["A=owner"],
            )
        self.assertEqual(result["database"], db_status)
        self.assertEqual(result["threads"], [{
            "label": "A", "owner": "owner", "mode": "claimed",
            "active_batch": own_claim["batch"], "active_items": 1,
        }])
        self.assertEqual(
            {item["batch"] for item in result["outstanding_batches"]},
            {own_claim["batch"], other_claim["batch"]},
        )

    def test_snapshot_rejects_duplicate_peer_owner(self):
        with self.assertRaisesRegex(ValueError, "unique"):
            workflowctl.snapshot(self.state, "小小漫迷", ["A=owner", "B=owner"])

    def test_claim_search_resumes_owned_task_without_another_taskbook(self):
        first = self.claim_search()
        second = self.claim_search()
        self.assertEqual(second["status"], "resumed")
        self.assertEqual(first["task_id"], second["task_id"])
        self.assertEqual(len(list(self.taskbooks.glob("*.json"))), 1)

    def test_search_excludes_claimed_queries_before_bounded_database_selection(self):
        workflowctl.shared_discovery.claim_search(self.discovery, "claimed", "量子", "other", "other-task")
        rows = [{"query": "beyond-first-page", "source": "量子", "category": "动漫"}]
        with mock.patch.object(workflowctl.dbctl, "searches", return_value=rows) as lookup:
            result = workflowctl.claim_search_task(
                self.state, "owner", "量子", discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        lookup.assert_called_once_with(limit=100, sources=["量子"], query=None,
                                       exclude_queries=["claimed"], preferred_queries=[])
        self.assertEqual(result["query"], "beyond-first-page")

    def test_search_exact_query_beyond_old_first_page_is_delegated_to_database(self):
        with mock.patch.object(workflowctl.dbctl, "searches", return_value=[{"query": "exact", "source": "量子"}]) as lookup:
            result = workflowctl.claim_search_task(
                self.state, "owner", "量子", query="exact", discovery_root=self.discovery, taskbooks=self.taskbooks,
            )
        self.assertEqual(lookup.call_args.kwargs["query"], "exact")
        self.assertEqual(result["query"], "exact")

    def test_search_prepared_commit_resumes_evidence_instead_of_browsing_again(self):
        first = self.claim_search()
        self.work.mkdir(parents=True, exist_ok=True)
        workflowctl.atomic_json(self.work / f"search-commit-{first['task_id']}.json", {
            "owner": "owner", "task_id": first["task_id"], "phase": "prepared", "evidence": "evidence.json",
        })
        second = self.claim_search()
        self.assertEqual(second["next_action"]["kind"], "commit_search")

    def test_search_prepared_after_successful_sync_resumes_when_database_pending_is_empty(self):
        claim = self.claim_search()
        url = "https://www.xxmanmi.com/video2/50.html"
        evidence_path = self.work / "search-evidence.json"
        self.work.mkdir(parents=True, exist_ok=True)
        workflowctl.atomic_json(evidence_path, {
            "task_id": claim["task_id"], "owner": "owner", "query": "测试查询", "channel": "量子",
            "category": "动漫", "collected_at": "2026-09-28T01:00:00Z",
            "input_value_after_action": "测试查询", "selected_channels": ["量子"], "result_count": 1,
            "visible_detail_links": [{"url": url, "title": "测试节目", "status": "已完结"}],
        })
        with mock.patch.object(workflowctl.dbctl, "lookup", return_value=[{"url": url, "exists": False}]):
            with mock.patch.object(workflowctl.dbctl, "sync", return_value={"imported_records": 1}):
                with mock.patch.object(workflowctl.coordctl, "enqueue_items", side_effect=RuntimeError("interrupted after sync")):
                    with self.assertRaisesRegex(RuntimeError, "interrupted"):
                        workflowctl.commit_search(self.state, "owner", evidence_path,
                            discovery_root=self.discovery, taskbooks=self.taskbooks)
        with mock.patch.object(workflowctl.dbctl, "searches", return_value=[]) as no_pending_lookup:
            resumed = workflowctl.claim_search_task(self.state, "owner", "量子",
                discovery_root=self.discovery, taskbooks=self.taskbooks)
        no_pending_lookup.assert_not_called()
        self.assertEqual(resumed["status"], "resumed_commit")
        self.assertEqual(resumed["next_action"]["evidence"], str(evidence_path.resolve()))
        with mock.patch.object(workflowctl.dbctl, "sync", return_value={"imported_records": 0}):
            completed = workflowctl.commit_search(self.state, "owner", evidence_path,
                discovery_root=self.discovery, taskbooks=self.taskbooks)
        self.assertEqual(completed["enqueued"], 1)

    def test_search_owned_pending_beyond_first_hundred_is_prioritized_in_database(self):
        workflowctl.shared_discovery.claim_search(self.discovery, "zzz-owned", "量子", "owner", "owned-task")
        rows = [{"query": f"new-{index:03}", "source": "量子"} for index in range(101)]
        rows.append({"query": "zzz-owned", "source": "量子"})
        def pending(**kwargs):
            preferred = kwargs["preferred_queries"]
            return sorted(rows, key=lambda row: (row["query"] not in preferred, row["query"]))[:kwargs["limit"]]
        with mock.patch.object(workflowctl.dbctl, "searches", side_effect=pending) as lookup:
            resumed = workflowctl.claim_search_task(self.state, "owner", "量子",
                discovery_root=self.discovery, taskbooks=self.taskbooks)
        self.assertEqual(lookup.call_args.kwargs["preferred_queries"], ["zzz-owned"])
        self.assertEqual(resumed["query"], "zzz-owned")
        self.assertEqual(resumed["task_id"], "owned-task")

    def test_search_task_id_cannot_escape_taskbook_directory(self):
        with self.assertRaisesRegex(ValueError, "filename"):
            self.claim_search(task_id="../outside")

    def claim_details(self, numbers):
        items = [observed(f"https://www.xxmanmi.com/video2/{number}.html") for number in numbers]
        workflowctl.coordctl.enqueue_items(self.state, "owner", items)
        return workflowctl.claim_detail_task(self.state, "owner", len(items), taskbooks=self.taskbooks)

    def append_detail(self, url):
        with (self.work / "details.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(detail(url), ensure_ascii=False) + "\n")

    def append_raw(self, url):
        record = {
            "url": url, "title": "raw title", "source": "小小漫迷", "collectedAt": "2026-09-28T01:00:00Z",
            "reason": "numeric placeholders", "episodeDomCount": 1,
            "rawEntries": [{"label": "第1集", "group": "playList0", "dataWeb": "123"}],
            "lineLabels": [{"for": "playList0", "text": "量子"}],
        }
        with (self.work / "raw-details.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")

    def test_claim_detail_resumes_only_remaining_urls(self):
        claim = self.claim_details([31, 32])
        self.append_detail(claim["items"][0]["url"])
        resumed = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(resumed["batch"], claim["batch"])
        self.assertEqual(resumed["items"], claim["items"][1:])
        self.assertEqual(resumed["observed"], {"captured": 1})
        self.assertEqual(resumed["next_action"]["kind"], "capture")
        self.assertEqual(workflowctl.read_state(self.state)["next_batch"], 2)

    def test_claim_detail_journal_boundary_ignores_old_retry_observation(self):
        self.work.mkdir(parents=True, exist_ok=True)
        url = "https://www.xxmanmi.com/video2/33.html"
        self.append_detail(url)
        claim = self.claim_details([33])
        self.assertEqual(claim["remaining"], 1)
        self.append_detail(url)
        resumed = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(resumed["remaining"], 0)
        self.assertEqual(resumed["next_action"]["kind"], "complete_detail")

    def test_claim_detail_refuses_partial_journal_before_allocating(self):
        self.work.mkdir(parents=True, exist_ok=True)
        (self.work / "details.ndjson").write_text('{"partial":', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "partial"):
            self.claim_details([34])
        self.assertIsNone(workflowctl.coordctl.current_batch(self.state, "owner")["active_batch"])

    def test_complete_detail_mixed_batch_audits_each_kind_and_preserves_manual_review(self):
        claim = self.claim_details([35, 36, 37, 38])
        urls = [row["url"] for row in claim["items"]]
        self.append_detail(urls[0])
        self.append_raw(urls[1])
        finishes = [
            {"ok": True, "error_count": 0, "audit": {"records": 1, "passed": 1, "resources_checked": 1}},
            {"ok": True, "error_count": 0, "audit": {"raw_records": 1, "raw_passed": 1, "raw_entries": 1}},
        ]
        with mock.patch.object(workflowctl.batchctl, "run_batch", side_effect=finishes) as run:
            result = workflowctl.complete_detail(
                self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks,
                failed=[[urls[2], "permission denied"]], skipped=[[urls[3], "stopped after permission denial"]],
            )
        self.assertEqual(run.call_args_list, [mock.call("finish", [self.work.resolve()], tail=1),
                                               mock.call("audit-raw", [self.work.resolve()], tail=1)])
        self.assertEqual(self.cover_calls, [
            (self.work.resolve(), claim["batch"], [urls[0]]),
        ])
        self.assertEqual(result["manual_review"], 3)
        state = workflowctl.read_state(self.state)
        self.assertEqual(state["items"][urls[0]]["state"], "acknowledged")
        self.assertTrue(all(state["items"][url]["state"] == "manual_review" for url in urls[1:]))
        with mock.patch.object(workflowctl.batchctl, "run_batch") as no_finish:
            again = workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(again["status"], "already_complete")
        no_finish.assert_not_called()

    def test_complete_detail_rejects_missing_pages_before_ready(self):
        claim = self.claim_details([39, 40])
        self.append_detail(claim["items"][0]["url"])
        with self.assertRaisesRegex(ValueError, "every batch URL"):
            workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(workflowctl.read_state(self.state)["batches"][claim["batch"]]["state"], "claimed")

    def test_complete_detail_failed_audit_leaves_ready_and_reentry_finishes(self):
        claim = self.claim_details([41])
        self.append_detail(claim["items"][0]["url"])
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value={"ok": False, "error_count": 1}):
            with self.assertRaisesRegex(RuntimeError, "did not verify"):
                workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(workflowctl.read_state(self.state)["batches"][claim["batch"]]["state"], "ready")
        resumed = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(resumed["next_action"]["kind"], "complete_detail")
        finish = {"ok": True, "error_count": 0, "audit": {"records": 1, "passed": 1}}
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
            completed = workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(completed["status"], "acknowledged")

    def test_complete_detail_cover_failure_leaves_ready_and_reentry_only_finalizes(self):
        claim = self.claim_details([411])
        self.append_detail(claim["items"][0]["url"])
        finish = {"ok": True, "error_count": 0,
                  "audit": {"records": 1, "passed": 1, "resources_checked": 1}}
        self.cover_gate_mock.side_effect = RuntimeError("cover unavailable")
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
            with self.assertRaisesRegex(RuntimeError, "cover unavailable"):
                workflowctl.complete_detail(
                    self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks,
                )
        state = workflowctl.read_state(self.state)
        self.assertEqual(state["batches"][claim["batch"]]["state"], "ready")
        self.assertFalse((self.work / f"{claim['batch']}-verification.json").exists())
        resumed = workflowctl.claim_detail_task(
            self.state, "owner", taskbooks=self.taskbooks,
        )
        self.assertEqual(resumed["next_action"]["kind"], "complete_detail")
        self.cover_gate_mock.side_effect = self.selected_covers
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
            completed = workflowctl.complete_detail(
                self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks,
            )
        self.assertEqual(completed["status"], "acknowledged")

    def test_cover_gate_revalidates_every_selected_item_and_detects_missing_artifact(self):
        urls = ["https://www.xxmanmi.com/video2/501.html",
                "https://www.xxmanmi.com/video2/502.html"]
        targets = [
            {"source_url": urls[0], "program_id": 501, "title": "节目501", "resource_count": 2},
            {"source_url": urls[1], "program_id": 502, "title": "节目502", "resource_count": 3},
        ]
        outputs = [
            {"ok": True, "items": [
                {"program_id": 501, "status": "selected", "selection": "generated",
                 "artifact": {"sha256": "1" * 64, "bytes": 1200}},
                {"program_id": 502, "status": "selected", "selection": "generated",
                 "artifact": {"sha256": "2" * 64, "bytes": 1300}},
            ]},
            {"ok": False, "items": [
                {"program_id": 501, "status": "unavailable",
                 "error": "selected JPEG is missing"},
                {"program_id": 502, "status": "selected", "selection": "existing",
                 "artifact": {"sha256": "2" * 64, "bytes": 1300}},
            ]},
        ]
        requests = []

        def run(command, **_kwargs):
            requests.append(json.loads(Path(command[-1]).read_text(encoding="utf-8")))
            payload = outputs.pop(0)
            return workflowctl.subprocess.CompletedProcess(
                command, 0 if payload["ok"] else 1, stdout=json.dumps(payload), stderr="",
            )

        with mock.patch.object(workflowctl, "_cover_targets", return_value=targets):
            with mock.patch.object(workflowctl.subprocess, "run", side_effect=run) as invoked:
                first_result = self.real_cover_gate(self.work, "batch-cover", urls)
                self.assertTrue(first_result["cover_ok"])
                with self.assertRaisesRegex(RuntimeError, "selected 1/2"):
                    self.real_cover_gate(self.work, "batch-cover", urls)
                second = json.loads(
                    (self.work / "batch-cover-covers.json").read_text(encoding="utf-8")
                )
                self.assertFalse(second["complete"])
                self.assertEqual(second["items"][0]["status"], "unavailable")
                self.assertEqual(invoked.call_count, 2)
        self.assertEqual([row["program_id"] for row in requests[0]], [501, 502])
        self.assertEqual([row["program_id"] for row in requests[1]], [501, 502])

    def test_ack_failure_reentry_preserves_verification_and_authenticates_legacy_cover_flags(self):
        for legacy_overwrite in (False, True):
            with self.subTest(legacy_overwrite=legacy_overwrite):
                claim = self.claim_details([504 + int(legacy_overwrite)])
                url = claim["items"][0]["url"]
                self.append_detail(url)
                target = {"source_url": url, "program_id": 504 + int(legacy_overwrite),
                          "title": "测试节目", "resource_count": 1}
                def central(selection):
                    payload = {"ok": True, "items": [{"program_id": target["program_id"],
                        "status": "selected", "selection": selection,
                        "artifact": {"sha256": "a" * 64}}]}
                    return workflowctl.subprocess.CompletedProcess(
                        [], 0, stdout=json.dumps(payload), stderr="")
                finish = {"ok": True, "error_count": 0,
                          "audit": {"records": 1, "passed": 1, "resources_checked": 1}}
                with mock.patch.object(workflowctl, "_cover_targets", return_value=[target]), \
                        mock.patch.object(workflowctl.subprocess, "run",
                                          side_effect=[central("generated"), central("existing")]) as worker, \
                        mock.patch.object(workflowctl, "_ensure_batch_covers", side_effect=self.real_cover_gate), \
                        mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
                    with mock.patch.object(workflowctl.coordctl, "acknowledge_batch",
                                           side_effect=PermissionError("ack replacement failed")):
                        with self.assertRaises(PermissionError):
                            workflowctl.complete_detail(self.state, "owner", claim["batch"], True,
                                                        taskbooks=self.taskbooks)
                    verification_path = self.work / f"{claim['batch']}-verification.json"
                    cover_path = self.work / f"{claim['batch']}-covers.json"
                    original_verification = verification_path.read_bytes()
                    original_cover = cover_path.read_bytes()
                    if legacy_overwrite:
                        overwritten = json.loads(original_cover)
                        overwritten["items"][0]["selection"] = "existing"
                        workflowctl.atomic_json(cover_path, overwritten)
                    result = workflowctl.complete_detail(self.state, "owner", claim["batch"], True,
                                                         taskbooks=self.taskbooks)
                self.assertEqual(result["status"], "acknowledged")
                self.assertEqual(worker.call_count, 2)
                self.assertEqual(verification_path.read_bytes(), original_verification)
                self.assertEqual(cover_path.read_bytes(), original_cover)
                recoveries = list(self.work.glob(f"{claim['batch']}-cover-recovery.json"))
                self.assertEqual(len(recoveries), int(legacy_overwrite))

    def test_cover_recovery_uses_short_name_in_uuid_owner_work_directory(self):
        work = self.root / ("project-" + "p" * 60) / "04-01a10441-ac69-7e21-9504-766e44174742"
        work.mkdir(parents=True)
        batch_id = "batch-001941"
        target = {"source_url": "https://www.xxmanmi.com/video2/506.html", "program_id": 506,
                  "title": "测试节目", "resource_count": 1}
        original = {"version": 1, "batch_id": batch_id, "complete": True,
                    "cover_checked_items": 1, "items": [{**target, "status": "selected",
                    "selection": "generated", "artifact": {"sha256": "b" * 64}}]}
        receipt_path = work / f"{batch_id}-covers.json"
        workflowctl.atomic_json(receipt_path, original)
        original_bytes = receipt_path.read_bytes()
        digest = hashlib.sha256(original_bytes).hexdigest()
        workflowctl.atomic_json(work / f"{batch_id}-verification.json", {
            "batch_id": batch_id, "verifier": "workflowctl complete-detail", "work": str(work.resolve()),
            "cover_receipt": str(receipt_path.resolve()), "cover_receipt_sha256": digest})
        fresh = json.loads(original_bytes)
        fresh["items"][0]["selection"] = "existing"
        workflowctl.atomic_json(receipt_path, fresh)
        self.assertEqual(workflowctl._preserve_bound_cover_receipt(work, batch_id, receipt_path, fresh), digest)
        self.assertEqual(receipt_path.read_bytes(), original_bytes)
        recovery = work / f"{batch_id}-cover-recovery.json"
        self.assertEqual(json.loads(recovery.read_bytes())["restored_sha256"], digest)
        self.assertLess(len(str(recovery)) + len(".abcdefgh.tmp"), 260)

    def test_bound_cover_revalidation_rejects_changed_image_tamper_and_missing_image(self):
        url = "https://www.xxmanmi.com/video2/506.html"
        target = {"source_url": url, "program_id": 506, "title": "测试节目", "resource_count": 1}
        for failure in ("changed_image", "tampered_hash", "missing_image"):
            with self.subTest(failure=failure):
                batch_id = "batch-" + failure
                cover_path = self.work / f"{batch_id}-covers.json"
                original = {"version": 1, "batch_id": batch_id, "complete": True,
                            "cover_checked_items": 1, "items": [{**target, "status": "selected",
                            "selection": "generated", "artifact": {"sha256": "b" * 64}}]}
                workflowctl.atomic_json(cover_path, original)
                before = cover_path.read_bytes()
                digest = hashlib.sha256(before).hexdigest()
                verification = {"batch_id": batch_id, "verifier": "workflowctl complete-detail",
                                "work": str(self.work.resolve()), "cover_receipt": str(cover_path.resolve()),
                                "cover_receipt_sha256": "f" * 64 if failure == "tampered_hash" else digest}
                verification_path = self.work / f"{batch_id}-verification.json"
                workflowctl.atomic_json(verification_path, verification)
                verification_bytes = verification_path.read_bytes()
                item = {"program_id": 506, "status": "selected", "selection": "existing",
                        "artifact": {"sha256": "c" * 64 if failure == "changed_image" else "b" * 64}}
                if failure == "missing_image":
                    item = {"program_id": 506, "status": "unavailable", "reason": "missing_image"}
                payload = {"ok": failure != "missing_image", "items": [item]}
                process = workflowctl.subprocess.CompletedProcess(
                    [], 0 if payload["ok"] else 3, stdout=json.dumps(payload), stderr="")
                with mock.patch.object(workflowctl, "_cover_targets", return_value=[target]), \
                        mock.patch.object(workflowctl.subprocess, "run", return_value=process):
                    with self.assertRaises((ValueError, RuntimeError)):
                        self.real_cover_gate(self.work, batch_id, [url])
                self.assertEqual(cover_path.read_bytes(), before)
                self.assertEqual(verification_path.read_bytes(), verification_bytes)
                self.assertFalse(list(self.work.glob(f"{batch_id}-cover-recovery.json")))

    def test_cover_gate_receipt_preserves_coverctl_reason(self):
        url = "https://www.xxmanmi.com/video2/503.html"
        targets = [{"source_url": url, "program_id": 503, "title": "节目503",
                    "resource_count": 1}]
        payload = {"ok": False, "items": [{
            "program_id": 503, "status": "unavailable",
            "reason": "source_unavailable", "retryable": True,
        }]}
        process = workflowctl.subprocess.CompletedProcess(
            [], 3, stdout=json.dumps(payload), stderr="",
        )
        with mock.patch.object(workflowctl, "_cover_targets", return_value=targets), \
                mock.patch.object(workflowctl.subprocess, "run", return_value=process):
            with self.assertRaisesRegex(RuntimeError, "selected 0/1"):
                self.real_cover_gate(self.work, "batch-cover-reason", [url])
        receipt = json.loads(
            (self.work / "batch-cover-reason-covers.json").read_text(encoding="utf-8")
        )
        self.assertEqual(receipt["items"][0]["error"], "source_unavailable")

    def test_cover_target_lookup_requires_exact_captured_programs_with_resources(self):
        urls = ["https://www.xxmanmi.com/video2/601.html",
                "https://www.xxmanmi.com/video2/602.html"]
        rows = [
            {"source_url": urls[0], "program_id": 601, "title": "节目601",
             "captured": True, "resource_count": 1},
            {"source_url": urls[1], "program_id": 602, "title": "节目602",
             "captured": True, "resource_count": 2},
        ]
        with mock.patch.object(workflowctl.dbctl, "sql_json", return_value=rows):
            targets = workflowctl._cover_targets(urls)
        self.assertEqual([row["program_id"] for row in targets], [601, 602])
        rows[1]["resource_count"] = 0
        with mock.patch.object(workflowctl.dbctl, "sql_json", return_value=rows):
            with self.assertRaisesRegex(RuntimeError, "no resources"):
                workflowctl._cover_targets(urls)

    def test_complete_detail_rejects_duplicate_or_unclaimed_observations(self):
        claim = self.claim_details([42])
        self.append_detail(claim["items"][0]["url"])
        self.append_detail(claim["items"][0]["url"])
        with self.assertRaisesRegex(ValueError, "duplicate/conflicting"):
            workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)

    def test_complete_detail_requires_explicit_stop_and_owner(self):
        claim = self.claim_details([43])
        with self.assertRaisesRegex(ValueError, "logs-stopped"):
            workflowctl.complete_detail(self.state, "owner", claim["batch"], False, taskbooks=self.taskbooks)
        workflowctl.coordctl.register_thread(self.state, "other", "peer", str(self.root / "other"))
        with self.assertRaisesRegex(ValueError, "not owned"):
            workflowctl.complete_detail(self.state, "other", claim["batch"], True, taskbooks=self.taskbooks)

    def test_complete_detail_ready_reentry_reuses_failure_declarations(self):
        claim = self.claim_details([44, 45])
        self.append_detail(claim["items"][0]["url"])
        failed_url = claim["items"][1]["url"]
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value={"ok": False, "error_count": 1}):
            with self.assertRaisesRegex(RuntimeError, "did not verify"):
                workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks,
                                            failed=[[failed_url, "browser permission denied"]])
        resumed = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(resumed["items"], [])
        self.assertEqual(json.loads(Path(resumed["capture_input"]).read_text(encoding="utf-8"))["items"], [])
        finish = {"ok": True, "error_count": 0, "audit": {"records": 1, "passed": 1}}
        with mock.patch.object(workflowctl.batchctl, "run_batch", return_value=finish):
            result = workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(result["manual_review"], 1)
        self.assertEqual(workflowctl.read_state(self.state)["items"][failed_url]["state"], "manual_review")

    def test_countdown_notice_cannot_fail_or_skip_an_uninspected_detail(self):
        claim = self.claim_details([451])
        url = claim["items"][0]["url"]
        for outcome in ("failed", "skipped"):
            with self.subTest(outcome=outcome):
                with self.assertRaisesRegex(ValueError, "countdown is not failure evidence"):
                    workflowctl.complete_detail(
                        self.state, "owner", claim["batch"], True,
                        taskbooks=self.taskbooks,
                        **{outcome: [[url, "验证码期限0天，AX未显示资源链接"]]},
                    )
                batch = workflowctl.read_state(self.state)["batches"][claim["batch"]]
                self.assertEqual(batch["state"], "claimed")
                self.assertFalse((self.work / f"{claim['batch']}-results.json").exists())
                self.assertEqual(self.cover_calls, [])

    def test_missing_browser_tab_keeps_batch_claimed_and_preserves_saved_pages(self):
        claim = self.claim_details([452, 453])
        self.append_detail(claim["items"][0]["url"])
        unvisited = claim["items"][1]["url"]
        for outcome in ("failed", "skipped"):
            for reason in ("No tab with id: 123", "Tab is not part of the current browser session"):
                with self.subTest(outcome=outcome, reason=reason):
                    with self.assertRaisesRegex(ValueError, "Browser tab is missing"):
                        workflowctl.complete_detail(
                            self.state, "owner", claim["batch"], True,
                            taskbooks=self.taskbooks,
                            **{outcome: [[unvisited, reason]]},
                        )
                    self.assertEqual(workflowctl.read_state(self.state)["batches"][claim["batch"]]["state"], "claimed")
                    self.assertFalse((self.work / f"{claim['batch']}-results.json").exists())
                    self.assertEqual(self.cover_calls, [])
        resumed = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(resumed["batch"], claim["batch"])
        self.assertEqual(resumed["remaining"], 1)
        self.assertEqual([item["url"] for item in resumed["items"]], [unvisited])
        self.assertEqual(resumed["observed"]["captured"], 1)

    def test_complete_detail_all_failed_never_audits_historical_capture_tail(self):
        self.work.mkdir(parents=True, exist_ok=True)
        self.append_detail("https://www.xxmanmi.com/video2/999.html")
        claim = self.claim_details([46])
        with mock.patch.object(workflowctl.batchctl, "run_batch") as no_audit:
            with mock.patch.object(workflowctl.dbctl, "sync", return_value={}) as sync:
                result = workflowctl.complete_detail(self.state, "owner", claim["batch"], True,
                    taskbooks=self.taskbooks, failed=[[claim["items"][0]["url"], "permission denied"]])
        no_audit.assert_not_called()
        self.cover_gate_mock.assert_not_called()
        sync.assert_called_once_with(self.work.resolve(), include_status=False)
        self.assertEqual(result["outcomes"], {"failed": 1})
        self.assertEqual(result["manual_review"], 1)

    def test_complete_detail_journal_change_during_audit_prevents_ack(self):
        claim = self.claim_details([47])
        url = claim["items"][0]["url"]
        self.append_detail(url)
        def changed(*args, **kwargs):
            self.append_detail(url)
            return {"ok": True, "error_count": 0, "audit": {"records": 1, "passed": 1}}
        with mock.patch.object(workflowctl.batchctl, "run_batch", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "changed during finalization"):
                workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(workflowctl.read_state(self.state)["batches"][claim["batch"]]["state"], "ready")

    def test_complete_detail_same_size_same_url_rewrite_during_audit_prevents_ack(self):
        claim = self.claim_details([49])
        self.append_detail(claim["items"][0]["url"])
        journal = self.work / "details.ndjson"
        before = journal.stat()
        original = journal.read_bytes()
        def changed(*args, **kwargs):
            rewritten = original.replace(b"episode-1.m3u8", b"episode-2.m3u8")
            self.assertNotEqual(rewritten, original)
            self.assertEqual(len(rewritten), len(original))
            journal.write_bytes(rewritten)
            # Make the signature change explicit even on coarse filesystem clocks.
            os.utime(journal, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
            self.assertEqual(journal.stat().st_size, before.st_size)
            return {"ok": True, "error_count": 0, "audit": {"records": 1, "passed": 1}}
        with mock.patch.object(workflowctl.batchctl, "run_batch", side_effect=changed):
            with self.assertRaisesRegex(RuntimeError, "changed during finalization"):
                workflowctl.complete_detail(self.state, "owner", claim["batch"], True, taskbooks=self.taskbooks)
        self.assertEqual(workflowctl.read_state(self.state)["batches"][claim["batch"]]["state"], "ready")

    def test_claim_detail_legacy_active_batch_ignores_observation_before_claim(self):
        self.work.mkdir(parents=True, exist_ok=True)
        url = "https://www.xxmanmi.com/video2/48.html"
        old = detail(url)
        old["collectedAt"] = "2000-01-01T00:00:00Z"
        (self.work / "details.ndjson").write_text(json.dumps(old) + "\n", encoding="utf-8")
        workflowctl.coordctl.enqueue_items(self.state, "owner", [observed(url)])
        workflowctl.coordctl.claim_batch(self.state, "owner", 1)
        resumed = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(resumed["remaining"], 1)
        self.assertEqual(resumed["observed"], {})

    def test_claim_detail_empty_does_not_claim_goal_completion(self):
        result = workflowctl.claim_detail_task(self.state, "owner", taskbooks=self.taskbooks)
        self.assertEqual(result["status"], "empty")
        self.assertEqual(result["next_action"]["kind"], "find_work")
        self.assertEqual(workflowctl.read_state(self.state)["next_batch"], 1)

    def test_snapshot_uses_one_coordination_snapshot(self):
        with mock.patch.object(workflowctl, "read_state", wraps=workflowctl.read_state) as read:
            with mock.patch.object(workflowctl.coordctl, "status_state") as extra_read:
                with mock.patch.object(workflowctl.dbctl, "status", return_value={}):
                    workflowctl.snapshot(self.state, "小小漫迷", [])
        read.assert_called_once()
        extra_read.assert_not_called()


if __name__ == "__main__":
    unittest.main()
