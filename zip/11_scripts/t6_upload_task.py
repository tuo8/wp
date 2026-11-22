import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i, dec

tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"   # hca-2026-11-23
NAME = "hca-kb1.bin"
SIZE = 1024
req = s(3, DIR) + s(4, NAME) + s(6, "application/octet-stream") + i(7, SIZE)
r, body = api.rpc("v6.services.pub.PubUserFile", "CreateUploadTask", req, token=tok)
print("HTTP", r["http"], "grpc", r["status"], r["message"])
print(json.dumps(body, ensure_ascii=False, indent=1)[:2000])
# 原始字段落盘到 secrets（含凭据，不交付）
if r["frames"]:
    raw = r["frames"][0][1]
    open(os.path.join("..", "..", "capture", "secrets", "upload_task_kb1.bin"), "wb").write(raw)
