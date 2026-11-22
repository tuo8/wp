"""
HTTP API Server
===============
FastAPI server exposing the storage engine over HTTP.
This is what mobile/desktop clients talk to.

Key endpoints:
- POST /api/upload         — upload a file (multipart), returns file metadata + chunk info
- POST /api/instant-check  — check if file can be instant-transferred (秒传)
- GET  /api/files          — list files in a directory
- GET  /api/files/{id}     — get file metadata
- GET  /api/download/{id}  — download a file (streams chunks reassembled)
- GET  /api/chunk/{cid}    — download a single chunk by CID (for P2P-style fetching)
- GET  /api/stats          — storage statistics
- DELETE /api/files/{id}   — delete a file
- POST /api/mkdir          — create a directory
"""

import os
import json
import time
import base64
import hashlib
import tempfile
from typing import Optional, List, Dict
from fastapi import FastAPI, UploadFile, File, HTTPException, Query, Request, Response, Body
from fastapi.responses import StreamingResponse, JSONResponse, FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pathlib import Path

from storage_engine import StorageEngine, FileMeta, quick_fingerprint
from cid_engine import compute_cid, parse_cid
from webdav_gateway import WebDAVGateway
from auth_manager import AuthManager
from sync_manager import SyncManager
from offline_engine import Aria2, OfflineEngine
try:
    from sftp_server import SftpService
except ImportError:
    SftpService = None
from slices_server import (build_slices_app, default_cache as edge_cache,
                           prewarm as prewarm_cdn, serve_block,
                           start_slices_server)
from share_manager import ShareManager
from p2p_signaling import P2PSignalingServer
from grpc_compat import GrpcCompatRouter
from fastapi.websockets import WebSocket
import protocol as proto
from extra_routes import mount_extra_routes
from commercial_security import throttled_chunk_iter

# Initialize
STORAGE_ROOT = os.environ.get("STORAGE_ROOT", "./storage_data")
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE", str(4 * 1024 * 1024)))

engine = StorageEngine(STORAGE_ROOT, chunk_size=CHUNK_SIZE)
auth = AuthManager(STORAGE_ROOT)
share = ShareManager(engine)

# ---- BT/HTTP 离线下载引擎（aria2 后端） ----
ARIA2_EXE = os.environ.get("ARIA2_EXE", str(
    Path(__file__).resolve().parent.parent / "tools" / "aria2"
    / "aria2-1.37.0-win-64bit-build1" / "aria2c.exe"))
OFFLINE_ROOT = os.path.join(STORAGE_ROOT, "offline")
aria2 = Aria2(
    ARIA2_EXE,
    port=int(os.environ.get("ARIA2_PORT", "6800")),
    secret=os.environ.get("ARIA2_SECRET", "mydrive"),
    download_root=os.path.join(OFFLINE_ROOT, "downloads"),
    meta_dir=os.path.join(OFFLINE_ROOT, "meta"),
    dht_port=int(os.environ.get("ARIA2_DHT_PORT", "6881")),
)
offline_engine = OfflineEngine(engine, OFFLINE_ROOT, aria2,
                               identity_factory=proto.new_identity)
offline_engine.start()
sync_mgr = SyncManager(engine)
p2p_server = P2PSignalingServer(engine)

app = FastAPI(title="MyDrive API", version="0.2.0")

# Mount WebDAV gateway (Basic auth against the same user database)
webdav = WebDAVGateway(
    app, engine, prefix="/webdav", auth=auth,
    auth_required=os.environ.get("MYDRIVE_AUTH_REQUIRED", "1").lower() not in ("0", "false", "no"),
)

# Mount the v6 protocol compatibility layer (gRPC-Web + REST gateway)
PRESIGN_SECRET = os.environ.get("MYDRIVE_PRESIGN_SECRET") or auth.secret_key
# ---- 独立 SFTP 端口（对齐 sftp://<host>:2022） ----
sftp_service = None
if SftpService is not None and os.environ.get("SFTP_ENABLE", "1").lower() not in ("0", "false", "no"):
    sftp_service = SftpService(
        engine, auth,
        cfg_path=os.path.join(STORAGE_ROOT, "json", "sftp.json"),
        port=int(os.environ.get("SFTP_PORT", "2022")),
        host_key=os.environ.get("SFTP_HOST_KEY", ""),
        bind=os.environ.get("SFTP_BIND", "0.0.0.0"),
    )
    sftp_service.start()

# ---- 切片专用出口（独立端口，模拟 slices.qzy.xyz） ----
SLICES_PORT = int(os.environ.get("SLICES_PORT", "8766"))
if not os.environ.get("SLICES_PUBLIC_URL"):
    os.environ["SLICES_PUBLIC_URL"] = f"http://127.0.0.1:{SLICES_PORT}"
proto.SLICES_PUBLIC_URL = os.environ["SLICES_PUBLIC_URL"].rstrip("/")
slices_app = build_slices_app(engine, PRESIGN_SECRET)
slices_server = None
if os.environ.get("SLICES_ENABLE", "1").lower() not in ("0", "false", "no"):
    slices_server = start_slices_server(
        slices_app,
        host=os.environ.get("SLICES_BIND", "0.0.0.0"),
        port=SLICES_PORT,
    )

grpc_compat = GrpcCompatRouter(app, engine, auth, presign_secret=PRESIGN_SECRET, share=share,
                               offline=offline_engine, sftp=sftp_service)


# ==================== MODELS ====================

class InstantCheckRequest(BaseModel):
    sha1: str
    head_sha1: str
    file_size: int
    name: str = ""


class InstantCheckResponse(BaseModel):
    instant_transfer: bool
    content_identity: Optional[str] = None
    file_identity: Optional[str] = None


class MkdirRequest(BaseModel):
    name: str
    parent: str = "root"


class FileInfo(BaseModel):
    """v6-aligned file record; keeps the legacy keys the web UI reads."""
    identity: str
    parent: str = "root"
    name: str
    path: str
    size: int
    mime_type: str
    type: int = 0
    is_dir: bool = False
    dir: bool = False
    content_identity: str = ""
    chunks: int = 0
    create_ts: int = 0
    update_ts: int = 0
    delete_ts: int = 0
    trashed: bool = False
    deleted: bool = False
    starred: bool = False
    store_type: int = 0
    version: int = 1
    sort_name: str = ""
    nodes: int = 0
    files: int = 0
    directories: int = 0


class ListInfo(BaseModel):
    token: str = ""
    total: int = 0
    limit: int = 0
    skip: int = 0


class ScanListResponse(BaseModel):
    files: List[FileInfo]
    list_info: ListInfo


class UploadInitRequest(BaseModel):
    name: str
    parent: str = "root"
    file_size: int = 0
    sha1: str = ""
    head_sha1: str = ""


class UploadCompleteRequest(BaseModel):
    task: str
    name: str
    parent: str = "root"
    size: int
    sha1: str = ""
    head_sha1: str = ""
    cids: List[str]


class StatsResponse(BaseModel):
    total_files: int
    total_chunks: int
    logical_bytes: int
    disk_bytes: int
    dedup_ratio: float
    fast_lookup_entries: int


# ==================== HELPERS ====================

AUTH_REQUIRED = os.environ.get("MYDRIVE_AUTH_REQUIRED", "1").lower() not in ("0", "false", "no")


def _bearer(request: Request) -> str:
    raw = request.headers.get("Authorization", "")
    if raw.lower().startswith("bearer "):
        return raw[7:].strip()
    # Allow token via query for streaming/download links opened in a new tab
    return request.query_params.get("access_token", "") or ""


def optional_user(request: Request) -> Optional[dict]:
    """Resolve the caller, or None when anonymous / invalid."""
    return auth.verify_token(_bearer(request))


def require_user(request: Request) -> dict:
    """Resolve the caller or reject with 401."""
    user = optional_user(request)
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Authentication required",
            headers={"WWW-Authenticate": "Bearer"},
        )
    return user


def caller_id(request: Request) -> Optional[str]:
    """User id for data isolation.

    With MYDRIVE_AUTH_REQUIRED=1 (default) every data endpoint demands a token.
    With it disabled (single-user local dev) anonymous access maps to 'anonymous'.
    """
    if AUTH_REQUIRED:
        return require_user(request)["user_id"]
    user = optional_user(request)
    return user["user_id"] if user else None


def meta_to_info(meta: FileMeta) -> FileInfo:
    return FileInfo(
        identity=meta.identity,
        parent=meta.parent,
        name=meta.name,
        path=meta.path,
        size=meta.size,
        mime_type=meta.mime_type,
        type=meta.type,
        is_dir=meta.is_dir,
        dir=meta.is_dir,
        content_identity=meta.content_identity,
        chunks=len(meta.chunks),
        create_ts=meta.create_ts,
        update_ts=meta.update_ts,
        delete_ts=meta.delete_ts,
        trashed=meta.trashed,
        deleted=meta.deleted,
        starred=meta.starred,
        store_type=meta.store_type,
        version=meta.version,
        sort_name=meta.sort_name,
        nodes=meta.nodes,
        files=meta.files,
        directories=meta.directories,
    )


def _commercial():
    return globals().get("extra_services", {}).get("commercial")

# ==================== ENDPOINTS ====================

@app.get("/api/stats")
async def get_stats(request: Request) -> StatsResponse:
    """Get storage statistics for the calling user."""
    stats = engine.get_storage_stats(user_id=caller_id(request))
    return StatsResponse(**stats)


@app.get("/api/quota")
async def get_quota(request: Request) -> dict:
    """Quota in the ``UserStatisticsAndQuota`` shape used by the v6 API."""
    uid = caller_id(request) or "anonymous"
    quota = auth.get_quota(uid)
    stats = engine.get_storage_stats(user_id=uid)
    total = int(quota.get("quota") or 0)
    used = int(quota.get("used") or 0)
    return {
        "disk_statistics_quota": {
            "bytes_quota": total,
            "bytes_used": used,
            "bytes_free": max(0, total - used),
            "files_quota": 100000,
            "files_used": int(stats.get("total_files", 0)),
            "bytes_trash_used": 0,
        },
        "traffic_statistics_quota": {
            "daily_bytes_download_quota": int(os.environ.get("MYDRIVE_DL_QUOTA", 1024 ** 4)),
            "daily_bytes_upload_quota": int(os.environ.get("MYDRIVE_UL_QUOTA", 10 * 1024 ** 3)),
            "bytes_downloaded_today": 0,
            "bytes_uploaded_today": 0,
        },
        "offline_task_statistics_quota": {
            "daily_tasks_quota": int(os.environ.get("MYDRIVE_OFFLINE_QUOTA", 100)),
            "tasks_commited_today": 0,
            "daily_bytes_quota": 0,
            "bytes_commited_today": 0,
        },
    }


@app.post("/api/instant-check")
async def instant_check(req: InstantCheckRequest, request: Request) -> InstantCheckResponse:
    """
    Check if a file can be instant-transferred (秒传).
    Only content already owned by the caller counts — cross-user hashes must
    never reveal that a file exists in someone else's account.
    """
    uid = caller_id(request)
    cid = engine.check_instant_transfer(req.sha1, req.head_sha1, req.file_size, user_id=uid)
    if cid:
        return InstantCheckResponse(instant_transfer=True, content_identity=cid)
    return InstantCheckResponse(instant_transfer=False)


@app.post("/api/upload")
async def upload_file(
    request: Request,
    file: UploadFile = File(...),
    parent: str = Query("root"),
    sha1: str = Query(""),
    head_sha1: str = Query(""),
) -> FileInfo:
    """Stream an upload to disk, enforce size/quota limits, then chunk it."""
    uid = caller_id(request)
    quota = auth.get_quota(uid) if uid else {"quota": 0, "used": 0}
    eff_quota = int(quota.get("quota") or 0)
    cm = _commercial()
    if uid and cm:
        eff_quota = max(eff_quota, int(cm.get_membership(uid).get("storage_quota_bytes", 0)))

    try:
        max_upload = max(0, int(os.environ.get("MAX_UPLOAD_BYTES", str(10 * 1024 ** 3))))
    except (TypeError, ValueError):
        max_upload = 10 * 1024 ** 3
    temp_dir = os.environ.get("UPLOAD_TEMP_DIR") or None
    if temp_dir:
        os.makedirs(temp_dir, exist_ok=True)
    upload_name = Path((file.filename or "upload").replace("\\", "/")).name or "upload"
    temp_path = None
    size = 0
    reserved = False
    stored = False
    try:
        with tempfile.NamedTemporaryFile(mode="wb", delete=False, dir=temp_dir, suffix=".mydrive-upload") as tmp:
            temp_path = tmp.name
            while True:
                block = await file.read(1024 * 1024)
                if not block:
                    break
                size += len(block)
                if max_upload and size > max_upload:
                    raise HTTPException(status_code=413, detail="上传文件超过服务器单文件大小限制")
                if uid and eff_quota and int(quota.get("used") or 0) + size > eff_quota:
                    raise HTTPException(status_code=413, detail="超出当前会员存储空间配额，请清理空间或升级 VIP")
                tmp.write(block)

        if uid:
            if not auth.reserve_usage(uid, size, eff_quota):
                raise HTTPException(status_code=413, detail="存储空间不足：并发上传后已超出配额")
            reserved = True

        meta = engine.chunk_file(temp_path, name=upload_name, parent=parent, user_id=uid or "anonymous")
        stored = True
        return meta_to_info(meta)
    except Exception:
        if reserved and uid and not stored:
            auth.update_usage(uid, -size)
        raise
    finally:
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except OSError:
                pass
        await file.close()


@app.get("/api/files")
async def list_files(
    request: Request,
    parent: str = Query("root"),
    limit: int = Query(0, description="0 = return the whole directory"),
    token: str = Query("", description="cursor returned by a previous call"),
) -> List[FileInfo]:
    """List files in a directory owned by the caller.

    Passing ``limit`` or ``token`` switches to cursor paging; the response is
    still a plain array so existing clients keep working. Use
    ``/api/files/scan`` when you need the paging cursor back.
    """
    uid = caller_id(request)
    if limit > 0 or token:
        result = engine.scan_list(parent, user_id=uid, limit=limit or 100,
                                  token=token or None)
        return [meta_to_info(m) for m in result["files"]]
    files = engine.list_files(parent, user_id=uid)
    return [meta_to_info(m) for m in files]


@app.get("/api/files/scan", response_model=ScanListResponse)
async def scan_files(
    request: Request,
    parent: str = Query("root"),
    limit: int = Query(100, ge=1, le=1000),
    token: str = Query(""),
    trashed: bool = Query(False),
) -> ScanListResponse:
    """Cursor-paged listing — the HTTP twin of ``PubUserFile.List``.

    Ordering is ``type ASC, create_ts DESC``; the returned ``list_info.token``
    is a base64(JSON) cursor, exactly like the upstream service.
    """
    uid = caller_id(request)
    result = engine.scan_list(parent, user_id=uid, limit=limit,
                              token=token or None, include_trashed=trashed)
    return ScanListResponse(
        files=[meta_to_info(m) for m in result["files"]],
        list_info=ListInfo(**result["list_info"]),
    )


@app.get("/api/files/{identity}")
async def get_file(identity: str, request: Request) -> FileInfo:
    """Get file metadata (only if the caller owns it)."""
    meta = engine.get_file_meta(identity, user_id=caller_id(request))
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    cm = _commercial()
    if cm and cm.is_cid_blocked(meta.content_identity):
        raise HTTPException(status_code=451, detail="该文件因违规已被限制访问")
    return meta_to_info(meta)


@app.get("/api/files/{identity}/chunks")
async def get_file_chunks(identity: str, request: Request):
    """Get the chunk CID list for a file so clients can download in parallel."""
    meta = engine.get_file_meta(identity, user_id=caller_id(request))
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    return {
        "identity": meta.identity,
        "name": meta.name,
        "size": meta.size,
        "content_identity": meta.content_identity,
        "chunks": [
            {
                "cid": c.cid,
                "index": c.index,
                "start": c.start,
                "end": c.end,
                "size": c.size,
            }
            for c in meta.chunks
        ],
    }


@app.get("/api/download/{identity}")
async def download_file(identity: str, request: Request):
    """Download a file. Streams chunks reassembled in order."""
    meta = engine.get_file_meta(identity, user_id=caller_id(request))
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    cm = _commercial()
    if cm and cm.is_cid_blocked(meta.content_identity):
        raise HTTPException(status_code=451, detail="该文件因违规已被限制访问")

    speed_bps = 0
    if cm:
        uid_dl = caller_id(request)
        if uid_dl:
            speed_bps = int(cm.get_membership(uid_dl).get("download_speed_bps", 0))

    def _raw_chunks():
        for chunk in meta.chunks:
            data = engine.get_chunk(chunk.cid)
            if data is None:
                break
            yield data

    def stream():
        yield from throttled_chunk_iter(_raw_chunks(), speed_bps)

    headers = {
        "Content-Disposition": f'attachment; filename="{meta.name}"',
        "Content-Length": str(meta.size),
    }
    return StreamingResponse(stream(), media_type=meta.mime_type, headers=headers)


@app.get("/api/chunk/{cid}")
async def download_chunk(cid: str, request: Request):
    """Download one chunk. Access is limited to users owning a file with this chunk."""
    uid = caller_id(request)
    if not engine.can_user_access_chunk(cid, user_id=uid):
        raise HTTPException(status_code=404, detail="Chunk not found")
    data = engine.get_chunk(cid)
    if data is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return StreamingResponse(
        iter([data]),
        media_type="application/octet-stream",
        headers={"Content-Length": str(len(data))},
    )


@app.post("/api/mkdir")
async def mkdir(req: MkdirRequest, request: Request) -> FileInfo:
    """Create a directory."""
    meta = engine.create_dir(req.name, req.parent, user_id=caller_id(request) or "anonymous")
    return meta_to_info(meta)


# ==================== PRESIGNED SLICES (v6 auth_key) ====================
#
# Upstream download flow:
#   GetFileSlicesDownloadInfo -> [{identity, download_address}, ...]
#   GET https://slices.qzy.xyz/ipfs/blocks/<h2>/<h2>/<cid>?auth_key=<exp>-<kid>-<user>-<sig>
#
# Here the same shape is served from /slices/blocks/<h2>/<h2>/<cid>.

UPLOAD_SESSIONS: Dict[str, dict] = proto.UPLOAD_SESSIONS


def _presign_secret() -> str:
    """Server-side signing key for slice URLs (shared with the token secret)."""
    return os.environ.get("MYDRIVE_PRESIGN_SECRET") or auth.secret_key


def _slice_page(meta: FileMeta, limit: int, token: str) -> dict:
    cursor = proto.decode_cursor(token) or {}
    skip = max(0, int(cursor.get("skip", 0) or 0))
    take = max(1, min(int(cursor.get("limit") or limit or 100), 1000))
    window = meta.chunks[skip:skip + take]

    slices = []
    for chunk in window:
        cred = proto.build_slice_query(chunk.cid, meta.user_id, _presign_secret(),
                                      ttl=proto.PRESIGN_TTL)
        slices.append({
            "identity": chunk.cid,
            "index": chunk.index,
            "size": chunk.size,
            "store_type": 0,
            "encrypt": 0,
            "download_token": cred["auth_key"],
            "download_address": proto.slice_url(chunk.cid, cred["auth_key"], "",
                                                cred["session"]),
            "expire_ts": proto.split_auth_key(cred["auth_key"])["expire_ts"],
        })

    next_skip = skip + len(slices)
    next_token = ""
    if next_skip < len(meta.chunks):
        next_token = proto.encode_cursor(meta.user_id, next_skip, take)
    return {
        "slices": slices,
        "list_info": {"token": next_token, "total": len(meta.chunks),
                      "limit": take, "skip": skip},
    }


@app.get("/api/files/{identity}/download-info")
async def file_download_info(
    identity: str,
    request: Request,
    limit: int = Query(100, ge=1, le=1000),
    token: str = Query(""),
) -> dict:
    """Presigned per-slice download descriptors (GetFileSlicesDownloadInfo)."""
    meta = engine.get_file_meta(identity, user_id=caller_id(request))
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    if meta.is_dir:
        raise HTTPException(status_code=400, detail="Directory has no slices")
    page = _slice_page(meta, limit, token)
    return {"file": meta_to_info(meta).model_dump(), **page}


@app.get("/ipfs/blocks/{h1}/{h2}/{cid}")
@app.head("/ipfs/blocks/{h1}/{h2}/{cid}")
@app.get("/slices/blocks/{h1}/{h2}/{cid}")
@app.head("/slices/blocks/{h1}/{h2}/{cid}")
async def download_slice(h1: str, h2: str, cid: str, request: Request):
    """切片出口：预签名 auth_key 即凭据，无 Bearer 也可下载（同线上）。"""
    return await serve_block(engine, _presign_secret(), cid, request, edge_cache())


# ==================== CDN CONTROL PLANE ====================

class PrewarmRequest(BaseModel):
    cids: List[str] = []
    identity: str = ""          # 也可以直接给文件 identity，自动展开其全部分片


@app.post("/api/cdn/prewarm")
async def cdn_prewarm(req: PrewarmRequest, request: Request) -> dict:
    """把块推进边缘缓存（对齐线上的缓存预热动作）。"""
    require_user(request)
    cids = list(req.cids or [])
    if req.identity:
        meta = engine.get_file_meta(req.identity, user_id=caller_id(request))
        if not meta:
            raise HTTPException(status_code=404, detail="file not found")
        cids.extend(c.cid for c in meta.chunks)
    return prewarm_cdn(edge_cache(), engine, cids)


@app.post("/api/cdn/purge")
async def cdn_purge(request: Request, cid: str = "") -> dict:
    """按 CID 失效单个块；不带 cid 则清空整个边缘缓存。"""
    require_user(request)
    removed = edge_cache().purge(cid)
    return {"purged": removed, "cid": cid, "node": edge_cache().stats()["node"]}


@app.get("/api/cdn/stats")
async def cdn_stats(request: Request) -> dict:
    require_user(request)
    return edge_cache().stats()


# ==================== RESUMABLE UPLOAD (CreateUploadTask) ====================

@app.post("/api/upload/init")
async def upload_init(req: UploadInitRequest, request: Request) -> dict:
    """Create an upload task — the twin of ``PubUserFile.CreateUploadTask``.

    Returns ``block_size`` / ``block_hash_type`` / ``block_version`` /
    ``block_codec`` with the exact values the live service uses
    (4 MiB, 0x12 sha2-256, 1, 0x55 raw).
    """
    uid = caller_id(request)
    owner = uid or "anonymous"

    # 秒传: known content for this user -> create the record, no upload needed
    if req.sha1:
        cid = engine.check_instant_transfer(req.sha1, req.head_sha1,
                                            req.file_size, user_id=uid)
        if cid:
            meta = engine.create_file_from_content(cid, req.name, req.parent,
                                                   user_id=owner, size=req.file_size)
            if meta:
                if uid:
                    auth.update_usage(uid, meta.size)
                return {
                    "created": True,
                    "instant": True,
                    "identity": meta.identity,
                    "content_identity": meta.content_identity,
                    "task": "",
                    "upload_address": "",
                    "block_size": proto.BLOCK_SIZE,
                    "block_hash_type": proto.BLOCK_HASH_TYPE,
                    "block_version": proto.BLOCK_VERSION,
                    "block_codec": proto.BLOCK_CODEC,
                }

    session = proto.new_upload_session()
    session_id = proto.parse_upload_session(session)
    UPLOAD_SESSIONS[session_id] = {
        "user_id": owner,
        "name": req.name,
        "parent": req.parent,
        "file_size": req.file_size,
        "sha1": req.sha1,
        "head_sha1": req.head_sha1,
        "blocks": {},
        "created_ts": int(time.time()),
    }
    return {
        "created": False,
        "instant": False,
        "task": session,
        "upload_address": "/int/blocks",
        "upload_token": session_id,
        "block_size": proto.BLOCK_SIZE,
        "block_hash_type": proto.BLOCK_HASH_TYPE,
        "block_version": proto.BLOCK_VERSION,
        "block_codec": proto.BLOCK_CODEC,
    }


# ---- 真实分片端点：/int/blocks/{task}/{cid}、/int/blocks/{task} ----
# 与线上 simple-file-proxy 完全同形：GET 探测已存在（秒传），
# POST 原始字节写块（201），最后 POST JSON 数组提交。

def _session_of(task: str, uid: Optional[str]) -> dict:
    session_id = proto.parse_upload_session(task)
    session = UPLOAD_SESSIONS.get(session_id or "")
    if not session or (uid and session["user_id"] != uid):
        raise HTTPException(status_code=404, detail="upload session not found")
    return session


@app.get("/int/blocks/{task}/{cid}")
async def blocks_probe(task: str, cid: str, request: Request):
    """秒传探测：块已存在时返回 200 + JSON。"""
    uid = caller_id(request)
    _session_of(task, uid)
    size = engine.chunk_size_of(cid)
    if size is None:
        raise HTTPException(status_code=404, detail="block not found")
    return {"cid": cid, "size": size}


@app.post("/int/blocks/{task}/{cid}")
async def blocks_put(task: str, cid: str, request: Request):
    """写入一个分片；cid 必须等于 body 的 sha256（raw codec）。"""
    uid = caller_id(request)
    session = _session_of(task, uid)
    data = await request.body()
    expected = compute_cid(data)
    if expected != cid:
        raise HTTPException(status_code=400, detail="invalid slice id")
    engine._store_chunk(cid, data)
    session["blocks"][len(session["blocks"])] = cid
    return Response(status_code=201)


@app.post("/int/blocks/{task}")
async def blocks_commit(task: str, request: Request):
    """提交分片 CID 列表，服务端组装 UnixFS 根节点并落库。"""
    uid = caller_id(request)
    session = _session_of(task, uid)
    try:
        cids = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid block list")
    if not isinstance(cids, list) or not cids:
        raise HTTPException(status_code=400, detail="invalid block list")
    cids = [str(c) for c in cids]

    missing = [c for c in cids if engine.chunk_size_of(c) is None]
    if missing:
        return JSONResponse(status_code=500,
                            content={"error": "failed to make file from blocks: block not found"})

    owner = session["user_id"]
    quota = auth.get_quota(uid) if uid else {"quota": 0, "used": 0}
    if uid and quota.get("quota") and quota["used"] + session["file_size"] > quota["quota"]:
        return JSONResponse(status_code=413, content={"error": "quota exceeded"})

    meta = engine.create_file_from_blocks(
        session["name"], cids, parent=session["parent"], user_id=owner,
        size=session["file_size"], sha1_hex=session.get("sha1", ""),
        head_sha1_hex=session.get("head_sha1", ""),
    )
    if uid:
        auth.update_usage(uid, meta.size)
    UPLOAD_SESSIONS.pop(proto.parse_upload_session(task), None)
    return {
        "user_identity": owner,
        "parent": meta.parent,
        "path": meta.path,
        "name": meta.name,
        "size": meta.size,
        "identity": meta.identity,
        "content_identity": meta.content_identity,
        "type": meta.type,
        "mime_type": meta.mime_type,
        "create_ts": meta.create_ts,
        "update_ts": meta.update_ts,
    }


@app.post("/api/upload/block")
async def upload_block(
    request: Request,
    task: str = Query(...),
    cid: str = Query(...),
    index: int = Query(-1),
):
    """Upload one 4 MiB slice. The CID must match the body's sha256."""
    uid = caller_id(request)
    session_id = proto.parse_upload_session(task)
    session = UPLOAD_SESSIONS.get(session_id or "")
    if not session or (uid and session["user_id"] != uid):
        raise HTTPException(status_code=404, detail="Upload session not found")

    data = await request.body()
    expected = compute_cid(data)
    if expected != cid:
        raise HTTPException(status_code=400,
                            detail=f"block hash mismatch (expected {expected})")

    engine._store_chunk(cid, data)
    slot = index if index >= 0 else len(session["blocks"])
    session["blocks"][slot] = cid
    return {
        "status": "ok",
        "cid": cid,
        "size": len(data),
        "received": len(session["blocks"]),
    }


@app.post("/api/upload/complete")
async def upload_complete(req: UploadCompleteRequest, request: Request) -> dict:
    """Stitch uploaded slices into a file record."""
    uid = caller_id(request)
    session_id = proto.parse_upload_session(req.task)
    session = UPLOAD_SESSIONS.get(session_id or "")
    if not session or (uid and session["user_id"] != uid):
        raise HTTPException(status_code=404, detail="Upload session not found")

    cids = req.cids or [session["blocks"][k] for k in sorted(session["blocks"])]
    if not cids:
        raise HTTPException(status_code=400, detail="No blocks uploaded")

    for cid in cids:
        if engine.chunk_size_of(cid) is None:
            raise HTTPException(status_code=400, detail=f"Missing block {cid}")

    quota = auth.get_quota(uid) if uid else {"quota": 0, "used": 0}
    if uid and quota.get("quota") and quota["used"] + req.size > quota["quota"]:
        raise HTTPException(status_code=413, detail="Quota exceeded")

    try:
        meta = engine.create_file_from_blocks(
            req.name, cids, parent=req.parent, user_id=session["user_id"],
            size=req.size, sha1_hex=req.sha1, head_sha1_hex=req.head_sha1,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if uid:
        auth.update_usage(uid, meta.size)
    UPLOAD_SESSIONS.pop(session_id, None)
    return {"status": "ok", "file": meta_to_info(meta).model_dump()}


@app.delete("/api/files/{identity}")
async def delete_file(identity: str, request: Request, permanent: bool = Query(False)):
    """Trash a file (or delete it permanently)."""
    uid = caller_id(request)
    meta = engine.get_file_meta(identity, user_id=uid)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    engine.set_trashed(identity, trashed=not permanent, permanent=permanent, user_id=uid)
    if uid and permanent:
        auth.update_usage(uid, -meta.size)
    return {"status": "ok", "permanent": permanent}


@app.post("/api/trash/{identity}/recover")
async def recover_file(identity: str, request: Request):
    """Restore a file from the recycle bin."""
    uid = caller_id(request)
    ok = engine.set_trashed(identity, trashed=False, user_id=uid)
    if not ok:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "ok", "trashed": False}


@app.get("/api/verify/{cid}")
async def verify_chunk(cid: str, request: Request):
    """Verify a chunk exists and return its size + hash."""
    uid = caller_id(request)
    if not engine.can_user_access_chunk(cid, user_id=uid):
        raise HTTPException(status_code=404, detail="Chunk not found")
    data = engine.get_chunk(cid)
    if data is None:
        raise HTTPException(status_code=404, detail="Chunk not found")
    return {
        "cid": cid,
        "size": len(data),
        "sha256": hashlib.sha256(data).hexdigest(),
        "exists": True,
    }


# ==================== FILE OPERATIONS ====================

class MoveRequest(BaseModel):
    identity: str
    new_parent: str


@app.post("/api/move")
async def move_file(req: MoveRequest, request: Request):
    """Move a file to a new parent directory (paths of children follow)."""
    uid = caller_id(request)
    meta = engine.move(req.identity, req.new_parent, user_id=uid)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "ok", "file": meta_to_info(meta).model_dump()}


class RenameRequest(BaseModel):
    identity: str
    new_name: str


@app.post("/api/rename")
async def rename_file(req: RenameRequest, request: Request):
    """Rename a file, refreshing path, type and sort key."""
    uid = caller_id(request)
    meta = engine.rename(req.identity, req.new_name, user_id=uid)
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    return {"status": "ok", "file": meta_to_info(meta).model_dump()}


# ==================== AUTH (OAuth2 + Multi-User) ====================

class RegisterRequest(BaseModel):
    username: str
    password: str

class LoginRequest(BaseModel):
    username: str
    password: str

class TokenRequest(BaseModel):
    refresh_token: str

@app.post("/api/auth/register")
async def register(req: RegisterRequest):
    """Register a new user."""
    result = auth.register(req.username, req.password)
    if not result["success"]:
        raise HTTPException(status_code=409, detail=result["error"])
    return result

@app.post("/api/auth/login")
async def login(req: LoginRequest):
    """Login and get access + refresh tokens."""
    result = auth.login(req.username, req.password)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result

@app.post("/api/auth/refresh")
async def refresh_token(req: TokenRequest):
    """Refresh an expired access token."""
    result = auth.refresh(req.refresh_token)
    if not result["success"]:
        raise HTTPException(status_code=401, detail=result["error"])
    return result

@app.post("/api/auth/device-code")
async def device_code():
    """Generate a device code for device flow (TV, IoT)."""
    return auth.generate_device_code()

@app.post("/api/auth/device-poll")
async def device_poll(device_code: str = ""):
    """Poll for device code authorization."""
    result = auth.poll_device_code(device_code)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result

@app.get("/api/auth/me")
async def get_me(request: Request):
    """Get current user info from token."""
    token = request.headers.get("Authorization", "").replace("Bearer ", "")
    user = auth.verify_token(token)
    if not user:
        raise HTTPException(status_code=401, detail="Invalid token")
    return {"username": user["username"], "user_id": user["user_id"], "quota": auth.get_quota(user["user_id"])}

@app.post("/api/auth/device-authorize")
async def device_authorize(device_code: str = "", username: str = "", password: str = ""):
    """Authorize a device code (user logs in to approve)."""
    result = auth.authorize_device(device_code, username, password)
    if not result["success"]:
        raise HTTPException(status_code=400, detail=result["error"])
    return result


# ==================== SHARE / TICKETS ====================

class ShareRequest(BaseModel):
    file_identity: str
    password: str = ""
    expire_hours: int = 0
    download_limit: int = 0

@app.post("/api/share")
async def create_share(req: ShareRequest, request: Request):
    """Create a share link for a file (owner only)."""
    uid = caller_id(request)
    result = share.create_share(req.file_identity, req.password, req.expire_hours,
                                req.download_limit, user_id=uid or "anonymous")
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Share failed"))
    return result

@app.get("/api/share/{share_id}")
async def get_share(share_id: str, password: str = ""):
    """Get shared file info (requires password if set)."""
    result = share.get_share(share_id, password)
    if not result["success"]:
        raise HTTPException(status_code=403 if "password" in result.get("error", "") else 404, detail=result["error"])
    return result

@app.get("/api/share/{share_id}/download")
async def download_shared(share_id: str, password: str = ""):
    """Download a shared file."""
    result = share.get_share(share_id, password)
    if not result["success"]:
        raise HTTPException(status_code=403, detail=result["error"])
    meta = engine.get_file_meta(result["file_identity"])
    if not meta:
        raise HTTPException(status_code=404, detail="File not found")
    cm = _commercial()
    if cm and cm.is_cid_blocked(meta.content_identity):
        raise HTTPException(status_code=451, detail="该分享文件因违规已被限制访问")
    def stream():
        for _, _, data in engine.stream_chunks(meta.identity):
            yield data
    share.increment_download(share_id)
    return StreamingResponse(stream(), media_type=meta.mime_type,
                           headers={"Content-Disposition": f'attachment; filename="{meta.name}"'})

@app.get("/api/shares")
async def list_shares(request: Request):
    """List share links created by the caller."""
    return share.list_shares(user_id=caller_id(request))


class ShareReactRequest(BaseModel):
    action: str = "like"  # like | dislike


@app.post("/api/share/{share_id}/react")
async def react_share(share_id: str, req: ShareReactRequest):
    """Like or dislike a share (PubFileShare.Like / Dislike)."""
    result = share.react(share_id, req.action)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Share not found"))
    return result


class ComplaintRequest(BaseModel):
    content: str = ""
    reason: str = ""


@app.post("/api/share/{share_id}/complaint")
async def complaint_share(share_id: str, req: ComplaintRequest, request: Request):
    """File a complaint against a share (PubFileShare.ComplaintShare)."""
    uid = caller_id(request) or "anonymous"
    result = share.add_complaint(share_id, uid, req.content or req.reason)
    if not result.get("success"):
        raise HTTPException(status_code=404, detail=result.get("error", "Share not found"))
    return result


@app.get("/api/complaints")
async def list_complaints(request: Request):
    """List complaints filed by the caller."""
    return share.list_complaints(user_id=caller_id(request))


class SaveShareRequest(BaseModel):
    password: str = ""
    parent: str = "root"
    name: str = ""


@app.post("/api/share/{share_id}/save")
async def save_share(share_id: str, req: SaveShareRequest, request: Request) -> FileInfo:
    """Save a shared file into the caller's own drive (PubFileShare.Save)."""
    uid = caller_id(request) or "anonymous"
    result = share.save_share(share_id, uid, req.password, req.parent, req.name)
    if not result.get("success"):
        raise HTTPException(status_code=403, detail=result.get("error", "Save failed"))
    return meta_to_info(result["file"])

@app.delete("/api/share/{share_id}")
async def delete_share(share_id: str, request: Request):
    """Delete a share link owned by the caller."""
    share.delete_share(share_id, user_id=caller_id(request))
    return {"status": "ok"}

class TicketRequest(BaseModel):
    subject: str
    body: str
    priority: str = "normal"

@app.post("/api/ticket")
async def create_ticket(req: TicketRequest, request: Request):
    """Create a support ticket for the caller."""
    user_id = caller_id(request) or "anonymous"
    return share.create_ticket(user_id, req.subject, req.body, req.priority)

@app.get("/api/tickets")
async def list_tickets(request: Request):
    """List tickets for the caller."""
    user_id = caller_id(request) or "anonymous"
    return share.list_tickets(user_id)

@app.get("/api/ticket/{ticket_id}")
async def get_ticket(ticket_id: str, request: Request):
    """Get ticket details (owner only)."""
    uid = caller_id(request)
    result = share.get_ticket(ticket_id)
    if not result or result.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Ticket not found")
    return result

class TicketReplyRequest(BaseModel):
    reply: str

@app.post("/api/ticket/{ticket_id}/reply")
async def reply_ticket(ticket_id: str, req: TicketReplyRequest, request: Request):
    """Reply to a support ticket (owner only)."""
    uid = caller_id(request)
    result = share.get_ticket(ticket_id)
    if not result or result.get("user_id") != uid:
        raise HTTPException(status_code=404, detail="Ticket not found")
    share.reply_ticket(ticket_id, req.reply)
    return {"status": "ok"}


# ==================== OFFLINE DOWNLOAD ====================

class OfflineDownloadRequest(BaseModel):
    url: str
    parent: str = "root"
    name: str = ""

def _offline_status_str(status: int) -> str:
    """v6 分桶 status -> 前端字符串。"""
    if status >= 1000:
        return "completed"
    if status >= 200:
        return "failed"
    if status >= 100:
        return "downloading"
    if status >= 0:
        return "pending"
    return "failed"


def _offline_task_json(task: dict) -> dict:
    status = int(task.get("status") or 0)
    return {
        "task_id": task.get("identity"),
        "identity": task.get("identity"),
        "task_identity": task.get("task_identity"),
        "name": task.get("name") or "",
        "url": task.get("url") or "",
        "status": _offline_status_str(status),
        "status_code": status,
        "progress": float(task.get("progress") or 0),
        "size": int(task.get("size") or 0),
        "bytes_total": int(task.get("bytes_total") or 0),
        "bytes_processed": int(task.get("bytes_processed") or 0),
        "save_path": task.get("save_path") or "/",
        "type": int(task.get("type") or 0),
        "message": task.get("message") or "",
        "create_ts": int(task.get("create_ts") or 0),
        "update_ts": int(task.get("update_ts") or 0),
    }


class OfflineAddRequest(BaseModel):
    url: str
    name: str = ""
    parent: str = "/"
    save_path: str = ""
    selected_files: Optional[List[int]] = None


@app.get("/api/endpoints")
async def service_endpoints(request: Request):
    """对外暴露的协议入口（Web UI 设置页用）。"""
    host = request.url.hostname or "127.0.0.1"
    http_port = int(os.environ.get("PORT", "8765"))
    return {
        "http": f"http://{host}:{http_port}",
        "webdav": f"http://{host}:{http_port}/webdav",
        "sftp": f"sftp://{host}:{os.environ.get('SFTP_PORT', '2022')}",
        "sftp_enabled": bool(sftp_service and sftp_service.ready),
        "slices": proto.SLICES_PUBLIC_URL or f"http://{host}:{SLICES_PORT}",
        "slices_port": SLICES_PORT,
        "aria2_port": int(os.environ.get("ARIA2_PORT", "6800")),
    }


@app.post("/api/offline/add")
async def add_offline_download(req: OfflineAddRequest, request: Request):
    """添加离线下载：支持 magnet / .torrent / HTTP(S) 直链。"""
    uid = caller_id(request) or "anonymous"
    cm = _commercial()
    if cm:
        mem = cm.get_membership(uid)
        ost = offline_engine.stats(uid)
        if ost.get("tasks_commited_today", 0) >= mem["daily_offline_quota"]:
            raise HTTPException(status_code=429, detail="已达到今日离线下载次数上限，请升级 VIP")
    payload = {
        "url": req.url,
        "name": req.name or "",
        "save_path": req.save_path or req.parent or "/",
        "selected_files": req.selected_files or [],
    }
    try:
        task = offline_engine.add(payload, uid)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"success": True, "instant_saved": bool(task.get("instant_saved")), **_offline_task_json(task)}


@app.get("/api/offline/list")
async def list_offline_downloads(request: Request):
    """列出调用者的离线下载任务（按创建时间倒序）。"""
    uid = caller_id(request) or "anonymous"
    return [_offline_task_json(t) for t in offline_engine.list(uid, 200)]


@app.get("/api/offline/{task_id}")
async def get_offline_status(task_id: str, request: Request):
    uid = caller_id(request) or "anonymous"
    for t in offline_engine.list(uid, 500):
        if t["identity"] == task_id:
            return _offline_task_json(t)
    raise HTTPException(status_code=404, detail="Task not found")


@app.delete("/api/offline/{task_id}")
async def cancel_offline_download(task_id: str, request: Request):
    uid = caller_id(request) or "anonymous"
    count = offline_engine.delete([task_id], uid, delete_files=False)
    if not count:
        raise HTTPException(status_code=404, detail="Task not found")
    return {"status": "ok", "deleted": count}


# ==================== SYNC (Local <-> Cloud) ====================

class SyncAddRequest(BaseModel):
    local_path: str
    cloud_parent: str = "root"
    direction: str = "upload"  # upload, download, bidirectional

@app.post("/api/sync/add")
async def add_sync_task(req: SyncAddRequest, request: Request):
    """Add a sync pair (local folder <-> cloud folder)."""
    result = sync_mgr.add_pair(req.local_path, req.cloud_parent, req.direction,
                              user_id=caller_id(request) or "anonymous")
    return result

@app.get("/api/sync/list")
async def list_sync_tasks(request: Request):
    """List sync pairs owned by the caller."""
    return sync_mgr.list_pairs(user_id=caller_id(request))

@app.post("/api/sync/run/{pair_id}")
async def run_sync(pair_id: str, request: Request):
    """Run sync for a specific pair."""
    return sync_mgr.sync_pair(pair_id, user_id=caller_id(request))

@app.post("/api/sync/run-all")
async def run_all_sync(request: Request):
    """Run all sync pairs owned by the caller."""
    uid = caller_id(request)
    results = []
    for pair in sync_mgr.list_pairs(user_id=uid):
        results.append(sync_mgr.sync_pair(pair["id"], user_id=uid))
    return {"results": results}

@app.delete("/api/sync/{pair_id}")
async def delete_sync(pair_id: str, request: Request):
    """Delete a sync pair."""
    sync_mgr.delete_pair(pair_id, user_id=caller_id(request))
    return {"status": "ok"}

@app.get("/api/sync/status")
async def sync_status():
    """Get overall sync status."""
    return sync_mgr.get_status()


# ==================== P2P (WebRTC Signaling) ====================

@app.get("/api/p2p/peers")
async def list_p2p_peers(request: Request):
    """List all online peers."""
    caller_id(request)
    return {"peers": p2p_server.list_peers(), "count": len(p2p_server.peers)}

@app.get("/api/p2p/peers/{cid}")
async def find_peers_for_chunk(cid: str, request: Request):
    """Find which peers have a specific chunk."""
    caller_id(request)
    return {"cid": cid, "peers": p2p_server.get_peers_for_cid(cid)}

class AnnounceRequest(BaseModel):
    peer_id: str
    cids: List[str]

@app.post("/api/p2p/announce")
async def announce_chunks(req: AnnounceRequest, request: Request):
    """Announce chunks the caller actually owns, so others can fetch them."""
    uid = caller_id(request)
    owned = [c for c in req.cids if engine.can_user_access_chunk(c, user_id=uid)]
    await p2p_server.announce_chunks(req.peer_id, owned)
    return {"status": "ok", "announced": len(owned), "rejected": len(req.cids) - len(owned)}

@app.websocket("/ws/p2p")
async def p2p_websocket(ws: WebSocket):
    """WebSocket endpoint for WebRTC signaling (SDP/ICE exchange).

    Browsers cannot set headers on a WebSocket handshake, so the access token
    travels as a query parameter and is verified before the socket is accepted.
    """
    if AUTH_REQUIRED:
        user = auth.verify_token(ws.query_params.get("access_token", ""))
        if not user:
            await ws.close(code=4401)
            return
    await p2p_server.handle_websocket(ws)


# ==================== EXTRA ROUTES (PIKPAK / HALALCLOUD FULL FEATURES) ====================
extra_services = mount_extra_routes(
    app=app,
    engine=engine,
    auth=auth,
    share=share,
    offline_engine=offline_engine,
    storage_root=STORAGE_ROOT,
    caller_id_fn=caller_id,
    meta_to_info_fn=meta_to_info,
    offline_task_json_fn=_offline_task_json,
)

# ==================== WEB UI ====================

static_dir = Path(__file__).parent / "static"

@app.get("/", response_class=HTMLResponse)
async def web_ui():
    """Serve the main web application."""
    return FileResponse(static_dir / "index.html")


# Mount static files (for any future assets)
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ==================== MAIN ====================

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", "8765"))
    print(f"\n{'='*60}")
    print(f"  MyDrive — IPFS Content-Addressed Cloud Drive")
    print(f"  Storage root: {STORAGE_ROOT}")
    print(f"  Chunk size: {CHUNK_SIZE:,} bytes ({CHUNK_SIZE // 1024 // 1024}MB)")
    print(f"  Web UI: http://127.0.0.1:{port}")
    print(f"  API docs: http://127.0.0.1:{port}/docs")
    print(f"{'='*60}\n")
    uvicorn.run(app, host="127.0.0.1", port=port)
