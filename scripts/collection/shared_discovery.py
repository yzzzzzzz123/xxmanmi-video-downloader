"""Reserve searches and preserve explicit claim handoff and recovery evidence."""

import argparse
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
import uuid
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ROOT = PROJECT_ROOT / "Datebase/xxmanmi-resource-db/.runtime/title-discovery"
LOCK_TIMEOUT = 10.0
HANDOFF_LEDGER_VERSION = 1
ABANDONMENT_LEDGER_VERSION = 1
MAX_HANDOFF_CLAIMS = 8


def normalized(value: str) -> str:
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", value).casefold()).strip()


def required(value: str, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    value = value.strip()
    if not value:
        raise ValueError(f"{label} must not be empty")
    return value


def write_exclusive(path: Path, record: dict) -> None:
    data = (json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


def atomic_write(path: Path, record: dict) -> None:
    """Replace one complete JSON authority file, never a set of claim files."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as stream:
            temporary = stream.name
            json.dump(record, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@contextmanager
def claim_lock(root: Path, timeout: float = LOCK_TIMEOUT):
    """Serialize claim creation and handoff; never break an unknown lock."""
    root.mkdir(parents=True, exist_ok=True)
    lock_path = root / ".search-claims.lock"
    token = uuid.uuid4().hex
    deadline = time.monotonic() + max(0.0, float(timeout))
    payload = {
        "pid": os.getpid(), "token": token,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    while True:
        try:
            write_exclusive(lock_path, payload)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise TimeoutError("search claim lock is busy; unknown lock was not removed")
            time.sleep(min(0.025, max(0.0, deadline - time.monotonic())))
    try:
        yield
    finally:
        try:
            current = json.loads(lock_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("owned search claim lock changed or disappeared") from exc
        if not isinstance(current, dict) or current.get("token") != token:
            raise RuntimeError("search claim lock ownership changed; foreign lock was not removed")
        lock_path.unlink()


def claim_digest(query: str, channel: str) -> str:
    key = normalized(query) + "\0" + normalized(channel)
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def canonical_sha256(value: dict) -> str:
    data = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def read_claim(path: Path, digest: str) -> dict:
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"claim file is incomplete or invalid: {path}") from exc
    if not isinstance(record, dict):
        raise ValueError(f"claim file is not an object: {path}")
    for field in ("query", "channel", "owner", "task_id", "claimed_at"):
        if not isinstance(record.get(field), str) or not record[field].strip():
            raise ValueError(f"claim file has invalid {field}: {path}")
    if claim_digest(record["query"], record["channel"]) != digest:
        raise ValueError(f"claim file does not match its query/channel key: {path}")
    return record


def load_handoff_ledger(root: Path) -> tuple[Path, dict]:
    path = root / "search-claim-handoffs.json"
    if not path.exists():
        return path, {"version": HANDOFF_LEDGER_VERSION, "transfers": []}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("search handoff ledger is incomplete or invalid") from exc
    if (
        not isinstance(ledger, dict)
        or ledger.get("version") != HANDOFF_LEDGER_VERSION
        or not isinstance(ledger.get("transfers"), list)
    ):
        raise ValueError("search handoff ledger has an unsupported structure")

    last_transfer: dict[str, tuple[str, str]] = {}
    for transfer in ledger["transfers"]:
        if not isinstance(transfer, dict) or not isinstance(transfer.get("claims"), list):
            raise ValueError("search handoff ledger contains an invalid transfer")
        transfer_id = transfer.get("transfer_id")
        old_owner = transfer.get("old_owner")
        new_owner = transfer.get("new_owner")
        new_task_id = transfer.get("new_task_id")
        evidence_hash = transfer.get("evidence_sha256")
        if not all(
            isinstance(value, str) and value
            for value in (transfer_id, old_owner, new_owner, new_task_id, transfer.get("transferred_at"))
        ):
            raise ValueError("search handoff ledger contains invalid ownership")
        if not isinstance(evidence_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", evidence_hash):
            raise ValueError("search handoff ledger contains an invalid evidence hash")
        confirmed_stop = transfer.get("old_owner_confirmed") is True
        forced_archived_stop = (
            transfer.get("user_forced_stop") is True
            and transfer.get("session_archived") is True
        )
        if any(
            transfer.get(field) is not True
            for field in ("old_owner_stopped", "new_owner_confirmed", "user_authorized")
        ) or not (confirmed_stop or forced_archived_stop):
            raise ValueError("search handoff ledger contains an unconfirmed transfer")
        if old_owner == new_owner or not 1 <= len(transfer["claims"]) <= MAX_HANDOFF_CLAIMS:
            raise ValueError("search handoff ledger contains an invalid claim set")
        seen: set[str] = set()
        for item in transfer["claims"]:
            if not isinstance(item, dict):
                raise ValueError("search handoff ledger contains an invalid claim entry")
            digest = item.get("digest")
            from_owner = item.get("from_owner")
            from_task_id = item.get("from_task_id")
            source_hash = item.get("source_claim_sha256")
            query = item.get("query")
            channel = item.get("channel")
            if (
                not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or digest in seen or from_owner != old_owner
                or not isinstance(from_task_id, str) or not from_task_id
                or not isinstance(source_hash, str)
                or not re.fullmatch(r"[0-9a-f]{64}", source_hash)
                or not isinstance(query, str) or not isinstance(channel, str)
                or claim_digest(query, channel) != digest
            ):
                raise ValueError("search handoff ledger contains inconsistent claim history")
            previous = last_transfer.get(digest)
            if previous is not None and previous != (from_owner, from_task_id):
                raise ValueError("search handoff ledger claim history is discontinuous")
            seen.add(digest)
            last_transfer[digest] = (new_owner, new_task_id)
    return path, ledger


def parent_host_missing_selector_audit(item: dict) -> bool:
    """Settled private action remains hash-bound; no current DOM or replay fiction."""
    tab_id = item.get('browser_tab_id')
    inventory = item.get('browser_tab_inventory_ids')
    retained = item.get('authorized_retained_action') is True
    missing_errors = {f'No tab with id: {tab_id}.', f'Error: No tab with id: {tab_id}.'}
    if retained and inventory == []:
        message = f'Tab not found: {tab_id}. Existing tabs: none'
        missing_errors.update({message, f'Error: {message}'})
    error = item.get('finalization_gate_error')
    candidate_ok = item.get('browser_evidence_exists') is False or (
        retained and item.get('browser_evidence_exists') is True
        and isinstance(item.get('saved_candidate_evidence'), str)
        and bool(item['saved_candidate_evidence'])
        and re.fullmatch(r'[0-9a-f]{64}', item.get('saved_candidate_evidence_sha256', '')) is not None)
    return (item.get('reason') == 'quarantined_unverifiable_result'
            and item.get('parent_host_authorized') is True
            and re.fullmatch(r'[0-9a-f]{64}', item.get('parent_config_sha256', '')) is not None
            and item.get('browser_host_terminated') is False
            and item.get('browser_tab_exists') is False
            and item.get('browser_host_thread_id') == item.get('owner')
            and isinstance(item.get('browser_host_session_id'), str)
            and bool(item['browser_host_session_id'])
            and item['browser_host_session_id'] != item['browser_host_thread_id']
            and item.get('original_module_instance_available') is True
            and item.get('action_state') == 'completed_unverifiable'
            and type(item.get('action_attempts')) is int and item['action_attempts'] == 1
            and item.get('execute_returned') is True
            and item.get('execute_next_action') == 'finalize_search_action'
            and item.get('finalize_returned') is True
            and type(item.get('finalize_attempts')) is int
            and (item['finalize_attempts'] >= 1 if retained else item['finalize_attempts'] == 1)
            and (isinstance(error, str) and bool(error.strip()) if retained else
                 error == 'Public input value and selected channel must exactly match the claimed search')
            and (not retained or item.get('fault_repair_authorization_basis') == 'direct_user')
            and isinstance(item.get('action_id'), str)
            and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', item['action_id']) is not None
            and item.get('manual_review_required') is True
            and item.get('search_success') is False
            and item.get('logs_stopped') is True
            and item.get('replay_authorized') is False
            and item.get('commit_receipt_exists') is False
            and candidate_ok
            and item.get('search_journal_contains_task') is False
            and item.get('dom_summary') is None
            and isinstance(tab_id, str) and bool(tab_id)
            and item.get('browser_tab_missing_error') in missing_errors
            and isinstance(inventory, list)
            and all(isinstance(v, str) for v in inventory) and tab_id not in inventory)


def parent_host_retired_browser_audit(item: dict) -> bool:
    """Terminal manual review with a live nonce retained dormant, never released."""
    owner, session = item.get('owner'), item.get('browser_host_session_id')
    tab, inventory = item.get('browser_tab_id'), item.get('browser_tab_inventory_ids')
    steps = item.get('terminal_actions')
    if (item.get('reason') != 'quarantined_unverifiable_result'
            or any(item.get(k) is not True for k in (
                'parent_host_authorized', 'authorized_retained_action',
                'retired_browser_retained_action', 'chrome_process_gap_verified',
                'original_module_instance_available', 'manual_review_required', 'logs_stopped'))
            or any(item.get(k) is not False for k in (
                'browser_host_terminated', 'browser_tab_exists', 'public_ui_completed',
                'search_success', 'released', 'replay_authorized', 'browser_evidence_exists',
                'commit_receipt_exists', 'search_journal_contains_task'))
            or item.get('fault_repair_authorization_basis') != 'direct_user'
            or item.get('retained_capability_status') != 'quarantined_dormant_not_released'
            or item.get('action_state') != 'settled_unverifiable_retired_browser'
            or type(item.get('action_attempts')) is not int or item['action_attempts'] != 1
            or not isinstance(owner, str) or not owner or item.get('browser_host_thread_id') != owner
            or not isinstance(session, str) or not session or session == owner
            or re.fullmatch(r'[0-9a-f]{64}', str(item.get('parent_config_sha256', ''))) is None
            or re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                            str(item.get('action_id', ''))) is None
            or not isinstance(tab, str) or not tab or not isinstance(inventory, list)
            or any(not isinstance(v, str) for v in inventory) or tab in inventory
            or item.get('dom_summary') is not None
            or item.get('chrome_absent_diagnostic_exit') != 1
            or item.get('chrome_reopened_running_check_exit') != 0
            or any(not isinstance(item.get(k), str) or not item[k] for k in (
                'chrome_absent_diagnostic_tool', 'chrome_reopened_running_check_source',
                'reopened_browser_inventory_tool', 'original_browser_unavailable_error',
                'finalization_gate_error'))
            or 'Browser is not available:' not in item['original_browser_unavailable_error']
            or not isinstance(steps, list) or len(steps) < 2
            or any(not isinstance(v, dict) or v.get('tool_terminal') is not True
                   or v.get('status') not in ('completed', 'failed')
                   or not isinstance(v.get('tool_id'), str) or not v['tool_id'] for v in steps)
            or len({v['tool_id'] for v in steps}) != len(steps)):
        return False
    if item.get('original_action_state') == 'action_failed':
        return (item.get('execute_returned') is False and item.get('execute_next_action') is None
                and item.get('execute_error') == item['original_browser_unavailable_error']
                and item.get('finalize_returned') is False
                and type(item.get('finalize_attempts')) is int and item['finalize_attempts'] == 0)
    return (item.get('original_action_state') == 'action_unknown'
            and item.get('execute_returned') is True
            and item.get('execute_next_action') == 'inspect_search_action'
            and item.get('execute_reason') == 'click_timeout'
            and item.get('execute_error') is None and item.get('finalize_returned') is True
            and type(item.get('finalize_attempts')) is int and item['finalize_attempts'] >= 1)


def parent_input_missing_tab_audit(item: dict) -> bool:
    """A source-audited zero-submit reset with a truly missing tab is manual only."""
    tab_id = item.get('browser_tab_id')
    inventory = item.get('browser_tab_inventory_ids')
    owner = item.get('owner')
    session = item.get('browser_host_session_id')
    config_hash = item.get('parent_config_sha256')
    steps = item.get('terminal_actions')
    if not (item.get('reason') == 'abandoned_uninspectable_input_action'
            and item.get('parent_input_authorized') is True
            and isinstance(config_hash, str)
            and re.fullmatch(r'[0-9a-f]{64}', config_hash) is not None
            and item.get('fault_repair_authorization_basis') == 'direct_user'
            and isinstance(owner, str) and bool(owner)
            and isinstance(session, str) and bool(session) and session != owner
            and item.get('browser_host_thread_id') == owner
            and item.get('browser_host_terminated') is False
            and item.get('browser_host_session_reset') is True
            and item.get('source_verified_no_submit') is True
            and item.get('browser_tab_exists') is False
            and isinstance(tab_id, str) and bool(tab_id)
            and item.get('browser_tab_missing_error') in {
                f'No tab with id: {tab_id}.', f'Error: No tab with id: {tab_id}.'}
            and isinstance(inventory, list)
            and all(isinstance(v, str) for v in inventory) and tab_id not in inventory
            and item.get('recovery_mode') == 'missing_tab_after_unsubmitted_kernel_reset'
            and item.get('original_module_instance_available') is False
            and item.get('action_state') == 'input_fill_unknown_unsubmitted'
            and item.get('input_result') == 'unknown' and item.get('prepare_result') == 'unknown'
            and all(type(item.get(k)) is int and item[k] == v for k, v in (
                ('action_attempts', 1), ('input_fill_attempts', 1), ('submit_attempts', 0)))
            and item.get('manual_review_required') is True and item.get('search_success') is False
            and item.get('logs_stopped') is True and item.get('replay_authorized') is False
            and all(item.get(k) is False for k in (
                'browser_evidence_exists', 'commit_receipt_exists', 'search_journal_contains_task'))
            and isinstance(steps, list) and len(steps) == 3):
        return False
    operations = ('unsubmitted_fill_prepare', 'readonly_tab_inventory', 'readonly_original_tab_probe')
    if not all(isinstance(step, dict) and type(step.get('sequence')) is int
               and step['sequence'] == index + 1 and step.get('operation') == operations[index]
               and step.get('tool_terminal') is True and isinstance(step.get('tool_id'), str)
               and bool(step['tool_id']) for index, step in enumerate(steps)):
        return False
    return (len({step['tool_id'] for step in steps}) == 3
            and steps[0].get('status') == 'failed'
            and steps[0].get('error') == 'js execution timed out; kernel reset, rerun your request'
            and all(step.get('status') == 'completed' for step in steps[1:])
            and steps[1].get('inventory_tab_ids') == inventory
            and steps[2].get('error') == item['browser_tab_missing_error'])


def terminal_unrecorded_search_audit(item: dict) -> bool:
    """A main-authorized terminal quarantine is never successful search evidence."""
    actions = item.get('terminal_actions')
    if not (item.get('reason') == 'quarantined_terminal_search_without_evidence'
            and item.get('main_authorized') is True
            and item.get('action_state') == 'all_tool_calls_terminal'
            and item.get('browser_action_inflight') is False
            and item.get('original_action_proof_available') is False
            and item.get('manual_review_required') is True
            and item.get('replay_authorized') is False
            and item.get('logs_stopped') is True
            and item.get('browser_host_terminated') is False
            and item.get('browser_host_thread_id') == item.get('owner')
            and isinstance(item.get('browser_host_session_id'), str)
            and bool(item['browser_host_session_id'])
            and isinstance(item.get('browser_tab_exists'), bool)
            and isinstance(item.get('browser_tab_id'), str) and bool(item['browser_tab_id'])
            and isinstance(actions, list) and 1 <= len(actions) <= 16
            and type(item.get('terminal_action_count')) is int and item['terminal_action_count'] == len(actions)
            and all(isinstance(action, dict) and type(action.get('sequence')) is int and action['sequence'] == index
                    and action.get('tool_terminal') is True
                    and isinstance(action.get('operation'), str) and action['operation']
                    for index, action in enumerate(actions, 1))
            and item.get('journals_before') == item.get('journals_after')
            and isinstance(item.get('journals_before'), list)
            and all(item.get(key) is False for key in
                    ('browser_evidence_exists', 'commit_receipt_exists',
                     'search_journal_contains_task', 'capture_lock_exists'))):
        return False
    if item['browser_tab_exists']:
        dom = item.get('dom_summary')
        return (isinstance(dom, dict) and dom.get('input_value_after_action') == item.get('query')
                and dom.get('selected_channels') == [item.get('channel')]
                and dom.get('result_container_visible') is True
                and dom.get('pagination_pending') is False
                and type(dom.get('result_count')) is int and 0 <= dom['result_count'] <= 200)
    inventory = item.get('inventory_tab_ids')
    return (isinstance(inventory, list) and all(isinstance(value, str) for value in inventory)
            and item['browser_tab_id'] not in inventory)


def load_abandonment_ledger(root: Path) -> tuple[Path, dict]:
    """Load terminal search-claim records without making them reusable."""
    path = root / "search-claim-abandonments.json"
    if not path.exists():
        return path, {"version": ABANDONMENT_LEDGER_VERSION, "abandonments": []}
    try:
        ledger = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError("search abandonment ledger is incomplete or invalid") from exc
    if (
        not isinstance(ledger, dict)
        or ledger.get("version") != ABANDONMENT_LEDGER_VERSION
        or not isinstance(ledger.get("abandonments"), list)
    ):
        raise ValueError("search abandonment ledger has an unsupported structure")
    seen: set[str] = set()
    for item in ledger["abandonments"]:
        if not isinstance(item, dict):
            raise ValueError("search abandonment ledger contains an invalid record")
        digest = item.get("digest")
        values = (
            item.get("abandonment_id"), item.get("abandoned_at"),
            item.get("owner"), item.get("task_id"), item.get("query"),
            item.get("channel"), item.get("source_claim_sha256"),
            item.get("evidence_sha256"),
        )
        reason = item.get("reason")
        unknown_action = (
            reason == "abandoned_unknown_action"
            and item.get("browser_host_terminated") is True
            and item.get("browser_tab_exists") is False
            and item.get("original_module_instance_available") is False
            and item.get("action_state") == "unknown"
        )
        unverifiable_result = (
            reason == "quarantined_unverifiable_result"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and isinstance(item.get("original_module_instance_available"), bool)
            and item.get("action_state") == "completed_unverifiable"
            and (item.get("original_module_instance_available") is True
                 or item.get("public_ui_completed") is True)
        )
        foreign_host_quarantine = (
            reason == "quarantined_foreign_host_session"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("original_module_instance_available") is True
            and item.get("action_state") == "completed_unverifiable"
            and item.get("action_attempts") == 1
            and item.get("foreign_host_session_mismatch") is True
            and item.get("browser_host_session_id") != item.get("browser_host_thread_id")
            and isinstance(item.get("browser_host_thread_id"), str)
            and bool(item.get("browser_host_thread_id").strip())
            and isinstance(item.get("browser_host_checked_at"), str)
            and bool(item.get("browser_host_checked_at").strip())
            and isinstance(item.get("browser_host_check_method"), str)
            and bool(item.get("browser_host_check_method").strip())
            and isinstance(item.get("browser_tab_id"), str)
            and bool(item.get("browser_tab_id").strip())
            and isinstance(item.get("browser_tab_checked_at"), str)
            and bool(item.get("browser_tab_checked_at").strip())
            and isinstance(item.get("browser_tab_check_method"), str)
            and bool(item.get("browser_tab_check_method").strip())
            and ((isinstance(item.get("action_id"), str) and bool(item.get("action_id").strip()))
                 or (item.get("action_identity_source") == "legacy_helper_no_id"
                     and item.get("action_id") is None
                     and item.get("helper_entry") == "runSearchActionEvidence"
                     and item.get("helper_call_attempts") == 1
                     and item.get("action_id_emitted") is False
                     and item.get("active_action_capability_available") is False
                     and item.get("helper_returned") is True
                     and item.get("click_returned") == "unknown"
                     and item.get("click_completion_receipt_available") is False
                     and item.get("browser_tab_readable") is True
                     and item.get("browser_tab_session_id") == item.get("browser_host_session_id")
                     and item.get("browser_tab_owner_thread_id") == item.get("owner")
                     and item.get("result_error") == "Zero results require a visible empty-result indicator"
                     and isinstance(item.get("dom_summary"), dict)
                     and item["dom_summary"].get("loading_indicator") is None))
            and isinstance(item.get("result_error"), str)
            and bool(item.get("result_error").strip())
            and item.get("manual_review_required") is True
            and isinstance(item.get("dom_summary"), dict)
            and item["dom_summary"].get("input_value_after_action") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
            and item["dom_summary"].get("result_count") == 0
            and item["dom_summary"].get("visible_detail_links") == []
            and item["dom_summary"].get("empty_result_visible") is False
            and item["dom_summary"].get("pagination_pending") is False
            and item["dom_summary"].get("result_container_visible") is True
        )
        foreign_host_stale_results = (
            reason == "quarantined_foreign_host_stale_results"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("browser_tab_readable") is True
            and item.get("action_state") == "completed_unverifiable"
            and item.get("action_attempts") == 1
            and item.get("foreign_host_session_mismatch") is True
            and item.get("browser_host_session_id") != item.get("browser_host_thread_id")
            and isinstance(item.get("browser_host_thread_id"), str)
            and bool(item.get("browser_host_thread_id").strip())
            and isinstance(item.get("action_id"), str) and bool(item.get("action_id").strip())
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("journals_before") == item.get("journals_after")
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and item.get("capture_lock_exists") is False
            and isinstance(item.get("dom_summary"), dict)
            and isinstance(item.get("pre_action_dom_summary"), dict)
            and item["dom_summary"].get("input_value_after_action") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
            and item["dom_summary"].get("visible_detail_links")
                == item["pre_action_dom_summary"].get("visible_detail_links")
            and bool(item["dom_summary"].get("visible_detail_links"))
            and item["dom_summary"].get("result_count") == len(item["dom_summary"].get("visible_detail_links"))
            and item["pre_action_dom_summary"].get("query") != item.get("query")
        )
        foreign_host_input_mismatch = (
            reason == "quarantined_foreign_host_input_mismatch"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("browser_tab_readable") is True
            and item.get("action_state") == "completed_unverifiable"
            and item.get("action_attempts") == 1
            and item.get("click_returned") is True
            and item.get("foreign_host_session_mismatch") is True
            and item.get("browser_host_session_id") != item.get("browser_host_thread_id")
            and item.get("browser_host_thread_id") == item.get("owner")
            and isinstance(item.get("action_id"), str) and bool(item.get("action_id").strip())
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("journals_before") == item.get("journals_after")
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and item.get("capture_lock_exists") is False
            and isinstance(item.get("dom_summary"), dict)
            and isinstance(item["dom_summary"].get("input_value_after_action"), str)
            and item["dom_summary"].get("input_value_after_action") != item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
            and item["dom_summary"].get("result_count") == 0
            and item["dom_summary"].get("visible_detail_links") == []
            and item["dom_summary"].get("empty_result_visible") is False
            and item["dom_summary"].get("pagination_pending") is False
            and item["dom_summary"].get("result_container_visible") is True
        )
        foreign_host_tab_gone = (
            reason == "quarantined_foreign_host_tab_gone"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is False
            and item.get("original_module_instance_available") is True
            and item.get("action_state") == "completed_unverifiable"
            and item.get("action_attempts") == 1
            and item.get("foreign_host_session_mismatch") is True
            and item.get("browser_host_session_id") != item.get("browser_host_thread_id")
            and item.get("browser_host_thread_id") == item.get("owner")
            and isinstance(item.get("browser_tab_id"), str)
            and isinstance(item.get("browser_tab_missing_error"), str)
            and item["browser_tab_missing_error"] in {
                f"No tab with id: {item.get('browser_tab_id')}",
                f"No tab with id: {item.get('browser_tab_id')}.",
                f"Error: No tab with id: {item.get('browser_tab_id')}",
                f"Error: No tab with id: {item.get('browser_tab_id')}.",
            }
            and item.get("journals_before") == item.get("journals_after")
            and isinstance(item.get("action_id"), str)
            and bool(item.get("action_id").strip())
            and isinstance(item.get("result_error"), str)
            and bool(item.get("result_error").strip())
            and item.get("manual_review_required") is True
            and isinstance(item.get("dom_summary"), dict)
            and item["dom_summary"].get("input_value_after_action") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
            and item["dom_summary"].get("result_count") == 0
            and item["dom_summary"].get("visible_detail_links") == []
            and item["dom_summary"].get("empty_result_visible") is False
            and item["dom_summary"].get("pagination_pending") is False
            and item["dom_summary"].get("result_container_visible") is True
        )
        legacy_evidence_gate = (
            reason == "quarantined_legacy_evidence_gate_failure"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("original_module_instance_available") is True
            and item.get("action_state") == "completed_unverifiable"
            and item.get("action_attempts") == 1
            and item.get("click_returned") is True
            and item.get("legacy_evidence_gate_failure") is True
            and item.get("browser_host_session_id") == item.get("owner")
            and item.get("browser_host_thread_id") == item.get("owner")
            and item.get("old_api_version") == 3
            and item.get("new_api_version") == 4
            and item.get("private_action_record_exportable") is False
            and item.get("finalize_attempts") == 1
            and isinstance(item.get("action_id"), str)
            and bool(item.get("action_id").strip())
            and isinstance(item.get("finalize_gate_error"), str)
            and "Search results did not visibly change from the pre-action snapshot" in item.get("finalize_gate_error")
            and item.get("journals_before") == item.get("journals_after")
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and isinstance(item.get("dom_summary"), dict)
            and item["dom_summary"].get("input_value_after_action") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
            and item["dom_summary"].get("result_count") == 0
            and item["dom_summary"].get("visible_detail_links") == []
            and item["dom_summary"].get("empty_result_visible") is True
            and item["dom_summary"].get("pagination_pending") is False
            and item["dom_summary"].get("result_container_visible") is True
        )
        confirmed_click_tab_gone = (
            reason == "quarantined_confirmed_click_tab_gone"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is False
            and item.get("original_module_instance_available") is True
            and item.get("action_state") == "completed_unverifiable"
            and item.get("action_attempts") == 1
            and item.get("click_returned") is True
            and item.get("browser_host_session_id") == item.get("owner")
            and item.get("browser_host_thread_id") == item.get("owner")
            and isinstance(item.get("browser_tab_id"), str)
            and bool(item.get("browser_tab_id").strip())
            and item.get("browser_tab_missing_error") == f"Error: No tab with id: {item.get('browser_tab_id')}."
            and isinstance(item.get("browser_tab_inventory_ids"), list)
            and all(isinstance(value, str) for value in item.get("browser_tab_inventory_ids"))
            and item.get("browser_tab_id") not in item.get("browser_tab_inventory_ids")
            and isinstance(item.get("action_id"), str)
            and bool(item.get("action_id").strip())
            and item.get("journals_before") == item.get("journals_after")
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and isinstance(item.get("dom_summary"), dict)
            and item["dom_summary"].get("input_value_after_action") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
            and item["dom_summary"].get("result_count") == 0
            and item["dom_summary"].get("visible_detail_links") == []
            and item["dom_summary"].get("empty_result_visible") is True
            and item["dom_summary"].get("pagination_pending") is False
            and item["dom_summary"].get("result_container_visible") is True
        )
        confirmed_click_no_outcome = (
            reason == "abandoned_confirmed_click_no_outcome"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is False
            and item.get("action_state") == "completed_outcome_unavailable"
            and item.get("action_attempts") == 1
            and item.get("click_returned") is True
            and isinstance(item.get("browser_host_session_id"), str)
            and bool(item.get("browser_host_session_id").strip())
            and item.get("browser_host_thread_id") == item.get("owner")
            and isinstance(item.get("browser_tab_id"), str)
            and bool(item.get("browser_tab_id").strip())
            and item.get("browser_tab_missing_error") == f"Error: No tab with id: {item.get('browser_tab_id')}."
            and isinstance(item.get("browser_tab_inventory_ids"), list)
            and all(isinstance(value, str) for value in item.get("browser_tab_inventory_ids"))
            and item.get("browser_tab_id") not in item.get("browser_tab_inventory_ids")
            and isinstance(item.get("action_id"), str)
            and bool(item.get("action_id").strip())
            and item.get("action_task_id") == item.get("task_id")
            and item.get("action_outcome") == "unavailable"
            and item.get("after_dom_exportable") is False
            and isinstance(item.get("original_module_instance_available"), bool)
            and isinstance(item.get("action_result_receipt"), dict)
            and item["action_result_receipt"].get("task_id") == item.get("task_id")
            and item["action_result_receipt"].get("actionId") == item.get("action_id")
            and item["action_result_receipt"].get("next_action") in {"finalize_search_action", "inspect_search_action"}
            and item.get("journals_before") == item.get("journals_after")
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and "dom_summary" not in item
        )
        confirmed_click_stale_non_target = (
            reason == "abandoned_confirmed_click_stale_non_target_result"
            and item.get("browser_host_terminated") is False
            and item.get("browser_host_session_id") == item.get("owner")
            and item.get("browser_host_thread_id") == item.get("owner")
            and item.get("browser_tab_exists") is True
            and item.get("browser_tab_readable") is True
            and isinstance(item.get("browser_tab_id"), str)
            and bool(item.get("browser_tab_id").strip())
            and item.get("action_state") == "completed_outcome_unavailable"
            and item.get("action_attempts") == 1
            and item.get("click_returned") is True
            and isinstance(item.get("action_id"), str)
            and bool(item.get("action_id").strip())
            and item.get("action_outcome") == "unavailable"
            and item.get("after_dom_exportable") is False
            and isinstance(item.get("stable_non_target_result"), dict)
            and item.get("journals_before") == item.get("journals_after")
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
        )
        confirmed_click_helper_gate_error = (
            reason == "abandoned_confirmed_click_helper_gate_error"
            and item.get("browser_host_terminated") is False
            and item.get("browser_host_session_id") == item.get("owner")
            and isinstance(item.get("browser_host_thread_id"), str)
            and bool(item.get("browser_host_thread_id").strip())
            and item.get("browser_tab_exists") is False
            and isinstance(item.get("browser_tab_id"), str)
            and bool(item.get("browser_tab_id").strip())
            and item.get("browser_tab_missing_error") == f"No tab with id: {item.get('browser_tab_id')}."
            and isinstance(item.get("browser_tab_inventory_ids"), list)
            and all(isinstance(value, str) for value in item.get("browser_tab_inventory_ids"))
            and item.get("browser_tab_id") not in item.get("browser_tab_inventory_ids")
            and item.get("action_state") == "completed_outcome_unavailable"
            and item.get("action_attempts") == 1
            and item.get("click_returned") is True
            and item.get("helper_error") == "Search results did not visibly change from the pre-action snapshot; inspect stale or identical results"
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("journals_before") == item.get("journals_after")
            and item.get("browser_evidence_exists") is False
            and item.get("action_result_receipt_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
        )
        invalid_plan = (
            reason == "cancelled_pre_search_invalid_plan"
            and item.get("browser_host_terminated") is False
            and isinstance(item.get("browser_tab_exists"), bool)
            and item.get("original_module_instance_available") is True
            and item.get("action_state") == "not_started_invalid_plan"
            and item.get("action_attempts") == 0
        )
        claim_task_scope_mismatch = (
            reason == "quarantined_claim_task_scope_mismatch"
            and item.get("browser_host_terminated") is False
            and item.get("action_state") == "not_started_scope_mismatch"
            and item.get("action_attempts") == 0
            and item.get("input_fill_attempts") == 0
            and item.get("click_attempts") == 0
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("claim_query") == item.get("query")
            and isinstance(item.get("claim_query"), str)
            and isinstance(item.get("taskbook_query"), str)
            and item.get("taskbook_query") != item.get("claim_query")
            and item.get("claim_query_codepoints") == [ord(char) for char in item.get("claim_query", "")]
            and item.get("taskbook_query_codepoints") == [ord(char) for char in item.get("taskbook_query", "")]
            and item.get("claim_sha256") == item.get("source_claim_sha256")
            and isinstance(item.get("taskbook_sha256"), str)
            and re.fullmatch(r"[0-9a-f]{64}", item.get("taskbook_sha256", "")) is not None
            and item.get("journals_before") == item.get("journals_after")
            and item.get("logs_stopped") is True
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and item.get("capture_lock_exists") is False
        )
        uninspectable_input = (
            reason == "abandoned_uninspectable_input_action"
            and item.get("browser_host_terminated") is False
            and (item.get("browser_tab_exists") is True or parent_input_missing_tab_audit(item))
            and item.get("original_module_instance_available") is False
            and item.get("action_state") == "input_fill_unknown_unsubmitted"
            and item.get("action_attempts") == 1
            and item.get("submit_attempts") == 0
        )
        uninspectable_click = (
            reason == "abandoned_uninspectable_click_action"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("original_module_instance_available") is False
            and item.get("action_state") == "search_click_unknown"
            and item.get("action_attempts") == 1
            and item.get("search_click_attempted") is True
            and item.get("search_click_completed") == "unknown"
        )
        foreign_host_click_unknown = (
            reason == "abandoned_foreign_host_click_unknown"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("browser_host_session_reset") is True
            and item.get("original_module_instance_available") is False
            and item.get("foreign_host_session_mismatch") is True
            and isinstance(item.get("browser_host_session_id"), str)
            and bool(item.get("browser_host_session_id").strip())
            and item.get("browser_host_session_id") != item.get("owner")
            and item.get("browser_host_thread_id") == item.get("owner")
            and item.get("browser_tab_session_id") == item.get("browser_host_session_id")
            and item.get("browser_tab_owner_thread_id") == item.get("owner")
            and item.get("browser_tab_readable") is True
            and item.get("action_state") == "search_click_unknown"
            and item.get("action_attempts") == 1
            and item.get("search_click_attempted") is True
            and item.get("search_click_completed") == "unknown"
            and item.get("active_action_capability_available") is False
            and item.get("action_id_recoverable") is False
            and item.get("execute_receipt") is False
            and item.get("inspect_receipt") is False
            and item.get("finalize_receipt") is False
            and item.get("journals_before") == item.get("journals_after")
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("browser_evidence_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and isinstance(item.get("dom_summary"), dict)
            and item["dom_summary"].get("input_value_after_action") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
        )
        live_owner_click_unknown = (
            reason == "abandoned_live_owner_click_unknown"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("browser_host_session_id") == item.get("owner")
            and isinstance(item.get("browser_host_thread_id"), str)
            and bool(item.get("browser_host_thread_id", "").strip())
            and item.get("browser_host_context_reset") is True
            and item.get("browser_tab_session_id") == item.get("owner")
            and item.get("browser_tab_owner_thread_id") == item.get("browser_host_thread_id")
            and item.get("browser_tab_readable") is True
            and item.get("original_module_instance_available") is False
            and item.get("action_state") == "search_click_unknown"
            and item.get("action_attempts") == 1
            and item.get("helper_call_attempts") == 1
            and item.get("search_click_attempted") is True
            and item.get("search_click_completed") == "unknown"
            and item.get("helper_call_timed_out") is True
            and item.get("action_context_reset") is True
            and item.get("action_id_recoverable") is False
            and item.get("execute_receipt") is False
            and item.get("inspect_receipt") is False
            and item.get("finalize_receipt") is False
            and item.get("journals_before") == item.get("journals_after")
            and item.get("manual_review_required") is True
            and item.get("replay_authorized") is False
            and item.get("browser_evidence_exists") is False
            and item.get("action_result_receipt_exists") is False
            and item.get("commit_receipt_exists") is False
            and item.get("search_journal_contains_task") is False
            and item.get("capture_lock_exists") is False
            and isinstance(item.get("dom_summary"), dict)
            and item["dom_summary"].get("input_value") == item.get("query")
            and item["dom_summary"].get("selected_channels") == [item.get("channel")]
        )
        unavailable_before_start = (
            reason == "cancelled_pre_search_session_unavailable"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is False
            and item.get("original_module_instance_available") is False
            and item.get("action_state") == "not_started_session_unavailable"
            and item.get("action_attempts") == 0
        )
        uninspectable_navigation = (
            reason == "abandoned_uninspectable_navigation_action"
            and item.get("browser_host_terminated") is False
            and item.get("browser_tab_exists") is True
            and item.get("original_module_instance_available") is False
            and item.get("action_state") == "navigation_unknown"
            and item.get("action_attempts") == 1
            and item.get("navigation_attempted") is True
            and item.get("navigation_completed") == "unknown"
        )
        if (
            not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
            or digest in seen
            or any(not isinstance(value, str) or not value for value in values)
            or not (unknown_action or unverifiable_result or foreign_host_quarantine or foreign_host_stale_results or foreign_host_input_mismatch or foreign_host_tab_gone or legacy_evidence_gate or confirmed_click_tab_gone or confirmed_click_no_outcome or confirmed_click_stale_non_target or confirmed_click_helper_gate_error or invalid_plan or claim_task_scope_mismatch or uninspectable_input or uninspectable_click or foreign_host_click_unknown or live_owner_click_unknown or unavailable_before_start or uninspectable_navigation or terminal_unrecorded_search_audit(item) or parent_host_missing_selector_audit(item) or parent_host_retired_browser_audit(item))
            or item.get("replay_authorized") is not False
            or claim_digest(item["query"], item["channel"]) != digest
            or not re.fullmatch(r"[0-9a-f]{64}", item["source_claim_sha256"])
            or not re.fullmatch(r"[0-9a-f]{64}", item["evidence_sha256"])
        ):
            raise ValueError("search abandonment ledger contains inconsistent evidence")
        seen.add(digest)
    return path, ledger


def abandonment_for(digest: str, claim: dict, source_claim_sha256: str,
                    ledger: dict) -> dict | None:
    matches = [item for item in ledger["abandonments"] if item["digest"] == digest]
    if not matches:
        return None
    item = matches[0]
    if (
        item["owner"] != claim["owner"]
        or item["task_id"] != claim["task_id"]
        or item["source_claim_sha256"] != source_claim_sha256
    ):
        raise ValueError("search abandonment ledger does not match claim history")
    return item


def effective_claim(
    original: dict, digest: str, ledger: dict, source_claim_sha256: str,
) -> dict:
    owner = original["owner"]
    task_id = original["task_id"]
    latest_transfer = None
    for transfer in ledger["transfers"]:
        for item in transfer["claims"]:
            if item["digest"] != digest:
                continue
            if item["source_claim_sha256"] != source_claim_sha256:
                raise ValueError("original claim changed after recorded handoff")
            if item["from_owner"] != owner or item["from_task_id"] != task_id:
                raise ValueError("search handoff ledger does not match original claim history")
            owner = transfer["new_owner"]
            task_id = transfer["new_task_id"]
            latest_transfer = transfer["transfer_id"]
    effective = dict(original)
    effective.update({"owner": owner, "task_id": task_id})
    if latest_transfer is not None:
        effective["handoff_id"] = latest_transfer
    return effective


def require_collection_running(root: Path) -> None:
    state_path = root.parent / "collection-coordination/state.json"
    if state_path.exists() and json.loads(state_path.read_text(encoding="utf-8")).get("collection_paused"):
        raise ValueError("collection paused by user; searches and discovery are disabled")


def load_cleanup_ledger(root: Path) -> dict:
    path = root / "search-claim-cleanups.json"
    if not path.exists():
        return {"version": 1, "cancelled": {}}
    value = json.loads(path.read_text(encoding="utf-8"))
    if value.get("version") != 1 or not isinstance(value.get("cancelled"), dict):
        raise ValueError("invalid search cleanup ledger")
    return value


def cleanup_for(digest: str, claim: dict, claim_hash: str, ledger: dict) -> dict | None:
    record = ledger["cancelled"].get(digest)
    if record is not None and (
        record.get("owner") != claim["owner"] or record.get("task_id") != claim["task_id"]
        or record.get("claim_sha256") != claim_hash
        or record.get("status") != "cancelled_owner_cleanup"
    ):
        raise ValueError("search cleanup record does not match claim history")
    return record


def cancel_owner_searches(root: Path, owner: str, evidence: dict) -> dict:
    """Quarantine stopped-owner searches; retain originals and never enable replay."""
    if (evidence.get("thread") != owner or evidence.get("user_authorized") is not True
            or evidence.get("owner_stopped") is not True
            or not evidence.get("method") or not evidence.get("summary")):
        raise ValueError("cleanup requires matching authorization and stop evidence")
    with claim_lock(root):
        _, handoffs = load_handoff_ledger(root)
        ledger = load_cleanup_ledger(root)
        added = 0
        for path in sorted((root / "claims").glob("*.json")):
            digest = path.stem
            original = read_claim(path, digest)
            claim_hash = file_sha256(path)
            current = effective_claim(original, digest, handoffs, claim_hash)
            if current["owner"] != owner:
                continue
            if cleanup_for(digest, current, claim_hash, ledger) is None:
                ledger["cancelled"][digest] = {
                    "owner": owner, "task_id": current["task_id"], "claim_sha256": claim_hash,
                    "status": "cancelled_owner_cleanup", "cancelled_at": datetime.now(timezone.utc).isoformat(),
                    "evidence_sha256": canonical_sha256(evidence), "replay_authorized": False,
                }
                added += 1
        if added:
            atomic_write(root / "search-claim-cleanups.json", ledger)
        return {"cancelled_searches": added}


def claim_search(
    root: Path, query: str, channel: str, owner: str, task_id: str, *,
    lock_timeout: float = LOCK_TIMEOUT,
) -> dict:
    require_collection_running(root)
    query = required(query, "query")
    channel = required(channel, "channel")
    owner = required(owner, "owner")
    task_id = required(task_id, "task_id")
    digest = claim_digest(query, channel)
    claims = root / "claims"
    path = claims / f"{digest}.json"
    record = {
        "query": query,
        "channel": channel,
        "owner": owner,
        "task_id": task_id,
        "claimed_at": datetime.now(timezone.utc).isoformat(),
    }
    with claim_lock(root, lock_timeout):
        claims.mkdir(parents=True, exist_ok=True)
        try:
            _ledger_path, ledger = load_handoff_ledger(root)
            _abandonment_path, abandonment_ledger = load_abandonment_ledger(root)
            if path.exists():
                original = read_claim(path, digest)
                claim_hash = file_sha256(path)
                existing = effective_claim(original, digest, ledger, claim_hash)
                if cleanup_for(digest, existing, claim_hash, load_cleanup_ledger(root)) is not None:
                    return {"ok": False, "reason": "cancelled_owner_cleanup", "path": str(path),
                            "claim": {**existing, "status": "cancelled_owner_cleanup"}}
                abandoned = abandonment_for(digest, existing, claim_hash, abandonment_ledger)
                if abandoned is not None:
                    return {
                        "ok": False, "reason": abandoned["reason"],
                        "path": str(path), "claim_sha256": claim_hash,      
                        "claim": {**existing, "status": abandoned["reason"],
                                  "abandonment_id": abandoned["abandonment_id"]},
                    }
                same_task = existing["owner"] == owner and existing["task_id"] == task_id
                return {
                    "ok": same_task,
                    "reason": "already_owned" if same_task else "already_claimed",
                    "path": str(path),
                    "claim_sha256": claim_hash,
                    "claim": existing,
                }
            if any(
                item["digest"] == digest
                for transfer in ledger["transfers"] for item in transfer["claims"]
            ):
                raise ValueError("handoff ledger refers to a missing original claim")
            write_exclusive(path, record)
        except (OSError, ValueError):
            return {
                "ok": False, "reason": "claim_incomplete_review_required",
                "path": str(path),
            }
        return {
            "ok": True, "reason": "claimed", "path": str(path),
            "claim_sha256": file_sha256(path), "claim": record,
        }


def inspect_claim(
    root: Path, query: str, channel: str, *, lock_timeout: float = LOCK_TIMEOUT,
) -> dict:
    """Read the authoritative effective owner without changing original evidence."""
    query = required(query, "query")
    channel = required(channel, "channel")
    digest = claim_digest(query, channel)
    path = root / "claims" / f"{digest}.json"
    with claim_lock(root, lock_timeout):
        _ledger_path, ledger = load_handoff_ledger(root)
        _abandonment_path, abandonment_ledger = load_abandonment_ledger(root)
        if not path.exists():
            if any(
                item["digest"] == digest
                for transfer in ledger["transfers"] for item in transfer["claims"]
            ):
                raise ValueError("handoff ledger refers to a missing original claim")
            return {"ok": True, "reason": "unclaimed", "claim": None}
        original = read_claim(path, digest)
        claim_hash = file_sha256(path)
        current = effective_claim(original, digest, ledger, claim_hash)
        if cleanup_for(digest, current, claim_hash, load_cleanup_ledger(root)) is not None:
            return {"ok": True, "reason": "cancelled_owner_cleanup", "path": str(path),
                    "claim_sha256": claim_hash,
                    "claim": {**current, "status": "cancelled_owner_cleanup"}}
        abandoned = abandonment_for(digest, current, claim_hash, abandonment_ledger)
        if abandoned is not None:
            return {
                "ok": True, "reason": abandoned["reason"], "path": str(path),
                "claim_sha256": claim_hash,
                "original_owner": original["owner"],
                "original_task_id": original["task_id"],
                "claim": {**current, "status": abandoned["reason"],
                          "abandonment_id": abandoned["abandonment_id"]},       
                "abandonment": abandoned,
            }
        return {
            "ok": True, "reason": "claimed", "path": str(path),
            "claim_sha256": claim_hash,
            "original_owner": original["owner"],
            "original_task_id": original["task_id"],
            "claim": current,
        }


def abandon_search(
    root: Path, owner: str, task_id: str, query: str, channel: str,
    claim_sha256: str, evidence_sha256: str, audit: dict,
    *, lock_timeout: float = LOCK_TIMEOUT,
) -> dict:
    """Append one terminal audit record; never delete or reopen the claim."""
    owner = required(owner, "owner")
    task_id = required(task_id, "task_id")
    query = required(query, "query")
    channel = required(channel, "channel")
    digest = claim_digest(query, channel)
    if not re.fullmatch(r"[0-9a-f]{64}", claim_sha256 or ""):
        raise ValueError("abandonment requires a valid claim_sha256")
    if not re.fullmatch(r"[0-9a-f]{64}", evidence_sha256 or ""):
        raise ValueError("abandonment requires a valid evidence_sha256")
    required_audits = {
        "quarantined_terminal_search_without_evidence": {
            "main_authorized": True, "action_state": "all_tool_calls_terminal",
            "browser_action_inflight": False, "original_action_proof_available": False,
            "manual_review_required": True, "replay_authorized": False,
        },
        "abandoned_unknown_action": {
            "browser_host_terminated": True, "browser_tab_exists": False,
            "original_module_instance_available": False, "action_state": "unknown",
            "replay_authorized": False,
        },
        "quarantined_unverifiable_result": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "action_state": "completed_unverifiable", "replay_authorized": False,
        },
        "quarantined_foreign_host_session": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "foreign_host_session_mismatch": True,
            "browser_host_thread_id": owner,
            "manual_review_required": True, "replay_authorized": False,
        },
        "quarantined_foreign_host_stale_results": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "browser_tab_readable": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "foreign_host_session_mismatch": True,
            "browser_host_thread_id": owner,
            "manual_review_required": True, "replay_authorized": False,
        },
        "quarantined_foreign_host_input_mismatch": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "browser_tab_readable": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "click_returned": True, "foreign_host_session_mismatch": True,
            "browser_host_thread_id": owner,
            "manual_review_required": True, "replay_authorized": False,
        },
        "quarantined_foreign_host_tab_gone": {
            "browser_host_terminated": False, "browser_tab_exists": False,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "foreign_host_session_mismatch": True,
            "browser_host_thread_id": owner,
            "manual_review_required": True, "replay_authorized": False,
        },
        "quarantined_legacy_evidence_gate_failure": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "click_returned": True, "legacy_evidence_gate_failure": True,
            "private_action_record_exportable": False,
            "finalize_attempts": 1, "manual_review_required": True,
            "replay_authorized": False,
        },
        "quarantined_legacy_evidence_gate_failure": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "legacy_evidence_gate_failure": True,
            "manual_review_required": True, "replay_authorized": False,
        },
        "quarantined_confirmed_click_tab_gone": {
            "browser_host_terminated": False, "browser_tab_exists": False,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "click_returned": True, "browser_host_session_id": owner,
            "browser_host_thread_id": owner, "manual_review_required": True,
            "replay_authorized": False,
        },
        "abandoned_confirmed_click_no_outcome": {
            "browser_host_terminated": False, "browser_tab_exists": False,
            "action_state": "completed_outcome_unavailable", "action_attempts": 1,
            "click_returned": True, "action_outcome": "unavailable",
            "after_dom_exportable": False, "manual_review_required": True,
            "replay_authorized": False,
        },
        "abandoned_confirmed_click_stale_non_target_result": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "browser_tab_readable": True, "browser_host_session_id": owner,
            "browser_host_thread_id": owner,
            "action_state": "completed_outcome_unavailable", "action_attempts": 1,
            "click_returned": True, "action_outcome": "unavailable",
            "after_dom_exportable": False, "manual_review_required": True,
            "replay_authorized": False,
        },
        "abandoned_confirmed_click_helper_gate_error": {
            "browser_host_terminated": False, "browser_tab_exists": False,
            "browser_host_session_id": owner,
            "action_state": "completed_outcome_unavailable", "action_attempts": 1,
            "click_returned": True, "manual_review_required": True,
            "replay_authorized": False,
        },
        "cancelled_pre_search_invalid_plan": {
            "browser_host_terminated": False,
            "original_module_instance_available": True,
            "action_state": "not_started_invalid_plan", "action_attempts": 0,
            "replay_authorized": False,
        },
        "quarantined_claim_task_scope_mismatch": {
            "browser_host_terminated": False,
            "action_state": "not_started_scope_mismatch", "action_attempts": 0,
            "input_fill_attempts": 0, "click_attempts": 0,
            "manual_review_required": True, "replay_authorized": False,
        },
        "abandoned_uninspectable_input_action": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": False,
            "action_state": "input_fill_unknown_unsubmitted", "action_attempts": 1,
            "submit_attempts": 0, "replay_authorized": False,
        },
        "abandoned_uninspectable_click_action": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": False,
            "action_state": "search_click_unknown", "action_attempts": 1,
            "search_click_attempted": True, "search_click_completed": "unknown",
            "replay_authorized": False,
        },
        "abandoned_foreign_host_click_unknown": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "browser_host_session_reset": True,
            "original_module_instance_available": False,
            "foreign_host_session_mismatch": True,
            "action_state": "search_click_unknown", "action_attempts": 1,
            "search_click_attempted": True, "search_click_completed": "unknown",
            "manual_review_required": True, "replay_authorized": False,
        },
        "abandoned_live_owner_click_unknown": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "browser_host_session_id": owner,
            "browser_host_context_reset": True,
            "browser_tab_session_id": owner,
            "original_module_instance_available": False,
            "action_state": "search_click_unknown", "action_attempts": 1,
            "helper_call_attempts": 1,
            "search_click_attempted": True, "search_click_completed": "unknown",
            "helper_call_timed_out": True, "action_context_reset": True,
            "action_id_recoverable": False,
            "execute_receipt": False, "inspect_receipt": False, "finalize_receipt": False,
            "manual_review_required": True, "replay_authorized": False,
        },
        "cancelled_pre_search_session_unavailable": {
            "browser_host_terminated": False, "browser_tab_exists": False,
            "original_module_instance_available": False,
            "action_state": "not_started_session_unavailable", "action_attempts": 0,
            "replay_authorized": False,
        },
        "abandoned_uninspectable_navigation_action": {
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": False,
            "action_state": "navigation_unknown", "action_attempts": 1,
            "navigation_attempted": True, "navigation_completed": "unknown",
            "replay_authorized": False,
        },
    }
    reason = audit.get("reason") if isinstance(audit, dict) else None
    required_audit = required_audits.get(reason)
    if (reason == 'quarantined_unverifiable_result'
            and parent_host_missing_selector_audit({**audit, 'owner': owner})):
        required_audit = {**required_audit, 'browser_tab_exists': False}
    if (reason == 'quarantined_unverifiable_result'
            and parent_host_retired_browser_audit({**audit, 'owner': owner})):
        required_audit = {**required_audit, 'browser_tab_exists': False,
                          'action_state': 'settled_unverifiable_retired_browser'}
    if (reason == 'abandoned_uninspectable_input_action'
            and parent_input_missing_tab_audit({**audit, 'owner': owner})):
        required_audit = {**required_audit, 'browser_tab_exists': False}
    if (required_audit is None or any(audit.get(k) != v for k, v in required_audit.items())
            or (reason == "quarantined_terminal_search_without_evidence"
                and not terminal_unrecorded_search_audit({**audit, 'owner': owner, 'query': query, 'channel': channel}))
            or (reason == "cancelled_pre_search_invalid_plan"
                and not isinstance(audit.get("browser_tab_exists"), bool))
            or (reason == "quarantined_unverifiable_result"
                and (not isinstance(audit.get("original_module_instance_available"), bool)
                     or (audit.get("original_module_instance_available") is False
                         and audit.get("public_ui_completed") is not True)))):
        raise ValueError("abandonment audit does not prove a terminal non-replayable action")
    with claim_lock(root, lock_timeout):
        _handoff_path, handoffs = load_handoff_ledger(root)
        ledger_path, abandonments = load_abandonment_ledger(root)
        claim_path = root / "claims" / f"{digest}.json"
        if not claim_path.exists():
            raise ValueError("search claim does not exist")
        original = read_claim(claim_path, digest)
        actual_hash = file_sha256(claim_path)
        if actual_hash != claim_sha256:
            raise ValueError("search claim evidence hash changed")
        current = effective_claim(original, digest, handoffs, actual_hash)
        if current["owner"] != owner or current["task_id"] != task_id:
            raise ValueError("search abandonment owner/task does not match effective claim")
        existing = abandonment_for(digest, current, actual_hash, abandonments)
        if existing is not None:
            if existing["evidence_sha256"] != evidence_sha256:
                raise ValueError("search claim was already abandoned with different evidence")
            return {
                "ok": True, "reason": "already_" + reason,
                "abandonment_id": existing["abandonment_id"],
                "ledger": str(ledger_path),
            }
        record = {
            "abandonment_id": "abandon-" + uuid.uuid4().hex,
            "abandoned_at": datetime.now(timezone.utc).isoformat(),
            "owner": owner, "task_id": task_id, "query": query, "channel": channel,
            "digest": digest, "source_claim_sha256": actual_hash,
            "evidence_sha256": evidence_sha256,
            **audit,
        }
        abandonments["abandonments"].append(record)
        atomic_write(ledger_path, abandonments)
        return {
            "ok": True, "reason": reason,
            "abandonment_id": record["abandonment_id"],
            "ledger": str(ledger_path),
        }


def clean_handoff_evidence(
    evidence: dict, old_owner: str, new_owner: str, new_task_id: str,
    force_user_authorized: bool = False,
) -> list[dict]:
    if not isinstance(evidence, dict):
        raise ValueError("handoff evidence must be a JSON object")
    expected = {
        "old_owner": old_owner,
        "new_owner": new_owner,
        "new_task_id": new_task_id,
    }
    for field, value in expected.items():
        if evidence.get(field) != value:
            raise ValueError(f"handoff evidence {field} does not match the command")
    for field in ("new_owner_confirmed", "user_authorized", "old_owner_stopped"):
        if evidence.get(field) is not True:
            raise ValueError(f"handoff evidence requires {field}=true")
    confirmed_stop = evidence.get("old_owner_confirmed") is True
    forced_archived_stop = (
        force_user_authorized is True
        and evidence.get("user_forced_stop") is True
        and evidence.get("session_archived") is True
        and evidence.get("archived_session_id") == old_owner
        and isinstance(evidence.get("archive_method"), str)
        and bool(evidence["archive_method"].strip())
    )
    if not (confirmed_stop or forced_archived_stop):
        raise ValueError(
            "handoff evidence requires old_owner_confirmed=true or an explicitly authorized archived-session stop"
        )
    claims = evidence.get("claims")
    if not isinstance(claims, list) or not 1 <= len(claims) <= MAX_HANDOFF_CLAIMS:
        raise ValueError("handoff evidence must list between 1 and 8 claims")
    cleaned = []
    seen: set[str] = set()
    for item in claims:
        if not isinstance(item, dict):
            raise ValueError("each handoff claim must be an object")
        query = required(item.get("query", ""), "query")
        channel = required(item.get("channel", ""), "channel")
        digest = claim_digest(query, channel)
        expected_hash = item.get("claim_sha256")
        if not isinstance(expected_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", expected_hash):
            raise ValueError("each handoff claim requires a valid claim_sha256")
        if digest in seen:
            raise ValueError("handoff evidence contains a duplicate query/channel claim")
        seen.add(digest)
        cleaned.append({
            "query": query, "channel": channel, "digest": digest,
            "claim_sha256": expected_hash.lower(),
        })
    return cleaned


def handoff_search(
    root: Path, old_owner: str, new_owner: str, new_task_id: str,
    evidence: dict, *, force_user_authorized: bool = False,
    lock_timeout: float = LOCK_TIMEOUT,
) -> dict:
    """Atomically append one all-or-nothing authority override for 1-8 claims."""
    old_owner = required(old_owner, "old_owner")
    new_owner = required(new_owner, "new_owner")
    new_task_id = required(new_task_id, "new_task_id")
    if old_owner == new_owner:
        raise ValueError("old_owner and new_owner must be different")
    cleaned = clean_handoff_evidence(
        evidence, old_owner, new_owner, new_task_id, force_user_authorized,
    )

    with claim_lock(root, lock_timeout):
        ledger_path, ledger = load_handoff_ledger(root)
        _abandonment_path, abandonment_ledger = load_abandonment_ledger(root)
        transferred_claims = []
        for item in cleaned:
            path = root / "claims" / f"{item['digest']}.json"
            if not path.exists():
                raise ValueError("handoff claim does not exist")
            original = read_claim(path, item["digest"])
            claim_hash = file_sha256(path)
            if claim_hash != item["claim_sha256"]:
                raise ValueError("handoff claim evidence hash changed")
            current = effective_claim(original, item["digest"], ledger, claim_hash)
            if cleanup_for(item["digest"], current, claim_hash, load_cleanup_ledger(root)) is not None:
                raise ValueError("cleaned-up claims cannot be handed off or replayed")
            if abandonment_for(item["digest"], current, claim_hash, abandonment_ledger) is not None:
                raise ValueError("abandoned unknown-action claims cannot be handed off or replayed")
            if current["owner"] != old_owner:
                raise ValueError("handoff old_owner does not own every listed claim")
            if current["task_id"] == new_task_id:
                raise ValueError("new_task_id must identify a new task book")
            transferred_claims.append({
                "query": original["query"], "channel": original["channel"],
                "digest": item["digest"],
                "source_claim_sha256": item["claim_sha256"],
                "from_owner": current["owner"],
                "from_task_id": current["task_id"],
            })

        transfer_id = "handoff-" + uuid.uuid4().hex
        transferred_at = datetime.now(timezone.utc).isoformat()
        ledger["transfers"].append({
            "transfer_id": transfer_id,
            "old_owner": old_owner,
            "new_owner": new_owner,
            "new_task_id": new_task_id,
            "transferred_at": transferred_at,
            "old_owner_stopped": True,
            "old_owner_confirmed": evidence.get("old_owner_confirmed") is True,
            "user_forced_stop": evidence.get("user_forced_stop") is True,
            "session_archived": evidence.get("session_archived") is True,
            "new_owner_confirmed": True,
            "user_authorized": True,
            "evidence_sha256": canonical_sha256(evidence),
            "claims": transferred_claims,
        })
        atomic_write(ledger_path, ledger)
        return {
            "ok": True, "reason": "handed_off", "transfer_id": transfer_id,
            "count": len(transferred_claims), "new_owner": new_owner,
            "new_task_id": new_task_id, "ledger": str(ledger_path),
        }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "command", choices=["claim-search", "inspect-claim", "handoff-search"],
    )
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--owner")
    parser.add_argument("--task-id")
    parser.add_argument("--query")
    parser.add_argument("--channel")
    parser.add_argument("--old-owner")
    parser.add_argument("--new-owner")
    parser.add_argument("--new-task-id")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--force-user-authorized", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "claim-search":
            result = claim_search(
                args.root, args.query or "", args.channel or "",
                args.owner or "", args.task_id or "",
            )
        elif args.command == "inspect-claim":
            result = inspect_claim(args.root, args.query or "", args.channel or "")
        else:
            if args.evidence is None:
                raise ValueError("handoff-search requires --evidence")
            try:
                evidence = json.loads(args.evidence.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise ValueError("handoff evidence file is incomplete or invalid") from exc
            result = handoff_search(
                args.root, args.old_owner or "", args.new_owner or "",
                args.new_task_id or "", evidence,
                force_user_authorized=args.force_user_authorized,
            )
    except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
        result = {"ok": False, "reason": str(exc)}
    # Keep CLI transport ASCII-safe under Windows legacy console encodings;
    # persisted evidence remains UTF-8 with original text.
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0 if result["ok"] else 2


if __name__ == "__main__":
    sys.exit(main())
