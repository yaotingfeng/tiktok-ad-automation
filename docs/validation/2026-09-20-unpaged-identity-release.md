# 非分页身份目录修复与测试服发布验收

## 问题与边界

- 环境：新加坡测试服务器，星屿租户，BC `7666010978596749328`。
- 业务样本：草稿 `e5262622-b380-413b-91f3-b61fb53b4ae3`、旧冻结预览 `4d9817b1-4981-4270-82d6-70ba5c8bcd88`，包含 `Haunted by a Jealous Ghost` 与 `Living Beneath His Roof`。
- 两部剧的网眼剧目、推广链接、素材及 LemonShow 小程序均已匹配。五个账户的场景任务实际阻塞在 identity 读取，并非剧目或小程序目录缺失。
- 真实 `identity_get` 返回 58 条完整身份，`page_info` 四项均为零，表示非分页；旧解析器把其他目录的单页 50 条限制误用于该响应并产生 `scene_response_unverified`，页面将下游阻塞投影为“所选小程序不在当前账户的可用目录”。

## 修复

- 发布提交：`e2ef7d5f3c5abbd47b88912eb91675ba5bc403d7`。
- 只对第一页、分页四字段齐全且全部严格为整数零的 identity 非分页响应取消 50 条计数限制。
- 继续强制校验每条字段、身份 ID 唯一性、可用状态、总数一致性及第一页/末页语义；账户角色、小程序等分页接口继续限制每页 50 条，MCP 传输层继续限制响应体为 8 MiB。
- 场景合同版本升级为 `dual-channel-scene-2026-09-20-v4`。同一草稿重新准备时产生新的 scope，不复用旧版 `scene_response_unverified` 终态任务；旧冻结预览保持不可变。

## 回归证据

- 新增 58 条 identity 的 API/MCP 双通道回归。旧实现两项均准确失败于 `scene_response_unverified`；修复后两项通过。
- 服务器隔离 PostgreSQL 与独立 Redis 中，`test_scene_validation.py` 为 29 passed；完整场景目录及读取合同为 101 passed，耗时 219.48 秒。
- Ruff、格式、ty、Python compileall 与差异检查通过。第一次本机运行因 macOS Python 3.14 与数据库驱动握手兼容问题未进入业务测试，最终通过 Linux 隔离环境完成正式验证。

## 备份、切换与恢复

- 变更前停止 API/Beat，四个 Worker 正常排空；暂停备份 timer，并在完成后恢复。
- 完整备份批次：`/var/backups/tt-ada-staging/20260920T112946Z/`。PostgreSQL、Redis、原项目、私有配置和完整运行配置/证书五个归档均通过 SHA-256 校验。
- 项目归档独立解压核对 1,322 个文件；Redis RDB 在独立实例中 PING/读取通过；PostgreSQL 在隔离数据库恢复后核对 22 张业务表及 4,309 份加密响应，迁移前后摘要一致。数据库 head 前后均为 `draft_scene_phase`，Alembic 无待执行操作。
- 隔离恢复数据库和临时目录已删除；完整备份和上一个 release 保留。同机备份没有异地副本。

## 发布后验收

- `current` 指向 `e2ef7d5f3c5abbd47b88912eb91675ba5bc403d7`。API、Resource、Result、Build、Control、Beat 六个服务均为 active 且 `NRestarts=0`；四个 Celery 节点均返回 pong，备份 timer 为 active。
- 启动检查通过健康页、已构建登录页、回调业务错误边界和受保护 API 边界；发布前后配置摘要一致。
- 使用原失败账户和原冻结 MCP 路由进行一次真实只读 identity 调用，新版本返回 `seen=58`、`total_number=58`、`last=true`，并保留 2 条合格匹配；原症状已在真实数据上消失。
- 本次没有调用 TikTok 写接口、没有创建或启用广告，也没有自动改写旧冻结预览。网页需要对原草稿执行“重新准备”，完成后重新生成预览；旧预览继续保留为历史证据。
