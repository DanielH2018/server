"""Tests for the artifact index/server.

Covers the three things a wrong answer is invisible in: metadata extraction (a missed
`<title>` degrades silently to a filename slug), the mtime-keyed cache (a stale index
serves links to pruned files), and path confinement (a traversal escape hands out
arbitrary host files behind an authenticated route).
"""

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import artifact_server as srv  # noqa: E402


@pytest.fixture
def root(tmp_path):
    (tmp_path / "daniel-box").mkdir()
    (tmp_path / "daniel-server").mkdir()
    return tmp_path


def write(root, host, name, body):
    path = root / host / name
    path.write_text(body, encoding="utf-8")
    return path


ARTIFACT = """<!doctype html><html><head><meta charset="utf-8">
<title>Ansible k3s Drift Audit</title></head>
<body data-updated="2026-08-16 18:20">
<h1>Ansible k3s Drift Audit</h1>
<p>Nine roles disagree with the live cluster.</p>
<section data-slice="1" data-status="done"><h2>Slice 1</h2></section>
<section data-slice="2" data-status="active"><h2>Slice 2</h2></section>
<script>var noise = "should not be indexed";</script>
</body></html>"""


class TestParseHtml:
    def test_extracts_title_heading_and_updated(self):
        meta = srv.parse_html(ARTIFACT)
        assert meta["title"] == "Ansible k3s Drift Audit"
        assert meta["heading"] == "Ansible k3s Drift Audit"
        assert meta["updated"] == "2026-08-16 18:20"

    def test_counts_slices_by_status(self):
        assert srv.parse_html(ARTIFACT)["slices"] == {
            "total": 2,
            "done": 1,
            "active": 1,
            "planned": 0,
        }

    def test_summary_is_prose_not_a_repeat_of_the_heading(self):
        summary = srv.parse_html(ARTIFACT)["summary"]
        assert summary.startswith("Nine roles disagree")

    def test_meta_description_wins_over_derived_prose(self):
        body = '<html><head><title>T</title><meta name="description" content="Chosen."></head><body><p>Derived.</p></body></html>'
        assert srv.parse_html(body)["summary"] == "Chosen."

    def test_script_and_style_content_is_not_indexed(self):
        assert "should not be indexed" not in srv.parse_html(ARTIFACT)["text"]

    def test_excerpt_is_bounded(self, monkeypatch):
        monkeypatch.setattr(srv, "EXCERPT_CHARS", 20)
        assert len(srv.parse_html("<p>" + "x " * 500 + "</p>")["text"]) <= 20

    def test_entities_are_unescaped(self):
        assert srv.parse_html("<title>a &amp; b</title>")["title"] == "a & b"


class TestParseMarkdown:
    def test_title_from_first_heading_and_summary_from_first_prose(self):
        meta = srv.parse_markdown("# Free Tier Cloud\n\nWhat is still free.\n")
        assert meta["title"] == "Free Tier Cloud"
        assert meta["summary"] == "What is still free."


class TestSlugTitle:
    def test_strips_date_suffix_and_separators(self):
        assert (
            srv.slug_title("ansible-k3s-drift-audit_2026-08-16.html")
            == "ansible k3s drift audit"
        )

    def test_falls_back_to_the_name_when_nothing_remains(self):
        assert srv.slug_title("2026-08-16.html") == "2026-08-16.html"


class TestBuildIndex:
    def test_facets_come_from_what_the_corpus_holds(self, root):
        write(
            root,
            "daniel-box",
            "a.html",
            '<title>A</title><meta name="artifact:category" content="backup">'
            '<meta name="artifact:status" content="done">',
        )
        index = srv.build_index(root, known_services=KNOWN)
        assert index["categories"] == ["backup"]
        assert index["statuses"] == ["done"]

    def test_metadata_reaches_the_index_entry(self, root):
        write(
            root,
            "daniel-box",
            "a.html",
            "<title>Longhorn restore drill</title><p>longhorn restore ran</p>",
        )
        entry = srv.build_index(root, known_services=KNOWN)["artifacts"][0]
        assert "longhorn" in entry["services"]
        assert entry["source"]["services"] == "derived"

    def test_indexes_both_hosts_with_host_scoped_urls(self, root):
        write(root, "daniel-box", "a_2026-08-16.html", ARTIFACT)
        write(root, "daniel-server", "b.html", "<title>B</title>")
        index = srv.build_index(root)
        assert index["count"] == 2
        assert index["hosts"] == ["daniel-box", "daniel-server"]
        urls = {e["url"] for e in index["artifacts"]}
        assert urls == {"/a/daniel-box/a_2026-08-16.html", "/a/daniel-server/b.html"}

    def test_untitled_file_falls_back_to_its_filename_slug(self, root):
        write(root, "daniel-box", "no-title-doc.html", "<p>body</p>")
        assert srv.build_index(root)["artifacts"][0]["title"] == "no title doc"

    def test_non_artifact_files_are_skipped(self, root):
        write(root, "daniel-box", "notes.sqlite", "binary-ish")
        assert srv.build_index(root)["count"] == 0

    def test_markdown_with_an_html_twin_is_flagged_as_a_companion(self, root):
        write(root, "daniel-box", "doc.md", "# Doc\n")
        write(root, "daniel-box", "doc.html", "<title>Doc</title>")
        by_name = {e["name"]: e for e in srv.build_index(root)["artifacts"]}
        assert by_name["doc.md"]["companion_html"] is True
        assert by_name["doc.html"]["companion_html"] is False

    def test_lone_markdown_is_not_a_companion(self, root):
        write(root, "daniel-box", "solo.md", "# Solo\n")
        assert srv.build_index(root)["artifacts"][0]["companion_html"] is False

    def test_newest_first(self, root):
        old = write(root, "daniel-box", "old.html", "<title>Old</title>")
        new = write(root, "daniel-box", "new.html", "<title>New</title>")
        import os

        os.utime(old, (1_700_000_000, 1_700_000_000))
        os.utime(new, (1_800_000_000, 1_800_000_000))
        titles = [e["title"] for e in srv.build_index(root)["artifacts"]]
        assert titles == ["New", "Old"]

    def test_nested_subdirectories_keep_their_relative_path(self, root):
        (root / "daniel-box" / "sub").mkdir()
        write(root, "daniel-box", "sub/deep.html", "<title>Deep</title>")
        assert (
            srv.build_index(root)["artifacts"][0]["url"]
            == "/a/daniel-box/sub/deep.html"
        )

    def test_missing_root_indexes_empty_rather_than_raising(self, tmp_path):
        assert srv.build_index(tmp_path / "absent")["count"] == 0

    def test_loose_files_at_the_root_are_ignored(self, root):
        (root / "stray.html").write_text("<title>Stray</title>")
        assert srv.build_index(root)["count"] == 0


KNOWN = [
    "longhorn",
    "traefik",
    "nut",
    "registry",
    "sonarr",
    "wg-easy",
    "kopia",
    "happy",
]


class TestDeriveServices:
    def test_word_match_tags_the_service(self):
        assert "longhorn" in srv.derive_services("The longhorn volume failed", KNOWN)

    def test_a_substring_is_not_a_match(self):
        """`nut` lives inside "minute", which appears in nearly every artifact here.

        Measured 2026-08-19 over the real corpus: substring matching tagged 33 of 48
        documents `nut`, word matching 16. This is the assertion that pins the difference.
        """
        assert (
            srv.derive_services(
                "The job runs every 5 minutes, give or take a minute", KNOWN
            )
            == []
        )

    def test_ambiguous_name_needs_two_mentions(self):
        assert srv.derive_services("pushed it to the container registry", KNOWN) == []
        assert "registry" in srv.derive_services(
            "the registry rejected the push; registry GC ran", KNOWN
        )

    def test_unambiguous_name_needs_only_one(self):
        assert srv.derive_services("sonarr could not import", KNOWN) == ["sonarr"]

    def test_hyphenated_names_match(self):
        assert "wg-easy" in srv.derive_services("wg-easy lost its peer", KNOWN)

    def test_retired_names_are_still_matched(self):
        assert "kopia" in srv.derive_services("the kopia repository was retired", KNOWN)

    def test_an_ordinary_word_service_name_does_not_tag_prose(self):
        """`happy` is a retired service AND an English word — one use must not tag it."""
        assert srv.derive_services("the result was a happy outcome", KNOWN) == []

    def test_matching_is_case_insensitive(self):
        assert "traefik" in srv.derive_services("Traefik routed it", KNOWN)

    def test_most_mentioned_first_and_capped(self, monkeypatch):
        monkeypatch.setattr(srv, "MAX_SERVICES", 2)
        text = "sonarr sonarr sonarr traefik traefik longhorn"
        assert srv.derive_services(text, KNOWN + ["longhorn"]) == ["sonarr", "traefik"]

    def test_no_known_services_yields_nothing(self):
        assert srv.derive_services("longhorn traefik sonarr", []) == []


class TestDeriveCategory:
    def test_title_outweighs_body(self):
        """A document is about what its title says, even against a body full of other words.

        The body here scores `backup` several times over; the title scores `cost` twice. The
        title's 5x weight is what decides it.
        """
        cat = srv.derive_category(
            "Free tier spend review", "the backup and restore of the longhorn snapshot"
        )
        assert cat == "cost"

    def test_infra_is_a_fallback_not_a_competitor(self):
        """Every document here says pod/cluster/deploy, so infra must never outrank a
        specific category — measured, it beat `cost` 4-3 before this rule existed."""
        assert (
            srv.derive_category(
                "Grafana alert rules", "the pod on the node in the cluster deploy"
            )
            == "monitoring"
        )

    def test_infra_still_wins_when_nothing_specific_scores(self):
        assert (
            srv.derive_category("Ansible role layout", "the cluster deploy") == "infra"
        )

    def test_security_wins_on_its_own_words(self):
        assert (
            srv.derive_category("Authelia session audit", "sso and permission review")
            == "security"
        )

    def test_no_keywords_yields_no_category(self):
        assert srv.derive_category("Grocery list", "apples and pears") == ""


class TestDeriveStatus:
    def test_any_active_slice_makes_the_document_active(self):
        assert srv.derive_status({"done": 2, "active": 1, "planned": 3}) == "active"

    def test_all_done_makes_it_done(self):
        assert srv.derive_status({"done": 3, "active": 0, "planned": 0}) == "done"

    def test_remaining_planned_work_is_planned(self):
        assert srv.derive_status({"done": 1, "active": 0, "planned": 2}) == "planned"

    def test_no_slices_yields_no_status(self):
        assert srv.derive_status(None) == ""
        assert srv.derive_status({}) == ""


class TestDeclaredMetadata:
    def test_html_meta_tags(self):
        body = (
            '<meta name="artifact:category" content="security">'
            '<meta name="artifact:status" content="done">'
            '<meta name="artifact:services" content="authelia, traefik">'
            '<meta name="artifact:tags" content="sso,audit">'
        )
        meta = srv.declared_metadata(body)
        assert meta["category"] == "security"
        assert meta["status"] == "done"
        assert meta["services"] == ["authelia", "traefik"]
        assert meta["tags"] == ["sso", "audit"]

    def test_markdown_frontmatter(self):
        body = "---\ncategory: backup\nstatus: active\nservices: longhorn, b2\n---\n\n# Doc\n"
        meta = srv.declared_metadata(body, is_markdown=True)
        assert meta["category"] == "backup"
        assert meta["status"] == "active"
        assert meta["services"] == ["longhorn", "b2"]

    def test_absent_metadata_is_empty(self):
        assert srv.declared_metadata("<p>nothing here</p>") == {}

    def test_markdown_without_frontmatter_is_empty(self):
        assert srv.declared_metadata("# Just a heading\n", is_markdown=True) == {}


class TestApplyMetadata:
    def test_declared_beats_derived_and_is_marked_declared(self):
        entry = {"title": "cost review", "text": "b2 spend cap", "slices": {"done": 1}}
        srv.apply_metadata(
            entry, '<meta name="artifact:category" content="security">', False, KNOWN
        )
        assert entry["category"] == "security"
        assert entry["source"]["category"] == "declared"

    def test_derived_values_are_marked_derived(self):
        entry = {"title": "B2 cost review", "text": "spend cap billing", "slices": None}
        srv.apply_metadata(entry, "<p>x</p>", False, KNOWN)
        assert entry["category"] == "cost"
        assert entry["source"]["category"] == "derived"

    def test_a_field_with_neither_is_absent_rather_than_guessed_empty(self):
        entry = {"title": "Grocery list", "text": "apples", "slices": None}
        srv.apply_metadata(entry, "<p>x</p>", False, KNOWN)
        assert "category" not in entry
        assert "status" not in entry
        assert entry["source"] == {}

    def test_tags_are_declared_only(self):
        entry = {"title": "t", "text": "longhorn", "slices": None}
        srv.apply_metadata(entry, "<p>x</p>", False, KNOWN)
        assert "tags" not in entry


class TestKnownServicesFile:
    def test_reads_and_normalises_the_rendered_list(self, tmp_path):
        path = tmp_path / "known_services.json"
        path.write_text('["Longhorn", "traefik", "traefik", ""]')
        assert srv.load_known_services(path) == ["longhorn", "traefik"]

    def test_missing_file_is_not_fatal(self, tmp_path):
        assert srv.load_known_services(tmp_path / "absent.json") == []

    def test_malformed_file_is_not_fatal(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("{not json")
        assert srv.load_known_services(path) == []


class TestIndexCache:
    def test_reuses_the_index_when_nothing_changed(self, root):
        write(root, "daniel-box", "a.html", "<title>A</title>")
        cache = srv.IndexCache(root)
        assert cache.get() is cache.get()

    def test_rebuilds_when_a_file_is_added(self, root):
        write(root, "daniel-box", "a.html", "<title>A</title>")
        cache = srv.IndexCache(root)
        first = cache.get()
        write(root, "daniel-box", "b.html", "<title>B</title>")
        assert cache.get() is not first
        assert cache.get()["count"] == 2

    def test_rebuilds_when_a_file_is_pruned(self, root):
        path = write(root, "daniel-box", "a.html", "<title>A</title>")
        cache = srv.IndexCache(root)
        cache.get()
        path.unlink()
        assert cache.get()["count"] == 0

    def test_rebuilds_when_content_changes_in_place(self, root):
        path = write(root, "daniel-box", "a.html", "<title>A</title>")
        cache = srv.IndexCache(root)
        cache.get()
        path.write_text("<title>A rewritten with more bytes</title>")
        assert cache.get()["artifacts"][0]["title"] == "A rewritten with more bytes"


class TestSafePath:
    def test_resolves_a_real_artifact(self, root):
        write(root, "daniel-box", "a.html", "x")
        assert srv.safe_path(root, "daniel-box", "a.html").name == "a.html"

    @pytest.mark.parametrize(
        "host,rel",
        [
            ("daniel-box", "../daniel-server/secret.html"),
            ("daniel-box", "../../etc/passwd"),
            ("..", "etc/passwd"),
            ("", "a.html"),
            ("daniel-box/../..", "etc/passwd"),
            ("daniel-box", "/etc/passwd"),
        ],
    )
    def test_refuses_to_escape_the_host_tree(self, root, host, rel):
        write(root, "daniel-server", "secret.html", "x")
        assert srv.safe_path(root, host, rel) is None

    def test_refuses_a_symlink_pointing_out_of_the_tree(self, root, tmp_path):
        outside = tmp_path.parent / "outside.html"
        outside.write_text("secret")
        (root / "daniel-box" / "link.html").symlink_to(outside)
        assert srv.safe_path(root, "daniel-box", "link.html") is None

    def test_missing_file_is_none(self, root):
        assert srv.safe_path(root, "daniel-box", "absent.html") is None

    def test_a_directory_is_not_servable(self, root):
        (root / "daniel-box" / "sub").mkdir()
        assert srv.safe_path(root, "daniel-box", "sub") is None


class TestServing:
    """End-to-end over a real socket — the handler wiring is where a route typos itself."""

    @pytest.fixture
    def client(self, root, monkeypatch):
        import threading
        from http.client import HTTPConnection
        from http.server import ThreadingHTTPServer

        monkeypatch.setattr(srv, "ROOT", root)
        srv.Handler.cache = srv.IndexCache(root)
        server = ThreadingHTTPServer(("127.0.0.1", 0), srv.Handler)
        # shutdown() blocks until serve_forever notices, which it does once per poll interval —
        # 0.5s by default, paid by every test in this class as teardown. Each test still gets
        # its own server, because each mutates its own `root`.
        threading.Thread(
            target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True
        ).start()
        conn = HTTPConnection("127.0.0.1", server.server_address[1])
        yield conn
        conn.close()
        server.shutdown()
        server.server_close()

    def get(self, client, path):
        client.request("GET", path)
        res = client.getresponse()
        return res.status, res.read(), dict(res.getheaders())

    def test_root_serves_the_gui(self, client):
        status, body, headers = self.get(client, "/")
        assert status == 200
        assert b"Homelab Artifacts" in body
        assert headers["Content-Type"].startswith("text/html")

    def test_healthz(self, client):
        assert self.get(client, "/healthz")[0] == 200

    def test_index_json_lists_the_artifacts(self, client, root):
        write(root, "daniel-box", "a.html", ARTIFACT)
        status, body, headers = self.get(client, "/api/index.json")
        assert status == 200
        assert headers["Content-Type"] == "application/json"
        payload = json.loads(body)
        assert payload["artifacts"][0]["title"] == "Ansible k3s Drift Audit"

    def test_serves_an_artifact_body(self, client, root):
        write(root, "daniel-box", "a.html", ARTIFACT)
        status, body, headers = self.get(client, "/a/daniel-box/a.html")
        assert status == 200
        assert b"Nine roles disagree" in body
        assert headers["Content-Type"].startswith("text/html")

    def test_percent_encoded_traversal_is_refused(self, client, root):
        write(root, "daniel-server", "secret.html", "secret")
        assert (
            self.get(client, "/a/daniel-box/%2e%2e/daniel-server/secret.html")[0] == 404
        )

    def test_encoded_space_in_a_filename_resolves(self, client, root):
        write(root, "daniel-box", "two words.html", "<title>Two</title>")
        assert self.get(client, "/a/daniel-box/two%20words.html")[0] == 200

    def test_unknown_route_is_404(self, client):
        assert self.get(client, "/nope")[0] == 404

    def test_responses_are_marked_noindex(self, client):
        assert self.get(client, "/")[2]["X-Robots-Tag"] == "noindex, nofollow"

    def test_query_string_is_ignored_when_routing(self, client, root):
        write(root, "daniel-box", "a.html", "<title>A</title>")
        assert self.get(client, "/a/daniel-box/a.html?v=1")[0] == 200
