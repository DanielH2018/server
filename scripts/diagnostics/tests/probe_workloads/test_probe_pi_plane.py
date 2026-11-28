"""`probe.py targets --pi` and `probe.py pi containers`: first-command triage for daniel-pi.

The cluster gets `targets`, `alerts` and `health --docker` for a first read of
what's wrong; daniel-pi runs no kubelet and none of them see it. This is the Docker-plane
equivalent.
"""

import json


from diagnostics import probe
from diagnostics.probe_lib import core, pi_plane
from lib.kubectl import WrongCluster


PROMETHEUS_TEMPLATE_TEXT = pi_plane.PROMETHEUS_TEMPLATE_PATH.read_text()


def test_declared_pi_job_names_finds_exactly_the_known_four():
    assert pi_plane.declared_pi_job_names(PROMETHEUS_TEMPLATE_TEXT) == {
        "node-pi",
        "alloy-pi",
        "dockerd-pi",
        "containerd-pi",
    }


def test_declared_pi_job_names_ignores_a_pod_discovery_job():
    text = """\
      - job_name: node
        kubernetes_sd_configs: []
      - job_name: node-pi
        static_configs:
          - targets: ['{{ k8s_pi_client_ip }}:9100']
"""
    assert pi_plane.declared_pi_job_names(text) == {"node-pi"}


def _target(pool, health="up", origin="daniel-pi", last_error=""):
    return {
        "scrapePool": pool,
        "health": health,
        "labels": {"origin": origin, "instance": origin},
        "lastError": last_error,
    }


def test_format_pi_targets_all_up_is_clean():
    declared = {"node-pi", "alloy-pi"}
    active = [_target("node-pi"), _target("alloy-pi")]
    text, code = pi_plane.format_pi_targets(declared, active)
    assert code == 0
    assert "2/2 daniel-pi targets up" in text


def test_format_pi_targets_flags_a_down_job():
    declared = {"node-pi", "alloy-pi"}
    active = [
        _target("node-pi"),
        _target("alloy-pi", health="down", last_error="timeout"),
    ]
    text, code = pi_plane.format_pi_targets(declared, active)
    assert code == 1
    assert "alloy-pi: down — timeout" in text


def test_format_pi_targets_flags_a_declared_job_gone_missing():
    declared = {"node-pi", "alloy-pi"}
    active = [_target("node-pi")]
    text, code = pi_plane.format_pi_targets(declared, active)
    assert code == 1
    assert "alloy-pi: MISSING" in text


def test_from_the_landing_host_the_pi_is_one_ssh_call():
    assert pi_plane.pi_containers_argv("daniel-box.lan") == [
        "ssh",
        "daniel-pi",
        "docker inspect $(docker ps -aq)",
    ]


def test_from_another_host_the_pi_is_reached_through_the_landing_host():
    # The Pi's own shell must expand the substitution, so daniel-box's shell gets it quoted.
    assert pi_plane.pi_containers_argv("daniel-server") == [
        "ssh",
        "daniel-box",
        "ssh daniel-pi 'docker inspect $(docker ps -aq)'",
    ]


def _container(
    name, status="running", health=None, networks=("proxy",), image="x:latest"
):
    state: dict[str, object] = {"Status": status}
    if health:
        state["Health"] = {"Status": health}
    return {
        "Name": f"/{name}",
        "Config": {"Image": image},
        "State": state,
        "NetworkSettings": {"Networks": {n: {} for n in networks}},
    }


def test_format_pi_containers_all_clean():
    containers = [_container("glances"), _container("wg-easy", health="healthy")]
    text, code = pi_plane.format_pi_containers(containers)
    assert code == 0
    assert "all 2 containers clean" in text


def test_format_pi_containers_flags_a_detached_running_container():
    containers = [_container("alloy", networks=())]
    text, code = pi_plane.format_pi_containers(containers)
    assert code == 1
    assert "DETACHED" in text


def test_format_pi_containers_flags_an_unhealthy_container():
    containers = [_container("wg-easy", health="unhealthy")]
    text, code = pi_plane.format_pi_containers(containers)
    assert code == 1
    assert "health=unhealthy" in text


def test_format_pi_containers_does_not_flag_a_merely_stopped_container():
    # A stopped-but-not-detached container is not this check's job: gating on any non-running
    # container would read red on a normal day.
    containers = [_container("old-helper", status="exited", networks=())]
    text, code = pi_plane.format_pi_containers(containers)
    assert code == 0
    assert "status=exited" in text


def test_format_pi_containers_empty_is_a_failure_not_a_pass():
    _text, code = pi_plane.format_pi_containers([])
    assert code == 1


def test_run_pi_targets_dry_run_prints_the_curl(monkeypatch, capsys):
    monkeypatch.setattr(core, "domain", lambda: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")

    class Ns:
        dry_run = True

    assert pi_plane.run_pi_targets(Ns()) == 0
    out = capsys.readouterr().out
    assert "/api/v1/targets" in out


def test_run_pi_containers_dry_run_prints_the_ssh_argv(capsys):
    class Ns:
        dry_run = True

    assert pi_plane.run_pi_containers(Ns()) == 0
    out = capsys.readouterr().out
    assert out.strip() == " ".join(pi_plane.pi_containers_argv())


def test_run_pi_targets_end_to_end_reports_a_missing_job(monkeypatch, capsys):
    monkeypatch.setattr(core, "domain", lambda: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")

    def fake_fetch(url, resolve=None):
        return json.dumps({"data": {"activeTargets": [_target("node-pi")]}})

    monkeypatch.setattr(core, "fetch", fake_fetch)

    class Ns:
        dry_run = False

    assert pi_plane.run_pi_targets(Ns()) == 1
    out = capsys.readouterr().out
    assert "alloy-pi: MISSING" in out


def test_format_targets_all_up_is_clean():
    text, code = pi_plane.format_targets([_target("node"), _target("loki")])
    assert (code, text) == (0, "2/2 targets up")


def test_format_targets_flags_only_the_down_target():
    """The healthy target prints no row: the view is the exceptions and a count."""
    active = [_target("node"), _target("loki", health="down", last_error="refused")]
    active[1]["labels"]["job"] = "loki"
    text, code = pi_plane.format_targets(active)
    assert code == 1
    assert text.splitlines() == ["1/2 targets up", "  ✗ loki [daniel-pi] refused"]


def test_run_targets_prints_the_count(capsys):
    payload = {"data": {"activeTargets": [_target("node")]}}

    class Ns:
        pi = False

    code = pi_plane.run_targets(
        Ns(), fetch_json=lambda url, resolve=None: (payload, None)
    )
    assert code == 0
    assert capsys.readouterr().out == "1/1 targets up\n"


def _run_targets_one_down(scaled):
    """Run plain `targets` on one down `terraria-stats` target; `scaled` is the lookup's answer."""
    target = _target("terraria-stats", health="down", last_error="refused")
    target["labels"]["job"] = "terraria-stats"
    payload = {"data": {"activeTargets": [_target("node"), target]}}

    class Ns:
        pi = False

    return pi_plane.run_targets(
        Ns(),
        fetch_json=lambda url, resolve=None: (payload, None),
        scaled_lookup=lambda cluster: scaled,
    )


def test_run_targets_passes_a_down_target_whose_deployment_is_scaled_to_zero(capsys):
    """An idle on-demand game server's exporter is down by design, not a failure."""
    assert _run_targets_one_down({"terraria-stats"}) == 0
    assert capsys.readouterr().out.splitlines() == [
        "1/2 targets up, 1 scaled to 0",
        "  - terraria-stats [daniel-pi] scaled to 0",
    ]


def test_run_targets_fails_a_down_target_whose_deployment_runs(capsys):
    assert _run_targets_one_down(set()) == 1
    assert capsys.readouterr().out.splitlines() == [
        "1/2 targets up",
        "  ✗ terraria-stats [daniel-pi] refused",
    ]


def _deployment(name, replicas):
    return {"metadata": {"name": name}, "spec": {"replicas": replicas}}


def test_scaled_to_zero_deployments_names_only_the_zero_replica_ones():
    items = [_deployment("terraria-stats", 0), _deployment("grafana", 1)]
    names = pi_plane.scaled_to_zero_deployments(
        "prod", read=lambda *args: {"items": items}
    )
    assert names == {"terraria-stats"}


def test_scaled_to_zero_deployments_is_empty_when_the_read_fails():
    """A failed read exempts nothing, so the down target stays red, never a traceback."""

    def refuse(*args):
        raise WrongCluster("serves staging")

    assert pi_plane.scaled_to_zero_deployments("prod", read=refuse) == set()


def test_plain_targets_names_a_verdict_and_json_names_none():
    """Plain `targets` judges; `--json` is the raw document the SessionStart hook parses."""
    from diagnostics.probe_lib.cli_parser import _build_parser

    parse = _build_parser().parse_args
    assert probe.verdict_path(parse(["targets"])) == "targets"
    assert probe.verdict_path(parse(["targets", "--json"])) is None
