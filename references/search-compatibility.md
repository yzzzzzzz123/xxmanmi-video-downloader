# 搜索接口兼容：特定宿主按需读取

默认按 [搜索短流程](search-workflow.md)使用三段动作；只有明确宿主限制需要其他现有入口时读取本文件，不作为默认上下文。接口仍须API v4、真实DOM与原claim证据，不绕过浏览器权限。

在批准的浏览器 REPL 导入 `scripts/collection/search-evidence.mjs`，按当前页面核实选中渠道及空结果/加载中选择器。`claim` 为 CLI JSON 返回值，`SEARCH_MODULE_URL` 为上述模块实际 file URL。持久会话会缓存同一 ESM URL；代码刚更新时必须在没有在途 `actionId` 的安全检查点按文件 mtime 破缓存导入，并核对 API 版本。已经 prepare 的 `actionId` 只存在于原模块实例，必须继续用原实例 inspect/finalize，不能为重载而重放 execute。以下快照分开在对应 UI 阶段执行；未观察加载提示时不伪造 `loadingSnapshot`：

```javascript
var xxmmSearchFs = await import('node:fs/promises');
var xxmmSearchUrl = new URL(SEARCH_MODULE_URL);
xxmmSearchUrl.searchParams.set('mtime', String((await xxmmSearchFs.stat(xxmmSearchUrl)).mtimeMs));
var xxmmSearch = await import(xxmmSearchUrl.href);
if (xxmmSearch.SEARCH_EVIDENCE_API_VERSION !== 4) throw new Error('search-evidence module is stale');
var xxmmSearchOptions = {selectedChannelSelector, emptyResultSelector, loadingSelector};
var before = await tab.playwright.evaluate(xxmmSearch.readSearchDom, xxmmSearchOptions);
// 用当前批准工具正常输入查询词、点击真实渠道；界面出现加载提示时才取：
var loadingSnapshot = await tab.playwright.evaluate(xxmmSearch.readSearchDom, xxmmSearchOptions);
// 现场确认结果更新且加载结束后另一次调用：
var after = await tab.playwright.evaluate(xxmmSearch.readSearchDom, xxmmSearchOptions);
console.log(await xxmmSearch.saveSearchEvidence({...claim,owner:OWNER}, after, claim.work,
  {before, loadingSnapshot}));
```

若站点 loading 短于宿主两次调用间隔，使用单次待决的页面内只读观察，并让模块并发
执行调用方提供的批准浏览器 UI 动作。观察状态只存在于这次调用的闭包内，不写 `window`
或 DOM；模块不填词、不选择渠道，`triggerAction` 必须是现场核实后的正常浏览器点击：

```javascript
var observed = await xxmmSearch.observeSearchAction({
  tab,
  searchOptions: xxmmSearchOptions,
  triggerAction: () => tab.playwright.locator(ACTION_SELECTOR).click()
});
// 受限宿主的页面执行窗口约 3 秒；模块默认 1.8 秒返回。若 timedOut，先现场确认 UI 已完成，
// 再读取一次真实 DOM 并绑定为同一次观察的最终快照；没有过渡证据时仍不会放行：
if (observed.timedOut) {
  var finalSnapshot = await tab.playwright.evaluate(xxmmSearch.readSearchDom, xxmmSearchOptions);
  observed = xxmmSearch.finalizeTimedOutSearchObservation(observed, finalSnapshot);
}
console.log(await xxmmSearch.saveSearchEvidence({...claim,owner:OWNER}, observed.after, claim.work,
  {before: observed.before, loadingSnapshot: observed.loadingSnapshot,
    transitionSnapshot: observed.transitionSnapshot}));
```

观察器默认在一次待决调用内每 10ms 用页面 `setTimeout` 采样，只保留 before、首个真实
可见 loading、首个公开结果状态变化和 after，不回显采样流，也不要求页面全局对象可扩展。
连续零结果可以用“空提示可见→暂时隐藏→重新可见”的完整公开 DOM 周期证明刷新；观察器
不会在中间空提示刚隐藏时提前收口。各阶段带同一 observation id，混用不同点击的快照
仍被门禁拒绝。
若观察器也没有捕获 loading 且结果未变化，仍须停写或回到 CUA 核实，不能按空结果放行。

当前受限浏览器若把同标签动作与待决观察串行化，或不提供可持久化观察状态，使用模块的
高层动作入口。它先核实 selector 只命中一个可见、启用、带真实 handler 且文本等于 claim
渠道的控件，再通过批准的 `locator.click()` 点击一次；随后按有界时间重复读取公开 DOM，
稳定后直接保存证据。相同零结果只会以 `verified_ui_action` 入证，调用方不能手造该能力：

```javascript
console.log(await xxmmSearch.runSearchActionEvidence({
  tab,
  task: {...claim, owner: OWNER},
  workDirectory: claim.work,
  searchOptions: xxmmSearchOptions,
  actionSelector: ACTION_SELECTOR
}));
```

`ACTION_SELECTOR` 必须是本轮现场核实的真实渠道按钮 CSS selector；模块不猜 selector、
不填搜索词、不切换任务、不访问网络接口。控件歧义、禁用、文字/handler 不符、最终空态
无可见提示、分页未尽或结果不完整仍拒绝保存。
调用前必须把正式 claim 的 `task_id`、`query`、`channel` 与本线程 `owner` 一起传入；当前API v4在浏览器读取/点击前检查这些字段，防止点击后才缺owner。模块版本变化时同步示例并核验实际导出，不删除版本门禁。
