"""Command line interface for jobhunt."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, sources
from .config import Config, ConfigError, data_dir
from .digest import collect
from .http import HttpError
from .models import Company, FetchResult, Job
from .render import render_digest
from .scoring import score_job
from .store import USER_STATUSES, Store
from .util import human_age, now_iso, truncate

STARTER_CONFIG = """# jobhunt configuration
#
# companies: the career pages you want watched. Add them with
#     jobhunt add <url>
# which works out which job board the page uses and fills in the details.
companies: []

# rules: how a posting earns its way into your digest.
rules:
  # A posting must reach this score to be kept.
  min_score: 3

  # Keyword weights. Matching is case-insensitive and respects word
  # boundaries, so "go" will not match "goal". Prefix a term with "re:" to
  # use a regular expression instead.
  keywords:
    python: 3
    typescript: 2
    kubernetes: 2
    aws: 1
    "machine learning": 3
    postgres: 1

  # A keyword found in the job title is worth this multiple of its weight.
  title_bonus: 2

  # If set, the title must contain at least one of these.
  title_include: []

  # Hard rejections.
  title_exclude:
    - intern
    - internship
    - graduate
    - "vice president"
  exclude_keywords: []

  # Location filters, matched against the posting's location field.
  location_include: []
  location_exclude: []

  # Keep only postings flagged remote.
  remote_only: false

  # Ignore postings older than this many days. 0 disables the filter.
  max_age_days: 0
"""


# -- terminal helpers ------------------------------------------------------


# Set by _configure_output, so the decision is made once, after the console
# has had a chance to be put into a mode that understands colour.
_COLOR = {"enabled": False}


def _enable_ansi() -> bool:
    """Ask a Windows console to interpret colour codes.

    Windows consoles understand ANSI escapes but do not act on them unless
    virtual terminal processing is switched on. Without this, cmd.exe prints
    the escape sequences literally, which is worse than no colour at all.
    Everywhere else there is nothing to do.
    """
    if os.name != "nt":
        return True

    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False

        enable_vt = 0x0004
        if mode.value & enable_vt:
            return True
        return bool(kernel32.SetConsoleMode(handle, mode.value | enable_vt))
    except Exception:  # noqa: BLE001 - any failure just means no colour
        return False


def _use_color() -> bool:
    return _COLOR["enabled"]


def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _use_color() else text


def bold(t: str) -> str:
    return _c(t, "1")


def dim(t: str) -> str:
    return _c(t, "2")


def green(t: str) -> str:
    return _c(t, "32")


def red(t: str) -> str:
    return _c(t, "31")


def yellow(t: str) -> str:
    return _c(t, "33")


# Output symbols. Windows falls back to a legacy code page when stdout is
# redirected to a file, and cannot encode any of the nicer characters, so an
# ASCII set stands in there rather than crashing the run.
_FANCY = {
    "ok": "✓",
    "fail": "✗",
    "skip": "–",
    "warn": "!",
    "dot": "·",
    "dash": "—",
    "arrow": "→",
    "ellipsis": "…",
    "bullet": "•",
    "rule": "─",
}
_ASCII = {
    "ok": "OK",
    "fail": "X",
    "skip": "-",
    "warn": "!",
    "dot": "|",
    "dash": "-",
    "arrow": "->",
    "ellipsis": "...",
    "bullet": "*",
    "rule": "-",
}

# Mutated in place by _configure_output so callers can read it at call time.
SYM = dict(_FANCY)


def _configure_output() -> None:
    """Make printing safe whatever the terminal encoding is.

    Two separate problems: characters this CLI prints deliberately, handled by
    swapping in the ASCII set, and characters it cannot predict — an accented
    name in a job title — handled by replacing rather than raising.
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, ValueError, OSError):
            pass

    # Colour only where it will actually be rendered: not into a file, not
    # when NO_COLOR is set, and on Windows only once the console agrees to
    # interpret the codes rather than print them.
    try:
        is_terminal = sys.stdout.isatty()
    except (AttributeError, ValueError):
        is_terminal = False
    _COLOR["enabled"] = (
        is_terminal and not os.environ.get("NO_COLOR") and _enable_ansi()
    )

    encoding = getattr(sys.stdout, "encoding", None) or "ascii"
    try:
        "".join(_FANCY.values()).encode(encoding)
    except (UnicodeEncodeError, LookupError):
        SYM.update(_ASCII)
    else:
        # Set rather than merely leave alone, so the result reflects the
        # current stream whatever it was set to before.
        SYM.update(_FANCY)


def _fmt_score(value) -> str:
    value = float(value or 0)
    return str(int(value)) if value.is_integer() else f"{value:.1f}"


def _table(headers: list[str], rows: list[list[str]], max_widths: list[int] | None = None) -> str:
    if not rows:
        return ""
    widths = [len(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], len(cell))
    if max_widths:
        widths = [min(w, m) if m else w for w, m in zip(widths, max_widths)]

    def line(cells: list[str], style=lambda s: s) -> str:
        out = []
        for i, cell in enumerate(cells):
            clipped = cell if len(cell) <= widths[i] else cell[: widths[i] - 1] + SYM["ellipsis"]
            out.append(style(clipped.ljust(widths[i])))
        return "  ".join(out).rstrip()

    body = [line(headers, bold)]
    body.extend(line(row) for row in rows)
    return "\n".join(body)


def _store(args) -> Store:
    base = Path(args.data_dir) if args.data_dir else data_dir()
    base.mkdir(parents=True, exist_ok=True)
    return Store(base / "jobs.db")


def _load_config(args) -> Config:
    return Config.load(Path(args.config) if args.config else None)


def _row_to_job(row) -> Job:
    return Job(
        source=row["source"],
        company=row["company"],
        title=row["title"],
        url=row["url"],
        external_id=row["external_id"] or "",
        location=row["location"] or "",
        department=row["department"] or "",
        posted_at=row["posted_at"],
        description=row["description"] or "",
        remote=bool(row["remote"]),
    )


# -- commands --------------------------------------------------------------


def cmd_init(args) -> int:
    path = Path(args.config) if args.config else Config().path
    if path.exists() and not args.force:
        print(f"{path} already exists. Use --force to overwrite.")
        return 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER_CONFIG, encoding="utf-8")
    print(f"Wrote {green(str(path))}")
    print("\nNext: add the companies you care about, e.g.")
    print(dim("  jobhunt add https://www.example.com/careers"))
    return 0


def _normalize_url(url: str) -> str:
    """Drop the fragment: it never reaches the server, so it is noise here."""
    return (url or "").strip().split("#", 1)[0]


def _facets_from_url(url: str) -> dict:
    """Read Workday filters out of a URL copied from the browser.

    Filtering a board on the site puts opaque facet ids in the query string,
    such as ``?locationCountry=29247e57...``. Keeping them means one
    country's roles are read from a board that lists a whole multinational,
    rather than every posting worldwide.
    """
    from urllib.parse import parse_qs, urlparse

    query = parse_qs(urlparse(url).query)
    facets: dict = {}
    for key, values in query.items():
        if key == "q":
            # The board's own search box; Workday takes it as searchText.
            if values and values[0].strip():
                facets["searchText"] = values[0].strip()
            continue
        # Facet ids are 32 hex digits. Anything else in a copied URL is
        # tracking (?source=LinkedIn, utm_…), and sent as a facet it would
        # filter the board down to nothing.
        clean = [v for v in values if _WORKDAY_FACET_RE.match(v)]
        if not clean:
            continue
        facets[key] = clean if len(clean) > 1 else clean[0]
    return facets


_WORKDAY_FACET_RE = re.compile(r"^[0-9a-f]{32}$", re.I)


def cmd_add(args) -> int:
    config = _load_config(args)
    args.url = _normalize_url(args.url)

    for existing in config.companies:
        if existing.url.rstrip("/") == args.url.rstrip("/"):
            print(f"{yellow('Already configured')}: {existing.name} ({existing.url})")
            return 1

    if args.source:
        source_name = args.source
        token = sources.get(source_name).detect(args.url) or args.url
        sniffed = False
    else:
        print(dim(f"Inspecting {args.url} {SYM['ellipsis']}"))
        source_name, token, sniffed = sources.resolve(args.url, sniff=not args.no_sniff)

    name = args.name or _suggest_name(args.url, token)
    params = _facets_from_url(args.url) if source_name == "workday" else {}
    company = Company(
        name=name, url=args.url, source=source_name, token=token, params=params
    )

    note = " (found embedded in the page)" if sniffed else ""
    print(f"Board: {bold(source_name)} {SYM['dot']} token {bold(token)}{note}")
    if params:
        print(f"Filter: {', '.join(f'{k}={v}' for k, v in params.items())}")
        print(dim("  (taken from the URL; only roles matching it will be fetched)"))

    if source_name == "generic":
        print(
            yellow(
                "No known job board detected. Falling back to reading the page itself, "
                "which is best-effort - check the results below."
            )
        )

    if not args.no_check:
        try:
            jobs = sources.fetch_company(company)
        except (HttpError, sources.UnknownSource) as exc:
            print(red(f"Could not read that board: {exc}"))
            print(dim("Add it anyway with --no-check, or pass --source to override."))
            return 1

        print(f"Found {bold(str(len(jobs)))} open roles.")
        for job in jobs[:5]:
            location = f" {SYM['dash']} {job.location}" if job.location else ""
            print(dim(f"  {SYM['bullet']} {truncate(job.title, 70)}{location}"))
        if len(jobs) > 5:
            print(dim(f"  {SYM['ellipsis']} and {len(jobs) - 5} more"))
        if not jobs:
            print(
                yellow(
                    "That board returned nothing. It may genuinely have no openings, "
                    "or the page may render its jobs with JavaScript."
                )
            )

    config.companies.append(company)
    config.save()
    print(f"\nAdded {green(name)} to {config.path}.")
    print(dim("Note: saving rewrites the config file, so YAML comments are not preserved."))
    return 0


def _suggest_name(url: str, token: str) -> str:
    """Best guess at a display name.

    A token is only a usable name when it is a bare slug — Workday tokens are
    host/tenant/site triples and RSS tokens are whole URLs, both of which make
    nonsense names. Those fall back to reading the URL.
    """
    from .config import _name_from_url, _prettify

    if token and not re.search(r"[/.:]", token):
        return _prettify(token) or token.title()
    return _name_from_url(url)


def cmd_remove(args) -> int:
    """Stop watching a company.

    Refuses an ambiguous name rather than guessing, since one company's name
    is often a prefix of another's.
    """
    config = _load_config(args)
    needle = args.company.strip().lower()

    exact = [
        c for c in config.companies if c.name.lower() == needle or c.url.lower() == needle
    ]
    matches = exact or [c for c in config.companies if needle in c.name.lower()]

    if not matches:
        print(red(f"No configured company matching {args.company!r}"))
        return 1
    if len(matches) > 1:
        print(yellow(f"{args.company!r} matches more than one company:"))
        for company in matches:
            print(f"  - {company.name}")
        print(dim("Give the full name to say which."))
        return 1

    company = matches[0]
    config.companies.remove(company)
    config.save()
    print(f"Removed {green(company.name)} from {config.path}.")

    with _store(args) as store:
        stored = store.count_for_company(company.name)
        if args.purge:
            deleted = store.delete_company(company.name)
            print(f"Deleted {deleted} collected postings.")
        elif stored:
            print(
                dim(
                    f"{stored} collected postings are still in the database and will "
                    "no longer be refreshed. Re-run with --purge to delete them."
                )
            )
    return 0


def cmd_companies(args) -> int:
    config = _load_config(args)
    if not config.companies:
        print("No companies configured yet. Add one with:")
        print(dim("  jobhunt add https://boards.greenhouse.io/example"))
        return 0

    with _store(args) as store:
        known = {row["company"]: row for row in store.companies()}

    rows = []
    for company in config.companies:
        stats = known.get(company.name)
        rows.append(
            [
                company.name,
                company.source,
                company.token or "-",
                str(stats["matching"] or 0) if stats else "-",
                str(stats["total"] or 0) if stats else "-",
                "" if company.enabled else "disabled",
            ]
        )

    print(_table(["COMPANY", "BOARD", "TOKEN", "MATCH", "SEEN", ""], rows, [28, 16, 24, 6, 6, 10]))
    return 0


def cmd_resolve(args) -> int:
    """Re-detect the board behind a configured URL.

    Useful when a company was added without network access to sniff its page,
    and when an employer migrates to a different ATS.
    """
    config = _load_config(args)

    if args.company:
        match = config.find(args.company)
        if not match:
            print(red(f"No configured company matching {args.company!r}"))
            return 1
        targets = [match]
    else:
        targets = config.active
        if not args.all:
            targets = [c for c in targets if c.source in ("", "auto", "generic")]

    if not targets:
        print("Nothing to resolve - every company already has a known board.")
        print(dim("Use --all to re-check them anyway."))
        return 0

    changed = 0
    for company in targets:
        before = (company.source, company.token)
        name, token, sniffed = sources.resolve(company.url, sniff=True)

        if name == "generic":
            # `resolve` also reports generic when the page could not be
            # fetched, so never downgrade a company that already has a working
            # board — a network blip would otherwise wipe the whole config.
            if before[0] in ("", "auto", "generic"):
                print(f"  {yellow(SYM['skip'])} {company.name}: no board found, still generic")
                print(
                    dim(
                        "     The page may render its jobs with JavaScript, or it may "
                        "not have been reachable. Look for a 'view all jobs' link and "
                        "add that URL instead."
                    )
                )
            else:
                print(
                    f"  {yellow(SYM['warn'])} {company.name}: could not confirm its "
                    f"{before[0]} board just now; leaving it as is"
                )
            continue

        if (name, token) != before:
            company.source, company.token = name, token
            changed += 1
            note = " (found embedded in the page)" if sniffed else ""
            origin = before[0] or "auto"
            print(
                f"  {green(SYM['ok'])} {company.name}: {origin} {SYM['arrow']} "
                f"{bold(name)} {SYM['dot']} {token}{note}"
            )
        else:
            print(f"  {dim(SYM['skip'])} {company.name}: unchanged ({name})")

    if changed:
        config.save()
        print(f"\nUpdated {changed} of {len(targets)} in {config.path}.")
        print(dim("Next: jobhunt fetch"))
    else:
        print(f"\nNo changes to {config.path}.")
    return 0


def cmd_fetch(args) -> int:
    config = _load_config(args)
    targets = config.active
    if args.company:
        match = config.find(args.company)
        if not match:
            print(red(f"No configured company matching {args.company!r}"))
            return 1
        targets = [match]

    if not targets:
        print("Nothing to fetch. Add a company first with `jobhunt add <url>`.")
        return 1

    def run(company: Company) -> FetchResult:
        try:
            return FetchResult(company.name, sources.fetch_company(company))
        except (HttpError, sources.UnknownSource) as exc:
            return FetchResult(company.name, [], str(exc))
        except Exception as exc:  # a bad board should not sink the whole run
            return FetchResult(company.name, [], f"{type(exc).__name__}: {exc}")

    print(
        f"Fetching {len(targets)} {'board' if len(targets) == 1 else 'boards'} "
        f"{SYM['ellipsis']}\n"
    )
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        results = list(pool.map(run, targets))

    total_new = total_matching = failures = 0

    with _store(args) as store:
        for result in results:
            if not result.ok:
                failures += 1
                print(f"  {red(SYM['fail'])} {result.company}: {result.error}")
                continue

            # Workday reads each body with a separate call, and one that fails
            # leaves the text blank. Scoring that blank would quietly reject a
            # role that matched yesterday, so reuse the text already stored.
            known = store.descriptions([job.uid for job in result.jobs if not job.description])
            for job in result.jobs:
                if not job.description and known.get(job.uid):
                    job.description = known[job.uid]

            scored = [(job, score_job(job, config.rules)) for job in result.jobs]
            kept = [v for _, v in scored if v.kept]

            # A board that answers with nothing at all is far more often a
            # hiccup (a maintenance page, a changed layout) than an employer
            # withdrawing every role at once. Closing on it would empty the
            # company from the digest, so leave what is known as it is.
            if not result.jobs and store.count_open(result.company):
                print(
                    f"  {yellow(SYM['warn'])} {result.company}: no roles returned "
                    f"{SYM['dash']} keeping the ones already listed open"
                )
                continue

            stats = store.upsert(scored, close_missing_for=result.company)

            total_new += stats.new
            total_matching += len(kept)

            detail = f"{len(result.jobs)} roles, {len(kept)} match"
            if stats.new:
                detail += f", {green(str(stats.new) + ' new')}"
            if stats.closed:
                detail += dim(f", {stats.closed} closed")
            print(f"  {green(SYM['ok'])} {result.company}: {detail}")

        store.meta_set("last_fetch_at", now_iso())

    print(
        f"\n{bold('Done.')} {total_new} newly seen, {total_matching} matching your rules"
        + (f", {red(str(failures) + ' failed')}" if failures else "")
    )
    if total_new:
        print(dim("Next: jobhunt digest --open"))
    return 1 if failures and failures == len(results) else 0


def cmd_list(args) -> int:
    with _store(args) as store:
        rows = store.query(
            status=args.status,
            company=args.company,
            min_score=args.min_score,
            rejected=args.rejected or bool(args.reason),
            reason=args.reason,
            include_closed=args.include_closed,
            limit=args.limit,
        )

        if not rows:
            print("Nothing to show. Run `jobhunt fetch` first, or relax the filters.")
            return 0

        table_rows = []
        for row in rows:
            reason = row["rejected"] or ""
            table_rows.append(
                [
                    row["uid"],
                    _fmt_score(row["score"]),
                    row["company"],
                    row["title"],
                    row["location"] or ("Remote" if row["remote"] else ""),
                    human_age(row["posted_at"]),
                    reason if (args.rejected or args.reason) else row["status"],
                ]
            )

        header_last = "WHY" if (args.rejected or args.reason) else "STATUS"
        print(
            _table(
                ["ID", "SCORE", "COMPANY", "TITLE", "LOCATION", "AGE", header_last],
                table_rows,
                [12, 5, 22, 46, 24, 5, 40],
            )
        )
        print(dim(f"\n{len(rows)} shown {SYM['dot']} open one with `jobhunt show <id>`"))
    return 0


def cmd_show(args) -> int:
    with _store(args) as store:
        matches = store.resolve(args.job_id)
        if not matches:
            print(red(f"No job with id starting {args.job_id!r}"))
            return 1
        if len(matches) > 1:
            print(yellow(f"{len(matches)} jobs match that prefix:"))
            for row in matches:
                print(f"  {row['uid']}  {row['company']} {SYM['dash']} {row['title']}")
            return 1

        row = matches[0]
        try:
            matched = json.loads(row["matched"] or "[]")
        except ValueError:
            matched = []

        print(bold(row["title"]))
        print(f"{row['company']}  {SYM['dot']}  {row['location'] or 'location not stated'}")
        print(row["url"])
        print()
        print(f"{'Score':<12} {_fmt_score(row['score'])}  ({', '.join(matched) or 'no keyword hits'})")
        print(f"{'Status':<12} {row['status']}")
        print(f"{'Board':<12} {row['source']}")
        if row["department"]:
            print(f"{'Team':<12} {row['department']}")
        if row["posted_at"]:
            print(f"{'Posted':<12} {row['posted_at'][:10]} ({human_age(row['posted_at'])} ago)")
        print(f"{'First seen':<12} {row['first_seen'][:10]}")
        if row["closed_at"]:
            print(f"{'Closed':<12} {yellow(row['closed_at'][:10])}")
        if row["rejected"]:
            print(f"{'Rejected':<12} {yellow(row['rejected'])}")
        if row["note"]:
            print(f"{'Note':<12} {row['note']}")

        if row["description"]:
            print("\n" + dim(SYM["rule"] * 60))
            body = row["description"]
            print(body if args.full else truncate(body, 1500))
    return 0


def cmd_mark(args) -> int:
    with _store(args) as store:
        matches = store.resolve(args.job_id)
        if not matches:
            print(red(f"No job with id starting {args.job_id!r}"))
            return 1
        if len(matches) > 1:
            print(yellow("That id prefix is ambiguous; be more specific."))
            return 1

        row = matches[0]
        store.set_status(row["uid"], args.status)
        if args.note:
            store.set_note(row["uid"], args.note)
        print(f"{row['company']} {SYM['dash']} {row['title']} {SYM['arrow']} {green(args.status)}")
    return 0


def cmd_digest(args) -> int:
    """Write the digest file.

    An inbox, not a newsletter: every open role you have not triaged, however
    long ago it arrived. Filtering by "since the last digest" meant a role you
    did not get through vanished the next day while still being open.
    """
    out = Path(args.out) if args.out else Path("out/digest.html")

    with _store(args) as store:
        rows, subtitle, since = collect(
            store,
            show_all=args.all,
            min_score=args.min_score,
            company=args.company,
            since=args.since,
            limit=args.limit,
        )
        html = render_digest(rows, subtitle=subtitle, since=since)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(html, encoding="utf-8")

        if not args.no_mark:
            store.meta_set("last_digest_at", now_iso())

    print(f"Wrote {green(str(out))} {SYM['dash']} {subtitle}")
    if not rows:
        print(
            dim(
                "Nothing untriaged. Mark roles with `jobhunt mark <id> applied|ignored`, "
                "or use --all to see everything again."
            )
        )

    if args.open:
        webbrowser.open(out.resolve().as_uri())
    return 0


def cmd_serve(args) -> int:
    """Serve the digest with working buttons.

    The written file cannot change anything: opened over file:// it has no
    route back to the database. Served, the same page can.
    """
    from .server import serve

    base = Path(args.data_dir) if args.data_dir else data_dir()
    try:
        server = serve(base / "jobs.db", port=args.port, show_all=args.all)
    except OSError as exc:
        print(red(f"Could not start on port {args.port}: {exc}"))
        print(dim("Another copy may already be running. Try --port 8766."))
        return 1

    url = f"http://127.0.0.1:{server.server_address[1]}/"
    print(f"Digest at {green(url)}")
    print(dim("Buttons mark roles as you read. Ctrl+C to stop."))

    if not args.no_open:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()
    return 0


def cmd_rescore(args) -> int:
    """Re-apply the current rules to everything already stored."""
    config = _load_config(args)
    with _store(args) as store:
        rows = store.all_jobs()
        kept = 0
        for row in rows:
            verdict = score_job(_row_to_job(row), config.rules)
            store.update_verdict(row["uid"], verdict)
            if verdict.kept and not row["closed_at"]:
                kept += 1

    print(f"Rescored {len(rows)} stored roles {SYM['dot']} {kept} now match your rules")
    return 0


def cmd_departments(args) -> int:
    """List the departments seen, so a broad board can be narrowed."""
    with _store(args) as store:
        rows = store.departments(args.company)

    if not rows:
        print("Nothing collected yet. Run `jobhunt fetch` first.")
        return 0

    table_rows = [
        [row["department"], row["company"], str(row["total"]), str(row["matching"] or 0)]
        for row in rows
    ]
    print(_table(["DEPARTMENT", "COMPANY", "SEEN", "MATCH"], table_rows, [40, 24, 6, 6]))
    print(
        dim(
            "\nOn SmartRecruiters you can filter server-side by adding to that "
            "company in config.yaml:\n"
            "    params:\n      department: <name from above>"
        )
    )
    return 0


def cmd_stats(args) -> int:
    with _store(args) as store:
        counts = store.counts()
        last_fetch = store.meta_get("last_fetch_at")
        companies = store.companies()

    print(bold("Pipeline"))
    print(f"  {counts['total']:>5}  roles seen in total")
    print(f"  {counts['matching']:>5}  currently match your rules")
    print(f"  {counts['unseen']:>5}  not yet triaged")
    print(f"  {counts['applied']:>5}  applied")
    print(f"  {counts['closed']:>5}  delisted since first seen")
    if last_fetch:
        print(dim(f"\nLast fetch {human_age(last_fetch)} ago"))

    if companies:
        print("\n" + bold("By company"))
        rows = [
            [row["company"], str(row["matching"] or 0), str(row["total"] or 0)]
            for row in companies
        ]
        print(_table(["COMPANY", "MATCH", "SEEN"], rows, [30, 6, 6]))
    return 0


# -- argument parsing ------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="jobhunt",
        description="Watch company career pages and surface the roles worth your time.",
    )
    parser.add_argument("--version", action="version", version=f"jobhunt {__version__}")
    parser.add_argument("--config", help="path to config.yaml (default: ./config.yaml)")
    parser.add_argument("--data-dir", help="where jobs.db lives (default: ./data)")

    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("init", help="write a starter config.yaml")
    p.add_argument("--force", action="store_true", help="overwrite an existing config")
    p.set_defaults(func=cmd_init)

    p = sub.add_parser("add", help="add a company careers URL")
    p.add_argument("url")
    p.add_argument("--name", help="display name (default: inferred)")
    p.add_argument("--source", help="force a board type instead of detecting it")
    p.add_argument("--no-sniff", action="store_true", help="do not fetch the page to detect its board")
    p.add_argument("--no-check", action="store_true", help="skip the test fetch")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("remove", help="stop watching a company")
    p.add_argument("company", help="name (or URL) of the company to drop")
    p.add_argument(
        "--purge", action="store_true", help="also delete its collected postings"
    )
    p.set_defaults(func=cmd_remove)

    p = sub.add_parser("companies", help="list configured companies")
    p.set_defaults(func=cmd_companies)

    p = sub.add_parser("resolve", help="re-detect the board behind configured URLs")
    p.add_argument("--company", help="resolve only this company")
    p.add_argument("--all", action="store_true", help="re-check every company, not just generic ones")
    p.set_defaults(func=cmd_resolve)

    p = sub.add_parser("fetch", help="pull the latest postings from every board")
    p.add_argument("--company", help="fetch only this company")
    p.add_argument("--workers", type=int, default=6, help="parallel fetches (default: 6)")
    p.set_defaults(func=cmd_fetch)

    p = sub.add_parser("list", help="list stored roles")
    p.add_argument("--company")
    p.add_argument("--status", choices=USER_STATUSES)
    p.add_argument("--min-score", type=float)
    p.add_argument("--limit", type=int, default=40)
    p.add_argument("--rejected", action="store_true", help="show what was filtered out, and why")
    p.add_argument(
        "--reason",
        help="only rejections whose reason contains this, e.g. 'location' (implies --rejected)",
    )
    p.add_argument("--include-closed", action="store_true", help="include delisted roles")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("show", help="show one role in full")
    p.add_argument("job_id")
    p.add_argument("--full", action="store_true", help="print the whole description")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("mark", help="set a role's status")
    p.add_argument("job_id")
    p.add_argument("status", choices=USER_STATUSES)
    p.add_argument("--note", help="attach a note")
    p.set_defaults(func=cmd_mark)

    p = sub.add_parser("digest", help="write the HTML digest")
    p.add_argument("--out", help="output path (default: out/digest.html)")
    p.add_argument("--open", action="store_true", help="open it in your browser")
    p.add_argument(
        "--all", action="store_true", help="include triaged roles too, not just untriaged"
    )
    p.add_argument("--since", help="ISO date; include roles first seen after it")
    p.add_argument("--company")
    p.add_argument("--min-score", type=float)
    p.add_argument("--limit", type=int)
    p.add_argument("--no-mark", action="store_true", help="do not advance the 'last digest' marker")
    p.set_defaults(func=cmd_digest)

    p = sub.add_parser("serve", help="open the digest with working buttons")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--all", action="store_true", help="include triaged roles too")
    p.add_argument("--no-open", action="store_true", help="do not open a browser")
    p.set_defaults(func=cmd_serve)

    p = sub.add_parser("rescore", help="re-apply rules to stored roles without refetching")
    p.set_defaults(func=cmd_rescore)

    p = sub.add_parser("departments", help="list departments seen, to narrow a broad board")
    p.add_argument("--company")
    p.set_defaults(func=cmd_departments)

    p = sub.add_parser("stats", help="summarise the pipeline")
    p.set_defaults(func=cmd_stats)

    return parser


def main(argv: list[str] | None = None) -> int:
    _configure_output()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(red(f"Config error: {exc}"))
        return 2
    except sources.UnknownSource as exc:
        print(red(str(exc)))
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
