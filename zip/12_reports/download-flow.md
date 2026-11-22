# 下载流程（两条通道 + 实测数据）

> 证据：`04_download/direct_download.json`、`04_download/slice_download.json`、
> `04_download/app_downloaded_kb1.bin`、`04_download/app_downloaded_mb50.bin`、
> `05_cid_and_chunks/gb_download.json`、`05_cid_and_chunks/gb_per_block.json`

## 通道总览

| | 通道 A：整文件网关 | 通道 B：分片网关 |
|---|---|---|
| 取址 API | `PubUserFile/GetDirectDownloadAddress` | `PubUserFile/GetSliceDownloadAddress` |
| 数据端点 | `ipfs-gateway.qzy.xyz/file/{cid}/{uid}/{name}?auth_key=…&session=…` | `slices.qzy.xyz/ipfs/blocks/{a}/{bc}/{cid}?auth_key=…` |
| 内容形态 | 明文整文件字节流 | 单块（根节点也是块），**逐块 XOR 加密** |
| 支持 Range | 是（206，`Accept-Ranges: bytes`） | 单块整取，无 Range |
| 典型用途 | 在线播放、Web 直链 | App 内「下载」、大文件断点/并发 |
| 校验 | 只能整文件 SHA-256 | **逐块校验**：`sha256(dec) == cid` |

## 通道 A：整文件网关（CONFIRMED）

10 MiB 对象实测：

| 次 | HTTP | 字节 | SHA-256 | server | via | age |
|---|---|---|---|---|---|---|
| 1 | 200 | 10 485 760 | 匹配 | Byte-nginx | `cache05.czmp,cache03.sclfcu01` | 0 |
| 2 | 200 | 10 485 760 | 匹配 | Byte-nginx | `cache03.sclfcu01` | 1 |

3.15 GiB 对象实测：

| 请求 | 结果 |
|---|---|
| HEAD | 200，`Content-Length: 3381078505`，`Accept-Ranges: bytes`，`ETag: "bafybeiebig24…hh4"` |
| `Range: bytes=0-1023` | 206，`Content-Range: bytes 0-1023/3381078505` |
| `Range: bytes=3381077505-3381078504` | 206（取尾 1 000 B 成功） |

- `ETag` 恒等于对象 CID ⇒ 网关把 CID 直接当作缓存键。
- `Last-Modified` 是固定占位值（`Sun, 04 Jun 1989 15:04:05 GMT`），不能用于时效判断。
- 该通道**看不到分块结构**：网关在服务端把 DAG 拼成连续字节，播放器/浏览器无需理解 UnixFS。

## 通道 B：分片网关（CONFIRMED）

### 取址响应字段

```
{ identity: <cid>, download_address: https://slices.qzy.xyz/ipfs/blocks/2/63/<cid>?auth_key=…,
  encrypt: <int>, store_type: …, expire_at: … }
```

- `encrypt` 是**每个块各自随机**的整数（10 MiB 实测 99/112/117/110；3.15 GiB 全量 807 块去重后 32 种，取值 50–110）。解密 = 逐字节 XOR。
- 根节点同样加密，必须先解根才能拿到子块 CID。
- 路径 `/ipfs/blocks/2/63/{cid}` 的两级数字取自 multihash 摘要前缀 ⇒ 后端是**内容寻址 + 两级目录分桶**的本地块库，不是 key 平铺的对象存储。

### 小文件逐块还原（10 MiB）

| 节点 | 字节 | encrypt | ETag | 耗时 |
|---|---|---|---|---|
| 根（dag-pb） | 165 | 99 | `bafybeiesgdft…` | — |
| 块 1 | 4 194 304 | 112 | `bafkreiccgyrr…` | 916 ms |
| 块 2 | 4 194 304 | 117 | `bafkreif4uxfu…` | 773 ms |
| 块 3 | 2 097 152 | 110 | `bafkreies3too…` | 472 ms |

拼装 10 485 760 B，与原文**逐字节相等**。

### 大文件全量（3.15 GiB / 807 块）

| 指标 | 值 |
|---|---|
| 成功块 / 总块 | **807 / 807**，0 失败 |
| 并发 | 4 |
| 数据耗时 | 268.02 s |
| 吞吐 | **12.62 MB/s** |
| 取址耗时 | p50 187 ms，p95 407 ms（807 次） |
| 单块取回 | p50 996 ms，p95 1 513 ms，max 3 891 ms |
| 整文件 SHA-256 | `9241bf39ec7f1f5622bbf1d82f50f202030a3fa2ebbde5ecd7999a45e4ea13c1` |
| 边缘缓存 | 807/807 带 `age`，取值 0–2 |
| 回源节点 | `pic01.hhhtcu` 211、`pic02.hhhtcu` 207、`pic03.hhhtcu` 200、`pic04.hhhtcu` 189 |

并发标定（同 32 块）：

| 并发 | 吞吐 | 单块 p50 |
|---|---|---|
| 1 | 4.85 MB/s | 766 ms |
| 4 | 14.29 MB/s | 856 ms |
| 8 | 15.78 MB/s | 1 695 ms |
| 16 | 15.27 MB/s | 3 276 ms |

→ 4 并发已达 90% 峰值；再往上只增延迟。这与客户端「下载线程数 = 4」的默认配置一致，也是 App 侧 50 MiB 下载实测并发峰值 4 的原因。

## App 侧实证（不经我方脚本，直接看客户端行为）

| 对象 | App 内下载结果 | SHA-256 与源文件 |
|---|---|---|
| hca-kb1-full.bin（1 KB） | 落盘 `files/downloads/hca-kb1-full.bin` | 一致 |
| hca-up-mb50.bin（50 MiB） | 落盘 `files/downloads/hca-up-mb50.bin`，出口 `116.136.20.228:443`（slices.qzy.xyz） | 一致 |

- 小文件走 `drcdn2`（API 同域），大文件切到 `slices.qzy.xyz` 加密分片网关。
- 下载完成后文件落在应用外部目录 `Android/data/<pkg>/files/downloads/`，文件名保持原名。

## 断点/续传

- 未观察到独立的「断点续传」接口：分片通道天然可按块续传（按块 CID 重取），网关通道依赖 HTTP Range。
- 传输任务状态由客户端本地 SQLite 驱动（`sync_task_config`），停用后进程内无流量、恢复后继续 —— 断点信息在**客户端**，不在服务端会话。

## 对比结论

- **同一份块存储，两个读出口**：A 面向「按字节顺序读」（播放），B 面向「按块并发读 + 校验」（下载）。
- 只有 B 通道能给出**端到端可验证性**（逐块哈希），A 通道只能靠整文件哈希。
- 两条通道都命中同一套 CDN（`via: cacheXX.*`），回源都到 `pic0x.hhhtcu` 块存储集群。
