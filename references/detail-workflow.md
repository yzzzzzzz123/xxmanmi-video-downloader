## 详情：一次领批、一次采集、一次收批

当前 Node REPL 每次浏览器 DOM/导航调用显式设置 `timeout_ms=120000`，详情单页段与正式搜索模块调用设置 `timeout_ms=240000`，避免默认30秒或60秒超时重置 kernel。外层 `functions.exec` 提前 yield 后只等待原 cell；不把外层 yield 当成调用失败、不重放未知动作。kernel 确已终态时按当前 Chrome Skill 重新 bootstrap 同一浏览器 binding，从正式 claim 与落盘证据恢复，不能用宿主异常判节目失败。

```powershell
python -X utf8 -B scripts/collection/workflowctl.py claim-detail --thread OWNER --limit 4
```

可重入：优先恢复自己的 active batch；没有才原子领取。自动生成任务书、固定日志证据边界与 `capture_input` JSON，只返回未采条目和 `next_action`。在本线程安全检查点执行，不与同 owner 的采集同时运行；已落盘项不会重新导航。`remaining=0` 按 `next_action` 收批，`status=empty` 先检查真实pending，再按[公开列表流程](cua-workflow.md#列表发现)从本人已观察列表、来源和真实下一页补充详情URL，经唯一共享队列去重后领批。无法继续时保留断点并内部上报真实阻塞，不创建关键词、榜单发现或分类任务。空队列不代表目标完成，不循环空领。

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

媒体格式判定仅在解析临时值时忽略URL末尾的ASCII空格，与浏览器`new URL`的既有格式解析一致；原journal、`dataWeb`、媒体地址原串及其SHA/去重身份不改，DOM完整数量和独立审计门禁保留。含空白的原始地址仍须满足既有网站健康谓词才可展示，格式识别通过不代表每条资源可播放。

详情标题比较仅将 HTML 数字字符引用与空白、零宽空格归一化；例如实际 DOM 字面 `Let&#039;s` 可对应列表的 `Let's`。保存的节目标题解码数字字符引用，原始 DOM 证据保留；其他不同标题仍拒绝，不能人为改队列标题来通过核验。

默认复用已迁入正式代码的 `capture-with-recovery.mjs`：仅原有精确标题异常、同一 URL、零资源、无访问限制时，先持久化诊断，再普通导航恢复一次；正常成功页不增加 DOM 调用，其他失败停止 lane 并保留成功日志。提供自定义 `readDom/navigate/selectors` 时自动禁用该默认检查器，返回 `recoveryMode=custom-browser-no-retry`，需按相应浏览器文档处理；`recoverTransient:false` 可显式关闭。页面变化、权限/验证码或用户操作冲突由模型处理。不要将输出计数改成成功、不无限重试。模块不会替模型宣告页面加载完成或权限已解除。

收批遇到 Windows 原子替换错误时，先读正式 `current` 确认是否已 ack；只允许原 owner 从既有日志重入，不重采。入口仅对 WinError 5/32/33 在原锁内重试同一临时文件，最多约0.5秒；持续失败保留旧状态，不删他人锁、不改权限。重入仍核验每張封面，已绑定 verification 的首轮封面回执保持原哈希；旧版本把 `generated` 改为 `existing` 时，只能由程序在新核验成功且原 SHA-256 精确匹配后恢复原字节并另存恢复证据。图片、节目身份、资源数或审计真正变化仍拒绝 ack，不手改 verification。

明确停止本目录日志写入后：

```powershell
python -X utf8 -B scripts/collection/workflowctl.py complete-detail --thread OWNER --batch BATCH --logs-stopped
```

程序从本批 journal 自动生成 results，执行 ready、增量同步、完整详情/raw 的逐页审计；随后对本批每个 captured 节目同步执行封面门禁并把逐项结果写入批次回执，全部通过后才生成 verification 和 ack。已有封面也须验证；raw/failed/skipped 不要求封面，raw 仍进入 `manual_review`，不计完整节目。未取得观察的页面必须按实际原因追加 `--failed URL "原因"` 或 `--skipped URL "原因"`（可重复）；不可将未尝试页静默遗漏。缺证据、半行、冲突、审计或封面失败时拒绝 ack，保留 `ready/cover_pending` 断点。旧全 captured 流程 `complete-captured` 受同一门禁；可恢复阶段不用手写 JSON，历史 ack 仍须纳入封面补验，不能据 ack 跳过。

这里的收批“同步”仅指写入本机Docker PostgreSQL；`complete-detail`、`complete-captured`、`finish` 和 `ack` 不连接生产。正式提交事件复用已授权的[事件增量同步](all-catalogue-sync.md)，不等待300节目。采集线程不得另起发布器或逐批催促；用户暂停、仅改规范或明确只采本地时不发布，既有账本保留。

封面步骤只通过独立中央 worker 和全局锁处理，输入限定为本批已核验入库资源，经本地 relay 有界解码。不得异步 enqueue 后先行 ack，不得把文件存在当作验证，也不得用浏览器播放取链、猜 URL、重复图、坏图或文字卡补位。无合格真实帧时停止收批与下一次 claim，修复后从封面断点幂等重入。

中央 writer 的开头三个取帧窗口均未选中时，仅对前两条确有解码候选的已核验资源探测本地 relay 的真实片长；片长足够才在约 55% 处按整分钟对齐再取一个有界窗口。质量判定允许全帧基本可用、中央主体细节强的深色舞台画面；竖版容器若有明确的上下深色边框和中间横向实景，仅裁出真实画面后重新验质、去重，拒绝文字卡、黑屏和劣质画面。`quality_rejected` 且资源与规则均未变化时不重复抓相同窗口；确认新资源或门禁实现改进后才针对原批次重入，并保留旧审计。

批次完成封面门禁并 ack 后，默认继续 `claim-detail`。采集规范不含分类、类型补核或系列归组步骤，不生成或写入新分类字段。既有节目、资源、分类、系列及关联和采集证据保持原样，不归档、删表、回退ack或堵下一批。禁止模型重新实现调度、拼接媒体地址或代写核验声明。

来源失败或已耗尽窗口反复拒绝时，由主线程按 [中央封面故障修复](cover-recovery.md) 核对真正尝试过的线路及新真实帧。最多八条候选按已核验实际线路轮流选取，避免前面分集占满预算；定向采样或真实像素裁剪仍由中央 writer 执行原质量和全库去重门禁。采集线程不自行安装图像，修复后沿用原批次收尾。

收批入口拒绝把通用验证码倒计时/更新提示作为失败或跳过原因。失败应来自正式采集器的实际 DOM/导航错误，不能用 AX 链接不可见代替属性检查。此前因此误判的已 ack 项保留历史证据，逐页取得实际属性可读的修复证据后，通过 `coordctl.py retry` 恢复，不能手改共享状态。

浏览器返回 `No tab with id` 或标签不属于当前浏览器会话时，收批入口拒绝将它作为节目失败/跳过原因。先确认原工具已终态，再在同一浏览器 binding 内获取或创建自己的任务标签，从 `claim-detail` 返回的未观察项续采；不重新选择浏览器、不重放结果未知的动作、不占用用户或其他线程标签。空标签清单不证明浏览器失联，已落盘页面不重新导航。
