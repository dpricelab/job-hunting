# jobhunt

Watch the career pages of companies you actually want to work for, score every
posting against your own rules, and get a digest of what's new since last time.

No accounts, no scraping of sites that forbid it, no external services. It
reads the public job-board APIs that company career pages already call from
your browser, keeps everything in a local SQLite file, and writes a single
self-contained HTML page you open in your browser.

```
$ jobhunt fetch
Fetching 6 boards …

  ✓ Northwind Data: 34 roles, 3 match, 2 new
  ✓ Meridian Labs: 12 roles, 1 match
  ✓ Helix Systems: 58 roles, 4 match, 1 new
  ✗ Cobalt Retail: HTTP 404 for https://boards-api.greenhouse.io/v1/boards/cobalt/jobs

Done. 3 newly seen, 8 matching your rules, 1 failed

$ jobhunt digest --open
Wrote out/digest.html — 3 roles · new since 09 Aug 2026
```

## Install

```bash
git clone https://github.com/dpricelab/job-hunting
cd job-hunting
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
```

On Windows the activation step differs, and the rest is the same:

```bat
python -m venv .venv
.venv\Scripts\activate.bat        REM Command Prompt
.venv\Scripts\Activate.ps1        REM PowerShell
pip install -e .
```

That puts a `jobhunt` command on your PATH. If the shell reports it is not
recognised — usually because the install landed outside an activated virtual
environment — `python -m jobhunt` runs exactly the same code and does not
depend on PATH at all. Requires Python 3.10+; the only dependencies are
`requests` and `PyYAML`.

## Getting started

```bash
jobhunt init                                    # writes config.yaml
jobhunt add https://www.example.com/careers     # once per company
jobhunt fetch                                   # pull the latest postings
jobhunt digest --open                           # see what's new
```

### Adding a company

Give `add` whatever careers URL you have — the company's own page is fine:

```bash
$ jobhunt add https://www.northwind-data.com/careers
Inspecting https://www.northwind-data.com/careers …
Board: greenhouse · token northwinddata (found embedded in the page)
Found 34 open roles.
  • Senior Python Engineer, Platform — Remote (UK)
  • Infrastructure Engineer — Manchester, UK
  … and 32 more
```

It works out which job board sits behind the page and stores the details, so
later fetches hit the board's API directly. Pointing it straight at a board URL
(`https://boards.greenhouse.io/northwinddata`) works too and skips the lookup.

Boards read natively: **Greenhouse** (including EU), **Lever**, **Ashby**,
**SmartRecruiters**, **Recruitee**, **Workable**, **Workday**, **Personio**,
and any **RSS/Atom** feed.

> **Workday note.** Workday's listing endpoint is thinner than the others, so
> jobhunt fetches each posting's detail separately (8 at a time, capped at 400
> per run). That makes a Workday fetch slower, and it is what lets keyword rules
> score against the job body at all. Three quirks are handled for you:
>
> - The listing has no body text; descriptions come from the detail call.
> - Dates arrive as "Posted 5 Days Ago" rather than a date, so `max_age_days`
>   is approximate until the detail call supplies a real `startDate`.
> - Locations are sometimes summarised as "2 Locations"; the real place comes
>   from the detail call.
>
> Tenants also vary in what they put in `bulletFields`. Postings are identified
> by that value only when it is shaped like a requisition id, and by their
> unique path otherwise — trusting it blindly would give every job in one city
> the same identity.

For anything else, jobhunt falls back to reading the page itself: first any
[schema.org `JobPosting`](https://schema.org/JobPosting) data embedded for
Google Jobs, then a link-shaped guess. The fallback is best-effort and says so
— a careers page rendered entirely in JavaScript will come back empty. When
that happens, look for the "view all jobs" link on the page, which usually
points at the real board, and add that URL instead.

## Configuring what counts as a match

Everything lives in `config.yaml`. Scoring is deterministic: no API keys, no
per-run cost, and identical inputs always give identical results.

```yaml
companies:
  - name: Northwind Data
    url: https://www.northwind-data.com/careers
    source: greenhouse
    token: northwinddata

rules:
  min_score: 3        # a posting needs this many points to reach your digest
  title_bonus: 2      # a keyword in the title is worth double

  keywords:
    python: 3
    kubernetes: 2
    terraform: 2
    "machine learning": 3
    aws: 1

  title_exclude:      # hard rejections, checked before scoring
    - intern
    - graduate
  exclude_keywords:
    - php

  title_include: []       # if set, the title must match one of these
  location_include: []    # matched against the posting's location field
  location_exclude:
    - "New York"

  remote_only: false
  max_age_days: 60        # 0 disables the age filter
```

Matching is case-insensitive and respects word boundaries, so `go` will not
match `goal`. Punctuation works as you'd expect (`c++` matches "C++ Engineer").
For anything more precise, prefix a term with `re:` to supply a regex:

```yaml
keywords:
  "re:(staff|principal|lead) engineer": 5
```

After editing rules, re-apply them to everything already collected without
re-fetching a thing:

```bash
jobhunt rescore
```

### Why didn't I see that job?

Every rejection records its reason:

```
$ jobhunt list --rejected
ID            SCORE  COMPANY         TITLE                     WHY
9f2a1c...     1      Cobalt Retail   Backend Engineer          score 1 below min_score 3
3b81e0...     0      Helix Systems   Engineering Intern        title excludes 'intern'
```

## Day to day

```bash
jobhunt fetch                      # refresh every board (runs 6 at a time)
jobhunt list                       # what's matching, best first
jobhunt list --status new          # not yet triaged
jobhunt show 9f2a1c               # full detail for one role, by id prefix
jobhunt mark 9f2a1c applied --note "referred by Sam"
jobhunt digest --open              # new since last digest
jobhunt digest --all --open        # rebuild the whole list
jobhunt stats                      # pipeline summary
```

`digest` shows only roles first seen since the previous digest, which is what
makes it useful on a schedule. `--all` rebuilds everything, and `--no-mark`
leaves the "last digest" marker alone so the next run still counts them as new.

Statuses are `new`, `seen`, `applied` and `ignored`, and they survive
re-fetching — marking something `applied` keeps it that way even as the posting
is refreshed.

### Running it on a schedule

A weekday-morning cron entry:

```cron
0 8 * * 1-5 cd ~/job-hunting && .venv/bin/jobhunt fetch && .venv/bin/jobhunt digest
```

Then open `out/digest.html` with your coffee.

## How it behaves

- **Dedupe across runs.** A posting is identified by its board id, so tracking
  parameters appearing in a URL will not resurface it as new.
- **Delisting.** After a successful fetch, previously-seen roles that have
  vanished from a board are marked closed and drop out of listings. If one
  reappears, it is reopened. `--include-closed` shows them.
- **Failures are contained.** One unreachable board reports its error and the
  rest of the run continues. Requests retry with exponential backoff on
  timeouts and 5xx/429 responses.
- **Your data stays local.** `data/jobs.db` and `config.yaml` are gitignored.

## Layout

```
jobhunt/
  cli.py            command line interface
  config.py         YAML config loading and validation
  models.py         Job and Company
  scoring.py        keyword and rule scoring
  store.py          SQLite persistence
  render.py         HTML digest
  http.py           shared HTTP client with retries
  sources/
    __init__.py     registry, URL → reader
    ats.py          Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee,
                    Workable, Workday, Personio, plus embedded-board sniffing
    feeds.py        RSS and Atom
    generic.py      JSON-LD and link heuristics
tests/              118 tests, no network access required
```

## Tests

```bash
python3 -m unittest discover -s tests -t .
```

The integration suite runs the real CLI against a local HTTP server, so the
whole pipeline — fetch, score, store, close, digest — is covered without
touching an external job board.

## Notes and limits

- `jobhunt add` rewrites `config.yaml`, so comments you add by hand are not
  preserved. Edit rules by hand and use `add` only for companies to be safe.
- LinkedIn and Indeed are deliberately not supported. Both prohibit scraping in
  their terms and actively block it, so anything built on them breaks quickly.
  Company boards carry the same roles, earlier and without the noise.
- The generic fallback cannot see jobs that only exist after JavaScript runs.
  Use the underlying board URL when that happens.
