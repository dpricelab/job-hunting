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

def _locations_text(i: int) -> str:
    if i == 44:
        return "3 Locations"  # Workday summarises instead of naming a place
    return "London, United Kingdom" if i % 2 else "Remote, United Kingdom"


POSTINGS = [
    {
        "title": f"Role {i}",
        "externalPath": f"/job/London/Role-{i}_JR{1000 + i}",
        "locationsText": _locations_text(i),
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
        batch = POSTINGS[offset : offset + limit]

        # Faithful to the real API: the total is reported on the first page
        # only, and later pages carry zero. Reading it every time is what
        # truncated real boards to two pages.
        total = len(POSTINGS) if offset == 0 else 0

        # Past the end the board serves its first page again rather than an
        # empty one, which is what the repeat guard exists to stop.
        if not batch:
            batch = POSTINGS[:limit]
            total = len(POSTINGS)

        self._send({"total": total, "jobPostings": batch})

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
                    "location": "Manchester, United Kingdom",
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

    def test_url_with_a_locale_segment_keeps_the_locale(self):
        self.assertEqual(
            ats.detect_workday(
                "https://springernature.wd3.myworkdayjobs.com/en-US/SpringerNatureCareers"
            ),
            "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers/en-US",
        )

    def test_deep_link_to_a_single_posting(self):
        self.assertEqual(
            ats.detect_workday(
                "https://springernature.wd3.myworkdayjobs.com/de-DE/"
                "SpringerNatureCareers/job/Berlin/Editor_JR1234"
            ),
            "springernature.wd3.myworkdayjobs.com/springernature/SpringerNatureCareers/de-DE",
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


class WorkdayIdentityTests(unittest.TestCase):
    """bulletFields is not uniform across tenants.

    Most put the requisition id there, but some put a location string and some
    omit it. Trusting it blindly would give every job in one city the same
    identity, silently collapsing them into a single stored record.
    """

    def build(self, entry: dict):
        return ats._workday_job(entry, "acme.wd1.myworkdayjobs.com", "Careers", "en-US", "Acme")

    def test_requisition_shaped_values_are_used(self):
        for value in ("JR1234", "R-12345", "REQ_123", "123456"):
            with self.subTest(value=value):
                job = self.build(
                    {"title": "Engineer", "externalPath": "/job/X/Engineer", "bulletFields": [value]}
                )
                self.assertEqual(job.external_id, value)

    def test_location_shaped_values_fall_back_to_the_path(self):
        for value in ("London, United Kingdom", "2 Locations", "Remote", "New York"):
            with self.subTest(value=value):
                job = self.build(
                    {
                        "title": "Engineer",
                        "externalPath": "/job/London/Engineer_1",
                        "bulletFields": [value],
                    }
                )
                self.assertEqual(job.external_id, "/job/London/Engineer_1")

    def test_missing_bullet_fields_fall_back_to_the_path(self):
        job = self.build({"title": "Engineer", "externalPath": "/job/London/Engineer_1"})
        self.assertEqual(job.external_id, "/job/London/Engineer_1")

    def test_jobs_sharing_a_location_bullet_stay_distinct(self):
        entries = [
            {
                "title": f"Engineer {i}",
                "externalPath": f"/job/London/Engineer-{i}",
                "bulletFields": ["London, United Kingdom"],
                "locationsText": "London, United Kingdom",
            }
            for i in range(3)
        ]
        uids = {self.build(entry).uid for entry in entries}
        self.assertEqual(len(uids), 3)

    def test_vague_location_text_is_dropped_pending_the_detail_call(self):
        job = self.build(
            {"title": "Engineer", "externalPath": "/job/X/E", "locationsText": "2 Locations"}
        )
        self.assertEqual(job.location, "")

    def test_a_real_location_is_kept(self):
        job = self.build(
            {
                "title": "Engineer",
                "externalPath": "/job/X/E",
                "locationsText": "London, United Kingdom",
            }
        )
        self.assertEqual(job.location, "London, United Kingdom")


class WorkdayFacetTests(unittest.TestCase):
    """Reading one country from a board that lists a whole multinational."""

    def test_params_become_applied_facets(self):
        facets, text = ats.workday_facets({"locationCountry": "29247e57"})
        self.assertEqual(facets, {"locationCountry": ["29247e57"]})
        self.assertEqual(text, "")

    def test_lists_pass_through_and_search_text_is_separate(self):
        facets, text = ats.workday_facets(
            {"locations": ["a1", "b2"], "searchText": "editor"}
        )
        self.assertEqual(facets, {"locations": ["a1", "b2"]})
        self.assertEqual(text, "editor")

    def test_empty_values_are_dropped(self):
        facets, _ = ats.workday_facets({"locationCountry": ["", None, "x"]})
        self.assertEqual(facets, {"locationCountry": ["x"]})

    def test_no_params_means_no_filter(self):
        self.assertEqual(ats.workday_facets(None), ({}, ""))

    def test_facets_reach_the_request_body(self):
        seen: list[dict] = []

        def fake_post_json(url, body, **kwargs):
            seen.append(body)
            return {"total": 0, "jobPostings": []}

        original = ats.post_json
        ats.post_json = fake_post_json
        try:
            ats.fetch_workday(
                "wk.wd3.myworkdayjobs.com/wk/External",
                "Wolters Kluwer",
                params={"locationCountry": "29247e57"},
            )
        finally:
            ats.post_json = original

        self.assertEqual(seen[0]["appliedFacets"], {"locationCountry": ["29247e57"]})
        self.assertEqual(seen[0]["limit"], ats.WORKDAY_PAGE_SIZE)

    def test_facets_are_read_from_a_filtered_url_on_add(self):
        from jobhunt.cli import _facets_from_url

        url = "https://wk.wd3.myworkdayjobs.com/External?locationCountry=29247e57&jobFamilyGroup=abc"
        self.assertEqual(
            _facets_from_url(url), {"locationCountry": "29247e57", "jobFamilyGroup": "abc"}
        )

    def test_a_plain_url_yields_no_facets(self):
        from jobhunt.cli import _facets_from_url

        self.assertEqual(_facets_from_url("https://wk.wd3.myworkdayjobs.com/External"), {})

    def test_detection_ignores_the_query_string(self):
        self.assertEqual(
            ats.detect_workday("https://wk.wd3.myworkdayjobs.com/External?locationCountry=x"),
            "wk.wd3.myworkdayjobs.com/wk/External",
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
        # 45 postings is three pages, so this fails if paging stops at the
        # second page — which is what reading `total` from every page did.
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertEqual(len(jobs), len(POSTINGS))
        self.assertEqual(jobs[0].title, "Role 0")
        self.assertEqual(jobs[-1].title, "Role 44")

    def test_a_zero_total_on_later_pages_does_not_end_paging(self):
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertGreater(len(jobs), 40)

    def test_postings_are_not_collected_twice(self):
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertEqual(len({job.uid for job in jobs}), len(jobs))

    def test_fields_are_mapped(self):
        job = ats.fetch_workday(self.token, "Springer Nature")[1]
        self.assertEqual(job.source, "workday")
        self.assertEqual(job.company, "Springer Nature")
        self.assertEqual(job.external_id, "JR1001")
        self.assertEqual(job.location, "London, United Kingdom")
        self.assertEqual(
            job.url, f"http://{self.host}/en-US/{SITE}/job/London/Role-1_JR1001"
        )

    def test_job_urls_use_the_locale_from_the_configured_url(self):
        job = ats.fetch_workday(f"{self.token}/de-DE", "Springer Nature")[1]
        self.assertEqual(
            job.url, f"http://{self.host}/de-DE/{SITE}/job/London/Role-1_JR1001"
        )

    def test_descriptions_are_fetched_and_flattened(self):
        jobs = ats.fetch_workday(self.token, "Springer Nature")
        self.assertEqual(jobs[0].description, "Description for Role 0 using Python.")
        self.assertEqual(_Handler.detail_calls, len(POSTINGS))

    def test_remote_roles_are_flagged(self):
        jobs = {job.title: job for job in ats.fetch_workday(self.token, "Springer Nature")}
        self.assertTrue(jobs["Role 0"].remote)  # "Remote, United Kingdom"
        self.assertFalse(jobs["Role 1"].remote)

    def test_detail_supplies_a_location_when_the_listing_is_vague(self):
        jobs = {job.title: job for job in ats.fetch_workday(self.token, "Springer Nature")}
        self.assertEqual(jobs["Role 44"].location, "Manchester, United Kingdom")
        # A location the listing did name is left alone.
        self.assertEqual(jobs["Role 1"].location, "London, United Kingdom")

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
