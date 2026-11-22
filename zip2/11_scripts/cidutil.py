import hashlib, base64

def cidv1_raw(data: bytes) -> str:
    h = hashlib.sha256(data).digest()
    raw = b"\x01\x55\x12\x20" + h
    return "b" + base64.b32encode(raw).decode().lower().rstrip("=")

def cidv1_dagpb(data: bytes) -> str:
    h = hashlib.sha256(data).digest()
    raw = b"\x01\x70\x12\x20" + h
    return "b" + base64.b32encode(raw).decode().lower().rstrip("=")

def sha256hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()
