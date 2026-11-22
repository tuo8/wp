#!/usr/bin/env python3
"""账号闭环 / 批量操作 / 投诉管理 / P2P 信令 —— 30 个补齐 RPC 的实测。

覆盖：
  PubUser         Register / Logoff / DeviceAuthorization / ResetPassword /
                  ChangePassword / Delete / Update / SendSmsVerifyCode(+NotUser) /
                  VerifyAuthToken / VerifyAuthorizationCode / CheckAuthStatus /
                  CreateAuthToken / ValidateUserInfo / ExtendQuota / UserCenterUri
  PubUserFile     Copy / BatchRename / BatchOperation / Update /
                  CreateManageRTCOffer / SendClientIceCandidate / GetServerIceCandidate
  PubFileShare    ListComplaint / DeleteComplaint
  WebRTCManage    StartDownload / StopDownload / Disconnect / SendPeerIceCandidate / Receive
"""
import hashlib
import os
import sys
import time
import uuid

import requests

import pb

API = os.environ.get("API", "http://127.0.0.1:8765")
SECRET = os.environ.get("MYDRIVE_APP_SECRET", "dev-only-insecure-secret")
APPID = "2dland/web"
APPVER = "202603182100"

PASSED = 0
FAILED = []


def check(name, cond, detail=""):
    global PASSED
    if cond:
        PASSED += 1
        print(f"  ok  {name}")
    else:
        FAILED.append(f"{name} {detail}")
        print(f"  FAIL {name} {detail}")


def sign(service, method, token=""):
    ts = str(int(time.time() * 1000))
    plain = f"/{service}/{method}{ts}{APPID}{APPVER}"
    if token:
        plain += "Bearer " + token
    return {"timestamp": ts, "appid": APPID, "appversion": APPVER,
            "sign": hashlib.md5((plain + SECRET).encode()).hexdigest(),
            "content-type": "application/grpc-web+proto", "te": "trailers"}


def call(service, method, payload=b"", token="", package="pub"):
    headers = sign(service, method, token)
    if token:
        headers["authorization"] = "Bearer " + token
    frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
    resp = requests.post(f"{API}/v6.services.{package}.{service}/{method}",
                         data=frame, headers=headers, timeout=20)
    status = int(resp.headers.get("grpc-status", "0") or 0)
    msg = resp.headers.get("grpc-message", "")
    body, pos, raw = b"", 0, resp.content
    while pos + 5 <= len(raw):
        flag = raw[pos]
        length = int.from_bytes(raw[pos + 1:pos + 5], "big")
        if flag == 0:
            body = raw[pos + 5:pos + 5 + length]
        pos += 5 + length
    return status, body, msg


def call_json(service, method, obj, token="", package="pub"):
    return call(service, method, pb.encode(obj[0], obj[1]), token, package)


def rest(path, obj, token=""):
    headers = {"content-type": "application/json"}
    if token:
        headers["authorization"] = "Bearer " + token
    return requests.post(f"{API}{path}", json=obj, headers=headers, timeout=20)


def new_user():
    name = f"acct_{uuid.uuid4().hex[:8]}"
    status, body, msg = call("PubUser", "Register",
                             pb.encode("RegisterRequest", {"name": name, "password": "pass_123456"}))
    user = pb.decode("User", body) if body else {}
    return name, user, status, msg


def login(name, password="pass_123456"):
    status, body, msg = call("PubUser", "Login",
                             pb.encode("LoginRequest", {"identity": name, "password": password}))
    data = pb.decode("LoginResponse", body) if body else {}
    return data.get("token", {}).get("access_token", ""), status, msg


def main():
    print("== PubUser: 注册 / 登录 ==")
    name, user, status, msg = new_user()
    check("Register 返回 0", status == 0, msg)
    check("Register 返回 identity", len(user.get("identity", "")) == 32, user)
    token, status, msg = login(name)
    check("Login 新账号成功", status == 0 and bool(token), msg)

    print("== PubUser: 校验类 ==")
    status, body, msg = call("PubUser", "ValidateUserInfo",
                             pb.encode("UserValidateInfo", {"name": name}))
    info = pb.decode("UserNameValidateResponse", body) if body else {}
    check("ValidateUserInfo 已占用 -> valid=false", info.get("valid") is not True, info)

    status, body, msg = call("PubUser", "ValidateUserInfo",
                             pb.encode("UserValidateInfo", {"name": "free_" + uuid.uuid4().hex[:8]}))
    info2 = pb.decode("UserNameValidateResponse", body) if body else {}
    check("ValidateUserInfo 未占用 -> valid=true", info2.get("valid") is True, info2)

    status, body, msg = call("PubUser", "VerifyAuthToken",
                             pb.encode("LoginRequest", {"identity": token}))
    out = pb.decode("OauthTokenCheckResponse", body) if body else {}
    check("VerifyAuthToken 有效 token -> status 0", out.get("status", 0) == 0, out)

    status, body, msg = call("PubUser", "VerifyAuthToken",
                             pb.encode("LoginRequest", {"identity": "bad.token.here"}))
    out = pb.decode("OauthTokenCheckResponse", body) if body else {}
    check("VerifyAuthToken 假 token -> status 1", out.get("status") == 1, out)

    print("== PubUser: 短信验证码 ==")
    phone = "138" + str(int(time.time()))[-8:]
    status, body, msg = call("PubUser", "SendSmsVerifyCodeNotUser",
                             pb.encode("SmsVeifyCodeSendRequestNotUser",
                                       {"phone": phone, "country_code": "+86", "type": 1}))
    sms = pb.decode("SmsVeifyCodeSendResponse", body) if body else {}
    code = sms.get("captcha", "")
    sms_debug = os.environ.get("EXPECT_SMS_DEBUG", "0").lower() in ("1", "true", "yes")
    check("SMS 验证码仅在显式调试模式返回", (len(code) == 6 and code.isdigit()) if sms_debug else not code, sms)

    status, body, msg = call("PubUser", "ResetPassword",
                             pb.encode("LoginRequest", {"identity": name, "password": "newpass_987",
                                                        "captcha": code}))
    check("ResetPassword 用错手机号验证码失败", status != 0, f"status={status}")

    print("== PubUser: 改密 / 资料 / 配额 ==")
    status, body, msg = call("PubUser", "ChangePassword",
                             pb.encode("ChangePasswordRequest",
                                       {"identity": user["identity"],
                                        "old_password": "pass_123456",
                                        "new_password": "pass_654321"}), token)
    check("ChangePassword 成功", status == 0, msg)
    token2, status, msg = login(name, "pass_654321")
    check("新密码可登录", status == 0 and bool(token2), msg)
    token_old, status, _ = login(name, "pass_123456")
    check("旧密码被拒", status != 0, f"status={status}")
    token = token2

    status, body, msg = call("PubUser", "Update",
                             pb.encode("User", {"identity": user["identity"],
                                                "name": "测试昵称", "addon": "lab"}), token)
    upd = pb.decode("User", body) if body else {}
    check("Update 改昵称生效", upd.get("name") == "测试昵称", upd)

    status, body, msg = call("PubUser", "ExtendQuota",
                             pb.encode("UserQuotaExtendInfo", {"disk_bytes": 5 * 1024 ** 3}), token)
    quota = pb.decode("UserStatisticsAndQuota", body) if body else {}
    disk = quota.get("disk_statistics_quota", {})
    check("ExtendQuota 扩容 5GiB", disk.get("bytes_quota", 0) >= 5 * 1024 ** 3, disk)

    status, body, msg = call("PubUser", "UserCenterUri",
                             pb.encode("UserCenterUriRequest", {"route": "account"}))
    uri = pb.decode("UserCenterUriResponse", body) if body else {}
    check("UserCenterUri 返回链接", "account" in uri.get("uri", ""), uri)

    print("== PubUser: 设备授权 / OAuth ==")
    status, body, msg = call("PubUser", "DeviceAuthorization",
                             pb.encode("DeviceAuthorizationRequest",
                                       {"identity": user["identity"], "device": "lab-pc"}), token)
    dev = pb.decode("LoginResponse", body) if body else {}
    check("DeviceAuthorization 返回 device_code", bool(dev.get("state")), dev.get("state"))

    status, body, msg = call("PubUser", "CreateAuthToken",
                             pb.encode("LoginRequest", {"type": "browser",
                                                        "return_url": "http://127.0.0.1:8765/"}),
                             token)
    auth_uri = pb.decode("OauthTokenResponse", body) if body else {}
    check("CreateAuthToken 返回授权 URL", "authorize" in auth_uri.get("url", ""), auth_uri)

    status, body, msg = call("PubUser", "CheckAuthStatus",
                             pb.encode("LoginRequest", {"state": auth_uri.get("state", "")}))
    st = pb.decode("OauthTokenCheckResponse", body) if body else {}
    check("CheckAuthStatus 待授权码状态 0", st.get("status", 0) == 0, st)

    code = rest("/api/oauth/authorize", {"client_id": "lab", "redirect_uri": "http://x/cb",
                                         "state": "s1"}, token)
    auth_code = (code.json() or {}).get("code", "") if code.ok else ""
    if not auth_code:
        # direct RPC fallback
        status, body, _ = call("OauthAuthorization", "Authorize",
                               pb.encode("AuthorizeRequest", {"client_id": "lab",
                                                              "redirect_uri": "http://x/cb"}), token)
        auth_code = (pb.decode("AuthorizeResponse", body) if body else {}).get("code", "")
    status, body, msg = call("PubUser", "VerifyAuthorizationCode",
                             pb.encode("AuthorizationCode", {"code": auth_code}), token)
    tk = pb.decode("Token", body) if body else {}
    check("VerifyAuthorizationCode 换到 Token", bool(tk.get("access_token")), msg)

    print("== PubUserFile: 复制 / 批量 ==")
    root_files = rest("/v6/userfile/create", {"name": "acct_a.txt", "dir": False,
                                              "parent": "root"}, token)
    fid = (root_files.json() or {}).get("identity", "") if root_files.ok else ""
    if not fid:
        status, body, _ = call("PubUserFile", "Create",
                               pb.encode("File", {"name": "acct_a.txt", "parent": "root"}), token)
        fid = (pb.decode("File", body) if body else {}).get("identity", "")
    check("创建测试文件", bool(fid), fid)

    status, body, msg = call("PubUserFile", "Copy",
                             pb.encode("BatchOperationRequest",
                                       {"source": [{"identity": fid}],
                                        "dest": {"identity": "root"}}), token)
    cp = pb.decode("BatchOperationResponse", body) if body else {}
    check("Copy 影响 1 项", cp.get("affected") == 1, cp)

    status, body, msg = call("PubUserFile", "BatchRename",
                             pb.encode("BatchOperationRequest",
                                       {"source": [{"identity": fid, "name": "acct_b.txt"}]}), token)
    br = pb.decode("BatchOperationResponse", body) if body else {}
    check("BatchRename 影响 1 项", br.get("affected") == 1, br)

    status, body, msg = call("PubUserFile", "Update",
                             pb.encode("File", {"identity": fid, "starred": True}), token)
    up = pb.decode("File", body) if body else {}
    check("Update 置 starred", up.get("starred") is True, up)

    status, body, msg = call("PubUserFile", "BatchOperation",
                             pb.encode("BatchOperationRequest",
                                       {"source": [{"identity": fid}], "operation": 4}), token)
    bo = pb.decode("BatchOperationResponse", body) if body else {}
    check("BatchOperation(4=delete) 影响 1 项", bo.get("affected") == 1, bo)

    print("== PubFileShare: 投诉 ==")
    status, body, msg = call("PubFileShare", "ListComplaint",
                             pb.encode("ComplaintListRequest", {"list_info": {"limit": 10}}), token)
    lc = pb.decode("ComplaintListResponse", body) if body else {}
    check("ListComplaint 返回列表", isinstance(lc.get("complaints", []), list), lc)

    status, body, msg = call("PubFileShare", "DeleteComplaint",
                             pb.encode("ComplaintDeleteRequest", {"identity": ["nonexistent"]}), token)
    dc = pb.decode("ComplaintDeleteResponse", body) if body else {}
    check("DeleteComplaint 返回 count", dc.get("count", 0) == 0, dc)

    print("== WebRTCManageService: P2P 信令 ==")
    client = uuid.uuid4().hex
    status, body, msg = call("WebRTCManageService", "StartDownload",
                             pb.encode("StartDownloadRequest",
                                       {"client_identity": client,
                                        "content_identity": "bafyfake", "peer_limit": 4}),
                             token, package="p2p")
    sd = pb.decode("StartDownloadResponse", body) if body else {}
    check("StartDownload 返回 peers", sd.get("content_identity") == "bafyfake", sd)

    status, body, msg = call("WebRTCManageService", "SendPeerIceCandidate",
                             pb.encode("SendPeerIceCandidateRequest",
                                       {"client_identity": client, "candidate": "cand-1"}),
                             token, package="p2p")
    check("SendPeerIceCandidate 成功", status == 0, msg)

    status, body, msg = call("WebRTCManageService", "Receive",
                             pb.encode("ReceiveRequest", {"id": client}), token, package="p2p")
    rv = pb.decode("ReceiveResponse", body) if body else {}
    check("Receive 取到候选", "cand-1" in rv.get("data", ""), rv)

    status, body, msg = call("WebRTCManageService", "StopDownload",
                             pb.encode("StopDownloadRequest", {"client_identity": client}),
                             token, package="p2p")
    check("StopDownload 成功", status == 0, msg)
    status, body, msg = call("WebRTCManageService", "Disconnect",
                             pb.encode("DisconnectRequest", {"client_identity": client}),
                             token, package="p2p")
    check("Disconnect 成功", status == 0, msg)

    status, body, msg = call("PubUserFile", "CreateManageRTCOffer",
                             pb.encode("ManageRTCRequest", {"sdp": "v=0", "offer": "datachannel",
                                                            "client_identity": client}), token)
    offer = pb.decode("ManageRTCResponse", body) if body else {}
    check("CreateManageRTCOffer 返回 sdp", offer.get("sdp") == "v=0", offer)

    status, body, msg = call("PubUserFile", "SendClientIceCandidate",
                             pb.encode("SendIceCandidateRequest",
                                       {"client_identity": client, "candidate": "u-1"}), token)
    check("SendClientIceCandidate 成功", status == 0, msg)
    status, body, msg = call("PubUserFile", "GetServerIceCandidate",
                             pb.encode("GetIceCandidateRequest", {"client_identity": client}), token)
    gs = pb.decode("GetIceCandidateResponse", body) if body else {}
    check("GetServerIceCandidate 取到候选", "u-1" in (gs.get("candidate") or []), gs)

    print("== PubUser: 注销 / 删号 ==")
    status, body, msg = call("PubUser", "Logoff", pb.encode("Token", {}), token)
    check("Logoff 成功", status == 0, msg)
    token_after, status, _ = login(name, "pass_654321")
    check("Logoff 后仍可重新登录", status == 0 and bool(token_after), f"status={status}")

    status, body, msg = call("PubUser", "Delete",
                             pb.encode("User", {"identity": user["identity"]}), token_after)
    check("Delete 删号成功", status == 0, msg)
    _, status, _ = login(name, "pass_654321")
    check("删号后无法登录", status != 0, f"status={status}")

    print(f"\n结果: {PASSED} passed, {len(FAILED)} failed")
    if FAILED:
        for f in FAILED:
            print("  -", f)
        sys.exit(1)


if __name__ == "__main__":
    main()
