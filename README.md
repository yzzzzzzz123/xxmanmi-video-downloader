# 小小漫迷 / 漫映项目

项目包含公开页面资源采集、PostgreSQL 原始资源库、漫映在线片库、视频下载和受控生产同步。采集从浏览器实际可见的 DOM 读取详情与媒体地址；下载器只处理已经观察到的未加密点播资源。

唯一技能入口：[skills/xxmanmi-video-downloader/SKILL.md](skills/xxmanmi-video-downloader/SKILL.md)。阅读技能、整理项目和运行检查不会启动采集或发布生产。

## 目录与职责

| 目录 | 内容 |
| --- | --- |
| [skills/](skills/) | 主技能，以及清理、节目同步、应用发布、公众号发布技能 |
| [references/](references/) | 按任务读取的采集、断点恢复、下载和同步细则 |
| [scripts/collection/](scripts/collection/) | 共享认领、公开 DOM 采集辅助、恢复、正式收批与同步 CLI |
| [scripts/catalogue/](scripts/catalogue/) | 已授权的历史系列退役工具及验证；不在启动时执行 |
| [scripts/download.py](scripts/download.py) | 已观察媒体队列的 HLS 下载器，Linux/macOS/WSL 使用 |
| [Datebase/xxmanmi-resource-db/](Datebase/xxmanmi-resource-db/README.md) | 数据库结构、Docker 配置与入库工具；沿用既有目录名 |
| [web/](web/README.md) | 网站、账号、播放器、封面工具、静态资源和部署资料 |
| [scripts/projectctl.py](scripts/projectctl.py) | 本地源码检查与完整源码包导出 |

源码、技能和必要网页资产在本项目内；`.runtime/`、`claims/`、数据库卷、封面缓存、虚拟环境及秘密属于运行环境。整理源码不删除这些数据。迁机时须另行迁移数据库与必要断点，并在目标机配置凭据。

## 本地安装

Python 3.11+；数据库需要 Docker 与 Compose，封面和下载需要 FFmpeg。浏览器操作使用当前环境已授权的浏览器工具，不能将本项目当作独立 HTTP 爬虫。

在项目根目录执行：

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r scripts/collection/requirements.txt
.\.venv\Scripts\python.exe -m pip install -r web/requirements.txt
.\.venv\Scripts\python.exe Datebase/xxmanmi-resource-db/dbctl.py status
.\.venv\Scripts\python.exe web/manage.py status
```

Linux/macOS 使用 `.venv/bin/python`。运行依赖只维护 [web/requirements.txt](web/requirements.txt)；Playwright collection uses [scripts/collection/requirements.txt](scripts/collection/requirements.txt) and installed Google Chrome，Windows 与 Linux 的 Pillow 版本按平台选择。数据库初装与导入见[数据库说明](Datebase/xxmanmi-resource-db/README.md)，不要在已有环境重建数据卷或重跑生产初装。

网站配置参考 [web/config.example.json](web/config.example.json)，复制为 `web/config.local.json` 后填写本机路径；该本机文件不提交、不打包。也可使用 `MANYING_RUNTIME`、`MANYING_DB_ROOT` 等环境变量。已有配置继续使用，不能以示例覆盖。

## 使用入口

| 任务 | 入口 |
| --- | --- |
| 开始或继续采集 | 让 Agent 读取[主技能](skills/xxmanmi-video-downloader/SKILL.md)，按用户明确范围续原断点 |
| 查询本地资源库 | `python Datebase/xxmanmi-resource-db/dbctl.py status` |
| 查询网站状态 | `python web/manage.py status` |
| 启动本地网站 | `python web/manage.py start`，后台隐藏启动；本机访问 `http://127.0.0.1:8088` |
| 查询事件同步状态 | `python scripts/collection/production_live.py status` |
| 下载已观察媒体 | 按[下载与交付](references/download-and-delivery.md)和[队列约定](references/queue-contract.md)执行；Windows 使用 WSL |
| 同步节目数据 | [manying-production-sync](skills/manying-production-sync/SKILL.md)，保护生产用户数据 |
| 发布网站代码 | [manying-project-deploy](skills/manying-project-deploy/SKILL.md)，需要本次明确生产授权 |

Current execution: one agent directly owns isolated headless Playwright, resource audits, covers, ACK and verified catalogue sync. No collector subagents or CUA. See [the workflow](references/low-token-workflow.md).

生产节目同步按提交事件处理，不等待 300 个节目。旧冻结轮工具只用于原历史包的恢复；数据同步与应用发布使用各自技能，查询状态和整理项目都不触发生产写入。

## 检查与源码打包

下列命令只读取源码，不连接数据库、不启动浏览器、采集者或网站，也不访问生产：

```powershell
python -B scripts/projectctl.py check
python -B scripts/projectctl.py package
```

默认生成 `dist/xxmanmi-video-downloader.zip`，包含项目源码、技能、参考、静态资源、依赖清单和部署资料，包内附逐文件 SHA-256 清单。排除凭据、本机配置、运行状态、认领记录、数据库导出、视频、缓存及开发环境；不会因 Git 中尚未跟踪而漏掉现有正式源码。已有同名包不覆盖，可用 `--output` 指定新路径。

现有验证入口：

```powershell
python -B -m unittest discover -s scripts/collection -p test_shared_discovery.py -q
python -B -m unittest discover -s skills/project-cleanup/scripts -p test_cleanupctl.py -q
python -B -m unittest discover -s web -p test_ai_search_fallback.py -q
npm --prefix web ci --ignore-scripts
npm --prefix web run test:ui
node --test scripts/collection/test-capture-batch.mjs scripts/collection/test-capture-with-recovery.mjs scripts/collection/test-detail-capture.mjs scripts/collection/test-search-lifecycle.mjs
```

Python 验证使用已安装项目依赖的环境。DOM 测试依赖 `jsdom`、`css-tree`，仅用于开发；数据库集成测试使用独立测试 schema，不能指向生产。下载器测试须在 Linux/macOS/WSL 运行。

只处理公开、正常可访问的资源，不绕过登录、验证码、付费权限或 DRM。网页是否展示节目与管理库是否保留节目是两套口径：管理库保留真实原始记录，观看端只展示通过资源与封面门禁的节目。
