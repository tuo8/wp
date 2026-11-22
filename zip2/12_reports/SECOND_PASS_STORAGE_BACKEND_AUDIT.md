# 清真云（6pan / HalalCloud）当前存储后端 · 第二轮定向取证

> 取证窗口：2026-11-22 ~ 2026-11-23（与第一轮同一证据集，本轮新增 5 组定向实验）
> 证据根目录：`halalcloud-current-audit/`
> 历史隔离：**未使用**任何 kagami-storage、旧版报告、旧抓包；本文所有观测均来自本轮 `11_scripts/t29–t33` 与 `07_storage_backend/`、`08_persistence_tests/` 下的当前文件。
> 本轮不修改 MyDrive 业务代码，不触碰他人数据，不做端口扫描，不绕认证。

---

## 0. 本轮范围、方法与两个必须先声明的观测条件

### 0.1 本轮做了什么（只做增量）

| 编号 | 实验 | 脚本 | 产出 |
|---|---|---|---|
| R2-1 | 用同一 CID 反复申请分片地址（每次 `auth_key` 不同），观察是否真的回源 | `t29/t32/t33` | `07_storage_backend/backend_probe_round2.json`、`address_fields_round2.json` |
| R2-2 | 逐项检查响应头指纹（404/400/200/HEAD/Range/无缓存头） | `t30` | `07_storage_backend/origin_fingerprint.json` |
| R2-3 | 24 块分层采样，记录 `via` / `age` / `x-response-cache` / `x-bdcdn-cache-status` | `t31` | `07_storage_backend/cdn_layer_samples.json` |
| R2-4 | 公开 DNS（CNAME 链）、ASN、证书核查 | 手工命令 | 本文 §4、`ip_asn_round2.json` |
| R2-5 | 删除后 **63 分钟**再读（新地址 + 新 auth_key）+ 用已删 `content_identity` 做秒传 | `t33` | `08_persistence_tests/post_delete_reread_round2.json` |

### 0.2 观测条件一：本机出口在第二轮**不在国内**

`t30` 记录：本机出口 `104.28.x.x`，`Cloudflare Warp`，AS13335，US。四个独立服务一致（`ipip.net` / `ip-api` / `ip.sb` / `cloudflare.com/cdn-cgi/trace`，`loc=US colo=LAX`）。

后果：**第二轮所有 HTTP 观测的 `via` 链尾部出现 `oversea-SG-SIN12` / `oversea-US-IAD3` / `oversea-US-DAL1` 等海外节点**。第一轮（App 侧，出口为中国联通/移动 116.x）的链只有 `cacheXX.xxmp,picYY.hhhtcu` 两跳。这是**客户端位置差异**，不是服务端拓扑变化。所有涉及"地理位置/节点归属"的推断都必须带这个前提。

### 0.3 观测条件二：`via` 链的先后顺序决定节点角色判定

同一 `via` 头在两轮里形态不同：

```
第一轮（中国出口）：cache24.sxmp, pic02.hhhtcu
第二轮（美国出口）：cache07.tzmp, cache15.zjhuzhou-mp01, cache05.oversea-SG-SIN12,
                    cache16.CN-HK-HKG8, cache05.oversea-US-IAD3, cache03.oversea-US-IAD1
```

客户端位置从中国换到美国后，**链的尾部**整体换成了海外节点，**链的首部**仍是国内 `cacheXX` 命名。链的长度会随"客户端侧新增了几跳"而增加，而新增的跳只出现在尾部 ⇒ **尾部条目靠近客户端，首部条目靠近源站**。这条推论是下文推翻"pic0x 是回源节点"的关键，见 §4.3。

---

## 1. 证据基线（本轮只认这些）

| 编号 | 证据 | 文件 |
|---|---|---|
| E2-01 | `slices.qzy.xyz` CNAME → `slices.qzy.xyz.volcgslb.com` → `sx-img-all.volcgtm.com`；`ipfs-gateway.qzy.xyz`、`simple-file-proxy.2dland.cn` → `sx-univ-all.volcgtm.com`；`drcdn2.2dland.cn` → `drcdn2.2dland.cn.eo.dnse2.com` | 本文 §4.1 |
| E2-02 | 块 404 响应体为 `read [<cid>] error: not found`（83 B，HTTP 404）；畸形 CID 为 HTTP 400（61 B） | `origin_fingerprint.json` `absent_raw_cid` / `absent_dagpb_cid` |
| E2-03 | 全部响应带 `server: Byte-nginx`、`vary: Origin`、`x-request-id`、`x-response-cache`、`x-bdcdn-cache-status`、`x-tt-trace-tag: id=5`、`x-exception-info: O4\|404` | `origin_fingerprint.json`、`backend_probe_round2.json` |
| E2-04 | 块路径为 `/ipfs/blocks/{1位hex}/{2位hex}/{cid}`，两级扇出 | `t32`、`gb_per_block.json` |
| E2-05 | 每块一个 1 字节 XOR 键（`encrypt` 字段，范围 50–122，807 块共 32 个不同键），逐次请求随机 | `gb_download.json`、`slice_download.json` |
| E2-06 | 块响应 `etag` = 该块 CID；整文件网关 `etag` = 根 CID，`last-modified: Sun, 04 Jun 1989 15:04:05 GMT`，`accept-ranges: bytes`，Range 返回 `206` + `content-range: bytes 0-1023/3381078505` | `backend_probe_round2.json` `gateway_head` / `gateway_range_nocache` |
| E2-07 | 807 块全量下载：`x-response-cache: miss` + `x-bdcdn-cache-status: TCP_MISS,...` 贯穿全链，`age` 0–2 | `gb_per_block.json` |
| E2-08 | 整文件网关响应 `x-response-cache: parent_hit`、`age: 2875`（≈48 min） | `backend_probe_round2.json` `gateway_head` |
| E2-09 | `GetSliceDownloadAddress` 响应地址消息只有 3 个字段：`{1: cid, 2: url, 4: encrypt}`；外层 `{2: 截止时间ms, 3: "127.0.0.1", 4: 1}` | `address_fields_round2.json` |
| E2-10 | 文件记录字段全集 `{1 identity, 3 parent, 4 name, 5 path, 6 mime, 7 size, 8 类型(目录=20/文件=40), 9, 10, 24 content_identity, 31}`，**无 store_type / etag / bucket / region** | `address_fields_round2.json` `list_dir` / `info_Get`；`api_calls.jsonl` 目录记录 `8: 20` |
| E2-11 | 客户端 `storeType` 只用于 `i===10` 时**跳过 CID 校验**；实际取值为 `undefined` → `NaN`，该分支为死代码 | `webclient/index-C6KzmNsb.js` |
| E2-12 | 三个自有 CID 的 DHT provider 响应为 16 字节（空），对照 CID 为 30 432 字节 | `public_ipfs_probe.json` |
| E2-13 | 删除后 63.2 min：根 + 叶子用**新 auth_key** 仍 200 且 CID 复算一致，链上仍然全 `MISS`、`age` 0–4 | `post_delete_reread_round2.json` |
| E2-14 | 用已删文件的 `content_identity` 直接 `Create`（不传任何块）→ 898 ms 成功建回同名文件记录 | `post_delete_reread_round2.json` `create_from_deleted_identity` |

---

## 2. 对第一轮 5 个判断的复核结论

| # | 第一轮说法 | 本轮结论 | 等级 |
|---|---|---|---|
| 1 | `pic01–04.hhhtcu` 是**回源节点** | **改判**：它们是客户端侧区域节点，不是回源节点 | `CONTRADICTED`（原判定） |
| 2 | 两级 CID 目录证明后端文件系统 | **收窄**：只证明 URL / 对象 Key 的组织形式 | `CONTRADICTED`（原判定） |
| 3 | 无对象存储/私有 IPFS/分布式 FS 的公开证据 | 维持，但补充两条公开线索（CDN 归属、块服务非标准 IPFS 实现） | `STRONGLY_SUPPORTED` |
| 4 | `store_type` / `wcs_etag` 有其他样本 | **无任何有效样本**：当前 API 不返回这两个字段，客户端 `storeType` 是死代码 | `CONFIRMED`（字段不存在） |
| 5 | 公共 DHT 无 Provider ⇒ 公共 IPFS 被排除 | **改判**：只能说明"当时无公开 provider 记录" | `CONTRADICTED`（原判定） |

### 2.1 为什么第 1 条要改判

- 中国出口那一轮，`via` 尾部是 `pic01–04.hhhtcu`（211/207/200/189 次）；换到美国出口后再取 24 个块，**`pic*` 一次都没出现**，尾部全部变成 `oversea-*`。
- 如果 `pic0x` 是源站侧节点，源站不动，链尾就不该随客户端位置整体更换。
- 命名也吻合客户端位置：第一轮 App 出口 `116.136.20.228`（联通，116.136/16 属内蒙古），`hhht` 是呼和浩特；第二轮出口在美西，链尾就变成 `oversea-US-*`。
- 结论：`pic01–04.hhhtcu` 是**区域接入/取流前端**（该区域专属的入口），**不是回源节点**。第一轮把它写成"四节点回源、回源几乎均分"属过度解读，本文予以撤回。

### 2.2 为什么第 2 条要收窄

`/ipfs/blocks/2/68/{cid}` 这种两级 16 进制扇出，**只能**证明"这个 HTTP 服务把 CID 映射成两级路径段"。它可以对应真实目录、可以对应对象存储的 key 前缀、也可以是 KV 的桶前缀、甚至是反向代理 rewrite 出来的假路径。本轮没有任何一条证据能区分这四种（见 §7-G）。

### 2.3 为什么第 5 条要改判

`delegated-ipfs.dev/routing/v1/providers/{cid}` 返回 200 + 16 字节（空 protobuf 列表），对照 CID 返回 30 432 字节。按 IPFS 官方文档，`providers` 为空意味着**该时刻没有节点向 DHT 公告**，可能原因包括：节点不公告（私有网络 / `Announce` 关闭 / 只做 client 模式）、节点离线、路由服务视角差异。**空列表不能证明"不在公共 IPFS"，只能证明"没有可被该路由服务看到的公告记录"**。原表述已撤回，改用 §6 的三段式结论。

---

## 3. 公开接口与客户端实现核查（对应任务第 3 节）

### 3.1 服务端返回里到底有没有 storage backend 字段

`t32` 把 5 类调用的 protobuf 全字段打了出来，逐字段核对：

| 调用 | 返回字段 | 是否含存储后端标识 |
|---|---|---|
| `GetSliceDownloadAddress` | `1: {1 cid, 2 url, 4 encrypt}`，外层 `2: 截止时间, 3: "127.0.0.1", 4: 1` | **否** |
| `GetDirectDownloadAddress` | `{1: 预签名 URL, ...}` | **否** |
| `List` | 每记录 11 个字段（见 E2-10） | **否** |
| `Get`（单文件） | 与 `List` 记录同构 | **否** |
| `GetFileInfo` / `Stat` / `GetFile` | `grpc 12 unknown method` | 不存在该方法 |

关键词全表扫描（`02_network_capture/api_calls.jsonl` 全量 + 上述响应原文）：

```
store_type / storeType / wcs_etag / wcsEtag / bucket / region / endpoint
storage_class / storageClass / origin / backend / s3 / oss / cos / bos / tos / r2
→ 0 命中
```

> 外层的 `3: "127.0.0.1"` 值得记一笔：它出现在每一次地址响应里，值为回环地址。合理猜测是"客户端应使用的下载 host 覆盖字段"，默认不生效。**这是猜测，标 `INFERRED`，不作为结论。**

### 3.2 `storeType` 的真实语义（前端代码级）

真正被执行的取块函数（`index-C6KzmNsb.js`）：

```js
async function _2(e,t,n,r,i,a){
  t!==10 && await aDe(oW);                       // 重试节流
  let s = await (await fetch(e,{signal:o})).blob(), c = await v2(s);
  for(let e=0;e<c.length;e++) c[e]^=r;           // 用 r=encrypt 逐字节 XOR
  let l = new Blob([c],{type:s.type});
  return i===10 || await iDe(l,n) ? l            // i=storeType；===10 时跳过校验
       : _2(e,t-1,n,r,i,a);
}
```

调用点：`_2(o, 10, cid, addresses[0].encrypt, Number(addresses[0].storeType))`。

三个事实：
1. `storeType` 只影响**要不要做 CID 校验**（`i===10` 跳过），与"块存在哪种存储"无关；
2. 服务端从不返回该字段（E2-09），`Number(undefined)` → `NaN`，`NaN===10` 永假 ⇒ **校验永远执行**，该分支是死代码；
3. 因此网上流传的"store_type 枚举 = 对象存储类型"在本客户端**没有任何代码支撑**。

`wcs_etag`：前端包 0 命中、抓包 0 命中、响应 0 命中。**删掉这个假设**。

### 3.3 备用代码 vs 实际执行代码的区分

| 项 | 性质 | 判定依据 |
|---|---|---|
| `storeType===10` 分支 | 备用/遗留 | 服务端不返回该字段，运行时恒走校验分支 |
| `_files.json` 中的 `FileSystemDirectoryHandle` 等 chunk | 浏览器 File System Access API 封装（保存到本地），与后端无关 | 文件名 + 内容均为 `showDirectoryPicker` 相关 |
| `config-D-fftcyx.js` | 运行时配置（API base 等） | 被 `index.html` 引用 |
| 签名算法 `appid/appversion/appsecret` | **实际执行** | 每次 gRPC-Web 调用必须带 `sign`，否则 `grpc-status 3` |
| `region`（21 次命中） | 与本主题无关，来自 `Intl.DateTimeFormat` / 时区 | 上下文均为 `timeZone`/`locale` |

---

## 4. 回源节点与 CDN 归属调查（对应任务第 4 节）

### 4.1 公开 DNS：CNAME 链的**直接证据**

```
slices.qzy.xyz            → slices.qzy.xyz.volcgslb.com → sx-img-all.volcgtm.com
ipfs-gateway.qzy.xyz      → ipfs-gateway.qzy.xyz.volcgslb.com → sx-univ-all.volcgtm.com
simple-file-proxy.2dland.cn → simple-file-proxy.2dland.cn.volcgslb.com → sx-univ-all.volcgtm.com
drcdn2.2dland.cn          → drcdn2.2dland.cn.eo.dnse2.com
```

- `volcgslb.com` / `volcgtm.com`：火山引擎（Volcengine）全局流量管理 / 调度域名的公开后缀形态。
- `eo.dnse2.com`：腾讯云 EdgeOne 的 CNAME 后缀形态。

**能确认的**：下载域名（`slices` / `ipfs-gateway`）与上传域名（`simple-file-proxy`）走**同一个厂商的 GTM 调度**；API 域名（`drcdn2`）走**另一家**。这是 CDN/接入层的直接证据。
**不能确认的**：CNAME 归属只能说明"谁在调度流量"，**不能**说明存储落在哪。

### 4.2 ASN 与响应头（间接线索，且相互矛盾的部分必须留白）

| 观测对象 | ASN / 归属 | 说明 |
|---|---|---|
| `slices.qzy.xyz` → 116.136.20.227/228 | AS4837 中国联通 CHINA169；geo 库标"北京·金融街" | 116.136/16 实际为中国联通内蒙古段，geo 库与分配记录冲突 ⇒ **城市级归属标 UNKNOWN** |
| `simple-file-proxy` / `ipfs-gateway` → 101.72.219.x、101.26.38.162 | AS4837 中国联通河北（承德） | 上传与整文件下载共用同一组 IP，说明**同一入口集群同时承担上传与整文件读** |
| `drcdn2.2dland.cn` → 119.188.175.5 / 221.204.163.72 | AS4837 山东济南 / 山西太原 | API 侧多省发散，符合 CDN 调度 |
| `2dland.cn` → 117.21.184.135 | AS139201 江西九江 IDC（电信） | 门户站，与存储无关 |

响应头指纹：

| 头 | 值 | 解读 | 等级 |
|---|---|---|---|
| `server` | `Byte-nginx` | 源站应用自称（非标准 nginx 默认值） | `CONFIRMED`（观测）/ 厂商归属 `UNKNOWN` |
| `x-tt-trace-tag` | `id=5` | 字节系链路追踪头的命名习惯 | `INFERRED` |
| `x-bdcdn-cache-status` | `TCP_MISS,...` 与跳数等长 | 与"百度 CDN"缩写同名，但与 `volcgtm` CNAME 存在冲突 | `UNKNOWN`（存在透传/沿用旧头的可能，不下结论） |
| `x-exception-info` | `O4\|404` / `O8\|404` / `O4\|400` | 节点侧异常码，数字与命中节点编号相关 | `INFERRED` |
| `x-request-ip` / `x-response-cinfo` | 客户端 IPv6（`2a09:bac5:...`，Cloudflare WARP 段） | 回显客户端地址，可用于核对链路 | `CONFIRMED` |
| `vary` | `Origin` | 按 Origin 变化缓存键 | `CONFIRMED` |

> 按要求：**不因节点名含某缩写就认定服务商或存储产品**。`pic`/`hhhtcu`/`tzmp`/`sxmp` 等一律只作路径标识使用。

### 4.3 `pic0x.hhhtcu` 的角色判定（本轮最重要的修正）

判据链：
1. 出现条件：只在"客户端在中国联通/移动网络"时出现，作为 `via` **尾部**；
2. 消失条件：客户端出口换到美国后，24 次采样 0 次出现；
3. 计数分布：211/207/200/189 近似均分，符合"同一入口的 4 个实例做负载均衡"，而不是"4 个源站副本"；
4. `via` 顺序推论（§0.3）：尾部靠近客户端。

**结论**：`pic01–04.hhhtcu` = 该区域的**接入/取流前端实例组**，角色 `STRONGLY_SUPPORTED` 为"客户端侧"；"回源节点"判定 `CONTRADICTED`。
**仍然未知**：真正的源站在 `via` 链中是否被记录（很可能不记录，因为源站通常不在 CDN 的 via 体系内）。`UNKNOWN`。

### 4.4 块服务的实现指纹（比节点名更有价值）

| 观测 | 含义 | 等级 |
|---|---|---|
| 404 body = `read [<cid>] error: not found` | 源站程序自己生成的错误（Go 风格 `%v` 拼接），**不是** CDN 错误页，也不是 Kubo 的 `blockstore: block not found` JSON | `CONFIRMED` |
| 畸形 CID → HTTP 400（61 B） | 存在 CID 语法校验层（先解析后查存储） | `CONFIRMED` |
| 路径 `/ipfs/blocks/{a}/{bc}/{cid}` | **不是** Kubo/Boxo 的任何标准端点（标准是 `/api/v0/block/get`、网关 `/ipfs/{cid}`） | `CONFIRMED` |
| 逐次随机 1 字节 XOR（32 键/807 块） | 自定义传输混淆层；IPFS 栈内不存在该机制 | `CONFIRMED` |
| `etag` = CID | 内容寻址语义被显式暴露到 HTTP 层 | `CONFIRMED` |
| 整文件网关 `last-modified: 1989-06-04` | 人为设置的固定早期时间（压制条件请求/启发式缓存） | `CONFIRMED`（行为）/ 目的 `INFERRED` |

这五条合起来指向一个判断：**对外暴露的是一套自研的内容寻址块服务（CID 为键、逐块可取、逐块可校验），而不是某个开源 IPFS 节点的 HTTP 端口。**

---

## 5. 持久化行为补充测试（对应任务第 5 节）

### 5.1 关键方法论：先把"缓存命中"从"源站可读"里剔除

第一轮的"删除后仍可取"有一个漏洞：预签名 URL 本身带 `age` 递增，可能只是 CDN 缓存。本轮用三个手段拆开：

| 手段 | 原理 |
|---|---|
| 每次重新申请地址 | `auth_key` 每次不同 ⇒ 若缓存键含 query，则必然 MISS，必须回源 |
| 读 `x-response-cache` / `x-bdcdn-cache-status` | 全链任一层命中都会显式写出 |
| 读 `age` | 命中会累积，回源后重新从 0 计 |

### 5.2 结果

| 时间点 | 对象 | 新地址 | HTTP | 字节 | CID 复算 | 链上缓存 | age |
|---|---|---|---|---|---|---|---|
| 删除 +0 s | 10 MiB 旧 URL | 否（复用） | 200 | 10 485 760 | 一致 | — | 0 |
| 删除 +490 s | 10 MiB 旧 URL | 否（复用） | 200 | 10 485 760 | 一致 | — | 495 |
| 删除 +3790 s（63.2 min） | 根 dag-pb | **是** | 200 | 165 | **一致** | 全链 MISS | 0 |
| 删除 +3790 s | 根 dag-pb（换 key 第二次） | **是** | 200 | 165 | **一致** | 全链 MISS | 0 |
| 删除 +3790 s | 叶子块 | **是** | 200 | 4 194 304 | **一致** | 全链 MISS | 4 |
| 删除 +3790 s | 叶子块（第二次） | **是** | 200 | 4 194 304 | **一致** | 全链 MISS | 0 |
| 对照（未删） | 大文件叶子 | 是 | 200 | 4 194 304 | 一致 | 全链 MISS | 1 |

### 5.3 秒传复活实验（决定性的一条）

用已删除文件的 `content_identity`（`bafybeiesgdft…pck4`）直接调 `Create`，**不上传任何字节**：

```
请求：parent=b648aca0…, name=hca-revive-probe.bin, size=10485760, content_identity=bafybeiesgdft…
响应：200，898 ms，返回完整文件记录（identity aba993f4…，path /hca-2026-11-23/hca-revive-probe.bin）
收尾：已 Delete 清理
```

这证明：**删除是元数据操作**。文件记录消失后，块库中该内容仍被完整保留，服务端能凭 content_identity 立即重建记录并对外提供读取。

### 5.4 但不要过度解读

- 63 min 内"仍可读"**不能**推出"永久保留"，只能推出"回收窗口 ≥ 63 min"。第一轮观察到 490 s，本轮把窗口推到 63 min，仍是**下界**。
- 也不能推出"有多副本"。单对象单次读取成功，与副本数无关。
- 整文件网关那条路径**确实存在缓存**（`x-response-cache: parent_hit`、`age: 2875`）。因此任何基于"播放还能播"的持久化结论都不可靠；**本文的持久化结论只建立在块路径上**（块路径全链 MISS + 每次新 auth_key）。

### 5.5 缓存 vs 源站 vs 延迟回收 —— 三分表

| 现象 | 归因 | 依据 | 等级 |
|---|---|---|---|
| 旧预签名 URL 在 490 s 内可读、`age` 递增到 495 | CDN 缓存（不可变对象长 TTL） | `age` 单调递增、URL 未变 | `CONFIRMED` |
| 整文件网关 2875 s 后 `parent_hit` | 父层缓存（整文件可缓存） | `x-response-cache: parent_hit` | `CONFIRMED` |
| 新 auth_key + 全链 MISS 仍 200 | **源站块库可读** | 缓存层自证未命中 | `STRONGLY_SUPPORTED` |
| 删除 63 min 后仍能秒传重建 | **延迟回收（GC 未执行或引用计数未清零）** | 无需上传即成功 | `STRONGLY_SUPPORTED` |
| 回收窗口上限 | 未知 | 只测到 63 min | `UNKNOWN` |

---

## 6. 公共 IPFS 结论修正（对应任务第 6 节）

三段式，逐段给等级：

1. **数据模型是 IPFS/UnixFS 兼容** — `CONFIRMED`。叶子 `cidv1 raw`（`\x01\x55\x12\x20`+sha256）、多块文件根为 `dag-pb`（`\x01\x70\x12\x20`）、`Data=\x08\x02\x18<size>` + blocksizes + `PBLink(Hash,Name="",Tsize)`；807 块根节点 41 973 B，离线复算与线上 `bafybeiebig24…hh4` **完全一致**。
2. **对外服务不是标准 IPFS 节点** — `STRONGLY_SUPPORTED`。路径、错误体、XOR 层、`Byte-nginx`、无 `/api/v0/*`、无网关 `?format=`/`?filename=` 语义（`/file/{cid}/{uid}/{name}` 是自研路由）。
3. **是否接入公共 IPFS 网络（公告/可被检索）** — `UNKNOWN`。仅观测到"三个自有 CID 在 `delegated-ipfs.dev` 上无 provider 记录（16 B 空响应），对照 CID 有 30 432 B 记录"。该结果的可能解释至少有四种：节点不公告、节点离线、路由服务视角差异、私有 swarm。**不能据此排除，也不能据此确认。**

> 更正第一轮表述：「公共 IPFS 可访问 = 错：DHT 无 provider」→ 应改为「**未观测到公开 provider 记录；是否接入公共网络未能确定**」。

---

## 7. 最终七问（A–G）

### A. 是否能确认底层为自建文件系统块库？

**分两半回答，不能合并：**

- 「对外是一套**自建的内容寻址块服务**」→ `STRONGLY_SUPPORTED`
  - 自研 HTTP 端点 + 自研错误体 + 自研 XOR 层 + `etag=CID` + 按 CID 逐块 404 + 秒传按 content_identity 重建。
- 「底层介质的**文件系统**（本地盘 / 分布式 FS 的目录树）」→ `UNKNOWN`
  - 两级 16 进制扇出只是 key 组织；对象存储同样能承接这种 key；没有任何一条证据能看到 `mount`、目录列表、inode、`df`。

**综合判定：`STRONGLY_SUPPORTED`（自建块服务）/ `UNKNOWN`（是否文件系统）。**

### B. 是否能确认使用私有 IPFS 节点？

`UNKNOWN`（且有一条弱反证）。
- 无直接证据：没有 libp2p 端口、multiaddr、bitswap、`/api/v0/id`、peer id 的任何观测。
- 弱反证：块服务端点与错误体均非 Kubo/Boxo 默认行为；XOR 混淆层在 IPFS 栈内不存在。
- 合理表述：**"按 IPFS 数据模型组织，但由自研服务提供"**；内部是否用 go-ipfs 的库做 DAG 组装（很可能，因为 DAG 字节级一致）无法从外部区分。

### C. 是否能排除对象存储？

**不能。** `CONTRADICTED`（针对"能排除"这一主张）。
对外只暴露自定义块服务，对象存储完全可以藏在它后面（`/ipfs/blocks/2/68/<cid>` → `oss://bucket/2/68/<cid>` 这种映射是常见做法）。所有响应头里 `x-amz-*` / `x-oss-*` / `x-cos-*` / `x-bce-*` 均为 0 命中——这只说明**中间层把云厂商头吃掉了**，不说明底下不是对象存储。

### D. 是否能确认实际物理存储介质？

`UNKNOWN`。零证据。可确认的只有"数据被组织为 4 MiB 不可变块、以 CID 为键、可随机读取"。

### E. 是否能确认源站节点数量和节点角色？

`UNKNOWN`。可确认的只有：
- `pic01–04.hhhtcu` 四个命名，出现在中国出口路径的**尾部**，负载近似均分 → 客户端侧接入实例组（`STRONGLY_SUPPORTED`）；
- 上传与整文件下载共用 `101.72.219.x / 101.26.38.162` 一组 IP（`CONFIRMED`）；
- 真正的源站数量、角色、是否独立集群：**没有任何观测**。`via` 链在源站侧通常是断的。

### F. 是否能确认多副本、纠删码和容灾策略？

`UNKNOWN`。零证据。
可侧写的两点（均不作为副本证据）：
- 单块大小恒为 4 MiB、末块为余数，**没有**纠删码分片/校验片的痕迹（纠删码通常会改变块尺寸或产生 parity 对象）；
- 删除后 63 min 仍可秒传重建 → **至少存在延迟回收**，与副本数无关。

### G. 如果仍不能确定，缺少哪一种服务端证据？

按"能一锤定音"排序：

| 缺口 | 能定什么 | 合法获取途径 |
|---|---|---|
| 块库目录/对象 key 的真实映射（如一次 `ls` 或对象存储 list 的响应） | A（是否文件系统）、C（是否对象存储） | 需服务端授权/运维侧提供；外部不可得 |
| 云厂商对象存储的响应头或 SDK 错误码 | C | 需穿透中间层；外部不可得 |
| 节点清单与集群拓扑（副本因子、EC 参数、机架/可用区） | E、F | 需架构文档；外部不可得 |
| 源站真实 IP 与监听端口（非 443） | E | 需端口扫描 ⇒ **本轮明确不做** |
| 多地域出口的并发读取对比（延迟/一致性） | 缓存分层与源站位置 | 需多地探针；本轮只有中国/美国两个出口，且第二轮出口为 WARP，样本不足 |
| 删除后更长窗口（24 h / 7 d）的复读 | 回收窗口上限 | 可继续做，但受账号额度与时间限制 |

---

## 8. 结论汇总（可直接引用的等级表）

| # | 结论 | 等级 | 主要证据 |
|---|---|---|---|
| 1 | 存储对外形态 = 自研内容寻址块服务（CID 为键、逐块可取可校验） | STRONGLY_SUPPORTED | E2-02/03/04/05/06 |
| 2 | 块大小 4 MiB，叶子 raw CID，多块根为 UnixFS dag-pb | CONFIRMED | 第一轮 + 807 块复算 |
| 3 | `pic01–04.hhhtcu` 是客户端侧区域接入节点，不是回源 | CONTRADICTED（原判定）/ 新判定 STRONGLY_SUPPORTED | E2 链尾随出口变化 |
| 4 | 两级 CID 路径只证明 key 组织，不证明文件系统 | CONFIRMED（作为限制） | §2.2 |
| 5 | API 与客户端均无 `store_type` / `wcs_etag` 的实际语义 | CONFIRMED | E2-09/10/11 |
| 6 | 删除为元数据操作，块保留 ≥63 min，可秒传复活 | STRONGLY_SUPPORTED | E2-13/14 |
| 7 | 块路径读取确实回源（非缓存命中） | STRONGLY_SUPPORTED | 全链 MISS + 新 auth_key |
| 8 | 整文件网关存在父层缓存（≥2875 s） | CONFIRMED | E2-08 |
| 9 | 下载/上传域名的 CDN 调度属火山引擎系，API 域名属腾讯 EdgeOne | CONFIRMED（CNAME）/ 存储归属 UNKNOWN | E2-01 |
| 10 | 底层是对象存储？私有 IPFS？分布式 FS？ | UNKNOWN / UNKNOWN（弱反证）/ UNKNOWN | §7-B/C |
| 11 | 多副本 / 纠删码 / 容灾 | UNKNOWN | §7-F |
| 12 | 公共 DHT 无 provider 仅说明无公开公告 | CONTRADICTED（原判定） | E2-12 |

---

## 9. 本轮新增/更新的证据文件

| 文件 | 内容 |
|---|---|
| `07_storage_backend/backend_probe_round2.json` | 6 组请求头指纹（200/HEAD/Range/无缓存头/缺失块） |
| `07_storage_backend/address_fields_round2.json` | 地址消息与文件记录的 protobuf 全字段（`auth_key`/`session` 已脱敏） |
| `07_storage_backend/origin_fingerprint.json` | 404/400 错误体、出口 IP、缓存层采样 |
| `07_storage_backend/cdn_layer_samples.json` | 24 块分层采样（via/age/缓存状态） |
| `07_storage_backend/ip_asn_round2.json` | 观测 IP 的 ASN / 归属 |
| `08_persistence_tests/post_delete_reread_round2.json` | 删除 63 min 后复读 + 秒传复活（已脱敏） |
| `11_scripts/t29–t33_*.py` | 本轮全部实验脚本（可复跑） |

> 交付包内**不含**任何 token / cookie / 密码 / 长期凭据；预签名 URL 的 `auth_key`、`session` 已替换为 `[REDACTED]`。
