"""The setup plane keeps working once the deploy-plane narrowing modules are gone (#7454).

The entry points run in a subprocess with the two modules marked unimportable in `sys.modules`,
which is what deleting them does to anything still reaching them. The two module names stay
spelled apart so this file stays out of the issue's `git grep` over `scripts`.

Run: uv run pytest scripts/deploy_tools/tests/test_setup_readers_outlive_deploy_narrowing.py
"""

import ast
import json
import re
import subprocess
import sys
from pathlib import Path, PurePosixPath

import pytest
from _row_table import tracked
from deployer_lib.deploy_setup_roles import SECRET_READERS_SCRIPT
from lib.git import git
from lib.git_testing import git_out, scrubbed_env
from lib.proc_testing import run
from lib.repo_paths import REPO

from _narrow_fixtures import Tree, build_tree

BROAD = "broad"
CONTAINERS = "containers"
NARROW_LIB = "scripts/deploy_tools/narrow_lib"
GONE_MODULES = [f"deploy_tools.narrow_lib.{name}" for name in (BROAD, CONTAINERS)]
REACH = "scripts/deploy_tools/reach.py"
PLAN = "docs/target-architecture/deploy/d6-delete-old-path.md"
LAND = "scripts/deploy_tools/land.py"

# A moved symbol may lose its leading underscore, because it becomes another module's API.
MOVED_SYMBOLS = (
    "Context",
    "grep",
    "sort_hits",
    "template_importers",
    "role_tags",
    "context_for",
)
DEPLOY_PLANE_RULES = frozenset(
    {
        "narrow_cmd",
        "broad_path_tags",
        "_changed_half",
        "_containers_list_tags",
        "_list_reader_tags",
        "_filter_tags",
    }
)
K8S_READINGS = frozenset({"k8s_lookup_readers", "k8s_roles_holding"})

SECRETS = "ansible/vars/secrets.yml"
SETUP_READER = "ansible/roles/setup/claude_code/templates/env.j2"
BASE_SECRETS = """\
jellyfin_api_key: ENC[AES256_GCM,data:aaa]
setup_only_token: ENC[AES256_GCM,data:ccc]
sops:
    mac: mac1
"""
ROTATED_SETUP_TOKEN = BASE_SECRETS.replace("ccc", "ddd").replace("mac1", "mac2")
ROTATED_K8S_KEY = BASE_SECRETS.replace("aaa", "bbb").replace("mac1", "mac2")

# Reader tests that name neither gone module in their bodies, so they survive the move as written.
READER_TESTS = (
    "test_narrow_secret_keys.py::test_a_new_key_a_setup_role_reads_names_that_roles_file",
    "test_narrow_secret_keys.py::test_a_key_only_a_k8s_role_reads_names_no_setup_file",
    "test_narrow_secret_keys.py::test_a_key_the_play_reads_refuses_the_setup_answer",
    "test_narrow_secret_keys.py::test_deploy_tags_names_the_k8s_reader_of_a_rotated_key",
    "test_narrow_secret_keys.py::test_deploy_tags_refuses_most_of_the_fleet_naming_it",
    "test_narrow_secret_keys.py::test_a_shipper_reading_none_of_the_secrets_keys_is_unread",
    "test_narrow_inventory_keys.py::test_a_moved_key_a_setup_template_reads_names_that_template",
    "test_narrow_inventory_keys.py::test_key_readers_merges_the_secrets_half_beside_the_inventory_half",
)


def _run_without_gone_modules(
    code: str, *argv: str
) -> subprocess.CompletedProcess[str]:
    """Run `code` in a fresh interpreter where importing either gone module raises."""
    blocked = (
        f"import sys\nfor name in {GONE_MODULES!r}:\n    sys.modules[name] = None\n"
    )
    return run(
        [sys.executable, "-c", blocked + code, *argv],
        cwd=REPO,
        env=scrubbed_env(),
    )


def _run_script_without_gone_modules(
    script: str, *argv: str
) -> subprocess.CompletedProcess[str]:
    """Run `script` as `python script argv` runs it, with its own directory on `sys.path`."""
    path = str(REPO / script)
    code = (
        "import runpy\n"
        "sys.argv = sys.argv[1:]\n"
        "sys.path.insert(0, str(__import__('pathlib').Path(sys.argv[0]).parent))\n"
        "runpy.run_path(sys.argv[0], run_name='__main__')\n"
    )
    return _run_without_gone_modules(code, path, *argv)


def _top_level_defs(rel: str) -> set[str]:
    tree = ast.parse((REPO / rel).read_text(encoding="utf-8"))
    return {
        node.name
        for node in tree.body
        if isinstance(node, ast.ClassDef | ast.FunctionDef)
    }


def _modules_holding_the_moved_symbols() -> list[str]:
    """Each tracked `narrow_lib` module, other than the gone two, defining all the symbols."""
    modules = [
        rel
        for rel in tracked(f"{NARROW_LIB}/*.py")
        if "/tests/" not in rel and PurePosixPath(rel).stem not in (BROAD, CONTAINERS)
    ]
    assert f"{NARROW_LIB}/secret_keys.py" in modules
    return [
        rel
        for rel in modules
        if set(MOVED_SYMBOLS) <= {name.lstrip("_") for name in _top_level_defs(rel)}
    ]


@pytest.fixture
def secrets_tree(tmp_path) -> Tree:
    """A checkout with a secret a setup role reads and a secret one k8s role reads."""
    tree = build_tree(tmp_path)
    tree.write(SECRETS, BASE_SECRETS)
    tree.write(
        "ansible/roles/k8s/jellyfin/templates/secret.yaml.j2",
        "key: {{ jellyfin_api_key }}\n",
    )
    tree.write(SETUP_READER, "T={{ setup_only_token }}\n")
    tree.commit("secrets")
    return tree


def _rotate(tree: Tree, secrets: str) -> tuple[str, str]:
    old = git_out(tree.root, "rev-parse", "HEAD")
    tree.write(SECRETS, secrets)
    return old, tree.commit("rotate")


def test_land_help_exits_zero_without_the_gone_modules():
    done = _run_script_without_gone_modules(LAND, "--help")
    assert done.returncode == 0, done.stderr


def test_secret_setup_readers_help_exits_zero_without_the_gone_modules():
    done = _run_script_without_gone_modules(SECRET_READERS_SCRIPT, "--help")
    assert done.returncode == 0, done.stderr


def test_the_issues_grep_over_scripts_and_ansible_prints_nothing():
    pattern = f"narrow_lib.{BROAD}\\|narrow_lib import.*{BROAD}"
    done = git("grep", "-n", pattern, "--", "scripts", "ansible", cwd=REPO, check=False)
    assert done.returncode <= 1, done.stderr
    assert done.stdout == ""


def test_the_secret_key_reader_tests_pass_without_the_gone_modules():
    tests_dir = Path(__file__).resolve().parent
    node_ids = [str(tests_dir / node) for node in READER_TESTS]
    done = _run_without_gone_modules(
        "import pytest\n"
        "raise SystemExit(pytest.main(['-n0', '-p', 'no:cacheprovider', *sys.argv[1:]]))\n",
        *node_ids,
    )
    assert done.returncode == 0, done.stdout[-2000:]


@pytest.mark.parametrize("name", [BROAD, CONTAINERS])
def test_the_deploy_plane_module_is_not_tracked(name: str):
    assert tracked(f"{NARROW_LIB}/{name}.py") == []


def test_no_module_keeps_a_deploy_plane_rule():
    """Moving the remainder under another name is not deleting it."""
    modules = [rel for rel in tracked("scripts/*.py") if "/tests/" not in rel]
    assert LAND in modules
    kept = {
        rel: found
        for rel in modules
        if (found := _top_level_defs(rel) & DEPLOY_PLANE_RULES)
    }
    assert kept == {}


def test_reach_holds_no_k8s_reading():
    tree = ast.parse((REPO / REACH).read_text(encoding="utf-8"))
    names = {
        getattr(node, "attr", None) or getattr(node, "id", None)
        for node in ast.walk(tree)
    }
    assert "services_from_changed_paths" in names
    assert not names & K8S_READINGS


def test_a_narrow_lib_module_that_stays_holds_the_moved_symbols():
    assert len(_modules_holding_the_moved_symbols()) == 1


def test_the_plans_d6_18b_item_names_the_module_holding_the_moved_symbols():
    modules = _modules_holding_the_moved_symbols()
    assert modules, "no narrow_lib module holds the moved symbols"
    plan = (REPO / PLAN).read_text(encoding="utf-8")
    item = re.search(r"\*\*D6\.18b\b.*?(?=^\d+\. \*\*D6\.)", plan, re.S | re.M)
    assert item is not None
    assert PurePosixPath(modules[0]).name in item.group()


def test_the_secret_readers_script_names_the_setup_reader_of_a_moved_key(secrets_tree):
    """The deployer's subprocess: its stdout is the contract the tick parses."""
    old, new = _rotate(secrets_tree, ROTATED_SETUP_TOKEN)
    done = _run_script_without_gone_modules(
        SECRET_READERS_SCRIPT, old, new, "--repo", str(secrets_tree.root)
    )
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {
        "paths": [SETUP_READER],
        "unread": {},
        "refused": "",
    }


def test_the_secret_readers_script_names_the_deploy_tag_of_a_moved_key(secrets_tree):
    old, new = _rotate(secrets_tree, ROTATED_K8S_KEY)
    done = _run_script_without_gone_modules(
        SECRET_READERS_SCRIPT,
        "--deploy-tags",
        old,
        new,
        "--repo",
        str(secrets_tree.root),
    )
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip() == "jellyfin"


def test_a_setup_plane_landing_reads_the_key_readers_of_a_range(secrets_tree):
    old, new = _rotate(secrets_tree, ROTATED_SETUP_TOKEN)
    code = (
        "import json\n"
        "from pathlib import Path\n"
        f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
        "from deploy_tools.land_lib.key_readers import readable_key_readers\n"
        "found = readable_key_readers(sys.argv[1], Path(sys.argv[2]))\n"
        "print(json.dumps([reader.path for reader in found]))\n"
    )
    done = _run_without_gone_modules(code, f"{old}..{new}", str(secrets_tree.root))
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == [SETUP_READER]


def test_land_reach_and_narrow_setup_import_without_the_gone_modules():
    """Both stay until H7, so neither may lean on what this change deletes."""
    code = (
        f"sys.path.insert(0, {str(REPO / 'scripts')!r})\n"
        f"sys.path.insert(0, {str(REPO / 'scripts/deploy_tools')!r})\n"
        "import narrow_setup\n"
        "from deploy_tools.land_lib import land_reach\n"
    )
    done = _run_without_gone_modules(code)
    assert done.returncode == 0, done.stderr
