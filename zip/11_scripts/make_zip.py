import os, json, zipfile, hashlib
ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BASE = os.path.dirname(ROOT)
ZIP = os.path.join(BASE, "halalcloud-current-audit.zip")
n = 0
with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
    for dp, dn, fn in os.walk(ROOT):
        for f in sorted(fn):
            full = os.path.join(dp, f)
            rel = os.path.relpath(full, BASE).replace(os.sep, "/")
            if rel.endswith(".zip"):
                continue
            z.write(full, rel)
            n += 1
h = hashlib.sha256()
with open(ZIP, "rb") as f:
    for b in iter(lambda: f.read(1 << 20), b""):
        h.update(b)
print(json.dumps({"zip": ZIP, "entries": n, "bytes": os.path.getsize(ZIP),
                  "sha256": h.hexdigest()}, indent=1))
