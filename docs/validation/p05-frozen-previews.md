# P05 冻结预览验证

实现 `generate_preview/continue_preview/get_preview_summary/get_preview_units/load_frozen_unit/get_frozen_groups`，配套 7 个 HTTP 接口和持久任务恢复。迁移 `0005d_build_previews` 接在 Scene facts 之后，未改已提交迁移。

- 每剧先保存链接、完整素材分组及一次抽样的正文，再铺全部有效账户。全量输入行另存快照，未匹配与重复不构造虚假组合。
- 输入与素材每页最多 100 行；单步只处理一个分组；一次事务最多 200 步，20 秒软工作窗口。任务进度、抽样、批次标识与后继 outbox 同事务保存。
- 所有判断只读本地 Scene/material readiness。预览不会触发素材分发或广告创建。未知平台限额保留阻断原因；不把内部工程批量值当 TikTok 限额。
- 实际命名保存受保护前缀；按 Scene 各层长度规则检查。Campaign 名称用 SHA-256 索引查找并核对原文，长 CJK 名称不会导致 PostgreSQL B-tree 行尺寸错误。相同账户重名的所有组合明确排除。
- 统计只汇总 READY/PREPARING 的 Campaign、AdGroup、Ad 和每日预算。预算保持 Decimal，一部剧一个户只有一个 Campaign 预算。
- PostgreSQL trigger 在冻结后禁止子表修改；草稿 revision 变化使旧预览过期，已冻结意图继续可读取供已提交执行使用。提交资格由后续执行模块检查。
- 三级名字、素材 ID、正文 ID/文本、CTA 选项、账户连接、策略版本与小型场景事实都已固定，不读取后续策略默认值。

验证：`tests/modules/builds` 共 124 项通过，其中新增预览 15 项；覆盖 2 剧×3 户×3 组×2 SP=6/18/36、600 每日预算、局部阻断、文案跨户一致、分页/跨租户/viewer、冻结不可变、新素材不扩展、重复投递恢复与真实 PostgreSQL 并发生成/推进/编辑。Ruff、mypy、新增模块 Ty 通过，Alembic upgrade/check 无差异。

这是离线代码与数据库验证；真实 TikTok/S3/版权方未调用。Scene/OAuth 已独立审查通过；冻结预览独立审查待执行。
