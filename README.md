# MyDrive

一个**内容寻址（IPFS 风格）网盘服务端**的独立实现：文件按 4 MiB 定长分块、每块以 CID 命名、文件根 CID 是真正的 UnixFS `dag-pb` 节点，因此同一份内容在任何节点上的 CID 完全一致，天然支持全局秒传。

服务端提供 HTTP/JSON + gRPC-Web 双协议、WebDAV、SFTP、切片（slices）边缘缓存与 BT/HTTP 离线下载，附带一个零依赖的单页 Web 客户端。

> 本项目的协议形态参考了某商业网盘客户端的**公开可观测行为**（请求结构、签名算法、分片寻址方式），用于互操作性研究。代码为独立实现，**不包含**任何第三方源码、私有密钥或受版权保护的内容。

---

## 目录

- [功能](#功能)
- [架构](#架构)
- [快速开始](#快速开始)
- [端口](#端口)
- [目录结构](#目录结构)
- [协议实现要点](#协议实现要点)
- [测试](#测试)
- [已知限制](#已知限制)

---

## 功能

**存储与文件**

- 4 MiB 定长分块 + SHA-256 CIDv1，块级去重（引用计数）
- 文件根 CID 为标准 UnixFS `dag-pb`：单块折叠、174 links/节点、balanced 分层
- 全局秒传：`(sha1, size)` 唯一索引，跨用户命中同一 `content_identity`
- 目录树、递归复制、批量重命名、回收站（软删/恢复/彻底删除）、搜索、排序
- 文件详情、预览、直链、批量操作

**协议层**

- gRPC-Web 二进制帧（`application/grpc-web+proto`）+ gRPC-Web JSON 双模式
- 手写 protobuf 编解码（`pb.py`），无 `protoc` 依赖
- 108 个 RPC / 62 条 REST 路由，含请求签名校验（MD5 拼接式）
- 账号全生命周期：注册、登录、刷新、登出、改密、注销、短信验证码、设备码授权、OAuth token

**传输与接入**

- HTTP 分段上传（`init → 逐块 PUT → complete`），秒传前置探测
- WebDAV（PROPFIND 207 / Range / 基本认证）
- SFTP（独立端口，paramiko 服务端实现，直接映射网盘目录树）
- 切片出口：预签名 `auth_key` + 会话绑定，Range/HEAD，ETag 即 CID

**离线下载**

- aria2 后端：HTTP/HTTPS 直链 + BitTorrent（magnet / `.torrent`）
- 真实状态机：`0-99` 排队 → `100+进度/2` 下载中 → `1000` 完成 → `<0` 失败
- 完成后按目录结构入库，多文件保留层级

**边缘缓存**

- LRU + TTL 内存缓存，`X-Bdcdn-Cache-Status` / `Age` / `via` 等真实回源与命中头
- 控制面：`prewarm`（预热）、`purge`（失效）、`stats`（命中率）

---

## 架构

```
                        ┌──────────────────────────────┐
   浏览器 / 客户端 ────▶ │  api_server.py  (FastAPI)    │
                        │  ├ /api/*       HTTP/JSON    │
                        │  ├ /v6.services.*  gRPC-Web  │
                        │  ├ /dav/*       WebDAV       │
                        │  └ /ipfs/blocks/*  slices    │
                        └───────┬──────────────────────┘
                                │
        ┌───────────────┬───────┴────────┬────────────────┐
        ▼               ▼                ▼                ▼
 ┌────────────┐  ┌────────────┐  ┌────────────┐  ┌──────────────┐
 │storage_    │  │auth_       │  │share_      │  │offline_      │
 │engine.py   │  │manager.py  │  │manager.py  │  │engine.py     │
 │            │  │            │  │            │  │              │
 │ 分块/CID   │  │ 账号/令牌  │  │ 分享/投诉  │  │ aria2 守护   │
 │ 去重/秒传  │  │ 验证码     │  │            │  │ 状态机/入库  │
 └─────┬──────┘  └────────────┘  └────────────┘  └──────┬───────┘
       │                                                 │
       ▼                                                 ▼
 ┌──────────────────────────┐                    ┌──────────────┐
 │ chunks/  按 CID 落盘      │                    │    aria2c    │
 │ meta/    SQLite 元数据    │                    │  BT / HTTP   │
 │ XOR 私有块格式（可选）     │                    └──────────────┘
 └──────────────────────────┘
```

**核心模块**

| 文件 | 职责 |
|---|---|
| `cid_engine.py` | CIDv0/v1 编解码、UnixFS `dag-pb` 组装、文件根 CID 计算 |
| `storage_engine.py` | 分块入库、块编解码（明文/XOR）、去重、秒传、目录树、回收站 |
| `grpc_compat.py` | gRPC-Web 帧解析、RPC 分派、签名校验、REST 路由映射 |
| `v6_extra.py` | 扩展 RPC 实现（账号、批量、共享、P2P 信令等） |
| `pb.py` | protobuf message 定义与手写编解码 |
| `offline_engine.py` | aria2 JSON-RPC 客户端、torrent 解析、任务状态机、完成后入库 |
| `slices_server.py` | 切片出口 + 边缘缓存 + 预签名校验 |
| `sftp_server.py` | SFTP 服务端（paramiko），读写直通网盘 |
| `webdav_gateway.py` | WebDAV 网关 |

---

## 快速开始

### 1. 安装依赖

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
```

### 2. 准备 aria2（可选，仅离线下载需要）

从 [aria2 releases](https://github.com/aria2/aria2/releases) 下载对应平台的二进制，放到：

```
tools/aria2/aria2-1.37.0-win-64bit-build1/aria2c.exe   # Windows
```

或用环境变量指定任意路径：

```bash
export ARIA2_EXE=/usr/local/bin/aria2c
```

没有 aria2 时服务照常启动，只是离线下载功能不可用。

### 3. 启动

```bash
# Linux / macOS
./start.sh

# Windows
启动.bat
```

或手动：

```bash
cd mydrive && python api_server.py
```

打开 <http://127.0.0.1:8765>，注册任意账号即可使用。

### 4. 配置（可选）

```bash
cp .env.example .env
```

`start.sh` 会读取环境变量；`api_server.py` 直接读 `os.environ`。

---

## 端口

| 端口 | 服务 | 说明 |
|---|---|---|
| `8765` | Web / HTTP API / gRPC-Web / WebDAV | 主入口 |
| `2022` | SFTP | 独立服务，凭据与网页登录一致 |
| `8766` | Slices 边缘缓存 | `/ipfs/blocks/2/<xx>/<cid>?auth_key=...` |
| `6800` | aria2 JSON-RPC | 离线下载引擎 |

端口均可通过环境变量覆盖。

---

## 目录结构

```
.
├── mydrive/                     # 服务端代码
│   ├── api_server.py            # 入口：装配所有服务
│   ├── cid_engine.py            # CID / UnixFS
│   ├── storage_engine.py        # 存储引擎
│   ├── grpc_compat.py           # gRPC-Web + REST
│   ├── v6_extra.py              # 扩展 RPC
│   ├── pb.py                    # protobuf 编解码
│   ├── auth_manager.py          # 账号体系
│   ├── share_manager.py         # 分享
│   ├── offline_engine.py        # 离线下载
│   ├── slices_server.py         # 切片 + 边缘缓存
│   ├── sftp_server.py           # SFTP
│   ├── webdav_gateway.py        # WebDAV
│   ├── sync_manager.py          # 同步
│   ├── encryption_layer.py      # 客户端加密
│   ├── static/index.html        # 单页 Web 客户端
│   └── *_test.py                # 测试
├── docs/
│   ├── architecture.md          # 实现说明与交接文档
│   ├── protocol-reverse-engineering.md
│   └── screenshots/
├── tools/
│   └── make_test_torrent.py     # 生成 BT 测试种子
├── start.sh / 启动.bat
├── requirements.txt
└── .env.example
```

---

## 协议实现要点

### 文件根 CID 是真 UnixFS

块 CID 是 `raw`（`bafkrei...`），文件根 CID 是 `dag-pb` + balanced 分层。要与其他实现逐字节对齐，有三个容易踩的坑：

1. **PBLink 的空 `Name` 必须显式编码成 `12 00`**，省略会让节点从 113 字节变成 109 字节，CID 全错；
2. 每节点最多 **174** 个 links，超过就多做一层；
3. 单块文件直接折叠成分片自身的 raw CID，不套 `dag-pb`。

### 请求签名

```
sign = MD5("/" + service + "/" + method + timestamp + appid + appversion
           [+ "Bearer " + token] + secret)
```

头里带 `timestamp` / `appid` / `appversion` / `sign`，登录后额外带 `authorization`。服务端默认容忍 5 分钟时钟偏移。

### 切片凭据

`auth_key = <expire_ts>-<kid>-<user>-<sig>`，配合 `session=-<32hex>` 查询参数。签名材料覆盖「路径 + 过期时间 + kid + 用户 + session」，改动任一字段立即失效。`kid` 与 `session` 每次签发都重新随机。

### 私有块格式（可选）

部分来源的块以**整块 XOR `0x75`** 落盘，CID 仍覆盖明文——因此 CID 校验和解密可以独立进行。存储层用 `chunks.format` 列标记，读路径自动解码，切片出口可选择输出密文或明文（`?decrypt=1`）。

```python
# 存储层开关，不需要时保持默认明文即可
engine.chunk_file(path, chunk_format=1)   # 1 = XOR 0x75
```

---

## 测试

每个模块都有独立测试，覆盖单元与端到端：

```bash
cd mydrive

python codec_test.py            # 块编解码（明文/XOR 往返、CID 一致性）
python cdn_test.py              # 切片鉴权 + 边缘缓存 + 离线块密文
python slices_test.py           # 切片出口（Range/HEAD/过期/伪造签名）
python e2e_test.py              # 登录 → 上传 → 秒传 → 下载 → 跨用户隔离
python v6_extra_test.py         # 扩展 RPC
python v6_account_test.py       # 账号闭环 / 批量 / 投诉 / P2P 信令
python sftp_test.py             # SFTP 读写与目录映射
python offline_test.py          # 离线任务状态机
python bt_test.py               # BT 全链路（需 aria2 + 本地种子服务）
python verify_upload_shape.py   # 上传链路形态校验
```

多数测试需要服务已在 `127.0.0.1:8765` 运行。`bt_test.py` 还需要一个提供 `.torrent` 的本地静态服务，可用 `tools/make_test_torrent.py` 生成。

---

## 已知限制

| 项 | 状态 |
|---|---|
| 原生 HTTP/2 gRPC | 未实现，仅 gRPC-Web（前置 Envoy 或 hypercorn 可补齐） |
| 短信 / 邮件验证码 | lab 模式下直接返回，未接真实网关 |
| 对象存储后端 | 本地磁盘 + SQLite，无多机调度 |
| 边缘缓存 | 进程内 LRU，非分布式 CDN |
| 文件版本历史 | `File.version` 为递增计数，无快照回滚 |
| 回收站保留期 | 无自动清理策略 |
| BT 做种/限速策略 | 依赖 aria2 默认行为 |
| 安卓客户端 | 未实现，仅 Web 端 |

---

## 免责声明

本项目用于**个人学习与服务端架构研究**：内容寻址存储、UnixFS 结构、gRPC-Web 兼容层、边缘缓存与 P2P 下载链路。

- 不含任何第三方源码、反编译产物或私有密钥；
- 不提供、不代理任何受版权保护的内容；
- 与其他服务互通时，请自行确认符合对方服务条款与当地法律；
- 用于生产环境前，务必替换默认密钥、启用 TLS、限制访问来源。

## License

MIT


---

## 新增增强功能（对标清真云 & PikPak）

### 1. 有限空间存大文件：冷热分级存储 + 压缩 + 垃圾回收 (`tiered_storage.py`)
- **热缓存 + 冷对象存储分层**：本地磁盘仅作为有上限的 LRU 热缓存（`LOCAL_CACHE_MAX_BYTES`），超出阈值自动将冷分块迁移到 **S3 兼容对象存储**（内置零依赖 AWS SigV4 客户端，支持 MinIO / Cloudflare R2 / Backblaze B2 / 阿里云 OSS / AWS S3）或 **外部挂载目录 / NAS / Rclone 目录**，并释放本地副本。
- **按需拉取（4 MiB 粒度）**：读取或云播大文件时，仅按需从冷存储拉取当前播放/下载的分块，几十 GB 视频无需整文件落盘。
- **智能分块压缩**：可选开启 `CHUNK_COMPRESSION=1`，对文档/日志/源码自动 zlib 压缩，压缩率不足 8% 的视频/压缩包自动跳过。
- **引用计数垃圾回收与回收站清理**：支持 `POST /api/storage/gc`、一键清空回收站 `POST /api/trash/empty` 及过期回收站清理 `POST /api/trash/purge-expired`。

### 2. 云播与媒体体验 (`media_service.py`)
- **FFmpeg 按需 HLS 转码/封装**：支持 `原画直连 / HLS 原画(Remux) / 1080P / 720P / 480P` 切换，兼容 MKV/HEVC/AVI 等浏览器非原生格式。
- **字幕自动挂载与转换**：自动扫描同目录 `.srt` / `.ass` / `.vtt` 字幕并实时转为 WebVTT。
- **封面缩略图、逐帧雪碧图预览与续播记忆**：持久化保存播放进度、倍速（0.5x–2.0x）与画质偏好。

### 3. 离线下载增强与 Telegram 机器人 (`offline_engine.py`, `telegram_bot.py`)
- **跨用户全局磁力/URL 零拷贝秒存**：任意用户已离线入库的 `infohash` 或链接，其他用户提交时毫秒级克隆元数据入库，零流量零额外磁盘。
- **种子解析选文件、批量离线、暂停/恢复与全局限速**。
- **类 PikPak Telegram 机器人**：支持 Webhook (`/api/telegram/webhook`) 与 Web 内置模拟器 (`/api/telegram/simulate`)，绑定账号后转发磁力/链接自动离线。

### 4. Web 端文件管理与公开分享闭环 (`extra_routes.py`, `static/enhancements.js`)
- 智能分类（收藏夹 / 视频 / 图片相册 / 音频 / 文档）、跨目录全局搜索、真实复制、文件版本快照与回滚、悬浮批量操作栏（含打包 ZIP 下载）以及独立公开分享提取码与一键转存页 `/s/{share_id}`。

### 新增端到端测试
```bash
cd mydrive
python3 pikpak_halal_test.py   # 35 项全功能自动化验收测试
```


## 生产部署

生产环境请使用 [单机生产部署指南](docs/production-deployment.md) 和 `deploy/docker-compose.production.yml`。现有多实例 PostgreSQL 配置仍为实验性，尚未通过完整的数据一致性验证。
