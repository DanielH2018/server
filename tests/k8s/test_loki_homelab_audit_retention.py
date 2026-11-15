"""loki-homelab keeps the k8s-audit stream for 7 days and every other stream for the default."""

import pytest

from _helpers import ALL_VARS, load_yaml
from _k8s_render import rendered_docs
from lib import yaml_fast

JOB = load_yaml(ALL_VARS)["loki_streams"]["k8s_audit"]["job"]


@pytest.fixture(scope="module")
def limits() -> dict:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            if "loki.yaml" in doc["data"]:
                return yaml_fast.safe_load(doc["data"]["loki.yaml"])["limits_config"]
    raise AssertionError("loki-homelab renders no ConfigMap carrying loki.yaml")


def test_the_audit_stream_keeps_seven_days(limits: dict) -> None:
    assert limits["retention_stream"] == [
        {"selector": f'{{job="{JOB}"}}', "priority": 1, "period": "168h"}
    ]


def test_other_streams_keep_the_default_window(limits: dict) -> None:
    assert limits["retention_period"] == "744h"
