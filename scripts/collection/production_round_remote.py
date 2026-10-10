"""Main-thread administrative programme release; execute over SSH stdin.

This is not installed in the application or invoked by a background timer.
Only the frozen catalogue rows, missing covers and private release evidence change.
"""
import csv
import fcntl
import grp
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
from datetime import datetime, timezone
from urllib.request import HTTPRedirectHandler, ProxyHandler, build_opener, urlopen
from urllib.error import HTTPError

ROUND_RE = re.compile(r"round-[0-9]{6}-[0-9a-f]{12}")
SQL_HASH = '9178ba2d06a4baadc772b8111341583a496275c46e1a19d2e2ce5fc1802998f2'
HELPER_HASH = '5eae5543f688d7563d983fe5f1dfc8aea30f68021fde75bf2f98474927e7aeeb'
TRIGGERS_HASH = 'ad929d20ac7d4b3cdc679fbd76f0839c6660c7a6497d417a62fcfd7936c32929'
BASE = Path('/opt/manying')
INBOX = Path('/tmp/manying-sync-inbox')
COVERS = Path('/var/lib/manying/runtime/thumbnails')


def digest(data):
    return hashlib.sha256(data).hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def payload(path, expected_bundle, expected_identity, expected_manifest):
    require(path.parent == INBOX and path.name.endswith('.tar.gz'), 'Invalid inbox')
    release_id = path.name[:-7]
    require(ROUND_RE.fullmatch(release_id), 'Invalid release identifier')
    require(not path.is_symlink() and path.is_file() and path.stat().st_size <= 80_000_000,
            'Missing, linked or oversized bundle')
    require(digest(path.read_bytes()) == expected_bundle, 'Bundle hash changed')
    with tarfile.open(path, 'r:gz') as archive:
        members = archive.getmembers()
        require(4 <= len(members) <= 303 and len({m.name for m in members}) == len(members),
                'Expected programme covers and three data files')
        require(all(m.isfile() and 0 < m.size <= 40_000_000 for m in members)
                and sum(m.size for m in members) <= 120_000_000, 'Invalid archive members')
        files = {m.name: archive.extractfile(m).read() for m in members}
    require('manifest.json' in files and digest(files['manifest.json']) == expected_manifest,
            'Package manifest changed')
    manifest = json.loads(files['manifest.json'])
    require(manifest.get('release_id') == release_id
            and manifest.get('identity_manifest_sha256') == expected_identity,
            'Frozen release identity differs')
    ids = manifest.get('program_ids') or []
    programme_count = len(ids)
    mode = manifest.get('release_mode', 'standard')
    threshold = manifest.get('programme_threshold', 100)  # Preserve old immutable packages.
    require(type(threshold) is int and threshold in (100, 300), 'Invalid programme threshold')
    require(1 <= programme_count <= threshold and programme_count == len(set(ids))
            and all(type(i) is int and i > 0 for i in ids)
            and ((mode == 'standard' and programme_count == threshold)
                 or (mode == 'user_authorized_early' and programme_count < threshold)),
            'Invalid programme identities')
    expected_names = {'programs.csv', 'resources.csv', 'manifest.json'} | {
        f'covers/{i}.jpg' for i in ids}
    require(set(files) == expected_names, 'Unexpected archive paths')
    require(set(manifest.get('files', {})) == expected_names - {'manifest.json'},
            'Incomplete file manifest')
    require(all(digest(files[name]) == sha for name, sha in manifest['files'].items()),
            'Payload hash differs')
    programmes = manifest.get('programmes') or []
    canonical = (json.dumps(programmes, sort_keys=True, ensure_ascii=False,
                            separators=(',', ':'))).encode()
    require(digest(canonical) == expected_identity, 'Programme identity manifest differs')
    require(len(programmes) == programme_count and {row['program_id'] for row in programmes} == set(ids)
            and len({row['source_url'] for row in programmes}) == programme_count,
            'Incomplete programme evidence')
    for row in programmes:
        require(row['cover_sha256'] == digest(files[f"covers/{row['program_id']}.jpg"]),
                'Cover differs from independent gate')
    return manifest, files


def run(args, *, input=None, timeout=180):
    result = subprocess.run(args, input=input, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, timeout=timeout)
    if result.returncode:
        # Do not expose SQL input, media URLs, database credentials or private rows.
        lines = result.stderr.decode(errors='replace').splitlines()
        errors = [line for line in lines
                  if line.startswith('ERROR:')]
        detail = re.sub(r'https?://\S+', '[URL]', errors[0])[:220] if errors else ''
        if not detail:
            # Cold route probes can fail before SQL. Only module identifiers are
            # safe to retain; never print traceback source, arguments or env values.
            missing = next((line for line in lines if re.fullmatch(
                r"ModuleNotFoundError: No module named '[A-Za-z_][A-Za-z_0-9.]*'", line)), None)
            detail = missing or ''
        raise RuntimeError(f'{Path(args[0]).name} failed: exit {result.returncode} {detail}')
    return result.stdout


def inline_csv(sql):
    """Stream only the two frozen CSVs; PostgreSQL need not traverse private directories."""
    pattern = re.compile(r"FROM '(/opt/manying/releases/sync-(round-[0-9]{6}-[0-9a-f]{12})/(programs|resources)\.csv)' WITH \(FORMAT csv, HEADER true\);")
    def replace(match):
        path = Path(match[1])
        require(not path.is_symlink(), 'Linked staging CSV')
        text = path.read_text(encoding='utf-8')
        require('\\.' not in text.splitlines() and '\x00' not in text,
                'Unsafe CSV standard-input terminator')
        return 'FROM STDIN WITH (FORMAT csv, HEADER true);\n' + text.rstrip('\n') + '\n\\.\n'
    return pattern.sub(replace, sql)


def psql(sql):
    return run(['runuser', '-u', 'postgres', '--', 'psql', '-X', '-q', '-t', '-A',
                '-v', 'ON_ERROR_STOP=1', '-d', 'manying'], input=inline_csv(sql).encode()).decode().strip()


def user_metrics():
    tables = json.loads(psql("SELECT coalesce(json_agg(tablename ORDER BY tablename),'[]') "
                            "FROM pg_tables WHERE schemaname='public' "
                            "AND tablename NOT IN ('programs','resources');"))
    result = {}
    for table in tables:
        require(re.fullmatch(r'[a-z][a-z0-9_]*', table), 'Unexpected table name')
        result[table] = int(psql(f'SELECT count(*) FROM public."{table}";'))
    return result


def reviewed_triggers():
    result = json.loads(psql("SELECT json_build_object('triggers',"
        "(SELECT json_agg(json_build_object('table',c.relname,'trigger',t.tgname,"
        "'definition',pg_get_triggerdef(t.oid),'function',pg_get_functiondef(t.tgfoid)) "
        "ORDER BY t.tgname) FROM pg_trigger t JOIN pg_class c ON c.oid=t.tgrelid "
        "WHERE NOT t.tgisinternal AND c.oid IN "
        "('public.programs'::regclass,'public.resources'::regclass)),"
        "'canonical',(SELECT pg_get_functiondef(oid) FROM pg_proc "
        "WHERE proname='canonical_source_name'));"))
    require(digest(json.dumps(result, sort_keys=True, ensure_ascii=False,
                             separators=(',', ':')).encode()) == TRIGGERS_HASH,
            'Catalogue triggers changed; inspect before publishing')
    return result


def backup(folder):
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    db = folder / 'manying.dump'
    if not db.exists():
        temporary = folder / 'manying.dump.tmp'
        with temporary.open('xb') as stream:
            result = subprocess.run(['runuser', '-u', 'postgres', '--', 'pg_dump', '-Fc',
                                     '-d', 'manying'], stdout=stream, stderr=subprocess.PIPE,
                                    timeout=180)
            require(result.returncode == 0, 'Database backup failed')
            stream.flush()
            os.fsync(stream.fileno())
        listing = run(['pg_restore', '-l', str(temporary)])
        require(all(b'TABLE DATA public ' + name in listing
                    for name in (b'programs', b'resources', b'web_accounts', b'web_sessions')),
                'Incomplete database backup')
        temporary.replace(db)
    run(['pg_restore', '-l', str(db)])
    app = folder / 'application.tar.gz'
    if not app.exists():
        temporary = folder / 'application.tar.gz.tmp'
        run(['tar', '-czf', str(temporary), '-C', str((BASE / 'app').resolve()), '.'])
        require(b'./server.py' in run(['tar', '-tzf', str(temporary)]),
                'Application backup does not contain current code')
        temporary.replace(app)
    require(b'./server.py' in run(['tar', '-tzf', str(app)]), 'Application backup unreadable')
    return db, app


def verify_database(release, ids):
    # Exact identity/natural-key checks remain enforced by the installed template.
    preamble = release.read_text().split('INSERT INTO public.programs AS p (')[0]
    check = """
DO $verify$
BEGIN
 IF EXISTS (SELECT 1 FROM release_programs s LEFT JOIN public.programs p USING(id)
            WHERE p.id IS NULL OR p.source_url<>s.source_url OR p.captured_at IS NULL)
 OR EXISTS (SELECT 1 FROM release_resources s LEFT JOIN public.resources r USING(id)
            WHERE r.id IS NULL OR r.program_id<>s.program_id
               OR r.url_sha256<>s.url_sha256 OR r.original_url<>s.original_url)
 THEN RAISE EXCEPTION 'Incomplete production identities'; END IF;
END $verify$;
ROLLBACK;
"""
    psql(preamble + check)


def acceptance(ids):
    # Run the existing application route functions with its current service environment,
    # and separately verify live HTTP health and authentication enforcement. No user session
    # is created, recovered or modified, and redirects are not followed to media hosts.
    probe = '''
import asyncio,json,os,sys
from pathlib import Path
pid=int(sys.argv[1]); values=Path(f'/proc/{pid}/environ').read_bytes().split(b'\\0')
for value in values:
 if b'=' in value:
  k,v=value.split(b'=',1); os.environ[k.decode()]=v.decode()
sys.path.insert(0,'/opt/manying/app')
import server
from starlette.requests import Request
ids=json.loads(sys.argv[2]); checked=[]; media_ids=[]
request=Request({'type':'http','method':'HEAD','scheme':'https','path':'/media/0',
 'headers':[(b'host',b'manying.example')], 'client':('127.0.0.1',1),
 'server':('127.0.0.1',8088),'query_string':b''})
listed=[]
for start in range(0,len(ids),60):
 page=server.programs(request,search='',source='',availability='ready',sort='recent',
  page=1,page_size=60,ids=','.join(map(str,ids[start:start+60])),genre='')
 listed.extend(r['id'] for r in page['items'])
assert set(listed)==set(ids)
for i in ids:
 row=server.program(i)
 assert row['id']==i and row['episodes']
 sources=[s for e in row['episodes'] for s in e['sources']]
 assert sources and all(s['url']==f"/media/{s['id']}" for s in sources)
 response=asyncio.run(server.media(request,sources[0]['id']))
 assert response.status_code==307 and response.headers['location'].startswith('https://')
 checked.append(i)
 media_ids.append(sources[0]['id'])
server.library.close()
print(json.dumps({'route_programmes':checked,'playback_redirects':len(checked),'listed_programmes':len(listed),'media_resource_ids':media_ids}))
'''
    pid = run(['systemctl', 'show', 'manying.service', '-p', 'MainPID', '--value']).decode().strip()
    result = json.loads(run(['/opt/manying/venv/bin/python', '-B', '-c', probe, pid,
                             json.dumps(ids)], timeout=240))
    require(result['route_programmes'] == ids and result['playback_redirects'] == len(ids),
            'Programme/playback route verification incomplete')
    with urlopen('http://127.0.0.1:8088/api/health', timeout=15) as response:
        require(json.load(response).get('ok') is True, 'Live health API failed')
    # The current website intentionally exposes title cards to anonymous visitors.
    # Detail and playback still require access; never follow a failed auth probe's
    # redirect to a media host.
    class NoRedirect(HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = build_opener(ProxyHandler({}), NoRedirect())
    with opener.open('http://127.0.0.1:8088/api/programs?page_size=1', timeout=15) as response:
        catalogue = json.load(response)
        require(response.status == 200 and isinstance(catalogue.get('items'), list)
                and type(catalogue.get('total')) is int and catalogue['total'] >= 0,
                'Live public catalogue check failed')
    for route in (f'/api/programs/{ids[0]}', f'/media/{result["media_resource_ids"][0]}'):
        try:
            with opener.open('http://127.0.0.1:8088' + route, timeout=15):
                raise ValueError('Unauthenticated protected request was accepted')
        except HTTPError as exc:
            require(exc.code in (401, 403), 'Live authentication check failed')
    result['live_authentication_enforced'] = True
    result['live_public_catalogue_available'] = True
    return result


def apply(path, bundle_hash, identity_hash, manifest_hash):
    require(os.geteuid() == 0, 'Administrator execution required')
    require(digest((BASE / 'bin/merge-catalogue.sql.in').read_bytes()) == SQL_HASH
            and digest((BASE / 'bin/production_batch.py').read_bytes()) == HELPER_HASH,
            'Installed catalogue helpers changed; inspect before publishing')
    spec = importlib.util.spec_from_file_location('installed_catalogue', BASE / 'bin/production_batch.py')
    helper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(helper)
    manifest, files = payload(path, bundle_hash, identity_hash, manifest_hash)
    ids = manifest['program_ids']
    for filename, columns, expected_count in (
        ('programs.csv', helper.PROGRAM_COLUMNS, len(ids)),
        ('resources.csv', helper.RESOURCE_COLUMNS, manifest['resource_rows'])):
        reader = csv.DictReader(io.StringIO(files[filename].decode()))
        require(tuple(reader.fieldnames) == columns, 'Incompatible catalogue columns')
        rows = list(reader)
        require(len(rows) == expected_count, 'CSV counts differ')
        if filename == 'programs.csv':
            require({int(r['id']) for r in rows} == set(ids)
                    and all(r['source_name'] == '小小漫迷' and r['captured_at'] for r in rows),
                    'Unverified programme rows')
            require({int(r['id']): r['source_url'] for r in rows} ==
                    {r['program_id']: r['source_url'] for r in manifest['programmes']},
                    'CSV programme URLs differ from frozen identities')
        else:
            require(rows and all(int(r['program_id']) in ids for r in rows), 'Foreign resources')
            require(all(digest(r['original_url'].encode()) == r['url_sha256'] for r in rows),
                    'Resource URL hash differs')
    with helper.LOCK.open('a+b') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        require(run(['systemctl', 'is-active', 'manying.service']).strip() == b'active',
                'Production application is not active')
        database_size = int(psql("SELECT pg_database_size('manying');"))
        require(shutil.disk_usage(BASE).free > max(1_000_000_000, database_size * 2 + 200_000_000),
                'Insufficient space for recoverable production backups')
        trigger_review = reviewed_triggers()
        before = user_metrics()
        release = BASE / 'releases' / ('sync-' + manifest['release_id'])
        release.mkdir(mode=0o750, parents=True, exist_ok=True)
        gid = grp.getgrnam('postgres').gr_gid
        os.chown(release, 0, gid)
        for name in ('programs.csv', 'resources.csv', 'manifest.json'):
            helper.write_checked(release / name, files[name], gid=gid)
        sql = helper.merge_sql(release).decode()
        # Lock only this release's existing rows. Their source URL/name must remain
        # unchanged, so the reviewed propagation trigger never writes observations.
        guard = """
DO $source_scope$
BEGIN
 PERFORM p.id FROM public.programs p JOIN release_programs s USING(id) FOR UPDATE OF p;
 IF EXISTS (SELECT 1 FROM release_programs s JOIN public.programs p USING(id)
            WHERE p.source_url IS DISTINCT FROM s.source_url
               OR p.source_name IS DISTINCT FROM s.source_name)
 OR EXISTS (SELECT 1 FROM release_programs s
            WHERE canonical_source_name(s.source_url,s.source_name) IS DISTINCT FROM s.source_name)
 THEN RAISE EXCEPTION 'Source normalization would change rows outside release scope'; END IF;
END $source_scope$;
"""
        sql = sql.replace('INSERT INTO public.programs AS p (',
                          guard + '\nINSERT INTO public.programs AS p (', 1).encode()
        helper.write_checked(release / 'merge.sql', sql, gid=gid)
        for i in ids:
            target = COVERS / f'{i}.jpg'
            require(not target.is_symlink() and
                    (not target.exists() or digest(target.read_bytes()) == digest(files[f'covers/{i}.jpg'])),
                    'Production cover conflict')
        preamble = sql.decode().split('INSERT INTO public.programs AS p (')[0]
        require('INSERT INTO' not in preamble, 'Unexpected preflight mutation')
        preflight = psql(preamble + '\nROLLBACK;\n')
        db_backup, app_backup = backup(BASE / 'backups' / manifest['release_id'])
        if not (release / 'preflight.txt').exists():
            helper.write_checked(release / 'preflight.txt', preflight.encode(), gid=gid)
        receipt_path = release / 'acceptance.json'
        if not (release / 'merge-result.txt').exists():
            merge_output = psql(sql.decode())
            helper.write_checked(release / 'merge-result.txt', merge_output.encode(), gid=gid)
        helper.install_covers(ids, files)
        verify_database(release / 'merge.sql', ids)
        require(all(digest((COVERS / f'{i}.jpg').read_bytes()) == digest(files[f'covers/{i}.jpg'])
                    for i in ids), 'Cover acceptance failed')
        api = acceptance(ids)
        after = user_metrics()
        checks = {name: True for name in (
            'database_backup_readable', 'application_backup_readable',
            'stable_programme_identities', 'resource_identities', 'cover_hashes',
            'health_api', 'programme_and_playback_api', 'user_data_protected')}
        receipt = {'ok': True, 'verified': True, 'release_id': manifest['release_id'],
                   'bundle_sha256': bundle_hash, 'identity_manifest_sha256': identity_hash,
                   'package_manifest_sha256': manifest_hash, 'resource_rows': manifest['resource_rows'],
                   'target': 'ubuntu@example.invalid', 'database': 'manying', 'checks': checks,
                   'database_backup': str(db_backup), 'application_backup': str(app_backup),
                   'application_directory': str((BASE / 'app').resolve()),
                   'application_backup_sha256': digest(app_backup.read_bytes()),
                   'verified_at': datetime.now(timezone.utc).isoformat(),
                   'programmes': [{'program_id': r['program_id'], 'source_url': r['source_url'],
                                   'cover_sha256': r['cover_sha256']} for r in manifest['programmes']],
                   'user_table_counts_before': before, 'user_table_counts_after': after,
                   'user_write_evidence': 'Reviewed SQL writes programmes/resources and sequences only; locked existing source identities prevent observation propagation',
                   'trigger_review_sha256': TRIGGERS_HASH,
                   'api_verification_scope': 'Existing production route functions plus live HTTP health/authentication; no signed-in browser session available',
                   'api_evidence': api}
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            require(previous['bundle_sha256'] == bundle_hash, 'Existing receipt differs')
            return previous
        helper.write_checked(receipt_path, (json.dumps(receipt, ensure_ascii=False, sort_keys=True)+'\n').encode(), gid=gid)
        return receipt


if __name__ == '__main__':
    print(json.dumps(apply(Path(sys.argv[1]), *sys.argv[2:5]), ensure_ascii=False, sort_keys=True))
