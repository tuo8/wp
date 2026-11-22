# 上传流程（逐步实测记录）

> 全部步骤均由本次运行真实发出，原始请求/响应见 `02_network_capture/api_calls.jsonl`（1 006 条）、
> `03_upload/upload_*.json`、`03_upload/commit_kb1.json`。

## 0. 签名（CONFIRMED）

所有 gRPC-Web 调用必须带 5 个头，否则 `grpc-status 3: missing timestamp`：

```
plain = "/{service}/{method}{ts}{APPID}{APPVER}" [+ "Bearer " + token]
sign  = md5(plain + APPSECRET)

timestamp: <ms>
appid:     2dland/web
appversion:202603182100
authorization: Bearer <access_token>
sign:      <md5>
```

`APPSECRET` 与 `APPID` 取自官方 Web 客户端 `drive.2dland.cn/assets/index-C6KzmNsb.js`
（已存档 `07_storage_backend/webclient/`）。同一套签名对 App 侧同样有效 ⇒ 客户端与 Web 共用一套应用凭据。

## 1. 建任务 `CreateUploadTask`

```
POST https://drcdn2.2dland.cn/v6.services.pub.PubUserFile/CreateUploadTask   (gRPC-Web)
req:  parent=<dir identity>, name, mime, size
resp: upload_address = https://simple-file-proxy.2dland.cn/int/blocks
      session        = "upload_session::<uuid>_<rand>:ts::<ms>"
      block_size     = 4194304
      block_hash_type= 18   (sha2-256)
      block_codec    = 85   (0x55, raw)
```

`block_size` 由**服务端下发**，客户端不写死（实测 7 种尺寸全部拿到 4 194 304）。

## 2. 上传块

```
POST {upload_address}/{session}/{cid}
Content-Type: application/octet-stream
→ 201 Created
```

- 方法是 **POST**（PUT 实测失败/不适用）。
- 块内容 = 原始字节；`cid = CIDv1(raw, sha2-256)` = `0x01 0x55 0x12 0x20 || sha256(block)`。
- 单块 4 MiB 耗时 0.39–0.50 s（同会话串行），末块（2 MiB）0.24 s。

## 3. commit 块清单

```
POST {upload_address}/{session}
body = ["<cid>", "<cid>", ...]        ← 纯 JSON 字符串数组
→ 200 {"user_identity","parent","name","mime_type","size","content_identity"}
```

- 错误结构（对象数组 / `{"blocks":[...]}`）返回 `500 failed to unmarshal block list`。
- 服务端在这里**生成 UnixFS 根节点**并返回 `content_identity`：
  - 单块文件 → 根 = 该块 CID（`bafkrei…`，无额外节点）
  - 多块文件 → 根 = dag-pb 文件节点（`bafybei…`）
- 3.15 GiB 实测：807 个链接的根节点 **41 973 B，单层不分层**，与本地复算逐字节一致。

## 4. 实测上传记录

| 文件 | 字节 | 块数 | 根标识 | 耗时 | 落盘证据 |
|---|---|---|---|---|---|
| hca-kb1.bin | 1 024 | 1 | `bafkreiaq6bcz…bz44y` | 0.28 s | `upload_kb1.json` |
| hca-mb10.bin | 10 485 760 | 3 | `bafybeiesgdft…pck4` | 1.67 s | `upload_hca-mb10.bin.json` |
| hca-mb50.bin | 52 428 800 | 13 | `bafybeiejwiek…grzu` | 5.09 s | `upload_hca-mb50.bin.json` |
| CAWD-987-AI.mp4（既有对象） | 3 381 078 505 | 807 | `bafybeiebig24…hh4` | 由 App 侧上传，本次只做读取侧取证 | `05_cid_and_chunks/gb_*.json` |

- 100 KB / 1 MB / 4 MB / 100 MB 四种尺寸的块确实发出并入库（`api_calls.jsonl` 中有 **18 条 `CreateUploadTask`**，`block_size` 全部为 4 194 304、`block_codec` 全部为 85），但**逐文件的根标识/耗时未单独落盘**，此处标注 `NOT RECORDED`，不以推测值填充。
- 账号**上行额度 10 GB/天**，因此没有对 3.15 GB 对象做重复上传，只用读取侧（下载/播放）取证。
- 删除测试（`08_persistence_tests/delete_persist.json`）删除了其中若干测试对象，所以当前目录只剩 `hca-kb1-full.bin`、`hca-up-mb50.bin`。

## 5. 去重（秒传）（CONFIRMED）

```
PubUserFile/Create(parent, name, size, content_identity=<已存在 CID>)
→ 200，83 ms / 84 ms，未传输任何字节
```

反例：拿 CID 数组 commit 到**新 session**（块未上传）→
`500 {"error":"failed to make file from blocks: block not found"}`
⇒ 块按会话暂存，跨会话不自动命中；去重发生在「文件记录」层。

## 6. 客户端侧观察

- App 上传时连接 `simple-file-proxy.2dland.cn`（101.72.219.x / 101.26.38.162，server: Byte-nginx）。
- 传输引擎由本地 SQLite 驱动：`sync_task_config.global_status` 为 `paused` 时进程内**零流量**，改为 `running` 并重启后流量恢复（`07_storage_backend/client_metadata.sqlite`）。
- 上传**无客户端加密**：块即明文，服务端以 CID 命名做内容自校验。
