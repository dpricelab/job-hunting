"""Deterministic keyword + rule scoring.

Every posting is judged against the user's rules and either kept with a score
or rejected with a reason. Rejection reasons are preserved so `jobhunt list
--rejected` can explain why something did not show up.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import lru_cache

from .models import Job


@dataclass
class Verdict:
    score: float = 0.0
    matched: list[str] = field(default_factory=list)
    rejected: str | None = None

    @property
    def kept(self) -> bool:
        return self.rejected is None


@lru_cache(maxsize=1024)
def _pattern(term: str) -> re.Pattern:
    """Turn a config term into a matcher.

    Terms are matched case-insensitively on word boundaries so that "go" does
    not match "goal". Prefix a term with `re:` to supply a raw regex instead.
    """
    term = term.strip()
    if term.lower().startswith("re:"):
        return re.compile(term[3:], re.IGNORECASE)

    escaped = re.escape(term)
    prefix = r"\b" if term[:1].isalnum() else ""
    suffix = r"\b" if term[-1:].isalnum() else ""
    return re.compile(prefix + escaped + suffix, re.IGNORECASE)


def _first_match(terms, text: str) -> str | None:
    if not text:
        return None
    for term in terms or []:
        if _pattern(term).search(text):
            return term
    return None


def _age_days(posted_at: str | None) -> float | None:
    if not posted_at:
        return None
    try:
        dt = datetime.fromisoformat(posted_at)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - dt).total_seconds() / 86400.0


def score_job(job: Job, rules: dict) -> Verdict:
    """Score one job. Hard filters run first, then keyword weighting."""
    title = job.title or ""
    location = job.location or ""
    haystack = job.haystack

    hit = _first_match(rules.get("title_exclude"), title)
    if hit:
        return Verdict(rejected=f"title excludes {hit!r}")

    hit = _first_match(rules.get("exclude_keywords"), haystack)
    if hit:
        return Verdict(rejected=f"excluded keyword {hit!r}")

    title_include = rules.get("title_include") or []
    if title_include and not _first_match(title_include, title):
        return Verdict(rejected="title matched no title_include term")

    hit = _first_match(rules.get("location_exclude"), location)
    if hit:
        return Verdict(rejected=f"location excludes {hit!r}")

    location_include = rules.get("location_include") or []
    if location_include and not _first_match(location_include, location):
        return Verdict(rejected="location matched no location_include term")

    if rules.get("remote_only") and not job.remote:
        return Verdict(rejected="not remote")

    max_age = rules.get("max_age_days") or 0
    if max_age:
        age = _age_days(job.posted_at)
        if age is not None and age > max_age:
            return Verdict(rejected=f"older than {int(max_age)}d")

    keywords = rules.get("keywords") or {}
    title_bonus = rules.get("title_bonus", 2) or 1

    score = 0.0
    matched: list[str] = []
    for term, weight in keywords.items():
        pattern = _pattern(str(term))
        if not pattern.search(haystack):
            continue
        earned = float(weight)
        if pattern.search(title):
            earned *= float(title_bonus)
            matched.append(f"{term}*")
        else:
            matched.append(str(term))
        score += earned

    min_score = rules.get("min_score", 0) or 0
    if score < min_score:
        return Verdict(
            score=score,
            matched=matched,
            rejected=f"score {_fmt(score)} below min_score {_fmt(min_score)}",
        )

    return Verdict(score=score, matched=matched)


def _fmt(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:.1f}"
