"""生成 MANIFEST.json：遍历交付目录，现场计算每个文件的 SHA-256 / 大小 / 分类。
禁止手填哈希；本脚本是清单的唯一来源。"""
import os, sys, json, hashlib, time, platform, subprocess

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
SKIP = {"MANIFEST.json", "build_manifest.py"}
SKIP_EXT = {".zip", ".tmp"}

CATEGORY = {
    "00_scope": "scope",
    "01_app_identity": "evidence/app-identity",
    "02_network_capture": "evidence/network",
    "03_upload": "evidence/upload",
    "04_download": "evidence/download",
    "05_cid_and_chunks": "evidence/cid-dag",
    "06_protocol_analysis": "evidence/protocol",
    "07_storage_backend": "evidence/backend",
    "08_persistence_tests": "evidence/persistence",
    "09_architecture": "deliverable/architecture",
    "10_raw_evidence": "raw/runtime",
    "11_scripts": "tooling",
    "12_reports": "deliverable/report",
    "historical_reference": "isolated/historical",
}

def sha256(path, buf=1 << 20):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(buf)
            if not b:
                break
            h.update(b)
    return h.hexdigest()

def main():
    files = []
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in sorted(dirnames) if d != ".git"]
        for fn in sorted(filenames):
            if fn in SKIP or os.path.splitext(fn)[1].lower() in SKIP_EXT:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, ROOT).replace(os.sep, "/")
            top = rel.split("/")[0]
            st = os.stat(full)
            files.append({
                "path": rel,
                "category": CATEGORY.get(top, "other"),
                "bytes": st.st_size,
                "sha256": sha256(full),
                "mtime": time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(st.st_mtime)),
            })

    def tool(cmd):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
            return (out.stdout or out.stderr).strip().splitlines()[0]
        except Exception as e:
            return "unavailable: " + type(e).__name__

    manifest = {
        "schema": "halalcloud-current-audit/manifest/v1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generator": "11_scripts/build_manifest.py",
        "note": "所有 SHA-256 由本脚本现场计算；文件名/大小/哈希三者互相绑定，禁止手填。",
        "environment": {
            "os": platform.platform(),
            "python": sys.version.split()[0],
            "adb": tool(["D:/an/pj/Android/Sdk/platform-tools/adb.exe", "version"]),
            "frida": tool([sys.executable, "-c", "import frida;print(frida.__version__)"]),
        },
        "excluded": ["MANIFEST.json（本文件）", "*.zip（打包产物）", "*.tmp（临时分块文件）"],
        "file_count": len(files),
        "total_bytes": sum(f["bytes"] for f in files),
        "categories": {},
        "files": files,
    }
    for f in files:
        manifest["categories"].setdefault(f["category"], {"count": 0, "bytes": 0})
        c = manifest["categories"][f["category"]]
        c["count"] += 1
        c["bytes"] += f["bytes"]

    out = os.path.join(ROOT, "MANIFEST.json")
    json.dump(manifest, open(out, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    print("files:", len(files), "bytes:", manifest["total_bytes"])
    for k, v in sorted(manifest["categories"].items()):
        print(f"  {k:28} {v['count']:>4} files  {v['bytes']/1e6:9.2f} MB")
    print("->", out)

if __name__ == "__main__":
    main()
