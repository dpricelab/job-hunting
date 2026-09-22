import tempfile
import unittest
from pathlib import Path

from jobhunt.models import Job
from jobhunt.scoring import Verdict
from jobhunt.store import Store


def job(title="Backend Engineer", external_id="1", company="Acme", **kwargs) -> Job:
    return Job(
        source="greenhouse",
        company=company,
        title=title,
        url=f"https://example.com/jobs/{external_id}",
        external_id=external_id,
        location=kwargs.pop("location", "London, UK"),
        **kwargs,
    )


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name) / "jobs.db")

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_insert_then_reinsert_counts_as_an_update(self):
        stats = self.store.upsert([(job(), Verdict(score=5))])
        self.assertEqual((stats.new, stats.updated), (1, 0))

        stats = self.store.upsert([(job(), Verdict(score=5))])
        self.assertEqual((stats.new, stats.updated), (0, 1))
        self.assertEqual(len(self.store.query()), 1)

    def test_first_seen_is_preserved_across_refetches(self):
        self.store.upsert([(job(), Verdict(score=5))])
        original = self.store.query()[0]["first_seen"]
        self.store.upsert([(job(title="Backend Engineer II"), Verdict(score=6))])
        row = self.store.query()[0]
        self.assertEqual(row["first_seen"], original)
        self.assertEqual(row["title"], "Backend Engineer II")
        self.assertEqual(row["score"], 6)

    def test_a_blank_refetch_keeps_the_stored_description(self):
        # A failed detail call yields no body; that is not the role changing.
        self.store.upsert([(job(description="Peer review of journals."), Verdict(score=5))])
        self.store.upsert([(job(description=""), Verdict(score=5))])
        self.assertEqual(self.store.query()[0]["description"], "Peer review of journals.")

    def test_descriptions_are_looked_up_by_uid(self):
        with_body = job(external_id="1", description="Journals.")
        without = job(external_id="2", description="")
        self.store.upsert([(with_body, Verdict()), (without, Verdict())])
        found = self.store.descriptions([with_body.uid, without.uid, "missing"])
        self.assertEqual(found, {with_body.uid: "Journals."})

    def test_rejected_jobs_are_hidden_from_the_default_query(self):
        self.store.upsert(
            [
                (job(external_id="1"), Verdict(score=5)),
                (job(external_id="2"), Verdict(rejected="not remote")),
            ]
        )
        self.assertEqual(len(self.store.query()), 1)
        self.assertEqual(len(self.store.query(rejected=True)), 1)

    def test_missing_jobs_are_closed_after_a_successful_fetch(self):
        self.store.upsert(
            [(job(external_id="1"), Verdict(score=5)), (job(external_id="2"), Verdict(score=5))]
        )
        stats = self.store.upsert(
            [(job(external_id="1"), Verdict(score=5))], close_missing_for="Acme"
        )
        self.assertEqual(stats.closed, 1)
        self.assertEqual(len(self.store.query()), 1)
        self.assertEqual(len(self.store.query(include_closed=True)), 2)

    def test_closing_only_affects_the_named_company(self):
        self.store.upsert([(job(external_id="1", company="Acme"), Verdict(score=5))])
        self.store.upsert([(job(external_id="9", company="Globex"), Verdict(score=5))])
        stats = self.store.upsert(
            [(job(external_id="9", company="Globex"), Verdict(score=5))],
            close_missing_for="Globex",
        )
        self.assertEqual(stats.closed, 0)
        self.assertEqual(len(self.store.query()), 2)

    def test_a_reappearing_job_is_reopened(self):
        self.store.upsert([(job(external_id="1"), Verdict(score=5))])
        self.store.upsert([], close_missing_for="Acme")
        self.assertEqual(len(self.store.query()), 0)

        self.store.upsert([(job(external_id="1"), Verdict(score=5))], close_missing_for="Acme")
        self.assertEqual(len(self.store.query()), 1)

    def test_query_filters(self):
        self.store.upsert(
            [
                (job(external_id="1", company="Acme", title="Python Engineer"), Verdict(score=9)),
                (job(external_id="2", company="Globex", title="Go Engineer"), Verdict(score=2)),
            ]
        )
        self.assertEqual(len(self.store.query(min_score=5)), 1)
        self.assertEqual(len(self.store.query(company="Globex")), 1)
        self.assertEqual(len(self.store.query(limit=1)), 1)

    def test_results_are_ordered_by_score(self):
        self.store.upsert(
            [
                (job(external_id="1", title="Low"), Verdict(score=1)),
                (job(external_id="2", title="High"), Verdict(score=8)),
            ]
        )
        self.assertEqual([r["title"] for r in self.store.query()], ["High", "Low"])

    def test_status_round_trip(self):
        self.store.upsert([(job(), Verdict(score=5))])
        uid = self.store.query()[0]["uid"]

        self.assertTrue(self.store.set_status(uid, "applied"))
        self.assertEqual(self.store.query(status="applied")[0]["uid"], uid)
        with self.assertRaises(ValueError):
            self.store.set_status(uid, "banana")

    def test_status_survives_a_refetch(self):
        self.store.upsert([(job(), Verdict(score=5))])
        uid = self.store.query()[0]["uid"]
        self.store.set_status(uid, "applied")
        self.store.upsert([(job(), Verdict(score=5))])
        self.assertEqual(self.store.query()[0]["status"], "applied")

    def test_resolve_by_uid_prefix(self):
        self.store.upsert([(job(), Verdict(score=5))])
        uid = self.store.query()[0]["uid"]
        self.assertEqual(len(self.store.resolve(uid[:6])), 1)
        self.assertEqual(len(self.store.resolve("zzzzzz")), 0)

    def test_counts(self):
        self.store.upsert(
            [
                (job(external_id="1"), Verdict(score=5)),
                (job(external_id="2"), Verdict(rejected="not remote")),
            ]
        )
        counts = self.store.counts()
        self.assertEqual(counts["total"], 2)
        self.assertEqual(counts["matching"], 1)
        self.assertEqual(counts["unseen"], 1)

    def test_meta_round_trip(self):
        self.assertIsNone(self.store.meta_get("last_digest_at"))
        self.store.meta_set("last_digest_at", "2024-01-01T00:00:00+00:00")
        self.assertEqual(self.store.meta_get("last_digest_at"), "2024-01-01T00:00:00+00:00")
        self.store.meta_set("last_digest_at", "2024-02-01T00:00:00+00:00")
        self.assertEqual(self.store.meta_get("last_digest_at"), "2024-02-01T00:00:00+00:00")

    def test_since_filter_drives_the_new_only_digest(self):
        self.store.upsert([(job(external_id="1"), Verdict(score=5))])
        first_seen = self.store.query()[0]["first_seen"]
        self.assertEqual(len(self.store.query(since=first_seen)), 0)
        self.assertEqual(len(self.store.query(since="2000-01-01T00:00:00+00:00")), 1)


class UidTests(unittest.TestCase):
    def test_uid_is_stable_when_the_url_gains_tracking_params(self):
        a = job()
        b = job()
        b.url = "https://example.com/jobs/1?utm_source=digest"
        self.assertEqual(a.uid, b.uid)

    def test_uid_differs_across_companies(self):
        self.assertNotEqual(job(company="Acme").uid, job(company="Globex").uid)

    def test_remote_is_inferred_from_the_location(self):
        self.assertTrue(job(location="Remote (UK)").remote)
        self.assertFalse(job(location="London, UK").remote)


if __name__ == "__main__":
    unittest.main()
