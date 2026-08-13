"""Core data shapes shared by sources, scoring and storage."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field

from .util import looks_remote, slugify


@dataclass
class Job:
    """One posting, normalized across every board we know how to read."""

    source: str
    company: str
    title: str
    url: str
    external_id: str = ""
    location: str = ""
    department: str = ""
    posted_at: str | None = None
    description: str = ""
    remote: bool = False

    def __post_init__(self) -> None:
        self.title = " ".join((self.title or "").split())
        self.company = " ".join((self.company or "").split())
        self.location = " ".join((self.location or "").split())
        self.department = " ".join((self.department or "").split())
        self.url = (self.url or "").strip()
        self.external_id = str(self.external_id or "").strip()
        if not self.remote:
            self.remote = looks_remote(self.location, self.title)

    @property
    def uid(self) -> str:
        """Stable identity across runs.

        Keyed on the board's own id when we have one, because posting URLs
        sometimes gain tracking parameters between fetches.
        """
        anchor = self.external_id or self.url
        raw = f"{self.source}:{slugify(self.company)}:{anchor}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]

    @property
    def haystack(self) -> str:
        """Everything scoring rules are allowed to match against."""
        return "\n".join(
            part
            for part in (self.title, self.location, self.department, self.description)
            if part
        ).lower()


@dataclass
class Company:
    """A configured target: where it is and how to read it."""

    name: str
    url: str
    source: str = "auto"
    token: str = ""
    enabled: bool = True
    # Extra query parameters for boards that support server-side filtering.
    # Useful when one token covers a whole group and you want one division.
    params: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        data = {"name": self.name, "url": self.url, "source": self.source}
        if self.token:
            data["token"] = self.token
        if self.params:
            data["params"] = dict(self.params)
        if not self.enabled:
            data["enabled"] = False
        return data

    @classmethod
    def from_dict(cls, data: dict) -> "Company":
        return cls(
            name=data.get("name") or "",
            url=data.get("url") or "",
            source=data.get("source") or "auto",
            token=data.get("token") or "",
            enabled=bool(data.get("enabled", True)),
            params=dict(data.get("params") or {}),
        )


@dataclass
class FetchResult:
    company: str
    jobs: list[Job] = field(default_factory=list)
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error
