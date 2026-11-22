import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
parent = s(1, DIR)
req = s(1, parent) + s(3, i(2, 50))
r, body = api.rpc("v6.services.pub.PubUserFile", "List", req, token=tok)
files = body.get(1, [])
if isinstance(files, dict):
    files = [files]
print("files in hca dir:", len(files))
for f in files:
    print(" ", json.dumps(f, ensure_ascii=False)[:400])
