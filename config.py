from dataclasses import dataclass


@dataclass(slots=True)
class RSSConfig:
    """插件运行配置。"""

    poll_interval_seconds: int = 300

    @classmethod
    def from_context(cls, context) -> "RSSConfig":
        """从 AstrBot 上下文中加载配置，若无配置则使用默认值。"""
        runtime_conf = getattr(context, "config", {}) or {}
        interval = runtime_conf.get("poll_interval_seconds", cls.poll_interval_seconds)
        return cls(poll_interval_seconds=int(interval))
