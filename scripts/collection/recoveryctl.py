"""Fixed recovery routing and a durable one-hour all-project stop budget."""
import argparse
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import coordctl

# Stop/adopt/resume are state-only recovery operations. They must work even when
# a database client cannot import; collection/sync keep their existing gates.
PROJECT = Path(__file__).resolve().parents[2]
STATE = PROJECT / 'Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json'
save_json = coordctl._atomic_write
utc_now = coordctl.utc_now

MAIN = PROJECT / '.runtime/main-agent/state.json'
POLICY = PROJECT / '.runtime/main-agent/automation-policy.json'
RECOVERY = PROJECT / '.runtime/main-agent/recovery-current.json'

# Templates select existing gates. They never invent evidence or replay actions.
TEMPLATES = [
    ('user_pause', r'collection paused by user', 'stop_new_work', None, False),
    ('search_claim_lock_blocked', r'search claim lock is busy', 'inspect_exact_lock_owner_then_one_bounded_retry_no_unlock', 'references/search-fault-recovery.md', True),
    ('shared_writer_wait', r'(state lock is busy|writer.*busy|database.*lock)', 'wait_verified_existing_writer', None, False),
    ('page_without_resources', r'DOM episode count is empty|暂无资源|获取失败', 'bounded_page_failure_then_continue', 'references/detail-workflow.md', False),
    ('chrome_unavailable', r'Browser is not available|No browser.*available', 'documented_family_reconnect_preserve_action', 'references/search-fault-recovery.md', True),
    ('search_preparation_ui_mismatch', r'Search preparation does not match one complete claimed public UI state', 'retain_unexecuted_claim_restore_observed_search_view_then_one_prepare', 'references/recovery-templates.md', True),
    ('search_no_visible_empty', r'Zero results require a visible empty-result indicator', 'retain_original_action_read_current_dom_then_exact_quarantine_gate', 'references/search-fault-recovery.md', True),
    ('retained_detail_dom_gate', r'Parent-host quarantine requires an actual complete read-only query/channel DOM snapshot', 'verify_real_current_detail_and_matching_historical_dom_then_one_formal_retry', 'references/search-fault-recovery.md', True),
    ('prepared_tab_object_mismatch', r'Only a settled prepared action on its original tab can be terminally released', 'recover_exact_original_prepare_tab_reference_no_new_wrapper', 'references/recovery-templates.md', True),
    ('terminal_journal_key_missing', r'host-mismatch journal snapshot does not match worker logs', 'compare_original_snapshots_then_formal_repair_terminal_journals', 'references/recovery-templates.md', True),
    ('owned_tab_missing', r'No tab with id:|Tab not found:|not part of (?:the current )?browser session', 'restore_detail_tab_or_authorized_retained_action_gate', 'references/search-fault-recovery.md', True),
    ('kernel_reset', r'kernel reset|execution timed out|execution interrupted', 'reconcile_original_handle_and_nonce_no_replay', 'references/terminal-search-recovery.md', True),
    ('storage_quota', r'QuotaExceededError', 'owned_site_storage_bounded_recovery', 'references/browser-storage-recovery.md', True),
    ('central_cover_gate', r'cover gate selected|source_unavailable|duplicate_rejected|quality_rejected', 'central_audit_then_verified_relay_repair', 'references/cover-recovery.md', True),
    ('search_evidence_gate', r'Public input value|query.*channel|abandonment audit|search.*evidence|host-mismatch evidence|finaliz', 'exact_known_gate_only_keep_action', 'references/search-fault-recovery.md', True),
    ('public_navigation_connection_closed', r'net::ERR_CONNECTION_CLOSED', 'read_owned_dom_then_one_observed_navigation', 'references/recovery-templates.md', True),
    ('database_unavailable', r'Connection.*refused|OperationalError|database.*unavailable|connection.*closed', 'existing_database_lifecycle_no_reinstall', None, True),
    ('identity_or_audit_conflict', r'identity conflict|audit.*verify|hash.*differ|verification.*conflict', 'retain_evidence_no_overwrite_main_repair', None, True),
]


def policy():
    return json.loads(POLICY.read_text('utf-8')) if POLICY.is_file() else {'enabled': False}


def state():
    return json.loads(RECOVERY.read_text('utf-8')) if RECOVERY.is_file() else {
        'version': 1, 'cycle_started_at': utc_now(), 'incidents': {}, 'stopped': False}


def mutate(operation):
    with coordctl.StateLock(RECOVERY, timeout=5):
        current = state()
        result = operation(current)
        save_json(RECOVERY, current)
        return result


def slot_for(owner):
    main = json.loads(MAIN.read_text('utf-8'))
    matches = [(slot, entry) for slot, entry in main.get('slots', {}).items()
               if entry.get('owner') == owner and slot in ('01', '02', '03', '04')]
    if len(matches) != 1:
        raise ValueError('Recovery owner must be one current, real collection slot')
    return matches[0]


def classify(error):
    for name, pattern, action, reference, incident in TEMPLATES:
        if re.search(pattern, error, re.I):
            return {'template': name, 'action': action, 'reference': reference,
                    'incident': incident, 'replay_authorized': False}
    return {'template': 'unknown', 'action': 'retain_checkpoint_main_diagnosis',
            'reference': None, 'incident': True, 'replay_authorized': False}


def record(owner, error, *, command=None, batch=None, tool_id=None):
    if policy().get('enabled') is not True:
        return {'ok': True, 'enabled': False}
    slot, binding = slot_for(owner)
    route = classify(error)
    if not route['incident']:
        return {'ok': True, **route}
    def note(current):
        if current['stopped']:
            return {'ok': False, 'status': 'stopped'}
        incident = current['incidents'].get(slot)
        if not incident or incident.get('resolved_at'):
            at = datetime.now(timezone.utc)
            incident = {'slot': slot, 'owner': owner, 'first_failed_at': at.isoformat(),
                        'deadline': (at + timedelta(seconds=3600)).isoformat(),
                        'work': binding['work'], 'attempts': 0}
            current['incidents'][slot] = incident
        # New attempts, new native turns or replacement UUIDs do not reset time.
        gesture_error = 'Timed out after 10000ms waiting for CDP command Input.synthesizeScrollGesture.'
        gesture_sha = hashlib.sha256(gesture_error.encode()).hexdigest()
        if error == gesture_error or incident.get('error_sha256') == gesture_sha:
            incident['gesture_outcome'] = 'unknown'
        incident.update(last_seen_at=utc_now(), template=route['template'],
                        error_sha256=hashlib.sha256(error.encode()).hexdigest(),
                        command=command, batch=batch, tool_id=tool_id)
        incident['attempts'] += 1
        return {'ok': True, **route, 'slot': slot, 'deadline': incident['deadline'],
                'attempts': incident['attempts']}
    return mutate(note)


def _native_navigation_budget_start(owner, binding, incident, source_tool, cycle_started_at):
    """Prove the earlier failed navigation in this still-unresolved fault chain."""
    from app_server_control import RPC

    # Other fault families need their own recovery-boundary proof before inclusion.
    if (incident.get('template') != 'public_navigation_connection_closed'
            or incident.get('batch') or incident.get('command')):
        raise ValueError('Budget correction only supports the verified public navigation fault')
    with RPC() as rpc:
        native = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
    if (native.get('id') != owner or native.get('parentThreadId') != binding.get('host_parent')
            or not isinstance(native.get('cwd'), str) or not native['cwd']
            or Path(native['cwd']).resolve() not in (PROJECT.resolve(), PROJECT.parent.resolve())):
        raise ValueError('Native failure owner/parent/cwd differs from the registered slot')
    # The app-server returns Windows extended-length paths for this same local file.
    path = Path(native['path'].removeprefix('\\\\?\\')).resolve()
    sessions = (Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions').resolve()
    if (not path.is_relative_to(sessions) or not path.name.endswith('-' + owner + '.jsonl')):
        raise ValueError('Native receipt path is not the original canonical session')
    wanted = {source_tool, incident['tool_id']}
    found = {tool: [] for tool in wanted}
    browser_failures = []
    with path.open('rb') as stream:
        header = json.loads(stream.readline())
        if header.get('type') != 'session_meta' or header.get('payload', {}).get('id') != owner:
            raise ValueError('Canonical native session header differs from the owner')
        start = max(0, path.stat().st_size - 8 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = row.get('payload', {})
            item = payload.get('item', {})
            if (row.get('type') == 'event_msg' and payload.get('type') == 'item_completed'
                    and item.get('id') in wanted):
                found[item['id']].append((row, offset, hashlib.sha256(line).hexdigest()))
            if (row.get('type') == 'event_msg' and payload.get('type') == 'item_completed'
                    and payload.get('thread_id') == owner and item.get('type') == 'McpToolCall'
                    and item.get('status') == 'failed' and item.get('server') == 'node_repl'
                    and (((item.get('result') or {}).get('_meta') or {}).get('codex/toolSurface') or {}).get('backend') == 'chrome'):
                browser_failures.append(row)
    if any(len(rows) != 1 for rows in found.values()):
        raise ValueError('Both exact terminal tools must be present once in bounded native receipts')

    def failure(tool, *, original):
        row, offset, digest = found[tool][0]
        payload = row['payload']; item = payload['item']; result = item.get('result') or {}
        surface = (result.get('_meta') or {}).get('codex/toolSurface') or {}
        target = (result.get('_meta') or {}).get('browser_use', {}).get('url')
        texts = [part.get('text') for part in result.get('content', []) if part.get('type') == 'text']
        if (payload.get('thread_id') != owner or not payload.get('turn_id')
                or item.get('type') != 'McpToolCall' or item.get('server') != 'node_repl'
                or item.get('tool') != 'js' or len(texts) != 1 or not isinstance(texts[0], str)
                or surface.get('backend') != 'chrome' or surface.get('browserFamily') != 'chrome'
                or not surface.get('browserId') or target not in ('https://www.xxmanmi.com/', 'https://xxmanmi.com/')
                or '.goto(' not in (item.get('arguments') or {}).get('code', '')):
            raise ValueError('Native receipt does not prove the original public Chrome navigation')
        error = texts[0]
        if item.get('status') == 'completed' and not original:
            caught = json.loads(error)
            if (caught.get('meta') != {'callId': tool, 'sessionId': binding['host_parent'], 'threadId': owner}
                    or caught.get('outcome', {}).get('ok') is not False):
                raise ValueError('Current terminal tool does not prove its caught navigation failure')
            error = caught['outcome'].get('error')
        elif item.get('status') != 'failed':
            raise ValueError('The earlier source tool must be a terminal native failure')
        if (not isinstance(error, str) or not re.fullmatch(
                r'Browser Use cannot open https://(?:www\.)?xxmanmi\.com/? in tab \d+\. '
                r'Browser reported: net::ERR_CONNECTION_CLOSED', error)):
            raise ValueError('Native receipt does not contain the exact public navigation failure')
        begin, end = payload.get('started_at_ms'), payload.get('completed_at_ms')
        if type(begin) is not int or type(end) is not int or not 0 < begin <= end:
            raise ValueError('Native failure lacks a real completion timestamp')
        at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=end)
        logged = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
        if logged.tzinfo is None or not timedelta(0) <= logged - at <= timedelta(seconds=5):
            raise ValueError('Native completion and persisted event timestamps disagree')
        return at, {'tool_id': tool, 'turn_id': payload['turn_id'], 'status': item['status'],
                    'completed_at': at.isoformat(), 'byte_offset': offset, 'record_sha256': digest,
                    'browser_id': surface['browserId'], 'target': target,
                    'native_error_sha256': hashlib.sha256(error.encode()).hexdigest()}

    earlier, proof = failure(source_tool, original=True)
    latest, latest_proof = failure(incident['tool_id'], original=source_tool == incident['tool_id'])
    cycle = datetime.fromisoformat(cycle_started_at.replace('Z', '+00:00'))
    last_seen = datetime.fromisoformat(incident['last_seen_at'].replace('Z', '+00:00'))
    if (not cycle <= earlier <= latest <= last_seen <= datetime.now(timezone.utc) + timedelta(seconds=5)
            or proof['browser_id'] != latest_proof['browser_id'] or proof['target'] != latest_proof['target']
            or proof['native_error_sha256'] != latest_proof['native_error_sha256']):
        raise ValueError('Native failures are not in the same current-cycle navigation chain')
    # record() can change the template of an unresolved incident. A later DOM
    # failure could then have been formally resolved without an ACK/search commit.
    # Reject that possible boundary instead of borrowing a previous incident.
    for row in browser_failures:
        if datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00')) < earlier:
            continue
        item = row['payload']['item']; result = item.get('result') or {}
        texts = [part.get('text', '') for part in result.get('content', []) if part.get('type') == 'text']
        if (len(texts) != 1 or hashlib.sha256(texts[0].encode()).hexdigest() != proof['native_error_sha256']
                or '.goto(' not in (item.get('arguments') or {}).get('code', '')):
            raise ValueError('Another Chrome failure may mark an intervening browser recovery boundary')
    return earlier, {'canonical_session': str(path), 'earlier_failure': proof,
                     'current_failure': latest_proof, 'native_error_sha256': proof['native_error_sha256'],
                     'recorded_error_sha256': incident['error_sha256']}


def _native_budget_receipts(owner, binding, source_tool, *, batch_id=None):
    """Read one original terminal event and optional claim receipts, without replay."""
    from app_server_control import RPC

    with RPC() as rpc:
        native = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
    if (native.get('id') != owner or native.get('parentThreadId') != binding.get('host_parent')
            or not isinstance(native.get('cwd'), str) or not native['cwd']
            or Path(native['cwd']).resolve() not in (PROJECT.resolve(), PROJECT.parent.resolve())):
        raise ValueError('Native failure owner/parent/cwd differs from the registered slot')
    path = Path(native['path'].removeprefix('\\\\?\\')).resolve()
    sessions = (Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))) / 'sessions').resolve()
    if (not path.is_relative_to(sessions) or not path.name.endswith('-' + owner + '.jsonl')):
        raise ValueError('Native receipt path is not the original canonical session')
    found = []; claims = []
    with path.open('rb') as stream:
        header = json.loads(stream.readline())
        if header.get('type') != 'session_meta' or header.get('payload', {}).get('id') != owner:
            raise ValueError('Canonical native session header differs from the owner')
        start = max(0, path.stat().st_size - 8 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        while True:
            offset = stream.tell()
            line = stream.readline()
            if not line:
                break
            try:
                row = json.loads(line)
            except (ValueError, UnicodeDecodeError):
                continue
            payload = row.get('payload', {})
            if (row.get('type') == 'event_msg' and payload.get('type') == 'item_completed'
                    and payload.get('item', {}).get('id') == source_tool):
                found.append((row, offset, hashlib.sha256(line).hexdigest()))
            item = payload.get('item', {})
            if (batch_id and row.get('type') == 'event_msg' and payload.get('type') == 'item_completed'
                    and payload.get('thread_id') == owner and item.get('type') == 'CommandExecution'
                    and item.get('status') == 'completed' and batch_id in item.get('aggregated_output', '')):
                claims.append((row, offset, hashlib.sha256(line).hexdigest()))
    if len(found) != 1:
        raise ValueError('Exact terminal tool must be present once in bounded native receipts')
    return path, found[0], claims


def _native_inventory_budget_start(owner, binding, incident, source_tool, cycle_started_at):
    """Preserve the actual first time of the exact read-only inventory failure."""
    if (incident.get('template') != 'chrome_unavailable' or incident.get('resolved_at')
            or incident.get('attempts') != 1 or source_tool != incident.get('tool_id')
            or incident.get('batch') or incident.get('command')):
        raise ValueError('Inventory correction requires the single original unresolved failure')
    path, (row, offset, digest), _ = _native_budget_receipts(owner, binding, source_tool)
    payload = row['payload']; item = payload['item']
    code = (item.get('arguments') or {}).get('code', '')
    pattern = (r"const (?P<probe>[A-Za-z_$][\w$]*) = \{meta:\{callId:nodeRepl\.requestMeta\.callId,"
        r"sessionId:nodeRepl\.requestMeta\.sessionId,threadId:nodeRepl\.requestMeta\.threadId\}, "
        r"types:\{(?P<browser>[A-Za-z_$][\w$]*):typeof (?P=browser),"
        r"(?P<tab>[A-Za-z_$][\w$]*):typeof (?P=tab)\}, "
        r"inventory:typeof (?P=browser)==='undefined'\?null:await (?P=browser)\.tabs\.list\(\)\}; "
        r"nodeRepl\.write\(JSON\.stringify\((?P=probe)\)\);")
    result = item.get('result') or {}; blocks = result.get('content') or []
    if (payload.get('thread_id') != owner or not payload.get('turn_id')
            or item.get('type') != 'McpToolCall' or item.get('status') != 'failed'
            or item.get('server') != 'node_repl' or item.get('tool') != 'js'
            or item.get('readOnlyHint') is not True or not re.fullmatch(pattern, code)
            or result.get('isError') is not True or len(blocks) != 1
            or blocks[0].get('type') != 'text'):
        raise ValueError('Native tool does not prove the exact read-only inventory failure')
    error = blocks[0].get('text', '')
    error_sha = hashlib.sha256(error.encode()).hexdigest()
    if (not re.fullmatch(r'Browser is not available: [-A-Za-z0-9]+', error)
            or error_sha != incident.get('error_sha256')):
        raise ValueError('Native inventory failure differs from the recorded exact error')
    from app_server_control import RPC
    with RPC() as rpc:
        turns = rpc.call('thread/turns/list', {'threadId':owner,'limit':1,
            'sortDirection':'desc','itemsView':'full'}).get('data',[])
    if (not turns or turns[0]['id'] != payload['turn_id']
            or turns[0]['status'] not in ('completed','interrupted','failed')):
        raise ValueError('Another native turn may mark an intervening browser recovery')
    begin, end = payload.get('started_at_ms'), payload.get('completed_at_ms')
    if type(begin) is not int or type(end) is not int or not 0 < begin <= end:
        raise ValueError('Native inventory failure lacks a real completion timestamp')
    at = datetime(1970,1,1,tzinfo=timezone.utc) + timedelta(milliseconds=end)
    logged = datetime.fromisoformat(row['timestamp'].replace('Z','+00:00'))
    cycle = datetime.fromisoformat(cycle_started_at.replace('Z','+00:00'))
    seen = datetime.fromisoformat(incident['last_seen_at'].replace('Z','+00:00'))
    if (logged.tzinfo is None or not timedelta(0) <= logged-at <= timedelta(seconds=5)
            or not cycle <= at <= seen <= datetime.now(timezone.utc)+timedelta(seconds=5)):
        raise ValueError('Native inventory timestamps differ from the current fault cycle')
    proof = {'tool_id':source_tool,'turn_id':payload['turn_id'],'status':'failed',
             'completed_at':at.isoformat(),'byte_offset':offset,'record_sha256':digest,
             'native_error_sha256':error_sha,'code_sha256':hashlib.sha256(code.encode()).hexdigest()}
    return at, {'proof_kind':'read_only_inventory_unavailable','canonical_session':str(path),
                'native_records':[proof],'recorded_error_sha256':incident['error_sha256'],
                'replay_authorized':False}


def _native_cdp_deadline_budget_start(owner, binding, incident, source_tool, cycle_started_at):
    """Prove the first exact CDP deadline in this unresolved original-tab chain."""
    error = 'Timed out after 10000ms waiting for CDP command Input.synthesizeScrollGesture.'
    error_sha = hashlib.sha256(error.encode()).hexdigest()
    if (incident.get('template') != 'unknown' or incident.get('resolved_at')
            or incident.get('attempts') != 1 or not incident.get('tool_id')
            or incident.get('batch') or incident.get('command')
            or incident.get('error_sha256') != error_sha):
        raise ValueError('Scroll correction requires the single exact unresolved native failure')
    path, (row, offset, digest), _ = _native_budget_receipts(owner, binding, source_tool)
    payload = row['payload']; item = payload['item']
    current_path, (current_row, current_offset, current_digest), _ = (
        _native_budget_receipts(owner, binding, incident['tool_id']))
    current_payload = current_row['payload']; current_item = current_payload['item']
    current_code = (current_item.get('arguments') or {}).get('code', '')
    pattern = (r"await (?P<tab>[A-Za-z_$][\w$]*)\.dom_cua\.scroll\(\{x:0,y:1200\}\); "
               r"nodeRepl\.write\(JSON\.stringify\(\{scroll:'issued_documented_dom_cua',"
               r"deltaY:1200\}\)\);")
    scroll_match = re.fullmatch(pattern, current_code)
    def exact_failure(p, native_item, message):
        result = native_item.get('result') or {}
        return (p.get('thread_id') == owner and bool(p.get('turn_id'))
                and native_item.get('type') == 'McpToolCall' and native_item.get('status') == 'failed'
                and native_item.get('server') == 'node_repl' and native_item.get('tool') == 'js'
                and result.get('isError') is True
                and result.get('content') == [{'type': 'text', 'text': message}])
    if (path != current_path or not scroll_match
            or not exact_failure(current_payload, current_item, error)):
        raise ValueError('Native tool does not prove the exact failed scroll gesture')
    native_records = [(current_row, current_offset, current_digest)]
    if source_tool != incident['tool_id']:
        readonly_dom = """()=>({observed_at:new Date().toISOString(),url:location.href,readyState:document.readyState,title:document.title,selected:[...document.querySelectorAll('input[type=radio]:checked')].map(x=>({id:x.id,name:x.name,value:x.value,label:[...x.labels||[]].map(l=>l.innerText.trim())})),links:[...document.querySelectorAll('#newList a[data-web]')].map(a=>({href:a.href,title:[...a.querySelectorAll('.weui-media-box__bd')].map(x=>[...x.childNodes].filter(n=>n.nodeType===3).map(n=>n.textContent.trim()).join('')).join(''),status:a.querySelector('.weui-media-box__desc')?.innerText.trim()||''})),pagination:[...document.querySelectorAll('a,button')].filter(x=>/下一页|下页|更多/.test(x.innerText)).map(x=>({text:x.innerText.trim(),href:x.href||''}))})"""
        read_pattern = (r"nodeRepl\.write\(JSON\.stringify\(\{sessionId:nodeRepl\.requestMeta\.sessionId,"
            r"threadId:nodeRepl\.requestMeta\.threadId,tabBinding:typeof (?P<tab>[A-Za-z_$][\w$]*),"
            r"module:typeof [A-Za-z_$][\w$]*,dom:await (?P=tab)\.playwright\.evaluate\("
            + re.escape(readonly_dom) + r"\)\}\)\);")
        read_match = re.fullmatch(read_pattern, (item.get('arguments') or {}).get('code', ''))
        def surface(native_item):
            return (native_item.get('result') or {}).get('_meta', {}).get('codex/toolSurface', {})
        before_dispatch = 'CDP operation exceeded its deadline before command dispatch'
        first_surface, last_surface = surface(item), surface(current_item)
        if (payload.get('turn_id') != current_payload.get('turn_id')
                or item.get('readOnlyHint') is not True or not read_match
                or read_match['tab'] != scroll_match['tab']
                or not exact_failure(payload, item, before_dispatch)
                or first_surface.get('backend') != 'chrome'
                or first_surface.get('browserFamily') != 'chrome'
                or first_surface.get('kind') != 'browserUse' or not first_surface.get('browserId')
                or any(first_surface.get(k) != last_surface.get(k)
                       for k in ('backend', 'browserFamily', 'browserId', 'kind'))):
            raise ValueError('Earlier read-only deadline differs from the original Chrome failure chain')
        native_records.insert(0, (row, offset, digest))
    from app_server_control import RPC
    with RPC() as rpc:
        turns = rpc.call('thread/turns/list', {'threadId': owner, 'limit': 3,
            'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
    original = next((t for t in turns if t['id'] == payload['turn_id']), None)
    if (not original or original['status'] not in ('inProgress', 'completed', 'interrupted', 'failed')
            or (original['status'] == 'inProgress' and turns[0]['id'] != original['id'])):
        raise ValueError('The original failed CDP tool is not in the recent original turns')
    epoch = datetime(1970, 1, 1, tzinfo=timezone.utc)
    cycle = datetime.fromisoformat(cycle_started_at.replace('Z', '+00:00'))
    seen = datetime.fromisoformat(incident['last_seen_at'].replace('Z', '+00:00'))
    proofs = []; times = []
    for native_row, native_offset, native_digest in native_records:
        p = native_row['payload']; native_item = p['item']
        begin, end = p.get('started_at_ms'), p.get('completed_at_ms')
        if type(begin) is not int or type(end) is not int or not 0 < begin <= end:
            raise ValueError('Native CDP failure lacks real completion timestamps')
        at = epoch + timedelta(milliseconds=end)
        logged = datetime.fromisoformat(native_row['timestamp'].replace('Z', '+00:00'))
        if (logged.tzinfo is None or not timedelta(0) <= logged-at <= timedelta(seconds=5)
                or not cycle <= epoch+timedelta(milliseconds=begin) <= at <= seen
                <= datetime.now(timezone.utc)+timedelta(seconds=5)):
            raise ValueError('Native CDP timestamps differ from the current fault cycle')
        times.append(at)
        proofs.append({'tool_id': native_item['id'], 'turn_id': p['turn_id'], 'status': 'failed',
            'completed_at': at.isoformat(), 'byte_offset': native_offset, 'record_sha256': native_digest,
            'native_error_sha256': hashlib.sha256(native_item['result']['content'][0]['text'].encode()).hexdigest(),
            'code_sha256': hashlib.sha256(native_item['arguments']['code'].encode()).hexdigest()})
    if len(times) == 2 and times[0] >= times[1]:
        raise ValueError('Earlier native read-only deadline must precede the recorded gesture timeout')
    return times[0], {'proof_kind': 'native_cdp_deadline_chain', 'canonical_session': str(path),
                'native_records': proofs, 'recorded_error_sha256': error_sha,
                'gesture_outcome': 'unknown', 'replay_authorized': False}


def _native_lookup_budget_start(owner, binding, incident, source_tool, cycle_started_at):
    """Prove the single recorded read-only lookup failed before it was registered."""
    if (incident.get('template') != 'unknown' or incident.get('resolved_at')
            or type(incident.get('attempts')) is not int or incident['attempts'] != 1
            or source_tool != incident.get('tool_id') or not source_tool
            or incident.get('batch') or incident.get('command')):
        raise ValueError('Lookup budget correction requires the single original unresolved failure')
    path, (row, offset, digest), _ = _native_budget_receipts(owner, binding, source_tool)
    payload = row['payload']; item = payload['item']; command = item.get('command')
    if (payload.get('thread_id') != owner or not payload.get('turn_id')
            or item.get('type') != 'CommandExecution' or item.get('source') != 'unified_exec_startup'
            or item.get('status') != 'failed' or type(item.get('exit_code')) is not int
            or item['exit_code'] != 1 or item.get('cwd') != (PROJECT / 'Datebase/xxmanmi-resource-db').as_uri()
            or not isinstance(command, list) or len(command) != 3
            or not all(isinstance(part, str) for part in command)
            or Path(command[0]).resolve() != (Path(os.environ.get('SystemRoot', 'C:/Windows'))
                                             / 'System32/WindowsPowerShell/v1.0/powershell.exe').resolve()
            or command[1] != '-Command' or not re.fullmatch(
                r"& '[A-Za-z]:/(?:[A-Za-z0-9_. -]+/)+python\.exe' -X utf8 -B dbctl\.py lookup --url "
                r"'https://(?:www\.)?xxmanmi\.com/video[0-9]+/[A-Za-z0-9]+\.html'", command[2])):
        raise ValueError('Native receipt does not prove the exact read-only project lookup failure')
    output = item.get('aggregated_output')
    if not isinstance(output, str) or item.get('stdout') != output or item.get('stderr') != '':
        raise ValueError('Native lookup output is missing or differs across receipt fields')
    # Remove actual CSI/OSC controls, never printable backslash escape spellings.
    error = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', output).strip('\r\n')
    expected = (r'fork/exec C:\Program Files\Docker\cli-plugins\docker-compose.exe: '
                'The paging file is too small for this operation to complete.')
    error_sha = hashlib.sha256(error.encode()).hexdigest()
    if error != expected or error_sha != incident.get('error_sha256'):
        raise ValueError('Native lookup resource error does not match the recorded exact error hash')
    begin, end = payload.get('started_at_ms'), payload.get('completed_at_ms')
    if type(begin) is not int or type(end) is not int or not 0 < begin <= end:
        raise ValueError('Native failure lacks a real completion timestamp')
    at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=end)
    logged = datetime.fromisoformat(row['timestamp'].replace('Z', '+00:00'))
    cycle = datetime.fromisoformat(cycle_started_at.replace('Z', '+00:00'))
    last_seen = datetime.fromisoformat(incident['last_seen_at'].replace('Z', '+00:00'))
    if (logged.tzinfo is None or not timedelta(0) <= logged - at <= timedelta(seconds=5)
            or not cycle <= at <= last_seen <= datetime.now(timezone.utc) + timedelta(seconds=5)):
        raise ValueError('Native lookup failure timestamps do not prove the current fault cycle')
    proof = {'tool_id': source_tool, 'turn_id': payload['turn_id'], 'status': item['status'],
             'completed_at': at.isoformat(), 'byte_offset': offset, 'record_sha256': digest,
             'native_error_sha256': error_sha}
    return at, {'canonical_session': str(path), 'earlier_failure': proof, 'current_failure': proof,
                'native_error_sha256': error_sha, 'recorded_error_sha256': incident['error_sha256']}


def _unawaited_capture_checkpoint(owner, binding, batch_id):
    """Snapshot the original claim; absence of journal growth is not an execution outcome."""
    if not isinstance(batch_id, str) or not re.fullmatch(r'batch-\d{6}', batch_id):
        raise ValueError('Empty-receipt budget proof requires the original formal batch')
    work = Path(binding['work']).resolve()
    coordination = coordctl._read_state(STATE)
    actor = coordination.get('threads', {}).get(owner, {})
    batch = coordination.get('batches', {}).get(batch_id, {})
    urls = batch.get('item_urls', [])
    if (actor.get('role') not in ('collector', 'peer') or actor.get('active_batch') != batch_id
            or Path(actor.get('work', '')).resolve() != work or batch.get('thread') != owner
            or batch.get('state') != 'claimed' or Path(batch.get('work', '')).resolve() != work
            or len(urls) != 4 or len(set(urls)) != 4 or (work / '.capture-helper.lock').exists()):
        raise ValueError('Empty-receipt proof requires the unchanged original four-item claim')
    items = []
    for url in urls:
        record = coordination.get('items', {}).get(url, {})
        if (record.get('batch') != batch_id or record.get('state') != 'claimed'
                or record.get('outcome') is not None or record.get('item', {}).get('url') != url):
            raise ValueError('An original claimed item changed while its capture outcome is unknown')
        items.append(record['item'])
    book_path = PROJECT / f'Datebase/xxmanmi-resource-db/.runtime/collection-task-books/detail-{batch_id}.json'
    input_path = work / f'{batch_id}-capture-input.json'
    documents = []
    for path in (book_path, input_path):
        if path.is_symlink() or path.stat().st_size > 128 * 1024:
            raise ValueError('Original claim evidence is not a bounded regular file')
        data = path.read_bytes()
        documents.append((json.loads(data), hashlib.sha256(data).hexdigest()))
    (book, book_sha), (capture_input, input_sha) = documents
    if (book.get('kind') != 'detail' or book.get('owner') != owner or book.get('batch_id') != batch_id
            or Path(book.get('work', '')).resolve() != work
            or Path(book.get('coordination_state', '')).resolve() != STATE.resolve()
            or book.get('scope', {}).get('observed_urls') != urls
            or capture_input != {'batch': batch_id, 'items': items, 'owner': owner,
                                 'taskbook': str(book_path), 'work': str(work)}):
        raise ValueError('Original taskbook/input differs from the current owner and exact claimed items')
    offsets = book.get('journal_offsets', {})
    if set(offsets) != {'details.ndjson', 'raw-details.ndjson'}:
        raise ValueError('Original taskbook must retain both pre-capture journal boundaries')
    journals = {}
    for name, size in offsets.items():
        path = work / name
        stat = path.stat()
        if type(size) is not int or size < 0 or path.is_symlink() or stat.st_size != size:
            raise ValueError('Journal growth or a changed original boundary prevents empty-receipt proof')
        with path.open('rb') as stream:
            if size:
                stream.seek(size - 1)
                if stream.read(1) != b'\n':
                    raise ValueError('Original journal boundary is not a complete line')
        journals[name] = {'size': size, 'mtime_ns': stat.st_mtime_ns, 'inode': stat.st_ino}
    return {'actor': actor, 'batch': batch, 'items': items, 'taskbook_created_at': book['created_at'],
            'taskbook': str(book_path), 'taskbook_sha256': book_sha,
            'capture_input': str(input_path), 'capture_input_sha256': input_sha, 'journals': journals}


def _native_unawaited_capture_budget_start(owner, binding, incident, source_tool, cycle_started_at):
    """Prove loss of a tracked completion receipt, never failure or termination of the capture."""
    if (incident.get('template') != 'unknown' or incident.get('resolved_at')
            or type(incident.get('attempts')) is not int or incident['attempts'] != 1
            or source_tool != incident.get('tool_id') or not source_tool
            or not incident.get('batch') or incident.get('command')):
        raise ValueError('Empty-receipt correction requires the single original unresolved batch incident')
    checkpoint = _unawaited_capture_checkpoint(owner, binding, incident['batch'])
    path, (row, offset, digest), claims = _native_budget_receipts(
        owner, binding, source_tool, batch_id=incident['batch'])
    payload = row['payload']; item = payload['item']; result = item.get('result') or {}
    code = (item.get('arguments') or {}).get('code', '')
    tab = re.search(r"listed\.find\(x=>x\.id==='(\d+)'\)", code) if isinstance(code, str) else None
    if (payload.get('thread_id') != owner or not payload.get('turn_id')
            or item.get('type') != 'McpToolCall' or item.get('server') != 'node_repl'
            or item.get('tool') != 'js' or item.get('status') != 'completed'
            or result.get('content') != [{'type': 'text', 'text': ''}] or result.get('isError') is not False
            or not tab):
        raise ValueError('Native receipt is not the exact completed empty-text capture call')
    expected_code = (
        "(async()=>{const fs=await import('node:fs/promises'); const inputPath='"
        + Path(checkpoint['capture_input']).as_posix()
        + "'; const task=JSON.parse(await fs.readFile(inputPath,'utf8')); const mod=await import('"
        + (PROJECT / 'scripts/collection/detail-capture.mjs').as_uri()
        + "?list-status-v2-amp'); const listed=await chrome.tabs.list(); const own=listed.find(x=>x.id==='"
        + tab[1] + "'); if(!own) throw Error('本人tab不在当前公开inventory'); const t=await chrome.tabs.get('"
        + tab[1] + "'); const lane={tab:t,items:task.items.slice(0,1),workDirectory:task.work,name:task.owner,"
        "alreadyLoadedFirst:false}; console.log(JSON.stringify({owner:task.owner,work:task.work,count:task.items.length,"
        "first:task.items[0],tab:{id:own.id,url:own.url},meta:{sessionId:nodeRepl.requestMeta.sessionId,"
        "threadId:nodeRepl.requestMeta.threadId}})); console.log(JSON.stringify(await mod.runDetailBatchSegment"
        "(lane,{recoverTransient:false})));})()")
    if code != expected_code:
        raise ValueError('Native code is not the original complete unawaited/unretained capture IIFE')

    def completed_time(record):
        event = record['payload']
        begin, end = event.get('started_at_ms'), event.get('completed_at_ms')
        if type(begin) is not int or type(end) is not int or not 0 < begin <= end:
            raise ValueError('Native receipt lacks a real completion timestamp')
        at = datetime(1970, 1, 1, tzinfo=timezone.utc) + timedelta(milliseconds=end)
        logged = datetime.fromisoformat(record['timestamp'].replace('Z', '+00:00'))
        if logged.tzinfo is None or not timedelta(0) <= logged - at <= timedelta(seconds=5):
            raise ValueError('Native completion and persisted event timestamps disagree')
        return at

    at = completed_time(row)
    cycle = datetime.fromisoformat(cycle_started_at.replace('Z', '+00:00'))
    claimed = datetime.fromisoformat(checkpoint['batch']['claimed_at'].replace('Z', '+00:00'))
    created = datetime.fromisoformat(checkpoint['taskbook_created_at'].replace('Z', '+00:00'))
    seen = datetime.fromisoformat(incident['last_seen_at'].replace('Z', '+00:00'))
    if not cycle <= claimed <= created <= at <= seen <= datetime.now(timezone.utc) + timedelta(seconds=5):
        raise ValueError('Empty receipt is outside the current original claim and incident cycle')
    claim_proofs = []
    for claim_row, claim_offset, claim_digest in claims:
        if completed_time(claim_row) > at:
            continue
        claim = claim_row['payload']['item']; command = claim.get('command', [])
        if (not isinstance(command, list) or len(command) != 3 or command[1] != '-Command'
                or not isinstance(command[2], str) or not re.fullmatch(
                    r'[A-Za-z]:/[A-Za-z0-9_./-]+/python\.exe -X utf8 -B scripts/collection/workflowctl\.py '
                    r'claim-detail --thread ' + re.escape(owner) + r' --limit 4', command[2])):
            continue
        if (not isinstance(command[0], str) or Path(command[0]).resolve() != (
                Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32/WindowsPowerShell/v1.0/powershell.exe').resolve()
                or claim.get('cwd') != PROJECT.as_uri() or type(claim.get('exit_code')) is not int
                or claim['exit_code'] != 0 or not claim_row['payload'].get('turn_id')):
            raise ValueError('Original claim command does not prove the registered project invocation')
        output = claim.get('aggregated_output', '')
        output = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)', '', output).strip('\r\n')
        claim_result = json.loads(output)
        claim_items = claim_result.get('items')
        if (claim_result.get('command') != 'claim-detail' or claim_result.get('ok') is not True
                or claim_result.get('batch') != incident['batch'] or claim_result.get('status') != 'claimed'
                or claim_result.get('batch_state') != 'claimed' or claim_result.get('observed') != {}
                or type(claim_result.get('remaining')) is not int or claim_result['remaining'] != 4
                or not isinstance(claim_items, list) or any(not isinstance(entry, dict) for entry in claim_items)
                or [entry.get('url') for entry in claim_items] != checkpoint['batch']['item_urls']
                or claim_result.get('work') != str(Path(binding['work']).resolve())
                or claim_result.get('capture_input') != checkpoint['capture_input']
                or claim_result.get('taskbook') != checkpoint['taskbook']
                or not created <= completed_time(claim_row) <= at):
            raise ValueError('Original native claim receipt differs from the unchanged four-item input')
        claim_proofs.append({'tool_id': claim['id'], 'turn_id': claim_row['payload']['turn_id'],
                             'status': 'completed', 'completed_at': completed_time(claim_row).isoformat(),
                             'byte_offset': claim_offset, 'record_sha256': claim_digest})
    if len(claim_proofs) != 1:
        raise ValueError('One original successful native claim receipt is required before the empty receipt')
    if _unawaited_capture_checkpoint(owner, binding, incident['batch']) != checkpoint:
        raise ValueError('Original claim or journal checkpoint changed during native verification')
    observed = {'tool_id': source_tool, 'turn_id': payload['turn_id'], 'status': 'completed',
                'completed_at': at.isoformat(), 'byte_offset': offset, 'record_sha256': digest,
                'content': [{'type': 'text', 'text': ''}], 'code_sha256': hashlib.sha256(code.encode()).hexdigest()}
    return at, {'proof_kind': 'unawaited_capture_receipt_gap', 'canonical_session': str(path),
                'native_status': 'completed', 'capture_outcome': 'unknown', 'replay_authorized': False,
                'source_observation': observed, 'native_records': [observed, claim_proofs[0]],
                'batch_checkpoint': checkpoint, 'recorded_error_sha256': incident['error_sha256']}


def _require_unresolved_navigation_chain(owner, binding, since):
    coordination = coordctl._read_state(STATE)
    actor = coordination.get('threads', {}).get(owner, {})
    if (actor.get('role') not in ('collector', 'peer')
            or Path(actor.get('work', '')).resolve() != Path(binding['work']).resolve()):
        raise ValueError('Formal owner/work differs from the registered failure owner')
    for batch in coordination.get('batches', {}).values():
        if (batch.get('thread') == owner and batch.get('state') == 'acknowledged'
                and datetime.fromisoformat(batch['acknowledged_at'].replace('Z', '+00:00')) >= since):
            raise ValueError('An intervening formal ACK prevents joining these failure incidents')
    for path in Path(binding['work']).glob('search-commit-*.json'):
        receipt = json.loads(path.read_bytes())
        if (receipt.get('owner') == owner and receipt.get('phase') == 'complete'
                and datetime.fromisoformat(receipt['completed_at'].replace('Z', '+00:00')) >= since):
            raise ValueError('An intervening formal search commit prevents joining these incidents')


def tighten_budget(owner, source_tool, *, authorized=False):
    """Only move an existing fault budget earlier, using its original native receipt."""
    config = policy()
    if (not authorized or config.get('enabled') is not True or not config.get('main_thread')
            or os.environ.get('CODEX_THREAD_ID') != config['main_thread']):
        raise ValueError('Only the authorized registered business main may tighten a fault budget')
    slot, binding = slot_for(owner)
    snapshot = state(); incident = snapshot['incidents'].get(slot)
    if (snapshot.get('stopped') or not incident or incident.get('resolved_at')
            or incident.get('owner') != owner or not source_tool or not incident.get('tool_id')
            or binding.get('registration_verified') is not True or not binding.get('host_parent')
            or Path(incident.get('work', '')).resolve() != Path(binding['work']).resolve()
            or not Path(binding['work']).resolve().is_relative_to(PROJECT.resolve())):
        raise ValueError('Budget correction requires the existing unresolved registered incident')
    prove_start = _native_navigation_budget_start
    if incident.get('template') == 'chrome_unavailable':
        prove_start = _native_inventory_budget_start
    if incident.get('template') == 'unknown':
        scroll_error = 'Timed out after 10000ms waiting for CDP command Input.synthesizeScrollGesture.'
        if incident.get('error_sha256') == hashlib.sha256(scroll_error.encode()).hexdigest():
            prove_start = _native_cdp_deadline_budget_start
        else:
            prove_start = (_native_unawaited_capture_budget_start if incident.get('batch')
                           else _native_lookup_budget_start)
    earlier, proof = prove_start(owner, binding, incident, source_tool, snapshot['cycle_started_at'])
    first = datetime.fromisoformat(incident['first_failed_at'])
    deadline = earlier + timedelta(seconds=3600)
    if earlier >= first:
        raise ValueError('Native receipt must strictly precede the existing first failure time')
    if deadline >= datetime.fromisoformat(incident['deadline']):
        raise ValueError('Budget correction must strictly shorten the existing deadline')
    _require_unresolved_navigation_chain(owner, binding, earlier)

    def tighten(saved):
        if (saved.get('stopped') or saved.get('cycle_started_at') != snapshot['cycle_started_at']
                or saved.get('incidents', {}).get(slot) != incident
                or policy() != config or slot_for(owner) != (slot, binding)):
            raise ValueError('Incident or registered authority changed during native verification')
        _require_unresolved_navigation_chain(owner, binding, earlier)
        if proof.get('proof_kind') == 'unawaited_capture_receipt_gap' and (
                _unawaited_capture_checkpoint(owner, binding, incident['batch']) != proof['batch_checkpoint']):
            raise ValueError('Original claim or journals changed before budget correction')
        with Path(proof['canonical_session']).open('rb') as stream:
            records = proof.get('native_records') or [proof['earlier_failure'], proof['current_failure']]
            for record in records:
                stream.seek(record['byte_offset'])
                if hashlib.sha256(stream.readline()).hexdigest() != record['record_sha256']:
                    raise ValueError('Original native receipt evidence changed during verification')
        active = saved['incidents'][slot]
        active.setdefault('budget_tightenings', []).append({
            'verified_at': utc_now(), 'verified_by': config['main_thread'],
            'previous_first_failed_at': active['first_failed_at'], 'previous_deadline': active['deadline'],
            'native_proof': proof})
        active.update(first_failed_at=earlier.isoformat(), deadline=deadline.isoformat())
        return {'ok': True, 'slot': slot, 'first_failed_at': active['first_failed_at'],
                'deadline': active['deadline'], 'source_tool': source_tool, 'replay_authorized': False}
    if proof.get('proof_kind') == 'unawaited_capture_receipt_gap':
        # resume already uses STATE -> RECOVERY; never invert that lock order.
        with coordctl.StateLock(STATE, timeout=5):
            return mutate(tighten)
    return mutate(tighten)


def _browser_idle_progress(owner, binding, incident, source_tool):
    """Prove restored read-only Chrome access when no detail work is available."""
    from app_server_control import RPC
    from urllib.parse import urlsplit

    main_owner = policy().get('main_thread')
    if not main_owner or os.environ.get('CODEX_THREAD_ID') != main_owner:
        raise ValueError('Only the registered business main may verify browser recovery')
    if (incident.get('gesture_outcome') == 'unknown'
            or any(t.get('native_proof', {}).get('gesture_outcome') == 'unknown'
                   for t in incident.get('budget_tightenings', []))):
        raise ValueError('The original gesture outcome remains unknown; a real ACK or search commit is required')
    if binding.get('registration_verified') is not True or not binding.get('host_parent'):
        raise ValueError('Browser recovery requires the verified original slot binding')
    if incident.get('batch') or incident.get('command'):
        raise ValueError('Browser recovery cannot resolve a workflow command or batch gate')
    with RPC() as rpc:
        native = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
        page = rpc.call('thread/turns/list', {
            'threadId': owner, 'limit': 3, 'sortDirection': 'desc', 'itemsView': 'full'})
    turns = page.get('data', [])
    if (native.get('id') != owner or native.get('parentThreadId') != binding.get('host_parent')
            or not isinstance(native.get('cwd'), str) or not native['cwd']
            or Path(native.get('cwd', '')).resolve() not in (PROJECT.resolve(), PROJECT.parent.resolve())
            or not turns or turns[0].get('status') != 'completed'
            or turns[0].get('startedAt', 0) < datetime.fromisoformat(incident['last_seen_at']).timestamp()):
        raise ValueError('Browser recovery requires the original owner and a later completed turn')
    entries = [(turn, item) for turn in reversed(turns) for item in turn.get('items', [])]
    failed = next((item for _, item in entries if item.get('id') == incident.get('tool_id')), None)
    recovered = next(((turn, item) for turn, item in entries if item.get('id') == source_tool), None)
    if failed is None or recovered is None or recovered[0]['id'] != turns[0]['id']:
        raise ValueError('Original failure and recovery must have native tool receipts')

    def browser_receipt(item, status):
        result = item.get('result') or {}
        surface = (result.get('_meta') or {}).get('codex/toolSurface') or {}
        code = (item.get('arguments') or {}).get('code', '')
        # Only the settled locator read shape is eligible; no extra statements,
        # computed selectors, escaped literals, method arguments, or other calls.
        locator_read = status == 'failed' and re.fullmatch(
            r'\s*const\s+(?P<locator>[A-Za-z_$][A-Za-z0-9_$]*)\s*=\s*'
            r'[A-Za-z_$][A-Za-z0-9_$]*\.playwright\.locator\(\s*'
            r'''(?:'[^'\\\r\n]*'|"[^"\\\r\n]*")\s*\)\s*;\s*'''
            r'nodeRepl\.write\(\s*JSON\.stringify\(\s*\{\s*'
            r'count\s*:\s*await\s+(?P=locator)\.count\(\s*\)\s*,\s*'
            r'visible\s*:\s*await\s+(?P=locator)\.isVisible\(\s*\)\s*,\s*'
            r'text\s*:\s*await\s+(?P=locator)\.innerText\(\s*\)\s*'
            r'\}\s*\)\s*\)\s*;?\s*', code) is not None
        if (item.get('type') != 'mcpToolCall' or item.get('server') != 'node_repl'
                or item.get('tool') != 'js' or item.get('status') != status
                or surface.get('backend') != 'chrome' or surface.get('browserFamily') != 'chrome'
                or not surface.get('browserId')
                or ('.playwright.evaluate(' not in code and not locator_read)
                or re.search(r'\.(?:click|fill|goto|reload|close|press|selectOption|check|uncheck)\s*\(|'
                             r'executePrepared|runSearchAction|localStorage|sessionStorage|fetch\s*\(', code)):
            raise ValueError('Only original read-only Chrome DOM calls qualify')
        texts = [part.get('text', '') for part in result.get('content', []) if part.get('type') == 'text']
        if len(texts) != 1:
            raise ValueError('Browser recovery requires one complete native text receipt')
        return result, surface, texts[0], locator_read

    _, failed_surface, error, failed_locator_read = browser_receipt(failed, 'failed')
    known_failure = (error == 'Playwright selector deadline exceeded' if failed_locator_read else
                     re.fullmatch(r'playwright\.evaluate exceeded its deadline|No tab with id: \d+\.', error))
    if (hashlib.sha256(error.encode()).hexdigest() != incident.get('error_sha256')
            or not known_failure):
        raise ValueError('Browser recovery cannot resolve an unknown action or non-browser failure')
    _, recovered_surface, text, _ = browser_receipt(recovered[1], 'completed')
    if failed_surface['browserId'] != recovered_surface['browserId']:
        raise ValueError('Recovery changed the original Chrome binding')
    failure_index = next(i for i, (_, item) in enumerate(entries) if item.get('id') == failed['id'])
    recovery_index = next(i for i, (_, item) in enumerate(entries) if item.get('id') == source_tool)
    if recovery_index <= failure_index or any(item.get('status') == 'failed'
                                             for _, item in entries[failure_index + 1:]):
        raise ValueError('A later native failure remains unresolved')
    snapshot = json.loads(text)
    address = urlsplit(snapshot.get('url', ''))
    observed = datetime.fromisoformat(snapshot.get('observed_at', '').replace('Z', '+00:00'))
    if (address.scheme != 'https' or address.hostname not in ('xxmanmi.com', 'www.xxmanmi.com')
            or address.query or snapshot.get('readyState') != 'complete'
            or not snapshot.get('title') or not isinstance(snapshot.get('links'), list)
            or not snapshot['links'] or observed < datetime.fromisoformat(incident['last_seen_at'])
            or observed > datetime.now(timezone.utc) + timedelta(seconds=5)):
        raise ValueError('Recovery lacks a fresh, complete public-page DOM receipt')
    coordination = coordctl._read_state(STATE)
    actor = coordination.get('threads', {}).get(owner, {})
    if (coordination.get('collection_paused') or actor.get('role') not in ('collector', 'peer')
            or actor.get('active_batch') or Path(actor.get('work', '')).resolve() != Path(binding['work']).resolve()
            or any(item.get('state') == 'queued' for item in coordination.get('items', {}).values())):
        raise ValueError('Available or unsettled detail work still requires formal ACK progress')
    return {'kind': 'native_browser_restored_no_detail_supply', 'tool_id': source_tool,
            'failure_tool_id': failed['id'], 'native_turn': recovered[0]['id'],
            'browser_family': 'chrome', 'browser_id': recovered_surface['browserId'],
            'observed_at': snapshot['observed_at'], 'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'counts_as_programme_success': False}


def progress(owner, *, batch=None, search_receipt=None, browser_source_tool=None):
    if policy().get('enabled') is not True:
        return {'ok': True, 'enabled': False}
    slot, binding = slot_for(owner)
    current = state()
    incident = current['incidents'].get(slot)
    if not incident or incident.get('resolved_at') or current['stopped']:
        return {'ok': True, 'changed': False}
    since = datetime.fromisoformat(incident['first_failed_at'])
    proof = None
    if batch:
        coordination = coordctl._read_state(STATE)
        row = coordination.get('batches', {}).get(batch)
        if (row and row['thread'] == owner and row['state'] == 'acknowledged'
                and datetime.fromisoformat(row['acknowledged_at'].replace('Z', '+00:00')) >= since
                and row.get('verification', {}).get('verified') is True):
            proof = {'kind': 'formal_ack', 'batch': batch, 'at': row['acknowledged_at']}
    elif browser_source_tool:
        proof = _browser_idle_progress(owner, binding, incident, browser_source_tool)
    elif search_receipt:
        path = Path(search_receipt).resolve()
        if path.parent != Path(binding['work']).resolve():
            raise ValueError('Search progress receipt is outside this owner work')
        row = json.loads(path.read_text('utf-8'))
        if (row.get('owner') == owner and row.get('phase') == 'complete'
                and datetime.fromisoformat(row['completed_at'].replace('Z', '+00:00')) >= since):
            proof = {'kind': 'formal_search_commit', 'receipt': str(path),
                     'sha256': hashlib.sha256(path.read_bytes()).hexdigest()}
    if proof is None:
        return {'ok': False, 'error': 'No new formal workflow progress; deadline preserved'}
    def resolved(saved):
        active = saved['incidents'].get(slot)
        if (active and not saved['stopped'] and active['first_failed_at'] == incident['first_failed_at']
                and (not browser_source_tool or
                     (active.get('tool_id') == incident.get('tool_id')
                      and active.get('last_seen_at') == incident.get('last_seen_at')))):
            active.update(resolved_at=utc_now(), resolution=proof)
            return {'ok': True, 'resolved': True, 'slot': slot}
        return {'ok': True, 'changed': False}
    return mutate(resolved)


def check():
    config = policy()
    if config.get('enabled') is not True:
        return {'ok': True, 'enabled': False, 'stop_required': False}
    current = state()
    now = datetime.now(timezone.utc)
    incidents = []
    for slot, row in current['incidents'].items():
        if not row.get('resolved_at'):
            age = max(0, int((now - datetime.fromisoformat(row['first_failed_at'])).total_seconds()))
            incidents.append({'slot': slot, 'owner': row['owner'], 'template': row['template'],
                              'age_seconds': age, 'deadline': row['deadline'], 'expired': age > 3600})
    return {'ok': True, 'stopped': current['stopped'], 'cycle_started_at': current['cycle_started_at'],
            'incidents': incidents, 'stop_required': current['stopped'] or any(v['expired'] for v in incidents),
            'reporting': 'stop_cycle_only'}


def stop_project(*, caller_thread=None):
    if not check().get('stop_required'):
        return {'ok': True, 'status': 'no_expired_incident', 'stopped': False}
    # Separate from the short ledger lock: only one driver issues stop RPCs.
    with coordctl.StateLock(RECOVERY.with_name('stop-execution.json'), timeout=1):
        return _stop_project(caller_thread=caller_thread)


def recover_stop_driver(*, caller_thread, request_id, authorized=False):
    """Preserve an interrupted stop driver's lock only after proving it exited.

    Ordinary state/claim locks stay non-breaking. This explicit main-only repair
    can finish the interrupted stop, but cannot resume, retire owners, or
    discard the original lock evidence.
    """
    if (not authorized or not request_id or not caller_thread
            or caller_thread != os.environ.get('CODEX_THREAD_ID')
            or caller_thread != policy().get('main_thread') or os.name != 'nt'):
        raise ValueError('Stop-driver recovery requires this Windows main and a direct user request')
    import ctypes
    from ctypes import wintypes
    from app_server_control import RPC
    from process_stop import (collection_terminal, dedicated_owner, process_snapshot,
                              protected_ancestors, targets_for, windows_argv)
    lock_path = RECOVERY.with_name('stop-execution.json.lock')
    with coordctl.StateLock(RECOVERY.with_name('stop-driver-recovery.json'), timeout=1):
        config, current = policy(), state()
        main = json.loads(MAIN.read_text('utf-8'))
        coordination = coordctl._read_state(STATE)
        owners = targets_for(config, main, coordination, current, PROJECT)
        if caller_thread != main['main_thread']:
            raise ValueError('Business main identity changed')
        if not lock_path.exists():
            return {'ok': True, 'status': 'no_stop_driver_lock', 'gate_kept_closed': True}
        if lock_path.is_symlink() or lock_path.resolve() != lock_path.absolute():
            raise ValueError('Stop-driver lock must be the exact local runtime file')
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD,
            wintypes.LPVOID, wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
        kernel.CreateFileW.restype = wintypes.HANDLE
        kernel.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.WaitForSingleObject.restype = wintypes.DWORD
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int,
            wintypes.LPVOID, wintypes.DWORD]
        handle = kernel.CreateFileW(str(lock_path), 0x80000000 | 0x00010000, 0,
                                   None, 3, 0x00200000, None)
        if handle == wintypes.HANDLE(-1).value:
            raise ctypes.WinError(ctypes.get_last_error())
        process = None
        try:
            buffer, length = ctypes.create_string_buffer(4096), wintypes.DWORD()
            if not kernel.ReadFile(handle, buffer, len(buffer), ctypes.byref(length), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if not 0 < length.value < len(buffer):
                raise ValueError('Invalid stop-driver lock size')
            raw = buffer.raw[:length.value]
            lock = json.loads(raw)
            if (set(lock) != {'pid', 'created_at', 'token'}
                    or type(lock['pid']) is not int or lock['pid'] <= 0
                    or not isinstance(lock['token'], str)
                    or not re.fullmatch(r'[0-9a-f]{32}', lock['token'])):
                raise ValueError('Invalid stop-driver lock identity')
            created = datetime.fromisoformat(lock['created_at'].replace('Z', '+00:00'))
            cycle = datetime.fromisoformat(current['cycle_started_at'].replace('Z', '+00:00'))
            if created.tzinfo is None or not cycle <= created <= datetime.now(timezone.utc):
                raise ValueError('Stop-driver lock is outside the current cycle')
            process = kernel.OpenProcess(0x1000 | 0x100000, False, lock['pid'])
            if process:
                if kernel.WaitForSingleObject(process, 0) != 0:
                    raise ValueError('Stop driver or a reused PID is still alive; lock preserved')
            elif ctypes.get_last_error() != 87:
                raise ctypes.WinError(ctypes.get_last_error())
            # The dead driver may have exited after interrupting only some
            # targets. Finish that same authorized stop while the original
            # lock is pinned exclusively; never reopen the collection gate.
            completion = _stop_project(caller_thread=caller_thread)
            if not completion.get('ok') or not state().get('stop_complete'):
                raise ValueError('Interrupted stop remains incomplete; lock preserved')
            receipts = {}
            with RPC() as rpc:
                for owner, work in owners.items():
                    thread = rpc.call('thread/read', {'threadId': owner, 'includeTurns': False})['thread']
                    if (thread.get('id') != owner or Path(thread['cwd']).resolve()
                            not in {PROJECT.resolve(), PROJECT.resolve().parent}):
                        raise ValueError('Native stop target identity/cwd mismatch')
                    if work is not None:
                        source = thread.get('source') or {}
                        spawned = source.get('subAgent', {}).get('thread_spawn', {}) if isinstance(source, dict) else {}
                        if (thread.get('parentThreadId') or spawned.get('parent_thread_id')) != main['host_parent_thread']:
                            raise ValueError('Native collector parent mismatch')
                    goal = rpc.call('thread/goal/get', {'threadId': owner}).get('goal')
                    turns = rpc.call('thread/turns/list', {'threadId': owner, 'limit': 1,
                        'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
                    latest = turns[0] if turns else {}
                    if ((goal and goal.get('status') != 'paused')
                            or (owner != caller_thread and latest.get('status') == 'inProgress')):
                        raise ValueError('A registered stop target is still active; lock preserved')
                    if (thread.get('status') or {}).get('type') != 'notLoaded':
                        terminals = rpc.call('thread/backgroundTerminals/list', {'threadId': owner})
                        if (terminals.get('nextCursor') or any(collection_terminal(t, work, PROJECT)
                                for t in terminals.get('data', []))):
                            raise ValueError('A registered collection terminal remains; lock preserved')
                    receipts[owner] = {'goal_status': goal.get('status') if goal else None,
                        'turn_status': latest.get('status'), 'runtime_status': thread.get('status')}
            rows = process_snapshot()
            protected = protected_ancestors(rows, os.getpid())
            if any(row.get('ProcessId') == lock['pid'] for row in rows):
                raise ValueError('Stop-driver PID was reused; lock preserved')
            for row in rows:
                if row.get('ExecutablePath') and row.get('CommandLine'):
                    owner = dedicated_owner(windows_argv(row['CommandLine']), row['ExecutablePath'], owners, PROJECT)
                    if owner and not (owner == caller_thread and row['ProcessId'] in protected):
                        raise ValueError('A dedicated collection process remains; lock preserved')
            digest = hashlib.sha256(raw).hexdigest()
            archive = lock_path.with_name(f'stop-execution.interrupted-{lock["pid"]}-{digest[:16]}.json.lock')
            proof = {'request_id': request_id, 'owner': caller_thread, 'at': utc_now(),
                'cycle_started_at': current['cycle_started_at'], 'driver_pid': lock['pid'],
                'driver_created_at': lock['created_at'], 'driver_exit_verified': True,
                'lock_sha256': digest, 'preserved_lock': str(archive), 'targets': receipts,
                'gate_kept_closed': True, 'phase': 'verified_before_preserving_lock'}
            def save_proof(saved):
                if not saved.get('stopped') or saved['cycle_started_at'] != current['cycle_started_at']:
                    raise ValueError('Recovery cycle changed')
                saved['stop_driver_recovery'] = proof
            mutate(save_proof)
            class RenameInfo(ctypes.Structure):
                _fields_ = [('ReplaceIfExists', wintypes.BOOLEAN), ('RootDirectory', wintypes.HANDLE),
                            ('FileNameLength', wintypes.DWORD), ('FileName', wintypes.WCHAR * 1)]
            name = str(archive).encode('utf-16-le')
            size = RenameInfo.FileName.offset + len(name)
            rename = ctypes.create_string_buffer(max(size, ctypes.sizeof(RenameInfo)))
            info = RenameInfo.from_buffer(rename)
            info.FileNameLength = len(name)
            ctypes.memmove(ctypes.addressof(rename) + RenameInfo.FileName.offset, name, len(name))
            if not kernel.SetFileInformationByHandle(handle, 3, rename, size):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            if process:
                kernel.CloseHandle(process)
            kernel.CloseHandle(handle)
        if hashlib.sha256(archive.read_bytes()).hexdigest() != digest or lock_path.exists():
            raise ValueError('Interrupted stop-driver lock preservation did not verify')
        def finish(saved):
            if saved.get('stop_driver_recovery', {}).get('lock_sha256') != digest:
                raise ValueError('Stop-driver proof changed')
            saved['stop_driver_recovery'].update(phase='original_lock_preserved', verified_at=utc_now())
        mutate(finish)
        return {'ok': True, 'status': 'interrupted_stop_driver_lock_preserved',
                'driver_pid': lock['pid'], 'lock_sha256': digest,
                'preserved_lock': str(archive), 'gate_kept_closed': True}


def _stop_project(*, caller_thread=None):
    config = policy()
    required = check()
    if config.get('stop_authorized') is not True or not required['stop_required']:
        return {'ok': True, 'status': 'no_expired_incident', 'stopped': False}
    old = state()
    force_required = config.get('force_process_stop_authorized') is True
    if old.get('stop_complete') and (not force_required or old.get('process_stop_receipt', {}).get('ok')):
        return {'ok': True, 'stopped': True, 'status': 'already_stopped',
                'checkpoint': str(RECOVERY)}
    from app_server_control import RPC
    with coordctl.StateLock(STATE, timeout=5):
        coordination = coordctl._read_state(STATE)
        coordination['collection_paused'] = True
        coordination['collection_pause_reason'] = 'User one-hour unrecovered collector stop policy'
        coordctl._atomic_write(STATE, coordination)
    with coordctl.StateLock(MAIN, timeout=5):
        main = json.loads(MAIN.read_text('utf-8'))
        main.update(paused=True, main_phase='one_hour_recovery_budget_stop')
        save_json(MAIN, main)
    completed_urls = {slot: set() for slot in main['slots']}
    start = datetime.fromisoformat(required['cycle_started_at'].replace('Z', '+00:00'))
    for row in coordination['batches'].values():
        if (row.get('state') == 'acknowledged'
                and datetime.fromisoformat(row['acknowledged_at'].replace('Z', '+00:00')) >= start):
            slot = next((key for key, value in main['slots'].items() if value['owner'] == row['thread']), None)
            if slot in completed_urls:
                completed_urls[slot].update(v['url'] for v in row.get('results', []) if v['outcome'] == 'captured')
    completed = {slot: len(urls) for slot, urls in completed_urls.items()}
    def checkpoint(current):
        current.update(stopped=True, stopped_at=current.get('stopped_at') or utc_now(),
                       stop_cause=required['incidents'], completed_captured_ack=completed,
                       reports=current.get('reports') or {'pending': True, 'delivered': False})
        current.setdefault('thread_stop_receipts', {})
    mutate(checkpoint)
    targets = [value['owner'] for value in main['slots'].values()]
    targets += [main['host_parent_thread'], main['main_thread']]
    receipts = {}
    process_receipt = None
    with RPC() as rpc:
        for tid in dict.fromkeys(targets):
            try:
                thread = rpc.call('thread/read', {'threadId': tid, 'includeTurns': False})['thread']
                project = PROJECT.resolve()
                # Native children inherit the shared workspace cwd, not their
                # registered project work. Do not expand the stop target set.
                if thread.get('id') != tid or Path(thread['cwd']).resolve() not in {project, project.parent}:
                    raise ValueError('Refusing to stop a thread outside this project')
                binding = next((v for v in main['slots'].values() if v['owner'] == tid), None)
                if binding is not None:
                    registered = coordination.get('threads', {}).get(tid, {})
                    work = Path(binding['work']).resolve()
                    source = thread.get('source') or {}
                    spawned = source.get('subAgent', {}).get('thread_spawn', {}) if isinstance(source, dict) else {}
                    parent = thread.get('parentThreadId') or spawned.get('parent_thread_id')
                    if (not work.is_relative_to(project)
                            or registered.get('role') not in {'collector', 'peer'}
                            or not registered.get('work')
                            or Path(registered['work']).resolve() != work
                            or parent != main['host_parent_thread']):
                        raise ValueError('Refusing to stop a collector without its exact project work and original host binding')
                elif tid not in {main['main_thread'], main['host_parent_thread']}:
                    raise ValueError('Refusing to stop an unregistered project control thread')
                goal = rpc.call('thread/goal/get', {'threadId': tid}).get('goal')
                if goal and goal.get('status') != 'paused':
                    rpc.call('thread/goal/set', {'threadId': tid, 'status': 'paused'})
                turns = rpc.call('thread/turns/list', {'threadId': tid, 'limit': 1,
                                                     'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
                turn = turns[0] if turns else {}
                if tid != caller_thread and turn.get('status') == 'inProgress':
                    rpc.call('turn/interrupt', {'threadId': tid, 'turnId': turn['id']})
                actual_goal = rpc.call('thread/goal/get', {'threadId': tid}).get('goal')
                latest = rpc.call('thread/turns/list', {'threadId': tid, 'limit': 1,
                    'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
                actual_turn = latest[0] if latest else {}
                terminal = actual_turn.get('status') != 'inProgress' or tid == caller_thread
                paused = not actual_goal or actual_goal.get('status') == 'paused'
                receipts[tid] = {'ok': paused and terminal, 'goal_paused_or_absent': paused,
                                 'interrupt_requested': tid != caller_thread and turn.get('status') == 'inProgress',
                                 'actual_turn_status': actual_turn.get('status'),
                                 'actual_goal_status': actual_goal.get('status') if actual_goal else None,
                                 'caller_finishing_stop_report': tid == caller_thread}
            except Exception as error:
                receipts[tid] = {'ok': False, 'error_type': type(error).__name__}
        if force_required:
            from process_stop import stop_owned_processes
            process_receipt = stop_owned_processes(config, main, coordination, state(), PROJECT, rpc,
                                                   caller_thread=caller_thread)
    def saved(current):
        current['thread_stop_receipts'].update(receipts)
        if process_receipt is not None:
            current['process_stop_receipt'] = process_receipt
        current['stop_complete'] = (all(v.get('ok', True) for v in receipts.values())
                                    and (process_receipt is None or process_receipt['ok']))
    mutate(saved)
    if caller_thread != main['main_thread'] and not state().get('stop_report_request_queued'):
        import subprocess
        from app_server_control import SOCK, CODEX
        from production_sync import hidden_options
        message = ('已触发用户唯一自动停工条件：采集线程连续故障超过3600秒未恢复。'
                   '核验 .runtime/main-agent/recovery-current.json 的真实停工回执；'
                   '未完成时只补齐既有精确停工流程。对用户仅显示working，不发送周期汇报。'
                   'Goal已按用户授权暂停；不得恢复、领批或新建线程。')
        try:
            delivered = subprocess.run([CODEX, 'queue', '--remote', 'unix://' + SOCK.replace('\\', '/'),
                                    '--thread', main['main_thread'], '--message', message],
                                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL, timeout=20, **hidden_options())
            mutate(lambda current: current.update(stop_report_request_queued=delivered.returncode == 0))
        except Exception as error:
            mutate(lambda current: current.update(stop_report_request_error=type(error).__name__))
    return {'ok': state()['stop_complete'], 'stopped': True,
            'cycle': check()['cycle_started_at'], 'completed_captured_ack': completed,
            'expired_slots': [v['slot'] for v in required['incidents'] if v['expired']],
            'thread_stop_receipts': receipts, 'process_stop_receipt': process_receipt,
            'checkpoint': str(RECOVERY)}


def guard(caller_thread=None):
    status = check()
    if status['stop_required']:
        if not status.get('stopped') or not state().get('stop_complete'):
            stop_project(caller_thread=caller_thread)
        raise RuntimeError('Project stopped by one-hour recovery policy; durable checkpoint=' + str(RECOVERY))
    return status


def resume_project(*, caller_thread, request_id, authorized=False):
    """An explicit user restart begins a new cycle; retries never reset a cycle.

    Preserve the entire stopped ledger in-place. Open the shared gate LAST so a
    partial write or failed native Goal resume remains fail-closed and resumable.
    """
    if not authorized or not request_id or caller_thread != policy().get('main_thread'):
        raise ValueError('Resume requires the actual main and explicit user authorization/request')
    from app_server_control import RPC
    with coordctl.StateLock(RECOVERY.with_name('stop-execution.json'), timeout=5):
        current = state()
        resume = current.get('user_resume', {})
        continuing = resume.get('request_id') == request_id and not current.get('stopped')
        if current.get('stopped') and (resume.get('request_id') == request_id or any(
                row.get('state', {}).get('user_resume', {}).get('request_id') == request_id
                for row in current.get('previous_cycles', []))):
            raise ValueError('A completed restart request cannot restart a later stop')
        if not current.get('stopped') and not continuing:
            return {'ok': True, 'status': 'already_running', 'cycle_reset': False}
        if current.get('stopped') and not current.get('stop_complete'):
            raise ValueError('Verify the original project stop before restarting')
        main = json.loads(MAIN.read_text('utf-8'))
        if main.get('main_thread') != caller_thread:
            raise ValueError('Business main identity changed')
        with coordctl.StateLock(STATE, timeout=5):
            coordination = coordctl._read_state(STATE)
            if not continuing and not coordination.get('collection_paused'):
                raise ValueError('Stopped recovery ledger disagrees with the shared gate')
        # The native user lifecycle is changed only for this explicit resume.
        with RPC() as rpc:
            goal = rpc.call('thread/goal/get', {'threadId': caller_thread}).get('goal')
            if not goal:
                adoption = main.get('main_identity_adoption', {})
                if (caller_thread != os.environ.get('CODEX_THREAD_ID')
                        or adoption.get('to') != caller_thread
                        or adoption.get('authorization') != 'direct_user'):
                    raise ValueError('Persistent Goal missing; no authorized main adoption')
                thread = rpc.call('thread/read', {'threadId': caller_thread,
                                                 'includeTurns': False})['thread']
                if thread.get('id') != caller_thread or Path(thread['cwd']).resolve() not in {
                        PROJECT.resolve(), PROJECT.resolve().parent}:
                    raise ValueError('The adopting user main is not the current native thread')
            elif goal.get('status') == 'paused':
                rpc.call('thread/goal/set', {'threadId': caller_thread, 'status': 'active'})
            actual = rpc.call('thread/goal/get', {'threadId': caller_thread}).get('goal')
            if goal and (not actual or actual.get('status') != 'active'):
                raise ValueError('Native Goal did not actually resume')
        if not continuing:
            def begin(saved):
                if not saved.get('stopped') or not saved.get('stop_complete'):
                    raise ValueError('Stop ledger changed during user restart')
                history = saved.get('previous_cycles', [])
                previous = {key: value for key, value in saved.items() if key != 'previous_cycles'}
                digest = hashlib.sha256(json.dumps(previous, sort_keys=True,
                    ensure_ascii=False, separators=(',', ':')).encode('utf-8')).hexdigest()
                at = utc_now()
                saved.clear()
                saved.update(version=1, stopped=False, incidents={}, cycle_started_at=at,
                    previous_cycles=history + [{'state': previous, 'sha256': digest}],
                    user_resume={'request_id': request_id, 'owner': caller_thread,
                                 'at': at, 'authorization': 'direct_user',
                                 'prior_cycle_preserved': True, 'gate_opened': False})
                return {'ok': True}
            mutate(begin)
        with coordctl.StateLock(MAIN, timeout=5):
            main = json.loads(MAIN.read_text('utf-8'))
            main.update(paused=False, main_phase='collection_supervision',
                        collection_resume=state()['user_resume'])
            save_json(MAIN, main)
        with coordctl.StateLock(STATE, timeout=5):
            coordination = coordctl._read_state(STATE)
            coordination['collection_paused'] = False
            coordination['collection_pause_reason'] = None
            coordctl._atomic_write(STATE, coordination)
        mutate(lambda saved: saved['user_resume'].update(gate_opened=True))
        return {'ok': True, 'status': 'resumed_by_direct_user',
                'cycle_started_at': state()['cycle_started_at'],
                'native_goal_status': actual.get('status') if actual else None,
                'control_mode': 'native_goal' if actual else 'explicit_current_user_main',
                'identities_and_batches_preserved': True}


def adopt_main(*, caller_thread, request_id, authorized=False, live_handoff=False):
    """Explicitly transfer the business-main binding; never replace collectors."""
    if (not authorized or not request_id or not caller_thread
            or caller_thread != os.environ.get('CODEX_THREAD_ID')):
        raise ValueError('Main adoption requires this actual thread and explicit user request')
    from app_server_control import RPC
    with coordctl.StateLock(RECOVERY.with_name('stop-execution.json'), timeout=5):
        config = policy()
        current = state()
        main = json.loads(MAIN.read_text('utf-8'))
        coordination = coordctl._read_state(STATE)
        stopped_handoff = (current.get('stopped') and current.get('stop_complete')
                           and main.get('paused') and coordination.get('collection_paused'))
        if not stopped_handoff and not live_handoff:
            raise ValueError('Main adoption requires verified stop and closed gates')
        if live_handoff and (current.get('stopped') or main.get('paused')
                             or coordination.get('collection_paused')
                             or check().get('stop_required')):
            raise ValueError('Live main handoff cannot resume a pause or bypass the fault deadline')
        pending = current.get('main_identity_adoption', {})
        prior = pending.get('from') if pending.get('request_id') == request_id else config['main_thread']
        if main.get('main_thread') not in {prior, caller_thread} or config['main_thread'] not in {prior, caller_thread}:
            raise ValueError('Unexpected main identity change')
        handoff_proof = None
        with RPC() as rpc:
            thread = rpc.call('thread/read', {'threadId': caller_thread, 'includeTurns': False})['thread']
            if thread.get('id') != caller_thread or Path(thread['cwd']).resolve() not in {
                    PROJECT.resolve(), PROJECT.resolve().parent}:
                raise ValueError('Main identity/cwd mismatch')
            if caller_thread in {v['owner'] for v in main['slots'].values()} | {main['host_parent_thread']}:
                raise ValueError('Main cannot become a collector or the original host')
            if live_handoff:
                if prior == caller_thread:
                    raise ValueError('Live handoff requires a distinct, explicitly retired main')
                previous = rpc.call('thread/read', {'threadId': prior,
                                                   'includeTurns': False})['thread']
                if (previous.get('id') != prior
                        or Path(previous['cwd']).resolve() not in {PROJECT.resolve(), PROJECT.resolve().parent}
                        or previous.get('status', {}).get('type') not in {'idle', 'notLoaded'}):
                    raise ValueError('Previous main must finish its native turn before live handoff')
                goal = rpc.call('thread/goal/get', {'threadId': prior}).get('goal')
                if goal and goal.get('status') not in {'paused', 'complete'}:
                    raise ValueError('Previous main still has an automatic continuation Goal')
                turns = rpc.call('thread/turns/list', {'threadId': prior, 'limit': 1,
                    'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
                terminal = turns[0] if turns else {}
                if terminal.get('status') not in {'completed', 'interrupted', 'failed'}:
                    raise ValueError('Previous main lacks a verified terminal native turn')
                if any(item.get('status') in {'inProgress', 'running', 'pending'}
                       for item in terminal.get('items', []) if item.get('type') != 'reasoning'):
                    raise ValueError('Previous main has an unsettled native action')
                inventory = ({'data': []} if previous['status']['type'] == 'notLoaded' else
                    rpc.call('thread/backgroundTerminals/list', {'threadId': prior}))
                if inventory.get('data') or inventory.get('nextCursor'):
                    raise ValueError('Previous main still owns background commands')
                handoff_proof = {'previous_turn': terminal['id'],
                    'previous_turn_status': terminal['status'],
                    'previous_goal_status': goal.get('status') if goal else None,
                    'background_commands': 0, 'cycle_and_fault_budget_preserved': True,
                    'collection_gate_preserved': True}
        adoption = {'from': prior, 'to': caller_thread, 'request_id': request_id,
                    'at': (pending.get('at') if pending.get('request_id') == request_id else None) or utc_now(),
                    'authorization': 'direct_user',
                    'collectors_preserved': True, 'host_preserved': True}
        if handoff_proof:
            adoption['live_handoff'] = handoff_proof
        def save_adoption(saved):
            if live_handoff and check().get('stop_required'):
                raise ValueError('Fault deadline reached during main handoff')
            previous_adoption = saved.get('main_identity_adoption') or main.get('main_identity_adoption')
            if previous_adoption and previous_adoption != adoption:
                saved.setdefault('main_identity_history', []).append(previous_adoption)
            saved['main_identity_adoption'] = adoption
        mutate(save_adoption)
        with coordctl.StateLock(MAIN, timeout=5):
            main = json.loads(MAIN.read_text('utf-8'))
            main.update(main_thread=caller_thread, title_discovery_owner=caller_thread,
                        main_identity_adoption=adoption)
            save_json(MAIN, main)
        with coordctl.StateLock(POLICY, timeout=5):
            config = policy()
            config.update(main_thread=caller_thread, main_identity_adoption=adoption)
            save_json(POLICY, config)
        return {'ok': True, 'main_thread': caller_thread, 'prior_main': prior,
                'collectors_and_checkpoints_preserved': True,
                'gate_kept_closed': bool(stopped_handoff), 'live_handoff': handoff_proof}


def bind_replacement(*, caller_thread, request_id, slot, previous_owner,
                     replacement_owner, authorized=False):
    """Bind one cleaned slot to a native replacement without renewing its budget."""
    if (not authorized or caller_thread != os.environ.get('CODEX_THREAD_ID')
            or caller_thread != policy().get('main_thread')
            or slot not in ('01', '02', '03', '04')
            or not request_id or not previous_owner or not replacement_owner
            or previous_owner == replacement_owner):
        raise ValueError('Replacement requires the actual main and explicit single-slot authorization')
    from app_server_control import RPC
    from contextlib import ExitStack
    import sys
    sys.path.insert(0, str(PROJECT / 'skills/project-cleanup/scripts'))
    import cleanupctl

    with coordctl.StateLock(RECOVERY.with_name('stop-execution.json'), timeout=5):
        main = json.loads(MAIN.read_text('utf-8'))
        binding = main['slots'][slot]
        current = state()
        if (current.get('stopped') or check()['stop_required']
                or current.get('user_resume', {}).get('request_id') != request_id
                or main.get('paused') or main.get('main_thread') != caller_thread
                or binding.get('owner') not in (previous_owner, replacement_owner)):
            raise ValueError('Replacement cannot change the current cycle or bypass a stop')
        cleanup = cleanupctl.paths(PROJECT)[2]
        receipts = {name: cleanup / f'{name}-{previous_owner}.json'
                    for name in ('session', 'owner', 'work', 'stop')}
        hashes = {name: hashlib.sha256(path.read_bytes()).hexdigest()
                  for name, path in receipts.items()}
        proof = {name: json.loads(path.read_text('utf-8')) for name, path in receipts.items()}
        session_root = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
        old_work = Path(proof['owner']['original_registration']['work']).resolve()
        if (proof['session'].get('owner') != previous_owner
                or proof['session'].get('deleted') is not True
                or cleanupctl.session_files(session_root, previous_owner)
                or proof['owner'].get('owner') != previous_owner
                or proof['stop'].get('thread') != previous_owner
                or proof['stop'].get('owner_stopped') is not True
                or proof['stop'].get('user_authorized') is not True
                or proof['work'].get('owner') != previous_owner
                or proof['work'].get('verified') is not True
                or proof['work'].get('removed') is not True
                or Path(proof['work']['original_work']).resolve() != old_work
                or old_work.exists() or not Path(proof['work']['archive']).is_file()):
            raise ValueError('Exact old session/owner/work cleanup is not verified')
        with RPC() as rpc:
            native = rpc.call('thread/read', {'threadId': replacement_owner,
                                             'includeTurns': False})['thread']
            latest = rpc.call('thread/turns/list', {'threadId': replacement_owner,
                'limit': 1, 'sortDirection': 'desc', 'itemsView': 'full'})['data'][0]
            goal = rpc.call('thread/goal/get', {'threadId': replacement_owner}).get('goal')
            commands = rpc.call('thread/backgroundTerminals/list', {'threadId': replacement_owner})
        if (native.get('id') != replacement_owner
                or native.get('parentThreadId') != main['host_parent_thread']
                or native.get('model') != 'gpt-6-luna'
                or native.get('reasoningEffort') != 'medium'
                or native.get('forkedFromId') is not None
                or Path(native['cwd']).resolve() not in (PROJECT.resolve(), PROJECT.parent.resolve())
                or latest.get('status') not in ('completed', 'interrupted', 'failed')
                or goal or commands.get('data') or commands.get('nextCursor')
                or any(item.get('status') in ('inProgress', 'running', 'pending')
                       for item in latest.get('items', []) if item.get('type') != 'reasoning')):
            raise ValueError('Replacement lacks its own terminal native registration checkpoint')
        task_name = native['source']['subAgent']['thread_spawn']['agent_path']
        with ExitStack() as locks:
            for path in (STATE, RECOVERY, MAIN, POLICY):
                locks.enter_context(coordctl.StateLock(path, timeout=5))
            coordination = coordctl._read_state(STATE)
            saved = state()
            main = json.loads(MAIN.read_text('utf-8'))
            config = policy()
            registered = coordination['threads'].get(replacement_owner, {})
            retired = coordination.get('retired_threads', {}).get(previous_owner, {})
            work = Path(registered.get('work', '')).resolve()
            expected = PROJECT / 'Datebase/xxmanmi-resource-db/.runtime/collection-workers' / (slot + '-' + replacement_owner)
            other_owners = {entry['owner'] for key, entry in main['slots'].items() if key != slot}
            if (saved.get('cycle_started_at') != current['cycle_started_at']
                    or saved.get('stopped') or check()['stop_required']
                    or saved.get('user_resume', {}).get('request_id') != request_id
                    or main.get('main_thread') != caller_thread or config.get('main_thread') != caller_thread
                    or main['slots'][slot] != binding or main.get('paused')
                    or coordination.get('collection_paused')
                    or previous_owner in coordination['threads']
                    or previous_owner not in coordination.get('retired_threads', {})
                    or Path(retired.get('work', '')).resolve() != old_work
                    or Path(retired.get('cleanup_receipt', '')).resolve() != receipts['owner'].resolve()
                    or set(coordination['threads']) != other_owners | {replacement_owner}
                    or registered.get('role') not in ('collector', 'peer')
                    or registered.get('active_batch') or work != expected.resolve()
                    or not work.is_dir() or work.is_symlink()
                    or (hasattr(work, 'is_junction') and work.is_junction())
                    or any(hashlib.sha256(path.read_bytes()).hexdigest() != hashes[name]
                           for name, path in receipts.items())):
                raise ValueError('Slot, cleanup, registration, or cycle changed before binding')
            incident = saved.get('incidents', {}).get(slot)
            if not incident or incident.get('resolved_at') or incident.get('owner') not in (previous_owner, replacement_owner):
                raise ValueError('Replacement requires the unresolved original slot budget')
            existing = saved.get('slot_replacements', {}).get(slot)
            change = existing or {'at': utc_now(), 'slot': slot, 'from': previous_owner,
                'to': replacement_owner, 'request_id': request_id, 'work': str(work),
                'prior_binding': binding, 'cleanup_receipt_sha256': hashes,
                'native_registration_turn': latest['id'], 'first_failed_at': incident['first_failed_at'],
                'deadline': incident['deadline'], 'replay_authorized': False}
            if change['from'] != previous_owner or change['to'] != replacement_owner or change['request_id'] != request_id:
                raise ValueError('Conflicting single-slot replacement')
            saved.setdefault('slot_replacements', {})[slot] = change
            if incident['owner'] == previous_owner:
                incident.setdefault('owner_history', []).append({'owner': previous_owner,
                    'work': incident['work'], 'replaced_at': change['at'],
                    'original_tool_id': incident.get('tool_id'), 'gesture_outcome': incident.get('gesture_outcome')})
                incident.update(owner=replacement_owner, work=str(work))
            save_json(RECOVERY, saved)
            main['slots'][slot] = {'owner': replacement_owner, 'work': str(work),
                'host_parent': main['host_parent_thread'], 'task_name': task_name,
                'browser_group': slot, 'model': 'gpt-6-luna', 'reasoning_effort': 'medium',
                'registration_verified': True, 'classification_enabled': False,
                'title_discovery_enabled': False, 'replaces': previous_owner}
            main.setdefault('slot_replacements', {})[slot] = change
            save_json(MAIN, main)
            config.setdefault('slot_replacements', {})[slot] = change
            save_json(POLICY, config)
            return {'ok': True, 'slot': slot, 'owner': replacement_owner, 'work': str(work),
                    'cycle_started_at': saved['cycle_started_at'], 'first_failed_at': incident['first_failed_at'],
                    'deadline': incident['deadline'], 'budget_preserved': True, 'replay_authorized': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['init', 'plan', 'record', 'tighten-budget', 'progress', 'check', 'stop', 'recover-stop-driver', 'resume', 'adopt-main', 'bind-replacement'])
    parser.add_argument('--thread')
    parser.add_argument('--error')
    parser.add_argument('--error-file', type=Path)
    parser.add_argument('--batch')
    parser.add_argument('--source-tool')
    parser.add_argument('--search-receipt', type=Path)
    parser.add_argument('--browser-source-tool')
    parser.add_argument('--authorized', action='store_true')
    parser.add_argument('--request-id', help='Actual explicit user restart request identifier')
    parser.add_argument('--slot', choices=['01', '02', '03', '04'])
    parser.add_argument('--owner', help='Exact cleaned previous owner')
    parser.add_argument('--replacement', help='Native replacement registered with its own independent work')
    parser.add_argument('--live-handoff', action='store_true',
                        help='Explicit main-only handoff after the previous main is terminal; preserve the running cycle')
    args = parser.parse_args()
    if args.command == 'tighten-budget' and (
            not args.authorized or not args.thread or not args.source_tool
            or args.error is not None or args.error_file is not None or args.batch
            or args.search_receipt or args.browser_source_tool or args.request_id or args.live_handoff):
        parser.error('tighten-budget requires only --authorized, --thread and --source-tool')
    error = args.error or (args.error_file.read_text('utf-8') if args.error_file else '')
    if args.command == 'init':
        result = mutate(lambda value: {'ok': True, 'cycle_started_at': value['cycle_started_at']})
    elif args.command == 'plan':
        result = {'ok': True, **classify(error)}
    elif args.command == 'record' and args.thread and error:
        result = record(args.thread, error, batch=args.batch, tool_id=args.source_tool)
    elif args.command == 'tighten-budget':
        result = tighten_budget(args.thread, args.source_tool, authorized=args.authorized)
    elif args.command == 'progress' and args.thread:
        if args.browser_source_tool and (not args.authorized or args.batch or args.search_receipt):
            parser.error('Browser recovery requires main authorization and one exact native tool')
        result = progress(args.thread, batch=args.batch, search_receipt=args.search_receipt,
                          browser_source_tool=args.browser_source_tool)
    elif args.command == 'check':
        result = check()
    elif args.command == 'stop' and args.authorized:
        result = stop_project(caller_thread=args.thread)
    elif args.command == 'recover-stop-driver':
        result = recover_stop_driver(caller_thread=args.thread, request_id=args.request_id,
                                     authorized=args.authorized)
    elif args.command == 'resume' and args.authorized and args.thread and args.request_id:
        result = resume_project(caller_thread=args.thread, request_id=args.request_id,
                                authorized=True)
    elif args.command == 'adopt-main' and args.authorized and args.thread and args.request_id:
        result = adopt_main(caller_thread=args.thread, request_id=args.request_id,
                            authorized=True, live_handoff=args.live_handoff)
    elif args.command == 'bind-replacement':
        result = bind_replacement(caller_thread=args.thread, request_id=args.request_id,
            slot=args.slot, previous_owner=args.owner, replacement_owner=args.replacement,
            authorized=args.authorized)
    else:
        parser.error('Missing required owner/error or explicit stop authorization')
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
