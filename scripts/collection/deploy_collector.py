"""Freeze reviewed collector source/stop evidence and install on the known server."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile

PROJECT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(PROJECT/'skills/manying-wechat-publisher/scripts'))
from china_relay import ssh_proxy_option
HOME=Path.home()
BASE=['-i',str(HOME/'.ssh/manying_deploy_20260929'),'-o','BatchMode=yes',
      '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=12']
TARGET='ubuntu@43.134.92.99'

def run(command,**kwargs):
    return subprocess.run(command,check=True,creationflags=subprocess.CREATE_NO_WINDOW,**kwargs)

def archive(path, files):
    manifest={name:hashlib.sha256(data).hexdigest() for name,data in files.items()}
    files={**files,'package-manifest.json':json.dumps(manifest,sort_keys=True).encode()}
    with tarfile.open(path,'w:gz') as output:
        for name,data in sorted(files.items()):
            item=tarfile.TarInfo(name);item.size=len(data);item.mode=0o600;item.mtime=0
            output.addfile(item,io.BytesIO(data))
    return hashlib.sha256(path.read_bytes()).hexdigest()

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--release',required=True)
    parser.add_argument('--authorized-production',action='store_true')
    args=parser.parse_args()
    import re
    if not args.authorized_production or not re.fullmatch(r'collector-\d{8}-r\d+',args.release):
        parser.error('Explicit server collector release authorization required')
    root=PROJECT/'.runtime/collection-server';root.mkdir(parents=True,exist_ok=True)
    state=json.loads((PROJECT/'Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json').read_text())
    recovery=json.loads((PROJECT/'.runtime/main-agent/recovery-current.json').read_text())
    outbox=json.loads((PROJECT/'.runtime/production-sync/live-events.json').read_text())
    if (not state.get('collection_paused') or not recovery.get('stop_complete')
            or outbox.get('pending') or outbox.get('dirty') or outbox.get('worker')):
        raise RuntimeError('Verified local stop and exact outbox settlement required')
    owner=state['collection_active_owner'];work=Path(state['threads'][owner]['work'])
    if state['threads'][owner].get('active_batch') or state['threads'][owner].get('mode')!='idle':
        raise RuntimeError('Original collector has an unsettled claim')
    source={}
    roots=['scripts/collection','web','Datebase/xxmanmi-resource-db','references',
           'skills/xxmanmi-video-downloader','skills/manying-production-sync','skills/manying-project-deploy']
    extensions={'.py','.mjs','.js','.css','.html','.svg','.png','.jpg','.webp','.woff','.woff2','.sql','.txt','.md','.toml','.yaml','.yml'}
    for prefix in roots:
        for folder,directories,names in os.walk(PROJECT/prefix,followlinks=False):
            directories[:]=[name for name in directories if name not in {'.runtime','.secrets','.git','.venv','node_modules','__pycache__'}]
            for name in names:
                path=Path(folder)/name;relative=path.relative_to(PROJECT)
                if path.is_symlink() or path.suffix.lower() not in extensions or name=='config.local.json':
                    continue
                if 'static' in relative.parts and path.suffix.lower() in {'.png','.jpg','.webp','.woff','.woff2'}:
                    continue
                source[relative.as_posix()]=path.read_bytes()
    for name in ['AGENTS.md','README.md']:
        source[name]=(PROJECT/name).read_bytes()
    handoff={}
    bindings={'coordination.json':'Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json',
        'main.json':'.runtime/main-agent/state.json','policy.json':'.runtime/main-agent/automation-policy.json',
        'recovery.json':'.runtime/main-agent/recovery-current.json',
        'original-native-goal.json':'.runtime/collection-server/original-native-goal.json',
        'local-handoff.md':'.runtime/collection-server/local-handoff.md',
        'outbox-production-verification.json':'.runtime/collection-server/outbox-production-verification.json'}
    for name,relative in bindings.items():handoff[name]=(PROJECT/relative).read_bytes()
    for path in work.iterdir():
        if path.is_file() and not path.is_symlink() and path.suffix in {'.json','.ndjson','.md'}:
            handoff['original-work/'+path.name]=path.read_bytes()
    deletions=PROJECT/'Datebase/xxmanmi-resource-db/.runtime/program-deletions.json'
    if deletions.exists():handoff['program-deletions.json']=deletions.read_bytes()
    code=root/(args.release+'-code.tar.gz');evidence=root/(args.release+'-handoff.tar.gz')
    code_sha=archive(code,source);handoff_sha=archive(evidence,handoff)
    (root/'local-disabled.json').write_text(json.dumps({'retired':True,'server':'43.134.92.99',
        'original_owner':owner,'release':args.release,'reason':'User requires server-only production collection'},ensure_ascii=False,indent=2))
    proxy=ssh_proxy_option(20)
    run(['ssh',*proxy,*BASE,TARGET,'install -d -m 0700 /tmp/manying-collector-inbox'],capture_output=True,timeout=30)
    for path in [code,evidence]:
        run(['scp','-q',*proxy,*BASE,str(path),TARGET+':/tmp/manying-collector-inbox/'+path.name],capture_output=True,timeout=180)
    installer=(PROJECT/'scripts/collection/install_collector.py').read_bytes()
    remote='sudo -n python3 - '+args.release+' '+code_sha+' '+handoff_sha
    process=subprocess.Popen(['ssh',*proxy,*BASE,TARGET,remote],stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    process.stdin.write(installer);process.stdin.close()
    for line in iter(process.stdout.readline,b''):
        print(line.decode('utf-8','replace'),end='',flush=True)
    raise SystemExit(process.wait())

if __name__=='__main__':main()
