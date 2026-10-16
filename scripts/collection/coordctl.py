#!/usr/bin/env python3
"""Small cross-process coordinator for explicitly observed detail-page batches.

This program coordinates state only.  It does not discover or start Codex
processes, control a browser, or query a database. Explicit idle handoffs inspect
stopped journal snapshots but never change journal files.
"""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Collection
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import os
import re
from pathlib import Path
import secrets
import sys
import tempfile
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit


STATE_VERSION = 1
MAX_CLAIM_SIZE = 8
OUTCOMES = frozenset({"captured", "raw", "failed", "skipped"})
ROLES = frozenset({"peer", "coordinator", "collector"})
DEFAULT_LOCK_TIMEOUT = 10.0


class CoordinationError(RuntimeError):
    """Expected state, authorization, or validation error."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_name(value: str, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise CoordinationError(f"{label} must be a nonempty trimmed string")
    if len(value) > 128 or any(ord(char) < 32 for char in value):
        raise CoordinationError(f"{label} contains unsupported characters")
    return value


def _work_path(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.strip():
        raise CoordinationError("work must be a nonempty path")
    # strict=False semantics: resolve every existing component (including a
    # Windows junction/symlink) without requiring a not-yet-created leaf.
    physical = os.path.realpath(os.path.abspath(os.path.normpath(value.strip())))
    key = os.path.normcase(physical).replace("\\", "/").rstrip("/")
    return physical, key


def _page_url(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise CoordinationError("each item requires a detail-page URL")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise CoordinationError("detail-page URLs must use HTTP(S)")
    if parsed.username is not None or parsed.password is not None:
        raise CoordinationError("detail-page URLs must not contain credentials")
    host = parsed.hostname.lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    try:
        port = parsed.port
    except ValueError as error:
        raise CoordinationError("detail-page URL has an invalid port") from error
    default_port = (parsed.scheme.lower() == "http" and port == 80) or (
        parsed.scheme.lower() == "https" and port == 443
    )
    netloc = host if port is None or default_port else f"{host}:{port}"
    return urlunsplit((parsed.scheme.lower(), netloc, parsed.path or "/", parsed.query, ""))


def _load_json_file(path: str | Path, collection_key: str | None = None) -> Any:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as error:
        raise CoordinationError(f"cannot read valid JSON from {path}: {error}") from error
    if collection_key and isinstance(value, dict):
        value = value.get(collection_key)
    return value


class StateLock:
    """Exclusive lock file which never breaks or deletes somebody else's lock."""

    def __init__(self, state_path: str | Path, timeout: float = DEFAULT_LOCK_TIMEOUT):
        self.state_path = Path(state_path)
        self.lock_path = self.state_path.with_name(self.state_path.name + ".lock")
        self.timeout = max(0.0, float(timeout))
        self.token = secrets.token_hex(16)

    def __enter__(self) -> "StateLock":
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.monotonic() + self.timeout
        payload = json.dumps(
            {"pid": os.getpid(), "created_at": utc_now(), "token": self.token},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        while True:
            try:
                fd = os.open(self.lock_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, payload)
                    os.fsync(fd)
                finally:
                    os.close(fd)
                return self
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise CoordinationError(
                        f"state lock is busy; an existing/stale lock was not removed: {self.lock_path}"
                    )
                time.sleep(0.025)

    def __exit__(self, exc_type, exc, traceback) -> None:
        try:
            lock = json.loads(self.lock_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            if exc_type is None:
                raise CoordinationError("owned state lock changed or disappeared; it was not removed")
            return
        if lock.get("token") != self.token:
            if exc_type is None:
                raise CoordinationError("state lock ownership changed; foreign lock was not removed")
            return
        self.lock_path.unlink()


def _read_state(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise CoordinationError(f"state does not exist: {path}") from error
    except (OSError, json.JSONDecodeError) as error:
        raise CoordinationError(f"cannot read valid coordinator state: {error}") from error
    if not isinstance(state, dict) or state.get("version") != STATE_VERSION:
        raise CoordinationError("unsupported or invalid coordinator state")
    return state


def replace_state_file(source: str, destination: Path) -> None:
    """Retry only the same atomic replacement on bounded Windows file errors."""
    for attempt in range(11):
        try:
            os.replace(source, destination)
            return
        except OSError as error:
            if getattr(error, "winerror", None) not in {5, 32, 33} or attempt == 10:
                raise
            time.sleep(0.05)


def _atomic_write(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_name = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as handle:
            temp_name = handle.name
            json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_state_file(temp_name, path)
        temp_name = None
    finally:
        if temp_name:
            try:
                os.unlink(temp_name)
            except FileNotFoundError:
                pass


def _update(
    state_path: str | Path,
    operation: Callable[[dict[str, Any]], tuple[dict[str, Any], bool]],
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    path = Path(state_path)
    with StateLock(path, lock_timeout):
        state = _read_state(path)
        result, changed = operation(state)
        if changed:
            state["updated_at"] = utc_now()
            _atomic_write(path, state)
        return result


def _require_thread(state: dict[str, Any], thread: str) -> dict[str, Any]:
    thread = _validate_name(thread, "thread")
    record = state["threads"].get(thread)
    if not record:
        raise CoordinationError(f"thread is not registered: {thread}")
    return record


def _require_peer(state: dict[str, Any], thread: str) -> dict[str, Any]:
    """Authorize a registered trusted peer; persisted legacy roles grant no privileges."""
    require_collection_running(state)
    if state.get('execution_mode') == 'single' and thread != state.get('collection_active_owner'):
        raise CoordinationError('single agent mode: historical collectors cannot write')
    return _require_thread(state, thread)


def require_collection_running(state: dict[str, Any]) -> None:
    if state.get("collection_paused"):
        raise CoordinationError("collection paused by user; only inspection and cleanup are allowed")


def init_state(
    state_path: str | Path,
    thread: str,
    work: str,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    thread = _validate_name(thread, "thread")
    work_path, work_key = _work_path(work)
    path = Path(state_path)
    with StateLock(path, lock_timeout):
        if path.exists():
            raise CoordinationError(f"state already exists: {path}")
        now = utc_now()
        state = {
            "version": STATE_VERSION,
            "created_at": now,
            "updated_at": now,
            "coordination_mode": "peer",
            "reporting_mode": "per_thread",
            "next_batch": 1,
            "threads": {
                thread: {
                    "role": "peer",
                    "work": work_path,
                    "work_key": work_key,
                    "registered_at": now,
                    "mode": "idle",
                    "active_batch": None,
                }
            },
            "queue": [],
            "items": {},
            "batches": {},
        }
        _atomic_write(path, state)
    return {
        "ok": True, "command": "init", "thread": thread,
        "coordination_mode": "peer", "reporting_mode": "per_thread",
        "worker_limit": None,
    }


def unlimit_state(
    state_path: str | Path,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Remove a legacy worker cap under the same lock as every state update."""
    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        had_limit = "max_workers" in state
        previous = state.pop("max_workers", None)
        return {
            "ok": True, "command": "unlimit", "previous_limit": previous,
            "worker_limit": None, "registered_workers": len(state["threads"]),
        }, had_limit

    return _update(state_path, operation, lock_timeout)


def register_thread(
    state_path: str | Path,
    thread: str,
    role: str | None = None,
    work: str | None = None,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    thread = _validate_name(thread, "thread")
    if role is None:
        role = "peer"
    if role not in ROLES:
        raise CoordinationError("role must be peer, coordinator, or collector")
    if work is None:
        raise CoordinationError("work must be provided")
    work_path, work_key = _work_path(work)

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if thread in state.get("retired_threads", {}):
            raise CoordinationError("thread was replaced; its old registration cannot be resumed")
        require_collection_running(state)
        if state.get('execution_mode')=='single' and thread!=state.get('collection_active_owner'):
            raise CoordinationError('single agent mode does not register collection subagents')
        existing = state["threads"].get(thread)
        if existing:
            if existing["work_key"] == work_key:
                return {
                    "ok": True, "command": "register", "thread": thread,
                    "role": "peer", "registered": False,
                }, False
            raise CoordinationError("thread is already registered with different work")
        if any(record["work_key"] == work_key for record in state["threads"].values()):
            raise CoordinationError("each thread requires an independent worker directory")
        state["threads"][thread] = {
            "role": role,
            "work": work_path,
            "work_key": work_key,
            "registered_at": utc_now(),
            "mode": "idle",
            "active_batch": None,
        }
        return {
            "ok": True, "command": "register", "thread": thread,
            "role": "peer", "registered": True,
        }, True

    return _update(state_path, operation, lock_timeout)


def replace_idle_thread(
    state_path: str | Path,
    thread: str,
    owner: str,
    work: str,
    reason: str,
    evidence: dict[str, Any],
    logs_stopped: bool,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Replace an explicitly handed-off idle slot, preserving every old batch.

    Like ready/ack, external stop evidence is a trusted operator assertion.
    This does not terminate a process or infer that an idle peer is abandoned.
    """
    thread = _validate_name(thread, "thread")
    owner = _validate_name(owner, "owner")
    if thread == owner:
        raise CoordinationError("replacement must have a different real thread identity")
    if logs_stopped is not True:
        raise CoordinationError("replace-idle requires explicit logs_stopped confirmation")
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("replace-idle requires a nonempty explicit reason")
    if not isinstance(evidence, dict) or any(
        evidence.get(key) != value for key, value in {
            "previous_thread": owner, "replacement_thread": thread,
        }.items()
    ) or evidence.get("user_authorized") is not True or evidence.get("owner_stopped") is not True:
        raise CoordinationError("replacement requires matching user authorization and stopped-owner evidence")
    if any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
           for key in ("method", "summary")):
        raise CoordinationError("replacement evidence requires a method and summary")
    if not isinstance(evidence.get("journals"), list):
        raise CoordinationError("replacement evidence requires a complete journal snapshot")
    expected_work, expected_key = _work_path(work)
    checked = json.loads(json.dumps(evidence, ensure_ascii=False))

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        old = _require_thread(state, owner)
        if thread in state["threads"] or thread in state.get("retired_threads", {}):
            raise CoordinationError("replacement identity is already registered or retired")
        if old["mode"] != "idle" or old["active_batch"] is not None or any(
            batch["thread"] == owner and batch["state"] in {"claimed", "ready"}
            for batch in state["batches"].values()
        ):
            raise CoordinationError("replacement requires an idle owner with no in-flight batch")
        if old["work_key"] != expected_key:
            raise CoordinationError("expected worker directory no longer matches")
        if not Path(expected_work).is_dir():
            raise CoordinationError("worker directory is missing; preserve and review its journals")
        snapshot = []
        for path in sorted(Path(expected_work).glob("*.ndjson")):
            if path.is_symlink():
                raise CoordinationError("journal links require separate review")
            data = path.read_bytes()
            if data and not data.endswith(b"\n"):
                raise CoordinationError("journal has an incomplete final line; preserve and review it")
            snapshot.append({"name": path.name, "size": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})
        if snapshot != checked["journals"]:
            raise CoordinationError("journal snapshot changed; recheck stopped-owner evidence")
        now = utc_now()
        state.setdefault("retired_threads", {})[owner] = {
            **old, "retired_at": now, "replaced_by": thread,
            "reason": reason.strip()[:2000], "evidence": checked,
        }
        del state["threads"][owner]
        state["threads"][thread] = {
            "role": "peer", "mode": "idle", "active_batch": None,
            "work": old["work"], "work_key": old["work_key"],
            "registered_at": now, "replaces": owner,
        }
        return {
            "ok": True, "command": "replace-idle", "thread": thread,
            "previous_thread": owner, "work": old["work"],
            "registered_workers": len(state["threads"]),
            "worker_limit": None, "history_preserved": True,
        }, True

    return _update(state_path, operation, lock_timeout)


def _handoff_journals(value: Any, label: str) -> list[dict[str, Any]]:
    """Validate a complete, deterministically ordered byte-snapshot contract."""
    if not isinstance(value, list):
        raise CoordinationError(f"{label} must be a complete journal snapshot")
    names = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"name", "size", "sha256"}:
            raise CoordinationError(f"{label} requires name/size/sha256 entries")
        name, size, digest = entry["name"], entry["size"], entry["sha256"]
        if (not isinstance(name, str) or not name.lower().endswith(".ndjson")
                or "/" in name or "\\" in name or ":" in name
                or any(ord(char) < 32 for char in name)):
            raise CoordinationError(f"{label} contains an invalid journal name")
        if type(size) is not int or size < 0:
            raise CoordinationError(f"{label} requires nonnegative integer sizes")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(char not in "0123456789abcdef" for char in digest)):
            raise CoordinationError(f"{label} requires lowercase SHA-256 digests")
        names.append(name)
    if names != sorted(names) or len(names) != len(set(names)):
        raise CoordinationError(f"{label} must be sorted by name without duplicates")
    return value


def _snapshot_handoff_journals(work: str) -> list[dict[str, Any]]:
    """Read only top-level journals; stop declarations are not process controls."""
    try:
        directory = Path(work)
        if not directory.is_dir():
            raise CoordinationError("worker directory is missing; preserve and review its journals")
        snapshot = []
        for path in sorted(directory.iterdir(), key=lambda entry: entry.name):
            if not path.name.lower().endswith(".ndjson"):
                continue
            if path.is_symlink() or not path.is_file():
                raise CoordinationError("journal links or non-files require separate review")
            before = path.stat()
            data = path.read_bytes()
            after = path.stat()
            identity = lambda info: (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
            if identity(before) != identity(after) or len(data) != after.st_size:
                raise CoordinationError("journal changed while reading; recheck stopped-writer evidence")
            if data and not data.endswith(b"\n"):
                raise CoordinationError("journal has an incomplete final line; preserve and review it")
            snapshot.append({"name": path.name, "size": len(data),
                             "sha256": hashlib.sha256(data).hexdigest()})
        return snapshot
    except OSError as error:
        raise CoordinationError(f"cannot verify stopped journal snapshot: {error}") from error


def retire_idle_thread(
    state_path: str | Path,
    owner: str,
    work: str,
    reason: str,
    evidence: dict[str, Any],
    logs_stopped: bool,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Retire an explicitly stopped idle peer without changing its history or files."""
    owner = _validate_name(owner, "owner")
    if logs_stopped is not True or not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("retire-idle requires stopped logs and a reason")
    if (not isinstance(evidence, dict) or evidence.get("previous_thread") != owner
            or evidence.get("user_authorized") is not True
            or evidence.get("owner_stopped") is not True
            or any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
                   for key in ("method", "summary"))):
        raise CoordinationError("retire-idle requires matching authorization and stopped-owner evidence")
    _handoff_journals(evidence.get("journals"), "journals")
    checked = json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    expected_work, expected_key = _work_path(work)

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        old = _require_thread(state, owner)
        if (old.get("mode") != "idle" or old.get("active_batch") is not None
                or any(batch["thread"] == owner and batch["state"] in {"claimed", "ready"}
                       for batch in state["batches"].values())):
            raise CoordinationError("retire-idle requires no in-flight batch")
        if (old.get("work_key") != expected_key
                or _work_path(old["work"])[1] != expected_key
                or _work_path(checked.get("previous_work"))[1] != expected_key):
            raise CoordinationError("retire-idle work directory does not match")
        for _ in range(2):
            if _snapshot_handoff_journals(expected_work) != checked["journals"]:
                raise CoordinationError("retire-idle journal snapshot changed")
        now = utc_now()
        state.setdefault("retired_threads", {})[owner] = {
            **old, "retired_at": now, "reason": reason.strip()[:2000], "evidence": checked,
        }
        del state["threads"][owner]
        return {"ok": True, "command": "retire-idle", "thread": owner,
                "registered_workers": len(state["threads"]), "history_preserved": True}, True

    return _update(state_path, operation, lock_timeout)


def retired_record_digest(record: dict[str, Any]) -> str:
    """Bind a recovery receipt to one exact retirement, not just an identity."""
    return hashlib.sha256(json.dumps(
        record, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")).hexdigest()


def reactivate_retired_idle_thread(
    state_path: str | Path,
    thread: str,
    work: str,
    reason: str,
    evidence: dict[str, Any],
    logs_stopped: bool,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Restore the SAME explicitly resumed identity after a direct idle retirement.

    Host identity/authorization and search checks are operator assertions, not
    process controls. Never reverses a takeover or creates a replacement identity.
    """
    thread = _validate_name(thread, "thread")
    if logs_stopped is not True or not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("reactivate-retired-idle requires stopped logs and a reason")
    if (not isinstance(evidence, dict) or evidence.get("thread") != thread
            or any(evidence.get(key) is not True for key in (
                "user_authorized", "same_identity_resumed", "owner_stopped", "search_claims_checked",
            ))
            or any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
                   for key in ("method", "summary", "retired_record_sha256"))):
        raise CoordinationError("reactivation requires authorization and matching resumed-identity evidence")
    _handoff_journals(evidence.get("journals"), "journals")
    checked = json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    expected_work, expected_key = _work_path(work)
    if _work_path(checked.get("work"))[1] != expected_key:
        raise CoordinationError("reactivation work evidence does not match")

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        if thread in state["threads"]:
            raise CoordinationError("reactivation identity is already registered; inspect current")
        old = state.get("retired_threads", {}).get(thread)
        if not isinstance(old, dict) or not old.get("retired_at"):
            raise CoordinationError("reactivation requires an existing retirement")
        if old.get("replaced_by"):
            raise CoordinationError("reactivation cannot reverse replacement lineage")
        if retired_record_digest(old) != checked["retired_record_sha256"]:
            raise CoordinationError("retirement changed; refresh recovery evidence")
        if (old.get("mode") != "idle" or old.get("active_batch") is not None
                or any(batch["thread"] == thread and batch["state"] in {"claimed", "ready"}
                       for batch in state["batches"].values())):
            raise CoordinationError("reactivation requires no in-flight batch")
        if (old.get("work_key") != expected_key
                or _work_path(old["work"])[1] != expected_key):
            raise CoordinationError("reactivation original work directory does not match")
        for peer in state["threads"].values():
            peer_key = _work_path(peer["work"])[1]
            if (peer.get("work_key") == expected_key or peer_key == expected_key
                    or peer_key.startswith(expected_key + "/")
                    or expected_key.startswith(peer_key + "/")):
                raise CoordinationError("reactivation work directory conflicts with a registered peer")
        for _ in range(2):
            if _snapshot_handoff_journals(expected_work) != checked["journals"]:
                raise CoordinationError("reactivation journal snapshot changed")
        now = utc_now()
        state.setdefault("reactivation_history", []).append({
            "thread": thread, "reactivated_at": now, "reason": reason.strip()[:2000],
            "retirement": old, "evidence": checked,
        })
        state["threads"][thread] = {
            "role": "peer", "mode": "idle", "active_batch": None,
            "work": old["work"], "work_key": old["work_key"],
            "registered_at": now, "reactivated_from": old["retired_at"],
        }
        del state["retired_threads"][thread]
        return {
            "ok": True, "command": "reactivate-retired-idle", "thread": thread,
            "work": old["work"], "registered_workers": len(state["threads"]),
            "history_preserved": True,
        }, True

    return _update(state_path, operation, lock_timeout)


def takeover_idle_thread(
    state_path: str | Path,
    thread: str,
    owner: str,
    work: str,
    receiver_work: str,
    reason: str,
    evidence: dict[str, Any],
    logs_stopped: bool,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """An already registered peer takes an explicitly handed-off idle directory.

    Both stopped-writer declarations are trusted operator assertions, not proof
    that another process was terminated. Every state and snapshot check runs in
    one shared state lock. No batch, queue item, or journal is moved or rewritten.
    """
    thread = _validate_name(thread, "thread")
    owner = _validate_name(owner, "owner")
    if thread == owner:
        raise CoordinationError("takeover requires different real thread identities")
    if logs_stopped is not True:
        raise CoordinationError("takeover-idle requires explicit logs_stopped confirmation")
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("takeover-idle requires a nonempty explicit reason")
    if not isinstance(evidence, dict) or any(
        evidence.get(key) != value for key, value in {
            "previous_thread": owner, "receiver_thread": thread,
        }.items()
    ) or any(evidence.get(key) is not True for key in (
        "user_authorized", "owner_stopped", "receiver_stopped",
    )):
        raise CoordinationError("takeover requires matching authorization and both stopped-writer declarations")
    if any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
           for key in ("method", "summary")):
        raise CoordinationError("takeover evidence requires a method and summary")
    _handoff_journals(evidence.get("journals"), "journals")
    _handoff_journals(evidence.get("receiver_journals"), "receiver_journals")
    try:
        checked = json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise CoordinationError("takeover evidence must be finite JSON data") from error

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        receiver = _require_thread(state, thread)
        old = _require_thread(state, owner)
        if any(identity in state.get("retired_threads", {}) for identity in (thread, owner)):
            raise CoordinationError("registered takeover identities must not also be retired")
        for identity, record in ((thread, receiver), (owner, old)):
            if record.get("mode") != "idle" or record.get("active_batch") is not None or any(
                batch["thread"] == identity and batch["state"] in {"claimed", "ready"}
                for batch in state["batches"].values()
            ):
                raise CoordinationError("takeover requires both peers idle with no in-flight batch")

        expected_work, expected_key = _work_path(work)
        expected_receiver_work, expected_receiver_key = _work_path(receiver_work)

        def verify_paths() -> None:
            for record, expected, evidence_key in (
                (old, expected_key, "previous_work"),
                (receiver, expected_receiver_key, "receiver_work"),
            ):
                if (record.get("work_key") != expected
                        or _work_path(record["work"])[1] != expected
                        or _work_path(checked.get(evidence_key))[1] != expected):
                    raise CoordinationError("expected original worker directory no longer matches")
            # Reject physical aliases and overlapping directories, including a
            # third peer whose recorded path has since become a junction alias.
            keys = {identity: _work_path(record["work"])[1]
                    for identity, record in state["threads"].items()}
            for identity in (thread, owner):
                key = keys[identity]
                for other, other_key in keys.items():
                    if other != identity and (key == other_key
                            or key.startswith(other_key + "/")
                            or other_key.startswith(key + "/")):
                        raise CoordinationError("takeover requires non-overlapping independent worker directories")

        verify_paths()
        # Two complete passes detect additions/removals as well as in-place or
        # append changes between directory reads. Uncooperative writers are not
        # frozen by the state lock; explicit stopping remains mandatory.
        for _ in range(2):
            if _snapshot_handoff_journals(expected_work) != checked["journals"]:
                raise CoordinationError("owner journal snapshot changed; recheck stopped-writer evidence")
            if _snapshot_handoff_journals(expected_receiver_work) != checked["receiver_journals"]:
                raise CoordinationError("receiver journal snapshot changed; recheck stopped-writer evidence")
            verify_paths()

        now = utc_now()
        previous_work = receiver["work"]
        previous_key = receiver["work_key"]
        history = receiver.get("takeovers", [])
        if not isinstance(history, list):
            raise CoordinationError("receiver takeover history is invalid; review it before handoff")
        handoff = {
            "taken_over_at": now, "previous_thread": owner,
            "previous_work": previous_work, "previous_work_key": previous_key,
            "work": old["work"], "work_key": old["work_key"],
            "reason": reason.strip()[:2000], "evidence": checked,
        }
        state.setdefault("retired_threads", {})[owner] = {
            **old, "retired_at": now, "replaced_by": thread,
            "reason": reason.strip()[:2000], "evidence": checked,
        }
        del state["threads"][owner]
        receiver["takeovers"] = [*history, handoff]
        receiver["work"] = old["work"]
        receiver["work_key"] = old["work_key"]
        return {
            "ok": True, "command": "takeover-idle", "thread": thread,
            "previous_thread": owner, "previous_work": previous_work,
            "work": receiver["work"], "registered_workers": len(state["threads"]),
            "worker_limit": None, "history_preserved": True,
            "owner_journals": len(checked["journals"]),
            "receiver_journals": len(checked["receiver_journals"]),
        }, True

    return _update(state_path, operation, lock_timeout)


def restore_retired_idle_thread(
    state_path: str | Path,
    thread: str,
    owner: str,
    work: str,
    reason: str,
    evidence: dict[str, Any],
    logs_stopped: bool,
    receiver_confirmed: bool,
    force_user_authorized: bool = False,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Return a previously retired real identity to its stopped original work.

    This is only the reverse of the immediately preceding takeover, with a fresh
    stopped-owner receipt. Keep both retirement and batch histories auditable.
    """
    thread = _validate_name(thread, "thread")
    owner = _validate_name(owner, "owner")
    if thread == owner or logs_stopped is not True or receiver_confirmed is not True:
        raise CoordinationError("restore-retired-idle requires distinct identities and both confirmations")
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("restore-retired-idle requires an explicit reason")
    if not isinstance(evidence, dict) or any(evidence.get(key) != value for key, value in {
        "from_thread": owner, "intended_receiver_thread": thread,
        "user_authorized": True,
        "owner_stopped": True, "journals_stopped": True,
        "database_writes_stopped": True, "browser_actions_stopped": True,
        "new_claims_stopped": True, "current_status": "idle",
    }.items()) or evidence.get("active_batch") is not None:
        raise CoordinationError("restore-retired-idle requires matching stopped-owner receipt")
    confirmed_stop = evidence.get("old_owner_confirmed") is True
    forced_archived_stop = (
        force_user_authorized is True
        and evidence.get("user_forced_stop") is True
        and evidence.get("session_archived") is True
        and evidence.get("archived_session_id") == owner
        and isinstance(evidence.get("archive_method"), str)
        and bool(evidence["archive_method"].strip())
    )
    if not (confirmed_stop or forced_archived_stop):
        raise CoordinationError(
            "restore-retired-idle requires owner confirmation or an explicitly authorized archived-session stop"
        )
    if any(not isinstance(evidence.get(key), str) or not evidence[key].strip()
           for key in ("handoff_id", "method", "summary", "last_batch", "last_batch_acknowledged_at")):
        raise CoordinationError("restore-retired-idle receipt lacks handoff and last-batch evidence")
    if evidence.get("last_batch_status") != "acknowledged":
        raise CoordinationError("restore-retired-idle requires acknowledged last batch")
    _handoff_journals(evidence.get("journals"), "journals")
    try:
        checked = json.loads(json.dumps(evidence, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise CoordinationError("restore-retired-idle receipt must be finite JSON") from error
    expected_work, expected_key = _work_path(work)

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        old = _require_thread(state, owner)
        retired = state.get("retired_threads", {}).get(thread)
        if thread in state["threads"] or not isinstance(retired, dict):
            raise CoordinationError("receiver must have exactly one retired identity")
        if owner in state.get("retired_threads", {}):
            raise CoordinationError("owner has conflicting retired history")
        if retired.get("replaced_by") != owner or retired.get("mode") != "idle" or retired.get("active_batch") is not None:
            raise CoordinationError("retired identity does not belong to this takeover lineage")
        if old.get("mode") != "idle" or old.get("active_batch") is not None or any(
            batch.get("thread") in (owner, thread) and batch.get("state") in {"claimed", "ready"}
            for batch in state["batches"].values()
        ):
            raise CoordinationError("restore-retired-idle requires both identities without in-flight batches")
        if any(record.get("work_key") != expected_key or _work_path(record["work"])[1] != expected_key
               for record in (old, retired)) or _work_path(checked.get("work"))[1] != expected_key:
            raise CoordinationError("restore-retired-idle work directory changed")
        if any(
            identity != owner and (expected_key == key or expected_key.startswith(key + "/") or key.startswith(expected_key + "/"))
            for identity, record in state["threads"].items()
            for key in [_work_path(record["work"])[1]]
        ):
            raise CoordinationError("restore-retired-idle work conflicts with another peer")
        last = state["batches"].get(checked["last_batch"])
        if (not isinstance(last, dict) or last.get("thread") != owner
                or last.get("state") != "acknowledged"
                or last.get("acknowledged_at") != checked["last_batch_acknowledged_at"]):
            raise CoordinationError("last acknowledged batch changed")
        for _ in range(2):
            if _snapshot_handoff_journals(expected_work) != checked["journals"]:
                raise CoordinationError("stopped owner journal snapshot changed")
        now = utc_now()
        state["retired_threads"][owner] = {
            **old, "retired_at": now, "replaced_by": thread,
            "reason": reason.strip()[:2000], "evidence": checked,
            "receiver_confirmed": True,
        }
        del state["threads"][owner]
        del state["retired_threads"][thread]
        state["threads"][thread] = {
            "role": "peer", "mode": "idle", "active_batch": None,
            "work": old["work"], "work_key": old["work_key"],
            "registered_at": now, "replaces": owner,
            "prior_retirement": retired,
            "handoff": {"at": now, "reason": reason.strip()[:2000], "evidence": checked},
        }
        return {
            "ok": True, "command": "restore-retired-idle", "thread": thread,
            "previous_thread": owner, "work": old["work"],
            "registered_workers": len(state["threads"]), "history_preserved": True,
        }, True

    return _update(state_path, operation, lock_timeout)


def _clean_item(value: Any) -> tuple[str, dict[str, str]]:
    if not isinstance(value, dict):
        raise CoordinationError("each enqueued item must be an object")
    url = _page_url(value.get("url"))
    item = {"url": url}
    for field in ("title", "status", "source", "category", "query"):
        field_value = value.get(field, "")
        if not isinstance(field_value, str):
            raise CoordinationError(f"item {field} must be a string")
        item[field] = field_value
    return url, item


def enqueue_items(
    state_path: str | Path,
    thread: str,
    items: list[dict[str, Any]],
    *,
    refresh_evidence: str | Path | None = None,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    if not isinstance(items, list):
        raise CoordinationError("items JSON must be an array or an object containing items")
    cleaned: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()
    duplicate_input = 0
    for value in items:
        key, item = _clean_item(value)
        if key in seen:
            duplicate_input += 1
            continue
        seen.add(key)
        cleaned.append((key, item))

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        peer = _require_peer(state, thread)
        refresh = None
        if refresh_evidence is not None:
            proof_path = Path(refresh_evidence).resolve()
            if (state.get('execution_mode') != 'single' or proof_path.is_symlink()
                    or not proof_path.is_relative_to(Path(peer['work']).resolve())):
                raise CoordinationError('Refresh requires the single agent public DOM receipt')
            proof_data = proof_path.read_bytes()
            refresh = json.loads(proof_data)
            snapshot = refresh.get('snapshot', {})
            if (refresh.get('status') != 'observed' or snapshot.get('view') != 'latest'
                    or snapshot.get('loading') or not snapshot.get('panelVisible')
                    or snapshot.get('readyState') != 'complete'
                    or [_clean_item(v) for v in snapshot.get('items', [])] != cleaned):
                raise CoordinationError('Refresh items do not match the settled latest DOM')
        added = 0
        refreshed = 0
        existing = duplicate_input
        for key, item in cleaned:
            if key in state["items"]:
                record = state['items'][key]
                old = record['item']
                if (refresh is not None and record['state']=='acknowledged'
                        and not record.get('user_deleted') and item['title']==old['title']
                        and item.get('status') and item['status']!=old.get('status')
                        and key not in state['queue']):
                    record.setdefault('update_history', []).append({
                        'at':utc_now(),'previous_item':old,'previous_batch':record.get('batch'),
                        'previous_outcome':record.get('outcome'),'evidence':str(proof_path),
                        'evidence_sha256':hashlib.sha256(proof_data).hexdigest()})
                    record.update(item=item,state='queued',batch=None,outcome=None)
                    state['queue'].append(key)
                    added += 1
                    refreshed += 1
                    continue
                existing += 1
                continue
            state["items"][key] = {
                "item": item,
                "state": "queued",
                "batch": None,
                "outcome": None,
                "enqueued_at": utc_now(),
            }
            state["queue"].append(key)
            added += 1
        return {
            "ok": True, "command": "enqueue", "added": added,
            "duplicates": existing, "refreshed": refreshed, "queued": len(state["queue"]),
        }, added > 0

    return _update(state_path, operation, lock_timeout)


def assign_items(
    state_path: str | Path,
    thread: str,
    owner: str,
    items: list[dict[str, Any]],
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Atomically adopt one explicit, previously untracked manual batch."""
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CLAIM_SIZE:
        raise CoordinationError("assigned items must contain between 1 and 8 entries")
    cleaned: list[tuple[str, dict[str, str]]] = []
    seen: set[str] = set()
    for value in items:
        key, item = _clean_item(value)
        if key in seen:
            raise CoordinationError(f"duplicate detail URL in assigned batch: {key}")
        seen.add(key)
        cleaned.append((key, item))

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        _require_peer(state, thread)
        owner_record = _require_thread(state, owner)
        if owner_record["active_batch"] is not None or owner_record["mode"] != "idle":
            raise CoordinationError("assigned owner already has an active batch")
        overlaps = [key for key, _item in cleaned if key in state["items"]]
        if overlaps:
            raise CoordinationError(
                f"assigned URLs already exist in coordination state; no items were assigned (overlap={len(overlaps)})"
            )
        batch_id = f"batch-{state['next_batch']:06d}"
        state["next_batch"] += 1
        now = utc_now()
        keys = []
        for key, item in cleaned:
            keys.append(key)
            state["items"][key] = {
                "item": item, "state": "claimed", "batch": batch_id,
                "outcome": None, "enqueued_at": now,
            }
        state["batches"][batch_id] = {
            "thread": owner,
            "work": owner_record["work"],
            "state": "claimed",
            "item_urls": keys,
            "claimed_at": now,
            "ready_at": None,
            "acknowledged_at": None,
            "released_at": None,
            "logs_stopped": False,
            "results": None,
            "verification": None,
            "release_reason": None,
            "assignment": "peer_manual_adoption",
        }
        owner_record.update({"mode": "claimed", "active_batch": batch_id})
        return {
            "ok": True, "command": "assign", "owner": owner,
            "batch": batch_id, "work": owner_record["work"],
            "items": [item for _key, item in cleaned],
        }, True

    return _update(state_path, operation, lock_timeout)


def claim_batch(
    state_path: str | Path,
    thread: str,
    limit: int,
    *,
    eligible_urls: Collection[str] | None = None,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    if not isinstance(limit, int) or not 1 <= limit <= MAX_CLAIM_SIZE:
        raise CoordinationError("claim limit must be between 1 and 8")
    allowed_urls: set[str] | None = None
    if eligible_urls is not None:
        if not isinstance(eligible_urls, (list, tuple, set, frozenset)):
            raise CoordinationError("eligible URLs must be a collection of detail-page URLs")
        allowed_urls = set()
        for value in eligible_urls:
            canonical = _page_url(value)
            if canonical != value:
                raise CoordinationError("eligible URLs must be canonical detail-page URLs")
            allowed_urls.add(canonical)

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        require_collection_running(state)
        record = _require_peer(state, thread)
        if record["active_batch"] is not None or record["mode"] != "idle":
            raise CoordinationError("thread must be acknowledged or explicitly released before another claim")
        selected = (state["queue"][:limit] if allowed_urls is None else
                    [key for key in state["queue"] if key in allowed_urls][:limit])
        if not selected:
            return {
                "ok": True, "command": "claim", "thread": thread,
                "batch": None, "work": record["work"], "items": [],
            }, False
        if allowed_urls is None:
            del state["queue"][:len(selected)]
        else:
            selected_urls = set(selected)
            state["queue"][:] = [key for key in state["queue"] if key not in selected_urls]
        batch_id = f"batch-{state['next_batch']:06d}"
        state["next_batch"] += 1
        now = utc_now()
        for key in selected:
            state["items"][key].update({"state": "claimed", "batch": batch_id})
        state["batches"][batch_id] = {
            "thread": thread,
            "work": record["work"],
            "state": "claimed",
            "item_urls": selected,
            "claimed_at": now,
            "ready_at": None,
            "acknowledged_at": None,
            "released_at": None,
            "logs_stopped": False,
            "results": None,
            "verification": None,
            "release_reason": None,
        }
        record.update({"mode": "claimed", "active_batch": batch_id})
        return {
            "ok": True, "command": "claim", "thread": thread,
            "batch": batch_id, "work": record["work"],
            "items": [state["items"][key]["item"] for key in selected],
        }, True

    return _update(state_path, operation, lock_timeout)


def _clean_results(results: Any) -> dict[str, dict[str, str]]:
    if not isinstance(results, list):
        raise CoordinationError("results JSON must be an array or an object containing results")
    cleaned: dict[str, dict[str, str]] = {}
    for value in results:
        if not isinstance(value, dict):
            raise CoordinationError("each ready result must be an object")
        url = _page_url(value.get("url"))
        if url in cleaned:
            raise CoordinationError(f"duplicate result URL: {url}")
        outcome = value.get("outcome")
        if outcome not in OUTCOMES:
            raise CoordinationError("each result outcome must be captured, raw, failed, or skipped")
        note = value.get("note", "")
        if not isinstance(note, str):
            raise CoordinationError("result note must be a string")
        cleaned[url] = {"url": url, "outcome": outcome, "note": note[:2000]}
    return cleaned


def ready_batch(
    state_path: str | Path,
    thread: str,
    batch_id: str,
    results: list[dict[str, Any]],
    logs_stopped: bool,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    if logs_stopped is not True:
        raise CoordinationError("ready requires explicit confirmation that journal writing has stopped")
    cleaned = _clean_results(results)

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        record = _require_peer(state, thread)
        batch = state["batches"].get(batch_id)
        if not batch or batch["thread"] != thread or record["active_batch"] != batch_id:
            raise CoordinationError("batch is not the active batch owned by this thread")
        if batch["state"] != "claimed":
            raise CoordinationError("only a claimed batch can become ready")
        expected = set(batch["item_urls"])
        actual = set(cleaned)
        if actual != expected:
            raise CoordinationError(
                f"ready results must cover every claimed URL exactly once (missing={len(expected-actual)}, extra={len(actual-expected)})"
            )
        ordered = [cleaned[key] for key in batch["item_urls"]]
        now = utc_now()
        batch.update({
            "state": "ready", "ready_at": now, "logs_stopped": True,
            "results": ordered,
        })
        record["mode"] = "awaiting_ack"
        for result in ordered:
            state["items"][result["url"]].update({
                "state": "ready", "outcome": result["outcome"],
            })
        return {
            "ok": True, "command": "ready", "thread": thread,
            "batch": batch_id, "logs_stopped": True,
            "outcomes": dict(sorted(Counter(r["outcome"] for r in ordered).items())),
        }, True

    return _update(state_path, operation, lock_timeout)


def _clean_verification(
    value: Any,
    batch_id: str,
    expected_work: str,
    expected_items: int,
    expected_captured_urls: list[str],
    coordination: dict[str, Any] | None = None,
) -> dict[str, Any]:
    expected_captured = len(expected_captured_urls)
    if not isinstance(value, dict):
        raise CoordinationError("verification must be a JSON object from the external verifier")
    if value.get("batch_id") != batch_id or value.get("verified") is not True:
        raise CoordinationError("verification must match batch_id and contain verified=true")
    verifier = value.get("verifier")
    if not isinstance(verifier, str) or not verifier.strip():
        raise CoordinationError("verification requires a nonempty verifier")
    verification_work = value.get("work")
    try:
        _work, verification_key = _work_path(verification_work)
        _expected, expected_key = _work_path(expected_work)
    except CoordinationError as error:
        raise CoordinationError("verification requires the batch worker directory") from error
    if verification_key != expected_key:
        raise CoordinationError("verification worker directory does not match the target batch")
    if value.get("sync_ok") is not True or value.get("audit_ok") is not True:
        raise CoordinationError("verification requires explicit sync_ok=true and audit_ok=true")
    evidence = value.get("evidence")
    if not isinstance(evidence, dict):
        raise CoordinationError("verification requires structured external evidence")
    method = evidence.get("method")
    summary = evidence.get("summary")
    if not isinstance(method, str) or not method.strip() or not isinstance(summary, str) or not summary.strip():
        raise CoordinationError("verification evidence requires nonempty method and summary")
    if type(evidence.get("checked_items")) is not int or evidence["checked_items"] != expected_items:
        raise CoordinationError("verification evidence checked_items must match the target batch")
    if expected_captured:
        pending = value.get("cover_pending_items", [])
        if (not isinstance(pending, list) or any(url not in expected_captured_urls for url in pending)
                or len(set(pending)) != len(pending)):
            raise CoordinationError("invalid cover pending item identities")
        if pending and (value.get("cover_ok") is not False or value.get("cover_settled") is not True):
            raise CoordinationError("pending covers require an explicit incomplete settlement")
        if not pending and value.get("cover_ok") is not True:
            raise CoordinationError("captured results require cover_ok=true")
        if (type(value.get("cover_checked_items")) is not int
                or value["cover_checked_items"] != expected_captured - len(pending)):
            raise CoordinationError(
                "cover_checked_items must match the captured result count"
            )
        receipt = value.get("cover_receipt")
        digest = value.get("cover_receipt_sha256")
        if not isinstance(receipt, str) or not receipt.strip():
            raise CoordinationError("captured results require a nonempty cover receipt path")
        if (not isinstance(digest, str) or len(digest) != 64
                or any(character not in "0123456789abcdefABCDEF" for character in digest)):
            raise CoordinationError("captured results require a valid cover receipt sha256")
        expected_receipt = Path(_expected) / f"{batch_id}-covers.json"
        supplied_receipt = Path(receipt)
        supplied_key = os.path.normcase(os.path.abspath(os.path.normpath(supplied_receipt)))
        expected_receipt_key = os.path.normcase(str(expected_receipt))
        if supplied_key != expected_receipt_key:
            raise CoordinationError(
                "cover_receipt must be the target batch receipt in its worker directory"
            )
        if supplied_receipt.is_symlink() or not supplied_receipt.is_file():
            raise CoordinationError("cover_receipt must be an existing regular file")
        try:
            receipt_bytes = supplied_receipt.read_bytes()
        except OSError as error:
            raise CoordinationError("cover_receipt could not be read") from error
        actual_digest = hashlib.sha256(receipt_bytes).hexdigest()
        if actual_digest != digest.lower():
            raise CoordinationError("cover_receipt_sha256 does not match the receipt bytes")
        try:
            receipt_document = json.loads(receipt_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise CoordinationError("cover_receipt must contain valid JSON") from error
        if (not isinstance(receipt_document, dict)
                or type(receipt_document.get("version")) is not int
                or receipt_document["version"] != 1
                or receipt_document.get("batch_id") != batch_id
                or receipt_document.get("complete") is not (not bool(pending))
                or (pending and (receipt_document.get("settled") is not True
                                 or receipt_document.get("cover_pending_items") != pending))
                or type(receipt_document.get("cover_checked_items")) is not int
                or receipt_document["cover_checked_items"] != expected_captured - len(pending)
                or not isinstance(receipt_document.get("items"), list)
                or len(receipt_document["items"]) != expected_captured):
            raise CoordinationError("cover_receipt summary does not match the captured batch")
        expected_urls = set(expected_captured_urls)
        receipt_urls: set[str] = set()
        program_ids: set[int] = set()
        for item in receipt_document["items"]:
            if not isinstance(item, dict):
                raise CoordinationError("cover_receipt items must be structured objects")
            source_url = item.get("source_url")
            program_id = item.get("program_id")
            artifact = item.get("artifact")
            artifact_digest = artifact.get("sha256") if isinstance(artifact, dict) else None
            if (not isinstance(source_url, str) or source_url not in expected_urls
                    or source_url in receipt_urls):
                raise CoordinationError(
                    "cover_receipt URLs must exactly match captured results without duplicates"
                )
            if (type(program_id) is not int or program_id <= 0
                    or program_id in program_ids):
                raise CoordinationError(
                    "cover_receipt program_id values must be unique positive integers"
                )
            if source_url in pending:
                if item.get("status") != "unavailable" or coordination is None:
                    raise CoordinationError("pending cover requires a central unavailable result")
                web = str(Path(__file__).resolve().parents[2] / "web")
                if web not in sys.path:
                    sys.path.insert(0, web)
                from collection_cover_policy import verify_pending
                verify_pending(item, coordination)
                receipt_urls.add(source_url)
                program_ids.add(program_id)
                continue
            if (item.get("status") != "selected"
                    or item.get("selection") not in {"existing", "generated"}):
                raise CoordinationError("cover_receipt requires selected cover items")
            if (not isinstance(artifact_digest, str) or len(artifact_digest) != 64
                    or any(character not in "0123456789abcdefABCDEF"
                           for character in artifact_digest)):
                raise CoordinationError(
                    "cover_receipt selected items require an artifact sha256"
                )
            receipt_urls.add(source_url)
            program_ids.add(program_id)
        if receipt_urls != expected_urls:
            raise CoordinationError("cover_receipt URLs do not cover every captured result")
    # Keep the verifier's compact evidence, but never reinterpret it as a DB operation here.
    return json.loads(json.dumps(value, ensure_ascii=False))


def acknowledge_batch(
    state_path: str | Path,
    thread: str,
    batch_id: str,
    verification: dict[str, Any],
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        _require_peer(state, thread)
        batch = state["batches"].get(batch_id)
        if not batch or batch["state"] != "ready" or batch["logs_stopped"] is not True:
            raise CoordinationError("only a ready, stopped batch can be acknowledged")
        checked = _clean_verification(
            verification, batch_id, batch["work"], len(batch["item_urls"]),
            [result["url"] for result in batch["results"]
             if result["outcome"] == "captured"],
            coordination=state,
        )
        owner = state["threads"][batch["thread"]]
        if owner["active_batch"] != batch_id:
            raise CoordinationError("batch owner state is inconsistent")
        manual = 0
        acknowledged = 0
        pending = set(checked.get("cover_pending_items", []))
        if pending:
            batch.setdefault("resource_results", json.loads(json.dumps(batch["results"])))
        for result in batch["results"]:
            item_record = state["items"][result["url"]]
            if result["url"] in pending:
                result["outcome"] = "cover_pending"
                result["note"] = "Resources audited; central DIRECT cover attempts exhausted; cover remains unavailable"
                item_record["cover_status"] = "pending"
            if result["outcome"] == "captured":
                item_record["state"] = "acknowledged"
                acknowledged += 1
            else:
                item_record["state"] = "manual_review"
                manual += 1
        now = utc_now()
        batch.update({
            "state": "acknowledged", "acknowledged_at": now,
            "verification": checked, "acknowledged_by": thread,
            "complete_captured": not pending,
        })
        owner.update({"mode": "idle", "active_batch": None})
        return {
            "ok": True, "command": "ack", "batch": batch_id,
            "state": "acknowledged", "captured_acknowledged": acknowledged,
            "manual_review": manual,
        }, True

    return _update(state_path, operation, lock_timeout)


def release_batch(
    state_path: str | Path,
    thread: str,
    batch_id: str,
    reason: str,
    logs_stopped: bool = False,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("release requires a nonempty explicit reason")
    reason = reason.strip()[:2000]

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        _require_peer(state, thread)
        batch = state["batches"].get(batch_id)
        if not batch or batch["state"] not in {"claimed", "ready"}:
            raise CoordinationError("only an active claimed/ready batch can be released")
        if batch["state"] == "claimed" and logs_stopped is not True:
            raise CoordinationError("releasing a claimed batch requires explicit logs_stopped confirmation")
        owner = state["threads"][batch["thread"]]
        if owner["active_batch"] != batch_id:
            raise CoordinationError("batch owner state is inconsistent")
        for key in batch["item_urls"]:
            state["items"][key]["state"] = "manual_review"
        batch.update({
            "state": "released", "released_at": utc_now(),
            "release_reason": reason, "logs_stopped": True,
        })
        owner.update({"mode": "idle", "active_batch": None})
        return {
            "ok": True, "command": "release", "batch": batch_id,
            "state": "released", "manual_review": len(batch["item_urls"]),
            "requeued": 0,
        }, True

    return _update(state_path, operation, lock_timeout)


def rebind_work(
    state_path: str | Path,
    thread: str,
    owner: str,
    work: str,
    reason: str,
    logs_stopped: bool,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    if logs_stopped is not True:
        raise CoordinationError("rebind-work requires explicit logs_stopped confirmation")
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("rebind-work requires a nonempty explicit reason")
    reason = reason.strip()[:2000]
    new_work, new_key = _work_path(work)

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        _require_peer(state, thread)
        owner_record = _require_thread(state, owner)
        if owner_record["active_batch"] is not None or owner_record["mode"] != "idle":
            raise CoordinationError("work can only be rebound while the owner is idle with no active batch")
        if owner_record["work_key"] == new_key:
            raise CoordinationError("new worker directory resolves to the current worker directory")
        if any(
            name != owner and record["work_key"] == new_key
            for name, record in state["threads"].items()
        ):
            raise CoordinationError("new worker directory conflicts with another registered owner")
        old_work = owner_record["work"]
        changed_at = utc_now()
        owner_record.setdefault("work_rebindings", []).append({
            "changed_at": changed_at,
            "old_work": old_work,
            "new_work": new_work,
            "reason": reason,
        })
        owner_record.update({"work": new_work, "work_key": new_key})
        return {
            "ok": True, "command": "rebind-work", "owner": owner,
            "old_work": old_work, "new_work": new_work,
            "changed_at": changed_at,
        }, True

    return _update(state_path, operation, lock_timeout)


def retry_items(
    state_path: str | Path,
    thread: str,
    items: list[dict[str, Any]],
    reason: str,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    if not isinstance(reason, str) or not reason.strip():
        raise CoordinationError("retry requires a nonempty explicit reason")
    reason = reason.strip()[:2000]
    if not isinstance(items, list) or not 1 <= len(items) <= MAX_CLAIM_SIZE:
        raise CoordinationError("retry items must contain between 1 and 8 entries")
    keys: list[str] = []
    requests: dict[str, dict[str, Any]] = {}
    seen: set[str] = set()
    for value in items:
        if not isinstance(value, dict):
            raise CoordinationError("each retry item must be an object containing url")
        key = _page_url(value.get("url"))
        if key in seen:
            raise CoordinationError(f"duplicate detail URL in retry request: {key}")
        seen.add(key)
        keys.append(key)
        requests[key] = value

    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        peer = _require_peer(state, thread)
        invalid = [
            key for key in keys
            if key not in state["items"]
            or state["items"][key]["state"] != "manual_review"
            or state["items"][key].get("user_deleted") is not None
            or key in state["queue"]
        ]
        if invalid:
            raise CoordinationError(
                "every retry URL must already be manual_review and absent from the queue; "
                f"no items were changed (invalid={len(invalid)})"
            )
        corrections = {}
        for key in keys:
            path_value = requests[key].get('title_correction_evidence')
            if path_value is None:
                continue
            if not isinstance(path_value, str) or not path_value.strip():
                raise CoordinationError('title correction evidence requires a file path')
            path = Path(path_value).resolve()
            if not path.is_relative_to(Path(peer['work']).resolve()):
                raise CoordinationError('title correction evidence must belong to this peer work directory')
            try:
                data = path.read_bytes()
                proof = json.loads(data)
            except (OSError, ValueError) as error:
                raise CoordinationError('cannot read title correction DOM evidence') from error
            if not isinstance(proof, dict):
                raise CoordinationError('title correction evidence must be an object')
            heading = proof.get('heading')
            count = proof.get('listDataWeb')
            old = state['items'][key]['item']
            if (proof.get('page_url') != key or not isinstance(heading, str)
                    or not heading.strip() or heading != heading.strip()
                    or type(count) is not int or count <= 0
                    or not isinstance(proof.get('observed_at'), str)
                    or not proof['observed_at']
                    or not old.get('title', '').startswith(heading)):
                raise CoordinationError('title correction requires same-page heading and nonempty public DOM evidence')
            suffix = old['title'][len(heading):].strip()
            if (re.fullmatch(r'(?:更新至|全)[0-9]+集|已完结|完结', suffix) is None
                    or old.get('status', '') not in ('', suffix)):
                raise CoordinationError('title correction must remove only an observed appended update status')
            corrections[key] = {'title': heading, 'status': suffix,
                                'evidence': str(path), 'evidence_sha256': hashlib.sha256(data).hexdigest(),
                                'previous_item': dict(old)}
        retried_at = utc_now()
        for key in keys:
            record = state["items"][key]
            retry_count = int(record.get("retry_count", 0)) + 1
            history = {
                "retry_number": retry_count,
                "retried_at": retried_at,
                "reason": reason,
                "previous_batch": record.get("batch"),
                "previous_outcome": record.get("outcome"),
            }
            if key in corrections:
                correction = corrections[key]
                history['title_correction'] = correction
                record['item'] = {**record['item'], 'title': correction['title'],
                                  'status': correction['status']}
            record.setdefault("retry_history", []).append(history)
            record.update({
                "state": "queued", "batch": None, "outcome": None,
                "retry_count": retry_count,
            })
            state["queue"].append(key)
        return {
            "ok": True, "command": "retry", "requeued": len(keys),
            "retried_at": retried_at, "automatic_requeue": False,
        }, True

    return _update(state_path, operation, lock_timeout)


def status_state(
    state_path: str | Path,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        item_counts = Counter(record["state"] for record in state["items"].values())
        batch_counts = Counter(record["state"] for record in state["batches"].values())
        threads = [
            {
                "thread": name, "role": "peer", "mode": record["mode"],
                "active_batch": record["active_batch"], "work": record["work"],
                "active_items": len(state["batches"][record["active_batch"]]["item_urls"])
                if record["active_batch"] else 0,
            }
            for name, record in sorted(state["threads"].items())
        ]
        return {
            "ok": True, "command": "status", "coordination_mode": "peer",
            "reporting_mode": "per_thread",
            "worker_limit": None, "registered_workers": len(threads),
            "threads": threads, "items": dict(sorted(item_counts.items())),
            "batches": dict(sorted(batch_counts.items())),
        }, False

    return _update(state_path, operation, lock_timeout)


def current_batch(
    state_path: str | Path,
    thread: str,
    *,
    lock_timeout: float = DEFAULT_LOCK_TIMEOUT,
) -> dict[str, Any]:
    """Return at most one thread's own resumable batch, never the full queue."""
    def operation(state: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        record = _require_thread(state, thread)
        batch_id = record["active_batch"]
        if batch_id is None:
            return {
                "ok": True, "command": "current", "thread": thread,
                "role": "peer", "status": "idle", "active_batch": None,
                "work": record["work"], "items": [],
            }, False
        batch = state["batches"].get(batch_id)
        if not batch or batch["thread"] != thread or len(batch["item_urls"]) > MAX_CLAIM_SIZE:
            raise CoordinationError("active batch state is inconsistent")
        return {
            "ok": True, "command": "current", "thread": thread,
            "role": "peer", "status": batch["state"],
            "active_batch": batch_id, "work": record["work"],
            "items": [state["items"][key]["item"] for key in batch["item_urls"]],
        }, False

    return _update(state_path, operation, lock_timeout)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    def state_command(name: str) -> argparse.ArgumentParser:
        command = subparsers.add_parser(name)
        command.add_argument("--state", required=True)
        return command

    command = state_command("init")
    identity = command.add_mutually_exclusive_group(required=True)
    identity.add_argument("--thread")
    identity.add_argument("--coordinator", help="legacy alias for --thread; grants no privileges")
    command.add_argument("--work", required=True)

    state_command("unlimit")

    command = state_command("register")
    command.add_argument("--thread", required=True)
    command.add_argument(
        "--role", default="peer", choices=sorted(ROLES),
        help="legacy metadata is accepted; all registered threads are equal peers",
    )
    command.add_argument("--work", required=True)

    command = state_command("enqueue")
    command.add_argument("--thread", required=True)
    command.add_argument("--items", required=True)

    command = state_command("replace-idle")
    command.add_argument("--thread", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("takeover-idle")
    command.add_argument("--thread", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--receiver-work", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("restore-retired-idle")
    command.add_argument("--thread", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--logs-stopped", action="store_true")
    command.add_argument("--receiver-confirmed", action="store_true")
    command.add_argument("--force-user-authorized", action="store_true")

    command = state_command("retire-idle")
    command.add_argument("--owner", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("reactivate-retired-idle")
    command.add_argument("--thread", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--evidence", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("assign")
    command.add_argument("--thread", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--items", required=True)

    command = state_command("claim")
    command.add_argument("--thread", required=True)
    command.add_argument("--limit", required=True, type=int)

    command = state_command("ready")
    command.add_argument("--thread", required=True)
    command.add_argument("--batch", required=True)
    command.add_argument("--results", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("ack")
    command.add_argument("--thread", required=True)
    command.add_argument("--batch", required=True)
    command.add_argument("--verification", required=True)

    command = state_command("release")
    command.add_argument("--thread", required=True)
    command.add_argument("--batch", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("rebind-work")
    command.add_argument("--thread", required=True)
    command.add_argument("--owner", required=True)
    command.add_argument("--work", required=True)
    command.add_argument("--reason", required=True)
    command.add_argument("--logs-stopped", action="store_true")

    command = state_command("retry")
    command.add_argument("--thread", required=True)
    command.add_argument("--items", required=True)
    command.add_argument("--reason", required=True)

    state_command("status")

    command = state_command("current")
    command.add_argument("--thread", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "init":
            result = init_state(args.state, args.thread or args.coordinator, args.work)
        elif args.command == "unlimit":
            result = unlimit_state(args.state)
        elif args.command == "register":
            result = register_thread(args.state, args.thread, args.role, args.work)
        elif args.command == "replace-idle":
            result = replace_idle_thread(
                args.state, args.thread, args.owner, args.work, args.reason,
                _load_json_file(args.evidence), args.logs_stopped,
            )
        elif args.command == "takeover-idle":
            result = takeover_idle_thread(
                args.state, args.thread, args.owner, args.work, args.receiver_work,
                args.reason, _load_json_file(args.evidence), args.logs_stopped,
            )
        elif args.command == "restore-retired-idle":
            result = restore_retired_idle_thread(
                args.state, args.thread, args.owner, args.work, args.reason,
                _load_json_file(args.evidence), args.logs_stopped, args.receiver_confirmed,
                args.force_user_authorized,
            )
        elif args.command == "retire-idle":
            result = retire_idle_thread(
                args.state, args.owner, args.work, args.reason,
                _load_json_file(args.evidence), args.logs_stopped,
            )
        elif args.command == "reactivate-retired-idle":
            result = reactivate_retired_idle_thread(
                args.state, args.thread, args.work, args.reason,
                _load_json_file(args.evidence), args.logs_stopped,
            )
        elif args.command == "enqueue":
            result = enqueue_items(args.state, args.thread, _load_json_file(args.items, "items"))
        elif args.command == "assign":
            result = assign_items(
                args.state, args.thread, args.owner, _load_json_file(args.items, "items"),
            )
        elif args.command == "claim":
            result = claim_batch(args.state, args.thread, args.limit)
        elif args.command == "ready":
            result = ready_batch(
                args.state, args.thread, args.batch,
                _load_json_file(args.results, "results"), args.logs_stopped,
            )
        elif args.command == "ack":
            result = acknowledge_batch(
                args.state, args.thread, args.batch, _load_json_file(args.verification),
            )
        elif args.command == "release":
            result = release_batch(
                args.state, args.thread, args.batch, args.reason, args.logs_stopped,
            )
        elif args.command == "rebind-work":
            result = rebind_work(
                args.state, args.thread, args.owner, args.work,
                args.reason, args.logs_stopped,
            )
        elif args.command == "retry":
            result = retry_items(
                args.state, args.thread, _load_json_file(args.items, "items"), args.reason,
            )
        elif args.command == "status":
            result = status_state(args.state)
        else:
            result = current_batch(args.state, args.thread)
    except CoordinationError as error:
        # CLI streams stay ASCII-safe even when PowerShell launches Python with
        # a legacy console encoding. State files still preserve original UTF-8.
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True, separators=(",", ":")), file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
