# AI 工作台管理范围及 DSH 会话修复

已按用户授权提交、推送 GitHub 与公司 main，并发布既有生产入口 `https://tkada.shortclipflix.com`（ACK/test）。

## 界面修复

- 顶部“继承上下文”与投手配置不再展开全量账户 ID，改为账户/规则数量及绑定状态摘要。
- BC 与账户合并在“管理范围”，切换 BC 清空旧账户。
- 复用广告搭建账户选择器，支持搜索、分页、全选本页、全选筛选结果、清空与确认/取消。已有绑定可以重新选择；排除其他 BC 和所有权冲突账户。
- 自动规则弹窗支持名称/ID 搜索、分页、批量选择、清空与草稿取消。
- 工作台原27项、新增手机/桌面2项、受影响账户选择器3项通过；TypeScript 与生产/演示构建通过。生产只读打开现有50账户投手，数量摘要、两个选择器及无横向溢出已核对；未保存生产投手设置。

TK-ADA：`5f7750adcc58c135830558005da113243a277ca5`，镜像摘要 `sha256:e8e5f23054da00668ce9f2ac961509ead1254179f6c51b3d544f49c3000a8e38`。11角色Ready，逐角色正常退出并更新镜像，运行配置与资源不变。

## DSH 修复与真实验证

原因：Host plugin-manager 被关闭，而官方 standard Agent 预设仍启用依赖它的 tool-plugin-manager，导致 `session/create` 返回 `agent-preset/invalid`，插件管理页亦不可用。之前只有页面与工作区启动验证，未覆盖真实会话装配。

- 恢复官方 plugin-manager；HMR仍关闭，新安装插件按官方界面提示重启生效。
- 安装固定pnpm10.34.9，缓存和依赖存储使用原持久卷可写目录。
- 重启保留 profile 内管理员安装的依赖、额外 bundle 和模型设置；部署仍管理必需的宿主接入行。
- 12项原生测试在本地与最终AMD64镜像均通过，新增真实standard会话创建和插件清单保全验证。
- 生产同一session/create请求：修复前返回waiting for pluginManager，修复后返回ok=true与新sessionId。
- 浏览器实际点击新建会话，发送仅要求回复“连接成功”的连通性消息，现有com/claude-opus-5-5已成功回复；未调用工具，未改文件或业务数据。
- 插件列表、添加插件按钮及内置子智能体配置页正常可读。pnpm在真实profile目录返回持久存储路径 `/var/lib/dsh/data/pnpm/store/v10`。未安装任意第三方插件，未将列表可读声称为所有外部插件均已联调。

DSH：`fcf90354980dc9496a32e0ca2da29a2bad951ecc`，镜像摘要 `sha256:56f530252058df5a4da7c68ff2e8cbef9f9c871091567c512fdbc8cb96b38399`。独立Recreate发布，Ready；模型Secret、PVC、Service及11个TK-ADA应用部署保持不变。

本批无数据库迁移、后台任务合同或开关变化；共享发布锁与CI协调保留，未新建备份、未执行恢复演练，未进行真实广告或素材下单。
