# 新加坡测试环境自动原件清理启用

- 用户明确要求开启自动清理。目标为新加坡测试站点，运行版本保持 `b9850420f5fca646048ff1ac7376f50affaa9ea9`，数据库 head 保持 `mcp_multibc_runtime`，无代码部署或数据库迁移。
- 原因：三服务实际环境均为 `MATERIAL_CLEANUP_ENABLED=false`；24 个已核实来源账户的原件处于 `pending / cleanup_disabled`，其中新批次 23 个。
- 先暂停备份 timer、冻结 API 写入、停止 Beat 并正常排空 Worker，再创建 `/var/backups/tt-ada-staging/20260912T172309Z/` 完整备份。数据库 dump、Redis RDB、配置归档、独立项目及前端构建归档均校验 SHA-256；数据库恢复至独立临时库验证 head、Redis 在独立 socket 装载并读取、项目和配置隔离解压比对均通过。依赖和缓存排除项及工具链版本记入私有清单。
- 修改服务器 `/etc/tt-ada-staging/app.env` 的 `MATERIAL_CLEANUP_ENABLED=true`，校验设置与调用额度后重启 API、Worker、Beat。检查三个运行进程均加载 true，存储、数据库、Redis、MCP 和调用额度等配置一致；备份 timer 已恢复。
- 现有自动任务完成全部 24 个原件删除，均有 HEAD 确认，错误为空。账户素材仍为 24 个 available；原件全局和租户容量占用均归零。
- 租户真实登录后的同源队列接口确认新批次：ready_count=23、cleaned_count=23、failed_count=0、reserved_bytes=0。HTTPS、登录页与静态资源、平台/租户登录和权限隔离、回调边界检查通过。
- 本次只启用并验证临时原件清理，不创建或删除广告账户素材，不执行广告搭建。后续原件在来源账户核验成功、活动使用释放后自动清理。
