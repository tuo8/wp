# 清真云（6pan / HalalCloud）当前线上真实存储架构独立取证

**报告版本**：v1.0（本轮实测）
**取证时间**：2026-11-22 16:50 — 2026-11-23 01:40（UTC+8）
**报告对象**：清真云当前线上运行实例（不是历史版本，不是旧仓库）
**结论口径**：仅使用本轮新采集数据；所有历史资料被隔离在 `historical_reference/`，未参与任何结论。

---

## 0. 方法说明与边界（先读这一节）

本次取证有两条证据链，互为交叉验证：

**链 A — 官方客户端本体（运行时观测）**
使用官方 1.0.24 安装包（`base.apk` + `split_config.arm64_v8a.apk` + `split_config.zh.apk`）重打包，仅做了三件事：
1) 注入 Frida gadget 以便在进程内观测 socket 行为；
2) 移除 `requiredSplitTypes` 以便合成单体包安装（MIUI 的 adb 安装确认页在该 ROM 上崩溃，见 §A.3）；
3) 不修改任何业务代码、不修改任何域名/端点。

业务逻辑、网络库、协议实现全部是官方原样。App 侧只能观测到**连接级**信息（目标 IP/端口、每条连接发送字节数、时间），**不能解密 TLS 内容**（原因见 §A.4）。

**链 B — 协议级独立复核（同账号、同端点）**
用官方 **Web 客户端**（`drive.2dland.cn`，公开可访问）中内嵌的签名算法，构造同构请求，直接与本轮实测到的端点交互。这样能拿到完整的 HTTP/gRPC 状态码、响应头、结构化响应体与真实 CID。

> 说明：该签名算法来自官方 Web 客户端公开 JS bundle 的静态分析，属于"当前官方客户端的实现细节"，不是历史资料。算法与 appid/appversion 在报告中给出；`appsecret` 属凭据，**已脱敏**，其获取位置在 §B.1 中说明，以便第三方独立复核。

**未做的事（明确声明）**
- 未破解 TLS、未安装根证书、未使用 Magisk、未提权。
- 未访问任何其他用户的数据；全部测试文件由本人生成并上传到本人账号。
- 未扫描服务端非授权端口、未尝试绕过鉴权。
- 未使用任何历史抓包、历史 CID、历史域名清单作为证据。

---

## 1. 调查对象

| 项目 | 值 | 证据 |
|---|---|---|
| App 名称 | HalalCloud（清真云 / 6pan） | `aapt2 dump badging` |
| 包名 | `com.halalcloud.diskmanager.halalcloud` | 同上 |
| Version Name | **1.0.24** | 同上 |
| Version Code | **24** | 同上 |
| targetSdk / compileSdk | 36 / 36 | 同上 |
| minSdk | 24 | 同上 |
| 官方 base.apk SHA-256 | `904b3fd5d4e6c3dba96c8c7fc0c8f4730c319f289f4afb0c86691931f5721c87` | `01_app_identity/official_apk_sha256.txt` |
| split_config.arm64_v8a.apk SHA-256 | `3514540100dbcc20f5737c79fa072c37169facbb446c64fa6efdd275c1628c42` | 同上 |
| split_config.zh.apk SHA-256 | `5c6d1b51c22072efefa4f2240ef2786e81532bb398540005388cb4659b25e166` | 同上 |
| 官方签名证书 SHA-256 | `2edbacbf4197c221ec40fdc270b6a087bb934283a22703610078ba3b36f781f8` | `01_app_identity/official_signing_cert.txt` |
| 测试设备 | Redmi K60 Ultra（`23046PNC9C`，代号 yuechu） | `adb shell getprop` |
| 系统 | Android 15，arm64-v8a | 同上 |
| 网络 | Wi-Fi `4k电视_5G`（非代理、非 VPN） | `dumpsys netstats` 网络键 |
| 登录账号 | `loli001`（uid 见脱敏说明，12 TB 配额已用 5 TB） | App 主页 + 接口返回 |

**客户端身份确认**：安装包来自官方分发渠道的 1.0.24 全套 split，签名证书与官方一致（重打包版使用自签名，仅用于注入，未改变业务代码）。

### 1.1 客户端开发栈（静态）

- Flutter（`libflutter.so` 11.3 MB、`libapp.so` 10.7 MB，AOT）
- BoringSSL **静态链接进 libflutter.so**：`readelf -d` 仅有 `libc/libdl/libm/libandroid/libEGL/libGLESv2/liblog/libjnigraphics`，无 `libssl.so` 依赖 → 这是 TLS 不可解密的根因。
- 原生依赖：`libmpv.so`（17 MB，播放器）、`libsqlite3.so`、`libsentry.so`、`libmediakitandroidhelper.so`。
- 客户端自带本地数据库 `halalcloud.db.sqlite`（同步任务/状态机）。

---

## 2. 当前真实网络架构

### 2.1 实测端点清单

| 域名 | 角色 | 实测 IP | 服务端指纹 | 证据 |
|---|---|---|---|---|
| `drcdn2.2dland.cn` | **控制面 API** | 119.188.175.5 / 221.204.163.72 | `server: envoy` | App 运行时 connect；gRPC-Web 200 |
| `ucenter.2dland.cn` | OAuth 授权 | 116.136.159.41 | Let's Encrypt `*.2dland.cn` | App 登录跳转 URL |
| `simple-file-proxy.2dland.cn` | **上传入口** | 101.26.38.162；101.72.219.132–174 | `server: Byte-nginx` | POST 块 201；commit 200 |
| `slices.qzy.xyz` | **加密分片网关** | 116.136.20.227 / .228 | `server: Byte-nginx` | 取块 200，XOR 还原校验通过 |
| `ipfs-gateway.qzy.xyz` | **整文件网关** | 101.72.219.x；101.75.249.63-64 | `server: Byte-nginx` | 整文件 200，Range 206 |
| `drive.2dland.cn` | Web 客户端 | 116.136.159.41 | nginx + Vite SPA | 首页 200 |
| `sentry.qiecdn.com` | 错误上报 | 116.136.159.35 | — | App 冷启动 TLS SNI |

完整清单见 `02_network_capture/domain-inventory.csv` 与 `host_inventory.json`。

### 2.2 控制面协议（CONFIRMED）

- 传输：**gRPC-Web**，`POST https://drcdn2.2dland.cn/<service>/<method>`，`Content-Type: application/grpc-web+proto`，5 字节帧头（1 字节 flag + 4 字节大端长度）。
- 边缘：`server: envoy`（山西/山东联通线路），TLS 1.3，证书 TrustAsia DV。
- **服务端强制签名**，缺失时逐个报错，实测序列：

```
无头              → grpc-status 3, "missing timestamp"
+timerstamp       → "missing appid"
+appid            → "missing appversion"
+以上三者         → "missing sign"
```

- 签名算法（官方 Web 客户端实现，已复现成功）：

```
ts   = Date.now()
plain = "/" + service + "/" + method + String(ts + appid + appversion)
若带令牌: plain += "Bearer " + token
sign = MD5(plain + appsecret)
请求头: timestamp / appid / appversion / (authorization) / sign
```

`appid = 2dland/web`，`appversion = 202603182100`；`appsecret` 在公开 JS bundle 中，交付件已脱敏（位置见 §B.1）。

- 客户端自报的 OAuth `client_id` 为 **`devDebugger/1.0`**（登录跳转 URL 实测），与签名用的 `appid=2dland/web` 不同——正式版 App 复用了"调试器"客户端标识。

### 2.3 数据面协议（CONFIRMED）

- 上传：`POST https://simple-file-proxy.2dland.cn/int/blocks/{session}/{cid}`，响应 **201**。
- 提交：`POST https://simple-file-proxy.2dland.cn/int/blocks/{session}`，body 为 **CID 字符串 JSON 数组**，响应 200 + 完整文件记录。
- 分片下载：`GET https://slices.qzy.xyz/ipfs/blocks/{a}/{bc}/{cid}?auth_key=...&session=...`，返回**加密块**。
- 整文件下载：`GET https://ipfs-gateway.qzy.xyz/file/{cid}/{uid}/{filename}?auth_key=...`，返回**明文**，支持 Range。
- 全部数据面响应头带多层缓存链：`server: Byte-nginx`、`via: cache05.czmp,cache03.sclfcu01`（两级或以上）。

---

## 3. 上传架构

### 3.1 完整流程（逐步实测）

```
1) PubUserFile/CreateUploadTask  (File{parent,name,mime,size})
   → UploadTask{
       upload_address = https://simple-file-proxy.2dland.cn/int/blocks
       task           = upload_session::<uuid>_<10字符>:ts::<毫秒>
       block_size     = 4194304        (4 MiB)
       block_hash_type= 18             (0x12 = sha2-256)
       block_version  = 1
       block_codec    = 85             (0x55 = raw)
     }
2) 对每个 4 MiB 分块： POST {upload_address}/{task}/{cid}    → 201
3) 提交：              POST {upload_address}/{task}  body=["cid1","cid2",...]  → 200
   → 返回文件记录（含 content_identity）
```

`UploadTask` 中 `access_key/secret_key/bucket/region/endpoint`（字段 10–15）**全部为空** → 客户端不直接对接对象存储，全部经代理。

### 3.2 分片参数（CONFIRMED）

| 参数 | 值 | 说明 |
|---|---|---|
| 分片大小 | **4 194 304 B（4 MiB）** | 服务端下发，非客户端写死 |
| 分片数 | `ceil(size / 4 MiB)` | 10 MiB → 3 块（4+4+2 MiB） |
| 块编码 | `0x55` raw | 无 dag-pb 包装的裸块 |
| 块哈希 | `0x12` sha2-256 | 32 字节摘要 |
| 块 CID | `bafkrei…`（CIDv1/raw） | 可独立复算 |
| 文件根 | 单块：= 块 CID；多块：`bafybei…`（CIDv1/dag-pb） | UnixFS 文件节点 |

### 3.3 实测上传记录

| 文件 | 大小 | 块数 | 块 CID | 根标识 | 耗时 |
|---|---|---|---|---|---|
| hca-kb1.bin | 1 024 B | 1 | `bafkreiaq6bczrpqp6szuzpb6slnhtmjbu52k3p5yik2xl54lcu3avbz44y` | 同块 CID | 0.28 s |
| hca-mb10.bin | 10 485 760 B | 3 | `bafkreiccgyrrt57…`、`bafkreif4uxfuwls…`、`bafkreies3too3ld…` | `bafybeiesgdftdz5o2sauh7ohyszoiqqwqcskkxjeleoc75nbctxboxpck4` | **1.67 s** |

单块 4 MiB 上传耗时 0.39–0.50 s，末块 2 MiB 0.24 s（同一会话串行）。

### 3.4 分片边界验证（CONFIRMED）

10 MiB 文件被切成 **4194304 / 4194304 / 2097152** 三块，与 `block_size` 完全吻合；若分片大小是 1 MiB 或 8 MiB，块数会是 10 或 2，实测为 3 → **分片大小确为 4 MiB**。

### 3.5 服务端去重（秒传）（CONFIRMED）

用已存在的 `content_identity` 直接调 `PubUserFile/Create`：

```
hca-fastdup-1.bin: 200, 耗时 83 ms   （未上传任何字节）
hca-fastdup-2.bin: 200, 耗时 84 ms
```

→ 服务端按内容标识去重，同一 CID 可零传输创建多个文件条目。

反例：直接拿 CID 数组 commit 到**新会话**（未上传块）→ `500 {"error":"failed to make file from blocks: block not found"}`
→ 块按**会话隔离暂存**，跨会话不自动命中；秒传只发生在"文件记录"这一层。

### 3.6 加密与校验

- 上传链路**无客户端侧加密**：块内容即原始字节（CID = sha256(原始块)），服务端未要求额外加密头。
- 服务端校验：块以 CID 命名，内容自校验（哈希不匹配无法入库）。

---

## 4. 下载架构

### 4.1 两条并行通道（CONFIRMED）

**通道 1：整文件网关（明文）**

```
PubUserFile/GetDirectDownloadAddress(identity, path, expire)
 → https://ipfs-gateway.qzy.xyz/file/{content_identity}/{uid}/{filename}?auth_key=...&session=...&filename=...
```

实测（10 MiB）：

| 次数 | HTTP | 字节 | SHA-256 匹配 | server | via | age |
|---|---|---|---|---|---|---|
| 1 | 200 | 10 485 760 | 是 | Byte-nginx | `cache05.czmp,cache03.sclfcu01` | 0 |
| 2 | 200 | 10 485 760 | 是 | Byte-nginx | `cache03.sclfcu01` | 1 |

- `Accept-Ranges: bytes`，`Range: bytes=0-1023` → **206**，`Content-Range: bytes 0-1023/10485760`，内容与原文前 1 KiB 哈希一致。
- `ETag` = 文件的 CID 字符串。
- `Last-Modified: Sun, 04 Jun 1989 15:04:05 GMT`（固定占位值，非真实时间）。
- 第二次下载 `via` 少一跳、`age` 递增 → 命中上层 CDN 缓存。

**通道 2：分片网关（加密，按 DAG 逐块）**

```
PubUserFile/GetSliceDownloadAddress([cid])
 → { identity, download_address: https://slices.qzy.xyz/ipfs/blocks/2/63/{cid}?auth_key=...,
     encrypt: <int>, store_type, expire_at }
```

实测 10 MiB 文件逐块还原：

| 节点 | 字节 | encrypt 键 | ETag | 耗时 |
|---|---|---|---|---|
| 根节点（dag-pb） | 165 | **99** | `bafybeiesgdft…` | — |
| 子块 1 | 4 194 304 | **112** | `bafkreiccgyrr…` | 916 ms |
| 子块 2 | 4 194 304 | **117** | `bafkreif4uxfu…` | 773 ms |
| 子块 3 | 2 097 152 | **110** | `bafkreies3too…` | 472 ms |

拼接结果：10 485 760 B，与原始文件**逐字节相等**（`byte_equal: true`）。

**关键点**：`encrypt` 是**每个节点各自随机**的整数键（99/112/117/110），不是固定常量；解密方式是逐字节 XOR。根节点同样被加密，所以必须先用根键解开 165 字节的 dag-pb 才能拿到子块 CID。

### 4.2 内容寻址结构（独立复算，CONFIRMED）

用自写 DAG 构造器（`11_scripts/dagbuild.py`，不依赖任何 IPFS 库）从 3 个原始块重建 UnixFS 文件节点：

```
PBLink  = Hash(rawCID bytes) + Name("") + Tsize(块大小)
PBNode  = Links(field 2) × 3 后接 Data(field 1)
Data    = UnixFS{Type=2(File), filesize=10485760, blocksizes=[4194304,4194304,2097152]}
CID     = b"\\x01\\x70\\x12\\x20" + sha256(PBNode)
```

复算结果 `bafybeiesgdftdz5o2sauh7ohyszoiqqwqcskkxjeleoc75nbctxboxpck4` 与服务端 `content_identity` **完全一致**，节点长度 165 B 也与线上根节点字节数完全一致。

→ **CID 生成规则已被完整还原，可离线复现。**

### 4.3 分片路径规律

`slices.qzy.xyz/ipfs/blocks/2/63/{cid}`、`.../2/34/{cid}` 中，`2` 与 `63`/`34` 取自 multihash 摘要的前几位十六进制（两级目录扇出）。这暴露了**后端块存储的目录布局**：内容寻址 + 两级目录分桶，典型的本地/分布式文件系统块库结构，而不是对象存储的 key 平铺。

### 4.4 3.15 GiB 对象：单层 DAG，807 链接（CONFIRMED）

对象 `/hca-2026-11-23/CAWD-987-AI.mp4`，3 381 078 505 B，根 `bafybeiebig24qud3ct3xipi6arjyym7i3qlrcjmjbit6wn3manxmfhrhh4`：

| 项 | 实测 |
|---|---|
| 叶子块 | **807** 个（806 × 4 194 304 B + 1 × 469 481 B） |
| 根节点 | 单个 dag-pb，**41 973 B**，807 条 Link |
| 层级 | **单层**，无中间节点 |
| blocksizes 求和 | 3 381 078 505 B = 文件大小 |
| 根 CID 复算 | **MATCH** |
| 全量下载 | 807/807 块校验通过，12.62 MB/s，4 并发，268.0 s |
| 整文件 SHA-256 | `9241bf39ec7f1f5622bbf1d82f50f202030a3fa2ebbde5ecd7999a45e4ea13c1` |

→ 推翻了「超过 696 MiB 必然出现二层 DAG」这一常见假设：该服务只在**节点序列化后超过 1 MiB** 时才分层，807 个链接的节点仅 41.9 KB，因此仍是一层。

### 4.5 播放与下载走不同读出口（CONFIRMED）

| 行为 | 端点 | 形态 |
|---|---|---|
| 播放 | `ipfs-gateway.qzy.xyz`（101.72.219.149/166） | 明文整文件，按需渐进拉流，单连接约 2 MB/s，暂停即停，**不落盘** |
| 下载 | `slices.qzy.xyz`（116.136.20.227/228） | 加密分片，4 并发逐块解密校验，**落盘**到 `files/downloads/` |

端点归因不是靠 IP 表猜的：对 App 进程同时挂 `getaddrinfo` 与 `connect`，播放期间**唯一**成功解析的域名是 `ipfs-gateway.qzy.xyz`，且 `connect` 紧随其后。详见 `12_reports/GB_FILE_DOWNLOAD_AND_PLAYBACK.md`。

---

## 5. 实际存储后端

### 5.1 逐项判定

| 问题 | 判定 | 依据 |
|---|---|---|
| 是否使用 IPFS 数据格式？ | **CONFIRMED** | CIDv1（raw + dag-pb）、UnixFS File 节点、4 MiB 块 + Tsize + blocksizes，全部可离线复算一致 |
| 是否使用 IPFS 的 CID 作为内容标识？ | **CONFIRMED** | `content_identity` 字段 = CID；网关 ETag = CID；客户端详情页直接展示 CID |
| 数据是否进入公共 IPFS 网络？ | **CONTRADICTED** | `delegated-ipfs.dev/routing/v1/providers/{cid}` 返回 `{"Providers":[]}`；对照 CID 返回 29 KB provider 列表 |
| 是否自建内容寻址块存储？ | **STRONGLY_SUPPORTED** | 块 URL 为 `/ipfs/blocks/{a}/{bc}/{cid}` 两级扇出；删除文件后块仍按 CID 可取；网关只认自家 auth_key |
| 上传是否直达对象存储？ | **CONTRADICTED** | `UploadTask` 的 access_key/secret_key/bucket/region/endpoint 全空；无 S3 预签名 URL；全部经 `simple-file-proxy` |
| 下载是否有独立 Gateway？ | **CONFIRMED** | `ipfs-gateway.qzy.xyz` 与 API 域、上传域均不同域 |
| 是否有独立分片服务？ | **CONFIRMED** | `slices.qzy.xyz` 独立域，按 DAG 节点逐块发地址 |
| 是否有 CDN / 多层缓存？ | **CONFIRMED** | 全部数据面响应 `via:` 含 2 个及以上缓存节点；重复下载 `age` 递增 |
| 是否多副本 / 跨区域冗余？ | **UNKNOWN** | 只能观察到"多次下载命中不同缓存节点"，这是 CDN 行为，不能证明源站多副本 |
| 实际持久化介质（磁盘/对象存储/云盘） | **SERVER_SIDE_PERSISTENCE_BACKEND_UNCONFIRMED** | 见 §5.2 |

### 5.2 为什么"物理介质"仍无法确认

已排除的：
- 不是公共 IPFS（DHT 无 provider）；
- 不是客户端直传对象存储（无凭据、无预签名、无 bucket 名）；
- 不是纯缓存（删除文件后 8 分钟内块仍可取，且 `age` 从 0 递增说明有源站回源）。

无法确认的：
- 块存储落地在**自建磁盘集群 / 分布式文件系统 / 私有对象存储**中的哪一种。线索：`slices.qzy.xyz` 的路径是文件系统式两级目录；`ParseFileSliceResponse` 里存在 `wcs_etag` 字段（WCS 兼容存储的 ETag），但我们所有响应中该字段**均为空**，`store_type` 也为空 → 只能记为线索，不能当结论。
- 副本数、纠删码、跨可用区策略：无合法公开接口可查。

结论：**逻辑架构已确认（自建内容寻址块库 + 双层网关 + CDN），物理介质未确认。**

---

## 6. 证据等级汇总

| 结论 | 等级 | 证据编号 |
|---|---|---|
| 客户端为 HalalCloud 1.0.24 (24)，Flutter 静态 BoringSSL | CONFIRMED | E-001, E-002 |
| 控制面 = drcdn2.2dland.cn，gRPC-Web + envoy | CONFIRMED | E-003, E-004 |
| 服务端强制 timestamp/appid/appversion/sign | CONFIRMED | E-005 |
| 上传块大小 4 MiB、codec 0x55、hash 0x12 | CONFIRMED | E-006, E-007 |
| 上传入口 simple-file-proxy.2dland.cn/int/blocks | CONFIRMED | E-007 |
| 块按会话暂存，commit 传 CID 数组 | CONFIRMED | E-008 |
| 单块文件 CID = raw 块 CID；多块 = dag-pb 根 | CONFIRMED | E-009 |
| 根 CID 可离线复算并一致 | CONFIRMED | E-010 |
| 分片下载经 slices.qzy.xyz，逐节点 XOR 加密 | CONFIRMED | E-011 |
| 整文件下载经 ipfs-gateway.qzy.xyz，支持 Range | CONFIRMED | E-012 |
| 数据不在公共 IPFS DHT | CONTRADICTED（对"公共 IPFS"假设） | E-013 |
| 服务端按 content_identity 秒传去重 | CONFIRMED | E-014 |
| 删除文件后块在观察窗口内仍按 CID 可取 | CONFIRMED | E-015 |
| 3.15 GiB 对象 = 807 块 + 单层 dag-pb 根（41 973 B） | CONFIRMED | E-016 |
| 播放走 `ipfs-gateway.qzy.xyz`，下载走 `slices.qzy.xyz` | CONFIRMED | E-017 |
| 播放为按需渐进拉流，不落盘整文件 | STRONGLY_SUPPORTED | E-018 |
| 删除后 ≥490 s 仍可重新取块（不只是 URL 未过期） | CONFIRMED | E-019 |
| 物理持久化介质类型 | UNKNOWN | — |
| 多副本 / 纠删码 | UNKNOWN | — |

---

## 7. 最终判断（逐条回答）

**A. 当前客户端使用什么上传协议？**
gRPC-Web（控制面）+ 自定义 HTTP 块上传（数据面）。控制面走 `drcdn2.2dland.cn`（envoy，强制签名）；数据面按服务端下发的 `upload_address` 以 `POST /int/blocks/{session}/{cid}` 逐块上传 4 MiB 块，最后以 CID 数组 POST 到 `/int/blocks/{session}` 完成提交。`CONFIRMED`

**B. 是否使用 IPFS？用在哪一层？**
用 IPFS 的**数据模型与标识体系**（CIDv1、UnixFS、dag-pb、raw 块、4 MiB 分块 + Tsize），不用公共 IPFS 网络。数据存于自建内容寻址块库，URL 布局 `/ipfs/blocks/{a}/{bc}/{cid}` 与之对应，但只对自家网关开放（auth_key 鉴权）。`数据格式 CONFIRMED / 公共网络 CONTRADICTED`

**C. CID 如何生成？**
块：`CIDv1(raw, sha2-256)`；文件：≤4 MiB 时根 = 块 CID；>4 MiB 时根 = `CIDv1(dag-pb, sha2-256)`，节点内 Links 依次为各 raw 块 CID（Name="", Tsize=块长），Data 为 UnixFS{Type=File, filesize, blocksizes}。已离线复算，与线上完全一致。`CONFIRMED`

**D. 文件实际分片大小？**
**4 194 304 字节（4 MiB）**，末块为余数（10 MiB → 4+4+2 MiB；3.15 GiB → 806×4 MiB + 469 481 B）。`CONFIRMED`

**E. 上传的文件最终写到哪里？**
经 `simple-file-proxy.2dland.cn`（Byte-nginx 边缘）暂存 → 提交后写入后端内容寻址块库（块以 CID 命名，路径两级扇出）。不经过客户端直连的对象存储。`CONFIRMED（写入路径）/ INFERRED（介质类型）`

**F. 实际持久化介质是什么？**
`SERVER_SIDE_PERSISTENCE_BACKEND_UNCONFIRMED`。可确认不是公共 IPFS、不是客户端直传对象存储、不是纯缓存；无法确认是自建磁盘集群 / 分布式文件系统 / 私有对象存储中的哪一种。`UNKNOWN`

**G. 下载是否经过独立 Gateway、分片服务器和 CDN？**
是，三者都有且互相独立：`ipfs-gateway.qzy.xyz`（整文件明文、Range 206）、`slices.qzy.xyz`（逐块加密下发）、以及 `via:` 头所示的多层 CDN 缓存。`CONFIRMED`

**H. 能否证明多副本与持久化？**
持久化：部分可证——删除所有引用后 8 分钟内块仍可按 CID 取回，且两次下载 `age` 从 0 → 递增、`via` 变化，说明存在源站与缓存两级。
多副本：不能证明。不同网络/不同时间命中不同缓存节点只能说明 CDN 调度，不构成副本证据。`部分 CONFIRMED / 多副本 UNKNOWN`

**I. 哪些结论还不能确定，缺什么证据？**
1) 物理介质类型 —— 缺服务端配置或公开文档，或可验证的对象存储特征（如 S3 响应头、bucket 名）。
2) 副本/纠删码策略 —— 缺合法可控的节点故障实验。
3) App 侧上传/下载的**完整 HTTP 明文** —— 缺 TLS 解密能力（Flutter 静态 BoringSSL + targetSdk 36 不信任用户 CA + 设备无 root）。
4) `store_type` / `wcs_etag` 的真实取值 —— 我们的文件全部为空，可能需要特定存储池或老数据才填充。

**J. 要复刻这套架构，必须实现哪些组件？**
1) 控制面 gRPC-Web 服务（签名校验：timestamp/appid/appversion/sign）+ 元数据存储（文件树 + content_identity）
2) 上传代理：会话管理（`upload_session::`）、按 CID 接收并校验块、会话内块清单提交、文件记录生成
3) 内容寻址块库：4 MiB 块 + sha2-256 + 两级目录扇出
4) DAG 组装器：UnixFS dag-pb 根节点（Links/Tsize/blocksizes）
5) 分片网关：按 DAG 逐节点下发 + 每节点随机 XOR 键（`encrypt` 字段）
6) 整文件网关：DAG 遍历拼接 + HTTP Range
7) 签名 URL 体系：`auth_key`（含过期时间、用户标识、HMAC）+ `session`
8) CDN 多层缓存
9) 客户端：本地元数据库 + 传输任务状态机（暂停/恢复、下载线程数）

---

## 8. 与"历史结论"的差异（明确标注）

本轮**没有**使用历史资料，但为便于读者对照，以下是本轮实测与常见历史说法的差异点：

| 历史常见说法 | 本轮实测 |
|---|---|
| 上传域名硬编码在客户端 | 客户端不含上传域名，由 `CreateUploadTask` 下发（`simple-file-proxy.2dland.cn`） |
| XOR 密钥固定 0x75 | **错**：每个节点随机（本轮实测 99/112/117/110/52） |
| 分片大小 1 MiB | **错**：4 MiB |
| 公共 IPFS 可访问 | **错**：DHT 无 provider |
| >696 MiB 必然出现二层 DAG（174 扇出） | **错**：3.15 GiB 仍是单层，807 条链接挂在 41.9 KB 的根节点上；该实现只在节点 >1 MiB 时才分层 |
| 播放与下载走同一条通道 | **错**：播放走明文网关（Range），下载走加密分片网关（逐块 XOR） |
| 登录走腾讯防水墙 | 本轮为 OAuth 设备码（`client_id=devDebugger/1.0`） |

（以上"历史说法"仅作对照说明，未作为任何结论的依据。）

---

## 9. 未完成项与障碍（如实记录）

1. **App 侧数据面连接已部分捕获，但 HTTP 明文仍不可得**。
   已完成的：冷启动/登录/列表/详情/下载/播放的连接级观测——App 内 50 MiB 下载出口 `116.136.20.228:443`（`slices.qzy.xyz`）、并发峰值 4；3.15 GiB 播放出口 `101.72.219.149/166:443`（`ipfs-gateway.qzy.xyz`，DNS+connect 双重归因）；下载产物 `04_download/app_downloaded_kb1.bin`、`app_downloaded_mb50.bin` 与源文件 SHA-256 一致。
   障碍：Flutter 静态 BoringSSL + targetSdk 36（不信任用户 CA）+ 无 root，因此**看不到请求头/Range 头原文**，只能从端点、字节数、时间序列推断。未采用提权或装系统证书方案。

2. **TLS 明文不可得**。原因同上；这是本轮唯一硬性技术障碍，已在所有涉及请求头原文的结论处标注推断等级。

3. **大文件覆盖到 3.15 GiB，但未做「上传」侧大文件**。账号上行额度 10 GB/天，3.15 GiB 对象只做了读取侧（下载 + 播放）取证；上传侧最大做到 50 MiB（13 块）。DAG 分层行为已由 3.15 GiB 单层结构直接推翻旧假设，无需再靠上传大文件验证。

---

## 10. 附录：证据文件索引

- 逐条证据映射：`12_reports/evidence-index.md`
- 3.15 GiB 专项：`12_reports/GB_FILE_DOWNLOAD_AND_PLAYBACK.md`
- 链路细节：`12_reports/upload-flow.md`、`12_reports/download-flow.md`
- 后端判定：`12_reports/storage-backend-evidence.md`
- 删除窗口：`12_reports/persistence-test-results.md`
- 完整性校验：根目录 `MANIFEST.json`（由 `11_scripts/build_manifest.py` 计算，全部 SHA-256 非手填）
