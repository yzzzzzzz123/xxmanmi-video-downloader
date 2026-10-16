#!/usr/bin/env python3
"""Journal the original owner's completed native CUA detail observations.

No site requests, browser transport, MCP tool invocation, fabricated results,
claim changes, resource auditing, covers or ACKs occur here. Those remain in the
native browser and existing formal workflow respectively.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess

import coordctl
import recoveryctl
import workflowctl as workflow
from app_server_control import RPC, hidden_options

HERE = Path(__file__).resolve().parent
ADAPTER = HERE / 'native-detail-capture.mjs'


def node_call(value):
    node = shutil.which('node.exe' if os.name == 'nt' else 'node')
    if not node:
        raise RuntimeError('Existing Node runtime is unavailable')
    result = subprocess.run([node, str(ADAPTER)], input=json.dumps(value, ensure_ascii=False),
                            text=True, encoding='utf-8', capture_output=True, timeout=30,
                            **hidden_options())
    if result.returncode:
        # Diagnostics contain helper code/errors, never the submitted DOM payload.
        raise RuntimeError('Native detail adapter failed: ' + result.stderr[-1800:])
    return json.loads(result.stdout)


def claimed(args):
    if os.environ.get('CODEX_THREAD_ID') != args.thread:
        raise ValueError('Only the original collection owner may prepare or import its DOM')
    slot, binding = recoveryctl.slot_for(args.thread)
    state = workflow.read_state(workflow.DEFAULT_STATE)
    coordctl.require_collection_running(state)
    peer = coordctl._require_peer(state, args.thread)
    batch = state['batches'].get(args.batch)
    if (not batch or batch['thread'] != args.thread or batch['state'] != 'claimed'
            or peer.get('active_batch') != args.batch or args.url not in batch['item_urls']):
        raise ValueError('Expected the original active claimed detail URL')
    work = Path(peer['work']).resolve()
    if Path(batch['work']).resolve() != work:
        raise ValueError('Claim work directory changed')
    root = (workflow.DB_ROOT / '.runtime').resolve()
    if not work.is_relative_to(root) or work == root or not work.is_dir():
        raise ValueError('Unsafe or missing formal work directory')
    item = state['items'][args.url]['item']
    book_path = workflow.DEFAULT_TASKBOOKS / f'detail-{args.batch}.json'
    if not book_path.is_file():
        raise ValueError('Run the formal claim-detail entry before preparing DOM evidence')
    _, book = workflow._detail_book(workflow.DEFAULT_STATE, args.thread, args.batch,
                                    work, batch, workflow.DEFAULT_TASKBOOKS)
    return binding, work, batch, item, book


def code_for(args, start):
    return node_call({'action': 'code', 'owner': args.thread, 'batch': args.batch,
                      'url': args.url, 'tabVariable': args.tab_variable,
                      'tabId': args.tab_id, 'start': start,
                      'segmentSize': getattr(args, 'segment_size', 1000),
                      'codeFormat': getattr(args, 'code_format', 'function')})['code']


def generated_code_match(args, expected, actual):
    """Exact comparison against 64 explicitly audited compact renderings.

    Only six single ASCII spaces beside fixed punctuation may be omitted.
    No generic whitespace normalization, token rewriting or result repair occurs.
    Missing/changed reader anchors fail closed; every other byte stays exact.
    """
    if not isinstance(actual, str):
        return None
    if actual == expected:
        return {'code': actual, 'spacing_profile': 0}
    if getattr(args, 'code_format', 'function') != 'compact':
        return None
    anchors = [
        (f'if (String({args.tab_variable}.id) !==', ' '),
        ('Math.imul(hashA ^ code,', ' 16777619)'),
        ('hashB = Math.imul(hashB, 33) ^ code;', ' } };'),
        ("label: (node.textContent || '').trim(),", ' group: groupOf(node)'),
        ('writable: true,', ' enumerable: true'),
        ('return {...header,', ' groupCounts, lineLabels};'),
    ]
    offsets = {}
    for bit, (left, right) in enumerate(anchors):
        anchor = left + right
        if expected.count(anchor) != 1:
            return None
        offsets[expected.index(anchor) + len(left)] = bit
    cursor = profile = 0
    for offset, char in enumerate(expected):
        if offset in offsets and (cursor >= len(actual) or actual[cursor] != ' '):
            profile |= 1 << offsets[offset]
            continue
        if cursor >= len(actual) or actual[cursor] != char:
            return None
        cursor += 1
    if cursor != len(actual):
        return None
    # Reconstruct the selected finite rendering and require byte equality.
    variant = ''.join(char for offset, char in enumerate(expected)
                      if not (offset in offsets and profile & (1 << offsets[offset])))
    if variant != actual:
        return None
    return {'code': variant, 'spacing_profile': profile}


def owner_receipt_items(args, binding):
    # Read only official settled receipts; never send tool/input calls to a child.
    if getattr(args, 'native_session', None):
        return canonical_receipt_items(args, binding)
    with RPC() as rpc:
        thread = rpc.call('thread/read', {'threadId': args.thread, 'includeTurns': False})['thread']
        if (thread.get('id') != args.thread or thread.get('parentThreadId') != binding['host_parent']
                or Path(thread.get('cwd', '')).resolve() not in
                (workflow.PROJECT_ROOT.resolve(), workflow.PROJECT_ROOT.parent.resolve())):
            raise ValueError('Native owner, parent or workspace differs from formal binding')
        turns = rpc.call('thread/turns/list', {'threadId': args.thread, 'limit': 2,
                        'sortDirection': 'desc', 'itemsView': 'full'}).get('data', [])
    if not turns or any(i.get('type') == 'mcpToolCall' and i.get('status') == 'inProgress'
                        for i in turns[0].get('items', [])):
        raise ValueError('A native tool is still in flight; retain its original handle')
    return [i for t in reversed(turns) for i in t.get('items', [])]


def canonical_receipt_items(args, binding):
    """Read the owner's real append-only native log, without control-socket access."""
    path = Path(args.native_session).resolve()
    sessions = (Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))
                / 'sessions').resolve()
    if (not path.is_relative_to(sessions) or path.is_symlink()
            or not path.name.endswith('-' + args.thread + '.jsonl')):
        raise ValueError('Expected the original canonical session under CODEX_HOME/sessions')
    items = []
    in_flight = set()
    with path.open('rb') as stream:
        header = json.loads(stream.readline())
        meta = header.get('payload', {})
        if (header.get('type') != 'session_meta' or meta.get('id') != args.thread
                or meta.get('parent_thread_id') != binding['host_parent']
                or Path(meta.get('cwd', '')).resolve() not in
                (workflow.PROJECT_ROOT.resolve(), workflow.PROJECT_ROOT.parent.resolve())):
            raise ValueError('Canonical native owner, parent or workspace differs from binding')
        end = os.fstat(stream.fileno()).st_size
        start = max(0, end - 8 * 1024 * 1024)
        stream.seek(start)
        if start:
            stream.readline()
        while stream.tell() < end:
            offset = stream.tell()
            line = stream.readline(end - offset)
            if not line.endswith(b'\n'):
                raise ValueError('Native log has an unfinished record; retain the original handle')
            row = json.loads(line)
            if row.get('type') != 'event_msg':
                continue
            payload = row.get('payload', {})
            if not isinstance(payload, dict):
                raise ValueError('Malformed canonical native event')
            item = payload.get('item', {})
            if not isinstance(item, dict):
                raise ValueError('Malformed canonical native tool item')
            if (row.get('type') != 'event_msg' or payload.get('thread_id') != args.thread
                    or item.get('type') != 'McpToolCall'):
                continue
            if payload.get('type') == 'item_started':
                in_flight.add(item['id'])
            elif payload.get('type') == 'item_completed':
                in_flight.discard(item['id'])
                items.append({**item, 'type': 'mcpToolCall', '_canonical_receipt': {
                    'path': str(path), 'byte_offset': offset,
                    'record_sha256': hashlib.sha256(line).hexdigest(),
                    'turn_id': payload.get('turn_id')}})
    if in_flight:
        raise ValueError('A native tool is still in flight; retain its original handle')
    return items


def discover_receipts(args, binding):
    """Expose genuine native IDs omitted from the model's tool-result display."""
    candidates = []
    rejected = []
    for call in owner_receipt_items(args, binding):
        result = call.get('result') or {}
        surface = (result.get('_meta') or {}).get('codex/toolSurface') or {}
        contents = result.get('content', [])
        if (call.get('type') != 'mcpToolCall' or call.get('status') != 'completed'
                or call.get('server') != 'cua_repl' or call.get('tool') != 'js'
                or call.get('error') or result.get('isError')
                or surface.get('backend') != 'chrome' or surface.get('browserFamily') != 'chrome'
                or str(surface.get('browserId')) != args.browser_id
                or len(contents) != 1 or contents[0].get('type') != 'text'):
            continue
        try:
            payload = json.loads(contents[0]['text'])
        except (ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        expected = {'schema': 'xxmanmi-native-detail-v1', 'owner': args.thread,
                    'batch': args.batch, 'expectedUrl': args.url, 'tabId': args.tab_id}
        segment_size = getattr(args, 'segment_size', 1000)
        if segment_size != 1000:
            expected['segmentSize'] = segment_size
        start = payload.get('start')
        if (any(payload.get(k) != v for k, v in expected.items())
                or (start is not None and (type(start) is not int or start < 0 or start % segment_size))):
            continue
        match = generated_code_match(args, code_for(args, start),
                                     (call.get('arguments') or {}).get('code'))
        if match is None:
            rejected.append({'call_id': call['id'], 'start': start,
                             'reason': 'generated_code_mismatch'})
            continue
        if not isinstance(payload.get('observation'), dict):
            rejected.append({'call_id': call['id'], 'start': start,
                             'reason': 'missing_dom_observation'})
            continue
        candidates.append({'call_id': call['id'], 'start': start,
                           'observed_at': payload.get('observedAt'),
                           **({'spacing_profile': match['spacing_profile']}
                              if match['spacing_profile'] else {})})
    return {'ok': True, 'status': 'read_only_receipt_candidates', 'calls': candidates[-101:],
            'rejected': rejected[-101:],
            'next_action': 'use_actual_ids_for_import_which_revalidates_all_evidence'}


def read_receipts(args, binding, batch):
    ids = [args.metadata_call, *args.segment_call]
    segment_size = getattr(args, 'segment_size', 1000)
    if len(ids) != len(set(ids)) or len(ids) > 101:
        raise ValueError('Source calls must be distinct and bounded')
    items = owner_receipt_items(args, binding)
    positions = []
    proofs = []
    payloads = []
    claimed_at = datetime.fromisoformat(batch['claimed_at'].replace('Z', '+00:00'))
    now = datetime.now(timezone.utc)
    previous_at = None
    for index, call_id in enumerate(ids):
        found = [(n, i) for n, i in enumerate(items) if i.get('id') == call_id]
        if len(found) != 1:
            raise ValueError('Exact native source call missing from bounded owner receipts')
        position, call = found[0]
        result = call.get('result') or {}
        surface = (result.get('_meta') or {}).get('codex/toolSurface') or {}
        contents = result.get('content', [])
        start = None if index == 0 else (index - 1) * segment_size
        code = code_for(args, start)
        match = generated_code_match(args, code, (call.get('arguments') or {}).get('code'))
        if (call.get('type') != 'mcpToolCall' or call.get('status') != 'completed'
                or call.get('server') != 'cua_repl' or call.get('tool') != 'js'
                or call.get('error') or result.get('isError')
                or match is None
                or surface.get('backend') != 'chrome' or surface.get('browserFamily') != 'chrome'
                or str(surface.get('browserId')) != args.browser_id
                or len(contents) != 1 or contents[0].get('type') != 'text'):
            raise ValueError('Expected the exact generated read-only native CUA call')
        payload = json.loads(contents[0]['text'])
        expected = {'schema': 'xxmanmi-native-detail-v1', 'owner': args.thread,
                    'batch': args.batch, 'expectedUrl': args.url, 'tabId': args.tab_id, 'start': start}
        if segment_size != 1000:
            expected['segmentSize'] = segment_size
        if any(payload.get(k) != v for k, v in expected.items()):
            raise ValueError('Native output binding differs from original claim/tab')
        if not isinstance(payload.get('observation'), dict):
            raise ValueError('Native call did not return a DOM observation')
        observed_at = datetime.fromisoformat(payload['observedAt'].replace('Z', '+00:00'))
        if (observed_at.tzinfo is None or observed_at < claimed_at
                or not -5 <= (now - observed_at).total_seconds() <= 3600
                or (previous_at and observed_at < previous_at)):
            raise ValueError('Native DOM observation is stale or out of order')
        previous_at = observed_at
        positions.append(position)
        payloads.append(payload)
        proofs.append({'call_id': call_id, 'code_sha256': hashlib.sha256(match['code'].encode()).hexdigest(),
                       **({'canonical_code_sha256': hashlib.sha256(code.encode()).hexdigest(),
                           'spacing_profile': match['spacing_profile'],
                           'code_verification': 'exact_audited_compact_rendering/v1'}
                          if match['spacing_profile'] else {}),
                       'output_sha256': hashlib.sha256(contents[0]['text'].encode()).hexdigest(),
                       'surface': surface,
                       **({'canonical_receipt': call['_canonical_receipt']}
                          if '_canonical_receipt' in call else {})})
    if positions != sorted(positions):
        raise ValueError('Native source calls are out of order')
    between = items[positions[0]:positions[-1] + 1]
    if any(i.get('type') == 'mcpToolCall' and i.get('id') not in ids for i in between):
        raise ValueError('Unexpected browser action between native DOM segments')
    return {'schema': 'xxmanmi-native-detail-evidence-v1', 'owner': args.thread,
            'batch': args.batch, 'tabId': args.tab_id, 'browserId': args.browser_id,
            **({'segmentSize': segment_size} if segment_size != 1000 else {}),
            'metadata': payloads[0], 'segments': payloads[1:], 'sourceCalls': ids, 'proofs': proofs}


def run(args):
    binding, work, batch, item, book = claimed(args)
    if args.command == 'receipts':
        return discover_receipts(args, binding)
    if args.command == 'prepare':
        if args.url in workflow._detail_observations(work, batch, book):
            return {'ok': True, 'status': 'already_journaled', 'next_action': 'claim-detail'}
        return {'ok': True, 'code': code_for(args, args.start), 'timeout_ms': 120000,
                'code_format': getattr(args, 'code_format', 'function'),
                'segment_size': getattr(args, 'segment_size', 1000),
                'next_action': 'execute_exact_code_in_original_native_cua'}
    evidence = read_receipts(args, binding, batch)
    with workflow.work_lock(work):
        with coordctl.StateLock(workflow.DEFAULT_STATE):
            new_binding, new_work, new_batch, new_item, new_book = claimed(args)
            if (new_binding, new_work, new_batch, new_item, new_book) != (binding, work, batch, item, book):
                raise ValueError('Formal owner/claim changed while reading native evidence')
            observed = workflow._detail_observations(work, batch, book)
            if args.url in observed:
                return {'ok': True, 'status': 'already_journaled', 'outcome': observed[args.url],
                        'next_action': 'claim-detail'}
            digest = hashlib.sha256(json.dumps(evidence, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
            path = work / f'{args.batch}-native-detail-{digest[:24]}.json'
            if path.is_symlink():
                raise ValueError('Unsafe native evidence path')
            data = json.dumps(evidence, ensure_ascii=False, indent=2) + '\n'
            try:
                with path.open('x', encoding='utf-8', newline='\n') as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
            except FileExistsError:
                if path.read_text(encoding='utf-8') != data:
                    raise ValueError('Existing native evidence differs; retaining it')
            evidence['evidencePath'] = str(path)
            result = node_call({'action': 'journal', 'evidence': evidence, 'item': item, 'work': str(work)})
            return {**result, 'batch': args.batch, 'source_calls': evidence['sourceCalls'],
                    'evidence': str(path), 'next_action': 'claim-detail_then_formal_complete-detail'}


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    sub = result.add_subparsers(dest='command', required=True)
    for name in ['prepare', 'receipts', 'import']:
        command = sub.add_parser(name)
        for flag in ['thread', 'batch', 'url', 'tab-variable', 'tab-id', 'browser-id']:
            command.add_argument('--' + flag, required=True)
        command.add_argument('--code-format', choices=['function', 'string', 'compact', 'compact-v2'], default='function',
                             help='Exact generated code form; compact uses a single-line function without nested string escaping')
        command.add_argument('--segment-size', type=int, choices=[100, 1000], default=1000,
                             help='Entries per native DOM segment; use 100 when tool output is truncated')
        if name == 'prepare':
            command.add_argument('--start', type=int)
        else:
            command.add_argument('--native-session', type=Path,
                                 help='Original canonical native log, with narrowly granted read access')
        if name == 'import':
            command.add_argument('--metadata-call', required=True)
            command.add_argument('--segment-call', action='append', required=True)
    return result


if __name__ == '__main__':
    args = None
    try:
        args = parser().parse_args()
        print(json.dumps(run(args), ensure_ascii=False))
    except Exception as error:
        failure = {'ok': False, 'error_type': type(error).__name__, 'error': str(error)}
        if (args is not None and args.command == 'import'
                and args.thread == os.environ.get('CODEX_THREAD_ID')):
            try:
                import recoveryctl
                failure['recovery'] = recoveryctl.record(
                    args.thread, str(error), command='native_detailctl import', batch=args.batch)
            except Exception as recovery_error:
                failure['recovery_error_type'] = type(recovery_error).__name__
        print(json.dumps(failure, ensure_ascii=False))
        raise SystemExit(1)
