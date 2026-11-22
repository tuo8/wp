# MyDrive production deployment (single node)

This deployment profile is intentionally single-node and uses SQLite for local metadata plus a configurable remote chunk backend. CloudFlare ImgBed can be selected as the authoritative Telegram-backed object store, with local SSD retained as a hot cache; MinIO/S3 remains available by setting COLD_STORAGE_TYPE=s3. Active API modules now route their metadata operations through the shared database adapter, but the legacy `offline_downloader.py` helper still opens a separate SQLite file and is not wired into the active API runtime. The multi-instance/PostgreSQL compose file remains experimental until that legacy path is migrated or explicitly retired and a real PostgreSQL integration/restore test suite passes.

## 1. Prepare secrets and environment

Copy `deploy/.env.production.example` to `deploy/.env.production`. Replace every `REPLACE_...` value. Generate independent secrets, for example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Generate separate values for `MYDRIVE_SECRET`, `MYDRIVE_PRESIGN_SECRET`, `ARIA2_SECRET`, MinIO credentials, ImgBed upload/admin credentials, and `PAYMENT_WEBHOOK_SECRET`. Keep ImgBed secrets only in the deployment environment; never commit real values. Set `ADMIN_USERS` to trusted account usernames or user IDs. The first public registrant is no longer promoted to administrator; without an allowlist, administrative endpoints fail closed.

Pin `PYTHON_BASE_IMAGE`, `MINIO_IMAGE`, and `MINIO_MC_IMAGE` to reviewed versions. Prefer immutable image digests. The example file deliberately uses placeholders for MinIO images rather than guessing a release tag.

## 2. Start

```bash
cd deploy
cp .env.production.example .env.production
# Edit .env.production, then:
docker compose --env-file .env.production -f docker-compose.production.yml up -d --build
```

The API, Slices, and optional SFTP ports bind to `PUBLIC_BIND_IP` (default `127.0.0.1`). Set `SLICES_PUBLIC_URL` to the HTTPS URL clients can reach. Put a maintained TLS reverse proxy in front of both the API and Slices endpoints before public exposure. MinIO ports are internal to the Compose network and are not published to the host.

## 3. Important runtime settings

- `MYDRIVE_ENV=production` requires strong `MYDRIVE_SECRET`, `MYDRIVE_PRESIGN_SECRET`, and `ARIA2_SECRET` values at startup. Known development defaults are rejected.
- `MYDRIVE_AUTH_REQUIRED=1` is mandatory for protected API routes.
- `MYDRIVE_SMS_DEBUG=0` is mandatory in production. No real SMS gateway is implemented yet, so SMS-based recovery remains unavailable until a provider adapter is added. Do not enable debug code exposure on an internet-facing instance.
- Passwords require at least 10 characters. Password changes, resets, and account-wide logout increment a token version, invalidating already-issued access tokens and refresh tokens.
- Payment webhooks must sign the raw request body with `PAYMENT_WEBHOOK_SECRET` and include `order_no`, `amount_cents`, and `currency`; mismatched amounts/currencies are rejected. A real payment-provider adapter and live provider integration test are still required before accepting money.
- `MAX_UPLOAD_BYTES` defaults to 10 GiB. Uploads are streamed to `UPLOAD_TEMP_DIR` and chunked from disk; ensure that volume has enough free space.
- To use ImgBed as the primary chunk store, set `COLD_STORAGE_TYPE=imgbed`, `IMGBED_PRIMARY_STORAGE=1`, `IMGBED_BASE_URL`, and `IMGBED_AUTH_CODE`. Configure `IMGBED_ADMIN_USERNAME` and `IMGBED_ADMIN_PASSWORD` so permanent garbage collection can delete remote objects. Each MyDrive 4 MiB CID block is uploaded to ImgBed's Telegram channel under a deterministic `<CID>.bin` name, then downloaded and SHA-256 verified before the upload is accepted. Local SSD remains a bounded hot cache via `LOCAL_CACHE_MAX_BYTES`. The adapter targets the legacy artai8 ImgBed API: `POST /upload`, `GET /file/<id>`, and `DELETE /api/manage/delete/<id>`; it does not use the newer upstream chunked-upload API. Configure ImgBed's KV namespace, Telegram Bot token and channel ID, upload auth code, and admin Basic Auth credentials. The adapter disables ImgBed's cross-channel auto-retry so a failed Telegram upload cannot silently land in another storage channel. The current Telegram Bot API `getFile` download limit is 20 MB, so the existing 4 MiB MyDrive blocks fit comfortably. Each block still creates a separate Telegram message; high-volume/large-file use must account for Telegram per-chat rate limits and channel capacity. Important: this legacy ImgBed delete API removes the file's KV metadata and purges its CDN cache, but its current Telegram delete path does not delete the original Telegram channel message. MyDrive deletion is therefore a logical unlink, not guaranteed Telegram storage reclamation; channel cleanup requires a separate ImgBed enhancement that records Telegram message IDs and invokes Telegram's deleteMessage method.
- Keep `AUTO_OFFLOAD_COLD=1` when using primary ImgBed storage. For the existing MinIO/S3 backend, set `COLD_STORAGE_TYPE=s3` and `IMGBED_PRIMARY_STORAGE=0`. Switching an existing deployment between cold backends is not an automatic migration; keep the previous backend online and migrate/verify data before changing it. The deployment environment variables override an older saved `tiered_storage.json` backend selection, so setting `COLD_STORAGE_TYPE=imgbed` in the Compose environment takes effect even if the existing data volume previously recorded `s3`.
- `SFTP_ENABLE=0` by default. Enable only when required and restrict the port at the firewall.

## 4. Backup and recovery

Back up the complete `mydrive_data` volume and the selected remote object store. With ImgBed, back up its KV metadata and Telegram channel/Bot configuration as well as MyDrive's database; a database-only backup is insufficient because file metadata references remote CID objects. Test restoring both volumes to an isolated instance before accepting production data. Keep encryption/signing secrets in a secret manager and include them in the recovery plan without committing them to Git.

## 5. Current production-readiness limits

- Multi-node PostgreSQL mode is not yet certified. The adapter has compatibility support for active managers, but no real PostgreSQL end-to-end test or failover/restore drill has been run; the legacy `offline_downloader.py` helper remains SQLite-only.
- SMS provider delivery and verified phone-binding are not implemented; debug codes are disabled by default.
- DNS validation in offline download is an application-layer defense; production network policy should also deny outbound access to private, loopback, link-local, and cloud metadata ranges.
- ImgBed primary mode has adapter-level upload/read/delete, retry, backend-identity, and checksum tests, but a live end-to-end test against the deployed ImgBed instance and Telegram channel, restore drill, and outage/failover test are still required before production data is entrusted to it. ImgBed is the single remote primary in this mode, not a dual-write replica of MinIO; keep independent backups.
