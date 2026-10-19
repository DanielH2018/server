"""The crowdsec-jobs sidecar's scheduler, and the pod layout it runs from.

`files/crowdsec_jobs.py` runs the remote allowlist, the AppSec verify and the bouncer prune in
the crowdsec pod since H2.4b, and the home allowlist since H2.4c, on the schedules their host
crons had. The first tests drive its
schedule and its verdict handling with stubs. The last ones read the rendered Deployment and
ConfigMap, build the sidecar's `/jobs` directory exactly as the kubelet mounts it, and run every
module from it, so a module the ConfigMap stops carrying fails here rather than in the pod.

Run: uv run pytest ansible/roles/k8s/crowdsec/tests/test_crowdsec_jobs.py
"""

import subprocess
import sys
from datetime import UTC, datetime

import pytest
from _k8s_render import render_role_template
from lib import yaml_fast

import appsec_verify
import bouncer_prune
import crowdsec_jobs as cj
import home_allowlist
import remote_allowlist

JOBS = {job.tag: job for job in cj.JOBS}


def _minutes(tag: str) -> list[int]:
    return [minute for minute in range(60) if cj.due(JOBS[tag], minute)]


def test_each_job_keeps_its_host_crons_utc_minutes():
    # The retired crons: `*/5`, `*/15` and `17` in their minute fields.
    assert _minutes("crowdsec-remote-allowlist") == list(range(0, 60, 5))
    assert _minutes("crowdsec-appsec-verify") == [0, 15, 30, 45]
    assert _minutes("crowdsec-bouncer-prune") == [17]


def test_the_home_allowlist_runs_a_minute_after_each_discovery_slot(cronjob):
    # The CronJob writes at minutes 0, 5, ...; the job reads one minute later.
    period = int(cronjob["spec"]["schedule"].split()[0].removeprefix("*/"))
    assert _minutes("crowdsec-home-allowlist") == list(range(1, 60, period))


def test_every_timeout_ends_a_hung_run_before_its_next_slot():
    for job in cj.JOBS:
        assert job.timeout_s < job.period * 60, job.tag


def _run(job_tag, returncode=0, stdout="", raises=None):
    """Runs one job through `run_job` with a stubbed child and push; returns (printed, pushes)."""
    printed, pushes = [], []

    def runner(argv, **kwargs):
        assert argv == [sys.executable, str(cj.JOBS_DIR / JOBS[job_tag].script)]
        assert kwargs["timeout"] == JOBS[job_tag].timeout_s
        if raises:
            raise raises
        return subprocess.CompletedProcess(argv, returncode, stdout)

    def push(status, msg, base, token, log):
        pushes.append((status, msg, base, token))
        return True

    env = {
        "REMOTE_ALLOWLIST_PUSH_URL": "http://kuma:3001/api/push/tok-remote",
        "APPSEC_PUSH_URL": "http://kuma:3001/api/push/tok-appsec",
    }
    rc = cj.run_job(
        JOBS[job_tag], runner=runner, push=push, env=env, out=printed.append
    )
    return rc, printed, pushes


def test_a_clean_run_pushes_up_with_its_last_line():
    rc, printed, pushes = _run(
        "crowdsec-remote-allowlist", stdout="noise\nremote-ips: 2 live\n"
    )
    assert rc == 0
    assert printed == ["crowdsec-remote-allowlist: status=up remote-ips: 2 live"]
    assert pushes == [("up", "remote-ips: 2 live", "http://kuma:3001", "tok-remote")]


def test_a_failed_run_pushes_down_and_logs_the_lines_above_its_summary():
    rc, printed, pushes = _run(
        "crowdsec-appsec-verify",
        returncode=1,
        stdout="Traceback line\nno enabled appsec-configs\n",
    )
    assert rc == 1
    assert printed == [
        "crowdsec-appsec-verify: Traceback line",
        "crowdsec-appsec-verify: status=down no enabled appsec-configs",
    ]
    assert pushes == [
        ("down", "no enabled appsec-configs", "http://kuma:3001", "tok-appsec")
    ]


def test_a_run_with_no_output_names_its_script_and_exit_code():
    _, _, pushes = _run("crowdsec-appsec-verify", returncode=2)
    assert pushes[0][:2] == ("down", "appsec_verify.py exited 2 with no output")


def test_a_run_past_its_timeout_is_down():
    _, _, pushes = _run(
        "crowdsec-remote-allowlist",
        raises=subprocess.TimeoutExpired(cmd="x", timeout=1),
    )
    assert pushes[0][:2] == (
        "down",
        "remote_allowlist.py killed after its 180s timeout",
    )


def test_the_bouncer_prune_logs_and_pushes_nothing():
    _, printed, pushes = _run(
        "crowdsec-bouncer-prune", returncode=1, stdout="refused: no live row\n"
    )
    assert pushes == []
    assert printed == ["crowdsec-bouncer-prune: status=down refused: no live row"]


class _Alive:
    def __init__(self, alive):
        self.alive = alive

    def is_alive(self):
        return self.alive


def test_a_job_still_running_at_its_next_slot_is_skipped():
    started, printed = [], []

    def start(job):
        started.append(job.tag)
        return _Alive(True)

    running: dict[str, cj.Running] = {"crowdsec-remote-allowlist": _Alive(True)}
    cj.tick(0, running, start=start, out=printed.append)
    assert started == ["crowdsec-appsec-verify"]
    assert printed == [
        "crowdsec-remote-allowlist: skipped minute 00, the previous run is still going"
    ]
    running["crowdsec-remote-allowlist"] = _Alive(False)
    cj.tick(5, running, start=start)
    assert started == ["crowdsec-appsec-verify", "crowdsec-remote-allowlist"]


def test_no_job_starts_in_the_startup_grace():
    # The clock starts at 12:59:30 UTC and wakes 1 ms early once, which sleeps again.
    now = [1792303170.0]
    wakes = iter([-0.001])

    def sleep(seconds):
        now[0] += seconds + next(wakes, 0.0)

    started = []

    def start(job):
        started.append((job.tag, int(now[0] // 60) % 60))
        return _Alive(False)

    cj.main(clock=lambda: now[0], sleep=sleep, start=start, minutes=7)
    # 13:00 and 13:01 fall inside the 120 s grace from 12:59:30; 13:05 is the first slot after,
    # and 13:06 is the home allowlist's.
    assert started == [
        ("crowdsec-remote-allowlist", 5),
        ("crowdsec-home-allowlist", 6),
    ]


def test_a_late_wake_still_runs_the_minute_it_slept_to():
    # From 13:04:30 UTC the sleep to 13:05 overshoots by 61 s and wakes at 13:06:01. The clock
    # then reads minute 6, which has no job; the boundary it slept to is minute 5, which starts
    # the remote allowlist. Minute 7, the next boundary, has no job either.
    now = [1792328670.0]
    wakes = iter([61.0])

    def sleep(seconds):
        now[0] += seconds + next(wakes, 0.0)

    started = []

    def start(job):
        started.append(job.tag)
        return _Alive(False)

    cj.main(clock=lambda: now[0], sleep=sleep, start=start, minutes=2, grace_s=0)
    assert started == ["crowdsec-remote-allowlist"]


@pytest.fixture(scope="module")
def cronjob():
    [doc] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "home-ips-cronjob.yaml.j2")
    )
    return doc


@pytest.fixture(scope="module")
def pod():
    [deployment] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "deployment.yaml.j2")
    )
    return deployment["spec"]["template"]["spec"]


def _mounts(container):
    return {m["mountPath"]: m["name"] for m in container["volumeMounts"]}


def test_the_sidecar_runs_the_scheduler_from_the_configmap_with_the_tools_the_modules_name(
    pod,
):
    [sidecar] = [c for c in pod["containers"] if c["name"] == "crowdsec-jobs"]
    assert sidecar["command"] == ["python3", "/jobs/crowdsec_jobs.py"]
    volumes = {v["name"]: v for v in pod["volumes"]}
    mounts = _mounts(sidecar)
    assert volumes[mounts["/jobs"]]["configMap"]["name"] == "crowdsec-jobs"
    # The paths each module runs its binaries from are the copies the initContainers make.
    copies = {
        c["command"][-1]: _mounts(c)["/tools"]
        for c in pod["initContainers"]
        if c["name"].startswith("jobs-")
    }
    for path in (
        appsec_verify.CSCLI,
        bouncer_prune.CSCLI,
        remote_allowlist.CSCLI,
        remote_allowlist.KUBECTL,
        home_allowlist.CSCLI,
        home_allowlist.KUBECTL,
    ):
        assert copies[path] == mounts["/tools"]
    # The engine's config and database, which cscli reads in the sidecar as in the engine.
    [engine] = [c for c in pod["containers"] if c["name"] == "crowdsec"]
    for path in ("/etc/crowdsec", "/var/lib/crowdsec/data"):
        assert mounts[path] == _mounts(engine)[path]
    # Each pushing job's variable reaches the sidecar from the crowdsec-jobs Secret.
    env = {e["name"]: e.get("valueFrom", {}) for e in sidecar["env"]}
    for job in cj.JOBS:
        if job.push_env:
            assert env[job.push_env]["secretKeyRef"]["name"] == "crowdsec-jobs"


def test_the_engines_trace_link_resolves_to_a_writable_directory_in_the_sidecar(pod):
    # The engine's entrypoint links `trace` into crowdsec-db as
    # /staging/var/lib/crowdsec/data/trace, a path only the engine image has. cscli creates its
    # trace directory through that link on every call, so the sidecar mounts a directory there.
    [sidecar] = [c for c in pod["containers"] if c["name"] == "crowdsec-jobs"]
    [mount] = [
        m
        for m in sidecar["volumeMounts"]
        if m["mountPath"] == "/staging/var/lib/crowdsec/data/trace"
    ]
    assert not mount.get("readOnly")
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert "emptyDir" in volumes[mount["name"]]


def test_only_the_sidecar_holds_the_service_account_token(pod):
    # The Role reads every homelab pod's log, so the token must not reach the engine container,
    # which parses attacker-controlled requests.
    assert pod["serviceAccountName"] == "crowdsec-jobs"
    assert pod["automountServiceAccountToken"] is False
    [token] = [v for v in pod["volumes"] if v["name"] == "crowdsec-jobs-token"]
    assert any("serviceAccountToken" in src for src in token["projected"]["sources"])
    holders = [
        c["name"]
        for c in pod["containers"] + pod["initContainers"]
        if "crowdsec-jobs-token" in _mounts(c).values()
    ]
    assert holders == ["crowdsec-jobs"]


def test_the_kubeconfig_names_the_paths_the_sidecar_mounts(pod):
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "jobs-configmap.yaml.j2")
    )
    kubeconfig = yaml_fast.safe_load(configmap["data"]["kubeconfig"])
    [sidecar] = [c for c in pod["containers"] if c["name"] == "crowdsec-jobs"]
    env = {e["name"]: e.get("value") for e in sidecar["env"]}
    mounts = _mounts(sidecar)
    # KUBECONFIG names the file the ConfigMap mount carries.
    assert env["KUBECONFIG"] == "/jobs/kubeconfig"
    assert mounts["/jobs"] == "crowdsec-jobs-code"
    token_dir = next(
        path for path, name in mounts.items() if name == "crowdsec-jobs-token"
    )
    [user] = kubeconfig["users"]
    [cluster] = kubeconfig["clusters"]
    assert user["user"]["tokenFile"] == f"{token_dir}/token"
    assert cluster["cluster"]["certificate-authority"] == f"{token_dir}/ca.crt"
    assert env["PYTHONUNBUFFERED"] == "1"


def test_each_log_line_carries_the_syslog_prefix_and_is_flushed():
    # monitor-bridge's Swallowed Push Verdicts and probe.py alerts both want exactly two
    # whitespace-free tokens before the tag: a time and the host.
    printed = []
    cj.emit(
        "crowdsec-appsec-verify: status=down no enabled appsec-configs",
        now=lambda: datetime(2026, 10, 18, 13, 5, tzinfo=UTC),
        host=lambda: "crowdsec-6f9c-x2",
        write=lambda *args, **kwargs: printed.append((args, kwargs)),
    )
    assert printed == [
        (
            (
                "2026-10-18T13:05:00Z crowdsec-6f9c-x2 "
                "crowdsec-appsec-verify: status=down no enabled appsec-configs",
            ),
            {"flush": True},
        )
    ]


def test_every_module_imports_from_the_pods_mounted_layout(tmp_path):
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "jobs-configmap.yaml.j2")
    )
    for key, text in configmap["data"].items():
        (tmp_path / key).write_text(text)
    assert {job.script for job in cj.JOBS} <= set(configmap["data"])
    # `--help` returns after every module-level import has run, so exit 0 proves the import
    # closure resolves from the mounted files alone. The pod runs `python3 /jobs/<script>`,
    # which puts the script's own directory on sys.path, so this does too. `-S` drops the
    # venv's site-packages, because the pod's bare python image has no wheels, and `-E` and
    # `-s` drop PYTHONPATH and the user site.
    for script in ["crowdsec_jobs.py", *(job.script for job in cj.JOBS)]:
        proc = subprocess.run(
            [sys.executable, "-E", "-s", "-S", str(tmp_path / script), "--help"],
            cwd=tmp_path,
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert proc.returncode == 0, (script, proc.stderr)


def test_the_sidecar_pages_a_lookup_older_than_three_cronjob_periods(pod, cronjob):
    [sidecar] = [c for c in pod["containers"] if c["name"] == "crowdsec-jobs"]
    env = {e["name"]: e.get("value") for e in sidecar["env"]}
    period = int(cronjob["spec"]["schedule"].split()[0].removeprefix("*/"))
    assert int(env["HOME_IPS_STALE_AFTER_S"]) == 3 * period * 60
    assert env["HOME_IPS_CONFIGMAP"] == "crowdsec-home-ips"


def test_the_discovery_imports_from_the_files_its_cronjob_mounts(cronjob, tmp_path):
    """The CronJob mounts two keys of the crowdsec-jobs ConfigMap; the module needs no other."""
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "jobs-configmap.yaml.j2")
    )
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    [volume] = pod["volumes"]
    assert volume["configMap"]["name"] == configmap["metadata"]["name"]
    for item in volume["configMap"]["items"]:
        (tmp_path / item["path"]).write_text(configmap["data"][item["key"]])
    [container] = pod["containers"]
    assert container["command"] == ["python3", "/jobs/home_ips_discover.py"]
    proc = subprocess.run(
        [
            sys.executable,
            "-E",
            "-s",
            "-S",
            str(tmp_path / "home_ips_discover.py"),
            "--help",
        ],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr


def test_the_discovery_runs_on_the_host_network_of_the_primary_node(cronjob):
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    assert pod["hostNetwork"] is True
    assert pod["dnsPolicy"] == "ClusterFirstWithHostNet"
    assert pod["nodeSelector"] == {"kubernetes.io/hostname": "daniel-box"}


def fields_an_apply_would_own(configmap: dict) -> list[str]:
    """The data keys a rendered ConfigMap hands to Ansible's and Flux's apply.

    The CronJob is the only writer of crowdsec-home-ips' data (the DECIDED marker in
    templates/home-ips-configmap.yaml.j2). A key in the render would be reset on every deploy,
    clobbering the lookup the CronJob wrote.
    """
    return sorted({**configmap.get("data", {}), **configmap.get("binaryData", {})})


def test_the_home_ips_configmap_renders_no_field_the_cronjob_writes():
    [configmap] = yaml_fast.safe_load_all(
        render_role_template("crowdsec", "home-ips-configmap.yaml.j2")
    )
    assert configmap["metadata"]["name"] == "crowdsec-home-ips"
    assert fields_an_apply_would_own(configmap) == []


def test_a_placeholder_data_key_in_the_render_is_flagged():
    placeholder = {"metadata": {"name": "crowdsec-home-ips"}, "data": {"ipv4": ""}}
    assert fields_an_apply_would_own(placeholder) == ["ipv4"]
