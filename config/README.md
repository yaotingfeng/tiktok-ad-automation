# 环境配置统一入口

所有环境使用根目录 `.env.example` 这一份完整参数模板。环境名称、项目内文件名和服务器加载位置登记在 `config/environments.json`，不依赖聊天记录查找。模板和索引进 Git；填写真实值的环境文件被 `.gitignore` 排除。

| 环境 | 项目内私有文件 | 实际加载方式 |
| --- | --- | --- |
| local | `.env` | 在 `backend/` 启动后端，Settings 读取 `../.env`；进程环境变量优先 |
| staging | `.env.staging` | 部署到测试服务器 `/etc/tt-ada-staging/app.env`，systemd 向 API、Worker、Beat 注入 |
| production | `.env.production` | 部署到生产服务器 `/etc/tt-ada/production.env`，固定 `deploy/production-compose.sh` 通过 `--env-file` 读取并注入容器 |

`.env.staging` 和 `.env.production` 是部署准备文件，创建后不会自动加载或切换运行环境。部署成功后，服务器固定路径中的配置是该实例的生效来源；本地副本不代表已部署。前端只有公开构建参数，使用 `frontend/.env.example`；禁止放入存储或平台密钥。

## 换环境或首次安装

在项目根目录执行（命令也可通过绝对路径从其他目录调用）：

```bash
python3 scripts/environment.py local init
python3 scripts/environment.py staging init
python3 scripts/environment.py production init
python3 scripts/environment.py staging info
python3 scripts/environment.py production path
```

`init` 根据同一模板生成所选环境文件，权限为 0600；已有文件会拒绝覆盖，不生成或复制其他环境的密钥。非本地环境的数据库/Redis地址留空，必须按目标部署填写；其他占位值也须填写后才能使用。`info` 只显示位置与存在性，不显示文件内容。项目原有 `.env` 无需迁移。

新机器从模板初始化后，通过受控传输或密钥管理恢复该环境的真实值。Git 本身不会保存、恢复密钥。不要将测试环境的数据库连接、加密密钥、OAuth 注册材料直接复制到生产；已有数据库必须保留其对应的加密密钥，否则无法解密原授权。`MCP_CLIENT_REGISTRATION_REF` 指向该环境的独立私有注册文件，也须单独备份。

## 部署与同步

每次先按[功能开关清单与发布确认](../docs/runbooks/deployment.md#功能开关清单与发布确认)核对当前值与拟定值。首次部署和新增/改变语义的开关须向用户说明并确认；已有明确确认继续有效，普通升级保持原值。`init` 生成的 false 是模板默认值，不是用户的部署选择；确认后在目标环境显式填写两个 `MATERIAL_*_ENABLED` 值，后续新增开关同样处理。

先按对应 runbook 冻结写入、排空任务并完成全部备份与恢复验证，再将项目内所选环境文件受控传输到目标服务器，以受限权限安装至表中固定路径。配置文件中引用的注册材料、证书等文件同时按 runbook 核对。不要直接把模板覆盖到现有服务器。

- 测试环境：[新加坡部署手册](../docs/runbooks/staging-singapore.md)。配置为 root:tt-ada、0640；更新后重启 API、Worker、Beat 并核实实际配置一致。
- 生产环境：[骏伯生产手册](../docs/runbooks/production-junbo.md)。使用固定发布脚本重建受影响容器；单纯 restart 不会更新 Compose 注入的值。
- 本地环境：[本地启动手册](../docs/runbooks/bootstrap-deployment.md)。编辑 `.env` 后重启相应进程。

服务器固定路径独立于 `/releases/<SHA>`，因此更新、回退项目版本不会丢失环境配置。若在服务器直接修改配置，须同步更新该环境的受控副本并记录部署验证；不要以过期本地副本覆盖服务器。

投放策略、租户、账户授权与版权方连接属于数据库业务数据；R2 CORS/桶权限属于 Cloudflare 配置；Nginx、systemd、证书属于部署配置。它们不塞入 `.env`，其位置和恢复要求统一由上述 runbook 管理。完整恢复必须包含这些数据和文件。
