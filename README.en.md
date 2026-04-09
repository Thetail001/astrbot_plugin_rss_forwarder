# astrbot_plugin_rss_forwarder (English)

[中文](./README.md) | [日本語](./README.ja.md)

`astrbot_plugin_rss_forwarder` is an AstrBot plugin focused on RSS / RSSHub delivery orchestration. It fetches updates from multiple feeds and proactively pushes them to configured chat targets using panel-driven routing rules.

## Positioning

This project is not meant to be a drop-in clone of [`Soulter/astrbot_plugin_rss`](https://github.com/Soulter/astrbot_plugin_rss). Its current focus is broader:

- persistent deduplication that survives restarts
- panel-friendly visual configuration for feeds, targets, jobs, and delivery modes
- startup-safe scheduling and invalid-target suppression for real deployments
- future enrichment with translation, summarization, and agent-assisted page/image extraction

## Highlights

- Multiple feed sources with per-feed enable/disable.
- Auth modes: `none`, `query` (`?key=...`), `header` (`Authorization: Bearer ...`).
- Job-based routing (`feeds[] -> targets[]`).
- Independent daily digest jobs via `daily_digests[]`.
- Scheduled polling via `interval_seconds` (implemented) and `cron` field (reserved, currently fallback).
- Startup-safe first poll delay: waits `45` seconds by default before the first poll after plugin startup.
- Permanent deduplication (once pushed, never pushed again) + feed cursor persistence (ETag / Last-Modified / last_success_time).
- Admin commands: `/rss list`, `/rss status`, `/rss run [job_id]`, `/rss pause [job_id]`, `/rss resume [job_id]`, `/rss digest run [digest_id]`.
- Three-stage translation chain: `LLM -> Google Translate -> GitHub Models`.
- `text` / `image` rendering mode.
- Daily digests support both `text` and `image` rendering, and can use a GUI-editable prompt template.

## Key Differences From `astrbot_plugin_rss`

- delivery orchestration instead of basic subscription polling only
- restart-safe dedup persistence
- panel-driven feed/target/job configuration
- startup-delay and retry-guard logic for unstable platform readiness
- a clearer path for future LLM and agent enrichment features

## Panel Configuration

The plugin ships `_conf_schema.json`, so all major settings can be edited from AstrBot plugin UI:

- `feeds[]`
- `targets[]`
- `jobs[]`
- `daily_digests[]`
- `translation.llm_*`
- `translation.google_translate_*`
- `translation.github_models_*`
- `startup_delay_seconds`, `render_mode`, `summary_max_chars`, `render_card_template`

Translation order in the UI matches runtime order: `LLM -> Google -> GitHub Models`.

Key translation fields:

- `translation.llm_enabled`
- `translation.llm_provider_id`
- `translation.llm_timeout_seconds`
- `translation.llm_profile`
- `translation.max_input_chars`
- `translation.google_translate_enabled`
- `translation.google_translate_api_key`
- `translation.google_translate_target_lang`
- `translation.google_translate_timeout_seconds`
- `translation.google_translate_proxy_mode`
- `translation.google_translate_proxy_url`
- `translation.github_models_enabled`
- `translation.github_models_model`
- `translation.github_models_timeout_seconds`
- `translation.github_models_token_file`
- `translation.github_models_proxy_mode`
- `translation.github_models_proxy_url`

Example:

```json
{
  "translation": {
    "llm_enabled": true,
    "llm_provider_id": "",
    "llm_timeout_seconds": 15,
    "llm_profile": "rss_enrich",
    "max_input_chars": 2000,
    "google_translate_enabled": true,
    "google_translate_api_key": "YOUR_GOOGLE_TRANSLATE_API_KEY",
    "google_translate_target_lang": "zh-CN",
    "google_translate_timeout_seconds": 15,
    "google_translate_proxy_mode": "system",
    "google_translate_proxy_url": "",
    "github_models_enabled": true,
    "github_models_model": "openai/gpt-4o-mini",
    "github_models_timeout_seconds": 15,
    "github_models_token_file": "github.token"
  }
}
```

Notes:
- Dedup state is persisted to both AstrBot KV and `data/plugin_data/astrbot_rss/state.json`
- Items older than a feed's `last_success_time` are marked seen without being pushed again
- `startup_delay_seconds` defaults to `45` so platform adapters have time to become ready
- All `translation.*` fields are available in AstrBot plugin UI
- `daily_digests[]` works independently from realtime jobs, so a feed can be archived and summarized even if it is never pushed as an immediate RSS message

Daily digest fields:

- `id`
- `title`
- `feed_ids`
- `target_ids`
- `send_time`
- `window_hours`
- `max_items`
- `render_mode`
- `prompt_template`
- `enabled`

## Translation Credential Setup

### 1. AstrBot LLM provider

Configure an available model provider in AstrBot first, then select it in `translation.llm_provider_id` from the plugin UI. If left empty, the plugin tries the current-session provider or AstrBot default provider.

### 2. Google Cloud Translation API key

Create a Google Cloud project, enable `Cloud Translation API` Basic v2, then create an API key under `APIs & Services -> Credentials`. Paste that key into `translation.google_translate_api_key`.

### 3. GitHub Models token

Create a GitHub token with `models` access. The plugin reads it from `data/github.token` by default, or from `ASTRBOT_GITHUB_TOKEN`, `GITHUB_TOKEN`, or `GH_TOKEN`. `translation.github_models_token_file` can be changed from the plugin UI if another file path is preferred.

## Roadmap

See [ROADMAP.md](./ROADMAP.md).

## Daily Digest Notes

`daily_digests[]` is meant for scheduled rollups such as a daily industry brief. The default prompt works out of the box, while `prompt_template` in the UI can be adjusted when a different summary style is needed.
