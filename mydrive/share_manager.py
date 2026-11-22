"""
Share Manager — 文件分享 + 工单系统
====================================
1. 创建分享链接（可设密码、过期时间、下载次数限制）
2. 访问分享文件（密码校验、过期检查、次数检查）
3. 工单系统（用户提交问题，管理员查看）
"""

import os
import json
import time
import uuid
import hashlib
import hmac
import secrets
import sqlite3
from pathlib import Path
from typing import Optional

from storage_engine import StorageEngine


class ShareManager:
    @staticmethod
    def _hash_share_password(password: str) -> str:
        salt = secrets.token_bytes(16)
        n, r, p = 2 ** 14, 8, 1
        digest = hashlib.scrypt((password or "").encode("utf-8"), salt=salt,
                                n=n, r=r, p=p, dklen=32)
        return f"scrypt${n}${r}${p}${salt.hex()}${digest.hex()}"

    @staticmethod
    def _verify_share_password(password: str, stored_hash: str):
        """Verify current scrypt hashes and transparently upgrade legacy SHA-256 hashes."""
        stored = stored_hash or ""
        if stored.startswith("scrypt$"):
            try:
                _, n, r, p, salt_hex, digest_hex = stored.split("$", 5)
                expected = bytes.fromhex(digest_hex)
                actual = hashlib.scrypt((password or "").encode("utf-8"),
                                        salt=bytes.fromhex(salt_hex), n=int(n),
                                        r=int(r), p=int(p), dklen=len(expected))
                return hmac.compare_digest(actual, expected), None
            except (ValueError, TypeError):
                return False, None
        legacy = hashlib.sha256((password or "").encode("utf-8")).hexdigest()
        if stored and hmac.compare_digest(legacy, stored):
            return True, ShareManager._hash_share_password(password)
        return False, None

    def __init__(self, engine: StorageEngine):
        self.engine = engine
        self.db_path = engine.db_dir / "share.sqlite"
        self._init_db()

    def _init_db(self):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS shares (
                share_id TEXT PRIMARY KEY,
                file_identity TEXT NOT NULL,
                password_hash TEXT,
                has_password INTEGER DEFAULT 0,
                expire_ts INTEGER DEFAULT 0,
                download_limit INTEGER DEFAULT 0,
                download_count INTEGER DEFAULT 0,
                created_ts INTEGER,
                creator_user_id TEXT,
                like_count INTEGER DEFAULT 0,
                dislike_count INTEGER DEFAULT 0,
                save_count INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_shares_file ON shares(file_identity)")

        # v6 FileShare carries like/dislike/save counters
        share_cols = {row[1] for row in c.execute("PRAGMA table_info(shares)")}
        for col in ("like_count", "dislike_count", "save_count"):
            if col not in share_cols:
                c.execute(f"ALTER TABLE shares ADD COLUMN {col} INTEGER DEFAULT 0")

        c.execute("""
            CREATE TABLE IF NOT EXISTS complaints (
                identity TEXT PRIMARY KEY,
                share_id TEXT NOT NULL,
                user_id TEXT,
                share_owner TEXT,
                content TEXT,
                status INTEGER DEFAULT 0,
                flag INTEGER DEFAULT 0,
                created_ts INTEGER,
                updated_ts INTEGER
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_complaints_share ON complaints(share_id)")
        c.execute("""
            CREATE TABLE IF NOT EXISTS tickets (
                ticket_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                subject TEXT,
                body TEXT,
                priority TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'open',
                created_ts INTEGER,
                updated_ts INTEGER,
                reply TEXT
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_tickets_user ON tickets(user_id)")
        conn.commit()
        conn.close()

    def create_share(self, file_identity: str, password: str = "",
                     expire_hours: int = 0, download_limit: int = 0,
                     user_id: str = "anonymous") -> dict:
        # Only the owner may publish a share link for a file.
        meta = self.engine.get_file_meta(file_identity, user_id=user_id)
        if not meta:
            return {"success": False, "error": "File not found"}

        share_id = uuid.uuid4().hex[:12]
        pw_hash = ""
        has_password = 0
        if password:
            pw_hash = self._hash_share_password(password)
            has_password = 1

        expire_ts = int(time.time()) + expire_hours * 3600 if expire_hours else 0

        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute(
            "INSERT INTO shares (share_id, file_identity, password_hash, has_password, "
            "expire_ts, download_limit, download_count, created_ts, creator_user_id) "
            "VALUES (?, ?, ?, ?, ?, ?, 0, ?, ?)",
            (share_id, file_identity, pw_hash, has_password, expire_ts, download_limit,
             int(time.time()), user_id or "anonymous")
        )
        conn.commit()
        conn.close()

        return {
            "success": True,
            "share_id": share_id,
            "url": f"/api/share/{share_id}",
            "has_password": bool(has_password),
            "expire_ts": expire_ts,
        }

    def get_share(self, share_id: str, password: str = "") -> dict:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT file_identity, password_hash, has_password, expire_ts, "
                  "download_limit, download_count, like_count, dislike_count, save_count, "
                  "creator_user_id, created_ts "
                  "FROM shares WHERE share_id = ?", (share_id,))
        row = c.fetchone()
        conn.close()

        if not row:
            return {"success": False, "error": "Share not found"}

        (file_identity, pw_hash, has_pw, expire_ts, dl_limit, dl_count,
         like_count, dislike_count, save_count, owner, created) = row

        if expire_ts and int(time.time()) > expire_ts:
            return {"success": False, "error": "Share expired"}

        if dl_limit and dl_count >= dl_limit:
            return {"success": False, "error": "Download limit reached"}

        if has_pw:
            if not password:
                return {"success": False, "error": "Password required"}
            valid_password, upgraded_hash = self._verify_share_password(password, pw_hash)
            if not valid_password:
                return {"success": False, "error": "Wrong password"}
            if upgraded_hash:
                upgrade_conn = sqlite3.connect(str(self.db_path))
                upgrade_conn.execute(
                    "UPDATE shares SET password_hash = ? WHERE share_id = ? AND password_hash = ?",
                    (upgraded_hash, share_id, pw_hash))
                upgrade_conn.commit()
                upgrade_conn.close()

        meta = self.engine.get_file_meta(file_identity)
        if not meta:
            return {"success": False, "error": "File not found"}

        return {
            "success": True,
            "share_id": share_id,
            "file_identity": file_identity,
            "name": meta.name,
            "size": meta.size,
            "mime_type": meta.mime_type,
            "type": meta.type,
            "create_ts": created,
            "expire_ts": expire_ts,
            "has_password": bool(has_pw),
            "like": int(like_count or 0),
            "dislike": int(dislike_count or 0),
            "save_count": int(save_count or 0),
            "user_identity": owner or "",
        }

    def get_record(self, share_id: str) -> Optional[dict]:
        """Raw share row (no password gate) — for owners and protocol replies."""
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT share_id, file_identity, has_password, expire_ts, download_limit, "
                  "download_count, like_count, dislike_count, save_count, created_ts, "
                  "creator_user_id FROM shares WHERE share_id = ?", (share_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "share_id": row[0], "file_identity": row[1], "has_password": bool(row[2]),
            "expire_ts": row[3] or 0, "download_limit": row[4] or 0,
            "download_count": row[5] or 0, "like": row[6] or 0,
            "dislike": row[7] or 0, "save_count": row[8] or 0,
            "create_ts": row[9] or 0, "user_identity": row[10] or "",
        }

    def react(self, share_id: str, action: str = "like") -> dict:
        """Like / dislike a share — mirrors ``PubFileShare.Like`` / ``Dislike``."""
        column = "dislike_count" if action == "dislike" else "like_count"
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute(f"UPDATE shares SET {column} = {column} + 1 WHERE share_id = ?", (share_id,))
        if c.rowcount == 0:
            conn.close()
            return {"success": False, "error": "Share not found"}
        conn.commit()
        c.execute("SELECT like_count, dislike_count FROM shares WHERE share_id = ?", (share_id,))
        like_count, dislike_count = c.fetchone()
        conn.close()
        return {"success": True, "like": int(like_count or 0),
                "dislike": int(dislike_count or 0)}

    def add_complaint(self, share_id: str, user_id: str, content: str) -> dict:
        """File a complaint against a share (``ComplaintShare``)."""
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT creator_user_id FROM shares WHERE share_id = ?", (share_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "Share not found"}
        now = int(time.time())
        identity = uuid.uuid4().hex
        c.execute(
            "INSERT INTO complaints (identity, share_id, user_id, share_owner, content, "
            "status, flag, created_ts, updated_ts) VALUES (?, ?, ?, ?, ?, 0, 0, ?, ?)",
            (identity, share_id, user_id, row[0], content, now, now),
        )
        conn.commit()
        conn.close()
        return {"success": True, "identity": identity, "share_id": share_id,
                "file_share_identity": share_id, "file_share_user_identity": row[0],
                "content": content, "status": 0, "create_ts": now}

    def list_complaints(self, user_id: str = None) -> list:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        if user_id is None:
            c.execute("SELECT identity, share_id, user_id, share_owner, content, status, "
                      "created_ts FROM complaints ORDER BY created_ts DESC")
        else:
            c.execute("SELECT identity, share_id, user_id, share_owner, content, status, "
                      "created_ts FROM complaints WHERE user_id = ? ORDER BY created_ts DESC",
                      (user_id,))
        rows = c.fetchall()
        conn.close()
        return [
            {"identity": r[0], "file_share_identity": r[1], "user_identity": r[2],
             "file_share_user_identity": r[3], "content": r[4], "status": r[5],
             "create_ts": r[6]}
            for r in rows
        ]

    def delete_complaints(self, identities: list, user_id: str = None) -> int:
        """Bulk-delete complaints by identity (``PubFileShare.DeleteComplaint``)."""
        ids = [i for i in (identities or []) if i]
        if not ids:
            return 0
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        marks = ",".join("?" * len(ids))
        if user_id is None:
            c.execute(f"DELETE FROM complaints WHERE identity IN ({marks})", ids)
        else:
            c.execute(f"DELETE FROM complaints WHERE identity IN ({marks}) AND user_id = ?",
                      ids + [user_id])
        count = c.rowcount if c.rowcount and c.rowcount > 0 else 0
        conn.commit()
        conn.close()
        return count

    def save_share(self, share_id: str, user_id: str, password: str = "",
                   parent: str = "root", name: str = "") -> dict:
        """Save a shared file into the caller's drive (``PubFileShare.Save``)."""
        info = self.get_share(share_id, password)
        if not info.get("success"):
            return info

        meta = self.engine.get_file_meta(info["file_identity"])
        if not meta:
            return {"success": False, "error": "File not found"}

        target_name = name or meta.name
        clone = self.engine.clone_file(meta, target_name, parent=parent,
                                       user_id=user_id or "anonymous")
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("UPDATE shares SET save_count = save_count + 1 WHERE share_id = ?", (share_id,))
        conn.commit()
        conn.close()
        return {"success": True, "file": clone, "save_count": int(info.get("save_count", 0)) + 1}

    def increment_download(self, share_id: str):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("UPDATE shares SET download_count = download_count + 1 WHERE share_id = ?", (share_id,))
        conn.commit()
        conn.close()

    def list_shares(self, user_id: str = None) -> list:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        if user_id is None:
            c.execute("SELECT share_id, file_identity, has_password, expire_ts, download_limit, "
                      "download_count, created_ts FROM shares")
        else:
            c.execute("SELECT share_id, file_identity, has_password, expire_ts, download_limit, "
                      "download_count, created_ts FROM shares WHERE creator_user_id = ?", (user_id,))
        rows = c.fetchall()
        conn.close()

        results = []
        for row in rows:
            sid, fid, has_pw, exp, dl_lim, dl_cnt, created = row
            meta = self.engine.get_file_meta(fid)
            results.append({
                "share_id": sid,
                "file_name": meta.name if meta else "unknown",
                "file_identity": fid,
                "has_password": bool(has_pw),
                "expire_ts": exp,
                "download_limit": dl_lim,
                "download_count": dl_cnt,
                "created_ts": created,
                "url": f"/api/share/{sid}",
            })
        return results

    def delete_share(self, share_id: str, user_id: str = None):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        if user_id is None:
            c.execute("DELETE FROM shares WHERE share_id = ?", (share_id,))
        else:
            c.execute("DELETE FROM shares WHERE share_id = ? AND creator_user_id = ?",
                      (share_id, user_id))
        conn.commit()
        conn.close()

    # ==================== TICKETS ====================

    def create_ticket(self, user_id: str, subject: str, body: str, priority: str = "normal") -> dict:
        ticket_id = uuid.uuid4().hex[:12]
        now = int(time.time())
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute(
            "INSERT INTO tickets (ticket_id, user_id, subject, body, priority, status, created_ts, updated_ts) "
            "VALUES (?, ?, ?, ?, ?, 'open', ?, ?)",
            (ticket_id, user_id, subject, body, priority, now, now)
        )
        conn.commit()
        conn.close()
        return {"success": True, "ticket_id": ticket_id, "status": "open"}

    def list_tickets(self, user_id: str) -> list:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT ticket_id, subject, priority, status, created_ts, updated_ts, reply "
                  "FROM tickets WHERE user_id = ? ORDER BY created_ts DESC", (user_id,))
        rows = c.fetchall()
        conn.close()
        return [
            {
                "ticket_id": r[0], "subject": r[1], "priority": r[2],
                "status": r[3], "created_ts": r[4], "updated_ts": r[5],
                "has_reply": bool(r[6]),
            }
            for r in rows
        ]

    def get_ticket(self, ticket_id: str) -> Optional[dict]:
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("SELECT ticket_id, user_id, subject, body, priority, status, created_ts, updated_ts, reply "
                  "FROM tickets WHERE ticket_id = ?", (ticket_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "ticket_id": row[0], "user_id": row[1], "subject": row[2],
            "body": row[3], "priority": row[4], "status": row[5],
            "created_ts": row[6], "updated_ts": row[7], "reply": row[8],
        }

    def reply_ticket(self, ticket_id: str, reply: str):
        conn = sqlite3.connect(str(self.db_path))
        c = conn.cursor()
        c.execute("UPDATE tickets SET reply = ?, status = 'answered', updated_ts = ? WHERE ticket_id = ?",
                  (reply, int(time.time()), ticket_id))
        conn.commit()
        conn.close()
