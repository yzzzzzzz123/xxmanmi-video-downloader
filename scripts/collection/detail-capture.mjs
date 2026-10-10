// Reads only the caller's already-approved browser tab. No browser creation,
// hidden state, fetch, playback, retry, database writes, or background service.
import {runCaptureBatch} from './capture-batch.mjs';
import {appendObservation, writeIssue} from './capture-journal.mjs';
import {captureDetailWithRecovery} from './capture-with-recovery.mjs';

const DEFAULT_SELECTORS = Object.freeze({
  title: '#video', episodes: '#list a[data-web]',
  groups: '[class*="playList"]', lines: 'label[for]',
});

function pageUrl(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) {
    throw new Error('Expected an observed HTTP(S) detail URL');
  }
  url.hash = '';
  return url.href;
}

function decodeNumericCharacterReferences(value) {
  return value.replace(/&#(?:([0-9]{1,7})|x([0-9a-f]{1,6}));/gi, (original, decimal, hex) => {
    const codePoint = Number.parseInt(decimal ?? hex, hex === undefined ? 10 : 16);
    return codePoint > 0 && codePoint <= 0x10ffff &&
      (codePoint < 0xd800 || codePoint > 0xdfff) ? String.fromCodePoint(codePoint) : original;
  });
}

function comparableTitle(value) {
  // Some detail headings display a literal &amp; while their list link shows &.
  // Normalize only for comparison; keep the observed title and assigned item.
  return decodeNumericCharacterReferences(value).replace(/&amp;/g, '&')
    .replace(/[\s\u200B]/g, '');
}

function comparableQueueTitle(value) {
  // Public list links can include an episode-status badge after the title.
  // Preserve the assigned text; strip only the observed trailing badge here.
  const title = decodeNumericCharacterReferences(value).trim()
    .replace(/\s+(?:更新至|全)[0-9]+集$/u, '');
  return comparableTitle(title);
}

// Self-contained function suitable for the approved tab.playwright.evaluate.
// Selectors match the previously verified xxmanmi public detail DOM. A caller
// must inspect changed layouts before supplying different selectors.
export function readDetailDom({selectors, start = null, limit = 1000}) {
  const nodes = Array.from(document.querySelectorAll(selectors.episodes));
  const groupOf = node => String(node.closest(selectors.groups)?.className || '');
  const lineLabels = Array.from(document.querySelectorAll(selectors.lines))
    .map(label => ({for: label.getAttribute('for') || '',
      text: (label.querySelector('p')?.textContent || label.textContent || '').trim()}));
  // Two independent 32-bit rolling hashes catch same-count DOM replacements
  // without returning complete URL lists for every segment. Length-prefixed
  // values retain field/node boundaries. This is consistency detection, not an
  // authenticity/security digest; the approved browser remains the authority.
  let hashA = 2166136261;
  let hashB = 5381;
  let characters = 0;
  const fingerprintValue = value => {
    const text = `${value.length}:${value}`;
    characters += text.length;
    for (let index = 0; index < text.length; index += 1) {
      const code = text.charCodeAt(index);
      hashA = Math.imul(hashA ^ code, 16777619);
      hashB = Math.imul(hashB, 33) ^ code;
    }
  };
  for (const node of nodes) {
    fingerprintValue(node.textContent || '');
    fingerprintValue(groupOf(node));
    fingerprintValue(node.getAttribute('data-web') ?? '');
  }
  for (const label of lineLabels) { fingerprintValue(label.for); fingerprintValue(label.text); }
  const header = {
    url: location.href,
    title: (document.querySelector(selectors.title)?.textContent || '').trim(),
    episodeDomCount: nodes.length,
    fingerprint: `${characters}:${(hashA >>> 0).toString(16)}:${(hashB >>> 0).toString(16)}`,
  };
  if (start !== null) {
    return {...header, start, entries: nodes.slice(start, start + limit).map((node, index) => ({
      index: start + index,
      label: (node.textContent || '').trim(),
      group: groupOf(node), dataWeb: node.getAttribute('data-web'),
    }))};
  }
  const groupCounts = Object.create(null);
  for (const node of nodes) {
    const group = groupOf(node);
    Object.defineProperty(groupCounts, group, {
      value: (groupCounts[group] || 0) + 1, writable: true, enumerable: true, configurable: true,
    });
  }
  return {...header, groupCounts, lineLabels};
}

function validateHeader(observed, item, count = null) {
  if (!observed || pageUrl(observed.url) !== pageUrl(item.url)) {
    throw new Error('Detail URL does not match the assigned queue item');
  }
  if (typeof observed.title !== 'string' || !observed.title.trim()) throw new Error('Missing DOM program title');
  // The site sometimes wraps its visible title in U+200B zero-width spaces.
  if (item.title && comparableQueueTitle(item.title) !== comparableTitle(observed.title)) {
    // Keep the exact legacy message so the existing conditional recovery helper
    // can wrap this function without broadening its one-retry rule.
    throw new Error('DOM program title does not match the assigned queue item');
  }
  if (!Number.isSafeInteger(observed.episodeDomCount) || observed.episodeDomCount <= 0 ||
      (count !== null && observed.episodeDomCount !== count)) {
    throw new Error('DOM episode count is empty or changed; refusing completion');
  }
}

function lineFor(group, labels) {
  const groupNumber = group.split(/\s+/).map(value => /^playList(\d+)$/.exec(value)).find(Boolean)?.[1];
  if (groupNumber === undefined) return '';
  const matches = new Set(labels.filter(label => /^\d+$/.test(label.for) && Number(label.for) === Number(groupNumber))
    .map(label => label.text).filter(Boolean));
  return matches.size === 1 ? [...matches][0] : '';
}

function mediaUrl(raw, page) {
  // Numeric resource IDs and placeholders are never expanded into URLs.
  if (!/^(?:https?:\/\/|\/\/)/i.test(raw)) return null;
  try {
    const parsed = new URL(raw, page);
    if (!['http:', 'https:'].includes(parsed.protocol) || parsed.username || parsed.password ||
        /(^|\.)xxmanmi\.com$/i.test(parsed.hostname) || !/\.(m3u8|mp4)$/i.test(parsed.pathname)) return null;
    return raw.startsWith('//') ? parsed.href : raw;
  } catch { return null; }
}

/** Capture one exact observed page. Resolve only after durable journal writing.
 * readDom(tab, fn, args) and navigate(tab, url) may adapt another approved tool;
 * neither callback may use a separate browser, HTTP scraper, or hidden API.
 */
export async function captureDetail(tab, item, workDirectory, {
  alreadyLoaded = false,
  selectors = DEFAULT_SELECTORS,
  chunkSize = 1000,
  readDom = (boundTab, fn, args) => boundTab.playwright.evaluate(fn, args),
  navigate = (boundTab, url) => boundTab.goto(url),
  writeObservation = appendObservation,
} = {}) {
  pageUrl(item?.url);
  if (typeof workDirectory !== 'string' || !workDirectory.trim()) throw new TypeError('Explicit workDirectory required');
  if (typeof alreadyLoaded !== 'boolean' || !Number.isInteger(chunkSize) || chunkSize < 1 || chunkSize > 1000) {
    throw new TypeError('alreadyLoaded must be boolean; chunkSize must be 1..1000');
  }
  if ([readDom, navigate, writeObservation].some(value => typeof value !== 'function') ||
      Object.keys(DEFAULT_SELECTORS).some(key => typeof selectors?.[key] !== 'string' || !selectors[key].trim())) {
    throw new TypeError('Explicit valid browser callbacks and DOM selectors are required');
  }
  if (!alreadyLoaded) await navigate(tab, item.url);
  const metadata = await readDom(tab, readDetailDom, {selectors});
  validateHeader(metadata, item);
  if (typeof metadata.fingerprint !== 'string' || !metadata.fingerprint) throw new Error('Missing DOM consistency fingerprint');
  if (!Array.isArray(metadata.lineLabels) || metadata.lineLabels.some(label =>
    typeof label?.for !== 'string' || typeof label.text !== 'string')) throw new Error('Incomplete DOM line labels');
  if (!metadata.groupCounts || typeof metadata.groupCounts !== 'object') throw new Error('Missing DOM group counts');
  const rawEntries = [];
  const groups = new Map();
  for (let start = 0; start < metadata.episodeDomCount; start += chunkSize) {
    const part = await readDom(tab, readDetailDom, {selectors, start, limit: chunkSize});
    validateHeader(part, item, metadata.episodeDomCount);
    if (part.fingerprint !== metadata.fingerprint || part.title !== metadata.title || part.start !== start || !Array.isArray(part.entries) ||
        part.entries.length !== Math.min(chunkSize, metadata.episodeDomCount - start)) {
      throw new Error('DOM segment changed or truncated; refusing completion');
    }
    for (let index = 0; index < part.entries.length; index += 1) {
      const entry = part.entries[index];
      if (entry?.index !== start + index || ['label', 'group', 'dataWeb'].some(key => typeof entry[key] !== 'string')) {
        throw new Error('DOM segment index or entry is incomplete; refusing completion');
      }
      groups.set(entry.group, (groups.get(entry.group) || 0) + 1);
      rawEntries.push({label: entry.label, group: entry.group, dataWeb: entry.dataWeb});
    }
  }
  if (Object.keys(metadata.groupCounts).length !== groups.size ||
      [...groups].some(([group, count]) => metadata.groupCounts[group] !== count)) {
    throw new Error('DOM group counts changed; refusing completion');
  }
  const observation = {
    url: pageUrl(metadata.url), title: decodeNumericCharacterReferences(metadata.title).replace(/\u200B/g, '').trim(), status: item.status || '',
    source: item.source || '小小漫迷', category: item.category || '', query: item.query || '',
    collectedAt: new Date().toISOString(), episodeDomCount: metadata.episodeDomCount,
    lineLabels: metadata.lineLabels, domFingerprint: metadata.fingerprint,
  };
  const episodes = [];
  const invalidDomEntries = [];
  let unresolved = false;
  for (const entry of rawEntries) {
    const line = lineFor(entry.group, metadata.lineLabels);
    const url = mediaUrl(entry.dataWeb, observation.url);
    if (url) episodes.push({...entry, line, url});
    else if (/^(?:undefined|null|nan)?$/i.test(entry.dataWeb.trim())) {
      invalidDomEntries.push({...entry, line, reason: 'Observed empty/undefined DOM placeholder'});
    } else unresolved = true;
  }
  const kind = episodes.length && !unresolved ? 'details' : 'raw-details';
  if (kind === 'details') Object.assign(observation, {episodes, invalidDomEntries});
  else Object.assign(observation, {rawEntries, lineLabels: metadata.lineLabels,
    reason: 'Public DOM contains unresolved or unsupported resource values; no media URL was inferred'});
  await writeObservation(kind, observation, workDirectory);
  return {outcome: kind === 'details' ? 'captured' : 'raw', episodeDomCount: observation.episodeDomCount,
    mediaCount: kind === 'details' ? episodes.length : 0, placeholderCount: invalidDomEntries.length};
}

/** One collector = one lane. Success summaries never contain episode URLs. */
export async function runDetailBatch(lane, options = {}) {
  if (!lane || Array.isArray(lane)) throw new TypeError('Pass exactly one assigned detail lane');
  if (options.recoverTransient !== undefined && typeof options.recoverTransient !== 'boolean') {
    throw new TypeError('recoverTransient must be boolean when supplied');
  }
  // The promoted recovery inspector uses the established default tab/DOM API.
  // An adapted browser or custom selector must never silently fall back to that
  // API; such callers get their exact capture adapter with no implicit retry.
  const customBrowser = ['readDom', 'navigate', 'selectors'].some(key => options[key] !== undefined);
  const useRecovery = options.recoverTransient !== false && !customBrowser;
  const reportIssue = options.writeIssue || writeIssue;
  const capture = (tab, item, work, state) => captureDetail(tab, item, work, {...options, ...state});
  let captured = 0;
  let raw = 0;
  const summary = await runCaptureBatch([lane], {
    captureItem: async (tab, item, work, state) => {
      const result = useRecovery
        ? await captureDetailWithRecovery(tab, item, work, {
          captureDetail: capture, reportIssue, alreadyLoaded: state.alreadyLoaded,
        })
        : await capture(tab, item, work, state);
      if (result.outcome === 'captured') captured += 1;
      else raw += 1;
    },
    writeIssue: reportIssue,
  });
  return {...summary, captured, raw,
    recoveryMode: useRecovery ? 'single-transient' : customBrowser ? 'custom-browser-no-retry' : 'disabled'};
}

/** Keep the claimed batch intact while bounding each host call to one page. */
export async function runDetailBatchSegment(lane, options = {}) {
  if (!lane || Array.isArray(lane) || !Array.isArray(lane.items) || !lane.items.length) {
    throw new TypeError('Pass one lane with its remaining claimed items');
  }
  const urls = lane.items.map(item => pageUrl(item?.url));
  if (new Set(urls).size !== urls.length) throw new Error('Duplicate detail URL in claimed segment input');
  const summary = await runDetailBatch({...lane, items: lane.items.slice(0, 1)}, options);
  const remainingItems = lane.items.slice(1).map(item => ({...item}));
  let handoffError;
  // Browser-owned tabs are otherwise closed when this native turn finishes.
  // Keep only the live page needed by this unfinished claimed workflow, not
  // routine search/research tabs or an already completed capture.
  if ((summary.failed || remainingItems.length) && typeof lane.tab?.markHandoff === 'function') {
    try { await lane.tab.markHandoff(); }
    catch (error) { handoffError = String(error?.message || error); }
  }
  return {...summary, deferred: remainingItems.length, remainingItems,
    ...(handoffError ? {tab_handoff_error: handoffError} : {}),
    next_action: handoffError ? 'inspect_handoff_failure'
      : summary.failed ? 'inspect_detail_failure'
      : remainingItems.length ? 'capture_detail_segment' : 'complete_detail'};
}
