"""Diagnostic: show exactly what a Workday board returns, page by page.

Run when a Workday source looks like it is returning fewer roles than the
board really has:

    python tools/probe_workday.py Elsevier

It pages through the CXS jobs endpoint the same way the reader does, and
reports the reported total, what each page actually returned, and how many
distinct postings have been seen. If the unique count stops climbing while
pages keep arriving, the board is repeating itself and paging is the problem.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt.config import Config  # noqa: E402
from jobhunt.http import HttpError, post_json  # noqa: E402
from jobhunt.sources.ats import WORKDAY_PAGE_SIZE, WORKDAY_SCHEME  # noqa: E402

MAX_PAGES = 12


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python tools/probe_workday.py <company name>")
        return 2

    needle = " ".join(argv)
    config = Config.load()
    company = config.find(needle)

    if company is None:
        print(f"No configured company matching {needle!r}. Known companies:")
        for entry in config.companies:
            print(f"  - {entry.name}  [{entry.source}]")
        return 1

    if company.source != "workday":
        print(f"{company.name} is on '{company.source}', not workday. Nothing to probe.")
        return 1

    parts = company.token.split("/")
    host, tenant, site = parts[0], parts[1], parts[2]
    base = f"{WORKDAY_SCHEME}://{host}/wday/cxs/{tenant}/{site}"

    print(f"Company : {company.name}")
    print(f"Endpoint: {base}/jobs")
    print(f"Paging  : limit={WORKDAY_PAGE_SIZE}\n")

    seen: set[str] = set()
    offset = 0

    print(f"{'page':>4}  {'offset':>6}  {'returned':>8}  {'total':>6}  {'unique':>6}  note")
    print("-" * 62)

    for page in range(1, MAX_PAGES + 1):
        try:
            payload = post_json(
                f"{base}/jobs",
                {
                    "appliedFacets": {},
                    "limit": WORKDAY_PAGE_SIZE,
                    "offset": offset,
                    "searchText": "",
                },
            )
        except HttpError as exc:
            print(f"{page:>4}  {offset:>6}  request failed: {exc}")
            return 1

        batch = payload.get("jobPostings") or []
        total = payload.get("total")

        before = len(seen)
        for entry in batch:
            seen.add(entry.get("externalPath") or repr(entry))
        fresh = len(seen) - before

        note = ""
        if not batch:
            note = "empty page -> reader stops here"
        elif fresh == 0:
            note = "all repeats of earlier pages"
        elif fresh < len(batch):
            note = f"only {fresh} new"

        print(f"{page:>4}  {offset:>6}  {len(batch):>8}  {str(total):>6}  {len(seen):>6}  {note}")

        if not batch:
            break
        offset += len(batch)
        if total and offset >= total:
            print(f"\nReader would stop: offset {offset} >= reported total {total}")
            break
    else:
        print(f"\nStopped after {MAX_PAGES} pages (probe limit, not a board limit)")

    print(f"\nDistinct postings seen: {len(seen)}")

    if batch:
        print("\nFields present on the first posting of the last page:")
        for key, value in sorted(batch[0].items()):
            preview = str(value)
            if len(preview) > 70:
                preview = preview[:69] + "..."
            print(f"  {key:<22} {preview}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
