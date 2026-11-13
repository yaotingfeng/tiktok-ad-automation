ARG NODE_IMAGE=node:24.22.2-bookworm-slim
ARG NODE_BUILD_IMAGE=node:24.22.2-bookworm
FROM ${NODE_BUILD_IMAGE} AS dependencies
WORKDIR /opt/tkada-dsh
COPY integrations/dsh-ai-workbench/deployment/package.json integrations/dsh-ai-workbench/deployment/package-lock.json ./
RUN npm ci --omit=dev --no-audit --no-fund && npm cache clean --force

FROM ${NODE_IMAGE}
WORKDIR /opt/tkada-dsh
COPY --from=dependencies /opt/tkada-dsh ./
COPY integrations/dsh-ai-workbench ./plugin
ENV DSH_HOME=/var/lib/dsh/home DSH_WORKSPACE=/var/lib/dsh/workspace NODE_OPTIONS=--max-old-space-size=640
RUN mkdir -p /var/lib/dsh && chown -R node:node /var/lib/dsh
ARG VCS_REF=local
LABEL org.opencontainers.image.revision=${VCS_REF}
USER node
EXPOSE 9080
CMD ["node", "/opt/tkada-dsh/plugin/deployment/start.mjs"]
