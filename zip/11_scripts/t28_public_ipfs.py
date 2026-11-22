import sys, os, json, requests
HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "07_storage_backend", "public_ipfs_probe.json")
ours = json.load(open(os.path.join(HERE, "..", "03_upload", "upload_hca-mb10.bin.json")))["content_identity"]
leaf = json.load(open(os.path.join(HERE, "..", "03_upload", "upload_hca-mb10.bin.json")))["block_cids"][0]
gb = "bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4"
control = "bafybeigdyrzt5sfp7udm7hu76uh7y26nf3efuylqabf3oclgtqy55fbzdi"
s = requests.Session(); s.trust_env = False
res = {}
targets = [("dht_provider_ours", f"https://delegated-ipfs.dev/routing/v1/providers/{ours}"),
           ("dht_provider_ours_gb", f"https://delegated-ipfs.dev/routing/v1/providers/{gb}"),
           ("dht_provider_ours_leaf", f"https://delegated-ipfs.dev/routing/v1/providers/{leaf}"),
           ("dht_provider_control", f"https://delegated-ipfs.dev/routing/v1/providers/{control}"),
           ("public_gw_ours", f"https://ipfs.io/ipfs/{ours}"),
           ("public_gw_control", f"https://ipfs.io/ipfs/{control}"),
           ("dweb_ours", f"https://{ours}.ipfs.dweb.link/")]
for label, url in targets:
    try:
        r = s.get(url, timeout=25, headers={"User-Agent": "Mozilla/5.0"})
        body = r.text[:200]
        n = body.count('"Provider"') or body.count("Provider") if r.status_code == 200 else 0
        res[label] = {"http": r.status_code, "len": len(r.content), "providers_seen": n,
                      "head": body if r.status_code != 200 else "<binary/ok>"}
    except Exception as e:
        res[label] = {"error": type(e).__name__ + ": " + str(e)[:100]}
    print(label, "->", json.dumps(res[label], ensure_ascii=False)[:200])
json.dump({"ts": __import__("time").strftime("%Y-%m-%dT%H:%M:%S"), "ours_root": ours, "ours_leaf": leaf,
           "ours_gb": gb, "control": control, "results": res}, open(OUT, "w"), indent=2)
