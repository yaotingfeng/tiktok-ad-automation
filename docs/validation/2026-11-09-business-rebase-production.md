# 2026-11-09 经营季度重建追赶生产验收

应用 `2a7569b96a5efcac2338b000fc4c240985cabcac` 在保留公司最新主分支其它已发布改动后精确双端推送、构建并完成生产验收；平台摘要 `sha256:de0b9ddcaf7501f1a297f0c89463188aa038813a86fbfd5aeac73b2e54f07c56`。独立源码审查无Critical/Important；business101项及500硬上限1项通过，精确候选20通过，文档父提交归并前后整个backend Git树相同。34.56MB真实规模输入整链VERIFY变更8.14秒、稳定发布3.46秒、进程峰值797MiB，均是本地测试数据，不冒称生产耗时。

新增VERIFY/REBASE为持久阶段，无DDL。发布先取消Reporting消费，连续两次active/reserved/scheduled/queue全0，scale0确认旧Pod UID全部消失；新consumer scale1前落盘forward-only，随后精确digest/八份源码/真实12fork通过才更新其它生产者。后台12正常排空零Pod、Beat最后，全11角色/schema0101/配置与四开关/DB池/后台typed12与prefetch12/CPU3内存10Gi/publisher8保持。锁释放、CI恢复；按既有授权无新备份或恢复演练。禁止回退不认识新阶段的旧程序。

## 实际故障与收敛

旧程序两个大BC连续三轮在SUBSIDY末尾因来源更新整批SUPERSEDED，再复制整个季度。精确冻结版本差异只在11/08各8个账号日，包含版本/观察/更新时间，其中一个BC另2个count变化，不能放松真实版本fence。新程序只有结构/授权/计算与目录等不变时，保存真实新snapshot、找最早dirty、每批最多500原子删除候选尾部day及summary，再重算尾部；稳定前保留旧正式版本。

上线Reporting开始于04:04:13 UTC。前2/5/10min的进度仅COPY，不能报已验收：第一大BC先复制598081日明细及350000汇总，第二BC8,500日明细。04:21:22 UTC实读两大BC原UUID均COMPLETE、正式Q4切至原候选且截止11/08，day/summary非空、manifest一致；各自REBASE1轮、dirty11/08，候选尾部删除day5496/5266和summary3778/5947。两组旧前缀确定PK均4/4内容一致。处理计数10992/10533是累计尾部处理量，不能当净事实行数。

同期生产由另一已授权策略修复推进到0549cd4278abc2a2f653a85dcb403966a5b2e42c；已逐一核整个business/reporting/sync_control Git树与本次应用相同。04:22:24 UTC小BC跨季度门禁也正常完成Q3并原子切manifest；三个BC scope/published revision为4/4、6/6、23/23，六份真正用户可读的正式季度均COMPLETE、day/summary非空，Q4全截至11/08。小BC结构scope变化不能要求保留原前缀。

一次性通过原生business.work发送60个短有效期唤醒（expires3、retryFalse），期间不创建业务候选、不直接改事实/水位，已有BC/run锁与预算保持。60条仅确认发送回执；旧Reporting Pod随兼容发布退役，具体执行/过期分布unknown，不能把这60当完成或归因倍数。生产正式结果来自独立只读DB回验，不由发送回执推断。无需为本轮正式发布再发送脉冲。

私有证据：hotfix1109ad、business-rebase-verify1109ad/postboost-result、business-catchup1109ad，均位于`.runtime/sync-production-20261031/`。

## 尚未完成的队列目标

12:22北京时间队列目录4219/报表5090/总9309，尚未清空。上述经营报表收敛不能解释或代替目录/补链队列清空。后台已测近满且大量时间在真实远端读取，后续容量只在同代码实测吞吐与资源约束下调整，不能无条件取消真实未满足历史需求，也不把先前590去重计同步完成。
