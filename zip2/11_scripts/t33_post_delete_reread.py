"""第二轮 §5：删除后长窗口复读 + 缓存/源站区分 + 块是否仍被保留。

1. 重新申请分片地址（每次 auth_key 不同 => 缓存键不同，命中即说明源站仍有块）
2. 校验取回字节的 CID，记录 via / age / x-response-cache
3. 用已删除对象的 content_identity 做一次秒传（Create），检验块库是否仍保留该内容
4. 收尾删除本脚本新建的对象，不留垃圾
"""
import sys, os, json, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import api
from hcc import s, i, S
from cidutil import cidv1_raw, cidv1_dagpb

tok = api.ensure_token()
OUT = os.path.join(HERE, "..", "08_persistence_tests", "post_delete_reread_round2.json")
DIR = "b648aca0cc94070f0de2a016b4aded76"
PARENT = "/hca-2026-11-23"

DEL_ROOT = "bafybeiesgdftdz5o2sauh7ohyszoiqqwqcskkxjeleoc75nbctxboxpck4"
DEL_LEAF = "bafkreiccgyrrt57i5i7igzdnhcwdqfpkqw4njn3mhxuvgv6xrn67ikopd4"
LIVE_LEAF = "bafkreibixcbkqexjahswi2lsfv2cgmd5ueyf2j2u3z3n3i7eiofdssr5ba"

rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "events": []}


def log(**kw):
    kw["ts"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    rec["events"].append(kw)
    print(json.dumps(kw, ensure_ascii=False)[:500])


def fetch_block(label, cid):
    t0 = time.time()
    r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                    s(1, cid) + i(2, 1) + i(5, 1), token=tok)
    if not sd or 1 not in sd:
        log(event="slice_addr", label=label, cid=cid, grpc=r["status"], msg=r["message"], addr_ok=False)
        return None
    a = sd[1]
    url, key = a.get(2), a.get(4) or 0
    resp = S.get(url, timeout=120)
    body = bytes(b ^ key for b in resp.content) if resp.status_code == 200 else b""
    ok = bool(body) and (cidv1_raw(body) == cid or cidv1_dagpb(body) == cid)
    h = resp.headers
    log(event="block_fetch", label=label, cid=cid, http=resp.status_code, bytes=len(resp.content),
        cid_ok=ok, ms=int((time.time() - t0) * 1000), key=key,
        via=h.get("via"), age=h.get("age"), x_response_cache=h.get("x-response-cache"),
        x_bdcdn=h.get("x-bdcdn-cache-status"), etag=h.get("etag"),
        url_host=url.split("/")[2], auth_key_uniq=url.split("auth_key=")[1][:20] if "auth_key=" in url else None)
    return ok


# 1) 目录现状
r, body = api.rpc("v6.services.pub.PubUserFile", "List", s(1, s(1, DIR)) + s(3, i(2, 100)), token=tok)
files = body.get(1, []) if body else []
if isinstance(files, dict):
    files = [files]
log(event="list", n=len(files), names=[f.get(4) for f in files])

# 2) 已删除对象：根 + 叶子，各取两次（第二次换新 auth_key）
fetch_block("deleted_root_1", DEL_ROOT)
time.sleep(2)
fetch_block("deleted_root_2", DEL_ROOT)
fetch_block("deleted_leaf_1", DEL_LEAF)
time.sleep(2)
fetch_block("deleted_leaf_2", DEL_LEAF)
# 3) 对照：未删除大文件叶子
fetch_block("live_leaf_control", LIVE_LEAF)

# 4) 秒传检验：已删除内容是否能再次“凭空”创建（块库是否仍保留）
meta = json.load(open(os.path.join(HERE, "..", "03_upload", "upload_hca-mb10.bin.json")))
ident = meta["content_identity"]
name = "hca-revive-probe.bin"
req = s(3, DIR) + s(4, name) + s(6, "application/octet-stream") + i(7, 10485760) + s(24, ident)
t0 = time.time()
r, body = api.rpc("v6.services.pub.PubUserFile", "Create", req, token=tok)
log(event="create_from_deleted_identity", http=r["http"], grpc=r["status"], msg=r["message"],
    ms=int((time.time() - t0) * 1000), resp=body)
revive_identity = (body or {}).get(1) if isinstance(body, dict) else None

# 5) 收尾删除
if revive_identity:
    r2, b2 = api.rpc("v6.services.pub.PubUserFile", "Delete", s(1, s(1, revive_identity)), token=tok)
    log(event="cleanup_delete", grpc=r2["status"], msg=r2["message"])

json.dump(rec, open(OUT, "w"), indent=1, ensure_ascii=False)
print("saved ->", OUT)
