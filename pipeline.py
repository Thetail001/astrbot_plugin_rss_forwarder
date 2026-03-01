import asyncio
from typing import Any

from astrbot.api import logger

from .config import RSSConfig


class FeedPipeline:
    """处理层：负责在分发前对条目进行可选增强。"""

    def __init__(self, context, config: RSSConfig) -> None:
        self.context = context
        self._config = config

    async def process(self, entry: dict[str, Any]) -> dict[str, Any]:
        """执行分发前处理，失败时始终回退到原始条目。"""
        if not self._config.llm_enabled:
            return entry

        try:
            return await self.enrich_with_llm(entry, self._config.llm_profile)
        except Exception as exc:
            logger.warning("pipeline llm enrich failed, fallback to raw entry: %s", exc)
            return entry

    async def enrich_with_llm(
        self,
        entry: dict[str, Any],
        profile: str,
    ) -> dict[str, Any]:
        """LLM 增强钩子：按 profile 生成摘要/翻译结果并写回 summary。"""
        input_text = self._build_llm_input(entry)
        if not input_text:
            return entry

        provider_id = await self._resolve_provider_id(entry)
        if not provider_id:
            return entry

        prompt = self._build_prompt(input_text)
        llm_call = self.context.llm_generate(
            chat_provider_id=provider_id,
            prompt=prompt,
            profile=profile,
        )
        result = await asyncio.wait_for(llm_call, timeout=self._config.timeout)

        generated_text = self._extract_generated_text(result)
        if not generated_text:
            return entry

        enriched = dict(entry)
        enriched["summary"] = generated_text
        return enriched

    def _build_llm_input(self, entry: dict[str, Any]) -> str:
        title = str(entry.get("title", "")).strip()
        summary = str(entry.get("summary", "")).strip()
        content = str(entry.get("content", "")).strip()
        parts = [part for part in [title, summary, content] if part]
        merged = "\n\n".join(parts)
        if not merged:
            return ""
        return merged[: self._config.max_input_chars]

    async def _resolve_provider_id(self, entry: dict[str, Any]) -> str:
        origin = str(entry.get("unified_msg_origin", "")).strip()
        event = entry.get("event")
        if not origin and event is not None:
            origin = str(getattr(event, "unified_msg_origin", "")).strip()

        if not origin:
            return ""

        try:
            provider_id = await self.context.get_current_chat_provider_id(umo=origin)
        except Exception as exc:
            logger.warning("get_current_chat_provider_id failed: %s", exc)
            return ""
        return str(provider_id or "").strip()

    @staticmethod
    def _build_prompt(input_text: str) -> str:
        return (
            "请对以下 RSS 内容执行处理：\n"
            "1. 输出一段中文摘要；\n"
            "2. 若原文不是中文，额外给出中文翻译；\n"
            "3. 总长度不超过 180 字。\n\n"
            f"内容：\n{input_text}"
        )

    @staticmethod
    def _extract_generated_text(result: Any) -> str:
        if result is None:
            return ""
        if isinstance(result, str):
            return result.strip()
        completion_text = getattr(result, "completion_text", None)
        if isinstance(completion_text, str) and completion_text.strip():
            return completion_text.strip()
        if isinstance(result, dict):
            for key in ("text", "content", "message", "result"):
                value = result.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return str(result).strip()
