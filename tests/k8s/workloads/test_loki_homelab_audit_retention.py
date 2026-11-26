"""loki-homelab gives the k8s-audit stream, and only that stream, a 7-day retention."""

import pytest

from lib.estate import shared_vars
from _k8s_render import rendered_docs
from lib import yaml_fast

JOB = shared_vars()["loki_streams"]["k8s_audit"]["job"]


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
