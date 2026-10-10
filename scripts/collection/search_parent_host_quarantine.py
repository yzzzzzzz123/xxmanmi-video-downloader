"""Validate the real parent host for a stranded, settled prepared search.

Only the existing manual-review quarantine may use this proof. It authorizes
neither search success nor replay. Retired-browser proofs remain dormant, not released.
"""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

SELECTOR_ERROR = 'Public input value and selected channel must exactly match the claimed search'
READONLY_NON_SEARCH_INPUT_MODE = 'readonly_non_search_page_after_unsubmitted_kernel_reset'


def validate_unsubmitted_input(evidence, owner, work, *, project_root):
    """Verify a source-audited, terminal pre-submit timeout, not a search outcome."""
    path = Path(project_root) / '.runtime/main-agent/state.json'
    raw = path.read_bytes()
    config = json.loads(raw)
    auth = config.get('fault_repair_authorization', {})
    slot = next((v for v in config.get('slots', {}).values() if v.get('owner') == owner), None)
    host, tab = evidence.get('browser_host'), evidence.get('browser_tab')
    if (config.get('paused') is True or auth.get('authorized') is not True
            or auth.get('basis') != 'direct_user'
            or auth.get('scope') != 'existing_workflow_fault_source_and_tests'
            or not slot or Path(slot.get('work', '')).resolve() != Path(work).resolve()
            or not isinstance(host, dict) or not config.get('host_parent_thread')
            or host.get('session_id') != config['host_parent_thread']
            or host.get('session_id') == owner or host.get('thread_id') != owner
            or host.get('terminated') is not False or host.get('tools_terminal') is not True
            or host.get('session_reset') is not True
            or evidence.get('main_authorized') is not True
            or evidence.get('browser_action_inflight') is not False):
        raise ValueError('Parent input recovery requires the authorized exact live owner binding')
    inventory = tab.get('inventory_tab_ids') if isinstance(tab, dict) else None
    missing = isinstance(tab, dict) and tab.get('exists') is False
    non_search_page = evidence.get('recovery_mode') == READONLY_NON_SEARCH_INPUT_MODE
    if (not isinstance(tab, dict) or type(tab.get('exists')) is not bool
            or tab.get('readable') is not (True if non_search_page else False)
            or (non_search_page and missing)
            or not isinstance(tab.get('tab_id'), str) or not tab['tab_id']
            or tab.get('session_id') != host['session_id']
            or tab.get('owner_thread_id') != owner
            or not isinstance(inventory, list) or any(not isinstance(v, str) for v in inventory)
            or (tab['tab_id'] in inventory if missing else tab['tab_id'] not in inventory)
            or (not non_search_page and evidence.get('dom_summary') is not None)):
        raise ValueError('Parent input recovery requires the actual retained tab inventory, never fabricated DOM')
    missing_errors = {f'No tab with id: {tab["tab_id"]}.', f'Error: No tab with id: {tab["tab_id"]}.'}
    if missing and tab.get('missing_error') not in missing_errors:
        raise ValueError('Missing pre-submit tab requires its exact current read error, not inventory alone')
    matches = [v for v in config.get('unsubmitted_input_recoveries', [])
               if all(v.get(k) == evidence.get(k) for k in ('owner', 'task_id', 'query', 'channel'))]
    if len(matches) != 1:
        raise ValueError('One main-verified original pre-submit source record required')
    proof = matches[0]
    if (proof.get('proof_verified') is not True or proof.get('source_verified_no_submit') is not True
            or proof.get('all_tools_terminal') is not True or proof.get('runtime_reset_verified') is not True
            or proof.get('tab_id') != tab['tab_id']):
        raise ValueError('Original source must prove no submit and the terminal runtime reset')
    steps = evidence.get('terminal_actions')
    expected_ids = proof.get('terminal_tool_ids')
    if (not isinstance(expected_ids, list) or len(expected_ids) != 3
            or any(not isinstance(v, str) or not v for v in expected_ids)
            or len(set(expected_ids)) != 3 or not isinstance(steps, list) or len(steps) != 3
            or any(not isinstance(v, dict) or v.get('tool_id') != expected_ids[i]
                   or type(v.get('sequence')) is not int or v['sequence'] != i + 1
                   or v.get('tool_terminal') is not True or not isinstance(v.get('operation'), str)
                   or not v['operation'].strip() for i, v in enumerate(steps))
            or proof.get('reset_error') != 'js execution timed out; kernel reset, rerun your request'):
        raise ValueError('Three actual terminal tools and the original kernel-reset receipt required')
    if missing:
        # A confirmed missing tab is not an unreadable retained tab. Only the
        # main-audited zero-submit reset may use this manual, non-replay branch.
        if (proof.get('recovery_mode') != 'missing_tab_after_unsubmitted_kernel_reset'
                or proof.get('missing_error') != tab.get('missing_error')
                or proof.get('inventory_tab_ids') != inventory
                or [v['operation'] for v in steps] != [
                    'unsubmitted_fill_prepare', 'readonly_tab_inventory', 'readonly_original_tab_probe']
                or steps[0].get('status') != 'failed'
                or steps[0].get('error') != proof['reset_error']
                or any(v.get('status') != 'completed' for v in steps[1:])
                or steps[1].get('inventory_tab_ids') != inventory
                or steps[2].get('error') != tab['missing_error']):
            raise ValueError('Missing input tab requires main-verified reset, inventory and exact read receipts')
    elif non_search_page:
        dom = evidence.get('dom_summary')
        loading = dom.get('loading_indicator') if isinstance(dom, dict) else None
        parsed = urlsplit(dom.get('page_url') or '') if isinstance(dom, dict) else None
        if (proof.get('recovery_mode') != READONLY_NON_SEARCH_INPUT_MODE
                or proof.get('source_verified_only_fill') is not True
                or proof.get('inventory_tab_ids') != inventory
                or proof.get('claim_sha256') != evidence.get('claim_sha256')
                or not re.fullmatch(r'[0-9a-f]{64}', proof.get('taskbook_sha256') or '')
                or proof.get('taskbook_sha256') != evidence.get('taskbook_sha256')
                or [v['operation'] for v in steps] != [
                    'unsubmitted_input_fill', 'readonly_tab_inventory', 'readonly_non_search_page']
                or steps[0].get('status') != 'failed'
                or steps[0].get('error') != proof['reset_error']
                or any(v.get('status') != 'completed' or v.get('error') is not None
                       for v in steps[1:])
                or steps[1].get('inventory_tab_ids') != inventory
                or not isinstance(dom, dict) or dom != proof.get('readonly_dom')
                or steps[2].get('dom_summary') != dom
                or not isinstance(dom.get('observed_at'), str) or not dom['observed_at'].strip()
                or dom.get('input_value_after_action') is not None
                or dom.get('selected_channels') != []
                or dom.get('result_container_visible') is not False
                or type(dom.get('result_count')) is not int or dom['result_count'] != 0
                or dom.get('visible_detail_links') != []
                or dom.get('pagination_pending') is not False
                or dom.get('empty_result_visible') is not False
                or not isinstance(loading, dict)
                or type(loading.get('visible_count')) is not int or loading['visible_count'] != 0
                or loading.get('visible_texts') != []
                or not parsed or parsed.scheme not in {'http', 'https'}
                or parsed.username or parsed.password
                or not parsed.hostname or not (parsed.hostname.lower() == 'xxmanmi.com'
                    or parsed.hostname.lower().endswith('.xxmanmi.com'))
                or not re.fullmatch(r'/video[0-9]+/[^/]+\.html', parsed.path)
                or tab.get('url') != dom['page_url'] or proof.get('tab_url') != dom['page_url']):
            raise ValueError('Readable non-search input recovery requires the exact source-audited no-submit detail-page receipts')
    elif (any(v.get('status') != 'failed' or v.get('error') != proof['reset_error']
              for v in steps[:2]) or steps[2].get('status') != 'completed'):
        raise ValueError('Actual fill, bounded readonly failure and inventory tool receipts required')
    action = evidence.get('search_action', {})
    if (evidence.get('reason') != 'abandoned_uninspectable_input_action'
            or not isinstance(action, dict)
            or any(type(action.get(k)) is not int or action[k] != v
                   for k, v in (('action_attempts', 1), ('input_fill_attempts', 1), ('submit_attempts', 0)))
            or action.get('action_id') is not None or action.get('input_result') != 'unknown'
            or action.get('prepare_result') != ('not_attempted' if non_search_page else 'unknown')):
        raise ValueError('Parent input recovery cannot hide a click, known action or known preparation')
    audit = {
        'parent_input_authorized': True, 'parent_config_sha256': hashlib.sha256(raw).hexdigest(),
        'browser_host_session_id': host['session_id'], 'browser_host_thread_id': owner,
        'browser_tab_id': tab['tab_id'], 'browser_tab_inventory_ids': inventory,
        'terminal_actions': steps, 'input_result': 'unknown',
        'prepare_result': 'not_attempted' if non_search_page else 'unknown',
        'manual_review_required': True, 'search_success': False,
        'fault_repair_authorization_basis': 'direct_user',
    }
    if non_search_page:
        audit.update({'browser_tab_exists': True, 'browser_tab_readable': True,
                      'browser_tab_url': dom['page_url'], 'readonly_dom_summary': dom,
                      'recovery_mode': READONLY_NON_SEARCH_INPUT_MODE,
                      'browser_host_session_reset': True, 'source_verified_no_submit': True,
                      'source_verified_only_fill': True})
    elif missing:
        audit.update({'browser_tab_exists': False, 'browser_tab_missing_error': tab['missing_error'],
                      'recovery_mode': 'missing_tab_after_unsubmitted_kernel_reset',
                      'browser_host_session_reset': True, 'source_verified_no_submit': True})
    return path, hashlib.sha256(raw).hexdigest(), audit


def validate(evidence, owner, work, query, channel, *, project_root, retained_action=False):
    config_path = Path(project_root) / ('.runtime/main-agent/state.json' if retained_action
                                       else '.runtime/supervisor/config.json')
    raw = config_path.read_bytes()
    config = json.loads(raw)
    if retained_action:
        authorization = config.get('fault_repair_authorization', {})
        if (config.get('paused') is True or authorization.get('authorized') is not True
                or authorization.get('basis') != 'direct_user'
                or authorization.get('scope') != 'existing_workflow_fault_source_and_tests'):
            raise ValueError('Retained-action recovery requires direct-user fault-repair authorization')
    parent = config.get('host_parent_thread') if retained_action else config.get('main_thread')
    host, tab, action, dom = (evidence.get(k) for k in
                             ('browser_host', 'browser_tab', 'search_action', 'dom_summary'))
    slot = next((v for v in config.get('slots', {}).values() if v.get('owner') == owner), None)
    if (not isinstance(parent, str) or not parent.strip()
            or not slot or Path(slot.get('work', '')).resolve() != Path(work).resolve()
            or parent == owner
            or not isinstance(host, dict)
            or host.get('session_id') != parent
            or host.get('thread_id') != owner
            or host.get('terminated') is not False or host.get('tools_terminal') is not True):
        raise ValueError('Parent host must match the configured real main session and exact owner slot')
    if (not isinstance(tab, dict) or type(tab.get('exists')) is not bool
            or not isinstance(tab.get('tab_id'), str)
            or not tab['tab_id'] or tab.get('session_id') != host['session_id']
            or tab.get('owner_thread_id') != owner):
        raise ValueError('Parent-host quarantine requires the exact original owner tab identity')
    if retained_action and evidence.get('recovery_mode') == 'retired_browser_retained_action':
        audit = _validate_retired_browser(evidence, config, owner, query, channel, host, tab, action, dom)
        audit['parent_config_sha256'] = hashlib.sha256(raw).hexdigest()
        return config_path, hashlib.sha256(raw).hexdigest(), audit
    missing = tab['exists'] is False
    if missing:
        errors = {f'No tab with id: {tab["tab_id"]}.', f'Error: No tab with id: {tab["tab_id"]}.'}
        inventory = tab.get('inventory_tab_ids')
        # Current Chrome tabs.get reports a different exact absence message.
        # Accept it only in authorized retained-action recovery with an empty
        # independently observed inventory; keep all other proof gates intact.
        if retained_action and inventory == []:
            message = f'Tab not found: {tab["tab_id"]}. Existing tabs: none'
            errors.update({message, f'Error: {message}'})
        if (tab.get('readable') is not False or tab.get('missing_error') not in errors
                or not isinstance(inventory, list) or any(not isinstance(v, str) for v in inventory)
                or tab['tab_id'] in inventory):
            raise ValueError('Missing parent-host tab requires its exact error and actual confirming inventory')
        if dom is not None:
            raise ValueError('Do not label historical DOM as current when the original tab is missing')
    elif tab.get('readable') is not True:
        raise ValueError('Parent-host quarantine requires the readable original owner tab')
    if (not isinstance(action, dict)
            or not isinstance(action.get('action_id'), str)
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}', action['action_id'])
            or action.get('execute_next_action') != 'finalize_search_action'
            or action.get('execute_returned') is not True
            or type(action.get('action_attempts')) is not int or action['action_attempts'] != 1
            or action.get('finalize_returned') is not True
            or type(action.get('finalize_attempts')) is not int
            or (action['finalize_attempts'] < 1 if retained_action else action['finalize_attempts'] != 1)
            or action.get('original_module_instance_available') is not True
            or (not retained_action and action.get('private_action_record_exportable') is not False)
            or (not retained_action and action.get('result_error') != SELECTOR_ERROR)
            or not isinstance(action.get('result_error'), str) or not action['result_error'].strip()
            or evidence.get('browser_action_inflight') is not False
            or evidence.get('main_authorized') is not True):
        raise ValueError('Parent-host quarantine requires the exact returned prepared action and selector gate failure')
    loading = dom.get('loading_indicator') if isinstance(dom, dict) else None
    loading_settled = loading is None or (
        isinstance(loading, dict)
        and type(loading.get('visible_count', 0)) is int
        and loading.get('visible_count', 0) == 0
    )
    # A retained, settled failed search can outlive navigation to a detail page.
    # Preserve the real null input; it is not a matching search or an empty result.
    history = evidence.get('historical_readonly_observation')
    retained_detail_dom = bool(
        retained_action and not missing and isinstance(dom, dict)
        and action.get('result_error') == 'Zero results require a visible empty-result indicator'
        and 'input_value_after_action' in dom and dom['input_value_after_action'] is None
        and dom.get('selected_channels') == []
        and dom.get('result_container_visible') is False
        and type(dom.get('result_count')) is int and dom['result_count'] == 0
        and dom.get('visible_detail_links') == []
        and dom.get('empty_result_visible') is False
        and dom.get('pagination_pending') is False and loading_settled
        and dom.get('page_url') == tab.get('url')
        and re.fullmatch(r'/video[0-9]+/[0-9]+\.html', urlsplit(dom.get('page_url') or '').path)
        and isinstance(history, dict)
        and history.get('input_value_after_action') == query
        and history.get('selected_channels') == [channel]
        and history.get('result_container_visible') is True
        and type(history.get('result_count')) is int and history['result_count'] == 0
        and history.get('visible_detail_links') == []
        and history.get('empty_result_visible') is False
        and history.get('pagination_pending') is False
        and isinstance(history.get('observed_at'), str) and history['observed_at'].strip()
        and isinstance(dom.get('observed_at'), str)
        and history['observed_at'] < dom['observed_at']
        and urlsplit(history.get('page_url') or '').netloc == urlsplit(dom.get('page_url') or '').netloc
    )
    if not missing and (not isinstance(dom, dict)
            or (not isinstance(dom.get('input_value_after_action'), str) and not retained_detail_dom)
            or not isinstance(dom.get('selected_channels'), list)
            or any(not isinstance(v, str) for v in dom['selected_channels'])
            or (not retained_action and dom.get('input_value_after_action') != query)
            or (not retained_action and dom.get('selected_channels') != [channel])
            or type(dom.get('result_count')) is not int or not 0 <= dom['result_count'] <= 200
            or not isinstance(dom.get('visible_detail_links'), list)
            or dom['result_count'] != len(dom['visible_detail_links'])
            or type(dom.get('result_container_visible')) is not bool
            or (not retained_action and dom.get('result_container_visible') is not True)
            or dom.get('pagination_pending') is not False
            or type(dom.get('empty_result_visible')) is not bool
            or not loading_settled):
        raise ValueError('Parent-host quarantine requires an actual complete read-only query/channel DOM snapshot')
    if not missing and (not isinstance(dom.get('observed_at'), str) or not dom['observed_at'].strip()):
        raise ValueError('Actual read-only observation timestamp required')
    for page_url in (() if missing else (dom.get('page_url'), tab.get('url'))):
        parsed = urlsplit(page_url or '')
        if (parsed.scheme not in {'http', 'https'} or parsed.username or parsed.password
                or not parsed.hostname or not (parsed.hostname.lower() == 'xxmanmi.com'
                    or parsed.hostname.lower().endswith('.xxmanmi.com'))):
            raise ValueError('Actual original public xxmanmi page address required')
    return config_path, hashlib.sha256(raw).hexdigest(), {
        'parent_host_authorized': True,
        'parent_config_sha256': hashlib.sha256(raw).hexdigest(),
        'browser_host_session_id': host['session_id'],
        'browser_host_thread_id': owner,
        'browser_tab_id': tab['tab_id'],
        'browser_tab_exists': not missing,
        'browser_tab_missing_error': tab.get('missing_error') if missing else None,
        'browser_tab_inventory_ids': tab.get('inventory_tab_ids') if missing else None,
        'action_id': action['action_id'],
        'action_attempts': 1,
        'execute_returned': True,
        'finalize_returned': True,
        'finalize_attempts': action['finalize_attempts'],
        'execute_next_action': action['execute_next_action'],
        'finalization_gate_error': action['result_error'],
        'authorized_retained_action': retained_action,
        'current_detail_page_after_failed_search': retained_detail_dom,
        'fault_repair_authorization_basis': 'direct_user' if retained_action else None,
        'dom_summary': dom,
        'historical_readonly_observation': history if missing or retained_detail_dom else None,
        'manual_review_required': True,
        'search_success': False,
    }


def _validate_retired_browser(evidence, config, owner, query, channel, host, tab, action, dom):
    """Isolate a settled failed/unknown action; do not release its live proof.

    Only a main-source-verified Chrome process gap and exact original receipts
    may use this branch. Empty inventories or module loss alone are insufficient.
    """
    proofs = [v for v in config.get('retained_retired_browser_actions', [])
              if isinstance(v, dict) and all(v.get(k) == evidence.get(k)
                 for k in ('owner', 'task_id', 'query', 'channel'))]
    if len(proofs) != 1:
        raise ValueError('One main-verified retired-browser action proof required')
    proof = proofs[0]
    expected = proof.get('action_contract')
    steps = proof.get('terminal_actions')
    inventory = tab.get('inventory_tab_ids')
    if (any(proof.get(k) is not True for k in (
                'proof_verified', 'all_tools_terminal', 'original_module_available_verified',
                'chrome_process_gap_verified', 'original_browser_unavailable_verified'))
            or proof.get('tab_id') != tab['tab_id']
            or not isinstance(expected, dict) or not isinstance(action, dict)
            or any(action.get(k) != value for k, value in expected.items())
            or expected.get('state') != 'settled_unverifiable_retired_browser'
            or expected.get('original_action_state') not in ('action_failed', 'action_unknown')
            or not re.fullmatch(r'[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}',
                                str(expected.get('action_id', '')))
            or type(action.get('action_attempts')) is not int or action['action_attempts'] != 1
            or action.get('original_module_instance_available') is not True
            or action.get('public_ui_completed') is not False
            or action.get('replay_authorized') is not False
            or evidence.get('browser_action_inflight') is not False
            or evidence.get('main_authorized') is not True
            or tab.get('exists') is not False or tab.get('readable') is not False
            or not isinstance(inventory, list) or any(not isinstance(v, str) for v in inventory)
            or inventory != proof.get('inventory_tab_ids') or tab['tab_id'] in inventory
            or dom is not None
            or not isinstance(steps, list) or len(steps) < 2
            or any(not isinstance(v, dict) or v.get('tool_terminal') is not True
                   or v.get('status') not in ('completed', 'failed')
                   or not isinstance(v.get('tool_id'), str) or not v['tool_id']
                   for v in steps)
            or len({v['tool_id'] for v in steps}) != len(steps)
            or evidence.get('terminal_actions') != steps
            or proof.get('chrome_absent_diagnostic_exit') != 1
            or proof.get('chrome_reopened_running_check_exit') != 0
            or not proof.get('chrome_absent_diagnostic_tool')
            or not proof.get('chrome_reopened_running_check_source')
            or not proof.get('reopened_browser_inventory_tool')
            or not isinstance(proof.get('original_browser_unavailable_error'), str)
            or 'Browser is not available:' not in proof['original_browser_unavailable_error']
            or host.get('original_browser_unavailable_error') != proof['original_browser_unavailable_error']):
        raise ValueError('Exact settled action, verified Chrome process gap and absent old tab required')
    if expected['original_action_state'] == 'action_failed':
        if (action.get('execute_returned') is not False or action.get('execute_next_action') is not None
                or action.get('finalize_returned') is not False
                or type(action.get('finalize_attempts')) is not int or action['finalize_attempts'] != 0
                or action.get('execute_error') != proof['original_browser_unavailable_error']):
            raise ValueError('Failed retired-browser execute must not be promoted to a returned click')
    elif (action.get('execute_returned') is not True
            or action.get('execute_next_action') != 'inspect_search_action'
            or action.get('execute_reason') != 'click_timeout'
            or action.get('execute_error') is not None
            or action.get('finalize_returned') is not True
            or type(action.get('finalize_attempts')) is not int or action['finalize_attempts'] < 1):
        raise ValueError('Unknown retired-browser action must retain its actual timeout receipts')
    return {
        'parent_host_authorized': True, 'authorized_retained_action': True,
        'retired_browser_retained_action': True, 'fault_repair_authorization_basis': 'direct_user',
        'browser_host_session_id': host['session_id'], 'browser_host_thread_id': owner,
        'browser_tab_id': tab['tab_id'], 'browser_tab_exists': False,
        'browser_tab_inventory_ids': inventory, 'dom_summary': None,
        'action_id': action['action_id'], 'action_state': action['state'],
        'original_action_state': action['original_action_state'], 'action_attempts': 1,
        'execute_returned': action['execute_returned'], 'execute_next_action': action['execute_next_action'],
        'execute_error': action.get('execute_error'), 'execute_reason': action.get('execute_reason'),
        'finalize_returned': action['finalize_returned'], 'finalize_attempts': action['finalize_attempts'],
        'finalization_gate_error': action['result_error'], 'terminal_actions': steps,
        'chrome_absent_diagnostic_tool': proof['chrome_absent_diagnostic_tool'],
        'chrome_absent_diagnostic_exit': 1,
        'chrome_reopened_running_check_source': proof['chrome_reopened_running_check_source'],
        'chrome_reopened_running_check_exit': 0,
        'reopened_browser_inventory_tool': proof['reopened_browser_inventory_tool'],
        'original_browser_unavailable_error': proof['original_browser_unavailable_error'],
        'chrome_process_gap_verified': True, 'public_ui_completed': False,
        'manual_review_required': True, 'search_success': False, 'released': False,
        'retained_capability_status': 'quarantined_dormant_not_released',
    }
