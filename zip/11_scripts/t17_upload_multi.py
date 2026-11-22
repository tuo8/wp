import sys, os, json, time
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import cidv1_raw, sha256hex
tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
fname = sys.argv[1]
p = os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", fname)
data = open(p, "rb").read()
name = fname.replace("hca-", "hca-up-")
t_all = time.time()
req = s(3, DIR) + s(4, name) + s(6, "application/octet-stream") + i(7, len(data))
r, task = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
addr, sess, bsz = task[2], task[3], task[16]
print(f"file={name} size={len(data)} sha256={sha256hex(data)} block_size={bsz}")
blocks = [data[o:o+bsz] for o in range(0, len(data), bsz)]
cids = []
t_up = time.time()
for idx, b in enumerate(blocks):
    c = cidv1_raw(b); cids.append(c)
    t0 = time.time()
    rr = S.post(f"{addr}/{sess}/{c}", data=b, headers={"Content-Type": "application/octet-stream"}, timeout=300)
    print(f"  block[{idx}] size={len(b)} cid={c} -> {rr.status_code} {round((time.time()-t0)*1000)}ms")
t_commit = time.time()
rr = S.post(addr + "/" + sess, data=json.dumps(cids).encode(), headers={"Content-Type": "application/json"}, timeout=300)
print("commit ->", rr.status_code, "in", round((time.time()-t_commit)*1000), "ms")
try:
    rec = rr.json()
    print("  server record: size=", rec.get("size"), "content_identity=", rec.get("content_identity"),
          "mime=", rec.get("mime_type"), "parent=", rec.get("parent"))
except Exception:
    print("  body:", rr.text[:200]); rec = {}
print("total upload time:", round(time.time() - t_all, 2), "s")
out = {"file": name, "size": len(data), "sha256": sha256hex(data), "block_size": bsz,
       "n_blocks": len(blocks), "block_cids": cids, "content_identity": rec.get("content_identity"),
       "upload_seconds": round(time.time() - t_all, 2), "commit_http": rr.status_code}
json.dump(out, open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_%s.json" % fname), "w"), indent=2)
