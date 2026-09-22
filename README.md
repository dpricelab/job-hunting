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

On macOS, `git` arrives with the Xcode command line tools, which the first
`git` command offers to install. The system Python can be older than 3.10 —
check with `python3 --version` and install a current one from python.org if it
is.

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
>
> **Big boards can be filtered server-side.** A multinational's Workday site
> lists every role worldwide. Filter it in the browser — by country, say — and
> paste the resulting URL into `jobhunt add`: the filter ids in the query
> string are kept and applied to every fetch, so only those roles are read.
> To change a filter later, edit the company's `params` in `config.yaml`.

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
  title_bypass_score: 0   # unless the body scores at least this (0 = hard gate)
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

### When a good title says nothing useful

A title gate is blunt: a publisher advertising a "Partnerships Manager" whose
work is publications and journal editors gets rejected before the body is ever
read. `title_bypass_score` lets a posting earn its way in on body alone.

Pick the threshold by looking at what it lets through, not by intuition. Every
advert at a publisher mentions its own journals, so boilerplate alone scores a
few points on any role in the building — set it too low and the gate stops
meaning anything.

### Why didn't I see that job?

Every rejection records its reason:

```
$ jobhunt list --rejected
ID            SCORE  COMPANY         TITLE                     WHY
9f2a1c...     1      Cobalt Retail   Backend Engineer          score 1 below min_score 3
3b81e0...     0      Helix Systems   Engineering Intern        title excludes 'intern'
```

## Day to day

The whole routine is two commands. Everything else is occasional.

```bash
jobhunt fetch                      # refresh every board (6 at a time)
jobhunt digest --open              # what's new since last time
```

Or, to triage by clicking instead of typing:

```bash
jobhunt serve                      # same page, with working buttons
```

The written file cannot change anything — opened over `file://` it has no
route back to the database. Served, each role gets **Applied** and **Not for
me** buttons that write as you read, with an undo. It listens on loopback
only, and every request carries a token minted at startup and embedded in the
page, so a website open in another tab cannot post to it. Ctrl+C to stop.

The equivalent from the terminal:

```bash
jobhunt mark 9f2a1c applied --note "referred by Sam"
jobhunt mark a39a15 ignored
```

Marking matters more than it looks. Statuses survive re-fetching, so a role
you have applied to stays marked as the posting is refreshed, and
`--status new` keeps meaning "not yet triaged" rather than "everything".

### Looking around

```bash
jobhunt list                       # matching roles, best first
jobhunt list --status new          # not yet triaged
jobhunt list --company Wiley       # one source
jobhunt show 9f2a1c                # one role in full, by id prefix
jobhunt stats                      # pipeline summary
jobhunt digest --all --open        # rebuild the full list, not just new
```

### Why didn't I see that job?

```bash
jobhunt list --rejected                    # everything filtered out, and why
jobhunt list --reason location             # only one kind of rejection
jobhunt departments --company Informa      # what a broad board is tagged with
```

### Changing the rules

Edit `config.yaml`, then re-apply to what you already have — no re-fetch:

```bash
jobhunt rescore
```

### Changing the sources

```bash
jobhunt add https://example.com/careers    # detects the board behind it
jobhunt resolve                            # re-detect generic ones
jobhunt remove "Royal Society"             # stop watching; --purge drops its data
jobhunt companies                          # what is being watched
```

### When a source looks wrong

Each probe explains one class of failure rather than leaving you guessing:

```bash
python tools/probe_generic.py "Sage"                  # why a page yielded what it did
python tools/probe_workday.py Elsevier                # paging and field names
python tools/probe_smartrecruiters.py "Taylor & Francis" editor
```

### Running it on a schedule

`scripts/daily.sh` fetches and rebuilds the digest. It finds the repository
from its own path and uses the virtual environment if there is one, because a
scheduler runs neither a login shell nor from a predictable directory. If every
board fails — typically the machine waking before Wi-Fi is back — it waits a
minute and tries once more. It does not move the "New" marker: that moves when
you open the digest yourself, with `digest` or `serve`, so the badges mean new
since you last looked.

On macOS:

```bash
sh scripts/install-macos-schedule.sh        # weekdays at 07:00
sh scripts/install-macos-schedule.sh 8 30   # or pick a time
```

That writes a launchd agent and loads it. launchd rather than cron on purpose:
cron simply skips a run if the machine is asleep at the appointed moment, which
for a laptop is most mornings, whereas launchd runs the job when the machine
next wakes.

```bash
launchctl start com.jobhunt.daily                            # run it now
launchctl unload ~/Library/LaunchAgents/com.jobhunt.daily.plist   # stop
```

Output goes to `out/schedule.log`.

On Linux, cron is fine, since a server is rarely asleep:

```cron
0 7 * * 1-5 /home/you/job-hunting/scripts/daily.sh >> /home/you/job-hunting/out/schedule.log 2>&1
```

On Windows, point Task Scheduler at a batch file running the same two commands.

Either way, `out/digest.html` is waiting when you sit down.

## How it behaves

- **Dedupe across runs.** A posting is identified by its board id, so tracking
  parameters appearing in a URL will not resurface it as new.
- **Delisting.** After a successful fetch, previously-seen roles that have
  vanished from a board are marked closed and drop out of listings. If one
  reappears, it is reopened. `--include-closed` shows them. A board that
  suddenly returns no roles at all is treated as a hiccup, not a mass
  withdrawal, and its roles are left open.
- **Failures are contained.** One unreachable board reports its error and the
  rest of the run continues. Requests retry with exponential backoff on
  timeouts and 5xx/429 responses.
- **Your data stays local.** `config.yaml` is tracked so the watch list and
  rules follow you between machines. `data/jobs.db` is not, because it is
  binary and changes on every run — **copy it across by hand when moving to a
  new machine**, or you lose which roles you have already applied to or
  ignored, and every posting reappears as new. It is one portable SQLite file
  and works on any operating system.

## Layout

```
jobhunt/
  cli.py            command line interface
  config.py         YAML config loading and validation
  models.py         Job and Company
  scoring.py        keyword and rule scoring
  store.py          SQLite persistence
  render.py         HTML digest
  digest.py         what belongs in it, shared by the file and the server
  server.py         local server whose buttons write to the database
  http.py           shared HTTP client with retries
  sources/
    __init__.py     registry, URL → reader
    ats.py          Greenhouse, Lever, Ashby, SmartRecruiters, Recruitee,
                    Workable, Workday, Personio, plus embedded-board sniffing
    feeds.py        RSS and Atom
    generic.py      JSON-LD, embedded framework payloads, link heuristics
tools/              probes that explain why a source returned what it did
tests/              242 tests, no network access required
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
