"""3.15 GiB 对象的 DAG 结构探测：层级、扇出、块数、块大小分布；以及网关 Range 行为。"""
import sys, os, json, time, base64, hashlib
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api, hcc
from hcc import s, i, S, dec
from dagbuild import b32
from cidutil import sha256hex

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "05_cid_and_chunks", "gb_structure.json")
tok = api.ensure_token()
IDENT = "59227eb8d779e2abfa7d66f901124a77"
CID = "bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4"
NAME = "CAWD-987-AI.mp4"
SIZE = 3381078505
rec = {"target": {"name": NAME, "identity": IDENT, "cid": CID, "size": SIZE}, "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}

# ---- 1. 网关 Range 行为 ----
r, dd = api.rpc("v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
                s(1, IDENT) + i(2, 1) + s(3, "/hca-2026-11-23/" + NAME) + i(9, 7200), token=tok)
url = dd[1]
rec["gateway_url_shape"] = url.split("?")[0]
h0 = S.head(url, timeout=60)
rec["gateway_head"] = {"http": h0.status_code, "len": h0.headers.get("content-length"),
                       "etag": h0.headers.get("etag"), "server": h0.headers.get("server"),
                       "via": h0.headers.get("via"), "accept_ranges": h0.headers.get("accept-ranges")}
rg = S.get(url, headers={"Range": "bytes=0-1023"}, timeout=60)
rec["gateway_range_0_1023"] = {"http": rg.status_code, "bytes": len(rg.content),
                               "content_range": rg.headers.get("content-range"),
                               "etag": rg.headers.get("etag"), "via": rg.headers.get("via"),
                               "sha256_prefix_block": sha256hex(rg.content)}
rg2 = S.get(url, headers={"Range": "bytes=3381077505-3381078504"}, timeout=60)
rec["gateway_range_tail"] = {"http": rg2.status_code, "bytes": len(rg2.content),
                             "content_range": rg2.headers.get("content-range")}
print("gateway:", rec["gateway_head"], rec["gateway_range_0_1023"]["http"], rec["gateway_range_tail"])

# ---- 2. 分片地址批量行为 ----
def slice_addr(cid, count=1):
    r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                    s(1, cid) + i(2, 1) + i(5, count), token=tok)
    return r, sd

r, sd = slice_addr(CID, 1)
root_info = sd[1]
rec["slice_root_addr"] = {"url": root_info[2].split("?")[0], "encrypt": root_info[4],
                          "cid": root_info[1], "ms": r["ms"]}
t0 = time.time()
rr = S.get(root_info[2], timeout=60)
root_raw = rr.content
key = root_info[4] or 0
root_data = bytes(b ^ key for b in root_raw) if key else root_raw
rec["root_block"] = {"http": rr.status_code, "raw": len(root_raw), "dec": len(root_data),
                     "encrypt": key, "etag": rr.headers.get("etag"), "via": rr.headers.get("via"),
                     "sha256": sha256hex(root_data), "ms": round((time.time()-t0)*1000)}
node = dec(root_data)
data = node[1][0]
udata = dec(data)
dtype, dsize, dbs = udata.get(1, [None])[0], udata.get(3, [0])[0], udata.get(4, [])
links = node.get(2, [])
if isinstance(links, bytes): links = [links]
link_cids = []
for lk in links:
    f = dec(lk)
    link_cids.append((b32(f[1][0]), f.get(3, [0])[0]))
rec["root_node"] = {"unixfs_type": dtype, "filesize_field": dsize, "declared_size": SIZE,
                    "blocksizes_count": len(dbs), "blocksizes_sum": sum(dbs),
                    "links": len(link_cids), "node_bytes": len(root_data),
                    "cid_recompute": b32(b"\x01\x70\x12\x20" + hashlib.sha256(root_data).digest()),
                    "cid_match": b32(b"\x01\x70\x12\x20" + hashlib.sha256(root_data).digest()) == CID}
print("root:", rec["root_node"])

# ---- 3. 一层下降：判断子节点是叶子(raw)还是中间节点(dag-pb) ----
level1 = []
for cid, tsize in link_cids[:8]:
    r, sd = slice_addr(cid, 1)
    info = sd[1]
    k = info[4] or 0
    x = S.get(info[2], timeout=60).content
    xd = bytes(b ^ k for b in x) if k else x
    is_pb = xd[:2] == b"\x0a" or (len(xd) > 8 and xd[0] in (0x0a, 0x12))
    sub = dec(xd)
    sublinks = sub.get(2, [])
    if isinstance(sublinks, bytes): sublinks = [sublinks]
    subsize = dec(sub[1][0]).get(3, [0])[0] if 1 in sub else None
    level1.append({"cid": cid, "tsize": tsize, "dec_bytes": len(xd), "unixfs_filesize": subsize,
                   "sublinks": len(sublinks), "sha_ok": sha256hex(xd) == hashlib.sha256(xd).hexdigest(),
                   "cid_recompute_match": (b32(b"\x01\x70\x12\x20" + hashlib.sha256(xd).digest()) == cid
                                           or b32(b"\x01\x55\x12\x20" + hashlib.sha256(xd).digest()) == cid)})
    print(" child", cid[:24], len(xd), "sublinks", len(sublinks), "filesize", subsize)
rec["level1_children"] = level1
rec["structure"] = ("two_level_dag" if level1 and level1[0]["sublinks"] > 0 else
                    "one_level_dag_leaves_at_root" if level1 else "unknown")
json.dump(rec, open(OUT, "w"), indent=2, ensure_ascii=False)
print("structure:", rec["structure"], "-> ", OUT)
