"""Small shared helpers: text cleanup, date coercion, slugs."""

from __future__ import annotations

import html
import re
import unicodedata
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

_TAG_RE = re.compile(r"<[^>]+>")
_BR_RE = re.compile(r"(?i)<br\s*/?>")
_BLOCK_RE = re.compile(
    r"(?i)</?(p|div|ul|ol|h[1-6]|tr|table|section|article|header|footer|blockquote)\b[^>]*>"
)
_LI_OPEN_RE = re.compile(r"(?i)<li\b[^>]*>")
_LI_CLOSE_RE = re.compile(r"(?i)</li\s*>")
_SCRIPT_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
_HSPACE_RE = re.compile(r"[ \t\r\f\v]+")
_MANY_NL_RE = re.compile(r"\n{3,}")

_REMOTE_HINTS = (
    "remote",
    "anywhere",
    "distributed",
    "work from home",
    "wfh",
    "home-based",
    "home based",
)


def html_to_text(raw: str | None) -> str:
    """Flatten an HTML (or entity-encoded HTML) blob into readable plain text.

    Some boards double-encode their description field, so entities are
    unescaped both before and after tag stripping.
    """
    if not raw:
        return ""
    s = html.unescape(str(raw))
    s = _SCRIPT_RE.sub(" ", s)
    s = _BR_RE.sub("\n", s)
    s = _LI_OPEN_RE.sub("\n• ", s)
    s = _LI_CLOSE_RE.sub("\n", s)
    s = _BLOCK_RE.sub("\n", s)
    # Remaining tags are inline (<b>, <em>, <a>…); dropping them outright keeps
    # "Build <b>things</b>." from becoming "Build things ."
    s = _TAG_RE.sub("", s)
    s = html.unescape(s)
    s = s.replace("\xa0", " ")
    s = _HSPACE_RE.sub(" ", s)
    s = "\n".join(line.strip() for line in s.split("\n"))
    s = _MANY_NL_RE.sub("\n\n", s)
    return s.strip()


def to_iso(value) -> str | None:
    """Coerce the many date shapes job boards emit into a UTC ISO-8601 string.

    Handles ISO strings, RFC-2822 strings, and epoch seconds or milliseconds.
    Returns None when the value cannot be understood.
    """
    if value is None or value == "":
        return None

    if isinstance(value, bool):
        return None

    if isinstance(value, (int, float)):
        seconds = float(value)
        if seconds > 1e11:  # milliseconds
            seconds /= 1000.0
        if seconds < 1e9:
            # Before September 2001: not a posting date but something else
            # numeric, such as a bare year, which would otherwise read as 1970.
            return None
        try:
            return datetime.fromtimestamp(seconds, timezone.utc).isoformat()
        except (OverflowError, OSError, ValueError):
            return None

    text = str(value).strip()
    if not text:
        return None

    if text.isdigit():
        if len(text) == 8:  # a compact date, 20260930
            try:
                return datetime.strptime(text, "%Y%m%d").replace(tzinfo=timezone.utc).isoformat()
            except ValueError:
                return None
        return to_iso(int(text))

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        dt = None
    if dt is None:
        try:
            dt = parsedate_to_datetime(text)
        except (TypeError, ValueError):
            return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).isoformat()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def slugify(text: str) -> str:
    normalized = unicodedata.normalize("NFKD", text or "")
    ascii_only = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_only).strip("-").lower()
    return slug or "unknown"


def looks_remote(*parts: str) -> bool:
    blob = " ".join(p for p in parts if p).lower()
    return any(hint in blob for hint in _REMOTE_HINTS)


def truncate(text: str, limit: int = 280) -> str:
    text = " ".join((text or "").split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


def human_age(iso: str | None) -> str:
    """Render an ISO timestamp as a compact relative age, e.g. '3d'."""
    if not iso:
        return ""
    try:
        dt = datetime.fromisoformat(iso)
    except ValueError:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = datetime.now(timezone.utc) - dt
    days = delta.days
    if days < 0:
        return "new"
    if days == 0:
        hours = delta.seconds // 3600
        return f"{hours}h" if hours else "now"
    if days < 30:
        return f"{days}d"
    if days < 365:
        return f"{days // 30}mo"
    return f"{days // 365}y"
