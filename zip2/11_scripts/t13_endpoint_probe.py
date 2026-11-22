import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
req = s(3, DIR) + s(4, "probe-endpoint.bin") + s(6, "application/octet-stream") + i(7, 1024)
r, task = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
addr, sess = task[2], task[3]
print("addr", addr)
cands = [f"{addr}/{sess}", f"{addr}/{sess}/finish", f"{addr}/{sess}/commit",
         f"{addr}/{sess}/complete", f"{addr}/finish", f"{addr}/commit"]
for u in cands:
    for m in ("GET", "POST"):
        try:
            rr = S.request(m, u, data=b"" if m == "POST" else None, timeout=20)
            print(f"{m} {u.split('/int/blocks')[1][:60]:60s} -> {rr.status_code} {rr.text[:60]!r}")
        except Exception as e:
            print(m, u, "ERR", e)
