"""
v6 扩展 RPC 实现
=================
补齐线上 v6 API 中 MyDrive 尚未实现的服务：

* ``PubUserFile`` 的上传令牌 / 切片解析 / 下载地址 / 预览
* ``PubSlice`` 的 Slice / Meta 元数据
* ``PubDavConfig``、``PubSftpConfig`` 的挂载配置
* ``PubOfflineTask`` 的离线下载任务
* ``PubTicket`` 的工单与对话
* ``OauthAuthorization`` 的授权码 + 设备码流程
* ``PubUser`` 的设置 / 容量扩展查询

数据落在 ``storage_root/json/*.json``，按用户分桶。
"""

import base64
import hashlib
import json
import os
import secrets
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

import pb
import protocol as proto

# gRPC status codes (kept local to avoid an import cycle with grpc_compat)
OK = 0
INVALID_ARGUMENT = 3
NOT_FOUND = 5
PERMISSION_DENIED = 7
ALREADY_EXISTS = 6
UNIMPLEMENTED = 12
UNAUTHENTICATED = 16


class ExtraError(Exception):
    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


_LOCK = threading.RLock()


# ==================== JSON STORE ====================

def _json_path(ctx, name: str) -> Path:
    base = Path(getattr(ctx.engine, "root", ".")) / "json"
    base.mkdir(parents=True, exist_ok=True)
    return base / name


def _load(ctx, name: str, default):
    path = _json_path(ctx, name)
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _save(ctx, name: str, value) -> None:
    path = _json_path(ctx, name)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _now_ms() -> int:
    return int(time.time() * 1000)


def _meta(ctx, identity: str):
    if not identity:
        raise ExtraError(INVALID_ARGUMENT, "identity required")
    meta = ctx.engine.get_file_meta(identity, user_id=ctx.uid)
    if not meta:
        raise ExtraError(NOT_FOUND, "file not found")
    return meta


# ==================== PubUser: settings / quota ====================

def h_get_settings(ctx, req: dict) -> dict:
    data = _load(ctx, "settings.json", {})
    current = data.get(ctx.owner, {})
    return {"settings": [{"key": k, "value": v} for k, v in current.items()]}


def h_set_settings(ctx, req: dict) -> dict:
    items = req.get("settings") or []
    if isinstance(items, dict):
        items = [items]
    with _LOCK:
        data = _load(ctx, "settings.json", {})
        current = data.setdefault(ctx.owner, {})
        for item in items:
            key = (item or {}).get("key")
            if key:
                current[key] = (item or {}).get("value", "")
        _save(ctx, "settings.json", data)
    return {"settings": [{"key": k, "value": v} for k, v in current.items()]}


def h_available_extend_quota(ctx, req: dict) -> dict:
    """GetAvailableExtendQuota —— 线上返回 12，这里给出可扩展的额度上限。"""
    return {
        "daily_bytes_download": int(os.environ.get("MYDRIVE_DL_QUOTA", 1024 ** 4)),
        "daily_bytes_upload": int(os.environ.get("MYDRIVE_UL_QUOTA", 10 * 1024 ** 3)),
        "daily_offline_tasks": int(os.environ.get("MYDRIVE_OFFLINE_QUOTA", 100)),
        "daily_offline_tasks_bytes": int(os.environ.get("MYDRIVE_OFFLINE_BYTES", 100 * 1024 ** 3)),
        "disk_bytes": 0,
        "disk_files": 0,
    }


# ==================== PubUserFile extras ====================

def h_create_upload_token(ctx, req: dict) -> dict:
    token = proto.build_auth_key(
        cid=req.get("identity") or "upload",
        user_identity=ctx.owner,
        secret=ctx.presign_secret,
        ttl=3600,
    )
    return {"token": token}


def h_download_preview_info(ctx, req: dict) -> dict:
    meta = _meta(ctx, req.get("identity"))
    editable = meta.mime_type.startswith(("text/", "application/json"))
    return {
        "identity": meta.identity,
        "parent": meta.parent,
        "name": meta.name,
        "path": meta.path,
        "mime_type": meta.mime_type,
        "size": meta.size,
        "type": meta.type,
        "can_preview": _previewable(meta.mime_type, meta.name),
        "can_edit": editable,
        "preview_type": 1 if _previewable(meta.mime_type, meta.name) else 0,
        "download_type": 1,
        "sha1": _sha1_of_file(ctx, meta),
        "can_download": not meta.trashed,
        "content_identity": meta.content_identity,
    }


def _previewable(mime: str, name: str) -> bool:
    if mime.startswith(("image/", "video/", "audio/", "text/")):
        return True
    return name.lower().endswith((".pdf", ".md", ".json", ".txt", ".doc", ".docx",
                                  ".xls", ".xlsx", ".ppt", ".pptx"))


_SHA1_CACHE: Dict[str, str] = {}


def _sha1_of_file(ctx, meta) -> str:
    key = meta.content_identity or meta.identity
    if key in _SHA1_CACHE:
        return _SHA1_CACHE[key]
    digest = hashlib.sha1()
    try:
        for chunk in meta.chunks:
            data = ctx.engine.get_chunk(chunk.cid)
            if data:
                digest.update(data)
    except Exception:
        return ""
    value = digest.hexdigest() if meta.size else hashlib.sha1(b"").hexdigest()
    _SHA1_CACHE[key] = value
    return value


def h_parse_file_slice(ctx, req: dict) -> dict:
    meta = _meta(ctx, req.get("identity"))
    sizes, offset = [], 0
    for chunk in meta.chunks:
        sizes.append({"start_index": offset, "end_index": offset + chunk.size, "size": chunk.size})
        offset += chunk.size
    return {
        "content_identity": meta.content_identity,
        "meta_nodes": [meta.content_identity] if meta.content_identity else [],
        "raw_nodes": [c.cid for c in meta.chunks],
        "file_size": meta.size,
        "sizes": sizes,
        "sha1": _sha1_of_file(ctx, meta),
        "name": meta.name,
        "path": meta.path,
        "store_type": meta.store_type,
    }


def _slice_address(ctx, cid: str) -> dict:
    cred = proto.build_slice_query(cid, ctx.owner, ctx.presign_secret, ttl=86400)
    return {
        "identity": cid,
        "download_address": proto.slice_url(cid, cred["auth_key"], "", cred["session"]),
        "download_token": cred["auth_key"],
        "encrypt": 0,
        "store_type": 0,
    }


def h_slice_download_address(ctx, req: dict) -> dict:
    identities = req.get("identity") or []
    if isinstance(identities, str):
        identities = [identities]
    if not identities:
        raise ExtraError(INVALID_ARGUMENT, "identity required")
    return {
        "addresses": [_slice_address(ctx, cid) for cid in identities],
        "expire_at": _now_ms() + 86400 * 1000,
        "request_address": ctx.base_url,
        "version": 1,
    }


def h_direct_download_address(ctx, req: dict) -> dict:
    meta = _meta(ctx, req.get("identity"))
    token = proto.build_auth_key(meta.identity, ctx.owner, ctx.presign_secret, ttl=3600)
    return {
        "download_address": f"{ctx.base_url}/api/files/{meta.identity}/download?auth_key={token}",
        "expire_at": _now_ms() + 3600 * 1000,
        "request_address": ctx.base_url,
        "version": 1,
        "encrypt": 0,
        "store_type": meta.store_type,
        "file_size": meta.size,
        "path": meta.path,
        "name": meta.name,
        "content_identity": meta.content_identity,
    }


def h_preview_doc(ctx, req: dict) -> dict:
    meta = _meta(ctx, req.get("identity"))
    ok = _previewable(meta.mime_type, meta.name)
    return {
        "identity": meta.identity,
        "parent": meta.parent,
        "name": meta.name,
        "path": meta.path,
        "mime_type": meta.mime_type,
        "size": meta.size,
        "type": meta.type,
        "can_preview": ok,
        "preview_address": f"{ctx.base_url}/api/files/{meta.identity}/download" if ok else "",
        "can_edit": meta.mime_type.startswith(("text/", "application/json")),
    }


def h_report_preview_status(ctx, req: dict) -> dict:
    return dict(req or {})


def h_list_recent(ctx, req: dict) -> dict:
    parent = (req.get("parent") or {}).get("identity") or "root"
    info = req.get("list_info") or {}
    limit = int(info.get("limit") or 100)
    files = ctx.engine.list_files(parent, user_id=ctx.uid)
    files = [f for f in files if not f.trashed]
    files.sort(key=lambda f: f.update_ts, reverse=True)
    from grpc_compat import file_pb  # local import: avoids a cycle at module load
    return {"files": [file_pb(f) for f in files[:limit]], "list_info": {"limit": limit}}


# ==================== PubSlice ====================

def h_slice_get(ctx, req: dict) -> dict:
    identity = req.get("identity") or ""
    if not identity:
        raise ExtraError(INVALID_ARGUMENT, "identity required")
    size = ctx.engine.chunk_size_of(identity)
    return {
        "identity": identity,
        "type": req.get("type") or 0,
        "status": 0 if size is not None else 5,
        "create_ts": _now_ms(),
        "update_ts": _now_ms(),
        "server": ctx.base_url,
        "need_data": size is None,
    }


def h_slice_create(ctx, req: dict) -> dict:
    out = dict(req or {})
    out["status"] = 0
    out.setdefault("create_ts", _now_ms())
    out["update_ts"] = _now_ms()
    out["server"] = ctx.base_url
    return out


def h_meta_get(ctx, req: dict) -> dict:
    identity = req.get("identity") or ""
    if not identity:
        raise ExtraError(INVALID_ARGUMENT, "identity required")
    data = ctx.engine.get_chunk(identity) or b""
    return {"identity": identity, "data": data}


def h_meta_create(ctx, req: dict) -> dict:
    identity = req.get("identity") or ""
    data = req.get("data") or b""
    if isinstance(data, str):
        data = data.encode()
    if identity and data:
        try:
            ctx.engine._store_chunk(identity, data)
        except Exception:
            pass
    return {"identity": identity, "data": data}


# ==================== PubDavConfig ====================

DAV_DEFAULT = {
    "enable_flag": 1,
    "root": "/",
    "write_flag": 1,
    "lock_flag": 0,
    "chunked_flag": 1,
    "path_style": 0,
    "status": 0,
    "prefix": "",
}


def _dav_bucket(ctx) -> dict:
    data = _load(ctx, "dav.json", {})
    cfg = data.get(ctx.owner)
    if not cfg:
        record = ctx.auth.get_user(ctx.uid) or {}
        cfg = dict(DAV_DEFAULT)
        cfg.update({
            "user_identity": ctx.owner,
            "identity": proto.new_identity(),
            "username": record.get("username", ctx.owner[:8]),
            "password": secrets.token_hex(3),
            "update_ts": _now_ms(),
            "last_access_ts": 0,
            "code": 0,
            "message": "",
            "remark": "",
        })
        data[ctx.owner] = cfg
        _save(ctx, "dav.json", data)
    return cfg


def _dav_save(ctx, cfg: dict) -> None:
    data = _load(ctx, "dav.json", {})
    cfg["update_ts"] = _now_ms()
    data[ctx.owner] = cfg
    _save(ctx, "dav.json", data)


def h_dav_get(ctx, req: dict) -> dict:
    return _dav_bucket(ctx)


def h_dav_update(ctx, req: dict) -> dict:
    cfg = _dav_bucket(ctx)
    for key in ("root", "write_flag", "lock_flag", "chunked_flag", "path_style",
                "prefix", "remark", "username", "password"):
        if key in (req or {}):
            cfg[key] = req[key]
    _dav_save(ctx, cfg)
    return cfg


def h_dav_enable(ctx, req: dict) -> dict:
    cfg = _dav_bucket(ctx)
    cfg["enable_flag"] = 1
    cfg["status"] = 0
    _dav_save(ctx, cfg)
    return cfg


def h_dav_disable(ctx, req: dict) -> dict:
    cfg = _dav_bucket(ctx)
    cfg["enable_flag"] = 0
    _dav_save(ctx, cfg)
    return cfg


def h_dav_create(ctx, req: dict) -> dict:
    cfg = _dav_bucket(ctx)
    h_dav_update(ctx, req or {})
    return _dav_bucket(ctx)


def h_dav_delete(ctx, req: dict) -> dict:
    data = _load(ctx, "dav.json", {})
    cfg = data.pop(ctx.owner, None) or _dav_bucket(ctx)
    _save(ctx, "dav.json", data)
    cfg["enable_flag"] = 0
    return cfg


def h_dav_list(ctx, req: dict) -> dict:
    return {"configs": [_dav_bucket(ctx)], "list_info": {"limit": 100}}


def h_dav_validate_username(ctx, req: dict) -> dict:
    wanted = (req or {}).get("username") or ""
    taken = False
    for owner, cfg in (_load(ctx, "dav.json", {}) or {}).items():
        if owner != ctx.owner and cfg.get("username") == wanted:
            taken = True
            break
    return {"code": 0 if wanted and not taken else 6,
            "message": "" if not taken else "username already used",
            "valid": bool(wanted) and not taken}


def h_dav_server_info(ctx, req: dict) -> dict:
    return {
        "server_name": "webdav-server",
        "server_version": "0.0.3-build202402220030",
        "server_software": "Microsoft-IIS/8.0",
        "server_address": f"{ctx.base_url}/webdav",
        "server_port": "443",
        "server_protocol": "HTTP/1.1",
        "server_root": "/",
        "server_ssl": True,
        "max_users": 5,
    }


# ==================== PubSftpConfig ====================

def _sftp_key() -> str:
    body = base64.b64encode(secrets.token_bytes(48)).decode().rstrip("=")
    return f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI{body} mydrive@localhost"


def _sftp_bucket(ctx) -> dict:
    data = _load(ctx, "sftp.json", {})
    cfg = data.get(ctx.owner)
    if not cfg:
        record = ctx.auth.get_user(ctx.uid) or {}
        cfg = {
            "user_identity": ctx.owner,
            "enable_flag": 1,
            "root": "/",
            "write_flag": 1,
            "lock_flag": 0,
            "chunked_flag": 1,
            "path_style": 0,
            "status": 0,
            "prefix": "",
            "code": 0,
            "message": "",
            "username": record.get("username", ctx.owner[:8]),
            "password": secrets.token_hex(3),
            "update_ts": _now_ms(),
            "ssh_key": _sftp_key(),
        }
        data[ctx.owner] = cfg
        _save(ctx, "sftp.json", data)
    return cfg


def _sftp_save(ctx, cfg: dict) -> None:
    data = _load(ctx, "sftp.json", {})
    cfg["update_ts"] = _now_ms()
    data[ctx.owner] = cfg
    _save(ctx, "sftp.json", data)


def h_sftp_get(ctx, req: dict) -> dict:
    return _sftp_bucket(ctx)


def h_sftp_update(ctx, req: dict) -> dict:
    cfg = _sftp_bucket(ctx)
    for key in ("root", "write_flag", "lock_flag", "chunked_flag", "path_style",
                "prefix", "username", "password"):
        if key in (req or {}):
            cfg[key] = req[key]
    _sftp_save(ctx, cfg)
    return cfg


def h_sftp_enable(ctx, req: dict) -> dict:
    cfg = _sftp_bucket(ctx)
    cfg["enable_flag"] = 1
    _sftp_save(ctx, cfg)
    return cfg


def h_sftp_disable(ctx, req: dict) -> dict:
    cfg = _sftp_bucket(ctx)
    cfg["enable_flag"] = 0
    _sftp_save(ctx, cfg)
    return cfg


def h_sftp_update_keys(ctx, req: dict) -> dict:
    cfg = _sftp_bucket(ctx)
    cfg["ssh_key"] = (req or {}).get("ssh_key") or _sftp_key()
    _sftp_save(ctx, cfg)
    return cfg


def h_sftp_validate_username(ctx, req: dict) -> dict:
    wanted = (req or {}).get("username") or ""
    taken = any(owner != ctx.owner and cfg.get("username") == wanted
                for owner, cfg in (_load(ctx, "sftp.json", {}) or {}).items())
    return {"code": 0 if wanted and not taken else 6,
            "message": "" if not taken else "username already used",
            "valid": bool(wanted) and not taken}


# ==================== PubOfflineTask ====================

def _guess_name_from_url(url: str) -> str:
    if url.startswith("magnet:"):
        for part in url.split("&"):
            if part.startswith("dn="):
                from urllib.parse import unquote_plus
                return unquote_plus(part[3:])
        return "magnet_task"
    from urllib.parse import urlparse, unquote
    path = unquote(urlparse(url).path or "")
    return os.path.basename(path) or "offline_task"


def h_offline_parse(ctx, req: dict) -> dict:
    url = (req or {}).get("url") or (req or {}).get("file") or ""
    if not url:
        raise ExtraError(INVALID_ARGUMENT, "url required")
    engine = getattr(ctx, "offline", None)
    if engine is not None:
        return engine.parse(url, "/", ctx.owner)
    name = _guess_name_from_url(url)
    task_id = proto.new_identity()
    meta = {
        "identity": task_id,
        "type": 3 if url.startswith("magnet:") else 2,
        "status": 0,
        "create_ts": _now_ms(),
        "update_ts": _now_ms(),
        "url": url,
        "name": name,
        "size": 0,
        "progress": 0,
        "bytes_total": 0,
        "bytes_processed": 0,
        "message": "",
    }
    return {"meta": meta,
            "task_files": [{"identity": proto.new_identity(), "path": "/" + name,
                            "status": 0, "name": name, "size": 0, "index": 0,
                            "directory": False, "create_ts": _now_ms(),
                            "update_ts": _now_ms()}]}


def h_offline_add(ctx, req: dict) -> dict:
    engine = getattr(ctx, "offline", None)
    if engine is not None:
        try:
            return engine.add(req or {}, ctx.owner)
        except ValueError as exc:
            raise ExtraError(INVALID_ARGUMENT, str(exc))
    task = dict(req or {})
    task.setdefault("identity", proto.new_identity())
    task["create_ts"] = _now_ms()
    task["update_ts"] = _now_ms()
    task.setdefault("status", 0)
    task.setdefault("progress", 0)
    with _LOCK:
        data = _load(ctx, "offline.json", {})
        bucket = data.setdefault(ctx.owner, [])
        bucket.insert(0, task)
        _save(ctx, "offline.json", data)
    return task


def h_offline_list(ctx, req: dict) -> dict:
    info = (req or {}).get("list_info") or {}
    limit = int(info.get("limit") or 100)
    engine = getattr(ctx, "offline", None)
    if engine is not None:
        return {"tasks": engine.list(ctx.owner, limit, info.get("token") or ""),
                "list_info": {"limit": limit}}
    tasks = _load(ctx, "offline.json", {}).get(ctx.owner, [])
    return {"tasks": tasks[:limit], "list_info": {"limit": limit}}


def h_offline_delete(ctx, req: dict) -> dict:
    wanted = set((req or {}).get("identity") or [])
    engine = getattr(ctx, "offline", None)
    if engine is not None:
        return {"count": engine.delete(sorted(wanted), ctx.owner,
                                       bool((req or {}).get("delete_files")))}
    with _LOCK:
        data = _load(ctx, "offline.json", {})
        bucket = data.get(ctx.owner, [])
        keep = [t for t in bucket if t.get("identity") not in wanted]
        data[ctx.owner] = keep
        _save(ctx, "offline.json", data)
    return {"count": len(bucket) - len(keep)}


# ==================== PubTicket ====================

def h_ticket_create(ctx, req: dict) -> dict:
    ticket = dict(req or {})
    ticket.setdefault("identity", proto.new_identity())
    ticket["create_ts"] = _now_ms()
    ticket.setdefault("status", "open")
    ticket.setdefault("read", False)
    with _LOCK:
        data = _load(ctx, "tickets.json", {})
        data.setdefault(ctx.owner, []).insert(0, ticket)
        _save(ctx, "tickets.json", data)
    return ticket


def h_ticket_list(ctx, req: dict) -> dict:
    info = (req or {}).get("list_info") or {}
    limit = int(info.get("limit") or 100)
    tickets = _load(ctx, "tickets.json", {}).get(ctx.owner, [])
    return {"tickets": tickets[:limit], "list_info": {"limit": limit}}


def h_ticket_get(ctx, req: dict) -> dict:
    wanted = (req or {}).get("identity")
    for ticket in _load(ctx, "tickets.json", {}).get(ctx.owner, []):
        if ticket.get("identity") == wanted:
            return ticket
    raise ExtraError(NOT_FOUND, "ticket not found")


def h_ticket_comment(ctx, req: dict) -> dict:
    wanted = (req or {}).get("identity")
    with _LOCK:
        data = _load(ctx, "tickets.json", {})
        bucket = data.get(ctx.owner, [])
        for ticket in bucket:
            if ticket.get("identity") == wanted:
                ticket["comment_count"] = int(ticket.get("comment_count", 0)) + 1
                ticket["update_ts"] = _now_ms()
                _save(ctx, "tickets.json", data)
                return ticket
    raise ExtraError(NOT_FOUND, "ticket not found")


def h_ticket_close(ctx, req: dict) -> dict:
    wanted = set((req or {}).get("list") or [])
    done = []
    with _LOCK:
        data = _load(ctx, "tickets.json", {})
        for ticket in data.get(ctx.owner, []):
            if ticket.get("identity") in wanted:
                ticket["status"] = "closed"
                ticket["update_ts"] = _now_ms()
                done.append(ticket["identity"])
        _save(ctx, "tickets.json", data)
    return {"list": done}


def h_ticket_delete(ctx, req: dict) -> dict:
    wanted = set((req or {}).get("list") or [])
    with _LOCK:
        data = _load(ctx, "tickets.json", {})
        bucket = data.get(ctx.owner, [])
        keep = [t for t in bucket if t.get("identity") not in wanted]
        data[ctx.owner] = keep
        _save(ctx, "tickets.json", data)
    return {"list": list(wanted - {t["identity"] for t in keep})}


def h_contact_get(ctx, req: dict) -> dict:
    data = _load(ctx, "contact.json", {})
    return data.get(ctx.owner, {})


def h_contact_update(ctx, req: dict) -> dict:
    with _LOCK:
        data = _load(ctx, "contact.json", {})
        current = data.setdefault(ctx.owner, {})
        current.update({k: v for k, v in (req or {}).items() if v})
        _save(ctx, "contact.json", data)
    return current


def h_conversation_list(ctx, req: dict) -> dict:
    ticket_id = (req or {}).get("ticket_identity") or ""
    info = (req or {}).get("list_info") or {}
    limit = int(info.get("limit") or 100)
    threads = _load(ctx, "conversations.json", {}).get(ticket_id, [])
    return {"conversations": threads[:limit], "list_info": {"limit": limit}}


# ==================== OauthAuthorization ====================

_AUTH_CODES: Dict[str, dict] = {}


def h_oauth_authorize(ctx, req: dict) -> dict:
    code = secrets.token_hex(16)
    _AUTH_CODES[code] = {
        "client_id": (req or {}).get("client_id", ""),
        "redirect_uri": (req or {}).get("redirect_uri", ""),
        "state": (req or {}).get("state", ""),
        "scope": (req or {}).get("scope", ""),
        "code_challenge": (req or {}).get("code_challenge", ""),
        "expires_ts": int(time.time()) + 600,
    }
    return {
        "code": code,
        "redirect_uri": (req or {}).get("redirect_uri", ""),
        "state": (req or {}).get("state", ""),
        "status": "ok" if ctx.uid else "login_required",
        "expires_in": 600,
        "expires_in_ts": int(time.time()) * 1000 + 600000,
    }


def h_oauth_device_code(ctx, req: dict) -> dict:
    info = ctx.auth.generate_device_code()
    return {
        "user_code": info["user_code"],
        "device_code": info["device_code"],
        "expires_in": info["expires_in"],
        "verification_uri": info.get("verification_url", ""),
        "interval": info["interval"],
        "expires_in_ts": _now_ms() + info["expires_in"] * 1000,
    }


def h_oauth_authorize_state(ctx, req: dict) -> dict:
    code = (req or {}).get("code") or ""
    entry = _AUTH_CODES.get(code)
    if not entry:
        return {"code": code, "status": "invalid"}
    return {
        "code": code,
        "state": entry.get("state", ""),
        "status": "ok" if int(time.time()) <= entry["expires_ts"] else "expired",
        "redirect_uri": entry.get("redirect_uri", ""),
        "scope": entry.get("scope", ""),
        "client_id": entry.get("client_id", ""),
        "response_type": "code",
        "version": "2.0",
    }


def h_oauth_device_state(ctx, req: dict) -> dict:
    device_code = (req or {}).get("device_code") or ""
    result = ctx.auth.poll_device_code(device_code)
    if result.get("success"):
        return {
            "user_code": (req or {}).get("user_code", ""),
            "device_code": device_code,
            "login": True,
            "status": "authorized",
            "access_token": result.get("access_token", ""),
            "refresh_token": result.get("refresh_token", ""),
            "expires_in": result.get("expires_in", 3600),
            "expires_in_ts": _now_ms() + 3600 * 1000,
        }
    return {
        "user_code": (req or {}).get("user_code", ""),
        "device_code": device_code,
        "login": False,
        "status": result.get("error", "authorization_pending"),
    }


def h_oauth_get_token(ctx, req: dict) -> dict:
    code = (req or {}).get("code") or ""
    entry = _AUTH_CODES.pop(code, None)
    if not entry:
        raise ExtraError(UNAUTHENTICATED, "invalid_grant")
    if int(time.time()) > entry["expires_ts"]:
        raise ExtraError(UNAUTHENTICATED, "expired_token")
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    record = ctx.auth.get_user(ctx.uid) or {}
    tokens = ctx.auth._generate_tokens(ctx.uid, record.get("username", ""))
    return {
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "expires_in": 3600,
        "token_type": "Bearer",
        "scope": entry.get("scope", ""),
        "expires_in_ts": _now_ms() + 3600 * 1000,
    }


def h_oauth_refresh(ctx, req: dict) -> dict:
    result = ctx.auth.refresh((req or {}).get("refresh_token") or "")
    if not result.get("success"):
        raise ExtraError(UNAUTHENTICATED, result.get("error", "refresh failed"))
    return {
        "access_token": result["access_token"],
        "refresh_token": result["refresh_token"],
        "expires_in": 3600,
        "token_type": "Bearer",
        "expires_in_ts": _now_ms() + 3600 * 1000,
    }


# ==================== PubUser: account lifecycle ====================

def _file_pb(meta) -> dict:
    """FileMeta -> v6 ``File`` message dict (local copy; avoids importing grpc_compat)."""
    return {
        "identity": meta.identity, "parent": meta.parent, "name": meta.name,
        "path": meta.path, "mime_type": meta.mime_type, "size": meta.size,
        "type": meta.type, "create_ts": meta.create_ts, "update_ts": meta.update_ts,
        "delete_ts": meta.delete_ts, "deleted": meta.deleted, "dir": meta.is_dir,
        "hidden": meta.hidden, "locked": meta.locked, "shared": meta.shared,
        "starred": meta.starred, "trashed": meta.trashed,
        "content_identity": meta.content_identity, "store_type": meta.store_type,
        "version": meta.version, "files": meta.files, "directories": meta.directories,
        "nodes": meta.nodes, "sort_name": meta.sort_name,
    }


def _user_pb(ctx, record: dict) -> dict:
    """Serialize an auth record into the v6 ``User`` message."""
    if not record:
        return {}
    profile = ctx.auth.get_profile(record.get("user_id", "")) if record.get("user_id") else {}
    return {
        "identity": record.get("user_id", ""),
        "type": 0,
        "status": 1,
        "create_ts": int(record.get("created_ts") or 0) * 1000,
        "update_ts": _now_ms(),
        "name": profile.get("name") or record.get("username", ""),
        "addon": profile.get("addon", ""),
        "icon": profile.get("icon", ""),
    }


def _login_response(ctx, result: dict, username: str = "", device: str = "") -> dict:
    now = _now_ms()
    record = ctx.auth.get_user(result["user_id"]) or {}
    profile = ctx.auth.get_profile(result["user_id"])
    return {
        "token": {
            "identity": proto.new_identity(),
            "user_identity": result["user_id"],
            "device": device,
            "version": 1,
            "access_token": result["access_token"],
            "refresh_token": result["refresh_token"],
            "update_ts": now,
            "access_token_expire_ts": now + 3600 * 1000,
            "refresh_token_expire_ts": now + 30 * 86400 * 1000,
        },
        "user": _user_pb(ctx, record),
        "last_login": {
            "identity": result["user_id"],
            "last_login_ts": int(profile.get("last_login_ts") or 0),
            "last_login_ip": profile.get("last_login_ip", ""),
            "last_login_device": profile.get("last_login_device", ""),
        },
        "state": "",
        "code": "",
    }


def h_user_register(ctx, req: dict) -> dict:
    username = (req or {}).get("name") or (req or {}).get("identity") or ""
    password = (req or {}).get("password") or ""
    sms = (req or {}).get("sms_captcha") or ""
    if sms and not ctx.auth.verify_sms_code(username, sms):
        raise ExtraError(INVALID_ARGUMENT, "invalid sms captcha")
    result = ctx.auth.register(username, password)
    if not result.get("success"):
        raise ExtraError(ALREADY_EXISTS if "exists" in result.get("error", "").lower()
                         else INVALID_ARGUMENT, result.get("error", "register failed"))
    ctx.auth.touch_login(result["user_id"])
    return _user_pb(ctx, ctx.auth.get_user(result["user_id"]) or result)


def h_user_logoff(ctx, req: dict) -> dict:
    ctx.auth.logoff((req or {}).get("refresh_token") or "", ctx.uid or "")
    record = ctx.auth.get_user(ctx.uid) if ctx.uid else None
    return _user_pb(ctx, record or {"user_id": ctx.uid or "", "username": ""})


def h_user_device_authorization(ctx, req: dict) -> dict:
    """v6 ``DeviceAuthorization`` — mint a device code for the signed-in user."""
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    info = ctx.auth.generate_device_code()
    record = ctx.auth.get_user(ctx.uid) or {}
    now = _now_ms()
    tokens = ctx.auth._generate_tokens(ctx.uid, record.get("username", ""))
    return {
        "token": {
            "identity": proto.new_identity(),
            "user_identity": ctx.uid,
            "device": (req or {}).get("device", ""),
            "version": 1,
            "access_token": tokens["access_token"],
            "refresh_token": tokens["refresh_token"],
            "update_ts": now,
            "access_token_expire_ts": now + 3600 * 1000,
            "refresh_token_expire_ts": now + 30 * 86400 * 1000,
        },
        "user": _user_pb(ctx, record),
        "state": info["device_code"],
        "code": info["user_code"],
    }


def h_user_reset_password(ctx, req: dict) -> dict:
    username = (req or {}).get("identity") or (req or {}).get("input") or ""
    new_password = (req or {}).get("password") or ""
    captcha = (req or {}).get("captcha") or ""
    result = ctx.auth.reset_password(username, new_password, captcha)
    if not result.get("success"):
        raise ExtraError(UNAUTHENTICATED, result.get("error", "reset failed"))
    return _user_pb(ctx, ctx.auth.get_user(result["user_id"]))


def h_user_change_password(ctx, req: dict) -> dict:
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    requested = (req or {}).get("identity") or ""
    if requested and str(requested) != str(ctx.uid):
        raise ExtraError(PERMISSION_DENIED, "cannot change another user's password")
    target = ctx.uid
    result = ctx.auth.change_password(
        target, (req or {}).get("old_password") or "", (req or {}).get("new_password") or "")
    if not result.get("success"):
        raise ExtraError(UNAUTHENTICATED, result.get("error", "change failed"))
    return _user_pb(ctx, ctx.auth.get_user(target))


def h_user_delete(ctx, req: dict) -> dict:
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    requested = (req or {}).get("identity") or ""
    if requested and str(requested) != str(ctx.uid):
        raise ExtraError(PERMISSION_DENIED, "cannot delete another user's account")
    target = ctx.uid
    result = ctx.auth.delete_user(target, (req or {}).get("password") or "")
    if not result.get("success"):
        raise ExtraError(PERMISSION_DENIED, result.get("error", "delete failed"))
    return _user_pb(ctx, ctx.auth.get_user(target) or {"user_id": target})


def h_user_update(ctx, req: dict) -> dict:
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    patch = {}
    for field in ("name", "addon", "icon"):
        if (req or {}).get(field) not in (None, ""):
            patch[field] = (req or {}).get(field)
    if patch:
        ctx.auth.update_profile(ctx.uid, **patch)
    if (req or {}).get("password"):
        result = ctx.auth.change_password(
            ctx.uid, (req or {}).get("old_password") or "", (req or {}).get("password"))
        if not result.get("success"):
            raise ExtraError(INVALID_ARGUMENT, result.get("error", "update failed"))
    return _user_pb(ctx, ctx.auth.get_user(ctx.uid))


def _sms_captcha(ctx, phone: str, scene: int = 0) -> dict:
    result = ctx.auth.send_sms_code(phone, scene)
    if not result.get("success"):
        raise ExtraError(INVALID_ARGUMENT, result.get("error", "send failed"))
    return {
        "captcha": result.get("code", ""),
        "type": scene,
        "addon": phone,
        "expire_ts": int(result.get("expire_ts") or 0),
    }


def h_user_send_sms(ctx, req: dict) -> dict:
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    profile = ctx.auth.get_profile(ctx.uid)
    phone = profile.get("phone") or (req or {}).get("addon") or ctx.user.get("username", "")
    return _sms_captcha(ctx, phone, int((req or {}).get("type") or 0))


def h_user_send_sms_not_user(ctx, req: dict) -> dict:
    code = (req or {}).get("country_code") or ""
    phone = (req or {}).get("phone") or ""
    if code and not phone.startswith(code):
        phone = code + phone
    return _sms_captcha(ctx, phone, int((req or {}).get("type") or 0))


def h_user_verify_auth_token(ctx, req: dict) -> dict:
    token = (req or {}).get("identity") or (req or {}).get("input") or ""
    user = ctx.auth.verify_token(token)
    if not user:
        return {"status": 1, "code": "invalid_token", "message": "invalid token"}
    tokens = ctx.auth._generate_tokens(user["user_id"], user.get("username", ""))
    return {"status": 0, "code": "", "message": "",
            "login": _login_response(ctx, {"user_id": user["user_id"], **tokens},
                                     user.get("username", ""))}


def h_user_verify_authorization_code(ctx, req: dict) -> dict:
    code = (req or {}).get("code") or ""
    entry = _AUTH_CODES.pop(code, None)
    if not entry or int(time.time()) > entry["expires_ts"]:
        raise ExtraError(UNAUTHENTICATED, "invalid_grant")
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    record = ctx.auth.get_user(ctx.uid) or {}
    tokens = ctx.auth._generate_tokens(ctx.uid, record.get("username", ""))
    now = _now_ms()
    return {
        "identity": proto.new_identity(),
        "user_identity": ctx.uid,
        "device": entry.get("client_id", ""),
        "version": 1,
        "access_token": tokens["access_token"],
        "refresh_token": tokens["refresh_token"],
        "update_ts": now,
        "access_token_expire_ts": now + 3600 * 1000,
        "refresh_token_expire_ts": now + 30 * 86400 * 1000,
    }


def h_user_check_auth_status(ctx, req: dict) -> dict:
    state = (req or {}).get("state") or ""
    entry = _AUTH_CODES.get(state)
    if not entry:
        poll = ctx.auth.poll_device_code(state)
        if poll.get("success"):
            return {
                "status": 0, "code": "", "message": "", "state": state,
                "oauth": {"state": state},
                "login": _login_response(ctx, poll, "", (req or {}).get("device", "")),
            }
        return {"status": 2, "code": poll.get("error", "pending"),
                "message": poll.get("error", "authorization_pending"), "state": state}
    if int(time.time()) > entry["expires_ts"]:
        return {"status": 3, "code": "expired_token", "message": "expired", "state": state}
    return {"status": 0, "code": "", "message": "", "state": state,
            "oauth": {"state": state, "url": entry.get("redirect_uri", "")}}


def h_user_create_auth_token(ctx, req: dict) -> dict:
    state = (req or {}).get("state") or secrets.token_hex(16)
    _AUTH_CODES[state] = {
        "client_id": (req or {}).get("type", ""),
        "redirect_uri": (req or {}).get("return_url", ""),
        "state": state,
        "scope": (req or {}).get("scope", ""),
        "expires_ts": int(time.time()) + 600,
    }
    base = getattr(ctx, "base_url", "") or ""
    return {
        "url": f"{base}/#/authorize?state={state}",
        "state": state,
        "type": (req or {}).get("type", ""),
        "input": (req or {}).get("input", ""),
        "callback": (req or {}).get("callback", ""),
        "return_url": (req or {}).get("return_url", ""),
        "return_type": int((req or {}).get("return_type") or 0),
    }


def h_user_validate_info(ctx, req: dict) -> dict:
    name = (req or {}).get("name") or ""
    phone = (req or {}).get("phone") or ""
    email = (req or {}).get("email") or ""
    if name:
        taken = ctx.auth.user_exists(name)
        return {"code": 1 if taken else 0, "message": "name taken" if taken else "",
                "valid": not taken}
    if phone:
        taken = ctx.auth.user_exists(phone)
        return {"code": 1 if taken else 0, "message": "phone taken" if taken else "",
                "valid": not taken}
    if email:
        taken = ctx.auth.user_exists(email)
        return {"code": 1 if taken else 0, "message": "email taken" if taken else "",
                "valid": not taken}
    return {"code": 3, "message": "name/phone/email required", "valid": False}


def h_user_extend_quota(ctx, req: dict) -> dict:
    if not ctx.uid:
        raise ExtraError(UNAUTHENTICATED, "login required")
    result = ctx.auth.extend_quota(ctx.uid, int((req or {}).get("disk_bytes") or 0),
                                   int((req or {}).get("disk_files") or 0))
    if not result.get("success"):
        raise ExtraError(NOT_FOUND, result.get("error", "extend failed"))
    stats = ctx.engine.get_storage_stats(user_id=ctx.uid)
    total, used = int(result["bytes_quota"]), int(result["bytes_used"])
    return {
        "disk_statistics_quota": {
            "bytes_quota": total, "bytes_used": used,
            "bytes_free": max(0, total - used),
            "files_quota": 100000 + int((req or {}).get("disk_files") or 0),
            "files_used": int(stats.get("total_files", 0)),
        },
        "traffic_statistics_quota": {
            "daily_bytes_download_quota": int(os.environ.get("MYDRIVE_DL_QUOTA", 1024 ** 4)),
            "daily_bytes_upload_quota": int(os.environ.get("MYDRIVE_UL_QUOTA", 10 * 1024 ** 3)),
        },
        "offline_task_statistics_quota": {
            "daily_tasks_quota": 100, "daily_bytes_quota": 100 * 1024 ** 3,
        },
    }


def h_user_center_uri(ctx, req: dict) -> dict:
    base = (getattr(ctx, "base_url", "") or "http://127.0.0.1:8765").rstrip("/")
    route = (req or {}).get("route") or "account"
    scheme = "http" if (req or {}).get("disable_ssl") else base.split("://")[0]
    host = base.split("://", 1)[-1]
    return {"uri": f"{scheme}://{host}/#/{route}"}


# ==================== PubUserFile: copy / batch ====================

_OP_MAP = {0: "move", 1: "copy", 2: "trash", 3: "recover", 4: "delete", 5: "rename"}


def _batch_result(affected: int) -> dict:
    now = _now_ms()
    return {"task": proto.new_identity(), "status": 0, "affected": affected,
            "create_ts": now, "progress": affected, "total": affected}


def _source_ids(req: dict) -> List[str]:
    ids = []
    for item in (req or {}).get("source") or []:
        if isinstance(item, dict) and item.get("identity"):
            ids.append(item["identity"])
    return ids


def h_file_copy(ctx, req: dict) -> dict:
    dest = ((req or {}).get("dest") or {}).get("identity") or None
    affected = 0
    for identity in _source_ids(req):
        if ctx.engine.copy(identity, new_parent=dest, user_id=ctx.uid):
            affected += 1
    return _batch_result(affected)


def h_file_batch_rename(ctx, req: dict) -> dict:
    affected = 0
    for item in (req or {}).get("source") or []:
        identity = (item or {}).get("identity")
        new_name = (item or {}).get("name")
        if identity and new_name and ctx.engine.rename(identity, new_name, user_id=ctx.uid):
            affected += 1
    return _batch_result(affected)


def h_file_batch_operation(ctx, req: dict) -> dict:
    op_code = int((req or {}).get("operation") or 0)
    op = _OP_MAP.get(op_code, "move")
    dest = ((req or {}).get("dest") or {}).get("identity") or None
    if op == "rename":
        return h_file_batch_rename(ctx, req)
    affected = ctx.engine.batch(_source_ids(req), op, dest=dest, user_id=ctx.uid)
    return _batch_result(affected)


def h_file_update(ctx, req: dict) -> dict:
    identity = (req or {}).get("identity") or ""
    meta = ctx.engine.update(
        identity,
        name=(req or {}).get("name") or None,
        mime_type=(req or {}).get("mime_type") or None,
        hidden=(req or {}).get("hidden"),
        locked=(req or {}).get("locked"),
        starred=(req or {}).get("starred"),
        user_id=ctx.uid,
    )
    if not meta:
        raise ExtraError(NOT_FOUND, "file not found")
    return _file_pb(meta)


# ==================== PubFileShare: complaints ====================

def h_share_list_complaint(ctx, req: dict) -> dict:
    if not ctx.share:
        return {"complaints": []}
    info = (req or {}).get("list_info") or {}
    limit = int(info.get("limit") or 100)
    rows = ctx.share.list_complaints(ctx.uid)
    return {"complaints": rows[:limit], "list_info": {"limit": limit, "total": len(rows)}}


def h_share_delete_complaint(ctx, req: dict) -> dict:
    if not ctx.share:
        return {"count": 0}
    ids = list((req or {}).get("identity") or [])
    return {"count": ctx.share.delete_complaints(ids, ctx.uid)}


# ==================== WebRTC signalling (v6.services.p2p) ====================

_RTC_LOCK = threading.RLock()
_RTC_PEERS: Dict[str, dict] = {}          # client_identity -> {content, sdp, candidates}
_RTC_CANDIDATES: Dict[str, List[str]] = {}


def h_rtc_start_download(ctx, req: dict) -> dict:
    client = (req or {}).get("client_identity") or ctx.owner
    content = (req or {}).get("content_identity") or ""
    with _RTC_LOCK:
        _RTC_PEERS[client] = {"content": content, "sdp": "", "ts": time.time()}
        peers = [{"client_identity": client, "content_identity": content, "sdp": ""}]
    return {"content_identity": content, "peers": peers, "backend_peers": []}


def h_rtc_stop_download(ctx, req: dict) -> dict:
    client = (req or {}).get("client_identity") or ctx.owner
    with _RTC_LOCK:
        _RTC_PEERS.pop(client, None)
        _RTC_CANDIDATES.pop(client, None)
    return {"content_identity": (req or {}).get("content_identity") or ""}


def h_rtc_disconnect(ctx, req: dict) -> dict:
    client = (req or {}).get("client_identity") or ctx.owner
    with _RTC_LOCK:
        _RTC_PEERS.pop(client, None)
        _RTC_CANDIDATES.pop(client, None)
    return {"client_identity": client}


def h_rtc_send_candidate(ctx, req: dict) -> dict:
    client = (req or {}).get("client_identity") or ctx.owner
    candidate = (req or {}).get("candidate") or ""
    with _RTC_LOCK:
        if candidate:
            _RTC_CANDIDATES.setdefault(client, []).append(candidate)
    return {"client_identity": client}


def h_rtc_receive(ctx, req: dict) -> dict:
    client = (req or {}).get("id") or ctx.owner
    with _RTC_LOCK:
        queue = _RTC_CANDIDATES.get(client) or []
        _RTC_CANDIDATES[client] = []
    if not queue:
        return {"data": ""}
    return {"data": json.dumps({"candidates": queue}, ensure_ascii=False)}


def h_rtc_offer(ctx, req: dict) -> dict:
    """``PubUserFile.CreateManageRTCOffer`` — return the SDP answer for a data channel."""
    client = (req or {}).get("client_identity") or ctx.owner
    with _RTC_LOCK:
        _RTC_PEERS.setdefault(client, {"content": "", "sdp": "", "ts": time.time()})
        _RTC_PEERS[client]["sdp"] = (req or {}).get("sdp") or ""
    return {
        "sdp": (req or {}).get("sdp") or "",
        "encode_key": "",
        "name": "",
        "connection": "connected",
        "file_size": 0,
    }


def h_userfile_send_ice(ctx, req: dict) -> dict:
    client = (req or {}).get("client_identity") or ctx.owner
    candidate = (req or {}).get("candidate") or ""
    with _RTC_LOCK:
        if candidate:
            _RTC_CANDIDATES.setdefault(client, []).append(candidate)
    return {"client_identity": client, "candidate": candidate}


def h_userfile_get_ice(ctx, req: dict) -> dict:
    client = (req or {}).get("client_identity") or ctx.owner
    with _RTC_LOCK:
        queue = _RTC_CANDIDATES.get(client) or []
        _RTC_CANDIDATES[client] = []
    return {"client_identity": client, "candidate": queue}


def h_sftp_create(ctx, req: dict) -> dict:
    return h_sftp_update(ctx, req)


# ==================== registration table ====================

EXTRA_RPCS: Dict[tuple, dict] = {
    # PubUser
    ("PubUser", "GetSettings"): {"in": "Settings", "out": "Settings", "fn": h_get_settings},
    ("PubUser", "SetSettings"): {"in": "Settings", "out": "Settings", "fn": h_set_settings},
    ("PubUser", "GetAvailableExtendQuota"): {
        "in": "User", "out": "UserQuotaExtendInfo", "fn": h_available_extend_quota},

    # PubUserFile
    ("PubUserFile", "CreateUploadToken"): {"in": "File", "out": "UploadToken", "fn": h_create_upload_token},
    ("PubUserFile", "GetDownloadAndPreviewInfo"): {
        "in": "File", "out": "DownloadAndPreviewInfo", "fn": h_download_preview_info},
    ("PubUserFile", "ParseFileSlice"): {
        "in": "File", "out": "ParseFileSliceResponse", "fn": h_parse_file_slice},
    ("PubUserFile", "GetSliceDownloadAddress"): {
        "in": "SliceDownloadAddressRequest", "out": "SliceDownloadAddressResponse",
        "fn": h_slice_download_address},
    ("PubUserFile", "GetDirectDownloadAddress"): {
        "in": "DirectDownloadRequest", "out": "FileDownloadAddressResponse",
        "fn": h_direct_download_address},
    ("PubUserFile", "PreviewDoc"): {"in": "File", "out": "DocFilePreview", "fn": h_preview_doc},
    ("PubUserFile", "CreateDoc"): {"in": "File", "out": "DocFilePreview", "fn": h_preview_doc},
    ("PubUserFile", "ReportPreviewStatus"): {
        "in": "PreviewStatus", "out": "PreviewStatus", "fn": h_report_preview_status},
    ("PubUserFile", "ListRecentUpdatedFiles"): {
        "in": "ListRecentUpdatedFilesRequest", "out": "FileListResponse", "fn": h_list_recent},

    # PubSlice
    ("PubSlice", "GetSlice"): {"in": "Slice", "out": "Slice", "fn": h_slice_get},
    ("PubSlice", "CreateSlice"): {"in": "Slice", "out": "Slice", "fn": h_slice_create},
    ("PubSlice", "GetMeta"): {"in": "Meta", "out": "Meta", "fn": h_meta_get},
    ("PubSlice", "CreateMeta"): {"in": "Meta", "out": "Meta", "fn": h_meta_create},

    # PubDavConfig
    ("PubDavConfig", "Get"): {"in": "DavConfig", "out": "DavConfig", "fn": h_dav_get},
    ("PubDavConfig", "Update"): {"in": "DavConfig", "out": "DavConfig", "fn": h_dav_update},
    ("PubDavConfig", "Enable"): {"in": "DavConfig", "out": "DavConfig", "fn": h_dav_enable},
    ("PubDavConfig", "Disable"): {"in": "DavConfig", "out": "DavConfig", "fn": h_dav_disable},
    ("PubDavConfig", "Create"): {"in": "DavConfig", "out": "DavConfig", "fn": h_dav_create},
    ("PubDavConfig", "Delete"): {"in": "DavConfig", "out": "DavConfig", "fn": h_dav_delete},
    ("PubDavConfig", "List"): {"in": "DavConfigListRequest", "out": "DavConfigListResponse", "fn": h_dav_list},
    ("PubDavConfig", "ListAll"): {"in": "DavConfig", "out": "DavConfigListResponse", "fn": h_dav_list},
    ("PubDavConfig", "ValidateUserName"): {
        "in": "DavConfig", "out": "UserNameValidateResponse", "fn": h_dav_validate_username},
    ("PubDavConfig", "ServerInfo"): {"in": "DavConfig", "out": "DavServerInfo", "fn": h_dav_server_info},

    # PubSftpConfig
    ("PubSftpConfig", "Get"): {"in": "SftpConfig", "out": "SftpConfig", "fn": h_sftp_get},
    ("PubSftpConfig", "Update"): {"in": "SftpConfig", "out": "SftpConfig", "fn": h_sftp_update},
    ("PubSftpConfig", "Enable"): {"in": "SftpConfig", "out": "SftpConfig", "fn": h_sftp_enable},
    ("PubSftpConfig", "Disable"): {"in": "SftpConfig", "out": "SftpConfig", "fn": h_sftp_disable},
    ("PubSftpConfig", "UpdateKeys"): {"in": "SftpConfig", "out": "SftpConfig", "fn": h_sftp_update_keys},
    ("PubSftpConfig", "ValidateUserName"): {
        "in": "SftpConfig", "out": "UserNameValidateResponse", "fn": h_sftp_validate_username},

    # PubOfflineTask
    ("PubOfflineTask", "Parse"): {"in": "TaskParseRequest", "out": "TaskParseResponse", "fn": h_offline_parse},
    ("PubOfflineTask", "Add"): {"in": "UserTask", "out": "UserTask", "fn": h_offline_add},
    ("PubOfflineTask", "List"): {"in": "OfflineTaskListRequest", "out": "OfflineTaskListResponse",
                                 "fn": h_offline_list},
    ("PubOfflineTask", "Delete"): {"in": "OfflineTaskDeleteRequest", "out": "OfflineTaskDeleteResponse",
                                   "fn": h_offline_delete},

    # PubTicket
    ("PubTicket", "Create"): {"in": "Ticket", "out": "Ticket", "fn": h_ticket_create},
    ("PubTicket", "List"): {"in": "TicketListRequest", "out": "TicketListResponse", "fn": h_ticket_list},
    ("PubTicket", "Get"): {"in": "Ticket", "out": "Ticket", "fn": h_ticket_get},
    ("PubTicket", "Comment"): {"in": "Ticket", "out": "Ticket", "fn": h_ticket_comment},
    ("PubTicket", "Close"): {"in": "StringList", "out": "StringList", "fn": h_ticket_close},
    ("PubTicket", "Delete"): {"in": "StringList", "out": "StringList", "fn": h_ticket_delete},
    ("PubTicket", "GetContactInfo"): {"in": "ContactInfo", "out": "ContactInfo", "fn": h_contact_get},
    ("PubTicket", "UpdateContactInfo"): {"in": "ContactInfo", "out": "ContactInfo", "fn": h_contact_update},
    ("PubTicket", "ListConversation"): {
        "in": "ConversationListRequest", "out": "ConversationListResponse", "fn": h_conversation_list},

    # OauthAuthorization
    ("OauthAuthorization", "Authorize"): {"in": "AuthorizeRequest", "out": "AuthorizeResponse",
                                          "fn": h_oauth_authorize},
    ("OauthAuthorization", "DeviceCode"): {"in": "AuthorizeRequest", "out": "DeviceCodeAuthorizeResponse",
                                           "fn": h_oauth_device_code, "auth": False},
    ("OauthAuthorization", "GetAuthorizeState"): {"in": "AuthorizeState", "out": "AuthorizeState",
                                                  "fn": h_oauth_authorize_state, "auth": False},
    ("OauthAuthorization", "GetDeviceCodeState"): {
        "in": "DeviceCodeAuthorizeState", "out": "DeviceCodeAuthorizeState",
        "fn": h_oauth_device_state, "auth": False},
    ("OauthAuthorization", "GetToken"): {"in": "TokenRequest", "out": "TokenResponse", "fn": h_oauth_get_token},
    ("OauthAuthorization", "RefreshToken"): {"in": "RefreshTokenRequest", "out": "TokenResponse",
                                             "fn": h_oauth_refresh, "auth": False},

    # ---- PubUser: account lifecycle (register / password / sms / device) ----
    ("PubUser", "Register"): {"in": "RegisterRequest", "out": "User",
                              "fn": h_user_register, "auth": False},
    ("PubUser", "Logoff"): {"in": "Token", "out": "User", "fn": h_user_logoff},
    ("PubUser", "DeviceAuthorization"): {"in": "DeviceAuthorizationRequest", "out": "LoginResponse",
                                         "fn": h_user_device_authorization},
    ("PubUser", "ResetPassword"): {"in": "LoginRequest", "out": "User",
                                   "fn": h_user_reset_password, "auth": False},
    ("PubUser", "ChangePassword"): {"in": "ChangePasswordRequest", "out": "User",
                                    "fn": h_user_change_password},
    ("PubUser", "Delete"): {"in": "User", "out": "User", "fn": h_user_delete},
    ("PubUser", "Update"): {"in": "User", "out": "User", "fn": h_user_update},
    ("PubUser", "SendSmsVerifyCode"): {"in": "SmsVeifyCodeSendRequest", "out": "SmsVeifyCodeSendResponse",
                                       "fn": h_user_send_sms},
    ("PubUser", "SendSmsVerifyCodeNotUser"): {
        "in": "SmsVeifyCodeSendRequestNotUser", "out": "SmsVeifyCodeSendResponse",
        "fn": h_user_send_sms_not_user, "auth": False},
    ("PubUser", "VerifyAuthToken"): {"in": "LoginRequest", "out": "OauthTokenCheckResponse",
                                     "fn": h_user_verify_auth_token, "auth": False},
    ("PubUser", "VerifyAuthorizationCode"): {"in": "AuthorizationCode", "out": "Token",
                                             "fn": h_user_verify_authorization_code},
    ("PubUser", "CheckAuthStatus"): {"in": "LoginRequest", "out": "OauthTokenCheckResponse",
                                     "fn": h_user_check_auth_status, "auth": False},
    ("PubUser", "CreateAuthToken"): {"in": "LoginRequest", "out": "OauthTokenResponse",
                                     "fn": h_user_create_auth_token},
    ("PubUser", "ValidateUserInfo"): {"in": "UserValidateInfo", "out": "UserNameValidateResponse",
                                      "fn": h_user_validate_info, "auth": False},
    ("PubUser", "ExtendQuota"): {"in": "UserQuotaExtendInfo", "out": "UserStatisticsAndQuota",
                                 "fn": h_user_extend_quota},
    ("PubUser", "UserCenterUri"): {"in": "UserCenterUriRequest", "out": "UserCenterUriResponse",
                                   "fn": h_user_center_uri, "auth": False},

    # ---- PubUserFile: copy / update / batch ----
    ("PubUserFile", "Copy"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse",
                              "fn": h_file_copy},
    ("PubUserFile", "BatchRename"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse",
                                     "fn": h_file_batch_rename},
    ("PubUserFile", "BatchOperation"): {"in": "BatchOperationRequest", "out": "BatchOperationResponse",
                                        "fn": h_file_batch_operation},
    ("PubUserFile", "Update"): {"in": "File", "out": "File", "fn": h_file_update},

    # ---- PubUserFile: WebRTC data-channel signalling ----
    ("PubUserFile", "CreateManageRTCOffer"): {"in": "ManageRTCRequest", "out": "ManageRTCResponse",
                                              "fn": h_rtc_offer},
    ("PubUserFile", "SendClientIceCandidate"): {"in": "SendIceCandidateRequest",
                                                "out": "SendIceCandidateResponse",
                                                "fn": h_userfile_send_ice},
    ("PubUserFile", "GetServerIceCandidate"): {"in": "GetIceCandidateRequest",
                                               "out": "GetIceCandidateResponse",
                                               "fn": h_userfile_get_ice},

    # ---- PubFileShare: complaint management ----
    ("PubFileShare", "ListComplaint"): {"in": "ComplaintListRequest", "out": "ComplaintListResponse",
                                        "fn": h_share_list_complaint},
    ("PubFileShare", "DeleteComplaint"): {"in": "ComplaintDeleteRequest", "out": "ComplaintDeleteResponse",
                                          "fn": h_share_delete_complaint},

    # ---- PubSftpConfig: Create (aliased to Update) ----
    ("PubSftpConfig", "Create"): {"in": "SftpConfig", "out": "SftpConfig", "fn": h_sftp_create},

    # ---- v6.services.p2p.WebRTCManageService ----
    ("WebRTCManageService", "StartDownload"): {"in": "StartDownloadRequest",
                                               "out": "StartDownloadResponse",
                                               "fn": h_rtc_start_download},
    ("WebRTCManageService", "StopDownload"): {"in": "StopDownloadRequest",
                                              "out": "StopDownloadResponse",
                                              "fn": h_rtc_stop_download},
    ("WebRTCManageService", "Disconnect"): {"in": "DisconnectRequest", "out": "DisconnectResponse",
                                            "fn": h_rtc_disconnect},
    ("WebRTCManageService", "SendPeerIceCandidate"): {"in": "SendPeerIceCandidateRequest",
                                                      "out": "SendPeerIceCandidateResponse",
                                                      "fn": h_rtc_send_candidate},
    ("WebRTCManageService", "Receive"): {"in": "ReceiveRequest", "out": "ReceiveResponse",
                                         "fn": h_rtc_receive},
}
