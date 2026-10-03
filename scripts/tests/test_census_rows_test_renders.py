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
from _render_helper_rules import (
    bare_jinja_envs,
    local_repo_roots,
    shell_template_source_reads,
)
from _row_table import Census, Subject, check, proof_problems
from _test_module_rules import MODULE_FLOOR, pytest_only_modules

from lib.repo_paths import REPO

ROWS = (
    Census(
        name="tests-build-no-bare-jinja-environment",
        reason=(
            "A bare `jinja2.Environment()` has none of Ansible's whitespace flags or filters, "
            "so a `{% for %}` leaves a blank line per iteration and `| bool` fails or means "
            "Python's `bool()`. Use `lib.ansible_jinja_env.make_ansible_env` (pass "
            "`undefined_cls=` for a strict render) or `_helpers.jinja_env` for a "
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
                "from lib.ansible_jinja_env import make_ansible_env\nmake_ansible_env()\n",
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
                "ansible/tests/setup/test_kuma_check_timer.py",
                "ansible/tests/staging/test_staging_network.py",
                "scripts/deploy_tools/tests/test_deploy_exit_codes.py",
                "scripts/docs/tests/test_mkdocs_repo_links.py",
            }
        ),
        allow={
            "ansible/tests/_helpers.py": (
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
                'T: Path = Path("ansible/roles/setup/k3s/templates/drill.sh.j2")\n'
                "T.read_bytes()\n",
            ),
            Subject(
                "c.py",
                '(ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2").read_text()\n',
            ),
            Subject("d.py", 'for p in TEMPLATES.glob("*.sh.j2"):\n    pass\n'),
            # A resolver hands back paths, so the module names no `*.sh.j2` anywhere.
            Subject(
                "e.py",
                "for tpl, _task_file, _cron, _env in ct.iter_cron_targets():\n"
                "    tpl.read_text()\n",
            ),
            Subject("f.py", "for tpl in discover_templates():\n    tpl.read_text()\n"),
            Subject(
                "g.py",
                "CRONS = ct.cron_job_scripts()\nfor tpl, _task in CRONS.items():\n"
                "    tpl.read_bytes()\n",
            ),
            # A fixture hands its resolver result to consumers as a parameter.
            Subject(
                "h.py",
                "@pytest.fixture(scope='module')\ndef cron_map():\n"
                "    return ct.cron_job_scripts()\ndef test_x(cron_map):\n"
                "    for tpl in cron_map:\n        tpl.read_text()\n",
            ),
            Subject(
                "i.py",
                "@pytest.fixture\ndef cron_map():\n    yield ct.cron_job_scripts()\n"
                "def test_x(cron_map):\n    for tpl, _task in cron_map.items():\n"
                "        tpl.read_bytes()\n",
            ),
            # A local helper's parameter does the reading, so the CALL is the read.
            Subject(
                "j.py",
                "def _source(path):\n"
                "    return '\\n'.join(l for l in path.read_text().splitlines())\n"
                'CHECK = ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2"\n'
                "_source(path=CHECK)\n",
            ),
            Subject(
                "k.py",
                "def _source(path):\n    return path.read_text()\n"
                '_source(ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2")\n',
            ),
        ),
        green=(
            Subject(
                "a.py",
                "from _shell_render import rendered_shell_text\n"
                'SCRIPT = rendered_shell_text("setup", "k3s", "drill.sh.j2")\n',
            ),
            Subject("b.py", 'NAMES = frozenset({"drill.sh.j2"})\n'),
            Subject(
                "c.py",
                'tpl = tmp_path / "good.sh.j2"\ntpl.write_text("")\ntpl.read_text()\n',
            ),
            # A resolver pointed at a tree the test wrote itself, through a fixture or not.
            Subject(
                "d.py",
                "for tpl in ct.cron_job_scripts(tmp_path):\n    tpl.read_text()\n",
            ),
            Subject(
                "e.py",
                "@pytest.fixture\ndef cron_map(tmp_path):\n"
                "    return ct.cron_job_scripts(tmp_path)\ndef test_x(cron_map):\n"
                "    for tpl in cron_map:\n        tpl.read_text()\n",
            ),
            Subject(
                "f.py",
                "@pytest.fixture\ndef names():\n    return frozenset({'drill.sh.j2'})\n"
                "def test_x(names):\n    for tpl in names:\n        tpl.read_text()\n",
            ),
            # `iter_cron_targets` yields the template first; its task FILE is not the rule.
            Subject(
                "g.py",
                "for _tpl, task_file, _cron, _env in ct.iter_cron_targets():\n"
                "    task_file.read_text()\n",
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
                'CHECK = ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2"\n'
                "_name(CHECK)\n",
            ),
            Subject(
                "j.py",
                "from _shell_render import render_shell_script\n"
                'render_shell_script("setup", "k3s", "drill.sh.j2")\n',
            ),
            Subject(
                "k.py",
                "def _source(path):\n    return path.read_text()\n"
                'CHECK = ROLES / "setup" / "k3s" / "templates" / "drill.sh.j2"\n'
                "def test_x(_source):\n    _source(CHECK)\n",
            ),
            Subject(
                "l.py",
                "def _source(path):\n    return path.read_text()\n"
                '_source(tmp_path / "good.sh.j2")\n',
            ),
        ),
        min_matches=MODULE_FLOOR,
        allow={
            "ansible/tests/deploy/test_setup_render_manifest.py": (
                "hashes the template's raw BYTES — a trailing newline and a truncated-read "
                "comparison, neither of which survives a render"
            ),
            "scripts/validate/tests/test_shell_template_cron_rules.py": (
                "hands a cron rule its own second argument — the rule under test takes the "
                "template TEXT, and `shell_templates.py` reads it off disk the same way in "
                "production, so a render would test a different call than the gate makes"
            ),
        },
    ),
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)
