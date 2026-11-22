import sys, os, json, hashlib, time
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import cidv1_raw
tok = api.ensure_token()
src = open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin"), "rb").read()
cid = cidv1_raw(src)
req = s(1, cid) + i(2, 1) + i(5, 1)
r, body = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress", req, token=tok)
info = body[1]
url, key = info[2], info[4]
print("slice_url:", url[:120] + "...")
print("encrypt_key:", key)
t0 = time.time()
rr = S.get(url, timeout=60)
print("GET ->", rr.status_code, round((time.time()-t0)*1000), "ms", "bytes", len(rr.content))
for k in ("server", "via", "content-type", "content-length", "etag", "x-cache", "age", "cache-control", "yyyy"):
    if k in rr.headers: print("  ", k, ":", rr.headers[k])
raw = rr.content
dec = bytes(b ^ key for b in raw)
print("xor key:", key)
print("sha256(decoded) =", hashlib.sha256(dec).hexdigest())
print("sha256(original)=", hashlib.sha256(src).hexdigest())
print("MATCH:", dec == src)
print("head hex:", dec[:24].hex())
json.dump({"cid": cid, "url": url, "encrypt": key, "http": rr.status_code,
           "bytes": len(raw), "sha256_decoded": hashlib.sha256(dec).hexdigest(),
           "sha256_original": hashlib.sha256(src).hexdigest(), "match": dec == src,
           "headers": dict(rr.headers)},
          open(os.path.join(os.path.dirname(__file__), "..", "04_download", "slice_probe_kb1.json"), "w"), indent=2)
