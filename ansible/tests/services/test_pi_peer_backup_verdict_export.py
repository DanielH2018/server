"""pull-pi-peers.sh exports its verdict to a ConfigMap, and its Role grants exactly that call.

M6 batch B7: the CronJob's verdict leaves the pod as annotations on one ConfigMap, which
kube-state-metrics exports. The image has no kubectl, so the script sends one merge PATCH with
curl. The whole script runs here with stubs on `PATH`: `curl` records each call and answers the
PATCH with a chosen status, `rsync` and `find` stand in for the pull, and the ServiceAccount
mount is a directory in `tmp_path`.

Run: uv run pytest ansible/tests/services/test_pi_peer_backup_verdict_export.py
"""

import json
import os

from _helpers import ROLES
from _k8s_render import render_role_template
from lib import yaml_fast
from lib.proc_testing import fake_bin, path_with, run

SCRIPT_PATH = ROLES / "k8s/pi-peer-backup/files/pull-pi-peers.sh"
SCHEDULED = "pi-peer-backup-29312345-x7k2q"
GATE = "pi-peer-backup-deploy-gate-1a2b3c4d-x7k2q"
CONFIGMAP = "pi-peer-backup-verdict"
URL = f"https://kubernetes.default.svc/api/v1/namespaces/homelab/configmaps/{CONFIGMAP}"

# A curl that records each call as one line of NUL-free JSON and answers the PATCH with
# $PATCH_CODE; every other call is the Kuma push and gets a 200.
CURL = r"""#!/bin/sh
patch=0
for a in "$@"; do [ "$a" = PATCH ] && patch=1; done
printf '%s\n' "$*" >> "$CURL_LOG"
if [ "$patch" = 1 ]; then
  for a in "$@"; do case "$a" in --data) next=1 ;; *) [ "${next:-0}" = 1 ] && printf '%s' "$a" > "$PATCH_BODY"; next=0 ;; esac; done
  printf '%s' "$PATCH_CODE"
else
  printf '\n200 text/plain'
fi
"""
RSYNC_OK = "#!/bin/sh\nexit 0\n"
FIND_TWO_FILES = "#!/bin/sh\nprintf 'a\\nb\\n'\n"


def _run(
    tmp_path,
    *,
    hostname=SCHEDULED,
    configmap=CONFIGMAP,
    patch_code="200",
    rsync=RSYNC_OK,
):
    sa = tmp_path / "sa"
    sa.mkdir(exist_ok=True)
    (sa / "token").write_text("tok-abc")
    (sa / "namespace").write_text("homelab")
    (sa / "ca.crt").write_text("ca")
    log, body = tmp_path / "curl.log", tmp_path / "patch.body"
    stubs = fake_bin(
        tmp_path / "bin",
        curl=CURL,
        rsync=rsync,
        find=FIND_TWO_FILES,
        ssh="#!/bin/sh\nexit 0\n",
    )
    env = dict(os.environ)
    env.pop("HC_PING_URL", None)
    env |= {
        "PATH": path_with(stubs, env=env),
        "HOME": str(tmp_path / "home"),
        "HOSTNAME": hostname,
        "PI_SRC": "ubuntu@192.0.2.1:/src/",
        "KUMA_PUSH_URL": "http://kuma.example/api/push/token",
        "SERVICEACCOUNT_DIR": str(sa),
        "CURL_LOG": str(log),
        "PATCH_BODY": str(body),
        "PATCH_CODE": patch_code,
    }
    if configmap:
        env["VERDICT_CONFIGMAP"] = configmap
    result = run(["bash", str(SCRIPT_PATH)], env=env)
    calls = log.read_text().splitlines() if log.exists() else []
    patches = [c for c in calls if " PATCH " in f" {c} "]
    return result, patches, (json.loads(body.read_text()) if body.exists() else None)


def _annotations(body):
    return body["metadata"]["annotations"]


def test_a_good_pull_patches_the_up_verdict_and_exits_zero(tmp_path):
    result, patches, body = _run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert len(patches) == 1, patches
    assert URL in patches[0]
    assert "Authorization: Bearer tok-abc" in patches[0]
    assert "Content-Type: application/merge-patch+json" in patches[0]
    ann = _annotations(body)
    assert ann["homelab/check-verdict"] == "up"
    assert ann["homelab/check-message"] == "pulled 2 peer file(s) from daniel-pi"
    assert ann["homelab/check-last-run"].endswith("Z")


def test_a_failed_pull_patches_down_with_the_address_redacted_and_exits_one(tmp_path):
    rsync = (
        "#!/bin/sh\necho 'rsync: connection to ubuntu@192.0.2.1 refused' >&2\nexit 12\n"
    )
    result, patches, body = _run(tmp_path, rsync=rsync)
    assert result.returncode == 1
    assert len(patches) == 1
    ann = _annotations(body)
    assert ann["homelab/check-verdict"] == "down"
    assert "192.0.2.1" not in json.dumps(body)
    assert "<ip>" in ann["homelab/check-message"]


def test_a_rejected_patch_fails_a_good_pull(tmp_path):
    # The success path exits 1 on a failed export, so the CronJob's last successful time stops
    # moving and PiPeerBackupNotReporting fires.
    result, patches, _ = _run(tmp_path, patch_code="403")
    assert len(patches) == 1
    assert result.returncode == 1
    assert "export failed" in result.stderr


def test_the_deploy_gate_run_exports_nothing(tmp_path):
    result, patches, body = _run(tmp_path, hostname=GATE)
    assert result.returncode == 0, result.stderr
    assert patches == []
    assert body is None


def test_no_configmap_exports_nothing(tmp_path):
    result, patches, _ = _run(tmp_path, configmap="")
    assert result.returncode == 0, result.stderr
    assert patches == []


def test_the_role_grants_patch_on_the_one_named_configmap_and_the_cronjob_sets_it():
    docs = [
        d
        for d in yaml_fast.safe_load_all(
            render_role_template("pi-peer-backup", "rbac.yaml.j2")
        )
        if d
    ]
    [role] = [d for d in docs if d["kind"] == "Role"]
    assert role["rules"] == [
        {
            "apiGroups": [""],
            "resources": ["configmaps"],
            "verbs": ["patch"],
            "resourceNames": [CONFIGMAP],
        }
    ]
    [cronjob] = [
        d
        for d in yaml_fast.safe_load_all(
            render_role_template("pi-peer-backup", "cronjob.yaml.j2")
        )
        if d
    ]
    pod = cronjob["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    env = {e["name"]: e.get("value") for e in pod["containers"][0]["env"]}
    assert env["VERDICT_CONFIGMAP"] == CONFIGMAP
    assert pod["serviceAccountName"] == "pi-peer-backup"
    assert pod["automountServiceAccountToken"] is True
    [configmap] = [
        d
        for d in yaml_fast.safe_load_all(
            render_role_template("pi-peer-backup", "verdict-configmap.yaml.j2")
        )
        if d
    ]
    assert configmap["metadata"]["name"] == CONFIGMAP
    assert "annotations" not in configmap["metadata"]
