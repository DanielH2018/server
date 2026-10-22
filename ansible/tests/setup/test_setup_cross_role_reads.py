"""Setup roles that call a filter plugin, or read another setup role's defaults, match the tables.

The setup roles calling a filter plugin's filters are a deployer table,
`deploy_cross_role.SETUP_ROLES_CALLING_FILTER_PLUGINS` (#3874). `ansible/deploy.yml` runs no
setup role, so the deployer re-applies or records them beside the deploy plane.
`_FILTER_CALLERS_EXEMPT` names each call that leaves nothing rendered for a plugin change to
stale, with the reason. A plugin registering no filter, such as `filter_errors.py`, inherits
the callers of the plugins importing it (#4527).

A setup role reading another's `defaults/` variable is a row of
`SETUP_FILES_SHIPPED_BY_OTHER_ROLES` (#4303), held by its own census: the scan matches a
defaults key as a word, the way the filter-plugin census matches a filter name.
`_DEFAULTS_READS_EXEMPT` names each match that is not a read, with what it matched instead
(#4357). `test_setup_cross_role_files.py` holds the rest of that table.

Run: uv run pytest ansible/tests/setup/test_setup_cross_role_reads.py
"""

import ast
import re

from _helpers import ROLES
from _role_census import role_dirs
from deploy_tools.narrow_lib import filters as narrow_filters
from lib import yaml_fast

from deployer_lib.deploy_cross_role import (
    SETUP_FILES_SHIPPED_BY_OTHER_ROLES,
    SETUP_ROLES_CALLING_FILTER_PLUGINS,
)

# A setup role naming a plugin's filter where no rendered value can go stale, with the reason.
_FILTER_CALLERS_EXEMPT = {
    ("ansible/filter_plugins/toposort.py", "docker_install"): (
        "`filter_by_platform` runs in the `loop:` of `engine-upgrade.yml`, a never-tagged "
        "task file an operator runs by hand, so it renders nothing the plugin change leaves"
    ),
}
KNOWN_FILTER_CALLERS = {
    "ansible/filter_plugins/service_tier.py": {"k3s"},
    "ansible/filter_plugins/k8s_autodeploy.py": {"gitops_deploy"},
    "ansible/filter_plugins/longhorn_groups.py": {"k3s"},
    # Registers no filter, but `longhorn_groups` and `service_tier` wrap theirs in it (#4527).
    "ansible/filter_plugins/filter_errors.py": {"k3s"},
}
_COMMENT_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)


def filter_callers(
    names: dict[str, set[str]], texts: dict[str, dict[str, str]]
) -> dict[str, frozenset[str]]:
    """Each plugin whose filter a setup role's YAML or template names, mapped to those roles.

    Args:
        names: a plugin path -> the filter names it registers.
        texts: a role -> {its file path: text}, `.py`, `.md` and `tests/` already left out:
            Python names a filter without piping into it, and neither of the other two
            reaches a host.
    """
    out: dict[str, set[str]] = {}
    for plugin, filters in names.items():
        words = re.compile(rf"(?<!\w)({'|'.join(map(re.escape, filters))})(?!\w)")
        for role, files in texts.items():
            if any(words.search(_COMMENT_LINE.sub("", t)) for t in files.values()):
                out.setdefault(plugin, set()).add(role)
    return {plugin: frozenset(roles) for plugin, roles in out.items()}


def _registers_no_filter(text: str) -> bool:
    """Whether `FilterModule.filters()` returns a literal empty dict, as a helper's does."""
    for cls in ast.parse(text).body:
        if isinstance(cls, ast.ClassDef) and cls.name == "FilterModule":
            for fn in cls.body:
                if isinstance(fn, ast.FunctionDef) and fn.name == "filters":
                    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
                    return (
                        len(returns) == 1
                        and isinstance(returns[0].value, ast.Dict)
                        and not returns[0].value.keys
                    )
    return False


def _plugin_texts() -> dict[str, str]:
    repo = ROLES.parent.parent
    return {
        str(p.relative_to(repo)): p.read_text()
        for p in sorted((ROLES.parent / "filter_plugins").glob("*.py"))
    }


def _plugin_names() -> dict[str, set[str]]:
    """Each plugin's filter names, less a helper that registers none (`filter_errors.py`)."""
    return {
        path: narrow_filters.filter_names(text, path)
        for path, text in _plugin_texts().items()
        if not _registers_no_filter(text)
    }


def helper_callers(
    texts: dict[str, str], callers: dict[str, frozenset[str]]
) -> dict[str, frozenset[str]]:
    """Each helper plugin mapped to the union of the callers of the plugins importing it.

    A helper registers no filter, so no setup role names one of its own, yet a change to it
    changes every filter its importers wrap in it (#4527). Importers are found by the import
    spelling `narrow_lib/filters.py:plugin_names` greps for.

    Args:
        texts: a plugin path -> its source.
        callers: a plugin path -> the setup roles calling one of its filters.
    """
    out = {}
    for helper, text in texts.items():
        if not _registers_no_filter(text):
            continue
        stem = re.escape(helper.rsplit("/", 1)[-1].removesuffix(".py"))
        imports = re.compile(rf"^\s*(from|import)\s+{stem}\b", re.MULTILINE)
        roles = frozenset().union(
            *(
                callers.get(path, frozenset())
                for path, source in texts.items()
                if path != helper and imports.search(source)
            )
        )
        if roles:
            out[helper] = roles
    return out


def _setup_role_texts() -> dict[str, dict[str, str]]:
    texts = {}
    for role_dir in role_dirs(ROLES / "setup"):
        texts[role_dir.name] = {
            str(f): f.read_text()
            for f in sorted(role_dir.rglob("*"))
            if f.is_file()
            and f.suffix not in (".py", ".md", ".pyc")
            and "tests" not in f.relative_to(role_dir).parts
        }
    return texts


def _tree_filter_callers() -> dict[str, frozenset[str]]:
    found = filter_callers(_plugin_names(), _setup_role_texts())
    kept = {
        plugin: frozenset(r for r in roles if (plugin, r) not in _FILTER_CALLERS_EXEMPT)
        for plugin, roles in found.items()
    }
    kept = {plugin: roles for plugin, roles in kept.items() if roles}
    return kept | helper_callers(_plugin_texts(), kept)


def test_the_filter_callers_table_equals_the_tree():
    assert _tree_filter_callers() == SETUP_ROLES_CALLING_FILTER_PLUGINS
    for plugin, roles in KNOWN_FILTER_CALLERS.items():
        assert SETUP_ROLES_CALLING_FILTER_PLUGINS.get(plugin) == roles, plugin


def test_every_filter_caller_exemption_is_still_a_caller():
    # An exemption for a call that is gone is dead weight, and would hide a new one.
    found = filter_callers(_plugin_names(), _setup_role_texts())
    for plugin, role in _FILTER_CALLERS_EXEMPT:
        assert role in found.get(plugin, ()), (plugin, role)


def test_a_setup_role_calling_a_filter_is_flagged():
    texts = {"k3s": {"defaults/main.yml": "x: \"{{ list | tier_claims('a') }}\"\n"}}
    assert filter_callers({"p.py": {"tier_claims"}}, texts) == {
        "p.py": frozenset({"k3s"})
    }


def test_a_helper_plugin_inherits_the_callers_of_the_plugins_importing_it():
    helper = "class FilterModule:\n    def filters(self):\n        return {}\n"
    texts = {
        "fp/h.py": helper,
        "fp/a.py": "from h import wrap\n",
        "fp/b.py": "import h\n",
        "fp/c.py": "from hx import wrap\n",
    }
    callers = {
        "fp/a.py": frozenset({"k3s"}),
        "fp/b.py": frozenset({"gitops_deploy"}),
        "fp/c.py": frozenset({"claude_code"}),
    }
    assert helper_callers(texts, callers) == {
        "fp/h.py": frozenset({"k3s", "gitops_deploy"})
    }
    # A helper no caller-bearing plugin imports maps to nothing, as an uncalled plugin does.
    assert helper_callers(texts, {"fp/c.py": frozenset({"k3s"})}) == {}


def test_a_setup_role_naming_a_filter_only_in_a_comment_or_a_longer_word_is_clean():
    texts = {"k3s": {"a.yml": "# tier_claims is read here\ny: my_tier_claims_x\n"}}
    assert filter_callers({"p.py": {"tier_claims"}}, texts) == {}


# Word matches of a defaults key that are not reads of the variable, each with what the scan
# matched instead. An exemption the scan no longer finds fails below.
_DEFAULTS_READS_EXEMPT = {
    ("ansible/roles/setup/k3s/defaults/main.yml", "hypervisor"): (
        "etcd-restore-drill-vm.sh.j2 declares a bash local named k3s_version, filled from "
        "`k3s --version` on the host, and staging-nwfilter.xml.j2 names k3s_agent_node_ips "
        "only inside an XML comment"
    ),
}


def defaults_readers(
    keys: dict[str, set[str]], texts: dict[str, dict[str, str]]
) -> dict[str, frozenset[str]]:
    """Each `defaults/` file mapped to the OTHER setup roles naming one of its keys (#4303).

    Args:
        keys: a defaults path -> the top-level keys it defines.
        texts: as `filter_callers` takes them.
    """
    found = filter_callers(keys, texts)
    out = {path: roles - {path.split("/")[3]} for path, roles in found.items()}
    return {path: roles for path, roles in out.items() if roles}


def _defaults_keys() -> dict[str, set[str]]:
    repo = ROLES.parent.parent
    return {
        str(p.relative_to(repo)): set(yaml_fast.safe_load(p.read_text()) or {})
        for role_dir in role_dirs(ROLES / "setup")
        for p in sorted((role_dir / "defaults").glob("*.yml"))
    }


def _tree_defaults_readers() -> dict[str, frozenset[str]]:
    return defaults_readers(_defaults_keys(), _setup_role_texts())


def test_the_defaults_rows_equal_the_trees_cross_role_reads():
    found = _tree_defaults_readers()
    kept = {
        path: frozenset(r for r in roles if (path, r) not in _DEFAULTS_READS_EXEMPT)
        for path, roles in found.items()
    }
    recorded = {
        path: roles
        for path, roles in SETUP_FILES_SHIPPED_BY_OTHER_ROLES.items()
        if "/defaults/" in path
    }
    assert {path: roles for path, roles in kept.items() if roles} == recorded
    assert recorded.get("ansible/roles/setup/initial_setup/defaults/main.yml") == {
        "claude_code",
        "homelab_builder",
        "renovate_agent",
    }


def test_every_defaults_exemption_is_still_matched():
    found = _tree_defaults_readers()
    for path, role in _DEFAULTS_READS_EXEMPT:
        assert role in found.get(path, ()), (path, role)


def test_a_role_reading_another_roles_default_is_flagged():
    owner = "ansible/roles/setup/initial_setup/defaults/main.yml"
    texts = {
        "initial_setup": {"tasks/crons.yml": "name: '{{ initial_setup_group }}'\n"},
        "claude_code": {"tasks/a.yml": "groups: '{{ [initial_setup_group] }}'\n"},
    }
    assert defaults_readers({owner: {"initial_setup_group"}}, texts) == {
        owner: frozenset({"claude_code"})
    }


def test_a_role_reading_only_its_own_default_is_clean():
    owner = "ansible/roles/setup/initial_setup/defaults/main.yml"
    texts = {"initial_setup": {"tasks/crons.yml": "x: '{{ initial_setup_group }}'\n"}}
    assert defaults_readers({owner: {"initial_setup_group"}}, texts) == {}
