#!/usr/bin/env python3
"""DHT provider 阳性对照 + 公共网关探测结果归因。

目的：把「查询接口本身可用」与「目标 CID 无 Provider」分开；并把网关的失败
归类为「网络/中间层拦截」而不是「内容不存在」。只读外部请求，不碰账号。

用法： python ipfs_disposition.py
"""
import json
import subprocess
import time

OURS = {
    "small_txt_raw": "bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey",
    "png_raw": "bafkreifgyqrjh4e3bnqtps73kkf2fye4277fx5dghgbyiw3idfsay4f7gm",
    "20m_root_dagpb": "bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4",
}
# 公开已知、长期被多处 pin 的 CID（IPFS 文档里的 hello world），用作阳性对照
CONTROL = {"hello_world_docs": "bafkreiem4twkqzsq2aj4shbycd4yvoj2cx72vezicletlhi7dijjciqpui"}


def curl(url, timeout=25):
    """返回原始字节，避免 Windows 控制台编码干扰。"""
    t0 = time.time()
    p = subprocess.run(["curl", "-sS", "-m", str(timeout), "--noproxy", "*",
                        "-o", "-", "-w", "%{http_code}", url],
                       capture_output=True)
    body = p.stdout or b""
    raw = (p.stderr or b"").decode("utf-8", "replace")
    code = 0
    if len(body) >= 3 and body[-3:].isdigit():
        code = int(body[-3:])
        body = body[:-3]
    else:
        tail = body[-4:]
        if tail.strip().isdigit():
            code = int(tail.strip())
            body = body[:-len(tail)]
    return {"url": url, "http": code, "bytes": len(body),
            "seconds": round(time.time() - t0, 2),
            "body": body, "stderr": raw.strip()[:200]}


def providers(cid):
    r = curl(f"https://delegated-ipfs.dev/routing/v1/providers/{cid}")
    try:
        j = json.loads(r["body"].decode("utf-8", "replace"))
        n = len(j.get("Providers") or [])
    except Exception:
        n = None
    return {"cid": cid, "http": r["http"], "provider_count": n,
            "body_head": r["body"][:200].decode("utf-8", "replace")}


def main():
    pc = []
    for label, cid in list(OURS.items()) + list(CONTROL.items()):
        row = providers(cid)
        row["label"] = label
        row["is_control"] = label in CONTROL
        pc.append(row)
        print(f"provider {label:20s} {cid[:16]}… -> {row['provider_count']}")

    gw = []
    targets = [("hello_world_docs", CONTROL["hello_world_docs"]),
               ("small_txt_raw", OURS["small_txt_raw"])]
    for label, cid in targets:
        for base in ["https://ipfs.io/ipfs/", "https://dweb.link/ipfs/"]:
            r = curl(base + cid)
            err = r["stderr"]
            if r["http"] == 429:
                verdict = "gateway_rate_limited"          # 网关限流/迁移提示，与内容无关
            elif "schannel" in err or "CRYPT_E" in err:
                verdict = "local_tls_failure"             # 本机证书吊销链不可达
            elif r["http"] == 0:
                verdict = "network_blocked_or_timeout"
            elif r["http"] == 200:
                verdict = "content_returned"
            else:
                verdict = f"http_{r['http']}"
            gw.append({
                "label": label, "is_control": label == "hello_world_docs",
                "gateway": base, "cid": cid, "http": r["http"], "bytes": r["bytes"],
                "seconds": r["seconds"], "verdict": verdict,
                "body_head": r["body"][:120].decode("utf-8", "replace"),
                "stderr": err,
            })
            print(f"gateway  {label:20s} {base:26s} http={r['http']:>4} -> {verdict}")

    json.dump(pc, open("evidence/providers_positive_control.json", "w"),
              ensure_ascii=False, indent=2)
    json.dump(gw, open("evidence/gateway_probe_disposition.json", "w"),
              ensure_ascii=False, indent=2)
    print("written: providers_positive_control.json, gateway_probe_disposition.json")


if __name__ == "__main__":
    main()
