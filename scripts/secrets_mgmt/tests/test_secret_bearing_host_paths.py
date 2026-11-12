"""Tests for the derivation in scripts/secrets_mgmt/secret_bearing_host_paths.py.

`.claude/hooks/block-protected-bash.py` denies a content-printing read of every path this
module returns. A derivation that quietly narrows therefore unblocks a read that prints a
credential into the terminal and the transcript, and nothing else in the tree notices.

The real-tree tests name members the census must find, so an empty walk fails here. The
synthetic cases give each rule one input it must flag and one it must not.

Nothing in this file prints the content of a secret-bearing file. The assertions are over
paths and secret NAMES, which is exactly what the module returns and what the hook's deny
message already says out loud.

Run: uv run pytest scripts/secrets_mgmt/tests/test_secret_bearing_host_paths.py
"""

import ast

import pytest

from lib.repo_paths import REPO
from secrets_mgmt.secret_bearing_host_paths import (
    SECRET_PATH_ROOTS,
    non_secret_names,
    secret_bearing_host_paths,
)

# The hook's own copy of the root list. It short-circuits on this before importing anything,
# so a root added here and not there makes the guard stop firing for the new tree.
HOOK = REPO / ".claude" / "hooks" / "block-protected-bash.py"


@pytest.fixture(scope="module")
def real_census():
    return secret_bearing_host_paths()


# ── the real tree ────────────────────────────────────────────────────────────────────────
def test_the_census_finds_a_declared_output_and_a_credential(real_census):
    """Named members: k3s-host-checks' etcd S3 env file and the packages' ping-key credential."""
    assert "r2_secret_access_key" in real_census["/etc/rancher/k3s/etcd-s3.env"]
    assert real_census["/run/credstore/healthchecks_ping_key"] == [
        "healthchecks_ping_key"
    ]


def test_every_returned_path_is_under_a_secret_root(real_census):
    stray = [p for p in real_census if not p.startswith(SECRET_PATH_ROOTS)]
    assert not stray, f"{stray} would be invisible to the hook's cheap gate"


def test_the_hooks_gate_matches_the_modules():
    """Read the hook's literal rather than importing it — it reads stdin at import time."""
    tree = ast.parse(HOOK.read_text(), filename=str(HOOK))
    literals = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "_SECRET_PATH_ROOTS"
            for t in node.targets
        )
    ]
    assert literals, f"_SECRET_PATH_ROOTS not found in {HOOK}"
    assert ast.literal_eval(literals[0]) == SECRET_PATH_ROOTS


def test_the_ignore_tier_names_an_address_and_not_a_password():
    """`email` and `r2_bucket` are tracked but hold no credential; their passwords do."""
    ignored = non_secret_names()
    assert {"email", "r2_bucket"} <= ignored
    assert not {"smtp_notify_app_password", "r2_secret_access_key"} & ignored


# ── the synthetic tree: one input it must flag, one it must not ──────────────────────────
def _tree(tmp_path, spec_text, *, spec="hosts/components/demo/component.yml"):
    path = tmp_path / spec
    path.parent.mkdir(parents=True)
    path.write_text(spec_text)
    (tmp_path / "secret_rotation.yml").write_text(
        "entries:\n"
        "  demo_push_token:\n"
        "    tier: auto\n"
        "  demo_bucket:\n"
        "    tier: ignore\n"
    )
    return tmp_path


def _census(tree):
    return secret_bearing_host_paths(tree, tree / "secret_rotation.yml")


def _output(keys, **extra):
    fields = ", ".join(f"{k}: {v}" for k, v in extra.items())
    return (
        "outputs:\n  - {src: templates/demo.conf.j2, path: /etc/demo/demo.conf,"
        f' owner: root, group: root, mode: "0600", keys: [{keys}]'
        + (f", {fields}" if fields else "")
        + "}\n"
    )


def test_an_output_is_flagged_at_its_path_and_its_rendered_file(tmp_path):
    tree = _tree(tmp_path, _output("demo_push_token"))
    assert _census(tree) == {
        "/etc/demo/demo.conf": ["demo_push_token"],
        "/run/homelab-secrets/etc/demo/demo.conf": ["demo_push_token"],
    }


def test_a_persisted_output_renders_under_var_lib(tmp_path):
    tree = _tree(
        tmp_path, _output("demo_push_token", persist="true", destination="demo/x")
    )
    assert "/var/lib/homelab-secrets/demo/x" in _census(tree)


def test_an_output_holding_only_an_ignore_tier_name_is_clean(tmp_path):
    tree = _tree(tmp_path, _output("demo_bucket"))
    assert _census(tree) == {}


def test_a_package_credential_is_flagged_in_the_credstore(tmp_path):
    tree = _tree(
        tmp_path,
        "credentials:\n  - demo_push_token\n  - demo_bucket\n",
        spec="hosts/demo-host/package.yml",
    )
    assert _census(tree) == {"/run/credstore/demo_push_token": ["demo_push_token"]}


def test_a_component_with_no_output_or_credential_is_clean(tmp_path):
    tree = _tree(
        tmp_path,
        "files:\n  - {src: templates/x.sh.j2, dst: /usr/lib/homelab/x, mode: '0755'}\n",
    )
    assert _census(tree) == {}
