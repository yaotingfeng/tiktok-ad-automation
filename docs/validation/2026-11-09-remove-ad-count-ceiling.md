# 2026-11-09 取消广告总量上限

用户在生产故障排查后明确要求取消 10,000 条广告总量限制。

## 问题与变更

- xuyin 的“阿门 测试”v1 为 20 组 × 20 基础广告 × 50 文案版本，单剧、单账户共 20,000 条。生产只读调用结构规划器复现 `configuration_invalid`，内部原因是策略展开广告数量超过本地安全上限。
- 删除 `MAX_TOTAL_PLANNED_ADS` 及单策略、跨剧跨账户整批两处总量拦截。预算和广告数完整计算，不截断、不减少文案，不修改租户策略数据。
- 保留固定组数和基础广告数各 100、单广告最多 50 创意单元、文案池容量、场景资格、权限、并发和调用限流。
- 无数据库迁移、配置/资源/队列合同变化；不包含分批生成重构，不代表任意规模容量或真实平台创建已通过。

## 验证

- 两项新行为先在旧代码失败，分别命中单策略和全批旧上限；修改后通过。
- 实际规划断言 20 × 20 × 50 共 20,000 条；跨剧跨账户整批试算断言 10,000 系列、20,000 广告组、120,000 广告及完整预算。
- 独立临时 PostgreSQL 测试库执行 `tests/modules/builds/test_structure_preview.py tests/modules/strategies`：139 passed、1 failed（9.79 秒）。包含结构试算接口返回 20,000 条成功。
- 唯一失败 `test_structure_replay_is_stable_for_sorted_materials_and_copies` 的夹具仅有 `material_id`、缺少当前规划器需要的 `unit_id`。在修改前 HEAD 的 schemas/structure 源码上复现相同错误，本轮未修改该无关测试。
- 四个修改的 Python 文件 Ruff check、格式检查通过；未调用真实广告创建接口。

## 发布状态

应用 `0549cd4278abc2a2f653a85dcb403966a5b2e42c` 已推送公司 main/GitHub 并完成生产发布。11 角色/9 Worker/唯一 Beat、精确镜像摘要、实际配置/四开关、schema0101 均验收通过；后台实际12/typed12/prefetch12、publisher8进程/CPU4与原资源及正常退出宽限保持，发布锁已释放。xuyin“阿门 测试”原配置只读试算与实际规划均为1系列/20广告组/20,000广告，日预算USD500，跨2剧×3账户为120,000广告；未修改策略或提交真实广告。无DDL、新备份或恢复演练。

镜像摘要：`sha256:81b2b5b5bbdf7dc5b297ecfdfbf0f8940298fce78898173cc79dd2b23fe07571`。私有发布证据位于 `.runtime/ad-count-ceiling-20261109/`；CI 已恢复原启用状态，无 active/queued 构建。
