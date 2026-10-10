// Durable local writers for explicit worker directories. No database/network I/O.
import {lstat, mkdir, open, readFile, realpath, unlink} from 'node:fs/promises';
import {randomUUID} from 'node:crypto';
import {fileURLToPath} from 'node:url';
import path from 'node:path';

const RUNTIME = fileURLToPath(new URL('../../Datebase/xxmanmi-resource-db/.runtime/', import.meta.url));

async function workerDirectory(workDirectory, runtimeRoot) {
  if (typeof workDirectory !== 'string' || !workDirectory.trim()) throw new TypeError('Explicit workDirectory required');
  const root = path.resolve(runtimeRoot);
  const relative = path.relative(root, path.resolve(workDirectory));
  if (!relative || relative === '..' || relative.startsWith('..' + path.sep) || path.isAbsolute(relative)) {
    throw new Error('Worker directory must be strictly inside the database runtime root');
  }
  let directory = await realpath(root);
  for (const component of relative.split(path.sep)) {
    directory = path.join(directory, component);
    await mkdir(directory).catch(error => { if (error.code !== 'EEXIST') throw error; });
    const entry = await lstat(directory);
    if (entry.isSymbolicLink() || !entry.isDirectory()) throw new Error('Unsafe worker directory');
  }
  return directory;
}

async function safeFile(filename) {
  try {
    const entry = await lstat(filename);
    if (entry.isSymbolicLink() || !entry.isFile() || entry.nlink > 1) throw new Error('Unsafe capture journal file');
  } catch (error) { if (error.code !== 'ENOENT') throw error; }
}

function validateObservation(kind, record) {
  if (!record || typeof record !== 'object' || typeof record.collectedAt !== 'string' ||
      !/(?:Z|[+-]\d{2}:\d{2})$/.test(record.collectedAt) || Number.isNaN(Date.parse(record.collectedAt))) {
    throw new Error('Observation requires a timestamp with timezone');
  }
  if (kind === 'navigation-issues') return;
  if (!['details', 'raw-details'].includes(kind)) throw new Error('Unsupported capture journal');
  const url = new URL(record.url);
  if (!['http:', 'https:'].includes(url.protocol) || !record.title || !record.source ||
      !Number.isSafeInteger(record.episodeDomCount) || record.episodeDomCount <= 0) throw new Error('Incomplete detail observation');
  if (kind === 'raw-details') {
    if (!record.reason || !Array.isArray(record.rawEntries) || record.rawEntries.length !== record.episodeDomCount ||
        record.rawEntries.some(entry => ['label', 'group', 'dataWeb'].some(key => typeof entry?.[key] !== 'string')) ||
        !Array.isArray(record.lineLabels) || record.lineLabels.some(label => typeof label?.for !== 'string' || typeof label.text !== 'string')) {
      throw new Error('Incomplete raw DOM observation');
    }
    return;
  }
  const placeholders = record.invalidDomEntries || [];
  if (!Array.isArray(record.episodes) || !record.episodes.length || !Array.isArray(placeholders) ||
      record.episodes.length + placeholders.length !== record.episodeDomCount) throw new Error('DOM result truncated');
  for (const episode of record.episodes) {
    const media = new URL(episode.url);
    if (!['http:', 'https:'].includes(media.protocol) || /(^|\.)xxmanmi\.com$/i.test(media.hostname) ||
        !/\.(m3u8|mp4)$/i.test(media.pathname) || typeof episode.label !== 'string' || typeof episode.line !== 'string') {
      throw new Error('Invalid original media record');
    }
  }
  for (const entry of placeholders) {
    if (['label', 'group', 'dataWeb', 'line', 'reason'].some(key => typeof entry?.[key] !== 'string') ||
        !entry.reason.trim() || !/^(?:undefined|null|nan)?$/i.test(entry.dataWeb.trim())) throw new Error('Invalid DOM placeholder evidence');
  }
}

// File identity lets us release our own partially written lock when write/sync
// fails, while preserving an unknown or replaced lock. io is injected by offline
// fault tests; production callers use only the checked worker path above.
export async function withCaptureLock(lockPath, action, io = {open, lstat, unlink}) {
  const token = randomUUID();
  const lock = await io.open(lockPath, 'wx'); // Busy/crashed lock requires inspection; never steal it.
  let identity;
  try {
    identity = await lock.stat();
    await lock.writeFile(token, 'utf8');
    await lock.sync();
    return await action();
  } finally {
    await lock.close();
    if (identity) {
      const current = await io.lstat(lockPath);
      if (current.isSymbolicLink() || current.dev !== identity.dev || current.ino !== identity.ino) {
        throw new Error('Capture writer lock changed; refusing release');
      }
      await io.unlink(lockPath);
    }
  }
}

export async function appendObservation(kind, observation, workDirectory, {runtimeRoot = RUNTIME} = {}) {
  validateObservation(kind, observation);
  const directory = await workerDirectory(workDirectory, runtimeRoot);
  const lockPath = path.join(directory, '.capture-helper.lock');
  return withCaptureLock(lockPath, async () => {
    const filename = path.join(directory, `${kind}.ndjson`);
    await safeFile(filename);
    const stream = await open(filename, 'a+');
    try {
      const stat = await stream.stat();
      if (stat.size) {
        const tail = Buffer.alloc(1);
        await stream.read(tail, 0, 1, stat.size - 1);
        if (tail[0] !== 10) throw new Error('Journal has an unfinished trailing line; preserve it and inspect before appending');
      }
      await stream.writeFile(JSON.stringify(observation) + '\n', 'utf8');
      await stream.sync();
    } finally { await stream.close(); }
    return {kind, journalPath: filename};
  });
}

export async function writeIssue(issue, workDirectory, options) {
  return appendObservation('navigation-issues', issue, workDirectory, options);
}

export async function saveEvidence(evidence, workDirectory, {runtimeRoot = RUNTIME} = {}) {
  if (!/^[A-Za-z0-9][A-Za-z0-9_.-]{0,179}$/.test(evidence?.task_id || '')) throw new Error('Unsafe evidence task ID');
  const directory = await workerDirectory(workDirectory, runtimeRoot);
  const filename = path.join(directory, `${evidence.task_id}-evidence.json`);
  await safeFile(filename);
  const data = JSON.stringify(evidence) + '\n';
  let stream;
  try { stream = await open(filename, 'wx'); }
  catch (error) {
    if (error.code !== 'EEXIST' || await readFile(filename, 'utf8') !== data) throw error;
    return {evidencePath: filename, alreadySaved: true};
  }
  try { await stream.writeFile(data, 'utf8'); await stream.sync(); } finally { await stream.close(); }
  return {evidencePath: filename, alreadySaved: false};
}
