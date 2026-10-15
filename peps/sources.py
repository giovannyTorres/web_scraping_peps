"""Discovery adapters and bounded extraction for explicitly approved sources."""
from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from urllib.parse import quote, urlencode, urljoin, urlsplit
from xml.etree import ElementTree


class _TextExtractor(HTMLParser):
    ignored = {"script", "style", "noscript", "svg", "iframe", "template", "head", "nav", "form"}

    def __init__(self, limit: int = 200_000):
        super().__init__(convert_charrefs=True)
        self.limit = limit
        self.chunks: list[str] = []
        self.title: list[str] = []
        self.ignored_stack: list[str] = []
        self.in_title = False
        self.length = 0
        self.published_at = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and (values.get("property") or values.get("name") or "").lower() in {"article:published_time", "datepublished", "pubdate"}:
            self.published_at = values.get("content")
        if tag in self.ignored:
            self.ignored_stack.append(tag)
        if not self.ignored_stack and tag in {"p", "div", "br", "li", "section", "article", "h1", "h2", "h3"}:
            self._append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag == "title":
            self.in_title = False
        if tag in self.ignored_stack:
            # Recover gracefully from malformed markup without exposing scripts.
            index = len(self.ignored_stack) - 1 - self.ignored_stack[::-1].index(tag)
            self.ignored_stack = self.ignored_stack[:index]
        if not self.ignored_stack and tag in {"p", "div", "li", "section", "article", "h1", "h2", "h3"}:
            self._append(" ")

    def _append(self, value: str) -> None:
        if self.length < self.limit:
            value = value[:self.limit - self.length]
            self.chunks.append(value)
            self.length += len(value)

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title.append(data[:1000])
        if not self.ignored_stack:
            self._append(data)

    @property
    def text(self) -> str:
        return re.sub(r"\s+", " ", "".join(self.chunks)).strip()


def extract_text(html: str, max_chars: int = 200_000) -> str:
    """Extract visible text; never executes JavaScript or loads subresources."""
    parser = _TextExtractor(max(1, min(max_chars, 1_000_000)))
    parser.feed(str(html or "")[:5_000_000])
    parser.close()
    return parser.text


def build_search_url(entity: str | dict, source: dict) -> str:
    name = entity.get("name", "") if isinstance(entity, dict) else str(entity)
    name = re.sub(r"\s+", " ", name).strip()
    if not name or len(name) > 300:
        raise ValueError("Entity name must contain 1 to 300 characters")
    # Escape search language operators inside the quoted entity name.
    name = name.replace('"', " ").replace("\\", " ")
    query = '"' + name + '" (México OR Mexico)'
    if source.get("id") == "gdelt" or source.get("kind") == "gdelt":
        base = source.get("base_url", "https://api.gdeltproject.org/api/v2/doc/doc")
        params = dict(query=query, mode="ArtList", format="json", sort="DateDesc",
                      maxrecords=max(1, min(int(source.get("max_records", 100)), 250)),
                      timespan=source.get("timespan", "7d"))
        return base + ("&" if "?" in base else "?") + urlencode(params)
    template = source.get("url") or source.get("search_url") or source.get("base_url")
    if not template:
        raise ValueError("Source has no discovery URL")
    if "{query}" in template:
        return template.replace("{query}", quote(query, safe=""))
    if source.get("kind") in {"rss", "atom"}:
        return template
    raise ValueError("Direct sources require an explicit public document URL")


def _date(value: str | None) -> str | None:
    if not value:
        return None
    value = str(value).strip()
    parsed = None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%d%H%M%S", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(value, fmt)
                break
            except ValueError:
                continue
    if parsed is None:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat()


def _public_document_url(value: str, base: str) -> str | None:
    try:
        url = urljoin(base, value.strip())
        parts = urlsplit(url)
        if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password or parts.port not in (None, 443):
            return None
        return url
    except (ValueError, AttributeError):
        return None


def parse_results(fetch_result: dict, source: dict) -> list[dict]:
    """Return candidates, not allegations. Candidate URLs require fetcher validation.

    RSS/GDELT discovery snippets never establish an adverse event. The pipeline
    must retrieve the actual document, disambiguate the subject and retain proof.
    Invalid discovery responses raise ValueError so the durable queue retries;
    a valid empty feed or {"articles": []} is a successful empty result.
    """
    if fetch_result.get("status") not in {"ok", "not_modified"}:
        return []
    body = str(fetch_result.get("body") or "")
    if len(body) > min(20_000_000, int(source.get('parse_max_chars', 5_000_000))):
        raise ValueError('Response exceeds parser size limit; no silent truncation')
    base = fetch_result.get("final_url") or fetch_result.get("url") or source.get("base_url", "")
    kind, source_id = source.get("kind", "news"), source.get("id", "unknown")
    candidates = []
    limit = max(1, min(int(source.get('max_items', source.get('max_records', 250))), 100000))
    fetch_result['parse_metadata'] = {'observed_entries': 0, 'truncated': False}
    if source_id == "gdelt" or kind == "gdelt":
        try:
            payload = json.loads(body)
        except (ValueError, RecursionError) as exc:
            raise ValueError("GDELT returned malformed JSON; discovery was not completed") from exc
        if not isinstance(payload, dict):
            raise ValueError("GDELT response must be a JSON object with articles")
        if (payload.get("error") or payload.get("errors") or payload.get("success") is False
                or str(payload.get("status", "")).lower() in {"error", "failed", "failure"}):
            raise ValueError("GDELT returned an error envelope; discovery was not completed")
        articles = payload.get("articles")
        if not isinstance(articles, list):
            raise ValueError("GDELT response is missing a valid articles array")
        fetch_result['parse_metadata'] = {'observed_entries': len(articles), 'truncated': len(articles) > limit}
        for article in articles[:limit]:
            if not isinstance(article, dict) or not isinstance(article.get("url"), str) or not article["url"].strip():
                raise ValueError("GDELT returned a malformed article record")
            candidates.append(dict(url=article["url"], title=article.get("title", ""), text=article.get("description", ""), published_at=_date(article.get("published_at") or article.get("datepublished")), discovered_at=_date(article.get("seendate")), source_kind="news", source_id=source_id))
    elif kind in {"rss", "atom"}:
        # ElementTree does not resolve external entities; reject declarations too.
        if "<!DOCTYPE" in body.upper() or "<!ENTITY" in body.upper():
            raise ValueError("RSS/Atom declarations and external entities are forbidden")
        try:
            root = ElementTree.fromstring(body)
        except (ElementTree.ParseError, ValueError) as exc:
            raise ValueError("RSS/Atom returned malformed XML; discovery was not completed") from exc
        root_tag = root.tag.rsplit("}", 1)[-1].lower()
        if root_tag not in {"rss", "feed", "rdf"}:
            raise ValueError("Response is not a recognized RSS/Atom feed")
        if root_tag == "rss" and not any(child.tag.rsplit("}", 1)[-1] == "channel" for child in root):
            raise ValueError("RSS response is missing its channel")
        for entry in root.iter():
            if entry.tag.rsplit("}", 1)[-1] not in {"item", "entry"}:
                continue
            fetch_result['parse_metadata']['observed_entries'] += 1
            if len(candidates) >= limit:
                fetch_result['parse_metadata']['truncated'] = True
                continue
            fields, link = {}, ""
            for element in entry:
                tag = element.tag.rsplit("}", 1)[-1]
                value = "".join(element.itertext()).strip()
                fields.setdefault(tag, value)
                if tag == "link" and element.attrib.get("rel", "alternate") == "alternate":
                    link = element.attrib.get("href") or value
            candidates.append(dict(url=link or fields.get("link", ""), title=fields.get("title", ""), text=fields.get("description") or fields.get("summary") or fields.get("encoded") or fields.get("content", ""), published_at=_date(fields.get("pubDate") or fields.get("published") or fields.get("date")), updated_at=_date(fields.get("updated")), source_kind=source.get("document_kind", "news"), source_id=source_id))
    else:
        parser = _TextExtractor()
        parser.feed(body)
        parser.close()
        candidates.append(dict(url=base, title=" ".join(parser.title).strip()[:1000], text=parser.text, published_at=_date(parser.published_at), source_kind=kind, source_id=source_id))
    results, seen = [], set()
    for item in candidates:
        url = _public_document_url(str(item.get("url") or ""), base)
        if not url or url in seen:
            continue
        seen.add(url)
        item.update(url=url, title=extract_text(str(item.get("title") or ""), 1000), text=extract_text(str(item.get("text") or "")))
        results.append(item)
    return results
