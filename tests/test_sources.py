import unittest
import xml.etree.ElementTree as ET

from jobhunt import sources
from jobhunt.sources import ats, feeds, generic


class DetectionTests(unittest.TestCase):
    def test_greenhouse_board_urls(self):
        for url in (
            "https://boards.greenhouse.io/acme",
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://job-boards.greenhouse.io/acme",
            "https://boards.eu.greenhouse.io/acme",
        ):
            with self.subTest(url=url):
                self.assertEqual(ats.detect_greenhouse(url), "acme")

    def test_greenhouse_embed_url_uses_the_for_parameter(self):
        url = "https://boards.greenhouse.io/embed/job_board?for=acme"
        self.assertEqual(ats.detect_greenhouse(url), "acme")

    def test_lever_and_ashby(self):
        self.assertEqual(ats.detect_lever("https://jobs.lever.co/acme"), "acme")
        self.assertEqual(ats.detect_lever("https://jobs.lever.co/acme/abc-123"), "acme")
        self.assertEqual(ats.detect_ashby("https://jobs.ashbyhq.com/acme"), "acme")

    def test_other_vendors(self):
        self.assertEqual(
            ats.detect_smartrecruiters("https://jobs.smartrecruiters.com/Acme"), "Acme"
        )
        self.assertEqual(ats.detect_recruitee("https://acme.recruitee.com/"), "acme")
        self.assertEqual(ats.detect_workable("https://apply.workable.com/acme/"), "acme")
        self.assertEqual(ats.detect_personio("https://acme.jobs.personio.de/"), "acme")

    def test_non_matching_urls_return_none(self):
        self.assertIsNone(ats.detect_greenhouse("https://example.com/careers"))
        self.assertIsNone(ats.detect_lever("https://example.com/careers"))

    def test_registry_detect_falls_back_to_generic(self):
        name, token = sources.detect("https://example.com/careers")
        self.assertEqual(name, "generic")
        self.assertEqual(token, "https://example.com/careers")

    def test_registry_detect_prefers_a_known_board(self):
        self.assertEqual(sources.detect("https://jobs.lever.co/acme"), ("lever", "acme"))

    def test_feed_detection(self):
        self.assertTrue(feeds.detect_feed("https://example.com/jobs.rss"))
        self.assertTrue(feeds.detect_feed("https://example.com/careers/feed"))
        self.assertIsNone(feeds.detect_feed("https://example.com/careers"))


class SmartRecruitersParamTests(unittest.TestCase):
    """One token can cover a whole group, so server-side filters matter."""

    def test_sub_site_urls_still_resolve_to_the_company_token(self):
        self.assertEqual(
            ats.detect_smartrecruiters(
                "https://careers.smartrecruiters.com/InformaGroupPlc/taylor-and-francis?search=manager"
            ),
            "InformaGroupPlc",
        )

    def test_configured_params_reach_the_request(self):
        seen = {}

        def fake_get_json(url, params=None, **kwargs):
            seen["url"] = url
            seen["params"] = params
            return {"totalFound": 0, "content": []}

        original = ats.get_json
        ats.get_json = fake_get_json
        try:
            ats.fetch_smartrecruiters(
                "InformaGroupPlc", "Taylor & Francis", params={"department": "Taylor & Francis"}
            )
        finally:
            ats.get_json = original

        self.assertEqual(seen["params"]["department"], "Taylor & Francis")
        self.assertEqual(seen["params"]["offset"], 0)
        self.assertEqual(seen["params"]["limit"], 100)

    def test_pagination_is_not_overridden_by_params(self):
        seen = []

        def fake_get_json(url, params=None, **kwargs):
            seen.append(dict(params or {}))
            return {"totalFound": 0, "content": []}

        original = ats.get_json
        ats.get_json = fake_get_json
        try:
            ats.fetch_smartrecruiters(
                "Acme", "Acme", params={"limit": 5, "offset": 999, "q": "manager"}
            )
        finally:
            ats.get_json = original

        self.assertEqual(seen[0]["limit"], 100)
        self.assertEqual(seen[0]["offset"], 0)
        self.assertEqual(seen[0]["q"], "manager")

    def test_params_are_optional(self):
        def fake_get_json(url, params=None, **kwargs):
            return {"totalFound": 0, "content": []}

        original = ats.get_json
        ats.get_json = fake_get_json
        try:
            self.assertEqual(ats.fetch_smartrecruiters("Acme", "Acme"), [])
        finally:
            ats.get_json = original


class SniffTests(unittest.TestCase):
    def test_finds_a_greenhouse_iframe(self):
        html = """
        <html><body>
          <iframe src="https://boards.greenhouse.io/embed/job_board?for=acmecorp"></iframe>
        </body></html>
        """
        self.assertEqual(ats.sniff_embedded_board(html), ("greenhouse", "acmecorp"))

    def test_finds_a_lever_link(self):
        html = '<a href="https://jobs.lever.co/acme-inc/">See our openings</a>'
        self.assertEqual(ats.sniff_embedded_board(html), ("lever", "acme-inc"))

    def test_ignores_pages_with_no_board(self):
        self.assertIsNone(ats.sniff_embedded_board("<html><body>No jobs here</body></html>"))


class FeedParsingTests(unittest.TestCase):
    RSS = """<?xml version="1.0"?>
    <rss version="2.0"><channel>
      <title>Acme jobs</title>
      <item>
        <title>Backend Engineer</title>
        <link>https://example.com/jobs/1</link>
        <guid>job-1</guid>
        <pubDate>Wed, 02 Oct 2024 09:00:00 GMT</pubDate>
        <description>&lt;p&gt;Build &lt;b&gt;things&lt;/b&gt;.&lt;/p&gt;</description>
        <category>Engineering</category>
      </item>
    </channel></rss>"""

    ATOM = """<?xml version="1.0"?>
    <feed xmlns="http://www.w3.org/2005/Atom">
      <entry>
        <title>Data Scientist</title>
        <link rel="alternate" href="https://example.com/jobs/2"/>
        <id>job-2</id>
        <published>2024-10-02T09:00:00Z</published>
        <summary>Models and pipelines.</summary>
      </entry>
    </feed>"""

    def test_rss_items_become_jobs(self):
        jobs = feeds._parse_rss(ET.fromstring(self.RSS), "Acme")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.title, "Backend Engineer")
        self.assertEqual(job.url, "https://example.com/jobs/1")
        self.assertEqual(job.department, "Engineering")
        self.assertTrue(job.posted_at.startswith("2024-10-02"))
        self.assertEqual(job.description, "Build things.")

    def test_atom_entries_become_jobs(self):
        jobs = feeds._parse_atom(ET.fromstring(self.ATOM), "Acme")
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0].title, "Data Scientist")
        self.assertEqual(jobs[0].url, "https://example.com/jobs/2")


class JsonLdTests(unittest.TestCase):
    PAGE = """
    <html><head>
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "JobPosting",
      "title": "Platform Engineer",
      "datePosted": "2024-09-01",
      "identifier": {"@type": "PropertyValue", "value": "PE-7"},
      "url": "/careers/platform-engineer",
      "hiringOrganization": {"@type": "Organization", "name": "Acme"},
      "jobLocation": {
        "@type": "Place",
        "address": {"addressLocality": "Bristol", "addressCountry": "GB"}
      },
      "description": "<p>Run the <b>platform</b>.</p>"
    }
    </script>
    </head><body></body></html>
    """

    REMOTE_PAGE = """
    <script type="application/ld+json">
    {"@graph": [{"@type": "JobPosting", "title": "SRE",
      "jobLocationType": "TELECOMMUTE", "url": "https://example.com/sre"}]}
    </script>
    """

    def test_extracts_a_posting(self):
        jobs = generic._from_jsonld(self.PAGE, "https://example.com/careers", "Acme")
        self.assertEqual(len(jobs), 1)
        job = jobs[0]
        self.assertEqual(job.title, "Platform Engineer")
        self.assertEqual(job.external_id, "PE-7")
        self.assertEqual(job.url, "https://example.com/careers/platform-engineer")
        self.assertEqual(job.location, "Bristol, GB")
        self.assertEqual(job.description, "Run the platform.")
        self.assertTrue(job.posted_at.startswith("2024-09-01"))

    def test_walks_into_graph_nodes_and_reads_remote_flag(self):
        jobs = generic._from_jsonld(self.REMOTE_PAGE, "https://example.com/", "Acme")
        self.assertEqual(len(jobs), 1)
        self.assertTrue(jobs[0].remote)

    def test_ignores_non_job_structured_data(self):
        page = '<script type="application/ld+json">{"@type":"Organization","name":"Acme"}</script>'
        self.assertEqual(generic._from_jsonld(page, "https://example.com/", "Acme"), [])

    def test_survives_malformed_json(self):
        page = '<script type="application/ld+json">{not json</script>'
        self.assertEqual(generic._from_jsonld(page, "https://example.com/", "Acme"), [])


class AnchorFallbackTests(unittest.TestCase):
    PAGE = """
    <html><body>
      <nav><a href="/about">About</a><a href="/careers">Careers</a></nav>
      <ul>
        <li><a href="/careers/jobs/senior-backend-engineer">Senior Backend Engineer</a></li>
        <li><a href="/careers/jobs/product-designer">Product Designer</a></li>
        <li><a href="/careers/jobs/senior-backend-engineer">Apply</a></li>
      </ul>
      <a href="https://twitter.com/acme/jobs">Follow us</a>
    </body></html>
    """

    def test_picks_out_posting_links(self):
        jobs = generic._from_anchors(self.PAGE, "https://example.com/careers", "Acme")
        titles = sorted(job.title for job in jobs)
        self.assertEqual(titles, ["Product Designer", "Senior Backend Engineer"])

    def test_urls_are_absolute(self):
        jobs = generic._from_anchors(self.PAGE, "https://example.com/careers", "Acme")
        self.assertTrue(all(job.url.startswith("https://example.com/") for job in jobs))

    def test_offsite_links_are_dropped(self):
        jobs = generic._from_anchors(self.PAGE, "https://example.com/careers", "Acme")
        self.assertFalse(any("twitter" in job.url for job in jobs))


if __name__ == "__main__":
    unittest.main()
