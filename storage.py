import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable
from urllib.parse import urlsplit, urlunsplit

try:
    from astrbot.api.star import StarTools
except ImportError:  # pragma: no cover - unit tests may run without AstrBot runtime.
    StarTools = None


class FeedStorage:
    """存储层：负责去重、游标和持久化。"""

    FEED_STATE_PREFIX = "feed_state:"
    CONTENT_KEY_PREFIX = "content_seen:"
    CONTENT_INDEX_KEY = "content_seen_index"
    DEDUP_VERSION_KEY = "content_seen_version"

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
        self._disk_state: dict[str, Any] = {"kv": {}}

    async def get(self, key: str, default: Any = None) -> Any:
        """封装 KV 读取。"""
        await self._ensure_state_loaded()
        kv_store = self._disk_state.setdefault("kv", {})
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
        self._disk_state.setdefault("kv", {})[key] = value
        self._flush_state()

        encoded = json.dumps(value, ensure_ascii=False)
        if self._put_kv_data is None:
            self._fallback_store[key] = encoded
            return
        await self._put_kv_data(key, encoded)

    async def delete(self, key: str) -> None:
        """封装 KV 删除。"""
        await self._ensure_state_loaded()
        self._disk_state.setdefault("kv", {}).pop(key, None)
        self._flush_state()

        if self._delete_kv_data is None:
            self._fallback_store.pop(key, None)
            return
        await self._delete_kv_data(key)

    async def has_seen(self, item_id: str) -> bool:
        await self._get_dedup_version()

        # NOTE:
        # _seen_ids is only an in-memory acceleration set and does not carry TTL.
        # We still need to validate persisted record expiration to avoid permanent
        # false positives after long-running processes.
        cached = item_id in self._seen_ids
        record = await self.get(self._content_key(item_id), default=None)
        if not record:
            record = await self._read_legacy_content_record(item_id)
        if not record:
            if cached:
                self._seen_ids.discard(item_id)
            return False

        expire_at = int(record.get("expire_at", 0))
        if expire_at and expire_at < int(time.time()):
            await self.delete(self._content_key(item_id))
            self._seen_ids.discard(item_id)
            return False

        self._seen_ids.add(item_id)
        return True

    async def mark_seen(self, item_id: str, ttl_seconds: int = 86400) -> None:
        await self._get_dedup_version()
        self._seen_ids.add(item_id)
        expire_at = int(time.time()) + max(ttl_seconds, 0)
        await self.put(
            self._content_key(item_id),
            {
                "id": item_id,
                "expire_at": expire_at,
                "updated_at": int(time.time()),
            },
        )
        seen_index = await self.get(self.CONTENT_INDEX_KEY, default=[])
        if not isinstance(seen_index, list):
            seen_index = []
        if item_id not in seen_index:
            seen_index.append(item_id)
            await self.put(self.CONTENT_INDEX_KEY, seen_index)

    async def clear_seen(self) -> int:
        """清空已推送去重记录，返回删除数量。

        说明：在 KV 不支持按前缀枚举键时，采用去重版本号自增实现“逻辑清空”，
        旧版本键即使存在也不会再命中。
        """
        await self._get_dedup_version()
        seen_index = await self.get(self.CONTENT_INDEX_KEY, default=[])
        if not isinstance(seen_index, list):
            seen_index = []

        deleted = 0
        for item_id in seen_index:
            await self.delete(self._content_key(str(item_id)))
            deleted += 1

        await self.delete(self.CONTENT_INDEX_KEY)
        self._seen_ids.clear()
        version = await self._get_dedup_version()
        self._dedup_version = version + 1
        await self.put(self.DEDUP_VERSION_KEY, self._dedup_version)
        return deleted

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
            self._disk_state = {"kv": {}}
        self._disk_state.setdefault("kv", {})

    def _flush_state(self) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as fp:
            json.dump(self._disk_state, fp, ensure_ascii=False, sort_keys=True)

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
        # 仅依赖内存缓存；首次使用前由 _get_dedup_version 初始化。
        version = self._dedup_version if self._dedup_version is not None else 0
        return f"{self.CONTENT_KEY_PREFIX}v{version}:{item_id}"

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
