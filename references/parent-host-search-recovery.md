# 原 prepared 动作的父宿主隔离

仅用于原 API v4 `prepare→execute` 已单次返回 `finalize_search_action`，finalize 精确报 `Public input value and selected channel must exactly match the claimed search` 的真实故障。原模块复制了错误选择器，修改外部变量不会改变原 actionId。禁止重放 execute、重载原模块、手填 selected_channels 或将只读结果直接入队。

真实 browser session 是监督配置的 ROOT UUID、thread 是原 owner 时，主线程可批准原 owner 使用 `quarantine-search ... --logs-stopped --authorized-parent-host`。它复用 `quarantined_unverifiable_result` 终态并保留原 actionId；不生成搜索成功、不入队。先在原 tab 用当前实际选择器只读取得完整 query/channel DOM，保存原 work 内的证据。

证据沿用一般 quarantine 的 version/reason/owner/task/query/channel/claim_sha256、`journals`、local_state、summary 和真实 host/tab 时间及方法，并补充：

- `main_authorized=true`、`browser_action_inflight=false`。
- `browser_host.session_id` 为配置的真实 ROOT、`thread_id` 为 owner、`terminated=false`、`tools_terminal=true`；tab 保留真实 tab_id/url/session_id/owner_thread_id，exists/readable 都为 true。
- `search_action` 保留原 action_id，state=completed_unverifiable、action_attempts=1、execute_returned=true、execute_next_action=finalize_search_action、finalize_returned=true、finalize_attempts=1、original_module_instance_available=true、private_action_record_exportable=false、精确 result_error 与 replay_authorized=false。
- `dom_summary` 是原 tab 的实际只读快照，包含 page_url/observed_at、精确 input_value_after_action、仅一个实际 selected_channels、完整 result_count/visible_detail_links、empty_result_visible、result_container_visible=true、pagination_pending=false，且没有 loading。
- local_state 的 browser_evidence_exists/commit_receipt_exists/search_journal_contains_task 都为 false。`journals` 使用当前真实停写日志快照。

CLI 核对配置的真实父 session、原槽位/独占 work，双重停写检查期间父绑定和证据不可变化，暂停优先。真实终态回执返回后，仍用**原模块实例** `releasePreparedSearchAction({tab, actionId, terminalReceipt, terminalEvidencePath})`，由它校验原私有动作、正式 ledger 及精确证据 SHA 后释放。释放通过后才继续其他 claim；后续 prepare 前先核实选中渠道 selector 只命中一个当前按钮。

若原 tab 随后真实消失，保留原 tab 对象/模块/actionId，先核实所有工具终态并取实际 tab inventory。tab 的 exists/readable 设为 false，保存真实 `missing_error`（精确 `No tab with id: ID.` 或工具实际返回的 `Error: No tab with id: ID.`）和不含原 ID 的 inventory_tab_ids；不能提供 current dom_summary。以前的只读观察只能放 `historical_readonly_observation`，如实保留原错误选择器返回的空 selected_channels 和另行观察到的 current 按钮事实，不填成当前 DOM。此路径仍只隔离精确已返回 execute/finalize 的原 selector 故障，并由原模块核验原私有动作后释放。

工具仍在途、click 未知、缺真实 tab 消失证据或实际 finalize 次数不匹配时保留断点，不能套用本入口。一般 quarantine 的原 owner-session 路径保持既有条件；新 flag 只对上述配置匹配的精确 selector 故障开放。
