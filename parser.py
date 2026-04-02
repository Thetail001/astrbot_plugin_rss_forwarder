from __future__ import annotations

import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from html import unescape
from typing import Any
from urllib.parse import urlparse

try:
    from defusedxml import ElementTree as ET
except ImportError:  # pragma: no cover - fallback for environments without defusedxml.
    from xml.etree import ElementTree as ET

from astrbot.api import logger


class FeedParser:
    """解析层：将 RSS/Atom 转换为统一条目结构。"""

    _IMG_SRC_RE = re.compile(r"<img\b[^>]*\bsrc=['\"]([^'\"]+)['\"]", re.IGNORECASE)

    def parse(self, raw_items: list[dict], job=None) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        for raw in raw_items:
            feed_id = str(raw.get("feed_id", "")).strip()
            body = str(raw.get("body", "") or "")
            if not body:
                continue
            try:
                parsed = self._parse_xml(feed_id, body)
                for item in parsed:
                    if job is not None:
                        item.setdefault("job_id", getattr(job, "id", ""))
                    entries.append(item)
            except Exception as exc:
                logger.warning("parse feed=%s failed: %s", feed_id, exc)
        return entries

    def _parse_xml(self, feed_id: str, xml_text: str) -> list[dict[str, Any]]:
        root = ET.fromstring(xml_text)
        tag = self._strip_ns(root.tag)
        if tag == "rss":
            return self._parse_rss(feed_id, root)
        if tag == "feed":
            return self._parse_atom(feed_id, root)
        return []

    def _parse_rss(self, feed_id: str, root) -> list[dict[str, Any]]:
        channel = root.find("channel")
        if channel is None:
            return []
        feed_title = self._text(channel.find("title"))
        result: list[dict[str, Any]] = []
        for item in channel.findall("item"):
            title = self._text(item.find("title"))
            link = self._text(item.find("link"))
            guid = self._text(item.find("guid"))
            summary = self._text(item.find("description"))
            published = self._normalize_time(self._text(item.find("pubDate")))
            image_url = self._extract_rss_image_url(item, summary)
            category = self._text(item.find("category"))
            author = self._text(item.find("author"))
            item_id = guid or link or sha256(f"{title}|{published}".encode("utf-8")).hexdigest()
            result.append(
                {
                    "feed_id": feed_id,
                    "feed_title": feed_title,
                    "title": title,
                    "link": link,
                    "guid": item_id,
                    "summary": summary,
                    "published_at": published,
                    "source": feed_title,
                    "image_url": image_url,
                    "category": category,
                    "author": author,
                }
            )
        return result

    def _parse_atom(self, feed_id: str, root) -> list[dict[str, Any]]:
        ns = self._namespace(root.tag)
        feed_title = self._text(root.find(self._tag(ns, "title")))
        result: list[dict[str, Any]] = []
        for entry in root.findall(self._tag(ns, "entry")):
            title = self._text(entry.find(self._tag(ns, "title")))
            id_text = self._text(entry.find(self._tag(ns, "id")))
            summary = self._text(entry.find(self._tag(ns, "summary"))) or self._text(
                entry.find(self._tag(ns, "content"))
            )
            published = self._normalize_time(
                self._text(entry.find(self._tag(ns, "published")))
                or self._text(entry.find(self._tag(ns, "updated")))
            )
            link = ""
            image_url = ""
            for link_node in entry.findall(self._tag(ns, "link")):
                href = (link_node.attrib.get("href") or "").strip()
                rel = (link_node.attrib.get("rel") or "alternate").strip().lower()
                link_type = (link_node.attrib.get("type") or "").strip().lower()
                if href and rel in {"alternate", ""} and not link:
                    link = href
                if href and rel == "enclosure" and link_type.startswith("image/") and not image_url:
                    image_url = href

            if not image_url:
                for child in entry.iter():
                    if child is entry:
                        continue
                    local = self._strip_ns(child.tag).lower()
                    if local in {"content", "thumbnail", "image"}:
                        url = (child.attrib.get("url") or child.attrib.get("href") or "").strip()
                        if self._is_http_url(url):
                            image_url = url
                            break

            if not image_url:
                image_url = self._extract_image_from_html(summary)

            # Atom Category: <category term="..."/>
            category = ""
            cat_node = entry.find(self._tag(ns, "category"))
            if cat_node is not None:
                category = (cat_node.attrib.get("term") or "").strip()

            # Atom Author: <author><name>...</name></author>
            author = ""
            author_node = entry.find(self._tag(ns, "author"))
            if author_node is not None:
                author = self._text(author_node.find(self._tag(ns, "name")))

            item_id = id_text or link or sha256(f"{title}|{published}".encode("utf-8")).hexdigest()
            result.append(
                {
                    "feed_id": feed_id,
                    "feed_title": feed_title,
                    "title": title,
                    "link": link,
                    "guid": item_id,
                    "summary": summary,
                    "published_at": published,
                    "source": feed_title,
                    "image_url": image_url,
                    "category": category,
                    "author": author,
                }
            )
        return result

    def _extract_rss_image_url(self, item, summary: str) -> str:
        enclosure = item.find("enclosure")
        if enclosure is not None:
            url = (enclosure.attrib.get("url") or "").strip()
            mime = (enclosure.attrib.get("type") or "").strip().lower()
            if self._is_http_url(url) and (not mime or mime.startswith("image/")):
                return url

        for child in item:
            local = self._strip_ns(child.tag).lower()
            if local in {"enclosure", "content", "thumbnail", "image"}:
                url = (child.attrib.get("url") or child.attrib.get("href") or "").strip()
                mime = (child.attrib.get("type") or "").strip().lower()
                if self._is_http_url(url) and (local != "enclosure" or not mime or mime.startswith("image/")):
                    return url

        return self._extract_image_from_html(summary)

    def _extract_image_from_html(self, html_text: str) -> str:
        if not html_text:
            return ""
        match = self._IMG_SRC_RE.search(html_text)
        if not match:
            return ""
        src = unescape((match.group(1) or "").strip())
        if self._is_http_url(src):
            return src
        return ""

    @staticmethod
    def _is_http_url(url: str) -> bool:
        if not url:
            return False
        parsed = urlparse(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.netloc)

    @staticmethod
    def _strip_ns(tag: str) -> str:
        return tag.split("}", 1)[-1] if "}" in tag else tag

    @staticmethod
    def _namespace(tag: str) -> str:
        if tag.startswith("{") and "}" in tag:
            return tag[1 : tag.index("}")]
        return ""

    @staticmethod
    def _tag(ns: str, name: str) -> str:
        return f"{{{ns}}}{name}" if ns else name

    @staticmethod
    def _text(node) -> str:
        if node is None:
            return ""
        raw = "".join(node.itertext()).strip()
        # 还原 &amp; 等转义字符
        return unescape(raw)

    @staticmethod
    def _normalize_time(raw: str) -> str:
        text = str(raw or "").strip()
        if not text:
            return ""
        try:
            dt = parsedate_to_datetime(text)
        except Exception:
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except Exception:
                return text
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).isoformat()
