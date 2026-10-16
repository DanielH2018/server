"""The stale-checkout guard refuses a checkout that would write a Flux-owned object (H1.1h).

Each case runs the role's real `tasks/stale-checkout-guard.yml` and `stale-checkout-check.yml`
in a real `ansible-playbook`, against a stub `k3s` that answers the inventory read from a
script of answers. ansible-core's own tag selection decides which instances run, so the
selection cases test the per-tag-set design rather than a model of it. The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *Verification*, *Stale-checkout
guard*.

Run: uv run pytest ansible/tests/setup/test_k3s_stale_checkout_guard.py
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from lib.git_testing import scrubbed_env
from lib.proc_testing import fake_bin

from _helpers import ANSIBLE
from lib import yaml_fast
from lib.repo_paths import K3S_DEFAULTS, K3S_ROLE

TASKS = K3S_ROLE / "tasks"
GUARD = TASKS / "stale-checkout-guard.yml"

# Inventory ids the role's real rows name, at the role's defaults.
SPEAKER = "metallb-system_speaker_apps_DaemonSet"
READONLY_SA = "kube-system_homelab-readonly__ServiceAccount"
COREDNS = "kube-system_coredns_apps_Deployment"
UNMAPPED = "homelab_jellyfin_apps_Deployment"

REFUSED = (
    "The connection to the server 127.0.0.1:6443 was refused - "
    "did you specify the right host or port?"
)
NO_FLUX = 'error: the server doesn\'t have a resource type "kustomizations"'

# Each call takes the next line of `answers`, and the last line repeats. Every call appends
# to `calls`, so a test counts the tries the bounded retry made.
_K3S = """#!/bin/sh
here=$(dirname "$0")
echo call >> "$here/calls"
n=$(wc -l < "$here/calls")
answer=$(sed -n "${n}p" "$here/answers")
[ -n "$answer" ] || answer=$(tail -n 1 "$here/answers")
case "$answer" in
  refuse) cat "$here/refuse.txt" >&2; exit 1 ;;
  no-flux) cat "$here/no-flux.txt" >&2; exit 1 ;;
  inventory) cat "$here/inventory.json" ;;
esac
"""

# A `sudo` stand-in for `become: true`: it runs the `-c` command the sudo plugin builds, as
# the current user. Copied from ansible/tests/longhorn/test_longhorn_api.py.
_BECOME = (
    "#!/bin/sh\n"
    'last=""\n'
    'prev=""\n'
    'for a in "$@"; do prev="$last"; last="$a"; done\n'
    'if [ "$prev" = "-c" ]; then exec /bin/sh -c "$last"; fi\n'
    'exec "$@"\n'
)


def _inventory(owned: dict[str, list[str]]) -> dict:
    """A `kubectl get kustomizations -o json` answer: one Kustomization per key."""
    items = [
        {
            "metadata": {"name": name},
            "status": {"inventory": {"entries": [{"id": i, "v": "v1"} for i in ids]}},
        }
        for name, ids in owned.items()
    ]
    # A Kustomization that has never applied carries no inventory.
    items.append({"metadata": {"name": "not-yet-applied"}, "status": {}})
    return {"items": items}


def _run(
    tmp_path: Path,
    answers: list[str],
    owned: dict[str, list[str]] | None = None,
    *,
    args: tuple[str, ...] = (),
    guard: list | None = None,
    binary: bool = True,
    retries: int = 2,  # three tries
) -> tuple[subprocess.CompletedProcess[str], int]:
    """Run the guard once and return the run and how many reads the stub answered."""
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True)
    shutil.copy(TASKS / "stale-checkout-check.yml", tasks)
    if guard is None:
        shutil.copy(GUARD, tasks)
    else:
        (tasks / GUARD.name).write_text(yaml.safe_dump(guard, sort_keys=False))
    bin_dir = fake_bin(tmp_path / "bin", k3s=_K3S, fake_become=_BECOME)
    (bin_dir / "answers").write_text("\n".join(answers) + "\n")
    (bin_dir / "refuse.txt").write_text(REFUSED + "\n")
    (bin_dir / "no-flux.txt").write_text(NO_FLUX + "\n")
    (bin_dir / "inventory.json").write_text(json.dumps(_inventory(owned or {})))
    play = [
        {
            "hosts": "localhost",
            "connection": "local",
            "gather_facts": False,
            "vars_files": [str(K3S_DEFAULTS)],
            "tasks": [{"ansible.builtin.import_tasks": str(tasks / GUARD.name)}],
        }
    ]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    # Extra vars, because a play's `vars_files` outrank its `vars`.
    extra = {
        "ansible_become_exe": str(bin_dir / "fake_become"),
        "k3s_stale_checkout_binary": str(
            bin_dir / "k3s" if binary else tmp_path / "absent" / "k3s"
        ),
        "k3s_stale_checkout_retries": retries,
        "k3s_stale_checkout_delay": 0,
        "k3s_manage_backup_targets": True,
    }
    env = scrubbed_env()
    env["ANSIBLE_FILTER_PLUGINS"] = str(ANSIBLE / "filter_plugins")
    env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    env["ANSIBLE_CONFIG"] = str(tmp_path / "none.cfg")
    env["ANSIBLE_NOCOLOR"] = "1"
    result = subprocess.run(
        [
            "ansible-playbook",
            "play.yml",
            "-i",
            "localhost,",
            "-e",
            json.dumps(extra),
            *args,
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    calls = bin_dir / "calls"
    return result, len(calls.read_text().splitlines()) if calls.exists() else 0


def _failed_naming(result: subprocess.CompletedProcess[str], *needles: str) -> None:
    output = result.stdout + result.stderr
    assert result.returncode != 0, output
    for needle in needles:
        assert needle in output, f"{needle!r} not in the failure:\n{output}"


def _passed(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


def test_a_checkout_holding_install_metallb_fails_while_flux_owns_the_speaker(tmp_path):
    result, _ = _run(
        tmp_path, ["inventory"], {"metallb": [SPEAKER]}, args=("--tags", "metallb")
    )
    _failed_naming(result, "Install MetalLB", SPEAKER, "Kustomization metallb")


def test_an_inventory_holding_no_mapped_id_passes(tmp_path):
    result, calls = _run(tmp_path, ["inventory"], {"jellyfin": [UNMAPPED]})
    _passed(result)
    assert calls == 8, "every instance reads the inventory under a whole-file run"


def _without(task: str) -> list:
    """The real guard with the row for `task` deleted, and its instance if that empties it."""
    guard = []
    for instance in yaml_fast.safe_load(GUARD.read_text()):
        rows = [
            r for r in instance["vars"]["k3s_stale_checkout_rows"] if r["task"] != task
        ]
        if rows:
            guard.append({**instance, "vars": {"k3s_stale_checkout_rows": rows}})
    return guard


def test_a_checkout_whose_coredns_writers_are_gone_passes_while_flux_owns_coredns(
    tmp_path,
):
    guard = _without("Run the CoreDNS image k3s bundles")
    guard = [
        i
        for i in guard
        if not any(
            r["task"] == "Point cluster DNS at Pi-hole first"
            for r in i["vars"]["k3s_stale_checkout_rows"]
        )
    ]
    result, _ = _run(
        tmp_path,
        ["inventory"],
        {"coredns": [COREDNS]},
        args=("--tags", "coredns"),
        guard=guard,
    )
    _passed(result)


def test_the_read_only_identity_fails_only_while_its_writer_is_in_the_checkout(
    tmp_path,
):
    owned = {"cluster-base": [READONLY_SA]}
    host_half_only, _ = _run(
        tmp_path / "a",
        ["inventory"],
        owned,
        args=("--tags", "kubeconfig"),
        guard=_without("Apply the read-only cluster identity"),
    )
    _passed(host_half_only)
    restored, _ = _run(
        tmp_path / "b", ["inventory"], owned, args=("--tags", "kubeconfig")
    )
    _failed_naming(restored, "Apply the read-only cluster identity", READONLY_SA)


def test_an_api_without_flux_passes_on_the_first_try(tmp_path):
    result, calls = _run(tmp_path, ["no-flux"], args=("--tags", "kubeconfig"))
    _passed(result)
    assert calls == 1


def test_a_host_without_k3s_passes_without_reading(tmp_path):
    result, calls = _run(tmp_path, ["refuse"], binary=False)
    _passed(result)
    assert calls == 0


def test_an_api_that_never_answers_fails_after_the_bounded_retry(tmp_path):
    result, calls = _run(tmp_path, ["refuse"], args=("--tags", "kubeconfig"), retries=3)
    _failed_naming(result, "the k3s API is unreachable", "was refused")
    assert calls == 4, "`retries: 3` is the first try plus three retries"


def test_two_refusals_then_a_clean_inventory_pass(tmp_path):
    result, calls = _run(
        tmp_path,
        ["refuse", "refuse", "inventory"],
        {"jellyfin": [UNMAPPED]},
        args=("--tags", "kubeconfig"),
    )
    _passed(result)
    assert calls == 3


def test_two_refusals_then_a_mapped_id_fail_and_name_the_task(tmp_path):
    result, _ = _run(
        tmp_path,
        ["refuse", "refuse", "inventory"],
        {"cluster-base": [READONLY_SA]},
        args=("--tags", "kubeconfig"),
    )
    _failed_naming(result, "Apply the read-only cluster identity")


def test_a_check_run_still_reads_and_fails(tmp_path):
    result, calls = _run(
        tmp_path,
        ["inventory"],
        {"cluster-base": [READONLY_SA]},
        args=("--tags", "kubeconfig", "--check"),
    )
    _failed_naming(result, "Apply the read-only cluster identity")
    assert calls == 1


# ── Selection: ansible-core's own --tags and --skip-tags decide which instances run ──


def test_a_host_only_tag_reaches_no_instance_with_the_api_down(tmp_path):
    # A guard that regains `always`, or a union tag set naming node-address, reads here.
    result, calls = _run(tmp_path, ["refuse"], args=("--tags", "node-address"))
    _passed(result)
    assert calls == 0


def test_tags_kubeconfig_runs_the_kubeconfig_instance(tmp_path):
    result, calls = _run(
        tmp_path,
        ["inventory"],
        {"cluster-base": [READONLY_SA]},
        args=("--tags", "kubeconfig"),
    )
    _failed_naming(result, "Apply the read-only cluster identity")
    assert calls == 1, "only the kubeconfig instance is selected"


def test_skip_tags_kubeconfig_still_runs_the_metallb_instance(tmp_path):
    # Bring-up step 1. One guard tagged with the union of the writers' tags would be skipped
    # here while the metallb writers still ran.
    result, _ = _run(
        tmp_path,
        ["inventory"],
        {"metallb": [SPEAKER]},
        args=("--skip-tags", "kubeconfig"),
    )
    _failed_naming(result, "Install MetalLB", SPEAKER)
