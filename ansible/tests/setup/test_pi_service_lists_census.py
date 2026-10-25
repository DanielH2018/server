#!/usr/bin/env python3
"""The Pi's two service lists hold each service once, and every reader reads both (H5.1).

daniel-pi's host_vars list its services under `containers_list`, which Ansible's Docker play
deploys, and `compose_services`, which the `homelab-daniel-pi` package deploys. H3's two-writer
rule applies to the pair, and so does a reader rule: a service that moves from one key to the
other must change nothing a reader renders. This census fails on three conditions:

- a name appears in both keys;
- a `compose_services` entry other than `pi-canary` has no legacy entry for its Ansible-era
  compose file in the Pi package;
- one of the three cluster readers (monitor-bridge, observability, artifacts) or the recovery
  heartbeat's script renders differently after any one entry moves.

The cluster readers render twice. One render is `_k8s_render`'s, which builds `hostvars` with
`lib.render_context`. The other is C1's renderer (`renderer_lib/cluster.py:render_tag`) in
placeholder mode, which builds its own `hostvars` and is what publishes a flux-owned reader.
Each render takes the moved lists as its `hostvars` for the Pi, so neither needs a patched
module.

Run: uv run pytest ansible/tests/setup/test_pi_service_lists_census.py
"""

import copy
import dataclasses
import shutil

import pytest
import yaml
from _k8s_render import render_texts
from lib import yaml_fast
from lib.render_context import REPO_INVENTORY
from lib.repo_paths import HOST_VARS, K8S_ROLES, REPO
from renderer import homelab_render
from renderer.renderer_lib import cluster
from renderer.renderer_lib import host as host_mode
from renderer.renderer_lib.placeholders import placeholder_vars

HOST = "daniel-pi"
# The hosts a reader's `hostvars` names: the Pi's lists, and the cluster's own addresses.
HOSTS = (HOST, "daniel-box", "daniel-server")
READERS = ("monitor-bridge", "observability", "artifacts")
BRIDGE = frozenset({"monitor-bridge"})
# The one `compose_services` entry with no Ansible-era compose file (H5.2b's canary).
NO_LEGACY = "pi-canary"
RECOVERY_SCRIPT = "/usr/lib/homelab/pi-recovery-health"
# Named members the census must find, so a census that read neither key would fail.
MUST_FIND = {"docker-proxy", "alloy"}
PUBLISHED_PORTS = "PI_PUBLISHED_PORTS"


def host_doc(host: str) -> dict:
    return yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text())


def pi_lists(doc: dict) -> tuple[list[dict], list[dict]]:
    return doc["containers_list"] or [], doc["compose_services"] or []


def names_in_both(doc: dict) -> set[str]:
    """The names that appear in `containers_list` and in `compose_services`."""
    ansible, package = pi_lists(doc)
    return {e["name"] for e in ansible} & {e["name"] for e in package}


def without_legacy(doc: dict, legacy: list[dict]) -> set[str]:
    """The `compose_services` names whose Ansible-era compose file no legacy entry retires."""
    paths = [entry.get("path", "") for entry in legacy]
    return {
        entry["name"]
        for entry in pi_lists(doc)[1]
        if entry["name"] != NO_LEGACY
        and not any(
            p.endswith(f"/containers/{entry['name']}/docker-compose.yml") for p in paths
        )
    }


def moved(name: str) -> dict:
    """The Pi's host_vars with `name` moved from `containers_list` to `compose_services`."""
    doc = host_doc(HOST)
    entry = next(e for e in doc["containers_list"] if e["name"] == name)
    doc["containers_list"] = [e for e in doc["containers_list"] if e is not entry]
    doc["compose_services"] = [*doc["compose_services"], entry]
    return doc


def moved_inventory(tmp_path, pi: dict):
    """The repo's inventory with the Pi's host_vars replaced by `pi`."""
    host_vars = tmp_path / "host_vars"
    shutil.copytree(REPO_INVENTORY.host_vars, host_vars)
    (host_vars / f"{HOST}.yml").write_text(yaml.safe_dump(pi, sort_keys=False))
    return dataclasses.replace(REPO_INVENTORY, host_vars=host_vars)


def ansible_render(pi: dict, only=frozenset(READERS), overrides=None) -> dict:
    """Each reader's rendered template, keyed by (role, template), through `_k8s_render`."""
    hostvars = {host: host_doc(host) for host in HOSTS} | {HOST: pi}
    texts = render_texts({"hostvars": hostvars, **(overrides or {})}, only=only)
    return {(role, template): text for role, template, text in texts}


def c1_render(pi: dict, roles=K8S_ROLES, tags=READERS, extra=None) -> dict:
    """Each reader's C1 render in placeholder mode, keyed by (tag, directory, file)."""
    stubs = {**placeholder_vars(homelab_render.secret_keys()), **(extra or {})}
    rendered = {}
    for tag in tags:
        hostvars = copy.deepcopy(
            cluster.tag_context(roles / tag, tag, stubs)["hostvars"]
        )
        hostvars[HOST] |= {
            key: pi[key] for key in ("containers_list", "compose_services")
        }
        tag_render = cluster.render_tag(
            tag, {**stubs, "hostvars": hostvars}, roles=roles
        )
        for directory, files in tag_render.directories().items():
            rendered |= {(tag, directory, name): text for name, text in files.items()}
    return rendered


def recovery_script(tmp_path, inventory=REPO_INVENTORY) -> bytes:
    """The recovery heartbeat's rendered script, from a copy of `hosts/` without `pi-compose`.

    A moved entry has no service tree in the real component, so the whole package would refuse
    to render. The script reads only the two lists, and `pi-compose` contributes nothing to it.
    """
    root = tmp_path / "hosts"
    shutil.copytree(REPO / "hosts", root, dirs_exist_ok=True)
    # Shared files resolve against the repo root that holds `hosts/`.
    (tmp_path / "ansible").symlink_to(REPO / "ansible")
    package = root / HOST / "package.yml"
    spec = yaml_fast.safe_load(package.read_text())
    spec["components"] = [c for c in spec["components"] if c != "pi-compose"]
    package.write_text(yaml.safe_dump(spec))
    rendered = host_mode.render_host(HOST, root, inventory=inventory)
    return rendered.files[RECOVERY_SCRIPT][0]


def published_ports(texts) -> str:
    """monitor-bridge's `PI_PUBLISHED_PORTS` value, from the pod environment in its Deployment.

    The Deployment renders the variable as an `- name:` line and a `value:` line after it.
    """
    values = set()
    for text in texts:
        lines = [line.strip() for line in text.splitlines()]
        for at, line in enumerate(lines):
            if line == f"- name: {PUBLISHED_PORTS}":
                values.add(lines[at + 1])
    (value,) = values
    return value


def bridge_texts(rendered: dict) -> list[str]:
    return [text for key, text in rendered.items() if "monitor-bridge" in key]


def test_no_name_is_in_both_lists():
    assert names_in_both(host_doc(HOST)) == set()


def test_a_name_in_both_lists_is_flagged():
    doc = {
        "containers_list": [{"name": "autoheal"}, {"name": "alloy"}],
        "compose_services": [{"name": "autoheal"}],
    }
    assert names_in_both(doc) == {"autoheal"}


def test_every_compose_service_has_a_legacy_entry():
    legacy = host_mode.render_host(HOST).legacy
    assert without_legacy(host_doc(HOST), legacy) == set()


def test_a_compose_service_with_no_legacy_entry_is_flagged():
    legacy = [{"path": "/home/ubuntu/server/containers/alloy/docker-compose.yml"}]
    doc = {
        "containers_list": [],
        "compose_services": [
            {"name": "alloy"},
            {"name": "autoheal"},
            {"name": NO_LEGACY},
        ],
    }
    assert without_legacy(doc, legacy) == {"autoheal"}


def test_the_census_reads_both_keys_and_finds_the_named_members():
    doc = host_doc(HOST)
    assert set(doc) >= {"containers_list", "compose_services"}
    assert MUST_FIND <= {e["name"] for e in sum(pi_lists(doc), [])}
    # Each render produced monitor-bridge's value, so an empty render cannot pass the moves below.
    assert "alloy:12345" in published_ports(
        bridge_texts(ansible_render(doc, only=BRIDGE))
    )
    assert "alloy:12345" in published_ports(
        bridge_texts(c1_render(doc, tags=("monitor-bridge",)))
    )


@pytest.fixture(scope="module")
def unmoved(tmp_path_factory) -> dict:
    """The three renders of the inventory as it is, built once for every move below."""
    doc = host_doc(HOST)
    return {
        "ansible": ansible_render(doc),
        "c1": c1_render(doc),
        "recovery": recovery_script(tmp_path_factory.mktemp("unmoved")),
    }


@pytest.mark.parametrize("name", ["wg-easy", "autoheal", "alloy"])
def test_a_move_changes_no_render(name, unmoved, tmp_path):
    pi = moved(name)
    assert name in {e["name"] for e in pi["compose_services"]}
    assert ansible_render(pi) == unmoved["ansible"]
    assert c1_render(pi) == unmoved["c1"]
    after = recovery_script(tmp_path, moved_inventory(tmp_path, pi))
    assert after == unmoved["recovery"]


def test_a_reader_of_containers_list_alone_is_flagged(tmp_path):
    """REJECT: a monitor-bridge that reads `containers_list` alone renders differently after a move.

    The fixture is a copy of the role whose env template drops the second key. Both renders
    must see the move, or the equality above proves nothing about them.
    """
    # `ansible/roles/k8s/`: the plane in the path picks the host a role renders for, and the
    # Deployment reads the env template through `playbook_dir`.
    playbook = tmp_path / "ansible"
    roles = playbook / "roles" / "k8s"
    shutil.copytree(K8S_ROLES / "monitor-bridge", roles / "monitor-bridge")
    template = roles / "monitor-bridge" / "templates" / "config" / "env.yaml.j2"
    source = template.read_text()
    second = " + (hostvars['daniel-pi'].compose_services | published_ports)"
    assert second in source
    template.write_text(source.replace(second, ""))
    fixture = {"playbook_dir": str(playbook)}
    unmoved_pi, moved_pi = host_doc(HOST), moved("alloy")

    after = ansible_render(moved_pi, BRIDGE, fixture)
    assert after != ansible_render(unmoved_pi, BRIDGE, fixture)
    assert "alloy:12345" not in published_ports(bridge_texts(after))

    tags = ("monitor-bridge",)
    after = c1_render(moved_pi, roles, tags, fixture)
    assert after != c1_render(unmoved_pi, roles, tags, fixture)
    assert "alloy:12345" not in published_ports(bridge_texts(after))
