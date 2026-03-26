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
PACKAGE_NAME = "astrbot_rss_testpkg_dispatcher"
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


config_module = _load_module("config")
dispatcher_module = _load_module("dispatcher")
RSSConfig = config_module.RSSConfig
FeedDispatcher = dispatcher_module.FeedDispatcher


class _FakeContext:
    def __init__(self, fail_first_send: bool = False):
        self.fail_first_send = fail_first_send
        self.send_calls = 0
        self.sent: list[tuple[str, object]] = []

    async def send_message(self, unified_msg_origin, payload):
        self.send_calls += 1
        if self.fail_first_send and self.send_calls == 1:
            raise RuntimeError("temporary send failure")
        self.sent.append((unified_msg_origin, payload))


class _FakeStorage:
    def __init__(self):
        self.pending: set[str] = set()
        self.sent: set[str] = set()
        self.claims: list[str] = []
        self.confirms: list[str] = []
        self.releases: list[str] = []

    async def claim_dispatch(self, fingerprint: str, ttl_seconds: int = 0) -> bool:
        self.claims.append(fingerprint)
        if fingerprint in self.pending or fingerprint in self.sent:
            return False
        self.pending.add(fingerprint)
        return True

    async def confirm_dispatch(self, fingerprint: str, ttl_seconds: int = 0) -> None:
        self.confirms.append(fingerprint)
        self.pending.discard(fingerprint)
        self.sent.add(fingerprint)

    async def release_dispatch(self, fingerprint: str) -> None:
        self.releases.append(fingerprint)
        self.pending.discard(fingerprint)


class DispatcherTests(unittest.IsolatedAsyncioTestCase):
    def _build_config(self):
        return RSSConfig.from_context(
            {
                "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
                "targets": [
                    {
                        "id": "target-1",
                        "platform": "qq",
                        "unified_msg_origin": "default:GroupMessage:1",
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
                "render_mode": "text",
                "dedup_ttl_seconds": 3600,
            }
        )

    async def test_duplicate_dispatch_is_blocked_before_send(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_text_message_chain = lambda item: "payload"

        async def fake_hash(_image_url: str) -> str:
            return "image-sha256"

        dispatcher._hash_image_bytes = fake_hash

        item = {
            "job_id": "job-1",
            "guid": "guid-1",
            "title": "Title",
            "summary": "Summary",
            "link": "https://example.com/post/1",
            "published_at": "2026-03-27T00:00:00+00:00",
            "image_url": "https://example.com/a.jpg",
        }

        first = await dispatcher.dispatch(item)
        second = await dispatcher.dispatch(item)

        self.assertEqual(first.success_count, 1)
        self.assertEqual(second.skipped_duplicate_count, 1)
        self.assertEqual(len(context.sent), 1)
        self.assertEqual(len(storage.confirms), 1)

    async def test_duplicate_dispatch_uses_source_fields_when_translation_differs(self):
        context = _FakeContext()
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_text_message_chain = lambda item: "payload"

        async def fake_hash(_image_url: str) -> str:
            return "image-sha256"

        dispatcher._hash_image_bytes = fake_hash

        first_item = {
            "job_id": "job-1",
            "guid": "guid-translation",
            "title": "第一版中文标题",
            "summary": "第一版中文摘要",
            "_source_title": "English title",
            "_source_summary": "English summary",
            "link": "https://example.com/post/source",
            "published_at": "2026-03-27T00:00:00+00:00",
            "image_url": "https://example.com/a.jpg",
        }
        second_item = {
            "job_id": "job-1",
            "guid": "guid-translation",
            "title": "第二版中文标题",
            "summary": "第二版中文摘要",
            "_source_title": "English title",
            "_source_summary": "English summary",
            "link": "https://example.com/post/source",
            "published_at": "2026-03-27T00:00:00+00:00",
            "image_url": "https://example.com/a.jpg",
        }

        first = await dispatcher.dispatch(first_item)
        second = await dispatcher.dispatch(second_item)

        self.assertEqual(first.success_count, 1)
        self.assertEqual(second.skipped_duplicate_count, 1)
        self.assertEqual(len(context.sent), 1)

    async def test_failed_send_releases_dispatch_claim_for_retry(self):
        context = _FakeContext(fail_first_send=True)
        storage = _FakeStorage()
        dispatcher = FeedDispatcher(context=context, config=self._build_config(), storage=storage)
        dispatcher._build_text_message_chain = lambda item: "payload"

        item = {
            "job_id": "job-1",
            "guid": "guid-2",
            "title": "Retry",
            "summary": "Retry summary",
            "link": "https://example.com/post/2",
            "published_at": "2026-03-27T00:01:00+00:00",
        }

        first = await dispatcher.dispatch(item)
        context.fail_first_send = False
        second = await dispatcher.dispatch(item)

        self.assertEqual(first.transient_failure_count, 1)
        self.assertEqual(second.success_count, 1)
        self.assertEqual(len(storage.releases), 1)
        self.assertEqual(len(context.sent), 1)


if __name__ == "__main__":
    unittest.main()
