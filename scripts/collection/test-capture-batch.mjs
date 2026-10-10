// Pure offline mocks: no browser, filesystem, database, or network access.
import assert from 'node:assert/strict';
import test from 'node:test';
import {runCaptureBatch} from './capture-batch.mjs';

const worker = number => `C:/offline-fixture/worker-0${number}`;
const item = (id, title = `节目${id}`) => Object.freeze({
  url: `https://catalog.example.invalid/video2/${id}.html`,
  title,
  source: '观察渠道',
});
const lane = (number, items, extra = {}) => ({
  name: `worker-0${number}`,
  tab: {id: `tab-${number}`},
  workDirectory: worker(number),
  items,
  ...extra,
});

test('rejects a third lane before capture or diagnostic work starts', async () => {
  let calls = 0;
  await assert.rejects(runCaptureBatch([
    lane(1, [item(1)]), lane(2, [item(2)]), lane(3, [item(3)]),
  ], {
    captureItem: async () => { calls += 1; },
    writeIssue: async () => { calls += 1; },
  }), /one or two/);
  assert.equal(calls, 0);
});

test('deduplicates by normalized URL but keeps equal titles on different URLs', async () => {
  const captures = [];
  const sharedTitle = '同名但不同详情地址';
  const summary = await runCaptureBatch([
    lane(1, [item(11, sharedTitle)]),
    lane(2, [item(12, sharedTitle)]),
  ], {
    captureItem: async (_tab, observed) => { captures.push(observed.url); },
    writeIssue: async () => assert.fail('no diagnostic expected'),
  });
  assert.equal(summary.journaled, 2);
  assert.equal(summary.failed, 0);
  assert.equal(new Set(captures).size, 2);
  assert.doesNotMatch(JSON.stringify(summary), /catalog\.example\.invalid/,
    'successful URL lists must not leak into the compact summary');

  let duplicateCalls = 0;
  await assert.rejects(runCaptureBatch([
    lane(1, [item(20)]),
    lane(2, [{...item(20), url: `${item(20).url}#fragment`}]),
  ], {
    captureItem: async () => { duplicateCalls += 1; },
    writeIssue: async () => { duplicateCalls += 1; },
  }), /Duplicate detail URL/);
  assert.equal(duplicateCalls, 0);
});

test('runs lanes concurrently while every individual lane remains sequential', async () => {
  const activeByTab = new Map();
  let activeLanes = 0;
  let peakLanes = 0;
  let releaseFirstPair;
  let firstPair = 0;
  const paired = new Promise(resolve => { releaseFirstPair = resolve; });

  const captureItem = async (tab, observed) => {
    assert.equal(activeByTab.get(tab) ?? 0, 0, 'same tab must not overlap');
    activeByTab.set(tab, 1);
    activeLanes += 1;
    peakLanes = Math.max(peakLanes, activeLanes);
    if (observed.url.endsWith('/31.html') || observed.url.endsWith('/41.html')) {
      firstPair += 1;
      if (firstPair === 2) releaseFirstPair();
      await paired;
    }
    await Promise.resolve();
    activeLanes -= 1;
    activeByTab.set(tab, 0);
  };

  const summary = await runCaptureBatch([
    lane(1, [item(31), item(32)]),
    lane(2, [item(41), item(42)]),
  ], {captureItem, writeIssue: async () => assert.fail('no diagnostic expected')});
  assert.equal(peakLanes, 2);
  assert.equal(summary.journaled, 4);
  assert.equal(summary.ok, true);
});

test('a capture error stops only its lane and returns one compact durable failure', async () => {
  const captures = [];
  const issues = [];
  const bad = new Error('fixture capture failed');
  const summary = await runCaptureBatch([
    lane(1, [item(51), item(52), item(53)]),
    lane(2, [item(61), item(62)]),
  ], {
    captureItem: async (_tab, observed) => {
      captures.push(observed.url);
      if (observed.url.endsWith('/51.html')) throw bad;
    },
    writeIssue: async (issue, workDirectory) => { issues.push({issue, workDirectory}); },
  });

  assert.equal(summary.ok, false);
  assert.equal(summary.requested, 5);
  assert.equal(summary.attempted, 3);
  assert.equal(summary.journaled, 2);
  assert.equal(summary.failed, 1);
  assert.equal(summary.skipped, 2);
  assert.equal(captures.some(url => url.endsWith('/52.html') || url.endsWith('/53.html')), false);
  assert.equal(captures.some(url => url.endsWith('/61.html')), true);
  assert.equal(captures.some(url => url.endsWith('/62.html')), true);
  assert.equal(issues.length, 1);
  assert.equal(issues[0].workDirectory, worker(1));
  assert.equal(issues[0].issue.retryAttempt, 0);
  assert.equal(summary.lanes[0].errors.length, 1);
});

test('diagnostic writer failure is exposed after both lanes settle and is never retried', async () => {
  let captureCalls = 0;
  let writerCalls = 0;
  let otherLaneCompleted = false;
  const captureFailure = new Error('capture unavailable');
  const writerFailure = new Error('journal append unavailable');

  await assert.rejects(runCaptureBatch([
    lane(1, [item(71), item(72)]),
    lane(2, [item(81)]),
  ], {
    captureItem: async (_tab, observed) => {
      captureCalls += 1;
      if (observed.url.endsWith('/71.html')) throw captureFailure;
      otherLaneCompleted = true;
    },
    writeIssue: async () => {
      writerCalls += 1;
      throw writerFailure;
    },
  }), error => {
    assert.ok(error instanceof AggregateError);
    assert.match(error.message, /diagnostics could not be persisted/);
    assert.equal(error.summary.failed, 1);
    assert.equal(error.summary.journaled, 1);
    assert.equal(error.summary.lanes[0].errors[0].writer.message, writerFailure.message);
    return true;
  });
  assert.equal(captureCalls, 2, 'failed lane is not retried and the other lane still completes');
  assert.equal(writerCalls, 1, 'a failed diagnostic writer is never retried');
  assert.equal(otherLaneCompleted, true);
});

test('an always-failing capture is called once, with no hidden or infinite retry', async () => {
  let captureCalls = 0;
  let writerCalls = 0;
  const summary = await runCaptureBatch([lane(1, [item(91), item(92)])], {
    captureItem: async () => {
      captureCalls += 1;
      throw new Error('still failing');
    },
    writeIssue: async () => { writerCalls += 1; },
  });
  assert.equal(captureCalls, 1);
  assert.equal(writerCalls, 1);
  assert.equal(summary.attempted, 1);
  assert.equal(summary.failed, 1);
  assert.equal(summary.skipped, 1);
});

test('rejects concurrent reuse of a tab or worker directory', async () => {
  const sharedTab = {id: 'same-tab'};
  const first = lane(1, [item(101)], {tab: sharedTab});
  const secondSameTab = lane(2, [item(102)], {tab: sharedTab});
  const dependencies = {
    captureItem: async () => assert.fail('preflight should reject'),
    writeIssue: async () => assert.fail('preflight should reject'),
  };
  await assert.rejects(runCaptureBatch([first, secondSameTab], dependencies), /same tab/);

  const secondSameWorker = lane(2, [item(103)], {workDirectory: `${worker(1)}\\`});
  await assert.rejects(runCaptureBatch([lane(1, [item(104)]), secondSameWorker], dependencies),
    /same worker directory/);

  const aliasedWorker = lane(2, [item(105)], {
    workDirectory: 'C:/offline-fixture/child/../worker-01',
  });
  await assert.rejects(runCaptureBatch([lane(1, [item(106)]), aliasedWorker], dependencies),
    /same worker directory/);
});

test('different wrappers with the same tab id cannot enter concurrent batches', async () => {
  let release;
  let started;
  let idReads = 0;
  const blocked = new Promise(resolve => { release = resolve; });
  const entered = new Promise(resolve => { started = resolve; });
  const tabWrapper = suffix => ({
    get id() {
      idReads += 1;
      return 'observed-tab-501';
    },
    wrapper: suffix,
  });
  const firstLane = lane(1, [item(111)], {tab: tabWrapper('first')});
  const firstRun = runCaptureBatch([firstLane], {
    captureItem: async () => { started(); await blocked; },
    writeIssue: async () => assert.fail('no diagnostic expected'),
  });
  await entered;

  await assert.rejects(runCaptureBatch([
    lane(2, [item(112)], {tab: tabWrapper('second')}),
  ], {
    captureItem: async () => assert.fail('concurrent tab wrapper should be rejected'),
    writeIssue: async () => assert.fail('concurrent tab wrapper should be rejected'),
  }), /Tab is already active/);
  assert.equal(idReads, 2, 'preflight only reads the already-bound wrappers id values');

  release();
  await firstRun;
  const afterRelease = await runCaptureBatch([
    lane(2, [item(113)], {tab: tabWrapper('third')}),
  ], {
    captureItem: async () => {},
    writeIssue: async () => assert.fail('no diagnostic expected'),
  });
  assert.equal(afterRelease.journaled, 1, 'finally releases the tab id reservation');
});

test('worker aliases cannot enter concurrent batches and reservation is released on failure', async () => {
  let release;
  let started;
  const blocked = new Promise(resolve => { release = resolve; });
  const entered = new Promise(resolve => { started = resolve; });
  const firstRun = runCaptureBatch([
    lane(1, [item(121)], {workDirectory: 'C:/offline-fixture/parent/../worker-01'}),
  ], {
    captureItem: async () => { started(); await blocked; },
    writeIssue: async () => assert.fail('no diagnostic expected'),
  });
  await entered;

  await assert.rejects(runCaptureBatch([
    lane(2, [item(122)], {workDirectory: 'c:\\offline-fixture\\worker-01\\'}),
  ], {
    captureItem: async () => assert.fail('concurrent worker alias should be rejected'),
    writeIssue: async () => assert.fail('concurrent worker alias should be rejected'),
  }), /Worker directory is already active/);
  release();
  await firstRun;

  let writerCalls = 0;
  await assert.rejects(runCaptureBatch([
    lane(1, [item(123)]),
  ], {
    captureItem: async () => { throw new Error('capture failed'); },
    writeIssue: async () => { writerCalls += 1; throw new Error('writer failed'); },
  }), AggregateError);
  assert.equal(writerCalls, 1);

  const afterFailure = await runCaptureBatch([lane(1, [item(124)])], {
    captureItem: async () => {},
    writeIssue: async () => assert.fail('no diagnostic expected'),
  });
  assert.equal(afterFailure.journaled, 1, 'finally releases reservations after fatal writer errors');
});
