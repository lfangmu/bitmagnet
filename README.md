# BitMagnet 部署工程（media 标准版）

自托管 BitTorrent 索引器 / DHT 爬虫 / Torznab 接口。基于官方 `ghcr.io/bitmagnet-io/bitmagnet` 镜像，
按 media-stack 的工程标准封装为**可公开、一键安装可用**的部署模板：标准 compose、`.env.example`、
`deploy.sh`、以及一份可自动跑通的**验收契约**（`verify_clean.sh`）。

> 本工程仅做部署封装，BitMagnet 本体为 MIT 协议（github.com/bitmagnet-io/bitmagnet）。

## 组件

| 服务 | 镜像 | 端口 | 说明 |
|------|------|------|------|
| bitmagnet | `ghcr.io/bitmagnet-io/bitmagnet:latest` | 3333 / 3334(TCP+UDP) | Web UI + GraphQL + Torznab + DHT 爬虫 |
| postgres | `postgres:16-alpine` | （不对外） | 元数据存储，官方 minimal 配置，不含 redis |

## 快速开始

```bash
cp .env.example .env      # 按需改密码/端口/数据目录
./deploy.sh               # 建数据目录 + 拉镜像 + 启动
./verify_clean.sh         # 跑验收契约，确认安装可用
```

Web UI： http://<本机IP>:3333 （GraphQL Playground 在 `/graphql`，Torznab 在 `/torznab`）

## 配置（.env）

| 变量 | 默认 | 说明 |
|------|------|------|
| `POSTGRES_PASSWORD` | `bitmagnet` | postgres 与 bitmagnet 必须一致 |
| `WEB_PORT` | `3333` | 宿主机映射端口（容器内固定 3333） |
| `DATA_DIR` | `./data` | postgres 数据 + bitmagnet 配置根目录 |
| `TMDB_API_KEY` | 空 | 可选，开启影视元数据 enrichment |
| `TMDB_ENABLED` | `false` | TMDB 开关；填了 API Key 时改 `true`（默认关，开箱即索引，无需外网 TMDB） |

## 验收契约（verify_clean.sh 断言）

1. `bitmagnet-postgres` healthy、`bitmagnet` running
2. `http://localhost:3333/` 返回 200
3. Torznab `t=capabilities` 返回 200
4. DHT 爬虫在 180s 内索引到数据（网络受限时仅为 WARN，非工程缺陷）

## 复用历史数据

若此前数据在 `/vol1/bitmagnet`，将 `.env` 中 `DATA_DIR=/vol1/bitmagnet` 且 `POSTGRES_PASSWORD`
与当时一致即可直接挂载旧库，无需重新爬虫。

## 设计说明

- 不含 redis / gluetun：对齐官方 minimal，轻量部署；如需队列增强或 VPN 出口可自行追加服务。
- 不引入 PUID/PGID：该概念属于 linuxserver 镜像，本工程镜像以各自默认用户运行。
- 本工程为通用公开模板，**不含任何环境专属的网络 hack**（如特定 DNS / 代理绕过），
  使用者按需根据自身网络环境自行配置。
