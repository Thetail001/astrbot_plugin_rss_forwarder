from astrbot.api import logger


class FeedDispatcher:
    """分发层：负责把新内容推送到目标会话/渠道。"""

    async def dispatch(self, item: dict) -> None:
        # 预留：按需接入 AstrBot 消息发送能力。
        logger.info("dispatch item: %s", item)
