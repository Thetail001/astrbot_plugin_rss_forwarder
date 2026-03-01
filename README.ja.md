# AstrBot RSS Forwarder（日本語）

[中文](./README.md) | [English](./README.en.md)

AstrBot RSS Forwarder は、複数の RSS / RSSHub フィードを定期取得し、指定したチャット（グループ/チャンネル/DM）へ自動配信する AstrBot プラグインです。

## 主な機能

- 複数フィード対応（フィード単位で有効/無効）。
- 認証モード：`none` / `query` / `header`。
- ジョブ単位の配信ルーティング（複数 feed + 複数 target）。
- 定期実行：`interval_seconds` 実装済み、`cron` は将来拡張用（現状は interval フォールバック）。
- 重複排除・ETag/Last-Modified 永続化。
- 管理コマンド：`/rss list` / `/rss status` / `/rss run` / `/rss pause` / `/rss resume`。
- LLM 拡張ポイント（失敗時は自動フォールバック）。

## 設定

`_conf_schema.json` により、AstrBot のプラグイン管理画面から主要項目を編集できます。
