"""
Tiered Large-File Storage & Space-Saving Engine
================================================
Solves limited local disk capacity for large files (matching how HalalCloud /
PikPak handle massive libraries on bounded servers):

1. Hot / Cold Tiering:
   - Hot tier: local disk (`chunks/`) bounded by `local_cache_max_bytes`
   - Cold tier: pluggable backend:
       * `s3`  — zero-dependency AWS SigV4 client for MinIO / Cloudflare R2 /
                 Backblaze B2 / Aliyun OSS / AWS S3
       * `dir` — mounted external disk / NAS / Rclone / WebDAV mount directory
       * `none`— local-only mode
   - When local cache exceeds `local_cache_max_bytes`, least-recently-used
     chunks are automatically offloaded to the cold tier and evicted locally.
   - On read, cold-only chunks are transparently hydrated back to local hot
     cache on demand (streaming-friendly, 4 MiB chunk granularity so even a
     50 GB video only pulls the chunks being played!).

2. Transparent Smart Block Compression (optional):
   - Compresses compressible blocks with zlib (level 6) and stores a 4-byte
     magic header `MZLB`.
   - Automatically skips already-compressed data (MP4/MKV/ZIP/JPG) if the
     compressed output is >= 92% of original size.

3. Reference-Counted Garbage Collection & Trash Auto-Expiry:
   - Reclaims unreferenced chunks from both local hot cache and cold tier.
"""

from __future__ import annotations

import datetime
import hashlib
import hmac
import json
import os
import sqlite3
from db_adapter import connect_db
import threading
import time
import urllib.parse
import urllib.request
import zlib
from pathlib import Path
from typing import Dict, List, Optional, Tuple

COMP_MAGIC = b"MZLB"


def smart_compress(data: bytes) -> Tuple[bytes, bool]:
    """Compress block if it saves at least 8% space; otherwise keep raw."""
    if not data or len(data) < 256:
        return data, False
    comp = zlib.compress(data, level=6)
    if len(comp) + len(COMP_MAGIC) < int(len(data) * 0.92):
        return COMP_MAGIC + comp, True
    return data, False


def smart_decompress(data: bytes) -> bytes:
    """Decompress block if it carries our compression header."""
    if data and data.startswith(COMP_MAGIC):
        try:
            return zlib.decompress(data[len(COMP_MAGIC):])
        except zlib.error:
            return data
    return data


# ==================== COLD TIER BACKENDS ====================

class ColdBackend:
    """Abstract interface for cold object storage backends."""

    kind: str = "none"

    def put_chunk(self, key: str, data: bytes) -> bool:
        raise NotImplementedError

    def get_chunk(self, key: str) -> Optional[bytes]:
        raise NotImplementedError

    def delete_chunk(self, key: str) -> bool:
        raise NotImplementedError

    def exists(self, key: str) -> bool:
        raise NotImplementedError

    def health_check(self) -> dict:
        return {"ok": True, "kind": self.kind}


class DirectoryColdBackend(ColdBackend):
    """Cold storage backed by a secondary directory / mounted NAS / Rclone mount."""

    kind = "dir"

    def __init__(self, root_dir: str):
        self.root = Path(root_dir)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        safe = key.lstrip("/")
        return self.root / safe

    def put_chunk(self, key: str, data: bytes) -> bool:
        p = self._path(key)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(data)
        tmp.replace(p)
        return True

    def get_chunk(self, key: str) -> Optional[bytes]:
        p = self._path(key)
        if p.exists():
            return p.read_bytes()
        return None

    def delete_chunk(self, key: str) -> bool:
        p = self._path(key)
        if p.exists():
            p.unlink()
            return True
        return False

    def exists(self, key: str) -> bool:
        return self._path(key).exists()

    def stats(self) -> dict:
        total_files = 0
        total_bytes = 0
        if self.root.exists():
            for r, _, files in os.walk(str(self.root)):
                for fn in files:
                    if fn.endswith(".tmp"):
                        continue
                    total_files += 1
                    total_bytes += os.path.getsize(os.path.join(r, fn))
        return {"kind": self.kind, "root": str(self.root), "objects": total_files, "bytes": total_bytes}


class S3CompatBackend(ColdBackend):
    """Zero-dependency S3-compatible client using AWS Signature Version 4.

    Compatible with MinIO, Cloudflare R2, Backblaze B2, AWS S3, Aliyun OSS.
    """

    kind = "s3"

    def __init__(
        self,
        endpoint: str,
        bucket: str,
        access_key: str,
        secret_key: str,
        region: str = "auto",
        prefix: str = "mydrive-chunks",
        timeout: float = 30.0,
    ):
        self.endpoint = endpoint.rstrip("/")
        self.bucket = bucket.strip("/")
        self.access_key = access_key
        self.secret_key = secret_key
        self.region = region or "us-east-1"
        self.prefix = prefix.strip("/")
        self.timeout = timeout

    def _object_url(self, key: str) -> Tuple[str, str, str]:
        full_key = f"{self.prefix}/{key.lstrip('/')}" if self.prefix else key.lstrip("/")
        encoded_key = urllib.parse.quote(full_key, safe="/-_.~")
        parsed = urllib.parse.urlparse(self.endpoint)
        host = parsed.netloc
        canonical_uri = f"/{self.bucket}/{encoded_key}"
        url = f"{parsed.scheme}://{host}{canonical_uri}"
        return url, host, canonical_uri

    def _sign_headers(self, method: str, host: str, canonical_uri: str, payload: bytes = b"") -> Dict[str, str]:
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        payload_hash = hashlib.sha256(payload).hexdigest()

        canonical_headers = (
            f"host:{host}\n"
            f"x-amz-content-sha256:{payload_hash}\n"
            f"x-amz-date:{amz_date}\n"
        )
        signed_headers = "host;x-amz-content-sha256;x-amz-date"
        canonical_request = "\n".join([
            method,
            canonical_uri,
            "",
            canonical_headers,
            signed_headers,
            payload_hash,
        ])
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        def _sign(key_bytes: bytes, msg: str) -> bytes:
            return hmac.new(key_bytes, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = _sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region = _sign(k_date, self.region)
        k_service = _sign(k_region, "s3")
        k_signing = _sign(k_service, "aws4_request")
        signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()

        auth_header = (
            f"AWS4-HMAC-SHA256 Credential={self.access_key}/{credential_scope}, "
            f"SignedHeaders={signed_headers}, Signature={signature}"
        )
        return {
            "Host": host,
            "x-amz-date": amz_date,
            "x-amz-content-sha256": payload_hash,
            "Authorization": auth_header,
        }

    def _request(self, method: str, key: str, payload: bytes = b"") -> Tuple[int, bytes]:
        url, host, canonical_uri = self._object_url(key)
        headers = self._sign_headers(method, host, canonical_uri, payload)
        if payload:
            headers["Content-Length"] = str(len(payload))
            headers["Content-Type"] = "application/octet-stream"
        req = urllib.request.Request(url, data=(payload if method == "PUT" else None), headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def put_chunk(self, key: str, data: bytes) -> bool:
        status, _ = self._request("PUT", key, data)
        return 200 <= status < 300

    def get_chunk(self, key: str) -> Optional[bytes]:
        status, body = self._request("GET", key)
        if 200 <= status < 300:
            return body
        return None

    def delete_chunk(self, key: str) -> bool:
        status, _ = self._request("DELETE", key)
        return status in (200, 204, 404)

    def exists(self, key: str) -> bool:
        status, _ = self._request("HEAD", key)
        return status == 200

    def presign_url(self, method: str, key: str, expires: int = 900) -> str:
        """Generate an AWS SigV4 query-string presigned URL for direct client PUT/GET."""
        expires = max(60, min(604800, int(expires)))
        url, host, canonical_uri = self._object_url(key)
        now = datetime.datetime.now(datetime.timezone.utc)
        amz_date = now.strftime("%Y%m%dT%H%M%SZ")
        date_stamp = now.strftime("%Y%m%d")
        credential_scope = f"{date_stamp}/{self.region}/s3/aws4_request"
        params = [
            ("X-Amz-Algorithm", "AWS4-HMAC-SHA256"),
            ("X-Amz-Credential", f"{self.access_key}/{credential_scope}"),
            ("X-Amz-Date", amz_date),
            ("X-Amz-Expires", str(expires)),
            ("X-Amz-SignedHeaders", "host"),
        ]
        canonical_qs = "&".join(
            f"{urllib.parse.quote(k, safe='')}={urllib.parse.quote(v, safe='')}"
            for k, v in sorted(params)
        )
        canonical_request = "\n".join([
            method.upper(),
            canonical_uri,
            canonical_qs,
            f"host:{host}\n",
            "host",
            "UNSIGNED-PAYLOAD",
        ])
        string_to_sign = "\n".join([
            "AWS4-HMAC-SHA256",
            amz_date,
            credential_scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        ])

        def _sign(key_bytes: bytes, msg: str) -> bytes:
            return hmac.new(key_bytes, msg.encode("utf-8"), hashlib.sha256).digest()

        k_date = _sign(("AWS4" + self.secret_key).encode("utf-8"), date_stamp)
        k_region = _sign(k_date, self.region)
        k_service = _sign(k_region, "s3")
        k_signing = _sign(k_service, "aws4_request")
        sig = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
        return f"{url}?{canonical_qs}&X-Amz-Signature={sig}"


# ==================== TIERED CHUNK MANAGER ====================

class TieredStorageManager:
    """Manages hot local cache + cold tier offloading + block compression + GC."""

    def __init__(self, storage_root: str, db_path: Path, chunks_dir: Path):
        self.root = Path(storage_root)
        self.db_path = db_path
        self.chunks_dir = chunks_dir
        self.cfg_path = self.root / "json" / "tiered_storage.json"
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.backend: Optional[ColdBackend] = None
        self.config = self._load_config()
        self._init_db()
        self._rebuild_backend()

    def _default_config(self) -> dict:
        return {
            "cold_type": os.environ.get("COLD_STORAGE_TYPE", "none").lower(),  # none | dir | s3
            "cold_dir": os.environ.get("COLD_STORAGE_DIR", ""),
            "s3_endpoint": os.environ.get("S3_ENDPOINT", ""),
            "s3_bucket": os.environ.get("S3_BUCKET", ""),
            "s3_access_key": os.environ.get("S3_ACCESS_KEY", ""),
            "s3_secret_key": os.environ.get("S3_SECRET_KEY", ""),
            "s3_region": os.environ.get("S3_REGION", "auto"),
            "s3_prefix": os.environ.get("S3_PREFIX", "mydrive-chunks"),
            "local_cache_max_bytes": int(os.environ.get("LOCAL_CACHE_MAX_BYTES", "0")),  # 0 = unlimited
            "compression_enabled": os.environ.get("CHUNK_COMPRESSION", "0").lower() in ("1", "true", "yes"),
            "auto_offload": os.environ.get("AUTO_OFFLOAD_COLD", "1").lower() in ("1", "true", "yes"),
            "trash_retention_days": int(os.environ.get("TRASH_RETENTION_DAYS", "30")),
        }

    def _load_config(self) -> dict:
        cfg = self._default_config()
        if self.cfg_path.exists():
            try:
                saved = json.loads(self.cfg_path.read_text(encoding="utf-8"))
                cfg.update(saved)
            except Exception:
                pass
        return cfg

    def save_config(self, updates: dict) -> dict:
        with self._lock:
            allowed = set(self._default_config().keys())
            for k, v in updates.items():
                if k in allowed:
                    if k in ("local_cache_max_bytes", "trash_retention_days"):
                        v = max(0, int(v))
                    elif k in ("compression_enabled", "auto_offload"):
                        v = bool(v)
                    self.config[k] = v
            self.cfg_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")
            self._rebuild_backend()
            return self.get_public_config()

    def get_public_config(self) -> dict:
        c = dict(self.config)
        if c.get("s3_secret_key"):
            c["s3_secret_key_set"] = True
            c["s3_secret_key"] = "***"
        else:
            c["s3_secret_key_set"] = False
        c["cold_active"] = self.backend is not None
        return c

    def _rebuild_backend(self):
        kind = (self.config.get("cold_type") or "none").lower()
        if kind == "dir" and self.config.get("cold_dir"):
            self.backend = DirectoryColdBackend(self.config["cold_dir"])
        elif kind == "s3" and self.config.get("s3_endpoint") and self.config.get("s3_bucket"):
            self.backend = S3CompatBackend(
                endpoint=self.config["s3_endpoint"],
                bucket=self.config["s3_bucket"],
                access_key=self.config.get("s3_access_key", ""),
                secret_key=self.config.get("s3_secret_key", ""),
                region=self.config.get("s3_region", "auto"),
                prefix=self.config.get("s3_prefix", "mydrive-chunks"),
            )
        else:
            self.backend = None

    def _init_db(self):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS chunk_tiers (
                cid TEXT PRIMARY KEY,
                raw_size INTEGER NOT NULL DEFAULT 0,
                stored_size INTEGER NOT NULL DEFAULT 0,
                compressed INTEGER NOT NULL DEFAULT 0,
                in_local INTEGER NOT NULL DEFAULT 1,
                in_cold INTEGER NOT NULL DEFAULT 0,
                last_access_ts INTEGER NOT NULL DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_chunk_tiers_lru ON chunk_tiers(in_local, last_access_ts)")
        tier_cols = {row[1] for row in c.execute("PRAGMA table_info(chunk_tiers)")}
        if "stored_sha256" not in tier_cols:
            c.execute("ALTER TABLE chunk_tiers ADD COLUMN stored_sha256 TEXT DEFAULT ''")
        conn.commit()
        conn.close()

    def _cold_key(self, cid: str) -> str:
        return f"{cid[:2]}/{cid[2:4]}/{cid}"

    def _local_path(self, cid: str) -> Path:
        return self.chunks_dir / cid[:2] / cid[2:4] / cid

    @property
    def compression_enabled(self) -> bool:
        return bool(self.config.get("compression_enabled", False))

    def record_chunk_stored(self, cid: str, raw_size: int, stored_size: int, compressed: bool,
                            stored_sha256: str = ""):
        now = int(time.time())
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO chunk_tiers (cid, raw_size, stored_size, compressed, in_local, in_cold, last_access_ts, stored_sha256)
            VALUES (?, ?, ?, ?, 1, 0, ?, ?)
            ON CONFLICT(cid) DO UPDATE SET
                raw_size=excluded.raw_size,
                stored_size=excluded.stored_size,
                compressed=excluded.compressed,
                in_local=1,
                last_access_ts=excluded.last_access_ts,
                stored_sha256=excluded.stored_sha256
            """,
            (cid, raw_size, stored_size, 1 if compressed else 0, now, stored_sha256),
        )
        conn.commit()
        conn.close()

        # If cold backend is configured and local cache cap is exceeded, offload LRU chunks
        if self.backend and self.config.get("auto_offload", True) and self.config.get("local_cache_max_bytes", 0) > 0:
            self.enforce_local_cache_limit()

    def touch(self, cid: str):
        now = int(time.time())
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE chunk_tiers SET last_access_ts = ? WHERE cid = ?", (now, cid))
        conn.commit()
        conn.close()

    def hydrate_from_cold(self, cid: str) -> Optional[bytes]:
        """Fetch and verify a cold-only chunk before atomically caching it locally."""
        if not self.backend:
            return None
        with self._lock:
            raw = self.backend.get_chunk(self._cold_key(cid))
            if raw is None:
                return None
            conn = connect_db(self.db_path)
            c = conn.cursor()
            c.execute("SELECT stored_size, stored_sha256 FROM chunk_tiers WHERE cid = ?", (cid,))
            row = c.fetchone()
            conn.close()
            if row:
                expected_size = int(row[0] or 0)
                expected_hash = str(row[1] or "")
                if expected_size and len(raw) != expected_size:
                    return None
                if expected_hash and not hmac.compare_digest(
                    hashlib.sha256(raw).hexdigest(), expected_hash
                ):
                    return None
            local = self._local_path(cid)
            local.parent.mkdir(parents=True, exist_ok=True)
            temp = local.with_name(local.name + ".hydrate.tmp")
            try:
                temp.write_bytes(raw)
                temp.replace(local)
            finally:
                if temp.exists():
                    try:
                        temp.unlink()
                    except OSError:
                        pass
            now = int(time.time())
            conn = connect_db(self.db_path)
            conn.execute(
                "UPDATE chunk_tiers SET in_local = 1, last_access_ts = ? WHERE cid = ?",
                (now, cid),
            )
            conn.commit()
            conn.close()
            if self.config.get("local_cache_max_bytes", 0) > 0:
                self.enforce_local_cache_limit(exclude_cid=cid)
            return raw

    def offload_chunk(self, cid: str, evict_local: bool = True) -> bool:
        """Verify the cold copy and commit its metadata before evicting the hot copy."""
        if not self.backend:
            return False
        with self._lock:
            local = self._local_path(cid)
            if not local.exists():
                return False
            data = local.read_bytes()
            checksum = hashlib.sha256(data).hexdigest()
            key = self._cold_key(cid)
            if not self.backend.put_chunk(key, data):
                return False
            remote = self.backend.get_chunk(key)
            if remote is None or len(remote) != len(data) or not hmac.compare_digest(
                hashlib.sha256(remote).hexdigest(), checksum
            ):
                return False

            now = int(time.time())
            conn = connect_db(self.db_path)
            c = conn.cursor()
            c.execute(
                """INSERT INTO chunk_tiers
                   (cid, raw_size, stored_size, compressed, in_local, in_cold, last_access_ts, stored_sha256)
                   VALUES (?, ?, ?, 0, 1, 1, ?, ?)
                   ON CONFLICT(cid) DO UPDATE SET
                       in_cold=1, in_local=1, stored_size=excluded.stored_size,
                       last_access_ts=excluded.last_access_ts, stored_sha256=excluded.stored_sha256""",
                (cid, len(data), len(data), now, checksum),
            )
            conn.commit()
            conn.close()

            if evict_local:
                try:
                    local.unlink()
                except OSError:
                    # Both copies remain; the DB already records the verified cold copy.
                    return True
                conn = connect_db(self.db_path)
                conn.execute("UPDATE chunk_tiers SET in_local = 0 WHERE cid = ?", (cid,))
                conn.commit()
                conn.close()
            return True

    def enforce_local_cache_limit(self, exclude_cid: str = "") -> dict:
        """Evict oldest local chunks to cold storage until local cache fits quota."""
        max_bytes = int(self.config.get("local_cache_max_bytes", 0))
        if max_bytes <= 0 or not self.backend:
            return {"offloaded": 0, "freed_bytes": 0, "local_bytes": self.local_cache_bytes()}

        with self._lock:
            local_bytes = self.local_cache_bytes()
            if local_bytes <= max_bytes:
                return {"offloaded": 0, "freed_bytes": 0, "local_bytes": local_bytes}

            conn = connect_db(self.db_path)
            c = conn.cursor()
            c.execute(
                "SELECT cid, stored_size FROM chunk_tiers "
                "WHERE in_local = 1 ORDER BY last_access_ts ASC"
            )
            candidates = c.fetchall()
            conn.close()

            offloaded = 0
            freed = 0
            for cid, stored_size in candidates:
                if local_bytes <= max_bytes:
                    break
                if cid == exclude_cid:
                    continue
                local = self._local_path(cid)
                size_on_disk = local.stat().st_size if local.exists() else int(stored_size or 0)
                if self.offload_chunk(cid, evict_local=True):
                    offloaded += 1
                    freed += size_on_disk
                    local_bytes = max(0, local_bytes - size_on_disk)
            return {"offloaded": offloaded, "freed_bytes": freed, "local_bytes": local_bytes}

    def offload_all_to_cold(self, evict_local: bool = True) -> dict:
        """Manually push all local chunks to cold storage to free local disk."""
        if not self.backend:
            return {"offloaded": 0, "freed_bytes": 0, "error": "Cold storage backend not configured"}
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT cid FROM chunk_tiers WHERE in_local = 1")
        cids = [r[0] for r in c.fetchall()]
        conn.close()

        # Also catch chunks written before chunk_tiers existed
        for r, _, files in os.walk(str(self.chunks_dir)):
            for fn in files:
                if fn not in cids:
                    cids.append(fn)

        offloaded = 0
        freed = 0
        for cid in cids:
            local = self._local_path(cid)
            sz = local.stat().st_size if local.exists() else 0
            if self.offload_chunk(cid, evict_local=evict_local):
                offloaded += 1
                if evict_local:
                    freed += sz
        return {"offloaded": offloaded, "freed_bytes": freed}

    def delete_chunk_everywhere(self, cid: str) -> int:
        """Delete a no-longer-referenced chunk from local and cold tiers."""
        with self._lock:
            freed = 0
            local = self._local_path(cid)
            if local.exists():
                freed += local.stat().st_size
                try:
                    local.unlink()
                except OSError:
                    return 0
            if self.backend:
                try:
                    self.backend.delete_chunk(self._cold_key(cid))
                except Exception:
                    # Keep tier metadata so a failed remote delete can be retried.
                    return freed
            conn = connect_db(self.db_path)
            c = conn.cursor()
            c.execute("DELETE FROM chunk_tiers WHERE cid = ?", (cid,))
            conn.commit()
            conn.close()
            return freed

    def local_cache_bytes(self) -> int:
        total = 0
        if self.chunks_dir.exists():
            for r, _, files in os.walk(str(self.chunks_dir)):
                for fn in files:
                    try:
                        total += os.path.getsize(os.path.join(r, fn))
                    except OSError:
                        pass
        return total

    def get_stats(self) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*), COALESCE(SUM(raw_size),0), COALESCE(SUM(stored_size),0), "
            "SUM(CASE WHEN in_local=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN in_cold=1 THEN 1 ELSE 0 END), "
            "SUM(CASE WHEN compressed=1 THEN 1 ELSE 0 END) "
            "FROM chunk_tiers"
        )
        row = c.fetchone() or (0, 0, 0, 0, 0, 0)
        conn.close()
        local_bytes = self.local_cache_bytes()
        raw_sum = int(row[1] or 0)
        stored_sum = int(row[2] or 0)
        return {
            "tracked_chunks": int(row[0] or 0),
            "local_chunks": int(row[3] or 0),
            "cold_chunks": int(row[4] or 0),
            "compressed_chunks": int(row[5] or 0),
            "local_cache_bytes": local_bytes,
            "local_cache_max_bytes": int(self.config.get("local_cache_max_bytes", 0)),
            "compression_saved_bytes": max(0, raw_sum - stored_sum),
            "cold_type": self.config.get("cold_type", "none"),
            "cold_active": self.backend is not None,
            "compression_enabled": self.compression_enabled,
            "trash_retention_days": int(self.config.get("trash_retention_days", 30)),
        }
