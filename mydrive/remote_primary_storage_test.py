"""Regression tests for remote-authoritative primary block storage."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path

from storage_engine import StorageEngine, CHUNK_FMT_PLAIN, CHUNK_FMT_XOR75


class MemoryBackend:
    kind = "memory-test"

    def __init__(self):
        self.objects = {}

    def put_chunk(self, key, data):
        self.objects[key] = bytes(data)
        return True

    def get_chunk(self, key):
        return self.objects.get(key)

    def delete_chunk(self, key):
        self.objects.pop(key, None)
        return True

    def exists(self, key):
        return key in self.objects


class RemotePrimaryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = StorageEngine(self.tmp.name)
        self.backend = MemoryBackend()
        self.engine.tiered.backend = self.backend
        self.engine.tiered.config["primary_storage"] = True
        self.engine.tiered.config["local_cache_max_bytes"] = 0

    def tearDown(self):
        self.tmp.cleanup()

    def test_upload_is_remote_authoritative_and_leaves_no_local_chunk(self):
        data = b"remote primary block" * 1000
        from cid_engine import compute_cid
        cid = compute_cid(data)
        self.engine._store_chunk(cid, data, CHUNK_FMT_PLAIN)
        self.assertEqual(self.backend.get_chunk(self.engine.tiered._cold_key(cid)), data)
        self.assertFalse(self.engine._chunk_path(cid).exists())
        self.assertEqual(self.engine.get_chunk(cid), data)

    def test_remote_read_does_not_hydrate_local_disk(self):
        data = b"range friendly content" * 400
        from cid_engine import compute_cid
        cid = compute_cid(data)
        self.engine._store_chunk(cid, data)
        self.assertEqual(self.engine.get_chunk(cid), data)
        self.assertFalse(self.engine._chunk_path(cid).exists())

    def test_existing_local_block_is_migrated_on_read(self):
        data = b"legacy local block" * 200
        from cid_engine import compute_cid
        cid = compute_cid(data)
        self.engine.tiered.config["primary_storage"] = False
        self.engine._store_chunk(cid, data)
        self.assertTrue(self.engine._chunk_path(cid).exists())
        self.engine.tiered.config["primary_storage"] = True
        self.assertEqual(self.engine.get_chunk_raw(cid), data)
        self.assertFalse(self.engine._chunk_path(cid).exists())
        self.assertEqual(self.backend.get_chunk(self.engine.tiered._cold_key(cid)), data)
        self.assertEqual(self.engine.get_chunk(cid), data)

    def test_xor_encoded_block_round_trips(self):
        data = b"offline encoded content" * 300
        from cid_engine import compute_cid
        cid = compute_cid(data)
        self.engine._store_chunk(cid, data, CHUNK_FMT_XOR75)
        self.assertEqual(self.engine.get_chunk(cid), data)
        self.assertEqual(self.engine.get_chunk_raw(cid), data.translate(bytes(b ^ 0x75 for b in range(256))))
        self.assertFalse(self.engine._chunk_path(cid).exists())

    def test_remote_corruption_is_rejected(self):
        data = b"verified bytes"
        from cid_engine import compute_cid
        cid = compute_cid(data)
        self.engine._store_chunk(cid, data)
        key = self.engine.tiered._cold_key(cid)
        self.backend.objects[key] = b"corrupt"
        self.assertIsNone(self.engine.tiered.get_primary_chunk(cid))


if __name__ == "__main__":
    unittest.main()
