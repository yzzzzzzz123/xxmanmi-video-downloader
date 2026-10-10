# 搜索参数完整性

在首次搜索、宿主重建或发现查询词差异时读取；正常任务复用该流程，不向主线程逐个候选请求普通调度。

正式 search taskbook 的 schema 是 `scope.queries` 与 `scope.channels` 两个长度为1的数组。不存在 `scope.query`。从原本地 JSON 程序读取 `book.scope.queries[0]`、`book.scope.channels[0]`、`book.task_id`、`book.owner`；核对正式解析后的有效 claim、文件 SHA、本人 work 和当前暂停门禁。接管过的 claim 使用现有正式 effective-claim 解析，不以原 claim 文件的旧 owner 冒充当前身份。

同一个读取出来的 query 变量用于浏览器 fill 和 `prepareSearchAction` 的 task.query，同一个 channel 变量用于渠道与 task.channel。读取项目任务书可用批准的 Node 环境本地文件接口；此许可不涉及浏览器 profile、cookie、storage 或隐藏页面数据。禁止从 PTY 屏幕换行、模型摘要、聊天或人工转写构造参数；终端显示不能替代原 JSON 字节。内部空格、标点和字形不得自行增删、trim或替换。

任一真实 JSON 参数不一致时，在任何输入、导航、搜索动作前保存精确原字符串、codepoints、任务书/有效 claim SHA 与原阶段，使用匹配的未开始恢复入口。已经执行过 UI 的任务保留原模块/actionId、实际 DOM、日志和原证据，不能套用“动作未开始”，不能改原任务书或证据来通过 commit。根据真实已返回点击、原 tab 是否可读及原句柄是否可用选择既有正式隔离；只有原模块实际释放或宿主正式撤销旧执行实例后才宣称对应能力已撤销。缺匹配入口就保留断点。

已正式隔离且主线程核实原prepare任务词本身错位、工具全部终态时，仅可按 [故障恢复](search-fault-recovery.md) 的`resume-misbound-search`窄分支保留原cap休眠并接其他任务；不得把休眠写成释放，不重放或重置原实例。

工作 tab 跨 turn 仍要使用时，按当前 Chrome 文档的正式 markHandoff 入口保留本人创建的标签，并记录实际结果；只对本人标签执行。原 tab 真实 missing 而 binding 仍有效、没有未知或待决动作时，可在原槽创建唯一自有替代标签，不必重新绑定相同 owner/work。存在原搜索句柄或未知动作时先处理它，禁止为满足 missing-tab 门禁主动关闭原标签。暂停、用户停止和其他槽/用户标签保护始终优先。
