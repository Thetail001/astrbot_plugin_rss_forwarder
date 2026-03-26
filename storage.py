import hashlib
import json
import time
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
    CONTENT_INDEX_KEY = "content_seen_index"
    DEDUP_VERSION_KEY = "content_seen_version"
    DISPATCH_GUARD_PREFIX = "dispatch_guard:"

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
        self._write_disk_state(self._disk_state)

    def _write_disk_state(self, state: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        with self._state_path.open("w", encoding="utf-8") as fp:
            json.dump(state, fp, ensure_ascii=False, sort_keys=True)

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
                    loaded.setdefault("kv", {})
                    return loaded
        except (OSError, json.JSONDecodeError):
            pass
        return {"kv": {}}

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
            kv = state.setdefault("kv", {})
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
