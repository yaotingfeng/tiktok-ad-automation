# R2 导入验收证据

当前文档区分已执行的本地合成验收和仍待授权的实际部署验证。测试与脚本不会自动向 TikTok 或运营 R2 桶发送容量任务。

## 可重复命令

先在独立 PostgreSQL `_test` 数据库应用当前迁移。脚本使用现有 `tests.database.require_test_database`，在导入连接/创建fixture前拒绝普通业务库；必须与 backend 当前 `DATABASE_URL` 指向同一个专用测试库。不要用主开发库或生产数据库。应用配置由安全环境注入，不把 DSN放在命令输出、报告或提交中。

从 backend 工作目录、使用包含测试依赖的项目 Python：

```sh
python -m alembic upgrade head
python -m pytest tests/acceptance/test_r2_probe.py tests/acceptance/test_r2_capacity.py -q
python ../scripts/acceptance-r2-capacity.py --count 10000 --output /tmp/r2-capacity-10000.json
python ../scripts/acceptance-r2-capacity.py --count 20000 --output /tmp/r2-capacity-20000.json
```

输出文件必须是新文件，脚本不覆盖旧证据。迁移也只能在确认测试数据库之后执行；容量脚本自身不运行迁移。仓库的私有 test runner可注入环境，禁止打印其环境JSON。

每次运行创建新的tenant、用户、BC和run_id，使用真实JWT访问应用FastAPI路由；没有认证依赖override或伪造API成功。文件元数据按200个一组受理，重新建立HTTP client后重放旧receipt，冻结清单，完整读完100条/页的所有结果并逐一核对client_index→material_id。summary单独请求5次并统计SQL。finally只移除该tenant的fixture、该tenant对global预算的精确贡献；成功与失败清理均有专用PG测试。

脚本的多文件接收计量不上传文件字节。另有三文件、256-byte合成窗口：两个文件获准，第三个waiting_capacity；通过明确标记的synthetic deletion-evidence fixture调用真实release helper后，第三个恢复。该结果证明账务背压及恢复，不证明R2物理占用、清理Worker或取消在途PUT已经正确完成。来源账户公平性、WorkerRSS、校验临时磁盘、1000混合故障和源/目标全链路明确输出pending/not_measured。

`--scenario mixed-faults-1000` 不执行pytest业务链，仍返回pending及退出2。已实现的独立命令是 `python -m pytest tests/acceptance/test_r2_pipeline.py -q`，其6条完整MP4链及1,000文件控制面故障记录见[业务链验收](r2-pipeline.md)。后者覆盖容量等待、旧revision、取消和丢Create/Complete响应，400个对象经真实清理worker回收；不包含1,000次SDK入库、实际429、过期签名服务端执行、进程重启和Worker峰值RSS。更广的运行指标仍待独立验收，不能混入已完成计数。

## 已执行的合成容量运行（2026-09-10）

环境：本机专用PostgreSQL、真实FastAPI/JWT、R2控制面传输替身；无视频字节、TikTok调用或真实R2请求。受理速率只按chunk请求耗时计算，不代表平台上传速率或每天可交付的素材数。下表两次运行受当时本机负载影响，不用于比较优化前后。

| 指标 | 10,000文件 | 20,000文件 |
| --- | ---: | ---: |
| chunk数（每次≤200） | 50 | 100 |
| 完整分页数（每页≤100） | 100 | 200 |
| 无重无漏的material数 | 10,000 | 20,000 |
| 每次chunk SQL数 | 19 | 19 |
| 每页SQL数 | 10 | 10 |
| 每次summary SQL数 | 9 | 9 |
| chunk P50/P95（ms） | 429.028 / 2708.933 | 222.243 / 1463.065 |
| page P50/P95（ms） | 6.267 / 13.849 | 16.725 / 39.783 |
| summary P50/P95（ms） | 2.860 / 3.000 | 2.625 / 2.833 |
| 合成元数据受理（文件/秒） | 234.620 | 496.800 |
| 整体运行时间（秒） | 44.885 | 48.356 |
| 合成窗口预留峰值（bytes） | 256 / 256 | 256 / 256 |
| waiting后证据释放恢复 | 通过 | 通过 |
| 自有fixture清理 | 通过 | 通过 |

run_id：10k为`1b31e9c597204d02ad290f8189eb49b2`，20k为`77f016c8f0b94cbb9477278c0e1dec10`。原始本地JSON位于本次隔离工作树 `.runtime/r2-capacity-10000.json`、`.runtime/r2-capacity-20000.json`；不包含连接信息或业务凭据。这些是可重跑的工程观测，不是生产SLO。

脚本边界测试：14项通过，包括默认零网络、无效namespace阻断、签名PUT/GET小闭环、GET/digest/delete故障后精确key清理、未知Create/PUT不假报清理完成、SDK wire日志隐藏、非测试库拒绝、真实路由/预算与异常fixture清理。Task3另有20k明细分页、并发chunk幂等、作用域、未知结果恢复与取消竞态测试；这些不替代下列live条件。

## 实际部署仍待完成

| 项目 | 当前证据 | 状态 |
| --- | --- | --- |
| 当前私有桶R2签名PUT/GET/HEAD/Delete兼容 | 仅传输替身；本轮未执行`--probe` | pending |
| 目标前端origin真实CORS与匿名访问拒绝 | 配置示例；无部署截图/回读 | pending |
| 当前官方媒体HTTPS精确host allowlist | 不提供未经验证的域名 | pending |
| 官方SDK URL源上传及VID/md5/来源账户回读 | 需授权小批、当前连接与媒体证据 | pending |
| 1 GiB URL视频 | 产品与默认配置上限已调至 1 GiB；真实 R2/TikTok 大文件联调待完成 | pending |
| 原件删除后新目标分发、封面与搭建 | Task6/7及6条合成完整链已通过；真实授权链路未执行 | live pending |
| 1000混合故障完整Worker链 | 控制面1,000文件、400次清理已通过；1,000源/目标入库和进程故障未覆盖 | wider load pending |
| 在途/未知PUT取消的可靠终止与空间释放 | 不能用TTL、Abort响应或一次空页代替证据 | pending |
| Linux prefork硬时限/单Beat/队列重启 | 部署要求和任务边界测试，不是实际Linux演练 | pending |
| 真实日量、带宽、平台配额、Worker内存与磁盘 | 元数据测试未覆盖 | pending |

实际验收顺序和安全回滚见[部署runbook](../runbooks/r2-video-upload.md)。两个开关默认False；真实清理启用前必须先证明后续目标来源路径。保留素材主身份、VID/MID、文件名和强摘要，不能以删除记录代替删除对象或恢复丢失字节。
