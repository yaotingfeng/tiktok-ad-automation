# Provider Expansion Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 接入兑吧、刚刚好、容量三种版权方连接，使其可在 TK-ADA 中发现应用、解析剧目、幂等准备 TikTok Minis 推广链接并进入投放搭建。

**Architecture:** 将三个 Node CLI 的已验证 HTTP 契约移植为后端 Python adapter；运行时只使用租户加密凭据和统一 durable workflow，不启动 CLI。先把网眼/嘉书包装到同一 adapter/flow 接口，再用能力 schema 驱动连接表单和投放页的 provider-specific link config。

**Tech Stack:** Python 3.14、FastAPI、SQLModel/Alembic、httpx、Celery、PostgreSQL、React/TypeScript、TanStack Query、shadcn/ui、pytest、Playwright。

**Spec:** `docs/superpowers/specs/2026-10-02-provider-expansion-design.md`

## Global Constraints

- 不调用 Node CLI、不读取 `~/.duiba-link-accounts.json`、`~/.gghys-portal-accounts.json` 或 `~/.rongliang-link-accounts.json`。
- 凭据和会话只写租户加密凭据；日志、API 响应、`channel_config` 不得包含密码、token、cookie。
- `other` 继续是手动版权方内部 kind；自动连接只允许 `wangyan`、`jiashu`、`duiba`、`gangganhao`、`rongliang`。
- 未知外部写入不得盲目重放；只有 adapter 声明并证明请求幂等时才能按相同 effect 恢复。
- 真实版权方读写和真实 TikTok 广告操作必须分别验收，并需要具体授权。

## Review Focus

- 兑吧 `/link/page` 持续 500 时，固定请求能安全恢复，非幂等或参数变化不能重放；测试归入 Task 3。
- 刚刚好 IAP/mixed 没有支付模板时，任务应阻塞且不创建链接；测试归入 Task 4。
- 容量创建成功但列表/URL 回查缺失时，任务保持 `result_unknown`，不能取列表第一条冒充结果；测试归入 Task 5。
- 应用 ID、版权方剧目 ID、TikTok Minis ID 和归因名不能混淆；测试归入 Task 2 和 Task 6。
- 新 kind 的凭据校验、会话刷新和租户隔离不能回退到另一账号；测试归入 Task 1 和 Task 7。

### Task 1: 扩展稳定类型、凭据和连接生命周期

**Files:**
- Modify: `backend/app/modules/providers/models.py`, `schemas.py`, `connections.py`, `session_refresh.py`, `router.py`
- Create: `backend/app/alembic/versions/<date>_provider_kinds.py`
- Modify: `backend/tests/modules/providers/test_connection_isolation.py`, `test_auto_relogin.py`

**Interfaces:**
- `ProviderKind = Literal["wangyan", "jiashu", "duiba", "gangganhao", "rongliang"]`
- `credential_fields(kind: str) -> frozenset[str]`
- `adapter_for_kind(kind: str, http: httpx.Client, credentials: dict[str, str], application_id: str = "") -> ProviderClient`

- [x] **Step 1: Write failing tests** for all five automatic kinds, exact credential fields, invalid fields, and migration acceptance; assert `other` remains manual-only.
- [x] **Step 2: Run** `uv run pytest tests/modules/providers/test_connection_isolation.py tests/modules/providers/test_auto_relogin.py -q`; expect new-kind validation failures.
- [x] **Step 3: Add migration and dispatch registry.** Extend database checks without editing historical migrations. Route login, application discovery, session opening and refresh through the registry; keep per-provider token/session field names private.
- [x] **Step 4: Run tests** and `uv run ruff check app/modules/providers` (or the repository’s configured equivalent); expect existing two providers unchanged and new fixtures accepted.
- [x] **Step 5: Commit** `providers: add stable connection kinds for new copyright sources`.

### Task 2: Define normalized adapter contract and capability schema

**Files:**
- Modify: `backend/app/modules/providers/adapters/contract.py`, `link_steps.py`, `drama_identity.py`, `schemas.py`
- Create: `backend/app/modules/providers/adapters/registry.py`, `capabilities.py`
- Modify: `backend/tests/modules/providers/test_provider_protocols.py`, `test_contracts.py`

**Interfaces:**
- `ProviderAdapter.search(title: str, cursor: str | None) -> SearchPage`
- `ProviderAdapter.lookup_link(drama_id: str, config: dict[str, Any], cursor: str | None) -> LinkLookupPage`
- `ProviderAdapter.create_link(drama_id: str, config: dict[str, Any]) -> LinkReceipt`
- `ProviderAdapter.read_link(remote_id: str) -> LinkReceipt`
- `ProviderAdapter.verify_link(receipt: LinkReceipt, drama: DramaCandidate, config: dict[str, Any]) -> VerifiedLink`
- `ProviderAdapter.capabilities(application: ProviderApplication) -> LinkConfigSchema`

- [x] **Step 1: Write contract tests** for cursor completeness, normalized candidate identity, URL validation, empty-vs-missing `protected_base`, and capability field validation.
- [x] **Step 2: Run** `uv run pytest tests/modules/providers/test_provider_protocols.py tests/modules/providers/test_contracts.py -q`; expect missing protocol types.
- [x] **Step 3: Implement registry and generic verification.** Move hard-coded Jiashu/Wangyan verification behind the adapter interface while retaining the existing durable effect and remote-scope tables.
- [x] **Step 4: Verify** existing Jiashu/Wangyan fixtures and add display-ID rules for numeric Wangyan, opaque new IDs, and Capacities’ compilation IDs.
- [x] **Step 5: Commit** `providers: normalize adapter and link capability contracts`.

### Task 3: Port the 兑吧 adapter

**Files:**
- Create: `backend/app/modules/providers/adapters/duiba.py`
- Create: `backend/tests/modules/providers/fixtures/duiba-contract.json`, `test_duiba_protocol.py`
- Modify: `backend/app/modules/providers/link_steps.py`, `backend/tests/modules/providers/test_link_recovery.py`

**Interfaces:**
- `DuibaClient.login(http, account, password) -> DuibaClient`
- `DuibaClient.discover_applications() -> list[ApplicationDescriptor]`
- `DuibaClient.search(title, cursor)`, `lookup_link(...)`, `create_link(...)`, `read_link(...)`

- [x] **Step 1: Write fixture tests** for JWT login, app discovery, exact title candidates, episode/Lc ID mapping, link creation response, and URL/attribution normalization.
- [x] **Step 2: Run** `uv run pytest tests/modules/providers/test_duiba_protocol.py -q`; expect no adapter.
- [x] **Step 3: Implement** `/auth/login`, `/drama/page`, `/drama/preview`, `/drama/copy-miniapps`, `/miniapp/myList`, `/link/create` and guarded `/link/page` handling with the project `request_json` error mapping.
- [x] **Step 4: Add safe-idempotent recovery.** If `/link/page` is unavailable, allow a same-digest create recovery only through an explicit adapter capability; parameter changes and unverified responses remain `result_unknown`.
- [x] **Step 5: Run** protocol plus recovery tests; commit `providers: add Duiba adapter`.

### Task 4: Port the 刚刚好 adapter and payment-template validation

**Files:**
- Create: `backend/app/modules/providers/adapters/gangganhao.py`
- Create: `backend/tests/modules/providers/fixtures/gangganhao-contract.json`, `test_gangganhao_protocol.py`
- Modify: `backend/app/modules/providers/capabilities.py`, `backend/app/modules/providers/link_steps.py`

**Interfaces:**
- `GangganhaoClient.login(http, portal_id, username, password) -> GangganhaoClient`
- `GangganhaoClient.discover_applications() -> list[ApplicationDescriptor]`
- `GangganhaoClient.search(title, cursor)`, `lookup_link(...)`, `create_link(...)`, `read_link(...)`

- [x] **Step 1: Write tests** for portal JWT login, `authorizerAppId` mapping, `publishId` identity, list/detail reuse matching, and IAA vs IAP/mixed template requirements.
- [x] **Step 2: Run** `uv run pytest tests/modules/providers/test_gangganhao_protocol.py -q`; expect missing adapter.
- [x] **Step 3: Implement** `/portal/distributor/login`, `/apps`, `/series`, `/series/{publishId}`, `/campaign-links`, `/campaign-links/{id}`, `/campaign-link`, plus template reads and expiry refresh.
- [x] **Step 4: Ensure** `free_episode_count`, `episode_seq`, `payment_template_id`, and optional `name` are canonicalized before reuse keys are computed; never reuse a link with a different template.
- [x] **Step 5: Run** protocol/workflow tests; commit `providers: add Gangganhao adapter`.

### Task 5: Port the 容量 adapter and post-create readback

**Files:**
- Create: `backend/app/modules/providers/adapters/rongliang.py`
- Create: `backend/tests/modules/providers/fixtures/rongliang-contract.json`, `test_rongliang_protocol.py`
- Modify: `backend/app/modules/providers/link_steps.py`, `backend/app/modules/providers/session_refresh.py`

**Interfaces:**
- `RongliangClient.login(http, email, password) -> RongliangClient`
- `RongliangClient.discover_applications() -> list[ApplicationDescriptor]`
- `RongliangClient.search(title, cursor)`, `lookup_link(...)`, `create_link(...)`, `read_link(...)`

- [x] **Step 1: Write tests** for form-data app discovery, compilation/episode mapping, exact reuse tuple, create acknowledgement, list lookup, URL detail, and token expiry codes.
- [x] **Step 2: Run** `uv run pytest tests/modules/providers/test_rongliang_protocol.py -q`; expect missing adapter.
- [x] **Step 3: Implement** form login/cookie transport and the compilation, episode, form-data, link page/create/url endpoints. Keep signed video URLs out of logs and persistence.
- [x] **Step 4: Make post-create recovery strict.** A successful create without a uniquely identified batch and URL is `result_unknown`; do not choose the first list record without matching compilation, episode, client and platform.
- [x] **Step 5: Run** protocol, recovery and session tests; commit `providers: add Rongliang adapter`.

### Task 6: Expose provider capabilities in API and投放页

**Files:**
- Modify: `backend/app/modules/providers/router.py`, `schemas.py`, `catalog.py`
- Modify: `frontend/src/features/providers/presentation.tsx`, `ConnectionPanel.tsx`, `queries.ts`
- Create: `frontend/src/features/builds/ProviderLinkConfigFields.tsx`
- Modify: `frontend/src/features/builds/BuildInputPage.tsx`, `ApplicationPicker.tsx`, `ManualLinkSheet.tsx`
- Test: `frontend/tests/providers.spec.ts`, relevant build page tests

**Interfaces:**
- `GET /api/tenants/{tenant_id}/providers/connections/{connection_id}/applications/{application_id}/capabilities`
- `LinkConfigSchema` includes `provider_kind`, `fields`, `defaults`, `required_when`, and `schema_version`.

- [x] **Step 1: Write UI tests** for the three connection types, credential field labels, capability-driven config controls, template required state, and preserving link config in draft idempotency.
- [x] **Step 2: Run** `bunx playwright test tests/providers.spec.ts`; expect new labels/controls to fail.
- [x] **Step 3: Add** kind labels, type-safe generated client models, connection form fields, capability query and provider-specific config renderer using shadcn components.
- [x] **Step 4: Wire** normalized `link_config` into create/update draft requests and validate it server-side through the selected adapter; keep manual `other` path unchanged.
- [x] **Step 5: Run** `bun run build` and provider/build Playwright tests; commit `providers: expose new source capabilities in build workspace`.

### Task 7: End-to-end offline validation and staged real acceptance

**Files:**
- Modify: `backend/tests/modules/providers/test_catalog.py`, `test_preparation_api.py`, `test_pagination.py`, `test_tenant_repository.py`
- Create: `docs/validation/2026-10-02-provider-expansion.md`
- Modify: `docs/implementation-progress.md`

- [x] **Step 1: Add cross-provider workflow cases** for candidate ambiguity, duplicate input, link reuse, config conflict, auth expiry, session refresh, unknown write, and tenant separation.
- [x] **Step 2: Run the required offline checks:**
  - `uv run pytest tests/modules/providers -q`
  - `bunx playwright test tests/providers.spec.ts`
  - `bun run build`
  - `node --check ../duiba-drama-link-tool/duiba-link-cli.js`
  - `node --check ../gangganhao-drama-link-tool/gghys-link-cli.js`
  - `node --check ../rongliang-drama-link-tool/rongliang-link-cli.js`
- [x] **Step 3: Record** fixture coverage, migration head, generated client refresh, and known provider limitations. Keep real credential acceptance separate from offline evidence.
- [x] **Step 4: With explicit user-provided credentials only, run read-only verification** for login/apps/search on each selected provider; run link creation only as a separately confirmed operation and record URL/readback evidence.
- [x] **Step 5: Commit** `providers: validate multi-source copyright integration` after checking `git status -sb` and the focused diff.
