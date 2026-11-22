import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
from upload import upload, DIR
from cidutil import cidv1_raw

res = upload(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin"), "hca-kb1-e2e.bin")
tok = api.ensure_token()
# 创建文件条目，content_identity = 根 CID
req = s(3, DIR) + s(4, "hca-kb1-e2e.bin") + s(6, "application/octet-stream") + i(7, res["size"]) + s(24, cidv1_raw(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin"), "rb").read()))
r, body = api.rpc("v6.services.pub.PubUserFile", "Create", req, token=tok)
print("Create ->", r["http"], r["status"], r["message"])
print(json.dumps(body, ensure_ascii=False)[:600])
json.dump(res, open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_kb1.json"), "w"), indent=2)
