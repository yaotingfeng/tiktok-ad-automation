# Provider Ad Naming Implementation Plan

> **For agentic workers:** 按当前用户的按需技能偏好，在本会话顺序执行以下检查项；不另行启动完整工作流或创建分支。

**Goal:** 提供版权方通用命名模板，保留网眼归因规则，用数据库约束支持的 12 位随机号替代新预览的 UUID 名称后缀。

**Architecture:** 策略 JSON 增加完整模板，专用规则继续使用独立后缀。预览冻结版权方规范标识和外部剧目 ID，并在保存预览时用 PostgreSQL 唯一约束处理随机号冲突。

**Tech Stack:** FastAPI、SQLModel、PostgreSQL、Alembic、React、shadcn/ui、Playwright。

**Spec:** [功能设计](../specs/2026-09-11-provider-ad-naming.md)

## Global Constraints

- 默认模板 `{provider_pinyin}-{drama_name}-{drama_id}-{random}`，特殊规则优先；批次号 12 位数字，最多重试 10 次。
- 不修改已冻结广告名称，不覆盖版权方归因前缀，不更换币种或触发真实投放。
- 当前分支实施，仅显式暂存本轮文件；保留 `docs/design-history/`。

### Task 1: 命名契约与服务

**Files:** `backend/app/modules/strategies/{schemas,naming,service}.py`、`backend/tests/modules/strategies/test_naming.py`。

**Interfaces:** `StrategyConfig.campaign_name_template: str`；`validate_name_template(template: str)`；`render_names` 新增 `provider_pinyin`、`external_drama_id` 和 `template` 参数。

- [x] 用以下断言覆盖通用、特殊、非法变量及长度：
  ```python
  assert campaign == "jiashu-The Bond-106001-123456789012"
  assert group == campaign + "-g01"
  assert ad == group + "-sp2"
  ```
- [x] 新增严格变量校验：仅允许规范变量，禁止属性、下标、格式转换、控制字符；普通模板必须包含 `drama_id` 与 `random`，特殊后缀必须包含 `batch_short_id`。
- [x] `protected_base` 非空时仅执行专用后缀；否则渲染普通模板，插入值作为字面文本，不二次解释剧名中的花括号。

### Task 2: 冻结上下文与短号

**Files:** `backend/app/modules/builds/{preview_models,previews}.py`、新增 `backend/app/modules/builds/batch_numbers.py`、新增迁移 `backend/app/alembic/versions/0017_preview_naming.py`、预览和迁移回归。

**Interfaces:** `PreviewDrama.provider_pinyin`、`PreviewDrama.external_drama_id`；`insert_preview_with_number(session, row)` 在保存点内分配并写入唯一编号，不提交调用方事务。

- [x] 增加两个默认空字符串的快照列，旧冻结行无需数据回填；从同租户的版权方连接和剧目读取新快照。
- [x] 生成 `f"{secrets.randbelow(10**12):012d}"`；仅捕获 `uq_preview_batch_short` 的冲突，回滚保存点并重试；其他完整性异常原样抛出。
- [x] 新预览在分配成功后计算摘要并投递队列；相同草稿版本返回原预览及编号。旧版未冻结预览返回明确失效状态。
- [x] 在独立 PostgreSQL 测试库强制产生重复编号，验证重试、全冲突失败后事务可继续、其他错误不被吞、恢复编号不变和快照不受后续版权方名称变更影响。
- [x] 在临时迁移库验证从 `r2_part_receipts` 升级、旧冻结名称和摘要保留，以及空库到 head。

### Task 3: 策略页面与交付

**Files:** `frontend/src/features/strategies/{StrategyForm,StrategyNamingExample,validation}.tsx/ts`、生成的 API 客户端、`frontend/tests/strategies.spec.ts`、实施进度。

**Interfaces:** 通用模板编辑框、可点击插入的命名变量、特殊规则后缀编辑框和双示例；表单指纹和保存请求包含新模板。

- [x] 用 `scripts/generate-client.sh` 从当前 OpenAPI 生成客户端；不手写生成类型。
- [x] 表单默认通用模板；错误分别定位通用模板或特殊后缀；只读、版本冲突和恢复保存逻辑纳入新字段。
- [x] 页面示例普通规则为 `jiashu-The Bond-106001-123456789012`，特殊规则仍为 `{b30008/s328302/c3}-The Bond-20260908-123456789012`，展示继承的组号和 SP 编号。
- [x] 策略与搭建后端相关回归、真实 PostgreSQL 短号测试、迁移测试、策略页面 Playwright、TypeScript/Vite、改动文件静态检查通过。
- [x] 记录检查结果、迁移及发布边界，检查暂存差异后聚焦提交。


## 验证与交付记录

- 真实 PostgreSQL 与 Redis 在独立本地测试库执行；迁移测试另建临时历史库。保留本地开发库和生产库。
- 策略与搭建回归按主模块和批量展开组分别运行；批量展开夹具需显式固定其模拟的 S3 类型和区域，避免继承开发机 R2 配置。主组 461 项及批量展开 8 项通过，1 项 Linux 专用测试跳过；结果记录于 `docs/implementation-progress.md`。
- 浏览器回归：策略 36 项、预览 26 项；TypeScript/Vite、Ruff/Biome、mypy、ty、Alembic check 通过。
