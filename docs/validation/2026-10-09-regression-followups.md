# 2026-10-09 扩展回归待处理项

## 证据范围

为接续本地验收，在全新专用 PostgreSQL 数据库中启动了后端非 acceptance 回归（收集 4,515 项）。扫描至约 39% 后主动停止，将本轮范围保持为原会话的验收修复；本记录不是全仓最终统计，未运行部分仍未知。

过程日志记录了 57 项 call 失败和 5 项 teardown 错误。其中 4 项双通道流程、2 项历史迁移和 1 项场景读取在进程加载测试后完成修正，随后独立复测通过。下表保留其余 50 项 call 失败和 5 项 teardown 错误（52 个不同用例）；尚未逐项独立复现，不把夹具连带失败等同于独立产品缺陷。

命令：从 `backend/` 运行 `pytest -q --ignore=tests/acceptance --tb=short`，使用专用 `_test` PostgreSQL 数据库及 Redis DB 2。原始本地过程日志为 `/tmp/tk-ada-regression-clean.log` 和 `/tmp/tk-ada-live-failures.log`；这两个路径只用于本机会话续接，不是可移植测试工件。

## 已观察到的类型

- 账户适配器返回新增 `create_time`，旧精确字典断言未同步。
- capability 夹具删除连接时仍有 discovery run 外键引用，导致清理错误；必须先修清理边界，再独立复测后续受影响用例。
- 目录发现多项合成响应与当前流程不一致，包含 `tiktok_response_error`；需核对完整回执，不应直接放宽生产校验。
- 成员关系拒绝码与旧断言不同（`tenant_forbidden` / `action_forbidden`）。
- 历史迁移测试使用当前 ORM 新字段，或临时约束探针缺少 `source_bc_id`。
- MCP 权限/阶段、广告管理与广告目录另有失败，尚未完成根因确认。

## 待独立复测用例

### `tests/modules/accounts/test_account_adapters.py`

- `test_unknown_capabilities_are_not_normalized_from_unverified_fields`（call）

### `tests/modules/accounts/test_capability_boundaries.py`

- `test_capability_rechecks_current_actor_and_claim_before_business_http[viewer]`（teardown）
- `test_capability_rechecks_current_actor_and_claim_before_business_http[claim]`（teardown）
- `test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever[OUTCOME_UNKNOWN-mcp_refresh_unknown]`（call）
- `test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever[OUTCOME_UNKNOWN-mcp_refresh_unknown]`（teardown）
- `test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever[NO_REFRESH_TOKEN-mcp_refresh_reauth_required]`（call）
- `test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever[NO_REFRESH_TOKEN-mcp_refresh_reauth_required]`（teardown）
- `test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever[REFRESH_PENDING-mcp_refresh_pending]`（call）
- `test_unknown_refresh_blocks_normal_capability_instead_of_requeueing_forever[REFRESH_PENDING-mcp_refresh_pending]`（teardown）

### `tests/modules/accounts/test_directory_observation_migration.py`

- `test_upgrade_preserves_history_and_separates_observation_from_authority`（call）

### `tests/modules/accounts/test_directory_staging_migration.py`

- `test_staging_migration_preserves_live_directory_and_refuses_evidence_loss`（call）

### `tests/modules/accounts/test_discovery.py`

- `test_finalization_requires_bc_end_evidence`（call）
- `test_complete_scan_retires_only_own_connection`（call）
- `test_missing_metadata_retained_blocked`（call）
- `test_stale_generation_cannot_finalize`（call）
- `test_api_directory_publishes_actual_role_permissions_without_a_second_job[ADMIN]`（call）
- `test_api_directory_publishes_actual_role_permissions_without_a_second_job[OPERATOR]`（call）
- `test_api_directory_publishes_actual_role_permissions_without_a_second_job[ANALYST]`（call）
- `test_api_directory_publishes_actual_role_permissions_without_a_second_job[None]`（call）

### `tests/modules/accounts/test_discovery_worker.py`

- `test_full_discovery_and_duplicate_delivery`（call）
- `test_candidate_only_promoted_after_full_scan`（call）
- `test_each_call_admission_defer_keeps_same_page_without_send[0]`（call）
- `test_each_call_admission_defer_keeps_same_page_without_send[1]`（call）
- `test_each_call_admission_defer_keeps_same_page_without_send[2]`（call）
- `test_each_call_admission_defer_keeps_same_page_without_send[3]`（call）
- `test_each_call_admission_defer_keeps_same_page_without_send[4]`（call）
- `test_missing_page_evidence_retains_progress_and_does_not_finalize`（call）
- `test_worker_sdk_network_holds_no_run_or_connection_locks`（call）
- `test_same_run_concurrent_workers_send_once`（call）
- `test_two_tenant_asset_claim_is_serialized_and_conflict_retained`（call）
- `test_second_page_failure_resumes_same_run_and_does_not_remove_old_access`（call）
- `test_expired_claim_recovered_with_durable_watchdog`（call）
- `test_permission_revoked_before_next_call_stops_without_sending`（call）
- `test_redis_unavailable_defers_without_sending`（call）
- `test_too_short_lease_is_rejected_before_sdk`（call）
- `test_historical_run_without_candidate_does_not_infer_current_route`（call）
- `test_sdk_authorized_response_capacity_is_checked_before_parsing`（call）

### `tests/modules/accounts/test_mcp_authorization.py`

- `test_non_admin_without_tenant_membership_cannot_authorize`（call）
- `test_inactive_membership_blocks_callback[redeem]`（call）
- `test_inactive_membership_blocks_callback[publish]`（call）
- `test_inactive_membership_blocks_callback[cancel]`（call）

### `tests/modules/accounts/test_mcp_binding.py`

- `test_membership_rechecked_before_next_call_and_binding`（call）
- `test_non_admin_without_membership_cannot_read_another_admin_candidate`（call）

### `tests/modules/accounts/test_mcp_binding_revision.py`

- `test_migrated_json_checks_accept_legacy_and_validate_new_generation[material_distribution-ck_material_distribution_source_route]`（call）
- `test_upgrade_preserves_old_routes_and_replaces_single_bc_uniqueness`（call）
- `test_runtime_scope_migration_uses_only_valid_original_frozen_evidence`（call）

### `tests/modules/accounts/test_mcp_multibc_discovery.py`

- `test_publish_preserves_existing_default_and_write_authorization`（call）
- `test_runtime_scans_are_independent_and_preserve_other_bc_evidence`（call）

### `tests/modules/accounts/test_mcp_permissions.py`

- `test_mcp_capability_worker_uses_frozen_connection_and_keeps_write_unknown`（call）

### `tests/modules/accounts/test_router.py`

- `test_api_connection_sync_starts_one_full_directory_run`（call）

### `tests/modules/accounts/test_worker_route_authorization.py`

- `test_current_authority_fences_next_request[authorization-route_authorization_changed-OFFICIAL_MCP]`（call）

### `tests/modules/ad_management/test_execution.py`

- `test_management_queue_is_registered_with_one_slot`（call）

### `tests/modules/ad_management/test_submission_concurrency.py`

- `test_expired_preview_and_failed_outbox_leave_no_task`（call）

### `tests/modules/ads/test_directory.py`

- `test_remote_identity_upsert_ignores_channel_but_separates_account_and_kind`（call）

### `tests/modules/ads/test_directory_migration.py`

- `test_additive_upgrade_constraints_and_safe_downgrade`（call）
