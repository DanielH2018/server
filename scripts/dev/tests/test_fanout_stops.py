import json
from datetime import UTC, datetime

from fanout_lib.stops import load_stops, stop_lines


def test_stops_list_each_stopped_batch_in_the_window_with_its_blocker(tmp_path):
    records = {
        "7001-20261105T120000Z.json": {"outcome": "needs-input", "pr": "", "blocker": "needs input: no key"},
        "7002-20261105T120000Z.json": {"outcome": "failed"},
        "7003-20261105T120000Z.json": {"outcome": "pr", "blocker": ""},
        "7004-20261020T120000Z.json": {"outcome": "needs-input", "blocker": "needs input: old"},
    }  # fmt: skip
    for name, record in records.items():
        (tmp_path / name).write_text(json.dumps(record))
    (tmp_path / "7005-20261105T120000Z.json").write_text("{not json")

    stops = load_stops(tmp_path, datetime(2026, 11, 1, tzinfo=UTC))

    assert stop_lines(stops) == [
        "batches stopped for a person: 2 (needs-input 1, failed 1), 1 with no blocker line",
        "  7001 needs-input: needs input: no key",
    ]
    assert stop_lines([]) == ["batches stopped for a person: 0"]
