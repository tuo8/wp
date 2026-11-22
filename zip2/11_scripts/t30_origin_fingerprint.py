"""第二轮：源站/缓存分层与错误指纹探测（只读，全部为自己账号内的公开读取）。"""
import sys, os, json, time, base64, hashlib, socket
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api, hcc
from hcc import s, i, S
from cidutil import cidv1_raw, cidv1_dagpb, sha256hex

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "07_storage_backend", "origin_fingerprint.json")
tok = api.ensure_token()
rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "egress": {}, "tests": {}}

# 出口网络（公开 IP 归属查询）
try:
    import requests
    ss = requests.Session(); ss.trust_env = False
    rec["egress"] = ss.get("http://ip-api.com/json/?fields=query,country,isp,as", timeout=12).json()
except Exception as e:
    rec["egress"] = {"error": repr(e)}

def addr(cid, count=1):
    r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                    s(1, cid) + i(2, 1) + i(5, count), token=tok)
    a = sd.get(1) if sd else None
    if isinstance(a, list):
        a = a[0] if a else None
    return r, a

def get_block(cid, extra_headers=None, ipv4=False, url_override=None):
    r, a = addr(cid)
    if not a or not a.get(2):
        return {"stage": "addr", "grpc": r["status"], "msg": r["message"], "raw": a}
    url = url_override or a[2]
    key = a.get(4) or 0
    sess = S
    # 强制 IPv4：不改 URL（保留 SNI），只把 getaddrinfo 结果过滤为 AF_INET
    _orig_gai = None
    if ipv4:
        import requests
        sess = requests.Session(); sess.trust_env = False
        _orig_gai = socket.getaddrinfo
        def _v4(host, port, family=0, *a, **kw):
            return [x for x in _orig_gai(host, port, socket.AF_INET, *a, **kw) if x[0] == socket.AF_INET]
        socket.getaddrinfo = _v4
    t0 = time.time()
    try:
        rr = sess.get(url, headers=extra_headers or {}, timeout=90)
    finally:
        if _orig_gai is not None:
            socket.getaddrinfo = _orig_gai
    body = rr.content
    ok = None
    if rr.status_code == 200 and len(body) == 4194304:
        ok = (cidv1_raw(bytes(b ^ key for b in body)) == cid or cidv1_dagpb(bytes(b ^ key for b in body)) == cid)
    return {"http": rr.status_code, "bytes": len(body), "ms": round((time.time() - t0) * 1000),
            "sha_ok": ok, "error_body": None if ok or rr.status_code == 200 else body[:200].decode("utf-8", "replace"),
            "headers": {k.lower(): v for k, v in rr.headers.items()}}

# ---------- 1. 合法但不存在于本账号的 CID：错误指纹 ----------
absent_raw = cidv1_raw(b"halalcloud-second-pass-nonexistent-" + os.urandom(8))
absent_pb = cidv1_dagpb(b"halalcloud-second-pass-nonexistent-dagpb-" + os.urandom(8))
rec["tests"]["absent_raw_cid"] = {"cid": absent_raw, **get_block(absent_raw)}
rec["tests"]["absent_dagpb_cid"] = {"cid": absent_pb, **get_block(absent_pb)}
for k in ("absent_raw_cid", "absent_dagpb_cid"):
    t = rec["tests"][k]
    print(k, t.get("http"), t.get("bytes"), "|", (t.get("error_body") or "")[:120])
    print("   via:", (t.get("headers") or {}).get("via"), "| x-exception:", (t.get("headers") or {}).get("x-exception-info"),
          "| x-resp-cache:", (t.get("headers") or {}).get("x-response-cache"))

# ---------- 2. 缓存分层：同一块，默认(IPv6/境外出口) vs 强制 IPv4 ----------
LEAF = "bafkreibixcbkqexjahswi2lsfv2cgmd5ueyf2j2u3z3n3i7eiofdssr5ba"
sample = []
for k in range(6):
    g = get_block(LEAF)
    h = g.get("headers", {})
    sample.append({"mode": "default", "n": k, "http": g["http"], "ms": g["ms"],
                   "via": h.get("via"), "age": h.get("age"),
                   "x_response_cache": h.get("x-response-cache"),
                   "x_bdcdn": h.get("x-bdcdn-cache-status")})
    time.sleep(1.5)
for k in range(3):
    try:
        g = get_block(LEAF, ipv4=True)
        h = g.get("headers", {})
        sample.append({"mode": "force_ipv4", "n": k, "http": g["http"], "ms": g["ms"],
                       "via": h.get("via"), "age": h.get("age"),
                       "x_response_cache": h.get("x-response-cache"),
                       "x_bdcdn": h.get("x-bdcdn-cache-status")})
    except Exception as e:
        sample.append({"mode": "force_ipv4", "n": k, "error": type(e).__name__ + ": " + str(e)[:120]})
    time.sleep(1.5)
rec["tests"]["cache_layer_samples"] = sample
for x in sample:
    print(f"  {x['mode']:11} {x['ms']:>5}ms via={str(x['via'])[:70]} age={x['age']} cache={x['x_response_cache']}")

# ---------- 3. 缓存键：附加无关 query 参数是否回源 ----------
r, a = addr(LEAF)
base_url = a[2]
bust = base_url + "&_cb=1"
g = get_block(LEAF, url_override=bust)
h = g.get("headers", {})
rec["tests"]["cache_key_probe"] = {"added_param": "_cb=1", "http": g["http"], "ms": g["ms"],
                                   "via": h.get("via"), "age": h.get("age"),
                                   "x_response_cache": h.get("x-response-cache"),
                                   "x_bdcdn": h.get("x-bdcdn-cache-status")}
print("cachebust:", rec["tests"]["cache_key_probe"]["ms"], "ms", rec["tests"]["cache_key_probe"]["x_response_cache"],
      (rec["tests"]["cache_key_probe"]["via"] or "")[:60])

# ---------- 4. 已删除对象的叶子块再读（t+~55 min） ----------
DEL_LEAF = "bafkreiccgyrrt57i5i7igzdnhcwdqfpkqw4njn3mhxuvgv6xrn67ikopd4"
g = get_block(DEL_LEAF)
h = g.get("headers", {})
rec["tests"]["deleted_leaf_reread"] = {"http": g["http"], "bytes": g["bytes"], "ms": g["ms"],
                                       "sha_ok": g["sha_ok"], "via": h.get("via"), "age": h.get("age"),
                                       "x_response_cache": h.get("x-response-cache")}
print("deleted leaf:", rec["tests"]["deleted_leaf_reread"])

# ---------- 5. 网关错误指纹：合法但不存在的根 ----------
r, dd = api.rpc("v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
                s(1, "59227eb8d779e2abfa7d66f901124a77") + i(2, 1) + s(3, "/hca-2026-11-23/CAWD-987-AI.mp4") + i(9, 3600), token=tok)
gurl = dd[1]
# 把 URL 里的 cid 换成不存在的（保留 auth_key），观察网关错误体质
bad = gurl.replace("bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4", absent_pb)
rr = S.get(bad, timeout=60)
rec["tests"]["gateway_absent_cid"] = {"http": rr.status_code, "bytes": len(rr.content),
                                      "body_head": rr.text[:300],
                                      "headers": {k.lower(): v for k, v in rr.headers.items()}}
print("gateway absent:", rr.status_code, rr.text[:150].replace("\n", " "))

json.dump(rec, open(OUT, "w"), indent=1, ensure_ascii=False)
print("->", OUT)
