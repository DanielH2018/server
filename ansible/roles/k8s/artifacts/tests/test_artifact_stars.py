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

    @pytest.fixture
    def serve(self, root):
        servers = []

        def start(store):
            handler = type(
                "StarHandler",
                (srv.Handler,),
                {"cache": srv.IndexCache(root, store), "stars": store},
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
