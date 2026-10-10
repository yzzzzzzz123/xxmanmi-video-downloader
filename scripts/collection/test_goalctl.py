import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import goalctl


URL = "https://www.xxmanmi.com/video2/1.html"


def fixture():
    verification = {
        "batch_id": "batch-000001", "verified": True, "sync_ok": True,
        "audit_ok": True, "verifier": "workflowctl complete-detail",
        "evidence": {"checked_items": 1, "method": "bound audit", "summary": "passed"},
    }
    batch = {"state": "acknowledged", "thread": "worker",
             "acknowledged_at": "2026-10-04T01:00:00Z", "item_urls": [URL],
             "results": [{"url": URL, "outcome": "captured"}],
             "verification": verification}
    state = {"batches": {"batch-000001": batch},
             "threads": {"worker": {"mode": "idle", "active_batch": None}}}
    rows = [{"id": 1, "source_url": URL, "source_name": "小小漫迷", "title": "节目",
             "captured_at": "2026-10-04T00:59:00Z", "resource_count": 2,
             "resource_last_seen": "2026-10-04T00:59:00Z"}]
    covers = [{"id": 1, "source": "小小漫迷", "title": "节目",
               "captured_at": rows[0]["captured_at"], "resource_count": 2,
               "thumbnail": "/api/thumbnails/1"}]
    return state, rows, covers


class GoalCountTests(unittest.TestCase):
    def acceptance(self, state, rows, covers):
        items = goalctl.qualification_gap_items(state, rows, covers)
        return {'version': 1, 'authority': 'user', 'source': '小小漫迷',
                'accepted_at': '2026-10-04T03:00:00Z', 'decision_basis': 'explicit user acceptance',
                'production_new_increment': False, 'count': len(items), 'items': items}

    def test_explicit_acceptance_counts_goal_without_fabricating_audit_or_new_increment(self):
        state, rows, covers = fixture()
        state['batches'] = {}
        accepted = self.acceptance(state, rows, covers)
        result = goalctl.summarize(state, rows, covers, target=1,
                                   since='2026-10-04T02:00:00Z', acceptance=accepted)
        self.assertEqual(result['qualified_programs'], 1)
        self.assertEqual(result['qualified_programs_lower_bound'], 0)
        self.assertEqual(result['user_accepted_programs'], 1)
        self.assertEqual(result['qualified_since'], 0)
        self.assertEqual(result['unqualified_captured'], 0)
        self.assertEqual(result['audit_unqualified_captured'], 1)
        self.assertEqual(result['qualification_gaps'], {'missing_batch_audit': 1})
        self.assertTrue(result['data_gates_satisfied'])
        self.assertEqual(state['batches'], {})

    def test_user_acceptance_does_not_expand_to_changed_rows_wrong_ids_or_missing_covers(self):
        state, rows, covers = fixture()
        state['batches'] = {}
        accepted = self.acceptance(state, rows, covers)
        for mutation in ('changed', 'wrong_id', 'wrong_source', 'bad_cover', 'raw'):
            with self.subTest(mutation=mutation):
                r, c = copy.deepcopy((rows, covers))
                if mutation == 'changed':
                    r[0]['resource_last_seen'] = '2026-10-04T02:00:00Z'
                elif mutation == 'wrong_id':
                    r[0]['id'] = 2
                elif mutation == 'wrong_source':
                    r[0]['source_name'] = 'other'
                elif mutation == 'raw':
                    r[0]['captured_at'] = None
                else:
                    c[0]['thumbnail'] = None
                result = goalctl.summarize(state, r, c, acceptance=accepted)
                self.assertEqual(result['qualified_programs'], 0)
                self.assertEqual(result['user_accepted_programs'], 0)

    def test_a_later_real_audit_does_not_double_count_or_make_accepted_history_new(self):
        state, rows, covers = fixture()
        no_audit = {**state, 'batches': {}}
        accepted = self.acceptance(no_audit, rows, covers)
        result = goalctl.summarize(state, rows, covers, acceptance=accepted,
                                   since='2026-10-04T00:00:00Z')
        self.assertEqual(result['qualified_programs'], 1)
        self.assertEqual(result['audited_qualified_programs'], 1)
        self.assertEqual(result['user_accepted_programs'], 0)
        self.assertEqual(result['qualified_since'], 0)

    def test_acceptance_does_not_finish_goal_with_an_inflight_batch(self):
        state, rows, covers = fixture()
        state['batches'] = {'pending': {'state': 'claimed'}}
        accepted = self.acceptance(state, rows, covers)
        result = goalctl.summarize(state, rows, covers, target=1, acceptance=accepted)
        self.assertEqual(result['qualified_programs'], 1)
        self.assertFalse(result['data_gates_satisfied'])

    def test_one_sources_acceptance_does_not_affect_other_sources_reporting(self):
        state, rows, covers = fixture()
        state['batches'] = {}
        accepted = self.acceptance(state, rows, covers)
        rows[0]['source_name'] = covers[0]['source'] = 'other'
        result = goalctl.summarize(state, rows, covers, source='other', acceptance=accepted)
        self.assertEqual(result['qualified_programs'], 0)
        self.assertEqual(result['user_accepted_programs'], 0)

    def test_malformed_duplicate_or_unauthorized_acceptance_is_rejected(self):
        state, rows, covers = fixture()
        state['batches'] = {}
        original = self.acceptance(state, rows, covers)
        for mutation in ('authority', 'count', 'duplicate', 'production'):
            with self.subTest(mutation=mutation):
                accepted = copy.deepcopy(original)
                if mutation == 'authority':
                    accepted['authority'] = 'model'
                elif mutation == 'count':
                    accepted['count'] = 2
                elif mutation == 'duplicate':
                    accepted['items'] *= 2
                    accepted['count'] = 2
                else:
                    accepted['production_new_increment'] = True
                with self.assertRaises(ValueError):
                    goalctl.summarize(state, rows, covers, acceptance=accepted)

    def test_check_loads_persisted_acceptance_for_the_exact_coordinator(self):
        state, rows, covers = fixture()
        state['batches'] = {}
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'state.json'
            path.write_text(json.dumps(state), encoding='utf-8')
            ledger = path.parent / 'user-qualification-acceptance.json'
            ledger.write_text(json.dumps(self.acceptance(state, rows, covers)), encoding='utf-8')
            with mock.patch.object(goalctl, 'cover_feed', return_value=covers), \
                    mock.patch.object(goalctl, 'database_rows', return_value=rows):
                result = goalctl.check(path, target=1)
            self.assertEqual(result['qualified_programs'], 1)
            self.assertFalse(result['sampling_changed'])
            self.assertEqual(len(result['user_acceptance_sha256']), 64)

    def test_audit_database_and_current_cover_are_all_required(self):
        state, rows, covers = fixture()
        result = goalctl.summarize(state, rows, covers, target=1)
        self.assertEqual(result["qualified_programs_lower_bound"], 1)
        self.assertTrue(result["data_gates_satisfied"])
        self.assertTrue(result["host_stop_required_before_completion"])
        for mutation in ("unverified", "captured_only", "wrong_id", "bad_cover", "changed_resources"):
            with self.subTest(mutation=mutation):
                s, r, c = copy.deepcopy((state, rows, covers))
                if mutation == "unverified":
                    s["batches"]["batch-000001"]["verification"]["audit_ok"] = False
                elif mutation == "captured_only":
                    s["batches"] = {}
                elif mutation == "wrong_id":
                    c[0]["id"] = 2
                elif mutation == "bad_cover":
                    c[0]["thumbnail"] = None
                else:
                    r[0]["resource_last_seen"] = "2026-10-04T01:00:01Z"
                result = goalctl.summarize(s, r, c, target=1)
                self.assertEqual(result["qualified_programs_lower_bound"], 0)
                self.assertFalse(result["data_gates_satisfied"])

    def test_other_source_cover_cannot_qualify_or_hide_backlog(self):
        state, rows, covers = fixture()
        covers[0]["source"] = "樱花动漫"
        result = goalctl.summarize(state, rows, covers, target=1)
        self.assertEqual(result["qualified_programs_lower_bound"], 0)
        self.assertEqual(result["cover_pending"], 1)

    def test_repeated_url_counts_once_and_does_not_become_new_again(self):
        state, rows, covers = fixture()
        repeated = copy.deepcopy(state["batches"]["batch-000001"])
        repeated["acknowledged_at"] = "2026-10-04T02:00:00Z"
        repeated["thread"] = "other"
        repeated["verification"]["batch_id"] = "batch-000002"
        state["batches"]["batch-000002"] = repeated
        result = goalctl.summarize(state, rows, covers, target=1,
                                   since="2026-10-04T01:30:00Z", peers=[("01", "worker")])
        self.assertEqual(result["qualified_programs_lower_bound"], 1)
        self.assertEqual(result["qualified_since"], 0)
        self.assertEqual(result["threads"]["01"]["qualified_since"], 0)
        result = goalctl.summarize(state, rows, covers, since="2026-10-04T00:30:00Z",
                                   peers=[("01", "worker")])
        self.assertEqual(result["threads"]["01"]["qualified_since"], 1)

    def test_ready_batch_blocks_completion_even_when_target_is_met(self):
        state, rows, covers = fixture()
        state["batches"]["batch-000002"] = {"state": "ready"}
        result = goalctl.summarize(state, rows, covers, target=1)
        self.assertEqual(result["qualified_programs_lower_bound"], 1)
        self.assertEqual(result["inflight_batches"], {"ready": 1})
        self.assertFalse(result["data_gates_satisfied"])

    def test_raw_and_failed_results_never_count_as_captured(self):
        state, rows, covers = fixture()
        state["batches"]["batch-000001"]["results"][0]["outcome"] = "raw"
        self.assertEqual(goalctl.summarize(state, rows, covers)["qualified_programs_lower_bound"], 0)

    def test_bound_legacy_audit_can_use_current_independent_cover_validation(self):
        state, rows, covers = fixture()
        state["batches"]["batch-000001"]["verification"] = {
            "batch_id": "batch-000001", "verified": True, "verifier": "batchctl finish",
            "captured_pages_passed": 1, "resources_checked": 2, "error_count": 0,
        }
        self.assertEqual(goalctl.summarize(state, rows, covers)["qualified_programs_lower_bound"], 1)
        state["batches"]["batch-000001"]["verification"]["captured_pages_passed"] = 2
        self.assertEqual(goalctl.summarize(state, rows, covers)["qualified_programs_lower_bound"], 0)

    def test_changed_state_prevents_a_completion_candidate(self):
        state, rows, covers = fixture()
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "state.json"
            path.write_text(json.dumps(state), encoding="utf-8")

            def changed_feed():
                path.write_text(json.dumps({**state, "changed": True}), encoding="utf-8")
                return covers

            with mock.patch.object(goalctl, "cover_feed", side_effect=changed_feed), \
                    mock.patch.object(goalctl, "database_rows", return_value=rows):
                result = goalctl.check(path, target=1)
            self.assertTrue(result["sampling_changed"])
            self.assertFalse(result["data_gates_satisfied"])

    def test_inconsistent_cover_snapshot_fails_without_counts(self):
        with mock.patch.object(goalctl.coverctl, "status_snapshot", side_effect=goalctl.coverctl.StatusError("inconsistent_cover_snapshot")) as status, \
                mock.patch.object(goalctl, "database_rows") as database:
            with self.assertRaises(goalctl.coverctl.StatusError):
                goalctl.cover_feed()
            database.assert_not_called()
            self.assertEqual(status.call_count, 3)

    def test_retry_discards_every_row_from_the_inconsistent_snapshot(self):
        snapshots = iter((False, True))

        def scan(page_loader):
            stable = next(snapshots)
            page_loader(1, 60)
            if not stable:
                raise goalctl.coverctl.StatusError("inconsistent_status_total")

        with mock.patch.object(goalctl.coverctl, "status_snapshot", side_effect=scan), \
                mock.patch.object(goalctl.coverctl, "request_status_page", side_effect=[
                    {"items": [{"id": 1}]}, {"items": [{"id": 2}]}]):
            self.assertEqual(goalctl.cover_feed(), [{"id": 2}])

    def test_database_query_is_read_only_and_quotes_the_source(self):
        with mock.patch.object(goalctl.workflowctl.dbctl, "sql_json", return_value=[]) as sql:
            self.assertEqual(goalctl.database_rows("source'quoted"), [])
        query = sql.call_args.args[0]
        self.assertIn("BEGIN READ ONLY", query)
        self.assertIn("ROLLBACK", query)
        self.assertIn("source''quoted", query)

    def test_audit_for_a_page_outside_its_assigned_batch_cannot_count(self):
        state, rows, covers = fixture()
        state["batches"]["batch-000001"]["item_urls"] = ["https://www.xxmanmi.com/video2/2.html"]
        self.assertEqual(goalctl.summarize(state, rows, covers)["qualified_programs_lower_bound"], 0)


if __name__ == "__main__":
    unittest.main()
