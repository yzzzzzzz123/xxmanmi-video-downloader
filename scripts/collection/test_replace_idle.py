import concurrent.futures
import hashlib
import json
from pathlib import Path
import tempfile
import unittest

import coordctl
from test_coordctl import observed_item, verification_for


class ReplaceIdleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.state = self.root / "state.json"
        self.work = self.root / "old-work"
        self.work.mkdir()
        self.log = self.work / "details.ndjson"
        self.log.write_bytes(b'{"kept":"original evidence"}\n')
        coordctl.init_state(self.state, "old", str(self.work))
        coordctl.register_thread(self.state, "peer-b", work=str(self.root / "b"))
        coordctl.register_thread(self.state, "peer-c", work=str(self.root / "c"))

    def evidence(self, new="new"):
        data = self.log.read_bytes()
        return {
            "previous_thread": "old", "replacement_thread": new,
            "user_authorized": True, "owner_stopped": True,
            "method": "owner confirms stop; host idle; journal snapshot",
            "summary": "User explicitly requests replacing the stopped idle owner.",
            "journals": [{"name": self.log.name, "size": len(data),
                          "sha256": hashlib.sha256(data).hexdigest()}],
        }

    def replace(self, new="new", evidence=None, stopped=True, work=None):
        return coordctl.replace_idle_thread(
            self.state, new, "old", str(work or self.work), "explicit user handoff",
            evidence if evidence is not None else self.evidence(new), stopped,
        )

    def test_history_files_and_peer_claims_are_preserved(self):
        coordctl.enqueue_items(self.state, "old", [observed_item(1), observed_item(2)])
        old_batch = coordctl.claim_batch(self.state, "old", 1)
        coordctl.ready_batch(self.state, "old", old_batch["batch"],
                             [{"url": old_batch["items"][0]["url"], "outcome": "skipped"}], True)
        coordctl.acknowledge_batch(self.state, "old", old_batch["batch"], verification_for(old_batch))
        coordctl.claim_batch(self.state, "peer-b", 1)
        before = json.loads(self.state.read_text())
        log_before = self.log.read_bytes()
        result = self.replace()
        after = json.loads(self.state.read_text())
        self.assertEqual(result["registered_workers"], 3)
        self.assertNotIn("max_workers", after)
        for key in ("batches", "items", "queue", "next_batch"):
            self.assertEqual(before[key], after[key])
        self.assertEqual(before["threads"]["peer-b"], after["threads"]["peer-b"])
        self.assertEqual(self.log.read_bytes(), log_before)
        self.assertEqual(coordctl.current_batch(self.state, "new")["work"], str(self.work))
        self.assertEqual(after["retired_threads"]["old"]["replaced_by"], "new")
        with self.assertRaisesRegex(coordctl.CoordinationError, "replaced"):
            coordctl.register_thread(self.state, "old", work=str(self.work))
        with self.assertRaisesRegex(coordctl.CoordinationError, "not registered"):
            coordctl.claim_batch(self.state, "old", 1)

    def test_inflight_owner_and_stale_snapshot_fail_without_mutation(self):
        proof = self.evidence()
        self.log.write_bytes(b'{"changed":true}\n')
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "snapshot changed"):
            self.replace(evidence=proof)
        self.assertEqual(before, self.state.read_bytes())
        coordctl.enqueue_items(self.state, "old", [observed_item(3)])
        claim = coordctl.claim_batch(self.state, "old", 1)
        for ready in (False, True):
            if ready:
                coordctl.ready_batch(self.state, "old", claim["batch"],
                                     [{"url": claim["items"][0]["url"], "outcome": "raw"}], True)
            before = self.state.read_bytes()
            with self.assertRaisesRegex(coordctl.CoordinationError, "idle owner"):
                self.replace()
            self.assertEqual(before, self.state.read_bytes())

    def test_explicit_authorization_and_stopped_owner_are_required(self):
        for field in ("user_authorized", "owner_stopped", "previous_thread", "replacement_thread", "method", "summary"):
            proof = self.evidence()
            proof.pop(field)
            before = self.state.read_bytes()
            with self.assertRaises(coordctl.CoordinationError):
                self.replace(evidence=proof)
            self.assertEqual(before, self.state.read_bytes())
        with self.assertRaisesRegex(coordctl.CoordinationError, "logs_stopped"):
            self.replace(stopped=False)

    def test_wrong_work_existing_identity_and_partial_journal_fail_atomically(self):
        for values in ({"work": self.root / "wrong"}, {"new": "peer-b"}, {"new": "old"}):
            before = self.state.read_bytes()
            with self.assertRaises(coordctl.CoordinationError):
                self.replace(**values)
            self.assertEqual(before, self.state.read_bytes())
        self.log.write_bytes(b'{"partial":')
        before = self.state.read_bytes()
        with self.assertRaisesRegex(coordctl.CoordinationError, "incomplete"):
            self.replace()
        self.assertEqual(before, self.state.read_bytes())

    def test_two_replacements_racing_only_one_gets_the_slot(self):
        def attempt(new):
            try:
                return self.replace(new=new)["ok"]
            except coordctl.CoordinationError:
                return False
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(attempt, ("new-a", "new-b")))
        self.assertEqual(sorted(results), [False, True])
        self.assertEqual(coordctl.status_state(self.state)["registered_workers"], 3)


if __name__ == "__main__":
    unittest.main()
