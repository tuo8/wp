"""第二轮：CDN 分层采样（判断 picNN 是父层缓存还是源站）+ 长窗口持久化监测。
用法：python t31_layer_sample.py [--monitor]
"""
import sys, os, json, time
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api
from hcc import s, i, S
from cidutil import cidv1_raw, cidv1_dagpb

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_S = os.path.join(HERE, "..", "07_storage_backend", "cdn_layer_samples.json")
OUT_M = os.path.join(HERE, "..", "08_persistence_tests", "long_window_monitor.json")
tok = api.ensure_token()

GB_LEAFS = None
def leafs():
    global GB_LEAFS
    if GB_LEAFS:
        return GB_LEAFS
    pb = json.load(open(os.path.join(HERE, "..", "05_cid_and_chunks", "gb_per_block.json")))
    GB_LEAFS = [x["cid"] for x in pb]
    return GB_LEAFS

def addr(cid):
    r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                    s(1, cid) + i(2, 1) + i(5, 1), token=tok)
    a = sd.get(1) if sd else None
    if isinstance(a, list):
        a = a[0] if a else None
    return a

def fetch(cid):
    a = addr(cid)
    if not a or not a.get(2):
        return {"cid": cid, "error": "no_addr"}
    key = a.get(4) or 0
    t0 = time.time()
    rr = S.get(a[2], timeout=90)
    dec = bytes(b ^ key for b in rr.content)
    h = {k.lower(): v for k, v in rr.headers.items()}
    ok = cidv1_raw(dec) == cid or cidv1_dagpb(dec) == cid
    return {"cid": cid, "http": rr.status_code, "bytes": len(dec), "cid_ok": ok,
            "ms": round((time.time() - t0) * 1000), "age": h.get("age"),
            "via": h.get("via"), "x_response_cache": h.get("x-response-cache"),
            "x_bdcdn": h.get("x-bdcdn-cache-status"), "ts": time.strftime("%Y-%m-%dT%H:%M:%S")}

def sample_layers(n=24):
    rows = []
    L = leafs()
    step = max(1, len(L) // n)
    for k in range(n):
        cid = L[(k * step) % len(L)]
        try:
            rows.append(fetch(cid))
        except Exception as e:
            rows.append({"cid": cid, "error": type(e).__name__ + ": " + str(e)[:80]})
        time.sleep(0.4)
    agg = {}
    for r in rows:
        k = (r.get("x_response_cache"), len((r.get("via") or "").split(",")))
        agg[str(k)] = agg.get(str(k), 0) + 1
    summary = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "n": len(rows),
               "cache_state_x_hops": agg,
               "distinct_via_tail": sorted({(r.get("via") or "").split(",")[-1] for r in rows if r.get("via")}),
               "distinct_via_head": sorted({(r.get("via") or "").split(",")[0] for r in rows if r.get("via")}),
               "gb_leaf_avg_ms": round(sum(r["ms"] for r in rows if "ms" in r) / max(1, len([r for r in rows if "ms" in r]))),
               "all_cid_ok": all(r.get("cid_ok") for r in rows if "cid_ok" in r)}
    json.dump({"summary": summary, "rows": rows}, open(OUT_S, "w"), indent=1, ensure_ascii=False)
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return summary

def monitor(rounds=6, gap=480):
    DEL_ROOT = "bafybeiesgdftdz5o2sauh7ohyszoiqqwqcskkxjeleoc75nbctxboxpck4"
    DEL_LEAF = "bafkreiccgyrrt57i5i7igzdnhcwdqfpkqw4njn3mhxuvgv6xrn67ikopd4"
    LIVE = leafs()[0]
    hist = json.load(open(OUT_M)) if os.path.exists(OUT_M) else {"start": time.strftime("%Y-%m-%dT%H:%M:%S"), "samples": []}
    for k in range(rounds):
        row = {"t": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "deleted_root": fetch(DEL_ROOT),
               "deleted_leaf": fetch(DEL_LEAF),
               "live_leaf": fetch(LIVE)}
        hist["samples"].append(row)
        json.dump(hist, open(OUT_M, "w"), indent=1, ensure_ascii=False)
        print("sample", k, row["t"], "root_ok", row["deleted_root"].get("cid_ok"),
              "leaf_ok", row["deleted_leaf"].get("cid_ok"),
              "cache", row["deleted_root"].get("x_response_cache"), flush=True)
        if k < rounds - 1:
            time.sleep(gap)

if __name__ == "__main__":
    if "--monitor" in sys.argv:
        monitor()
    else:
        sample_layers()
