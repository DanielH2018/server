"""`probe.py targets --pi` and `probe.py pi containers`: first-command triage for daniel-pi.

The cluster gets `targets`, `kuma-drift`, `alerts` and `health --docker` for a first read of
what's wrong; daniel-pi runs no kubelet and none of them see it. This is the Docker-plane
equivalent.
"""

import json

from diagnostics.probe_lib import core, pi_plane


PROMETHEUS_TEMPLATE_TEXT = pi_plane.PROMETHEUS_TEMPLATE_PATH.read_text()


def test_declared_pi_job_names_finds_exactly_the_known_two():
    # Only two exist in the repo today (node-pi, alloy-pi) — glances has no Prometheus job
    # anywhere, so forcing a bigger set here would fabricate one. A named frozenset, not a
    # bare count, so a future rename shows up as a specific missing/extra name.
    assert pi_plane.declared_pi_job_names(PROMETHEUS_TEMPLATE_TEXT) == {
        "node-pi",
        "alloy-pi",
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
    # The `monitors` N/N-up mistake, avoided: a job that vanished from the scrape config
    # entirely must not read as "1/1 up" just because the survivor is healthy.
    declared = {"node-pi", "alloy-pi"}
    active = [_target("node-pi")]
    text, code = pi_plane.format_pi_targets(declared, active)
    assert code == 1
    assert "alloy-pi: MISSING" in text


def test_format_pi_targets_notes_glances_is_not_scraped():
    declared = {"node-pi", "alloy-pi"}
    active = [_target("node-pi"), _target("alloy-pi")]
    text, _ = pi_plane.format_pi_targets(declared, active)
    assert "glances" in text.lower()


def test_pi_containers_argv_is_a_single_ssh_call():
    argv = pi_plane.pi_containers_argv()
    assert argv[0] == "ssh"
    assert argv[1] == pi_plane.PI_HOST
    # Everything after the host is ONE remote command string — ssh spawns exactly once
    # regardless of how many docker subcommands that string chains together.
    assert len(argv) == 3


def _container(
    name, status="running", health=None, networks=("proxy",), image="x:latest"
):
    state = {"Status": status}
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
    # The reboot-detach failure this repo has hit before: `Up (healthy)` with NO network at
    # all — MEMORY: containers-lose-network-across-pi-reboot.md.
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
    # A stopped-but-not-detached container is not this check's job: this host runs short-
    # lived helpers (docker-proxy-lifecycle) and gating the exit code on ANY non-running
    # container would read red on a normal day and get ignored.
    containers = [_container("old-helper", status="exited", networks=())]
    text, code = pi_plane.format_pi_containers(containers)
    assert code == 0
    assert "status=exited" in text


def test_format_pi_containers_empty_is_a_failure_not_a_pass():
    _text, code = pi_plane.format_pi_containers([])
    assert code == 1


def test_run_pi_targets_dry_run_prints_the_curl(monkeypatch, capsys):
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
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
    assert out.strip().startswith(f"ssh {pi_plane.PI_HOST}")


def test_run_pi_targets_end_to_end_reports_a_missing_job(monkeypatch, capsys):
    monkeypatch.setattr(core, "sops_extract", lambda key: "example.test")
    monkeypatch.setattr(core, "metallb_vip", lambda: "10.0.0.240")

    def fake_fetch(url, resolve=None):
        return json.dumps({"data": {"activeTargets": [_target("node-pi")]}})

    monkeypatch.setattr(core, "fetch", fake_fetch)

    class Ns:
        dry_run = False

    assert pi_plane.run_pi_targets(Ns()) == 1
    out = capsys.readouterr().out
    assert "alloy-pi: MISSING" in out
