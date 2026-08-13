"""Workday reader, driven against a local mock of the CXS job-board API.

The real endpoints are:
    POST /wday/cxs/<tenant>/<site>/jobs        paginated listing, 20 per page
    GET  /wday/cxs/<tenant>/<site><externalPath>   one posting, with body text

The mock below mirrors that shape, including the 20-per-page cap and the
relative "Posted 5 Days Ago" wording the listing returns instead of a date.
"""

import json
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from jobhunt.sources import ats

TENANT = "springernature"
SITE = "SpringerNatureCareers"

POSTINGS = [
    {
        "title": f"Role {i}",
        "externalPath": f"/job/London/Role-{i}_JR{1000 + i}",
        "locationsText": "London, United Kingdom" if i % 2 else "Remote, United Kingdom",
        "postedOn": "Posted 5 Days Ago",
        "bulletFields": [f"JR{1000 + i}"],
    }
    for i in range(45)  # spans three pages
]

DESCRIPTIONS = {
    p["externalPath"]: f"<p>Description for <b>{p['title']}</b> using Python.</p>"
    for p in POSTINGS
}


class _Handler(BaseHTTPRequestHandler):
    detail_calls = 0
    fail_details = False

    def _send(self, payload, status=200):
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def do_POST(self):
        if not self.path.endswith(f"/wday/cxs/{TENANT}/{SITE}/jobs"):
            return self._send({"error": "not found"}, 404)

        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length) or "{}")
        offset = int(body.get("offset") or 0)
        limit = int(body.get("limit") or 20)

        # Workday ignores any limit above 20.
        limit = min(limit, 20)
        self._send({"total": len(POSTINGS), "jobPostings": POSTINGS[offset : offset + limit]})

    def do_GET(self):
        prefix = f"/wday/cxs/{TENANT}/{SITE}"
        if not self.path.startswith(prefix):
            return self._send({"error": "not found"}, 404)

        type(self).detail_calls += 1
        if type(self).fail_details:
            return self._send({"error": "boom"}, 500)

        external_path = self.path[len(prefix) :]
        description = DESCRIPTIONS.get(external_path)
        if description is None:
            return self._send({"error": "not found"}, 404)

        self._send(
            {
                "jobPostingInfo": {
                    "jobDescription": description,
                    "startDate": "2026-08-01",
                    "remoteType": "Remote" if "Remote" in external_path else "",
                }
            }
        )

    def log_message(self, *args):
        pass


class WorkdayDetectionTests(unittest.TestCase):
    def test_board_url(self):
        self.assertEqual(
            ats.detect_workday("https://springernature.wd3.myworkdayjobs.com/SpringerNatureCareers"),
            "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers",
        )

    def test_url_with_a_locale_segment(self):
        self.assertEqual(
            ats.detect_workday(
                "https://springernature.wd3.myworkdayjobs.com/en-US/SpringerNatureCareers"
            ),
            "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers",
        )

    def test_deep_link_to_a_single_posting(self):
        self.assertEqual(
            ats.detect_workday(
                "https://springernature.wd3.myworkdayjobs.com/en-US/"
                "SpringerNatureCareers/job/London/Editor_JR1234"
            ),
            "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers",
        )

    def test_api_url_names_tenant_and_site_explicitly(self):
        self.assertEqual(
            ats.detect_workday(
                "https://acme.wd5.myworkdayjobs.com/wday/cxs/acmeglobal/ExternalSite/jobs"
            ),
            "acme.wd5.myworkdayjobs.com/acmeglobal/ExternalSite",
        )

    def test_other_data_centres(self):
        self.assertTrue(ats.detect_workday("https://acme.wd1.myworkdayjobs.com/Careers"))
        self.assertTrue(ats.detect_workday("https://acme.wd103.myworkdayjobs.com/Careers"))

    def test_non_workday_urls_are_ignored(self):
        self.assertIsNone(ats.detect_workday("https://boards.greenhouse.io/acme"))
        self.assertIsNone(ats.detect_workday("https://acme.myworkday.com/Careers"))
        self.assertIsNone(ats.detect_workday("https://springernature.wd3.myworkdayjobs.com/"))

    def test_registry_routes_workday_urls(self):
        from jobhunt import sources

        name, token = sources.detect(
            "https://springernature.wd3.myworkdayjobs.com/SpringerNatureCareers"
        )
        self.assertEqual(name, "workday")
        self.assertEqual(token, "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers")

    def test_sniffing_finds_a_workday_link_on_a_careers_page(self):
        html = (
            '<a href="https://springernature.wd3.myworkdayjobs.com/SpringerNatureCareers">'
            "See our vacancies</a>"
        )
        self.assertEqual(
            ats.sniff_embedded_board(html),
            ("workday", "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers"),
        )


class WorkdayDateTests(unittest.TestCase):
    def days_ago(self, iso: str) -> int:
        parsed = datetime.fromisoformat(iso)
        return round((datetime.now(timezone.utc) - parsed).total_seconds() / 86400)

    def test_relative_wording(self):
        self.assertEqual(self.days_ago(ats._workday_posted_at("Posted 5 Days Ago")), 5)
        self.assertEqual(self.days_ago(ats._workday_posted_at("Posted Today")), 0)
        self.assertEqual(self.days_ago(ats._workday_posted_at("Posted Yesterday")), 1)
        self.assertEqual(self.days_ago(ats._workday_posted_at("Posted 2 Weeks Ago")), 14)
        self.assertEqual(self.days_ago(ats._workday_posted_at("Posted 30+ Days Ago")), 30)

    def test_absolute_dates_still_parse(self):
        self.assertTrue(ats._workday_posted_at("2026-08-01").startswith("2026-08-01"))

    def test_missing_or_unparseable_values(self):
        self.assertIsNone(ats._workday_posted_at(None))
        self.assertIsNone(ats._workday_posted_at(""))
        self.assertIsNone(ats._workday_posted_at("sometime soon"))


class WorkdayFetchTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        cls.host = f"127.0.0.1:{cls.server.server_port}"
        cls.token = f"{cls.host}/{TENANT}/{SITE}"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls._scheme = ats.WORKDAY_SCHEME
        ats.WORKDAY_SCHEME = "http"

    @classmethod
    def tearDownClass(cls):
        ats.WORKDAY_SCHEME = cls._scheme
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self):
        _Handler.detail_calls = 0
        _Handler.fail_details = False

    def test_pagination_collects_every_posting(self):
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertEqual(len(jobs), len(POSTINGS))
        self.assertEqual(jobs[0].title, "Role 0")
        self.assertEqual(jobs[-1].title, "Role 44")

    def test_fields_are_mapped(self):
        job = ats.fetch_workday(self.token, "Springer Nature")[1]
        self.assertEqual(job.source, "workday")
        self.assertEqual(job.company, "Springer Nature")
        self.assertEqual(job.external_id, "JR1001")
        self.assertEqual(job.location, "London, United Kingdom")
        self.assertEqual(
            job.url, f"http://{self.host}/{SITE}/job/London/Role-1_JR1001"
        )

    def test_descriptions_are_fetched_and_flattened(self):
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertEqual(jobs[0].description, "Description for Role 0 using Python.")
        self.assertEqual(_Handler.detail_calls, len(POSTINGS))

    def test_remote_roles_are_flagged(self):
        jobs = {job.title: job for job in ats.fetch_workday(self.token, "Springer Nature")}
        self.assertTrue(jobs["Role 0"].remote)  # "Remote, United Kingdom"
        self.assertFalse(jobs["Role 1"].remote)

    def test_detail_start_date_overrides_the_relative_wording(self):
        job = ats.fetch_workday(self.token, "Springer Nature")[0]
        self.assertTrue(job.posted_at.startswith("2026-08-01"))

    def test_failing_detail_calls_do_not_lose_the_board(self):
        _Handler.fail_details = True
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertEqual(len(jobs), len(POSTINGS))
        self.assertEqual(jobs[0].description, "")
        # The listing still gives a usable date from the relative wording.
        self.assertIsNotNone(jobs[0].posted_at)

    def test_uid_is_stable_across_fetches(self):
        first = {job.uid for job in ats.fetch_workday(self.token, "Springer Nature")}
        second = {job.uid for job in ats.fetch_workday(self.token, "Springer Nature")}
        self.assertEqual(first, second)

    def test_a_malformed_token_is_reported_clearly(self):
        from jobhunt.http import HttpError

        with self.assertRaises(HttpError) as caught:
            ats.fetch_workday("justahost", "Springer Nature")
        self.assertIn("malformed workday token", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
