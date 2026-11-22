import sys, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc
from hcc import s, call, _tok, _save
d = _tok()
r = call("v6.services.oauth.OauthAuthorization", "RefreshToken",
         s(1, "devDebugger/1.0") + s(3, d["refresh_token"]) + s(4, "refresh_token"))
print("HTTP", r["http"], "grpc", r["status"], r["message"], "ms", r["ms"])
if r["frames"]:
    dec = hcc.dec(r["frames"][0][1])
    print("fields:", {k: (type(v[0]).__name__, (len(v[0]) if isinstance(v[0], bytes) else v[0])) for k, v in dec.items()})
    at = dec.get(1, [b""])[0]
    rt = dec.get(2, [b""])[0]
    if at:
        _save({"refresh_token": (rt.decode() if isinstance(rt, bytes) else rt) or d["refresh_token"],
               "access_token": at.decode() if isinstance(at, bytes) else at})
        print("ACCESS_TOKEN_SAVED len", len(at))
