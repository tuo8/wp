"""Focused regression tests for account security, quotas, and download SSRF guards."""
import os
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from auth_manager import AuthManager
from offline_engine import _validate_download_url
from share_manager import ShareManager


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

    def test_password_change_requires_current_password(self):
        uid = self.create_user()
        self.assertFalse(self.auth.change_password(uid, "", "NewPassphrase_123")["success"])
        self.assertFalse(self.auth.change_password(uid, "incorrect", "NewPassphrase_123")["success"])
        self.assertTrue(self.auth.change_password(uid, "InitialPass_123", "NewPassphrase_123")["success"])
        self.assertFalse(self.auth.login("security_user", "InitialPass_123")["success"])
        self.assertTrue(self.auth.login("security_user", "NewPassphrase_123")["success"])

    def test_reset_requires_one_time_code_for_recovery_phone(self):
        uid = self.create_user()
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
