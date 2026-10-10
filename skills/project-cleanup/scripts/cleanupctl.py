"""Bounded project cleanup; default dry run. Never kills processes by name."""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import uuid
import zipfile
from urllib.parse import urlsplit

PROJECT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(PROJECT / "scripts/collection"))
import coordctl
import shared_discovery


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def require_child(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or parent not in path.parents:
        raise ValueError("target is not a strict child of the allowed directory")
    return path


def paths(project):
    runtime = Path(project).resolve() / "Datebase/xxmanmi-resource-db/.runtime"
    return runtime, runtime / "collection-coordination/state.json", runtime / "cleanup"


def safe_owner(owner):
    if not isinstance(owner, str) or not owner.strip() or any(c in owner for c in "/\\:") or owner in {".", ".."}:
        raise ValueError("owner must be a safe exact identity")
    return owner


def scoped_work(project, work):
    runtime, _, _ = paths(project)
    resolved = Path(work).resolve()
    legacy = Path(project).resolve() / ".runtime/workers"
    if resolved.parent == legacy.resolve():
        return require_child(resolved, legacy)
    return require_child(resolved, runtime)


def session_files(session_root, owner):
    try:
        if str(uuid.UUID(owner)) != owner:
            return []
    except ValueError:
        return []
    found = []
    for folder in ("sessions", "archived_sessions"):
        base = Path(session_root) / folder
        for path in base.rglob(f"*{owner}*.jsonl"):
            if path.is_symlink():
                raise ValueError("session link requires manual review")
            with path.open(encoding="utf-8") as stream:
                line = stream.readline(65537)
            if len(line) > 65536:
                raise ValueError("session metadata exceeds bound")
            value = json.loads(line)
            meta = value.get("payload", {})
            if value.get("type") == "session_meta" and meta.get("id") == owner:
                found.append((path, meta, folder == "archived_sessions"))
    return found


def inventory(project, session_root, protected):
    runtime, state_path, output = paths(project)
    state = coordctl._read_state(state_path)
    entries = []
    for owner, record in {**state.get("retired_threads", {}), **state["threads"]}.items():
        try:
            scoped_work(project, record["work"])
            in_scope = True
        except ValueError:
            in_scope = False
        files = session_files(session_root, owner) if in_scope and owner not in protected else []
        is_child = bool(files) and all(isinstance(meta.get("source"), dict)
                                      and "subagent" in meta["source"] for _, meta, _ in files)
        entries.append({
            "owner": owner, "work": record["work"], "registered": owner in state["threads"],
            "active_batch": record.get("active_batch"), "protected": owner in protected,
            "session_files": len(files), "archived_files": sum(item[2] for item in files),
            "session_delete_candidate": in_scope and is_child and owner not in protected,
            "session_absent": not files and owner not in protected,
        })
    report = {"at": coordctl.utc_now(), "project": str(Path(project).resolve()),
              "protected_threads": sorted(protected), "owners": entries}
    coordctl._atomic_write(output / "scan.json", report)
    return {"ok": True, "command": "scan", "registered": len(state["threads"]),
            "retired": len(state.get("retired_threads", {})),
            "inflight": sum(b["state"] in {"claimed", "ready"} for b in state["batches"].values()),
            "session_candidates": sum(e["session_delete_candidate"] for e in entries),
            "session_absent": sum(e["session_absent"] for e in entries),
            "report": str(output / "scan.json")}


def feishu_connection(protected):
    """Use installed bridge control; credentials stay only in child environment."""
    from codex_feishu_bridge.cli import _paths, _control_socket
    from codex_feishu_bridge.control import request_control
    async def connection():
        socket = _control_socket(_paths())
        status = await request_control(socket, "status")
        if (status.get("thread_id") not in protected or not status.get("task_running")
                or status.get("terminal_waiting_for_thread")):
            raise ValueError("bridge is not the confirmed active protected main session")
        context = await request_control(socket, "prepare_terminal", {})
        if context.get("mode") != "resume" or context.get("thread_id") != status["thread_id"]:
            raise ValueError("bridge context changed; do not start or switch sessions")
        endpoint = context.get("appserver_endpoint", "")
        token = context.get("appserver_token")
        if (urlsplit(endpoint).hostname not in {"127.0.0.1", "localhost", "::1"}
                or not endpoint.startswith("ws://") or not isinstance(token, str) or len(token) < 32):
            raise ValueError("bridge did not return a valid authenticated loopback endpoint")
        environment = os.environ.copy()
        environment["CODEX_FEISHU_REMOTE_TOKEN"] = token
        return endpoint, environment
    return asyncio.run(connection())


def delete_session(project, session_root, owner, protected, apply=False, codex_bin=None, remote=None,
                   reviewed_retired_session=False, feishu_host=False):
    safe_owner(owner)
    runtime, state_path, output = paths(project)
    state = coordctl._read_state(state_path)
    record = state["threads"].get(owner) or state.get("retired_threads", {}).get(owner)
    if not record or owner in protected:
        raise ValueError("unknown or protected owner")
    scoped_work(project, record["work"])
    files = session_files(session_root, owner)
    receipt_path = output / f"session-{owner}.json"
    if not files:
        previous = read_json(receipt_path) if receipt_path.exists() else {}
        return {"ok": previous.get("deleted") is True, "status": "already_absent",
                "owner": owner, "receipt": str(receipt_path)}
    if not all(isinstance(meta.get("source"), dict) and "subagent" in meta["source"]
               for _, meta, _ in files) and not (
                   reviewed_retired_session and owner in state.get("retired_threads", {})
                   and all(item[2] for item in files)):
        raise ValueError("non-child session requires separate explicit host review")
    if not apply:
        return {"ok": True, "dry_run": True, "owner": owner, "session_files": len(files)}
    binary = codex_bin or shutil.which("codex")
    if not binary:
        raise ValueError("Codex CLI not found; provide --codex-bin")
    options = ["--remote", remote] if remote else []
    environment = None
    if feishu_host:
        if remote:
            raise ValueError("choose --remote or --feishu-host, not both")
        endpoint, environment = feishu_connection(protected)
        options = ["--remote", endpoint, "--remote-auth-token-env", "CODEX_FEISHU_REMOTE_TOKEN"]
    receipt = {"owner": owner, "at": coordctl.utc_now(), "user_authorized": True, "deleted": False}
    # A process may return exit code zero with an Error: message; require both checks.
    for action, extra in (("archive", []), ("delete", ["--force"])):
        if action == "archive" and all(item[2] for item in files):
            continue
        result = subprocess.run([str(binary), action, *options, *extra, owner],
                                capture_output=True, text=True, encoding="utf-8", errors="replace",
                                timeout=25, env=environment,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        response = result.stdout + result.stderr
        success_word = "Archived session" if action == "archive" else "Deleted session"
        if result.returncode != 0 or "Error:" in response or success_word not in response:
            receipt.update(status="host_rejected", stage=action)
            coordctl._atomic_write(receipt_path, receipt)
            return {"ok": False, "owner": owner, "status": "host_rejected", "stage": action,
                    "receipt": str(receipt_path)}
    receipt["deleted"] = not session_files(session_root, owner)
    receipt["status"] = "deleted" if receipt["deleted"] else "delete_not_verified"
    coordctl._atomic_write(receipt_path, receipt)
    return {"ok": receipt["deleted"], "owner": owner, "status": receipt["status"], "receipt": str(receipt_path)}


def prepare_stop(project, session_root, owner, confirmation=None):
    safe_owner(owner)
    runtime, state_path, output = paths(project)
    state = coordctl._read_state(state_path)
    record = state["threads"].get(owner) or state.get("retired_threads", {}).get(owner)
    if not record:
        raise ValueError("unknown owner")
    work = scoped_work(project, record["work"])
    receipt_path = output / f"session-{owner}.json"
    session_receipt = read_json(receipt_path) if receipt_path.exists() else {}
    session_id = session_receipt.get("session_id", owner)
    if session_receipt.get("deleted") is True and not session_files(session_root, session_id):
        method = "supported Codex CLI deletion receipt and verified absence of exact session files"
        summary = "User requested all project collectors stopped and deleted; session deletion verified."
    elif confirmation and (confirmation.get("thread") == owner or owner in confirmation.get("owners", [])):
        if (confirmation.get("user_authorized") is not True or confirmation.get("owner_stopped") is not True
                or not confirmation.get("method") or not confirmation.get("summary")):
            raise ValueError("invalid stop confirmation")
        method, summary = confirmation["method"], confirmation["summary"]
    else:
        raise ValueError("no verified session deletion or explicit matched stop confirmation")
    evidence = {"thread": owner, "user_authorized": True, "owner_stopped": True,
                "method": method, "summary": summary,
                "journals": coordctl._snapshot_handoff_journals(str(work))}
    target = output / f"stop-{owner}.json"
    coordctl._atomic_write(target, evidence)
    return {"ok": True, "owner": owner, "evidence": str(target)}


def retire_owner(project, owner, evidence, apply=False, cancel_inflight=False):
    safe_owner(owner)
    runtime, state_path, output = paths(project)
    if (evidence.get("thread") != owner or evidence.get("user_authorized") is not True
            or evidence.get("owner_stopped") is not True
            or any(not isinstance(evidence.get(k), str) or not evidence[k].strip()
                   for k in ("method", "summary"))):
        raise ValueError("matching explicit authorization and stop evidence required")
    coordctl._handoff_journals(evidence.get("journals"), "journals")
    receipt_path = output / f"owner-{owner}.json"

    def operation(state):
        old = state["threads"].get(owner) or state.get("retired_threads", {}).get(owner)
        if not old:
            raise ValueError("unknown owner")
        work = scoped_work(project, old["work"])
        inflight = [(bid, b) for bid, b in state["batches"].items()
                    if b["thread"] == owner and b["state"] in {"claimed", "ready"}]
        if inflight and not cancel_inflight:
            raise ValueError("in-flight batch requires explicit cancellation or safe handoff")
        for _ in range(2):
            if coordctl._snapshot_handoff_journals(str(work)) != evidence["journals"]:
                raise ValueError("stopped journal snapshot changed")
        if not apply:
            return {"ok": True, "dry_run": True, "owner": owner, "cancel_batches": len(inflight)}, False
        for batch_id, batch in inflight:
            if old.get("active_batch") != batch_id:
                raise ValueError("active batch pointer mismatch")
            for key in batch["item_urls"]:
                item = state["items"][key]
                if item.get("batch") != batch_id or item["state"] not in {"claimed", "ready"}:
                    raise ValueError("item ownership mismatch")
        # Lock order: coordination then discovery; no discovery path waits on coordination.
        searches = shared_discovery.cancel_owner_searches(runtime / "title-discovery", owner, evidence)
        receipt = {"at": coordctl.utc_now(), "owner": owner, "original_registration": old,
                   "cancelled_batches": [{"batch_id": bid, **b} for bid, b in inflight], "evidence": evidence, **searches}
        if not receipt_path.exists() or owner in state["threads"]:
            coordctl._atomic_write(receipt_path, receipt)
        for batch_id, batch in inflight:
            for key in batch["item_urls"]:
                state["items"][key]["state"] = "manual_review"
            batch.update(state="released", released_at=coordctl.utc_now(), logs_stopped=True,
                         release_reason="user stopped collectors; cleanup cancellation; not captured or acknowledged")
        registered = owner in state["threads"]
        if registered:
            state.setdefault("retired_threads", {})[owner] = {
                **old, "mode": "idle", "active_batch": None, "retired_at": coordctl.utc_now(),
                "reason": "user-authorized project cleanup", "evidence": evidence,
                "cleanup_receipt": str(receipt_path),
            }
            del state["threads"][owner]
        return {"ok": True, "owner": owner, "retired": registered, "cancelled_batches": len(inflight),
                **searches, "receipt": str(receipt_path)}, registered or bool(inflight)

    return coordctl._update(state_path, operation, coordctl.DEFAULT_LOCK_TIMEOUT)


def file_snapshot(directory):
    values = []
    for path in sorted(directory.rglob("*")):
        if path.is_symlink() or (hasattr(path, "is_junction") and path.is_junction()):
            raise ValueError("links/junctions require separate review")
        if path.is_file():
            data = path.read_bytes()
            values.append({"name": path.relative_to(directory).as_posix(), "size": len(data),
                           "sha256": hashlib.sha256(data).hexdigest()})
    return values


def archive_work(project, session_root, owner, apply=False, remove_work=False):
    safe_owner(owner)
    runtime, state_path, output = paths(project)
    with coordctl.StateLock(state_path):
        state = coordctl._read_state(state_path)
        old = state.get("retired_threads", {}).get(owner)
        if owner in state["threads"] or not old:
            raise ValueError("work cleanup requires a retired owner")
        receipt_path = output / f"session-{owner}.json"
        session_receipt = read_json(receipt_path) if receipt_path.exists() else {}
        if session_receipt.get("deleted") is not True or session_files(session_root, session_receipt.get("session_id", owner)):
            raise ValueError("session deletion is not verified; preserve work")
        work = scoped_work(project, old["work"])
        safe_root = runtime.resolve() if runtime.resolve() in work.parents else (Path(project).resolve() / ".runtime/workers")
        relative = work.relative_to(safe_root)
        allowed = (len(relative.parts) == 2 and relative.parts[0] in {"workers", "collection-workers"}
                   or len(relative.parts) == 1 and (relative.name.startswith("catalog-resume-")
                       or relative.name in {f"collector-thread-{owner}", f"collection-work-{owner}"}
                       or (safe_root != runtime.resolve() and relative.name == owner)))
        if not allowed:
            raise ValueError("not an approved dedicated worker directory")
        for record in state["threads"].values():
            active = Path(record["work"]).resolve()
            if active == work or active in work.parents or work in active.parents:
                raise ValueError("work overlaps an active peer")
        archive_receipt = output / f"work-{owner}.json"
        if not work.exists():
            return {"ok": archive_receipt.exists(), "owner": owner, "status": "already_absent"}
        if Path(old["work"]).is_symlink() or (hasattr(Path(old["work"]), "is_junction") and Path(old["work"]).is_junction()):
            raise ValueError("worker directory is a link")
        snapshot = file_snapshot(work)
        if not apply:
            return {"ok": True, "dry_run": True, "owner": owner, "files": len(snapshot)}
        archive = output / "archives" / (owner + "-" + coordctl.retired_record_digest({"files": snapshot})[:16] + ".zip")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            with zipfile.ZipFile(archive, "x", compression=zipfile.ZIP_DEFLATED) as bundle:
                for item in snapshot:
                    bundle.write(work / item["name"], item["name"])
        with zipfile.ZipFile(archive) as bundle:
            actual = [{"name": i.filename, "size": i.file_size,
                       "sha256": hashlib.sha256(bundle.read(i)).hexdigest()} for i in bundle.infolist()]
        if actual != snapshot or file_snapshot(work) != snapshot:
            raise ValueError("archive/work verification changed; source preserved")
        receipt = {"owner": owner, "original_work": str(work), "archive": str(archive),
                   "files": snapshot, "verified": True, "removed": False}
        coordctl._atomic_write(archive_receipt, receipt)
        if remove_work:
            require_child(work, safe_root)
            shutil.rmtree(work)
            receipt["removed"] = True
            coordctl._atomic_write(archive_receipt, receipt)
        return {"ok": True, "owner": owner, "files": len(snapshot), "removed": receipt["removed"],
                "receipt": str(archive_receipt)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["scan", "pause", "resume", "session-delete", "prepare-stop", "retire-owner", "archive-work"])
    parser.add_argument("--project", type=Path, default=PROJECT)
    parser.add_argument("--session-root", type=Path, default=Path(os.environ.get("USERPROFILE", str(Path.home()))) / ".codex")
    parser.add_argument("--protect-thread", action="append", default=[])
    parser.add_argument("--owner")
    parser.add_argument("--evidence", type=Path)
    parser.add_argument("--codex-bin")
    parser.add_argument("--remote")
    parser.add_argument("--authorized", action="store_true")
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--cancel-inflight", action="store_true")
    parser.add_argument("--remove-work", action="store_true")
    parser.add_argument("--reviewed-retired-session", action="store_true")
    parser.add_argument("--feishu-host", action="store_true")
    args = parser.parse_args()
    try:
        if args.apply and not args.authorized:
            raise ValueError("--apply requires explicit --authorized")
        if args.command not in {"scan", "pause", "resume"} and not args.owner:
            raise ValueError("exact --owner required")
        if args.owner in args.protect_thread:
            raise ValueError("protected owner")
        if args.command in {"scan", "session-delete"} and not args.protect_thread:
            raise ValueError("--protect-thread must include the actual current main session")
        if args.command in {"pause", "resume"}:
            _, state_path, _ = paths(args.project)
            def change_mode(state):
                if args.apply:
                    state["collection_paused"] = args.command == "pause"
                    state["collection_control"] = {"at": coordctl.utc_now(), "user_authorized": True,
                                                   "mode": args.command}
                return {"ok": True, "command": args.command, "dry_run": not args.apply}, args.apply
            result = coordctl._update(state_path, change_mode, coordctl.DEFAULT_LOCK_TIMEOUT)
        elif args.command == "scan":
            result = inventory(args.project, args.session_root, set(args.protect_thread))
        elif args.command == "session-delete":
            result = delete_session(args.project, args.session_root, args.owner, set(args.protect_thread),
                                    args.apply, args.codex_bin, args.remote, args.reviewed_retired_session,
                                    args.feishu_host)
        elif args.command == "prepare-stop":
            result = prepare_stop(args.project, args.session_root, args.owner,
                                  read_json(args.evidence) if args.evidence else None)
        elif args.command == "retire-owner":
            if not args.evidence:
                raise ValueError("--evidence required")
            result = retire_owner(args.project, args.owner, read_json(args.evidence), args.apply, args.cancel_inflight)
        else:
            result = archive_work(args.project, args.session_root, args.owner, args.apply, args.remove_work)
        print(json.dumps(result, ensure_ascii=True, separators=(",", ":")))
        return 0 if result.get("ok") else 1
    except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=True), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
