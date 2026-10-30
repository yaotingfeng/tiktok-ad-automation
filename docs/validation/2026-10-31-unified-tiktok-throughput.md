# 2026-10-31 统一TikTok配额与后台批量管理本地验收

## 范围与环境

用户批准统一优化创建与管理配额、直接后台提交、平台合批及最多50请求并发。当前main原有其他会话提交和四个未追踪前端构建文件全部保留。不调用真实TikTok广告写入，不执行浏览器原选择，不修改线上配置、不推送、不发布。

本地Python 3.14项目`.venv`，真实PostgreSQL数据库`tkada_final_bulk_test_20261031`和Redis DB13；平台响应仅在传输边界替身。前端使用独立5198端口，避免复用其他会话服务。生产SDK应用档位仍未核实，本地Basic只是明确假设。

## 行为证据

- 10748对象直接提交仅冻结意图、完整选择和路由，不读取广告目录；后台200成员检查点，选择过期/删除后可继续，百分比不重复计算。
- SDK创建、管理、素材、目录及报表按实际物理接口配额；别名共桶、不同接口独立，原子QPS/滚动分钟/UTC日与平台共享冷却，不靠新token/BC增加同应用额度。
- 状态20/20/1、素材同广告20、预算生效语义、Smart+ ROAS按父系列去重；缺失/重复/矛盾成员回执UNKNOWN且不自动重发。
- 两个任务共享同一请求池，3与50窗口实测峰值匹配；Redis资源限制与平台速率单独控制。网络调用不持SQL事务，SDK客户端不跨并发请求共享last_response。
- 事务内保存请求领取/重试/准备检查点和持久恢复消息；锁竞争和本地配额等待不会耗尽传输重试预算。
- UNKNOWN通过原路由、明确ID和新鲜状态只读核查；观察匹配也保留“受理仍未知”，不把当前状态归因为本次请求成功。

## 已完成检查

后端命令（backend目录，设置上述DATABASE_URL及TEST_REDIS_URL）：

```bash
../.venv/bin/python -m pytest \
  tests/integrations/tiktok/test_platform_quotas.py \
  tests/integrations/tiktok/test_management_adapters.py \
  tests/integrations/tiktok/test_management_batches.py \
  tests/integrations/tiktok/test_management_sdk_rejections.py \
  tests/integrations/tiktok/test_build_quota.py \
  tests/modules/builds/test_sdk_contract.py \
  tests/modules/ad_management -q
```

本次基线357项通过；随后ROAS冲突仅隔离当前系列及future收齐回归连同合批/并发9项通过。取消/Redis恢复修复后，管理全套196项通过（含新5例取消/失联恢复与1例ROAS系列隔离），最终独立只读复审通过。

前端`PLAYWRIGHT_PORT=5198 bun x playwright test tests/ad-management.spec.ts --project=workspace --workers=2 --reporter=line`：18项通过，包括直接提交、不再预览/确认、幂等重试、准备失败中文原因、明细分页及UNKNOWN异步核查。`bun run build`：TypeScript、正式与演示构建通过。

全新本地数据库`tkada_bulk_migration_verify_20261031`完成从空库升head、0069→0067→0069往返；`alembic check`返回`No new upgrade operations detected`。未对生产运行迁移，不将空库往返作为生产旧任务迁移验收。

## 审阅与发布边界

独立审阅覆盖租户/账户/路由代数、事务恢复、未知结果、取消、共享额度及SDK拒绝解析。已按发现补齐回归；最终确认Task-first锁阻止取消被覆盖、取消后过期claim转UNKNOWN只读核查、活跃claim保留唤醒、RedisError自动退避。未发现剩余Critical/Important。

发布需统一审阅0068/0069任务合同迁移、确认实际SDK应用档位及MCP独立额度、选定资源在途预算与50窗口容量，保留精确版本和正常退出。生产尚保留原配置，本地通过不代表线上已提速。具体操作依据[运行说明](../runbooks/tiktok-throughput.md)。


## 收尾记录

- 取消最终专项61项及管理全套196项通过；新的取消交错、活跃/过期claim、单项恢复、Redis broker retry共5例。
- 扩展既有配额/账户/创建准入回归发现严格解析boolean code时遗漏安全request_id，保留UNKNOWN语义并修复证据提取；创建相关专项46项通过（45项按筛选未执行），包括原失败boolean_code用例；扩展suite其余159例已通过。
- 演示操作注册与引导同步更新，demo:coverage为240/240；`bun scripts/test-demo-management.ts`验证准备进度、幂等、租户/BC隔离、分页、取消及错误行保留通过。TypeScript/Biome通过；未运行完整浏览器演示引导，不记为通过。
- 正式/演示前端、迁移模型、ruff与差异空白检查通过；构建产物及本地数据库不提交。
