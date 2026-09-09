# TikTok 短剧自动投放平台

多租户 TikTok 短剧投放工作台。后端为 FastAPI、PostgreSQL、Celery/Redis，前端为 React、TypeScript、shadcn/ui，TikTok 接入固定版本官方 Python SDK。

当前实现和验证记录见 [实施进度](docs/implementation-progress.md)。产品规则、页面设计及七阶段任务见 [交付计划](docs/superpowers/plans/2026-09-08-tiktok-00-delivery-roadmap.md)。

- [本地运行与 HTTPS 部署](docs/runbooks/bootstrap-deployment.md)
- [工程版本基线](docs/engineering-baseline.md)
- [整体设计](docs/superpowers/specs/2026-09-08-tiktok-00-overall-design.md)
- [功能、页面与实际验收截图](docs/acceptance/functional-delivery.md)

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

保留上游 FastAPI full-stack 模板 MIT 许可证，固定来源见工程基线。
