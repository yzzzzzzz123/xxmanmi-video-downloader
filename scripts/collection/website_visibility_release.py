"""Scoped public-catalogue fix: preserve current production files and all DB rows."""
import argparse
import hashlib
import json
from pathlib import Path
import subprocess

from production_sync import HOST, KEY, PROJECT, WorkerLock, hidden_options, save_json

FOLDER = PROJECT / '.runtime/website-visibility-r60'
PATCHES = FOLDER / 'patches.json'
MANIFEST = FOLDER / 'manifest.json'
RECEIPT = FOLDER / 'receipt.json'


def remote(mode, manifest=None):
    patches = json.loads(PATCHES.read_text('utf-8'))
    library = Path(__file__).with_name('production_round_remote.py').read_text('utf-8')
    source = "legacy={'__name__':'backup_library'}\nexec("+repr(library)+",legacy)\n"
    source += 'PATCHES='+repr(patches)+'\nMANIFEST='+repr(manifest)+'\n'
    source += (PROJECT/'web/deploy/visibility_release.py').read_text('utf-8')
    result = subprocess.run(['ssh','-i',str(KEY),'-o','BatchMode=yes',
        '-o','StrictHostKeyChecking=yes','-o','ConnectTimeout=10',HOST,
        'sudo -n python3 - '+mode], input=source.encode('utf-8'), stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, timeout=900, **hidden_options())
    if result.returncode:
        last=result.stderr.decode('utf-8',errors='replace').strip().splitlines()
        raise RuntimeError(last[-1][:220] if last else 'Visibility release failed')
    return json.loads(result.stdout.decode('utf-8').splitlines()[-1])


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['diagnose','diagnose-business','inspect','publish','verify'])
    parser.add_argument('--authorized',action='store_true')
    args=parser.parse_args()
    if args.command=='publish' and not args.authorized:
        parser.error('Explicit website visibility change authorization required')
    with WorkerLock():
        if args.command in ('diagnose','diagnose-business'):
            result=remote(args.command)
        elif args.command=='inspect':
            result=remote('inspect')
            if MANIFEST.exists() and json.loads(MANIFEST.read_text('utf-8'))!=result:
                raise RuntimeError('Frozen visibility manifest differs; inspect original release')
            save_json(MANIFEST,result)
        else:
            manifest=json.loads(MANIFEST.read_text('utf-8'))
            expected=hashlib.sha256(json.dumps(json.loads(PATCHES.read_text('utf-8')),
                sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            if expected!=manifest['patches_sha256']:
                raise RuntimeError('Frozen visibility patches changed')
            result=remote('apply' if args.command=='publish' else 'verify',manifest)
            if args.command=='publish': save_json(RECEIPT,result)
        print(json.dumps(result,ensure_ascii=False,sort_keys=True))


if __name__=='__main__':
    main()
