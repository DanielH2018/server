"""Censuses of the pytest-only modules: the render-and-path shared-helper rules, one row each.

A test module renders Jinja and finds the repo root through the shared helpers, never by hand:
its Jinja environment comes from `lib.ansible_jinja_env`, the repo root from `lib.repo_paths`,
and a shell template's text from `_shell_render`. Each rule was a test in
`test_tests_share_render_and_path_helpers.py` until #3430. The detectors are in
`_render_helper_rules.py`, and the census is `_test_module_rules.pytest_only_modules`, shared
with the git and subprocess rows in `test_census_rows_test_modules.py`.

Run: uv run pytest scripts/tests/test_census_rows_test_renders.py
"""

import pytest
from _inventory_path_rules import inline_inventory_paths
from _render_helper_rules import (
    bare_jinja_envs,
    local_repo_roots,
    shell_template_source_reads,
)
from _row_table import Census, Subject, check, proof_problems, share
from _test_module_rules import MODULE_FLOOR, pytest_only_modules

from lib.repo_paths import REPO

# The sibling module the sibling-import red subject reads, at the path it resolves to.
_SIBLINGS = {REPO / "ansible/tests/x/_drill.py": 'HOSTS = REPO / "hosts"\n'}

ROWS = (
    Census(
        name="tests-build-no-bare-jinja-environment",
        reason=(
            "A bare `jinja2.Environment()` has none of Ansible's whitespace flags or filters, "
            "so a `{% for %}` leaves a blank line per iteration and `| bool` fails or means "
            "Python's `bool()`. Use `lib.ansible_jinja_env.register_ansible_filters(make_env())` "
            "(pass `make_env` `undefined_cls=` for a strict render) or `_jinja_env.jinja_env` for a "
            "structure-valued expression. `liquid.Environment` is another language."
        ),
        files=pytest_only_modules,
        offence=lambda s: bare_jinja_envs(s.text),
        red=(
            Subject("a.py", "from jinja2 import Environment\nEnvironment()\n"),
            Subject("b.py", "import jinja2\njinja2.Environment(trim_blocks=True)\n"),
            Subject(
                "c.py", "from jinja2.nativetypes import NativeEnvironment as N\nN()\n"
            ),
        ),
        green=(
            Subject(
                "a.py",
                "from lib.ansible_jinja_env import make_env, register_ansible_filters\n"
                "register_ansible_filters(make_env())\n",
            ),
            Subject("b.py", "import liquid\nliquid.Environment()\n"),
            Subject("c.py", '"""jinja2.Environment() used to be built here."""\n'),
        ),
        min_matches=MODULE_FLOOR,
        must_find=frozenset(
            {
                ".claude/hooks/tests/test_hook_scripts_executable.py",
                "ansible/roles/k8s/monitor-bridge/tests/test_check_ha.py",
                "ansible/tests/k8s/_manifest_guards.py",
                "ansible/tests/longhorn/_restore_drill.py",
                "ansible/tests/longhorn/test_longhorn_reap_guard.py",
                "ansible/tests/staging/test_staging_network.py",
                "scripts/docs/tests/test_mkdocs_repo_links.py",
            }
        ),
        allow={
            "ansible/tests/_jinja_env.py": (
                "the sanctioned expression helper itself: `jinja_env` is the "
                "NativeEnvironment every other test borrows"
            ),
            "ansible/roles/k8s/home-assistant/tests/jinja_harness.py": (
                "renders Home Assistant's Jinja, not Ansible's; Ansible's filters would be "
                "wrong there"
            ),
        },
    ),
    Census(
        name="tests-take-the-repo-root-from-repo-paths",
        reason=(
            "Forty-seven test modules computed the repo root from `Path(__file__)` under three "
            "names, where `lib.repo_paths.REPO` already is that path. Import it (`REPO as "
            "_REPO` keeps a local name). The arithmetic is evaluated against the module's own "
            "location, so `parents[3]` is flagged in one directory and passes in another."
        ),
        files=pytest_only_modules,
        offence=lambda s: local_repo_roots(s.text, (REPO / s.rel).resolve()),
        red=(
            Subject(
                "scripts/x/tests/test_y.py",
                "REPO = Path(__file__).resolve().parents[3]\n",
            ),
            Subject(
                "scripts/x/tests/test_y.py",
                "_REPO: Path = Path(__file__).parent.parent.parent.parent\n",
            ),
            Subject(
                "scripts/x/tests/test_y.py",
                "ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname("
                "os.path.abspath(__file__)))))\n",
            ),
        ),
        green=(
            Subject("scripts/x/tests/test_y.py", "from lib.repo_paths import REPO\n"),
            # `parents[1]` is the module's own package, which is not what REPO names.
            Subject(
                "scripts/x/tests/test_y.py",
                "HERE = Path(__file__).resolve().parents[1]\n",
            ),
        ),
        min_matches=MODULE_FLOOR,
    ),
    Census(
        name="tests-take-inventory-paths-from-repo-paths",
        reason=(
            "`lib.repo_paths` owns `HOSTS_INI`, `ALL_VARS`, `HOST_VARS`, `K3S_SERVER`, "
            "`K3S_HOST_CHECKS`, `K3S_FILES` and `K3S_DEFAULTS`, and #3912, #3982 and #4133 "
            "moved the test modules "
            "onto them. A hit is a `/` chain whose string parts spell one of those paths; the "
            "message names the constant. Its root is `REPO` (or `_REPO`, `REPO_ROOT`), `ANSIBLE`, `ROLES`, "
            "`INVENTORY`, `K3S_SERVER` or `K3S_HOST_CHECKS` (also as `Path(...)` or "
            "`v.ROLES`), an "
            'alias of one, a local name bound to an anchored chain (`K3S = REPO / "hosts" / '
            '"components" / "k3s-server"`, then `K3S / "defaults" / "main.yml"`), or '
            "`Path(__file__)` arithmetic "
            "evaluated at the module (#4135). A chain counts only for the segments it spells "
            'itself, so `K3S_SERVER / "templates"` passes. A module binding its own `INVENTORY` is a '
            'hit too. A repo-relative string such as `"ansible/inventory/group_vars/all.yml"` '
            "is not a chain, which is how the deploy classifiers' tests pass those as inputs. "
            "A name imported from a first-party module takes that module's binding "
            "(`from _restore_drill import K3S`), and an imported module's `m.__file__` is "
            "its file, resolved the way pytest imports it (#4155)."
        ),
        files=pytest_only_modules,
        offence=lambda s: inline_inventory_paths(s.text, REPO / s.rel, _SIBLINGS),
        red=(
            Subject("a.py", 'REPO / "ansible/inventory/group_vars/all.yml"\n'),
            Subject("b.py", 'ANSIBLE / "inventory" / "group_vars" / "all.yml"\n'),
            Subject("c.py", 'Path(REPO) / "ansible/inventory/host_vars" / name\n'),
            Subject(
                "d.py", '_REPO / "hosts/components" / "k3s-server/defaults/main.yml"\n'
            ),
            Subject(
                "e.py",
                'REPO / "hosts" / "components" / "k3s-server" / "defaults" / "main.yml"\n',
            ),
            # A local INVENTORY shadowing the repo_paths one, and a chain built from it.
            Subject("f.py", 'INVENTORY = ANSIBLE / "inventory"\n'),
            Subject("g.py", 'INVENTORY / "group_vars" / "all.yml"\n'),
            # The k3s component directories and files/, however the chain is rooted (#4133).
            Subject("h.py", 'REPO / "hosts" / "components" / "k3s-host-checks"\n'),
            Subject(
                "i.py", 'REPO / "hosts/components/k3s-host-checks/files" / "x.py"\n'
            ),
            Subject(
                "j.py", 'v.ANSIBLE.parent / "hosts/components/k3s-server" / "units"\n'
            ),
            # A path built in two steps (#4135). The first step of `l.py` is a hit on its
            # own; `k.py`'s owns nothing, so only resolving HOSTS makes it red.
            Subject(
                "l.py",
                'K3S = REPO / "hosts" / "components" / "k3s-server"\n'
                'K3S / "defaults" / "main.yml"\n',
            ),
            Subject(
                "k.py",
                'HOSTS = REPO / "hosts"\nHOSTS / "components/k3s-server/defaults/main.yml"\n',
            ),
            # A root outside the anchor set, evaluated from the module's own location.
            Subject(
                "ansible/roles/k8s/x/tests/test_y.py",
                "ROLE = Path(__file__).resolve().parents[1]\n"
                'ROLE.parents[2] / "inventory" / "host_vars" / "daniel-box.yml"\n',
            ),
            # A root bound in a sibling module, which owns nothing until the import resolves.
            Subject(
                "ansible/tests/x/test_y.py",
                "from _drill import HOSTS\n"
                'HOSTS / "components" / "k3s-server" / "defaults" / "main.yml"\n',
            ),
            # A root computed from another module's file: monitor-bridge's `files/check.py`.
            Subject(
                "ansible/roles/k8s/monitor-bridge/tests/test_y.py",
                "import check\n"
                "Path(check.__file__).resolve().parents[5]"
                ' / "hosts/components/k3s-server" / "defaults" / "main.yml"\n',
            ),
        ),
        green=(
            Subject(
                "a.py", "from lib.repo_paths import ALL_VARS\nALL_VARS.read_text()\n"
            ),
            Subject("b.py", 'classify(["ansible/inventory/group_vars/all.yml"])\n'),
            Subject("c.py", 'tmp_path / "inventory" / "group_vars" / "all.yml"\n'),
            Subject("d.py", 'REPO / "ansible" / "templates"\n'),
            Subject("e.py", 'K3S_SERVER / "templates" / "config.yaml.j2"\n'),
            # A local alias of a constant spells nothing past it.
            Subject("f.py", 'K3S = K3S_HOST_CHECKS\nK3S / "templates"\n'),
            Subject(
                "g.py",
                'REPO / "hosts" / "components" / name / "defaults" / "main.yml"\n',
            ),
        ),
        min_matches=MODULE_FLOOR,
        # The classifier tests holding repo-relative inventory strings as inputs, and modules
        # #4002, #4133 and #4135 converted, are in the census the row reads.
        must_find=frozenset(
            {
                "ansible/tests/deploy/test_tree_lock_single_definition.py",
                "ansible/tests/setup/test_host_python_invocations.py",
                "evals/tests/test_eval_cases.py",
                "ansible/tests/setup/test_claude_agent_ssh_login.py",
                "ansible/tests/k8s/_manifest_guards.py",
            }
        ),
        allow={
            "scripts/lib/tests/test_render_guard.py": (
                "`test_anchors_resolve_to_the_real_tree` checks that `repo_paths`' own "
                "`INVENTORY` anchor reaches the real tree, so it spells a path under it"
            ),
        },
    ),
    Census(
        name="tests-read-shell-templates-rendered",
        reason=(
            "A guard reading a `*.sh.j2`'s SOURCE matches `{{ ... }}` the moment a value it "
            "asserts on moves into a role default, and a pattern matching nothing passes "
            "(#3178). Use `_shell_render.rendered_shell_text(plane, role, name)`, or "
            "`render_shell_script` where the test runs the script. The detector sees a path "
            "ending in `*.sh.j2`, a `glob('*.sh.j2')` roster, a loop over a template resolver "
            "(#3200), a resolver result handed through a fixture parameter (#3206), and a read "
            "inside a local helper (#3220). It cannot see an `rglob('*')` census. An `allow` "
            "entry is permanent: its subject is something a render erases. The list grows "
            "only when the detector learns a spelling it was blind to; a conversion never "
            "adds an entry."
        ),
        files=pytest_only_modules,
        offence=lambda s: shell_template_source_reads(s.text),
        red=(
            Subject(
                "a.py", 'DRILL = K3S / "templates" / "drill.sh.j2"\nDRILL.read_text()\n'
            ),
            Subject(
                "b.py",
                'T: Path = Path("hosts/components/k3s-host-checks/templates/drill.sh.j2")\n'
                "T.read_bytes()\n",
            ),
            Subject(
                "c.py",
                '(K3S_HOST_CHECKS / "templates" / "drill.sh.j2").read_text()\n',
            ),
            Subject("d.py", 'for p in TEMPLATES.glob("*.sh.j2"):\n    pass\n'),
            # One `.sh.j2` constant that no substring of the source text spells (#5501).
            Subject("k2.py", 'T = ROLES / "t" / "drill.sh" ".j2"\nT.read_text()\n'),
            # A resolver hands back paths, so the module names no `*.sh.j2` anywhere.
            Subject(
                "e.py", "for tpl in sv.discover_templates():\n    tpl.read_text()\n"
            ),
            Subject("f.py", "for tpl in discover_templates():\n    tpl.read_text()\n"),
            Subject(
                "g.py",
                "TEMPLATES = sv.discover_templates()\nfor tpl in TEMPLATES:\n"
                "    tpl.read_bytes()\n",
            ),
            # A fixture hands its resolver result to consumers as a parameter.
            Subject(
                "h.py",
                "@pytest.fixture(scope='module')\ndef templates():\n"
                "    return sv.discover_templates()\ndef test_x(templates):\n"
                "    for tpl in templates:\n        tpl.read_text()\n",
            ),
            Subject(
                "i.py",
                "@pytest.fixture\ndef templates():\n    yield sv.discover_templates()\n"
                "def test_x(templates):\n    for tpl in templates:\n"
                "        tpl.read_bytes()\n",
            ),
            # A local helper's parameter does the reading, so the CALL is the read.
            Subject(
                "j.py",
                "def _source(path):\n"
                "    return '\\n'.join(l for l in path.read_text().splitlines())\n"
                'CHECK = K3S_HOST_CHECKS / "templates" / "drill.sh.j2"\n'
                "_source(path=CHECK)\n",
            ),
            Subject(
                "k.py",
                "def _source(path):\n    return path.read_text()\n"
                '_source(K3S_HOST_CHECKS / "templates" / "drill.sh.j2")\n',
            ),
        ),
        green=(
            Subject(
                "a.py",
                "from _shell_render import rendered_shell_text\n"
                'SCRIPT = rendered_shell_text("components", "k3s-host-checks", "drill.sh.j2")\n',
            ),
            Subject("b.py", 'NAMES = frozenset({"drill.sh.j2"})\n'),
            Subject(
                "c.py",
                'tpl = tmp_path / "good.sh.j2"\ntpl.write_text("")\ntpl.read_text()\n',
            ),
            # A resolver pointed at a tree the test wrote itself, through a fixture or not.
            Subject(
                "d.py",
                "for tpl in sv.discover_templates(tmp_path):\n    tpl.read_text()\n",
            ),
            Subject(
                "e.py",
                "@pytest.fixture\ndef templates(tmp_path):\n"
                "    return sv.discover_templates(tmp_path)\ndef test_x(templates):\n"
                "    for tpl in templates:\n        tpl.read_text()\n",
            ),
            Subject(
                "f.py",
                "@pytest.fixture\ndef names():\n    return frozenset({'drill.sh.j2'})\n"
                "def test_x(names):\n    for tpl in names:\n        tpl.read_text()\n",
            ),
            # A tuple-unpack over a resolver binds only its first element.
            Subject(
                "g.py",
                "for _tpl, other in sv.discover_templates():\n    other.read_text()\n",
            ),
            Subject(
                "h.py",
                "for path in discover_templates():\n    render_template(path)\n"
                "def rendered_or_source_text(path):\n    return path.read_text()\n",
            ),
            # Four ways a helper call is not a read: the helper reads nothing, it is imported,
            # a test parameter shadows its name, and the argument is a `tmp_path` template.
            Subject(
                "i.py",
                "def _name(path):\n    return path.name\n"
                'CHECK = K3S_HOST_CHECKS / "templates" / "drill.sh.j2"\n'
                "_name(CHECK)\n",
            ),
            Subject(
                "j.py",
                "from _shell_render import render_shell_script\n"
                'render_shell_script("components", "k3s-host-checks", "drill.sh.j2")\n',
            ),
            Subject(
                "k.py",
                "def _source(path):\n    return path.read_text()\n"
                'CHECK = K3S_HOST_CHECKS / "templates" / "drill.sh.j2"\n'
                "def test_x(_source):\n    _source(CHECK)\n",
            ),
            Subject(
                "l.py",
                "def _source(path):\n    return path.read_text()\n"
                '_source(tmp_path / "good.sh.j2")\n',
            ),
        ),
        min_matches=MODULE_FLOOR,
    ),
)

# Every row here and in its sibling file reads the same census, so they judge each module in
# one pass over one tree (#6437, #6465).
ROWS = share(ROWS)


_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
