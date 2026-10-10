"""Create a verified local production-baseline file from the live production catalogue.

This utility is deliberately read-only against production.  It exports only
programme identity pairs, verifies that each pair still maps to the same local
programme identity, then atomically writes an input accepted by
``production_round.py init``.
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import subprocess
import tempfile
from pathlib import Path

import psycopg

from production_sync import HOST, KEY, db_options, utc_now


REMOTE_IDENTITY_QUERY = (
    "sudo -n runuser -u postgres -- psql -X -q -t -A -d manying -c "
    "'COPY (SELECT id, source_url FROM public.programs ORDER BY id) "
    "TO STDOUT WITH (FORMAT csv);'"
)


def remote_identities() -> list[dict[str, object]]:
    result = subprocess.run(
        ["ssh", "-i", str(KEY), "-o", "BatchMode=yes",
         "-o", "StrictHostKeyChecking=yes", "-o", "ConnectTimeout=10",
         HOST, REMOTE_IDENTITY_QUERY],
        check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=60,
    )
    rows = [{"id": int(row[0]), "source_url": row[1]}
            for row in csv.reader(io.StringIO(result.stdout))]
    if (not rows or any(row["id"] <= 0 or not row["source_url"] for row in rows)
            or len({row["id"] for row in rows}) != len(rows)
            or len({row["source_url"] for row in rows}) != len(rows)):
        raise ValueError("Production identity export is not unique and complete")
    return rows


def verify_local(rows: list[dict[str, object]]) -> None:
    with psycopg.connect(**db_options()) as conn:
        local = {url: int(program_id) for program_id, url in conn.execute(
            "SELECT id, source_url FROM programs"
        )}
    missing = [row["source_url"] for row in rows if row["source_url"] not in local]
    changed = [row["source_url"] for row in rows
               if row["source_url"] in local and local[row["source_url"]] != row["id"]]
    if missing or changed:
        raise ValueError(
            f"Production identities do not match local catalogue: "
            f"missing={len(missing)}, changed={len(changed)}"
        )


def atomic_json(path: Path, value: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n",
                                     dir=path.parent, delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, sort_keys=True, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
        temporary = Path(stream.name)
    os.replace(temporary, path)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Read-only production identity export for ledger recovery"
    )
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--resumed-at", required=True,
                        help="UTC cutoff used to re-scan verified local batches")
    args = parser.parse_args()
    rows = remote_identities()
    verify_local(rows)
    atomic_json(args.output, {
        "production_identity_baseline_verified": True,
        "production_captured_count": len(rows),
        "captured_count": len(rows),
        "captured_baseline": rows,
        "resumed_at": args.resumed_at,
        "recovered_at": utc_now(),
        "recovery_method": "read_only_remote_identity_export_and_local_match",
    })
    print(json.dumps({"ok": True, "captured_count": len(rows),
                      "output": str(args.output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
