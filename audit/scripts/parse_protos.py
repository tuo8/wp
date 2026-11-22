#!/usr/bin/env python3
"""从 proto 中提取 RPC / REST 路由清单（花括号配平解析）。"""
import re
import glob
import os
import json

BASE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "downloads",
                    "halalcloud_apk", "protos")


def parse_file(path):
    s = open(path, encoding="utf-8", errors="replace").read()
    pkg = re.search(r"package\s+([\w.]+)\s*;", s)
    svc = re.search(r"service\s+(\w+)\s*\{", s)
    sname = (pkg.group(1) + "." + svc.group(1)) if (pkg and svc) else "?"
    out = []
    for m in re.finditer(r"rpc\s+(\w+)\s*\(", s):
        j = s.find("{", m.end())
        if j < 0:
            continue
        depth, k = 0, j
        while k < len(s):
            if s[k] == "{":
                depth += 1
            elif s[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        body = s[j:k]
        h = re.search(r'post:\s*"([^"]+)"', body)
        out.append({"service": sname, "rpc": m.group(1),
                    "rest": h.group(1) if h else None,
                    "commented": s[max(0, m.start() - 3):m.start()].strip().startswith("//")})
    return out


if __name__ == "__main__":
    allr = []
    for f in sorted(glob.glob(os.path.join(BASE, "*.proto"))):
        allr += parse_file(f)
    with_rest = [r for r in allr if r["rest"]]
    print("RPC total:", len(allr), "| 有 REST 注解:", len(with_rest))
    for r in allr:
        print(f"{r['service']:40s} {r['rpc']:30s} {r['rest'] or '-'}")
    json.dump(allr, open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                      "evidence", "rest_routes.json"), "w"),
              ensure_ascii=False, indent=2)
