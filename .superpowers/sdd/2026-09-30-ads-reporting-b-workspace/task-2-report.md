# B2 实施记录

## 决策

- `aggregate_metrics()` 只接受相同币种、时区、归因和目标口径，使用
  `Decimal` 汇总 `spend` 与 `native_growth_ad_revenue_value_d0`，再按汇总值计算
  `d0_roas`。消耗为零或不可用时 ROAS 为 `None`；零、MISSING、UNSUPPORTED、
  FAILED 保持各自语义。
- 六维事实读取先执行 `require_tenant()`，再使用所选 BC 的
  `authorized_grants()` 约束显式 `tenant_id` 和 advertiser。暂停/删除对象的已发布
  事实仍可见；目录缺失时保留事实身份行。
- drama 维度读取最新 `CampaignNameProjection`，以 provider + 第二段剧名归组；
  provider 不同的同名剧分开，缺失或无效投影的 campaign 以 external 行保留。
- material 维度仅接受能通过 `AdMaterialReference.use_ref`（以及事实中明确的
  `ad_id`/`ad_material_id`）证明的广告级使用事实。VID/name 不去重，不复制父广告
  金额；无法证明时返回 UNSUPPORTED 覆盖。
- 趋势只消费与已发布 `ReportFact` 对应的 `ReportObservation`。事实存在但观测
  缺失返回 INCOMPLETE；事实也不存在返回 UNSUPPORTED。成员摘要/分组修订变化
  返回 `SCOPE_CHANGED`，桶日期断开返回 `DATE_CHANGED`，负差值原样保留。

## 共享契约修复

`schemas.py` 增加了 B2 接口所需的 `TrendPoint` 和 `TrendPublic` DTO；这是 brief
中“TrendPublic 定义在 schemas”但当前 B1 文件尚不存在的最小契约补齐。未增加
优化目标、预算或旧 `d0_revenue`/`VALUE` 字段。

## 验证

使用本地 PostgreSQL 17 的专用 `tiktok_b2_test_20260930` 数据库和独立 Redis 测试
库运行：

```text
uv run --frozen ruff check app/modules/reporting/aggregation.py app/modules/reporting/trends.py app/modules/reporting/schemas.py tests/modules/reporting/test_aggregation.py tests/modules/reporting/test_trends.py
uv run --frozen pytest tests/modules/reporting/test_aggregation.py tests/modules/reporting/test_trends.py -q  # 7 passed
uv run --frozen pytest tests/modules/reporting -q  # 53 passed
```

未调用 TikTok/MCP、广告写入或部署操作。

## 关注事项

生产 `publish_report()` 仍未自动追加 `ReportObservation`；趋势实现因此明确报告
不完整/不支持状态，不从事实历史臆造点。A1 素材五段 subject 迁移及观测发布仍由
上游 owner/root 按简报处理。
