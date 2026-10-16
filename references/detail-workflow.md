> Historical reference only. Current execution uses single_agentctl.py and Playwright; do not resume collector subagents or CUA. See low-token-workflow.md.

## 详情：一次领批、一次采集、一次收批

当前 Node REPL 每次浏览器 DOM/导航调用显式设置 `timeout_ms=120000`，详情单页段与正式搜索模块调用设置 `timeout_ms=240000`，避免默认30秒或60秒超时重置 kernel。外层 `functions.exec` 提前 yield 后只等待原 cell；不把外层 yield 当成调用失败、不重放未知动作。kernel 确已终态时按当前 Chrome Skill 重新 bootstrap 同一浏览器 binding，从正式 claim 与落盘证据恢复，不能用宿主异常判节目失败。

```powershell
python -X utf8 -B scripts/collection/workflowctl.py claim-detail --thread OWNER --limit 4
```

可重入：优先恢复自己的 active batch；没有才原子领取。自动生成任务书、固定日志证据边界与 `capture_input` JSON，只返回未采条目和 `next_action`。在本线程安全检查点执行，不与同 owner 的采集同时运行；已落盘项不会重新导航。`remaining=0` 按 `next_action` 收批，`status=empty` 先检查真实pending，再按[公开列表流程](cua-workflow.md#列表发现)从本人已观察列表、来源和真实下一页补充详情URL，经唯一共享队列去重后领批。无法继续时保留断点并内部上报真实阻塞，不创建关键词、榜单发现或分类任务。空队列不代表目标完成，不循环空领。

Windows PowerShell 读取输入 JSON、任务书及参考文档时，显式使用 `Get-Content -Encoding UTF8`；Python 使用 `read_text(encoding='utf-8-sig')`，Node 使用 `readFile(path, 'utf8')`。未指定编码的终端输出出现乱码时，先以 UTF-8 解析实际文件并对照正式 claim，不将显示乱码认定为落盘损坏；文件和 claim 一致则继续原批次，不改写数据、重入队或登记虚假损坏故障。

批准的浏览器初始化完成后，先按 CUA 工作流打开并右侧停靠开发者工具，核对实际 DOM 选择器。必须读取 `#list a[data-web]` 属性与数量；可访问性快照没显示 `href`、页面出现“更新验证码/验证码期限0天”，都不证明资源不可读。不得据这些提示跳过正式模块、先点验证码或手工声明 failed/skipped。随后在宿主允许的 Node REPL 中只加载一次项目模块；`tab` 必须是自己已有的任务标签。以下 `CAPTURE_INPUT` 直接使用 CLI 返回的路径，`MODULE_URL` 是项目 `scripts/collection/detail-capture.mjs` 的实际 file URL。

```javascript
var xxmmFs = await import('node:fs/promises');
var xxmmDetail = await import(MODULE_URL);
var xxmmTask = JSON.parse(await xxmmFs.readFile(CAPTURE_INPUT, 'utf8'));
console.log(await xxmmDetail.runDetailBatch({
  tab, items: xxmmTask.items, workDirectory: xxmmTask.work, name: xxmmTask.owner
}));
```

采集调用必须在工具顶层`await`到真实结果；裸`(async()=>{...})()`若未等待且未保留Promise，宿主`completed`或空输出不能当作采集完成，仍按结果未知的原断点流程处理。

宿主的 MCP 调用若有 300 秒硬上限，保持原来每批4个节目的认领与四线程并发，改用模块的 `runDetailBatchSegment(lane, options)`：每次只运行一个详情页的同一正式采集器，结果 `next_action=capture_detail_segment` 时，下一次宿主调用再把 `items` 设为返回的 `remainingItems`，并把 `alreadyLoadedFirst=false`（仅现场已加载的首段可为true）。不要把多段循环塞回一次宿主调用。`inspect_detail_failure` 时先停止lane并有界核对该页实际错误；诊断结束前不继续未访问页。该页确认真实读取失败后，只为该URL保存实际失败原因，再用本段返回的 `remainingItems` 在原批次逐页采集其余未访问页，不重放已落盘的 captured/raw 页。全部条目各自完成采集或实际失败诊断后才统一收批，不能因前页失败就把后续未访问页批量 skipped。重启或结果未知时先用正式 `claim-detail` 的remaining恢复，不重放原段。新模块需在无在途调用的检查点按mtime破缓存导入；不能为更新模块丢弃旧在途实例。

模块通过当前标签的 `goto` 和受限 `playwright.evaluate` 读取公开 DOM，每段不超过 1000 条，核对页址、标题、总数、索引、分组与快照一致性，逐页持久化到原目录。返回 `captured/raw/journaled/failed` 等短计数；`journaled` 不等于入库。单个 agent 仅一个 lane；不创建浏览器、不发送站点 HTTP 请求。其他批准浏览器可注入 `readDom(tab,fn,args)` / `navigate(tab,url)` 适配，不能借适配绕开权限。

当前宿主若只提供原生 `cua_repl`，且不能在该 runtime 导入本地 Node 模块，使用 `native_detailctl.py` 的两阶段适配。不要跨 runtime 传递 tab、猜内部 bridge 或重新建立浏览器连接。原 owner 在正式 `claim-detail` 后，对本人已加载并检查过的详情页执行：

```powershell
python -X utf8 -B scripts/collection/native_detailctl.py prepare --thread OWNER --batch BATCH --url OBSERVED_URL --tab-variable OWN_TAB_VARIABLE --tab-id OWN_TAB_ID --browser-id OWN_BROWSER_ID
```

PowerShell 中可将同一次 `prepare` 的输出先赋给 `$prepared = <原 prepare 命令> | ConvertFrom-Json`，再显示 `$prepared.code`，直接取得已解码的代码字段；不要手动拆解整份 JSON 的引号或反斜杠。`string` 格式内层仍有必要的转义，不能再次解码或改写。

每段都重新调用对应 `prepare --start N` 并复制该次完整 `.code`；不能只把 metadata 绑定头的 `start:null` 改成数字。分段还需要生成器提供 `evaluate` 的 `start/limit` 参数，只有 `episodeDomCount`、没有 `entries` 的观察仍是 metadata，不能导入为分段。`compact` 也保留生成器的全部空格，例如 `querySelectorAll(selectors.lines)) .map` 中的空格不能省略；不按视觉效果重新排版或抄取 PTY 折行。错误链保留，改用完整新链，既有代码逐字门禁不变。

原生 CUA 返回较多条目时可能截断 JSON，或把截断文本放入外层工具结果；`completed` 不代表得到完整观察。若正式 receipts 缺少大段回执，保留旧结果，核实截断后在本页 `prepare`、`receipts`、`import` 全部显式加 `--segment-size 100`。先完整新读 metadata，再按其实际总数执行 `--start 0、100、200……`，每段最多100条；不混入旧1000条大段，也不补写被截断的数据。默认1000及旧生成代码保持兼容。所选大小绑定原生代码和输出、连续偏移、总段数及正式 writer 的完整DOM指纹验证；原owner、来源、代码字节、时间、独立审计/封面和ACK门禁全部保留。

复制字符串时若反复损坏内层引号，可在本页三个阶段统一使用 `--code-format compact`。它把同一个正式 reader 的独立注释行和行首尾空白移除，以空格连接为单行函数，直接使用文档支持的函数参数；reader 及参数的语法树与原 `function` 格式相同，省去 `string` 格式的第二层转义。旧两种格式字节不变，所有格式仍逐字匹配所选生成代码。准备结果直接取 `.code` 作为 `cua_repl.js` 的 `code` 参数；不得增加 Base64、`atob`、`eval`、`new Function` 或哈希搬运封装。这些封装不是生成的原样代码，CUA 还会拒绝从字符串生成代码。任一段提交失败后，重新生成并执行完整 metadata 与全部 segments；旧 metadata 与重试段之间夹有失败工具调用时，原严格门禁仍拒绝导入。

若多次出现模型重新排版造成的 `generated_code_mismatch`，可在本页的 `prepare`、`receipts`、`import` 全部显式加 `--code-format string`。它使用浏览器文档支持的 `evaluate` 字符串参数，将同一个正式 DOM 读取函数的调用表达式及原参数完整 JSON 转义为单行代码；函数、注释和空白均保留，不接受手动改写或旧格式的近似代码。默认 `function` 生成字节保持兼容；两种格式各自仍须与所选格式的完整生成代码逐字相等。同一页切换格式时从新鲜 metadata 开始完整重读全部 segments，不能混用旧观察，已落盘的其他页不重采。缺少实际 `observation` 的完成回执也必须拒绝，不能把工具 completed 或仅有绑定头当成 DOM 读取成功。

将返回的 `code` 原样提交本人原生 `cua_repl.js`（`timeout_ms=120000`），取得 metadata 的真实 call ID。保留代码中的换行、缩进和注释，不手动压缩或改写；`receipts` 的 `rejected` 若报告 `generated_code_mismatch`，原调用不能用于入库。保留旧回执，从同一原任务、本人标签重新生成并执行完整的一组 metadata 与所有 segment，不混用新旧观察。随后用同一命令加 `--start 0` 生成首段；按实际 `episodeDomCount` 及所选 `segment-size`（默认1000，截断恢复用100）逐段生成和执行。从 metadata 到最后一段之间只执行生成的 DOM 读取代码，允许 shell `prepare`；额外的 `url()`、DOM 探针、`getState` 或 `rewriteDocumentation` 同样会打断链。库存、导航和文档恢复应在 metadata 之前完成；链被打断后重新完整采集，不放宽连续性门禁。保留所有真实 call ID，不手写 DOM 内容。准备代码本身不读页面、不创建新认领。

工具显示未包含 call ID 时，不必结束原线程或重读页面。完成各段后，将上述 `prepare` 改为 `receipts`（保持全部绑定参数，不带 `--start`），由本人 CLI 只读最近两个原生 turn 的真实回执；返回 `start=null` 的 metadata 和各段对应的实际 `call_id`。这一步不写日志，正式导入仍会重新核验完整证据。metadata、各段和导入应在当前连续工作中完成，避免回执超出有界窗口或一小时有效期。

```powershell
python -X utf8 -B scripts/collection/native_detailctl.py import --thread OWNER --batch BATCH --url OBSERVED_URL --tab-variable OWN_TAB_VARIABLE --tab-id OWN_TAB_ID --browser-id OWN_BROWSER_ID --metadata-call ACTUAL_METADATA_CALL --segment-call ACTUAL_SEGMENT_CALL
```

多个分段重复 `--segment-call`，顺序与 DOM 索引一致。原 owner 的 CLI 从官方只读 native receipt 取回真实结果，核对父子身份、浏览器、标签、原认领、完整代码、调用终态、顺序和时间，再复用 `detail-capture.mjs` 的全部一致性与原始资源校验和正式 journal writer。它不调用浏览器工具、不请求站点、不生成 ACK；日志已落盘时幂等返回。随后重新 `claim-detail` 确认 remaining，并照常执行下面的 `complete-detail --logs-stopped` 审计、封面和 ACK 门禁。任何证据缺失或读取权限错误都保留原批次，不换入口绕过。

Windows 沙盒无法访问宿主控制 socket 时，不扩大 app-server、认证目录或 npm 目录权限。当前业务主可从官方 `thread/read` 核验原 owner 的 canonical session 路径，复用原生日志已有的只读权限；不增加写入权限，不开放控制 socket，也不扩大 sessions 目录权限。绑定保存于 `.runtime/main-agent/native-receipt-bindings.json`。原 owner 的 `receipts` 和 `import` 可加 `--native-session` 指向该绑定路径，直接读取原生追加日志最近 8 MiB 的真实完成事件；文件必须仍在原 CODEX_HOME/sessions 下，原生头部的 owner、父线程、cwd 均须与正式登记一致。仍核验原 batch、浏览器、标签、完整生成代码、真实终态、调用顺序、分段完整性和一小时观察有效期，并保存原始行偏移与 SHA-256；半行或在途调用拒绝。该入口不接受复制/手写回执、不重读网页、不访问控制 socket、不写原生日志；读取拒绝时保留原断点交主线程。

媒体格式判定仅在解析临时值时忽略URL末尾的ASCII空格，与浏览器`new URL`的既有格式解析一致；原journal、`dataWeb`、媒体地址原串及其SHA/去重身份不改，DOM完整数量和独立审计门禁保留。含空白的原始地址仍须满足既有网站健康谓词才可展示，格式识别通过不代表每条资源可播放。

CUA 的普通 `Tab` 与 DOM `Tab` 可能是不同接口。若当前实际打印的普通 CUA 文档未列 `playwright`，但同次 `Other Browser APIs` 明确提供 `agent.browsers.get`、`Browser.tabs.get` 和 `Tab.playwright.evaluate`，在原 `cua_repl` 内按本人已核验的同一 Chrome ID 与标签 ID 取得 DOM `Tab`，再用该实际变量生成代码。不要给普通 CUA `Tab` 猜补属性，也不要据此认定 DOM 能力消失、换 runtime 或重建页面。当前文档未提供这些接口或同绑定不可用时，保留实际错误交主线程；标签核验必须在 metadata 链之前完成。

`prepare` 是本地纯代码生成入口。其已成功返回的 `.code` 因上下文压缩丢失时，可以用同一正式任务参数重新运行该条 `prepare` 并读取完整代码；这不执行浏览器动作，也不属于重放未知采集。不得按记忆重构函数。若已结束回合、补读浏览器文档或检查标签库存，则先完成本人标签恢复，再生成一整组新的 metadata 与实际所需分段，不混用旧观察。

metadata 的真实 `episodeDomCount=0` 且页面明确显示“获取失败，请刷新页面”“暂无资源”时，不生成空分段、不调用详情 import，也不把错误提示当节目标题。保留原生页面与 metadata 回执及原任务 URL；按既有有界失败流程，在本批次正式 `complete-detail --logs-stopped --failed URL REASON` 中如实记录该页，其他未完成项仍先正常处理。失败项保持 `manual_review`，不计 captured；不放宽正数分集、标题或分段完整性门禁，不因单个无资源页反复重试或结束整路。

详情标题比较仅将 HTML 数字字符引用与空白、零宽空格归一化；例如实际 DOM 字面 `Let&#039;s` 可对应列表的 `Let's`。保存的节目标题解码数字字符引用，原始 DOM 证据保留；其他不同标题仍拒绝，不能人为改队列标题来通过核验。

默认复用已迁入正式代码的 `capture-with-recovery.mjs`：仅原有精确标题异常、同一 URL、零资源、无访问限制时，先持久化诊断，再普通导航恢复一次；正常成功页不增加 DOM 调用，其他失败停止 lane 并保留成功日志。提供自定义 `readDom/navigate/selectors` 时自动禁用该默认检查器，返回 `recoveryMode=custom-browser-no-retry`，需按相应浏览器文档处理；`recoverTransient:false` 可显式关闭。页面变化、权限/验证码或用户操作冲突由模型处理。不要将输出计数改成成功、不无限重试。模块不会替模型宣告页面加载完成或权限已解除。

收批遇到 Windows 原子替换错误时，先读正式 `current` 确认是否已 ack；只允许原 owner 从既有日志重入，不重采。入口仅对 WinError 5/32/33 在原锁内重试同一临时文件，最多约0.5秒；持续失败保留旧状态，不删他人锁、不改权限。重入仍核验每張封面，已绑定 verification 的首轮封面回执保持原哈希；旧版本把 `generated` 改为 `existing` 时，只能由程序在新核验成功且原 SHA-256 精确匹配后恢复原字节并另存恢复证据。图片、节目身份、资源数或审计真正变化仍拒绝 ack，不手改 verification。

明确停止本目录日志写入后：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py complete-detail --thread OWNER --batch BATCH --logs-stopped
```

程序从本批 journal 自动生成 results，执行 ready、增量同步、完整详情/raw 的逐页审计；随后对本批每个 captured 节目同步执行封面门禁并把逐项真实结果写入批次回执。原批次级 verification 和 ACK 不得将无封面项伪装为整批完整成功；无封面跳过适用下述规则，须由正式入口保留逐项状态。已有封面也须验证；raw/failed/skipped 不要求封面，raw 仍进入 `manual_review`，不计完整节目。未取得观察的页面必须按实际原因追加 `--failed URL "原因"` 或 `--skipped URL "原因"`（可重复）；不可将未尝试页静默遗漏，也不能借这两个页面参数冒充封面跳过。缺证据、半行、冲突或审计失败仍拒绝 ACK，保留 `ready/cover_pending` 断点。旧全 captured 流程 `complete-captured` 受同一门禁；可恢复阶段不用手写 JSON，历史 ACK 仍须纳入封面补验，不能据 ACK 跳过。

这里的收批“同步”仅指写入本机Docker PostgreSQL；`complete-detail`、`complete-captured`、`finish` 和 `ack` 不连接生产。正式提交事件复用已授权的[事件增量同步](all-catalogue-sync.md)，不等待300节目。采集线程不得另起发布器或逐批催促；用户暂停、仅改规范或明确只采本地时不发布，既有账本保留。

封面步骤沿 `agent → 独立中央 worker → 本地 relay → 已核验媒体域名`，媒体出网默认 `DIRECT`，浏览器及系统 TUN 保持开启；按[中央封面路由规范](cover-recovery.md)先核对实际直连，已有授权流程不重复确认。输入限定为本批已核验入库资源，经本地 relay 有界解码，中央鉴权、公网 IP 逐跳校验、全局锁、质量去重及 ACK 门禁保持。不得异步 enqueue 后先行 ACK，不得把文件存在当作验证，也不得用浏览器播放取链、猜 URL、重复图、坏图或文字卡补位。

实际直连已确认且原有限候选、窗口与质量门禁确实耗尽，单节目仍无封面时可保留资源、原证据和 `cover_pending` / `unavailable`，跳过该节目继续其他节目；不伪造封面成功或完整 captured 完成，网站仍不展示。全项目 `01`–`04` 按中央封面实际处理完成顺序合并统计不同节目连续失败，第 1–6 个可跳，第 7 个强制全局停写、停工并保留进度；真实合格封面成功才清零，raw、同节目重试、换 batch / owner 或重启不计新节目、不重置。系统、鉴权、数据库及结果未知的工具故障沿原恢复流程处理。正式工具暂不支持时，由主线程先适配既有收批入口，保留逐 item 真实状态，不手改 ACK 或绕过原批次门禁，不编造 `--skip-cover`；规范维护不解除已有暂停。完整规则见[单节目无封面与连续失败停工](cover-recovery.md#单节目无封面与连续失败停工)。

中央 writer 的开头三个取帧窗口均未选中时，仅对前两条确有解码候选的已核验资源探测本地 relay 的真实片长；片长足够才在约 55% 处按整分钟对齐再取一个有界窗口。质量判定允许全帧基本可用、中央主体细节强的深色舞台画面；竖版容器若有明确的上下深色边框和中间横向实景，仅裁出真实画面后重新验质、去重，拒绝文字卡、黑屏和劣质画面。`quality_rejected` 且资源与规则均未变化时不重复抓相同窗口；确认新资源或门禁实现改进后才针对原批次重入，并保留旧审计。

正式入口核验本批逐项封面结果及 verification / ACK、确认可接续，且未触发连续第 7 个无封面停工时，默认继续 `claim-detail`；已批准跳过的无封面项保持真实未完成状态。采集规范不含分类、类型补核或系列归组步骤，不生成或写入新分类字段。既有节目、资源、分类、系列及关联和采集证据保持原样，不归档、删表、回退 ACK 或堵下一批。禁止模型重新实现调度、拼接媒体地址或代写核验声明。

上下文压缩后 `current=idle/active_batch=null` 时，先核对原批次的正式 ACK、verification 和已有原生收尾回执。确认本人原批已成功收尾，就继续领批；不要因恢复指令中的旧 `ready` 与新 `idle` 不同而误报冲突、重复收尾或结束整路。原批确未 ACK、owner 不符或回执有实际矛盾时才保留断点交主线程核验，不能从压缩摘要猜批号。

若 `current` 已是本人新领取的批次，同样核对旧批 ACK 与本人后续 `claim-detail` 的真实成功回执。旧批已收尾、后续认领归属本人且 work 一致，就按当前正式批次的 remaining 继续；恢复消息中的旧批号只是已完成的断点，不是永久限定。不得因正常续领后的批号变化误报冲突或再次收尾旧批。

来源失败或已耗尽窗口反复拒绝时，由主线程按 [中央封面故障修复](cover-recovery.md) 核对真正尝试过的线路及新真实帧。最多八条候选按已核验实际线路轮流选取，避免前面分集占满预算；定向采样或真实像素裁剪仍由中央 writer 执行原质量和全库去重门禁。采集线程不自行安装图像，修复后沿用原批次收尾。

收批入口拒绝把通用验证码倒计时/更新提示作为失败或跳过原因。失败应来自正式采集器的实际 DOM/导航错误，不能用 AX 链接不可见代替属性检查。此前因此误判的已 ack 项保留历史证据，逐页取得实际属性可读的修复证据后，通过 `coordctl.py retry` 恢复，不能手改共享状态。

浏览器返回 `No tab with id` 或标签不属于当前浏览器会话时，收批入口拒绝将它作为节目失败/跳过原因。先确认原工具已终态，再在同一浏览器 binding 内获取或创建自己的任务标签，从 `claim-detail` 返回的未观察项续采；不重新选择浏览器、不重放结果未知的动作、不占用用户或其他线程标签。空标签清单不证明浏览器失联，已落盘页面不重新导航。
