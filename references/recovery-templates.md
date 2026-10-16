# 固定恢复模板与停机预算

`recoveryctl.py` 只选择已验证入口并计时，不编造DOM、私有action或工具回执。主线程与采集者共用，不为已知错误重复推理。

压缩或迟到消息中的旧 capture 错误不重新开启故障周期。正式 record 仅在原 owner/parent/cwd 的唯一 canonical 原生调用、完整绑定、真实完成时间，以及同 owner 已 verified/captured/无待补封面的 batch ACK 全部证明“原调用先完成、同批 ACK 后完成”时忽略迟到重复；缺少证明仍正常登记并保留首次预算。已误登记的单一 unknown 旧报告由正式 progress 记录该 ACK 边界解除，原首次时间、deadline、原记录及哈希全部保留，并在持锁提交时核验没有新故障变化。证明曾被拒绝的旧代码只是故障历史，不会使旧回执可用于 capture/import，不覆盖未终态或含其他未知动作的事件。

故障修复期间对用户只显示`working`，不汇报修复日志、进度或技术细节；原错误、断点、证据和验收回执仍由既有正式流程保留，不另建报告文件。修复后继续主线程监督和原线程采集，不以回复状态后退出代替执行。

```powershell
python -X utf8 -B scripts/collection/recoveryctl.py plan --error-file <原错误回执>
python -X utf8 -B scripts/collection/recoveryctl.py record --thread <真实OWNER> --error-file <原错误回执> --source-tool <实际工具ID>
python -X utf8 -B scripts/collection/recoveryctl.py check
```

CLI异常已自动记录。浏览器直接报错由原owner记录一次，按JSON的`template/action/reference`只读取匹配段；不发送普通进度。模板覆盖Chrome不可用、工作标签缺失、kernel重置、存储配额、搜索证据门禁、共享搜索锁、封面门禁、数据库及身份/审计冲突。

真实已返回的浏览器错误应立即计入既有故障预算；`record --source-tool`为可选项。暂时读不到实际调用ID时可省略该参数，保留原回执，不能因等ID延迟计时，也不能用函数名、cell编号或猜测值代替。主线程可从原owner原生回执核实真实调用与终态；补记来源仍保留首次故障时间。

当前业务主已核验真实错误并明确授权固定恢复后，`check` 中继续列出未解除 incident 是保留故障预算，不等于停工。`ok=true`、`stopped=false`、`stop_required=false` 且未超时，原正式入口也允许继续时，应沿已授权断点完成恢复，以新真实 ACK 或正式搜索提交解除故障；不能先清空账本，也不能仅因旧 `unknown` 仍在而拒绝已授权的恢复。该规则不授权重放未知或在途动作，不覆盖实际关闭的写闸或其他正式拒绝。

公开列表的本地来源筛选点击与采集提交须分开处理。若当前业务主从原生回执确认，唯一未知操作仅为已观察的首页来源 radio/label 点击，调用已终态失败并返回 kernel reset；原 owner 的新鲜批准浏览器库存又确认原标签已消失，且正式状态无活动批次、没有与该点击关联的搜索 claim、prepared action 或写入命令，则可定向授权原 owner 建立同号唯一新标签，从已授权首页重新观察。主线程的原生授权消息须引用实际失败调用、原标签 ID、成功库存调用和正式 current 核验。旧点击结果仍标记 unknown、原回执与故障预算保留；不重放旧点击、不把旧页面结果入队、不据此解除 incident 或伪造 ACK。这个窄路径不适用于搜索提交、采集/写入、下载、私有 prepared action、仍存在的原标签或任何未终态调用；这些仍走原句柄及正式隔离门禁。

- 先取当前正式owner、work、任务、batch和原句柄；所有参数解析正式JSON，禁止手写目录或抄PTY折行。详情工作标签丢失只能恢复本人标签并续未完成页；搜索保留原模块及action，按精确门禁处理，不重放。
- CUA 会在回合结束后自动清理未保留的自建标签；REPL 变量仍存在、上一回合创建成功，都不能证明该标签仍在。续跑先取同一浏览器绑定的新鲜库存；满足原详情缺失门禁后，仅恢复本人标签，并用实际新变量与新 `tab.id` 重新生成 `prepare` 代码，不能沿用旧 ID。只有确需从现场继续的未完成工作，才按当前浏览器文档在结束回合前 `markHandoff()`；不为普通来源页或已完成任务保留标签。读取完整 API 文档用 `cua.rewriteDocumentation()`，不把库存或 AX 输出未附带 API 文档误判为接口不存在，也不去另一个 Node runtime 查找 CUA 对象。
- 当前 Chrome 扩展的 `createBrowserTab` 不支持 `visible` 选项，创建本人任务标签时省略该字段，仅按有效文档传 `sessionName`。`Capability is not available: visibility` 是参数拒绝；核验本人新鲜库存确认未创建标签、无在途动作后，可省略该参数恢复一次。不要把后台辅助进程隐藏要求转换为 Chrome 不支持的标签选项，也不要因此另起浏览器进程。
- Chrome明确不可用后按Chrome Skill重连同family，读取本人公开库存；不重置Node私有证明或操作用户标签。无法恢复的页面，按已授权有界流程回本人站点主页，再续未完成任务。
- 原浏览器变量不存在但已初始化agent仍在时，先走[原浏览器对象丢失](search-fault-recovery.md#原浏览器对象丢失或标签失效)的只读绑定核验。`agent.browsers.list()`只证明浏览器可用，不能代替本人`browser.tabs.list()`；用清单中同一Chrome的真实ID重新取得浏览器对象后才读取标签库存。保留原tab/action/module，不重置kernel，不把库存成功当作故障恢复；未知动作仍禁止重放、导航或新建标签。
- 已终态的公开页面导航报 `net::ERR_CONNECTION_CLOSED` 属于浏览器网络故障，不按数据库连接关闭处理。保留原调用和首次故障预算，先读本人绑定的实际标签库存及新鲜页面状态；无在途或未知动作时，若页面仍不可用，可对之前真实DOM已观察的同一目标入口有界导航一次。成功后从新鲜公开DOM继续正式供给与采集；再次失败保留回执交主线程，不循环重试、不猜URL、不重启数据库或共享Chrome。导航故障仍由后续真实搜索提交或批次ACK证明恢复，不能把只读DOM成功冒充采集完成。
- 原线程续跑时，若正式`current`无active batch且没有在途或未知动作，先读取本Chrome绑定的最新公开标签库存，再复用确实存在的本人标签。存量ID已消失或本人库存为空时，仅恢复本人同号工作标签；不把旧ID、空库存或正常供给等待当作线程死亡。
- 搜索锁忙只检查精确锁与实际归属，等待已确认存活的写入者或做一次有界正式重入；未知锁不删。不是凭`idle/notLoaded`清理线程。
- `prepareSearchAction`已明确返回`Search preparation does not match one complete claimed public UI state`、未生成actionId且从未execute时，保留原claim、模块和本人tab，按`search_preparation_ui_mismatch`核对当前公开UI。搜索页与“最新”页的渠道控件不得混用；仅通过实际可见入口回到搜索视图，读取同一完整DOM中的原词、可见结果容器、唯一可见搜索渠道按钮及其真实选中状态。现场重新核实选择器和原任务书后，原owner有界重入一次prepare，成功才按返回的next_action继续execute/finalize/commit。不能改DOM样式、从claim补造选中状态、绕过原门禁或重放execute；原prepare仍在途、已经有actionId或任何execute结果未知时不适用。再次拒绝保留新回执交当前业务主，不循环prepare。
- finalize精确报`Zero results require a visible empty-result indicator`时走`search_no_visible_empty`模板：保留原prepare的tab对象、原模块/action和实际execute/finalize回执，停止本标签导航，读取当前真实DOM。原词/渠道仍匹配时用既有host-mismatch门禁；已经导航离开时不可把历史DOM当当前DOM，按已授权retained-action入口核验当前真实快照。门禁拒绝只保留证据交主线程，不重放、补写空态或堆叠新动作；只有正式隔离和原对象release成功后续采。
- 当前详情页真实输入null触发`Parent-host quarantine requires an actual complete read-only query/channel DOM snapshot`时，原owner保留完整原JSON及原动作，仅按`search-fault-recovery.md`的current-detail窄门禁重入一次既有`quarantine-search --authorized-retained-action`；程序保留并区分匹配历史搜索DOM与当前详情DOM。不得把null改成查询词、制造空态、重新导航或清空私有Map。
- host-mismatch隔离报`evidence does not match the effective claim/task`时，先逐字段对照正式claim、task与证据，不直接重试。若其他事实均已真实匹配，仅证据reason误用了普通路径的`quarantined_unverifiable_result`，而本人实际foreign host满足专用门禁，则保留旧证据，由原owner在同一目录另存仅reason改为`quarantined_foreign_host_session`的派生证据，使用原模块/action执行一次既有正式入口。这是格式修正，不更改query/channel/身份/DOM/hash、不重新execute、不放宽门禁；任一真实事实不匹配仍保留断点。
- release报`Only a settled prepared action on its original tab can be terminally released`时，先核对同一action的实际prepare工具参数。search-evidence模块绑定的是原tab对象本身，不只是tab.id；相同ID的新包装对象仍被拒绝。复用prepare时保留且未被重赋值的原变量、原模块/action和已经成功的正式隔离回执，最多一次重入release；证据路径解析正式JSON，不手抄多重转义路径。不能重新get包装对象、重载模块或重新execute来修复。原对象已丢失或工具尚在途则保留断点，转对应私有证明恢复门禁。
- host-mismatch日志快照拒绝时不手填哈希或刷新旧证明。只有原证据缺少`journals`而已有真实`journals_before`和`journals_after`两数组完全相同、且仍等于当前停写日志时，原owner调用`workflowctl.py repair-terminal-journals --thread OWNER --evidence 原JSON --logs-stopped`。程序持锁双读，保留原文件，仅在同目录生成派生证据并补齐规范字段，不改DOM/action/claim/hash事实；输出不代表隔离成功。原owner再用派生路径重入一次既有隔离/release。任一快照不同或日志已变化则拒绝修复，保留断点，不更新旧哈希绕过门禁。
- 原canonical日志校验值仅抄漏一个字符时，同一恢复CLI可加`--original-snapshot-call 实际原工具调用ID`。程序从原owner的原生回执读取停写时成对且相等的原快照，只允许一个63位校验值恢复为原回执的64位值，其他字段完全相同，并仍要求原快照等于当前停写日志及锁内双读不变。原文件保留，派生证据记录原调用及输出哈希；不能提供新快照、替换64位旧哈希或刷新变化过的日志，也不重放搜索。修复输出仍不代表隔离/release成功。
- 封面默认经实际媒体域名DIRECT出口请求，先核验真实连接再读中央审计的候选、解码数和来源错误；多节目立即失败先诊断合法本地relay。质量/重复拒绝继续拒绝；原有界尝试耗尽而确实无合格封面的单节目，按[封面流程](cover-recovery.md)保留待补并跳过。四路共享连续失败计数，前1–6个可跳过、第7个强制全局暂停；真实合格封面成功才清零。系统、鉴权、数据库或未知工具错误仍走原故障恢复，不冒充节目失败。
- 用户明确要求删除并跳过某个片源不可用节目时，已核验停工的业务主可用 `program_skip.py --thread 主ID --request-id 本次用户请求ID --batch 原单节目批次 --program-id 实际节目ID --source-url 实际详情URL --reason 实际原因 --authorized`。这是删除入口，不适用于上述默认保留数据的封面跳过。入口只接纳原中央封面 `source_unavailable` 回执和已终态、无在途工具的原owner；先保存精确节目备份和生产可读备份，在既有写锁内删除该节目的本地与生产管理行，再独立核验不存在。原采集日志、ready结果、封面错误和旧故障周期保留；批次按用户取消释放，绝不记captured ACK。删除URL持久禁止重新入库或重排，其他节目和生产用户数据保留。入口不恢复采集；完成后仅用本次明确继续指令执行正式resume，沿原四路断点续采。此授权不扩大为删除未知动作、数据库故障或整个多节目批次。
- 普通页面无资源可保存真实failed/manual_review后续采；不把页面不可用或有真实活句柄的正常等待自动视为线程故障。
- 同一槽位真实故障计时写入`.runtime/main-agent/recovery-current.json`，首次时间持久化。重试、新回合、换模板或替换UUID不能重置。数据、审计、封面和未知动作故障仍须新的正式搜索commit或batch ACK证明恢复；原证据、哈希和门禁不变。
- 原生只读绑定探针仅包含请求身份、变量typeof和零参数`tabs.list()`，真实终态failed且精确报`Browser is not available`，登记迟于实际失败时，业务主可对同一原工具调用用`tighten-budget --authorized`收紧首次时间。入口只接受单次原`chrome_unavailable`、同owner/原父/cwd/work、当前周期、原生readOnlyHint与唯一文本错误哈希、真实完成时间及未变原回执；要求仍是该已终态原回合，且没有中间正式ACK/search commit。只保留真实首次预算，不解除故障、不重放或更换私有动作；原线程按同family正式恢复，新的真实ACK才能证明采集恢复。
- 原生滚动精确报`Timed out after 10000ms waiting for CDP command Input.synthesizeScrollGesture.`时，业务主可用`tighten-budget --authorized`保留真实首次时间。仅接纳本周期原owner/parent/cwd/work、最近三个原生回合内已终态failed的确切`dom_cua.scroll({x:0,y:1200})`及原固定输出语句、唯一完整错误和未变原生日志。若同一原回合中更早的原只读列表探针精确报`CDP operation exceeded its deadline before command dispatch`，入口同时核验完整固定只读脚本、同一tab变量和原生Chrome family/browserId、两个真实失败回执及时间顺序，以该更早回执计时；两类路径都要求没有中间正式ACK/search commit。不接纳其他手势、坐标或脚本；只收紧预算，保留`gesture_outcome=unknown`，不证明滚动未执行，不解除故障、不授权重放、导航或恢复采集。后续标签缺失等故障也保留此未知结果，不能用只读DOM进展解除，仍须真实ACK或搜索commit。
- 原单页采集IIFE未顶层等待且未保留Promise，原生`completed`只返回空text时，已绑定该工具与原批次的首次`unknown`可由登记业务主用同一`tighten-budget`收紧回执不可追踪故障预算。程序仅接纳原完整调用、此前唯一原生认领回执的同owner/批次/路径及有序四URL、当前完整正式输入及两份日志仍等于任务书边界的精确证明，沿用当前cycle、无中间ACK/search commit、原记录SHA和锁内复核；证明明确记录`completed/capture_outcome=unknown`，零增量不代表未执行，不能借此前移解除故障、释放资源或授权重放，先有正式ACK即拒绝。
- 单次登记为`unknown`的原生命令若明确在创建前失败，精确错误为`Failed to create unified exec process: helper_unknown_error: setup refresh had errors`，登记业务主可用同一`tighten-budget --authorized`入口。程序核验原owner/parent/cwd/work、当前cycle、唯一`CommandExecution`原回执、`unified_exec_startup`、failed/exit -1、零执行时长、实际完成时间和完整错误哈希，沿用锁内复核及无中间ACK/search commit门禁。只收紧首次时间，保留故障和断点；不重放命令、不修改沙盒或权限，不把执行器探针成功当作采集恢复。
- 单次登记为`unknown`的只读`dbctl.py lookup`若原生终态为failed/exit 1，且精确错误为Docker Compose启动时`The paging file is too small for this operation to complete.`，登记业务主也可用同一`tighten-budget`入口；仅允许`attempts=1`、无batch/command、来源等于原登记工具，程序核验原owner/parent/cwd/work、当前cycle、真实命令及去除真实ANSI控制后的完整错误哈希。复用原生日志唯一回执、完成时间、行哈希、锁内复核和无中间正式ACK/search commit门禁，只收紧首次时间及截止并保留登记字段；串行只读重验成功仍须新的正式ACK或搜索commit才能解除故障。
- 已登记的`public_navigation_connection_closed`若登记晚于真实首次失败，仅登记业务主可调用`recoveryctl.py tighten-budget --thread 原OWNER --source-tool 首次真实失败工具ID --authorized`。程序从RPC指定的原owner原生日志核验首次终态failed及当前同链失败（后者可为捕获同一异常的终态回执）、同Chrome绑定和当前cycle，且中间不能有正式ACK、搜索commit或其他Chrome失败造成的可能恢复边界；此导航错误本就不适用只读DOM恢复门。只把首次时间及两小时截止收紧，保留原登记时间、原回执和attempts，不接受手填时间/路径，不延后、不清除故障、不授权重放；重复或更晚时间拒绝，其他故障类型仍保留原预算。
- **只读Chrome故障已恢复而详情供给为空时，不等待不存在的新节目ACK。** 仅当前登记业务主可调用`recoveryctl.py progress --thread 原OWNER --browser-source-tool 实际成功DOM调用ID --authorized`。程序在最近三个原生回合内核验原owner、parent/cwd/work及同Chrome绑定；原错误必须是实际只读evaluate的标签缺失或DOM超时，或整段仅为静态locator声明及同一变量零参数count/isVisible/innerText读取的精确`Playwright selector deadline exceeded`，两类调用与错误不得混配。新已完成回合须晚于最近故障，成功仍须为只读evaluate原生回执，包含新鲜公开页面URL、完整readyState与真实链接；原失败之后的中间及后续回执不得还有其他失败工具。本owner无active batch、共享队列无可领项才适用；原计时保留并记录恢复证明，不计节目成功、不改队列、不绕过任何数据门禁。其他未知动作、kernel重置、数据或封面故障不得使用。轻量读取限制节点数并优先`textContent`，一次有界尝试不超过30秒，仍失败则保留原断点。

全项目连续第7个不同节目取不到合格封面时立即强制暂停，不等待两小时；任一槽位连续故障超过7200秒未恢复时也执行全局停工。两类条件分别使用真实正式证据，先全局关采集写入闸，保存既有日志、批次、同步队列与断点，按已登记真实身份暂停Goal并中断本项目01–04和主会话/旧relay；用户已授权随后强制终止所属采集后台命令与全部已核验的专属进程。正式停工复用`recoveryctl.py stop --authorized --thread <真实主线程UUID>`与`process_stop.py`的精确owner、PID/启动时间/可执行路径核验和固定句柄终止；若入口尚不支持封面连续失败原因，先适配既有入口，不伪造两小时超时绕过。核验实际退出并保存正式回执，保护当前停工驱动直至回执完成；共享宿主、桥接/MCP、用户浏览器、数据库、网页服务及其他项目保留。不按名称批杀、不删数据、不重放未知工具；失败保持写闸关闭及`stop_complete=false`。完成正式回执后停止，不向用户汇报、不自行恢复，仅保留最小working状态。显式用户停止/暂停仍优先。

截止检查复用每次正式CLI和主会话的原生续跑安全点，不另装监督服务或定时器；未验证宿主唤醒时不得声称有硬实时停机保证。未知故障保留原工具与断点交主线程判断，仍受同一截止限制，不无限试探。

用户明确授权单槽位清理替换时，先由原owner完成一次有界恢复；真实门禁仍阻塞才按清理Skill删除精确会话、退役owner、验证归档和专属目录移除。原宿主以无历史fork的新UUID补同号 `gpt-6-luna/medium`，新owner仅注册独立目录并结束原生准备回合。真实业务主用 `recoveryctl.py bind-replacement --authorized --thread 主UUID --request-id 本周期新用户请求ID --slot 同号 --owner 已清理旧UUID --replacement 真实新UUID` 正式衔接。入口核验原生父、模型、准备回合终态、清理回执和四路唯一注册，持锁只更新该槽位及main/policy的替换回执；原故障首次时间、截止、旧owner/工具与未知效果保留，库存成功不解除故障。绑定后原宿主定向续新owner，真实正式ACK或搜索commit才证明恢复；无明确删除授权不得用此入口替换浏览器故障线程。

停用返回`ValueError`且原生cwd为共享workspace父目录时，不扩大目标到所有workspace线程。仅固定登记的主会话/原relay/01–04可处理；原生ID和cwd必须匹配项目或其直接父workspace，采集者还须协调角色为正式`collector`或`peer`、登记work等于slot work、真实目录位于项目内且原生parent为原relay。满足这些实际事实后重入`recoveryctl stop --authorized --thread 主ID`，重新读取各Goal和turn终态；旧失败回执、paused文件或interrupt已接受均不能当作全部停用成功。

停工驱动在回执完成前退出、留下`stop-execution.json.lock`时，不手删锁。用户明确要求重试后，仅当前真实业务主可执行`recoveryctl.py recover-stop-driver --authorized --thread 主ID --request-id 本次真实用户请求ID`。入口核验关闭的写闸、原六个登记身份与父线程，并通过Windows进程句柄证明原锁PID已退出；PID复用、存活或身份不明均拒绝。它独占原文件句柄后，先沿既有精确停工流程补齐仍在进行的原线程与所属采集命令，再核验原生Goal/turn实际终态、后台命令和专属进程已停止。停工恢复驱动保留到回执完成；核验失败时保持原锁与写闸。全部通过后将原锁全部字节原子保留到同目录并核验哈希，不处理协调、写库、搜索或采集锁，不恢复采集。随后重入正式`stop`核对实际终态回执；只有用户明确要求开始新周期时才执行`resume`，当前已消费的重启请求不能再次复用。旧故障周期、锁证据和原owner断点全部保留。
