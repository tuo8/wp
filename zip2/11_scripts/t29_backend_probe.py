"""第二轮：后端字段与源站行为探测（只读，使用自有测试对象）。"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api, hcc
from hcc import s, i, S
from cidutil import sha256hex, cidv1_raw, cidv1_dagpb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "07_storage_backend", "backend_probe_round2.json")
tok = api.ensure_token()
rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "probes": {}}

KB1 = "bafkreiaq6bczrpqp6szuzpb6slnhtmjbu52k3p5yik2xl54lcu3avbz44y"
MB10_ROOT = "bafybeiesgdftdz5o2sauh7ohyszoiqqwqcskkxjeleoc75nbctxboxpck4"
MB10_LEAF = "bafkreiccgyrrt57i5i7igzdnhcwdqfpkqw4njn3mhxuvgv6xrn67ikopd4"
GB_ROOT = "bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4"
GB_LEAF = "bafkreibixcbkqexjahswi2lsfv2cgmd5ueyf2j2u3z3n3i7eiofdssr5ba"

# ---------- 1. GetSliceDownloadAddress 全字段 ----------
def addr_full(cid, count=1):
    r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                    s(1, cid) + i(2, 1) + i(5, count), token=tok)
    return r, sd

fields = {}
for label, cid in (("kb1_leaf", KB1), ("mb10_root", MB10_ROOT), ("gb_root", GB_ROOT), ("gb_leaf", GB_LEAF)):
    r, sd = addr_full(cid)
    fields[label] = {"grpc": r["status"], "ms": r["ms"], "raw_fields": sd,
                     "addresses_count": len(sd.get(1, [])) if isinstance(sd.get(1), list) else 1}
    print(label, "->", json.dumps(sd, ensure_ascii=False)[:400])
rec["probes"]["slice_address_fields"] = fields

# 批量取址：一次要 8 个，看返回几个 / 字段差异
r, sd = addr_full(GB_ROOT, 8)
addrs = sd.get(1, [])
rec["probes"]["batch_addresses"] = {
    "requested": 8, "returned": len(addrs) if isinstance(addrs, list) else 1,
    "sample": addrs[:3] if isinstance(addrs, list) else addrs,
}
print("batch:", rec["probes"]["batch_addresses"]["returned"])

# ---------- 2. 取块：常规 vs no-cache，抓全部响应头 ----------
def fetch_headers(cid, hdr=None, tag=None):
    r, sd = addr_full(cid)
    info = sd[1] if isinstance(sd.get(1), dict) else sd[1][0]
    url, key = info[2], info.get(4) or 0
    t0 = time.time()
    rr = S.get(url, headers=hdr or {}, timeout=90)
    body = rr.content
    out = {"tag": tag, "cid": cid, "http": rr.status_code, "bytes": len(body),
           "ms": round((time.time() - t0) * 1000),
           "headers": {k.lower(): v for k, v in rr.headers.items()},
           "sha_ok": (cidv1_raw(bytes(b ^ key for b in body)) == cid or cidv1_dagpb(bytes(b ^ key for b in body)) == cid) if body else None,
           "url_path": url.split("?")[0].split(".xyz")[-1]}
    return out

base = fetch_headers(GB_LEAF, tag="normal")
nc = fetch_headers(GB_LEAF, {"Cache-Control": "no-cache", "Pragma": "no-cache"}, tag="no_cache")
nc2 = fetch_headers(GB_LEAF, {"Cache-Control": "no-store"}, tag="no_store")
rec["probes"]["slice_normal"] = base
rec["probes"]["slice_no_cache"] = nc
rec["probes"]["slice_no_store"] = nc2
for k in ("slice_normal", "slice_no_cache", "slice_no_store"):
    p = rec["probes"][k]
    print(k, p["http"], p["bytes"], "via=", p["headers"].get("via"), "age=", p["headers"].get("age"),
          "server=", p["headers"].get("server"), "x-cache=", p["headers"].get("x-cache"))

# HEAD 同一块
r, sd = addr_full(GB_LEAF)
info = sd[1]
h = S.head(info[2], timeout=60)
rec["probes"]["slice_head"] = {"http": h.status_code, "headers": {k.lower(): v for k, v in h.headers.items()}}

# ---------- 3. 不存在的块：错误形态 ----------
FAKE = "bafkreiaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
r, sd = addr_full(FAKE)
info = sd[1] if isinstance(sd.get(1), dict) else (sd.get(1) or [{}])[0]
missing = {"grpc": r["status"], "msg": r["message"], "addr_present": bool(info.get(2)) if isinstance(info, dict) else None}
if isinstance(info, dict) and info.get(2):
    rr = S.get(info[2], timeout=60)
    missing.update({"http": rr.status_code, "bytes": len(rr.content),
                    "body_head": rr.text[:300], "headers": {k.lower(): v for k, v in rr.headers.items()}})
rec["probes"]["missing_block"] = missing
print("missing:", json.dumps(missing, ensure_ascii=False)[:300])

# ---------- 4. 网关：no-cache 强制回源 ----------
r, dd = api.rpc("v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
                s(1, "59227eb8d779e2abfa7d66f901124a77") + i(2, 1) + s(3, "/hca-2026-11-23/CAWD-987-AI.mp4") + i(9, 3600), token=tok)
gurl = dd[1]
g1 = S.head(gurl, timeout=60)
g2 = S.get(gurl, headers={"Range": "bytes=0-1023", "Cache-Control": "no-cache", "Pragma": "no-cache"}, timeout=60)
rec["probes"]["gateway_head"] = {"http": g1.status_code, "headers": {k.lower(): v for k, v in g1.headers.items()}}
rec["probes"]["gateway_range_nocache"] = {"http": g2.status_code, "bytes": len(g2.content),
                                          "headers": {k.lower(): v for k, v in g2.headers.items()}}
print("gw head:", g1.status_code, g1.headers.get("via"), "| gw range:", g2.status_code, g2.headers.get("via"))

# ---------- 5. 删除对象的再读取（t+~50 min） ----------
DEL_ROOT = MB10_ROOT
DEL_LEAF = MB10_LEAF
r, sd = addr_full(DEL_ROOT)
info = sd[1] if isinstance(sd.get(1), dict) else (sd.get(1) or [{}])[0]
after = {"grpc": r["status"], "msg": r["message"], "has_url": bool(info.get(2)) if isinstance(info, dict) else False}
if after["has_url"]:
    rr = S.get(info[2], timeout=60)
    key = info.get(4) or 0
    after.update({"http": rr.status_code, "raw_bytes": len(rr.content),
                  "dec_bytes": len(rr.content),
                  "via": rr.headers.get("via"), "age": rr.headers.get("age"),
                  "server": rr.headers.get("server")})
rec["probes"]["deleted_object_reread"] = after
print("deleted reread:", json.dumps(after, ensure_ascii=False)[:300])

json.dump(rec, open(OUT, "w"), indent=1, ensure_ascii=False)
print("->", OUT)
