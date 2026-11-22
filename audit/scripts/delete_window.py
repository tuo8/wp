#!/usr/bin/env python3
"""删除后旧签名 URL 的存活窗口（受控计时）。"""
import os
import sys
import json
import time
import hashlib

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hc  # noqa: E402
from hc import pb_string, pb_bytes, http  # noqa: E402
from upload_test import upload_file, ROOT  # noqa: E402
from download_test import direct_address, slices_info  # noqa: E402

EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
TF = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testfiles")


def op(m, ids):
    req = b"".join(pb_bytes(1, pb_string(1, i)) for i in ids)
    r = hc.authed("v6.services.pub.PubUserFile", m, req, timeout=60)
    return {"grpc_status": r.get("grpc_status"), "grpc_message": r.get("grpc_message")}


def main():
    ts = time.strftime("%H%M%S")
    name = f"hc-audit-ttl-{ts}.txt"
    rec = upload_file(os.path.join(TF, "hc-test-small.txt"), name, ROOT, "text/plain")
    time.sleep(3)
    from find_entry import list_recent
    r = list_recent(ROOT, 50)
    fs = (r.get("pb") or {}).get(1) or []
    if isinstance(fs, dict):
        fs = [fs]
    ent = next((f for f in fs if isinstance(f, dict) and f.get(4) == name), None)
    if not ent:
        print("entry not found"); return
    ident, cid = ent[1], ent.get(24)
    sha = hashlib.sha256(open(os.path.join(TF, "hc-test-small.txt"), "rb").read()).hexdigest()

    d = direct_address(ident)
    url = (d.get("pb") or {}).get(1)
    si = slices_info(ident)
    sl = (si.get("pb") or {}).get(1) or []
    if isinstance(sl, dict):
        sl = [sl]
    surl, key = (sl[0][2], sl[0].get(4)) if sl else (None, 0)

    out = {"file": name, "identity": ident, "cid": cid, "sha256": sha, "checks": []}

    def check(tag, elapsed):
        e = {"tag": tag, "elapsed_s": round(elapsed, 1)}
        if url:
            g = http("GET", url, timeout=60)
            e["direct_status"] = g["status"]
            e["direct_bytes"] = len(g["body"])
            e["direct_match"] = hashlib.sha256(g["body"]).hexdigest() == sha
        if surl:
            g2 = http("GET", surl, timeout=60)
            tbl = bytes(b ^ key for b in range(256))
            e["slice_status"] = g2["status"]
            e["slice_bytes"] = len(g2["body"])
            e["slice_match"] = hashlib.sha256(g2["body"].translate(tbl)).hexdigest() == sha
        e["api_direct"] = direct_address(ident).get("grpc_status")
        e["api_slices"] = slices_info(ident).get("grpc_status")
        out["checks"].append(e)
        print(json.dumps(e, ensure_ascii=False), flush=True)

    t0 = time.time()
    check("before_delete", 0)
    out["trash"] = op("Trash", [ident])
    check("after_trash", time.time() - t0)
    out["purge"] = op("DeleteTrash", [ident])
    for wait in (0, 30, 60, 120, 240):
        target = t0 + wait
        while time.time() < target:
            time.sleep(1)
        check(f"after_purge+{wait}s", time.time() - t0)

    json.dump(out, open(os.path.join(EV, "delete_window.json"), "w"), ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
