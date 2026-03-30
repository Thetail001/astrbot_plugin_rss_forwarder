import asyncio
import json
import os
import re
from html import unescape
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import ProxyHandler, Request, build_opener

from astrbot.api import logger

from .config import DEFAULT_DAILY_DIGEST_PROMPT, RSSConfig


class FeedPipeline:
    """处理层：负责在分发前对条目进行可选增强。"""

    GITHUB_MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
    GOOGLE_TRANSLATE_ENDPOINT = "https://translation.googleapis.com/language/translate/v2"
    _TAG_RE = re.compile(r"<[^>]+>")
    _SPACE_RE = re.compile(r"\s+")

    def __init__(self, context, config: RSSConfig) -> None:
        self.context = context
        self._config = config

    async def process(self, entry: dict[str, Any]) -> dict[str, Any]:
        """执行分发前处理，失败时始终回退到清洗后的原文。"""
        if not self._translation_enabled():
            return entry

        source = self._extract_source_fields(entry)
        if not source["title"] and not source["summary"]:
            return entry

        enriched = dict(entry)
        enriched.setdefault("_source_title", source.get("title", ""))
        enriched.setdefault("_source_summary", source.get("summary", ""))

        (
            translated,
            selected_engine,
            llm_reason,
            github_reason,
            google_reason,
        ) = await self._translate_fields(entry, source)
        if not translated:
            translated = self._build_fallback_fields(source)
            selected_engine = "fallback"

        logger.info(
            "translation item=%s engine=%s llm=%s google=%s github=%s",
            self._item_ref(entry),
            selected_engine,
            llm_reason or "-",
            google_reason or "-",
            github_reason or "-",
        )

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
            "github": {
                "enabled": bool(self._config.github_models_enabled),
                "timeout_seconds": int(self._config.github_models_timeout_seconds),
                "model": str(self._config.github_models_model),
                "token_source": "",
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
        report["github"]["token_source"] = self._github_token_source()

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
                report["github"]["error"] = "skipped_after_llm_success"
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
                report["github"]["error"] = "skipped_after_google_success"
                return report
            report["google"]["error"] = google_reason
        else:
            report["google"]["error"] = "google_disabled"

        if self._config.github_models_enabled:
            loop = asyncio.get_running_loop()
            start = loop.time()
            github_fields, github_reason = await self._try_github_models_translate_fields(source)
            report["github"]["latency_ms"] = int((loop.time() - start) * 1000)
            if github_fields:
                report["github"]["ok"] = True
                report["github"]["preview"] = self._compose_preview(github_fields)
                report["github"]["error"] = ""
                report["selected_engine"] = "github_models"
            else:
                report["github"]["error"] = github_reason
        else:
            report["github"]["error"] = "github_models_disabled"

        return report

    async def build_daily_digest_content(
        self,
        digest: dict[str, Any],
        items: list[dict[str, Any]],
        *,
        unified_msg_origin: str = "",
    ) -> dict[str, str]:
        prepared_items = self._prepare_digest_items(
            items,
            limit=int(digest.get("max_items", 20) or 20),
        )
        if not prepared_items:
            return {
                "content": "",
                "engine": "empty",
                "llm_reason": "empty_items",
            }

        digest_entry = {"unified_msg_origin": str(unified_msg_origin or "").strip()}
        llm_reason = "llm_disabled"
        if self._config.llm_enabled:
            content, llm_reason = await self._try_llm_daily_digest_content(
                digest_entry,
                digest,
                prepared_items,
            )
            if content:
                return {
                    "content": content,
                    "engine": "llm",
                    "llm_reason": llm_reason,
                }

        return {
            "content": self._build_daily_digest_fallback_text(prepared_items),
            "engine": "fallback",
            "llm_reason": llm_reason,
        }

    async def _translate_fields(
        self,
        entry: dict[str, Any],
        source: dict[str, str],
    ) -> tuple[dict[str, str], str, str, str, str]:
        llm_reason = "llm_disabled"
        github_reason = "github_models_disabled"
        google_reason = "google_disabled"

        if self._config.llm_enabled:
            llm_fields, llm_reason = await self._try_llm_translate_fields(entry, source)
            if llm_fields:
                return (
                    llm_fields,
                    "llm",
                    llm_reason,
                    "skipped_after_llm_success",
                    "skipped_after_llm_success",
                )

        if self._config.google_translate_enabled:
            google_fields, google_reason = await self._try_google_translate_fields(source)
            if google_fields:
                return google_fields, "google", llm_reason, "skipped_after_google_success", google_reason

        if self._config.github_models_enabled:
            github_fields, github_reason = await self._try_github_models_translate_fields(source)
            if github_fields:
                return github_fields, "github_models", llm_reason, github_reason, google_reason

        return {}, "fallback", llm_reason, github_reason, google_reason

    async def _try_llm_daily_digest_content(
        self,
        entry: dict[str, Any],
        digest: dict[str, Any],
        items: list[dict[str, str]],
    ) -> tuple[str, str]:
        provider_id = await self._resolve_provider_id(entry)
        if not provider_id:
            return "", "provider_missing"

        prompt = self._build_daily_digest_prompt(digest, items)
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
            return "", "timeout"
        except Exception as exc:
            return "", f"exception:{type(exc).__name__}"

        generated_text = self._sanitize_daily_digest_text(self._extract_generated_text(result))
        if not generated_text:
            return "", "invalid_payload"
        return generated_text, "ok"

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

    async def _try_github_models_translate_fields(
        self,
        source: dict[str, str],
    ) -> tuple[dict[str, str], str]:
        token = self._resolve_github_models_token()
        if not token:
            logger.warning(
                "github_models_enabled=true but no GitHub token is available, skip github models"
            )
            return {}, "token_missing"

        title = source.get("title", "")
        summary = source.get("summary", "")
        if not title or not summary:
            return {}, "source_empty"

        try:
            translated = await asyncio.wait_for(
                asyncio.to_thread(self._github_models_translate_blocking, source, token),
                timeout=self._config.github_models_timeout_seconds,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "github models translate timeout after %ss",
                self._config.github_models_timeout_seconds,
            )
            return {}, "timeout"
        except Exception as exc:
            logger.warning("github models translate failed: %s", exc)
            return {}, f"exception:{type(exc).__name__}"

        parsed = self._parse_llm_translation(translated)
        if not parsed:
            return {}, "invalid_payload"

        title_cn = self._sanitize_text(str(parsed.get("title", "") or ""))
        summary_cn = self._sanitize_text(str(parsed.get("summary", "") or ""))
        if not title_cn or not summary_cn:
            return {}, "empty_fields"

        return {"title": title_cn, "summary": summary_cn}, "ok"

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

    def _github_models_translate_blocking(
        self,
        source: dict[str, str],
        token: str,
    ) -> str:
        prompt = self._build_prompt(source)
        body = json.dumps(
            {
                "model": self._config.github_models_model,
                "messages": [{"role": "user", "content": prompt}],
                "response_format": {"type": "json_object"},
            }
        ).encode("utf-8")

        req = Request(
            url=self.GITHUB_MODELS_ENDPOINT,
            data=body,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
                "Content-Type": "application/json",
                "User-Agent": "astrbot_plugin_rss_forwarder/0.4.1 (+https://github.com/RhoninSeiei/astrbot_plugin_rss_forwarder)",
            },
            method="POST",
        )

        opener = self._build_github_models_opener()
        timeout = self._config.github_models_timeout_seconds
        try:
            with opener.open(req, timeout=timeout) as resp:  # noqa: S310
                raw = resp.read().decode("utf-8", errors="ignore")
        except HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="ignore")
            except Exception:
                detail = str(exc)
            raise RuntimeError(f"github models http error {exc.code}: {detail}") from exc
        except URLError as exc:
            raise RuntimeError(f"github models network error: {exc}") from exc

        data = json.loads(raw)
        choices = data.get("choices") or []
        if not isinstance(choices, list) or not choices:
            return ""

        message = choices[0].get("message") or {}
        content = message.get("content", "")
        return str(content or "").strip()

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
        return self._build_proxy_opener(
            self._config.google_translate_proxy_mode,
            self._config.google_translate_proxy_url,
        )

    def _build_github_models_opener(self):
        return self._build_proxy_opener(
            self._config.github_models_proxy_mode,
            self._config.github_models_proxy_url,
        )

    @staticmethod
    def _build_proxy_opener(mode: str, proxy_url: str):
        normalized_mode = str(mode or "system").strip().lower()
        normalized_proxy_url = str(proxy_url or "").strip()

        if normalized_mode == "off":
            return build_opener(ProxyHandler({}))

        if normalized_mode == "custom":
            if not normalized_proxy_url:
                return build_opener(ProxyHandler({}))
            return build_opener(
                ProxyHandler(
                    {"http": normalized_proxy_url, "https": normalized_proxy_url}
                )
            )

        return build_opener()

    def _build_llm_proxy_kwargs(self) -> dict[str, Any]:
        mode = str(self._config.llm_proxy_mode or "system").strip().lower()
        proxy_url = str(self._config.llm_proxy_url or "").strip()

        if mode == "custom" and proxy_url:
            return {"proxy": proxy_url, "trust_env": False}
        if mode == "off":
            return {"trust_env": False}
        return {}

    def _build_daily_digest_prompt(
        self,
        digest: dict[str, Any],
        items: list[dict[str, str]],
    ) -> str:
        template = (
            str(digest.get("prompt_template", DEFAULT_DAILY_DIGEST_PROMPT)).strip()
            or DEFAULT_DAILY_DIGEST_PROMPT
        )
        payload = json.dumps(items, ensure_ascii=False, indent=2)
        values = {
            "title": str(digest.get("title", "")).strip() or "RSS 日报",
            "window_start": str(digest.get("window_start_text", "")).strip(),
            "window_end": str(digest.get("window_end_text", "")).strip(),
            "max_items": int(digest.get("max_items", len(items)) or len(items)),
            "items": payload,
        }
        try:
            return template.format(**values)
        except Exception:
            return DEFAULT_DAILY_DIGEST_PROMPT.format(**values)

    def _prepare_digest_items(self, items: list[dict[str, Any]], limit: int) -> list[dict[str, str]]:
        prepared: list[dict[str, str]] = []
        for item in items[: max(limit, 1)]:
            title = self._sanitize_text(str(item.get("title", "") or ""))
            source = self._sanitize_text(
                str(item.get("feed_title", "") or item.get("source", "") or "")
            )
            summary = self._sanitize_text(str(item.get("summary", "") or item.get("content", "") or ""))
            if len(summary) > 200:
                summary = summary[:199].rstrip() + "…"
            prepared.append(
                {
                    "source": source or "未知来源",
                    "title": title or "(无标题)",
                    "summary": summary,
                    "link": str(item.get("link", "") or "").strip(),
                    "published_at": str(item.get("published_at", "") or "").strip(),
                }
            )
        return prepared

    @staticmethod
    def _build_daily_digest_fallback_text(items: list[dict[str, str]]) -> str:
        lines = []
        for index, item in enumerate(items, start=1):
            source = str(item.get("source", "") or "").strip() or "未知来源"
            title = str(item.get("title", "") or "").strip() or "(无标题)"
            lines.append(f"{index}. [{source}] {title}")
        return "\n".join(lines)

    @classmethod
    def _sanitize_daily_digest_text(cls, text: str) -> str:
        stripped = cls._strip_code_fence(text)
        lines = [line.strip() for line in stripped.splitlines() if line.strip()]
        return "\n".join(lines)

    @staticmethod
    def _astrbot_data_dir() -> Path:
        try:
            from astrbot.core.utils.astrbot_path import get_astrbot_data_path

            return Path(get_astrbot_data_path())
        except Exception:
            return Path("data")

    def _resolve_github_models_token_path(self) -> Path:
        configured = str(self._config.github_models_token_file or "").strip() or "github.token"
        path = Path(configured)
        if path.is_absolute():
            return path
        return self._astrbot_data_dir() / path

    def _resolve_github_models_token(self) -> str:
        env_token = str(
            os.getenv("ASTRBOT_GITHUB_TOKEN") or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or ""
        ).strip()
        if env_token:
            return env_token

        token_path = self._resolve_github_models_token_path()
        try:
            return token_path.read_text(encoding="utf-8").strip()
        except OSError:
            return ""

    def _github_token_source(self) -> str:
        if str(os.getenv("ASTRBOT_GITHUB_TOKEN") or "").strip():
            return "env:ASTRBOT_GITHUB_TOKEN"
        if str(os.getenv("GITHUB_TOKEN") or "").strip():
            return "env:GITHUB_TOKEN"
        if str(os.getenv("GH_TOKEN") or "").strip():
            return "env:GH_TOKEN"
        return str(self._resolve_github_models_token_path())

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

    def _translation_enabled(self) -> bool:
        return any(
            [
                self._config.llm_enabled,
                self._config.github_models_enabled,
                self._config.google_translate_enabled,
            ]
        )

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
