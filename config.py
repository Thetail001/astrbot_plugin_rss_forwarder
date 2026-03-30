from dataclasses import dataclass, field
from urllib.parse import urlparse

from astrbot.api import logger


class ConfigValidationError(ValueError):
    """配置校验失败。"""


DEFAULT_DAILY_DIGEST_PROMPT = (
    "请根据以下 RSS 条目生成一份简体中文日报，严格输出纯文本编号列表。\n"
    "要求：\n"
    "1) 只输出编号列表，不要导语、总结、分类标题或 Markdown 代码块；\n"
    "2) 最多输出 {max_items} 条；\n"
    "3) 每条一句话，优先保留来源信息；\n"
    "4) 如果多条内容高度相近，可合并为一条更准确的概述。\n\n"
    "统计窗口：{window_start} 至 {window_end}\n"
    "条目：\n{items}"
)


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
class DailyDigestConfig:
    id: str
    title: str
    feed_ids: list[str]
    target_ids: list[str]
    send_time: str = "09:00"
    window_hours: int = 24
    max_items: int = 20
    render_mode: str = "text"
    prompt_template: str = DEFAULT_DAILY_DIGEST_PROMPT
    enabled: bool = False


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
    daily_digests: list[DailyDigestConfig] = field(default_factory=list)
    timezone: str = "Asia/Shanghai"

    # 翻译增强
    llm_enabled: bool = False
    llm_provider_id: str = ""
    llm_profile: str = "rss_enrich"
    llm_timeout_seconds: int = 15
    max_input_chars: int = 2000
    llm_proxy_mode: str = "system"  # off|system|custom
    llm_proxy_url: str = ""

    github_models_enabled: bool = False
    github_models_model: str = "openai/gpt-4o-mini"
    github_models_timeout_seconds: int = 15
    github_models_token_file: str = "github.token"
    github_models_proxy_mode: str = "system"  # off|system|custom
    github_models_proxy_url: str = ""

    google_translate_enabled: bool = False
    google_translate_api_key: str = ""
    google_translate_target_lang: str = "zh-CN"
    google_translate_timeout_seconds: int = 15
    google_translate_proxy_mode: str = "system"  # off|system|custom
    google_translate_proxy_url: str = ""

    dedup_ttl_seconds: int = 7 * 24 * 60 * 60
    startup_delay_seconds: int = 45
    render_mode: str = "text"
    summary_max_chars: int = 280
    render_card_template: RenderCardTemplateConfig = field(default_factory=RenderCardTemplateConfig)

    @property
    def timeout(self) -> int:
        """兼容旧代码字段名。"""
        return self.llm_timeout_seconds

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
    def from_context(cls, context_or_config) -> "RSSConfig":
        """从 AstrBot 上下文或插件配置对象加载配置并进行完整性校验。"""
        if isinstance(context_or_config, dict):
            runtime_conf = context_or_config
        else:
            runtime_conf = getattr(context_or_config, "config", {}) or {}

        feeds_raw = cls._normalize_collection(runtime_conf.get("feeds", []))
        targets_raw = cls._normalize_collection(runtime_conf.get("targets", []))
        jobs_raw = cls._normalize_collection(runtime_conf.get("jobs", []))
        daily_digests_raw = cls._normalize_collection(runtime_conf.get("daily_digests", []))

        feeds = [
            FeedConfig(
                id=str(item.get("id", "")).strip(),
                url=str(item.get("url", "")).strip(),
                auth_mode=str(item.get("auth_mode", "none")).strip() or "none",
                key=str(item.get("key", "")).strip(),
                enabled=bool(item.get("enabled", True)),
                timeout=int(item.get("timeout", 10)),
            )
            for item in feeds_raw
        ]
        targets = [
            TargetConfig(
                id=str(item.get("id", "")).strip(),
                platform=str(item.get("platform", "")).strip(),
                unified_msg_origin=str(item.get("unified_msg_origin", "")).strip(),
                enabled=bool(item.get("enabled", True)),
            )
            for item in targets_raw
        ]
        jobs = [
            JobConfig(
                id=str(item.get("id", "")).strip(),
                feed_ids=cls._normalize_id_list(item.get("feed_ids", [])),
                target_ids=cls._normalize_id_list(item.get("target_ids", [])),
                cron=str(item.get("cron", "")).strip(),
                interval_seconds=int(item.get("interval_seconds", 0) or 0),
                batch_size=int(item.get("batch_size", 10)),
                enabled=bool(item.get("enabled", True)),
            )
            for item in jobs_raw
        ]
        daily_digests = [
            DailyDigestConfig(
                id=str(item.get("id", "")).strip(),
                title=(
                    str(item.get("title", "")).strip() or str(item.get("id", "")).strip()
                ),
                feed_ids=cls._normalize_id_list(item.get("feed_ids", [])),
                target_ids=cls._normalize_id_list(item.get("target_ids", [])),
                send_time=str(item.get("send_time", "09:00")).strip() or "09:00",
                window_hours=int(item.get("window_hours", 24) or 24),
                max_items=int(item.get("max_items", 20) or 20),
                render_mode=str(item.get("render_mode", "text")).strip() or "text",
                prompt_template=str(
                    item.get("prompt_template", DEFAULT_DAILY_DIGEST_PROMPT)
                ).strip()
                or DEFAULT_DAILY_DIGEST_PROMPT,
                enabled=bool(item.get("enabled", False)),
            )
            for item in daily_digests_raw
        ]

        jobs = cls._build_implicit_job_if_needed(feeds, targets, jobs, daily_digests)

        def conf_value(key: str, default, legacy_keys: list[str] | None = None):
            translation_conf = runtime_conf.get("translation", {})
            if isinstance(translation_conf, dict) and translation_conf.get(key) is not None:
                return translation_conf.get(key)

            if runtime_conf.get(key) is not None:
                return runtime_conf.get(key)

            for old_key in legacy_keys or []:
                if runtime_conf.get(old_key) is not None:
                    return runtime_conf.get(old_key)
            return default

        llm_timeout_seconds = int(conf_value("llm_timeout_seconds", 15, legacy_keys=["timeout"]))

        config = cls(
            feeds=feeds,
            targets=targets,
            jobs=jobs,
            daily_digests=daily_digests,
            timezone=str(runtime_conf.get("timezone", "Asia/Shanghai")).strip() or "Asia/Shanghai",
            llm_enabled=bool(conf_value("llm_enabled", False)),
            llm_provider_id=str(conf_value("llm_provider_id", "")).strip(),
            llm_profile=str(conf_value("llm_profile", "rss_enrich")).strip() or "rss_enrich",
            llm_timeout_seconds=llm_timeout_seconds,
            max_input_chars=int(conf_value("max_input_chars", 2000)),
            llm_proxy_mode=str(
                conf_value("llm_proxy_mode", "system")
            )
            .strip()
            .lower()
            or "system",
            llm_proxy_url=str(conf_value("llm_proxy_url", "")).strip(),
            github_models_enabled=bool(conf_value("github_models_enabled", False)),
            github_models_model=str(
                conf_value("github_models_model", "openai/gpt-4o-mini")
            ).strip()
            or "openai/gpt-4o-mini",
            github_models_timeout_seconds=int(
                conf_value("github_models_timeout_seconds", llm_timeout_seconds)
            ),
            github_models_token_file=str(
                conf_value("github_models_token_file", "github.token")
            ).strip()
            or "github.token",
            github_models_proxy_mode=str(
                conf_value("github_models_proxy_mode", "system")
            )
            .strip()
            .lower()
            or "system",
            github_models_proxy_url=str(conf_value("github_models_proxy_url", "")).strip(),
            google_translate_enabled=bool(
                conf_value("google_translate_enabled", False, legacy_keys=["google_enabled"])
            ),
            google_translate_api_key=str(
                conf_value("google_translate_api_key", "", legacy_keys=["google_api_key"])
            ).strip(),
            google_translate_target_lang=str(
                conf_value("google_translate_target_lang", "zh-CN", legacy_keys=["google_target_lang"])
            ).strip()
            or "zh-CN",
            google_translate_timeout_seconds=int(
                conf_value(
                    "google_translate_timeout_seconds",
                    llm_timeout_seconds,
                    legacy_keys=["google_timeout_seconds"],
                )
            ),
            google_translate_proxy_mode=str(
                conf_value("google_translate_proxy_mode", "system", legacy_keys=["google_proxy_mode"])
            )
            .strip()
            .lower()
            or "system",
            google_translate_proxy_url=str(
                conf_value("google_translate_proxy_url", "", legacy_keys=["google_proxy_url"])
            ).strip(),
            dedup_ttl_seconds=int(runtime_conf.get("dedup_ttl_seconds", 7 * 24 * 60 * 60)),
            startup_delay_seconds=int(runtime_conf.get("startup_delay_seconds", 45)),
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

    @staticmethod
    def _normalize_collection(value) -> list[dict]:
        """兼容 AstrBot 配置面板与手工 JSON 的多种写法，统一为 list[dict]。"""
        if isinstance(value, list):
            return [item for item in value if isinstance(item, dict)]
        if isinstance(value, dict):
            # 兼容错误配置为对象映射的情况：{"id1": {...}, "id2": {...}}
            return [item for item in value.values() if isinstance(item, dict)]
        return []

    @staticmethod
    def _normalize_id_list(value) -> list[str]:
        """兼容 list 或逗号/换行分隔字符串。"""
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        if isinstance(value, str):
            text = value.replace("\n", ",")
            return [part.strip() for part in text.split(",") if part.strip()]
        return []

    @staticmethod
    def _build_implicit_job_if_needed(
        feeds: list[FeedConfig],
        targets: list[TargetConfig],
        jobs: list[JobConfig],
        daily_digests: list[DailyDigestConfig],
    ) -> list[JobConfig]:
        """当仅配置了 feeds/targets 而未配置 jobs 时，自动生成一个默认任务。"""
        if jobs:
            return jobs
        if daily_digests:
            return jobs

        enabled_feeds = [feed.id for feed in feeds if feed.enabled and feed.id]
        enabled_targets = [target.id for target in targets if target.enabled and target.id]
        if not enabled_feeds or not enabled_targets:
            return jobs

        return [
            JobConfig(
                id="default",
                feed_ids=enabled_feeds,
                target_ids=enabled_targets,
                interval_seconds=300,
                batch_size=10,
                enabled=True,
            )
        ]

    def validate(self) -> None:
        self._validate_unique_ids("feed", [feed.id for feed in self.feeds])
        self._validate_unique_ids("target", [target.id for target in self.targets])
        self._validate_unique_ids("job", [job.id for job in self.jobs])
        self._validate_unique_ids("daily_digest", [digest.id for digest in self.daily_digests])

        feed_ids = {feed.id for feed in self.feeds}
        target_ids = {target.id for target in self.targets}

        for feed in self.feeds:
            if not feed.enabled:
                continue
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
            if not target.enabled:
                continue
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
        if self.llm_timeout_seconds <= 0:
            raise ConfigValidationError("llm_timeout_seconds 必须 > 0")
        if self.github_models_timeout_seconds <= 0:
            raise ConfigValidationError("github_models_timeout_seconds 必须 > 0")
        if self.google_translate_timeout_seconds <= 0:
            raise ConfigValidationError("google_translate_timeout_seconds 必须 > 0")
        if self.llm_proxy_mode not in {"off", "system", "custom"}:
            raise ConfigValidationError("llm_proxy_mode 必须是 off/system/custom")
        if self.github_models_proxy_mode not in {"off", "system", "custom"}:
            raise ConfigValidationError("github_models_proxy_mode 必须是 off/system/custom")
        if self.google_translate_proxy_mode not in {"off", "system", "custom"}:
            raise ConfigValidationError("google_translate_proxy_mode 必须是 off/system/custom")

        if self.dedup_ttl_seconds <= 0:
            raise ConfigValidationError("dedup_ttl_seconds 必须 > 0")
        if self.startup_delay_seconds < 0:
            raise ConfigValidationError("startup_delay_seconds 必须 >= 0")
        if self.render_mode not in {"text", "image"}:
            raise ConfigValidationError("render_mode 必须是 text 或 image")
        if self.summary_max_chars <= 0:
            raise ConfigValidationError("summary_max_chars 必须 > 0")
        if self.llm_enabled and not self.llm_profile:
            raise ConfigValidationError("llm_enabled=true 时 llm_profile 不能为空")
        if not self.timezone:
            raise ConfigValidationError("timezone 不能为空")

        if self.github_models_enabled and not self.github_models_model:
            raise ConfigValidationError("github_models_enabled=true 时 github_models_model 不能为空")

        if self.google_translate_enabled and not self.google_translate_api_key:
            logger.warning(
                "google_translate_enabled=true 但 google_translate_api_key 为空，Google 翻译将不可用"
            )

        if self.llm_proxy_mode == "custom" and not self.llm_proxy_url:
            logger.warning(
                "llm_proxy_mode=custom 但未配置 llm_proxy_url，将回退为默认 provider 网络配置"
            )

        if self.github_models_proxy_mode == "custom" and not self.github_models_proxy_url:
            logger.warning(
                "github_models_proxy_mode=custom 但未配置 github_models_proxy_url，将回退为直连"
            )

        if self.google_translate_proxy_mode == "custom" and not self.google_translate_proxy_url:
            logger.warning(
                "google_translate_proxy_mode=custom 但未配置 google_translate_proxy_url，将回退为直连"
            )

        for job in self.jobs:
            if not job.enabled:
                continue
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

        for digest in self.daily_digests:
            if not digest.enabled:
                continue
            if not digest.id:
                raise ConfigValidationError("daily_digests.id 不能为空")
            if not digest.title:
                raise ConfigValidationError(f"daily_digests[{digest.id}].title 不能为空")
            if not digest.feed_ids:
                raise ConfigValidationError(f"daily_digests[{digest.id}].feed_ids 不能为空")
            if not digest.target_ids:
                raise ConfigValidationError(f"daily_digests[{digest.id}].target_ids 不能为空")
            self._validate_send_time(digest.send_time, f"daily_digests[{digest.id}].send_time")
            if digest.window_hours <= 0:
                raise ConfigValidationError(f"daily_digests[{digest.id}].window_hours 必须 > 0")
            if digest.max_items <= 0:
                raise ConfigValidationError(f"daily_digests[{digest.id}].max_items 必须 > 0")
            if digest.render_mode not in {"text", "image"}:
                raise ConfigValidationError(
                    f"daily_digests[{digest.id}].render_mode 必须是 text 或 image"
                )
            if not digest.prompt_template:
                raise ConfigValidationError(
                    f"daily_digests[{digest.id}].prompt_template 不能为空"
                )

            missing_feeds = [fid for fid in digest.feed_ids if fid not in feed_ids]
            if missing_feeds:
                raise ConfigValidationError(
                    f"daily_digests[{digest.id}] 引用了不存在的 feed_ids: {missing_feeds}"
                )
            missing_targets = [tid for tid in digest.target_ids if tid not in target_ids]
            if missing_targets:
                raise ConfigValidationError(
                    f"daily_digests[{digest.id}] 引用了不存在的 target_ids: {missing_targets}"
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

    @staticmethod
    def _validate_send_time(value: str, field_name: str) -> None:
        text = str(value or "").strip()
        parts = text.split(":")
        if len(parts) != 2 or not all(part.isdigit() for part in parts):
            raise ConfigValidationError(f"{field_name} 必须是 HH:MM 格式")
        hour = int(parts[0])
        minute = int(parts[1])
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            raise ConfigValidationError(f"{field_name} 必须是合法的 HH:MM 时间")
