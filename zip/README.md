# halalcloud-current-audit — 清真云（6pan / HalalCloud）当前线上存储架构取证

本目录只包含**本次运行**产生的证据，历史资料隔离在 `historical_reference/`（其中内容一律标注
`HISTORICAL_REFERENCE_NOT_CURRENT_EVIDENCE`，本次未引用）。

- 取证时间：2026-11-23 01:14 — 01:45（UTC+8）
- 账号：本机自有账号（loli001），6.8K 文件 / 12 TB 配额 / 上行额度 10 GB/天
- 客户端：`com.halalcloud.diskmanager.halalcloud` 1.0.24 (24)，targetSdk 36，Flutter
- 测试目录：账号自建 `/hca-2026-11-23/`（未触碰他人数据，未做任何服务端入侵）

## 先读哪几份

| 文件 | 内容 |
|---|---|
| `12_reports/FINAL_CURRENT_STORAGE_AUDIT.md` | 主报告（结论 + 证据等级 + 未完成项） |
| `12_reports/GB_FILE_DOWNLOAD_AND_PLAYBACK.md` | 3.15 GiB 大文件：下载 vs 播放的存储路径 |
| `12_reports/upload-flow.md` / `download-flow.md` | 两条链路的逐步实测 |
| `12_reports/storage-backend-evidence.md` | 上传/处理/持久化/分发 四层判定 |
| `12_reports/persistence-test-results.md` | 删除后 ≥490 s 仍可重新取块 |
| `12_reports/evidence-index.md` | 每条结论 → 具体证据文件与字段 |
| `09_architecture/architecture.mmd` | 架构图（Mermaid） |
| `MANIFEST.json` | 全量文件清单 + 现场计算的 SHA-256 |

## 目录约定

```
00_scope/             任务范围与边界
01_app_identity/      官方包哈希、签名证书、aapt、静态字符串
02_network_capture/   端点清单、调用日志、TLS、DNS/IP
03_upload/            7 种尺寸测试语料 + 上传记录 + 目录列表
04_download/          两条通道的下载记录 + App 侧真实下载产物
05_cid_and_chunks/    3.15 GiB DAG 结构、全量下载、逐块明细、首尾样本
06_protocol_analysis/ 协议字段推导（内容并入 12_reports/upload-flow.md）
07_storage_backend/   公共 IPFS 探测、客户端 SQLite、Web 客户端静态资源
08_persistence_tests/ 删除后持久化窗口
09_architecture/      Mermaid 架构图
10_raw_evidence/      App 运行时原始采集（Frida JSONL + 截图）
11_scripts/           全部可复现脚本
12_reports/           报告
historical_reference/ 历史资料隔离区（本次为空）
```

## 复现

```bash
# 依赖：python 3.15（requests、frida）、adb、frida-server/gadget、账号 access_token
python 11_scripts/t25_list_gb.py        # 列目录
python 11_scripts/t26_gb_structure.py   # DAG 结构 + 网关 Range
python 11_scripts/t27_gb_download.py    # 807 块全量下载 + 并发标定
python 11_scripts/t24_delete_persist.py # 删除后持久化窗口（会真删测试对象）
python 11_scripts/build_manifest.py     # 重新计算 MANIFEST
```

签名算法所需的 `APPSECRET` 在交付版 `11_scripts/hcc.py` 中已改为环境变量 `HCC_APPSECRET`；
提取方式见 `12_reports/upload-flow.md` §0（来源是官方 Web 客户端的公开 JS）。

## 安全说明

- 不含任何 access_token / refresh_token / cookie / 密码；凭据文件在工作区外的 `capture/secrets/`。
- 测试对象都在账号自建的测试目录内；删除测试只删自己刚上传的对象。
