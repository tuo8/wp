import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
r, body = api.rpc("v6.services.pub.PubUserFile", "List", s(1, s(1, DIR)) + s(3, i(2, 50)), token=tok)
files = body.get(1, [])
if isinstance(files, dict): files = [files]
ident, cid = None, None
for f in files:
    if f.get(4) == "hca-up-mb10.bin":
        ident, cid = f.get(1), f.get(24)
print("identity", ident, "\ncid", cid)
r, pb = api.rpc("v6.services.pub.PubUserFile", "ParseFileSlice", s(1, ident), token=tok)
print("ParseFileSlice:", json.dumps(pb, ensure_ascii=False)[:800])
r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                s(1, cid) + i(2, 1) + s(3, "hca-up-mb10.bin") + i(5, 1), token=tok)
print("\nGetSliceDownloadAddress:", r["http"], r["status"], r["message"])
print(json.dumps(sd, ensure_ascii=False)[:900])
r, dd = api.rpc("v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
                s(1, ident) + i(2, 1) + s(3, "/hca-2026-11-23/hca-up-mb10.bin") + i(9, 3600), token=tok)
print("\nGetDirectDownloadAddress:", r["http"], r["status"], r["message"])
print(json.dumps(dd, ensure_ascii=False)[:700])
