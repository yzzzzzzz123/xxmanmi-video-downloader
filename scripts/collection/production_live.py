"""Event-driven catalogue deltas. No model, timer, new service or application deploy."""
import argparse
import ctypes
import json
import os
import re
import subprocess
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import psycopg
import catalogue_align as alignment
import coordctl
import collection_runtime
from production_sync import (PROJECT, RUNTIME, STATE, WorkerLock, db_options,
                             hidden_options, save_json, utc_now)

POLICY = PROJECT / '.runtime/main-agent/automation-policy.json'
MAIN = PROJECT / '.runtime/main-agent/state.json'
QUEUE = RUNTIME / 'live-events.json'
CURRENT = RUNTIME / 'catalogue-live-current.json'


def allowed():
    if not collection_runtime.production() and collection_runtime.LOCAL_RETIRED.exists():
        return False
    if not POLICY.is_file():
        return False
    policy = json.loads(POLICY.read_text('utf-8'))
    main = json.loads(MAIN.read_text('utf-8'))
    return (policy.get('enabled') is True and policy.get('sync', {}).get('enabled') is True
            and policy['sync'].get('mode') == 'event_delta' and not main.get('paused')
            and not coordctl._read_state(STATE).get('collection_paused'))


def ledger():
    return json.loads(QUEUE.read_text('utf-8')) if QUEUE.exists() else {
        'version': 1, 'revision': 0, 'pending': {}, 'worker': None,
        'verified_events': 0, 'verified_releases': 0}


def mutate(operation):
    QUEUE.parent.mkdir(parents=True, exist_ok=True)
    with coordctl.StateLock(QUEUE, timeout=5):
        state = ledger()
        result = operation(state)
        save_json(QUEUE, state)
        return result


def enqueue(program_ids, *, owner=None, batch=None):
    if collection_runtime.production():
        return {'ok':True,'queued':0,'pending':0,'mode':'production-direct'}
    ids = alignment.clean_ids(list(program_ids)) if program_ids else []
    def add(state):
        for value in ids:
            state['revision'] += 1
            state['pending'][str(value)] = {'revision': state['revision'], 'at': utc_now(),
                                          'owner': owner, 'batch': batch}
        return {'ok': True, 'queued': len(ids), 'pending': len(state['pending'])}
    return mutate(add)


def lookup_ids(urls):
    if not urls:
        return []
    options = db_options()
    options['options'] = '-c default_transaction_read_only=on -c statement_timeout=15000'
    with psycopg.connect(**options) as connection, connection.cursor() as cursor:
        cursor.execute("""SELECT p.id FROM programs p
            WHERE p.source_url=ANY(%s) AND p.captured_at IS NOT NULL
              AND EXISTS (SELECT 1 FROM resources r WHERE r.program_id=p.id
                          AND r.media_type IN ('m3u8','MP4')
                          AND r.original_url ~* '^https://[^[:space:]]+$')
            ORDER BY p.id""",
                       (list(set(urls)),))
        return [row[0] for row in cursor.fetchall()]


def mark_dirty(owner, *, batch=None, receipt=None):
    """Persist intent BEFORE local DB writes, so a killed post-commit hook is recoverable."""
    if collection_runtime.production():
        return
    if not allowed():
        return
    key = 'batch:' + batch if batch else 'search:' + str(Path(receipt).resolve())
    def mark(current):
        intents = current.setdefault('dirty', {})
        if key in intents and intents[key]['owner'] != owner:
            raise ValueError('Foreign synchronization intent')
        intents.setdefault(key, {'owner': owner, 'batch': batch,
            'receipt': str(Path(receipt).resolve()) if receipt else None,
            'token': uuid.uuid4().hex, 'at': utc_now()})
    mutate(mark)


def clear_dirty(key, token=None):
    if collection_runtime.production():
        return
    def clear(current):
        item = current.get('dirty', {}).get(key)
        if item and (token is None or item['token'] == token):
            del current['dirty'][key]
    mutate(clear)


def notify_batch(owner, batch_id, state_path=STATE):
    if collection_runtime.production():
        import server_production
        current = server_production.read(server_production.CURRENT,{})
        return {'ok':True,'mode':'production-direct','queued':0,
            'production_verified':current.get('batch')==batch_id and current.get('phase')=='verified'}
    if not allowed():
        return {'ok': True, 'enabled': False}
    state = coordctl._read_state(Path(state_path))
    batch = state.get('batches', {}).get(batch_id)
    if not batch or batch.get('thread') != owner:
        raise ValueError('Live event must belong to the real batch owner')
    accepted = [row['url'] for row in batch.get('results', [])
                if row.get('outcome') == 'captured'
                and row['url'] not in batch.get('verification', {}).get('cover_pending_items', [])]
    result = enqueue(lookup_ids(accepted) if batch.get('state') == 'acknowledged' else [],
                     owner=owner, batch=batch_id)
    if batch.get('state') == 'acknowledged':
        clear_dirty('batch:' + batch_id)
    result['executor'] = kick()
    return result


def notify_search(owner, receipt_path):
    if not allowed():
        return {'ok': True, 'enabled': False}
    record = json.loads(Path(receipt_path).read_text('utf-8'))
    if record.get('owner') != owner or record.get('phase') != 'complete':
        raise ValueError('Live search event requires this owner\'s committed receipt')
    urls = [row['url'] for row in record['search_record']['links']]
    result = enqueue(lookup_ids(urls), owner=owner)
    clear_dirty('search:' + str(Path(receipt_path).resolve()))
    result['executor'] = kick()
    return result


def reconcile_dirty():
    if collection_runtime.production():
        return {'recovered':0,'mode':'production-direct'}
    if not allowed():
        return {'recovered': 0}
    intents = dict(ledger().get('dirty', {}))
    recovered = 0
    for key, intent in list(intents.items())[:16]:
        if intent.get('batch'):
            row = coordctl._read_state(STATE).get('batches', {}).get(intent['batch'])
            if not row or row.get('thread') != intent['owner']:
                raise ValueError('Synchronization intent no longer matches its owner')
            if row.get('state') != 'acknowledged' and intent.get('reconciled_at'):
                continue
            notify_batch(intent['owner'], intent['batch'])
            def noted(current):
                item = current.get('dirty', {}).get(key)
                if item and item['token'] == intent['token']:
                    item['reconciled_at'] = utc_now()
            mutate(noted)
            recovered += 1
        elif intent.get('receipt'):
            path = Path(intent['receipt'])
            if path.is_file() and json.loads(path.read_text('utf-8')).get('phase') == 'complete':
                notify_search(intent['owner'], path)
                recovered += 1
    return {'recovered': recovered}


def process_identity(pid):
    if os.name != 'nt':
        try:
            return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[19]
        except OSError:
            return None
    from ctypes import wintypes
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel.OpenProcess(0x1000, False, int(pid))
    if not handle:
        return None
    try:
        code = wintypes.DWORD()
        if not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value != 259:
            return None
        times = [wintypes.FILETIME() for _ in range(4)]
        if not kernel.GetProcessTimes(handle, *(ctypes.byref(value) for value in times)):
            return None
        return str((times[0].dwHighDateTime << 32) | times[0].dwLowDateTime)
    finally:
        kernel.CloseHandle(handle)


def kick():
    if collection_runtime.production():
        return {'started':False,'reason':'production_direct_no_ssh_executor'}
    if not allowed():
        return {'started': False, 'reason': 'disabled_or_stopped'}
    # Collector sandboxes can enqueue committed data but cannot use the host's
    # SSH transport. The existing business-main pulse starts that executor.
    caller = os.environ.get('CODEX_THREAD_ID')
    main_thread = json.loads(MAIN.read_text('utf-8')).get('main_thread')
    if caller and caller != main_thread:
        return {'started': False, 'reason': 'queued_for_business_main'}
    def launch(state):
        worker = state.get('worker')
        if worker and worker.get('identity') and process_identity(worker['pid']) == worker['identity']:
            return {'started': False, 'reason': 'existing_executor', 'pid': worker['pid']}
        if not state['pending']:
            return {'started': False, 'reason': 'no_events'}
        retry = state.get('retry_not_before')
        if retry and datetime.fromisoformat(retry) > datetime.now(timezone.utc):
            return {'started': False, 'reason': 'bounded_backoff'}
        token = uuid.uuid4().hex
        process = subprocess.Popen(
            [sys.executable, '-X', 'utf8', '-B', str(Path(__file__).resolve()),
             'drain', '--authorized', '--executor-token', token], cwd=PROJECT,
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            **hidden_options())
        state['worker'] = {'pid': process.pid, 'identity': process_identity(process.pid),
                           'token': token, 'started_at': utc_now()}
        return {'started': True, 'pid': process.pid}
    return mutate(launch)


def drain(token=None, max_passes=4):
    if not allowed():
        return {'ok': True, 'status': 'stopped', 'production_written': False}
    completed = 0
    try:
        with WorkerLock():
            for _ in range(max_passes):
                if not allowed():
                    break
                # Serialize the first read with the launcher publishing its lease.
                state = mutate(lambda current: dict(current))
                if token and (state.get('worker') or {}).get('token') != token:
                    return {'ok': True, 'status': 'superseded_executor'}
                frozen = json.loads(CURRENT.read_text('utf-8')) if CURRENT.exists() else None
                if frozen and (frozen.get('state') != 'verified'
                        or not frozen.get('independent_verified_at')):
                    revisions = frozen['manifest']['event_revisions']
                    ids = frozen['manifest']['program_ids']
                else:
                    keys = sorted(state['pending'], key=lambda key: state['pending'][key]['revision'])[:500]
                    if not keys:
                        break
                    revisions = {key: state['pending'][key]['revision'] for key in keys}
                    ids = sorted(map(int, keys))
                if (frozen and frozen.get('state') == 'verified'
                        and not frozen.get('independent_verified_at')):
                    # Publication may have succeeded before its independent read
                    # failed. Finish that exact frozen receipt before preparing
                    # another snapshot; an already_aligned response leaves the
                    # old CURRENT untouched and would otherwise starve new events.
                    # Another authorized deployment can switch the app after the
                    # transaction receipt but before independent inspection. Use
                    # the existing immutable-payload rebind; preserve the old
                    # receipt and reapply/verify under the new app binding. Never
                    # waive the application-unchanged or user-data checks.
                    rebound = alignment.rebind_frozen_delta(frozen, CURRENT)
                    if rebound:
                        if not allowed():
                            break
                        alignment.publish(current_path=CURRENT)
                    verified = alignment.verify_frozen(CURRENT)
                    release_id = verified['release_id']
                else:
                    prepared = alignment.prepare(ids, current_path=CURRENT, event_revisions=revisions)
                    if not allowed():
                        break
                    if prepared['state'] != 'already_aligned':
                        alignment.publish(current_path=CURRENT)
                        verified = alignment.verify_frozen(CURRENT)
                        release_id = verified['release_id']
                    else:
                        release_id = None
                def accepted(current):
                    for key, version in revisions.items():
                        if current['pending'].get(key, {}).get('revision') == version:
                            del current['pending'][key]
                    current['verified_events'] += len(revisions)
                    current['verified_releases'] += bool(release_id)
                    current['last_verified_at'] = utc_now()
                    current['last_release_id'] = release_id or current.get('last_release_id')
                    current['last_verified_programmes'] = len(revisions)
                    current['error_count'] = 0
                    current.pop('last_error', None)
                    current.pop('retry_not_before', None)
                mutate(accepted)
                completed += len(revisions)
        return {'ok': True, 'status': 'drained', 'verified_events': completed,
                'pending': len(ledger()['pending'])}
    except Exception as error:
        if isinstance(error, OSError) and (getattr(error, 'errno', None) in (11, 13, 36)
                or getattr(error, 'winerror', None) in (32, 33, 36)):
            mutate(lambda current: current.update(retry_not_before=(datetime.now(timezone.utc)
                + timedelta(seconds=30)).isoformat()))
            return {'ok': True, 'status': 'writer_busy', 'pending': len(ledger()['pending'])}
        def failed(state):
            count = state.get('error_count', 0) + 1
            state['error_count'] = count
            state['last_error'] = {'at': utc_now(), 'type': type(error).__name__,
                                   'message': re.sub(r'https?://\S+', '[URL]', str(error))[:230]}
            state['retry_not_before'] = (datetime.now(timezone.utc) + timedelta(seconds=min(60 * count, 300))).isoformat()
        mutate(failed)
        return {'ok': False, 'status': 'snapshot_retained', 'error_type': type(error).__name__,
                'pending': len(ledger()['pending'])}
    finally:
        if token:
            def released(state):
                if (state.get('worker') or {}).get('token') == token:
                    state['worker'] = None
            mutate(released)
            # Close the event-arrived-while-exiting race, but honor all backoffs.
            kick()


def prime(since):
    options = db_options()
    options['options'] = '-c default_transaction_read_only=on -c statement_timeout=30000'
    with psycopg.connect(**options) as connection, connection.cursor() as cursor:
        cursor.execute('''SELECT p.id FROM programs p WHERE p.last_seen>%s OR p.captured_at>%s
            OR EXISTS(SELECT 1 FROM resources r WHERE r.program_id=p.id AND r.last_seen>%s)
            OR EXISTS(SELECT 1 FROM source_observations o WHERE o.program_id=p.id AND o.collected_at>%s)
            OR EXISTS(SELECT 1 FROM program_classifications c WHERE c.program_id=p.id AND c.curated_at>%s)
            ORDER BY p.id''', (since,) * 5)
        ids = [row[0] for row in cursor.fetchall()]
    for offset in range(0, len(ids), 500):
        enqueue(ids[offset:offset + 500])
    return {'ok': True, 'queued': len(ids), 'production_written': False}


def status():
    if collection_runtime.production():
        import server_production
        return server_production.status()
    state = ledger()
    worker = state.get('worker')
    return {'ok': True, 'enabled': allowed(), 'pending': len(state['pending']),
            'dirty_intents': len(state.get('dirty', {})),
            'executor_live': bool(worker and worker.get('identity') and process_identity(worker['pid']) == worker['identity']),
            'verified_events': state['verified_events'], 'verified_releases': state['verified_releases'],
            'last_verified_at': state.get('last_verified_at'), 'last_error': state.get('last_error')}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['status', 'kick', 'drain', 'prime'])
    parser.add_argument('--authorized', action='store_true')
    parser.add_argument('--since')
    parser.add_argument('--executor-token')
    args = parser.parse_args()
    if args.command != 'status' and not args.authorized:
        parser.error('Explicit live-sync authorization required')
    result = (status() if args.command == 'status' else kick() if args.command == 'kick'
              else prime(args.since) if args.command == 'prime' and args.since
              else drain(args.executor_token) if args.command == 'drain' else
              {'ok': False, 'error': 'prime requires --since'})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get('ok', True) else 1


if __name__ == '__main__':
    raise SystemExit(main())
