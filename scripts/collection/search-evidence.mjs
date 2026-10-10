// Structured evidence from public DOM only. The high-level action helper performs
// one validated locator click; it never fills input, chooses a channel by itself,
// or declares unobserved pagination exhausted.
import {saveEvidence} from './capture-journal.mjs';
import {lstat, open, realpath} from 'node:fs/promises';
import {createHash, randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const SEARCH_ACTION_CAPABILITY = Symbol('xxmm-search-action');
const preparedSearchActions = new Map();
let preparingSearchActions = 0;
const DEFAULT_RUNTIME = fileURLToPath(new URL('../../Datebase/xxmanmi-resource-db/.runtime/', import.meta.url));

async function readBoundedTerminalFile(filename, maxBytes) {
  const entry = await lstat(filename);
  if (!entry.isFile() || entry.isSymbolicLink() || entry.nlink !== 1) throw new Error('Unsafe terminal search file');
  const file = await open(filename, 'r');
  try {
    const identity = await file.stat();
    if (identity.dev !== entry.dev || identity.ino !== entry.ino || identity.size > maxBytes) {
      throw new Error('Terminal search file changed or exceeds the bounded read limit');
    }
    const buffer = Buffer.alloc(identity.size + 1);
    let offset = 0;
    while (offset < buffer.length) {
      const {bytesRead} = await file.read(buffer, offset, buffer.length - offset, offset);
      if (!bytesRead) break;
      offset += bytesRead;
    }
    if (offset !== identity.size) throw new Error('Terminal search file changed during read');
    return buffer.subarray(0, offset);
  } finally {
    await file.close();
  }
}
// Search changes the result pane in place.  Do not wait for a navigation after
// the normal UI click: completion remains gated by the later public DOM read.
const SEARCH_CLICK_OPTIONS = Object.freeze({timeoutMs: 5000, noWaitAfter: true});

function assertSearchTask(task) {
  for (const key of ['task_id', 'owner', 'query', 'channel']) {
    if (typeof task?.[key] !== 'string' || !task[key].trim()) throw new Error(`Search task requires ${key}`);
  }
}

// Persistent Node sessions cache ESM by URL. Callers use this marker to verify
// that a cache-busted import includes the current Browser timeoutMs contract.
export const SEARCH_EVIDENCE_API_VERSION = 4;

function isClickTimeout(error) {
  const message = String(error?.message || error || '');
  return error?.name === 'TimeoutError' || /(?:timed?\s*out|timeout)/i.test(message);
}

export function readSearchDom({selectedChannelSelector, emptyResultSelector = null, loadingSelector = null}) {
  if (typeof selectedChannelSelector !== 'string' || !selectedChannelSelector.trim()) {
    throw new Error('Supply the currently inspected selector for selected channel labels');
  }
  const visible = element => element.getClientRects().length > 0 && !element.closest('[hidden],[aria-hidden="true"]');
  const list = document.querySelector('#searchList');
  const links = Array.from(list?.querySelectorAll('a[href]') || []).filter(visible);
  const pagination = Array.from(document.querySelectorAll('a,button,botton')).filter(element =>
    visible(element) && /下一页|下页|加载更多|更多结果|next/i.test(element.textContent || '') &&
    !element.hasAttribute('disabled') && element.getAttribute('aria-disabled') !== 'true');
  const loading = loadingSelector ? Array.from(document.querySelectorAll(loadingSelector)).filter(visible) : [];
  return {
    observed_at: new Date().toISOString(),
    page_url: location.href,
    input_value_after_action: document.querySelector('#searchInput')?.value ?? null,
    selected_channels: Array.from(document.querySelectorAll(selectedChannelSelector)).filter(visible)
      .map(element => (element.textContent || '').trim()),
    result_count: links.length,
    visible_detail_links: links.slice(0, 200).map(anchor => ({
      url: anchor.href,
      title: Array.from(anchor.querySelector('.weui-media-box__bd')?.childNodes || [])
        .filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim(),
      status: (anchor.querySelector('p')?.textContent || '').trim(),
    })),
    empty_result_visible: !!emptyResultSelector && Array.from(document.querySelectorAll(emptyResultSelector)).some(visible),
    pagination_pending: pagination.length > 0,
    result_container_visible: !!list && visible(list),
    loading_indicator: loadingSelector ? {selector: loadingSelector, visible_count: loading.length,
      visible_texts: loading.map(element => (element.textContent || '').trim())} : null,
  };
}

export function readSearchActionDom({actionSelector}) {
  if (typeof actionSelector !== 'string' || !actionSelector.trim()) {
    throw new Error('Supply the currently inspected search action selector');
  }
  const visible = element => element.getClientRects().length > 0 && !element.closest('[hidden],[aria-hidden="true"]');
  const matches = Array.from(document.querySelectorAll(actionSelector));
  return {
    observed_at: new Date().toISOString(),
    page_url: location.href,
    input_value: document.querySelector('#searchInput')?.value ?? null,
    total_count: matches.length,
    visible_controls: matches.filter(visible).map(element => ({
      tag: element.tagName,
      text: (element.textContent || '').trim(),
      onclick: element.getAttribute('onclick') || '',
      disabled: element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true',
    })),
  };
}

export function readSearchPreparationDom({actionSelector, selectedChannelSelector,
  emptyResultSelector = null, loadingSelector = null}) {
  if (typeof actionSelector !== 'string' || !actionSelector.trim() ||
      typeof selectedChannelSelector !== 'string' || !selectedChannelSelector.trim()) {
    throw new Error('Supply inspected action and selected-channel selectors');
  }
  const visible = element => element.getClientRects().length > 0 && !element.closest('[hidden],[aria-hidden="true"]');
  const actionMatches = Array.from(document.querySelectorAll(actionSelector));
  const list = document.querySelector('#searchList');
  const links = Array.from(list?.querySelectorAll('a[href]') || []).filter(visible);
  const pagination = Array.from(document.querySelectorAll('a,button,botton')).filter(element =>
    visible(element) && /下一页|下页|加载更多|更多结果|next/i.test(element.textContent || '') &&
    !element.hasAttribute('disabled') && element.getAttribute('aria-disabled') !== 'true');
  const loading = loadingSelector ? Array.from(document.querySelectorAll(loadingSelector)).filter(visible) : [];
  const observedAt = new Date().toISOString();
  const inputValue = document.querySelector('#searchInput')?.value ?? null;
  return {
    action: {observed_at: observedAt, page_url: location.href, input_value: inputValue,
      total_count: actionMatches.length,
      visible_controls: actionMatches.filter(visible).map(element => ({tag: element.tagName,
        text: (element.textContent || '').trim(), onclick: element.getAttribute('onclick') || '',
        disabled: element.hasAttribute('disabled') || element.getAttribute('aria-disabled') === 'true'}))},
    before: {observed_at: observedAt, page_url: location.href, input_value_after_action: inputValue,
      selected_channels: Array.from(document.querySelectorAll(selectedChannelSelector)).filter(visible)
        .map(element => (element.textContent || '').trim()), result_count: links.length,
      visible_detail_links: links.slice(0, 200).map(anchor => ({url: anchor.href,
        title: Array.from(anchor.querySelector('.weui-media-box__bd')?.childNodes || [])
          .filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim(),
        status: (anchor.querySelector('p')?.textContent || '').trim()})),
      empty_result_visible: !!emptyResultSelector &&
        Array.from(document.querySelectorAll(emptyResultSelector)).some(visible),
      pagination_pending: pagination.length > 0, result_container_visible: !!list && visible(list),
      loading_indicator: loadingSelector ? {selector: loadingSelector, visible_count: loading.length,
        visible_texts: loading.map(element => (element.textContent || '').trim())} : null},
  };
}

// Arm this before the normal browser click when the site's loading indicator is
// too short-lived for a second host-side call to observe. The observer only reads
// the public DOM; it neither fills the input nor dispatches the search action.
export function beginSearchDomObservation({selectedChannelSelector, emptyResultSelector = null,
  loadingSelector = null, sampleIntervalMs = 10, timeoutMs = 15000}) {
  if (typeof selectedChannelSelector !== 'string' || !selectedChannelSelector.trim()) {
    throw new Error('Supply the currently inspected selector for selected channel labels');
  }
  if (typeof loadingSelector !== 'string' || !loadingSelector.trim()) {
    throw new Error('A currently inspected loading selector is required for transition observation');
  }
  if (!Number.isSafeInteger(sampleIntervalMs) || sampleIntervalMs < 5 || sampleIntervalMs > 100) {
    throw new Error('sampleIntervalMs must be an integer from 5 through 100');
  }
  if (!Number.isSafeInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 30000) {
    throw new Error('timeoutMs must be an integer from 1000 through 30000');
  }

  const stateKey = '__xxmmSearchDomObservationV1__';
  const prior = window[stateKey];
  if (prior) {
    // Also clean observations armed by the previous module implementation.
    prior.observer?.disconnect();
    prior.active = false;
    if (prior.intervalId !== null && typeof window.clearInterval === 'function') {
      window.clearInterval(prior.intervalId);
    }
    if (prior.timerId !== null && typeof window.clearTimeout === 'function') {
      window.clearTimeout(prior.timerId);
    }
    if (prior.deadlineId != null && typeof window.clearTimeout === 'function') {
      window.clearTimeout(prior.deadlineId);
    }
    delete window[stateKey];
  }
  const visible = element => element.getClientRects().length > 0 && !element.closest('[hidden],[aria-hidden="true"]');
  const read = () => {
    const list = document.querySelector('#searchList');
    const links = Array.from(list?.querySelectorAll('a[href]') || []).filter(visible);
    const pagination = Array.from(document.querySelectorAll('a,button,botton')).filter(element =>
      visible(element) && /下一页|下页|加载更多|更多结果|next/i.test(element.textContent || '') &&
      !element.hasAttribute('disabled') && element.getAttribute('aria-disabled') !== 'true');
    const loading = Array.from(document.querySelectorAll(loadingSelector)).filter(visible);
    return {
      observed_at: new Date().toISOString(),
      page_url: location.href,
      input_value_after_action: document.querySelector('#searchInput')?.value ?? null,
      selected_channels: Array.from(document.querySelectorAll(selectedChannelSelector)).filter(visible)
        .map(element => (element.textContent || '').trim()),
      result_count: links.length,
      visible_detail_links: links.slice(0, 200).map(anchor => ({
        url: anchor.href,
        title: Array.from(anchor.querySelector('.weui-media-box__bd')?.childNodes || [])
          .filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim(),
        status: (anchor.querySelector('p')?.textContent || '').trim(),
      })),
      empty_result_visible: !!emptyResultSelector &&
        Array.from(document.querySelectorAll(emptyResultSelector)).some(visible),
      pagination_pending: pagination.length > 0,
      result_container_visible: !!list && visible(list),
      loading_indicator: {selector: loadingSelector, visible_count: loading.length,
        visible_texts: loading.map(element => (element.textContent || '').trim())},
    };
  };

  const observationId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const tag = (snapshot, phase) => ({...snapshot, observation_id: observationId,
    observation_phase: phase});
  const state = {observationId, read, observer: null, intervalId: null, timerId: null,
    deadlineId: null, active: true, timedOut: false, error: null,
    before: tag(read(), 'before'), loadingSnapshot: null};
  const startedAt = Date.now();
  const cleanup = () => {
    state.active = false;
    state.observer?.disconnect();
    state.observer = null;
    if (state.intervalId !== null) window.clearInterval(state.intervalId);
    if (state.timerId !== null) window.clearTimeout(state.timerId);
    if (state.deadlineId !== null) window.clearTimeout(state.deadlineId);
    state.intervalId = state.timerId = state.deadlineId = null;
  };
  state.cleanup = cleanup;
  const expire = () => { state.timedOut = true; cleanup(); };
  const sample = () => {
    if (!state.active) return;
    if (Date.now() - startedAt >= timeoutMs) { expire(); return; }
    try {
      const snapshot = read();
      if (!state.loadingSnapshot && snapshot.loading_indicator.visible_count > 0) {
        state.loadingSnapshot = tag(snapshot, 'loading');
      }
    } catch (error) {
      state.error = error;
      cleanup();
    }
  };
  try {
    const Observer = window.MutationObserver;
    if (typeof Observer === 'function') {
      state.observer = new Observer(sample);
      state.observer.observe(document.documentElement, {subtree: true, childList: true,
        attributes: true, attributeFilter: ['class', 'style', 'hidden', 'aria-hidden']});
    }
    const hasTimeout = typeof window.setTimeout === 'function' && typeof window.clearTimeout === 'function';
    if (hasTimeout) state.deadlineId = window.setTimeout(expire, timeoutMs);
    if (typeof window.setInterval === 'function' && typeof window.clearInterval === 'function') {
      state.intervalId = window.setInterval(sample, sampleIntervalMs);
    } else if (hasTimeout) {
      const schedule = () => {
        state.timerId = window.setTimeout(() => {
          state.timerId = null;
          sample();
          if (state.active) schedule();
        }, sampleIntervalMs);
      };
      schedule();
    } else {
      throw new Error('This browser context has no supported DOM sampling timer');
    }
    Object.defineProperty(window, stateKey, {value: state, configurable: true});
  } catch (error) {
    cleanup();
    throw error;
  }
  return {observationId, beforeResultCount: state.before.result_count};
}

export function finishSearchDomObservation({observationId}) {
  const stateKey = '__xxmmSearchDomObservationV1__';
  const state = window[stateKey];
  if (!state || typeof observationId !== 'string' || state.observationId !== observationId) {
    throw new Error('Search DOM observation is missing or does not match this action');
  }
  try {
    if (state.error) throw state.error;
    const after = {...state.read(), observation_id: observationId, observation_phase: 'after'};
    return {before: state.before, loadingSnapshot: state.loadingSnapshot, after,
      timedOut: state.timedOut === true};
  } finally {
    // The DOM read itself can throw (detached page, invalidated selector, etc.).
    if (typeof state.cleanup === 'function') state.cleanup();
    else {
      state.active = false;
      state.observer?.disconnect();
      if (state.intervalId != null && typeof window.clearInterval === 'function') window.clearInterval(state.intervalId);
      if (state.timerId != null && typeof window.clearTimeout === 'function') window.clearTimeout(state.timerId);
    }
    delete window[stateKey];
  }
}

// Restricted browser contexts may expose timers but make window non-extensible.
// Keep the observation state inside one pending evaluation while the caller uses
// the approved browser API for the real UI action.
export function observeSearchDomDuringAction({selectedChannelSelector, emptyResultSelector = null,
  loadingSelector = null, sampleIntervalMs = 10, timeoutMs = 15000}) {
  if (typeof selectedChannelSelector !== 'string' || !selectedChannelSelector.trim()) {
    throw new Error('Supply the currently inspected selector for selected channel labels');
  }
  if (typeof loadingSelector !== 'string' || !loadingSelector.trim()) {
    throw new Error('A currently inspected loading selector is required for transition observation');
  }
  if (!Number.isSafeInteger(sampleIntervalMs) || sampleIntervalMs < 5 || sampleIntervalMs > 100 ||
      !Number.isSafeInteger(timeoutMs) || timeoutMs < 1000 || timeoutMs > 30000 ||
      typeof window.setTimeout !== 'function') {
    throw new Error('Search observation requires supported 5-100ms sampling and 1-30s timeout');
  }
  const visible = element => element.getClientRects().length > 0 && !element.closest('[hidden],[aria-hidden="true"]');
  const read = () => {
    const list = document.querySelector('#searchList');
    const links = Array.from(list?.querySelectorAll('a[href]') || []).filter(visible);
    const pagination = Array.from(document.querySelectorAll('a,button,botton')).filter(element =>
      visible(element) && /下一页|下页|加载更多|更多结果|next/i.test(element.textContent || '') &&
      !element.hasAttribute('disabled') && element.getAttribute('aria-disabled') !== 'true');
    const loading = Array.from(document.querySelectorAll(loadingSelector)).filter(visible);
    return {
      observed_at: new Date().toISOString(),
      page_url: location.href,
      input_value_after_action: document.querySelector('#searchInput')?.value ?? null,
      selected_channels: Array.from(document.querySelectorAll(selectedChannelSelector)).filter(visible)
        .map(element => (element.textContent || '').trim()),
      result_count: links.length,
      visible_detail_links: links.slice(0, 200).map(anchor => ({
        url: anchor.href,
        title: Array.from(anchor.querySelector('.weui-media-box__bd')?.childNodes || [])
          .filter(node => node.nodeType === 3).map(node => node.textContent).join('').trim(),
        status: (anchor.querySelector('p')?.textContent || '').trim(),
      })),
      empty_result_visible: !!emptyResultSelector &&
        Array.from(document.querySelectorAll(emptyResultSelector)).some(visible),
      pagination_pending: pagination.length > 0,
      result_container_visible: !!list && visible(list),
      loading_indicator: {selector: loadingSelector, visible_count: loading.length,
        visible_texts: loading.map(element => (element.textContent || '').trim())},
    };
  };

  const observationId = `${Date.now()}-${Math.random().toString(16).slice(2)}`;
  const tag = (snapshot, phase) => ({...snapshot, observation_id: observationId,
    observation_phase: phase});
  const before = tag(read(), 'before');
  let loadingSnapshot = null;
  let transitionSnapshot = null;
  const startedAt = Date.now();
  const changedFromBefore = snapshot =>
    JSON.stringify(snapshot.visible_detail_links) !== JSON.stringify(before.visible_detail_links) ||
    snapshot.empty_result_visible !== before.empty_result_visible;
  return new Promise((resolve, reject) => {
    const sample = () => {
      try {
        const snapshot = read();
        if (!loadingSnapshot && snapshot.loading_indicator.visible_count > 0) {
          loadingSnapshot = tag(snapshot, 'loading');
        }
        if (!transitionSnapshot && changedFromBefore(snapshot)) {
          transitionSnapshot = tag(snapshot, 'transition');
        }
        const stablePublicResult = snapshot.loading_indicator.visible_count === 0 &&
          (snapshot.visible_detail_links.length > 0 || snapshot.empty_result_visible === true);
        if ((loadingSnapshot && snapshot.loading_indicator.visible_count === 0) ||
            (!loadingSnapshot && transitionSnapshot && stablePublicResult) ||
            Date.now() - startedAt >= timeoutMs) {
          resolve({before, loadingSnapshot, transitionSnapshot,
            after: tag(snapshot, 'after'), timedOut: Date.now() - startedAt >= timeoutMs});
          return;
        }
        window.setTimeout(sample, sampleIntervalMs);
      } catch (error) {
        reject(error);
      }
    };
    window.setTimeout(sample, sampleIntervalMs);
  });
}

export async function observeSearchAction({tab, searchOptions, triggerAction,
  armDelayMs = 100, observationTimeoutMs = 1800}) {
  if (!tab?.playwright || typeof tab.playwright.evaluate !== 'function' ||
      typeof triggerAction !== 'function') {
    throw new Error('observeSearchAction requires the approved tab and a normal UI action');
  }
  if (!Number.isSafeInteger(armDelayMs) || armDelayMs < 0 || armDelayMs > 1000) {
    throw new Error('armDelayMs must be an integer from 0 through 1000');
  }
  const observing = Promise.resolve(tab.playwright.evaluate(observeSearchDomDuringAction,
    {...searchOptions, timeoutMs: observationTimeoutMs}));
  let observationFailed = false;
  let observationError;
  // Attach immediately: evaluation can reject while the host-side arming delay
  // is still pending. A trigger error must not leave an unhandled rejection.
  observing.catch(error => { observationFailed = true; observationError = error; });
  await new Promise(resolve => setTimeout(resolve, armDelayMs));
  if (observationFailed) throw observationError;
  try {
    await triggerAction();
  } catch (error) {
    Promise.resolve(observing).catch(() => {});
    throw error;
  }
  return await observing;
}

export function finalizeTimedOutSearchObservation(observed, snapshot) {
  const observationId = observed?.before?.observation_id;
  if (observed?.timedOut !== true || typeof observationId !== 'string' || !observationId ||
      !snapshot || typeof snapshot.observed_at !== 'string') {
    throw new Error('Only a timed-out observed search can be finalized with a later DOM snapshot');
  }
  return {...observed, after: {...snapshot, observation_id: observationId,
    observation_phase: 'after'}};
}

function observedUiAction(task, before, after, uiAction) {
  if (uiAction?.capability !== SEARCH_ACTION_CAPABILITY || uiAction.taskId !== task.task_id ||
      uiAction.before !== before || uiAction.after !== after ||
      uiAction.control?.text !== task.channel || typeof uiAction.control?.onclick !== 'string' ||
      !uiAction.control.onclick.trim() ||
      before?.input_value_after_action !== task.query ||
      JSON.stringify(after?.selected_channels) !== JSON.stringify([task.channel])) return false;
  const channelChanged = JSON.stringify(before.selected_channels) !==
    JSON.stringify(after.selected_channels);
  const resultsChanged = JSON.stringify(before.visible_detail_links) !==
      JSON.stringify(after.visible_detail_links) ||
    before.empty_result_visible !== after.empty_result_visible;
  const loadingChanged = typeof before.loading_indicator?.selector === 'string' &&
    before.loading_indicator.selector === after.loading_indicator?.selector &&
    Number.isSafeInteger(before.loading_indicator.visible_count) &&
    Number.isSafeInteger(after.loading_indicator.visible_count) &&
    before.loading_indicator.visible_count !== after.loading_indicator.visible_count;
  if (!channelChanged && !resultsChanged && !loadingChanged) return false;
  const times = [uiAction.beforeAt, uiAction.actionStartedAt, uiAction.actionCompletedAt,
    uiAction.afterAt].map(value => Date.parse(value));
  return times.every(Number.isFinite) && times[0] <= times[1] && times[1] <= times[2] &&
    times[2] <= times[3];
}

function observedResultStateTransition(task, before, transition, after) {
  if (!transition || transition.result_container_visible !== true ||
      transition.pagination_pending !== false ||
      !Array.isArray(transition.visible_detail_links) ||
      transition.result_count !== transition.visible_detail_links.length ||
      transition.page_url !== after.page_url || transition.input_value_after_action !== task.query ||
      JSON.stringify(transition.selected_channels) !== JSON.stringify([task.channel]) ||
      (transition.loading_indicator && transition.loading_indicator.visible_count !== 0)) return false;
  const observationIds = [before?.observation_id, transition.observation_id, after.observation_id];
  if (observationIds.some(value => typeof value !== 'string' || !value) ||
      new Set(observationIds).size !== 1 || before.observation_phase !== 'before' ||
      transition.observation_phase !== 'transition' || after.observation_phase !== 'after') return false;
  if (JSON.stringify(transition.visible_detail_links) === JSON.stringify(before.visible_detail_links) &&
      transition.empty_result_visible === before.empty_result_visible) return false;
  const times = [before.observed_at, transition.observed_at, after.observed_at].map(value => Date.parse(value));
  return times.every(Number.isFinite) && times[0] <= times[1] && times[1] <= times[2];
}

function observedLoadingTransition(task, before, loading, after) {
  const initial = loading?.loading_indicator;
  const final = after.loading_indicator;
  if (!initial || !final || typeof initial.selector !== 'string' || !initial.selector.trim() ||
      initial.selector !== final.selector ||
      (before?.loading_indicator && before.loading_indicator.visible_count !== 0) ||
      !Number.isSafeInteger(initial.visible_count) || initial.visible_count < 1 ||
      !Array.isArray(initial.visible_texts) || initial.visible_texts.length !== initial.visible_count ||
      initial.visible_texts.some(value => typeof value !== 'string') ||
      final.visible_count !== 0 || !Array.isArray(final.visible_texts) || final.visible_texts.length !== 0 ||
      loading.page_url !== after.page_url || loading.input_value_after_action !== task.query ||
      JSON.stringify(loading.selected_channels) !== JSON.stringify([task.channel])) return false;
  const observationIds = [before?.observation_id, loading.observation_id, after.observation_id];
  if (observationIds.some(value => value !== undefined) &&
      (observationIds.some(value => typeof value !== 'string' || !value) ||
       new Set(observationIds).size !== 1 || before.observation_phase !== 'before' ||
       loading.observation_phase !== 'loading' || after.observation_phase !== 'after')) return false;
  const times = [before?.observed_at, loading.observed_at, after.observed_at].map(value => Date.parse(value));
  return times.every(Number.isFinite) && times[0] <= times[1] && times[1] <= times[2];
}

export function buildSearchEvidence(task, snapshot,
  {before, loadingSnapshot, transitionSnapshot, uiAction} = {}) {
  assertSearchTask(task);
  if (!snapshot || snapshot.input_value_after_action !== task.query ||
      JSON.stringify(snapshot.selected_channels) !== JSON.stringify([task.channel])) {
    throw new Error('Public input value and selected channel must exactly match the claimed search');
  }
  const page = new URL(snapshot.page_url);
  if (!['http:', 'https:'].includes(page.protocol) || page.username || page.password ||
      !/(^|\.)xxmanmi\.com$/i.test(page.hostname)) throw new Error('Search evidence must come from the current xxmanmi page');
  if (snapshot.result_container_visible !== true || snapshot.pagination_pending !== false) {
    throw new Error('Search result container or pagination requires inspection');
  }
  if (snapshot.loading_indicator && snapshot.loading_indicator.visible_count !== 0) throw new Error('Search is still visibly loading');
  const links = snapshot.visible_detail_links;
  if (!Array.isArray(links) || links.length > 200 || !Number.isSafeInteger(snapshot.result_count) ||
      snapshot.result_count !== links.length) throw new Error('Search DOM result count is incomplete or truncated');
  if (links.length === 0 && snapshot.empty_result_visible !== true) throw new Error('Zero results require a visible empty-result indicator');
  const loadingTransition = observedLoadingTransition(task, before, loadingSnapshot, snapshot);
  const resultStateTransition = observedResultStateTransition(task, before, transitionSnapshot, snapshot);
  const verifiedUiAction = observedUiAction(task, before, snapshot, uiAction);
  if (!before || !Array.isArray(before.visible_detail_links) ||
      (JSON.stringify(before.visible_detail_links) === JSON.stringify(links) &&
       before.empty_result_visible === snapshot.empty_result_visible &&
       !loadingTransition && !resultStateTransition && !verifiedUiAction)) {
    throw new Error('Search results did not visibly change from the pre-action snapshot; inspect stale or identical results');
  }
  const seen = new Set();
  const cleaned = links.map(link => {
    const url = new URL(link?.url);
    if (!['http:', 'https:'].includes(url.protocol) || url.hash || url.username || url.password ||
        !/(^|\.)xxmanmi\.com$/i.test(url.hostname) || url.href !== link.url ||
        typeof link.title !== 'string' || !link.title.trim() || typeof link.status !== 'string' || seen.has(link.url)) {
      throw new Error('Invalid, duplicate, or incomplete public detail link');
    }
    seen.add(link.url);
    return {url: link.url, title: link.title, status: link.status};
  });
  return {task_id: task.task_id, owner: task.owner, query: task.query, channel: task.channel,
    category: task.category || '', collected_at: new Date().toISOString(),
    input_value_after_action: snapshot.input_value_after_action,
    selected_channels: [...snapshot.selected_channels], result_count: cleaned.length,
    visible_detail_links: cleaned,
    refresh_evidence: loadingTransition ? {kind: 'observed_loading_transition',
      before_at: before.observed_at, loading_at: loadingSnapshot.observed_at, done_at: snapshot.observed_at,
      loading_indicator: loadingSnapshot.loading_indicator, done_indicator: snapshot.loading_indicator,
    } : resultStateTransition ? {kind: 'observed_result_state_transition',
      before_at: before.observed_at, transition_at: transitionSnapshot.observed_at,
      done_at: snapshot.observed_at, intermediate_result_count: transitionSnapshot.result_count,
      intermediate_empty_result_visible: transitionSnapshot.empty_result_visible,
    } : verifiedUiAction ? {kind: 'verified_ui_action', before_at: uiAction.beforeAt,
      action_started_at: uiAction.actionStartedAt, action_completed_at: uiAction.actionCompletedAt,
      done_at: uiAction.afterAt, control: {tag: uiAction.control.tag, text: uiAction.control.text},
    } : {kind: 'changed_public_results'}};
}

export async function saveSearchEvidence(task, snapshot, workDirectory,
  {before, loadingSnapshot, transitionSnapshot, uiAction, ...writerOptions} = {}) {
  const evidence = buildSearchEvidence(task, snapshot,
    {before, loadingSnapshot, transitionSnapshot, uiAction});
  const saved = await saveEvidence(evidence, workDirectory, writerOptions);
  return {...saved, task_id: task.task_id, result_count: evidence.result_count};
}

export async function runSearchActionEvidence({tab, task, workDirectory, searchOptions,
  actionSelector, settleDelayMs = 250, minimumSettleMs = 1000, maxSettleReads = 8,
  ...writerOptions}) {
  assertSearchTask(task);
  if (!tab?.playwright || typeof tab.playwright.evaluate !== 'function' ||
      typeof tab.playwright.locator !== 'function') {
    throw new Error('runSearchActionEvidence requires the approved browser tab');
  }
  if (!Number.isSafeInteger(settleDelayMs) || settleDelayMs < 50 || settleDelayMs > 1000 ||
      !Number.isSafeInteger(minimumSettleMs) || minimumSettleMs < settleDelayMs ||
      minimumSettleMs > 5000 || !Number.isSafeInteger(maxSettleReads) ||
      maxSettleReads < 1 || maxSettleReads > 12) {
    throw new Error('Invalid bounded search settle policy');
  }
  const controlSnapshot = await tab.playwright.evaluate(readSearchActionDom, {actionSelector});
  const controls = controlSnapshot?.visible_controls;
  if (controlSnapshot?.input_value !== task?.query || controlSnapshot.total_count !== 1 ||
      !Array.isArray(controls) || controls.length !== 1 || controls[0].text !== task.channel ||
      controls[0].disabled || !controls[0].onclick.trim()) {
    throw new Error('Search action must be one visible enabled inspected control for the claimed channel');
  }
  const before = await tab.playwright.evaluate(readSearchDom, searchOptions);
  const actionStartedAt = new Date().toISOString();
  let actionCompletedAt = null;
  let clickTimedOut = false;
  try {
    await tab.playwright.locator(actionSelector).click(SEARCH_CLICK_OPTIONS);
    actionCompletedAt = new Date().toISOString();
  } catch (error) {
    if (!isClickTimeout(error)) throw error;
    // The Browser adapter may dispatch the click and then time out while waiting
    // for a page-load event that this in-page search never emits.  Do not replay
    // the action and do not treat the timeout itself as proof; only a later public
    // DOM transition may make this attempt admissible.
    clickTimedOut = true;
  }
  const settleStarted = Date.now();
  let after = null;
  for (let index = 0; index < maxSettleReads; index += 1) {
    await new Promise(resolve => setTimeout(resolve, settleDelayMs));
    after = await tab.playwright.evaluate(readSearchDom, searchOptions);
    const stable = after?.result_container_visible === true && after.pagination_pending === false &&
      (!after.loading_indicator || after.loading_indicator.visible_count === 0) &&
      (after.result_count > 0 || after.empty_result_visible === true);
    if (stable && Date.now() - settleStarted >= minimumSettleMs) break;
  }
  const uiAction = clickTimedOut ? undefined :
    {capability: SEARCH_ACTION_CAPABILITY, taskId: task.task_id, before, after,
      beforeAt: before.observed_at, actionStartedAt, actionCompletedAt,
      afterAt: after?.observed_at, control: controls[0]};
  return await saveSearchEvidence(task, after, workDirectory, {before, uiAction, ...writerOptions});
}

export async function prepareSearchAction({tab, task, workDirectory, searchOptions,
  actionSelector, ...writerOptions}) {
  assertSearchTask(task);
  if (!tab?.playwright || typeof tab.playwright.evaluate !== 'function' ||
      typeof tab.playwright.locator !== 'function' || preparedSearchActions.size + preparingSearchActions >= 16) {
    throw new Error('prepareSearchAction requires an approved tab and an available action slot');
  }
  // Reserve before the first await so concurrent callers cannot overrun the cap.
  preparingSearchActions += 1;
  try {
    const prepared = await tab.playwright.evaluate(readSearchPreparationDom,
      {...searchOptions, actionSelector});
    const controls = prepared?.action?.visible_controls;
    const page = new URL(prepared?.before?.page_url);
    if (!['http:', 'https:'].includes(page.protocol) || page.username || page.password ||
        !/(^|\.)xxmanmi\.com$/i.test(page.hostname) || prepared.action.input_value !== task?.query ||
        prepared.before.input_value_after_action !== task.query || prepared.action.total_count !== 1 ||
        !Array.isArray(controls) || controls.length !== 1 || controls[0].text !== task.channel ||
        controls[0].disabled || !controls[0].onclick.trim() ||
        prepared.before.result_container_visible !== true || prepared.before.pagination_pending !== false ||
        prepared.before.result_count !== prepared.before.visible_detail_links?.length) {
      throw new Error('Search preparation does not match one complete claimed public UI state');
    }
    const actionId = randomUUID();
    preparedSearchActions.set(actionId, {status: 'prepared', tab, task: {...task}, workDirectory,
      searchOptions: {...searchOptions}, actionSelector, writerOptions, before: prepared.before,
      control: controls[0], operationPending: false});
    return {actionId, task_id: task.task_id, next_action: 'execute_search_action'};
  } finally {
    preparingSearchActions -= 1;
  }
}

// Cancels only this unexecuted local capability, not its durable search claim.
// A caller must still use the workflow CLI to resolve/transfer that claim.
export function cancelPreparedSearchAction({tab, actionId}) {
  const prepared = preparedSearchActions.get(actionId);
  if (!prepared || prepared.tab !== tab || prepared.status !== 'prepared' || prepared.operationPending) {
    throw new Error('Only an unexecuted prepared action can be cancelled on its original tab');
  }
  preparedSearchActions.delete(actionId);
  return {actionId, task_id: prepared.task.task_id, status: 'cancelled_before_execution',
    claim_resolved: false};
}

// Read the actual workflowctl terminal ledger, never trust an in-memory boolean
// or a receipt alone. This does not create quarantine records, authorize replay,
// close tabs, or release actions whose host operation is still pending.
export async function releasePreparedSearchAction({tab, actionId, terminalReceipt, terminalEvidencePath}) {
  const prepared = preparedSearchActions.get(actionId);
  if (!prepared || prepared.tab !== tab || prepared.operationPending ||
      !['action_completed', 'action_unknown', 'action_failed'].includes(prepared.status)) {
    throw new Error('Only a settled prepared action on its original tab can be terminally released');
  }
  prepared.operationPending = true;
  try {
    const runtimeRoot = prepared.writerOptions.runtimeRoot || DEFAULT_RUNTIME;
    const expectedLedger = path.resolve(runtimeRoot, 'title-discovery', 'search-claim-abandonments.json');
    if (terminalReceipt?.ok !== true || typeof terminalReceipt.ledger !== 'string' ||
        path.resolve(terminalReceipt.ledger) !== expectedLedger ||
        typeof terminalReceipt.abandonment_id !== 'string' || !terminalReceipt.abandonment_id ||
        ['task_id', 'query', 'channel'].some(key => terminalReceipt[key] !== prepared.task[key])) {
      throw new Error('Release requires a matching workflow terminal receipt from the runtime ledger');
    }
    if (await realpath(path.dirname(expectedLedger)) !== path.join(await realpath(runtimeRoot), 'title-discovery')) {
      throw new Error('Unsafe terminal search ledger');
    }
    const ledger = JSON.parse((await readBoundedTerminalFile(expectedLedger, 16 * 1024 * 1024)).toString('utf8'));
    const records = ledger?.version === 1 && Array.isArray(ledger.abandonments) ?
      ledger.abandonments.filter(record => record?.abandonment_id === terminalReceipt.abandonment_id) : [];
    const record = records.length === 1 ? records[0] : null;
    const genericQuarantine = record?.reason === 'quarantined_unverifiable_result';
    const identifiedQuarantine = ['quarantined_foreign_host_session', 'quarantined_foreign_host_stale_results',
      'quarantined_foreign_host_input_mismatch', 'quarantined_foreign_host_tab_gone',
      'quarantined_confirmed_click_tab_gone'].includes(record?.reason);
    if (!record || ['task_id', 'owner', 'query', 'channel'].some(key => record[key] !== prepared.task[key]) ||
        record.replay_authorized !== false || record.logs_stopped !== true ||
        record.action_state !== 'completed_unverifiable' || record.commit_receipt_exists !== false ||
        record.search_journal_contains_task !== false || record.original_module_instance_available !== true ||
        !/^[a-f0-9]{64}$/.test(record.source_claim_sha256 || '') ||
        !/^[a-f0-9]{64}$/.test(record.evidence_sha256 || '') ||
        (!genericQuarantine && !identifiedQuarantine) ||
        (record.action_id != null && record.action_id !== actionId) ||
        (identifiedQuarantine && (record.action_id !== actionId || prepared.status !== 'action_completed')) ||
        (prepared.status !== 'action_completed' && record.public_ui_completed !== true)) {
      throw new Error('Terminal ledger does not prove this action is resolved without replay');
    }
    // The generic CLI ledger historically omits action_id. Bind its original
    // evidence bytes (already hashed by workflowctl) instead of allowing one
    // task-level receipt to release another action on the same task/tab. Legacy
    // evidence without an exact action ID cannot authorize releasing a handle.
    if (genericQuarantine) {
      const relative = typeof terminalEvidencePath === 'string' ?
        path.relative(path.resolve(prepared.workDirectory), path.resolve(terminalEvidencePath)) : '';
      if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
        throw new Error('Release requires action-bound terminal evidence inside the original worker directory');
      }
      const evidencePath = path.resolve(terminalEvidencePath);
      if (await realpath(evidencePath) !== path.join(await realpath(prepared.workDirectory), relative)) {
        throw new Error('Unsafe terminal search evidence path');
      }
      const bytes = await readBoundedTerminalFile(evidencePath, 1024 * 1024);
      if (createHash('sha256').update(bytes).digest('hex') !== record.evidence_sha256) {
        throw new Error('Terminal evidence hash does not match the workflow ledger');
      }
      const evidence = JSON.parse(bytes.toString('utf8').replace(/^\uFEFF/, ''));
      const action = evidence?.search_action;
      if (evidence.version !== 1 || evidence.reason !== record.reason ||
          ['task_id', 'owner', 'query', 'channel'].some(key => evidence[key] !== prepared.task[key]) ||
          evidence.claim_sha256 !== record.source_claim_sha256 || evidence.logs_stopped !== true ||
          action?.action_id !== actionId || action.action_attempts !== 1 ||
          action.state !== 'completed_unverifiable' || action.replay_authorized !== false ||
          action.original_module_instance_available !== true ||
          (prepared.status !== 'action_completed' && action.public_ui_completed !== true)) {
        throw new Error('Terminal evidence does not identify this exact resolved action');
      }
    }
    preparedSearchActions.delete(actionId);
    return {actionId, task_id: prepared.task.task_id, status: 'released_terminal',
      abandonment_id: record.abandonment_id, replay_authorized: false};
  } finally {
    prepared.operationPending = false;
  }
}

export async function executePreparedSearchAction({tab, actionId}) {
  const prepared = preparedSearchActions.get(actionId);
  if (!prepared || prepared.tab !== tab || prepared.status !== 'prepared') {
    throw new Error('Prepared search action is missing, belongs to another tab, or was already executed');
  }
  prepared.status = 'action_started';
  prepared.actionStartedAt = new Date().toISOString();
  try {
    await tab.playwright.locator(prepared.actionSelector).click(SEARCH_CLICK_OPTIONS);
  } catch (error) {
    if (isClickTimeout(error)) {
      prepared.status = 'action_unknown';
      return {actionId, task_id: prepared.task.task_id, next_action: 'inspect_search_action',
        reason: 'click_timeout'};
    }
    prepared.status = 'action_failed';
    throw error;
  }
  prepared.actionCompletedAt = new Date().toISOString();
  prepared.status = 'action_completed';
  return {actionId, task_id: prepared.task.task_id, next_action: 'finalize_search_action'};
}

export async function finalizePreparedSearchAction({tab, actionId, settleDelayMs = 1000}) {
  const prepared = preparedSearchActions.get(actionId);
  if (!prepared || prepared.tab !== tab || prepared.operationPending ||
      !['action_completed', 'action_unknown'].includes(prepared.status) ||
      !Number.isSafeInteger(settleDelayMs) || settleDelayMs < 250 || settleDelayMs > 5000) {
    throw new Error('Only one completed or timed-out prepared action can be inspected on its original tab');
  }
  prepared.operationPending = true;
  try {
    await new Promise(resolve => setTimeout(resolve, settleDelayMs));
    const after = await tab.playwright.evaluate(readSearchDom, prepared.searchOptions);
    const uiAction = prepared.status === 'action_completed' ?
      {capability: SEARCH_ACTION_CAPABILITY, taskId: prepared.task.task_id,
        before: prepared.before, after, beforeAt: prepared.before.observed_at,
        actionStartedAt: prepared.actionStartedAt, actionCompletedAt: prepared.actionCompletedAt,
        afterAt: after?.observed_at, control: prepared.control} : undefined;
    const saved = await saveSearchEvidence(prepared.task, after, prepared.workDirectory,
      {before: prepared.before, uiAction, ...prepared.writerOptions});
    preparedSearchActions.delete(actionId);
    return saved;
  } finally {
    // On failure retain the original proof for read-only inspection or formal
    // quarantine, but unlock the handle for a subsequent non-overlapping call.
    prepared.operationPending = false;
  }
}
