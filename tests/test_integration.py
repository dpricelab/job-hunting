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

    def test_digest_reports_only_new_roles_on_the_second_run(self):
        self.run_cli("fetch")
        digest = self.base / "digest.html"

        code, out = self.run_cli("digest", "--out", str(digest))
        self.assertEqual(code, 0, out)
        html = digest.read_text()
        self.assertIn("Senior Python Engineer", html)
        self.assertNotIn("Head of Sales", html)

        # Nothing changed, so the next digest is empty.
        self.run_cli("digest", "--out", str(digest))
        self.assertIn("Nothing new this run", digest.read_text())

        # A new posting shows up, and only that one.
        _Handler.payload = SECOND_FETCH
        self.run_cli("fetch")
        self.run_cli("digest", "--out", str(digest))
        html = digest.read_text()
        self.assertIn("Staff Python Engineer", html)
        self.assertNotIn("Senior Python Engineer", html)

        # --all rebuilds the full picture.
        self.run_cli("digest", "--out", str(digest), "--all")
        html = digest.read_text()
        self.assertIn("Staff Python Engineer", html)
        self.assertIn("Senior Python Engineer", html)

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


if __name__ == "__main__":
    unittest.main()
