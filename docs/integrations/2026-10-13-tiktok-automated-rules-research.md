# TikTok 自动规则调研报告

- 调研日期:2026-10-13
- 来源:TikTok 广告管理平台「工具 → 自动规则(automated-rules-v2)」创建页实测(广告主 7683446566593675271,中文界面);TikTok API for Business v1.3「自动规则」8 个接口文档;仓库锁定的官方 Python SDK `business_api_client/api/automated_rules_api.py`。
- 调研方式:只读。切换过创建抽屉内的下拉项,未点击「完成」,未创建任何规则,未调用任何写接口。

## 1. 功能总览

自动规则是**单广告主账户内**的「定时检查 → 条件判断 → 执行动作 → 通知」机制。一条规则由四部分组成:

| 区块 | 内容 |
| --- | --- |
| 应用至 | 选择对象层级(推广系列 / 广告组 / 广告)与范围(全部投放中、全部已暂停、特定对象) |
| 条件与操作 | 最多 5 个条件(全部满足才触发,AND 关系);1 个执行操作 |
| 规则设置 | 运行排期(持续 / 自定义 / 仅一次)、通知(邮件) |
| 规则名称 | 默认「规则 yyyyMMddHHmmss」 |

页面左侧导航:管理规则、操作记录、操作日志。空列表时展示 8 个模板(见第 7 节)。

### 平台硬限制(页面提示原文归纳)

- 不适用于预订型广告、「推广」功能创建的广告、TikTok Shop 商家中心创建的广告计划。
- **只应用于最近创建的 500 个广告**。
- 日预算调整不影响创建广告时设置的「预算排期增加」;自动预算规则不适用于已启用「基于目标增加预算」或「定时增加预算」的推广系列。
- 规则按所选时区运行,不支持夏令时变更。
- 时间周期选「今天」时页面提示:可能影响数据预测准确性,谨慎使用。
- API 文档重要提示:**接口目前仅支持直客广告主;规则通知只会发到开发者注册邮箱,管理多个广告主的 SaaS 平台无法用它给各广告主分别发通知。**(对我们的影响见第 9 节)

## 2. 应用至(适用范围)

UI 为两级级联选择:

| 一级 | 二级 | 对应 API `pre_condition_type` |
| --- | --- | --- |
| 处于「投放中」和「部分投放受限」状态 | 所有投放中推广系列 / 广告组 / 广告 | `ALL_ACTIVE_CAMPAIGN` / `ALL_ACTIVE_AD_GROUP` / `ALL_ACTIVE_AD` |
| 已暂停 | 所有已暂停的推广系列 / 广告组 / 广告 | `ALL_INACTIVE_CAMPAIGN` / `ALL_INACTIVE_AD_GROUP` / `ALL_INACTIVE_AD` |
| 特定范围 | 其他推广系列 / 其他广告组 / 其他广告 → 下方出现可搜索的勾选列表(「选择推广系列列表中的特定广告」) | `SELECTED` + `dimension_ids` |

API 另有 UI 未直接暴露的「所选上级下的对象」:`ALL_ACTIVE_AD_GROUP_UNDER_SELECTED`、`ALL_ACTIVE_AD_UNDER_SELECTED`、`ALL_INACTIVE_AD_GROUP_UNDER_SELECTED`、`ALL_INACTIVE_AD_UNDER_SELECTED`(用 `dimension` + `dimension_ids` 声明上级)。

`apply_objects[].dimension`:`CAMPAIGN` / `ADGROUP` / `AD`。**层级决定可用指标**(第 3 节)。

## 3. 条件

### 3.1 条件行结构

`条件(指标) | 计算方法 | 时间周期 | 运算符 | 值 + 单位`,右侧删除按钮;「添加条件 (n/5)」上限 5 条,条件之间为 AND。

各列是否出现、可选什么,随所选指标变化:

- **计算方法**:仅「总花费」「展示数」有,选项「所有对象 / 每个对象」(默认每个对象)。「所有对象」= 范围内全部对象合计后判断。API `calculation_type`:`ALL_OBJECTS` / `OF_EACH_OBJECT`(文档写只支持 `COST`)。
- **时间周期**:今天 / 昨天 / 最近三天 / 最近五天 / 最近七天 / 广告发布期间 → API `range_type`:`TODAY` / `YESTERDAY` / `PAST_THREE_DAYS` / `PAST_FIVE_DAYS` / `PAST_SEVEN_DAYS` / `LIFETIME`。部分指标子集受限(见表)。
- **运算符**:数值类为「大于 / 小于 / 介于(>且<)」→ `GT` / `LT` / `BETWEEN`(介于需 2 个值);「创建天数」额外有「等于」→ `MATCH`;名称类为「包含 / 不包含 / 名称开头 / 名称结尾 / 是」→ `CONTAINS` / `NOT_CONTAINS` / `START_WITH` / `END_WITH` / `STRING_EQUAL`;枚举类只有「是」+ 下拉值。
- **单位**:金额类显示账户币种(USD),比率类 `%`,天数类「天」,其余无单位。

### 3.2 推广系列层级可选指标(UI 实测 56 项)

列说明:计算 = 有无计算方法;时间 = 可选时间周期(全 = 今天/昨天/3天/5天/7天/发布期间;无今日 = 昨天/3天/5天/7天);API = 文档中的 `subject_type`(— 表示 API 文档未列出)。

**Settings(设置)**

| 指标 | 运算符 / 值 | API |
| --- | --- | --- |
| 推广目标 | 是:访问量 / 视频播放量 / 覆盖人数 / 线索收集 / 网站转化量 / 社区互动 / 商品销量 / 应用推广 | `OBJECTIVE`(值 TRAFFIC / APP_PROMOTION / WEB_CONVERSIONS / VIDEO_VIEWS / REACH / LEAD_GENERATION) |
| 预算类型 | 是:日预算 / 总预算 | — |
| 名称 | 包含 / 不包含 / 名称开头 / 名称结尾 / 是,文本 ≤512 字 | `NAME` |
| 创建天数 | > / < / 介于 / =,单位天 | `DAYS_SINCE_CREATION` |

**Basic data(基础数据)**

| 指标 | 计算 | 时间 | 单位 | API |
| --- | --- | --- | --- | --- |
| 日预算消耗率 | 无 | 仅今天 | % | `DAILY_BUDGET_SPENDING_RATE` |
| 总预算消耗率 | 无 | 仅发布期间 | % | `LIFETIME_BUDGET_SPENDING_RATE` |
| 总花费 | 有 | 全 | USD | `COST` |
| 平均点击成本(目标页面) | 无 | 全 | USD | `CPC` |
| CPM | 无 | 全 | USD | `CPM` |
| 展示数 | 有 | 全 | — | `IMPRESSION` |
| 点击量(目标页面) | 无 | 全 | — | `CLICK` |
| 点击率(目标页面) | 无 | 全 | % | `CTR` |
| 覆盖人数 | 无 | 无今日 | — | — |
| 覆盖千人成本 | 无 | 无今日 | USD | — |
| 频次 | 无 | 无今日 | — | — |
| 转化数 | 无 | 全 | — | `CONVERSION` |
| 平均转化成本 | 无 | 全 | USD | `CPA` |
| 转化率 (CVR) | 无 | 全 | % | `CVR` |
| 实时转化 | 无 | 全 | — | 推测 `CONVERSION`/`NOT_REAL_TIME_CONVERSION` 成对,待联调确认 |
| 实时平均转化成本 | 无 | 全 | USD | 同上(`NOT_REAL_TIME_CPA`) |
| 实时转化率 (CVR) | 无 | 全 | % | 同上(`NOT_REAL_TIME_CVR`) |
| 转化量 (SKAN) | 无 | 全 | — | `CONVERSION_SKAN`(仅出现在校验表) |
| 平均转化成本 (SKAN) | 无 | 全 | USD | — |
| 转化率(SKAN,展示) | 无 | 全 | % | `CVR_SKAN`(仅出现在校验表) |
| 平均付费成本(网站) | 无 | 全 | USD | — |
| 平均付费成本(所有渠道) | 无 | 全 | USD | — |

**Video play(视频播放)**:视频播放量、视频播放 2 秒次数、视频播放 6 秒次数、播放完成次数、播放 75% / 50% / 25% 次数(时间全);任务视频 6 秒完播率(%)、平均视频播放时长、用户平均播放时长(无今日)。API 均未列出。

**Engagement(互动)**:点击数(全部)、点击率(全部,%)、付费关注数、广告跳过数(时间全)。API 未列出。

**Interactive add-on(互动组件)**:创新互动样式展示次数 / 目标页面点击数 / 操作点击数(时间全)。API 未列出。

**LIVE**:直播播放量(时间全)。API 未列出。

**In-app event(应用内事件)**:广告花费回报-付费 → `ROAS_PURCHASE`;付费 ROAS (SKAN) → `ROAS_PURCHASE_SKAN`;广告展示 ROAS → `ROAS_IMPRESSION_ADS`(时间全)。

**Page event(页面事件)**:支付完成 ROAS → `ROAS_COMPLETE_PAYMENT`。

**Shop event(商店事件)**:ROAS(商店)→ `ROAS_PURCHASES_ONSITE`;付费数 / 平均付费成本(USD)/ 付费率(%)/ 平均订单价值(USD)/ 总收入(USD)/ 开始结账数 / 商品页浏览数(商店)。后 7 项 API 未列出。

### 3.3 广告组层级的差异

Settings 变为:预算类型、**自排期开始日期以来的天数**(> / < / 介于)、**出价策略**、**优化目标**、**优化事件**、名称、创建天数(无「推广目标」)。

- 出价策略(是):成本上限 / 出价上限 / 最大投放量 / 最高价值 / 最高总收入 / 广告花费回报(ROAS)保本 / 目标 CPA(仅限店铺广告)/ 目标 ROAS(仅限店铺广告)。API `BID_STRATEGY` 值仅列 COST_CAP / BID_CAP / MAX_CONVERSION / LOWEST_COST。
- 优化目标(是):覆盖人数 / 播放2s次数 / 播放 6 秒次数(专注观看)/ 观看 15 秒次数(专注观看)/ TikTok 页面访问 / 账号关注 / 落地页浏览量 / 目标页面访问 / 转化 / 线索量 / 转化线索量 / 会话数 / 安装 / 应用内事件 / 金额(目标 ROAS)/ 观众留存率 / 直播中商品点击数 / 开始结账数 / 付费数 / 总收入 / 偏好线索 / 点击 / 展现。API `OPTIMIZATION_GOAL` 值仅列 9 个。
- 优化事件(是):查看内容 / 加入购物车 / 下单事件 / 付费数 / 完成注册 / 添加付款信息 / 开始结账数 / 搜索 / 加入心愿单 / 下载 / 点击按钮 / 联系 / 订阅 / 提交表单。API 未列出。

Basic data 额外出现:实时单次成效费用、实时成效数、实时成效率(时间全)、平均成效成本(无今日,USD)→ `COST_PER_RESULT`、成效数 → `RESULT`、成效率(无今日,%)→ `RESULT_RATE`、深层目标成效数 / 平均成本 / 成效率、计费比例。

### 3.4 值校验(API)

| 指标类 | 精度 | 范围 |
| --- | --- | --- |
| NAME | — | 长度 (0, 512] |
| COST / CPA / NOT_REAL_TIME_CPA / CPM / CPC / COST_PER_RESULT | 账户币种精度 | [0, 100000000] |
| 各类消耗率、CVR、CTR、RESULT_RATE | 0.01 | [0, 100] |
| IMPRESSION / CLICK / CONVERSION / RESULT 等计数 | 1 | [0, 10000000000] |
| 各类 ROAS | 0.01 | [0, 100000] |

API 还有 `NO_CONDITION`(无条件,配合定时开关使用,即「计划投放」模板)。

### 3.5 UI 与 API 的落差(结论)

UI 约 70 项指标,API 文档只列 28 个 `subject_type`。视频播放、互动、商店事件、覆盖/频次、成效与深层目标、优化事件等**只能在 UI 配,API 文档未承诺**。是否被接口接受需联调实测;方案设计时 API 能力以文档清单为准。

## 4. 执行操作

每条规则只能选 **1 个**操作:

| UI | API `actions[].subject_type` | 额外配置 |
| --- | --- | --- |
| 启用 | `TURN_ON` | 无 |
| 暂停 | `TURN_OFF` | 无 |
| 仅发送通知 | `MESSAGE` | 无 |
| 调整日预算 | `DAILY_BUDGET` | 调整方式 + 数值 + 上/下限 + 调整频控 |
| 调整总预算 | `LIFETIME_BUDGET` | 同上 |
| 调整出价 | `BID` | 同上 |

调整类配置:

- 调整方式:提高幅度 / 降低幅度 / 调整至 → `action_type`:`INCREASE` / `DECREASE` / `ADJUST_TO`。
- 数值类型:`value_type`:`EXACT`(金额)/ `PERCENT`(百分比)。
- `value.value`:增减时 [0.01, 1e12];调整至时推广系列 ≥50、广告组 ≥20。
- `value.limit` + `use_limit`:提高时为上限、降低时为下限(推广系列 ≥50、广告组 ≥20)。例:提高 20%、上限 100 USD。
- 调整频控 `frequency_info.type`:仅一次 `ONLY_ONCE` / 每 24 小时一次 `ONCE_IN_24_H` / 每 48 小时 `ONCE_IN_48_H` / 每周 `ONCE_IN_1_W` / 自定义 `CUSTOM`(`N_MINTUE_Y_TIMES` / `N_HOUR_Y_TIMES` / `N_DAY_Y_TIMES`,N=`time`,Y=`count`)。UI 实测在「持续」排期下频控固定「仅一次」且不可改。

## 5. 规则设置

### 5.1 规则排期

| UI | API `rule_exec_info.exec_time_type` | 配置 |
| --- | --- | --- |
| 持续:每 30 分钟运行一次 | `PER_HALF_HOUR` | 无 |
| 自定义:特定日期和时间段内每 30 分钟运行 | `HALF_HOUR_IN_SPECIFIC_TIME_PERIOD` | 频次 按天/按周/按月(`date_type` DAY/WEEK/MONTH,`num` 周 1–7、月 1–28);时区;时间段「首次运行时间 / 末次运行时间」,可添加最多 7 段 |
| 仅一次:运行后自动关闭 | `SPECIFIC_TIME_ACCURATE_ONCE` | 时区 + 运行时间 `YYYY-MM-DD HH:MM`,不超过一年后 |
| (UI 未暴露)每天固定时刻 | `CUSTOM` | `exec_time` = `HH:MM` |

### 5.2 通知(本次不沿用,改飞书)

UI:开关「并发送通知」,接收者(BC 成员多选),通知安排「每次通知 / 在特定时间统一发送通知」,勾选「不满足条件时也发送通知」。API:`notification_type`(`TASK_FINISH` 仅配 MESSAGE / `ANY_CHANGES` / `NOT_NOTIFICATION`)、`email_setting`(`notification_period`、`email_exec_time`、`no_result_notification`、`mute_option`)。

## 6. 接口清单(v1.3,官方 SDK `AutomatedRulesApi` 均已覆盖)

| 接口 | 方法 | 路径 | 说明 |
| --- | --- | --- | --- |
| 创建规则 | POST | `/optimizer/rule/create/` | `rules[]` 可批量,返回 `rule_ids` |
| 根据 ID 获取 | GET | `/optimizer/rule/get/` | `rule_ids` |
| 按条件获取 | GET | `/optimizer/rule/list/` | 筛选 status(ON/OFF/DELETED)、rule_info(ID/名称)、data_dimension;分页 |
| 获取规则结果 | GET | `/optimizer/rule/result/list/` | 筛选 status、rule_info、action、time(UTC)、data_dimension;返回每条规则最近一次 `change_success / no_change / change_fail`、`task_exec_id` |
| 获取结果详情 | GET | `/optimizer/rule/result/get/` | 入参 `{exec_id, rule_id}[]`;返回每个对象的动作、SUCCESS/FAIL、错误信息、对象 ID/名称 |
| 更新规则 | POST | `/optimizer/rule/update/` | 全量覆盖,需带 `rule_id` |
| 更新规则状态 | POST | `/optimizer/rule/update/status/` | `TURN_ON` / `TURN_OFF` / `DELETE`,批量原子(任一失败全部不更新) |
| 规则绑定/解绑 | POST | `/optimizer/rule/batch_bind/` | 把已有规则绑定/解绑到更多对象 |

所有接口均以 `advertiser_id` 为作用域,**没有跨广告主的规则**。

## 7. 内置模板

计划投放(在特定时间开启广告)、发掘潜力(提高具备潜力广告组的出价)、提升效果(为效果好的广告加预算)、控制预算(关闭消耗超预期的广告)、获得转化量(提高出价拿更多转化)、加热效果出色的最大投放量广告(提高其预算)、加热效果出色的成本上限广告(提高其预算)、控制平均转化成本(停止效果不佳的广告组)。

## 8. 交互设计要点(值得沿用)

1. 右侧抽屉式编辑,四块卡片纵向排列,底部固定「完成」。
2. 指标用二级级联(分类 → 指标),选完后同一行的计算方法/时间周期/运算符/单位联动变化,不适用的列直接隐藏。
3. 范围用二级级联,选「特定范围」后在下方内嵌带搜索的勾选列表。
4. 模板卡片「采用 / 预览」,一键带出预设条件。
5. 风险提示就地展示(「今天」时间周期、预算排期冲突、SKAN 4.0)。

## 9. 对 TK-ADA 的影响与风险

1. **只能单账户**:规则、结果、绑定全部按 `advertiser_id` 隔离;多账户、按剧、全账户维度必须由 TK-ADA 自己做一层。
2. **「仅支持直客」声明**:我们的账户多为 BC/代理托管,接口是否会拒绝需先实测一次(需授权的真实写入)。若被拒,原生规则路线不可用。
3. **通知**:原生通知只发开发者邮箱,飞书通知必须由 TK-ADA 轮询 `result/list` + `result/get` 后自行推送。
4. **500 广告上限**:大量铺剧的账户,较早创建的广告可能不受规则覆盖。
5. **指标落差**:API 文档 28 个指标远少于 UI;以文档为准设计,扩展项需联调确认。
6. **剧维度**:原生规则无剧概念。可用「名称包含剧 ID」近似筛选单个对象;但「某剧全部账户合计花费/ROAS」这类聚合判断原生做不到。TK-ADA 报表已有按剧名归组(`reporting` 剧维度)和 30 分钟热数据同步(`HOT_INTERVAL`),具备自建聚合判断的数据基础。
7. **执行通道**:TK-ADA 已有 `ad_management` 的状态/预算/ROAS 批量变更(`MutationField = roas | budget | status`),自建引擎可复用;出价调整需补齐。

## 10. 写入验证结果(2026-10-13)

- 范围:新骏伯 BC,MCP 连接 `tiktok-ads-new-junbo`,账户 `7683818840198987797`({LemonShow Minis}-MAX-广西智谱-6140-2-p1,角色 `ROLE_CHILD_ADVERTISER`,状态 `STATUS_ENABLE`)。
- `optimizer_rule_create`(测试规则:已暂停推广系列、点击 > 9999999999、仅通知、不发邮件、2027-10-01 单次)返回 `code 40002`:`Insufficient permission: Please ask the BC admin with resource permission to grant access to the advertiser.`(request_id `20261013232621ECA0F7CCB1CD451716C1`)。未创建任何规则。
- 同一账户 `optimizer_rule_list_get` 返回 `code 0`,规则数 0:读取可用,写入被拒。
- 结论:在 BC 子账户上,原生规则写入不可用,与文档中「仅支持直客广告主」的限制相符。另一种解释(授权用户在该账户只有只读资产权限)尚未完全排除:本次没有用同一连接对该账户做其他写入来对照。

### 10.1 补充对照验证(同一账户)

- 用户在网页后台为 `7683818840198987797` 手动创建规则「规则 20261013083333」(rule_id `7696168194428731412`,推广系列 / 所有投放中,总花费(每个对象,今天)> 1000.00,仅发送通知,每 30 分钟,状态 ON)。
- API `optimizer_rule_list_get` 能读到该规则,字段与网页一致:读取通道可用,网页建的规则可被导入。
- API `optimizer_rule_create` 按该规则的字段格式再试一次,仍返回 `40002`(request_id `20261013233640CC8D696D01EC95B53210`)。
- 授权用户在新骏伯 BC「麦斯国际运营522」(`7683817908149272592`)为 ADMIN;对该账户的资产权限为 ADMIN。该账户 `owner_bc_name` 为「Max Engine Number 3」(代理商 BC 拥有,分享给新骏伯 BC)。
- 结论:排除权限不足。原生规则在开放 API 上只允许直客账户写入;代理商开户、分享到我方 BC 的账户无法通过 API 创建、更新规则。TK-ADA 改为自建规则引擎(方案 B)。

## 11. 2026-10-18：父级状态与人工停投保护补充核对

用户反馈累计ROAS恢复规则重新开启九月份父级已停投的历史素材，要求核对状态能否参与规则判断。

- 本报告第2节记录原生TikTok把投放中、已暂停、特定对象放在应用范围pre_condition_type；还支持在选定父级之下筛选子级。第3节的UI实测设置指标以及当前官方“Automated Rule Conditions”清单，没有提供可据以确认“系列/广告组/广告状态分别作为普通指标条件”的证据。不能表述成已核实TikTok原生提供这些指标而TK-ADA漏抄。
- 当前官方参考：https://ads.tiktok.com/resources/help/article/automated-rule-conditions?redirected=2 （设置条件与表现条件分开）；https://ads.tiktok.com/resources/help/article/ad-statuses-and-definitions-in-ads-manager （广告状态包含系列暂停、广告组暂停等父级原因）。官方文档未承诺自动规则能判断人工暂停来源。
- TK-ADA现状：catalog仅提供名称、创建天数、预算、ROAS出价等对象属性；没有层级状态枚举条件。in_rule_scope只在素材ACTIVE范围过滤parents_enabled=false；PAUSED恢复只判断素材自身DISABLE，父级关闭仍可命中。这正是本批历史素材被重新开启的范围缺口。
- 使用本批已保存240项候选只读回放：增加父级广告/广告组/系列全部开启筛选后剩2项，继续要求有本组规则的历史PAUSE记录后剩1项。此回放不是执行，父级值来自已有目录快照。
- 后续方案建议：把账户状态、系列/广告组/广告开关做可选择的枚举筛选；素材恢复默认要求所有父级开关开启，来源明确为自动止损且无后续人工覆盖。状态过滤与表现条件分组显示并同时满足，避免占满现有5个表现条件；合计口径在合计前逐个筛选成员，不能拿第一个成员的状态代表整体。执行前重新核对父级，缺失/过期/不明就跳过，不顺带开启父级。
- 账户=Advertiser，广告组=Ad Group，二者不是同一层级。账户审核/受限等资格状态与系列/广告组/广告启停开关需分别展示。状态启用也不等于有余额、预算、排期和审核资格。
- 仅父级状态过滤不能判断“上级还开着、但人工单独停了素材”的原因。系统内人工操作可结合管理记录判断；平台外同状态重复暂停不能从一次状态快照读出来源，需要可靠变更记录或明确人工排除标记，来源不明不得推定自动暂停。

以上为研究结论和待实现方案，尚未修改产品条件或执行逻辑。生产恢复规则保持暂停；既有239项回退提案未因本段讨论被视为已获确认。
