"""Source registry: map a careers URL to something that can read it."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from ..models import Company, Job
from . import ats, feeds, generic


@dataclass(frozen=True)
class SourceDef:
    name: str
    detect: Callable[[str], str | None]
    fetch: Callable[[str, str], list[Job]]
    confident: bool = True  # False means "best effort, may return junk"


# Order matters: specific vendors first, the catch-all last.
REGISTRY: tuple[SourceDef, ...] = (
    SourceDef("greenhouse", ats.detect_greenhouse, ats.fetch_greenhouse),
    SourceDef("lever", ats.detect_lever, ats.fetch_lever),
    SourceDef("ashby", ats.detect_ashby, ats.fetch_ashby),
    SourceDef("smartrecruiters", ats.detect_smartrecruiters, ats.fetch_smartrecruiters),
    SourceDef("recruitee", ats.detect_recruitee, ats.fetch_recruitee),
    SourceDef("workable", ats.detect_workable, ats.fetch_workable),
    SourceDef("personio", ats.detect_personio, ats.fetch_personio),
    SourceDef("rss", feeds.detect_feed, feeds.fetch_feed),
    SourceDef("generic", generic.detect_generic, generic.fetch_generic, confident=False),
)

BY_NAME = {source.name: source for source in REGISTRY}


class UnknownSource(Exception):
    pass


def get(name: str) -> SourceDef:
    try:
        return BY_NAME[name]
    except KeyError:
        raise UnknownSource(
            f"unknown source {name!r}; known sources: {', '.join(BY_NAME)}"
        ) from None


def detect(url: str) -> tuple[str, str]:
    """Pick a reader for a URL purely from its shape. Never fails —
    the generic reader claims anything left over."""
    for source in REGISTRY:
        token = source.detect(url)
        if token:
            return source.name, token
    return "generic", url


def resolve(url: str, sniff: bool = True) -> tuple[str, str, bool]:
    """Work out how to read a careers URL.

    When the URL alone only matches the generic fallback, optionally fetch the
    page and look for an embedded ATS board — this is what makes a plain
    ``https://example.com/careers`` URL usable.

    Returns (source_name, token, sniffed).
    """
    name, token = detect(url)
    if name != "generic" or not sniff:
        return name, token, False

    from ..http import HttpError, get_text

    try:
        html = get_text(url)
    except HttpError:
        return name, token, False

    found = ats.sniff_embedded_board(html)
    if found:
        sniffed_name, sniffed_token = found
        return sniffed_name, sniffed_token, True
    return name, token, False


def fetch_company(company: Company) -> list[Job]:
    """Read every posting for one configured company."""
    name = company.source
    token = company.token

    if name in ("", "auto"):
        name, token, _ = resolve(company.url)
    elif not token:
        source = get(name)
        token = source.detect(company.url) or company.url

    return get(name).fetch(token, company.name)
