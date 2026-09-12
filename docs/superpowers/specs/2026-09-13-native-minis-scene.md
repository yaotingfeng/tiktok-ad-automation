# 原生 Minis 场景适配

## 真实故障与用户授权

用户授权解决现有草稿的全部卡点并实际提交广告，保留现有剧目、账户、预算及 ROAS。服务器官方 MCP 实际返回：目标账户具备可用 TT_USER 自有身份，BC_AUTH_TT 过滤返回空；TT_USER 返回的 page_info 四项为 0；地区工具拒绝 promotion_type=MINI_APP；IAA 当日 ROAS 为 QUALIFIED，而购买价值 vo_min_roas 为 NOT_SUPPORT。

## 修正边界

- 查询目标账户身份并支持 TT_USER 与当前 BC 的 BC_AUTH_TT；身份必须 AVAILABLE、可推视频、非 GPPPA。自有身份不能填入 BC 授权 ID，BC 身份不能缺实际同 BC ID。多个可用身份仍阻塞，不能任意挑选。
- identity_get 仅第一页且四项严格整数 0 的非分页元数据按有界完整列表解码；普通分页仍严格验证，缺项/重复 ID/跨 BC 继续拒绝。
- 地区查询保留 advertiser_id、APP_PROMOTION、MINIS 和 TikTok placement，去掉该工具不支持的 MINI_APP 枚举；地区和 Minis 的允许国家交集仍来自实际返回。
- 具备 IAA 当日最小 ROAS 资格时使用 IMPRESSION_LEVEL_AD_REVENUE；购买价值资格对应 ACTIVE_PAY。两种资格均不满足仍阻塞。ROAS 数值和预算沿用冻结策略。
- 共享身份合同覆盖场景、广告请求编译、发送和回读；TT_USER 不发送空/虚构 BC 字段。场景合同升级为 v2，重新准备而不复用旧场景事实；不改冻结通道授权代数，不迁移数据库，不增加开关。

## 验证

合成回归验证身份边界、非分页元数据、地区参数、IAA 资格、请求编解码及原有 BC 身份。真实验收继续通过服务器租户 API 生成预览、提交、后台创建及平台回读；模拟通过不作为已创建广告的证据。
