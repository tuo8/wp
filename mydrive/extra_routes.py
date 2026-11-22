"""
Extra Routes — PikPak & HalalCloud Full Feature Surface
=======================================================
Mounts all enhanced APIs onto the main FastAPI `app`:
- Tiered large-file storage (S3/R2/MinIO/NAS cold tier, LRU cache, compression, GC)
- Trash empty & auto-expiry cleanup
- Smart categories (video/image/audio/doc/archive/starred), global search, star, real copy,
  batch operations, batch ZIP download, file version snapshots & rollback
- Cloud Media streaming (FFmpeg HLS transcode/remux, thumbnails, preview sprite,
  auto-matched WebVTT subtitles, playback progress/speed memory)
- Enhanced Offline Downloads (parse file tree, selective download, batch add,
  pause/resume, speed limit, global instant-save cache)
- PikPak-style Telegram Bot adapter (webhook + simulator + config)
- Standalone Public Share Page (`/s/{share_id}`) with password prompt & one-click save
"""

from __future__ import annotations

import io
import os
import urllib.parse
import zipfile
from pathlib import Path
from typing import Any, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from media_service import MediaService
from telegram_bot import TelegramBotAdapter
from commercial_manager import CommercialManager, enable_sqlite_wal
from cluster_infra import ClusterCoordinator
from commercial_security import AuditLogger, RateLimiter, RateLimitMiddleware, collect_health, export_prometheus_metrics
from tiered_storage import S3CompatBackend


class TieredConfigRequest(BaseModel):
    cold_type: Optional[str] = None
    cold_dir: Optional[str] = None
    s3_endpoint: Optional[str] = None
    s3_bucket: Optional[str] = None
    s3_access_key: Optional[str] = None
    s3_secret_key: Optional[str] = None
    s3_region: Optional[str] = None
    s3_prefix: Optional[str] = None
    local_cache_max_bytes: Optional[int] = None
    compression_enabled: Optional[bool] = None
    auto_offload: Optional[bool] = None
    trash_retention_days: Optional[int] = None


class CopyRequest(BaseModel):
    identity: str
    new_parent: Optional[str] = None
    new_name: Optional[str] = None


class BatchRequest(BaseModel):
    identities: List[str]
    operation: str  # move | copy | trash | delete | recover | star | unstar
    dest: Optional[str] = None


class BatchDownloadRequest(BaseModel):
    identities: List[str]


class StarRequest(BaseModel):
    starred: Optional[bool] = None


class SnapshotRequest(BaseModel):
    note: str = ""


class PlaybackProgressRequest(BaseModel):
    position_sec: float
    duration_sec: float = 0.0
    playback_rate: float = 1.0
    quality: str = "original"


class OfflineParseRequest(BaseModel):
    url: str
    save_path: str = "/"


class OfflineBatchRequest(BaseModel):
    urls: List[str]
    save_path: str = "/"


class OfflineSpeedLimitRequest(BaseModel):
    max_download: str = "0"
    max_upload: str = "0"


class OfflineInstantRegisterRequest(BaseModel):
    url: str
    file_identity: str
    name: str = ""


class TelegramConfigRequest(BaseModel):
    bot_token: Optional[str] = None
    default_save_path: Optional[str] = None
    webhook_secret: Optional[str] = None
    polling_enabled: Optional[bool] = None



class RedeemCodeGenRequest(BaseModel):
    plan: str = "vip"
    duration_days: int = 30
    count: int = 1


class CreateOrderRequest(BaseModel):
    plan: str = "vip"
    duration_days: int = 30
    provider: str = "generic"


class RedeemRequest(BaseModel):
    code: str


class ModerateRequest(BaseModel):
    reason: str = "violation"


class PresignUploadRequest(BaseModel):
    cid: str
    expires: int = 900

class TelegramSimulateRequest(BaseModel):
    chat_id: str = "demo_chat"
    text: str


def mount_extra_routes(
    app: FastAPI,
    engine,
    auth,
    share,
    offline_engine,
    storage_root: str,
    caller_id_fn,
    meta_to_info_fn,
    offline_task_json_fn,
):
    """Attach all enhanced PikPak / HalalCloud routes to `app`."""
    media = MediaService(engine, storage_root)
    tg_bot = TelegramBotAdapter(engine, auth, offline_engine, storage_root)
    commercial = CommercialManager(storage_root)
    audit = AuditLogger(storage_root)
    cluster = ClusterCoordinator()
    if os.environ.get('RATE_LIMIT_ENABLE', '1').lower() not in ('0', 'false', 'no'):
        app.add_middleware(RateLimitMiddleware, limiter=RateLimiter(), cluster=cluster)
    # Enable SQLite WAL mode for multi-user concurrency across core DBs
    enable_sqlite_wal(engine.db_path)
    if hasattr(auth, "db_path"):
        enable_sqlite_wal(Path(auth.db_path))


    def _is_admin_user(request: Request) -> bool:
        uid = caller_id_fn(request) or ""
        if not uid:
            return False
        env_admins = [x.strip().lower() for x in os.environ.get("ADMIN_USERS", "").split(",") if x.strip()]
        if not env_admins:
            # Fail closed: never promote the first public registrant to administrator.
            return False
        username = ""
        try:
            user = auth.get_user(uid) if hasattr(auth, "get_user") else None
            if isinstance(user, dict):
                username = (user.get("username") or "").lower()
        except Exception:
            pass
        return uid.lower() in env_admins or (bool(username) and username in env_admins)

    def _require_admin(request: Request):
        if not _is_admin_user(request):
            raise HTTPException(status_code=403, detail="需要管理员权限（可通过 ADMIN_USERS 环境变量配置）")

    # Background maintenance daemon: uses cluster distributed lock so only 1 replica runs GC
    def _maintenance_loop():
        import time as _t
        interval = max(60, int(os.environ.get("MAINTENANCE_INTERVAL_SEC", "3600")))
        while True:
            _t.sleep(interval)
            tok = cluster.acquire_lock("maintenance_gc", ttl_sec=300)
            if not tok:
                continue
            try:
                engine.purge_expired_trash()
                engine.gc_chunks()
            except Exception:
                pass
            finally:
                cluster.release_lock("maintenance_gc", tok)
    import threading as _th
    _th.Thread(target=_maintenance_loop, daemon=True, name="mydrive-maintenance").start()

    # ==================== TIERED STORAGE & SPACE SAVING ====================

    @app.get("/api/storage/tiered/config")
        _require_admin(request)
    async def get_tiered_config(request: Request):
        caller_id_fn(request)
        return engine.tiered.get_public_config()

    @app.post("/api/storage/tiered/config")
        _require_admin(request)
    async def update_tiered_config(req: TieredConfigRequest, request: Request):
        caller_id_fn(request)
        updates = {k: v for k, v in (req.model_dump() if hasattr(req, 'model_dump') else req.dict()).items() if v is not None}
        # Preserve existing secret if masked placeholder sent back
        if updates.get("s3_secret_key") == "***":
            updates.pop("s3_secret_key", None)
        return engine.tiered.save_config(updates)

    @app.get("/api/storage/tiered/stats")
        _require_admin(request)
    async def get_tiered_stats(request: Request):
        uid = caller_id_fn(request)
        base = engine.get_storage_stats(user_id=uid)
        tiered = engine.tiered.get_stats()
        return {**base, "tiered": tiered}

    @app.post("/api/storage/tiered/offload")
        _require_admin(request)
    async def offload_cold(request: Request, evict_local: bool = True):
        caller_id_fn(request)
        return engine.tiered.offload_all_to_cold(evict_local=evict_local)

    @app.post("/api/storage/gc")
        _require_admin(request)
    async def run_chunk_gc(request: Request):
        caller_id_fn(request)
        return engine.gc_chunks()

    @app.post("/api/trash/empty")
    async def empty_trash(request: Request):
        uid = caller_id_fn(request)
        return engine.empty_trash(user_id=uid)

    @app.post("/api/trash/purge-expired")
        _require_admin(request)
    async def purge_expired_trash(request: Request, days: Optional[int] = None):
        caller_id_fn(request)
        return engine.purge_expired_trash(retention_days=days)

    # ==================== SEARCH, CATEGORIES, STAR, COPY, BATCH, VERSIONS ====================

    @app.get("/api/search")
    async def global_search_files(
        request: Request,
        q: str = Query(..., min_length=1),
        limit: int = Query(100, ge=1, le=500),
    ):
        uid = caller_id_fn(request)
        res = engine.search_files(q, user_id=uid, limit=limit)
        if isinstance(res, dict):
            files = res.get("files", [])
        elif isinstance(res, tuple):
            files = res[0]
        else:
            files = res
        return [meta_to_info_fn(f) for f in files]

    @app.get("/api/category/{category}")
    async def list_category_files(
        category: str,
        request: Request,
        limit: int = Query(200, ge=1, le=1000),
    ):
        uid = caller_id_fn(request)
        files = engine.list_by_category(category, user_id=uid, limit=limit)
        return [meta_to_info_fn(f) for f in files]

    @app.post("/api/files/{identity}/star")
    async def star_file(identity: str, req: StarRequest, request: Request):
        uid = caller_id_fn(request)
        meta = engine.toggle_star(identity, starred=req.starred, user_id=uid)
        if not meta:
            raise HTTPException(status_code=404, detail="File not found")
        return meta_to_info_fn(meta)

    @app.post("/api/copy")
    async def copy_file_api(req: CopyRequest, request: Request):
        uid = caller_id_fn(request)
        meta = engine.copy(req.identity, new_parent=req.new_parent, new_name=req.new_name, user_id=uid)
        if not meta:
            raise HTTPException(status_code=404, detail="File not found")
        return meta_to_info_fn(meta)

    @app.post("/api/batch")
    async def batch_files_api(req: BatchRequest, request: Request):
        uid = caller_id_fn(request)
        op = req.operation.lower()
        if op in ("star", "unstar"):
            affected = 0
            for fid in req.identities:
                if engine.toggle_star(fid, starred=(op == "star"), user_id=uid):
                    affected += 1
            return {"status": "ok", "operation": op, "affected": affected}
        affected = engine.batch(req.identities, op, dest=req.dest, user_id=uid)
        if op == "delete":
            engine.gc_chunks()
        return {"status": "ok", "operation": op, "affected": affected}

    @app.post("/api/batch/download")
    async def batch_zip_download(req: BatchDownloadRequest, request: Request):
        uid = caller_id_fn(request)
        if not req.identities:
            raise HTTPException(status_code=400, detail="identities required")

        buf = io.BytesIO()
        added = 0

        def _add_node(zf: zipfile.ZipFile, fid: str, prefix: str):
            nonlocal added
            meta = engine.get_file_meta(fid, user_id=uid)
            if not meta or meta.deleted:
                return
            arcname = f"{prefix}{meta.name}"
            if meta.is_dir:
                for child in engine.list_files(parent=meta.identity, user_id=uid):
                    _add_node(zf, child.identity, arcname + "/")
            else:
                data = b"".join((c[2] if isinstance(c, tuple) else c) for c in engine.stream_chunks(meta.identity, user_id=uid))
                zf.writestr(arcname, data)
                added += 1

        with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for fid in req.identities:
                _add_node(zf, fid, "")

        if added == 0:
            raise HTTPException(status_code=404, detail="No downloadable files found")
        buf.seek(0)
        return StreamingResponse(
            buf,
            media_type="application/zip",
            headers={"Content-Disposition": 'attachment; filename="mydrive_batch.zip"'},
        )

    @app.get("/api/files/{identity}/versions")
    async def list_file_versions(identity: str, request: Request):
        uid = caller_id_fn(request)
        meta = engine.get_file_meta(identity, user_id=uid)
        if not meta:
            raise HTTPException(status_code=404, detail="File not found")
        return {
            "current_version": meta.version,
            "current_cid": meta.content_identity,
            "versions": engine.list_versions(identity, user_id=uid),
        }

    @app.post("/api/files/{identity}/versions/snapshot")
    async def create_file_snapshot(identity: str, req: SnapshotRequest, request: Request):
        uid = caller_id_fn(request)
        snap = engine.save_version_snapshot(identity, note=req.note, user_id=uid)
        if not snap:
            raise HTTPException(status_code=404, detail="File not found")
        return snap

    @app.post("/api/files/{identity}/versions/{version_id}/restore")
    async def restore_file_version(identity: str, version_id: int, request: Request):
        uid = caller_id_fn(request)
        meta = engine.restore_version(identity, version_id, user_id=uid)
        if not meta:
            raise HTTPException(status_code=404, detail="Version not found")
        return meta_to_info_fn(meta)

    # ==================== CLOUD MEDIA & STREAMING (PIKPAK CLOUD PLAY) ====================

    @app.get("/api/media/{identity}/probe")
    async def probe_media_api(identity: str, request: Request):
        uid = caller_id_fn(request)
        info = media.probe_media(identity, user_id=uid)
        if "error" in info:
            raise HTTPException(status_code=404, detail=info["error"])
        info["subtitles"] = media.find_subtitles(identity, user_id=uid)
        info["progress"] = media.get_progress(uid or "anonymous", identity)
        return info

    @app.get("/api/media/{identity}/hls/{quality}/index.m3u8")
    async def hls_playlist_api(identity: str, quality: str, request: Request):
        uid = caller_id_fn(request)
        out_dir = media.ensure_hls(identity, quality, user_id=uid)
        if not out_dir:
            raise HTTPException(status_code=503, detail="HLS transcode unavailable (check ffmpeg or file)")
        playlist_text = (out_dir / "index.m3u8").read_text(encoding="utf-8")
        token = request.query_params.get("access_token", "")
        if token:
            lines = []
            for line in playlist_text.splitlines():
                if line.endswith(".ts"):
                    line = f"{line}?access_token={urllib.parse.quote(token)}"
                lines.append(line)
            playlist_text = "\n".join(lines) + "\n"
        return PlainTextResponse(playlist_text, media_type="application/vnd.apple.mpegurl")

    @app.get("/api/media/{identity}/hls/{quality}/{segment}")
    async def hls_segment_api(identity: str, quality: str, segment: str, request: Request):
        uid = caller_id_fn(request)
        if "/" in segment or ".." in segment or not segment.endswith(".ts"):
            raise HTTPException(status_code=400, detail="Invalid segment")
        out_dir = media.ensure_hls(identity, quality, user_id=uid)
        if not out_dir:
            raise HTTPException(status_code=404, detail="Segment not found")
        seg_path = out_dir / segment
        if not seg_path.exists():
            raise HTTPException(status_code=404, detail="Segment not found")
        return FileResponse(seg_path, media_type="video/mp2t")

    @app.get("/api/media/{identity}/thumbnail")
    async def media_thumbnail_api(identity: str, request: Request):
        uid = caller_id_fn(request)
        thumb = media.get_thumbnail(identity, user_id=uid)
        if not thumb:
            raise HTTPException(status_code=404, detail="Thumbnail unavailable")
        return FileResponse(thumb, media_type="image/jpeg")

    @app.get("/api/media/{identity}/sprite")
    async def media_sprite_api(identity: str, request: Request):
        uid = caller_id_fn(request)
        sprite = media.get_preview_sprite(identity, user_id=uid)
        if not sprite:
            raise HTTPException(status_code=404, detail="Sprite unavailable")
        return FileResponse(sprite, media_type="image/jpeg")

    @app.get("/api/media/{identity}/subtitles")
    async def media_subtitles_api(identity: str, request: Request):
        uid = caller_id_fn(request)
        return {"subtitles": media.find_subtitles(identity, user_id=uid)}

    @app.get("/api/media/{identity}/subtitle.vtt")
    async def media_subtitle_vtt_api(identity: str, request: Request):
        uid = caller_id_fn(request)
        vtt = media.get_subtitle_vtt(identity, user_id=uid)
        if vtt is None:
            raise HTTPException(status_code=404, detail="Subtitle not found")
        return PlainTextResponse(vtt, media_type="text/vtt; charset=utf-8")

    @app.get("/api/media/{identity}/progress")
    async def get_playback_progress(identity: str, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        return media.get_progress(uid, identity)

    @app.post("/api/media/{identity}/progress")
    async def save_playback_progress(identity: str, req: PlaybackProgressRequest, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        return media.save_progress(
            uid, identity, req.position_sec, req.duration_sec, req.playback_rate, req.quality
        )

    # ==================== OFFLINE ENHANCEMENTS ====================

    @app.post("/api/offline/parse")
    async def parse_offline_url(req: OfflineParseRequest, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        try:
            return offline_engine.parse(req.url, save_path=req.save_path, user_id=uid, timeout=10.0)
        except Exception as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/offline/batch")
    async def add_offline_batch(req: OfflineBatchRequest, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        results = offline_engine.add_batch(req.urls, req.save_path, uid)
        formatted = []
        for r in results:
            if r["ok"]:
                formatted.append({"ok": True, **offline_task_json_fn(r["task"]), "instant_saved": bool(r["task"].get("instant_saved"))})
            else:
                formatted.append(r)
        return {"count": len(formatted), "results": formatted}

    @app.post("/api/offline/{task_id}/pause")
    async def pause_offline_task(task_id: str, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        if not offline_engine.pause(task_id, uid):
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "paused", "task_id": task_id}

    @app.post("/api/offline/{task_id}/resume")
    async def resume_offline_task(task_id: str, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        if not offline_engine.resume(task_id, uid):
            raise HTTPException(status_code=404, detail="Task not found")
        return {"status": "resumed", "task_id": task_id}

    @app.post("/api/offline/speed-limit")
    async def set_offline_speed_limit(req: OfflineSpeedLimitRequest, request: Request):
        caller_id_fn(request)
        return offline_engine.set_speed_limit(req.max_download, req.max_upload)

    @app.post("/api/offline/instant-register")
    async def register_offline_instant(req: OfflineInstantRegisterRequest, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        try:
            task = offline_engine.register_instant_cache(req.url, req.file_identity, uid, name=req.name)
            return {"status": "ok", **offline_task_json_fn(task)}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    # ==================== TELEGRAM BOT ADAPTER ====================

    @app.get("/api/telegram/config")
    async def get_telegram_config(request: Request):
        caller_id_fn(request)
        return tg_bot.get_public_config()

    @app.post("/api/telegram/config")
    async def save_telegram_config(req: TelegramConfigRequest, request: Request):
        caller_id_fn(request)
        updates = {k: v for k, v in (req.model_dump() if hasattr(req, 'model_dump') else req.dict()).items() if v is not None}
        return tg_bot.save_config(updates)

    @app.post("/api/telegram/webhook")
    async def telegram_webhook(request: Request):
        secret = tg_bot.config.get("webhook_secret")
        if secret and request.headers.get("X-Telegram-Bot-Api-Secret-Token") != secret:
            raise HTTPException(status_code=403, detail="Invalid webhook secret")
        payload = await request.json()
        return tg_bot.handle_update(payload, send_reply=True)

    @app.post("/api/telegram/simulate")
    async def telegram_simulate(req: TelegramSimulateRequest, request: Request):
        caller_id_fn(request)
        update = {"message": {"chat": {"id": req.chat_id}, "text": req.text}}
        return tg_bot.handle_update(update, send_reply=False)

    # ==================== STANDALONE PUBLIC SHARE PAGE ====================

    @app.get("/s/{share_id}", response_class=HTMLResponse)
    async def public_share_page(share_id: str):
        html = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="UTF-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>MyDrive 分享文件</title>
<style>
  body{{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f5f7;margin:0;display:flex;align-items:center;justify-content:center;min-height:100vh;color:#1d1d1f}}
  .card{{background:#fff;border-radius:16px;box-shadow:0 8px 30px rgba(0,0,0,.08);padding:28px;width:92%;max-width:480px}}
  h1{{font-size:20px;margin:0 0 8px}}
  .meta{{font-size:13px;color:#6e6e73;margin-bottom:18px}}
  input{{width:100%;padding:10px 12px;border:1px solid #d2d2d7;border-radius:8px;font-size:14px;box-sizing:border-box;margin-bottom:12px}}
  .row{{display:flex;gap:10px;flex-wrap:wrap}}
  button,a.btn{{flex:1;padding:10px 14px;border-radius:8px;border:none;font-size:14px;cursor:pointer;text-align:center;text-decoration:none;font-weight:500}}
  .primary{{background:#0071e3;color:#fff}}
  .secondary{{background:#e8e8ed;color:#1d1d1f}}
  .msg{{font-size:13px;margin-top:12px;color:#0071e3}}
  .err{{color:#ff3b30}}
</style>
</head>
<body>
<div class="card">
  <h1>📦 MyDrive 公开分享</h1>
  <div id="info" class="meta">正在加载分享信息...</div>
  <div id="pwdBox" style="display:none">
    <input id="pwd" type="password" placeholder="请输入提取码">
    <button class="primary" onclick="loadShare()" style="width:100%">验证提取码</button>
  </div>
  <div id="actions" class="row" style="display:none">
    <a id="dlBtn" class="btn primary" href="#">直接下载</a>
    <button class="secondary" onclick="saveToDrive()">⚡ 一键转存到我的网盘</button>
  </div>
  <div id="msg" class="msg"></div>
</div>
<script>
const SHARE_ID = {share_id!r};
let currentPwd = '';
async function loadShare() {{
  const pwdEl = document.getElementById('pwd');
  if (pwdEl && pwdEl.value) currentPwd = pwdEl.value.trim();
  const r = await fetch('/api/share/' + encodeURIComponent(SHARE_ID) + (currentPwd ? '?password=' + encodeURIComponent(currentPwd) : ''));
  const d = await r.json();
  if (d.need_password || d.error === 'password_required' || r.status === 403) {{
    document.getElementById('info').textContent = '该分享受提取码保护';
    document.getElementById('pwdBox').style.display = 'block';
    document.getElementById('actions').style.display = 'none';
    return;
  }}
  if (!r.ok || d.error) {{
    document.getElementById('info').innerHTML = '<span class="err">' + (d.detail || d.error || '分享不存在或已失效') + '</span>';
    return;
  }}
  const f = d.file || d;
  document.getElementById('pwdBox').style.display = 'none';
  document.getElementById('info').innerHTML =
    '<strong>' + (f.name || '未命名文件') + '</strong><br>' +
    '大小：' + ((f.size || 0) / 1024 / 1024).toFixed(2) + ' MB · 浏览 ' + (d.views || 0) + ' · 下载 ' + (d.downloads || 0);
  document.getElementById('dlBtn').href = '/api/share/' + encodeURIComponent(SHARE_ID) + '/download' + (currentPwd ? '?password=' + encodeURIComponent(currentPwd) : '');
  document.getElementById('actions').style.display = 'flex';
}}
async function saveToDrive() {{
  const token = localStorage.getItem('mydrive_token') || '';
  const msg = document.getElementById('msg');
  if (!token) {{
    msg.innerHTML = '<span class="err">请先在 <a href="/">主页</a> 登录你的账号，再点击一键转存。</span>';
    return;
  }}
  const r = await fetch('/api/share/' + encodeURIComponent(SHARE_ID) + '/save', {{
    method: 'POST',
    headers: {{'Content-Type': 'application/json', 'Authorization': 'Bearer ' + token}},
    body: JSON.stringify({{password: currentPwd, parent: 'root'}})
  }});
  const d = await r.json();
  if (r.ok) {{
    msg.textContent = '✅ 已秒存到你的网盘根目录！';
  }} else {{
    msg.innerHTML = '<span class="err">转存失败：' + (d.detail || JSON.stringify(d)) + '</span>';
  }}
}}
loadShare();
</script>
</body>
</html>"""
        return HTMLResponse(html)


    # ==================== COMMERCIAL VIP, PRESIGN S3/R2, MODERATION & EMBEDDED SUBTITLES ====================

    @app.get("/api/vip/me")
    async def get_my_vip(request: Request):
        uid = caller_id_fn(request) or "anonymous"
        info = commercial.get_membership(uid)
        stats = engine.get_storage_stats(user_id=uid)
        return {**info, "used_bytes": stats["logical_bytes"], "total_files": stats["total_files"]}

    @app.post("/api/vip/codes/generate")
    async def generate_vip_codes(req: RedeemCodeGenRequest, request: Request):
        _require_admin(request)
        try:
            codes = commercial.generate_codes(req.plan, req.duration_days, req.count)
            audit.log('vip.generate_codes', caller_id_fn(request) or '', req.plan, request.client.host if request.client else '', f'count={len(codes)}')
            return {"codes": codes}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.post("/api/vip/redeem")
    async def redeem_vip_code(req: RedeemRequest, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        try:
            res = commercial.redeem_code(uid, req.code)
            if hasattr(auth, 'extend_quota'):
                try: auth.extend_quota(uid, int(res.get('storage_quota_bytes', 0)))
                except Exception: pass
            audit.log('vip.redeem', uid, req.code, request.client.host if request.client else '', res.get('plan', ''))
            return res
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/admin/moderate")
    async def list_moderated_cids(request: Request):
        _require_admin(request)
        return {"blocked": commercial.list_blocked_cids()}

    @app.post("/api/admin/moderate/{cid}")
    async def moderate_block_cid(cid: str, req: ModerateRequest, request: Request):
        _require_admin(request)
        res = commercial.block_cid(cid, reason=req.reason)
        audit.log('admin.block_cid', caller_id_fn(request) or '', cid, request.client.host if request.client else '', req.reason)
        return res

    @app.delete("/api/admin/moderate/{cid}")
    async def moderate_unblock_cid(cid: str, request: Request):
        _require_admin(request)
        return commercial.unblock_cid(cid)

    @app.post("/api/storage/presign-upload")
    async def presign_chunk_upload(req: PresignUploadRequest, request: Request):
        caller_id_fn(request)
        key = engine.tiered._cold_key(req.cid)
        if isinstance(engine.tiered.backend, S3CompatBackend):
            url = engine.tiered.backend.presign_url("PUT", key, expires=req.expires)
            return {"mode": "s3_direct", "method": "PUT", "cid": req.cid, "url": url, "expires": req.expires}
        return {"mode": "server_fallback", "method": "POST", "cid": req.cid, "url": "/api/upload/block", "expires": req.expires}

    @app.get("/api/storage/presign-download/{identity}")
    async def presign_file_download(identity: str, request: Request, expires: int = Query(900, ge=60, le=86400)):
        uid = caller_id_fn(request)
        meta = engine.get_file_meta(identity, user_id=uid)
        if not meta or meta.is_dir:
            raise HTTPException(status_code=404, detail="File not found")
        if commercial.is_cid_blocked(meta.content_identity):
            raise HTTPException(status_code=451, detail="该文件因违规已被限制访问")
        is_s3 = isinstance(engine.tiered.backend, S3CompatBackend)
        chunks_out = []
        for ch in meta.chunks:
            if is_s3:
                url = engine.tiered.backend.presign_url("GET", engine.tiered._cold_key(ch.cid), expires=expires)
            else:
                url = f"/api/chunk/{ch.cid}"
            chunks_out.append({"index": ch.index, "cid": ch.cid, "size": ch.size, "url": url})
        return {
            "identity": meta.identity,
            "name": meta.name,
            "size": meta.size,
            "mode": "s3_direct" if is_s3 else "server_chunk",
            "expires": expires,
            "chunks": chunks_out,
        }

    @app.get("/api/media/{identity}/embedded-subtitle/{stream_index}.vtt")
    async def embedded_subtitle_vtt_api(identity: str, stream_index: int, request: Request):
        uid = caller_id_fn(request)
        vtt = media.extract_embedded_subtitle_vtt(identity, stream_index, user_id=uid)
        if vtt is None:
            raise HTTPException(status_code=404, detail="Embedded subtitle unavailable")
        return PlainTextResponse(vtt, media_type="text/vtt; charset=utf-8")


    # ==================== V3.0 COMMERCIAL SAAS: ORDERS, HEALTH, METRICS, AUDIT & OPS ====================

    @app.post("/api/vip/orders/create")
    async def create_vip_order(req: CreateOrderRequest, request: Request):
        uid = caller_id_fn(request) or "anonymous"
        try:
            order = commercial.create_order(uid, req.plan, req.duration_days, req.provider)
            audit.log("order.create", uid, order["order_no"], request.client.host if request.client else "", req.plan)
            return order
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

    @app.get("/api/vip/orders")
    async def list_my_orders(request: Request):
        uid = caller_id_fn(request) or "anonymous"
        return {"orders": commercial.list_orders(user_id=uid)}

    @app.post("/api/vip/orders/webhook")
    async def payment_webhook(request: Request):
        raw = await request.body()
        sig = request.headers.get("X-Pay-Signature", "")
        if not commercial.verify_webhook_signature(raw, sig):
            raise HTTPException(status_code=403, detail="Invalid payment webhook signature")
        import json as _json
        data = _json.loads(raw.decode("utf-8"))
        order_no = data.get("order_no", "")
        try:
            res = commercial.mark_order_paid(order_no)
            if hasattr(auth, 'extend_quota'):
                try: auth.extend_quota(res['user_id'], int(res.get('membership', {}).get('storage_quota_bytes', 0)))
                except Exception: pass
            audit.log("order.paid", res["user_id"], order_no, request.client.host if request.client else "", res["plan"])
            return {"ok": True, "order": res}
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))

    @app.get("/api/health")
    async def health_check():
        h = collect_health(engine, offline_engine, media, storage_root)
        h["cluster"] = cluster.status()
        return h

    @app.get("/api/metrics")
    async def prometheus_metrics():
        return PlainTextResponse(export_prometheus_metrics(engine, commercial, storage_root), media_type="text/plain; version=0.0.4")

    @app.get("/api/admin/audit-logs")
    async def get_audit_logs(request: Request, limit: int = Query(100, ge=1, le=500)):
        _require_admin(request)
        return {"logs": audit.recent(limit=limit)}

    @app.get("/api/admin/dashboard")
    async def get_admin_dashboard(request: Request):
        _require_admin(request)
        st = engine.get_storage_stats()
        ts = engine.tiered.get_stats()
        rev = commercial.revenue_summary()
        health = collect_health(engine, offline_engine, media, storage_root)
        return {
            "storage": st,
            "tiered": ts,
            "revenue": rev,
            "blocked_cids": commercial.list_blocked_cids(),
            "health": health,
            "cluster": cluster.status(),
            "recent_logs": audit.recent(limit=20),
        }

    @app.get("/manifest.json")
    async def pwa_manifest():
        mf = Path(__file__).parent / "static" / "manifest.json"
        if mf.exists():
            return FileResponse(mf, media_type="application/manifest+json")
        raise HTTPException(status_code=404, detail="manifest not found")

    return {"media": media, "telegram": tg_bot, "commercial": commercial, "audit": audit, "cluster": cluster}
