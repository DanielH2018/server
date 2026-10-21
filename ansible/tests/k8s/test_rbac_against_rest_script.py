"""A REST-client Job's rendered RBAC grants every call its script makes.

The scripts here call the API server through a small `Kube` client rather than kubectl, so each
case subclasses that client to record `(method, path)` pairs, and `_script_rbac.rest_needs`
reads the needs from those. The kubectl-driven readers' cases are in
test_rbac_against_script.py, beside the derivation's own tests. Each case pins the plan's grant
set for its script, so a recorder that stopped recording cannot pass on an empty set.

Run: uv run pytest ansible/tests/k8s/test_rbac_against_rest_script.py
"""

import importlib

from _helpers import K8S_ROLES
from _k8s_render import render_role_template
from _script_rbac import grants, missing, rest_needs
from lib import yaml_fast


def _jellyfin_cap_calls(monkeypatch) -> list[tuple[str, str]]:
    """D5's jellyfin cap: the Job script's calls on an uncapped volume, its longest path.

    The replies hold the claim bound and the cap unset, so the script goes on from the reads
    to the patch and every call it can make is recorded.
    """
    monkeypatch.syspath_prepend(str(K8S_ROLES / "jellyfin" / "files"))
    script = importlib.import_module("jellyfin_snapshot_cap")
    calls = []

    class Recorder(script.Kube):
        def __init__(self):
            super().__init__("https://api", "token")

        def call(self, method: str, path: str, body=None) -> tuple[int, dict]:
            calls.append((method, path))
            if path.endswith("/jellyfin-config"):
                return 200, {"spec": {"volumeName": "pvc-4f1c"}}
            return 200, {"spec": {"snapshotMaxSize": "0"}}

    env = {
        "CAP_NAMESPACE": "homelab",
        "CAP_CLAIM": "jellyfin-config",
        "CAP_MAX_SIZE": "17179869184",
    }
    assert script.main([], env, Recorder()) == 0
    assert [method for method, _ in calls] == ["GET", "GET", "PATCH"]
    return calls


def _jellyfin_cap_rbac() -> list[dict]:
    return list(
        yaml_fast.safe_load_all(render_role_template("jellyfin", "verify/rbac.yaml.j2"))
    )


def test_the_jellyfin_cap_role_grants_every_call_its_script_makes(monkeypatch) -> None:
    needs = rest_needs(_jellyfin_cap_calls(monkeypatch))
    # The plan's grant set for the cap (*jellyfin's cap*).
    assert needs == {
        ("homelab", "", "persistentvolumeclaims", "get", "jellyfin-config"),
        ("longhorn-system", "longhorn.io", "volumes", "get", "pvc-4f1c"),
        ("longhorn-system", "longhorn.io", "volumes", "patch", "pvc-4f1c"),
    }
    granted = grants(_jellyfin_cap_rbac(), "homelab", "jellyfin-snapshot-cap")
    assert missing(needs, granted) == []


def test_the_jellyfin_cap_role_without_patch_is_flagged(monkeypatch) -> None:
    needs = rest_needs(_jellyfin_cap_calls(monkeypatch))
    docs = _jellyfin_cap_rbac()
    for doc in docs:
        for rule in doc.get("rules") or []:
            if rule["resources"] == ["volumes"]:
                rule["verbs"].remove("patch")
    assert missing(needs, grants(docs, "homelab", "jellyfin-snapshot-cap")) == [
        ("longhorn-system", "longhorn.io", "volumes", "patch", "pvc-4f1c")
    ]


def test_the_jellyfin_verify_account_holds_no_grant() -> None:
    """The verify Job reads no API; the cap's patch grant must not reach its account."""
    assert grants(_jellyfin_cap_rbac(), "homelab", "jellyfin-verify") == []


def _pihole_guard_calls(monkeypatch) -> list[tuple[str, str]]:
    """D5's pihole sibling guard: its calls when instance 1 serves, its longest path.

    Instance 1's list answers with a ready endpoint, so the script goes on to read pihole-2's
    slices, and every call it can make is recorded.
    """
    monkeypatch.syspath_prepend(str(K8S_ROLES / "pihole" / "files"))
    script = importlib.import_module("pihole_sibling_guard")
    calls = []

    class Recorder(script.Kube):
        def __init__(self):
            super().__init__("https://api", "token")

        def get(self, path: str) -> tuple[int, dict]:
            calls.append(("GET", path))
            ready = {"conditions": {"ready": True}}
            return 200, {"items": [{"endpoints": [ready]}]}

    env = {
        "GUARD_NAMESPACE": "homelab",
        "GUARD_INSTANCE_SERVICE": "pihole",
        "GUARD_SIBLING_SERVICE": "pihole-2-web",
        "GUARD_WAIT_SECONDS": "300",
    }
    assert script.main([], env, Recorder()) == 0
    assert len(calls) == 2
    return calls


def _pihole_guard_rbac() -> list[dict]:
    return list(
        yaml_fast.safe_load_all(
            render_role_template("pihole", "guard-inert/rbac.yaml.j2")
        )
    )


def test_the_pihole_guard_role_grants_every_call_its_script_makes(monkeypatch) -> None:
    needs = rest_needs(_pihole_guard_calls(monkeypatch))
    # The operator's grant on #5695.
    assert needs == {("homelab", "discovery.k8s.io", "endpointslices", "list", None)}
    granted = grants(_pihole_guard_rbac(), "homelab", "pihole-sibling-guard")
    assert missing(needs, granted) == []


def test_the_pihole_guard_role_without_list_is_flagged(monkeypatch) -> None:
    needs = rest_needs(_pihole_guard_calls(monkeypatch))
    docs = _pihole_guard_rbac()
    for doc in docs:
        for rule in doc.get("rules") or []:
            rule["verbs"].remove("list")
    assert missing(needs, grants(docs, "homelab", "pihole-sibling-guard")) == [
        ("homelab", "discovery.k8s.io", "endpointslices", "list", None)
    ]


def _home_ips_calls(monkeypatch) -> list[tuple[str, str]]:
    """H2.4c: the crowdsec-home-ips CronJob's calls on a good lookup, its one write."""
    monkeypatch.syspath_prepend(str(K8S_ROLES / "crowdsec" / "files"))
    script = importlib.import_module("home_ips_discover")
    calls = []

    class Recorder(script.Kube):
        def __init__(self):
            super().__init__("https://api", "token")

        def call(self, method: str, path: str, body=None) -> tuple[int, dict]:
            calls.append((method, path))
            return 200, {}

    env = {"HOME_IPS_NAMESPACE": "homelab", "HOME_IPS_CONFIGMAP": "crowdsec-home-ips"}
    assert (
        script.main(
            [],
            env,
            Recorder(),
            opener=lambda *a, **k: (_ for _ in ()).throw(OSError("offline")),
            read_if_inet6=lambda: "",
        )
        == 0
    )
    return calls


def _home_ips_rbac() -> list[dict]:
    return list(
        yaml_fast.safe_load_all(
            render_role_template("crowdsec", "home-ips-rbac.yaml.j2")
        )
    )


def test_the_home_ips_role_grants_every_call_its_script_makes(monkeypatch) -> None:
    needs = rest_needs(_home_ips_calls(monkeypatch))
    # The operator's grant on #5701, less the `get` the merge patch never needs.
    assert needs == {("homelab", "", "configmaps", "patch", "crowdsec-home-ips")}
    granted = grants(_home_ips_rbac(), "homelab", "crowdsec-home-ips")
    assert missing(needs, granted) == []


def test_the_home_ips_role_without_patch_is_flagged(monkeypatch) -> None:
    needs = rest_needs(_home_ips_calls(monkeypatch))
    docs = _home_ips_rbac()
    for doc in docs:
        for rule in doc.get("rules") or []:
            rule["verbs"] = ["get"]
    assert missing(needs, grants(docs, "homelab", "crowdsec-home-ips")) == [
        ("homelab", "", "configmaps", "patch", "crowdsec-home-ips")
    ]
