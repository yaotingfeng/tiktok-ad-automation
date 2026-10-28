# 目录同步任务死循环问题 - 完整根因分析和解决方案

**日期**: 2026-10-28
**问题**: ad_directory_run 任务死循环，claim_generation 达到 50-78 次

---

## 问题现象

- 队列积压：27,000+ 任务
- 死循环任务：部分任务重试 50-78 次仍在循环
- 错误码：`directory_publish_retry`
- 状态：`WAITING_REMOTE`
- 队列几乎不下降，系统资源浪费

---

## 🎯 真正的根本原因（完整因果链）

### 源头：历史数据污染 🔥

```
表统计：
- 总行数: 1,792,658 (179万行)
- 已完成: 1,633,457 (163万行垃圾！)
- 待处理: 26,258 
- 表大小: 3131 MB (3GB!)
```

**问题**：
- 163万行已完成的历史记录**从未清理**
- 占用 3GB 空间，拖慢所有查询
- 即使使用主键查询，也因为表太大而受影响

### 第二层：锁竞争

```python
# 代码中的查询
.with_for_update()  # ❌ 没有 skip_locked
```

**恶化过程**：
1. Worker A 锁定一行，开始处理（可能调用外部 TikTok API，10-30秒）
2. Worker B 尝试锁定同一行，**阻塞等待** Worker A
3. Worker B 等待时间过长，被 Celery 软超时杀死（30-60秒）
4. 触发 `SoftTimeLimitExceeded` 或 `DBAPIError`
5. 代码判定为可重试，`claim_generation++`
6. 2分钟后重试，遇到同样的问题
7. 循环往复，最终 claim_generation 达到 50+

### 第三层：代码缺陷

```python
# 原代码
run.claim_generation += 1  # ❌ 无上限检查
```

**后果**：
- 一旦进入重试循环，永远无法自动停止
- 成为僵尸任务

---

## 完整的恶性循环

```
163万行历史数据 
    ↓
表太大（3GB），所有查询变慢
    ↓  
查询+锁等待时间变长（10-30秒）
    ↓
持有锁期间可能在调用外部API（更慢）
    ↓
其他事务等待同一行的锁
    ↓
等待超过 Celery 软超时（30-60秒）
    ↓
被杀死，触发 SoftTimeLimitExceeded/DBAPIError
    ↓
代码判定为可重试
    ↓
claim_generation++，2分钟后重试
    ↓
重试时遇到同样的慢查询+锁等待
    ↓
无限循环，最终 claim_generation > 50
    ↓
占用系统资源，队列无法消化
```

---

## ✅ 已完成的修复

### 修复 1: 添加重试上限（止血）
```python
# 添加常量
MAX_CLAIM_GENERATION = 100

# 添加检查
if run.claim_generation >= MAX_CLAIM_GENERATION:
    run.status = "FAILED"
    run.error_code = "max_retries_exceeded"
    return "FAILED"
```
**效果**：防止无限重试，僵尸任务最多重试100次后自动失败

### 修复 2: 添加 skip_locked（减少锁等待）
```python
# 修改前
.with_for_update()

# 修改后
.with_for_update(skip_locked=True)
```
**效果**：
- Worker 不再等待已锁定的行
- 直接跳过，处理下一个任务
- 大幅减少锁等待时间

### 修复 3: 手动清理僵尸任务
```sql
UPDATE ad_directory_run
SET status = 'FAILED', error_code = 'max_retries_exceeded'
WHERE claim_generation > 50;
```
**效果**：清理了 3 个死循环任务

**当前结果**：
- 队列正常下降：27,598 → 26,994（约 85个/分钟）
- 系统稳定运行

---

## ⚠️ 待执行的关键修复（治本）

### P0: 清理历史数据 🔥 **最重要**

**现状**：163万行垃圾数据占用 3GB

**清理脚本**：`backend/scripts/cleanup_ad_directory_history.py`

```bash
# 1. 预览（DRY RUN）
cd backend
uv run python scripts/cleanup_ad_directory_history.py

# 2. 确认后执行清理（需修改 DRY_RUN = False）
uv run python scripts/cleanup_ad_directory_history.py

# 3. 回收空间
psql -c "VACUUM ANALYZE ad_directory_run;"
```

**预期效果**：
- 删除 30 天前的 COMPLETE 记录（约 150万行）
- 释放约 2.5-2.8 GB 空间
- 查询速度显著提升（预计 5-10倍）
- 锁等待时间大幅减少

### P1: 添加定时清理任务

```python
# Celery Beat 定时任务
@celery_app.task
def cleanup_old_directory_runs():
    """每天清理 30 天前的已完成记录"""
    # 调用清理脚本逻辑
```

**防止问题再次发生**

---

## 次要优化建议

### 1. 数据库配置（可选）
```sql
-- 设置合理的超时，快速失败
ALTER DATABASE app SET statement_timeout = '30000';  -- 30秒
ALTER DATABASE app SET lock_timeout = '5000';        -- 5秒
```

### 2. 监控告警
- `claim_generation > 10` 时告警
- 表大小超过 1GB 时告警
- 监控查询延迟

### 3. 考虑分区表（长期）
```sql
-- 按月分区，自动删除旧分区
CREATE TABLE ad_directory_run (...) PARTITION BY RANGE (created_at);
```

---

## 验证指标

### 清理历史数据后应该看到：
1. 表大小：3GB → 300-500MB
2. 查询速度：提升 5-10 倍
3. 锁等待：几乎消失
4. `claim_generation`：很少超过 10

### 成功标准（7天观察期）：
- ✅ 无新的 `claim_generation > 20` 任务
- ✅ 队列处理速度稳定在 100+ 个/分钟
- ✅ 无任务超时重试
- ✅ 数据库锁等待 < 1%

---

## 总结

### 为什么之前的分析不够准确？

1. **第一次分析**：只看到代码没有重试上限 → 治标不治本
2. **第二次分析**：认为是数据库配置问题（超时=0）→ 是恶化因素但不是源头
3. **第三次分析**：找到真正源头 → **163万行历史垃圾数据**

### 关键教训

> **"慢查询的根源往往不是查询本身，而是数据！"**

- 查询优化的前提是**数据干净**
- 索引再好，也扛不住百万级垃圾数据
- **定期清理历史数据是系统健康的基础**

### 优先级

1. 🔥 **P0**: 清理历史数据（最重要，治本）
2. ⚠️ **P1**: 添加定时清理（防止复发）
3. ℹ️ **P2**: 数据库配置优化（锦上添花）

---

**代码提交**：
- `7f424a1d`: 添加重试上限
- `e8a1c9d6`: 添加 skip_locked + 清理脚本

**相关文档**：
- 完整分析：`.runtime/true-root-cause-20261028.md`
- 修复说明：`.runtime/directory-retry-deadloop-fix-20261028.md`
