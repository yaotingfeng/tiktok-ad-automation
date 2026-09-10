# TT ADA 骏伯生产环境与发布规则

## 环境与首发授权

2026-09-10 用户授权在下列生产服务器发布 TT ADA、创建平台管理员 admin、租户 junbo 及其管理员 junbo，并进行基础功能验收。密码及 SSH 凭据不得写入 Git；开发者应用、TikTok 授权、版权方登录和 R2 接入留待基础验收完成后单独配置。

| 项目 | 当前确认的信息 |
| --- | --- |
| 服务器 | `43.139.185.139`，Ubuntu 24.04.4 LTS，x86_64 |
| SSH | `ubuntu`，端口 `54231`；使用运维凭据或已授权 SSH 密钥 |
| 资源 | 16 vCPU、约 32 GiB 内存；首次核查可用约 16 GiB，根盘剩余约 820 GiB |
| 域名 | `manjuad.gzjunbo.net` 已解析到上述 IP |
| 拟用公网入口 | `https://manjuad.gzjunbo.net:8000/`；同端口 HTTP 自动转 HTTPS |
| 端口核查 | 8000 和拟用内部端口 18000 未监听；需新增仅 8000 的防火墙放行并从公网验收 |
| 共存项目 | 80/443 和现有 Docker 容器归原项目使用；禁止停止、替换或修改其配置和数据 |
| 现有反向代理 | 宝塔 Nginx，配置入口 `/www/server/nginx/conf/nginx.conf` |
| 域名证书 | `/etc/nginx/ssl/gzjunbo.net/gzjunbo.net.pem`；只引用现有证书，不复制私钥入仓库 |

此节为部署前核查记录，最终发布证据见 `docs/validation/2026-09-10-production-release.md`（验收后生成）。只有本文记录的环境与固定版本可作为后续发布目标，不能将其他项目的数据库、端口、目录或本地测试数据用于生产。

## 本次部署边界

- 独立目录 `/opt/tt-ada`，独立 Compose 项目 `tt-ada-production`，独立 PostgreSQL/Redis 持久卷。
- 私有环境配置保存在 `/etc/tt-ada/production.env`；仅通过 SSH 在主机使用，不进 Git、不输出完整 Compose 展开配置。
- 使用 `compose.yml` 与 `compose.production.yml`；API 仅绑定 `127.0.0.1:18000`，由独立 Nginx 8000 入口提供 HTTPS。不要加载本地 override 或占用 80/443 的 staging 配置。
- 素材、搭建、控制队列使用独立 Linux prefork Worker；Beat 只有一个；按服务限制 CPU、内存及日志大小。
- 初次部署空库执行 Alembic，创建新生产账号及租户，不复制本地数据库。上传、清理开关均保持关闭，实际广告操作尚未验收。
- 发布必须固定 Git SHA、镜像和迁移版本；迁移前停止写入并正常排空任务，备份数据库及对应版本配置，验证恢复能力。禁止 `down -v`、清空队列、重建有数据的卷或用旧镜像直接搭配不兼容的新表结构。

## 版本与目录约定

- 截至首发，仓库只有已发布的默认分支 `feat/platform-implementation`，没有 `main`；本次按现有分支提交和推送，不擅自创建分支。以后若用户建立 main，应先核对祖先关系和测试记录，再按仓库默认规则发布 main。
- 每次发布以完整 40 位 Git SHA 为单位。只打包已提交、已推送并通过相关检查的源代码；禁止把整个本地目录、`.env`、`.runtime`、`.worktrees`、会话、上传视频或本地数据库复制到生产。
- 生产源码：`/opt/tt-ada/releases/<SHA>`，发布后不可原地编辑；应用镜像 `tt-ada:<SHA>` 不覆盖旧 tag。数据库、Redis 及环境配置独立于版本目录。
- `/opt/tt-ada/current` 仅在新版本启动及验收通过后切换。备份在 `/opt/tt-ada/backups`，目录 0700、文件 0600，仅 root 可读。
- 所有 Docker 操作使用版本内 `deploy/production-compose.sh`；脚本固定项目名、配置文件和镜像，必须 sudo。不得对整台主机执行 `docker system prune` 或不带项目范围的停止操作。

## 首次发布

1. 运维端核对 `git status -sb`、`git rev-parse --show-toplevel`、提交差异、测试及远程 SHA。执行 `git archive --format=tar <SHA>`，通过 SSH 传到上述同名版本目录。
   首次创建 `/opt/tt-ada/releases`、`/opt/tt-ada/backups`（0700）、`/etc/tt-ada`（0700）及 `/var/log/nginx`；不要假定宝塔安装已建立系统 Nginx 日志目录。
2. 私有环境文件由运维安全创建：`PROJECT_NAME=TT ADA`、`FRONTEND_HOST=https://manjuad.gzjunbo.net:8000`、`TT_ADA_BACKEND_PORT=18000`、独立 `SECRET_KEY`、`CONNECTION_ENCRYPTION_KEY`、`POSTGRES_PASSWORD`，以及用户授权的初始化管理员凭据。不要复用本地密钥。
3. 以下命令在服务器执行，`RELEASE_SHA` 必须替换为已验证的完整 SHA。配置检查只能输出 `--quiet`、服务名或必要非敏感字段，不能输出整个环境。
   国内服务器的生产构建使用腾讯云 Debian 镜像站并保留 APT 签名校验；uv 从官方 PyPI 安装固定版本 0.9.26。Python/Bun、uv.lock 和 bun.lock 的版本约束不变。不要修改整机 Docker 镜像源或其他项目依赖来解决单项目构建问题。

```bash
RELEASE_SHA=<完整40位SHA>
TT_COMPOSE=/opt/tt-ada/releases/$RELEASE_SHA/deploy/production-compose.sh
sudo "$TT_COMPOSE" config --quiet
sudo "$TT_COMPOSE" build prestart
sudo "$TT_COMPOSE" up -d --wait db redis
sudo "$TT_COMPOSE" run --rm --no-deps prestart
sudo "$TT_COMPOSE" run --rm --no-deps prestart alembic current
sudo "$TT_COMPOSE" run --rm --no-deps prestart alembic heads
sudo "$TT_COMPOSE" up -d --no-build --no-deps --wait backend worker worker-builds worker-control beat
```

4. 仅新增 `/www/server/panel/vhost/nginx/tt-ada.conf`，内容来自 `deploy/nginx.production.conf`。先备份该文件的已有版本（如存在），再 `sudo nginx -t`；成功才 `sudo systemctl reload nginx`。只放行 `8000/tcp`，数据库、Redis、18000 不对公网开放。不覆盖原站点配置或证书。
5. 从公网核对 HTTPS 证书、HTTP 跳转、登录及 API。证书应覆盖 `*.gzjunbo.net`；当前证书有效期至 2026-12-18，续期后由统一证书流程 reload Nginx，不重新生成或覆盖其他站点证书。
6. 初始化 junbo 普通用户（非 superuser）和 junbo 租户的 tenant_admin。用户按 username、租户按完整名称查询后创建；同名租户多条停止自动选择。创建租户的 POST 没有幂等键，未知结果先查询，不重复创建。
7. 完成下述基础验收、备份恢复演练，再切换 current，并安装每日备份定时器：

```bash
sudo ln -s /opt/tt-ada/releases/$RELEASE_SHA /opt/tt-ada/current.next
sudo mv -Tf /opt/tt-ada/current.next /opt/tt-ada/current
sudo install -m 0644 /opt/tt-ada/current/deploy/tt-ada-backup.service /etc/systemd/system/
sudo install -m 0644 /opt/tt-ada/current/deploy/tt-ada-backup.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now tt-ada-backup.timer
```

## 后续更新和数据库迁移

1. 先阅读本文和最新发布记录，确认当前 SHA、镜像 ID、Alembic head、数据库备份及外部开关。核对 80/443 原项目可访问，确认剩余内存和磁盘。审查代码及迁移，不在生产生成迁移。
2. 相关测试通过后提交、推送，打包新 SHA 到新目录，运行新版本的 `config --quiet` 和 `build prestart`。构建失败不触碰正在运行的版本。
3. 安排维护窗口，停止旧 API 接收写入，再停止 Beat；正常停止三个 Worker 并核实没有正在执行的任务。素材 Worker 等待时间最多 960 秒，禁止用强杀缩短业务任务退出。排队及尚未发布的消息保留，迁移必须兼容其任务名与 payload。

```bash
OLD_COMPOSE=/opt/tt-ada/current/deploy/production-compose.sh
sudo "$OLD_COMPOSE" stop backend beat
sudo "$OLD_COMPOSE" stop worker worker-builds worker-control
sudo /opt/tt-ada/current/deploy/backup-production.sh before-release
```

4. 备份目录必须有 `COMPLETE`，并通过 `sha256sum -c SHA256SUMS`。记录备份路径、旧 SHA、旧镜像 ID、旧 head。随后在新版本执行 `run --rm --no-deps prestart`；非零退出时禁止启动新 API/Worker。
5. 对比新版本 `alembic current` 与 `alembic heads`，必须只有一个相同 head；再按首次发布命令启动 API、三个 Worker 和唯一 Beat。验证通过后才原子切换 current。
6. 保留旧镜像、旧版本目录及发版前备份。发布记录写明新旧 SHA、迁移 ID、验证结果、异常与回滚入口，提交并推送到仓库。

数据库规则：只能新增审查后的 Alembic 迁移；禁止改写已发布迁移、手工增删业务列/索引或直接改数据绕过状态机。删列、类型转换、非空约束、索引创建及数据回填必须评估锁表、数据量和旧任务兼容性；需要时分版本添加、回填、切换、清理。普通重新启动不是数据库升级，不重新初始化已有账号密码。

## 备份、恢复与回滚

- `deploy/backup-production.sh` 保存 PostgreSQL custom archive、全局角色、Redis RDB、独立加密/签名配置、Git SHA、镜像 ID、Alembic head 和校验摘要。日志只输出备份路径，不输出凭据；Redis AOF 持久卷保留。
- 每天北京时间 03:30 左右自动备份；发版前必须额外执行停写备份。每日在线备份的 PostgreSQL/Redis 不构成同一时刻快照，灾后恢复必须核对 outbox/任务状态，不能直接全量重放。
- 首发及关键迁移后，将备份恢复到同一 TT ADA PostgreSQL 实例的**新临时验证数据库**，执行 `pg_restore --exit-on-error --no-owner --no-acl`，比较迁移版本、用户、租户、成员和策略记录数量；不连接 Worker，不使用生产 app 库作为恢复目标。验证后只删除本次创建的临时库。
- 至少保留最近 14 份日备份及最近 7 次发布前备份；脚本不会自动删除旧备份。清理时逐一确认完整标记、恢复验证及保留范围，不做整机或全桶清理。当前备份在同机，不能抵御主机/磁盘整体故障；异机备份目的地须另行配置。
- 无迁移且旧代码兼容时：停止新 API/Beat/Worker，使用旧版本脚本启动旧镜像，验收后切回 current。不要重建数据库/Redis 卷。
- 涉及不兼容迁移时，优先修复前进；只有停止所有写入、明确回滚期间新增数据的处理方式并确认恢复范围后，才恢复与旧镜像匹配的备份。禁止自动 `alembic downgrade` 或把旧镜像直接接到不兼容的新库；恢复备份可能丢失备份后的业务变更。
- TikTok 上已创建的广告及已删除的 R2 原件不会随数据库回滚恢复。外部任务结果必须回读核对，禁止通过清空队列、重置状态或重新创建广告来掩盖未知结果。

## 上线验收与日常检查

- 公网 HTTPS 页面、HTTP 同端口跳转、证书及 `scripts/check-bootstrap.py` 通过；此脚本在运维端或源码目录运行，不能假定存在于应用镜像内。
- admin 平台管理权限正常；junbo 登录后只有 junbo 租户 tenant_admin 权限，平台用户/租户接口及其他租户访问返回 403。
- 从真实浏览器验证租户进入、成员列表、策略创建/编辑/刷新恢复及停用。验收策略标为上线验收并停用；策略版本不可变，没有删除接口，不直接删数据库记录。
- 没有 BC 时素材、搭建、任务正确给出接入引导；TikTok 配置为未配置，不能授权或创建广告。版权方/R2 的真实可用性不以页面通过替代。
- `config --quiet`、db/redis/backend 健康、三个 Worker `inspect ping` 和各自队列正确、唯一 Beat；用 `jobs.probe` 验证 PostgreSQL outbox → Beat → control Worker 的完成记录。
- 记录容器内存/CPU、日志无异常重启、备份 timer 和备份完整性；确认原项目 80/443 及原 Docker 容器仍正常。
- 首次生产主机对 `business-api.tiktok.com:443` 的无凭据连接探测出现超时。正式配置 TikTok 前须重新核验 DNS、TLS、API 和媒体出站访问；基础上线不代表这条网络已解决。
