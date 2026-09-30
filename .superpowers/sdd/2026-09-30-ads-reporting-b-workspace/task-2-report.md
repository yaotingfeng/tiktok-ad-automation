# B2 实施记录（复审修复轮）

## 决策与范围

- 事实读取继续通过 B1 `require_tenant()`、选定 BC 的 `authorized_grants()`，并在每条查询中显式绑定 tenant 与 advertiser 列；未因目录状态过滤历史事实，暂停/删除账户仍保留历史投放金额。
- 六维聚合只在相同币种、账户本地时区、归因和指标可用性桶内以 `Decimal` 求和。DAY/RANGE/HOUR 先按兼容粒度和请求版本选择不重叠桶；`ReportCoverage.COMPLETE_EMPTY` 只覆盖其明确目标，缺失、FAILED、部分覆盖分别保留状态，不伪造零。D0 ROAS 使用汇总 D0 收入除以汇总 spend，零或不可用 spend 返回空值并保留对应状态。
- drama 行和趋势均使用 `BC + provider + 第二段剧名` 作为有效系列身份；同 BC、同 provider、同剧名的授权账户合并并保留全部 campaign refs，不同 provider 分开；无效/缺失投影的外部 campaign 仍可见。
- 趋势保留同一桶的新旧观测，按同日相邻桶或同桶修正计算 Decimal 差值，保留负修正；day/hour 请求只消费对应 DAY/HOUR 粒度，避免混合粒度重复点。成员摘要、归组/指标口径不兼容时返回 `SCOPE_CHANGED`，日期或桶不连续时返回 `DATE_CHANGED`。drama 趋势先读取当前授权范围内最新 `CampaignNameProjection`，再将 campaign 观测重投影和聚合。
- A1 素材 subject 已统一为五段 `(material, grouping_dimension, grouping_value, main_material_id, main_material_type)`；SDK 适配器、facts 校验和发布链不再接受四段回退。素材聚合只使用实际 `AdMaterialReference.use_ref` 与事实明确广告身份证明，无法证明时显式 `UNSUPPORTED`，不按 VID/name 合并或复制父广告金额。素材合同没有 D0 收入时，公开向量仍显式返回 D0=`UNSUPPORTED`。
- 成功的 account/campaign facts 发布在同一事务追加 `ReportObservation`；完整空结果不写零观测，失败/不完整发布不写可用观测，重复发布按租户、账户、subject、桶和版本幂等。预算模式、`roas_bid`/目标 ROAS、`create_time` 和 D0 ROAS 筛选由聚合层明确应用；`ad_types`、命名状态、ID/搜索、spend 和排序不会静默失效。

## 修改文件

- `app/modules/reporting/aggregation.py`
- `app/modules/reporting/trends.py`
- `app/modules/reporting/facts.py`
- `app/modules/reporting/filters.py`
- `app/modules/reporting/schemas.py`
- `app/integrations/tiktok/adapters/sdk_reporting.py`
- `tests/modules/reporting/test_aggregation.py`
- `tests/modules/reporting/test_trends.py`
- `tests/modules/reporting/test_filters.py`
- `tests/integrations/tiktok/test_reporting_adapters.py`

## 验证

测试使用专用 PostgreSQL/Redis 环境（从 `test.env` 加载，未打印凭据），未调用 TikTok/MCP、广告写入或部署：

```text
uv run --frozen pytest tests/modules/reporting/test_trends.py tests/modules/reporting/test_aggregation.py tests/modules/reporting/test_filters.py tests/integrations/tiktok/test_reporting_adapters.py -q  # 38 passed
uv run --frozen pytest tests/modules/reporting tests/integrations/tiktok/test_reporting_adapters.py -q  # 72 passed
uv run --frozen ruff check app/modules/reporting/trends.py app/modules/reporting/filters.py app/modules/reporting/schemas.py
```

新增回归覆盖同桶负修正、五段素材身份、素材 D0 显式 unsupported、筛选配置字段与 D0 阈值；既有回归覆盖汇总 ROAS、零/缺失/unsupported、混桶隔离、素材 use_ref 不重复计数、外部 campaign、租户/BC 授权边界。

## 关注事项

- `ReportObservation` 仅接受 account/campaign subject；adgroup/ad/material 趋势仍返回明确 unsupported/incomplete，而不是从事实临时拼历史点。
- 观测 membership digest 保持同一发布桶可比较；最新名称投影的 grouping revision 变化会阻止错误增量并标记 scope 变化。

## 复审修复轮 2

- 素材证明现在要求每个事实/指标行都有唯一 typed ad identity，并与唯一完整
  `AdMaterialReference.use_ref`（含存在时的 `ad_material_id`）匹配；部分映射的事实
  只保留显式 INCOMPLETE/UNSUPPORTED 覆盖，不返回 use_ref 或 COMPLETE 金额。
- 生产观测的 digest 改为当前账户完整目录及 provider/剧名投影成员集合的稳定排序摘要。
  目录或投影不完整时不追加可用观测；成功发布仍在原事务内幂等追加。
- 趋势日期窗口按每条 observation 的本地时区计算，mixed timezone 与 DAY/HOUR 粒度分开；
  query、ID、目录状态/命名、预算/创建/目标 ROAS 和 spend/D0 ROAS 过滤统一应用。
- 本轮专用环境新增 material 部分证明、目录成员 digest、非 UTC 日期和趋势共享筛选回归；
  reporting + adapter 全集 `uv run --frozen pytest tests/modules/reporting tests/integrations/tiktok/test_reporting_adapters.py -q`
  通过 76 项；Ruff 与 `git diff --check` 通过。
