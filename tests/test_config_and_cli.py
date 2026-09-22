import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from jobhunt.cli import main
from jobhunt.config import Config, ConfigError
from jobhunt.util import html_to_text, slugify, to_iso, truncate


class UtilTests(unittest.TestCase):
    def test_html_to_text_strips_tags_and_entities(self):
        self.assertEqual(html_to_text("<p>Hello <b>world</b></p>"), "Hello world")
        self.assertEqual(html_to_text("A &amp; B"), "A & B")

    def test_html_to_text_handles_double_encoded_markup(self):
        self.assertEqual(html_to_text("&lt;p&gt;Nested&lt;/p&gt;"), "Nested")

    def test_html_to_text_turns_list_items_into_bullets(self):
        self.assertIn("• One", html_to_text("<ul><li>One</li><li>Two</li></ul>"))

    def test_html_to_text_drops_scripts(self):
        self.assertEqual(html_to_text("<script>evil()</script>Text"), "Text")

    def test_html_to_text_on_empty_input(self):
        self.assertEqual(html_to_text(None), "")
        self.assertEqual(html_to_text(""), "")

    def test_to_iso_accepts_the_shapes_boards_emit(self):
        self.assertTrue(to_iso("2024-10-02T09:00:00Z").startswith("2024-10-02"))
        self.assertTrue(to_iso("Wed, 02 Oct 2024 09:00:00 GMT").startswith("2024-10-02"))
        self.assertTrue(to_iso(1727859600).startswith("2024-10-02"))
        self.assertTrue(to_iso(1727859600000).startswith("2024-10-02"))

    def test_to_iso_rejects_nonsense(self):
        self.assertIsNone(to_iso(None))
        self.assertIsNone(to_iso(""))
        self.assertIsNone(to_iso("next Tuesday"))

    def test_a_bare_year_is_not_read_as_seconds_since_1970(self):
        self.assertIsNone(to_iso("2026"))
        self.assertIsNone(to_iso(2026))

    def test_a_compact_date_is_read_as_a_date(self):
        self.assertEqual(to_iso("20260930"), "2026-09-30T00:00:00+00:00")

    def test_naive_timestamps_are_treated_as_utc(self):
        self.assertEqual(to_iso("2024-10-02T09:00:00"), "2024-10-02T09:00:00+00:00")

    def test_slugify_and_truncate(self):
        self.assertEqual(slugify("Acme Corp!"), "acme-corp")
        self.assertEqual(slugify("Ünïcode Ltd"), "unicode-ltd")
        self.assertEqual(truncate("abcdefghij", 5), "abcd…")
        self.assertEqual(truncate("short", 20), "short")


class _EncodedStream:
    """A stdout stand-in reporting a given encoding.

    Not a StringIO subclass: `encoding` is read-only on the io types.
    """

    def __init__(self, encoding: str):
        self.encoding = encoding
        self._chunks: list[str] = []

    def write(self, text: str) -> int:
        self._chunks.append(text)
        return len(text)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return False

    def getvalue(self) -> str:
        return "".join(self._chunks)


class OutputEncodingTests(unittest.TestCase):
    """Printing must survive a terminal that cannot represent the symbols.

    Redirecting output to a file on Windows drops stdout to a legacy code
    page, which cannot encode the tick this CLI prints on success. That
    crashed the whole run partway through a fetch.
    """

    def setUp(self):
        from jobhunt import cli

        self.cli = cli
        self._saved = dict(cli.SYM)
        self._stdout = sys.stdout

    def tearDown(self):
        sys.stdout = self._stdout
        self.cli.SYM.clear()
        self.cli.SYM.update(self._saved)

    def configure_with(self, encoding: str):
        sys.stdout = _EncodedStream(encoding)
        self.cli._configure_output()

    def test_colour_is_off_when_output_is_not_a_terminal(self):
        # Redirecting to a file must not litter it with escape sequences.
        self.configure_with("utf-8")
        self.assertFalse(self.cli._use_color())
        self.assertEqual(self.cli.green("ok"), "ok")

    def test_enabling_ansi_never_raises(self):
        self.assertIn(self.cli._enable_ansi(), (True, False))

    def test_legacy_code_page_falls_back_to_ascii(self):
        self.configure_with("cp1252")
        self.assertEqual(self.cli.SYM["ok"], "OK")
        self.assertEqual(self.cli.SYM["fail"], "X")

    def test_utf8_keeps_the_nicer_symbols(self):
        self.configure_with("utf-8")
        self.assertEqual(self.cli.SYM["ok"], "✓")

    def test_every_fallback_symbol_is_ascii(self):
        self.configure_with("cp1252")
        for key, value in self.cli.SYM.items():
            with self.subTest(symbol=key):
                value.encode("ascii")  # raises if it is not

    def test_an_unknown_encoding_does_not_raise(self):
        self.configure_with("not-a-real-encoding")
        self.assertEqual(self.cli.SYM["ok"], "OK")

    def test_every_symbol_survives_the_reported_encoding(self):
        # The guarantee that matters: whatever is chosen must be printable.
        for encoding in ("cp1252", "ascii", "utf-8"):
            with self.subTest(encoding=encoding):
                self.configure_with(encoding)
                "".join(self.cli.SYM.values()).encode(encoding)


class NameInferenceTests(unittest.TestCase):
    """Display names are guessed from the URL when --name is not given."""

    def suggest(self, url: str) -> str:
        from jobhunt import sources
        from jobhunt.cli import _suggest_name

        _, token = sources.detect(url)
        return _suggest_name(url, token)

    def test_workday_uses_the_site_not_the_tenant(self):
        # The tenant is often a parent company: RELX hosts Elsevier's board.
        self.assertEqual(
            self.suggest("https://relx.wd3.myworkdayjobs.com/ElsevierJobs"), "Elsevier"
        )

    def test_workday_site_names_are_split_on_camel_case(self):
        self.assertEqual(
            self.suggest(
                "https://springernature.wd3.myworkdayjobs.com/SpringerNatureCareers"
            ),
            "Springer Nature",
        )

    def test_a_locale_segment_is_skipped(self):
        self.assertEqual(
            self.suggest("https://relx.wd3.myworkdayjobs.com/en-US/ElsevierJobs"), "Elsevier"
        )

    def test_workday_tokens_are_never_used_verbatim_as_names(self):
        # The token is a host/tenant/site triple; using it raw produced
        # "Relx.Wd3.Myworkdayjobs.Com/Relx/Elsevierjobs".
        name = self.suggest("https://relx.wd3.myworkdayjobs.com/ElsevierJobs")
        self.assertNotIn("/", name)
        self.assertNotIn(".", name)

    def test_other_boards_still_read_well(self):
        self.assertEqual(self.suggest("https://boards.greenhouse.io/acme-corp"), "Acme Corp")
        self.assertEqual(self.suggest("https://jobs.lever.co/acme"), "Acme")
        self.assertEqual(self.suggest("https://acme.recruitee.com/"), "Acme")
        self.assertEqual(
            self.suggest("https://www.northwind-data.com/careers"), "Northwind Data"
        )

    def test_rss_tokens_are_whole_urls_and_fall_back_to_the_host(self):
        self.assertEqual(self.suggest("https://example.com/jobs.rss"), "Example")

    def test_an_uninformative_site_name_falls_back_to_the_host(self):
        self.assertEqual(
            self.suggest("https://acme.wd1.myworkdayjobs.com/ExternalCareers"), "Acme"
        )


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "config.yaml"

    def tearDown(self):
        self.tmp.cleanup()

    def write(self, text: str) -> Config:
        self.path.write_text(text, encoding="utf-8")
        return Config.load(self.path)

    def test_missing_file_yields_defaults(self):
        config = Config.load(self.path)
        self.assertEqual(config.companies, [])
        self.assertEqual(config.rules["min_score"], 1)

    def test_companies_and_rules_load(self):
        config = self.write(
            """
            companies:
              - name: Acme
                url: https://boards.greenhouse.io/acme
                source: greenhouse
                token: acme
            rules:
              min_score: 4
              keywords:
                python: 3
            """
        )
        self.assertEqual(len(config.companies), 1)
        self.assertEqual(config.companies[0].name, "Acme")
        self.assertEqual(config.rules["min_score"], 4)
        # Unspecified rules still get their defaults.
        self.assertEqual(config.rules["title_bonus"], 2)

    def test_a_bare_url_string_is_accepted(self):
        config = self.write("companies:\n  - https://jobs.lever.co/acme\n")
        self.assertEqual(config.companies[0].url, "https://jobs.lever.co/acme")
        self.assertEqual(config.companies[0].name, "Acme")

    def test_keywords_may_be_given_as_a_plain_list(self):
        config = self.write("rules:\n  keywords: [python, rust]\n")
        self.assertEqual(config.rules["keywords"], {"python": 1, "rust": 1})

    def test_a_single_string_filter_is_wrapped_into_a_list(self):
        config = self.write("rules:\n  title_exclude: intern\n")
        self.assertEqual(config.rules["title_exclude"], ["intern"])

    def test_invalid_yaml_raises_config_error(self):
        with self.assertRaises(ConfigError):
            self.write("companies: [unclosed\n")

    def test_bad_keyword_weight_raises_config_error(self):
        with self.assertRaises(ConfigError):
            self.write("rules:\n  keywords:\n    python: heavy\n")

    def test_company_without_url_raises_config_error(self):
        with self.assertRaises(ConfigError):
            self.write("companies:\n  - name: Acme\n")

    def test_save_then_load_round_trips(self):
        config = self.write(
            "companies:\n  - url: https://jobs.lever.co/acme\n    name: Acme\n"
            "rules:\n  min_score: 7\n"
        )
        config.save()
        reloaded = Config.load(self.path)
        self.assertEqual(reloaded.companies[0].name, "Acme")
        self.assertEqual(reloaded.rules["min_score"], 7)

    def test_find_matches_by_name_or_substring(self):
        config = self.write(
            "companies:\n  - {name: Acme Corp, url: 'https://jobs.lever.co/acme'}\n"
        )
        self.assertIsNotNone(config.find("Acme Corp"))
        self.assertIsNotNone(config.find("acme"))
        self.assertIsNone(config.find("globex"))


class CliTests(unittest.TestCase):
    """End-to-end CLI checks that never touch the network."""

    def setUp(self):
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

    def test_init_writes_a_config(self):
        code, out = self.run_cli("init")
        self.assertEqual(code, 0)
        self.assertTrue(self.config.exists())
        self.assertIn("rules", self.config.read_text())

        code, out = self.run_cli("init")
        self.assertEqual(code, 1)
        self.assertIn("already exists", out)

    def test_add_detects_the_board_without_network_access(self):
        self.run_cli("init")
        code, out = self.run_cli(
            "add", "https://jobs.lever.co/acme", "--no-sniff", "--no-check"
        )
        self.assertEqual(code, 0)
        self.assertIn("lever", out)

        config = Config.load(self.config)
        self.assertEqual(config.companies[0].source, "lever")
        self.assertEqual(config.companies[0].token, "acme")

    def test_add_refuses_duplicates(self):
        self.run_cli("init")
        self.run_cli("add", "https://jobs.lever.co/acme", "--no-sniff", "--no-check")
        code, out = self.run_cli("add", "https://jobs.lever.co/acme", "--no-sniff", "--no-check")
        self.assertEqual(code, 1)
        self.assertIn("Already configured", out)

    def test_companies_lists_what_was_added(self):
        self.run_cli("init")
        self.run_cli(
            "add", "https://jobs.lever.co/acme", "--name", "Acme", "--no-sniff", "--no-check"
        )
        code, out = self.run_cli("companies")
        self.assertEqual(code, 0)
        self.assertIn("Acme", out)

    def test_fetch_with_no_companies_explains_itself(self):
        self.run_cli("init")
        code, out = self.run_cli("fetch")
        self.assertEqual(code, 1)
        self.assertIn("Add a company first", out)

    def test_digest_writes_a_page_even_with_no_results(self):
        self.run_cli("init")
        out_path = self.base / "digest.html"
        code, out = self.run_cli("digest", "--out", str(out_path))
        self.assertEqual(code, 0)
        self.assertTrue(out_path.exists())
        self.assertIn("Job digest", out_path.read_text())

    def test_stats_runs_on_an_empty_database(self):
        code, out = self.run_cli("stats")
        self.assertEqual(code, 0)
        self.assertIn("roles seen in total", out)

    def test_show_reports_a_missing_id(self):
        code, out = self.run_cli("show", "deadbeef")
        self.assertEqual(code, 1)
        self.assertIn("No job", out)


if __name__ == "__main__":
    unittest.main()
