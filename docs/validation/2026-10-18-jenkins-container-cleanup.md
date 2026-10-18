# Jenkins 临时容器残留清理

## 现场与实际处理

公司 Jenkins `http://123.207.4.46:8080`，宿主 Docker 通过 Jenkins 已有 docker.sock 操作；不涉及 ACK 运行资源、正式数据库或线上备份。清理前核对 TK-ADA 构建均结束（当时最新 #42 FAILURE、#41 SUCCESS），没有工具容器仍执行。

发现 63 个临时测试容器，58 个运行、5 个退出；36 个为 `tkada-ci-*`，27 个为 `tkada-backup-test-*`。最老一批创建于 2026-10-17 02:27，已运行约 38 小时；其余与 2026-10-18 #29–36 的 ABORTED 构建时间吻合。

清理严格验证容器名格式、本项目 ACR 镜像、无 bind 挂载；只删除已核对的容器 ID 及附属匿名卷，另删除其挂载的 5 个具名合成备份卷。第二步核实 6 个同名测试网络均无端点后删除。回读 `docker ps -aq --filter name=tkada-` 为零；首批清理前后两个非本项目容器均保留。最终检查时另一个 ad-manager 构建已启动，其 3 个容器仍正常运行，Jenkins、Nginx 均在。

## 原因与修复

检查与恢复测试经宿主 Docker socket 创建容器，实际是工具容器的兄弟资源。已有 `check.sh` EXIT trap、Python finally 只在进程正常获得退出机会时执行。构建被中止后，外层只 `docker stop` 工具容器；停止工具容器不会级联停止宿主的测试数据库，也不能保证内部清理执行完成。未有外部资源回收兜底。

为本次构建临时容器、网络、具名卷统一附加 owner 标签，并将归属从 Jenkins 工具容器透传到隔离检查环境及恢复测试。外层 EXIT trap 和 Jenkins `post always` 均按同一构建标签清理；先删除仍可能创建子资源的工具容器，再枚举其余测试资源。清理失败会显式失败，重复执行安全，不改全机 Docker 清理策略。

新增 `TKADA_CI_OWNER` 为 CI 资源归属参数，不是业务开关。不标记或操作 ACK 的正式发布资源。宿主/Jenkins 突然退出且 post 未运行时仍需手工按已结束构建的标签清理；未引入后台定时强删。

## 验证与交付边界

- 修复前新增回归运行失败，因清理入口与标签实现不存在；修复后 3 项通过。
- `TEST_CI_RESOURCE_DOCKER=1 python3 deploy/ci/test_resource_cleanup.py`：真实 Docker 验证强制结束外层容器后测试子容器仍运行，外部清理可回收容器/网络/卷，重复清理安全且另一 owner 资源完整保留；全为本机合成资源。
- `node scripts/test-deployment-assets.mjs`：4 组共 76 项发布资产/编排测试通过。
- 修改的 Python Ruff、编译，3 个 shell 语法与 `git diff --check` 通过。

宿主残留已实际清理。防复发代码仅本地提交，未获本轮推送指令，不推送、不触发自动发布；Jenkins 尚未使用新清理逻辑。未运行整套业务/浏览器回归或完整恢复集成，不将定向 Docker 回归称为线上流水线中止验收。
