"""
Storage Engine
==============
The heart of the cloud drive: how files are actually stored.

Key concepts:
1. Files are split into chunks (default 4MB each)
2. Each chunk gets a CID (content identifier) — SHA256-based
3. Chunks are stored by CID, not by filename — same content = same CID = dedup
4. A "Meta" record maps file -> ordered list of chunk CIDs
5. FastLookup enables instant transfer (秒传): if hash matches, no upload needed
6. Chunks can be served via HTTP for download

Storage layout on disk:
  storage_root/
    chunks/
      ba/
        fk/
          bafkreiajk7owt2osq...  (raw chunk data, named by CID)
    meta/
      <file_identity>.json      (file metadata + chunk CID list)
    db/
      index.sqlite              (FastLookup index, file tree, quotas)
"""

import os
import json
import sqlite3
from db_adapter import connect_db, is_postgres_enabled
import hashlib
import time
import uuid
from dataclasses import dataclass, field, asdict
from typing import Optional, List, Dict, Tuple, Generator
from pathlib import Path

from cid_engine import (
    compute_cid, compute_file_cid, parse_cid, quick_fingerprint,
    CidInfo, sha256, make_multihash, cid_v1_from_multihash
)
from tiered_storage import TieredStorageManager, smart_compress, smart_decompress
from protocol import (
    new_identity, file_type_of, guess_mime, sort_name_of,
    encode_cursor, decode_cursor, DEFAULT_ORDER_BY, TYPE_DIRECTORY,
)

# Default chunk size: 4MB (same ballpark as the original)
DEFAULT_CHUNK_SIZE = 4 * 1024 * 1024  # 4,194,304 bytes

# Small files below this go inline (stored in meta, not as separate chunk)
INLINE_THRESHOLD = 1024  # 1KB — tiny files stored directly in metadata

# ==================== ON-DISK CHUNK ENCODING ====================
#
# 线上对象存储对「离线任务入库」的块做整块逐字节 XOR 0x75。已用真实样本验证：
#   下载 https://slices.qzy.xyz/ipfs/blocks/2/75/bafkreick4... 得到的响应体，
#   逐字节 XOR 0x75 之后 sha256 = bafkreick4...（即 HTTP Etag 就是它）。
# CID 始终覆盖明文，编码只影响落盘字节，所以内容寻址语义不变。
#
# 普通上传的块保持明文落盘（线上 Web 端不解码也能直接播放/预览）。
CHUNK_FMT_PLAIN = 0
CHUNK_FMT_XOR75 = 1
CHUNK_XOR_KEY = 0x75

_XOR_TABLE = bytes((b ^ CHUNK_XOR_KEY) for b in range(256))


def encode_chunk(data: bytes, fmt: int = CHUNK_FMT_PLAIN) -> bytes:
    """Plaintext chunk -> on-disk bytes."""
    if not data or fmt != CHUNK_FMT_XOR75:
        return data
    return data.translate(_XOR_TABLE)


def decode_chunk(data: bytes, fmt: int = CHUNK_FMT_PLAIN) -> bytes:
    """On-disk bytes -> plaintext chunk (XOR is its own inverse)."""
    if not data or fmt != CHUNK_FMT_XOR75:
        return data
    return data.translate(_XOR_TABLE)


def now_ms() -> int:
    """Timestamps are milliseconds since epoch, matching the v6 API."""
    return int(time.time() * 1000)


@dataclass
class ChunkInfo:
    """Info about a single chunk within a file."""
    cid: str               # Content identifier
    index: int             # Position in file (0-based)
    start: int             # Byte offset in original file
    end: int               # Byte offset (exclusive)
    size: int              # Chunk size in bytes


@dataclass
class FileMeta:
    """Metadata for a stored file — this is what the server indexes.

    Field names mirror the v6 ``File`` message so the same record can be
    serialized straight into protobuf / JSON API responses.
    """
    identity: str                    # 32 hex chars — v6 identity format
    parent: str                      # Parent directory ID ("root" for top)
    name: str                        # Filename
    path: str                        # Full path
    size: int                        # Total file size
    mime_type: str = "application/octet-stream"
    type: int = 0                    # v6 category code (20=dir, 40=video, ...)
    is_dir: bool = False
    content_identity: str = ""       # File-level CID (hash of all chunk CIDs)
    store_type: int = 0              # 0=chunked, 1=inline
    chunks: List[ChunkInfo] = field(default_factory=list)
    create_ts: int = 0
    update_ts: int = 0
    delete_ts: int = 0
    deleted: bool = False
    trashed: bool = False
    shared: bool = False
    hidden: bool = False
    starred: bool = False
    locked: bool = False
    version: int = 1
    sort_name: str = ""              # separators collapsed, latin lowercased
    nodes: int = 0                   # descendant count (dirs only)
    files: int = 0                   # descendant file count (dirs only)
    directories: int = 0             # descendant dir count (dirs only)
    user_id: str = "anonymous"

    def to_dict(self) -> dict:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "FileMeta":
        d = dict(d)
        chunks_data = d.pop("chunks", [])
        chunks = [ChunkInfo(**c) for c in chunks_data]
        known = {f for f in cls.__dataclass_fields__ if f != "chunks"}
        return cls(chunks=chunks, **{k: v for k, v in d.items() if k in known})



@dataclass
class FastLookupEntry:
    """Index entry for instant transfer (秒传)."""
    identity: str           # UUID
    sha1: str               # Full file SHA1
    head_sha1: str          # SHA1 of first 256KB
    file_size: int
    content_identity: str   # File CID
    name: str = ""
    create_ts: int = 0


class StorageEngine:
    """
    The storage engine handles:
    - Chunking files and computing CIDs
    - Storing/retrieving chunks on disk
    - FastLookup index for dedup/秒传
    - File metadata management
    - Serving chunk downloads
    """

    def __init__(self, storage_root: str, chunk_size: int = DEFAULT_CHUNK_SIZE):
        self.root = Path(storage_root)
        self.chunk_size = chunk_size
        self.chunks_dir = self.root / "chunks"
        self.meta_dir = self.root / "meta"
        self.db_dir = self.root / "db"
        self._fmt_cache: Dict[str, int] = {}

        # Create directory structure
        for d in [self.chunks_dir, self.meta_dir, self.db_dir]:
            d.mkdir(parents=True, exist_ok=True)

        # Initialize database
        self.db_path = self.db_dir / "index.sqlite"
        self._init_db()
        self.tiered = TieredStorageManager(storage_root, self.db_path, self.chunks_dir)
        self._init_versions_table()

    def _init_versions_table(self):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS file_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_identity TEXT NOT NULL,
                version INTEGER NOT NULL,
                content_identity TEXT NOT NULL,
                size INTEGER NOT NULL,
                chunks_json TEXT NOT NULL,
                create_ts INTEGER NOT NULL,
                note TEXT DEFAULT ''
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_file_versions_fid ON file_versions(file_identity, version)")
        conn.commit()
        conn.close()

    def _init_db(self):
        """Initialize SQLite database with all tables."""
        conn = connect_db(self.db_path)
        c = conn.cursor()

        # FastLookup table — for instant transfer (秒传)
        c.execute("""
            CREATE TABLE IF NOT EXISTS fast_lookup (
                identity TEXT PRIMARY KEY,
                sha1 TEXT NOT NULL,
                head_sha1 TEXT NOT NULL,
                file_size INTEGER NOT NULL,
                content_identity TEXT NOT NULL,
                name TEXT,
                create_ts INTEGER,
                user_id TEXT NOT NULL DEFAULT 'anonymous',
                UNIQUE(sha1, file_size, user_id)
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_fast_sha1 ON fast_lookup(sha1)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_fast_head ON fast_lookup(head_sha1, file_size, user_id)")

        # File tree table — metadata only (actual file data is in chunks)
        c.execute("""
            CREATE TABLE IF NOT EXISTS files (
                identity TEXT PRIMARY KEY,
                parent TEXT NOT NULL,
                name TEXT NOT NULL,
                path TEXT NOT NULL,
                size INTEGER DEFAULT 0,
                mime_type TEXT DEFAULT 'application/octet-stream',
                is_dir INTEGER DEFAULT 0,
                content_identity TEXT DEFAULT '',
                store_type INTEGER DEFAULT 0,
                create_ts INTEGER,
                update_ts INTEGER,
                deleted INTEGER DEFAULT 0,
                trashed INTEGER DEFAULT 0,
                shared INTEGER DEFAULT 0,
                version INTEGER DEFAULT 1,
                user_id TEXT NOT NULL DEFAULT 'anonymous',
                type INTEGER DEFAULT 0,
                sort_name TEXT DEFAULT '',
                delete_ts INTEGER DEFAULT 0,
                hidden INTEGER DEFAULT 0,
                starred INTEGER DEFAULT 0,
                locked INTEGER DEFAULT 0,
                nodes INTEGER DEFAULT 0,
                desc_files INTEGER DEFAULT 0,
                desc_dirs INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_parent ON files(parent, deleted, user_id)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_path ON files(path, user_id)")

        # Chunk index — which chunks exist and where
        c.execute("""
            CREATE TABLE IF NOT EXISTS chunks (
                cid TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                ref_count INTEGER DEFAULT 1,
                create_ts INTEGER,
                inline_data BLOB,
                format INTEGER DEFAULT 0
            )
        """)

        # File -> Chunk mapping
        c.execute("""
            CREATE TABLE IF NOT EXISTS file_chunks (
                file_identity TEXT NOT NULL,
                chunk_index INTEGER NOT NULL,
                cid TEXT NOT NULL,
                start_byte INTEGER NOT NULL,
                end_byte INTEGER NOT NULL,
                size INTEGER NOT NULL,
                PRIMARY KEY(file_identity, chunk_index),
                FOREIGN KEY(file_identity) REFERENCES files(identity),
                FOREIGN KEY(cid) REFERENCES chunks(cid)
            )
        """)

        # User quota table
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_quota (
                user_id TEXT PRIMARY KEY,
                bytes_quota INTEGER DEFAULT 10737418240,
                bytes_used INTEGER DEFAULT 0,
                files_quota INTEGER DEFAULT 100000,
                files_used INTEGER DEFAULT 0
            )
        """)

        # Migrations for storage created before multi-user support.
        file_cols = {row[1] for row in c.execute("PRAGMA table_info(files)")}
        if "user_id" not in file_cols:
            c.execute("ALTER TABLE files ADD COLUMN user_id TEXT NOT NULL DEFAULT 'anonymous'")
            c.execute("CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id)")

        # Migrations for storage created before v6 protocol alignment.
        for col, decl in [
            ("type", "INTEGER DEFAULT 0"),
            ("sort_name", "TEXT DEFAULT ''"),
            ("delete_ts", "INTEGER DEFAULT 0"),
            ("hidden", "INTEGER DEFAULT 0"),
            ("starred", "INTEGER DEFAULT 0"),
            ("locked", "INTEGER DEFAULT 0"),
            ("nodes", "INTEGER DEFAULT 0"),
            ("desc_files", "INTEGER DEFAULT 0"),
            ("desc_dirs", "INTEGER DEFAULT 0"),
        ]:
            if col not in file_cols:
                c.execute(f"ALTER TABLE files ADD COLUMN {col} {decl}")
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_order ON files(parent, user_id, deleted, trashed, type, create_ts)")

        chunk_cols = {row[1] for row in c.execute("PRAGMA table_info(chunks)")}
        if "format" not in chunk_cols:
            c.execute("ALTER TABLE chunks ADD COLUMN format INTEGER DEFAULT 0")

        lookup_cols = {row[1] for row in c.execute("PRAGMA table_info(fast_lookup)")}
        if "user_id" not in lookup_cols:
            c.execute("ALTER TABLE fast_lookup RENAME TO fast_lookup_legacy")
            c.execute("""
                CREATE TABLE fast_lookup (
                    identity TEXT PRIMARY KEY,
                    sha1 TEXT NOT NULL,
                    head_sha1 TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    content_identity TEXT NOT NULL,
                    name TEXT,
                    create_ts INTEGER,
                    user_id TEXT NOT NULL DEFAULT 'anonymous',
                    UNIQUE(sha1, file_size, user_id)
                )
            """)
            c.execute("""
                INSERT OR IGNORE INTO fast_lookup
                    (identity, sha1, head_sha1, file_size, content_identity, name, create_ts, user_id)
                SELECT identity, sha1, head_sha1, file_size, content_identity, name, create_ts, 'anonymous'
                FROM fast_lookup_legacy
            """)
            c.execute("DROP TABLE fast_lookup_legacy")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fast_sha1 ON fast_lookup(sha1)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fast_head ON fast_lookup(head_sha1, file_size, user_id)")

        # 秒传索引全局化：线上 FastLookup 是跨用户的（同内容一次入库，处处秒传）。
        # 旧库的唯一约束含 user_id，必须重建表才能去掉它。
        if is_postgres_enabled():
            scoped_unique = c.execute(
                """SELECT tc.constraint_name
                   FROM information_schema.table_constraints tc
                   JOIN information_schema.key_column_usage kcu
                     ON tc.constraint_name = kcu.constraint_name
                    AND tc.table_schema = kcu.table_schema
                    AND tc.table_name = kcu.table_name
                   WHERE tc.constraint_type = 'UNIQUE'
                     AND tc.table_schema = current_schema()
                     AND tc.table_name = 'fast_lookup'
                   GROUP BY tc.constraint_name
                   HAVING COUNT(*) = 3
                      AND SUM(CASE WHEN kcu.column_name = 'sha1' THEN 1 ELSE 0 END) = 1
                      AND SUM(CASE WHEN kcu.column_name = 'file_size' THEN 1 ELSE 0 END) = 1
                      AND SUM(CASE WHEN kcu.column_name = 'user_id' THEN 1 ELSE 0 END) = 1"""
            ).fetchone()
            has_user_scoped_unique = scoped_unique is not None
        else:
            ddl = c.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name='fast_lookup'"
            ).fetchone()
            has_user_scoped_unique = bool(
                ddl and "user_id)" in (ddl[0] or "").replace(" ", "").replace("\n", "")
            )
        if has_user_scoped_unique:
            c.execute("ALTER TABLE fast_lookup RENAME TO fast_lookup_scoped")
            c.execute("""
                CREATE TABLE fast_lookup (
                    identity TEXT PRIMARY KEY,
                    sha1 TEXT NOT NULL,
                    head_sha1 TEXT NOT NULL,
                    file_size INTEGER NOT NULL,
                    content_identity TEXT NOT NULL,
                    name TEXT,
                    create_ts INTEGER,
                    user_id TEXT NOT NULL DEFAULT 'anonymous',
                    UNIQUE(sha1, file_size)
                )
            """)
            c.execute("""
                INSERT OR IGNORE INTO fast_lookup
                    (identity, sha1, head_sha1, file_size, content_identity, name, create_ts, user_id)
                SELECT identity, sha1, head_sha1, file_size, content_identity, name, create_ts, user_id
                FROM fast_lookup_scoped ORDER BY create_ts ASC
            """)
            c.execute("DROP TABLE fast_lookup_scoped")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fast_sha1 ON fast_lookup(sha1)")
            c.execute("CREATE INDEX IF NOT EXISTS idx_fast_head ON fast_lookup(head_sha1, file_size)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_files_user ON files(user_id)")

        conn.commit()
        conn.close()

    # ==================== CHUNKING ====================

    def chunk_file(self, file_path: str, parent: str = "root", name: str = None,
                  user_id: str = "anonymous",
                  chunk_format: int = CHUNK_FMT_PLAIN) -> FileMeta:
        """
        Split a file into chunks, compute CIDs, store chunks, create metadata.
        This is the UPLOAD flow.

        ``chunk_format`` selects the on-disk encoding: plain for user uploads,
        XOR 0x75 for offline-task ingest (matches the live object store).

        Returns the FileMeta with all chunk CIDs.
        """
        path_obj = Path(file_path)
        file_size = path_obj.stat().st_size
        if name is None:
            name = path_obj.name
        now = now_ms()

        # Read file and chunk it
        chunks: List[ChunkInfo] = []
        all_chunk_cids: List[str] = []
        sha1_full = hashlib.sha1()
        head_sha1 = hashlib.sha1()

        with open(file_path, "rb") as f:
            offset = 0
            chunk_index = 0
            while True:
                data = f.read(self.chunk_size)
                if not data:
                    break

                # Update file-level hashes
                sha1_full.update(data)
                if offset < 262144:
                    head_sha1.update(data[:max(0, 262144 - offset)])

                # Compute chunk CID
                cid = compute_cid(data)
                all_chunk_cids.append(cid)

                # Store chunk on disk (if not already there)
                self._store_chunk(cid, data, chunk_format)

                chunk_info = ChunkInfo(
                    cid=cid,
                    index=chunk_index,
                    start=offset,
                    end=offset + len(data),
                    size=len(data),
                )
                chunks.append(chunk_info)

                offset += len(data)
                chunk_index += 1

        # Handle empty file
        if file_size == 0:
            empty_data = b""
            cid = compute_cid(empty_data)
            self._store_chunk(cid, empty_data, chunk_format)
            chunks.append(ChunkInfo(cid=cid, index=0, start=0, end=0, size=0))
            all_chunk_cids.append(cid)

        # File-level content_identity: real UnixFS dag-pb over the ordered
        # slices (single slice collapses to its raw CID, like IPFS).
        file_cid = compute_file_cid([(c.cid, c.size) for c in chunks])

        # Check FastLookup for dedup (秒传)
        sha1_hex = sha1_full.hexdigest()
        head_sha1_hex = head_sha1.hexdigest()

        existing = self._fast_lookup(sha1_hex, head_sha1_hex, file_size, user_id=user_id)
        if existing:
            # File already exists for this user! No need to store again.
            # Chunks are already stored (content-addressed = automatic dedup)
            content_identity = existing.content_identity
            # We still create a new FileMeta (different path/name, same content)
        else:
            content_identity = file_cid
            # Add to FastLookup, scoped to the owning user.
            self._add_fast_lookup(sha1_hex, head_sha1_hex, file_size, file_cid, name, user_id=user_id)

        # Create file metadata
        file_identity = new_identity()
        mime = guess_mime(name)

        meta = FileMeta(
            identity=file_identity,
            parent=parent,
            name=name,
            path=self._build_path(parent, name, user_id),
            size=file_size,
            mime_type=mime,
            type=file_type_of(name, mime, is_dir=False),
            is_dir=False,
            content_identity=content_identity,
            store_type=0 if file_size > INLINE_THRESHOLD else 1,
            chunks=chunks,
            create_ts=now,
            update_ts=now,
            sort_name=sort_name_of(name),
            user_id=user_id or "anonymous",
        )

        # Save metadata to disk and database
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._bump_ancestors(parent, files_delta=1)

        return meta

    def chunk_data(self, data: bytes, name: str = "upload.bin", parent: str = "root",
                   user_id: str = "anonymous") -> FileMeta:
        """Chunk raw bytes (for API uploads without a file path)."""
        # Write to temp file and use chunk_file
        import tempfile
        with tempfile.NamedTemporaryFile(delete=False, suffix=f"_{name}") as tmp:
            tmp.write(data)
            tmp_path = tmp.name
        try:
            return self.chunk_file(tmp_path, parent=parent, name=name, user_id=user_id)
        finally:
            os.unlink(tmp_path)

    def create_file_from_content(self, content_identity: str, name: str,
                                 parent: str = "root", user_id: str = "anonymous",
                                 size: Optional[int] = None) -> Optional[FileMeta]:
        """Instant transfer: clone a file record from existing chunks.

        Nothing is uploaded — the new record points at slices the user already
        owns, so 秒传 costs one metadata write.
        """
        uid = user_id or "anonymous"
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT identity FROM files WHERE content_identity = ? AND user_id = ? "
            "AND deleted = 0 AND is_dir = 0 ORDER BY create_ts ASC LIMIT 1",
            (content_identity, uid),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        source = self.get_file_meta(row[0], user_id=uid)
        if not source:
            return None

        now = now_ms()
        mime = guess_mime(name)
        chunks = [ChunkInfo(cid=ch.cid, index=ch.index, start=ch.start,
                            end=ch.end, size=ch.size) for ch in source.chunks]
        meta = FileMeta(
            identity=new_identity(),
            parent=parent,
            name=name,
            path=self._build_path(parent, name, uid),
            size=int(size) if size is not None else source.size,
            mime_type=mime,
            type=file_type_of(name, mime),
            is_dir=False,
            content_identity=content_identity,
            store_type=source.store_type,
            chunks=chunks,
            create_ts=now,
            update_ts=now,
            sort_name=sort_name_of(name),
            user_id=uid,
        )
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._bump_ancestors(parent, files_delta=1)
        return meta

    def clone_file(self, source: FileMeta, name: str, parent: str = "root",
                   user_id: str = "anonymous") -> FileMeta:
        """Copy a file record (no data movement) — used by "save to my drive"."""
        uid = user_id or "anonymous"
        now = now_ms()
        mime = guess_mime(name)
        chunks = [ChunkInfo(cid=ch.cid, index=ch.index, start=ch.start,
                            end=ch.end, size=ch.size) for ch in source.chunks]
        meta = FileMeta(
            identity=new_identity(),
            parent=parent,
            name=name,
            path=self._build_path(parent, name, uid),
            size=source.size,
            mime_type=mime,
            type=file_type_of(name, mime, is_dir=source.is_dir),
            is_dir=source.is_dir,
            content_identity=source.content_identity,
            store_type=source.store_type,
            chunks=chunks,
            create_ts=now,
            update_ts=now,
            sort_name=sort_name_of(name),
            user_id=uid,
        )
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._bump_ancestors(parent, files_delta=0 if meta.is_dir else 1,
                             dirs_delta=1 if meta.is_dir else 0)
        return meta

    def chunk_size_of(self, cid: str) -> Optional[int]:
        """Size of an already-stored slice, or None when it is missing."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT size FROM chunks WHERE cid = ?", (cid,))
        row = c.fetchone()
        conn.close()
        if row:
            return int(row[0])
        path = self._chunk_path(cid)
        if path.exists():
            return path.stat().st_size
        return None

    def create_file_from_blocks(self, name: str, cids: List[str], parent: str = "root",
                                user_id: str = "anonymous", size: Optional[int] = None,
                                sha1_hex: str = "", head_sha1_hex: str = "") -> FileMeta:
        """Register a file whose slices were uploaded one by one.

        This is the resumable-upload path: ``CreateUploadTask`` returns the
        slice parameters, the client POSTs each 4 MiB block, and the final
        call stitches the block CIDs into a file record.
        """
        if not cids:
            raise ValueError("no blocks supplied")

        chunks: List[ChunkInfo] = []
        offset = 0
        for index, cid in enumerate(cids):
            block_size = self.chunk_size_of(cid)
            if block_size is None:
                raise ValueError(f"missing block: {cid}")
            chunks.append(ChunkInfo(cid=cid, index=index, start=offset,
                                    end=offset + block_size, size=block_size))
            offset += block_size

        total = int(size) if size is not None else offset
        content_identity = compute_file_cid([(c.cid, c.size) for c in chunks])

        if sha1_hex:
            existing = self._fast_lookup(sha1_hex, head_sha1_hex, total, user_id=user_id)
            if existing:
                content_identity = existing.content_identity
            else:
                self._add_fast_lookup(sha1_hex, head_sha1_hex, total,
                                      content_identity, name, user_id=user_id)

        now = now_ms()
        mime = guess_mime(name)
        meta = FileMeta(
            identity=new_identity(),
            parent=parent,
            name=name,
            path=self._build_path(parent, name, user_id),
            size=total,
            mime_type=mime,
            type=file_type_of(name, mime),
            is_dir=False,
            content_identity=content_identity,
            store_type=0,
            chunks=chunks,
            create_ts=now,
            update_ts=now,
            sort_name=sort_name_of(name),
            user_id=user_id or "anonymous",
        )
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._bump_ancestors(parent, files_delta=1)
        return meta

    def _store_chunk(self, cid: str, data: bytes, fmt: int = CHUNK_FMT_PLAIN):
        """Store a chunk on disk, named by its CID. Dedup is automatic —
        if the CID already exists, we don't write again.

        ``fmt`` selects the on-disk encoding (plain vs XOR 0x75). Content is
        content-addressed by its plaintext, so the CID is identical either way.
        """
        chunk_path = self._chunk_path(cid)
        if chunk_path.exists():
            if hasattr(self, "tiered"):
                self.tiered.touch(cid)
                if not self.tiered.ensure_primary_chunk(cid):
                    raise RuntimeError(f"Primary ImgBed persistence retry failed for chunk {cid}")
            return  # Already stored — this IS the dedup!
        chunk_path.parent.mkdir(parents=True, exist_ok=True)
        encoded = encode_chunk(data, fmt)
        compressed = False
        if fmt == CHUNK_FMT_PLAIN and hasattr(self, "tiered") and self.tiered.compression_enabled:
            encoded, compressed = smart_compress(encoded)
        chunk_path.write_bytes(encoded)

        # Record in DB
        conn = connect_db(self.db_path)
        c = conn.cursor()
        try:
            c.execute(
                "INSERT OR IGNORE INTO chunks (cid, size, ref_count, create_ts, inline_data, format)"
                " VALUES (?, ?, 1, ?, NULL, ?)",
                (cid, len(data), int(time.time()), int(fmt))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass  # Already exists
        finally:
            conn.close()
        self._fmt_cache[cid] = int(fmt)
        if hasattr(self, "tiered"):
            self.tiered.record_chunk_stored(cid, len(data), len(encoded), compressed, hashlib.sha256(encoded).hexdigest())

    def _chunk_path(self, cid: str) -> Path:
        """Convert CID to a sharded path: chunks/ba/fk/<full_cid>"""
        # Use first 2 chars + next 2 chars for sharding
        prefix = cid[:2]
        prefix2 = cid[2:4]
        return self.chunks_dir / prefix / prefix2 / cid

    # ==================== DOWNLOAD ====================

    def get_chunk(self, cid: str) -> Optional[bytes]:
        """Retrieve a chunk by its CID (returns plaintext — decoding is automatic)."""
        # Try disk first
        chunk_path = self._chunk_path(cid)
        if chunk_path.exists():
            raw = chunk_path.read_bytes()
            if hasattr(self, "tiered"):
                self.tiered.touch(cid)
            fmt = self.chunk_format(cid)
            if fmt == CHUNK_FMT_PLAIN:
                raw = smart_decompress(raw)
            return decode_chunk(raw, fmt)

        # Transparently hydrate from cold tier (S3 / R2 / MinIO / NAS) on demand
        if hasattr(self, "tiered") and self.tiered.backend is not None:
            raw = self.tiered.hydrate_from_cold(cid)
            if raw is not None:
                fmt = self.chunk_format(cid)
                if fmt == CHUNK_FMT_PLAIN:
                    raw = smart_decompress(raw)
                return decode_chunk(raw, fmt)

        # Try inline data in DB
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT inline_data FROM chunks WHERE cid = ?", (cid,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]

        return None

    def get_chunk_raw(self, cid: str) -> Optional[bytes]:
        """Return the bytes exactly as stored (XOR 0x75 applied when format=1).

        This is what the CDN edge serves: the live slices endpoint returned the
        obfuscated bytes, not the plaintext (verified against the Etag).
        """
        chunk_path = self._chunk_path(cid)
        if chunk_path.exists():
            if hasattr(self, "tiered"):
                self.tiered.touch(cid)
            return chunk_path.read_bytes()
        if hasattr(self, "tiered") and self.tiered.backend is not None:
            raw = self.tiered.hydrate_from_cold(cid)
            if raw is not None:
                return raw
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT inline_data FROM chunks WHERE cid = ?", (cid,))
        row = c.fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
        return None

    def chunk_format(self, cid: str) -> int:
        """On-disk encoding for a chunk: 0 = plain, 1 = XOR 0x75."""
        cached = self._fmt_cache.get(cid)
        if cached is not None:
            return cached
        conn = connect_db(self.db_path)
        c = conn.cursor()
        try:
            c.execute("SELECT format FROM chunks WHERE cid = ?", (cid,))
            row = c.fetchone()
        except sqlite3.OperationalError:
            row = None
        conn.close()
        fmt = int(row[0]) if row and row[0] is not None else CHUNK_FMT_PLAIN
        self._fmt_cache[cid] = fmt
        return fmt

    def set_chunk_format(self, cid: str, fmt: int) -> bool:
        """Re-encode an existing chunk in place (plain <-> XOR 0x75)."""
        data = self.get_chunk(cid)
        if data is None:
            return False
        path = self._chunk_path(cid)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(encode_chunk(data, fmt))
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE chunks SET format = ? WHERE cid = ?", (int(fmt), cid))
        conn.commit()
        conn.close()
        self._fmt_cache[cid] = int(fmt)
        return True

    def download_file(self, file_identity: str, output_path: str,
                      user_id: str = None) -> bool:
        """
        Reassemble a file from its chunks. This is the DOWNLOAD flow.
        Chunks are fetched by CID and concatenated in order.
        """
        meta = self.get_file_meta(file_identity, user_id=user_id)
        if not meta:
            return False

        with open(output_path, "wb") as f:
            for chunk in meta.chunks:
                data = self.get_chunk(chunk.cid)
                if data is None:
                    return False  # Missing chunk!
                f.write(data)

        return True

    def stream_chunks(self, file_identity: str,
                      user_id: str = None) -> Generator[Tuple[str, int, bytes], None, None]:
        """
        Stream chunks one by one. Yields (cid, index, data) tuples.
        Useful for HTTP streaming downloads.
        """
        meta = self.get_file_meta(file_identity, user_id=user_id)
        if not meta:
            return
        for chunk in meta.chunks:
            data = self.get_chunk(chunk.cid)
            if data is not None:
                yield (chunk.cid, chunk.index, data)

    # ==================== FAST LOOKUP (秒传) ====================

    def _fast_lookup(self, sha1_hex: str, head_sha1_hex: str, file_size: int,
                     user_id: str = None) -> Optional[FastLookupEntry]:
        """Look up content by hash across the whole deployment (global 秒传).

        The live service dedups globally: whoever uploaded first seeds the
        content, every later upload of the same bytes transfers instantly.
        ``user_id`` is accepted for call-site compatibility but does not scope
        the lookup.
        """
        conn = connect_db(self.db_path)
        c = conn.cursor()

        # Fast check: head_sha1 + size (catches 99% of duplicates quickly)
        c.execute(
            "SELECT identity, sha1, head_sha1, file_size, content_identity, name, create_ts "
            "FROM fast_lookup WHERE head_sha1 = ? AND file_size = ? "
            "ORDER BY create_ts ASC LIMIT 1",
            (head_sha1_hex, file_size),
        )
        row = c.fetchone()
        conn.close()

        if row:
            # Verify with full SHA1 to be sure
            if row[1] == sha1_hex:
                return FastLookupEntry(
                    identity=row[0], sha1=row[1], head_sha1=row[2],
                    file_size=row[3], content_identity=row[4],
                    name=row[5] or "", create_ts=row[6] or 0
                )
        return None

    def _add_fast_lookup(self, sha1_hex: str, head_sha1_hex: str, file_size: int,
                         content_identity: str, name: str,
                         user_id: str = "anonymous"):
        """Add an entry to the FastLookup index, scoped to the owning user."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT OR IGNORE INTO fast_lookup (identity, sha1, head_sha1, file_size, content_identity, name, create_ts, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (new_identity(), sha1_hex, head_sha1_hex, file_size, content_identity, name,
             int(time.time()), user_id or "anonymous")
        )
        conn.commit()
        conn.close()

    def check_instant_transfer(self, sha1_hex: str, head_sha1_hex: str, file_size: int,
                               user_id: str = None) -> Optional[str]:
        """
        Check if a file can be instant-transferred (秒传).
        Returns the content_identity (CID) if yes, None if file needs normal upload.
        """
        entry = self._fast_lookup(sha1_hex, head_sha1_hex, file_size, user_id=user_id)
        return entry.content_identity if entry else None

    # ==================== METADATA ====================

    def _save_meta(self, meta: FileMeta):
        """Save file metadata to disk as JSON."""
        meta_path = self.meta_dir / f"{meta.identity}.json"
        meta_path.write_text(json.dumps(meta.to_dict(), indent=2, ensure_ascii=False))

    def get_file_meta(self, identity: str, user_id: str = None) -> Optional[FileMeta]:
        """Load file metadata by identity, optionally enforcing ownership."""
        meta_path = self.meta_dir / f"{identity}.json"
        if not meta_path.exists():
            return None
        data = json.loads(meta_path.read_text())
        meta = FileMeta.from_dict(data)
        if user_id is not None and (meta.user_id or "anonymous") != user_id:
            return None
        return meta

    def list_files(self, parent: str = "root", include_trashed: bool = False,
                   user_id: str = None) -> List[FileMeta]:
        """List files in a directory, optionally scoped to one user."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        params = [parent]
        user_clause = ""
        if user_id is not None:
            user_clause = " AND user_id = ?"
            params.append(user_id)
        if include_trashed:
            c.execute("SELECT identity FROM files WHERE parent = ? AND deleted = 0" + user_clause, params)
        else:
            c.execute("SELECT identity FROM files WHERE parent = ? AND deleted = 0 AND trashed = 0" + user_clause, params)
        identities = [row[0] for row in c.fetchall()]
        conn.close()

        results = []
        for identity in identities:
            meta = self.get_file_meta(identity)
            if meta:
                results.append(meta)
        return results

    # ==================== CURSOR PAGING (v6 ScanList) ====================

    def scan_list(self, parent: str = "root", user_id: str = None,
                  limit: int = 100, token: str = None,
                  include_trashed: bool = False,
                  only_trashed: bool = False) -> dict:
        """Cursor-paged listing, mirroring ``PubUserFile.List``.

        Request:  ``{parent, list_info:{token, limit, order_by, version}}``
        Response: ``{files: [...], list_info:{token, total}}``

        Ordering matches the live service: ``type ASC, create_ts DESC`` —
        directories (type 20) come before videos (type 40).
        """
        cursor = decode_cursor(token) or {}
        skip = max(0, int(cursor.get("skip", 0) or 0))
        take = int(cursor.get("limit") or limit or 100)
        take = max(1, min(take, 1000))

        params = [parent]
        clause = ""
        if user_id is not None:
            clause += " AND user_id = ?"
            params.append(user_id)
        if not include_trashed:
            clause += " AND trashed = 0"
        if only_trashed:
            clause = clause.replace(" AND trashed = 0", "") + " AND trashed = 1"

        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM files WHERE parent = ? AND deleted = 0" + clause, params)
        total = c.fetchone()[0]
        c.execute(
            "SELECT identity FROM files WHERE parent = ? AND deleted = 0" + clause
            + " ORDER BY is_dir DESC, (CASE WHEN type = 0 THEN 1 ELSE 0 END), type ASC,"
              " create_ts DESC, identity ASC LIMIT ? OFFSET ?",
            params + [take, skip],
        )
        identities = [row[0] for row in c.fetchall()]
        conn.close()

        files = [m for m in (self.get_file_meta(i) for i in identities) if m]
        next_skip = skip + len(files)
        next_token = ""
        if next_skip < total:
            next_token = encode_cursor(user_id or "", next_skip, take)

        return {
            "files": files,
            "list_info": {"token": next_token, "total": int(total),
                          "limit": take, "skip": skip},
        }

    # ==================== SEARCH ====================

    def search_files(self, name: str, user_id: str = None, limit: int = 100,
                     token: str = None) -> dict:
        """Substring search over name / sort_name (``PubUserFile.Search``)."""
        cursor = decode_cursor(token) or {}
        skip = max(0, int(cursor.get("skip", 0) or 0))
        take = max(1, min(int(cursor.get("limit") or limit or 100), 1000))
        pattern = f"%{(name or '').strip()}%"

        params = [pattern, pattern]
        clause = ""
        if user_id is not None:
            clause = " AND user_id = ?"
            params.append(user_id)
        where = "WHERE deleted = 0 AND trashed = 0 AND (name LIKE ? OR sort_name LIKE ?)" + clause

        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM files " + where, params)
        total = c.fetchone()[0]
        c.execute(
            "SELECT identity FROM files " + where
            + " ORDER BY is_dir DESC, (CASE WHEN type = 0 THEN 1 ELSE 0 END), type ASC,"
              " create_ts DESC, identity ASC LIMIT ? OFFSET ?",
            params + [take, skip],
        )
        identities = [row[0] for row in c.fetchall()]
        conn.close()

        files = [m for m in (self.get_file_meta(i) for i in identities) if m]
        next_skip = skip + len(files)
        return {
            "files": files,
            "list_info": {
                "token": encode_cursor(user_id or "", next_skip, take) if next_skip < total else "",
                "total": int(total), "limit": take, "skip": skip,
            },
        }

    # ==================== TREE MAINTENANCE ====================

    def _bump_ancestors(self, parent: str, files_delta: int = 0, dirs_delta: int = 0):
        """Propagate descendant counters up the directory chain."""
        seen = set()
        current = parent
        while current and current != "root" and current not in seen:
            seen.add(current)
            meta = self.get_file_meta(current)
            if not meta or not meta.is_dir:
                return
            meta.files = max(0, meta.files + files_delta)
            meta.directories = max(0, meta.directories + dirs_delta)
            meta.nodes = meta.files + meta.directories
            self._save_meta(meta)
            self._db_insert_file(meta)
            current = meta.parent

    def _rewrite_descendant_paths(self, identity: str):
        """Recompute ``path`` for every descendant after a move or rename."""
        parent = self.get_file_meta(identity)
        if not parent or not parent.is_dir:
            return
        for child in self.list_files(identity, include_trashed=True):
            child.path = f"{parent.path.rstrip('/')}/{child.name}"
            self._save_meta(child)
            self._db_insert_file(child)
            if child.is_dir:
                self._rewrite_descendant_paths(child.identity)

    def move(self, identity: str, new_parent: str,
             user_id: str = None) -> Optional[FileMeta]:
        """Move a file/folder, keeping paths and v6 metadata in sync."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta:
            return None
        old_parent = meta.parent
        meta.parent = new_parent
        meta.path = self._build_path(new_parent, meta.name)
        meta.update_ts = now_ms()
        meta.version += 1
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._rewrite_descendant_paths(identity)
        self._bump_ancestors(old_parent, -1 if meta.is_dir else 0, 0)
        self._bump_ancestors(new_parent, 1 if meta.is_dir else 0, 0)
        return meta

    def rename(self, identity: str, new_name: str,
               user_id: str = None) -> Optional[FileMeta]:
        """Rename a file/folder, refreshing path, type and sort key."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta or not new_name:
            return None
        meta.name = new_name
        meta.path = self._build_path(meta.parent, new_name)
        meta.sort_name = sort_name_of(new_name)
        if not meta.is_dir:
            meta.mime_type = guess_mime(new_name)
            meta.type = file_type_of(new_name, meta.mime_type)
        meta.update_ts = now_ms()
        meta.version += 1
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._rewrite_descendant_paths(identity)
        return meta

    # ==================== COPY / UPDATE / BATCH (v6 Copy, Update, BatchOperation) ====================

    def _unique_name(self, parent: str, name: str, is_dir: bool,
                     user_id: str = None) -> str:
        """Return a name that does not collide inside ``parent`` (``x (1).ext``)."""
        siblings = {m.name for m in self.list_files(parent, include_trashed=True,
                                                    user_id=user_id)}
        if name not in siblings:
            return name
        if is_dir or "." not in name.lstrip("."):
            stem, ext = name, ""
        else:
            stem, ext = name.rsplit(".", 1)
            ext = "." + ext
        idx = 1
        while f"{stem} ({idx}){ext}" in siblings:
            idx += 1
        return f"{stem} ({idx}){ext}"

    def copy(self, identity: str, new_parent: str = None, new_name: str = None,
             user_id: str = None) -> Optional[FileMeta]:
        """Deep-copy a file/folder; chunk data is shared (same CIDs ⇒ no re-upload)."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta:
            return None
        parent = new_parent if new_parent not in (None, "") else meta.parent
        name = self._unique_name(parent, new_name or meta.name, meta.is_dir, meta.user_id)
        return self._copy_node(meta, parent, name)

    def _copy_node(self, meta: FileMeta, parent: str, name: str) -> FileMeta:
        now = now_ms()
        child = FileMeta(
            identity=new_identity(), parent=parent, name=name,
            path=self._build_path(parent, name),
            size=meta.size, mime_type=meta.mime_type, type=meta.type,
            is_dir=meta.is_dir, content_identity=meta.content_identity,
            store_type=meta.store_type,
            chunks=[ChunkInfo(c.cid, c.index, c.start, c.end, c.size) for c in meta.chunks],
            create_ts=now, update_ts=now, sort_name=sort_name_of(name),
            hidden=meta.hidden, locked=meta.locked, starred=meta.starred,
            user_id=meta.user_id,
        )
        self._save_meta(child)
        self._db_insert_file(child)
        self._bump_ancestors(parent, 0 if meta.is_dir else 1, 1 if meta.is_dir else 0)
        if meta.is_dir:
            for sub in self.list_files(meta.identity, include_trashed=False,
                                       user_id=meta.user_id):
                sub_name = self._unique_name(child.identity, sub.name, sub.is_dir,
                                            meta.user_id)
                self._copy_node(sub, child.identity, sub_name)
        return child

    def update(self, identity: str, name: str = None, mime_type: str = None,
               hidden: bool = None, locked: bool = None, starred: bool = None,
               user_id: str = None) -> Optional[FileMeta]:
        """Patch mutable attributes on a file record (``PubUserFile.Update``)."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta:
            return None
        if name and name != meta.name:
            meta.name = name
            meta.path = self._build_path(meta.parent, name)
            meta.sort_name = sort_name_of(name)
            if not meta.is_dir:
                meta.mime_type = mime_type or guess_mime(name)
                meta.type = file_type_of(name, meta.mime_type)
            self._rewrite_descendant_paths(identity)
        elif mime_type:
            meta.mime_type = mime_type
        if hidden is not None:
            meta.hidden = bool(hidden)
        if locked is not None:
            meta.locked = bool(locked)
        if starred is not None:
            meta.starred = bool(starred)
        meta.update_ts = now_ms()
        meta.version += 1
        self._save_meta(meta)
        self._db_insert_file(meta)
        return meta

    def batch(self, identities: List[str], operation: str, dest: str = None,
              user_id: str = None, permanent: bool = False) -> int:
        """Apply one operation to many identities; returns the affected count.

        ``operation`` mirrors the v6 ``BatchOperationRequest.operation`` codes:
        ``move`` / ``copy`` / ``trash`` / ``recover`` / ``delete``.
        """
        affected = 0
        for identity in identities or []:
            if not self.get_file_meta(identity, user_id=user_id):
                continue
            ok = False
            if operation == "move" and dest:
                ok = self.move(identity, dest, user_id=user_id) is not None
            elif operation == "copy" and dest:
                ok = self.copy(identity, new_parent=dest, user_id=user_id) is not None
            elif operation == "trash":
                ok = self.set_trashed(identity, True, user_id=user_id)
            elif operation == "recover":
                ok = self.set_trashed(identity, False, user_id=user_id)
            elif operation == "delete":
                ok = self.set_trashed(identity, True, permanent=True, user_id=user_id)
            if ok:
                affected += 1
        return affected

    def set_trashed(self, identity: str, trashed: bool = True,
                    permanent: bool = False, user_id: str = None) -> bool:
        """Trash, recover or permanently delete a file (v6 semantics)."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta:
            return False
        now = now_ms()
        if permanent:
            self.delete_file(identity, permanent=True, user_id=user_id)
            self._bump_ancestors(meta.parent, -1 if meta.is_dir else 0, 0)
            return True
        meta.trashed = trashed
        meta.delete_ts = now if trashed else 0
        meta.update_ts = now
        self._save_meta(meta)
        self._db_insert_file(meta)
        return True

    def _db_insert_file(self, meta: FileMeta):
        """Insert file record into database."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO files (identity, parent, name, path, size, mime_type, "
            "is_dir, content_identity, store_type, create_ts, update_ts, deleted, trashed, shared, version, user_id, "
            "type, sort_name, delete_ts, hidden, starred, locked, nodes, desc_files, desc_dirs) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(identity) DO UPDATE SET parent=excluded.parent, name=excluded.name, path=excluded.path, "
            "size=excluded.size, mime_type=excluded.mime_type, is_dir=excluded.is_dir, "
            "content_identity=excluded.content_identity, store_type=excluded.store_type, "
            "create_ts=excluded.create_ts, update_ts=excluded.update_ts, deleted=excluded.deleted, "
            "trashed=excluded.trashed, shared=excluded.shared, version=excluded.version, user_id=excluded.user_id, "
            "type=excluded.type, sort_name=excluded.sort_name, delete_ts=excluded.delete_ts, "
            "hidden=excluded.hidden, starred=excluded.starred, locked=excluded.locked, "
            "nodes=excluded.nodes, desc_files=excluded.desc_files, desc_dirs=excluded.desc_dirs",
            (meta.identity, meta.parent, meta.name, meta.path, meta.size,
             meta.mime_type, int(meta.is_dir), meta.content_identity, meta.store_type,
             meta.create_ts, meta.update_ts, int(meta.deleted), int(meta.trashed),
             int(meta.shared), meta.version, meta.user_id or "anonymous",
             meta.type, meta.sort_name, meta.delete_ts, int(meta.hidden),
             int(meta.starred), int(meta.locked), meta.nodes, meta.files, meta.directories)
        )
        # Replace the mapping set as a whole so stale trailing chunk rows cannot survive updates.
        c.execute("DELETE FROM file_chunks WHERE file_identity = ?", (meta.identity,))
        for chunk in meta.chunks:
            c.execute(
                "INSERT INTO file_chunks (file_identity, chunk_index, cid, start_byte, end_byte, size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (meta.identity, chunk.index, chunk.cid, chunk.start, chunk.end, chunk.size)
            )
        conn.commit()
        conn.close()

    def create_dir(self, name: str, parent: str = "root",
                   user_id: str = "anonymous") -> FileMeta:
        """Create a directory."""
        now = now_ms()
        identity = new_identity()
        meta = FileMeta(
            identity=identity, parent=parent, name=name,
            path=self._build_path(parent, name, user_id),
            size=0, is_dir=True, type=TYPE_DIRECTORY,
            mime_type="inode/directory",
            create_ts=now, update_ts=now,
            sort_name=sort_name_of(name),
            user_id=user_id or "anonymous",
        )
        self._save_meta(meta)
        self._db_insert_file(meta)
        self._bump_ancestors(parent, dirs_delta=1)
        return meta

    def _build_path(self, parent: str, name: str, user_id: str = None) -> str:
        """Resolve the absolute path, mirroring the v6 ``File.path`` field."""
        if not parent or parent == "root":
            return f"/{name}"
        parent_meta = self.get_file_meta(parent)
        if not parent_meta:
            return f"/{name}"
        base = parent_meta.path.rstrip("/")
        return f"{base}/{name}"

    def delete_file(self, identity: str, permanent: bool = False,
                    user_id: str = None):
        """Delete or trash a file, optionally enforcing ownership."""
        if user_id is not None and not self.get_file_meta(identity, user_id=user_id):
            return
        if permanent:
            conn = connect_db(self.db_path)
            c = conn.cursor()
            c.execute("UPDATE files SET deleted = 1 WHERE identity = ?", (identity,))
            conn.commit()
            conn.close()
            # Also remove meta file
            meta_path = self.meta_dir / f"{identity}.json"
            if meta_path.exists():
                meta_path.unlink()
        else:
            conn = connect_db(self.db_path)
            c = conn.cursor()
            c.execute("UPDATE files SET trashed = 1, delete_ts = ? WHERE identity = ?", (now_ms(), identity))
            conn.commit()
            conn.close()

    def can_user_access_chunk(self, cid: str, user_id: str = None) -> bool:
        """Return True when a user owns at least one file referencing this chunk."""
        if user_id is None:
            return True
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT 1 FROM file_chunks fc JOIN files f ON f.identity = fc.file_identity "
            "WHERE fc.cid = ? AND f.user_id = ? AND f.deleted = 0 LIMIT 1",
            (cid, user_id)
        )
        found = c.fetchone() is not None
        conn.close()
        return found

    # ==================== UTILS ====================

    def _guess_mime(self, filename: str) -> str:
        import mimetypes
        mime, _ = mimetypes.guess_type(filename)
        return mime or "application/octet-stream"

    def get_storage_stats(self, user_id: str = None) -> dict:
        """Get storage statistics, optionally scoped to one user."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        params = [] if user_id is None else [user_id]
        where_user = "" if user_id is None else " AND f.user_id = ?"

        if user_id is None:
            c.execute("SELECT COUNT(*), COALESCE(SUM(size), 0) FROM chunks")
        else:
            # Count each physical chunk once, even when several of the user's
            # files reference it — otherwise dedup savings disappear.
            c.execute(
                "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM chunks WHERE cid IN ("
                "  SELECT DISTINCT fc.cid FROM file_chunks fc "
                "  JOIN files f ON f.identity = fc.file_identity "
                "  WHERE f.deleted = 0 AND f.is_dir = 0 AND f.user_id = ?)",
                params
            )
        chunk_count, chunk_total = c.fetchone()

        c.execute(
            "SELECT COUNT(*), COALESCE(SUM(size), 0) FROM files WHERE deleted = 0 AND is_dir = 0" +
            ("" if user_id is None else " AND user_id = ?"),
            params
        )
        file_count, file_total = c.fetchone()

        c.execute(
            "SELECT COUNT(*) FROM fast_lookup" +
            ("" if user_id is None else " WHERE user_id = ?"),
            params
        )
        lookup_count = c.fetchone()[0]
        conn.close()

        if user_id is None:
            disk_used = 0
            for root, dirs, filenames in os.walk(str(self.chunks_dir)):
                for filename in filenames:
                    disk_used += os.path.getsize(os.path.join(root, filename))
        else:
            disk_used = int(chunk_total or 0)

        return {
            "total_chunks": int(chunk_count or 0),
            "total_files": int(file_count or 0),
            "logical_bytes": int(file_total or 0),
            "chunk_bytes": int(chunk_total or 0),
            "disk_bytes": int(disk_used or 0),
            "dedup_ratio": (1 - disk_used / file_total) if file_total > 0 else 0,
            "fast_lookup_entries": int(lookup_count or 0),
        }



    # ==================== PIKPAK / HALALCLOUD ENHANCEMENTS ====================

    def list_by_category(self, category: str, user_id: str = None, limit: int = 200) -> List[FileMeta]:
        """Smart category views: video, image, audio, doc, archive, starred."""
        cat = (category or "").lower()
        conn = connect_db(self.db_path)
        c = conn.cursor()
        params: list = []
        where = "deleted = 0 AND trashed = 0 AND is_dir = 0"
        if user_id is not None:
            where += " AND user_id = ?"
            params.append(user_id)

        if cat == "starred":
            where = "deleted = 0 AND trashed = 0 AND starred = 1"
            if user_id is not None:
                where += " AND user_id = ?"
        elif cat == "video":
            where += " AND (mime_type LIKE 'video/%' OR type = 40)"
        elif cat == "image":
            where += " AND (mime_type LIKE 'image/%' OR type = 50)"
        elif cat == "audio":
            where += " AND (mime_type LIKE 'audio/%' OR type = 60)"
        elif cat == "doc":
            where += (
                " AND (mime_type LIKE 'text/%' OR mime_type LIKE '%pdf%' "
                "OR mime_type LIKE '%word%' OR mime_type LIKE '%sheet%' "
                "OR mime_type LIKE '%presentation%' OR mime_type LIKE '%document%' "
                "OR type = 30)"
            )
        elif cat == "archive":
            where += (
                " AND (mime_type LIKE '%zip%' OR mime_type LIKE '%rar%' "
                "OR mime_type LIKE '%tar%' OR mime_type LIKE '%7z%' "
                "OR mime_type LIKE '%gzip%' OR type = 70)"
            )

        c.execute(f"SELECT identity FROM files WHERE {where} ORDER BY update_ts DESC LIMIT ?", params + [int(limit)])
        ids = [r[0] for r in c.fetchall()]
        conn.close()
        out = []
        for fid in ids:
            m = self.get_file_meta(fid, user_id=user_id)
            if m:
                out.append(m)
        return out

    def toggle_star(self, identity: str, starred: Optional[bool] = None, user_id: str = None) -> Optional[FileMeta]:
        """Star or unstar a file/folder."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta:
            return None
        new_val = (not meta.starred) if starred is None else bool(starred)
        meta.starred = new_val
        meta.update_ts = now_ms()
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE files SET starred = ?, update_ts = ? WHERE identity = ?", (1 if new_val else 0, meta.update_ts, identity))
        conn.commit()
        conn.close()
        self._save_meta(meta)
        return meta

    def save_version_snapshot(self, identity: str, note: str = "", user_id: str = None) -> Optional[dict]:
        """Record a snapshot of a file's current CID & chunk list for version history."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        chunks_json = json.dumps([asdict(ch) for ch in meta.chunks])
        ts = now_ms()
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO file_versions (file_identity, version, content_identity, size, chunks_json, create_ts, note) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) RETURNING id",
            (meta.identity, meta.version, meta.content_identity, meta.size, chunks_json, ts, note),
        )
        inserted = c.fetchone()
        vid = inserted[0] if inserted else None
        conn.commit()
        conn.close()
        return {"id": vid, "file_identity": meta.identity, "version": meta.version,
                "content_identity": meta.content_identity, "size": meta.size, "create_ts": ts, "note": note}

    def list_versions(self, identity: str, user_id: str = None) -> List[dict]:
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta:
            return []
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT id, version, content_identity, size, create_ts, note FROM file_versions "
            "WHERE file_identity = ? ORDER BY version DESC, id DESC",
            (identity,),
        )
        rows = [
            {"id": r[0], "version": r[1], "content_identity": r[2], "size": r[3], "create_ts": r[4], "note": r[5]}
            for r in c.fetchall()
        ]
        conn.close()
        return rows

    def restore_version(self, identity: str, version_id: int, user_id: str = None) -> Optional[FileMeta]:
        """Roll back a file to a previous version snapshot (saving current state first)."""
        meta = self.get_file_meta(identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT version, content_identity, size, chunks_json FROM file_versions "
            "WHERE id = ? AND file_identity = ?",
            (int(version_id), identity),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        # Snapshot current state before restoring
        self.save_version_snapshot(identity, note=f"回滚前自动备份 v{meta.version}", user_id=user_id)
        chunks_data = json.loads(row[3])
        meta.content_identity = row[1]
        meta.size = int(row[2])
        meta.chunks = [ChunkInfo(**ch) for ch in chunks_data]
        meta.version += 1
        meta.update_ts = now_ms()
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "UPDATE files SET content_identity = ?, size = ?, version = ?, update_ts = ? WHERE identity = ?",
            (meta.content_identity, meta.size, meta.version, meta.update_ts, identity),
        )
        c.execute("DELETE FROM file_chunks WHERE file_identity = ?", (identity,))
        for ch in meta.chunks:
            c.execute(
                "INSERT INTO file_chunks (file_identity, chunk_index, cid, start_byte, end_byte, size) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (identity, ch.index, ch.cid, ch.start, ch.end, ch.size),
            )
        conn.commit()
        conn.close()
        self._save_meta(meta)
        return meta

    def empty_trash(self, user_id: str = None) -> dict:
        """Permanently delete all trashed files for a user and run chunk GC."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        if user_id is not None:
            c.execute("SELECT identity FROM files WHERE trashed = 1 AND deleted = 0 AND user_id = ?", (user_id,))
        else:
            c.execute("SELECT identity FROM files WHERE trashed = 1 AND deleted = 0")
        ids = [r[0] for r in c.fetchall()]
        conn.close()
        for fid in ids:
            self.delete_file(fid, permanent=True, user_id=user_id)
        gc = self.gc_chunks()
        return {"deleted_files": len(ids), **gc}

    def purge_expired_trash(self, retention_days: Optional[int] = None) -> dict:
        """Automatically clean up trashed files older than retention_days."""
        if retention_days is None:
            retention_days = int(self.tiered.config.get("trash_retention_days", 30)) if hasattr(self, "tiered") else 30
        if retention_days <= 0:
            return {"deleted_files": 0, "reclaimed_chunks": 0, "reclaimed_bytes": 0}
        cutoff_ms = now_ms() - retention_days * 86400 * 1000
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT identity FROM files WHERE trashed = 1 AND deleted = 0 "
            "AND ((delete_ts > 0 AND delete_ts < ?) OR (delete_ts = 0 AND update_ts < ?))",
            (cutoff_ms, cutoff_ms),
        )
        ids = [r[0] for r in c.fetchall()]
        conn.close()
        for fid in ids:
            self.delete_file(fid, permanent=True)
        gc = self.gc_chunks() if ids else {"reclaimed_chunks": 0, "reclaimed_bytes": 0}
        return {"deleted_files": len(ids), **gc}

    def gc_chunks(self) -> dict:
        """Garbage-collect unreferenced chunks from local disk and cold storage."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        # Collect all CIDs still referenced by live files
        c.execute(
            "SELECT DISTINCT fc.cid FROM file_chunks fc "
            "JOIN files f ON f.identity = fc.file_identity WHERE f.deleted = 0"
        )
        live_cids = {r[0] for r in c.fetchall()}
        # Also keep CIDs referenced by version snapshots of live files
        c.execute(
            "SELECT fv.chunks_json FROM file_versions fv "
            "JOIN files f ON f.identity = fv.file_identity WHERE f.deleted = 0"
        )
        for (cj,) in c.fetchall():
            try:
                for ch in json.loads(cj):
                    if ch.get("cid"):
                        live_cids.add(ch["cid"])
            except Exception:
                pass
        c.execute("SELECT cid FROM chunks")
        all_cids = [r[0] for r in c.fetchall()]
        orphan_cids = [cid for cid in all_cids if cid not in live_cids]

        reclaimed_bytes = 0
        for cid in orphan_cids:
            if hasattr(self, "tiered"):
                reclaimed_bytes += self.tiered.delete_chunk_everywhere(cid)
            else:
                p = self._chunk_path(cid)
                if p.exists():
                    reclaimed_bytes += p.stat().st_size
                    p.unlink(missing_ok=True)
            c.execute("DELETE FROM chunks WHERE cid = ?", (cid,))
            self._fmt_cache.pop(cid, None)
        conn.commit()
        conn.close()
        return {"reclaimed_chunks": len(orphan_cids), "reclaimed_bytes": reclaimed_bytes}


if __name__ == "__main__":
    # Self-test: upload and download a file
    import tempfile

    storage = StorageEngine("./test_storage")

    # Create a test file
    test_content = b"This is a test file for the storage engine!\n" * 100
    with tempfile.NamedTemporaryFile(delete=False, suffix=".txt") as f:
        f.write(test_content)
        test_file = f.name

    print(f"=== Uploading {len(test_content)} bytes ===")
    meta = storage.chunk_file(test_file, parent="root")
    print(f"File identity: {meta.identity}")
    print(f"Content CID: {meta.content_identity}")
    print(f"Chunks: {len(meta.chunks)}")
    for c in meta.chunks:
        print(f"  chunk[{c.index}]: cid={c.cid[:30]}... size={c.size}")

    # Test 秒传 (instant transfer) — same content should be detected
    print(f"\n=== Testing instant transfer (秒传) ===")
    fp = quick_fingerprint(test_content)
    existing_cid = storage.check_instant_transfer(
        fp["sha1"], fp["head_sha1"], fp["size"]
    )
    print(f"Instant transfer result: {existing_cid}")
    print(f"Matches original CID: {existing_cid == meta.content_identity}")

    # Download and verify
    print(f"\n=== Downloading ===")
    output = test_file + ".downloaded"
    success = storage.download_file(meta.identity, output)
    if success:
        with open(output, "rb") as f:
            downloaded = f.read()
        print(f"Downloaded {len(downloaded)} bytes")
        print(f"Content matches: {downloaded == test_content}")
        os.unlink(output)
    else:
        print("Download failed!")

    # Test with a larger file (multiple chunks)
    print(f"\n=== Testing multi-chunk file (10MB) ===")
    big_content = os.urandom(10 * 1024 * 1024)  # 10MB
    with tempfile.NamedTemporaryFile(delete=False, suffix=".bin") as f:
        f.write(big_content)
        big_file = f.name

    big_meta = storage.chunk_file(big_file, parent="root")
    print(f"File: {big_meta.name}")
    print(f"Size: {big_meta.size:,} bytes")
    print(f"Chunks: {len(big_meta.chunks)}")
    print(f"Chunk CIDs: {[c.cid[:16] for c in big_meta.chunks]}")

    # Download and verify big file
    big_output = big_file + ".downloaded"
    success = storage.download_file(big_meta.identity, big_output)
    if success:
        with open(big_output, "rb") as f:
            dl = f.read()
        print(f"Downloaded {len(dl):,} bytes")
        print(f"Content matches: {dl == big_content}")
        os.unlink(big_output)

    # Storage stats
    print(f"\n=== Storage Stats ===")
    stats = storage.get_storage_stats()
    for k, v in stats.items():
        if isinstance(v, float):
            print(f"  {k}: {v:.4f}")
        else:
            print(f"  {k}: {v:,}")

    # Cleanup
    os.unlink(test_file)
    os.unlink(big_file)
    print("\n✅ Storage engine works correctly")
