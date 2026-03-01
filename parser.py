from __future__ import annotations

from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from hashlib import sha256
from typing import Any
from xml.etree import ElementTree as ET

from astrbot.api import logger


class FeedParser:
    """解析层：将 RSS/Atom 转换为统一条目结构。"""

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
            for link_node in entry.findall(self._tag(ns, "link")):
                href = (link_node.attrib.get("href") or "").strip()
                rel = (link_node.attrib.get("rel") or "alternate").strip()
                if href and rel in {"alternate", ""}:
                    link = href
                    break
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
                }
            )
        return result

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
        return "".join(node.itertext()).strip()

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
