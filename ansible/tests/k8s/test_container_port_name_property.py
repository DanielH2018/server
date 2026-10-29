"""A rendered-property row enforces Kubernetes' IANA_SVC_NAME rule on container port names (#6543).

#6537 rendered a port named `dockerhub-mirror` (16 characters), every PR check passed, and
Flux's dry run refused the Deployment. The row may carry any name, so these tests find it by
behaviour: a row in `test_rendered_properties.PROPERTIES` that reports an offender for a pod
whose only defect is its port name, and reports none for the same pod with a valid one.

Run: uv run pytest ansible/tests/k8s/test_container_port_name_property.py
"""

import pytest

from _property_table import check
from _workload_property_rows import POD_KINDS
from test_rendered_properties import PROPERTIES

# A real role, because some rows read the role's directory.
_ROLE = "registry"
_TPL = "zz-port-name-fixture.yaml.j2"
_OFFENDER = f"{_ROLE}/{_TPL} ["
_VALID = "dockerhub"


def _pod_doc(kind: str, port_name: str) -> dict:
    pod = {
        "metadata": {"labels": {"app": "fixture"}},
        "spec": {
            "containers": [
                {
                    "name": "fixture",
                    "image": "registry:2",
                    "ports": [{"name": port_name, "containerPort": 5000}],
                }
            ]
        },
    }
    spec: dict = {"template": pod}
    if kind == "CronJob":
        spec = {"schedule": "0 0 * * *", "jobTemplate": {"spec": {"template": pod}}}
    return {
        "kind": kind,
        "metadata": {"name": "fixture", "namespace": "fixture"},
        "spec": spec,
    }


def _offender_lines(prop, doc: dict) -> list[str]:
    try:
        problems = check(prop, [(_ROLE, _TPL, doc)])
    except KeyError, TypeError, AttributeError, ValueError, OSError:
        # A row whose selector needs fields this fixture lacks is not the port-name row.
        return []
    return [p for p in problems if _OFFENDER in p]


def _port_name_rows(kind: str, bad: str) -> list[str]:
    """Rows that flag the pod with port `bad` and accept the same pod with `_VALID`."""
    return [
        prop.name
        for prop in PROPERTIES
        if _offender_lines(prop, _pod_doc(kind, bad))
        and not _offender_lines(prop, _pod_doc(kind, _VALID))
    ]


def test_a_row_flags_dockerhub_mirror_and_accepts_dockerhub():
    assert _port_name_rows("Deployment", "dockerhub-mirror"), (
        "no rendered-property row flags a container port named 'dockerhub-mirror' "
        "(16 characters) while accepting 'dockerhub'"
    )


@pytest.mark.parametrize("kind", sorted(POD_KINDS))
def test_the_row_covers_every_pod_kind(kind: str):
    assert _port_name_rows(kind, "dockerhub-mirror"), (
        f"no row flags a 16-character container port name in a {kind}"
    )


@pytest.mark.parametrize(
    "bad",
    ["Dockerhub", "docker_hub", "-dockerhub", "dockerhub-", "docker.hub"],
)
def test_a_short_name_off_the_iana_pattern_is_flagged(bad: str):
    assert _port_name_rows("Deployment", bad), (
        f"no row flags container port name {bad!r}, which is at most 15 characters but "
        "does not match [a-z0-9]([a-z0-9-]*[a-z0-9])?"
    )


def test_a_name_with_no_letter_is_flagged():
    assert _port_name_rows("Deployment", "8080"), (
        "no row flags container port name '8080', which holds no letter"
    )


def test_a_name_of_exactly_fifteen_characters_is_accepted():
    # The only difference from the 16-character red name is the length, so a row that
    # flags this one has its bound off by one.
    assert _port_name_rows("Deployment", "dockerhub-mirror"), (
        "no row flags 'dockerhub-mirror', so the 15-character bound is untested"
    )
    assert _port_name_rows("Deployment", "dockerhub-mirro") == [], (
        "a row flags the 15-character name 'dockerhub-mirro', which Kubernetes accepts"
    )
