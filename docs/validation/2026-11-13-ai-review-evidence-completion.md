# AI 复盘事实补齐验收

分支：`codex/ai-ad-workbench-v1`，日期：2026-11-13。承接同日全分支上线审查中的事实缺口。TK-ADA 继续拥有业务事实、计算和授权；插件拥有方法，DSH 运行会话。

## 交付行为

1. **真实回读**：规则动作 ACCEPTED 后幂等排入既有只读核查；保存真实对象值、期望值、观察时间和平台回执。冻结 `execution_observations` 并在后续建议反馈中区分 VERIFIED、OBSERVED_DIFFERENT、READBACK_REQUIRED、FAILED、UNKNOWN。目录刷新不能覆盖真实回读；受理成功不等于执行已验证。
2. **成熟窗口**：每账户当地 D0/D3/D7 分别查七个完整成熟日。D7 为当地今日前 8–14 日，复用现有每日 32 日回刷。逐日保存覆盖、来源版本、成熟后观测资格和实际指标字段；缺少收入、过早观测、未回刷、平台不支持均明确降级，素材 D3 不支持，不用总收入替代。
3. **规则历史事实**：执行时保存全部候选对象（含未命中）、父子关系、属性及原始 range→entity→metric 指标、阈值、跳过原因和输入摘要。`historical_replay` 是有界模型视图；原始完整快照保留在规则证据表。
4. **Agent 只读回测**：`tkada_backtest_rule` 经任务 JWT/fence/配置/范围校验，只对当前任务冻结的源证据及摘要重放候选条件，归档可引用 ID。结果表示同样本的新增/移除命中，不能证明误关、实际节省或因果收益。无原窗口/指标或旧历史缺失时拒绝完整回测；重试幂等、每轮十个候选且有归档字节预算。
5. **策略及素材关联**：真实发生记录→submission/unit→成功 Campaign ID→正式报表，关联策略版本。素材则沿已鉴权生产来源→同租户真实订单/job/制作策略→入库文件→验证视频 ID→素材报表关联。多个来源冲突时不强行归属；手工广告仍参与账户/规则分析。
6. **订单兑现**：冻结订单策略、job、排期、状态、失败原因和生产结果，隐藏回写凭据及签名链接；不把订单完成当作素材已用于广告。修正真实订单的 `request_body.payload` 合同，测试通过真实 `create_order()` 生成数据。
7. **实际进入 Agent**：上述数据进入持久化复盘证据；插件 1.3.0 主方法及规则专项声明读取位置、资格与结论限制。回测结果工具返回正文并归档；引用可用于完成接口中的建议，完成后令牌不能继续读取。

## 本地验证

- 使用真实 PostgreSQL，跨连接行为使用真实事务；Redis 使用独立测试进程（原测试 Redis 出现 MISCONF，未改动业务 Redis）。外部平台/生产接口仅在 HTTP/传输边界使用替身。
- 新增验证覆盖：受理≠真实观察、未知/差异/失败、观察截止、租户/BC/账户/优化师隔离、成熟 D7、过早观测、缺收入、规则未命中事实及历史冻结、候选阈值重放、任务工具归档与引用、真实搭建 ID 与手工广告区分、签名生产谱系持久化和错误 job 原子拒绝。
- 修复真实优化师规则范围解析的错误连接模型导入路径；自动规则测试查询改为按本用例租户断言，避免独立事务集成夹具残留影响其他用例。
- 最终全新隔离数据库回归：**732 passed, 1 deselected, 77 warnings**。DSH 原生插件 **7 passed**；Ruff/格式（本轮 31 个 Python 文件）、Node 语法和差异检查通过。警告含既有 rule/run 双向外键排序及上游弃用提示。
- 后端验证命令：`PYTHONPATH=backend .venv/bin/python -m pytest backend/tests/modules/automation_rules backend/tests/modules/ai_workbench backend/tests/modules/ad_management backend/tests/modules/materials/test_push.py backend/tests/modules/materials/test_production_push.py backend/tests/acceptance/test_ai_workbench_flow.py backend/tests/modules/builds/test_ai_workbench_handoff.py -k "not test_direct_10748_submission_does_not_expand_directory" -q`，显式指定隔离 PostgreSQL/Redis。
- 插件验证：`DSH_TEST_PACKAGE_ROOT=<本机 DSH 宿主根目录> node --test integrations/dsh-ai-workbench/*.test.mjs`。本轮无前端产品代码变更，不重复此前页面/构建验收。
- 扩展广告管理套件有一个既有 SQL 数量门槛用例：10748 对象提交执行 26 条 SQL，期望 ≤25。本轮改动前 `b2f1cbad` 的 backend 归档独立重跑同样失败（仅附当前 migration 文件以识别测试库 head）；该提交路径本轮未改。最终回归明确排除此用例，不把它报告为通过。

## 发布和外部边界

新增迁移 `0128_material_production_origin`，API/Worker/Beat 和插件 1.3.0 配套更新。取消并重建旧格式待处理复盘；已完成历史保留。

素材制作方需在已签名回传中携带 `production_origin={order_id,job_id,strategy_id}`；TK-ADA 接收、校验、归档和读取已验证，未知外部生产工具未改动/联调。旧历史无谱系不可补造，旧规则无判断事实不可恢复回测。账户需有授权且既有同步/成熟回刷正常运行，代码无法生成尚未同步的真实数据。

未配置或验证真实模型业务质量，未进行生产部署、真实广告写入或真实素材下单。上述本地通过不替代外部环境验收，也不代表自动经营收益已验证。
