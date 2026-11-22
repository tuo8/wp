import sys, os, json, time, hashlib
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import cidv1_raw, sha256hex
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
p = os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin")
data = open(p, "rb").read()
name = "hca-kb1-full.bin"
req = s(3, DIR) + s(4, name) + s(6, "application/octet-stream") + i(7, len(data))
r, task = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
addr, sess, bsz = task[2], task[3], task[16]
print("addr", addr, "bsz", bsz)
blocks = [data[o:o+bsz] for o in range(0, len(data), bsz)] or [b""]
cids = [cidv1_raw(b) for b in blocks]
for c, b in zip(cids, blocks):
    rr = S.post(addr + "/" + sess + "/" + c, data=b, headers={"Content-Type": "application/octet-stream"}, timeout=120)
    print("  put", c, len(b), "->", rr.status_code)
rr = S.post(addr + "/" + sess, data=json.dumps(cids).encode(), headers={"Content-Type": "application/json"}, timeout=120)
print("commit ->", rr.status_code, rr.text[:200])
json.dump({"name": name, "size": len(data), "sha256": sha256hex(data), "cids": cids,
           "commit_http": rr.status_code, "commit_body": rr.text},
          open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "commit_kb1.json"), "w"), indent=2)
