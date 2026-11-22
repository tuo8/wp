"""只读复核：20 MiB 文件 5 片各自 XOR 密钥解码后的哈希一致性。
不写账号数据，只调用 GetFileSlicesDownloadInfo + GET 分片。"""
import sys, json, time, hashlib
sys.path.insert(0, '.')
import hc
from hc import pb_bytes, pb_string, pb_int, http

IDENT = "[REDACTED_ID]"
LOCAL = "testfiles/hc-test-20m.bin"
CHUNK = 4194304

def slices_info(ident):
    req = pb_bytes(1, pb_string(1, ident))
    return hc.authed("v6.services.pub.PubUserFile", "GetFileSlicesDownloadInfo", req)

r = slices_info(IDENT)
sl = (r.get('pb') or {}).get(1) or []
if isinstance(sl, dict): sl = [sl]
plain = open(LOCAL, 'rb').read()
buf = b''; rows = []
for i, s in enumerate(sl):
    url = s.get(2); key = s.get(4)
    t0 = time.time()
    st = http("GET", url, timeout=60)
    body = st['body']
    key = int(key) if isinstance(key, (int, str)) and str(key).isdigit() else 0
    dec = body.translate(bytes(b ^ key for b in range(256))) if 0 < key < 256 else body
    seg = plain[i*CHUNK:(i+1)*CHUNK]
    rows.append({
        "index": i,
        "cid": s.get(1),
        "xor_key": key,
        "http_status": st['status'],
        "wire_bytes": len(body),
        "decoded_sha256": hashlib.sha256(dec).hexdigest(),
        "expected_sha256": hashlib.sha256(seg).hexdigest(),
        "match": hashlib.sha256(dec).hexdigest() == hashlib.sha256(seg).hexdigest(),
        "seconds": round(time.time()-t0, 3),
        "content_type": st['headers'].get('content-type'),
        "server": st['headers'].get('server'),
        "age": st['headers'].get('age'),
        "bdcdn": st['headers'].get('x-bdcdn-cache-status'),
    })
    buf += dec

whole = hashlib.sha256(buf).hexdigest()
out = {
    "file": "hc-audit-20m-202611222050.bin",
    "identity": IDENT,
    "verified_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    "note": "只读复核（无上传、无删除）。每片使用该片响应里的 encrypt 单独解码，纠正 downloads.json 中单键套用 5 片的脚本缺陷。",
    "chunk_count": len(rows),
    "slices": rows,
    "assembed_sha256": whole,
    "local_sha256": hashlib.sha256(plain).hexdigest(),
    "whole_match": whole == hashlib.sha256(plain).hexdigest(),
    "all_slices_match": all(x['match'] for x in rows),
}
json.dump(out, open('evidence/verify_20m_per_slice.json', 'w'), ensure_ascii=False, indent=2)
for x in rows:
    print(f"#{x['index']} key={x['xor_key']:>3} {x['http_status']} {x['wire_bytes']:>8}B match={x['match']} bdcdn={x['bdcdn']} age={x['age']}")
print("whole_match", out['whole_match'], "all_slices", out['all_slices_match'])
