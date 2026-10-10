# 小小漫迷项目协作入口

先读唯一主技能 [skills/xxmanmi-video-downloader/SKILL.md](skills/xxmanmi-video-downloader/SKILL.md)，按当前任务选择参考；同任务未变内容复用。根目录不保留技能副本或兼容入口。

## 授权边界

默认只在本地修改和验证，不直接修改生产代码、上传部署、切换线上版本或重启生产服务。“优化”“修复”“继续”及以前任务的发布授权不构成本次生产授权。项目发布须完成本地验收后取得当次明确指令；已独立授权的节目数据同步按限定流程执行，不能顺带发布应用。

查看状态、修改规范、整理或打包不启动或恢复采集、不写生产。用户当前明确清理授权只覆盖本次范围，旧取消的删除清单不自动重试；不安装定期清理、监督服务、计划任务或登录自启动。

## 正式状态与数据

业务主线程及 01–04 四个 `luna/medium` 槽位以正式 JSON 中的真实 UUID、owner、work 为准，工作路径不得猜测或抄终端折行。唯一共享协调状态是 `Datebase/xxmanmi-resource-db/.runtime/collection-coordination/state.json`。

数据库、未完成批次、claim、原工具句柄、证据、封面和生产冻结包是恢复依据，不能按文件年龄、未跟踪状态或“旧版本”删除。未知动作不重放，不假造 captured、审计、封面或 ACK。历史分类、系列归档及生产用户数据保留；缺少已退役活跃系列表不得自动重建。

## 按任务执行

- 采集：[采集原则](references/collection-policy.md)、[采集短入口](references/low-token-workflow.md)。主线程只恢复与最小监督，不代采、供词、查榜、分类或系列归组。
- 监督与停工：[主线程流程](references/main-supervision.md)、[恢复模板](references/recovery-templates.md)。采集运行、修复和停工仅显示 `working`；用户主动提问正常回答。任一槽位真实连续故障超过 3600 秒，关闭写闸并精确停止登记执行，保留进度；确认死亡才清理并同槽补位。
- 主线程修复既有采集故障：沿用已确认的最小修复授权，修复后原线程续跑，不运行项目回归测试；必要语法、参数和正式收批门禁保留。另行授权的开发、整理和封装任务按本次范围验证。
- 节目同步：[统一对齐](references/all-catalogue-sync.md)、[同步技能](skills/manying-production-sync/SKILL.md)。全部管理记录接纳，普通提交事件增量同步；独立生产验收后才算成功，生产账号与用户行为不被本地覆盖。
- 网站开发：`web/README.md`（私有网站环境提供）；上线用[项目发布技能](skills/manying-project-deploy/SKILL.md)。本地网页与中央封面服务统一使用 `web/manage.py` 管理，变更服务前检查在途封面任务。

## Windows 与产物

本机后台执行 `exec_command` 使用 `tty:true`；辅助子进程从创建时使用 `CREATE_NO_WINDOW` / `SW_HIDE` 或 `windowsHide:true`，`Start-Process` 必须带 `-WindowStyle Hidden`。不弹出辅助窗口，不以最小化代替隐藏，保护用户主动打开的应用与交互终端。

只生成当前任务必需的正式产物；不复制秘密、整段聊天、运行缓存或重复诊断材料进源码和交付包。源码包使用 `scripts/projectctl.py package`，运行数据迁移单独处理。

## GitHub 开源范围

公开仓库只发布 `skills/`、`references/` 和必要的 `scripts/` 工具。`Datebase/` 整个目录（含结构、配置、数据和运行工具）、网站业务代码、运行状态、素材、媒体、凭据及本机配置保持私有。发布前核对完整 Git 历史与待提交文件，不用整项目源码包作为公开上传包。
