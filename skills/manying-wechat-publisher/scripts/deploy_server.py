#!/usr/bin/env python3
"""Explicit, isolated server install. No website restart, database access or credential copying."""
import argparse
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import tarfile

SKILL = Path(__file__).resolve().parents[1]
PROJECT = SKILL.parents[1]
SSH = ['ssh', '-T', '-i', str(Path.home()/'.ssh/manying_deploy_20260929'),
       '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=10',
       'ubuntu@43.134.92.99']


REMOTE = r'''
import base64,hashlib,io,json,os,pathlib,pwd,shutil,subprocess,tarfile,time,urllib.request
ROOT=pathlib.Path('/opt/manying-publisher')
HOME=pathlib.Path('/var/lib/manying-publisher')
ETC=pathlib.Path('/etc/manying-publisher')
def command(args,**kwargs):
    return subprocess.run(args,check=True,**kwargs)
def prop(unit,key):
    return subprocess.run(['systemctl','show',unit,'--value','--property='+key],capture_output=True,text=True).stdout.strip()
before={'pid':prop('manying','MainPID'),'active':prop('manying','ActiveState')}
if MODE=='inspect':
    print(json.dumps({'site':before,'publisher_exists':ROOT.exists(),'free_bytes':shutil.disk_usage('/opt').free,
                      'publisher_state':prop('manying-publisher','ActiveState')}));raise SystemExit()
if prop('manying-publisher','ActiveState') in ('active','activating') or prop('manying-publisher-login','ActiveState') in ('active','activating'):
    raise RuntimeError('A publication or interactive login is active; preserve it')
deployment_lock=None
if (HOME/'worker.lock').exists():
    import fcntl
    deployment_lock=(HOME/'worker.lock').open('rb')
    try:fcntl.flock(deployment_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:raise RuntimeError('A manual worker or browser operation is active; preserve it')
if before['active']!='active':raise RuntimeError('Site baseline is not healthy')
if hashlib.sha256(base64.b64decode(PACK)).hexdigest()!=DIGEST:raise RuntimeError('Payload hash mismatch')
release=ROOT/'releases'/RELEASE
if release.exists():raise RuntimeError('Use a new release ID; existing release is immutable')
ROOT.mkdir(exist_ok=True)
release.mkdir(parents=True)
with tarfile.open(fileobj=io.BytesIO(base64.b64decode(PACK)),mode='r:gz') as package:
    package.extractall(release,filter='data')
for path in (release/'workspace').rglob('*'):
    path.chmod(0o755 if path.is_dir() else 0o644)
try:account=pwd.getpwnam('manying-publisher')
except KeyError:
    command(['useradd','--system','--create-home','--home-dir',str(HOME),'--shell','/usr/sbin/nologin','manying-publisher'])
    account=pwd.getpwnam('manying-publisher')
HOME.mkdir(exist_ok=True);HOME.chmod(0o700);os.chown(HOME,account.pw_uid,account.pw_gid)
for sub in ['articles','jobs','.codex']:
    p=HOME/sub;p.mkdir(exist_ok=True);p.chmod(0o700);os.chown(p,account.pw_uid,account.pw_gid)
log_path=ROOT/('install-'+RELEASE+'.log')
with log_path.open('ab') as log:
    env={**os.environ,'DEBIAN_FRONTEND':'noninteractive','NEEDRESTART_MODE':'l',
         'PLAYWRIGHT_BROWSERS_PATH':str(ROOT/'browsers')}
    command(['apt-get','update'],env=env,stdout=log,stderr=log,timeout=300)
    command(['apt-get','install','-y','python3-venv','xvfb','xauth','x11vnc','novnc','websockify','fonts-noto-cjk'],
            env=env,stdout=log,stderr=log,timeout=600)
    if not (ROOT/'venv/bin/python').exists():command(['python3','-m','venv',str(ROOT/'venv')],stdout=log,stderr=log)
    command([str(ROOT/'venv/bin/python'),'-m','pip','install','playwright==1.64.0'],
            env=env,stdout=log,stderr=log,timeout=300)
    command([str(ROOT/'venv/bin/python'),'-m','playwright','install','--with-deps','chromium'],
            env=env,stdout=log,stderr=log,timeout=600)
log_path.chmod(0o600)
binary=ROOT/'bin/codex'
if not binary.exists():
    request=urllib.request.Request('https://api.github.com/repos/openai/codex/releases/tags/rust-v0.162.1',headers={'User-Agent':'ManyingPublisherInstaller'})
    with urllib.request.urlopen(request,timeout=30) as response:meta=json.load(response)
    asset=next(a for a in meta['assets'] if a['name']=='codex-package-x86_64-unknown-linux-musl.tar.gz')
    with urllib.request.urlopen(asset['browser_download_url'],timeout=120) as response:data=response.read()
    digest=asset.get('digest','')
    if not digest.startswith('sha256:') or hashlib.sha256(data).hexdigest()!=digest.split(':',1)[1]:
        raise RuntimeError('Official Codex asset digest mismatch or missing')
    native=ROOT/'codex-0.162.1';native.mkdir()
    with tarfile.open(fileobj=io.BytesIO(data),mode='r:gz') as package:package.extractall(native,filter='data')
    candidates=[p for p in native.rglob('codex') if p.is_file() and os.access(p,os.X_OK)]
    if len(candidates)!=1:raise RuntimeError('Cannot identify the packaged Codex binary')
    (ROOT/'bin').mkdir(exist_ok=True);binary.symlink_to(candidates[0])
version=subprocess.run([str(binary),'--version'],capture_output=True,text=True,check=True).stdout.strip()
if version!='codex-cli 0.162.1':raise RuntimeError('Unexpected Codex binary version')
seed=(ROOT/'releases'/SEED_RELEASE/'seed-articles') if SEED_RELEASE else release/'seed-articles'
seeded=0
if seed.exists():
    for article in seed.iterdir():
        destination=HOME/'articles'/article.name
        if destination.exists():
            if (destination/'manifest.json').read_bytes()!=(article/'manifest.json').read_bytes():
                raise RuntimeError('Preserve existing server article; seed differs')
            continue
        shutil.copytree(article,destination);seeded+=1
        for p in [destination,*destination.rglob('*')]:
            os.chown(p,account.pw_uid,account.pw_gid);p.chmod(0o700 if p.is_dir() else 0o600)
config=release/'workspace/skills/manying-wechat-publisher/deploy/publisher.json'
ETC.mkdir(exist_ok=True);ETC.chmod(0o755)
target=ETC/'publisher.json'
if target.exists():
    if json.loads(target.read_text())!=json.loads(config.read_text()):raise RuntimeError('Existing deployment config differs; preserve it')
else:shutil.copyfile(config,target);target.chmod(0o644)
units=release/'workspace/skills/manying-wechat-publisher/deploy'
for name in ['manying-publisher.service','manying-publisher.timer','manying-publisher-login.service','manying-publisher-desktop.service']:
    destination=pathlib.Path('/etc/systemd/system')/name
    if destination.exists():shutil.copy2(destination,ROOT/(name+'.before-'+RELEASE))
    shutil.copyfile(units/name,destination);destination.chmod(0o644)
old=os.readlink(ROOT/'current') if (ROOT/'current').is_symlink() else None
temporary=ROOT/('current-'+RELEASE);temporary.symlink_to(release);os.replace(temporary,ROOT/'current')
command(['systemd-analyze','verify','/etc/systemd/system/manying-publisher.service',
         '/etc/systemd/system/manying-publisher.timer','/etc/systemd/system/manying-publisher-desktop.service',
         '/etc/systemd/system/manying-publisher-login.service'],stdout=subprocess.DEVNULL)
command(['systemctl','daemon-reload'])
command(['systemctl','start','manying-publisher-desktop.service'])
time.sleep(4)
if prop('manying-publisher-desktop','ActiveState')!='active':raise RuntimeError('Private desktop failed')
command(['systemctl','enable','--now','manying-publisher.timer'],stdout=subprocess.DEVNULL)
python=str(ROOT/'venv/bin/python');runner=str(ROOT/'current/workspace/skills/manying-wechat-publisher/scripts/server_runner.py')
runtime_env=['HOME='+str(HOME),'CODEX_HOME='+str(HOME/'.codex'),'PLAYWRIGHT_BROWSERS_PATH='+str(ROOT/'browsers'),
             'DISPLAY=:97','XAUTHORITY='+str(HOME/'.Xauthority'),'MANYING_RPA_PROXY=http://127.0.0.1:17897']
if deployment_lock:
    fcntl.flock(deployment_lock,fcntl.LOCK_UN);deployment_lock.close()
doctor=subprocess.run(['runuser','-u','manying-publisher','--','env',*runtime_env,python,runner,'--config',str(target),'doctor'],capture_output=True,text=True,timeout=60)
with urllib.request.urlopen('http://127.0.0.1:8088/api/health',timeout=10) as response:health=json.load(response)
after={'pid':prop('manying','MainPID'),'active':prop('manying','ActiveState')}
receipt={'release':RELEASE,'sha256':DIGEST,'previous':old,'seeded_articles':seeded,'codex':version,
         'timer':prop('manying-publisher.timer','ActiveState'),'next':prop('manying-publisher.timer','NextElapseUSecRealtime'),
         'desktop':prop('manying-publisher-desktop','ActiveState'),'doctor':json.loads(doctor.stdout),
         'site_pid_unchanged':before==after,'site_healthy':bool(health.get('ok',health.get('status')=='ok'))}
(ROOT/('receipt-'+RELEASE+'.json')).write_text(json.dumps(receipt,ensure_ascii=False,indent=2))
print(json.dumps(receipt,ensure_ascii=False))
'''


def bundle(article_root, include_seed=True):
    stream=io.BytesIO()
    with tarfile.open(fileobj=stream,mode='w:gz') as package:
        for source in SKILL.rglob('*'):
            if source.is_file() and '__pycache__' not in source.parts and source.suffix != '.pyc':
                package.add(source,arcname='workspace/skills/manying-wechat-publisher/'+source.relative_to(SKILL).as_posix())
        package.add(SKILL/'deploy/AGENTS.md',arcname='workspace/AGENTS.md')
        count=0
        for manifest_file in article_root.glob('*/manifest.json'):
            manifest=json.loads(manifest_file.read_text(encoding='utf-8'))
            if manifest.get('status') != 'published':
                raise ValueError('Resolve all local in-flight articles before moving the publishing owner')
            if not include_seed:
                count+=1
                continue
            directory=manifest_file.parent
            names={'manifest.json','content.json','research.md','article.html','paste.html','native-images.json'}
            names.update(x['file'] for x in manifest.get('images',[]))
            for name in names:
                source=directory/name
                if source.is_file():package.add(source,arcname='seed-articles/'+directory.name+'/'+name)
            count+=1
    return stream.getvalue(),count


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('mode',choices=('inspect','apply'))
    parser.add_argument('--release')
    parser.add_argument('--authorized-production',action='store_true')
    parser.add_argument('--seed-from-release',help='Reuse an already uploaded immutable seed on this server')
    parser.add_argument('--ssh-interface',type=int,help='Use the domestic physical interface without changing TUN routing')
    args=parser.parse_args()
    if args.mode=='apply' and (not args.authorized_production or not args.release or not __import__('re').fullmatch(r'publisher-\d{8}-r\d+',args.release)):
        parser.error('An explicit production action and a new immutable release ID are required')
    if args.seed_from_release and not __import__('re').fullmatch(r'publisher-\d{8}-r\d+',args.seed_from_release):
        parser.error('Invalid seed release ID')
    prefix='MODE='+repr(args.mode)+'\nSEED_RELEASE='+repr(args.seed_from_release)+'\n'
    if args.mode=='apply':
        data,count=bundle(PROJECT/'.runtime/manying-articles',include_seed=not args.seed_from_release)
        digest=hashlib.sha256(data).hexdigest()
        prefix+='PACK='+repr(base64.b64encode(data).decode())+'\nDIGEST='+repr(digest)+'\nRELEASE='+repr(args.release)+'\n'
        print(json.dumps({'phase':'upload_and_install','seeded':count,'bytes':len(data),'sha256':digest}),flush=True)
    transport=SSH.copy()
    if args.ssh_interface:
        from china_relay import ssh_proxy_option
        transport[1:1]=ssh_proxy_option(args.ssh_interface)
    result=subprocess.run(transport+['sudo -n python3 -'],input=(prefix+REMOTE).encode('utf-8'),
                          capture_output=True,timeout=2200,
                          creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
    print(result.stdout.decode('utf-8',errors='replace'))
    if result.returncode:
        print(result.stderr.decode('utf-8',errors='replace')[-1500:])
        raise SystemExit(result.returncode)


if __name__=='__main__':main()
