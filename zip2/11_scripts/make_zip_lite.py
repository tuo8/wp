"""生成精简交付包（lite）：剔除可由脚本重建的大体积中间产物，保留全部证据与哈希。

被剔除的文件是"随机测试数据"与"下载副本"，它们的价值只在于 SHA-256 与字节数，
这两项已经被写成独立清单 EXCLUDED_BINARIES.sha256，完整性可校验，且可用脚本重新生成。
"""
import os, json, hashlib, zipfile

ROOT = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
BASE = os.path.dirname(ROOT)
OUT_ZIP = os.path.join(BASE, "halalcloud-current-audit-lite.zip")
EXCL_TXT = os.path.join(ROOT, "12_reports", "EXCLUDED_BINARIES.sha256")

# 剔除规则：纯随机数据 / 下载副本 / 采样字节块（见 excluded()）


def excluded(rel):
    r = rel.replace(os.sep, "/")
    if r.startswith("03_upload/testcorpus/") and r.endswith(".bin"):
        return True
    if r.startswith("04_download/app_downloaded_") and r.endswith(".bin"):
        return True
    if r.startswith("05_cid_and_chunks/gb_") and r.endswith("_sample.bin"):
        return True
    return False


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


keep, drop = [], []
for dp, dn, fn in os.walk(ROOT):
    for f in sorted(fn):
        full = os.path.join(dp, f)
        rel = os.path.relpath(full, ROOT)
        if f.endswith(".zip"):
            continue
        (drop if excluded(rel) else keep).append((rel, full))

# 1) 被剔除文件清单
lines = ["# 被剔除的大体积中间产物（不在 lite 包内）",
         "# 格式: sha256  bytes  path",
         "# 校验: sha256sum -c EXCLUDED_BINARIES.sha256  （需先把同名文件放回原路径）",
         ""]
drop_bytes = 0
for rel, full in sorted(drop):
    sz = os.path.getsize(full)
    drop_bytes += sz
    lines.append(f"{sha256(full)}  {sz}  {rel.replace(os.sep, '/')}")
open(EXCL_TXT, "w", encoding="utf-8", newline="\n").write("\n".join(lines) + "\n")

# 2) 打包
entries = 0
with zipfile.ZipFile(OUT_ZIP, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
    for rel, full in keep:
        z.write(full, os.path.join("halalcloud-current-audit", rel).replace(os.sep, "/"))
        entries += 1

h = sha256(OUT_ZIP)
keep_bytes = sum(os.path.getsize(f) for _, f in keep)
print(json.dumps({
    "lite_zip": OUT_ZIP,
    "entries": entries,
    "zip_bytes": os.path.getsize(OUT_ZIP),
    "uncompressed_kept_bytes": keep_bytes,
    "dropped_files": len(drop),
    "dropped_bytes": drop_bytes,
    "zip_sha256": h,
}, indent=1, ensure_ascii=False))
