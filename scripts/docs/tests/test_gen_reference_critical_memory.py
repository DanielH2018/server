"""The critical tier's memory p95 table that the request census reads.

Run: uv run pytest scripts/docs/tests/test_gen_reference_critical_memory.py
"""

import json

import pytest

from diagnostics.probe_lib import core
from docs import build_docs
from docs.reference import critical_memory

MIB = 2**20


def _series(pod, container, mib, namespace="homelab"):
    return {
        "metric": {"namespace": namespace, "pod": pod, "container": container},
        "value": [0, str(mib * MIB)],
    }


def _must_record_series():
    return [
        _series(f"{workload}-7d8465b555-nvqqq", container, 10)
        for workload, container in (
            key.split("/")[1:] for key in critical_memory.MUST_RECORD
        )
    ]


def test_a_workload_records_its_highest_pod_rounded_up_to_the_step():
    series = _must_record_series() + [
        _series("traefik-9548c46cf-m8zhh", "traefik", 163),
        _series("traefik-9548c46cf-bhblq", "traefik", 219),
    ]

    table = critical_memory.p95_table(series)

    assert table["homelab/traefik/traefik"] == 224
    assert table["homelab/pihole/pihole"] == 16


def test_a_result_missing_a_must_record_member_is_unreadable():
    series = [s for s in _must_record_series() if s["metric"]["container"] != "traefik"]

    with pytest.raises(core.Unreadable, match="homelab/traefik/traefik"):
        critical_memory.p95_table(series)


def test_main_writes_the_table_the_census_reads(tmp_path):
    out = tmp_path / "critical-memory-p95.json"

    assert (
        critical_memory.main(
            ["--out", str(out)], query=lambda promql: _must_record_series()
        )
        == 0
    )

    assert critical_memory.read_table(out) == {
        key: 16 for key in critical_memory.MUST_RECORD
    }
    assert json.loads(out.read_text())["generated_from"] == critical_memory.SOURCE


def test_the_docs_cron_regenerates_the_committed_table():
    outputs = {out for _, out in build_docs.GENERATORS}

    assert str(critical_memory.TABLE.relative_to(critical_memory.REPO)) in outputs
