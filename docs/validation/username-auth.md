# 普通账号登录与 0015 迁移

平台身份现在为 3–64 位 ASCII 字母、数字、下划线、点或短横线，首尾空白去除、大小写统一为小写。数据库同时约束格式与全局唯一。JWT 的 subject 仍是用户 UUID，密码哈希、成员关系和操作人外键均保留。平台管理员可在用户编辑中设置新密码；用户仍可凭旧密码修改自己的密码。忘记密码页面只引导联系平台管理员。

本系统邮箱列、邮箱 DTO、邮件找回/重置/测试邮件端点、邮件模板及 SMTP 配置已移除。旧迁移中的 email 必须保留，以便重放原 schema；网眼第三方登录 email 和证书 ACME_EMAIL 不属于平台用户邮箱，保持原合同。`emails`、`dkimpy`、`puremagic` 依赖移除；框架仍间接需要的 email-validator/Jinja2 不强制删除。

## 主环境升级（由集成负责人执行）

1. 停止应用写入与后台 worker；确认当前 Alembic head 为 `0014_recovery_candidates`。在权限为 0700 的私有目录创建完整 PostgreSQL custom-format 备份，备份文件权限 0600。连接信息只通过环境/受限 passfile 传入，不放命令参数或日志。先将备份恢复到独立的 `*_test` 库验证。
2. 升级前导出完整映射。权威 helper 是 `backend/app/alembic/versions/0015_username_auth.py::username_mapping(rows)`，输入为有 `id`、`email` 属性的 SQLAlchemy 行。下面代码在 backend 目录运行，不导入应用 Settings，因此可在原 FIRST_SUPERUSER 仍是旧邮箱时使用。指定私有的绝对输出路径，文件使用排他创建，不覆盖已有映射。

```python
import csv
import importlib.util
import os
from pathlib import Path
from dotenv import dotenv_values
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url

config = dotenv_values("../.env")
url = make_url(config["DATABASE_URL"]).set(drivername="postgresql+psycopg")
spec = importlib.util.spec_from_file_location(
    "username_migration", "app/alembic/versions/0015_username_auth.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)
engine = create_engine(url)
with engine.connect() as connection:
    rows = connection.execute(text('SELECT id, email FROM "user"')).all()
old = {row.id: row.email for row in rows}
# USERNAME_MAPPING_PATH 必须指向备份私有目录；不输出到终端。
path = Path(os.environ["USERNAME_MAPPING_PATH"])
assert path.is_absolute()
fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
with os.fdopen(fd, "w", newline="") as output:
    writer = csv.writer(output)
    writer.writerow(["user_id", "old_identifier", "username"])
    for user_id, username in module.username_mapping(rows):
        writer.writerow([str(user_id), old[user_id], username])
engine.dispose()
```

3. 对应私有映射更新 `.env` 中 `FIRST_SUPERUSER` 的值。**变量名不变**；原 `admin@example.com` 对应 `admin`。密码无需更改或输出。删除不再使用的 SMTP/EMAILS_FROM 环境配置。
4. 在已恢复备份的隔离测试库运行 `PYTHONPATH=. alembic upgrade head`、`alembic check`，验证用户总数、UUID、哈希、成员与审计关系以及新账号登录后，再对主库执行同样迁移。不得 reset DB。部署应用与 worker 后用原密码、新账号检查权限和业务页面。
5. 映射和备份含旧身份信息，保持私有，不提交 Git，不贴入报告。0015 会彻底删除原 email 列，**自动 downgrade 明确失败**；若需回退，停止写入并恢复升级前完整备份及对应旧应用版本，不能承诺保留升级后的新写入。

## 映射规则

优先处理 `admin@example.com`，保证得到 `admin`。其余按用户 UUID 确定顺序：取小写前缀，非法字符替换为短横线，截断为 64；过短/纯非 ASCII 前缀使用 `user-<完整 UUID hex>`。冲突追加完整 UUID 后缀并缩短前缀，仍冲突则加确定性计数。映射不依赖数据库返回顺序，备份恢复后结果一致。

## 验证记录

- 先红后绿：普通 username 创建、邮箱表单/旧邮件端点拒绝；真实唯一索引冲突返回 409 且 session 可继续使用。
- 相关 API、CRUD、租户和 submission 读取：133 项通过。
- 最终 core + 0015 + 账号专项：48 项通过（账号/迁移18项）。真实 pg_dump/pg_restore 两库升级，包括同前缀、大小写、中文、符号、超长值及 admin 冲突；保留 UUID/hash/创建时间/活跃及管理员状态、普通 operator 成员关系和审计 actor；两库映射一致，Alembic check 通过。
- 真实 API Chromium 37 项通过：管理员创建/改名/设新密码再登录、自改密码、普通成员无平台权限但保留登录、邮箱格式拒绝、联系管理员找回。原有暗色主题与权限重定向断言已同步为现有浅色/403 页面行为。
- TypeScript 全量（包含 tests）与生产 build 通过；Ty 10 个生产文件、Mypy 14 个相关文件、Ruff 全部改动 Python、Biome 31 个前端文件通过；uv lock --check --offline 通过。
- 真实准备/冻结和 HTTP 租户隔离、平台代管身份及版权方协议 36 项通过；新增 browser 第二租户较长标签的账号 fixture 回归通过，防止账号超过 64 位。
- 工作台全部263用例已覆盖通过：已构建静态应用262项通过；唯一开发HTML harness（BulkAccountInput）在隔离Vite单独1项通过。静态运行该开发路径不会存在，因此该项按实际harness运行。开发服务器曾被并行auth结果目录写入触发page reload，导致Sheet卸载；切静态构建消除干扰，两个原失败用例通过，未放宽超时或断言。
- 本次自建username_auth随机测试库及8013/5193/5194服务、私有环境/浏览器认证状态已清理；主数据库和主服务未改动。
- 未访问真实 TikTok/网眼/嘉书/S3，业务边界回归使用测试 transport，真实登录浏览器仅连接专用测试数据库。
