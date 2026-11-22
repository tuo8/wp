import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
meta = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))
root = meta["content_identity"]
for name in ["hca-fastdup-1.bin", "hca-fastdup-2.bin"]:
    r, body = api.rpc("v6.services.pub.PubUserFile", "Create",
                      s(3, DIR) + s(4, name) + s(6, "application/octet-stream") + i(7, meta["size"]) + s(24, root), token=tok)
    print(f"{name}: http={r['http']} grpc={r['status']} msg={r['message']}")
    print("   ", json.dumps(body, ensure_ascii=False)[:300] if body else None)
