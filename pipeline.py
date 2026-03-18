import asyncio
import json
import re
from html import unescape
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import logger

from .config import RSSConfig


class FeedPipeline:
    """处理层：负责在分发前对条目进行可选增强。"""

    GOOGLE_TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
    _TAG_RE = re.compile(r"<[^>]+>")
    _SPACE_RE = re.compile(r"\s+")

    def __init__(self, context, config: RSSConfig) -> None:
        self.context = context
        self._config = config

    async def process(self, entry: dict[str, Any]) -> dict[str, Any]:
        """执行分发前处理，失败时始终回退到清洗后的原文。"""
        if not self._config.llm_enabled and not self._config.google_translate_enabled:
            return entry

        source = self._extract_source_fields(entry)
        if not source["title"] and not source["summary"]:
            return entry

        translated, selected_engine, llm_reason, google_reason = await self._translate_fields(entry, source)
        if not translated:
            translated = self._build_fallback_fields(source)
            selected_engine = "fallback"

        logger.info(
            "translation item=%s engine=%s llm=%s google=%s",
            self._item_ref(entry),
            selected_engine,
            llm_reason or "-",
            google_reason or "-",
        )

        enriched = dict(entry)
        if translated.get("title"):
            enriched["title"] = translated["title"]
        if translated.get("summary"):
            enriched["summary"] = translated["summary"]
        return enriched

    async def diagnose_translation(self, entry: dict[str, Any] | None = None) -> dict[str, Any]:
        """执行翻译链路自检，不触发消息分发。"""
        sample = dict(entry or {})
        sample.setdefault("title", "RSS translation diagnostic title")
        sample.setdefault("summary", "This is a translation diagnostics message from RSS forwarder.")

        source = self._extract_source_fields(sample)
        input_text = self._build_input_text(source)

        report: dict[str, Any] = {
            "input_chars": len(input_text),
            "selected_engine": "fallback",
            "llm": {
                "enabled": bool(self._config.llm_enabled),
                "timeout_seconds": int(self._config.llm_timeout_seconds),
                "provider_id": "",
                "ok": False,
                "latency_ms": 0,
                "error": "",
                "preview": "",
            },
            "google": {
                "enabled": bool(self._config.google_translate_enabled),
                "timeout_seconds": int(self._config.google_translate_timeout_seconds),
                "target_lang": str(self._config.google_translate_target_lang),
                "ok": False,
                "latency_ms": 0,
                "error": "",
                "preview": "",
            },
        }

        if not input_text:
            report["error"] = "empty_input"
            return report

        provider_id = await self._resolve_provider_id(sample)
        report["llm"]["provider_id"] = provider_id

        llm_fields: dict[str, str] = {}
        if self._config.llm_enabled:
            loop = asyncio.get_running_loop()
            start = loop.time()
            llm_fields, llm_reason = await self._try_llm_translate_fields(sample, source)
            report["llm"]["latency_ms"] = int((loop.time() - start) * 1000)
            if llm_fields:
                report["llm"]["ok"] = True
                report["llm"]["preview"] = self._compose_preview(llm_fields)
                report["llm"]["error"] = ""
                report["selected_engine"] = "llm"
                report["google"]["error"] = "skipped_after_llm_success"
                return report
            report["llm"]["error"] = llm_reason
        elif provider_id:
            report["llm"]["error"] = "llm_disabled"
        else:
            report["llm"]["error"] = "llm_disabled_or_provider_missing"

        if self._config.google_translate_enabled:
            loop = asyncio.get_running_loop()
            start = loop.time()
            google_fields, google_reason = await self._try_google_translate_fields(source)
            report["google"]["latency_ms"] = int((loop.time() - start) * 1000)
            if google_fields:
                report["google"]["ok"] = True
                report["google"]["preview"] = self._compose_preview(google_fields)
                report["google"]["error"] = ""
                report["selected_engine"] = "google"
            else:
                report["google"]["error"] = google_reason
        else:
            report["google"]["error"] = "google_disabled"

        return report

    async def _translate_fields(
        self,
        entry: dict[str, Any],
        source: dict[str, str],
    ) -> tuple[dict[str, str], str, str, str]:
        llm_reason = "llm_disabled"
        google_reason = "google_disabled"

        if self._config.llm_enabled:
            llm_fields, llm_reason = await self._try_llm_translate_fields(entry, source)
            if llm_fields:
                return llm_fields, "llm", llm_reason, "skipped_after_llm_success"

        if self._config.google_translate_enabled:
            google_fields, google_reason = await self._try_google_translate_fields(source)
            if google_fields:
                return google_fields, "google", llm_reason, google_reason

        return {}, "fallback", llm_reason, google_reason

    async def _try_llm_translate_fields(
        self,
        entry: dict[str, Any],
        source: dict[str, str],
    ) -> tuple[dict[str, str], str]:
        provider_id = await self._resolve_provider_id(entry)
        if not provider_id:
            logger.warning("llm enabled but no available provider id, skip llm enrich")
            return {}, "provider_missing"

        prompt = self._build_prompt(source)
        llm_kwargs: dict[str, Any] = {
            "chat_provider_id": provider_id,
            "prompt": prompt,
        }
        profile = str(self._config.llm_profile or "").strip()
        if profile:
            llm_kwargs["profile"] = profile

        llm_kwargs.update(self._build_llm_proxy_kwargs())

        llm_call = self.context.llm_generate(**llm_kwargs)
        try:
            result = await asyncio.wait_for(llm_call, timeout=self._config.llm_timeout_seconds)
        except asyncio.TimeoutError:
            logger.warning(
                "pipeline llm enrich timeout after %ss, fallback to next translator",
                self._config.llm_timeout_seconds,
            )
            return {}, "timeout"
        except Exception as exc:
            logger.warning("pipeline llm enrich failed, fallback to next translator: %s", exc)
            return {}, f"exception:{type(exc).__name__}"

        generated_text = self._extract_generated_text(result)
        parsed = self._parse_llm_translation(generated_text)
        if not parsed:
            logger.warning("pipeline llm enrich returned non-json/invalid payload")
            return {}, "invalid_payload"

        title = self._sanitize_text(str(parsed.get("title", "") or ""))
        summary = self._sanitize_text(str(parsed.get("summary", "") or ""))
        if not title or not summary:
            return {}, "empty_fields"

        return {"title": title, "summary": summary}, "ok"

    async def _try_google_translate_fields(self, source: dict[str, str]) -> tuple[dict[str, str], str]:
        api_key = str(self._config.google_translate_api_key or "").strip()
        if not api_key:
            logger.warning("google_translate_enabled=true but api key is empty, skip google translate")
            return {}, "api_key_missing"

        title = source.get("title", "")
        summary = source.get("summary", "")
        if not title or not summary:
            return {}, "source_empty"

        try:
            translated = await asyncio.wait_for(
                asyncio.to_thread(self._google_translate_batch_blocking, [title, summary]),
                timeout=self._config.google_translate_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "google translate timeout after %ss",
                self._config.google_translate_timeout_seconds,
            )
            return {}, "timeout"
        except Exception as exc:
            logger.warning("google translate failed: %s", exc)
            return {}, f"exception:{type(exc).__name__}"

        if len(translated) < 2:
            return {}, "empty_result"

        title_cn = self._sanitize_text(translated[0])
        summary_cn = self._sanitize_text(translated[1])
        if not title_cn or not summary_cn:
            return {}, "empty_fields"

        return {"title": title_cn, "summary": summary_cn}, "ok"

    def _google_translate_batch_blocking(self, texts: list[str]) -> list[str]:
        payload: list[tuple[str, str]] = []
        for text in texts:
            value = str(text or "").strip()
            if value:
                payload.append(("q", value))
        payload.extend(
            [
                ("target", self._config.google_translate_target_lang),
                ("format", "text"),
                ("key", self._config.google_translate_api_key),
            ]
        )
        body = urlencode(payload, doseq=True).encode("utf-8")

        req = Request(
            url=self.GOOGLE_TRANSLATE_ENDPOINT,
            data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "astrbot_plugin_rss_forwarder/0.2 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            },
            method="POST",
        )

        opener = self._build_google_opener()
        timeout = self._config.google_translate_timeout_seconds
        try:
            with opener.open(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", errors="ignore")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"google translate http error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"google translate network error: {exc}") from exc

        data = json.loads(raw)
        if isinstance(data, dict) and data.get("error"):
            message = str((data.get("error") or {}).get("message", "unknown error"))
            raise RuntimeError(f"google translate api error: {message}")

        translations = ((data.get("data") or {}).get("translations") or []) if isinstance(data, dict) else []
        if not isinstance(translations, list):
            return []

        output: list[str] = []
        for item in translations:
            text = str((item or {}).get("translatedText", "")).strip()
            if text:
                output.append(unescape(text))
        return output

    def _build_google_opener(self):
        mode = str(self._config.google_translate_proxy_mode or "system").strip().lower()
        proxy_url = str(self._config.google_translate_proxy_url or "").strip()

        if mode == "off":
            return build_opener(ProxyHandler({}))

        if mode == "custom":
            if not proxy_url:
                return build_opener(ProxyHandler({}))
            return build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))

        return build_opener()

    def _build_llm_proxy_kwargs(self) -> dict[str, Any]:
        mode = str(self._config.llm_proxy_mode or "system").strip().lower()
        proxy_url = str(self._config.llm_proxy_url or "").strip()

        if mode == "custom" and proxy_url:
            return {"proxy": proxy_url, "trust_env": False}
        if mode == "off":
            return {"trust_env": False}
        return {}

    async def _resolve_provider_id(self, entry: dict[str, Any]) -> str:
        provider_id = str(self._config.llm_provider_id or "").strip()
        if provider_id:
            return provider_id

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

    def _extract_source_fields(self, entry: dict[str, Any]) -> dict[str, str]:
        title = self._sanitize_text(str(entry.get("title", "") or ""))
        summary = self._sanitize_text(str(entry.get("summary", "") or entry.get("content", "") or ""))
        if not summary and title:
            summary = title
        return {"title": title, "summary": summary}

    def _build_input_text(self, source: dict[str, str]) -> str:
        parts = [part for part in [source.get("title", ""), source.get("summary", "")] if part]
        merged = "\n\n".join(parts)
        if not merged:
            return ""
        return merged[: self._config.max_input_chars]

    @staticmethod
    def _build_fallback_fields(source: dict[str, str]) -> dict[str, str]:
        title = str(source.get("title", "") or "").strip()
        summary = str(source.get("summary", "") or "").strip()
        return {"title": title, "summary": summary}

    def _build_prompt(self, source: dict[str, str]) -> str:
        input_text = self._build_input_text(source)
        return (
            "请将以下 RSS 新闻翻译为简体中文，并严格只返回 JSON。\n"
            "输出格式必须是：{\"title\":\"...\",\"summary\":\"...\"}\n"
            "要求：\n"
            "1) title 必须是中文标题，不要保留英文原文；\n"
            "2) summary 必须是中文摘要，不要出现“翻译：”或“摘要：”标签；\n"
            "3) summary 控制在 180 字以内。\n\n"
            f"内容：\n{input_text}"
        )

    @classmethod
    def _parse_llm_translation(cls, text: str) -> dict[str, str] | None:
        raw = str(text or "").strip()
        if not raw:
            return None

        stripped = cls._strip_code_fence(raw)
        candidates = [stripped]

        first = stripped.find("{")
        last = stripped.rfind("}")
        if first != -1 and last != -1 and first < last:
            candidates.append(stripped[first : last + 1])

        for candidate in candidates:
            try:
                data = json.loads(candidate)
            except Exception:
                continue
            if isinstance(data, dict):
                return {
                    "title": str(data.get("title", "") or ""),
                    "summary": str(data.get("summary", "") or ""),
                }
        return None

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        value = str(text or "").strip()
        if value.startswith("```"):
            value = re.sub(r"^```[a-zA-Z0-9_-]*\n", "", value)
            if value.endswith("```"):
                value = value[:-3]
        return value.strip()

    @classmethod
    def _sanitize_text(cls, text: str) -> str:
        value = unescape(str(text or ""))
        if not value:
            return ""
        value = cls._TAG_RE.sub(" ", value)
        value = value.replace("\u00a0", " ")
        value = cls._SPACE_RE.sub(" ", value).strip()
        return value

    @classmethod
    def _compose_preview(cls, fields: dict[str, str]) -> str:
        title = cls._preview(fields.get("title", ""), limit=40)
        summary = cls._preview(fields.get("summary", ""), limit=80)
        if title and summary:
            return f"标题：{title} | 摘要：{summary}"
        return title or summary

    @staticmethod
    def _item_ref(entry: dict[str, Any]) -> str:
        for key in ("id", "guid", "link", "title"):
            value = str(entry.get(key, "") or "").strip()
            if value:
                return value[:120]
        return "unknown"

    @staticmethod
    def _preview(text: str, limit: int = 120) -> str:
        value = str(text or "").strip()
        if len(value) <= limit:
            return value
        return value[:limit] + "..."

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
