# 交付范围说明（DELIVERY SCOPE）

> 结论先说：**不需要把 238 MB 全交付。** 其中约 223.5 MiB（93.7%）是"可重建的随机测试数据与下载副本"，
> 它们的证据价值已经在 JSON 里被固化成 SHA-256 + 字节数，文件本身不必随包走。
> 真正构成"证据"的只有 **8.9 MiB**。

---

## 1. 体积构成与判断

| 分层 | 内容 | 体积 | 是否必须交付 | 理由 |
|---|---|---|---|---|
| A 必备 | 10 份报告、架构图、`02_network_capture/`（抓包与域名清单）、`08_persistence_tests/`、`07_storage_backend/`（除 JS 外）、`05_cid_and_chunks/*.json`、`03_upload/*.json`、`04_download/*.json`、`11_scripts/`、`MANIFEST.json` | ≈ 1.0 MiB | **是** | 所有结论的唯一出处；哈希可自校验 |
| B 建议保留 | `07_storage_backend/webclient/index-C6KzmNsb.js`（4.4 MiB，appsecret 已脱敏）、`10_raw_evidence/*.jsonl`（310 KiB）、3 张播放器截图（2.2 MiB） | ≈ 6.9 MiB | 建议 | JS 是"`storeType` 是死代码"这唯一一条结论的原始依据；截图是播放行为的旁证 |
| C 可剔除 | 7 个随机测试语料、2 个 App 下载副本、2 个首尾采样块 | **223.5 MiB** | **否** | 纯随机字节或服务端内容副本；字节数、SHA-256、块 CID 已全部记录 |

实测：目录 `233 MiB` → 完整包 `249.7 MB`（zip）→ **精简包 `4.19 MB`（zip，108 项）**。

## 2. 被剔除的 11 个文件（清单在 `12_reports/EXCLUDED_BINARIES.sha256`）

| 文件 | 字节 | 为什么可以剔除 | 怎么重建 |
|---|---|---|---|
| `03_upload/testcorpus/hca-kb1.bin` | 1 024 | 随机语料 | `os.urandom` 同尺寸即可；其 SHA-256 与上传后的 `content_identity` 已在 `upload_kb1.json` |
| `…/hca-kb100.bin` | 102 400 | 同上 | 同上 |
| `…/hca-mb1.bin` | 1 048 576 | 同上 | 同上 |
| `…/hca-mb4.bin` | 4 194 304 | 同上（正好一块） | 同上 |
| `…/hca-mb10.bin` | 10 485 760 | 同上；根 CID `bafybeiesgdft…pck4` 已复算一致 | 同上 |
| `…/hca-mb50.bin` | 52 428 800 | 同上 | 同上 |
| `…/hca-mb100.bin` | 104 857 600 | 同上 | 同上 |
| `04_download/app_downloaded_kb1.bin` | 1 024 | App 下载副本，SHA-256 与源一致已记录 | 重新从 App 下载 |
| `04_download/app_downloaded_mb50.bin` | 52 428 800 | 同上 | 同上 |
| `05_cid_and_chunks/gb_head_sample.bin` | 4 194 304 | 3.15 GB 文件首块采样，内容 = 源文件 `[0, 4 MiB)` | 用 `t27_gb_download.py` 再取 |
| `05_cid_and_chunks/gb_tail_sample.bin` | 4 663 785 | 末两块采样 | 同上 |

合计 **11 个文件 / 234 406 377 B（223.5 MiB）**。

> 注意：这些文件被剔除**不影响任何一条结论的可验证性**——所有"我拿到了 X 字节、SHA-256 是 Y、CID 是 Z"的断言，
> 其 X/Y/Z 都在 `03_upload/*.json`、`04_download/*.json`、`05_cid_and_chunks/*.json`、`MANIFEST.json` 里。

## 3. 两个包，按场景取一个

| 包 | 大小 | 条目 | SHA-256 | 适用 |
|---|---|---|---|---|
| `halalcloud-current-audit.zip` | 约 249.7 MB | 约 121 | 以 `MANIFEST.json` 与打包命令输出为准 | 需要"原样复现、不看脚本也能核字节"的场合（归档、审计留痕） |
| `halalcloud-current-audit-lite.zip` | **约 4.2 MB** | 约 110 | 以打包命令输出为准（每次增删文档都会变） | 评审、传阅、邮件/IM 发送、代码仓库提交 |

> 两个包的 SHA-256 每次重新打包都会变（zip 内含时间戳），**核验一律以最新一次打包输出为准**，
> 包内文件的哈希以 `MANIFEST.json`（完整包）与 `EXCLUDED_BINARIES.sha256`（剔除项）为准。

lite 包内保留了 `EXCLUDED_BINARIES.sha256`，拿到完整包或原始目录后可直接校验：

```bash
# 把被剔除的文件放回原路径后
cd halalcloud-current-audit && sha256sum -c 12_reports/EXCLUDED_BINARIES.sha256
```

## 3.1 lite 包内 `MANIFEST.json` 的口径

`MANIFEST.json` 记录的是**取证目录里的全部 119 项**（含被剔除的 11 项），这样完整包与 lite 包共用同一份清单。
在 lite 包里核对时，用 `12_reports/EXCLUDED_BINARIES.sha256` 判断哪 11 项不在包内即可——两者互补，不冲突。

## 4. 如果对方强制要求"全量原件"

只需要回答一句：**全量包里那 223.5 MiB 全是 `os.urandom` 产生的随机字节和服务端内容的本地副本，不含任何独有信息。**
若确需提交，用完整包即可；若担心体积，可只补 3 个"有语义"的：`hca-mb10.bin`（10 MiB，多块 DAG 的最小样本）、
`hca-mb100.bin`（100 MiB，25 块）、`gb_head_sample.bin`（4 MiB，3.15 GB 文件的首块）——合计 119 MiB，其余 8 个可永久剔除。
