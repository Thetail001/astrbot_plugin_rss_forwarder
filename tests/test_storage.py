import json
import tempfile
import unittest
from pathlib import Path

from storage import FeedStorage


class FeedStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_seen_records_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.mark_seen("item-1", ttl_seconds=3600)

            restored = FeedStorage(storage_dir=tmpdir)
            self.assertTrue(await restored.has_seen("item-1"))

            state_path = Path(tmpdir) / "state.json"
            self.assertTrue(state_path.exists())
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertIn("content_seen:v0:item-1", payload["kv"])

    def test_build_seen_keys_include_normalized_link_fingerprint(self):
        storage = FeedStorage(storage_dir=".")

        keys = storage.build_seen_keys(
            {
                "guid": "guid-1",
                "link": "HTTPS://Example.com/path?a=1#fragment",
            }
        )

        self.assertEqual(keys[0], "guid-1")
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[1], storage.build_link_fingerprint({"link": "https://example.com/path?a=1"}))

    def test_build_link_fingerprint_returns_empty_for_missing_link(self):
        storage = FeedStorage(storage_dir=".")

        self.assertEqual(storage.build_link_fingerprint({"link": ""}), "")
        self.assertEqual(storage.build_seen_keys({"guid": "guid-1"}), ["guid-1"])

    async def test_reads_legacy_backend_keys_and_migrates_them(self):
        stored: dict[str, str] = {
            "content_seen:legacy-item": json.dumps(
                {"id": "legacy-item", "expire_at": 9999999999, "updated_at": 1},
                ensure_ascii=False,
            )
        }

        async def get_kv_data(key: str, default=None):
            return stored.get(key, default)

        async def put_kv_data(key: str, value):
            stored[key] = value

        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(
                get_kv_data=get_kv_data,
                put_kv_data=put_kv_data,
                storage_dir=tmpdir,
            )
            storage._dedup_version = 1

            self.assertTrue(await storage.has_seen("legacy-item"))
            self.assertIn("content_seen:v1:legacy-item", stored)


if __name__ == "__main__":
    unittest.main()
