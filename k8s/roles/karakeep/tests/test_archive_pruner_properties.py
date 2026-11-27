"""Properties of the archive pruner's `plan` over any mix of protections, ages and assets.

Run: uv run pytest k8s/roles/karakeep/tests/test_archive_pruner_properties.py
"""

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

import karakeep_archive_pruner as pruner

NOW = datetime(2026, 10, 12, 12, 0, tzinfo=UTC)
AUTO_TAGS = frozenset({"0-5m", "30m+"})
TAG_NAMES = (*sorted(AUTO_TAGS), "keep", "rust")
SECOND = timedelta(seconds=1)


def stamp(moment):
    return moment.isoformat().replace("+00:00", "Z")


@st.composite
def ages(draw, screenshot_days, delete_days):
    edges = [
        timedelta(days=d) + s
        for d in (screenshot_days, delete_days)
        for s in (-SECOND, timedelta(0), SECOND)
        if timedelta(days=d) + s >= timedelta(0)
    ]
    return draw(
        st.one_of(
            st.sampled_from(edges),
            st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=400)),
        )
    )


@st.composite
def worlds(draw):
    """A library of bookmarks, the two rule thresholds, and the set of highlighted ids."""
    screenshot_days = draw(st.integers(0, 120))
    delete_days = draw(st.integers(screenshot_days, 240))
    ids = draw(st.lists(st.text("abcdef", min_size=1, max_size=4), unique=True))
    bookmarks = [
        {
            "id": bid,
            "title": bid,
            "archived": True,
            "favourited": draw(st.booleans()),
            "note": draw(st.one_of(st.none(), st.just(" \n\t"), st.text(max_size=3))),
            "modifiedAt": stamp(NOW - draw(ages(screenshot_days, delete_days))),
            "tags": draw(
                st.lists(
                    st.fixed_dictionaries(
                        {
                            "name": st.sampled_from(TAG_NAMES),
                            "attachedBy": st.sampled_from(("ai", "human", None)),
                        }
                    ),
                    max_size=3,
                )
            ),
            "assets": [
                {"id": f"{bid}-{n}", "assetType": kind}
                for n, kind in enumerate(
                    draw(
                        st.lists(
                            st.sampled_from(("screenshot", "bannerImage")), max_size=3
                        )
                    )
                )
            ],
        }
        for bid in ids
    ]
    highlighted = draw(st.sets(st.sampled_from(ids))) if ids else set()
    return bookmarks, screenshot_days, delete_days, highlighted


def protected(b, highlighted):
    return (
        b["favourited"]
        or bool((b["note"] or "").strip())
        or any(
            t["attachedBy"] != "ai" and t["name"] not in AUTO_TAGS for t in b["tags"]
        )
        or b["id"] in highlighted
    )


def due(b, now, days):
    return now - pruner.parse_ts(b["modifiedAt"]) >= timedelta(days=days)


def run_plan(world, now=NOW, calls=None):
    bookmarks, screenshot_days, delete_days, highlighted = world

    def has_highlights(bid):
        if calls is not None:
            calls.append(bid)
        return bid in highlighted

    actions, _ = pruner.plan(
        bookmarks, now, screenshot_days, delete_days, AUTO_TAGS, has_highlights
    )
    return actions


def deleted(actions):
    return [a.bookmark_id for a in actions if a.kind == "delete"]


@given(worlds())
def test_the_deletes_are_exactly_the_due_bookmarks_no_protection_covers(world):
    bookmarks, _, delete_days, highlighted = world
    expected = {
        b["id"]
        for b in bookmarks
        if due(b, NOW, delete_days) and not protected(b, highlighted)
    }
    assert set(deleted(run_plan(world))) == expected


@given(worlds())
def test_no_bookmark_gets_more_than_one_delete(world):
    ids = deleted(run_plan(world))
    assert len(ids) == len(set(ids))


@given(worlds())
def test_highlights_are_read_only_for_due_bookmarks_nothing_else_protects(world):
    bookmarks, _, delete_days, _ = world
    calls = []
    run_plan(world, calls=calls)
    assert sorted(calls) == sorted(
        b["id"]
        for b in bookmarks
        if due(b, NOW, delete_days) and not protected(b, highlighted=())
    )


@given(worlds(), st.timedeltas(min_value=timedelta(0), max_value=timedelta(days=200)))
def test_the_delete_set_only_grows_as_time_passes(world, later):
    assert set(deleted(run_plan(world))) <= set(
        deleted(run_plan(world, now=NOW + later))
    )


@given(worlds())
def test_screenshots_are_detached_only_from_due_bookmarks_that_survive(world):
    bookmarks, screenshot_days, _, _ = world
    actions = run_plan(world)
    gone = set(deleted(actions))
    expected = {
        (b["id"], a["id"])
        for b in bookmarks
        if b["id"] not in gone and due(b, NOW, screenshot_days)
        for a in b["assets"]
        if a["assetType"] == "screenshot"
    }
    detached = [
        (a.bookmark_id, a.asset_id) for a in actions if a.kind == "detach-screenshot"
    ]
    assert sorted(detached) == sorted(expected)
