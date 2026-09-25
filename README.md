# 小小漫迷视频采集 Skill

面向 Codex 的 CUA 采集工作流与 Python HLS 下载辅助工具。默认由 Agent 按全站范围发现资源，也支持用户指定节目、链接、分类和集数。只处理正常可访问的未加密点播资源，不绕过验证码、登录、付费或 DRM。

## 使用

让 Agent 阅读 [SKILL.md](SKILL.md)，再说明要采集的范围和输出目录。浏览器发现由 CUA 在原任务标签中完成；Python 脚本只下载已经观察并入队的媒体 URL，**不是独立的全自动全站爬虫**。

Python 3.11+，支持 macOS/Linux；需要 FFmpeg。可使用系统 FFmpeg，也可安装私有依赖：

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python scripts/test_download.py
```

将真实浏览观察按 [输入格式](references/queue-contract.md) 保存为批次 JSON 后：

```bash
.venv/bin/python scripts/download.py --output ./downloads ingest < observed-batch.json
.venv/bin/python scripts/download.py --output ./downloads run --limit 1
.venv/bin/python scripts/download.py --output ./downloads status
```

跨机器使用时显式指定 `--output`；省略时沿用原用户配置的本地默认路径。首次小样验证通过后，可省略 `--limit 1` 处理当前队列。继续站点发现仍由 Agent 按 [CUA 工作流](references/cua-workflow.md) 执行。

## 特性与边界

- 单并发直连下载，不改系统代理；默认保留 10 GiB 空闲空间。
- 原子保存 manifest，逐集记录结果、大小和 SHA-256；续跑跳过完整文件，未完成单集从头下载。
- 支持备选线路、相同媒体 URL 去重、防覆盖、有限重试及中断恢复。
- 首集完整解码；所有分集校验视频流与成片时长。
- 明确区分“当前队列完成”和“指定范围/全站覆盖完成”。

已通过 20 项本地测试，以及一个真实分集的下载、完整解码、中断续跑和容量保护验证。完整同标签逐页导航链与全站分页穷尽尚未实测，首次扩展前需要现场验证。

仓库不包含视频、运行 manifest、浏览器凭据、虚拟环境或测试下载产物。使用者应确认资源下载和使用权限。
