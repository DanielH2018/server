"""The k3s remediation names the agent join beside `--tags k3s`, which never reaches an agent.

`k3s-bringup.yml` applies `tasks/agent.yml` only from the join plays, which match no host
without `-e join_agent=<host>`. #5551 changed `templates/registries.yaml.j2`, which both
`server.yml` and `agent.yml` render, and the printed `--tags k3s` left daniel-server's
`registries.yaml` without the `ghcr.io` mirror while the `manual_plane` line cleared (#5606).

The rows here come from the real derivation, `RoleIndex` read with `k3s-bringup.yml`, which
resolves the join play's `include_role ... tasks_from: agent` to `k3s_agent` (#5628).
"""

from deploy_tools.narrow_lib import setup_index as narrow_setup_index
from deployer_lib import deploy_setup_roles
from deployer_lib.deploy_changes import services_from_changed_paths
from deployer_lib.deploy_remediation import broad_remediation, manual_plane_remediation
from lib import yaml_fast
from deployer_lib.deploy_setup_roles import setup_role_playbook
from gitops_markers import AGENT_JOIN_TAGS
from lib.repo_paths import ALL_VARS, REPO

K3S = "ansible/roles/setup/k3s"


def _index() -> narrow_setup_index.RoleIndex:
    """The k3s role at HEAD, read with the playbook its remediation prints."""
    playbook = setup_role_playbook("k3s")
    assert playbook == "ansible/k3s-bringup.yml", playbook
    text = (REPO / playbook).read_text()
    return narrow_setup_index.RoleIndex("k3s", "HEAD", str(REPO), text)


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
    """`registries.yaml.j2`: `--tags k3s_server`, one join per agent host, then the clear.

    `readers_of` is what `narrow_setup.path_tags` asks of a changed template. The server
    command drops `k3s_agent`, which selects nothing on the server play, and the clear keeps
    the whole row. `broad_remediation` is what `deploy.sh` and `land.sh` print from the change
    set itself, with no row, so it keeps the whole-role tag and the join.
    """
    cs = services_from_changed_paths([f"{K3S}/templates/registries.yaml.j2"])
    assert cs.setup_roles == {"k3s"}
    row = _index().readers_of("registries.yaml.j2")
    assert row == {"k3s_server", "k3s_agent"}
    joins = [f"-e join_agent={host} --tags k3s_agent`" for host in _agent_hosts()]
    ledger = manual_plane_remediation(cs.setup_roles, {"k3s": row})
    _ordered(
        ledger,
        "k3s-bringup.yml --tags k3s_server`",
        *joins,
        "clear-owed manual_plane k3s --applied k3s_agent,k3s_server`",
    )
    broad = broad_remediation(
        cs.broad_deploy, cs.broad_setup, cs.setup_roles, playbooks=cs.manual_playbooks
    )
    _ordered(broad, "git merge --ff-only", "k3s-bringup.yml --tags k3s`", *joins)


def test_a_server_only_change_set_prints_no_agent_command_once_narrowed():
    """The change set `tasks/server.yml` alone, with its row from the real derivation.

    The ledger surfaces print from the row the narrowing recorded, and `tags_of` is what
    `narrow_setup.path_tags` asks of a changed `tasks/` file, so the row is derived here
    rather than written in. `broad_remediation` over the same change set has no row, and
    prints the whole role with the join beside it: the over-apply the `DECIDED:` marker at
    `gitops_markers.AGENT_JOIN_TAGS` accepts for `deploy.sh --changed` and `land.sh`'s fallback.
    """
    path = f"{K3S}/tasks/server.yml"
    cs = services_from_changed_paths([path])
    assert cs.setup_roles == {"k3s"}
    row = {"k3s": _index().tags_of(path.removeprefix(f"{K3S}/"))}
    narrowed = manual_plane_remediation(cs.setup_roles, row)
    assert "--tags k3s_server" in narrowed
    assert "join_agent" not in narrowed
    whole = broad_remediation(
        cs.broad_deploy, cs.broad_setup, cs.setup_roles, playbooks=cs.manual_playbooks
    )
    assert "-e join_agent=" in whole


def test_a_routing_with_no_agent_hosts_prints_a_placeholder_not_a_host():
    """An older `setup_routing.py` emits no `agent_hosts`, and a guessed host is worse."""
    route = deploy_setup_roles.SetupRoute("ansible/k3s-bringup.yml", "k3s", False, None)
    deploy_setup_roles.use_routing({"k3s": route})
    cmd = manual_plane_remediation({"k3s"}, {})
    assert "-e join_agent=<agent host> --tags k3s_agent" in cmd
    assert "daniel-server" not in cmd


def test_an_agent_only_change_narrows_to_the_join_alone():
    """The issue's Verify-by: `tasks/agent.yml` alone prints the join and no server command.

    The server play under `--tags k3s_agent` selects nothing and exits 0, and under `--tags
    k3s` arms the two gated control-plane tasks for work the server does not need.
    """
    path = f"{K3S}/tasks/agent.yml"
    cs = services_from_changed_paths([path])
    row = _index().tags_of(path.removeprefix(f"{K3S}/"))
    assert row == {"k3s_agent"}
    text = manual_plane_remediation(cs.setup_roles, {"k3s": row})
    for host in _agent_hosts():
        assert f"k3s-bringup.yml -e join_agent={host} --tags k3s_agent`" in text, text
    assert "k3s-bringup.yml --tags" not in text, text
    assert "WARNING" not in text, text


def test_every_tag_a_play_include_alone_reaches_is_an_agent_join_tag():
    """The split assumes a play-included file's tag is one `agent_join_cmds` prints.

    A tag from any other opt-in play would land on the server command with no way to select
    that play's hosts, and Ansible exits 0 having applied nothing. Every playbook naming a
    `tasks_from:` is read; `agent.yml` is the member the census must find.
    """
    seen = set()
    for playbook in sorted((REPO / "ansible").glob("*.yml")):
        text = playbook.read_text()
        if "tasks_from" not in text:
            continue
        for role in narrow_setup_index.playbook_roles(text):
            index = narrow_setup_index.RoleIndex(role, "HEAD", str(REPO), text)
            for rel in sorted(set(index.play_included) - index.reachable):
                tags = index.tags_of(rel)
                assert tags <= {AGENT_JOIN_TAGS.get(role)}, (playbook.name, rel, tags)
                seen.add((role, rel))
    assert ("k3s", "tasks/agent.yml") in seen, seen
