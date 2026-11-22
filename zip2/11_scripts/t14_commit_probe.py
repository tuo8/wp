import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import cidv1_raw
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
data = open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin"), "rb").read()
cid = cidv1_raw(data)
shapes = [
    ("empty list", []),
    ("list of str", [cid]),
    ("obj blocks str", {"blocks": [cid]}),
    ("obj blocks objs", {"blocks": [{"cid": cid, "size": len(data)}]}),
    ("obj blocks id/size", {"blocks": [{"id": cid, "size": len(data), "index": 0}]}),
    ("obj list", {"block_list": [cid]}),
    ("obj list size", {"block_list": [{"id": cid, "size": len(data)}]}),
]
for label, body in shapes:
    req = s(3, DIR) + s(4, "probe-commit.bin") + s(6, "application/octet-stream") + i(7, len(data))
    r, task = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
    sess = task[3]
    rr = S.post(task[2] + "/" + sess, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}, timeout=30)
    print(f"{label:22s} -> {rr.status_code} {rr.text[:160]!r}")
