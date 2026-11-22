#!/usr/bin/env python3
"""删除后 CID 可读性：先留存签名 URL，删除后复用旧 URL 观察行为。

用于区分「API 拒绝签发」与「块/缓存仍然存在」。
"""
import os
import sys
import json
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hc  # noqa: E402
from hc import pb_string, pb_bytes, http  # noqa: E402
from download_test import slices_info, direct_address  # noqa: E402

EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
NAME = "hc-audit-1m-202611222050.bin"
IDENT = "[REDACTED_ID]"
SHA = "ce443b404acfb5f3f787381daebd7b837482606f1bee59a23de5965dd8c51fcc"


def op(method, sources):
    req = b"".join(pb_bytes(1, pb_string(1, s)) for s in sources)
    r = hc.authed("v6.services.pub.PubUserFile", method, req, timeout=60)
    return {"grpc_status": r.get("grpc_status"), "grpc_message": r.get("grpc_message")}


def main():
    r = {"file": NAME, "identity": IDENT, "sha256": SHA}
    si = slices_info(IDENT)
    sl = (si.get("pb") or {}).get(1) or []
    if isinstance(sl, dict):
        sl = [sl]
    slice_url = sl[0][2] if sl else None
    key = sl[0].get(4) if sl else 0
    d = direct_address(IDENT)
    direct_url = (d.get("pb") or {}).get(1)

    pre = http("GET", slice_url, timeout=60)
    tbl = bytes(b ^ key for b in range(256)) if isinstance(key, int) else None
    r["pre_delete"] = {
        "slice_status": pre["status"], "bytes": len(pre["body"]),
        "xor_key": key,
        "plain_match": hashlib.sha256(pre["body"].translate(tbl)).hexdigest() == SHA if tbl else None,
    }
    if direct_url:
        g = http("GET", direct_url, timeout=90)
        r["pre_delete"]["direct_status"] = g["status"]
        r["pre_delete"]["direct_match"] = hashlib.sha256(g["body"]).hexdigest() == SHA

    r["trash"] = op("Trash", [IDENT])
    time.sleep(4)

    post = http("GET", slice_url, timeout=60)
    tbl2 = bytes(b ^ key for b in range(256))
    r["post_trash"] = {
        "slice_status": post["status"], "bytes": len(post["body"]),
        "still_plain_match": hashlib.sha256(post["body"].translate(tbl2)).hexdigest() == SHA,
        "api_slices": slices_info(IDENT).get("grpc_status"),
        "api_direct": direct_address(IDENT).get("grpc_status"),
    }
    if direct_url:
        g = http("GET", direct_url, timeout=90)
        r["post_trash"]["old_direct_status"] = g["status"]
        r["post_trash"]["old_direct_match"] = hashlib.sha256(g["body"]).hexdigest() == SHA

    r["purge"] = op("DeleteTrash", [IDENT])
    time.sleep(4)
    post2 = http("GET", slice_url, timeout=60)
    r["post_purge"] = {
        "slice_status": post2["status"], "bytes": len(post2["body"]),
        "still_plain_match": hashlib.sha256(post2["body"].translate(tbl2)).hexdigest() == SHA,
        "api_slices": slices_info(IDENT).get("grpc_status"),
        "api_direct": direct_address(IDENT).get("grpc_status"),
    }
    if direct_url:
        g = http("GET", direct_url, timeout=90)
        r["post_purge"]["old_direct_status"] = g["status"]
        r["post_purge"]["old_direct_match"] = hashlib.sha256(g["body"]).hexdigest() == SHA

    print(json.dumps(r, ensure_ascii=False, indent=2))
    json.dump(r, open(os.path.join(EV, "delete_cache.json"), "w"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
