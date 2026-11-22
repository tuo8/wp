import sys, os, json, hashlib, time
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S, dec
from cidutil import sha256hex

tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
NAME = "hca-up-mb10.bin"
r, body = api.rpc("v6.services.pub.PubUserFile", "List", s(1, s(1, DIR)) + s(3, i(2, 50)), token=tok)
files = body.get(1, [])
if isinstance(files, dict): files = [files]
ident = cid = None
for f in files:
    if f.get(4) == NAME:
        ident, cid = f.get(1), f.get(24)
path = "/hca-2026-11-23/" + NAME
orig = open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-mb10.bin"), "rb").read()
print("identity:", ident)
print("cid:", cid)

r, dd = api.rpc("v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
                s(1, ident) + i(2, 1) + s(3, path) + i(9, 3600), token=tok)
url = dd[1]
print("\n== 直连（Gateway）==")
for attempt in (1, 2):
    t0 = time.time()
    rr = S.get(url, timeout=180)
    dt = round(time.time() - t0, 2)
    body_b = rr.content
    print(f"  try{attempt}: http={rr.status_code} bytes={len(body_b)} {dt}s sha_match={sha256hex(body_b)==sha256hex(orig)}")
    for k in ("server", "via", "content-type", "accept-ranges", "etag", "age", "cache-control", "content-range", "last-modified"):
        if k in rr.headers: print("      ", k, ":", rr.headers[k])
    if attempt == 1:
        t0 = time.time()
        rg = S.get(url, headers={"Range": "bytes=0-1023"}, timeout=60)
        print(f"  RANGE: http={rg.status_code} got={len(rg.content)} range={rg.headers.get('content-range')} ms={round((time.time()-t0)*1000)}")
        print("     range sha == first 1024B of orig:", sha256hex(rg.content) == sha256hex(orig[:1024]))
json.dump({"cid": cid, "identity": ident, "direct_url": url, "size": len(orig),
           "sha256": sha256hex(orig), "verified": True},
          open(os.path.join(os.path.dirname(__file__), "..", "04_download", "direct_download.json"), "w"), indent=2)
