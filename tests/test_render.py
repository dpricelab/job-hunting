"""The digest page itself: deadline wording, links and the buttons' markup."""

import re
import unittest
from datetime import datetime, timezone

from jobhunt.render import _closing_label, render_digest

# Mid-morning, so a naive "subtract the timestamps" gets date-only deadlines
# wrong in both directions.
NOW = datetime(2026, 9, 22, 10, 30, tzinfo=timezone.utc)


def row(**kwargs) -> dict:
    base = dict(
        uid="abc123def456",
        company="BMJ",
        title="Managing Editor",
        url="https://example.com/jobs/1",
        score=9,
        matched="[]",
        status="new",
        first_seen="2026-09-20T00:00:00+00:00",
    )
    base.update(kwargs)
    return base


class ClosingLabelTests(unittest.TestCase):
    def label(self, deadline):
        return _closing_label(deadline, now=NOW)

    def test_a_date_only_deadline_is_open_all_that_day(self):
        self.assertEqual(self.label("2026-09-22T00:00:00+00:00"), "Closes today")

    def test_a_date_only_deadline_tomorrow_says_tomorrow(self):
        self.assertEqual(self.label("2026-09-23T00:00:00+00:00"), "Closes tomorrow")

    def test_days_are_counted_in_calendar_days(self):
        self.assertEqual(self.label("2026-09-29T00:00:00+00:00"), "Closes in 7 days")

    def test_yesterday_is_closed(self):
        self.assertEqual(self.label("2026-09-21T00:00:00+00:00"), "Closed")

    def test_a_timed_deadline_that_has_passed_is_closed(self):
        self.assertEqual(self.label("2026-09-22T09:00:00+00:00"), "Closed")

    def test_a_timed_deadline_later_today_is_today(self):
        self.assertEqual(self.label("2026-09-22T17:00:00+00:00"), "Closes today")

    def test_nothing_is_said_without_a_usable_deadline(self):
        self.assertEqual(self.label(None), "")
        self.assertEqual(self.label("soon"), "")


class LinkTests(unittest.TestCase):
    def href(self, url):
        html = render_digest([row(url=url)])
        return re.search(r'<h2 class="title"><a href="([^"]*)"', html).group(1)

    def test_web_links_are_kept(self):
        self.assertEqual(self.href("https://example.com/jobs/1"), "https://example.com/jobs/1")

    def test_script_links_are_not_made_clickable(self):
        # Posting URLs come from the careers page, and the served page holds
        # the token that can change statuses.
        self.assertEqual(self.href("javascript:alert(1)"), "#")
        self.assertEqual(self.href(" JavaScript:alert(1)"), "#")
        self.assertEqual(self.href("data:text/html,hi"), "#")


class ButtonTests(unittest.TestCase):
    def test_undo_knows_what_to_restore(self):
        # With --all the page shows triaged roles too; undoing "Not for me" on
        # one already marked applied must put back "applied", not "new".
        html = render_digest([row(status="applied")], api_token="t")
        self.assertIn('data-was="applied"', html)

    def test_the_written_file_has_no_buttons(self):
        html = render_digest([row()])
        self.assertNotIn("data-status=", html)
        self.assertNotIn("jobhunt-token", html)


if __name__ == "__main__":
    unittest.main()
