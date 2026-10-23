"""manifest-prune-check reports a failed `kubectl get` as DOWN, never as zero objects.

The orphan arm reads each kind's live objects from `kubectl get <kind> -A`. A list that fails
yields no rows, and before #4604 that read as "no orphans of this kind": an unreachable
apiserver turned the whole sweep UP, and one failing kind hid behind the seven that answered.

The real template is rendered and run under bash. kubectl, the declared-set reader and both
sourced libraries are stubbed, and every path to a real host file is pointed into `tmp_path`.
The script prepends /usr/local/bin to PATH, where daniel-box's real `k3s` lives, so the stub
replaces the `KUBECTL` literal rather than shadowing `k3s` on PATH.
"""

from lib.proc_testing import run, write_exec
from _shell_render import render_shell_script

TEMPLATE = ("setup", "k3s", "manifest-prune-check.sh.j2")
KINDS = (
    "ingressroute",
    "middleware",
    "service",
    "tlsoption",
    "deployment",
    "daemonset",
    "cronjob",
    "networkpolicy",
)
SEP = "\x1e"


def _run(tmp_path, failing=()):
    """Run the rendered check once against a cluster where every kind holds one declared object.

    Returns the exit code, the `status=` logger lines, and every (status, msg) push.
    `failing` names the kinds whose `kubectl get` exits non-zero.
    """
    body = render_shell_script(*TEMPLATE)

    calls = tmp_path / "calls"
    calls.write_text("")
    kuma_lib = tmp_path / "kuma-push-lib.sh"
    kuma_lib.write_text(
        f'kuma_push() {{ printf "push %s\\n%s{SEP}" "$1" "$2" >> "{calls}"; }}\n'
        f'logger() {{ printf "log %s{SEP}" "$*" >> "{calls}"; }}\n'
        "boot_grace_active() { return 1; }\n"
    )
    check_lib = tmp_path / "homelab-check-lib.sh"
    check_lib.write_text("write_check_textfile() { :; }\n")
    drift_lib = tmp_path / "setup-drift-lib.sh"
    drift_lib.write_text(
        'setup_drift_scan() { DRIFTED=""; STALE=""; DEPLOYED_NOTE=""; MANIFEST_NOTE=""; }\n'
    )
    push_env = tmp_path / "kuma-push.env"
    push_env.write_text("MANIFEST_PRUNE_PUSH_TOKEN=stubtoken\n")
    manifests = tmp_path / "manifests"
    manifests.mkdir()
    uv = write_exec(
        tmp_path / "uv",
        "#!/usr/bin/env bash\n" + "".join(f"echo {kind}/app\n" for kind in KINDS),
    )
    kubectl = write_exec(
        tmp_path / "k3s",
        "#!/usr/bin/env bash\n"
        f'case " {" ".join(failing)} " in\n'
        '  *" $3 "*) echo "error: the server could not find the requested resource" >&2; exit 1 ;;\n'
        "esac\n"
        "echo 'homelab app '\n",
    )

    replacements = {
        "[[ $EUID -ne 0 ]]": "[[ 0 -ne 0 ]]",
        "/usr/local/lib/kuma-push-lib.sh": str(kuma_lib),
        "/usr/local/lib/homelab-check-lib.sh": str(check_lib),
        "/usr/local/lib/setup-drift-lib.sh": str(drift_lib),
        "/etc/rancher/k3s/kuma-push.env": str(push_env),
        "/etc/rancher/k3s/manifests": str(manifests),
        "/etc/healthchecks/ping.env": str(tmp_path / "absent-ping.env"),
        "/usr/local/bin/uv run": f"{uv} run",
        'KUBECTL="k3s kubectl"': f'KUBECTL="{kubectl} kubectl"',
    }
    for literal, stub in replacements.items():
        assert literal in body, f"{'/'.join(TEMPLATE)} no longer references {literal}"
        body = body.replace(literal, stub)

    result = run(["bash", str(write_exec(tmp_path / "check.sh", body))])
    entries = [e for e in calls.read_text().split(SEP) if e]
    logs = [
        e[len("log ") :]
        for e in entries
        if e.startswith("log -t manifest-prune-check status=")
    ]
    pushes = [
        tuple(e[len("push ") :].split("\n", 1))
        for e in entries
        if e.startswith("push ")
    ]
    return result.returncode, logs, pushes


def test_lists_that_all_succeed_are_clean(tmp_path):
    returncode, logs, pushes = _run(tmp_path)
    assert returncode == 0
    assert len(logs) == 1 and "status=up " in logs[0]
    assert [status for status, _ in pushes] == ["up"]


def test_one_failed_list_is_flagged(tmp_path):
    returncode, logs, pushes = _run(tmp_path, failing=("middleware",))
    assert returncode == 1
    assert len(logs) == 1 and "status=down " in logs[0]
    assert "kubectl get failed for middleware" in logs[0]
    assert [status for status, _ in pushes] == ["down"]
    assert "kubectl get failed for middleware" in pushes[0][1]


def test_every_list_failing_is_flagged(tmp_path):
    returncode, logs, _ = _run(tmp_path, failing=KINDS)
    assert returncode == 1
    assert len(logs) == 1 and "status=down " in logs[0]
    assert f"kubectl get failed for {', '.join(KINDS)}" in logs[0]
