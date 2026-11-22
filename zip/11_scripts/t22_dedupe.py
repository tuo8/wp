import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
meta = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))
cids = meta["block_cids"]
# 用同内容、不同文件名，尝试不传块直接提交
name = "hca-up-mb10-dedupe.bin"
r, task = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask",
                  s(3, DIR) + s(4, name) + s(6, "application/octet-stream") + i(7, meta["size"]), token=tok)
print("created flag(1):", task.get(1), "task:", task.get(3)[:40])
addr, sess = task[2], task[3]
rr = S.post(addr + "/" + sess, data=json.dumps(cids).encode(),
            headers={"Content-Type": "application/json"}, timeout=60)
print("commit-without-upload ->", rr.status_code, rr.text[:260])
json.dump({"name": name, "commit_http": rr.status_code, "body": rr.text},
          open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "dedupe_test.json"), "w"), indent=2)
