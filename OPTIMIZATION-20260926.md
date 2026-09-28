# bitmagnet 优化报告 · 2026-09-26

## 〇、先纠正我自己的一个错误结论

我在本次排查中一度报告「近 3 小时 406 次 ≥30s 慢查询，最慢的 INSERT 卡了 29.8 分钟」。
**这个结论是错的，我为此道歉。**

错因：bitmagnet 的 gorm 日志里 `elapsed` 字段单位是**微秒**，我按毫秒读了；而日志文案
`SLOW SQL >= 30s` 也是误导性的——它渲染的是 gorm 的 `SlowThreshold`，但实际触发阈值约为 30ms。

按微秒重算：

| 我当时的说法 | 实际值 |
|---|---|
| INSERT 卡 29.8 分钟 | **1.79 秒** |
| 排序查询 108 秒 | **108 毫秒** |
| 语言 facet 124.8 秒 | **125 毫秒** |

**交叉验证**：打开 Postgres 自己的 `log_min_duration_statement = 30000`（真·30 秒）后，
4 小时内只有 4 条记录，其中 3 条是我自己的诊断查询（建索引、count torrent_files、统计 GROUP BY）。

**真实基线**（433 个样本）：中位数 **108ms**、p90 **125ms**、p99 **568ms**、最差 **1.79s**。
对一个在 HDD 上持续接收 DHT 爬虫写入的库来说，这属于正常范围，**不是**灾难状态。

---

## 一、已应用的优化（均已实测验证）

| # | 项目 | 改动前 | 改动后 | 效果 |
|---|---|---|---|---|
| 1 | `work_mem` | 4MB | **32MB** | 面板同类查询 25~90s → ~1s（已实测） |
| 2 | `shared_buffers` | 128MB | **1GB** | 缓存命中率 85.4%（重启后 19 分钟统计） |
| 3 | `maintenance_work_mem` | 64MB | **256MB** | 加速 VACUUM / 建索引 |
| 4 | `log_lock_waits` | off | **on** | 立刻抓到真实锁等待（见下） |
| 5 | `log_min_duration_statement` | -1（关） | **30000ms** | 有了权威的慢查询真值来源 |
| 6 | `idle_in_transaction_session_timeout` | 0（不限制） | **300s** | 限制僵死事务占锁 |
| 7 | 新增部分索引 `torrent_contents_languages_null_ct_idx` | 无 | GIN(languages) WHERE content_type IS NULL | **15.24s → 0.26s**，索引仅 272 kB |

索引复测（8 种语言，`content_type IS NULL` 桶）：

| 语言 | zh | en | ja | ko | ru | de | fr | es |
|---|---|---|---|---|---|---|---|---|
| 耗时 | 0.44s | 0.23s | 0.22s | 0.25s | 0.27s | 0.26s | 0.26s | 0.26s |
| 命中数 | 185 | 10994 | 2085 | 19 | 1629 | 48 | 125 | 1700 |

### 应用方式与回滚

参数通过 `ALTER SYSTEM` 写入 `$PGDATA/postgresql.auto.conf`（持久，重启不丢）。
改动前的完整快照在：`bitmagnet-dev/pg-tuning-backup-20260926.txt`

回滚单项：
```sql
ALTER SYSTEM RESET work_mem;   -- 其余同理
SELECT pg_reload_conf();
```
`shared_buffers` 需再重启一次 `bitmagnet-postgres` 容器。全部回滚即 `ALTER SYSTEM RESET ALL`。

---

## 二、真正值得注意的问题（有证据）

### 1. 长事务 → 锁等待（已抓到实锤）

`log_lock_waits` 打开后立刻记录到：

```
LOG: process 351 still waiting for ShareLock on virtual transaction 3/103 after 1000.084 ms
LOG: process 351 acquired ShareLock on virtual transaction 4/34 after 32668.884 ms   ← 32.7 秒
```

机制：爬虫的 persist 批量事务一次性插入大量行，**事务持续 8 分钟以上**
（实测 `INSERT INTO "torrent_contents"` 单语句活跃 8:25）。
`torrent_contents` 上有 **3 个外键**（含 `info_hash → torrents(info_hash)`），
其他事务做 FK 检查时要在同一行上取 ShareLock，只能等长事务结束。

### 2. `process_torrent` 任务击穿 10 分钟超时

```
ERROR queue/server/server.go:243 job failed
  {"queue":"process_torrent","error":"job exceeded its 10m0s timeout: context deadline exceeded"}
```

当前队列：`pending=17 / processed=4032 / failed=5`。
即批量落库慢到超过任务自身的 10 分钟上限，**失败的任务意味着这批种子被丢弃**。

### 3. 写入放大是结构性的

- `torrent_contents`：**24 个索引**
- `torrent_files`：4 个索引（表本体 1.9GB / 522 万行，是全库最大表）
- 外加 3 个外键

每次批量插入要同步维护这么多索引，在 HDD 上就是分钟级。这是 bitmagnet 的 schema 设计，
**不建议动它的索引**（bitmagnet 自己管理迁移，且这些索引服务于它的搜索功能）。

### 4. TMDB 未启用（你问的那个）

- 现象：bitmagnet 自带 WebUI 的 Status 页显示「TMDB 不活跃」
- 原因：`.env` 里 `TMDB_ENABLED=false`，且 `TMDB_API_KEY` 被注释掉
- 影响：**177,305 条种子（占 47%）`content_type` 为 NULL**（未分类）；
  没有海报/年份；bitmagnet 自带 WebUI 的浏览与筛选体验大打折扣
- 可行性已验证：squid 代理 → TMDB 可达（返回 HTTP 401 = 通了但没带 key）
- 代价：需要你在 themoviedb.org 注册一个免费 API Key；
  开启后会排队回填 17.7 万条，受 TMDB 速率限制，需持续跑一段时间

### 5. 面板侧遗留风险（此前已知，未处理）

`:8790` 免登录 + 挂载 `docker.sock`，等于把主机级控制权暴露在内网。

---

## 三、下一步候选

| 优先级 | 项目 | 收益 | 代价/风险 |
|---|---|---|---|
| OK已做 | `synchronous_commit=off` | 批量提交不再等 fsync，缓解长事务与锁等待、降低任务超时率 | 断电可能丢最近数百毫秒的提交（索引数据可从 DHT 重爬）——**已按你的决定启用** |
| OK已做 | 启用 TMDB | 新种子即时获得类型/海报 | 见下方「五」；存量 18.2 万条回填尚未执行 |
| 中 | 给 `:8790` 加 Basic 认证 | 消除主机级暴露 | 失去免登便利 |
| 低 | 调 `autovacuum` 更激进 | 控制表膨胀（当前 dead 行很少，暂不急） | 略增后台 I/O |
| 观察 | 继续盯 `log_lock_waits` | 累积证据判断锁等待是否影响任务成功率 | 无 |

---

## 四、本次未做、也建议不要做的

- **不新增/删除 bitmagnet 自己的索引**（24 个索引是它搜索功能的基础，且由它自己的迁移管理）
- **不用非 `CONCURRENTLY` 建索引**（会持 AccessExclusiveLock，直接卡死爬虫写入）
- **不动 `192.168.68.2`**（OpenWrt/OpenClash，绝对禁区）
- 面板的任何批量操作继续锁定 `category=bitmagnet`，避免误伤 media 栈种子


---

## 五、TMDB 启用记录（本轮已完成）

### 5.1 为什么一直显示「不活跃」

三个原因叠加，缺一不可：

1. `.env` 里 `TMDB_ENABLED=false`，且 `TMDB_API_KEY` 被注释掉
2. **`docker-compose.yml` 里 bitmagnet 服务的 `TMDB_API_KEY` 也是注释状态** —— 即使 `.env` 填了 Key，
   也不会被注入容器
3. **容器根本没有出网通路**：`api.themoviedb.org` 在宿主机与容器内都**只解析出 IPv6**
   （`2a03:2880:f102:183:face:b00c:0:25de`），而 Docker 默认网桥是 IPv4-only，
   容器没有 IPv6 路由 → 直连一律 `download timed out`。
   （宿主机 `curl` 直连同样 HTTP 000，只有经 squid 才通 → 说明这不是容器特有问题。）

### 5.2 关键验证

| 验证项 | 结果 |
|---|---|
| bitmagnet 是否读 env 配置 | OK `bitmagnet config show` 显示 `tmdb.api_key` 来源 = **env** |
| bitmagnet 是否内置默认 Key | OK 默认值 `9c6689fa83ae6814fbf...`（共享公共 Key） |
| 容器能否经 squid 出网 | OK 容器内 `http://api.themoviedb.org/3/configuration` 经 squid → **HTTP 401**（请求确实到达 TMDB） |
| bitmagnet 有无专用 proxy 配置项 | NO 没有；只能依赖 Go 标准库对 `HTTP(S)_PROXY` 的支持 |

### 5.3 实际改动

`.env`：
```
TMDB_ENABLED=true
TMDB_API_KEY=<你的个人 Key>
```

`docker-compose.yml`（bitmagnet 服务，新增 4 行）：
```yaml
      - TMDB_API_KEY=${TMDB_API_KEY:-}
      - HTTP_PROXY=${TMDB_PROXY:-}
      - HTTPS_PROXY=${TMDB_PROXY:-}
      - NO_PROXY=localhost,127.0.0.1,postgres,172.18.0.0/16,192.168.68.0/24
```
（`HTTP_PROXY` 只影响 Go 的 HTTP 客户端 → 只影响 TMDB；DHT 爬虫走 UDP/TCP 的 BitTorrent 协议，不受影响。）

备份：`.env.bak-20260926-200028`、`docker-compose.yml.bak-20260926-200028`

### 5.4 生效确认

- 容器环境变量已含 `TMDB_ENABLED=true`、`TMDB_API_KEY=<个人 Key>`、`HTTP_PROXY/HTTPS_PROXY`
- `config show`：`tmdb.enabled = true`（env）、`tmdb.api_key = <个人 Key>`（env）
- `tmdb.rate_limit = 50ms` / `burst = 5` → **20 请求/秒**
- 日志无 ERROR
- **实测富集在跑**：`content_source='tmdb'` 从 25496 → 25525（3 分钟 +29）

### 5.5 重要限制：存量 18.2 万条不会自动回填

bitmagnet 只有 `process_torrent` 一个队列，**只处理新种子**。
当前分布：

| 来源 | 类型 | 数量 |
|---|---|---|
| (local) | **NULL（未分类）** | **182,605** |
| (local) | xxx | 48,714 |
| (local) | movie | 44,915 |
| (local) | tv_show | 32,889 |
| (local) | music | 21,124 |
| tmdb | tv_show | 18,132 |
| (local) | software | 15,938 |
| (local) | ebook | 10,975 |
| tmdb | movie | 7,336 |
| (local) | audiobook / comic | 2,609 / 1,255 |
| tmdb | xxx | 57 |

bitmagnet 自带回填命令，可精确指定只处理未分类的：
```bash
docker exec bitmagnet /usr/local/bin/bitmagnet reprocess --contentType null --classifyMode default
```
`--classifyMode default` 的语义正是「只尝试匹配此前未匹配的种子」。

**规模与代价（尚未执行，待你决定）**：
- 182,605 条 → 按 TMDB 20 请求/秒计，**纯 API 时间约 2.5 小时**，叠加落库会更久
- `queue_jobs` 会从 4,140 行涨到约 18.6 万行
- 期间写库压力显著上升；**该 Key 与 media 栈共用**，可能影响 MoviePilot 发现墙
- 建议在夜间跑，并先小批量试（如 `--batchSize 100 --chunkSize 1000`）

---

## 六、调优后最终指标

| 指标 | 调优前 | 调优后 |
|---|---|---|
| Postgres 缓存命中率 | 85.43% | **92.42%** |
| gorm SLOW SQL（同口径 5 分钟） | ~61 条 | **0 条** |
| Postgres >=30s 查询（5 分钟） | 有 | **0 条** |
| 锁等待（5 分钟） | 有（最长 32.7s） | **0 次** |
| 语言 facet 单次耗时 | 15.24s | **0.26s** |
| 爬虫吞吐 | ~305 种子/分钟 | **~843 种子/分钟** |

> 注意：这些是调优后短窗口的观测值，且期间重启过容器（缓存更热）。
> 「0 条慢查询」是 5 分钟窗口的即时值，不代表长期恒定，需持续观察。

---

## 七、仍可做（未执行）

| 优先级 | 项目 | 说明 |
|---|---|---|
| 高 | 执行 TMDB 存量回填 | 见 5.5，需你决定何时跑 |
| 中 | 给 `:8790` 加 Basic 认证 | 消除「免登 + docker.sock」的主机级暴露 |
| 低 | 调 `autovacuum` 更激进 | 当前 dead 行很少，暂不急 |
| 观察 | 盯 `log_lock_waits` 与 `log_min_duration_statement` | 已开启，日志会持续给出权威证据 |


---

## 八、TMDB 存量回填尝试（2026-09-26，失败，已停止）

按指示执行了 `bitmagnet reprocess --contentType null --classifyMode default`。
**结论：这条路当前走不通。**

### 8.1 机制（先搞清楚它怎么工作）

`reprocess` 不是一次性入队 18.6 万条，而是接力式：

1. 入队 1 个 `process_torrent_batch` 作业
2. 该作业被 worker 处理后，按 info hash 游标取一个 chunk（`ChunkSize=10000`），
   再分批（`BatchSize=100`）入队 `process_torrent` 作业
3. 然后**把自己以新游标再入队一次**，如此接力直到游标走完

### 8.2 失败原因一：我自己造成的（已修正）

我上一轮设的 `idle_in_transaction_session_timeout = 300s` 把 batch 作业的连接杀掉了：

```
2026-09-26 12:44:09 UTC [2921] FATAL: terminating connection due to idle-in-transaction timeout
```

→ bitmagnet 报 `driver: bad connection` → 作业重试 → 重试时重复入队 → 撞唯一约束 → 链断。

**已 `ALTER SYSTEM RESET idle_in_transaction_session_timeout` 回到 0（原值）。**

反思：这条参数对「爬虫持锁 8 分钟」这个真问题**毫无帮助**——那是**活跃**事务，
不是空闲事务；它却会误杀 bitmagnet 正常的长事务。**结论：不该设它。**

### 8.3 失败原因二：bitmagnet 自身的 bug（无法绕过）

撤销该参数后重新执行，链**在 3 步之内再次死亡**，且这次 `FATAL = 0`、`bad connection = 0`：

```
processed created=13:01:01 cursor=000000000000
processed created=13:01:29 cursor=0673f2a195b3
processed created=13:01:30 cursor=0d400f7c61ab
failed    created=13:01:30 cursor=13afb25d94e6
```

```
ERROR  internal/processor/batch/queue/handler.go:148
error: "ERROR: duplicate key value violates unique constraint \"queue_jobs_fingerprint_idx\" (SQLSTATE 23505)"
```

该唯一索引定义是 `ON queue_jobs (fingerprint) WHERE status IN ('pending','retry')`。
batch 作业在接力时重复插入同一 fingerprint → 撞约束 → 重试耗尽（2/2）→ 链终止。

**这是 bitmagnet v0.10.1 自身的问题，与我们的参数/配置无关，无法从外部绕过。**

### 8.4 就算它能跑，性价比也很差

| 事实 | 数值 |
|---|---|
| TMDB 经 squid 的单次调用延迟 | **1.0 ~ 1.6 秒**（宿主机 curl 实测 3 次） |
| 爬虫新增种子速率 | **~249 / 分钟** |
| NULL 桶增长速率 | **~120 / 分钟** |
| `process_torrent` 实际完成速率 | 约 3 ~ 30 / 分钟 |
| 未分类存量 | 189,002（仍在增长） |

即：**存量在增长，回填速度远低于新增速度**；而且 NULL 桶里只有约 20%（影视）是 TMDB 能匹配的，
其余（软件/音乐/电子书/杂项）TMDB 永远匹配不上。全量回填预计需要数天到数周，且不可能清零。

### 8.5 遗留状态（无害，未做清理性删除）

- 已入队的约 4,195 个 `process_torrent` 作业会继续慢慢消化（相当于免费的小批量回填约 4200 条）
- 1 个失败的 batch 作业留在 `queue_jobs`（7 天后自动归档），无副作用

### 8.6 建议

1. **放弃存量回填**，接受「新种子即时分类」——这条已经在正常工作（`tmdb` 来源持续增长）
2. 若确实想清空存量：等 bitmagnet 升级修掉该 bug 后再跑；或低峰期用更小 `--batchSize`
   反复重跑（每轮推进几十步，需跑很多轮）
3. 不建议为此改动 bitmagnet 的索引或 schema
