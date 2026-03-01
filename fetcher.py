import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import Request, urlopen

from astrbot.api import logger

from .config import RSSConfig
from .storage import FeedStorage


@dataclass(slots=True)
class FetchedFeed:
    feed_id: str
    body: str
    etag: str
    last_modified: str
    status: int


class FeedFetcher:
    """抓取层：负责从远端源拉取原始 XML 数据。"""

    def __init__(self, config: RSSConfig, storage: FeedStorage) -> None:
        self._config = config
        self._storage = storage

    async def fetch(self, job) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        feed_map = {feed.id: feed for feed in self._config.feeds if feed.enabled}
        for feed_id in job.feed_ids:
            feed = feed_map.get(feed_id)
            if feed is None:
                continue
            fetched = await self._fetch_single_feed(feed)
            if fetched is None:
                continue
            items.append(
                {
                    "feed_id": fetched.feed_id,
                    "body": fetched.body,
                    "etag": fetched.etag,
                    "last_modified": fetched.last_modified,
                    "status": fetched.status,
                }
            )
        return items

    async def _fetch_single_feed(self, feed) -> FetchedFeed | None:
        state = await self._storage.get_feed_state(feed.id)
        etag = str(state.get("etag", "")).strip()
        last_modified = str(state.get("last_modified", "")).strip()

        url, headers = self._build_url_and_headers(feed)
        if etag:
            headers["If-None-Match"] = etag
        if last_modified:
            headers["If-Modified-Since"] = last_modified

        def _request_once():
            req = Request(url=url, headers=headers)
            with urlopen(req, timeout=feed.timeout) as resp:  # noqa: S310
                body = resp.read().decode("utf-8", errors="ignore")
                return FetchedFeed(
                    feed_id=feed.id,
                    body=body,
                    etag=str(resp.headers.get("ETag", "")).strip(),
                    last_modified=str(resp.headers.get("Last-Modified", "")).strip(),
                    status=int(getattr(resp, "status", 200) or 200),
                )

        try:
            return await asyncio.to_thread(_request_once)
        except Exception as exc:
            # urllib 对 304 也会抛异常，直接忽略
            if "304" in str(exc):
                logger.info("feed=%s not modified (304)", feed.id)
                return None
            logger.warning("fetch feed=%s failed: %s", feed.id, exc)
            return None

    @staticmethod
    def _build_url_and_headers(feed) -> tuple[str, dict[str, str]]:
        headers = {
            "User-Agent": "AstrBot-RSS/0.2 (+https://github.com/AstrBot-RSS)",
            "Accept": "application/rss+xml, application/atom+xml, application/xml, text/xml;q=0.9, */*;q=0.1",
        }
        url = feed.url

        if feed.auth_mode == "query" and feed.key:
            parsed = urlparse(url)
            q = dict(parse_qsl(parsed.query, keep_blank_values=True))
            q["key"] = feed.key
            url = urlunparse(parsed._replace(query=urlencode(q)))
        elif feed.auth_mode == "header" and feed.key:
            headers["Authorization"] = f"Bearer {feed.key}"

        return url, headers
