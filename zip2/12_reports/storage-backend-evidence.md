# 存储后端证据汇编（判定「上传/处理/持久化/分发」四层）

> 原则：只用本次探测结果；无法证实的写 `SERVER_SIDE_PERSISTENCE_BACKEND_UNCONFIRMED`。

## 1. 四层拆分

| 层 | 组件 | 证据等级 | 关键证据 |
|---|---|---|---|
| 上传入口 | `simple-file-proxy.2dland.cn`（Byte-nginx，101.72.219.x / 101.26.38.162） | CONFIRMED | `POST /int/blocks/{session}/{cid}` → 201；`CreateUploadTask` 返回该地址 |
| 处理层 | 服务端在 commit 时构造 UnixFS 根，生成 `content_identity` | CONFIRMED | 3.15 GiB 根节点 41 973 B / 807 链接，本地复算逐字节一致 |
| 持久化层 | 自建内容寻址块库，路径 `/ipfs/blocks/{a}/{bc}/{cid}` | STRONGLY_SUPPORTED | 取块 URL 形态 + 两级目录分桶 + 回源节点 `pic0x.hhhtcu` |
| 分发层 | 多层 CDN（`via: cacheXX.<isp>`），边缘 `age` 0–2 | CONFIRMED | 807 次取块全部带 age；`via` 链可枚举 |

## 2. 为什么判定「内容寻址块存储」而不是对象存储

| 观察 | 含义 |
|---|---|
| 取块 URL 为 `/ipfs/blocks/2/63/{cid}` | 路径由 multihash 摘要前缀分两级目录 ⇒ 文件系统式块库，非 `bucket/key` 平铺 |
| 块以 CID 命名，`ETag` 就是 CID | 内容自校验，写入即不可变（immutable） |
| 每块单独发放 `auth_key` 且单独加密 | 授权粒度到块，说明存储层按块寻址 |
| 单块 4 MiB 固定、末块变长 | 上传/读取都以块为调度单位 |
| 多块文件有 UnixFS dag-pb 根 | 与 IPFS 数据模型兼容（但见第 3 节，不等于加入公共网络） |

## 3. 是否在公共 IPFS 网络（CONTRADICTED）

`delegated-ipfs.dev/routing/v1/providers/{cid}`：

| CID | 结果 |
|---|---|
| 本次自有根 `bafybeiesgdft…pck4` | `{"Providers":[]}`（16 B 空响应） |
| 3.15 GiB 根 `bafybeiebig24…hh4` | `{"Providers":[]}` |
| 自有叶子块 `bafkreiccgyrr…` | `{"Providers":[]}` |
| 对照（公共已知 CID `bafybeigdyrzt…bzdi`） | **30 432 B provider 列表** |

- 对照组能返回 provider，说明探测器与出口网络工作正常；自有 CID 全为空 ⇒ **没有向公共 DHT 公告**。
- `ipfs.io` / `dweb.link` 在本网络 `ConnectTimeout`（不可达），无法做反向抓取验证，故这一条按「无 provider 记录」判定，而非「抓取失败」判定。

## 4. 秒传/去重的作用域（CONFIRMED）

- 同账号内：已有 `content_identity` → `Create` 直接成功（83/84 ms，零传输）。
- 跨会话：拿 CID 数组 commit 到未上传块的会话 → `500 failed to make file from blocks: block not found`
  ⇒ 块按会话暂存，去重是**文件记录层**的映射，不是全局块去重。

## 5. 无法证实的部分（如实记录）

| 问题 | 结论 |
|---|---|
| 物理介质类型（SSD/HDD/分布式对象存储） | `SERVER_SIDE_PERSISTENCE_BACKEND_UNCONFIRMED` |
| 副本数 / 纠删码策略 | UNKNOWN（无接口暴露，且不做越权探测） |
| 块库是否跨地域 | UNKNOWN（回源节点名 `pic01–04.hhhtcu` 只能说明同一集群 4 节点） |
| 删除后数据保留多久 | 见 `persistence-test-results.md`：≥490 s 仍可读，上限未知 |

## 6. 分发层拓扑（实测推得）

```
客户端 ──TLS──> cacheXX.<isp>  (边缘, age 0–2)
                     │ 未命中/回源
                     ▼
              pic01–04.hhhtcu   (块存储集群, 4 节点轮转)
                     │
                     ▼
              自建块库 /ipfs/blocks/{a}/{bc}/{cid}
```

- 807 次取块的回源分布几乎均分（211/207/200/189），说明源站是 4 节点轮询，无单点。
- 边缘节点数十个（`cache03.tzmp`、`cache20.czmp`、`cache25.sxmp`、`cache48.sjzmp`…），按运营商/地域命名。
