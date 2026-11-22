"""CloudFlare ImgBed adapter used as MyDrive's remote chunk store.

Targets the CloudFlare ImgBed API in the artai8 fork:
POST /upload, GET /file/<id>, DELETE /api/manage/delete/<id>.
MyDrive's existing 4 MiB CID blocks are uploaded as individual Telegram files.
"""
from __future__ import annotations
import base64, json, os, re, secrets
import urllib.error, urllib.parse, urllib.request
from typing import Optional


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ImgBedBackend:
    kind = "imgbed"
    MAX_CHUNK_BYTES = 16 * 1024 * 1024

    def __init__(self, base_url: str, auth_code: str, admin_username: str = "",
                 admin_password: str = "", timeout: float = 60.0):
        parsed = urllib.parse.urlsplit((base_url or "").strip())
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ValueError("IMGBED_BASE_URL must be an absolute HTTP(S) URL")
        if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
            raise ValueError("IMGBED_BASE_URL must use HTTPS outside localhost")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("IMGBED_BASE_URL must not contain credentials, query, or fragment")
        if not auth_code:
            raise ValueError("IMGBED_AUTH_CODE is required for ImgBed uploads")
        self.base_url = base_url.rstrip("/")
        self.auth_code = auth_code
        self.admin_username = admin_username
        self.admin_password = admin_password
        self.timeout = max(3.0, float(timeout))
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

    @classmethod
    def from_env(cls):
        return cls(os.environ.get("IMGBED_BASE_URL", ""), os.environ.get("IMGBED_AUTH_CODE", ""),
                   os.environ.get("IMGBED_ADMIN_USERNAME", ""), os.environ.get("IMGBED_ADMIN_PASSWORD", ""),
                   float(os.environ.get("IMGBED_TIMEOUT", "60")))

    @staticmethod
    def _remote_id(key: str) -> str:
        cid = str(key).rsplit("/", 1)[-1]
        if not re.fullmatch(r"[A-Za-z0-9._-]{16,120}", cid):
            raise ValueError("Invalid CID object key")
        return cid + ".bin"

    def _request(self, method: str, url: str, body: Optional[bytes] = None, headers: Optional[dict] = None):
        request_headers = {"User-Agent": "MyDrive-ImgBed-Storage/1.0", "Accept": "*/*"}
        request_headers.update(headers or {})
        request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                return int(response.status), response.read()
        except urllib.error.HTTPError as exc:
            try: return int(exc.code), exc.read()
            finally: exc.close()
        except (urllib.error.URLError, TimeoutError, OSError, ValueError):
            return 0, b""

    def put_chunk(self, key: str, data: bytes) -> bool:
        if len(data) > self.MAX_CHUNK_BYTES: return False
        try: remote_id = self._remote_id(key)
        except ValueError: return False
        boundary = "----MyDriveImgBed" + secrets.token_hex(16)
        fields = [("uploadChannel","telegram"),("uploadNameType","origin"),("autoRetry","false"),
                  ("returnFormat","default"),("serverCompress","false")]
        parts = []
        for name, value in fields:
            parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n").encode())
        parts.append((f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{remote_id}\"\r\n"
                      "Content-Type: application/octet-stream\r\n\r\n").encode() + data + b"\r\n")
        parts.append(f"--{boundary}--\r\n".encode())
        body = b"".join(parts)
        url = self.base_url + "/upload?" + urllib.parse.urlencode({
            "uploadChannel":"telegram","uploadNameType":"origin","autoRetry":"false",
            "returnFormat":"default","serverCompress":"false"})
        status, response_body = self._request("POST", url, body, {
            "Content-Type": f"multipart/form-data; boundary={boundary}", "authCode": self.auth_code})
        if status != 200: return False
        try:
            payload = json.loads(response_body.decode("utf-8"))
            items = payload if isinstance(payload, list) else payload.get("data", [payload])
            if isinstance(items, dict): items = [items]
            src = next((item.get("src") for item in items if isinstance(item, dict) and item.get("src")), "")
            returned_id = urllib.parse.unquote(urllib.parse.urlsplit(src).path.rsplit("/",1)[-1])
            return returned_id == remote_id
        except (ValueError, TypeError, AttributeError, UnicodeDecodeError):
            return False

    def get_chunk(self, key: str) -> Optional[bytes]:
        try: remote_id = self._remote_id(key)
        except ValueError: return None
        url = self.base_url + "/file/" + urllib.parse.quote(remote_id, safe="")
        status, body = self._request("GET", url, headers={"Accept":"application/octet-stream"})
        return body if status == 200 else None

    def delete_chunk(self, key: str) -> bool:
        if not self.admin_username or not self.admin_password: return False
        try: remote_id = self._remote_id(key)
        except ValueError: return False
        token = base64.b64encode(f"{self.admin_username}:{self.admin_password}".encode()).decode("ascii")
        url = self.base_url + "/api/manage/delete/" + urllib.parse.quote(remote_id, safe="")
        status, _ = self._request("DELETE", url, headers={"Authorization":"Basic " + token})
        return status in (200,204,404)

    def exists(self, key: str) -> bool:
        return self.get_chunk(key) is not None

    def health_check(self) -> dict:
        status, _ = self._request("GET", self.base_url + "/")
        return {"ok":status == 200,"kind":self.kind,"status":status}
