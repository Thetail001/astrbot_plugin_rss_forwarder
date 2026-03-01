from typing import Any

from astrbot.api import logger

from config import RSSConfig


class FeedDispatcher:
    """分发层：负责把新内容推送到目标会话/渠道。"""

    def __init__(self, context, config: RSSConfig) -> None:
        self.context = context
        self._config = config
        self._target_map = {
            target.id: target
            for target in config.targets
            if target.enabled and target.unified_msg_origin
        }
        self._job_target_origins = self._build_job_target_map(config)

    def _build_job_target_map(self, config: RSSConfig) -> dict[str, list[str]]:
        mapping: dict[str, list[str]] = {}
        for job in config.jobs:
            if not job.enabled:
                continue
            origins = [
                self._target_map[target_id].unified_msg_origin
                for target_id in job.target_ids
                if target_id in self._target_map
            ]
            if origins:
                mapping[job.id] = origins
        return mapping

    def _resolve_origins(self, item: dict[str, Any]) -> list[str]:
        origins: set[str] = set()

        job_ids = item.get("job_ids") or []
        if isinstance(job_ids, str):
            job_ids = [job_ids]
        for job_id in job_ids:
            origins.update(self._job_target_origins.get(str(job_id), []))

        job_id = str(item.get("job_id", "")).strip()
        if job_id:
            origins.update(self._job_target_origins.get(job_id, []))

        feed_id = str(item.get("feed_id", "")).strip()
        if feed_id:
            for job in self._config.jobs:
                if job.enabled and feed_id in job.feed_ids:
                    origins.update(self._job_target_origins.get(job.id, []))

        if not origins:
            for origin_list in self._job_target_origins.values():
                origins.update(origin_list)
        return sorted(origins)

    @staticmethod
    def _build_plain_text(item: dict[str, Any]) -> str:
        title = str(item.get("title", "")).strip()
        summary = str(item.get("summary", "")).strip()
        link = str(item.get("link", "")).strip()
        lines = [line for line in [title, summary, link] if line]
        return "\n".join(lines) if lines else str(item)

    def _build_message_chain(self, item: dict[str, Any]):
        existing = item.get("message_chain")
        if existing is not None:
            return existing

        text = self._build_plain_text(item)
        link = str(item.get("link", "")).strip()
        image_url = str(item.get("image", "") or item.get("cover", "")).strip()
        components: list[Any] = []

        try:
            from astrbot.api.message_components import Image, MessageChain, Plain

            components.append(Plain(text))
            if link:
                components.append(Plain(f"\n{link}"))
            if image_url:
                components.append(Image.fromURL(image_url))
            return MessageChain(components)
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("build MessageChain failed, fallback to plain text: %s", exc)
            if image_url:
                return f"{text}\n封面: {image_url}"
            return text

    @staticmethod
    def _as_chain_result_if_possible(item: dict[str, Any], message_chain):
        event = item.get("event")
        if event and hasattr(event, "chain_result"):
            try:
                return event.chain_result(message_chain)
            except Exception as exc:
                logger.warning("event.chain_result failed, fallback to message_chain: %s", exc)
        return message_chain

    async def dispatch(self, item: dict) -> None:
        origins = self._resolve_origins(item)
        if not origins:
            logger.warning("skip dispatch: no available targets for item=%s", item)
            return

        message_chain = self._build_message_chain(item)
        payload = self._as_chain_result_if_possible(item, message_chain)

        for unified_msg_origin in origins:
            try:
                await self.context.send_message(unified_msg_origin, payload)
            except Exception as exc:
                logger.error(
                    "主动消息发送失败 origin=%s: %s。若当前平台不支持主动消息，请在支持的会话渠道配置 target。",
                    unified_msg_origin,
                    exc,
                )
