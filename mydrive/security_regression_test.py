"""Focused regression tests for account security, quotas, and download SSRF guards."""
import os
import sqlite3
import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch

from auth_manager import AuthManager
from offline_engine import _validate_download_url
from share_manager import ShareManager
from tiered_storage import TieredStorageManager
from commercial_manager import CommercialManager
from offline_engine import OfflineEngine
from media_service import MediaService
from db_adapter import PgCursorWrapper, translate_sql_to_pg


class AuthSecurityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mydrive-security-test-")
        self.env = patch.dict(os.environ, {"MYDRIVE_ENV": "development", "MYDRIVE_SMS_DEBUG": "0"})
        self.env.start()
        self.auth = AuthManager(self.tmp.name)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def create_user(self):
        result = self.auth.register("security_user", "InitialPass_123")
        self.assertTrue(result["success"], result)
        return result["user_id"]

    def test_password_change_requires_current_password_and_revokes_access(self):
        user = self.auth.register("security_user", "InitialPass_123")
        self.assertTrue(user["success"], user)
        uid = user["user_id"]
        access_token = user["access_token"]
        self.assertIsNotNone(self.auth.verify_token(access_token))
        self.assertFalse(self.auth.change_password(uid, "", "NewPassphrase_123")["success"])
        self.assertFalse(self.auth.change_password(uid, "incorrect", "NewPassphrase_123")["success"])
        self.assertTrue(self.auth.change_password(uid, "InitialPass_123", "NewPassphrase_123")["success"])
        self.assertIsNone(self.auth.verify_token(access_token))
        self.assertFalse(self.auth.login("security_user", "InitialPass_123")["success"])
        self.assertTrue(self.auth.login("security_user", "NewPassphrase_123")["success"])

    def test_reset_requires_one_time_code_for_recovery_phone_and_revokes_access(self):
        user = self.auth.register("security_user", "InitialPass_123")
        uid = user["user_id"]
        old_access = user["access_token"]
        old_refresh = user["refresh_token"]
        self.assertIsNotNone(self.auth.verify_token(old_access))
        self.assertFalse(self.auth.reset_password("security_user", "ResetPassphrase_123", "")["success"])
        self.auth.update_profile(uid, phone="+15551234567")
        conn = sqlite3.connect(self.auth.db_path)
        conn.execute(
            "INSERT INTO sms_codes(phone,code,scene,created_ts,expires_ts,used) VALUES(?,?,?,?,?,0)",
            ("+15551234567", "123456", 0, 1, 4102444800),
        )
        conn.commit()
        conn.close()
        self.assertFalse(self.auth.reset_password("security_user", "ResetPassphrase_123", "000000")["success"])
        self.assertTrue(self.auth.reset_password("security_user", "ResetPassphrase_123", "123456")["success"])
        self.assertIsNone(self.auth.verify_token(old_access))
        self.assertFalse(self.auth.refresh(old_refresh)["success"])
        self.assertFalse(self.auth.reset_password("security_user", "ReplayPassphrase_123", "123456")["success"])

    def test_sms_codes_are_not_exposed_without_debug_flag(self):
        self.assertFalse(self.auth.send_sms_code("+15551234567")["success"])
        with patch.dict(os.environ, {"MYDRIVE_SMS_DEBUG": "1"}):
            result = self.auth.send_sms_code("+15551234567")
        self.assertTrue(result["success"])
        self.assertEqual(len(result["code"]), 6)

    def test_quota_reservation_is_atomic_and_bounded(self):
        uid = self.create_user()
        self.assertTrue(self.auth.reserve_usage(uid, 80, 100))
        self.assertFalse(self.auth.reserve_usage(uid, 21, 100))
        self.assertEqual(self.auth.get_quota(uid)["used"], 80)

    def test_account_wide_logout_revokes_access_token(self):
        user = self.auth.register("logout_user", "InitialPass_123")
        token = user["access_token"]
        self.assertIsNotNone(self.auth.verify_token(token))
        self.auth.logoff(user_id=user["user_id"])
        self.assertIsNone(self.auth.verify_token(token))

    def test_account_removal_requires_password_confirmation(self):
        uid = self.create_user()
        self.assertFalse(self.auth.delete_user(uid, "")["success"])
        self.assertFalse(self.auth.delete_user(uid, "incorrect")["success"])
        self.assertTrue(self.auth.delete_user(uid, "InitialPass_123")["success"])


class SharePasswordTests(unittest.TestCase):
    def test_scrypt_hash_and_legacy_upgrade(self):
        stored = ShareManager._hash_share_password("share-secret-phrase")
        valid, upgrade = ShareManager._verify_share_password("share-secret-phrase", stored)
        self.assertTrue(valid)
        self.assertIsNone(upgrade)
        self.assertFalse(ShareManager._verify_share_password("wrong", stored)[0])
        legacy = __import__("hashlib").sha256(b"legacy-share-pass").hexdigest()
        valid, upgraded = ShareManager._verify_share_password("legacy-share-pass", legacy)
        self.assertTrue(valid)
        self.assertTrue(upgraded.startswith("scrypt$"))


class _MemoryColdBackend:
    kind = "memory"
    def __init__(self):
        self.objects = {}
        self.corrupt_reads = False
    def put_chunk(self, key, data):
        self.objects[key] = bytes(data)
        return True
    def get_chunk(self, key):
        data = self.objects.get(key)
        if data is not None and self.corrupt_reads:
            return data + b"corrupt"
        return data
    def delete_chunk(self, key):
        return self.objects.pop(key, None) is not None
    def exists(self, key):
        return key in self.objects


class TieredStorageIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mydrive-tiered-test-")
        root = Path(self.tmp.name)
        (root / "db").mkdir()
        self.db_path = root / "db" / "index.sqlite"
        self.chunks_dir = root / "chunks"
        self.manager = TieredStorageManager(str(root), self.db_path, self.chunks_dir)
        self.backend = _MemoryColdBackend()
        self.manager.backend = self.backend
        self.cid = "bafybeigdyrzt5sfp7udm7hu76v3r2m4j5x6k7q8w9e0a1b2c3d4e5f6g7h"
        self.data = b"chunk-payload-for-cold-storage-integrity" * 100
        self.local = self.manager._local_path(self.cid)
        self.local.parent.mkdir(parents=True, exist_ok=True)
        self.local.write_bytes(self.data)
        import hashlib
        self.manager.record_chunk_stored(self.cid, len(self.data), len(self.data), False,
                                         hashlib.sha256(self.data).hexdigest())

    def tearDown(self):
        self.tmp.cleanup()

    def test_corrupt_cold_copy_never_evicts_local_chunk(self):
        self.backend.corrupt_reads = True
        self.assertFalse(self.manager.offload_chunk(self.cid, evict_local=True))
        self.assertTrue(self.local.exists())
        import sqlite3
        conn = sqlite3.connect(self.db_path)
        row = conn.execute("SELECT in_local, in_cold FROM chunk_tiers WHERE cid=?", (self.cid,)).fetchone()
        conn.close()
        self.assertEqual(row, (1, 0))

    def test_verified_offload_and_hydration(self):
        self.assertTrue(self.manager.offload_chunk(self.cid, evict_local=True))
        self.assertFalse(self.local.exists())
        self.backend.corrupt_reads = True
        self.assertIsNone(self.manager.hydrate_from_cold(self.cid))
        self.assertFalse(self.local.exists())
        self.backend.corrupt_reads = False
        self.assertEqual(self.manager.hydrate_from_cold(self.cid), self.data)
        self.assertEqual(self.local.read_bytes(), self.data)


class CommercialStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="mydrive-commercial-test-")
        self.env = patch.dict(os.environ, {"MYDRIVE_ENV": "development"})
        self.env.start()
        self.manager = CommercialManager(self.tmp.name)

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def test_redeem_code_is_one_time(self):
        code = self.manager.generate_codes(plan="vip", duration_days=30, count=1)[0]["code"]
        membership = self.manager.redeem_code("buyer-1", code)
        self.assertEqual(membership["plan"], "vip")
        with self.assertRaises(ValueError):
            self.manager.redeem_code("buyer-2", code)

    def test_payment_callback_is_idempotent(self):
        order = self.manager.create_order("buyer-1", plan="vip", duration_days=30)
        first = self.manager.mark_order_paid(order["order_no"])
        expiry = first["membership"]["expire_ts"]
        second = self.manager.mark_order_paid(order["order_no"])
        self.assertEqual(second["status"], "paid")
        self.assertEqual(self.manager.get_membership("buyer-1")["expire_ts"], expiry)


    def test_payment_amount_and_currency_must_match_order(self):
        order = self.manager.create_order("buyer-2", plan="vip", duration_days=30)
        with self.assertRaises(ValueError):
            self.manager.mark_order_paid(order["order_no"], amount_cents=order["amount_cents"] - 1, currency="CNY")
        self.assertEqual(self.manager.list_orders(user_id="buyer-2")[0]["status"], "pending")
        with self.assertRaises(ValueError):
            self.manager.mark_order_paid(order["order_no"], amount_cents=order["amount_cents"], currency="USD")
        paid = self.manager.mark_order_paid(order["order_no"], amount_cents=order["amount_cents"], currency="CNY")
        self.assertEqual(paid["status"], "paid")

    def test_production_payment_callback_requires_amount_and_currency(self):
        order = self.manager.create_order("buyer-3", plan="vip", duration_days=30)
        with patch.dict(os.environ, {"MYDRIVE_ENV": "production"}):
            with self.assertRaises(ValueError):
                self.manager.mark_order_paid(order["order_no"])

    def test_order_amount_uses_exact_day_proration(self):
        order = self.manager.create_order("buyer-4", plan="vip", duration_days=15)
        self.assertEqual(order["amount_cents"], 995)
        with self.assertRaises(ValueError):
            self.manager.create_order("buyer-5", plan="vip", duration_days=0)

    def test_webhook_requires_configured_secret(self):
        payload = b'{"order":"demo"}'
        with patch.dict(os.environ, {"MYDRIVE_ENV": "production"}, clear=False):
            os.environ.pop("PAYMENT_WEBHOOK_SECRET", None)
            self.assertFalse(self.manager.verify_webhook_signature(payload, "anything"))
        with patch.dict(os.environ, {"PAYMENT_WEBHOOK_SECRET": "s" * 40, "MYDRIVE_ENV": "production"}):
            import hashlib, hmac
            signature = hmac.new(("s" * 40).encode(), payload, hashlib.sha256).hexdigest()
            self.assertTrue(self.manager.verify_webhook_signature(payload, signature))

    def test_concurrent_membership_extensions_are_not_lost(self):
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(lambda _: self.manager.set_membership("buyer-1", "vip", 1), range(5)))
        expiry = self.manager.get_membership("buyer-1")["expire_ts"]
        self.assertGreaterEqual(expiry, int(__import__("time").time()) + 4 * 86400)


class _FakePgCursor:
    def __init__(self):
        self.description = [("cid",), ("name",), ("type",), ("not_null",), ("dflt_value",), ("pk",)]
        self.rowcount = 0
        self.sql = ""
        self.params = None
    def execute(self, sql, params=None):
        self.sql, self.params = sql, params
        return self
    def fetchall(self):
        return [(0, "identity", "text", 1, None, 1)]
    def fetchone(self):
        rows = self.fetchall()
        return rows[0] if rows else None
    def __iter__(self):
        return iter(self.fetchall())


class DatabaseAdapterTests(unittest.TestCase):
    def test_pragma_table_info_is_emulated_for_postgres(self):
        raw = _FakePgCursor()
        cursor = PgCursorWrapper(raw)
        cursor.execute("PRAGMA table_info(files)")
        row = cursor.fetchone()
        self.assertEqual(row[1], "identity")
        cursor.execute("PRAGMA table_info(files)")
        self.assertEqual(list(cursor)[0][1], "identity")
        self.assertIn("information_schema.columns", raw.sql)
        self.assertEqual(raw.params, ("files",))

    def test_sqlite_integer_columns_map_to_postgres_bigint(self):
        sql = translate_sql_to_pg(
            "CREATE TABLE quota_test (id INTEGER PRIMARY KEY AUTOINCREMENT, bytes_quota INTEGER, updated_ms INTEGER)"
        )
        self.assertIn("id BIGSERIAL PRIMARY KEY", sql)
        self.assertIn("bytes_quota BIGINT", sql)
        self.assertIn("updated_ms BIGINT", sql)

    def test_sqlite_insert_ignore_translation_remains_supported(self):
        sql = translate_sql_to_pg("INSERT OR IGNORE INTO test_table (id) VALUES (?)")
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertIn("%s", sql)


class ActiveDatabaseModuleTests(unittest.TestCase):
    def test_offline_engine_metadata_cache_uses_adapter(self):
        class FakeAria2:
            def __init__(self, root):
                self.download_root = str(Path(root) / "downloads")
                self.meta_dir = str(Path(root) / "meta")
        with tempfile.TemporaryDirectory(prefix="offline-adapter-test-") as root:
            engine = OfflineEngine(None, str(Path(root) / "offline"), FakeAria2(root))
            metadata = {"name": "example", "size": 123, "files": [{"path": "a.bin", "length": 123}]}
            engine._meta_cache_put("test-infohash", metadata)
            self.assertEqual(engine._meta_cache_get("test-infohash")["name"], "example")

    def test_media_service_initializes_database_through_adapter(self):
        with tempfile.TemporaryDirectory(prefix="media-adapter-test-") as root:
            service = MediaService(None, root)
            self.assertTrue(service.db_path.exists())
            conn = sqlite3.connect(service.db_path)
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            conn.close()
            self.assertIn("playback_progress", tables)


class OfflineSSRFTests(unittest.TestCase):
    def test_private_and_reserved_targets_are_rejected(self):
        targets = [
            "http://127.0.0.1/private",
            "http://10.0.0.8/",
            "http://169.254.169.254/latest/meta-data/",
            "http://192.0.2.10/",
            "http://[::1]/",
            "file:///etc/passwd",
            "ftp://example.com/file",
        ]
        for target in targets:
            with self.subTest(target=target):
                with self.assertRaises(ValueError):
                    _validate_download_url(target)

    def test_public_ip_and_tracker_are_accepted(self):
        _validate_download_url("https://8.8.8.8/file.zip")
        _validate_download_url(
            "magnet:?xt=urn:btih:0123456789012345678901234567890123456789"
            "&tr=udp%3A%2F%2F8.8.8.8%3A6969%2Fannounce"
        )

    def test_private_magnet_tracker_is_rejected(self):
        target = (
            "magnet:?xt=urn:btih:0123456789012345678901234567890123456789"
            "&tr=http%3A%2F%2F127.0.0.1%2Fannounce"
        )
        with self.assertRaises(ValueError):
            _validate_download_url(target)

    def test_private_magnet_webseed_is_rejected(self):
        target = (
            "magnet:?xt=urn:btih:0123456789012345678901234567890123456789"
            "&ws=http%3A%2F%2F169.254.169.254%2Fsecret"
        )
        with self.assertRaises(ValueError):
            _validate_download_url(target)


if __name__ == "__main__":
    unittest.main()
