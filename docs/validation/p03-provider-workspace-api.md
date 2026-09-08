# P03 版权方工作区补充接口

所有路径前缀为 `/api/tenants/{tenant_id}/providers`。查询只读取本地已记录事实，需要租户 read 权限，与顶栏 BC 无关，不触发外部登录、建链或回查。

| 路径 | 契约 |
| --- | --- |
| `GET /links` | Page[ProviderLinkPublic]；connection_id、application_id、query、status、cursor、limit=50或100；query字面包含剧名或精确外部剧目ID |
| `GET /links/{link_id}` | 同DTO的完整记录；含原始URL、受保护归因名、版本、验证时间、版权方/应用身份和已知业务配置 |
| `GET /link-preparations/{task_id}/summary` | PreparationSummary：任务范围、配置、total_count/ready_count/pending_count/exception_count/counts；SQL聚合所有输入行，无全量明细下载 |
| `GET /link-preparations/{task_id}` | 既有结果页新增status与exceptions_only；内部解析/查询/创建/验证均属于公开pending，异常不含pending/ready；游标绑定筛选 |
| `GET /connections` | 既有分页新增query（连接名称字面包含）、kind、status；游标绑定筛选 |

历史页展示 PromotionLink 记录，URL携带task_id时展示本次结果；不新增准备任务列表与伪造创建时间。ProviderLinkPublic包括link_id/drama_id/external_drama_id/title/language/provider_kind/connection_id/connection_name/connection_status/application_id/application_name/tiktok_minis_id/status/version/url/protected_base/verified_at/config/config_display_incomplete。

ResolvedLink冲突行新增existing_config/requested_config。仅公开已知标量业务设置（episode、charge_level、channel_prefix、chapter_index）；drama_num以episode显示，任意原始响应和内部_work不出接口。协议校验后已取得的配置可展示差异，未可靠取得则existing_config为null，界面显示尚未核实；不能凭空补齐。历史与摘要的config_display_incomplete表示未完整展示原配置。

没有新增普通重试未知写入的接口；当前工作流已自动安排合法回查，页面只读取核实进度。候选消歧和显式连接验证仍使用已实现的写入口。

验证：新增3项真实PostgreSQL/API测试覆盖205条历史分页、101条异常分页、205条SQL汇总、筛选游标、跨租户、配置字段安全与连接筛选；既有真实协议替身冲突回归补验已知episode差异。根providers整组158项通过（补充冲突断言另行定向通过后提交），素材API注册及素材68项回归通过，mypy/ty/Ruff通过。所有外部调用均为测试替身，没有真实版权方或TikTok业务调用。
