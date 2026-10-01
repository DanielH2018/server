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

WHAT THIS FILE DELIBERATELY DOES NOT ASSERT. The sidecars carry no readinessProbe, and that
rule lives in `ansible/tests/k8s/test_readiness_coverage.py` alone: all three are rows in its
`_NO_READINESS` record, and its `test_the_record_has_no_stale_entries` goes red the moment one
gains a probe. Asserting it here as well split one rule's rationale across two files (#3055).
Do not re-add it; add a row there.
"""

import re

import yaml
from _helpers import REPO
from _k8s_render import rendered_docs
from lib import yaml_fast

ARRS = ("sonarr", "radarr", "prowlarr")
METRICS_PORT = 9707


def _docs():
    return list(rendered_docs())


def _sidecars() -> dict[str, dict]:
    """{role: the rendered exportarr sidecar container} for the three *arr Deployments.

    Every guard below that reads one field off the sidecar goes through here, so the
    non-vacuity check is written once: a renamed container or a render that dropped a role
    leaves the dict short, and each caller asserts `set(...) == set(ARRS)` against it. Nine
    guards in this repo broke by looping `continue` past everything and asserting over nothing.
    """
    found: dict[str, dict] = {}
    for role, name, doc in _docs():
        if doc.get("kind") != "Deployment" or role not in ARRS:
            continue
        if name != "deployment.yaml.j2":
            continue
        for container in doc["spec"]["template"]["spec"]["containers"]:
            if container.get("name") == "exportarr":
                found[role] = container
    return found


def _census(found: dict) -> None:
    """Refuse a short census. Every caller reads a field off `_sidecars()`, so a role missing
    from the dict means that role rendered no `exportarr` container at all — never that the
    field is absent from one."""
    assert set(found) == set(ARRS), (
        f"rendered no exportarr sidecar for {sorted(set(ARRS) - set(found))} — the guard "
        "below would pass vacuously"
    )


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


def test_the_sidecar_is_restarted_when_it_wedges():
    """The sidecar keeps a livenessProbe and no readinessProbe, and only one half lives here.

    The readiness half moved to `ansible/tests/k8s/test_readiness_coverage.py`, which records
    all three exportarr containers in `_NO_READINESS` with this reason — a readinessProbe on a
    sidecar takes the whole pod out of its *arr's Service whenever the exporter hiccups, so
    monitoring takes down the thing it monitors. That file's
    `test_the_record_has_no_stale_entries` goes red the moment one of the three gains a probe,
    so asserting it again here only duplicated the rule and split its rationale across two
    files (#3055). `ARRS` is exactly the three roles recorded there.

    Liveness is the half no table covers: nothing else in the suite asks whether an exempt
    container is restartable at all, and a wedged exporter with no probe is simply silent.

    Census first: the loop `continue`s past every container not named `exportarr`, so a rename
    or an empty render would otherwise pass with zero assertions.
    """
    probed = _sidecars()
    _census(probed)
    for role, container in sorted(probed.items()):
        assert "livenessProbe" in container, (
            f"{role}'s exportarr should still be restarted when it wedges"
        )


def test_the_sidecar_cpu_limit_covers_a_scrape_burst():
    """The cap is sized against the CFS quota per period, not the average rate (#2017).

    The sidecar does its work in one burst per scrape, so a limit that looks like 500x the
    24h mean can still throttle every scrape: at 100m sonarr's sidecar hit the quota in
    ~48% of periods. 500m is the smallest shared value whose 50ms-per-period quota covers
    sonarr's measured 25-86ms burst; the comment in the macro carries the measurement.

    Census first (#2036): the loop `continue`s past every container not named `exportarr`,
    so a renamed container or an empty render would otherwise pass with zero assertions.
    """
    limits = {}
    for role, _, doc in _docs():
        if doc.get("kind") != "Deployment":
            continue
        for container in doc["spec"]["template"]["spec"].get("containers", []):
            if container.get("name") != "exportarr":
                continue
            limits[role] = container["resources"]["limits"]["cpu"]

    assert set(limits) == set(ARRS), (
        f"rendered no exportarr sidecar for {sorted(set(ARRS) - set(limits))} — this guard "
        "would pass vacuously"
    )
    for role, cpu in limits.items():
        assert cpu == "500m", (
            f"{role}'s exportarr cpu limit moved -- re-measure the CFS throttle ratio "
            "before changing it; a mean-based justification is how 100m shipped"
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
        tasks = (REPO / f"ansible/roles/k8s/{arr}/tasks/main.yml").read_text()
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

    Read off the RENDERED container rather than regexed out of each role's defaults (#2809).
    The pin reaches the pod as a macro ARGUMENT — `exportarr(app, image, ...)` in
    `ansible/templates/exportarr.yml.j2`, passed as `exportarr_image=` by each role — and every
    role renders in its OWN defaults context. So an invocation naming another role's variable
    ships `image: STUB` while all three text pins still read identical and agree. Measured on
    this tree: crossing radarr's argument to `sonarr_exportarr_image` renders
    `{'radarr': 'STUB', ...}` here and leaves the defaults regex clean.
    """
    # fact: ansible/roles/k8s/radarr/CLAUDE.md#At a glance
    # fact: ansible/roles/k8s/sonarr/CLAUDE.md#Notable
    images = {role: c["image"] for role, c in _sidecars().items()}
    _census(images)
    assert len(set(images.values())) == 1, f"exportarr pins have drifted: {images}"


def test_each_pin_is_a_variable_renovate_can_see():
    """Where the pin LIVES is the requirement here, so this half stays a source check.

    Renovate's k8s-images manager matches `roles/k8s/*/defaults` and an `_image:` key, and
    nothing else. A pin hoisted into group_vars, or inlined into the macro invocation, renders
    an identical manifest and silently stops being offered updates — the escape
    `crowdsec_k8s_image` made. No render assertion can see that, because the manifest is the
    same either way.
    """
    for arr in ARRS:
        text = (REPO / f"ansible/roles/k8s/{arr}/defaults/main.yml").read_text()
        assert re.search(rf"^{arr}_exportarr_image:\s*\S+", text, re.M), (
            f"{arr} has no {arr}_exportarr_image pin in its own defaults, so Renovate will "
            "never offer it an update"
        )


def _sidecar_args() -> dict[str, list[str]]:
    """{role: the exportarr sidecar's rendered args} for the three *arr Deployments."""
    return {role: c["args"] for role, c in _sidecars().items()}


def test_only_sonarr_enables_the_additional_metrics_collector():
    """The flag is per-app, and both halves of that are assertions.

    `--enable-additional-metrics` gates sonarr's episode collector, which is what publishes
    `sonarr_episode_quality_total`. Upstream annotates it `(slow)`: the collector issues two
    extra app API calls per series per scrape. radarr's and prowlarr's collectors have no such
    block, so the flag buys them nothing and would only add scrape cost — which is why the
    shared macro takes a parameter rather than a blanket arg.

    Non-vacuity first: a renamed role would otherwise leave both halves passing over an empty
    set, which is the failure mode that broke nine guards in six pull requests.
    """
    args = _sidecar_args()
    assert set(args) == set(ARRS), (
        f"rendered no exportarr sidecar for {sorted(set(ARRS) - set(args))} — this guard "
        "would pass vacuously"
    )

    flag = "--enable-additional-metrics"
    assert flag in args["sonarr"], (
        f"sonarr's exportarr must carry {flag}; without it the scrape publishes no "
        f"sonarr_episode_quality_total. Got {args['sonarr']}"
    )
    for arr in ("radarr", "prowlarr"):
        assert flag not in args[arr], (
            f"{arr}'s exportarr must NOT carry {flag} — its collector has no block behind "
            f"the flag, so it is pure scrape cost. Got {args[arr]}"
        )


def _scrape_jobs() -> list[dict]:
    """claude-otel's scrape_configs, read from the RENDERED prometheus ConfigMap.

    Through the render rather than the template text (#2809). The text form split
    `prometheus.yaml.j2` on the job's name and then asked whether two strings appeared
    anywhere in the slice that followed, which three different mistakes satisfy: `9707` in one
    of the four prose comments that sit above the job, the `__meta_...port_number` label used
    as a `target_label` instead of in a `keep` rule, or the two appearing in separate relabel
    entries that have nothing to do with each other. The parsed job is what Prometheus loads,
    so a keep rule there is a keep rule.
    """
    for role, template, doc in rendered_docs():
        if role != "claude-otel" or "prometheus" not in str(template):
            continue
        if doc.get("kind") != "ConfigMap":
            continue
        return yaml_fast.safe_load(doc["data"]["prometheus.yml"])["scrape_configs"]
    raise AssertionError(
        "no prometheus ConfigMap rendered — this guard watches nothing"
    )


def port_keep_rule(jobs: list[dict]) -> dict | None:
    """The `exportarr` job's relabel entry that keeps targets by container port number.

    None when the job is absent, or keeps on anything other than that label — which is the
    `app`-label mistake this guard exists to refuse.
    """
    for job in jobs:
        if job.get("job_name") != "exportarr":
            continue
        for rule in job.get("relabel_configs") or []:
            if rule.get("action") != "keep":
                continue
            if rule.get("source_labels") == [
                "__meta_kubernetes_pod_container_port_number"
            ]:
                return rule
    return None


def test_prometheus_scrapes_the_sidecars_by_port_not_by_app_label():
    """Selecting on `app` would also match the *arr's own container port.

    The sidecar shares its *arr's pod, so both containers carry `app: sonarr`. A label-only
    keep rule therefore scrapes sonarr's web UI as if it were a metrics endpoint — which does
    not error, it just yields a target that returns HTML and a job that is permanently down.
    """
    rule = port_keep_rule(_scrape_jobs())
    assert rule is not None, (
        "the exportarr job must keep targets on "
        "__meta_kubernetes_pod_container_port_number — an `app` label alone matches the "
        "*arr's own web port too"
    )
    assert rule["regex"] == str(METRICS_PORT), (
        f"the keep rule matches port {rule['regex']!r}, not the sidecar's {METRICS_PORT}"
    )


def test_a_job_that_keeps_on_the_app_label_is_flagged():
    """The reject half: the shape whose targets return HTML, run against a synthetic job list.

    The tree is supposed to be clean, so the accept case above passes identically whether the
    rule works or matches nothing at all.
    """
    assert (
        port_keep_rule(
            [
                {
                    "job_name": "exportarr",
                    "relabel_configs": [
                        {
                            "source_labels": ["__meta_kubernetes_pod_label_app"],
                            "action": "keep",
                            "regex": "sonarr|radarr|prowlarr",
                        },
                        # Present, but as a rename rather than a filter — the slice the text
                        # form read could not tell this from a keep rule.
                        {
                            "source_labels": [
                                "__meta_kubernetes_pod_container_port_number"
                            ],
                            "target_label": "port",
                        },
                    ],
                }
            ]
        )
        is None
    )


def test_a_job_list_without_the_exportarr_entry_is_flagged():
    assert port_keep_rule([{"job_name": "speedtest"}]) is None


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
        assert yaml_fast.safe_load(yaml.safe_dump(containers)) == containers
