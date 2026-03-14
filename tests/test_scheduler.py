import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(info=lambda *a, **k: None)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "astrbot_rss_testpkg"
package_module = types.ModuleType(PACKAGE_NAME)
package_module.__path__ = [str(ROOT)]
sys.modules[PACKAGE_NAME] = package_module


def _load_module(module_name: str):
    full_name = f"{PACKAGE_NAME}.{module_name}"
    spec = spec_from_file_location(full_name, ROOT / f"{module_name}.py")
    module = module_from_spec(spec)
    sys.modules[full_name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


_load_module("config")
dispatcher_module = _load_module("dispatcher")
_load_module("fetcher")
_load_module("parser")
_load_module("pipeline")
_load_module("storage")
RSSScheduler = _load_module("scheduler").RSSScheduler
DispatchResult = dispatcher_module.DispatchResult


class RSSSchedulerTests(unittest.TestCase):
    def test_history_items_are_suppressed_when_older_than_last_success(self):
        item = {
            "feed_id": "feed-1",
            "published_at": "2026-03-15T00:00:00+00:00",
        }
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertTrue(RSSScheduler._should_mark_history_only(item, feed_state_map))

    def test_newer_items_are_not_suppressed(self):
        item = {
            "feed_id": "feed-1",
            "published_at": "2026-03-15T03:00:01+00:00",
        }
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertFalse(RSSScheduler._should_mark_history_only(item, feed_state_map))

    def test_items_without_timestamp_are_not_suppressed(self):
        item = {"feed_id": "feed-1", "published_at": ""}
        feed_state_map = {"feed-1": {"last_success_time": 1773536400}}

        self.assertFalse(RSSScheduler._should_mark_history_only(item, feed_state_map))


class SchedulerPermanentFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_permanent_target_failures_are_treated_as_seen(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

            async def has_seen(self, item_id):
                return False

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def parse(self, raw_items, job):
                return [{"feed_id": "feed-1", "guid": "item-1", "published_at": ""}]

        class FakeDispatcher:
            async def dispatch(self, item):
                return DispatchResult(permanent_failure_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(id="job-1", feed_ids=["feed-1"], enabled=True, interval_seconds=300)
        storage = FakeStorage()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=FakeDispatcher(),
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(storage.marked, [("item-1", 123)])


if __name__ == "__main__":
    unittest.main()
