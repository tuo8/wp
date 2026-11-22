import sys, os, json, requests
cid = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))["content_identity"]
child = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))["block_cids"][0]
pos = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"  # 公共已知 CID 作阳性对照
s = requests.Session(); s.trust_env = False
res = {}
for label, url in [
    ("dht_provider_ours", f"https://delegated-ipfs.dev/routing/v1/providers/{cid}"),
    ("dht_provider_control", f"https://delegated-ipfs.dev/routing/v1/providers/{pos}"),
    ("public_gw_ours", f"https://ipfs.io/ipfs/{cid}"),
    ("public_gw_control", f"https://ipfs.io/ipfs/{pos}"),
    ("dweb_ours", f"https://{cid}.ipfs.dweb.link/"),
]:
    try:
        r = s.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        res[label] = {"http": r.status_code, "len": len(r.content), "head": r.text[:160] if r.status_code != 200 else "<binary ok>"}
    except Exception as e:
        res[label] = {"error": str(e)[:120]}
    print(label, "->", json.dumps(res[label], ensure_ascii=False)[:220])
json.dump({"cid": cid, "child": child, "results": res},
          open(os.path.join(os.path.dirname(__file__), "07_storage_backend", "public_ipfs_probe.json"), "w"), indent=2)
