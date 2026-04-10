import asyncio
import inspect
import time
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from zoneinfo import ZoneInfo

from astrbot.api import logger

from .config import DailyDigestConfig, JobConfig, RSSConfig
from .dispatcher import FeedDispatcher
from .fetcher import FeedFetcher
from .parser import FeedParser
from .pipeline import FeedPipeline
from .storage import FeedStorage


@dataclass(slots=True)
class JobExecutionResult:
    """任务执行结果快照。"""

    started_at: datetime
    duration_ms: int
    fetched_count: int
    pushed_count: int
    error_summary: str = ""


@dataclass(slots=True)
class DigestExecutionResult:
    """日报执行结果快照。"""

    started_at: datetime
    duration_ms: int
    item_count: int
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
        pipeline: FeedPipeline | None = None,
    ) -> None:
        self._config = config
        self._fetcher = fetcher
        self._parser = parser
        self._dispatcher = dispatcher
        self._storage = storage
        self._pipeline = pipeline
        self._job_tasks: dict[str, asyncio.Task] = {}
        self._job_locks: dict[str, asyncio.Lock] = {}
        self._job_results: dict[str, JobExecutionResult] = {}
        self._paused_jobs: set[str] = set()
        self._digest_feed_tasks: dict[str, asyncio.Task] = {}
        self._digest_feed_locks: dict[str, asyncio.Lock] = {}
        self._digest_send_tasks: dict[str, asyncio.Task] = {}
        self._digest_send_locks: dict[str, asyncio.Lock] = {}
        self._digest_results: dict[str, DigestExecutionResult] = {}

    @property
    def running(self) -> bool:
        all_tasks = [
            *self._job_tasks.values(),
            *self._digest_feed_tasks.values(),
            *self._digest_send_tasks.values(),
        ]
        return any(not task.done() for task in all_tasks)

    @property
    def last_results(self) -> dict[str, JobExecutionResult]:
        return dict(self._job_results)

    @property
    def digest_results(self) -> dict[str, DigestExecutionResult]:
        return dict(self._digest_results)

    @property
    def config(self) -> RSSConfig:
        return self._config

    @property
    def storage(self) -> FeedStorage:
        return self._storage

    async def start(self) -> None:
        if self.running:
            return

        await self._cancel_stale_job_tasks()
        for job in self._jobs():
            if not job.enabled:
                continue
            self._register_job(job)
        for feed_id in self._digest_only_feed_ids():
            self._register_digest_feed(feed_id)
        for digest in self._daily_digests():
            if not digest.enabled:
                continue
            self._register_daily_digest(digest)

        logger.info(
            "RSS scheduler started, registered jobs=%s digest_feeds=%s daily_digests=%s",
            list(self._job_tasks.keys()),
            list(self._digest_feed_tasks.keys()),
            list(self._digest_send_tasks.keys()),
        )

    async def stop(self) -> None:
        all_tasks = [
            *self._job_tasks.values(),
            *self._digest_feed_tasks.values(),
            *self._digest_send_tasks.values(),
        ]
        if not all_tasks:
            return

        for task in all_tasks:
            task.cancel()

        for task in all_tasks:
            with suppress(asyncio.CancelledError):
                await task

        self._job_tasks.clear()
        self._job_locks.clear()
        self._digest_feed_tasks.clear()
        self._digest_feed_locks.clear()
        self._digest_send_tasks.clear()
        self._digest_send_locks.clear()
        logger.info("RSS scheduler stopped")

    async def _cancel_stale_job_tasks(self) -> None:
        current_task = asyncio.current_task()
        stale_tasks: list[asyncio.Task] = []
        for task in asyncio.all_tasks():
            if task is current_task or task.done():
                continue
            task_name = task.get_name()
            if not task_name.startswith(
                (
                    "rss-job-",
                    "rss-digest-feed-",
                    "rss-digest-send-",
                )
            ):
                continue
            stale_tasks.append(task)

        if not stale_tasks:
            return

        for task in stale_tasks:
            task.cancel()

        for task in stale_tasks:
            with suppress(asyncio.CancelledError):
                await task

        logger.warning(
            "cancelled stale rss scheduler tasks=%s",
            [task.get_name() for task in stale_tasks],
        )

    async def run_once(self) -> None:
        """手动触发：并发执行所有启用 job。"""
        await self.run_job_once()

    async def run_job_once(self, job_id: str | None = None) -> bool:
        """手动触发任务，job_id 为空时触发所有启用且未暂停任务。"""
        if job_id:
            job = self.get_job(job_id)
            if job is None or not job.enabled or job.id in self._paused_jobs:
                return False
            await self._run_job_once_guarded(job)
            return True

        await asyncio.gather(
            *(
                self._run_job_once_guarded(job)
                for job in self._jobs()
                if job.enabled and job.id not in self._paused_jobs
            )
        )
        return True

    async def run_daily_digest_once(self, digest_id: str) -> bool:
        digest = self.get_daily_digest(digest_id)
        if digest is None or not digest.enabled:
            return False
        await self._run_daily_digest_once_guarded(digest, scheduled_date=None)
        return True

    def get_job(self, job_id: str) -> JobConfig | None:
        return next((job for job in self._jobs() if job.id == job_id), None)

    def get_daily_digest(self, digest_id: str) -> DailyDigestConfig | None:
        return next(
            (
                digest
                for digest in self._daily_digests()
                if digest.id == digest_id
            ),
            None,
        )

    async def pause_job(self, job_id: str) -> bool:
        """暂停指定任务。"""
        job = self.get_job(job_id)
        if job is None or not job.enabled:
            return False

        self._paused_jobs.add(job_id)
        task = self._job_tasks.get(job_id)
        if task and not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        self._job_tasks.pop(job_id, None)
        return True

    def resume_job(self, job_id: str) -> bool:
        """恢复指定任务。"""
        job = self.get_job(job_id)
        if job is None or not job.enabled:
            return False

        self._paused_jobs.discard(job_id)
        self._register_job(job)
        return True

    @property
    def paused_jobs(self) -> set[str]:
        return set(self._paused_jobs)

    async def test_translation(self, sample_text: str = "") -> dict:
        """执行翻译链路测试，不触发分发与去重。"""
        if self._pipeline is None:
            return {"error": "pipeline_not_configured"}

        diagnose_func = getattr(self._pipeline, "diagnose_translation", None)
        if not callable(diagnose_func):
            return {"error": "pipeline_diagnose_not_supported"}

        sample_entry = {
            "title": "RSS Translation Test",
            "summary": sample_text or "This is a translation diagnostics message from RSS forwarder.",
            "content": sample_text or "",
        }
        report = await diagnose_func(sample_entry)
        report["config"] = {
            "llm_enabled": bool(getattr(self._config, "llm_enabled", False)),
            "llm_timeout_seconds": int(getattr(self._config, "llm_timeout_seconds", 0) or 0),
            "llm_proxy_mode": str(getattr(self._config, "llm_proxy_mode", "system") or "system"),
            "google_translate_enabled": bool(
                getattr(self._config, "google_translate_enabled", False)
            ),
            "google_translate_target_lang": str(
                getattr(self._config, "google_translate_target_lang", "zh-CN") or "zh-CN"
            ),
            "google_translate_timeout_seconds": int(
                getattr(self._config, "google_translate_timeout_seconds", 0) or 0
            ),
            "google_translate_proxy_mode": str(
                getattr(self._config, "google_translate_proxy_mode", "system") or "system"
            ),
            "github_models_enabled": bool(
                getattr(self._config, "github_models_enabled", False)
            ),
            "github_models_model": str(
                getattr(self._config, "github_models_model", "openai/gpt-4o-mini")
                or "openai/gpt-4o-mini"
            ),
            "github_models_timeout_seconds": int(
                getattr(self._config, "github_models_timeout_seconds", 0) or 0
            ),
            "github_models_proxy_mode": str(
                getattr(self._config, "github_models_proxy_mode", "system") or "system"
            ),
        }
        return report

    def _digest_only_feed_ids(self) -> list[str]:
        realtime_feed_ids = {
            feed_id
            for job in self._jobs()
            if job.enabled
            for feed_id in job.feed_ids
        }
        digest_feed_ids = {
            feed_id
            for digest in self._daily_digests()
            if digest.enabled
            for feed_id in digest.feed_ids
        }
        return sorted(digest_feed_ids - realtime_feed_ids)

    def _register_digest_feed(self, feed_id: str) -> None:
        if feed_id in self._digest_feed_tasks and not self._digest_feed_tasks[feed_id].done():
            return
        self._digest_feed_locks.setdefault(feed_id, asyncio.Lock())
        self._digest_feed_tasks[feed_id] = asyncio.create_task(
            self._digest_feed_loop(feed_id),
            name=f"rss-digest-feed-{feed_id}",
        )

    def _register_daily_digest(self, digest: DailyDigestConfig) -> None:
        if digest.id in self._digest_send_tasks and not self._digest_send_tasks[digest.id].done():
            return
        self._digest_send_locks.setdefault(digest.id, asyncio.Lock())
        self._digest_send_tasks[digest.id] = asyncio.create_task(
            self._daily_digest_loop(digest),
            name=f"rss-digest-send-{digest.id}",
        )

    def _register_job(self, job: JobConfig) -> None:
        if job.id in self._job_tasks and not self._job_tasks[job.id].done():
            return

        self._job_locks.setdefault(job.id, asyncio.Lock())
        self._job_tasks[job.id] = asyncio.create_task(
            self._job_loop(job),
            name=f"rss-job-{job.id}",
        )

    async def _digest_feed_loop(self, feed_id: str) -> None:
        initial_delay = self._startup_delay_seconds()
        if initial_delay > 0:
            logger.info(
                "digest-feed=%s waiting startup delay=%ss before first poll",
                feed_id,
                initial_delay,
            )
            await asyncio.sleep(initial_delay)
        while True:
            await self._collect_digest_feed_once(feed_id)
            await asyncio.sleep(self._poll_interval_seconds())

    async def _daily_digest_loop(self, digest: DailyDigestConfig) -> None:
        initial_delay = self._startup_delay_seconds()
        if initial_delay > 0:
            logger.info(
                "daily-digest=%s waiting startup delay=%ss before first check",
                digest.id,
                initial_delay,
            )
            await asyncio.sleep(initial_delay)
        while True:
            if await self._should_run_daily_digest(digest):
                await self._run_daily_digest_once_guarded(
                    digest,
                    scheduled_date=self._local_now().date().isoformat(),
                )
            await asyncio.sleep(30)

    async def _job_loop(self, job: JobConfig) -> None:
        interval = self._resolve_interval(job)
        initial_delay = self._startup_delay_seconds()
        if initial_delay > 0:
            logger.info("job=%s waiting startup delay=%ss before first poll", job.id, initial_delay)
            await asyncio.sleep(initial_delay)
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
            self._poll_interval_seconds(),
        )
        return self._poll_interval_seconds()

    def _jobs(self) -> list[JobConfig]:
        return list(getattr(self._config, "jobs", []) or [])

    def _daily_digests(self) -> list[DailyDigestConfig]:
        return list(getattr(self._config, "daily_digests", []) or [])

    def _targets(self) -> list[Any]:
        return list(getattr(self._config, "targets", []) or [])

    def _startup_delay_seconds(self) -> int:
        return max(int(getattr(self._config, "startup_delay_seconds", 0) or 0), 0)

    def _poll_interval_seconds(self) -> int:
        return max(int(getattr(self._config, "poll_interval_seconds", 300) or 300), 1)

    def _local_timezone(self):
        timezone_name = str(getattr(self._config, "timezone", "Asia/Shanghai") or "Asia/Shanghai")
        try:
            return ZoneInfo(timezone_name)
        except Exception:
            return timezone.utc

    def _local_now(self) -> datetime:
        return datetime.now(self._local_timezone())

    def _format_local_timestamp(self, timestamp_value: int) -> str:
        parsed = datetime.fromtimestamp(int(timestamp_value), tz=timezone.utc)
        return parsed.astimezone(self._local_timezone()).strftime("%Y-%m-%d %H:%M")

    async def _should_run_daily_digest(self, digest: DailyDigestConfig) -> bool:
        now_local = self._local_now()
        if now_local.strftime("%H:%M") < digest.send_time:
            return False
        status = await self._get_daily_digest_status(digest.id)
        return str(status.get("last_schedule_date", "") or "") != now_local.date().isoformat()

    async def _collect_digest_feed_once(self, feed_id: str) -> None:
        feed_lock = self._digest_feed_locks.setdefault(feed_id, asyncio.Lock())
        if feed_lock.locked():
            logger.warning("skip digest-feed=%s: previous collection still in progress", feed_id)
            return

        async with feed_lock:
            try:
                raw_items = await self._call_fetch_feed_ids([feed_id])
                if not raw_items:
                    return
                items = self._call_parse(raw_items, None)
                await self._archive_items(items)
                feed_meta = self._extract_feed_meta(raw_items)
                now_ts = int(time.time())
                meta = feed_meta.get(feed_id, {})
                await self._storage.update_feed_state(
                    feed_id,
                    etag=meta.get("etag"),
                    last_modified=meta.get("last_modified"),
                    last_success_time=now_ts,
                    bootstrap_done=True,
                )
                logger.info(
                    "digest-feed=%s collected: fetched=%s parsed=%s",
                    feed_id,
                    len(raw_items),
                    len(items),
                )
            except Exception:
                logger.exception("digest-feed=%s collection failed", feed_id)

    async def _archive_items(self, items: list[dict]) -> None:
        archive_func = getattr(self._storage, "archive_digest_items", None)
        if not callable(archive_func):
            return
        try:
            await archive_func(items)
        except Exception:
            logger.exception("archive digest items failed")

    async def _call_fetch_feed_ids(self, feed_ids: list[str]) -> list[dict]:
        fetch_feed_ids = getattr(self._fetcher, "fetch_feed_ids", None)
        if callable(fetch_feed_ids):
            return await fetch_feed_ids(feed_ids)

        temp_job = type("TempJob", (), {"feed_ids": feed_ids})()
        return await self._call_fetch(temp_job)

    async def _run_daily_digest_once_guarded(
        self,
        digest: DailyDigestConfig,
        *,
        scheduled_date: str | None,
    ) -> None:
        digest_lock = self._digest_send_locks.setdefault(digest.id, asyncio.Lock())
        if digest_lock.locked():
            logger.warning("skip daily-digest=%s: previous run still in progress", digest.id)
            return

        async with digest_lock:
            started_at = datetime.now()
            started_perf = time.perf_counter()
            item_count = 0
            pushed_count = 0
            error_summary = ""
            status_fields: dict[str, Any] = {}

            try:
                window_end_ts = int(time.time())
                window_start_ts = window_end_ts - max(int(digest.window_hours), 1) * 3600
                items = await self._list_daily_digest_items(
                    digest.feed_ids,
                    window_start_ts=window_start_ts,
                    window_end_ts=window_end_ts,
                    limit=digest.max_items,
                )
                item_count = len(items)
                status_fields.update(
                    {
                        "last_run_at": window_end_ts,
                        "last_window_start_ts": window_start_ts,
                        "last_window_end_ts": window_end_ts,
                        "last_item_count": item_count,
                    }
                )
                if scheduled_date:
                    status_fields["last_schedule_date"] = scheduled_date

                if not items:
                    status_fields["last_error"] = ""
                    status_fields["last_skipped_reason"] = "empty_window"
                    await self._update_daily_digest_status(digest.id, **status_fields)
                    logger.info("daily-digest=%s skipped: empty window", digest.id)
                    return

                target_origins = self._resolve_digest_target_origins(digest.target_ids)
                first_origin = target_origins[0] if target_origins else ""
                digest_context = {
                    "id": digest.id,
                    "title": digest.title,
                    "prompt_template": digest.prompt_template,
                    "max_items": digest.max_items,
                    "window_start_text": self._format_local_timestamp(window_start_ts),
                    "window_end_text": self._format_local_timestamp(window_end_ts),
                }
                if self._pipeline is not None:
                    content_result = await self._pipeline.build_daily_digest_content(
                        digest_context,
                        items,
                        unified_msg_origin=first_origin,
                    )
                    content = str(content_result.get("content", "")).strip()
                    content_engine = str(content_result.get("engine", "fallback") or "fallback")
                    llm_reason = str(content_result.get("llm_reason", "") or "")
                else:
                    content = "\n".join(
                        f"{index}. [{str(item.get('feed_title', '') or item.get('source', '') or '未知来源').strip() or '未知来源'}] {str(item.get('title', '')).strip() or '(无标题)'}"
                        for index, item in enumerate(items, start=1)
                    )
                    content_engine = "fallback"
                    llm_reason = "pipeline_not_configured"

                digest_payload = {
                    "id": digest.id,
                    "title": digest.title or digest.id,
                    "target_ids": digest.target_ids,
                    "render_mode": digest.render_mode,
                    "window_start_text": self._format_local_timestamp(window_start_ts),
                    "window_end_text": self._format_local_timestamp(window_end_ts),
                    "item_count": item_count,
                    "content": content,
                    "links": [
                        {
                            "source": str(
                                item.get("feed_title", "") or item.get("source", "") or "未知来源"
                            ).strip()
                            or "未知来源",
                            "link": str(item.get("link", "") or "").strip(),
                        }
                        for item in items
                        if str(item.get("link", "") or "").strip()
                    ],
                }
                dispatch_result = await self._dispatcher.dispatch_daily_digest(digest_payload)
                pushed_count = dispatch_result.success_count
                status_fields["last_content_engine"] = content_engine
                status_fields["last_llm_reason"] = llm_reason

                if pushed_count > 0:
                    status_fields["last_sent_at"] = int(time.time())
                    status_fields["last_error"] = ""
                    status_fields["last_skipped_reason"] = ""
                elif (
                    dispatch_result.skipped_duplicate_count > 0
                    and dispatch_result.permanent_failure_count == 0
                    and dispatch_result.transient_failure_count == 0
                ):
                    status_fields["last_error"] = ""
                    status_fields["last_skipped_reason"] = "duplicate"
                else:
                    error_summary = "dispatch_failed"
                    status_fields["last_error"] = error_summary
                    status_fields["last_skipped_reason"] = ""

                await self._update_daily_digest_status(digest.id, **status_fields)
                logger.info(
                    "daily-digest=%s finished: items=%s pushed=%s engine=%s error=%s",
                    digest.id,
                    item_count,
                    pushed_count,
                    status_fields.get("last_content_engine", ""),
                    error_summary,
                )
            except Exception as exc:
                error_summary = f"{type(exc).__name__}: {exc}"
                await self._update_daily_digest_status(
                    digest.id,
                    **status_fields,
                    last_error=error_summary,
                    last_skipped_reason="",
                )
                logger.exception("daily-digest=%s execution failed", digest.id)
            finally:
                duration_ms = int((time.perf_counter() - started_perf) * 1000)
                self._digest_results[digest.id] = DigestExecutionResult(
                    started_at=started_at,
                    duration_ms=duration_ms,
                    item_count=item_count,
                    pushed_count=pushed_count,
                    error_summary=error_summary,
                )

    def _resolve_digest_target_origins(self, target_ids: list[str]) -> list[str]:
        enabled_targets = {
            target.id: target
            for target in self._targets()
            if target.enabled and target.unified_msg_origin
        }
        return sorted(
            {
                enabled_targets[target_id].unified_msg_origin
                for target_id in target_ids
                if target_id in enabled_targets
            }
        )

    def _build_seen_keys(self, item: dict) -> list[str]:
        build_keys = getattr(self._storage, "build_seen_keys", None)
        if callable(build_keys):
            keys = [str(key).strip() for key in build_keys(item) if str(key).strip()]
            if keys:
                return keys

        item_id = str(self._storage.build_dedup_key(item)).strip()
        return [item_id] if item_id else []

    async def _has_seen_any(self, keys: list[str]) -> bool:
        for key in keys:
            if await self._storage.has_seen(key):
                return True
        return False

    async def _mark_seen_all(self, keys: list[str], ttl_seconds: int) -> None:
        for key in keys:
            await self._storage.mark_seen(key, ttl_seconds=ttl_seconds)

    async def _get_daily_digest_status(self, digest_id: str) -> dict[str, Any]:
        getter = getattr(self._storage, "get_daily_digest_status", None)
        if not callable(getter):
            return {}
        result = await getter(digest_id)
        return dict(result) if isinstance(result, dict) else {}

    async def _update_daily_digest_status(self, digest_id: str, **fields: Any) -> dict[str, Any]:
        updater = getattr(self._storage, "update_daily_digest_status", None)
        if not callable(updater):
            return {}
        result = await updater(digest_id, **fields)
        return dict(result) if isinstance(result, dict) else {}

    async def _list_daily_digest_items(
        self,
        feed_ids: list[str],
        *,
        window_start_ts: int,
        window_end_ts: int,
        limit: int,
    ) -> list[dict[str, Any]]:
        lister = getattr(self._storage, "list_digest_items", None)
        if not callable(lister):
            return []
        result = await lister(
            feed_ids,
            window_start_ts=window_start_ts,
            window_end_ts=window_end_ts,
            limit=limit,
        )
        return list(result or [])

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
            parsed_count = 0
            skipped_seen_count = 0
            skipped_batch_duplicate_count = 0
            skipped_dispatch_duplicate_count = 0
            skipped_invalid_target_count = 0
            dispatch_fail_count = 0
            error_summary = ""
            seen_in_run: set[str] = set()

            try:
                raw_items = await self._call_fetch(job)
                fetched_count = len(raw_items)
                feed_state_map = {
                    feed_id: await self._storage.get_feed_state(feed_id)
                    for feed_id in job.feed_ids
                }

                items = self._call_parse(raw_items, job)
                parsed_count = len(items)
                await self._archive_items(items)
                for item in items:
                    seen_keys = self._build_seen_keys(item)
                    if not seen_keys:
                        skipped_seen_count += 1
                        continue
                    item_id = seen_keys[0]
                    if any(key in seen_in_run for key in seen_keys):
                        skipped_batch_duplicate_count += 1
                        logger.warning(
                            "skip job=%s duplicate item in current batch: id=%s title=%s",
                            job.id,
                            item_id,
                            str(item.get("title", "")).strip(),
                        )
                        continue
                    if await self._has_seen_any(seen_keys):
                        skipped_seen_count += 1
                        continue
                    seen_in_run.update(seen_keys)

                    event_item = dict(item)
                    event_item.setdefault("job_id", job.id)
                    if self._pipeline is not None:
                        event_item = await self._pipeline.process(event_item)
                    dispatch_result = await self._dispatcher.dispatch(event_item)
                    if dispatch_result.success_count <= 0:
                        if (
                            dispatch_result.skipped_duplicate_count > 0
                            and dispatch_result.permanent_failure_count == 0
                            and dispatch_result.transient_failure_count == 0
                        ):
                            await self._mark_seen_all(
                                seen_keys,
                                ttl_seconds=self._config.dedup_ttl_seconds,
                            )
                            skipped_dispatch_duplicate_count += dispatch_result.skipped_duplicate_count
                            continue
                        permanent_or_disabled = (
                            dispatch_result.permanent_failure_count > 0
                            or dispatch_result.skipped_disabled_count > 0
                        )
                        if permanent_or_disabled and dispatch_result.transient_failure_count == 0:
                            await self._mark_seen_all(
                                seen_keys,
                                ttl_seconds=self._config.dedup_ttl_seconds,
                            )
                            skipped_invalid_target_count += 1
                            continue
                        dispatch_fail_count += 1
                        continue
                    await self._mark_seen_all(seen_keys, ttl_seconds=self._config.dedup_ttl_seconds)
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
                        bootstrap_done=True,
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
                "job=%s finished: fetched=%s parsed=%s pushed=%s skipped_seen=%s skipped_batch_duplicate=%s skipped_dispatch_duplicate=%s skipped_invalid_target=%s dispatch_fail=%s duration_ms=%s error=%s",
                job.id,
                fetched_count,
                parsed_count,
                pushed_count,
                skipped_seen_count,
                skipped_batch_duplicate_count,
                skipped_dispatch_duplicate_count,
                skipped_invalid_target_count,
                dispatch_fail_count,
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

    @classmethod
    def _should_mark_history_only(
        cls,
        item: dict,
        feed_state_map: dict[str, dict[str, int | str]],
        *,
        bootstrap_only: bool = True,
    ) -> bool:
        feed_id = str(item.get("feed_id", "")).strip()
        if not feed_id:
            return False

        feed_state = feed_state_map.get(feed_id) or {}
        if bootstrap_only and bool(feed_state.get("bootstrap_done", False)):
            return False

        try:
            last_success_time = int(feed_state.get("last_success_time", 0) or 0)
        except (TypeError, ValueError):
            return False
        if last_success_time <= 0:
            return False

        published_at = cls._parse_item_timestamp(item.get("published_at"))
        if published_at is None:
            return False
        return int(published_at.timestamp()) <= last_success_time

    @staticmethod
    def _parse_item_timestamp(raw_value) -> datetime | None:
        text = str(raw_value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
