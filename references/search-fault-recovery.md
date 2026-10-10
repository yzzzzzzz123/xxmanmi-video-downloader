# 搜索故障恢复：只读精确故障

本文件仅在正式CLI或浏览器出现对应真实故障时展开；不重放未知动作，不放宽成功门禁。恢复优先使用现有入口；必要的最小故障修复遵循 [主线程修复边界](main-supervision.md)，修复后从原断点立即续跑，不以项目回归测试为前置条件。不得写临时程序或伪造回执；无匹配恢复能力时保留断点，由原owner报告真实阻塞。

## 原浏览器对象丢失或标签失效

仅针对真实`No tab with id`/`Tab not found`，或原浏览器变量`typeof`为`undefined`的故障。已初始化agent仍可用时，缺少某个局部变量不代表Chrome已关闭。复用原agent和实际变量名，不重新初始化SDK、换backend或重置Node。

先核对原owner请求身份与原生回执中的Chrome family/browserId。已有本次成功的`agent.browsers.list()`回执就复用；否则只读取一次。清单含同一Chrome真实ID时，以`await agent.browsers.get(实际ID)`保存在新的浏览器变量中，再以`await 新浏览器变量.tabs.list()`有界读取一次实际标签库存。浏览器可用清单不等于标签库存，不能在只读了前者后宣称本人标签不存在或恢复完成。实际ID不匹配、身份不明或读取失败则保留真实错误交业务主，不猜变量或ID。

这一步仅恢复读取库存的浏览器对象，不覆盖原tab对象、module、action或nonce；即使旧滚动结果未知，也只能进行上述只读核验，不重放滚动，不导航、不新建标签，不清除故障或重置首次预算。只有原动作已真实终态且无未知动作时，才沿既有门禁复用确实存在的本人标签，或恢复本人同号工作标签。用户暂停或已正式停工时不唤醒采集者执行本步骤；新的明确续采周期才恢复执行。

## claim与动作能力异常

若正式 `claim-search` 与其任务书的单一查询词存在逐码点差异，不能任选一个输入，也不能修改 claim 或任务书来消除冲突。确认尚未填词/点击、无 evidence/commit/search journal/写锁，且停写后的两份 worker-journal SHA-256 快照完全一致时，保存 JSON 证据（包括 claim/taskbook 查询原文及码点、claim 与 taskbook 哈希、0 次 fill/0 次 click）并执行：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py claim-task-scope-mismatch --thread OWNER --query "claim 的精确原词" --channel "实际渠道" --evidence EVIDENCE.json --logs-stopped
```

该入口再次从有效 claim 与原任务书核验 owner、task、渠道、哈希和逐码点差异，只追加 `quarantined_claim_task_scope_mismatch` 人工复核终态；claim 与任务书均保留，设置 `replay_authorized=false`，不生成搜索证据、不写数据库、不 commit、不入队。输入或点击已尝试、claim/taskbook 已一致、日志变化或任一回执/锁存在时拒绝并保留断点。

若原浏览器宿主已明确终止、原标签已不存在，且分段动作既没有可恢复的 `actionId`/模块实例，也没有 execute/inspect/finalize 回执，禁止重放未知动作。只有同时核对 owner 无在途详情批次、任务书与 claim 匹配、工作日志停写、`${task_id}-evidence.json` 和 `search-commit-${task_id}.json` 不存在、`searches.ndjson` 没有本任务记录后，才准备包含宿主终态、标签清单检查、日志 SHA-256 快照及 `replay_authorized=false` 的审计 JSON，执行：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py abandon-search --thread OWNER --query "原查询词" --channel "原渠道" --evidence ABANDONMENT.json --logs-stopped
```

程序保留原 claim，向 `search-claim-abandonments.json` 追加 `abandoned_unknown_action` 终态，将该组合从后续自动领取中隔离；它不提交空结果、不写搜索记录，也不允许 handoff 或重新搜索该组合。成功后原 owner 可另行领取其他 query/channel。任一宿主、标签、日志、证据、commit 或动作回执条件不满足时拒绝废弃，回到原实例 inspect/finalize 或 commit 断点。

若实际 browser `session_id` 与 owner thread UUID 不同，host 仍存活、原任务 tab 可读，但一次搜索 helper 调用超时重置执行上下文，导致点击结果未知且原模块/action capability 不可恢复，不得把两种身份合并，也不得用普通 `abandon-click`（它要求同 owner host session）。只有 claim/taskbook 精确匹配、恰好一次 unknown click、真实 foreign session/thread 与同 host tab 记录、DOM 摘要对应原 query/channel、无 evidence/commit/search journal/写锁、owner 无 active batch，且 worker NDJSON 双 SHA-256 快照相同，才可使用专用终态：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py abandon-foreign-host-click-unknown --thread OWNER --query "原查询词" --channel "原渠道" --evidence ABANDONMENT.json --logs-stopped
```

该命令只追加 `abandoned_foreign_host_click_unknown` 人工复核 ledger 记录；不提交、不入队、不解锁后重放。身份、动作、原 tab、任务书、缺少回执或日志任一门禁不符即保留 claim。

若同 owner 的 live browser 会话与 `owner` 精确匹配，原任务 tab 仍可读且公开 DOM 查询词/渠道精确匹配，但唯一一次 `prepareSearchAction + executePreparedSearchAction` 调用超时并重置执行上下文，导致 action capability/ID 与执行回执不可恢复，不得重放 click。仅在精确 claim/taskbook 匹配、tab URL 属于 `xxmanmi.com`、DOM 保留 query/channel、helper 调用恰为一次且错误精确为 `js execution timed out; kernel reset, rerun your request`、无 action/evidence/commit/search journal/写锁、owner 无 active batch，且停写后的双份 worker-journal SHA-256 快照完全一致时，才可准备真实审计 JSON 并执行：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py abandon-live-owner-click-unknown --thread OWNER --query "原查询词" --channel "原渠道" --evidence ABANDONMENT.json --logs-stopped
```

该入口要求 browser `session_id == owner`、原 tab 在该 session 内可读、精确 query/channel 同时出现在动作调用前核验和超时后的 DOM 摘要中；`action_attempts=1`、`search_click_completed=unknown`、无可恢复 action ID/execute/inspect/finalize 回执。它仅追加 `abandoned_live_owner_click_unknown` 人工复核终态，固定 `replay_authorized=false`，不生成搜索证据、不写数据库、不 commit、不入队。任一身份、DOM、调用次数、超时错误、receipt、锁或双日志门禁不符时拒绝并保留 claim。

## 缺少空态、身份失配或标签消失

若 click 已明确返回且原模块仍可用，但正式 finalize 因零结果缺少可见空态拒绝证据，同时浏览器 host 的真实 `session_id` 与 owner thread UUID 不同，不得将 threadId 冒充 sessionId，也不得复用一般 `quarantine-search` 的同 owner-session 断言。停止 UI 操作并保留原模块、actionId、原 tab 与 DOM 事实；人工整理包含 claim SHA、外部 host session ID 与 owner thread ID、actionId、DOM 摘要、日志快照且 `replay_authorized=false` 的 JSON 后，才可调用专用终态入口：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py quarantine-search-host-mismatch --thread OWNER --query "精确查询词" --channel "实际渠道" --evidence EVIDENCE.json --logs-stopped
```

该入口要求恰好一次已返回点击、host session 确实不同于 owner 且记录 thread 匹配、当前 DOM 精确对应 query/channel 且零链接/无空态、没有 evidence/commit/task journal 回执、日志停写及 claim/taskbook 哈希完全匹配。它只把组合追加到不可重放隔离 ledger 并标注 `manual_review_required`，不保存可提交搜索 evidence、不写 DB、不 commit、不入队；任一条件不符即拒绝且保留原 claim。

主线程可对当前 owner 明确批准上述同类故障的窄恢复规则，避免每个正常无结果候选都等待逐查询调度。授权仅覆盖 APIv4 原模块和原 action 仍在、唯一 execute 已真实返回、finalize 精确拒绝缺少可见空态、原 tab 当前可读且 DOM 为原词/原渠道/0链接/无加载或分页、无在途批次或未知工具、正式任务书/claim 原值一致、无本 task evidence/commit/journal且双停写快照一致的情况。原 owner 逐项真实核实后，执行同一正式 `quarantine-search-host-mismatch`；成功后以保留的原模块 `releasePreparedSearchAction` 消费实际回执，再接下一个组合。不重复点击、不叠加未释放动作、不把隔离当空搜索成功。参数错位、标签消失、私有能力丢失、工具未知或 CLI 写入失败均超出此授权，保存断点交主线程修复。

隔离 CLI 的 Windows 原子替换若失败，先只读核实 claim 与 ledger 是否已形成终态，不能重放浏览器动作。一次正式重试仍失败时交主线程：主线程程序读取原任务书、原证据及真实失败回执中的 SHA，复核暂停、owner/日志及无终态门禁后，经同一正式 CLI 处理；不手改账本、ACL 或源证据。只有真实终态写入后才由原 owner 释放原私有能力。

旧高层 `runSearchActionEvidence` 在点击阶段内部捕获timeout，随后可能精确报 `Zero results require a visible empty-result indicator`，但不返回actionId或点击回执；不能由末尾错误反推click成功。当前入口为这类一次、已返回helper错误且原模块仍在的情况接受严格legacy隔离：action_id字段缺省，`helper_entry=runSearchActionEvidence`、`helper_call_attempts=1`、`helper_returned=true`、`click_returned=unknown`、`click_completion_receipt_available=false`、`action_id_emitted=false`、`active_action_capability_available=false`；result_error须精确对应上述错误，原tab当前可读且真实session/thread绑定、DOM无loading、精确词/渠道及0链接无空提示。既有哈希/停写/无证据与commit门禁不变，ledger保存缺少动作身份的真实事实，只隔离且禁止重放。后续搜索优先三段式保留动作句柄，不继续使用易丢收据的旧高层组合调用。

如果 host 仍存活，但原 task tab 随后确实消失，禁止把旧 DOM 说成当前 tab 可读或重建页面。只可在之前真实读取的 DOM 快照、同一已确认点击/actionId 仍可核实、tab inventory 不含原 id 且对原 tab 的只读读取返回精确 `No tab with id: <id>` 错误时，使用 `quarantine-search-host-tab-gone`。证据须分别记录真实 foreign `session_id` 和 owner `thread_id`、原 tab id/精确错误、两次完全相同的 worker journal SHA 快照、claim SHA 及原搜索 DOM 摘要；CLI 仍要求无 evidence/commit/task journal、日志停写，只允许产生不可复放的 `manual_review_required` ledger 终态。

```powershell
python -X utf8 -B scripts/collection/workflowctl.py quarantine-search-host-tab-gone --thread OWNER --query "精确查询词" --channel "实际渠道" --evidence EVIDENCE.json --logs-stopped
```

## 原模块保留但旧门禁无法收尾

主线程已获本项目故障修复长期授权。仅在原APIv4模块、原tab对象和原actionId仍保留，唯一execute真实返回、finalize已返回真实失败、所有工具终态时，可用兼容入口。不得重载模块、删除Map、重复点击或伪称私有证明丢失。

原owner保存其工作目录内的真实JSON：`version=1`、`reason=quarantined_unverifiable_result`、精确owner/task/query/channel/claim_sha256、`logs_stopped=true`、`main_authorized=true`、`browser_action_inflight=false`、实际`browser_host`/`browser_tab`、`search_action`、`local_state`及`journals`。动作记录保留原UUID、`state=completed_unverifiable`、整数`action_attempts=1`、`execute_returned=true`、`execute_next_action=finalize_search_action`、真实finalize次数/错误、`original_module_instance_available=true`、`replay_authorized=false`。CLI只读核对`.runtime/main-agent/state.json`的直接用户授权、原slot/目录和真实`host_parent_thread`，不混淆业务主线程与浏览器host。

原tab可读时`dom_summary`直接保留正式`readSearchDom`返回的完整结构对象（含真实`observed_at`），不手抄漏字段或把事后clock当旧DOM观察时间；输入错位或容器不可见不能改成成功。tab消失时必须保留真实精确缺标签错误及实际inventory不含原ID，`dom_summary`置null；旧格式为`No tab with id: ID.`（可含`Error: `前缀），仅本节已授权retained-action且实际inventory为空时也接受当前Chrome的`Tab not found: ID. Existing tabs: none`，不得改写错误或以空库存代替错误。已观察旧DOM仅保存在`historical_readonly_observation`，没有旧DOM不编造。已有candidate必须保留原字节；只有精确原owner/task/channel下query及input同时错位时允许附`--allow-saved-candidate`，并记录`local_state.saved_candidate_sha256`；有效candidate仍走commit。无commit/本task搜索journal、无在途详情/写锁、停写快照一致后才执行：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py quarantine-search --thread OWNER --query "原查询词" --channel "原渠道" --evidence ORIGINAL_WORK/TERMINAL.json --logs-stopped --authorized-retained-action
```

成功仅追加正式人工复核隔离ledger，不搜索成功、不入队、不入库、不重放。原owner再用**已加载原模块**`releasePreparedSearchAction({tab: 原tab对象, actionId: 原UUID, terminalReceipt: 实际CLI回执, terminalEvidencePath: 原JSON路径})`消费回执，真实`released_terminal`后才能领其他任务。停写、授权、认领、candidate、commit或日志变化则保留断点，不重复动作。

当前原标签已转到真实`/videoN/数字.html`详情页时，仅已授权retained-action分支可收尾：原唯一execute和失败finalize已终态，失败精确为`Zero results require a visible empty-result indicator`；当前完整DOM必须如实为输入null、无渠道/搜索容器/链接/空态/加载/分页，URL等于原tab真实URL。同时保留时间早于当前快照、同站点、精确原词/渠道且零链接无空态的真实历史DOM，明确它不是当前DOM。正常claim/身份/原模块/日志/无在途门禁仍全部执行，audit标注`current_detail_page_after_failed_search=true`；只人工复核隔离、不搜索成功、不重放。缺任一事实继续拒绝，不能改字段凑门禁。

### 已隔离但旧模块的任务词本身错位

仅“历史DOM有空格”不能证明prepare参数错位；主线程须核实原prepare实际调用（包括补齐owner的真实成功调用）、原唯一成功execute、原actionId及旧模块释放的精确身份不匹配错误。已有同task正式不可重放隔离终态，且原工具均终态、原cap保留而没有正在执行操作时，主线程可将这些真实证据登记到现有主断点的`retained_misbound_actions`。

原owner执行`workflowctl.py resume-misbound-search --thread OWNER --query "正式原词" --channel "原渠道" --evidence 原隔离JSON --logs-stopped`。CLI核验直接用户授权、原slot、claim/taskbook、已有隔离ledger及原证据哈希、真实prepared_query差异、成功execute与失败finalize、无新证据/commit/在途详情/写锁以及稳定日志。只生成正式恢复回执，不改原ledger、任务或私有Map，不宣称`released_terminal`；回执必须明确`released=false`、`retained_capability_status=quarantined_dormant_not_released`、`search_success=false`、`replay_authorized=false`。

这是允许继续**其他合法任务**的窄分支，不是释放旧能力：原模块、原tab对象、原nonce及失败附件保留休眠，不再调用旧nonce，不重载/重置/清Map，不创建新agent。下一任务全部参数程序读取真实taskbook，同一query变量用于fill及task.query；禁止手抄或修饰空格。未知动作、无正式隔离、有效candidate、未证明原参数差异或工具在途均拒绝本入口。未来真正核实宿主撤销/原模块释放时才标记旧能力撤销。

## Chrome 已关闭但原动作证明仍保留

只适用于主线程已逐条核实并登记在主断点 `retained_retired_browser_actions` 的原 nonce：唯一 execute 已终态失败或返回 `inspect_search_action/click_timeout`，原模块仍在，实际 Chrome 未运行诊断随后由用户重新打开，且新 Chrome 连接的真实库存不含旧 tab。空库存本身、进程存在或口头称“丢失”不能替代这些证明。此分支不宣称点击完成，不要求或允许重放旧动作。

原 owner 程序读取该精确登记项，在原 work 保存正式 JSON；沿用 `reason=quarantined_unverifiable_result`，增加 `recovery_mode=retired_browser_retained_action`。`search_action` 直接复用登记的 `action_contract`，`terminal_actions` 复用已核实原工具回执；host 保留真实父 session/子 thread 和实际 `original_browser_unavailable_error`，tab 为真实旧 id、`exists=false/readable=false` 与实际库存，`dom_summary=null`。正常 claim/task 哈希、无 candidate/commit/task journal/在途详情/写锁和稳定双日志门禁不变：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py quarantine-search --thread OWNER --query "原词" --channel "原渠道" --evidence ORIGINAL_WORK/TERMINAL.json --logs-stopped --authorized-retained-action
```

只有真实 CLI 成功且回执明确 `released=false`、`retained_capability_status=quarantined_dormant_not_released`、`search_success=false/replay_authorized=false` 时，才可在同原线程、同已加载模块和新本人工作 tab 接**其他合法任务**。原 nonce、原 tab 对象和模块保留休眠，不再调用旧 execute/finalize/release，不重载模块、不清 Map，也不把该人工复核隔离计为搜索成功或合格节目。门禁失败保留断点交主线程；不得填假 finalize 返回、可见空态或能力丢失。尚有未知工具、实际 Chrome 关闭未核实或旧 tab 仍存在均拒绝。

## 父宿主未提交输入超时

原 fill/prepare 调用与唯一有界只读恢复均已真实返回 kernel reset，原 tab 仍在但 DOM 不可读时，不伪称标签缺失，不再读 DOM/重填，也不把未知 prepare 写成成功。主线程核对原调用源码确无 execute/click/Enter，并核实实际父 session、子 owner、原 tab inventory 与重置后模块绑定不可访问后，在原主断点 `unsubmitted_input_recoveries` 登记精确 owner/task/query/channel/tab、三条实际终态工具 ID、`proof_verified/source_verified_no_submit/all_tools_terminal/runtime_reset_verified=true` 和实际 reset 错误。

原 owner 保存有界真实 JSON，沿用 `abandoned_uninspectable_input_action` 合同；`main_authorized=true`、`browser_action_inflight=false`，host 保留真实父 session/子 thread、`session_reset=true/tools_terminal=true`；tab 为真实 `exists=true/readable=false/owned_by_original_session=true`、实际原 tab ID 与包含该 ID 的 `inventory_tab_ids`，不附当前 DOM。动作仅 `input_fill_unknown_unsubmitted`、整数 fill/attempt=1、submit=0、`input_result/prepare_result=unknown`、原模块不可访问、无 actionId/execute/inspect/finalize 回执、不重放。`terminal_actions` 按真实顺序保留 fill、一次只读超时、一次 inventory 核验，含原 tool_id/sequence/operation/status/error/tool_terminal，与主线程已核实记录严格匹配。

原 owner 用 `workflowctl.py abandon-input --thread OWNER --query "原词" --channel "原渠道" --evidence ORIGINAL_WORK/FILE.json --logs-stopped --authorized-parent-input`。CLI 复核直接用户授权、原绑定、claim/taskbook、无详情批次/锁/有效 evidence/commit/task journal、稳定日志及未变化证据，只追加不可重放人工复核终态：`manual_review_required=true/search_success=false`。正式成功后才能按已发生的真实 runtime reset 恢复同槽 Chrome/模块、处理原自有标签并 markHandoff，接续其他合法任务；不是为解锁主动 reset，不换 owner、不宣称旧搜索成功。任何提交、已知 action、原模块仍有私有证明、工具未终态或源码未经核实都拒绝此窄入口。

### 未提交填词超时，但本人页实际是详情页

仅一次 `fill` 已终态返回 kernel reset，源码确无 prepare/execute/click/Enter，而唯一只读核验显示原自有 tab 可读但在真实 `xxmanmi.com/videoN/*.html` 详情页（input=null、无渠道和搜索结果容器）时，不套用标签缺失或 DOM 超时门禁，也不重填。主线程在 `unsubmitted_input_recoveries` 登记 `recovery_mode=readonly_non_search_page_after_unsubmitted_kernel_reset`、精确 owner/task/query/channel/tab、原 claim/taskbook SHA、实际原 tab 创建及父 session/子 thread、按实际顺序的 fill失败/库存成功/只读DOM成功三条工具ID，以及完整原 `readonly_dom`。

原 owner 按 `abandon-input --authorized-parent-input --logs-stopped` 既有合同保存证据；该模式的 tab 为 exists/readable=true、实际库存含原ID、dom_summary逐字段复用已登记DOM，三条 terminal_actions 分别为 `unsubmitted_input_fill`、`readonly_tab_inventory`、`readonly_non_search_page`。保留 input_result=unknown，prepare_result=not_attempted、submit_attempts=0；仍须原任务/claim、停写双日志、无详情批次/锁/有效搜索证据/commit/journal。正式CLI只追加不可重放人工复核终态，不入队、不搜索成功、不入库，门禁失败保留断点。

成功后才在同原线程、本人原tab接其他合法任务。下一任务填词前先检查本人页面的公开DOM；仍是详情页且无在途未知动作时，导航到已知公开搜索主页，再以 freshDOM确认输入框后填词，不在缺少搜索框的详情页直接fill，不复放已隔离组合。

## 旧结果或输入错位

若唯一一次 click 明确返回、原 tab 仍可读，但当前精确输入/渠道下仍显示与目标无关且与 click 前完全相同的旧结果，使用 `quarantine-search-foreign-host-stale-results`，不能套用要求零链接的 host-mismatch 路径。证据须含精确 claim/task/actionId、foreign host 与 owner 分离身份、同一 tab 前后 DOM 摘要（旧结果非目标且链接不变）、两次相同 journal SHA 快照，以及 evidence/commit/task journal/锁均不存在。该命令只追加不可重放的 `manual_review_required` ledger 终态，不生成 evidence、commit、journal 或入队；任何不一致均拒绝并保留 claim。

若唯一一次已返回 click 后，原 task tab 仍可读，但 DOM 可证实实际输入与 effective claim 原词严格不等（例如误填了额外空格），而 channel 仍精确匹配，零链接又没有可见空态，则使用 `quarantine-search-foreign-host-input-mismatch`。证据必须精确匹配 claim/task/hash，保留真实 foreign `session_id` 与 owner `thread_id`、原 tab 身份、唯一 actionId、DOM 实际输入、稳定的双 worker-log SHA 快照，并证明正式 evidence/commit/task journal/锁均不存在。此路径只写不可复放的 `manual_review_required` 隔离终态；错误输入绝不能伪装成 claim query 的搜索证据，也不能 commit、入库或入队。若任一条件不成立，保留 claim 并停止操作。

```powershell
python -X utf8 -B scripts/collection/workflowctl.py quarantine-search-foreign-host-input-mismatch --thread OWNER --query "effective claim 原词" --channel "实际渠道" --evidence EVIDENCE.json --logs-stopped
```
