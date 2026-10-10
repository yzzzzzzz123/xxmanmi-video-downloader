import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordctl


def observed_item(number, title=None):
    return {
        "url": f"https://www.example.invalid/video2/{number}.html",
        "title": title or f"节目{number}",
        "status": "",
        "source": "真实UI渠道",
        "category": "测试",
        "query": title or f"节目{number}",
    }


def verification_for(claim, cover_count=None, captured_urls=None, **overrides):
    if cover_count is None:
        cover_count = len(claim["items"])
    value = {
        "batch_id": claim["batch"],
        "verified": True,
        "verifier": "batchctl/per-item audit",
        "work": claim["work"],
        "sync_ok": True,
        "audit_ok": True,
        "evidence": {
            "method": "batchctl finish plus per-item raw review",
            "checked_items": len(claim["items"]),
            "summary": "all assigned items were synced and checked against their stopped journals",
        },
    }
    if cover_count:
        if captured_urls is None:
            captured_urls = [item["url"] for item in claim["items"][:cover_count]]
        if len(captured_urls) != cover_count:
            raise ValueError("test fixture captured_urls must match cover_count")
        receipt = Path(claim["work"]) / f"{claim['batch']}-covers.json"
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt_bytes = json.dumps(
            {
                "version": 1,
                "batch_id": claim["batch"],
                "complete": True,
                "cover_checked_items": cover_count,
                "items": [{
                    "source_url": url,
                    "program_id": index,
                    "status": "selected",
                    "selection": "existing",
                    "artifact": {
                        "sha256": hashlib.sha256(url.encode("utf-8")).hexdigest(),
                    },
                } for index, url in enumerate(captured_urls, 1)],
            },
            ensure_ascii=False, sort_keys=True,
        ).encode("utf-8")
        receipt.write_bytes(receipt_bytes)
        value.update({
            "cover_ok": True,
            "cover_checked_items": cover_count,
            "cover_receipt": str(receipt),
            "cover_receipt_sha256": hashlib.sha256(receipt_bytes).hexdigest(),
        })
    value.update(overrides)
    return value


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "coord-state.json"
        self.work_main = self.root / "worker-main"
        self.work_one = self.root / "worker-01"
        self.work_two = self.root / "worker-02"
        coordctl.init_state(self.state, "main", str(self.work_main))

    def tearDown(self):
        self.temporary.cleanup()

    def register_collectors(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        coordctl.register_thread(self.state, "worker-02", "collector", str(self.work_two))

    def test_transient_windows_replace_retries_same_bytes_under_owned_lock(self):
        replace = coordctl.os.replace
        attempts = []
        def sharing_error(source, destination):
            self.assertTrue(self.state.with_name(self.state.name + ".lock").exists())
            attempts.append((source, Path(source).read_bytes()))
            if len(attempts) < 3:
                error = PermissionError("Windows file temporarily unavailable")
                error.winerror = 5 if len(attempts) == 1 else 32
                raise error
            return replace(source, destination)
        with mock.patch.object(coordctl.os, "replace", side_effect=sharing_error), \
                mock.patch.object(coordctl.time, "sleep"):
            coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.assertEqual(len(attempts), 3)
        self.assertEqual(len(set(attempts)), 1)
        self.assertIn("worker-01", json.loads(self.state.read_bytes())["threads"])
        self.assertFalse(list(self.root.glob("coord-state.json.*.tmp")))

    def test_persistent_windows_replace_error_preserves_state_and_cleans_own_temp(self):
        before = self.state.read_bytes()
        error = PermissionError("Windows access denied")
        error.winerror = 5
        with mock.patch.object(coordctl.os, "replace", side_effect=error) as replace, \
                mock.patch.object(coordctl.time, "sleep"):
            with self.assertRaises(PermissionError):
                coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.assertEqual(replace.call_count, 11)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(list(self.root.glob("coord-state.json.*.tmp")))
        self.assertFalse(self.state.with_name(self.state.name + ".lock").exists())

    def test_non_windows_permission_error_is_not_retried(self):
        with mock.patch.object(coordctl.os, "replace", side_effect=PermissionError("denied")) as replace, \
                mock.patch.object(coordctl.time, "sleep") as sleep:
            with self.assertRaises(PermissionError):
                coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.assertEqual(replace.call_count, 1)
        sleep.assert_not_called()

    def enqueue(self, *items):
        return coordctl.enqueue_items(self.state, "main", list(items))

    def test_init_allows_more_than_three_peers_but_rejects_work_aliases(self):
        status = coordctl.status_state(self.state)
        self.assertEqual(status["registered_workers"], 1)
        self.assertEqual(status["coordination_mode"], "peer")
        self.assertEqual(status["reporting_mode"], "per_thread")
        self.assertEqual(status["threads"][0]["role"], "peer")
        self.assertIsNone(status["worker_limit"])
        coordctl.register_thread(
            self.state, "worker-01", work=str(self.work_one / "child" / ".."),
        )
        with self.assertRaisesRegex(coordctl.CoordinationError, "independent worker"):
            coordctl.register_thread(self.state, "worker-alias", "collector", str(self.work_one))
        coordctl.register_thread(self.state, "worker-02", "collector", str(self.work_two))
        for number in range(3, 8):
            coordctl.register_thread(
                self.state, f"worker-{number:02d}", "collector",
                str(self.root / f"worker-{number:02d}"),
            )
        self.assertEqual(coordctl.status_state(self.state)["registered_workers"], 8)

    def test_unlimit_migrates_legacy_state_without_changing_batches(self):
        self.enqueue(observed_item(703))
        claim = coordctl.claim_batch(self.state, "main", 1)
        old = json.loads(self.state.read_text(encoding="utf-8"))
        old["max_workers"] = 3
        self.state.write_text(json.dumps(old), encoding="utf-8")
        migrated = coordctl.unlimit_state(self.state)
        self.assertEqual(migrated["previous_limit"], 3)
        after = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("max_workers", after)
        for key in ("threads", "queue", "items", "batches", "next_batch"):
            self.assertEqual(after[key], old[key])
        self.assertEqual(coordctl.current_batch(self.state, "main")["active_batch"], claim["batch"])
        self.assertIsNone(coordctl.unlimit_state(self.state)["previous_limit"])

    def test_single_peer_can_complete_its_own_full_batch_flow(self):
        added = coordctl.enqueue_items(self.state, "main", [observed_item(701)])
        self.assertEqual(added["added"], 1)
        claim = coordctl.claim_batch(self.state, "main", 1)
        coordctl.ready_batch(self.state, "main", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
        ], True)
        acknowledged = coordctl.acknowledge_batch(
            self.state, "main", claim["batch"], verification_for(claim),
        )
        self.assertTrue(acknowledged["ok"])
        current = coordctl.current_batch(self.state, "main")
        self.assertEqual(current["status"], "idle")
        self.assertEqual(current["items"], [])

    def test_new_peer_defaults_and_legacy_state_role_are_compatible(self):
        registered = coordctl.register_thread(
            self.state, "worker-01", work=str(self.work_one),
        )
        self.assertEqual(registered["role"], "peer")

        self.enqueue(observed_item(702))
        claim = coordctl.claim_batch(self.state, "main", 1)
        coordctl.ready_batch(self.state, "main", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
        ], True)
        coordctl.acknowledge_batch(
            self.state, "main", claim["batch"], verification_for(claim),
        )

        state = json.loads(self.state.read_text(encoding="utf-8"))
        state.pop("coordination_mode")
        state.pop("reporting_mode")
        state["coordinator"] = "main"
        state["threads"]["main"]["role"] = "coordinator"
        state["threads"]["worker-01"]["role"] = "collector"
        batch_history = state["batches"].copy()
        self.state.write_text(json.dumps(state), encoding="utf-8")

        status = coordctl.status_state(self.state)
        self.assertEqual(status["coordination_mode"], "peer")
        self.assertEqual(status["reporting_mode"], "per_thread")
        self.assertNotIn("coordinator", status)
        self.assertNotIn("progress_reporter", status)
        self.assertEqual({row["role"] for row in status["threads"]}, {"peer"})
        current = coordctl.current_batch(self.state, "main")
        self.assertEqual(current["role"], "peer")
        repeat = coordctl.register_thread(
            self.state, "main", work=str(self.work_main),
        )
        self.assertFalse(repeat["registered"])
        self.assertEqual(repeat["role"], "peer")
        preserved = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(preserved["coordinator"], "main")
        self.assertEqual(preserved["threads"]["main"]["role"], "coordinator")
        self.assertEqual(preserved["threads"]["worker-01"]["role"], "collector")
        self.assertEqual(preserved["batches"], batch_history)

    def test_physical_work_resolution_rejects_junction_or_symlink_aliases(self):
        physical = str(self.root / "physical-worker")
        alias_one = str(self.root / "junction-one")
        alias_two = str(self.root / "symlink-two")
        realpath = coordctl.os.path.realpath

        def resolved(path):
            return physical if path in {alias_one, alias_two} else realpath(path)

        with mock.patch("coordctl.os.path.realpath", side_effect=resolved):
            coordctl.register_thread(self.state, "worker-01", "collector", alias_one)
            with self.assertRaisesRegex(coordctl.CoordinationError, "independent worker"):
                coordctl.register_thread(self.state, "worker-02", "collector", alias_two)

    def test_enqueue_deduplicates_urls_not_titles(self):
        same_title = "同名不同详情页"
        first = observed_item(1, same_title)
        second = observed_item(2, same_title)
        duplicate = {**first, "url": first["url"] + "#fragment"}
        result = self.enqueue(first, second, duplicate)
        self.assertEqual(result["added"], 2)
        self.assertEqual(result["duplicates"], 1)
        claim = coordctl.claim_batch(self.state, "main", 8)
        self.assertEqual(len(claim["items"]), 2)
        self.assertEqual({item["title"] for item in claim["items"]}, {same_title})

    def test_registered_peer_can_enqueue_but_unregistered_actor_cannot(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        added = coordctl.enqueue_items(self.state, "worker-01", [observed_item(8)])
        self.assertEqual(added["added"], 1)
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.enqueue_items(self.state, "unknown-peer", [observed_item(9)])
        self.assertEqual(self.state.read_bytes(), state_before)

    def test_claim_limit_and_one_active_batch_per_thread(self):
        self.enqueue(*(observed_item(number) for number in range(1, 5)))
        with self.assertRaisesRegex(coordctl.CoordinationError, "between 1 and 8"):
            coordctl.claim_batch(self.state, "main", 9)
        claim = coordctl.claim_batch(self.state, "main", 2)
        self.assertEqual(len(claim["items"]), 2)
        with self.assertRaisesRegex(coordctl.CoordinationError, "acknowledged or explicitly released"):
            coordctl.claim_batch(self.state, "main", 1)

    def test_current_returns_only_the_threads_resumable_batch(self):
        self.enqueue(*(observed_item(number) for number in range(1, 4)))
        idle = coordctl.current_batch(self.state, "main")
        self.assertEqual(idle["status"], "idle")
        self.assertEqual(idle["items"], [])
        claim = coordctl.claim_batch(self.state, "main", 2)
        current = coordctl.current_batch(self.state, "main")
        self.assertEqual(current["active_batch"], claim["batch"])
        self.assertEqual(current["status"], "claimed")
        self.assertEqual(current["work"], str(self.work_main.resolve()))
        self.assertEqual(current["items"], claim["items"])
        self.assertEqual(len(current["items"]), 2)
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.current_batch(self.state, "unknown-thread")

    def test_ready_requires_complete_results_and_stopped_logs(self):
        self.enqueue(observed_item(1), observed_item(2))
        claim = coordctl.claim_batch(self.state, "main", 2)
        one_result = [{"url": claim["items"][0]["url"], "outcome": "captured"}]
        with self.assertRaisesRegex(coordctl.CoordinationError, "writing has stopped"):
            coordctl.ready_batch(self.state, "main", claim["batch"], one_result, False)
        with self.assertRaisesRegex(coordctl.CoordinationError, "cover every claimed URL"):
            coordctl.ready_batch(self.state, "main", claim["batch"], one_result, True)
        results = [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
            {"url": claim["items"][1]["url"], "outcome": "raw", "note": "公开编号"},
        ]
        ready = coordctl.ready_batch(self.state, "main", claim["batch"], results, True)
        self.assertEqual(ready["outcomes"], {"captured": 1, "raw": 1})
        with self.assertRaisesRegex(coordctl.CoordinationError, "acknowledged or explicitly released"):
            coordctl.claim_batch(self.state, "main", 1)

    def test_batch_owner_can_self_ack_with_strong_external_verification(self):
        self.register_collectors()
        self.enqueue(observed_item(1), observed_item(2))
        claim = coordctl.claim_batch(self.state, "worker-01", 2)
        coordctl.ready_batch(self.state, "worker-01", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
            {"url": claim["items"][1]["url"], "outcome": "failed"},
        ], True)
        invalid = {"batch_id": claim["batch"], "verified": False, "verifier": "batchctl"}
        with self.assertRaisesRegex(coordctl.CoordinationError, "verified=true"):
            coordctl.acknowledge_batch(self.state, "worker-01", claim["batch"], invalid)
        acknowledged = coordctl.acknowledge_batch(
            self.state, "worker-01", claim["batch"], verification_for(claim, cover_count=1),
        )
        self.assertEqual(acknowledged["captured_acknowledged"], 1)
        self.assertEqual(acknowledged["manual_review"], 1)
        self.assertIsNone(coordctl.claim_batch(self.state, "worker-01", 1)["batch"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["batches"][claim["batch"]]["acknowledged_by"], "worker-01")

    def test_registered_peer_can_ack_another_owner_but_unregistered_cannot(self):
        self.register_collectors()
        self.enqueue(observed_item(11))
        claim = coordctl.claim_batch(self.state, "worker-01", 1)
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "owned by this thread"):
            coordctl.ready_batch(self.state, "worker-02", claim["batch"], [
                {"url": claim["items"][0]["url"], "outcome": "captured"},
            ], True)
        self.assertEqual(self.state.read_bytes(), state_before)
        coordctl.ready_batch(self.state, "worker-01", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
        ], True)
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.acknowledge_batch(
                self.state, "unknown-peer", claim["batch"], verification_for(claim),
            )
        self.assertEqual(self.state.read_bytes(), state_before)
        coordctl.acknowledge_batch(
            self.state, "worker-02", claim["batch"], verification_for(claim),
        )
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["batches"][claim["batch"]]["acknowledged_by"], "worker-02")

    def test_ack_rejects_wrong_batch_work_or_missing_external_evidence_atomically(self):
        self.register_collectors()
        self.enqueue(observed_item(21))
        claim = coordctl.claim_batch(self.state, "worker-01", 1)
        coordctl.ready_batch(self.state, "worker-01", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
        ], True)
        invalid_records = [
            verification_for(claim, batch_id="batch-999999"),
            verification_for(claim, work=str(self.work_two)),
            verification_for(claim, sync_ok=False),
            verification_for(claim, audit_ok=False),
            verification_for(claim, evidence={}),
            verification_for(claim, evidence={
                "method": "batchctl", "checked_items": 2, "summary": "wrong scope",
            }),
        ]
        for invalid_record in invalid_records:
            state_before = self.state.read_bytes()
            with self.assertRaises(coordctl.CoordinationError):
                coordctl.acknowledge_batch(
                    self.state, "worker-02", claim["batch"], invalid_record,
                )
            self.assertEqual(self.state.read_bytes(), state_before)

    def test_ack_requires_exact_cover_receipt_for_captured_results(self):
        self.enqueue(observed_item(22), observed_item(23))
        claim = coordctl.claim_batch(self.state, "main", 2)
        coordctl.ready_batch(self.state, "main", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
            {"url": claim["items"][1]["url"], "outcome": "raw"},
        ], True)
        base = verification_for(claim, cover_count=1)
        invalid = []
        for key in ("cover_ok", "cover_checked_items", "cover_receipt",
                    "cover_receipt_sha256"):
            value = copy.deepcopy(base)
            value.pop(key)
            invalid.append(value)
        invalid.extend([
            verification_for(claim, cover_count=1, cover_ok=False),
            verification_for(claim, cover_count=2),
            verification_for(claim, cover_count=1, cover_receipt=" "),
            verification_for(claim, cover_count=1, cover_receipt_sha256="not-a-hash"),
        ])
        for value in invalid:
            state_before = self.state.read_bytes()
            with self.assertRaises(coordctl.CoordinationError):
                coordctl.acknowledge_batch(self.state, "main", claim["batch"], value)
            self.assertEqual(self.state.read_bytes(), state_before)
        acknowledged = coordctl.acknowledge_batch(
            self.state, "main", claim["batch"], base,
        )
        self.assertEqual(acknowledged["captured_acknowledged"], 1)

    def test_ack_rejects_wrong_cover_path_digest_and_symlink_atomically(self):
        self.enqueue(observed_item(221))
        claim = coordctl.claim_batch(self.state, "main", 1)
        coordctl.ready_batch(self.state, "main", claim["batch"], [{
            "url": claim["items"][0]["url"], "outcome": "captured",
        }], True)
        valid = verification_for(claim)
        receipt = Path(valid["cover_receipt"])
        other = receipt.with_name("other-covers.json")
        other.write_bytes(receipt.read_bytes())
        invalid = [
            {**valid, "cover_receipt": str(other)},
            {**valid, "cover_receipt_sha256": "0" * 64},
        ]
        for value in invalid:
            state_before = self.state.read_bytes()
            with self.assertRaises(coordctl.CoordinationError):
                coordctl.acknowledge_batch(
                    self.state, "main", claim["batch"], value,
                )
            self.assertEqual(self.state.read_bytes(), state_before)
        with mock.patch.object(Path, "is_symlink", autospec=True, return_value=True):
            state_before = self.state.read_bytes()
            with self.assertRaisesRegex(coordctl.CoordinationError, "regular file"):
                coordctl.acknowledge_batch(
                    self.state, "main", claim["batch"], valid,
                )
            self.assertEqual(self.state.read_bytes(), state_before)
        receipt.unlink()
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "regular file"):
            coordctl.acknowledge_batch(
                self.state, "main", claim["batch"], valid,
            )
        self.assertEqual(self.state.read_bytes(), state_before)

    def test_ack_parses_cover_receipt_and_rejects_weak_or_mismatched_items(self):
        self.enqueue(observed_item(222), observed_item(223))
        claim = coordctl.claim_batch(self.state, "main", 2)
        coordctl.ready_batch(self.state, "main", claim["batch"], [{
            "url": item["url"], "outcome": "captured",
        } for item in claim["items"]], True)

        def weak(_document):
            return {"batch_id": claim["batch"]}

        def incomplete(document):
            document["complete"] = False
            return document

        def foreign_url(document):
            document["items"][0]["source_url"] = observed_item(999)["url"]
            return document

        def duplicate_url(document):
            document["items"][1]["source_url"] = document["items"][0]["source_url"]
            return document

        def duplicate_id(document):
            document["items"][1]["program_id"] = document["items"][0]["program_id"]
            return document

        def nonselected(document):
            document["items"][0]["status"] = "unavailable"
            return document

        def invalid_selection(document):
            document["items"][0]["selection"] = "cached"
            return document

        def invalid_artifact(document):
            document["items"][0]["artifact"] = {"sha256": "short"}
            return document

        for mutation in (
            weak, incomplete, foreign_url, duplicate_url, duplicate_id,
            nonselected, invalid_selection, invalid_artifact,
        ):
            with self.subTest(mutation=mutation.__name__):
                verification = verification_for(claim)
                receipt = Path(verification["cover_receipt"])
                document = json.loads(receipt.read_text(encoding="utf-8"))
                altered = mutation(document)
                data = json.dumps(
                    altered, ensure_ascii=False, sort_keys=True,
                ).encode("utf-8")
                receipt.write_bytes(data)
                verification["cover_receipt_sha256"] = hashlib.sha256(data).hexdigest()
                state_before = self.state.read_bytes()
                with self.assertRaises(coordctl.CoordinationError):
                    coordctl.acknowledge_batch(
                        self.state, "main", claim["batch"], verification,
                    )
                self.assertEqual(self.state.read_bytes(), state_before)

    def test_ack_with_no_captured_results_does_not_require_cover_evidence(self):
        self.enqueue(observed_item(24))
        claim = coordctl.claim_batch(self.state, "main", 1)
        coordctl.ready_batch(self.state, "main", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "failed"},
        ], True)
        acknowledged = coordctl.acknowledge_batch(
            self.state, "main", claim["batch"], verification_for(claim, cover_count=0),
        )
        self.assertEqual(acknowledged["captured_acknowledged"], 0)
        self.assertEqual(acknowledged["manual_review"], 1)

    def test_release_is_explicit_peer_action_and_never_requeues(self):
        self.register_collectors()
        self.enqueue(observed_item(1), observed_item(2))
        claim = coordctl.claim_batch(self.state, "worker-01", 2)
        with self.assertRaisesRegex(coordctl.CoordinationError, "nonempty explicit reason"):
            coordctl.release_batch(self.state, "main", claim["batch"], "")
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.release_batch(self.state, "unknown-peer", claim["batch"], "wrong actor", True)
        with self.assertRaisesRegex(coordctl.CoordinationError, "logs_stopped"):
            coordctl.release_batch(self.state, "worker-02", claim["batch"], "terminal stopped")
        released = coordctl.release_batch(
            self.state, "worker-02", claim["batch"], "terminal stopped", True,
        )
        self.assertEqual(released["requeued"], 0)
        status = coordctl.status_state(self.state)
        self.assertEqual(status["items"], {"manual_review": 2})
        self.assertNotIn("queued", status["items"])

    def test_rebind_work_preserves_old_batch_history_and_owner_identity(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.enqueue(observed_item(151))
        claim = coordctl.claim_batch(self.state, "worker-01", 1)
        coordctl.ready_batch(self.state, "worker-01", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "captured"},
        ], True)
        coordctl.acknowledge_batch(
            self.state, "main", claim["batch"], verification_for(claim),
        )
        batch_before_rebind = json.loads(self.state.read_text(encoding="utf-8"))["batches"][claim["batch"]]
        old_work = str(self.work_one.resolve())
        new_work = self.root / "trae-worker-02"
        rebound = coordctl.rebind_work(
            self.state, "main", "worker-01", str(new_work),
            "correct terminal ownership", True,
        )
        self.assertEqual(rebound["old_work"], old_work)
        self.assertEqual(rebound["new_work"], str(new_work.resolve()))

        state = json.loads(self.state.read_text(encoding="utf-8"))
        owner = state["threads"]["worker-01"]
        self.assertEqual(owner["role"], "collector")
        self.assertEqual(owner["work"], str(new_work.resolve()))
        self.assertEqual(len(owner["work_rebindings"]), 1)
        self.assertEqual(owner["work_rebindings"][0]["old_work"], old_work)
        self.assertEqual(owner["work_rebindings"][0]["reason"], "correct terminal ownership")
        old_batch = state["batches"][claim["batch"]]
        self.assertEqual(old_batch, batch_before_rebind)

    def test_rebind_work_rejects_active_owner_and_conflicts_but_allows_peer_actor(self):
        self.register_collectors()
        self.enqueue(observed_item(161))
        claim = coordctl.claim_batch(self.state, "worker-01", 1)
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "owner is idle"):
            coordctl.rebind_work(
                self.state, "main", "worker-01", str(self.root / "new-work"),
                "still active", True,
            )
        self.assertEqual(self.state.read_bytes(), state_before)

        coordctl.release_batch(
            self.state, "main", claim["batch"], "stopped before correction", True,
        )
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "conflicts with another"):
            coordctl.rebind_work(
                self.state, "main", "worker-01", str(self.work_two),
                "conflicting path", True,
            )
        self.assertEqual(self.state.read_bytes(), state_before)
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.rebind_work(
                self.state, "unknown-peer", "worker-01", str(self.root / "new-work"),
                "unregistered cannot rebind", True,
            )
        with self.assertRaisesRegex(coordctl.CoordinationError, "logs_stopped"):
            coordctl.rebind_work(
                self.state, "worker-02", "worker-01", str(self.root / "new-work"),
                "missing confirmation", False,
            )
        rebound = coordctl.rebind_work(
            self.state, "worker-02", "worker-01", str(self.root / "new-work"),
            "peer approved terminal correction", True,
        )
        self.assertEqual(rebound["owner"], "worker-01")

    def test_explicit_retry_preserves_batch_history_and_records_each_attempt(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.enqueue(observed_item(171))
        first_claim = coordctl.claim_batch(self.state, "worker-01", 1)
        coordctl.ready_batch(self.state, "worker-01", first_claim["batch"], [
            {"url": first_claim["items"][0]["url"], "outcome": "failed", "note": "fixable"},
        ], True)
        coordctl.acknowledge_batch(
            self.state, "main", first_claim["batch"], verification_for(first_claim, cover_count=0),
        )
        state_before_retry = json.loads(self.state.read_text(encoding="utf-8"))
        old_batch = state_before_retry["batches"][first_claim["batch"]]
        url = first_claim["items"][0]["url"]

        retried = coordctl.retry_items(
            self.state, "main", [{"url": url}], "collector fix approved",
        )
        self.assertEqual(retried["requeued"], 1)
        self.assertFalse(retried["automatic_requeue"])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(state["batches"][first_claim["batch"]], old_batch)
        record = state["items"][url]
        self.assertEqual(record["state"], "queued")
        self.assertIsNone(record["batch"])
        self.assertEqual(record["retry_count"], 1)
        self.assertEqual(record["retry_history"][0]["previous_batch"], first_claim["batch"])
        self.assertEqual(record["retry_history"][0]["previous_outcome"], "failed")
        self.assertEqual(record["retry_history"][0]["reason"], "collector fix approved")

        duplicate_retry_state = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "manual_review"):
            coordctl.retry_items(self.state, "main", [{"url": url}], "must not double queue")
        self.assertEqual(self.state.read_bytes(), duplicate_retry_state)

        second_claim = coordctl.claim_batch(self.state, "worker-01", 1)
        coordctl.release_batch(
            self.state, "main", second_claim["batch"], "second attempt stopped", True,
        )
        coordctl.retry_items(self.state, "main", [{"url": url}], "second repair approved")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        record = state["items"][url]
        self.assertEqual(record["retry_count"], 2)
        self.assertEqual([entry["retry_number"] for entry in record["retry_history"]], [1, 2])
        self.assertEqual(record["retry_history"][1]["previous_batch"], second_claim["batch"])
        self.assertEqual(state["batches"][first_claim["batch"]], old_batch)

    def title_correction_fixture(self):
        self.register_collectors()
        self.enqueue(observed_item(801, '动画更新至13集'), observed_item(802, '另片全2集'))
        claim = coordctl.claim_batch(self.state, 'worker-01', 2)
        coordctl.ready_batch(self.state, 'worker-01', claim['batch'], [
            {'url': i['url'], 'outcome': 'failed', 'note': 'listing title included status'}
            for i in claim['items']], True)
        coordctl.acknowledge_batch(self.state, 'main', claim['batch'],
                                  verification_for(claim, cover_count=0))
        self.work_one.mkdir(exist_ok=True)
        proof = self.work_one / 'title-dom.json'
        proof.write_text(json.dumps({'page_url': claim['items'][0]['url'], 'heading': '动画',
                                     'listDataWeb': 13, 'observed_at': '2026-09-30T11:08:44Z'}),
                         encoding='utf-8')
        return claim, proof

    def test_retry_corrects_only_observed_appended_status_and_preserves_history(self):
        claim, proof = self.title_correction_fixture()
        before = json.loads(self.state.read_text(encoding='utf-8'))
        url = claim['items'][0]['url']
        coordctl.retry_items(self.state, 'worker-01', [
            {'url': url, 'title_correction_evidence': str(proof)}], 'same-page DOM title correction')
        after = json.loads(self.state.read_text(encoding='utf-8'))
        record = after['items'][url]
        self.assertEqual(record['item']['title'], '动画')
        self.assertEqual(record['item']['status'], '更新至13集')
        self.assertEqual(record['item']['url'], url)
        self.assertEqual(after['batches'][claim['batch']], before['batches'][claim['batch']])
        correction = record['retry_history'][0]['title_correction']
        self.assertEqual(correction['previous_item'], before['items'][url]['item'])
        self.assertEqual(correction['evidence_sha256'], hashlib.sha256(proof.read_bytes()).hexdigest())
        self.assertEqual(coordctl.claim_batch(self.state, 'worker-01', 1)['items'][0]['title'], '动画')

    def test_mixed_title_correction_rejects_wrong_page_or_foreign_evidence_atomically(self):
        claim, proof = self.title_correction_fixture()
        before = self.state.read_bytes()
        for wrong_path in (proof, self.root / 'foreign-proof.json'):
            with self.subTest(path=wrong_path):
                with self.assertRaises(coordctl.CoordinationError):
                    coordctl.retry_items(self.state, 'worker-01', [
                        {'url': claim['items'][0]['url'], 'title_correction_evidence': str(proof)},
                        {'url': claim['items'][1]['url'], 'title_correction_evidence': str(wrong_path)}],
                        'reject wrong-page or foreign evidence')
                self.assertEqual(self.state.read_bytes(), before)
    def test_retry_is_registered_peer_only_and_mixed_requests_are_atomic(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.enqueue(observed_item(181), observed_item(182), observed_item(183))
        claim = coordctl.claim_batch(self.state, "worker-01", 2)
        coordctl.ready_batch(self.state, "worker-01", claim["batch"], [
            {"url": claim["items"][0]["url"], "outcome": "failed"},
            {"url": claim["items"][1]["url"], "outcome": "captured"},
        ], True)
        coordctl.acknowledge_batch(
            self.state, "main", claim["batch"], verification_for(
                claim, cover_count=1,
                captured_urls=[claim["items"][1]["url"]],
            ),
        )
        manual_url = claim["items"][0]["url"]
        acknowledged_url = claim["items"][1]["url"]
        queued_url = observed_item(183)["url"]

        for invalid in (
            [{"url": manual_url}, {"url": acknowledged_url}],
            [{"url": manual_url}, {"url": queued_url}],
            [{"url": manual_url}, {"url": manual_url + "#duplicate"}],
            [{"url": manual_url}, {"url": observed_item(999)["url"]}],
        ):
            state_before = self.state.read_bytes()
            with self.assertRaises(coordctl.CoordinationError):
                coordctl.retry_items(self.state, "main", invalid, "mixed request")
            self.assertEqual(self.state.read_bytes(), state_before)

        queued_claim = coordctl.claim_batch(self.state, "main", 1)
        claimed_url = queued_claim["items"][0]["url"]
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "manual_review"):
            coordctl.retry_items(
                self.state, "main", [{"url": manual_url}, {"url": claimed_url}],
                "claimed is ineligible",
            )
        self.assertEqual(self.state.read_bytes(), state_before)
        coordctl.ready_batch(self.state, "main", queued_claim["batch"], [
            {"url": claimed_url, "outcome": "captured"},
        ], True)
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "manual_review"):
            coordctl.retry_items(
                self.state, "main", [{"url": manual_url}, {"url": claimed_url}],
                "ready is ineligible",
            )
        self.assertEqual(self.state.read_bytes(), state_before)

        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.retry_items(
                self.state, "unknown-peer", [{"url": manual_url}], "unknown cannot approve",
            )
        self.assertEqual(self.state.read_bytes(), state_before)
        retried = coordctl.retry_items(
            self.state, "worker-01", [{"url": manual_url}], "peer approved repair",
        )
        self.assertEqual(retried["requeued"], 1)

    def test_registered_peer_can_atomically_assign_untracked_manual_work(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        same_title = "手工保留同名页"
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.assign_items(self.state, "unknown-peer", "worker-01", [
                observed_item(200, same_title),
            ])
        self.assertEqual(self.state.read_bytes(), state_before)
        assigned = coordctl.assign_items(self.state, "worker-01", "worker-01", [
            observed_item(201, same_title), observed_item(202, same_title),
        ])
        self.assertEqual(len(assigned["items"]), 2)
        status = coordctl.status_state(self.state)
        worker = next(row for row in status["threads"] if row["thread"] == "worker-01")
        self.assertEqual(worker["active_batch"], assigned["batch"])
        self.assertEqual(worker["active_items"], 2)
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "already has an active batch"):
            coordctl.assign_items(self.state, "main", "worker-01", [observed_item(203)])
        self.assertEqual(self.state.read_bytes(), state_before)

    def test_assign_rejects_any_url_already_known_to_state(self):
        coordctl.register_thread(self.state, "worker-01", "collector", str(self.work_one))
        self.enqueue(observed_item(301))
        state_before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "overlap=1"):
            coordctl.assign_items(self.state, "main", "worker-01", [
                {**observed_item(301), "url": observed_item(301)["url"] + "#same"},
                observed_item(302),
            ])
        self.assertEqual(self.state.read_bytes(), state_before)

    def test_existing_lock_is_not_removed_as_stale(self):
        lock_path = self.state.with_name(self.state.name + ".lock")
        lock_path.write_text('{"pid":999999,"token":"foreign"}', encoding="utf-8")
        with self.assertRaisesRegex(coordctl.CoordinationError, "was not removed"):
            coordctl.status_state(self.state, lock_timeout=0.03)
        self.assertTrue(lock_path.exists())
        lock_path.unlink()  # Explicit test-owner cleanup, never coordinator auto-recovery.

    def test_parallel_cli_claims_are_disjoint_and_state_remains_valid_json(self):
        self.register_collectors()
        self.enqueue(observed_item(1), observed_item(2))
        script = Path(coordctl.__file__)
        commands = [
            [sys.executable, str(script), "claim", "--state", str(self.state),
             "--thread", thread, "--limit", "1"]
            for thread in ("worker-01", "worker-02")
        ]
        processes = [subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                     for command in commands]
        outputs = []
        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            outputs.append(json.loads(stdout))
        claimed_urls = [output["items"][0]["url"] for output in outputs]
        self.assertEqual(len(set(claimed_urls)), 2)
        json.loads(self.state.read_text(encoding="utf-8"))

    def test_status_is_compact_and_does_not_emit_queued_urls(self):
        secret_url = observed_item(777)["url"]
        self.enqueue(observed_item(777))
        status = coordctl.status_state(self.state)
        self.assertEqual(status["coordination_mode"], "peer")
        self.assertEqual(status["reporting_mode"], "per_thread")
        self.assertNotIn("progress_reporter", status)
        self.assertNotIn("coordinator", status)
        self.assertEqual(status["items"], {"queued": 1})
        self.assertNotIn(secret_url, json.dumps(status, ensure_ascii=False))

    def test_repeated_ready_and_ack_are_rejected_without_state_damage(self):
        self.enqueue(observed_item(401))
        claim = coordctl.claim_batch(self.state, "main", 1)
        results = [{"url": claim["items"][0]["url"], "outcome": "captured"}]
        coordctl.ready_batch(self.state, "main", claim["batch"], results, True)
        ready_state = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "only a claimed batch"):
            coordctl.ready_batch(self.state, "main", claim["batch"], results, True)
        self.assertEqual(self.state.read_bytes(), ready_state)
        verification = verification_for(claim)
        coordctl.acknowledge_batch(self.state, "main", claim["batch"], verification)
        acked_state = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "only a ready"):
            coordctl.acknowledge_batch(self.state, "main", claim["batch"], verification)
        self.assertEqual(self.state.read_bytes(), acked_state)

    def test_cli_json_inputs_and_outputs(self):
        items_path = self.root / "items.json"
        items_path.write_text(
            json.dumps({"items": [observed_item(1)]}, ensure_ascii=False), encoding="utf-8-sig",
        )
        process = subprocess.run([
            sys.executable, str(Path(coordctl.__file__)), "enqueue", "--state", str(self.state),
            "--thread", "main", "--items", str(items_path),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 0, process.stderr)
        self.assertEqual(json.loads(process.stdout)["added"], 1)

    def test_cli_init_thread_and_legacy_coordinator_role_aliases(self):
        script = str(Path(coordctl.__file__))
        new_state = self.root / "new-style-state.json"
        new_work = self.root / "new-style-work"
        new_init = subprocess.run([
            sys.executable, script, "init", "--state", str(new_state),
            "--thread", "peer-a", "--work", str(new_work),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(new_init.returncode, 0, new_init.stderr)
        new_result = json.loads(new_init.stdout)
        self.assertEqual(new_result["thread"], "peer-a")
        self.assertEqual(new_result["reporting_mode"], "per_thread")
        self.assertNotIn("coordinator", new_result)

        default_register = subprocess.run([
            sys.executable, script, "register", "--state", str(new_state),
            "--thread", "peer-b", "--work", str(self.root / "new-style-work-b"),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(default_register.returncode, 0, default_register.stderr)
        self.assertEqual(json.loads(default_register.stdout)["role"], "peer")

        legacy_state = self.root / "legacy-command-state.json"
        legacy_init = subprocess.run([
            sys.executable, script, "init", "--state", str(legacy_state),
            "--coordinator", "legacy-main", "--work", str(self.root / "legacy-work"),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(legacy_init.returncode, 0, legacy_init.stderr)
        legacy_register = subprocess.run([
            sys.executable, script, "register", "--state", str(legacy_state),
            "--thread", "legacy-worker", "--role", "collector",
            "--work", str(self.root / "legacy-worker"),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(legacy_register.returncode, 0, legacy_register.stderr)
        self.assertEqual(json.loads(legacy_register.stdout)["role"], "peer")
        legacy_raw = json.loads(legacy_state.read_text(encoding="utf-8"))
        self.assertEqual(legacy_raw["threads"]["legacy-worker"]["role"], "collector")

    def test_invalid_bom_json_never_changes_state(self):
        invalid = self.root / "invalid.json"
        invalid.write_bytes(b"\xef\xbb\xbf{not-json")
        state_before = self.state.read_bytes()
        process = subprocess.run([
            sys.executable, str(Path(coordctl.__file__)), "enqueue", "--state", str(self.state),
            "--thread", "main", "--items", str(invalid),
        ], capture_output=True, text=True, timeout=10)
        self.assertEqual(process.returncode, 1)
        self.assertFalse(json.loads(process.stderr)["ok"])
        self.assertEqual(self.state.read_bytes(), state_before)

    def test_cli_chinese_roundtrip_is_ascii_safe_under_cp936(self):
        items_path = self.root / "中文-items.json"
        chinese_title = "凡人修仙传：中文标题核验"
        items_path.write_text(json.dumps({
            "items": [observed_item(901, chinese_title)],
        }, ensure_ascii=False), encoding="utf-8-sig")
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp936"
        script = str(Path(coordctl.__file__))

        enqueue = subprocess.run([
            sys.executable, script, "enqueue", "--state", str(self.state),
            "--thread", "main", "--items", str(items_path),
        ], capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(enqueue.returncode, 0, enqueue.stderr)
        self.assertTrue(enqueue.stdout.isascii())
        self.assertEqual(json.loads(enqueue.stdout)["added"], 1)
        self.assertIn(chinese_title, self.state.read_text(encoding="utf-8"))

        claim = subprocess.run([
            sys.executable, script, "claim", "--state", str(self.state),
            "--thread", "main", "--limit", "1",
        ], capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(claim.returncode, 0, claim.stderr)
        self.assertTrue(claim.stdout.isascii())
        claimed = json.loads(claim.stdout)
        self.assertEqual(claimed["items"][0]["title"], chinese_title)

        current = subprocess.run([
            sys.executable, script, "current", "--state", str(self.state),
            "--thread", "main",
        ], capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(current.returncode, 0, current.stderr)
        self.assertTrue(current.stdout.isascii())
        self.assertEqual(json.loads(current.stdout)["items"][0]["title"], chinese_title)

        error = subprocess.run([
            sys.executable, script, "current", "--state", str(self.state),
            "--thread", "不存在的线程",
        ], capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(error.returncode, 1)
        self.assertTrue(error.stderr.isascii())
        self.assertIn("不存在的线程", json.loads(error.stderr)["error"])


class TakeoverIdleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "coord-state.json"
        self.old_work = self.root / "worker-old"
        self.receiver_work = self.root / "worker-receiver"
        self.other_work = self.root / "worker-other"
        for directory in (self.old_work, self.receiver_work, self.other_work):
            directory.mkdir()
        for directory in (self.old_work, self.receiver_work):
            (directory / "details.ndjson").write_bytes(b'{"fixture":"complete"}\n')
            (directory / "raw-details.ndjson").write_bytes(b"")
            (directory / "navigation-issues.ndjson").write_bytes(b'{"fixture":"issue"}\n')
            (directory / "keep.txt").write_bytes(b"not a journal; never change me")
        coordctl.init_state(self.state, "receiver", str(self.receiver_work))
        coordctl.register_thread(self.state, "old-owner", work=str(self.old_work))
        coordctl.register_thread(self.state, "other", work=str(self.other_work))

    def tearDown(self):
        self.temporary.cleanup()

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def write_fixture_state(self, value):
        self.state.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")

    def journal_snapshot(self, directory):
        result = []
        for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
            if path.name.lower().endswith(".ndjson"):
                data = path.read_bytes()
                result.append({"name": path.name, "size": len(data),
                               "sha256": hashlib.sha256(data).hexdigest()})
        return result

    def evidence(self):
        return {
            "previous_thread": "old-owner", "receiver_thread": "receiver",
            "previous_work": str(self.old_work), "receiver_work": str(self.receiver_work),
            "user_authorized": True, "owner_stopped": True, "receiver_stopped": True,
            "method": "explicit handoff and stopped journal byte snapshots",
            "summary": "用户授权；双方已明确停写，仅接管目录归属。",
            "journals": self.journal_snapshot(self.old_work),
            "receiver_journals": self.journal_snapshot(self.receiver_work),
        }

    def takeover(self, **overrides):
        arguments = {
            "state_path": self.state, "thread": "receiver", "owner": "old-owner",
            "work": str(self.old_work), "receiver_work": str(self.receiver_work),
            "reason": "explicit user-authorized idle handoff", "evidence": self.evidence(),
            "logs_stopped": True,
        }
        arguments.update(overrides)
        return coordctl.takeover_idle_thread(**arguments)

    def assert_rejected(self, message=None, **overrides):
        before = self.state.read_bytes()
        with self.assertRaises(coordctl.CoordinationError) as caught:
            self.takeover(**overrides)
        if message:
            self.assertIn(message, str(caught.exception))
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse(self.state.with_suffix(".json.lock").exists())

    def test_success_preserves_both_histories_and_all_files(self):
        coordctl.enqueue_items(self.state, "receiver", [observed_item(1), observed_item(2)])
        for thread in ("old-owner", "receiver"):
            claim = coordctl.claim_batch(self.state, thread, 1)
            coordctl.ready_batch(self.state, thread, claim["batch"], [{
                "url": claim["items"][0]["url"], "outcome": "captured",
            }], True)
            coordctl.acknowledge_batch(self.state, thread, claim["batch"], verification_for(claim))
        fixture = self.read_state()
        fixture["threads"]["receiver"]["takeovers"] = [{"prior_handoff": "preserved"}]
        fixture["threads"]["receiver"]["work_rebindings"] = [{"old_record": "preserved"}]
        self.write_fixture_state(fixture)
        before = self.read_state()
        files = {path: path.read_bytes() for directory in (
            self.old_work, self.receiver_work, self.other_work,
        ) for path in directory.iterdir()}
        result = self.takeover()
        after = self.read_state()
        self.assertEqual(result["command"], "takeover-idle")
        self.assertTrue(result["history_preserved"])
        self.assertEqual(result["registered_workers"], 2)
        self.assertEqual(result["owner_journals"], 3)
        self.assertEqual(result["receiver_journals"], 3)
        for key in before:
            if key not in {"threads", "updated_at"}:
                self.assertEqual(after[key], before[key])
        self.assertNotIn("old-owner", after["threads"])
        retired = after["retired_threads"]["old-owner"]
        for key, value in before["threads"]["old-owner"].items():
            self.assertEqual(retired[key], value)
        self.assertEqual(retired["replaced_by"], "receiver")
        self.assertEqual(retired["evidence"], self.evidence())
        receiver = after["threads"]["receiver"]
        for key, value in before["threads"]["receiver"].items():
            if key not in {"work", "work_key", "takeovers"}:
                self.assertEqual(receiver[key], value)
        self.assertEqual(receiver["work"], before["threads"]["old-owner"]["work"])
        self.assertEqual(receiver["takeovers"][:-1], [{"prior_handoff": "preserved"}])
        handoff = receiver["takeovers"][-1]
        self.assertEqual(handoff["previous_work"], before["threads"]["receiver"]["work"])
        self.assertEqual(handoff["evidence"], self.evidence())
        self.assertEqual(after["threads"]["other"], before["threads"]["other"])
        self.assertEqual({path: path.read_bytes() for path in files}, files)

    def restore_receipt(self, batch):
        return {
            "handoff_id": "return-to-retired-owner",
            "from_thread": "receiver", "intended_receiver_thread": "old-owner",
            "work": str(self.old_work), "user_authorized": True,
            "old_owner_confirmed": True, "owner_stopped": True,
            "journals_stopped": True, "database_writes_stopped": True,
            "browser_actions_stopped": True, "new_claims_stopped": True,
            "current_status": "idle", "active_batch": None,
            "last_batch": batch, "last_batch_status": "acknowledged",
            "last_batch_acknowledged_at": self.read_state()["batches"][batch]["acknowledged_at"],
            "method": "owner stop receipt and two locked journal passes",
            "summary": "Return the actual retired identity to the stopped worker directory.",
            "journals": self.journal_snapshot(self.old_work),
        }

    def test_restore_retired_identity_after_explicit_stop_preserves_history(self):
        self.takeover()
        coordctl.enqueue_items(self.state, "receiver", [observed_item(13)])
        batch = coordctl.claim_batch(self.state, "receiver", 1)
        coordctl.ready_batch(self.state, "receiver", batch["batch"], [{
            "url": batch["items"][0]["url"], "outcome": "captured",
        }], True)
        coordctl.acknowledge_batch(self.state, "receiver", batch["batch"], verification_for(batch))
        before = self.read_state()
        result = coordctl.restore_retired_idle_thread(
            self.state, "old-owner", "receiver", str(self.old_work),
            "user-authorized return", self.restore_receipt(batch["batch"]),
            True, True,
        )
        after = self.read_state()
        self.assertEqual(result["registered_workers"], 2)
        self.assertEqual(after["threads"]["old-owner"]["prior_retirement"],
                         before["retired_threads"]["old-owner"])
        self.assertEqual(after["retired_threads"]["receiver"]["replaced_by"], "old-owner")
        self.assertEqual(after["batches"], before["batches"])
        self.assertEqual(after["items"], before["items"])
        self.assertEqual(after["threads"]["other"], before["threads"]["other"])
        self.assertEqual(after["threads"]["old-owner"]["work"], str(self.old_work))

    def test_restore_retired_identity_after_user_forced_archived_stop(self):
        self.takeover()
        coordctl.enqueue_items(self.state, "receiver", [observed_item(131)])
        batch = coordctl.claim_batch(self.state, "receiver", 1)
        coordctl.ready_batch(self.state, "receiver", batch["batch"], [{
            "url": batch["items"][0]["url"], "outcome": "captured",
        }], True)
        coordctl.acknowledge_batch(self.state, "receiver", batch["batch"], verification_for(batch))
        evidence = self.restore_receipt(batch["batch"])
        evidence.update({
            "old_owner_confirmed": False,
            "user_forced_stop": True,
            "session_archived": True,
            "archived_session_id": "receiver",
            "archive_method": "supported session archive command",
        })
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "explicitly authorized"):
            coordctl.restore_retired_idle_thread(
                self.state, "old-owner", "receiver", str(self.old_work),
                "forced return", evidence, True, True,
            )
        self.assertEqual(self.state.read_bytes(), before)
        result = coordctl.restore_retired_idle_thread(
            self.state, "old-owner", "receiver", str(self.old_work),
            "forced return", evidence, True, True, True,
        )
        self.assertTrue(result["ok"])
        after = self.read_state()
        self.assertEqual(after["threads"]["old-owner"]["work"], str(self.old_work))
        self.assertTrue(after["retired_threads"]["receiver"]["evidence"]["user_forced_stop"])

    def test_restore_rejects_stale_journal_and_unacknowledged_batch_atomically(self):
        self.takeover()
        coordctl.enqueue_items(self.state, "receiver", [observed_item(14)])
        batch = coordctl.claim_batch(self.state, "receiver", 1)
        coordctl.ready_batch(self.state, "receiver", batch["batch"], [{
            "url": batch["items"][0]["url"], "outcome": "captured",
        }], True)
        coordctl.acknowledge_batch(self.state, "receiver", batch["batch"], verification_for(batch))
        receipt = self.restore_receipt(batch["batch"])
        before = self.state.read_bytes()
        with self.assertRaises(coordctl.CoordinationError):
            coordctl.restore_retired_idle_thread(
                self.state, "old-owner", "receiver", str(self.old_work),
                "return", receipt, True, False,
            )
        self.assertEqual(self.state.read_bytes(), before)
        (self.old_work / "details.ndjson").write_bytes(b'{"changed":true}\n')
        with self.assertRaisesRegex(coordctl.CoordinationError, "snapshot changed"):
            coordctl.restore_retired_idle_thread(
                self.state, "old-owner", "receiver", str(self.old_work),
                "return", receipt, True, True,
            )
        self.assertEqual(self.state.read_bytes(), before)

    def test_retired_identity_cannot_register_claim_or_assign(self):
        self.takeover()
        before = self.state.read_bytes()
        attempts = [
            lambda: coordctl.register_thread(self.state, "old-owner", work=str(self.old_work)),
            lambda: coordctl.register_thread(self.state, "old-owner", work=str(self.other_work)),
            lambda: coordctl.claim_batch(self.state, "old-owner", 1),
            lambda: coordctl.assign_items(self.state, "receiver", "old-owner", [observed_item(77)]),
            lambda: coordctl.assign_items(self.state, "old-owner", "receiver", [observed_item(77)]),
        ]
        for attempt in attempts:
            with self.subTest(attempt=attempt), self.assertRaises(coordctl.CoordinationError):
                attempt()
            self.assertEqual(self.state.read_bytes(), before)
        claim = coordctl.assign_items(self.state, "receiver", "receiver", [observed_item(77)])
        self.assertEqual(claim["work"], coordctl._work_path(str(self.old_work))[0])

    def test_authorization_stop_identity_and_metadata_are_required(self):
        for key, invalid in (
            ("user_authorized", False), ("user_authorized", 1),
            ("owner_stopped", False), ("receiver_stopped", False),
            ("previous_thread", "other"), ("receiver_thread", "other"),
            ("method", " "), ("summary", ""),
            ("previous_work", str(self.other_work)),
            ("receiver_work", str(self.other_work)),
        ):
            with self.subTest(key=key, invalid=invalid):
                evidence = self.evidence()
                evidence[key] = invalid
                self.assert_rejected(evidence=evidence)
        for overrides in (
            {"logs_stopped": False}, {"reason": " "}, {"thread": "old-owner"},
            {"thread": " unknown "}, {"work": str(self.other_work)},
            {"receiver_work": str(self.other_work)}, {"evidence": None},
        ):
            with self.subTest(overrides=overrides):
                self.assert_rejected(**overrides)

    def test_both_participants_must_be_registered_and_not_retired(self):
        original = self.read_state()
        for identity in ("receiver", "old-owner"):
            for mutation in ("unregistered", "retired"):
                with self.subTest(identity=identity, mutation=mutation):
                    state = copy.deepcopy(original)
                    if mutation == "unregistered":
                        del state["threads"][identity]
                    else:
                        state["retired_threads"] = {identity: {"prior_record": True}}
                    self.write_fixture_state(state)
                    self.assert_rejected()
        self.write_fixture_state(original)

    def test_busy_modes_active_pointers_and_orphan_claimed_ready_batches_block(self):
        original = self.read_state()
        for identity in ("receiver", "old-owner"):
            for condition in ("mode", "active_batch", "claimed", "ready"):
                with self.subTest(identity=identity, condition=condition):
                    state = copy.deepcopy(original)
                    if condition == "mode":
                        state["threads"][identity]["mode"] = "awaiting_ack"
                    elif condition == "active_batch":
                        state["threads"][identity]["active_batch"] = "batch-elsewhere"
                    else:
                        state["batches"]["orphan"] = {"thread": identity, "state": condition}
                    self.write_fixture_state(state)
                    self.assert_rejected("both peers idle")
        self.write_fixture_state(original)

    def test_unrelated_active_peer_is_preserved_and_does_not_block(self):
        coordctl.enqueue_items(self.state, "other", [observed_item(99)])
        claim = coordctl.claim_batch(self.state, "other", 1)
        before = self.read_state()
        self.takeover()
        after = self.read_state()
        self.assertEqual(after["threads"]["other"], before["threads"]["other"])
        self.assertEqual(after["batches"][claim["batch"]], before["batches"][claim["batch"]])

    def test_snapshot_contract_requires_sorted_unique_complete_entries(self):
        invalid_snapshots = [
            None, {}, [{"name": "details.ndjson"}],
            [{"name": "../details.ndjson", "size": 1, "sha256": "a" * 64}],
            [{"name": "details.ndjson", "size": True, "sha256": "a" * 64}],
            [{"name": "details.ndjson", "size": -1, "sha256": "a" * 64}],
            [{"name": "details.ndjson", "size": 1, "sha256": "A" * 64}],
            list(reversed(self.evidence()["journals"])),
            [self.evidence()["journals"][0]] * 2,
            self.evidence()["journals"][:-1],
        ]
        for key in ("journals", "receiver_journals"):
            for snapshot in invalid_snapshots:
                with self.subTest(key=key, snapshot=snapshot):
                    evidence = self.evidence()
                    evidence[key] = snapshot
                    self.assert_rejected(evidence=evidence)

    def test_changed_bytes_hash_size_and_added_journal_are_rejected(self):
        for directory in (self.old_work, self.receiver_work):
            path = directory / "details.ndjson"
            original = path.read_bytes()
            for changed in (original + b'{}\n', original.replace(b"complete", b"modified")):
                with self.subTest(directory=directory, changed=changed):
                    evidence = self.evidence()
                    path.write_bytes(changed)
                    self.assert_rejected("snapshot changed", evidence=evidence)
                    path.write_bytes(original)
            evidence = self.evidence()
            added = directory / "new-observations.ndjson"
            added.write_bytes(b'{}\n')
            self.assert_rejected("snapshot changed", evidence=evidence)
            added.unlink()

    def test_half_line_is_rejected_even_with_matching_hash_in_evidence(self):
        for directory in (self.old_work, self.receiver_work):
            with self.subTest(directory=directory):
                path = directory / "details.ndjson"
                original = path.read_bytes()
                path.write_bytes(b'{"incomplete":')
                self.assert_rejected("incomplete final line")
                path.write_bytes(original)

    def test_change_between_snapshot_passes_is_rejected_atomically(self):
        original_reader = coordctl._snapshot_handoff_journals
        calls = []

        def change_after_first_pair(work):
            result = original_reader(work)
            calls.append(work)
            if len(calls) == 2:
                (self.old_work / "appeared.ndjson").write_bytes(b'{}\n')
            return result

        with mock.patch.object(coordctl, "_snapshot_handoff_journals", side_effect=change_after_first_pair):
            self.assert_rejected("snapshot changed")
        self.assertEqual(len(calls), 3)

    def test_missing_directory_and_journal_links_are_rejected(self):
        expected = self.evidence()
        original_is_dir = Path.is_dir
        with mock.patch.object(Path, "is_dir", autospec=True, side_effect=lambda path:
                               False if path == self.old_work else original_is_dir(path)):
            self.assert_rejected("directory is missing", evidence=expected)
        with mock.patch.object(Path, "is_symlink", return_value=True):
            self.assert_rejected("journal links", evidence=expected)

    def test_original_work_mismatch_alias_and_third_party_conflict_are_rejected(self):
        original = self.read_state()
        for conflict in ("stored_key", "same_directory", "nested_directory", "third_party"):
            with self.subTest(conflict=conflict):
                state = copy.deepcopy(original)
                arguments = {}
                if conflict == "stored_key":
                    state["threads"]["receiver"]["work_key"] = "stale-key"
                elif conflict in {"same_directory", "nested_directory"}:
                    directory = self.old_work if conflict == "same_directory" else self.old_work / "child"
                    directory.mkdir(exist_ok=True)
                    physical, key = coordctl._work_path(str(directory))
                    state["threads"]["receiver"].update(work=physical, work_key=key)
                    evidence = self.evidence()
                    evidence["receiver_work"] = physical
                    evidence["receiver_journals"] = self.journal_snapshot(directory)
                    arguments = {"receiver_work": physical, "evidence": evidence}
                else:
                    state["threads"]["other"].update(
                        work=state["threads"]["old-owner"]["work"],
                        work_key=state["threads"]["old-owner"]["work_key"],
                    )
                self.write_fixture_state(state)
                self.assert_rejected(**arguments)
        self.write_fixture_state(original)

    def test_existing_state_lock_is_respected_and_not_deleted(self):
        before = self.state.read_bytes()
        with coordctl.StateLock(self.state):
            with self.assertRaisesRegex(coordctl.CoordinationError, "lock is busy"):
                self.takeover(lock_timeout=0)
            self.assertTrue(self.state.with_suffix(".json.lock").exists())
            self.assertEqual(self.state.read_bytes(), before)

    def test_replace_idle_still_rejects_registered_receiver_and_accepts_new_identity(self):
        before = self.state.read_bytes()
        evidence = self.evidence()
        evidence["replacement_thread"] = "receiver"
        with self.assertRaisesRegex(coordctl.CoordinationError, "already registered"):
            coordctl.replace_idle_thread(
                self.state, "receiver", "old-owner", str(self.old_work), "compatibility", evidence, True,
            )
        self.assertEqual(self.state.read_bytes(), before)
        evidence["replacement_thread"] = "new-peer"
        result = coordctl.replace_idle_thread(
            self.state, "new-peer", "old-owner", str(self.old_work), "compatibility", evidence, True,
        )
        self.assertEqual(result["command"], "replace-idle")
        self.assertEqual(self.read_state()["threads"]["receiver"]["work"],
                         coordctl._work_path(str(self.receiver_work))[0])

    def test_real_cli_bom_evidence_ascii_output_and_duplicate_takeover_rejection(self):
        evidence_path = self.root / "handoff.json"
        evidence_path.write_text(json.dumps(self.evidence(), ensure_ascii=False), encoding="utf-8-sig")
        command = [
            sys.executable, str(Path(coordctl.__file__)), "takeover-idle",
            "--state", str(self.state), "--thread", "receiver", "--owner", "old-owner",
            "--work", str(self.old_work), "--receiver-work", str(self.receiver_work),
            "--reason", "用户授权接管", "--evidence", str(evidence_path), "--logs-stopped",
        ]
        environment = os.environ.copy()
        environment["PYTHONIOENCODING"] = "cp936"
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.isascii())
        self.assertEqual(json.loads(result.stdout)["command"], "takeover-idle")
        before = self.state.read_bytes()
        duplicate = subprocess.run(command, capture_output=True, text=True, timeout=10, env=environment)
        self.assertEqual(duplicate.returncode, 1, duplicate.stdout)
        self.assertEqual(self.state.read_bytes(), before)
        self.assertEqual(self.read_state()["retired_threads"]["old-owner"]["reason"], "用户授权接管")


class RetireIdleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state.json"
        self.work = self.root / "worker"
        self.work.mkdir()
        (self.work / "details.ndjson").write_bytes(b'{"captured":true}\n')
        coordctl.init_state(self.state, "owner", str(self.work))

    def tearDown(self):
        self.temporary.cleanup()

    def evidence(self):
        return {"previous_thread": "owner", "previous_work": str(self.work),
                "user_authorized": True, "owner_stopped": True,
                "method": "owner confirmed stop and journal byte snapshot",
                "summary": "Retire an idle owner while preserving all completed batches and files.",
                "journals": coordctl._snapshot_handoff_journals(str(self.work))}

    def retire(self, evidence=None):
        return coordctl.retire_idle_thread(
            self.state, "owner", str(self.work), "user removed spare collector",
            evidence or self.evidence(), True,
        )

    def test_retire_idle_preserves_history_and_prevents_reuse(self):
        coordctl.enqueue_items(self.state, "owner", [observed_item(88)])
        before = json.loads(self.state.read_text(encoding="utf-8"))
        result = self.retire()
        after = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(result["registered_workers"], 0)
        self.assertEqual(after["retired_threads"]["owner"]["work"], before["threads"]["owner"]["work"])
        self.assertEqual(after["items"], before["items"])
        self.assertEqual((self.work / "details.ndjson").read_bytes(), b'{"captured":true}\n')
        with self.assertRaises(coordctl.CoordinationError):
            coordctl.register_thread(self.state, "owner", work=str(self.work))

    def test_retire_rejects_active_batch_and_changed_journal_without_mutation(self):
        old_evidence = self.evidence()
        (self.work / "details.ndjson").write_bytes(b'{"captured":true}\n{"new":true}\n')
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "snapshot changed"):
            self.retire(old_evidence)
        self.assertEqual(self.state.read_bytes(), before)
        coordctl.enqueue_items(self.state, "owner", [observed_item(89)])
        coordctl.claim_batch(self.state, "owner", 1)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "in-flight"):
            self.retire()
        self.assertEqual(self.state.read_bytes(), before)


class ReactivateRetiredIdleTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.state = self.root / "state.json"
        self.work = self.root / "worker"
        self.work.mkdir()
        (self.work / "details.ndjson").write_bytes(b'{"captured":true}\n')
        coordctl.init_state(self.state, "owner", str(self.work))
        coordctl.register_thread(self.state, "peer", work=str(self.root / "peer"))
        coordctl.enqueue_items(self.state, "peer", [observed_item(901), observed_item(902)])
        coordctl.claim_batch(self.state, "peer", 1)
        coordctl.retire_idle_thread(self.state, "owner", str(self.work), "user stop", {
            "previous_thread": "owner", "previous_work": str(self.work),
            "user_authorized": True, "owner_stopped": True,
            "method": "explicit stop receipt", "summary": "Owner stopped at idle.",
            "journals": coordctl._snapshot_handoff_journals(str(self.work)),
        }, True)

    def tearDown(self):
        self.temporary.cleanup()

    def read_state(self):
        return json.loads(self.state.read_text(encoding="utf-8"))

    def evidence(self):
        return {
            "thread": "owner", "work": str(self.work), "user_authorized": True,
            "same_identity_resumed": True, "owner_stopped": True, "search_claims_checked": True,
            "method": "original host session resumed and identity confirmed",
            "summary": "User explicitly reversed this direct retirement; no pending search claims.",
            "retired_record_sha256": coordctl.retired_record_digest(self.read_state()["retired_threads"]["owner"]),
            "journals": coordctl._snapshot_handoff_journals(str(self.work)),
        }

    def reactivate(self, evidence=None, **kwargs):
        return coordctl.reactivate_retired_idle_thread(
            self.state, "owner", str(self.work), "用户恢复原身份",
            self.evidence() if evidence is None else evidence, True, **kwargs,
        )

    def assert_rejected(self, pattern, evidence=None):
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, pattern):
            self.reactivate(evidence)
        self.assertEqual(self.state.read_bytes(), before)

    def test_restore_preserves_retirement_peers_batches_queue_journals_and_can_claim(self):
        before = self.read_state()
        result = self.reactivate()
        after = self.read_state()
        self.assertEqual(result["command"], "reactivate-retired-idle")
        self.assertEqual(result["registered_workers"], 2)
        self.assertNotIn("owner", after["retired_threads"])
        self.assertEqual(after["reactivation_history"][0]["retirement"], before["retired_threads"]["owner"])
        self.assertEqual(after["threads"]["peer"], before["threads"]["peer"])
        for key in ("items", "queue", "batches", "next_batch"):
            self.assertEqual(after[key], before[key])
        self.assertEqual((self.work / "details.ndjson").read_bytes(), b'{"captured":true}\n')
        self.assertEqual(coordctl.current_batch(self.state, "owner")["status"], "idle")
        self.assertEqual(coordctl.claim_batch(self.state, "owner", 1)["items"], [observed_item(902)])

    def test_requires_authorization_original_identity_and_external_stop_checks(self):
        for key in ("user_authorized", "same_identity_resumed", "owner_stopped", "search_claims_checked"):
            evidence = self.evidence()
            evidence[key] = False
            self.assert_rejected("matching resumed-identity evidence", evidence)
        evidence = self.evidence()
        evidence["thread"] = "new-identity"
        self.assert_rejected("matching resumed-identity evidence", evidence)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "stopped logs"):
            coordctl.reactivate_retired_idle_thread(self.state, "owner", str(self.work), "reason", self.evidence(), False)
        self.assertEqual(self.state.read_bytes(), before)

    def test_rejects_stale_retirement_and_replacement_lineage(self):
        evidence = self.evidence()
        state = self.read_state()
        state["retired_threads"]["owner"]["reason"] = "changed decision"
        coordctl._atomic_write(self.state, state)
        self.assert_rejected("retirement changed", evidence)
        state["retired_threads"]["owner"]["replaced_by"] = "peer"
        coordctl._atomic_write(self.state, state)
        self.assert_rejected("replacement lineage")

    def test_rejects_any_inflight_batch_even_if_retired_record_says_idle(self):
        state = self.read_state()
        next(iter(state["batches"].values()))["thread"] = "owner"
        coordctl._atomic_write(self.state, state)
        self.assert_rejected("in-flight")

    def test_rejects_conflicting_same_or_nested_work_directory(self):
        for conflicting in (self.work, self.work / "child", self.root):
            state = self.read_state()
            path, key = coordctl._work_path(str(conflicting))
            state["threads"]["peer"].update(work=path, work_key=key)
            coordctl._atomic_write(self.state, state)
            self.assert_rejected("conflicts")

    def test_rejects_work_mismatch_and_changed_journals(self):
        evidence = self.evidence()
        evidence["work"] = str(self.root / "other")
        self.assert_rejected("work evidence", evidence)
        evidence = self.evidence()
        (self.work / "details.ndjson").write_bytes(b'{"new":true}\n')
        self.assert_rejected("snapshot changed", evidence)
        evidence = self.evidence()
        with mock.patch.object(coordctl, "_snapshot_handoff_journals", side_effect=[evidence["journals"], []]):
            self.assert_rejected("snapshot changed", evidence)

    def test_does_not_break_foreign_lock(self):
        before = self.state.read_bytes()
        with coordctl.StateLock(self.state, timeout=1):
            with self.assertRaisesRegex(coordctl.CoordinationError, "lock is busy"):
                self.reactivate(lock_timeout=0)
            self.assertEqual(self.state.read_bytes(), before)

    def test_real_cli_and_repeated_recovery_cannot_mutate_again(self):
        receipt = self.root / "resume.json"
        receipt.write_text(json.dumps(self.evidence()), encoding="utf-8-sig")
        command = [sys.executable, str(Path(coordctl.__file__)), "reactivate-retired-idle",
                   "--state", str(self.state), "--thread", "owner", "--work", str(self.work),
                   "--reason", "user restored original", "--evidence", str(receipt), "--logs-stopped"]
        options = {"capture_output": True, "text": True, "timeout": 10,
                   "creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)}
        result = subprocess.run(command, **options)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)["history_preserved"])
        before = self.state.read_bytes()
        repeat = subprocess.run(command, **options)
        self.assertEqual(repeat.returncode, 1)
        self.assertIn("already registered", repeat.stderr)
        self.assertEqual(self.state.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
