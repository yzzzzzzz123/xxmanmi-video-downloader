"""Freeze and align the complete local catalogue, including unresolved records.

Only catalogue tables/covers are exported. Production user data is never imported.
"""
import argparse
import csv
import hashlib
import io
import json
import subprocess
import tarfile
from pathlib import Path

import psycopg
from production_sync import (COVERS, PROJECT, RUNTIME, HOST, KEY, WorkerLock, db_options,
                             hidden_options, run_command, save_json, utc_now)

csv.field_size_limit(40_000_000)  # Public DOM observation JSON may exceed CSV's 128 KiB default.

TABLES = {
    'programs': {'columns': ['id','source_url','title','update_status','source_name',
        'category','discovery_query','first_seen','last_seen','captured_at','episode_dom_count'], 'keys':['id']},
    'resources': {'columns':['id','program_id','original_url','url_sha256','episode_labels',
        'lines','media_type','first_seen','last_seen'], 'keys':['id']},
    'source_observations': {'columns':['program_id','collected_at','source_name','state','observation'],
        'keys':['program_id','collected_at']},
    'program_classifications': {'columns':['program_id','source_url','observed_title','main_category',
        'countries','genres','identity','evidence','curated_at'], 'keys':['program_id']},
}
CURRENT = RUNTIME / 'catalogue-alignment-current.json'
POLICY = RUNTIME / 'catalogue-sync-policy.json'
THRESHOLD = 300


def policy_active():
    return POLICY.exists() and json.loads(POLICY.read_text('utf-8')).get('policy') == 'all_catalogue_records'


def status():
    """Bounded local inventory: no recognition/audit filter and no remote write."""
    frozen = json.loads(CURRENT.read_text('utf-8')) if CURRENT.exists() else None
    baseline = set()
    if frozen and frozen['state'] == 'verified':
        path = Path(frozen['bundle'])
        if sha(path.read_bytes()) != frozen['bundle_sha256']:
            raise ValueError('Verified baseline bundle changed')
        with tarfile.open(path, 'r:gz') as archive:
            data = archive.extractfile('programs.csv').read()
        if sha(data) != frozen['manifest']['tables']['programs']['sha256']:
            raise ValueError('Verified baseline programme data changed')
        baseline = {row['source_url'] for row in csv.DictReader(io.StringIO(data.decode('utf-8'), newline=''))}
    options = db_options()
    options['options'] = '-c default_transaction_read_only=on -c statement_timeout=30000'
    with psycopg.connect(**options) as connection, connection.cursor() as cursor:
        cursor.execute('SELECT source_url FROM public.programs')
        current = {row[0] for row in cursor.fetchall()}
        cursor.execute('SELECT count(*) FROM programs WHERE captured_at IS NOT NULL')
        captured = cursor.fetchone()[0]
        cursor.execute('SELECT count(*) FROM resources')
        resources = cursor.fetchone()[0]
    pending = len(current - baseline)
    automation_path = PROJECT / '.runtime/main-agent/automation-policy.json'
    automation = json.loads(automation_path.read_text('utf-8')) if automation_path.exists() else {}
    event_mode = automation.get('sync', {}).get('mode') == 'event_delta'
    return {'ok':True, 'policy':'all_catalogue_records', 'local_programmes':len(current),
        'local_captured':captured, 'local_resources':resources, 'pending':pending,
        'new_since_last_alignment':pending, 'threshold':None if event_mode else THRESHOLD,
        'sync_mode':'event_delta' if event_mode else 'legacy_threshold',
        'should_sync':None if event_mode else (pending >= THRESHOLD or bool(frozen and frozen['state'] != 'verified')),
        'event_status_entry':'scripts/collection/production_live.py status' if event_mode else None,
        'active_release':frozen['release_id'] if frozen and frozen['state'] != 'verified' else None,
        'active_programmes':frozen['manifest']['tables']['programs']['rows'] if frozen and frozen['state'] != 'verified' else 0,
        'last_verified_programmes':len(baseline) if frozen and frozen['state'] == 'verified' else None,
        'last_verified_at':frozen.get('verified_at') if frozen and frozen['state'] == 'verified' else None,
        'production_inventory_is_current':False, 'production_written':False}


def sha(data):
    return hashlib.sha256(data).hexdigest()


def remote(mode, *args):
    library = Path(__file__).with_name('production_round_remote.py').read_text('utf-8')
    source = "legacy = {'__name__': 'catalogue_alignment_backup_library'}\nexec(" + repr(library) + ", legacy)\n"
    source += Path(__file__).with_name('catalogue_align_remote.py').read_text('utf-8')
    command = ['ssh','-i',str(KEY),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes',
               '-o','ConnectTimeout=10',HOST,'sudo -n python3 - ' + ' '.join([mode,*args])]
    result = subprocess.run(command, input=source.encode(), stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=900, **hidden_options())
    if result.returncode:
        # All remote failures are bounded diagnostics, never raw rows/SQL/credentials.
        safe = result.stderr.decode(errors='replace').strip().splitlines()
        raise RuntimeError('Catalogue alignment failed; snapshot retained: ' + (safe[-1][:230] if safe else str(result.returncode)))
    return json.loads(result.stdout.decode().strip().splitlines()[-1])


def clean_ids(program_ids):
    if program_ids is None:
        return None
    if any(type(value) is not int or value < 1 for value in program_ids):
        raise ValueError('Delta requires positive integer programme IDs')
    ids = sorted(set(program_ids))
    if not ids or len(ids) > 500 or any(type(value) is not int or value < 1 for value in ids):
        raise ValueError('Delta requires 1..500 real programme IDs')
    return ids


def snapshot(program_ids=None):
    program_ids = clean_ids(program_ids)
    files, tables = {}, {}
    options = db_options()
    options['options'] = '-c default_transaction_read_only=on -c statement_timeout=120000'
    with psycopg.connect(**options) as connection, connection.transaction(), connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY')
        cursor.execute("SET LOCAL TIME ZONE 'UTC'")
        for table, spec in TABLES.items():
            columns, keys = ','.join(spec['columns']), ','.join(spec['keys'])
            parent = 'id' if table == 'programs' else 'program_id'
            where = (' WHERE ' + parent + ' IN (' + ','.join(map(str, program_ids)) + ')'
                     if program_ids is not None else '')
            output = io.BytesIO()
            with cursor.copy(f'COPY (SELECT {columns} FROM public.{table}{where} ORDER BY {keys}) TO STDOUT WITH (FORMAT csv, HEADER true)') as stream:
                while data := stream.read(): output.write(data)
            data = output.getvalue()
            with io.StringIO(data.decode('utf-8'), newline='') as stream:
                reader = csv.DictReader(stream)
                if reader.fieldnames != spec['columns']: raise ValueError('Snapshot column mismatch')
                rows = list(reader)
            tables[table] = {**spec, 'rows':len(rows), 'sha256':sha(data)}
            files[table+'.csv'] = data
            if table == 'programs':
                programmes = rows
        if program_ids is not None and {int(row['id']) for row in programmes} != set(program_ids):
            raise ValueError('Delta IDs do not all exist in the current database')
        captured_where = (' AND id IN (' + ','.join(map(str, program_ids)) + ')'
                          if program_ids is not None else '')
        cursor.execute('SELECT count(*) FROM programs WHERE captured_at IS NOT NULL' + captured_where)
        captured = cursor.fetchone()[0]
    covers = {}
    for row in programmes:
        path = COVERS / (row['id'] + '.jpg')
        if path.is_file() and not path.is_symlink(): covers[row['id']] = sha(path.read_bytes())
    files['classification-schema.sql'] = (PROJECT / 'Datebase/xxmanmi-resource-db/classification-schema.sql').read_bytes()
    return files, tables, covers, captured


def rebind_frozen_delta(frozen, current_path):
    """Retain the exact payload when an application release invalidates its binding."""
    manifest = frozen['manifest']
    ids = clean_ids(manifest['program_ids'])
    before = remote('inspect', ','.join(map(str, ids)))
    application = before['application_directory']
    if application == manifest['application_before']:
        return None
    path = Path(frozen['bundle'])
    if sha(path.read_bytes()) != frozen['bundle_sha256']:
        raise ValueError('Frozen bundle changed before application rebind')
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        expected = set(manifest['files']) | {'manifest.json'}
        if (len(members) != len(expected) or {m.name for m in members} != expected
                or any(not m.isfile() for m in members)):
            raise ValueError('Frozen members changed before application rebind')
        files = {m.name: archive.extractfile(m).read() for m in members}
    if (sha(files['manifest.json']) != frozen['manifest_sha256']
            or json.loads(files['manifest.json']) != manifest
            or any(sha(files[name]) != digest for name, digest in manifest['files'].items())):
        raise ValueError('Frozen payload changed before application rebind')
    prepared_at = utc_now()
    identity = sha(json.dumps({'bundle': frozen['bundle_sha256'],
        'application': application, 'at': prepared_at}, sort_keys=True).encode())
    release_id = ('catalogue-' + prepared_at[:19].replace('-', '').replace(':', '')
                  .replace('T', '-') + '-' + identity[:12])
    manifest = {**manifest, 'release_id': release_id, 'application_before': application,
                'prepared_at': prepared_at, 'supersedes': frozen['release_id']}
    files['manifest.json'] = (json.dumps(manifest, ensure_ascii=False, sort_keys=True) + '\n').encode()
    replacement = path.parent / (release_id + '.tar.gz')
    with tarfile.open(replacement, 'x:gz') as archive:
        for name, data in sorted(files.items()):
            member = tarfile.TarInfo(name)
            member.size = len(data); member.mode = 0o600; member.mtime = 0
            archive.addfile(member, io.BytesIO(data))
    save_json(path.parent / (release_id + '-rebind.json'), {
        'snapshot': frozen, 'superseded_by': release_id, 'observed_at': prepared_at,
        'application_directory': application, 'reason': 'application_changed_since_freeze'})
    save_json(current_path, {'state': 'frozen', 'release_id': release_id,
        'bundle': str(replacement), 'bundle_sha256': sha(replacement.read_bytes()),
        'manifest_sha256': sha(files['manifest.json']), 'manifest': manifest})
    return {'ok': True, 'release_id': release_id, 'state': 'frozen',
            'supersedes': frozen['release_id'], 'payload_unchanged': True,
            'production_written': False}


def prepare(program_ids=None, *, current_path=None, event_revisions=None):
    import collection_runtime
    collection_runtime.require_write()
    if collection_runtime.production():
        raise RuntimeError('Server collection promotes accepted staged batches; SSH catalogue sync is disabled')
    current_path = Path(current_path or CURRENT)
    program_ids = clean_ids(program_ids)
    if current_path.exists():
        old = json.loads(current_path.read_text('utf-8'))
        if old.get('state') != 'verified':
            if (old.get('state') == 'frozen' and old['manifest'].get('sync_mode') == 'delta'
                    and program_ids == old['manifest'].get('program_ids')
                    and event_revisions and event_revisions == old['manifest'].get('event_revisions')):
                rebound = rebind_frozen_delta(old, current_path)
                if rebound:
                    return rebound
            return {'ok':True,'release_id':old['release_id'],'state':old['state'], 'retry_existing':True,
                    'production_written':False}
    files, tables, covers, captured = snapshot(program_ids)
    before = remote('inspect', *([','.join(map(str, program_ids))] if program_ids else []))
    if (all(before['tables'].get(name, {}).get('sha256') == spec['sha256']
            for name, spec in tables.items())
            and all(before['covers'].get(key) == value for key, value in covers.items())):
        return {'ok':True, 'state':'already_aligned', 'production_written':False,
                'rows':{name:spec['rows'] for name,spec in tables.items()}}
    identity = sha(json.dumps({'tables':tables,'covers':covers},sort_keys=True).encode())
    release_id = 'catalogue-' + utc_now()[:19].replace('-','').replace(':','').replace('T','-') + '-' + identity[:12]
    selected = {key:value for key,value in covers.items() if before['covers'].get(key) != value}
    for key in selected:
        data = (COVERS / (key+'.jpg')).read_bytes()
        if sha(data) != covers[key]: raise ValueError('Cover changed during snapshot')
        files['covers/'+key+'.jpg'] = data
    manifest = {'version':1,'release_id':release_id,'authorization':'User explicitly requires all local catalogue records aligned, including empty/dead/legacy/excluded records; no recognition filter.',
        'policy':'all_catalogue_records','target':HOST,'database':'manying','tables':tables,
        'captured_programmes':captured,'covers':covers,'cover_payload':selected,
        'files':{name:sha(data) for name,data in files.items()},'application_before':before['application_directory'],
        'prepared_at':utc_now()}
    if program_ids is not None:
        manifest.update(sync_mode='delta', program_ids=program_ids,
                        event_revisions=event_revisions or {})
    files['manifest.json'] = (json.dumps(manifest,ensure_ascii=False,sort_keys=True)+'\n').encode()
    folder = RUNTIME / 'catalogue-alignments'
    folder.mkdir(parents=True,exist_ok=True)
    path = folder / (release_id+'.tar.gz')
    with tarfile.open(path,'x:gz') as archive:
        for name,data in sorted(files.items()):
            member = tarfile.TarInfo(name); member.size=len(data); member.mode=0o600; member.mtime=0
            archive.addfile(member,io.BytesIO(data))
    save_json(current_path, {'state':'frozen','release_id':release_id,'bundle':str(path),'bundle_sha256':sha(path.read_bytes()),
        'manifest_sha256':sha(files['manifest.json']),'manifest':manifest})
    return {'ok':True,'release_id':release_id,'state':'frozen','rows':{k:v['rows'] for k,v in tables.items()},
            'captured_programmes':captured,'covers_to_align':len(selected),'production_written':False}


def publish(*, current_path=None):
    import collection_runtime
    collection_runtime.require_write()
    if collection_runtime.production():
        raise RuntimeError('Server collection promotes accepted staged batches; SSH catalogue sync is disabled')
    current_path = Path(current_path or CURRENT)
    frozen = json.loads(current_path.read_text('utf-8'))
    if frozen['state'] == 'verified':
        return {'ok':True,'release_id':frozen['release_id'],'already_verified':True,'production_written':False}
    path = Path(frozen['bundle'])
    if sha(path.read_bytes()) != frozen['bundle_sha256']: raise ValueError('Frozen bundle changed')
    common = ['-i',str(KEY),'-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10']
    run_command(['ssh',*common,HOST,'install -d -m 0700 /tmp/manying-sync-inbox'],30)
    target = '/tmp/manying-sync-inbox/'+path.name
    run_command(['scp','-q',*common,str(path),HOST+':'+target],300)
    receipt = remote('apply',target,frozen['bundle_sha256'],frozen['manifest_sha256'])
    required_checks = ('all_catalogue_fields_equal','all_catalogue_rows_equal','all_local_covers_equal',
        'application_backup_readable','application_unchanged','database_backup_readable',
        'health_and_playback_routes','user_data_protected','web_classification_permission')
    if frozen['manifest'].get('sync_mode') == 'delta':
        required_checks = ('delta_fields_equal','delta_rows_equal','delta_covers_equal', *required_checks[3:])
        if (receipt.get('verification_scope') != 'delta'
                or receipt.get('program_ids') != frozen['manifest']['program_ids']):
            raise ValueError('Production delta receipt scope differs')
    if (receipt.get('ok') is not True or receipt.get('release_id') != frozen['release_id']
            or receipt.get('bundle_sha256') != frozen['bundle_sha256']
            or receipt.get('manifest_sha256') != frozen['manifest_sha256']
            or any(receipt.get('tables',{}).get(table,{}).get('sha256') != spec['sha256']
                   or receipt.get('tables',{}).get(table,{}).get('rows') != spec['rows']
                   for table,spec in frozen['manifest']['tables'].items())
            or receipt.get('covers_checked') != len(frozen['manifest']['covers'])
            or receipt.get('captured_programmes') != frozen['manifest']['captured_programmes']
            or any(receipt.get('checks',{}).get(name) is not True for name in required_checks)):
        raise ValueError('Production alignment receipt incomplete')
    receipt_path = RUNTIME / 'catalogue-alignments' / (frozen['release_id']+'-acceptance.json')
    save_json(receipt_path,receipt)
    frozen.update(state='verified',receipt=str(receipt_path),verified_at=receipt['verified_at'])
    save_json(current_path,frozen)
    return {'ok':True,'release_id':frozen['release_id'],'state':'production_verified',
        'rows':{k:v['rows'] for k,v in receipt['tables'].items()},'captured_programmes':receipt['captured_programmes'],
        'covers':receipt['covers_checked'],'receipt':str(receipt_path),'production_written':True}


def verify(*, current_path=None):
    current_path = Path(current_path or CURRENT)
    frozen=json.loads(current_path.read_text('utf-8'))
    ids = clean_ids(frozen['manifest'].get('program_ids'))
    _, tables, covers, captured = snapshot(ids)
    actual=remote('inspect', *([','.join(map(str, ids))] if ids else []))
    comparison={table:{'local_rows':spec['rows'],'production_rows':actual['tables'].get(table,{}).get('rows'),
        'equal':spec['sha256']==actual['tables'].get(table,{}).get('sha256')} for table,spec in tables.items()}
    cover_diff=sum(actual['covers'].get(key)!=value for key,value in covers.items())
    application_unchanged=actual['application_directory']==frozen['manifest']['application_before']
    return {'ok':all(row['equal'] for row in comparison.values()) and cover_diff==0
            and captured==actual['captured_programmes'],
        'release_id':frozen['release_id'],'tables':comparison,'local_captured':captured,
        'production_captured':actual['captured_programmes'],'cover_mismatches':cover_diff,
        'application_unchanged':application_unchanged,
        'production_written':False}


def verify_frozen(current_path):
    """Read production independently against the unchanged local frozen bundle."""
    frozen = json.loads(Path(current_path).read_text('utf-8'))
    if sha(Path(frozen['bundle']).read_bytes()) != frozen['bundle_sha256']:
        raise ValueError('Frozen verification bundle changed')
    with tarfile.open(frozen['bundle'], 'r:gz') as archive:
        manifest_bytes = archive.extractfile('manifest.json').read()
    if (sha(manifest_bytes) != frozen['manifest_sha256']
            or json.loads(manifest_bytes) != frozen['manifest']):
        raise ValueError('Frozen verification manifest changed')
    manifest = frozen['manifest']
    ids = clean_ids(manifest.get('program_ids'))
    actual = remote('inspect', *([','.join(map(str, ids))] if ids else []))
    valid = (all(actual['tables'].get(t, {}).get('sha256') == s['sha256']
                 and actual['tables'].get(t, {}).get('rows') == s['rows']
                 for t, s in manifest['tables'].items())
             and all(actual['covers'].get(k) == v for k, v in manifest['covers'].items())
             and actual['captured_programmes'] == manifest['captured_programmes']
             and actual['application_directory'] == manifest['application_before'])
    if not valid:
        raise ValueError('Independent production read differs from the frozen delta')
    frozen['independent_verified_at'] = utc_now()
    save_json(Path(current_path), frozen)
    return {'ok': True, 'scope': 'delta' if ids else 'all',
            'programmes': len(ids) if ids else manifest['tables']['programs']['rows'],
            'release_id': frozen['release_id'], 'production_written': False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['status','prepare','publish','verify'])
    parser.add_argument('--authorized',action='store_true')
    args=parser.parse_args()
    if args.command in ('prepare','publish') and not args.authorized: parser.error('Explicit catalogue-alignment authorization required')
    with WorkerLock():
        result={'status':status,'prepare':prepare,'publish':publish,'verify':verify}[args.command]()
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
        return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
