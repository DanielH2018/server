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
        threading.Thread(target=server.serve_forever, daemon=True).start()
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
