import asyncio
from contextlib import suppress

from astrbot.api import logger

from config import RSSConfig
from dispatcher import FeedDispatcher
from fetcher import FeedFetcher
from parser import FeedParser
from storage import FeedStorage


class RSSScheduler:
    """调度层：周期执行抓取 -> 解析 -> 去重 -> 分发。"""

    def __init__(
        self,
        config: RSSConfig,
        fetcher: FeedFetcher,
        parser: FeedParser,
        dispatcher: FeedDispatcher,
        storage: FeedStorage,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._parser = parser
        self._dispatcher = dispatcher
        self._storage = storage
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def start(self) -> None:
        if self.running:
            return
        self._task = asyncio.create_task(self._loop(), name="rss-scheduler")
        logger.info("RSS scheduler started")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        logger.info("RSS scheduler stopped")

    async def _loop(self) -> None:
        while True:
            await self.run_once()
            await asyncio.sleep(self._config.poll_interval_seconds)

    async def run_once(self) -> None:
        raw_items = await self._fetcher.fetch()
        items = self._parser.parse(raw_items)
        for item in items:
            item_id = str(item.get("id", ""))
            if not item_id or await self._storage.has_seen(item_id):
                continue
            await self._dispatcher.dispatch(item)
            await self._storage.mark_seen(item_id)
