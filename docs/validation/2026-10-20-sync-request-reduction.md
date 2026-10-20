# 同步请求精简生产发布验收（2026-10-20）

## 版本与授权

用户明确要求合并、推送并发布生产，取代此前“不合并、不推送、不发布”的交付限制。目标为新加坡 ACK `test` 命名空间，入口 https://tkada.shortclipflix.com 。未启动第二条并行发布流程，未人工调用 TikTok 业务接口；恢复原有启用的生产调度。

保留四个原提交：阶段1 `fac300659e0784fb6cfa2ba237a4fb86385ca388`、阶段2 `bb92ac5e1d647b3a5f6a49b6fe5bde5fa09e16e2`、阶段3 `cf560a3282ab398b5084d5fc73a4376de53e7b5d`、目录健康余量修正 `95c34bf20e1bb165b96bb7c428f925035447e18a`。main新增的两个文档提交先合入已有工作树分支，再将main快进至合并提交 `481974839bf0f63bda0cbf3da8ffa078863892af`；没有变基改写四个提交。该精确版本已推送origin/main及company/main，并由company推送自动触发Jenkins #100。

原main工作区未跟踪的同名设计/计划先校验并备存在忽略目录 `.runtime/sync-reduction-release/untracked-documents/`，其他未跟踪文件未删除、未暂存。发布后的验收文档单独提交并推送origin；company保留已发布精确版本，避免仅记录证据又触发一次发布。

## CI 实际结果

[Jenkins #100](http://123.207.4.46:8080/job/tiktok-ad-automation/100/console) 最终 `SUCCESS`，批次 `20261020T132356-a8a98c`，2026-10-20北京时间22:06:41结束。

- 后端总计4884：首轮4882通过、1失败后独立重跑通过、1跳过，最终0失败。重跑项为 `tests/modules/accounts/test_discovery_worker.py::test_each_call_admission_defer_keeps_same_page_without_send[4]`，不把首轮记为全绿。后端真实端到端四组56项包含在总数内。
- Ruff `app tests`、ty通过；前端624项工作区测试、4项批量验收、1项R2浏览器验收及构建通过。
- 发布工具真实PostgreSQL/Redis隔离、候选迁移、损坏归档拒绝、真实Celery排空/恢复验证通过。
- 后端检查1087秒，前端956秒，发布工具235秒。此轮合并仅补入main文档；原本地最终三模块963通过、mypy505对main510无新增、修改文件格式通过的证据见实施进度，不把存量mypy及44个格式问题记为已解决。

## 备份、迁移和恢复

本次包含0041/0042数据库迁移，采用完整流程，未使用快速发布备份豁免。发布前数据库大小22,660,030,991字节。正常停止生产者、取消消费并排空，冻结后备份，再在隔离实例恢复验证；没有清队列。

- 完整备份：PVC `tiktok-ad-automation-backup` 的 `/backup/latest/`；升级前版本 `3b4f8c5ed5e6fe0d761fe5b8510297eed3b5a838`，schema `0040_material_production`。
- manifest SHA256 `c0f6b356cca6573fc091264d9d15410f3d34fb017feea03ddff277a27f6b2de6`。
- PostgreSQL恢复170表逐表一致，6份凭据可解密，Redis56键验证通过；一致性标记 `frozen`。备份仅保留最近一份，无异地副本。
- 冻结后备份443秒、备份后隔离恢复与验证807秒，正式迁移30秒，恢复角色50秒。较长停写窗口是本次全库恢复验证的实际成本。
- `0041_rule_frequency.py`：两张规则表JSONB中的5M升级为15M，下行恢复为5M。
- `0042_drop_ad_material_main.py`：删除目录素材表两列及成对约束；下行恢复nullable列和约束，不能恢复已删除的假设非空数据。本次额外非空计数触发8秒查询超时后停止，不将超时解释为0；完整冻结备份提供数据恢复依据。

迁移前候选演练通过后正式升级；新代码与新schema一起恢复，没有回切旧程序。运行证据在Jenkins私有批次目录以及本机忽略的 `.runtime/sync-reduction-release/`，不提交私密配置、原始业务数据或备份。

## 上线后核查

2026-10-20北京时间22:10只读核查：

- 11个Deployment均1/1 Ready，镜像全部精确为48197483；Beat仅1个，9个Worker就绪检查通过。镜像digest `sha256:76d8ecf033ee6caa0c44f22b2acb8abc9c2eb200b72f0ed07b098649e3ef6174`。
- 发布器逐角色验证实际配置和MCP注册摘要、镜像digest、四开关及数据库连接池；四开关均true，Worker池2+1、API/Beat5+10，原并发/配额未改。
- HTTPS健康端点返回200、`true`。数据库head为 `0042_drop_ad_material_main`。
- 规则定义：15M三条、1H一条；历史运行定义：15M共807条、1H共82条。全部4+889条均通过新 `RuleDefinition.model_validate`，两表无5M。
- `ad_material_reference` 两列及 `ck_ad_material_main_identity` 均已消失；`material_report_day`、`material_post_link` 各两列仍在。
- 实际热报/素材周期15分钟、广告目录30分钟、上级60分钟、执行余量5分钟、报表SLO20分钟。目录健康阈值20/35/65分钟，规划提前5分钟保持10/25/55分钟。

## 未宣称通过的业务指标

22:09:42的两条健康记录均为DEGRADED，目录缺口分别3642和254；广告/素材过期账户均0、待操作0、未关联帖子0。全备停写刚结束，不能仅据这一时间点确认目录缺口的全部原因，也不能将服务Ready等同于业务健康全绿。应继续观察恢复后的目录刷新、补拉量、请求量和年龄分布；本次未取得稳定周期请求降幅或p95验收数据。未新做OAuth或真实广告写入验收。
