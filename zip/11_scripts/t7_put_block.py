import sys, os, json, time
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, dec, S
from cidutil import cidv1_raw

tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
NAME = sys.argv[1] if len(sys.argv) > 1 else "hca-kb1.bin"
p = "03_upload/testcorpus/hca-kb1.bin"
if not os.path.exists(p):
    p = os.path.join("..", "03_upload", "testcorpus", "hca-kb1.bin")
data = open(p, "rb").read()
print("file bytes", len(data))

req = s(3, DIR) + s(4, NAME) + s(6, "application/octet-stream") + i(7, len(data))
r, body = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
if 2 not in body:
    print("TASK_BODY:", json.dumps(body, ensure_ascii=False))
    sys.exit(2)
addr = body[2]; sess = body[3]
print("upload_address:", addr)
print("block_size:", body.get(16), "hash_type:", body.get(17), "codec:", body.get(19))

cid = cidv1_raw(data)
print("block cid:", cid)

# 尝试 PUT
for url in [f"{addr}/{sess}/{cid}"]:
    h = {"Content-Type": "application/octet-stream", "Content-Length": str(len(data))}
    t0 = time.time()
    rr = S.put(url, data=data, headers=h, timeout=60)
    print("PUT", url[:120])
    print("  ->", rr.status_code, "ms", round((time.time()-t0)*1000), "len", len(rr.content))
    for k in ("server","content-type","via","x-cache","etag","date"):
        if k in rr.headers: print("  ", k, ":", rr.headers[k])
    print("  body:", rr.text[:300])
