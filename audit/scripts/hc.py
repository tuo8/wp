#!/usr/bin/env python3
"""HalalCloud 审计用最小客户端：签名 + gRPC-Web + 令牌刷新。

复用了 downloads/pcap/halal_client.py 里的签名与 protobuf 编解码，
不修改任何既有文件；刷新后的令牌单独存放在 audit/.token.json。
"""
import os
import sys
import json
import time
import hashlib
import urllib.request
import urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
PCAP = os.path.join(os.path.dirname(HERE), "downloads", "pcap")
sys.path.insert(0, PCAP)

from halal_client import (grpc_call, decode, pb_string, pb_int, pb_bytes,  # noqa: E402
                          sign_headers, varint, tag, CTX)

APPID = "2dland/web"
APPVERSION = "202603182100"
APPSECRET = os.environ.get("HC_APP_SECRET", "[REDACTED]")  # 原值硬编码于 Web 前端包
HOST = "https://grpcuserapi.2dland.cn"
TOKEN_NEW = os.path.join(HERE, ".token.json")

USER_ID = os.environ.get("HC_USER_ID", "[REDACTED_ID]")


def load_token():
    p = TOKEN_NEW
    if not os.path.exists(p):
        p = os.path.join(PCAP, ".halal_token")
    d = json.load(open(p))
    rt = d.get("refresh_token") or (d.get("user") or {}).get("refreshToken")
    return d.get("access_token"), rt


def save_token(access, refresh):
    json.dump({"access_token": access, "refresh_token": refresh,
               "saved_at": int(time.time())}, open(TOKEN_NEW, "w"), indent=2)


def jwt_exp(tok):
    import base64
    try:
        p = tok.split(".")[1]
        p += "=" * (-len(p) % 4)
        return json.loads(base64.urlsafe_b64decode(p)).get("exp", 0)
    except Exception:
        return 0


def call(service, method, payload=b"", token=None, timeout=30, host=None):
    r = grpc_call(service, method, payload, token=token, timeout=timeout, host=host)
    out = {"http": r["http"], "grpc_status": r["status"], "grpc_message": r["message"]}
    if r["frames"]:
        out["pb"] = decode(r["frames"][0])
        out["raw_len"] = len(r["frames"][0])
        out["raw_hex"] = r["frames"][0].hex()
    return out


def refresh():
    """用 refresh_token 换新 access_token。"""
    at, rt = load_token()
    if not rt:
        return None
    for use_expired in (False, True):
        r = call("v6.services.pub.PubUser", "Refresh", pb_string(6, rt),
                 token=(at if use_expired else None))
        pb = r.get("pb") or {}
        new_at = pb.get(5)
        if new_at:
            save_token(new_at, pb.get(6) or rt)
            return {"ok": True, "access_token": new_at,
                    "expire_ts": pb.get(8), "refresh_expire_ts": pb.get(9),
                    "with_expired_auth": use_expired}
        last = r
    return {"ok": False, "detail": last}


def authed(service, method, payload=b"", timeout=30):
    """带自动刷新的一次调用。"""
    at, _ = load_token()
    if at and jwt_exp(at) < time.time() + 60:
        refresh()
        at, _ = load_token()
    r = call(service, method, payload, token=at, timeout=timeout)
    if r.get("grpc_status") in ("16", 16) or "token" in str(r.get("grpc_message", "")).lower():
        if refresh().get("ok"):
            at, _ = load_token()
            r = call(service, method, payload, token=at, timeout=timeout)
            r["refreshed"] = True
    return r


def http(method, url, body=None, headers=None, timeout=60, want_body=True):
    req = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    t0 = time.time()
    try:
        r = urllib.request.urlopen(req, timeout=timeout, context=CTX)
        data = r.read() if want_body else b""
        return {"status": r.status, "headers": dict(r.headers), "body": data,
                "elapsed": round(time.time() - t0, 3), "error": None}
    except urllib.error.HTTPError as e:
        return {"status": e.code, "headers": dict(e.headers), "body": e.read() if want_body else b"",
                "elapsed": round(time.time() - t0, 3), "error": "HTTPError"}
    except Exception as e:
        return {"status": None, "headers": {}, "body": b"",
                "elapsed": round(time.time() - t0, 3), "error": repr(e)}


if __name__ == "__main__":
    at, rt = load_token()
    print("access token exp:", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(jwt_exp(at))) if at else None)
    print("now            :", time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()))
    print(json.dumps(refresh(), ensure_ascii=False)[:400])
