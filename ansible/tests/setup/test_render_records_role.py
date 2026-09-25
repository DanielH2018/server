"""The render-record producer reads the directory the renders write, on a graced schedule.

`render_records_record_dir` mirrors `manifests_render_record_dir` because the manifests role's
defaults are not in scope in a setup play. A drift between them turns every run red with "no
record" while the records sit in the other directory.

Run: uv run pytest ansible/tests/setup/test_render_records_role.py
"""

import re

from lib import yaml_fast
from _helpers import REPO

ROLE = REPO / "ansible/roles/setup/render_records"
MANIFESTS_DEFAULTS = REPO / "ansible/roles/k8s/manifests/defaults/main.yml"
ALL_VARS = REPO / "ansible/inventory/group_vars/all.yml"


def _load(path):
    return yaml_fast.safe_load(path.read_text())


def test_the_producer_reads_the_directory_render_record_writes():
    assert (
        _load(ROLE / "defaults/main.yml")["render_records_record_dir"]
        == _load(MANIFESTS_DEFAULTS)["manifests_render_record_dir"]
    )


def _seconds(duration: str) -> int:
    value, unit = re.fullmatch(r"(\d+)(min|s)", duration).groups()
    return int(value) * (60 if unit == "min" else 1)


def test_the_monitor_outlasts_one_hourly_period_plus_a_deferred_rerun():
    interval = _load(ALL_VARS)["render_records_push_interval_s"]
    restart = _seconds(_load(ROLE / "defaults/main.yml")["render_records_restart_sec"])
    assert interval > 3600 + restart, (
        f"render_records_push_interval_s={interval} expires before an hourly run that "
        f"deferred once and reran after {restart}s could push"
    )
