#!/usr/bin/env python3
"""生成自制测试文件：随机文本/二进制/PNG，全部可复现（种子固定）。"""
import os
import zlib
import struct
import random
import hashlib

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "testfiles")
os.makedirs(OUT, exist_ok=True)
rnd = random.Random(20261122)


def rnd_bytes(n):
    return bytes(rnd.getrandbits(8) for _ in range(n))


def make_txt(path, n=4096):
    with open(path, "w", encoding="utf-8") as f:
        f.write("# HalalCloud audit test file\n")
        f.write("generated: 2026-11-22T20:50+08:00\n")
        while f.tell() < n:
            f.write(hashlib.sha256(str(rnd.random()).encode()).hexdigest() + "\n")


def make_png(path, w=800, h=600):
    raw = bytearray()
    for y in range(h):
        raw.append(0)
        for x in range(w):
            r = (x * 255) // (w - 1)
            g = (y * 255) // (h - 1)
            b = ((x + y) * 255) // (w + h - 2)
            raw += bytes((r, g, b))

    def chunk(tag, data):
        c = struct.pack(">I", len(data)) + tag + data
        return c + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    open(path, "wb").write(png)


def make_mp4(path, seconds=3, w=320, h=240, fps=10):
    """构造一个极简可解析的 MP4 容器（无编码帧，仅用于容器上传/预览字段测试）。"""
    import struct as st
    timescale = 1000
    dur = seconds * timescale

    def box(tag, payload):
        return st.pack(">I", 8 + len(payload)) + tag + payload

    ftyp = box(b"ftyp", b"isom" + st.pack(">I", 0x200) + b"isomiso2avc1mp41")
    mvhd = box(b"mvhd", st.pack(">IIIII", 0, 0, 0, timescale, dur)
               + st.pack(">I", 0x00010000) + st.pack(">H", 0x0100) + b"\x00\x00"
               + b"\x00" * 8 + st.pack(">9i", 0x00010000, 0, 0, 0, 0x00010000, 0, 0, 0, 0x40000000)
               + b"\x00" * 24 + st.pack(">I", 2))
    moov = box(b"moov", mvhd)
    mdat = box(b"mdat", b"\x00" * (w * h // 64))
    open(path, "wb").write(ftyp + moov + mdat)


if __name__ == "__main__":
    make_txt(os.path.join(OUT, "hc-test-small.txt"), 4096)
    for name, n in [("hc-test-1m.bin", 1024 * 1024),
                    ("hc-test-4m.bin", 4 * 1024 * 1024),
                    ("hc-test-20m.bin", 20 * 1024 * 1024)]:
        p = os.path.join(OUT, name)
        with open(p, "wb") as f:
            step = 1 << 20
            for _ in range(n // step):
                f.write(rnd_bytes(step))
    make_png(os.path.join(OUT, "hc-test-image.png"))
    make_mp4(os.path.join(OUT, "hc-test-video.mp4"))

    for f in sorted(os.listdir(OUT)):
        p = os.path.join(OUT, f)
        d = open(p, "rb").read()
        print(f"{f:24s} {len(d):>10d}  sha256={hashlib.sha256(d).hexdigest()}")
