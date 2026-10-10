---
name: manying-wechat-publisher
description: 为漫映Manying微信公众号制作并发布有事实依据的影视和动漫图文，轮换文章类型与风格，以真实相关配图和短段落表达；用本地CLI完成素材、排版、去重和断点管理，微信后台发布使用已登录浏览器。用于公众号选题、写稿、发布和恢复，不用于微信群聊或漫映网站部署。
---

# 漫映公众号发文

本文件位于`manying-wechat-publisher`项目根目录，是本技能的唯一维护源；`scripts/`与`references/`随项目一起维护。全局技能安装入口可以链接到此项目，不另存一份独立正文。

目标是读者愿意看的文章，不是大量生成同质化稿件。用户指定题材、口吻、频率和发布范围时优先遵从；未指定题材时，根据可核实的近期消息和读者需求选择。不要把“有热度”写成没有证据的热搜排名。

用户要求后续每次发文轮换选题方向、文章类型、叙事口吻、图文版式和互动方式。相邻文章应有不同的类型与风格，不能只换作品名、标题或配图继续套同一模板；多篇任务也逐篇执行轮换。保留更多真实相关配图和短段落的要求，具体判断见[内容与数据合同](references/editorial.md)。

默认优先近期新番、新电影和新一季，核对真实播出年份与当前进度，不连续用古早经典主导选题。配图优先作品本身的官方剧照、场景图和宣传海报，逐张检查清晰度、构图与封面裁切；不自行制作抽象图卡、流程示意图或AI仿作替代动漫画面。只有用户明确要求怀旧内容或自制视觉时，才按该次要求调整。

内容深度和阅读节奏也要轮换：有时深入拆解，有时轻快短读。合适的位置可加入视频或动图。每篇均用自然的正文介绍和微信「原文链接」引导至 `https://manying.site/`；按用户长期要求，为符合平台条件的自写正文声明文字原创，保留真实创作来源标注。每天存档，起稿和发表前查重，具体执行见[内容与数据合同](references/editorial.md)。

## 范围与运行方式

- 当前会话已有明确的漫映发文授权时，正常稿件不逐篇重复请示；仅写技能、讨论方案、准备草稿不等于发布授权。发布前核对后台账号确为漫映Manying。
- 本技能不修改漫映网站、不动采集线程、不重启其他服务。微信登录、管理员扫码、验证码及新增法律声明由用户处理，不绕过。
- CLI只处理本地文件、公开图片下载和状态记录，不读取微信凭据，不模拟微信协议，不直接发送文章。
- 持续发布依赖实际运行的会话或已配置的产品调度机制。未配置调度时不要声称后台会自动发文，也不因创建技能而注册后台任务。没有明确频率时先做好当前稿，再约定后续节奏，避免连续刷屏。

## CLI优先与最短上下文

脚本：`scripts/publisherctl.py`，仅依赖Python标准库。运行 `python <脚本路径> --help` 查看命令；每个命令输出一份有界JSON。

工作目录沿用用户项目的文章目录；本机默认 `./.runtime/manying-articles`。一次只维护当前文章；先看状态，别把整个历史、浏览器文档和正文重复加载进上下文。

```powershell
$publisherCli = './skills/manying-wechat-publisher/scripts/publisherctl.py'
$articleRoot = './.runtime/manying-articles'
python $publisherCli status --root $articleRoot --limit 5
python $publisherCli new --root $articleRoot --topic '作品名与本篇角度' --title '本篇标题'
```

后续命令的 `--dir` 使用 `new` 返回的真实目录。不要为同一在途文章另建一个工作目录。旧版manifest由`status`只读展示；不要自动迁移、覆盖，尤其是已经等待扫码或结果不明的稿件。

## 主流程

1. **选题与证据。** 先查最近稿件的主题、类型和风格，避开同题同角度重复，按[内容与数据合同](references/editorial.md)确定本篇的轮换方案。将类型、口吻、图文组织、互动方式及与近期稿件的差异简记在当前稿件的`research.md`，供下次复用。最新动态、日期、上映地区、平台、评分等查权威来源，区分消息发布日期与事件日期；将采用的来源和对应事实保存到`content.json`，后续复用，不反复搜索已核验事实。
2. **正文与配图。** 写入结构化`content.json`。用多张确实相关、权属依据清楚的作品原有图片与短段落承载重点，按本篇类型安排顺序和节奏；图片数量、篇幅与版式按选题判断，不固定为一种图数或A–E选择模板。优先适用的官方宣传素材，不把公开可访问等同于授权。用户明确要求AI新图时才使用当时可用的imagegen技能，标明性质，不伪装正片截图。
3. **程序准备。** `fetch-image`下载已核实的公开图片，`register-image`导入自有/授权本地图片，`render`生成本地预览HTML和粘贴HTML，`check`校验文件、字数、摘要、素材与来源。CLI不能替代事实、版权、图片内容和排版的人工/模型判断。
4. **后台编辑。** 真正开始浏览器操作时才阅读 [后台发布与有界恢复](references/browser-publishing.md)，同时遵守当前安装的Browser/Chrome技能。复用已登录浏览器、有效绑定和既有草稿。先上传本地图片，再用实际微信图片DOM生成粘贴HTML，不反复尝试外链图片自动导入。
5. **验收与保存。** 验证标题、正文、配图、动图实际动画、封面、摘要及创作来源、文字原创声明、正文网站介绍与「原文链接」；看到保存回执后记录`draft-saved`。原生图片、封面和最终预览核验用`verify`分别落盘，缺项不能进入发布准备状态。再运行每日存档脚本的`check`，结合题材角度和表达判断排除重复。
6. **发布与确认。** 用户授权有效、`check --stage publish`通过后才提交。需要管理员扫码时记录`awaiting-admin`、保留当前验证页并明确请求用户扫码；结果不明记录`uncertain`，先查后台历史，不重复提交。只有后台已发表记录和实际公开文章URL都核实后才能记录`published`。
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
- 安装本技能本身不启动发布、不消耗群发次数，也不解除尚未完成的扫码验证。
