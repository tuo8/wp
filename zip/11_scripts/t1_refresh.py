import json, sys
sys.path.insert(0, __file__.rsplit("/",1)[0])
from hcc import refresh
r = refresh()
print("HTTP", r.status_code)
for k, v in r.headers.items():
    if k.lower() in ("server","via","content-type","content-length","date","cache-control","x-cache","x-request-id","traceparent"):
        print(f"{k}: {v}")
body = r.text
print("BODY_LEN", len(body))
print("BODY_HEAD", body[:400])
