#!/usr/bin/env python3
"""批量上传本次自制测试文件并记录证据（严格顺序、无并发）。"""
import os
import sys
import json
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from upload_test import upload_file, find_entry, ROOT  # noqa: E402
import hc  # noqa: E402
from hc import pb_string, pb_int, pb_bytes  # noqa: E402

EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
TS = time.strftime("%Y%m%d%H%M")

CASES = [
    ("hc-test-small.txt", f"hc-audit-small-{TS}.txt", "text/plain"),
    ("hc-test-1m.bin", f"hc-audit-1m-{TS}.bin", "application/octet-stream"),
    ("hc-test-4m.bin", f"hc-audit-4m-{TS}.bin", "application/octet-stream"),
    ("hc-test-20m.bin", f"hc-audit-20m-{TS}.bin", "application/octet-stream"),
    ("hc-test-image.png", f"hc-audit-image-{TS}.png", "image/png"),
    ("hc-test-video.mp4", f"hc-audit-video-{TS}.mp4", "video/mp4"),
]

out = {"started": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "user": "[REDACTED]", "root": ROOT,
       "cases": []}

for local, remote, mime in CASES:
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testfiles", local)
    print(f"\n=== upload {local} -> {remote} ===", flush=True)
    rec = upload_file(p, remote, ROOT, mime)
    entry = find_entry(remote, ROOT)
    rec["entry"] = entry
    out["cases"].append(rec)
    print(json.dumps({k: rec.get(k) for k in
                      ("size", "chunk_count", "upload_seconds", "upload_Bps")},
                     ensure_ascii=False), flush=True)
    print("  content_identity:", (entry or {}).get("24"), flush=True)
    print("  entry mime:", (entry or {}).get("6"), "type:", (entry or {}).get("8"),
          "nodes:", (entry or {}).get("30"), flush=True)
    time.sleep(1)

out["finished"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
os.makedirs(EV, exist_ok=True)
json.dump(out, open(os.path.join(EV, "uploads.json"), "w"), ensure_ascii=False, indent=2)
print("\nsaved evidence/uploads.json")
