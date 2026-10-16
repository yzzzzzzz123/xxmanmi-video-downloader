# 本地 RPA 正式发表与结果补账

承接[持续会话与后台编辑](rpa-publishing.md)，沿用同一个rpa_session.py进程及原稿目录，不切浏览器工具。维护技能不构成再次发表授权，实际发文任务才带--authorized-publish运行。

## 1. 保存版本和门禁

核对真实账号、标题、摘要、配图、两种封面、文字原创、赞赏、适用来源、正文网站介绍和https://manying.site/原文链接。原稿已真实保存，从当前保存卡片打开阅读预览并核验。

```powershell
python -X utf8 -B "$skillRoot/scripts/publisherctl.py" check --dir $articleDir --stage publish
python -X utf8 -B "$skillRoot/scripts/archive_articles.py" check --root $articleRoot --dir $articleDir
```

变量沿用当前真实路径，verify只记真实观察；查重还须复核题材与表达。脚本再次检查门禁和查重，不替代模型判断。

## 2. 设置和确认分开操作

切回本次编辑tab，核对页面后分步请求。**不要预先拼成一串盲点计划**，每次返回并观察新页面再继续。

```json
{"op":"publication-open"}
```

这只点击编辑器“发表”，等待设置弹窗并返回通知、定时及额度文字，尚未提交。默认即时公开，不额外群发；用户明确要求通知时按其授权范围处理。

```json
{"op":"publication-config","notify":false}
```

脚本正常关闭群发、分组、定时开关并读回。开启群发须有有效通知授权、启动带--authorized-notify，并传notify:true。本版专门流程只支持即时、非分组发表；定时或分组须按现场DOM另行适配验收，不把未实现的设置当成功。

```json
{"op":"publication-review"}
```

这点击设置页“发表”，等待真实“继续发表”，返回确认文字和本稿标题。核对通知范围、即时设置、标题、封面和篇数。出现不同确认文案先重新观察和适配。

```json
{"op":"submit"}
```

仅此步尝试最终点击。先核对稿件、render_digest、通知配置和门禁，点击前在rpa-session-state.json及独立rpa-submit-intent.json写submit/pending；真实点击返回后才记submitted。独立提交意图不会被后续观察或导航覆盖。可能已发出的超时记publication_unknown；进程在点击与回执间中断时，同样阻止重发。选择器或文案在点击前不符，不发动作。

submit不是发表成功。submitted、awaiting_admin、publication_unknown及未知submit意图只能恢复核查，不重复点、不另建同题稿。每次页面变化先observe。实际管理员验证会记awaiting_admin并保留页面，扫码由用户处理，不保存二维码、验证码或认证URL，不自动操作认证控件。

## 3. 后台与公开页核验

正常进入本次后台的“发表记录”：

```json
{"op":"history"}
```

“等候发表”“审核中”保留submitted及实际platform_status，不是失败，不重发。有界wait或稍后刷新一次；不无限刷后台。当前页未命中须核对相关分页，重复标题或未知状态先辨认真实记录。

本稿后台为“已发表”，且链接为真实公开URL后：

```json
{"op":"open-public"}
{"op":"readcheck"}
```

open-public重新读取当前后台记录再打开对应链接，拒绝审核中的临时地址。readcheck检查真实公开正文、原生图片、适用来源和阅读原文，并保存wechat-public.png；存在原生映射时比较图片顺序与身份。用本地view_image核实图注和实际显示，GIF/视频另看播放。预览链接、只读到标题或点击返回都不算公开核验。

```json
{"op":"record-published","note":"本次实际后台记录及公开正文、图片、标注和原文入口的核验依据"}
```

要求同篇、同render_digest的后台已发表证据与真实公开页核验同时存在才记published，不能仅传任意URL记账。已有published不再编辑或提交。

## 4. 本地记录滞后和多篇恢复

本地仍blocked/prepared但后台已发表时，按history→open-public→readcheck核对原稿，再record-published补账。记录published-reconciled，不伪造本次提交或未执行的旧验收。

公开页可读到的实际发表时间保存为published_at，当前核验时间另存published_at_observed。归档优先实际发表日，避免昨晚文章归到今天。

```powershell
python -X utf8 -B "$skillRoot/scripts/archive_articles.py" archive --root $articleRoot
# 补归档时 --date 使用实际发表日期
```

多篇任务逐篇选择article，各自查历史和验收，不把上一篇事实/封面/预览依据沿用到下一篇；保持内容类型与风格轮换。后台自动生成的贴图草稿不因为“发布文章”指令而一并发布。

## 5. 交接与结束

- 完成后公开URL入账、按实际日期归档，发送quit关闭本次专属会话，报告真实标题、结果和链接。
- 管理员验证保留当前交互Chrome与进程，请用户扫码；完成后先观察、查历史，不重新开始。待验证时普通quit拒绝关闭；用户明确要求停止关闭才发送{"op":"quit","close":true}。
- 登录、验证码、新法律声明或权限由用户正常处理，不换浏览器工具、接口或登录存储绕过。
- 断开/超时保留原稿和提交意图，从同一RPA引擎的正常后台恢复核查，不清意图强制重试。

已在正常后台完成过三篇文章的独立RPA发表与公开页核验，证明流程可用；不代表未来始终登录、DOM永不变化或平台验证可以无人值守。
