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
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

from ..http import get_text
from ..models import Job
from ..util import html_to_text, looks_remote, to_iso

JOB_URL_HINT = re.compile(
    # Agencies and search firms rarely say "job": they advertise assignments,
    # appointments or opportunities.
    r"(job|career|position|opening|vacanc|posting|role|assignment|appointment|opportunit)",
    re.I,
)

# Pages that match the hint above but are never a posting. "Equal
# opportunities" is the notable trap, since it contains "opportunit".
JOB_URL_ANTI_HINT = re.compile(
    r"(equal[-_ ]?opportunit|privacy|cookie|terms[-_ ]|/terms|policy|disclaimer|"
    r"accessibility|sitemap|newsletter|subscribe|"
    # Agency site furniture that reads like a posting: Redwood's "Your career"
    # and "Brief us on a role" were being collected as jobs.
    r"submit[-_ ]?a[-_ ]?(vacancy|job|role)|your[-_ ]?career|post[-_ ]?a[-_ ]?(job|role)|"
    # Job boards carry accounts, advice and recruiter sections on job-like
    # paths. Volunteer listings are not paid roles.
    r"/account|/signin|/sign-in|/login|/register|/recruiters?|career[-_ ]?advice|"
    r"/volunteer|/saved|/alerts?)",
    re.I,
)

# Hosts that are somebody else's job board, worth following off-site.
ATS_HOST_RE = re.compile(
    r"(greenhouse|lever|ashby|workable|recruitee|smartrecruiters|personio|myworkdayjobs)",
    re.I,
)
NAV_TEXT = re.compile(
    r"^(all|view all|see all|apply|apply now|learn more|read more|back|next|previous|"
    r"home|about|contact|search|filter|sort|login|sign in|menu|careers?|jobs?|"
    r"openings?|open roles|vacancies|join us|work with us|see openings|"
    # Aggregator boards put these alongside the listings themselves.
    r"submit a job|post a job|advertise|advertise a job|advertise with us|"
    r"jobs board|job board|careers hub)$",
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

    # Server-rendered pages can still keep their listings in a JSON payload
    # meant for a JavaScript framework, where no amount of link-guessing will
    # find them. Redwood's 255KB page carries 22 links and 20 vacancies.
    jobs = _from_embedded_state(html, url, company)
    if jobs:
        return jobs

    return _from_anchors(html, url, company)


# -- embedded application state -------------------------------------------

# (label, pattern, entity_encoded). Inertia stores its payload in an HTML
# attribute, so that one arrives escaped.
EMBEDDED_STATE_PATTERNS: list[tuple[str, re.Pattern, bool]] = [
    ("Inertia.js (data-page)", re.compile(r'data-page="([^"]{200,})"'), True),
    (
        "Next.js (__NEXT_DATA__)",
        re.compile(r'id="__NEXT_DATA__"[^>]*>(.{200,}?)</script>', re.DOTALL),
        False,
    ),
    (
        "Nuxt (window.__NUXT__)",
        re.compile(r"window\.__NUXT__\s*=\s*(.{200,}?)</script>", re.DOTALL),
        False,
    ),
    (
        "Remix (__remixContext)",
        re.compile(r"window\.__remixContext\s*=\s*(.{200,}?)</script>", re.DOTALL),
        False,
    ),
    (
        "Generic application/json",
        re.compile(
            r'<script[^>]+type=["\']application/json["\'][^>]*>(.{200,}?)</script>',
            re.DOTALL | re.IGNORECASE,
        ),
        False,
    ),
]

TITLE_KEYS = ("title", "job_title", "jobTitle", "position", "role_name", "name")
URL_KEYS = ("url", "link", "permalink", "apply_url", "job_url", "href")
SLUG_KEYS = ("shortcode", "slug", "path", "reference", "ref")
ID_KEYS = ("id", "uuid", "job_id", "shortcode", "reference", "ref", "slug")

# An opaque token rather than anything a person wrote: Redwood stores its
# locations as codes like "ERSQLYRF4M" that point at a record elsewhere in the
# payload. Blanking one that cannot be resolved is deliberate — an unknown
# location still passes the location rules, whereas a code never matches
# anywhere and would silently bin the posting.
OPAQUE_CODE_RE = re.compile(r"^[A-Z0-9]{6,16}$")
LOCATION_KEYS = ("location", "city", "town", "region", "place", "office", "locations")
DESCRIPTION_KEYS = ("description", "summary", "excerpt", "content", "body", "intro")
DATE_KEYS = ("published_at", "posted_at", "created_at", "date", "live_from")
# Redwood's payload calls it publish_until; other sites use other names.
DEADLINE_KEYS = (
    "publish_until",
    "closing_date",
    "closes_at",
    "deadline",
    "valid_through",
    "validThrough",
    "expires_at",
    "end_date",
    "apply_by",
)


def find_embedded_state(html: str):
    """Yield (label, parsed) for every embedded payload that parses as JSON."""
    for label, pattern, encoded in EMBEDDED_STATE_PATTERNS:
        for match in pattern.finditer(html or ""):
            raw = match.group(1)
            if encoded:
                raw = unescape(raw)
            try:
                yield label, json.loads(raw.strip().rstrip(";"))
            except (json.JSONDecodeError, ValueError):
                continue  # Nuxt and Remix sometimes emit JS, not JSON


def _has_detail(entry: dict) -> bool:
    return any(
        _first_value(entry, keys) is not None
        for keys in (LOCATION_KEYS, DESCRIPTION_KEYS, DATE_KEYS, DEADLINE_KEYS, DETAIL_KEYS)
    )


def _array_score(entries: list[dict]) -> tuple[int, int]:
    """Rank candidate arrays by how posting-like they are, then by size.

    Size alone is not enough: a lookup table of locations carries a name and
    an identifier on every row, so it qualifies as job-like and can be longer
    than the listings it serves. Real postings carry detail — a description,
    a date, a salary — that a lookup row does not.
    """
    return (sum(1 for entry in entries if _has_detail(entry)), len(entries))


def _from_embedded_state(html: str, page_url: str, company: str) -> list[Job]:
    best: list[dict] = []
    best_score = (0, 0)
    index: dict[str, str] = {}

    for _label, payload in find_embedded_state(html):
        for candidate in _candidate_arrays(payload):
            score = _array_score(candidate)
            if score > best_score:
                best_score = score
                best = candidate
                index = _reference_index(payload)

    return [_job_from_mapping(entry, page_url, company, index) for entry in best]


def _reference_index(payload, depth: int = 0, index: dict | None = None) -> dict[str, str]:
    """Map opaque codes to the names they stand for.

    Payloads often normalise their data, storing a location as a code and the
    readable name in a separate list. This collects any record carrying both
    so those references can be resolved.
    """
    if index is None:
        index = {}
    if depth > 8:
        return index

    if isinstance(payload, list):
        for item in payload:
            _reference_index(item, depth + 1, index)
    elif isinstance(payload, dict):
        name = ""
        for key in ("name", "title", "label", "city"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                name = value.strip()
                break
        if name:
            for key in ("shortcode", "code", "id", "uuid", "slug", "reference"):
                value = payload.get(key)
                if isinstance(value, str) and OPAQUE_CODE_RE.match(value):
                    index.setdefault(value, name)
        for value in payload.values():
            _reference_index(value, depth + 1, index)

    return index


def _candidate_arrays(node, depth: int = 0):
    """Yield lists that look like a set of job postings.

    Being strict matters more than being thorough: a menu of links also has
    "title" on every item, so an entry must carry an identifier of some kind
    and not read like navigation.
    """
    if depth > 8:
        return
    if isinstance(node, list):
        entries = [item for item in node if isinstance(item, dict) and _looks_like_job(item)]
        if len(entries) >= 2:
            yield entries
        for item in node:
            yield from _candidate_arrays(item, depth + 1)
    elif isinstance(node, dict):
        for value in node.values():
            yield from _candidate_arrays(value, depth + 1)


DETAIL_KEYS = (
    "department",
    "team",
    "category",
    "sector",
    "salary",
    "contract_type",
    "employment_type",
    "closing_date",
)


def _looks_like_job(entry: dict) -> bool:
    """Does this record read like a posting rather than a menu item?

    A title plus either an identifier or some posting-ish detail. Kept
    deliberately loose: a stray menu entry that slips through is rejected by
    the title rules moments later, whereas a real vacancy that fails this
    test is lost silently.
    """
    title = _first_value(entry, TITLE_KEYS)
    if not isinstance(title, str) or not (3 <= len(title.strip()) <= 120):
        return False
    if NAV_TEXT.match(title.strip()):
        return False

    has_identity = any(
        isinstance(_first_value(entry, keys), (str, int))
        for keys in (URL_KEYS, SLUG_KEYS, ID_KEYS)
    )
    has_detail = any(
        _first_value(entry, keys) is not None
        for keys in (LOCATION_KEYS, DESCRIPTION_KEYS, DATE_KEYS, DEADLINE_KEYS, DETAIL_KEYS)
    )
    return has_identity or has_detail


def _first_value(entry: dict, keys):
    for key in keys:
        if key in entry and entry[key] not in (None, ""):
            return entry[key]
    return None


def _flatten(value) -> str:
    """Locations arrive as a string, an object, or a list of either."""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        for key in ("name", "title", "label", "city"):
            if value.get(key):
                return str(value[key])
        return ""
    if isinstance(value, list):
        parts = [_flatten(item) for item in value]
        return ", ".join(p for p in parts if p)
    return str(value)


def _job_from_mapping(
    entry: dict, page_url: str, company: str, index: dict[str, str] | None = None
) -> Job:
    title = str(_first_value(entry, TITLE_KEYS) or "").strip()

    url = _first_value(entry, URL_KEYS)
    if isinstance(url, str) and url:
        absolute = urljoin(page_url, url)
    else:
        slug = _first_value(entry, SLUG_KEYS)
        # Postings usually live beneath the listing page, so join to its path
        # rather than replacing it.
        absolute = urljoin(page_url.rstrip("/") + "/", str(slug)) if slug else page_url

    identifier = _first_value(entry, ID_KEYS)

    location = _flatten(_first_value(entry, LOCATION_KEYS)).strip()
    if OPAQUE_CODE_RE.match(location):
        location = (index or {}).get(location, "")

    return Job(
        source="embedded",
        company=company,
        external_id=str(identifier) if identifier is not None else absolute,
        title=title,
        url=absolute,
        location=location,
        department=_flatten(_first_value(entry, ("department", "team", "category", "sector"))),
        posted_at=to_iso(_first_value(entry, DATE_KEYS)),
        deadline=to_iso(_first_value(entry, DEADLINE_KEYS)),
        description=html_to_text(_flatten(_first_value(entry, DESCRIPTION_KEYS))),
    )


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
        deadline=to_iso(node.get("validThrough")),
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


def link_rejection(href: str, text: str, page_url: str) -> tuple[str | None, str]:
    """Why an anchor is not a posting, or None if it might be one.

    Shared with the probe, so what it reports and what the reader does cannot
    drift apart.
    """
    if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
        return "not a page link", ""
    if NAV_TEXT.match(text):
        return "link text looks like navigation", ""
    if not (3 <= len(text) <= 120):
        return "link text too short or too long", ""

    absolute = urljoin(page_url, href)
    parsed = urlparse(absolute)

    # Match the path, never the host. charityjob.co.uk contains "job", so
    # matching the whole URL made all 259 links on the page look like
    # postings, "Log in" and "Career advice" among them.
    path = parsed.path + (f"?{parsed.query}" if parsed.query else "")

    if not JOB_URL_HINT.search(path):
        return "URL has no job-like word in its path", absolute
    if JOB_URL_ANTI_HINT.search(path):
        return "URL is an excluded page (account, advice, policy, ...)", absolute
    if absolute.rstrip("/") == page_url.rstrip("/"):
        return "link points back at this page", absolute
    if parsed.netloc != urlparse(page_url).netloc and not ATS_HOST_RE.search(parsed.netloc):
        return "link leaves this site", absolute

    return None, absolute


def _from_anchors(html: str, page_url: str, company: str) -> list[Job]:
    collector = _AnchorCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception:  # malformed markup should not kill the run
        pass

    jobs: list[Job] = []
    seen: set[str] = set()

    for href, text in collector.anchors:
        reason, absolute = link_rejection(href, text, page_url)
        if reason or absolute in seen:
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
