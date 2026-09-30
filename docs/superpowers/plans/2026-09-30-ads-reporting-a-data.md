# 广告目录、双通道与采集 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建立覆盖外部广告的真实目录、可验证的双通道报告合同及可恢复同步，为六维查询和批量管理提供持久事实。

**Architecture:** 沿用 PostgreSQL、Redis、Celery、Outbox 和冻结的 `FrozenTikTokRoute`。广告目录与报告分别暂存、完整发布，通过现有 typed gateway 接入 MCP 和官方 Python SDK；本阶段不实现六维 HTTP 查询或广告写操作。

**Tech Stack:** Python >=3.14、uv、SQLModel、Alembic、PostgreSQL、Redis、Celery、Pydantic、pytest、现有固定版本官方 TikTok Python SDK。

**Spec:** `docs/superpowers/specs/2026-09-30-ads-management-reporting-design.md`，实施前通读，尤其 §4–7、§10、§12。

## Global Constraints

- 系列名统一按 `版权方-剧名-其他备注` 识别，剧名在第二段；不要求剧 ID、版权方登录或本工具搭建记录。
- 当前 BC 内按“版权方＋剧名”归组，系列下级继承此归属；不建立人工剧关联流程或独立业务剧目录。
- 本模块不包含分成、返点、赠款、赔付及经营净收益指标；金额使用十进制定点数，保留上游精度。
- 广告完整详情每 3 小时同步，核心报表每 30 分钟主动拉取；采集频率不是上游实时性承诺。
- 同时建设 MCP 与官方 API 接入；API 真实联调待授权，合同测试不能宣称通道已经接通。
- 复用 `FrozenTikTokRoute` 的 tenant、BC、connection、channel、授权/绑定代数；任务不得随默认连接切换。
- 不调用真实外部服务，不读取私有配置内容，不部署；迁移仅在独立测试数据库验证。
- 所有代码增加必要中文业务注释；接口通过官方 SDK 或固定 MCP 合同，无自建 TikTok HTTP 网关。
- 路径均相对独立仓库根；测试命令在 `backend/` 执行，Git 命令在仓库根执行。
- 数据库测试先按 `config/README.md`、`config/environments.json` 确认环境；`DATABASE_URL` 必须为带 test 名的独立 PostgreSQL 库，`TEST_REDIS_URL` 为不同于应用库的非零 Redis 库，沿用 `tests/database.py` 校验。
- 每任务提交前执行 `git status -sb`、`git rev-parse --show-toplevel`，只暂存该任务明确路径，检查 `git diff --cached`；同时记录 `docs/implementation-progress.md`，不推送、不新建分支。

## Review Focus

- RF1：系列名空第二段、组合 Unicode、仅两段与备注改名，不得误归组；A1/A4 测试固定解析与名称版本行为。
- RF2：同数字 ID 出现在不同账户、Smart+ 集合与创意、两个通道，不能合并错对象或重复计费；A2/A3 测试固定身份。
- RF3：分页中断、重复页、20,000 广告截断和迟到任务，不能发布半份或覆盖新事实；A4/A6 测试固定完整性。
- RF4：收入缺失、零金额、异币种/归因、跨日及名称成员变化，不能产生伪零或错误观测差值；A5/A7 测试固定口径。
- RF5：绑定变更、双投递和旧工作者恢复，不能换通道续跑或越代发布；A8 用真实 PostgreSQL/Redis 验证。

---

## 文件与跨阶段边界

- `contracts/ads.py`、`contracts/reporting.py` 定义只读合同；`adapters/{mcp,sdk}_{ads,reporting}.py` 负责通道映射；`gateway.py` 增加 `ads`、`reports` 成员。
- `modules/ads/{naming,models,directory,sync_models,sync,tasks}.py` 分别负责名称、目录存储、授权读取、采集状态、完整发布、任务；`modules/reporting/{contracts,models,facts,sync_models,sync,scheduling,tasks}.py` 负责指标与采集，最终六维查询由阶段 B 编写。
- A1 类型是阶段 B/C 的共享入口；A2 的 `locate/list_objects` 仅为目录对象读取，B 自行实现筛选分页/聚合；C 自行定义管理写合同。
- 各新包增加 `__init__.py`。迁移登记在 `backend/app/alembic/env.py`，不借用创建意图或创建步骤表。
- 新测试 fixture 在所属测试目录 `conftest.py` 定义并复用根 `session/context/other_context/redis_client`；目录 seed 必须创建真实租户成员、BC 访问关系，外部替身只放传输边界。
- 阶段 A 只有 `backend/app/alembic/versions/ads_reporting_data.py` 一个迁移，`revision='ads_reporting_data'`、`down_revision='material_push'`；A2 创建全部表并测试，后续任务消费已有结构，不重写已执行迁移。总路线图统一后续链 `ads_reporting_data -> reporting_queries -> ad_management`。

### Task 1 (A1): 固定名称、对象与只读传输合同

**Files:** Create `backend/app/integrations/tiktok/contracts/ads.py`、`backend/app/integrations/tiktok/contracts/reporting.py`、`backend/app/modules/ads/naming.py`；Test `backend/tests/contracts/test_ads_reporting_contracts.py`。

**Interfaces:**
- Produces `EntityRef(tenant_id:UUID,advertiser_id:str,kind:Literal['campaign','adgroup','ad','creative'],remote_id:str)`、`MaterialUseRef(ad_ref:EntityRef,platform_material_id:str,ad_material_id:str|None,material_type:str)`。普通广告素材没有广告内 ID 时仍可读取；只有 ID 存在且能力支持才可供 C 独立素材启停，不得用 VID 替代。
- Produces `CampaignIdentity(provider_label:str|None,drama_name:str|None,status:Literal['VALID','INVALID'])`；`parse_campaign_name(name:str)->CampaignIdentity`。
- Produces `AdEntity(ref,parent_ref:EntityRef|None,ad_type:str,name:str,configuration:dict,operation_status:str|None,review_status:str|None,delivery_status:str|None,observed_at:datetime)`；`AdMaterialUsage(use_ref:MaterialUseRef,local_material_id:UUID|None,operation_status:str|None,complete:bool)`；`CallEvidence` 复用 `contracts/common.py`，不新建替代类型。
- Produces `DirectoryQuery(advertiser_id:str,kind:str,ad_type:str,page:int,page_size:int,ids:tuple[str,...],parent_ids:tuple[str,...],include_deleted:bool)`；`DirectoryPage(items:tuple[AdEntity,...],materials:tuple[AdMaterialUsage,...],next_page:int|None,complete:bool,evidence:CallEvidence,page:int=1)`，与账户目录同名类型不互换。
- Produces `ReportQuery(advertiser_id,report_contract,metric_family,dimensions:tuple[str,...],metrics:tuple[str,...],start_date:date,end_date:date,granularity:str,currency,timezone,attribution,filter_ids:tuple[str,...],page:int)`；字符串字段均为 `str`。
- Produces `ReportRow(subject_key:tuple[str,...],bucket_start:datetime,bucket_end:datetime,values:dict[str,Decimal|None],availability:dict[str,str])`、`ReportPage(rows:tuple[ReportRow,...],next_page:int|None,complete:bool,evidence:CallEvidence,page:int=1)`、`ReportTask(task_id,advertiser_id,status:Literal['PENDING','RUNNING','READY','FAILED'],query:ReportQuery)`。

- [x] **RED：** 添加 RF1 及身份校验测试，非空 ID、时区日期、完整标记/后续页冲突均须拒绝。响应携带实际请求 page（正整数，next_page 必须更大），不从游标推测页号；完整异步文件使用 page=1。
```python
assert parse_campaign_name(' 嘉书 - 总裁归来 -账户2').drama_name == '总裁归来'
assert parse_campaign_name('嘉书--测试').status == 'INVALID'
assert parse_campaign_name('嘉书-总裁归来').status == 'VALID'
assert parse_campaign_name('嘉书-Cafe\u0301-备注') == parse_campaign_name('嘉书-Café')
```
- [x] **Run RED：** `uv run --frozen pytest --confcutdir=tests/contracts tests/contracts/test_ads_reporting_contracts.py -q`，预期新模块缺失导致失败。
- [x] **Implement：** 实现上述不可变 DTO 和 NFC/trim/保留空段解析；定义 `AdsReadOperations.read_page(query:DirectoryQuery)->DirectoryPage`；`ReportOperations.read_page(query:ReportQuery)->ReportPage`、`create_task(query:ReportQuery)->ReportTask`、`check_task(task:ReportTask)->ReportTask`、`download_task(task:ReportTask)->ReportPage`。
- [x] **GREEN：** 重跑上述命令，全部通过；下载合同明确仅 `READY` 且整个文件成功解析才 `complete=True`，不模拟平台分页。
- [x] **Commit：** 明确暂存本任务列出的四个文件、新包 init 和进度记录，提交 `ads: define directory and reporting read contracts`。

### Task 2 (A2): 目录持久身份、素材使用关系与授权读取

**Files:** Create `backend/app/modules/ads/models.py`、`backend/app/modules/ads/directory.py`、`backend/app/modules/ads/sync_models.py`、`backend/app/modules/reporting/models.py`、`backend/app/modules/reporting/sync_models.py`、`backend/app/alembic/versions/ads_reporting_data.py`、`backend/tests/modules/ads/conftest.py`；Modify `backend/app/alembic/env.py`；Test `backend/tests/modules/ads/test_directory.py`、`backend/tests/modules/ads/test_directory_migration.py`。

**Interfaces:**
- Consumes A1 `EntityRef/AdEntity/AdMaterialUsage` 与 `accounts.access.usable_grants`；Produces `AdObject`（EntityRef 四键唯一，持久化 AdEntity 全字段及 `published_version:int`）、`CampaignNameProjection(campaign_ref,raw_name,provider_label,drama_name,status,parser_revision:int,name_revision:int,grouping_revision:int)`、`AdMaterialReference`（持久化 AdMaterialUsage，另含外部素材 name、可空 main_material_id/main_material_type、creative_ids）。名称投影保留按 name_revision 排序的审计记录；grouping_revision 仅在规范化的版权方/剧名/有效性发生变化时递增，备注变化只更新 name_revision。
- Produces `locate(session:Session,*,context:TenantContext,bc_id:str,ref:EntityRef)->AdObject`；`list_objects(session:Session,*,context:TenantContext,bc_id:str,advertiser_ids:tuple[str,...],kind:str,parent:EntityRef|None=None)->tuple[AdObject,...]`。
- Produces A4/A5/A7 下列字段合同规定的全部持久模型：`AdDirectoryRun/AdDirectoryPage/ReportFact/ReportCoverage/ReportObservation/ReportSyncRun/ReportStagedPage/SyncSchedule`，以及 A3 的 `AccountBalanceObservation`（含 balance_scope/scope_id，区分独占账户与共享 Portfolio）；包括运行唯一键、冻结路由、领取代数、分页与异步 task_id、版本、覆盖和错误字段。模型创建集中此任务，采集行为分后续任务实现。

- [x] **RED：** 真实数据库 fixture `directory_seed` 暴露授权 context/bc/ref；新增 RF2、跨 BC 拒绝、外部素材无本地记录仍可读和迁移唯一约束测试。
```python
row = locate(session, context=directory_seed.context, bc_id=directory_seed.bc_id, ref=directory_seed.ref)
assert row.remote_id == directory_seed.ref.remote_id
assert row.published_version == 1
with pytest.raises(DomainError):
    locate(session, context=directory_seed.other_context, bc_id=directory_seed.bc_id, ref=directory_seed.ref)
```
- [x] **Run RED：** `uv run --frozen pytest tests/modules/ads/test_directory.py tests/modules/ads/test_directory_migration.py -q`，预期新模型/读取接口缺失失败。
- [x] **Implement：** 添加全部 A 表的租户/账户复合约束、发布键及索引；源连接仅为证据，不进对象唯一键。保留普通/Smart+ 类型、三类状态和广告内素材身份。访问从当前成员及 BC grant 开始，绝不按 `MaterialFile.bc_id` 过滤投放素材；`list_objects` 只供已界定账户/父级集合的内部展开，B 的大列表另用服务端分页查询。
- [x] **GREEN：** 重跑两文件；同对象双通道 upsert 只一行、异账户/异 kind 不碰撞；迁移测试沿用 `tests/migration_database.py` 的独立库流程且不改历史迁移。
- [x] **Commit：** 明确暂存本任务列出路径及进度记录，提交 `ads: persist scoped remote directory and material references`。

### Task 3 (A3): 普通与 Smart+ 目录双通道及余额观测

**Files:** Create `backend/app/integrations/tiktok/adapters/sdk_ads.py`、`backend/app/integrations/tiktok/adapters/mcp_ads.py`；Modify `backend/app/integrations/tiktok/gateway.py`、`backend/app/integrations/tiktok/mcp/tool-contracts.json`、`backend/app/integrations/tiktok/mcp/protocol-profile.json`、`backend/app/integrations/tiktok/mcp/results.py`、A1 `contracts/ads.py`、A2 `models.py`；Test `backend/tests/integrations/tiktok/test_ads_read_adapters.py`、`backend/tests/contracts/test_tiktok_sdk_surface.py`。

**Interfaces:**
- Consumes A1 `DirectoryQuery/Page`；Produces `SdkAdsReadOperations`/`McpAdsReadOperations`、`TikTokGateway.ads:AdsReadOperations`。
- Adds `read_balance(advertiser_id:str)->AccountBalance(amount:Decimal|None,currency:str,availability:str,observed_at:datetime,evidence:CallEvidence)`，持久化 `AccountBalanceObservation`，不混入 period facts。

- [x] **RED：** 参数化两个通道传输替身 `ads_transport`；固定普通三级、Smart+ 三级及自动创意读回映射，覆盖拒权、工具 schema 漂移、缺余额。
```python
page = ads_transport.gateway.ads.read_page(query=ads_transport.smart_ad_query)
assert page.items[0].ref.kind == 'ad'
assert page.items[0].ref.remote_id == 'smart-ad-7'
assert page.materials[0].use_ref.ad_material_id == 'ad-material-9'
regular = ads_transport.gateway.ads.read_page(query=ads_transport.regular_ad_query)
assert regular.materials[0].use_ref.ad_material_id is None
assert regular.materials[0].use_ref.platform_material_id == 'video-11'
assert ads_transport.gateway.ads.read_balance('account-test').amount is None
```
- [x] **Run RED：** `uv run --frozen pytest tests/integrations/tiktok/test_ads_read_adapters.py -q`，预期 `gateway.ads` 缺失；SDK surface 使用 `--confcutdir=tests/contracts` 单独验证。
- [x] **Implement：** 注册每个物理端点的精确只读操作并经现有额度/路由门禁，统一业务 Protocol 不等于多个工具共用一个发送键；manifest 变更同步摘要，保留原 schema 校验；普通 `*/get/`、Smart+ `smart_plus/*/get/` 分别调用。自动创意补读 `/ad/get/`，不可得则材料完整性为 false；余额接口是 BC 财务分页读取，需独立 BC 财务证据并按当前账户权限过滤，不由广告 read/build 权限推断；缺证据显示不可用，不降级伪零。
- [x] **GREEN：** 重跑；每次分页发送前验证冻结代数，检查 SDK 实际 method/path/参数；未授权 API 和无工具旧类型返回明确能力状态，绝不借 build 权限或另一通道兜底。
- [x] **Commit：** 明确暂存本任务列出路径及进度记录，提交 `ads: add scoped API and MCP directory readers`。

**A3 实施补充（根代理协调）：** 共享解析使用 `adapters/ads_read.py`，余额持久化使用 `modules/ads/balances.py`；补齐 `admission.py` 财务读范围、`official/accounts.py` 可选精确数字解析、`core/errors.py` 明确错误分类。复用已有同物理端点的 read 操作键，避免重复配额桶。`mcp/transport.py` 完整采集目录后按本次工具核验完整 schema；已完成的 READ 业务拒绝仅在无其他错误时保留会话，写入/不明/中断规则保持。相应隔离测试更新缺失工具错误码，保留未发送断言。

`DirectoryPage` 增加 `materials_complete/material_missing_reason`，与主目录分页完整性分开；`AdMaterialUsage` 携带 A2 的名称、显式原生主素材身份和创意 IDs。帖子素材用真实 `tiktok_item_id` 与内部 `TIKTOK_POST` 类型保存，不推断视频库/报表身份；A4 允许 Smart+ 广告页附带真实 creative 子实体，后续 B/C 继续按类型验证能力。

### Task 4 (A4): 完整目录暂存、发布及名称迁组

**Files:** Create `backend/app/modules/ads/sync.py`；Consume A2 `backend/app/modules/ads/sync_models.py`；Test `backend/tests/modules/ads/test_directory_sync.py`。

**Interfaces:**
- Consumes A2 对象及 A3 gateway；Produces `AdDirectoryRun`、`AdDirectoryPage`（冻结 route/账户/类型/页、claim_generation、observed_at、coverage、发布版本）。
- Produces `stage_directory_page(session,*,run_id:UUID,page:DirectoryPage,claim_generation:int)->None`、`publish_directory(session,*,run_id:UUID,claim_generation:int)->int`；所有发布版本为单调整数。

- [x] **RED：** fixture `directory_run` 创建真实 staging/live 数据；RF1/RF3 覆盖失败中页、重复页、末页缺失、原名备注修改、未见≠删除。
```python
stage_directory_page(session, run_id=directory_run.id, page=directory_run.first_page, claim_generation=1)
with pytest.raises(DomainError):
    publish_directory(session, run_id=directory_run.id, claim_generation=1)
assert locate(session, context=directory_run.context, bc_id=directory_run.bc_id, ref=directory_run.ref).published_version == 1
```
- [x] **Run RED：** `uv run --frozen pytest tests/modules/ads/test_directory_sync.py -q`，预期新发布接口缺失失败。
- [x] **Implement：** 完整分页后事务发布；旧版本晚到拒绝；缺失对象只标本次未见，明确远端删除才能改删除状态。名称首两段变更递增 name_revision，备注变更不迁组；历史事实不改写为剧合计。
- [x] **GREEN：** 重跑；断点续接不重复对象，缺父系列安排定向补齐，命名不规范保留目录；素材使用关系不依赖本地上传或创建记录。
- [x] **Commit：** `348dc04` 初始实现，随后以 `8f5d929`、`3b7d657`、`4596c97`、`9252b8a`、`6b1008b`、`be2dd10` 完成修复与复核；最终提交标题为目录完整发布与名称投影。

### Task 5 (A5): 指标合同与不可混写的事实发布边界

**Files:** Create `backend/app/modules/reporting/contracts.py`、`backend/app/modules/reporting/facts.py`、`backend/tests/modules/reporting/conftest.py`；Consume A2 reporting models/sync_models；Test `backend/tests/modules/reporting/test_facts.py`。

**Interfaces:**
- Produces `MetricDefinition(name,unit,additivity,supported_contracts)`、`ReportContract(key,metric_family,dimensions,metrics,granularities,empty_result_policy)`；`ReportFact` 键含 tenant/advertiser/subject_key/bucket/granularity/report_contract/metric_family/currency/timezone/attribution，每个 metric_name 一行，值为 NUMERIC/Decimal、availability、published_version；描述/关联 attributes 为白名单 JSONB，不进事实身份或金额，不能写原始响应/下载 URL。
- Produces `ReportCoverage`（同发布分片键、status、missing_reason、采集时间/版本）、`ReportSyncRun/ReportStagedPage`（route、claim_generation、query、页、异步 task_id、状态）；`ReportObservation` 保存账户/系列 subject、日期/口径/values、observed_at、membership_digest、name_revision、grouping_revision，不持久化剧合计。差值兼容性比较 grouping_revision，不能把仅备注变化当成迁组。
- Produces `publish_report(session,*,run_id:UUID,claim_generation:int)->int`；`observation_delta(previous:ReportObservation,current:ReportObservation)->dict[str,Decimal]|None`，B 消费事实和此比较函数，不在此实现六维查询。

- [x] **RED：** RF4 fixture `observations` 给负修正/跨日/改名记录；真实库覆盖指标组不互抹、空结果、缺指标不补零。
```python
assert observation_delta(observations.previous, observations.corrected)['spend'] == Decimal('-1.25')
assert observation_delta(observations.previous, observations.renamed) is None
assert observation_delta(observations.previous, observations.next_day) is None
```
- [x] **Run RED：** `uv run --frozen pytest tests/modules/reporting/test_facts.py -q`，实现前 RED 未被本轮捕获；未伪造失败结果。
- [x] **Implement：** 登记 spend、`native_growth_ad_revenue_value_d0`、`native_growth_total_ad_impression_value` 及流量/效果原始量；维度/通道/类型逐合同开放，素材 Minis 收入未验证则不可用。分区发布锁以完整指标组为边界，完整空结果按合同替换，旧任务不覆盖新版本。
- [x] **GREEN：** 重跑；补充币种/时区/归因/成员集合差异都返回 None、只改备注仍可比较、真实零与未提供分开、父子事实不互加、分母零不由存储层填比率。
- [x] **Commit：** `fc12e60` 初始实现，`589e0a7` 完成分片/合同/属性修复，`940412e` 收紧公开合同与未知行，`33801a4` 将 `ad_type` 纳入规范分片身份；最终复审通过。

### Task 6 (A6): 同步分片及平台异步报告双通道

**Files:** Create `backend/app/integrations/tiktok/adapters/sdk_reporting.py`、`backend/app/integrations/tiktok/adapters/mcp_reporting.py`、`backend/app/modules/reporting/sync.py`；Modify `backend/app/integrations/tiktok/gateway.py`、`backend/app/integrations/tiktok/mcp/tool-contracts.json`；Test `backend/tests/integrations/tiktok/test_reporting_adapters.py`、`backend/tests/modules/reporting/test_report_sync.py`。

**Interfaces:**
- Consumes A1 ReportOperations 与 A5 发布状态；Produces `TikTokGateway.reports` 及双通道实现，`plan_report_shards(query:ReportQuery,*,entity_ids:tuple[str,...],max_ids:int)->tuple[ReportQuery,...]`。
- Produces `collect_report_step(session,*,run_id:UUID,claim_generation:int,gateway:TikTokGateway)->str`，返回 `CONTINUE/WAIT/READY/FAILED`；一次调用只做有界物理请求，异步任务号与 query/route 固定绑定。

- [x] **RED：** RF3 测试超过 20,000 广告、100 IDs 分片、重复页、异步未 READY/下载半文件及较旧发布；fixture `report_query` 和 `report_sync` 放所属 conftest。
```python
shards = plan_report_shards(report_query, entity_ids=tuple(str(i) for i in range(20001)), max_ids=100)
assert len(shards) == 201
assert sum(len(shard.filter_ids) for shard in shards) == 20001
assert collect_report_step(session, run_id=report_sync.id, claim_generation=1, gateway=report_sync.pending_gateway) == 'WAIT'
```
- [x] **Run RED：** `uv run --frozen pytest tests/integrations/tiktok/test_reporting_adapters.py tests/modules/reporting/test_report_sync.py -q`，预期新适配/采集接口缺失失败。
- [x] **Implement：** 映射 integrated、Smart+ overview/breakdown、task create/check/download；overview 拒绝时间拆分，异步使用完整生成文件语义和有界流式解析，不伪造平台分页；SDK async_req 不是 report task。同步超过对象上限必须拆过滤器，不仅翻页；未知截断不能发布完整覆盖。
- [x] **GREEN：** 重跑；权限/schema/维度不支持不静默降级，素材 D0 不平均分摊；每个 HTTP/MCP 发送仍走相同额度/冻结路由门禁，超时创建异步报告不得在紧循环重建任务。
- [ ] **Commit：** 明确暂存本任务列出路径及进度记录，提交 `reporting: collect partitioned sync and asynchronous reports`。

### Task 7 (A7): 持久计划、回补与刷新合并

**Files:** Create `backend/app/modules/reporting/scheduling.py`、`backend/app/modules/ads/tasks.py`、`backend/app/modules/reporting/tasks.py`；Consume A2 两域 `sync_models.py`；Test `backend/tests/modules/reporting/test_scheduling.py`。

**Interfaces:**
- Produces `SyncRequest(route:FrozenTikTokRoute,advertiser_ids:tuple[str,...],scope:Literal['directory','active','report','balance','history','targeted'],start_date:date|None,end_date:date|None,refs:tuple[EntityRef,...]=())`、`request_sync(session:Session,*,context:TenantContext,request:SyncRequest)->UUID`；B 手动刷新与 C 操作后定向刷新只提交该本地入口。
- Produces `SyncSchedule`（租户授权后台身份、冻结 route、账户、scope、next_due_at、请求历史覆盖）及 `enqueue_due_syncs(session,*,now:datetime)->tuple[UUID,...]`；任务仅运行 run_id/claim_generation 所指持久工作。

- [x] **RED：** RF4 固定账户本地今日和边界日期；相同请求并发及长任务到下一周期仍返回原运行，停投但处归因窗对象必须入核心查询。
```python
first = request_sync(session, context=sync_seed.context, request=sync_seed.request)
assert request_sync(session, context=sync_seed.context, request=sync_seed.request) == first
assert sync_seed.planned_window(kind='initial').days == 30
assert sync_seed.planned_window(kind='unknown_attribution').days == 35
```
- [x] **Run RED：** `uv run --frozen pytest tests/modules/reporting/test_scheduling.py -q`，预期调度接口缺失失败；fixture `planned_window` 必须读取实际持久计划，不能复制实现算法。
- [x] **Implement：** 固定目录3h；活跃配置/余额30min；核心当日+前日30min；近7日3h；每日13:00 UTC归因窗+7（未知35）；每周最近90日已请求范围；初始30日。首次30日后，归因回补可扩展到35日或更长，成功覆盖按实际发布记录，不能仍标只有30日。持久唯一键包含路由代数、账户、合同/指标组、窗口，重复请求合并但不跨不同授权语义合并。
- [x] **GREEN：** 重跑；配置集合为启用或近7日有消耗，余额失败不阻断报告；新绑定账户能建立初始计划，解绑停止后续调度；后台身份权限重新读取；观测保留系列粒度，跨日/改名不生成伪半小时流量。
- [x] **Commit：** `ca8bd7b` 初始实现，`c66ea51`、`e480a94`、`bb54b80`、`2629c50` 完成复审修复；最终独立复审 `task-7-rereview-2.md` APPROVED。

### Task 8 (A8): 队列公平性、冻结权限与恢复验收

**Files:** Modify `backend/app/jobs/celery_app.py`、`backend/app/jobs/tasks.py`、`backend/app/core/config.py`、`.env.example`、`compose.yml`、`compose.production.yml`、`docs/runbooks/deployment.md`、`docs/runbooks/staging-singapore.md`、A7 两域 `tasks.py`；Create `deploy/staging-ads-directory.service`、`deploy/staging-ads-reporting.service`、`docs/validation/2026-09-30-ads-reporting-data.md`；Test `backend/tests/jobs/test_ads_reporting_queues.py`、`backend/tests/modules/reporting/test_sync_recovery.py`。

**Interfaces:**
- Consumes A7 持久调度、现有 `enqueue_after_commit/register_dispatch_task/admit_tiktok_call/verify_route`；Produces 注册 `ads.sync_step`、`reporting.sync_step`、`reporting.scan_due`，业务任务签名沿用 `(*,tenant_id:str,actor_id:str,payload:dict)->None`。
- Adds `ads-directory`、`ads-reporting` 独立队列，管理队列由 C 注册；配置 `ADS_SYNC_ENABLED` 默认 false、`ADS_DIRECTORY_WORKER_CONCURRENCY=1`、`ADS_REPORTING_WORKER_CONCURRENCY=1`，真实启用及部署值须按现有部署确认流程验证，不在本任务开启。

- [ ] **RED：** RF5 用两真实 DB session 和真实 Redis：重复领取只一个有效代数、旧代不得发布、解绑后不再调用替身、默认通道变更不改冻结 route；跨队列积压仍保留后台最低执行份额。
```python
assert sync_recovery.deliver_twice().published_versions == 1
sync_recovery.rebind_bc()
sync_recovery.resume_old_claim()
assert sync_recovery.transport_call_count == 0
assert sync_recovery.run.route.channel == sync_recovery.original_route.channel
```
- [ ] **Run RED：** `uv run --frozen pytest tests/jobs/test_ads_reporting_queues.py tests/modules/reporting/test_sync_recovery.py -q`，预期任务注册/恢复规则缺失失败。
- [ ] **Implement：** Beat 仅扫描到期计划；Outbox 投递有界分片，领取代数与发布事务核验，重启续页/异步 task_id。参照现有 `deploy/staging-control.service` 增加两个独立消费者模板，Compose 同样明确消费者及变量注入；更新非空 `TIKTOK_CALL_POLICIES` 对新增精确操作的校验与示例。当前报告优先、历史最后，用独立槽保证公平，不增加上游共享配额。
- [ ] **GREEN：** 重跑本阶段新增测试、现有 `tests/jobs/test_outbox.py`、`tests/jobs/test_task_routes.py`、`tests/modules/accounts/test_worker_route_authorization.py`；`uv run --frozen ruff check app/modules/ads app/modules/reporting app/integrations/tiktok` 与 `uv run --frozen python -m compileall -q app/modules/ads app/modules/reporting` 通过。队列测试覆盖 Beat 开关、任务到消费者映射、模板变量注入及缺少调用策略时拒绝发送。
- [ ] **Commit：** 明确暂存本任务列出路径及进度记录，提交 `reporting: verify queue fairness and frozen sync recovery`；验收文档分列离线合同、真实 PostgreSQL/Redis、模拟容量和未完成的 MCP/API 真实联调，不把预计规模或采集30min写成已验证承诺。
