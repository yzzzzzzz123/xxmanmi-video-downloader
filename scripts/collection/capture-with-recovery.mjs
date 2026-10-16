// Only orchestrates injected capture/issue functions and the supplied connected tab.
// No browser creation, media requests, database access, filesystem I/O, or imports.
const TITLE_MISMATCH = 'DOM program title does not match the assigned queue item';
const TRANSIENT_TITLE = '获取失败，请刷新页面';

function withoutFragment(value) {
  const url = new URL(value);
  if (!['http:', 'https:'].includes(url.protocol)) throw new Error('Expected an HTTP(S) page URL');
  url.hash = '';
  return url.href;
}

function errorSummary(error) {
  return {
    name: typeof error?.name === 'string' ? error.name : 'Error',
    message: String(error?.message ?? error).slice(0, 2000),
  };
}

function accessRestrictions(text) {
  const compact = text.replace(/\s+/g, '');
  const spaced = text.replace(/\s+/g, ' ');
  const indicators = [
    ['login_required', /(?:请(?:您)?(?:先)?|需要(?:先)?|必须(?:先)?|需先)(?:登录|登陆)|(?:登录|登陆)后(?:才能|方可|查看|访问|继续|观看|获取)/u, compact],
    ['verification_required', /(?:输入|填写|提交)(?:正确的|有效的|下方的?|安全|图形|图片|短信|动态)?验证码|(?:请(?:您)?|需要|必须)(?:先)?(?:提供|验证)(?:正确的|有效的)?验证码|(?:请(?:您)?|需要|必须)(?:先)?(?:完成|通过|进行)(?:人机|安全|身份|访问)?验证|人机验证|验证码(?:错误|失败|不正确|已过期)/u, compact],
    ['access_restricted', /访问受限|访问被拒绝|拒绝访问|禁止访问|权限不足|无权访问|未授权访问|请先认证|需要认证/u, compact],
    ['access_restricted', /access\s+(?:denied|restricted)|permission\s+denied|unauthori[sz]ed|forbidden|(?:login|authentication)\s+required|(?:sign|log)\s+in\s+to|verify\s+(?:that\s+)?you\s+are\s+(?:a\s+)?human|complete\s+(?:the\s+)?(?:captcha|verification)|enter\s+(?:the\s+)?(?:captcha|verification\s+code)|captcha\s+challenge/i, spaced],
  ];
  // Generic headers such as “更新验证码 / 验证码期限0天” are not access challenges.
  return indicators.flatMap(([kind, pattern, value]) => {
    const match = pattern.exec(value);
    return match ? [{kind, matchedText: match[0]}] : [];
  });
}

/**
 * Capture once, and conditionally recover one precisely observed transient page.
 * reportIssue(issue, workDirectory) must persist diagnostics before resolving.
 * The caller owns persistence/sync; returned capture summaries are unmodified.
 */
export async function captureDetailWithRecovery(tab, item, workDirectory, {
  captureDetail,
  reportIssue,
  alreadyLoaded = false,
} = {}) {
  if (typeof captureDetail !== 'function' || typeof reportIssue !== 'function') {
    throw new TypeError('captureDetail and reportIssue must be explicitly injected functions');
  }
  if (!item || typeof item !== 'object' || typeof alreadyLoaded !== 'boolean') {
    throw new TypeError('Expected a queue item and a boolean alreadyLoaded flag');
  }
  // Freeze a copy, not the shared queue item. Never alter the expected title to
  // accept a failed/different page, including when retrying the real collector.
  const queuedItem = Object.freeze({...item});
  const expected = Object.freeze({
    url: queuedItem.url ?? null,
    title: queuedItem.title ?? null,
    source: queuedItem.source ?? null,
    category: queuedItem.category ?? null,
    query: queuedItem.query ?? null,
  });

  let initialError;
  try {
    return await captureDetail(tab, queuedItem, workDirectory, {alreadyLoaded});
  } catch (error) {
    initialError = error;
  }

  let observed = null;
  let restrictions = [];
  const report = async (state, reason, error = initialError, extra = {}) => {
    const issue = {
      kind: 'detail-capture-recovery',
      collectedAt: new Date().toISOString(),
      state,
      reason,
      expected: {...expected},
      observed,
      accessRestrictions: restrictions,
      initialError: errorSummary(initialError),
      error: errorSummary(error),
      retryLimit: 1,
      retryAttempt: 0,
      ...extra,
    };
    try {
      await reportIssue(issue, workDirectory);
    } catch (reportError) {
      throw new AggregateError([error, reportError],
        'Recovery diagnostic could not be recorded; no further recovery is allowed', {cause: error});
    }
  };

  if (initialError?.message !== TITLE_MISMATCH) {
    await report('needs_review', 'non_title_error');
    throw initialError;
  }

  try {
    observed = await tab.playwright.evaluate(() => {
      const visibleText = document.body?.innerText;
      return {
        url: location.href,
        title: (document.querySelector('#video')?.textContent || '').trim(),
        episodeDomCount: document.querySelectorAll('#list a[data-web]').length,
        visibleText: typeof visibleText === 'string' ? visibleText.slice(0, 24000) : null,
        visibleTextTruncated: typeof visibleText === 'string' && visibleText.length > 24000,
      };
    });
  } catch (inspectionError) {
    const failure = new AggregateError([initialError, inspectionError],
      'Read-only recovery inspection failed; no retry was attempted', {cause: initialError});
    await report('needs_review', 'inspection_failed', failure, {
      inspectionError: errorSummary(inspectionError),
    });
    throw failure;
  }

  let refusal = null;
  if (!observed || typeof observed.url !== 'string' || typeof observed.title !== 'string'
      || !Number.isInteger(observed.episodeDomCount) || observed.episodeDomCount < 0
      || typeof observed.visibleText !== 'string'
      || typeof observed.visibleTextTruncated !== 'boolean') {
    refusal = 'invalid_dom_snapshot';
  } else {
    restrictions = accessRestrictions(observed.visibleText);
    try {
      if (withoutFragment(observed.url) !== withoutFragment(expected.url)) refusal = 'different_page_url';
    } catch {
      refusal = 'invalid_page_url';
    }
    if (!refusal && restrictions.length) refusal = 'explicit_access_restriction';
    if (!refusal && observed.visibleTextTruncated) refusal = 'visible_text_truncated';
    if (!refusal && observed.title !== TRANSIENT_TITLE) refusal = 'different_dom_title';
    if (!refusal && observed.episodeDomCount !== 0) refusal = 'nonzero_resource_count';
  }
  if (refusal) {
    await report('needs_review', refusal);
    throw initialError;
  }

  // A durable pre-attempt record is required. Failure here must not navigate.
  await report('retrying', 'same_page_transient_failure_title_with_zero_resources', initialError, {
    retryAttempt: 1,
    navigationAttempted: false,
  });
  let result;
  let failureStage = 'navigation';
  try {
    await tab.goto(queuedItem.url); // Exactly one ordinary same-URL navigation.
    failureStage = 'capture';
    result = await captureDetail(tab, queuedItem, workDirectory, {alreadyLoaded: true});
  } catch (retryError) {
    await report('needs_review', 'single_retry_failed', retryError, {
      retryAttempt: 1,
      navigationAttempted: true,
      failureStage,
    });
    throw retryError; // No recursive retry or second diagnostic/navigation loop.
  }
  await report('recovered', 'capture_succeeded_after_single_navigation', initialError, {
    retryAttempt: 1,
    navigationAttempted: true,
  });
  return result;
}

