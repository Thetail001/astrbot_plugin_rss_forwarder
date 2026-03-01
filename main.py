from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from .commands import RSSCommands
from .config import RSSConfig
from .dispatcher import FeedDispatcher
from .fetcher import FeedFetcher
from .parser import FeedParser
from .pipeline import FeedPipeline
from .scheduler import RSSScheduler
from .storage import FeedStorage


@register("astrbot_rss", "AstrBot-RSS", "RSS 订阅抓取与推送插件", "0.2.0")
class RSSPlugin(Star, RSSCommands):
    def __init__(self, context: Context, config=None):
        super().__init__(context, config)

        runtime_source = config if config is not None else context
        config = RSSConfig.from_context(runtime_source)
        parser = FeedParser()
        storage = FeedStorage(
            plugin_name="astrbot_rss",
            get_kv_data=getattr(self, "get_kv_data", None),
            put_kv_data=getattr(self, "put_kv_data", None),
            delete_kv_data=getattr(self, "delete_kv_data", None),
        )
        fetcher = FeedFetcher(config=config, storage=storage)
        dispatcher = FeedDispatcher(context=context, config=config)
        pipeline = FeedPipeline(context=context, config=config)

        self.scheduler = RSSScheduler(
            config=config,
            fetcher=fetcher,
            parser=parser,
            dispatcher=dispatcher,
            storage=storage,
            pipeline=pipeline,
        )

    async def initialize(self):
        """插件初始化：仅做资源编排（启动调度器）。"""
        await self.scheduler.start()

    async def terminate(self):
        """插件销毁：仅做资源编排（关闭任务）。"""
        await self.scheduler.stop()

    # NOTE:
    # 这些装饰器必须挂在主插件模块(main.py)中的类方法上，
    # 否则插件面板不会把它们识别为当前插件行为项。
    @filter.regex(r"^/?rss(?:\s+.*)?$")
    async def _rss_router(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_router(self, event):
            yield result

    @filter.command("rss list")
    async def _rss_list(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_list(self, event):
            yield result

    @filter.command("rss run")
    async def _rss_run(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_run(self, event):
            yield result

    @filter.command("rss reset")
    @filter.regex(r"^/?rss\s+reset\s*$")
    async def _rss_reset(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_reset(self, event):
            yield result

    @filter.command("rss status")
    async def _rss_status(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_status(self, event):
            yield result

    @filter.command("rss pause")
    async def _rss_pause(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_pause(self, event):
            yield result

    @filter.command("rss resume")
    async def _rss_resume(self, event: AstrMessageEvent):
        async for result in RSSCommands.rss_resume(self, event):
            yield result
