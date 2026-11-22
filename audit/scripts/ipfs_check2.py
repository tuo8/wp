#!/usr/bin/env python3
"""用 curl（带常规 UA，绕开 Cloudflare 对 urllib UA 的 1010 拦截）复核网关与 provider。"""
import os
import sys
import json
import time
import hashlib
import subprocess
import tempfile

EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0 Safari/537.36"

GATEWAYS = ["https://ipfs.io/ipfs/", "https://dweb.link/ipfs/",
            "https://gateway.pinata.cloud/ipfs/", "https://4everland.io/ipfs/",
            "https://trustless-gateway.link/ipfs/", "https://ipfs-gateway.qzy.xyz/ipfs/"]

CIDS = {
    "small_txt_raw": "bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey",
    "png_raw": "bafkreifgyqrjh4e3bnqtps73kkf2fye4277fx5dghgbyiw3idfsay4f7gm",
    "20m_root_dagpb": "bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4",
}


def curl_get(url, timeout=25, out_file=None):
    cmd = ["curl", "-sS", "-L", "--max-time", str(timeout), "-A", UA,
           "-w", "\n__META__%{http_code} %{size_download} %{time_total}\\n", url]
    if out_file:
        cmd[1:1] = ["-o", out_file]
    p = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace")
    body = p.stdout or ""
    meta = {"status": None, "bytes": 0, "seconds": None}
    if "__META__" in body:
        head, tail = body.rsplit("__META__", 1)
        parts = tail.strip().split()
        if len(parts) >= 3:
            meta = {"status": int(parts[0]), "bytes": int(float(parts[1])),
                    "seconds": float(parts[2])}
        body = head
    return {"meta": meta, "body": body, "stderr": (p.stderr or "").strip()}


def main():
    gw_out = []
    for name, cid in CIDS.items():
        for gw in GATEWAYS:
            url = gw + cid
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".bin")
            tmp.close()
            r = curl_get(url, 25, tmp.name)
            data = open(tmp.name, "rb").read()
            os.unlink(tmp.name)
            rec = {"cid": name, "gateway": gw, "status": r["meta"]["status"],
                   "bytes": r["meta"]["bytes"], "seconds": r["meta"]["seconds"],
                   "sha256": hashlib.sha256(data).hexdigest() if data else None,
                   "body_head": data[:60].decode("utf-8", "replace") if data else "",
                   "stderr": r["stderr"][:120]}
            gw_out.append(rec)
            print(f"{name:15s} {gw:42s} {rec['status']} {rec['bytes']:>8}B "
                  f"{str(rec['sha256'])[:12]} {rec['stderr'][:60]}", flush=True)
            time.sleep(0.4)
    json.dump(gw_out, open(os.path.join(EV, "gateway_probe_curl.json"), "w"),
              ensure_ascii=False, indent=2)

    pv_out = []
    for name, cid in CIDS.items():
        url = f"https://delegated-ipfs.dev/routing/v1/providers/{cid}"
        r = curl_get(url, 40)
        body = r["body"]
        try:
            j = json.loads(body)
            provs = j.get("Providers") or []
        except Exception:
            provs = []
        rec = {"cid": name, "status": r["meta"]["status"], "provider_count": len(provs),
               "providers": [{"PeerID": (p.get("ID") or p.get("PeerID") or "")[:16] + "…",
                              "Addrs": (p.get("Addrs") or [])[:2]} for p in provs[:5]],
               "body_head": body[:120]}
        pv_out.append(rec)
        print(f"providers {name:15s} status={rec['status']} count={rec['provider_count']} "
              f"body={body[:60]}", flush=True)
        time.sleep(0.6)
    json.dump(pv_out, open(os.path.join(EV, "providers_curl.json"), "w"),
              ensure_ascii=False, indent=2)


if __name__ == "__main__":
    main()
