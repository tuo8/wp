"""当前线上协议复核客户端（同账号、同端点）。
用途：在不依赖历史结论的前提下，直接与当前服务交互并记录真实请求/响应。
凭据从 ../capture/secrets/session.json 读取，绝不写入交付目录。
"""
import json, os, time, base64
import requests

HERE = os.path.dirname(os.path.abspath(__file__))
SECRETS = os.path.join(HERE, "..", "..", "capture", "secrets", "session.json")
HOST = "https://drcdn2.2dland.cn"
CLIENT_ID = "devDebugger/1.0"
APPID = "2dland/web"
APPVER = "202603182100"
    # 注意：交付包内 webclient JS 中的 appsecret 已脱敏删除。
    # 复跑前请重新抓取 https://drive.2dland.cn/ 的 assets/index-*.js 并设置 HCC_APPSECRET，
    # 提取方法见 12_reports/upload-flow.md §0。
def _appsecret():
    """交付版不硬编码常量：优先环境变量，其次从存档的官方 Web 客户端 JS 中提取。"""
    v = os.environ.get("HCC_APPSECRET")
    if v:
        return v
    js = os.path.join(HERE, "..", "07_storage_backend", "webclient", "index-C6KzmNsb.js")
    if os.path.exists(js):
        import re
        m = re.search(r"2dland/web`,[A-Za-z0-9_$]+=`(\d+)`,[A-Za-z0-9_$]+=`([A-Za-z0-9]{8,32})`", open(js, encoding="utf-8").read())
        if m:
            return m.group(2)
    return ""

APPSECRET = _appsecret()

S = requests.Session()
S.trust_env = False
S.verify = True

def _tok():
    return json.load(open(SECRETS))

def _save(d):
    json.dump(d, open(SECRETS, "w"))

def refresh():
    d = _tok()
    body = {"client_id": CLIENT_ID, "refresh_token": d["refresh_token"], "grant_type": "refresh_token"}
    r = S.post(HOST + "/v6/oauth/refresh_token", json=body, timeout=20,
               headers={"Content-Type": "application/json", "Accept": "application/json"})
    return r

def api(path, payload=None, method="POST", token=None, extra=None, timeout=30):
    h = {"Content-Type": "application/json", "Accept": "application/json",
         "User-Agent": "HalalCloud-Dart/1.0.24; Android"}
    if token:
        h["Authorization"] = "Bearer " + token
    if extra:
        h.update(extra)
    t0 = time.time()
    r = S.request(method, HOST + path, json=payload, headers=h, timeout=timeout)
    return r, round((time.time() - t0) * 1000)

# ---------- minimal protobuf ----------
def vi(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out

def fld(num, wt, payload):
    return vi((num << 3) | wt) + payload

def s(num, v):
    b = v.encode() if isinstance(v, str) else v
    return fld(num, 2, vi(len(b)) + b)

def i(num, v):
    return fld(num, 0, vi(v))

def dec(buf):
    out, p = {}, 0
    while p < len(buf):
        key, p = _vi(buf, p)
        num, wt = key >> 3, key & 7
        if wt == 0:
            v, p = _vi(buf, p)
        elif wt == 2:
            ln, p = _vi(buf, p); v = buf[p:p+ln]; p += ln
        elif wt == 5:
            v = buf[p:p+4]; p += 4
        elif wt == 1:
            v = buf[p:p+8]; p += 8
        else:
            break
        out.setdefault(num, []).append(v)
    return out

def _vi(buf, p):
    r = s_ = 0
    while True:
        b = buf[p]; p += 1
        r |= (b & 0x7F) << s_
        s_ += 7
        if not b & 0x80:
            return r, p

def grpc(path, payload=b"", token=None, timeout=30, ctype="application/grpc-web+proto", host=HOST):
    h = {"Content-Type": ctype, "X-Grpc-Web": "1", "Accept": "application/grpc-web+proto",
         "User-Agent": "HalalCloud-Dart/1.0.24; Android"}
    if token:
        h["Authorization"] = "Bearer " + token
    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
    t0 = time.time()
    r = S.post(host + path, data=frame, headers=h, timeout=timeout)
    ms = round((time.time()-t0)*1000)
    frames = []
    b = r.content
    p = 0
    while p + 5 <= len(b):
        flag = b[p]; ln = int.from_bytes(b[p+1:p+5], "big")
        frames.append((flag, b[p+5:p+5+ln])); p += 5 + ln
    return {"http": r.status_code, "ms": ms, "headers": dict(r.headers),
            "frames": frames, "grpc_status": r.headers.get("grpc-status"),
            "grpc_message": r.headers.get("grpc-message"), "raw": b}


def sign_headers(service, method, token=None, ts=None):
    import hashlib
    ts = str(ts or int(time.time() * 1000))
    plain = "/%s/%s%s%s%s" % (service, method, ts, APPID, APPVER)
    h = {"timestamp": ts, "appid": APPID, "appversion": APPVER}
    if token:
        h["authorization"] = "Bearer " + token
        plain += "Bearer " + token
    h["sign"] = hashlib.md5((plain + APPSECRET).encode()).hexdigest()
    return h

def call(service, method, payload=b"", token=None, timeout=30, host=HOST):
    h = {"Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1",
         "Accept": "application/grpc-web+proto", "te": "trailers",
         "User-Agent": "Mozilla/5.0 (Linux; Android 15) HalalCloudWeb"}
    h.update(sign_headers(service, method, token))
    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
    t0 = time.time()
    r = S.post(host + "/" + service + "/" + method, data=frame, headers=h, timeout=timeout)
    ms = round((time.time()-t0)*1000)
    frames, p2, b = [], 0, r.content
    while p2 + 5 <= len(b):
        fl = b[p2]; ln = int.from_bytes(b[p2+1:p2+5], "big")
        frames.append((fl, b[p2+5:p2+5+ln])); p2 += 5 + ln
    return {"http": r.status_code, "ms": ms, "status": r.headers.get("grpc-status"),
            "message": r.headers.get("grpc-message"), "frames": frames,
            "headers": dict(r.headers)}
