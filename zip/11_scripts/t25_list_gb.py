import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import api
from hcc import s, i

tok = api.ensure_token()
DIR = "b648aca0cc94070f0de2a016b4aded76"
r, body = api.rpc("v6.services.pub.PubUserFile", "List", s(1, s(1, DIR)) + s(3, i(2, 200)), token=tok)
files = body.get(1, [])
if isinstance(files, dict): files = [files]
out = []
for f in files:
    out.append({"name": f.get(4), "identity": f.get(1), "size": f.get(3), "cid": f.get(24), "mtime": f.get(6)})
out.sort(key=lambda x: -(x["size"] or 0))
json.dump(out, open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "dir_listing.json"), "w"), indent=2, ensure_ascii=False)
for x in out[:15]:
    print(f'{x["size"]:>12}  {x["name"]}  {x["cid"]}  {x["identity"]}')
