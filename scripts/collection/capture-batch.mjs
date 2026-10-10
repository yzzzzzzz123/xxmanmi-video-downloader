// Coordinate only caller-supplied tabs, observed items, and durable capture helpers.
// This module does not create/connect a browser, access the network or database,
// or retry an item. A captureItem call must resolve only after its journal write.
import path from 'node:path';

const MAX_LANES = 2;
const MAX_ERROR_MESSAGE = 1200;
const inFlightTabs = new Set();
const inFlightWorkDirectories = new Set();

function normalizedPageUrl(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol)) {
    throw new TypeError('Every observed item URL must use HTTP(S)');
  }
  url.hash = '';
  return url.href;
}

function normalizedWorkDirectory(value) {
  if (typeof value !== 'string' || !value.trim()) {
    throw new TypeError('Every lane requires an explicit workDirectory');
  }
  return path.resolve(value.trim()).replace(/\\/g, '/').replace(/\/+$/u, '').toLowerCase();
}

function tabConcurrencyKey(tab) {
  const id = tab.id;
  if (['string', 'number', 'bigint'].includes(typeof id) && String(id).length) {
    return `tab-id:${String(id)}`;
  }
  return tab;
}

function errorSummary(error) {
  return {
    name: typeof error?.name === 'string' ? error.name : 'Error',
    message: String(error?.message ?? error).slice(0, MAX_ERROR_MESSAGE),
  };
}

function validateAndSnapshot(lanes) {
  if (!Array.isArray(lanes) || lanes.length < 1 || lanes.length > MAX_LANES) {
    throw new TypeError('Expected one or two explicitly assigned capture lanes');
  }

  const tabs = new Set();
  const workDirectories = new Set();
  const pageUrls = new Set();

  return lanes.map((lane, laneIndex) => {
    if (!lane || typeof lane !== 'object' || !lane.tab ||
        (typeof lane.tab !== 'object' && typeof lane.tab !== 'function')) {
      throw new TypeError(`Lane ${laneIndex + 1} requires an already-bound tab`);
    }
    const tabKey = tabConcurrencyKey(lane.tab);
    if (tabs.has(tabKey)) throw new Error('The same tab cannot be used by two concurrent lanes');
    tabs.add(tabKey);

    const workDirectoryKey = normalizedWorkDirectory(lane.workDirectory);
    if (workDirectories.has(workDirectoryKey)) {
      throw new Error('The same worker directory cannot be used by two concurrent lanes');
    }
    workDirectories.add(workDirectoryKey);

    if (!Array.isArray(lane.items) || lane.items.length < 1) {
      throw new TypeError(`Lane ${laneIndex + 1} requires explicit observed items`);
    }
    if (lane.alreadyLoadedFirst !== undefined && typeof lane.alreadyLoadedFirst !== 'boolean') {
      throw new TypeError('alreadyLoadedFirst must be a boolean when supplied');
    }

    const items = lane.items.map((item, itemIndex) => {
      if (!item || typeof item !== 'object' || typeof item.url !== 'string') {
        throw new TypeError(`Lane ${laneIndex + 1} item ${itemIndex + 1} lacks an observed URL`);
      }
      const pageUrl = normalizedPageUrl(item.url);
      if (pageUrls.has(pageUrl)) {
        throw new Error(`Duplicate detail URL in capture batch: ${pageUrl}`);
      }
      pageUrls.add(pageUrl);
      return Object.freeze({...item});
    });

    return Object.freeze({
      name: typeof lane.name === 'string' && lane.name.trim()
        ? lane.name.trim() : `lane-${laneIndex + 1}`,
      tab: lane.tab,
      tabKey,
      workDirectory: lane.workDirectory,
      workDirectoryKey,
      items: Object.freeze(items),
      alreadyLoadedFirst: lane.alreadyLoadedFirst === true,
    });
  });
}

function laneSummary(lane, attempted, journaled, errors) {
  const failed = errors.length ? 1 : 0;
  return {
    name: lane.name,
    requested: lane.items.length,
    attempted,
    journaled,
    failed,
    skipped: lane.items.length - attempted,
    errors,
  };
}

async function runLane(lane, captureItem, writeIssue) {
  let attempted = 0;
  let journaled = 0;

  for (let index = 0; index < lane.items.length; index += 1) {
    const item = lane.items[index];
    attempted += 1;
    try {
      // The injected helper owns ordinary navigation and durable journal writing.
      // This coordinator deliberately makes exactly one call for this item.
      await captureItem(lane.tab, item, lane.workDirectory, {
        alreadyLoaded: lane.alreadyLoadedFirst && index === 0,
      });
      journaled += 1;
    } catch (captureError) {
      const compactError = {
        index,
        title: typeof item.title === 'string' ? item.title.slice(0, 200) : '',
        url: normalizedPageUrl(item.url),
        capture: errorSummary(captureError),
      };
      const issue = {
        kind: 'capture-batch-lane-failure',
        collectedAt: new Date().toISOString(),
        state: 'needs_review',
        lane: lane.name,
        itemIndex: index,
        expected: {
          url: compactError.url,
          title: compactError.title,
          source: typeof item.source === 'string' ? item.source : '',
        },
        error: compactError.capture,
        retryAttempt: 0,
      };

      try {
        // Await one durable diagnostic write. Never retry a failed writer.
        await writeIssue(issue, lane.workDirectory);
      } catch (writerError) {
        compactError.writer = errorSummary(writerError);
        const summary = laneSummary(lane, attempted, journaled, [compactError]);
        const failure = new AggregateError(
          [captureError, writerError],
          `Capture failed in ${lane.name} and its diagnostic could not be persisted`,
          {cause: captureError},
        );
        Object.defineProperty(failure, 'laneSummary', {value: summary, enumerable: true});
        throw failure;
      }
      return laneSummary(lane, attempted, journaled, [compactError]);
    }
  }

  return laneSummary(lane, attempted, journaled, []);
}

function batchSummary(laneSummaries) {
  const sum = key => laneSummaries.reduce((total, lane) => total + lane[key], 0);
  const failed = sum('failed');
  return {
    ok: failed === 0,
    laneCount: laneSummaries.length,
    requested: sum('requested'),
    attempted: sum('attempted'),
    journaled: sum('journaled'),
    failed,
    skipped: sum('skipped'),
    lanes: laneSummaries,
  };
}

/**
 * Run one or two capture lanes. Items are sequential inside each lane and the
 * lanes run concurrently. captureItem must perform the durable journal append
 * before resolving; database synchronization remains a separate caller step.
 * The caller must still assign physically independent worker directories. This
 * module compares lexical absolute paths only; the existing writer remains
 * responsible for rejecting symlinks/junctions and unsafe journal locations.
 */
export async function runCaptureBatch(lanes, {captureItem, writeIssue} = {}) {
  if (typeof captureItem !== 'function' || typeof writeIssue !== 'function') {
    throw new TypeError('captureItem and writeIssue must be explicitly injected functions');
  }
  const assignedLanes = validateAndSnapshot(lanes);
  for (const lane of assignedLanes) {
    if (inFlightTabs.has(lane.tabKey)) throw new Error(`Tab is already active in another batch: ${lane.name}`);
    if (inFlightWorkDirectories.has(lane.workDirectoryKey)) {
      throw new Error(`Worker directory is already active in another batch: ${lane.name}`);
    }
  }
  for (const lane of assignedLanes) {
    inFlightTabs.add(lane.tabKey);
    inFlightWorkDirectories.add(lane.workDirectoryKey);
  }

  try {
    const settled = await Promise.allSettled(
      assignedLanes.map(lane => runLane(lane, captureItem, writeIssue)),
    );

    const laneSummaries = settled.map((result, index) => result.status === 'fulfilled'
      ? result.value
      : result.reason?.laneSummary ?? laneSummary(assignedLanes[index], 0, 0, [{
        index: 0,
        title: '',
        url: '',
        capture: errorSummary(result.reason),
      }]));
    const summary = batchSummary(laneSummaries);
    const fatalErrors = settled.filter(result => result.status === 'rejected').map(result => result.reason);
    if (fatalErrors.length) {
      const failure = new AggregateError(
        fatalErrors,
        'One or more capture-lane diagnostics could not be persisted',
        {cause: fatalErrors[0]},
      );
      Object.defineProperty(failure, 'summary', {value: summary, enumerable: true});
      throw failure;
    }
    return summary;
  } finally {
    for (const lane of assignedLanes) {
      inFlightTabs.delete(lane.tabKey);
      inFlightWorkDirectories.delete(lane.workDirectoryKey);
    }
  }
}
