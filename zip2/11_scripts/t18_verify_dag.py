import sys, os, json
sys.path.insert(0, "/".join(__file__.split("/")[:-1]))
from dagbuild import cidv1_raw_bytes, dagpb_file_root, b32
from cidutil import cidv1_raw
meta = json.load(open(os.path.join(os.path.dirname(__file__), "..", "03_upload", "upload_hca-mb10.bin.json")))
p = os.path.join(os.path.dirname(__file__), "..", "03_upload", "testcorpus", "hca-mb10.bin")
data = open(p, "rb").read()
bsz = meta["block_size"]
blocks = [data[o:o+bsz] for o in range(0, len(data), bsz)]
child = [cidv1_raw_bytes(b) for b in blocks]
sizes = [len(b) for b in blocks]
root, node = dagpb_file_root(child, sizes, len(data))
print("blocks:", sizes)
print("child CIDs:", meta["block_cids"])
print("recomputed root:", root)
print("server  root  :", meta["content_identity"])
print("MATCH:", root == meta["content_identity"])
print("dag-pb node len:", len(node))
