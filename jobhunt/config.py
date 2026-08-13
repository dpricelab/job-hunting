"""Loading, validating and saving the user's YAML config."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from .models import Company

DEFAULT_RULES: dict = {
    "min_score": 1,
    "remote_only": False,
    "max_age_days": 0,  # 0 disables the age filter
    "title_include": [],
    "title_exclude": [],
    "location_include": [],
    "location_exclude": [],
    "exclude_keywords": [],
    "keywords": {},
    "title_bonus": 2,  # keyword weights are multiplied by this when hit in the title
}


class ConfigError(Exception):
    """Raised when the config file exists but cannot be used."""


def config_path() -> Path:
    return Path(os.environ.get("JOBHUNT_CONFIG", "config.yaml")).expanduser()


def data_dir() -> Path:
    path = Path(os.environ.get("JOBHUNT_DATA_DIR", "data")).expanduser()
    path.mkdir(parents=True, exist_ok=True)
    return path


@dataclass
class Config:
    companies: list[Company] = field(default_factory=list)
    rules: dict = field(default_factory=lambda: dict(DEFAULT_RULES))
    path: Path = field(default_factory=config_path)

    @classmethod
    def load(cls, path: Path | None = None) -> "Config":
        path = Path(path) if path else config_path()
        if not path.exists():
            return cls(path=path)

        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{path} is not valid YAML: {exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError(f"{path} should contain a YAML mapping at the top level")

        companies_raw = raw.get("companies") or []
        if not isinstance(companies_raw, list):
            raise ConfigError("'companies' must be a list")

        companies = []
        for entry in companies_raw:
            if isinstance(entry, str):
                entry = {"url": entry}
            if not isinstance(entry, dict):
                raise ConfigError(f"bad company entry: {entry!r}")
            company = Company.from_dict(entry)
            if not company.url:
                raise ConfigError(f"company entry is missing a url: {entry!r}")
            if not company.name:
                company.name = _name_from_url(company.url)
            companies.append(company)

        rules = dict(DEFAULT_RULES)
        user_rules = raw.get("rules") or {}
        if not isinstance(user_rules, dict):
            raise ConfigError("'rules' must be a mapping")
        rules.update(user_rules)
        _validate_rules(rules)

        return cls(companies=companies, rules=rules, path=path)

    def save(self, path: Path | None = None) -> Path:
        """Write the config back out.

        Note: this is a plain dump, so hand-written comments in the file are
        not preserved. Commands that mutate config say so before writing.
        """
        target = Path(path) if path else self.path
        payload = {
            "companies": [c.to_dict() for c in self.companies],
            "rules": self.rules,
        }
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
            encoding="utf-8",
        )
        return target

    def find(self, needle: str) -> Company | None:
        needle_l = needle.strip().lower()
        for company in self.companies:
            if company.name.lower() == needle_l or company.url.lower() == needle_l:
                return company
        for company in self.companies:
            if needle_l in company.name.lower():
                return company
        return None

    @property
    def active(self) -> list[Company]:
        return [c for c in self.companies if c.enabled]


def _validate_rules(rules: dict) -> None:
    keywords = rules.get("keywords") or {}
    if isinstance(keywords, list):
        # Allow a bare list of terms; every term is then worth one point.
        keywords = {str(term): 1 for term in keywords}
        rules["keywords"] = keywords
    if not isinstance(keywords, dict):
        raise ConfigError("'rules.keywords' must be a mapping of term -> weight")
    for term, weight in keywords.items():
        if not isinstance(weight, (int, float)):
            raise ConfigError(f"weight for keyword {term!r} must be a number")

    for key in (
        "title_include",
        "title_exclude",
        "location_include",
        "location_exclude",
        "exclude_keywords",
    ):
        value = rules.get(key) or []
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            raise ConfigError(f"'rules.{key}' must be a list")
        rules[key] = [str(v) for v in value]

    for key in ("min_score", "max_age_days", "title_bonus"):
        value = rules.get(key, 0)
        if not isinstance(value, (int, float)):
            raise ConfigError(f"'rules.{key}' must be a number")

    rules["remote_only"] = bool(rules.get("remote_only", False))


# On these boards the company is the first path segment (jobs.lever.co/acme),
# so the hostname itself says nothing about who is hiring.
_PATH_KEYED_HOSTS = (
    "greenhouse.io",
    "lever.co",
    "ashbyhq.com",
    "smartrecruiters.com",
    "workable.com",
)
_GENERIC_LABELS = {
    "boards",
    "job-boards",
    "jobs",
    "job",
    "apply",
    "careers",
    "career",
    "api",
    "www",
    "embed",
    "job_board",
    "com",
    "io",
    "co",
    "org",
    "net",
}


def _name_from_url(url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    parsed = urlparse(url if "//" in url else f"https://{url}")
    host = (parsed.netloc or "").split(":")[0].lower()
    path_parts = [p for p in parsed.path.split("/") if p]

    def prettify(value: str) -> str:
        return value.replace("-", " ").replace("_", " ").strip().title()

    if any(known in host for known in _PATH_KEYED_HOSTS):
        embedded = parse_qs(parsed.query).get("for")
        if embedded:
            return prettify(embedded[0])
        for segment in path_parts:
            if segment.lower() not in _GENERIC_LABELS:
                return prettify(segment)

    host_labels = [p for p in host.split(".") if p.lower() not in _GENERIC_LABELS]
    if host_labels:
        return prettify(host_labels[0])
    if path_parts:
        return prettify(path_parts[0])
    return host or url
