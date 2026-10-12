"""The archive pruner's rules, driven through main() against a recording fake of the REST API.

Every test runs the script end to end and asserts on the HTTP requests that reach the
transport, so the apply gate is checked where a DELETE would actually be sent.

Run: uv run pytest ansible/roles/k8s/karakeep/tests
"""

import ast
import io
import json
import urllib.parse
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml

import karakeep_archive_pruner

ROLE = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
ARGS = [
    "--screenshot-days=30",
    "--delete-days=90",
    "--max-deletes=50",
    "--auto-tag=0-5m",
]


def bookmark(bid, age_days, tags=(), assets=(), **extra):
    return {
        "id": bid,
        "title": bid,
        "archived": True,
        "favourited": False,
        "note": None,
        "modifiedAt": (NOW - timedelta(days=age_days))
        .isoformat()
        .replace("+00:00", "Z"),
        "tags": [{"id": n, "name": n, "attachedBy": by} for n, by in tags],
        "assets": [{"id": aid, "assetType": t} for aid, t in assets],
        **extra,
    }


class FakeKarakeep:
    """Serves one page of archived bookmarks, two pages of tags, and records every request."""

    def __init__(self, bookmarks, highlighted=(), tag_pages=((), ())):
        self.bookmarks = bookmarks
        self.highlighted = set(highlighted)
        self.tag_pages = [list(p) for p in tag_pages]
        self.calls = []

    @contextmanager
    def __call__(self, req, timeout):
        url = urllib.parse.urlparse(req.full_url)
        path = url.path.removeprefix("/api/v1/")
        self.calls.append((req.get_method(), path))
        body = None
        if path == "users/me":
            body = {"id": "u"}
        elif path == "bookmarks":
            assert urllib.parse.parse_qs(url.query)["archived"] == ["true"]
            body = {"bookmarks": self.bookmarks, "nextCursor": None}
        elif path == "tags":
            page = int(urllib.parse.parse_qs(url.query).get("cursor", ["0"])[0])
            last = page == len(self.tag_pages) - 1
            body = {
                "tags": self.tag_pages[page],
                "nextCursor": None if last else str(page + 1),
            }
        elif path.endswith("/highlights"):
            bid = path.split("/")[1]
            body = {"highlights": [{"id": "h"}] if bid in self.highlighted else []}
        yield io.BytesIO(json.dumps(body).encode() if body is not None else b"")

    def deletes(self):
        return [p for m, p in self.calls if m == "DELETE"]


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.setenv("KARAKEEP_API_URL", "http://karakeep:3000/api/v1")
    monkeypatch.setenv("KARAKEEP_API_KEY", "k")


def run(api, *extra):
    return karakeep_archive_pruner.main([*ARGS, *extra], opener=api, now=NOW)


def test_after_30_days_only_the_screenshot_is_detached():
    api = FakeKarakeep(
        [bookmark("b", 31, assets=[("s1", "screenshot"), ("i1", "bannerImage")])]
    )
    run(api, "--apply")
    assert api.deletes() == ["bookmarks/b/assets/s1"]


def test_before_30_days_nothing_is_touched():
    api = FakeKarakeep([bookmark("b", 29, assets=[("s1", "screenshot")])])
    run(api, "--apply")
    assert api.deletes() == []


def test_after_90_days_a_bookmark_with_only_ai_and_auto_tags_is_deleted_not_detached():
    api = FakeKarakeep(
        [
            bookmark(
                "b",
                91,
                tags=[("rust", "ai"), ("0-5m", "human")],
                assets=[("s1", "screenshot")],
            )
        ]
    )
    run(api, "--apply")
    assert api.deletes() == ["bookmarks/b"]


@pytest.mark.parametrize(
    "extra, highlighted",
    [
        ({"tags": [{"id": "t", "name": "keep", "attachedBy": "human"}]}, ()),
        ({"favourited": True}, ()),
        ({"note": "read again"}, ()),
        ({}, ("b",)),
    ],
    ids=["manual-tag", "favourited", "note", "highlights"],
)
def test_a_person_s_mark_protects_the_bookmark_but_not_its_screenshot(
    extra, highlighted
):
    b = bookmark("b", 200, assets=[("s1", "screenshot")])
    b.update(extra)
    api = FakeKarakeep([b], highlighted=highlighted)
    run(api, "--apply")
    assert api.deletes() == ["bookmarks/b/assets/s1"]


def test_a_dry_run_sends_no_delete():
    api = FakeKarakeep(
        [bookmark("old", 120), bookmark("mid", 40, assets=[("s1", "screenshot")])]
    )
    run(api)
    assert api.deletes() == []


def test_max_deletes_caps_deletions_per_run():
    api = FakeKarakeep([bookmark(f"b{i}", 100) for i in range(3)])
    karakeep_archive_pruner.main(
        ["--screenshot-days=30", "--delete-days=90", "--max-deletes=2", "--apply"],
        opener=api,
        now=NOW,
    )
    assert api.deletes() == ["bookmarks/b0", "bookmarks/b1"]


def test_a_bookmark_missing_its_tags_aborts_before_any_change():
    malformed = bookmark("b", 200)
    del malformed["tags"]
    api = FakeKarakeep([bookmark("ok", 200), malformed])
    with pytest.raises(SystemExit):
        run(api, "--apply")
    assert api.deletes() == []


def test_the_auto_tag_default_names_every_tag_the_time_tagger_attaches():
    tree = ast.parse((ROLE / "files" / "karakeep-time-tagger.py").read_text())
    attached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            getattr(t, "id", None) == "TIME_TAGS" for t in node.targets
        ):
            attached |= set(ast.literal_eval(node.value))
        if isinstance(node, ast.FunctionDef) and node.name == "estimate_reading_time":
            attached |= {
                r.value.value
                for r in ast.walk(node)
                if isinstance(r, ast.Return) and isinstance(r.value, ast.Constant)
            }
    assert {"0-1m", "0-5m", "30m+"} <= attached  # Both name sets were read.
    defaults = yaml.safe_load((ROLE / "defaults" / "main.yml").read_text())
    assert set(defaults["karakeep_k8s_pruner_auto_tags"]) == attached


def tag(name, human, ai=0):
    return {
        "id": name,
        "name": name,
        "numBookmarksByAttachedType": {"human": human, "ai": ai},
    }


def test_the_log_lists_every_manually_attached_tag_across_tag_pages(capsys):
    api = FakeKarakeep(
        [],
        tag_pages=(
            [tag("Read Later", 3), tag("rust", 0, ai=40)],
            [tag("Recipes", 12), tag("0-5m", 900)],
        ),
    )
    run(api)
    out = capsys.readouterr().out
    assert "tags with manual attachments, whole library: 3" in out
    assert "  900  0-5m  (listed as auto)" in out
    assert "   12  Recipes\n" in out
    assert "    3  Read Later\n" in out
    assert "rust" not in out
