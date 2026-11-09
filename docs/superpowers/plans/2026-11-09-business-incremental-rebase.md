# Business incremental rebase implementation plan

**Goal:** 来源刷新时仅重算真实变化尾部，避免反复复制已证明相同的季度前缀。

**Architecture:** projection 编排 VERIFY 与原发布不变量；prefix 比较冻结输入并有界清理；tasks 统一重型单例预算。无需迁移。

**Spec:** `docs/superpowers/specs/2026-11-09-business-incremental-rebase.md`

执行方法已由 root 指定：当前实现者直接开发，不派生代理、不提交、不操作生产。

- [x] 新真实 PG 测试复现末日刷新造成 SUPERSEDED，验证旧红。
- [x] 在 projection.py / prefix.py / tasks.py 实现 VERIFY/REBASE、严格结构与状态比较、500 行候选清理、18 秒重型单次预算。
- [x] 验证全量构建等价、旧 publication 保持、版本变化/覆盖资格/结构变化、重复变化、中断回滚与重放。
- [x] 使用 185k 真实状态测量整个 VERIFY 链，验证 18 秒与 2 GiB 上限。
- [x] 运行相关 business 回归、Ruff、严格 mypy；记录证据交 root 独立审查。

本地验证已完成，独立源审进行中；未提交/发布。详见 `docs/validation/2026-11-09-business-incremental-rebase.md`。
