"""Single business agent: bounded Playwright -> audit -> cover -> ACK -> live sync."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from datetime import datetime, timezone

import coordctl
import recoveryctl as recovery

OBJECTIVE = ('以当前唯一 agent 单线程持续维护小小漫迷每日新资源与更新：直接负责独立隐藏 Playwright '
    '公开 DOM 采集、正式认领、资源入库审计、中央 DIRECT 独立封面、真实 ACK，以及既有受控生产 '
    '增量同步和独立验收。不再创建或调度采集子线程，不使用 CUA 采集。每批一个节目，沿实际公开 '
    '来源增量观察，空队列按断点等待新更新。保留旧线程全部数据、认领、历史与证据，已停止的旧 '
    'ready 批次由当前 agent 依据正式交接验收。仅显示 working，用户主动提问正常回答。保持 TUN '
    '和精确域名 DIRECT、资源完整性和封面质量门禁；连续第 7 个不同节目无合格封面或真实故障 '
    '超过 7200 秒时完成正式停工。用户明确暂停或停止立即优先。不部署生产应用，不安装后台 '
    '监督服务或计划任务，不重放未知动作；持续执行至用户停止或正式停工，单批完成不结束 Goal。')


def sha(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def handoff_single_execution(state, previous, owner, work, request):
    """Transfer a stopped single executor without rewriting historical batches."""
    if not state.get('collection_paused'):
        raise ValueError('Execution handoff requires a closed collection gate')
    if (state.get('execution_mode') != 'single'
            or state.get('collection_active_owner') != previous or previous == owner):
        raise ValueError('Execution handoff differs from the original single owner')
    old = state['threads'][previous]
    if (old.get('active_batch') or old.get('mode') != 'idle'
            or any(b.get('thread') == previous and b.get('state') in {'claimed', 'ready'}
                   for b in state['batches'].values())):
        raise ValueError('Original executor must settle its claim before handoff')
    path, key = coordctl._work_path(str(work))
    if owner in state['threads'] or owner in state.get('retired_threads', {}):
        raise ValueError('Receiver already has a collection registration')
    if any(v.get('work_key') == key for v in state['threads'].values()):
        raise ValueError('Receiver requires its own collection directory')
    receipt = {'at':coordctl.utc_now(), 'from':previous, 'to':owner,
               'original_work':old['work'], 'work':path, 'authorization':request,
               'data_and_evidence_preserved':True}
    old.update(execution_retired_at=receipt['at'], execution_handoff_to=owner)
    state['threads'][owner] = {'role':'peer', 'work':path, 'work_key':key,
        'registered_at':receipt['at'], 'mode':'idle', 'active_batch':None,
        'execution_handoff':receipt}
    state.setdefault('single_execution_handoffs', []).append(receipt)
    state['collection_active_owner'] = owner
    return receipt


def transition(state, owner, work, prior_slots, request):
    """Transfer execution, retain original owners/evidence and never synthesize ACK."""
    if not state.get('collection_paused'):
        raise ValueError('Migration requires the closed collection gate')
    if state.get('execution_mode') == 'single':
        if state.get('collection_active_owner') != owner:
            raise ValueError('Existing single agent differs')
        return state['single_agent_migration']
    now = coordctl.utc_now()
    ready, requeued = [], []
    for binding in prior_slots.values():
        old = binding['owner']
        peer = state['threads'].get(old, {})
        if Path(peer.get('work', '')).resolve() != Path(binding['work']).resolve():
            raise ValueError('Original work binding changed')
        batch_id = peer.get('active_batch')
        if batch_id:
            batch = state['batches'][batch_id]
            if batch['thread'] != old or batch['state'] not in {'claimed', 'ready'}:
                raise ValueError('Unexpected original batch state')
            if batch['state'] == 'ready':
                if batch.get('logs_stopped') is not True:
                    raise ValueError('Ready batch has no stopped journal proof')
                ready.append({'owner':old, 'batch':batch_id, 'work':peer['work'],
                              'original_batch_sha256':sha(batch)})
            else:
                # Original actors/writers have been formally stopped. These known
                # public URLs may be observed afresh by the new owner; old evidence stays.
                for url in batch['item_urls']:
                    item = state['items'][url]
                    item.setdefault('execution_handoffs', []).append({
                        'at':now,'from':old,'to':owner,'original_batch':batch_id,
                        'previous_state':item['state'],'request_id':request['request_id']})
                    item.update(state='queued',batch=None,outcome=None)
                    if url not in state['queue']:
                        state['queue'].append(url)
                    requeued.append(url)
                batch.update(state='released',released_at=now,logs_stopped=True,
                    release_reason='Direct user migration to single agent; URLs handed off, evidence retained',
                    execution_handoff_to=owner)
                peer.update(mode='idle',active_batch=None)
        peer['execution_retired_at'] = now
    work_path, work_key = coordctl._work_path(str(work))
    existing = state['threads'].get(owner)
    if existing and existing['work_key'] != work_key:
        raise ValueError('Main already has a different collection work binding')
    state['threads'].setdefault(owner, {'role':'peer','work':work_path,'work_key':work_key,
        'registered_at':now,'mode':'idle','active_batch':None})
    receipt = {'at':now,'owner':owner,'authorization':request,'prior_slots':prior_slots,
        'ready_handoffs':ready,'requeued_urls':requeued,'data_and_evidence_preserved':True}
    state.update(execution_mode='single',collection_active_owner=owner,
                 single_agent_migration=receipt)
    return receipt


def migrate(owner, request_id, authorized=False):
    receipt_path = recovery.MAIN.parent / ('single-migration-'+str(request_id)+'.json')
    if not request_id or not authorized:
        raise ValueError('Migration requires direct user authorization')
    with coordctl.StateLock(recovery.RECOVERY.with_name('stop-execution.json'), timeout=5):
        stored = json.loads(receipt_path.read_text('utf-8')) if receipt_path.exists() else None
        request = recovery._direct_user_request(owner,request_id,authorized,
            recorded=stored['authorization'] if stored else None)
        stopped = recovery.state()
        if (not stopped.get('stopped') or not stopped.get('stop_complete')
                or not stopped.get('process_stop_receipt', {}).get('ok')):
            raise ValueError('Original native actors and dedicated processes must be verified stopped')
        main = json.loads(recovery.MAIN.read_text('utf-8'))
        prior = stored['prior_slots'] if stored else main['slots']
        if not stored and set(prior) != {'01','02','03','04'}:
            raise ValueError('Migration requires the original four registered bindings')
        if owner != main['main_thread'] or not main.get('paused'):
            raise ValueError('Actual business main must be paused during migration')
        work = recovery.PROJECT/'Datebase/xxmanmi-resource-db/.runtime/collection-workers'/('main-'+owner)
        if work.is_symlink():
            raise ValueError('Unsafe main work directory')
        work.mkdir(parents=True,exist_ok=True)
        prepared = stored or {'phase':'prepared','authorization':request,'prior_slots':prior,
            'stop_receipt_sha256':sha(stopped),'work':str(work)}
        recovery.save_json(receipt_path,prepared)
        from app_server_control import RPC
        with RPC() as rpc:
            previous = rpc.call('thread/goal/get',{'threadId':owner}).get('goal')
            if not previous or previous.get('status')!='paused':
                raise ValueError('Original main Goal must be paused before changing its user-directed scope')
            prepared.setdefault('previous_goal',previous)
            recovery.save_json(receipt_path,prepared)
            # This is an agent-applied mutation of an already verified user item.
            # origin=user tries to append a second instruction while this turn owns
            # the active writer; origin=automatic preserves the existing instruction.
            rpc.call('thread/goal/set',{'threadId':owner,'objective':OBJECTIVE,'status':'paused','origin':'automatic'})
            actual=rpc.call('thread/goal/get',{'threadId':owner})['goal']
            if (actual['objective']!=OBJECTIVE or actual['createdAt']!=previous['createdAt']
                    or actual['tokensUsed']<previous['tokensUsed']):
                raise ValueError('Native Goal scope update did not preserve its real identity/usage')
            prepared['native_goal_scope_updated']=True
        with coordctl.StateLock(recovery.STATE,timeout=5):
            state = coordctl._read_state(recovery.STATE)
            receipt = transition(state,owner,work,prior,request)
            coordctl._atomic_write(recovery.STATE,state)
        with coordctl.StateLock(recovery.MAIN,timeout=5):
            main = json.loads(recovery.MAIN.read_text('utf-8'))
            main.update(execution_mode='single',main_phase='single_agent_migrated_paused',
                single_agent_migration=receipt,slots={'main':{'owner':owner,'work':str(work),
                    'registration_verified':True,'capture_engine':'playwright'}})
            recovery.save_json(recovery.MAIN,main)
        with coordctl.StateLock(recovery.POLICY,timeout=5):
            config = recovery.policy()
            config.update(execution_mode='single',recovery_budget_seconds=7200,
                default_batch_size=1,source_refresh_seconds=1800,reporting='user_requested_only')
            recovery.save_json(recovery.POLICY,config)
        prepared.update(phase='committed',migration=receipt)
        recovery.save_json(receipt_path,prepared)
        return {'ok':True,'execution_mode':'single','owner':owner,'gate_opened':False,
            'ready_handoffs':len(receipt['ready_handoffs']),'requeued':len(receipt['requeued_urls']),
            'receipt':str(receipt_path),'data_and_evidence_preserved':True}


def authorized_handoff(owner, source_owner, batch_id):
    if owner != os.environ.get('CODEX_THREAD_ID') or owner != recovery.policy().get('main_thread'):
        raise ValueError('Only the actual single business agent may finalize a handoff')
    state = coordctl._read_state(recovery.STATE)
    coordctl._require_peer(state,owner)
    entries = [r for r in state.get('single_agent_migration',{}).get('ready_handoffs',[])
               if r['owner']==source_owner and r['batch']==batch_id]
    batch = state['batches'].get(batch_id,{})
    if (len(entries)!=1 or batch.get('thread')!=source_owner
            or batch.get('state') not in {'ready','acknowledged'}
            or Path(batch.get('work','')).resolve()!=Path(entries[0]['work']).resolve()
            or (batch['state']=='ready' and sha(batch)!=entries[0]['original_batch_sha256'])):
        raise ValueError('Original stopped ready batch handoff does not match')
    return source_owner


def prioritize_new_resources(owner):
    """Prefer observed pages without a usable capture; preserve claims and FIFO ties."""
    import workflowctl as workflow
    state = coordctl._read_state(recovery.STATE)
    if state['threads'][owner].get('active_batch') or not state['queue']:
        return
    rows = workflow.dbctl.lookup(state['queue'][:100])
    priority = {row['url'] for row in rows if row.get('captured') is not True}
    if not priority:
        return
    def reorder(current):
        coordctl.require_collection_running(current)
        coordctl._require_peer(current, owner)
        if current['threads'][owner].get('active_batch'):
            return {'ok': True}, False
        queue = ([url for url in current['queue'] if url in priority]
                 + [url for url in current['queue'] if url not in priority])
        changed = queue != current['queue']
        current['queue'] = queue
        return {'ok': True}, changed
    coordctl._update(recovery.STATE, reorder)


def run(owner, max_views=8):
    import collection_runtime
    collection_runtime.require_write()
    from server_production import staged_batch
    import playwrightctl
    import workflowctl as workflow
    import operationsctl
    import production_live
    slot,_ = playwrightctl.owner_context(owner)
    if slot != 'main':
        raise ValueError('This entry only runs the single business agent')
    pulse = operationsctl.pulse(owner)
    if pulse.get('status')=='stopped':
        return pulse
    state = coordctl._read_state(recovery.STATE)
    for row in state.get('single_agent_migration',{}).get('ready_handoffs',[]):
        if state['batches'][row['batch']]['state']=='ready':
            with staged_batch(owner,row['batch']):
                result = workflow.complete_detail(workflow.DEFAULT_STATE,owner,row['batch'],True,
                    source_owner=row['owner'])
            recovery.progress(owner,batch=row['batch'])
            production_live.notify_batch(row['owner'],row['batch'])
            production_live.kick()
            return {'ok':True,'stage':'original_batch_finalized','result':result,
                    'production':production_live.status()}
    prioritize_new_resources(owner)
    claim = playwrightctl.collect(owner)
    if not claim.get('batch'):
        found = playwrightctl.discover(owner,max_views=max_views)
        if found.get('added',0)==0:
            return {'ok':True,'stage':'discovery','result':found,'production':production_live.status()}
        prioritize_new_resources(owner)
        claim = playwrightctl.collect(owner)
    skipped = [[row['url'], row['reason']] for row in claim.get('results', [])
               if row.get('outcome') == 'skipped']
    with staged_batch(owner,claim['batch']):
        result = workflow.complete_detail(workflow.DEFAULT_STATE,owner,claim['batch'],True,
                                          skipped=skipped)
    recovery.progress(owner,batch=claim['batch'])
    production_live.notify_batch(owner,claim['batch'])
    production_live.kick()
    return {'ok':True,'stage':'acknowledged','capture':{k:claim.get(k) for k in
            ['batch','engine','receipt','results','browser_closed']},'result':result,
            'production':production_live.status()}


def wait_for_poll(owner, seconds=45):
    """One finite owner-bound wait; no daemon, model calls, state rewrite or browser."""
    import playwrightctl
    slot,work=playwrightctl.owner_context(owner)
    if slot!='main' or not 1<=seconds<=50:
        raise ValueError('Only the single agent may wait for at most 50 seconds')
    recovery.guard(caller_thread=owner)
    state=coordctl._read_state(recovery.STATE)
    if state['queue'] or state['threads'][owner].get('active_batch'):
        return {'ok':True,'status':'work_available','waited_seconds':0}
    checkpoint=work/'playwright-discovery.json'
    saved=json.loads(checkpoint.read_text('utf-8'))
    if saved.get('owner')!=owner or saved.get('action_inflight'):
        raise ValueError('Original discovery owner/action must be reconciled before waiting')
    if not saved.get('completed_at') or saved.get('pending'):
        return {'ok':True,'status':'discovery_available','waited_seconds':0}
    due=datetime.fromisoformat(saved['completed_at'].replace('Z','+00:00')).timestamp()+playwrightctl.SOURCE_REFRESH_SECONDS
    duration=min(seconds,max(0,due-datetime.now(timezone.utc).timestamp()))
    started=time.monotonic()
    time.sleep(duration)
    recovery.guard(caller_thread=owner)
    state=coordctl._read_state(recovery.STATE)
    remaining=max(0,int(due-datetime.now(timezone.utc).timestamp()))
    return {'ok':True,'status':'stopped' if state.get('collection_paused') else
        'work_available' if state['queue'] or state['threads'][owner].get('active_batch') else
        'ready_to_poll' if remaining==0 else 'waiting_for_public_change',
        'waited_seconds':round(time.monotonic()-started,3),'retry_after_seconds':remaining,
        'owner':owner,'browser_started':False,'checkpoint_preserved':True}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['migrate','run','wait'])
    parser.add_argument('--thread',required=True)
    parser.add_argument('--request-id')
    parser.add_argument('--authorized',action='store_true')
    parser.add_argument('--max-views',type=int,choices=range(1,21),default=8)
    parser.add_argument('--steps',type=int,choices=range(1,21),default=1)
    parser.add_argument('--seconds',type=int,choices=range(1,51),default=45)
    args=parser.parse_args()
    try:
        if args.command=='run' and args.steps>1:
            for step in range(args.steps):
                started=time.monotonic()
                result=run(args.thread,args.max_views)
                detail=result.get('result',{}); production=result.get('production',{})
                print(json.dumps({'ok':result.get('ok'), 'step':step+1,
                    'stage':result.get('stage'), 'batch':detail.get('batch'),
                    'outcomes':detail.get('outcomes'), 'added':detail.get('added'),
                    'pending_sync':production.get('pending'),
                    'sync_error':production.get('last_error'),
                    'seconds':round(time.monotonic()-started,1)},ensure_ascii=False),flush=True)
                if result.get('status')=='stopped':
                    return 0
            return 0
        result=(migrate(args.thread,args.request_id,args.authorized) if args.command=='migrate'
                else wait_for_poll(args.thread,args.seconds) if args.command=='wait'
                else run(args.thread,args.max_views))
    except Exception as error:
        result={'ok':False,'error':str(error)[:1500]}
        if args.command=='run':
            try:
                result['recovery']=recovery.record(args.thread,str(error),command='single_agentctl run')
            except Exception:
                pass
    print(json.dumps(result,ensure_ascii=False,sort_keys=True))
    return 0 if result.get('ok') else 1


if __name__=='__main__':
    raise SystemExit(main())
