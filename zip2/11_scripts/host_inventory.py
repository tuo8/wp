import socket, ssl, json, sys
HOSTS = ["drcdn2.2dland.cn", "ucenter.2dland.cn", "simple-file-proxy.2dland.cn",
         "slices.qzy.xyz", "ipfs-gateway.qzy.xyz", "drive.2dland.cn", "2dland.cn"]
out = []
for h in HOSTS:
    rec = {"host": h}
    try:
        infos = socket.getaddrinfo(h, 443, proto=socket.IPPROTO_TCP)
        rec["ip"] = sorted({x[4][0] for x in infos})
    except Exception as e:
        rec["dns_error"] = str(e); out.append(rec); print(json.dumps(rec, ensure_ascii=False)); continue
    ips = rec["ip"]
    if ips:
        try:
            ctx = ssl.create_default_context()
            with socket.create_connection((ips[0], 443), timeout=8) as sk:
                with ctx.wrap_socket(sk, server_hostname=h) as ss:
                    c = ss.getpeercert()
                    rec["tls"] = ss.version()
                    rec["issuer"] = dict(x[0] for x in c.get("issuer", []))
                    rec["not_after"] = c.get("notAfter")
                    rec["san"] = [v for k, v in c.get("subjectAltName", [])][:6]
        except Exception as e:
            rec["tls_error"] = str(e)
    out.append(rec)
    print(json.dumps(rec, ensure_ascii=False))
json.dump(out, open("02_network_capture/host_inventory.json", "w"), indent=2, ensure_ascii=False)
