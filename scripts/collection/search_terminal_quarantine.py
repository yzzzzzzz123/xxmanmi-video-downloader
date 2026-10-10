"""Conservative main-authorized isolation of settled searches with no usable proof.

This creates only a terminal manual-review record. It never commits search results,
queues observed links, creates browser action capabilities, or authorizes replay.
"""
import hashlib
import json
from pathlib import Path
import time

REASON = 'quarantined_terminal_search_without_evidence'


def quarantine(state_path, owner, query, channel, evidence_path, logs_stopped,
               authorized, *, discovery_root, taskbooks, workflow):
    if not authorized or not logs_stopped:
        raise ValueError('Terminal search quarantine requires main authorization and stopped logs')
    if json.loads(Path(state_path).read_bytes()).get('collection_paused') is True:
        raise ValueError('Collection paused; preserve the search checkpoint')
    record, work = workflow.owner_record(state_path, owner)
    if record.get('active_batch') is not None:
        raise ValueError('Owner has an active detail batch')
    evidence_path = Path(evidence_path)
    if evidence_path.is_symlink() or not evidence_path.resolve().is_relative_to(work.resolve()):
        raise ValueError('Quarantine evidence must be inside the original owner work')
    if not evidence_path.is_file() or evidence_path.stat().st_size > 1024 * 1024:
        raise ValueError('Invalid or oversized quarantine evidence')
    raw = evidence_path.read_bytes()
    evidence = json.loads(raw.decode('utf-8-sig'))
    current = workflow.shared_discovery.inspect_claim(discovery_root, query, channel)
    claim = current.get('claim') or {}
    task_id, claim_hash = claim.get('task_id'), current.get('claim_sha256')
    expected = {'version': 1, 'reason': REASON, 'owner': owner, 'task_id': task_id,
                'query': query, 'channel': channel, 'claim_sha256': claim_hash,
                'logs_stopped': True, 'main_authorized': True}
    if not isinstance(evidence, dict) or any(evidence.get(k) != v for k, v in expected.items()):
        raise ValueError('Quarantine does not match the effective claim/task/hash')
    if claim.get('owner') != owner or current.get('reason') not in ('claimed', REASON):
        raise ValueError('Search claim is not owned by the exact target')
    book = json.loads(workflow._taskbook_path(taskbooks, task_id).read_bytes())
    if (book.get('kind') != 'search' or book.get('owner') != owner
            or Path(book.get('work', '')).resolve() != work.resolve()
            or book.get('scope', {}).get('queries') != [query]
            or book.get('scope', {}).get('channels') != [channel]):
        raise ValueError('Search taskbook does not match the exact target')
    host, tab = evidence.get('browser_host'), evidence.get('browser_tab')
    if (not isinstance(host, dict) or host.get('thread_id') != owner
            or host.get('terminated') is not False or host.get('tools_terminal') is not True
            or not isinstance(tab, dict)):
        raise ValueError('Actual owner thread, live host and settled tools are required')
    for group in (host, tab):
        for key in ('checked_at', 'method'):
            workflow._nonempty_text(group.get(key), key)
    workflow._nonempty_text(evidence.get('summary'), 'summary')
    local = evidence.get('local_state')
    if not isinstance(local, dict) or any(local.get(key) is not False for key in
            ('browser_evidence_exists', 'commit_receipt_exists', 'search_journal_contains_task')):
        raise ValueError('Existing search results must use their official recovery path')
    audit = {key: evidence.get(key) for key in
             ('terminal_actions', 'terminal_action_count', 'original_action_proof_available',
              'browser_action_inflight', 'dom_summary', 'journals_before', 'journals_after')}
    audit.update(reason=REASON, main_authorized=True, action_state='all_tool_calls_terminal',
                 browser_host_terminated=False, browser_host_thread_id=host['thread_id'],
                 browser_host_session_id=host.get('session_id'), browser_tab_exists=tab.get('exists'),
                 browser_tab_id=tab.get('tab_id'), inventory_tab_ids=tab.get('inventory_tab_ids'),
                 manual_review_required=True, replay_authorized=False, logs_stopped=True,
                 browser_evidence_exists=False, commit_receipt_exists=False,
                 search_journal_contains_task=False, capture_lock_exists=False,
                 summary=evidence['summary'])
    if not workflow.shared_discovery.terminal_unrecorded_search_audit(
            {**audit, 'owner': owner, 'query': query, 'channel': channel}):
        raise ValueError('Action outcomes, tab state or stopped-log proof are incomplete')
    with workflow.work_lock(work):
        if any((work / name).exists() for name in ('.capture-helper.lock', '.capture.lock')):
            raise RuntimeError('Capture writer may still be active')
        if workflow.owner_record(state_path, owner)[0].get('active_batch') is not None:
            raise RuntimeError('Owner acquired an active detail batch')
        if any((work / name).exists() for name in
               (f'{task_id}-evidence.json', f'search-commit-{task_id}.json')):
            raise RuntimeError('Saved evidence or commit exists; do not abandon it')
        journals = workflow._journal_snapshot(work)
        if evidence['journals_before'] != journals:
            raise ValueError('Stopped-log snapshot differs from actual worker logs')
        journal_path = work / 'searches.ndjson'
        if journal_path.exists() and any(json.loads(line).get('task_id') == task_id
                for line in journal_path.read_text('utf-8').splitlines() if line.strip()):
            raise RuntimeError('Search journal already contains this task')
        time.sleep(0.2)
        if workflow._journal_snapshot(work) != journals or evidence_path.read_bytes() != raw:
            raise RuntimeError('Evidence or worker logs changed during stopped-write validation')
        receipt = workflow.shared_discovery.abandon_search(
            discovery_root, owner, task_id, query, channel, claim_hash,
            hashlib.sha256(raw).hexdigest(), audit)
    return {'ok': True, 'command': 'quarantine-search-terminal', 'status': receipt['reason'],
            'task_id': task_id, 'query': query, 'channel': channel,
            'abandonment_id': receipt['abandonment_id'], 'ledger': receipt['ledger'],
            'manual_review_required': True, 'replay_authorized': False,
            'search_success': False, 'next_action': 'claim_other_search'}
