"""
PikPak-style Telegram Bot Adapter
=================================
Lets users forward magnet links, HTTP/HTTPS download URLs, or commands directly
to a Telegram Bot to trigger cloud offline downloads and check storage status.

Supports:
- Webhook mode: POST /api/telegram/webhook
- Simulation/Test mode: POST /api/telegram/simulate (works without external internet)
- Optional background long-polling if TELEGRAM_BOT_TOKEN & TELEGRAM_POLLING=1
- Account binding: `/bind <username> <password>` maps a Telegram chat_id to a MyDrive user
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
from db_adapter import connect_db
import threading
import time
import urllib.request
from pathlib import Path
from typing import Optional

URL_PATTERN = re.compile(r"(magnet:\?xt=urn:btih:[^\s]+|https?://[^\s]+)", re.IGNORECASE)


class TelegramBotAdapter:
    """Bridges Telegram Bot messages to MyDrive offline downloads and file queries."""

    def __init__(self, storage, auth, offline_engine, storage_root: str):
        self.storage = storage
        self.auth = auth
        self.offline = offline_engine
        self.cfg_path = Path(storage_root) / "json" / "telegram_bot.json"
        self.cfg_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path = Path(storage_root) / "db" / "telegram.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.config = self._load_config()
        self._init_db()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def _load_config(self) -> dict:
        cfg = {
            "bot_token": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            "default_save_path": os.environ.get("TELEGRAM_SAVE_PATH", "/Telegram"),
            "webhook_secret": os.environ.get("TELEGRAM_WEBHOOK_SECRET", ""),
            "polling_enabled": os.environ.get("TELEGRAM_POLLING", "0").lower() in ("1", "true", "yes"),
        }
        if self.cfg_path.exists():
            try:
                cfg.update(json.loads(self.cfg_path.read_text(encoding="utf-8")))
            except Exception:
                pass
        return cfg

    def save_config(self, updates: dict) -> dict:
        for k in ("bot_token", "default_save_path", "webhook_secret", "polling_enabled"):
            if k in updates:
                self.config[k] = updates[k]
        self.cfg_path.write_text(json.dumps(self.config, indent=2, ensure_ascii=False), encoding="utf-8")
        return self.get_public_config()

    def get_public_config(self) -> dict:
        return {
            "bot_token_set": bool(self.config.get("bot_token")),
            "default_save_path": self.config.get("default_save_path", "/Telegram"),
            "webhook_secret_set": bool(self.config.get("webhook_secret")),
            "polling_enabled": bool(self.config.get("polling_enabled")),
            "bound_chats": self._count_bindings(),
        }

    def _init_db(self):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS tg_bindings (
                chat_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                username TEXT NOT NULL,
                bound_at INTEGER NOT NULL
            )
        """)
        conn.commit()
        conn.close()

    def _count_bindings(self) -> int:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM tg_bindings")
        n = c.fetchone()[0]
        conn.close()
        return int(n or 0)

    def bind_chat(self, chat_id: str, user_id: str, username: str):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "INSERT INTO tg_bindings (chat_id, user_id, username, bound_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(chat_id) DO UPDATE SET user_id=excluded.user_id, "
            "username=excluded.username, bound_at=excluded.bound_at",
            (str(chat_id), user_id, username, int(time.time())),
        )
        conn.commit()
        conn.close()

    def get_bound_user(self, chat_id: str) -> Optional[dict]:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("SELECT user_id, username FROM tg_bindings WHERE chat_id = ?", (str(chat_id),))
        row = c.fetchone()
        conn.close()
        if not row:
            return None
        return {"user_id": row[0], "username": row[1]}

    def handle_update(self, update: dict, send_reply: bool = True) -> dict:
        """Process a Telegram Update object and return the reply payload."""
        msg = update.get("message") or update.get("channel_post") or {}
        chat = msg.get("chat") or {}
        chat_id = str(chat.get("id", ""))
        text = (msg.get("text") or msg.get("caption") or "").strip()

        if not chat_id:
            return {"ok": False, "reply": "Missing chat_id"}

        reply = self._dispatch(chat_id, text)
        if send_reply and self.config.get("bot_token"):
            self._send_telegram_message(chat_id, reply)
        return {"ok": True, "chat_id": chat_id, "reply": reply}

    def _dispatch(self, chat_id: str, text: str) -> str:
        if not text or text.startswith("/start") or text.startswith("/help"):
            return (
                "欢迎使用 MyDrive 离线下载机器人（PikPak 风格）！\n"
                "可用命令：\n"
                "• /bind <用户名> <密码> — 绑定网盘账号\n"
                "• 直接发送磁力链接 (magnet:?) 或 HTTP/HTTPS 直链 — 自动创建离线任务（命中缓存秒存）\n"
                "• /tasks — 查看最近离线任务进度\n"
                "• /quota — 查看网盘空间统计"
            )

        if text.startswith("/bind"):
            parts = text.split()
            if len(parts) < 3:
                return "用法：/bind <用户名> <密码>"
            username, password = parts[1], parts[2]
            res = self.auth.login(username, password)
            if not res:
                return "绑定失败：用户名或密码错误。"
            self.bind_chat(chat_id, res["user_id"], res["username"])
            return f"绑定成功！当前账号：{res['username']}。现在直接转发磁力或下载链接即可离线保存到 {self.config.get('default_save_path', '/Telegram')}。"

        bound = self.get_bound_user(chat_id)
        if not bound:
            return "请先使用 `/bind <用户名> <密码>` 绑定你的 MyDrive 账号。"

        user_id = bound["user_id"]

        if text.startswith("/quota"):
            st = self.storage.get_storage_stats(user_id=user_id)
            mb = st["logical_bytes"] / (1024 * 1024)
            return f"账号 {bound['username']}：共 {st['total_files']} 个文件，已用逻辑空间 {mb:.2f} MB（去重率 {st['dedup_ratio']*100:.1f}%）。"

        if text.startswith("/tasks"):
            tasks = self.offline.list(user_id=user_id, limit=5)
            if not tasks:
                return "当前没有离线任务。"
            lines = ["最近离线任务："]
            for t in tasks[:5]:
                pct = t.get("progress", 0)
                status = t.get("status", 0)
                tag = "✅ 完成" if status == 1000 else ("❌ 失败" if status < 0 else f"⏳ {pct}%")
                lines.append(f"• {t.get('name', '未命名')} [{tag}]")
            return "\n".join(lines)

        urls = URL_PATTERN.findall(text)
        if not urls:
            return "未识别到磁力链接或 HTTP/HTTPS 链接。请发送链接或输入 /help 查看命令。"

        save_path = self.config.get("default_save_path", "/Telegram")
        replies = []
        for u in urls[:10]:
            try:
                task = self.offline.add({"url": u, "save_path": save_path}, user_id=user_id)
                instant = "⚡ 命中全局秒存，已直接入库！" if task.get("instant_saved") else "已加入离线下载队列"
                replies.append(f"• {task.get('name', u[:40])}: {instant}")
            except Exception as e:
                replies.append(f"• {u[:40]}: 添加失败 ({e})")
        return "\n".join(replies)

    def _send_telegram_message(self, chat_id: str, text: str):
        token = self.config.get("bot_token")
        if not token:
            return
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = json.dumps({"chat_id": chat_id, "text": text}).encode("utf-8")
        req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10):
                pass
        except Exception:
            pass
