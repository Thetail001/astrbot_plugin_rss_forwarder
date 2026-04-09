import hashlib
import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

try:
    import fcntl
except ImportError:  # pragma: no cover - non-POSIX fallback.
    fcntl = None

try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - unit tests may run without AstrBot runtime.
    StarTools = None


class FeedStorage:
    """存储层：负责去重、游标和持久化。"""

    FEED_STATE_PREFIX = "feed_state:"
    CONTENT_KEY_PREFIX = "content_seen:"
    DEDUP_VERSION_KEY = "content_seen_version"
    DISPATCH_GUARD_PREFIX = "dispatch_guard:"
    DAILY_DIGEST_RETENTION_SECONDS = 30 * 24 * 60 * 60

    def __init__(
        self,
        plugin_name: str = "astrbot_rss",
        get_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        put_kv_data: Callable[[str, Any], Awaitable[Any]] | None = None,
        delete_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        storage_dir: str | Path | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._delete_kv_data = delete_kv_data
        self._fallback_store: dict[str, str] = {}
        self._seen_ids: set[str] = set()
        self._dedup_version: int | None = None
        cache_root = Path(storage_dir) if storage_dir is not None else self.plugin_cache_dir()
        self._state_path = cache_root / "state.json"
        self._state_loaded = False
        self._disk_state: dict[str, Any] = {'kv': {}}

    async def get(self, key: str, default: Any = None) -> Any:
        """封装 KV 读取。"""
        await self._ensure_state_loaded()
        kv_store = self._disk_state.setdefault('kv', {})
        if key in kv_store:
            return kv_store[key]

        raw = await self._read_raw_from_backend(key)
        decoded = self._decode_value(raw)
        if decoded is None:
            return default
        kv_store[key] = decoded
        self._flush_state()
        return decoded

    async def put(self, key: str, value: Any) -> None:
        """封装 KV 写入。"""
        await self._ensure_state_loaded()
        self._disk_state.setdefault('kv', {})[key] = value
        self._flush_state()

        encoded = json.dumps(value, ensure_ascii=False)
        if self._put_kv_data is None:
            self._fallback_store[key] = encoded
            return
        await self._put_kv_data(key, encoded)

    async def delete(self, key: str) -> None:
        """封装 KV 删除。"""
        await self._ensure_state_loaded()
        self._disk_state.setdefault('kv', {}).pop(key, None)
        self._flush_state()

        if self._delete_kv_data is None:
            self._fallback_store.pop(key, None)
            return
        await self._delete_kv_data(key)

    async def has_seen(self, item_id: str) -> bool:
        """检查条目是否已推送。

        去重记录永久有效，不会过期。定期清理通过 cleanup_old_records() 控制存储大小。
        """
        # 内存缓存加速
        if item_id in self._seen_ids:
            return True

        # 检查持久化存储
        record = await self.get(self._content_key(item_id), default=None)
        if record is not None:
            self._seen_ids.add(item_id)
            return True

        # 兼容旧版本数据（迁移后删除）
        record = await self._read_legacy_content_record(item_id)
        if record is not None:
            # 迁移到新格式（永不过期）
            await self.mark_seen(item_id)
            return True

        return False

    async def mark_seen(self, item_id: str, ttl_seconds: int = 0) -> None:
        """标记条目为已推送。

        Args:
            item_id: 条目唯一标识
            ttl_seconds: 已废弃，保留参数用于向后兼容
        """
        self._seen_ids.add(item_id)
        await self.put(
            self._content_key(item_id),
            {
                "id": item_id,
                "pushed_at": int(time.time()),
            },
        )

    async def clear_seen(self) -> int:
        """Clear all seen records and return the number of deleted items."""
        deleted = 0
        keys_to_delete = []

        # Collect keys from memory cache
        for item_id in list(self._seen_ids):
            keys_to_delete.append(self._content_key(item_id))

        # Collect from disk state
        await self._ensure_state_loaded()
        kv_store = self._disk_state.get('kv', {})
        for key in list(kv_store.keys()):
            if key.startswith(self.CONTENT_KEY_PREFIX):
                if key not in keys_to_delete:
                    keys_to_delete.append(key)

        # Execute deletion
        for key in keys_to_delete:
            await self.delete(key)
            deleted += 1

        self._seen_ids.clear()
        return deleted

    async def cleanup_old_records(self, max_records: int = 1000) -> int:
        """清理旧的去重记录，只保留最近的 max_records 条。

        Args:
            max_records: 最大保留记录数，默认 1000

        Returns:
            删除的记录数量
        """
        await self._ensure_state_loaded()
        kv_store = self._disk_state.get('kv', {})

        # 收集所有去重记录及其推送时间
        records: list[tuple[str, int]] = []
        for key, value in kv_store.items():
            if not key.startswith(self.CONTENT_KEY_PREFIX):
                continue
            if not isinstance(value, dict):
                continue
            pushed_at = value.get("pushed_at", 0)
            records.append((key, pushed_at))

        if len(records) <= max_records:
            return 0

        # 按时间排序，删除最旧的
        records.sort(key=lambda x: x[1])
        to_delete = records[:-max_records]

        deleted = 0
        for key, _ in to_delete:
            await self.delete(key)
            deleted += 1

        # 同时清理内存缓存
        for key, _ in to_delete:
            # 从 key 提取 item_id
            item_id = key[len(self.CONTENT_KEY_PREFIX):]
            self._seen_ids.discard(item_id)

        return deleted

    async def archive_digest_items(
        self,
        items: list[dict[str, Any]],
        retention_seconds: int | None = None,
    ) -> int:
        if not items:
            return 0

        effective_retention = (
            int(retention_seconds)
            if retention_seconds is not None
            else self.DAILY_DIGEST_RETENTION_SECONDS
        )

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            archive = section.setdefault("archive", {})
            self._prune_digest_archive(archive, now, effective_retention)

            updated = 0
            for item in items:
                record = self._build_digest_archive_record(item, now)
                if not record:
                    continue
                archive[record["archive_key"]] = record
                updated += 1

            self._write_disk_state(state)
            return updated

        return int(self._with_state_lock(callback))

    async def list_digest_items(
        self,
        feed_ids: list[str],
        *,
        window_start_ts: int,
        window_end_ts: int,
        limit: int,
        retention_seconds: int | None = None,
    ) -> list[dict[str, Any]]:
        selected_feed_ids = {str(feed_id).strip() for feed_id in feed_ids if str(feed_id).strip()}
        if not selected_feed_ids or limit <= 0:
            return []

        effective_retention = (
            int(retention_seconds)
            if retention_seconds is not None
            else self.DAILY_DIGEST_RETENTION_SECONDS
        )

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            archive = section.setdefault("archive", {})
            self._prune_digest_archive(archive, now, effective_retention)

            matched: list[dict[str, Any]] = []
            for record in archive.values():
                if not isinstance(record, dict):
                    continue
                feed_id = str(record.get("feed_id", "")).strip()
                if feed_id not in selected_feed_ids:
                    continue
                record_ts = self._record_window_timestamp(record)
                if record_ts < window_start_ts or record_ts > window_end_ts:
                    continue
                matched.append(dict(record))

            matched.sort(
                key=lambda item: (
                    int(self._record_window_timestamp(item)),
                    int(item.get("collected_at", 0) or 0),
                ),
                reverse=True,
            )
            self._write_disk_state(state)
            return matched[:limit]

        return list(self._with_state_lock(callback) or [])

    async def get_daily_digest_status(self, digest_id: str) -> dict[str, Any]:
        digest_key = str(digest_id or "").strip()
        if not digest_key:
            return {}

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            status = section.setdefault("status", {})
            record = status.get(digest_key)
            return dict(record) if isinstance(record, dict) else {}

        return dict(self._with_state_lock(callback) or {})

    async def update_daily_digest_status(self, digest_id: str, **fields: Any) -> dict[str, Any]:
        digest_key = str(digest_id or "").strip()
        if not digest_key:
            return {}

        def callback(state: dict[str, Any], now: int):
            section = self._daily_digest_section(state)
            status = section.setdefault("status", {})
            record = status.get(digest_key)
            if not isinstance(record, dict):
                record = {}
            for key, value in fields.items():
                if value is None:
                    continue
                record[key] = value
            record["updated_at"] = now
            status[digest_key] = record
            self._write_disk_state(state)
            return dict(record)

        return dict(self._with_state_lock(callback) or {})

    async def claim_dispatch(self, fingerprint: str, ttl_seconds: int = 120) -> bool:
        """发送前占位，避免并发实例重复发送同一条消息。"""
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return True
        return bool(
            self._update_dispatch_guard(
                key,
                action="claim",
                ttl_seconds=max(int(ttl_seconds), 1),
            )
        )

    async def confirm_dispatch(self, fingerprint: str, ttl_seconds: int = 86400) -> None:
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return
        self._update_dispatch_guard(
            key,
            action="confirm",
            ttl_seconds=max(int(ttl_seconds), 1),
        )

    async def release_dispatch(self, fingerprint: str) -> None:
        key = self._dispatch_guard_key(fingerprint)
        if not key:
            return
        self._update_dispatch_guard(key, action="release")

    async def _get_dedup_version(self) -> int:
        if self._dedup_version is not None:
            return self._dedup_version
        raw = await self.get(self.DEDUP_VERSION_KEY, default=0)
        try:
            self._dedup_version = int(raw)
        except Exception:
            self._dedup_version = 0
        return self._dedup_version

    async def get_feed_state(self, feed_id: str) -> dict[str, Any]:
        return await self.get(self._feed_state_key(feed_id), default={})

    async def update_feed_state(
        self,
        feed_id: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        last_success_time: int | None = None,
        bootstrap_done: bool | None = None,
    ) -> dict[str, Any]:
        state = await self.get_feed_state(feed_id)
        if etag is not None:
            state["etag"] = etag
        if last_modified is not None:
            state["last_modified"] = last_modified
        if last_success_time is not None:
            state["last_success_time"] = last_success_time
        if bootstrap_done is not None:
            state["bootstrap_done"] = bool(bootstrap_done)
        await self.put(self._feed_state_key(feed_id), state)
        return state

    def build_dedup_key(self, item: dict[str, Any]) -> str:
        """优先使用 guid/id，其次使用 link 哈希。"""
        for field in ("guid", "id"):
            value = str(item.get(field, "")).strip()
            if value:
                return value

        link = str(item.get("link", "")).strip()
        if link:
            return hashlib.sha256(link.encode("utf-8")).hexdigest()

        # 兜底，避免空键导致重复推送。
        payload = json.dumps(item, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def build_link_fingerprint(self, item: dict[str, Any]) -> str:
        """基于规范化 link 生成第二层去重键。"""
        link = self._normalize_link(str(item.get("link", "")).strip())
        if not link:
            return ""
        digest = hashlib.sha256(link.encode("utf-8")).hexdigest()
        return f"link:{digest}"

    def build_seen_keys(self, item: dict[str, Any]) -> list[str]:
        """返回需要同时参与去重的键。"""
        keys: list[str] = []
        primary_key = str(self.build_dedup_key(item)).strip()
        if primary_key:
            keys.append(primary_key)

        link_fingerprint = self.build_link_fingerprint(item)
        if link_fingerprint and link_fingerprint not in keys:
            keys.append(link_fingerprint)
        return keys

    def build_digest_archive_key(self, item: dict[str, Any]) -> str:
        link_fingerprint = self.build_link_fingerprint(item)
        if link_fingerprint:
            return link_fingerprint
        seen_keys = self.build_seen_keys(item)
        return seen_keys[0] if seen_keys else ""

    def plugin_cache_dir(self) -> Path:
        """如需大文件缓存，请按规范写入 data/plugin_data/{plugin_name}/。"""
        if StarTools is not None:
            try:
                return Path(StarTools.get_data_dir(self._plugin_name))
            except Exception:
                pass
        return Path("data") / "plugin_data" / self._plugin_name

    async def _ensure_state_loaded(self) -> None:
        if self._state_loaded:
            return
        self._state_loaded = True
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    self._disk_state = loaded
        except (OSError, json.JSONDecodeError):
            self._disk_state = {'kv': {}}
        self._disk_state.setdefault('kv', {})

    def _flush_state(self) -> None:
        self._write_disk_state(self._disk_state)

    def _write_disk_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _daily_digest_section(state: dict[str, Any]) -> dict[str, Any]:
        section = state.setdefault("daily_digest", {})
        if not isinstance(section, dict):
            section = {}
            state["daily_digest"] = section
        section.setdefault("archive", {})
        section.setdefault("status", {})
        return section

    def _build_digest_archive_record(self, item: dict[str, Any], now: int) -> dict[str, Any] | None:
        archive_key = self.build_digest_archive_key(item)
        if not archive_key:
            return None
        seen_keys = self.build_seen_keys(item)
        return {
            "archive_key": archive_key,
            "item_key": str(self.build_dedup_key(item)).strip(),
            "seen_keys": seen_keys,
            "feed_id": str(item.get("feed_id", "")).strip(),
            "feed_title": str(item.get("feed_title", "") or item.get("source", "")).strip(),
            "title": str(item.get("title", "")).strip(),
            "summary": str(item.get("summary", "") or item.get("content", "") or "").strip(),
            "link": str(item.get("link", "")).strip(),
            "image_url": str(item.get("image_url", "")).strip(),
            "published_at": str(item.get("published_at", "")).strip(),
            "collected_at": now,
        }

    def _prune_digest_archive(
        self,
        archive: dict[str, Any],
        now: int,
        retention_seconds: int,
    ) -> None:
        cutoff = now - max(int(retention_seconds), 1)
        expired_keys = [
            key
            for key, record in archive.items()
            if not isinstance(record, dict) or int(record.get("collected_at", 0) or 0) < cutoff
        ]
        for key in expired_keys:
            archive.pop(key, None)

    @classmethod
    def _record_window_timestamp(cls, record: dict[str, Any]) -> int:
        published_ts = cls._parse_iso_timestamp(str(record.get("published_at", "")).strip())
        if published_ts is not None:
            return published_ts
        return int(record.get("collected_at", 0) or 0)

    @staticmethod
    def _parse_iso_timestamp(raw_value: str) -> int | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.astimezone(timezone.utc).timestamp())

    async def _read_raw_from_backend(self, key: str) -> Any:
        if self._get_kv_data is None:
            return self._fallback_store.get(key)
        try:
            # AstrBot PluginKVStoreMixin.get_kv_data(key, default)
            return await self._get_kv_data(key, None)
        except TypeError:
            # 兼容仅接收 key 的实现
            return await self._get_kv_data(key)

    def _decode_value(self, raw: Any) -> Any:
        if raw in (None, ""):
            return None
        if isinstance(raw, dict) and set(raw.keys()) == {"val"}:
            return self._decode_value(raw.get("val"))
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except json.JSONDecodeError:
                return raw
            return self._decode_value(decoded)
        return raw

    async def _read_legacy_content_record(self, item_id: str) -> dict[str, Any] | None:
        legacy_keys = [f"{self.CONTENT_KEY_PREFIX}{item_id}"]
        if self._dedup_version not in (None, 0):
            legacy_keys.append(f"{self.CONTENT_KEY_PREFIX}v0:{item_id}")

        for legacy_key in legacy_keys:
            record = await self.get(legacy_key, default=None)
            if record:
                await self.put(self._content_key(item_id), record)
                return record
        return None

    @classmethod
    def _feed_state_key(cls, feed_id: str) -> str:
        return f"{cls.FEED_STATE_PREFIX}{feed_id}"

    def _content_key(self, item_id: str) -> str:
        """生成去重记录的存储键。"""
        return f"{self.CONTENT_KEY_PREFIX}{item_id}"

    def _dispatch_guard_key(self, fingerprint: str) -> str:
        value = str(fingerprint or "").strip()
        if not value:
            return ""
        return f"{self.DISPATCH_GUARD_PREFIX}{value}"

    @staticmethod
    def _normalize_link(link: str) -> str:
        if not link:
            return ""
        parsed = urlsplit(link)
        if not any((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)):
            return link
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                "",
            )
        )

    @staticmethod
    def _is_guard_active(record: Any, now: int) -> bool:
        if not isinstance(record, dict):
            return False
        expire_at = int(record.get("expire_at", 0) or 0)
        return expire_at <= 0 or expire_at >= now

    def _load_disk_state_from_file(self) -> dict[str, Any]:
        try:
            if self._state_path.exists():
                with self._state_path.open("r", encoding="utf-8") as fp:
                    loaded = json.load(fp)
                if isinstance(loaded, dict):
                    loaded.setdefault('kv', {})
                    return loaded
        except (OSError, json.JSONDecodeError):
            pass
        return {'kv': {}}

    def _with_state_lock(self, callback: Callable[[dict[str, Any], int], Any]) -> Any:
        lock_path = self._state_path.parent / "state.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_fp:
            if fcntl is not None:
                fcntl.flock(lock_fp.fileno(), fcntl.LOCK_EX)
            try:
                state = self._load_disk_state_from_file()
                now = int(time.time())
                result = callback(state, now)
                self._disk_state = state
                self._state_loaded = True
                return result
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_fp.fileno(), fcntl.LOCK_UN)

    def _update_dispatch_guard(
        self,
        key: str,
        *,
        action: str,
        ttl_seconds: int = 0,
    ) -> bool | None:
        def callback(state: dict[str, Any], now: int):
            kv = state.setdefault('kv', {})
            record = kv.get(key)

            if action == "claim":
                if self._is_guard_active(record, now):
                    return False
                kv[key] = {
                    "state": "pending",
                    "expire_at": now + max(ttl_seconds, 1),
                    "updated_at": now,
                }
                self._write_disk_state(state)
                return True

            if action == "confirm":
                kv[key] = {
                    "state": "sent",
                    "expire_at": now + max(ttl_seconds, 1),
                    "updated_at": now,
                }
                self._write_disk_state(state)
                return None

            if action == "release":
                if isinstance(record, dict) and record.get("state") == "pending":
                    kv.pop(key, None)
                    self._write_disk_state(state)
                return None

            raise ValueError(f"unknown dispatch guard action: {action}")

        return self._with_state_lock(callback)
