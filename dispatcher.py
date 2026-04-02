import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from html import escape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit, urlunsplit
from urllib.request import Request, urlopen

from astrbot.api import logger

from .config import RSSConfig


@dataclass(slots=True)
class DispatchResult:
    success_count: int = 0
    permanent_failure_count: int = 0
    transient_failure_count: int = 0
    skipped_disabled_count: int = 0
    skipped_duplicate_count: int = 0


class FeedDispatcher:
    """分发层：负责把新内容推送到目标会话/渠道。"""

    _PENDING_DISPATCH_TTL_SECONDS = 120
    _IMAGE_HASH_TIMEOUT_SECONDS = 8
    _IMAGE_HASH_MAX_BYTES = 8 * 1024 * 1024

    def __init__(self, context, config: RSSConfig, storage=None) -> None:
        self.context = context
        self._config = config
        self._storage = storage
        self._target_map = {
            target.id: target
            for target in config.targets
            if target.enabled and target.unified_msg_origin
        }
        self._job_target_origins = self._build_job_target_map(config)
        self._disabled_origins: set[str] = set()

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

        # Note: 不再使用 fallback 推送到所有 target
        # 如果找不到匹配的 origins，返回空列表（不推送）
        # 这避免了 feed 内容被错误地推送到不相关的 target
        return sorted(origins)

    def _resolve_target_origins(self, target_ids: list[str]) -> list[str]:
        origins = {
            self._target_map[target_id].unified_msg_origin
            for target_id in target_ids
            if target_id in self._target_map
        }
        return sorted(origin for origin in origins if origin)

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
        category = str(item.get("category", "")).strip()
        author = str(item.get("author", "")).strip()

        return {
            "title": title,
            "source": source,
            "published_at": published_at,
            "summary": summary,
            "link": link,
            "truncated": "1" if truncated else "0",
            "category": category,
            "author": author,
        }

    @staticmethod
    def _normalize_text(value: Any) -> str:
        text = str(value or "").strip()
        return " ".join(text.split())

    @staticmethod
    def _normalize_url(url: str) -> str:
        text = str(url or "").strip()
        if not text:
            return ""
        parsed = urlsplit(text)
        if not any((parsed.scheme, parsed.netloc, parsed.path, parsed.query, parsed.fragment)):
            return text
        return urlunsplit(
            (
                parsed.scheme.lower(),
                parsed.netloc.lower(),
                parsed.path,
                parsed.query,
                "",
            )
        )

    async def _build_dispatch_fingerprint(self, item: dict[str, Any], origin: str) -> str:
        source_title = item.get("_source_title", "")
        source_summary = item.get("_source_summary", "")
        title = self._normalize_text(source_title or item.get("title", ""))
        summary = self._normalize_text(source_summary or item.get("summary", "") or item.get("content", ""))
        payload = {
            "origin": str(origin or "").strip(),
            "guid": self._normalize_text(item.get("guid", "") or item.get("id", "")),
            "link": self._normalize_url(str(item.get("link", "") or "")),
            "title": title,
            "published_at": self._normalize_text(item.get("published_at", "") or item.get("published", "")),
            "summary_sha256": (
                hashlib.sha256(summary.encode("utf-8")).hexdigest() if summary else ""
            ),
            "render_mode": str(self._config.render_mode or "text").strip(),
        }
        image_url = str(item.get("image_url", "") or "").strip()
        if image_url:
            image_digest = await self._hash_image_bytes(image_url)
            if image_digest:
                payload["image_sha256"] = image_digest
            else:
                payload["image_url"] = self._normalize_url(image_url)
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _build_daily_digest_fingerprint(self, digest: dict[str, Any], origin: str) -> str:
        content = self._normalize_text(digest.get("content", ""))
        payload = {
            "origin": str(origin or "").strip(),
            "digest_id": self._normalize_text(digest.get("id", "")),
            "title": self._normalize_text(digest.get("title", "")),
            "window_start": self._normalize_text(digest.get("window_start_text", "")),
            "window_end": self._normalize_text(digest.get("window_end_text", "")),
            "item_count": int(digest.get("item_count", 0) or 0),
            "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest() if content else "",
            "render_mode": str(digest.get("render_mode", "text") or "text").strip(),
        }
        encoded = json.dumps(payload, sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    async def _hash_image_bytes(self, image_url: str) -> str:
        normalized = self._normalize_url(image_url)
        if not normalized:
            return ""

        try:
            return await asyncio.to_thread(self._hash_image_bytes_sync, normalized)
        except (HTTPError, URLError, OSError, ValueError):
            return ""

    def _hash_image_bytes_sync(self, image_url: str) -> str:
        request = Request(
            image_url,
            headers={"User-Agent": "AstrBotRSSForwarder/0.4.1"},
        )
        digest = hashlib.sha256()
        total = 0
        with urlopen(request, timeout=self._IMAGE_HASH_TIMEOUT_SECONDS) as response:
            while True:
                chunk = response.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > self._IMAGE_HASH_MAX_BYTES:
                    raise ValueError("image_too_large_for_hash")
                digest.update(chunk)
        if total <= 0:
            return ""
        return digest.hexdigest()

    async def _claim_dispatch(self, fingerprint: str) -> bool:
        claim = getattr(self._storage, "claim_dispatch", None)
        if not callable(claim):
            return True
        try:
            return bool(
                await claim(
                    fingerprint,
                    ttl_seconds=self._PENDING_DISPATCH_TTL_SECONDS,
                )
            )
        except Exception as exc:
            logger.warning("claim dispatch fingerprint failed: %s", exc)
            return True

    async def _confirm_dispatch(self, fingerprint: str) -> None:
        confirm = getattr(self._storage, "confirm_dispatch", None)
        if not callable(confirm):
            return
        try:
            await confirm(
                fingerprint,
                ttl_seconds=max(int(getattr(self._config, "dedup_ttl_seconds", 0) or 0), 1),
            )
        except Exception as exc:
            logger.warning("confirm dispatch fingerprint failed: %s", exc)

    async def _release_dispatch(self, fingerprint: str) -> None:
        release = getattr(self._storage, "release_dispatch", None)
        if not callable(release):
            return
        try:
            await release(fingerprint)
        except Exception as exc:
            logger.warning("release dispatch fingerprint failed: %s", exc)

    @staticmethod
    def _safe_format(template: str, values: dict[str, str]) -> str:
        try:
            return template.format(**values)
        except Exception:
            return template

    @staticmethod
    def _resolve_messagechain_cls():
        """优先使用 core MessageChain，避免 API re-export 差异。"""
        try:
            from astrbot.core.message.message_event_result import MessageChain

            return MessageChain
        except Exception:
            from astrbot.api.message_components import MessageChain

            return MessageChain

    @staticmethod
    def _resolve_plain_cls():
        try:
            from astrbot.api.message_components import Plain

            return Plain
        except Exception:
            from astrbot.core.message.message_components import Plain

            return Plain

    @staticmethod
    def _resolve_image_cls():
        try:
            from astrbot.api.message_components import Image

            return Image
        except Exception:
            from astrbot.core.message.components import Image

            return Image

    def _create_message_chain(
        self,
        text_lines: list[str],
        link_line: str | None = None,
        image_url: str | None = None,
    ):
        MessageChain = self._resolve_messagechain_cls()
        Plain = self._resolve_plain_cls()

        lines = [line for line in text_lines if line]
        if link_line:
            lines.append(link_line)
        plain_text = "\n".join(lines)

        components: list[Any] = [Plain(plain_text)]

        if image_url:
            try:
                Image = self._resolve_image_cls()
                components.append(Image.fromURL(image_url))
            except Exception as exc:
                logger.warning("build image component failed, keep text only: %s", exc)

        try:
            return MessageChain(chain=components)
        except TypeError:
            chain = MessageChain()
            if hasattr(chain, "message"):
                return chain.message(plain_text)
            if hasattr(chain, "chain"):
                chain.chain = components
                return chain
            raise

    def _build_text_message_chain(self, item: dict[str, Any]):
        """构建文本模式的消息链，图片无效时降级为纯文本。"""
        data = self._build_render_data(item)
        template = self._config.render_card_template

        title = self._safe_format(template.title, data)
        source = self._safe_format(template.source, data)
        published_at = self._safe_format(template.published_at, data)
        summary = self._safe_format(template.summary, data)
        link_text = self._safe_format(template.link_text, data)
        link = data["link"]
        image_url = str(item.get("image_url", "") or "").strip()

        text_lines = [
            line
            for line in [
                title,
                f"来源：{source}" if source else "",
                f"分类：{data['category']}" if data["category"] else "",
                f"作者：{data['author']}" if data["author"] else "",
                f"时间：{published_at}" if published_at else "",
                summary,
            ]
            if line
        ]

        link_line = ""
        if link:
            link_line = f"{link_text}: {link}" if data["truncated"] == "1" else link

        try:
            # 尝试发送图文合并消息
            return self._create_message_chain(text_lines, link_line or None, image_url or None)
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("build text message chain with image failed, fallback to text only: %s", exc)
            # 降级为纯文本（不含图片）
            try:
                if link and not link_line:
                    link_line = f"{link_text}: {link}"
                return self._create_message_chain(
                    text_lines + ([link_line] if link_line else []), None, None
                )
            except Exception as inner_exc:
                logger.error("build text only chain also failed: %s", inner_exc)
                # 最简保底
                return self._create_message_chain([title or "RSS 推送"], None, None)

    def _build_text_only_chain(self, item: dict[str, Any]):
        """最简降级：只发送文本，不包含图片。用于图片渲染完全失败时。"""
        data = self._build_render_data(item)
        template = self._config.render_card_template

        title = self._safe_format(template.title, data)
        source = self._safe_format(template.source, data)
        published_at = self._safe_format(template.published_at, data)
        summary = self._safe_format(template.summary, data)
        link_text = self._safe_format(template.link_text, data)
        link = data["link"]

        text_lines = [
            line
            for line in [
                title,
                f"来源：{source}" if source else "",
                f"分类：{data['category']}" if data["category"] else "",
                f"作者：{data['author']}" if data["author"] else "",
                f"时间：{published_at}" if published_at else "",
                summary,
                f"{link_text}: {link}" if link else "",
            ]
            if line
        ]

        return self._create_message_chain(text_lines, None, None)

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

        meta_parts = [f"来源：{source}"]
        if data["category"]:
            meta_parts.append(f"分类：{data['category']}")
        if data["author"]:
            meta_parts.append(f"作者：{data['author']}")
        meta_parts.append(f"时间：{published_at}")
        meta_text = " · ".join(meta_parts)

        return (
            "<html><head><meta charset='utf-8' /><style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#f5f7fb;padding:16px;}"
            ".card{background:#fff;border-radius:12px;padding:16px;box-shadow:0 2px 12px rgba(30,55,90,.12);max-width:680px;}"
            ".title{font-size:22px;font-weight:700;line-height:1.4;margin-bottom:8px;color:#111827;}"
            ".meta{color:#6b7280;font-size:13px;margin-bottom:12px;}"
            ".summary{color:#1f2937;font-size:15px;line-height:1.7;white-space:pre-wrap;}"
            ".link{display:inline-block;margin-top:12px;color:#2563eb;text-decoration:none;font-weight:600;}"
            "</style></head><body>"
            f"<div class='card'><div class='title'>{title}</div><div class='meta'>{meta_text}</div>"
            f"<div class='summary'>{summary}</div>{footer}</div></body></html>"
        )

    async def _build_image_payload(self, item: dict[str, Any]) -> tuple[Any, bool]:
        """构建图片模式的消息载荷。

        Returns:
            tuple: (payload, is_fallback_to_text)
            - payload: 渲染后的消息对象
            - is_fallback_to_text: 是否已降级为文本模式（此时原图已包含在 payload 中）
        """
        html = self._build_card_html(item)

        try:
            image_result = await self.html_render(html)
            # 卡片渲染成功时，主 payload 是卡片图片
            return self._as_image_result_if_possible(item, image_result), False
        except Exception as exc:  # pragma: no cover - 依赖运行环境
            logger.warning("image render failed, fallback to text mode: %s", exc)
            # 降级到文本模式，图片会随文本一起发送
            try:
                chain = self._build_text_message_chain(item)
                return self._as_chain_result_if_possible(item, chain), True
            except Exception as inner_exc:
                logger.error("text fallback also failed: %s", inner_exc)
                # 最简降级：只发送文本，不包含图片
                chain = self._build_text_only_chain(item)
                return self._as_chain_result_if_possible(item, chain), True

    def _build_daily_digest_text_chain(self, digest: dict[str, Any]):
        title = str(digest.get("title", "")).strip() or "RSS 日报"
        window_start = str(digest.get("window_start_text", "")).strip()
        window_end = str(digest.get("window_end_text", "")).strip()
        item_count = int(digest.get("item_count", 0) or 0)
        content = str(digest.get("content", "")).strip()
        links = list(digest.get("links", []) or [])

        lines = [title]
        if window_start and window_end:
            lines.append(f"统计区间：{window_start} - {window_end}")
        lines.append(f"条目数：{item_count}")
        if content:
            lines.extend(["", content])
        if links:
            lines.append("")
            lines.append("链接：")
            for index, item in enumerate(links, start=1):
                link = str((item or {}).get("link", "")).strip()
                if not link:
                    continue
                source = str((item or {}).get("source", "")).strip()
                label = f"{index}. [{source}] {link}" if source else f"{index}. {link}"
                lines.append(label)
        return self._create_message_chain(lines)

    def _build_daily_digest_card_html(self, digest: dict[str, Any]) -> str:
        title = escape(str(digest.get("title", "")).strip() or "RSS 日报")
        window_start = escape(str(digest.get("window_start_text", "")).strip())
        window_end = escape(str(digest.get("window_end_text", "")).strip())
        item_count = int(digest.get("item_count", 0) or 0)
        content = escape(str(digest.get("content", "")).strip()).replace("\n", "<br/>")
        window_text = f"{window_start} - {window_end}" if window_start and window_end else ""

        return (
            "<html><head><meta charset='utf-8' /><style>"
            "body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;background:#eef3f9;padding:18px;}"
            ".card{background:#fff;border-radius:14px;padding:18px;box-shadow:0 2px 16px rgba(30,55,90,.12);max-width:760px;}"
            ".title{font-size:24px;font-weight:700;line-height:1.4;margin-bottom:8px;color:#111827;}"
            ".meta{color:#6b7280;font-size:13px;margin-bottom:16px;}"
            ".content{color:#1f2937;font-size:15px;line-height:1.8;white-space:pre-wrap;}"
            "</style></head><body>"
            f"<div class='card'><div class='title'>{title}</div>"
            f"<div class='meta'>统计区间：{window_text} · 条目数：{item_count}</div>"
            f"<div class='content'>{content}</div></div></body></html>"
        )

    async def _build_daily_digest_image_payload(self, digest: dict[str, Any]):
        html = self._build_daily_digest_card_html(digest)
        return await self.html_render(html)

    def _build_image_only_chain(self, image_url: str) -> Any | None:
        """构建仅包含图片的消息链，失败时返回 None 而不是抛出异常。"""
        if not image_url:
            return None

        MessageChain = self._resolve_messagechain_cls()
        Image = self._resolve_image_cls()

        try:
            return MessageChain(chain=[Image.fromURL(image_url)])
        except Exception as exc:
            logger.warning("build image only chain failed for url=%s: %s", image_url, exc)
            return None

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

    @staticmethod
    def _is_rich_media_error(exc: Exception) -> bool:
        """检测是否为富媒体传输失败错误（QQ图片发送失败）。"""
        text = str(exc or "").lower()
        return any(marker in text for marker in ["rich media transfer failed", "ntevent", "sendmsg"])

    async def _try_send_with_fallback(
        self,
        unified_msg_origin: str,
        payload: Any,
        item: dict,
        fingerprint: str,
        result: DispatchResult,
    ) -> bool:
        """尝试发送消息，富媒体失败时自动降级为纯文本重试。

        Returns:
            bool: 是否发送成功
        """
        try:
            await self.context.send_message(unified_msg_origin, payload)
            return True
        except Exception as exc:
            if not self._is_rich_media_error(exc):
                # 不是富媒体错误，直接抛出让上层处理
                raise

            logger.warning(
                "rich media send failed for origin=%s, retrying with text only: %s",
                unified_msg_origin,
                exc,
            )

            # 构建纯文本降级消息
            try:
                text_chain = self._build_text_only_chain(item)
                text_payload = self._as_chain_result_if_possible(item, text_chain)
                await self.context.send_message(unified_msg_origin, text_payload)
                logger.info(
                    "text-only fallback succeeded for origin=%s item=%s",
                    unified_msg_origin,
                    str(item.get("guid", "") or item.get("title", "")).strip()[:50],
                )
                # 标记为降级成功
                result.transient_failure_count += 1  # 记录一次失败但最终成功
                return True
            except Exception as fallback_exc:
                logger.error(
                    "text-only fallback also failed for origin=%s: %s",
                    unified_msg_origin,
                    fallback_exc,
                )
                raise

    async def dispatch(self, item: dict) -> DispatchResult:
        origins = self._resolve_origins(item)
        if not origins:
            logger.warning("skip dispatch: no available targets for item=%s", item)
            return DispatchResult()

        extra_image_payload = None
        if self._config.render_mode == "image":
            payload, source_image_already_included = await self._build_image_payload(item)
            image_url = str(item.get("image_url", "") or "").strip()
            if image_url and not source_image_already_included:
                image_chain = self._build_image_only_chain(image_url)
                if image_chain is not None:
                    extra_image_payload = self._as_chain_result_if_possible(item, image_chain)
                else:
                    logger.info("skip invalid image_url in image mode: %s", image_url)
        else:
            try:
                chain = self._build_text_message_chain(item)
            except Exception:
                return DispatchResult(transient_failure_count=1)
            payload = self._as_chain_result_if_possible(item, chain)

        result = DispatchResult()
        for unified_msg_origin in origins:
            if unified_msg_origin in self._disabled_origins:
                result.skipped_disabled_count += 1
                continue
            fingerprint = await self._build_dispatch_fingerprint(item, unified_msg_origin)
            if not await self._claim_dispatch(fingerprint):
                result.skipped_duplicate_count += 1
                logger.warning(
                    "skip duplicate dispatch origin=%s item=%s fingerprint=%s",
                    unified_msg_origin,
                    str(item.get("guid", "") or item.get("title", "")).strip(),
                    fingerprint[:12],
                )
                continue
            try:
                # 使用带降级的发送方法
                sent_ok = await self._try_send_with_fallback(
                    unified_msg_origin, payload, item, fingerprint, result
                )
                if sent_ok:
                    result.success_count += 1
                    await self._confirm_dispatch(fingerprint)

                # 额外的原图发送（如果主消息成功）
                if sent_ok and extra_image_payload is not None:
                    try:
                        await self.context.send_message(unified_msg_origin, extra_image_payload)
                    except Exception as exc:
                        logger.warning(
                            "extra source image send failed origin=%s: %s",
                            unified_msg_origin,
                            exc,
                        )
            except Exception as exc:
                await self._release_dispatch(fingerprint)
                if self._is_permanent_target_error(exc):
                    self._disabled_origins.add(unified_msg_origin)
                    result.permanent_failure_count += 1
                    logger.error(
                        "主动消息发送失败 origin=%s: %s。已将该 target 标记为无效，本次运行内不再重试。",
                        unified_msg_origin,
                        exc or "unknown error",
                    )
                    continue
                result.transient_failure_count += 1
                logger.error(
                    "主动消息发送失败 origin=%s: %s。若当前平台不支持主动消息，请在支持的会话渠道配置 target。",
                    unified_msg_origin,
                    exc,
                )
        return result

    async def dispatch_daily_digest(self, digest: dict[str, Any]) -> DispatchResult:
        target_ids = list(digest.get("target_ids", []) or [])
        origins = self._resolve_target_origins(target_ids)
        if not origins:
            logger.warning("skip daily digest dispatch: no available targets for digest=%s", digest)
            return DispatchResult()

        render_mode = str(digest.get("render_mode", "text") or "text").strip().lower()
        try:
            if render_mode == "image":
                payload = self._as_image_result_if_possible(
                    digest,
                    await self._build_daily_digest_image_payload(digest),
                )
            else:
                chain = self._build_daily_digest_text_chain(digest)
                payload = self._as_chain_result_if_possible(digest, chain)
        except Exception as exc:
            logger.error("build daily digest payload failed id=%s: %s", digest.get("id", ""), exc)
            return DispatchResult(transient_failure_count=1)

        result = DispatchResult()
        for unified_msg_origin in origins:
            if unified_msg_origin in self._disabled_origins:
                result.skipped_disabled_count += 1
                continue
            fingerprint = await self._build_daily_digest_fingerprint(digest, unified_msg_origin)
            if not await self._claim_dispatch(fingerprint):
                result.skipped_duplicate_count += 1
                logger.warning(
                    "skip duplicate daily digest origin=%s digest=%s fingerprint=%s",
                    unified_msg_origin,
                    str(digest.get("id", "")).strip(),
                    fingerprint[:12],
                )
                continue
            try:
                await self.context.send_message(unified_msg_origin, payload)
                result.success_count += 1
                await self._confirm_dispatch(fingerprint)
            except Exception as exc:
                await self._release_dispatch(fingerprint)
                if self._is_permanent_target_error(exc):
                    self._disabled_origins.add(unified_msg_origin)
                    result.permanent_failure_count += 1
                    logger.error(
                        "日报发送失败 origin=%s: %s。已将该 target 标记为无效，本次运行内不再重试。",
                        unified_msg_origin,
                        exc or "unknown error",
                    )
                    continue
                result.transient_failure_count += 1
                logger.error("日报发送失败 origin=%s: %s", unified_msg_origin, exc)
        return result

    @staticmethod
    def _is_permanent_target_error(exc: Exception) -> bool:
        text = str(exc or "").strip().lower()
        if not text:
            return True
        permanent_markers = (
            "not support",
            "unsupported",
            "invalid",
            "not found",
            "no such",
            "无效",
            "不支持",
            "不存在",
            "找不到",
        )
        return any(marker in text for marker in permanent_markers)
