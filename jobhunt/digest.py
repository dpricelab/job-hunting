"""Choosing what belongs in the digest.

Shared by the file that `jobhunt digest` writes and the page `jobhunt serve`
hands out, so the two cannot disagree about what you are looking at.
"""

from __future__ import annotations

from .store import Store


def collect(
    store: Store,
    *,
    show_all: bool = False,
    min_score: float | None = None,
    company: str | None = None,
    since: str | None = None,
):
    """Return (rows, subtitle, last_digest_at).

    Untriaged and still open by default: a role stays until you mark it or the
    employer withdraws it, never merely because time passed.
    """
    last_digest = store.meta_get("last_digest_at")

    rows = store.query(
        rejected=False,
        status=None if show_all else "new",
        min_score=min_score,
        company=company,
        since=since,
    )

    scope = "everything matching, triaged or not" if show_all else (
        "open roles you have not triaged yet"
    )
    subtitle = f"{len(rows)} {'role' if len(rows) == 1 else 'roles'} · {scope}"

    fresh = sum(1 for row in rows if last_digest and row["first_seen"] > last_digest)
    if fresh:
        subtitle += f" · {fresh} new since last time"

    return rows, subtitle, last_digest
