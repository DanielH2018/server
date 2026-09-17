"""n8n's web port admits traefik and monitor-bridge only, and the deploy probe proves it.

`n8n-broker` (roles/k8s/n8n/templates/networkpolicy.yaml.j2) is the ONLY thing fencing the n8n
pod: `netpol-baseline-exempt: "true"` takes it out of the namespace baseline, so a rule there
with no `from:` admits every pod in the cluster. That is how it read until 2026-09-17 (#1926):
karakeep's headless Chrome, qbittorrent or the n8n runners could dial `n8n:5678` directly,
skipping Traefik and with it Authelia's two_factor, the CrowdSec bouncer and the rate-limit.

Two invariants, each a predicate with a passing and a rejecting input, then applied to the real
rendered manifests:

- **The 5678 rule names exactly the two callers.** traefik (where the auth middleware runs) and
  monitor-bridge (check_n8n dials the API with X-N8N-API-KEY). A rule with no `from:` is the
  hole; a rule with an extra peer is a widening nobody asked for.
- **The probe Job asserts 5678 REFUSED, and its control is not n8n.** Until #1926 the probe's
  control dialled `n8n:5678`, so every deploy asserted the hole was open. A control on n8n's own
  port and a fence on that port cannot both hold.

Run: uv run pytest ansible/tests/services/test_n8n_web_port_is_fenced.py
"""

import pytest
from _k8s_render import rendered_docs

WEB_PORT = 5678
BROKER_PORT = 5679
WEB_PEERS = frozenset({"traefik", "monitor-bridge"})


# --- the rules, as predicates -------------------------------------------------------


def peers_admitted_on(policy: dict, port: int) -> set[str] | None:
    """The `app` labels a policy admits on `port`, or None when a rule on it has no `from:`.

    None is the dangerous shape — a rule with no peer admits every source — and it is kept
    distinct from the empty set (no rule at all, which admits nothing).
    """
    admitted: set[str] = set()
    for rule in policy["spec"].get("ingress", []):
        if not any(int(p.get("port", -1)) == port for p in rule.get("ports", [])):
            continue
        if "from" not in rule:
            return None
        for peer in rule["from"]:
            admitted.add(
                peer.get("podSelector", {}).get("matchLabels", {}).get("app", "?")
            )
    return admitted


def web_port_is_fenced(policy: dict) -> bool:
    return peers_admitted_on(policy, WEB_PORT) == set(WEB_PEERS)


def probe_asserts_web_port_refused(script: str) -> bool:
    """True when the script fails on a SUCCESSFUL connect to n8n:5678 and never uses it as control."""
    return (
        f"if nc -w 5 -z n8n {WEB_PORT}" in script
        and f"if ! nc -w 5 -z n8n {WEB_PORT}" not in script
        and "nc -w 5 -z traefik 80" in script
    )


# --- red proofs -----------------------------------------------------------------------


def _policy(rules):
    return {"spec": {"ingress": rules}}


def _rule(port, *apps, no_from=False):
    rule = {"ports": [{"protocol": "TCP", "port": port}]}
    if not no_from:
        rule["from"] = [{"podSelector": {"matchLabels": {"app": a}}} for a in apps]
    return rule


def test_the_fenced_shape_is_clean():
    assert web_port_is_fenced(
        _policy(
            [
                _rule(WEB_PORT, "traefik", "monitor-bridge"),
                _rule(BROKER_PORT, "n8n-runners"),
            ]
        )
    )


@pytest.mark.parametrize(
    "rules",
    [
        pytest.param([_rule(WEB_PORT, no_from=True)], id="no-from-admits-everything"),
        pytest.param([_rule(WEB_PORT, "traefik")], id="monitor-bridge-dropped"),
        pytest.param(
            [_rule(WEB_PORT, "traefik", "monitor-bridge", "karakeep")], id="extra-peer"
        ),
        pytest.param([_rule(BROKER_PORT, "n8n-runners")], id="no-rule-at-all"),
    ],
)
def test_the_open_shapes_are_flagged(rules):
    assert not web_port_is_fenced(_policy(rules))


def test_the_inverted_probe_is_clean():
    script = (
        "if ! nc -w 5 -z traefik 80; then exit 1; fi\n"
        f"if nc -w 5 -z n8n {WEB_PORT}; then exit 1; fi\n"
        f"if nc -w 5 -z n8n {BROKER_PORT}; then exit 1; fi\n"
    )
    assert probe_asserts_web_port_refused(script)


def test_the_pre_1926_probe_is_flagged():
    """The old shape: n8n:5678 as the CONTROL, which asserted the hole on every deploy."""
    script = (
        f"if ! nc -w 5 -z n8n {WEB_PORT}; then exit 1; fi\n"
        f"if nc -w 5 -z n8n {BROKER_PORT}; then exit 1; fi\n"
    )
    assert not probe_asserts_web_port_refused(script)


# --- applied to the rendered manifests -------------------------------------------------


def _n8n_doc(kind: str, name: str) -> dict:
    for role, _tpl, doc in rendered_docs():
        if (
            role == "n8n"
            and doc.get("kind") == kind
            and doc["metadata"]["name"] == name
        ):
            return doc
    raise AssertionError(f"n8n renders no {kind} named {name}")


def test_n8n_broker_fences_the_web_port_to_traefik_and_monitor_bridge():
    policy = _n8n_doc("NetworkPolicy", "n8n-broker")
    admitted = peers_admitted_on(policy, WEB_PORT)
    assert admitted == set(WEB_PEERS), (
        f"n8n-broker admits {admitted!r} on :{WEB_PORT}; expected exactly {sorted(WEB_PEERS)}. "
        "None means a rule with no `from:` — every pod in the cluster reaches n8n and skips "
        "Authelia, CrowdSec and the rate-limit (#1926)."
    )


def test_n8n_broker_still_fences_the_broker_to_the_runners():
    """The point of the file, kept while the web port was fenced beside it."""
    policy = _n8n_doc("NetworkPolicy", "n8n-broker")
    assert peers_admitted_on(policy, BROKER_PORT) == {"n8n-runners"}


def test_the_probe_proves_the_fence_rather_than_the_hole():
    job = _n8n_doc("Job", "n8n-netpol-probe")
    containers = job["spec"]["template"]["spec"]["containers"]
    script = "\n".join("\n".join(c.get("command", [])) for c in containers)
    assert probe_asserts_web_port_refused(script), (
        "n8n-netpol-probe must lead with a control on traefik:80 and fail when n8n:5678 "
        "ACCEPTS a connection from the unlabelled probe pod. Rendered script:\n"
        + script
    )


def test_the_probe_pod_carries_no_admitted_label():
    """A probe labelled `app: traefik` would join traefik's Endpoints and pass for the wrong reason."""
    job = _n8n_doc("Job", "n8n-netpol-probe")
    app = job["spec"]["template"]["metadata"]["labels"].get("app")
    assert app not in WEB_PEERS | {"n8n-runners"}, f"probe pod is labelled app={app}"
