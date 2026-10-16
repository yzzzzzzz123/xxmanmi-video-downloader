# 服务器自动发文

2026-10-15用户已取消服务器公众号执行，后续回到本机按当次篇数授权发布。服务器配置已禁用，定时器与专属服务停止，原稿与证据保留。下文仅供历史恢复，不构成重新启动服务器、定时器或国内转发的授权。

适用当前用户已确认的生产模式：北京时间每天12:00和18:30各3篇。定时器在该时刻启动一批，实际发表时间以逐篇编辑、平台审核和公开页核验结果为准；不承诺三个链接同一秒出现。不额外群发通知。

## 隔离部署

独立用户`manying-publisher`、独立`/opt/manying-publisher`代码、`/var/lib/manying-publisher`文章和登录资料；不使用网站数据库或网站服务。systemd以只读代码、专用可写目录、内存和CPU上限运行。微信公众号在服务器正常扫码；Codex默认独立登录，本次用户另行明确要求将本机Codex凭据通过加密SSH转移到服务器，已按私有目录700、认证文件600保存。认证内容不进入发布包、仓库、日志或知识库。

`manying-publisher.timer`每次启动`server_runner.py run-due`。定时器不追补错过的时段；任务槽和启动前的历史目录清单先落盘，全程文件锁串行执行。前批出现运行中、未完成或未知结果时，后批停住；不因为服务重启就另建三篇。

Codex承担选题、事实核对、独立写作和看图验收，使用本技能的持续RPA完成正常后台操作。不是只放一个cron去点击发表。模型登录失效、配额不足、微信扫码、页面变化或证据不足都必须保留真实状态。

生产调度启用后，服务器是唯一发文执行者。本机保留历史和转发程序，不并行启动另一套自动发文。临时人工补发也须先查服务器账本和微信发表记录，避免两个执行者重复选题或提交。

## 临时国内出口

按用户选择，通过这台国内Windows电脑转发。服务器RPA只连接`http://127.0.0.1:17897`，由加密SSH反向隧道连接本机`127.0.0.1:17896`的受限TLS CONNECT代理；仅放行微信相关域名和明确的出口检测域名，不解密TLS、不开放公网代理。Codex模型连接继续由新加坡服务器正常访问。

本机`china_relay.py`指定实际国内物理网卡，通过Windows IP_UNICAST_IF约束该程序自己的DNS/TCP和SSH传输；不修改系统代理、Clash模式、TUN或其他程序路由。物理网卡失效不回退海外出口。实现依据[微软IPPROTO_IP文档](https://learn.microsoft.com/en-us/windows/win32/winsock/ipproto-ip-socket-options)。更换网卡后必须重新核对interface索引和服务器实测出口。

本机隐藏计划任务`Codex-ManyingPublisherChinaRelay`在用户登录时启动，故障后重启；SSH断开自动重连。电脑须开机、用户保持登录、网络在线且不休眠。本机重启后未登录、网络中断或凭据失效时，该时段不能保证发文；`waiting_china_proxy`如实保留，默认不追补错过的时段。

每批先从服务器经该代理请求出口检测，确认中国大陆，再启动微信；真实账号和公开页仍独立核验。私有桌面同一SSH隧道转到本机`127.0.0.1:16087`，仅本机可访问；服务器VNC/noVNC也只监听localhost。

## 当前机器路径

- 工作区：`/opt/manying-publisher/current/workspace`
- 文章根：`/var/lib/manying-publisher/articles`
- 批次账本：`/var/lib/manying-publisher/jobs/YYYYMMDD-HHMM/job.json`
- 配置：`/etc/manying-publisher/publisher.json`
- Python：`/opt/manying-publisher/venv/bin/python`
- Codex：`/opt/manying-publisher/bin/codex`

以上路径是该部署的配置值，不改回Windows默认路径。命令的`--root`和`--dir`始终取本次真实目录。

## 每批执行与完成条件

先读job.json的before目录清单，再查看新增目录和后台。最多新建3个本批目录；恢复批次时只补剩余步骤。已有submitted/awaiting_admin/publication_unknown或提交意图不明的，先history→open-public→readcheck，绝不重放submit。

RPA启动带`--channel chromium --headed --authorized-draft --authorized-publish`，通过DISPLAY=:97的私有桌面供用户正常处理验证。保持一个持续会话。每篇仍逐张查看官方原图、横版和方形封面、当前保存阅读页与正式公开页；不得脚本化伪造verify。

`record-published`在实际后台与公开页核验后，额外保存`public-verification.json`，绑定标题、真实公开URL、render_digest、图片加载、正文顺序、入口和公开截图哈希。服务器只在本批恰好3个新增目录均有匹配回执时记completed，模型最终回复和退出0均不替代该检查。

## 运维

```bash
sudo systemctl list-timers manying-publisher.timer --no-pager
sudo -u manying-publisher env HOME=/var/lib/manying-publisher CODEX_HOME=/var/lib/manying-publisher/.codex /opt/manying-publisher/venv/bin/python /opt/manying-publisher/current/workspace/skills/manying-wechat-publisher/scripts/server_runner.py --config /etc/manying-publisher/publisher.json status
sudo systemctl stop manying-publisher.timer
```

停止timer不代表取消正在提交的发表；在途仍须查清。恢复指定批次使用`resume --job YYYYMMDD-HHMM`，不能默认恢复最新一批或清提交意图。没有追补授权，不补发过期槽。

用户另行明确要求立即发3篇时，使用`start-manual --job 当前北京时间YYYYMMDD-HHMM --authorized-publish`，不伪造定时器时间。任务账本先落盘，沿用同一全局锁、旧批次阻断、3篇限额和真实公开回执；相同ID不能重复启动。要求测试服务器直连时加`--network direct`，只为该批及其恢复移除RPA代理；日常定时器配置不变。运行时从既有生产服务复制相同隔离和资源限制到专属手动单元，不直接以root运行模型。

需要用户验证时，本批`handoff.json`仅保存waiting_human、真实目录和无认证数据的原因。模型保留RPA与页面等待正常人工验证；恢复时更新状态，未知提交仍先查历史。

## 两次首次登录

模型使用服务器专属`CODEX_HOME`。由用户完成`codex login --device-auth`显示的一次性入口与代码，不读或分享auth.json。若设备码方式不可用，按[OpenAI官方登录文档](https://developers.openai.com/codex/auth)使用SSH转发localhost回调，不绕组织限制。

微信登录运行`manying-publisher-login.service`，用户通过SSH转发的私有noVNC桌面正常扫码；该服务只登录和核对账号，不发表。VNC与网页仅监听localhost，不开放防火墙。默认不存二维码截图；用户当次明确要求本机扫码图时，可以把当前正常登录画面临时保存到指定本机位置，必须实际检查二维码可见，不放入仓库、发布包、应用日志或知识库。

登录、发文和doctor共享同一浏览器锁。登录或发文占用时doctor返回busy，不打开第二个同profile浏览器，不能用诊断打断当前扫码。登录状态即时写入私有login-status.json；收到停止信号后正常关闭浏览器保存会话。扫码确认后先等登录服务核对账号并退出，再做持久会话doctor检查。

`doctor`只读检查模型登录、现场微信账号和可用性，不生成或发表文章。部署验收可跑隔离UI测试，不能为了测试服务额外发三篇。

资源限制还须用真实微信登录页在systemd单元内验收，观察窗口持续显示、MemoryPeak和实际退出原因；在单元外通过隔离测试不能替代此检查。小内存机器的浏览器可能只出现Page crashed/TargetClosedError，父进程结果仍是exit-code；检查实际内存峰值和限制，不仅看单位是否报告oom-kill。按现有可用内存和交换空间调整本应用的界限，不重启网站。

实现使用[Codex非交互运行](https://developers.openai.com/codex/noninteractive)的JSONL事件及专属登录。模型限额仍以实际账号为准；服务状态须明确区分installed、waiting_model_login、waiting_wechat_login、running、awaiting_admin、attention和completed。
