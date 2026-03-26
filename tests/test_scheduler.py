import asyncio
import sys
import types
import unittest
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
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
config_module = sys.modules[f"{PACKAGE_NAME}.config"]
RSSScheduler = _load_module("scheduler").RSSScheduler
DispatchResult = dispatcher_module.DispatchResult
RSSConfig = config_module.RSSConfig


class RSSSchedulerTests(unittest.TestCase):
    def test_config_defaults_startup_delay_to_45_seconds(self):
        config = RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "qq:group:1",
                        "enabled": True,
                    }
                ],
                "jobs": [
                    {
                        "id": "job-1",
                        "feed_ids": ["feed-1"],
                        "target_ids": ["target-1"],
                        "interval_seconds": 300,
                        "enabled": True,
                    }
                ],
            }
        )

        self.assertEqual(config.startup_delay_seconds, 45)

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


class SchedulerTaskCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_cancels_stale_job_tasks(self):
        async def stale_loop():
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                raise

        stale_task = asyncio.create_task(stale_loop(), name="rss-job-stale")
        await asyncio.sleep(0)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
            startup_delay_seconds=0,
        )
        scheduler = RSSScheduler(
            config=config,
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=None,
        )

        await scheduler.start()

        self.assertTrue(stale_task.cancelled() or stale_task.done())


class SchedulerBatchDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_duplicate_items_in_same_batch_are_dispatched_once(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"]]

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
                return [
                    {"feed_id": "feed-1", "guid": "dup-1", "title": "Same Item", "published_at": ""},
                    {"feed_id": "feed-1", "guid": "dup-1", "title": "Same Item", "published_at": ""},
                ]

        class FakeDispatcher:
            def __init__(self):
                self.calls = 0

            async def dispatch(self, item):
                self.calls += 1
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(id="job-1", feed_ids=["feed-1"], enabled=True, interval_seconds=300)
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)

        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(storage.marked, [("dup-1", 123)])

    async def test_same_link_with_different_guid_is_dispatched_once(self):
        class FakeStorage:
            def __init__(self):
                self.marked = []
                self.seen = set()

            def build_dedup_key(self, item):
                return item["guid"]

            def build_seen_keys(self, item):
                return [item["guid"], f"link:{item['link']}"]

            async def has_seen(self, item_id):
                return item_id in self.seen

            async def mark_seen(self, item_id, ttl_seconds=0):
                self.seen.add(item_id)
                self.marked.append((item_id, ttl_seconds))

            async def get_feed_state(self, feed_id):
                return {"last_success_time": 0}

            async def update_feed_state(self, *args, **kwargs):
                return {}

        class FakeFetcher:
            async def fetch(self, job):
                return [{"feed_id": "feed-1"}]

        class FakeParser:
            def __init__(self):
                self._calls = 0

            def parse(self, raw_items, job):
                self._calls += 1
                if self._calls == 1:
                    return [
                        {
                            "feed_id": "feed-1",
                            "guid": "guid-1",
                            "link": "https://example.com/news/1",
                            "title": "Same Link",
                            "published_at": "",
                        }
                    ]
                return [
                    {
                        "feed_id": "feed-1",
                        "guid": "guid-2",
                        "link": "https://example.com/news/1",
                        "title": "Same Link",
                        "published_at": "",
                    }
                ]

        class FakeDispatcher:
            def __init__(self):
                self.calls = 0

            async def dispatch(self, item):
                self.calls += 1
                return DispatchResult(success_count=1)

        config = types.SimpleNamespace(
            jobs=[],
            dedup_ttl_seconds=123,
            poll_interval_seconds=300,
        )
        job = types.SimpleNamespace(id="job-1", feed_ids=["feed-1"], enabled=True, interval_seconds=300)
        storage = FakeStorage()
        dispatcher = FakeDispatcher()
        scheduler = RSSScheduler(
            config=config,
            fetcher=FakeFetcher(),
            parser=FakeParser(),
            dispatcher=dispatcher,
            storage=storage,
            pipeline=None,
        )

        await scheduler._run_job_once_guarded(job)
        await scheduler._run_job_once_guarded(job)

        self.assertEqual(dispatcher.calls, 1)
        self.assertEqual(
            storage.marked,
            [("guid-1", 123), ("link:https://example.com/news/1", 123)],
        )


class SchedulerTranslationTest(unittest.IsolatedAsyncioTestCase):
    async def test_test_translation_returns_pipeline_error_when_missing(self):
        config = types.SimpleNamespace(jobs=[])
        scheduler = RSSScheduler(
            config=config,
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=None,
        )

        result = await scheduler.test_translation(sample_text="hello")

        self.assertEqual(result.get("error"), "pipeline_not_configured")

    async def test_test_translation_returns_report_and_config_snapshot(self):
        class FakePipeline:
            async def diagnose_translation(self, entry):
                self.entry = entry
                return {
                    "input_chars": len(entry.get("summary", "")),
                    "llm": {"ok": True, "latency_ms": 10, "provider_id": "p"},
                    "github": {"ok": False, "latency_ms": 12, "error": "token_missing"},
                    "google": {"ok": False, "latency_ms": 20, "error": "no_key"},
                }

        pipeline = FakePipeline()
        config = types.SimpleNamespace(
            jobs=[],
            llm_enabled=True,
            llm_timeout_seconds=15,
            llm_proxy_mode="system",
            github_models_enabled=True,
            github_models_model="openai/gpt-4o-mini",
            github_models_timeout_seconds=18,
            github_models_proxy_mode="custom",
            google_translate_enabled=True,
            google_translate_target_lang="zh-CN",
            google_translate_timeout_seconds=15,
            google_translate_proxy_mode="off",
        )
        scheduler = RSSScheduler(
            config=config,
            fetcher=types.SimpleNamespace(),
            parser=types.SimpleNamespace(),
            dispatcher=types.SimpleNamespace(),
            storage=types.SimpleNamespace(),
            pipeline=pipeline,
        )

        result = await scheduler.test_translation(sample_text="sample")

        self.assertEqual(pipeline.entry.get("summary"), "sample")
        self.assertEqual(result.get("llm", {}).get("ok"), True)
        self.assertEqual(result.get("github", {}).get("error"), "token_missing")
        self.assertEqual(result.get("config", {}).get("github_models_model"), "openai/gpt-4o-mini")
        self.assertEqual(result.get("google", {}).get("error"), "no_key")
        self.assertEqual(result.get("config", {}).get("google_translate_proxy_mode"), "off")


if __name__ == "__main__":
    unittest.main()
