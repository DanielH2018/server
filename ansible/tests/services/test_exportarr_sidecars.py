"""Guards the exportarr sidecars across the three *arr roles.

The sidecar is one shared macro (`ansible/templates/exportarr.yml.j2`) invoked from three
Deployments, which is what keeps the three from drifting. What a shared macro cannot pin is
everything AROUND the invocation — the Service port, the Secret, the image pin, the scrape
job — and every one of those is per-role and silently omissible: leave one out and that *arr
alone goes unmonitored while the other two prove the pattern works.

The image pin is deliberately duplicated three times rather than shared from group_vars.
Renovate's k8s-images manager matches `roles/k8s/*/defaults` and `_image:` keys only, so a
shared var one directory up is invisible to it — the escape `crowdsec_k8s_image` made, recorded
in renovate.json. Duplication that a test keeps in lockstep beats a single copy nothing tracks.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml
from _helpers import REPO

_REPO = REPO
sys.path.insert(0, str(Path(__file__).parent))

from _k8s_render import rendered_docs  # noqa: E402

ARRS = ("sonarr", "radarr", "prowlarr")
METRICS_PORT = 9707


def _docs():
    return list(rendered_docs())


def test_every_arr_runs_an_exportarr_sidecar():
    found = {
        role
        for role, _, doc in _docs()
        if doc.get("kind") == "Deployment"
        and any(
            c.get("name") == "exportarr"
            for c in doc["spec"]["template"]["spec"].get("containers", [])
        )
    }
    assert found == set(ARRS), "missing an exportarr sidecar: %s" % sorted(
        set(ARRS) - found
    )


def test_the_sidecar_talks_to_localhost_not_the_service():
    """In-pod, so the sidecar needs no entry in that *arr's NetworkPolicy caller list.

    Pointed at the Service name instead it would still work — right up until someone reads the
    policy, sees no exportarr, and correctly concludes nothing needs admitting.
    """
    for role, _, doc in _docs():
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            if container.get("name") != "exportarr":
                continue
            url = next(e["value"] for e in container["env"] if e["name"] == "URL")
            assert url.startswith("http://localhost:"), (
                f"{role}'s exportarr must reach its *arr over localhost, got {url}"
            )


def test_the_api_key_is_a_secret_reference_never_a_literal():
    for role, _, doc in _docs():
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            if container.get("name") != "exportarr":
                continue
            apikey = next(e for e in container["env"] if e["name"] == "APIKEY")
            assert "value" not in apikey, (
                f"{role}'s exportarr APIKEY is inline — it would show in "
                "`kubectl describe pod`; use secretKeyRef"
            )
            assert apikey["valueFrom"]["secretKeyRef"]["name"] == f"{role}-exportarr"


def test_the_sidecar_has_no_readiness_probe():
    """A not-ready sidecar removes the whole POD from its Service.

    The *arr and the exporter share one pod, so a readinessProbe here turns an exporter outage
    into an application outage — monitoring taking down the thing it monitors. Liveness is
    correct and present; readiness is the one that must not be added.
    """
    for role, _, doc in _docs():
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            if container.get("name") != "exportarr":
                continue
            assert "readinessProbe" not in container, (
                f"{role}'s exportarr must not have a readinessProbe — it would take the "
                f"{role} pod out of its Service whenever the exporter hiccuped"
            )
            assert "livenessProbe" in container, (
                f"{role}'s exportarr should still be restarted when it wedges"
            )


def test_every_arr_service_exposes_the_metrics_port():
    """Not needed by the scrape (Prometheus dials the pod), but needed by everything else.

    A Service port is how a human, a dashboard datasource or a future in-cluster caller reaches
    the exporter without knowing pod IPs.
    """
    for role, _, doc in _docs():
        if doc.get("kind") != "Service" or doc["metadata"]["name"] not in ARRS:
            continue
        ports = doc["spec"]["ports"]
        assert any(p["port"] == METRICS_PORT for p in ports), (
            f"{role}'s Service does not expose {METRICS_PORT}"
        )
        # Kubernetes rejects a mix of named and unnamed ports on a multi-port Service, and the
        # rejection lands at apply time — after every repo-side check has read green.
        assert all("name" in p for p in ports), (
            f"{role}'s Service mixes named and unnamed ports, which the API server rejects"
        )


def test_every_arr_renders_its_exportarr_secret():
    found = {
        doc["metadata"]["name"]
        for _, _, doc in _docs()
        if doc.get("kind") == "Secret"
        and doc["metadata"]["name"].endswith("-exportarr")
    }
    assert found == {f"{a}-exportarr" for a in ARRS}


def test_the_secret_is_staged_through_the_no_log_path():
    """A Secret listed in manifests_files instead of manifests_secret_files renders 0644 and
    prints its decrypted contents in the play recap."""
    for arr in ARRS:
        tasks = (_REPO / f"ansible/roles/k8s/{arr}/tasks/main.yml").read_text()
        assert "manifests_secret_files:" in tasks, (
            f"{arr} renders a Secret but declares no manifests_secret_files"
        )
        secret_block = tasks.split("manifests_secret_files:", 1)[1]
        assert "secret-exportarr.yaml" in secret_block.split("manifests")[0], (
            f"{arr}'s secret-exportarr.yaml must be under manifests_secret_files"
        )


def test_the_image_pins_stay_in_lockstep():
    """Three copies, one version. Renovate groups them; this catches a hand-edit that does not.

    Drift here is quiet: the three exporters keep working at different versions until one
    upstream release changes a metric name, and then one dashboard panel goes blank.
    """
    pins = {}
    for arr in ARRS:
        text = (_REPO / f"ansible/roles/k8s/{arr}/defaults/main.yml").read_text()
        match = re.search(rf"^{arr}_exportarr_image:\s*(\S+)", text, re.M)
        assert match, f"{arr} has no {arr}_exportarr_image pin in its own defaults"
        pins[arr] = match.group(1)

    assert len(set(pins.values())) == 1, f"exportarr pins have drifted: {pins}"


def test_prometheus_scrapes_the_sidecars_by_port_not_by_app_label():
    """Selecting on `app` would also match the *arr's own container port.

    The sidecar shares its *arr's pod, so both containers carry `app: sonarr`. A label-only
    keep rule therefore scrapes sonarr's web UI as if it were a metrics endpoint — which does
    not error, it just yields a target that returns HTML and a job that is permanently down.
    """
    prom = (
        _REPO / "ansible/roles/k8s/claude-otel/templates/prometheus.yaml.j2"
    ).read_text()
    job = prom.split("- job_name: exportarr", 1)
    assert len(job) == 2, "claude-otel declares no `exportarr` scrape job"
    block = job[1].split("- job_name:", 1)[0]

    assert str(METRICS_PORT) in block, (
        "the job must keep targets by container port number"
    )
    assert "__meta_kubernetes_pod_container_port_number" in block, (
        "the keep rule must be on the port number — an `app` label alone matches the *arr's "
        "own web port too"
    )


def test_the_rendered_sidecar_is_a_sibling_container_not_a_nested_key():
    """The macro is invoked inside a YAML list, and its indentation is load-bearing.

    A macro that renders one level off still parses — it lands as a key on the preceding
    container instead of as a new list item — so the manifest validator passes and the sidecar
    silently never runs. Only counting the containers catches that.
    """
    for role, name, doc in _docs():
        if doc.get("kind") != "Deployment" or role not in ARRS:
            continue
        if name != "deployment.yaml.j2":
            continue
        containers = doc["spec"]["template"]["spec"]["containers"]
        assert len(containers) == 2, (
            f"{role}/{name} should render exactly the app and its exportarr sidecar, "
            f"got {[c.get('name') for c in containers]}"
        )
        assert yaml.safe_load(yaml.safe_dump(containers)) == containers
