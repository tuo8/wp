import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
from cidutil import cidv1_raw
tok = api.ensure_token()
cid = cidv1_raw(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin"), "rb").read())
print("cid", cid)
# ParseFileSlice(File{identity=cid})
for label, req in [
    ("identity=cid", s(1, cid)),
    ("content_identity=cid", s(24, cid)),
]:
    r, body = api.rpc("v6.services.pub.PubUserFile", "ParseFileSlice", req, token=tok)
    print(label, "->", r["http"], r["status"], r["message"])
    print("   ", json.dumps(body, ensure_ascii=False)[:500])
# GetSliceDownloadAddress([cid])
req = s(1, cid) + i(2, 1) + i(5, 1)
r, body = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress", req, token=tok)
print("sliceaddr ->", r["http"], r["status"], r["message"])
print("   ", json.dumps(body, ensure_ascii=False)[:700])
