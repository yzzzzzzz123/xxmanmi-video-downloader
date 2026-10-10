"""Explicit main-thread transport/acceptance of a frozen 300 round or legacy retry."""
import argparse
import json
import re
import subprocess
from pathlib import Path

from production_round import LEDGER, package, accept_receipt
from production_sync import HOST, KEY, RUNTIME, WorkerLock, hidden_options, run_command, save_json, utc_now


def publish(ledger):
    packed = package(ledger)  # Threshold, immutable bundle and all local evidence first.
    release = next(row for row in ledger['rounds'] if row['release_id'] == packed['release_id'])
    release_id = release['release_id']
    if release['state'] == 'verified':
        return {'ok': True, 'release_id': release_id, 'already_verified': True}
    if re.fullmatch(r'round-[0-9]{6}-[0-9a-f]{12}', release_id) is None:
        raise ValueError('Unsafe frozen release identifier')
    for field in ('bundle_sha256', 'manifest_sha256', 'package_manifest_sha256'):
        if re.fullmatch(r'[0-9a-f]{64}', release[field]) is None:
            raise ValueError('Invalid frozen release hash')
    common = ['-i', str(KEY), '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
              '-o', 'ConnectTimeout=10']
    release.setdefault('publish_started_at', utc_now())
    save_json(LEDGER, ledger)  # After this point retain the same manifest for every retry.
    remote = f'/tmp/manying-sync-inbox/{release_id}.tar.gz'
    run_command(['ssh', *common, HOST, 'install -d -m 0700 /tmp/manying-sync-inbox'], 30)
    run_command(['scp', '-q', *common, packed['bundle'], f'{HOST}:{remote}'], 120)
    script = Path(__file__).with_name('production_round_remote.py').read_bytes()
    command = (f'sudo -n python3 - {remote} {release["bundle_sha256"]} '
               f'{release["manifest_sha256"]} {release["package_manifest_sha256"]}')
    result = subprocess.run(['ssh', *common, HOST, command], input=script,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            timeout=600, **hidden_options())
    if result.returncode:
        # Retain bounded diagnostic type; never print raw SQL, private rows or URLs.
        text = result.stderr.decode('utf-8', errors='replace').strip().splitlines()
        safe = text[-1] if text else f'exit {result.returncode}'
        save_json(RUNTIME / f'{release_id}-failure.json',
                  {'release_id': release_id, 'returncode': result.returncode,
                   'diagnostic': safe, 'pending_preserved': True})
        raise RuntimeError('Programme release failed; frozen round retained. ' + safe)
    receipt = json.loads(result.stdout.decode('utf-8').splitlines()[-1])
    receipt_path = RUNTIME / f'{release_id}-acceptance.json'
    save_json(receipt_path, receipt)
    accept_receipt(ledger, receipt)
    save_json(LEDGER, ledger)
    return {'ok': True, 'release_id': release_id, 'programmes': len(release['programmes']),
            'resources': receipt['resource_rows'], 'status': 'production_verified',
            'receipt': str(receipt_path), 'production_written': True,
            'api_verification_scope': receipt.get('api_verification_scope')}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--authorized', action='store_true',
                        help='Explicit main-agent invocation under the programme-sync contract')
    args = parser.parse_args(argv)
    if not args.authorized:
        parser.error('Publishing requires the main agent to pass --authorized')
    with WorkerLock():
        from catalogue_align import policy_active, prepare as prepare_catalogue, publish as publish_catalogue
        if policy_active():
            prepared = prepare_catalogue()
            result = prepared if prepared.get('state') == 'already_aligned' else publish_catalogue()
            print(json.dumps(result, ensure_ascii=False, sort_keys=True))
            return
        ledger = json.loads(LEDGER.read_text(encoding='utf-8'))
        print(json.dumps(publish(ledger), ensure_ascii=False, sort_keys=True))


if __name__ == '__main__':
    main()
