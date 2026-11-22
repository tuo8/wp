"""
Commercial Membership, Quota, Redemption Code & Moderation Manager
==================================================================
Brings PikPak / HalalCloud commercial operations to MyDrive:
- Tiered plans: `free`, `vip`, `svip` with storage quota, daily offline quota,
  download speed cap, and feature gates.
- Redemption codes (兑换码): generate & redeem codes to upgrade accounts.
- Content moderation (CID blocklist): block/unblock violating CIDs across shares
  and downloads with audit reason.
- SQLite WAL helper to enable high-concurrency reads/writes across all DBs.
"""

from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import sqlite3
from db_adapter import connect_db, is_postgres_enabled
import time
from pathlib import Path
from typing import Dict, List, Optional

PLANS: Dict[str, dict] = {
    "free": {
        "name": "免费版",
        "storage_quota_bytes": 10 * 1024 * 1024 * 1024,       # 10 GB
        "daily_offline_quota": 5,
        "download_speed_bps": 2 * 1024 * 1024,                # 2 MB/s
        "hls_transcode": True,
        "max_quality": "720p",
    },
    "vip": {
        "name": "VIP 会员",
        "storage_quota_bytes": 1024 * 1024 * 1024 * 1024,     # 1 TB
        "daily_offline_quota": 100,
        "download_speed_bps": 0,                              # unlimited
        "hls_transcode": True,
        "max_quality": "1080p",
    },
    "svip": {
        "name": "SVIP 超级会员",
        "storage_quota_bytes": 10 * 1024 * 1024 * 1024 * 1024,  # 10 TB
        "daily_offline_quota": 10000,
        "download_speed_bps": 0,                                # unlimited
        "hls_transcode": True,
        "max_quality": "original",
    },
}


def enable_sqlite_wal(db_path: Path):
    """Enable WAL mode & busy timeout for better multi-user concurrency."""
    if is_postgres_enabled() or not db_path.exists():
        return
    try:
        conn = sqlite3.connect(str(db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.close()
    except Exception:
        pass


class CommercialManager:
    """Manages VIP tiers, redemption codes, and CID content moderation."""

    def __init__(self, storage_root: str):
        self.db_path = Path(storage_root) / "db" / "commercial.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        enable_sqlite_wal(self.db_path)

    def _conn(self) -> sqlite3.Connection:
        conn = connect_db(self.db_path, timeout=5.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._conn() as c:
            c.execute("""
                CREATE TABLE IF NOT EXISTS user_memberships (
                    user_id TEXT PRIMARY KEY,
                    plan TEXT NOT NULL DEFAULT 'free',
                    expire_ts INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL DEFAULT 0
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS redeem_codes (
                    code TEXT PRIMARY KEY,
                    plan TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    used_by TEXT DEFAULT '',
                    used_at INTEGER DEFAULT 0,
                    created_at INTEGER NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS blocked_cids (
                    cid TEXT PRIMARY KEY,
                    reason TEXT DEFAULT '',
                    blocked_at INTEGER NOT NULL
                )
            """)
            c.execute("""
                CREATE TABLE IF NOT EXISTS payment_orders (
                    order_no TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    plan TEXT NOT NULL,
                    duration_days INTEGER NOT NULL,
                    amount_cents INTEGER NOT NULL,
                    currency TEXT NOT NULL DEFAULT 'CNY',
                    status TEXT NOT NULL DEFAULT 'pending',
                    provider TEXT DEFAULT 'generic',
                    created_at INTEGER NOT NULL,
                    paid_at INTEGER DEFAULT 0
                )
            """)

    def get_membership(self, user_id: str) -> dict:
        now = int(time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT plan, expire_ts FROM user_memberships WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        plan_key = "free"
        expire_ts = 0
        if row:
            if row["plan"] in PLANS and (row["expire_ts"] == 0 or row["expire_ts"] > now):
                plan_key = row["plan"]
                expire_ts = int(row["expire_ts"])
        spec = PLANS[plan_key]
        return {
            "user_id": user_id,
            "plan": plan_key,
            "plan_name": spec["name"],
            "expire_ts": expire_ts,
            "storage_quota_bytes": spec["storage_quota_bytes"],
            "daily_offline_quota": spec["daily_offline_quota"],
            "download_speed_bps": spec["download_speed_bps"],
            "max_quality": spec["max_quality"],
            "available_plans": PLANS,
        }

    def _set_membership_in_transaction(self, c, user_id: str, plan: str, duration_days: int, now: int):
        duration_seconds = max(1, int(duration_days)) * 86400
        expire_ts = 0 if plan == "free" else now + duration_seconds
        c.execute(
            """INSERT INTO user_memberships (user_id, plan, expire_ts, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(user_id) DO UPDATE SET
                   plan=excluded.plan,
                   expire_ts=CASE
                       WHEN user_memberships.plan=excluded.plan AND user_memberships.expire_ts=0 THEN 0
                       WHEN user_memberships.plan=excluded.plan AND user_memberships.expire_ts>excluded.updated_at
                           THEN user_memberships.expire_ts + ?
                       ELSE excluded.expire_ts
                   END,
                   updated_at=excluded.updated_at""",
            (user_id, plan, expire_ts, now, duration_seconds),
        )

    def set_membership(self, user_id: str, plan: str, duration_days: int = 30) -> dict:
        if plan not in PLANS:
            raise ValueError(f"Unknown plan: {plan}")
        now = int(time.time())
        with self._conn() as c:
            self._set_membership_in_transaction(c, user_id, plan, duration_days, now)
        return self.get_membership(user_id)

    def generate_codes(self, plan: str = "vip", duration_days: int = 30, count: int = 1) -> List[dict]:
        if plan not in ("vip", "svip"):
            raise ValueError("Plan must be vip or svip")
        count = max(1, min(50, int(count)))
        now = int(time.time())
        out = []
        with self._conn() as c:
            for _ in range(count):
                code = f"{plan.upper()}-{secrets.token_hex(4).upper()}-{secrets.token_hex(4).upper()}"
                c.execute(
                    "INSERT INTO redeem_codes (code, plan, duration_days, created_at) VALUES (?, ?, ?, ?)",
                    (code, plan, int(duration_days), now),
                )
                out.append({"code": code, "plan": plan, "duration_days": int(duration_days)})
        return out

    def redeem_code(self, user_id: str, code: str) -> dict:
        code = (code or "").strip().upper()
        if not code:
            raise ValueError("兑换码不能为空")
        now = int(time.time())
        with self._conn() as c:
            row = c.execute(
                "SELECT code, plan, duration_days, used_by FROM redeem_codes WHERE code = ?",
                (code,),
            ).fetchone()
            if not row:
                raise ValueError("无效的兑换码")
            if row["used_by"]:
                raise ValueError("该兑换码已被使用")
            claim = c.execute(
                "UPDATE redeem_codes SET used_by = ?, used_at = ? "
                "WHERE code = ? AND (used_by = '' OR used_by IS NULL)",
                (user_id, now, code),
            )
            if claim.rowcount != 1:
                raise ValueError("该兑换码已被使用")
            self._set_membership_in_transaction(c, user_id, row["plan"], int(row["duration_days"]), now)
        return self.get_membership(user_id)

    # ==================== CONTENT MODERATION ====================

    def block_cid(self, cid: str, reason: str = "violation") -> dict:
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO blocked_cids (cid, reason, blocked_at) VALUES (?, ?, ?) "
                "ON CONFLICT(cid) DO UPDATE SET reason=excluded.reason, blocked_at=excluded.blocked_at",
                (cid, reason, now),
            )
        return {"cid": cid, "blocked": True, "reason": reason, "blocked_at": now}

    def unblock_cid(self, cid: str) -> dict:
        with self._conn() as c:
            c.execute("DELETE FROM blocked_cids WHERE cid = ?", (cid,))
        return {"cid": cid, "blocked": False}

    def is_cid_blocked(self, cid: str) -> bool:
        if not cid:
            return False
        with self._conn() as c:
            row = c.execute("SELECT 1 FROM blocked_cids WHERE cid = ?", (cid,)).fetchone()
        return row is not None

    def list_blocked_cids(self) -> List[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT cid, reason, blocked_at FROM blocked_cids ORDER BY blocked_at DESC").fetchall()
        return [dict(r) for r in rows]

    # ==================== PAYMENT ORDERS & WEBHOOK ====================

    PLAN_PRICES_CENTS = {
        "vip": 1990,    # ¥19.90 / 30 days
        "svip": 4990,   # ¥49.90 / 30 days
    }

    def create_order(self, user_id: str, plan: str = "vip", duration_days: int = 30, provider: str = "generic") -> dict:
        if plan not in self.PLAN_PRICES_CENTS:
            raise ValueError("Plan must be vip or svip")
        months = max(1, round(int(duration_days) / 30))
        amount_cents = self.PLAN_PRICES_CENTS[plan] * months
        order_no = f"ORD-{int(time.time())}-{secrets.token_hex(4).upper()}"
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                "INSERT INTO payment_orders (order_no, user_id, plan, duration_days, amount_cents, currency, status, provider, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'CNY', 'pending', ?, ?)",
                (order_no, user_id, plan, int(duration_days), amount_cents, provider, now),
            )
        return {
            "order_no": order_no,
            "user_id": user_id,
            "plan": plan,
            "duration_days": int(duration_days),
            "amount_cents": amount_cents,
            "currency": "CNY",
            "status": "pending",
            "provider": provider,
            "created_at": now,
        }

    def verify_webhook_signature(self, payload_bytes: bytes, signature: str) -> bool:
        secret = os.environ.get("PAYMENT_WEBHOOK_SECRET", "").strip()
        runtime_mode = os.environ.get("MYDRIVE_ENV", "development").strip().lower()
        if not secret or (runtime_mode in ("prod", "production") and len(secret) < 32):
            return False
        expected = hmac.new(secret.encode("utf-8"), payload_bytes, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, (signature or "").strip().lower())

    def mark_order_paid(self, order_no: str) -> dict:
        now = int(time.time())
        with self._conn() as c:
            row = c.execute("SELECT * FROM payment_orders WHERE order_no = ?", (order_no,)).fetchone()
            if not row:
                raise ValueError("Order not found")
            if row["status"] == "paid":
                return dict(row)
            if row["status"] != "pending":
                raise ValueError("Order is not payable")
            claim = c.execute(
                "UPDATE payment_orders SET status = 'paid', paid_at = ? "
                "WHERE order_no = ? AND status = 'pending'",
                (now, order_no),
            )
            if claim.rowcount != 1:
                current = c.execute("SELECT * FROM payment_orders WHERE order_no = ?", (order_no,)).fetchone()
                if current and current["status"] == "paid":
                    return dict(current)
                raise ValueError("Order state changed; payment was not applied")
            self._set_membership_in_transaction(
                c, row["user_id"], row["plan"], int(row["duration_days"]), now)
            result = dict(row)
            result["status"] = "paid"
            result["paid_at"] = now
        return {**result, "membership": self.get_membership(row["user_id"])}

    def list_orders(self, user_id: Optional[str] = None, limit: int = 50) -> List[dict]:
        with self._conn() as c:
            if user_id:
                rows = c.execute(
                    "SELECT * FROM payment_orders WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                    (user_id, int(limit)),
                ).fetchall()
            else:
                rows = c.execute(
                    "SELECT * FROM payment_orders ORDER BY created_at DESC LIMIT ?",
                    (int(limit),),
                ).fetchall()
        return [dict(r) for r in rows]

    def revenue_summary(self) -> dict:
        with self._conn() as c:
            row = c.execute(
                "SELECT COUNT(*), COALESCE(SUM(amount_cents), 0) FROM payment_orders WHERE status = 'paid'"
            ).fetchone()
            vip_cnt = c.execute(
                "SELECT COUNT(*) FROM user_memberships WHERE plan IN ('vip','svip') AND (expire_ts=0 OR expire_ts>?)",
                (int(time.time()),),
            ).fetchone()[0]
        return {
            "paid_orders": int(row[0] or 0),
            "total_revenue_cents": int(row[1] or 0),
            "active_vip_users": int(vip_cnt or 0),
        }
