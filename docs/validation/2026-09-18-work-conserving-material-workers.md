# 2026-09-18 素材空闲槽复用与封面续跑查询

## 范围与真实证据

仅新加坡测试环境、原提交，不重建原未知广告、不新增素材补发授权。延续原三素材槽、11进程、配额、超时及功能开关；不是增加不受控并发。

`5d16f4b`部署后，06:09精确只读排队40个已ACK视频。约06:14资源准备队列为空，结果队列113；结果池最近五分钟只完成31个封面核验，总299.36秒、均9.66秒、最大21.94秒，未处理视频核验，没有worker死亡或异常栈。两个准备槽不能消费结果，属于队列隔离导致的空闲容量浪费。

共享准备池改为`resources,resource-results`双队列，仍2个prefork；专用结果1槽保留。沿已有Redis round_robin和prefetch=1公平消费，两个队列有货时轮流，上传占满共享池时专用结果池仍可推进。只修改测试环境service，不改生产compose或上游调用额度。

封面续跑真实PG20×10测试另证实 `_resume`400条、`_continue`406条SQL，逐成员读取与autoflush形成N+1；4ms/SQL时仅_resume已2.605秒。本机尚未跨越原5秒期限，不能宣称已本地复现deadline。批量续跑修复已随本次版本部署。

## 验证

- 封面最终_resume400→3、_continue406→10条SQL；原5秒短事务内，4ms/SQL延迟实测0.055秒/0.093秒。新增13项及原关键7项20通过48.21秒，根代理独立13通过37.71秒；Ruff/ty/diff绿。
- 独立审查抓到批次/成员反向锁P1：只在_continue补batch锁仍遗漏此前_publish已锁job及预发送拆分、异常路径。真实双连接先RED（另一会话不能NOWAIT锁job），最终统一_publish素材→batch→job；_resume及MID/arm/receipt/except/continue先batch→job，不反向拿素材。新双连接续跑/发布测试均在等待batch时不占job；nonce/dispatch/revision/成员范围/过期均在集中更新前校验，单wake与armed不再POST保持。
- 06:27:39UTC，原40项视频UNKNOWN已全部恢复出UNKNOWN，17补发持续15ready+2READY；完整103/180、实际AD215/360，14封面UNKNOWN和13上游READBACK未知尚在，不宣称全批成功。
- 队列测试先RED（准备池无法消费结果、双积压只消费准备），再GREEN；4文件30通过、3个Linux prefork场景本机跳过，根代理复验30通过/3跳过6.47秒。服务器Linux真实prefork、队列隔离、200成员4ms延迟、栅栏和双连接锁序共19项通过198.44秒，测试角色恢复NOLOGIN/NOCREATEDB、私有bootstrap库移除。
- 所有恢复仍通过正式应用服务与outbox，不挪Redis消息、不伪造成功。17项补发仍15视频ready、2来源封面READY；40视频、两封面和15READBACK仅恢复只读。
- 另有已成功创建的CAMPAIGN `1876646980650113`在同账户完整23条列表和全状态精确读取均不可见。创建request_id `20260918134029F2239F8E08E1B2E90FC7`，06:15:58读取request_id `2026091814155838428E4A06E8927BD27D`。同通道另一样本正常返回并正式恢复成功，尚无本地过滤/解析缺陷证据；不能将全部未知回读归为短暂一致性，也不允许据空结果重建。
- 060724Z正式五归档/22表/1449响应再次核验后，只清理其恢复测试库`tt_ada_tenant_library_5d16f4b7_test`及`Co3T13wx`临时解压副本；正式备份/业务/版本保留，可从备份重建。剩余约1.34百万KiB，为下一次完整备份预留。

## 实际部署与恢复

- 运行版本 `e9b0001a704975a1507852b0a4931005bc3a65d8`，固定归档1220文件预检通过。旧任务正常排空，完整备份`/var/backups/tt-ada-staging/20260918T064018Z/`：五归档校验、1385项目文件字节、22表全行摘要、1451加密响应、独立PG/Redis/私有配置恢复及Alembic无变更检查全部通过。
- 六服务11进程实际cwd/私有环境、前端字节、额度/开关、两个timer、HTTPS及双管理员身份隔离通过；head仍`material_approved_reissue`。第一次核验发生在prefork/API启动完成前，只有6进程且HTTPS非JSON，等待启动后重新全部通过，不将首次失败混作成功。
- 实际Celery订阅确认：resources池`resources,resource-results`，results池`resource-results`，builds/control保持各自队列。第一次独立脚本直接路径执行误取共享venv的旧editable包，配置校验失败且未修改业务；改以当前backend标准输入入口后通过。
- 原提交12项已armed的UNKNOWN BUILD封面，经正式`_material_reconciliation`只读接续；未重新上传/分享。原UNKNOWN AD整行摘要持续`c8eeef9758a98470404af8cfd5435842`。正式HTTP RETRY请求`ed075bea-7868-5724-9b21-e2520009b568`、恢复`fc5d8ed1-9131-4ef7-888e-eb4480048a85`实际COMPLETED/2，仅明确未发送失败，不是批次重建。
- 06:45:40 UTC完整仍103/180，素材齐109；实际AD215/360、另2QUEUED。近期3分钟45项MATERIAL真实成功；原17补发15视频ready+2来源封面READY。14项素材步骤未知和13项READBACK未知尚未全部解决，不能宣称整批完成。
- 上线后约4分半，共享准备池已完成30次verify_cover与7次prepare_cover，专用结果池完成19次verify_cover；证明空闲槽复用实际生效，不把任务次数等同唯一素材成功数。结果队列93（此前单池113；中间采样89，新增工作可使数值回升），builds16，正常继续消费。
- 06:42首次健康窗口没有新Traceback/死锁/statement timeout/Worker lost/OOM，六服务NRestarts=0；这只是短窗口，不是万级容量证明。
- 发布后仅686192KiB剩余。再次核验本次正式五归档、临时副本字节、22表/1451归档和零DB会话，清理`tt_ada_tenant_library_e9b0001a_test`及`/root/tt-ada-tenant-library-restore.KmDH8svZ`；正式备份、当前业务和全部release均保留，临时副本可重建。可用空间恢复1111052KiB，仍需关注20GB测试盘容量。
- 06:47:31 UTC复核：完整103/180、AD215/360不变，素材齐增至111，近3分钟54项MATERIAL成功；新增两项CAMPAIGN成功、ADGROUP继续运行。自06:41起六服务无自动重启及上述五类异常；内存约1528MiB、swap1138MiB。这是素材继续推进的证据，尚不是广告全部完成。
