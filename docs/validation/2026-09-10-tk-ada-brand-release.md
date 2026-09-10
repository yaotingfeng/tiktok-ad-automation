# TK-ADA 名称与品牌排版发布 · 2026-09-10

用户要求将产品统一更名为 TK-ADA，放大左上角标题、副标题改为较小的“广告投放工具”，登录按钮简化为“登录”。已在本地和生产完成发布。

## 变更与版本

- 代码提交：`d09c070fca637eb54fa0fe2779d07d04aff6d556`，已推送 `origin/feat/platform-implementation`。
- 生产入口：`https://manjuad.gzjunbo.net:8000/`，current 指向 `/opt/tt-ada/releases/d09c070fca637eb54fa0fe2779d07d04aff6d556`。
- 镜像：`tt-ada:d09c070fca637eb54fa0fe2779d07d04aff6d556`；ID `sha256:aacaaeac26632eee683193e230aa93b0c99ba9b9c7adf63611711e6c13b9815c`。
- 前版为 `016217f65a39330b4b715ab043fe866eea87810c`，旧版本目录、镜像和发版前备份保留。本文等验收后追加的文档提交不改变运行镜像。
- 主品牌18px、字重600；副标题11px、文案“广告投放工具”。登录按钮静止状态仅“登录”、无箭头，登录页标题“登录 TK-ADA”，各路由标签和提示统一 TK-ADA。
- 保持原全局 CSS、业务页标题、布局、颜色、卡片和间距；两行品牌文字使用局部排版类。默认配置、本地和生产私有配置 PROJECT_NAME 已同步，OpenAPI 标题为 TK-ADA。
- 原技术目录、Compose项目、镜像前缀、备份服务名、持久卷和端口保留，不因显示名称更新另建数据环境。

## 验证

- TypeScript 与 Vite 生产构建通过，本地 Bun 1.4.2 与生产容器均实际构建成功。
- 33个改动 TS/TSX 文件的 Biome 检查、git diff --check 通过。更新既有登录/导航测试定位器；workspace-shell 22项通过（7.6秒），覆盖登录、权限、移动导航和断点布局。
- 本地实际8011登录页核对品牌18px/11px、名称、按钮“登录”且无SVG，截图通过人工查看。仅重启已核实PID/工作目录的本地API，并更新进程登记。
- 生产实际浏览器只读验收18项通过：admin/junbo登录、平台用户/租户、权限隔离、租户账户/连接/成员/版权方/素材/搭建/任务/策略页面；每页核对品牌文字和字号。无JavaScript异常、5xx或越过保护范围的请求。未新增业务测试记录。
- 生产与本地 bootstrap 均通过，OpenAPI 标题均为 TK-ADA。三个生产 Worker ping 正常，唯一 Beat；原站80/443状态及页面SHA256保持不变。

## 发布与数据保护

1. 新镜像构建成功后暂停备份timer，确认既有备份服务inactive。
2. 正常停止旧API、Beat、三个Worker后执行发布前备份：`/opt/tt-ada/backups/20260910T083221Z-before-tk-ada-brand`。COMPLETE存在，所有摘要校验通过。
3. 只修改生产私有 PROJECT_NAME；执行prestart，Alembic upgrade为空操作，current/唯一head均为 `r2_part_receipts`。
4. 启动新镜像，验收通过后原子切换current。安装新备份单元描述并恢复timer，active；当次下一触发时间为2026-09-11 03:34:11北京时间。
5. 发布前后用户2、租户1、成员1、策略3、策略版本5保持一致；原账号可登录。本轮无迁移，不还原数据库或改写业务记录。

若需回退，按生产规则停写并排空新服务，使用前版镜像恢复，必要时将 PROJECT_NAME 改回前版值；本轮数据库结构相同，无须回退数据库。不得重建持久卷或清空队列。

真实 TikTok/R2/版权方联调边界延续首发记录，本轮未更改外部开关、网络策略或凭据，也未执行真实广告操作。

截图及完整脱敏结果位于忽略目录 `.runtime/production-ui-2026-09-10T08-33-44-173Z/`，本地登录截图为 `.runtime/tk-ada-local-login.png`；运行凭据和原始备份不提交。
