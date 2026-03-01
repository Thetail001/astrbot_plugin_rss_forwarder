from astrbot.api.event import AstrMessageEvent, filter


class RSSCommands:
    """命令入口。"""

    scheduler = None

    @filter.command("rss_status")
    async def rss_status(self, event: AstrMessageEvent):
        running = bool(self.scheduler and self.scheduler.running)
        status_text = "运行中" if running else "未运行"
        yield event.plain_result(f"RSS 调度器状态：{status_text}")
