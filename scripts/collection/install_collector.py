"""Root installer fed through authenticated SSH; leaves collection stopped for QA."""
import hashlib
import grp
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shutil
import subprocess
import sys
import tarfile

def run(command,**kwargs):
    result=subprocess.run(command,capture_output=True,check=False,**kwargs)
    if result.returncode:
        detail=result.stderr.decode('utf-8','replace')[-450:]
        if globals().get('password'):detail=detail.replace(password,'[REDACTED]')
        raise RuntimeError('Installer command failed: '+Path(command[0]).name+'; '+detail)
    return result.stdout

def progress(stage,**extra):print(json.dumps({'install':stage,**extra}),flush=True)

def private(path,data,uid=0,gid=0,mode=0o600):
    path.parent.mkdir(parents=True,exist_ok=True)
    path.write_bytes(data if isinstance(data,bytes) else data.encode())
    path.chmod(mode);os.chown(path,uid,gid)

def extract(archive,target,expected):
    if hashlib.sha256(archive.read_bytes()).hexdigest()!=expected:raise ValueError('Package digest changed')
    with tarfile.open(archive) as bundle:
        entries=bundle.getmembers()
        if any(not entry.isfile() or Path(entry.name).is_absolute() or '..' in Path(entry.name).parts for entry in entries):
            raise ValueError('Unexpected package entry')
        manifest=json.loads(bundle.extractfile('package-manifest.json').read())
        if set(manifest)!={entry.name for entry in entries if entry.name!='package-manifest.json'}:
            raise ValueError('Package manifest scope differs')
        for name,digest in manifest.items():
            data=bundle.extractfile(name).read()
            if hashlib.sha256(data).hexdigest()!=digest:raise ValueError('Package member changed')
            destination=target/name
            if destination.is_symlink():raise ValueError('Unexpected existing source link')
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(data);destination.chmod(0o644)
    return manifest

release,code_sha,handoff_sha=sys.argv[1:]
if os.geteuid()!=0 or not re.fullmatch(r'collector-\d{8}-r\d+',release):raise ValueError('Expected authorized root deployment')
base=Path('/opt/manying-collector');home=Path('/var/lib/manying-collector');project=home/'workspace'
for unit in ['manying-collector.service','manying-collector-cover.service']:
    active=subprocess.run(['systemctl','is-active',unit],capture_output=True,text=True).stdout.strip()
    if active in {'active','activating','deactivating'}:raise ValueError('Stop original collector before changing its source')
try:group=grp.getgrnam('manying-collector')
except KeyError:
    run(['groupadd','--system','manying-collector']);group=grp.getgrnam('manying-collector')
try:account=pwd.getpwnam('manying-collector')
except KeyError:
    run(['useradd','--system','--gid','manying-collector','--home-dir',str(home),'--shell','/usr/sbin/nologin','manying-collector']);account=pwd.getpwnam('manying-collector')
if account.pw_gid!=group.gr_gid:
    run(['usermod','--gid','manying-collector','manying-collector']);account=pwd.getpwnam('manying-collector')
uid,gid=account.pw_uid,account.pw_gid
home.mkdir(parents=True,exist_ok=True);home.chmod(0o750);os.chown(home,0,gid)
base.mkdir(parents=True,exist_ok=True)
stage=base/'releases'/release
if stage.exists() and (stage/'code.sha256').exists() and (stage/'code.sha256').read_text()!=code_sha:
    raise ValueError('Immutable release name already contains different code')
inbox=Path('/tmp/manying-collector-inbox')
manifest=extract(inbox/(release+'-code.tar.gz'),stage/'source',code_sha)
private(stage/'code.sha256',code_sha,mode=0o644)
project.mkdir(parents=True,exist_ok=True)
for name in manifest:
    target=project/name;target.parent.mkdir(parents=True,exist_ok=True)
    if target.is_symlink():raise ValueError('Unexpected workspace source link')
    shutil.copyfile(stage/'source'/name,target);target.chmod(0o644);os.chown(target,0,0)
for parent in [project,*[p for p in project.rglob('*') if p.is_dir() and '.runtime' not in p.parts]]:
    parent.chmod(0o755)
for path in [project/'.runtime',project/'.runtime/collection-server',project/'.runtime/main-agent',
             project/'Datebase/xxmanmi-resource-db/.runtime',home/'.codex',home/'cover-runtime',home/'secrets',
             home/'user-home',home/'user-home/.config',home/'user-home/.cache']:
    path.mkdir(parents=True,exist_ok=True);path.chmod(0o700);os.chown(path,uid,gid)
runtime=project/'.runtime/collection-server'
if not (runtime/'host.json').exists():
    migration=runtime/'migration';extract(inbox/(release+'-handoff.tar.gz'),migration,handoff_sha)
    for path in [migration,*migration.rglob('*')]:
        os.chown(path,uid,gid);path.chmod(0o700 if path.is_dir() else 0o600)
    deletion=migration/'program-deletions.json'
    if deletion.exists():private(project/'Datebase/xxmanmi-resource-db/.runtime/program-deletions.json',deletion.read_bytes(),uid,gid)
progress('source_and_original_evidence_installed',source_files=len(manifest))

backup=Path('/var/backups/manying')/release;backup.mkdir(parents=True,exist_ok=True);backup.chmod(0o700)
dump=backup/'database.dump'
if not dump.exists():
    with dump.open('wb') as stream:
        result=subprocess.run(['sudo','-n','-u','postgres','pg_dump','-Fc','-d','manying'],stdout=stream,stderr=subprocess.PIPE)
        if result.returncode:raise RuntimeError('Production database backup failed')
    dump.chmod(0o600)
run(['pg_restore','--list',str(dump)])
progress('production_backup_readable',bytes=dump.stat().st_size)

secret=home/'secrets/postgres_password.txt'
if not secret.exists():private(secret,secrets.token_urlsafe(36),uid,gid)
password=secret.read_text().strip()
role="DO $$ BEGIN IF NOT EXISTS(SELECT 1 FROM pg_roles WHERE rolname='manying_collector') THEN CREATE ROLE manying_collector LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION; END IF; END $$;\n"
role+="ALTER ROLE manying_collector PASSWORD '"+password.replace("'","''")+"';\n"
ddl=(project/'scripts/collection/server-schema.sql').read_text()
run(['sudo','-n','-u','postgres','psql','-X','-q','-v','ON_ERROR_STOP=1','-d','manying'],input=(role+ddl).encode())
protected=run(['sudo','-n','-u','postgres','psql','-X','-A','-t','-d','manying','-c',
    "SELECT bool_or(has_table_privilege('manying_collector',quote_ident(schemaname)||'.'||quote_ident(tablename),'SELECT,INSERT,UPDATE,DELETE')) FROM pg_tables WHERE schemaname='public' AND tablename LIKE 'web_%'"]).decode().strip()
if protected!='f':raise RuntimeError('Collector role unexpectedly has production user-table access')
progress('staging_schema_ready',user_table_access=False)

config=Path('/etc/manying-collector');config.mkdir(parents=True,exist_ok=True);config.chmod(0o750);os.chown(config,0,gid)
value={'mode':'production','network':'native-direct','database':'manying','host':'127.0.0.1','port':5432,
       'user':'manying_collector','password_file':str(secret),'production_covers':'/var/lib/manying/runtime/thumbnails'}
private(config/'collector.json',json.dumps(value,indent=2)+'\n',0,gid,0o640)
if not (home/'.codex/auth.json').exists():
    private(home/'.codex/auth.json',Path('/var/lib/manying-publisher/.codex/auth.json').read_bytes(),uid,gid)
private(home/'.codex/config.toml','approval_policy = "never"\nsandbox_mode = "danger-full-access"\n',uid,gid)
(base/'bin').mkdir(exist_ok=True)
canonical=Path('/opt/manying-publisher/bin/codex').resolve().parent
for name in ['codex','codex-code-mode-host']:
    source=canonical/name
    if not source.is_file():raise RuntimeError('Official Codex runtime companion is missing')
    shutil.copy2(source,base/'bin'/name);(base/'bin'/name).chmod(0o755)
if not (base/'venv/bin/python').exists():run(['python3','-m','venv',str(base/'venv')])
run([str(base/'venv/bin/pip'),'install','--disable-pip-version-check','-q','-r',str(project/'web/requirements.txt'),'playwright==1.64.0'])
if not (base/'browsers').exists():
    shutil.copytree('/opt/manying-publisher/browsers',base/'browsers',copy_function=os.link,ignore=shutil.ignore_patterns('.links'))
progress('dedicated_model_and_browser_runtime_installed')

if not shutil.which('setfacl'):
    run(['apt-get','update','-qq']);run(['apt-get','install','-y','-qq','acl'],env={**os.environ,'DEBIAN_FRONTEND':'noninteractive'})
for path in ['/var/lib/manying','/var/lib/manying/runtime']:
    run(['setfacl','-m','u:manying-collector:--x',path])
covers=Path(value['production_covers'])
run(['setfacl','-m','u:manying-collector:rwx',str(covers)])
run(['setfacl','-d','-m','u:manying-collector:rwx,u:manying:r-x',str(covers)])
for path in covers.iterdir():
    if path.is_file() and not path.is_symlink():run(['setfacl','-m','u:manying-collector:r--',str(path)])
environment={
 'HOME':str(home/'user-home'),'CODEX_HOME':str(home/'.codex'),'XXMANMI_CODEX_BIN':str(base/'bin/codex'),
 'XDG_CONFIG_HOME':str(home/'user-home/.config'),'XDG_CACHE_HOME':str(home/'user-home/.cache'),'SHELL':'/bin/bash',
 'MANYING_COLLECTION_MODE':'production','MANYING_COLLECTION_CONFIG':str(config/'collector.json'),
 'MANYING_RUNTIME':str(home/'cover-runtime'),'MANYING_PORT':'8091','MANYING_HOST':'127.0.0.1',
 'IMAGEIO_FFMPEG_EXE':'/usr/bin/ffmpeg',
 'MANYING_COVER_REFERENCE_DIRECTORY':str(covers),'MANYING_THUMBNAILS':'0',
 'PLAYWRIGHT_BROWSERS_PATH':str(base/'browsers'),'PYTHONUTF8':'1',
 'PYTHONPATH':str(project/'scripts/collection')+':'+str(project/'web'),
 'PATH':str(base/'bin')+':'+str(base/'venv/bin')+':/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'}
private(config/'worker.env',''.join(key+'='+value+'\n' for key,value in environment.items()),0,gid,0o640)
security='''User=manying-collector
Group=manying-collector
EnvironmentFile=/etc/manying-collector/worker.env
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
InaccessiblePaths=/etc/manying /opt/manying
ReadWritePaths=/var/lib/manying-collector /var/lib/manying/runtime/thumbnails
TasksMax=256
Nice=10
KillMode=control-group
TimeoutStopSec=45
'''
host_unit='''[Unit]
Description=Manying server collection native Codex Goal
After=network-online.target postgresql.service manying-collector-cover.service
Wants=network-online.target
Wants=manying-collector-cover.service
[Service]
Type=simple
'''+security+f'''WorkingDirectory={project}
ExecStart={base}/venv/bin/python -u -B scripts/collection/collector_host.py
Restart=on-failure
RestartSec=30
MemoryHigh=900M
MemoryMax=1250M
MemorySwapMax=768M
CPUQuota=100%
[Install]
WantedBy=multi-user.target
'''
cover_unit='''[Unit]
Description=Manying isolated staging cover worker
After=network-online.target postgresql.service
PartOf=manying-collector.service
[Service]
Type=simple
'''+security+f'''Environment=MANYING_COLLECTION_STAGE=1
WorkingDirectory={project}/web
ExecStart={base}/venv/bin/python -m uvicorn server_cover:app --host 127.0.0.1 --port 8091 --workers 1
Restart=on-failure
RestartSec=15
MemoryHigh=500M
MemoryMax=750M
MemorySwapMax=512M
CPUQuota=70%
'''
for name,data in [('manying-collector.service',host_unit),('manying-collector-cover.service',cover_unit)]:
    private(Path('/etc/systemd/system')/name,data,mode=0o644)
run(['systemctl','daemon-reload'])
private(runtime/'installation.json',json.dumps({'release':release,'code_sha256':code_sha,'handoff_sha256':handoff_sha,
    'source_files':len(manifest),'enabled':False,'backup':str(dump),'user_tables_protected':True},indent=2),uid,gid)
progress('installed_stopped_for_validation',release=release)
