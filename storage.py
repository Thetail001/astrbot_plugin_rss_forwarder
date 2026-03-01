import hashlib
import json
import time
from pathlib import Path
from typing import Any, Awaitable, Callable


class FeedStorage:
    """存储层：负责去重、游标和持久化。"""

    FEED_STATE_PREFIX = "feed_state:"
    CONTENT_KEY_PREFIX = "content_seen:"

    def __init__(
        self,
        plugin_name: str = "astrbot_rss",
        get_kv_data: Callable[[str], Awaitable[Any]] | None = None,
        put_kv_data: Callable[[str, Any], Awaitable[Any]] | None = None,
        delete_kv_data: Callable[[str], Awaitable[Any]] | None = None,
    ) -> None:
        self._plugin_name = plugin_name
        self._get_kv_data = get_kv_data
        self._put_kv_data = put_kv_data
        self._delete_kv_data = delete_kv_data
        self._fallback_store: dict[str, str] = {}
        self._seen_ids: set[str] = set()

    async def get(self, key: str, default: Any = None) -> Any:
        """封装 KV 读取。"""
        if self._get_kv_data is None:
            raw = self._fallback_store.get(key)
        else:
            try:
                # AstrBot PluginKVStoreMixin.get_kv_data(key, default)
                raw = await self._get_kv_data(key, None)
            except TypeError:
                # 兼容仅接收 key 的实现
                raw = await self._get_kv_data(key)
        if raw in (None, ""):
            return default
        if isinstance(raw, str):
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                return raw
        return raw

    async def put(self, key: str, value: Any) -> None:
        """封装 KV 写入。"""
        encoded = json.dumps(value, ensure_ascii=False)
        if self._put_kv_data is None:
            self._fallback_store[key] = encoded
            return
        await self._put_kv_data(key, encoded)

    async def delete(self, key: str) -> None:
        """封装 KV 删除。"""
        if self._delete_kv_data is None:
            self._fallback_store.pop(key, None)
            return
        await self._delete_kv_data(key)

    async def has_seen(self, item_id: str) -> bool:
        if item_id in self._seen_ids:
            return True
        record = await self.get(self._content_key(item_id), default=None)
        if not record:
            return False
        expire_at = int(record.get("expire_at", 0))
        if expire_at and expire_at < int(time.time()):
            await self.delete(self._content_key(item_id))
            return False
        self._seen_ids.add(item_id)
        return True

    async def mark_seen(self, item_id: str, ttl_seconds: int = 86400) -> None:
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

    async def get_feed_state(self, feed_id: str) -> dict[str, Any]:
        return await self.get(self._feed_state_key(feed_id), default={})

    async def update_feed_state(
        self,
        feed_id: str,
        *,
        etag: str | None = None,
        last_modified: str | None = None,
        last_success_time: int | None = None,
    ) -> dict[str, Any]:
        state = await self.get_feed_state(feed_id)
        if etag is not None:
            state["etag"] = etag
        if last_modified is not None:
            state["last_modified"] = last_modified
        if last_success_time is not None:
            state["last_success_time"] = last_success_time
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

    def plugin_cache_dir(self) -> Path:
        """如需大文件缓存，请按规范写入 data/plugin_data/{plugin_name}/。"""
        return Path("data") / "plugin_data" / self._plugin_name

    @classmethod
    def _feed_state_key(cls, feed_id: str) -> str:
        return f"{cls.FEED_STATE_PREFIX}{feed_id}"

    @classmethod
    def _content_key(cls, item_id: str) -> str:
        return f"{cls.CONTENT_KEY_PREFIX}{item_id}"
