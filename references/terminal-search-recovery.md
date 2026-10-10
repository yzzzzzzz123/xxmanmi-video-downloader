# 无正式搜索证据的终态隔离

原 prepared actionId 仍在、真实父 session 与子 thread 不同且 finalize 因错误渠道 selector 被拒绝时，先读 [父宿主原动作恢复](parent-host-search-recovery.md)，通过正式隔离回执由原模块释放；不能把保留的私有动作说成丢失。

仅处理真实搜索故障。先尝试原模块正式 finalize/commit；可用私有动作证明、正式 evidence、commit 或 task search journal 存在时使用原恢复流程。全部已发工具确已终态，但原私有证明不可导出、正式搜索证据不存在时，主线程可批准原 owner 使用 `workflowctl.py quarantine-search-terminal --thread OWNER --query QUERY --channel CHANNEL --evidence FILE --logs-stopped --authorized`。暂停优先，不重放未知动作。

证据位于原 owner work 中，最多1MiB；`version=1`、`reason=quarantined_terminal_search_without_evidence`、owner/task/query/channel/claim SHA 精确匹配有效 claim 与 taskbook，`logs_stopped=true`、`main_authorized=true`、`browser_action_inflight=false`、`original_action_proof_available=false`。

`terminal_actions` 按真实顺序保留1–16条工具交互，每项有连续整数 `sequence`、原操作说明 `operation`、经原工具终态核实的 `tool_terminal=true`；`terminal_action_count` 为实际总数。Enter 是否产生搜索无法证明时如实写入操作说明，不把多次交互压成一次。任何工具仍在途则此入口拒绝。

`browser_host` 保存真实 `session_id`、`thread_id=OWNER`、`terminated=false`、`tools_terminal=true`、`checked_at`、`method`。session可以为实际父会话，不改写成owner。`browser_tab` 保存真实原 `tab_id`、`exists`、`checked_at`、`method`。仍存在时附原标签只读 `dom_summary`：实际 `input_value_after_action=QUERY`、`selected_channels=[CHANNEL]`、整数 `result_count` 0–200、`result_container_visible=true`、`pagination_pending=false`。标签缺失时附批准浏览器接口实际返回的 `inventory_tab_ids`，不得含原ID。DOM只为终态诊断，不生成成功证据。

`local_state` 的 `browser_evidence_exists`、`commit_receipt_exists`、`search_journal_contains_task` 均为真实false；`journals_before` 与 `journals_after` 为两次实际 `_journal_snapshot(work)` 元数据，完全相等；`summary` 简述具体故障与原证明缺失。CLI再次复核无详情active batch、无capture锁、无evidence/commit/task journal、日志及证据停写，然后以原claim SHA和证据SHA追加正式abandonment。

回执必须为真实 `manual_review_required=true`、`replay_authorized=false`、`search_success=false`。保留原claim、taskbook、所有日志和现场附件。不得把可见的1条或7条结果当作成功搜索，不从这些附件直接入队。正式隔离后仅接续其他合法共享候选；原query/channel保持不可重放。原标签已缺失时，在无未知动作且隔离完成后按批准Chrome流程建立同槽唯一新自有标签，不占用其他槽位或用户标签。

该入口不退役owner、不删除标签、不改变目标、批次、生产账本或小时报告水位；不能代替有私有actionId时的正式句柄释放，也不用于宣称线程死亡。真实父宿主下未提交 fill/prepare 超时、原 tab 仍在但唯一有界只读恢复也超时的情况，按 [搜索故障恢复](search-fault-recovery.md#父宿主未提交输入超时) 的经源码核实窄分支处理，不能强关标签来套用 missing-tab 门禁。

仅一次输入 fill 超时并已真实返回 kernel reset、没有任何搜索点击或 actionId 时，也须区分“工具调用终态”和“输入结果未知”。原 tab 尚可读但输入与 claim 不符时不能套用本入口的有 tab 门禁，保留 claim 且不重填。若后续批准浏览器只读核验确认原 tab 真实消失、实际 inventory 不含该 ID，所有原调用和核验工具均已终态、无私有动作证明及正式搜索记录，则主线程可批准原 owner 使用本入口的 missing-tab 路径。terminal_actions 如实记录每次真实调用与未知 fill 结果，不补造 click、actionId 或成功输入；旧输入只作历史附件。隔离完成后才能建立同槽唯一新自有 tab 并采集其他候选，原 query/channel 保持不可重放。
