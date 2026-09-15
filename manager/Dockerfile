FROM python:3.13-slim
WORKDIR /app

# 运行时零 Python 外部依赖（仅标准库：http.server / urllib / xml）。
# 但配置页保存 TMDB 等项后需要 `docker compose up -d` 重建受影响容器，
# 故打入 docker CLI + compose 插件（静态二进制，无额外系统库依赖；仅支持 linux/amd64）。
ARG DOCKER_VERSION=29.7.0
ARG COMPOSE_VERSION=2.33.0

RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl \
 && rm -rf /var/lib/apt/lists/*

# docker CLI 静态二进制
RUN curl -fsSL "https://download.docker.com/linux/static/stable/x86_64/docker-${DOCKER_VERSION}.tgz" -o /tmp/docker.tgz \
 && tar -xzf /tmp/docker.tgz -C /tmp \
 && mv /tmp/docker/docker /usr/local/bin/docker \
 && rm -rf /tmp/docker /tmp/docker.tgz

# docker compose 插件（CLI 标准插件路径，docker 自动发现）
RUN mkdir -p /usr/local/lib/docker/cli-plugins \
 && curl -fsSL "https://github.com/docker/compose/releases/download/v${COMPOSE_VERSION}/docker-compose-linux-x86_64" \
    -o /usr/local/lib/docker/cli-plugins/docker-compose \
 && chmod +x /usr/local/lib/docker/cli-plugins/docker-compose

COPY app.py /app/app.py
EXPOSE 8790
CMD ["python", "/app/app.py"]
