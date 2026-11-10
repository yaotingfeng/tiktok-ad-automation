# 搭建失败诊断日志

## 变更

本轮只补日志，不更改搭建参数、重试次数、历史状态或真实广告。

- `step_evidence.summary` 增加失败现场白名单：冻结账户/连接/剧目标识、链接摘要、父步骤和 Minis 标识。
- 本地失败保留脱敏后的说明；封面不可用保留当前目标与来源任务的状态、VID、发送标记、图片候选、扫描页数及总数。只记录当前冻结素材的有界样本，并标明是否截断。
- 运行日志增加 `build_failure_recorded`，可用租户、BC、批次、步骤、尝试次数和证据编号回查持久记录。事件表示已加入当前事务，最终结果以数据库提交为准。
- 继续沿用原平台错误码、脱敏说明、请求编号和传输诊断；不把完整请求、凭据或签名链接写入普通日志。

无数据库迁移。新增日志只对新执行生效；历史缺失回执无法补造。日志代码已随应用 `4cd98958` 整批发布生产，详见整批发布记录。

## 验证

使用本轮独立本地 PostgreSQL / Redis（私有测试启动器位于 `.runtime/xingyu-build-diagnosis-20261110/test.py`）：

- 新增回归先运行失败，分别证明本地说明参数与来源诊断不存在；实现后通过。
- `test_failure_diagnostics.py`、`test_execution_state.py`、`test_cover_first_share.py` 合计 **30 passed**。
- 修改文件 Ruff、Python 编译、`git diff --check` 通过。
- 扩大到旧 `test_execution.py` 时，28项通过、5项在夹具准备阶段报 `identity_catalog_unavailable`，未进入执行器。该组未计入通过结论。
- mypy 对三文件检查仍报告 `execution.py:299` 既有的 `parent_request(kind=str)` 类型问题；本轮新增诊断模块与状态模块无报错，不宣称整组类型检查通过。

生产排查始终使用只读事务；私有原始证据保存在上述 `.runtime` 目录，未提交真实报表或凭据。未重试或创建任何广告。
