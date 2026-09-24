"""What the remediation prints once the deployer has narrowed a setup role's tag (#2307).

Split out of test_deploy_remediation.py, which is at its module-length cap. The narrowed
`--tags` replaces the whole-role tag; the whole-role warning goes with it, EXCEPT where the
narrowed tags still reach the role's gated control-plane tasks, which keep a warning of their
own.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_narrowed.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_narrowed.py

import pathlib

import yaml

import deploy_remediation

from deploy_remediation import broad_remediation, manual_plane_remediation

_K3S_TASKS = pathlib.Path(__file__).parents[2] / "k3s" / "tasks"


# ── #2307: a derived narrow tag replaces the role tag, and the warning with it ──────────────


def test_a_narrowed_role_prints_its_own_tags_and_drops_the_warning():
    """The narrowing's whole point, on both composers.

    The warning describes what `--tags k3s` does. Printed beside `--tags kubeconfig` it would
    warn about a run the operator is not being told to make, which is how a real warning gets
    read as boilerplate.
    """
    narrow = {"k3s": frozenset({"kubeconfig"})}
    for cmd in (
        broad_remediation(False, True, {"k3s"}, narrow_tags=narrow),
        manual_plane_remediation({"k3s"}, narrow),
    ):
        assert "ansible/k3s-bringup.yml --tags kubeconfig" in cmd
        assert "WARNING" not in cmd


def test_a_role_the_derivation_refused_keeps_the_role_tag_and_the_warning():
    """The rejecting half: an empty tag set is a refusal, not an empty `--tags` value.

    `--tags` with nothing after it runs the WHOLE playbook, so a refusal that leaked through
    as an empty string would prescribe every setup role on the host.
    """
    for narrow in ({}, {"k3s": frozenset()}):
        cmd = manual_plane_remediation({"k3s"}, narrow)
        assert "ansible/k3s-bringup.yml --tags k3s" in cmd
        assert "WARNING" in cmd


def test_several_narrow_tags_are_one_comma_joined_tags_value():
    """Two ranges can make one role pending, and both tags have to run."""
    narrow = {"k3s": frozenset({"kubeconfig", "coredns"})}
    assert "--tags coredns,kubeconfig" in manual_plane_remediation({"k3s"}, narrow)


# ── a narrowed tag that still reaches the gated tasks keeps the warning (#2324 review) ──


def test_a_narrowed_tag_reaching_the_gated_tasks_keeps_the_warning():
    """`--tags k3s_server` arms the restart and the re-encryption as surely as `--tags k3s`.

    Every task in `tasks/server.yml` carries `k3s_server`, so a range touching that file
    narrows to it. Dropping the warning there would print the dangerous command bare.
    """
    for narrow in (
        {"k3s": frozenset({"k3s_server"})},
        {"k3s": frozenset({"k3s_server", "kubeconfig"})},
    ):
        cmd = manual_plane_remediation({"k3s"}, narrow)
        assert "--tags " in cmd and "k3s_server" in cmd.split("WARNING")[0]
        assert "WARNING" in cmd
        assert "rotate-keys" in cmd, "the warning still names the re-encryption"
        assert "WHOLE role" not in cmd, "the narrowed command is not the whole role"


def test_the_gated_tags_are_every_tag_the_gated_tasks_carry():
    """Derived from `tasks/server.yml`, so a gated task retagged cannot slip the warning.

    The deployer's venv cannot import yaml, so the set is a constant there; this is where it
    is checked against the role. A gated task is one reading a control-plane gate, the
    rotate-keys command, or the log drop-in restart.
    """

    tasks = yaml.safe_load((_K3S_TASKS / "server.yml").read_text())
    markers = (
        *deploy_remediation._K3S_CONTROL_PLANE_GATES,
        "rotate-keys",
        "Restart k3s",
    )
    gated = [t for t in tasks if any(m in yaml.safe_dump(t) for m in markers)]
    assert len(gated) >= 3, f"only {len(gated)} gated tasks found in tasks/server.yml"
    carried = set().union(*(set(t.get("tags") or []) for t in gated))
    assert carried, "the gated tasks carry no tags at all"
    assert carried <= deploy_remediation._MAXIMAL_ROLE_GATED_TAGS["k3s"], (
        f"gated tasks carry tags the warning does not watch: {sorted(carried)}"
    )
