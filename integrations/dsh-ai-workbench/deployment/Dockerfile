ARG NODE_IMAGE=node:24.22.2-bookworm-slim
ARG NODE_BUILD_IMAGE=node:24.22.2-bookworm
FROM ${NODE_BUILD_IMAGE} AS dependencies
WORKDIR /opt/tkada-dsh
COPY integrations/dsh-ai-workbench/deployment/package.json integrations/dsh-ai-workbench/deployment/package-lock.json ./
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm ci --registry=${NPM_REGISTRY} --omit=dev --no-audit --no-fund && npm cache clean --force

FROM ${NODE_IMAGE}
WORKDIR /opt/tkada-dsh
# 官方插件管理器通过 pnpm 安装 profile 依赖，不能只开放按钮却缺少执行器。
ARG NPM_REGISTRY=https://registry.npmjs.org
RUN npm install --registry=${NPM_REGISTRY} --global pnpm@10.34.9 --no-audit --no-fund && npm cache clean --force
COPY --from=dependencies /opt/tkada-dsh ./
COPY integrations/dsh-ai-workbench ./plugin
ENV DSH_HOME=/var/lib/dsh/home DSH_WORKSPACE=/var/lib/dsh/workspace NODE_OPTIONS=--max-old-space-size=640 XDG_DATA_HOME=/var/lib/dsh/data XDG_CACHE_HOME=/var/lib/dsh/cache NPM_CONFIG_CACHE=/var/lib/dsh/cache/npm
RUN mkdir -p /var/lib/dsh && chown -R node:node /var/lib/dsh
ARG VCS_REF=local
LABEL org.opencontainers.image.revision=${VCS_REF}
USER node
EXPOSE 9080 9081
CMD ["node", "/opt/tkada-dsh/plugin/deployment/start.mjs"]
