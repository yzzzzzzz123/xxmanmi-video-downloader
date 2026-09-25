# 队列、依赖与命令

下载器使用 Python 3.11+ 标准库和 FFmpeg（macOS/Linux，文件锁使用 `fcntl`）。优先使用用户明确指定的 FFmpeg、PATH 中的 FFmpeg，其次是当前 Python 环境的 `imageio_ffmpeg`。无可用 FFmpeg 时，可在本 Skill 的私有 `.venv` 安装 `requirements.txt`；不修改全局 Python、PATH 或系统代理：

```bash
python3 -m venv /Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/.venv
/Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/.venv/bin/python -m pip install -r /Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/requirements.txt
```

下面以 `python3` 举例；使用私有环境时替换为其 Python 路径。`--output` 是主命令参数，必须在子命令前。默认输出目录已写在脚本中，也可每次显式传入不同目录。

## 输入

`ingest` 从 stdin 读取不超过 16 MiB 的 UTF-8 JSON；批次文件由 Agent 根据 CUA 观察创建，保留在输出目录的 `observations/` 下，便于恢复浏览范围。不得放入 cookies、登录令牌或整份 Network 日志。

```json
{
  "scope": {"mode": "site", "selection": ""},
  "pages": [{
    "page_url": "https://www.xxmanmi.com/video2/119740.html",
    "title": "百家讲坛",
    "episodes": [{
      "label": "20251202",
      "line": "量子",
      "url": "https://v.lzcdn28.com/20251208/5615_bb24865b/index.m3u8"
    }]
  }],
  "coverage": {
    "discovered": ["https://www.xxmanmi.com/", "https://www.xxmanmi.com/video2/119740.html"],
    "visited": ["https://www.xxmanmi.com/video2/119740.html"],
    "failed": {},
    "exhausted": false
  }
}
```

这是已验证样本的结构示例，不是全站种子全集；不要未经浏览核对就批量生成其他资源 URL。也支持仅更新 `coverage` 的批次。`visited` 表示该页面所需发现工作已完成，`discovered - visited` 即待访问页面；失败页面不能隐藏。重访成功可重新提交 `visited` 或在 `coverage.resolved` 列出要清除的失败 URL。动态同 URL 的筛选/页码状态、下一步操作与穷尽证据保存在批次额外字段 `browser_checkpoint` 中，供 Agent 恢复；下载脚本忽略该字段，不会自动遍历。

指定模式用 `{"mode":"specified","selection":"节目/分类/URL 与集数边界的完整文字"}`；selection 必填，实际范围过滤由 CUA 采集方完成，脚本不会解析自然语言。后续批次可省略 scope，沿用当前范围；空新 manifest 默认为 site。

## 命令

```bash
python3 /Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/scripts/download.py ingest < /absolute/path/to/observed-batch.json
python3 /Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/scripts/download.py run --limit 1
python3 /Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/scripts/download.py status
```

正式执行当前已入队分集：`run`；继续未完成分集仍是 `run`。该命令不负责继续站点发现。默认磁盘阈值 `--min-free-gib 10`，`--limit 0`（默认）代表不限制当前队列处理数。

`run --retry-failed` 明确重试 failed/conflict 项；先阅读其错误，不自动反复重试。输出 exit 0 表示本次队列处理没有失败，不等于站点完成；exit 1 是配置/输入/锁等错误，exit 2 是容量/中断暂停，exit 3 是队列含失败或冲突。

切换为不同指定范围，或从指定改为全站：新批次带明确 scope 并执行 `ingest --new-scope`。此操作只重建 active 列表与 coverage，不删历史 catalog 和文件，必须把新范围要处理的分集重新提交入队。相同范围的续跑不要使用 `--new-scope`。

`ingest` 与 `run` 互斥，不能在正在运行的写入进程旁另开写进程；`status` 可在下载期间读取最近一次原子保存的 manifest。发现/下载宜交替小批进行，或发现记录先保存为批次文件，待下载批次结束再入队。

## 文件、完整性与恢复

- `manifest.json`：持久目录，包含 scope、active、catalog entries、coverage、错误、选中源、HLS 时长/分片数、成片时长/音视频信息、大小和 SHA-256。状态每集更新，写入使用同目录临时文件、fsync 与原子替换。
- 视频路径：`节目名_资源ID_线路编号/分集标签_稳定短ID.mp4`，非标准页面使用 URL 哈希作资源 ID。稳定后缀防止大小写/清理非法字符后的文件名碰撞；不要手工猜文件名，应读 manifest。
- 下载中使用同目录 `.part.mp4`。只有该 manifest 明确拥有的未完成文件会在重试时重建；未知文件、软链接、哈希冲突不会覆盖。发布使用无覆盖硬链接，再移除已验证的临时名字；崩溃发生在发布过程中也能按哈希核对恢复。
- 同页同标签视为同一集、多 URL 为备选；跨页相同 URL 如已有验证成片则标记 duplicate，并用 `duplicate_of` 指向实际文件，不再占一份空间。不按节目名/标签猜跨页内容相同。
- 已完成文件每次续跑核对大小与 SHA-256；改名、删除或外部修改会记 conflict。不要直接改 manifest 状态以绕过校验。
- HLS 支持相对 master/分片链接、最高带宽嵌套变体（最多 5 层）、正常未加密 ENDLIST 点播。拒绝加密、直播、外置 AUDIO rendition、非公共媒体地址。HTTP(S) 直连不携带浏览器 cookies。
- 时长容差为 `max(2 秒, HLS 总时长 × 0.0005)`；首集完整解码通过后设置 first_decode_verified。若线路格式改变，应单独临时目录做首集全解码验证，而不是删除正式 manifest 标记。
- SIGINT/SIGTERM 会停止当前 FFmpeg、保存 paused；强制 SIGKILL 后仍可根据最后保存的 owning 状态续跑。当前未完成单集从头重建，已完成集不重下。没有下载总时长上限。

## 本地回归

```bash
python3 /Users/yuzhizheng/Desktop/codex/skills/xxmanmi-video-downloader/scripts/test_download.py
```

测试使用隔离临时目录，不下载真实资源。它验证输入/范围合并、覆盖标记、文件冲突、原子恢复、磁盘保护、代理隔离等；不证明网站当前可访问，也不代替真实同标签导航或全站覆盖验证。
