"""Delete one explicitly user-rejected, cover-blocked singleton programme.

Runs while collection is stopped. It preserves journals and the original ready
results, releases rather than ACKs the rejected batch, and never resumes agents.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess

import workflowctl as workflow
import recoveryctl
import production_live
from app_server_control import RPC
from process_stop import targets_for
from production_sync import WorkerLock, HOST, KEY, hidden_options

TABLES = ('resources', 'source_observations', 'program_classifications', 'programs')


def require(condition, message):
    if not condition:
        raise ValueError(message)


def snapshot_sql(program_id):
    fields = []
    for table in TABLES:
        key = 'id' if table == 'programs' else 'program_id'
        fields.append("'" + table + "',(SELECT coalesce(json_agg(row_to_json(x)),'[]'::json) "
                      f'FROM (SELECT * FROM public.{table} WHERE {key}={program_id}) x)')
    return 'SELECT json_build_object(' + ','.join(fields) + ');'


def delete_sql(program_id, source_url, snapshot):
    expected = workflow.dbctl.payload_sql(snapshot)
    source = workflow.dbctl.literal(source_url)
    check = snapshot_sql(program_id).removesuffix(';')
    statements = [f'DELETE FROM public.{table} WHERE '
                  + ('id' if table == 'programs' else 'program_id') + f'={program_id};'
                  for table in TABLES]
    return f"""DO $exact$ BEGIN
      IF ({check})::jsonb IS DISTINCT FROM {expected} THEN
        RAISE EXCEPTION 'Programme changed after deletion backup';
      END IF;
      IF EXISTS(SELECT 1 FROM programs WHERE id={program_id} AND source_url<>{source}) THEN
        RAISE EXCEPTION 'Programme identity changed';
      END IF;
      IF EXISTS(SELECT 1 FROM pg_constraint WHERE contype='f'
        AND confrelid IN ('programs'::regclass,'resources'::regclass)
        AND conrelid NOT IN ('resources'::regclass,'source_observations'::regclass,
                            'program_classifications'::regclass)) THEN
        RAISE EXCEPTION 'Unexpected dependent table; protect user data';
      END IF;
    END $exact$;
    """ + '\n'.join(statements) + '\n' + snapshot_sql(program_id)


def remote_delete(task):
    library = Path(__file__).with_name('production_round_remote.py').read_text('utf-8')
    source = "legacy={'__name__':'programme_deletion_library'}\nexec(" + repr(library) + ',legacy)\n'
    source += 'task=' + repr(task) + '\n'
    source += '''
import fcntl, hashlib, json, os
from pathlib import Path
with Path('/var/lib/manying/runtime/production-sync.lock').open('a+b') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX|fcntl.LOCK_NB)
    require=legacy['require']; psql=legacy['psql']; run=legacy['run']
    require(run(['systemctl','is-active','manying.service']).strip()==b'active','Production not active')
    application=str((Path('/opt/manying')/'app').resolve())
    require(application==task['application'],'Production application changed')
    legacy['reviewed_triggers']()
    folder=Path('/opt/manying/backups')/('program-delete-'+task['request_id'])
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    before=json.loads(psql(task['snapshot_sql']))
    rows=before['programs']
    require(not rows or (len(rows)==1 and rows[0]['source_url']==task['source_url']), 'Production identity differs')
    saved=folder/'programme.json'
    if not saved.exists():
        saved.write_text(json.dumps(before,ensure_ascii=False,sort_keys=True),encoding='utf-8')
    previous=json.loads(saved.read_text('utf-8'))
    require(before==previous or all(not v for v in before.values()), 'Production programme changed')
    db,app=legacy['backup'](folder)
    users_before=legacy['user_metrics']()
    if rows:
        # The JSON value is encoded as base64, so no row data enters a SQL literal.
        import base64
        payload="convert_from(decode('"+base64.b64encode(json.dumps(before,separators=(',',':'),ensure_ascii=False).encode()).decode()+"','base64'),'UTF8')::jsonb"
        sql=task['delete_template'].replace('EXPECTED_SNAPSHOT',payload)
        after=json.loads(psql("BEGIN; SET LOCAL lock_timeout='5s'; SET LOCAL statement_timeout='30s'; "
            "SELECT pg_advisory_xact_lock(hashtextextended('manying:catalogue',0)); " + sql + " COMMIT;" ).splitlines()[-1])
    else:
        after=before
    require(all(not v for v in after.values()),'Production deletion incomplete')
    users_after=legacy['user_metrics']()
    require(all(users_after.get(k,0)>=v for k,v in users_before.items() if k not in ('source_observations','program_classifications')), 'Production user data count decreased')
    require(str((Path('/opt/manying')/'app').resolve())==application,'Production application changed')
    receipt={'ok':True,'program_id':task['program_id'],'deleted':True,'application_unchanged':True,
        'user_data_protected':True,'database_backup_readable':True,'application_backup_readable':True,
        'database_backup':str(db),'programme_backup':str(saved),'programme_backup_sha256':hashlib.sha256(saved.read_bytes()).hexdigest()}
    (folder/'receipt.json').write_text(json.dumps(receipt,sort_keys=True),encoding='utf-8')
    print(json.dumps(receipt))
'''
    result = subprocess.run(['ssh', '-i', str(KEY), '-o', 'BatchMode=yes',
        '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10', HOST,
        'sudo -n python3 -'], input=source.encode('utf-8'), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=600, **hidden_options())
    if result.returncode:
        raise RuntimeError('Programme deletion transport failed; backups/checkpoint retained, exit '
                           + str(result.returncode))
    return json.loads(result.stdout.decode('utf-8').strip().splitlines()[-1])


def delete_program(*, caller, request_id, batch_id, program_id, source_url, reason, authorized):
    require(authorized and re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', request_id or ''),
            'An explicit user deletion request is required')
    require(type(program_id) is int and program_id>0 and isinstance(reason,str)
            and bool(reason.strip()), 'Exact programme and deletion reason required')
    main = json.loads(recoveryctl.MAIN.read_text('utf-8'))
    recovery = recoveryctl.state()
    state = workflow.read_state(workflow.DEFAULT_STATE)
    require(caller == os.environ.get('CODEX_THREAD_ID') == main['main_thread']
            == recoveryctl.policy()['main_thread'], 'Only the current business main can delete')
    require(recovery.get('stop_complete') is True, 'Verify the original complete stop first')
    targets_for(recoveryctl.policy(), main, state, recovery, workflow.PROJECT_ROOT)
    batch = state['batches'][batch_id]
    owner = batch['thread']
    require(batch['item_urls'] == [source_url] and batch['state'] in ('ready','released')
            and batch['logs_stopped'] is True, 'Only the stopped singleton batch is in scope')
    require(any(v['owner'] == owner for v in main['slots'].values()), 'Original slot owner required')
    work = Path(batch['work']).resolve()
    cover_path = work / (batch_id + '-covers.json')
    cover_bytes = cover_path.read_bytes()
    cover = json.loads(cover_bytes)
    require(cover.get('batch_id') == batch_id and cover.get('complete') is False
            and cover.get('items') == [{'error':'source_unavailable','program_id':program_id,
                'resource_count':1,'source_url':source_url,'status':'unavailable',
                'title':state['items'][source_url]['item']['title']}],
            'The original failed central cover receipt must identify this programme')
    with RPC() as rpc:
        thread = rpc.call('thread/read', {'threadId':owner,'includeTurns':False})['thread']
        source=thread.get('source') or {}
        parent=thread.get('parentThreadId') or (source.get('subAgent',{}).get('thread_spawn',{})
            .get('parent_thread_id') if isinstance(source,dict) else None)
        require(thread.get('id')==owner and parent==main['host_parent_thread']
                and Path(thread['cwd']).resolve() in (workflow.PROJECT_ROOT.resolve(),
                    workflow.PROJECT_ROOT.resolve().parent), 'Native owner/parent/cwd mismatch')
        turn = rpc.call('thread/turns/list', {'threadId':owner,'limit':1,
            'sortDirection':'desc','itemsView':'full'}).get('data',[])
        require(turn and turn[0]['status'] in ('completed','interrupted','failed'),
                'The original owner must have a terminal turn')
        require(not any(i.get('status') in ('inProgress','running','pending')
                        for i in turn[0].get('items',[]) if i.get('type') != 'reasoning'),
                'An original tool is still unsettled')
        if thread.get('status',{}).get('type') != 'notLoaded':
            terminals = rpc.call('thread/backgroundTerminals/list', {'threadId':owner})
            require(not terminals.get('data') and not terminals.get('nextCursor'),
                    'The original owner still has background commands')
    path = workflow.PROJECT_ROOT / '.runtime/main-agent/program-deletions' / (request_id + '.json')
    with WorkerLock(), workflow.work_lock(work), workflow.dbctl.database_writer():
        require(production_live.ledger().get('worker') is None,'Sync executor must be stopped')
        frozen_path=production_live.CURRENT
        frozen=json.loads(frozen_path.read_text('utf-8')) if frozen_path.exists() else None
        require(not frozen or (frozen.get('state')=='verified' and frozen.get('independent_verified_at')),
                'Finish any frozen production transaction before deleting')
        saved = json.loads(path.read_text('utf-8')) if path.exists() else None
        snapshot = workflow.dbctl.sql_json(snapshot_sql(program_id))
        if saved is None:
            require(len(snapshot['programs']) == 1 and snapshot['programs'][0]['source_url'] == source_url,
                    'Local programme identity differs')
            saved = {'version':1,'request_id':request_id,'authorization':'direct_user',
                'program_id':program_id,'source_url':source_url,'batch':batch_id,'owner':owner,
                'reason':reason,'at':workflow.utc_now(),'prior_batch':batch,
                'cover_receipt':str(cover_path),'cover_receipt_sha256':hashlib.sha256(cover_bytes).hexdigest(),
                'journals':workflow._journal_versions(work),'local_backup':snapshot}
            workflow.atomic_json(path,saved)
        require(saved['source_url'] == source_url and saved['program_id'] == program_id
                and saved['batch'] == batch_id and snapshot in (saved['local_backup'],
                    {name:[] for name in TABLES}), 'Local programme or deletion request changed')
        require(workflow._journal_versions(work)==saved['journals'],'Original journals changed')
        from catalogue_align import remote
        remote_before = remote('inspect', str(program_id))
        task = {'request_id':request_id,'program_id':program_id,'source_url':source_url,
                'application':remote_before['application_directory'],'snapshot_sql':snapshot_sql(program_id),
                'delete_template':delete_sql(program_id,source_url,{}).replace(workflow.dbctl.payload_sql({}),'EXPECTED_SNAPSHOT')}
        saved['production'] = remote_delete(task)
        workflow.atomic_json(path,saved)
        if snapshot['programs']:
            ledger_path = workflow.dbctl.PROGRAM_DELETIONS
            ledger = json.loads(ledger_path.read_text('utf-8')) if ledger_path.exists() else {'version':1,'urls':{}}
            require(ledger.get('version')==1 and isinstance(ledger.get('urls'),dict),
                    'Invalid programme deletion ledger')
            ledger['urls'][source_url] = {'program_id':program_id,'receipt':str(path),'request_id':request_id}
            workflow.atomic_json(ledger_path,ledger)
            after = workflow.dbctl.sql_json(workflow.dbctl.write_transaction(delete_sql(program_id,source_url,snapshot)))
            require(all(not values for values in after.values()),'Local deletion incomplete')
        def release(current):
            row=current['batches'][batch_id]
            require(current.get('collection_paused') is True and row['thread']==owner
                    and row['item_urls']==[source_url], 'Paused batch identity changed')
            if row['state'] != 'released':
                require(row['state']=='ready' and current['threads'][owner]['active_batch']==batch_id,
                        'Original owner batch changed')
                row.update(state='released',released_at=workflow.utc_now(),
                    release_reason='User deleted unavailable programme: '+reason)
                current['threads'][owner].update(mode='idle',active_batch=None)
            current['items'][source_url].update(state='manual_review',user_deleted=str(path))
            require(source_url not in current['queue'],'Deleted programme must not be queued')
            return {'ok':True},True
        workflow.coordctl._update(workflow.DEFAULT_STATE,release)
        production_live.clear_dirty('batch:'+batch_id)
        def cleared(current):
            require(current.get('worker') is None,'An event executor unexpectedly started')
            current.get('pending',{}).pop(str(program_id),None)
        production_live.mutate(cleared)
        actual = remote('inspect',str(program_id))
        require(all(v['rows']==0 for v in actual['tables'].values()),'Independent production deletion check failed')
        require(all(not v for v in workflow.dbctl.sql_json(snapshot_sql(program_id)).values()),
                'Independent local deletion check failed')
        require(workflow._journal_versions(work)==saved['journals'],'Original journals changed')
        saved.update(complete=True,verified_at=workflow.utc_now(),batch_released=True,
                     captured_ack=False,independent_production_absence=True)
        workflow.atomic_json(path,saved)
    return {'ok':True,'status':'user_deleted_programme','program_id':program_id,
            'batch_released':True,'captured_ack':False,'receipt':str(path),'collection_resumed':False}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--thread',required=True)
    parser.add_argument('--request-id',required=True)
    parser.add_argument('--batch',required=True)
    parser.add_argument('--program-id',required=True,type=int)
    parser.add_argument('--source-url',required=True)
    parser.add_argument('--reason',required=True)
    parser.add_argument('--authorized',action='store_true')
    args=parser.parse_args()
    result=delete_program(caller=args.thread,request_id=args.request_id,batch_id=args.batch,
        program_id=args.program_id,source_url=args.source_url,reason=args.reason,authorized=args.authorized)
    print(json.dumps(result,ensure_ascii=False))


if __name__=='__main__':
    main()
