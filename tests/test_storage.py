import json
import tempfile
import time
import unittest
from pathlib import Path

from storage import FeedStorage

from storage import FeedStorage


class FeedStorageTests(unittest.IsolatedAsyncioTestCase):
    async def test_persists_seen_records_to_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            await storage.mark_seen("item-1")  # ttl_seconds 参数已废弃，忽略

            restored = FeedStorage(storage_dir=tmpdir)
            self.assertTrue(await restored.has_seen("item-1"))

            state_path = Path(tmpdir) / "state.json"
            self.assertTrue(state_path.exists())
            payload = json.loads(state_path.read_text(encoding="utf-8"))
            # 新格式：不再使用版本号前缀
            self.assertIn("content_seen:item-1", payload["kv"])
            # 验证记录格式：使用 pushed_at 而不是 expire_at
            record = payload["kv"]["content_seen:item-1"]
            self.assertIn("pushed_at", record)
            self.assertNotIn("expire_at", record)

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
        """Test that legacy records with v0: prefix are correctly detected and migrated."""
        stored: dict[str, str] = {
            # Legacy v0 format with version prefix
            "content_seen:v0:legacy-item": json.dumps(
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

            # Legacy record should be detected and migrated
            self.assertTrue(await storage.has_seen("legacy-item"))
            # New format uses content_seen:item-id without version prefix
            self.assertIn("content_seen:legacy-item", stored)
            # Verify migrated record uses new format: pushed_at instead of expire_at
            record = json.loads(stored["content_seen:legacy-item"])
            self.assertIn("pushed_at", record)
            self.assertNotIn("expire_at", record)

    async def test_dispatch_guard_claim_confirm_and_release(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)

            self.assertTrue(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))
            self.assertFalse(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))

            await storage.release_dispatch("fingerprint-1")
            self.assertTrue(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))

            await storage.confirm_dispatch("fingerprint-1", ttl_seconds=3600)
            self.assertFalse(await storage.claim_dispatch("fingerprint-1", ttl_seconds=30))

    async def test_archive_digest_items_and_query_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            now_ts = 1774699200
            item = {
                "feed_id": "feed-1",
                "feed_title": "Feed",
                "guid": "guid-1",
                "title": "Title",
                "summary": "Summary",
                "link": "https://example.com/post/1",
                "published_at": "2026-03-28T06:00:00+00:00",
                "image_url": "https://example.com/a.jpg",
            }

            original_time = time.time
            try:
                time.time = lambda: now_ts
                await storage.archive_digest_items([item])
                await storage.archive_digest_items([dict(item, title="Updated Title")])
            finally:
                time.time = original_time

            items = await storage.list_digest_items(
                ["feed-1"],
                window_start_ts=1774656000,
                window_end_ts=1774742400,
                limit=10,
            )

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Updated Title")
            self.assertEqual(items[0]["item_key"], "guid-1")

    async def test_list_digest_items_falls_back_to_collected_at_when_published_at_missing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)
            now_ts = 1774699200
            item = {
                "feed_id": "feed-1",
                "feed_title": "Feed",
                "guid": "guid-2",
                "title": "Collected Title",
                "summary": "Summary",
                "link": "https://example.com/post/2",
                "published_at": "",
            }

            original_time = time.time
            try:
                time.time = lambda: now_ts
                await storage.archive_digest_items([item])
            finally:
                time.time = original_time

            items = await storage.list_digest_items(
                ["feed-1"],
                window_start_ts=1774695600,
                window_end_ts=1774702800,
                limit=10,
            )

            self.assertEqual(len(items), 1)
            self.assertEqual(items[0]["title"], "Collected Title")

    async def test_cleanup_old_records_removes_oldest_first(self):
        """Test that cleanup_old_records removes oldest records first."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)

            # Mark 5 items as seen with different timestamps
            for i in range(5):
                await storage.mark_seen(f"item-{i}")
                # Small delay to ensure different timestamps
                time.sleep(0.01)

            # Cleanup with max 3 records
            deleted = await storage.cleanup_old_records(max_records=3)
            self.assertEqual(deleted, 2)

            # Verify oldest 2 are removed, newest 3 remain
            self.assertFalse(await storage.has_seen("item-0"))
            self.assertFalse(await storage.has_seen("item-1"))
            self.assertTrue(await storage.has_seen("item-2"))
            self.assertTrue(await storage.has_seen("item-3"))
            self.assertTrue(await storage.has_seen("item-4"))

    async def test_cleanup_old_records_noop_when_under_limit(self):
        """Test that cleanup does nothing when under limit."""
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)

            await storage.mark_seen("item-1")
            await storage.mark_seen("item-2")

            deleted = await storage.cleanup_old_records(max_records=10)
            self.assertEqual(deleted, 0)

            # Both should still exist
            self.assertTrue(await storage.has_seen("item-1"))
            self.assertTrue(await storage.has_seen("item-2"))

    async def test_daily_digest_status_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            storage = FeedStorage(storage_dir=tmpdir)

            await storage.update_daily_digest_status(
                "digest-1",
                last_schedule_date="2026-03-27",
                last_item_count=5,
                last_error="",
            )
            status = await storage.get_daily_digest_status("digest-1")

            self.assertEqual(status["last_schedule_date"], "2026-03-27")
            self.assertEqual(status["last_item_count"], 5)


if __name__ == "__main__":
    unittest.main()
