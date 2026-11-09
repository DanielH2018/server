"""How a promtool rule-test file is split into parts that run in parallel.

No promtool runs here. Each test parses the parts with PyYAML, which also refuses an alias whose
anchor is in another part, so a split promtool would reject fails here first.

Run: uv run pytest scripts/validate/tests/test_rule_test_parts.py
"""

from pathlib import Path


from lib import yaml_fast

from validate.alert_conventions import RULE_TESTS_DIR
from validate.validate_lib.rule_test_parts import split_rule_test


def _groups(eval_times: list[str], anchors: dict[int, str] | None = None) -> str:
    """A rule-test file with one group per eval time; `anchors` maps an index to an extra line."""
    anchors = anchors or {}
    text = "rule_files:\n  - rules.yaml\nevaluation_interval: 1m\ntests:\n"
    for i, eval_time in enumerate(eval_times):
        text += (
            f"  # group {i}\n"
            "  - interval: 1m\n"
            "    input_series: []\n"
            "    alert_rule_test:\n"
            f"      - eval_time: {eval_time}\n"
            f"        alertname: Alert{i}\n"
            f"{anchors.get(i, '')}"
        )
    return text


def _write(tmp_path: Path, text: str) -> Path:
    source = tmp_path / "src" / "thing.test.yaml"
    source.parent.mkdir()
    source.write_text(text)
    (tmp_path / "out").mkdir()
    return source


def test_a_split_of_the_largest_test_file_runs_every_group_once_is_clean(tmp_path):
    source = RULE_TESTS_DIR / "host-checks.test.yaml"
    original = yaml_fast.safe_load(source.read_text())

    parts = split_rule_test(source, 4, tmp_path)

    assert len(parts) == 4, parts
    seen = []
    for path, indices in parts:
        doc = yaml_fast.safe_load(path.read_text())
        assert doc["evaluation_interval"] == original["evaluation_interval"]
        assert all(
            Path(f).is_absolute() and Path(f).is_file() for f in doc["rule_files"]
        )
        assert doc["tests"] == [original["tests"][i] for i in indices]
        seen += indices
    assert sorted(seen) == list(range(len(original["tests"])))


def test_groups_sharing_an_anchor_stay_in_one_part_in_source_order(tmp_path):
    anchors = {
        0: "        exp_alerts: &shared\n          - exp_labels: {a: b}\n",
        2: "        exp_alerts: *shared\n",
    }
    source = _write(tmp_path, _groups(["1h", "1h", "1h", "1h"], anchors))

    parts = split_rule_test(source, 4, tmp_path / "out")

    holding = [indices for _path, indices in parts if 0 in indices]
    assert holding == [[0, 2]], parts
    for path, _indices in parts:
        yaml_fast.safe_load(path.read_text())


def test_the_costliest_group_gets_a_part_of_its_own(tmp_path):
    source = _write(tmp_path, _groups(["26h", "175h", "26h", "1h", "1h"]))

    parts = split_rule_test(source, 2, tmp_path / "out")

    assert sorted(indices for _path, indices in parts) == [[0, 2, 3, 4], [1]]


def test_a_file_whose_groups_the_text_does_not_show_runs_whole(tmp_path):
    source = _write(tmp_path, _groups(["1h", "1h"]) + "group_eval_order: []\n")

    assert split_rule_test(source, 4, tmp_path / "out") == [(source, [0, 1])]
    assert not any((tmp_path / "out").iterdir())
