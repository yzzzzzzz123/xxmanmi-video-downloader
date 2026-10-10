"""Search claim identity, recovery and cleanup stay intact after relocation."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import shared_discovery


class SharedDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name) / "title-discovery"

    def tearDown(self):
        self.temporary.cleanup()

    def claim(self, number, owner="old-owner", task_id="old-task"):
        return shared_discovery.claim_search(
            self.root, f"节目{number}", "量子", owner, task_id,
        )

    def evidence(self, claims, **overrides):
        value = {
            "old_owner": "old-owner",
            "new_owner": "new-owner",
            "new_task_id": "new-task",
            "old_owner_confirmed": True,
            "new_owner_confirmed": True,
            "user_authorized": True,
            "old_owner_stopped": True,
            "claims": [
                {
                    "query": claim["claim"]["query"],
                    "channel": claim["claim"]["channel"],
                    "claim_sha256": claim["claim_sha256"],
                }
                for claim in claims
            ],
        }
        value.update(overrides)
        return value

    def handoff(self, evidence):
        return shared_discovery.handoff_search(
            self.root, "old-owner", "new-owner", "new-task", evidence,
        )

    def abandonment_audit(self):
        return {
            "reason": "abandoned_unknown_action",
            "browser_host_terminated": True,
            "browser_tab_exists": False,
            "original_module_instance_available": False,
            "active_action_capability_available": False,
            "action_state": "unknown",
            "replay_authorized": False,
            "logs_stopped": True,
            "journals": [],
            "browser_evidence_exists": False,
            "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "summary": "terminal host and unknown action",
        }

    def unverifiable_result_audit(self):
        return {
            "reason": "quarantined_unverifiable_result",
            "browser_host_terminated": False,
            "browser_tab_exists": True,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable",
            "replay_authorized": False,
        }

    def invalid_plan_audit(self):
        return {
            "reason": "cancelled_pre_search_invalid_plan",
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": True,
            "action_state": "not_started_invalid_plan", "action_attempts": 0,
            "replay_authorized": False,
        }

    def uninspectable_input_audit(self):
        return {
            "reason": "abandoned_uninspectable_input_action",
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": False,
            "action_state": "input_fill_unknown_unsubmitted", "action_attempts": 1,
            "submit_attempts": 0, "replay_authorized": False,
        }

    def uninspectable_click_audit(self):
        return {
            "reason": "abandoned_uninspectable_click_action",
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": False,
            "action_state": "search_click_unknown", "action_attempts": 1,
            "search_click_attempted": True, "search_click_completed": "unknown",
            "replay_authorized": False,
        }

    def live_owner_click_unknown_audit(self):
        return {
            "reason": "abandoned_live_owner_click_unknown",
            "browser_host_terminated": False, "browser_tab_exists": True,
            "browser_host_session_id": "old-owner", "browser_host_thread_id": "old-owner",
            "browser_host_context_reset": True,
            "browser_tab_session_id": "old-owner", "browser_tab_owner_thread_id": "old-owner",
            "browser_tab_readable": True,
            "original_module_instance_available": False,
            "action_state": "search_click_unknown", "action_attempts": 1,
            "helper_call_attempts": 1,
            "search_click_attempted": True, "search_click_completed": "unknown",
            "helper_call_timed_out": True, "action_context_reset": True,
            "action_id_recoverable": False,
            "execute_receipt": False, "inspect_receipt": False, "finalize_receipt": False,
            "journals_before": [], "journals_after": [],
            "manual_review_required": True, "replay_authorized": False,
            "browser_evidence_exists": False, "action_result_receipt_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "capture_lock_exists": False,
            "dom_summary": {"input_value": "节目309", "selected_channels": ["量子"]},
        }

    def unavailable_before_start_audit(self):
        return {
            "reason": "cancelled_pre_search_session_unavailable",
            "browser_host_terminated": False, "browser_tab_exists": False,
            "original_module_instance_available": False,
            "action_state": "not_started_session_unavailable", "action_attempts": 0,
            "replay_authorized": False,
        }

    def uninspectable_navigation_audit(self):
        return {
            "reason": "abandoned_uninspectable_navigation_action",
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": False,
            "action_state": "navigation_unknown", "action_attempts": 1,
            "navigation_attempted": True, "navigation_completed": "unknown",
            "replay_authorized": False,
        }

    def test_claim_search_contract_remains_compatible(self):
        first = self.claim(1)
        self.assertEqual(first["reason"], "claimed")
        self.assertRegex(first["claim_sha256"], r"^[0-9a-f]{64}$")
        same = self.claim(1)
        self.assertEqual(same["reason"], "already_owned")
        other = self.claim(1, owner="other", task_id="other-task")
        self.assertFalse(other["ok"])
        self.assertEqual(other["reason"], "already_claimed")

    def test_retired_chart_round_command_is_rejected_without_writes(self):
        with mock.patch.object(sys, 'argv', [
                'shared_discovery.py', 'start-round', '--root', str(self.root)]), \
                mock.patch.object(sys, 'stderr'), self.assertRaises(SystemExit) as stopped:
            shared_discovery.main()
        self.assertEqual(stopped.exception.code, 2)
        self.assertFalse(self.root.exists())

    def test_abandoned_unknown_action_is_terminal_and_cannot_be_handed_off(self):
        claim = self.claim(300)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目300", "量子",
            claim["claim_sha256"], "a" * 64, self.abandonment_audit(),
        )
        self.assertEqual(result["reason"], "abandoned_unknown_action")
        inspected = shared_discovery.inspect_claim(self.root, "节目300", "量子")
        self.assertEqual(inspected["reason"], "abandoned_unknown_action")
        self.assertEqual(
            shared_discovery.claim_search(
                self.root, "节目300", "量子", "old-owner", "old-task",
            )["reason"],
            "abandoned_unknown_action",
        )
        with self.assertRaisesRegex(ValueError, "cannot be handed off"):
            self.handoff(self.evidence([claim]))

    def test_unverifiable_completed_result_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(302)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目302", "量子",
            claim["claim_sha256"], "b" * 64, self.unverifiable_result_audit(),
        )
        self.assertEqual(result["reason"], "quarantined_unverifiable_result")
        inspected = shared_discovery.inspect_claim(self.root, "节目302", "量子")
        self.assertEqual(inspected["reason"], "quarantined_unverifiable_result")
        self.assertEqual(
            shared_discovery.claim_search(
                self.root, "节目302", "量子", "old-owner", "old-task",
            )["reason"],
            "quarantined_unverifiable_result",
        )

    def test_completed_public_ui_allows_module_loss_without_replay(self):
        claim = self.claim(303)
        audit = self.unverifiable_result_audit()
        audit.update({"original_module_instance_available": False, "public_ui_completed": True})
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目303", "量子",
            claim["claim_sha256"], "c" * 64, audit,
        )
        self.assertEqual(result["reason"], "quarantined_unverifiable_result")

    def test_invalid_plan_before_ui_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(304)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目304", "量子",
            claim["claim_sha256"], "d" * 64, self.invalid_plan_audit(),
        )
        self.assertEqual(result["reason"], "cancelled_pre_search_invalid_plan")
        self.assertEqual(
            shared_discovery.claim_search(self.root, "节目304", "量子", "old-owner", "old-task")["reason"],
            "cancelled_pre_search_invalid_plan",
        )

    def test_uninspectable_input_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(305)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目305", "量子",
            claim["claim_sha256"], "e" * 64, self.uninspectable_input_audit(),
        )
        self.assertEqual(result["reason"], "abandoned_uninspectable_input_action")

    def test_uninspectable_click_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(306)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目306", "量子",
            claim["claim_sha256"], "f" * 64, self.uninspectable_click_audit(),
        )
        self.assertEqual(result["reason"], "abandoned_uninspectable_click_action")

    def test_live_owner_unknown_click_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(309)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目309", "量子",
            claim["claim_sha256"], "a" * 64, self.live_owner_click_unknown_audit(),
        )
        self.assertEqual(result["reason"], "abandoned_live_owner_click_unknown")
        replay = shared_discovery.claim_search(self.root, "节目309", "量子", "old-owner", "old-task")
        self.assertEqual(replay["reason"], "abandoned_live_owner_click_unknown")

    def test_session_unavailable_before_start_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(307)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目307", "量子",
            claim["claim_sha256"], "0" * 64, self.unavailable_before_start_audit(),
        )
        self.assertEqual(result["reason"], "cancelled_pre_search_session_unavailable")

    def test_uninspectable_navigation_is_terminal_and_cannot_be_reclaimed(self):
        claim = self.claim(308)
        result = shared_discovery.abandon_search(
            self.root, "old-owner", "old-task", "节目308", "量子",
            claim["claim_sha256"], "1" * 64, self.uninspectable_navigation_audit(),
        )
        self.assertEqual(result["reason"], "abandoned_uninspectable_navigation_action")

    def test_handoff_is_one_atomic_overlay_and_preserves_original_claims(self):
        claims = [self.claim(1), self.claim(2)]
        original_bytes = {claim["path"]: Path(claim["path"]).read_bytes() for claim in claims}
        result = self.handoff(self.evidence(claims))
        self.assertTrue(result["ok"])
        self.assertEqual(result["count"], 2)

        ledger = json.loads(Path(result["ledger"]).read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["transfers"]), 1)
        self.assertEqual(len(ledger["transfers"][0]["claims"]), 2)
        for path, contents in original_bytes.items():
            self.assertEqual(Path(path).read_bytes(), contents)

        current = shared_discovery.inspect_claim(self.root, "节目1", "量子")
        self.assertEqual(current["original_owner"], "old-owner")
        self.assertEqual(current["original_task_id"], "old-task")
        self.assertEqual(current["claim"]["owner"], "new-owner")
        self.assertEqual(current["claim"]["task_id"], "new-task")
        self.assertIn("handoff_id", current["claim"])
        self.assertEqual(
            shared_discovery.claim_search(
                self.root, "节目1", "量子", "old-owner", "old-task",
            )["reason"],
            "already_claimed",
        )
        self.assertEqual(
            shared_discovery.claim_search(
                self.root, "节目1", "量子", "new-owner", "new-task",
            )["reason"],
            "already_owned",
        )

    def test_handoff_requires_stop_both_confirmations_and_user_authorization(self):
        claim = self.claim(3)
        for field in (
            "old_owner_confirmed", "new_owner_confirmed",
            "user_authorized", "old_owner_stopped",
        ):
            evidence = self.evidence([claim])
            evidence[field] = False
            with self.assertRaisesRegex(ValueError, field):
                self.handoff(evidence)
            self.assertFalse((self.root / "search-claim-handoffs.json").exists())
        with self.assertRaisesRegex(ValueError, "must be different"):
            shared_discovery.handoff_search(
                self.root, "old-owner", "old-owner", "new-task",
                self.evidence([claim], new_owner="old-owner"),
            )

    def test_handoff_accepts_explicit_user_forced_archived_session(self):
        claim = self.claim(301)
        evidence = self.evidence([claim])
        evidence.update({
            "old_owner_confirmed": False,
            "user_forced_stop": True,
            "session_archived": True,
            "archived_session_id": "old-owner",
            "archive_method": "supported session archive command",
        })
        with self.assertRaisesRegex(ValueError, "explicitly authorized"):
            self.handoff(evidence)
        result = shared_discovery.handoff_search(
            self.root, "old-owner", "new-owner", "new-task", evidence,
            force_user_authorized=True,
        )
        self.assertTrue(result["ok"])
        ledger = json.loads(Path(result["ledger"]).read_text(encoding="utf-8"))
        self.assertFalse(ledger["transfers"][0]["old_owner_confirmed"])
        self.assertTrue(ledger["transfers"][0]["user_forced_stop"])

    def test_wrong_owner_or_changed_hash_rejects_whole_batch(self):
        claims = [self.claim(4), self.claim(5)]
        evidence = self.evidence(claims)
        evidence["claims"][1]["claim_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "hash changed"):
            self.handoff(evidence)
        self.assertFalse((self.root / "search-claim-handoffs.json").exists())
        for number in (4, 5):
            self.assertEqual(
                shared_discovery.inspect_claim(self.root, f"节目{number}", "量子")["claim"]["owner"],
                "old-owner",
            )

        wrong_owner = self.evidence(claims, old_owner="someone-else")
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.handoff(wrong_owner)

    def test_missing_duplicate_or_oversized_claim_evidence_is_rejected(self):
        claim = self.claim(6)
        missing = self.evidence([claim])
        missing["claims"][0].pop("claim_sha256")
        with self.assertRaisesRegex(ValueError, "claim_sha256"):
            self.handoff(missing)

        duplicate = self.evidence([claim, claim])
        with self.assertRaisesRegex(ValueError, "duplicate"):
            self.handoff(duplicate)

        many = [self.claim(number) for number in range(10, 19)]
        with self.assertRaisesRegex(ValueError, "between 1 and 8"):
            self.handoff(self.evidence(many))

    def test_invalid_claim_or_ledger_is_never_treated_as_available(self):
        claim = self.claim(20)
        Path(claim["path"]).write_text("{half", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "incomplete or invalid"):
            self.handoff(self.evidence([claim]))

        clean_root = Path(self.temporary.name) / "invalid-ledger"
        clean_claim = shared_discovery.claim_search(
            clean_root, "节目21", "量子", "old-owner", "old-task",
        )
        (clean_root / "search-claim-handoffs.json").write_text("{}", encoding="utf-8")
        blocked = shared_discovery.claim_search(
            clean_root, "节目22", "量子", "old-owner", "old-task",
        )
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["reason"], "claim_incomplete_review_required")
        self.assertFalse((clean_root / "claims" / f"{shared_discovery.claim_digest('节目22', '量子')}.json").exists())
        self.assertTrue(Path(clean_claim["path"]).exists())

    def test_claim_and_handoff_share_one_lock_without_stale_lock_removal(self):
        self.root.mkdir(parents=True)
        lock = self.root / ".search-claims.lock"
        shared_discovery.write_exclusive(lock, {"pid": 999, "token": "foreign"})
        with self.assertRaisesRegex(TimeoutError, "unknown lock was not removed"):
            shared_discovery.claim_search(
                self.root, "节目30", "量子", "old-owner", "old-task", lock_timeout=0,
            )
        self.assertTrue(lock.exists())
        lock.unlink()

    def test_atomic_replace_failure_keeps_previous_authority_complete(self):
        first = self.claim(31)
        self.handoff(self.evidence([first]))
        ledger = self.root / "search-claim-handoffs.json"
        before = ledger.read_bytes()

        second = self.claim(32)
        evidence = self.evidence([second])
        with mock.patch("shared_discovery.os.replace", side_effect=OSError("simulated")):
            with self.assertRaisesRegex(OSError, "simulated"):
                self.handoff(evidence)
        self.assertEqual(ledger.read_bytes(), before)
        self.assertEqual(
            shared_discovery.inspect_claim(self.root, "节目32", "量子")["claim"]["owner"],
            "old-owner",
        )

    def test_successive_handoffs_keep_a_continuous_history(self):
        claim = self.claim(40)
        self.handoff(self.evidence([claim]))
        current = shared_discovery.inspect_claim(self.root, "节目40", "量子")
        second_evidence = {
            "old_owner": "new-owner", "new_owner": "third-owner",
            "new_task_id": "third-task", "old_owner_confirmed": True,
            "new_owner_confirmed": True, "user_authorized": True,
            "old_owner_stopped": True,
            "claims": [{
                "query": "节目40", "channel": "量子",
                "claim_sha256": current["claim_sha256"],
            }],
        }
        shared_discovery.handoff_search(
            self.root, "new-owner", "third-owner", "third-task", second_evidence,
        )
        final = shared_discovery.inspect_claim(self.root, "节目40", "量子")
        self.assertEqual(final["claim"]["owner"], "third-owner")
        ledger = json.loads((self.root / "search-claim-handoffs.json").read_text(encoding="utf-8"))
        self.assertEqual(len(ledger["transfers"]), 2)

    def test_cli_handoff_and_inspect_accept_utf8_bom_evidence(self):
        claim = self.claim(50)
        evidence_path = Path(self.temporary.name) / "handoff.json"
        evidence_path.write_text(
            json.dumps(self.evidence([claim]), ensure_ascii=False), encoding="utf-8-sig",
        )
        script = str(Path(shared_discovery.__file__))
        handoff = subprocess.run([
            sys.executable, script, "handoff-search", "--root", str(self.root),
            "--old-owner", "old-owner", "--new-owner", "new-owner",
            "--new-task-id", "new-task", "--evidence", str(evidence_path),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(handoff.returncode, 0, handoff.stderr)
        self.assertEqual(json.loads(handoff.stdout)["reason"], "handed_off")

        inspected = subprocess.run([
            sys.executable, script, "inspect-claim", "--root", str(self.root),
            "--query", "节目50", "--channel", "量子",
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(inspected.returncode, 0, inspected.stderr)
        self.assertEqual(json.loads(inspected.stdout)["claim"]["owner"], "new-owner")


if __name__ == "__main__":
    unittest.main()
