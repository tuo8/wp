"""当前线上 API 复核客户端（签名头 = 官方 Web 客户端算法）。
所有调用记录到 02_network_capture/api_calls.jsonl（脱敏：不写 token/secret）。
"""
import sys, os, json, time, base64
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hcc
from hcc import s, i, dec, call, _tok, _save

LOG = os.path.join(os.path.dirname(__file__), "..", "02_network_capture", "api_calls.jsonl")

def log(rec):
    with open(LOG, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

def ensure_token():
    d = _tok()
    if d.get("access_token"):
        return d["access_token"]
    r = call("v6.services.oauth.OauthAuthorization", "RefreshToken",
             s(1, "devDebugger/1.0") + s(3, d["refresh_token"]) + s(4, "refresh_token"))
    dd = dec(r["frames"][0][1])
    at = dd[1][0].decode(); rt = dd[2][0].decode()
    _save({"access_token": at, "refresh_token": rt})
    return at

def dump(buf, depth=0, maxdepth=4):
    out = {}
    try:
        fields = dec(buf)
    except Exception:
        return None
    for k, v in fields.items():
        vals = []
        for x in v:
            if isinstance(x, int):
                vals.append(x)
            else:
                try:
                    txt = x.decode()
                    printable = all(32 <= ord(c) < 127 or c in "\n\t" for c in txt) and len(txt) > 0
                except Exception:
                    printable = False
                if printable and len(x) < 2500:
                    vals.append(txt)
                elif depth < maxdepth and len(x) > 0:
                    sub = dump(x, depth + 1, maxdepth)
                    vals.append(sub if sub else "<bin %d>" % len(x))
                else:
                    vals.append("<bin %d>" % len(x))
        out[k] = vals if len(vals) > 1 else vals[0]
    return out

def rpc(service, method, payload=b"", token=None):
    t0 = time.time()
    r = call(service, method, payload, token=token)
    body = None
    if r["frames"]:
        body = dump(r["frames"][0][1])
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "service": service, "method": method,
           "req_len": len(payload), "http": r["http"], "grpc_status": r["status"],
           "grpc_message": r["message"], "ms": r["ms"],
           "resp_len": len(r["frames"][0][1]) if r["frames"] else 0,
           "server": r["headers"].get("server"), "resp": body}
    log(rec)
    return r, body
