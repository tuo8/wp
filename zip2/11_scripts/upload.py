"""当前线上上传协议复核：CreateUploadTask -> POST /int/blocks/{session}/{cid} -> Create(content_identity)"""
import sys, os, json, time, hashlib
sys.path.insert(0, "/".join(os.path.dirname(os.path.abspath(__file__)).split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import cidv1_raw, sha256hex

DIR = "b648aca0cc94070f0de2a016b4aded76"

def upload(path, remote_name, parent=DIR, block_size=4194304):
    tok = api.ensure_token()
    data = open(path, "rb").read()
    digest = sha256hex(data)
    req = s(3, parent) + s(4, remote_name) + s(6, "application/octet-stream") + i(7, len(data))
    r, task = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
    assert 2 in task, task
    addr, sess = task[2], task[3]
    bsz = task.get(16, block_size)
    blocks = [data[o:o+bsz] for o in range(0, len(data), bsz)] or [b""]
    cids = []
    t_start = time.time()
    for idx, blk in enumerate(blocks):
        c = cidv1_raw(blk)
        cids.append(c)
        t0 = time.time()
        rr = S.post(addr + "/" + sess + "/" + c, data=blk,
                    headers={"Content-Type": "application/octet-stream"}, timeout=180)
        api.log({"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "op": "PUT_BLOCK", "file": remote_name,
                 "block_index": idx, "block_size": len(blk), "cid": c,
                 "http": rr.status_code, "ms": round((time.time()-t0)*1000),
                 "server": rr.headers.get("server"), "via": rr.headers.get("via"),
                 "content_length": rr.headers.get("content-length")})
        print(f"  block {idx} {len(blk)}B cid={c} -> {rr.status_code} {round((time.time()-t0)*1000)}ms")
    root = cids[0] if len(cids) == 1 else None
    print(f"  file={remote_name} size={len(data)} sha256={digest} blocks={len(cids)} root={root}")
    return {"file": remote_name, "size": len(data), "sha256": digest, "blocks": cids,
            "root_cid": root, "addr": addr, "session": sess, "block_size": bsz,
            "elapsed_s": round(time.time()-t_start, 2)}
