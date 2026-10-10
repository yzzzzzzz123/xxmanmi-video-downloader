# 多 agent 共享采集

采集日常入口见 [低 Token 工作流](low-token-workflow.md)：`claim-detail` 恢复/领批并生成任务书，`complete-detail --logs-stopped` 核验收批。主线程日常只读 [主线程短流程](main-supervision.md)。本文仅用于首次编组、交接、恢复和分步异常；正常每批不加载全部历史规则。

适用于用户明确要求采集/续跑；只读查询、Skill修改、代码任务和无关终端不自动加入。默认1个当前模型主线程加4个`gpt-6-luna`/`medium`采集agent。主线程只负责恢复和最小监督，不准备关键词、不查榜供词、不代采或逐批收批；健康线程自主完成公开列表发现、详情采集和本地验收。正式提交复用已授权[事件增量同步](all-catalogue-sync.md)，不等待300节目；正常、修复和停工均不汇报，只显示`working`。

四个逻辑槽位固定01–04；Chrome工作分组名称精确为`01`、`02`、`03`、`04`，禁止“01采集线程”等文字，宿主`nameSession`同号命名。内部task_name可有重建后缀，后缀不进入组名。维护真实UUID、独占目录、binding/标签映射，不用展示名代替身份。默认复用一个本人工作标签、单lane，临时标签用完安全关闭。**定时轮换完全删除，只有确认死亡且原写入已停止才调用清理Skill清干净旧实例并补回原槽位；困难优先原owner修复，健康线程保持运行。** 不新增第五采集者、不重置失败预算。`requestMeta.threadId`是owner，父`sessionId`可不同，不单凭两ID不同判失配。

## 入口与唯一状态

项目和上级工作区的 `AGENTS.md` 将相关任务路由到本 Skill。新采集任务先核对本会话已有子 agent、真实 owner、在途批次和宿主容量；默认维持四个采集线程，只补缺位。工具容量不足如实报告实际数量与缺位，不绕过并发限制，也不把主线程或历史登记算作采集者。共享协调程序没有硬编码人数上限不改变本项目的四线程配置。用户明确指定既有终端参与时才按下文核对并纳入这四个名额，不额外扩编。仅打开终端不表示它已开工。

从项目根执行 `python -B scripts/collection/coordctl.py <命令>`。选择现有可用 Python，不安装额外依赖。下文 `STATE` 指项目绝对路径下 `Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json`；所有线程使用同一个文件。`THREAD` 必须来自当前会话真实线程 ID，并与 01–04 名称、工作目录建立明确映射；拿不到真实 ID 时先核实宿主身份，不能随机生成或借用旧 UUID 代替。

协调程序只管理本地详情队列与状态，不控制浏览器、不访问站点、不写PostgreSQL。注册采集peer直接从本人实际可见列表、来源和分页补充真实详情URL，经共享队列独占认领后采集、本地入库和核验；主线程只恢复和最小监督，不注册为采集者，不创建关键词、榜单发现、分类或系列任务。每个采集agent独占工作目录和任务标签，只传一个lane；允许按需临时标签，但不增加并行lane。

首次由任一已获授权的参与者初始化一次：

```text
init --state STATE --thread THREAD --work ABSOLUTE_WORK
```

旧版状态若仍含 `max_workers`，通过 `unlimit --state STATE` 在原锁内移除，保留线程、队列和批次；已迁移状态无需重复操作。

已有状态不得重复 init、另建竞争状态或因锁忙接管。状态看似缺失时先核对路径与 `coordination-request.md` 的未迁移分配，并同其他 peer 对账，不能直接清空重建；状态存在则读取 `status`，保留既有身份和断点。所有 worker 日志放在数据库 `.runtime` 下各自目录，禁止混写、改名或迁移已有目录；大小写、斜线、链接别名都不能用于创建第二个 worker。

## 用户指定既有终端参与时的身份核对

若用户明确要求接入已经开启的其他 Codex 终端，先恢复自己的 `current`，再检查候选身份和真实空闲状态。共享队列中的 `idle` 只表示没有在途采集批次，不能证明整个 Codex 会话空闲。默认编组创建本进程的子 agent，不遍历无关终端。

优先使用当前已连接的宿主提供的会话列表/状态，限定本项目已登记的真实终端及用户明确为此任务开启的候选，不扫描无关聊天。已有 app-server 连接若提供 `thread/loaded/list`、`thread/read`，用它们核对已加载及运行状态；`idle` 仍需结合真实终端身份、待处理输入和目标自身确认。`notLoaded` 不表示用户现在开着的终端，`active`、`systemError` 或无可靠状态不视为空闲。不要为查状态另起一个 app-server 后把它的空列表当作现有会话状态。[官方线程状态接口](https://developers.openai.com/codex/app-server)。

只联系身份明确、当前无其他任务/待处理输入、未被用户暂停且无尚未解除权限阻塞的已开启会话。若宿主无法给出可靠状态，已确认候选可先收到一次不分配采集页的身份/空闲询问；对方确认前不登记为运行中、不分配页面。不可用或归属不明则由当前已登记 agent 继续自己的任务书，报告具体限制，不阻塞健康线程或冒充指定终端。

### 用户指的是 Trae 中已有的 Codex 终端时

先把对象核对到实际终端，不把当前多代理树的 `/root`、子 agent 名、目录编号或展示别名当作其他终端的唯一身份。必要时只读检查候选 Codex 进程及其父链，确认确由 Trae 终端启动，排除 app-server、桥接服务和当前会话；结合启动时间及该候选 `session_meta` 的真实会话 ID、cwd、originator 交叉核对。不要回显完整进程命令行/凭据，不读无关会话聊天，不能仅凭 PID 大小、低 CPU 或同名 `/root` 判为空闲。PID 会复用，必须同时核对启动时间。

当前 CLI 若有 `queue`，先读本机 `codex queue --help`，再对已核实候选的**完整 UUID**使用 `codex queue --thread THREAD --message "身份核对与本项目协作请求"`；它向已有会话发消息，不另起一个采集 Codex。不传模型、安全策略或代理覆盖参数。身份与空闲已确认后，在同一受支持入口发送加入请求：读取本项目 Skill，复核自己仍空闲，自行登记/恢复 `current`、领取有界任务书与独占小批、采集入库核验、只向正式主线程内部上报真实阻塞。无需等待某个固定线程批准。

投递前检查共享交接记录及现有未处理邀请；同一目标只保留一条未完成邀请，多 peer 同时发现同一候选时先约定一个投递者。记录真实目标 ID、发送凭证和待确认状态，不高频催促；仅在明确投递失败或新状态证据出现后有界重试。收到其他任务或用户暂停时目标以最新用户要求为准，不强制入组。队列返回消息 ID 仅代表投递成功，不是接收、开工或采集完成；以目标自身确认和实际登记/领取为准。不能误杀其他进程、注入键盘、绕过权限或将归属不明会话当作可用工人。

已确认目标的后续批次直接通过共享协调状态领取；需要唤醒现有终端时复用定向 queue。新终端仅打开但未运行处理任务时，不假报已加入。用户更改归属时，先让旧 owner 停写、保存/核验已完成页并 ack，再由参与交接的注册 peer 执行 `rebind-work --state STATE --thread THREAD --owner OWNER --work NEW --reason 真实更改原因 --logs-stopped`。该命令只允许空闲 owner 和独立真实路径，保留原批次工作目录与全部历史；之后才能将释放的目录登记给经核实的新 owner。不能直接编辑 state、搬日志或把正在写入的目录送给第二个线程。

### 主线程负责死线程清理与全新补位

实际清理统一调用 [清理 Skill](../skills/project-cleanup/SKILL.md)，先用其 CLI 短摘要判定所需步骤。下述补位只在用户仍授权采集时执行；用户明确全部停用/删除或仅做清理时，清完不重建 agent、不继续采集。

这是用户已明确授权的默认维护流程，发现死亡线程后无需逐次询问是否换新。采集 agent 在安全检查点报告自己的异常，主线程负责判断、清理和补位。`idle`、`notLoaded`、无新日志、一次超时均不能单独证明死亡；核对真实 UUID、最新回合终态及工具句柄，区分正常等待、用户暂停、权限等待与已终止/失效的执行。仍有活跃工具句柄时先检查该句柄结果，防止两次执行写同一批次。

1. **确认停写**：对确认死亡、失效或已被替代的精确会话，使用宿主支持的停止、归档和删除入口结束旧执行，停用其专属浏览器标签和辅助进程；不得按进程名批量关闭共享服务或用户终端。记录原名称、UUID、owner/work、current、任务书和全部顶层 NDJSON 的排序大小与 SHA-256。宿主终止证据可证明旧 owner 停写，不要求已死亡的 agent 再发确认消息。
2. **优先接替未完成任务**：核对已核验页、尚未完成页、详情与搜索认领。能安全接替时通过正式带锁交接入口转交给新 UUID，复用已保存的有效证据，仅继续未完成部分。历史批次保留原 owner，后续处理关联接替回执；不能冒用旧 UUID、直接改 state 或让两人共写。旧批次若必须先收尾/释放才能交接，就先据真实结果收尾/释放，再给新 agent 建立关联的接续任务书。
3. **接替不了就删除旧任务并换任务**：日志损坏、动作结果无法核实、任务书不可恢复或正式交接仍不具备条件时，持久记录取消原因和最小审计证据，取消并从活跃任务/认领中删除该旧任务，给补位 agent 分配新的替代任务。不得把失败标成成功，或让无人执行的 claim 永久占用队列。已核验入库节目不删除；未知浏览器动作按正式 `abandon-search` 隔离，不重放。以后确有恢复依据的详情 URL 只能通过正式 retry/新任务流程重新认领。
4. **清理干净旧身份**：转交或取消详情/搜索任务后，从活跃协调登记删除旧 owner，清理孤立 claim、任务占用及该会话专属执行资源。将必要会话、任务和日志证据归档并校验后，删除已不用的原运行目录和临时文件；已转交给新 owner 的目录不得删除，历史批次需要的证据引用必须仍可追溯。退役记录只作为归档审计与禁止旧 UUID 复用的依据，不作为运行 worker、待恢复线程或可用名额。所有被替代、多余和死亡的本项目 owner 都要处理，不能只改 mode=idle 或留着不断堆积。
5. **同槽位全新补位并验收**：使用 `gpt-6-luna` / `medium` 新UUID和独占目录/标签补回01–04缺位，内部task_name不强求复用旧路径。领取正式交接任务或新的替代任务；核验新owner实际登记、领批并有真实进展，旧owner已无活跃占用，稳态仍为四个采集者。

现有命令能力须如实区分：`release` 将未完成详情送待复核，`retire-idle` 将登记移入 `retired_threads`；不等于已归档/清目录/补位。`replace-idle` 等仍只接受安全空闲交接，不能伪造无在途条件。缺少所需带锁取消/转交/清理入口时先实现验证，不虚构参数或手改状态。仅改Skill不变更运行态。旧 `restore-retired-idle` 仅供用户明确恢复原身份，不用于死亡换新。主动轮换先排空与交接，不把健康线程判死；新会话不fork全历史，最小交接包按生命周期规范生成，历史任务书与证据保留。

```text
status --state STATE
register --state STATE --thread THREAD --work ABSOLUTE_WORK
claim --state STATE --thread THREAD --limit 4
```

登记幂等；目录不能暗中更换。旧`--coordinator`/`--role`调用只为兼容本地历史状态，主线程职责以当前Skill为准。新增采集线程必须用自身真实身份登记并使用独占工作目录，不绕过注册。每个peer按自己的有界任务书领取批次；队列不足时先核对真实pending和cover_pending，再沿已观察的公开列表、来源和真实分页补充详情URL。不能绕过队列采集、猜链接、创建关键词或榜单发现任务，空队列不代表全站完成。在途批次拒绝重复claim；健康线程用current续未完成页，死亡换新先正式交接。ready后完成本地同步、核验、封面门禁和ack，才领下一批。

每个注册 peer 都可补队列：从项目根用 `python -B Datebase/xxmanmi-resource-db/dbctl.py pending --limit <小批数量> --source 小小漫迷 --unvisited-only` 获取真实已发现详情，排除 `coordination-request.md` 中未迁移且仍被保留的范围，再提交项目真实条目数组：

```text
enqueue --state STATE --thread THREAD --items OBSERVED_ITEMS.json
```

`OBSERVED_ITEMS.json`为已观察的`{title,status,url,source}`对象数组，不新增category、query、类型标签或系列字段；旧记录与旧任务上下文保持原样。不得猜详情URL或按同名合并。程序按详情URL去重并在跨进程独占锁内领批，批次和owner固定。锁忙有界等待，陈旧锁或失联worker不会自动删除或超时抢占。

手工交接迁移时先对账：已核验入库页不再 enqueue；在途手工页保持原 owner，不混入自动队列；未登记终端的保留页继续保留，等其登记后任一参与交接的 peer 可通过 `assign --state STATE --thread THREAD --owner OWNER --items OBSERVED_ITEMS.json` 明确分配。该命令要求 owner 已登记且空闲，任何 URL 已在协调状态中则整批拒绝，不抢已有 claim。新程序上线不代表旧保留自动失效，也不许可提前同步仍在写入的日志。

## 采集、落盘与 ready

低Token默认入口是`scripts/collection/workflowctl.py`：先从公开列表观察真实详情URL并用`coordctl enqueue`补共享队列，再由claim-detail和complete-detail生成任务书、采集和收批。历史搜索仅按[原搜索恢复](search-workflow.md)续原claim与证据，不创建关键词待办。数据库URL状态只用`dbctl.py lookup --url ...`，不临时写SQL。

### 用户明确指定接替一个已停写的空闲线程

不把失联、超时或 `idle` 自动当成可接管。用户明确指定接替后，先由旧 owner 确认停写，核对其无在途批次并记录原目录所有 `*.ndjson` 的文件名、字节数和 SHA-256。通过下列入口原子替换登记；不手改状态、不借用旧线程身份、不影响其他线程：

```text
replace-idle --state STATE --thread NEW_THREAD --owner OLD_THREAD --work ORIGINAL_WORK --reason 原因 --evidence HANDOFF.json --logs-stopped
```

`HANDOFF.json` 必须包含匹配的 `previous_thread` / `replacement_thread`、明确为 true 的 `user_authorized` / `owner_stopped`、非空 `method` / `summary`，以及按文件名排序的 `journals: [{name,size,sha256}]`。程序拒绝仍在途、目录变化、日志变化或半行日志，保留所有历史批次、待补项及原工作目录，将旧登记归档到 `retired_threads`；旧线程不得重新登记或继续写旧目录。接管仅转交旧线程的工作目录，原权限拒绝仍需通过受支持浏览器权限机制解除，不能借交接绕过。待补页必须在故障解除后按 `retry` 流程处理。

若接收方已经登记，先完成双方在途批次并明确停写，改用 `takeover-idle`，不能先删除登记来适配 `replace-idle`：

```text
takeover-idle --state STATE --thread RECEIVER --owner OLD_THREAD --work ORIGINAL_WORK --receiver-work RECEIVER_ORIGINAL_WORK --reason 原因 --evidence HANDOFF.json --logs-stopped
```

其证据使用 `previous_thread` / `receiver_thread`、`previous_work` / `receiver_work`、均为 true 的 `user_authorized` / `owner_stopped` / `receiver_stopped`、非空 `method` / `summary`，以及双方目录各自全部顶层 NDJSON 的排序快照 `journals` / `receiver_journals`（每项 `{name,size,sha256}`）。必须取得真实停写回执；空闲状态、投递消息或快照稳定都不代替确认。程序在共享锁内双遍校验，保留双方历史批次及目录，旧身份归档，接收方后续使用原 owner 的目录。搜索认领还须按热门片名发现 Skill 的 `handoff-search` 转交；两个命令不是跨文件事务，双方保持停写直到两处归属都已核实，部分成功时保存阶段证据，不能提前恢复采集。

若接收者的**真实线程 ID 已在先前交接中归档**，不能伪造新 ID 或手改 `retired_threads`。只有归档项明确 `replaced_by=OLD_THREAD`、双方无在途批次、旧 owner 明确停写并提供当前目录完整日志快照与最后已 ack 批次回执时，才可逆向交接：

```text
restore-retired-idle --state STATE --thread RETIRED_RECEIVER --owner OLD_THREAD --work ORIGINAL_WORK --reason 真实移交原因 --evidence STOP_RECEIPT.json --logs-stopped --receiver-confirmed
```

回执需对应 `from_thread`、`intended_receiver_thread`、`work`，确认用户授权、旧 owner 停止日志/数据库/浏览器/领新任务、`current_status=idle` 和 `last_batch_status=acknowledged`，并列出按文件名排序的全部顶层 NDJSON 大小与 SHA-256。程序在唯一状态锁内核查身份、最后批次及双遍日志快照，将旧 owner 归档、恢复接收者真实 UUID，历史归档嵌入新登记记录，不改旧批次或日志。随后按 `handoff-search` 单独转交仍由旧 owner 持有的查询认领；两处都核验前保持停写。

用户在接收线程的直接对话明确要求强制停止当前 owner 时，仅可用 Codex 支持的会话级归档入口停止精确 UUID，不得按进程名批量终止共享服务。确认活动会话记录已移入归档、协调状态 idle、双方无在途批次、最后批次已 ack 且目录全部顶层 NDJSON 快照稳定后，可在上述命令追加 `--force-user-authorized`，回执以 `user_forced_stop=true`、`session_archived=true`、精确 `archived_session_id` 和非空 `archive_method` 代替旧 owner 主动确认。缺少任一证据仍拒绝；归档保留会话历史，不等于删除。

### 无接收者的旧 owner 退役

固定四个采集名额之外的旧/多余 owner，或已确认死亡且无需转交原目录的 owner，由主线程按上面的流程确认停写、转交或取消在途任务、处置搜索 claim，再用 `coordctl.py` 的 `retire-idle` 移除活跃登记。仍活跃的旧线程先结束其任务并停止，不能仅凭 idle 直接删；也不能因有旧任务就无限期保留失效 owner。

```text
retire-idle --state STATE --owner OLD_THREAD --work ORIGINAL_WORK --reason 具体原因 --evidence RETIRE.json --logs-stopped
```

`RETIRE.json` 包含匹配的 `previous_thread`、`previous_work`、`user_authorized=true`、`owner_stopped=true`、非空 `method` / `summary` 和原工作目录全部顶层 NDJSON 的排序快照 `journals: [{name,size,sha256}]`。停写声明须由真实宿主终态或旧 owner 的停写回执支持。命令在共享锁内复核空闲状态和双遍日志快照，将登记移到 `retired_threads`；它不会删除目录或搜索 claim。主线程还须完成上述归档后的原目录清理和搜索占用清理，核对全部旧 owner 无活跃残留；保留已核验数据及可追溯的历史审计。

每线程只读取自己 claim 返回的 URL 和本目录日志。导航通过已批准浏览器，按 CUA 工作流读取当前公开 DOM。独立标签、顺序采集、逐页持久化全部实际分集/线路/地址；可复用 `capture-batch.mjs` 和已审核的 DOM 助手，显式注入自己的工作目录与异常 writer，不能沿用其他线程的默认 writer。

完整地址页追加 `details.ndjson`；仅原始编号页追加 `raw-details.ndjson`，不得拼地址或标 captured。一次页失败按有界恢复规则处理，保留已成功页；验证码/权限/用户标签冲突时暂停相关浏览器操作。不能为赶进度播放、下载或绕过限制。

### 用户明确恢复直接退役的原身份

仅当用户明确改为恢复某个原 UUID、宿主协作入口已唤醒该原会话并由其确认身份和停写，且此前是无接收者的 `retire-idle` 时，可使用以下入口；它不替代死亡线程换新规则，也不修改默认人数。`idle`、`notLoaded`、快照不变或消息已投递均不能单独代替本人恢复/停写确认。

```text
reactivate-retired-idle --state STATE --thread ORIGINAL_UUID --work ORIGINAL_WORK --reason 用户本次恢复原因 --evidence RESUME.json --logs-stopped
```

`RESUME.json` 必须包含匹配的 `thread` / `work`，均为 true 的 `user_authorized` / `same_identity_resumed` / `owner_stopped` / `search_claims_checked`，非空 `method` / `summary`、全部顶层 NDJSON 的排序快照 `journals`，以及当前完整退役记录经 `coordctl.retired_record_digest(record)` 计算的 `retired_record_sha256`。先只读核对原 owner 的搜索占用，保留同身份认领，不接管他人查询。证据只记真实核验结果。

程序持唯一状态锁核查原记录无 `replaced_by`、无在途批次、退役摘要未变、原目录不与任何已登记目录重叠，并双遍核查日志。成功仅恢复相同 UUID 和原目录的 idle 登记，完整退役回执移入 `reactivation_history`；不改任何批次、队列、搜索认领或目录文件。重复恢复拒绝写入，此时先检查 `current`，不要重建身份。曾被其他身份接管的退役记录仍使用上面的逆向交接流程。恢复后由原会话核对 `current` 并续行，主线程不得伪装该身份采集。

### 互助修复与共享环境

环境、浏览器运行时、标签失效或采集异常由所属线程先保存断点并报告，主监控 agent 负责诊断、安排修复和核对恢复结果；所属线程仍负责自己的任务书、日志和入库验收。必要时请求或提供有范围的诊断、修复与交叉验证，先互通已试步骤、短错误和影响范围，约定修改执行者与验证者，避免多人同时改同一文件、权限或服务。共享环境修复若影响其他在途任务，先共同确认停写检查点和维护窗口；局部故障不阻塞健康线程。互助不意味着默认接管他人工作、抢在途任务或接管用户标签。

保留已成功日志，有界重试；需要停写时如实逐页标 failed/skipped/raw，不假装采完。协助者和故障线程均须遵守现有权限及安全边界，不能绕过验证或擅自提权；确需额外授权/用户操作时，负责当前修复步骤的线程提出最小需求。修复后由所属线程做最小启动和公开 DOM 验证，必要时由其他 peer 交叉验证，回报真实结果并互相通知恢复。队列 retry 可由任一已注册 peer 根据修复证据显式办理，不要求某个固定线程审批。

本批结束后停止对自己的日志追加，为批次中每个 URL 写一条结果，未尝试页也必须报告，不可静默遗漏：

```json
[{"url":"实际已领取的详情页","outcome":"captured"}]
```

`outcome` 仅为 `captured`、`raw`、`failed`、`skipped`；这里 `captured` 只表示完整观察已落盘，不是数据库验收。

```text
ready --state STATE --thread THREAD --batch BATCH --results RESULTS.json --logs-stopped
```

ready 后保持目录静止，不能立刻领下一批或继续补日志；由本线程自行按下一节入库、核验、同步封面并 ack。其他 peer 协助收尾前先与 owner 约定单一处理者，避免同时对同一目录执行 finish。等待锁或协助结果时使用产品等待机制，不高频轮询。任一其他线程不在线不妨碍本线程正常入库和协调；不能因此篡改他人 owner 或丢弃日志。

若本批每个 claimed URL 都已在本线程 `details.ndjson` 尾部形成完整 `captured` 观察，并已明确停止日志，优先执行：

```text
python -B scripts/collection/workflowctl.py complete-captured --state STATE --thread THREAD --batch BATCH --logs-stopped
```

此兼容命令只处理全 captured 批次，并受与新批相同的封面门禁。新批默认用 `workflowctl.py complete-detail --thread THREAD --batch BATCH --logs-stopped`：按任务书固定日志边界验证 captured/raw，自动生成 results、ready、带数据库写锁的 finish/finish-raw、逐页 audit，再为 captured 节目同步完成封面门禁和批次回执，最后才 verification 与 ack；未观察页用重复 `--failed URL 原因` / `--skipped URL 原因` 明确声明。raw/failed/skipped 不要求封面，仍待复核且不计完整节目。已 ready 的批次重入复用原真实结果；半行、URL/归属冲突、核验或封面失败拒绝 ack。仅程序明确拒绝的异常才使用下述分步协议，不手拼可由程序生成的证据。

## 各线程自行入库、核验与去重

本线程提交 ready 且日志停止后，从项目根执行 `python -B Datebase/xxmanmi-resource-db/batchctl.py finish --work ABSOLUTE_WORK --tail <本批完整观察数>`。不同 peer 可各自收尾自己的批次；不要同步仍在写入的目录。同一批次的协助收尾先交接处理权，不凭超时抢写。普通完整页须逐页核对 DOM 数、原始地址、分集、线路、媒体格式及 captured 状态；raw 单独增量同步并比对原始 JSON，不计完成。无完整详情时不能 audit 历史 tail 冒充本批或把 raw 计数当完成；使用本批实际逐项对账证据。ready 是协议上的停写承诺，不是物理文件锁，执行者须核验日志完整性和断点，异常则不 ack。

去重有三层：共享状态锁保证同一详情 URL 只由一个在途批次拥有；数据库唯一约束保证相同 `source_url` 只有一个节目、同节目同媒体 URL 不重复建资源；事务与幂等检查点保护重复同步和并发提交，较旧观察不得回退较新状态。分集与线路关联完整保留，不用标题、同系列或媒体格式合并不同详情地址。同一原始地址的不同有效标签/线路是需要保留的关联，不应被“去重”丢弃。不要绕开程序直接写 SQL 或重置检查点。

数据库写入采用**各 peer 原子取锁、轮流写入**：新版 `dbctl` 写入口与 `batchctl finish` 共享数据库目录下 `.runtime/database-writer.lock`。`finish` 在首个状态检查前取锁，持有至同步、规划、逐页审计和最终结果证据生成结束；内部短数据库事务再做锁内检查点复核。锁默认最多等 30 秒，失败明确退出，异常只释放仍属于自身 token 的锁，不删除未知陈旧锁。其他线程可继续采集自己独占的页面，只在需要写库时排队。启用新版前确认旧版写入进程结束；长期导入模块的进程需重新加载，不能假定旧实例自动获得保护。独立 `coordctl ack` 不写 PostgreSQL，在 finish 释放写锁后据实际证据调用，不宣称跨两个 CLI 一直持有数据库锁。

数据库同步和逐页审计成功后、ack 前，收批程序必须把本批每个 captured 节目交给独立中央封面 worker；worker 在全局封面锁内用全库指纹/标题状态核验已有封面或安装新封面，并把逐项结果原子写入绑定 batch/work 的回执。封面只取该节目已核验资源，经本地 relay 有界解码真实帧；不得浏览器播放取链、猜 URL，或用重复图、坏图、文字卡补位。异步 enqueue、文件存在和非中央 writer 的输出都不证明完成。任一 captured 项未通过时批次保持 `ready/cover_pending`，不得 ack 或 claim 下一批；raw/failed/skipped 不进入门禁。重入须复用回执和幂等结果，不能重复抓帧或绕过锁。

核验无误后，本线程或已约定的协助 peer 准备与该批次和原工作目录绑定的核验文件，并 ack：

```json
{"batch_id":"BATCH","verified":true,"verifier":"逐页核验、必要 raw 对账及封面门禁","work":"本批原绝对工作目录","sync_ok":true,"audit_ok":true,"cover_ok":true,"cover_receipt":"绑定本 batch/work 的回执路径","evidence":{"method":"实际使用的核验方法","checked_items":4,"summary":"本批每个 URL 的资源与封面核验结果"}}
```

```text
ack --state STATE --thread THREAD --batch BATCH --verification VERIFY.json
```

核验字段是执行者的外部证据声明，协调程序不能替代数据库或封面检查。须明确提供匹配的 `batch_id`、原 `work`、成功同步/审计、`cover_ok=true`、可读的绑定回执、非空 `evidence.method` / `summary` 及精确等于本批条目数的 `checked_items`（示例为 4，实际按批次填写）；不能只完成同步、只看全库总数/封面文件增加或转述 worker 声称成功。失败/待补保留复核状态，不自动重排；修复后由有实际证据的注册 peer 显式发起新尝试，不删除整批或清空数据库。`release --state STATE --thread THREAD --batch BATCH --reason 具体原因 --logs-stopped` 仅在确认原 owner 停写且交接清楚时执行；不释放仍在运行或无法确认的批次。重复 ready/ack 明确拒绝并保留原状态，不改批号强行重放。

确认故障已解除且允许再次尝试后，任一注册 peer 可用 `retry --state STATE --thread THREAD --items RETRY_ITEMS.json --reason 真实修复依据`，输入为 1–8 个 `{url}` 对象。只允许已有 `manual_review` 项；未知、重复、queued/claimed/ready/acknowledged 任一项使整批拒绝。成功仅将指定项各放回队列一次，保留旧批次/结果/核验证据并记录重试次数与原因；不会自动重试编号页、权限失败或仍在运行的页。随后正常 claim；不能用重复 enqueue 绕过待补审核。

任何peer补充共享详情队列后，各已ack的agent按下一份有界任务书claim小批，无须用户逐批分工。新增URL只能来自已观察的公开列表、来源、真实下一页或数据库真实pending；不经关键词供给、分类或系列归组。coordctl只调度详情URL，不把搜索任务伪装成详情页，不据空队列认定全站穷尽。

## 正式记录与最小监督

采集者保存原任务、批次结果、待补/失败和真实阻塞，复用正式事件与回执内部上报；正常成功只落程序计数，不逐批广播。主线程只定向恢复真正中断的原线程，健康线程自主执行，见[短流程](main-supervision.md)。正常、修复和停工均不向用户汇报，只显示`working`，不调用差量报告入口或另建报告文件。

规范不包含分类Skill、核验步骤、标签补齐、系列归组或新分类字段写入要求。既有节目、资源、分类、系列及关联、任务、claim、证据和回执保持原样，不归档、迁移或删表；规范修改不改网站或分类程序代码、不变更登记、不联系或恢复暂停线程、不发布生产。

仅异常时展开精确owner的current、任务书和回执，不循环读取健康线程或全库status。首次真实故障时间持久保留，超过一小时仍未恢复按正式停止流程关闭写闸、中断本项目采集并核验实际终态；共享服务和所有数据保留，不汇报、不自动恢复。

同一详情页只算一个节目，不同详情URL分别保留；journaled、ready和ack不能代替数据库真实完成数。并发finish的全库差量不能直接作为某线程新增或相加，使用绑定URL的核验证据与同一来源过滤的真实快照。不猜数量、不把queued/pending当生产成功、不宣称未经验证的无人值守能力。
