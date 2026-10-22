"""The stale-checkout guard refuses a checkout that would write a Flux-owned object (H1.1h).

Each case runs the role's real `tasks/stale-checkout-guard.yml` and `stale-checkout-check.yml`
in a real `ansible-playbook`, against a stub `k3s` that answers the inventory read from a
script of answers. ansible-core's own tag selection decides which instances run, so the
selection cases test the per-tag-set design rather than a model of it. Cases that share a
selection and a guard run as separate inventory hosts of one `ansible-playbook`, each with
its own stub, which halves the module's run time (#5578). The plan is
docs/target-architecture/host/h1-cluster-config-to-flux.md, *Verification*, *Stale-checkout
guard*.

Run: uv run pytest ansible/tests/setup/test_k3s_stale_checkout_guard.py
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple

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
COREDNS = "kube-system_coredns_apps_Deployment"
RECONCILE = "longhorn-system_longhorn-reconcile_batch_CronJob"
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
    items: list[dict] = [
        {
            "metadata": {"name": name},
            "status": {"inventory": {"entries": [{"id": i, "v": "v1"} for i in ids]}},
        }
        for name, ids in owned.items()
    ]
    # A Kustomization that has never applied carries no inventory, and one that applied
    # nothing carries a null entry list.
    items.append({"metadata": {"name": "not-yet-applied"}, "status": {}})
    items.append(
        {"metadata": {"name": "empty"}, "status": {"inventory": {"entries": None}}}
    )
    return {"items": items}


class Case(NamedTuple):
    """One host of a run: the stub's script of answers and the inventory it serves."""

    answers: list[str]
    owned: dict[str, list[str]] | None = None
    binary: bool = True
    retries: int = 2  # three tries


class Outcome(NamedTuple):
    """One host's result: whether it failed, its own output lines, and the reads it made."""

    failed: bool
    output: str
    calls: int


# A recap line: `<host> : ok=3 changed=0 unreachable=0 failed=1 skipped=2 rescued=1 ...`.
_RECAP = re.compile(
    r"^(\S+)\s+:\s+ok=\d+\s+changed=\d+\s+unreachable=\d+\s+failed=(\d+)", re.M
)


def _run(
    tmp_path: Path,
    cases: dict[str, Case],
    *,
    args: tuple[str, ...] = (),
    guard: list | None = None,
) -> dict[str, Outcome]:
    """Run the guard once, one inventory host per case, and return each host's outcome.

    The hosts share the selection (`args`) and the guard, and differ in what their stub
    answers. Forks run them in parallel, and a host that fails does not stop the others, so
    a run of six cases costs about what a run of one does. Each module task is ~0.15s of
    ansible-core importing its own module_utils, which no setting removes (#5578).
    """
    tasks = tmp_path / "tasks"
    tasks.mkdir(parents=True)
    shutil.copy(TASKS / "stale-checkout-check.yml", tasks)
    if guard is None:
        shutil.copy(GUARD, tasks)
    else:
        (tasks / GUARD.name).write_text(yaml.safe_dump(guard, sort_keys=False))
    # The role defaults as inventory group vars, so each host's vars below outrank them. A
    # play's `vars_files` would outrank host vars, and so would `-e`.
    (tmp_path / "group_vars").mkdir()
    shutil.copy(K3S_DEFAULTS, tmp_path / "group_vars" / "all.yml")
    hosts = {}
    for name, case in cases.items():
        bin_dir = fake_bin(tmp_path / name, k3s=_K3S, fake_become=_BECOME)
        (bin_dir / "answers").write_text("\n".join(case.answers) + "\n")
        (bin_dir / "refuse.txt").write_text(REFUSED + "\n")
        (bin_dir / "no-flux.txt").write_text(NO_FLUX + "\n")
        (bin_dir / "inventory.json").write_text(
            json.dumps(_inventory(case.owned or {}))
        )
        hosts[name] = {
            "ansible_connection": "local",
            "ansible_become_exe": str(bin_dir / "fake_become"),
            "k3s_stale_checkout_binary": str(
                bin_dir / "k3s" if case.binary else tmp_path / "absent" / "k3s"
            ),
            "k3s_stale_checkout_retries": case.retries,
        }
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump({"all": {"hosts": hosts}}))
    play = [
        {
            "hosts": "all",
            "gather_facts": False,
            "tasks": [{"ansible.builtin.import_tasks": str(tasks / GUARD.name)}],
        }
    ]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    extra = {"k3s_stale_checkout_delay": 0, "k3s_manage_backup_targets": True}
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
            "hosts.yml",
            "--forks",
            str(len(cases)),
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
    output = result.stdout + result.stderr
    # A selection that runs no task prints an empty recap, so a host it omits passed. The
    # exit code is 2 exactly when some host failed, which catches a recap this regex misread.
    failed = dict.fromkeys(cases, False)
    failed.update((host, int(n) > 0) for host, n in _RECAP.findall(output))
    assert set(failed) == set(cases), f"an unknown host in the recap:\n{output}"
    assert result.returncode == (2 if any(failed.values()) else 0), output
    outcomes = {}
    for name in cases:
        calls = tmp_path / name / "calls"
        own = "\n".join(line for line in output.splitlines() if f"[{name}]" in line)
        outcomes[name] = Outcome(
            failed[name],
            own,
            len(calls.read_text().splitlines()) if calls.exists() else 0,
        )
    return outcomes


def _run_one(tmp_path: Path, case: Case, **kwargs) -> Outcome:
    return _run(tmp_path, {"case": case}, **kwargs)["case"]


def _failed_naming(outcome: Outcome, *needles: str) -> None:
    assert outcome.failed, outcome.output
    for needle in needles:
        assert needle in outcome.output, (
            f"{needle!r} not in the failure:\n{outcome.output}"
        )


def _passed(outcome: Outcome) -> None:
    assert not outcome.failed, outcome.output


pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


# Two shared runs, one host per case: the whole-file run, and the `--tags coredns` run of
# the real guard. Every other case needs its own selection or its own guard, so runs alone.
@pytest.fixture(scope="module")
def whole_file(tmp_path_factory) -> dict[str, Outcome]:
    return _run(
        tmp_path_factory.mktemp("whole-file"),
        {
            "unmapped": Case(["inventory"], {"jellyfin": [UNMAPPED]}),
            "no-k3s": Case(["refuse"], binary=False),
        },
    )


@pytest.fixture(scope="module")
def coredns(tmp_path_factory) -> dict[str, Outcome]:
    owned = {"coredns": [COREDNS]}
    return _run(
        tmp_path_factory.mktemp("coredns"),
        {
            "owned": Case(["inventory"], owned),
            "no-flux": Case(["no-flux"]),
            "never-answers": Case(["refuse"], retries=3),
            "refusals-then-clean": Case(
                ["refuse", "refuse", "inventory"], {"jellyfin": [UNMAPPED]}
            ),
            "refusals-then-owned": Case(["refuse", "refuse", "inventory"], owned),
        },
        args=("--tags", "coredns"),
    )


def test_an_inventory_holding_no_mapped_id_passes(whole_file):
    _passed(whole_file["unmapped"])
    assert whole_file["unmapped"].calls == 5, (
        "every instance reads the inventory under a whole-file run"
    )


def test_a_row_whose_task_name_is_a_template_names_it_as_written(tmp_path):
    # The weekly-shard rows keep `{{ shard }}` literal with `{% raw %}`; a run that rendered
    # it again would fail on an undefined `shard` instead of naming the task.
    outcome = _run_one(
        tmp_path,
        Case(["inventory"], {"longhorn-reconcile": [RECONCILE]}),
        args=("--tags", "longhorn"),
    )
    _failed_naming(
        outcome, "Move volumes whose weekday changed out of shard d{{ shard }}"
    )


def _without(*tasks: str) -> list:
    """The real guard with the rows for `tasks` deleted.

    An instance left with no row keeps a stand-in row naming an id no inventory here holds, so
    a selection still runs its read and its match. Without it the instance would vanish, and
    a pass would prove only that nothing ran.
    """
    guard = []
    for instance in yaml_fast.safe_load(GUARD.read_text()):
        rows = [
            r
            for r in instance["vars"]["k3s_stale_checkout_rows"]
            if r["task"] not in tasks
        ] or [{"task": "A writer this checkout still holds", "ids": [UNMAPPED]}]
        guard.append({**instance, "vars": {"k3s_stale_checkout_rows": rows}})
    return guard


def test_a_checkout_whose_coredns_writers_are_gone_passes_while_flux_owns_coredns(
    tmp_path,
):
    guard = _without(
        "Point cluster DNS at Pi-hole first", "Run the CoreDNS image k3s bundles"
    )
    outcome = _run_one(
        tmp_path,
        Case(["inventory"], {"coredns": [COREDNS]}),
        args=("--tags", "coredns"),
        guard=guard,
    )
    _passed(outcome)
    assert outcome.calls == 1, (
        "the coredns instance read the inventory and matched nothing"
    )


def test_an_api_without_flux_passes_on_the_first_try(coredns):
    _passed(coredns["no-flux"])
    assert coredns["no-flux"].calls == 1


def test_a_host_without_k3s_passes_without_reading(whole_file):
    _passed(whole_file["no-k3s"])
    assert whole_file["no-k3s"].calls == 0


def test_an_api_that_never_answers_fails_after_the_bounded_retry(coredns):
    outcome = coredns["never-answers"]
    _failed_naming(outcome, "the k3s API is unreachable", "was refused")
    assert outcome.calls == 4, "`retries: 3` is the first try plus three retries"


def test_two_refusals_then_a_clean_inventory_pass(coredns):
    _passed(coredns["refusals-then-clean"])
    assert coredns["refusals-then-clean"].calls == 3


def test_two_refusals_then_a_mapped_id_fail_and_name_the_task(coredns):
    _failed_naming(coredns["refusals-then-owned"], "Run the CoreDNS image k3s bundles")


def test_a_check_run_still_reads_and_fails(tmp_path):
    outcome = _run_one(
        tmp_path,
        Case(["inventory"], {"coredns": [COREDNS]}),
        args=("--tags", "coredns", "--check"),
    )
    _failed_naming(outcome, "Run the CoreDNS image k3s bundles")
    assert outcome.calls == 1


# ── Selection: ansible-core's own --tags and --skip-tags decide which instances run ──


@pytest.mark.parametrize("tag", ["node-address", "kubeconfig"])
def test_a_host_only_tag_reaches_no_instance_with_the_api_down(tmp_path, tag):
    # A guard that regains `always`, or a union tag set naming node-address, reads here.
    # `kubeconfig` joined the host-only tags in H1.2, when the identity went to Flux and only
    # the kubeconfig's host half stayed.
    outcome = _run_one(tmp_path, Case(["refuse"]), args=("--tags", tag))
    _passed(outcome)
    assert outcome.calls == 0


def test_tags_coredns_runs_the_coredns_instance(coredns):
    _failed_naming(coredns["owned"], "Run the CoreDNS image k3s bundles", COREDNS)
    assert coredns["owned"].calls == 1, "only the coredns instance is selected"


def test_skip_tags_kubeconfig_still_runs_the_coredns_instance(tmp_path):
    # fact: ansible/roles/setup/k3s/CLAUDE.md#Notable
    # Bring-up step 1. One guard tagged with the union of the writers' tags would be skipped
    # here while the coredns writers still ran. H1.3 moved the check here from the metallb
    # instance, which went with its writers.
    outcome = _run_one(
        tmp_path,
        Case(["inventory"], {"coredns": [COREDNS]}),
        args=("--skip-tags", "kubeconfig"),
    )
    _failed_naming(outcome, "Run the CoreDNS image k3s bundles", COREDNS)
