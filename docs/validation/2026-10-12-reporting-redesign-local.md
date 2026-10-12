# 广告报表重做：本地实施与验收记录

范围：已批准计划任务 1–14；任务 15 未执行。基线 `747e0b3`，在既有 detached worktree `reporting-redesign-20261012` 实施。未新建分支、未 push、未部署或修改服务器配置，未调用 TikTok Ads MCP 工具或真实 TikTok API，未执行广告写入、授权或供应。外部响应均为测试替身；PG18/Redis8 是本任务独立的本地测试容器。

## 1. 任务与提交

| 任务 | 提交 | 标题 |
|---|---|---|
| 1 | b05c21f | reporting: 新增按账户存放的分区报表事实表与同步任务表 |
| 2 | 7738fc8 | reporting: 统一报表平台字段到存储列的映射与解析 |
| 3 | 22dd148 | tiktok: 平台限流业务码 40100 进入延后重试而非判失败 |
| 4 | 6cb91bb | tiktok: 新增只走官方 SDK 的报表读取器 |
| 5 | 40560e5 | reporting: 报表按账户日期段整份替换发布并在库内汇总系列 |
| 6 | 6646ab2 | reporting: 报表任务按租户轮转执行并在截断时整份重拉 |
| 6 修正 | 6ec4990 | reporting: 截断拆分改为读取完整系列并限制超时重试 |
| 7 | 71a9164 | reporting: 授权后自动建立 BC 同步计划并按活跃账户分档刷新 |
| 8 | 59acd78 | ads: 目录同步请求独立于报表调度并在管理操作后定向刷新 |
| 9 | f5beece | reporting: Beat 派发分档同步任务并在额度紧张时自动放缓 |
| 10 | 53060f5 | reporting: 手动刷新插队热刷并提供按 BC 的同步状态 |
| 11 | 4dc6268 | reporting: 五维按广告日汇总并支持回收排序与数据缺口提示 |
| 12 | 2cd9138 | reporting: 素材日汇总关联使用位置且保留未知归属金额 |
| 13 | e12cef5 | frontend: 报表展示 D0–D30 回收及账户同步与对账状态 |
| 14 | dd7f6f9 | reporting: 删除旧报表路径并修复调度发布与查询边界 |

所有提交末尾均含 `Co-Authored-By: Codex <noreply@openai.com>`。任务 14 的审查修复均在 `dd7f6f9` 内。

## 2. 偏离计划与原因

| 任务 | 计划写法 | 实际实现及原因 |
|---|---|---|
| 1 | 共享指标 Column 定义；测试口令示例 ci-only | SQLModel 继承用 `sa_type=Numeric` 为每表生成独立列，避免 Column 不能复用；测试口令满足实际八位校验并提供 PROJECT_NAME。金额精度和 NULL 不变。执行时 head 为 `0022_provider_link_defaults`，0023 down_revision 与之完全一致。 |
| 3–4 | 测试替身、SDK 参数以实际代码为准 | 用真实 ApiClient.deserialize 与合成响应；已安装官方 SDK 原生支持 advertiser_ids、bc_id、UTC 参数，无自定义 HTTP 报表回退。共享准入补 reports.bc 白名单，发送前和准入后逐账户验证冻结范围，拒绝非法页码/总数。 |
| 5 | 发布日期段与 SQL 汇总 | 补齐小时本地日范围和系列身份校验，越界或缺失身份不发布，防止污染日期或账户。 |
| 6 | 缓存系列拆分、系列子任务分别发布 | 按用户裁定始终读取本次日期段完整 campaign_day 系列清单，每 30 个系列分批，全部完成后一次整份发布，避免遗漏新系列和部分 COMPLETE。拆分超时第六次 FAILED/mark_failed；普通额度延后不计次数。补租户幂等键、过期领取恢复、旧令牌围栏及读取计数。 |
| 7 | UTC 中午后小时窗口也只含今天；DiscoveryRun 字段以实际为准 | 按规格 §3.4 和用户裁定，campaign_hour 始终账户本地今天+昨天；ad/material 日保持 UTC 12:00 分界。实际 DiscoveryRun 用 COMPLETE、completed_at/created_at，限定 BC；稳定选主并停用已删除绑定。预算 Settings、模板、部署手册同提交完成。 |
| 8 | 仅迁移目录请求 | 既有 Alembic check 暴露任务 1 ORM 与迁移索引/FK 元数据不一致，补齐并仅排除实际报表子分区；不修改已有迁移。 |
| 9 | outbox 传 claim_token；32 天筛选直接调用 | 实际 outbox 拒绝 token 键，传 claim_id 映射数据库令牌；账户级读取按平台 30 天上限分片并按账户时区分组。补 SDK 脱敏权限分类、按增量计数、历史立即规划、目录恢复派发，保证去除旧 Beat 后不断链。 |
| 10 | 示例 API 路径；创建全部计划 | 采用实际 /api 前缀，手动请求仅维护当前租户计划，防止一个租户请求修改其他租户。 |
| 11 | 拼接剧键、事实名称回退 | 剧键用 JSON 元组防止分隔符碰撞；取日期段最新非空名称；选择依当前授权而非历史来源连接。SQL 严格汇总后稳定排序与计划一致。 |
| 12 | 素材事实与使用位置 | 无指标映射偏离；任务 14 按用户裁定补完整空素材对象的零值，D3/D30 保持 UNSUPPORTED/NULL。 |
| 13 | 在既有页面改列与状态 | 依仍有效旧规格 §3 恢复默认今日；多币种使用 ISO 币种码。六个既有 Biome 文件与基线逐文件零差异，未扩大范围修改。 |
| 14 | 删除旧表/模块/夹具 | 保留新查询仍需的租户/账户夹具，仅发布新事实；删除旧异步报表额度桶、合同与清单摘要。旧工具删除改变共享摘要，逐字段验证剩余 33 个合同不变后补素材证据版本注册，未改变素材业务逻辑或伪造真实证据。 |
| 14 | 计划示例的单事务扫描、月分区预建、逻辑调用计数 | 独立审查要求补统一预算、逐 BC 提交、空计划过滤、各档位退避、DEFAULT 搬迁、读取起账户锁、对账双向更新和真实发送边界计数；对应回归见第 9 节。 |
| 14 | 未定义大 BC 筛选续接及独立余额任务存储 | 0024 增加 `ad_sync_bc.screening_progress` 和 `report_balance_run`：账户筛选逐请求续接；余额每 30 分钟独立按 BC 单页读取，冻结集合存库，队列只传运行 ID/页码。解决期限反复耗尽、平方级读取、outbox 大载荷限制；不改变日期/指标/可见性规则。 |
| 14 | 完整但无事实对象未明确；旧迁移测试先 head 后 downgrade | 按用户两项裁定：完整空对象加法指标为 0，平台 NULL 严格保留、不完整空对象 NULL；只重排 builds 单个迁移测试顺序，保留全部断言及 0024 不可逆约束。 |
| 14 | 发布后刷新页面 | 主动刷新完成才换快照；普通后台发布只提示加载，保留跨页选择和预览，遵守旧工作台冻结合同。 |

## 3. 后端全量验证

最后一次六分片原始汇总（未改写计数）：

```text
后端合计：4642 项，通过 4567，失败 68，并行不稳定 0，用时 71.8 分钟
```

逐项 JSONL 实际为 4567 passed、63 failed（含 setup error）、12 skipped。汇总额外计入 `test_template_item_api_removed` 的 5 个随机 UUID 参数 ID：收集与执行时 ID 不同，执行时对应 5 项全部 passed；这是已知无关的收集 ID 问题。失败超过 CI 脚本自动重跑上限 20，因此另建独立测试库原样复跑全部 63 个实际失败，结果为 **1 failed, 62 passed in 1208.37s (0:20:08)**。原始全量结果不以复跑结果覆盖。

已知无关：`test_username_migration` 的 `pg_dump` 17 低于 PostgreSQL 18，未修改客户端配置或迁移断言。其他 62 项全部在原断言、原超时阈值下独立复跑通过，归为本机并行环境不稳定。失败堆栈集中在本地数据库等待后触发的调用期限、流程状态和并发等待；抽样堆栈停在未改动的 `bounded_resources.py` / psycopg 等待处，同样两项基线测试也通过。未为这些失败改动账户、搭建或素材业务代码。全量中的报表模块 136 项全部通过。

| 分片 | 原始 pytest 汇总 |
|---|---|
| 0 | 10 failed, 704 passed, 1 skipped, 40 warnings in 3896.17s |
| 1 | 12 failed, 696 passed, 6 skipped, 69 warnings in 4299.52s |
| 2 | 6 failed, 702 passed, 1 skipped, 23 warnings, 5 errors in 3962.17s |
| 3 | 3 failed, 817 passed, 86 warnings, 13 errors in 4167.88s |
| 4 | 6 failed, 824 passed, 3 skipped, 153 warnings in 4031.77s |
| 5 | 8 failed, 824 passed, 1 skipped, 88 warnings in 3328.95s |

命令：

```bash
uv run --frozen python ../deploy/ci/backend_tests.py --shards 6 \
  --work /tmp/ci-backend-reporting-final3-20261012 \
  --durations ../deploy/ci/test-durations.json tests
```

环境为计划指定的本地 PG18/Redis8；环境文件读取关闭，使用合成测试凭据和 CI AWS 占位值、UV_OFFLINE。没有真实账户凭据或报表数据。

实际失败逐项归类（完整 node ID；62 项“复跑通过”均指新库、单进程、原样复跑）：

| 用例 | 结论 |
|---|---|
| `tests/acceptance/test_isolation_recovery.py::test_remote_commit_lost_reply_uses_get_without_recreating[ad-False-acceptance_scenario0]` | 并行不稳定；独立复跑通过 |
| `tests/api/routes/test_username_migration.py::test_backup_restore_upgrade_preserves_identity_and_resolves_collisions` | 已知无关：pg_dump 17 / PG18 不匹配 |
| `tests/integrations/tiktok/test_dual_channel_flow.py::test_scene_to_enabled_ads_uses_creation_receipts_on_one_channel[cross-bc-OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/integrations/tiktok/test_dual_channel_flow.py::test_scene_to_enabled_ads_uses_creation_receipts_on_one_channel[cross-bc-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/integrations/tiktok/test_dual_channel_flow.py::test_scene_to_enabled_ads_uses_creation_receipts_on_one_channel[same-bc-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/capabilities/test_independent_review.py::test_malformed_role_schema_fails_instead_of_retrying_forever[role0]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_build_gateway.py::test_factory_exposes_adgroup_status_supplement[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_build_gateway.py::test_factory_exposes_scoped_build_read[AD-smart_plus_ad_id-build.get_ads-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_build_gateway.py::test_factory_exposes_scoped_build_read[ADGROUP-adgroup_id-build.get_adgroups-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_build_gateway.py::test_factory_exposes_scoped_build_read[CAMPAIGN-campaign_id-build.get_campaigns-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_build_gateway.py::test_factory_exposes_scoped_build_read[CTA-creative_portfolio_id-build.get_cta_portfolio-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_mcp_directory_publish.py::test_invalid_final_evidence_records_error_preserving_old[expired]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_mcp_directory_publish.py::test_invalid_final_evidence_records_error_preserving_old[future]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_mcp_directory_publish.py::test_removed_membership_stops_worker_before_http` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_runtime_directory_mcp.py::test_build_permission_removed_during_mcp_initialize_blocks_business_http` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_runtime_directory_mcp.py::test_changed_mcp_subject_cannot_refresh_old_facts` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_runtime_directory_mcp.py::test_expired_access_token_waits_for_normal_refresh_then_resumes_same_run` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_runtime_directory_mcp.py::test_fresh_upload_only_facts_require_reobservation_before_cached_build_permission` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_runtime_directory_mcp.py::test_mcp_complete_reobservation_enables_build_and_upload_for_actual_role[None-False]` | 并行不稳定；独立复跑通过 |
| `tests/modules/accounts/test_runtime_directory_mcp.py::test_unknown_refresh_outcome_blocks_runtime_without_business_http` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_dual_channel_scenes.py::test_http_receipt_publish_checks_current_semantics_but_accepts_credential_rotation[read_fact-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_dual_channel_scenes.py::test_same_job_finishes_with_new_token_and_discovery_observation[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_dual_channel_scenes.py::test_same_job_finishes_with_new_token_and_discovery_observation[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_concurrency.py::test_two_deliveries_have_one_request_and_no_open_database_transaction[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_review.py::test_scene_reader_uses_only_select_in_postgres_readonly_transaction` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_unique_pages.py::test_repeated_unselected_minis_id_never_completes[False-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_unique_pages.py::test_repeated_unselected_minis_id_never_completes[True-OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_unique_pages.py::test_repeated_unselected_minis_id_never_completes[True-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary[cta-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary[minis_regions-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary[missing_total-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary[region-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary[vbo-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_invalid_remote_scene_facts_fail_at_the_actual_channel_boundary[wrong_role-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene/test_scene_validation.py::test_unpaginated_identity_response_accepts_more_than_page_size[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene_jobs/test_draft_bootstrap.py::test_unknown_accounts_bootstrap_before_strict_resolution_then_wait_for_shared_scene` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/scene_jobs/test_pagination.py::test_205_identities_and_minis_require_complete_identity_and_five_mini_pages` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_accepted_create_drop_is_unknown_and_redelivery_does_not_send[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_client_cleanup_failure_observes_committed_id_and_never_recreates[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_client_cleanup_failure_observes_committed_id_and_never_recreates[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_final_quota_denial_requeues_identical_armed_attempt_without_http[ADGROUP-OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_final_quota_denial_requeues_identical_armed_attempt_without_http[ADGROUP-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_final_quota_denial_requeues_identical_armed_attempt_without_http[CAMPAIGN-OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_final_quota_denial_requeues_identical_armed_attempt_without_http[CAMPAIGN-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_gateway_rejects_quota_lease_shorter_than_worker_deadline[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_gateway_rejects_quota_lease_shorter_than_worker_deadline[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_initialize_rechecks_current_build_permission_and_claim[initialize-claim-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_initialize_rechecks_current_build_permission_and_claim[initialize-viewer-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_initialize_rechecks_current_build_permission_and_claim[tools/list-claim-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_initialize_rechecks_current_build_permission_and_claim[tools/list-viewer-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_mcp_known_ad_rejection_retries_same_frozen_request_once[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_channel_execution.py::test_worker_uses_real_gateway_and_keeps_receipt_before_close[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_execution_tasks.py::test_outbox_builds_all_layers_and_only_reads_ambiguous_creates[1-True-True]` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_expansion_delivery.py::test_page_budget_yields_one_current_successor` | 并行不稳定；独立复跑通过 |
| `tests/modules/builds/test_safe_not_sent_recovery.py::test_mcp_exhaustion_manual_retry_then_all_original_children_create[CTA-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_batch_candidate_scope.py::test_ten_thousand_preceding_rows_cannot_split_legal_anchor_rectangle[anchor-outside-page-OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_batch_candidate_scope.py::test_ten_thousand_preceding_rows_cannot_split_legal_anchor_rectangle[anchor-outside-page-OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_batch_distribution.py::test_complete_twenty_by_ten_rectangle_sends_once[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_batch_distribution.py::test_complete_twenty_by_ten_rectangle_sends_once[OFFICIAL_MCP]` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_batch_distribution.py::test_two_full_batches_reach_share_http_concurrently_without_overlapping_members[OFFICIAL_API]` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_covers.py::test_known_id_wrong_hash_or_geometry_never_ready` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_covers.py::test_old_ready_cover_get_and_ensure_reuse_without_readback` | 并行不稳定；独立复跑通过 |
| `tests/modules/materials/test_covers.py::test_stale_ready_cover_explicit_reconciliation_only_refreshes_known_image` | 并行不稳定；独立复跑通过 |

## 4. 静态检查与前端

- `uv run --frozen ruff check app tests`：通过。
- `uv run --frozen ty check app`：通过。
- 修改的 Python 文件 `ruff format --check`：通过，47 个文件。
- 前端本次改动范围 Biome：通过。
- 全前端 Biome：六个既有格式失败，与基线 `747e0b3` 内容完全一致：`src/features/accounts/McpAuthorizationSheet.tsx`、`src/features/strategies/StrategyList.tsx`、`src/features/tenants/TenantScope.tsx`、`src/index.css`、`tests/acceptance-batch.spec.ts`、`tests/tenants-accounts.spec.ts`。没有因本次改动新增的问题。
- `bun run build`：通过。
- `bun x playwright test --project=workspace`：任务 14 提交前 494 passed (4.0m)，整合 main 后 494 passed (6.0m)。覆盖后台更新保持分页/选择/预览、主动刷新后换快照、余额和放缓提示。

## 5. Review Focus

| 项目 | 结论 | 证据 |
|---|---|---|
| 账户日期边界和时区 | 通过 | `test_replace_only_touches_requested_accounts_and_dates`；UTC 13:00 日与小时分离测试 |
| 删除广告的当日消耗 | 通过 | `test_ad_day_payload_always_includes_status_all`；查询直接由完整广告事实汇总 |
| 活跃转零不误删既有当日事实 | 通过 | `test_inactive_account_keeps_published_rows_until_daily_refresh` |
| 两个 BC 可见、账户只同步一次 | 通过 | `test_shared_account_is_planned_once`、`test_shared_account_visible_in_both_bcs`；空主计划不占窗口 |
| 截断拆分整份重拉、不能发布半份 | 通过 | `test_truncation_splits_then_fails_visibly`、新系列发现、后页失败及系列批仍截断保留旧事实测试 |

## 6. 删除文件及残留检查

生产文件（11）：

- `backend/app/modules/reporting/{aggregation,contracts,facts,scheduling,sync,sync_models,tasks,trends}.py`
- `backend/app/integrations/tiktok/adapters/{mcp_reporting,sdk_reporting}.py`
- `backend/app/integrations/tiktok/contracts/reporting.py`

测试文件（8）：

- `backend/tests/modules/reporting/{test_aggregation,test_facts,test_report_sync,test_reporting_accuracy,test_scheduling,test_sync_recovery,test_trends}.py`
- `backend/tests/integrations/tiktok/test_reporting_adapters.py`

测试结束后清除本工作树 Python 缓存，将本次未跟踪的前端构建产物归档到本地工作记录（其中第三方 TanStack bundle 含通用 aggregation 符号），执行用户指定的原样 grep，源文件输出：

```text
backend/tests/modules/reporting/test_legacy_removed.py:52:                "aggregation",
backend/tests/modules/materials/test_ingest_models.py:511:        # No rescans or aggregation: only one file lookup and atomic header delta.
```

第一处是防止已删除旧模块恢复的回归断言；第二处是未修改的素材测试普通英文注释。生产源代码无旧报表引用。

## 7. 任务 15 人工清单（本轮均未执行）

1. 按部署手册核对目标确为 staging，完成数据库、Redis、项目独立归档、私有配置及证书备份，记录校验和、迁移 head 和恢复验证；排空旧队列后执行迁移及发布。
2. 人工确认并记录 `ADS_SYNC_ENABLED`、非空 `TIKTOK_CALL_POLICIES`、`TIKTOK_REPORT_DAILY_CALL_BUDGET` 在 API、所有 Worker、Beat 的实际加载值，移除已删除旧报表操作配置。
3. 已授权新骏伯 BC `7683817908149272592` 开启总开关后自动生成 BC 计划、历史回补与同步任务，不依赖手动刷新。
4. 抽三个有消耗账户最近 30 天逐日及逐系列对账：广告明细消耗和账户报表一致；收入差不超过 0.01×当日广告数。按计划人工使用新骏伯只读对照，不混用其他 BC。
5. 核对已删除广告历史消耗仍计入；无长期 FAILED；请求数量符合预算估算；40100 为延后而非失败。
6. 验证六维查询、排序、余额币种/资金池范围、账户本地更新时间、缺口、刷新和跨页冻结操作。
7. 验证官方 API 的 BC 报表权限及素材报表 20,000 行截断行为；将结果写入规格、独立 staging 验收记录和实施进度，提交记录。

## 8. Git 状态与集成

任务 14 在既有 detached worktree 提交；main 的既有 `AGENTS.md` 修改及未跟踪前端生成文件必须保持原样。另一会话已将场景依赖测试提交为 `8b22d7e`；整合执行时 main 已有的其他会话提交（至 `8b22d7e`），不把这些搭建业务和部署清单改动归入本任务，不 push。仅 `docs/implementation-progress.md` 发生冲突，保留双方全部记录；报表页面自动保留 main 的 `isLoading` 首次加载提示改动。报表后端实现与已完成全量的 `dd7f6f9` 一致。

整合后追加验证：307 项后端受影响测试初次 305 passed、2 failed；两项广告目录 MCP 替身在调用前期限/本地资源检查失败，新库原样独立复跑 2 passed (3.95s)。前端完整 workspace 494 passed (6.0m)，build、全仓 ruff、ty 通过，Biome 保持同六处既有格式失败、本次文件通过。没有因集成再修改业务代码。

main 采用本地快进合并；实际集成提交与最终 Git 状态见最终回报。

## 9. 独立审查问题、修复与回归

以下全部在任务 14 提交 `dd7f6f9` 修复；任务 6 两项另见 `6ec4990`。

| 问题 | 修复 | 对应测试 |
|---|---|---|
| 多 BC 共用长事务，后续超时回滚之前计划 | 全局 150 秒预算、逐计划事务、先派发再筛选 | `test_scan_commits_completed_plan_before_later_timeout` |
| 空计划占满 50 个扫描名额 | SQL 过滤无负责账户计划 | `test_empty_plan_does_not_consume_scan_limit` |
| daily 等到期字段绕过额度/读取退避 | 所有档位统一 retry_at 下界 | `test_daily_admission_waits_until_retry_deadline`（QPM/QPD） |
| DEFAULT 已有行时新月分区无法创建 | 父表锁内搬迁 DEFAULT 并 ATTACH，发布前预建 | `test_partition_creation_moves_existing_default_rows`、`test_daily_publication_creates_previous_month_partition` |
| 不同 tier 重叠读取/发布发生冲突和旧值晚覆盖 | 从读取前到提交持有账户/kind 事务锁 | `test_overlapping_tiers_defer_before_reading_and_publish_in_order`（真实 PG 并发） |
| 对账依赖完成顺序，保留旧 ad_sum | 两种发布均重算当前广告合计 | `test_reconciliation_tracks_latest_ad_publication`（两种顺序） |
| 入队后页面不随发布更新；后续修复又打断后台选择 | 主动刷新或用户加载才更换快照，普通后台更新提示 | Playwright“同步发布后清除第二页快照和选择…”、“后台发布不打断…”、“当前选择打开预览…” |
| 本地拒绝也记作物理发送 | SDK 真正发送边界增计数 | `test_physical_counter_excludes_local_admission_and_validation` |
| 缺少预算放缓提示 | 状态 API 返回 slowed，页面展示 | `test_status_exposes_budget_slowdown_and_material_publication`、Playwright“额度放缓状态可见” |
| 删除旧任务后余额采集/展示断链 | 独立半小时余额计划，独立币种/资金池/时间与导出 | `test_balance_refresh_is_independent_and_visible`、`test_balance_schedule_ignores_report_backoff_and_coalesces`、余额导出与发布版本测试 |
| 大 BC 回退筛选总从首批重来 | 0024 持久筛选游标，身份/日期/范围变更时重建 | `test_account_fallback_resumes_completed_requests_after_deadline`、`test_two_thousand_account_fallback_eventually_finishes_without_repeating_pages` |
| 逐账户扫描 BC 余额导致平方级读取 | 每页 100 个，单页观测与下一页同事务，重复页围栏 | `test_balance_batch_reads_one_bounded_page_and_resumes_at_requested_page`、`test_balance_page_commits_observations_with_next_page` |
| 大冻结账户集合超出队列载荷上限 | 数据库存运行范围，队列仅 run_id/page | `test_large_balance_scope_stays_out_of_bounded_queue_payload`（2,000 个 19 位 ID） |
| 新增余额清理元组机械性误插元素 | 修正解包，终态清理、ACTIVE 保留 | `test_cleanup_removes_expired_jobs_hours_and_snapshot_rows` |
| 完整空对象 NULL 污染同剧合计 | 按用户裁定分覆盖状态使用 0/NULL | `test_complete_empty.py`（六种情形） |

## 10. 迁移相关测试调整清单

仅修改 `backend/tests/modules/builds/test_preview_skipped_migration.py`：原迁移升级→原存在断言→原降级→原不存在断言→升级最新 head→原 Alembic check。没有删除或跳过任何断言；没有改 builds 业务代码；0024 downgrade 仍要求恢复发布前备份。

搜索全部测试中的 `command.upgrade` / `command.downgrade`：其余旧迁移测试已在升级最新 head 之前完成降级，或只做向前升级，无其他穿越 0024 降级的顺序需要调整。完整审计保存在本地工作记录 `migration-order-audit.txt`，不含凭据或真实数据。
