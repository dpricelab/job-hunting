import unittest
from datetime import datetime, timedelta, timezone

from jobhunt.config import DEFAULT_RULES
from jobhunt.models import Job
from jobhunt.scoring import score_job


def make_job(**kwargs) -> Job:
    defaults = dict(
        source="greenhouse",
        company="Acme",
        title="Senior Python Engineer",
        url="https://example.com/jobs/1",
        external_id="1",
        location="London, UK",
        description="We use Python and Postgres.",
    )
    defaults.update(kwargs)
    return Job(**defaults)


def rules(**overrides) -> dict:
    merged = dict(DEFAULT_RULES)
    merged.update(overrides)
    return merged


class TitleBypassTests(unittest.TestCase):
    """A strong body can carry a title the gate does not recognise.

    Publishers advertise roles whose titles say nothing about publishing —
    "Partnerships Manager" at BMJ, working on publications with journal
    editors — which a title gate rejects before ever reading the body.
    """

    BOILERPLATE = (
        "We publish The BMJ, one of the world's most impactful medical journals "
        "and over 70 influential speciality journals."
    )

    def rules(self, bypass):
        return rules(
            title_include=["editor", "publisher"],
            keywords={"editorial": 3, "journals": 2, "content": 1},
            title_bonus=2,
            min_score=1,
            title_bypass_score=bypass,
        )

    def score(self, title, description, bypass):
        return score_job(
            make_job(title=title, description=description, location=""),
            self.rules(bypass),
        )

    def test_a_gate_miss_is_kept_when_the_body_scores_high_enough(self):
        body = "Editorial collaboration with journals, content collections. " + self.BOILERPLATE
        verdict = self.score("Partnerships Manager", body, bypass=6)
        self.assertTrue(verdict.kept)
        self.assertGreaterEqual(verdict.score, 6)

    def test_the_same_posting_is_rejected_below_the_threshold(self):
        body = "Editorial collaboration with journals, content collections. " + self.BOILERPLATE
        verdict = self.score("Partnerships Manager", body, bypass=99)
        self.assertFalse(verdict.kept)
        self.assertIn("title_include", verdict.rejected)

    def test_boilerplate_alone_does_not_get_a_posting_through(self):
        # Every advert at a publisher mentions its journals; that must not be
        # enough on its own, or the gate stops meaning anything.
        verdict = self.score("Senior Sales Executive", self.BOILERPLATE, bypass=6)
        self.assertFalse(verdict.kept)
        self.assertIn("title_include", verdict.rejected)

    def test_zero_keeps_the_gate_absolute(self):
        body = "Editorial work on journals and content. " + self.BOILERPLATE
        verdict = self.score("Partnerships Manager", body, bypass=0)
        self.assertFalse(verdict.kept)

    def test_a_rejected_bypass_still_reports_its_score(self):
        # Seeing how close it came is what makes the threshold tunable.
        body = "Editorial work on journals. " + self.BOILERPLATE
        verdict = self.score("Partnerships Manager", body, bypass=99)
        self.assertGreater(verdict.score, 0)
        self.assertTrue(verdict.matched)

    def test_a_title_match_still_wins_regardless_of_the_bypass(self):
        # An impossible bypass threshold must not affect a title that matches.
        verdict = self.score("Managing Editor", "Editorial oversight of journals.", bypass=99)
        self.assertTrue(verdict.kept)

    def test_hard_exclusions_are_not_bypassed(self):
        # The bypass must not become a way around title_exclude.
        merged = self.rules(bypass=1)
        merged["title_exclude"] = ["intern"]
        body = "Editorial work on journals and content. " + self.BOILERPLATE
        verdict = score_job(
            make_job(title="Editorial Intern", description=body, location=""), merged
        )
        self.assertFalse(verdict.kept)
        self.assertIn("intern", verdict.rejected)

    def test_location_still_applies_to_a_bypassed_posting(self):
        merged = self.rules(bypass=1)
        merged["location_include"] = ["London"]
        body = "Editorial work on journals and content. " + self.BOILERPLATE
        verdict = score_job(
            make_job(title="Partnerships Manager", description=body, location="New York"),
            merged,
        )
        self.assertFalse(verdict.kept)
        self.assertIn("location", verdict.rejected)


class ScoringTests(unittest.TestCase):
    def test_keyword_in_title_earns_the_bonus(self):
        verdict = score_job(
            make_job(), rules(keywords={"python": 3}, title_bonus=2, min_score=0)
        )
        self.assertTrue(verdict.kept)
        self.assertEqual(verdict.score, 6)
        self.assertEqual(verdict.matched, ["python*"])

    def test_keyword_only_in_body_earns_base_weight(self):
        verdict = score_job(
            make_job(title="Senior Engineer"),
            rules(keywords={"postgres": 4}, title_bonus=2, min_score=0),
        )
        self.assertEqual(verdict.score, 4)
        self.assertEqual(verdict.matched, ["postgres"])

    def test_scores_accumulate_across_keywords(self):
        verdict = score_job(
            make_job(title="Engineer"),
            rules(keywords={"python": 3, "postgres": 2}, min_score=0),
        )
        self.assertEqual(verdict.score, 5)

    def test_word_boundaries_prevent_substring_hits(self):
        job = make_job(title="Goalkeeper Coach", description="Manage goals.")
        verdict = score_job(job, rules(keywords={"go": 5}, min_score=0))
        self.assertEqual(verdict.score, 0)

    def test_terms_with_punctuation_still_match(self):
        job = make_job(title="C++ Systems Engineer", description="Modern C++ work.")
        verdict = score_job(job, rules(keywords={"c++": 3}, title_bonus=1, min_score=0))
        self.assertEqual(verdict.score, 3)

    def test_regex_terms_are_supported(self):
        job = make_job(title="Staff Engineer, Platform")
        verdict = score_job(
            job, rules(keywords={"re:(staff|principal) engineer": 5}, title_bonus=1, min_score=0)
        )
        self.assertEqual(verdict.score, 5)

    def test_below_min_score_is_rejected_but_keeps_its_score(self):
        verdict = score_job(make_job(), rules(keywords={"python": 1}, title_bonus=1, min_score=10))
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.score, 1)
        self.assertIn("below min_score", verdict.rejected)

    def test_title_exclude_rejects_before_scoring(self):
        job = make_job(title="Python Engineering Intern")
        verdict = score_job(job, rules(keywords={"python": 9}, title_exclude=["intern"]))
        self.assertFalse(verdict.kept)
        self.assertIn("intern", verdict.rejected)

    def test_excluded_keyword_anywhere_rejects(self):
        job = make_job(description="This role requires on-call rotations and PHP.")
        verdict = score_job(job, rules(exclude_keywords=["php"]))
        self.assertFalse(verdict.kept)

    def test_title_include_acts_as_a_gate(self):
        job = make_job(title="Marketing Manager")
        verdict = score_job(job, rules(title_include=["engineer", "developer"]))
        self.assertFalse(verdict.kept)
        self.assertIn("title_include", verdict.rejected)

    def test_location_exclude_matches_location_only(self):
        job = make_job(location="New York, US", description="Our London office is lovely.")
        verdict = score_job(job, rules(location_exclude=["New York"]))
        self.assertFalse(verdict.kept)
        self.assertIn("New York", verdict.rejected)

    def test_the_trailing_country_code_rule_spares_uk_and_eu(self):
        # The rule in config.yaml that drops "Cambridge, MA" and "Oxford, OH".
        rule = [r"re:,\s*(?!gb\b|uk\b|eu\b)[a-z]{2}\s*$"]
        for place in ("Remote, EU", "London, UK", "London, GB"):
            verdict = score_job(make_job(location=place), rules(location_exclude=rule, min_score=0))
            self.assertTrue(verdict.kept, place)
        for place in ("Cambridge, MA", "Oxford, OH"):
            verdict = score_job(make_job(location=place), rules(location_exclude=rule, min_score=0))
            self.assertFalse(verdict.kept, place)

    def test_location_include_gates_stated_locations(self):
        job = make_job(location="Manchester, UK")
        verdict = score_job(job, rules(location_include=["London"]))
        self.assertFalse(verdict.kept)
        self.assertIn("location_include", verdict.rejected)

    def test_location_include_keeps_a_matching_location(self):
        job = make_job(location="London, UK")
        verdict = score_job(
            job, rules(location_include=["London"], keywords={"python": 1}, min_score=0)
        )
        self.assertTrue(verdict.kept)

    def test_a_posting_with_no_location_survives_the_location_filter(self):
        # Several boards omit location; rejecting those would hide good roles
        # for a reason the user never sees.
        job = make_job(location="")
        verdict = score_job(
            job, rules(location_include=["London"], keywords={"python": 1}, min_score=0)
        )
        self.assertTrue(verdict.kept)

    def test_remote_only_rejects_office_roles(self):
        verdict = score_job(make_job(location="London, UK"), rules(remote_only=True))
        self.assertFalse(verdict.kept)
        self.assertEqual(verdict.rejected, "not remote")

    def test_remote_only_keeps_remote_roles(self):
        job = make_job(location="Remote - Europe")
        verdict = score_job(job, rules(remote_only=True, keywords={"python": 1}, min_score=0))
        self.assertTrue(verdict.kept)

    def test_max_age_days_rejects_stale_postings(self):
        old = (datetime.now(timezone.utc) - timedelta(days=90)).isoformat()
        verdict = score_job(make_job(posted_at=old), rules(max_age_days=30))
        self.assertFalse(verdict.kept)
        self.assertIn("older than", verdict.rejected)

    def test_missing_posted_date_survives_the_age_filter(self):
        verdict = score_job(
            make_job(posted_at=None), rules(max_age_days=30, keywords={"python": 1}, min_score=0)
        )
        self.assertTrue(verdict.kept)


if __name__ == "__main__":
    unittest.main()
