#!/usr/bin/env python3
"""下载链路验证：GetFileSlicesDownloadInfo -> 逐片 GET -> 拼接比对 SHA-256。

同时测 GetDirectDownloadAddress（整文件直链）与 Range 请求。
"""
import os
import sys
import json
import time
import hashlib
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hc  # noqa: E402
from hc import pb_string, pb_int, pb_bytes, http  # noqa: E402

EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
ROOT = hc.USER_ID
XOR = bytes(b ^ 0x75 for b in range(256))


def redact(u):
    try:
        p = urllib.parse.urlsplit(u)
        q = urllib.parse.parse_qs(p.query)
        return f"{p.scheme}://{p.netloc}{p.path}" + (
            "?" + "&".join(f"{k}=[REDACTED:{len(v[0])}]" for k, v in q.items())
            if q else "")
    except Exception:
        return u


def slices_info(identity, limit=1000):
    list_info = pb_int(2, limit)
    req = pb_bytes(1, pb_string(1, identity)) + pb_bytes(2, list_info)
    return hc.authed("v6.services.pub.PubUserFile", "GetFileSlicesDownloadInfo", req, timeout=60)


def direct_address(identity):
    req = pb_string(1, identity)
    return hc.authed("v6.services.pub.PubUserFile", "GetDirectDownloadAddress", req, timeout=60)


def download_file(identity, expect_sha256, expect_size, label=""):
    rec = {"identity": identity, "expect_sha256": expect_sha256, "expect_size": expect_size}
    r = slices_info(identity)
    pb = r.get("pb") or {}
    slices = pb.get(1) or []
    if isinstance(slices, dict):
        slices = [slices]
    rec["slices_grpc"] = {"status": r.get("grpc_status"), "message": r.get("grpc_message"),
                          "count": len(slices)}
    if not slices:
        rec["error"] = "no slices"
        return rec

    first = slices[0]
    rec["slice_shape"] = {"identity": first.get(1),
                          "url_redacted": redact(first.get(2) or ""),
                          "host": urllib.parse.urlsplit(first.get(2) or "").netloc,
                          "encrypt": first.get(4), "store_type": first.get(5)}

    buf = bytearray()
    det = []
    t0 = time.time()
    for s in slices:
        u = s.get(2)
        st = http("GET", u, timeout=180)
        det.append({"idx": len(det), "status": st["status"], "len": len(st["body"]),
                    "elapsed": st["elapsed"]})
        buf += st["body"]
    dt = time.time() - t0
    raw = bytes(buf)
    rec["slices_download"] = {
        "count": len(det),
        "seconds": round(dt, 2),
        "bytes": len(raw),
        "Bps": round(len(raw) / max(dt, 0.001), 1),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "match_plain": hashlib.sha256(raw).hexdigest() == expect_sha256,
        "per_slice": det[:3] + ([{"...": len(det) - 3}] if len(det) > 3 else []),
        "headers_sample": {k: v for k, v in (st["headers"] or {}).items()
                           if k.lower() in ("server", "etag", "content-length", "age",
                                            "x-bdcdn-cache-status", "via", "content-type",
                                            "x-cache", "cf-cache-status", "x-tt-trace-tag",
                                            "x-response-cinfo", "vary")},
    }
    # 若明文不匹配，按 SliceDownloadInfo.encrypt 的值做单字节 XOR 解混淆
    key = first.get(4) or 0
    if isinstance(key, int) and 0 < key < 256:
        tbl = bytes(b ^ key for b in range(256))
        dec = raw.translate(tbl)
        rec["slices_download"]["xor_key_tested"] = key
        rec["slices_download"]["match_after_xor_key"] = hashlib.sha256(dec).hexdigest() == expect_sha256
        if rec["slices_download"]["match_after_xor_key"]:
            rec["slices_download"]["sha256_after_xor_key"] = hashlib.sha256(dec).hexdigest()
    dec75 = raw.translate(XOR)
    rec["slices_download"]["match_after_xor75"] = hashlib.sha256(dec75).hexdigest() == expect_sha256

    # 整文件直链
    d = direct_address(identity)
    dpb = d.get("pb") or {}
    rec["direct"] = {"status": d.get("grpc_status"), "message": d.get("grpc_message"),
                     "url_redacted": redact(dpb.get(1) or ""),
                     "host": urllib.parse.urlsplit(dpb.get(1) or "").netloc,
                     "expire_at": dpb.get(2), "encrypt": dpb.get(5),
                     "store_type": dpb.get(6), "file_size": dpb.get(7),
                     "content_identity": dpb.get(10)}
    if dpb.get(1):
        t1 = time.time()
        st = http("GET", dpb[1], timeout=300)
        rec["direct"]["download"] = {
            "status": st["status"], "bytes": len(st["body"]),
            "seconds": round(time.time() - t1, 2),
            "sha256": hashlib.sha256(st["body"]).hexdigest(),
            "match_plain": hashlib.sha256(st["body"]).hexdigest() == expect_sha256,
            "match_after_xor75": hashlib.sha256(st["body"].translate(XOR)).hexdigest()
            == expect_sha256,
            "headers": {k: v for k, v in (st["headers"] or {}).items()
                        if k.lower() in ("server", "etag", "content-length", "content-type",
                                         "content-range", "accept-ranges", "age",
                                         "x-bdcdn-cache-status", "via", "x-cache")},
        }
        # Range 请求测试（断点续传）
        rng = http("GET", dpb[1], headers={"Range": "bytes=100-199"}, timeout=60)
        rec["range"] = {"status": rng["status"], "len": len(rng["body"]),
                        "content_range": (rng["headers"] or {}).get("Content-Range"),
                        "accept_ranges": (rng["headers"] or {}).get("Accept-Ranges")}
    return rec


if __name__ == "__main__":
    name = sys.argv[1]
    sha = sys.argv[2]
    size = int(sys.argv[3])
    ident = sys.argv[4]
    rec = download_file(ident, sha, size, name)
    rec["file"] = name
    print(json.dumps(rec, ensure_ascii=False, indent=2)[:4000])
    os.makedirs(EV, exist_ok=True)
    json.dump(rec, open(os.path.join(EV, "dl_%s.json" % name.replace("/", "_")), "w"),
              ensure_ascii=False, indent=2)
