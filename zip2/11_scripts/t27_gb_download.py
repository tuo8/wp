"""3.15 GiB 全量下载：807 块逐块校验 + 并发标定 + CDN 节点分布 + 整文件 SHA-256。
不落盘整文件（3.15 GiB），仅保留首/尾 2 MiB 样本与逐块元数据。"""
import sys, os, json, time, hashlib, threading, queue
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api, hcc
from hcc import s, i, S, dec
from dagbuild import b32
from cidutil import sha256hex, cidv1_raw

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "05_cid_and_chunks", "gb_download.json")
SAMP = os.path.join(HERE, "..", "05_cid_and_chunks")
tok = api.ensure_token()
CID = "bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4"
SIZE = 3381078505
rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "target": {"cid": CID, "size": SIZE, "name": "CAWD-987-AI.mp4"}}
t_start = time.time()

def save():
    json.dump(rec, open(OUT, "w"), indent=2, ensure_ascii=False)

def slice_addr(cid, count=1, retry=3):
    last = None
    for k in range(retry):
        try:
            r, sd = api.rpc("v6.services.pub.PubUserFile", "GetSliceDownloadAddress",
                            s(1, cid) + i(2, 1) + i(5, count), token=tok)
            return r, sd
        except Exception as e:
            last = e; time.sleep(0.5 * (k + 1))
    raise last

def get_block(cid, retry=4):
    """返回 (data, meta)"""
    t0 = time.time()
    for k in range(retry):
        try:
            r, sd = slice_addr(cid)
            info = sd[1]
            key = info[4] or 0
            t1 = time.time()
            rr = S.get(info[2], timeout=90)
            raw = rr.content
            data = bytes(b ^ key for b in raw) if key else raw
            return data, {"cid": cid, "http": rr.status_code, "raw": len(raw), "dec": len(data),
                          "encrypt": key, "addr_ms": round((t1 - t0) * 1000),
                          "fetch_ms": round((time.time() - t1) * 1000),
                          "via": rr.headers.get("via"), "age": rr.headers.get("age"),
                          "etag": rr.headers.get("etag"), "server": rr.headers.get("server"),
                          "url_host": info[2].split("?")[0].split("/")[2], "attempts": k + 1}
        except Exception as e:
            time.sleep(1.0 * (k + 1))
            err = repr(e)
    raise RuntimeError(err)

# ---------- 0. 分片地址批量能力 ----------
r, sd = slice_addr(CID, 1)
root_info = sd[1]
key = root_info[4] or 0
root_data = bytes(b ^ key for b in S.get(root_info[2], timeout=60).content) if key else S.get(root_info[2], timeout=60).content
node = dec(root_data); links = node.get(2, [])
if isinstance(links, bytes): links = [links]
leaf_cids = [b32(dec(lk)[1][0]) for lk in links]
blocksizes = dec(node[1][0])[4]
rec["dag"] = {"root_cid": CID, "leaf_count": len(leaf_cids), "blocksizes": len(blocksizes),
              "sum_blocksizes": sum(blocksizes), "max_block": max(blocksizes), "min_block": min(blocksizes),
              "cid_recompute_match": b32(b"\x01\x70\x12\x20" + hashlib.sha256(root_data).digest()) == CID}
print("leaves:", len(leaf_cids), "sum:", sum(blocksizes))
save()

# 批量地址：一次请求 8 个看返回条数
try:
    r, sd = slice_addr(leaf_cids[0], 8)
    n_addr = len(sd.get(1, {})) if isinstance(sd.get(1), dict) else len(sd[1])
    rec["slice_addr_batch_test"] = {"requested": 8, "returned": n_addr}
except Exception as e:
    rec["slice_addr_batch_test"] = {"error": repr(e)}
print("batch addr:", rec["slice_addr_batch_test"])
save()

# ---------- 1. 并发标定（前 32 块） ----------
def bench(cids, conc):
    q = queue.Queue(); out = []
    for c in cids: q.put(c)
    lock = threading.Lock()
    def w():
        while True:
            try: c = q.get_nowait()
            except queue.Empty: return
            try:
                d, m = get_block(c)
                with lock: out.append((m, len(d)))
            except Exception as e:
                with lock: out.append(({"error": repr(e), "cid": c}, 0))
    t0 = time.time()
    ts = [threading.Thread(target=w) for _ in range(conc)]
    [t.start() for t in ts]; [t.join() for t in ts]
    dt = time.time() - t0
    b = sum(x[1] for x in out)
    lat = sorted(m["fetch_ms"] for m, _ in out if "fetch_ms" in m)
    return {"conc": conc, "blocks": len(out), "bytes": b, "seconds": round(dt, 2),
            "mbps": round(b / dt / 1e6, 2), "p50_fetch_ms": lat[len(lat)//2] if lat else None,
            "max_fetch_ms": lat[-1] if lat else None}

rec["concurrency_bench"] = []
for conc in (1, 4, 8, 16):
    b = bench(leaf_cids[:32], conc)
    rec["concurrency_bench"].append(b)
    print("bench", b)
    save()

# ---------- 2. 全量下载（并发 4，与客户端下载线程数一致） ----------
CONC = 4
results = {}
lock = threading.Lock()
idx_q = queue.Queue()
for n in range(len(leaf_cids)): idx_q.put(n)
done = [0]
t_full = time.time()
h = hashlib.sha256()
# 顺序落哈希需按序：分块取回后缓存到临时文件（按索引偏移写）
tmp = os.path.join(SAMP, "_gb_blocks.tmp")
f = open(tmp, "wb")
f.truncate(SIZE)
head = bytearray(); tail = bytearray()

def worker():
    while True:
        try: n = idx_q.get_nowait()
        except queue.Empty: return
        for attempt in range(3):
            try:
                d, m = get_block(leaf_cids[n])
                ok = cidv1_raw(d) == leaf_cids[n] and len(d) == blocksizes[n]
                with lock:
                    if not ok: raise ValueError("cid/size mismatch at %d" % n)
                    f.seek(sum(blocksizes[:n]) if n == 0 else offsets[n])
                    f.write(d)
                    if n < 1: head.extend(d)
                    if n >= len(leaf_cids) - 2: tail.extend(d)
                    results[n] = m
                    done[0] += 1
                    if done[0] % 50 == 0:
                        el = time.time() - t_full
                        print(f"  {done[0]}/{len(leaf_cids)} {el:.0f}s {done[0]*4/el:.1f} MiB/s")
                        save()
                break
            except Exception as e:
                m = {"cid": leaf_cids[n], "error": repr(e)}
                with lock: results[n] = m
                time.sleep(1 + attempt)

offsets = [0]
for bs in blocksizes[:-1]: offsets.append(offsets[-1] + bs)

ts = [threading.Thread(target=worker) for _ in range(CONC)]
[t.start() for t in ts]; [t.join() for t in ts]
f.flush(); f.close()
t_data = time.time() - t_full

# 整文件哈希（顺序读取临时文件）
t_h = time.time()
with open(tmp, "rb") as fh:
    while True:
        chunk = fh.read(8 << 20)
        if not chunk: break
        h.update(chunk)
hash_ms = round((time.time() - t_h) * 1000)

vias = {}
errs = [r for r in results.values() if "error" in r]
for m in results.values():
    if "via" in m:
        for v in (m["via"] or "").split(","):
            vias[v] = vias.get(v, 0) + 1
lat = sorted(m["fetch_ms"] for m in results.values() if "fetch_ms" in m)
addr = sorted(m["addr_ms"] for m in results.values() if "addr_ms" in m)
ages = [m["age"] for m in results.values() if m.get("age")]

rec["full_download"] = {
    "concurrency": CONC, "blocks_ok": len(results) - len(errs), "blocks_err": len(errs),
    "errors": errs[:5],
    "data_seconds": round(t_data, 2), "mbps": round(SIZE / t_data / 1e6, 2),
    "total_seconds": round(time.time() - t_start, 2),
    "sha256": h.hexdigest(), "bytes_hashed": SIZE,
    "hash_seconds": hash_ms / 1000,
    "fetch_ms_p50": lat[len(lat)//2], "fetch_ms_p95": lat[int(len(lat)*0.95)], "fetch_ms_max": lat[-1],
    "addr_ms_p50": addr[len(addr)//2], "addr_ms_p95": addr[int(len(addr)*0.95)],
    "cdn_nodes": dict(sorted(vias.items(), key=lambda x: -x[1])),
    "age_present": len(ages), "age_min": min(ages) if ages else None, "age_max": max(ages) if ages else None,
    "url_hosts": sorted({m["url_host"] for m in results.values() if "url_host" in m}),
    "encrypt_values": sorted({m["encrypt"] for m in results.values() if "encrypt" in m})[:20],
    "encrypt_distinct": len({m["encrypt"] for m in results.values() if "encrypt" in m}),
}
# 首尾样本
open(os.path.join(SAMP, "gb_head_sample.bin"), "wb").write(bytes(head))
open(os.path.join(SAMP, "gb_tail_sample.bin"), "wb").write(bytes(tail))
rec["sample_prefix_sha256"] = sha256hex(bytes(head))
rec["sample_suffix_sha256"] = sha256hex(bytes(tail))
per_block = [{"n": n, "cid": results[n].get("cid"), "via": results[n].get("via"),
              "age": results[n].get("age"), "fetch_ms": results[n].get("fetch_ms"),
              "addr_ms": results[n].get("addr_ms"), "encrypt": results[n].get("encrypt")}
             for n in sorted(results)]
json.dump(per_block, open(os.path.join(SAMP, "gb_per_block.json"), "w"), indent=1)
os.remove(tmp)
save()
print("DONE", json.dumps(rec["full_download"], ensure_ascii=False)[:600])
