FROM oven/bun:1.4.2 AS frontend-build

WORKDIR /app

COPY package.json bun.lock /app/
COPY frontend/package.json /app/frontend/
COPY packages/react-email/package.json /app/packages/react-email/

RUN bun install --frozen-lockfile

COPY ./frontend /app/frontend
WORKDIR /app/frontend

ARG VITE_API_URL=

RUN bun run build


FROM python:3.14

# 国内生产主机可选择已验证的镜像站；APT 仍验证 Debian 签名和包摘要。
ARG DEBIAN_MIRROR=https://deb.debian.org
RUN sed -E -i "s|https?://deb.debian.org|${DEBIAN_MIRROR}|g" /etc/apt/sources.list.d/debian.sources \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 update \
    && apt-get -o Acquire::Retries=3 -o Acquire::http::Timeout=30 -o Acquire::https::Timeout=30 install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1

# 镜像站只替换下载地址，包版本与锁文件摘要保持不变。
ARG PYPI_INDEX_URL=https://pypi.org/simple
ARG PYPI_FILES_URL=https://files.pythonhosted.org
# SDK 和本项目的构建依赖也使用同一索引。
ENV UV_DEFAULT_INDEX=${PYPI_INDEX_URL}
# 使用 PyPI 分发的固定 uv 版本，避免构建依赖额外的 GHCR 大文件连接。
# Ref: https://docs.astral.sh/uv/getting-started/installation/#pypi
RUN pip install --no-cache-dir --index-url "${PYPI_INDEX_URL}" uv==0.9.26

# Compile bytecode
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#compiling-bytecode
ENV UV_COMPILE_BYTECODE=1

# uv Cache
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#caching
ENV UV_LINK_MODE=copy

WORKDIR /app/

# Place executables in the environment at the front of the path
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#using-the-environment
ENV PATH="/app/.venv/bin:$PATH"

# Keep workspace manifests in the dependency layer; the SDK is pinned by uv.lock.
COPY pyproject.toml uv.lock /app/
COPY backend/pyproject.toml /app/backend/
# frozen 同步使用锁文件内的下载 URL；仅修改镜像内副本的来源，不重新解析依赖。
RUN python -c 'import os; from pathlib import Path; p = Path("uv.lock"); p.write_text(p.read_text().replace("https://pypi.org/simple", os.environ["PYPI_INDEX_URL"]).replace("https://files.pythonhosted.org", os.environ["PYPI_FILES_URL"]))'
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-install-workspace --package app

COPY ./backend/scripts /app/backend/scripts

COPY ./backend/pyproject.toml ./backend/alembic.ini /app/backend/

COPY ./backend/app /app/backend/app

COPY --from=frontend-build /app/backend/app/frontend /app/backend/app/frontend

# Sync the project
# Ref: https://docs.astral.sh/uv/guides/integration/docker/#intermediate-layers
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package app

WORKDIR /app/backend/

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4", "--no-access-log"]
