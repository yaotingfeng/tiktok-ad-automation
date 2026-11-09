# 2026-11-09 后台关联报表连续小步

状态：代码与独立审查完成，待联合候选精确发布。

P五分钟material_links209步/204父，仅7完成；HTTP中位373ms，整步2.91s，重新调度及本地工作占比高。S窄样本有合法分页约67分钟未续步。不是已证固定两槽或平台慢，也没有修改缓存TTL/负证据。

后台material_links同一grant最多3步，每步独立提交；继续保留private token/gen/head与授权、HTTP前后TransactionBoundaryReader围栏。worker入口固定20秒reader绝对期限，后继步骤不重给，正常末步事务按原边界收尾；原170/180上限不扩大。完成、依赖、准入、错误或失租停止，最后一次outcome/refill。仅后台links预留主端点3单位，consumer从不可变ledger再次裁剪min(3,reserved_units)；旧grant1只读1步，新grant3给旧consumer1步按真实费用闭合。缺账本不制造授权，按规范退出不留下新空占。

所有出口及循环前无条件核原head，预算二次消失也不能跳过围栏。SDK_SCOPE_INTERRUPTS原样传播，避免后继owner/gen/token变化用ExecutionLost遮盖软中断后闭合未知成本。已发送成本不退，未知发送不冒充完成；当前日/其它kind单步不变。无DDL/新业务开关/平台额度或公平权重变化。

真实PG/Redis：最终30重点7.10秒、原执行边界9项2.89秒；相关105项通过，之前显式跳过的传输基准已以SYNC_COST_TRACE=1补绿（24重点+基准共25项159.99秒）。独立审查2个Important组合路径均先红后绿并复核通过。Ruff/格式/差异通过；严格类型129项与旧源规范化Counter一致，无新增（dispatcher/executions零错）。

完整生产S f674 detached全树验证：campaign_batch_still_truncated旧单项同样COMPLETE!=STALE失败2.17秒；仅覆盖U四源+新测试后30项通过，同一单项同因失败7.78秒。此非U引入，不把含既有失败的整套称全绿；该既有覆盖语义另行诊断，不修改测试掩盖。

上线需核对同grant实际多步/逐步commit、金额/关联事实与费用ledger、后继保护、当前日行为及队列趋势；不以Task成功日志代替父/冻结需求满足，不宣称积压已清零。
