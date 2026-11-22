"""Regression tests for ImgBed adapter and primary storage mode."""
import hashlib, json, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from imgbed_backend import ImgBedBackend
from tiered_storage import TieredStorageManager


class ImgBedBackendTests(unittest.TestCase):
    def setUp(self):
        self.backend=ImgBedBackend("https://imgbed.example","upload-auth-code","admin","admin-password")
        self.key="ba/fk/bafybeigdyrzt5sfp7udm7hu76v3r2m4j5x6k7q8w9e0a1b2c3d4e5f6g7h"
        self.remote_id=self.key.rsplit("/",1)[-1]+".bin"

    def test_upload_uses_telegram_channel_and_cid_filename(self):
        seen={}
        def fake(method,url,body=None,headers=None):
            seen.update(method=method,url=url,body=body,headers=headers)
            return 200,json.dumps([{"src":"/file/"+self.remote_id}]).encode()
        with patch.object(self.backend,"_request",side_effect=fake):
            self.assertTrue(self.backend.put_chunk(self.key,b"payload"))
        self.assertEqual(seen["method"],"POST")
        self.assertIn("uploadChannel=telegram",seen["url"])
        self.assertIn("uploadNameType=origin",seen["url"])
        self.assertEqual(seen["headers"]["authCode"],"upload-auth-code")
        self.assertIn(('filename="'+self.remote_id+'"').encode(),seen["body"])

    def test_upload_rejects_unexpected_filename(self):
        with patch.object(self.backend,"_request",return_value=(200,b'[{"src":"/file/other.bin"}]')):
            self.assertFalse(self.backend.put_chunk(self.key,b"x"))

    def test_download_route(self):
        with patch.object(self.backend,"_request",return_value=(200,b"chunk")) as req:
            self.assertEqual(self.backend.get_chunk(self.key),b"chunk")
        self.assertIn("/file/"+self.remote_id,req.call_args.args[1])

    def test_delete_uses_basic_admin_auth(self):
        with patch.object(self.backend,"_request",return_value=(200,b"")) as req:
            self.assertTrue(self.backend.delete_chunk(self.key))
        self.assertEqual(req.call_args.kwargs["headers"]["Authorization"],"Basic YWRtaW46YWRtaW4tcGFzc3dvcmQ=")

    def test_delete_fails_closed_without_admin_credentials(self):
        self.assertFalse(ImgBedBackend("https://imgbed.example","code").delete_chunk(self.key))

    def test_non_local_http_rejected(self):
        with self.assertRaises(ValueError): ImgBedBackend("http://imgbed.example","code")


class ImgBedPrimaryStorageTests(unittest.TestCase):
    def test_primary_mode_remote_verification_and_local_cache(self):
        class MemoryBackend:
            kind="imgbed"
            def __init__(self): self.objects={}
            def put_chunk(self,k,d): self.objects[k]=bytes(d); return True
            def get_chunk(self,k): return self.objects.get(k)
            def delete_chunk(self,k): self.objects.pop(k,None); return True
            def exists(self,k): return k in self.objects
        with tempfile.TemporaryDirectory() as root:
            p=Path(root); (p/"db").mkdir()
            m=TieredStorageManager(root,p/"db/index.sqlite",p/"chunks")
            m.backend=MemoryBackend(); m.config["primary_storage"]=True
            cid="bafybeigdyrzt5sfp7udm7hu76v3r2m4j5x6k7q8w9e0a1b2c3d4e5f6g7h"; data=b"primary-data"*100
            local=m._local_path(cid); local.parent.mkdir(parents=True,exist_ok=True); local.write_bytes(data)
            m.record_chunk_stored(cid,len(data),len(data),False,hashlib.sha256(data).hexdigest())
            self.assertEqual(m.backend.objects[m._cold_key(cid)],data)
            self.assertTrue(local.exists())
            self.assertEqual(m.get_stats()["cold_chunks"],1)

    def test_failed_primary_upload_retries_from_local(self):
        class Flaky:
            kind="imgbed"
            def __init__(self): self.objects={}; self.fail=True
            def put_chunk(self,k,d):
                if self.fail: self.fail=False; return False
                self.objects[k]=bytes(d); return True
            def get_chunk(self,k): return self.objects.get(k)
            def delete_chunk(self,k): self.objects.pop(k,None); return True
            def exists(self,k): return k in self.objects
        with tempfile.TemporaryDirectory() as root:
            p=Path(root); (p/"db").mkdir()
            m=TieredStorageManager(root,p/"db/index.sqlite",p/"chunks")
            m.backend=Flaky(); m.config["primary_storage"]=True
            cid="bafybeigdyrzt5sfp7udm7hu76v3r2m4j5x6k7q8w9e0a1b2c3d4e5f6g7h"; data=b"retry-data"
            local=m._local_path(cid); local.parent.mkdir(parents=True,exist_ok=True); local.write_bytes(data)
            with self.assertRaises(RuntimeError):
                m.record_chunk_stored(cid,len(data),len(data),False,hashlib.sha256(data).hexdigest())
            self.assertTrue(local.exists()); self.assertTrue(m.ensure_primary_chunk(cid))
            self.assertEqual(m.backend.objects[m._cold_key(cid)],data)

    def test_remote_delete_failure_preserves_local_and_tier_record(self):
        class FailingDelete:
            kind="imgbed"
            def delete_chunk(self,key): return False
        with tempfile.TemporaryDirectory() as root:
            p=Path(root); (p/"db").mkdir()
            m=TieredStorageManager(root,p/"db/index.sqlite",p/"chunks")
            m.backend=FailingDelete()
            cid="bafybeigdyrzt5sfp7udm7hu76v3r2m4j5x6k7q8w9e0a1b2c3d4e5f6g7h"
            local=m._local_path(cid); local.parent.mkdir(parents=True,exist_ok=True); local.write_bytes(b"keep-me")
            db=__import__("sqlite3").connect(m.db_path)
            db.execute("INSERT INTO chunk_tiers (cid,raw_size,stored_size,in_local,in_cold,cold_backend) VALUES (?,7,7,1,1,'imgbed')",(cid,))
            db.commit(); db.close()
            self.assertEqual(m.delete_chunk_everywhere(cid),0)
            self.assertTrue(local.exists())
            db=__import__("sqlite3").connect(m.db_path)
            self.assertIsNotNone(db.execute("SELECT cid FROM chunk_tiers WHERE cid=?",(cid,)).fetchone())
            db.close()

    def test_hydration_rejects_different_backend(self):
        class Other:
            kind="imgbed"
            def get_chunk(self,k): return b"wrong"
        with tempfile.TemporaryDirectory() as root:
            p=Path(root); (p/"db").mkdir()
            m=TieredStorageManager(root,p/"db/index.sqlite",p/"chunks"); m.backend=Other()
            cid="bafybeigdyrzt5sfp7udm7hu76v3r2m4j5x6k7q8w9e0a1b2c3d4e5f6g7h"
            import sqlite3
            db=sqlite3.connect(m.db_path)
            db.execute("INSERT INTO chunk_tiers (cid,raw_size,stored_size,in_local,in_cold,cold_backend) VALUES (?,5,5,0,1,'s3')",(cid,))
            db.commit(); db.close()
            self.assertIsNone(m.hydrate_from_cold(cid))


if __name__=="__main__": unittest.main()
