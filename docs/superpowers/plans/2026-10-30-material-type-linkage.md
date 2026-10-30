# 按素材类型完成报表关联

Goal: 修复轮播已有广告归属却永久等待视频 VID 的循环；保留真实的视频、归属和图片证据缺口。

Architecture: 共用类型感知的关联完成 SQL，用于任务准入、等待、目录补读、健康检查和概览范围。复用现有 MaterialPostLink 与目录合同，不改表结构、金额事实、授权范围或并发配置。

Tech stack: Python / SQLAlchemy / PostgreSQL / Celery，TikTok 官方 SDK。

Spec: 用户已确认的类型区分方案及 `docs/validation/2026-10-30-sync-material-type-root-cause.md`。

## 官方文档核验（2026-10-30）

- [Creative Breakdown](https://business-api.tiktok.com/portal/docs/run-an-upgraded-smart-creative-breakdown-report/v1.3)：`VIDEO_SPARK_ADS` 和 `CAROUSEL_SPARK_ADS` 的主素材是帖子 ID；`CAROUSEL_NON_SPARK_ADS` 是轮播 ID。不能把这些 ID 无差别视为视频 VID。
- [Creative Overview](https://business-api.tiktok.com/portal/docs/run-an-upgraded-smart-creative-overview-report/v1.3)：支持广告与主素材联合维度；分页上限 100。按主素材过滤还需同时限定单一素材类型。此次不增加接口或改变现有分页。
- [Breakdown dimensions](https://business-api.tiktok.com/portal/docs?id=1843337879988226)：拆分支持主素材与时间维度；广告归属沿用概览及按广告筛选的拆分查询。

由文档和已有生产证据得出的实现规则：轮播归属完整不等于图片内容完整，当前广告图片组不能充当历史帖子的图片身份证明。目录已有图片序列、音乐和远端创意 ID 合同继续保留，不造历史绑定。

## Global Constraints

- 视频只有同租户、同账户、同素材类型的有效 VID 才完成；轮播须有同范围、同类型、非空广告归属。
- 未支持类型不能仅因非视频而标完整；未知归属继续补查、按已有负缓存与退避等待。
- 新旧任务使用同一条件，健康检查不得重造已完成轮播任务。目录常规刷新独立保留，轮播图片不足由既有 `carousel_linkage_complete` 如实表达，不走视频补链循环。
- 不将图片 ID 写入视频字段，不复制或摊分金额，不新增线上 API 请求。
- 独立 worktree，针对性真实 PostgreSQL 测试，最终审查后合并本地主线，不自动推送。

### Task 1: 类型感知关联与回归

Interfaces: 共用关联条件连接任务、目录、健康检查；复用任务 JSON 字段，定位续点以 ID+类型为键。缺少类型证据的旧定位缓存/已检查列表作废并重查，保留已提交映射、冻结概览分页和有效的新版本续点。

1. 写轮播完成、缺归属、混合视频、旧等待任务恢复、跨类型隔离回归，运行并观察失败。
2. 实现共用条件；只给视频请求补链目录；保持图片完整性独立。
3. 运行新用例及既有补链、健康、金额和轮播目录测试；检查查询计划和边界。
4. 更新实施记录，聚焦提交，独立审查后合并。

Expected: 已有归属轮播无 VID 也能完成且不再生成目录补查；视频缺 VID 仍等待；未知归属仍补链；当前图片不会被写成历史帖子的视频；财务值与冻结范围不变。

## Review Focus

核查类型条件是否遗漏任务入口或恢复路径；是否误把非轮播类型放行；类型不一致的历史映射是否遮住缺口；混合任务是否继续重读已完整轮播；图片缺口与关联完成是否混淆；租户、账户、BC、路由和金额是否保持原合同。特别审查同一数字 ID 跨类型及旧等待续点的行为。

## 完成记录

独立审查发现历史定位器仍丢失类型，已用四项失败反例复现后修复：同号不同类型回执、旧 ID-only 续点、同号双类型独立目标、缓存保留回执类型。最终记录见 [验收](../../validation/2026-10-30-typed-material-linkage.md)。无数据库迁移，但任务搜索缓存语义发生变化，后续发布必须统一相关角色版本，不能让新旧消费者混用定位续点。
