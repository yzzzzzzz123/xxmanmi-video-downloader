# 生产同步入口与历史轮恢复

当前日常流程是[提交事件增量与手动全库校准](all-catalogue-sync.md)，执行[节目同步技能](../skills/manying-production-sync/SKILL.md)及其[细则](../skills/manying-production-sync/references/release-procedure.md)。不等待 300 个节目，不生成关键词、分类或系列任务。

`production_live.py` 冻结待同步事件，由单一隐藏有界 CLI 持单写锁备份、事务发布并独立验收。`catalogue_align.py` 用于用户明确要求的全库校准；delta 回执只证明列出的节目范围，不能宣称全库一致。管理记录包括空、raw、失败与历史记录，观看端仍有资源和封面门禁。

生产账号、会员、会话、收藏、观看、支付和活动数据始终由生产保有。本地数据不得整库覆盖生产，节目同步不部署应用、不重启服务；项目变更发布另读[应用发布技能](../skills/manying-project-deploy/SKILL.md)。仅检查、整理、规范变更和已暂停采集不触发生产。

## 旧冻结轮

`production_round.py`、`production_round_publish.py` 和 `recover_production_baseline.py` 保留历史基线、冻结包与验收恢复能力，原 100/300 回执不重置。它们不作为新任务的常规触发方式；`production_sync.py once` 已禁用，旧按分钟安装器与周期报告工具已删除。

只有用户明确要求恢复既有冻结轮，才读取旧账本和原包身份：

1. 使用原 owner/work、原验证 SHA-256 和清理归档索引核对真实证据，缺失时保持未完成，不重采补造历史回执。
2. 校验原固定节目 ID、详情自然键、原资源、封面与包哈希；保留同一失败包，不混入后续新增节目。
3. 由既有发布工具执行本次已授权的幂等恢复并独立读取生产核对，生产数据保护与备份门禁不削弱。

历史用户认可回执继续由 goalctl 读取，不能充作新采集、抹去审计缺口或伪造 ACK。一次性历史认可写入工具已移除，现有 JSON 回执和统计兼容代码保留。
