import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {mkdtemp, mkdir, readFile, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {createHash} from 'node:crypto';
import {spawnSync} from 'node:child_process';
import {fileURLToPath} from 'node:url';
import {SEARCH_EVIDENCE_API_VERSION, beginSearchDomObservation, finishSearchDomObservation,
  cancelPreparedSearchAction, executePreparedSearchAction, finalizePreparedSearchAction,
  observeSearchAction, prepareSearchAction, readSearchDom, readSearchPreparationDom,
  releasePreparedSearchAction} from './search-evidence.mjs';

const task = {task_id: 'lifecycle-fixture', owner: 'offline-owner', query: '离线节目', channel: '量子'};
const searchOptions = {selectedChannelSelector: '.selected', emptyResultSelector: '.empty', loadingSelector: '.loading'};
function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return {promise, resolve, reject};
}

async function actionFixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-lifecycle-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const before = {observed_at: new Date(Date.now() - 1000).toISOString(),
    page_url: 'https://www.xxmanmi.com/', input_value_after_action: task.query,
    selected_channels: ['非凡'], result_count: 0, visible_detail_links: [],
    empty_result_visible: true, pagination_pending: false, result_container_visible: true,
    loading_indicator: {selector: '.loading', visible_count: 0, visible_texts: []}};
  const action = {input_value: task.query, total_count: 1,
    visible_controls: [{tag: 'BOTTON', text: task.channel, onclick: 'searchN(1,this)', disabled: false}]};
  const fixture = {root, clicks: 0, reads: 0, before,
    after: {...before, selected_channels: [task.channel], observed_at: new Date(Date.now() + 10000).toISOString()},
    click: async () => {}, read: null, prepareRead: null};
  fixture.tab = {id: 'offline-tab', playwright: {
    evaluate: async reader => {
      if (reader === readSearchPreparationDom) return fixture.prepareRead ? await fixture.prepareRead() : {action, before};
      assert.equal(reader, readSearchDom);
      fixture.reads += 1;
      return fixture.read ? await fixture.read() : fixture.after;
    },
    locator: selector => ({click: async options => {
      assert.equal(selector, '.channel');
      assert.deepEqual(options, {timeoutMs: 5000, noWaitAfter: true});
      fixture.clicks += 1;
      return await fixture.click();
    }}),
  }};
  fixture.prepare = () => prepareSearchAction({tab: fixture.tab, task,
    workDirectory: path.join(root, 'worker'), runtimeRoot: root, searchOptions, actionSelector: '.channel'});
  fixture.records = [];
  // This is the existing workflowctl/shared_discovery v1 durable ledger shape,
  // not a new cancellation protocol or a production claim/collector identity.
  fixture.terminal = async (actionId, overrides = {}, actionOverrides = {}) => {
    const ledger = path.join(root, 'title-discovery', 'search-claim-abandonments.json');
    const evidencePath = path.join(root, 'worker', `quarantine-${actionId}.json`);
    const evidence = {version: 1, ...task, reason: 'quarantined_unverifiable_result',
      claim_sha256: 'a'.repeat(64), logs_stopped: true, search_action: {action_id: actionId,
        state: 'completed_unverifiable', action_attempts: 1, replay_authorized: false,
        original_module_instance_available: true, public_ui_completed: true, ...actionOverrides}};
    const evidenceBytes = Buffer.from(JSON.stringify(evidence));
    await mkdir(path.dirname(evidencePath), {recursive: true});
    await writeFile(evidencePath, evidenceBytes);
    const record = {...task, abandonment_id: `abandon-${actionId}`, abandoned_at: new Date().toISOString(),
      reason: 'quarantined_unverifiable_result', browser_host_terminated: false, browser_tab_exists: true,
      original_module_instance_available: true, public_ui_completed: true, action_state: 'completed_unverifiable',
      replay_authorized: false, logs_stopped: true, journals: [], browser_evidence_exists: false,
      commit_receipt_exists: false, search_journal_contains_task: false,
      source_claim_sha256: 'a'.repeat(64), evidence_sha256: createHash('sha256').update(evidenceBytes).digest('hex'), ...overrides};
    fixture.records = fixture.records.filter(item => item.abandonment_id !== record.abandonment_id);
    fixture.records.push(record);
    await mkdir(path.dirname(ledger), {recursive: true});
    await writeFile(ledger, JSON.stringify({version: 1, abandonments: fixture.records}));
    return {ok: true, command: 'quarantine-search', status: record.reason, task_id: task.task_id,
      query: task.query, channel: task.channel, abandonment_id: record.abandonment_id, ledger};
  };
  fixture.evidencePath = actionId => path.join(root, 'worker', `quarantine-${actionId}.json`);
  fixture.release = async (actionId, overrides, actionOverrides) => releasePreparedSearchAction({tab: fixture.tab, actionId,
    terminalReceipt: await fixture.terminal(actionId, overrides, actionOverrides), terminalEvidencePath: fixture.evidencePath(actionId)});
  return fixture;
}

test('v4 staged signatures remain compatible and unused preparations can be cancelled repeatedly', async t => {
  assert.equal(SEARCH_EVIDENCE_API_VERSION, 4);
  const fixture = await actionFixture(t);
  for (let index = 0; index < 64; index += 1) {
    const {actionId} = await fixture.prepare();
    assert.throws(() => cancelPreparedSearchAction({tab: {}, actionId}), /original tab/);
    const cancelled = cancelPreparedSearchAction({tab: fixture.tab, actionId});
    assert.equal(cancelled.status, 'cancelled_before_execution');
    assert.equal(cancelled.claim_resolved, false);
    await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /missing/);
  }
  assert.equal(fixture.clicks, 0);
});

test('concurrent preparations reserve all 16 slots before awaiting DOM', async t => {
  const fixture = await actionFixture(t);
  const barrier = deferred();
  fixture.prepareRead = () => barrier.promise;
  const actions = Array.from({length: 16}, () => fixture.prepare());
  await assert.rejects(fixture.prepare(), /available action slot/);
  barrier.reject(new Error('fixture preparation failure'));
  const results = await Promise.allSettled(actions);
  assert.equal(results.filter(result => result.status === 'rejected').length, 16);
  fixture.prepareRead = null;
  const prepared = await Promise.all(Array.from({length: 16}, () => fixture.prepare()));
  await assert.rejects(fixture.prepare(), /available action slot/);
  for (const value of prepared) cancelPreparedSearchAction({tab: fixture.tab, actionId: value.actionId});
});

test('failed clicks retain no-replay identity until a real terminal ledger permits release', async t => {
  const fixture = await actionFixture(t);
  fixture.click = async () => { throw new Error('fixture adapter disconnected'); };
  for (let index = 0; index < 32; index += 1) {
    const {actionId} = await fixture.prepare();
    await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /adapter disconnected/);
    await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /already executed/);
    assert.throws(() => cancelPreparedSearchAction({tab: fixture.tab, actionId}), /unexecuted/);
    await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId,
      terminalReceipt: {ok: true}}), /matching workflow terminal receipt/);
    assert.equal((await fixture.release(actionId)).status, 'released_terminal');
  }
  assert.equal(fixture.clicks, 32);
});

test('unknown actions are not evicted to make room and cannot be released by completed-click fiction', async t => {
  const fixture = await actionFixture(t);
  fixture.click = async () => { const error = new Error('fixture click timeout'); error.name = 'TimeoutError'; throw error; };
  const actions = [];
  for (let index = 0; index < 16; index += 1) {
    const {actionId} = await fixture.prepare();
    actions.push(actionId);
    assert.equal((await executePreparedSearchAction({tab: fixture.tab, actionId})).next_action, 'inspect_search_action');
  }
  await assert.rejects(fixture.prepare(), /available action slot/);
  const actionId = actions[0];
  await assert.rejects(fixture.release(actionId, {public_ui_completed: false}), /does not prove/);
  await assert.rejects(fixture.release(actionId, {reason: 'quarantined_foreign_host_session', action_id: actionId}), /does not prove/);
  await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /already executed/);
  for (const id of actions) await fixture.release(id);
  assert.equal(fixture.clicks, 16);
});

test('pending click cannot be cancelled, finalized, or terminally released', async t => {
  const fixture = await actionFixture(t);
  const barrier = deferred();
  fixture.click = () => barrier.promise;
  const {actionId} = await fixture.prepare();
  const executing = executePreparedSearchAction({tab: fixture.tab, actionId});
  assert.throws(() => cancelPreparedSearchAction({tab: fixture.tab, actionId}), /unexecuted/);
  await assert.rejects(finalizePreparedSearchAction({tab: fixture.tab, actionId}), /completed or timed-out/);
  await assert.rejects(fixture.release(actionId), /settled prepared action/);
  barrier.resolve();
  await executing;
  await fixture.release(actionId);
});

test('finalize DOM failure retains proof for a later read without another click', async t => {
  const fixture = await actionFixture(t);
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  fixture.read = async () => { throw new Error('fixture read error'); };
  await assert.rejects(finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250}), /read error/);
  fixture.read = null;
  const saved = await finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250});
  assert.equal(saved.result_count, 0);
  assert.equal(JSON.parse(await readFile(saved.evidencePath, 'utf8')).refresh_evidence.kind, 'verified_ui_action');
  await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /missing/);
  assert.equal(fixture.clicks, 1);
});

test('concurrent finalize/release is excluded and evidence-gate rejection can be quarantined', async t => {
  const fixture = await actionFixture(t);
  fixture.after = {...fixture.after, empty_result_visible: false};
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  const finalizing = finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250});
  await assert.rejects(finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250}), /completed or timed-out/);
  await assert.rejects(fixture.release(actionId), /settled prepared action/);
  await assert.rejects(finalizing, /visible empty-result indicator/);
  const released = await fixture.release(actionId, {reason: 'quarantined_foreign_host_session', action_id: actionId});
  assert.equal(released.replay_authorized, false);
  assert.equal(fixture.reads, 1);
  assert.equal(fixture.clicks, 1);
});

test('original module releases a settled selector failure after its tab vanishes without DOM replay', async t => {
  const fixture = await actionFixture(t);
  fixture.after = {...fixture.after, selected_channels: []};
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  await assert.rejects(finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250}),
    /Public input value and selected channel/);
  const reads = fixture.reads;
  fixture.read = async () => { throw new Error('No tab with id: offline-tab.'); };
  const released = await fixture.release(actionId, {browser_tab_exists: false,
    parent_host_authorized: true, action_id: actionId, search_success: false});
  assert.equal(released.status, 'released_terminal');
  assert.equal(released.replay_authorized, false);
  assert.equal(fixture.clicks, 1);
  assert.equal(fixture.reads, reads);
  await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /missing/);
});

test('finalize writer failure preserves existing evidence and allows formal terminal release', async t => {
  const fixture = await actionFixture(t);
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  const evidencePath = path.join(fixture.root, 'worker', `${task.task_id}-evidence.json`);
  await mkdir(path.dirname(evidencePath), {recursive: true});
  const original = JSON.stringify({...task, result_count: 0, visible_detail_links: [], collected_at: '2020-01-01T00:00:00Z'});
  await writeFile(evidencePath, original);
  await assert.rejects(finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250}), {code: 'EEXIST'});
  assert.equal(await readFile(evidencePath, 'utf8'), original);
  await fixture.release(actionId, {browser_evidence_exists: true});
  assert.equal(fixture.clicks, 1);
});

test('terminal release validates durable identity, no-replay gates, path, and ledger version', async t => {
  const fixture = await actionFixture(t);
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  for (const override of [{owner: 'other'}, {task_id: 'other'}, {query: 'other'}, {channel: 'other'},
    {replay_authorized: true}, {logs_stopped: false}, {action_id: 'other'}, {commit_receipt_exists: true},
    {source_claim_sha256: ''}, {original_module_instance_available: false}, {reason: 'pretend_complete'}]) {
    await assert.rejects(fixture.release(actionId, override), /does not prove/);
  }
  const terminalReceipt = await fixture.terminal(actionId);
  await assert.rejects(releasePreparedSearchAction({tab: {}, actionId, terminalReceipt}), /original tab/);
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId,
    terminalReceipt: {...terminalReceipt, ledger: path.join(fixture.root, 'other.json')}}), /matching workflow terminal receipt/);
  await writeFile(terminalReceipt.ledger, JSON.stringify({version: 2, abandonments: fixture.records}));
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId, terminalReceipt}), /does not prove/);
  await writeFile(terminalReceipt.ledger, '{unfinished');
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId, terminalReceipt}), SyntaxError);
  await fixture.release(actionId);
});

test('task-level terminal receipts cannot release another action and generic evidence must hash-bind its ID', async t => {
  const fixture = await actionFixture(t);
  const first = await fixture.prepare();
  const second = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId: first.actionId});
  fixture.click = async () => { throw new Error('fixture click timeout'); };
  await executePreparedSearchAction({tab: fixture.tab, actionId: second.actionId});
  const terminalReceipt = await fixture.terminal(first.actionId);
  const terminalEvidencePath = fixture.evidencePath(first.actionId);
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId: second.actionId, terminalReceipt,
    terminalEvidencePath}), /exact resolved action/);
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId: first.actionId, terminalReceipt}), /action-bound terminal evidence/);
  await writeFile(terminalEvidencePath, '{}');
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId: first.actionId, terminalReceipt,
    terminalEvidencePath}), /hash does not match/);
  await assert.rejects(fixture.release(first.actionId, {}, {action_id: undefined}), /exact resolved action/);
  await assert.rejects(fixture.release(second.actionId, {}, {public_ui_completed: false}), /exact resolved action/);
  await assert.rejects(fixture.release(second.actionId, {public_ui_completed: false}), /does not prove/);
  await fixture.release(first.actionId);
  await fixture.release(second.actionId);
  assert.equal(fixture.clicks, 2);
});

test('terminal file reads reject oversized data and evidence outside the original worker', async t => {
  const fixture = await actionFixture(t);
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  let terminalReceipt = await fixture.terminal(actionId);
  await writeFile(terminalReceipt.ledger, Buffer.alloc(16 * 1024 * 1024 + 1, 32));
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId, terminalReceipt}), /bounded read limit/);
  terminalReceipt = await fixture.terminal(actionId);
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId, terminalReceipt,
    terminalEvidencePath: path.join(fixture.root, 'outside.json')}), /original worker directory/);
  await writeFile(fixture.evidencePath(actionId), Buffer.alloc(1024 * 1024 + 1, 32));
  await assert.rejects(releasePreparedSearchAction({tab: fixture.tab, actionId, terminalReceipt,
    terminalEvidencePath: fixture.evidencePath(actionId)}), /bounded read limit/);
  await fixture.release(actionId);
});

test('existing workflow CLI emits a compatible generic terminal receipt without schema changes', async t => {
  const fixture = await actionFixture(t);
  const {actionId} = await fixture.prepare();
  await executePreparedSearchAction({tab: fixture.tab, actionId});
  const terminalEvidencePath = fixture.evidencePath(actionId);
  // All coordination and claim files are temporary fixtures. Database/network
  // calls remain mocked; no live browser, owner, runtime, or production is used.
  const source = `
import io, json, sys
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock
import workflowctl
import recoveryctl
data = json.load(sys.stdin)
root, task = Path(data['root']), data['task']
work, state = root / 'worker', root / 'state.json'
discovery, books = root / 'title-discovery', root / 'taskbooks'
recoveryctl.PROJECT = root
recoveryctl.STATE = state
recoveryctl.MAIN = root / '.runtime/main-agent/state.json'
recoveryctl.POLICY = root / '.runtime/main-agent/automation-policy.json'
recoveryctl.RECOVERY = root / '.runtime/main-agent/recovery-current.json'
workflowctl.coordctl.init_state(state, task['owner'], str(work))
rows = [{'query': task['query'], 'source': task['channel'], 'category': ''}]
with mock.patch.object(workflowctl.dbctl, 'searches', return_value=rows), mock.patch.object(workflowctl.dbctl, 'sync', side_effect=AssertionError('offline test must not sync')):
    claim = workflowctl.claim_search_task(state, task['owner'], task['channel'], query=task['query'], task_id=task['task_id'], discovery_root=discovery, taskbooks=books)
    work.mkdir(exist_ok=True)
    evidence = {**task, 'version': 1, 'reason': 'quarantined_unverifiable_result', 'claim_sha256': claim['claim_sha256'], 'logs_stopped': True,
        'browser_host': {'session_id': task['owner'], 'terminated': False, 'checked_at': '2026-10-04T00:00:00Z', 'method': 'offline fixture'},
        'browser_tab': {'exists': True, 'checked_at': '2026-10-04T00:00:00Z', 'method': 'offline fixture'},
        'search_action': {'action_id': data['actionId'], 'state': 'completed_unverifiable', 'action_attempts': 1, 'replay_authorized': False, 'original_module_instance_available': True, 'public_ui_completed': True, 'result_error': 'fixture rejected result'},
        'local_state': {'browser_evidence_exists': False, 'commit_receipt_exists': False, 'search_journal_contains_task': False},
        'journals': workflowctl._journal_snapshot(work), 'summary': 'offline lifecycle compatibility test'}
    workflowctl.atomic_json(Path(data['terminalEvidencePath']), evidence)
    output = io.StringIO()
    argv = ['quarantine-search', '--state', str(state), '--thread', task['owner'], '--query', task['query'], '--channel', task['channel'], '--evidence', data['terminalEvidencePath'], '--logs-stopped', '--discovery-root', str(discovery), '--taskbooks', str(books)]
    with mock.patch.object(workflowctl.time, 'sleep'), redirect_stdout(output):
        assert workflowctl.main(argv) == 0
    assert workflowctl.shared_discovery.inspect_claim(discovery, task['query'], task['channel'])['reason'] == 'quarantined_unverifiable_result'
    print(output.getvalue())
`;
  const result = spawnSync('python', ['-X', 'utf8', '-B', '-c', source], {
    cwd: fileURLToPath(new URL('./', import.meta.url)), windowsHide: true, encoding: 'utf8', timeout: 10000,
    input: JSON.stringify({root: fixture.root, task, actionId, terminalEvidencePath}),
  });
  assert.equal(result.status, 0, result.stderr || String(result.error));
  const terminalReceipt = JSON.parse(result.stdout);
  const ledger = JSON.parse(await readFile(terminalReceipt.ledger, 'utf8'));
  assert.equal(ledger.abandonments[0].action_id, undefined);
  assert.equal((await releasePreparedSearchAction({tab: fixture.tab, actionId, terminalReceipt,
    terminalEvidencePath})).status, 'released_terminal');
});

for (const recoveryCase of ['hidden-container', 'missing-tab', 'saved-mismatch', 'nested-saved-mismatch']) {
  test(`retained-action CLI releases the already-loaded v4 module: ${recoveryCase}`, async t => {
    const fixture = await actionFixture(t);
    const nested = recoveryCase === 'nested-saved-mismatch';
    const {actionId} = nested ? await prepareSearchAction({tab: fixture.tab, task,
      workDirectory: path.join(fixture.root, 'worker', 'attempts', 'correction'),
      runtimeRoot: fixture.root, searchOptions, actionSelector: '.channel'}) : await fixture.prepare();
    await executePreparedSearchAction({tab: fixture.tab, actionId});
    fixture.after.empty_result_visible = false;
    fixture.after.result_container_visible = false;
    let finalizationError;
    await assert.rejects(finalizePreparedSearchAction({tab: fixture.tab, actionId, settleDelayMs: 250}), error => {
      finalizationError = error.message;
      return /result container or pagination requires inspection/.test(error.message);
    });
    const terminalEvidencePath = nested ? path.join(fixture.root, 'worker', 'attempts',
      'correction', `quarantine-${actionId}.json`) : fixture.evidencePath(actionId);
    const source = `
import io, hashlib, json, sys
from pathlib import Path
from contextlib import redirect_stdout
from unittest import mock
import workflowctl
import recoveryctl
data = json.load(sys.stdin)
root, task = Path(data['root']), data['task']
work, state = root / 'worker', root / 'state.json'
discovery, books = root / 'title-discovery', root / 'taskbooks'
recoveryctl.PROJECT = root
recoveryctl.STATE = state
recoveryctl.MAIN = root / '.runtime/main-agent/state.json'
recoveryctl.POLICY = root / '.runtime/main-agent/automation-policy.json'
recoveryctl.RECOVERY = root / '.runtime/main-agent/recovery-current.json'
workflowctl.coordctl.init_state(state, task['owner'], str(work))
workflowctl.PROJECT_ROOT = root
workflowctl.atomic_json(root / '.runtime/main-agent/state.json', {
    'host_parent_thread': 'parent-session', 'slots': {'01': {'owner': task['owner'], 'work': str(work)}},
    'fault_repair_authorization': {'authorized': True, 'basis': 'direct_user', 'scope': 'existing_workflow_fault_source_and_tests'}})
with mock.patch.object(workflowctl.dbctl, 'searches', return_value=[{'query': task['query'], 'source': task['channel'], 'category': ''}]), mock.patch.object(workflowctl.dbctl, 'sync', side_effect=AssertionError('must not sync')):
    claim = workflowctl.claim_search_task(state, task['owner'], task['channel'], query=task['query'], task_id=task['task_id'], discovery_root=discovery, taskbooks=books)
    work.mkdir(exist_ok=True)
    missing = data['recoveryCase'] != 'hidden-container'
    tab = {'exists': not missing, 'readable': not missing, 'tab_id': 'offline-tab', 'session_id': 'parent-session', 'owner_thread_id': task['owner'], 'url': 'https://www.xxmanmi.com/', 'checked_at': '2026-10-05T00:00:00Z', 'method': 'offline fixture'}
    if missing: tab.update(missing_error='No tab with id: offline-tab.', inventory_tab_ids=[])
    evidence = {**task, 'version': 1, 'reason': 'quarantined_unverifiable_result', 'claim_sha256': claim['claim_sha256'], 'logs_stopped': True, 'main_authorized': True, 'browser_action_inflight': False,
        'browser_host': {'session_id': 'parent-session', 'thread_id': task['owner'], 'terminated': False, 'tools_terminal': True, 'checked_at': '2026-10-05T00:00:00Z', 'method': 'offline fixture'}, 'browser_tab': tab,
        'search_action': {'action_id': data['actionId'], 'state': 'completed_unverifiable', 'action_attempts': 1, 'replay_authorized': False, 'original_module_instance_available': True, 'execute_returned': True, 'execute_next_action': 'finalize_search_action', 'finalize_returned': True, 'finalize_attempts': 1, 'result_error': data['finalizationError']},
        'dom_summary': None if missing else data['dom'], 'local_state': {'browser_evidence_exists': False, 'commit_receipt_exists': False, 'search_journal_contains_task': False}, 'journals': workflowctl._journal_snapshot(work), 'summary': 'offline backward compatibility test'}
    saved = data['recoveryCase'].endswith('saved-mismatch')
    if saved:
        candidate = {**task, 'query': task['query'] + ' mismatch', 'input_value_after_action': task['query'] + ' mismatch', 'selected_channels': [task['channel']], 'result_count': 1}
        candidate_path = work / (task['task_id'] + '-evidence.json')
        workflowctl.atomic_json(candidate_path, candidate)
        evidence['local_state'].update(browser_evidence_exists=True, saved_candidate_sha256=hashlib.sha256(candidate_path.read_bytes()).hexdigest())
    workflowctl.atomic_json(Path(data['terminalEvidencePath']), evidence)
    output = io.StringIO()
    argv = ['quarantine-search', '--state', str(state), '--thread', task['owner'], '--query', task['query'], '--channel', task['channel'], '--evidence', data['terminalEvidencePath'], '--logs-stopped', '--authorized-retained-action', '--discovery-root', str(discovery), '--taskbooks', str(books)]
    if saved: argv.append('--allow-saved-candidate')
    with mock.patch.object(workflowctl.time, 'sleep'), redirect_stdout(output):
        assert workflowctl.main(argv) == 0
    print(output.getvalue())
`;
    const result = spawnSync('python', ['-X', 'utf8', '-B', '-c', source], {
      cwd: fileURLToPath(new URL('./', import.meta.url)), windowsHide: true,
      encoding: 'utf8', timeout: 10000,
      input: JSON.stringify({root: fixture.root, task, actionId, terminalEvidencePath,
        recoveryCase, dom: fixture.after, finalizationError}),
    });
    assert.equal(result.status, 0, result.stderr || result.stdout || String(result.error));
    const terminalReceipt = JSON.parse(result.stdout);
    const ledger = JSON.parse(await readFile(terminalReceipt.ledger, 'utf8'));
    assert.equal(ledger.abandonments[0].action_id, actionId);
    assert.equal(ledger.abandonments[0].search_success, false);
    assert.equal((await releasePreparedSearchAction({tab: fixture.tab, actionId,
      terminalReceipt, terminalEvidencePath})).status, 'released_terminal');
    assert.equal(fixture.clicks, 1);
    await assert.rejects(executePreparedSearchAction({tab: fixture.tab, actionId}), /missing/);
  });
}

function observationFixture({interval = true, timeout = true, observer = true} = {}) {
  let now = 0;
  let nextId = 0;
  const timers = new Map();
  const intervals = new Map();
  const observers = new Set();
  const fixture = {timers, intervals, observers, reads: 0, failRead: false, failObserve: false, loading: false};
  const node = text => ({textContent: text, getClientRects: () => [1], closest: () => null,
    hasAttribute: () => false, getAttribute: () => null});
  const list = {...node(''), querySelectorAll: () => []};
  const window = {};
  if (timeout) {
    window.setTimeout = (callback, delay) => { const id = ++nextId; timers.set(id, {callback, at: now + delay}); return id; };
    window.clearTimeout = id => timers.delete(id);
  }
  if (interval) {
    window.setInterval = callback => { const id = ++nextId; intervals.set(id, callback); return id; };
    window.clearInterval = id => intervals.delete(id);
  }
  if (observer) window.MutationObserver = class {
    constructor(callback) { this.callback = callback; }
    observe() { observers.add(this); if (fixture.failObserve) throw new Error('fixture observe failed'); }
    disconnect() { observers.delete(this); }
  };
  const context = vm.createContext({window, Math, Date: class extends Date { static now() { return now; } },
    location: {href: 'https://www.xxmanmi.com/'}, document: {
      documentElement: {},
      querySelector: selector => {
        fixture.reads += 1;
        if (fixture.failRead) throw new Error('fixture DOM read failed');
        return selector === '#searchList' ? list : selector === '#searchInput' ? {value: task.query} : null;
      },
      querySelectorAll: selector => selector === '.selected' ? [node(task.channel)] :
        selector === '.empty' ? [node('暂无数据')] : selector === '.loading' && fixture.loading ? [node('加载中')] : [],
    }});
  fixture.context = context;
  fixture.window = window;
  fixture.begin = (args = {}) => {
    context.args = {...searchOptions, timeoutMs: 1000, ...args};
    return vm.runInContext(`(${beginSearchDomObservation.toString()})(args)`, context);
  };
  fixture.finish = observationId => {
    context.args = {observationId};
    return vm.runInContext(`(${finishSearchDomObservation.toString()})(args)`, context);
  };
  fixture.advance = value => {
    now += value;
    for (const [id, timer] of [...timers]) {
      if (timer.at <= now && timers.delete(id)) timer.callback();
    }
    for (const callback of [...intervals.values()]) callback();
  };
  fixture.assertClean = () => {
    assert.equal(timers.size, 0);
    assert.equal(intervals.size, 0);
    assert.equal(observers.size, 0);
  };
  return fixture;
}

test('legacy observer deadline releases handles with interval-only and timeout-only hosts', () => {
  for (const options of [{}, {timeout: false}, {interval: false}, {observer: false}]) {
    const fixture = observationFixture(options);
    const {observationId} = fixture.begin();
    fixture.advance(1001);
    fixture.assertClean();
    assert.equal(fixture.finish(observationId).timedOut, true);
    fixture.assertClean();
    assert.equal(fixture.window.__xxmmSearchDomObservationV1__, undefined);
  }
});

test('legacy finish DOM exceptions always disconnect observers and clear timers', () => {
  const fixture = observationFixture();
  const {observationId} = fixture.begin();
  fixture.failRead = true;
  assert.throws(() => fixture.finish(observationId), /DOM read failed/);
  fixture.assertClean();
  assert.equal(fixture.window.__xxmmSearchDomObservationV1__, undefined);
});

test('legacy sampling exceptions stop callbacks and remain explicit at finish', () => {
  for (const options of [{}, {interval: false}]) {
    const fixture = observationFixture(options);
    const {observationId} = fixture.begin();
    fixture.failRead = true;
    fixture.advance(20);
    fixture.assertClean();
    fixture.failRead = false;
    assert.throws(() => fixture.finish(observationId), /DOM read failed/);
    fixture.assertClean();
  }
});

test('legacy installation failure and nonextensible window do not strand installed handles', () => {
  for (const mode of ['observe', 'timer', 'window']) {
    const fixture = observationFixture();
    if (mode === 'observe') fixture.failObserve = true;
    if (mode === 'timer') fixture.window.setInterval = () => { throw new Error('fixture timer failed'); };
    if (mode === 'window') Object.preventExtensions(fixture.window);
    assert.throws(() => fixture.begin());
    fixture.assertClean();
  }
});

test('legacy replacement stays bounded and stale observer callbacks cannot sample again', () => {
  const fixture = observationFixture();
  let armed;
  for (let index = 0; index < 64; index += 1) {
    const previous = [...fixture.observers][0];
    armed = fixture.begin();
    const reads = fixture.reads;
    previous?.callback();
    assert.equal(fixture.reads, reads);
    assert.equal(fixture.intervals.size, 1);
    assert.equal(fixture.timers.size, 1);
    assert.equal(fixture.observers.size, 1);
  }
  fixture.finish(armed.observationId);
  fixture.assertClean();
});

test('early observer rejection is handled during arm delay and prevents a click', async () => {
  let clicks = 0;
  await assert.rejects(observeSearchAction({tab: {playwright: {
    evaluate: async () => { throw new Error('fixture cannot arm'); },
  }}, searchOptions, armDelayMs: 5, triggerAction: async () => { clicks += 1; }}), /cannot arm/);
  assert.equal(clicks, 0);
});

test('trigger failure keeps a handler attached to a later observer rejection', async () => {
  const observing = deferred();
  await assert.rejects(observeSearchAction({tab: {playwright: {evaluate: () => observing.promise}},
    searchOptions, armDelayMs: 0, triggerAction: async () => { throw new Error('fixture trigger failed'); }}), /trigger failed/);
  observing.reject(new Error('fixture late observer rejection'));
  await new Promise(resolve => setImmediate(resolve));
});
