"""Last-resort reader for careers pages that use no ATS we recognize.

Two strategies, best first:

1. schema.org ``JobPosting`` objects in JSON-LD. Plenty of careers pages embed
   these for Google Jobs, and they carry real structured data.
2. Anchor heuristics — links that look like postings. This is a guess, and the
   CLI labels it as low confidence, because a page rendered entirely in
   JavaScript will yield nothing useful.
"""

from __future__ import annotations

import json
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from ..http import get_text
from ..models import Job
from ..util import html_to_text, looks_remote, to_iso

JOB_URL_HINT = re.compile(r"(job|career|position|opening|vacanc|posting|role)", re.I)
NAV_TEXT = re.compile(
    r"^(all|view all|see all|apply|apply now|learn more|read more|back|next|previous|"
    r"home|about|contact|search|filter|sort|login|sign in|menu|careers?|jobs?|"
    r"openings?|open roles|vacancies|join us|work with us|see openings)$",
    re.I,
)
LDJSON_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)


def detect_generic(url: str) -> str | None:
    """The fallback always claims the URL — it is registered last."""
    return url


def fetch_generic(url: str, company: str) -> list[Job]:
    html = get_text(url)

    jobs = _from_jsonld(html, url, company)
    if jobs:
        return jobs
    return _from_anchors(html, url, company)


# -- schema.org JSON-LD ----------------------------------------------------


def _from_jsonld(html: str, page_url: str, company: str) -> list[Job]:
    jobs: list[Job] = []
    seen: set[str] = set()

    for block in LDJSON_RE.findall(html):
        try:
            payload = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        for node in _walk_jsonld(payload):
            job = _job_from_posting(node, page_url, company)
            if job and job.uid not in seen:
                seen.add(job.uid)
                jobs.append(job)

    return jobs


def _walk_jsonld(node):
    """Yield every JobPosting object nested anywhere in a JSON-LD payload."""
    if isinstance(node, list):
        for item in node:
            yield from _walk_jsonld(item)
        return
    if not isinstance(node, dict):
        return

    node_type = node.get("@type")
    types = node_type if isinstance(node_type, list) else [node_type]
    if any(str(t).lower() == "jobposting" for t in types if t):
        yield node

    for key in ("@graph", "itemListElement", "item", "mainEntity", "hasPart"):
        if key in node:
            yield from _walk_jsonld(node[key])


def _job_from_posting(node: dict, page_url: str, company: str) -> Job | None:
    title = node.get("title") or node.get("name") or ""
    if not title:
        return None

    url = node.get("url") or node.get("sameAs") or ""
    if isinstance(url, list):
        url = url[0] if url else ""
    url = urljoin(page_url, str(url)) if url else page_url

    org = node.get("hiringOrganization")
    org_name = org.get("name") if isinstance(org, dict) else (org if isinstance(org, str) else "")

    identifier = node.get("identifier")
    if isinstance(identifier, dict):
        identifier = identifier.get("value") or identifier.get("name") or ""
    external_id = str(identifier or "").strip()

    location = _location_text(node.get("jobLocation"))
    remote = str(node.get("jobLocationType") or "").upper() == "TELECOMMUTE"

    department = node.get("occupationalCategory") or node.get("industry") or ""
    if isinstance(department, list):
        department = ", ".join(str(d) for d in department)

    return Job(
        source="jsonld",
        company=company or str(org_name or ""),
        external_id=external_id or url,
        title=str(title),
        url=url,
        location=location,
        department=str(department or ""),
        posted_at=to_iso(node.get("datePosted")),
        description=html_to_text(node.get("description")),
        remote=remote or looks_remote(location),
    )


def _location_text(node) -> str:
    if not node:
        return ""
    if isinstance(node, list):
        parts = [_location_text(item) for item in node]
        return "; ".join(p for p in parts if p)
    if isinstance(node, str):
        return node
    if not isinstance(node, dict):
        return ""

    if node.get("name") and not node.get("address"):
        return str(node["name"])

    address = node.get("address")
    if isinstance(address, str):
        return address
    if isinstance(address, dict):
        parts = [
            address.get("addressLocality"),
            address.get("addressRegion"),
            address.get("addressCountry"),
        ]
        flat = []
        for part in parts:
            if isinstance(part, dict):
                part = part.get("name")
            if part:
                flat.append(str(part))
        return ", ".join(flat)
    return ""


# -- anchor heuristics -----------------------------------------------------


class _AnchorCollector(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._buffer: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag != "a":
            return
        self._flush()
        self._href = dict(attrs).get("href")
        self._buffer = []

    def handle_endtag(self, tag):
        if tag == "a":
            self._flush()

    def handle_data(self, data):
        if self._href is not None:
            self._buffer.append(data)

    def _flush(self):
        if self._href:
            text = " ".join("".join(self._buffer).split())
            if text:
                self.anchors.append((self._href, text))
        self._href = None
        self._buffer = []


def _from_anchors(html: str, page_url: str, company: str) -> list[Job]:
    collector = _AnchorCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # malformed markup should not kill the run
        pass

    page_host = urlparse(page_url).netloc
    page_key = page_url.rstrip("/")
    jobs: list[Job] = []
    seen: set[str] = set()

    for href, text in collector.anchors:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        if NAV_TEXT.match(text) or not (3 <= len(text) <= 120):
            continue

        absolute = urljoin(page_url, href)
        host = urlparse(absolute).netloc
        if not JOB_URL_HINT.search(absolute):
            continue
        # A link back to the careers page itself is navigation, not a posting.
        if absolute.rstrip("/") == page_key:
            continue
        # Only same-site links, or links into a known ATS host.
        if host != page_host and not re.search(
            r"(greenhouse|lever|ashby|workable|recruitee|smartrecruiters|personio)", host, re.I
        ):
            continue
        if absolute in seen:
            continue
        seen.add(absolute)

        jobs.append(
            Job(
                source="generic",
                company=company,
                external_id=absolute,
                title=text,
                url=absolute,
                description="",
            )
        )

    return jobs
