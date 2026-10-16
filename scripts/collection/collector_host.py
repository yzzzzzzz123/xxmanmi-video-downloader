"""Own one server Codex app-server; native Goal continuation owns collection."""
import argparse
import copy
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import time

import collection_runtime as runtime
import coordctl

ROOT = runtime.PROJECT / '.runtime/collection-server'
HOST = ROOT / 'host.json'
OBJECTIVE = ('在新加坡服务器以唯一采集agent持续更新漫映生产库 manying。单线程执行正式'
 'Playwright公开DOM采集、资源审计、独立真实帧封面与去重、ACK、生产表合并和独立验收。'
 '本地采集与数据库已停维护，不得连接或依赖Windows、本地数据库、SSH转发或国内电脑。'
 '复用已迁移的9个真实URL和来源断点，不猜URL、不HTTP抓站、不播放取链，不创建子agent、'
 '关键词池或分类任务。调用single_agentctl有界步骤后持续续跑；空队列继续真实来源发现，'
 '不要完成一个批次就退出Goal。无效/空/失败结果跳过且不入正式节目表。'
 '封面解码失败先有界修复并复验，仍失败则正式待补跳过继续其他队列，不因可跳过项阻塞整个Goal。'
 '每批先在生产库collector_stage隔离暂存，资源和封面验收通过才提升到public节目表。保护生产用户数据、'
 '历史、ID与证据。连续7个不同节目封面失败或连续真实故障超过7200秒正式停工并暂停Goal，'
 '不要自行复工；正常每10分钟简要记录真实新增/更新与阻塞，不向第三人发消息。')

def save(path,value):
    path.parent.mkdir(parents=True,exist_ok=True)
    coordctl._atomic_write(path,value)

def close_gate(reason):
    import recoveryctl
    if not recoveryctl.STATE.exists():return
    with coordctl.StateLock(recoveryctl.STATE):
        state=coordctl._read_state(recoveryctl.STATE)
        state.update(collection_paused=True,collection_pause_reason=reason)
        save(recoveryctl.STATE,state)
    if recoveryctl.MAIN.exists():
        with coordctl.StateLock(recoveryctl.MAIN):
            main=json.loads(recoveryctl.MAIN.read_text())
            main.update(paused=True,main_phase='server_host_stopped')
            save(recoveryctl.MAIN,main)

def initialization_repair():
    marker=ROOT/'initialization-repair.json'
    if not marker.exists():return None
    value=json.loads(marker.read_text())
    companion=Path('/opt/manying-collector/bin/codex-code-mode-host')
    if (marker.stat().st_uid!=0 or marker.stat().st_mode & 0o022
            or not companion.is_file() or companion.stat().st_uid!=0
            or hashlib.sha256(companion.read_bytes()).hexdigest()!=value.get('companion_sha256')):
        raise ValueError('Unverified server runtime repair')
    return value

def user_resume_request():
    marker = ROOT/'user-resume.json'
    if not marker.exists():
        return None
    if marker.is_symlink() or marker.stat().st_uid != 0 or marker.stat().st_mode & 0o022:
        raise ValueError('User continuation must come from the deployment administrator')
    value = json.loads(marker.read_text())
    if (not isinstance(value.get('text'), str) or not 1 <= len(value['text']) <= 16000
            or hashlib.sha256(value['text'].encode()).hexdigest() != value.get('text_sha256')):
        raise ValueError('Invalid user continuation request')
    return value

def authorize_user_resume(request_file):
    if os.geteuid() != 0:
        raise ValueError('Only the deployment administrator can relay a user continuation')
    host = json.loads(HOST.read_text())
    if host.get('state') not in {'blocked', 'paused'}:
        raise ValueError('The original server host is not paused or blocked')
    source = Path(request_file)
    if not source.is_absolute() or source.is_symlink() or source.stat().st_uid != 0:
        raise ValueError('Expected a fixed administrator-owned user request file')
    message = source.read_text('utf-8').strip()
    if not 1 <= len(message) <= 16000:
        raise ValueError('User continuation text is empty or too long')
    value = {'thread': host['thread'], 'text': message, 'text_sha256': hashlib.sha256(message.encode()).hexdigest(),
             'at': coordctl.utc_now(), 'authorization': 'explicit_user_continuation'}
    marker = ROOT/'user-resume.json'
    if marker.exists():
        raise ValueError('An earlier user continuation has not been consumed')
    save(marker, value)
    import grp
    os.chown(marker, 0, grp.getgrnam('manying-collector').gr_gid)
    marker.chmod(0o640)
    print(json.dumps({'user_continuation_recorded': True, 'thread': host['thread']}))

def authorize_initialization_repair():
    if os.geteuid()!=0:raise ValueError('Only the deployment administrator can authorize initialization repair')
    host=json.loads(HOST.read_text())
    if host.get('state') not in {'blocked','paused'}:raise ValueError('This repair is limited to the failed initial server start')
    import recoveryctl
    ledger=coordctl._read_state(recoveryctl.STATE)
    if (list((ROOT/'batches').glob('batch-*/production-verification.json'))
            or ledger.get('cover_outcomes',{}).get('stop_required')
            or any(x.get('resolved_at') is None and x.get('first_failed_at')
                and time.time()-__import__('datetime').datetime.fromisoformat(x['first_failed_at'].replace('Z','+00:00')).timestamp()>7200
                for x in recoveryctl.state().get('incidents',{}).values())):
        raise ValueError('Normal/fault-stopped collection cannot use initialization repair')
    companion=Path('/opt/manying-collector/bin/codex-code-mode-host')
    marker=ROOT/'initialization-repair.json'
    value={'thread':host['thread'],'reason':'verified_initial_server_runtime_repair',
        'companion_sha256':hashlib.sha256(companion.read_bytes()).hexdigest(),'at':coordctl.utc_now()}
    save(marker,value)
    import grp
    os.chown(marker,0,grp.getgrnam('manying-collector').gr_gid);marker.chmod(0o640)
    print(json.dumps({'initialization_repair_authorized':True,'thread':host['thread']}))

def bootstrap(owner):
    """Create a new server owner while retaining the stopped original snapshots."""
    import recoveryctl as recovery
    if HOST.exists():
        raise ValueError('An existing host must resume its exact original server thread')
    archive = ROOT/'migration'
    original = json.loads((archive/'coordination.json').read_text())
    old_owner = original['collection_active_owner']
    old = original['threads'][old_owner]
    stop = json.loads((archive/'recovery.json').read_text())
    goal = json.loads((archive/'original-native-goal.json').read_text())
    if (not original.get('collection_paused') or old.get('active_batch') or old.get('mode')!='idle'
            or not stop.get('stopped') or not stop.get('stop_complete')
            or (goal.get('goal') or {}).get('status')!='paused'
            or any(not x.get('resolved_at') for x in stop.get('incidents',{}).values())):
        raise ValueError('Original collection is not at a verified terminal checkpoint')
    state = copy.deepcopy(original)
    work = runtime.PROJECT/'Datebase/xxmanmi-resource-db/.runtime/collection-workers'/('server-'+owner)
    work.mkdir(parents=True,exist_ok=False)
    checkpoint = json.loads((archive/'original-work/playwright-discovery.json').read_text())
    if checkpoint.get('owner')!=old_owner or checkpoint.get('action_inflight'):
        raise ValueError('Original discovery identity or navigation outcome is unresolved')
    retained = work/'retained'
    retained.mkdir()
    mapping = {}
    for key,value in checkpoint.get('seen',{}).items():
        name = value.replace('\\','/').rsplit('/',1)[-1]
        source = archive/'original-work'/name
        if not source.is_file() or source.is_symlink():
            raise ValueError('Missing original source coverage evidence')
        target = retained/name
        target.write_bytes(source.read_bytes())
        mapping[value] = {'path':str(target),'sha256':hashlib.sha256(target.read_bytes()).hexdigest()}
        checkpoint['seen'][key]=str(target)
    checkpoint.update(owner=owner,server_handoff={'from':old_owner,
        'source_sha256':hashlib.sha256((archive/'original-work/playwright-discovery.json').read_bytes()).hexdigest(),
        'path_mapping':mapping})
    save(work/'playwright-discovery.json',checkpoint)
    authorization = {'operation':'server_production_only_migration',
        'original_owner':old_owner,'server_owner':owner,'at':coordctl.utc_now(),
        'original_goal_preserved':True,'local_collection_retired':True,
        'original_state_sha256':hashlib.sha256((archive/'coordination.json').read_bytes()).hexdigest()}
    state['threads'][owner]={'role':'peer','work':str(work),'work_key':os.path.normcase(str(work.resolve())),
        'registered_at':coordctl.utc_now(),'mode':'idle','active_batch':None,'server_handoff':authorization}
    state['threads'][old_owner]['execution_handoff_to']=owner
    state['collection_active_owner']=owner
    state['collection_paused']=False
    state['collection_pause_reason']=None
    state['server_migration']=authorization
    state.setdefault('single_execution_handoffs',[]).append(authorization)
    main=json.loads((archive/'main.json').read_text())
    main.update(main_thread=owner,title_discovery_owner=owner,paused=False,
        main_phase='server_collection',execution_mode='single',server_migration=authorization,
        slots={'main':{'owner':owner,'work':str(work),'registration_verified':True,'capture_engine':'playwright'}})
    policy=json.loads((archive/'policy.json').read_text())
    policy.update(main_thread=owner,execution_mode='single',enabled=True,
        force_process_stop_authorized=False,stop_authorized=True,server_migration=authorization,
        storage_mode='production_direct',sync={'enabled':True,'mode':'production_direct'})
    # This is the user's explicit host migration/resumption, not a reset of a fault budget.
    recovery_state={'version':1,'cycle_started_at':coordctl.utc_now(),'incidents':{},'stopped':False,
        'previous_cycles':[{'state':stop,'reason':'explicit_user_server_migration'}],
        'server_migration':authorization}
    save(recovery.POLICY,policy)
    save(recovery.MAIN,main)
    save(recovery.RECOVERY,recovery_state)
    save(recovery.STATE,state)
    return authorization

def run():
    runtime.configuration()
    ROOT.mkdir(parents=True,exist_ok=True)
    import fcntl
    lock=(ROOT/'host.lock').open('a+b')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    binary=os.environ['XXMANMI_CODEX_BIN']
    private_log=(ROOT/'app-server.stderr.log').open('ab')
    server=subprocess.Popen([binary,'--no-daemon','app-server','--listen','unix://'],
        stdin=subprocess.DEVNULL,stdout=private_log,stderr=private_log)
    stopping=False
    def request_stop(*unused):
        nonlocal stopping
        stopping=True
    signal.signal(signal.SIGTERM,request_stop)
    signal.signal(signal.SIGINT,request_stop)
    try:
        socket=Path(os.environ['CODEX_HOME'])/'app-server-control/app-server-control.sock'
        for _ in range(100):
            if socket.exists():break
            if server.poll() is not None:raise RuntimeError('Server Codex exited during startup')
            time.sleep(.2)
        from app_server_control import RPC
        with RPC() as rpc:
            existing=json.loads(HOST.read_text()) if HOST.exists() else None
            if existing:
                owner=existing['thread']
                import recoveryctl
                ledger=coordctl._read_state(recoveryctl.STATE)
                repair=initialization_repair()
                continuation=user_resume_request()
                if continuation and continuation.get('thread')!=owner:
                    raise ValueError('User continuation belongs to another server thread')
                if repair and repair.get('thread')!=owner:raise ValueError('Initialization repair belongs to another server thread')
                if ledger.get('collection_paused') and not (repair or continuation):
                    save(HOST,{**existing,'state':'paused','observed_at':coordctl.utc_now()})
                    return
                if repair:
                    if ledger.get('cover_outcomes',{}).get('stop_required'):
                        raise ValueError('Initialization repair cannot resume a policy stop')
                    if not recoveryctl.state().get('stopped'):
                        with coordctl.StateLock(recoveryctl.STATE):
                            ledger=coordctl._read_state(recoveryctl.STATE)
                            ledger.update(collection_paused=False,collection_pause_reason=None)
                            ledger.setdefault('server_environment_repairs',[]).append(repair)
                            save(recoveryctl.STATE,ledger)
                        with coordctl.StateLock(recoveryctl.MAIN):
                            main=json.loads(recoveryctl.MAIN.read_text());main.update(paused=False,main_phase='server_collection')
                            save(recoveryctl.MAIN,main)
                rpc.call('thread/resume',{'threadId':owner,'cwd':str(runtime.PROJECT)})
                if continuation:
                    rpc.call('turn/start',{'threadId':owner,'input':[{'type':'text','text':continuation['text']}]})
                    rpc.call('thread/goal/set',{'threadId':owner,'status':'active'})
                    existing.setdefault('user_continuations',[]).append({key:value for key,value in continuation.items() if key!='text'})
                    (ROOT/'user-resume.json').rename(ROOT/('user-resume-'+continuation['at'].replace(':','')+'.consumed.json'))
                if repair:
                    if recoveryctl.state().get('stopped'):
                        stopped=recoveryctl.stop_project(caller_thread=owner)
                        if not stopped.get('ok'):raise ValueError('Actual Linux process stop must verify before the explicit initialization resume')
                    rpc.call('turn/start',{'threadId':owner,'input':[{'type':'text','text':
                        '迁移会话已补齐官方Codex工具宿主，并修复服务用户的可写HOME/XDG目录，实际浏览器启动测试通过。Linux专属进程停工入口已适配并完成核验。现按用户本次完整迁移并直接在服务器执行的授权，明确继续当前同一个Goal和原认领/队列，不另建目标、不重置累计用量。若recovery状态stopped=true，请先从当前native turn读取本条真实userMessage id，执行正式 recoveryctl.resume_project(caller_thread=当前CODEX_THREAD_ID, request_id=该id, authorized=True)；保持已有批次和原证据。随后继续正常有界步骤。本条是继续授权，不能把它当作用户停止指令；一般环境故障先记录原7200秒恢复预算或请求部署诊断，不因首次错误立即伪称用户要求停止。'}]})
                    rpc.call('thread/goal/set',{'threadId':owner,'status':'active'})
                    existing.setdefault('environment_repairs',[]).append(repair)
                    (ROOT/'initialization-repair.json').rename(ROOT/('initialization-repair-'+repair['at'].replace(':','')+'.consumed.json'))
                goal=rpc.call('thread/goal/get',{'threadId':owner}).get('goal') or {}
                if goal.get('status')!='active':
                    save(HOST,{**existing,'state':goal.get('status','attention'),'observed_at':coordctl.utc_now()})
                    return
            else:
                original=json.loads((ROOT/'migration/original-native-goal.json').read_text())
                result=rpc.call('thread/start',{'cwd':str(runtime.PROJECT),
                    'model':original['model'],'approvalPolicy':'never','sandbox':'danger-full-access'})
                owner=result['thread']['id']
                authorization=bootstrap(owner)
                existing={'thread':owner,'original_thread':authorization['original_owner'],
                    'created_at':coordctl.utc_now(),'state':'initializing'}
                save(HOST,existing)
                prompt=(OBJECTIVE+'\n当前用户已明确授权迁移并直接在服务器续跑；原本地Goal已暂停。'
                    '\n先读AGENTS.md、skills/xxmanmi-video-downloader/SKILL.md、references/server-collection.md。'
                    '\n服务器上唯一正式数据库是manying，不调用本地同步/SSH/部署/改网站。'
                    '\n以当前CODEX_THREAD_ID作为owner运行 python -X utf8 -B scripts/collection/single_agentctl.py run --thread OWNER --max-views 8 --steps 1。'
                    '\n持续续跑并检查server_production.status真实回执。遇代码/环境故障只根据真实错误做有界诊断，正式停工保留断点。'
                    '\n本机无需也不允许后台本地采集。服务会保持连接，持续性由当前原生Goal负责。')
                rpc.call('turn/start',{'threadId':owner,'effort':original.get('reasoningEffort') or 'xhigh',
                    'approvalPolicy':'never','sandboxPolicy':{'type':'dangerFullAccess'},
                    'input':[{'type':'text','text':prompt}]})
                rpc.call('thread/goal/set',{'threadId':owner,'objective':OBJECTIVE,'status':'active'})
            while not stopping:
                if server.poll() is not None:
                    raise RuntimeError('Dedicated Codex app-server stopped unexpectedly')
                goal=rpc.call('thread/goal/get',{'threadId':owner}).get('goal') or {}
                thread=rpc.call('thread/read',{'threadId':owner,'includeTurns':False})['thread']
                save(HOST,{**existing,'state':'running' if goal.get('status')=='active' else goal.get('status','attention'),
                    'goal_status':goal.get('status'),'thread_status':thread.get('status'),
                    'observed_at':coordctl.utc_now()})
                if goal.get('status')!='active':
                    close_gate('Native server Goal is '+str(goal.get('status')))
                    return
                time.sleep(5)
            close_gate('Server collector service stop requested; preserve the original claim')
            try:rpc.call('thread/goal/set',{'threadId':owner,'status':'paused'})
            except Exception:pass
    finally:
        if server.poll() is None:
            server.terminate()
            try:server.wait(timeout=12)
            except subprocess.TimeoutExpired:server.kill();server.wait(timeout=5)
        private_log.close()
        lock.close()

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--authorize-initialization-repair',action='store_true')
    parser.add_argument('--authorize-user-resume',type=Path,metavar='REQUEST_FILE')
    args=parser.parse_args()
    if args.authorize_user_resume:authorize_user_resume(args.authorize_user_resume)
    elif args.authorize_initialization_repair:authorize_initialization_repair()
    else:run()
