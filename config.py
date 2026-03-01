from dataclasses import dataclass, field
from urllib.parse import urlparse


class ConfigValidationError(ValueError):
    """配置校验失败。"""


@dataclass(slots=True)
class FeedConfig:
    id: str
    url: str
    auth_mode: str = "none"
    key: str = ""
    enabled: bool = True
    timeout: int = 10


@dataclass(slots=True)
class TargetConfig:
    id: str
    platform: str
    unified_msg_origin: str = ""
    enabled: bool = True


@dataclass(slots=True)
class JobConfig:
    id: str
    feed_ids: list[str]
    target_ids: list[str]
    cron: str = ""
    interval_seconds: int = 0
    batch_size: int = 10
    enabled: bool = True


@dataclass(slots=True)
class RenderCardTemplateConfig:
    title: str = "{title}"
    source: str = "{source}"
    published_at: str = "{published_at}"
    summary: str = "{summary}"
    link_text: str = "查看全文"


@dataclass(slots=True)
class RSSConfig:
    """插件运行配置。"""

    feeds: list[FeedConfig]
    targets: list[TargetConfig]
    jobs: list[JobConfig]
    llm_enabled: bool = False
    llm_profile: str = "rss_enrich"
    max_input_chars: int = 2000
    timeout: int = 15
    render_mode: str = "text"
    summary_max_chars: int = 280
    render_card_template: RenderCardTemplateConfig = field(default_factory=RenderCardTemplateConfig)

    @property
    def poll_interval_seconds(self) -> int:
        """兼容当前调度器：取启用任务中的最小 interval。"""
        enabled_intervals = [
            job.interval_seconds
            for job in self.jobs
            if job.enabled and job.interval_seconds > 0
        ]
        return min(enabled_intervals, default=300)

    @classmethod
    def from_context(cls, context) -> "RSSConfig":
        """从 AstrBot 上下文中加载配置并进行完整性校验。"""
        runtime_conf = getattr(context, "config", {}) or {}
        feeds = [
            FeedConfig(
                id=str(item.get("id", "")).strip(),
                url=str(item.get("url", "")).strip(),
                auth_mode=str(item.get("auth_mode", "none")).strip() or "none",
                key=str(item.get("key", "")).strip(),
                enabled=bool(item.get("enabled", True)),
                timeout=int(item.get("timeout", 10)),
            )
            for item in runtime_conf.get("feeds", [])
        ]
        targets = [
            TargetConfig(
                id=str(item.get("id", "")).strip(),
                platform=str(item.get("platform", "")).strip(),
                unified_msg_origin=str(item.get("unified_msg_origin", "")).strip(),
                enabled=bool(item.get("enabled", True)),
            )
            for item in runtime_conf.get("targets", [])
        ]
        jobs = [
            JobConfig(
                id=str(item.get("id", "")).strip(),
                feed_ids=[str(fid).strip() for fid in item.get("feed_ids", [])],
                target_ids=[str(tid).strip() for tid in item.get("target_ids", [])],
                cron=str(item.get("cron", "")).strip(),
                interval_seconds=int(item.get("interval_seconds", 0) or 0),
                batch_size=int(item.get("batch_size", 10)),
                enabled=bool(item.get("enabled", True)),
            )
            for item in runtime_conf.get("jobs", [])
        ]
        config = cls(
            feeds=feeds,
            targets=targets,
            jobs=jobs,
            llm_enabled=bool(runtime_conf.get("llm_enabled", False)),
            llm_profile=str(runtime_conf.get("llm_profile", "rss_enrich")).strip() or "rss_enrich",
            max_input_chars=int(runtime_conf.get("max_input_chars", 2000)),
            timeout=int(runtime_conf.get("timeout", 15)),
            render_mode=str(runtime_conf.get("render_mode", "text")).strip() or "text",
            summary_max_chars=int(runtime_conf.get("summary_max_chars", 280)),
            render_card_template=RenderCardTemplateConfig(
                title=str(
                    (runtime_conf.get("render_card_template", {}) or {}).get("title", "{title}")
                ).strip()
                or "{title}",
                source=str(
                    (runtime_conf.get("render_card_template", {}) or {}).get("source", "{source}")
                ).strip()
                or "{source}",
                published_at=str(
                    (runtime_conf.get("render_card_template", {}) or {}).get(
                        "published_at", "{published_at}"
                    )
                ).strip()
                or "{published_at}",
                summary=str(
                    (runtime_conf.get("render_card_template", {}) or {}).get("summary", "{summary}")
                ).strip()
                or "{summary}",
                link_text=str(
                    (runtime_conf.get("render_card_template", {}) or {}).get("link_text", "查看全文")
                ).strip()
                or "查看全文",
            ),
        )
        config.validate()
        return config

    def validate(self) -> None:
        self._validate_unique_ids("feed", [feed.id for feed in self.feeds])
        self._validate_unique_ids("target", [target.id for target in self.targets])
        self._validate_unique_ids("job", [job.id for job in self.jobs])

        feed_ids = {feed.id for feed in self.feeds}
        target_ids = {target.id for target in self.targets}

        for feed in self.feeds:
            if not feed.id:
                raise ConfigValidationError("feeds.id 不能为空")
            self._validate_url(feed.url, f"feeds[{feed.id}].url")
            if feed.auth_mode not in {"none", "query", "header"}:
                raise ConfigValidationError(
                    f"feeds[{feed.id}].auth_mode 非法: {feed.auth_mode}"
                )
            if feed.timeout <= 0:
                raise ConfigValidationError(f"feeds[{feed.id}].timeout 必须 > 0")

        for target in self.targets:
            if not target.id:
                raise ConfigValidationError("targets.id 不能为空")
            if not target.platform:
                raise ConfigValidationError(f"targets[{target.id}].platform 不能为空")
            if not target.unified_msg_origin:
                raise ConfigValidationError(
                    f"targets[{target.id}] 需要 unified_msg_origin 或平台会话标识"
                )


        if self.max_input_chars <= 0:
            raise ConfigValidationError("max_input_chars 必须 > 0")
        if self.timeout <= 0:
            raise ConfigValidationError("timeout 必须 > 0")
        if self.render_mode not in {"text", "image"}:
            raise ConfigValidationError("render_mode 必须是 text 或 image")
        if self.summary_max_chars <= 0:
            raise ConfigValidationError("summary_max_chars 必须 > 0")
        if self.llm_enabled and not self.llm_profile:
            raise ConfigValidationError("llm_enabled=true 时 llm_profile 不能为空")

        for job in self.jobs:
            if not job.id:
                raise ConfigValidationError("jobs.id 不能为空")
            if not job.feed_ids:
                raise ConfigValidationError(f"jobs[{job.id}].feed_ids 不能为空")
            if not job.target_ids:
                raise ConfigValidationError(f"jobs[{job.id}].target_ids 不能为空")
            if not job.cron and job.interval_seconds <= 0:
                raise ConfigValidationError(
                    f"jobs[{job.id}] 必须提供 cron 或 interval_seconds"
                )
            if job.batch_size <= 0:
                raise ConfigValidationError(f"jobs[{job.id}].batch_size 必须 > 0")

            missing_feeds = [fid for fid in job.feed_ids if fid not in feed_ids]
            if missing_feeds:
                raise ConfigValidationError(
                    f"jobs[{job.id}] 引用了不存在的 feed_ids: {missing_feeds}"
                )
            missing_targets = [tid for tid in job.target_ids if tid not in target_ids]
            if missing_targets:
                raise ConfigValidationError(
                    f"jobs[{job.id}] 引用了不存在的 target_ids: {missing_targets}"
                )

    @staticmethod
    def _validate_unique_ids(kind: str, ids: list[str]) -> None:
        seen: set[str] = set()
        duplicated: set[str] = set()
        for item_id in ids:
            if not item_id:
                continue
            if item_id in seen:
                duplicated.add(item_id)
            seen.add(item_id)
        if duplicated:
            dup_text = ", ".join(sorted(duplicated))
            raise ConfigValidationError(f"{kind} id 重复: {dup_text}")

    @staticmethod
    def _validate_url(url: str, field_name: str) -> None:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ConfigValidationError(f"{field_name} 不是合法 URL: {url}")
