# 小小漫迷 / 漫映 Agent Skills

面向 Codex 等 Agent 的公开页面 DOM 采集、视频下载、断点恢复、受控清理、节目同步与公众号图文工作流。

本仓库开源 Skill、参考文档与配套工具，使用 [MIT License](LICENSE)。数据库整个目录、数据库结构与配置、采集记录、网站业务代码、账号数据、运行状态、媒体和本机凭据由使用者私有保存。

## Skill 入口

唯一主入口为 [skills/xxmanmi-video-downloader/SKILL.md](skills/xxmanmi-video-downloader/SKILL.md)，根目录不维护副本。

| Skill | 用途 |
| --- | --- |
| [xxmanmi-video-downloader](skills/xxmanmi-video-downloader/SKILL.md) | 按本次授权选择采集、下载与恢复流程 |
| [project-cleanup](skills/project-cleanup/SKILL.md) | 核实归属后清理失效线程与专属资源 |
| [manying-production-sync](skills/manying-production-sync/SKILL.md) | 节目管理数据与资源的受控同步工作流 |
| [manying-project-deploy](skills/manying-project-deploy/SKILL.md) | 应用发布、结构迁移与回退工作流 |
| [manying-wechat-publisher](skills/manying-wechat-publisher/SKILL.md) | 公众号选题、图文排版、查重、存档与浏览器发布 |

保留完整仓库目录，使 Skill 的相对路径能够找到 [references/](references/)、[scripts/](scripts/) 和 [AGENTS.md](AGENTS.md)。在 Agent 的项目上下文中读取主 Skill，再说明本次任务、范围和输出目录。加载 Skill 不启动采集、清理或生产发布。

## 下载与本地工具

Python 3.11+。下载器运行于 Linux、macOS 或 Windows 的 WSL，需要 FFmpeg 与 ffprobe；可使用系统安装，或按下面的依赖清单安装 `imageio-ffmpeg`。

```bash
git clone https://github.com/yzzzzzzz123/xxmanmi-video-downloader.git
cd xxmanmi-video-downloader
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

浏览器操作使用当前 Agent 环境已经授权的工具，只从实际打开的公开页面 DOM 读取资源。将真实观察保存为 [队列约定](references/queue-contract.md) 中的 JSON 后，显式指定片库之外的工作目录：

```bash
.venv/bin/python scripts/download.py --output ./downloads ingest < observed-batch.json
.venv/bin/python scripts/download.py --output ./downloads run --limit 1
.venv/bin/python scripts/download.py --output ./downloads status
```

下载器只处理已观察到的未加密点播 HLS，保存进度并校验时长与 SHA-256。站点发现、同标签导航和范围完成判断按 [CUA 工作流](references/cua-workflow.md) 与 [下载交付规则](references/download-and-delivery.md) 执行；当前队列完成只证明该队列的处理结果。

公众号工具使用 Python 标准库，可在 Windows 运行：

```powershell
python -X utf8 skills/manying-wechat-publisher/scripts/publisherctl.py --help
python -X utf8 skills/manying-wechat-publisher/scripts/archive_articles.py --help
```

## 私有环境依赖

采集入库、封面服务、节目同步和应用部署沿用项目环境合同，需要使用者自行提供私有 `Datebase/xxmanmi-resource-db/` 与 `web/` 模块、数据库、运行目录及授权连接。这些流程不能仅凭公开仓库直接完成。部分配套脚本会导入上述私有模块；它们作为集成参考提供，运行前须核对自己的环境。

公开版文档及脚本中的 `ubuntu@example.invalid` 和 `~/.ssh/manying_deploy` 是示例，必须在使用者私有副本中配置并核实实际目标。连接凭据留在本机安全配置中。节目数据同步与应用发布各自需要用户明确授权，并保留生产账号、会员、会话、收藏和观看数据。

数据库目录、SQL、数据导出、采集证据、封面、公众号稿件、运行 manifest、视频与秘密由 `.gitignore` 排除。公开检查与打包只读取本仓库的 Skill 和工具，不连接数据库、不启动采集或服务。

## 检查与公开源码包

```powershell
python -X utf8 -B scripts/projectctl.py check
python -X utf8 -B scripts/projectctl.py package
python -X utf8 -B -m unittest discover -s skills/manying-wechat-publisher/scripts -p 'test_*.py' -q
```

默认包为 `dist/xxmanmi-video-downloader.zip`，包含公开 Skill、参考和工具，并附逐文件 SHA-256 清单。已有包不覆盖；可用 `--output` 指定新路径。下载器的本地隔离测试在 Linux/macOS/WSL 执行：

```bash
python3 -B scripts/test_download.py
```

只处理正常可访问且已获使用授权的资源，遵守来源站点与平台要求。
