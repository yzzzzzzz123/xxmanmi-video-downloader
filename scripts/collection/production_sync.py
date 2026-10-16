"""Shared production transport and historical receipt helpers.

Current publishing uses production_live.py / catalogue_align.py. The legacy
per-batch command stays disabled and cannot publish or install a scheduler.
"""

import csv
import io
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import psycopg
from PIL import Image
from collection_receipts import cover_receipt_bytes

PROJECT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT / "web"))
from config import RUNTIME as WEB_RUNTIME, db_options  # noqa: E402

STATE = PROJECT / "Datebase" / "xxmanmi-resource-db" / ".runtime" / "collection-coordination" / "state.json"
RUNTIME = PROJECT / ".runtime" / "production-sync"
LOCK = RUNTIME / "worker.lock"
KEY = Path.home() / ".ssh" / "manying_deploy_20260929"
HOST = "ubuntu@43.134.92.99"
COVERS = WEB_RUNTIME / "thumbnails"
BATCH_RE = re.compile(r"batch-[0-9]{6}")
SHA_RE = re.compile(r"[0-9a-f]{64}")
PROGRAM_COLUMNS = (
    "id", "source_url", "title", "update_status", "source_name", "category",
    "discovery_query", "first_seen", "last_seen", "captured_at", "episode_dom_count",
)
RESOURCE_COLUMNS = (
    "id", "program_id", "original_url", "url_sha256", "episode_labels", "lines",
    "media_type", "first_seen", "last_seen",
)


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="\n") as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def verified_items(batch_id, batch):
    if not BATCH_RE.fullmatch(batch_id):
        raise ValueError("Unexpected batch ID")
    verification = batch.get("verification") or {}
    if (verification.get("batch_id") != batch_id
            or verification.get("verified") is not True
            or any(verification.get(field) is not True
                   for field in ("sync_ok", "audit_ok", "cover_ok"))):
        raise ValueError("Batch verification incomplete")
    receipt = json.loads(cover_receipt_bytes(PROJECT, batch_id, batch))
    captured = {row["url"] for row in batch.get("results", [])
                if row.get("outcome") == "captured"}
    if (receipt.get("batch_id") != batch_id
            or receipt.get("complete") is not True
            or receipt.get("cover_checked_items") != len(captured)
            or len(receipt.get("items", [])) != len(captured)):
        raise ValueError("Cover receipt does not match captured pages")
    items = {}
    for row in receipt["items"]:
        program_id = row.get("program_id")
        url = row.get("source_url")
        artifact = row.get("artifact") or {}
        digest = artifact.get("sha256")
        if (type(program_id) is not int or program_id <= 0 or program_id in items
                or url not in captured or row.get("status") != "selected"
                or not isinstance(digest, str) or not SHA_RE.fullmatch(digest)):
            raise ValueError("Unverified or duplicate cover item")
        items[program_id] = {"url": url, "sha256": digest}
    if {item["url"] for item in items.values()} != captured:
        raise ValueError("Captured pages and cover items differ")
    return items


def export_csv(cursor, table, columns, ids):
    column_sql = ", ".join(columns)
    id_sql = ", ".join(str(value) for value in ids)
    field = "id" if table == "programs" else "program_id"
    statement = (f"COPY (SELECT {column_sql} FROM {table} WHERE {field} IN ({id_sql}) "
                 "ORDER BY id) TO STDOUT WITH (FORMAT csv, HEADER true)")
    output = io.BytesIO()
    with cursor.copy(statement) as source:
        while chunk := source.read():
            output.write(chunk)
    data = output.getvalue()
    with io.StringIO(data.decode("utf-8")) as text:
        reader = csv.DictReader(text)
        if tuple(reader.fieldnames or ()) != columns:
            raise ValueError("Unexpected exported CSV columns")
        count = sum(1 for _ in reader)
    return data, count


def hidden_options():
    if os.name != "nt":
        return {}
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = subprocess.SW_HIDE
    return {"creationflags": subprocess.CREATE_NO_WINDOW, "startupinfo": startup}


def run_command(args, timeout):
    result = subprocess.run(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout, **hidden_options())
    if result.returncode:
        raise RuntimeError(f"Production transport failed ({args[0]}, exit {result.returncode})")
    return result.stdout.decode("utf-8", errors="replace").strip()


class WorkerLock:
    def __enter__(self):
        RUNTIME.mkdir(parents=True, exist_ok=True)
        self.stream = LOCK.open("a+b")
        self.stream.seek(0)
        if not self.stream.read(1):
            self.stream.seek(0)
            self.stream.write(b"0")
            self.stream.flush()
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return self

    def __exit__(self, *_args):
        self.stream.seek(0)
        if os.name == "nt":
            import msvcrt
            msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.stream, fcntl.LOCK_UN)
        self.stream.close()


def sync_once(limit):
    raise RuntimeError(
        "Per-batch production publishing is retired. Use production_live.py "
        "for authorized events or catalogue_align.py for explicit catalogue alignment."
    )


if __name__ == "__main__":
    raise SystemExit("Use production_live.py or catalogue_align.py; per-batch publishing is retired.")
