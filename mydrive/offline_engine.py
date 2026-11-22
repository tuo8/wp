"""离线下载引擎：aria2 子进程 + 任务状态机 + 完成后自动入库。

对齐线上 ``PubOfflineTask`` 语义：
  * ``UserTask`` 字段编号 / 含义与 proto 一致
  * ``task_identity = <infohash>_otp_btv1``
  * status 分桶：0-99 等待、100-199 下载中、200-999 重试失败、1000 完成、<0 错误
  * progress 为 0-100 百分比
  * 同一 infohash 重复添加直接秒传（status=1000）
"""
from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import re
import shutil
import socket
import sqlite3
import subprocess
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlsplit
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ---------- 状态常量（与 Web 端 offline.status 文案映射一致） ----------
ST_WAITING = 50          # 0-99 等待下载
ST_DOWNLOADING = 100     # 100-199 下载中（实际值 = 100 + progress//2）
ST_RETRY = 200           # 200-999 重试失败
ST_DONE = 1000           # 下载完成
ST_ERROR = -1            # 错误

TYPE_BT = 5050
TYPE_HTTP = 5000

MAX_TORRENT_METADATA_BYTES = 16 * 1024 * 1024


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _validate_public_endpoint(url: str, allowed_schemes=("http", "https", "udp")):
    """Reject non-public destinations before an offline task can reach the network."""
    parsed = urlsplit(url)
    if parsed.scheme.lower() not in allowed_schemes or not parsed.hostname:
        raise ValueError("Only public HTTP(S) URLs and public torrent trackers are allowed")
    if parsed.username or parsed.password:
        raise ValueError("URLs containing embedded credentials are not allowed")
    host = parsed.hostname.rstrip(".").lower()
    if host in ("localhost",) or host.endswith((".localhost", ".local", ".internal", ".home", ".lan")):
        raise ValueError("Local network destinations are not allowed")
    try:
        addresses = [ipaddress.ip_address(host)]
    except ValueError:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if parsed.scheme.lower() == "https" else 80),
                                       type=socket.SOCK_STREAM)
            addresses = list({ipaddress.ip_address(info[4][0].split("%", 1)[0]) for info in infos})
        except (OSError, ValueError):
            raise ValueError("Unable to resolve download host")
    if not addresses or any(not address.is_global for address in addresses):
        raise ValueError("Private, loopback, link-local, or reserved network destinations are not allowed")
    return parsed


def _validate_download_url(url: str):
    value = (url or "").strip()
    if value.lower().startswith("magnet:"):
        params = parse_qs(urlsplit(value).query)
        for tracker in params.get("tr", []):
            _validate_public_endpoint(tracker, allowed_schemes=("http", "https", "udp"))
        for source_key in ("ws", "as", "xs"):
            for source_url in params.get(source_key, []):
                _validate_public_endpoint(source_url, allowed_schemes=("http", "https"))
        return
    _validate_public_endpoint(value, allowed_schemes=("http", "https"))


def _safe_urlopen(request, timeout: float):
    """Open only a prevalidated public URL, without environment proxies or redirects."""
    target = request.full_url if isinstance(request, urllib.request.Request) else str(request)
    _validate_public_endpoint(target, allowed_schemes=("http", "https"))
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirectHandler())
    return opener.open(request, timeout=timeout)


B32 = "abcdefghijklmnopqrstuvwxyz234567"

# 公共 tracker（magnet 解析阶段用来快速拿到元数据）
TRACKERS = ",".join([
    "udp://tracker.opentrackr.org:1337/announce",
    "udp://open.demonii.com:1337/announce",
    "udp://tracker.torrent.eu.org:451/announce",
    "udp://exodus.desync.com:6969/announce",
    "udp://tracker.openbittorrent.com:6969/announce",
    "http://tracker.openbittorrent.com:80/announce",
])


# ---------------------------------------------------------------- bencode
def bdecode(data: bytes):
    """极简 bencode 解码（torrent 只用 dict/list/int/bytes）。"""
    pos = 0

    def parse():
        nonlocal pos
        c = data[pos:pos + 1]
        if c == b"i":
            end = data.index(b"e", pos)
            val = int(data[pos + 1:end])
            pos = end + 1
            return val
        if c == b"l":
            pos += 1
            out = []
            while data[pos:pos + 1] != b"e":
                out.append(parse())
            pos += 1
            return out
        if c == b"d":
            pos += 1
            out = {}
            while data[pos:pos + 1] != b"e":
                key = parse()
                out[key] = parse()
            pos += 1
            return out
        colon = data.index(b":", pos)
        length = int(data[pos:colon])
        start = colon + 1
        val = data[start:start + length]
        pos = start + length
        return val

    return parse()


def _txt(v) -> str:
    if isinstance(v, bytes):
        try:
            return v.decode("utf-8")
        except UnicodeDecodeError:
            return v.decode("utf-8", "replace")
    return str(v)


def infohash_from_magnet(url: str) -> str:
    """magnet -> 40 位小写 hex infohash。"""
    m = re.search(r"btih:([0-9a-zA-Z]{32,40})", url)
    if not m:
        return ""
    raw = m.group(1)
    if len(raw) == 40:
        return raw.lower()
    # base32 -> hex
    raw = raw.upper()
    bits = 0
    val = 0
    out = bytearray()
    for ch in raw:
        val = (val << 5) | B32.index(ch.lower())
        bits += 5
        if bits >= 8:
            bits -= 8
            out.append((val >> bits) & 0xFF)
    return bytes(out).hex()


def info_hash_of_torrent(raw: bytes) -> str:
    """从 .torrent 原始字节里切出 info 字典并 sha1 -> infohash。"""
    marker = raw.find(b"4:infod")
    if marker < 0:
        return ""
    start = marker + len(b"4:info")
    if raw[start:start + 1] != b"d":
        return ""
    depth = 0
    i = start
    in_str = False
    while i < len(raw):
        c = raw[i:i + 1]
        if in_str:
            if c == b":":
                length = int(raw[start:i]) if raw[start:i].isdigit() else 0
                i += length
                in_str = False
                continue
            if c.isdigit():
                i += 1
                continue
            in_str = False
            continue
        if c == b"d" or c == b"l":
            depth += 1
        elif c == b"e":
            depth -= 1
            if depth == 0:
                return hashlib.sha1(raw[start:i + 1]).hexdigest()
        elif c.isdigit():
            in_str = True
        i += 1
    return ""


def magnet_display_name(url: str) -> str:
    for part in url.split("&"):
        if part.startswith("dn="):
            from urllib.parse import unquote_plus
            return unquote_plus(part[3:])
    return ""


def torrent_files(torrent_path: str) -> Tuple[dict, List[dict]]:
    """解析 .torrent -> (meta, files)。files 元素含 path/size/directory。"""
    raw = Path(torrent_path).read_bytes()
    meta = bdecode(raw)
    info = meta.get(b"info") or {}
    name = _txt(info.get(b"name") or b"")
    out: List[dict] = []
    if b"files" in info:
        for item in info[b"files"]:
            parts = [_txt(p) for p in item.get(b"path") or []]
            if not parts:
                continue
            out.append({
                "path": "/" + name + "/" + "/".join(parts),
                "name": parts[-1],
                "size": int(item.get(b"length") or 0),
                "directory": False,
            })
    else:
        out.append({
            "path": "/" + name,
            "name": name,
            "size": int(info.get(b"length") or 0),
            "directory": False,
        })
    total = sum(f["size"] for f in out)
    return {"name": name, "size": total, "files": out}, out


# ---------------------------------------------------------------- aria2
class Aria2Error(RuntimeError):
    pass


class Aria2:
    """aria2 JSON-RPC 客户端 + 子进程生命周期。"""

    def __init__(self, exe: str, port: int = 6800, secret: str = "mydrive",
                 download_root: str = "", meta_dir: str = "", dht_port: int = 6881):
        self.exe = exe
        self.port = port
        self.secret = secret
        self.download_root = download_root
        self.meta_dir = meta_dir or download_root
        self.dht_port = dht_port
        self.proc: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()

    # -- rpc --
    def _rpc(self, method: str, params: Optional[list] = None, timeout: float = 15.0):
        payload = json.dumps({
            "jsonrpc": "2.0", "id": "mydrive",
            "method": method,
            "params": [f"token:{self.secret}"] + (params or []),
        }).encode()
        req = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/jsonrpc", data=payload,
            headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.URLError as exc:
            raise Aria2Error(f"aria2 unreachable: {exc}") from exc
        if "error" in data:
            raise Aria2Error(data["error"].get("message", "rpc error"))
        return data.get("result")

    def alive(self) -> bool:
        try:
            self._rpc("aria2.getVersion", timeout=2)
            return True
        except Exception:
            return False

    def start(self, wait: float = 12.0) -> bool:
        if self.alive():
            return True
        exe = self.exe
        if not os.path.exists(exe):
            return False
        os.makedirs(self.download_root, exist_ok=True)
        os.makedirs(self.meta_dir, exist_ok=True)
        args = [
            exe,
            "--enable-rpc=true",
            "--rpc-listen-all=false",
            f"--rpc-listen-port={self.port}",
            f"--rpc-secret={self.secret}",
            f"--dir={self.download_root}",
            "--continue=true",
            "--enable-dht=true",
            "--dht-listen-port=%d" % self.dht_port,
            "--listen-port=%d" % (self.dht_port + 1),
            "--dht-entry-point=dht.transmissionbt.com:6881",
            "--dht-entry-point=dht.libtorrent.org:25401",
            "--bt-tracker=" + TRACKERS,
            "--bt-enable-lpd=true",
            "--bt-save-metadata=true",
            "--bt-remove-unselected-file=false",
            "--seed-time=0",
            "--file-allocation=none",
            "--max-concurrent-downloads=5",
            "--max-connection-per-server=8",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--no-conf=true",
        ]
        creation = 0x08000000  # CREATE_NO_WINDOW
        try:
            self.proc = subprocess.Popen(
                args, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=creation if os.name == "nt" else 0)
        except OSError:
            return False
        deadline = time.time() + wait
        while time.time() < deadline:
            if self.alive():
                return True
            time.sleep(0.25)
        return False

    def stop(self):
        try:
            self._rpc("aria2.shutdown", timeout=3)
        except Exception:
            pass
        if self.proc:
            try:
                self.proc.terminate()
            except Exception:
                pass

    # -- 下载操作 --
    def add_uri(self, url: str, options: Optional[dict] = None) -> str:
        return self._rpc("aria2.addUri", [[url], options or {}])

    def add_torrent(self, data: bytes, options: Optional[dict] = None,
                    trackers: Optional[List[str]] = None) -> str:
        import base64 as _b64
        return self._rpc("aria2.addTorrent",
                         [_b64.b64encode(data).decode(), trackers or [], options or {}])

    def tell(self, gid: str) -> dict:
        keys = ["gid", "status", "totalLength", "completedLength", "downloadSpeed",
                "files", "errorCode", "errorMessage", "bittorrent", "dir", "infoHash"]
        try:
            return self._rpc("aria2.tellStatus", [gid, keys])
        except Aria2Error:
            return {}

    def remove(self, gid: str, force: bool = True):
        for m in ("aria2.forceRemove", "aria2.remove"):
            try:
                self._rpc(m, [gid], timeout=5)
                return
            except Aria2Error:
                continue

    def pause(self, gid: str) -> bool:
        try:
            self._rpc("aria2.pause", [gid], timeout=5)
            return True
        except Aria2Error:
            return False

    def unpause(self, gid: str) -> bool:
        try:
            self._rpc("aria2.unpause", [gid], timeout=5)
            return True
        except Aria2Error:
            return False

    def set_global_speed_limit(self, max_download: str = "0", max_upload: str = "0") -> bool:
        try:
            self._rpc("aria2.changeGlobalOption", [{
                "max-overall-download-limit": str(max_download or "0"),
                "max-overall-upload-limit": str(max_upload or "0"),
            }], timeout=5)
            return True
        except Aria2Error:
            return False

    def find_by_infohash(self, info_hash: str) -> Optional[str]:
        """Return the GID aria2 already holds for this BT infohash, if any."""
        if not info_hash:
            return None
        wanted = info_hash.lower()
        for method, params in (("aria2.tellActive", []),
                               ("aria2.tellWaiting", [0, 200]),
                               ("aria2.tellStopped", [0, 200])):
            try:
                items = self._rpc(method, params, timeout=6) or []
            except Aria2Error:
                continue
            for item in items:
                if (item.get("infoHash") or "").lower() == wanted:
                    return item.get("gid")
        return None

    def drop_infohash(self, info_hash: str) -> bool:
        """Evict a stale task for this infohash so it can be queued again.

        aria2 refuses a second ``addTorrent`` for the same infohash
        (``InfoHash ... is already registered``), while the live service simply
        treats a repeat Add as an instant transfer. Dropping the stale entry
        keeps our behaviour deterministic: re-add, then re-download or short-circuit.
        """
        if not self.find_by_infohash(info_hash):
            return False
        for _ in range(25):
            gid = self.find_by_infohash(info_hash)
            if not gid:
                return True
            self.remove(gid)
            try:
                self._rpc("aria2.removeDownloadResult", [gid], timeout=5)
            except Aria2Error:
                pass
            time.sleep(0.1)
        return not self.find_by_infohash(info_hash)

    def global_stat(self) -> dict:
        try:
            return self._rpc("aria2.getGlobalStat", timeout=4)
        except Aria2Error:
            return {}

    def stopped(self) -> List[dict]:
        keys = ["gid", "status", "totalLength", "completedLength", "files",
                "errorCode", "errorMessage", "infoHash", "bittorrent", "dir"]
        try:
            return self._rpc("aria2.tellStopped", [0, 200, keys], timeout=8)
        except Aria2Error:
            return []


# ---------------------------------------------------------------- 引擎
class OfflineEngine:
    """任务持久化 + 状态同步 + 完成后入库。"""

    def __init__(self, storage, root: str, aria2: Aria2, identity_factory=None):
        self.storage = storage
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.aria2 = aria2
        self.download_root = Path(aria2.download_root)
        self.meta_dir = Path(aria2.meta_dir)
        self._identity = identity_factory or self._default_identity
        self._lock = threading.RLock()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.db = self.root / "offline.db"
        self._init_db()

    # -- infra --
    @staticmethod
    def _default_identity() -> str:
        return os.urandom(12).hex()

    def _conn(self):
        conn = sqlite3.connect(self.db, timeout=15)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
            CREATE TABLE IF NOT EXISTS offline_tasks (
                identity TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                type INTEGER DEFAULT 5050,
                status INTEGER DEFAULT 50,
                update_ts INTEGER DEFAULT 0,
                file TEXT DEFAULT '',
                create_ts INTEGER DEFAULT 0,
                url TEXT DEFAULT '',
                size INTEGER DEFAULT 0,
                name TEXT DEFAULT '',
                task_identity TEXT DEFAULT '',
                code INTEGER DEFAULT 0,
                message TEXT DEFAULT '',
                addon TEXT DEFAULT '',
                progress INTEGER DEFAULT 0,
                bytes_total INTEGER DEFAULT 0,
                bytes_processed INTEGER DEFAULT 0,
                flag INTEGER DEFAULT 0,
                save_path TEXT DEFAULT '/',
                callbacks TEXT DEFAULT '[]',
                ignore_files TEXT DEFAULT '[]',
                save_identity TEXT DEFAULT '',
                gid TEXT DEFAULT '',
                files_json TEXT DEFAULT '[]',
                stored INTEGER DEFAULT 0
            )""")
            c.execute("CREATE INDEX IF NOT EXISTS idx_offline_user ON offline_tasks(user_id, create_ts DESC)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_offline_tid ON offline_tasks(task_identity)")
            c.execute("""
            CREATE TABLE IF NOT EXISTS offline_meta (
                infohash TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                size INTEGER DEFAULT 0,
                files_json TEXT DEFAULT '[]',
                update_ts INTEGER DEFAULT 0
            )""")

    def start(self):
        if not self.aria2.alive():
            self.aria2.start()
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="offline-sync", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    # -- 工具 --
    @staticmethod
    def _now() -> int:
        return int(time.time() * 1000)

    def _meta_cache_get(self, infohash: str) -> Optional[dict]:
        with self._conn() as c:
            row = c.execute("SELECT * FROM offline_meta WHERE infohash=?", (infohash,)).fetchone()
        if not row:
            return None
        return {"name": row["name"], "size": row["size"],
                "files": json.loads(row["files_json"] or "[]")}

    def _meta_cache_put(self, infohash: str, meta: dict):
        with self._conn() as c:
            c.execute("INSERT OR REPLACE INTO offline_meta VALUES (?,?,?,?,?)",
                      (infohash, meta.get("name", ""), meta.get("size", 0),
                       json.dumps(meta.get("files", []), ensure_ascii=False), self._now()))

    def _torrent_path(self, infohash: str) -> Path:
        return self.meta_dir / f"{infohash}.torrent"

    def _find_torrent(self, infohash: str) -> Optional[Path]:
        for cand in (self._torrent_path(infohash),
                     self.meta_dir / f"{infohash.upper()}.torrent"):
            if cand.exists():
                return cand
        if infohash:
            for p in self.meta_dir.glob("*.torrent"):
                if infohash.lower() in p.name.lower():
                    return p
        return None

    def _resolve_dir(self, path: str, user_id: str) -> Tuple[str, str]:
        """虚拟路径 -> (父目录 identity, 规范化路径)。"""
        path = (path or "/").strip() or "/"
        if path == "/":
            return "root", "/"
        parts = [p for p in path.split("/") if p]
        parent = "root"
        for part in parts:
            found = None
            for meta in self.storage.list_files(parent=parent, user_id=user_id):
                if meta.name == part and meta.is_dir:
                    found = meta
                    break
            if not found:
                found = self.storage.create_dir(part, parent=parent, user_id=user_id)
            parent = found.identity
        return parent, path

    def _dir_for_task(self, task: dict) -> Tuple[str, str]:
        """确保任务保存目录存在：<save_path>/<task name>。"""
        user_id = task["user_id"]
        base_parent, base_path = self._resolve_dir(task.get("save_path") or "/", user_id)
        name = (task.get("name") or task.get("task_identity") or "offline").strip() or "offline"
        name = re.sub(r"[\\/:*?\"<>|]", "_", name)[:120]
        want_path = (base_path.rstrip("/") + "/" + name) if base_path != "/" else "/" + name
        for meta in self.storage.list_files(parent=base_parent, user_id=user_id):
            if meta.is_dir and meta.name == name:
                return meta.identity, want_path
        meta = self.storage.create_dir(name, parent=base_parent, user_id=user_id)
        return meta.identity, want_path

    # -- Parse --
    def parse(self, url: str, save_path: str = "/", user_id: str = "anonymous",
              timeout: float = 22.0) -> dict:
        """解析任务元信息；magnet 走 aria2 抓 .torrent，本地已缓存则秒回。"""
        url = (url or "").strip()
        if not url:
            return {"meta": {}, "task_files": []}
        _validate_download_url(url)
        if url.startswith("magnet:"):
            infohash = infohash_from_magnet(url)
            meta = self._meta_cache_get(infohash) if infohash else None
            if not meta:
                meta = self._fetch_metadata(url, infohash, timeout)
            if not meta:
                return {"meta": {}, "task_files": []}
            name = meta.get("name") or magnet_display_name(url) or infohash[:16]
            tident = f"{infohash}_otp_btv1" if infohash else name
            now = self._now()
            task_meta = {
                "identity": tident, "type": TYPE_BT, "status": ST_WAITING,
                "update_ts": now, "create_ts": now, "url": url,
                "size": meta.get("size", 0), "name": name,
                "task_identity": tident, "progress": 0,
                "bytes_total": meta.get("size", 0), "bytes_processed": 0,
                "save_path": save_path or "/",
            }
            files = []
            for idx, item in enumerate(meta.get("files", [])):
                files.append({
                    "identity": f"{tident}:{hashlib.md5(item['path'].encode()).hexdigest()}",
                    "path": item["path"], "status": ST_WAITING,
                    "update_ts": now, "create_ts": now,
                    "name": item["name"], "size": item["size"],
                    "bytes_total": item["size"], "index": idx,
                    "directory": bool(item.get("directory")),
                })
            return {"meta": task_meta, "task_files": files}

        if url.lower().split("?")[0].endswith(".torrent"):
            meta = self._fetch_torrent_url(url)
            if meta:
                tident = f"{hashlib.sha1(url.encode()).hexdigest()[:40]}_otp_btv1"
                now = self._now()
                files = []
                for idx, item in enumerate(meta.get("files", [])):
                    files.append({
                        "identity": f"{tident}:{hashlib.md5(item['path'].encode()).hexdigest()}",
                        "path": item["path"], "status": ST_WAITING,
                        "update_ts": now, "create_ts": now,
                        "name": item["name"], "size": item["size"],
                        "bytes_total": item["size"], "index": idx,
                        "directory": bool(item.get("directory")),
                    })
                return {"meta": {"identity": tident, "type": TYPE_BT, "status": ST_WAITING,
                                 "update_ts": now, "create_ts": now, "url": url,
                                 "size": meta.get("size", 0), "name": meta.get("name", ""),
                                 "task_identity": tident, "progress": 0,
                                 "bytes_total": meta.get("size", 0), "bytes_processed": 0,
                                 "save_path": save_path or "/"},
                        "task_files": files}

        # HTTP(S) 直链：HEAD 取大小与文件名
        name = self._name_from_url(url)
        size = self._remote_size(url)
        now = self._now()
        return {
            "meta": {"identity": self._identity(), "type": TYPE_HTTP, "status": ST_WAITING,
                     "update_ts": now, "create_ts": now, "url": url,
                     "size": size, "name": name, "progress": 0,
                     "bytes_total": size, "bytes_processed": 0,
                     "save_path": save_path or "/"},
            "task_files": [{"identity": f"{self._identity()}:0", "path": "/" + name,
                            "status": ST_WAITING, "update_ts": now, "create_ts": now,
                            "name": name, "size": size, "bytes_total": size,
                            "index": 0, "directory": False}],
        }

    def _fetch_metadata(self, url: str, infohash: str, timeout: float) -> Optional[dict]:
        """用 aria2 以 metadata-only 模式抓 .torrent，成功则缓存并返回。"""
        _validate_download_url(url)
        if not self.aria2.alive() and not self.aria2.start(wait=6):
            return None
        cached = self._find_torrent(infohash) if infohash else None
        gid = ""
        if not cached:
            try:
                gid = self.aria2.add_uri(url, {
                    "bt-metadata-only": "true",
                    "bt-save-metadata": "true",
                    "dir": str(self.meta_dir),
                    "follow-torrent": "false",
                    "pause-metadata": "false",
                    "bt-tracker-connect-timeout": "5",
                    "bt-tracker-timeout": "5",
                    "max-redirect": "0",
                })
            except Aria2Error:
                return None
        deadline = time.time() + timeout
        while time.time() < deadline:
            cached = self._find_torrent(infohash) if infohash else None
            if cached:
                break
            if gid:
                st = self.aria2.tell(gid)
                if st.get("status") in ("complete",):
                    break
            time.sleep(1.0)
        if gid:
            self.aria2.remove(gid)
        cached = self._find_torrent(infohash)
        if not cached:
            return None
        try:
            meta, _ = torrent_files(str(cached))
        except Exception:
            return None
        if not meta.get("name"):
            return None
        self._meta_cache_put(infohash or cached.stem.lower(), meta)
        return meta

    def _fetch_torrent_url(self, url: str, want_path: bool = False):
        """下载远端 .torrent 并解析；拒绝私网目标、重定向和超大元数据。"""
        _validate_download_url(url)
        key = hashlib.sha1(url.encode()).hexdigest()[:40]
        path = self.meta_dir / f"{key}.torrent"
        try:
            if not path.exists():
                with _safe_urlopen(url, timeout=20) as resp:
                    data = resp.read(MAX_TORRENT_METADATA_BYTES + 1)
                if len(data) > MAX_TORRENT_METADATA_BYTES or not data.startswith(b"d"):
                    return None
                path.write_bytes(data)
            if want_path:
                return str(path)
            meta, _ = torrent_files(str(path))
            return meta if meta.get("name") else None
        except Exception:
            return None

    @staticmethod
    def _name_from_url(url: str) -> str:
        from urllib.parse import urlparse, unquote
        path = unquote(urlparse(url).path or "")
        return os.path.basename(path) or "offline_task"

    @staticmethod
    def _remote_size(url: str) -> int:
        try:
            _validate_download_url(url)
            req = urllib.request.Request(url, method="HEAD")
            with _safe_urlopen(req, timeout=8) as resp:
                return int(resp.headers.get("content-length") or 0)
        except Exception:
            return 0

    # -- Add --
    def add(self, req: dict, user_id: str) -> dict:
        url = (req or {}).get("url") or ""
        if not url:
            raise ValueError("url required")
        _validate_download_url(url)
        save_path = req.get("save_path") or "/"
        name = req.get("name") or ""
        ignore = req.get("ignore_files") or []
        is_magnet = url.startswith("magnet:")
        is_torrent_url = url.lower().split("?")[0].endswith(".torrent")
        infohash = infohash_from_magnet(url) if is_magnet else ""
        torrent_path = None
        if is_torrent_url:
            torrent_path = self._fetch_torrent_url(url, want_path=True)
            if torrent_path:
                try:
                    infohash = info_hash_of_torrent(Path(torrent_path).read_bytes()) or infohash
                except Exception:
                    pass
        tident = f"{infohash}_otp_btv1" if infohash else ""

        url_key = tident or ("url_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:24])
        # 全局跨用户磁力/URL秒存（PikPak 核心机制）：只要任意用户已离线入库，零流量零额外磁盘秒存
        if url_key and not req.get("flag"):
            with self._conn() as c:
                row = c.execute(
                    "SELECT * FROM offline_tasks WHERE (task_identity=? OR task_identity=?) AND stored=1 "
                    "ORDER BY update_ts DESC LIMIT 1", (tident or url_key, url_key)).fetchone()
            if row:
                cloned_path = self._clone_instant_files(row, save_path, user_id)
                task = self._row_to_task(row)
                task.update({
                    "identity": self._identity(),
                    "status": ST_DONE,
                    "progress": 100,
                    "create_ts": self._now(),
                    "update_ts": self._now(),
                    "save_path": save_path,
                    "file": cloned_path or task.get("file", ""),
                    "instant_saved": True,
                })
                self._insert(task, user_id, "")
                with self._conn() as c:
                    c.execute("UPDATE offline_tasks SET stored=1, files_json=? WHERE identity=?",
                              (row["files_json"] or "[]", task["identity"]))
                return task

        meta = self._meta_cache_get(infohash) if infohash else None
        if not meta and torrent_path:
            try:
                meta, _ = torrent_files(str(torrent_path))
            except Exception:
                meta = None
        if not meta and url.startswith("magnet:"):
            meta = self._fetch_metadata(url, infohash, 18.0)
        if not name:
            name = (meta or {}).get("name") or magnet_display_name(url) or self._name_from_url(url)
        if torrent_path and name.endswith(".torrent"):
            name = name[:-8]

        now = self._now()
        task = {
            "identity": self._identity(), "type": TYPE_BT if (is_magnet or is_torrent_url) else TYPE_HTTP,
            "status": ST_WAITING, "update_ts": now, "create_ts": now,
            "url": url, "size": (meta or {}).get("size", 0), "name": name,
            "task_identity": tident or name, "code": 0, "message": "",
            "addon": req.get("addon") or "", "progress": 0,
            "bytes_total": (meta or {}).get("size", 0), "bytes_processed": 0,
            "flag": int(req.get("flag") or 0), "save_path": save_path,
            "callbacks": req.get("callbacks") or [], "ignore_files": ignore,
            "save_identity": req.get("save_identity") or "",
        }

        gid = ""
        if self.aria2.alive() or self.aria2.start(wait=8):
            opts = {"dir": str(self.download_root), "bt-save-metadata": "true",
                    "follow-torrent": "true", "seed-time": "0",
                    "allow-overwrite": "true", "auto-file-renaming": "false",
                    "continue": "true", "max-redirect": "0"}
            selected_files = req.get("selected_files") or []
            if infohash:
                opts["bt-remove-unselected-file"] = "false"
                if selected_files:
                    opts["select-file"] = ",".join(str(int(i)) for i in selected_files)
                # 线上把重复 Add 当秒传；aria2 则会拒绝同一 infohash，先清残留
                self.aria2.drop_infohash(infohash)
            else:
                opts["out"] = name
            try:
                if torrent_path:
                    gid = self.aria2.add_torrent(Path(torrent_path).read_bytes(), opts,
                                                 TRACKERS.split(","))
                else:
                    gid = self.aria2.add_uri(url, opts)
            except Aria2Error as exc:
                if "already registered" in str(exc).lower() and infohash:
                    self.aria2.drop_infohash(infohash)
                    try:
                        if torrent_path:
                            gid = self.aria2.add_torrent(Path(torrent_path).read_bytes(), opts,
                                                         TRACKERS.split(","))
                        else:
                            gid = self.aria2.add_uri(url, opts)
                    except Aria2Error as exc2:
                        task.update({"status": ST_ERROR, "code": 1, "message": str(exc2)})
                else:
                    task.update({"status": ST_ERROR, "code": 1, "message": str(exc)})
        else:
            task.update({"status": ST_ERROR, "code": 2, "message": "aria2 not available"})

        self._insert(task, user_id, gid)
        return task

    def _insert(self, task: dict, user_id: str, gid: str):
        with self._lock, self._conn() as c:
            c.execute("""INSERT OR REPLACE INTO offline_tasks
                (identity,user_id,type,status,update_ts,file,create_ts,url,size,name,
                 task_identity,code,message,addon,progress,bytes_total,bytes_processed,
                 flag,save_path,callbacks,ignore_files,save_identity,gid,files_json,stored)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (
                task["identity"], user_id, task.get("type", TYPE_BT), task.get("status", ST_WAITING),
                task.get("update_ts", 0), task.get("file", ""), task.get("create_ts", 0),
                task.get("url", ""), task.get("size", 0), task.get("name", ""),
                task.get("task_identity", ""), task.get("code", 0), task.get("message", ""),
                task.get("addon", ""), task.get("progress", 0),
                task.get("bytes_total", 0), task.get("bytes_processed", 0),
                task.get("flag", 0), task.get("save_path", "/"),
                json.dumps(task.get("callbacks") or []), json.dumps(task.get("ignore_files") or []),
                task.get("save_identity", ""), gid, json.dumps([]), 0))

    def _row_to_task(self, row) -> dict:
        return {
            "identity": row["identity"], "type": row["type"], "status": row["status"],
            "update_ts": row["update_ts"], "file": row["file"], "create_ts": row["create_ts"],
            "url": row["url"], "size": row["size"], "name": row["name"],
            "task_identity": row["task_identity"], "code": row["code"], "message": row["message"],
            "addon": row["addon"], "progress": row["progress"],
            "bytes_total": row["bytes_total"], "bytes_processed": row["bytes_processed"],
            "flag": row["flag"], "save_path": row["save_path"],
            "callbacks": json.loads(row["callbacks"] or "[]"),
            "ignore_files": json.loads(row["ignore_files"] or "[]"),
            "save_identity": row["save_identity"],
        }

    # -- List / Delete --
    def list(self, user_id: str, limit: int = 100, token: str = "") -> List[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM offline_tasks WHERE user_id=? ORDER BY create_ts DESC LIMIT ?",
                (user_id, max(1, limit))).fetchall()
        return [self._row_to_task(r) for r in rows]

    def delete(self, identities: List[str], user_id: str, delete_files: bool = False) -> int:
        if not identities:
            return 0
        want = set(identities)
        removed = 0
        with self._lock, self._conn() as c:
            rows = c.execute(
                f"SELECT * FROM offline_tasks WHERE user_id=? AND (identity IN ({','.join('?' * len(want))}) "
                f"OR task_identity IN ({','.join('?' * len(want))}))",
                [user_id] + list(want) + list(want)).fetchall()
            for row in rows:
                if row["gid"]:
                    self.aria2.remove(row["gid"])
                if delete_files:
                    self._purge_files(row, user_id)
                c.execute("DELETE FROM offline_tasks WHERE identity=?", (row["identity"],))
                removed += 1
        return removed

    def _purge_files(self, row, user_id: str):
        """删除任务在网盘里落下的文件/目录。"""
        try:
            parent, _ = self._resolve_dir(row["save_path"] or "/", user_id)
            name = (row["name"] or "").strip()
            if not name:
                return
            for meta in self.storage.list_files(parent=parent, user_id=user_id):
                if meta.name == name:
                    self.storage.delete_file(meta.identity, permanent=True, user_id=user_id)
        except Exception:
            pass

    # -- 状态同步 --
    def _loop(self):
        while not self._stop.is_set():
            try:
                self.sync()
            except Exception:
                pass
            self._stop.wait(2.0)

    def sync(self):
        if not self.aria2.alive():
            self.aria2.start(wait=4)
            return
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM offline_tasks WHERE gid<>'' AND status<1000 AND status>=0").fetchall()
        for row in rows:
            st = self.aria2.tell(row["gid"])
            if not st:
                continue
            self._apply(row, st)

    def _apply(self, row, st: dict):
        status = st.get("status")
        total = int(st.get("totalLength") or 0)
        done = int(st.get("completedLength") or 0)
        progress = int(done * 100 / total) if total else 0
        new_status = row["status"]
        code = row["code"]
        message = row["message"]
        name = row["name"]

        bt = st.get("bittorrent") or {}
        if bt.get("info") and bt["info"].get("name"):
            name = bt["info"]["name"]

        if status == "active":
            new_status = ST_DOWNLOADING + min(99, progress // 2)
        elif status in ("waiting", "paused"):
            new_status = ST_WAITING
        elif status == "complete":
            new_status = ST_DONE
            progress = 100
        elif status in ("error", "removed"):
            err = str(st.get("errorCode") or "")
            # 404/超时等归为可重试；元数据失败归为错误
            new_status = ST_RETRY if err in ("1", "2", "3", "19", "22") else ST_ERROR
            code = int(st.get("errorCode") or 0)
            message = (st.get("errorMessage") or "")[:200]

        with self._lock, self._conn() as c:
            total_final = total or row["bytes_total"]
            size_final = max(int(row["size"] or 0), total_final)
            c.execute("""UPDATE offline_tasks SET status=?, progress=?, bytes_total=?,
                         bytes_processed=?, update_ts=?, name=?, code=?, message=?, size=?
                         WHERE identity=?""",
                      (new_status, progress, total_final, done,
                       self._now(), name, code, message, size_final, row["identity"]))
        if new_status == ST_DONE and not row["stored"]:
            self._ingest(row["identity"])

    # -- 入库 --
    def _ingest(self, identity: str):
        with self._conn() as c:
            row = c.execute("SELECT * FROM offline_tasks WHERE identity=?", (identity,)).fetchone()
        if not row or row["stored"]:
            return
        user_id = row["user_id"]
        gid = row["gid"]
        st = self.aria2.tell(gid) if gid else {}
        files = st.get("files") or []
        # aria2 的 files.path 是绝对磁盘路径
        abs_paths = [f.get("path") for f in files if f.get("path")]
        if not abs_paths:
            base = self.download_root / (row["name"] or "")
            if base.exists():
                abs_paths = [str(p) for p in base.rglob("*") if p.is_file()]
        if not abs_paths:
            with self._conn() as c:
                c.execute("UPDATE offline_tasks SET stored=1, message=? WHERE identity=?",
                          ("no local files to ingest", identity))
            return

        # 单文件任务：直接落在 save_path 下；多文件任务：建同名目录
        single = len(abs_paths) == 1 and (
            row["type"] == TYPE_HTTP
            or Path(abs_paths[0]).name == (row["name"] or "").strip())
        if single:
            base_parent, base_path = self._resolve_dir(row["save_path"] or "/", user_id)
            dir_identity, dir_path = base_parent, base_path
        else:
            dir_identity, dir_path = self._dir_for_task(dict(row))
        count = 0
        ingested_ids = []
        for path in abs_paths:
            p = Path(path)
            if not p.exists() or not p.is_file():
                continue
            rel = p.name
            try:
                rel = str(p.relative_to(self.download_root / (row["name"] or "")))
            except ValueError:
                rel = p.name
            parent = dir_identity
            if "/" in rel or "\\" in rel:
                parts = [x for x in re.split(r"[\\/]", rel) if x][:-1]
                for part in parts:
                    found = None
                    for meta in self.storage.list_files(parent=parent, user_id=user_id):
                        if meta.is_dir and meta.name == part:
                            found = meta
                            break
                    parent = found.identity if found else \
                        self.storage.create_dir(part, parent=parent, user_id=user_id).identity
            try:
                # 离线资源按线上对象存储的私有格式落盘（整块 XOR 0x75）
                m = self.storage.chunk_file(str(p), parent=parent, name=p.name,
                                            user_id=user_id, chunk_format=1)
                ingested_ids.append({"identity": m.identity, "name": m.name, "rel": rel})
                count += 1
            except Exception:
                continue
        stored_path = dir_path
        if single:
            stored_path = (dir_path.rstrip("/") + "/" + Path(abs_paths[0]).name) if dir_path != "/" \
                else "/" + Path(abs_paths[0]).name
        with self._conn() as c:
            c.execute("UPDATE offline_tasks SET stored=1, file=?, message=?, files_json=? WHERE identity=?",
                      (stored_path, "" if count else "no files ingested",
                       json.dumps(ingested_ids, ensure_ascii=False), identity))
        # 入库完成后自动清理离线临时下载目录，避免本地磁盘双倍占用
        if count > 0 and os.environ.get("OFFLINE_AUTO_CLEANUP", "1").lower() not in ("0", "false", "no"):
            for path in abs_paths:
                try:
                    p = Path(path)
                    if p.exists() and p.is_file():
                        p.unlink(missing_ok=True)
                except OSError:
                    pass
            try:
                base_dir = self.download_root / (row["name"] or "")
                if base_dir.exists() and base_dir.is_dir():
                    shutil.rmtree(base_dir, ignore_errors=True)
            except OSError:
                pass


    def _clone_instant_files(self, src_row, save_path: str, target_user_id: str) -> str:
        """Zero-copy clone of previously ingested offline files into target_user_id's drive."""
        try:
            items = json.loads(src_row["files_json"] or "[]")
        except Exception:
            items = []
        if not items:
            return src_row["file"] or ""
        parent_id, parent_path = self._resolve_dir(save_path or "/", target_user_id)
        cloned_first = ""
        for item in items:
            fid = item.get("identity") if isinstance(item, dict) else None
            if not fid:
                continue
            src_meta = self.storage.get_file_meta(fid)
            if not src_meta or src_meta.is_dir:
                continue
            cloned = self.storage.clone_file(src_meta, name=src_meta.name, parent=parent_id, user_id=target_user_id)
            if not cloned_first:
                cloned_first = cloned.path
        return cloned_first or parent_path

    def register_instant_cache(self, url: str, file_identity: str, user_id: str, name: str = "") -> dict:
        """Seed global instant-save cache for an infohash/URL pointing to an existing stored file."""
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta:
            raise ValueError("Source file not found")
        infohash = infohash_from_magnet(url) if url.startswith("magnet:") else ""
        tident = f"{infohash}_otp_btv1" if infohash else ("url_" + hashlib.sha1(url.encode("utf-8")).hexdigest()[:24])
        now = self._now()
        task = {
            "identity": self._identity(),
            "type": TYPE_BT if infohash else TYPE_HTTP,
            "status": ST_DONE,
            "update_ts": now,
            "create_ts": now,
            "url": url,
            "size": meta.size,
            "name": name or meta.name,
            "task_identity": tident,
            "code": 0,
            "message": "",
            "progress": 100,
            "bytes_total": meta.size,
            "bytes_processed": meta.size,
            "save_path": "/",
            "file": meta.path,
        }
        self._insert(task, user_id, "")
        files_json = json.dumps([{"identity": meta.identity, "name": meta.name, "rel": meta.name}], ensure_ascii=False)
        with self._conn() as c:
            c.execute("UPDATE offline_tasks SET stored=1, files_json=? WHERE identity=?", (files_json, task["identity"]))
        return task

    def pause(self, task_id: str, user_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT gid FROM offline_tasks WHERE identity=? AND user_id=?", (task_id, user_id)).fetchone()
        if not row:
            return False
        if row["gid"]:
            self.aria2.pause(row["gid"])
        with self._conn() as c:
            c.execute("UPDATE offline_tasks SET status=?, message='paused', update_ts=? WHERE identity=?",
                      (ST_WAITING, self._now(), task_id))
        return True

    def resume(self, task_id: str, user_id: str) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT gid FROM offline_tasks WHERE identity=? AND user_id=?", (task_id, user_id)).fetchone()
        if not row:
            return False
        if row["gid"]:
            self.aria2.unpause(row["gid"])
        with self._conn() as c:
            c.execute("UPDATE offline_tasks SET message='', update_ts=? WHERE identity=?", (self._now(), task_id))
        return True

    def set_speed_limit(self, max_download: str = "0", max_upload: str = "0") -> dict:
        ok = self.aria2.set_global_speed_limit(max_download, max_upload)
        return {"applied": ok, "max_download": max_download, "max_upload": max_upload}

    def add_batch(self, urls: List[str], save_path: str, user_id: str) -> List[dict]:
        results = []
        for u in urls:
            u = (u or "").strip()
            if not u:
                continue
            try:
                t = self.add({"url": u, "save_path": save_path}, user_id)
                results.append({"ok": True, "task": t})
            except Exception as e:
                results.append({"ok": False, "url": u, "error": str(e)})
        return results

    # -- 统计 --
    def stats(self, user_id: str) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM offline_tasks WHERE user_id=?",
                (user_id,)).fetchone()
            today = self._today_start()
            trow = c.execute(
                "SELECT COUNT(*) n, COALESCE(SUM(size),0) b FROM offline_tasks "
                "WHERE user_id=? AND create_ts>=?", (user_id, today)).fetchone()
        return {"daily_tasks_quota": 1024, "tasks_commited_today": trow["n"],
                "daily_bytes_quota": 10995116277760, "bytes_commited_today": trow["b"],
                "total_tasks": row["n"], "total_bytes": row["b"]}

    @staticmethod
    def _today_start() -> int:
        lt = time.localtime()
        start = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, 0, 0, 0, 0, 0, -1))
        return int(start * 1000)
