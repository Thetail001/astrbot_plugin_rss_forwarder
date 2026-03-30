import sys
import types
import unittest

astrbot_module = types.ModuleType("astrbot")
astrbot_api_module = types.ModuleType("astrbot.api")
astrbot_api_module.logger = types.SimpleNamespace(
    info=lambda *a, **k: None,
    warning=lambda *a, **k: None,
    error=lambda *a, **k: None,
)
sys.modules.setdefault("astrbot", astrbot_module)
sys.modules["astrbot.api"] = astrbot_api_module

from config import ConfigValidationError, RSSConfig


def _minimal_runtime_conf():
    return {
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


class ConfigTranslationTests(unittest.TestCase):
    def test_legacy_timeout_maps_to_llm_timeout(self):
        conf = _minimal_runtime_conf()
        conf.update(
            {
                "llm_enabled": True,
                "timeout": 21,
            }
        )

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(cfg.llm_timeout_seconds, 21)
        self.assertEqual(cfg.timeout, 21)

    def test_translation_section_overrides_legacy_keys(self):
        conf = _minimal_runtime_conf()
        conf.update(
            {
                "timeout": 21,
                "translation": {
                    "llm_enabled": True,
                    "llm_timeout_seconds": 9,
                    "llm_provider_id": "provider-A",
                    "llm_proxy_mode": "custom",
                    "llm_proxy_url": "http://127.0.0.1:7891",
                    "github_models_enabled": True,
                    "github_models_model": "openai/gpt-4o-mini",
                    "github_models_timeout_seconds": 13,
                    "github_models_token_file": "tokens/github.token",
                    "github_models_proxy_mode": "custom",
                    "github_models_proxy_url": "http://127.0.0.1:7892",
                    "google_translate_enabled": True,
                    "google_translate_api_key": "k",
                    "google_translate_target_lang": "ja",
                    "google_translate_timeout_seconds": 11,
                    "google_translate_proxy_mode": "custom",
                    "google_translate_proxy_url": "http://127.0.0.1:7890",
                },
            }
        )

        cfg = RSSConfig.from_context(conf)

        self.assertTrue(cfg.llm_enabled)
        self.assertEqual(cfg.llm_timeout_seconds, 9)
        self.assertEqual(cfg.llm_provider_id, "provider-A")
        self.assertEqual(cfg.llm_proxy_mode, "custom")
        self.assertEqual(cfg.llm_proxy_url, "http://127.0.0.1:7891")
        self.assertTrue(cfg.github_models_enabled)
        self.assertEqual(cfg.github_models_model, "openai/gpt-4o-mini")
        self.assertEqual(cfg.github_models_timeout_seconds, 13)
        self.assertEqual(cfg.github_models_token_file, "tokens/github.token")
        self.assertEqual(cfg.github_models_proxy_mode, "custom")
        self.assertEqual(cfg.github_models_proxy_url, "http://127.0.0.1:7892")
        self.assertTrue(cfg.google_translate_enabled)
        self.assertEqual(cfg.google_translate_api_key, "k")
        self.assertEqual(cfg.google_translate_target_lang, "ja")
        self.assertEqual(cfg.google_translate_timeout_seconds, 11)
        self.assertEqual(cfg.google_translate_proxy_mode, "custom")
        self.assertEqual(cfg.google_translate_proxy_url, "http://127.0.0.1:7890")

    def test_daily_digest_parses_and_preserves_no_implicit_job(self):
        conf = {
            "feeds": [{"id": "feed-1", "url": "https://example.com/rss", "enabled": True}],
            "targets": [
                {
                    "id": "target-1",
                    "platform": "qq",
                    "unified_msg_origin": "qq:group:1",
                    "enabled": True,
                }
            ],
            "jobs": [],
            "daily_digests": [
                {
                    "id": "digest-1",
                    "feed_ids": ["feed-1"],
                    "target_ids": ["target-1"],
                    "send_time": "09:00",
                    "window_hours": 24,
                    "max_items": 20,
                    "render_mode": "image",
                    "enabled": True,
                }
            ],
        }

        cfg = RSSConfig.from_context(conf)

        self.assertEqual(len(cfg.jobs), 0)
        self.assertEqual(len(cfg.daily_digests), 1)
        digest = cfg.daily_digests[0]
        self.assertEqual(digest.id, "digest-1")
        self.assertEqual(digest.title, "digest-1")
        self.assertEqual(digest.render_mode, "image")
        self.assertEqual(digest.send_time, "09:00")
        self.assertTrue(digest.enabled)

    def test_daily_digest_invalid_send_time_raises(self):
        conf = _minimal_runtime_conf()
        conf["daily_digests"] = [
            {
                "id": "digest-1",
                "feed_ids": ["feed-1"],
                "target_ids": ["target-1"],
                "send_time": "25:61",
                "enabled": True,
            }
        ]

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)

    def test_disabled_draft_entries_can_be_saved(self):
        conf = {
            "feeds": [
                {
                    "id": "",
                    "url": "",
                    "enabled": False,
                }
            ],
            "targets": [
                {
                    "id": "target-draft",
                    "platform": "qq",
                    "unified_msg_origin": "",
                    "enabled": False,
                }
            ],
            "jobs": [
                {
                    "id": "job-draft",
                    "feed_ids": [],
                    "target_ids": [],
                    "interval_seconds": 0,
                    "enabled": False,
                }
            ],
            "daily_digests": [
                {
                    "id": "digest-draft",
                    "feed_ids": [],
                    "target_ids": [],
                    "send_time": "09:00",
                    "enabled": False,
                }
            ],
        }

        cfg = RSSConfig.from_context(conf)

        self.assertFalse(cfg.feeds[0].enabled)
        self.assertFalse(cfg.targets[0].enabled)
        self.assertFalse(cfg.jobs[0].enabled)
        self.assertFalse(cfg.daily_digests[0].enabled)

    def test_enabled_target_requires_unified_msg_origin(self):
        conf = _minimal_runtime_conf()
        conf["targets"] = [
            {
                "id": "target-1",
                "platform": "qq",
                "unified_msg_origin": "",
                "enabled": True,
            }
        ]

        with self.assertRaises(ConfigValidationError):
            RSSConfig.from_context(conf)


if __name__ == "__main__":
    unittest.main()
