"""Diagnostic: ask a SmartRecruiters board directly what it holds.

One token can cover a whole corporate group, so a source can be working
perfectly and still return nothing you want. This queries the board's own
search rather than filtering locally, which distinguishes "the division's
roles are not on this board" from "the division has nothing open".

    python tools/probe_smartrecruiters.py "Taylor & Francis"
    python tools/probe_smartrecruiters.py "Taylor & Francis" editor

With no search term it reports the board's size and the departments it
advertises. With one, it reports what the board itself finds.

Output is plain ASCII so it survives redirection on Windows.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt.config import Config  # noqa: E402
from jobhunt.http import HttpError, get_json  # noqa: E402

API = "https://api.smartrecruiters.com/v1/companies"
PAGE = 100
SHOW = 25


def fetch_all(token: str, query: str | None) -> tuple[int, list[dict]]:
    postings: list[dict] = []
    offset = 0
    total = 0

    while True:
        params: dict = {"limit": PAGE, "offset": offset}
        if query:
            params["q"] = query
        payload = get_json(f"{API}/{token}/postings", params=params)

        batch = payload.get("content") or []
        total = payload.get("totalFound") or 0
        postings.extend(batch)
        offset += len(batch)

        if not batch or offset >= total or offset >= 1000:
            break

    return total, postings


def main(argv: list[str]) -> int:
    if not argv:
        print('usage: python tools/probe_smartrecruiters.py "<company>" [search term]')
        return 2

    needle = argv[0]
    query = " ".join(argv[1:]) or None

    config = Config.load()
    company = config.find(needle)
    if company is None:
        print(f"No configured company matching {needle!r}. Known companies:")
        for entry in config.companies:
            print(f"  - {entry.name}  [{entry.source}]")
        return 1
    if company.source != "smartrecruiters":
        print(f"{company.name} is on '{company.source}', not smartrecruiters.")
        return 1

    print(f"Company : {company.name}")
    print(f"Token   : {company.token}")
    print(f"Search  : {query or '(everything)'}\n")

    try:
        total, postings = fetch_all(company.token, query)
    except HttpError as exc:
        print(f"REQUEST FAILED: {exc}")
        return 1

    print(f"Board reports {total} matching postings; {len(postings)} retrieved.\n")

    if not postings:
        print("Nothing came back. If a search term was given, the board itself")
        print("has no posting containing that word — which is the board's own")
        print("answer, not this tool filtering it out.")
        return 0

    departments: dict[str, int] = {}
    for entry in postings:
        label = (entry.get("department") or {}).get("label") or "(none)"
        departments[label] = departments.get(label, 0) + 1

    print("Departments the board advertises:")
    for label, count in sorted(departments.items(), key=lambda kv: -kv[1]):
        print(f"  {count:>4}  {label}")

    print(f"\nFirst {min(SHOW, len(postings))} postings:")
    for entry in postings[:SHOW]:
        location = entry.get("location") or {}
        where = ", ".join(
            str(part)
            for part in (location.get("city"), location.get("country"))
            if part
        )
        label = (entry.get("department") or {}).get("label") or "-"
        print(f"  {entry.get('name', '?')}")
        print(f"      {where or 'no location'}  |  {label}")

    if len(postings) > SHOW:
        print(f"  ... and {len(postings) - SHOW} more")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
