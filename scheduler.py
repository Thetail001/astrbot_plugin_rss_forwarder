import asyncio
import inspect
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime

from astrbot.api import logger

from config import JobConfig, RSSConfig
from dispatcher import FeedDispatcher
from fetcher import FeedFetcher
from parser import FeedParser
from storage import FeedStorage


@dataclass(slots=True)
class JobExecutionResult:
    """任务执行结果快照。"""

    started_at: datetime
    duration_ms: int
    fetched_count: int
    pushed_count: int
    error_summary: str = ""


class RSSScheduler:
    """调度层：按 job 独立周期执行 fetch -> parse -> dedup -> dispatch。"""

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
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._job_results: dict[str, JobExecutionResult] = {}

    @property
    def running(self) -> bool:
        return any(not task.done() for task in self._job_tasks.values())

    @property
    def last_results(self) -> dict[str, JobExecutionResult]:
        return dict(self._job_results)

    async def start(self) -> None:
        if self.running:
            return

        for job in self._config.jobs:
            if not job.enabled:
                continue
            self._register_job(job)

        logger.info("RSS scheduler started, registered jobs=%s", list(self._job_tasks.keys()))

    async def stop(self) -> None:
        if not self._job_tasks:
            return

        tasks = list(self._job_tasks.values())
        for task in tasks:
            task.cancel()

        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task

        self._job_tasks.clear()
        self._job_locks.clear()
        logger.info("RSS scheduler stopped")

    async def run_once(self) -> None:
        """手动触发：并发执行所有启用 job。"""
        await asyncio.gather(
            *(self._run_job_once_guarded(job) for job in self._config.jobs if job.enabled)
        )

    def _register_job(self, job: JobConfig) -> None:
        if job.id in self._job_tasks and not self._job_tasks[job.id].done():
            return

        self._job_locks.setdefault(job.id, asyncio.Lock())
        self._job_tasks[job.id] = asyncio.create_task(
            self._job_loop(job),
            name=f"rss-job-{job.id}",
        )

    async def _job_loop(self, job: JobConfig) -> None:
        interval = self._resolve_interval(job)
        while True:
            await self._run_job_once_guarded(job)
            await asyncio.sleep(interval)

    def _resolve_interval(self, job: JobConfig) -> int:
        if job.interval_seconds > 0:
            return job.interval_seconds

        logger.warning(
            "job=%s configured with cron=%s but cron scheduling is not implemented yet, fallback interval=%s",
            job.id,
            job.cron,
            self._config.poll_interval_seconds,
        )
        return self._config.poll_interval_seconds

    async def _run_job_once_guarded(self, job: JobConfig) -> None:
        job_lock = self._job_locks.setdefault(job.id, asyncio.Lock())
        if job_lock.locked():
            logger.warning("skip job=%s: previous run still in progress", job.id)
            return

        async with job_lock:
            started_at = datetime.now()
            started_perf = time.perf_counter()
            fetched_count = 0
            pushed_count = 0
            error_summary = ""

            try:
                raw_items = await self._call_fetch(job)
                fetched_count = len(raw_items)

                items = self._call_parse(raw_items, job)
                for item in items:
                    item_id = self._storage.build_dedup_key(item)
                    if not item_id or await self._storage.has_seen(item_id):
                        continue

                    event_item = dict(item)
                    event_item.setdefault("job_id", job.id)
                    await self._dispatcher.dispatch(event_item)
                    await self._storage.mark_seen(item_id)
                    pushed_count += 1

                feed_meta = self._extract_feed_meta(raw_items)
                now_ts = int(time.time())
                for feed_id in job.feed_ids:
                    meta = feed_meta.get(feed_id, {})
                    await self._storage.update_feed_state(
                        feed_id,
                        etag=meta.get("etag"),
                        last_modified=meta.get("last_modified"),
                        last_success_time=now_ts,
                    )
            except Exception as exc:
                error_summary = f"{type(exc).__name__}: {exc}"
                logger.exception("job=%s execution failed", job.id)

            duration_ms = int((time.perf_counter() - started_perf) * 1000)
            self._job_results[job.id] = JobExecutionResult(
                started_at=started_at,
                duration_ms=duration_ms,
                fetched_count=fetched_count,
                pushed_count=pushed_count,
                error_summary=error_summary,
            )
            logger.info(
                "job=%s finished: fetched=%s pushed=%s duration_ms=%s error=%s",
                job.id,
                fetched_count,
                pushed_count,
                duration_ms,
                error_summary or "",
            )

    async def _call_fetch(self, job: JobConfig) -> list[dict]:
        fetch_func = self._fetcher.fetch
        if self._accepts_argument(fetch_func):
            return await fetch_func(job)
        return await fetch_func()

    def _call_parse(self, raw_items: list[dict], job: JobConfig) -> list[dict]:
        parse_func = self._parser.parse
        if self._accepts_argument(parse_func, expected_count=2):
            return parse_func(raw_items, job)
        return parse_func(raw_items)

    @staticmethod
    def _extract_feed_meta(raw_items: list[dict]) -> dict[str, dict[str, str]]:
        """从抓取结果中提取可选的 feed 元信息。"""
        meta_by_feed: dict[str, dict[str, str]] = {}
        for raw_item in raw_items:
            if not isinstance(raw_item, dict):
                continue
            feed_id = str(raw_item.get("feed_id", "")).strip()
            if not feed_id:
                continue
            etag = str(raw_item.get("etag", "")).strip()
            last_modified = str(raw_item.get("last_modified", "")).strip()
            if not etag and not last_modified:
                continue
            meta_by_feed[feed_id] = {
                "etag": etag,
                "last_modified": last_modified,
            }
        return meta_by_feed

    @staticmethod
    def _accepts_argument(func, expected_count: int = 1) -> bool:
        signature = inspect.signature(func)
        positional_or_keyword_params = [
            param
            for param in signature.parameters.values()
            if param.kind
            in {inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD}
        ]
        return len(positional_or_keyword_params) >= expected_count
