"""Read-only evidence counts for the collection goal; never collect or publish.

The audit count is a proven lower bound. Explicit user acceptance of a frozen
legacy set is counted separately and never presented as successful batch audits.
Batch audit evidence is joined to current database identities and the central
cover worker's complete, consistent validation feed. No cover files are accepted
merely because they exist. Host liveness must still be checked separately.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sys

import workflowctl

sys.path.insert(0, str(workflowctl.PROJECT_ROOT / "web"))
import coverctl


def timestamp(value):
    if isinstance(value, datetime):
        result = value
    elif isinstance(value, str):
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    else:
        raise ValueError("missing timestamp")
    if result.tzinfo is None:
        raise ValueError("timestamp requires timezone")
    return result.astimezone(timezone.utc)


def audited_pages(state):
    """Accept bound external audit evidence, including the explicit old contract."""
    pages = {}
    rejected = Counter()
    for batch_id, batch in state["batches"].items():
        if batch.get("state") != "acknowledged":
            continue
        results = batch.get("results", [])
        captured = [row.get("url") for row in results
                    if row.get("outcome") == "captured"]
        if not captured:
            continue
        verification = batch.get("verification") or {}
        evidence = verification.get("evidence") or {}
        assigned = batch.get("item_urls", [])
        modern = (verification.get("sync_ok") is True
                  and verification.get("audit_ok") is True
                  and type(evidence.get("checked_items")) is int
                  and evidence["checked_items"] == len(results)
                  and bool(evidence.get("method"))
                  and bool(evidence.get("summary")))
        legacy = (type(verification.get("captured_pages_passed")) is int
                  and verification["captured_pages_passed"] == len(captured)
                  and type(verification.get("error_count")) is int
                  and verification["error_count"] == 0
                  and type(verification.get("resources_checked")) is int
                  and verification["resources_checked"] > 0)
        if (verification.get("batch_id") != batch_id
                or verification.get("verified") is not True
                or not verification.get("verifier")
                or len(results) != len(assigned)
                or {row.get("url") for row in results} != set(assigned)
                or len(set(captured)) != len(captured)
                or any(not isinstance(url, str) or not url for url in captured)
                or not (modern or legacy)):
            rejected["incomplete_audit"] += len(captured)
            continue
        try:
            acknowledged_at = timestamp(batch.get("acknowledged_at"))
        except (ValueError, TypeError):
            rejected["invalid_ack_time"] += len(captured)
            continue
        for url in captured:
            current = pages.get(url)
            if current is None or acknowledged_at > current["at"]:
                pages[url] = {"at": acknowledged_at, "batch": batch_id,
                              "owner": batch.get("thread"),
                              "first_at": current["first_at"] if current else acknowledged_at,
                              "first_owner": current["first_owner"] if current else batch.get("thread")}
            if current is not None and acknowledged_at < pages[url]["first_at"]:
                pages[url]["first_at"] = acknowledged_at
                pages[url]["first_owner"] = batch.get("thread")
    return pages, dict(rejected)


def cover_feed():
    transient = {"inconsistent_status_total", "inconsistent_cover_snapshot",
                 "inconsistent_cover_counts"}
    # A live catalogue refresh may finish between pages. Restart the complete
    # read at most twice; never combine pages from different validation tokens.
    for attempt in range(3):
        rows = []

        def loader(page, page_size):
            payload = coverctl.request_status_page(page, page_size)
            if isinstance(payload, dict) and isinstance(payload.get("items"), list):
                rows.extend(payload["items"])
            return payload

        try:
            coverctl.status_snapshot(page_loader=loader)
        except coverctl.StatusError as error:
            if str(error) not in transient or attempt == 2:
                raise
        else:
            return rows
    raise coverctl.StatusError("inconsistent_cover_snapshot")


def database_rows(source):
    # Reuse the supported hidden local Docker client, with no new dependency
    # or credential-bearing child command. This transaction cannot write data.
    return workflowctl.dbctl.sql_json(f"""
        BEGIN READ ONLY;
        SET LOCAL statement_timeout='30s';
        SELECT COALESCE(json_agg(selected),'[]'::json) FROM (
            SELECT p.id,p.source_url,p.title,p.source_name,p.captured_at,
                   count(r.id)::int AS resource_count,
                   max(r.last_seen) AS resource_last_seen
            FROM programs p LEFT JOIN resources r ON r.program_id=p.id
            WHERE p.source_name={workflowctl.dbctl.literal(source)}
            GROUP BY p.id
        ) selected;
        ROLLBACK;
    """)


def row_fingerprint(row):
    fields = {key: row.get(key) for key in (
        "id", "source_url", "source_name", "title", "captured_at",
        "resource_count", "resource_last_seen")}
    return hashlib.sha256(json.dumps(fields, ensure_ascii=False, sort_keys=True,
                                     separators=(",", ":")).encode()).hexdigest()


def current_cover_valid(row, cover, source):
    return (cover is not None and cover.get("source") == source
            and cover.get("captured_at") is not None
            and cover.get("resource_count", 0) == row["resource_count"]
            and cover.get("title") == row["title"]
            and cover.get("thumbnail") == f'/api/thumbnails/{row["id"]}')


def audit_failure(row, audit, cover, source):
    if row.get("source_name") != source or row.get("resource_count", 0) <= 0:
        return "invalid_database_program"
    if audit is None:
        return "missing_batch_audit"
    try:
        changed = max(timestamp(row["captured_at"]), timestamp(row["resource_last_seen"]))
    except (ValueError, TypeError):
        return "invalid_database_timestamp"
    if changed > audit["at"]:
        return "database_newer_than_audit"
    if not current_cover_valid(row, cover, source):
        return "missing_current_cover_validation"
    return None


def qualification_gap_items(state, rows, covers, source="小小漫迷"):
    audits, _ = audited_pages(state)
    by_id = {row["id"]: row for row in covers}
    items = []
    for row in rows:
        if row.get("captured_at") is None:
            continue
        reason = audit_failure(row, audits.get(row["source_url"]), by_id.get(row["id"]), source)
        if reason:
            items.append({"program_id": row["id"], "source_url": row["source_url"],
                          "row_sha256": row_fingerprint(row), "audit_gap": reason})
    return sorted(items, key=lambda item: item["source_url"])


def accepted_entries(acceptance, source):
    if acceptance is None:
        return {}
    if (acceptance.get("version") != 1 or acceptance.get("authority") != "user"
            or not isinstance(acceptance.get("source"), str) or not acceptance.get("decision_basis")
            or acceptance.get("production_new_increment") is not False):
        raise ValueError("invalid user qualification acceptance")
    if acceptance["source"] != source:
        return {}
    timestamp(acceptance.get("accepted_at"))
    items = acceptance.get("items")
    if not isinstance(items, list) or acceptance.get("count") != len(items):
        raise ValueError("invalid user qualification acceptance items")
    result = {}
    for item in items:
        url = item.get("source_url")
        if (not isinstance(url, str) or not url or url in result
                or type(item.get("program_id")) is not int
                or not isinstance(item.get("row_sha256"), str)
                or len(item["row_sha256"]) != 64
                or item.get("audit_gap") not in {"missing_batch_audit", "database_newer_than_audit"}):
            raise ValueError("invalid or duplicate accepted programme")
        result[url] = item
    return result


def summarize(state, rows, covers, *, source="小小漫迷", target=10000,
              since=None, peers=None, acceptance=None):
    audits, rejected = audited_pages(state)
    by_id = {row["id"]: row for row in covers}
    captured = [row for row in rows if row.get("captured_at") is not None]
    if len({row["source_url"] for row in rows}) != len(rows):
        raise ValueError("duplicate database source URL")
    failures = Counter()
    qualified = set()
    accepted = set()
    overrides = accepted_entries(acceptance, source)
    remaining_failures = Counter()
    increments = Counter()
    threshold_time = timestamp(since) if since else None
    for row in captured:
        url = row["source_url"]
        audit = audits.get(url)
        cover = by_id.get(row["id"])
        reason = audit_failure(row, audit, cover, source)
        if reason:
            failures[reason] += 1
            override = overrides.get(url)
            if (reason in {"missing_batch_audit", "database_newer_than_audit"}
                    and override is not None and override["program_id"] == row["id"]
                    and override["row_sha256"] == row_fingerprint(row)
                    and current_cover_valid(row, cover, source)):
                accepted.add(url)
            else:
                remaining_failures[reason] += 1
            continue
        qualified.add(url)
        if threshold_time and audit["first_at"] > threshold_time and url not in overrides:
            increments[audit["first_owner"]] += 1
    pending_cover_rows = [row for row in captured
                          if by_id.get(row["id"], {}).get("source") != source
                          or by_id.get(row["id"], {}).get("thumbnail") != f'/api/thumbnails/{row["id"]}']
    pending_covers = len(pending_cover_rows)
    inflight = Counter(batch.get("state", "unknown")
                       for batch in state["batches"].values()
                       if batch.get("state") not in {"acknowledged", "released"})
    threads = {}
    row_titles = {row["source_url"]: row["title"] for row in rows}
    for label, owner in peers or []:
        registration = state["threads"].get(owner)
        if registration is None:
            raise ValueError(f"unregistered peer: {label}")
        batch_id = registration.get("active_batch")
        batch = state["batches"].get(batch_id, {})
        urls = batch.get("item_urls", [])
        threads[label] = {
            "owner": owner, "mode": registration.get("mode"), "batch": batch_id,
            "batch_state": batch.get("state"), "items": len(urls),
            "titles": [row_titles.get(url, "未知") for url in urls],
            "qualified_since": increments[owner] if since else None,
            "host_liveness": "not_checked",
        }
    return {
        "ok": True, "source": source, "target": target,
        "qualified_programs_lower_bound": len(qualified),
        "qualified_programs": len(qualified | accepted),
        "audited_qualified_programs": len(qualified),
        "user_accepted_programs": len(accepted),
        "qualification_policy": "audited_or_explicit_user_accepted",
        "captured_programs": len(captured), "discovered_programs": len(rows),
        "unqualified_captured": len(captured) - len(qualified | accepted),
        "audit_unqualified_captured": len(captured) - len(qualified),
        "remaining_qualification_gaps": dict(remaining_failures),
        "qualification_gaps": dict(failures), "rejected_audit_items": rejected,
        "cover_pending": pending_covers, "inflight_batches": dict(inflight),
        "cover_pending_sample": [{"program_id": row["id"], "title": row["title"],
                                  "source_url": row["source_url"]}
                                 for row in pending_cover_rows[:8]],
        "qualified_since": sum(increments.values()) if since else None,
        "since": since, "threads": threads,
        "data_gates_satisfied": len(qualified | accepted) >= target and not inflight and pending_covers == 0,
        "host_stop_required_before_completion": True,
    }


def check(state_path, **kwargs):
    before = state_path.read_bytes()
    state = json.loads(before)
    covers = cover_feed()
    rows = database_rows(kwargs.get("source", "小小漫迷"))
    acceptance_path = state_path.parent / "user-qualification-acceptance.json"
    acceptance_before = acceptance_path.read_bytes() if acceptance_path.exists() else None
    acceptance = json.loads(acceptance_before) if acceptance_before is not None else None
    result = summarize(state, rows, covers, acceptance=acceptance, **kwargs)
    acceptance_after = acceptance_path.read_bytes() if acceptance_path.exists() else None
    result["user_acceptance_sha256"] = hashlib.sha256(acceptance_before).hexdigest() if acceptance_before else None
    result["state_sha256"] = hashlib.sha256(before).hexdigest()
    result["sampling_changed"] = before != state_path.read_bytes() or acceptance_before != acceptance_after
    if result["sampling_changed"]:
        result["data_gates_satisfied"] = False
    result["at"] = workflowctl.utc_now()
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", type=Path, default=workflowctl.DEFAULT_STATE)
    parser.add_argument("--source", default="小小漫迷")
    parser.add_argument("--target", type=int, default=10000)
    parser.add_argument("--since", help="timezone-qualified report window start")
    parser.add_argument("--peer", action="append", default=[])
    args = parser.parse_args(argv)
    try:
        if args.target < 1 or not args.source.strip():
            raise ValueError("target and source must be nonempty")
        result = check(args.state, source=args.source.strip(), target=args.target,
                       since=args.since, peers=workflowctl._peer_specs(args.peer))
    except Exception as error:
        result = {"ok": False, "error_type": type(error).__name__, "reason": str(error)}
    print(workflowctl.compact_json(result))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
