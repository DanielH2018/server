"""Textual censuses of the tracked docs, templates, workflows and paths, one row each.

Each row replaces a test file of its own. A row's `reason` keeps what that file's
docstring said a reader needs before changing the rule. The harness supplies the
`subject gone` failure, the floor, the named members, the stale-allow check and the red/green
proof (`tests/_row_table.py`). Python-source rows are in `test_census_rows_python.py`.

Run: uv run pytest tests/repo/test_census_rows_text.py
"""

import re
from functools import cache

import pytest
from _config_rows import CONFIG_ROWS
from _shimmed_path_row import PREPENDED_PATHS, RESET_PATHS, SHIMMED_PATHS
from _test_script_bodies_row import TEST_SCRIPT_BODIES
from _component_contract_row import COMPONENT_TIMERS_CARRY_A_CONTRACT
from _agent_tools_key_row import AGENTS_LIMIT_TOOLS_WITH_THE_TOOLS_KEY
from _comment_ticket_refs_row import COMMENTS_CITE_NO_TICKET
from _etcd_snapshot_flags_row import ETCD_SNAPSHOT_LONG_FLAGS
from _runbook_k3s_sudo_row import RUNBOOK_K3S_COMMANDS_CARRY_SUDO
from _group_vars_host_reader_row import HOST_KEYS_IN_GROUP_VARS_HAVE_A_READER
from _kuma_push_row import AFFECTED_TABLE, NOTHING_SHIPPED_PUSHES_TO_UPTIME_KUMA
from _prek_files_paths_row import PREK_FILES_PATHS_RESOLVE
from _role_python_paths_row import ROLE_FILES_CITE_REAL_PYTHON_PATHS
from _unit_kubeconfig_row import UNITS_REACHING_THE_CLUSTER_SET_KUBECONFIG
from _read_only_skill_writes_row import READ_ONLY_REVIEW_SKILLS_WRITE_NO_TRACKED_FILE
from _review_ledger_glob_row import REVIEW_SKILLS_LIST_LEDGERS_FROM_ANY_USERS_STORE
from _archived_plan_citations_row import SHIPPED_FILES_CITE_NO_TARGET_ARCHITECTURE_STUB
from _ansible_mention_row import LIVE_DOCS_NAME_NO_ANSIBLE
from _helpers import REPO, discover_docs
from deploy_tools.land_lib import policy
from lib.facts.citations import macro_citations
from _host_package_units import host_package_units as _host_package_units
from _row_table import (
    Census,
    Subject,
    check,
    lines_matching,
    proof_problems,
    tracked,
)


# ── Shared macros named in the docs ───────────────────────────────────────────────────

# `docs/reference/backlog.md` renders the findings register, so its rows are issue titles. An
# issue proposing to delete a macro names it there, and nothing is copyable from a register row.
GENERATED_REGISTER = "docs/reference/backlog.md"


@cache
def _available_macros() -> frozenset[str]:
    return frozenset(rel.rsplit("/", 1)[1] for rel in tracked("k8s/templates/*.yml.j2"))


def _macro_docs() -> list[str]:
    # The corpus floor is `test_documented_paths_exist.py`'s: one corpus, one floor.
    docs = (str(d.relative_to(REPO)) for d in discover_docs())
    return [rel for rel in docs if rel != GENERATED_REGISTER]


def _missing_macro_offence(subject: Subject) -> list[str]:
    lines = subject.text.splitlines()
    # `macro_citations` keeps one directory level, so role-local app config
    # (`templates/config/config.yml.j2` in configarr) is told apart from a shared macro,
    # which the docs always give bare or as `templates/<macro>.yml.j2`.
    # A name a `config/`-qualified mention ties to a role's own app config is that file
    # wherever else the doc names it bare.
    local = {
        bare
        for line in lines
        for raw in macro_citations(line)
        for prefix, _, bare in [raw.rpartition("/")]
        if prefix == "config"
    }
    return [
        f"line {n} names {bare}"
        for n, line in enumerate(lines, 1)
        for raw in macro_citations(line)
        for bare in [raw.rpartition("/")[2]]
        if bare not in local
        and bare != "docker-compose.yml.j2"
        and bare not in _available_macros()
    ]


# ── Other selectors and predicates ────────────────────────────────────────────────────


# `.items()` anywhere on a line, unless that line also sorts.
UNSORTED_ITEMS = re.compile(r"\.items\(\)(?![^\n]*\bsort\b)")

STANDING_SLUG = "review-standing-decisions"
# The surfaces a review primes from; each must name the slug the memory store holds.
PRIMED_SURFACES = frozenset(
    {
        ".claude/skills/homelab-review/SKILL.md",
        ".claude/agents/skeptic.md",
        ".claude/skills/ha-review/SKILL.md",
    }
)


# ── Units the host packages ship ─────────────────────────────────────────────────────


def _namespace_dir_files() -> list[str]:
    # A role ships its Python from `files/`; only its `tests/` is a namespace directory.
    return tracked("scripts/*", "tests/*", "hosts/tests/*", "k8s/roles/*/tests/*")


def _compiled_offence(subject: Subject) -> list[str]:
    return (
        ["is a compiled module file"] if policy.is_compiled_module(subject.rel) else []
    )


ROWS = (
    Census(
        name="no-compiled-module-file-is-tracked",
        reason=(
            "A tracked pyc runs in place of its unchanged source, and an extension module is "
            "imported before the `.py` beside it, so either one replaces code with bytes no "
            "diff shows. `.gitignore` excludes both, which `git add -f` bypasses. The landing "
            "gate refuses them with the same predicate, `policy.is_compiled_module`; this row "
            "also covers a commit that reaches master another way."
        ),
        files=lambda: tracked("*"),
        offence=_compiled_offence,
        red=(
            Subject("scripts/lib/__pycache__/gh.cpython-314.pyc", ""),
            Subject("scripts/lib/gh.cpython-314-x86_64-linux-gnu.so", ""),
            Subject("scripts/lib/gh.pyc", ""),
        ),
        green=(
            Subject("scripts/lib/gh.py", ""),
            Subject("docs/pycache-notes.md", ""),
        ),
        min_matches=1000,
        must_find=frozenset({"scripts/deploy_tools/land_lib/policy.py"}),
    ),
    Census(
        name="documented-macros-exist",
        reason=(
            "A deleted macro stays named in the operator docs, and the one that breaks is the "
            "`/new-container` skill's skeleton: it opens with a `{% from '<macro>.yml.j2' "
            "import ... %}` inside a fenced block no renderer reads, so a copy of it cannot "
            "render. Restore the macro or fix the doc."
        ),
        files=_macro_docs,
        offence=_missing_macro_offence,
        red=(Subject("a.md", "{% from 'gone-macro.yml.j2' import x %}\n"),),
        green=(
            Subject("a.md", "{% from 'checksum-annotation.yml.j2' import x %}\n"),
            Subject("b.md", "`templates/config/app.yml.j2`, then app.yml.j2 again\n"),
        ),
        must_find=frozenset({"docs/adr/index.md", "CLAUDE.md"}),
    ),
    Census(
        name="root-claude-md-has-no-self-disclaiming-prose",
        reason=(
            'A line like "read it rather than trusting this one" is prose admitting it cannot '
            "hold the fact it just wrote out. Each such fact has an owner (a hook, a generator, "
            "a test), so cut the restatement to a pointer at that owner. Every session loads "
            "this file."
        ),
        files=lambda: tracked("CLAUDE.md"),
        offence=lines_matching(
            re.compile(
                r"rather than trusting|written here went stale|read it rather",
                re.IGNORECASE,
            )
        ),
        red=(
            Subject("a.md", "the current list; read it rather than trusting this one."),
        ),
        green=(
            Subject(
                "a.md", "`FRAGMENTS` in that generator lists every tunable it reads."
            ),
        ),
        must_find=frozenset({"CLAUDE.md"}),
    ),
    Census(
        name="markdown-carries-no-merge-conflict-marker",
        reason=(
            "A conflict resolved by hand can leave a marker behind, and mkdocs renders it, "
            "and the base section after a diff3 `|||||||` line, as prose (#6928). A reader "
            "cannot tell the stale copy from the live one. Delete the marker and the side "
            "master's later edits did not build on. A bare `=======` is left alone, because "
            "it is also a setext heading underline."
        ),
        files=lambda: tracked("*.md"),
        offence=lines_matching(re.compile(r"^(<{7}|\|{7}|>{7})( |$)")),
        red=(
            Subject("a.md", "- live bullet\n||||||| parent of 10ac6bb1e (Page)\n"),
            Subject("a.md", "<<<<<<< HEAD\n- ours\n"),
            Subject("a.md", "- theirs\n>>>>>>> topic\n"),
        ),
        green=(
            Subject("a.md", "Title\n=======\n\n| a | b |\n|---|---|\n"),
            Subject("a.md", "`<<<<<<< HEAD` opens a conflict.\n"),
        ),
        min_matches=300,
        must_find=frozenset(
            {
                "CLAUDE.md",
                "docs/adr/index.md",
            }
        ),
    ),
    Census(
        name="templates-iterate-dicts-sorted",
        reason=(
            "`.items()` renders a dict in insertion order, so reordering the source changes the "
            "rendered bytes with no change in meaning: the manifests role restarts the workload "
            "and image-builder rebuilds. Use `{% for k, v in d | dictsort %}`; keys differing "
            "only by case need `dictsort(true)`. Every `.j2` counts: a `templates/config/` file "
            "is ConfigMap bytes through `lookup('template')`."
        ),
        files=lambda: tracked("*.j2"),
        offence=lines_matching(UNSORTED_ITEMS),
        red=(Subject("a.j2", "{% for key, value in k8s_psa_labels.items() %}"),),
        green=(
            Subject("a.j2", "{% for key, value in k8s_psa_labels | dictsort %}"),
            Subject("b.j2", "{% for key, value in labels.items() | sort %}"),
        ),
        min_matches=392,
        must_find=frozenset(
            {
                "k8s/roles/observability/templates/00-namespace.yaml.j2",
                "k8s/roles/image-builder/templates/context-configmap.yaml.j2",
                "k8s/templates/checksum-annotation.yml.j2",
            }
        ),
    ),
    Census(
        name="no-init-py-in-namespace-dirs",
        reason=(
            "`scripts/`, `tests/` and a role's `tests/` resolve as PEP 420 namespace "
            "packages. An `__init__.py` renames every test module beneath it, which breaks the "
            "basename-unique convention and the `from <dir> import <mod>` form `scripts/` "
            "importers use. An editor or a scaffolding tool adds one silently."
        ),
        files=_namespace_dir_files,
        offence=lambda s: (
            ["is an __init__.py"] if s.rel.endswith("/__init__.py") else []
        ),
        red=(
            Subject("scripts/lib/__init__.py", ""),
            Subject("k8s/roles/svc/tests/__init__.py", ""),
        ),
        green=(Subject("scripts/lib/yaml_fast.py", ""),),
        min_matches=500,
        must_find=frozenset({"scripts/lib/yaml_fast.py", "tests/_helpers.py"}),
    ),
    Census(
        name="host-package-units-are-named-homelab",
        reason=(
            "`homelab-reconcile` stops, starts and restarts only `homelab-*` units, and "
            "`prerm` stops only those. A unit a host package ships under any other name is "
            "never stopped when the manifest drops it, and an Ansible-era unit of the same "
            "name collides with it on the host. Rename the unit `homelab-<name>` and list "
            "the Ansible-era unit as a legacy entry (H3, *Shared names for the host slices*)."
        ),
        files=_host_package_units,
        offence=lambda s: (
            []
            if s.rel.startswith("homelab-")
            else [f"shipped by {s.text} is not named homelab-*"]
        ),
        red=(
            Subject(
                "pi-sd-health.timer", "hosts/components/pi-heartbeats/component.yml"
            ),
        ),
        green=(
            Subject(
                "homelab-pi-sd-health.timer",
                "hosts/components/pi-heartbeats/component.yml",
            ),
        ),
        must_find=frozenset(
            {
                "homelab-updater.service",
                "homelab-updater.timer",
                "homelab-reconcile.service",
                "homelab-pi-sd-health.timer",
                "homelab-pi-gz-integrity.timer",
                "homelab-pi-recovery.timer",
                "homelab-pi-watchdog-health.timer",
                "homelab-pi-audit-health.timer",
            }
        ),
    ),
    Census(
        name="review-surfaces-name-the-standing-slug",
        reason=(
            "The positive half of the retired-slug row: each surface a review primes from must "
            f"name `{STANDING_SLUG}`, the slug the memory store holds."
        ),
        files=lambda: sorted(PRIMED_SURFACES),
        offence=lambda s: (
            [] if STANDING_SLUG in s.text else [f"does not name {STANDING_SLUG}"]
        ),
        red=(Subject("a.md", "prime from the standing list first"),),
        green=(Subject("a.md", f"prime from `{STANDING_SLUG}` first"),),
        min_matches=len(PRIMED_SURFACES),
        must_find=PRIMED_SURFACES,
    ),
    Census(
        name="no-template-hand-writes-depends-on",
        reason=(
            "The renderer generates every Flux `dependsOn` from toposort.py's map, so the play "
            "and Flux order roles by one source (D4.2). A hand-written edge would drift from "
            "it. Templates are the only place one takes effect outside the renderer: no k8s "
            "role applies a static manifest from `files/`. Left out: the flux role's vendored "
            "`gotk-components.yaml`, whose CRD schema names the field, and `docs/`'s examples."
        ),
        files=lambda: [
            rel
            for rel in tracked("k8s/roles/*", "k8s/templates/*")
            if rel.startswith("k8s/templates/") or rel.split("/")[3:4] == ["templates"]
        ],
        offence=lines_matching(re.compile(r"\bdependsOn\s*:")),
        red=(Subject("t.yaml.j2", "spec:\n  dependsOn:\n    - name: traefik\n"),),
        green=(Subject("t.yaml.j2", "spec:\n  path: ./cluster/x\n"),),
        min_matches=300,
        must_find=frozenset(
            {
                "k8s/templates/networkpolicy.yml.j2",
                "k8s/roles/netpol-baseline/templates/networkpolicy.yaml.j2",
                "k8s/roles/flux/templates/sync.yaml.j2",
            }
        ),
    ),
)

ROWS += CONFIG_ROWS
ROWS += (
    SHIMMED_PATHS,
    PREPENDED_PATHS,
    RESET_PATHS,
    TEST_SCRIPT_BODIES,
    ETCD_SNAPSHOT_LONG_FLAGS,
    RUNBOOK_K3S_COMMANDS_CARRY_SUDO,
    NOTHING_SHIPPED_PUSHES_TO_UPTIME_KUMA,
    AGENTS_LIMIT_TOOLS_WITH_THE_TOOLS_KEY,
    COMPONENT_TIMERS_CARRY_A_CONTRACT,
    PREK_FILES_PATHS_RESOLVE,
    ROLE_FILES_CITE_REAL_PYTHON_PATHS,
    UNITS_REACHING_THE_CLUSTER_SET_KUBECONFIG,
    REVIEW_SKILLS_LIST_LEDGERS_FROM_ANY_USERS_STORE,
    READ_ONLY_REVIEW_SKILLS_WRITE_NO_TRACKED_FILE,
    COMMENTS_CITE_NO_TICKET,
    SHIPPED_FILES_CITE_NO_TARGET_ARCHITECTURE_STUB,
    HOST_KEYS_IN_GROUP_VARS_HAVE_A_READER,
    LIVE_DOCS_NAME_NO_ANSIBLE,
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    # fact: .claude/skills/homelab-review/SKILL.md#2. Prime from memory FIRST (the signal-booster — do this before dispatching)
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)


def test_the_macro_corpus_skips_the_generated_register():
    """The one exclusion; `must_find` above is the positive control that a doc is still read."""
    assert GENERATED_REGISTER not in _macro_docs()


def test_the_kuma_push_census_skips_the_affected_test_table():
    """The table names test paths such as `tests/repo/_kuma_push_row.py`, which match the row."""
    assert AFFECTED_TABLE in tracked("scripts/*")
    assert AFFECTED_TABLE not in NOTHING_SHIPPED_PUSHES_TO_UPTIME_KUMA.files()
