// Pure in-memory fixtures: no actual tab, network, database, or journal writes.
import assert from 'node:assert/strict';
import test from 'node:test';
import vm from 'node:vm';
import {captureDetailWithRecovery} from './capture-with-recovery.mjs';

const TITLE_MISMATCH = 'DOM program title does not match the assigned queue item';
const TEMPORARY_TITLE = '获取失败，请刷新页面';
const WORK_DIRECTORY = 'C:/offline-fixture/worker-01';

function itemFixture() {
  return Object.freeze({
    url: 'https://catalog.example.invalid/detail/fixture.html',
    title: '离线测试原始节目',
    source: '离线测试原始渠道',
    query: '离线测试词',
    category: '离线测试分类',
  });
}

function snapshotFixture(item, overrides = {}) {
  return {
    url: item.url,
    title: TEMPORARY_TITLE,
    episodeDomCount: 0,
    visibleText: `公开导航 更新验证码 验证码期限0天 ${TEMPORARY_TITLE}`,
    visibleTextTruncated: false,
    ...overrides,
  };
}

function harness(options = {}) {
  const item = options.item ?? itemFixture();
  const initialError = Object.hasOwn(options, 'initialError')
    ? options.initialError : new Error(TITLE_MISMATCH);
  const snapshot = Object.hasOwn(options, 'snapshot')
    ? options.snapshot : snapshotFixture(item);
  const summary = Object.freeze({title: item.title, url: item.url, episodeDomCount: 2});
  const events = [];
  const captures = [];
  const evaluations = [];
  const navigations = [];
  const issues = [];
  const tab = {
    playwright: {
      async evaluate(callback, ...args) {
        events.push('evaluate');
        evaluations.push({callback, args});
        if (options.evaluateError) throw options.evaluateError;
        return options.evaluate ? options.evaluate(callback, ...args) : snapshot;
      },
    },
    async goto(url) {
      events.push('goto');
      navigations.push(url);
      if (options.gotoError) throw options.gotoError;
    },
  };
  const captureDetail = async (...args) => {
    captures.push(args);
    events.push(`capture:${captures.length}`);
    assert.ok(captures.length <= 2, 'a third capture attempt is forbidden');
    if (captures.length === 1 && initialError) throw initialError;
    if (captures.length === 2 && options.retryError) throw options.retryError;
    return summary;
  };
  const reportIssue = async (issue, directory) => {
    issues.push({issue: structuredClone(issue), directory});
    events.push(`issue:${issue.state}`);
    if (options.reportIssue) await options.reportIssue(issue, directory, issues.length);
    if (options.issueFailureAt === issues.length) throw options.issueError;
  };
  const run = (extraOptions = {}) => captureDetailWithRecovery(tab, item, WORK_DIRECTORY, {
    captureDetail, reportIssue, ...extraOptions,
  });
  return {item, initialError, snapshot, summary, events, captures, evaluations,
    navigations, issues, tab, captureDetail, reportIssue, run};
}

function assertIssue(record, h, state, originalError = h.initialError) {
  assert.equal(record.directory, WORK_DIRECTORY);
  const {issue} = record;
  assert.equal(issue.kind, 'detail-capture-recovery');
  assert.equal(issue.state, state);
  for (const field of ['url', 'title', 'source']) {
    assert.equal(issue.expected[field], h.item[field], `expected ${field} is preserved`);
  }
  assert.equal(typeof issue.reason, 'string');
  assert.ok(issue.reason.trim());
  assert.equal(typeof issue.collectedAt, 'string');
  assert.ok(Number.isFinite(Date.parse(issue.collectedAt)));
  if (originalError) {
    assert.ok(JSON.stringify(issue.error).includes(originalError.message),
      'issue must retain the relevant error summary');
  }
}

function assertAttempts(h, {capture = 1, evaluate = 0, goto = 0, issues = 1} = {}) {
  assert.equal(h.captures.length, capture);
  assert.equal(h.evaluations.length, evaluate);
  assert.equal(h.navigations.length, goto);
  assert.equal(h.issues.length, issues);
}

function runReadOnlyDomCallback(callback, {url, title, bodyText, episodeCount = 0}) {
  const reads = [];
  const forbidden = () => { throw new Error('Fixture forbids mutation, navigation, clicks, and network'); };
  const readOnly = target => new Proxy(target, {
    get(object, key) {
      if (!Object.hasOwn(object, key)) throw new Error(`Unexpected DOM access: ${String(key)}`);
      return Reflect.get(object, key);
    },
    set: forbidden, defineProperty: forbidden, deleteProperty: forbidden,
  });
  const document = readOnly({
    body: readOnly({get innerText() { reads.push('body.innerText'); return bodyText; }}),
    querySelector(selector) {
      assert.equal(selector, '#video');
      reads.push(selector);
      return readOnly({textContent: title});
    },
    querySelectorAll(selector) {
      assert.equal(selector, '#list a[data-web]');
      reads.push(selector);
      return Object.freeze(Array.from({length: episodeCount}, () => readOnly({})));
    },
  });
  const location = readOnly({get href() { reads.push('location.href'); return url; }});
  assert.equal(typeof callback, 'function');
  const result = vm.runInNewContext(`'use strict'; (${callback.toString()})()`, {
    document, location, fetch: forbidden, XMLHttpRequest: forbidden, WebSocket: forbidden,
    window: readOnly({location}),
  }, {timeout: 1000});
  return {snapshot: JSON.parse(JSON.stringify(result)), reads};
}

test('initial success returns original summary without diagnostic DOM, navigation, or issue', async () => {
  const h = harness({initialError: null});
  assert.strictEqual(await h.run(), h.summary);
  assertAttempts(h, {issues: 0});
  assert.equal(h.captures[0][3].alreadyLoaded, false);
});

test('initial alreadyLoaded option and unchanged queue metadata reach the injected capture', async () => {
  const h = harness({initialError: null});
  assert.strictEqual(await h.run({alreadyLoaded: true}), h.summary);
  assert.strictEqual(h.captures[0][0], h.tab);
  assert.deepEqual(h.captures[0][1], h.item);
  assert.equal(h.captures[0][2], WORK_DIRECTORY);
  assert.equal(h.captures[0][3].alreadyLoaded, true);
  assertAttempts(h, {issues: 0});
});

test('both injected dependencies are mandatory functions', async () => {
  const h = harness();
  for (const options of [undefined, {}, {captureDetail: h.captureDetail},
    {reportIssue: h.reportIssue}, {captureDetail: false, reportIssue: h.reportIssue},
    {captureDetail: h.captureDetail, reportIssue: 'not a function'}]) {
    await assert.rejects(captureDetailWithRecovery(h.tab, h.item, WORK_DIRECTORY, options));
  }
  assertAttempts(h, {capture: 0, issues: 0});
});

test('explicit temporary page recovers once and generic captcha header is not a challenge', async () => {
  const h = harness();
  assert.strictEqual(await h.run(), h.summary);
  assertAttempts(h, {capture: 2, evaluate: 1, goto: 1, issues: 2});
  assert.deepEqual(h.events, [
    'capture:1', 'evaluate', 'issue:retrying', 'goto', 'capture:2', 'issue:recovered',
  ]);
  assert.deepEqual(h.navigations, [h.item.url]);
  assertIssue(h.issues[0], h, 'retrying');
  assertIssue(h.issues[1], h, 'recovered', null);
  assert.deepEqual(h.issues[0].issue.observed, h.snapshot);
  assert.equal(h.captures[1][3].alreadyLoaded, true);
  for (const args of h.captures) assert.deepEqual(args[1], h.item);
});

test('retrying evidence is awaited before any recovery navigation', async () => {
  let release;
  let reached;
  const paused = new Promise(resolve => { release = resolve; });
  const logged = new Promise(resolve => { reached = resolve; });
  const h = harness({reportIssue: async issue => {
    if (issue.state === 'retrying') {
      reached();
      await paused;
    }
  }});
  const pending = h.run();
  await logged;
  assert.equal(h.navigations.length, 0);
  assert.equal(h.captures.length, 1);
  release();
  assert.strictEqual(await pending, h.summary);
  assert.equal(h.navigations.length, 1);
});

test('fragment-only URL differences are allowed with an exact diagnostic snapshot title', async () => {
  const item = Object.freeze({...itemFixture(), url: `${itemFixture().url}#queue`});
  const h = harness({item, snapshot: snapshotFixture(item, {
    url: `${itemFixture().url}#observed`, title: TEMPORARY_TITLE,
  })});
  assert.strictEqual(await h.run(), h.summary);
  assert.deepEqual(h.navigations, [item.url]);
  assert.equal(h.captures.length, 2);
});

test('other initial errors are reported and rethrown unchanged without DOM diagnosis', async () => {
  for (const message of ['network unavailable', 'Missing DOM program title',
    `${TITLE_MISMATCH} `, TITLE_MISMATCH.toLowerCase()]) {
    const initialError = new Error(message);
    const h = harness({initialError});
    await assert.rejects(h.run(), error => error === initialError);
    assertAttempts(h);
    assertIssue(h.issues[0], h, 'needs_review');
    assert.equal(h.issues[0].issue.observed, null);
  }
});

test('actual different titles, extra temporary text, or absent title are never rewritten', async () => {
  for (const title of ['其他真实节目', `${TEMPORARY_TITLE}。`, `提示：${TEMPORARY_TITLE}`,
    '', undefined, null, 0]) {
    const item = itemFixture();
    const h = harness({item, snapshot: snapshotFixture(item, {title})});
    await assert.rejects(h.run(), error => error === h.initialError);
    assertAttempts(h, {evaluate: 1});
    assertIssue(h.issues[0], h, 'needs_review');
    assert.deepEqual(h.captures[0][1], item);
  }
});

test('different page path, query, scheme, origin, and malformed URL forbid recovery', async () => {
  for (const url of ['https://catalog.example.invalid/detail/other.html',
    `${itemFixture().url}?other=1`, itemFixture().url.replace('https:', 'http:'),
    'https://other.example.invalid/detail/fixture.html', 'not a URL', null]) {
    const item = itemFixture();
    const h = harness({item, snapshot: snapshotFixture(item, {url})});
    await assert.rejects(h.run(), error => error === h.initialError);
    assertAttempts(h, {evaluate: 1});
    assertIssue(h.issues[0], h, 'needs_review');
  }
});

test('only numeric zero episode count qualifies, never nonzero or missing counts', async () => {
  for (const episodeDomCount of [1, -1, '0', false, null, undefined, NaN]) {
    const item = itemFixture();
    const h = harness({item, snapshot: snapshotFixture(item, {episodeDomCount})});
    await assert.rejects(h.run(), error => error === h.initialError);
    assertAttempts(h, {evaluate: 1});
    assertIssue(h.issues[0], h, 'needs_review');
  }
});

test('explicit access restrictions do not trigger refresh or playback', async () => {
  for (const challenge of ['请先登录', '请提交验证码', '访问受限',
    '输入验证码', '填写验证码', '提交验证码', 'login required', 'authentication required']) {
    const item = itemFixture();
    const h = harness({item, snapshot: snapshotFixture(item, {
      visibleText: `普通页眉 更新验证码 验证码期限0天 ${challenge} ${TEMPORARY_TITLE}`,
    })});
    await assert.rejects(h.run(), error => error === h.initialError);
    assertAttempts(h, {evaluate: 1});
    assertIssue(h.issues[0], h, 'needs_review');
  }
});

test('truncated or unavailable visible text fails closed', async () => {
  const item = itemFixture();
  const snapshots = [
    ...[true, 'false', undefined].map(visibleTextTruncated => snapshotFixture(item, {visibleTextTruncated})),
    ...[undefined, null, {}].map(visibleText => snapshotFixture(item, {visibleText})),
    null,
  ];
  for (const snapshot of snapshots) {
    const h = harness({item, snapshot});
    await assert.rejects(h.run(), error => error === h.initialError);
    assertAttempts(h, {evaluate: 1});
    assertIssue(h.issues[0], h, 'needs_review');
  }
});

test('diagnostic browser failure is aggregated with original capture cause and never navigates', async () => {
  const evaluateError = new Error('synthetic diagnostic browser exception');
  const h = harness({evaluateError});
  await assert.rejects(h.run(), error => {
    assert.ok(error instanceof AggregateError);
    assert.strictEqual(error.cause, h.initialError);
    assert.ok(error.errors.includes(evaluateError));
    return true;
  });
  assertAttempts(h, {evaluate: 1});
  assertIssue(h.issues[0], h, 'needs_review', null);
  assert.equal(h.issues[0].issue.error.name, 'AggregateError');
  assert.equal(h.issues[0].issue.initialError.message, h.initialError.message);
  assert.equal(h.issues[0].issue.inspectionError.message, evaluateError.message);
});

test('a second capture failure is recorded and rethrown, without third capture or second diagnosis', async () => {
  for (const message of [TITLE_MISMATCH, 'synthetic capture failure after refresh']) {
    const retryError = new Error(message);
    const h = harness({retryError});
    await assert.rejects(h.run(), error => error === retryError);
    assertAttempts(h, {capture: 2, evaluate: 1, goto: 1, issues: 2});
    assertIssue(h.issues[0], h, 'retrying');
    assertIssue(h.issues[1], h, 'needs_review', retryError);
    assert.deepEqual(h.events.slice(-2), ['capture:2', 'issue:needs_review']);
  }
});

test('recovery goto failure is recorded once and does not invoke another capture', async () => {
  const gotoError = new Error('synthetic closed tab during navigation');
  const h = harness({gotoError});
  await assert.rejects(h.run(), error => error === gotoError);
  assertAttempts(h, {capture: 1, evaluate: 1, goto: 1, issues: 2});
  assertIssue(h.issues[1], h, 'needs_review', gotoError);
});

test('failure to persist retrying evidence prevents navigation and retains original cause', async () => {
  const issueError = new Error('synthetic retry journal failure');
  const h = harness({issueError, issueFailureAt: 1});
  await assert.rejects(h.run(), error => {
    assert.ok(error instanceof AggregateError);
    assert.strictEqual(error.cause, h.initialError);
    assert.ok(error.errors.includes(issueError));
    return true;
  });
  assertAttempts(h, {evaluate: 1});
  assert.equal(h.issues[0].issue.state, 'retrying');
});

test('failure to persist an unrecoverable initial error is aggregated without DOM or navigation', async () => {
  const issueError = new Error('synthetic needs-review journal failure');
  const initialError = new Error('synthetic initial non-title error');
  const h = harness({initialError, issueError, issueFailureAt: 1});
  await assert.rejects(h.run(), error => {
    assert.ok(error instanceof AggregateError);
    assert.strictEqual(error.cause, initialError);
    assert.ok(error.errors.includes(issueError));
    return true;
  });
  assertAttempts(h);
});

test('failure to persist retry failure retains retry cause and never launches a third capture', async () => {
  const retryError = new Error('synthetic retry capture failure');
  const issueError = new Error('synthetic final journal failure');
  const h = harness({retryError, issueError, issueFailureAt: 2});
  await assert.rejects(h.run(), error => {
    assert.ok(error instanceof AggregateError);
    assert.strictEqual(error.cause, retryError);
    assert.ok(error.errors.includes(issueError));
    return true;
  });
  assertAttempts(h, {capture: 2, evaluate: 1, goto: 1, issues: 2});
});

test('failure to persist recovered evidence does not recapture or relabel success as another retry', async () => {
  const issueError = new Error('synthetic recovered journal failure');
  const h = harness({issueError, issueFailureAt: 2});
  await assert.rejects(h.run(), error => {
    assert.ok(error instanceof AggregateError);
    assert.ok(error.errors.includes(issueError));
    return true;
  });
  assertAttempts(h, {capture: 2, evaluate: 1, goto: 1, issues: 2});
  assert.deepEqual(h.issues.map(({issue}) => issue.state), ['retrying', 'recovered']);
});

test('actual diagnostic callback trims DOM title using only readonly fixture access', async () => {
  const item = itemFixture();
  let result;
  const h = harness({item, evaluate: callback => {
    result = runReadOnlyDomCallback(callback, {
      url: item.url, title: `  ${TEMPORARY_TITLE} \n`,
      bodyText: `更新验证码 验证码期限0天 ${TEMPORARY_TITLE}`,
    });
    return result.snapshot;
  }});
  assert.strictEqual(await h.run(), h.summary);
  assert.equal(result.snapshot.title, TEMPORARY_TITLE);
  assert.equal(result.snapshot.episodeDomCount, 0);
  assert.equal(result.snapshot.visibleTextTruncated, false);
  assert.deepEqual(new Set(result.reads), new Set([
    'body.innerText', 'location.href', '#video', '#list a[data-web]',
  ]));
  assertAttempts(h, {capture: 2, evaluate: 1, goto: 1, issues: 2});
});

test('actual diagnostic callback counts all fixture resource anchors and prevents recovery', async () => {
  const item = itemFixture();
  const h = harness({item, evaluate: callback => runReadOnlyDomCallback(callback, {
    url: item.url, title: TEMPORARY_TITLE, bodyText: TEMPORARY_TITLE, episodeCount: 3,
  }).snapshot});
  await assert.rejects(h.run(), error => error === h.initialError);
  assertAttempts(h, {evaluate: 1});
  assert.equal(h.issues[0].issue.observed.episodeDomCount, 3);
});

test('actual diagnostic callback flags over-24000 visible text and blocks unseen tail challenges', async () => {
  const item = itemFixture();
  for (const bodyText of ['x'.repeat(24000), `${'x'.repeat(24000)}请先登录`]) {
    const h = harness({item, evaluate: callback => runReadOnlyDomCallback(callback, {
      url: item.url, title: TEMPORARY_TITLE, bodyText,
    }).snapshot});
    if (bodyText.length === 24000) {
      assert.strictEqual(await h.run(), h.summary);
      assertAttempts(h, {capture: 2, evaluate: 1, goto: 1, issues: 2});
    } else {
      await assert.rejects(h.run(), error => error === h.initialError);
      assertAttempts(h, {evaluate: 1});
    }
    const observed = h.issues[0].issue.observed;
    assert.equal(observed.visibleText.length, 24000);
    assert.equal(observed.visibleTextTruncated, bodyText.length > 24000);
  }
});

