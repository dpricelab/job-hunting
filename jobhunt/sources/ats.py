"""Readers for the applicant-tracking systems most companies host jobs on.

Each reader turns a careers URL into a board token, then calls that vendor's
public job-board API. These are the documented, unauthenticated endpoints the
companies' own careers pages call from the browser — no scraping involved.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urlparse

from ..http import HttpError, get_json, get_text
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


def detect_greenhouse(url: str) -> str | None:
    parsed, host, segments = _parse(url)
    if "greenhouse.io" not in host:
        return None

    embedded = parse_qs(parsed.query).get("for")
    if embedded:
        return _clean_token(embedded[0])

    for segment in segments:
        if segment not in GREENHOUSE_SKIP:
            return _clean_token(segment)

    label = host.split(".")[0]
    if label not in ("boards", "job-boards", "www", "api", "boards-api"):
        return _clean_token(label)
    return None


def fetch_greenhouse(token: str, company: str) -> list[Job]:
    bases = (
        "https://boards-api.greenhouse.io/v1/boards",
        "https://boards-api.eu.greenhouse.io/v1/boards",
    )
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


def fetch_smartrecruiters(token: str, company: str) -> list[Job]:
    jobs = []
    offset = 0
    page_size = 100

    while True:
        payload = get_json(
            f"https://api.smartrecruiters.com/v1/companies/{token}/postings",
            params={"limit": page_size, "offset": offset},
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
    for name, pattern in EMBED_PATTERNS:
        for match in pattern.finditer(html or ""):
            token = match.group(1)
            if token and token.lower() not in _EMBED_NOISE:
                return name, token
    return None
