"""End-to-end run against a local HTTP server: add → fetch → list → digest.

This exercises the real HTTP client, the generic reader, scoring, storage and
rendering together, without depending on any external job board.
"""

import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from jobhunt.cli import main
from jobhunt.config import Config
from jobhunt.store import Store


def posting(title, slug, description, location="London, UK", posted="2024-09-01"):
    return {
        "@context": "https://schema.org",
        "@type": "JobPosting",
        "title": title,
        "datePosted": posted,
        "identifier": {"@type": "PropertyValue", "value": slug},
        "url": f"/jobs/{slug}",
        "hiringOrganization": {"@type": "Organization", "name": "Acme"},
        "jobLocation": {"@type": "Place", "address": {"addressLocality": location}},
        "description": description,
    }


ALL_POSTINGS = [
    posting(
        "Senior Python Engineer",
        "senior-python-engineer",
        "<p>Work on our <b>Python</b> services with Postgres.</p>",
    ),
    posting(
        "Engineering Intern",
        "engineering-intern",
        "<p>A summer internship writing Python.</p>",
    ),
    posting(
        "Head of Sales",
        "head-of-sales",
        "<p>Own the commercial pipeline.</p>",
    ),
]

# The second fetch drops the intern role and adds a new one, so the test can
# check both the "closed" path and the "new since last digest" path.
SECOND_FETCH = [
    ALL_POSTINGS[0],
    ALL_POSTINGS[2],
    posting(
        "Staff Python Engineer",
        "staff-python-engineer",
        "<p>Lead our Python platform work.</p>",
    ),
]

CONFIG = """
companies:
  - name: Acme
    url: {url}
    source: generic
rules:
  min_score: 3
  title_bonus: 2
  title_exclude:
    - intern
  keywords:
    python: 3
    postgres: 1
"""


class _Handler(BaseHTTPRequestHandler):
    payload = ALL_POSTINGS

    def do_GET(self):
        blocks = "\n".join(
            f'<script type="application/ld+json">{json.dumps(p)}</script>'
            for p in type(self).payload
        )
        body = f"<html><head>{blocks}</head><body><h1>Careers</h1></body></html>"
        encoded = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass  # keep the test output clean


class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/careers"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.payload = ALL_POSTINGS
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.config = self.base / "config.yaml"
        self.data = self.base / "data"
        self.config.write_text(CONFIG.format(url=self.url), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        args = ["--config", str(self.config), "--data-dir", str(self.data), *argv]
        with redirect_stdout(buffer):
            code = main(args)
        return code, buffer.getvalue()

    def rows(self, **kwargs):
        with Store(self.data / "jobs.db") as store:
            return [dict(r) for r in store.query(**kwargs)]

    def test_fetch_scores_and_filters(self):
        code, out = self.run_cli("fetch")
        self.assertEqual(code, 0, out)
        self.assertIn("Acme: 3 roles, 1 match", out)

        kept = self.rows()
        self.assertEqual([r["title"] for r in kept], ["Senior Python Engineer"])
        # python in the title (3 x 2) + postgres in the body (1)
        self.assertEqual(kept[0]["score"], 7)
        self.assertEqual(json.loads(kept[0]["matched"]), ["python*", "postgres"])

        rejected = {r["title"]: r["rejected"] for r in self.rows(rejected=True)}
        self.assertIn("intern", rejected["Engineering Intern"])
        self.assertIn("below min_score", rejected["Head of Sales"])

    def test_descriptions_are_flattened_to_text(self):
        self.run_cli("fetch")
        description = self.rows()[0]["description"]
        self.assertEqual(description, "Work on our Python services with Postgres.")

    def test_refetching_is_idempotent(self):
        self.run_cli("fetch")
        _, out = self.run_cli("fetch")
        self.assertIn("3 roles, 1 match", out)
        self.assertIn("0 newly seen", out)
        self.assertEqual(len(self.rows(include_closed=True, rejected=False)), 1)

    def test_delisted_roles_are_closed_and_new_ones_appear(self):
        self.run_cli("fetch")
        _Handler.payload = SECOND_FETCH
        code, out = self.run_cli("fetch")
        self.assertEqual(code, 0, out)

        titles = sorted(r["title"] for r in self.rows())
        self.assertEqual(titles, ["Senior Python Engineer", "Staff Python Engineer"])

        with Store(self.data / "jobs.db") as store:
            closed = [
                r["title"] for r in store.query(rejected=True, include_closed=True)
                if r["closed_at"]
            ]
        self.assertIn("Engineering Intern", closed)

    def test_a_board_returning_nothing_does_not_close_its_roles(self):
        # A maintenance page or changed layout reads as zero roles. Closing on
        # that would silently empty the company out of the digest.
        self.run_cli("fetch")
        _Handler.payload = []
        code, out = self.run_cli("fetch")
        self.assertEqual(code, 0, out)
        self.assertIn("no roles returned", out)
        self.assertEqual([r["title"] for r in self.rows()], ["Senior Python Engineer"])

    def test_a_role_losing_its_body_for_one_fetch_is_still_matched(self):
        # Only the body says Python here, so scoring a blank would reject it.
        body_only = posting("Platform Engineer", "platform", "<p>Python services.</p>")
        _Handler.payload = [body_only]
        self.run_cli("fetch")
        self.assertEqual([r["title"] for r in self.rows()], ["Platform Engineer"])

        _Handler.payload = [dict(body_only, description="")]
        self.run_cli("fetch")
        self.assertEqual([r["title"] for r in self.rows()], ["Platform Engineer"])

    def test_digest_limit_is_honoured(self):
        _Handler.payload = SECOND_FETCH
        self.run_cli("fetch")
        self.assertEqual(len(self.rows()), 2)
        digest = self.base / "digest.html"
        self.run_cli("digest", "--out", str(digest), "--limit", "1")
        self.assertEqual(digest.read_text().count('<article class="job"'), 1)

    def test_an_untriaged_role_stays_in_the_digest(self):
        # The digest is an inbox: not getting through the list must not lose
        # a role that is still open.
        self.run_cli("fetch")
        digest = self.base / "digest.html"

        self.run_cli("digest", "--out", str(digest))
        self.assertIn("Senior Python Engineer", digest.read_text())

        self.run_cli("digest", "--out", str(digest))
        self.assertIn("Senior Python Engineer", digest.read_text())

    def test_triaging_a_role_removes_it_from_the_digest(self):
        self.run_cli("fetch")
        uid = self.rows()[0]["uid"]
        digest = self.base / "digest.html"

        self.run_cli("mark", uid, "applied")
        self.run_cli("digest", "--out", str(digest))
        self.assertNotIn("Senior Python Engineer", digest.read_text())

        # --all still shows it, so nothing is truly hidden.
        self.run_cli("digest", "--out", str(digest), "--all")
        self.assertIn("Senior Python Engineer", digest.read_text())

    def test_arrivals_since_the_last_digest_are_flagged(self):
        self.run_cli("fetch")
        digest = self.base / "digest.html"
        self.run_cli("digest", "--out", str(digest))

        _Handler.payload = SECOND_FETCH
        self.run_cli("fetch")
        self.run_cli("digest", "--out", str(digest))

        html = digest.read_text()
        # Both are present; only the newcomer is badged.
        self.assertIn("Staff Python Engineer", html)
        self.assertIn("Senior Python Engineer", html)
        self.assertIn('class="flag new"', html)

    def test_a_rejected_role_never_reaches_the_digest(self):
        self.run_cli("fetch")
        digest = self.base / "digest.html"
        self.run_cli("digest", "--out", str(digest), "--all")
        self.assertNotIn("Head of Sales", digest.read_text())

    def test_digest_escapes_untrusted_text(self):
        # Job titles are third-party text and land in HTML we generate, so they
        # must be escaped rather than interpolated raw.
        _Handler.payload = [
            posting(
                'Python Engineer <img src=x onerror="alert(1)">',
                "xss",
                "Python and Postgres.",
            )
        ]
        self.run_cli("fetch")
        digest = self.base / "digest.html"
        self.run_cli("digest", "--out", str(digest))
        html = digest.read_text()

        self.assertIn("Python Engineer", html)
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img src=x", html)

    def test_rescore_applies_edited_rules_without_refetching(self):
        self.run_cli("fetch")
        self.assertEqual(len(self.rows()), 1)

        self.config.write_text(
            CONFIG.format(url=self.url).replace("min_score: 3", "min_score: 0"),
            encoding="utf-8",
        )
        code, out = self.run_cli("rescore")
        self.assertEqual(code, 0, out)
        self.assertIn("2 now match", out)  # the intern is still excluded by title

        titles = sorted(r["title"] for r in self.rows())
        self.assertEqual(titles, ["Head of Sales", "Senior Python Engineer"])

    def test_rescore_does_not_resurrect_delisted_roles(self):
        self.run_cli("fetch")
        _Handler.payload = SECOND_FETCH  # the intern role disappears
        self.run_cli("fetch")

        self.config.write_text(
            CONFIG.format(url=self.url).replace("    - intern\n", ""),
            encoding="utf-8",
        )
        self.run_cli("rescore")

        titles = [r["title"] for r in self.rows()]
        self.assertNotIn("Engineering Intern", titles)

    def test_list_show_and_mark_round_trip(self):
        self.run_cli("fetch")
        uid = self.rows()[0]["uid"]

        code, out = self.run_cli("list")
        self.assertEqual(code, 0)
        self.assertIn("Senior Python Engineer", out)

        code, out = self.run_cli("show", uid[:6])
        self.assertEqual(code, 0)
        self.assertIn("Senior Python Engineer", out)
        self.assertIn("python*", out)

        code, out = self.run_cli("mark", uid[:6], "applied", "--note", "referred by Sam")
        self.assertEqual(code, 0)
        self.assertIn("applied", out)

        self.assertEqual(self.rows(status="applied")[0]["note"], "referred by Sam")

    def test_list_rejected_explains_each_exclusion(self):
        self.run_cli("fetch")
        code, out = self.run_cli("list", "--rejected")
        self.assertEqual(code, 0)
        self.assertIn("Engineering Intern", out)
        self.assertIn("intern", out)

    def test_stats_summarises_the_pipeline(self):
        self.run_cli("fetch")
        code, out = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("roles seen in total", out)
        self.assertIn("Acme", out)

    def test_a_dead_board_fails_without_taking_down_the_run(self):
        self.config.write_text(
            CONFIG.format(url=self.url).replace(
                "rules:",
                "  - name: Dead\n"
                "    url: http://127.0.0.1:9/careers\n"
                "    source: generic\n"
                "rules:",
            ),
            encoding="utf-8",
        )
        code, out = self.run_cli("fetch")
        self.assertIn("Dead:", out)
        self.assertIn("Acme: 3 roles", out)
        self.assertEqual(code, 0)  # one board failed, the other did not


class RemoveTests(unittest.TestCase):
    """Dropping a source, including the near-miss names that make it risky."""

    CONFIG = (
        "companies:\n"
        "  - {name: Royal Society, url: 'https://rs.example/jobs', source: generic}\n"
        "  - {name: Royal Society of Chemistry, url: 'https://rsc.example/jobs', source: generic}\n"
        "  - {name: Wiley, url: 'https://wiley.example/jobs', source: generic}\n"
        "rules:\n  min_score: 0\n"
    )

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.config = self.base / "config.yaml"
        self.data = self.base / "data"
        self.config.write_text(self.CONFIG, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        args = ["--config", str(self.config), "--data-dir", str(self.data), *argv]
        with redirect_stdout(buffer):
            code = main(args)
        return code, buffer.getvalue()

    def seed(self):
        from jobhunt.models import Job
        from jobhunt.scoring import Verdict

        with Store(self.data / "jobs.db") as store:
            store.upsert(
                [
                    (
                        Job(
                            source="generic",
                            company=name,
                            title=f"{name} Editor",
                            url=f"https://x/{i}",
                            external_id=str(i),
                        ),
                        Verdict(score=5),
                    )
                    for i, name in enumerate(["Royal Society", "Royal Society of Chemistry"])
                ]
            )

    def names(self) -> list[str]:
        return [c.name for c in Config.load(self.config).companies]

    def test_removes_the_named_company(self):
        code, out = self.run_cli("remove", "Wiley")
        self.assertEqual(code, 0, out)
        self.assertNotIn("Wiley", self.names())
        self.assertEqual(len(self.names()), 2)

    def test_an_exact_name_wins_over_a_longer_one_containing_it(self):
        # "Royal Society" is a substring of "Royal Society of Chemistry".
        code, out = self.run_cli("remove", "Royal Society")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.names(), ["Royal Society of Chemistry", "Wiley"])

    def test_an_ambiguous_partial_name_is_refused(self):
        code, out = self.run_cli("remove", "Royal")
        self.assertEqual(code, 1)
        self.assertIn("more than one", out)
        self.assertEqual(len(self.names()), 3)  # nothing removed

    def test_an_unknown_name_is_an_error(self):
        code, out = self.run_cli("remove", "Elsevier")
        self.assertEqual(code, 1)
        self.assertIn("No configured company", out)

    def test_collected_postings_are_kept_by_default(self):
        self.seed()
        self.run_cli("remove", "Royal Society")
        with Store(self.data / "jobs.db") as store:
            self.assertEqual(store.count_for_company("Royal Society"), 1)

    def test_purge_deletes_only_that_company(self):
        self.seed()
        code, out = self.run_cli("remove", "Royal Society", "--purge")
        self.assertEqual(code, 0, out)
        with Store(self.data / "jobs.db") as store:
            self.assertEqual(store.count_for_company("Royal Society"), 0)
            # The similarly named company must be untouched.
            self.assertEqual(store.count_for_company("Royal Society of Chemistry"), 1)


class _CareersPageHandler(BaseHTTPRequestHandler):
    """A company careers page that embeds someone else's job board."""

    body = (
        "<html><body><h1>Work with us</h1>"
        '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmecorp"></iframe>'
        "</body></html>"
    )

    def do_GET(self):
        encoded = type(self).body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def log_message(self, *args):
        pass


class ResolveTests(unittest.TestCase):
    """`resolve` re-detects boards for companies stored as generic."""

    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _CareersPageHandler)
        cls.url = f"http://127.0.0.1:{cls.server.server_port}/careers"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _CareersPageHandler.body = (
            "<html><body><h1>Work with us</h1>"
            '<iframe src="https://boards.greenhouse.io/embed/job_board?for=acmecorp"></iframe>'
            "</body></html>"
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)
        self.config = self.base / "config.yaml"
        self.data = self.base / "data"

    def tearDown(self):
        self.tmp.cleanup()

    def run_cli(self, *argv) -> tuple[int, str]:
        buffer = io.StringIO()
        args = ["--config", str(self.config), "--data-dir", str(self.data), *argv]
        with redirect_stdout(buffer):
            code = main(args)
        return code, buffer.getvalue()

    def write_config(self, source="generic"):
        self.config.write_text(
            f"companies:\n  - name: Acme\n    url: {self.url}\n    source: {source}\n"
            f"rules:\n  min_score: 0\n",
            encoding="utf-8",
        )

    def test_a_generic_company_is_upgraded_to_its_real_board(self):
        self.write_config()
        code, out = self.run_cli("resolve")
        self.assertEqual(code, 0, out)
        # Asserted without the arrow between them: which character separates
        # the two depends on what the output encoding can represent.
        plain = out.replace("\x1b[1m", "").replace("\x1b[0m", "")
        self.assertRegex(plain, r"generic\s*\S*\s*greenhouse")

        config = Config.load(self.config)
        self.assertEqual(config.companies[0].source, "greenhouse")
        self.assertEqual(config.companies[0].token, "acmecorp")

    def test_a_page_with_no_board_is_reported_not_silently_changed(self):
        _CareersPageHandler.body = "<html><body>No openings right now.</body></html>"
        self.write_config()
        code, out = self.run_cli("resolve")
        self.assertEqual(code, 0)
        self.assertIn("still generic", out)
        self.assertEqual(Config.load(self.config).companies[0].source, "generic")

    def test_companies_with_a_known_board_are_skipped_by_default(self):
        self.write_config(source="lever")
        code, out = self.run_cli("resolve")
        self.assertEqual(code, 0)
        self.assertIn("Nothing to resolve", out)

    def test_all_rechecks_even_known_boards(self):
        self.write_config(source="lever")
        code, out = self.run_cli("resolve", "--all")
        self.assertEqual(code, 0)
        self.assertIn("Acme", out)

    def test_an_unreachable_page_never_downgrades_a_working_board(self):
        # resolve() also returns generic when the page cannot be fetched, so a
        # blip must not overwrite a company that already has a real board.
        self.config.write_text(
            "companies:\n  - name: Acme\n    url: http://127.0.0.1:9/careers\n"
            "    source: greenhouse\n    token: acmecorp\n"
            "rules:\n  min_score: 0\n",
            encoding="utf-8",
        )
        code, out = self.run_cli("resolve", "--all")
        self.assertEqual(code, 0)
        self.assertIn("could not confirm", out)

        config = Config.load(self.config)
        self.assertEqual(config.companies[0].source, "greenhouse")
        self.assertEqual(config.companies[0].token, "acmecorp")

    def test_an_unknown_company_name_is_an_error(self):
        self.write_config()
        code, out = self.run_cli("resolve", "--company", "Globex")
        self.assertEqual(code, 1)
        self.assertIn("No configured company", out)

    def test_add_strips_a_url_fragment(self):
        self.config.write_text("companies: []\nrules: {}\n", encoding="utf-8")
        code, out = self.run_cli("add", f"{self.url}#jobs", "--no-check")
        self.assertEqual(code, 0, out)
        self.assertEqual(Config.load(self.config).companies[0].url, self.url)


if __name__ == "__main__":
    unittest.main()
