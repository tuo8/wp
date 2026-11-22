#!/usr/bin/env python3
"""清真云真实上传链路验证：CreateUploadTask -> POST /int/blocks/<task>/<cid> -> commit。

只上传本次自制的随机测试文件；所有请求/响应结构写入 evidence/，令牌与 auth_key 打码。
"""
import os
import io
import sys
import json
import time
import hashlib
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hc  # noqa: E402
from hc import pb_string, pb_int, pb_bytes, http  # noqa: E402

B32 = "abcdefghijklmnopqrstuvwxyz234567"
ROOT = hc.USER_ID
EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")


def b32(data: bytes) -> str:
    bits = val = 0
    out = []
    for byte in data:
        val = (val << 8) | byte
        bits += 8
        while bits >= 5:
            out.append(B32[(val >> (bits - 5)) & 31])
            bits -= 5
    if bits:
        out.append(B32[(val << (5 - bits)) & 31])
    return "".join(out)


def cid_v1(codec: int, data: bytes) -> str:
    mh = b"\x12\x20" + hashlib.sha256(data).digest()
    return "b" + b32(bytes([1, codec]) + mh)


def redact_url(u: str) -> str:
    """保留域名/路径，打码 auth_key 与 session。"""
    try:
        p = urllib.parse.urlsplit(u)
        q = urllib.parse.parse_qs(p.query)
        parts = []
        for k in q:
            v = q[k][0]
            parts.append(f"{k}=[REDACTED:{len(v)}]")
        return f"{p.scheme}://{p.netloc}{p.path}" + ("?" + "&".join(parts) if parts else "")
    except Exception:
        return u


def file_msg(parent, name, size, mime=None, path=None):
    m = pb_string(3, parent) + pb_string(4, name) + pb_int(7, size)
    if path:
        m += pb_string(5, path)
    if mime:
        m += pb_string(6, mime)
    return m


def create_upload_task(parent, name, size, mime=None):
    r = hc.authed("v6.services.pub.PubUserFile", "CreateUploadTask",
                  file_msg(parent, name, size, mime))
    return r


def commit(addr, task, cids, timeout=120):
    return http("POST", f"{addr}/{task}", json.dumps(cids).encode(),
                {"Content-Type": "application/json"}, timeout=timeout)


def upload_file(local_path, name, parent=ROOT, mime="application/octet-stream",
                keep_evidence=True):
    data = open(local_path, "rb").read()
    size = len(data)
    rec = {
        "file": os.path.basename(local_path),
        "name": name,
        "size": size,
        "sha256": hashlib.sha256(data).hexdigest(),
        "md5": hashlib.md5(data).hexdigest(),
        "mime": mime,
    }

    t0 = time.time()
    r = create_upload_task(parent, name, size, mime)
    rec["create_upload_task"] = {"grpc_status": r.get("grpc_status"),
                                 "grpc_message": r.get("grpc_message"),
                                 "http": r.get("http")}
    p = r.get("pb") or {}
    if not p.get(2):
        rec["error"] = "no upload_address"
        rec["raw_hex_prefix"] = (r.get("raw_hex") or "")[:200]
        return rec

    addr = p[2]
    task = p.get(3)
    bs = int(p.get(16) or 4194304)
    codec = int(p.get(19) or 0x55)
    rec["upload_task"] = {
        "upload_address": addr,
        "task_prefix": (task or "")[:48] + "...",
        "block_size": bs,
        "block_hash_type": p.get(17),
        "block_version": p.get(18),
        "block_codec": codec,
        "created": p.get(1),
        "has_s3_credentials": bool(p.get(10) or p.get(11) or p.get(12)),
        "endpoint": p.get(14) or None,
    }

    cids = []
    chunk_stat = []
    for off in range(0, size, bs):
        chunk = data[off:off + bs]
        cid = cid_v1(codec, chunk)
        cids.append(cid)
        u = f"{addr}/{task}/{cid}"
        ct = time.time()
        st = http("POST", u, chunk, {"Content-Type": "application/octet-stream"}, timeout=180)
        chunk_stat.append({"index": len(cids) - 1, "len": len(chunk),
                           "cid": cid, "status": st["status"],
                           "elapsed": round(time.time() - ct, 3),
                           "resp": st["body"][:120].decode("utf-8", "replace")})
    rec["chunks"] = chunk_stat
    rec["chunk_count"] = len(cids)

    # HEAD 一片，确认服务端已落块
    if cids:
        head = http("GET", f"{addr}/{task}/{cids[0]}", timeout=60)
        rec["head_first_chunk"] = {"status": head["status"],
                                   "len": len(head["body"]),
                                   "sha256_match": hashlib.sha256(head["body"]).hexdigest()
                                   == hashlib.sha256(data[:bs]).hexdigest()}

    t1 = time.time()
    cm = commit(addr, task, cids)
    rec["commit"] = {"status": cm["status"], "elapsed": round(time.time() - t1, 3),
                     "resp": cm["body"][:400].decode("utf-8", "replace")}
    rec["upload_seconds"] = round(time.time() - t0, 2)
    rec["upload_Bps"] = round(size / max(rec["upload_seconds"], 0.001), 1)
    rec["local_chunk_cids"] = cids[:3] + (["..."] if len(cids) > 3 else [])
    return rec


def find_entry(name, parent=ROOT, limit=50):
    """按 create_ts 倒序列出父目录，找到同名条目（新文件排在最前）。"""
    list_info = pb_int(2, limit) + pb_bytes(3, pb_string(1, "create_ts"))
    req = pb_bytes(1, pb_string(3, parent)) + pb_bytes(3, list_info)
    r = hc.authed("v6.services.pub.PubUserFile", "List", req)
    pb = r.get("pb") or {}
    files = pb.get(1) or []
    if isinstance(files, dict):
        files = [files]
    for f in files:
        if isinstance(f, dict) and f.get(4) == name:
            return f
    return None


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("usage: upload_test.py <local_file> [remote_name] [mime] [parent]")
        sys.exit(1)
    lp = sys.argv[1]
    nm = sys.argv[2] if len(sys.argv) > 2 else os.path.basename(lp)
    mt = sys.argv[3] if len(sys.argv) > 3 else "application/octet-stream"
    pr = sys.argv[4] if len(sys.argv) > 4 else ROOT
    rec = upload_file(lp, nm, pr, mt)
    print(json.dumps(rec, ensure_ascii=False, indent=2)[:3000])
    e = find_entry(nm, pr)
    print("\n== entry ==")
    print(json.dumps(e, ensure_ascii=False, indent=2)[:1500] if e else "NOT FOUND")
    os.makedirs(EV, exist_ok=True)
    with open(os.path.join(EV, "last_upload.json"), "w") as f:
        json.dump({"upload": rec, "entry": e}, f, ensure_ascii=False, indent=2)
