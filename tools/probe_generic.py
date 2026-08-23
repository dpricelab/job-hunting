"""Diagnostic: show why the generic reader found what it found on a page.

Run when a generic source returns nothing, or far fewer roles than the page
appears to list:

    python tools/probe_generic.py "Royal Society"

The generic reader tries structured data first and falls back to guessing at
links, so a zero can mean several different things: the page was not
reachable, it carries no structured data and no matching links, or it is a
JavaScript shell whose listings never appear in the HTML at all. This reports
which, and shows the links it considered and why each was dropped.

Output is deliberately plain ASCII so it survives a redirect on Windows.
"""

from __future__ import annotations

import sys
from html import unescape
from pathlib import Path
from urllib.parse import urljoin, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from jobhunt.config import Config  # noqa: E402
from jobhunt.http import HttpError, get  # noqa: E402
from jobhunt.sources import generic  # noqa: E402

SHOW = 12


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: python tools/probe_generic.py <company name>")
        return 2

    needle = " ".join(argv)
    config = Config.load()
    company = config.find(needle)

    if company is None:
        print(f"No configured company matching {needle!r}. Known companies:")
        for entry in config.companies:
            print(f"  - {entry.name}  [{entry.source}]")
        return 1

    url = company.url
    print(f"Company : {company.name}")
    print(f"URL     : {url}")
    print(f"Source  : {company.source}")
    if company.source != "generic":
        print(
            f"  note: this company reads via '{company.source}', not the page itself,\n"
            "        so what follows describes the page rather than what it collects."
        )
    print()

    try:
        response = get(url)
    except HttpError as exc:
        print(f"FETCH FAILED: {exc}")
        print("\nThe page could not be read at all, so nothing else applies.")
        return 1

    html = response.text
    print(f"HTTP {response.status_code}, {len(html):,} bytes of HTML")
    print(f"Content-Type: {response.headers.get('Content-Type', 'unknown')}\n")

    # -- structured data --------------------------------------------------
    blocks = generic.LDJSON_RE.findall(html)
    postings = generic._from_jsonld(html, url, company.name)
    print("STRUCTURED DATA (schema.org JobPosting, the preferred path)")
    print(f"  ld+json script blocks found : {len(blocks)}")
    print(f"  JobPosting entries extracted: {len(postings)}")
    for job in postings[:SHOW]:
        print(f"    - {job.title}  [{job.location or 'no location'}]")
    if len(postings) > SHOW:
        print(f"    ... and {len(postings) - SHOW} more")
    print()

    # -- embedded application state ---------------------------------------
    # A page can be server-rendered yet still hide its listings inside a JSON
    # blob meant for a JavaScript framework. That looks identical to an empty
    # page from the link heuristics, but is very much readable.
    # Patterns come from the reader itself, so probe and reader cannot drift.
    print("EMBEDDED APPLICATION STATE (listings hidden in a JSON payload)")
    found_state = False
    for label, payload in generic.find_embedded_state(html):
        found_state = True
        print(f"  {label}: parsed")
        break
    if not found_state:
        print("  none found")

    embedded = generic._from_embedded_state(html, url, company.name)
    print(f"  postings extracted from it: {len(embedded)}")
    for job in embedded[:SHOW]:
        print(f"    - {job.title}  [{job.location or 'no location'}]  {job.url}")
    if len(embedded) > SHOW:
        print(f"    ... and {len(embedded) - SHOW} more")

    # Every site names its fields differently, and the reader maps them by
    # guessing. Show the real ones so a wrong guess is obvious.
    raw_best: list[dict] = []
    for _label, payload in generic.find_embedded_state(html):
        for candidate in generic._candidate_arrays(payload):
            if len(candidate) > len(raw_best):
                raw_best = candidate

    if raw_best:
        print("\n  raw fields on the first record:")
        for key, value in sorted(raw_best[0].items()):
            preview = str(value)
            if len(preview) > 70:
                preview = preview[:69] + "..."
            print(f"    {key:<24} {preview}")
        if len(raw_best) > 1:
            print("\n  same fields on the second record, to show what varies:")
            for key, value in sorted(raw_best[1].items()):
                preview = str(value)
                if len(preview) > 70:
                    preview = preview[:69] + "..."
                print(f"    {key:<24} {preview}")
    print()

    # -- link heuristics --------------------------------------------------
    collector = generic._AnchorCollector()
    try:
        collector.feed(html)
        collector.close()
    except Exception as exc:  # noqa: BLE001 - diagnostic, report anything
        print(f"  (HTML parsing raised {type(exc).__name__}: {exc})")

    page_host = urlparse(url).netloc
    page_key = url.rstrip("/")

    reasons: dict[str, int] = {}
    kept: list[tuple[str, str]] = []

    for href, text in collector.anchors:
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            reason = "not a page link"
        elif generic.NAV_TEXT.match(text):
            reason = "link text looks like navigation"
        elif not (3 <= len(text) <= 120):
            reason = "link text too short or too long"
        else:
            absolute = urljoin(url, href)
            if not generic.JOB_URL_HINT.search(absolute):
                reason = "URL has no job-like word in it"
            elif generic.JOB_URL_ANTI_HINT.search(absolute):
                reason = "URL matches an excluded page (policy, privacy, ...)"
            elif absolute.rstrip("/") == page_key:
                reason = "link points back at this page"
            elif urlparse(absolute).netloc != page_host and not generic.re.search(
                r"(greenhouse|lever|ashby|workable|recruitee|smartrecruiters|personio)",
                urlparse(absolute).netloc,
                generic.re.I,
            ):
                reason = "link leaves this site"
            else:
                kept.append((text, absolute))
                continue
        reasons[reason] = reasons.get(reason, 0) + 1

    print("LINK HEURISTICS (fallback, used only when there is no structured data)")
    print(f"  links on the page: {len(collector.anchors)}")
    print(f"  treated as jobs  : {len(kept)}")
    for text, absolute in kept[:SHOW]:
        print(f"    - {text}  ->  {absolute}")
    if len(kept) > SHOW:
        print(f"    ... and {len(kept) - SHOW} more")

    if reasons:
        print("\n  dropped, by reason:")
        for reason, count in sorted(reasons.items(), key=lambda kv: -kv[1]):
            print(f"    {count:>4}  {reason}")

    # -- verdict ----------------------------------------------------------
    print("\nVERDICT")
    if postings:
        print("  Structured data was found and is what the reader uses.")
    elif embedded:
        print("  The listings come from the embedded JSON payload above.")
    elif found_state:
        print("  There is an embedded payload, but no job-like records were")
        print("  recognised inside it. Its field names may be unusual.")
    elif kept:
        print("  No structured data; the reader is guessing from links above.")
    elif len(collector.anchors) < 10 and len(html) < 60000:
        print("  Almost no links and a small page: this is very likely a")
        print("  JavaScript shell whose listings are loaded after page load.")
        print("  The HTML the reader sees genuinely does not contain the jobs.")
    else:
        print("  Links exist but none look like postings. Check the dropped")
        print("  reasons above; the URL pattern may simply be unusual.")

    head = " ".join(html[:400].split())
    print(f"\nFirst 400 characters of the HTML:\n  {head}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
