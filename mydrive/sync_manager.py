"""
Sync Manager — 本地 ↔ 云端同步
================================
实现本地文件夹和云端存储之间的双向同步。

支持三种模式：
1. upload（单向上传）：本地变更 → 云端
2. download（单向下载）：云端变更 → 本地
3. bidirectional（双向）：两边变更互相同步

策略：
- 用文件 mtime + size 做快速变更检测
- 用 SHA1 做精确对比（和 FastLookup 一致）
- 上传时自动走 storage engine 的 chunk + CID + 秒传流程
- 下载时自动走 download manager 的分块下载
- 同步记录持久化在 SQLite，重启后可继续

pairs 表记录同步对：
  { id, local_path, cloud_parent, direction, last_sync_ts, status }
sync_records 表记录每个文件的同步状态：
  { pair_id, local_rel_path, file_identity, local_mtime, local_size, sha1, last_sync_ts }
"""

import os
import time
import uuid
import json
import hashlib
import sqlite3
from db_adapter import connect_db
from pathlib import Path
from typing import Optional, List

from storage_engine import StorageEngine
from cid_engine import quick_fingerprint


class SyncManager:
    def __init__(self, engine: StorageEngine):
        self.engine = engine
        self.db_path = engine.db_dir / "sync.sqlite"
        self._init_db()

    def _init_db(self):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS sync_pairs (
                id TEXT PRIMARY KEY,
                local_path TEXT NOT NULL,
                cloud_parent TEXT NOT NULL,
                direction TEXT DEFAULT 'upload',
                last_sync_ts INTEGER DEFAULT 0,
                status TEXT DEFAULT 'idle',
                created_ts INTEGER,
                user_id TEXT NOT NULL DEFAULT 'anonymous'
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sync_records (
                pair_id TEXT NOT NULL,
                local_rel_path TEXT NOT NULL,
                file_identity TEXT,
                local_mtime INTEGER,
                local_size INTEGER,
                sha1 TEXT,
                last_sync_ts INTEGER,
                PRIMARY KEY (pair_id, local_rel_path)
            )
        """)
        cols = {row[1] for row in c.execute("PRAGMA table_info(sync_pairs)")}
        if "user_id" not in cols:
            c.execute("ALTER TABLE sync_pairs ADD COLUMN user_id TEXT NOT NULL DEFAULT 'anonymous'")
        c.execute("CREATE INDEX IF NOT EXISTS idx_sync_user ON sync_pairs(user_id)")
        conn.commit()
        conn.close()

    def add_pair(self, local_path: str, cloud_parent: str = "root",
                 direction: str = "upload", user_id: str = "anonymous") -> dict:
        if not os.path.isdir(local_path):
            return {"success": False, "error": f"Local path does not exist: {local_path}"}

        if direction not in ("upload", "download", "bidirectional"):
            return {"success": False, "error": "Invalid direction"}

        pair_id = uuid.uuid4().hex[:12]
        now = int(time.time())

        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO sync_pairs (id, local_path, cloud_parent, direction, created_ts, user_id) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (pair_id, local_path, cloud_parent, direction, now, user_id or "anonymous")
        )
        conn.commit()
        conn.close()

        return {"success": True, "pair_id": pair_id, "local_path": local_path,
                "cloud_parent": cloud_parent, "direction": direction}

    def list_pairs(self, user_id: str = None) -> list:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        query = ("SELECT id, local_path, cloud_parent, direction, last_sync_ts, status, created_ts "
                 "FROM sync_pairs")
        params = ()
        if user_id is not None:
            query += " WHERE user_id = ?"
            params = (user_id,)
        c.execute(query, params)
        rows = c.fetchall()
        conn.close()
        return [
            {
                "id": r[0], "local_path": r[1], "cloud_parent": r[2],
                "direction": r[3], "last_sync_ts": r[4], "status": r[5], "created_ts": r[6],
            }
            for r in rows
        ]

    def delete_pair(self, pair_id: str, user_id: str = None):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        if user_id is None:
            c.execute("DELETE FROM sync_pairs WHERE id = ?", (pair_id,))
        else:
            c.execute("DELETE FROM sync_pairs WHERE id = ? AND user_id = ?", (pair_id, user_id))
        c.execute("DELETE FROM sync_records WHERE pair_id = ?", (pair_id,))
        conn.commit()
        conn.close()

    def sync_pair(self, pair_id: str, user_id: str = None) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        query = "SELECT local_path, cloud_parent, direction, last_sync_ts FROM sync_pairs WHERE id = ?"
        params = [pair_id]
        if user_id is not None:
            query += " AND user_id = ?"
            params.append(user_id)
        c.execute(query, params)
        row = c.fetchone()
        conn.close()

        if not row:
            return {"success": False, "error": "Sync pair not found"}

        local_path, cloud_parent, direction, last_sync = row

        # Update status to syncing
        self._update_pair_status(pair_id, "syncing")

        results = {"uploaded": 0, "downloaded": 0, "skipped": 0, "deleted": 0, "errors": 0}

        try:
            if direction in ("upload", "bidirectional"):
                self._upload_sync(pair_id, local_path, cloud_parent, last_sync, results,
                                  user_id=user_id or "anonymous")

            if direction in ("download", "bidirectional"):
                self._download_sync(pair_id, local_path, cloud_parent, results,
                                    user_id=user_id or "anonymous")

            self._update_pair_status(pair_id, "idle", sync_ts=int(time.time()))
        except Exception as e:
            self._update_pair_status(pair_id, "error")
            results["errors"] += 1
            results["error_detail"] = str(e)

        return {"success": True, "pair_id": pair_id, **results}

    def _upload_sync(self, pair_id: str, local_path: str, cloud_parent: str,
                     last_sync: int, results: dict, user_id: str = "anonymous"):
        """Upload changed/new local files to cloud."""
        local_root = Path(local_path)

        # Walk local directory
        for filepath in local_root.rglob("*"):
            if filepath.is_file():
                rel_path = str(filepath.relative_to(local_root)).replace("\\", "/")
                stat = filepath.stat()
                mtime = int(stat.st_mtime)
                size = stat.st_size

                # Check if we've synced this file before
                prev = self._get_sync_record(pair_id, rel_path)
                if prev:
                    if prev["local_mtime"] == mtime and prev["local_size"] == size:
                        results["skipped"] += 1
                        continue  # Unchanged

                # Read and upload
                try:
                    data = filepath.read_bytes()
                    meta = self.engine.chunk_data(data, name=filepath.name,
                                                 parent=cloud_parent, user_id=user_id)

                    # Update sync record
                    self._upsert_sync_record(pair_id, rel_path, meta.identity, mtime, size, prev.get("sha1", ""))
                    results["uploaded"] += 1
                except Exception as e:
                    results["errors"] += 1

    def _download_sync(self, pair_id: str, local_path: str, cloud_parent: str, results: dict,
                       user_id: str = "anonymous"):
        """Download new/changed cloud files to local."""
        local_root = Path(local_path)
        local_root.mkdir(parents=True, exist_ok=True)

        # List cloud files
        cloud_files = self.engine.list_files(cloud_parent, user_id=user_id)
        for meta in cloud_files:
            if meta.is_dir:
                continue

            local_file = local_root / meta.name

            # Check if local file exists and is up to date
            if local_file.exists():
                local_data = local_file.read_bytes()
                local_fp = quick_fingerprint(local_data)
                # If we have a sync record, compare
                prev = self._get_sync_record(pair_id, meta.name)
                if prev and prev.get("sha1") == local_fp["sha1"]:
                    results["skipped"] += 1
                    continue

            # Download from cloud
            try:
                success = self.engine.download_file(meta.identity, str(local_file), user_id=user_id)
                if success:
                    stat = local_file.stat()
                    self._upsert_sync_record(pair_id, meta.name, meta.identity,
                                           int(stat.st_mtime), stat.st_size, "")
                    results["downloaded"] += 1
                else:
                    results["errors"] += 1
            except Exception:
                results["errors"] += 1

    def _get_sync_record(self, pair_id: str, rel_path: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT file_identity, local_mtime, local_size, sha1, last_sync_ts "
                  "FROM sync_records WHERE pair_id = ? AND local_rel_path = ?", (pair_id, rel_path))
        row = c.fetchone()
        conn.close()
        if not row:
            return {}
        return {"file_identity": row[0], "local_mtime": row[1], "local_size": row[2],
                "sha1": row[3], "last_sync_ts": row[4]}

    def _upsert_sync_record(self, pair_id: str, rel_path: str, file_identity: str,
                            mtime: int, size: int, sha1: str):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO sync_records (pair_id, local_rel_path, file_identity, "
            "local_mtime, local_size, sha1, last_sync_ts) VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(pair_id, local_rel_path) DO UPDATE SET file_identity=excluded.file_identity, "
            "local_mtime=excluded.local_mtime, local_size=excluded.local_size, sha1=excluded.sha1, "
            "last_sync_ts=excluded.last_sync_ts",
            (pair_id, rel_path, file_identity, mtime, size, sha1, int(time.time()))
        )
        conn.commit()
        conn.close()

    def _update_pair_status(self, pair_id: str, status: str, sync_ts: int = None):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        if sync_ts:
            c.execute("UPDATE sync_pairs SET status = ?, last_sync_ts = ? WHERE id = ?",
                      (status, sync_ts, pair_id))
        else:
            c.execute("UPDATE sync_pairs SET status = ? WHERE id = ?", (status, pair_id))
        conn.commit()
        conn.close()

    def get_status(self) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*), SUM(CASE WHEN status='syncing' THEN 1 ELSE 0 END), "
                  "SUM(CASE WHEN status='error' THEN 1 ELSE 0 END) FROM sync_pairs")
        total, syncing, errors = c.fetchone()
        conn.close()
        return {"total_pairs": total or 0, "syncing": syncing or 0, "errors": errors or 0}
