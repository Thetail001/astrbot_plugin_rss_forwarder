from datetime import datetime
from html import escape
from typing import Any

from astrbot.api import logger

from .config import RSSConfig


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

    def _format_time(self, item: dict[str, Any]) -> str:
        raw_time = (
            item.get("published")
            or item.get("published_at")
            or item.get("pub_date")
            or item.get("updated")
            or item.get("time")
            or ""
        )
        time_text = str(raw_time).strip()
        if time_text:
            return time_text
        return datetime.now().strftime("%Y-%m-%d %H:%M")

    def _truncate_summary(self, item: dict[str, Any]) -> tuple[str, bool]:
        summary = str(item.get("summary", "") or item.get("content", "")).strip()
        if not summary:
            return "", False

        max_chars = self._config.summary_max_chars
        if len(summary) <= max_chars:
            return summary, False

        truncated = summary[: max_chars - 1].rstrip()
        return f"{truncated}…", True

    def _build_render_data(self, item: dict[str, Any]) -> dict[str, str]:
        title = str(item.get("title", "")).strip() or "(无标题)"
        source = str(item.get("source", "") or item.get("feed_title", "")).strip() or "未知来源"
        published_at = self._format_time(item)
        summary, truncated = self._truncate_summary(item)
        link = str(item.get("link", "")).strip()

        return {
            "title": title,
            "source": source,
            "published_at": published_at,
            "summary": summary,
            "link": link,
            "truncated": "1" if truncated else "0",
        }

    @staticmethod
    def _safe_format(template: str, values: dict[str, str]) -> str:
        try:
            return template.format(**values)
        except Exception:
            return template

    def _build_text_message_chain(self, item: dict[str, Any]):
        data = self._build_render_data(item)
        template = self._config.render_card_template

        title = self._safe_format(template.title, data)
        source = self._safe_format(template.source, data)
        published_at = self._safe_format(template.published_at, data)
        summary = self._safe_format(template.summary, data)
        link_text = self._safe_format(template.link_text, data)
        link = data["link"]

        try:
            from astrbot.api.message_components import Link, MessageChain, Plain

            text_lines = [
                line
                for line in [
                    title,
                    f"来源：{source}" if source else "",
                    f"时间：{published_at}" if published_at else "",
                    summary,
                ]
                if line
            ]
            components: list[Any] = [Plain("\n".join(text_lines))]

            if link:
                link_label = link_text if data["truncated"] == "1" else link
                components.append(Link(link_label, link))

            return MessageChain(components)
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("build text MessageChain failed, fallback to plain text: %s", exc)
            fallback_lines = [
                title,
                f"来源：{source}",
                f"时间：{published_at}",
                summary,
                f"{link_text}: {link}" if data["truncated"] == "1" and link else link,
            ]
            return "\n".join([line for line in fallback_lines if line])

    def _build_card_html(self, item: dict[str, Any]) -> str:
        data = self._build_render_data(item)
        template = self._config.render_card_template
        title = escape(self._safe_format(template.title, data))
        source = escape(self._safe_format(template.source, data))
        published_at = escape(self._safe_format(template.published_at, data))
        summary = escape(self._safe_format(template.summary, data))
        link = escape(data["link"])
        link_text = escape(self._safe_format(template.link_text, data) or "查看全文")

        footer = ""
        if link:
            footer = f'<a class="link" href="{link}">{link_text}</a>'

        return (
            "<html><head><meta charset='utf-8' /><style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f7fb;padding:16px;}"
            ".card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 12px rgba(30,55,90,.12);max-width:680px;}"
            ".title{font-size:22px;font-weight:700;line-height:1.4;margin-bottom:8px;color:#111827;}"
            ".meta{color:#6b7280;font-size:13px;margin-bottom:12px;}"
            ".summary{color:#1f2937;font-size:15px;line-height:1.7;white-space:pre-wrap;}"
            ".link{display:inline-block;margin-top:12px;color:#2563eb;text-decoration:none;font-weight:600;}"
            "</style></head><body>"
            f"<div class='card'><div class='title'>{title}</div><div class='meta'>来源：{source} · 时间：{published_at}</div>"
            f"<div class='summary'>{summary}</div>{footer}</div></body></html>"
        )

    async def _build_image_payload(self, item: dict[str, Any]):
        html = self._build_card_html(item)

        try:
            image_result = await self.html_render(html)
            return self._as_image_result_if_possible(item, image_result)
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("image render failed, fallback to text mode: %s", exc)
            chain = self._build_text_message_chain(item)
            return self._as_chain_result_if_possible(item, chain)

    async def html_render(self, html: str):
        if hasattr(self.context, "html_render"):
            return await self.context.html_render(html)
        raise RuntimeError("context.html_render is not available")

    @staticmethod
    def _as_chain_result_if_possible(item: dict[str, Any], message_chain):
        event = item.get("event")
        if event and hasattr(event, "chain_result"):
            try:
                return event.chain_result(message_chain)
            except Exception as exc:
                logger.warning("event.chain_result failed, fallback to message_chain: %s", exc)
        return message_chain

    @staticmethod
    def _as_image_result_if_possible(item: dict[str, Any], image_result):
        event = item.get("event")
        if event and hasattr(event, "image_result"):
            try:
                return event.image_result(image_result)
            except Exception as exc:
                logger.warning("event.image_result failed, fallback to image_result: %s", exc)
        return image_result

    async def dispatch(self, item: dict) -> int:
        origins = self._resolve_origins(item)
        if not origins:
            logger.warning("skip dispatch: no available targets for item=%s", item)
            return 0

        if self._config.render_mode == "image":
            payload = await self._build_image_payload(item)
        else:
            chain = self._build_text_message_chain(item)
            payload = self._as_chain_result_if_possible(item, chain)

        success_count = 0
        for unified_msg_origin in origins:
            try:
                await self.context.send_message(unified_msg_origin, payload)
                success_count += 1
            except Exception as exc:
                logger.error(
                    "主动消息发送失败 origin=%s: %s。若当前平台不支持主动消息，请在支持的会话渠道配置 target。",
                    unified_msg_origin,
                    exc,
                )
        return success_count
