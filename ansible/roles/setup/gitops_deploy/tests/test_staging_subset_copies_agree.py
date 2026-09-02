"""The staging subset exists in three places, and only one of them gates production.

`STAGING_SUBSET` in gitops_deploy.py is the copy `consult_staging` splits a deploy against, so
it decides what staging can speak for. `STAGING_SERVICES` in staging_gate.py is a default that
production never reaches — `consult_staging` always passes explicit `--tags` — so it governs
only a hand-run. `containers_list` in daniel-stage.yml is what the cluster actually runs, and is
the only one of the three that is true by construction rather than by assertion.

Until this test existed the governing copy had no test at all: adding a service to staging and
to staging_gate.py while forgetting gitops_deploy.py would leave the new service silently
ungated, reported as "unchecked" on every tick, with every check in the repo green. That is the
shape docs/staging-cluster.md calls Decision 6, and the three lists are tied together by that
decision rather than by anything a machine was checking.

Equality, not containment. All three are the same six services today, and a subset relation
would accept exactly the drift above.
"""

from __future__ import annotations

import ast
import pathlib

import gitops_deploy
import yaml

_FILES = pathlib.Path(__file__).resolve().parents[1] / "files"
_REPO = _FILES.parents[4]
_INVENTORY = _REPO / "ansible" / "inventory" / "host_vars" / "daniel-stage.yml"
_GATE_SCRIPT = _REPO / "scripts" / "deploy_tools" / "staging_gate.py"


def inventory_subset() -> set[str]:
    """What daniel-stage actually runs, read from the inventory rather than restated here.

    Restating the six names in this file would add a FOURTH copy while claiming to pin three.
    """
    entries = yaml.safe_load(_INVENTORY.read_text())["containers_list"]
    return {entry["name"] for entry in entries}


def literal_assignment(source: str, name: str) -> set[str]:
    """The value of a module-level `name = <literal>` assignment, without importing the module.

    An AST read rather than an import because the shared verdict below has to run against a
    synthetic source in the rejecting half, where there is no module to import.
    """
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return set(ast.literal_eval(node.value))
    raise AssertionError(f"{name} is gone")


def disagreeing_copies(copies: dict[str, set[str]]) -> dict[str, set[str]]:
    """The copies that differ from the inventory. The shared verdict both halves below run."""
    truth = copies["daniel-stage.yml containers_list"]
    return {where: names ^ truth for where, names in copies.items() if names != truth}


def test_every_copy_of_the_staging_subset_agrees() -> None:
    copies = {
        "daniel-stage.yml containers_list": inventory_subset(),
        "gitops_deploy.STAGING_SUBSET": set(gitops_deploy.STAGING_SUBSET),
        "staging_gate.STAGING_SERVICES": literal_assignment(
            _GATE_SCRIPT.read_text(), "STAGING_SERVICES"
        ),
    }
    drifted = disagreeing_copies(copies)
    assert not drifted, (
        f"the staging subset disagrees across its copies: {drifted}. gitops_deploy's "
        f"STAGING_SUBSET is the one that gates production — a service missing there is "
        f"reported as unchecked on every tick while every other check reads green."
    )


def test_the_agreement_check_rejects_a_drifted_copy() -> None:
    """The rejecting half.

    Without it a check that stopped reading one of the copies — an AST walk that matches nothing, a
    renamed key — would pass by finding no disagreement.
    """
    truth = inventory_subset()
    drifted = disagreeing_copies(
        {
            "daniel-stage.yml containers_list": truth,
            "gitops_deploy.STAGING_SUBSET": truth - {"registry"},
            "staging_gate.STAGING_SERVICES": truth | {"sonarr"},
        }
    )
    assert drifted.keys() == {
        "gitops_deploy.STAGING_SUBSET",
        "staging_gate.STAGING_SERVICES",
    }, f"the check no longer sees drift it is the only guard against: {drifted}"
    assert drifted["gitops_deploy.STAGING_SUBSET"] == {"registry"}
    assert drifted["staging_gate.STAGING_SERVICES"] == {"sonarr"}


def test_the_inventory_read_finds_the_real_list() -> None:
    """The premise the two tests above rest on.

    An inventory read that silently returned an empty set would make both of them vacuous, since
    every copy would then 'disagree' or the comparison would collapse.
    """
    names = inventory_subset()
    assert len(names) >= 6, f"daniel-stage's containers_list read as {names}"
    assert "freshrss" in names
