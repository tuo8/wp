"""第二轮 §3：抓取地址/文件记录的全部 protobuf 字段，定位 store_type / wcs_etag / bucket 等后台字段。

只读调用（List / GetSliceDownloadAddress / GetDirectDownloadAddress / GetFileInfo），
不修改任何服务端状态。
"""
import sys, os, json, time
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import api
from hcc import s, i

tok = api.ensure_token()
OUT = os.path.join(HERE, "..", "07_storage_backend", "address_fields_round2.json")
DIR = "b648aca0cc94070f0de2a016b4aded76"
GB_ROOT = "bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4"
SMALL_LEAF = "bafkreibixcbkqexjahswi2lsfv2cgmd5ueyf2j2u3z3n3i7eiofdssr5ba"

rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "calls": {}}


def dump(label, svc, method, body, note=""):
    r, resp = api.rpc(svc, method, body, token=tok)
    rec["calls"][label] = {"svc": svc, "method": method, "note": note,
                           "http": r["http"], "grpc": r["status"], "msg": r["message"],
                           "frames": len(r["frames"]), "resp": resp}
    print(f"== {label}: http={r['http']} grpc={r['status']} msg={r['message']}")
    print("   ", json.dumps(resp, ensure_ascii=False)[:900] if resp else None)
    return resp


# 1) 大文件根的分片地址（客户端实际调用的形态）
dump("slice_addr_gb_root", "v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
     s(1, GB_ROOT) + i(2, 1) + i(5, 1), "count=1")

# 2) 小文件叶块
dump("slice_addr_leaf", "v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
     s(1, SMALL_LEAF) + i(2, 1) + i(5, 1), "count=1")

# 3) 一次申请 4 个（客户端并发取址的形态）
dump("slice_addr_batch4", "v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
     s(1, GB_ROOT) + i(2, 1) + i(5, 4), "count=4")

# 4) 直连（整文件网关）
dump("direct_addr_gb", "v6.services.pub.PubUserFile", "GetDirectDownloadAddress",
     s(1, GB_ROOT) + i(2, 1) + s(3, "/hca-2026-11-23/x.mp4") + i(9, 7200), "identity=根CID")

# 5) 列表（文件记录全字段）
lst = dump("list_dir", "v6.services.pub.PubUserFile", "List",
           s(1, s(1, DIR)) + s(3, i(2, 50)), "目录列表")

# 6) 单文件信息
if lst and isinstance(lst, dict) and lst.get(1):
    files = lst[1] if isinstance(lst[1], list) else [lst[1]]
    ident = files[0].get(1)
    for m in ("GetFileInfo", "Get", "Stat", "GetFile"):
        r, resp = api.rpc("v6.services.pub.PubUserFile", m, s(1, ident), token=tok)
        rec["calls"]["info_" + m] = {"http": r["http"], "grpc": r["status"], "msg": r["message"], "resp": resp}
        print(f"== info_{m}: grpc={r['status']} msg={r['message']}")
        if r["status"] == 0:
            print("   ", json.dumps(resp, ensure_ascii=False)[:800])
            break

json.dump(rec, open(OUT, "w"), indent=1, ensure_ascii=False)
print("saved ->", OUT)
