# 证据索引（每个结论 → 可回溯文件）

> 所有哈希由 `11_scripts/build_manifest.py` 现场计算并写入 `MANIFEST.json`，**没有任何手填哈希**。

## A. 对象与客户端身份

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| 官方包版本 1.0.24 / versionCode 24 / targetSdk 36 | `01_app_identity/aapt_badging.txt` | `versionName`、`versionCode`、`targetSdkVersion` |
| 官方包 SHA-256 | `01_app_identity/official_apk_sha256.txt` | 整行 |
| 官方签名证书指纹 | `01_app_identity/official_signing_cert.txt` | `SHA256:` 行 |
| 客户端为 Flutter + 本地 SQLite 同步引擎 | `01_app_identity/static/libapp_strings.txt`、`07_storage_backend/client_metadata.sqlite` | `libapp.so` 字符串、`sync_task_config` 表 |
| 传输引擎被本地 DB 开关控制 | `07_storage_backend/client_metadata.sqlite` | `sync_task_config.global_status` = `paused`/`running` |

## B. 网络面

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| API 在 `drcdn2.2dland.cn`（server: envoy） | `02_network_capture/live_probe.jsonl`、`api_calls.jsonl` | `host`、`server`、`/v6.services.pub.PubUserFile/*` |
| 域名清单与用途 | `02_network_capture/domain-inventory.csv` | 每行的 `category` + `observed_ip` |
| 实际对端 IP 与端口 | `02_network_capture/host_inventory.json`、`live_probe.jsonl` | `conns[].dst` |
| 失败轮次也留档 | `live_probe_run1_dead.jsonl`、`live_probe_run2_paused.jsonl` | 两轮零流量/未启动的原始记录 |
| 签名算法来源 | `07_storage_backend/webclient/index-C6KzmNsb.js` | `timestamp/appid/appversion/sign` 拼接逻辑 |
| TLS 参数 | `02_network_capture/tls_drcdn2.txt` | 握手摘要 |

## C. 上传

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| 4 MiB 分块、raw 编码、sha2-256 | `03_upload/upload_hca-mb10.bin.json`、`upload_hca-mb50.bin.json` | `block_size`、`n_blocks`、`block_cids` |
| 服务端生成 UnixFS 根 | `03_upload/commit_kb1.json` | `commit_body.content_identity` |
| 秒传 83/84 ms | `03_upload/dedupe_test.json`（反例）+ 报告 §3.5（正例来自 `api_calls.jsonl` 的 `Create` 记录） | `http`、耗时 |
| 18 次 CreateUploadTask（含 7 种尺寸） | `02_network_capture/api_calls.jsonl` | `method == CreateUploadTask`，`resp.16/17/18` |
| 测试语料与其哈希 | `03_upload/testcorpus_manifest.json` + `testcorpus/*.bin` | `sha256`、`size_bytes` |

## D. 下载

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| 网关整文件下载 + SHA-256 校验 | `04_download/direct_download.json` | `size`、`sha256`、`verified` |
| 分片逐块解密后逐字节相等 | `04_download/slice_download.json` | `root`、`children[].encrypt`、`sha256_ok` |
| App 侧真实下载产物 | `04_download/app_downloaded_kb1.bin`、`app_downloaded_mb50.bin` | 文件本体（哈希见 MANIFEST） |
| 1 KB 分片探测 | `04_download/slice_probe_kb1.json` | `encrypt`、`etag` |
| 3.15 GiB DAG 结构 + Range | `05_cid_and_chunks/gb_structure.json` | `root_node.links=807`、`cid_match=true`、`gateway_range_*` |
| 3.15 GiB 全量下载与并发标定 | `05_cid_and_chunks/gb_download.json` | `dag`、`concurrency_bench`、`full_download.sha256` |
| 逐块明细（807 条） | `05_cid_and_chunks/gb_per_block.json` | 每块 `via`、`age`、`fetch_ms`、`encrypt` |
| 首/尾块样本（可复核） | `05_cid_and_chunks/gb_head_sample.bin`、`gb_tail_sample.bin` | 与 `gb_download.json` 的 `sample_*_sha256` 对应 |

## E. 存储后端

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| 自有 CID 在公共 DHT 无 provider | `07_storage_backend/public_ipfs_probe.json` | `dht_provider_ours*` 为 16 B 空响应，`dht_provider_control` 为 30 432 B |
| 块库路径与两级分桶 | `04_download/slice_download.json`（`url`）+ 报告 §4.3 | `/ipfs/blocks/{a}/{bc}/{cid}` |
| 回源 4 节点 + 边缘多层 | `05_cid_and_chunks/gb_download.json` | `full_download.cdn_nodes` |

## F. 持久化

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| 删除后 ≥490 s 仍可按 CID 取块 | `08_persistence_tests/delete_persist.json` | 各 `tag` 的 `new_slice_addr.has_url`、`new_slice_fetch.http` |
| 预签名 URL 删除后仍 200 且 `age` 递增 | 同上 | `presigned_old_url.age` 0→12→73→194→495 |
| 列表在并发删除下出现短暂不完整 | 同上 | `listing` 长度 1 → 3 |

## G. 播放

| 结论 | 证据文件 | 关键字段 |
|---|---|---|
| 播放解析的是 `ipfs-gateway.qzy.xyz` | `10_raw_evidence/playback_dns.jsonl` | `t=DNS` 全部为该域名；`t=CONNECT` 为 101.72.219.149/166 |
| 播放期间无 slices 通道 | 同上 + `playback_probe.jsonl` | 无 116.136.20.x 连接 |
| 入向速率与暂停即停 | `10_raw_evidence/playback_rx.jsonl` | `acc` 归零时刻 17:37:07 |
| 播放 UI 与进度（02:00:32） | `10_raw_evidence/playback_screen3.png` | 截图 |
| 播放不落盘 | 报告 §3.3（前后 `du -sk` 均为 51 295 KB） | 该测量为即时命令，未落盘为文件；数值写在报告中 |

## H. 架构与报告

| 文件 | 说明 |
|---|---|
| `09_architecture/architecture.mmd` | Mermaid 架构图（含播放/下载双通道） |
| `12_reports/FINAL_CURRENT_STORAGE_AUDIT.md` | 主报告 |
| `12_reports/GB_FILE_DOWNLOAD_AND_PLAYBACK.md` | 3.15 GiB 专项 |
| `12_reports/upload-flow.md` / `download-flow.md` | 两条链路的逐步记录 |
| `12_reports/storage-backend-evidence.md` | 四层判定与无法证实项 |
| `12_reports/persistence-test-results.md` | 删除后窗口 |

## I. 交付时的安全处理（必须知悉）

1. **不包含任何账号凭据**：无 access_token、refresh_token、cookie、密码。凭据文件在 `capture/secrets/`（工作区之外），未进入本目录。
2. `11_scripts/hcc.py` 中原本硬编码的 `APPSECRET` 常量已替换为环境变量 `HCC_APPSECRET`（交付版不含该常量）。
3. `07_storage_backend/webclient/index-C6KzmNsb.js` 是**线上公开可下载的前端资源**，按证据完整性保留原样；其中的 `APPID/APPVER/APPSECRET` 是客户端常量（不构成某账号的凭据，单独持有它无法读取本账号数据），提取方法本身就是报告 §0 的复现步骤。
4. `02_network_capture/api_calls.jsonl` 中出现的 `eyJ…` 字符串是**分页游标**（base64 的 `user_identity/skip/limit`），不是令牌。
5. 所有测试对象都建在账号自建的 `hca-2026-11-23` 目录内，未触碰他人数据；未做任何服务端入侵行为。
