import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {mkdtemp, mkdir, link, symlink, readFile, writeFile, rm} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {captureDetail, readDetailDom, runDetailBatch, runDetailBatchSegment} from './detail-capture.mjs';
import {appendObservation, withCaptureLock} from './capture-journal.mjs';
import {SEARCH_EVIDENCE_API_VERSION, beginSearchDomObservation, buildSearchEvidence, finishSearchDomObservation,
  executePreparedSearchAction, finalizePreparedSearchAction, finalizeTimedOutSearchObservation,
  observeSearchAction, observeSearchDomDuringAction, prepareSearchAction, readSearchActionDom,
  readSearchDom, readSearchPreparationDom, runSearchActionEvidence, saveSearchEvidence} from './search-evidence.mjs';

test('search evidence API exposes the cache-reload version', () => {
  assert.equal(SEARCH_EVIDENCE_API_VERSION, 4);
});

const item = {url: 'https://www.xxmanmi.com/video2/123.html', title: '节目', source: '小小漫迷'};
function browserFixture(count, mutate = value => value) {
  const calls = [];
  const entries = Array.from({length: count}, (_, index) => ({
    index, label: `第${index + 1}集`, group: index % 2 ? 'playList1' : 'playList0',
    dataWeb: `https://cdn.example.invalid/${index}.m3u8`,
  }));
  return {calls, entries, readDom: async (_tab, fn, args) => {
    assert.equal(fn, readDetailDom);
    calls.push(args);
    const header = {url: item.url, title: item.title, episodeDomCount: count, fingerprint: JSON.stringify(entries)};
    return mutate(args.start === undefined ? {...header,
      groupCounts: Object.fromEntries([...new Set(entries.map(entry => entry.group))]
        .map(group => [group, entries.filter(entry => entry.group === group).length])),
      lineLabels: [{for: '0', text: '量子'}, {for: '1', text: '下载'}],
    } : {...header, start: args.start, entries: entries.slice(args.start, args.start + args.limit)}, args);
  }};
}

test('2548 DOM entries use separate bounded evaluations and persist every original entry once', async () => {
  const fixture = browserFixture(2548);
  const writes = [];
  const result = await captureDetail({}, item, 'C:/fixture/worker', {...fixture, alreadyLoaded: true,
    writeObservation: async (...args) => writes.push(args)});
  assert.deepEqual(fixture.calls.map(call => call.start), [undefined, 0, 1000, 2000]);
  assert.equal(writes[0][0], 'details');
  assert.equal(writes[0][1].episodes.length, 2548);
  assert.equal(writes[0][1].episodes[2547].url, fixture.entries[2547].dataWeb);
  assert.equal(writes[0][1].episodes[1].line, '下载');
  assert.equal(result.mediaCount, 2548);
  assert.doesNotMatch(JSON.stringify(result), /cdn|m3u8/);
});

test('zero verification countdown and missing href never hide actual data-web resources', async () => {
  const episode = {textContent: '第01集', closest: () => ({className: 'playList0'}),
    getAttribute: name => name === 'data-web' ? 'https://cdn.example.invalid/one.m3u8' : null};
  const line = {textContent: '量子', querySelector: () => null,
    getAttribute: name => name === 'for' ? '0' : null};
  const context = {location: {href: item.url}, document: {
    body: {innerText: '更新验证码 验证码期限：0天00小时00分钟 节目 第01集'},
    querySelector: selector => selector === '#video' ? {textContent: item.title} : null,
    querySelectorAll: selector => selector === '#list a[data-web]' ? [episode]
      : selector === 'label[for]' ? [line] : [],
  }};
  const tab = {id: 'data-web-without-href', playwright: {
    evaluate: async (fn, args) => vm.runInNewContext(`(${fn.toString()})(args)`, {...context, args}),
  }};
  const observations = [];
  const result = await runDetailBatch({tab, items: [item], workDirectory: 'C:/fixture/worker',
    alreadyLoadedFirst: true}, {
    writeObservation: async (kind, value) => observations.push({kind, value}),
    writeIssue: async () => assert.fail('normal public resource must not become a challenge'),
  });
  assert.equal(result.captured, 1);
  assert.equal(result.failed, 0);
  assert.equal(observations[0].value.episodeDomCount, 1);
  assert.equal(observations[0].value.episodes[0].url, 'https://cdn.example.invalid/one.m3u8');
});

test('host segments preserve a four-page batch without rereading or renavigating saved pages', async () => {
  const items = Array.from({length: 4}, (_, index) => ({...item,
    url: `https://www.xxmanmi.com/video2/${120 + index}.html`}));
  const fixture = browserFixture(2);
  const visits = [];
  const writes = [];
  let current;
  let lane = {tab: {id: 'segmented'}, items, workDirectory: 'C:/fixture/worker'};
  const options = {navigate: async (_tab, url) => {
    visits.push(url); current = items.find(value => value.url === url);
  }, readDom: async (...args) => ({...await fixture.readDom(...args), url: current.url}),
  writeObservation: async (kind, value) => writes.push({kind, value}),
  writeIssue: async () => assert.fail('all four pages should be valid')};
  const actions = [];
  while (lane.items.length) {
    const result = await runDetailBatchSegment(lane, options);
    assert.equal(result.captured, 1);
    assert.equal(result.attempted, 1);
    assert.equal(result.skipped, 0);
    actions.push(result.next_action);
    lane = {...lane, items: result.remainingItems, alreadyLoadedFirst: false};
  }
  assert.deepEqual(visits, items.map(value => value.url));
  assert.deepEqual(writes.map(value => value.value.url), visits);
  assert.equal(fixture.calls.length, 8);
  assert.equal(items.length, 4);
  assert.deepEqual(actions, ['capture_detail_segment', 'capture_detail_segment',
    'capture_detail_segment', 'complete_detail']);
});

test('a failed segment stops its lane and does not navigate deferred pages', async () => {
  const items = [item, {...item, url: 'https://www.xxmanmi.com/video2/124.html'}];
  let navigations = 0;
  const result = await runDetailBatchSegment({tab: {id: 'failed-segment'}, items,
    workDirectory: 'C:/fixture/worker'}, {
    navigate: async () => { navigations++; throw new Error('No tab with id: failed-segment'); },
    writeIssue: async () => {},
  });
  assert.equal(result.failed, 1);
  assert.equal(result.journaled, 0);
  assert.equal(result.next_action, 'inspect_detail_failure');
  assert.equal(result.deferred, 1);
  assert.equal(navigations, 1);
  assert.equal(result.remainingItems[0].url, items[1].url);
});

test('serializable DOM reader has no outer lexical dependencies and emits actual indexes', () => {
  const nodes = [0, 1].map(index => ({textContent: `集${index}`, closest: () => ({className: 'playList0'}),
    getAttribute: () => `https://cdn.invalid/${index}.mp4`}));
  const context = {location: {href: item.url}, document: {
    querySelector: () => ({textContent: '节目'}),
    querySelectorAll: selector => selector === 'episodes' ? nodes : [{getAttribute: () => '0',
      querySelector: () => null, textContent: '线路'}],
  }, args: {selectors: {title: 'title', episodes: 'episodes', groups: 'group', lines: 'lines'}, start: 1, limit: 1}};
  const result = vm.runInNewContext(`(${readDetailDom.toString()})(args)`, context);
  assert.equal(result.episodeDomCount, 2);
  assert.equal(result.entries[0].index, 1);
  nodes[0].getAttribute = () => 'https://cdn.invalid/replaced.mp4';
  const changed = vm.runInNewContext(`(${readDetailDom.toString()})(args)`, context);
  assert.notEqual(result.fingerprint, changed.fingerprint, 'a change outside this segment still invalidates its full DOM fingerprint');
});

test('truncation, URL drift, title drift, wrong indexes and group drift never write', async () => {
  for (const mutation of [
    (value, args) => args.start === undefined ? value : {...value, entries: value.entries.slice(0, -1)},
    value => ({...value, url: 'https://www.xxmanmi.com/video2/999.html'}),
    value => ({...value, title: '其他节目'}),
    (value, args) => args.start === undefined ? value : {...value, entries: value.entries.map(entry => ({...entry, index: 99}))},
    (value, args) => args.start === undefined ? {...value, groupCounts: {unknown: 3}} : value,
    (value, args) => args.start === undefined ? value : {...value, fingerprint: 'same-count-replacement'},
  ]) {
    await assert.rejects(captureDetail({}, item, 'worker', {alreadyLoaded: true, ...browserFixture(3, mutation),
      writeObservation: async () => assert.fail('must not persist incomplete DOM')}));
  }
});

test('zero-width spaces around the actual DOM title do not reject the assigned title', async () => {
  const fixture = browserFixture(1, value => ({...value, title: '\u200B节目\u200B'}));
  let saved;
  const result = await captureDetail({}, item, 'worker', {...fixture, alreadyLoaded: true,
    writeObservation: async (_kind, record) => { saved = record; }});
  assert.equal(result.outcome, 'captured');
  assert.equal(saved.title, '节目');
  await assert.rejects(captureDetail({}, {...item, title: '其他节目'}, 'worker',
    {...fixture, alreadyLoaded: true, writeObservation: async () => assert.fail('wrong title')}),
  /DOM program title does not match/);
});

test('literal numeric HTML character references in a DOM title preserve the assigned title check', async () => {
  const assigned = {...item, title: "Let's Go怪奇组"};
  const fixture = browserFixture(12, value => ({...value, title: 'Let&#039;s Go怪奇组'}));
  let saved;
  const result = await captureDetail({}, assigned, 'worker', {...fixture, alreadyLoaded: true,
    writeObservation: async (_kind, record) => { saved = record; }});
  assert.equal(result.outcome, 'captured');
  assert.equal(saved.title, assigned.title);
  assert.equal(saved.episodeDomCount, 12);
  await assert.rejects(captureDetail({}, {...assigned, title: "Let's Go别的组"}, 'worker',
    {...fixture, alreadyLoaded: true, writeObservation: async () => assert.fail('wrong title')}),
  /DOM program title does not match/);
});

test('numeric or mixed unresolved IDs stay raw, while explicit placeholders preserve completeness', async () => {
  for (const raw of ['000123', 'https://cdn.example.invalid/opaque', 'undefined']) {
    const fixture = browserFixture(2);
    fixture.entries[1].dataWeb = raw;
    let saved;
    const result = await captureDetail({}, item, 'worker', {...fixture, alreadyLoaded: true,
      writeObservation: async (kind, record) => { saved = {kind, record}; }});
    if (raw === 'undefined') {
      assert.equal(result.outcome, 'captured');
      assert.equal(saved.record.invalidDomEntries[0].dataWeb, raw);
      assert.equal(saved.record.episodes.length + saved.record.invalidDomEntries.length, 2);
    } else {
      assert.equal(result.outcome, 'raw');
      assert.equal(saved.kind, 'raw-details');
      assert.equal(saved.record.rawEntries[1].dataWeb, raw);
      assert.equal(saved.record.episodes, undefined);
    }
  }
});

test('single-lane batch stops after durable failure and preserves compact outcome counts', async () => {
  const fixture = browserFixture(1);
  let issueWrites = 0;
  const summary = await runDetailBatch({tab: {id: 'detail-test'}, items: [item, {...item, url: item.url.replace('123', '124')}],
    workDirectory: 'C:/fixture/worker', alreadyLoadedFirst: true}, {...fixture,
    navigate: async () => { throw new Error('permission blocked'); },
    writeObservation: async () => {}, writeIssue: async () => { issueWrites += 1; }});
  assert.equal(summary.captured, 1);
  assert.equal(summary.failed, 1);
  assert.equal(issueWrites, 1);
  await assert.rejects(runDetailBatch([]), /exactly one/);
});

function recoveryBatchFixture({transient = true, staysBroken = false, restricted = false} = {}) {
  const ordinary = browserFixture(1);
  let broken = transient;
  let diagnosticReads = 0;
  let navigationCount = 0;
  let detailReads = 0;
  let writes = 0;
  const issues = [];
  const tab = {id: 'recovery-batch-fixture',
    goto: async () => { navigationCount += 1; broken = staysBroken; },
    playwright: {evaluate: async (fn, args) => {
      if (fn === readDetailDom) {
        detailReads += 1;
        if (broken) return {url: item.url, title: '获取失败，请刷新页面', episodeDomCount: 0};
        return ordinary.readDom(tab, fn, args);
      }
      diagnosticReads += 1;
      return {url: item.url, title: '获取失败，请刷新页面', episodeDomCount: 0,
        visibleText: restricted ? '请先登录' : '获取失败，请刷新页面', visibleTextTruncated: false};
    }},
  };
  return {
    run: (options = {}) => runDetailBatch({tab, items: [item], workDirectory: 'C:/fixture/worker', alreadyLoadedFirst: true}, {
      writeObservation: async () => { writes += 1; }, writeIssue: async issue => { issues.push(issue); }, ...options,
    }),
    counts: () => ({navigationCount, diagnosticReads, detailReads, writes}), issues,
  };
}

test('default detail batch preserves single transient recovery and records it before navigation', async () => {
  const fixture = recoveryBatchFixture();
  const summary = await fixture.run();
  assert.equal(summary.recoveryMode, 'single-transient');
  assert.equal(summary.captured, 1);
  assert.deepEqual(fixture.counts(), {navigationCount: 1, diagnosticReads: 1, detailReads: 3, writes: 1});
  assert.deepEqual(fixture.issues.map(issue => issue.state), ['retrying', 'recovered']);
});

test('normal default capture has no recovery DOM overhead; failed retry never runs twice', async () => {
  const healthy = recoveryBatchFixture({transient: false});
  assert.equal((await healthy.run()).captured, 1);
  assert.deepEqual(healthy.counts(), {navigationCount: 0, diagnosticReads: 0, detailReads: 2, writes: 1});
  const broken = recoveryBatchFixture({staysBroken: true});
  assert.equal((await broken.run()).failed, 1);
  assert.deepEqual(broken.counts(), {navigationCount: 1, diagnosticReads: 1, detailReads: 2, writes: 0});
  const restricted = recoveryBatchFixture({restricted: true});
  assert.equal((await restricted.run()).failed, 1);
  assert.equal(restricted.counts().navigationCount, 0);
});

test('custom DOM/browser adapters and explicit opt-out do not call the default recovery inspector', async () => {
  for (const options of [
    {readDom: async () => ({url: item.url, title: '获取失败，请刷新页面', episodeDomCount: 0})},
    {navigate: async () => assert.fail('already-loaded item must not navigate')},
    {selectors: {title: '#custom', episodes: '#list a[data-web]', groups: '[class*="playList"]', lines: 'label[for]'}},
    {recoverTransient: false},
  ]) {
    const fixture = recoveryBatchFixture();
    const summary = await fixture.run(options);
    assert.equal(summary.failed, 1);
    assert.equal(fixture.counts().diagnosticReads, 0);
    assert.equal(fixture.counts().navigationCount, 0);
    assert.notEqual(summary.recoveryMode, 'single-transient');
  }
});

test('journal writer preserves unfinished tails, refuses root escape and respects unknown locks', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-helper-test-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const work = path.join(root, 'worker');
  const record = {collectedAt: new Date().toISOString(), error: 'fixture'};
  await appendObservation('navigation-issues', record, work, {runtimeRoot: root});
  const journal = path.join(work, 'navigation-issues.ndjson');
  assert.deepEqual(JSON.parse((await readFile(journal, 'utf8')).trim()), record);
  await writeFile(journal, '{unfinished', 'utf8');
  await assert.rejects(appendObservation('navigation-issues', record, work, {runtimeRoot: root}), /unfinished/);
  assert.equal(await readFile(journal, 'utf8'), '{unfinished');
  await assert.rejects(appendObservation('navigation-issues', record, root, {runtimeRoot: root}), /strictly inside/);
  await writeFile(path.join(work, '.capture-helper.lock'), 'unknown writer');
  await assert.rejects(appendObservation('navigation-issues', record, work, {runtimeRoot: root}), /EEXIST/);
  assert.equal(await readFile(path.join(work, '.capture-helper.lock'), 'utf8'), 'unknown writer');
});

test('writer rejects linked directories and hard-linked journals without changing their targets', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-links-test-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const target = path.join(root, 'target');
  await mkdir(target);
  const linked = path.join(root, 'linked');
  await symlink(target, linked, process.platform === 'win32' ? 'junction' : 'dir');
  const record = {collectedAt: new Date().toISOString(), error: 'fixture'};
  await assert.rejects(appendObservation('navigation-issues', record, linked, {runtimeRoot: root}), /Unsafe worker/);
  const original = path.join(root, 'original.ndjson');
  await writeFile(original, '{}\n');
  await link(original, path.join(target, 'navigation-issues.ndjson'));
  await assert.rejects(appendObservation('navigation-issues', record, target, {runtimeRoot: root}), /Unsafe capture journal/);
  assert.equal(await readFile(original, 'utf8'), '{}\n');
});

test('own lock is released when initial write or sync fails; replaced locks remain untouched', async () => {
  for (const stage of ['writeFile', 'sync']) {
    let removed = 0;
    let closed = 0;
    const failure = new Error(`fixture ${stage} failure`);
    const lock = {stat: async () => ({dev: 1, ino: 2}), writeFile: async () => {}, sync: async () => {},
      close: async () => { closed += 1; }};
    lock[stage] = async () => { throw failure; };
    const io = {open: async () => lock, lstat: async () => ({dev: 1, ino: 2, isSymbolicLink: () => false}),
      unlink: async () => { removed += 1; }};
    await assert.rejects(withCaptureLock('fixture', async () => assert.fail('journal must not run'), io), error => error === failure);
    assert.equal(removed, 1);
    assert.equal(closed, 1);
    io.lstat = async () => ({dev: 1, ino: 9, isSymbolicLink: () => false});
    await assert.rejects(withCaptureLock('fixture', async () => {}, io), /lock changed/);
    assert.equal(removed, 1);
  }
});

const task = {task_id: 'search-offline', owner: 'fixture', query: '节目', channel: '量子', category: ''};
const snapshot = {page_url: 'https://www.xxmanmi.com/', input_value_after_action: '节目', selected_channels: ['量子'], result_count: 1,
  visible_detail_links: [{url: item.url, title: '节目', status: 'HD'}],
  result_container_visible: true, pagination_pending: false, empty_result_visible: false};
const before = {...snapshot, visible_detail_links: [], result_count: 0};

test('search evidence checks exact input/channel/count, fresh results, zero-state and pagination', () => {
  assert.equal(buildSearchEvidence(task, snapshot, {before}).result_count, 1);
  for (const overrides of [
    {input_value_after_action: '旧词'}, {selected_channels: ['非凡']}, {result_count: 2},
    {pagination_pending: true}, {result_container_visible: false},
    {page_url: 'https://elsewhere.invalid/'},
    {result_count: 0, visible_detail_links: []},
  ]) assert.throws(() => buildSearchEvidence(task, {...snapshot, ...overrides}, {before}));
  assert.throws(() => buildSearchEvidence(task, snapshot, {before: snapshot}), /visibly change/);
  assert.equal(buildSearchEvidence(task, {...snapshot, result_count: 0, visible_detail_links: [], empty_result_visible: true},
    {before: snapshot}).result_count, 0);
});

test('observed loading-to-done accepts identical results, including consecutive zero results', () => {
  for (const value of [snapshot, {...snapshot, result_count: 0, visible_detail_links: [], empty_result_visible: true}]) {
    const before = {...value, observed_at: '2026-09-28T00:00:00Z'};
    const loadingSnapshot = {...value, observed_at: '2026-09-28T00:00:01Z',
      loading_indicator: {selector: '#observed-loading', visible_count: 1, visible_texts: ['加载中']}};
    const after = {...value, observed_at: '2026-09-28T00:00:02Z',
      loading_indicator: {selector: '#observed-loading', visible_count: 0, visible_texts: []}};
    const evidence = buildSearchEvidence(task, after, {before, loadingSnapshot});
    assert.equal(evidence.result_count, value.result_count);
    assert.equal(evidence.refresh_evidence.kind, 'observed_loading_transition');
    assert.throws(() => buildSearchEvidence(task, after, {before, loadingSnapshot: true}), /visibly change/);
    assert.throws(() => buildSearchEvidence(task, after, {before, loadingSnapshot: {...loadingSnapshot,
      input_value_after_action: '旧词'}}), /visibly change/);
    assert.throws(() => buildSearchEvidence(task, {...after, loading_indicator: {
      selector: '#different-loader', visible_count: 0, visible_texts: []}}, {before, loadingSnapshot}), /visibly change/);
    assert.throws(() => buildSearchEvidence(task, after, {before, loadingSnapshot: {...loadingSnapshot,
      observed_at: '2026-09-28T00:00:03Z'}}), /visibly change/);
  }
});

test('in-page search observer captures a short loading transition between host calls', () => {
  let phase = 'before';
  let observerCallback;
  let intervalCallback;
  const visibleNode = text => ({textContent: text, getClientRects: () => [1], closest: () => null,
    hasAttribute: () => false, getAttribute: () => null});
  const list = {...visibleNode(''), querySelectorAll: () => [], querySelector: () => null};
  const selected = visibleNode(task.channel);
  const empty = visibleNode('暂无数据');
  const loading = visibleNode('加载中');
  const context = vm.createContext({
    location: {href: 'https://www.xxmanmi.com/'},
    document: {
      documentElement: {},
      querySelector: selector => selector === '#searchList' ? list :
        selector === '#searchInput' ? {value: task.query} : null,
      querySelectorAll: selector => {
        if (selector === '.selected') return [selected];
        if (selector === '.empty') return phase === 'loading' ? [] : [empty];
        if (selector === '.loading') return phase === 'loading' ? [loading] : [];
        return [];
      },
    },
    MutationObserver: class {
      constructor(callback) { observerCallback = callback; }
      observe() {}
      disconnect() {}
    },
    window: {
      MutationObserver: class {
        constructor(callback) { observerCallback = callback; }
        observe() {}
        disconnect() {}
      },
      setInterval(callback) { intervalCallback = callback; return 7; },
      clearInterval() {},
    },
    Math, Date,
  });
  context.args = {selectedChannelSelector: '.selected', emptyResultSelector: '.empty',
    loadingSelector: '.loading'};
  const armed = vm.runInContext(`(${beginSearchDomObservation.toString()})(args)`, context);
  phase = 'loading';
  observerCallback();
  intervalCallback();
  phase = 'after';
  context.doneArgs = {observationId: armed.observationId};
  const observed = vm.runInContext(`(${finishSearchDomObservation.toString()})(doneArgs)`, context);
  const evidence = buildSearchEvidence(task, observed.after, observed);
  assert.equal(evidence.result_count, 0);
  assert.equal(evidence.refresh_evidence.kind, 'observed_loading_transition');
  assert.equal(observed.loadingSnapshot.loading_indicator.visible_count, 1);
});

test('search observer falls back to interval sampling when MutationObserver is unavailable', () => {
  let phase = 'before';
  let intervalCallback;
  const visibleNode = text => ({textContent: text, getClientRects: () => [1], closest: () => null,
    hasAttribute: () => false, getAttribute: () => null});
  const list = {...visibleNode(''), querySelectorAll: () => [], querySelector: () => null};
  const context = vm.createContext({
    location: {href: 'https://www.xxmanmi.com/'},
    document: {
      documentElement: {},
      querySelector: selector => selector === '#searchList' ? list :
        selector === '#searchInput' ? {value: task.query} : null,
      querySelectorAll: selector => selector === '.selected' ? [visibleNode(task.channel)] :
        selector === '.empty' ? (phase === 'loading' ? [] : [visibleNode('暂无数据')]) :
        selector === '.loading' ? (phase === 'loading' ? [visibleNode('加载中')] : []) : [],
    },
    window: {
      setInterval(callback) { intervalCallback = callback; return 9; },
      clearInterval() {},
    },
    Math, Date,
  });
  context.args = {selectedChannelSelector: '.selected', emptyResultSelector: '.empty',
    loadingSelector: '.loading'};
  const armed = vm.runInContext(`(${beginSearchDomObservation.toString()})(args)`, context);
  phase = 'loading';
  intervalCallback();
  phase = 'after';
  context.doneArgs = {observationId: armed.observationId};
  const observed = vm.runInContext(`(${finishSearchDomObservation.toString()})(doneArgs)`, context);
  assert.equal(buildSearchEvidence(task, observed.after, observed).refresh_evidence.kind,
    'observed_loading_transition');
});

test('search observer uses recursive timeout sampling in the restricted browser context', () => {
  let phase = 'before';
  let timeoutCallback;
  const visibleNode = text => ({textContent: text, getClientRects: () => [1], closest: () => null,
    hasAttribute: () => false, getAttribute: () => null});
  const list = {...visibleNode(''), querySelectorAll: () => [], querySelector: () => null};
  const context = vm.createContext({
    location: {href: 'https://www.xxmanmi.com/'},
    document: {
      documentElement: {},
      querySelector: selector => selector === '#searchList' ? list :
        selector === '#searchInput' ? {value: task.query} : null,
      querySelectorAll: selector => selector === '.selected' ? [visibleNode(task.channel)] :
        selector === '.empty' ? (phase === 'loading' ? [] : [visibleNode('暂无数据')]) :
        selector === '.loading' ? (phase === 'loading' ? [visibleNode('加载中')] : []) : [],
    },
    window: {
      setTimeout(callback) { timeoutCallback = callback; return 11; },
      clearTimeout() {},
    },
    Math, Date,
  });
  context.args = {selectedChannelSelector: '.selected', emptyResultSelector: '.empty',
    loadingSelector: '.loading'};
  const armed = vm.runInContext(`(${beginSearchDomObservation.toString()})(args)`, context);
  phase = 'loading';
  timeoutCallback();
  phase = 'after';
  context.doneArgs = {observationId: armed.observationId};
  const observed = vm.runInContext(`(${finishSearchDomObservation.toString()})(doneArgs)`, context);
  assert.equal(buildSearchEvidence(task, observed.after, observed).refresh_evidence.kind,
    'observed_loading_transition');
});

test('single pending search observation keeps state without extending window', async () => {
  let phase = 'before';
  const timers = [];
  const visibleNode = text => ({textContent: text, getClientRects: () => [1], closest: () => null,
    hasAttribute: () => false, getAttribute: () => null});
  const list = {...visibleNode(''), querySelectorAll: () => [], querySelector: () => null};
  const window = Object.preventExtensions({setTimeout(callback) { timers.push(callback); }});
  const context = vm.createContext({
    location: {href: 'https://www.xxmanmi.com/'}, window, Math, Date,
    document: {
      querySelector: selector => selector === '#searchList' ? list :
        selector === '#searchInput' ? {value: task.query} : null,
      querySelectorAll: selector => selector === '.selected' ? [visibleNode(task.channel)] :
        selector === '.empty' ? (phase === 'loading' ? [] : [visibleNode('暂无数据')]) :
        selector === '.loading' ? (phase === 'loading' ? [visibleNode('加载中')] : []) : [],
    },
  });
  context.args = {selectedChannelSelector: '.selected', emptyResultSelector: '.empty',
    loadingSelector: '.loading', timeoutMs: 1000};
  const pending = vm.runInContext(`(${observeSearchDomDuringAction.toString()})(args)`, context);
  phase = 'loading';
  timers.shift()();
  phase = 'after';
  timers.shift()();
  const observed = await pending;
  assert.equal(buildSearchEvidence(task, observed.after, observed).refresh_evidence.kind,
    'observed_loading_transition');
  assert.equal(Object.prototype.hasOwnProperty.call(window, '__xxmmSearchDomObservationV1__'), false);
});

test('single pending observation waits for an identical zero-result empty-state cycle', async () => {
  let phase = 'before';
  const timers = [];
  const visibleNode = text => ({textContent: text, getClientRects: () => [1], closest: () => null,
    hasAttribute: () => false, getAttribute: () => null});
  const list = {...visibleNode(''), querySelectorAll: () => [], querySelector: () => null};
  const context = vm.createContext({
    location: {href: 'https://www.xxmanmi.com/'}, Math, Date,
    window: Object.preventExtensions({setTimeout(callback) { timers.push(callback); }}),
    document: {
      querySelector: selector => selector === '#searchList' ? list :
        selector === '#searchInput' ? {value: task.query} : null,
      querySelectorAll: selector => selector === '.selected' ? [visibleNode(task.channel)] :
        selector === '.empty' ? (phase === 'between' ? [] : [visibleNode('暂无数据')]) : [],
    },
  });
  context.args = {selectedChannelSelector: '.selected', emptyResultSelector: '.empty',
    loadingSelector: '.loading', timeoutMs: 1000};
  const pending = vm.runInContext(`(${observeSearchDomDuringAction.toString()})(args)`, context);
  phase = 'between';
  timers.shift()();
  assert.equal(timers.length, 1, 'intermediate missing empty state must not finish observation');
  phase = 'after';
  timers.shift()();
  const observed = await pending;
  assert.equal(observed.loadingSnapshot, null);
  assert.equal(observed.transitionSnapshot.empty_result_visible, false);
  assert.equal(buildSearchEvidence(task, observed.after, observed).refresh_evidence.kind,
    'observed_result_state_transition');
});

test('host search orchestrator arms observation before invoking the normal UI action', async () => {
  const calls = [];
  const observed = {before, loadingSnapshot: null, after: snapshot, timedOut: false};
  const tab = {playwright: {evaluate: async (reader, args) => {
    calls.push(['observe', reader, args]);
    return observed;
  }}};
  const result = await observeSearchAction({tab, searchOptions: {loadingSelector: '.loading'},
    triggerAction: async () => calls.push(['click']), armDelayMs: 0, observationTimeoutMs: 1234});
  assert.equal(calls[0][0], 'observe');
  assert.equal(calls[0][1], observeSearchDomDuringAction);
  assert.equal(calls[0][2].timeoutMs, 1234);
  assert.equal(calls[1][0], 'click');
  assert.equal(result, observed);
});

test('timed-out short observation can only be finalized by a later real DOM snapshot', () => {
  const observationId = 'short-window';
  const observed = {
    timedOut: true,
    before: {...snapshot, observed_at: '2026-09-28T00:00:00Z', observation_id: observationId,
      observation_phase: 'before'},
    transitionSnapshot: {...snapshot, observed_at: '2026-09-28T00:00:01Z', observation_id: observationId,
      observation_phase: 'transition', result_count: 0, visible_detail_links: [],
      empty_result_visible: false, loading_indicator: {selector: '.loading', visible_count: 0,
        visible_texts: []}},
    loadingSnapshot: null,
    after: null,
  };
  const current = {...snapshot, observed_at: '2026-09-28T00:00:02Z', result_count: 0,
    visible_detail_links: [], empty_result_visible: true,
    loading_indicator: {selector: '.loading', visible_count: 0, visible_texts: []}};
  const finalized = finalizeTimedOutSearchObservation(observed, current);
  assert.equal(finalized.after.observation_id, observationId);
  assert.equal(buildSearchEvidence(task, finalized.after, finalized).refresh_evidence.kind,
    'observed_result_state_transition');
  assert.throws(() => finalizeTimedOutSearchObservation({...observed, timedOut: false}, current),
    /timed-out observed search/);
});

test('validated locator action accepts channel-switch-only zero results', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-action-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const targetTask = {...task, channel: '非凡'};
  const before = {...snapshot, selected_channels: ['量子'], result_count: 0,
    visible_detail_links: [], empty_result_visible: true,
    loading_indicator: {selector: '.loading', visible_count: 0, visible_texts: []},
    observed_at: '2026-09-28T00:00:03Z'};
  const after = {...before, selected_channels: [targetTask.channel],
    observed_at: '2026-09-28T00:00:04Z'};
  let domReads = 0;
  let clicks = 0;
  const clockBase = Date.now();
  const tab = {playwright: {
    evaluate: async (reader, args) => {
      if (reader === readSearchActionDom) return {input_value: task.query, total_count: 1,
        visible_controls: [{tag: 'BOTTON', text: targetTask.channel, onclick: 'searchN(3,this)', disabled: false}]};
      assert.equal(reader, readSearchDom);
      assert.equal(args.loadingSelector, '.loading');
      domReads += 1;
      return {...(domReads === 1 ? before : after),
        observed_at: new Date(clockBase + (domReads === 1 ? -1000 : 1000)).toISOString()};
    },
    locator: selector => ({click: async options => {
      assert.equal(selector, '.channel');
      assert.deepEqual(options, {timeoutMs: 5000, noWaitAfter: true});
      clicks += 1;
    }}),
  }};
  const saved = await runSearchActionEvidence({tab, task: targetTask,
    workDirectory: path.join(root, 'worker'), searchOptions: {selectedChannelSelector: '.selected',
      emptyResultSelector: '.empty', loadingSelector: '.loading'}, actionSelector: '.channel',
    settleDelayMs: 50, minimumSettleMs: 50, maxSettleReads: 1, runtimeRoot: root});
  assert.equal(saved.result_count, 0);
  assert.equal(clicks, 1);
  assert.equal(domReads, 2);
  const evidence = JSON.parse(await readFile(saved.evidencePath, 'utf8'));
  assert.equal(evidence.refresh_evidence.kind, 'verified_ui_action');
  assert.deepEqual(evidence.refresh_evidence.control, {tag: 'BOTTON', text: targetTask.channel});
  assert.deepEqual(evidence.selected_channels, [targetTask.channel]);
  assert.throws(() => buildSearchEvidence(targetTask, after, {before: after,
    uiAction: {taskId: task.task_id}}), /visibly change/);
});

test('validated locator action rejects identical preselected zero results', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-unchanged-zero-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const zero = {...snapshot, result_count: 0, visible_detail_links: [], empty_result_visible: true,
    loading_indicator: {selector: '.loading', visible_count: 0, visible_texts: []}};
  let domReads = 0;
  let clicks = 0;
  const tab = {playwright: {
    evaluate: async reader => {
      if (reader === readSearchActionDom) return {input_value: task.query, total_count: 1,
        visible_controls: [{tag: 'BOTTON', text: task.channel, onclick: 'searchN(1,this)', disabled: false}]};
      assert.equal(reader, readSearchDom);
      return {...zero, observed_at: new Date(Date.now() + (++domReads) * 1000).toISOString()};
    },
    locator: () => ({click: async () => { clicks += 1; }}),
  }};
  await assert.rejects(runSearchActionEvidence({tab, task,
    workDirectory: path.join(root, 'worker'), searchOptions: {selectedChannelSelector: '.selected',
      emptyResultSelector: '.empty', loadingSelector: '.loading'}, actionSelector: '.channel',
    settleDelayMs: 50, minimumSettleMs: 50, maxSettleReads: 1, runtimeRoot: root}),
  /visibly change/);
  assert.equal(clicks, 1);
  assert.equal(domReads, 2);
});

test('search action helper refuses ambiguous, disabled, wrong-channel, or handlerless controls', async () => {
  for (const control of [
    {total_count: 2, visible_controls: []},
    {total_count: 1, visible_controls: [{text: '非凡', onclick: 'x()', disabled: false}]},
    {total_count: 1, visible_controls: [{text: task.channel, onclick: 'x()', disabled: true}]},
    {total_count: 1, visible_controls: [{text: task.channel, onclick: '', disabled: false}]},
  ]) {
    const tab = {playwright: {evaluate: async reader => {
      assert.equal(reader, readSearchActionDom);
      return {input_value: task.query, ...control};
    }, locator: () => ({click: async () => assert.fail('must not click')})}};
    await assert.rejects(runSearchActionEvidence({tab, task, workDirectory: 'fixture', searchOptions: {},
      actionSelector: '.channel'}), /one visible enabled inspected control/);
  }
});

test('search helpers reject a missing owner before reading DOM or clicking', async () => {
  let browserCalls = 0;
  const tab = {playwright: {
    evaluate: async () => { browserCalls += 1; },
    locator: () => { browserCalls += 1; return {click: async () => { browserCalls += 1; }}; },
  }};
  const incompleteTask = {...task, owner: ''};
  await assert.rejects(runSearchActionEvidence({tab, task: incompleteTask, workDirectory: 'fixture',
    searchOptions: {}, actionSelector: '.channel'}), /Search task requires owner/);
  await assert.rejects(prepareSearchAction({tab, task: incompleteTask, workDirectory: 'fixture',
    searchOptions: {}, actionSelector: '.channel'}), /Search task requires owner/);
  assert.equal(browserCalls, 0);
});

test('unselected first click may return changed results but cannot bless unchanged selected state', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-unselected-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  let clicks = 0;
  let before = {...snapshot, selected_channels: [], result_count: 0, visible_detail_links: [],
    empty_result_visible: true, result_container_visible: true, pagination_pending: false,
    observed_at: new Date(Date.now() - 1000).toISOString()};
  const after = {...snapshot, selected_channels: [task.channel], result_count: 1,
    visible_detail_links: [{url: 'https://www.xxmanmi.com/video2/123.html', title: '猎罪图鉴',
      status: '更新至20集'}], empty_result_visible: false, result_container_visible: true,
    pagination_pending: false, observed_at: new Date(Date.now() + 1000).toISOString()};
  const action = {input_value: task.query, total_count: 1,
    visible_controls: [{tag: 'BOTTON', text: task.channel, onclick: 'searchN(1,this)', disabled: false}]};
  const makeTab = finalSnapshot => ({playwright: {
    evaluate: async reader => {
      if (reader === readSearchActionDom) return action;
      if (reader === readSearchPreparationDom) return {action,
        before: {...before, result_container_visible: true, pagination_pending: false}};
      assert.equal(reader, readSearchDom);
      return domReads++ === 0 ? before : finalSnapshot;
    },
    locator: () => ({click: async () => { clicks += 1; }}),
  }});
  let domReads = 0;
  const changedTab = makeTab(after);
  const changed = await runSearchActionEvidence({tab: changedTab, task, workDirectory: path.join(root, 'changed'),
    searchOptions: {selectedChannelSelector: '.selected', emptyResultSelector: '.empty'}, actionSelector: '.channel',
    settleDelayMs: 50, minimumSettleMs: 50, maxSettleReads: 1, runtimeRoot: root});
  assert.equal(changed.result_count, 1);
  assert.deepEqual(JSON.parse(await readFile(changed.evidencePath, 'utf8')).selected_channels, [task.channel]);
  const prepared = await prepareSearchAction({tab: changedTab, task, workDirectory: path.join(root, 'prepare'),
    searchOptions: {selectedChannelSelector: '.selected'}, actionSelector: '.channel', runtimeRoot: root});
  assert.equal(prepared.next_action, 'execute_search_action');

  domReads = 0;
  before = {...before, selected_channels: [task.channel]};
  const unchanged = {...before,
    observed_at: new Date(Date.now() + 1000).toISOString()};
  const unchangedTab = makeTab(unchanged);
  await assert.rejects(runSearchActionEvidence({tab: unchangedTab, task, workDirectory: path.join(root, 'unchanged'),
    searchOptions: {selectedChannelSelector: '.selected', emptyResultSelector: '.empty'}, actionSelector: '.channel',
    settleDelayMs: 50, minimumSettleMs: 50, maxSettleReads: 1, runtimeRoot: root}), /visibly change/);
  assert.equal(clicks, 2);
});

test('staged search action keeps private proof while each browser phase stays short', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-staged-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const clockBase = Date.now();
  const zero = {...snapshot, result_count: 0, visible_detail_links: [], empty_result_visible: true,
    loading_indicator: {selector: '.loading', visible_count: 0, visible_texts: []}};
  let clicks = 0;
  const tab = {playwright: {
    evaluate: async reader => {
      if (reader === readSearchPreparationDom) return {action: {input_value: task.query, total_count: 1,
        visible_controls: [{tag: 'BOTTON', text: task.channel, onclick: 'searchN(1,this)', disabled: false}]},
      before: {...zero, selected_channels: ['非凡'],
        observed_at: new Date(clockBase - 1000).toISOString()}};
      assert.equal(reader, readSearchDom);
      return {...zero, observed_at: new Date(clockBase + 1000).toISOString()};
    },
    locator: selector => ({click: async options => {
      assert.equal(selector, '.channel');
      assert.deepEqual(options, {timeoutMs: 5000, noWaitAfter: true});
      clicks += 1;
    }}),
  }};
  const prepared = await prepareSearchAction({tab, task, workDirectory: path.join(root, 'worker'),
    searchOptions: {selectedChannelSelector: '.selected', emptyResultSelector: '.empty',
      loadingSelector: '.loading'}, actionSelector: '.channel', runtimeRoot: root});
  assert.equal(prepared.next_action, 'execute_search_action');
  const executed = await executePreparedSearchAction({tab, actionId: prepared.actionId});
  assert.equal(executed.next_action, 'finalize_search_action');
  const saved = await finalizePreparedSearchAction({tab, actionId: prepared.actionId, settleDelayMs: 250});
  assert.equal(saved.result_count, 0);
  assert.equal(clicks, 1);
  assert.equal(JSON.parse(await readFile(saved.evidencePath, 'utf8')).refresh_evidence.kind,
    'verified_ui_action');
  await assert.rejects(executePreparedSearchAction({tab, actionId: prepared.actionId}),
    /missing|already executed/);
});

test('timed-out staged click is inspected once and only public result change can save evidence', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-timeout-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const before = {...snapshot, observed_at: '2026-09-28T00:00:00Z'};
  const after = {...snapshot, observed_at: '2026-09-28T00:00:06Z', result_count: 1,
    visible_detail_links: [{url: 'https://www.xxmanmi.com/video2/123.html', title: '猎罪图鉴',
      status: '更新至20集'}]};
  let domReads = 0;
  let clicks = 0;
  const tab = {playwright: {
    evaluate: async reader => {
      if (reader === readSearchPreparationDom) return {action: {input_value: task.query, total_count: 1,
        visible_controls: [{tag: 'BOTTON', text: task.channel, onclick: 'searchN(1,this)', disabled: false}]},
      before};
      assert.equal(reader, readSearchDom);
      domReads += 1;
      return after;
    },
    locator: () => ({click: async options => {
      assert.deepEqual(options, {timeoutMs: 5000, noWaitAfter: true});
      clicks += 1;
      const error = new Error('Timed out after 5000ms waiting on click');
      error.name = 'TimeoutError';
      throw error;
    }}),
  }};
  const prepared = await prepareSearchAction({tab, task, workDirectory: path.join(root, 'worker'),
    searchOptions: {selectedChannelSelector: '.selected'}, actionSelector: '.channel', runtimeRoot: root});
  const executed = await executePreparedSearchAction({tab, actionId: prepared.actionId});
  assert.equal(executed.next_action, 'inspect_search_action');
  const saved = await finalizePreparedSearchAction({tab, actionId: prepared.actionId, settleDelayMs: 250});
  assert.equal(saved.result_count, 1);
  assert.equal(clicks, 1);
  assert.equal(domReads, 1);
  const evidence = JSON.parse(await readFile(saved.evidencePath, 'utf8'));
  assert.equal(evidence.refresh_evidence.kind, 'changed_public_results');
});

test('observer-tagged snapshots cannot be mixed across search actions', () => {
  const value = {...snapshot, result_count: 0, visible_detail_links: [], empty_result_visible: true,
    loading_indicator: {selector: '.loading', visible_count: 0, visible_texts: []}};
  const before = {...value, observed_at: '2026-09-28T00:00:00Z', observation_id: 'one',
    observation_phase: 'before'};
  const loadingSnapshot = {...value, observed_at: '2026-09-28T00:00:01Z', observation_id: 'two',
    observation_phase: 'loading', loading_indicator: {selector: '.loading', visible_count: 1,
      visible_texts: ['加载中']}};
  const after = {...value, observed_at: '2026-09-28T00:00:02Z', observation_id: 'one',
    observation_phase: 'after'};
  assert.throws(() => buildSearchEvidence(task, after, {before, loadingSnapshot}), /visibly change/);
  const transitionSnapshot = {...value, observed_at: '2026-09-28T00:00:01Z', observation_id: 'two',
    observation_phase: 'transition', empty_result_visible: false};
  assert.throws(() => buildSearchEvidence(task, after, {before, transitionSnapshot}), /visibly change/);
});

test('search evidence saves outside model output without overwriting another capture', async t => {
  const root = await mkdtemp(path.join(os.tmpdir(), 'xxmm-search-test-'));
  t.after(() => rm(root, {recursive: true, force: true}));
  const result = await saveSearchEvidence(task, snapshot, path.join(root, 'worker'), {before, runtimeRoot: root});
  assert.doesNotMatch(JSON.stringify(result), /video2|visible_detail_links/);
  assert.equal(JSON.parse(await readFile(result.evidencePath, 'utf8')).owner, task.owner);
  await assert.rejects(saveSearchEvidence(task, {...snapshot, visible_detail_links: [{...snapshot.visible_detail_links[0], status: 'new'}]},
    path.join(root, 'worker'), {before, runtimeRoot: root}), /EEXIST/);
});
