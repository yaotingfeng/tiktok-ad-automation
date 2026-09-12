# 测试环境 MCP BC 分页与真实授权验收

- 修复版本：`e290b1b826cfba2acb17fed58cb93ee04ea90ef5`，已提交并推送至既有 `feat/platform-implementation`。
- 实测原错误为官方 `bc_get` 业务码 40002，限制 `page_size <= 50`；工具 inputSchema 未声明上限。候选读取从 100 改为 50，业务拒绝使用固定脱敏提示。
- 回归：首次范围运行 82 passed、1 failed（多页 fixture 仍为旧 100 条）；更新为 101 个 BC 分三页后完整 binding 文件 14 passed，其他通过项未修改。Ruff、ty、diff 检查通过。外部调用使用传输层替身，PostgreSQL/Redis 使用独立测试实例范围。
- 发布前完整备份：`/var/backups/tt-ada-staging/20260912T075636Z/`，含 PostgreSQL、Redis、项目源码及实际前端构建产物、私有配置/证书。PG 新库恢复、Redis 独立 socket 装载、项目隔离解压、四份归档 SHA-256 均通过，存在 RELEASE_COMPLETE。
- 970 个跟踪文件 SHA-256 与固定提交一致；API/Worker/Beat 实际 cwd 均为该 release，备份与证书 timer active，无数据库迁移。HTTPS、登录/前端资源、管理员及租户权限隔离、私有回调错误边界检查通过。
- 用户指定 Sun Browser；使用 Computer Use 完成当前连接重新授权，官方页面返回 TK-ADA 后真实 BC 读取成功，弹窗显示 1 个可选 BC，原报错消失。用户明确确认该 BC 后，继续点击“绑定并发现账户”，UTC 08:01:56 完成；界面显示“可用 / 发现进度：已完成”，服务端连接 ACTIVE、发现 COMPLETE、阶段 FINALIZE、实际业务发送 6 次。
- 真实账户页已展示 42 个广告账户；数据库相同 tenant/BC/connection 下 42 条均 in_bc 且 authorized，币种、时区和平台状态正常展示。截图仅保存在私有 .runtime 目录，不将截图、身份信息或令牌写入仓库。
- 未手动切换默认连接；首次绑定发布在该 BC 尚无默认时自动初始化为当前 MCP，已有默认通过 on_conflict_do_nothing 保留。未上传素材或创建广告；42 个账户上传及搭建权限仍待核实，不能由目录成功推定。
