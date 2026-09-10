# 新加坡测试环境图标发布验收（2026-09-10）

- 用户授权将确认的黑白 TK-ADA 图标配置到系统。仅发布新加坡无 Docker 测试环境，入口 `https://tk-ada.137-220-150-31.sslip.io`；骏伯生产未变更。
- 固定源码 `8b59aed363c35bef317f6cd0fc30da48c40c723a`，原版本 `995f89569880df330b02314bfff5e1df33256b1a` 保留。API、Worker、Beat 同时指向新版本目录。
- 统一登录/侧栏/移动导航共享品牌组件；增加 favicon、Apple 主屏幕图标与 192/512px manifest 图标，保留 1254px 开发者平台上传原图；移除未使用 FastAPI 品牌资源。没有新增离线缓存或服务工作线程。

## 验证与发布

- 本地冻结依赖、TypeScript/Vite 构建、22 项 workspace-shell 回归、改动组件/manifest 的 Biome、diff 空白检查通过。
- 服务器 uv 冻结安装及 Bun 冻结安装成功，前端 tsc 因小内存退出 137。改用同一源码提交已通过本地构建的静态产物；上传包 SHA-256 `e1b4566d0964c01ce602af7a1ee20131db0ebe4d47b0f1e537a9c4f4bb82cc01`。移除 macOS 打包产生的 AppleDouble 元数据旁文件，最终产物仅保留应用资源。
- 停止 backup timer、确认备份任务结束，停止 API 新写入、Beat、正常排空 Worker，然后完成备份 `/var/backups/tt-ada-staging/20260910T142311Z/`（COMPLETE）。切换 current 后恢复三个应用服务及 backup timer。
- 数据库与源码 Alembic head 均为 `r2_part_receipts`；本次无后端/依赖/数据库迁移变化，未执行数据迁移。
- 公网 HTTPS health、登录 HTML、回调确定业务错误、未知 API 404 通过；实际平台管理员登录/profile 通过。Worker ping pong，API/Worker/Beat/备份 timer 均 active。
- 5 份正式 PNG 线上返回 image/png，与发布目录逐字节一致，尺寸依次为 32、180、192、512、1254；HTML favicon/Apple/manifest 引用与 manifest 中图标尺寸均通过。真实浏览器连接失效，因此没有追加线上视觉截图验收；本地浏览器回归与线上 HTTP 验收分别记录。
- 未执行真实 TikTok/R2/版权方操作，未修改账号密码或启用自动化。代码仅本地提交，未推送。

## 回退

本次无数据库变化。如需回退，按环境手册停止新写入并排空服务，将 current 切回保留的 `995f89569880df330b02314bfff5e1df33256b1a`，同版本启动 API/Worker/Beat，再验证入口与消费者；恢复备份 timer。不要覆盖数据库或 Redis 数据目录。
