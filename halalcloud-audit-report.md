# 清真云（HalalCloud）运行方式与功能验证审查报告

> **报告用途**：为 MyDrive（`tuo8/wp`）评估清真云能否作为存储后端，并如实记录其当前架构、API、上传下载链路与存储机制。
> **审查日期**：2026-11-22 20:45 — 22:40（GMT+8，UTC+8）
> **审查方式**：官方源码审查 + 官网/仓库资料 + **本人在授权账号内的真实上传/下载/CID 校验**
> **审查者**：WorkBuddy（自动化审计代理）；账号、令牌、密钥全部打码为 `[REDACTED]`
> **测试账号**：本人合法账号（`[REDACTED]`），额度 12 TiB，已用约 4.90 TiB
> **报告版本**：v1.0（首版）

---

## 0. 边界声明（本次实际遵守的约束）

| 约束 | 执行情况 |
|---|---|
| 只使用本人账号与自制测试文件 | ✅ 6 个测试文件全部由脚本随机生成（`audit/gen_files.py`，种子固定） |
| 不删除/修改既有文件 | ✅ 只删除**本次自建**的 3 个测试文件（video / 1m / ttl 小文件），删除前已留存哈希与签名 URL；其余测试文件（small、4m、20m、png、dedup）仍保留在账号根目录，用户可自行清理 |
| 无付费、无压测、无并发、无枚举 | ✅ 全程串行，单文件最大 20 MiB，未发起任何批量/并发请求 |
| 不绕过鉴权/证书固定 | ✅ 未做任何 MITM 解密；上传下载全部走官方公开 API 与官方下发的签名 URL |
| 敏感信息打码 | ✅ Token / auth_key / session / 用户 ID / WebDAV 口令 / appsecret 均以 `[REDACTED]` 呈现 |

> 说明：本次是**协议层直连**（用官方 Web 客户端同一套签名算法调用官方 gRPC-Web 接口），不等同于"用第三方客户端替代官方 App"。所有文件操作都可在官方 Web 端看到同样结果。

---

## 1. 执行摘要

### 1.1 一句话结论

**清真云是一个"IPFS 内容寻址 + 自建块存储 + 商业 CDN 加速"的混合架构：文件确实按 IPFS UnixFS 规则切片并生成真实 CID（可离线复算验证），但数据只存在清真云自建的私有块存储里，由火山引擎 CLB 与百度 CDN 承载，未接入公共 IPFS 网络（DHT 无任何 Provider 记录）。**

### 1.2 服务当前状态

| 项 | 结论 | 证据等级 |
|---|---|---|
| 官网声明 | 已进入**封存阶段（Archive Mode）**："已停止主动开发与维护……服务可用性、数据完整性及功能稳定性不再作出保证" | **C**（官网 2dland.cn 原文） |
| 登录/API | 仍可用；OAuth refresh 正常换发令牌，抽查的 20+ 个关键 RPC 全部返回正常 | **A**（本次实测，非 113 个全量） |
| 上传 | 可用，实测 6/6 成功 | **A** |
| 下载 | 可用，实测 6/6 成功，SHA-256 全部一致 | **A** |
| WebDAV | 可用（PROPFIND 返回 207） | **A** |
| 分享 | API 可创建（返回 share identity） | **A**（创建）/ **未验证**（匿名下载页行为） |
| 视频转码 | 未实测（无合规自制视频、本机无 ffmpeg）；源码/前端包存在两套转码方案证据 | **B/D** |
| 新用户注册 | 页面存在但需腾讯防水墙验证码，未注册 | **未验证** |
| 客服/工单 | `PubTicket` 服务存在且可列取 | **A**（接口存在）/ 未提交工单 |

### 1.3 结论可信度

- **架构判定（混合、非公共 IPFS）**：高。三条独立证据链（CID 可复算、DHT 无 Provider、数据面域名与 CDN 头）互相印证。
- **功能矩阵**：中高。API 与 Web 端功能已实测；Android 端逐项点击未全部执行（已标注）。
- **容量/长期保留承诺**：低。官方已声明"不再保证数据完整性"，且额度是**逻辑额度**（12 TiB），无任何 SLA 或保留期条款。

### 1.4 是否适合 MyDrive 集成

**可以接入，但不能作为唯一主存储。** 推荐"方案 E：可选外部来源 + Telegram 兜底"，理由见 §10。

### 1.5 三个最大风险

1. **服务已封存**：官方明确不保证可用性与数据完整性，随时可能停服且无赔偿条款。
2. **删除不彻底**：实测删除后元数据立即失效，但**旧签名 URL 仍能取回完整明文**（受控计时：删除后 0/30/60/120/240 秒连续 5 次取回，全部 200 且哈希一致，窗口仍在持续），说明物理块/边缘缓存未同步清除。
3. **接入依赖私有签名体系**：上传需要服务端下发的 `upload_session`，下载需要 `auth_key`（服务端签发、含 MD5 签名），任何一环变更都会让第三方集成失效；且 `appsecret` 硬编码在 Web 前端包里，属于可被随时轮换的共享密钥。

### 1.6 阶段完成情况（对应任务书 §10）

| 阶段 | 内容 | 状态 | 说明 |
|---|---|---|---|
| 阶段 1 | 官网/公告/GitHub 全仓库/源码与 License | ✅ 完成 | 9 个仓库全量元数据 + 6 个仓库源码解包审查；yuque 文档空间 JS 渲染，正文未获取 |
| 阶段 2 | 客户端只读功能盘点 | ⚠️ 部分完成 | Web/API 侧完整；Android 端因设备断开未逐项点击 |
| 阶段 3 | 小文件上传/下载/哈希/CID | ✅ 完成 | 6 个文件，全链路实测 |
| 阶段 4 | 1 MB / 4 MB / 20 MB 分片行为 | ✅ 完成 | 20 MB=5 片；未做断点续传中断测试（无安全手段模拟） |
| 阶段 5 | IPFS/Gateway/Provider/Pin | ⚠️ 部分完成 | CID 复算、DHT provider、私有网关全测；**公共网关因本机网络不可达未验证** |
| 阶段 6 | 媒体与分享 | ⚠️ 部分完成 | 分享创建 ✅、图片原图 ✅、Range ✅；视频预览/转码未测；匿名分享下载未测 |
| 阶段 7 | MyDrive 适配评估 | ✅ 完成 | 方案 A–E 评分 + 推荐 |
| 阶段 8 | 复核与报告 | ✅ 完成 | 敏感信息已打码；未完成项已逐条列出，无编造 |

---

## 2. 当前实际运行架构

### 2.1 架构图（实线=本次实测证实，虚线=源码/推断）

```mermaid
flowchart TB
  subgraph Client["客户端"]
    W["Web SPA<br/>drive.2dland.cn"]
    A["Android 1.0.24<br/>Flutter"]
    D["Desktop / Qt<br/>(WIP)"]
    T["第三方<br/>WebDAV / SDK"]
  end

  subgraph Edge["接入层（实测 DNS + 响应头）"]
    EO["腾讯 EdgeOne<br/>*.eo.dnse2.com"]
    BD["百度云加速<br/>*.a.bdydns.com"]
    VOL["火山引擎 CLB<br/>*.volcgslb.com"]
    BCDN["百度 CDN (bdcdn)<br/>X-Bdcdn-Cache-Status"]
  end

  subgraph Core["清真云自建服务"]
    API["gRPC-Web API<br/>grpcuserapi.2dland.cn<br/>108 启用 RPC / 62 REST"]
    UP["块上传代理<br/>simple-file-proxy.2dland.cn/int/blocks"]
    SL["分片下载<br/>slices.qzy.xyz/ipfs/blocks/..."]
    GW["私有 IPFS 网关<br/>ipfs-gateway.qzy.xyz/file/..."]
    DAV["WebDAV<br/>dav.2dland.cn (117.21.184.135)"]
    META[("元数据<br/>用户/文件/切片索引<br/>实现未验证")]
    STORE[("私有块存储<br/>按 CID 寻址<br/>Haystack? 未证实")]
  end

  subgraph Media["媒体（源码/前端包证据）"]
    MM["halal-multimedia<br/>自建转码 FFmpeg+SLS"]
    VOD["火山引擎 VOD<br/>vod.volcengineapi.com"]
  end

  subgraph Public["公共 IPFS 网络"]
    DHT["DHT / 公共网关"]
  end

  W -->|HTTPS| BD --> API
  A -->|TLS1.3 gRPC-Web| EO --> API
  T -->|WebDAV| DAV --> API
  API --> META
  W -->|上传字节| VOL --> UP --> STORE
  UP --> META
  W -->|下载字节| BCDN --> VOL --> SL --> STORE
  W -->|整文件/预览| BCDN --> GW --> STORE
  API -. 签发 auth_key .-> SL
  API -. 签发 auth_key .-> GW
  META -. 记录 CID/切片 .-> STORE
  MM -. 转码 .-> VOD
  GW -. 不宣告 Provider（实测 0 个） .-> DHT
  STORE -. 未证实是否 pin 到公共网络 .-> DHT
```

### 2.2 域名与基础设施（全部为本次 DNS/HTTP 实测）

| 域名 | 角色 | DNS 证据（CNAME → A） | 证据等级 |
|---|---|---|---|
| `grpcuserapi.2dland.cn` | 控制面 API（gRPC-Web / REST） | `→ grpcuserapi.2dland.cn.eo.dnse2.com`（腾讯 EdgeOne）→ `119.188.175.5` | **A** |
| `simple-file-proxy.2dland.cn` | **上传数据面**（`/int/blocks`） | `→ *.volcgslb.com`（火山引擎）→ `60.221.21.154/163` | **A** |
| `slices.qzy.xyz` | **分片下载**（`/ipfs/blocks/2/{xx}/{cid}`） | `→ slices.qzy.xyz.volcgslb.com` → `139.215.225.85/88` | **A** |
| `ipfs-gateway.qzy.xyz` | **整文件下载 / 预览**（`/file/{cid}/{uid}/{name}`） | `→ *.volcgslb.com` → `60.221.21.154/163`（与上传同组 IP） | **A** |
| `dav.2dland.cn` | WebDAV | `→ 2dland.cn` → `117.21.184.135`（直连，无 CDN） | **A** |
| `drive.2dland.cn` | Web 前端 | `→ *.a.bdydns.com`（百度云加速）→ `116.136.159.41` | **A** |
| `openapi.2dland.cn` | SDK 示例所指 REST 入口 | `→ *.a.bdydns.com`；根路径 404 | **A**（存在）/ 用途**未验证** |
| `ucenter.2dland.cn` | 账号中心 / OAuth | 200 | **A** |

**响应头证据（下载）**：

```
Server: Byte-nginx
Etag: "bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey"
X-Bdcdn-Cache-Status: TCP_HIT / TCP_MISS,TCP_MISS
Via: cache08.hdcu / cache48.sxmp,cache07.jlcccu13
X-Tt-Trace-Tag: id=5
X-Response-Cinfo: 116.95.146.173
Accept-Ranges: bytes
```

→ **`Byte-nginx` + `X-Tt-Trace-Tag` 为字节/火山系 nginx；`X-Bdcdn-*` 为百度 CDN 命中状态；`Etag` 直接等于 CID**（CDN 以 CID 为缓存键）。

### 2.3 与官方声明对照

- 官网封存公告：`https://2dland.cn/` → "清真云已进入封存阶段 / 已停止主动开发与维护 / 服务可用性、数据完整性及功能稳定性不再作出保证"（**C**）
- GitHub 仓库仍在更新（`stellarplayer-resourceauth-go` 2026-11-14、`halalcloud-qt` 2026-08-27），但那是**周边项目**，不等于云盘服务在维护（**B**）
- 结论：**"App 还能打开" ≠ "服务受维护" ≠ "数据有保障"** —— 本次实测完全支持这一判断。

---

## 3. 上传链路与下载链路（实测）

### 3.1 上传链路

```mermaid
sequenceDiagram
  participant C as 客户端
  participant API as grpcuserapi.2dland.cn
  participant UP as simple-file-proxy.2dland.cn
  participant S as 块存储

  C->>API: POST /v6.services.pub.PubUserFile/CreateUploadTask<br/>{path, size}
  API-->>C: upload_address, task(upload_session::…), block_size=4194304,<br/>hash_type=18(0x12 sha256), codec=85(0x55 raw), version=1
  loop 每 4 MiB 一片
    C->>C: 计算 CIDv1(raw, sha256)
    C->>UP: GET /int/blocks/{task}/{cid}  ← 秒传探测（返回 true 即跳过）
    C->>UP: POST /int/blocks/{task}/{cid}  body=原始字节
    UP-->>C: 201 Created
  end
  C->>UP: POST /int/blocks/{task}  body=JSON[CID...]
  UP-->>C: 200 {user_identity, parent, name, size, content_identity}
  Note over API,S: 文件根 CID 由服务端按 UnixFS 规则用切片 CID 现算，非客户端上传
```

**实测请求/响应（`hc-audit-20m-*.bin`，20 MiB，5 片）**

| 步骤 | 方法/路径 | 结果 |
|---|---|---|
| 建任务 | `POST https://grpcuserapi.2dland.cn/v6.services.pub.PubUserFile/CreateUploadTask` | 0 OK，返回 `upload_address=https://simple-file-proxy.2dland.cn/int/blocks`，`block_size=4194304`，`block_codec=85`，`block_hash_type=18` |
| 传片 ×5 | `POST https://simple-file-proxy.2dland.cn/int/blocks/{task}/{cid}` | 每片 201，单片刻 0.3–1.2 s |
| 提交 | `POST https://simple-file-proxy.2dland.cn/int/blocks/{task}`，body=`["bafkrei…","bafkrei…",…]` | 200，返回 `content_identity=bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4` |

**关键判定**

| 问题 | 结论 | 证据等级 |
|---|---|---|
| 是否先申请上传凭证 | 是，`CreateUploadTask` 下发 `upload_session` | **A** |
| 分片大小 | **4 MiB（4 194 304 B）**，服务端下发；SDK 常量 `IpfsDefaultBlockSize = 4*1024*1024` 一致 | **A / B** |
| 编号起点 | 无显式编号，**按每片自身的 CID 寻址** | **A** |
| 是否预签名 URL | 否。上传地址是固定的 `/int/blocks`，靠 `task` 会话串 + CID 定位 | **A** |
| 文件字节发往哪里 | `simple-file-proxy.2dland.cn`（火山引擎 CLB 后自建服务），**不经过 API 域名** | **A** |
| 是否直传 IPFS 网关/对象存储 | 否。无 S3/OSS/BOS 预签名，`UploadTask.access_key/bucket/endpoint` 字段全部为空 | **A** |
| 秒传/去重 | 有。SDK 先 `GET /{task}/{cid}`，返回 `true` 即跳过上传；同字节不同文件名实测得到同一 `content_identity` | **A / B** |

### 3.2 下载链路（两条并存）

```mermaid
flowchart LR
  A["客户端/浏览器"] -->|1. GetFileSlicesDownloadInfo| B["API"]
  B -->|2. 返回 slices[]: download_address + encrypt| A
  A -->|3. GET 每片| C["百度 CDN → 火山 CLB → slices.qzy.xyz"]
  C -->|4. XOR(encrypt) 解码 + sha256 校验| A

  A2["客户端/浏览器"] -->|1'. GetDirectDownloadAddress| B
  B -->|2'. 返回 /file/{cid}/{uid}/{name}?auth_key| A2
  A2 -->|3'. GET 整文件（支持 Range）| D["百度 CDN → ipfs-gateway.qzy.xyz"]
  D -->|4'. 网关侧解密，直接返回明文| A2
```

| 通道 | 域名 | 返回内容 | 是否加密 | 证据 |
|---|---|---|---|---|
| 分片通道 | `slices.qzy.xyz/ipfs/blocks/2/{hex2}/{cid}` | 4 MiB 原始块 | **是**：逐字节 XOR，密钥 = JSON 里 `encrypt` 字段（每片随机，0=明文） | **A** |
| 整文件通道 | `ipfs-gateway.qzy.xyz/file/{cid}/{user}/{name}` | 完整明文 | 否（网关内部解密） | **A** |
| WebDAV | `dav.2dland.cn` | 明文 | 否 | **A**（207 响应） |

**分片密钥实测**（同一 20 MiB 文件的 5 片）：

| 片序 | 切片 CID | `encrypt` |
|---|---|---|
| 0 | `bafkreifsejtnmtah77izvplaqhqjna3pcndojimw4izj7bsdcxeomvdbw4` | 98 |
| 1 | `bafkreicq4zdasdolvsl27f7susuedqzblddg3mnmtznceqsbadtmzvhusi` | 117 |
| 2 | `bafkreihe64uqk2optkacn5kdelt2mf37z7xfxgot5c7tyetbornpibvcua` | 99 |
| 3 | `bafkreidwfog22hbjtnvzwifykxw2gx6d2255byvyquxeriwfpvllptrjqe` | 106 |
| 4 | `bafkreie6jqumr3tldzze7ni7iv5eciqfmvzzcbnaf46ddum6e3nms3k36e` | 51 |

> **官方 Web 客户端源码级确认**（`taskDownload-*.js`，函数 `Wh`）：
> ```js
> let s = await (await fetch(url)).blob();
> let c = await qh(s);                    // → Uint8Array
> for (let e = 0; e < c.length; e++) c[e] ^= r;   // r 即 slices[i].encrypt
> ```
> 另外 `Kh()` 用 `sha256(解码后字节)` 与 CID 的 multihash 比对，**失败则丢弃重试**（最多 10 次）。证据等级 **B**，并与本次实测互相印证：6/6 文件按**各自分片的 `encrypt`** 逐个解 XOR 后，SHA-256 与本地原件完全一致（其中 20 MiB 文件 5 片密钥各不相同：98/117/99/106/51）。

**"是否经过清真云服务器"的判定**

- 控制面（签名、元数据、下载地址签发）：**全部经过** `grpcuserapi.2dland.cn`。
- 数据面（字节流）：**经过清真云自建服务** `simple-file-proxy` / `slices` / `ipfs-gateway`，前面套百度 CDN。
- **没有任何一段直连第三方对象存储或公共 IPFS 网关**。

---

## 4. 底层存储结论矩阵

| 机制 | 是否存在 | 证据 | 证据等级 | 置信度 | 备注 |
|---|---|---|---|---|---|
| 公开 IPFS 网络承载 | **否** | `delegated-ipfs.dev` 查 3 个 CID 全部 `{"Providers":[]}`；`trustless-gateway.link` 522/525；`ipfs.io`/`dweb.link` 本网络不可达（未验证项） | **A（DHT）/ 未验证（网关）** | 高 | 无 Provider 宣告 = 公共网络拿不到 |
| 自建 IPFS 节点/网关 | **是（网关层）** | `ipfs-gateway.qzy.xyz`、`slices.qzy.xyz` 按 `/ipfs/blocks/...` 与 `/file/{cid}` 提供内容；ETag=CID | **A** | 高 | 只是"以 IPFS 命名/寻址方式对外提供"，非标准 libp2p 节点 |
| IPFS Cluster | 无证据 | 9 个官方仓库无 cluster 依赖；无 8094/9094 类接口 | **未验证** | 中 | 不能断言不存在，只能说未见 |
| Pinning（持续保存保证） | 无法证明 | 无公开 pin 接口；无 pin 状态；DHT 无 Provider | **未验证** | — | 私有 pin 完全可能，只是不可观测 |
| 公共 Gateway | 无 | 同上 | **A（DHT）** | 高 | 公共网关读不到（能读的只有自家网关且需 auth_key） |
| 私有 Gateway | **是** | 未签名 `GET /ipfs/{cid}` → 403；带 `auth_key` → 200 | **A** | 高 | `auth_key` 由服务端签发，含 MD5 签名 |
| S3/BOS/OSS 等对象存储 | **未见证据** | `UploadTask` 的 `access_key/secret_key/bucket/region/endpoint` 全空；无预签名 PUT；`BaiduBce` 仓库仅说明"曾是客户端组件" | **B/未验证** | 低 | 不排除用对象存储做冷备，但**主链路不是** |
| 自建文件服务器 | **是** | `simple-file-proxy`、`slices`、`ipfs-gateway` 均为自建（`Byte-nginx`） | **A** | 高 | 块按 CID 寻址，4 MiB 对齐 |
| CDN 缓存 | **是** | `X-Bdcdn-Cache-Status: TCP_HIT/TCP_MISS`、`Via: cache08.hdcu` | **A** | 高 | 百度 CDN 在最前，火山 CLB 在中间 |
| 数据库/元数据服务 | **是（存在）** | 列表游标 `base64(JSON)` 带 `skip/limit/create_time`；`Task=lock__user_operation__*`；秒级最终一致性 | **A（行为）** | 高 | 具体引擎（MySQL/HBase/Redis）**未验证** |
| Telegram 等第三方备份 | 无证据 | 源码/流量均无 | **未验证** | — | 与"IPFS+CDN"完全无关 |
| Haystack（`kagami-storage`） | **历史系统** | 官方 README："曾于 2016-2022 年间作为 **6pan.cn** 的存储系统所使用" | **C** | 高 | 无任何证据表明用于当前清真云 |

### 4.1 CID 真实性核验（最关键的一节）

**结论：CID 是真实的内容寻址标识，可用文件字节离线复算出来。**

| 文件 | 服务端 `content_identity` | 本地独立复算 | 是否一致 |
|---|---|---|---|
| `hc-test-small.txt`（4 157 B） | `bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey` | `cidv1(raw, sha256(bytes))` 直接计算 | ✅ 完全一致 |
| `hc-test-1m.bin`（1 MiB） | `bafkreigoiq5uaswpwxz7pbzydwxl264dosbga3y35zm2eppfszo5rri7zq` | 同上（单块） | ✅ |
| `hc-test-4m.bin`（4 MiB，正好 1 片） | `bafkreigmhxtlmiqbovbmrhqwrfuihfcgukpwbx3titjrhs66y4fvbpxc7y` | 同上 | ✅ |
| `hc-test-20m.bin`（20 MiB = 5 片） | `bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4` | `ipfs-unixfs-importer`，`cidVersion:1, rawLeaves:true, chunker:4 MiB` | ✅ 完全一致 |
| 同上，但把 chunker 改成 IPFS 默认 **1 MiB** | — | `bafybeihx4ftfphmgo52mfbe43pmpkpdblo3o37l3gt6gxpc54umxfpr5ri` | ❌ 不同 → 反证线上是 **4 MiB 分片** |

> 复算脚本：`audit/cid_nodes.mjs`（依赖 `ipfs-unixfs-importer@17`、`@ipld/dag-pb`）。
> 由于 CIDv1 根是对整个 DAG 的 sha256，**根一致即等价于 DAG 结构、叶节点顺序、编码方式全部一致**——这比"看字符串像不像"强得多。

**因此可以确定**：
1. 4 MiB 切片 = `CIDv1(raw, sha256)`；
2. 文件根 = `UnixFS(dag-pb)`，由服务端用切片 CID 现算；
3. 下载后可用 `sha256(bytes)` 独立验证是否等于 CID —— 官方 Web 客户端正是这么做的（`Kh()`）。

### 4.2 删除语义（实测，含"删了但没真删"）

| 阶段 | 列表可见 | API 签发下载地址 | 删除前保存的旧签名 URL | 证据 |
|---|---|---|---|---|
| 删除前 | 是 | 0 OK | 200，SHA-256 一致 | **A** |
| `Trash`（进回收站） | 否 | **grpc 5 `file not found`** | **仍 200，内容完全一致** | **A** |
| `DeleteTrash`（永久删除） | 否 | **grpc 5 `file not found`** | **仍 200，内容完全一致** | **A** |

**受控计时实验**（`audit/delete_window.py`，单文件 4 157 B，永久删除后每 30–120 s 采样一次）：

| 采样点 | 直链状态 | 直链字节 | 直链 SHA-256 | 分片状态 | 分片 SHA-256 | API 签发 |
|---|---|---|---|---|---|---|
| 删除前 | 200 | 4 157 | ✅ | 200 | ✅ | 0 |
| `Trash` +2 s | 200 | 4 157 | ✅ | 200 | ✅ | **5** |
| `DeleteTrash` +3.8 s | 200 | 4 157 | ✅ | 200 | ✅ | **5** |
| +30 s | 200 | 4 157 | ✅ | 200 | ✅ | **5** |
| +60 s | 200 | 4 157 | ✅ | 200 | ✅ | **5** |
| +120 s | 200 | 4 157 | ✅ | 200 | ✅ | **5** |
| +240 s | 200 | 4 157 | ✅ | 200 | ✅ | **5** |

**解读（务必注意证据边界）**：
- **元数据删除是立即生效的**（API 拒绝再签发任何地址）。
- **物理块/边缘缓存没有同步失效**：删除后 240 秒的受控计时中（0/30/60/120/240 s 采样），旧 URL 每次都 200、字节数一致、SHA-256 一致，窗口在观测结束时仍未关闭。
- 这**不能**证明"永远删不掉"，只证明"删除不是原子的、存在可见窗口"。判定为：**删除 = 移除用户索引；块回收是异步的、不可观测的**（**A** 现象 / **D** 机制推断）。
- 对比：回收站列表 `ListTrash` 分页 100 条内未出现目标条目（可能被排到更深页），**回收站可用性部分验证**。

### 4.3 一致性与延迟（实测）

- 上传 commit 返回 200 后，**列表立即可见性有秒级延迟**：连续 6 次上传后立即 `List` 均未命中，1–3 s 后重试才出现（`audit/semantics_test.py` 的 `list_visible_after_s` 字段）。
- 判定：**最终一致性**，非强一致（**A**）。客户端做"上传完立刻刷新"需要考虑这点。

---

## 5. 客户端功能完整矩阵

> 基线：Web 端 `drive.2dland.cn`（实测）+ Android `1.0.24`（静态/接口）+ 官方 API。**Android 端未逐项点击**，凡未实测均标 `未测试`。

### 5.1 账户与认证

| 功能 | 状态 | 平台 | 测试方法/证据 | 等级 |
|---|---|---|---|---|
| 密码登录 | 部分可用（强制人机验证） | Web/Android | `Login` 现要求腾讯防水墙 `captcha.ticket`；本次以 `{}`、空 ticket、null 三种形态尝试均返回 `missing captcha ticket`，**未完成密码登录**（不绕过），改用 refresh 续期 | **A** |
| 验证码登录 | 存在（未用） | 全平台 | `SendSmsVerifyCode` / `SendSmsVerifyCodeNotUser` 负责发码，登录统一走 `Login`（无独立短信登录方法） | **B** |
| Token 刷新 | 可用 | 全平台 | `OauthAuthorization/RefreshToken`（`client_id + refresh_token + grant_type`）实测换发成功 | **A** |
| Token 有效期 | access 2 h；refresh 长效 | 全平台 | JWT `exp` 解析 | **A** |
| 设备授权 | 存在 | 客户端 | `DeviceAuthorization` RPC | **B** |
| 多设备登录 | 存在 | 全平台 | `Token.device`、`last_login_device` 字段 | **B** |
| 改密/找回 | 存在（未执行） | 全平台 | `ChangePassword` / `ResetPassword` | **B** |
| 注销账户 | 存在（未执行） | — | `PubUser/Delete` | **B** |
| 额度展示 | 可用 | Web/API | `GetStatisticsAndQuota` 返回 12 TiB/已用 | **A** |
| 会员/付费 | 存在（未购买） | Web | `user_center_uri` 跳转；`ExtendQuota` RPC | **B** |
| 邀请/积分 | 未见证据 | — | 无对应 RPC | **未验证** |

### 5.2 文件管理

| 功能 | 状态 | 证据 | 等级 |
|---|---|---|---|
| 列表/目录 | 可用 | `List` + 游标分页（`list_info.token` = base64 JSON，`order_by` 支持 `create_ts`） | **A** |
| 新建文件夹 | 可用 | `Create`（`File{direcotries,...}`，字段名服务端拼写错误保留原样） | **B** |
| 重命名/移动/复制 | 存在 | `Rename` / `Move` / `Copy` | **B**（未逐一实测） |
| 单文件上传 | 可用 | 6/6 成功 | **A** |
| 批量上传 | 可用（客户端侧循环） | Web 端 `taskUpload` 模块 | **B** |
| 文件夹上传 | 存在 | Web 端支持拖拽目录 | **未测试** |
| 下载（分片/整文件） | 可用 | 6/6 SHA-256 一致 | **A** |
| 删除 → 回收站 → 永久删除 | 可用 | `Trash` / `ListTrash` / `DeleteTrash` 实测 | **A** |
| 搜索 | 存在 | `Search` RPC | **B** |
| 收藏/星标 | 字段存在，接口未找到 | `File.starred`（字段 17）；62 条 REST + 108 RPC 中**未见独立 Star/Unstar 方法**，可能复用 `Create` 的 flag | **未验证** |
| 最近使用 | 存在 | `ListRecentUpdatedFiles` | **B** |
| 文件详情（大小/时间/CID） | 可用 | `List`/`Get` 返回 `content_identity`、`nodes`、`store_type` | **A** |
| 秒传/去重 | 可用 | 同字节不同名 → 同 CID；SDK 先 GET 探测 | **A/B** |
| 同名文件处理 | 客户端策略 | Web 端提示覆盖/重命名 | **未测试** |
| 文件版本 | 存在 | `File.version` | **未验证** |
| 分享链接 | 可用（创建） | `PubFileShare/Create` 返回 identity；分享页是 SPA | **A**（创建）/ **未验证**（匿名下载） |
| 提取码 | 存在 | `FileShare.password` / `has_password` | **B** |
| 分享有效期 | 字段存在，语义**未验证** | `FileShare.lifetime`；本次传 1，返回的 `expire_ts` 仅比创建晚数秒（疑似秒级），**不作"1 天/1 小时"解读** | **A（字段）/ 未验证（单位）** |
| 分享点赞/投诉/转存 | 存在 | `Like` / `Dislike` / `ComplaintShare` / `Save` | **B** |

### 5.3 媒体能力

| 功能 | 状态 | 证据 | 等级 |
|---|---|---|---|
| 图片预览 | 可用（原图） | 网关直出原图；未发现独立缩略图接口 | **A（原图）/ 未验证（缩略图）** |
| 视频播放 | 未测试 | 无合规自制视频（本机无 ffmpeg） | **未测试** |
| Range/断点续传 | 可用 | 6/6 返回 `206 Partial Content` + `Accept-Ranges: bytes` | **A** |
| 服务端转码 | 存在，双路径 | ① 前端包内含 `vod.volcengineapi.com` / `vod-license-m.volccdn.com`（火山 VOD）；② `halal-multimedia` README 自称"2dland.cn 线上使用的转码服务（生产级），依赖 FFMPEG 和 SLS" | **B/D** |
| 流媒体形式（HLS/DASH） | 未验证 | 需真实视频 | **未验证** |
| 字幕/元数据 | 未验证 | — | **未验证** |
| 播放时数据来源 | 推测为 `ipfs-gateway.qzy.xyz` + CDN | 与下载同一网关 | **D** |

### 5.4 体验与边界

| 功能 | 状态 | 证据 | 等级 |
|---|---|---|---|
| 离线下载（BT/磁力/HTTP） | 存在 | `PubOfflineTask`（Parse/Add/List/Delete）；APK 字符串 "Supported IPFS and HTTP(S) links"、"ipfs://"、"URL or IPFS CID" | **B** |
| WebDAV 挂载 | **可用** | 本账号 `PROPFIND /` → 207；`dav.2dland.cn` | **A** |
| SFTP | 存在 | `PubSftpConfig`（6 RPC，实测 `Get` 空响应） | **B** |
| 后台上传/下载 | 未测试 | 需真机操作 | **未测试** |
| 断点续传（下载） | 可用 | 206 + Range | **A** |
| 失败重试 | 客户端实现 | Web 端分片下载 10 次重试 | **B** |
| 缓存/清理 | 存在 | Web 端 IndexedDB 按 CID 缓存（`survivalCount`） | **B** |
| 客户端日志导出 | 未见证据 | — | **未验证** |

---

## 6. API 清单

- **总计：10 个 service / 113 个 RPC（其中 5 个为注释态），62 个同时暴露 REST/JSON（grpc-gateway）；其余仅 gRPC-Web。**
- 全量清单落盘：`audit/evidence/rest_routes.json`（含 service/rpc/rest 三元组，从官方 proto 解析）。
- 认证机制（只描述机制，不记录凭据）：每个请求需 `timestamp`(ms) + `appid` + `appversion` + `sign`，签名串为
  `"/{service}/{method}" + timestamp + appid + appversion [+ "Bearer " + token]`，再拼共享密钥取 MD5；
  用户态另加 `authorization: Bearer <JWT>`。缺 `sign` → `grpc-status 3 missing timestamp`；缺 token → `16 invalid accesstoken`。

### 6.1 核心接口表（均为本次实测或直接读 proto）

| 业务 | 方法 | 域名/路径 | 认证 | 关键请求字段 | 关键响应字段 | 传文件本体 | 证据 |
|---|---|---|---|---|---|---|---|
| 登录 | POST | `/v6/user/login` | 签名 | identity, password, **captcha(JSON)**, device | token{access,refresh,expire}, user | 否 | **A** |
| 刷新令牌 | POST | `/v6/oauth/refresh_token` | 签名 | client_id, refresh_token, grant_type | access_token, refresh_token, expires_in | 否 | **A** |
| 用户信息 | POST | `/v6/user/get` | 签名+JWT | identity | name, update_ts, … | 否 | **A** |
| 配额 | POST | `/v6/user/get_statistics_and_quota` | 签名+JWT | identity | 总量/已用/文件数/日流量配额 | 否 | **A** |
| 目录列表 | POST | `/v6/userfile/list` | 签名+JWT | parent, filter, list_info{token,limit,order_by} | files[], list_info | 否 | **A** |
| 新建 | POST | `/v6/userfile/create` | 签名+JWT | parent, name, path, dir | File | 否 | **B** |
| 建上传任务 | POST | `/v6/userfile/create_upload_task` | 签名+JWT | path, size | **upload_address**, task, block_size, codec/hash_type | 否 | **A** |
| 上传分片 | POST | `simple-file-proxy.2dland.cn/int/blocks/{task}/{cid}` | 会话串 | 原始字节 | 201 | **是** | **A** |
| 秒传探测 | GET | `simple-file-proxy.2dland.cn/int/blocks/{task}/{cid}` | 会话串 | — | `true`/`false` | 否 | **B**（SDK 实现） |
| 提交文件 | POST | `simple-file-proxy.2dland.cn/int/blocks/{task}` | 会话串 | JSON[CID] | File{content_identity,...} | 否 | **A** |
| 分片下载地址 | POST | `/v6/userfile/get_file_slices_download_info` | 签名+JWT | file.identity, list_info | slices[]{identity,download_address,**encrypt**,store_type} | 否 | **A** |
| 整文件直链 | POST | `/v6/userfile/get_direct_download_address` | 签名+JWT | identity | download_address, expire_at, content_identity | 否 | **A** |
| 下载字节（分片） | GET | `slices.qzy.xyz/ipfs/blocks/2/{xx}/{cid}?auth_key&session` | auth_key | — | 4 MiB 密文块 | **是** | **A** |
| 下载字节（整文件） | GET | `ipfs-gateway.qzy.xyz/file/{cid}/{uid}/{name}?auth_key&session` | auth_key | Range 可选 | 明文，`Accept-Ranges` | **是** | **A** |
| 预览信息 | POST | `/v6/userfile/get_download_and_preview_info` | 签名+JWT | identity | 预览/直链信息 | 否 | **A**（返回结构见 evidence） |
| 删除到回收站 | POST | `/v6/userfile/trash` | 签名+JWT | source[].identity | task, status, affected | 否 | **A** |
| 回收站列表 | POST | `/v6/userfile/list_trash` | 签名+JWT | parent, list_info | files[] | 否 | **A** |
| 永久删除 | POST | `/v6/userfile/delete_trash` | 签名+JWT | source[].identity | task, status | 否 | **A** |
| 分享创建 | gRPC | `v6.services.pub.PubFileShare/Create` | 签名+JWT | path_list, name, lifetime | identity, expire_ts | 否 | **A** |
| WebDAV | PROPFIND | `https://dav.2dland.cn/` | Basic（账号内 WebDAV 口令） | — | 207 multistatus | **是** | **A** |
| 离线任务 | POST | `/v6/offline_task/{parse,add,list,delete}` | 签名+JWT | url/magnet/ipfs | task[] | 否 | **B** |
| 工单 | gRPC | `PubTicket/*` | 签名+JWT | — | tickets | 否 | **B** |

> 表中所有 Token 值、`auth_key`、`session`、`upload_session`、用户 ID 均已打码，报告不含任何可用凭据。

### 6.2 REST 路径分组（62 条，节选）

```
认证  : /v6/user/{login,get,refresh,logoff,register,change_password,reset_password,
        device_authorization,get_statistics_and_quota,send_sms_code,...}（23）
OAuth : /v6/oauth/{authorize,device_code,get_authorize_state,get_device_code_state,
        get_token,refresh_token}（6）
文件  : /v6/userfile/{list,create,rename,trash,move,copy,delete,delete_trash,get,search,
        list_trash,list_recent_updated_files,create_upload_task,create_upload_token,
        get_slice_download_address,get_direct_download_address,
        get_download_and_preview_info,get_file_slices_download_info,
        parse_file_slice,star,unstar,...}（29）
离线  : /v6/offline_task/{parse,add,list,delete}（4）
```
其余（分享/DAV/SFTP/工单/WebRTC P2P）为 gRPC-only，无 REST 注解。

---

## 7. 测试文件结果

> 全部文件由 `audit/gen_files.py` 用固定种子随机生成，内容无隐私、无版权。上传目录 = 账号根目录。

| 文件 | 大小 (B) | 上传结果 | 下载结果 | SHA-256 一致 | CID / File ID | 分片 | 数据域名 |
|---|---|---|---|---|---|---|---|
| `hc-audit-small-*.txt` | 4 157 | ✅ 1.06 s | ✅ 0.35 s（分片）/0.50 s（直链） | ✅ | `bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey` / `582584c4…` | 1 | slices + ipfs-gateway |
| `hc-audit-1m-*.bin` | 1 048 576 | ✅ 1.22 s | ✅ 0.31 s / 0.67 s | ✅ | `bafkreigoiq5uaswpwxz7pbzydwxl264dosbga3y35zm2eppfszo5rri7zq` / `c1f15854…` | 1 | 同上 |
| `hc-audit-4m-*.bin` | 4 194 304 | ✅ 1.26 s | ✅ 0.55 s / 0.94 s | ✅ | `bafkreigmhxtlmiqbovbmrhqwrfuihfcgukpwbx3titjrhs66y4fvbpxc7y` / `e0f904a1…` | **1**（4 MiB 正好一片） | 同上 |
| `hc-audit-20m-*.bin` | 20 971 520 | ✅ 3.57 s | ✅ 3.08 s / 2.47 s | ✅ | `bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4` / `ffd2b906…` | **5** | 同上 |
| `hc-audit-image-*.png` | 339 211 | ✅ 1.08 s | ✅ 0.28 s / 0.77 s | ✅ | `bafkreifgyqrjh4e3bnqtps73kkf2fye4277fx5dghgbyiw3idfsay4f7gm` / `8d715bef…` | 1 | 同上 |
| `hc-audit-video-*.mp4` | 1 356 | ✅ 0.93 s | ✅ 0.11 s / 0.46 s | ✅ | `bafkreidx3ok7pwkwfmhbxqixuk4hoonsdltszbka3o6q7iqnx3t6qhkv5m` / `d55f8ab4…` | 1 | 同上 |

**逐文件 SHA-256（本地原件）**

```
hc-test-small.txt   d99a7997a3e1221af95c8c755e63b44d7c06a4bc32e91aeaacb3a48f97302326
hc-test-1m.bin      ce443b404acfb5f3f787381daebd7b837482606f1bee59a23de5965dd8c51fcc
hc-test-4m.bin      cc3de6b622017542c89e168968839446a29f60df7344d313cbdec70b50bee2fe
hc-test-20m.bin     38990a5b2d0cd1e6c978e8477eb3a34da47298965b8fc458ebcfc0114f4bf4b3
hc-test-image.png   a6c42293f09b0b6137cbfb528ba2e09cd7fe5bf4663983845b6819640c70bf33
hc-test-video.mp4   77db95f7d9562b0e1bc117a2b87739b21ae72c8540dbbd0fa20dbee7e81d55eb
```

**哈希一致性**：6/6 通过（分片通道按 `encrypt` 解 XOR 后比对；整文件通道直接比对）。

**未能覆盖**：>20 MiB 大文件（避免无谓占用用户额度）、真实视频转码、并发/限速边界（任务书禁止压测）。

---

## 8. 性能与限制（实测口径）

| 指标 | 实测值 | 条件 |
|---|---|---|
| 上传吞吐（20 MiB，5 片串行） | **≈ 5.9 MB/s**（3.57 s 含建任务+提交） | 家用宽带，百度/火山 CDN 节点 |
| 上传吞吐（4 MiB） | ≈ 3.3 MB/s（1.26 s，含固定开销） | 同上 |
| 上传固定开销 | ≈ 0.9–1.0 s/次（建任务 + 提交） | 小文件全部被此开销支配 |
| 下载吞吐（20 MiB，分片） | ≈ 6.8 MB/s（3.08 s） | 同一网络 |
| 下载吞吐（20 MiB，整文件） | ≈ 8.5 MB/s（2.47 s） | 同一网络 |
| 分片大小 | **4 MiB**，服务端下发 | 实测 + SDK 常量 |
| Range | 支持，`206` + `Accept-Ranges: bytes` | 6/6 |
| 限流 | 未触发 429 | 请求量极小 |
| 免费额度（本账号） | 总 12 TiB；已用 ≈ 4.90 TiB；文件 6 766 | `GetStatisticsAndQuota` |
| 日流量配额 | 下行 1 TiB / 上行 10 GiB（字段含义按既有逆向结论，**D**） | 同上 |
| 单文件上限 | **未验证**（未尝试超大文件） | — |
| 可靠性/保留承诺 | **官方明确不保证** | 封存公告 |
| 空间性质 | **逻辑额度**，非独立物理副本承诺 | 结合 CID 共享与删除语义推断（**D**） |

---

## 9. GitHub 仓库逐项审查（组织全量 9 个）

> 数据抓取：GitHub REST API（2026-11-22）+ 源码包 codeload 下载解包至 `audit/repos/`。

| 仓库 | 语言 | License | 最近 push | 归档 | 用途 | 与线上服务关系 | 对 MyDrive 价值 |
|---|---|---|---|---|---|---|---|
| `golang-sdk` | Go | NOASSERTION | 2025-05-08 | 否 | 官方 CLI + SDK（disk/dav/sftp/offline/share 子命令） | **直接对接线上**：硬编码 `grpcuserapi.2dland.cn:443`；`IpfsDefaultBlockSize=4 MiB` | ⭐⭐⭐ 上传/秒传/CID 逻辑可直接抄 |
| `golang-sdk-lite` | Go | Apache-2.0 | 2026-01-14 | 否 | 轻量 SDK，示例指向 `openapi.2dland.cn` | 对接线上（REST 入口） | ⭐⭐⭐ 最易集成 |
| `halalcloud-client` | C#/C++ | MIT | 2026-05-02 | 否 | 多平台桌面客户端，README 标注 **WIP** | 未证实可用于生产 | ⭐ 体积大（105 MB），完成度低 |
| `halal-multimedia` | C++ | 无 | 2024-06-14 | 否 | 转码 + 流媒体服务；README 自称"2dland.cn 线上使用的转码服务（生产级）"，FFmpeg + SLS，源自 gosun 分发系统 | **曾是线上转码组件**（**C**） | ⭐ 与网盘存储无关 |
| `kagami-storage` | Go | 无 | 2024-06-25 | 否 | Haystack 风格小文件存储（directory/store/pitchfork/proxy/ops） | README：**2016-2022 用于 6pan.cn**，**与当前清真云无证据关联** | ⭐ 历史系统，不建议复用 |
| `HalalCloudUniversal` | Dart | 无 | 2025-01-13 | 否 | Flutter 多驱动客户端（WebDAV 驱动为主） | 用 `grpcuserapi` + `dav.2dland.cn` | ⭐⭐ WebDAV 接法可参考 |
| `halalcloud-qt` | Batchfile/Qt | LGPL-3.0 | 2026-08-27 | 否 | **Qt 构建脚本/补丁集**（qtbase、qtsvg…），非业务代码 | 构建产物用于桌面端 | ⭐ 无业务价值 |
| `BaiduBce` | C# | Apache-2.0 | 2025-05-07 | **是** | 百度 BCE(BOS) .NET SDK 的 .NET Standard 移植 | 曾是客户端组件（离线下载/对象存储相关），**不能证明主存储用 BOS** | ⭐ 仅历史参考 |
| `stellarplayer-resourceauth-go` | Go | Apache-2.0 | 2026-11-14 | 否 | StellarPlayer Gateway 的 Resource Server SDK（ES256/JWKS 校验） | 与网盘无关，属播放器生态 | ✗ |

### 9.1 源码级关键发现（可直接引用的调用链）

**`golang-sdk/constants/ipfs.go`**
```go
const (
    IpfsDefaultBlockSize = 4 * 1024 * 1024 // 4MB
    IpfsMaxLinks         = 1024            // maxlinks
)
```
→ 官方 SDK 明确 4 MiB 块 + 1024 分叉，与本次实测完全吻合（**B**）。

**`golang-sdk/cmd/disk/upload.go`**
- `CreateUploadTask(File{Path, Size})` → 若 `result.Created` 为真则**文件已存在（秒传）**，直接返回；
- 逐片 `cid.Prefix{Codec: BlockCodec, MhType: BlockHashType, Version: 1}.Sum(chunk)`；
- `GET {uploadAddress}/{task}/{cid}` 返回 `true` 即跳过上传（块级去重）；
- 最后 `POST {uploadAddress}/{task}` 提交 CID 数组，服务端回 `File.ContentIdentity`。

**`kagami-storage/README.md`（原文）**
> "`kagami-storage` 曾用名 (`efs`) 是基于 facebook haystack 用 golang 实现的小文件存储系统。**曾于 2016-2022 年间 作为 6pan.cn 的存储系统所使用**。"
> directory 负责调度与元数据（存 **HBase**，`gosnowflake` 生成 file key）；store 负责物理存储；pitchfork 负责健康/磁盘监控；proxy 负责 bucket 与 API；ops 负责扩容/压缩。

→ §7 关键问题的答案：**没有证据表明 kagami-storage 是当前清真云的后端**，官方 README 自己把它定位为 6pan.cn 时代的历史系统（**C**）。

---

## 10. MyDrive 集成方案评分

评分 1–5（5 最好/最低风险）。成本项 5 = 最省。

| 方案 | 可行性 | 成本 | 带宽节省 | 可靠性 | 集成难度(5=最易) | 条款风险(5=最低) | 维护风险(5=最低) | 合计 | 结论 |
|---|---|---|---|---|---|---|---|---|---|
| **A. 服务端中转**（MyDrive 下载再转发） | 5 | 2 | 1 | 3 | 5 | 4 | 3 | 23 | 不推荐：出站流量全由自己承担，12 TiB 库等于自建 CDN |
| **B. 客户端直连**（后端只发签名地址） | 4 | 5 | **5** | 3 | 2 | 3 | 2 | 24 | 可行但工程量大：需复刻 MD5 签名 + `auth_key` 体系，且 `appsecret` 可被服务端轮换 |
| **C. IPFS 直连**（存 CID 走公共网关） | **1** | 4 | 4 | 1 | 3 | 3 | 1 | **17** | **不可行**：DHT 0 Provider，公共网关取不到（实测） |
| **D. 清真云主存储 + TG 备份** | 3 | 3 | 3 | 2 | 3 | 3 | 2 | 19 | 不推荐作主存储：封存 + 删除不彻底 |
| **E. 清真云仅作可选来源** | **5** | 4 | 4 | 4 | 4 | 4 | 4 | **29** | ✅ **推荐** |

**推荐：方案 E（可选外部来源 + Telegram 兜底）**

- MyDrive 只保存 `{external_provider: "halal", file_identity, content_identity(CID), size, sha256}`；
- 用户点"从清真云导入"时，由**浏览器/客户端直连**清真云取数（方案 B 的数据面），MyDrive 服务端零字节流量；
- 导入成功后立刻写一份到 Telegram（现有能力），此后以 TG 为主副本；
- 清真云不可用时，仅丢失"未导入"的那部分，已在 TG 的文件不受影响。

**备选：方案 B 的降级版**——只支持"清真云 → MyDrive 一次性迁移"（服务端中转一次，之后不再回源）。成本可算：迁移 1 TiB 需 1 TiB 出站，按常见云主机 0.5 元/GB 约 500 元；仅在用户明确要求时触发。

**绝对不建议**：把清真云当唯一主存储（方案 D/C）。理由只有一条就够：官方已书面声明"数据完整性不再保证"。

---

## 11. 风险与未验证事项

### 11.1 已确认风险（A）

1. **服务封存**：官网明示停止维护、不保证可用性与数据完整性。
2. **删除不彻底**：`Trash`/`DeleteTrash` 后旧签名 URL 仍可取回完整明文（≥240 秒受控窗口，仍未关闭）。
3. **凭据明文回传**：`PubDavConfig/Get` 直接返回 WebDAV 用户名与**明文口令**（本次已打码，不写入报告）。任何拿到账号 token 的一方都能拿到 WebDAV 全盘读写权限。
4. **共享密钥硬编码在前端**：Web 包内含 `appid/appversion/appsecret`，签名算法可被任何人复刻（本次即以此直连 API）。
5. **APK 签名证书为通用测试证书**（`CN=Android, O=Google Inc.`，SHA-256 `2edbacbf…`），非典型企业签名，更新分发链路可信度低。

### 11.2 高概率风险（D）

1. 额度是逻辑额度：多个用户上传相同内容只存一份（CID 去重），"已用空间"是账号维度的账面数字。
2. 块回收依赖异步任务，长期是否 GC 不可观测；被封存的服务很可能**不再执行 GC**，也**不再做副本修复**。
3. CDN 边缘缓存可能长期持有已删除文件的密文块（本次观测到 TCP_HIT 后仍可取回）。

### 11.3 资料不足 / 未验证

| 项 | 原因 | 补齐条件 |
|---|---|---|
| 公共网关能否取到内容 | `ipfs.io`/`dweb.link`/`pinata` 在本网络被代理阻断（502/超时） | 换可直连国际网络的出口重测 |
| 元数据数据库类型 | 无接口暴露 | 需服务端视角，不可得 |
| 是否 pin / pin 在哪 | 无公开 pin 接口 | 同上 |
| 视频转码真实行为 | 无合规自制视频；本机无 ffmpeg | 提供 ffmpeg 后可造 5–20 MiB 测试视频 |
| 单文件大小上限 | 未尝试超大文件 | 用户授权后可测 1–5 GiB 单文件 |
| 新用户注册 | 腾讯防水墙验证码，按规定不绕过 | 人工在浏览器注册 |
| Android 端逐项功能 | 审计期间设备断开（USB） | 重新连接后可补 |
| `openapi.2dland.cn` 用途 | 根路径 404，SDK 示例仅作 base url | 读 `golang-sdk-lite` 源码中的 path 常量 |
| 分享页匿名下载 | 分享页为 SPA，需前端 JS 取数 | 用浏览器自动化加载分享页 |

### 11.4 需要用户进一步授权

- 上传 >100 MiB 文件（会实际占用额度）
- 真实视频转码测试（需生成 5–20 MiB 视频）
- 注册新账号验证注册流程

### 11.5 明确不建议执行的测试

- 任何并发/压测/批量上传（违反任务书，且会触发风控）
- 枚举他人 file_id / share_id、越权访问
- 对非自建文件执行删除
- 绕过防水墙验证码、绕过证书固定
- 付费/订阅/额度变更操作

---

## 12. 原始证据附录

### 12.1 官方链接

| 类型 | 链接 | 抓取时间 | 备注 |
|---|---|---|---|
| 官网（封存公告） | `https://2dland.cn/` | 2026-11-22 21:0x | 原文："清真云已进入封存阶段……不再作出保证" |
| Web 客户端 | `https://drive.2dland.cn/` | 同上 | SPA，200 |
| 账号中心 | `https://ucenter.2dland.cn/` | 同上 | OAuth2 授权码 |
| WebDAV | `https://dav.2dland.cn/` | 同上 | 401 未认证 / 207 已认证 |
| GitHub 组织 | `https://github.com/halalcloud/` | 同上 | 9 个公开仓库 |
| 官方文档空间 | `https://2dland.yuque.com/r/organizations/homepage` | 同上 | JS 渲染，仅取到标题，正文**未获取** |

### 12.2 仓库链接与关键文件

| 仓库 | 关键路径 | 行号/要点 |
|---|---|---|
| `halalcloud/golang-sdk` | `constants/ipfs.go` | `IpfsDefaultBlockSize = 4*1024*1024`；`IpfsMaxLinks = 1024` |
| 同上 | `constants/appkey.go` | `AppID=devDebugger/1.0`，`AppSecret=[REDACTED]`（公开仓库内明文） |
| 同上 | `cmd/disk/upload.go` | `CreateUploadTask` → `postFileSlice` → `makeFile`（提交 CID 数组） |
| 同上 | `auth/auth_service.go:53,57,190,192` | 硬编码 `grpcuserapi.2dland.cn:443` |
| 同上 | `pkg/downloader/slice_downloader.go` | WebRTC(pion) DataChannel 传片，**实现为骨架**（`startIfStopTask` 直接 `return nil`） |
| `halalcloud/golang-sdk-lite` | `examples/*/main.go` | base url `openapi.2dland.cn` |
| `halalcloud/kagami-storage` | `README.md` | "曾于 2016-2022 年间 作为 6pan.cn 的存储系统所使用" |
| `halalcloud/halal-multimedia` | `README.md` | "2dland.cn 线上使用的转码服务（生产级别）…依赖 FFMPEG 和 SLS" |
| `halalcloud/BaiduBce` | `README.md` | "originally a component in halalcloud-client"（BOS SDK 移植） |
| `halalcloud/HalalCloudUniversal` | `lib/constants/api/server.dart:2` | `baseUrl = 'grpcuserapi.2dland.cn'` |
| 同上 | `lib/providers/driver_provider.dart:11` | `uri: 'https://dav.2dland.cn'` |

### 12.3 关键请求/响应（脱敏样例）

**CreateUploadTask 响应（20 MiB 文件）**
```json
{
  "2": "https://simple-file-proxy.2dland.cn/int/blocks",
  "3": "upload_session::[REDACTED]_[REDACTED]:ts::[REDACTED]",
  "16": 4194304,
  "17": 18,
  "18": 1,
  "19": 85
}
```

**commit 响应**
```json
{
  "user_identity": "[REDACTED]",
  "parent": "[REDACTED]",
  "name": "hc-audit-small-[REDACTED].txt",
  "mime_type": "text/plain",
  "size": 4157,
  "content_identity": "bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey"
}
```

**GetFileSlicesDownloadInfo（脱敏）**
```json
{
  "1": [{
    "1": "bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey",
    "2": "https://slices.qzy.xyz/ipfs/blocks/2/64/bafkrei…?auth_key=[REDACTED]&session=[REDACTED]",
    "4": 100,
    "5": null
  }]
}
```

**GetDirectDownloadAddress（脱敏）**
```json
{
  "1": "https://ipfs-gateway.qzy.xyz/file/bafkrei…/[REDACTED]/hc-audit-….txt?auth_key=[REDACTED]&session=[REDACTED]&filename=[REDACTED]",
  "2": 1795359086,
  "7": 4157,
  "10": "bafkreigztj4zpi7beinpsxemovpghncnpqdkjpbs5enovlftushzombdey"
}
```

**配额响应**
```json
{"1":{"1":13194139533312,"2":5385712344258,"4":655360,"5":6766,"6":1024},
 "2":{"1":1099511627776,"2":10737418240,"3":1024,"4":26575752},
 "3":{"1":1024,"2":1,"3":10995116277760,"4":1}}
```

### 12.4 命令与输出（可复现）

```bash
# 1) 刷新令牌（OAuth2 标准 refresh，非绕过）
POST https://grpcuserapi.2dland.cn/v6.services.oauth.OauthAuthorization/RefreshToken
  client_id=2dland/web, refresh_token=[REDACTED], grant_type=refresh_token
→ grpc-status 0, access_token 有效期 2 h

# 2) 生成测试文件
python audit/gen_files.py
# 3) 上传（建任务 → 逐片 POST → 提交）
python audit/run_uploads.py
# 4) 下载 + 哈希比对（含 XOR(encrypt) 解码）
python audit/download_test.py
# 5) CID 独立复算
node audit/cid_nodes.mjs audit/testfiles/hc-test-20m.bin 4194304
→ {"root":"bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4", ...}  # 与线上一致
node audit/cid_nodes.mjs audit/testfiles/hc-test-20m.bin 1048576
→ {"root":"bafybeihx4ftfphmgo52mfbe43pmpkpdblo3o37l3gt6gxpc54umxfpr5ri"}  # 不同 ⇒ 排除 1 MiB 分片

# 6) 公共 DHT 查询
curl -s "https://delegated-ipfs.dev/routing/v1/providers/bafybeia4uantfzxv25vtf2lmxelb3qtynvj6zdxfns3qmfl3co235akez4"
→ {"Providers":[]}
```

### 12.5 证据文件清单

| 文件 | 内容 |
|---|---|
| `audit/evidence/uploads.json` | 6 个文件的上传任务、分片、耗时、CID |
| `audit/evidence/downloads.json` | 分片/直链下载、耗时、哈希比对、响应头、Range |
| `audit/evidence/delete_semantics.json` / `delete_cache.json` / `delete_window.json` | 删除前后 API 与旧 URL 行为（含 240 s 受控计时采样） |
| `audit/evidence/gateway_probe_curl.json` | 6 个公共网关 × 3 个 CID 的取回结果 |
| `audit/evidence/providers_curl.json` | DHT provider 查询结果（全 0） |
| `audit/evidence/rest_routes.json` | 113 RPC / 62 REST 全量清单 |
| `audit/evidence/account_readonly.json` | 用户/配额/DAV/预览等只读采样（已脱敏） |
| `audit/evidence/gh_repos.json` | GitHub 组织 9 个仓库元数据 |
| `audit/repos/` | 6 个仓库源码（解包） |

---

## 13. 最终结论：15 个问题的回答

| # | 问题 | 回答 | 等级 |
|---|---|---|---|
| 1 | 线上服务是否仍可用？是否仍维护？ | **可用但不维护**。API/上传/下载/WebDAV 全部实测可用；官网明示"已停止主动开发与维护、封存阶段"。 | **A（可用）/ C（不维护）** |
| 2 | 宣称使用 IPFS，实测能否证实？ | **部分证实**：内容寻址、CID、UnixFS DAG、4 MiB 分片都是真的，可离线复算；**但"接入公共 IPFS 网络"未被证实，反而被证伪**（DHT 0 Provider）。 | **A** |
| 3 | 上传时文件字节发到哪个服务？ | `simple-file-proxy.2dland.cn/int/blocks/{session}/{cid}`（火山引擎 CLB 后自建服务），不经过 `grpcuserapi`。 | **A** |
| 4 | 下载时字节从哪返回？ | ① `slices.qzy.xyz`（4 MiB 密文块，XOR 密钥在响应里）；② `ipfs-gateway.qzy.xyz`（整文件明文）。两者前置百度 CDN，后置火山 CLB。 | **A** |
| 5 | 是否有自建 IPFS 节点/Cluster 的直接证据？ | **有私有网关的直接证据**（`/ipfs/blocks`、`/file/{cid}` 路由 + CID 作为 ETag）；**无 Cluster 证据**。 | **A（网关）/ 未验证（Cluster）** |
| 6 | 是否存在 Pinning？谁 Pin？有保证吗？ | **不可观测**。无 pin 接口、DHT 无 Provider，故无法证明有持续 pin；官方已声明不保证完整性 ⇒ 视为**无保证**。 | **未验证（机制）/ A（无承诺）** |
| 7 | 是否存在 S3/BOS/OSS 或自建存储等中心化后端？ | **确定有自建块存储**（CID 寻址、`Byte-nginx`）。对象存储**未见证据**（`UploadTask` 的 S3 字段全空、无预签名 PUT）；`BaiduBce` 仓库仅说明曾用于客户端。 | **A（自建）/ B（对象存储存疑）** |
| 8 | CID 是真实内容寻址还是内部字段？ | **真实**。三种文件的 CID 与本地独立复算逐字节一致；分片 CID = `sha256(4 MiB 块)`；前端下载完成还会用 `sha256` 反查 CID。 | **A** |
| 9 | 能否脱离客户端/官方 API 独立恢复？ | **不能，除非有签名 URL**。文件字节虽在自家网关，但需 `auth_key`（服务端签发、MD5 签名）；公共 IPFS 取不到。WebDAV 是唯一"标准协议"出口，但也要账号口令且已封存。 | **A** |
| 10 | 删除后元数据/Pin/物理数据如何变化？ | 元数据**立即失效**（API 拒绝签发）；**物理块与 CDN 缓存未同步失效**（旧 URL 仍可取回明文）；长期是否 GC 不可观测。 | **A（现象）/ D（机制）** |
| 11 | 免费容量、文件限制、流量限制、保存期限？ | 本账号 12 TiB 逻辑额度、已用 4.90 TiB；日配额下行 1 TiB/上行 10 GiB（字段级推断）；单文件上限**未验证**；保存期限**无公开承诺**。 | **A（数值）/ D（语义）/ 未验证（上限）** |
| 12 | 哪些功能真实可用，哪些只是源码存在？ | 真实可用：登录刷新、列表、上传、下载、秒传、删除/回收站、WebDAV、分享创建、Range 续传。仅源码/UI 存在：P2P(WebRTC) 下载（SDK 是骨架）、SFTP、离线 BT、转码（未实测）。 | **A/B** |
| 13 | 哪些仓库可直接复用，哪些是历史项目？ | 可复用：`golang-sdk`、`golang-sdk-lite`（上传/秒传/CID 逻辑）。历史/无关：`kagami-storage`（6pan.cn 时代）、`BaiduBce`（归档）、`halal-multimedia`（转码，与存储无关）、`halalcloud-qt`（构建脚本）、`stellarplayer-resourceauth-go`（播放器生态）。 | **B/C** |
| 14 | MyDrive 能否低成本接入并避免服务端承担文件本体流量？ | **能**，但只能走"客户端直连 + 服务端只存元数据"（方案 B 的数据面 / 方案 E）。服务端中转（方案 A）会让出站流量全落到自己身上，不划算。 | **A（架构可行性）/ D（成本估算）** |
| 15 | 若清真云彻底停服，MyDrive 用户文件能否恢复？ | **只有已落 Telegram（或本地）副本能恢复**。仅存 `file_identity` 的条目将失效：CID 在公共 IPFS 无 Provider，`qzy.xyz` 域名一旦下线即无法取回。故**必须双写**。 | **A** |

---

## 附：一句话给决策者

> 清真云"是真 IPFS 的形，不是真 IPFS 的实"——CID 可以复算、可以校验，但数据只躺在它自己的机房里，靠百度和火山的 CDN 分发，且服务已封存、删除不彻底。**当"可选来源 + 立刻双写 Telegram"用，可以；当唯一主存储用，不行。**
