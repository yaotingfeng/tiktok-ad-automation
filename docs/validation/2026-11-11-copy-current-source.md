# 按当前平台配置复制：本地验证与发布方案

## 用户要求与修复

用户明确要求：无论来源是否由TK-ADA搭建，均应按平台当前配置复制。原实现把成功搭建记录、版权方/剧目、推广链接和文案库关联当作准入条件，这些本地关联不应决定平台广告能否复制。本次以当前已授权目录中的系列、广告组、广告、Mini、身份及链接为事实源，不虚构历史关联。

报表将价值类型、竞价策略及出价分别呈现：`IMPRESSION_LEVEL_AD_REVENUE`为广告收入价值，`VO_MIN_ROAS`为目标ROAS；`VALUE`本身仅代表价值优化，不等同最高价值。当前页按完整租户/账户/实体身份补充读取配置，并校验目录发布版本。

复制将平台读取默认值转换为创建合同，保留实际预算、ROAS、定向、排期、完整文案（最多五条）及支持的广告设置。素材与CTA缺少本地记录时自动发起有界只读准备，核实真实视频/封面元数据，再接入原素材分发链；跨账户仍使用核实后的目标资产。未知或不可投放素材不会被标记为可用。错误指出层级及字段，读取失败可通过重新预览/重新读取来源显式重试。

来源指纹仅比较远端业务配置、层级和冻结路由，不把地区名称或本地素材映射变化当成源配置变化。同一素材登记由真实PostgreSQL事务锁串行，封面行锁先于账户素材更新。

## 迁移与历史行为

`0115_copy_current_source`接续`0114_retire_material`，增加来源事实任务表，并将仅适用于普通搭建的策略/版权方/链接/文案外键关联改为复制可选。普通搭建仍必须具备真实关联；来源内部预览分组身份与版权方剧目身份分离。预览最终冻结时检查创意分组完整性。

新复制合同版本为2。未提交的旧复制草稿有“重新读取来源”入口：保留已保存的逐行设置，废弃未提交子预览并重新生成。旧冻结正文不被改写；已经提交的版本1任务保持原执行意图。迁移不删除广告、历史记录或旧事实，不支持直接数据库降级。

支持范围仍为现有升级Smart+ Minis视频复制；普通/旧Smart+、轮播、跨币种或无法通过目标账户资格核验的设置，按实际能力给出具体原因。来源是否在本系统创建不再是限制。

## 本地证据

使用独立PostgreSQL与Redis；平台替身仅放在传输边界，未创建或启用真实广告。私有来源观察与测试日志仅保存在忽略目录`.runtime/copy-current-source/`。

- 最终后端定向回归139项通过（78.90秒）：见`backend-final.log`，命令为在`backend/`执行`uv run pytest tests/modules/builds/test_campaign_copy_compile.py tests/modules/builds/test_campaign_copy_flow.py tests/modules/builds/test_copy_queue.py tests/modules/builds/test_copy_invalidation.py tests/modules/builds/test_copy_source_facts.py tests/modules/builds/test_sdk_contract.py tests/modules/builds/test_copy_current_source_migration.py tests/modules/builds/scene_jobs/test_service.py tests/modules/builds/scene_jobs/test_schema.py tests/modules/reporting/test_dimension_query.py tests/test_migration_graph.py -q`。覆盖当前配置编译、无搭建历史来源、跨账户资产映射、完整多文案、源变化/权限撤销、持久队列/重入、读取重试/配额等待、双连接共享素材、SDK/MCP CTA读取、场景准备、报表、迁移图及历史升级。
- 历史升级用真实旧schema播种冻结预览及未知执行请求，升到0114后记录快照，再升0115；原行不变、新关联准确回填、冻结触发器恢复且普通搭建缺关联被拒绝。单独运行1项通过。
- 前端`ads-detail-config.spec.ts`、`campaign-copies.spec.ts`、`ads-reporting.spec.ts`（workspace项目）：53项通过，含旧草稿重新读取来源及独立价值/竞价展示。
- 前端`bun run build`：类型检查、正式版和体验版构建通过。
- 修改业务模块的定向`ty check`、修改Python文件的Ruff检查/格式化与语法编译、`git diff --check`通过；生成客户端来自OpenAPI生成脚本。
- 独立整体复核提出六项确定问题，均已修复并有相应回归。较早扩展运行中的两项`test_execution_scene_dependency.py`在未修改的身份目录fixture准备阶段失败（缺完整分页证明），不宣称整个后端或全量CI通过。最终定向回归不包含该既有fixture问题。

## 待选定的生产发布方案

目标为现有生产TK-ADA；发布前在共享锁内核对实际镜像与schema，要求当前为0114，不能只按文档猜测。保留现有环境配置、四个部署开关、额度、并发、数据库池及资源规格，不新增服务或开关。不创建本轮备份或恢复演练，遵循2026-10-29用户偏好。

1. 先完成精确提交的不可变镜像构建并与其他发布/CI协调，再冻结API新写入与Beat新投递，按现有宽限正常退出并排空受影响的搭建、素材、控制与结果任务。保留队列和未知请求证据，不强杀、不清队列。
2. 同一版本执行Alembic升级0114→0115；这是增量结构调整及新任务合同，不能走无迁移快修通道，也不能让旧Worker消费新任务。
3. 先启动新消费者，确认resources注册`campaign_copies.read_source`及原搭建/修复任务，再启动控制/Beat/API，所有应用角色使用同一镜像摘要。确认迁移head、注册、HTTPS和实际配置未变。
4. 开放后在当前授权租户/BC只读核验原来源显示、来源读取和复制预览；没有具体预览提交授权时不创建测试广告。旧草稿使用“重新读取来源”。记录真实业务和积压观察；异常时停止新任务，保留证据并前向修复，不直接降级数据库。

当前只完成本地修复；尚未推送、执行生产迁移或更新线上镜像。发布需选定上述具体方案。
