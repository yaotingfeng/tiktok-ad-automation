# Jenkins 检查与发布的工具镜像：公司 Jenkins 节点只有 git/docker/旧版 kubectl，且不能访问
# Docker Hub，因此在能联网的机器上构建一次并推送到本项目 ACR，流水线所有步骤都在其中运行。
# 版本与本机 deploy/ci/check.sh、deploy/release 的要求一致；变更后同步更新 Jenkinsfile 的 TOOLS_IMAGE。
# 构建：docker buildx build --platform linux/amd64 -f deploy/ci/tools.Dockerfile \
#   -t sg-acr-prod-registry.ap-southeast-1.cr.aliyuncs.com/test/tiktok-ad-automation-base:ci-tools-<版本> --push deploy/ci
FROM docker@sha256:cd58b396e427d1ee8cddcc3f3b7e8d8c2ba45755c5dd5b821b6e615e1ccf4586 AS docker-cli

FROM python:3.14

# PostgreSQL 18 客户端用于备份恢复验证；ffmpeg 用于素材测试；node 运行发布资产测试。
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg unzip git ffmpeg nodejs \
    && install -d /usr/share/postgresql-common/pgdg \
    && curl -fsSL -o /usr/share/postgresql-common/pgdg/apt.postgresql.org.asc https://www.postgresql.org/media/keys/ACCC4CF8.asc \
    && echo "deb [signed-by=/usr/share/postgresql-common/pgdg/apt.postgresql.org.asc] https://apt.postgresql.org/pub/repos/apt trixie-pgdg main" > /etc/apt/sources.list.d/pgdg.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends postgresql-client-18 \
    && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir uv==0.9.26

# 与 deploy/ci/check.sh 要求的 Bun 1.4.2 一致，校验官方发布摘要。
RUN curl -fsSL -o /tmp/bun.zip https://github.com/oven-sh/bun/releases/download/bun-v1.4.2/bun-linux-x64.zip \
    && echo "36368faef7527875d5ffa52e53cd48021741f2a83eb6208a8dd64068d422a913  /tmp/bun.zip" | sha256sum -c - \
    && unzip -q /tmp/bun.zip -d /tmp \
    && install -m 0755 /tmp/bun-linux-x64/bun /usr/local/bin/bun \
    && rm -rf /tmp/bun.zip /tmp/bun-linux-x64

# ACK 服务端为 1.36；节点自带的 1.23 客户端超出支持的版本差。
RUN curl -fsSL -o /usr/local/bin/kubectl https://dl.k8s.io/release/v1.36.2/bin/linux/amd64/kubectl \
    && echo "1e9045ec32bea85da43de85f0065358529ea7c7a152eca78154fba5b58c27d82  /usr/local/bin/kubectl" | sha256sum -c - \
    && chmod 0755 /usr/local/bin/kubectl

# 通过挂载的宿主 docker.sock 使用节点 Docker；buildx 用于发布镜像构建与摘要核对。
COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-buildx /usr/local/libexec/docker/cli-plugins/docker-buildx

# 浏览器测试的 Chromium 预装进镜像（版本同 frontend/package.json 的 @playwright/test），
# 避免每次检查从海外 CDN 下载；运行时以非 root 用户执行，目录需可写。
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
RUN cd /tmp && bun x playwright@1.62.1 install --with-deps chromium \
    && chmod -R a+rwX /ms-playwright \
    && rm -rf /root/.bun /var/lib/apt/lists/*

RUN git config --system --add safe.directory '*'

ENV BUN_BIN=/usr/local/bin/bun \
    PG_BIN_DIR=/usr/lib/postgresql/18/bin
