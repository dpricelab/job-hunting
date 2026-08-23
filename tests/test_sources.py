import json
import unittest
import xml.etree.ElementTree as ET

from jobhunt import sources
from jobhunt.http import HttpError
from jobhunt.sources import ats, feeds, generic


class DetectionTests(unittest.TestCase):
    def test_greenhouse_board_urls(self):
        for url in (
            "https://boards.greenhouse.io/acme",
            "https://boards.greenhouse.io/acme/jobs/123",
            "https://job-boards.greenhouse.io/acme",
        ):
            with self.subTest(url=url):
                self.assertEqual(ats.detect_greenhouse(url), "acme")

    def test_greenhouse_eu_boards_are_marked(self):
        for url in (
            "https://boards.eu.greenhouse.io/acme",
            "https://job-boards.eu.greenhouse.io/acme",
            "https://job-boards.eu.greenhouse.io/acme/jobs/123",
        ):
            with self.subTest(url=url):
                self.assertEqual(ats.detect_greenhouse(url), "eu:acme")

    def test_greenhouse_embed_url_uses_the_for_parameter(self):
        url = "https://boards.greenhouse.io/embed/job_board?for=acme"
        self.assertEqual(ats.detect_greenhouse(url), "acme")


class GreenhouseRegionTests(unittest.TestCase):
    """EU and US Greenhouse tokens live in separate namespaces."""

    def capture_bases(self, token: str) -> list[str]:
        tried: list[str] = []

        def fake_get_json(url, params=None, **kwargs):
            tried.append(url)
            if "eu.greenhouse.io" in url and "found" not in url:
                pass
            return {"jobs": []}

        original = ats.get_json
        ats.get_json = fake_get_json
        try:
            ats.fetch_greenhouse(token, "Acme")
        finally:
            ats.get_json = original
        return tried

    def test_eu_tokens_hit_the_eu_api_first(self):
        tried = self.capture_bases("eu:acme")
        self.assertIn("boards-api.eu.greenhouse.io", tried[0])
        self.assertIn("/acme/jobs", tried[0])

    def test_plain_tokens_hit_the_us_api_first(self):
        tried = self.capture_bases("acme")
        self.assertIn("boards-api.greenhouse.io", tried[0])
        self.assertNotIn("eu.greenhouse.io", tried[0])

    def test_the_prefix_is_stripped_from_the_request_path(self):
        tried = self.capture_bases("eu:acme")
        self.assertNotIn("eu:", tried[0])

    def test_the_other_region_is_still_tried_as_a_fallback(self):
        calls: list[str] = []

        def fake_get_json(url, params=None, **kwargs):
            calls.append(url)
            if "boards-api.eu" in url:
                raise HttpError("HTTP 404")
            return {"jobs": [{"id": 1, "title": "Engineer", "absolute_url": "https://x"}]}

        original = ats.get_json
        ats.get_json = fake_get_json
        try:
            jobs = ats.fetch_greenhouse("eu:acme", "Acme")
        finally:
            ats.get_json = original

        self.assertEqual(len(calls), 2)
        self.assertEqual(len(jobs), 1)

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


class EmbeddedStateTests(unittest.TestCase):
    """Listings kept in a framework's JSON payload rather than in the markup.

    Redwood's page is 255KB with 22 links and 20 vacancies, all of them inside
    an Inertia data-page attribute.
    """

    @staticmethod
    def inertia(payload: dict) -> str:
        from html import escape

        blob = escape(json.dumps(payload)) + " " * 250  # clear the length floor
        return f'<html><body><div id="app" data-page="{blob}"></div></body></html>'

    VACANCIES = {
        "props": {
            "vacancies": [
                {
                    "id": 41,
                    "title": "Senior Publisher, Journals",
                    "slug": "senior-publisher-journals",
                    "location": {"name": "London"},
                    "description": "<p>Own a <b>journals</b> portfolio.</p>",
                    "published_at": "2026-08-01",
                },
                {
                    "id": 42,
                    "title": "Commissioning Editor",
                    "slug": "commissioning-editor",
                    "location": "Oxford",
                    "description": "Commission scholarly content.",
                    "published_at": "2026-08-10",
                },
            ]
        }
    }

    def extract(self, payload: dict, url="https://agency.example/vacancies"):
        return generic._from_embedded_state(self.inertia(payload), url, "Agency")

    def test_vacancies_are_extracted(self):
        jobs = self.extract(self.VACANCIES)
        self.assertEqual(len(jobs), 2)
        self.assertEqual(jobs[0].title, "Senior Publisher, Journals")
        self.assertEqual(jobs[0].external_id, "41")

    def test_nested_location_objects_are_flattened(self):
        jobs = self.extract(self.VACANCIES)
        self.assertEqual(jobs[0].location, "London")
        self.assertEqual(jobs[1].location, "Oxford")

    def test_descriptions_are_flattened_to_text(self):
        jobs = self.extract(self.VACANCIES)
        self.assertEqual(jobs[0].description, "Own a journals portfolio.")

    def test_slugs_are_resolved_beneath_the_listing_page(self):
        jobs = self.extract(self.VACANCIES)
        self.assertEqual(
            jobs[0].url, "https://agency.example/vacancies/senior-publisher-journals"
        )

    def test_an_explicit_url_is_preferred_over_a_slug(self):
        payload = {
            "props": {
                "items": [
                    {"id": 1, "title": "Publisher", "slug": "x", "url": "/roles/publisher-1"},
                    {"id": 2, "title": "Editor", "slug": "y", "url": "/roles/editor-2"},
                ]
            }
        }
        jobs = self.extract(payload)
        self.assertEqual(jobs[0].url, "https://agency.example/roles/publisher-1")

    def test_dates_are_parsed(self):
        jobs = self.extract(self.VACANCIES)
        self.assertTrue(jobs[0].posted_at.startswith("2026-08-01"))

    def test_navigation_menus_are_not_mistaken_for_vacancies(self):
        payload = {
            "props": {
                "menu": [
                    {"title": "About", "url": "/about"},
                    {"title": "Contact", "url": "/contact"},
                    {"title": "Careers", "url": "/careers"},
                ]
            }
        }
        # "About" and "Contact" read as navigation, leaving too few to qualify.
        self.assertEqual(self.extract(payload), [])

    def test_the_largest_job_like_array_wins(self):
        payload = {
            "props": {
                "featured": [
                    {"id": 1, "title": "Featured Publisher", "slug": "a"},
                    {"id": 2, "title": "Featured Editor", "slug": "b"},
                ],
                "vacancies": [
                    {"id": i, "title": f"Role {i}", "slug": f"role-{i}"} for i in range(9)
                ],
            }
        }
        jobs = self.extract(payload)
        self.assertEqual(len(jobs), 9)

    def test_bare_titles_with_no_detail_are_ignored(self):
        # Marketing blurbs carry a title and nothing else.
        payload = {"props": {"blurbs": [{"title": "We value people"}, {"title": "Our ethos"}]}}
        self.assertEqual(self.extract(payload), [])

    def test_a_posting_with_no_identifier_is_still_collected(self):
        # Losing a real vacancy is worse than admitting a stray record, so a
        # title plus posting-ish detail is enough even with no id or slug.
        payload = {
            "props": {
                "roles": [
                    {"title": "Senior Publisher", "location": "London"},
                    {"title": "Managing Editor", "location": "London"},
                ]
            }
        }
        jobs = self.extract(payload)
        self.assertEqual(sorted(j.title for j in jobs), ["Managing Editor", "Senior Publisher"])

    def test_a_single_entry_is_not_enough_to_qualify(self):
        payload = {"props": {"thing": [{"id": 1, "title": "Lone Item", "slug": "x"}]}}
        self.assertEqual(self.extract(payload), [])

    def test_a_location_stored_as_a_code_is_resolved(self):
        # Redwood's real shape: the posting names a code, and the readable
        # place sits in a separate list.
        payload = {
            "props": {
                "locations": [
                    {"shortcode": "ERSQLYRF4M", "name": "London"},
                    {"shortcode": "FWBEDGYBZQ", "name": "Oxford"},
                ],
                "vacancies": [
                    {"id": 1, "title": "Commissioning Editor", "location": "ERSQLYRF4M"},
                    {"id": 2, "title": "Journals Manager", "location": "FWBEDGYBZQ"},
                ],
            }
        }
        jobs = {j.title: j for j in self.extract(payload)}
        self.assertEqual(jobs["Commissioning Editor"].location, "London")
        self.assertEqual(jobs["Journals Manager"].location, "Oxford")

    def test_an_unresolvable_code_becomes_no_location(self):
        # Left as a code it would match no location rule and bin the posting;
        # blank means "unknown", which still passes.
        payload = {
            "props": {
                "vacancies": [
                    {"id": 1, "title": "Commissioning Editor", "location": "ERSQLYRF4M"},
                    {"id": 2, "title": "Journals Manager", "location": "DEKPFNRBXH"},
                ]
            }
        }
        for job in self.extract(payload):
            self.assertEqual(job.location, "")

    def test_real_place_names_are_untouched(self):
        payload = {
            "props": {
                "vacancies": [
                    {"id": 1, "title": "Commissioning Editor", "location": "London, UK"},
                    {"id": 2, "title": "Journals Manager", "location": "Oxford"},
                ]
            }
        }
        self.assertEqual(
            sorted(j.location for j in self.extract(payload)), ["London, UK", "Oxford"]
        )

    def test_shortcode_becomes_the_posting_url(self):
        payload = {
            "props": {
                "vacancies": [
                    {"id": 914889, "title": "Commissioning Editor", "shortcode": "UGWL85DIFO"},
                    {"id": 901637, "title": "Journals Manager", "shortcode": "3U7W7XHKEK"},
                ]
            }
        }
        jobs = {j.title: j for j in self.extract(payload)}
        self.assertEqual(
            jobs["Commissioning Editor"].url,
            "https://agency.example/vacancies/UGWL85DIFO",
        )

    def test_postings_keep_distinct_identities(self):
        payload = {
            "props": {
                "vacancies": [
                    {"id": 914889, "title": "Editor", "shortcode": "AAA111", "location": "X1Y2Z3A4"},
                    {"id": 901637, "title": "Editor", "shortcode": "BBB222", "location": "X1Y2Z3A4"},
                ]
            }
        }
        jobs = self.extract(payload)
        self.assertEqual(len({j.uid for j in jobs}), 2)

    def test_malformed_payloads_do_not_raise(self):
        html = '<html><body><div data-page="{not valid json' + "x" * 250 + '"></div></body></html>'
        self.assertEqual(generic._from_embedded_state(html, "https://x.example/", "A"), [])

    def test_a_page_with_no_payload_yields_nothing(self):
        self.assertEqual(
            generic._from_embedded_state("<html><body>hi</body></html>", "https://x/", "A"), []
        )

    def test_next_js_payloads_are_read_too(self):
        payload = {
            "props": {
                "pageProps": {
                    "jobs": [
                        {"id": "a", "title": "Managing Editor", "url": "/j/a"},
                        {"id": "b", "title": "Journals Manager", "url": "/j/b"},
                    ]
                }
            }
        }
        html = (
            '<script id="__NEXT_DATA__" type="application/json">'
            + json.dumps(payload)
            + " " * 250
            + "</script>"
        )
        jobs = generic._from_embedded_state(html, "https://x.example/careers", "X")
        self.assertEqual(sorted(j.title for j in jobs), ["Journals Manager", "Managing Editor"])

    def test_the_reader_prefers_structured_data_when_both_exist(self):
        # JSON-LD is authoritative; the payload is only a fallback.
        page = (
            '<script type="application/ld+json">'
            '{"@type":"JobPosting","title":"From JSON-LD","url":"/a"}'
            "</script>" + self.inertia(self.VACANCIES)
        )
        jobs = generic._from_jsonld(page, "https://agency.example/vacancies", "Agency")
        self.assertEqual([j.title for j in jobs], ["From JSON-LD"])


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

    def test_agency_wording_is_recognised_as_postings(self):
        # Search firms advertise "assignments", not "jobs".
        page = """
        <html><body>
          <a href="/current-assignments/senior-publisher-london/">Senior Publisher, London</a>
          <a href="/current-assignments/journals-director/">Journals Director</a>
          <a href="/opportunities/commissioning-editor">Commissioning Editor</a>
        </body></html>
        """
        jobs = generic._from_anchors(
            page, "https://www.morganhealey.com/current-assignments/", "Agency"
        )
        self.assertEqual(len(jobs), 3)

    def test_equal_opportunities_is_not_mistaken_for_an_opportunity(self):
        page = """
        <html><body>
          <a href="/current-assignments/senior-publisher/">Senior Publisher</a>
          <a href="/equal-opportunities-policy/">Equal Opportunities Statement</a>
          <a href="/privacy-policy/">Privacy</a>
          <a href="/jobs/cookie-policy">Cookies</a>
        </body></html>
        """
        jobs = generic._from_anchors(page, "https://example.com/current-assignments/", "Agency")
        self.assertEqual([job.title for job in jobs], ["Senior Publisher"])

    def test_a_job_word_in_the_domain_does_not_match_every_link(self):
        # charityjob.co.uk contains "job", so matching the whole URL made all
        # 259 links on the page look like postings, "Log in" among them.
        page = """
        <html><body>
          <a href="/careeradvice/">Career advice</a>
          <a href="/recruiters">Are you recruiting?</a>
          <a href="/account/signin?usertype=candidate">Log in</a>
          <a href="/account/register?usertype=candidate">Sign up</a>
          <a href="/volunteer-jobs/?source=nav">Volunteer</a>
          <a href="/about-us">About us</a>
          <a href="/jobs/12345/journals-manager">Journals Manager</a>
          <a href="/jobs/67890/publications-editor">Publications Editor</a>
        </body></html>
        """
        jobs = generic._from_anchors(
            page, "https://www.charityjob.co.uk/editorial-jobs", "CharityJob"
        )
        self.assertEqual(
            sorted(job.title for job in jobs), ["Journals Manager", "Publications Editor"]
        )

    def test_a_posting_on_a_job_named_domain_still_resolves(self):
        page = '<a href="/job/1/managing-editor">Managing Editor</a>'
        jobs = generic._from_anchors(page, "https://jobsearch.example.com/", "X")
        self.assertEqual([job.title for job in jobs], ["Managing Editor"])

    def test_rejection_reasons_are_reported_for_the_probe(self):
        # A path with a job word in it, excluded on its own merits.
        reason, _ = generic.link_rejection("/volunteer-jobs/", "Volunteer", "https://x.com/jobs")
        self.assertIn("excluded page", reason)
        # No job word at all is caught earlier, and reported differently.
        reason, _ = generic.link_rejection("/account/signin", "Log in", "https://x.com/jobs")
        self.assertIn("no job-like word", reason)
        reason, _ = generic.link_rejection("/about", "About us", "https://x.com/jobs")
        self.assertIn("no job-like word", reason)
        reason, absolute = generic.link_rejection(
            "/jobs/1/editor", "Managing Editor", "https://x.com/jobs"
        )
        self.assertIsNone(reason)
        self.assertEqual(absolute, "https://x.com/jobs/1/editor")

    def test_aggregator_board_furniture_is_not_mistaken_for_a_job(self):
        # An industry jobs board sits its own admin links among the listings.
        page = """
        <html><body>
          <a href="/careers-hub/jobs-board/submit-a-job/">Submit a job</a>
          <a href="/careers-hub/jobs-board/advertising/">Advertise</a>
          <a href="/careers-hub/jobs-board/">Jobs board</a>
          <a href="/careers-hub/jobs-board/publisher-3">Publisher</a>
          <a href="/careers-hub/jobs-board/editorial-registrar-1">Editorial Registrar</a>
        </body></html>
        """
        jobs = generic._from_anchors(page, "https://www.alpsp.org/careers-hub/", "ALPSP")
        titles = sorted(job.title for job in jobs)
        self.assertEqual(titles, ["Editorial Registrar", "Publisher"])


if __name__ == "__main__":
    unittest.main()
