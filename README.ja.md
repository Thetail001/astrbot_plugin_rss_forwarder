# astrbot_plugin_rss_forwarder（日本語）

[中文](./README.md) | [English](./README.en.md)

`astrbot_plugin_rss_forwarder` は、AstrBot 向けの RSS / RSSHub 配信オーケストレーション用プラグインです。複数フィードを取得し、管理画面で構成したルールに従って指定チャットへ配信します。

## 位置付け

このプロジェクトは [`Soulter/astrbot_plugin_rss`](https://github.com/Soulter/astrbot_plugin_rss) の単純な代替ではありません。現在の主な方向性は次の通りです。

- 一度配信した内容は再配信しない永続的な重複排除
- feed / target / job / 配信方式を管理画面から視覚的に設定
- 起動直後の不安定な環境に配慮した初回遅延と無効 target 抑制
- 将来的な翻訳、要約、Agent によるページ・画像取得の拡張

## 主な機能

- 複数フィード対応（フィード単位で有効/無効）。
- 認証モード：`none` / `query` / `header`。
- ジョブ単位の配信ルーティング（複数 feed + 複数 target）。
- `daily_digests[]` による独立した日報タスク。
- 定期実行：`interval_seconds` 実装済み、`cron` は将来拡張用（現状は interval フォールバック）。
- 起動直後の初回遅延：プラグイン起動後、既定で `45` 秒待ってから最初のポーリングを行います。
- 永続的な重複排除（配信済みの内容は再配信しない）・ETag/Last-Modified 永続化。
- 管理コマンド：`/rss list` / `/rss status` / `/rss run` / `/rss pause` / `/rss resume` / `/rss digest run [digest_id]`。
- 3 段階の翻訳チェーン：`LLM -> Google Translate -> GitHub Models`。
- 日報は `text` と `image` の両方に対応し、GUI からプロンプトを調整できます。

## `astrbot_plugin_rss` との主な違い

- 単なる購読取得ではなく、配信オーケストレーションを重視
- 再起動に強い重複排除の永続化
- 管理画面からの feed / target / job 設定
- プラットフォーム未準備時の誤送信や誤再試行を抑える保護
- 将来の LLM / Agent 拡張に向けた構造

## 設定

`_conf_schema.json` により、AstrBot のプラグイン管理画面から主要項目を編集できます。

- `feeds[]`
- `targets[]`
- `jobs[]`
- `daily_digests[]`
- `translation.llm_*`
- `translation.google_translate_*`
- `translation.github_models_*`
- `startup_delay_seconds`
- `render_mode` / `summary_max_chars` / `render_card_template`

翻訳の順序は管理画面と実行時で共通です：`LLM -> Google -> GitHub Models`

主な翻訳項目:

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

例:

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

補足:
- 重複排除状態は AstrBot KV と `data/plugin_data/astrbot_rss/state.json` の両方に保存されます
- `last_success_time` より古い記事は再送せず、既読扱いのみ行います
- `startup_delay_seconds` の既定値は `45` 秒です
- `translation.*` 配下の項目はすべて AstrBot のプラグイン管理画面から設定できます
- `daily_digests[]` は即時配信ジョブと独立して動作し、即時配信を有効にしていない feed でも日報対象にできます

主な日報項目:

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

## 翻訳サービスの取得方法

### 1. AstrBot LLM Provider

先に AstrBot 本体で利用可能なモデル提供元を設定し、その後プラグイン管理画面の `translation.llm_provider_id` で対象 Provider を選択します。空欄の場合は、現在のセッションまたは既定 Provider を優先して利用します。

### 2. Google Cloud Translation API Key

Google Cloud Console でプロジェクトを作成し、`Cloud Translation API` Basic v2 を有効化してから、`APIs & Services -> Credentials` で API Key を発行します。発行した Key を `translation.google_translate_api_key` に設定します。

### 3. GitHub Models Token

`models` 権限を持つ GitHub token を作成します。既定では `data/github.token` から読み取り、必要に応じて `ASTRBOT_GITHUB_TOKEN`、`GITHUB_TOKEN`、`GH_TOKEN` でも指定できます。別のファイルを使う場合は `translation.github_models_token_file` を変更します。

## ロードマップ

[ROADMAP.md](./ROADMAP.md) を参照してください。

## 日報機能メモ

`daily_digests[]` は、毎日の業界要約やテーマ別サマリーに向いた機能です。既定の `prompt_template` をそのまま使えますが、管理画面から要約スタイルを調整することもできます。
