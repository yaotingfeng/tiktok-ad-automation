# 2026-10-10 生产域名切换验收

生产入口已按用户要求切换为 **https://tkada.shortclipflix.com**。真实 DNS、正常 CA/主机名校验的 HTTPS、浏览器登录页、原管理员账号登录及两个租户读取均通过。没有使用 hosts 或 ALB IP 覆盖解析。

## 配置变更

- 目标仍为阿里云新加坡 ACK `c19dbd82590a34b9f87cebb8c1a858796` / `prod`。复用共享 `prod-https`，仅追加本项目新域名的 TLS host 和 `/` 路由；完整 spec 回读等于原配置加这两项，其他条目保持一致。引用既有 `shortclipflix.com` 证书 Secret，不读取其私钥。
- CNAME 指向 `alb-ae6m00kf3iaolvbxim.ap-southeast-1.alb.aliyuncsslbintl.com`，证书覆盖 `*.shortclipflix.com`。
- 运行配置只改变 `FRONTEND_HOST`、`TIKTOK_REDIRECT_URI`、`MCP_REDIRECT_URI`，并替换为官方新注册的 MCP 客户端。九角色配置及注册摘要一致，额度和租约校验通过。
- MCP 官方动态注册返回 201，确认回调 `https://tkada.shortclipflix.com/api/integrations/tiktok/mcp/callback`。原注册材料保留；历史三份 MCP 授权须由用户重新授权。
- R2 CORS 追加新 origin，保留测试域名与旧生产域名；三者真实签名 URL 的 OPTIONS 均返回 204 和对应允许来源。共享桶原有公开状态未改变。
- 旧测试入口迁移提示已更新为新域名，Nginx 校验和 reload 成功；旧九应用服务仍 inactive/disabled。

应用镜像未重建：`783d3b836b61171153cf1863f4ecd7617bc3e7d6`，digest `sha256:3d3a03ec331f7adc2796a30c47dbc0836d64596164cbf6dc40701927aa94b891`；本轮为域名配置发布。迁移 Job 成功，head 仍为 `0019_scene_budget_page`。PostgreSQL `tkada`、Redis DB 15 和开关 true/true/false 保持不变。

## 备份与恢复证据

按配置发布要求，先停 API/Beat、排空七 Worker，再冻结消费者。active/reserved/scheduled、队列及 unacked 均为零后，备份 PostgreSQL public、Redis DB 15、独立发布源码、运行镜像中的实际前端构建、项目私有配置和共享入口声明。共享证书由平台持有，未导出其他项目 Secret。

在隔离 PostgreSQL 18 / Redis 8 中恢复验证：136 张业务表行数一致、4 份凭据解密成功、Redis 8 键恢复，候选迁移前后指纹一致。归档校验和与解压验证通过；验证容器、卷和网络已清理。

首次通过 exec 流传输的 PG dump 被截断，恢复验证发现 EOF，未用于切换。重新生成并传输的最终 dump 为 157,513,959 字节，源端/本地 SHA-256 一致，完整恢复成功。托管 Redis 的 RDB 由冻结的 DB 15 DUMP/PTTL 快照在隔离实例重建，没有对共享实例执行 BGSAVE、FLUSH 或覆盖持久文件。

私有证据根目录：`.runtime/company-production-20261010/ack/domain-shortclipflix/`。`manifest.json` 和 `backup-verified.json` 记录备份/恢复；`runtime.env`、`mcp-client.json` 与线上 Secret 逐项匹配，本地 `.env.production` 已同步。`config-after.tar.gz` 保存切换后的恢复配置，归档成员逐字节回读通过，摘要在 `config-after-verified.json`。原配置、注册和冻结备份全部保留，不入 Git。

## 验证结果与边界

| 检查 | 结果 |
| --- | --- |
| 本地部署/构建回归 | `node scripts/test-deployment-assets.mjs`：4 项构建 + 21 项部署测试通过 |
| Python 语法和差异 | 修改脚本 py_compile、git diff --check 通过 |
| 九 Deployment | 全部 ready，验证时重启数为 0 |
| Worker / Beat | 七 Worker ping 7/7；Beat 单副本、20 GiB PVC Bound |
| 真实域名访问 | 健康接口、浏览器登录页面、全部引用静态资源 200 |
| 登录和权限 | 原账号登录成功，两个租户可读；未登录 401、越权租户 403、未知 API 404 |
| API/MCP 回调边界 | 无有效 state 均返回 409 / invalid_oauth_state |
| MCP 配置 | 两租户均 configured / READY；不代表完成 OAuth |
| 临时资源 | 本轮 Pod/Secret `tiktok-ad-automation-domain-change` 按 UID 核对清理 |

实际结果见私有 `http-verification.json`、`services-verification.json`、`worker-ping.json`、`r2-cors-verified.json`、`cleanup-verified.json` 和 `final-status.json`。

**仍待用户操作**：TikTok API 应用 `7690990985458761748` 需登记新回调 `https://tkada.shortclipflix.com/api/integrations/tiktok/callback`。本轮检查新回调授权入口仍显示应用状态异常，当前浏览器账号无该应用管理权限；此前用户登记的是旧域名，不能视为新域名已登记。用户随后在生产手动完成 API/MCP 重新授权。

完整 OAuth 收码/换码、MCP tools/list、候选 BC、明确绑定后的账户发现及真实业务/容量测试尚未完成。生产托管备份策略尚未从云控制台核验；源备份 timer 仅备份旧源。当前共享 R2 桶原有 Public Development URL 已开启，不能将本次 CORS 验证当作私有桶合规证明。基础部署已具备人工测试条件，不能据此宣称全部业务验收完成。
