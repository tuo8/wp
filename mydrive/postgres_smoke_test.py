"""PostgreSQL integration smoke tests for active MyDrive managers.

Run with DATABASE_URL pointing at an isolated PostgreSQL test database.
"""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from auth_manager import AuthManager
from commercial_manager import CommercialManager
from db_adapter import connect_db, is_postgres_enabled
from media_service import MediaService
from offline_engine import OfflineEngine
from share_manager import ShareManager
from storage_engine import StorageEngine
from sync_manager import SyncManager
from telegram_bot import TelegramBotAdapter


class MemoryColdBackend:
    kind = "memory"
    def __init__(self):
        self.objects = {}
    def put_chunk(self, key, data):
        self.objects[key] = bytes(data)
        return True
    def get_chunk(self, key):
        return self.objects.get(key)
    def delete_chunk(self, key):
        return self.objects.pop(key, None) is not None
    def exists(self, key):
        return key in self.objects


class FakeAria2:
    def __init__(self, root):
        self.download_root = str(Path(root) / "downloads")
        self.meta_dir = str(Path(root) / "torrent-meta")


@unittest.skipUnless(is_postgres_enabled(), "DATABASE_URL is not set to PostgreSQL")
class PostgreSQLSmokeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        os.environ.setdefault("MYDRIVE_ENV", "development")
        os.environ.setdefault("MYDRIVE_SECRET", "postgres-smoke-test-secret-which-is-long-enough")
        cls.tmp = tempfile.TemporaryDirectory(prefix="mydrive-postgres-smoke-")
        cls.root = Path(cls.tmp.name)
        cls.storage = StorageEngine(str(cls.root / "storage"))
        cls.auth = AuthManager(str(cls.root / "auth"))
        cls.commercial = CommercialManager(str(cls.root / "commerce"))
        cls.media = MediaService(cls.storage, str(cls.root / "media"))
        cls.offline = OfflineEngine(cls.storage, str(cls.root / "offline"), FakeAria2(cls.root))
        cls.share = ShareManager(cls.storage)
        cls.sync = SyncManager(cls.storage)
        cls.telegram = TelegramBotAdapter(cls.storage, cls.auth, cls.offline, str(cls.root / "telegram"))

    @classmethod
    def tearDownClass(cls):
        cls.tmp.cleanup()

    def test_auth_and_commercial_transactions(self):
        user = self.auth.register("pg_user_" + os.urandom(4).hex(), "InitialPass_123")
        self.assertTrue(user["success"], user)
        self.assertIsNotNone(self.auth.verify_token(user["access_token"]))
        self.assertTrue(self.auth.change_password(user["user_id"], "InitialPass_123", "ChangedPass_123")["success"])
        self.assertIsNone(self.auth.verify_token(user["access_token"]))

        code = self.commercial.generate_codes(plan="vip", duration_days=30, count=1)[0]["code"]
        self.assertEqual(self.commercial.redeem_code(user["user_id"], code)["plan"], "vip")
        order = self.commercial.create_order(user["user_id"], plan="vip", duration_days=30)
        paid = self.commercial.mark_order_paid(
            order["order_no"], amount_cents=order["amount_cents"], currency=order["currency"])
        self.assertEqual(paid["status"], "paid")

    def test_storage_chunk_version_and_cold_tier_round_trip(self):
        source = self.root / "source.bin"
        payload = b"postgres-storage-smoke-test" * 1000
        source.write_bytes(payload)
        meta = self.storage.chunk_file(str(source), parent="root", name="source.bin", user_id="pg-storage-user")
        self.assertTrue(meta.identity)
        loaded = self.storage.get_file_meta(meta.identity, user_id="pg-storage-user")
        self.assertIsNotNone(loaded)
        snapshot = self.storage.save_version_snapshot(meta.identity, note="postgres smoke", user_id="pg-storage-user")
        self.assertIsNotNone(snapshot)
        self.assertIsNotNone(snapshot.get("id"))

        manager = self.storage.tiered
        backend = MemoryColdBackend()
        manager.backend = backend
        cid = "pgsmoke" + os.urandom(8).hex()
        local = manager._local_path(cid)
        local.parent.mkdir(parents=True, exist_ok=True)
        chunk = b"cold-tier-postgres-checksum" * 50
        local.write_bytes(chunk)
        manager.record_chunk_stored(cid, len(chunk), len(chunk), False, hashlib.sha256(chunk).hexdigest())
        self.assertTrue(manager.offload_chunk(cid, evict_local=True))
        self.assertFalse(local.exists())
        self.assertEqual(manager.hydrate_from_cold(cid), chunk)

    def test_active_auxiliary_managers_initialize_on_postgres(self):
        # Constructors create their schemas using connect_db and PRAGMA table_info emulation.
        self.assertTrue(self.media.db_path)
        self.assertTrue(self.offline.db)
        self.assertTrue(self.share.db_path)
        self.assertTrue(self.sync.db_path)
        self.assertTrue(self.telegram.db_path)
        conn = connect_db(self.storage.db_path)
        tables = {row[0] for row in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = current_schema()")}
        conn.close()
        for table in ("files", "chunks", "file_chunks", "file_versions", "chunk_tiers",
                      "shares", "sync_pairs", "sync_records", "tg_bindings",
                      "playback_progress", "offline_tasks", "offline_meta"):
            self.assertIn(table, tables, table)


if __name__ == "__main__":
    unittest.main(verbosity=2)
