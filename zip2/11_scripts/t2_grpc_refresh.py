import sys, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc
from hcc import grpc, s, _tok
d = _tok()
p = s(1, hcc.CLIENT_ID) + s(3, d["refresh_token"]) + s(4, "refresh_token")
r = grpc("/v6.services.oauth.OauthAuthorization/RefreshToken", p)
print("HTTP", r["http"], "ms", r["ms"], "grpc-status", r["grpc_status"], "grpc-msg", r["grpc_message"])
for k in ("server","content-type","grpc-encoding","date"):
    if k in r["headers"]: print(f"{k}: {r['headers'][k]}")
print("frames", [(f[0], len(f[1])) for f in r["frames"]])
if r["frames"]:
    dec = hcc.dec(r["frames"][0][1])
    print("fields", {k: (v[0][:12] if isinstance(v[0], bytes) else v[0]) for k, v in dec.items()})
