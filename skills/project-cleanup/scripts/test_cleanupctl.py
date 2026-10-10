import copy
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import cleanupctl as cleanup
from cleanupctl import coordctl, shared_discovery


class CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.project = Path(self.temp.name) / "project"
        self.runtime, self.state, self.output = cleanup.paths(self.project)
        self.owner = "00000000-0000-4000-8000-000000000001"
        self.work = self.runtime / "workers" / self.owner
        self.work.mkdir(parents=True)
        (self.work / "details.ndjson").write_bytes(b'{"existing":true}\n')
        self.sessions = Path(self.temp.name) / "codex"
        coordctl.init_state(self.state, self.owner, str(self.work))
        coordctl.register_thread(self.state, "peer", work=str(self.runtime / "workers/peer"))
        self.evidence = {"thread": self.owner, "user_authorized": True, "owner_stopped": True,
                         "method": "explicit owner stop", "summary": "User stopped this exact collector.",
                         "journals": coordctl._snapshot_handoff_journals(str(self.work))}

    def tearDown(self):
        self.temp.cleanup()

    def retire(self, **kwargs):
        return cleanup.retire_owner(self.project, self.owner, self.evidence, **kwargs)

    def make_session(self, child=True):
        path = self.sessions / "sessions" / f"rollout-{self.owner}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"type": "session_meta", "payload": {
            "id": self.owner, "source": {"subagent": {}} if child else "cli",
        }}) + "\n", encoding="utf-8")
        return path

    def test_dry_run_preserves_authority_and_journals(self):
        before = self.state.read_bytes()
        self.assertTrue(self.retire()["dry_run"])
        self.assertEqual(self.state.read_bytes(), before)
        self.assertFalse((self.runtime / "title-discovery").exists())

    def test_missing_stop_or_changed_journal_rejects_without_mutation(self):
        before = self.state.read_bytes()
        self.evidence["owner_stopped"] = False
        with self.assertRaises(ValueError):
            self.retire(apply=True)
        self.evidence["owner_stopped"] = True
        (self.work / "details.ndjson").write_bytes(b'{"changed":true}\n')
        with self.assertRaisesRegex(ValueError, "snapshot changed"):
            self.retire(apply=True)
        self.assertEqual(self.state.read_bytes(), before)

    def test_cancel_requires_explicit_flag_and_retains_batch_and_data(self):
        item = {"url": "https://example.invalid/video/1", "title": "real", "source": "channel"}
        coordctl.enqueue_items(self.state, self.owner, [item])
        claim = coordctl.claim_batch(self.state, self.owner, 1)
        before = self.state.read_bytes()
        with self.assertRaisesRegex(ValueError, "in-flight"):
            self.retire(apply=True)
        self.assertEqual(self.state.read_bytes(), before)
        result = self.retire(apply=True, cancel_inflight=True)
        self.assertEqual(result["cancelled_batches"], 1)
        state = cleanup.read_json(self.state)
        self.assertNotIn(self.owner, state["threads"])
        self.assertIn("peer", state["threads"])
        self.assertEqual(state["batches"][claim["batch"]]["state"], "released")
        self.assertEqual(state["items"][item["url"]]["state"], "manual_review")
        self.assertEqual((self.work / "details.ndjson").read_bytes(), b'{"existing":true}\n')
        original_receipt = Path(result["receipt"]).read_bytes()
        self.retire(apply=True, cancel_inflight=True)
        self.assertEqual(Path(result["receipt"]).read_bytes(), original_receipt)

    def test_search_quarantine_preserves_original_rejects_replay_and_is_idempotent(self):
        root = self.runtime / "title-discovery"
        claimed = shared_discovery.claim_search(root, "title", "actual channel", self.owner, "task")
        original = Path(claimed["path"]).read_bytes()
        self.assertEqual(self.retire(apply=True)["cancelled_searches"], 1)
        self.assertEqual(self.retire(apply=True)["cancelled_searches"], 0)
        inspected = shared_discovery.inspect_claim(root, "title", "actual channel")
        self.assertEqual(inspected["reason"], "cancelled_owner_cleanup")
        replay = shared_discovery.claim_search(root, "title", "actual channel", "peer", "new-task")
        self.assertFalse(replay["ok"])
        self.assertEqual(replay["reason"], "cancelled_owner_cleanup")
        self.assertEqual(Path(claimed["path"]).read_bytes(), original)

    def test_session_delete_protects_main_and_nonchild(self):
        self.make_session(child=False)
        with self.assertRaisesRegex(ValueError, "protected"):
            cleanup.delete_session(self.project, self.sessions, self.owner, {self.owner}, True)
        with self.assertRaisesRegex(ValueError, "non-child"):
            cleanup.delete_session(self.project, self.sessions, self.owner, {"main"}, True)

    def test_cli_exit_zero_error_is_not_success(self):
        self.make_session()
        failure = subprocess.CompletedProcess([], 0, "Error: failed to archive session", "")
        with mock.patch.object(cleanup.subprocess, "run", return_value=failure):
            result = cleanup.delete_session(self.project, self.sessions, self.owner, {"main"}, True, "codex")
        self.assertFalse(result["ok"])
        self.assertTrue(cleanup.session_files(self.sessions, self.owner))

    def test_authenticated_existing_host_keeps_token_out_of_args_and_receipt(self):
        path = self.make_session()
        secret = "test-secret-only-for-child-environment"
        def run(command, **options):
            self.assertNotIn(secret, " ".join(command))
            self.assertEqual(options["env"]["CODEX_FEISHU_REMOTE_TOKEN"], secret)
            if command[1] == "delete":
                path.unlink()
            return subprocess.CompletedProcess(command, 0,
                ("Archived session " if command[1] == "archive" else "Deleted session ") + self.owner, "")
        with mock.patch.object(cleanup, "feishu_connection", return_value=("ws://127.0.0.1:1", {"CODEX_FEISHU_REMOTE_TOKEN": secret})), mock.patch.object(cleanup.subprocess, "run", side_effect=run):
            result = cleanup.delete_session(self.project, self.sessions, self.owner, {"main"},
                                             True, "codex", feishu_host=True)
        self.assertTrue(result["ok"])
        self.assertNotIn(secret, Path(result["receipt"]).read_text())

    def test_archive_requires_deleted_session_and_preserves_source_until_verified(self):
        self.retire(apply=True)
        with self.assertRaisesRegex(ValueError, "deletion is not verified"):
            cleanup.archive_work(self.project, self.sessions, self.owner, True, True)
        coordctl._atomic_write(self.output / f"session-{self.owner}.json", {"deleted": True})
        result = cleanup.archive_work(self.project, self.sessions, self.owner, True, True)
        self.assertTrue(result["removed"])
        self.assertFalse(self.work.exists())
        receipt = cleanup.read_json(result["receipt"])
        self.assertTrue(Path(receipt["archive"]).is_file())
        self.assertEqual(receipt["original_work"], str(self.work))

    def test_archive_does_not_delete_transferred_work(self):
        self.retire(apply=True)
        coordctl._atomic_write(self.output / f"session-{self.owner}.json", {"deleted": True})
        state = cleanup.read_json(self.state)
        state["threads"]["peer"]["work"] = str(self.work)
        coordctl._atomic_write(self.state, state)
        with self.assertRaisesRegex(ValueError, "overlaps"):
            cleanup.archive_work(self.project, self.sessions, self.owner, True, True)
        self.assertTrue(self.work.exists())

    def test_root_and_path_traversal_are_rejected(self):
        with self.assertRaises(ValueError):
            cleanup.require_child(self.runtime, self.runtime)
        with self.assertRaises(ValueError):
            cleanup.require_child(self.project, self.runtime)
        with self.assertRaises(ValueError):
            cleanup.retire_owner(self.project, "../bad", self.evidence, True)

    def test_pause_blocks_collection_but_allows_current_and_cleanup(self):
        state = cleanup.read_json(self.state)
        state["collection_paused"] = True
        coordctl._atomic_write(self.state, state)
        with self.assertRaisesRegex(coordctl.CoordinationError, "paused"):
            coordctl.claim_batch(self.state, self.owner, 1)
        with self.assertRaisesRegex(coordctl.CoordinationError, "paused"):
            coordctl.register_thread(self.state, "new", work=str(self.runtime / "workers/new"))
        with self.assertRaisesRegex(ValueError, "paused"):
            shared_discovery.claim_search(self.runtime / "title-discovery", "a", "b", self.owner, "task")
        self.assertEqual(coordctl.current_batch(self.state, self.owner)["status"], "idle")
        self.assertTrue(self.retire(apply=True)["retired"])

    def test_prepare_stop_never_infers_death_from_idle_or_absence(self):
        with self.assertRaisesRegex(ValueError, "no verified"):
            cleanup.prepare_stop(self.project, self.sessions, self.owner)
        result = cleanup.prepare_stop(self.project, self.sessions, self.owner, self.evidence)
        self.assertEqual(cleanup.read_json(result["evidence"])["journals"], self.evidence["journals"])


if __name__ == "__main__":
    unittest.main()
