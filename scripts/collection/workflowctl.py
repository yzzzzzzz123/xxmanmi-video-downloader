#!/usr/bin/env python3
"""Deterministic glue for the collection steps around public-browser evidence.

The browser remains the only authority for site observations.  This tool owns
the repetitive local work: search task/claim creation, exact URL prechecks,
search journal sync/enqueue, and captured-batch ready/finish/ack finalization.
It never requests the source site or media URLs.
"""

from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import itertools
import json
import re
import os
from pathlib import Path
import secrets
import subprocess
import sys
import tempfile
import time
import unicodedata
from typing import Any
from urllib.parse import urlsplit


HERE = Path(__file__).resolve().parent
PROJECT_ROOT = HERE.parents[1]
DB_ROOT = PROJECT_ROOT / "Datebase" / "xxmanmi-resource-db"
for module_path in (str(DB_ROOT), str(HERE)):
    if module_path not in sys.path:
        sys.path.insert(0, module_path)

import batchctl  # noqa: E402
import coordctl  # noqa: E402
import dbctl  # noqa: E402
import shared_discovery  # noqa: E402


DEFAULT_STATE = DB_ROOT / ".runtime" / "collection-coordination" / "state.json"
DEFAULT_DISCOVERY = DB_ROOT / ".runtime" / "title-discovery"
DEFAULT_TASKBOOKS = DB_ROOT / ".runtime" / "collection-task-books"
DEFAULT_REQUESTS = DB_ROOT / ".runtime" / "user-priority"
COVERCTL = PROJECT_ROOT / "web" / "coverctl.py"
MAX_SEARCH_RESULTS = 200


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def compact_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", newline="\n", delete=False,
            dir=path.parent, prefix=path.name + ".", suffix=".tmp",
        ) as stream:
            temporary = stream.name
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        coordctl.replace_state_file(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


@contextmanager
def work_lock(work: Path, timeout: float = 10.0):
    """Serialize one owner's local workflow without breaking unknown locks."""
    work.mkdir(parents=True, exist_ok=True)
    path = work / ".workflowctl.lock"
    token = secrets.token_hex(24)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise RuntimeError(f"workflow lock is busy; it was not removed: {path}")
            time.sleep(0.025)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump({"token": token, "pid": os.getpid(), "created_at": utc_now()}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        yield
    finally:
        try:
            current = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError("owned workflow lock changed or disappeared") from exc
        if current.get("token") != token:
            raise RuntimeError("workflow lock ownership changed")
        path.unlink()


def read_state(state_path: Path) -> dict[str, Any]:
    return coordctl._read_state(state_path)  # shared validated state reader


def owner_record(state_path: Path, owner: str) -> tuple[dict[str, Any], Path]:
    state = read_state(state_path)
    coordctl.require_collection_running(state)
    if state.get('execution_mode')=='single' and owner!=state.get('collection_active_owner'):
        raise ValueError('Historical collection owner cannot run work in single agent mode')
    record = state.get("threads", {}).get(owner)
    if not record:
        raise ValueError(f"owner is not a registered peer: {owner}")
    work = Path(record["work"]).resolve()
    return record, work


def _task_id(owner: str) -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    suffix = secrets.token_hex(3)
    return f"search-{owner[:8]}-{stamp}-{suffix}"


def _taskbook(owner: str, task_id: str, work: Path, state_path: Path,
              query: str, channel: str, category: str,
              discovery_root: Path = DEFAULT_DISCOVERY) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "kind": "search",
        "owner": owner,
        "created_at": utc_now(),
        "objective": f"完成数据库正式待办‘{query} × {channel}’的公开 DOM 搜索、去重、同步与有证据入队。",
        "scope": {
            "queries": [query], "channels": [channel],
            "observed_urls": [], "category": category,
        },
        "limit": 1,
        "work": str(work),
        "inputs": [
            "dbctl.py searches 的当前正式待办",
            str(discovery_root / "claims" / f"{shared_discovery.claim_digest(query, channel)}.json"),
            str(state_path.resolve()),
        ],
        "acceptance": [
            "原子认领精确查询词与真实渠道",
            "浏览器只从当前公开 DOM 保存查询词、渠道和零结果或实际命中",
            "workflowctl commit-search 完成官方数据库去重、搜索同步与原子入队",
            "只有真实未访问详情进入 detail/finish/ack",
        ],
        "stop_conditions": [
            "组合已由其他 owner 认领", "浏览器权限、验证码、超时或用户冲突",
            "当前公开 DOM 无法可靠核实结果",
        ],
        "coordination_state": str(state_path.resolve()),
    }


def _taskbook_path(taskbooks: Path, task_id: str) -> Path:
    if (not isinstance(task_id, str) or not task_id.strip()
            or task_id in {".", ".."} or any(char in task_id for char in '/\\:')):
        raise ValueError("task_id must be a nonempty filename without path separators")
    return taskbooks / f"{task_id}.json"


def _channel_claims(root: Path, channel: str) -> tuple[dict[str, dict[str, Any]], set[str]]:
    """Inspect the existing claim authority once; do not create another queue."""
    claims = {}
    abandoned_queries: set[str] = set()
    with shared_discovery.claim_lock(root):
        _path, ledger = shared_discovery.load_handoff_ledger(root)
        _abandonment_path, abandonment_ledger = shared_discovery.load_abandonment_ledger(root)
        cleanup_ledger = shared_discovery.load_cleanup_ledger(root)
        for path in sorted((root / "claims").glob("*.json")):
            original = shared_discovery.read_claim(path, path.stem)
            if original["channel"] != channel:
                continue
            claim_hash = shared_discovery.file_sha256(path)
            effective = shared_discovery.effective_claim(
                original, path.stem, ledger, claim_hash,
            )
            if shared_discovery.cleanup_for(path.stem, effective, claim_hash, cleanup_ledger) is not None:
                abandoned_queries.add(effective["query"])
                continue
            if shared_discovery.abandonment_for(
                    path.stem, effective, claim_hash, abandonment_ledger) is not None:
                abandoned_queries.add(effective["query"])
                continue
            claims[effective["query"]] = effective
    return claims, abandoned_queries


def _commit_action(state_path: Path, owner: str, evidence: str,
                   discovery_root: Path, taskbooks: Path) -> dict[str, Any]:
    return {"kind": "commit_search", "evidence": evidence,
            "argv": [sys.executable, "-B", str(HERE / "workflowctl.py"),
                     "commit-search", "--state", str(state_path.resolve()),
                     "--thread", owner, "--evidence", evidence,
                     "--discovery-root", str(discovery_root.resolve()),
                     "--taskbooks", str(taskbooks.resolve())]}


def _prepared_search(state_path: Path, owner: str, work: Path, channel: str,
                      known: dict[str, dict[str, Any]], query: str | None,
                      discovery_root: Path, taskbooks: Path) -> dict[str, Any] | None:
    # A successful sync can remove this query from search_plan's pending view
    # before enqueue/receipt completion.  Recover from the existing claim and
    # journal receipt first, rather than depending on that pending SQL view.
    for candidate_query, claim in sorted(known.items()):
        if claim["owner"] != owner or query is not None and candidate_query != query:
            continue
        task_id = claim["task_id"]
        book_path = _taskbook_path(taskbooks, task_id)
        receipt_path = work / f"search-commit-{task_id}.json"
        if not receipt_path.exists():
            continue
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("phase") != "prepared":
            continue
        book = json.loads(book_path.read_text(encoding="utf-8"))
        if (receipt.get("owner") != owner or receipt.get("task_id") != task_id
                or not isinstance(receipt.get("evidence"), str) or not receipt["evidence"]
                or book.get("owner") != owner or Path(book.get("work", "")).resolve() != work
                or book.get("scope", {}).get("queries") != [candidate_query]
                or book.get("scope", {}).get("channels") != [channel]):
            raise ValueError("prepared search receipt conflicts with the effective claim/taskbook")
        return {
            "ok": True, "command": "claim-search", "status": "resumed_commit",
            "task_id": task_id, "query": candidate_query, "channel": channel,
            "category": book["scope"].get("category", ""), "work": str(work),
            "taskbook": str(book_path), "receipt": str(receipt_path),
            "next_action": _commit_action(state_path, owner, receipt["evidence"], discovery_root, taskbooks),
        }
    return None


def claim_search_task(state_path: Path, owner: str, channel: str, *,
                      query: str | None = None, task_id: str | None = None,
                      discovery_root: Path = DEFAULT_DISCOVERY,
                      taskbooks: Path = DEFAULT_TASKBOOKS) -> dict[str, Any]:
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch; finish it before claiming a search")
    channel = channel.strip()
    if not channel:
        raise ValueError("channel must be nonempty")
    dbctl.validate_ui_channel_sources([channel])
    if task_id is not None:
        _taskbook_path(taskbooks, task_id)
    if query is not None:
        query = query.strip()
        if not query:
            raise ValueError("query must be nonempty")
    known, abandoned_queries = _channel_claims(discovery_root, channel)
    prepared = _prepared_search(state_path, owner, work, channel, known, query, discovery_root, taskbooks)
    if prepared is not None:
        if task_id is not None and task_id != prepared["task_id"]:
            raise ValueError("prepared search must resume its existing task_id")
        return prepared
    excluded = sorted(
        {name for name, claim in known.items() if claim["owner"] != owner}
        | abandoned_queries
    )
    preferred = [name for name, claim in known.items() if claim["owner"] == owner]
    if query in abandoned_queries:
        terminal = shared_discovery.inspect_claim(discovery_root, query, channel)
        return {"ok": False, "command": "claim-search",
                "reason": terminal.get("reason", "abandoned_unknown_action"),
                "query": query, "channel": channel}
    if query in excluded:
        return {"ok": False, "command": "claim-search", "reason": "already_claimed",
                "claim": known[query]}
    candidates = dbctl.searches(
        limit=100, sources=[channel], query=query, exclude_queries=excluded, preferred_queries=preferred,
    )
    if query is not None and not candidates:
        raise ValueError("the exact query/channel is not in the current formal search queue")
    # Resume a pending owned claim before allocating another search task.  The
    # database excludes already committed search_runs before applying LIMIT.
    candidates.sort(key=lambda item: item["query"] not in known)
    for candidate in candidates:
        candidate_query = candidate["query"]
        existing_claim = known.get(candidate_query)
        current_task = existing_claim["task_id"] if existing_claim else task_id or _task_id(owner)
        if existing_claim and task_id is not None and task_id != current_task:
            raise ValueError("owned search must resume its existing task_id")
        path = _taskbook_path(taskbooks, current_task)
        claimed = shared_discovery.claim_search(
            discovery_root, candidate_query, channel, owner, current_task,
        )
        if claimed.get("reason") == "already_claimed":
            if query is not None:
                return {"ok": False, "reason": "already_claimed", "claim": claimed.get("claim")}
            continue
        if not claimed.get("ok"):
            raise RuntimeError(f"search claim failed: {claimed.get('reason')}")
        book = _taskbook(
            owner, current_task, work, state_path, candidate_query, channel,
            candidate.get("category") or "", discovery_root,
        )
        taskbooks.mkdir(parents=True, exist_ok=True)
        if path.exists():
            existing = json.loads(path.read_text(encoding="utf-8"))
            if existing.get("owner") != owner or existing.get("scope") != book["scope"]:
                raise RuntimeError("existing taskbook conflicts with the claimed search")
        else:
            shared_discovery.write_exclusive(path, book)
        next_action = {"kind": "browser_search", "taskbook": str(path)}
        receipt_path = work / f"search-commit-{current_task}.json"
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("owner") != owner or receipt.get("task_id") != current_task:
                raise ValueError("existing search receipt conflicts with its owner/task")
            if receipt.get("phase") == "prepared":
                next_action = _commit_action(state_path, owner, receipt["evidence"], discovery_root, taskbooks)
        return {
            "ok": True, "command": "claim-search", "task_id": current_task,
            "query": candidate_query, "channel": channel,
            "category": candidate.get("category") or "", "work": str(work),
            "taskbook": str(path), "claim_sha256": claimed.get("claim_sha256"),
            "status": "resumed" if existing_claim else "claimed",
            "next_action": next_action,
        }
    return {"ok": False, "command": "claim-search", "reason": "no_unclaimed_formal_search"}


def admit_discovery_search(state_path: Path, owner: str, deduped_path: Path,
                           query: str, channel: str, *, task_id: str | None = None,
                           discovery_root: Path = DEFAULT_DISCOVERY,
                           taskbooks: Path = DEFAULT_TASKBOOKS) -> dict[str, Any]:
    """Atomically reserve one verified discovery candidate and admit its formal search."""
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch; finish it before admitting a search")
    query = query.strip()
    channel = channel.strip()
    if not query or not channel:
        raise ValueError("query and channel must be nonempty")
    dbctl.validate_ui_channel_sources([channel])
    root = discovery_root.resolve()
    path = deduped_path.resolve()
    rounds = (root / "rounds").resolve()
    if rounds not in path.parents or path.name != "deduped.json":
        raise ValueError("deduped evidence must be a round's deduped.json under title-discovery")
    evidence = json.loads(path.read_text(encoding="utf-8"))
    candidates = evidence.get("new")
    if not isinstance(candidates, list):
        raise ValueError("deduped evidence has no valid new candidate list")
    candidate = next((item for item in candidates
                      if isinstance(item, dict) and item.get("title") == query), None)
    if candidate is None:
        raise ValueError("query is not an exact new candidate in the supplied deduped evidence")
    category = candidate.get("category") if isinstance(candidate.get("category"), str) else ""
    return _admit_evidenced_search(state_path, owner, query, channel, category, path,
                                  "admit-discovery-search", task_id=task_id,
                                  discovery_root=discovery_root, taskbooks=taskbooks)


def _requested_title_scope(request_path: Path, query: str, *,
                           request_root: Path = DEFAULT_REQUESTS) -> tuple[Path, str, str]:
    """Validate the same explicit title evidence for search and detail admission."""
    path = request_path.resolve()
    if request_root.resolve() not in path.parents:
        raise ValueError("request evidence must be under the user-priority directory")
    evidence = json.loads(path.read_text(encoding="utf-8-sig"))
    if (not isinstance(evidence, dict)
            or evidence.get("kind") != "user_requested_titles"
            or evidence.get("user_authorized") is not True
            or not isinstance(evidence.get("requested_at"), str)
            or not evidence["requested_at"].strip()):
        raise ValueError("explicit user title request evidence is required")
    candidates = evidence.get("items")
    if not isinstance(candidates, list) or not 1 <= len(candidates) <= 1000:
        raise ValueError("request evidence needs a bounded title list")
    query = query.strip()
    candidate = next((item for item in candidates
                      if isinstance(item, dict) and item.get("title") == query), None)
    if candidate is None:
        raise ValueError("query is not an exact user-requested title")
    category = candidate.get("category") if isinstance(candidate.get("category"), str) else ""
    return path, query, category


def admit_requested_search(state_path: Path, owner: str, request_path: Path,
                           query: str, channel: str, *, task_id: str | None = None,
                           discovery_root: Path = DEFAULT_DISCOVERY,
                           taskbooks: Path = DEFAULT_TASKBOOKS,
                           request_root: Path = DEFAULT_REQUESTS) -> dict[str, Any]:
    """Admit an exact user-requested title without manufacturing chart evidence."""
    path, query, category = _requested_title_scope(request_path, query, request_root=request_root)
    return _admit_evidenced_search(state_path, owner, query, channel.strip(), category, path,
                                  "admit-requested-search", task_id=task_id,
                                  discovery_root=discovery_root, taskbooks=taskbooks)


def _admit_evidenced_search(state_path: Path, owner: str, query: str, channel: str,
                           category: str, path: Path, command: str, *,
                           task_id: str | None, discovery_root: Path,
                           taskbooks: Path) -> dict[str, Any]:
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch; finish it before admitting a search")
    if not query or not channel:
        raise ValueError("query and channel must be nonempty")
    dbctl.validate_ui_channel_sources([channel])
    root = discovery_root.resolve()
    if task_id is not None:
        _taskbook_path(taskbooks, task_id)
    existing = shared_discovery.inspect_claim(root, query, channel)
    if existing.get("ok") and existing.get("reason") == "claimed":
        claim = existing.get("claim", {})
        if claim.get("owner") != owner:
            return {"ok": False, "command": command,
                    "reason": "already_claimed", "claim": claim}
        current_task = claim.get("task_id")
        if task_id is not None and task_id != current_task:
            raise ValueError("owned discovery claim must resume its existing task_id")
    else:
        current_task = task_id or _task_id(owner)
        claim_result = shared_discovery.claim_search(root, query, channel, owner, current_task)
        if not claim_result.get("ok"):
            return {"ok": False, "command": command,
                    "reason": claim_result.get("reason"), "claim": claim_result.get("claim")}
        claim = claim_result["claim"]
    book_path = _taskbook_path(taskbooks, current_task)
    book = _taskbook(owner, current_task, work, state_path, query, channel, category, root)
    book["inputs"].append(str(path))
    if command == "admit-requested-search":
        book["request_scope"] = {"query": query, "evidence": str(path),
                                 "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
    taskbooks.mkdir(parents=True, exist_ok=True)
    if book_path.exists():
        existing_book = json.loads(book_path.read_text(encoding="utf-8"))
        if (existing_book.get("owner") != owner
                or existing_book.get("scope") != book["scope"]
                or Path(existing_book.get("work", "")).resolve() != work):
            raise RuntimeError("existing taskbook conflicts with the discovery claim")
    else:
        shared_discovery.write_exclusive(book_path, book)
    receipt_path = work / f"search-commit-{current_task}.json"
    if receipt_path.exists():
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if (receipt.get("owner") != owner or receipt.get("task_id") != current_task
                or not isinstance(receipt.get("evidence"), str) or not receipt["evidence"]):
            raise ValueError("existing search receipt conflicts with its owner/task")
        if receipt.get("phase") == "complete":
            return {"ok": True, "command": command, "status": "already_complete",
                    "task_id": current_task, "query": query, "channel": channel,
                    "work": str(work), "taskbook": str(book_path),
                    "receipt": str(receipt_path), "next_action": {"kind": "none"}}
        if receipt.get("phase") != "prepared":
            raise ValueError("existing search receipt has an unknown phase")
        prepared = _prepared_search(state_path, owner, work, channel, {query: claim}, query,
                                    root, taskbooks)
        if prepared is None:
            raise RuntimeError("prepared search receipt could not be resumed")
        return {**prepared, "command": command}
    evidence_path = work / f"{current_task}-evidence.json"
    if evidence_path.exists():
        return {"ok": True, "command": command, "status": "resumed_commit",
                "task_id": current_task, "query": query, "channel": channel,
                "work": str(work), "taskbook": str(book_path),
                "next_action": _commit_action(state_path, owner, str(evidence_path.resolve()),
                                               root, taskbooks)}
    with work_lock(work):
        _ensure_search_plan(work, {"source": channel, "query": query, "category": category})
        synced = dbctl.sync(work, include_status=False)
    effective = shared_discovery.inspect_claim(root, query, channel)
    return {"ok": True, "command": command, "status": "resumed" if existing.get("reason") == "claimed" else "admitted",
            "task_id": current_task, "query": query, "channel": channel, "category": category,
            "work": str(work), "taskbook": str(book_path), "claim_sha256": effective.get("claim_sha256"),
            "sync_imported_records": synced.get("imported_records", 0),
            "next_action": {"kind": "browser_search", "taskbook": str(book_path)}}


def _journal_snapshot(work: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(work.glob("*.ndjson"), key=lambda item: item.name):
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise RuntimeError(f"worker journal has a partial tail: {path.name}")
        rows.append({
            "name": path.name,
            "size": len(data),
            "sha256": hashlib.sha256(data).hexdigest(),
        })
    return rows


def _retained_native_journals(owner: str, document: dict, call_id: str):
    """Read the original native receipt; never calculate replacement proof."""
    from app_server_control import RPC
    with RPC() as rpc:
        thread = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
    if thread.get('id') != owner:
        raise ValueError('Original snapshot receipt owner differs')
    path = Path(thread['path'])
    with path.open('rb') as stream:
        stream.seek(max(0, path.stat().st_size - 1024 * 1024))
        lines = stream.read().splitlines()
    original = result = None
    for line in lines:
        try:
            row = json.loads(line)
        except (ValueError, UnicodeDecodeError):
            continue
        payload = row.get('payload', {})
        if payload.get('call_id') != call_id:
            continue
        if payload.get('type') == 'custom_tool_call' and payload.get('name') == 'exec':
            original = payload.get('input', '')
        elif payload.get('type') == 'custom_tool_call_output':
            result = payload.get('output')
    required = (owner, document['task_id'],
        'a=workflowctl._journal_snapshot(work); time.sleep(.25); b=workflowctl._journal_snapshot(work)',
        "print('snapshot_a',json.dumps(a,ensure_ascii=False))",
        "print('snapshot_b',json.dumps(b,ensure_ascii=False))",
        "print('snapshots_equal',a==b)")
    if not isinstance(original, str) or not all(value in original for value in required):
        raise ValueError('Original native call is not the retained paired snapshot operation')
    if not isinstance(result, list):
        raise ValueError('Original native snapshot output is unavailable')
    text = '\n'.join(chunk.get('text', '') for chunk in result if isinstance(chunk, dict))
    match = re.search(r'(?m)^snapshot_a (\[[^\r\n]*\])', text)
    if not match or 'snapshots_equal True' not in text:
        raise ValueError('Original paired snapshot receipt is incomplete or unequal')
    snapshots = json.loads(match[1])
    return snapshots, {'native_call_id': call_id,
        'source_output_sha256': hashlib.sha256(json.dumps(result, ensure_ascii=False,
            sort_keys=True).encode('utf-8')).hexdigest()}


def repair_terminal_journals(state_path: Path, owner: str, evidence_path: Path,
                             logs_stopped: bool, original_snapshot_call: str | None = None) -> dict[str, Any]:
    """Restore retained snapshot formatting without renewing stale proofs."""
    if not logs_stopped:
        raise ValueError('repair-terminal-journals requires --logs-stopped')
    record, work = owner_record(state_path, owner)
    source = evidence_path.resolve()
    if evidence_path.is_symlink() or source.parent != work or record.get('active_batch') is not None:
        raise ValueError('Evidence must belong to this stopped, batch-free owner work')
    with work_lock(work):
        if (work / '.capture-helper.lock').exists():
            raise RuntimeError('Capture writer remains active; original proof retained')
        raw = source.read_bytes()
        document = json.loads(raw.decode('utf-8-sig'))
        if document.get('owner') != owner or document.get('logs_stopped') is not True:
            raise ValueError('Terminal evidence owner or stopped declaration differs')
        changed_fields = ['journals']
        if original_snapshot_call:
            before, provenance = _retained_native_journals(owner, document, original_snapshot_call)
            existing = document.get('journals')
            if not isinstance(existing, list) or len(existing) != len(before):
                raise ValueError('Original canonical journal shape differs')
            changed_fields = []
            for index, (stored, observed) in enumerate(zip(existing, before)):
                if stored == observed:
                    continue
                old_hash, real_hash = stored.get('sha256', ''), observed.get('sha256', '')
                if ({**stored, 'sha256': real_hash} != observed
                        or not re.fullmatch(r'[0-9a-f]{63}', old_hash)
                        or not re.fullmatch(r'[0-9a-f]{64}', real_hash)
                        or not any(real_hash[:i] + real_hash[i + 1:] == old_hash for i in range(64))):
                    raise ValueError('Only one omitted hash character from the original receipt may be restored')
                changed_fields.append(f'journals[{index}].sha256')
            if len(changed_fields) != 1:
                raise ValueError('Expected exactly one original hash transcription omission')
            after = before
            document['journal_transcription_recovery'] = provenance
        else:
            if document.get('journals') is not None:
                raise ValueError('Existing canonical journals are not overwritten')
            before = document.get('journals_before')
            after = document.get('journals_after')
        actual = _journal_snapshot(work)
        if not isinstance(before, list) or before != after or before != actual:
            raise ValueError('Original before/after snapshots are not identical to current logs; no repair')
        time.sleep(0.2)
        if source.read_bytes() != raw or _journal_snapshot(work) != actual:
            raise RuntimeError('Source or stopped journals changed; original proof retained')
        # No browser/claim/action/DOM/nonce/hash facts are changed or synthesized.
        document['journals'] = before
        output = source.with_name(source.stem + '-journals-corrected.json')
        if output.is_symlink():
            raise ValueError('Refusing a linked derived evidence path')
        if output.exists():
            if json.loads(output.read_text('utf-8-sig')) != document:
                raise ValueError('Derived evidence already exists with different facts')
        else:
            atomic_json(output, document)
        return {'ok': True, 'command': 'repair-terminal-journals',
            'task_id': document.get('task_id'), 'evidence': str(output),
            'source_sha256': hashlib.sha256(raw).hexdigest(),
            'changed_fields': changed_fields, 'journal_count': len(before),
            'search_success': False, 'replay_authorized': False}


def _nonempty_text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"abandonment evidence requires {label}")
    return value.strip()


def abandon_search_unknown_action(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Terminally quarantine a search whose UI action cannot be inspected safely."""
    if not logs_stopped:
        raise ValueError("abandon-search requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("abandonment evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("abandonment evidence must be a JSON object")
    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    if current.get("reason") == "abandoned_unknown_action":
        if evidence.get("claim_sha256") != current.get("claim_sha256"):
            raise ValueError("existing abandonment evidence does not match claim hash")
        return {
            "ok": True, "command": "abandon-search",
            "status": "already_abandoned_unknown_action",
            "task_id": current["claim"]["task_id"],
            "query": query, "channel": channel,
            "abandonment_id": current["claim"]["abandonment_id"],
            "ledger": str((discovery_root / "search-claim-abandonments.json").resolve()),
        }
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": "abandoned_unknown_action", "owner": owner,
        "task_id": task_id, "query": query, "channel": channel,
        "claim_sha256": claim_hash, "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("abandonment evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book_path = _taskbook_path(taskbooks, task_id)
    book = json.loads(book_path.read_text(encoding="utf-8"))
    if (
        book.get("kind") != "search" or book.get("owner") != owner
        or Path(book.get("work", "")).resolve() != work
        or book.get("scope", {}).get("queries") != [query]
        or book.get("scope", {}).get("channels") != [channel]
    ):
        raise ValueError("search taskbook does not match abandonment target")
    host = evidence.get("browser_host")
    tab = evidence.get("browser_tab")
    action = evidence.get("search_action")
    local = evidence.get("local_state")
    if not isinstance(host, dict) or (
        host.get("session_id") != owner or host.get("terminated") is not True
        or host.get("terminal_status") not in {"interrupted", "systemError", "terminated", "archived"}
    ):
        raise ValueError("abandonment requires proof that the original browser host terminated")
    if not isinstance(tab, dict) or tab.get("exists") is not False:
        raise ValueError("abandonment requires proof that the original browser tab no longer exists")
    if not isinstance(action, dict) or any((
        action.get("state") != "unknown",
        action.get("original_module_instance_available") is not False,
        action.get("active_action_capability_available") is not False,
        action.get("action_id_recoverable") is not False,
        action.get("execute_receipt") is not False,
        action.get("inspect_receipt") is not False,
        action.get("finalize_receipt") is not False,
        action.get("replay_authorized") is not False,
    )):
        raise ValueError("abandonment requires unknown-action proof and must refuse replay")
    if not isinstance(local, dict) or any((
        local.get("browser_evidence_exists") is not False,
        local.get("commit_receipt_exists") is not False,
        local.get("search_journal_contains_task") is not False,
    )):
        raise ValueError("abandonment evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")
    expected_evidence_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if expected_evidence_path.exists():
            raise RuntimeError("browser evidence exists; commit it instead of abandoning")
        if commit_path.exists():
            raise RuntimeError("search commit receipt exists; resume commit instead of abandoning")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals") != journals_before:
            raise ValueError("abandonment journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] != "searches.ndjson":
                continue
            for line in (work / item["name"]).read_text(encoding="utf-8").splitlines():
                if line.strip() and json.loads(line).get("task_id") == task_id:
                    raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        if _journal_snapshot(work) != journals_before:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": "abandoned_unknown_action",
            "browser_host_terminated": True,
            "browser_host_terminal_status": host["terminal_status"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "browser_tab_exists": False,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "original_module_instance_available": False,
            "active_action_capability_available": False,
            "action_state": "unknown", "replay_authorized": False,
            "logs_stopped": True, "journals": journals_before,
            "browser_evidence_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "abandon-search",
        "status": abandoned["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": abandoned["abandonment_id"],
        "ledger": str(Path(abandoned["ledger"]).resolve()),
        "next_action": {"kind": "claim_other_search", "replay_abandoned": False},
    }


def quarantine_search_unverifiable_result(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, allow_saved_candidate: bool = False, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS, authorized_parent_host: bool = False,
    authorized_retained_action: bool = False,
) -> dict[str, Any]:
    """Terminally isolate one settled UI action that cannot meet evidence gates."""
    if not logs_stopped:
        raise ValueError("quarantine-search requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("quarantine evidence is incomplete or invalid") from exc
    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": "quarantined_unverifiable_result", "owner": owner,
        "task_id": task_id, "query": query, "channel": channel,
        "claim_sha256": claim_hash, "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("quarantine evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match quarantine target")
    host, tab, action, local = (evidence.get(key) for key in
                                ("browser_host", "browser_tab", "search_action", "local_state"))
    parent_proof = None
    if authorized_parent_host and authorized_retained_action:
        raise ValueError("choose exactly one authorized recovery mode")
    if authorized_parent_host or authorized_retained_action:
        from search_parent_host_quarantine import validate as validate_parent_host
        if allow_saved_candidate and not authorized_retained_action:
            raise ValueError("parent-host selector recovery cannot replace saved evidence")
        if json.loads(Path(state_path).read_bytes()).get("collection_paused") is True:
            raise ValueError("collection paused; preserve the original prepared action")
        if (evidence_path.is_symlink() or not evidence_path.resolve().is_relative_to(work.resolve())
                or len(raw) > 1024 * 1024):
            raise ValueError("parent-host evidence must be bounded and inside the original owner work")
        parent_proof = validate_parent_host(evidence, owner, work, query, channel,
            project_root=PROJECT_ROOT, retained_action=authorized_retained_action)
    elif not isinstance(host, dict) or host.get("session_id") != owner or host.get("terminated") is not False:
        raise ValueError("quarantine requires the live original browser host")
    if not isinstance(tab, dict) or (parent_proof is None and tab.get("exists") is not True):
        raise ValueError("quarantine requires the original task tab")
    retired_dormant = parent_proof is not None and parent_proof[2].get("retired_browser_retained_action") is True
    if retired_dormant and allow_saved_candidate:
        raise ValueError("retired-browser recovery cannot replace a saved search candidate")
    if not isinstance(action, dict) or any((
        action.get("state") != ("settled_unverifiable_retired_browser" if retired_dormant else "completed_unverifiable"),
        action.get("action_attempts") != 1,
        action.get("replay_authorized") is not False,
    )) or not isinstance(action.get("result_error"), str) or not action["result_error"].strip():
        raise ValueError("quarantine requires exactly one settled, unverifiable action in the authorized mode")
    module_available = action.get("original_module_instance_available")
    if not isinstance(module_available, bool):
        raise ValueError("quarantine requires explicit original module availability")
    if module_available is False and action.get("public_ui_completed") is not True:
        raise ValueError("lost module requires visible completed UI state, never an unknown action")
    if (not isinstance(local, dict)
            or local.get("browser_evidence_exists") is not allow_saved_candidate
            or any(local.get(key) is not False for key in (
                "commit_receipt_exists", "search_journal_contains_task"))):
        raise ValueError("quarantine evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        candidate_path = work / f"{task_id}-evidence.json"
        commit_path = work / f"search-commit-{task_id}.json"
        if commit_path.exists():
            raise RuntimeError("saved commit exists; resume it instead of quarantining")
        if candidate_path.exists() != allow_saved_candidate:
            raise RuntimeError("saved evidence state does not match quarantine mode")
        candidate_sha256 = None
        if allow_saved_candidate:
            try:
                candidate_raw = candidate_path.read_bytes()
                if candidate_path.is_symlink() or len(candidate_raw) > 1024 * 1024:
                    raise ValueError("candidate must be bounded and inside original owner work")
                candidate = json.loads(candidate_raw.decode("utf-8-sig"))
            except (OSError, ValueError) as exc:
                raise RuntimeError("saved candidate evidence is unreadable") from exc
            candidate_sha256 = hashlib.sha256(candidate_raw).hexdigest()
            if authorized_retained_action:
                if (not isinstance(candidate, dict) or candidate.get("task_id") != task_id
                        or candidate.get("owner") != owner or candidate.get("channel") != channel
                        or candidate.get("selected_channels") != [channel]
                        or not isinstance(candidate.get("query"), str) or not candidate["query"]
                        or candidate["query"] == query
                        or candidate.get("input_value_after_action") != candidate["query"]
                        or evidence.get("local_state", {}).get("saved_candidate_sha256") != candidate_sha256):
                    raise RuntimeError("saved candidate must prove exact original-task query/input mismatch and unchanged bytes")
            elif (candidate.get("task_id") != task_id or candidate.get("owner") != owner
                    or candidate.get("query") != query or candidate.get("channel") != channel
                    or candidate.get("result_count") != 0
                    or candidate.get("visible_detail_links") != []):
                raise RuntimeError("saved candidate evidence does not prove this zero-result task")
        journals = _journal_snapshot(work)
        if evidence.get("journals") != journals:
            raise ValueError("quarantine journal snapshot does not match worker logs")
        for item in journals:
            if item["name"] == "searches.ndjson" and any(
                    json.loads(line).get("task_id") == task_id
                    for line in (work / item["name"]).read_text(encoding="utf-8").splitlines() if line.strip()):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        if _journal_snapshot(work) != journals:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": "quarantined_unverifiable_result", "browser_host_terminated": False,
            "browser_tab_exists": True, "original_module_instance_available": module_available,
            "public_ui_completed": action.get("public_ui_completed") is True,
            "action_state": "completed_unverifiable", "replay_authorized": False,
            "logs_stopped": True, "journals": journals, "browser_evidence_exists": allow_saved_candidate,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        if parent_proof is not None:
            config_path, config_sha256, parent_audit = parent_proof
            if (hashlib.sha256(config_path.read_bytes()).hexdigest() != config_sha256
                    or json.loads(Path(state_path).read_bytes()).get("collection_paused") is True
                    or evidence_path.read_bytes() != raw):
                raise RuntimeError("parent binding, pause gate or original evidence changed; preserve action")
            audit.update(parent_audit)
        if authorized_retained_action:
            if (owner_record(state_path, owner)[0].get("active_batch") is not None
                    or (work / ".capture-helper.lock").exists()
                    or commit_path.exists()
                    or candidate_path.exists() != allow_saved_candidate
                    or (allow_saved_candidate and candidate_path.read_bytes() != candidate_raw)):
                raise RuntimeError("original owner/action evidence state changed; preserve action")
        if allow_saved_candidate:
            audit.update({"saved_candidate_evidence": str(candidate_path.resolve()),
                          "saved_candidate_evidence_sha256": candidate_sha256})
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    result = {"ok": True, "command": "quarantine-search", "status": quarantined["reason"],
            "task_id": task_id, "query": query, "channel": channel,
            "abandonment_id": quarantined["abandonment_id"], "ledger": str(Path(quarantined["ledger"]).resolve()),
            "next_action": {"kind": "claim_other_search", "replay_abandoned": False}}
    if retired_dormant:
        result.update({"released": False, "search_success": False, "replay_authorized": False,
                       "retained_capability_status": "quarantined_dormant_not_released"})
        result["next_action"]["never_use_action_id"] = action["action_id"]
    return result


def resume_quarantined_misbound_search(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Permit OTHER tasks after a proven misbound, settled capability is quarantined.

    The old module/capability is retained dormant, NOT released or rewritten.
    An immutable manual-review terminal and verified main-thread proof are required.
    """
    if not logs_stopped:
        raise ValueError("resume-misbound-search requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    config_path = PROJECT_ROOT / ".runtime/main-agent/state.json"
    config_raw = config_path.read_bytes()
    config = json.loads(config_raw)
    auth = config.get("fault_repair_authorization", {})
    slot = next((v for v in config.get("slots", {}).values() if v.get("owner") == owner), {})
    if (config.get("paused") is True or auth.get("authorized") is not True
            or auth.get("basis") != "direct_user"
            or auth.get("scope") != "existing_workflow_fault_source_and_tests"
            or Path(slot.get("work", "")).resolve() != work
            or record.get("active_batch") is not None):
        raise ValueError("exact owner binding and standing fault-repair authorization required")
    raw = evidence_path.read_bytes()
    if evidence_path.is_symlink() or not evidence_path.resolve().is_relative_to(work) or len(raw) > 1024 * 1024:
        raise ValueError("original quarantine evidence must be bounded inside owner work")
    evidence = json.loads(raw)
    if not isinstance(evidence, dict):
        raise ValueError("original quarantine evidence must be an object")
    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_bytes())
    records = shared_discovery.load_abandonment_ledger(discovery_root)[1]["abandonments"]
    terminals = [v for v in records if v.get("owner") == owner and v.get("task_id") == task_id
                 and v.get("query") == query and v.get("channel") == channel]
    if len(terminals) != 1:
        raise ValueError("one authoritative original quarantine terminal required")
    terminal = terminals[0]
    action_id = terminal.get("action_id")
    proofs = [v for v in config.get("retained_misbound_actions", [])
              if v.get("owner") == owner and v.get("task_id") == task_id and v.get("action_id") == action_id]
    if len(proofs) != 1:
        raise ValueError("main-thread verified original prepare mismatch proof required")
    proof = proofs[0]
    history = evidence.get("historical_readonly_observation") or {}
    historical_input = history.get("input_value_after_action", history.get("actual_input_value"))
    if (claim.get("owner") != owner or book.get("owner") != owner or book.get("kind") != "search"
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]
            or proof.get("query") != query or proof.get("channel") != channel
            or not isinstance(proof.get("prepared_query"), str) or not proof["prepared_query"]
            or proof["prepared_query"] == query or proof.get("proof_verified") is not True
            or proof.get("all_tools_terminal") is not True
            or proof.get("release_error") != "Release requires a matching workflow terminal receipt from the runtime ledger"
            or not proof.get("original_prepare_tool") or not proof.get("original_execute_tool")
            or terminal.get("reason") != "quarantined_unverifiable_result"
            or terminal.get("manual_review_required") is not True or terminal.get("search_success") is not False
            or terminal.get("replay_authorized") is not False
            or terminal.get("original_module_instance_available") is not True
            or terminal.get("execute_returned") is not True or terminal.get("finalize_returned") is not True
            or terminal.get("action_attempts") != 1
            or terminal.get("source_claim_sha256") != current.get("claim_sha256")
            or terminal.get("evidence_sha256") != hashlib.sha256(raw).hexdigest()
            or evidence.get("search_action", {}).get("action_id") != action_id
            or historical_input != proof["prepared_query"]
            or ("actual_input_value" in history and history["actual_input_value"] != historical_input)):
        raise ValueError("settled original mismatch and immutable quarantine must be proved; never release or replay fiction")
    result = {"ok": True, "command": "resume-misbound-search", "owner": owner,
        "task_id": task_id, "action_id": action_id, "abandonment_id": terminal["abandonment_id"],
        "evidence_sha256": terminal["evidence_sha256"], "released": False,
        "retained_capability_status": "quarantined_dormant_not_released",
        "search_success": False, "replay_authorized": False,
        "next_action": {"kind": "claim_other_search", "require_original_taskbook_values": True,
                        "never_use_action_id": action_id}}
    with work_lock(work):
        journals = _journal_snapshot(work)
        if journals != terminal.get("journals"):
            raise RuntimeError("owner logs changed; preserve original capability")
        if (work / ".capture-helper.lock").exists() or any((work / v).exists() for v in
                (f"{task_id}-evidence.json", f"search-commit-{task_id}.json")):
            raise RuntimeError("active writer or saved successful evidence must be resumed instead")
        time.sleep(0.2)
        if (config_path.read_bytes() != config_raw or evidence_path.read_bytes() != raw
                or _journal_snapshot(work) != journals
                or owner_record(state_path, owner)[0].get("active_batch") is not None
                or json.loads(Path(state_path).read_bytes()).get("collection_paused") is True):
            raise RuntimeError("authority, owner, pause, evidence or logs changed")
        receipt_path = work / f"search-resume-{task_id}.json"
        if receipt_path.exists():
            if json.loads(receipt_path.read_bytes()) != result:
                raise RuntimeError("existing recovery receipt differs; preserve it")
        else:
            atomic_json(receipt_path, result)
    return {**result, "receipt": str(receipt_path.resolve())}


def quarantine_search_foreign_host_session(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Terminally quarantine one completed click whose browser host belongs to another runtime session."""
    if not logs_stopped:
        raise ValueError("quarantine-search-host-mismatch requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("host-mismatch quarantine evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("host-mismatch quarantine evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": "quarantined_foreign_host_session",
        "owner": owner, "task_id": task_id, "query": query,
        "channel": channel, "claim_sha256": claim_hash, "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("host-mismatch evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match host-mismatch quarantine target")

    host, tab, action, dom, local = (evidence.get(key) for key in
                                    ("browser_host", "browser_tab", "search_action",
                                     "dom_summary", "local_state"))
    if (not isinstance(host, dict)
            or not isinstance(host.get("session_id"), str)
            or not host["session_id"].strip()
            or host["session_id"] == owner
            or host.get("thread_id") != owner
            or host.get("terminated") is not False):
        raise ValueError("evidence must preserve the foreign runtime session and matching owner thread separately")
    if not isinstance(tab, dict) or tab.get("exists") is not True:
        raise ValueError("quarantine requires the still-readable original task tab")
    legacy_helper_without_id = (
        isinstance(action, dict) and "action_id" not in action
        and action.get("helper_entry") == "runSearchActionEvidence"
        and action.get("helper_call_attempts") == 1
        and action.get("helper_returned") is True
        and action.get("click_returned") == "unknown"
        and action.get("click_completion_receipt_available") is False
        and action.get("action_id_emitted") is False
        and action.get("active_action_capability_available") is False
        and action.get("result_error") == "Zero results require a visible empty-result indicator"
    )
    if (not isinstance(action, dict)
            or action.get("state") != "completed_unverifiable"
            or action.get("action_attempts") != 1
            or action.get("replay_authorized") is not False
            or action.get("original_module_instance_available") is not True
            or not legacy_helper_without_id and action.get("click_returned") is not True):
        raise ValueError("quarantine requires one unreplayed attempt with its original module and recorded outcome")
    if not legacy_helper_without_id:
        _nonempty_text(action.get("action_id"), "search_action.action_id")
    elif (tab.get("readable") is not True or tab.get("session_id") != host["session_id"]
          or tab.get("owner_thread_id") != owner):
        raise ValueError("legacy helper quarantine requires the readable original host/thread tab")
    _nonempty_text(action.get("result_error"), "search_action.result_error")
    if (not isinstance(dom, dict)
            or dom.get("input_value_after_action") != query
            or dom.get("selected_channels") != [channel]
            or dom.get("result_count") != 0
            or dom.get("visible_detail_links") != []
            or dom.get("empty_result_visible") is not False
            or dom.get("pagination_pending") is not False
            or dom.get("result_container_visible") is not True):
        raise ValueError("DOM summary must prove this exact query/channel and completed zero-result state without an empty indicator")
    _nonempty_text(dom.get("page_url"), "dom_summary.page_url")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    if legacy_helper_without_id and dom.get("loading_indicator") is not None:
        raise ValueError("legacy helper quarantine requires a settled DOM without a loading indicator")
    page = urlsplit(dom["page_url"])
    if (page.scheme not in {"http", "https"} or page.username or page.password
            or not page.hostname or not (page.hostname.lower() == "xxmanmi.com"
                                         or page.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("DOM summary page_url must be a public xxmanmi.com page")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    )):
        raise ValueError("host-mismatch evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals = _journal_snapshot(work)
        if evidence.get("journals") != journals:
            raise ValueError("host-mismatch journal snapshot does not match worker logs")
        for item in journals:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        if _journal_snapshot(work) != journals:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": "quarantined_foreign_host_session",
            "browser_host_terminated": False, "browser_tab_exists": True,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "foreign_host_session_mismatch": True,
            "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "browser_tab_id": tab["tab_id"],
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "action_id": action.get("action_id"), "dom_summary": dom,
            "action_identity_source": "legacy_helper_no_id" if legacy_helper_without_id else "prepared_action",
            "helper_entry": action.get("helper_entry"),
            "helper_call_attempts": action.get("helper_call_attempts"),
            "action_id_emitted": action.get("action_id_emitted"),
            "active_action_capability_available": action.get("active_action_capability_available"),
            "helper_returned": action.get("helper_returned"),
            "click_completion_receipt_available": action.get("click_completion_receipt_available"),
            "click_returned": action.get("click_returned"),
            "browser_tab_readable": tab.get("readable"),
            "browser_tab_session_id": tab.get("session_id"),
            "browser_tab_owner_thread_id": tab.get("owner_thread_id"),
            "result_error": action["result_error"],
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals": journals,
            "browser_evidence_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "quarantine-search-host-mismatch",
        "status": quarantined["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": quarantined["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(quarantined["ledger"]).resolve()),
    }


def quarantine_search_foreign_host_input_mismatch(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Quarantine one returned foreign-host click whose submitted query was not the claim."""
    reason = "quarantined_foreign_host_input_mismatch"
    if not logs_stopped:
        raise ValueError("quarantine-search-foreign-host-input-mismatch requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("input-mismatch quarantine evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("input-mismatch quarantine evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("input-mismatch evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match input-mismatch quarantine target")

    host, tab, action, dom, local = (evidence.get(key) for key in
                                    ("browser_host", "browser_tab", "search_action",
                                     "dom_summary", "local_state"))
    if (not isinstance(host, dict)
            or not isinstance(host.get("session_id"), str)
            or not host["session_id"].strip()
            or host["session_id"] == owner
            or host.get("thread_id") != owner
            or host.get("terminated") is not False):
        raise ValueError("evidence must preserve the live foreign host session and exact owner thread")
    for field in ("checked_at", "method"):
        _nonempty_text(host.get(field), f"browser_host.{field}")
    if (not isinstance(tab, dict) or tab.get("exists") is not True
            or tab.get("readable") is not True
            or tab.get("session_id") != host["session_id"]
            or tab.get("owner_thread_id") != owner):
        raise ValueError("quarantine requires the readable original task tab on the foreign host")
    for field in ("tab_id", "checked_at", "method"):
        _nonempty_text(tab.get(field), f"browser_tab.{field}")
    if (not isinstance(action, dict)
            or action.get("state") != "completed_unverifiable"
            or action.get("task_id") != task_id
            or action.get("action_attempts") != 1
            or action.get("click_returned") is not True
            or action.get("original_module_instance_available") is not True
            or action.get("replay_authorized") is not False):
        raise ValueError("quarantine requires exactly one returned task click and no replay")
    _nonempty_text(action.get("action_id"), "search_action.action_id")
    if action.get("result_error") != "Zero results require a visible empty-result indicator":
        raise ValueError("quarantine requires the recorded zero-result evidence-gate failure")
    actual_input = dom.get("input_value_after_action") if isinstance(dom, dict) else None
    if (not isinstance(dom, dict)
            or not isinstance(actual_input, str) or not actual_input.strip()
            or actual_input == query
            or dom.get("selected_channels") != [channel]
            or dom.get("result_count") != 0
            or dom.get("visible_detail_links") != []
            or dom.get("empty_result_visible") is not False
            or dom.get("pagination_pending") is not False
            or dom.get("result_container_visible") is not True
            or (dom.get("loading_indicator") is not None
                and (not isinstance(dom.get("loading_indicator"), dict)
                     or dom["loading_indicator"].get("visible_count") != 0))):
        raise ValueError("DOM must prove the exact channel and a mismatched submitted query with unverifiable zero results")
    _nonempty_text(dom.get("page_url"), "dom_summary.page_url")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    page = urlsplit(dom["page_url"])
    if (page.scheme not in {"http", "https"} or page.username or page.password
            or not page.hostname or not (page.hostname.lower() == "xxmanmi.com"
                                         or page.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("DOM summary page_url must be a public xxmanmi.com page")
    if (not isinstance(local, dict)
            or local.get("taskbook_scope_matches") is not True
            or any(local.get(key) is not False for key in (
                "browser_evidence_exists", "commit_receipt_exists",
                "search_journal_contains_task", "capture_lock_exists"))):
        raise ValueError("input-mismatch evidence must declare matching taskbook and absent evidence/commit/journal/lock")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals = _journal_snapshot(work)
        if evidence.get("journals_before") != journals or evidence.get("journals_after") != journals:
            raise ValueError("two input-mismatch journal snapshots must match current worker logs")
        for item in journals:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": reason,
            "browser_host_terminated": False,
            "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "browser_tab_exists": True, "browser_tab_readable": True,
            "browser_tab_session_id": tab["session_id"],
            "browser_tab_owner_thread_id": tab["owner_thread_id"],
            "browser_tab_id": tab["tab_id"],
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "foreign_host_session_mismatch": True,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "click_returned": True, "action_id": action["action_id"],
            "action_task_id": task_id, "result_error": action["result_error"],
            "claim_query": query, "dom_input_value": actual_input,
            "dom_input_mismatch": True, "dom_summary": dom,
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals,
            "journals_after": journals_after,
            "browser_evidence_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False, "capture_lock_exists": False,
            "summary": evidence["summary"],
        }
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "quarantine-search-foreign-host-input-mismatch",
        "status": quarantined["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": quarantined["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(quarantined["ledger"]).resolve()),
    }


def quarantine_search_foreign_host_stale_results(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Manual-review terminal for a returned foreign-host click leaving stale old results visible."""
    reason = "quarantined_foreign_host_stale_results"
    if not logs_stopped:
        raise ValueError("quarantine-search-foreign-host-stale-results requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("stale-results quarantine evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("stale-results quarantine evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("stale-results evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match stale-results quarantine target")

    host, tab, action, dom, prior, local = (evidence.get(key) for key in
        ("browser_host", "browser_tab", "search_action", "dom_summary",
         "pre_action_dom_summary", "local_state"))
    if (not isinstance(host, dict) or not isinstance(host.get("session_id"), str)
            or not host["session_id"].strip() or host["session_id"] == owner
            or host.get("thread_id") != owner or host.get("terminated") is not False):
        raise ValueError("evidence must prove a live foreign host and exact owner thread")
    if (not isinstance(tab, dict) or tab.get("exists") is not True
            or tab.get("readable") is not True or tab.get("owner_thread_id") != owner
            or tab.get("session_id") != host["session_id"]):
        raise ValueError("quarantine requires the readable original task tab on the foreign host")
    if (not isinstance(action, dict) or action.get("state") != "completed_unverifiable"
            or action.get("task_id") != task_id or action.get("action_attempts") != 1
            or action.get("click_returned") is not True
            or action.get("replay_authorized") is not False):
        raise ValueError("quarantine requires exactly one confirmed task click and no replay")
    _nonempty_text(action.get("action_id"), "search_action.action_id")
    _nonempty_text(action.get("result_error"), "search_action.result_error")
    if (not isinstance(dom, dict) or not isinstance(prior, dict)
            or dom.get("input_value_after_action") != query
            or dom.get("selected_channels") != [channel]
            or dom.get("result_container_visible") is not True
            or dom.get("pagination_pending") is not False
            or dom.get("loading_visible_count") != 0
            or prior.get("query") == query
            or not isinstance(prior.get("visible_detail_links"), list)
            or not prior["visible_detail_links"]
            or dom.get("visible_detail_links") != prior.get("visible_detail_links")
            or dom.get("result_count") != len(dom.get("visible_detail_links", []))
            or any(link.get("title") == query for link in dom["visible_detail_links"]
                   if isinstance(link, dict))):
        raise ValueError("DOM snapshots must prove exact target input/channel and unchanged visible non-target results")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    for summary, label in ((dom, "dom_summary"), (prior, "pre_action_dom_summary")):
        _nonempty_text(summary.get("page_url"), f"{label}.page_url")
        _nonempty_text(summary.get("observed_at"), f"{label}.observed_at")
        page = urlsplit(summary["page_url"])
        if (page.scheme not in {"http", "https"} or page.username or page.password
                or not page.hostname or not (page.hostname.lower() == "xxmanmi.com"
                    or page.hostname.lower().endswith(".xxmanmi.com"))):
            raise ValueError("DOM summary page_url must be a public xxmanmi.com page")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
        "capture_lock_exists",
    )):
        raise ValueError("stale-results evidence must declare absent artifacts, task journal, and lock")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first stale-results journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals_before or evidence.get("journals_after") != journals_after:
            raise RuntimeError("two stale-results journal snapshots are not stable")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_host_session_id": host["session_id"], "browser_host_thread_id": owner,
            "browser_host_checked_at": host["checked_at"], "browser_host_check_method": host["method"],
            "browser_tab_exists": True, "browser_tab_readable": True,
            "foreign_host_session_mismatch": True,
            "browser_tab_id": tab["tab_id"], "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "original_module_instance_available": True,
            "action_id": action["action_id"], "result_error": action["result_error"],
            "action_state": action["state"], "action_attempts": 1,
            "dom_summary": dom, "pre_action_dom_summary": prior,
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals_before,
            "journals_after": journals_after, "browser_evidence_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "capture_lock_exists": False, "summary": evidence["summary"],
        }
        terminal = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "quarantine-search-foreign-host-stale-results",
        "status": terminal["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": terminal["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(terminal["ledger"]).resolve()),
    }


def quarantine_search_legacy_evidence_gate(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Terminally isolate a confirmed click stranded by a legacy evidence gate."""
    reason = "quarantined_legacy_evidence_gate_failure"
    if not logs_stopped:
        raise ValueError("quarantine-search-legacy-evidence-gate requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("legacy evidence-gate quarantine evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("legacy evidence-gate quarantine evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("legacy evidence-gate evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match legacy evidence-gate quarantine target")

    host, tab, action, dom, local = (evidence.get(key) for key in
                                    ("browser_host", "browser_tab", "search_action",
                                     "dom_summary", "local_state"))
    if (not isinstance(host, dict) or host.get("session_id") != owner
            or not isinstance(host.get("thread_id"), str) or not host["thread_id"].strip()
            or host.get("terminated") is not False):
        raise ValueError("legacy evidence-gate quarantine requires the live owner browser session")
    if (not isinstance(tab, dict) or tab.get("exists") is not True
            or tab.get("owner_thread_id") != owner):
        raise ValueError("legacy evidence-gate quarantine requires the readable original owner tab")
    if (not isinstance(action, dict)
            or action.get("state") != "completed_unverifiable"
            or action.get("action_attempts") != 1
            or action.get("click_returned") is not True
            or action.get("replay_authorized") is not False
            or action.get("original_module_instance_available") is not True
            or action.get("private_action_record_exportable") is not False
            or action.get("finalize_attempts") != 1):
        raise ValueError("legacy evidence-gate quarantine requires one returned click and an unexportable private action record")
    action_id = _nonempty_text(action.get("action_id"), "search_action.action_id")
    finalization_error = _nonempty_text(action.get("finalize_gate_error"),
                                        "search_action.finalize_gate_error")
    if (action.get("old_api_version") != 3 or action.get("new_api_version") != 4
            or "Search results did not visibly change from the pre-action snapshot" not in finalization_error):
        raise ValueError("legacy evidence-gate quarantine requires the recorded v3-to-v4 gate failure")
    if (not isinstance(dom, dict)
            or dom.get("input_value_after_action") != query
            or dom.get("selected_channels") != [channel]
            or dom.get("result_count") != 0
            or dom.get("visible_detail_links") != []
            or dom.get("empty_result_visible") is not True
            or dom.get("pagination_pending") is not False
            or dom.get("result_container_visible") is not True):
        raise ValueError("DOM summary must prove the exact query/channel and visible zero-result state")
    _nonempty_text(dom.get("page_url"), "dom_summary.page_url")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    page = urlsplit(dom["page_url"])
    if (page.scheme not in {"http", "https"} or page.username or page.password
            or not page.hostname or not (page.hostname.lower() == "xxmanmi.com"
                                         or page.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("DOM summary page_url must be a public xxmanmi.com page")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    )):
        raise ValueError("legacy evidence-gate quarantine must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first legacy evidence-gate journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if (journals_after != journals_before
                or evidence.get("journals_after") != journals_after):
            raise RuntimeError("two legacy evidence-gate journal snapshots are not identical")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_tab_exists": True, "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "legacy_evidence_gate_failure": True,
            "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "browser_tab_id": tab_id,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "old_api_version": action["old_api_version"],
            "new_api_version": action["new_api_version"],
            "click_returned": True, "private_action_record_exportable": False,
            "finalize_attempts": 1,
            "action_id": action_id, "finalize_gate_error": finalization_error,
            "dom_summary": dom, "manual_review_required": True,
            "replay_authorized": False, "logs_stopped": True,
            "journals_before": journals_before, "journals_after": journals_after,
            "browser_evidence_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False, "summary": evidence["summary"],
        }
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "quarantine-search-legacy-evidence-gate",
        "status": quarantined["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": quarantined["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(quarantined["ledger"]).resolve()),
    }


def quarantine_search_foreign_host_tab_gone(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Quarantine a completed search after its foreign-host task tab disappears."""
    reason = "quarantined_foreign_host_tab_gone"
    if not logs_stopped:
        raise ValueError("quarantine-search-host-tab-gone requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("host-tab-gone evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("host-tab-gone evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("host-tab-gone evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match host-tab-gone target")

    host, tab, action, dom, local = (evidence.get(key) for key in
                                    ("browser_host", "browser_tab", "search_action",
                                     "dom_summary", "local_state"))
    if (not isinstance(host, dict)
            or not isinstance(host.get("session_id"), str)
            or not host["session_id"].strip()
            or host["session_id"] == owner
            or host.get("thread_id") != owner
            or host.get("terminated") is not False):
        raise ValueError("evidence must preserve the foreign runtime session and matching owner thread separately")
    if not isinstance(tab, dict) or tab.get("exists") is not False:
        raise ValueError("host-tab-gone quarantine requires the original task tab to be absent")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    missing_error = tab.get("missing_error")
    missing_message = f"No tab with id: {tab_id}"
    # Chrome tool transports may add an Error prefix or terminal period.
    # Match only this exact tab and preserve the original transport text.
    if not isinstance(missing_error, str) or missing_error not in {
        missing_message, missing_message + ".",
        "Error: " + missing_message, "Error: " + missing_message + ".",
    }:
        raise ValueError("browser_tab.missing_error must preserve the exact No tab with id error")
    inventory = tab.get("inventory_tab_ids")
    if not isinstance(inventory, list) or any(not isinstance(value, str) for value in inventory) or tab_id in inventory:
        raise ValueError("browser tab inventory must confirm the original task tab is absent")
    if (not isinstance(action, dict)
            or action.get("state") != "completed_unverifiable"
            or action.get("action_attempts") != 1
            or action.get("replay_authorized") is not False
            or action.get("original_module_instance_available") is not True
            or action.get("click_returned") is not True):
        raise ValueError("quarantine requires one confirmed click with the original module and no replay")
    _nonempty_text(action.get("action_id"), "search_action.action_id")
    _nonempty_text(action.get("result_error"), "search_action.result_error")
    if (not isinstance(dom, dict)
            or dom.get("input_value_after_action") != query
            or dom.get("selected_channels") != [channel]
            or dom.get("result_count") != 0
            or dom.get("visible_detail_links") != []
            or dom.get("empty_result_visible") is not False
            or dom.get("pagination_pending") is not False
            or dom.get("result_container_visible") is not True):
        raise ValueError("DOM summary must prove this exact query/channel and completed zero-result state without an empty indicator")
    _nonempty_text(dom.get("page_url"), "dom_summary.page_url")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    page = urlsplit(dom["page_url"])
    if (page.scheme not in {"http", "https"} or page.username or page.password
            or not page.hostname or not (page.hostname.lower() == "xxmanmi.com"
                                         or page.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("DOM summary page_url must be a public xxmanmi.com page")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    )):
        raise ValueError("host-tab-gone evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first host-tab-gone journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if (journals_after != journals_before
                or evidence.get("journals_after") != journals_after):
            raise RuntimeError("two host-tab-gone journal snapshots are not identical")
        audit = {
            "reason": reason,
            "browser_host_terminated": False, "browser_tab_exists": False,
            "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "foreign_host_session_mismatch": True,
            "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "browser_tab_id": tab_id,
            "browser_tab_missing_error": missing_error,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "action_id": action["action_id"], "result_error": action["result_error"],
            "dom_summary": dom,
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals_before,
            "journals_after": journals_after,
            "browser_evidence_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "quarantine-search-host-tab-gone",
        "status": quarantined["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": quarantined["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(quarantined["ledger"]).resolve()),
    }


def quarantine_search_confirmed_click_tab_gone(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Quarantine a returned click after its live owner's exact task tab vanishes."""
    reason = "quarantined_confirmed_click_tab_gone"
    if not logs_stopped:
        raise ValueError("quarantine-search-confirmed-click-tab-gone requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("confirmed-click tab-gone evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("confirmed-click tab-gone evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("confirmed-click tab-gone evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match confirmed-click tab-gone target")

    host, tab, action, dom, local = (evidence.get(key) for key in (
        "browser_host", "browser_tab", "search_action", "dom_summary", "local_state",
    ))
    if (not isinstance(host, dict) or host.get("session_id") != owner
            or not isinstance(host.get("thread_id"), str) or not host["thread_id"].strip()
            or host.get("terminated") is not False):
        raise ValueError("confirmed-click tab-gone requires the live non-foreign owner host")
    if not isinstance(tab, dict) or tab.get("exists") is not False:
        raise ValueError("confirmed-click tab-gone requires the original task tab to be absent")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    missing_error = f"Error: No tab with id: {tab_id}."
    if tab.get("missing_error") != missing_error:
        raise ValueError("browser_tab.missing_error must preserve the exact No tab with id error")
    inventory = tab.get("inventory_tab_ids")
    if (not isinstance(inventory, list) or any(not isinstance(value, str) for value in inventory)
            or tab_id in inventory):
        raise ValueError("browser tab inventory must confirm the original task tab is absent")
    if (not isinstance(action, dict)
            or action.get("state") != "completed_unverifiable"
            or action.get("action_attempts") != 1
            or action.get("click_returned") is not True
            or action.get("original_module_instance_available") is not True
            or action.get("replay_authorized") is not False):
        raise ValueError("confirmed-click tab-gone requires one explicitly returned action and no replay")
    action_id = _nonempty_text(action.get("action_id"), "search_action.action_id")
    result_error = _nonempty_text(action.get("result_error"), "search_action.result_error")
    if (not isinstance(dom, dict)
            or dom.get("recorded_after_action") is not True
            or dom.get("action_id") != action_id
            or dom.get("input_value_after_action") != query
            or dom.get("selected_channels") != [channel]
            or dom.get("result_count") != 0
            or dom.get("visible_detail_links") != []
            or dom.get("empty_result_visible") is not True
            or dom.get("pagination_pending") is not False
            or dom.get("result_container_visible") is not True):
        raise ValueError("previously recorded after-DOM summary must exactly match the action, query, channel, and empty results")
    _nonempty_text(dom.get("page_url"), "dom_summary.page_url")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    page = urlsplit(dom["page_url"])
    if (page.scheme not in {"http", "https"} or page.username or page.password
            or not page.hostname or not (page.hostname.lower() == "xxmanmi.com"
                                         or page.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("DOM summary page_url must be a public xxmanmi.com page")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    )):
        raise ValueError("confirmed-click tab-gone evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first confirmed-click journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals_before or evidence.get("journals_after") != journals_after:
            raise RuntimeError("two confirmed-click journal snapshots are not identical")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_tab_exists": False, "original_module_instance_available": True,
            "action_state": "completed_unverifiable", "action_attempts": 1,
            "click_returned": True, "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"], "browser_tab_id": tab_id,
            "browser_tab_missing_error": missing_error,
            "browser_tab_inventory_ids": inventory,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"], "action_id": action_id,
            "result_error": result_error, "dom_summary": dom,
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals_before,
            "journals_after": journals_after, "browser_evidence_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "quarantine-search-confirmed-click-tab-gone",
        "status": quarantined["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": quarantined["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(quarantined["ledger"]).resolve()),
    }


def abandon_confirmed_click_no_outcome(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Record a known click whose post-click outcome is no longer available."""
    reason = "abandoned_confirmed_click_no_outcome"
    if not logs_stopped:
        raise ValueError("abandon-confirmed-click-no-outcome requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("confirmed-click no-outcome evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("confirmed-click no-outcome evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("confirmed-click no-outcome evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match confirmed-click no-outcome target")

    host, tab, action, receipt, local = (evidence.get(key) for key in (
        "browser_host", "browser_tab", "search_action", "action_result", "local_state",
    ))
    if (not isinstance(host, dict)
            or not isinstance(host.get("session_id"), str)
            or not host["session_id"].strip()
            or host.get("thread_id") != owner
            or host.get("terminated") is not False):
        raise ValueError("evidence must preserve the live browser session and matching owner thread")
    if not isinstance(tab, dict) or tab.get("exists") is not False:
        raise ValueError("confirmed-click no-outcome requires the original task tab to be absent")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    missing_error = f"Error: No tab with id: {tab_id}."
    if tab.get("missing_error") != missing_error:
        raise ValueError("browser_tab.missing_error must preserve the exact No tab with id error")
    inventory = tab.get("inventory_tab_ids")
    if (not isinstance(inventory, list) or any(not isinstance(value, str) for value in inventory)
            or tab_id in inventory):
        raise ValueError("browser tab inventory must confirm the original task tab is absent")
    if (not isinstance(action, dict)
            or action.get("task_id") != task_id
            or action.get("state") != "completed_outcome_unavailable"
            or action.get("action_attempts") != 1
            or action.get("click_returned") is not True
            or action.get("replay_authorized") is not False
            or action.get("action_outcome") != "unavailable"
            or action.get("after_dom_exportable") is not False
            or not isinstance(action.get("original_module_instance_available"), bool)):
        raise ValueError("confirmed-click evidence must prove one returned action with unavailable outcome and no replay")
    action_id = _nonempty_text(action.get("action_id"), "search_action.action_id")
    if (not isinstance(receipt, dict)
            or receipt.get("task_id") != task_id
            or receipt.get("actionId") != action_id
            or receipt.get("next_action") not in {"finalize_search_action", "inspect_search_action"}):
        raise ValueError("action result receipt must exactly match the claimed task and action")
    if "dom_summary" in evidence:
        raise ValueError("no-outcome route must not fabricate or require a historical DOM summary")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    )):
        raise ValueError("confirmed-click no-outcome evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first confirmed-click journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals_before or evidence.get("journals_after") != journals_after:
            raise RuntimeError("two confirmed-click journal snapshots are not identical")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_tab_exists": False, "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"], "browser_tab_id": tab_id,
            "browser_tab_missing_error": missing_error,
            "browser_tab_inventory_ids": inventory,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "action_state": "completed_outcome_unavailable", "action_attempts": 1,
            "click_returned": True, "action_id": action_id, "action_task_id": task_id,
            "action_outcome": "unavailable", "after_dom_exportable": False,
            "action_result_receipt": receipt,
            "original_module_instance_available": action.get("original_module_instance_available"),
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals_before,
            "journals_after": journals_after, "browser_evidence_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "abandon-confirmed-click-no-outcome",
        "status": abandoned["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": abandoned["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(abandoned["ledger"]).resolve()),
    }


def abandon_confirmed_click_stale_non_target_result(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Isolate a returned search click when the sole old non-target result remains unchanged."""
    reason = "abandoned_confirmed_click_stale_non_target_result"
    if not logs_stopped:
        raise ValueError("abandon-confirmed-click-stale-non-target-result requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("stale-result evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("stale-result evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("stale-result evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match stale-result target")

    host, tab, action, before, after, local = (evidence.get(key) for key in (
        "browser_host", "browser_tab", "search_action", "before_dom", "after_dom", "local_state",
    ))
    if (not isinstance(host, dict) or host.get("session_id") != owner
            or not isinstance(host.get("thread_id"), str) or not host["thread_id"].strip()
            or host.get("terminated") is not False):
        raise ValueError("stale-result route requires the live owner browser host")
    if (not isinstance(tab, dict) or tab.get("exists") is not True
            or tab.get("readable") is not True or tab.get("session_id") != owner
            or tab.get("owner_thread_id") != owner):
        raise ValueError("stale-result route requires the readable original owner tab")
    _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    if (not isinstance(action, dict) or action.get("task_id") != task_id
            or action.get("state") != "completed_outcome_unavailable"
            or action.get("action_attempts") != 1 or action.get("click_returned") is not True
            or action.get("action_outcome") != "unavailable"
            or action.get("after_dom_exportable") is not False
            or action.get("replay_authorized") is not False
            or not isinstance(action.get("original_module_instance_available"), bool)):
        raise ValueError("stale-result route requires exactly one returned click with unavailable outcome and no replay")
    action_id = _nonempty_text(action.get("action_id"), "search_action.action_id")

    def checked_snapshot(value: Any, label: str) -> list[dict[str, str]]:
        if (not isinstance(value, dict) or value.get("recorded") is not True
                or value.get("input_value") != query or value.get("selected_channels") != [channel]
                or value.get("result_count") != 1 or value.get("loading") is not False
                or value.get("pagination_pending") is not False):
            raise ValueError(f"{label} must prove the exact query/channel and one settled result")
        _nonempty_text(value.get("page_url"), f"{label}.page_url")
        _nonempty_text(value.get("observed_at"), f"{label}.observed_at")
        page = urlsplit(value["page_url"])
        if (page.scheme not in {"http", "https"} or not page.hostname
                or not (page.hostname.lower() == "xxmanmi.com"
                        or page.hostname.lower().endswith(".xxmanmi.com"))):
            raise ValueError(f"{label}.page_url must be a public xxmanmi.com page")
        results = value.get("results")
        if not isinstance(results, list) or len(results) != 1 or not isinstance(results[0], dict):
            raise ValueError(f"{label} must contain exactly one visible result")
        title = _nonempty_text(results[0].get("title"), f"{label}.results[0].title")
        url = _nonempty_text(results[0].get("url"), f"{label}.results[0].url")
        parsed = urlsplit(url)
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or not (parsed.hostname.lower() == "xxmanmi.com"
                        or parsed.hostname.lower().endswith(".xxmanmi.com"))
                or not re.fullmatch(r"/video2/\d+\.html", parsed.path)):
            raise ValueError(f"{label}.results[0].url must be a visible public detail link")
        if title.strip() == query.strip():
            raise ValueError("the stable result matches the target query; it is not a proven non-target result")
        return [{"title": title, "url": url}]

    before_results = checked_snapshot(before, "before_dom")
    after_results = checked_snapshot(after, "after_dom")
    if before_results != after_results:
        raise ValueError("before/after DOM must prove the same sole old non-target result")
    if after.get("action_id") != action_id:
        raise ValueError("after-DOM snapshot must identify the single returned action")
    if (not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    ))):
        raise ValueError("stale-result evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    evidence_path_local = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if evidence_path_local.exists() or commit_path.exists():
            raise RuntimeError("search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first stale-result journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals_before or evidence.get("journals_after") != journals_after:
            raise RuntimeError("two stale-result journal snapshots are not identical")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_host_session_id": owner, "browser_host_thread_id": host["thread_id"],
            "browser_tab_exists": True, "browser_tab_readable": True,
            "browser_tab_id": tab["tab_id"], "action_state": action["state"],
            "action_attempts": 1, "click_returned": True, "action_id": action_id,
            "action_outcome": "unavailable", "after_dom_exportable": False,
            "stable_non_target_result": after_results[0],
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals_before,
            "journals_after": journals_after, "browser_evidence_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "abandon-confirmed-click-stale-non-target-result",
        "status": abandoned["reason"], "task_id": task_id, "query": query,
        "channel": channel, "abandonment_id": abandoned["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(abandoned["ledger"]).resolve()),
    }


def abandon_confirmed_click_helper_gate_error(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Isolate a returned click rejected by the exact helper stale-result gate."""
    reason = "abandoned_confirmed_click_helper_gate_error"
    expected_error = (
        "Search results did not visibly change from the pre-action snapshot; "
        "inspect stale or identical results"
    )
    if not logs_stopped:
        raise ValueError("abandon-confirmed-click-helper-gate-error requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("helper-gate evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("helper-gate evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True, "helper_error": expected_error,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("helper-gate evidence does not match the exact claim/task/error")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match helper-gate target")

    host, tab, action, local = (evidence.get(key) for key in (
        "browser_host", "browser_tab", "search_action", "local_state",
    ))
    if (not isinstance(host, dict) or host.get("session_id") != owner
            or not isinstance(host.get("thread_id"), str) or not host["thread_id"].strip()
            or host.get("terminated") is not False):
        raise ValueError("helper-gate route requires the live owner browser session")
    if not isinstance(tab, dict) or tab.get("exists") is not False:
        raise ValueError("helper-gate route requires the original task tab to be absent")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    missing_error = f"No tab with id: {tab_id}."
    if tab.get("missing_error") != missing_error:
        raise ValueError("browser_tab.missing_error must preserve the exact No tab with id error")
    inventory = tab.get("inventory_tab_ids")
    if (not isinstance(inventory, list) or any(not isinstance(value, str) for value in inventory)
            or tab_id in inventory):
        raise ValueError("browser tab inventory must confirm the original task tab is absent")
    if (not isinstance(action, dict) or action.get("task_id") != task_id
            or action.get("state") != "completed_outcome_unavailable"
            or action.get("action_attempts") != 1 or action.get("click_returned") is not True
            or action.get("result_error") != expected_error
            or action.get("replay_authorized") is not False):
        raise ValueError("helper-gate route requires one returned click with the exact recorded gate error")
    if "action_id" in action or "action_result" in evidence or "action_result_receipt" in evidence:
        raise ValueError("helper-gate route does not accept a guessed or fabricated action receipt")
    if (not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "action_result_receipt_exists",
        "commit_receipt_exists", "search_journal_contains_task",
    ))):
        raise ValueError("helper-gate evidence must declare absent receipt/evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    evidence_path_local = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if evidence_path_local.exists() or commit_path.exists():
            raise RuntimeError("search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first helper-gate journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals_before or evidence.get("journals_after") != journals_after:
            raise RuntimeError("two helper-gate journal snapshots are not identical")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_host_session_id": owner, "browser_host_thread_id": host["thread_id"],
            "browser_tab_exists": False, "browser_tab_id": tab_id,
            "browser_tab_missing_error": missing_error,
            "browser_tab_inventory_ids": inventory,
            "action_state": action["state"], "action_attempts": 1,
            "click_returned": True, "helper_error": expected_error,
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals_before,
            "journals_after": journals_after, "browser_evidence_exists": False,
            "action_result_receipt_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False, "summary": evidence["summary"],
        }
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "abandon-confirmed-click-helper-gate-error",
        "status": abandoned["reason"], "task_id": task_id, "query": query,
        "channel": channel, "abandonment_id": abandoned["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(abandoned["ledger"]).resolve()),
    }


def cancel_pre_search_invalid_plan(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Terminally record an invalid plan before any search UI action occurs."""
    if not logs_stopped:
        raise ValueError("cancel-pre-search requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("pre-search cancellation evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("pre-search cancellation evidence must be a JSON object")
    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id, claim_hash = claim.get("task_id"), current.get("claim_sha256")
    reason = evidence.get("reason")
    if reason not in {"cancelled_pre_search_invalid_plan", "cancelled_pre_search_session_unavailable"}:
        raise ValueError("pre-search cancellation requires a supported terminal reason")
    expected = {
        "version": 1, "reason": reason, "owner": owner,
        "task_id": task_id, "query": query, "channel": channel,
        "claim_sha256": claim_hash, "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("pre-search cancellation evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match pre-search cancellation target")
    host, tab, action, local, invalid = (evidence.get(key) for key in
                                         ("browser_host", "browser_tab", "search_action", "local_state", "invalid_plan"))
    if not isinstance(host, dict) or host.get("session_id") != owner or host.get("terminated") is not False:
        raise ValueError("pre-search cancellation requires the live original browser host")
    if not isinstance(tab, dict) or not isinstance(tab.get("exists"), bool):
        raise ValueError("pre-search cancellation requires a verified original task-tab state")
    if tab["exists"] is False:
        _nonempty_text(tab.get("missing_reason"), "browser_tab.missing_reason")
    if reason == "cancelled_pre_search_session_unavailable" and host.get("session_reset") is not True:
        raise ValueError("session-unavailable cancellation requires verified session reset")
    if not isinstance(action, dict) or any((
        action.get("action_attempts") != 0,
        action.get("execute_receipt") is not False, action.get("inspect_receipt") is not False,
        action.get("finalize_receipt") is not False, action.get("replay_authorized") is not False,
    )):
        raise ValueError("pre-search cancellation requires proof that no UI action occurred")
    invalid_case = reason == "cancelled_pre_search_invalid_plan"
    if invalid_case and (action.get("state") != "not_started_invalid_plan"
                         or action.get("original_module_instance_available") is not True):
        raise ValueError("invalid-plan cancellation requires a live original action module")
    if not invalid_case and (tab["exists"] is not False
                             or action.get("state") != "not_started_session_unavailable"
                             or action.get("original_module_instance_available") is not False):
        raise ValueError("session-unavailable cancellation requires no task tab and no original action module")
    if invalid_case and (not isinstance(invalid, dict) or invalid.get("reason") != "channel_not_visible_in_current_search_surface"
                         or not isinstance(invalid.get("visible_channels"), list)
                         or not invalid["visible_channels"] or channel in invalid["visible_channels"]):
        raise ValueError("pre-search cancellation requires the observed channel mismatch")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
            "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task")):
        raise ValueError("pre-search cancellation evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if (work / f"{task_id}-evidence.json").exists() or (work / f"search-commit-{task_id}.json").exists():
            raise RuntimeError("saved evidence or commit exists; resume it instead of cancelling")
        journals = _journal_snapshot(work)
        if evidence.get("journals") != journals:
            raise ValueError("pre-search cancellation journal snapshot does not match worker logs")
        for item in journals:
            if item["name"] == "searches.ndjson" and any(
                    json.loads(line).get("task_id") == task_id
                    for line in (work / item["name"]).read_text(encoding="utf-8").splitlines() if line.strip()):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        if _journal_snapshot(work) != journals:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_tab_exists": tab["exists"],
            "original_module_instance_available": action["original_module_instance_available"],
            "action_state": action["state"], "action_attempts": 0,
            "replay_authorized": False, "logs_stopped": True, "journals": journals,
            "browser_evidence_exists": False, "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        if invalid_case:
            audit["invalid_plan"] = invalid
        cancelled = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {"ok": True, "command": "cancel-pre-search", "status": cancelled["reason"],
            "task_id": task_id, "query": query, "channel": channel,
            "abandonment_id": cancelled["abandonment_id"],
            "ledger": str(Path(cancelled["ledger"]).resolve()),
            "next_action": {"kind": "claim_other_search", "replay_abandoned": False}}


def quarantine_claim_task_scope_mismatch(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Manual-review terminal for an exact claim/taskbook query mismatch before UI action."""
    reason = "quarantined_claim_task_scope_mismatch"
    if not logs_stopped:
        raise ValueError("claim-task-scope-mismatch requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("claim/taskbook mismatch evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("claim/taskbook mismatch evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id, claim_hash = claim.get("task_id"), current.get("claim_sha256")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book_path = _taskbook_path(taskbooks, task_id)
    book_raw = book_path.read_bytes()
    try:
        book = json.loads(book_raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("search taskbook is incomplete or invalid") from exc
    if not isinstance(book, dict):
        raise ValueError("search taskbook must be a JSON object")
    scope = book.get("scope")
    task_queries = scope.get("queries") if isinstance(scope, dict) else None
    if (book.get("kind") != "search" or book.get("task_id") != task_id
            or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or not isinstance(scope, dict) or scope.get("channels") != [channel]
            or not isinstance(task_queries, list) or len(task_queries) != 1
            or not isinstance(task_queries[0], str) or not task_queries[0]):
        raise ValueError("search taskbook identity or channel does not match the effective claim")
    task_query = task_queries[0]
    if task_query == query:
        raise ValueError("claim and taskbook queries are equal; mismatch quarantine is not allowed")
    claim_codepoints = [ord(char) for char in query]
    taskbook_codepoints = [ord(char) for char in task_query]
    taskbook_hash = hashlib.sha256(book_raw).hexdigest()
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "taskbook_sha256": taskbook_hash, "claim_query": query,
        "taskbook_query": task_query, "claim_query_codepoints": claim_codepoints,
        "taskbook_query_codepoints": taskbook_codepoints, "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("claim/taskbook mismatch evidence does not match exact owner/task/query codepoints")
    action = evidence.get("search_action")
    if (not isinstance(action, dict)
            or action.get("state") != "not_started_scope_mismatch"
            or action.get("input_fill_attempts") != 0
            or action.get("click_attempts") != 0
            or action.get("replay_authorized") is not False):
        raise ValueError("claim/taskbook mismatch requires proof no input or search click was attempted")
    local = evidence.get("local_state")
    if (not isinstance(local, dict)
            or local.get("taskbook_scope_mismatch") is not True
            or any(local.get(key) is not False for key in (
                "browser_evidence_exists", "commit_receipt_exists",
                "search_journal_contains_task", "capture_lock_exists"))):
        raise ValueError("claim/taskbook mismatch requires absent evidence, commit, task journal, and writer lock")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        if hashlib.sha256(book_path.read_bytes()).hexdigest() != taskbook_hash:
            raise RuntimeError("search taskbook changed during mismatch verification")
        journals = _journal_snapshot(work)
        if evidence.get("journals_before") != journals or evidence.get("journals_after") != journals:
            raise ValueError("two mismatch journal snapshots must match current worker logs")
        for item in journals:
            if item["name"] == "searches.ndjson" and any(
                    json.loads(line).get("task_id") == task_id
                    for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                    if line.strip()):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "action_state": "not_started_scope_mismatch", "action_attempts": 0,
            "input_fill_attempts": 0, "click_attempts": 0,
            "claim_sha256": claim_hash, "taskbook_sha256": taskbook_hash,
            "claim_query": query, "taskbook_query": task_query,
            "claim_query_codepoints": claim_codepoints,
            "taskbook_query_codepoints": taskbook_codepoints,
            "manual_review_required": True, "replay_authorized": False,
            "logs_stopped": True, "journals_before": journals,
            "journals_after": journals_after, "browser_evidence_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "capture_lock_exists": False, "summary": evidence["summary"],
        }
        quarantined = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "claim-task-scope-mismatch",
        "status": quarantined["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": quarantined["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(quarantined["ledger"]).resolve()),
    }


def abandon_uninspectable_input_action(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS, authorized_parent_input: bool = False,
) -> dict[str, Any]:
    """Isolate a timed-out pre-submit fill when its original session owns the tab."""
    if not logs_stopped:
        raise ValueError("abandon-input requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("input-action abandonment evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("input-action abandonment evidence must be a JSON object")
    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id, claim_hash = claim.get("task_id"), current.get("claim_sha256")
    reason = evidence.get("reason")
    if reason not in {"abandoned_uninspectable_input_action", "abandoned_uninspectable_click_action", "abandoned_uninspectable_navigation_action"}:
        raise ValueError("input-action abandonment requires a supported terminal reason")
    expected = {
        "version": 1, "reason": reason, "owner": owner,
        "task_id": task_id, "query": query, "channel": channel,
        "claim_sha256": claim_hash, "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("input-action abandonment evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book_raw = _taskbook_path(taskbooks, task_id).read_bytes()
    book = json.loads(book_raw.decode("utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match input-action abandonment target")
    if (evidence.get("recovery_mode") == "readonly_non_search_page_after_unsubmitted_kernel_reset"
            and evidence.get("taskbook_sha256") != hashlib.sha256(book_raw).hexdigest()):
        raise ValueError("readable non-search input recovery requires the unchanged source-audited taskbook")
    host, tab, action, local = (evidence.get(key) for key in
                                ("browser_host", "browser_tab", "search_action", "local_state"))
    parent_proof = None
    if authorized_parent_input:
        if (evidence_path.is_symlink() or not evidence_path.resolve().is_relative_to(work.resolve())
                or len(raw) > 1024 * 1024):
            raise ValueError("parent input evidence must be bounded and inside the original owner work")
        if json.loads(Path(state_path).read_bytes()).get("collection_paused") is True:
            raise ValueError("collection paused; preserve original input action")
        from search_parent_host_quarantine import validate_unsubmitted_input
        parent_proof = validate_unsubmitted_input(evidence, owner, work, project_root=PROJECT_ROOT)
    elif not isinstance(host, dict) or host.get("session_id") != owner or host.get("terminated") is not False or host.get("session_reset") is not True:
        raise ValueError("input-action abandonment requires a live browser host and verified reset")
    parent_tab_missing = parent_proof is not None and parent_proof[2].get("browser_tab_exists") is False
    if (not isinstance(tab, dict) or tab.get("owned_by_original_session") is not True
            or (tab.get("exists") is not True and not parent_tab_missing)):
        raise ValueError("input-action abandonment requires the retained original-session tab")
    if not isinstance(action, dict):
        raise ValueError("input-action abandonment requires an action record")
    input_case = reason == "abandoned_uninspectable_input_action" and not any((
        action.get("state") != "input_fill_unknown_unsubmitted", action.get("action_attempts") != 1,
        action.get("input_fill_attempts") != 1, action.get("submit_attempts") != 0,
    ))
    click_case = reason == "abandoned_uninspectable_click_action" and not any((
        action.get("state") != "search_click_unknown", action.get("action_attempts") != 1,
        action.get("search_click_attempted") is not True, action.get("search_click_completed") != "unknown",
    ))
    navigation_case = reason == "abandoned_uninspectable_navigation_action" and not any((
        action.get("state") != "navigation_unknown", action.get("action_attempts") != 1,
        action.get("navigation_attempted") is not True, action.get("navigation_completed") != "unknown",
    ))
    if (not (input_case or click_case or navigation_case)
            or any(action.get(key) is not False for key in
                   ("execute_receipt", "inspect_receipt", "finalize_receipt", "replay_authorized"))
            or action.get("original_module_instance_available") is not False):
        raise ValueError("input-action abandonment requires one uninspectable action without replay")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
            "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task")):
        raise ValueError("input-action abandonment evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if (work / f"{task_id}-evidence.json").exists() or (work / f"search-commit-{task_id}.json").exists():
            raise RuntimeError("saved evidence or commit exists; resume it instead of abandoning")
        journals = _journal_snapshot(work)
        if evidence.get("journals") != journals:
            raise ValueError("input-action abandonment journal snapshot does not match worker logs")
        for item in journals:
            if item["name"] == "searches.ndjson" and any(
                    json.loads(line).get("task_id") == task_id
                    for line in (work / item["name"]).read_text(encoding="utf-8").splitlines() if line.strip()):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        if _journal_snapshot(work) != journals:
            raise RuntimeError("worker journals changed during stopped-log verification")
        audit = {
            "reason": reason, "browser_host_terminated": False,
            "browser_tab_exists": tab["exists"], "original_module_instance_available": False,
            "action_state": action["state"], "action_attempts": 1,
            "replay_authorized": False,
            "logs_stopped": True, "journals": journals, "browser_evidence_exists": False,
            "commit_receipt_exists": False, "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        if input_case:
            audit.update({"input_fill_attempts": 1, "submit_attempts": 0})
        elif click_case:
            audit.update({"search_click_attempted": True, "search_click_completed": "unknown"})
        else:
            audit.update({"navigation_attempted": True, "navigation_completed": "unknown"})
        if parent_proof is not None:
            config_path, config_sha256, parent_audit = parent_proof
            if (hashlib.sha256(config_path.read_bytes()).hexdigest() != config_sha256
                    or evidence_path.read_bytes() != raw
                    or json.loads(Path(state_path).read_bytes()).get("collection_paused") is True
                    or owner_record(state_path, owner)[0].get("active_batch") is not None
                    or (work / f"{task_id}-evidence.json").exists()
                    or (work / f"search-commit-{task_id}.json").exists()):
                raise RuntimeError("parent input binding, evidence, pause or saved state changed")
            audit.update(parent_audit)
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {"ok": True, "command": "abandon-input", "status": abandoned["reason"],
            "task_id": task_id, "query": query, "channel": channel,
            "abandonment_id": abandoned["abandonment_id"],
            "ledger": str(Path(abandoned["ledger"]).resolve()),
            "next_action": {"kind": "claim_other_search", "replay_abandoned": False}}


def abandon_foreign_host_click_unknown(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Manual-review terminal for one unknown click in a reset foreign host session."""
    reason = "abandoned_foreign_host_click_unknown"
    if not logs_stopped:
        raise ValueError("abandon-foreign-host-click-unknown requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("foreign-host click evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("foreign-host click evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id = claim.get("task_id")
    claim_hash = current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("foreign-host click evidence does not match the effective claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book = json.loads(_taskbook_path(taskbooks, task_id).read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("taskbook does not match foreign-host click target")

    host, tab, action, dom, local = (evidence.get(key) for key in (
        "browser_host", "browser_tab", "search_action", "dom_summary", "local_state",
    ))
    if (not isinstance(host, dict)
            or not isinstance(host.get("session_id"), str)
            or not host["session_id"].strip()
            or host.get("session_id") == owner
            or host.get("thread_id") != owner
            or host.get("terminated") is not False
            or host.get("session_reset") is not True):
        raise ValueError("evidence must preserve distinct live host session and owner thread IDs plus verified reset")
    if (not isinstance(tab, dict) or tab.get("exists") is not True
            or tab.get("readable") is not True
            or tab.get("session_id") != host["session_id"]
            or tab.get("owner_thread_id") != owner):
        raise ValueError("foreign-host click requires the readable original tab and its true host/thread IDs")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    page_url = _nonempty_text(tab.get("url"), "browser_tab.url")
    parsed_tab = urlsplit(page_url)
    if (parsed_tab.scheme not in {"http", "https"} or parsed_tab.username or parsed_tab.password
            or not parsed_tab.hostname or not (parsed_tab.hostname.lower() == "xxmanmi.com"
                                               or parsed_tab.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("browser_tab.url must be a public xxmanmi.com page")
    if (not isinstance(action, dict)
            or action.get("state") != "search_click_unknown"
            or action.get("action_attempts") != 1
            or action.get("search_click_attempted") is not True
            or action.get("search_click_completed") != "unknown"
            or action.get("original_module_instance_available") is not False
            or action.get("active_action_capability_available") is not False
            or action.get("action_id_recoverable") is not False
            or action.get("execute_receipt") is not False
            or action.get("inspect_receipt") is not False
            or action.get("finalize_receipt") is not False
            or action.get("replay_authorized") is not False):
        raise ValueError("evidence must prove exactly one unknown click with lost action state and no replay")
    if (not isinstance(dom, dict)
            or dom.get("input_value_after_action") != query
            or dom.get("selected_channels") != [channel]
            or not isinstance(dom.get("result_count"), int)
            or isinstance(dom.get("result_count"), bool)
            or not isinstance(dom.get("visible_detail_links"), list)
            or dom.get("result_count") != len(dom["visible_detail_links"])
            or not isinstance(dom.get("empty_result_visible"), bool)
            or not isinstance(dom.get("pagination_pending"), bool)
            or not isinstance(dom.get("result_container_visible"), bool)):
        raise ValueError("DOM summary must preserve the exact query/channel and readable public result state")
    _nonempty_text(dom.get("page_url"), "dom_summary.page_url")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    parsed_dom = urlsplit(dom["page_url"])
    if (parsed_dom.scheme not in {"http", "https"} or parsed_dom.username or parsed_dom.password
            or not parsed_dom.hostname or not (parsed_dom.hostname.lower() == "xxmanmi.com"
                                               or parsed_dom.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("dom_summary.page_url must be a public xxmanmi.com page")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "commit_receipt_exists", "search_journal_contains_task",
    )):
        raise ValueError("foreign-host click evidence must declare absent evidence/commit/journal state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists():
            raise RuntimeError("saved search evidence or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first foreign-host journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if (journals_after != journals_before
                or evidence.get("journals_after") != journals_after):
            raise RuntimeError("two foreign-host journal snapshots are not identical")
        audit = {
            "reason": reason,
            "browser_host_terminated": False,
            "browser_host_session_id": host["session_id"],
            "browser_host_thread_id": host["thread_id"],
            "browser_host_session_reset": True,
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "foreign_host_session_mismatch": True,
            "browser_tab_exists": True,
            "browser_tab_id": tab_id,
            "browser_tab_url": page_url,
            "browser_tab_session_id": tab["session_id"],
            "browser_tab_owner_thread_id": tab["owner_thread_id"],
            "browser_tab_readable": True,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "original_module_instance_available": False,
            "action_state": "search_click_unknown",
            "action_attempts": 1,
            "search_click_attempted": True,
            "search_click_completed": "unknown",
            "active_action_capability_available": False,
            "action_id_recoverable": False,
            "execute_receipt": False,
            "inspect_receipt": False,
            "finalize_receipt": False,
            "dom_summary": dom,
            "manual_review_required": True,
            "replay_authorized": False,
            "logs_stopped": True,
            "journals_before": journals_before,
            "journals_after": journals_after,
            "browser_evidence_exists": False,
            "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "summary": evidence["summary"],
        }
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "abandon-foreign-host-click-unknown",
        "status": abandoned["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": abandoned["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(abandoned["ledger"]).resolve()),
    }


def abandon_live_owner_click_unknown(
    state_path: Path, owner: str, query: str, channel: str, evidence_path: Path,
    logs_stopped: bool, *, discovery_root: Path = DEFAULT_DISCOVERY,
    taskbooks: Path = DEFAULT_TASKBOOKS,
) -> dict[str, Any]:
    """Manual-review terminal for one timed-out click call in its live owner session."""
    reason = "abandoned_live_owner_click_unknown"
    if not logs_stopped:
        raise ValueError("abandon-live-owner-click-unknown requires --logs-stopped")
    record, work = owner_record(state_path, owner)
    if record.get("active_batch") is not None:
        raise ValueError("owner has an active detail batch")
    raw = evidence_path.read_bytes()
    try:
        evidence = json.loads(raw.decode("utf-8-sig"))
    except (UnicodeError, ValueError) as exc:
        raise ValueError("live-owner click evidence is incomplete or invalid") from exc
    if not isinstance(evidence, dict):
        raise ValueError("live-owner click evidence must be a JSON object")

    current = shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get("claim") or {}
    task_id, claim_hash = claim.get("task_id"), current.get("claim_sha256")
    expected = {
        "version": 1, "reason": reason, "owner": owner, "task_id": task_id,
        "query": query, "channel": channel, "claim_sha256": claim_hash,
        "logs_stopped": True,
    }
    if any(evidence.get(key) != value for key, value in expected.items()):
        raise ValueError("live-owner click evidence does not match the exact claim/task")
    if claim.get("owner") != owner or current.get("reason") != "claimed":
        raise ValueError("search claim is not actively owned by --thread")
    book_path = _taskbook_path(taskbooks, task_id)
    book = json.loads(book_path.read_text(encoding="utf-8"))
    if (book.get("kind") != "search" or book.get("owner") != owner
            or Path(book.get("work", "")).resolve() != work
            or book.get("scope", {}).get("queries") != [query]
            or book.get("scope", {}).get("channels") != [channel]):
        raise ValueError("search taskbook does not match live-owner click target")

    host, tab, action, dom, local = (evidence.get(key) for key in (
        "browser_host", "browser_tab", "search_action", "dom_summary", "local_state",
    ))
    if (not isinstance(host, dict) or host.get("session_id") != owner
            or not isinstance(host.get("thread_id"), str) or not host["thread_id"].strip()
            or host.get("terminated") is not False
            or host.get("context_reset") is not True):
        raise ValueError("live-owner route requires owner-matched live host and verified execution-context reset")
    if (not isinstance(tab, dict) or tab.get("exists") is not True
            or tab.get("readable") is not True or tab.get("session_id") != owner
            or tab.get("owner_thread_id") != host["thread_id"]):
        raise ValueError("live-owner route requires the readable original task tab in the owner session")
    tab_id = _nonempty_text(tab.get("tab_id"), "browser_tab.tab_id")
    page_url = _nonempty_text(tab.get("url"), "browser_tab.url")
    parsed_tab = urlsplit(page_url)
    if (parsed_tab.scheme not in {"http", "https"} or parsed_tab.username or parsed_tab.password
            or not parsed_tab.hostname or not (parsed_tab.hostname.lower() == "xxmanmi.com"
                                               or parsed_tab.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("browser_tab.url must be a public xxmanmi.com page")
    if (not isinstance(action, dict)
            or action.get("state") != "search_click_unknown"
            or action.get("action_attempts") != 1
            or action.get("helper_call_attempts") != 1
            or action.get("search_click_attempted") is not True
            or action.get("search_click_completed") != "unknown"
            or action.get("input_value_before_call") != query
            or action.get("selected_channels_before_call") != [channel]
            or action.get("call_timed_out") is not True
            or action.get("context_reset") is not True
            or action.get("tool_error") != "js execution timed out; kernel reset, rerun your request"
            or action.get("original_module_instance_available") is not False
            or action.get("action_id_recoverable") is not False
            or any(action.get(key) is not False for key in (
                "execute_receipt", "inspect_receipt", "finalize_receipt", "replay_authorized",
            ))):
        raise ValueError("evidence must prove one timed-out search helper call with lost action state and no replay")
    if any(key in action for key in ("action_id", "action_result", "receipt")):
        raise ValueError("live-owner unknown-click evidence cannot contain an action ID or receipt")
    if (not isinstance(dom, dict) or dom.get("input_value") != query
            or dom.get("selected_channels") != [channel]
            or dom.get("page_url") != page_url):
        raise ValueError("readable public DOM must preserve the exact query, channel, and task URL")
    if (not isinstance(dom.get("result_count"), int) or isinstance(dom.get("result_count"), bool)
            or not isinstance(dom.get("visible_detail_links"), list)
            or dom["result_count"] != len(dom["visible_detail_links"])):
        raise ValueError("DOM summary must preserve the visible detail-link count")
    _nonempty_text(dom.get("observed_at"), "dom_summary.observed_at")
    parsed_dom = urlsplit(page_url)
    if (parsed_dom.scheme not in {"http", "https"} or parsed_dom.username or parsed_dom.password
            or not parsed_dom.hostname or not (parsed_dom.hostname.lower() == "xxmanmi.com"
                                               or parsed_dom.hostname.lower().endswith(".xxmanmi.com"))):
        raise ValueError("DOM snapshot URL must be a public xxmanmi.com page")
    if (not isinstance(dom.get("empty_result_visible"), bool)
            or not isinstance(dom.get("pagination_pending"), bool)
            or dom.get("result_container_visible") is not True):
        raise ValueError("DOM summary must preserve bounded public result-state indicators")
    if not isinstance(local, dict) or any(local.get(key) is not False for key in (
        "browser_evidence_exists", "action_result_receipt_exists", "commit_receipt_exists",
        "search_journal_contains_task", "capture_lock_exists",
    )):
        raise ValueError("evidence must declare absent action/evidence/commit/journal/lock state")
    for group, label in ((host, "browser_host"), (tab, "browser_tab")):
        _nonempty_text(group.get("checked_at"), f"{label}.checked_at")
        _nonempty_text(group.get("method"), f"{label}.method")
    _nonempty_text(evidence.get("summary"), "summary")

    candidate_path = work / f"{task_id}-evidence.json"
    commit_path = work / f"search-commit-{task_id}.json"
    action_receipt = work / f"search-action-{task_id}.json"
    with work_lock(work):
        if (work / ".capture-helper.lock").exists():
            raise RuntimeError("capture writer is still active or requires review")
        if candidate_path.exists() or commit_path.exists() or action_receipt.exists():
            raise RuntimeError("search evidence, action receipt, or commit exists; resume its official path")
        journals_before = _journal_snapshot(work)
        if evidence.get("journals_before") != journals_before:
            raise ValueError("first live-owner journal snapshot does not match worker logs")
        for item in journals_before:
            if item["name"] == "searches.ndjson" and any(
                json.loads(line).get("task_id") == task_id
                for line in (work / item["name"]).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ):
                raise RuntimeError("search journal already contains this task")
        time.sleep(0.2)
        journals_after = _journal_snapshot(work)
        if journals_after != journals_before or evidence.get("journals_after") != journals_after:
            raise RuntimeError("two live-owner journal snapshots are not identical")
        audit = {
            "reason": reason,
            "browser_host_terminated": False,
            "browser_host_session_id": owner,
            "browser_host_thread_id": host["thread_id"],
            "browser_host_context_reset": True,
            "browser_host_checked_at": host["checked_at"],
            "browser_host_check_method": host["method"],
            "browser_tab_exists": True,
            "browser_tab_id": tab_id,
            "browser_tab_url": page_url,
            "browser_tab_session_id": owner,
            "browser_tab_owner_thread_id": host["thread_id"],
            "browser_tab_readable": True,
            "browser_tab_checked_at": tab["checked_at"],
            "browser_tab_check_method": tab["method"],
            "action_state": "search_click_unknown",
            "action_attempts": 1,
            "helper_call_attempts": 1,
            "search_click_attempted": True,
            "search_click_completed": "unknown",
            "helper_call_timed_out": True,
            "action_context_reset": True,
            "action_tool_error": action["tool_error"],
            "original_module_instance_available": False,
            "action_id_recoverable": False,
            "execute_receipt": False,
            "inspect_receipt": False,
            "finalize_receipt": False,
            "dom_summary": dom,
            "manual_review_required": True,
            "replay_authorized": False,
            "logs_stopped": True,
            "journals_before": journals_before,
            "journals_after": journals_after,
            "browser_evidence_exists": False,
            "action_result_receipt_exists": False,
            "commit_receipt_exists": False,
            "search_journal_contains_task": False,
            "capture_lock_exists": False,
            "summary": evidence["summary"],
        }
        abandoned = shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit,
        )
    return {
        "ok": True, "command": "abandon-live-owner-click-unknown",
        "status": abandoned["reason"], "task_id": task_id,
        "query": query, "channel": channel,
        "abandonment_id": abandoned["abandonment_id"],
        "manual_review_required": True, "replay_authorized": False,
        "ledger": str(Path(abandoned["ledger"]).resolve()),
    }


def _load_evidence(path: Path) -> tuple[dict[str, Any], str]:
    raw = path.read_bytes()
    value = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("browser evidence must be a JSON object")
    return value, hashlib.sha256(raw).hexdigest()


def _validate_search_evidence(evidence: dict[str, Any], owner: str) -> tuple[dict[str, Any], list[dict[str, str]]]:
    task_id = evidence.get("task_id")
    query = evidence.get("query")
    channel = evidence.get("channel")
    collected = evidence.get("collected_at", evidence.get("collectedAt"))
    if any(not isinstance(value, str) or not value.strip()
           for value in (task_id, query, channel, collected)):
        raise ValueError("evidence requires task_id, query, channel and collected_at")
    if evidence.get("owner") != owner:
        raise ValueError("evidence owner does not match --thread")
    if evidence.get("input_value_after_action") != query:
        raise ValueError("evidence input value does not exactly match query")
    if evidence.get("selected_channels") != [channel]:
        raise ValueError("evidence must confirm exactly the claimed channel")
    links = evidence.get("visible_detail_links", evidence.get("links"))
    if not isinstance(links, list) or len(links) > MAX_SEARCH_RESULTS:
        raise ValueError("evidence links must be a bounded array")
    if evidence.get("result_count") != len(links):
        raise ValueError("evidence result_count must match links")
    cleaned = []
    seen = set()
    for item in links:
        if not isinstance(item, dict):
            raise ValueError("each observed link must be an object")
        url = item.get("url")
        title = item.get("title")
        status = item.get("status", "")
        if (not isinstance(url, str) or not isinstance(title, str) or not title.strip()
                or not isinstance(status, str)):
            raise ValueError("each observed link requires URL, title and string status")
        canonical = coordctl._page_url(url)
        if canonical != url or dbctl.canonical_source_name(url) != "小小漫迷":
            raise ValueError("search evidence contains a noncanonical/non-xxmanmi detail URL")
        if url in seen:
            raise ValueError("search evidence contains a duplicate detail URL")
        seen.add(url)
        cleaned.append({"url": url, "title": title, "status": status})
    record = {
        "query": query, "source": channel,
        "category": evidence.get("category") or "",
        "collectedAt": collected, "links": cleaned,
    }
    return record, cleaned


def _ensure_search_journal(work: Path, record: dict[str, Any]) -> bool:
    path = work / "searches.ndjson"
    existing = []
    if path.exists():
        data = path.read_bytes()
        if data and not data.endswith(b"\n"):
            raise RuntimeError("searches.ndjson has a partial tail")
        for raw in data.splitlines():
            if raw.strip():
                existing.append(json.loads(raw.decode("utf-8")))
    exact = [item for item in existing if item == record]
    if exact:
        if len(exact) != 1:
            raise RuntimeError("the exact search observation appears more than once")
        return False
    if any(item.get("query") == record["query"] and item.get("source") == record["source"]
           for item in existing if isinstance(item, dict)):
        raise RuntimeError("a different observation already exists for this query/channel")
    with path.open("ab") as stream:
        stream.write((compact_json(record) + "\n").encode("utf-8"))
        stream.flush()
        os.fsync(stream.fileno())
    return True


def _ensure_search_plan(work: Path, record: dict[str, Any]) -> None:
    path = work / "search-plan.json"
    plans = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    if not isinstance(plans, list):
        raise RuntimeError("search-plan.json must be an array")
    key = (record["source"], record["query"])
    if not any((item.get("source", "量子"), item.get("query")) == key for item in plans):
        plans.append({"source": record["source"], "query": record["query"],
                      "category": record.get("category", "")})
        atomic_json(path, plans)


def _requested_query_for_book(book: dict[str, Any]) -> str | None:
    request = book.get("request_scope")
    if request is not None:
        if not isinstance(request, dict) or request.get("query") != book["scope"]["queries"][0]:
            raise ValueError("requested search scope conflicts with its taskbook")
        path = Path(request.get("evidence", ""))
        if hashlib.sha256(path.read_bytes()).hexdigest() != request.get("sha256"):
            raise ValueError("user request evidence changed after search admission")
        paths = [path]
    else:
        # Compatibility for requests admitted before the scope marker existed.
        paths = [Path(value) for value in book.get("inputs", [])
                 if isinstance(value, str) and "user-priority" in Path(value).parts
                 and Path(value).suffix == ".json"]
    for path in paths:
        evidence = json.loads(path.read_text(encoding="utf-8-sig"))
        if evidence.get("kind") != "user_requested_titles":
            continue
        query = book["scope"]["queries"][0]
        if (evidence.get("user_authorized") is not True
                or not any(isinstance(item, dict) and item.get("title") == query
                           for item in evidence.get("items", []))):
            raise ValueError("taskbook query is not in its authorized request evidence")
        return query
    if request is not None:
        raise ValueError("requested search scope has no valid request evidence")
    return None


def _requested_title_matches(title: str, query: str, category: str = "", *,
                             exact: bool = False) -> bool:
    # Keep the full search observation, but do not enqueue unrelated fuzzy hits
    # or commentary videos as the requested program. Unknown aliases need review.
    if re.search(r"电影解说|影视解说|\[解说\]|预告片", title) and not re.search(r"解说|预告", query):
        return False
    if category == "动漫" and re.search(r"短剧|真人版|电视剧版", title) and not re.search(r"短剧|真人版|电视剧版", query):
        return False
    if exact:
        def normalize_exact(value: str) -> str:
            # Ignore formatting punctuation, retaining symbols such as '+' and
            # all words/numbers that distinguish subtitles, versions and parts.
            return "".join(char for char in unicodedata.normalize("NFKC", value).casefold()
                           if not char.isspace() and not unicodedata.category(char).startswith("P"))
        expected = normalize_exact(query)
        return bool(expected) and normalize_exact(title) == expected
    normalize = lambda value: re.sub(r"[\W_]+", "", value, flags=re.UNICODE).casefold()
    actual = normalize(title)
    for alias in query.split("/"):
        expected = normalize(alias)
        if expected and (actual == expected or len(expected) > 2 and expected in actual):
            return True
    return False


def commit_search(state_path: Path, owner: str, evidence_path: Path,
                  *, discovery_root: Path = DEFAULT_DISCOVERY,
                  taskbooks: Path = DEFAULT_TASKBOOKS) -> dict[str, Any]:
    _record, work = owner_record(state_path, owner)
    evidence, evidence_hash = _load_evidence(evidence_path)
    search_record, links = _validate_search_evidence(evidence, owner)
    task_id = evidence["task_id"]
    book_path = _taskbook_path(taskbooks, task_id)
    book = json.loads(book_path.read_text(encoding="utf-8"))
    if (book.get("owner") != owner or book.get("scope", {}).get("queries") != [search_record["query"]]
            or book.get("scope", {}).get("channels") != [search_record["source"]]
            or Path(book.get("work", "")).resolve() != work):
        raise ValueError("browser evidence does not match its taskbook")
    claim = shared_discovery.inspect_claim(
        discovery_root, search_record["query"], search_record["source"],
    )
    if (not claim.get("ok") or claim.get("reason") != "claimed"
            or claim.get("claim", {}).get("owner") != owner
            or claim.get("claim", {}).get("task_id") != task_id):
        raise ValueError("browser evidence does not match the effective search claim")
    receipt_path = work / f"search-commit-{task_id}.json"
    import production_live
    production_live.mark_dirty(owner, receipt=receipt_path)
    with work_lock(work):
        if receipt_path.exists():
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            if receipt.get("evidence_sha256") != evidence_hash:
                raise RuntimeError("search commit evidence changed after preparation")
            if receipt.get("phase") == "complete":
                return {"ok": True, "command": "commit-search", "status": "already_complete",
                        "receipt": str(receipt_path), "enqueued": receipt.get("enqueued", 0)}
        else:
            urls = [item["url"] for item in links]
            database_rows = dbctl.lookup(urls) if urls else []
            by_url = {item["url"]: item for item in database_rows}
            if set(by_url) != set(urls):
                raise RuntimeError("database lookup did not return every observed URL")
            state = read_state(state_path)
            known = state.get("items", {})
            eligible = []
            requested_query = _requested_query_for_book(book)
            excluded_out_of_scope = []
            for item in links:
                if requested_query is not None and not _requested_title_matches(
                        item["title"], requested_query, book["scope"].get("category", "")):
                    excluded_out_of_scope.append(item["url"])
                    continue
                row = by_url[item["url"]]
                if row.get("exists") is not True and item["url"] not in known:
                    eligible.append({
                        **item, "source": search_record["source"],
                        "category": search_record["category"],
                        "query": search_record["query"],
                    })
            receipt = {
                "version": 1, "phase": "prepared", "task_id": task_id,
                "owner": owner, "evidence": str(evidence_path.resolve()),
                "evidence_sha256": evidence_hash, "claim_sha256": claim.get("claim_sha256"),
                "search_record": search_record, "database_precheck": database_rows,
                "coordination_precheck": {
                    item["url"]: known.get(item["url"], {}).get("state", "absent") for item in links
                },
                "eligible_items": eligible, "prepared_at": utc_now(),
                "excluded_out_of_scope": excluded_out_of_scope,
            }
            atomic_json(receipt_path, receipt)
        appended = _ensure_search_journal(work, receipt["search_record"])
        _ensure_search_plan(work, receipt["search_record"])
        synced = dbctl.sync(work, include_status=False)
        eligible = receipt["eligible_items"]
        enqueued = coordctl.enqueue_items(state_path, owner, eligible) if eligible else {
            "ok": True, "command": "enqueue", "added": 0, "duplicates": 0,
        }
        receipt.update({
            "phase": "complete", "completed_at": utc_now(), "journal_appended": appended,
            "sync_imported_records": synced.get("imported_records", 0),
            "enqueued": enqueued.get("added", 0), "enqueue_duplicates": enqueued.get("duplicates", 0),
        })
        atomic_json(receipt_path, receipt)
        return {
            "ok": True, "command": "commit-search", "status": "complete",
            "result_count": len(receipt["search_record"]["links"]),
            "enqueued": receipt["enqueued"], "duplicates": receipt["enqueue_duplicates"],
            "excluded_out_of_scope": len(receipt.get("excluded_out_of_scope", [])),
            "receipt": str(receipt_path),
        }


def _captured_results(work: Path, expected_urls: list[str]) -> tuple[list[dict[str, str]], int]:
    path = work / "details.ndjson"
    if not path.exists():
        raise ValueError("details.ndjson does not exist")
    size = path.stat().st_size
    lines, partial = batchctl.read_tail(path, len(expected_urls), size)
    if partial or len(lines) != len(expected_urls):
        raise ValueError("the complete details tail does not match the active batch size")
    records = []
    resources = 0
    for _offset, raw in lines:
        record = json.loads(raw.decode("utf-8"))
        document = dbctl.normalize("details", record)
        if not document["programs"][0]["captured"] or not document["resources"]:
            raise ValueError("active-batch detail evidence is not a complete captured observation")
        records.append(record)
        resources += len(document["resources"])
    actual = [coordctl._page_url(record["url"]) for record in records]
    if len(set(actual)) != len(actual) or set(actual) != set(expected_urls):
        raise ValueError("the details journal tail does not exactly match active batch URLs")
    if path.stat().st_size != size:
        raise RuntimeError("details.ndjson changed while preparing ready results")
    ordered = [{"url": url, "outcome": "captured"} for url in expected_urls]
    return ordered, resources


def _cover_targets(source_urls: list[str]) -> list[dict[str, Any]]:
    """Resolve exact captured programs without returning any media address."""
    if (not isinstance(source_urls, list) or not source_urls
            or len(source_urls) > coordctl.MAX_CLAIM_SIZE
            or any(not isinstance(url, str) or not url for url in source_urls)
            or len(set(source_urls)) != len(source_urls)):
        raise ValueError("cover targets require unique captured source URLs from one batch")
    payload = dbctl.payload_sql(source_urls)
    rows = dbctl.sql_json("""WITH requested AS (
      SELECT value AS source_url, ordinality
      FROM jsonb_array_elements_text(PAYLOAD) WITH ORDINALITY
    ), resource_counts AS (
      SELECT program_id, count(*)::int AS resource_count
      FROM resources GROUP BY program_id
    )
    SELECT COALESCE(json_agg(json_build_object(
      'source_url', requested.source_url,
      'program_id', programs.id,
      'title', programs.title,
      'captured', programs.captured_at IS NOT NULL,
      'resource_count', COALESCE(resource_counts.resource_count, 0)
    ) ORDER BY requested.ordinality), '[]'::json)
    FROM requested
    LEFT JOIN programs ON programs.source_url=requested.source_url
    LEFT JOIN resource_counts ON resource_counts.program_id=programs.id;
    """.replace("PAYLOAD", payload))
    if not isinstance(rows, list) or len(rows) != len(source_urls):
        raise RuntimeError("cover target lookup did not return every captured URL")
    targets = []
    program_ids = set()
    for expected_url, row in zip(source_urls, rows):
        if (not isinstance(row, dict) or row.get("source_url") != expected_url
                or type(row.get("program_id")) is not int or row["program_id"] <= 0
                or row["program_id"] in program_ids or row.get("captured") is not True
                or not isinstance(row.get("title"), str) or not row["title"].strip()
                or type(row.get("resource_count")) is not int
                or row["resource_count"] <= 0):
            raise RuntimeError("cover target is missing, uncaptured, duplicated, or has no resources")
        program_ids.add(row["program_id"])
        targets.append({
            "source_url": expected_url,
            "program_id": row["program_id"],
            "title": row["title"].strip(),
            "resource_count": row["resource_count"],
        })
    return targets


def _hidden_subprocess_options() -> dict[str, Any]:
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def _artifact(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    digest = value.get("sha256")
    if (not isinstance(digest, str) or len(digest) != 64
            or any(character not in "0123456789abcdefABCDEF" for character in digest)):
        return None
    cleaned: dict[str, Any] = {"sha256": digest.lower()}
    for key in ("path", "bytes", "width", "height", "score"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            cleaned[key] = item
        elif type(item) in (int, float) and item >= 0:
            cleaned[key] = item
    return cleaned


def _selected_cover(value: Any, target: dict[str, Any]) -> dict[str, Any] | None:
    if (not isinstance(value, dict) or value.get("source_url") != target["source_url"]
            or value.get("program_id") != target["program_id"]
            or value.get("title") != target["title"]
            or value.get("resource_count") != target["resource_count"]
            or value.get("status") != "selected"
            or value.get("selection") not in {"existing", "generated"}):
        return None
    artifact = _artifact(value.get("artifact"))
    if artifact is None:
        return None
    return {**target, "status": "selected", "selection": value["selection"],
            "artifact": artifact}


def _cover_failure(target: dict[str, Any], status: str = "error",
                   message: str = "cover selection did not complete") -> dict[str, Any]:
    return {**target, "status": status if status in {"unavailable", "error"} else "error",
            "error": str(message).strip()[:500] or "cover selection did not complete"}


def _preserve_bound_cover_receipt(work: Path, batch_id: str, receipt_path: Path,
                                  fresh: dict[str, Any]) -> str | None:
    """Keep authenticated first-selection evidence after revalidating its images.

    Older releases overwrote generated with existing on reentry. Recover only
    that bounded flag transition when the exact original SHA-256 authenticates
    the reconstructed bytes; never replace verification or accept changed images.
    """
    verification_path = work / f"{batch_id}-verification.json"
    if not verification_path.exists():
        return None
    if verification_path.is_symlink() or not receipt_path.is_file():
        raise ValueError("bound cover evidence requires regular receipt files")
    verification = json.loads(verification_path.read_text(encoding="utf-8"))
    digest = verification.get("cover_receipt_sha256")
    if (verification.get("batch_id") != batch_id
            or verification.get("verifier") not in {"workflowctl complete-detail", "workflowctl complete-captured via batchctl finish"}
            or Path(verification.get("work", "")).resolve() != work.resolve()
            or Path(verification.get("cover_receipt", "")).resolve() != receipt_path.resolve()
            or not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest) is None):
        raise ValueError("existing verification has invalid bound cover evidence")
    observed_bytes = receipt_path.read_bytes()
    observed = json.loads(observed_bytes)
    if (not isinstance(observed, dict) or set(observed) != set(fresh)
            or any(observed.get(key) != fresh[key] for key in
                   ("version", "batch_id", "complete", "cover_checked_items"))
            or not isinstance(observed.get("items"), list)
            or len(observed["items"]) != len(fresh["items"])
            or not 0 < len(observed["items"]) <= coordctl.MAX_CLAIM_SIZE):
        raise ValueError("existing cover receipt conflicts with current batch evidence")
    for previous, current in zip(observed["items"], fresh["items"]):
        target = {key: current[key] for key in
                  ("source_url", "program_id", "title", "resource_count")}
        if (_selected_cover(previous, target) != previous
                or {key: value for key, value in previous.items() if key != "selection"}
                != {key: value for key, value in current.items() if key != "selection"}):
            raise ValueError("existing cover receipt conflicts with current batch evidence")
    observed_digest = hashlib.sha256(observed_bytes).hexdigest()
    if observed_digest == digest:
        return digest
    choices = [(item["selection"],) if item["selection"] == "generated"
               else ("existing", "generated") for item in observed["items"]]
    recovered = None
    for flags in itertools.product(*choices):
        candidate = {**observed, "items": [{**item, "selection": flag}
                     for item, flag in zip(observed["items"], flags)]}
        data = (json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True)
                + "\n").encode("utf-8")
        if hashlib.sha256(data).hexdigest() == digest:
            recovered = candidate
            break
    if recovered is None:
        raise ValueError("existing cover receipt hash cannot be authenticated")
    recovery = {
        "version": 1, "batch_id": batch_id,
        "method": "original verification SHA-256 authenticated selection-flag recovery after fresh cover validation",
        "observed_sha256": observed_digest, "observed_receipt": observed,
        "restored_sha256": digest, "fresh_revalidation": fresh,
        "verification_unchanged": True,
    }
    # Keep the digest in the document: owner work paths are already long on Windows.
    recovery_path = work / f"{batch_id}-cover-recovery.json"
    if recovery_path.exists():
        if json.loads(recovery_path.read_text(encoding="utf-8")) != recovery:
            raise ValueError("existing cover recovery evidence conflicts")
    else:
        atomic_json(recovery_path, recovery)
    atomic_json(receipt_path, recovered)
    if hashlib.sha256(receipt_path.read_bytes()).hexdigest() != digest:
        raise RuntimeError("authenticated cover receipt restoration failed")
    return digest


def _ensure_batch_covers(work: Path, batch_id: str,
                         source_urls: list[str]) -> dict[str, Any]:
    """Run the central cover worker after DB finalization and require exact artifacts."""
    targets = _cover_targets(source_urls)
    receipt_path = work / f"{batch_id}-covers.json"
    request_path = work / f"{batch_id}-cover-items.json"
    if receipt_path.is_symlink():
        raise ValueError("cover receipt links require separate review")
    # Every finalization attempt asks coverctl to revalidate every target. Its
    # existing-selection path is cheap, and also proves the artifact still exists.
    pending = targets
    selected: dict[str, dict[str, Any]] = {}
    failures: dict[str, dict[str, Any]] = {}
    cover_pending: dict[str, dict[str, Any]] = {}
    if receipt_path.is_file():
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
        previous_items = previous.get("items", []) if previous.get("batch_id") == batch_id else []
        for target in targets:
            match = next((item for item in previous_items
                          if item.get("collection_cover", {}).get("cover_pending") is True
                          and all(item.get(key) == value for key, value in target.items())), None)
            if match is not None:
                if str(COVERCTL.parent) not in sys.path:
                    sys.path.insert(0, str(COVERCTL.parent))
                import collection_cover_policy
                collection_cover_policy.verify_pending(match, read_state(DEFAULT_STATE))
                cover_pending[target["source_url"]] = match
        pending = [target for target in targets if target["source_url"] not in cover_pending]
    atomic_json(request_path, [{key: target[key] for key in
                                ("source_url", "program_id", "title")}
                               for target in pending])
    command = [
        sys.executable, "-X", "utf8", "-B", str(COVERCTL), "ensure-batch",
        "--request-id", batch_id, "--items", str(request_path.resolve()),
    ]
    try:
        process = subprocess.run(
            command, capture_output=True, text=True, encoding="utf-8",
            timeout=900, **_hidden_subprocess_options(),
        ) if pending else None
    except (OSError, subprocess.TimeoutExpired) as exc:
        process = None
        failure_message = f"coverctl unavailable: {type(exc).__name__}"
    else:
        failure_message = f"coverctl exited with code {process.returncode}" if process else "retained pending covers"
    payload = None
    if process is not None and process.stdout:
        try:
            payload = json.loads(process.stdout)
        except (TypeError, ValueError):
            failure_message = "coverctl returned invalid JSON"
    response_by_id: dict[int, dict[str, Any]] = {}
    pending_ids = {target["program_id"] for target in pending}
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        malformed = False
        for item in payload["items"]:
            program_id = item.get("program_id") if isinstance(item, dict) else None
            if (type(program_id) is not int or program_id in response_by_id
                    or program_id not in pending_ids):
                malformed = True
                break
            response_by_id[program_id] = item
        if malformed:
            response_by_id = {}
            failure_message = "coverctl returned duplicate or foreign items"
    elif process is not None:
        failure_message = "coverctl response has no item results"

    for target in pending:
        response = response_by_id.get(target["program_id"])
        candidate = None
        if isinstance(response, dict):
            candidate = _selected_cover({**target, **response}, target)
        if candidate is not None:
            selected[target["source_url"]] = candidate
            continue
        status = response.get("status") if isinstance(response, dict) else "error"
        message = ((response.get("reason") or response.get("error"))
                   if isinstance(response, dict) else None) or failure_message
        failures[target["source_url"]] = _cover_failure(target, status, message)
        if isinstance(response, dict) and response.get("collection_cover", {}).get("cover_pending") is True:
            from importlib import import_module
            if str(COVERCTL.parent) not in sys.path:
                sys.path.insert(0, str(COVERCTL.parent))
            policy = import_module("collection_cover_policy")
            item = {**target, **failures[target["source_url"]],
                    "collection_cover": response["collection_cover"]}
            policy.verify_pending(item, read_state(DEFAULT_STATE))
            cover_pending[target["source_url"]] = item

    ordered = [selected.get(target["source_url"])
               or cover_pending.get(target["source_url"])
               or failures.get(target["source_url"])
               or _cover_failure(target) for target in targets]
    command_ok = (process is not None and process.returncode == 0
                  and isinstance(payload, dict) and payload.get("ok") is True)
    complete = len(selected) == len(targets) and command_ok
    settled = (complete or (not pending and len(cover_pending) == len(targets))
               or (process is not None and process.returncode in (0, 3)
               and isinstance(payload, dict) and len(selected) + len(cover_pending) == len(targets)))
    receipt = {"version": 1, "batch_id": batch_id, "complete": complete,
               "cover_checked_items": len(selected), "items": ordered}
    if cover_pending:
        receipt.update(settled=settled, cover_pending_items=list(cover_pending))
    if not settled:
        # Keep a previously bound receipt intact while recording failed revalidation.
        failure_path = (work / f"{batch_id}-cover-revalidation.json"
                        if (work / f"{batch_id}-verification.json").exists() else receipt_path)
        atomic_json(failure_path, receipt)
        import recoveryctl
        recoveryctl.guard(caller_thread=os.environ.get("CODEX_THREAD_ID"))
        raise RuntimeError(
            f"cover gate selected {len(selected)}/{len(targets)} programs; "
            f"batch remains ready; receipt={receipt_path}"
        )
    if cover_pending:
        bound_path = work / f"{batch_id}-verification.json"
        if bound_path.exists():
            bound = json.loads(bound_path.read_text(encoding="utf-8"))
            if (json.loads(receipt_path.read_text(encoding="utf-8")) != receipt
                    or hashlib.sha256(receipt_path.read_bytes()).hexdigest() != bound.get("cover_receipt_sha256")
                    or bound.get("cover_pending_items") != list(cover_pending)):
                raise ValueError("previously bound cover evidence cannot change during pending settlement")
        atomic_json(receipt_path, receipt)
        import recoveryctl
        recoveryctl.guard(caller_thread=os.environ.get("CODEX_THREAD_ID"))
        return {"cover_ok": False, "cover_settled": True,
                "cover_checked_items": len(selected), "cover_pending_items": list(cover_pending),
                "cover_receipt": str(receipt_path.resolve()),
                "cover_receipt_sha256": hashlib.sha256(receipt_path.read_bytes()).hexdigest()}
    receipt_hash = _preserve_bound_cover_receipt(work, batch_id, receipt_path, receipt)
    if receipt_hash is None:
        atomic_json(receipt_path, receipt)
        receipt_hash = hashlib.sha256(receipt_path.read_bytes()).hexdigest()
    return {"cover_ok": True, "cover_checked_items": len(targets),
            "cover_receipt": str(receipt_path.resolve()),
            "cover_receipt_sha256": receipt_hash}


def complete_captured(state_path: Path, owner: str, batch_id: str,
                      logs_stopped: bool) -> dict[str, Any]:
    if logs_stopped is not True:
        raise ValueError("complete-captured requires --logs-stopped")
    _owner, work = owner_record(state_path, owner)
    with work_lock(work):
        state = read_state(state_path)
        batch = state.get("batches", {}).get(batch_id)
        if not batch or batch.get("thread") != owner:
            raise ValueError("batch is not owned by this thread")
        import production_live
        production_live.mark_dirty(owner, batch=batch_id)
        if batch.get("state") == "acknowledged":
            return {"ok": True, "command": "complete-captured", "status": "already_complete",
                    "batch": batch_id}
        if state["threads"][owner].get("active_batch") != batch_id:
            raise ValueError("batch is not the owner's active batch")
        expected = list(batch["item_urls"])
        results_path = work / f"{batch_id}-results.json"
        if batch.get("state") == "claimed":
            results, _resources = _captured_results(work, expected)
            if results_path.exists() and json.loads(results_path.read_text(encoding="utf-8")) != results:
                raise RuntimeError("existing batch results conflict with captured journal evidence")
            atomic_json(results_path, results)
            ready = coordctl.ready_batch(state_path, owner, batch_id, results, True)
        elif batch.get("state") == "ready":
            ready = {"ok": True, "command": "ready", "status": "already_ready"}
            if any(item.get("outcome") != "captured" for item in batch.get("results") or []):
                raise ValueError("complete-captured only accepts all-captured ready batches")
        else:
            raise ValueError(f"unsupported active batch state: {batch.get('state')}")
        journal_versions = _journal_versions(work)
        finished = batchctl.run_batch("finish", [work], tail=len(expected))
        audit = finished.get("audit", {})
        if (finished.get("ok") is not True or finished.get("error_count") != 0
                or audit.get("records") != len(expected)
                or audit.get("passed") != len(expected)):
            raise RuntimeError("batchctl finish did not verify every captured batch item: "
                               + json.dumps(finished.get("errors", []), ensure_ascii=False))
        cover_verification = _ensure_batch_covers(work, batch_id, expected)
        if _journal_versions(work) != journal_versions:
            raise RuntimeError("journal evidence changed during cover finalization")
        verification = {
            "batch_id": batch_id, "verified": True,
            "verifier": "workflowctl complete-captured via batchctl finish",
            "work": str(work), "sync_ok": True, "audit_ok": True,
            "evidence": {
                "method": f"workflowctl complete-captured; batchctl finish --tail {len(expected)}",
                "checked_items": len(expected),
                "summary": (
                    f"Stopped journal tail exactly matched {len(expected)} claimed URLs; "
                    f"batchctl audited {audit.get('resources_checked', 0)} resources with zero errors."
                ),
            },
            **cover_verification,
        }
        verification_path = work / f"{batch_id}-verification.json"
        if verification_path.exists() and json.loads(verification_path.read_text(encoding="utf-8")) != verification:
            raise RuntimeError("existing verification file conflicts with deterministic evidence")
        atomic_json(verification_path, verification)
        acknowledged = coordctl.acknowledge_batch(
            state_path, owner, batch_id, verification,
        )
        return {
            "ok": True, "command": "complete-captured", "status": "acknowledged",
            "batch": batch_id, "ready": ready,
            "audit": {
                "records": audit.get("records"), "passed": audit.get("passed"),
                "resources_checked": audit.get("resources_checked"), "errors": 0,
            },
            "ack": acknowledged, "results": str(results_path),
            "verification": str(verification_path), "covers": cover_verification,
        }


DETAIL_JOURNALS = {"details.ndjson": "captured", "raw-details.ndjson": "raw"}


def _journal_sizes(work: Path) -> dict[str, int]:
    sizes = {}
    for name in DETAIL_JOURNALS:
        path = work / name
        if path.is_symlink():
            raise ValueError("detail journal links require separate review")
        size = path.stat().st_size if path.exists() else 0
        if size:
            with path.open("rb") as stream:
                stream.seek(size - 1)
                if stream.read(1) != b"\n":
                    raise ValueError(f"{name} has a partial tail; preserve it for review")
        sizes[name] = size
    return sizes


def _journal_versions(work: Path) -> dict[str, tuple[int, int] | None]:
    versions = {}
    for name in DETAIL_JOURNALS:
        path = work / name
        stamp = path.stat() if path.exists() else None
        versions[name] = (stamp.st_size, stamp.st_mtime_ns) if stamp else None
    return versions


def _detail_book(state_path: Path, owner: str, batch_id: str, work: Path,
                 batch: dict[str, Any], taskbooks: Path,
                 offsets: dict[str, int] | None = None) -> tuple[Path, dict[str, Any]]:
    path = _taskbook_path(taskbooks, f"detail-{batch_id}")
    if path.exists():
        book = json.loads(path.read_text(encoding="utf-8"))
        if (book.get("kind") != "detail" or book.get("owner") != owner
                or book.get("batch_id") != batch_id
                or Path(book.get("work", "")).resolve() != work
                or Path(book.get("coordination_state", "")).resolve() != state_path.resolve()
                or book.get("scope", {}).get("observed_urls") != batch["item_urls"]):
            raise ValueError("existing detail taskbook conflicts with the authoritative batch")
        return path, book
    book = {
        "task_id": f"detail-{batch_id}", "kind": "detail", "owner": owner,
        "batch_id": batch_id, "created_at": utc_now(), "work": str(work),
        "objective": "采集本批已观察详情页并逐页入库核验；raw/失败保留待复核。",
        "scope": {"observed_urls": batch["item_urls"]}, "limit": len(batch["item_urls"]),
        "inputs": [str(state_path.resolve())], "coordination_state": str(state_path.resolve()),
        "capture_engine": "playwright",
        "acceptance": ["独立无界面 Playwright、单页面单 lane，采集线程只读取脚本结果",
                       "实际检查 #list a[data-web] 属性及完整计数，不以 AX 无 href 判无资源",
                       "验证码期限/更新验证码提示不作为采集前置条件或失败依据",
                       "完整公开 DOM 观察", "停写后逐页 finish/audit/ack"],
        "stop_conditions": ["明确访问限制使实际 DOM 读取不可用，或用户操作冲突",
                            "日志或所有权不一致", "本批范围完成"],
        # These are a fixed evidence boundary, not another progress queue.
        "journal_offsets": offsets,
    }
    shared_discovery.write_exclusive(path, book)
    return path, book


def _detail_observations(work: Path, batch: dict[str, Any],
                         book: dict[str, Any]) -> dict[str, str]:
    """Read only bounded journal tails; never emit media payloads or full history."""
    expected = set(batch["item_urls"])
    found = {}
    captured_payloads = {}
    offsets = book.get("journal_offsets")
    sizes = _journal_sizes(work)
    for name, outcome in DETAIL_JOURNALS.items():
        size = sizes[name]
        start = offsets.get(name) if isinstance(offsets, dict) else None
        if start is not None and (type(start) is not int or start < 0 or size < start):
            raise ValueError("detail journal is shorter than its taskbook evidence boundary")
        if not size or size == start:
            continue
        path = work / name
        stamp = path.stat().st_mtime_ns
        lines, partial = batchctl.read_tail(path, len(expected) + 1, size)
        if partial:
            raise ValueError(f"{name} has a partial journal tail")
        for end, raw in lines:
            if start is not None and end <= start:
                continue
            record = json.loads(raw.decode("utf-8"))
            if start is None:
                # Older active batches have no pre-claim byte boundary.  Require
                # dated evidence from this attempt, never an earlier retry.
                observed_at = datetime.fromisoformat(record["collectedAt"].replace("Z", "+00:00"))
                claimed_at = datetime.fromisoformat(batch["claimed_at"].replace("Z", "+00:00"))
                if observed_at < claimed_at:
                    continue
            url = coordctl._page_url(record.get("url"))
            if url not in expected:
                raise ValueError("journal evidence after this batch boundary contains an unclaimed URL")
            if book.get('capture_engine')=='playwright':
                import playwrightctl
                evidence_path=Path(record.get('playwrightEvidence',''))
                if (not evidence_path.is_absolute() or evidence_path.is_symlink()
                        or not evidence_path.resolve().is_relative_to(work.resolve())
                        or not evidence_path.is_file() or evidence_path.stat().st_nlink!=1):
                    raise ValueError('Missing or unsafe Playwright source receipt')
                raw_evidence=evidence_path.read_bytes()
                evidence=json.loads(raw_evidence)
                original_item=read_state(Path(book['coordination_state']))['items'][url]['item']
                playwrightctl.validate_evidence(evidence,original_item,book['owner'],book['batch_id'])
                if record.get('playwrightJobId')!=evidence.get('jobId'):
                    raise ValueError('Playwright journal job identity differs from source receipt')
                evidence['evidencePath']=str(evidence_path)
                preview=playwrightctl.node('preview',evidence=evidence,item=original_item,work=str(work))
                if preview.get('observation')!=record or evidence_path.read_bytes()!=raw_evidence:
                    raise ValueError('Playwright source receipt does not reproduce the journal')
            if outcome == "captured":
                document = dbctl.normalize("details", record)
                if not document["programs"][0]["captured"] or not document["resources"]:
                    raise ValueError("detail evidence is not a complete captured observation")
            else:
                dbctl.validate_raw_details(record)
            payload = {key: value for key, value in record.items() if key != "collectedAt"}
            if url in found:
                # A lost reply can append the same full DOM capture twice. Keep
                # both source records, count this URL once, and ignore ONLY the
                # collection clock; media, identity, count and fingerprint must
                # all match. Raw, mixed-outcome or changed content stays blocked.
                if (outcome == "captured" and found[url] == "captured"
                        and payload == captured_payloads.get(url)):
                    continue
                raise ValueError("batch has duplicate/conflicting journal evidence; review before completing")
            found[url] = outcome
            if outcome == "captured":
                captured_payloads[url] = payload
        if path.stat().st_size != size or path.stat().st_mtime_ns != stamp:
            raise RuntimeError("detail journal changed while reading; retry at a stopped checkpoint")
    return found


def _repair_ascii_detail_fields(state_path, owner, batch_id, work, proof, source_tool,
                                data, evidence_path, taskbooks):
    """Restore only proven ASCII replacement loss in an entirely unstarted batch."""
    from xml.etree import ElementTree
    from urllib.parse import urljoin, urlsplit
    from app_server_control import RPC
    import recoveryctl

    slot, binding = recoveryctl.slot_for(owner)
    if (state_path.resolve() != DEFAULT_STATE.resolve() or taskbooks.resolve() != DEFAULT_TASKBOOKS.resolve()
            or binding.get('registration_verified') is not True or Path(binding['work']).resolve() != work
            or proof.get('formal_current') != {'capture_executed': False, 'prepare_executed': False}):
        raise ValueError('ASCII recovery requires the original registered, unstarted batch')
    state_tool = proof.get('source_state_tool')
    with RPC() as rpc:
        thread = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
        if (thread.get('parentThreadId') != binding['host_parent']
                or thread.get('status', {}).get('type') != 'idle'
                or Path(thread.get('cwd', '')).resolve() not in {PROJECT_ROOT.resolve(), PROJECT_ROOT.parent.resolve()}):
            raise ValueError('Original DOM owner must be idle with its registered parent/cwd')
        native = rpc.call('thread/turns/list', {'threadId': owner, 'limit': 3,
            'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
    receipts = {}
    for tool_id in {source_tool, state_tool}:
        calls = [item for turn in native for item in turn.get('items', []) if item.get('id') == tool_id]
        if len(calls) != 1:
            raise ValueError('ASCII recovery requires the exact bounded original DOM calls')
        call = calls[0]
        code = (call.get('arguments') or {}).get('code', '')
        result = call.get('result') or {}
        contents = result.get('content', [])
        surface = (result.get('_meta') or {}).get('codex/toolSurface') or {}
        if (call.get('type') != 'mcpToolCall' or call.get('status') != 'completed'
                or call.get('readOnlyHint') is not True or call.get('server') != 'cua_repl'
                or call.get('tool') != 'js' or '.playwright.' not in code or '.evaluate(' not in code
                or re.search(r'\.(?:goto|click|fill|press|reload|close|fetch|runDetailBatchSegment)\s*\(', code)
                or surface.get('backend') != 'chrome' or surface.get('browserFamily') != 'chrome'
                or not surface.get('browserId') or len(contents) != 1 or contents[0].get('type') != 'text'):
            raise ValueError('ASCII recovery requires completed read-only Chrome DOM receipts')
        receipts[tool_id] = (call, json.loads(contents[0]['text']))
    card_call, original = receipts[source_tool]
    state_call, state_result = receipts[state_tool]
    positions = [(turn['id'], index, item) for turn in native
                 for index, item in enumerate(turn.get('items', []))]
    card_pos = next((turn, index) for turn, index, item in positions if item.get('id') == source_tool)
    state_pos = next((turn, index) for turn, index, item in positions if item.get('id') == state_tool)
    if (card_pos[0] != state_pos[0] or state_pos[1] > card_pos[1]
            or any(item.get('type') == 'mcpToolCall' and item.get('readOnlyHint') is not True
                   for turn, index, item in positions
                   if turn == card_pos[0] and state_pos[1] < index < card_pos[1])):
        raise ValueError('Source/card receipts must belong to one unchanged public DOM observation')
    if original != proof.get('source_result') or state_result != proof.get('source_state_result'):
        raise ValueError('Saved ASCII evidence differs from the original native DOM results')
    card_meta = card_call['result']['_meta']
    state_meta = state_call['result']['_meta']
    page_url = card_meta.get('browser_use', {}).get('url', '')
    if (page_url != state_meta.get('browser_use', {}).get('url')
            or card_meta['codex/toolSurface']['browserId'] != state_meta['codex/toolSurface']['browserId']
            or urlsplit(page_url).scheme != 'https' or urlsplit(page_url).hostname not in {'xxmanmi.com', 'www.xxmanmi.com'}):
        raise ValueError('Original source/card browser and public page differ')
    latest_source = proof.get('source_view') == 'public_latest'
    if latest_source:
        # A latest-list claim has no query. Prove that view from visible DOM;
        # an old value in the hidden search input must never become its query.
        old = proof.get('original_item', {})
        url = coordctl._page_url(old.get('url', ''))
        view = state_result.get('view', {})
        card = state_result.get('targetCard', {})
        sources = [row for row in state_result.get('checkedSourceAndTypes', [])
                   if row.get('name') == 'newNum']
        ancestors = original.get('ancestry', [])
        active_tabs = [row for row in view.get('tabNodes', [])
                       if 'weui-bar__item_on' in row.get('className', '').split()]
        visible_containers = {row.get('id') for row in ancestors
                              if row.get('tag') == 'DIV' and row.get('visible') is True}
        if (old.get('query') != '' or state_result.get('url') != page_url
                or view.get('loading') != []
                or view.get('searchInput', {}).get('visible') is not False
                or len(active_tabs) != 1
                or 'data-id="like"' not in active_tabs[0].get('outerHTML', '')
                or original.get('activeQueryView') is not False
                or original.get('loadingTextVisible') is not False
                or original.get('targetVisible') is not True
                or original.get('targetHref') != url or card.get('href') != url
                or len(original.get('selectedMainView', [])) != 1
                or original['selectedMainView'][0].get('dataId') != 'like'
                or not {'newList', 'like'}.issubset(visible_containers)
                or not ancestors or ancestors[0].get('tag') != 'A'
                or ancestors[0].get('visible') is not True
                or len(sources) != 1 or sources[0].get('labelVisible') is not True
                or not isinstance(sources[0].get('value'), str)
                or not sources[0]['value'].isdigit()
                or sources[0].get('id') != 'newNum' + sources[0]['value']
                or 'input[type="radio"]:checked' not in state_call['arguments']['code']):
            raise ValueError('Empty-query ASCII recovery requires the settled visible latest source/card DOM')
        original = {'href': card['href'], 'html': card.get('innerHTML')}
        source_state = {'selected': [sources[0].get('label')], 'loading': 0, 'query': '',
                        'items': [{'href': card['href']}]}
    else:
        source_state = state_result.get('state', {})
    selected = source_state.get('selected', [])
    if len(selected) != 1 or source_state.get('loading') not in (0, []):
        raise ValueError('Original public source selection is not settled and unambiguous')
    old = proof.get('original_item', {})
    url = coordctl._page_url(old.get('url', ''))
    visible = source_state.get('results', source_state.get('items', []))
    if len([item for item in visible
            if urljoin(page_url, item.get('href', item.get('url', ''))) == url]) != 1:
        raise ValueError('Original selected source must contain exactly the claimed public URL')
    if 'href' in original:
        card = original
        tree = ElementTree.fromstring(card['html'])
    else:
        cards = [item for item in source_state.get('items', []) if urljoin(page_url, item.get('href', '')) == url]
        if len(cards) != 1:
            raise ValueError('Original source DOM must identify exactly one claimed result')
        card = cards[0]
        tree = ElementTree.fromstring('<div>' + card['html'] + '</div>')
    badges = list(tree)
    title = (tree.text or '').strip()
    if (urljoin(page_url, card.get('href', '')) != url or tree.tag != 'div' or not title
            or len(badges) != 1 or badges[0].tag != 'p' or list(badges[0])
            or 'weui-media-box__desc' not in badges[0].get('class', '').split()
            or (badges[0].tail or '').strip()):
        raise ValueError('Original DOM card does not contain separate title and status nodes')
    fields = {'title': title, 'status': (badges[0].text or '').strip(),
              'source': selected[0], 'query': source_state.get('query')}
    if (not all(isinstance(fields[key], str) and 0 < len(fields[key]) <= 1000
                for key in ('title', 'status', 'source'))
            or not isinstance(fields['query'], str) or len(fields['query']) > 1000
            or (not fields['query'] and not latest_source)
            or old.get('title') == title
            or any(old.get(key) != value.encode('ascii', 'replace').decode('ascii') for key, value in fields.items())):
        raise ValueError('Queue fields are not exactly the proven ASCII replacement loss')
    digest = hashlib.sha256(data).hexdigest()
    with work_lock(work):
        def operation(state):
            if (recoveryctl.slot_for(owner) != (slot, binding) or evidence_path.read_bytes() != data
                    or os.environ.get('CODEX_THREAD_ID') != recoveryctl.policy().get('main_thread')):
                raise ValueError('ASCII recovery authority/evidence changed during verification')
            peer = coordctl._require_peer(state, owner)
            batch = state['batches'].get(batch_id, {})
            if (batch.get('thread') != owner or batch.get('state') != 'claimed'
                    or peer.get('active_batch') != batch_id or url not in batch.get('item_urls', [])
                    or Path(batch.get('work', '')).resolve() != work):
                raise ValueError('ASCII recovery requires the same active claimed batch')
            _path, book = _detail_book(state_path, owner, batch_id, work, batch, taskbooks)
            versions = _journal_versions(work)
            if _detail_observations(work, batch, book) or any(work.glob(f'{batch_id}-native-detail-*.json')):
                raise ValueError('ASCII recovery cannot change a prepared or journalled batch')
            record = state['items'][url]
            history = record.setdefault('title_correction_history', [])
            if any(row.get('evidence_sha256') == digest for row in history):
                return {'ok': True, 'status': 'already_repaired', 'batch': batch_id, 'url': url}, False
            if record.get('state') != 'claimed' or record.get('batch') != batch_id or record['item'] != old:
                raise ValueError('Original damaged queue item changed')
            if versions != _journal_versions(work):
                raise ValueError('Owner journals changed during ASCII recovery')
            history.append({'corrected_at': utc_now(), 'batch': batch_id, 'previous_item': old,
                'correction_kind': 'ascii_replacement_loss', 'evidence': str(evidence_path),
                'evidence_sha256': digest, 'source_tool': source_tool, 'source_state_tool': state_tool})
            record['item'] = {**old, **fields}
            return {'ok': True, 'command': 'repair-detail-title', 'status': 'repaired',
                    'batch': batch_id, 'url': url, 'claim_and_evidence_preserved': True,
                    'next_action': 'original owner claim-detail to refresh remaining input'}, True
        return coordctl._update(state_path, operation)


def repair_detail_title(state_path: Path, owner: str, batch_id: str,
                        evidence_path: Path, source_tool: str, logs_stopped: bool, *,
                        taskbooks: Path = DEFAULT_TASKBOOKS) -> dict[str, Any]:
    """Repair proven public title/source text damage; retain the same claim."""
    from xml.etree import ElementTree
    from urllib.parse import urljoin
    from app_server_control import RPC
    import recoveryctl

    if (logs_stopped is not True or not os.environ.get('CODEX_THREAD_ID')
            or os.environ['CODEX_THREAD_ID'] != recoveryctl.policy().get('main_thread')):
        raise ValueError('Title repair requires the actual main and stopped owner logs')
    _record, work = owner_record(state_path, owner)
    evidence_path = evidence_path.resolve()
    if not evidence_path.is_relative_to(work):
        raise ValueError('Title evidence must belong to the original owner work directory')
    data = evidence_path.read_bytes()
    if len(data) > 128 * 1024:
        raise ValueError('Title evidence exceeds the bounded size')
    proof = json.loads(data)
    entity_layers = proof.get('correction_kind') == 'html_entity_escape_layers'
    formal = proof.get('formal_current', {})
    if (proof.get('schema') != 'detail-title-source-evidence/v1'
            or proof.get('owner') != owner or proof.get('batch') != batch_id
            or Path(proof.get('work', '')).resolve() != work):
        raise ValueError('Title evidence does not match the uncaptured original claim')
    if proof.get('correction_kind') == 'ascii_replacement_loss':
        return _repair_ascii_detail_fields(state_path, owner, batch_id, work, proof, source_tool,
                                          data, evidence_path, taskbooks)
    if entity_layers:
        if (formal.get('capture_attempted') is not True
                or type(formal.get('capture_journaled')) is not int or formal['capture_journaled'] != 0):
            raise ValueError('Entity title evidence must retain the real capture attempt with zero journalled results')
    elif formal.get('capture_executed') is not False:
        raise ValueError('Title evidence does not match the uncaptured original claim')
    detail = proof['fresh_detail_public_dom']
    if entity_layers:
        slot, binding = recoveryctl.slot_for(owner)
        if (state_path.resolve() != DEFAULT_STATE.resolve() or taskbooks.resolve() != DEFAULT_TASKBOOKS.resolve()
                or binding.get('registration_verified') is not True or not binding.get('host_parent')
                or Path(binding['work']).resolve() != work or not work.is_relative_to(DB_ROOT.resolve())):
            raise ValueError('Entity title repair requires the original registered project owner/work')
        url = coordctl._page_url(detail['url'])
        title = detail.get('videoTitle')
        original_title = proof.get('original_title')

        def decode_amp_layers(value):
            if not isinstance(value, str) or not value.strip() or len(value) > 1000:
                raise ValueError('Entity title evidence must contain a bounded nonempty title')
            for _ in range(4):
                decoded = value.replace('&amp;', '&')
                if decoded == value:
                    return value
                value = decoded
            if '&amp;' in value:
                raise ValueError('Title ampersand encoding exceeds four layers')
            return value

        if (original_title == title or decode_amp_layers(original_title) != decode_amp_layers(title)
                or detail.get('readyState') != 'complete' or detail.get('url') != url
                or not isinstance(detail.get('resourceLinks'), list) or not detail['resourceLinks']):
            raise ValueError('Detail title is not the exact proven ampersand encoding equivalent')
        observation_times = (detail['observed_at'],)
    else:
        if proof.get('correction_kind') is not None:
            raise ValueError('Unsupported detail title correction kind')
        listing = proof['fresh_public_list_anchor']
        anchor = listing['anchor']
        url = coordctl._page_url(anchor['href'])
        tree = ElementTree.fromstring(anchor['outerHTML'])
        body = list(tree)
        if (tree.tag != 'a' or urljoin(listing['page_url'], tree.get('href', '')) != url
                or len(body) != 1 or body[0].tag != 'div'
                or 'weui-media-box__bd' not in body[0].get('class', '').split()
                or (tree.text or '').strip() or (body[0].tail or '').strip()):
            raise ValueError('List evidence must contain one separate title body')
        badges = list(body[0])
        title = (body[0].text or '').strip()
        if (not title or len(badges) != 1 or badges[0].tag != 'p' or list(badges[0])
                or 'weui-media-box__desc' not in badges[0].get('class', '').split()
                or (badges[0].text or '').strip() != 'HD'
                or (badges[0].tail or '').strip()):
            raise ValueError('Title repair requires an independently observed HD paragraph')
        if (listing.get('ready_state') != 'complete' or detail.get('ready_state') != 'complete'
                or detail.get('page_url') != url or detail.get('heading_selector') != '#video'
                or detail.get('heading_text') != title
                or detail.get('resource_selector') != '#list a[data-web]'
                or type(detail.get('resource_count')) is not int or detail['resource_count'] < 1
                or anchor.get('textContent') != title + 'HD'
                or ''.join(tree.itertext()).strip() != title + 'HD'):
            raise ValueError('List title and same-page detail evidence disagree')
        observation_times = (listing['observed_at'], detail['observed_at'])
    # Bind the correction to the original owner's successful public DOM call.
    with RPC() as rpc:
        if entity_layers:
            thread = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
            if (thread.get('id') != owner or thread.get('parentThreadId') != binding['host_parent']
                    or not isinstance(thread.get('cwd'), str) or not thread['cwd']
                    or Path(thread['cwd']).resolve() not in (PROJECT_ROOT.resolve(), PROJECT_ROOT.parent.resolve())):
                raise ValueError('Native DOM owner/parent/cwd differs from the registered owner')
        native = rpc.call('thread/turns/list', {'threadId': owner, 'limit': 2,
            'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
    if not native or any(item.get('status') == 'inProgress'
            for item in native[0].get('items', [])
            if item.get('type') in {'mcpToolCall', 'commandExecution'}):
        raise ValueError('Original owner still has an in-flight tool; retain its handle')
    calls = [item for turn in native for item in turn.get('items', [])
             if item.get('id') == source_tool]
    if len(calls) != 1:
        raise ValueError('The exact original DOM call is not in bounded native receipts')
    call = calls[0]
    code = (call.get('arguments') or {}).get('code', '')
    contents = (call.get('result') or {}).get('content', [])
    if (call.get('type') != 'mcpToolCall' or call.get('status') != 'completed'
            or call.get('tool') != 'js' or 'node_repl' not in call.get('server', '')
            or '.playwright.evaluate(' not in code
            or re.search(r'\.(?:goto|click|fill|check|press|reload|close)\s*\(', code)
            or len(contents) != 1 or contents[0].get('type') != 'text'):
        raise ValueError('Title repair requires a completed read-only DOM receipt')
    original = json.loads(contents[0]['text'])
    if entity_layers:
        surface = ((call.get('result') or {}).get('_meta') or {}).get('codex/toolSurface') or {}
        # This exact observed query shape has no page mutations or replacement title.
        readonly_query = (
            r"const (?P<snapshot>[A-Za-z_$][\w$]*)=await [A-Za-z_$][\w$]*\.playwright\.evaluate\(\(\)=>\(\{"
            r"observed_at:new Date\(\)\.toISOString\(\),url:location\.href,readyState:document\.readyState,"
            r"title:document\.title,h1:\[\.\.\.document\.querySelectorAll\('h1'\)\]\.map\(x=>x\.innerText\),"
            r"videoTitle:document\.querySelector\('#video'\)\?\.innerText\?\?null,"
            r"resourceLinks:\[\.\.\.document\.querySelectorAll\('#list a\[data-web\]'\)\]"
            r"\.map\(a=>\(\{href:a\.href,text:a\.innerText\}\)\),"
            r"visibleText:document\.body\.innerText\.slice\(0,1800\)\}\)\);"
            r"nodeRepl\.write\(JSON\.stringify\(\{inspect:(?P=snapshot),meta:\{"
            r"callId:nodeRepl\.requestMeta\.callId,sessionId:nodeRepl\.requestMeta\.sessionId,"
            r"threadId:nodeRepl\.requestMeta\.threadId\}\}\)\);")
        if (original.get('inspect') != detail
                or original.get('meta') != {'callId': source_tool, 'sessionId': binding['host_parent'], 'threadId': owner}
                or surface.get('backend') != 'chrome' or surface.get('browserFamily') != 'chrome'
                or not surface.get('browserId')
                or not re.fullmatch(readonly_query, code)):
            raise ValueError('Saved entity title differs from the original native detail DOM result')
        failed_tools = [item for turn in native for item in turn.get('items', [])
                        if item.get('id') == proof.get('failure_source_tool')]
        if len(failed_tools) != 1 or proof.get('failure_source_tool') == source_tool:
            raise ValueError('Entity title repair requires the exact original failed capture receipt')
        failed_call = failed_tools[0]
        failed_contents = (failed_call.get('result') or {}).get('content', [])
        if (failed_call.get('type') != 'mcpToolCall' or failed_call.get('status') != 'completed'
                or failed_call.get('tool') != 'js' or 'node_repl' not in failed_call.get('server', '')
                or '.runDetailBatchSegment(' not in (failed_call.get('arguments') or {}).get('code', '')
                or len(failed_contents) != 1 or failed_contents[0].get('type') != 'text'):
            raise ValueError('Original capture is not a settled native batch segment result')
        failed_result = json.loads(failed_contents[0]['text'])
        capture = failed_result.get('result', {})
        lanes = capture.get('lanes', [])
        if (failed_result.get('meta') != {'callId': proof['failure_source_tool'],
                                        'sessionId': binding['host_parent'], 'threadId': owner}
                or capture.get('ok') is not False or type(capture.get('journaled')) is not int
                or capture['journaled'] != 0 or capture.get('requested') != 1 or capture.get('attempted') != 1
                or len(lanes) != 1 or lanes[0].get('name') != owner or lanes[0].get('journaled') != 0
                or lanes[0].get('errors') != [{'index': 0, 'title': original_title, 'url': url,
                    'capture': {'name': 'Error', 'message': 'DOM program title does not match the assigned queue item'}}]):
            raise ValueError('Original capture does not prove the exact unjournalled title mismatch')
    elif (original.get('observed_at') != listing['observed_at']
            or original.get('page_url') != listing['page_url']
            or original.get('anchor', {}).get('href') != url
            or original.get('anchor', {}).get('outerHTML') != anchor['outerHTML']):
        raise ValueError('Saved title evidence differs from the original native DOM result')
    digest = hashlib.sha256(data).hexdigest()
    with work_lock(work):
        def operation(state):
            if entity_layers and (recoveryctl.slot_for(owner) != (slot, binding)
                    or os.environ.get('CODEX_THREAD_ID') != recoveryctl.policy().get('main_thread')
                    or evidence_path.read_bytes() != data):
                raise ValueError('Entity title authority or original evidence changed during verification')
            peer = coordctl._require_peer(state, owner)
            batch = state['batches'].get(batch_id)
            if (not batch or batch['thread'] != owner or batch['state'] != 'claimed'
                    or peer.get('active_batch') != batch_id
                    or Path(batch['work']).resolve() != work or url not in batch['item_urls']):
                raise ValueError('Title repair requires the original active claimed batch')
            book_path = _taskbook_path(taskbooks, f'detail-{batch_id}')
            if not book_path.is_file():
                raise ValueError('Original detail taskbook is missing')
            _path, book = _detail_book(state_path, owner, batch_id, work, batch, taskbooks)
            versions = _journal_versions(work)
            if url in _detail_observations(work, batch, book):
                raise ValueError('Title repair cannot rewrite an already journaled item')
            for observed in observation_times:
                when = datetime.fromisoformat(observed.replace('Z', '+00:00'))
                claimed = datetime.fromisoformat(batch['claimed_at'].replace('Z', '+00:00'))
                if when < claimed or when > datetime.now(timezone.utc):
                    raise ValueError('Title evidence is outside the claimed observation period')
            record = state['items'][url]
            history = record.setdefault('title_correction_history', [])
            if (record['item'].get('title') == title and (entity_layers or record['item'].get('status') == 'HD')
                    and any(row.get('evidence_sha256') == digest for row in history)):
                return {'ok': True, 'command': 'repair-detail-title', 'status': 'already_repaired',
                        'batch': batch_id, 'url': url}, False
            old = dict(record['item'])
            if (record.get('batch') != batch_id or record.get('state') != 'claimed'
                    or (old.get('title') != original_title if entity_layers else
                        old.get('title') != title + 'HD' or old.get('status', '') != '')):
                raise ValueError('Queue item is not the exact proven concatenated title')
            if versions != _journal_versions(work):
                raise ValueError('Owner journals changed during title repair')
            corrected = {**old, 'title': title} if entity_layers else {**old, 'title': title, 'status': 'HD'}
            history.append({'corrected_at': utc_now(), 'batch': batch_id,
                'previous_item': old, 'evidence': str(evidence_path), 'evidence_sha256': digest,
                'source_tool': source_tool, 'title': title, 'status': corrected.get('status', ''),
                **({'correction_kind': 'html_entity_escape_layers', 'failure_source_tool': proof['failure_source_tool'],
                    'failure_result_sha256': hashlib.sha256(failed_contents[0]['text'].encode()).hexdigest()}
                   if entity_layers else {})})
            record['item'] = corrected
            return {'ok': True, 'command': 'repair-detail-title', 'status': 'repaired',
                    'batch': batch_id, 'url': url, 'claim_and_evidence_preserved': True,
                    'next_action': 'original owner claim-detail to refresh remaining input'}, True
        return coordctl._update(state_path, operation)


def claim_detail_task(state_path: Path, owner: str, limit: int = 4, *,
                       taskbooks: Path = DEFAULT_TASKBOOKS,
                       request_path: Path | None = None, query: str | None = None,
                       exact_title: bool = False,
                       request_root: Path = DEFAULT_REQUESTS) -> dict[str, Any]:
    if type(limit) is not int or not 1 <= limit <= coordctl.MAX_CLAIM_SIZE:
        raise ValueError("detail limit must be between 1 and 8")
    if (request_path is None) != (query is None):
        raise ValueError("requested detail claim requires both request and exact query")
    if exact_title and request_path is None:
        raise ValueError("exact-title requires an explicit user title request")
    category = ""
    if request_path is not None:
        _request, query, category = _requested_title_scope(request_path, query, request_root=request_root)
    _record, work = owner_record(state_path, owner)
    with work_lock(work):
        current = coordctl.current_batch(state_path, owner)
        batch_id = current.get("active_batch")
        offsets = None
        resumed = batch_id is not None
        if batch_id is None:
            offsets = _journal_sizes(work)
            if request_path is None:
                current = coordctl.claim_batch(state_path, owner, limit)
            else:
                state = read_state(state_path)
                eligible_urls = [url for url in state["queue"]
                                 if _requested_title_matches(state["items"][url]["item"]["title"],
                                                             query, category, exact=exact_title)]
                current = coordctl.claim_batch(state_path, owner, limit, eligible_urls=eligible_urls)
            batch_id = current["batch"]
        if batch_id is None:
            return {"ok": True, "command": "claim-detail", "status": "empty", "batch": None,
                    "remaining": 0, "items": [], "next_action": {"kind": "find_work",
                    "reason": "Playwright observes allocated public sources; not goal completion",
                    "argv": [sys.executable, "-B", str(HERE / "playwrightctl.py"),
                             "discover", "--thread", owner]}}
        state = read_state(state_path)
        batch = state["batches"][batch_id]
        if batch["thread"] != owner or state["threads"][owner]["active_batch"] != batch_id:
            raise ValueError("detail ownership changed while preparing the taskbook")
        taskbooks.mkdir(parents=True, exist_ok=True)
        path, book = _detail_book(state_path, owner, batch_id, work, batch, taskbooks, offsets)
        found = _detail_observations(work, batch, book)
        remaining = [state["items"][url]["item"] for url in batch["item_urls"] if url not in found]
        complete = batch["state"] == "ready" or not remaining
        capture_input = work / f"{batch_id}-capture-input.json"
        atomic_json(capture_input, {"batch": batch_id, "owner": owner, "work": str(work),
                                    "taskbook": str(path), "items": [] if complete else remaining})
        return {
            "ok": True, "command": "claim-detail", "status": "resumed" if resumed else "claimed",
            "batch": batch_id, "batch_state": batch["state"], "work": str(work),
            "taskbook": str(path), "capture_input": str(capture_input),
            "observed": dict(Counter(found.values())), "remaining": len(remaining),
            "items": [] if complete else remaining,
            "next_action": ({"kind": "complete_detail", "requires_logs_stopped": True,
                             "argv": [sys.executable, "-B", str(HERE / "workflowctl.py"),
                                      "complete-detail", "--state", str(state_path.resolve()),
                                      "--thread", owner, "--batch", batch_id,
                                      "--taskbooks", str(taskbooks.resolve()), "--logs-stopped"]}
                            if complete else {"kind": "capture", "input": str(capture_input),
                                              "engine": "playwright", "single_lane": True,
                                              "argv": [sys.executable, "-B", str(HERE / "playwrightctl.py"),
                                                       "collect", "--thread", owner, "--batch", batch_id]}),
        }


def complete_detail(state_path: Path, owner: str, batch_id: str, logs_stopped: bool, *,
                    taskbooks: Path = DEFAULT_TASKBOOKS,
                    failed: list[list[str]] | None = None,
                    skipped: list[list[str]] | None = None,
                    source_owner: str | None = None) -> dict[str, Any]:
    if logs_stopped is not True:
        raise ValueError("complete-detail requires --logs-stopped")
    actor = owner
    if source_owner is not None and source_owner != owner:
        from single_agentctl import authorized_handoff
        owner = authorized_handoff(actor, source_owner, batch_id)
        _record = read_state(state_path)['threads'][owner]
        work = Path(_record['work']).resolve()
    else:
        _record, work = owner_record(state_path, owner)
    with work_lock(work):
        state = read_state(state_path)
        batch = state.get("batches", {}).get(batch_id)
        if not batch or batch.get("thread") != owner or Path(batch["work"]).resolve() != work:
            raise ValueError("batch is not owned by this thread/work")
        import production_live
        production_live.mark_dirty(owner, batch=batch_id)
        if batch["state"] == "acknowledged":
            return {"ok": True, "command": "complete-detail", "status": "already_complete", "batch": batch_id}
        if state["threads"][owner].get("active_batch") != batch_id:
            raise ValueError("batch is not the owner's active batch")
        taskbooks.mkdir(parents=True, exist_ok=True)
        _path, book = _detail_book(state_path, owner, batch_id, work, batch, taskbooks)
        journal_versions = _journal_versions(work)
        found = _detail_observations(work, batch, book)
        results = {url: {"url": url, "outcome": outcome, "note": ""} for url, outcome in found.items()}
        if batch["state"] == "ready":
            prior = coordctl._clean_results(batch["results"])
            for url, row in results.items():
                if url in prior and prior[url]["outcome"] == row["outcome"]:
                    row["note"] = prior[url]["note"]
        declarations = [("failed", row) for row in failed or []] + [("skipped", row) for row in skipped or []]
        if batch["state"] == "ready":
            declarations.extend((row["outcome"], [row["url"], row.get("note", "")])
                                for row in batch["results"] if row["outcome"] in {"failed", "skipped"})
        for outcome, (url, note) in declarations:
            url = coordctl._page_url(url)
            if url not in batch["item_urls"] or not isinstance(note, str) or not note.strip():
                raise ValueError("failed/skipped declarations require a claimed URL and a real reason")
            if re.search(r"更新验证码|验证码\s*(?:期限|倒计时|有效期)|"
                         r"(?:verification|captcha)[ _-]*(?:countdown|expiry|expires|expired)",
                         note, re.IGNORECASE):
                raise ValueError(
                    "Generic verification countdown is not failure evidence. "
                    "Inspect actual data-web attributes with the official capture helper; "
                    "record its real DOM/navigation error instead."
                )
            if re.search(r"No tab with id:|not part of the current browser session",
                         note, re.IGNORECASE):
                raise ValueError(
                    "Browser tab is missing; this does not establish a page failure. "
                    "Restore this owner's task tab in the existing browser binding, "
                    "then resume the claimed batch from its remaining observations."
                )
            entry = {"url": url, "outcome": outcome, "note": note[:2000]}
            if url in results and results[url] != entry:
                raise ValueError("declared failure/skip conflicts with journal evidence")
            results[url] = entry
        if set(results) != set(batch["item_urls"]):
            raise ValueError("every batch URL requires captured/raw evidence or an explicit failed/skipped reason")
        ordered = [results[url] for url in batch["item_urls"]]
        results_path = work / f"{batch_id}-results.json"
        if results_path.exists():
            stored = coordctl._clean_results(json.loads(results_path.read_text(encoding="utf-8")))
            if stored != results:
                raise ValueError("existing results conflict with current batch evidence")
        else:
            atomic_json(results_path, ordered)
        if batch["state"] == "claimed":
            coordctl.ready_batch(state_path, owner, batch_id, ordered, True)
        elif batch["state"] != "ready" or coordctl._clean_results(batch["results"]) != results:
            raise ValueError("ready results conflict with current batch evidence")
        counts = Counter(row["outcome"] for row in ordered)
        audits = {}
        # Both media and raw audit belong to this one cooperative write interval.
        with dbctl.database_writer():
            for outcome, mode, total_key, passed_key in (
                ("captured", "finish", "records", "passed"),
                ("raw", "audit-raw" if counts["captured"] else "finish-raw", "raw_records", "raw_passed"),
            ):
                count = counts[outcome]
                if not count:
                    continue
                finished = batchctl.run_batch(mode, [work], tail=count)
                audit = finished.get("audit", {})
                if (finished.get("ok") is not True or finished.get("error_count") != 0
                        or audit.get(total_key) != count or audit.get(passed_key) != count):
                    raise RuntimeError(f"{mode} did not verify every {outcome} batch URL: "
                                       + json.dumps(finished.get("errors", []), ensure_ascii=False))
                audits[outcome] = audit
            # Unusable pages only settle their claim. Never import journals or
            # create programme rows merely to acknowledge a skipped page.
            if _journal_versions(work) != journal_versions:
                raise RuntimeError("journal evidence changed during finalization")
        captured_urls = [row["url"] for row in ordered if row["outcome"] == "captured"]
        # Mirror committed metadata even if a later cover gate blocks ACK.
        # A second event after ACK delivers the eventual accepted cover.
        try:
            import production_live
            production_live.notify_batch(owner, batch_id, state_path)
        except Exception:
            pass
        cover_verification = (_ensure_batch_covers(work, batch_id, captured_urls)
                              if captured_urls else {})
        if _journal_versions(work) != journal_versions:
            raise RuntimeError("journal evidence changed during cover finalization")
        verification = {
            "batch_id": batch_id, "verified": True, "verifier": "workflowctl complete-detail",
            "work": str(work), "sync_ok": True, "audit_ok": True,
            "evidence": {"method": "batch-bound journal validation and exact media/raw audits under one writer lock",
                         "checked_items": len(ordered), "summary": compact_json({"outcomes": dict(counts),
                         "audits": audits, "noncaptured": "raw/failed/skipped remain manual_review"})},
        }
        verification.update(cover_verification)
        verification_path = work / f"{batch_id}-verification.json"
        if verification_path.exists() and json.loads(verification_path.read_text(encoding="utf-8")) != verification:
            raise ValueError("existing verification conflicts with current batch evidence")
        atomic_json(verification_path, verification)
        ack = coordctl.acknowledge_batch(state_path, actor, batch_id, verification)
        if cover_verification.get("cover_pending_items"):
            pending_count = len(cover_verification["cover_pending_items"])
            counts["captured"] -= pending_count
            counts["cover_pending"] = pending_count
        return {"ok": True, "command": "complete-detail", "status": "acknowledged", "batch": batch_id,
                "outcomes": dict(counts), "audit": audits, "manual_review": ack["manual_review"],
                "results": str(results_path), "verification": str(verification_path),
                "covers": cover_verification}


def _peer_specs(values: list[str]) -> list[tuple[str, str]]:
    peers = []
    labels = set()
    owners = set()
    for value in values:
        if "=" not in value:
            raise ValueError("each --peer must be LABEL=OWNER")
        label, owner = (part.strip() for part in value.split("=", 1))
        if not label or not owner:
            raise ValueError("each --peer must contain a nonempty LABEL and OWNER")
        if label in labels or owner in owners:
            raise ValueError("--peer labels and owners must be unique")
        labels.add(label)
        owners.add(owner)
        peers.append((label, owner))
    return peers


def snapshot(state_path: Path, source: str, peer_values: list[str]) -> dict[str, Any]:
    """Return one bounded monitoring snapshot instead of several verbose commands."""
    source = source.strip()
    if not source:
        raise ValueError("--source must be nonempty")
    state = read_state(state_path)
    requested = _peer_specs(peer_values)
    if not requested:
        requested = [(owner, owner) for owner in sorted(state.get("threads", {}))]
    threads = []
    for label, owner in requested:
        record = state.get("threads", {}).get(owner)
        if not record:
            raise ValueError(f"snapshot peer is not registered: {owner}")
        batch_id = record.get("active_batch")
        batch = state.get("batches", {}).get(batch_id) if batch_id else None
        threads.append({
            "label": label,
            "owner": owner,
            "mode": record.get("mode"),
            "active_batch": batch_id,
            "active_items": len(batch.get("item_urls", [])) if batch else 0,
        })
    outstanding = []
    for batch_id, batch in sorted(state.get("batches", {}).items()):
        if batch.get("state") not in {"claimed", "ready"}:
            continue
        outstanding.append({
            "batch": batch_id,
            "owner": batch.get("thread"),
            "state": batch.get("state"),
            "items": len(batch.get("item_urls", [])),
            "claimed_at": batch.get("claimed_at"),
        })
    return {
        "ok": True,
        "command": "snapshot",
        "at": utc_now(),
        "database": dbctl.status(sources=[source]),
        "coordination": {
            "registered_threads": len(state.get("threads", {})),
            "items": dict(sorted(Counter(row["state"] for row in state["items"].values()).items())),
            "batches": dict(sorted(Counter(row["state"] for row in state["batches"].values()).items())),
        },
        "threads": threads,
        "outstanding_batches": outstanding,
    }


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description=__doc__)
    commands = root.add_subparsers(dest="command", required=True)

    claim = commands.add_parser("claim-search")
    claim.add_argument("--state", type=Path, default=DEFAULT_STATE)
    claim.add_argument("--thread", required=True)
    claim.add_argument("--channel", required=True)
    claim.add_argument("--query")
    claim.add_argument("--task-id")
    claim.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    claim.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    admit = commands.add_parser("admit-discovery-search",
                                help="reserve one verified discovery candidate and add its formal search plan")
    admit.add_argument("--state", type=Path, default=DEFAULT_STATE)
    admit.add_argument("--thread", required=True)
    admit.add_argument("--deduped", type=Path, required=True)
    admit.add_argument("--query", required=True)
    admit.add_argument("--channel", required=True)
    admit.add_argument("--task-id")
    admit.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    admit.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    requested = commands.add_parser("admit-requested-search",
                                    help="reserve an exact user-requested title with its request evidence")
    requested.add_argument("--state", type=Path, default=DEFAULT_STATE)
    requested.add_argument("--thread", required=True)
    requested.add_argument("--request", type=Path, required=True)
    requested.add_argument("--query", required=True)
    requested.add_argument("--channel", required=True)
    requested.add_argument("--task-id")
    requested.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    requested.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    commit = commands.add_parser("commit-search")
    commit.add_argument("--state", type=Path, default=DEFAULT_STATE)
    commit.add_argument("--thread", required=True)
    commit.add_argument("--evidence", type=Path, required=True)
    commit.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    commit.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    abandon = commands.add_parser(
        "abandon-search", help="quarantine a terminated-host search with unknown UI action",
    )
    abandon.add_argument("--state", type=Path, default=DEFAULT_STATE)
    abandon.add_argument("--thread", required=True)
    abandon.add_argument("--query", required=True)
    abandon.add_argument("--channel", required=True)
    abandon.add_argument("--evidence", type=Path, required=True)
    abandon.add_argument("--logs-stopped", action="store_true")
    abandon.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    abandon.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    terminal_quarantine = commands.add_parser(
        "quarantine-search-terminal", help="main-authorized manual-review isolation of settled unrecorded search actions",
    )
    terminal_quarantine.add_argument("--state", type=Path, default=DEFAULT_STATE)
    terminal_quarantine.add_argument("--thread", required=True)
    terminal_quarantine.add_argument("--query", required=True)
    terminal_quarantine.add_argument("--channel", required=True)
    terminal_quarantine.add_argument("--evidence", type=Path, required=True)
    terminal_quarantine.add_argument("--logs-stopped", action="store_true")
    terminal_quarantine.add_argument("--authorized", action="store_true")
    terminal_quarantine.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    terminal_quarantine.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    quarantine = commands.add_parser(
        "quarantine-search", help="isolate one completed UI search that cannot meet evidence gates",
    )
    quarantine.add_argument("--state", type=Path, default=DEFAULT_STATE)
    quarantine.add_argument("--thread", required=True)
    quarantine.add_argument("--query", required=True)
    quarantine.add_argument("--channel", required=True)
    quarantine.add_argument("--evidence", type=Path, required=True)
    quarantine.add_argument("--logs-stopped", action="store_true")
    quarantine.add_argument("--allow-saved-candidate", action="store_true",
                            help="preserve a rejected zero-result candidate, or an exact task query/input mismatch with authorized retained-action recovery")
    quarantine.add_argument("--authorized-parent-host", action="store_true",
                            help="main-authorized terminal isolation of an exact prepared selector failure in the configured parent host")
    quarantine.add_argument("--authorized-retained-action", action="store_true",
                            help="authorized manual-review isolation of a settled original action; never success or replay")
    quarantine.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    quarantine.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    resume_misbound = commands.add_parser("resume-misbound-search",
        help="continue OTHER tasks while preserving an already quarantined misbound capability dormant")
    resume_misbound.add_argument("--state", type=Path, default=DEFAULT_STATE)
    resume_misbound.add_argument("--thread", required=True)
    resume_misbound.add_argument("--query", required=True)
    resume_misbound.add_argument("--channel", required=True)
    resume_misbound.add_argument("--evidence", type=Path, required=True)
    resume_misbound.add_argument("--logs-stopped", action="store_true")
    resume_misbound.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    resume_misbound.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    host_quarantine = commands.add_parser(
        "quarantine-search-host-mismatch",
        help="terminally isolate one completed search whose live browser runtime belongs to another session",
    )
    host_quarantine.add_argument("--state", type=Path, default=DEFAULT_STATE)
    host_quarantine.add_argument("--thread", required=True)
    host_quarantine.add_argument("--query", required=True)
    host_quarantine.add_argument("--channel", required=True)
    host_quarantine.add_argument("--evidence", type=Path, required=True)
    host_quarantine.add_argument("--logs-stopped", action="store_true")
    host_quarantine.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    host_quarantine.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    input_mismatch = commands.add_parser(
        "quarantine-search-foreign-host-input-mismatch",
        help="manual-review quarantine for one returned foreign-host click with a mismatched actual search input",
    )
    input_mismatch.add_argument("--state", type=Path, default=DEFAULT_STATE)
    input_mismatch.add_argument("--thread", required=True)
    input_mismatch.add_argument("--query", required=True)
    input_mismatch.add_argument("--channel", required=True)
    input_mismatch.add_argument("--evidence", type=Path, required=True)
    input_mismatch.add_argument("--logs-stopped", action="store_true")
    input_mismatch.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    input_mismatch.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    stale_results = commands.add_parser(
        "quarantine-search-foreign-host-stale-results",
        help="manual-review quarantine for one foreign-host click that left old non-target results visible",
    )
    stale_results.add_argument("--state", type=Path, default=DEFAULT_STATE)
    stale_results.add_argument("--thread", required=True)
    stale_results.add_argument("--query", required=True)
    stale_results.add_argument("--channel", required=True)
    stale_results.add_argument("--evidence", type=Path, required=True)
    stale_results.add_argument("--logs-stopped", action="store_true")
    stale_results.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    stale_results.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    legacy_gate_quarantine = commands.add_parser(
        "quarantine-search-legacy-evidence-gate",
        help="terminally isolate a completed click stranded by a legacy evidence gate",
    )
    legacy_gate_quarantine.add_argument("--state", type=Path, default=DEFAULT_STATE)
    legacy_gate_quarantine.add_argument("--thread", required=True)
    legacy_gate_quarantine.add_argument("--query", required=True)
    legacy_gate_quarantine.add_argument("--channel", required=True)
    legacy_gate_quarantine.add_argument("--evidence", type=Path, required=True)
    legacy_gate_quarantine.add_argument("--logs-stopped", action="store_true")
    legacy_gate_quarantine.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    legacy_gate_quarantine.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    tab_gone_quarantine = commands.add_parser(
        "quarantine-search-host-tab-gone",
        help="terminally isolate one completed search after its foreign-host task tab disappears",
    )
    tab_gone_quarantine.add_argument("--state", type=Path, default=DEFAULT_STATE)
    tab_gone_quarantine.add_argument("--thread", required=True)
    tab_gone_quarantine.add_argument("--query", required=True)
    tab_gone_quarantine.add_argument("--channel", required=True)
    tab_gone_quarantine.add_argument("--evidence", type=Path, required=True)
    tab_gone_quarantine.add_argument("--logs-stopped", action="store_true")
    tab_gone_quarantine.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    tab_gone_quarantine.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    confirmed_tab_gone = commands.add_parser(
        "quarantine-search-confirmed-click-tab-gone",
        help="manual-review quarantine for a returned click after its live owner's task tab disappears",
    )
    confirmed_tab_gone.add_argument("--state", type=Path, default=DEFAULT_STATE)
    confirmed_tab_gone.add_argument("--thread", required=True)
    confirmed_tab_gone.add_argument("--query", required=True)
    confirmed_tab_gone.add_argument("--channel", required=True)
    confirmed_tab_gone.add_argument("--evidence", type=Path, required=True)
    confirmed_tab_gone.add_argument("--logs-stopped", action="store_true")
    confirmed_tab_gone.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    confirmed_tab_gone.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    no_outcome = commands.add_parser(
        "abandon-confirmed-click-no-outcome",
        help="manual-review terminal for a returned click whose outcome receipt is unavailable",
    )
    no_outcome.add_argument("--state", type=Path, default=DEFAULT_STATE)
    no_outcome.add_argument("--thread", required=True)
    no_outcome.add_argument("--query", required=True)
    no_outcome.add_argument("--channel", required=True)
    no_outcome.add_argument("--evidence", type=Path, required=True)
    no_outcome.add_argument("--logs-stopped", action="store_true")
    no_outcome.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    no_outcome.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    stale_non_target = commands.add_parser(
        "abandon-confirmed-click-stale-non-target-result",
        help="manual-review terminal when one returned click leaves the same sole old non-target result",
    )
    stale_non_target.add_argument("--state", type=Path, default=DEFAULT_STATE)
    stale_non_target.add_argument("--thread", required=True)
    stale_non_target.add_argument("--query", required=True)
    stale_non_target.add_argument("--channel", required=True)
    stale_non_target.add_argument("--evidence", type=Path, required=True)
    stale_non_target.add_argument("--logs-stopped", action="store_true")
    stale_non_target.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    stale_non_target.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    helper_gate_error = commands.add_parser(
        "abandon-confirmed-click-helper-gate-error",
        help="manual-review terminal for one returned click rejected by the exact stale-result helper gate",
    )
    helper_gate_error.add_argument("--state", type=Path, default=DEFAULT_STATE)
    helper_gate_error.add_argument("--thread", required=True)
    helper_gate_error.add_argument("--query", required=True)
    helper_gate_error.add_argument("--channel", required=True)
    helper_gate_error.add_argument("--evidence", type=Path, required=True)
    helper_gate_error.add_argument("--logs-stopped", action="store_true")
    helper_gate_error.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    helper_gate_error.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    cancel = commands.add_parser(
        "cancel-pre-search", help="record a verified invalid search plan before UI action",
    )
    cancel.add_argument("--state", type=Path, default=DEFAULT_STATE)
    cancel.add_argument("--thread", required=True)
    cancel.add_argument("--query", required=True)
    cancel.add_argument("--channel", required=True)
    cancel.add_argument("--evidence", type=Path, required=True)
    cancel.add_argument("--logs-stopped", action="store_true")
    cancel.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    cancel.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    scope_mismatch = commands.add_parser(
        "claim-task-scope-mismatch",
        help="manual-review terminal for an exact claim/taskbook query mismatch before UI action",
    )
    scope_mismatch.add_argument("--state", type=Path, default=DEFAULT_STATE)
    scope_mismatch.add_argument("--thread", required=True)
    scope_mismatch.add_argument("--query", required=True, help="exact effective claim query")
    scope_mismatch.add_argument("--channel", required=True)
    scope_mismatch.add_argument("--evidence", type=Path, required=True)
    scope_mismatch.add_argument("--logs-stopped", action="store_true")
    scope_mismatch.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    scope_mismatch.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)


    unavailable = commands.add_parser(
        "cancel-pre-search-session", help="record a verified session loss before any UI action",
    )
    unavailable.add_argument("--state", type=Path, default=DEFAULT_STATE)
    unavailable.add_argument("--thread", required=True)
    unavailable.add_argument("--query", required=True)
    unavailable.add_argument("--channel", required=True)
    unavailable.add_argument("--evidence", type=Path, required=True)
    unavailable.add_argument("--logs-stopped", action="store_true")
    unavailable.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    unavailable.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    input_abandon = commands.add_parser(
        "abandon-input", help="isolate one uninspectable timed-out input fill without search submit",
    )
    input_abandon.add_argument("--state", type=Path, default=DEFAULT_STATE)
    input_abandon.add_argument("--thread", required=True)
    input_abandon.add_argument("--query", required=True)
    input_abandon.add_argument("--channel", required=True)
    input_abandon.add_argument("--evidence", type=Path, required=True)
    input_abandon.add_argument("--logs-stopped", action="store_true")
    input_abandon.add_argument("--authorized-parent-input", action="store_true",
                              help="direct-user-authorized source-verified parent-host pre-submit recovery")
    input_abandon.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    input_abandon.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    click_abandon = commands.add_parser(
        "abandon-click", help="isolate one uninspectable timed-out search click without replay",
    )
    click_abandon.add_argument("--state", type=Path, default=DEFAULT_STATE)
    click_abandon.add_argument("--thread", required=True)
    click_abandon.add_argument("--query", required=True)
    click_abandon.add_argument("--channel", required=True)
    click_abandon.add_argument("--evidence", type=Path, required=True)
    click_abandon.add_argument("--logs-stopped", action="store_true")
    click_abandon.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    click_abandon.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    foreign_click_abandon = commands.add_parser(
        "abandon-foreign-host-click-unknown",
        help="manual-review terminal for one unknown click after a foreign host reset",
    )
    foreign_click_abandon.add_argument("--state", type=Path, default=DEFAULT_STATE)
    foreign_click_abandon.add_argument("--thread", required=True)
    foreign_click_abandon.add_argument("--query", required=True)
    foreign_click_abandon.add_argument("--channel", required=True)
    foreign_click_abandon.add_argument("--evidence", type=Path, required=True)
    foreign_click_abandon.add_argument("--logs-stopped", action="store_true")
    foreign_click_abandon.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    foreign_click_abandon.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    live_owner_click_abandon = commands.add_parser(
        "abandon-live-owner-click-unknown",
        help="manual-review terminal for one timed-out click helper call in its live owner session",
    )
    live_owner_click_abandon.add_argument("--state", type=Path, default=DEFAULT_STATE)
    live_owner_click_abandon.add_argument("--thread", required=True)
    live_owner_click_abandon.add_argument("--query", required=True)
    live_owner_click_abandon.add_argument("--channel", required=True)
    live_owner_click_abandon.add_argument("--evidence", type=Path, required=True)
    live_owner_click_abandon.add_argument("--logs-stopped", action="store_true")
    live_owner_click_abandon.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    live_owner_click_abandon.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    navigation_abandon = commands.add_parser(
        "abandon-navigation", help="isolate one uninspectable timed-out navigation without replay",
    )
    navigation_abandon.add_argument("--state", type=Path, default=DEFAULT_STATE)
    navigation_abandon.add_argument("--thread", required=True)
    navigation_abandon.add_argument("--query", required=True)
    navigation_abandon.add_argument("--channel", required=True)
    navigation_abandon.add_argument("--evidence", type=Path, required=True)
    navigation_abandon.add_argument("--logs-stopped", action="store_true")
    navigation_abandon.add_argument("--discovery-root", type=Path, default=DEFAULT_DISCOVERY)
    navigation_abandon.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)

    complete = commands.add_parser("complete-captured")
    complete.add_argument("--state", type=Path, default=DEFAULT_STATE)
    complete.add_argument("--thread", required=True)
    complete.add_argument("--batch", required=True)
    complete.add_argument("--logs-stopped", action="store_true")

    details = commands.add_parser("claim-detail", help="resume own batch or claim once; emit only remaining items")
    details.add_argument("--state", type=Path, default=DEFAULT_STATE)
    details.add_argument("--thread", required=True)
    details.add_argument("--limit", type=int, default=4)
    details.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)
    details.add_argument("--request", type=Path, help="explicit user title evidence under user-priority")
    details.add_argument("--query", help="exact title in --request; restrict new claims to matching queued details")
    details.add_argument("--exact-title", action="store_true",
                         help="with --request, require full normalized title equality for new claims")

    title_repair = commands.add_parser('repair-detail-title')
    title_repair.add_argument('--state', type=Path, default=DEFAULT_STATE)
    title_repair.add_argument('--thread', required=True)
    title_repair.add_argument('--batch', required=True)
    title_repair.add_argument('--evidence', type=Path, required=True)
    title_repair.add_argument('--source-tool', required=True)
    title_repair.add_argument('--logs-stopped', action='store_true')
    title_repair.add_argument('--taskbooks', type=Path, default=DEFAULT_TASKBOOKS)

    mixed = commands.add_parser("complete-detail", help="finalize exact captured/raw and declared failed/skipped results")
    mixed.add_argument("--state", type=Path, default=DEFAULT_STATE)
    mixed.add_argument("--thread", required=True)
    mixed.add_argument("--batch", required=True)
    mixed.add_argument("--taskbooks", type=Path, default=DEFAULT_TASKBOOKS)
    mixed.add_argument("--logs-stopped", action="store_true")
    mixed.add_argument("--failed", action="append", nargs=2, metavar=("URL", "REASON"), default=[])
    mixed.add_argument("--skipped", action="append", nargs=2, metavar=("URL", "REASON"), default=[])

    current = commands.add_parser('current')
    current.add_argument('--state', type=Path, default=DEFAULT_STATE)
    current.add_argument('--thread', required=True)

    repair_journals = commands.add_parser('repair-terminal-journals')
    repair_journals.add_argument('--state', type=Path, default=DEFAULT_STATE)
    repair_journals.add_argument('--thread', required=True)
    repair_journals.add_argument('--evidence', type=Path, required=True)
    repair_journals.add_argument('--logs-stopped', action='store_true')
    repair_journals.add_argument('--original-snapshot-call',
                                help='actual retained native paired-snapshot call; single omitted hash character only')

    monitor = commands.add_parser("snapshot")
    monitor.add_argument("--state", type=Path, default=DEFAULT_STATE)
    monitor.add_argument("--source", default="小小漫迷")
    monitor.add_argument(
        "--peer", action="append", default=[], metavar="LABEL=OWNER",
        help="limit thread detail to named peers; repeat for each peer",
    )
    return root


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    owner = getattr(args, 'thread', None)
    batch_id = getattr(args, 'batch', None)
    try:
        if owner and args.command not in ('current', 'snapshot'):
            import recoveryctl
            recoveryctl.guard(caller_thread=owner)
        if args.command == 'current':
            result = coordctl.current_batch(args.state, owner)
        elif args.command == 'repair-detail-title':
            result = repair_detail_title(args.state, owner, args.batch, args.evidence,
                                         args.source_tool, args.logs_stopped,
                                         taskbooks=args.taskbooks)
        elif args.command == 'repair-terminal-journals':
            result = repair_terminal_journals(args.state, owner, args.evidence,
                                             args.logs_stopped, args.original_snapshot_call)
        elif args.command == "claim-search":
            result = claim_search_task(
                args.state, args.thread, args.channel, query=args.query,
                task_id=args.task_id, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "admit-discovery-search":
            result = admit_discovery_search(
                args.state, args.thread, args.deduped, args.query, args.channel,
                task_id=args.task_id, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "admit-requested-search":
            result = admit_requested_search(
                args.state, args.thread, args.request, args.query, args.channel,
                task_id=args.task_id, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "commit-search":
            result = commit_search(
                args.state, args.thread, args.evidence,
                discovery_root=args.discovery_root, taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-search":
            result = abandon_search_unknown_action(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "quarantine-search-terminal":
            from search_terminal_quarantine import quarantine
            result = quarantine(args.state, args.thread, args.query, args.channel,
                args.evidence, args.logs_stopped, args.authorized,
                discovery_root=args.discovery_root, taskbooks=args.taskbooks,
                workflow=sys.modules[__name__])
        elif args.command == "quarantine-search":
            result = quarantine_search_unverifiable_result(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, args.allow_saved_candidate, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks, authorized_parent_host=args.authorized_parent_host,
                authorized_retained_action=args.authorized_retained_action,
            )
        elif args.command == "resume-misbound-search":
            result = resume_quarantined_misbound_search(
                args.state, args.thread, args.query, args.channel, args.evidence, args.logs_stopped,
                discovery_root=args.discovery_root, taskbooks=args.taskbooks)
        elif args.command == "quarantine-search-host-mismatch":
            result = quarantine_search_foreign_host_session(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "quarantine-search-foreign-host-input-mismatch":
            result = quarantine_search_foreign_host_input_mismatch(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "quarantine-search-foreign-host-stale-results":
            result = quarantine_search_foreign_host_stale_results(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "quarantine-search-legacy-evidence-gate":
            result = quarantine_search_legacy_evidence_gate(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "quarantine-search-host-tab-gone":
            result = quarantine_search_foreign_host_tab_gone(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "quarantine-search-confirmed-click-tab-gone":
            result = quarantine_search_confirmed_click_tab_gone(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-confirmed-click-no-outcome":
            result = abandon_confirmed_click_no_outcome(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-confirmed-click-stale-non-target-result":
            result = abandon_confirmed_click_stale_non_target_result(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-confirmed-click-helper-gate-error":
            result = abandon_confirmed_click_helper_gate_error(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "cancel-pre-search":
            result = cancel_pre_search_invalid_plan(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "claim-task-scope-mismatch":
            result = quarantine_claim_task_scope_mismatch(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "cancel-pre-search-session":
            result = cancel_pre_search_invalid_plan(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-input":
            result = abandon_uninspectable_input_action(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks, authorized_parent_input=args.authorized_parent_input,
            )
        elif args.command == "abandon-click":
            result = abandon_uninspectable_input_action(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-foreign-host-click-unknown":
            result = abandon_foreign_host_click_unknown(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-live-owner-click-unknown":
            result = abandon_live_owner_click_unknown(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "abandon-navigation":
            result = abandon_uninspectable_input_action(
                args.state, args.thread, args.query, args.channel, args.evidence,
                args.logs_stopped, discovery_root=args.discovery_root,
                taskbooks=args.taskbooks,
            )
        elif args.command == "complete-captured":
            result = complete_captured(
                args.state, args.thread, args.batch, args.logs_stopped,
            )
        elif args.command == "claim-detail":
            result = claim_detail_task(args.state, args.thread, args.limit, taskbooks=args.taskbooks,
                                       request_path=args.request, query=args.query, exact_title=args.exact_title)
        elif args.command == "complete-detail":
            result = complete_detail(args.state, args.thread, args.batch, args.logs_stopped,
                                     taskbooks=args.taskbooks, failed=args.failed, skipped=args.skipped)
        else:
            result = snapshot(args.state, args.source, args.peer)
    except Exception as error:
        if owner:
            try:
                import recoveryctl
                route = recoveryctl.record(owner, str(error), command=args.command, batch=batch_id)
                if args.command in ('complete-detail', 'complete-captured') and batch_id:
                    import production_live
                    production_live.notify_batch(owner, batch_id, args.state)
            except Exception:
                # The original formal failure remains authoritative. No fake ACK.
                pass
        print(compact_json({"ok": False, "command": args.command, "error": str(error)}), file=sys.stderr)
        return 1
    if owner and result.get('ok') and args.command in ('complete-detail', 'complete-captured', 'commit-search'):
        try:
            import production_live
            import recoveryctl
            if args.command == 'commit-search':
                result['live_sync'] = production_live.notify_search(owner, result['receipt'])
                recoveryctl.progress(owner, search_receipt=result['receipt'])
            else:
                result['live_sync'] = production_live.notify_batch(owner, batch_id, args.state)
                recoveryctl.progress(owner, batch=batch_id)
        except Exception as error:
            result['live_sync'] = {'ok': False, 'pending': True, 'error_type': type(error).__name__}
    print(compact_json(result))
    return 0 if result.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
