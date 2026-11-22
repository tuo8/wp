# 3.15 GB 大文件的「下载」与「播放」分别走什么存储路径

> 对象：`/hca-2026-11-23/CAWD-987-AI.mp4`
> 大小：3 381 078 505 B（3.15 GiB，界面显示 3.15 GB）
> 根标识：`bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4`
> 身份 ID：`59227eb8d779e2abfa7d66f901124a77`
> 取证时间：2026-11-23 01:29–01:39（UTC+8）
> 证据文件：`05_cid_and_chunks/gb_structure.json`、`gb_download.json`、`gb_per_block.json`、
> `10_raw_evidence/playback_dns.jsonl`、`playback_rx.jsonl`、`playback_screen3.png`

## 一句话结论

**同一个文件，两条完全不同的读取通道**：

| 行为 | 走哪条链路 | 存储形态 | 传输形态 | 是否落盘 |
|---|---|---|---|---|
| 点「下载」/ 协议层取文件 | `slices.qzy.xyz`（加密分片网关） | 807 个 4 MiB 内容寻址块 + 1 个 dag-pb 根节点 | **4 并发**逐块取回，每块一个随机 XOR 键解密，块级哈希校验 | 是，写入应用下载目录 |
| 点开播放（在线播放） | `ipfs-gateway.qzy.xyz`（明文整文件网关） | 同一份块存储在服务端被拼成连续字节流 | **单连接**渐进读取，支持 HTTP Range | 否，播放结束下载目录无任何新增文件 |

两者读的是**同一条自建内容寻址块存储**，只是服务端提供了两个不同的读出接口：一个按「块」给（带加密），一个按「文件字节流」给（明文 + Range）。

---

## 1. 该文件的 DAG 结构（CONFIRMED）

| 项 | 实测值 |
|---|---|
| 根节点类型 | UnixFS `Type=File(2)`，`filesize=3381078505` |
| 根节点字节数 | **41 973 B**（单个 dag-pb 节点） |
| 根节点链接数 | **807** |
| 叶子块大小 | 806 × 4 194 304 B + 1 × 469 481 B |
| blocksizes 求和 | 3 381 078 505 B（与文件大小**完全相等**） |
| 层级 | **单层**：root → 807 个 `raw` 叶子，**无中间节点** |
| 根 CID 独立复算 | `bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4` → **MATCH** |

关键点：

- 3.15 GiB 的文件**没有**出现 IPFS 常见的「中间层 dag-pb」，807 个链接全部挂在根节点上。原因是节点序列化后只有 41.9 KB，远低于 1 MiB 单块上限，服务端的 DAG 构造器只在超过上限时才分层。
- 因此**不能用「有中间层」当作 IPFS 特征**；这里用「raw 叶子 CID + UnixFS dag-pb 根 + 复算一致」来判定，而不是靠 CID 前缀猜。
- 807 个叶子 CID 全部为 `bafkrei…`（CIDv1 / raw / sha2-256），逐个校验通过（807/807）。

## 2.「下载」链路实测（CONFIRMED）

### 2.1 逐块取回与校验

- 取址：`PubUserFile/GetSliceDownloadAddress(cid)` → `https://slices.qzy.xyz/ipfs/blocks/{a}/{bc}/{cid}?auth_key=[REDACTED] `encrypt` 键。
- 传输：4 并发，**807/807 块成功，0 失败**，逐块 `sha256(解密后字节)` 必须等于该块 CID。
- 拼装：按 root 的 blocksizes 顺序写盘 → 整文件 **SHA-256 = `9241bf39ec7f1f5622bbf1d82f50f202030a3fa2ebbde5ecd7999a45e4ea13c1`**（3 381 078 505 B 全量参与计算）。

### 2.2 并发与吞吐（同一时段、同一网络）

| 并发 | 32 块耗时 | 吞吐 | 单块 p50 |
|---|---|---|---|
| 1 | 27.67 s | 4.85 MB/s | 766 ms |
| **4** | **9.40 s** | **14.29 MB/s** | 856 ms |
| 8 | 8.50 s | 15.78 MB/s | 1 695 ms |
| 16 | 8.79 s | 15.27 MB/s | 3 276 ms |

- 4 并发已拿到 90% 峰值；8/16 并发只增加排队延迟（p50 翻倍到 3.3 s），吞吐不再涨 → 瓶颈在单连接带宽与网关，不在并发数。
- 全量 807 块用 4 并发跑完：**268.0 s，12.62 MB/s**（含取址开销；取址 p50 187 ms / p95 407 ms，共 807 次）。

### 2.3 每块一个随机加密键

807 个块的 `encrypt` 值去重后 **32 种**（取值 50–110 等），同一文件不同块键不同 → 加密是**按块随机**的 XOR 掩码，不是文件级固定密钥。这也是为什么 `slices` 通道无法被普通下载器直接抓取：必须先拿到每个块的键，再按块解密。

### 2.4 分发层（CDN）

807 次取块的 `via` 头统计：

- 命中边缘缓存：`age` 0–2（807/807 都有 age 头）→ 块在边缘节点基本是热的。
- 回源节点高度集中：`pic01.hhhtcu 211`、`pic02.hhhtcu 207`、`pic03.hhhtcu 200`、`pic04.hhhtcu 189`（合计 807，正好一块一次）→ **源站侧是 4 台 `pic0x.hhhtcu` 组成的块存储集群**。
- 边缘节点分散在 `cache03.tzmp / cache20.czmp / cache25.sxmp / cache48.sjzmp …` 数十个节点。

## 3.「播放」链路实测（CONFIRMED）

### 3.1 端点归因（不是猜 IP，而是同时抓 DNS 与 connect）

对 App 进程同时挂 `getaddrinfo` 与 `connect`：

| 事件 | 内容 |
|---|---|
| 播放期间唯一成功解析的域名 | **`ipfs-gateway.qzy.xyz`**（5 次成功解析，无其他域名） |
| 实际连接目标 | `101.72.219.149:443`、`101.72.219.166:443`（`ipfs-gateway.qzy.xyz` 解析结果内的地址） |
| 播放期间是否出现 `slices.qzy.xyz`（116.136.20.227/228） | **否** |

→ 播放**不走**加密分片通道，走**明文整文件网关**。

> 注意：`101.72.219.163` 同时属于 `ipfs-gateway.qzy.xyz` 与 `simple-file-proxy.2dland.cn` 的解析集合（同一组 Byte-nginx 入口 IP）。所以**只靠 IP 无法区分**上传代理与下载网关，本结论是靠「同时抓到域名解析 + 紧随其后的 connect」得到的，不是靠 IP 表。

### 3.2 传输形态

- 单条 TCP 连接承担绝大部分流量：一个 fd 累计 **156.0 MB**，另一条 **13.1 MB**（换 IP 重连）。
- 速率约 **2.0–2.6 MB/s**，与播放位置推进同步；播放器显示总时长 **02:00:32**，3 381 078 505 B / 7 232 s ≈ **467 KB/s** 平均码率（≈3.7 Mbps）→ 实际拉取速率约为码率的 4–5 倍（读前缓冲）。
- 暂停后 **入向字节立即归零**（17:37:07 之后再无新增），恢复播放后继续 → 典型的**按需渐进拉流**，不是整文件预下载。
- 全程仅取回 169.1 MB（约占 3.15 GiB 的 5%），未触发全量下载。

### 3.3 是否落盘

播放前后应用外部目录对比：

```
/sdcard/Android/data/com.halalcloud.diskmanager.halalcloud/files
  播放前 51 295 KB → 播放后 51 295 KB
  downloads/ 内只有此前的 hca-kb1-full.bin(1 KB)、hca-up-mb50.bin(50 MB)
```
- 播放**不产生**任何本地视频文件；缓存只在内存/进程内部（应用非 debuggable，内部 cache 目录无法直接读取，这一点标注 `INFERRED`：无外部文件增长，但内部缓存大小未能直接取证）。

### 3.4 为什么播放能 Range

对同一对象直接验证网关能力：

| 请求 | 结果 |
|---|---|
| `HEAD /file/{cid}/{uid}/{name}` | 200，`Content-Length: 3381078505`，`Accept-Ranges: bytes`，`ETag: "bafybeiebig…"`（=CID） |
| `Range: bytes=0-1023` | **206**，`Content-Range: bytes 0-1023/3381078505`，返回 1 024 B |
| `Range: bytes=3381077505-3381078504` | **206**，`Content-Range: bytes 3381077505-3381078504/3381078505` |

→ 网关具备完整 Range 能力，播放器按字节区间拉流即可，无需理解 DAG。

## 4. 这意味着怎样的存储架构

```
上传：客户端 → simple-file-proxy(Byte-nginx) → 块按 CID 入库（4 MiB/块，raw）
commit：服务端按 blocksizes 生成 UnixFS 根（本例 807 链接、41 973 B 单节点）
读取①（下载）：GetSliceDownloadAddress → slices.qzy.xyz 按块给（每块随机 XOR 键）
读取②（播放）：GetDirectDownloadAddress → ipfs-gateway.qzy.xyz 按字节流给（明文 + Range）
落盘：同一套内容寻址块库（/ipfs/blocks/{a}/{bc}/{cid} 两级目录分桶），前置多层 CDN
```

- 内容寻址 + UnixFS 兼容的 DAG，但**不在公共 IPFS 网络**：该根 CID 在 `delegated-ipfs.dev/routing/v1/providers` 返回空 `{"Providers":[]}`，同批测试的对照 CID 返回 30 KB provider 列表；`ipfs.io` / `dweb.link` 在本网络不可达，无法反向验证（标注 `INFERRED`：无 provider 记录 ⇒ 未向公共 DHT 公告）。
- 播放与下载的**权限模型不同**：下载接口按块发放、逐块带 `auth_key` 与 `encrypt`；播放接口按文件发放一次性 `auth_key`（含 `session`、过期时间），因此播放链接可被外部播放器直接使用（只要不超期），下载链接不能。

## 5. 结论（按证据等级）

| 判断 | 等级 |
|---|---|
| 3.15 GiB 文件 = 807 × ≤4 MiB 内容寻址块 + 1 个 41 973 B UnixFS 根节点，单层 DAG | CONFIRMED（807 块全量校验 + 根 CID 复算一致） |
| 下载走 `slices.qzy.xyz` 加密分片通道，按块随机 XOR，4 并发 | CONFIRMED |
| 播放走 `ipfs-gateway.qzy.xyz` 明文整文件网关，HTTP Range 渐进 | CONFIRMED（DNS+connect 同时归因 + Range 206 实测） |
| 播放不落盘整文件 | STRONGLY_SUPPORTED（外部目录零增长；内部 cache 未直接取证） |
| 源站为 4 台 `pic0x.hhhtcu` 块存储节点 + 多层 CDN | STRONGLY_SUPPORTED（`via` 头 807 次统计） |
| 底层物理介质（SSD/HDD/对象存储） | **UNKNOWN**（`SERVER_SIDE_PERSISTENCE_BACKEND_UNCONFIRMED`） |
| 是否在公共 IPFS 网络可检索 | CONTRADICTED（DHT 无 provider） |

## 6. 复现命令

```bash
python 11_scripts/t26_gb_structure.py    # DAG 结构 + 网关 Range
python 11_scripts/t27_gb_download.py     # 807 块全量下载 + 并发标定 + 整文件哈希
python capture/run_dns.py  <out.jsonl>   # 播放期间 DNS/connect 归因（Frida gadget）
python capture/run_rx.py   <out.jsonl>   # 入向字节统计
```
