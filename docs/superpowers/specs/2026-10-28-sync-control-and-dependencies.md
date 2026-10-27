# 同步控制与目录依赖修复设计

用户要求剩余问题彻底修复并集中一次发布。当前生产e2b49f5f，schema0045。此次只修复已证明的执行链路，不扩大云资源、提高Worker并发、清空队列或跳过完整性校验。

## 根因

1. 23:59–00:03健康检查占control总执行容量50.6%，历史池忙95.6%，报表存在等待flush的短空档。历史份额和并发本轮保留。
2. 历史含删除对象的targeted目录被不覆盖其ID/冻结范围的后续live发布误判superseded；真实样本在第一页读取前等待5小时后被淘汰。
3. targeted背景依赖与全量历史扫描竞争同一个8在途窗口，没有独立保底。
4. 关联维护reconcile候选读取超过2秒，wake共用同事务/预算而未执行；逐条签名复核超时会回滚轮转进度。

## 约束与最终行为

- 每分钟只检查报表新鲜度、筛选年龄及management积压。重型目录/素材覆盖独立续步，仍须120秒内完整测完才发布；coverage观测起点3分钟后启动新轮，结果超过5分钟即过期。保留现有task/payload，v2 Redis私有进度隔离旧周期，当前broker优先级不改。
- 轻量observed_at与coverage_observed_at/coverage_completed_at分开。coverage_status=PENDING/COMPLETE/FAILED；首次无完整结果、超期和失败分别COVERAGE_PENDING/STALE/QUERY_FAILED。接口与页面不得把旧覆盖计数称为当前完整健康。范围含冻结身份、归属、账户时区当地日及规则，发布前再核验；不依据单个watermark缓存全部状态。
- 目录supersede统一使用superseding_directory_predicate(older,newer)：tenant/BC/actor/route/account/kind/type一致，新请求顺序更晚且已发布，ids和parent_ids两维度覆盖，include_deleted不能降级，双向排除material child。执行、发布、清理共用条件，已有对象观察版本保护保留。
- 普通ad/creative延迟发布只能更新本轮实际赢得观察版本的对象所对应素材；creative还必须验证每个creative归属广告。丢失观察竞争的素材保留审计并记不完整，不覆盖较新映射。
- 存量错误STALE依赖按原任务waiting_ids与仍未解析广告的交集识别，冻结身份一致且不存在在途/更新完整覆盖、超过十分钟去重期才提前原任务到期；恢复序号集合与due同事务保存并跨退避保留。不复活STALE、不改成功水位，正常入口重算缺口并建新请求。
- 未知账户补修查询归入独立2秒保存点，失败不会丢分钟健康观测或旧完整覆盖。
- 背景目录总窗口8不变，targeted/full各保底4且空闲互借，保留租户轮转和类内原请求年龄；实时目录池不改。
- reconcile/wake先物化现有active小集合、再按PK锁定重验。wake逐条保存点、超时保留失败事实并推进复核游标；两个维护事务独立且总预算约2秒，wake先1秒、reconcile后1秒。正常到期执行兜底，不伪造变更或完成。
- 不增加新的队列、布尔开关或数据库结构；真实数据与配置只存私有runtime。固定旧错误样本通过正常补读/依赖恢复，不直接标完成。

## 发布与验收

所有上述改动及所需接口页面一起验证通过后才推company/main并统一发布；不推GitHub、不创建新分支。按既有快修授权不新备份、不额外全量排空，保留正常退出、任务身份栅栏及精确SHA/配置验收。源版本的真实问题通过本地PG/Redis行为回归和受限生产只读SQL对照复现；上线后观察完整coverage周期、实时同步和固定历史/依赖任务的真实推进。外部平台分页变化保留有界重试，不能承诺第三方永不变化或用队列计数清零替代正确性。
