"""Historical frozen-round recovery; normal publishing uses production_live.py."""

import argparse
import copy
import hashlib
import io
import json
import re
import tarfile
from pathlib import Path

from production_sync import (COVERS, PROGRAM_COLUMNS, RESOURCE_COLUMNS, RUNTIME, STATE,
                             Image, WorkerLock, db_options, export_csv, psycopg,
                             save_json, utc_now, verified_items)

LEDGER = RUNTIME / "round-ledger.json"
ROUND_SIZE = 300
LEGACY_ROUND_SIZE = 100


def valid_release_size(release):
    # Missing threshold belongs to a pre-migration frozen round. Never resize
    # it or rewrite its hash-bound package/receipt just because policy changed.
    threshold = release.get('programme_threshold', LEGACY_ROUND_SIZE)
    count = len(release['programmes'])
    if type(threshold) is not int or threshold not in (LEGACY_ROUND_SIZE, ROUND_SIZE):
        return False
    mode = release.get('release_mode', 'standard')
    if mode == 'standard':
        return count == threshold
    return (mode == 'user_authorized_early' and 1 <= count < threshold
            and isinstance(release.get('authorization'), str)
            and bool(release['authorization'].strip()))


def checksum(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":")).encode()).hexdigest()


def initialize(baseline):
    raw = baseline.read_bytes()
    data = json.loads(raw.decode("utf-8"))
    rows = data["captured_baseline"]
    if (data.get("production_identity_baseline_verified") is not True
            or data.get("production_captured_count") != len(rows)
            or data.get("captured_count") != len(rows)
            or len({row["id"] for row in rows}) != len(rows)
            or len({row["source_url"] for row in rows}) != len(rows)):
        raise ValueError("Baseline must have a verified, unique production identity set")
    digest = hashlib.sha256(raw).hexdigest()
    if LEDGER.exists():
        ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        if ledger.get("baseline_sha256") != digest:
            raise ValueError("Existing baseline differs; never reset the pending counter")
        return ledger
    ledger = {"version": 1, "baseline_sha256": digest,
              "baseline": rows, "cutoff": data["resumed_at"],
              "first_success": {}, "rounds": [], "updated_at": utc_now()}
    save_json(LEDGER, ledger)
    return ledger


def accumulate(ledger, candidates):
    known = {row["source_url"] for row in ledger["baseline"]}
    baseline_ids = {row["source_url"]: row["id"] for row in ledger["baseline"]}
    ids = {row["id"]: row["source_url"] for row in ledger["baseline"]}
    ids.update({row["program_id"]: url for url, row in ledger["first_success"].items()})
    for row in sorted(candidates, key=lambda item: (item["acknowledged_at"], item["batch_id"], item["program_id"])):
        url, program_id = row["source_url"], row["program_id"]
        if url in baseline_ids and baseline_ids[url] != program_id:
            raise ValueError("Baseline source URL identity changed while counting")
        if program_id in ids and ids[program_id] != url:
            raise ValueError("Programme identity changed while counting")
        if url in ledger["first_success"]:
            if ledger["first_success"][url]["program_id"] != program_id:
                raise ValueError("Source URL identity changed while counting")
            continue
        if url not in known:
            ledger["first_success"][url] = row
            ids[program_id] = url


def freeze(ledger):
    active = next((row for row in ledger["rounds"]
                   if row["state"] not in {"verified", "cancelled_content_boundary"}), None)
    if active:
        if checksum(active["programmes"]) != active["manifest_sha256"]:
            raise ValueError("Frozen release manifest changed")
        return active
    published = {row["source_url"] for release in ledger["rounds"]
                 if release["state"] == "verified" for row in release["programmes"]}
    excluded = ledger.get("content_exclusions", {})
    pending = sorted((row for url, row in ledger["first_success"].items()
                      if url not in published and url not in excluded),
                     key=lambda row: (row["acknowledged_at"], row["batch_id"], row["program_id"]))
    if len(pending) < ROUND_SIZE:
        return None
    selected = pending[:ROUND_SIZE]
    digest = checksum(selected)
    active = {"release_id": f"round-{len(ledger['rounds']) + 1:06d}-{digest[:12]}",
              "state": "frozen", "manifest_sha256": digest, "programme_threshold": ROUND_SIZE,
              "programmes": selected, "frozen_at": utc_now(), "receipt": None}
    ledger["rounds"].append(active)
    return active


def exclude_content(ledger, evidence):
    """Cancel an unattempted round without changing its frozen identities or bundle."""
    boundaries = {"explicit_sexual_content", "sexual_content_involving_minors",
                  "unverified_sexual_content_version"}
    if (not isinstance(evidence, dict) or evidence.get("version") != 1
            or evidence.get("boundary") not in boundaries
            or evidence.get("remote_write_started") is not False
            or not all(isinstance(evidence.get(k), str) and evidence[k].strip()
                       for k in ("release_id", "basis", "checked_at", "metadata_source"))):
        raise ValueError("Content exclusion requires bounded safety evidence")
    release = next((r for r in ledger["rounds"]
                    if r["release_id"] == evidence["release_id"]), None)
    if (release is None or release["state"] != "frozen" or release.get("publish_started_at")
            or release.get("receipt")
            or any((RUNTIME / f'{release["release_id"]}{suffix}').exists()
                   for suffix in ("-failure.json", "-acceptance.json"))):
        raise ValueError("Only an unattempted frozen round can be excluded; preserve remote retry")
    if checksum(release["programmes"]) != release["manifest_sha256"]:
        raise ValueError("Frozen release manifest changed")
    ids = evidence.get("program_ids")
    selected = {r["program_id"]: r for r in release["programmes"]}
    if (not isinstance(ids, list) or not ids or any(type(i) is not int for i in ids)
            or len(ids) != len(set(ids)) or not set(ids).issubset(selected)):
        raise ValueError("Content exclusion must identify programmes in the frozen round")
    excluded = ledger.setdefault("content_exclusions", {})
    audit = {**copy.deepcopy(evidence), "evidence_sha256": checksum(evidence)}
    for program_id in ids:
        row = selected[program_id]
        excluded[row["source_url"]] = {"program_id": program_id, **audit}
    release["state"] = "cancelled_content_boundary"
    release["cancellation"] = audit
    return release


def freeze_early(ledger, count: int, reason: str):
    """Freeze every current sub-threshold item for one explicit user release.

    This is deliberately separate from ``freeze``: ordinary collection still
    requires exactly 300 programmes and callers must provide a durable reason.
    """
    if type(count) is not int or not 1 <= count < ROUND_SIZE:
        raise ValueError("Early release count must be 1..299")
    if not isinstance(reason, str) or not reason.strip():
        raise ValueError("Early release requires an explicit reason")
    active = next((row for row in ledger["rounds"]
                   if row["state"] not in {"verified", "cancelled_content_boundary"}), None)
    if active:
        raise ValueError("An existing release must be completed before an early release")
    published = {row["source_url"] for release in ledger["rounds"]
                 if release["state"] == "verified" for row in release["programmes"]}
    excluded = ledger.get("content_exclusions", {})
    pending = sorted((row for url, row in ledger["first_success"].items()
                      if url not in published and url not in excluded),
                     key=lambda row: (row["acknowledged_at"], row["batch_id"], row["program_id"]))
    if len(pending) != count:
        raise ValueError("Early release must freeze the complete current pending set")
    digest = checksum(pending)
    active = {"release_id": f"round-{len(ledger['rounds']) + 1:06d}-{digest[:12]}",
              "state": "frozen", "manifest_sha256": digest,
              "programmes": pending, "frozen_at": utc_now(), "receipt": None,
              "release_mode": "user_authorized_early", "authorization": reason.strip(),
              "programme_threshold": ROUND_SIZE}
    ledger["rounds"].append(active)
    return active


def scan(ledger):
    state = json.loads(STATE.read_text(encoding="utf-8"))
    candidates = []
    errors = []
    baseline_urls = {row["source_url"] for row in ledger["baseline"]}
    for batch_id, batch in sorted(state["batches"].items()):
        if batch.get("state") != "acknowledged" or batch.get("acknowledged_at", "") <= ledger["cutoff"]:
            continue
        if not any(row.get("outcome") == "captured" and row.get("url") not in baseline_urls
                   for row in batch.get("results", [])):
            continue
        try:
            items = verified_items(batch_id, batch)
            batch_candidates = []
            for program_id, item in items.items():
                if item["url"] in baseline_urls:
                    continue
                path = COVERS / f"{program_id}.jpg"
                if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                    raise ValueError("Verified cover missing or changed")
                batch_candidates.append({"program_id": program_id, "source_url": item["url"],
                                   "cover_sha256": item["sha256"], "batch_id": batch_id,
                                   "acknowledged_at": batch["acknowledged_at"]})
            candidates.extend(batch_candidates)
        except (ValueError, OSError, KeyError) as error:
            errors.append({"batch_id": batch_id, "reason": str(error)})
    if candidates:
        options = db_options()
        options["options"] = "-c default_transaction_read_only=on -c statement_timeout=60000"
        with psycopg.connect(**options) as connection, connection.cursor() as cursor:
            cursor.execute("SELECT id, source_url FROM programs p WHERE id = ANY(%s) "
                           "AND source_name = '小小漫迷' AND captured_at IS NOT NULL "
                           "AND EXISTS (SELECT 1 FROM resources r WHERE r.program_id=p.id)",
                           ([row["program_id"] for row in candidates],))
            actual = dict(cursor.fetchall())
        if any(actual.get(row["program_id"]) != row["source_url"] for row in candidates):
            raise ValueError("Current database does not match verified programme identities")
    accumulate(ledger, candidates)
    active = freeze(ledger)
    ledger["updated_at"] = utc_now()
    ledger["evidence_errors"] = errors
    save_json(LEDGER, ledger)
    published = sum(len(row["programmes"]) for row in ledger["rounds"] if row["state"] == "verified")
    eligible = sum(url not in ledger.get("content_exclusions", {})
                   for url in ledger["first_success"])
    return {"ok": True, "new_verified": eligible,
            "published": published, "pending": eligible - published,
            "content_excluded": len(ledger.get("content_exclusions", {})),
            "threshold": ROUND_SIZE, "active_release": active["release_id"] if active else None,
            "active_programmes": len(active['programmes']) if active else 0,
            "evidence_errors": errors, "production_written": False}


def package(ledger):
    """Freeze one consistent resource/cover snapshot; never invoke transport."""
    release = freeze(ledger)
    if release is None:
        raise ValueError("Fewer than 300 new verified programmes; no release package")
    programme_count = len(release["programmes"])
    if not valid_release_size(release):
        raise ValueError("Release programme count or authorization is invalid")
    folder = RUNTIME / 'round-bundles'
    folder.mkdir(parents=True, exist_ok=True)
    output = folder / (release['release_id'] + '.tar.gz')
    temporary = output.with_suffix('.tmp')
    if not output.exists() and release.get('bundle_sha256') and temporary.exists():
        if hashlib.sha256(temporary.read_bytes()).hexdigest() != release['bundle_sha256']:
            raise ValueError("Interrupted package does not match the durable release hash")
        temporary.replace(output)
    if output.exists():
        actual_hash = hashlib.sha256(output.read_bytes()).hexdigest()
        if actual_hash != release.get('bundle_sha256'):
            raise ValueError("Frozen release package differs from the durable ledger")
        return {"ok": True, "release_id": release['release_id'], "bundle": str(output),
                "bundle_sha256": actual_hash, "production_written": False}
    if release.get('bundle_sha256'):
        raise ValueError("Frozen release package is missing; never silently rebuild a retry")
    selected = {row['program_id']: row for row in release['programmes']}
    if len(selected) != programme_count:
        raise ValueError("Frozen release contains duplicate programme IDs")
    state = json.loads(STATE.read_text(encoding='utf-8'))
    for row in selected.values():
        batch = state['batches'][row['batch_id']]
        if batch.get('state') != 'acknowledged':
            raise ValueError("Frozen release batch is no longer acknowledged")
        evidence = verified_items(row['batch_id'], batch).get(row['program_id'])
        if evidence != {'url': row['source_url'], 'sha256': row['cover_sha256']}:
            raise ValueError("Frozen release evidence changed")
    ids = sorted(selected)
    options = db_options()
    options['options'] = '-c default_transaction_read_only=on -c statement_timeout=120000'
    with psycopg.connect(**options) as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute("SELECT id, source_url FROM programs p WHERE id = ANY(%s) "
                       "AND source_name='小小漫迷' AND captured_at IS NOT NULL "
                       "AND EXISTS (SELECT 1 FROM resources r WHERE r.program_id=p.id)", (ids,))
        actual = dict(cursor.fetchall())
        if actual != {program_id: row['source_url'] for program_id, row in selected.items()}:
            raise ValueError("Release programme/resource snapshot is incomplete or changed")
        programs, program_count = export_csv(cursor, 'programs', PROGRAM_COLUMNS, ids)
        resources, resource_count = export_csv(cursor, 'resources', RESOURCE_COLUMNS, ids)
    files = {'programs.csv': programs, 'resources.csv': resources}
    for program_id in ids:
        data = (COVERS / f'{program_id}.jpg').read_bytes()
        if hashlib.sha256(data).hexdigest() != selected[program_id]['cover_sha256']:
            raise ValueError("Release cover changed")
        with Image.open(io.BytesIO(data)) as image:
            image.verify()
        files[f'covers/{program_id}.jpg'] = data
    manifest = {'version': 1, 'release_id': release['release_id'],
                'identity_manifest_sha256': release['manifest_sha256'],
                'program_ids': ids, 'program_rows': program_count,
                'resource_rows': resource_count,
                'programmes': release['programmes'],
                'release_mode': release.get('release_mode', 'standard'),
                'files': {name: hashlib.sha256(data).hexdigest() for name, data in files.items()}}
    if 'programme_threshold' in release:
        manifest['programme_threshold'] = release['programme_threshold']
    files['manifest.json'] = (json.dumps(manifest, sort_keys=True, ensure_ascii=False) + '\n').encode()
    with tarfile.open(temporary, 'w:gz') as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o600, 0
            archive.addfile(member, io.BytesIO(data))
    release['bundle_sha256'] = hashlib.sha256(temporary.read_bytes()).hexdigest()
    release['resource_rows'] = resource_count
    release['package_manifest_sha256'] = hashlib.sha256(files['manifest.json']).hexdigest()
    save_json(LEDGER, ledger)
    temporary.replace(output)
    return {'ok': True, 'release_id': release['release_id'], 'bundle': str(output),
            'bundle_sha256': release['bundle_sha256'], 'program_rows': program_count,
            'resource_rows': resource_count, 'production_written': False}


def accept_receipt(ledger, receipt):
    """Record an independently checked production receipt, never publish data."""
    release = next((row for row in ledger['rounds']
                    if row['release_id'] == receipt.get('release_id')), None)
    if release is None or checksum(release['programmes']) != release['manifest_sha256']:
        raise ValueError('Production receipt does not identify the frozen release')
    if release['state'] == 'cancelled_content_boundary':
        raise ValueError('Cancelled content-boundary round cannot be accepted')
    if release['state'] == 'verified':
        if release['receipt'] != receipt:
            raise ValueError('Verified production receipt changed')
        return release
    programme_count = len(release['programmes'])
    if (receipt.get('ok') is not True or receipt.get('verified') is not True
            or not valid_release_size(release)
            or any(not isinstance(release.get(field), str)
                   or re.fullmatch(r'[0-9a-f]{64}', release[field]) is None
                   for field in ('bundle_sha256', 'package_manifest_sha256'))
            or type(release.get('resource_rows')) is not int or release['resource_rows'] < programme_count
            or receipt.get('bundle_sha256') != release['bundle_sha256']
            or receipt.get('identity_manifest_sha256') != release['manifest_sha256']
            or receipt.get('package_manifest_sha256') != release.get('package_manifest_sha256')
            or receipt.get('resource_rows') != release.get('resource_rows')
            or receipt.get('target') != 'ubuntu@43.134.92.99'
            or receipt.get('database') != 'manying'):
        raise ValueError('Production receipt is incomplete or differs from the frozen package')
    checks = receipt.get('checks') or {}
    required = ('database_backup_readable', 'application_backup_readable',
                'stable_programme_identities', 'resource_identities',
                'cover_hashes', 'health_api', 'programme_and_playback_api',
                'user_data_protected')
    if any(checks.get(name) is not True for name in required):
        raise ValueError('Production receipt is missing independent acceptance checks')
    if (not isinstance(receipt.get('database_backup'), str)
            or not receipt['database_backup'].startswith('/opt/manying/backups/')
            or not isinstance(receipt.get('application_backup'), str)
            or not receipt['application_backup'].startswith('/opt/manying/backups/')
            or not isinstance(receipt.get('verified_at'), str)
            or not receipt['verified_at']):
        raise ValueError('Production backup or verification evidence is missing')
    expected = {row['program_id']: (row['source_url'], row['cover_sha256'])
                for row in release['programmes']}
    actual = {}
    for row in receipt.get('programmes') or []:
        program_id = row.get('program_id')
        if type(program_id) is not int or program_id in actual:
            raise ValueError('Duplicate or invalid verified production programme')
        actual[program_id] = (row.get('source_url'), row.get('cover_sha256'))
    if actual != expected:
        raise ValueError('Production acceptance does not cover exactly the frozen programme set')
    release['receipt'] = copy.deepcopy(receipt)
    release['state'] = 'verified'
    release['verified_at'] = receipt['verified_at']
    return release


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("init", "scan", "freeze-early", "package", "accept-receipt", "exclude-content"))
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--receipt", type=Path)
    parser.add_argument("--count", type=int)
    parser.add_argument("--reason")
    parser.add_argument("--evidence", type=Path)
    args = parser.parse_args()
    with WorkerLock():
        from catalogue_align import policy_active, status as catalogue_status
        if policy_active() and args.command == 'scan':
            print(json.dumps(catalogue_status(), ensure_ascii=False, sort_keys=True))
            return
        if policy_active() and args.command in ('init','freeze-early','package','exclude-content'):
            parser.error('Catalogue policy is all records; use catalogue_align.py. Historical ledger is preserved read-only.')
        if args.command == "init":
            if args.baseline is None:
                parser.error("init requires --baseline")
            ledger = initialize(args.baseline)
        else:
            ledger = json.loads(LEDGER.read_text(encoding="utf-8"))
        if args.command == 'exclude-content':
            if args.evidence is None:
                parser.error('exclude-content requires --evidence')
            release = exclude_content(ledger, json.loads(args.evidence.read_text(encoding='utf-8')))
            save_json(LEDGER, ledger)
            result = {'ok': True, 'release_id': release['release_id'], 'status': release['state'],
                      'content_excluded': len(ledger['content_exclusions']), 'production_written': False}
        elif args.command == 'freeze-early':
            if args.count is None or args.reason is None:
                parser.error('freeze-early requires --count and --reason')
            release = freeze_early(ledger, args.count, args.reason)
            save_json(LEDGER, ledger)
            result = {'ok': True, 'release_id': release['release_id'],
                      'programmes': len(release['programmes']),
                      'release_mode': release['release_mode'],
                      'production_written': False}
        elif args.command == 'accept-receipt':
            if args.receipt is None:
                parser.error('accept-receipt requires --receipt')
            receipt = json.loads(args.receipt.read_text(encoding='utf-8'))
            if not any(row['release_id'] == receipt.get('release_id') and row['state'] == 'verified'
                       for row in ledger['rounds']):
                package(ledger)
            release = accept_receipt(ledger, receipt)
            save_json(LEDGER, ledger)
            result = {'ok': True, 'release_id': release['release_id'],
                      'status': 'production_verified', 'programmes': len(release['programmes']),
                      'production_written': False}
        else:
            result = package(ledger) if args.command == 'package' else scan(ledger)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
