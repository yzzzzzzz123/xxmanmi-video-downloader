# 小小漫迷项目协作入口

2026-10-14最新用户要求：采集任务迁到新加坡服务器，今后只更新生产库，本地库停止维护。原本地Goal和写闸已暂停；本机禁止恢复采集或本地同步。服务器迁移、唯一生产库及原生Goal执行见[服务器采集](references/server-collection.md)，该要求优先于下文旧本地执行描述。服务器使用专用systemd承载一个Codex app-server与原生Goal，不安装本地后台采集或额外监督agent。

读取唯一主技能 [skills/xxmanmi-video-downloader/SKILL.md](skills/xxmanmi-video-downloader/SKILL.md)，同任务未变内容复用。

## 当前单 agent 架构

选片优先用户点名的明确范围，其次经典、曾经大火或近期热门作品，减少冷门填充。指定片单可用真实可见站内搜索完成，不建立长期关键词池或分类任务。已有健康完整版本优先核对缺集与更新；同名多来源、解说、预告和幕后不冒充新增作品。

当前唯一 agent 通过独立隐藏 Playwright 顺序负责采集、资源入库审计、独立封面、ACK 和受控生产增量验收。默认每批一个节目，优先每天稳定更新新资源；不创建或调度采集子线程，不使用 CUA 采集。规范见[短流程](references/low-token-workflow.md)、[生命周期](references/main-supervision.md)。

用户明确开启/继续任务时复用本线程原生 Goal 并持续执行，单批/空队列不退出；正常、修复和停工仅显示 working，用户主动提问正常回答。只读、改规范、整理和打包不启动采集或写生产。不安装后台监督服务、计划任务或登录自启动。

## 授权和正式状态

默认本地修改验证；生产应用代码、部署、切换与重启需要本次明确授权。2026-10-15用户已长期授权服务器采集AI自行修复采集代码、封面处理及其运行环境，完成备份、测试、部署和独立复验，可重启采集专属服务并继续原任务，不再逐次询问权限。该授权限于采集修复；其他网站功能发布仍按原授权要求执行。具体权限与恢复见[服务器采集](references/server-collection.md)。已独立授权的节目数据同步仅按[统一同步](references/all-catalogue-sync.md)执行，保护生产账号与行为数据。网站及中央封面服务通过 web/manage.py 管理，重启前核对在途任务。

唯一共享状态为 Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json；正式主状态的 main owner/work 与当前 CODEX_THREAD_ID 一致。旧四路 owner、claim、任务书、证据和日志保留为历史。迁移先正式停止旧执行，当前 agent 依据交接独立验收旧 ready 批次，不伪造旧 owner ACK、不重放未知动作。

采集仅公开 DOM，不播放、不 HTTP 抓站点、不猜 URL 或 opaque ID、不绕访问限制。保持资源完整性、数据库锁、封面与 ACK 门禁；raw/失败/待补不能算完整成功。历史分类、系列关联与证据保留，不新建分类或系列任务。

浏览器 TUN 保持开启、站点和真实媒体域名精确 DIRECT。连续第 7 个不同节目封面失败或真实连续故障超过 7200 秒，关闭写闸、暂停 Goal，并精确核验采集程序及私有浏览器退出，保留断点；不自动复工、不按名称批杀共享服务。

## Windows 与产物

解码故障先有界修复和复验，仍失败则按正式封面待补跳过当前节目，继续后续队列；不能在还有可执行条目时仅因同一解码失败将整个Goal设为blocked。连续第7个失败及7200秒故障停工门禁保持。

exec_command 使用 tty:true；辅助子进程创建时用 CREATE_NO_WINDOW/SW_HIDE 或 windowsHide:true，Start-Process 必须带 -WindowStyle Hidden。不得弹窗或用最小化替代隐藏，保护用户主动打开的应用。

原数据库、claim、原工具句柄、证据、封面和生产冻结包保留；不按年龄或未跟踪状态删除。源码打包用 scripts/projectctl.py package，不复制凭据或运行状态。下载与交付见[下载参考](references/download-and-delivery.md)。
