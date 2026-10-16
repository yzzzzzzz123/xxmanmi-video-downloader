"""One production database, bounded staging, and accepted catalogue promotion."""
from contextlib import contextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import time
import urllib.request

import psycopg
from psycopg import sql
import collection_runtime as runtime
import coordctl

ROOT = runtime.PROJECT / '.runtime/collection-server'
CURRENT = ROOT / 'current-stage.json'
STATUS = ROOT / 'production-status.json'
TABLES = {
    'programs': (['id','source_url','title','update_status','source_name','category',
        'discovery_query','first_seen','last_seen','captured_at','episode_dom_count'], ['id']),
    'resources': (['id','program_id','original_url','url_sha256','episode_labels','lines',
        'media_type','first_seen','last_seen'], ['id']),
    'source_observations': (['program_id','collected_at','source_name','state','observation'],
        ['program_id','collected_at']),
}

def read(path, default=None):
    return json.loads(path.read_text()) if path.exists() else default

def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    coordctl._atomic_write(path, value)

def digest(cursor, schema, table, ids):
    columns, keys = TABLES[table]
    field = 'id' if table == 'programs' else 'program_id'
    query = sql.SQL('COPY (SELECT {} FROM {}.{} WHERE {}=ANY({}) ORDER BY {}) TO STDOUT WITH (FORMAT csv, HEADER true)').format(
        sql.SQL(',').join(map(sql.Identifier, columns)), sql.Identifier(schema),
        sql.Identifier(table), sql.Identifier(field), sql.Literal(ids),
        sql.SQL(',').join(map(sql.Identifier, keys)))
    buffer = io.BytesIO()
    with cursor.copy(query) as stream:
        for chunk in stream:
            buffer.write(chunk)
    data = buffer.getvalue()
    return {'sha256':hashlib.sha256(data).hexdigest(), 'bytes':len(data)}, data

def _prepare(owner, batch_id):
    import recoveryctl
    state = coordctl._read_state(recoveryctl.STATE)
    coordctl.require_collection_running(state)
    batch = state['batches'][batch_id]
    if batch['thread'] != owner or state['collection_active_owner'] != owner:
        raise ValueError('Stage must belong to the actual claimed collection batch')
    previous = read(CURRENT, {})
    if previous.get('owner') == owner and previous.get('batch') == batch_id:
        if previous.get('phase') not in {'prepared','committed','verified','cover_pending','empty'}:
            raise ValueError('Unresolved original staging operation')
        return previous
    if previous and previous.get('phase') not in {'verified','cover_pending','empty'}:
        raise ValueError('Settle the original staging batch before replacing it')
    urls = list(batch['item_urls'])
    folder = ROOT / 'batches' / batch_id
    folder.mkdir(parents=True, exist_ok=True)
    descriptor = {'owner':owner,'batch':batch_id,'urls':urls,'phase':'preparing',
                  'prepared_at':coordctl.utc_now(),'folder':str(folder)}
    save(CURRENT, descriptor)
    with psycopg.connect(**runtime.database_options(stage=True,write=True)) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                           ('manying:server-stage',))
            cursor.execute('TRUNCATE collector_stage.resources,collector_stage.source_observations,collector_stage.programs')
            cursor.execute('SELECT id FROM public.programs WHERE source_url=ANY(%s) ORDER BY id', (urls,))
            original_ids = [row[0] for row in cursor.fetchall()]
            for table, (columns, _) in TABLES.items():
                column_list = sql.SQL(',').join(map(sql.Identifier, columns))
                field = 'id' if table == 'programs' else 'program_id'
                cursor.execute(sql.SQL('INSERT INTO collector_stage.{} ({}) SELECT {} FROM public.{} WHERE {}=ANY(%s)').format(
                    sql.Identifier(table), column_list, column_list, sql.Identifier(table),
                    sql.Identifier(field)), (original_ids,))
                _, data = digest(cursor, 'public', table, original_ids)
                (folder / ('before-'+table+'.csv')).write_bytes(data)
            # Titles support the central cover worker's cross-catalogue duplicate gate.
            cursor.execute('SELECT id,title FROM public.programs')
            titles = {str(key):title for key,title in cursor.fetchall()}
    from config import RUNTIME
    save(RUNTIME/'cover-titles.json', titles)
    production_covers=Path(runtime.configuration()['production_covers'])
    (RUNTIME/'thumbnails').mkdir(parents=True,exist_ok=True)
    for key in original_ids:
        source=production_covers/f'{key}.jpg'
        if not source.is_file() or source.is_symlink():continue
        target=RUNTIME/'thumbnails'/f'{key}.jpg'
        temporary=target.with_name(target.name+'.seed.tmp')
        before=source.stat()
        shutil.copyfile(source,temporary)
        after=source.stat()
        if (before.st_size,before.st_mtime_ns)!=(after.st_size,after.st_mtime_ns):
            raise ValueError('Production cover changed during stage seeding')
        temporary.chmod(0o600);temporary.replace(target)
    descriptor.update(phase='prepared', original_ids=original_ids)
    save(CURRENT, descriptor)
    return descriptor

@contextmanager
def staged_batch(owner, batch_id):
    if not runtime.production():
        yield
        return
    import fcntl
    ROOT.mkdir(parents=True, exist_ok=True)
    with (ROOT/'staging.lock').open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        _prepare(owner,batch_id)
        previous = os.environ.get('MANYING_COLLECTION_STAGE')
        os.environ['MANYING_COLLECTION_STAGE'] = '1'
        try:
            yield
            finalize(owner,batch_id)
        finally:
            if previous is None:
                os.environ.pop('MANYING_COLLECTION_STAGE',None)
            else:
                os.environ['MANYING_COLLECTION_STAGE'] = previous

def _install_cover(source, target, expected, backup):
    if source.is_symlink() or target.is_symlink():
        raise ValueError('Cover links are not accepted for production promotion')
    if hashlib.sha256(source.read_bytes()).hexdigest() != expected:
        raise ValueError('Accepted stage cover changed')
    if target.exists() and hashlib.sha256(target.read_bytes()).hexdigest() == expected:
        return
    if target.exists():
        shutil.copyfile(target,backup)
    temporary = target.with_name(target.name+'.collector.tmp')
    shutil.copyfile(source,temporary)
    temporary.chmod(0o640)
    temporary.replace(target)

def finalize(owner,batch_id):
    import recoveryctl
    from production_sync import verified_items
    from config import RUNTIME
    current = read(CURRENT,{})
    if current.get('owner') != owner or current.get('batch') != batch_id:
        raise ValueError('Current stage identity changed')
    if current['phase'] == 'verified':
        return current
    state = coordctl._read_state(recoveryctl.STATE)
    batch = state['batches'][batch_id]
    if batch.get('state') != 'acknowledged':
        raise ValueError('No real resource/cover ACK for stage promotion')
    captured = [row for row in batch.get('results',[]) if row['outcome']=='captured']
    if not captured:
        current.update(phase='empty',settled_at=coordctl.utc_now())
        save(CURRENT,current)
        return current
    if batch.get('verification',{}).get('cover_pending_items'):
        current.update(phase='cover_pending',settled_at=coordctl.utc_now())
        save(CURRENT,current)
        return current
    items = verified_items(batch_id,batch)
    ids = sorted(items)
    folder = Path(current['folder'])
    expected = {}
    with psycopg.connect(**runtime.database_options(stage=True)) as connection:
        with connection.cursor() as cursor:
            cursor.execute('SELECT id,source_url,title FROM collector_stage.programs ORDER BY id')
            staged = cursor.fetchall()
            if {row[0] for row in staged} != set(ids) or any(items[key]['url'] != url for key,url,title in staged):
                raise ValueError('Stage contains programmes outside accepted batch')
            titles = {key:title for key,url,title in staged}
            for table in TABLES:
                expected[table], data = digest(cursor,'collector_stage',table,ids)
                (folder/('accepted-'+table+'.csv')).write_bytes(data)
    production_covers = Path(runtime.configuration()['production_covers'])
    current.update(expected=expected, program_ids=ids,
        covers={str(key):value['sha256'] for key,value in items.items()})
    save(CURRENT,current)
    if current['phase'] != 'committed':
        with psycopg.connect(**runtime.database_options(stage=True,write=True)) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                               ('xxmanmi:ingest:public',))
                cursor.execute('''SELECT count(*) FROM public.programs p JOIN collector_stage.programs s
                    ON p.id=s.id OR p.source_url=s.source_url
                    WHERE p.id<>s.id OR p.source_url<>s.source_url''')
                if cursor.fetchone()[0]:
                    raise ValueError('Production programme identity conflicts with accepted stage')
                cursor.execute('''SELECT count(*) FROM public.resources p JOIN collector_stage.resources s
                    ON p.id=s.id OR (p.program_id=s.program_id AND p.url_sha256=s.url_sha256)
                    WHERE p.id<>s.id OR p.program_id<>s.program_id OR p.url_sha256<>s.url_sha256''')
                if cursor.fetchone()[0]:
                    raise ValueError('Production resource identity conflicts with accepted stage')
                for key,value in items.items():
                    _install_cover(RUNTIME/'thumbnails'/f'{key}.jpg', production_covers/f'{key}.jpg',
                                   value['sha256'], folder/f'before-cover-{key}.jpg')
                for table,(columns,keys) in TABLES.items():
                    column_list = sql.SQL(',').join(map(sql.Identifier,columns))
                    assignments = sql.SQL(',').join(sql.SQL('{}=EXCLUDED.{}').format(
                        sql.Identifier(name),sql.Identifier(name)) for name in columns if name not in keys)
                    identity = sql.SQL('OVERRIDING SYSTEM VALUE ') if table in {'programs','resources'} else sql.SQL('')
                    cursor.execute(sql.SQL('INSERT INTO public.{} ({}) {}SELECT {} FROM collector_stage.{} WHERE true ON CONFLICT ({}) DO UPDATE SET {}').format(
                        sql.Identifier(table),column_list,identity,column_list,sql.Identifier(table),
                        sql.SQL(',').join(map(sql.Identifier,keys)),assignments))
        current.update(phase='committed',committed_at=coordctl.utc_now())
        save(CURRENT,current)
    actual = {}
    with psycopg.connect(**runtime.database_options()) as connection:
        with connection.cursor() as cursor:
            for table in TABLES:
                actual[table],_ = digest(cursor,'public',table,ids)
    if actual != expected:
        raise RuntimeError('Independent production field verification failed; original stage retained')
    for key,value in items.items():
        _install_cover(RUNTIME/'thumbnails'/f'{key}.jpg', production_covers/f'{key}.jpg',
                       value['sha256'], folder/f'before-cover-{key}.jpg')
        if hashlib.sha256((production_covers/f'{key}.jpg').read_bytes()).hexdigest() != value['sha256']:
            raise RuntimeError('Installed production cover differs from accepted stage')
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open('http://127.0.0.1:8088/api/health',timeout=8) as response:
        if json.load(response).get('ok') is not True:
            raise RuntimeError('Production application health check failed')
    receipt = {**current,'phase':'verified','verified_at':coordctl.utc_now(),
        'verification_scope':'delta','tables':actual,'backend':'production-direct',
        'new_programmes':len(set(ids)-set(current['original_ids'])),
        'updated_programmes':len(set(ids)&set(current['original_ids'])),
        'application_healthy':True,'production_covers_verified':True}
    save(folder/'production-verification.json',receipt)
    save(CURRENT,receipt)
    return receipt


def correct_reviewed_cover(owner, batch_id, rejection_path, selection_path, approved_sha256):
    """Correct one independently reviewed cover; preserve ACK and all catalogue rows."""
    import recoveryctl
    import workflowctl
    from config import RUNTIME
    from cover_sampling import sampling_hint
    from thumbnails import inspect_cover
    if not runtime.production() or os.environ.get('CODEX_THREAD_ID') != owner:
        raise ValueError('Actual server owner required')
    gate = recoveryctl.guard(caller_thread=owner)
    if gate.get('stopped') or gate.get('stop_required'):
        raise ValueError('Collection stop gate is closed')
    state = coordctl._read_state(recoveryctl.STATE)
    coordctl.require_collection_running(state)
    actor, batch = state['threads'][owner], state['batches'][batch_id]
    if (state['collection_active_owner'] != owner or actor.get('active_batch')
            or batch['thread'] != owner or batch.get('acknowledged_by') != owner
            or batch['state'] != 'acknowledged' or not batch['verification']['verified']):
        raise ValueError('Original settled ACK and owner required')
    work = Path(actor['work']).resolve()
    with workflowctl.work_lock(work):
        paths = [Path(rejection_path), Path(selection_path)]
        if any(p.is_symlink() or p.resolve().parent != work for p in paths):
            raise ValueError('Review evidence outside original work')
        rejected, selected = [read(p) for p in paths]
        if any(x.get('owner') != owner or x.get('batch') != batch_id for x in [rejected, selected]):
            raise ValueError('Review identity differs')
        pid = rejected['program_id']
        current = read(CURRENT, {})
        folder = ROOT / 'batches' / batch_id
        proof_path = folder / 'production-verification.json'
        original_proof = proof_path.read_bytes()
        if (current.get('owner') != owner or current.get('batch') != batch_id
                or current.get('phase') != 'verified' or current.get('program_ids') != [pid]
                or Path(current['folder']).resolve() != folder.resolve()
                or json.loads(original_proof) != current or selected.get('program_id') != pid):
            raise ValueError('Original production stage changed')
        result = selected['result']
        audit_path = Path(result['audit']['path'])
        if (audit_path.is_symlink()
                or audit_path.resolve().parent != (RUNTIME / 'cover-audits').resolve()
                or hashlib.sha256(audit_path.read_bytes()).hexdigest() != result['audit']['sha256']):
            raise ValueError('Central audit changed')
        audit = read(audit_path)
        observed = audit['result']
        keys = ['status', 'selection', 'artifact', 'selected_source_id',
                'selected_window_start', 'sampling_hint_sha256', 'direct_evidence']
        if (audit['program_id'] != pid or any(observed.get(k) != result.get(k) for k in keys)
                or observed.get('status') != 'selected' or observed.get('selection') != 'generated'
                or observed.get('decoded_candidates', 0) < 1
                or observed.get('direct_evidence', {}).get('verified') is not True
                or observed['direct_evidence']['source_url'] not in current['urls']):
            raise ValueError('Fresh decoded central cover with DIRECT evidence required')
        source = RUNTIME / 'thumbnails' / f'{pid}.jpg'
        target = Path(runtime.configuration()['production_covers']) / f'{pid}.jpg'
        old_sha, new_sha = rejected['rejected_sha256'], observed['artifact']['sha256']
        backup_image = Path(rejected['rejected_image_backup'])
        if (new_sha != approved_sha256 or len(new_sha) != 64 or new_sha == old_sha
                or source.is_symlink() or target.is_symlink()
                or source.parent.resolve() == target.parent.resolve()
                or hashlib.sha256(source.read_bytes()).hexdigest() != new_sha
                or inspect_cover(source) is None or backup_image.is_symlink()
                or backup_image.resolve().parent != work
                or hashlib.sha256(backup_image.read_bytes()).hexdigest() != old_sha):
            raise ValueError('Reviewed image changed or failed quality')
        rid = observed['selected_source_id']
        hint = sampling_hint(RUNTIME, pid, rejected['title'], rid)
        if (hint is None or hint['sha256'] != observed.get('sampling_hint_sha256')
                or hint['window'][0] != observed.get('selected_window_start')):
            raise ValueError('Reviewed window changed')
        with psycopg.connect(**runtime.database_options(stage=True)) as connection:
            with connection.cursor() as cursor:
                cursor.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',
                               ('xxmanmi:ingest:public',))
                cursor.execute('SELECT id,source_url,title FROM collector_stage.programs ORDER BY id')
                if cursor.fetchall() != [(pid, current['urls'][0], rejected['title'])]:
                    raise ValueError('Original staging programme changed')
                cursor.execute('SELECT id,program_id,media_type FROM collector_stage.resources WHERE id=%s', (rid,))
                if cursor.fetchone() not in [(rid, pid, 'm3u8'), (rid, pid, 'mp4')]:
                    raise ValueError('Decoded resource no longer belongs to stage')
                for table in TABLES:
                    frozen = (folder / ('accepted-' + table + '.csv')).read_bytes()
                    for schema in ['public', 'collector_stage']:
                        value, data = digest(cursor, schema, table, [pid])
                        if value != current['expected'][table] or data != frozen:
                            raise ValueError('Catalogue fields changed during correction')
                current_sha = current['covers'][str(pid)]
                target_sha = hashlib.sha256(target.read_bytes()).hexdigest()
                if current_sha == new_sha:
                    if target_sha != new_sha or not any(
                            x.get('old_sha256') == old_sha and x.get('new_sha256') == new_sha
                            for x in current.get('cover_corrections', [])):
                        raise ValueError('Existing correction is not verified')
                    return current
                if current_sha != old_sha or target_sha not in {old_sha, new_sha}:
                    raise ValueError('Production image changed since rejection')
                correction = {'owner': owner, 'batch': batch_id, 'program_id': pid,
                    'old_sha256': old_sha, 'new_sha256': new_sha, 'resource_id': rid,
                    'rejection_receipt': str(paths[0]),
                    'rejection_sha256': hashlib.sha256(paths[0].read_bytes()).hexdigest(),
                    'selection_receipt': str(paths[1]),
                    'selection_sha256': hashlib.sha256(paths[1].read_bytes()).hexdigest(),
                    'central_audit': result['audit'], 'selected_window_start': hint['window'][0],
                    'sampling_hint_sha256': hint['sha256'], 'ack_preserved': True,
                    'catalogue_rows_unchanged': True}
                intent_path = folder / f'cover-correction-{pid}-{new_sha[:16]}.json'
                prior = read(intent_path)
                if prior and any(prior.get(k) != v for k, v in correction.items()):
                    raise ValueError('Original correction intent changed')
                if target_sha == new_sha and not prior:
                    raise ValueError('Unknown production image write')
                save(intent_path, {**correction, 'phase': 'prepared'})
                proof_sha = hashlib.sha256(original_proof).hexdigest()
                archive = folder / f'before-cover-correction-{proof_sha}.json'
                if archive.exists() and archive.read_bytes() != original_proof:
                    raise ValueError('Original proof archive changed')
                if not archive.exists():
                    archive.write_bytes(original_proof)
                backup = folder / f'before-reviewed-cover-{pid}-{old_sha[:16]}.jpg'
                if backup.exists() and hashlib.sha256(backup.read_bytes()).hexdigest() != old_sha:
                    raise ValueError('Original cover backup changed')
                _install_cover(source, target, new_sha, backup)
                if hashlib.sha256(target.read_bytes()).hexdigest() != new_sha:
                    raise RuntimeError('Corrected image verification failed')
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        with opener.open('http://127.0.0.1:8088/api/health', timeout=8) as response:
            if json.load(response).get('ok') is not True:
                raise RuntimeError('Production application health check failed')
        correction.update(phase='verified', verified_at=coordctl.utc_now(),
                          original_production_verification=str(archive),
                          original_production_verification_sha256=proof_sha)
        save(intent_path, correction)
        current['covers'][str(pid)] = new_sha
        current.setdefault('cover_corrections', []).append(correction)
        current.update(verified_at=correction['verified_at'], application_healthy=True,
                       production_covers_verified=True)
        save(proof_path, current)
        save(CURRENT, current)
        return current


def status():
    receipts = []
    for path in (ROOT/'batches').glob('batch-*/production-verification.json'):
        value = read(path,{})
        if value.get('phase') == 'verified':
            receipts.append(value)
    return {'ok':True,'enabled':True,'mode':'production-direct','pending':0,
        'dirty_intents':0,'executor_live':False,'verified_events':sum(len(x['program_ids']) for x in receipts),
        'verified_releases':len(receipts),'last_verified_at':max((x['verified_at'] for x in receipts),default=None),
        'new_programmes':sum(x['new_programmes'] for x in receipts),
        'updated_programmes':sum(x['updated_programmes'] for x in receipts),'last_error':None,
        'stage':{k:v for k,v in read(CURRENT,{}).items() if k in {'owner','batch','phase','prepared_at'}}}
