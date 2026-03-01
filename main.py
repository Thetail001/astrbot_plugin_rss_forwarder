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
    def __init__(self, context: Context):
        super().__init__(context)

        config = RSSConfig.from_context(context)
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
