"""Readers for the applicant-tracking systems most companies host jobs on.

Each reader turns a careers URL into a board token, then calls that vendor's
public job-board API. These are the documented, unauthenticated endpoints the
companies' own careers pages call from the browser — no scraping involved.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

from ..http import HttpError, get_json, get_text, post_json
from ..models import Job
from ..util import html_to_text, looks_remote, to_iso


def _parse(url: str):
    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.netloc or "").split(":")[0].lower()
    segments = [s for s in parsed.path.split("/") if s]
    return parsed, host, segments


def _clean_token(token: str | None) -> str | None:
    if not token:
        return None
    token = token.strip().strip("/")
    return token or None


# --------------------------------------------------------------------------
# Greenhouse
# --------------------------------------------------------------------------

GREENHOUSE_SKIP = {"embed", "job_board", "jobs", "job"}

# Greenhouse runs a separate EU instance. Tokens are namespaced per region, so
# an "eu:" prefix records which one a URL named and the right API is tried
# first — the other is still tried as a fallback.
GREENHOUSE_EU_PREFIX = "eu:"
GREENHOUSE_US_API = "https://boards-api.greenhouse.io/v1/boards"
GREENHOUSE_EU_API = "https://boards-api.eu.greenhouse.io/v1/boards"


def detect_greenhouse(url: str) -> str | None:
    parsed, host, segments = _parse(url)
    if "greenhouse.io" not in host:
        return None

    prefix = GREENHOUSE_EU_PREFIX if ".eu.greenhouse.io" in host else ""

    embedded = parse_qs(parsed.query).get("for")
    if embedded:
        token = _clean_token(embedded[0])
        return prefix + token if token else None

    for segment in segments:
        if segment not in GREENHOUSE_SKIP:
            token = _clean_token(segment)
            return prefix + token if token else None

    label = host.split(".")[0]
    if label not in ("boards", "job-boards", "www", "api", "boards-api"):
        token = _clean_token(label)
        return prefix + token if token else None
    return None


def fetch_greenhouse(token: str, company: str) -> list[Job]:
    if token.startswith(GREENHOUSE_EU_PREFIX):
        token = token[len(GREENHOUSE_EU_PREFIX) :]
        bases = (GREENHOUSE_EU_API, GREENHOUSE_US_API)
    else:
        bases = (GREENHOUSE_US_API, GREENHOUSE_EU_API)

    payload = None
    last_error: Exception | None = None
    for base in bases:
        try:
            payload = get_json(f"{base}/{token}/jobs", params={"content": "true"})
            break
        except HttpError as exc:
            last_error = exc
    if payload is None:
        raise last_error or HttpError(f"greenhouse board {token!r} not found")

    jobs = []
    for entry in payload.get("jobs") or []:
        location = (entry.get("location") or {}).get("name") or ""
        departments = [
            d.get("name", "") for d in (entry.get("departments") or []) if d.get("name")
        ]
        offices = [o.get("name", "") for o in (entry.get("offices") or []) if o.get("name")]
        jobs.append(
            Job(
                source="greenhouse",
                company=company,
                external_id=str(entry.get("id") or ""),
                title=entry.get("title") or "",
                url=entry.get("absolute_url") or "",
                location=location or ", ".join(offices),
                department=", ".join(departments),
                posted_at=to_iso(entry.get("first_published") or entry.get("updated_at")),
                description=html_to_text(entry.get("content")),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Lever
# --------------------------------------------------------------------------


def detect_lever(url: str) -> str | None:
    _, host, segments = _parse(url)
    if "lever.co" not in host:
        return None
    return _clean_token(segments[0]) if segments else None


def fetch_lever(token: str, company: str) -> list[Job]:
    payload = get_json(f"https://api.lever.co/v0/postings/{token}", params={"mode": "json"})
    jobs = []
    for entry in payload or []:
        categories = entry.get("categories") or {}
        location = categories.get("location") or ""
        workplace = entry.get("workplaceType") or ""
        jobs.append(
            Job(
                source="lever",
                company=company,
                external_id=str(entry.get("id") or ""),
                title=entry.get("text") or "",
                url=entry.get("hostedUrl") or entry.get("applyUrl") or "",
                location=location,
                department=categories.get("team") or categories.get("department") or "",
                posted_at=to_iso(entry.get("createdAt")),
                description=entry.get("descriptionPlain")
                or html_to_text(entry.get("description")),
                remote=workplace.lower() == "remote" or looks_remote(location),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Ashby
# --------------------------------------------------------------------------


def detect_ashby(url: str) -> str | None:
    _, host, segments = _parse(url)
    if "ashbyhq.com" not in host:
        return None
    return _clean_token(segments[0]) if segments else None


def fetch_ashby(token: str, company: str) -> list[Job]:
    payload = get_json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    jobs = []
    for entry in payload.get("jobs") or []:
        jobs.append(
            Job(
                source="ashby",
                company=company,
                external_id=str(entry.get("id") or ""),
                title=entry.get("title") or "",
                url=entry.get("jobUrl") or entry.get("applyUrl") or "",
                location=entry.get("location") or "",
                department=entry.get("department") or entry.get("team") or "",
                posted_at=to_iso(entry.get("publishedAt") or entry.get("updatedAt")),
                description=entry.get("descriptionPlain")
                or html_to_text(entry.get("descriptionHtml")),
                remote=bool(entry.get("isRemote")),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# SmartRecruiters
# --------------------------------------------------------------------------


def detect_smartrecruiters(url: str) -> str | None:
    _, host, segments = _parse(url)
    if "smartrecruiters.com" not in host:
        return None
    return _clean_token(segments[0]) if segments else None


def fetch_smartrecruiters(token: str, company: str, params: dict | None = None) -> list[Job]:
    """Read a SmartRecruiters board.

    `params` passes through the API's own filters — `department`, `q`, `city`,
    `country`, `custom_field.<id>` — which matters when one token covers a
    whole group and you only want one division of it.
    """
    jobs = []
    offset = 0
    page_size = 100

    while True:
        query = {str(k): v for k, v in (params or {}).items()}
        query.update({"limit": page_size, "offset": offset})
        payload = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            params=query,
        )
        batch = payload.get("content") or []
        for entry in batch:
            location = entry.get("location") or {}
            parts = [location.get("city"), location.get("region"), location.get("country")]
            jobs.append(
                Job(
                    source="smartrecruiters",
                    company=company,
                    external_id=str(entry.get("id") or ""),
                    title=entry.get("name") or "",
                    url=f"https://jobs.smartrecruiters.com/{token}/{entry.get('id')}",
                    location=", ".join(p for p in parts if p),
                    department=(entry.get("department") or {}).get("label") or "",
                    posted_at=to_iso(entry.get("releasedDate") or entry.get("createdOn")),
                    description="",
                    remote=bool(location.get("remote")),
                )
            )

        offset += len(batch)
        total = payload.get("totalFound") or 0
        if not batch or offset >= total:
            break

    return jobs


# --------------------------------------------------------------------------
# Recruitee
# --------------------------------------------------------------------------


def detect_recruitee(url: str) -> str | None:
    _, host, _ = _parse(url)
    if not host.endswith("recruitee.com"):
        return None
    label = host.split(".")[0]
    return _clean_token(label) if label not in ("www", "api") else None


def fetch_recruitee(token: str, company: str) -> list[Job]:
    payload = get_json(f"https://{token}.recruitee.com/api/offers/")
    jobs = []
    for entry in payload.get("offers") or []:
        parts = [entry.get("city"), entry.get("country")]
        location = entry.get("location") or ", ".join(p for p in parts if p)
        jobs.append(
            Job(
                source="recruitee",
                company=company,
                external_id=str(entry.get("id") or ""),
                title=entry.get("title") or "",
                url=entry.get("careers_url") or entry.get("careers_apply_url") or "",
                location=location,
                department=entry.get("department") or "",
                posted_at=to_iso(entry.get("published_at") or entry.get("created_at")),
                description=html_to_text(entry.get("description")),
                remote=bool(entry.get("remote")),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Workable
# --------------------------------------------------------------------------


def detect_workable(url: str) -> str | None:
    _, host, segments = _parse(url)
    if "workable.com" not in host:
        return None
    if host.startswith("apply.") or host.startswith("jobs."):
        return _clean_token(segments[0]) if segments else None
    label = host.split(".")[0]
    return _clean_token(label) if label not in ("www", "api") else None


def fetch_workable(token: str, company: str) -> list[Job]:
    payload = get_json(
        f"https://apply.workable.com/api/v1/widget/accounts/{token}",
        params={"details": "true"},
    )
    jobs = []
    for entry in payload.get("jobs") or []:
        parts = [entry.get("city"), entry.get("state"), entry.get("country")]
        shortcode = entry.get("shortcode") or ""
        jobs.append(
            Job(
                source="workable",
                company=company,
                external_id=str(shortcode or entry.get("id") or ""),
                title=entry.get("title") or "",
                url=entry.get("url")
                or entry.get("application_url")
                or f"https://apply.workable.com/{token}/j/{shortcode}/",
                location=", ".join(p for p in parts if p),
                department=entry.get("department") or "",
                posted_at=to_iso(entry.get("published_on") or entry.get("created_at")),
                description=html_to_text(entry.get("description")),
                remote=bool(entry.get("telecommuting")),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Personio (XML feed)
# --------------------------------------------------------------------------


def detect_personio(url: str) -> str | None:
    _, host, _ = _parse(url)
    if "jobs.personio." not in host:
        return None
    return _clean_token(host.split(".")[0])


def fetch_personio(token: str, company: str) -> list[Job]:
    import xml.etree.ElementTree as ET

    tld = "de"
    text = ""
    last_error: Exception | None = None
    for candidate in ("de", "com"):
        try:
            text = get_text(f"https://{token}.jobs.personio.{candidate}/xml")
            tld = candidate
            break
        except HttpError as exc:
            last_error = exc
    if not text:
        raise last_error or HttpError(f"personio board {token!r} not found")

    try:
        root = ET.fromstring(text)
    except ET.ParseError as exc:
        raise HttpError(f"personio feed for {token!r} was not valid XML: {exc}") from exc

    jobs = []
    for position in root.iter("position"):
        job_id = _xml_text(position, "id")
        descriptions = [
            _xml_text(node, "value") for node in position.iter("jobDescription")
        ]
        location = _xml_text(position, "office")
        jobs.append(
            Job(
                source="personio",
                company=company,
                external_id=job_id,
                title=_xml_text(position, "name"),
                url=f"https://{token}.jobs.personio.{tld}/job/{job_id}",
                location=location,
                department=_xml_text(position, "department"),
                posted_at=to_iso(_xml_text(position, "createdAt")),
                description=html_to_text("\n".join(d for d in descriptions if d)),
            )
        )
    return jobs


# --------------------------------------------------------------------------
# Workday
# --------------------------------------------------------------------------

WORKDAY_HOST_RE = re.compile(r"^(?P<tenant>[A-Za-z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com$")
WORKDAY_URL_RE = re.compile(
    r"https?://[A-Za-z0-9_-]+\.wd\d+\.myworkdayjobs\.com/[A-Za-z0-9_/-]*", re.I
)
LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Za-z]{2})?$")

# Workday's search endpoint caps a page at 20 regardless of what you ask for.
WORKDAY_PAGE_SIZE = 20
WORKDAY_MAX_JOBS = 2000
# Descriptions come from a per-posting call, so scoring on body text costs one
# request per job. Bounded, and run concurrently to keep a fetch reasonable.
WORKDAY_DETAIL_WORKERS = 8
WORKDAY_MAX_DETAILS = 400

_POSTED_AGO_RE = re.compile(r"(\d+)\+?\s*(hour|day|week|month)s?\s+ago", re.I)

# bulletFields[0] is usually the requisition id, but not every tenant plays
# along: some put a location string there and some omit the field entirely.
# Only values shaped like a requisition id are trusted as an identity.
_REQ_ID_RE = re.compile(r"^[A-Za-z]{0,6}[-_ ]?\d{3,}$")

# Listings sometimes summarise rather than name a place ("2 Locations"), in
# which case the per-posting detail carries the real one.
_VAGUE_LOCATION_RE = re.compile(r"^\d+\s+locations?$", re.I)

WORKDAY_DEFAULT_LOCALE = "en-US"

# Real boards are always HTTPS; tests point this at a local HTTP mock.
WORKDAY_SCHEME = "https"


def detect_workday(url: str) -> str | None:
    """Return "host/tenant/site" (plus "/locale" when the URL named one).

    That is everything the CXS API and the public job URLs need.
    """
    parsed, host, segments = _parse(url)
    match = WORKDAY_HOST_RE.match(host)
    if not match:
        return None

    tenant = match.group("tenant")

    # An API URL already names both parts: /wday/cxs/<tenant>/<site>/jobs
    lowered = [s.lower() for s in segments]
    if "cxs" in lowered:
        index = lowered.index("cxs")
        if len(segments) > index + 2:
            return f"{host}/{segments[index + 1]}/{segments[index + 2]}"

    # Otherwise the site is the first non-locale path segment, and a locale
    # segment ahead of it is worth keeping for building job URLs later.
    locale = ""
    for segment in segments:
        if LOCALE_RE.match(segment):
            locale = locale or segment
            continue
        return f"{host}/{tenant}/{segment}" + (f"/{locale}" if locale else "")
    return None


def fetch_workday(token: str, company: str) -> list[Job]:
    parts = token.split("/")
    if len(parts) < 3 or not all(parts[:3]):
        raise HttpError(
            f"malformed workday token {token!r}; expected 'host/tenant/site[/locale]'"
        )

    host, tenant, site = parts[0], parts[1], parts[2]
    locale = parts[3] if len(parts) > 3 and parts[3] else WORKDAY_DEFAULT_LOCALE

    base = f"{WORKDAY_SCHEME}://{host}/wday/cxs/{tenant}/{site}"
    postings: list[dict] = []
    seen_keys: set[str] = set()
    offset = 0
    total: int | None = None

    while offset < WORKDAY_MAX_JOBS:
        payload = post_json(
            f"{base}/jobs",
            {
                "appliedFacets": {},
                "limit": WORKDAY_PAGE_SIZE,
                "offset": offset,
                "searchText": "",
            },
        )
        batch = payload.get("jobPostings") or []
        if not batch:
            break

        # Workday reports the total on the first page only; every later page
        # carries zero. Reading it each time ends paging after two pages, so
        # keep the first real figure and judge against that.
        reported = payload.get("total") or 0
        if reported and total is None:
            total = reported

        fresh = []
        for entry in batch:
            key = entry.get("externalPath") or (
                f"{entry.get('title')}|{entry.get('locationsText')}"
            )
            if key not in seen_keys:
                seen_keys.add(key)
                fresh.append(entry)

        # Past the end, some boards serve their first page again rather than
        # an empty one. Without this the loop would run to its hard limit.
        if not fresh:
            break

        postings.extend(fresh)
        offset += len(batch)
        if total and offset >= total:
            break

    pairs = [(entry, _workday_job(entry, host, site, locale, company)) for entry in postings]
    jobs = [job for entry, job in pairs if job.title]
    _attach_workday_descriptions(
        [(entry, job) for entry, job in pairs if job.title], base
    )
    return jobs


def _workday_external_id(entry: dict, external_path: str) -> str:
    """Pick a stable, unique identity for a posting.

    bulletFields[0] is the requisition id on most tenants, but some put a
    location string there — trusting that blindly would give every job in a
    city the same identity and collapse them into one record. Anything that
    is not requisition-shaped falls back to externalPath, which is unique.
    """
    bullets = entry.get("bulletFields") or []
    if bullets:
        candidate = str(bullets[0]).strip()
        if _REQ_ID_RE.match(candidate):
            return candidate
    return external_path


def _workday_job(entry: dict, host: str, site: str, locale: str, company: str) -> Job:
    external_path = entry.get("externalPath") or ""
    location = entry.get("locationsText") or ""
    if _VAGUE_LOCATION_RE.match(location.strip()):
        location = ""  # the detail call supplies a real one

    return Job(
        source="workday",
        company=company,
        external_id=_workday_external_id(entry, external_path),
        title=entry.get("title") or "",
        url=(
            f"{WORKDAY_SCHEME}://{host}/{locale}/{site}{external_path}"
            if external_path
            else f"{WORKDAY_SCHEME}://{host}/{locale}/{site}"
        ),
        location=location,
        posted_at=_workday_posted_at(entry.get("postedOn")),
        description="",
        remote=looks_remote(location, entry.get("title") or ""),
    )


def _attach_workday_descriptions(pairs: list[tuple[dict, Job]], base: str) -> None:
    """Fill in each posting's body text, which the list endpoint omits.

    Failures are swallowed per job: a missing description weakens scoring for
    that one role, which beats losing the whole board.
    """
    targets = [
        (job, entry["externalPath"]) for entry, job in pairs if entry.get("externalPath")
    ][:WORKDAY_MAX_DETAILS]
    if not targets:
        return

    def load(pair) -> None:
        job, path = pair
        try:
            # One retry only: the listing already succeeded, and hammering a
            # sick detail endpoint once per job is not worth the wall time.
            payload = get_json(f"{base}{path}", retries=2)
        except HttpError:
            return
        info = payload.get("jobPostingInfo") or {}
        job.description = html_to_text(info.get("jobDescription"))
        if not job.location and info.get("location"):
            job.location = " ".join(str(info["location"]).split())
        if info.get("remoteType"):
            job.remote = job.remote or "remote" in str(info["remoteType"]).lower()
        job.remote = job.remote or looks_remote(job.location)
        precise = to_iso(info.get("startDate") or info.get("postedOn"))
        if precise:
            job.posted_at = precise
        job.deadline = to_iso(info.get("endDate") or info.get("jobPostingEndDate"))

    with ThreadPoolExecutor(max_workers=WORKDAY_DETAIL_WORKERS) as pool:
        list(pool.map(load, targets))


def _workday_posted_at(text: str | None) -> str | None:
    """Turn Workday's relative wording into a timestamp.

    The list endpoint reports "Posted 5 Days Ago" rather than a date, so this
    is approximate by nature — good enough for the max_age_days filter.
    """
    if not text:
        return None

    lowered = str(text).strip().lower()
    now = datetime.now(timezone.utc)

    if "today" in lowered or "just posted" in lowered:
        return now.isoformat()
    if "yesterday" in lowered:
        return (now - timedelta(days=1)).isoformat()

    match = _POSTED_AGO_RE.search(lowered)
    if not match:
        return to_iso(text)

    count = int(match.group(1))
    unit = match.group(2)
    delta = {
        "hour": timedelta(hours=count),
        "day": timedelta(days=count),
        "week": timedelta(weeks=count),
        "month": timedelta(days=30 * count),
    }[unit]
    return (now - delta).isoformat()


def _xml_text(node, tag: str) -> str:
    found = node.find(tag)
    if found is None or found.text is None:
        return ""
    return found.text.strip()


# --------------------------------------------------------------------------
# Sniffing an ATS out of a company's own careers page
# --------------------------------------------------------------------------

EMBED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"boards-api\.(?:eu\.)?greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)", re.I)),
    ("greenhouse", re.compile(r"(?:job-)?boards\.(?:eu\.)?greenhouse\.io/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9_-]+)", re.I)),
    ("lever", re.compile(r"api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)", re.I)),
    ("ashby", re.compile(r"(?:jobs\.ashbyhq\.com|posting-api/job-board)/([A-Za-z0-9_.-]+)", re.I)),
    ("smartrecruiters", re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)", re.I)),
    ("recruitee", re.compile(r"([A-Za-z0-9_-]+)\.recruitee\.com", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/(?:api/v1/widget/accounts/)?([A-Za-z0-9_-]+)", re.I)),
    ("personio", re.compile(r"([A-Za-z0-9_-]+)\.jobs\.personio\.(?:de|com)", re.I)),
]

_EMBED_NOISE = {"embed", "job_board", "js", "api", "www", "assets", "static"}


def sniff_embedded_board(html: str) -> tuple[str, str] | None:
    """Find an ATS board embedded in a company's own careers page markup.

    Returns (source_name, token), or None when nothing recognizable is there.
    """
    # Workday first: its token needs the host as well as the path, so it is
    # rebuilt from the whole matched URL rather than a single capture group.
    for match in WORKDAY_URL_RE.finditer(html or ""):
        token = detect_workday(match.group(0))
        if token:
            return "workday", token

    for name, pattern in EMBED_PATTERNS:
        for match in pattern.finditer(html or ""):
            token = match.group(1)
            if token and token.lower() not in _EMBED_NOISE:
                return name, token
    return None
