"""RSS / Atom job feeds."""

from __future__ import annotations

import xml.etree.ElementTree as ET
from urllib.parse import urlparse

from ..http import HttpError, get_text
from ..models import Job
from ..util import html_to_text, to_iso

ATOM_NS = "{http://www.w3.org/2005/Atom}"
FEED_HINTS = ("/feed", "/rss", "/atom")


def detect_feed(url: str) -> str | None:
    parsed = urlparse(url if "//" in url else f"https://{url}")
    path = (parsed.path or "").lower()
    if path.endswith((".rss", ".atom", ".xml")):
        return url
    if any(hint in path for hint in FEED_HINTS):
        return url
    return None


def fetch_feed(url: str, company: str) -> list[Job]:
    text = get_text(url)
    try:
        root = ET.fromstring(text.strip())
    except ET.ParseError as exc:
        raise HttpError(f"{url} is not a parseable feed: {exc}") from exc

    jobs = _parse_rss(root, company) if root.tag.lower() == "rss" else []
    if not jobs:
        jobs = _parse_atom(root, company)
    if not jobs:
        jobs = _parse_rss(root, company)
    return jobs


def _parse_rss(root: ET.Element, company: str) -> list[Job]:
    jobs = []
    for item in root.iter("item"):
        title = _text(item, "title")
        link = _text(item, "link")
        if not title or not link:
            continue
        categories = [c.text.strip() for c in item.findall("category") if c is not None and c.text]
        jobs.append(
            Job(
                source="rss",
                company=company,
                external_id=_text(item, "guid") or link,
                title=title,
                url=link,
                location=_text(item, "location"),
                department=", ".join(categories),
                posted_at=to_iso(_text(item, "pubDate") or _text(item, "date")),
                description=html_to_text(_text(item, "description")),
            )
        )
    return jobs


def _parse_atom(root: ET.Element, company: str) -> list[Job]:
    jobs = []
    for entry in root.iter(f"{ATOM_NS}entry"):
        title = _text(entry, f"{ATOM_NS}title")
        link = ""
        for link_node in entry.findall(f"{ATOM_NS}link"):
            href = link_node.get("href")
            if href and link_node.get("rel", "alternate") == "alternate":
                link = href
                break
        if not title or not link:
            continue
        body = _text(entry, f"{ATOM_NS}content") or _text(entry, f"{ATOM_NS}summary")
        jobs.append(
            Job(
                source="rss",
                company=company,
                external_id=_text(entry, f"{ATOM_NS}id") or link,
                title=title,
                url=link,
                posted_at=to_iso(
                    _text(entry, f"{ATOM_NS}published") or _text(entry, f"{ATOM_NS}updated")
                ),
                description=html_to_text(body),
            )
        )
    return jobs


def _text(node: ET.Element, tag: str) -> str:
    found = node.find(tag)
    if found is None or found.text is None:
        return ""
    return found.text.strip()
