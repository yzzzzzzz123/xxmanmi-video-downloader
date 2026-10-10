---
name: xxmanmi-video-downloader
description: 小小漫迷项目统一入口，处理四路公开 DOM 采集、资源库、漫映网站、本地下载和受控节目同步。用于运行、恢复、维护或整理本项目；只读和规范任务不启动采集、不写生产。
---

# 小小漫迷 / 漫映项目

本技能与项目源码一起使用。技能目录的上两级是 `PROJECT_ROOT`；以下命令均在项目根目录执行，先读 [AGENTS.md](../../AGENTS.md)。安装、结构和源码包见 [README.md](../../README.md)。本文件是唯一主技能，根目录不保留副本或兼容入口。

## 按任务读取

| 当前任务 | 参考或技能 |
| --- | --- |
| 业务主线程运行、续跑和最小监督 | [主线程流程](../../references/main-supervision.md) |
| 01–04 采集槽位 | [采集原则](../../references/collection-policy.md)、[短入口](../../references/low-token-workflow.md)；按需读[详情](../../references/detail-workflow.md)、[原搜索断点](../../references/search-workflow.md)或 [CUA](../../references/cua-workflow.md) |
| 故障恢复 | [固定恢复模板](../../references/recovery-templates.md)，仅展开正式 CLI 返回的对应细则 |
| 已确认死亡的线程清理、用户明确清理 | [project-cleanup](../project-cleanup/SKILL.md)；整理源码不等于退役采集 owner |
| 节目与资源同步 | [统一对齐](../../references/all-catalogue-sync.md)、[manying-production-sync](../manying-production-sync/SKILL.md) |
| 网站开发与账号、播放行为 | `web/README.md`（私有网站环境提供） |
| 网站代码、配置或结构上线 | [manying-project-deploy](../manying-project-deploy/SKILL.md) |
| 采集原始资源库的部署、入库、查询 | `Datebase/xxmanmi-resource-db/README.md`（私有数据库环境提供） |
| 实际下载与片库交付 | [下载与交付](../../references/download-and-delivery.md)、[队列格式](../../references/queue-contract.md) |
| 漫映公众号文章制作或发布 | [manying-wechat-publisher](../manying-wechat-publisher/SKILL.md) |
| 本地项目检查、源码打包 | `python -B scripts/projectctl.py check` / `package` |

只读当前角色与阶段所需内容。先用已有正式 CLI 生成任务书、认领、核验和持久状态；不手写程序能生成的回执，不逐批重读全历史或全部参考。

## 核心约定

- 一个业务主线程与 01–04 四个 `luna/medium` 采集槽位沿实际公开列表、来源与分页发现详情 URL，先续原 owner 断点。主线程只恢复与最小监督，不供关键词、不查榜、不代采或代收批。Chrome 分组如使用，精确叫 `01`、`02`、`03`、`04`。
- 采集不承担分类、补标签或系列归组，不写新分类字段。来源、线路、分集和媒体格式属于真实资源身份，继续保存；节目按真实详情 URL 去重。已保存分类、系列历史归档和关联保留，缺少退役表不重建。
- 只观察当前已打开公开页面的 DOM，不 HTTP 抓站点、不猜 URL、不绕登录、验证码、付费或 DRM。原工具动作未知时等待原句柄，不重放、不换 nonce；无法继续保存真实断点和故障。
- 正式批次必须经过独占认领、资源审计、独立封面和真实 ACK。raw、失败、封面待补或同步排队如实记录；不能改成完成。共享协调状态唯一来自 `Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json`，UUID 和 work 从正式状态解析。
- 授权持续采集期间，真实提交事件由单一隐藏有界 CLI 增量同步生产，不等 300 个节目、不正常调用模型、不安装监督服务或计划任务。全部节目管理记录接纳；网站只展示资源与封面门禁通过的节目。生产账号、会员、会话、收藏、观看和活动数据不从本地覆盖。
- 任一槽位真实连续故障超过 3600 秒仍未恢复，先关闭写闸，再暂停和中断登记执行并精确终止已核实归属的采集专属进程；核对真实终态、保留全部进度，不自动恢复。首次故障时间不因重试或换身份清零；idle、正常锁等待、空队列不是死亡。10000 是里程碑，用户明确暂停或停止始终优先。
- 采集执行、修复和停工期间仅显示 `working`，用户主动提问正常答复。必要恢复沿用既有最小修复授权；健康线程不轮换，确认死亡才调用清理技能同槽补位。只读、规范、清理和打包不解除暂停、不创建 Goal、不联系已暂停采集者或执行生产写入。
- 下载工作目录与最终片库分离。Windows 下载用 WSL；默认片库解析桌面同名快捷方式的实际目标，只交付系列/季目录及有效原视频。片库整理规则与容量、断点检查按下载参考执行。

## 边界与运行环境

网站本地生命周期与中央封面服务只用 `web/manage.py` 管理；重启前核对在途封面，保护共享浏览器、数据库、用户应用和其他项目。Windows 后台命令使用 `tty:true`，辅助进程创建时隐藏，不弹出控制台或以最小化代替隐藏。

站点两个精确域名的既有直连规则见[网络路由](../../references/xxmanmi-network-routing.md)，不能扩大为后缀或改动其他代理行为。原任务、claim、证据、封面、历史回执和冻结生产包保留；无用源码、开发环境与运行记录分别识别，不能通过 Git 未跟踪状态判定垃圾。

仅节目同步使用节目同步技能；应用发布需要当前任务明确授权。项目检查与打包只读取源码，不连接数据库或生产，不复制本机凭据与运行状态。
