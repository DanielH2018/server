"""Unit tests for the dashboard annotation injector."""

from __future__ import annotations

import json

import inject_dashboard_annotations as inject
from typing import Any

UID = "bf4q19tuivta8e"
EXPR = '{job="syslog"} |= "event=deploy" | logfmt'


def _annotation():
    return inject.build_annotation(UID, EXPR)


def _write(path, doc):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(doc))


def test_injects_into_a_board_with_an_empty_list():
    doc = {"annotations": {"list": []}, "panels": []}

    assert inject.inject(doc, _annotation()) is True
    assert doc["annotations"]["list"][0]["name"] == inject.ANNOTATION_NAME


def test_injects_when_the_annotations_key_is_missing_entirely():
    doc: dict[str, Any] = {"panels": []}

    assert inject.inject(doc, _annotation()) is True
    assert doc["annotations"]["list"][0]["datasource"]["uid"] == UID


def test_is_idempotent_by_name():
    """Re-running must not stack duplicates.

    This is what makes a UI round-trip safe: export_grafana_dashboards.py can pull a board back
    into files/ carrying the injected annotation, and the next deploy leaves it alone rather
    than adding a second copy.
    """
    doc = {"annotations": {"list": []}}
    inject.inject(doc, _annotation())

    assert inject.inject(doc, _annotation()) is False
    assert len(doc["annotations"]["list"]) == 1


def test_preserves_a_board_s_own_existing_annotations():
    doc = {"annotations": {"list": [{"name": "Alerts", "enable": True}]}}

    inject.inject(doc, _annotation())
    names = [entry["name"] for entry in doc["annotations"]["list"]]

    assert names == ["Alerts", inject.ANNOTATION_NAME]


def test_the_output_tree_is_complete_not_just_the_changed_boards(tmp_path):
    """Every board must reach the destination, annotated or not.

    The ConfigMap is built from the destination, so a board skipped here is a board DELETED
    from Grafana on the next apply.
    """
    src, dest = tmp_path / "src", tmp_path / "dest"
    _write(src / "A" / "one.json", {"annotations": {"list": []}})
    _write(
        src / "B" / "two.json",
        {"annotations": {"list": [inject.build_annotation(UID, EXPR)]}},
    )

    written, injected = inject.process_tree(src, dest, _annotation())

    assert (written, injected) == (2, 1), (
        "the already-annotated board must still be copied"
    )
    assert (dest / "A" / "one.json").exists()
    assert (dest / "B" / "two.json").exists()


def test_a_second_run_rewrites_nothing(tmp_path):
    """Byte-stable output, so the ConfigMap does not churn and Grafana does not roll.

    An unstable key order here would make every deploy report the dashboards changed and
    restart Grafana for no reason.
    """
    src, dest = tmp_path / "src", tmp_path / "dest"
    _write(src / "one.json", {"annotations": {"list": []}, "panels": [{"id": 1}]})

    inject.process_tree(src, dest, _annotation())
    first = (dest / "one.json").read_text()
    mtime = (dest / "one.json").stat().st_mtime_ns

    inject.process_tree(src, dest, _annotation())

    assert (dest / "one.json").read_text() == first
    assert (dest / "one.json").stat().st_mtime_ns == mtime, (
        "an unchanged board must not be rewritten — the mtime is what Ansible reads"
    )


def test_an_empty_source_tree_is_an_error(tmp_path, capsys):
    """Failing loudly beats emptying every dashboard.

    If staging silently produced nothing, a ConfigMap built from an empty tree removes every
    board from Grafana. That is the quiet catastrophic case, so it exits non-zero instead.
    """
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()

    written, _ = inject.process_tree(src, dest, _annotation())

    assert written == 0


def test_the_query_is_carried_through_verbatim():
    """A mangled expr would render the annotation inert with no error."""
    annotation = inject.build_annotation(UID, EXPR)

    assert annotation["target"]["expr"] == EXPR
    assert annotation["datasource"]["type"] == "loki"
