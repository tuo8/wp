import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc, api
from hcc import s, i
tok = api.ensure_token()
# FileListRequest: parent(1)=File{identity(1)=""}, list_info(3)={limit(2)=10, version(4)=1}
parent = b""
listinfo = i(2, 10) + i(4, 1)
req = s(1, parent) + s(3, listinfo)
r, body = api.rpc("v6.services.pub.PubUserFile", "List", req, token=tok)
print("HTTP", r["http"], "grpc", r["status"], r["message"])
print(json.dumps(body, ensure_ascii=False)[:1200])
