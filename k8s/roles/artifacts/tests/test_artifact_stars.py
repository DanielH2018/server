"""Tests for starring: the star list, its place in the index, and POST /api/star.

A star exists to stop prune-artifacts.sh deleting an artifact, so the failures that matter
are the ones that lose a star silently: a write that clobbers the list, a toggle the cached
index never shows, and a request that writes a key no artifact has.
"""

import json
import os
import threading
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

import pytest

import artifact_server as srv
from lib.proc_testing import write_exec


@pytest.fixture
def root(tmp_path):
    (tmp_path / "tree" / "daniel-box").mkdir(parents=True)
    (tmp_path / "tree" / "daniel-server").mkdir()
    return tmp_path / "tree"


@pytest.fixture
def stars(tmp_path):
    return srv.StarStore(tmp_path / "stars.txt")


def write(root, host, name, body="<title>T</title>"):
    path = root / host / name
    path.write_text(body, encoding="utf-8")
    return path


@pytest.fixture
def serve(root):
    servers = []

    def start(store, swept_hosts=frozenset()):
        handler = type(
            "StarHandler",
            (srv.Handler,),
            {
                "cache": srv.IndexCache(root, store),
                "stars": store,
                "swept_hosts": swept_hosts,
            },
        )
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.01},
            daemon=True,
        ).start()
        servers.append(server)
        return HTTPConnection("127.0.0.1", server.server_address[1])

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


class TestStarStore:
    def test_a_missing_file_stars_nothing(self, stars):
        assert stars.load() == set()

    def test_mark_adds_and_removes_one_key(self, stars):
        stars.mark("daniel-box/a.html", True)
        stars.mark("daniel-server/b.html", True)
        stars.mark("daniel-box/a.html", False)
        assert stars.load() == {"daniel-server/b.html"}
        assert stars.path.read_text() == "daniel-server/b.html\n"

    def test_the_list_is_readable_by_other_users(self, stars):
        """The agent user's prune reads it as another uid; mkstemp alone would leave 0600."""
        stars.mark("daniel-box/a.html", True)
        assert stars.path.stat().st_mode & 0o777 == 0o644

    def test_mark_leaves_no_temp_file_behind(self, stars):
        stars.mark("daniel-box/a.html", True)
        assert [p.name for p in stars.path.parent.iterdir()] == ["stars.txt"]

    @pytest.mark.skipif(os.geteuid() == 0, reason="root reads a 0000 file")
    def test_an_unreadable_list_is_not_overwritten(self, stars):
        """Writing back what an unreadable file "held" would unstar every artifact at once."""
        stars.path.write_text("daniel-box/kept.html\n")
        stars.path.chmod(0)
        try:
            with pytest.raises(PermissionError):
                stars.mark("daniel-box/a.html", True)
        finally:
            stars.path.chmod(0o644)
        assert stars.path.read_text() == "daniel-box/kept.html\n"


class TestIndex:
    def test_entries_carry_their_star(self, root, stars):
        write(root, "daniel-box", "a.html")
        write(root, "daniel-server", "a.html")
        stars.mark("daniel-server/a.html", True)
        index = srv.build_index(root, known_services=[], stars=stars)
        starred = {(e["host"], e["path"]): e["starred"] for e in index["artifacts"]}
        assert starred == {
            ("daniel-box", "a.html"): False,
            ("daniel-server", "a.html"): True,
        }
        assert index["star_writable"] is True

    def test_a_read_only_store_shows_stars_but_is_not_writable(self, root, stars):
        write(root, "daniel-box", "a.html")
        stars.mark("daniel-box/a.html", True)
        readonly = srv.StarStore(stars.path, writable=False)
        index = srv.build_index(root, known_services=[], stars=readonly)
        assert index["artifacts"][0]["starred"] is True
        assert index["star_writable"] is False

    def test_no_store_stars_nothing(self, root):
        write(root, "daniel-box", "a.html")
        index = srv.build_index(root, known_services=[])
        assert index["artifacts"][0]["starred"] is False
        assert index["star_writable"] is False

    def test_the_cache_rebuilds_when_only_the_star_list_changes(self, root, stars):
        write(root, "daniel-box", "a.html")
        cache = srv.IndexCache(root, stars)
        assert cache.get()["artifacts"][0]["starred"] is False
        stars.mark("daniel-box/a.html", True)
        assert cache.get()["artifacts"][0]["starred"] is True


class TestStarEndpoint:
    """End-to-end over a real socket, with the store passed to a Handler subclass."""

    def post(self, conn, payload, ctype="application/json"):
        body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        conn.request("POST", "/api/star", body, {"Content-Type": ctype})
        res = conn.getresponse()
        return res.status, res.read()

    def index(self, conn):
        conn.request("GET", "/api/index.json")
        return json.loads(conn.getresponse().read())

    def test_star_then_unstar_round_trips_through_the_index(self, serve, root, stars):
        write(root, "daniel-server", "plan.html")
        conn = serve(stars)
        star = {"host": "daniel-server", "path": "plan.html", "starred": True}
        status, body = self.post(conn, star)
        assert status == 200, body
        assert json.loads(body) == star
        assert stars.load() == {"daniel-server/plan.html"}
        assert self.index(conn)["artifacts"][0]["starred"] is True

        assert self.post(conn, {**star, "starred": False})[0] == 200
        assert stars.load() == set()
        assert self.index(conn)["artifacts"][0]["starred"] is False

    def test_a_nested_path_is_stored_as_the_index_spells_it(self, serve, root, stars):
        (root / "daniel-box" / "sub").mkdir()
        write(root, "daniel-box", "sub/x.html")
        conn = serve(stars)
        payload = {"host": "daniel-box", "path": "/sub/./x.html", "starred": True}
        assert self.post(conn, payload)[0] == 200
        assert stars.load() == {"daniel-box/sub/x.html"}

    def test_a_form_post_is_refused(self, serve, root, stars):
        """Another origin can send text/plain without a preflight; only JSON is accepted."""
        write(root, "daniel-box", "a.html")
        conn = serve(stars)
        payload = {"host": "daniel-box", "path": "a.html", "starred": True}
        assert self.post(conn, payload, ctype="text/plain")[0] == 415
        assert stars.load() == set()

    @pytest.mark.parametrize(
        "payload",
        [
            {"host": "daniel-box", "path": "absent.html", "starred": True},
            {"host": "daniel-box", "path": "../daniel-server/b.html", "starred": True},
            {"host": "..", "path": "tree/daniel-box/a.html", "starred": True},
        ],
    )
    def test_a_key_naming_no_artifact_is_refused(self, serve, root, stars, payload):
        write(root, "daniel-box", "a.html")
        write(root, "daniel-server", "b.html")
        assert self.post(serve(stars), payload)[0] == 404
        assert stars.load() == set()

    @pytest.mark.parametrize(
        "payload",
        [
            b"not json",
            b"[]",
            json.dumps({"host": "daniel-box", "path": "a.html"}).encode(),
            json.dumps(
                {"host": "daniel-box", "path": "a.html", "starred": "yes"}
            ).encode(),
        ],
    )
    def test_a_malformed_body_is_refused(self, serve, root, stars, payload):
        write(root, "daniel-box", "a.html")
        assert self.post(serve(stars), payload)[0] == 400
        assert stars.load() == set()

    def test_starring_is_refused_without_a_writable_store(self, serve, root, stars):
        write(root, "daniel-box", "a.html")
        payload = {"host": "daniel-box", "path": "a.html", "starred": True}
        assert self.post(serve(None), payload)[0] == 403
        readonly = srv.StarStore(stars.path, writable=False)
        assert self.post(serve(readonly), payload)[0] == 403
        assert stars.load() == set()

    def test_post_to_another_route_is_404(self, serve, stars):
        conn = serve(stars)
        conn.request(
            "POST", "/api/index.json", b"{}", {"Content-Type": "application/json"}
        )
        assert conn.getresponse().status == 404


class TestStarOnArtifactPage:
    """The button a served artifact loads: its script tag, its script and its state read."""

    PAGE = "<html><body>plan</body></html>"
    OUTLINE = '<script src="/api/outline.js" defer></script>'
    TAG = (
        '<script src="/api/star.js" data-host="daniel-box" '
        'data-path="plan.html" defer></script>'
    )

    @pytest.fixture
    def get(self, serve):
        def request(store, path):
            conn = serve(store)
            conn.request("GET", path)
            res = conn.getresponse()
            return res.status, res.read()

        return request

    def test_no_star_list_adds_only_the_outline_tag(self, get, root):
        path = write(root, "daniel-box", "plan.html", self.PAGE)
        status, body = get(None, "/a/daniel-box/plan.html")
        assert status == 200
        assert body.decode() == f"<html><body>plan{self.OUTLINE}</body></html>"
        assert path.read_text() == self.PAGE

    def test_a_star_list_adds_the_tag_and_leaves_the_file_alone(self, get, root, stars):
        path = write(root, "daniel-box", "plan.html", self.PAGE)
        status, body = get(stars, "/a/daniel-box/plan.html")
        assert status == 200
        assert (
            body.decode() == f"<html><body>plan{self.OUTLINE}{self.TAG}</body></html>"
        )
        assert path.read_text() == self.PAGE

    def test_a_non_html_artifact_is_served_unchanged(self, get, root, stars):
        write(root, "daniel-box", "notes.md", "# Notes\n</body>")
        status, body = get(stars, "/a/daniel-box/notes.md")
        assert (status, body) == (200, b"# Notes\n</body>")

    @pytest.mark.parametrize(
        ("route", "script"),
        [("/api/star.js", srv.STAR_JS), ("/api/outline.js", srv.OUTLINE_JS)],
    )
    def test_each_script_route_serves_javascript(self, serve, route, script):
        conn = serve(None)
        conn.request("GET", route)
        res = conn.getresponse()
        assert res.status == 200
        # X-Content-Type-Options: nosniff makes a browser refuse a script of any other type.
        assert res.getheader("Content-Type") == "text/javascript; charset=utf-8"
        assert res.read() == script.encode()

    def test_get_star_reports_the_star_and_that_it_can_change(self, get, root, stars):
        write(root, "daniel-box", "plan.html")
        url = "/api/star?host=daniel-box&path=plan.html"
        assert json.loads(get(stars, url)[1]) == {
            "starred": False,
            "writable": True,
            "prunes_at": None,
        }
        stars.mark("daniel-box/plan.html", True)
        assert json.loads(get(stars, url)[1])["starred"] is True

    def test_get_star_on_a_swept_tree_reports_the_prune_date(self, serve, root, stars):
        path = write(root, "daniel-box", "plan.html")
        os.utime(path, (0, 1_800_000_000))
        conn = serve(stars, swept_hosts=frozenset({"daniel-box"}))
        conn.request("GET", "/api/star?host=daniel-box&path=plan.html")
        body = json.loads(conn.getresponse().read())
        # 1_800_000_000 is 2027-01-15T08:00:00Z; the default retention adds 30 days.
        assert body["prunes_at"] == "2027-02-14T08:00:00+00:00"

    def test_get_star_on_a_read_only_store_reports_not_writable(self, get, root, stars):
        write(root, "daniel-box", "plan.html")
        stars.mark("daniel-box/plan.html", True)
        readonly = srv.StarStore(stars.path, writable=False)
        status, body = get(readonly, "/api/star?host=daniel-box&path=plan.html")
        assert status == 200
        assert json.loads(body)["writable"] is False

    @pytest.mark.parametrize(
        "query",
        [
            "host=daniel-box&path=absent.html",
            "host=daniel-box&path=../daniel-server/b.html",
            "host=daniel-box",
            "path=plan.html",
            "host=daniel%00box&path=plan.html",
        ],
    )
    def test_get_star_naming_no_artifact_is_404(self, get, root, stars, query):
        write(root, "daniel-box", "plan.html")
        write(root, "daniel-server", "b.html")
        assert get(stars, f"/api/star?{query}")[0] == 404

    def test_get_star_with_starring_off_is_404(self, get, root):
        write(root, "daniel-box", "plan.html")
        assert get(None, "/api/star?host=daniel-box&path=plan.html")[0] == 404

    def test_the_tags_escape_their_attributes_and_append_without_a_body_close(self):
        assert srv.add_page_scripts(b"<p>x", "h", 'a"b.html', star=True) == (
            b'<p>x<script src="/api/outline.js" defer></script>'
            b'<script src="/api/star.js" data-host="h" '
            b'data-path="a&quot;b.html" defer></script>'
        )

    def test_the_tag_goes_before_the_last_body_close_in_any_case(self):
        body = srv.add_page_scripts(
            b"<pre></body></pre></BODY>", "h", "p.html", star=True
        )
        assert body.startswith(b"<pre></body></pre><script ")
        assert body.endswith(b"</script></BODY>")


class TestPrunesAt:
    """Which artifacts the star button counts down for: only those a sweep would delete."""

    SWEPT = frozenset({"daniel-box"})

    def test_a_tree_no_sweep_prunes_is_never_pruned(self, root):
        path = write(root, "daniel-server", "plan.html")
        assert srv.prunes_at("daniel-server", "plan.html", path, self.SWEPT) is None

    def test_an_executable_is_never_pruned(self, root):
        path = write_exec(root / "daniel-box" / "tool.sh", "true\n")
        assert srv.prunes_at("daniel-box", "tool.sh", path, self.SWEPT) is None

    def test_a_pinned_page_is_never_pruned(self, root):
        (root / "daniel-box" / "pinned").mkdir()
        path = write(root, "daniel-box", "pinned/map.html")
        assert srv.prunes_at("daniel-box", "pinned/map.html", path, self.SWEPT) is None
