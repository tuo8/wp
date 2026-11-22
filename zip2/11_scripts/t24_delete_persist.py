import sys, os, json, time, hashlib
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S
from cidutil import sha256hex

tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
meta = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))
root = meta["content_identity"]
OUT = os.path.join(os.path.dirname(__file__), "..", "08_persistence_tests", "delete_persist.json")
events = []

def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S")

def listing():
    r, body = api.rpc("v6.services.pub.PubUserFile", "List", s(1, s(1, DIR)) + s(3, i(2, 100)), token=tok)
    files = body.get(1, [])
    if isinstance(files, dict): files = [files]
    return [(f.get(4), f.get(1), f.get(24)) for f in files]

before = listing()
print("before:", before)
targets = [f for f in before if f[0] in ("hca-up-mb10.bin", "hca-fastdup-1.bin", "hca-fastdup-2.bin")]
print("targets:", targets)

# 删除前先取一次直连地址
ident = targets[0][1]
r, dd = api.rpc("v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
                s(1, ident) + i(2, 1) + s(3, "/hca-2026-11-23/" + targets[0][0]) + i(9, 7200), token=tok)
old_url = dd[1]
print("presigned url captured")

# 批量删除（source = 各 File{identity}）
src = b""
for _, identity, _cid in targets:
    f = s(1, identity)
    src += s(1, f)
r, body = api.rpc("v6.services.pub.PubUserFile", "Delete", src, token=tok)
print("Delete ->", r["http"], r["status"], r["message"], json.dumps(body, ensure_ascii=False)[:200] if body else "")
t_delete = now()
events.append({"ts": t_delete, "event": "delete", "http": r["http"], "grpc": r["status"], "resp": body})

def probe(tag):
    rec = {"ts": now(), "tag": tag}
    rec["listing"] = listing()
    rr = S.get(old_url, timeout=60)
    rec["presigned_old_url"] = {"http": rr.status_code, "bytes": len(rr.content),
                                "sha_ok": sha256hex(rr.content) == sha256hex(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-mb10.bin"), "rb").read()) if rr.status_code == 200 else None,
                                "via": rr.headers.get("via"), "age": rr.headers.get("age")}
    r2, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress", s(1, root) + i(2, 1) + i(5, 1), token=tok)
    rec["new_slice_addr"] = {"grpc": r2["status"], "msg": r2["message"],
                             "has_url": bool(sd and 1 in sd)}
    rr2 = S.get(sd[1][2], timeout=60) if rec["new_slice_addr"]["has_url"] else None
    if rr2 is not None:
        rec["new_slice_fetch"] = {"http": rr2.status_code, "bytes": len(rr2.content)}
    events.append(rec)
    print(json.dumps(rec, ensure_ascii=False)[:400])
    json.dump(events, open(OUT, "w"), indent=2, ensure_ascii=False)

for delay, tag in ((0, "t+0s"), (10, "t+10s"), (60, "t+70s"), (120, "t+190s"), (300, "t+490s")):
    time.sleep(delay)
    probe(tag)
print("done")
