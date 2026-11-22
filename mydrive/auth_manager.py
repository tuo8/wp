"""
Auth Manager — OAuth2 + Multi-User Authentication
=================================================
Implements:
1. User registration / login with PBKDF2 password hashing
2. JWT-like access tokens (HMAC-SHA256 signed)
3. Refresh tokens with rotation
4. Device code flow (for TV/IoT clients)
5. Per-user storage quota management
6. PKCE support for OAuth2 authorize flow

Token format: base64(header).base64(payload).base64(signature)
Header: {"alg": "HS256", "typ": "JWT"}
Payload: {"user_id": "...", "username": "...", "exp": ..., "iat": ..., "scope": "..."}
"""

import os
import json
import time
import uuid
import hashlib
import hmac
import base64
import sqlite3
from db_adapter import connect_db
import secrets
from pathlib import Path
from typing import Optional, Dict


class AuthManager:
    """Manages users, tokens, and quotas."""

    def __init__(self, storage_root: str):
        self.root = Path(storage_root)
        self.db_dir = self.root / "db"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.db_dir / "auth.sqlite"
        # Secret key for signing tokens — in production, load from env
        self.secret_key = os.environ.get("MYDRIVE_SECRET", "mydrive_default_secret_change_me")
        self._init_db()

    def _init_db(self):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id TEXT PRIMARY KEY,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                password_salt TEXT NOT NULL,
                created_ts INTEGER,
                bytes_quota INTEGER DEFAULT 10737418240,
                bytes_used INTEGER DEFAULT 0
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS refresh_tokens (
                token TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_ts INTEGER,
                expires_ts INTEGER,
                FOREIGN KEY(user_id) REFERENCES users(user_id)
            )
        """)
        c.execute("""
            CREATE TABLE IF NOT EXISTS device_codes (
                device_code TEXT PRIMARY KEY,
                user_code TEXT NOT NULL,
                client_id TEXT,
                status TEXT DEFAULT 'pending',
                user_id TEXT,
                access_token TEXT,
                refresh_token TEXT,
                created_ts INTEGER,
                expires_ts INTEGER,
                interval_sec INTEGER DEFAULT 5
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_device_user_code ON device_codes(user_code)")
        c.execute("CREATE INDEX IF NOT EXISTS idx_tokens_user ON refresh_tokens(user_id)")
        # Extended profile (name / email / phone / icon / addon) — mirrors v6 User fields
        c.execute("""
            CREATE TABLE IF NOT EXISTS user_profile (
                user_id TEXT PRIMARY KEY,
                name TEXT DEFAULT '',
                email TEXT DEFAULT '',
                phone TEXT DEFAULT '',
                country_code TEXT DEFAULT '',
                icon TEXT DEFAULT '',
                addon TEXT DEFAULT '',
                last_login_ts INTEGER DEFAULT 0,
                last_login_ip TEXT DEFAULT '',
                last_login_device TEXT DEFAULT ''
            )
        """)
        # SMS verification codes (v6 PubUser.SendSmsVerifyCode)
        c.execute("""
            CREATE TABLE IF NOT EXISTS sms_codes (
                phone TEXT NOT NULL,
                code TEXT NOT NULL,
                scene INTEGER DEFAULT 0,
                created_ts INTEGER,
                expires_ts INTEGER,
                used INTEGER DEFAULT 0
            )
        """)
        c.execute("CREATE INDEX IF NOT EXISTS idx_sms_phone ON sms_codes(phone, scene)")
        conn.commit()
        conn.close()

    # ==================== PASSWORD ====================

    def _hash_password(self, password: str, salt: str) -> str:
        """Hash password with PBKDF2."""
        dk = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100000, dklen=32)
        return dk.hex()

    def _verify_password(self, password: str, salt: str, expected_hash: str) -> bool:
        actual = self._hash_password(password, salt)
        return hmac.compare_digest(actual, expected_hash)

    # ==================== USER MANAGEMENT ====================

    def register(self, username: str, password: str) -> dict:
        if len(username) < 3:
            return {"success": False, "error": "Username too short"}
        if len(password) < 6:
            return {"success": False, "error": "Password too short"}

        salt = secrets.token_hex(16)
        pw_hash = self._hash_password(password, salt)
        # 32 hex chars — same identity format the v6 API uses everywhere
        user_id = uuid.uuid4().hex

        conn = connect_db(self.db_path)
        c = conn.cursor()
        try:
            c.execute(
                "INSERT INTO users (user_id, username, password_hash, password_salt, created_ts) VALUES (?, ?, ?, ?, ?)",
                (user_id, username, pw_hash, salt, int(time.time()))
            )
            conn.commit()
        except sqlite3.IntegrityError:
            conn.close()
            return {"success": False, "error": "Username already exists"}
        conn.close()

        tokens = self._generate_tokens(user_id, username)
        return {"success": True, **tokens, "user_id": user_id, "username": username}

    def login(self, username: str, password: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id, password_hash, password_salt FROM users WHERE username = ?", (username,))
        row = c.fetchone()
        conn.close()

        if not row:
            return {"success": False, "error": "User not found"}

        user_id, pw_hash, salt = row
        if not self._verify_password(password, salt, pw_hash):
            return {"success": False, "error": "Wrong password"}

        tokens = self._generate_tokens(user_id, username)
        return {"success": True, **tokens, "user_id": user_id, "username": username}

    # ==================== TOKENS ====================

    def _generate_tokens(self, user_id: str, username: str) -> dict:
        access_token = self._make_jwt(user_id, username, expires_in=3600)
        refresh_token = secrets.token_urlsafe(32)

        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO refresh_tokens (token, user_id, created_ts, expires_ts) VALUES (?, ?, ?, ?)",
            (refresh_token, user_id, int(time.time()), int(time.time()) + 86400 * 30)
        )
        conn.commit()
        conn.close()

        return {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "token_type": "Bearer",
            "expires_in": 3600,
        }

    def _make_jwt(self, user_id: str, username: str, expires_in: int = 3600) -> str:
        header = {"alg": "HS256", "typ": "JWT"}
        now = int(time.time())
        payload = {
            "user_id": user_id,
            "username": username,
            "iat": now,
            "exp": now + expires_in,
        }
        header_b64 = base64.urlsafe_b64encode(json.dumps(header).encode()).rstrip(b"=").decode()
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        signing_input = f"{header_b64}.{payload_b64}"
        signature = hmac.new(self.secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{header_b64}.{payload_b64}.{sig_b64}"

    def verify_token(self, token: str) -> Optional[dict]:
        """Verify a JWT access token. Returns user info or None."""
        if not token or "." not in token:
            return None

        parts = token.split(".")
        if len(parts) != 3:
            return None

        header_b64, payload_b64, sig_b64 = parts
        signing_input = f"{header_b64}.{payload_b64}"
        expected_sig = hmac.new(self.secret_key.encode(), signing_input.encode(), hashlib.sha256).digest()
        expected_sig_b64 = base64.urlsafe_b64encode(expected_sig).rstrip(b"=").decode()

        if not hmac.compare_digest(sig_b64, expected_sig_b64):
            return None

        # Decode payload
        padding = "=" * ((4 - len(payload_b64) % 4) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64 + padding))

        if int(time.time()) > payload.get("exp", 0):
            return None

        return {"user_id": payload["user_id"], "username": payload["username"]}

    def refresh(self, refresh_token: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id, expires_ts FROM refresh_tokens WHERE token = ?", (refresh_token,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "Invalid refresh token"}

        user_id, expires_ts = row
        if int(time.time()) > expires_ts:
            c.execute("DELETE FROM refresh_tokens WHERE token = ?", (refresh_token,))
            conn.commit()
            conn.close()
            return {"success": False, "error": "Refresh token expired"}

        c.execute("SELECT username FROM users WHERE user_id = ?", (user_id,))
        user_row = c.fetchone()
        conn.close()
        if not user_row:
            return {"success": False, "error": "User not found"}

        # Delete old refresh token (rotation)
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM refresh_tokens WHERE token = ?", (refresh_token,))
        conn.commit()
        conn.close()

        tokens = self._generate_tokens(user_id, user_row[0])
        return {"success": True, **tokens}

    # ==================== DEVICE CODE FLOW ====================

    def generate_device_code(self) -> dict:
        device_code = secrets.token_urlsafe(48)
        user_code = self._generate_user_code()
        now = int(time.time())

        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO device_codes (device_code, user_code, client_id, status, created_ts, expires_ts) "
            "VALUES (?, ?, ?, 'pending', ?, ?)",
            (device_code, user_code, "mydrive-client", now, now + 900)
        )
        conn.commit()
        conn.close()

        return {
            "device_code": device_code,
            "user_code": user_code,
            "verification_url": "http://127.0.0.1:8765/#device-authorize",
            "expires_in": 900,
            "interval": 5,
        }

    def _generate_user_code(self) -> str:
        import random
        import string
        chars = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
        return "-".join("".join(random.choices(chars, k=4)) for _ in range(2))

    def authorize_device(self, device_code: str, username: str, password: str) -> dict:
        login_result = self.login(username, password)
        if not login_result.get("success"):
            return login_result

        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT expires_ts FROM device_codes WHERE device_code = ? AND status = 'pending'", (device_code,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "Device code not found or already used"}
        if int(time.time()) > row[0]:
            conn.close()
            return {"success": False, "error": "Device code expired"}

        c.execute(
            "UPDATE device_codes SET status = 'authorized', user_id = ?, access_token = ?, refresh_token = ? "
            "WHERE device_code = ?",
            (login_result["user_id"], login_result["access_token"], login_result["refresh_token"], device_code)
        )
        conn.commit()
        conn.close()
        return {"success": True, "message": "Device authorized"}

    def poll_device_code(self, device_code: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT status, access_token, refresh_token, expires_ts FROM device_codes WHERE device_code = ?",
                  (device_code,))
        row = c.fetchone()
        conn.close()

        if not row:
            return {"success": False, "error": "invalid_device_code"}

        status, access_token, refresh_token, expires_ts = row
        if int(time.time()) > expires_ts:
            return {"success": False, "error": "expired_token"}

        if status == "authorized":
            return {
                "success": True,
                "access_token": access_token,
                "refresh_token": refresh_token,
                "token_type": "Bearer",
                "expires_in": 3600,
            }
        return {"success": False, "error": "authorization_pending"}

    # ==================== QUOTA ====================

    def get_user(self, user_id: str) -> Optional[Dict]:
        """Public user record (no password material)."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT user_id, username, created_ts, bytes_quota, bytes_used FROM users WHERE user_id = ?",
            (user_id,),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        return {
            "user_id": row[0], "username": row[1], "created_ts": row[2] or 0,
            "bytes_quota": row[3] or 0, "bytes_used": row[4] or 0,
        }

    def get_quota(self, user_id: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT bytes_quota, bytes_used FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        if not row:
            return {"quota": 0, "used": 0}
        return {"quota": row[0], "used": row[1]}

    def update_usage(self, user_id: str, delta_bytes: int):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("UPDATE users SET bytes_used = bytes_used + ? WHERE user_id = ?", (delta_bytes, user_id))
        conn.commit()
        conn.close()

    def reserve_usage(self, user_id: str, delta_bytes: int, quota_limit: int = 0) -> bool:
        """Atomically reserve logical storage quota to prevent concurrent over-allocation."""
        amount = max(0, int(delta_bytes or 0))
        limit = max(0, int(quota_limit or 0))
        conn = connect_db(self.db_path)
        c = conn.cursor()
        if limit:
            c.execute(
                "UPDATE users SET bytes_used = bytes_used + ? "
                "WHERE user_id = ? AND bytes_used + ? <= ?",
                (amount, user_id, amount, limit),
            )
        else:
            c.execute("UPDATE users SET bytes_used = bytes_used + ? WHERE user_id = ?",
                      (amount, user_id))
        reserved = c.rowcount == 1
        conn.commit()
        conn.close()
        return reserved

    # ==================== ACCOUNT LIFECYCLE (v6 PubUser) ====================

    def change_password(self, user_id: str, old_password: str, new_password: str) -> dict:
        """Change password only after verifying the current password; revoke sessions."""
        if not old_password:
            return {"success": False, "error": "Current password required"}
        if len(new_password or "") < 10:
            return {"success": False, "error": "Password must be at least 10 characters"}
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT username, password_hash, password_salt FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "User not found"}
        username, pw_hash, salt = row
        if not self._verify_password(old_password, salt, pw_hash):
            conn.close()
            return {"success": False, "error": "Wrong password"}
        new_salt = secrets.token_hex(16)
        new_hash = self._hash_password(new_password, new_salt)
        c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE user_id = ?",
                  (new_hash, new_salt, user_id))
        c.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return {"success": True, "user_id": user_id, "username": username}

    def reset_password(self, username: str, new_password: str, sms_code: str = "") -> dict:
        """Reset password only with a one-time code sent to the account's recovery phone."""
        if not sms_code:
            return {"success": False, "error": "SMS verification code required"}
        if len(new_password or "") < 10:
            return {"success": False, "error": "Password must be at least 10 characters"}
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id FROM users WHERE username = ?", (username,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "User not found"}
        user_id = row[0]
        c.execute("SELECT phone FROM user_profile WHERE user_id = ?", (user_id,))
        profile = c.fetchone()
        phone = (profile[0] if profile and profile[0] else "").strip()
        normalized_username = username.replace("+", "") if username else ""
        if not phone and normalized_username.isdigit() and 7 <= len(normalized_username) <= 20:
            phone = username
        if not phone:
            conn.close()
            return {"success": False, "error": "No recovery phone is configured for this account"}
        c.execute(
            "SELECT code, expires_ts, used FROM sms_codes WHERE phone = ? ORDER BY created_ts DESC LIMIT 1",
            (phone,))
        sms = c.fetchone()
        if not sms or sms[2] or not hmac.compare_digest(str(sms[0]), str(sms_code).strip()) or int(time.time()) > sms[1]:
            conn.close()
            return {"success": False, "error": "Invalid or expired SMS code"}
        c.execute("UPDATE sms_codes SET used = 1 WHERE phone = ? AND code = ? AND used = 0",
                  (phone, sms[0]))
        new_salt = secrets.token_hex(16)
        new_hash = self._hash_password(new_password, new_salt)
        c.execute("UPDATE users SET password_hash = ?, password_salt = ? WHERE user_id = ?",
                  (new_hash, new_salt, user_id))
        c.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return {"success": True, "user_id": user_id, "username": username}

    def delete_user(self, user_id: str, password: str = "") -> dict:
        """Delete an account only after password confirmation."""
        if not password:
            return {"success": False, "error": "Password required"}
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT username, password_hash, password_salt FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "User not found"}
        if not self._verify_password(password, row[2], row[1]):
            conn.close()
            return {"success": False, "error": "Wrong password"}
        c.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
        c.execute("DELETE FROM user_profile WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return {"success": True, "user_id": user_id, "username": row[0]}

    def logoff(self, refresh_token: str = "", user_id: str = "") -> dict:
        """v6 ``PubUser.Logoff`` — revoke one refresh token (or all of a user's)."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        if refresh_token:
            c.execute("DELETE FROM refresh_tokens WHERE token = ?", (refresh_token,))
        elif user_id:
            c.execute("DELETE FROM refresh_tokens WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return {"success": True}

    def get_profile(self, user_id: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT name, email, phone, country_code, icon, addon, last_login_ts, last_login_ip, "
            "last_login_device FROM user_profile WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        conn.close()
        keys = ["name", "email", "phone", "country_code", "icon", "addon",
                "last_login_ts", "last_login_ip", "last_login_device"]
        if not row:
            return {k: (0 if "ts" in k else "") for k in keys}
        return dict(zip(keys, row))

    def update_profile(self, user_id: str, **fields) -> dict:
        allowed = ["name", "email", "phone", "country_code", "icon", "addon"]
        patch = {k: v for k, v in (fields or {}).items() if k in allowed and v is not None}
        self.get_profile(user_id)  # ensure row exists on read path
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_profile (user_id) VALUES (?)", (user_id,))
        for k, v in patch.items():
            c.execute(f"UPDATE user_profile SET {k} = ? WHERE user_id = ?", (str(v), user_id))
        conn.commit()
        conn.close()
        return {"success": True, **patch}

    def touch_login(self, user_id: str, ip: str = "", device: str = "") -> None:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("INSERT OR IGNORE INTO user_profile (user_id) VALUES (?)", (user_id,))
        c.execute(
            "UPDATE user_profile SET last_login_ts = ?, last_login_ip = ?, last_login_device = ? "
            "WHERE user_id = ?", (int(time.time() * 1000), ip, device, user_id))
        conn.commit()
        conn.close()

    # ==================== SMS VERIFY CODE ====================

    def send_sms_code(self, phone: str, scene: int = 0, ttl: int = 300) -> dict:
        """Issue a verification code only in explicit development mode.

        A real SMS provider must be integrated before production SMS flows are enabled.
        Never return verification codes from a production API response.
        """
        phone = (phone or "").strip()
        if not phone:
            return {"success": False, "error": "phone required"}
        if os.environ.get("MYDRIVE_SMS_DEBUG", "0").strip().lower() not in ("1", "true", "yes"):
            return {"success": False, "error": "SMS provider is not configured"}
        code = f"{secrets.randbelow(1000000):06d}"
        now = int(time.time())
        expires = now + max(60, min(int(ttl), 600))
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("DELETE FROM sms_codes WHERE phone = ? AND used = 1", (phone,))
        c.execute(
            "INSERT INTO sms_codes (phone, code, scene, created_ts, expires_ts, used) VALUES (?,?,?,?,?,0)",
            (phone, code, scene, now, expires))
        conn.commit()
        conn.close()
        return {"success": True, "code": code, "expire_ts": expires}

    def verify_sms_code(self, phone: str, code: str) -> bool:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT code, expires_ts, used FROM sms_codes WHERE phone = ? ORDER BY created_ts DESC LIMIT 1",
            (phone,))
        row = c.fetchone()
        if not row or row[2] or row[0] != (code or "").strip() or int(time.time()) > row[1]:
            conn.close()
            return False
        c.execute("UPDATE sms_codes SET used = 1 WHERE phone = ?", (phone,))
        conn.commit()
        conn.close()
        return True

    # ==================== VALIDATION / QUOTA EXTEND ====================

    def user_exists(self, username: str) -> bool:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT 1 FROM users WHERE username = ?", (username,))
        row = c.fetchone()
        conn.close()
        return row is not None

    def extend_quota(self, user_id: str, disk_bytes: int = 0, disk_files: int = 0) -> dict:
        """v6 ``PubUser.ExtendQuota`` — top up the storage allowance."""
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT bytes_quota FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row:
            conn.close()
            return {"success": False, "error": "User not found"}
        if disk_bytes:
            c.execute("UPDATE users SET bytes_quota = bytes_quota + ? WHERE user_id = ?",
                      (int(disk_bytes), user_id))
        conn.commit()
        c.execute("SELECT bytes_quota, bytes_used FROM users WHERE user_id = ?", (user_id,))
        quota, used = c.fetchone()
        conn.close()
        return {"success": True, "bytes_quota": quota, "bytes_used": used, "disk_files": disk_files}

