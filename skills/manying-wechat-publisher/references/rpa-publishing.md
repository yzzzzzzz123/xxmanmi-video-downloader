# 本地 RPA：持续会话与后台编辑

后台编辑与最终发表都使用技能内Python/Playwright脚本，不调用浏览器工具。脚本正常操作DOM控件、文件上传、HTML剪贴板和鼠标裁切；不接管原用户浏览器、不复制登录存储、不调用微信私有接口。

## 启动一次，复用到发文结束

```powershell
$skillRoot = 'C:/Users/Admin/Desktop/codex/xxmanmi-video-downloader/skills/manying-wechat-publisher'
$articleRoot = 'C:/Users/Admin/Desktop/codex/xxmanmi-video-downloader/.runtime/manying-articles'
python -X utf8 -B "$skillRoot/scripts/publisherctl.py" status --root $articleRoot --limit 5
python -X utf8 -B "$skillRoot/scripts/rpa_browser.py" doctor
python -X utf8 -u -B "$skillRoot/scripts/rpa_session.py" --root $articleRoot --authorized-draft --authorized-publish
```

用exec_command(tty:true)启动最后一条，保存返回的真实session_id，之后用write_stdin发送**一行JSON加换行**。读取返回结果再决定下一步。启动参数只是执行门禁，不替代当次授权。仅维护技能或测试时，不启动真实公众号会话。

默认Chrome、headless，后台驱动隐藏启动。正常登录或管理员验证需要用户可见页面时，在开始该次交互会话时加--headed；这是交互Chrome，不是弹出辅助终端。不要用到时自动关窗的open --seconds承载发表验证。headless遇到验证时保留请求，不保存二维码，也不重发，见[本地RPA恢复](browser-publishing.md)。缺依赖只报告，不自动安装。

一个独立profile同时只运行一个会话，冲突时停止，不杀其他浏览器。结束本次工作才发送{"op":"quit"}，待管理员验证时保留进程和交互窗口。不注册常驻服务或计划任务。

## 请求、观察与导航

目录取status返回的真实原稿目录名；tab取本次tabs结果，不能沿用历史编号。选择文章会清除上篇的内存验收依据。

```json
{"op":"article","directory":"status返回的原稿目录名"}
{"op":"observe","limit":1800}
{"op":"tabs"}
{"op":"dom","selector":".weui-desktop-menu__link"}
```

observe返回有界可见文字与控件；dom只返回可见节点的标签、class、少量非敏感属性和父子结构，不导出认证URL、隐藏字段或整页HTML。tabs地址去除认证参数。screenshot只保存当前非验证页面到原稿目录，再用本地view_image查看。

后台账号须与manifest匹配，不能从网站品牌猜。下表是已验证定位线索，每次仍先观察；控件改变就按现场重新定位。

| 用途 | 定位线索 |
| --- | --- |
| 账号 | 编辑页.appmsg_account_name；后台.weui-desktop-account__nickname，当前首页.acount_box-nickname（须唯一且名称精确匹配） |
| 标题、正文 | .ProseMirror[data-placeholder]与.ProseMirror:not([data-placeholder])，不能混用 |
| 作者、摘要 | #author、#js_description |
| 草稿箱、发表记录 | #menu_10125、#menu_10126；必要时先展开“内容管理” |
| 阅读卡片 | 草稿a.weui-desktop-publish__cover__title，发表a.weui-desktop-mass-appmsg__title |

导航用click并传真实唯一可见selector及精确text。仅hover才出现的编辑入口先hover再dom观察。新tab入口加new_page:true，脚本用expect_page绑定；同页变化提供现场next_selector。标题卡片通常打开阅读页，不能当编辑入口。

```json
{"op":"click","selector":"#menu_10126","text":"发表记录"}
{"op":"history"}
```

history按本篇标题返回真实记录、状态和通过检查的公开URL，在途临时URL不输出。not_found_on_current_page只表示当前页未找到，旧稿须继续翻页核对。先查发表记录和草稿箱；已有稿恢复原编辑入口。published只核验，submitted/awaiting_admin/publication_unknown先查结果，不另建或重发。

click拒绝发表、认证、法律和删除动作，由专门操作处理发表与既有文字原创声明。不接受任意JS、HTTP或凭据输入。新建入口要求已有本篇历史观察且当前未发现同题草稿，这不替代历史分页和内容角度判断。

## 正文与原生配图

本地render、check通过，在确认空白的编辑器执行：

```json
{"op":"prepare"}
```

脚本核对账号、空标题和空正文，只上传content.json正文实际使用的登记图片。**逐张上传**，按每次新增data-imgfileid读取原生img，建立native-images.json，避免并发完成顺序与文件顺序不一致。重新render后正常HTML剪贴板粘贴，并等待当前正文节点出现内容。不改innerHTML、不调用编辑器内部保存函数。

prepare拒绝覆盖非空稿及重放未知准备动作。已有非空稿的剩余机械编辑可使用原rpa_browser.py run的**新观察、有界草稿计划**；先核实此前结果，不换目录或清状态重放。草稿计划仍不接受最终发表，保存文案支持“保存”“保存草稿”“保存为草稿”。

## 封面、原创和来源

```json
{"op":"cover-select","index":0}
{"op":"screenshot","name":"cover-review.png"}
```

index取本次正文图片顺序，从0开始。本地查看截图，检查2.35:1横版和1:1方形预览。方形可能裁掉半张脸，必要时点选当前截图中的方形预览，再拖动裁切框。pointer仅在当前“编辑封面”弹窗内可用，必须先取得并查看新截图。

pointer字段为kind:click|drag、x、y，拖动另传end_x、end_y。数值只来自现场截图，不固化本次坐标。每次坐标动作后重新截图。两种预览均核实后：

```json
{"op":"cover-confirm"}
{"op":"original"}
{"op":"links-source","source":"内容由AI生成"}
```

original沿用用户已确认的符合条件正文声明文字原创要求，只处理已知“微信公众平台原创声明及相关功能使用协议”，新法律声明交给用户。核对作者及可见赞赏状态，不能因不可见reward控件checked而误判开启。图片保留原权利人说明。

links-source从content.json读取原文链接，来源按实际创作方式提供，不是每篇自动选AI。右侧实际入口为.js_article_url_allow_click、.js_claim_source_desc，不强点隐藏复选框。原文确认控件是a.jsPopoverBt，不是button。保存后读回URL与标注。封面选好仍显示“拖拽或选择封面”时，以实际背景缩略图、裁切和保存版为准。

## 新保存与当前阅读预览

先dom观察实际历史版本/回执容器，找出能反映新增保存的唯一可见选择器。save请求传现场receipt_selector和新回执应含的receipt_text。默认按钮为“保存为草稿”，其他真实文案可传selector、text。脚本比较前后变化，只有新增回执/历史版本才记editor_saved；旧“已保存”不算。

当前后台可先点击`#history_bt`的“历史版本”，查看`#history_list`真实列表，再将它作为save的原生CSS回执容器，预期文字为“手动保存”。列表刷新会短暂清空，必须等到与原列表不同且包含新手动回执的完整状态；底栏“已保存”的时钟可能停留在旧自动保存时间，不能单独作为新保存证明。回调失败时先读真实历史版本核对结果，不盲目重放；依据新手动版本如实补录`draft-saved`后，再从保存卡片恢复预览。

保存后open-preview，tab为本次草稿箱页编号。它刷新真实草稿箱，从本稿卡片新打开阅读页，绑定本次render_digest。下面tab:0仅示意，必须替换成本次tabs返回的草稿箱页：

```json
{"op":"open-preview","tab":0}
{"op":"readcheck"}
```

readcheck核对标题、正文顺序、图片数与原生映射顺序，逐张滚动，按**当前DOM重新查询**等待naturalWidth>10，保存wechat-preview.png，公开页另存wechat-public.png。微信会替换懒加载节点，不能一直等待旧ElementHandle。它检查原文入口和适用AI标注，不自动替代图注、封面、GIF动画、事实或法律判断。

保存时可能重新编码微信CDN对象，阅读页URL与上传时不同，但`data-imgfileid`保留。两侧均为微信CDN且都有原生文件ID时，逐张精确比较该ID与顺序；缺少ID时只归一化已知尺寸后缀，仍精确比较图片主体路径，不能仅凭数量通过。会话因修复重启且本地已据真实回执记为`editor_saved`时，可用`open-preview`传`resume_saved:true`，从真实草稿箱重新打开当前保存稿；不能恢复未保存稿或跳过后续完整readcheck。修复已测试的本地实现后，可用`reload-local-code`更新方法并保留同一Chrome与页签；仅重载技能固定模块，不接收任意代码，提交在途或待验证时禁止使用。

查看本地截图后，用publisherctl verify记录facts、images、cover、preview、creation-source真实依据。render会清除旧观察检查；确认封面、改变声明/来源或重新保存时，相关观察检查也会失效。仅上传/排版、事实未变时可引用原已核来源与回执，不写死日期、不重复搜索同一证据，也不自动全部通过。正文或保存版变化后重新验收，再进入[本地RPA正式发表](rpa-publication.md)。

## 本地验收

原创声明的原生input可能被CSS隐藏。已知协议须继续逐字核对，通过可见label中的`i.weui-desktop-icon-checkbox`正常点击，再读回原生checked状态并确认；不强制点击隐藏input，不用JavaScript设置checked。当前原创弹窗已打开时，`original`可恢复同一弹窗。协议文字变化仍交用户处理。

服务器批次结束后systemd会回收子进程。不能仅凭模型写的`rpa_retained`断言浏览器仍在；需核对真实进程/单元状态。恢复批次时先查后台原稿与提交记录，未保存稿可能被后台自动保存，禁止再建同题编辑器。

从技能目录运行：

```powershell
python -X utf8 -B -m unittest discover -s scripts -p 'test_*.py'
```

测试只用隔离浏览器和本机回环模拟页，不访问微信、不改已发表文章、不消耗通知额度。接口以已安装Playwright为准，不为查用法调用浏览器工具。
