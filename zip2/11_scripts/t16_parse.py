import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
r, body = api.rpc("v6.services.pub.PubUserFile", "List", s(1, s(1, DIR)) + s(3, i(2, 50)), token=tok)
files = body.get(1, [])
if isinstance(files, dict): files = [files]
print("files:", len(files))
for f in files:
    print(" ", f.get(1), f.get(4), f.get(7), "content_identity=", f.get(24), "store_type=", f.get(26))
ident = None
for f in files:
    if f.get(4) == "hca-kb1-full.bin":
        ident = f.get(1); cid = f.get(24)
print("identity", ident, "cid", cid)
if ident:
    r, pb = api.rpc("v6.services.pub.PubUserFile", "ParseFileSlice", s(1, ident), token=tok)
    print("ParseFileSlice ->", r["http"], r["status"], r["message"])
    print(json.dumps(pb, ensure_ascii=False, indent=1)[:900])
