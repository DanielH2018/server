"""The include renderer, the two-pass inputs hash and the Compose env output (H5.2a1).

Each case is one of H5's *Verification* cases for H5.2a1. A render reads a scratch `hosts/` tree
that holds the real pi-compose template and tmpfiles file, with fixture services (`_compose_tree.py`).

Run: uv run pytest hosts/lib/tests/test_pi_compose_renderer.py
"""

import hashlib
import json

import pytest
from _compose_tree import (
    ALLOY,
    COMPOSE,
    CONFIG_ALLOY,
    ComposeTree,
    dependents_problems,
    text,
)

from renderer.renderer_lib.cluster import RenderRefused
from renderer.renderer_lib.environment import RenderError

UNIT = "/usr/lib/systemd/system/homelab-compose.service"
ENV = f"{COMPOSE}/compose.env"
TEMPLATE = f"/usr/lib/homelab/secret-templates{ENV}"


@pytest.fixture
def tree(tmp_path):
    return ComposeTree(tmp_path)


def test_the_project_includes_each_service_with_its_data_directory_and_env_file(tree):
    package = tree.render()
    assert text(package, f"{COMPOSE}/compose.yaml") == (
        "name: homelab\n"
        "include:\n"
        f"  - path: {COMPOSE}/alloy/compose.yaml\n"
        "    project_directory: /home/ubuntu/server/containers/alloy\n"
        f"    env_file: {ENV}\n"
        f"  - path: {COMPOSE}/autoheal/compose.yaml\n"
        "    project_directory: /home/ubuntu/server/containers/autoheal\n"
        f"    env_file: {ENV}\n"
        f"  - path: {COMPOSE}/docker-proxy/compose.yaml\n"
        "    project_directory: /home/ubuntu/server/containers/docker-proxy\n"
        f"    env_file: {ENV}\n"
    )
    assert f"{COMPOSE}/alloy/config.alloy" in package.files


def test_a_project_with_no_service_includes_nothing_and_names_no_env_file(tmp_path):
    tree = ComposeTree(tmp_path, services={}, keys=())
    tree.write(outputs=[])
    package = tree.render()
    assert text(package, f"{COMPOSE}/compose.yaml") == "name: homelab\ninclude: []\n"
    assert ENV not in package.links


def test_a_service_template_reads_container_item_and_its_files_hash_into_the_label(
    tree,
):
    package = tree.render()
    config = text(package, f"{COMPOSE}/alloy/config.alloy")
    assert config.startswith("// alloy ships logs")
    digest = hashlib.sha256(config.encode()).hexdigest()
    assert f'homelab.config-sha256: "{digest}"' in text(
        package, f"{COMPOSE}/alloy/compose.yaml"
    )
    tree.edit("alloy", "config.alloy.j2", CONFIG_ALLOY + "// one more line\n")
    assert (
        tree.render().files[f"{COMPOSE}/alloy/compose.yaml"]
        != package.files[f"{COMPOSE}/alloy/compose.yaml"]
    )


def test_a_service_file_other_than_the_compose_file_cannot_read_a_secret(tree):
    tree.edit("alloy", "config.alloy.j2", 'password = "{{ alloy_pi_http_password }}"\n')
    with pytest.raises(RenderError, match="alloy_pi_http_password"):
        tree.render()


def test_a_compose_file_keeps_its_placeholders_and_the_env_template_is_single_quoted(
    tree,
):
    package = tree.render()
    assert "WEBHOOK_URL=${monitor_discord_webhook_url}" in text(
        package, f"{COMPOSE}/autoheal/compose.yaml"
    )
    assert (
        text(package, TEMPLATE)
        == "domain='${domain}'\nmonitor_discord_webhook_url='${monitor_discord_webhook_url}'\n"
    )
    assert package.links[ENV] == f"/run/homelab-secrets{ENV}"
    assert ENV not in package.files


def test_the_env_output_is_marked_dotenv_and_owned_by_root(tree):
    package = tree.render()
    manifest = json.loads(text(package, "/usr/lib/homelab/secrets.json"))
    (row,) = manifest["outputs"]
    assert (row["format"], row["mode"], row["owner"], row["keys"]) == (
        "dotenv",
        "0600",
        "root",
        ["domain", "monitor_discord_webhook_url"],
    )


def test_an_output_without_a_format_keeps_no_format_field(tmp_path):
    tree = ComposeTree(tmp_path, services={}, keys=["domain"])
    tree.write(outputs=[tree.output(path="/etc/pi-compose.conf", format=None)])
    (row,) = json.loads(text(tree.render(), "/usr/lib/homelab/secrets.json"))["outputs"]
    assert "format" not in row


def test_a_format_other_than_dotenv_is_flagged(tree):
    tree.write(outputs=[tree.output(format="ini")])
    with pytest.raises(RenderRefused, match="format 'ini'"):
        tree.render()


def test_an_output_beside_the_compose_files_must_be_dotenv(tree):
    tree.write(outputs=[tree.output(format=None)])
    with pytest.raises(RenderRefused, match="plain absolute path"):
        tree.render()


def test_the_dependents_census_accepts_the_real_row_and_flags_a_row_without_the_unit(
    tree,
):
    assert dependents_problems(tree.render()) == []
    tree.write(outputs=[tree.output(dependents=[])])
    assert dependents_problems(tree.render()) == [
        "compose.env does not name homelab-compose.service as a dependent"
    ]


def test_a_key_the_compose_files_read_and_the_output_omits_is_refused(tree):
    tree.keys = ["domain"]
    tree.write()
    with pytest.raises(
        RenderRefused, match="read and undeclared \\['monitor_discord_webhook_url'\\]"
    ):
        tree.render()


def test_a_declared_key_no_compose_file_reads_is_refused(tree):
    tree.keys = ["domain", "monitor_discord_webhook_url", "alloy_pi_http_password"]
    tree.write()
    with pytest.raises(
        RenderRefused, match="declared and unread \\['alloy_pi_http_password'\\]"
    ):
        tree.render()


@pytest.mark.parametrize("literal", ["$HOME", "$5", "$$$HOME"])
def test_a_bare_dollar_in_a_compose_file_is_flagged_as_a_second_substitution(
    tree, literal
):
    tree.edit("alloy", "compose.yaml.j2", ALLOY + f'    command: [echo, "{literal}"]\n')
    with pytest.raises(RenderRefused, match="Compose would substitute"):
        tree.render()


@pytest.mark.parametrize("literal", ["${HOME}", "${HOME:-/root}"])
def test_a_literal_brace_dollar_in_a_compose_file_is_escaped_and_clean(tree, literal):
    # C1 turns the `$` before `{` into `$$`, which Compose reads back as one literal `$`.
    tree.edit("alloy", "compose.yaml.j2", ALLOY + f'    command: [echo, "{literal}"]\n')
    assert '"$' + literal in text(tree.render(), f"{COMPOSE}/alloy/compose.yaml")


def test_the_unit_carries_the_inputs_hash_and_any_project_change_changes_it(tree):
    first = text(tree.render(), UNIT)
    assert text(tree.render(), UNIT) == first
    digest = first.split("INPUTS_SHA256=")[1].split()[0]
    assert len(digest) == 64
    tree.edit(
        "autoheal",
        "compose.yaml.j2",
        tree.services["autoheal"]["compose.yaml.j2"] + "    tty: true\n",
    )
    assert text(tree.render(), UNIT) != first


def test_a_change_to_the_tmpfiles_file_or_an_extra_changes_the_inputs_hash(tree):
    first = text(tree.render(), UNIT)
    path = tree.component / "tmpfiles.d" / "homelab-compose.conf"
    path.write_text(path.read_text() + "d /var/lib/x 0755 ubuntu ubuntu -\n")
    assert text(tree.render(), UNIT) != first


def test_a_unit_that_omits_the_inputs_line_does_not_follow_the_project(tree):
    # The red proof for the check above: a unit without the line is byte-identical across a change.
    (tree.component / "units" / "homelab-compose.service.j2").write_text(
        "[Service]\nExecStart=/bin/true\n"
    )
    first = text(tree.render(), UNIT)
    tree.edit(
        "autoheal",
        "compose.yaml.j2",
        tree.services["autoheal"]["compose.yaml.j2"] + "    tty: true\n",
    )
    assert text(tree.render(), UNIT) == first


def test_a_file_that_reads_the_hash_may_not_install_under_the_compose_directory(tree):
    spec_files = [
        {
            "src": "units/homelab-compose.service.j2",
            "dst": f"{COMPOSE}/unit",
            "inputs_sha256": True,
        }
    ]
    tree.write(files=spec_files)
    with pytest.raises(RenderRefused, match="installs under"):
        tree.render()


@pytest.mark.parametrize(
    ("change", "match"),
    [
        ({"defaults": {}}, "compose_services"),
        (
            {"defaults": {"compose_services": [{"name": "ghost"}]}},
            "has no services/ghost",
        ),
        (
            {"defaults": {"compose_services": [{"name": "alloy"}, {"name": "alloy"}]}},
            "twice",
        ),
        ({"defaults": {"compose_services": [{"name": "../x"}]}}, "plain `name`"),
    ],
)
def test_a_service_list_that_breaks_a_rule_is_refused(tree, change, match):
    tree.write(**change)
    with pytest.raises(RenderRefused, match=match):
        tree.render()
