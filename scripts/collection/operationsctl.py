"""One bounded main-thread pulse: fixed recovery budget and live-sync events."""
import argparse
import json

import recoveryctl


def checkpoint(owner):
    """Canonical atomic main checkpoint; never hand-edit the large live JSON."""
    if owner != recoveryctl.policy().get('main_thread'):
        raise ValueError('Only the actual business main may checkpoint')
    import production_live
    with recoveryctl.coordctl.StateLock(recoveryctl.MAIN, timeout=5):
        main = json.loads(recoveryctl.MAIN.read_text('utf-8'))
        sync = production_live.status()
        entry = main.setdefault('current_automation', {})
        entry.update(actual_verified_event_versions=sync['verified_events'],
            actual_verified_delta_releases=sync['verified_releases'],
            last_observed_verified_at=sync['last_verified_at'],
            sync_pending=sync['pending'], recovery_check=recoveryctl.check(),
            reporting='stop_cycle_only', sync_mode='event_delta')
        main.update(hourly_report_seconds=0, detailed_report_seconds=0,
            next_report_at=None, next_detailed_report_at=None)
        recoveryctl.save_json(recoveryctl.MAIN, main)
    return {'ok': True, 'atomic_checkpoint': True, 'report': False}


def pulse(owner):
    config = recoveryctl.policy()
    if owner != config.get('main_thread'):
        raise ValueError('The business main thread owns the operations pulse')
    recovery = recoveryctl.check()
    if recovery.get('stop_required'):
        stopped = recoveryctl.stop_project(caller_thread=owner)
        return {'ok': stopped.get('ok', False), 'status': 'stopped',
                'report_cycle_once': True, 'stop': stopped}
    import production_live
    production_live.reconcile_dirty()
    executor = production_live.kick()
    sync = production_live.status()
    return {'ok': True, 'status': 'running', 'report': False,
            'incidents': recovery.get('incidents', []),
            'sync_pending': sync['pending'], 'sync_executor_live': sync['executor_live'],
            'sync_error': sync.get('last_error'), 'executor_started': executor.get('started', False)}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['pulse', 'checkpoint'])
    parser.add_argument('--thread', required=True)
    args = parser.parse_args()
    result = pulse(args.thread) if args.command == 'pulse' else checkpoint(args.thread)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
