"""The `a-single-predicate-check-is-a-row` census row: a one-predicate check is a row.

`test_census_rows_suite.py` runs it with its own rows, from a module of its own so that file
stays under its module-length cap.
"""

import ast
import functools
import re

from _row_table import REPO, Census, Subject, tracked

SUITE = "tests/repo/test_census_rows_suite.py"

READS_THE_TREE = re.compile(
    r"\.r?glob\(|\bls-files\b|\btracked\(|\bos\.walk\(|\brole_dirs\("
    r"|\.read_text\(|\.read_bytes\(|\bopen\(|\bsafe_load\(|\bload_\w+\(|\b_load\("
    r"|^from test_\w+ import",
    re.MULTILINE,
)
RUNS_CODE = re.compile(
    r"\bsubprocess\b|\brun\(\[(?!\"git\", \"ls-files\")|\brender\w*\("
    r"|\btmp_path(?:_factory)?\b|\bmonkeypatch\b|\burlopen\("
)
# `git_out` shells out to `git ls-files`, which a row may do.
RENDERS = re.compile(r"\brender\w*\(")
BUILDS_A_ROW = re.compile(r"\b(?:Census|RunbookRule|Property)\(")
RENDERING_MODULES = re.compile(r"renderer(?:\.|$)|_property_table$")


@functools.cache
def _helper_function_renders(module: str, name: str) -> bool:
    """Whether tracked module `module` defines function `name`, and it renders.

    The function renders when its own source does, or when it calls a function of the same
    module that does, at any depth.
    """
    for rel in tracked(f"*/{module.rpartition('.')[2]}.py"):
        text = (REPO / rel).read_text(errors="replace")
        defs = {
            node.name: node
            for node in ast.walk(ast.parse(text))
            if isinstance(node, ast.FunctionDef)
        }
        pending, seen = [name], set()
        while pending:
            current = pending.pop()
            if current in seen or current not in defs:
                continue
            seen.add(current)
            if RENDERS.search(ast.get_source_segment(text, defs[current]) or ""):
                return True
            pending += [
                call.func.id
                for call in ast.walk(defs[current])
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)
            ]
    return False


def _imported_call_renders(module: str, name: str) -> bool:
    return bool(RENDERING_MODULES.match(module)) or _helper_function_renders(
        module, name
    )


def _calls_a_rendering_helper(text: str) -> bool:
    """Whether the module calls a function it imports, and that function renders.

    A test that renders through `route_tree()` has no `render(` of its own to match. A call
    through an imported module, `homelab_render.cluster(...)`, counts as a call of the module.
    An import that is never called, such as a renderer constant, does not count.
    """
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return False
    imported = {
        (alias.asname or alias.name): (node.module, alias.name)
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for alias in node.names
    }
    callees = (
        node.func.value if isinstance(node.func, ast.Attribute) else node.func
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    )
    return any(
        isinstance(callee, ast.Name)
        and callee.id in imported
        and _imported_call_renders(*imported[callee.id])
        for callee in callees
    )


def _standalone_check_offence(subject: Subject) -> list[str]:
    text = subject.text
    if (
        BUILDS_A_ROW.search(text)
        or not READS_THE_TREE.search(text)
        or RUNS_CODE.search(text)
        or _calls_a_rendering_helper(text)
    ):
        return []
    return ["a standalone check over tracked text; make it a row"]


SINGLE_PREDICATE_CHECK_IS_A_ROW = Census(
    name="a-single-predicate-check-is-a-row",
    reason=(
        "A one-selector, one-predicate check is a row, not a file "
        "(.claude/rules/python-layout.md). A row fails `subject gone: delete this row` "
        "once its selector matches nothing, so it retires with its subject; a file "
        "passes over an empty census until someone notices. Write the check as a "
        "`Census` row, a row of the workflow table, a rendered-property row or a "
        "`RunbookRule` row. A check that needs "
        "a second render context, a cross-document join or a long exemption list keeps "
        "its file and goes in `allow` with that reason."
    ),
    files=lambda: tracked("tests/*/test_*.py"),
    offence=_standalone_check_offence,
    red=(
        Subject(
            "tests/repo/test_x.py",
            "def test_ci_pins_its_runner():\n"
            "    ci = safe_load((REPO / '.github/workflows/ci.yml').read_text())\n"
            "    assert ci['jobs']['pytest']['runs-on'] != 'ubuntu-latest'\n",
        ),
        Subject(
            "tests/repo/test_x.py",
            "def test_every_doc_is_short():\n"
            "    for p in DOCS.glob('*.md'):\n"
            "        assert len(p.read_text()) < 9000\n",
        ),
        Subject(
            "tests/repo/test_x.py",
            "from lib.git_testing import git_out\n"
            "def test_every_doc_is_short():\n"
            "    for p in git_out(REPO, 'ls-files', 'docs').splitlines():\n"
            "        assert len((REPO / p).read_text()) < 9000\n",
        ),
        Subject(
            "tests/repo/test_x.py",
            "".join(
                f"def test_doc_{i}_is_short():\n"
                f"    assert len((REPO / 'docs/{i}.md').read_text()) < 9000\n"
                for i in range(12)
            ),
        ),
        Subject(
            "tests/deploy/test_x.py",
            "from renderer.renderer_lib.restart import DEFAULT_TRIGGERS\n"
            "def test_every_trigger_is_known():\n"
            "    for role in role_dirs():\n"
            "        assert set(load_defaults(role)['restart_on']) <= DEFAULT_TRIGGERS\n",
        ),
    ),
    green=(
        Subject(
            "tests/repo/test_x.py",
            "ROWS = (Census(files=lambda: tracked('docs/*.md')),)\n"
            "def test_row_holds(): ...\n",
        ),
        Subject("tests/repo/test_x.py", "def test_parse(): assert parse('a') == 1\n"),
        Subject(
            "tests/repo/test_x.py",
            "def test_the_render_names_a_port():\n"
            "    assert 'port' in render_role_template('a', 'b.j2')\n",
        ),
        Subject(
            "tests/repo/test_x.py",
            "from test_alertmanager_routing import route_tree\n"
            "def test_the_tree_names_a_receiver():\n"
            "    assert 'blackhole' in route_tree()['receiver']\n"
            "    assert (REPO / 'a.yml').read_text()\n",
        ),
        Subject(
            "tests/deploy/test_x.py",
            "from renderer.renderer_lib.bundle import bundle_files\n"
            "def test_the_bundle_lists_a_kind():\n"
            "    assert 'a.yaml' in safe_load(bundle_files({})['index.yaml'])\n",
        ),
        Subject(
            "tests/k8s/test_x.py",
            "from renderer import homelab_render\n"
            "def test_a_tag_renders():\n"
            "    assert homelab_render.cluster(OUT, ['a'], None) == 0\n"
            "    assert (OUT / 'a.yaml').read_text()\n",
        ),
        Subject(
            "tests/k8s/test_x.py",
            "def test_a_scratch_tree_is_read(tmp_path_factory):\n"
            "    out = tmp_path_factory.mktemp('o')\n"
            "    assert (out / 'a.yaml').read_text()\n",
        ),
        Subject(
            "tests/k8s/test_x.py",
            "from _property_table import check\n"
            "from test_rendered_properties import PROPERTIES\n"
            "def test_a_row_flags_a_port():\n"
            "    assert any(check(p, [DOC]) for p in PROPERTIES)\n",
        ),
    ),
    min_matches=100,
    must_find=frozenset(
        {
            SUITE,
            "tests/repo/test_runbook_rules.py",
            "tests/k8s/test_rendered_properties.py",
        }
    ),
    allow={
        "tests/repo/test_ci_lint_group.py": (
            "a join of pyproject.toml's lint group and default groups against every "
            "workflow that installs the project"
        ),
        "tests/repo/test_claude_md_paragraphs_have_one_home.py": (
            "a join of each long CLAUDE.md paragraph against every other operator doc"
        ),
        "tests/repo/test_doc_values_hook.py": (
            "a join of the prek hook's `files` pattern against the paths the doc_values "
            "rows name and the paths the docs-refresh cron stages"
        ),
        "tests/repo/test_failure_class_detectors.py": (
            "a join of docs/failure-classes.md against the detector tests it cites"
        ),
        "tests/repo/test_host_package_case_inputs_census.py": (
            "a join of the render step's CASE_INPUTS against the modules the cases "
            "script imports and the tracked tree"
        ),
        "tests/repo/test_kind_node_image_follows_k3s.py": (
            "a join of CI's kind node image against the k3s version in the "
            "system-upgrade role's defaults"
        ),
        "tests/repo/test_renovate_canary_is_scheduled_not_per_merge.py": (
            "a join of ci.yml and renovate-config-canary.yml, which run the one "
            "retrying validator script"
        ),
        "tests/repo/test_vale_matches_the_ci_pin.py": (
            "a join of the Vale version and sha256 CI installs against the "
            "homelab-tools package pin"
        ),
        "tests/services/test_ha_trusted_proxies_cover_cloudflare.py": (
            "a join of Home Assistant's trusted_proxies against the pod CIDR and the "
            "Cloudflare ranges in the group vars"
        ),
        "tests/services/test_scrape_target_flap_rule.py": (
            "a join of the flap rules against the promtool cases that exercise them"
        ),
        "tests/services/test_rule_thresholds_owned_by_rules_files.py": (
            "a join of each pinned threshold against its rule's span in two "
            "directories, and of the rules files against the routing table"
        ),
        "tests/services/test_host_check_rule_literals.py": (
            "a join of the rule file against the inventory, which #9019 rewrites"
        ),
        "tests/services/test_claude_cgroup_consumers_agree_with_the_writer.py": (
            "a join of the writer script's metrics against the alert rules and the "
            "Grafana board that read them"
        ),
        "tests/repo/test_every_ci_job_is_gated.py": (
            "a join of ci.yml's jobs and the gate step's reads against the ruleset "
            "contexts in the github-checks defaults"
        ),
        "tests/repo/test_pytest_shards_partition_the_suite.py": (
            "a join of ci.yml's pytest matrix against pytest_shard.py's assignment of "
            "the tracked suite and its runner-seconds table"
        ),
        "tests/repo/test_release_fetches_use_pinned_tool.py": (
            "a join of every workflow's release fetches against the pinned-tool action "
            "and the tools it fetches"
        ),
        "tests/repo/test_static_ratchet_hook.py": (
            "a join of the prek hooks' entries and `files` gates against the modules "
            "they name, their import closure and ci.yml's prek steps"
        ),
        "tests/repo/test_testpaths_covers_every_test_file.py": (
            "a join of pyproject.toml's testpaths against every tracked test file"
        ),
        "tests/repo/test_ty_config_covers_the_repo.py": (
            "a join of ty's include and extra-paths against pytest's pythonpath and "
            "testpaths and every tracked Python file"
        ),
        "tests/services/test_alert_runbook_urls.py": (
            "a join of the alert rules against the docs pages and headings they link, "
            "the inventory hosts, the group vars' domain and the promtool cases"
        ),
        "tests/services/test_storage_rule_literals.py": (
            "a join of storage.yaml against the Longhorn robustness allow-list, the "
            "roles' snapshot caps and transients, and the host checks' disk floor"
        ),
        "tests/services/test_pi_peer_backup_schedule_clears_the_longhorn_slots.py": (
            "a join of pi-peer-backup's schedule against two other roles' Longhorn "
            "slots and the group vars' time zone"
        ),
        "tests/deploy/test_channel_tag_digest_pins_stay_off_autodeploy.py": (
            "a join of the roles' channel-tag digest pins against the auto-deploy denylist"
        ),
        "tests/deploy/test_ci_cancelled_is_not_a_verdict.py": (
            "runs ci_verdict over built check-runs, and joins its no-verdict constant "
            "against CLAUDE.md and docs/landing.md"
        ),
        "tests/deploy/test_land_help_names_every_verdict.py": (
            "a join of land.py's --help verdict line against the Verdict enum"
        ),
        "tests/deploy/test_landing_annotations.py": (
            "a join of the fields the land ledger writes, by running it, against the "
            "Landings dashboard's queries"
        ),
        "tests/deploy/test_renovate_denylist_marker_reaches_the_pr_title.py": (
            "a join of renovate.json's marker rules against the denylist rule prefixes "
            "the automerge test owns"
        ),
        "tests/deploy/test_renovate_edge_images_stay_one_per_pr.py": (
            "a join of renovate.json's edge rule against the denylist rule and the pins "
            "in the files it names"
        ),
        "tests/deploy/test_renovate_per_package_manual_rules_carry_the_denylist_marker.py": (
            "a join of renovate.json's per-package rules against the expected-deps table, "
            "the role tree and the auto-deploy denylist"
        ),
        "tests/k8s/test_cluster_base_mirrors_the_k3s_identity.py": (
            "a join of the read-only identity's keys across setup/k3s, cluster-base and "
            "the k3s-server component"
        ),
        "tests/k8s/test_netpol_probe_targets.py": (
            "a join of netpol-baseline's probe targets against the game servers and the "
            "k8s service list"
        ),
        "tests/k8s/test_uncovered_roles_justifications_resolve.py": (
            "a join of the security-context test's exemption block against the role "
            "templates and the tracked test modules"
        ),
        "tests/longhorn/test_weekly_and_nobackup_tiers_are_derived.py": (
            "renders the Longhorn tier lists through the repo's Jinja filters"
        ),
    },
)
