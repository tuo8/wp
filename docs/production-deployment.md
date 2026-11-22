# MyDrive production deployment (single node)

This deployment profile is intentionally single-node and uses SQLite for the local metadata stores plus MinIO for cold objects. It does **not** certify the existing multi-instance/PostgreSQL compose file as production-ready: several modules still open SQLite databases directly, so cluster-wide transaction consistency must be fixed and tested before scaling API replicas.

## 1. Prepare secrets and environment

Copy `deploy/.env.production.example` to `deploy/.env.production`. Replace every `REPLACE_...` value. Generate independent secrets, for example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Generate separate values for `MYDRIVE_SECRET`, `MYDRIVE_PRESIGN_SECRET`, `ARIA2_SECRET`, MinIO credentials, and `PAYMENT_WEBHOOK_SECRET`. Set `ADMIN_USERS` to trusted account usernames or user IDs. The first public registrant is no longer promoted to administrator; without an allowlist, administrative endpoints fail closed.

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
- `MAX_UPLOAD_BYTES` defaults to 10 GiB. Uploads are streamed to `UPLOAD_TEMP_DIR` and chunked from disk; ensure that volume has enough free space.
- `AUTO_OFFLOAD_COLD=0` is the safe default until cold-storage integrity and restore drills have passed. Enable automated eviction only after backups and restore tests are verified.
- `SFTP_ENABLE=0` by default. Enable only when required and restrict the port at the firewall.

## 4. Backup and recovery

Back up the complete `mydrive_data` volume and the `minio_data` volume consistently. A database-only backup is insufficient because file metadata references chunk objects and cold-storage objects. Test restoring both volumes to an isolated instance before accepting production data. Keep encryption/signing secrets in a secret manager and include them in the recovery plan without committing them to Git.

## 5. Current production-readiness limits

- Multi-node PostgreSQL mode is not yet certified because `share_manager.py`, `tiered_storage.py`, and other paths still use direct SQLite connections.
- SMS provider delivery and verified phone-binding are not implemented; debug codes are disabled by default.
- DNS validation in offline download is an application-layer defense; production network policy should also deny outbound access to private, loopback, link-local, and cloud metadata ranges.
- Cold-tier eviction remains disabled by default until integrity, concurrency, and disaster-recovery tests are completed.
