#!/usr/bin/env python3
"""账户/配额/DAV/预览等只读接口采样（脱敏写入 evidence）。"""
import os
import sys
import json

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hc  # noqa: E402
from hc import pb_string, pb_int, pb_bytes  # noqa: E402

EV = os.path.join(os.path.dirname(os.path.abspath(__file__)), "evidence")
ROOT = hc.USER_ID
PNG_ID = "[REDACTED_ID]"
out = {}


def call(svc, m, payload=b"", timeout=30):
    r = hc.authed(svc, m, payload, timeout=timeout)
    return {"grpc_status": r.get("grpc_status"), "grpc_message": r.get("grpc_message"),
            "pb": r.get("pb")}


out["user_get"] = call("v6.services.pub.PubUser", "Get", pb_string(1, ROOT))
out["quota"] = call("v6.services.pub.PubUser", "GetStatisticsAndQuota", pb_string(1, ROOT))
out["user_settings"] = call("v6.services.pub.PubUser", "GetSettings")
out["dav_server_info"] = call("v6.services.pub.PubDavConfig", "GetServerInfo")
out["dav_config"] = call("v6.services.pub.PubDavConfig", "Get")
out["sftp_config"] = call("v6.services.pub.PubSftpConfig", "Get")
out["share_list"] = call("v6.services.pub.PubFileShare", "List", pb_int(1, 10))
out["preview_png"] = call("v6.services.pub.PubUserFile", "GetDownloadAndPreviewInfo",
                          pb_string(1, PNG_ID))
out["offline_list"] = call("v6.services.pub.PubOfflineTask", "List", pb_int(1, 10))

print(json.dumps(out, ensure_ascii=False, indent=2)[:4000])
json.dump(out, open(os.path.join(EV, "account_readonly.json"), "w"), ensure_ascii=False, indent=2)
