#!/usr/bin/env python3
"""The rendered `concurrent-automatic-engine-upgrade-per-node-limit` Setting must be above 0.

Exit part 1 of the H1 plan needs every volume's engine to follow the manager after a Longhorn
upgrade. At `0` Longhorn never upgrades an engine on its own, so each hop leaves the previous
engine image referenced and the lag compounds across the ladder
(`docs/longhorn-upgrade.md`, step 4). Upstream ships `0`; this repo sets `3` through
`k3s_longhorn_concurrent_engine_upgrade`, so a defaults edit back to `0` is the regression.

`_limit_offences` is the check, over `(role, doc)` pairs. The clean and flagged tests below are
its accept and reject halves, and `NAMED` is the member it must find: a render with no such
Setting in the `longhorn` role fails rather than passing over nothing.

Run: uv run pytest ansible/tests/longhorn/test_engine_upgrade_limit_is_positive.py
"""

from _k8s_render import rendered_docs

SETTING = "concurrent-automatic-engine-upgrade-per-node-limit"
NAMED = ("longhorn", SETTING)


def _setting(role: str, value: object, name: str = SETTING) -> tuple[str, dict]:
    return role, {
        "apiVersion": "longhorn.io/v1beta2",
        "kind": "Setting",
        "metadata": {"name": name, "namespace": "longhorn-system"},
        "value": value,
    }


def _limit_offences(docs: list[tuple[str, dict]]) -> list[str]:
    """Every reason the engine-upgrade limit in `docs` is missing or not above 0."""
    members = {
        (role, doc["metadata"]["name"]): doc
        for role, doc in docs
        if doc.get("kind") == "Setting"
    }
    doc = members.get(NAMED)
    if doc is None:
        return [f"the {NAMED[0]} render holds no Setting {SETTING!r}"]
    try:
        limit = int(doc["value"])
    except TypeError, ValueError:
        return [f"{SETTING} renders {doc['value']!r}, which is not an integer"]
    if limit <= 0:
        return [
            f"{SETTING} renders {limit}, so engines never follow the manager "
            "and the old engine image keeps its references after an upgrade"
        ]
    return []


def test_the_real_render_is_clean() -> None:
    docs = [(role, doc) for role, _tpl, doc in rendered_docs()]
    assert _limit_offences(docs) == []


def test_a_zero_limit_is_flagged() -> None:
    assert _limit_offences([_setting("longhorn", "0")]) != []


def test_a_positive_limit_is_clean() -> None:
    assert _limit_offences([_setting("longhorn", "3")]) == []


def test_a_render_without_the_setting_is_flagged() -> None:
    other = _setting("longhorn", "3", name="default-replica-count")
    assert _limit_offences([other]) != []
    assert _limit_offences([_setting("metallb", "3")]) != []
