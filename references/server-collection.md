# 服务器采集与生产库唯一写入

用户在2026-10-14明确要求把采集任务完全搬到新加坡服务器，之后仅维护生产库；本地Goal已暂停，原库与历史只读保留。此要求替代旧的本地采集后SSH同步流程。本机不得再run、resume、kick或维护数据库。

服务器以专用`manying-collector`用户、一个Codex app-server和原生Goal持续执行。用户固定采集AI默认模型为 `gpt-6.1-sol`、推理力度为 `xhigh`；当前线程、恢复和后续重启均沿用，不自行切换其他模型或力度。不创建子agent或额外监督agent。systemd只承载执行环境；公众号定时任务保持停止。

唯一正式数据库为生产`manying`。每批仅在同库`collector_stage`中暂存当前节目，复用正式资源审计、真实帧封面质量/去重、连续失败停工与ACK。只有全部通过才按原ID和自然键事务合并`public.programs/resources/source_observations`，独立连接核对全部字段及实际封面哈希后计数。暂存不是另一套维护片库，失败和空页不进入正式节目表。分类、系列及生产用户表不导入、不覆盖。

唯一业务入口仍是`python -X utf8 -B scripts/collection/single_agentctl.py run --thread 当前CODEX_THREAD_ID --max-views 8 --steps 1`。服务器模式由固定私有配置启用，普通数据库读取直接查生产；正式导入必须在当前批次隔离上下文。`production_live`在该模式不启动SSH或本地同步执行器。

服务器没有本机Clash/TUN依赖。采集浏览器禁用环境代理，封面HTTP客户端保持`trust_env=False`，按实际公网socket与Linux路由记录原生直连证据。Windows精确DIRECT/TUN规范仅用于历史本机执行，不用伪造Windows环境。

私有8091端口仅保留暂存封面、状态及已授权的媒体relay路由；不提供网站账号或用户接口。正式网站8088保持原服务。共享封面读取用于跨全库去重，暂存通过后只安装本批已核验JPEG。

固定工作区`/var/lib/manying-collector/workspace`；正式状态、原交接快照、原Goal与路径映射在其`.runtime/collection-server`。旧owner及原证据原样保存，新服务器owner独立注册；不把Windows绝对路径当成有效服务器证据，不伪造旧ACK或清故障计数。

服务器Native Goal持续运行，单批或空队列不结束。真实故障7200秒或连续第7个不同节目封面失败时关写闸、暂停Goal并停止专属执行；服务启动不解除人工或故障暂停。

解码失败先有界修复，当前节目复验仍失败则按正式待补结果跳过并继续其他队列，不以可跳过的单节目解码失败反复阻塞整个Goal。专用服务固定使用系统FFmpeg，已鉴权HLS relay允许无扩展名分片；现有质量、去重和停工门禁不变。

真实用户明确继续已blocked/paused的服务器任务时，管理员用`collector_host.py --authorize-user-resume 绝对路径请求文件`记录该次指令，再启动原服务。宿主向同一原生线程投递实际user消息；该线程读取真实消息ID后调用正式`recoveryctl resume`。普通宿主暂停的继续保留当前故障周期和计数，不伪装成新周期；正式停工仍须先核验完整退出再复工。普通服务启动不制造继续授权。

运维：`systemctl status manying-collector.service manying-collector-cover.service`；停止：`systemctl stop manying-collector.service`。`host.json`与`server_production.status()`才是实际运行/生产验收依据；服务存在不代表采集成功。

## 采集AI自行修复授权

2026-10-15用户明确给予采集AI长期修复权限：采集源码、封面处理和采集运行环境出现可定位、可验证的故障时，由当前唯一AI自行完成诊断、原文件及权限备份、最小修复、测试、部署、必要的采集专属服务重启和独立复验，不因例行修复权限反复请求用户确认。修复后继续同一原生Goal、原批次与来源断点。

服务账户对 `scripts/collection` 及明确的封面Python模块具有写权限；通过系统Polkit仅管理 `manying-collector.service` 和 `manying-collector-cover.service`，直接使用 `systemctl --no-ask-password restart 服务名`，不依赖sudo。保留 `NoNewPrivileges=yes`、原服务身份和私有loopback监听。修改前核对真实在途任务，保留可回退原件，按变更运行相关测试并检查服务健康、原owner/ACK、实际图像和独立生产回执。测试失败不能算修复完成。

采集主服务通过 `Wants` 和 `After` 启动封面服务，不使用对封面服务的 `Requires`：重启被依赖的封面服务会沿 `Requires` 停止采集AI，宿主随后关闭写闸，再启动也无法自动继续。封面服务原有 `PartOf=manying-collector.service` 保留，停止采集主服务时仍能一并收拢专属工具。修改依赖后，在无在途claim和解码任务的安全点实测封面重启，确认唯一采集AI主PID、app-server子进程和原生Goal保持，私有健康检查通过且网站未重启；写闸与故障门禁照常执行。安装模板必须同步采用同一依赖，避免下次部署复发。

此长期授权适用于上述AI可完成的采集修复，不授权无关网站功能发布、修改账号/会员/会话/行为数据或绕过资源、封面、ACK、直连和独立验收门禁。用户明确暂停/停止、连续7个不同节目封面失败及7200秒故障停工仍按原规则执行，不能靠重启或重置计时解除。本次权限修复及恢复由用户当前指令授权，正式恢复使用上文原宿主的用户消息入口。
