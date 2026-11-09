# 2026-11-09 素材冻结桥读取分批

状态：代码与真实PG验证完成，待精确生产发布。

S已解决已知读取期限异常的空占租约，另有8次读取claim COMMIT泛化断连。精确关联8/8均素材child，冻结上下文94,550–119,910条materials、2,035–2,499 owners，evidence物理压缩约4.20–4.75MB。21.8–28.6秒任务内无SDK请求。不能据此断言线上全部为idle超时或已知回滚；未知commit处理保持原样。

include_materials=True不再session.get整个宽evidence。统一读取窄元数据，以500行服务端游标逐批校验全部材料父链/租户/冻结范围；读取需要时收集原值对象，发布仅校验。两个模式校验一致，后续同量子页复用原context，不扩大10秒idle或45秒事务预算，不吞OperationalError、不盲清新grant或重放HTTP。

真实PostgreSQL18验证旧路径两段各6秒CPU构造后在commit触发IdleInTransactionSessionTimeout；新路径3项通过，直接观测ServerCursor.fetchmany请求与实际缓冲均≤500，末批跨tenant损坏仍拒绝。相关69项通过，原quantum必要SQL计数3改4后整文件11项通过，后续四页仍复用。Ruff/格式/语法通过；严格类型当前13项、S基线15项，无新增。root源审通过。

最终返回仍保留全部素材对象，空间O(N)。极端负载若单批CPU超过10秒或总耗时超过45秒，仍按原边界终止；不宣称消除了所有断连。无DDL/消息/额度/开关或广告写入变化，按同结构快速发布授权保留精确版本与11角色实际验收，不新备份/恢复演练。
