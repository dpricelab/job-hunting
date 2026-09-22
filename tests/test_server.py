"""The served digest, whose buttons write to the database.

Exercised over real HTTP against a real store, because the point of this
feature is the round trip: click, write, reload and see it gone.
"""

import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from jobhunt.models import Job
from jobhunt.scoring import Verdict
from jobhunt.server import serve
from jobhunt.store import Store


def job(title: str, external_id: str) -> Job:
    return Job(
        source="test",
        company="Acme",
        title=title,
        url=f"https://example.com/{external_id}",
        external_id=external_id,
        location="London",
    )


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db = Path(self.tmp.name) / "jobs.db"

        with Store(self.db) as store:
            store.upsert(
                [
                    (job("Managing Editor", "1"), Verdict(score=18)),
                    (job("Journals Manager", "2"), Verdict(score=16)),
                    (job("Head of Sales", "3"), Verdict(rejected="title excluded")),
                ]
            )
            self.uids = [row["uid"] for row in store.query()]

        self.server = serve(self.db, port=0)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def get(self, path="/"):
        with urllib.request.urlopen(self.base + path) as response:
            return response.status, response.read().decode("utf-8")

    def post(self, payload: dict, token: str | None = "use-real"):
        headers = {"Content-Type": "application/json"}
        if token is not None:
            headers["X-Jobhunt-Token"] = (
                self.server.token if token == "use-real" else token
            )
        request = urllib.request.Request(
            self.base + "/api/status",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def status_of(self, uid: str) -> str:
        with Store(self.db) as store:
            return store.resolve(uid)[0]["status"]

    # -- the page -------------------------------------------------------

    def test_the_page_lists_matching_roles(self):
        status, html = self.get()
        self.assertEqual(status, 200)
        self.assertIn("Managing Editor", html)
        self.assertIn("Journals Manager", html)

    def test_rejected_roles_are_not_served(self):
        _, html = self.get()
        self.assertNotIn("Head of Sales", html)

    def test_the_page_carries_buttons_and_a_token(self):
        _, html = self.get()
        self.assertIn('data-status="applied"', html)
        self.assertIn(self.server.token, html)

    def test_an_unknown_path_is_not_found(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.get("/secrets")
        self.assertEqual(caught.exception.code, 404)

    # -- marking --------------------------------------------------------

    def test_marking_applied_persists(self):
        uid = self.uids[0]
        status, payload = self.post({"uid": uid, "status": "applied"})
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.status_of(uid), "applied")

    def test_a_marked_role_leaves_the_page(self):
        uid = self.uids[0]
        title = "Managing Editor" if "Managing" in self.get()[1] else ""
        self.post({"uid": uid, "status": "applied"})
        _, html = self.get()
        self.assertNotIn(title, html)
        self.assertIn("Journals Manager", html)  # the other one stays

    def test_undo_puts_it_back(self):
        uid = self.uids[0]
        self.post({"uid": uid, "status": "applied"})
        self.post({"uid": uid, "status": "new"})
        self.assertEqual(self.status_of(uid), "new")
        self.assertIn("Managing Editor", self.get()[1])

    # -- refusals -------------------------------------------------------

    def test_a_request_without_the_token_is_refused(self):
        uid = self.uids[0]
        status, _ = self.post({"uid": uid, "status": "applied"}, token=None)
        self.assertEqual(status, 403)
        self.assertEqual(self.status_of(uid), "new")

    def test_a_request_with_the_wrong_token_is_refused(self):
        uid = self.uids[0]
        status, _ = self.post({"uid": uid, "status": "applied"}, token="guessed")
        self.assertEqual(status, 403)
        self.assertEqual(self.status_of(uid), "new")

    def test_an_invented_status_is_refused(self):
        uid = self.uids[0]
        status, payload = self.post({"uid": uid, "status": "hired"})
        self.assertEqual(status, 400)
        self.assertFalse(payload["ok"])
        self.assertEqual(self.status_of(uid), "new")

    def test_an_unknown_uid_is_refused(self):
        status, payload = self.post({"uid": "nosuchjob", "status": "applied"})
        self.assertEqual(status, 400)
        self.assertIn("no such role", payload["detail"])

    def test_a_missing_uid_is_refused(self):
        status, _ = self.post({"status": "applied"})
        self.assertEqual(status, 400)

    def test_a_body_that_is_not_json_is_refused(self):
        request = urllib.request.Request(
            self.base + "/api/status",
            data=b"not json at all",
            headers={
                "Content-Type": "application/json",
                "X-Jobhunt-Token": self.server.token,
            },
            method="POST",
        )
        with self.assertRaises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(request)
        self.assertEqual(caught.exception.code, 400)

    def test_the_server_binds_only_to_loopback(self):
        self.assertEqual(self.server.server_address[0], "127.0.0.1")

    # -- what counts as new -----------------------------------------------

    def test_new_badges_show_and_survive_a_reload(self):
        # The scheduled run writes its file without moving the marker, so the
        # server is what decides "new since you last looked".
        with Store(self.db) as store:
            store.meta_set("last_digest_at", "2000-01-01T00:00:00+00:00")
        other = serve(self.db, port=0)
        try:
            self.assertIn('class="flag new"', other.render().decode())
            self.assertIn('class="flag new"', other.render().decode())
        finally:
            other.server_close()

    def test_opening_the_page_moves_the_marker(self):
        with Store(self.db) as store:
            store.meta_set("last_digest_at", "2000-01-01T00:00:00+00:00")
        serve(self.db, port=0).server_close()
        with Store(self.db) as store:
            self.assertGreater(store.meta_get("last_digest_at"), "2000-01-01")
        # So the next session does not badge the same roles again.
        again = serve(self.db, port=0)
        try:
            self.assertNotIn('class="flag new"', again.render().decode())
        finally:
            again.server_close()

    def test_each_run_mints_a_fresh_token(self):
        other = serve(self.db, port=0)
        try:
            self.assertNotEqual(other.token, self.server.token)
            self.assertGreaterEqual(len(other.token), 20)
        finally:
            other.server_close()


if __name__ == "__main__":
    unittest.main()
