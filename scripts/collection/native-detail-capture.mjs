// Two-phase adapter for native CUA, which cannot import this local Node module.
// The owner executes the generated read-only code through cua_repl. Python then
// retrieves that exact native receipt; this process only validates and journals
// the returned DOM. It never connects to or impersonates a browser.
import {fileURLToPath} from 'node:url';
import path from 'node:path';
import {captureDetail, DEFAULT_SELECTORS, readDetailDom} from './detail-capture.mjs';
import {appendObservation} from './capture-journal.mjs';

export function nativeDetailCode({owner, batch, url, tabVariable, tabId, start = null,
  codeFormat = 'function', segmentSize = 1000}) {
  if (![100, 1000].includes(segmentSize)) throw new Error('Unsupported native segment size');
  if (!/^[A-Za-z_$][\w$]*$/.test(tabVariable) || !/^\d+$/.test(tabId) ||
      (start !== null && (!Number.isSafeInteger(start) || start < 0 || start % segmentSize))) {
    throw new Error('Expected an existing tab binding and an exact segment boundary');
  }
  if (!['function', 'string', 'compact', 'compact-v2'].includes(codeFormat)) throw new Error('Unsupported native code format');
  const args = {selectors: DEFAULT_SELECTORS, ...(start === null ? {} : {start, limit: segmentSize})};
  const binding = {schema: 'xxmanmi-native-detail-v1', owner, batch, expectedUrl: url,
    tabId, start, ...(segmentSize === 1000 ? {} : {segmentSize})};
  // The documented evaluate string form preserves the exact reader, including
  // comments and whitespace, in one physical line for reliable native transfer.
  // A string is evaluated as an expression, so invoke the reader inside it;
  // a bare function declaration would return undefined instead of the DOM.
  // This exact reader has standalone comments and no multiline string literals.
  // Flatten its lines with separating spaces, without changing tokens or adding
  // a second string-escaping layer. This is not a general JavaScript minifier.
  let reader = ['compact', 'compact-v2'].includes(codeFormat)
    ? readDetailDom.toString().split('\n').map(line => line.trim())
      .filter(line => line && !line.startsWith('//')).join(' ')
    : readDetailDom.toString();
  // Preserve prior receipt formats. Change only two known object expressions
  // whose comma spacing was repeatedly lost in literal tool-argument transfer.
  // The importer still requires exact equality with this generated code.
  if (codeFormat === 'compact-v2') {
    for (const [original, replacement] of [
      ['writable: true, enumerable: true', 'writable: true,enumerable: true'],
      ['return {...header, groupCounts, lineLabels};', 'return {...header,groupCounts, lineLabels};'],
    ]) {
      if (reader.split(original).length !== 2) throw new Error('Native reader transfer anchor changed');
      reader = reader.replace(original, replacement);
    }
  }
  const evaluation = codeFormat === 'string'
    ? JSON.stringify(`(${readDetailDom.toString()})(${JSON.stringify(args)})`)
    : `${reader},${JSON.stringify(args)}`;
  const separator = codeFormat === 'function' ? '\n' : '';
  return `if (String(${tabVariable}.id) !== ${JSON.stringify(tabId)}) throw new Error("Owned tab binding changed");${separator}` +
    `nodeRepl.write(JSON.stringify({...${JSON.stringify(binding)},observedAt:new Date().toISOString(),` +
    `observation:await ${tabVariable}.playwright.evaluate(${evaluation})}));`;
}

export async function journalNativeDetail(evidence, item, work, {preview = false} = {}) {
  const segmentSize = evidence.segmentSize ?? 1000;
  if (![100, 1000].includes(segmentSize)) throw new Error('Unsupported native segment size');
  const metadata = evidence.metadata.observation;
  const segments = evidence.segments.map(part => part.observation);
  if (segments.length !== Math.ceil(metadata.episodeDomCount / segmentSize)) {
    throw new Error('Missing or extra native DOM segments');
  }
  let cursor = 0;
  let previewObservation;
  // Null is intentional: there is no browser object in this process. readDom
  // supplies only the authenticated observations collected by the native tool.
  const result = await captureDetail(null, item, work, {
    alreadyLoaded: true,
    chunkSize: segmentSize,
    navigate: async () => { throw new Error('Native receipt importer cannot navigate'); },
    readDom: async (_unused, reader, args) => {
      if (reader !== readDetailDom) throw new Error('Unexpected DOM reader');
      if (cursor++ === 0) {
        if (args.start !== undefined) throw new Error('Expected metadata first');
        return metadata;
      }
      const part = segments[cursor - 2];
      if (!part || part.start !== args.start || args.limit !== segmentSize) {
        throw new Error('Native DOM segment order differs from formal capture');
      }
      return part;
    },
    writeObservation: async (kind, observation, directory) => {
      if (cursor !== segments.length + 1) throw new Error('Unconsumed native DOM evidence');
      // Keep the actual observation time and a durable link to the native receipt.
      observation.collectedAt = evidence.metadata.observedAt;
      if (evidence.schema === 'xxmanmi-playwright-detail-evidence-v1') {
        observation.playwrightEvidence = evidence.evidencePath;
        observation.playwrightJobId = evidence.jobId;
      } else {
        observation.nativeEvidence = evidence.evidencePath;
        observation.nativeSourceCalls = evidence.sourceCalls;
      }
      if (preview) { previewObservation = observation; return {kind}; }
      return appendObservation(kind, observation, directory);
    },
  });
  return preview ? {...result, observation: previewObservation} : result;
}

if (process.argv[1] && fileURLToPath(import.meta.url).toLowerCase() === path.resolve(process.argv[1]).toLowerCase()) {
  let inputText = '';
  for await (const chunk of process.stdin) inputText += chunk;
  const input = JSON.parse(inputText);
  if (input.action === 'reader') process.stdout.write(JSON.stringify({code: readDetailDom.toString(), selectors: DEFAULT_SELECTORS}));
  else if (input.action === 'code') process.stdout.write(JSON.stringify({code: nativeDetailCode(input)}));
  else if (['journal','preview'].includes(input.action)) {
    const result = await journalNativeDetail(input.evidence, input.item, input.work,
      {preview: input.action === 'preview'});
    process.stdout.write(JSON.stringify({ok: true, journaled: 1, ...result}));
  } else throw new Error('Unsupported native detail adapter action');
}
