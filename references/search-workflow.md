## 历史搜索：仅恢复原认领和证据

当前常规流程直接采集公开列表、来源与分页中已观察到的详情URL，不再准备关键词或创建榜单发现轮次。本文仅供原搜索任务恢复或安全收尾，保留原任务书、owner、query×channel claim、模块、actionId和未知工具句柄；不得据本文领取新的关键词待办或恢复旧供词流程。

搜索证据的`result_count`仍记录实际命中数量，原节目、详情URL、资源及采集门禁分别保留。采集规范已删除分类职责、入口和执行步骤，不追加类型核验、标签或系列归组；原有节目、资源、分类、系列及关联和证据保持原样。本规范修改不改程序、不归档或删表、不变更登记、不恢复暂停采集或发布生产。

指定搜索的 `commit-search` 保留全部原始命中，但入队只收与指定原词匹配的正片、季或OVA；宽泛无关片及电影解说/预告片不占优先采集。回执中的 `excluded_out_of_scope` 单列，不能算“未找到”或采集完成。原文用 `/` 明示的别名可匹配，其他名称或年份含混须按实际页面证据复核，不靠模型猜。旧在途回执及已保存观察不覆写，范围外观察不计指定名单完成。

列表发现须按当前实际 DOM 将节目名称与状态节点分离，不能把整个链接的 `textContent` 当片名。例如当前列表 `.weui-media-box__bd` 的直接文本节点为片名，子 `p.weui-media-box__desc` 为状态；先核实结构，不把样例选择器套到未观察页面。已收批的错误拼接项只在 `manual_review` 时可由正式 `coordctl.py retry` 修复：items JSON 的相应项增加 `title_correction_evidence`（本线程工作目录里的真实同页 DOM 证据文件）。证据须含 `page_url`、详情 `heading`、正整数 `listDataWeb`、`observed_at`；入口只允许从旧片名后缀分离已观察的“更新至N集/全N集/已完结/完结”，保存原项及证据哈希到重试历史，URL及旧批次结果不变。真正资源有效性仍由正式详情采集/审计/封面门禁判定，不因改名算成功。

将正式 `claim-search` 的原始 stdout 保存为本线程 UTF-8 JSON，再直接解析 `query`、`channel`、`task_id`、工作目录和任务书路径给浏览器模块；不要根据 PTY 折行显示手抄词或路径，内部空格同样受精确门禁约束。已明确执行并 finalize 的错词搜索可以保留原证据，在独立 attempt 目录有界纠正一次；从原始任务 JSON 取原词，成功的新证据仍按原 claim/taskbook 提交，不修改旧证据或放宽比较。这不授权重放结果未知的动作。

怀疑 claim、任务书或执行参数的 `query` 空格不一致时，必须程序读取原始 JSON 并逐码点比较；PTY 折行或显示插空格不能作为参数错位证据。

```powershell
python -X utf8 -B scripts/collection/workflowctl.py claim-search --thread OWNER --channel "实际渠道"
python -X utf8 -B scripts/collection/workflowctl.py commit-search --thread OWNER --evidence EVIDENCE.json
```

仅在确认本人存在未完成的同渠道原claim时使用`claim-search`恢复；没有原claim时不调用该入口领取新待办，返回直接详情采集。先执行返回的 `next_action`：若为 `commit_search`，直接用其 evidence 重入提交，不重新浏览、搜索或覆盖证据；`browser_search` 才进入浏览器。任务书与搜索 claim 保留既有格式，不建立另一队列。搜索只用当前实际 UI 渠道；输入动作与结果更新必须现场核实。

## 初始化一次，保留原模块

核实当前公开DOM的输入、唯一渠道按钮和选中/加载/空态选择器，按正式claim原值填词。仅无在途action时导入当前模块；原动作仍须用原实例完成。

`searchN(...)` 渠道按钮会提交搜索，不是无副作用的预选。`selectedChannelSelector` 必须指向已核实的真实选中态；初始没有匹配时如实保留 `selected_channels=[]`，`prepareSearchAction` 不要求目标渠道已选中。`actionSelector` 才指向已观察到的唯一目标渠道按钮；禁止先裸 `click` 预选，也禁止用目标按钮选择器冒充选中态。仅尚未提交的新搜索按 `prepare → execute 一次 → finalize` 前进，当前在途动作沿原句柄和实际 `next_action` 恢复，不重放。

```javascript
var xxmmSearchFs = await import('node:fs/promises');
var xxmmSearchUrl = new URL(SEARCH_MODULE_URL);
xxmmSearchUrl.searchParams.set('mtime', String((await xxmmSearchFs.stat(xxmmSearchUrl)).mtimeMs));
var xxmmSearch = await import(xxmmSearchUrl.href);
if (xxmmSearch.SEARCH_EVIDENCE_API_VERSION !== 4) throw new Error('search-evidence module is stale');
var xxmmSearchOptions = {selectedChannelSelector, emptyResultSelector, loadingSelector};
```

## 三段动作与正式提交

若宿主对单次调用有 30 秒总上限，使用同一模块实例的三段可恢复入口，避免把多次浏览器
操作塞入一个调用。第一段把私有 capability 留在模块内，第二段只点击一次，第三段只读
稳定 DOM 并保存；严格按返回的 `next_action` 前进，不重复 execute：

`executePreparedSearchAction` 内部使用当前 Browser API 的 `timeoutMs` 把单次点击限制为
5 秒；若浏览器已派发点击、但等待页面 load 时超时，返回 `inspect_search_action`，不得重放
execute。第三段只读公开 DOM：结果确有变化时可按变化证据保存；结果相同且没有其他真实
过渡证据时仍拒绝保存。点击正常返回时仍保留私有 UI action capability。

```javascript
var preparedSearch = await xxmmSearch.prepareSearchAction({
  tab, task: {...claim, owner: OWNER}, workDirectory: claim.work,
  searchOptions: xxmmSearchOptions, actionSelector: ACTION_SELECTOR
});
var executedSearch = await xxmmSearch.executePreparedSearchAction({
  tab, actionId: preparedSearch.actionId
});
var savedSearch = await xxmmSearch.finalizePreparedSearchAction({
  tab, actionId: preparedSearch.actionId
});
console.log(savedSearch);
```

任一阶段宿主结果未知时停止并检查 claim/work，不重放该阶段。execute 明确返回
`finalize_search_action` 或 `inspect_search_action` 后才运行 finalize；后者只接受公开 DOM
确有变化的证据，不能把点击超时冒充成功。保存成功后仍由 `commit-search` 统一提交。

`finalizePreparedSearchAction` 在 `saveSearchEvidence` 成功后已消费原 action（`search-evidence.mjs` 第 761–764 行）；正常成功只把 `evidencePath` 交给 `commit-search`，不再调用 `releasePreparedSearchAction`。后者仅用于失败且已有正式隔离终态的原 action，必须使用匹配的隔离回执，不能把 commit receipt 当作隔离 receipt。

函数返回 `evidencePath`，直接交 `commit-search`。没有观察到加载阶段时省略 `loadingSnapshot`，由真实结果变化门禁判断；每轮重置 before/after，不能复用上轮 loading。选择器不能由查询词猜出；所选渠道不能从 claim 补填成“已选中”。重复批次只重赋任务/快照变量，模块初始化一次，避免持久 REPL 的 `const` 重声明。

若前后结果相同，必须有实际观察到的加载→完成证据或回到 CUA 逐步核实，不能因相同就冒充刷新成功。需复用连续零结果/相同命中的自动路径时，给 `readSearchDom` 传入已核实的 `loadingSelector`，在界面加载提示可见时保存 `loadingSnapshot`，完成后保存 `after`，并将 `{before,loadingSnapshot}` 传给 `saveSearchEvidence`；程序核对实际可见→隐藏、时间、词及渠道，不能手造 loading 标志。零结果须有页面空结果提示。分页待处理、截断和输入/渠道不符均拒绝自动提交。搜索辅助模块不能替代网站分页与覆盖判断；原证据格式与经现场核实的提交入口继续保留。

`commit-search` 统一执行 URL 预检、计划/日志同步、幂等入库与共享详情入队，只返回数量及收据路径。不要额外先查同一组 URL、读回整份证据或手工写 SQL。

精确失败才展开 [搜索故障恢复](search-fault-recovery.md)；仅宿主确需其他入口时读 [搜索接口兼容](search-compatibility.md)。普通采集不加载两篇。
