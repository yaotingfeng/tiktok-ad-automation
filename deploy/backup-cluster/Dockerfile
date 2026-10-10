# 构建上下文只含公开工具代码和经官方 SHA256 校验的 kubectl，不带运行凭据。
ARG BASE_IMAGE
FROM ${BASE_IMAGE}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends age \
    && rm -rf /var/lib/apt/lists/*
COPY kubectl /usr/local/bin/kubectl
COPY release /opt/tkada/deploy/release
COPY backup-cluster /opt/tkada/deploy/backup-cluster
WORKDIR /opt/tkada
ENTRYPOINT ["python", "/opt/tkada/deploy/backup-cluster/controller.py"]
CMD ["backup"]
