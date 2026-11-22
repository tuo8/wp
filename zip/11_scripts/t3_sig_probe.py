import sys, time, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
import hcc
from hcc import s, _tok, S, HOST
d = _tok()
payload = s(1, hcc.CLIENT_ID) + s(3, d["refresh_token"]) + s(4, "refresh_token")
frame = b"\x00" + len(payload).to_bytes(4, "big") + payload
path = "/v6.services.oauth.OauthAuthorization/RefreshToken"
combos = [
    ("only-timestamp", {"timestamp": str(int(time.time()*1000))}),
    ("ts+appid", {"timestamp": str(int(time.time()*1000)), "appid": "devDebugger/1.0"}),
    ("ts+appid+ver", {"timestamp": str(int(time.time()*1000)), "appid": "devDebugger/1.0", "appversion": "1.0.24"}),
]
for name, extra in combos:
    h = {"Content-Type": "application/grpc-web+proto", "X-Grpc-Web": "1", "Accept": "application/grpc-web+proto",
         "User-Agent": "HalalCloud-Dart/1.0.24; Android"}
    h.update(extra)
    r = S.post(HOST + path, data=frame, headers=h, timeout=20)
    print(name, "http", r.status_code, "status", r.headers.get("grpc-status"), "msg", r.headers.get("grpc-message"))
