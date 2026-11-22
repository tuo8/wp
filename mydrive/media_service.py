"""
Cloud Media & Streaming Service (PikPak-style Cloud Play)
=========================================================
Provides:
1. Video / Audio probing via ffprobe (duration, resolution, codecs).
2. On-the-fly HLS transcoding / remuxing via ffmpeg with selectable qualities
   (`original`, `1080p`, `720p`, `480p`) so MKV / HEVC / AVI / FLV play smoothly
   in any browser.
3. Video cover thumbnail + multi-frame storyboard preview sprite generation.
4. Automatic same-directory subtitle discovery (.srt / .vtt / .ass) with
   on-the-fly SRT/ASS -> WebVTT conversion.
5. Persistent per-user playback position, playback speed, and quality memory.
6. Bounded LRU transcode cache so temporary HLS segments don't eat disk space.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
from db_adapter import connect_db
import subprocess
import tempfile
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional

VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".mov", ".webm", ".flv", ".ts", ".m4v", ".wmv", ".rmvb"}
SUBTITLE_EXTS = {".vtt", ".srt", ".ass", ".ssa"}

QUALITY_PROFILES = {
    "original": {"label": "原画 (Remux)", "scale": None, "crf": None, "audio_bitrate": "192k"},
    "1080p": {"label": "1080P 高清", "scale": "scale=-2:min(1080\\,ih)", "crf": "23", "audio_bitrate": "128k"},
    "720p": {"label": "720P 标清", "scale": "scale=-2:min(720\\,ih)", "crf": "25", "audio_bitrate": "128k"},
    "480p": {"label": "480P 流畅", "scale": "scale=-2:min(480\\,ih)", "crf": "27", "audio_bitrate": "96k"},
}


def srt_to_vtt(srt_text: str) -> str:
    """Convert SubRip (.srt) text to WebVTT (.vtt)."""
    text = srt_text.replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    # Convert 00:00:01,500 --> 00:00:04,000 to 00:00:01.500 --> 00:00:04.000
    text = re.sub(r"(\d{2}:\d{2}:\d{2}),(\d{3})", r"\1.\2", text)
    return "WEBVTT\n\n" + text


def ass_to_vtt(ass_text: str) -> str:
    """Best-effort converter from ASS/SSA dialogue events to WebVTT."""
    lines = ["WEBVTT", ""]
    for raw in ass_text.splitlines():
        if not raw.startswith("Dialogue:"):
            continue
        parts = raw.split(",", 9)
        if len(parts) < 10:
            continue
        start = _ass_time_to_vtt(parts[1].strip())
        end = _ass_time_to_vtt(parts[2].strip())
        # Strip ASS override tags like {\an8} and replace \N with newline
        body = re.sub(r"\{[^}]*\}", "", parts[9]).replace("\\N", "\n").replace("\\n", "\n").strip()
        if start and end and body:
            lines.append(f"{start} --> {end}")
            lines.append(body)
            lines.append("")
    return "\n".join(lines)


def _ass_time_to_vtt(ts: str) -> str:
    # H:MM:SS.cc -> HH:MM:SS.mmm
    m = re.match(r"(\d+):(\d{2}):(\d{2})\.(\d{2})", ts)
    if not m:
        return ""
    h, mi, s, cs = m.groups()
    return f"{int(h):02d}:{mi}:{s}.{int(cs) * 10:03d}"


class MediaService:
    """Cloud video/audio streaming, thumbnailing, subtitles, and playback memory."""

    def __init__(self, storage, storage_root: str, max_cache_bytes: int = 2 * 1024 * 1024 * 1024):
        self.storage = storage
        self.root = Path(storage_root) / "media_cache"
        self.root.mkdir(parents=True, exist_ok=True)
        self.max_cache_bytes = int(os.environ.get("MEDIA_CACHE_MAX_BYTES", str(max_cache_bytes)))
        self.db_path = Path(storage_root) / "db" / "media.sqlite"
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.ffmpeg = shutil.which("ffmpeg")
        self.ffprobe = shutil.which("ffprobe")
        self._locks: Dict[str, threading.Lock] = {}
        self._global_lock = threading.Lock()
        self._init_db()

    def _init_db(self):
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute("""
            CREATE TABLE IF NOT EXISTS playback_progress (
                user_id TEXT NOT NULL,
                file_identity TEXT NOT NULL,
                position_sec REAL NOT NULL DEFAULT 0,
                duration_sec REAL NOT NULL DEFAULT 0,
                playback_rate REAL NOT NULL DEFAULT 1.0,
                quality TEXT NOT NULL DEFAULT 'original',
                updated_at INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (user_id, file_identity)
            )
        """)
        conn.commit()
        conn.close()

    def _key_lock(self, key: str) -> threading.Lock:
        with self._global_lock:
            if key not in self._locks:
                self._locks[key] = threading.Lock()
            return self._locks[key]

    def _materialize_temp(self, file_identity: str, user_id: Optional[str]) -> Optional[Path]:
        """Reassemble chunks into a temp file for ffmpeg/ffprobe."""
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        suffix = Path(meta.name).suffix or ".bin"
        tmp = tempfile.NamedTemporaryFile(prefix="mydrive_media_", suffix=suffix, delete=False)
        tmp_path = Path(tmp.name)
        tmp.close()
        ok = self.storage.download_file(file_identity, str(tmp_path), user_id=user_id)
        if not ok:
            tmp_path.unlink(missing_ok=True)
            return None
        return tmp_path

    def probe_media(self, file_identity: str, user_id: Optional[str]) -> dict:
        """Return codec, duration, resolution, and available qualities."""
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta:
            return {"error": "File not found"}
        cid = meta.content_identity or meta.identity
        cache_file = self.root / cid / "probe.json"
        if cache_file.exists():
            try:
                return json.loads(cache_file.read_text(encoding="utf-8"))
            except Exception:
                pass

        info = {
            "identity": meta.identity,
            "name": meta.name,
            "size": meta.size,
            "mime_type": meta.mime_type,
            "ffmpeg_available": bool(self.ffmpeg),
            "duration": 0.0,
            "width": 0,
            "height": 0,
            "video_codec": "",
            "audio_codec": "",
            "audio_tracks": [],
            "embedded_subtitles": [],
            "qualities": list(QUALITY_PROFILES.keys()),
        }
        if not self.ffprobe:
            return info

        tmp = self._materialize_temp(file_identity, user_id)
        if not tmp:
            return info
        try:
            cmd = [
                self.ffprobe, "-v", "quiet", "-print_format", "json",
                "-show_format", "-show_streams", str(tmp),
            ]
            out = subprocess.check_output(cmd, timeout=20)
            data = json.loads(out.decode("utf-8", errors="replace"))
            fmt = data.get("format", {})
            info["duration"] = float(fmt.get("duration") or 0.0)
            for s in data.get("streams", []):
                if s.get("codec_type") == "video" and not info["video_codec"]:
                    info["video_codec"] = s.get("codec_name", "")
                    info["width"] = int(s.get("width") or 0)
                    info["height"] = int(s.get("height") or 0)
                elif s.get("codec_type") == "audio":
                    if not info["audio_codec"]:
                        info["audio_codec"] = s.get("codec_name", "")
                    tags = s.get("tags") or {}
                    info["audio_tracks"].append({
                        "index": s.get("index"),
                        "codec": s.get("codec_name", ""),
                        "language": tags.get("language", "und"),
                        "title": tags.get("title", ""),
                        "channels": s.get("channels", 2),
                    })
                elif s.get("codec_type") == "subtitle":
                    tags = s.get("tags") or {}
                    sidx = s.get("index")
                    info["embedded_subtitles"].append({
                        "index": sidx,
                        "codec": s.get("codec_name", ""),
                        "language": tags.get("language", "und"),
                        "title": tags.get("title") or tags.get("language") or f"内封字幕 #{sidx}",
                        "url": f"/api/media/{meta.identity}/embedded-subtitle/{sidx}.vtt",
                    })
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(json.dumps(info, ensure_ascii=False), encoding="utf-8")
        except Exception as e:
            info["probe_warning"] = str(e)
        finally:
            tmp.unlink(missing_ok=True)
        return info

    def ensure_hls(self, file_identity: str, quality: str, user_id: Optional[str]) -> Optional[Path]:
        """Generate (or reuse cached) HLS playlist + segments for a video file."""
        if not self.ffmpeg:
            return None
        if quality not in QUALITY_PROFILES:
            quality = "720p"
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        cid = meta.content_identity or meta.identity
        out_dir = self.root / cid / f"hls_{quality}"
        playlist = out_dir / "index.m3u8"
        if playlist.exists():
            os.utime(playlist, None)
            return out_dir

        lock = self._key_lock(f"hls:{cid}:{quality}")
        with lock:
            if playlist.exists():
                return out_dir
            tmp = self._materialize_temp(file_identity, user_id)
            if not tmp:
                return None
            try:
                out_dir.mkdir(parents=True, exist_ok=True)
                prof = QUALITY_PROFILES[quality]
                seg_pattern = str(out_dir / "seg_%04d.ts")
                hw = os.environ.get("FFMPEG_HWACCEL", "none").lower()
                v_encoder = "libx264"
                if hw == "nvenc":
                    v_encoder = "h264_nvenc"
                elif hw == "vaapi":
                    v_encoder = "h264_vaapi"
                elif hw == "qsv":
                    v_encoder = "h264_qsv"
                elif hw == "videotoolbox":
                    v_encoder = "h264_videotoolbox"

                if quality == "original":
                    cmd = [
                        self.ffmpeg, "-y", "-i", str(tmp),
                        "-c:v", "copy", "-c:a", "aac", "-b:a", prof["audio_bitrate"],
                        "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                        "-hls_flags", "temp_file",
                        "-hls_segment_filename", seg_pattern, str(playlist),
                    ]
                else:
                    cmd = [
                        self.ffmpeg, "-y", "-i", str(tmp),
                        "-vf", prof["scale"],
                        "-c:v", v_encoder, "-preset", "veryfast", "-crf", prof["crf"],
                        "-c:a", "aac", "-b:a", prof["audio_bitrate"],
                        "-f", "hls", "-hls_time", "4", "-hls_list_size", "0",
                        "-hls_flags", "temp_file",
                        "-hls_segment_filename", seg_pattern, str(playlist),
                    ]

                # 流式边转边播：后台启动 ffmpeg，首个 .ts 分片落盘即立刻返回播放列表
                proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
                def _reaper(p, tmp_file):
                    try:
                        p.wait(timeout=600)
                    except Exception:
                        p.kill()
                    finally:
                        tmp_file.unlink(missing_ok=True)
                        self._trim_cache()
                threading.Thread(target=_reaper, args=(proc, tmp), daemon=True).start()

                deadline = time.time() + 20.0
                while time.time() < deadline:
                    if playlist.exists() and ".ts" in playlist.read_text(encoding="utf-8", errors="ignore"):
                        return out_dir
                    if proc.poll() is not None:
                        break
                    time.sleep(0.15)

                if playlist.exists() and ".ts" in playlist.read_text(encoding="utf-8", errors="ignore"):
                    return out_dir
                return None
            except Exception:
                tmp.unlink(missing_ok=True)
                return None

    def get_thumbnail(self, file_identity: str, user_id: Optional[str], timestamp: float = 1.0) -> Optional[Path]:
        """Extract a JPEG cover thumbnail for a video or image file."""
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        cid = meta.content_identity or meta.identity
        thumb = self.root / cid / "thumb.jpg"
        if thumb.exists():
            return thumb

        lock = self._key_lock(f"thumb:{cid}")
        with lock:
            if thumb.exists():
                return thumb
            tmp = self._materialize_temp(file_identity, user_id)
            if not tmp:
                return None
            try:
                thumb.parent.mkdir(parents=True, exist_ok=True)
                if (meta.mime_type or "").startswith("image/"):
                    try:
                        from PIL import Image
                        with Image.open(tmp) as im:
                            im.thumbnail((480, 360))
                            im.convert("RGB").save(thumb, "JPEG", quality=82)
                        return thumb
                    except Exception:
                        pass
                if self.ffmpeg:
                    cmd = [
                        self.ffmpeg, "-y", "-ss", str(max(0.0, timestamp)),
                        "-i", str(tmp), "-vframes", "1",
                        "-vf", "scale=480:-2", "-q:v", "4", str(thumb),
                    ]
                    rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                    if rc.returncode == 0 and thumb.exists():
                        return thumb
                    # Retry at 0s for very short clips
                    cmd[3] = "0"
                    rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
                    if rc.returncode == 0 and thumb.exists():
                        return thumb
                return None
            finally:
                tmp.unlink(missing_ok=True)

    def get_preview_sprite(self, file_identity: str, user_id: Optional[str], frames: int = 4) -> Optional[Path]:
        """Generate a 2x2 storyboard preview sheet for hover/seek preview."""
        if not self.ffmpeg:
            return None
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        cid = meta.content_identity or meta.identity
        sprite = self.root / cid / "sprite.jpg"
        if sprite.exists():
            return sprite

        lock = self._key_lock(f"sprite:{cid}")
        with lock:
            if sprite.exists():
                return sprite
            tmp = self._materialize_temp(file_identity, user_id)
            if not tmp:
                return None
            try:
                sprite.parent.mkdir(parents=True, exist_ok=True)
                cmd = [
                    self.ffmpeg, "-y", "-i", str(tmp),
                    "-vf", "fps=1/2,scale=240:-2,tile=2x2",
                    "-frames:v", "1", "-q:v", "5", str(sprite),
                ]
                rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=45)
                if rc.returncode == 0 and sprite.exists():
                    return sprite
                return None
            finally:
                tmp.unlink(missing_ok=True)

    def find_subtitles(self, file_identity: str, user_id: Optional[str]) -> List[dict]:
        """Find matching subtitle files (.srt, .vtt, .ass) in the same directory."""
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return []
        siblings = self.storage.list_files(parent=meta.parent, user_id=user_id)
        stem = Path(meta.name).stem.lower()
        results = []
        for f in siblings:
            if f.is_dir:
                continue
            ext = Path(f.name).suffix.lower()
            if ext not in SUBTITLE_EXTS:
                continue
            f_stem = Path(f.name).stem.lower()
            matched = f_stem == stem or f_stem.startswith(stem + ".") or f_stem.startswith(stem + "_")
            results.append({
                "identity": f.identity,
                "name": f.name,
                "ext": ext,
                "matched": matched,
                "url": f"/api/media/{f.identity}/subtitle.vtt",
            })
        # Matched subtitles first
        results.sort(key=lambda x: (not x["matched"], x["name"]))
        return results

    def get_subtitle_vtt(self, sub_identity: str, user_id: Optional[str]) -> Optional[str]:
        """Read a subtitle file and normalize it to WebVTT."""
        meta = self.storage.get_file_meta(sub_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        raw = b"".join((c[2] if isinstance(c, tuple) else c) for c in self.storage.stream_chunks(sub_identity, user_id=user_id))
        text = None
        for enc in ("utf-8-sig", "utf-8", "gb18030", "latin-1"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        if text is None:
            return None
        ext = Path(meta.name).suffix.lower()
        if ext == ".vtt":
            return text if text.lstrip().startswith("WEBVTT") else "WEBVTT\n\n" + text
        if ext == ".srt":
            return srt_to_vtt(text)
        if ext in (".ass", ".ssa"):
            return ass_to_vtt(text)
        return "WEBVTT\n\n" + text


    def extract_embedded_subtitle_vtt(self, file_identity: str, stream_index: int, user_id: Optional[str]) -> Optional[str]:
        """Extract an embedded subtitle stream from MKV/MP4 directly to WebVTT via ffmpeg."""
        if not self.ffmpeg:
            return None
        meta = self.storage.get_file_meta(file_identity, user_id=user_id)
        if not meta or meta.is_dir:
            return None
        cid = meta.content_identity or meta.identity
        vtt_cache = self.root / cid / f"emb_sub_{int(stream_index)}.vtt"
        if vtt_cache.exists():
            return vtt_cache.read_text(encoding="utf-8", errors="replace")
        tmp = self._materialize_temp(file_identity, user_id)
        if not tmp:
            return None
        try:
            vtt_cache.parent.mkdir(parents=True, exist_ok=True)
            cmd = [
                self.ffmpeg, "-y", "-i", str(tmp),
                "-map", f"0:{int(stream_index)}",
                "-f", "webvtt", str(vtt_cache),
            ]
            rc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
            if rc.returncode == 0 and vtt_cache.exists():
                return vtt_cache.read_text(encoding="utf-8", errors="replace")
            return None
        finally:
            tmp.unlink(missing_ok=True)

    # ==================== PLAYBACK PROGRESS MEMORY ====================

    def save_progress(
        self,
        user_id: str,
        file_identity: str,
        position_sec: float,
        duration_sec: float = 0.0,
        playback_rate: float = 1.0,
        quality: str = "original",
    ) -> dict:
        now = int(time.time())
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            """
            INSERT INTO playback_progress (user_id, file_identity, position_sec, duration_sec, playback_rate, quality, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(user_id, file_identity) DO UPDATE SET
                position_sec=excluded.position_sec,
                duration_sec=excluded.duration_sec,
                playback_rate=excluded.playback_rate,
                quality=excluded.quality,
                updated_at=excluded.updated_at
            """,
            (user_id, file_identity, float(position_sec), float(duration_sec), float(playback_rate), quality, now),
        )
        conn.commit()
        conn.close()
        return {
            "file_identity": file_identity,
            "position_sec": float(position_sec),
            "duration_sec": float(duration_sec),
            "playback_rate": float(playback_rate),
            "quality": quality,
            "updated_at": now,
        }

    def get_progress(self, user_id: str, file_identity: str) -> dict:
        conn = connect_db(self.db_path)
        c = conn.cursor()
        c.execute(
            "SELECT position_sec, duration_sec, playback_rate, quality, updated_at "
            "FROM playback_progress WHERE user_id = ? AND file_identity = ?",
            (user_id, file_identity),
        )
        row = c.fetchone()
        conn.close()
        if not row:
            return {"file_identity": file_identity, "position_sec": 0.0, "duration_sec": 0.0, "playback_rate": 1.0, "quality": "original"}
        return {
            "file_identity": file_identity,
            "position_sec": row[0],
            "duration_sec": row[1],
            "playback_rate": row[2],
            "quality": row[3],
            "updated_at": row[4],
        }

    def _trim_cache(self):
        """Keep media transcode cache under max_cache_bytes by evicting oldest CIDs."""
        if self.max_cache_bytes <= 0:
            return
        entries = []
        total = 0
        for child in self.root.iterdir():
            if not child.is_dir():
                continue
            sz = sum(f.stat().st_size for f in child.rglob("*") if f.is_file())
            mtime = child.stat().st_mtime
            entries.append((mtime, sz, child))
            total += sz
        if total <= self.max_cache_bytes:
            return
        entries.sort(key=lambda x: x[0])
        for _, sz, child in entries:
            if total <= self.max_cache_bytes:
                break
            shutil.rmtree(child, ignore_errors=True)
            total -= sz
