---
name: xxmanmi-video-downloader
description: 小小漫迷项目入口，单 agent 通过 Playwright 稳定更新公开资源，直接负责资源审计、封面、ACK 和受控节目同步；也用于资源库、下载与项目维护。只读或仅改规范不启动采集。
---

# 小小漫迷 / 漫映项目

从项目根使用本技能，先读 [AGENTS.md](../../AGENTS.md)。技能目录的上两级是 PROJECT_ROOT，安装与结构见 [README.md](../../README.md)。本文件为唯一主技能。

## 当前执行架构

2026-10-14最新用户决定：任务完全迁到服务器，之后只维护生产库，本地采集与数据库停止更新。以[服务器采集](../../references/server-collection.md)为当前部署与存储方式；保留下面的公开DOM、审计、独立封面、ACK与正式停工门禁。Windows和本地SSH增量同步描述仅用于历史恢复，不可据此启动本地任务。

2026-10-15用户长期授权：采集AI能自行完成的采集代码、封面及运行环境修复，直接备份、修改、测试、部署、重启采集专属服务并独立复验后继续原任务，不逐次请求修复权限。服务账户的写权限及Polkit操作见[服务器采集](../../references/server-collection.md)。无关网站发布和用户数据变更仍按原边界执行；原验收和故障停工门禁保留。

最新用户口径：只有取得真实受支持 HTTPS 媒体、完整审计与独立封面验收通过的才是节目。未解析编号、空资源及失败页直接跳过，不写节目库、不同步生产，也不保存这类 DOM 或 raw 采集证据；只保留必要的 URL 去重和任务状态。此要求优先于旧参考中将 raw/失败/空记录入库或全部同步的规则。进度以新增可用节目数为主。

连续采集不设置固定检查间隔：队列有条目就顺序继续，空队列立即检查下一公开来源；每次有界检查使用最多 8 个真实来源视图。只有页面尚未加载、锁被占用或真实故障才作必要的有界等待。

同一真实观察队列优先处理尚未取得可用资源的 URL，再处理已有可用节目的更新；保留在途 claim，并保持同类条目的原顺序。

选片优先满足用户点名的有限范围，再选经典、曾经大火或近期热门作品，减少冷门填充。用户指定片名允许通过真实可见的站内搜索查找，普通发现的“仅使用列表已观察标题”不限制明确指定任务；每次仍使用有界真实来源视图，不建立长期关键词池。已有健康完整版本先检查缺集和更新，同名多来源不当成不同作品，解说、预告、幕后和相似蹭名内容不替代正片。完成指定范围后继续原常规采集，原队列和在途claim保留。

一个当前 agent、一个本线程原生 `/goal`，顺序直接负责 **Playwright 采集 → 入库审计 → 独立封面 → ACK 验收 → 受控生产增量同步及独立验收**。不创建或调度采集子线程，不使用 CUA 采集，不另设监督层。默认每批一个节目，以每天稳定发现新节目和更新资源为目标；旧四路和 10000 数量目标不再决定执行方式。

用户要求开启或继续时，复用当前线程原生 Goal 并持续执行；静默只约束消息。单批结束、空队列、轮次结束或回复 working 都不能结束 Goal。既有 Goal 改为当前用户明确的单 agent 目标时保留创建时间与真实用量，不将旧目标伪报完成。

用户要求当前会话接管时，当前 agent 直接承担业务执行。原线程在安全点结束在途工具并暂停其 Goal 后，用正式 `recoveryctl.py adopt-main --authorized --live-handoff --thread OWNER --request-id 实际用户消息ID` 转交唯一主身份、采集身份和来源断点；原数据、验收、历史和故障首次时间保留。随后由当前线程的原生 Goal 直接续跑采集、验收与同步。

执行只读[短流程](../../references/low-token-workflow.md)、[生命周期](../../references/main-supervision.md)和[采集原则](../../references/collection-policy.md)。正常入口 `python -X utf8 -B scripts/collection/single_agentctl.py run --thread OWNER --max-views 2` 返回一个有界步骤的短结果；当前 agent 根据回执继续。OWNER/work/claim 解析正式状态，不手写路径或验收 JSON。

从旧四路迁移必须先正式停止旧执行和核验专属进程退出，迁移入口保持写闸关闭、保存原 owner/任务书/日志/证据。旧 ready 批次由当前 agent 按正式交接重新审计、检查封面并以自己身份 ACK；未完成 claim 依据明确交接保留历史并转交已观察 URL。旧会话不删除，旧采集者不再写入。具体入口见生命周期参考。

## 共同门禁

- 独立隐藏 Playwright 只读实际公开页面 DOM，不接管用户 Chrome，不 HTTP 抓站点、不播放、不猜 URL、不推断 opaque 资源、不绕访问限制。来源、线路、分集和媒体格式是资源身份，完整计数、连续分段和稳定指纹必须通过。
- 正式任务经过唯一共享协调状态的独占 claim、数据库锁、资源审计、独立封面和真实 ACK。raw、失败、封面待补与生产排队如实保存，不能算完整成功。已验收页面的真实更新须保留旧验收及当前 DOM 证据再入队，不覆盖在途 claim。
- 不创建关键词池、榜单供给、分类、标签或系列归组任务；现有节目、资源、分类、系列关联、用户数据、原任务与证据保留。
- TUN 保持开启；xxmanmi.com/www.xxmanmi.com 和实际核验媒体域名用精确 DOMAIN/DIRECT，真实连接单独核验，不能扩大为后缀或全局直连。封面细则见[封面流程](../../references/cover-recovery.md)和[网络路由](../../references/xxmanmi-network-routing.md)。
- 连续前 1–6 个不同节目无法获得合格封面，可保留资源和待补状态跳过；第 7 个立即正式停工。只有真实合格封面成功才清零。同节目重试、换批和重启不重置。
- 解码故障先按[封面故障流程](../../references/cover-recovery.md)有界修复并复验；修复后本节目仍失败，正式记录待补并跳过，继续后续队列。有其他可执行条目时，不能因反复查看同一解码错误就把整个Goal设为blocked；连续第7个失败与7200秒真实故障门禁仍执行。
- 任一真实连续故障超过 7200 秒仍未恢复，先关闭写闸，再暂停 Goal、停止登记执行并精确核验采集专属进程退出。首次故障时间跨重试/回合保留；idle、正常锁等待与空队列不是死亡。用户暂停或停止优先，不自动复工。
- 实际提交事件通过原单一隐藏有界 CLI 增量同步生产；queued/pending 不代表成功，必须有独立生产验收。保护生产账号、会员、会话、收藏、观看与活动，不部署应用代码。授权范围见[统一同步](../../references/all-catalogue-sync.md)。
- 持续执行期间每 10 分钟简要汇报一次实际进度，包括已验收完成量、当前步骤及实际阻塞；其余时间保持 working，用户主动提问正常回答。进度汇报统一以本条为准。只读、改规范、整理或打包不解除暂停或写生产；用户同时明确开启/继续时按原生 Goal 持续授权执行。每次等待不超过 60 秒；不安装监督服务、计划任务或登录启动项。
- Windows 后台命令使用 tty:true；辅助进程创建时隐藏，不弹窗、不以最小化代替隐藏，保护用户应用和共享服务。

## 按需入口

| 任务 | 参考 |
| --- | --- |
| 单 agent 执行和续跑 | [短流程](../../references/low-token-workflow.md)、[生命周期](../../references/main-supervision.md) |
| 封面和路由 | [封面](../../references/cover-recovery.md)、[路由](../../references/xxmanmi-network-routing.md) |
| 历史 CUA/搜索证据核验 | [原详情参考](../../references/detail-workflow.md)、[原搜索参考](../../references/search-workflow.md)；仅用于历史断点，不启用采集子线程 |
| 清理已停止执行 | [project-cleanup](../project-cleanup/SKILL.md)；不按文件年龄或 Git 未跟踪状态删除数据 |
| 节目生产同步 | [manying-production-sync](../manying-production-sync/SKILL.md) |
| 网站开发/部署 | [网站说明](../../web/README.md)、[应用部署](../manying-project-deploy/SKILL.md)；部署需要本次明确授权 |
| 资源库、下载 | [数据库](../../Datebase/xxmanmi-resource-db/README.md)、[下载交付](../../references/download-and-delivery.md)、[队列](../../references/queue-contract.md) |
| 公众号 | [manying-wechat-publisher](../manying-wechat-publisher/SKILL.md) |
| 源码检查、打包 | python -B scripts/projectctl.py check / package |

本地网站及中央封面服务通过 web/manage.py 管理，重启前核对在途封面。源码整理不删除运行数据；下载工作目录与最终片库分离，Windows 下载使用 WSL。
