# TK-ADA

TK-ADA 是多租户 TikTok 短剧广告自动投放工具。后端为 FastAPI、PostgreSQL、Celery/Redis，前端为 React、TypeScript、shadcn/ui，TikTok 接入固定版本官方 Python SDK。

当前实现和验证记录见 [实施进度](docs/implementation-progress.md)。产品规则、页面设计及七阶段任务见 [交付计划](docs/superpowers/plans/2026-09-08-tiktok-00-delivery-roadmap.md)。

当前本地代码位于 `/Users/yaotingfeng/Documents/ytf/ytf-os-ad-skill/projects/tiktok-ad-automation/`，是资料工作区内的独立 Git 仓库。2026-09-09 已从原同级目录迁入，保留 Git 历史与本地数据；下列命令均从这个应用根目录开始。

- [本地运行与 HTTPS 部署](docs/runbooks/bootstrap-deployment.md)
- [骏伯生产部署、更新和数据库发布规则](docs/runbooks/production-junbo.md)
- [2026-09-10 生产发布验收](docs/validation/2026-09-10-production-release.md)
- [工程版本基线](docs/engineering-baseline.md)
- [整体设计](docs/superpowers/specs/2026-09-08-tiktok-00-overall-design.md)
- [功能、页面与实际验收截图](docs/acceptance/functional-delivery.md)
- [R2 批量视频上传部署与恢复](docs/runbooks/r2-video-upload.md)
- [R2 浏览器验收](docs/acceptance/r2-browser.md)、[源入库、清理与目标分发验收](docs/acceptance/r2-pipeline.md)

本地启动先复制 `.env.example` 为 `.env` 并填写独立开发环境。凭据不提交。应用可在未配置 TikTok App 时启动登录与回调入口；真实授权、版权方和广告操作须另有实际联调证据。

```bash
uv sync --frozen --package app
bun install --frozen-lockfile
bun run --filter frontend build
cd backend
uv run alembic upgrade head
uv run python app/initial_data.py
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

默认管理账号由 `.env` 的 `FIRST_SUPERUSER` 和 `FIRST_SUPERUSER_PASSWORD` 初始化。没有公开注册入口。首次创建后改动环境变量不会自动重置已有用户密码。

登录和新建用户使用普通账号、密码，不需要邮箱。网眼、嘉书由各租户保存独立加密凭据，Python适配器自动重新登录并继续原任务；运行时不调用CLI或共享其本地账号文件。R2作为视频临时中转，源广告账户入库强回读成功后安排删除，保留文件名、摘要和实际账户VID；后续目标使用已授权TikTok素材路径。新导入和自动清理为两个独立部署开关，默认关闭，待私有桶和真实跨账户联调通过后开启。

当前已启动的本地地址为 `http://127.0.0.1:8011`。平台管理员登录后，在“平台租户管理”点击“进入租户”，再使用该租户的业务菜单。未连接 TikTok BC 时，可以先保存投放策略、打开版权方连接配置和管理成员；素材与广告搭建需要连接可用 BC。当前缺少真实 App 和授权，本地运行不等同于真实投放已验收，详见[首次使用与运行限制](docs/runbooks/bootstrap-deployment.md#首次使用与可用范围)。

保留上游 FastAPI full-stack 模板 MIT 许可证，固定来源见工程基线。
