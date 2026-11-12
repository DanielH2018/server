"""What the remediation prints once the deployer has narrowed a setup role's tag.

Split out of test_deploy_remediation.py, which is at its module-length cap. The narrowed
`--tags` replaces the whole-role tag.

Run: uv run pytest ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_narrowed.py
"""

# ansible/roles/setup/gitops_deploy/tests/test_deploy_remediation_narrowed.py

from deployer_lib.deploy_remediation import broad_remediation, manual_plane_remediation


# ── a derived narrow tag replaces the role tag ──────────────────────────────────────


def test_a_narrowed_role_prints_its_own_tags():
    """The narrowing's whole point, on both composers."""
    narrow = {"k3s": frozenset({"kubeconfig"})}
    for cmd in (
        broad_remediation(False, True, {"k3s"}, narrow_tags=narrow),
        manual_plane_remediation({"k3s"}, narrow),
    ):
        assert "ansible/k3s-bringup.yml --tags kubeconfig" in cmd


def test_a_role_the_derivation_refused_keeps_the_role_tag():
    """The rejecting half: an empty tag set is a refusal, not an empty `--tags` value.

    `--tags` with nothing after it runs the WHOLE playbook, so a refusal that leaked through
    as an empty string would prescribe every setup role on the host.
    """
    for narrow in ({}, {"k3s": frozenset()}):
        cmd = manual_plane_remediation({"k3s"}, narrow)
        assert "ansible/k3s-bringup.yml --tags k3s" in cmd


def test_several_narrow_tags_are_one_comma_joined_tags_value():
    """Two ranges can make one role pending, and both tags have to run."""
    narrow = {"k3s": frozenset({"kubeconfig", "coredns"})}
    assert "--tags coredns,kubeconfig" in manual_plane_remediation({"k3s"}, narrow)


# ── the clear command names the tags the apply beside it ran ─────────────────────


def test_a_narrowed_apply_prints_a_clear_that_names_what_it_applied():
    """A bare clear drops the whole line, and the row can grow between print and run.

    A second PR touching the same role widens the row to `coredns,kubeconfig` before the
    operator clears. The bare command would take `coredns` with it, leaving that change
    merged, unapplied and recorded nowhere.
    """
    cmd = manual_plane_remediation({"k3s"}, {"k3s": frozenset({"kubeconfig"})})
    assert "clear-owed manual_plane k3s --applied kubeconfig" in cmd


def test_a_whole_role_apply_prints_the_bare_clear():
    """The rejecting half: a whole-role apply covers whatever the row gained, so it takes it.

    `--applied` there would be wrong in the other direction — it would leave a line pending
    for a tag the operator just ran.
    """
    cmd = manual_plane_remediation({"k3s"}, {})
    assert "clear-owed manual_plane k3s`" in cmd
    assert "--applied" not in cmd


def test_two_roles_print_one_clear_each_and_only_the_narrowed_one_says_applied():
    """`common`'s row is always empty, so `--applied` there would keep its line for good.

    A shared `<role> --applied <tags>` placeholder would send the operator to do exactly that,
    and its `<` would read as a shell redirect when pasted.
    """
    cmd = manual_plane_remediation(
        {"k3s", "common"}, {"k3s": frozenset({"kubeconfig"})}
    )
    assert "clear-owed manual_plane common && " in cmd
    assert "clear-owed manual_plane k3s --applied kubeconfig`" in cmd
    assert "<" not in cmd.rsplit(", then ", 1)[-1], "the clear names no placeholder"
