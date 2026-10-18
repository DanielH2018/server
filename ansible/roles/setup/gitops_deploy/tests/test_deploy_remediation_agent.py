"""The k3s remediation names the agent join beside `--tags k3s`, which never reaches an agent.

`k3s-bringup.yml` applies `tasks/agent.yml` only from the join plays, which match no host
without `-e join_agent=<host>`. #5551 changed `templates/registries.yaml.j2`, which both
`server.yml` and `agent.yml` render, and the printed `--tags k3s` left daniel-server's
`registries.yaml` without the `ghcr.io` mirror while the `manual_plane` line cleared (#5606).
"""

import pytest

from deploy_tools.narrow_lib import setup_index as narrow_setup_index
from deployer_lib import deploy_setup_roles
from deployer_lib.deploy_changes import services_from_changed_paths
from deployer_lib.deploy_remediation import broad_remediation, manual_plane_remediation
from lib import yaml_fast
from lib.narrow_git import CannotNarrow
from lib.repo_paths import ALL_VARS, REPO

K3S = "ansible/roles/setup/k3s"


def _agent_hosts() -> list[str]:
    hosts = yaml_fast.safe_load(ALL_VARS.read_text())["k3s_agent_hosts"]
    assert hosts, "k3s_agent_hosts lists no host, so no join command could be checked"
    return hosts


def _ordered(text: str, *parts: str) -> None:
    at = [text.find(p) for p in parts]
    assert -1 not in at, (
        f"missing {[p for p, i in zip(parts, at, strict=True) if i < 0]} in {text}"
    )
    assert at == sorted(at), f"{parts} out of order in {text}"


def test_a_template_both_halves_render_prints_server_then_agent_then_clear():
    """The Verify-by case: server apply, one join per agent host, then the clear.

    The range's row is empty because the narrowing refuses `agent.yml` (pinned below), so
    the ledger surfaces print the whole-role command. `broad_remediation` is what `deploy.sh`
    and `land.sh` print from the change set itself.
    """
    cs = services_from_changed_paths([f"{K3S}/templates/registries.yaml.j2"])
    assert cs.setup_roles == {"k3s"}
    joins = [f"-e join_agent={host} --tags k3s_agent`" for host in _agent_hosts()]
    ledger = manual_plane_remediation(cs.setup_roles, {"k3s": frozenset()})
    _ordered(
        ledger, "k3s-bringup.yml --tags k3s`", *joins, "clear-owed manual_plane k3s`"
    )
    broad = broad_remediation(
        cs.broad_deploy, cs.broad_setup, cs.setup_roles, playbooks=cs.manual_playbooks
    )
    _ordered(broad, "git merge --ff-only", "k3s-bringup.yml --tags k3s`", *joins)


def test_a_server_only_change_prints_no_agent_command():
    """`tasks/server.yml` narrows to `k3s_server`, which the agent never reads."""
    cmd = manual_plane_remediation({"k3s"}, {"k3s": frozenset({"k3s_server"})})
    assert "--tags k3s_server" in cmd
    assert "join_agent" not in cmd


def test_a_routing_with_no_agent_hosts_prints_a_placeholder_not_a_host():
    """An older `setup_routing.py` emits no `agent_hosts`, and a guessed host is worse."""
    route = deploy_setup_roles.SetupRoute("ansible/k3s-bringup.yml", "k3s", False, None)
    deploy_setup_roles.use_routing({"k3s": route})
    cmd = manual_plane_remediation({"k3s"}, {})
    assert "-e join_agent=<agent host> --tags k3s_agent" in cmd
    assert "daniel-server" not in cmd


def test_a_change_reaching_agent_yml_refuses_to_narrow():
    """The premise of the remediation's `DECIDED:` marker: agent reach always reads as whole.

    The agent command is printed only beside an empty row. If the narrowing learned to
    resolve the join play's `include_role`, a row could hold `k3s_agent` and the printed
    server command would carry it with no `join_agent`, which applies nothing to the agent.
    `tags_of` is what `narrow_setup.path_tags` asks of a changed `tasks/` file, and
    `readers_of` what it asks of a changed template.
    """
    index = narrow_setup_index.RoleIndex("k3s", "HEAD", str(REPO))
    with pytest.raises(CannotNarrow, match=r"tasks/agent\.yml"):
        index.tags_of("tasks/agent.yml")
    with pytest.raises(CannotNarrow, match=r"tasks/agent\.yml"):
        index.readers_of("registries.yaml.j2")
    assert index.tags_of("tasks/server.yml") == {"k3s_server"}
