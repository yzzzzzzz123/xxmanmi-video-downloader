---
name: manying-wechat-publisher
description: 为漫映Manying微信公众号选题、写稿、配图并发布影视动漫文章。通过本地Python/Playwright RPA持续会话完成编辑、封面、声明、保存、发表和公开页核验，不使用浏览器工具。支持去重、扫码交接和断点恢复，不用于微信群聊或漫映网站部署。
---

# 漫映公众号发文

本文件位于`manying-wechat-publisher`项目根目录，是本技能的唯一维护源；`scripts/`与`references/`随项目一起维护。全局技能安装入口可以链接到此项目，不另存一份独立正文。

目标是读者愿意看的文章，不是大量生成同质化稿件。用户指定题材、口吻、频率和发布范围时优先遵从；未指定题材时，根据可核实的近期消息和读者需求选择。不要把“有热度”写成没有证据的热搜排名。

用户要求后续每次发文轮换选题方向、文章类型、叙事口吻、图文版式和互动方式。相邻文章应有不同的类型与风格，不能只换作品名、标题或配图继续套同一模板；多篇任务也逐篇执行轮换。保留更多真实相关配图和短段落的要求，具体判断见[内容与数据合同](references/editorial.md)。

默认优先近期新番、新电影和新一季，核对真实播出年份与当前进度，不连续用古早经典主导选题。配图优先作品本身的官方剧照、场景图和宣传海报，逐张检查清晰度、构图与封面裁切；不自行制作抽象图卡、流程示意图或AI仿作替代动漫画面。只有用户明确要求怀旧内容或自制视觉时，才按该次要求调整。

内容深度和阅读节奏也要轮换：有时深入拆解，有时轻快短读。合适的位置可加入视频或动图。每篇均用自然的正文介绍和微信「原文链接」引导至 `https://manying.site/`；按用户长期要求，为符合平台条件的自写正文声明文字原创，保留真实创作来源标注。每天存档，起稿和发表前查重，具体执行见[内容与数据合同](references/editorial.md)。

## 范围与运行方式

- 2026-10-15最新用户决定：公众号不在服务器执行，回到本机按当次授权有界发布。服务器发文服务、定时器和专属国内转发已停用，稿件与历史保留；不得依据旧部署说明自动恢复。网站与服务器采集继续按各自约定运行。
- 当前会话已有明确的漫映发文授权时，正常稿件不逐篇重复请示；仅写技能、讨论方案、准备草稿不等于发布授权。发布前核对后台账号确为漫映Manying。
- 本技能不修改漫映网站、不动采集线程、不重启其他服务。微信登录、管理员扫码、验证码及新增法律声明由用户处理，不绕过。
- 后台统一使用本地Python/Playwright RPA，不调用CUA、浏览器插件或浏览器工具，也不在异常时自动切换过去。`publisherctl.py`负责稿件与记录，`rpa_session.py`保持同一独立Chrome会话，`wechat_rpa.py`实现正常界面操作；`rpa_browser.py`保留诊断、登录和有界草稿计划。不读取或复制原用户Chrome的Cookie、登录存储，不调用微信私有发布接口。
- 持续发布依赖实际运行的会话或已配置的调度机制。本机发文按当前明确授权的篇数执行，默认不额外群发通知；仅创建或维护技能不启动发文。原服务器每天12:00和18:30各3篇的安排已被用户撤销，[服务器自动发文](references/server-automation.md)仅保留历史恢复资料。

## CLI优先与最短上下文

稿件脚本：`scripts/publisherctl.py`，仅依赖Python标准库。后台使用`scripts/rpa_session.py`，另需Playwright和已安装的Chrome/Chromium。用`exec_command`启动持续进程，再用`write_stdin`发送逐行JSON请求；Windows使用`tty:true`，后台驱动创建时隐藏。命令和会话复用见[本地RPA后台操作](references/rpa-publishing.md)。只读取当前阶段的有界DOM摘要与必要截图。

工作目录沿用用户项目的文章目录；本机默认 `C:/Users/Admin/Desktop/codex/xxmanmi-video-downloader/.runtime/manying-articles`。一次只维护当前文章；先看状态，别把整个历史、浏览器文档和正文重复加载进上下文。

```powershell
$publisherCli = 'C:/Users/Admin/Desktop/codex/xxmanmi-video-downloader/skills/manying-wechat-publisher/scripts/publisherctl.py'
$articleRoot = 'C:/Users/Admin/Desktop/codex/xxmanmi-video-downloader/.runtime/manying-articles'
python $publisherCli status --root $articleRoot --limit 5
python $publisherCli new --root $articleRoot --topic '作品名与本篇角度' --title '本篇标题'
```

后续命令的 `--dir` 使用 `new` 返回的真实目录。不要为同一在途文章另建一个工作目录。旧版manifest由`status`只读展示；不要自动迁移、覆盖，尤其是已经等待扫码或结果不明的稿件。

## 主流程

1. **选题与证据。** 先查最近稿件的主题、类型和风格，避开同题同角度重复，按[内容与数据合同](references/editorial.md)确定本篇的轮换方案。将类型、口吻、图文组织、互动方式及与近期稿件的差异简记在当前稿件的`research.md`，供下次复用。最新动态、日期、上映地区、平台、评分等查权威来源，区分消息发布日期与事件日期；将采用的来源和对应事实保存到`content.json`，后续复用，不反复搜索已核验事实。
2. **正文与配图。** 写入结构化`content.json`。用多张确实相关、权属依据清楚的作品原有图片与短段落承载重点，按本篇类型安排顺序和节奏；图片数量、篇幅与版式按选题判断，不固定为一种图数或A–E选择模板。优先适用的官方宣传素材，不把公开可访问等同于授权。用户明确要求AI新图时才使用当时可用的imagegen技能，标明性质，不伪装正片截图。
3. **程序准备。** `fetch-image`下载已核实的公开图片，`register-image`导入自有/授权本地图片，`render`生成本地预览HTML和粘贴HTML，`check`校验文件、字数、摘要、素材与来源。CLI不能替代事实、版权、图片内容和排版的人工/模型判断。
4. **后台编辑。** 先读[本地RPA后台操作](references/rpa-publishing.md)。用`doctor`检查依赖，启动一个`rpa_session.py`持续进程，核对真实账号、发表记录和原草稿。已发表的只核验与补账，在途稿先查结果。对确认为空的编辑器执行`prepare`：逐张上传→按新图片DOM建立文件映射→重新render→正常HTML剪贴板粘贴。封面、文字原创、创作来源和原文链接也在同一本地RPA会话完成。横版与方形裁切均看截图，坐标只来自当前截图；异常按[本地RPA恢复](references/browser-publishing.md)处理。
5. **验收与保存。** `save`比较现场历史版本/保存回执的前后变化，真实新增后才记`draft-saved`。从该保存稿卡片新打开阅读预览，`readcheck`核对正文顺序、图片身份和加载，并保存截图；它不自动把facts、cover等人工判断标成通过。核对标题、摘要、作者、图注、两种封面、文字原创、赞赏、适用来源、网站介绍和原文链接后，用`verify`记录真实依据。GIF/视频另查实际动画/播放。最后查重并复核题材与表达。
6. **发布与确认。** 授权有效、发表门禁和查重通过后，沿用同一会话，按[正式发表RPA操作](references/rpa-publication.md)依次调用`publication-open`→观察→`publication-config`→观察→`publication-review`→核对→`submit`。默认即时公开发表，不额外群发通知；用户明确要求通知时遵从其范围。不把未知确认串成盲点计划。最终点击前写持久意图，提交后不重放。扫码、审核中和等候发表保留真实状态。后台已发表记录与对应公开页均核验后才`record-published`；脚本退出0不等于发表成功。
7. **后续。** 正式公开页核验后，用`scripts/archive_articles.py archive`更新当日存档，保留全文、素材摘要、公开链接及类型、深度和节奏。输出标题、真实状态和必要的用户动作；不输出整套内部操作。用户再次要求继续时先查CLI状态和后台结果，再恢复断点。

## 常用命令

```powershell
python $publisherCli fetch-image --dir $articleDir --url 'https://官方站点/宣传图.jpg' --file 'cover.jpg' --credit '真实出处与权属说明'
python $publisherCli register-image --dir $articleDir --path '实际本地图片路径' --file 'another.png' --credit '图片出处、权属和真实性质'
python $publisherCli render --dir $articleDir
python $publisherCli check --dir $articleDir
python $publisherCli verify --dir $articleDir --field facts --note '实际核验依据'
python $publisherCli verify --dir $articleDir --field images --note '正文图片均加载完整，位置正确'
python $publisherCli verify --dir $articleDir --field cover --note '封面实际预览和裁切已检查'
python $publisherCli verify --dir $articleDir --field preview --note '当前保存版本的阅读预览已检查'
python $publisherCli verify --dir $articleDir --field creation-source --note '后台创作来源标注已保存'
python $publisherCli record --dir $articleDir --event draft-saved --note '后台真实保存时间或回执'
python $publisherCli check --dir $articleDir --stage publish
python $publisherCli record --dir $articleDir --event submit-requested --note '实际点击提交后观察到的状态'
python $publisherCli record --dir $articleDir --event awaiting-admin --note '微信要求管理员扫码验证'
python $publisherCli record --dir $articleDir --event published --url '实际核实的公开文章URL' --note '后台已发表记录与公开正文均核实'
```

`verify`和`record`只记录真实观察，不能因为想推进流程就把字段设为通过。公开发布页和保存后的阅读预览不是旧的临时预览。`published`终态不重新生成正文；需要改稿时另按用户修改授权处理。

## 明确的完成条件

- 草稿：后台实际保存，并验证正文配图、封面与必要声明；本地manifest记为`editor_saved`。
- 待扫码：内容准备完成，但微信要求验证；不是发布成功，也不能称全自动已经跑通。
- 发布：真实后台已发表记录和公开正文都已核实，公开URL入账，无重复文章。
- 本地记录滞后：由真实已发表记录打开公开页，核对原稿后`record-published`补账，不伪造历史提交或验收事件；按公开页实际发表日期归档。
- 安装本技能本身不启动发布、不消耗群发次数，也不解除尚未完成的扫码验证。
