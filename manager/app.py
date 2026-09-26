#!/usr/bin/env python3
"""BitMagnet 一键下载台（零外部依赖，仅用 Python 标准库）。

把 BitMagnet（自建 DHT 索引器）的磁力一键发到 qBittorrent 下载，并在单页内完成：
  - 已索引种子：直读 Postgres，关键词 + 类型 + 排序 + 大小区间过滤，批量下载 / 收藏 / 已下载去重
  - 实时 DHT 搜索：代理 BitMagnet Torznab t=search，补库外冷门资源
  - 下载队列：读 qBittorrent，页面内暂停/继续/删除/重新校验/改分类，粘贴磁力直接下载
  - 收藏：localStorage 暂存想下的磁力
  - 系统状态：多系统健康 + 出网健康(一键修复) + 隧道状态(一键重启) + 索引统计 + 重分类/重处理

Run: python app.py   (默认监听 8790)
"""
import os
import re
import json
import socket
import time
import base64
import threading
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
import urllib.request
import urllib.error
import urllib.parse
import ssl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
SSL_CTX = ssl._create_unverified_context()  # qBittorrent 现已启用自签 HTTPS

PORT = int(os.environ.get("PORT", "8790"))
# BitMagnet 服务地址（同 compose 内可用服务名 bitmagnet:3333；跨机填 http://<host>:3333）
BITMAGNET_HOST = os.environ.get("BITMAGNET_HOST", "http://bitmagnet:3333").rstrip("/")
# qBittorrent（可选）：留空则下载台不显示 qB 功能
QBIT_URL = os.environ.get("QBITTORRENT_URL", "").rstrip("/")
QBIT_USER = os.environ.get("QBITTORRENT_USER", "")
QBIT_PASS = os.environ.get("QBITTORRENT_PASS", "")
# 面板「系统状态」点开用的浏览器地址（免登优先）。内部调用仍用容器名/直连地址，
# 两者分离：BITMAGNET_HOST 是容器内可达的 http://bitmagnet:3333，浏览器解析不了。
PUB_BM_URL = os.environ.get("PUBLIC_BITMAGNET_URL", "")
PUB_QB_URL = os.environ.get("PUBLIC_QBITTORRENT_URL", "")
QBIT_CATEGORY = os.environ.get("QBITTORRENT_CATEGORY", "bitmagnet")
DOCKER_SOCK = os.environ.get("DOCKER_SOCK", "/var/run/docker.sock")
# Clash / OpenClash 出网自愈（可选）：留空则不启用
CLASH_API = os.environ.get("CLASH_API_URL", "").rstrip("/")
CLASH_TOKEN = os.environ.get("CLASH_API_TOKEN", "")
# TMDB 海报 / 年份补全（可选；bot 经 TMDB_PROXY 出网）
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "")
TMDB_PROXY = os.environ.get("TMDB_PROXY", "")
# Clash API 经此代理可达（可选，默认复用 TMDB_PROXY）
CLASH_PROXY = os.environ.get("CLASH_PROXY", TMDB_PROXY)
# 容器名（与 docker-compose.yml 一致，可覆盖）
BITMAGNET_CONTAINER = os.environ.get("BITMAGNET_CONTAINER", "bitmagnet")
BITMAGNET_DB_CONTAINER = os.environ.get("BITMAGNET_DB_CONTAINER", "bitmagnet-postgres")

# ---------------------------------------------------------------------------
# 页面配置：出网 / Clash 单一配置点
# 容器内挂载的宿主 .env 与 compose 路径（见 bitmagnet compose 的 volumes）。
# 仅允许白名单内的 key 被页面改写；保存后写盘并在后台触发 `docker compose up -d`。
# ---------------------------------------------------------------------------
# 容器内挂载的 BitMagnet 栈 .env 与 compose 路径（见本工程 docker-compose.yml 的 volumes）。
# 可通过环境变量覆盖，适配不同宿主目录布局。
BITMAGNET_ENV_FILE = os.environ.get("BITMAGNET_ENV_FILE", "/host/bitmagnet/.env")
BITMAGNET_COMPOSE_FILE = os.environ.get("BITMAGNET_COMPOSE_FILE", "/host/bitmagnet/docker-compose.yml")
DOCKER_BIN = os.environ.get("DOCKER_BIN", "/usr/bin/docker")
# 配置页可改写的 BitMagnet 环境变量白名单（逗号分隔，可用 BOT_CFG_KEYS 覆盖）。
# 默认仅 TMDB 相关（无环境专属网络 hack），Clash 等专属项不进公共模板。
CFG_KEYS = {
    "bitmagnet": set((os.environ.get("BOT_CFG_KEYS")
                      or "TMDB_API_KEY,TMDB_ENABLED,TMDB_PROXY").split(","))
}


def _read_env(path):
    d = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                d[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return d


def _write_env(path, updates):
    try:
        lines = open(path, encoding="utf-8").read().splitlines()
    except FileNotFoundError:
        lines = []
    keys = set(updates.keys())
    out, seen = [], set()
    for line in lines:
        st = line.strip()
        if st and not st.startswith("#") and "=" in st:
            k = st.split("=", 1)[0].strip()
            if k in keys:
                out.append("%s=%s" % (k, updates[k]))
                seen.add(k)
                continue
        out.append(line)
    for k in keys:
        if k not in seen:
            out.append("%s=%s" % (k, updates[k]))
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(out) + "\n")


# 短期缓存的实时 .env 读取：使配置保存后 bot 自身（如 TMDB 海报抓取）无需重启即生效
_BM_ENV_CACHE = {"ts": 0.0, "val": {}}


def _bm_live_env():
    now = time.time()
    if now - _BM_ENV_CACHE["ts"] < 5:
        return _BM_ENV_CACHE["val"]
    v = _read_env(BITMAGNET_ENV_FILE)
    _BM_ENV_CACHE["ts"] = now
    _BM_ENV_CACHE["val"] = v
    return v


def _host_port(url):
    """从 http(s)://host:port 解析出 (host, port)；解析失败或为空返回 (None, None)。"""
    if not url:
        return None, None
    try:
        p = urllib.parse.urlparse(url)
        host = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
        return host, port
    except Exception:
        return None, None


def _validate_val(k, v):
    if not isinstance(v, str):
        return "类型错误"
    if len(v) > 2000:
        return "过长"
    if "\n" in v or "\r" in v or "\0" in v:
        return "不能含换行/控制字符"
    if k in ("EGRESS_PROXY", "CLASH_SOCKS5", "CLASH_API_URL", "TMDB_PROXY"):
        if not (v.startswith("http://") or v.startswith("https://")
                or v.startswith("socks5://")):
            return "需以 http(s):// 或 socks5:// 开头"
    elif k == "CLASH_DNS":
        if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", v):
            return "需为 IPv4 地址"
    elif k == "TMDB_ENABLED" and v not in ("", "true", "false"):
        return "只能为 true / false / 空"
    return None


def load_config():
    bm = _read_env(BITMAGNET_ENV_FILE)
    return {"ok": True,
            "bitmagnet": {k: bm.get(k, "") for k in CFG_KEYS["bitmagnet"]}}


def _apply_config():
    # 后台触发：detach 进程，仅重建数据服务（postgres / bitmagnet），
    # 不重建 bot 自身，避免「up -d 重建 bot → 杀掉正在跑的 reconcile 进程」自锁。
    try:
        subprocess.Popen([DOCKER_BIN, "compose", "-f", BITMAGNET_COMPOSE_FILE,
                         "up", "-d", "postgres", "bitmagnet"],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception as e:
            print("apply_config err:", e)


def save_config(payload):
    updates = {}
    src = payload.get("bitmagnet", {}) or {}
    for k in CFG_KEYS["bitmagnet"]:
        if k in src:
            v = src[k]
            err = _validate_val(k, v)
            if err:
                return {"ok": False, "error": "%s: %s" % (k, err)}
            updates[k] = v
    if not updates:
        return {"ok": False, "error": "没有可保存的字段"}
    _write_env(BITMAGNET_ENV_FILE, updates)
    threading.Thread(target=_apply_config, daemon=True).start()
    return {"ok": True, "applied": True,
            "message": "已写入 .env，正在重建受影响容器…",
            "changed": {"bitmagnet": list(updates.keys())}}
# BitMagnet 栈容器（用 docker.sock 只读其状态 / 重启）
BM_CONTAINERS = os.environ.get("BM_CONTAINERS", "bitmagnet-postgres,bitmagnet").split(",")
# 出网修复黑名单（与 OpenClash 看门狗一致）：延迟能过但出网死的节点群
JUNK_KEYWORDS = ("流量", "到期", "重置", "GB", "官网", "更新软件", "高速下载")
PREFERRED_PREFIXES = ("2x专线",)
START_TS = time.time()


# --------------------------------------------------------------------------- #
# Docker Engine API（经 unix socket，零依赖）
# --------------------------------------------------------------------------- #
def docker_api(method, path, body=None, timeout=30):
    """调用 Docker Engine API，返回 (status:int, body:bytes)。
    docker.sock 不可达或任何 socket 错误，统一降级返回 (0, b"")，交由调用方降级处理，
    避免状态页在 docker 不可用时长时间挂起。"""
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(3)            # 连接阶段短超时：socket 不存在应快速降级
        sock.connect(DOCKER_SOCK)
        sock.settimeout(timeout)      # 读取响应用业务超时
        headers = ["%s %s HTTP/1.1" % (method, path), "Host: docker", "Accept: */*",
                   "Connection: close"]
        data = None
        if body is not None:
            if isinstance(body, str):
                body = body.encode()
            headers.append("Content-Type: application/json")
            headers.append("Content-Length: %d" % len(body))
            data = body
        sock.sendall(("\r\n".join(headers) + "\r\n\r\n").encode() + (data or b""))
        raw = b""
        while True:
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                break
            if not chunk:
                break
            raw += chunk
        sock.close()
    except OSError:
        return 0, b""
    head, sep, body_bytes = raw.partition(b"\r\n\r\n")
    if not sep:
        return 0, raw
    htext = head.decode("utf-8", "replace")
    m = re.search(r"HTTP/1\.1 (\d+)", htext)
    status = int(m.group(1)) if m else 0
    if re.search(r"Transfer-Encoding:\s*chunked", htext, re.I):
        return status, _unchunk(body_bytes)
    mlen = re.search(r"Content-Length:\s*(\d+)", htext, re.I)
    if mlen:
        return status, body_bytes[:int(mlen.group(1))]
    return status, body_bytes


def _unchunk(data):
    out = bytearray()
    i = 0
    while i < len(data):
        j = data.find(b"\r\n", i)
        if j == -1:
            break
        try:
            size = int(data[i:j].split(b";")[0], 16)
        except ValueError:
            break
        if size == 0:
            break
        i = j + 2
        out += data[i:i + size]
        i += size + 2
    return bytes(out)


def demux_logs(b):
    """Docker 日志在 TTY=false 时为 8 字节帧多路复用，剥离帧头。"""
    res = bytearray()
    i, n = 0, len(b)
    while i + 8 <= n:
        size = int.from_bytes(b[i + 4:i + 8], "big")
        i += 8
        if i + size > n:
            size = n - i
        res += b[i:i + size]
        i += size
    return res.decode("utf-8", "replace")


def get_containers():
    """返回 {name: {state, status, health, created, id}}，仅含 BM_CONTAINERS。"""
    status, body = docker_api("GET", "/containers/json?all=1", timeout=15)
    out = {}
    if status != 200:
        return out
    try:
        arr = json.loads(body)
    except Exception:
        return out
    for c in arr:
        names = [n.lstrip("/") for n in c.get("Names", [])]
        for nm in names:
            if nm in BM_CONTAINERS:
                health = None
                hobj = c.get("Health")
                if isinstance(hobj, dict):
                    health = hobj.get("Status")
                elif isinstance(c.get("State"), dict):
                    hs = c["State"].get("Health")
                    if isinstance(hs, dict):
                        health = hs.get("Status")
                if health is None:
                    st = c.get("Status", "")
                    if "unhealthy" in st:
                        health = "unhealthy"
                    elif "healthy" in st:
                        health = "healthy"
                out[nm] = {
                    "state": c.get("State", "unknown"),
                    "status": c.get("Status", ""),
                    "health": health,
                    "created": c.get("Created", ""),
                    "id": c.get("Id", ""),
                }
    return out


def docker_exec(cmd, timeout=40):
    """在 bitmagnet-postgres 跑 psql；返回解析后的文本（demux 后）。"""
    status, body = docker_api("POST", "/containers/bitmagnet-postgres/exec",
                              json.dumps({"Cmd": cmd, "AttachStdout": True, "AttachStderr": True}),
                              timeout=timeout)
    if status != 201:
        return None
    try:
        eid = json.loads(body)["Id"]
    except Exception:
        return None
    status2, out = docker_api("POST", "/exec/%s/start" % eid,
                              json.dumps({"Detach": False, "Tty": False}), timeout=timeout)
    if status2 != 200:
        return None
    return demux_logs(out)


# --------------------------------------------------------------------------- #
# BitMagnet 客户端（Postgres / Torznab）
# --------------------------------------------------------------------------- #
def pg_count():
    txt = docker_exec(["psql", "-U", "postgres", "-d", "bitmagnet", "-tAc", "SELECT count(*) FROM torrents"])
    if txt is None:
        return None
    m = re.search(r"\d+", txt)
    return int(m.group()) if m else None


_PG_CACHE = {"ts": 0.0, "val": None}


def bm_total():
    """已索引种子总数（pg_count，缓存 60s）；失败返回 None。"""
    now = time.time()
    if now - _PG_CACHE["ts"] < 60:
        return _PG_CACHE["val"]
    v = pg_count()
    _PG_CACHE["ts"] = now
    _PG_CACHE["val"] = v
    return v


def _parse_size(s):
    try:
        return int(s)
    except Exception:
        return None


def bm_indexed(limit=50, offset=0, q=None, ctype=None, sort="updated", order="desc",
               min_size=None, max_size=None, min_seeders=None):
    """已索引种子列表：直读 Postgres，支持关键词 / 类型 / 排序 / 大小区间。"""
    try:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
    except Exception:
        limit, offset = 50, 0
    inner_where = []
    params = []
    # torrent_contents 存了真实做种数（bitmagnet 本体 torrents 表没有该列），
    # 借此支持「按做种数排序」和「只看还有源的种子」，避免点到死种。
    seed_sub = ("(SELECT tc.seeders FROM torrent_contents tc WHERE tc.info_hash=tor.info_hash "
                "ORDER BY tc.seeders DESC NULLS LAST LIMIT 1)")
    # 无关键词时「仅看有源」要对 33 万行逐行跑子查询（实测 >25s 超时并拖慢库），
    # 故只在有关键词时生效；前端会在空搜索勾选时给出提示。
    if min_seeders is not None and not q:
        min_seeders = None
    if min_seeders is not None:
        try:
            inner_where.append("COALESCE(%s,0) >= %d" % (seed_sub, max(0, int(min_seeders))))
        except Exception:
            pass
    if q:
        qe = str(q).replace("'", "''")
        inner_where.append("tor.name ILIKE '%%%s%%'" % qe)
    if ctype and q:
        c = ctype.replace("'", "''")
        if ctype in ("unknown", "未分类"):
            inner_where.append("tor.info_hash NOT IN (SELECT info_hash FROM torrent_contents "
                              "WHERE content_type IS NOT NULL AND content_type NOT IN ('unknown','未分类',''))")
        else:
            inner_where.append("tor.info_hash IN (SELECT info_hash FROM torrent_contents WHERE content_type = '%s')" % c)
    if min_size is not None:
        inner_where.append("tor.size >= %d" % int(min_size))
    if max_size is not None:
        inner_where.append("tor.size <= %d" % int(max_size))
    iw = (" WHERE " + " AND ".join(inner_where)) if inner_where else ""
    # 「按做种数排序」要对每行跑一次子查询再全表排序。33 万行下实测 >30s，
    # 且会拖慢/阻塞 DHT 爬虫写入（曾因此堵住 INSERT）。故仅在有过滤条件
    # （关键词/类型/大小/最少做种）时才启用，否则回退「最近更新」。
    if sort == "seeders" and not inner_where:
        sort = "updated"
    if sort == "seeders":
        col = seed_sub
        ob = ("ASC" if order == "asc" else "DESC") + " NULLS LAST"
    else:
        col = "tor.updated_at" if sort == "updated" else ("tor.size" if sort == "size" else "tor.name")
        ob = "ASC" if order == "asc" else "DESC"
    sql = (
        "SELECT json_agg(row_to_json(t)) FROM ("
        " SELECT encode(tor.info_hash,'hex') AS infohash, tor.name, tor.size,"
        " (SELECT tc.content_type FROM torrent_contents tc WHERE tc.info_hash=tor.info_hash ORDER BY tc.seeders DESC NULLS LAST LIMIT 1) AS content_type,"
        " (SELECT tc.seeders FROM torrent_contents tc WHERE tc.info_hash=tor.info_hash ORDER BY tc.seeders DESC NULLS LAST LIMIT 1) AS seeders,"
        " (SELECT tc.leechers FROM torrent_contents tc WHERE tc.info_hash=tor.info_hash ORDER BY tc.seeders DESC NULLS LAST LIMIT 1) AS leechers"
        " FROM torrents tor" + iw +
        " ORDER BY " + col + " " + ob + " LIMIT %d OFFSET %d"
        ") t;" % (limit, offset)
    )
    # statement_timeout 是安全底线：宁可报错也不让慢查询长时间占着库、
    # 阻塞 bitmagnet 的 DHT 写入。
    # -q 必须加：否则 SET 语句自身会输出 "SET" 污染结果，导致 JSON 解析失败
    txt = docker_exec(["psql", "-U", "postgres", "-d", "bitmagnet", "-tAqc",
                       "SET statement_timeout='25s'; " + sql], timeout=40)
    if txt is None:
        return {"ok": False, "error": "Postgres 查询失败"}
    txt = txt.strip()
    if not txt or txt.startswith("ERROR"):
        if not txt:
            return {"ok": True, "count": 0, "items": []}
        return {"ok": False, "error": txt[:120]}
    try:
        arr = json.loads(txt)
    except Exception as e:
        return {"ok": False, "error": "解析失败: %s" % str(e)[:60], "raw": txt[:200]}
    items = []
    for r in arr:
        ih = r.get("infohash", "")
        items.append({
            "infohash": ih,
            "name": r.get("name", ""),
            "size": r.get("size", 0),
            "content_type": r.get("content_type") or "未分类",
            "seeders": r.get("seeders"),
            "leechers": r.get("leechers"),
            "magnet": ("magnet:?xt=urn:btih:%s" % ih) if ih else "",
        })
    tmdb_enrich(items)
    return {"ok": True, "count": len(items), "items": items}


def bm_search(q):
    """实时 DHT 搜索：代理 BitMagnet Torznab t=search，补库外冷门资源。"""
    q = (q or "").strip()
    if not q:
        return {"ok": False, "error": "缺少关键词"}
    url = BITMAGNET_HOST + "/torznab/?t=search&q=" + urllib.parse.quote(q)
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
    except Exception as e:
        return {"ok": False, "error": "Torznab 请求失败: %s" % str(e)[:100]}
    try:
        root = ET.fromstring(data)
    except Exception as e:
        return {"ok": False, "error": "解析失败: %s" % str(e)[:80]}
    items = []
    for it in _find_local(root, "item"):
        title = _child_text(it, "title")
        size = _parse_size(_child_text(it, "size"))
        seeders = _parse_size(_child_text(it, "seeders"))
        leechers = _parse_size(_child_text(it, "leechers"))
        link = _child_text(it, "link")
        encl = _child_attr(it, "enclosure", "url")
        magnet = link if (link and link.startswith("magnet:")) else (encl if (encl and encl.startswith("magnet:")) else "")
        items.append({
            "title": title, "magnet": magnet, "size": size,
            "seeders": seeders, "leechers": leechers, "infohash": _btih(magnet),
        })
    return {"ok": True, "count": len(items), "items": items}


def _find_local(root, tag):
    out = []
    for el in root.iter():
        t = el.tag
        if t == tag or (isinstance(t, str) and t.endswith("}" + tag)):
            out.append(el)
    return out


def _child_text(el, tag):
    for c in el.iter():
        t = c.tag
        if t == tag or (isinstance(t, str) and t.endswith("}" + tag)):
            return (c.text or "").strip()
    return ""


def _child_attr(el, tag, attr):
    for c in el.iter():
        t = c.tag
        if t == tag or (isinstance(t, str) and t.endswith("}" + tag)):
            return c.attrib.get(attr, "")
    return ""


def _btih(magnet):
    if not magnet:
        return ""
    m = re.search(r"urn:btih:([A-Za-z0-9]+)", magnet)
    if not m:
        return ""
    v = m.group(1)
    if re.fullmatch(r"[0-9a-fA-F]{40}", v):
        return v.lower()
    try:
        v = v.upper()
        pad = v + "=" * (-len(v) % 8)
        return base64.b32decode(pad).hex()
    except Exception:
        return ""


# --------------------------------------------------------------------------- #
# TMDB 元数据（海报 / 年份）—— bot 无直连外网，出网经 squid 代理；失败则跳过
# --------------------------------------------------------------------------- #
_TMDB_CACHE = {}  # key -> {"poster":..,"year":..,"ts":..}
_TMDB_CACHE_LOCK = threading.Lock()


def _tmdb_lookup(name, ctype):
    """查 TMDB 海报 + 年份；返回 (poster_url_or_None, year_or_None)。
    缓存策略：命中(有海报) 24h；TMDB 明确无结果(确定性负) 1h；瞬时错误(超时/出网抖) 仅 2min，
    出网恢复后尽快重试，避免 flaky egress 把卡片永久晾成空白。"""
    key = (name or "").lower() + "|" + (ctype or "")
    with _TMDB_CACHE_LOCK:
        c = _TMDB_CACHE.get(key)
        if c:
            ttl = 86400 if c.get("poster") else (120 if c.get("err") else 3600)
            if (time.time() - c["ts"]) < ttl:
                return c.get("poster"), c.get("year")
    poster = None
    year = None
    transient = False
    live = _bm_live_env()
    tmdb_key = live.get("TMDB_API_KEY") or TMDB_API_KEY
    tmdb_proxy = live.get("TMDB_PROXY") or TMDB_PROXY
    if tmdb_key and name:
        media = "tv" if ctype in ("tv_show", "tv") else "movie"
        q = urllib.parse.quote(name)
        url = "https://api.tmdb.org/3/search/%s?api_key=%s&query=%s&language=zh-CN" % (media, tmdb_key, q)
        try:
            handlers = []
            if tmdb_proxy:
                handlers.append(urllib.request.ProxyHandler(
                    {"http": tmdb_proxy, "https": tmdb_proxy}))
            opener = urllib.request.build_opener(*handlers)
            req = urllib.request.Request(url, headers={"User-Agent": "bm-bot/1.0"})
            with opener.open(req, timeout=2.5) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
            res = (j.get("results") or []) if isinstance(j, dict) else []
            if res:
                top = res[0]
                p = top.get("poster_path")
                if p:
                    poster = "https://image.tmdb.org/t/p/w154" + p
                yr = top.get("release_date") or top.get("first_air_date") or ""
                if yr and len(yr) >= 4:
                    try:
                        year = int(yr[:4])
                    except Exception:
                        year = None
        except Exception:
            transient = True
    with _TMDB_CACHE_LOCK:
        _TMDB_CACHE[key] = {"poster": poster, "year": year, "ts": time.time(), "err": transient}
    return poster, year


def tmdb_enrich(items):
    """并发给 movie/tv_show 卡片补全海报+年份（best-effort，失败跳过，不阻塞首屏）。
    不依赖出网探测前置跳过——bot 容器被 fnOS 挡住、拿不到可靠的出网读数，
    且 generate_204 与 TMDB 可达性并不相关；未命中走 15min 负缓存即可避免 outage 期慢超时。"""
    targets = [(i, it) for i, it in enumerate(items)
               if it.get("content_type") in ("movie", "tv_show", "tv") and not it.get("poster")]
    if not targets:
        return
    try:
        with ThreadPoolExecutor(max_workers=10) as ex:
            futs = {}
            for i_it in targets:
                i, it = i_it
                futs[ex.submit(_tmdb_lookup, it.get("name", ""), it.get("content_type"))] = i
            for f in as_completed(futs, timeout=10):
                i = futs[f]
                try:
                    poster, year = f.result()
                    items[i]["poster"] = poster
                    items[i]["year"] = year
                except Exception:
                    pass
    except Exception:
        pass


# --------------------------------------------------------------------------- #
# qBittorrent 客户端
# --------------------------------------------------------------------------- #
# --- tracker 注入 -----------------------------------------------------------
# bitmagnet 产生的 magnet 只有 xt/dn/xl，不含 tr=。没有 tracker 时 qB 仅靠
# DHT/PeX/LSD 找 peer，冷门/老种基本找不到源（seeds=0 -> stalledDL，永远不动）。
# 故统一追加公共 tracker，这是「一键下载」能否真正跑起来的关键。
TRACKER_LIST_URL = os.environ.get(
    "TRACKER_LIST_URL",
    "https://raw.githubusercontent.com/ngosang/trackerslist/master/trackers_best.txt")
_TRACKER_CACHE = {"ts": 0.0, "list": []}
_TRACKER_TTL = 12 * 3600
# 兜底名单（拉取失败时用）。已剔除实测失效的 open.demonii.com / tracker.torrent.eu.org
TRACKERS_FALLBACK = [
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.stealth.si:80/announce",
    "udp://tracker.qu.ax:6969/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.nyaa.vc:6969/announce",
    "udp://explodie.org:6969/announce",
    "udp://tracker.corpscorp.online:80/announce",
    "udp://tracker.bittor.pw:1337/announce",
    "udp://tracker.ducks.party:1984/announce",
    "udp://tracker-udp.gbitt.info:80/announce",
    "http://tracker.dler.org:6969/announce",
    "udp://tracker2.dler.org:80/announce",
    "http://tracker.dler.com:6969/announce",
    "http://tracker.renfei.net:8080/announce",
    "udp://tracker.peerfect.org:6969/announce",
    "udp://tracker.opentrackr.com:6969/announce",
    "udp://tracker.ilibr.org:6969/announce",
    "udp://tracker.farted.net:6969/announce",
    "udp://retracker01-msk-virt.corbina.net:80/announce",
    "udp://tracker.cyberia.is:6969/announce",
]


def get_trackers():
    """公共 tracker 列表：优先在线拉取（经 TMDB_PROXY 出网），失败回落内置名单。缓存 12h。"""
    now = time.time()
    if _TRACKER_CACHE["list"] and (now - _TRACKER_CACHE["ts"]) < _TRACKER_TTL:
        return _TRACKER_CACHE["list"]
    lst = []
    try:
        handlers = []
        if TMDB_PROXY:
            handlers.append(urllib.request.ProxyHandler(
                {"http": TMDB_PROXY, "https": TMDB_PROXY}))
        opener = urllib.request.build_opener(*handlers)
        req = urllib.request.Request(TRACKER_LIST_URL, headers={"User-Agent": "bm-bot/1.0"})
        with opener.open(req, timeout=20) as r:
            body = r.read().decode("utf-8", "replace")
        lst = [x.strip() for x in body.splitlines() if x.strip()]
        lst = [x for x in lst if "demonii.com" not in x and "torrent.eu.org" not in x]
    except Exception:
        lst = []
    if not lst:
        lst = list(TRACKERS_FALLBACK)
    _TRACKER_CACHE["ts"] = now
    _TRACKER_CACHE["list"] = lst
    return lst


def refresh_trackers():
    """手动刷新 tracker 列表缓存（清空 12h TTL 后重新拉取）。"""
    try:
        _TRACKER_CACHE["list"] = []
        _TRACKER_CACHE["ts"] = 0.0
        lst = get_trackers()
        return {"ok": True, "count": len(lst), "sample": lst[:3]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}

def with_trackers(magnet):
    """给 magnet 追加公共 tracker；原本就带 tr= 的原样返回。"""
    if not magnet.lower().startswith("magnet:") or "&tr=" in magnet.lower():
        return magnet
    try:
        trs = get_trackers()
    except Exception:
        trs = []
    if not trs:
        return magnet
    return magnet + "".join("&tr=" + urllib.parse.quote(t, safe="") for t in trs)


def qbit_add_trackers(hashes):
    """给已存在的种子补 tracker（救活 stalledDL 的任务）。返回 (ok_count, err_or_None)。"""
    hdrs, _token, err = qbit_session()
    if err:
        return 0, err
    trs = get_trackers()
    if not trs:
        return 0, "无可用 tracker"
    h2 = dict(hdrs)
    h2["Content-Type"] = "application/x-www-form-urlencoded"
    urls = "\n".join(trs)
    ok = 0
    for h in hashes:
        try:
            body = urllib.parse.urlencode({"hash": h, "urls": urls}).encode()
            req = urllib.request.Request(
                QBIT_URL + "/api/v2/torrents/addTrackers", data=body, headers=h2)
            with urllib.request.urlopen(req, timeout=20, context=SSL_CTX) as r:
                r.read()
            ok += 1
        except Exception:
            pass
    return ok, None


def qbit_session():
    """登录 qBittorrent v5，返回 (headers_dict, csrftoken_or_None, error_or_None)。"""
    try:
        data = urllib.parse.urlencode({"username": QBIT_USER, "password": QBIT_PASS}).encode()
        req = urllib.request.Request(QBIT_URL + "/api/v2/auth/login", data=data,
                                     headers={"Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=10, context=SSL_CTX) as r:
            ck = r.headers.get("Set-Cookie", "")
    except Exception as e:
        return None, None, "qBittorrent 登录失败: %s" % str(e)[:80]
    m = re.search(r"(QBT_SID_\d+=[^;]+)", ck) or re.search(r"(SID=[^;]+)", ck)
    if not m:
        return None, None, "未拿到 qBittorrent 会话"
    cm = re.search(r"(csrftoken=[^;]+)", ck)
    hdrs = {"Cookie": m.group(1) + ("; " + cm.group(1) if cm else "")}
    token = cm.group(1).split("=", 1)[1] if cm else None
    if token:
        hdrs["X-Csrftoken"] = token
    return hdrs, token, None


def qbit_add_magnet(magnet, category=QBIT_CATEGORY):
    """把 magnet 直接提交给 qBittorrent 下载（复用 v5 会话 Cookie + CSRF 登录）。"""
    magnet = (magnet or "").strip()
    if not magnet.lower().startswith("magnet:"):
        # 允许直接贴 40 位 infohash
        if re.fullmatch(r"[0-9a-fA-F]{40}", magnet):
            magnet = "magnet:?xt=urn:btih:" + magnet.lower()
        else:
            return False, "无效的 magnet / infohash"
    magnet = with_trackers(magnet)
    hdrs, _token, err = qbit_session()
    if err:
        return False, err
    add_hdrs = dict(hdrs)
    add_hdrs["Content-Type"] = "application/x-www-form-urlencoded"
    last = ""
    for cat in (category, ""):
        try:
            form = {"urls": magnet}
            if cat:
                form["category"] = cat
            body = urllib.parse.urlencode(form).encode()
            req = urllib.request.Request(QBIT_URL + "/api/v2/torrents/add", data=body, headers=add_hdrs)
            with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
                r.read()
            return True, "已提交 qBittorrent 下载" + (("（分类 %s）" % cat) if cat else "（默认目录）")
        except urllib.error.HTTPError as ex:
            if ex.code == 409:
                return True, "已在下载队列中（重复，未重复添加）"
            last = "HTTP %d" % ex.code
        except Exception as e:
            last = str(e)[:80]
    return False, "添加失败: %s" % last


def qbit_queue(category=QBIT_CATEGORY, show_all=False):
    """读取 qBittorrent 下载队列；默认只看本下载台分类（bitmagnet）。"""
    hdrs, _token, err = qbit_session()
    if err:
        return {"ok": False, "error": err}
    try:
        url = QBIT_URL + "/api/v2/torrents/info"
        if not show_all and category:
            url += "?category=" + urllib.parse.quote(category)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            arr = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": "读取队列失败: %s" % str(e)[:80]}
    order = {"downloading": 0, "stalledDL": 1, "queuedDL": 2, "pausedDL": 3,
             "uploading": 4, "stalledUP": 5, "queuedUP": 6, "pausedUP": 7,
             "checkingDL": 8, "checkingUP": 9, "error": 10, "missingFiles": 11}
    items = [{
        "hash": t.get("hash", ""),
        "name": t.get("name", ""),
        "size": t.get("size", 0),
        "progress": round(t.get("progress", 0) * 100, 1),
        "state": t.get("state", ""),
        "category": t.get("category", ""),
        "dlspeed": t.get("dlspeed", 0),
        "upspeed": t.get("upspeed", 0),
        "seeders": t.get("num_seeds", 0),
        "leechers": t.get("num_leechs", 0),
    } for t in arr]
    items.sort(key=lambda x: order.get(x["state"], 99))
    return {"ok": True, "count": len(items), "items": items}


def qbit_downloaded(show_all=False):
    """返回 qBittorrent 当前所有种子 infohash 集合（用于已下载去重标记）。"""
    hdrs, _token, err = qbit_session()
    if err:
        return {"ok": False, "error": err}
    try:
        url = QBIT_URL + "/api/v2/torrents/info"
        if not show_all and QBIT_CATEGORY:
            url += "?category=" + urllib.parse.quote(QBIT_CATEGORY)
        req = urllib.request.Request(url, headers=hdrs)
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            arr = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"ok": False, "error": "读取失败: %s" % str(e)[:80]}
    hashes = [t.get("hash", "").lower() for t in arr if t.get("hash")]
    return {"ok": True, "infohashes": hashes}


def qbit_action(action, hashes, category=None, delete_files=False):
    """队列内操作：pause/resume/recheck/delete/setCategory。"""
    hdrs, _token, err = qbit_session()
    if err:
        return {"ok": False, "error": err}
    path_map = {
        "pause": "/api/v2/torrents/stop",
        "resume": "/api/v2/torrents/start",
        "recheck": "/api/v2/torrents/recheck",
        "delete": "/api/v2/torrents/delete",
        "setCategory": "/api/v2/torrents/setCategory",
    }
    if action not in path_map:
        return {"ok": False, "error": "未知操作"}
    if isinstance(hashes, list):
        hashes = ",".join(hashes)
    body = {"hashes": str(hashes)}
    if action == "delete":
        body["deleteFiles"] = bool(delete_files)
    if action == "setCategory":
        body["category"] = category or ""
    try:
        req = urllib.request.Request(QBIT_URL + path_map[action],
                                     data=urllib.parse.urlencode(body).encode(),
                                     headers={**hdrs, "Content-Type": "application/x-www-form-urlencoded"})
        with urllib.request.urlopen(req, timeout=15, context=SSL_CTX) as r:
            r.read()
        return {"ok": True}
    except urllib.error.HTTPError as ex:
        return {"ok": False, "error": "HTTP %d" % ex.code}
    except Exception as e:
        return {"ok": False, "error": str(e)[:120]}


# --------------------------------------------------------------------------- #
# Clash / OpenClash 出网自愈（bot 自带，复用看门狗逻辑）
# --------------------------------------------------------------------------- #
def clash_req(method, path, body=None, timeout=12):
    url = CLASH_API + path
    req = urllib.request.Request(url, method=method)
    req.add_header("Authorization", "Bearer " + CLASH_TOKEN)
    if body is not None:
        data = json.dumps(body).encode()
        req.data = data
        req.add_header("Content-Type", "application/json")
    handlers = []
    if CLASH_PROXY:
        handlers.append(urllib.request.ProxyHandler(
            {"http": CLASH_PROXY, "https": CLASH_PROXY}))
    opener = urllib.request.build_opener(*handlers) if handlers else urllib.request
    try:
        with opener.open(req, timeout=timeout) as r:
            txt = r.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = None
        return r.status, j, txt
    except urllib.error.HTTPError as e:
        txt = e.read().decode("utf-8", "replace")
        try:
            j = json.loads(txt)
        except Exception:
            j = None
        return e.code, j, txt
    except Exception as e:
        return 0, None, str(e)[:120]


_EGRESS_CACHE = {"ts": 0.0, "val": None}


def _clash_node():
    """尽力拿当前 GLOBAL 节点名（bot 可能被 fnOS 挡住拿不到，返回 None 即可）。"""
    try:
        _, g, _ = clash_req("GET", "/proxies/GLOBAL")
        return g.get("now") if isinstance(g, dict) else None
    except Exception:
        return None


def egress_probe():
    """探测当前出网：bot 容器被 fnOS 防火墙挡住、无法直连 Clash API，
    故经宿主 squid 代理测 generate_204（squid 能出网即代表出网通）。结果缓存 30s。"""
    now = time.time()
    if now - _EGRESS_CACHE["ts"] < 30 and _EGRESS_CACHE["val"] is not None:
        return _EGRESS_CACHE["val"]
    node = _clash_node()
    ok = False
    try:
        handlers = []
        if TMDB_PROXY:
            handlers.append(urllib.request.ProxyHandler(
                {"http": TMDB_PROXY, "https": TMDB_PROXY}))
        opener = urllib.request.build_opener(*handlers)
        with opener.open(urllib.request.Request("http://www.google.com/generate_204"),
                         timeout=6) as r:
            ok = (r.status == 204)
    except Exception:
        ok = False
    val = {"ok": ok, "latency": None, "node": node,
           "error": (None if ok else "timeout")}
    _EGRESS_CACHE["ts"] = now
    _EGRESS_CACHE["val"] = val
    return val


def fix_egress():
    """挑最快可用节点钉到 GLOBAL（排除 JUNK 黑名单），钉前实测出网。
    仅当 CLASH_API_URL 已配置时可用。"""
    if not CLASH_API:
        return {"ok": False, "error": "未配置 CLASH_API_URL，出网自愈不可用（请在环境变量中配置）"}
    _, g, _ = clash_req("GET", "/proxies/GLOBAL")
    if not isinstance(g, dict):
        return {"ok": False, "error": "无法连接 Clash 控制器（CLASH_API_URL 不可达）；出网由 NAS 看门狗自动恢复，请稍候重试"}
    cands = [n for n in g.get("all", [])
             if n not in ("DIRECT", "REJECT") and not any(k in n for k in JUNK_KEYWORDS)]
    cands.sort(key=lambda n: (0 if any(n.startswith(p) for p in PREFERRED_PREFIXES) else 1, n))
    tested = 0
    for node in cands:
        if tested >= 6:
            break
        tested += 1
        st, j, _ = clash_req("GET", "/proxies/" + urllib.parse.quote(node, safe="") +
                             "/latency?url=" + urllib.parse.quote("http://www.google.com/generate_204", "") +
                             "&timeout=2500")
        lat = j.get("latency") if isinstance(j, dict) else None
        if st == 200 and isinstance(lat, (int, float)):
            clash_req("PUT", "/proxies/GLOBAL", {"name": node})
            p = egress_probe()
        if p["ok"]:
            _EGRESS_CACHE["ts"] = 0
            return {"ok": True, "pinned": node, "latency": lat}
    if cands:
        clash_req("PUT", "/proxies/GLOBAL", {"name": cands[0]})
        _EGRESS_CACHE["ts"] = 0
        return {"ok": False, "pinned": cands[0], "error": "候选均不出网，已兜底钉 " + cands[0]}
    return {"ok": False, "error": "无可用节点"}


# --------------------------------------------------------------------------- #
# 隧道 / 索引维护
# --------------------------------------------------------------------------- #
def tunnel_status():
    _h, _p = _host_port(BITMAGNET_HOST)
    reachable = tcp_ok(_h, _p) if _h else False
    cons = get_containers()
    bm = cons.get("bitmagnet", {})
    return {"ok": True, "reachable": reachable,
            "bitmagnet_state": bm.get("state"), "bitmagnet_health": bm.get("health")}


def tunnel_restart():
    """重启 bitmagnet 容器以重挂隧道网络命名空间（netns 孤儿陷阱修复）。"""
    status, _ = docker_api("POST", "/containers/bitmagnet/restart", timeout=60)
    return {"ok": status in (204, 200), "status": status}


def bm_stats():
    total = bm_total()
    by_type = []
    try:
        txt = docker_exec(["psql", "-U", "postgres", "-d", "bitmagnet", "-tAc",
                           "SELECT COALESCE(content_type,'unknown') AS ct, count(*) FROM torrent_contents GROUP BY ct ORDER BY 2 DESC"],
                          timeout=40)
        for line in (txt or "").strip().splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            a, b = line.rsplit("|", 1)
            by_type.append({"type": a.strip(), "count": int(b.strip())})
    except Exception:
        pass
    recent24h = None
    try:
        t = docker_exec(["psql", "-U", "postgres", "-d", "bitmagnet", "-tAc",
                         "SELECT count(*) FROM torrents WHERE updated_at > now()-interval '24 hours'"], timeout=40)
        m = re.search(r"\d+", t or "")
        recent24h = int(m.group()) if m else None
    except Exception:
        pass
    return {"ok": True, "total": total, "by_type": by_type, "recent24h": recent24h}


_MAINTAIN = {"running": False, "action": None, "started": 0, "log": "", "done": False, "ok": None}
_MAINTAIN_LOCK = threading.Lock()


def maintain_start(action):
    with _MAINTAIN_LOCK:
        if _MAINTAIN["running"]:
            return {"ok": False, "error": "已有维护任务进行中"}
        if action not in ("reprocess", "reclassify"):
            return {"ok": False, "error": "未知操作"}
        _MAINTAIN.update({"running": True, "action": action, "started": time.time(),
                          "log": "", "done": False, "ok": None})
    threading.Thread(target=_maintain_run, args=(action,), daemon=True).start()
    return {"ok": True, "status": "started", "action": action}


def _maintain_run(action):
    cmd = {"reprocess": "reprocess", "reclassify": "reclassify"}[action]
    status, body = docker_api("POST", "/containers/bitmagnet/exec",
                              json.dumps({"Cmd": ["bitmagnet", cmd], "AttachStdout": True, "AttachStderr": True}),
                              timeout=30)
    ok = False
    log = ""
    if status == 201:
        try:
            eid = json.loads(body)["Id"]
        except Exception:
            eid = None
        if eid:
            s2, out = docker_api("POST", "/exec/%s/start" % eid,
                                 json.dumps({"Detach": False, "Tty": False}), timeout=1800)
            log = demux_logs(out)
            ok = (s2 == 200)
    with _MAINTAIN_LOCK:
        _MAINTAIN.update({"running": False, "done": True, "ok": ok, "log": (log or "")[-4000:]})


def maintain_status():
    with _MAINTAIN_LOCK:
        return dict(_MAINTAIN)


def tcp_ok(host, port, timeout=4):
    """TCP 端口连通性探测。"""
    try:
        s = socket.create_connection((host, int(port)), timeout=timeout)
        s.close()
        return True
    except Exception:
        return False


def system_status():
    """汇总多个系统的在线状态：BitMagnet 栈 + 出网链路。"""
    bm_h, bm_p = _host_port(BITMAGNET_HOST)
    qb_h, qb_p = _host_port(QBIT_URL) if QBIT_URL else (None, None)
    targets = [
        ("BitMagnet 索引/隧道", bm_h, bm_p, "bitmagnet", PUB_BM_URL or BITMAGNET_HOST or None),
        ("元数据库 Postgres", None, 5432, "bitmagnet-postgres", None),
        ("qBittorrent 下载器", qb_h, qb_p, None, PUB_QB_URL or QBIT_URL or None),
    ]
    cons = get_containers()
    out = []
    for label, host, port, dname, url in targets:
        # 未配 host（如 Postgres 只在容器网络内监听、端口未发布）时不探活，
        # 否则 create_connection(None, ...) 必然失败会把在线服务误报成离线。
        tcp = tcp_ok(host, port) if host else False
        d = cons.get(dname) if dname else None
        if d:
            running = d["state"] == "running"
            detail = "启动 %s" % fmt_uptime(d["created"])
            # 能查到容器时以容器状态为准；仅显式配了 host 才叠加 TCP 探活
            ok = running and (tcp if host else True)
        else:
            # port 可能为 None（如未配置 qBittorrent），不能直接用 %d 格式化
            detail = ("端口 %d" % port) if port else "未配置"
            ok = tcp
        out.append({
            "label": label, "ok": ok, "tcp": tcp,
            "running": d["state"] if d else None,
            "detail": detail, "url": url,
        })
    bm = bm_total()
    reachable = tcp_ok(bm_h, bm_p) if bm_h else False
    return {
        "systems": out,
        "bm_total": bm,
        "bm_reachable": reachable,
        "manager_uptime": _uptime_from_ts(),
    }


# --------------------------------------------------------------------------- #
# 工具函数
# --------------------------------------------------------------------------- #
def fmt_size(n):
    try:
        n = int(n)
    except Exception:
        return "?"
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if n < 1024:
            return ("%.1f %s" % (n, unit)).replace(".0 ", " ")
        n /= 1024
    return "%.1f PB" % n


def fmt_uptime(val):
    if val in (None, ""):
        return "?"
    try:
        secs = max(0, time.time() - float(val))
    except (ValueError, TypeError):
        try:
            dt = datetime.fromisoformat(str(val).replace("Z", "+00:00"))
            secs = max(0, (datetime.now(timezone.utc) - dt).total_seconds())
        except Exception:
            return "?"
    d, rem = divmod(int(secs), 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


def _uptime_from_ts():
    secs = int(time.time() - START_TS)
    d, rem = divmod(secs, 86400)
    h, rem = divmod(rem, 3600)
    m, _ = divmod(rem, 60)
    if d:
        return "%dd %dh" % (d, h)
    if h:
        return "%dh %dm" % (h, m)
    return "%dm" % m


# --------------------------------------------------------------------------- #
# HTTP 服务
# --------------------------------------------------------------------------- #
PAGE = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>BitMagnet 一键下载台</title>
<style>
  :root{ --bg:#0f1115; --panel:#171a21; --panel2:#1e222b; --line:#2a2f3a;
         --txt:#e6e9ef; --muted:#8b93a3; --acc:#5b8cff; --green:#3ad07a;
         --red:#ff6b6b; --amber:#ffb454; --chip:#262b36; }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--txt);
       font:14px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"PingFang SC","Microsoft YaHei",sans-serif}
  header{position:sticky;top:0;z-index:10;background:var(--bg);padding:14px 22px;border-bottom:1px solid var(--line);display:flex;align-items:center;gap:12px;flex-wrap:wrap}
  header h1{font-size:17px;margin:0;font-weight:600}
  header .sub{color:var(--muted);font-size:12px}
  .wrap{max-width:1000px;margin:0 auto;padding:18px}
  .tabs{display:flex;gap:6px;margin-bottom:16px;flex-wrap:wrap}
  .tab{padding:8px 14px;border-radius:8px;background:var(--panel);border:1px solid var(--line);
       color:var(--muted);cursor:pointer;font-size:14px;font-weight:500}
  .tab.active{background:var(--acc);color:#fff;border-color:var(--acc)}
  .panel{display:none}
  .panel.active{display:block}
  .grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(300px,1fr));gap:14px}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:14px}
  .card h3{margin:0 0 6px;font-size:15px;display:flex;align-items:center;gap:8px}
  .row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:10px 0}
  input,select{font:inherit;color:var(--txt);background:var(--panel2);border:1px solid var(--line);
       border-radius:8px;padding:8px 10px;outline:none}
  input:focus,select:focus{border-color:var(--acc)}
  textarea{font:inherit;color:var(--txt);background:var(--panel2);border:1px solid var(--line);
       border-radius:8px;padding:8px 10px;outline:none;width:100%;resize:vertical}
  button,a.btn{cursor:pointer;border:1px solid var(--line);background:var(--panel2);border-radius:8px;
       padding:8px 14px;color:var(--txt);text-decoration:none;display:inline-block;font-size:14px}
  button:hover,a.btn:hover{border-color:var(--acc)}
  button.primary,a.btn.primary{background:var(--acc);border-color:var(--acc);color:#fff}
  button.ghost,a.btn.ghost{background:transparent}
  button:disabled{opacity:.5;cursor:not-allowed}
  .badge{font-size:12px;padding:2px 9px;border-radius:999px;font-weight:600}
  .b-up{background:rgba(58,208,122,.15);color:var(--green)}
  .b-down{background:rgba(255,107,107,.15);color:var(--red)}
  .b-warn{background:rgba(255,180,84,.15);color:var(--amber)}
  .muted{color:var(--muted)}
  .hint{color:var(--muted);font-size:12px}
  .result{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 14px;margin-bottom:10px}
  .result .t{font-weight:600;margin-bottom:4px;word-break:break-all}
  .result .m{color:var(--muted);font-size:12px;margin-bottom:8px;word-break:break-all}
  .result .acts{display:flex;gap:8px;flex-wrap:wrap;align-items:center}
  .stat{display:flex;gap:14px;flex-wrap:wrap;margin:6px 0 18px}
  .stat .box{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:12px 18px;min-width:140px}
  .stat .box .n{font-size:22px;font-weight:700}
  .stat .box .l{color:var(--muted);font-size:12px;margin-top:2px}
  .toolbar{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0;padding:8px;background:var(--panel);border:1px solid var(--line);border-radius:10px}
  .toast{position:fixed;right:18px;bottom:18px;background:var(--panel2);border:1px solid var(--line);
         padding:10px 16px;border-radius:10px;opacity:0;transition:.25s;pointer-events:none;max-width:60vw;z-index:50}
  .toast.show{opacity:1}
  .spinner{display:inline-block;width:14px;height:14px;border:2px solid var(--muted);
           border-top-color:var(--acc);border-radius:50%;animation:sp .7s linear infinite;vertical-align:-2px}
  @keyframes sp{to{transform:rotate(360deg)}}
  .chips{display:flex;gap:6px;flex-wrap:wrap;margin-top:6px}
  .chip{padding:3px 10px;border-radius:999px;background:var(--chip);border:1px solid var(--line);font-size:12px;cursor:pointer}
  .chip:hover{border-color:var(--acc)}
  .dist{font-size:12px;color:var(--muted);margin:3px 0}
  .dist b{color:var(--txt)}
  .sel{width:16px;height:16px}
  [data-theme="light"]{ --bg:#f4f6f9; --panel:#ffffff; --panel2:#eef1f6; --line:#d7dce5;
     --txt:#1b2230; --muted:#5c6473; --acc:#3a6df0; --green:#1f9d57; --red:#d8453a;
     --amber:#b9770f; --chip:#e7ebf2; }
  @media(max-width:560px){
    .wrap{padding:12px} header{padding:10px 12px}
    header h1{font-size:15px} header .sub{display:none}
    .tabs{overflow-x:auto;flex-wrap:nowrap;margin-bottom:12px}
    .tab{white-space:nowrap;padding:7px 11px;font-size:13px}
    .grid{grid-template-columns:1fr}
    .toolbar{flex-direction:column;align-items:stretch}
    .toolbar button,.toolbar select{width:100%}
    .card{padding:12px}
  }
</style>
</head>
<body>
<header>
  <h1>🧲 BitMagnet 一键下载台</h1>
  <span class="sub">搜索公开 DHT 磁力 · 一键发到 qBittorrent</span>
  <span style="flex:1"></span>
  <button class="ghost" id="themeBtn" onclick="toggleTheme()">☀️ 亮色</button>
  <span class="muted" id="clock"></span>
</header>

<div class="wrap">
  <div class="tabs">
    <div class="tab active" data-p="indexed">🧲 已索引种子</div>
    <div class="tab" data-p="search">🔎 实时 DHT</div>
    <div class="tab" data-p="queue">📥 下载队列</div>
    <div class="tab" data-p="watch">❤️ 收藏</div>
    <div class="tab" data-p="status">📊 系统状态</div>
  </div>

  <!-- 已索引种子 -->
  <div class="panel active" id="p-indexed">
    <div class="row">
      <input id="idxTerm" style="flex:1;min-width:160px" placeholder="搜已索引种子名称…">
      <select id="idxType">
        <option value="">全部类型</option>
        <option value="movie">电影</option>
        <option value="tv_show">剧集</option>
        <option value="anime">动漫</option>
        <option value="music">音乐</option>
        <option value="software">软件</option>
        <option value="book">书</option>
        <option value="game">游戏</option>
        <option value="xxx">成人</option>
        <option value="other">其他</option>
        <option value="unknown">未分类</option>
      </select>
      <select id="idxSort">
        <option value="updated">最近更新</option>
        <option value="seeders">做种数（有源优先）</option>
        <option value="size">大小</option>
        <option value="name">名称</option>
      </select>
      <select id="idxOrder">
        <option value="desc">倒序</option>
        <option value="asc">正序</option>
      </select>
      <label class="muted" style="display:flex;align-items:center;gap:4px;white-space:nowrap"><input type="checkbox" id="idxAlive" style="width:auto;margin:0"> 仅看有源</label>
      <span class="muted">大小≥</span><input id="idxMin" style="width:90px" placeholder="MB" inputmode="numeric">
      <span class="muted">≤</span><input id="idxMax" style="width:90px" placeholder="MB" inputmode="numeric">
      <span class="muted" id="idxCount"></span>
    </div>
    <div class="toolbar" id="idxToolbar" style="display:none">
      <span class="muted">已选 <b id="idxSelN">0</b> 个</span>
      <button class="primary" id="idxBatchDl">批量下载</button>
      <button id="idxBatchWatch">收藏选中</button>
      <button class="ghost" id="idxSelAll">全选本页</button>
    </div>
    <div id="idxResults"></div>
    <div class="row" style="margin-top:8px">
      <button id="idxMore" onclick="loadIndexed(true)">加载更多</button>
    </div>
  </div>

  <!-- 实时 DHT -->
  <div class="panel" id="p-search">
    <div class="row">
      <input id="sTerm" style="flex:1;min-width:200px" placeholder="实时搜 DHT（补库外冷门资源）…">
      <button class="primary" onclick="loadSearch()">搜索</button>
    </div>
    <div class="chips" id="sHistory"></div>
    <div id="sResults"></div>
  </div>

  <!-- 下载队列 -->
  <div class="panel" id="p-queue">
    <div class="card" style="margin-bottom:14px">
      <div class="t" style="margin-bottom:6px">粘贴磁力直接下载</div>
      <textarea id="pasteArea" rows="3" placeholder="可粘贴多个 magnet: 链接或 40 位 infohash，每行/空格分隔"></textarea>
      <div class="row" style="margin-top:8px">
        <button class="primary" onclick="pasteDownload()">提交下载</button>
        <span class="hint">直接发到 qBittorrent（分类 bitmagnet），无需先搜索</span>
        <button id="idxRefreshTr" class="ghost">🔄 刷新 tracker 列表</button>
      </div>
    </div>
    <div class="row">
      <label class="muted" style="align-self:center"><input type="checkbox" id="qAll" onchange="loadQueue()"> 显示全部（不限本台分类）</label>
      <span class="muted" style="align-self:center" id="qCount"></span>
    </div>
    <div class="toolbar" id="qToolbar" style="display:none">
      <span class="muted">已选 <b id="qSelN">0</b> 个</span>
      <button id="qSelAll">全选本页</button>
      <button id="qBatchPause">批量暂停</button>
      <button id="qBatchResume">批量继续</button>
      <button id="qBatchDel">批量删除</button>
      <select id="qBatchCat"><option value="">批量改分类…</option>
        <option value="bitmagnet">bitmagnet</option><option value="radarr">radarr</option>
        <option value="sonarr">sonarr</option><option value="dht">dht</option><option value="">无</option></select>
    </div>
    <div id="queueResults"></div>
  </div>

  <!-- 收藏 -->
  <div class="panel" id="p-watch">
    <div class="row">
      <button class="ghost" onclick="clearWatch()">清空收藏</button>
      <span class="muted" id="watchCount"></span>
    </div>
    <div id="watchResults"></div>
  </div>

  <!-- 系统状态 -->
  <div class="panel" id="p-status">
    <div class="stat" id="stats"></div>
    <div class="grid" id="sysGrid"></div>
    <div class="card" style="margin-top:14px">
      <h3>🌐 出网健康</h3>
      <div id="egressBody" class="m"></div>
      <div class="row" style="margin-top:8px">
        <button onclick="loadEgress()">测试</button>
      </div>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>🪢 隧道状态</h3>
      <div id="tunnelBody" class="m"></div>
      <div class="row" style="margin-top:8px">
        <button onclick="loadTunnel()">刷新</button>
        <button id="restartBtn" onclick="restartTunnel()">🔄 重启 BitMagnet</button>
      </div>
      <div class="hint">tun 重启后 bitmagnet 网络命名空间会失效（3333 全不可达），重启 bitmagnet 容器即可重挂。</div>
    </div>
    <div class="card" style="margin-top:14px">
      <h3>📈 索引统计</h3>
      <div id="statsBody" class="m"></div>
      <div class="row" style="margin-top:8px">
        <button onclick="loadStats()">刷新</button>
        <button id="reclassifyBtn" onclick="maintain('reclassify')">重分类</button>
        <button id="reprocessBtn" onclick="maintain('reprocess')">重处理索引</button>
      </div>
      <div class="hint" id="maintainHint"></div>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
const $ = s => document.querySelector(s);
function toast(msg){const t=$("#toast");t.textContent=msg;t.classList.add("show");
  clearTimeout(t._t);t._t=setTimeout(()=>t.classList.remove("show"),2600);}
function jget(u){return fetch(u).then(r=>r.json());}
function jpost(u,b){return fetch(u,{method:"POST",headers:{"Content-Type":"application/json"},
  body:JSON.stringify(b||{})}).then(r=>r.json());}
function escAttr(s){return (s||"").replace(/&/g,"&amp;").replace(/"/g,"&quot;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function escHtml(s){return (s||"").replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");}
function fmtSize(n){n=Number(n)||0;const u=["B","KB","MB","GB","TB"];
  for(const x of u){if(n<1024)return (n.toFixed(n<10&&n>0?1:0)+" "+x);n/=1024;}return (n.toFixed(1)+" PB");}
const CT={movie:"电影",tv:"剧集",tv_show:"剧集",anime:"动漫",music:"音乐",software:"软件",book:"书",game:"游戏",other:"其他",xxx:"成人",unknown:"未分类"};

// 已下载集合（去重标记）
let dlSet = new Set();
async function refreshDownloaded(){
  try{ const d=await jget("/api/queue/hashes?all=1"); if(d.ok) dlSet=new Set(d.infohashes||[]); }catch(e){}
}

function download(mag){
  return jpost("/api/download",{magnet:mag}).then(d=>{
    if(d.ok) toast(d.message||"已提交"); else toast("下载失败："+(d.error||""));
    return d;
  });
}

// 通用卡片渲染
function cardHtml(it, opts){
  opts=opts||{};
  const ih=it.infohash||"";
  const isDl = ih && dlSet.has(ih.toLowerCase());
  const size=it.size?fmtSize(it.size):"—";
  const ct=it.content_type||"未分类";
  const ctLabel=CT[ct]||ct;
  const seed=it.seeders!=null?it.seeders:"?";
  const leec=it.leechers!=null?it.leechers:"?";
  let acts="";
  if(isDl){
    acts+='<span class="badge b-up">已下载 ✓</span>';
  }else if(it.magnet){
    acts+='<button class="primary" data-dl="'+escAttr(it.magnet)+'">直接下载</button>';
  }
  if(it.magnet){
    acts+='<button data-copy="'+escAttr(it.magnet)+'">复制磁力</button>';
    acts+='<a class="btn ghost" href="'+escAttr(it.magnet)+'" target="_blank" rel="noopener noreferrer">打开</a>';
  }
  if(opts.watch && it.magnet){
    acts+='<button data-watch="'+escAttr(it.magnet)+'" data-title="'+escAttr(it.title||it.name||"")+'">收藏</button>';
  }
  if(opts.checkbox && ih){
    acts='<input type="checkbox" class="sel" data-ih="'+escAttr(ih)+'"> '+acts;
  }
  const poster = it.poster ? '<img src="'+escAttr(it.poster)+'" onerror="this.style.display=\'none\'" loading="lazy" style="width:54px;height:80px;object-fit:cover;border-radius:6px;float:left;margin:0 10px 6px 0">':'';
  const year = it.year?(' ('+it.year+')'):'';
  let seedBadge;
  if(it.seeders==null) seedBadge='做种 <span class="badge">未知</span>';
  else if(it.seeders>0) seedBadge='做种 <span class="badge b-up">'+it.seeders+'</span>';
  else seedBadge='做种 <span class="badge b-down">0 无源</span>';
  return '<div class="result" style="overflow:hidden">'+
    poster+
    '<div class="t">'+(it.title||it.name||"(无标题)")+year+'</div>'+
    '<div class="m">类型 <span class="badge b-warn">'+ctLabel+'</span> · 大小 '+size+' · '+seedBadge+' · 下载 '+leec+'</div>'+
    '<div class="acts">'+acts+'</div></div>';
}

function bindCardActions(box){
  box.querySelectorAll("button[data-copy]").forEach(b=>{
    b.onclick=()=>{ const t=b.getAttribute("data-copy");
      if(navigator.clipboard&&navigator.clipboard.writeText) navigator.clipboard.writeText(t).then(()=>toast("已复制磁力"),()=>toast("复制失败"));
      else toast("浏览器不支持复制"); };
  });
  box.querySelectorAll("button[data-dl]").forEach(b=>{
    b.onclick=()=>{ const mag=b.getAttribute("data-dl"); b.disabled=true; const old=b.textContent; b.textContent="提交中…";
      download(mag).then(d=>{ b.textContent=d.ok?"已提交 ✓":old; if(!d.ok)b.disabled=false; }); };
  });
  box.querySelectorAll("button[data-watch]").forEach(b=>{
    b.onclick=()=>{ addWatch(b.getAttribute("data-watch"), b.getAttribute("data-title")); };
  });
}

// ---- 已索引种子 ----
let idxItems=[], idxOffset=0; const IDX_LIMIT=50;
async function loadIndexed(append){
  const box=$("#idxResults");
  if(!append){idxItems=[];idxOffset=0;box.innerHTML='<div class="muted">加载中…</div>';}
  else{$("#idxMore").disabled=true;$("#idxMore").textContent="加载中…";}
  const term=$("#idxTerm").value.trim();
  const p=new URLSearchParams({limit:IDX_LIMIT, offset:idxOffset,
    sort:$("#idxSort").value, order:$("#idxOrder").value});
  if(term) p.set("q",term);
  if($("#idxType").value){
    if(term) p.set("ctype",$("#idxType").value);
    else toast("类型筛选需配合关键词（已忽略）");
  }
  const mn=parseFloat($("#idxMin").value), mx=parseFloat($("#idxMax").value);
  if(!isNaN(mn)&&mn>0) p.set("min_size", Math.floor(mn*1024*1024));
  if(!isNaN(mx)&&mx>0) p.set("max_size", Math.floor(mx*1024*1024));
  if($("#idxAlive") && $("#idxAlive").checked) p.set("min_seeders","1");
  const d=await jget("/api/indexed?"+p.toString());
  if(!d.ok){box.innerHTML='<div class="muted">❌ '+(d.error||"加载失败")+'</div>';$("#idxMore").disabled=false;$("#idxMore").textContent="加载更多";return;}
  idxItems=idxItems.concat(d.items||[]); idxOffset+=IDX_LIMIT;
  renderIndexed();
  $("#idxMore").disabled=false;$("#idxMore").textContent="加载更多";
  $("#idxMore").style.display=(d.items&&d.items.length<IDX_LIMIT)?"none":"inline-block";
}
function renderIndexed(){
  const term=$("#idxTerm").value.trim().toLowerCase();
  const type=$("#idxType").value;
  const filtered=idxItems.filter(it=>{
    if(term && !(it.name||"").toLowerCase().includes(term)) return false;
    return true;
  });
  $("#idxCount").textContent=(term?("搜索“"+term+"” 命中 "+idxItems.length):"已加载 "+idxItems.length)+" 条";
  const box=$("#idxResults");
  if(!idxItems.length){box.innerHTML='<div class="muted">'+(term?("未找到匹配 “"+escHtml(term)+"” 的种子。"):"暂无已索引种子（BitMagnet 仍在抓 DHT，稍后再来）。")+'</div>';updateSelN();return;}
  box.innerHTML="";
  filtered.forEach(it=>box.insertAdjacentHTML("beforeend", cardHtml(it,{checkbox:true})));
  bindCardActions(box); updateSelN();
}
function updateSelN(){
  const n=document.querySelectorAll("#idxResults input.sel:checked").length;
  $("#idxSelN").textContent=n;
  $("#idxToolbar").style.display=n>0?"flex":"none";
}
$("#idxResults").addEventListener("change",e=>{ if(e.target.classList.contains("sel")) updateSelN(); });
let _idxDeb;
$("#idxTerm").addEventListener("input",()=>{ clearTimeout(_idxDeb); _idxDeb=setTimeout(()=>loadIndexed(false),300); });
$("#idxType").addEventListener("change",renderIndexed);
$("#idxSort").addEventListener("change",()=>loadIndexed(false));
if($("#idxAlive")) $("#idxAlive").addEventListener("change",()=>{
  if($("#idxAlive").checked && !$("#idxTerm").value.trim()) toast("「仅看有源」需配合关键词使用");
  loadIndexed(false);
});
$("#idxOrder").addEventListener("change",()=>loadIndexed(false));
$("#idxMin").addEventListener("change",()=>loadIndexed(false));
$("#idxMax").addEventListener("change",()=>loadIndexed(false));
$("#idxBatchDl").onclick=async()=>{
  const ihs=[...document.querySelectorAll("#idxResults input.sel:checked")].map(x=>x.dataset.ih);
  let ok=0,fail=0;
  for(const ih of ihs){ const r=await download("magnet:?xt=urn:btih:"+ih); if(r.ok)ok++;else fail++; }
  toast("批量下载：成功 "+ok+" / 失败 "+fail);
  document.querySelectorAll("#idxResults input.sel").forEach(x=>x.checked=false); updateSelN();
  refreshDownloaded();
};
$("#idxBatchWatch").onclick=()=>{
  const ihs=[...document.querySelectorAll("#idxResults input.sel:checked")];
  ihs.forEach(x=>{ const it=idxItems.find(z=>z.infohash===x.dataset.ih); if(it) addWatch(it.magnet, it.name); });
  toast("已收藏 "+ihs.length+" 个");
};
$("#idxSelAll").onclick=()=>{ document.querySelectorAll("#idxResults input.sel").forEach(x=>x.checked=true); updateSelN(); };

// ---- 实时 DHT ----
async function loadSearch(){
  const q=$("#sTerm").value.trim();
  if(!q){toast("请输入关键词");return;}
  const box=$("#sResults"); box.innerHTML='<div class="muted">搜索中…</div>';
  pushHistory(q);
  const d=await jget("/api/search?q="+encodeURIComponent(q));
  if(!d.ok){box.innerHTML='<div class="muted">❌ '+(d.error||"搜索失败")+'</div>';return;}
  const arr=d.items||[];
  if(!arr.length){box.innerHTML='<div class="muted">未搜到结果（DHT 可能暂无该资源）。</div>';return;}
  box.innerHTML="";
  arr.forEach(it=>box.insertAdjacentHTML("beforeend", cardHtml(it,{watch:true})));
  bindCardActions(box);
  toast("实时 DHT 命中 "+arr.length+" 条");
}
function pushHistory(q){
  let h=JSON.parse(localStorage.getItem("bm_history")||"[]");
  h=h.filter(x=>x!==q); h.unshift(q); h=h.slice(0,10);
  localStorage.setItem("bm_history",JSON.stringify(h)); renderHistory();
}
function renderHistory(){
  const h=JSON.parse(localStorage.getItem("bm_history")||"[]");
  const box=$("#sHistory");
  if(!h.length){box.innerHTML="";return;}
  box.innerHTML="";
  h.forEach(q=>{ const c=document.createElement("span"); c.className="chip"; c.textContent=q;
    c.onclick=()=>{$("#sTerm").value=q;loadSearch();}; box.appendChild(c); });
}
$("#sTerm").addEventListener("keydown",e=>{ if(e.key==="Enter") loadSearch(); });

// ---- 下载队列 ----
async function loadQueue(){
  const box=$("#queueResults"); box.innerHTML='<div class="muted">加载中…</div>';
  const all=$("#qAll").checked?"1":"0";
  const d=await jget("/api/queue?all="+all);
  if(!d.ok){box.innerHTML='<div class="muted">❌ '+(d.error||"读取失败")+'</div>';return;}
  const arr=d.items||[];
  $("#qCount").textContent="共 "+arr.length+" 个任务";
  if(!arr.length){box.innerHTML='<div class="muted">下载队列为空。</div>';return;}
  box.innerHTML="";
  arr.forEach(t=>{
    const r=document.createElement("div"); r.className="result";
    const size=t.size?fmtSize(t.size):"—";
    const prog=t.progress||0;
    const cls=prog>=100?"b-up":(t.state==="error"||t.state==="missingFiles"?"b-down":"b-warn");
    const stLabel=QSTATE[t.state]||t.state;
    const cat=t.category?(' · 分类 '+escHtml(t.category)):'';
    const dl=t.dlspeed?fmtSize(t.dlspeed)+"/s":"";
    const up=t.upspeed?fmtSize(t.upspeed)+"/s":"";
    r.innerHTML='<div class="t">'+escHtml(t.name)+'</div>'+
      '<div class="m">状态 <span class="badge '+cls+'">'+stLabel+'</span> · 大小 '+size+cat+(dl?' · ↓'+dl:'')+(up?' · ↑'+up:'')+'</div>'+
      '<div style="background:var(--panel2);border:1px solid var(--line);border-radius:8px;height:8px;overflow:hidden;margin-top:6px">'+
        '<div style="width:'+prog+'%;height:100%;background:var(--acc)"></div></div>'+
      '<div class="muted" style="font-size:12px;margin-top:4px">进度 '+prog+'% · 做种 '+(t.seeders||0)+' · 下载 '+(t.leechers||0)+'</div>'+
      '<div class="acts"><input type="checkbox" class="qsel" data-qh="'+escAttr(t.hash)+'" data-cat="'+escAttr(t.category||'')+'"> '+
        '<button data-act="pause" data-h="'+escAttr(t.hash)+'">暂停</button>'+
        '<button data-act="resume" data-h="'+escAttr(t.hash)+'">继续</button>'+
        '<button data-act="recheck" data-h="'+escAttr(t.hash)+'">校验</button>'+
        '<button data-act="delete" data-h="'+escAttr(t.hash)+'">删除</button>'+
        '<select data-act="setCategory" data-h="'+escAttr(t.hash)+'">'+
          '<option value="">改分类…</option><option value="bitmagnet">bitmagnet</option>'+
          '<option value="radarr">radarr</option><option value="sonarr">sonarr</option>'+
          '<option value="dht">dht</option><option value="">无</option></select>'+
      '</div>';
    box.appendChild(r);
  });
  bindQueueActions(box);
}
const QSTATE={downloading:"下载中",stalledDL:"下载停滞",queuedDL:"排队中",pausedDL:"已暂停",
  uploading:"做种中",stalledUP:"做种停滞",queuedUP:"排队做种",pausedUP:"暂停做种",
  checkingDL:"校验中",checkingUP:"校验中",error:"错误",missingFiles:"文件缺失",metaDL:"元数据"};
function bindQueueActions(box){
  box.querySelectorAll("button[data-act]").forEach(b=>{
    b.onclick=()=>{ const act=b.dataset.act, h=b.dataset.h;
      if(act==="delete"){
        const row=b.closest(".result"); const cat=row?row.getAttribute("data-cat"):"";
        if(cat && cat!=="bitmagnet"){ toast("🚫 仅能删除本台(bitmagnet)任务，media 栈种子请在 media 面板管理"); return; }
        if(!confirm("确认从 qBittorrent 删除该任务？")) return;
      }
      qbitAction(act,h).then(d=>{ toast(d.ok?"已"+act:"失败："+(d.error||"")); if(d.ok) loadQueue(); });
    };
  });
  box.querySelectorAll("select[data-act='setCategory']").forEach(s=>{
    s.onchange=()=>{ const h=s.dataset.h, cat=s.value; if(!cat) return;
      qbitAction("setCategory",h,cat).then(d=>{ toast(d.ok?"已改分类":"失败："+(d.error||"")); s.value=""; if(d.ok) loadQueue(); });
    };
  });
}
function qbitAction(act,h,cat){
  return jpost("/api/queue/action",{action:act,hashes:h,category:cat});
}
function updateQSelN(){
  const n=document.querySelectorAll("#queueResults input.qsel:checked").length;
  $("#qSelN").textContent=n;
  $("#qToolbar").style.display=n>0?"flex":"none";
}
$("#queueResults").addEventListener("change",e=>{ if(e.target.classList.contains("qsel")) updateQSelN(); });
$("#qSelAll").onclick=()=>{ document.querySelectorAll("#queueResults input.qsel").forEach(x=>x.checked=true); updateQSelN(); };
async function qBatch(act,cat){
  const sel=[...document.querySelectorAll("#queueResults input.qsel:checked")];
  let hs=sel.map(x=>x.dataset.qh);
  if(!hs.length){toast("未选择任务");return;}
  if(act==="delete"){
    const blocked=sel.filter(x=>(x.dataset.cat||"")!=="bitmagnet");
    if(blocked.length) toast("🚫 已跳过 "+blocked.length+" 个非 bitmagnet 任务（media 栈种子受保护）");
    hs=sel.filter(x=>(x.dataset.cat||"")==="bitmagnet").map(x=>x.dataset.qh);
    if(!hs.length){ toast("没有可删除的本台任务"); return; }
    if(!confirm("确认删除选中的 "+hs.length+" 个 bitmagnet 任务？")) return;
  }
  let ok=0,fail=0;
  for(const h of hs){ const d=await qbitAction(act,h,cat); if(d.ok)ok++;else fail++; }
  toast("批量"+act+"：成功 "+ok+" / 失败 "+fail);
  document.querySelectorAll("#queueResults input.qsel").forEach(x=>x.checked=false); updateQSelN();
  loadQueue(); refreshDownloaded();
}
$("#qBatchPause").onclick=()=>qBatch("pause");
$("#qBatchResume").onclick=()=>qBatch("resume");
$("#qBatchDel").onclick=()=>qBatch("delete");
$("#idxRefreshTr").onclick=async()=>{ const b=$("#idxRefreshTr"); b.disabled=true; const old=b.textContent; b.textContent="刷新中…";
  try{ const d=await jget("/api/trackers/refresh");
    if(d.ok) toast("✅ tracker 已刷新，共 "+d.count+" 个"+(d.sample&&d.sample[0]?"（例："+d.sample[0]+"）":""));
    else toast("❌ "+(d.error||"失败"));
  }catch(e){ toast("❌ "+e); } b.disabled=false; b.textContent=old; };
$("#qBatchCat").onchange=()=>{ const cat=$("#qBatchCat").value; if(!cat) return; qBatch("setCategory",cat); $("#qBatchCat").value=""; };
async function pasteDownload(){
  const txt=$("#pasteArea").value.trim();
  if(!txt){toast("请粘贴 magnet 或 infohash");return;}
  const toks=txt.split(/[\s,;]+/).filter(Boolean);
  let ok=0,fail=0;
  for(const tk of toks){ const r=await download(tk); if(r.ok)ok++;else fail++; }
  toast("提交 "+toks.length+" 个：成功 "+ok+" / 失败 "+fail);
  $("#pasteArea").value=""; refreshDownloaded();
}

// ---- 收藏 ----
function getWatch(){ try{return JSON.parse(localStorage.getItem("bm_watch")||"[]");}catch(e){return [];} }
function addWatch(mag,title){
  if(!mag) return;
  let w=getWatch();
  if(w.some(x=>x.magnet===mag)){ toast("已在收藏"); return; }
  w.unshift({magnet:mag, title:title||"", added:Date.now()});
  localStorage.setItem("bm_watch",JSON.stringify(w)); renderWatch(); toast("已收藏");
}
function renderWatch(){
  const w=getWatch(); $("#watchCount").textContent="共 "+w.length+" 个";
  const box=$("#watchResults");
  if(!w.length){box.innerHTML='<div class="muted">收藏夹为空。在已索引/搜索结果点「收藏」即可加入。</div>';return;}
  box.innerHTML="";
  w.forEach(it=>box.insertAdjacentHTML("beforeend", cardHtml({title:it.title,magnet:it.magnet,infohash:_ih(it.magnet)},{checkbox:false})));
  // 收藏卡无下载按钮时仍给下载
  bindWatchActions(box);
}
function _ih(mag){ const m=/urn:btih:([0-9a-fA-F]{40})/.exec(mag||""); return m?m[1].toLowerCase():""; }
function bindWatchActions(box){
  box.querySelectorAll(".result").forEach(r=>{
    const mag=r.querySelector("a.btn.ghost");
    const m=mag?mag.getAttribute("href"):"";
    if(m){
      const acts=r.querySelector(".acts");
      const b=document.createElement("button"); b.className="primary"; b.textContent="直接下载";
      b.onclick=()=>download(m).then(d=>{ if(d.ok){b.textContent="已提交 ✓";} });
      acts.insertBefore(b, acts.firstChild);
    }
  });
  bindCardActions(box);
  box.querySelectorAll("button[data-watch]").forEach(()=>{});
  // 收藏项加移除
  const items=[...box.querySelectorAll(".result")];
  getWatch().forEach((it,idx)=>{
    const r=items[idx]; if(!r) return;
    const rm=document.createElement("button"); rm.className="ghost"; rm.textContent="移除";
    rm.onclick=()=>{ let w=getWatch(); w.splice(idx,1); localStorage.setItem("bm_watch",JSON.stringify(w)); renderWatch(); };
    r.querySelector(".acts").appendChild(rm);
  });
}
function clearWatch(){ if(!confirm("确认清空收藏？")) return; localStorage.removeItem("bm_watch"); renderWatch(); }

// ---- 系统状态 ----
async function loadSystem(){
  const g=$("#sysGrid"); g.innerHTML='<div class="muted">加载中…</div>';
  const d=await jget("/api/system");
  if(d.error){g.innerHTML='<div class="muted">'+d.error+'</div>';return;}
  g.innerHTML="";
  d.systems.forEach(s=>{
    const card=document.createElement("div"); card.className="card";
    const badge=s.ok?'<span class="badge b-up">在线</span>':(s.tcp?'<span class="badge b-warn">在网但无响应</span>':'<span class="badge b-down">离线</span>');
    const parts=[];
    if(s.running)parts.push("容器 "+s.running);
    if(s.detail)parts.push(s.detail);
    const jump = s.url ? ' <a class="btn ghost" style="float:right;padding:2px 10px;font-size:12px" href="'+escAttr(s.url)+'" target="_blank" rel="noopener noreferrer">打开 ↗</a>' : '';
    card.innerHTML='<h3>'+s.label+' '+badge+jump+'</h3><div class="muted" style="font-size:12px">'+parts.filter(Boolean).join(" · ")+'</div>';
    g.appendChild(card);
  });
  $("#stats").innerHTML=
    '<div class="box"><div class="n">'+(d.bm_total==null?'<span class="muted">N/A</span>':d.bm_total.toLocaleString())+'</div><div class="l">已索引种子</div></div>'+
    '<div class="box"><div class="n">'+(d.bm_reachable?'<span style="color:var(--green)">在线</span>':'<span style="color:var(--red)">离线</span>')+'</div><div class="l">BitMagnet 可达</div></div>'+
    '<div class="box"><div class="n">'+d.manager_uptime+'</div><div class="l">本服务运行时长</div></div>';
}
async function loadEgress(){
  const el=$("#egressBody"); el.innerHTML='<span class="muted">测试中…</span>';
  const d=await jget("/api/egress");
  if(d.error){el.innerHTML='<span class="badge b-down">异常</span> '+d.error;return;}
  el.innerHTML = (d.ok?'<span class="badge b-up">出网正常</span>':'<span class="badge b-down">出网异常</span>')
    + (d.node?(' · 当前节点 <b>'+escHtml(d.node)+'</b>'):'')
    + (d.latency!=null?' · 延迟 '+d.latency+'ms':'') + (d.error&&!d.ok?' · '+escHtml(d.error):'');
}
async function loadTunnel(){
  const el=$("#tunnelBody"); el.innerHTML='<span class="muted">检测中…</span>';
  const d=await jget("/api/tunnel");
  el.innerHTML = (d.reachable?'<span class="badge b-up">3333 可达</span>':'<span class="badge b-down">3333 不可达</span>')
    + ' · BitMagnet 容器 <b>'+escHtml(d.bitmagnet_state||'?')+'</b>'
    + (d.bitmagnet_health?(' · '+escHtml(d.bitmagnet_health)):'');
}
async function restartTunnel(){
  if(!confirm("确认重启 BitMagnet 容器以重挂隧道网络？（索引/下载会短暂中断）")) return;
  $("#restartBtn").disabled=true; $("#restartBtn").textContent="重启中…";
  const d=await jpost("/api/tunnel/restart",{});
  $("#restartBtn").disabled=false; $("#restartBtn").textContent="🔄 重启 BitMagnet";
  toast(d.ok?"已重启":"重启失败: "+(d.error||""));
  setTimeout(loadTunnel,4000);
}
async function loadStats(){
  const el=$("#statsBody"); el.innerHTML='<span class="muted">加载中…</span>';
  const d=await jget("/api/stats");
  if(d.error){el.innerHTML='<span class="muted">'+d.error+'</span>';return;}
  let html='<div>近 24 小时新增：<b>'+(d.recent24h==null?'N/A':d.recent24h.toLocaleString())+'</b> 条</div>';
  if(d.by_type && d.by_type.length){
    html+='<div style="margin-top:6px">按类型分布：</div>';
    d.by_type.slice(0,12).forEach(x=>{ html+='<div class="dist"><b>'+escHtml(CT[x.type]||x.type)+'</b>：'+x.count.toLocaleString()+'</div>'; });
  }
  el.innerHTML=html;
}
async function maintain(action){
  const btn=action==="reclassify"?$("#reclassifyBtn"):$("#reprocessBtn");
  btn.disabled=true;
  const d=await jpost("/api/maintain",{action:action});
  btn.disabled=false;
  if(!d.ok){toast("启动失败："+(d.error||""));return;}
  toast((action==="reclassify"?"重分类":"重处理")+"已启动，后台运行中");
  pollMaintain();
}
async function pollMaintain(){
  for(let i=0;i<30;i++){
    const d=await jget("/api/maintain");
    if(d.running){ $("#maintainHint").textContent="运行中…（"+Math.round((Date.now()-d.started*1000)/1000)+"s）"; }
    else if(d.done){ $("#maintainHint").textContent="上次任务："+(d.ok?"成功":"失败")+(d.log?" · "+d.log.slice(0,80):""); return; }
    else { $("#maintainHint").textContent=""; return; }
    await new Promise(r=>setTimeout(r,3000));
  }
}

function toggleTheme(){
  const cur=document.documentElement.getAttribute("data-theme")==="light"?"dark":"light";
  document.documentElement.setAttribute("data-theme",cur);
  localStorage.setItem("bm_theme",cur);
  $("#themeBtn").textContent = cur==="light"?"🌙 暗色":"☀️ 亮色";
}
(function(){ const t=localStorage.getItem("bm_theme")||"dark";
  document.documentElement.setAttribute("data-theme",t);
  const b=$("#themeBtn"); if(b) b.textContent=t==="light"?"🌙 暗色":"☀️ 亮色"; })();

let activePanel="indexed";
document.querySelectorAll(".tab").forEach(t=>t.onclick=()=>{
  document.querySelectorAll(".tab").forEach(x=>x.classList.remove("active"));
  document.querySelectorAll(".panel").forEach(x=>x.classList.remove("active"));
  t.classList.add("active");
  $("#p-"+t.dataset.p).classList.add("active");
  const p=t.dataset.p; activePanel=p;
  if(p==="status"){loadSystem();loadEgress();loadTunnel();loadStats();pollMaintain();}
  else if(p==="indexed"){loadIndexed(false);}
  else if(p==="queue"){loadQueue();}
  else if(p==="watch"){renderWatch();}
  else if(p==="search"){renderHistory();}
});

// 队列 Tab：每 5s 自动刷新（用户有选中时不打断）；状态 Tab：每 15s 拉一次
setInterval(()=>{ if(activePanel==="queue" && $("#qToolbar").style.display==="none") loadQueue(); }, 5000);
setInterval(()=>{ if(activePanel==="status"){ loadSystem(); loadEgress(); loadTunnel(); loadStats(); } }, 15000);

function tick(){$("#clock").textContent=new Date().toLocaleString("zh-CN");}
setInterval(tick,1000);tick();
// 初始化：拉去重集合 + 默认加载已索引
refreshDownloaded();
loadIndexed(false);
renderHistory();
</script>
</body>
</html>"""


class H(BaseHTTPRequestHandler):
    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj, ensure_ascii=False))

    def do_GET(self):
        if self.path == "/" or self.path == "":
            self._send(200, PAGE, "text/html; charset=utf-8")
            return
        if self.path.startswith("/api/system"):
            self._json(system_status())
            return
        if self.path.startswith("/api/indexed"):
            q = urllib.parse.urlparse(self.path).query
            params = urllib.parse.parse_qs(q)
            sort = params.get("sort", ["updated"])[0]
            if sort not in ("updated", "size", "name", "seeders"):
                sort = "updated"
            order = params.get("order", ["desc"])[0]
            if order not in ("asc", "desc"):
                order = "desc"
            ctype = params.get("ctype", [""])[0] or None
            min_size = params.get("min_size", [""])[0]
            max_size = params.get("max_size", [""])[0]
            min_seeders = params.get("min_seeders", [""])[0]
            self._json(bm_indexed(
                params.get("limit", ["50"])[0], params.get("offset", ["0"])[0],
                params.get("q", [""])[0] or None, ctype, sort, order,
                int(min_size) if min_size.isdigit() else None,
                int(max_size) if max_size.isdigit() else None,
                int(min_seeders) if min_seeders.isdigit() else None))
            return
        if self.path.startswith("/api/trackers/refresh"):
            self._json(refresh_trackers())
            return
        if self.path.startswith("/api/search"):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(bm_search(params.get("q", [""])[0]))
            return
        if self.path.startswith("/api/queue/hashes"):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(qbit_downloaded(show_all=(params.get("all", ["0"])[0] == "1")))
            return
        if self.path.startswith("/api/queue"):
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            self._json(qbit_queue(show_all=(params.get("all", ["0"])[0] == "1")))
            return
        if self.path.startswith("/api/egress"):
            self._json(egress_probe())
            return
        if self.path.startswith("/api/tunnel"):
            self._json(tunnel_status())
            return
        if self.path.startswith("/api/stats"):
            self._json(bm_stats())
            return
        if self.path.startswith("/api/maintain"):
            self._json(maintain_status())
            return
        if self.path.startswith("/api/config"):
            self._json(load_config())
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            payload = json.loads(raw or b"{}")
        except Exception:
            payload = {}
        if self.path == "/api/download":
            magnet = (payload.get("magnet") or "").strip()
            if not magnet:
                self._json({"ok": False, "error": "缺少 magnet"}, 400)
                return
            ok, msg = qbit_add_magnet(magnet)
            self._json({"ok": ok, "error": None if ok else msg, "message": msg},
                       200 if ok else 400)
            return
        if self.path == "/api/queue/action":
            action = payload.get("action")
            hashes = payload.get("hashes")
            category = payload.get("category")
            if not action or not hashes:
                self._json({"ok": False, "error": "缺少 action / hashes"}, 400)
                return
            d = qbit_action(action, hashes, category)
            self._json(d, 200 if d.get("ok") else 400)
            return
        if self.path == "/api/egress/fix":
            self._json(fix_egress())
            return
        if self.path.startswith("/api/tunnel/restart"):
            self._json(tunnel_restart())
            return
        if self.path == "/api/maintain":
            self._json(maintain_start(payload.get("action")))
            return
        if self.path == "/api/config":
            self._json(save_config(payload))
            return
        self._json({"error": "not found"}, 404)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    print("[bitmagnet-bot] listening on :%d  (BitMagnet=%s, qBittorrent=%s, Clash=%s)"
          % (PORT, BITMAGNET_HOST, QBIT_URL, CLASH_API))
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
