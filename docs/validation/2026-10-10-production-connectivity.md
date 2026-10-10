# 2026-10-10 生产域名适配与跨云连通性核验

## 结论

用户提供阿里云新加坡 PostgreSQL / Redis 内外网地址与凭据，指定域名 `tkada.galaxyshortdrama.com`。配置已私密登记，原测试服授权加密密钥保留。公司 TKE 集群目标不变，但本轮连通性未通过，尚不能切换业务。

## 实测

- 集群入口 `prod/public` 仍为 `134.175.255.190`，TLS hosts 仅声明 `*.jetmobo.com`、`*.gzxinran.net`；新域名没有对应规则或证书声明，DNS 查询无解析结果。
- 在本项目临时 Pod 中运行已发布 AMD64 镜像 `1d3b43bfbf22676ad2a574dad9ef31bfc0428755`。镜像拉取成功、平台自动注入 eklet toleration、容器启动成功。探针没有注入密钥，没有启动 API/Worker/Beat，也未执行数据库或广告写入。
- 四个地址均在集群中成功解析：PostgreSQL 内网 `192.168.1.22` / 外网 `43.107.10.210`；Redis 内网 `192.168.3.251` / 外网 `43.107.10.233`。每项使用 8 秒 socket 超时发送无凭据的 PostgreSQL SSLRequest 或 Redis PING，均返回 TimeoutError。未完成协议握手或认证；超时不能单独确定是哪一侧路由、防火墙、出口或白名单阻断。
- 本机 TCP 建连曾显示成功，但后续 PostgreSQL SSL 协商未成功、Redis TLS 握手提前结束；不把端口初测记为真实服务可用，也未发送凭据尝试绕过。
- 平台在探针结束后无法返回容器日志，第二次探针改用 Pod termination message 保存结果。两个本轮创建的临时 Pod 已按 UID 核对并清理；证据保留于 Git 忽略目录 `.runtime/company-production-20261010/`。

## 代码与验证

- 部署脚本允许用户明确指定的 `tkada.galaxyshortdrama.com`，保留公司单层 `*.jetmobo.com` 的现有入口支持；不任意放开其他域名。
- 在应用 Secret 写入和共享 Ingress rule 追加前，检查 Ingress TLS hosts 能否精确或单层通配覆盖目标域名。不读取共享证书 Secret，不自动替换共享证书；声明检查不能代替发布后的真实 HTTPS 证书验证。
- 更新生产环境初始化 origin、部署模板和运维清单。私有草稿中的新回调尚未在平台登记，不修改源 MCP JSON 冒充已登记。
- 4 项构建行为测试 + 20 项部署行为测试通过；覆盖缺少自定义域名证书时阻断写入、精确/通配声明接受和跨层通配拒绝。Ruff、格式、compileall 与差异检查通过。

## 剩余条件

1. 用户/运维明确 TK-ADA 的 PostgreSQL 数据库名、Redis 逻辑库编号及迁入权限。
2. 打通 TKE 到所提供阿里云端点的网络，核实允许的连接方式和 TLS；随后进行真实认证与目标恢复预检。
3. DNS A 记录指向集群入口，并由运维为共享入口配置覆盖新域名的证书和 TLS host。
4. 现有 TikTok API 应用、MCP 客户端登记新回调，R2 CORS 允许新 origin。
5. 一致性备份与恢复验证完成后，才能停旧消费者并迁入生产。测试服本轮保持运行。
