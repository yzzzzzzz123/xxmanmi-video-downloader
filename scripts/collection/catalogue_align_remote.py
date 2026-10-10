"""SSH-stdin executor for a hash-frozen, all-record catalogue alignment.

The caller supplies the reviewed production backup library as ``legacy``.
"""
import csv
import fcntl
import grp
import hashlib
import io
import json
import os
from pathlib import Path
import pwd
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone

csv.field_size_limit(40_000_000)

TABLES={
 'programs':(['id','source_url','title','update_status','source_name','category','discovery_query','first_seen','last_seen','captured_at','episode_dom_count'],['id']),
 'resources':(['id','program_id','original_url','url_sha256','episode_labels','lines','media_type','first_seen','last_seen'],['id']),
 'source_observations':(['program_id','collected_at','source_name','state','observation'],['program_id','collected_at']),
 'program_classifications':(['program_id','source_url','observed_title','main_category','countries','genres','identity','evidence','curated_at'],['program_id'])}
BASE=Path('/opt/manying'); COVERS=Path('/var/lib/manying/runtime/thumbnails')


def sha(data): return hashlib.sha256(data).hexdigest()


def require(condition,message):
    if not condition: raise ValueError(message)


def psql(sql,timeout=240):
    result=subprocess.run(['runuser','-u','postgres','--','psql','-X','-q','-t','-A','-v','ON_ERROR_STOP=1','-d','manying'],input=sql.encode(),stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=timeout)
    if result.returncode:
        lines=result.stderr.decode(errors='replace').splitlines()
        safe=next((line for line in lines if line.startswith('ERROR:')),'psql failed')
        safe=re.sub(r'https?://\S+','[URL]',safe)
        raise RuntimeError(safe[:230])
    return result.stdout


def clean_ids(program_ids):
    if program_ids is None:
        return None
    require(isinstance(program_ids,list) and 0<len(program_ids)<=500
            and all(type(value) is int and value>0 for value in program_ids)
            and program_ids==sorted(set(program_ids)), 'Unsafe delta scope')
    return program_ids


def scope_sql(table, program_ids, alias=''):
    if program_ids is None:
        return ''
    column = 'id' if table=='programs' else 'program_id'
    return (alias+'.' if alias else '')+column+' IN ('+','.join(map(str,program_ids))+')'


def export_table(table,columns,keys,program_ids=None):
    where = ' WHERE '+scope_sql(table,program_ids) if program_ids else ''
    return psql("BEGIN READ ONLY; SET LOCAL TIME ZONE 'UTC'; SET LOCAL statement_timeout='120s'; "
        +f'COPY (SELECT {",".join(columns)} FROM public.{table}{where} ORDER BY {",".join(keys)}) TO STDOUT WITH (FORMAT csv, HEADER true); ROLLBACK;')


def inspect(program_ids=None):
    program_ids=clean_ids(program_ids)
    tables={}
    for table,(columns,keys) in TABLES.items():
        exists=json.loads(psql(f"SELECT to_regclass('public.{table}') IS NOT NULL;").decode().replace('t','true').replace('f','false'))
        if not exists:
            tables[table]={'rows':0,'sha256':None}; continue
        data=export_table(table,columns,keys,program_ids)
        rows=sum(1 for _ in csv.DictReader(io.StringIO(data.decode('utf-8'),newline='')))
        tables[table]={'rows':rows,'sha256':sha(data)}
    ids=program_ids if program_ids is not None else json.loads(psql("SELECT coalesce(json_agg(id),'[]') FROM programs;").decode())
    covers={}
    for program_id in ids:
        path=COVERS/(str(program_id)+'.jpg')
        if path.is_file() and not path.is_symlink(): covers[str(program_id)]=sha(path.read_bytes())
    captured_scope=' AND '+scope_sql('programs',program_ids) if program_ids else ''
    captured=int(psql('SELECT count(*) FROM programs WHERE captured_at IS NOT NULL'+captured_scope+';'))
    return {'tables':tables,'covers':covers,'captured_programmes':captured,'application_directory':str((BASE/'app').resolve())}


def user_counts():
    names=json.loads(psql("SELECT json_agg(tablename ORDER BY tablename) FROM pg_tables WHERE schemaname='public';"))
    counts={}
    for table in names:
        if table in TABLES: continue
        require(re.fullmatch(r'[a-z_][a-z0-9_]*',table),'Unknown table identifier')
        counts[table]=int(psql(f'SELECT count(*) FROM public.{table};'))
    return counts


def apply(path,bundle_hash,manifest_hash):
    require(path.parent==Path('/tmp/manying-sync-inbox') and not path.is_symlink(),'Unsafe inbox')
    require(path.is_file() and path.stat().st_size<1_000_000_000,'Missing or oversized snapshot')
    require(sha(path.read_bytes())==bundle_hash,'Bundle hash differs')
    with tarfile.open(path,'r:gz') as archive:
        members=archive.getmembers()
        require(all(m.isfile() and m.size<250_000_000 for m in members) and len({m.name for m in members})==len(members),'Unsafe archive')
        files={m.name:archive.extractfile(m).read() for m in members}
    require('manifest.json' in files and sha(files['manifest.json'])==manifest_hash,'Manifest hash differs')
    manifest=json.loads(files['manifest.json']); rid=manifest['release_id']
    delta=manifest.get('sync_mode')=='delta'
    ids=clean_ids(manifest.get('program_ids')) if delta else None
    require(delta or 'program_ids' not in manifest,'Unexpected delta scope')
    require(re.fullmatch(r'catalogue-\d{8}-\d{6}-[0-9a-f]{12}',rid),'Unsafe release identifier')
    require(manifest['policy']=='all_catalogue_records' and manifest['target']=='ubuntu@example.invalid' and manifest['database']=='manying','Scope or target differs')
    require(set(manifest['tables'])==set(TABLES),'Unexpected catalogue tables')
    require(set(files)=={'manifest.json',*manifest['files']},'Unexpected payload files')
    require(all(sha(files[name])==digest for name,digest in manifest['files'].items()),'Payload checksum differs')
    expected_names={table+'.csv' for table in TABLES}|{'classification-schema.sql'}|{'covers/'+key+'.jpg' for key in manifest['cover_payload']}
    require(set(manifest['files'])==expected_names,'Unexpected payload path')
    for key,digest in manifest['covers'].items():
        require(re.fullmatch(r'[1-9][0-9]*',key) and re.fullmatch(r'[0-9a-f]{64}',digest),'Unsafe cover identity')
    for table,(columns,keys) in TABLES.items():
        spec=manifest['tables'][table]
        require(spec['columns']==columns and spec['keys']==keys,'Unexpected table contract')
        data=files[table+'.csv']; require(sha(data)==spec['sha256'],'Snapshot table hash differs')
        reader=csv.DictReader(io.StringIO(data.decode('utf-8'),newline=''))
        rows=list(reader)
        require(reader.fieldnames==columns and len(rows)==spec['rows'],'Snapshot rows/columns differ')
        if delta:
            parent='id' if table=='programs' else 'program_id'
            require(all(int(row[parent]) in ids for row in rows),'Delta row outside programme scope')
            if table=='programs':
                require({int(row['id']) for row in rows}==set(ids),'Missing delta programmes')
    if delta:
        require(set(manifest['covers'])<=set(map(str,ids)),'Delta cover outside programme scope')
    with Path('/var/lib/manying/runtime/production-sync.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        require(legacy['run'](['systemctl','is-active','manying.service']).strip()==b'active','Production not active')
        require(str((BASE/'app').resolve())==manifest['application_before'],'Application changed since freeze')
        legacy['reviewed_triggers']()
        before=user_counts()
        database_size=int(psql("SELECT pg_database_size('manying');"))
        require(shutil.disk_usage(BASE).free>max(1_000_000_000,database_size*2+path.stat().st_size*3),'Insufficient backup/staging space')
        folder=BASE/'backups'/rid
        db_backup,app_backup=legacy['backup'](folder)
        receipt_path=folder/'catalogue-acceptance.json'
        covers_backup=folder/'replaced-covers'; covers_backup.mkdir(mode=0o700,exist_ok=True)
        pid=legacy['run'](['systemctl','show','manying.service','-p','MainPID','--value']).decode().strip()
        env={}
        for value in Path('/proc/'+pid+'/environ').read_bytes().split(b'\0'):
            if b'=' in value:
                key,value=value.split(b'=',1); env[key.decode()]=value.decode()
        role=env.get('MANYING_DB_USER')
        require(role is not None and re.fullmatch(r'[a-z_][a-z0-9_]*',role),'Unverified web database role')
        sql="BEGIN; SET LOCAL lock_timeout='10s'; SET LOCAL statement_timeout='240s'; SET LOCAL TIME ZONE 'UTC';\n"
        if not delta:
            sql+=files['classification-schema.sql'].decode()+'\n'
        else:
            require(psql(f"SELECT has_table_privilege('{role}','public.program_classifications','SELECT');").strip()==b't',
                    'Live sync cannot migrate schema or change application permissions')
        for table,(columns,keys) in TABLES.items():
            sql+=f'CREATE TEMP TABLE stage_{table} (LIKE public.{table} INCLUDING DEFAULTS) ON COMMIT DROP;\n'
            text=files[table+'.csv'].decode('utf-8')
            require('\\.' not in text.splitlines() and '\x00' not in text,'Unsafe COPY terminator')
            sql+=f'COPY stage_{table} ({",".join(columns)}) FROM STDIN WITH (FORMAT csv, HEADER true);\n'+text+'\\.\n'
            sql+=f'ANALYZE stage_{table};\n'
        sql+='''DO $guard$ BEGIN
 IF EXISTS(SELECT 1 FROM stage_programs s JOIN programs p USING(id) WHERE s.source_url<>p.source_url)
 OR EXISTS(SELECT 1 FROM stage_programs s JOIN programs p USING(source_url) WHERE s.id<>p.id)
 THEN RAISE EXCEPTION 'Programme identity conflict'; END IF;
 IF EXISTS(SELECT 1 FROM stage_resources s JOIN resources r USING(id)
 WHERE s.program_id<>r.program_id OR s.url_sha256<>r.url_sha256 OR s.original_url<>r.original_url)
 OR EXISTS(SELECT 1 FROM stage_resources s JOIN resources r USING(program_id,url_sha256) WHERE s.id<>r.id)
 THEN RAISE EXCEPTION 'Resource identity conflict'; END IF;
 IF EXISTS(SELECT 1 FROM stage_program_classifications s LEFT JOIN stage_programs p
 ON s.program_id=p.id WHERE p.id IS NULL)
 THEN RAISE EXCEPTION 'Classification parent identity conflict'; END IF;
 END $guard$;\n'''
        for table,(columns,keys) in TABLES.items():
            join=' AND '.join('p.'+key+'=s.'+key for key in keys)
            bounded=' AND '+scope_sql(table,ids,'p') if delta else ''
            sql+=f"DO $extras$ BEGIN IF EXISTS(SELECT 1 FROM public.{table} p LEFT JOIN stage_{table} s ON {join} WHERE s.{keys[0]} IS NULL{bounded}) THEN RAISE EXCEPTION 'Production-only {table} retained; no destructive alignment'; END IF; END $extras$;\n"
            fields=[c for c in columns if c not in keys]
            identity=' OVERRIDING SYSTEM VALUE' if table in ('programs','resources') else ''
            updates=','.join(c+'=EXCLUDED.'+c for c in fields)
            old_tuple=','.join('p.'+c for c in fields); new_tuple=','.join('EXCLUDED.'+c for c in fields)
            sql+=f'INSERT INTO public.{table} AS p ({",".join(columns)}){identity} SELECT {",".join(columns)} FROM stage_{table} WHERE true ON CONFLICT ({",".join(keys)}) DO UPDATE SET {updates} WHERE ({old_tuple}) IS DISTINCT FROM ({new_tuple});\n'
        for table in ('programs','resources'):
            sql+=f"SELECT setval(pg_get_serial_sequence('public.{table}','id'),greatest((SELECT max(id) FROM {table}),1),true);\n"
        if not delta:
            sql+=f'GRANT SELECT ON public.program_classifications TO {role};\n'
        sql+='COMMIT;\n'
        psql(sql,timeout=480)
        user=pwd.getpwnam('manying'); group=grp.getgrnam('manying')
        for key,digest in manifest['cover_payload'].items():
            data=files['covers/'+key+'.jpg']; require(sha(data)==digest,'Cover payload differs')
            target=COVERS/(key+'.jpg'); require(not target.is_symlink(),'Linked cover target')
            if target.exists() and sha(target.read_bytes())!=digest:
                saved=covers_backup/(key+'.jpg')
                if not saved.exists(): saved.write_bytes(target.read_bytes()); os.chmod(saved,0o600)
            if not target.exists() or sha(target.read_bytes())!=digest:
                temporary=COVERS/('.'+rid+'-'+key+'.tmp')
                with temporary.open('wb') as stream:
                    stream.write(data); stream.flush(); os.fsync(stream.fileno())
                os.chown(temporary,user.pw_uid,group.gr_gid); os.chmod(temporary,0o640); os.replace(temporary,target)
        actual=inspect(ids)
        require(all(actual['tables'][t]['sha256']==s['sha256'] and actual['tables'][t]['rows']==s['rows'] for t,s in manifest['tables'].items()),'Full row/field reconciliation failed')
        require(all(actual['covers'].get(k)==v for k,v in manifest['covers'].items()),'Full cover reconciliation failed')
        playable=[]
        for row in csv.DictReader(io.StringIO(files['programs.csv'].decode(),newline='')):
            if row['captured_at'] and row['id'] in manifest['cover_payload']: playable.append(int(row['id']))
        if not playable:
            playable=json.loads(psql('SELECT json_agg(id) FROM (SELECT id FROM programs WHERE captured_at IS NOT NULL ORDER BY id LIMIT 3) p;'))
        api=legacy['acceptance'](playable[:30])
        require(psql(f"SELECT has_table_privilege('{role}','public.program_classifications','SELECT');").strip()==b't','Website classification permission failed')
        after=user_counts()
        receipt={'ok':True,'release_id':rid,'bundle_sha256':bundle_hash,'manifest_sha256':manifest_hash,
          'verification_scope':'delta' if delta else 'all','program_ids':ids,
          'tables':actual['tables'],'captured_programmes':actual['captured_programmes'],'covers_checked':len(manifest['covers']),
          'database_backup':str(db_backup),'application_backup':str(app_backup),'application_directory':actual['application_directory'],
          'user_table_counts_before':before,'user_table_counts_after':after,'user_data_write_scope':'Only programs/resources/source_observations/program_classifications and catalogue sequences; no user table writes.',
          'checks':{'database_backup_readable':True,'application_backup_readable':True,'all_catalogue_rows_equal':not delta,
           'all_catalogue_fields_equal':not delta,'all_local_covers_equal':not delta,
           'delta_rows_equal':delta,'delta_fields_equal':delta,'delta_covers_equal':delta,'web_classification_permission':True,
           'health_and_playback_routes':True,'user_data_protected':True,'application_unchanged':actual['application_directory']==manifest['application_before']},
          'api_evidence':api,'verified_at':datetime.now(timezone.utc).isoformat()}
        with receipt_path.open('w',encoding='utf-8') as stream:
            json.dump(receipt,stream,ensure_ascii=False,sort_keys=True,indent=2); stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
        os.chmod(receipt_path,0o600)
        return receipt


if __name__=='__main__':
    try:
        result=inspect(clean_ids(sorted(set(map(int,sys.argv[2].split(','))))) if len(sys.argv)>2 else None) if sys.argv[1]=='inspect' else apply(Path(sys.argv[2]),sys.argv[3],sys.argv[4])
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    except Exception as exc:
        print(type(exc).__name__+': '+str(exc)[:230],file=sys.stderr)
        raise SystemExit(1)
