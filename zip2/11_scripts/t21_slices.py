import sys, os, json, base64, time
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, S, dec
from cidutil import sha256hex
from dagbuild import b32

tok = api.ensure_token()
NAME = "hca-up-mb10.bin"
orig = open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-mb10.bin"), "rb").read()
meta = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))
root_cid = meta["content_identity"]

def slice_get(identity):
    r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                    s(1, identity) + i(2, 1) + i(5, 1), token=tok)
    info = sd[1]
    url, key = info[2], info[4]
    t0 = time.time()
    rr = S.get(url, timeout=120)
    data = bytes(b ^ key for b in rr.content) if key else rr.content
    return {"identity": info[1], "url": url.split("?")[0], "encrypt": key, "http": rr.status_code,
            "raw_bytes": len(rr.content), "dec_bytes": len(data), "data": data,
            "server": rr.headers.get("server"), "via": rr.headers.get("via"),
            "etag": rr.headers.get("etag"), "ms": round((time.time()-t0)*1000)}

print("== 分片链路 ==")
root = slice_get(root_cid)
print(f"  root node: {root['dec_bytes']}B encrypt={root['encrypt']} etag={root['etag']}")
node = dec(root["data"])
links = node.get(2, [])
if isinstance(links, bytes): links = [links]
print(f"  dag-pb links: {len(links)}")
parts, child_info = [], []
for lk in links:
    f = dec(lk)
    cid_bytes = f[1][0]
    child = b32(cid_bytes)
    got = slice_get(child)
    parts.append(got["data"])
    child_info.append({k: v for k, v in got.items() if k != "data"} | {"cid": child})
    print(f"    child {child[:20]}... {got['dec_bytes']}B encrypt={got['encrypt']} etag={got['etag']} ms={got['ms']}")
joined = b"".join(parts)
print("\n  joined bytes:", len(joined), " sha_match:", sha256hex(joined) == sha256hex(orig), " byte_equal:", joined == orig)
json.dump({"root": {k: v for k, v in root.items() if k != "data"}, "children": child_info,
           "joined_bytes": len(joined), "sha256_ok": joined == orig},
          open(os.path.join(os.path.dirname(__file__), "..", "04_download", "slice_download.json"), "w"), indent=2)
