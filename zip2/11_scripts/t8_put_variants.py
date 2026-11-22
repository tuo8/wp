import sys, os, json, time
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import cidv1_raw
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
data = open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-kb1.bin"), "rb").read()
name = sys.argv[1]
req = s(3, DIR) + s(4, name) + s(6, "application/octet-stream") + i(7, len(data))
r, body = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
addr, sess = body[2], body[3]
cid = cidv1_raw(data)
import urllib.parse
variants = [
    ("POST path", "POST", f"{addr}/{sess}/{cid}", {}),
    ("PUT query", "PUT", f"{addr}?session={urllib.parse.quote(sess)}&cid={cid}", {}),
    ("POST bare+headers", "POST", addr, {"X-Upload-Session": sess, "X-Block-Cid": cid}),
    ("PUT bare+headers", "PUT", addr, {"X-Upload-Session": sess, "X-Block-Cid": cid}),
]
for label, m, url, extra in variants:
    h = {"Content-Type": "application/octet-stream"}
    h.update(extra)
    try:
        t0 = time.time()
        rr = S.request(m, url, data=data, headers=h, timeout=40)
        print(f"{label}: {rr.status_code} {round((time.time()-t0)*1000)}ms server={rr.headers.get('server')} via={rr.headers.get('via')} allow={rr.headers.get('allow')} body={rr.text[:120]!r}")
    except Exception as e:
        print(label, "ERR", e)
