# 清真云审计 · 原始证据与脚本

本目录是 [`halalcloud-audit-report.md`](../halalcloud-audit-report.md) 的可复核材料：**实际执行过的脚本** + **实际落盘的脱敏结果**。

- 采集窗口：2026-11-22 20:45 – 22:40（UTC+8）
- 采集者：WorkBuddy 自动化审计代理
- 对象：本人合法账号 + 自造测试文件；无第三方数据、无压测、无付费操作
- 目录：`evidence/`（结果）、`scripts/`（代码）、`MANIFEST.json`（逐文件 SHA-256）
- 文件数：**32 个** = 17 份证据 + 13 个脚本 + 本 README + MANIFEST

---

## 1. 脱敏规则（全部文件已按此处理）

| 类型 | 处理 |
|---|---|
| 用户 ID / 文件 ID / 分享 ID（32 位十六进制） | `[REDACTED_ID]` |
| `upload_session::…`、`lock__user_operation__…` | `[REDACTED]` / `lock__user_operation__[REDACTED_ID]` |
| URL 查询串（`auth_key`、`session`、`filename`…） | 只保留参数名，值 → `[REDACTED]` |
| `session` / `token` / `auth_key` / `password` / `secret` / `sign` / `download_address` 等键 | 整段丢弃 |
| 账号私有内容（用户昵称、WebDAV 用户名与口令、分享条目、回收站条目名） | 整体移除，只保留条数或调用状态 |
| 脚本中的 `appsecret`、用户 ID、账号名 | 改为环境变量 `HC_APP_SECRET` / `HC_USER_ID` 读取，默认值为 `[REDACTED]` |

**未发布的三类中间文件**（仍在原工作目录，不入库）：

| 文件 | 不发布原因 |
|---|---|
| `oauth_refresh.json` | 含可用 access/refresh JWT 原文 |
| `find_entry.json` / `last_upload.json` | 含用户 ID、上传会话串与提交响应原文，已被 `uploads.json` 覆盖 |
| `gateway_probe.json` / `providers.json` | 与已发布的 `gateway_probe_curl.json` / `providers_curl.json` 重复，且旧版把网络错误记成 `status: null`，易误读 |

---

## 2. 证据清单与读法

### 2.1 上传链路

| 文件 | 内容 | 关键结论 |
|---|---|---|
| `evidence/uploads.json` | 6 个测量文件：建任务、每片 CID、HTTP 状态、耗时、提交响应 | `block_size=4194304`、`block_codec=85`、`hash_type=18`；每片独立 CID；提交后返回根 CID |
| `evidence/root_state_now.json` | 复核时根目录残留的 8 个 `hc-*` 对象 | 3 个同名不同文件共享同一 CID → 内容级去重成立 |

### 2.2 下载与哈希

| 文件 | 内容 | 关键结论 |
|---|---|---|
| `evidence/downloads.json` | 6 个文件的分片/直链下载、字节数、SHA-256、Range 状态、响应头 | 直链 6/6 一致；分片需按该片 `encrypt` 解码 |
| `evidence/verify_20m_per_slice.json` | 20 MiB 文件 5 片逐片解码复核 | 5 片密钥 98/117/99/106/51 各不相同，逐片解码后整文件 SHA-256 与本地一致 |
| `evidence/slice_raw.bin` | 一片 4 KiB 密文原始字节 | 内容是本次自造测试文件，可自行 XOR 验证 |

> `downloads.json` 里 20 MiB 那条的 `match_after_xor_key` 为 `false`，是**脚本缺陷**：该脚本把第 0 片的 `encrypt` 套用到全部 5 片。`verify_20m_per_slice.json` 是事后按片复核的正确结果。两处并存，不隐藏。

### 2.3 删除语义

| 文件 | 对象 | 关键结论 |
|---|---|---|
| `evidence/delete_cache.json` | 1 MiB 文件（内容唯一 CID） | Trash + DeleteTrash 后，API 拒绝签发（grpc 5），但旧直链与分片地址仍 200 且内容一致 |
| `evidence/delete_semantics.json` | 1 356 B 视频文件 | 同上；回收站列表 100 条内未见目标（分页未穷尽） |
| `evidence/delete_window.json` | 4 157 B 文本，删除后 0/30/60/120/240 s 采样 | 240 s 内每次均 200 + 哈希一致 |

> **重要限制**：`delete_window` 用的文件与另外 4 个留存文件**内容相同（同 CID）**，因此"仍可下载"可能只是因为块仍被其他文件引用，不能单独证明"删除后块未回收"。唯一 CID 的证据是 `delete_cache.json` / `delete_semantics.json`，但它们没有做时间序列。这一点在报告 §4.2 已明确标注。

### 2.4 IPFS 层

| 文件 | 内容 | 关键结论 |
|---|---|---|
| `evidence/providers_curl.json` | 3 个自有 CID 的 provider 查询 | 全部 `{"Providers":[]}` |
| `evidence/providers_positive_control.json` | 同一接口查公开已知 CID | 返回 9 个 provider → **接口本身可用**，空结果是针对这些 CID 的 |
| `evidence/gateway_probe_curl.json` | 6 个网关 × 3 个 CID | 全部失败，但失败原因在网络层 |
| `evidence/gateway_probe_disposition.json` | 失败归因 + 阳性对照 | `429`（限流）/ 本机 TLS 吊销链失败；**对照 CID 同样失败** ⇒ 该通道无判别力 |

> 结论口径：**"未在任何可查的公共 DHT 路由上发现 provider 记录"** ≠ "全球公共 IPFS 网络里绝对没有节点"。DHT 查询只反映 delegated-ipfs.dev 这一路由节点的视角，且 provider 记录会过期（默认 24 h 量级）。公共网关能否取回，本次**没有取得有效样本**，不作结论。

### 2.5 协议与账户

| 文件 | 内容 |
|---|---|
| `evidence/rest_routes.json` | 从官方 proto 解析：113 RPC（5 个注释态）/ 62 REST，含 service→rpc→path |
| `evidence/account_readonly.json` | 配额数值、预览接口、DAV/SFTP 接口可用性（私有内容已剔除） |
| `evidence/dav_headers.txt` / `dav_propfind.xml` | WebDAV `PROPFIND /` 返回 207 的响应头与报文 |
| `evidence/gh_repos.json` | GitHub 组织 9 个仓库的公开元数据（裁剪字段） |

---

## 3. 脚本清单

| 脚本 | 作用 | 是否写账号 |
|---|---|---|
| `hc.py` | 最小 gRPC-Web 客户端：签名、调用、令牌刷新 | 只刷新令牌 |
| `gen_files.py` | 生成 6 个固定种子的测试文件（文本/1M/4M/20M/PNG/MP4） | 否 |
| `upload_test.py` | 单文件：建任务 → 逐片 POST → 提交 | 是 |
| `run_uploads.py` | 串行上传全部文件并写 `uploads.json` | 是 |
| `download_test.py` | 分片 + 直链下载，比对 SHA-256、记录 Range 与响应头 | 否（只读） |
| `verify_20m.py` | 20 MiB 逐片按 `encrypt` 解码复核 | 否 |
| `delete_cache_test.py` | 唯一 CID 文件删除后旧地址可读性 | 是（删自建文件） |
| `delete_window.py` | 删除后 240 s 时间序列采样 | 是（删自建文件） |
| `ipfs_check2.py` | DHT provider 与公共网关探测 | 否 |
| `ipfs_disposition.py` | provider 阳性对照 + 网关失败归因 | 否 |
| `parse_protos.py` | 从 proto 提取 RPC/REST 清单 | 否 |
| `account_probe.py` | 配额/DAV/预览只读采样 | 否 |
| `cid_nodes.mjs` | 用 `ipfs-unixfs-importer` 离线复算根 CID 与 DAG 统计 | 否 |

### 复现步骤

```bash
# 0) 依赖
pip install -r ../requirements.txt        # 仅标准库 + 少量第三方（见下）
npm i ipfs-unixfs-importer@17 blockstore-core multiformats @ipld/dag-pb

# 1) 凭据（自行提供，不随仓库分发）
export HC_APP_SECRET='...'   # Web 前端包内的共享密钥
export HC_USER_ID='...'      # 账号根目录 identity
# 令牌：把 {access_token, refresh_token} 写入 audit/.token.json

# 2) 生成测试文件
python gen_files.py

# 3) 上传 / 下载 / 复核
python run_uploads.py
python download_test.py
python verify_20m.py

# 4) CID 独立复算
node cid_nodes.mjs ../testfiles/hc-test-20m.bin 4194304
node cid_nodes.mjs ../testfiles/hc-test-20m.bin 1048576   # 应得到不同根，反证不是 1 MiB 分片

# 5) IPFS 层
python ipfs_disposition.py

# 6) 删除语义（会删除自建测试文件）
python delete_cache_test.py
python delete_window.py
```

> 脚本默认从 `audit/` 目录运行，`testfiles/` 与 `evidence/` 为相对路径；路径可按需调整。

---

## 4. 明确无法找回 / 未采集的材料

这些项目在报告中已标注为「未验证 / 未测试」，**不使用推测值填充**：

| 项 | 状态 | 原因 |
|---|---|---|
| 分享创建成功时的原始响应（share identity） | **未落盘** | 当时只在终端打印，未写 JSON；报告 §5.2 已降级为 **B（接口存在，未留存响应）** |
| 去重实验的独立证据文件 | **未落盘** | 但可由 `root_state_now.json`（3 个不同名文件同 CID）+ `uploads.json` 交叉验证 |
| 匿名分享页下载行为 | 未测试 | 分享页为 SPA，需浏览器自动化 |
| 视频预览 / 转码 | 未测试 | 本机无 ffmpeg，无合规自制视频 |
| Android 端逐项点击 | 未完成 | 审计中途 USB 断连 |
| 单文件大小上限 | 未验证 | 未上传超大文件（避免占用额度） |
| 元数据数据库类型 / 是否 pin | 不可得 | 无接口暴露，需服务端视角 |
| 公共网关能否取回 | **无效样本** | 本机网络对 4 个公共网关均不可达或被 Cloudflare 拦截 |

---

## 5. 证据强度标记

报告中沿用统一标记：

- **A**：本次实测，有落盘证据
- **B**：源码/协议定义明确，或 UI 可见，但未实测
- **C**：官方公开文字（官网、README）
- **D**：由 A/B 推断，无直接证据
- **未验证 / 未测试**：没有可采信样本，不做结论
