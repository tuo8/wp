"""独立复算 UnixFS/dag-pb 根 CID（不依赖任何 IPFS 库）"""
import hashlib, base64

def vi(n):
    out = b""
    while True:
        b = n & 0x7F
        n >>= 7
        out += bytes([b | (0x80 if n else 0)])
        if not n:
            return out

def cidv1_raw_bytes(data):
    return b"\x01\x55\x12\x20" + hashlib.sha256(data).digest()

def b32(b):
    return "b" + base64.b32encode(b).decode().lower().rstrip("=")

def unixfs_file_data(filesize, blocksizes):
    m = b"\x08\x02"                      # Type = File(2)
    if filesize < 262144:
        m += b"\x18" + vi(filesize)
    else:
        m += b"\x18" + vi(filesize)
        for bs in blocksizes:
            m += b"\x20" + vi(bs)        # blocksizes
    return m

def dagpb_file_root(child_cids, blocksizes, filesize):
    node = b""
    for cid_bytes, tsize in zip(child_cids, blocksizes):
        link = b"\x0a" + vi(len(cid_bytes)) + cid_bytes   # Hash (bytes)
        link += b"\x12\x00"                                # Name = ""
        link += b"\x18" + vi(tsize)                        # Tsize
        node += b"\x12" + vi(len(link)) + link             # PBLink (field 2)
    data = unixfs_file_data(filesize, blocksizes)
    node += b"\x0a" + vi(len(data)) + data                 # Data (field 1)
    root = b"\x01\x70\x12\x20" + hashlib.sha256(node).digest()
    return b32(root), node
