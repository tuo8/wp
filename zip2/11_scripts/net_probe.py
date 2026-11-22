import socket, ssl, json, sys, hashlib
host = "drcdn2.2dland.cn"
out = {}
try:
    infos = socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)
    ips = sorted({i[4][0] for i in infos})
    out["dns"] = ips
except Exception as e:
    out["dns_error"] = str(e); ips = []
print(json.dumps({"host": host, "dns": out.get("dns")}, ensure_ascii=False))
for ip in ips[:3]:
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((ip, 443), timeout=8) as s:
            with ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = ss.getpeercert()
                print(json.dumps({
                    "ip": ip, "tls": ss.version(), "cipher": ss.cipher()[0],
                    "subject": dict(x[0] for x in cert.get("subject", [])),
                    "issuer": dict(x[0] for x in cert.get("issuer", [])),
                    "notAfter": cert.get("notAfter"),
                    "SAN": cert.get("subjectAltName", [])[:8],
                }, ensure_ascii=False))
    except Exception as e:
        print(json.dumps({"ip": ip, "tls_error": str(e)}))
