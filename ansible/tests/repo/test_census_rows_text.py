"""Textual censuses of the tracked docs, templates, workflows and paths, one row each.

Each row was a test file of its own until #3407. A row's `reason` keeps what that file's
docstring said a reader needs before changing the rule. The harness supplies the
`subject gone` failure, the floor, the named members, the stale-allow check and the red/green
proof (`ansible/tests/_row_table.py`). Python-source rows are in `test_census_rows_python.py`.

Run: uv run pytest ansible/tests/repo/test_census_rows_text.py
"""

import re
from functools import cache

import pytest
from _helpers import REPO, discover_docs
from deploy_tools.land_lib import policy
from lib.facts.citations import macro_citations
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
    return frozenset(
        rel.rsplit("/", 1)[1] for rel in tracked("ansible/templates/*.yml.j2")
    )


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

_RUNS_ON = re.compile(r"^\s*runs-on:\s*(.+?)\s*$", re.MULTILINE)
_VERSIONED = re.compile(r"^[a-z]+-\d+(\.\d+)?(-[a-z]+)?$")

# `.items()` anywhere on a line, unless that line also sorts.
UNSORTED_ITEMS = re.compile(r"\.items\(\)(?![^\n]*\bsort\b)")

STANDING_SLUG = "review-standing-decisions"
RETIRED_SLUGS = ("homelab-review-standing-donot-reflag",)
# The surfaces a review primes from; each must name the slug the memory store holds.
PRIMED_SURFACES = frozenset(
    {
        ".claude/skills/homelab-review/SKILL.md",
        ".claude/agents/skeptic.md",
        ".claude/skills/ha-review/SKILL.md",
    }
)


# The inline form: the heading phrase followed by a colon and the list itself. The pointer
# form ends the phrase with a full stop, so this matches the drifting copy and not its
# replacement.
INLINE_REFLAG_LIST = re.compile(r"Honor accepted designs \(don't re-flag\):")


def _namespace_dir_files() -> list[str]:
    # A role ships its Python from `files/`; only its `tests/` is a namespace directory.
    return tracked("scripts/*", "ansible/tests/*", "ansible/roles/*/*/tests/*")


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
        name="role-docs-deploy-through-deploy-sh",
        reason=(
            "`scripts/deploy.sh` takes the tree lock, snapshots HEAD and checks the tag and "
            "staleness before it runs the playbook; a bare `ansible-playbook ansible/deploy.yml` "
            "does none of that. The bare form spreads by copying a sibling doc. Write "
            '`./scripts/deploy.sh --tags "<svc>"`, adding `-e target=daniel-pi` for a Pi service.'
        ),
        files=lambda: [
            rel for rel in tracked("ansible/roles/*/*/CLAUDE.md") if rel.count("/") == 4
        ],
        offence=lines_matching(re.compile(r"ansible-playbook\s+ansible/deploy\.yml")),
        red=(
            Subject(
                "a.md",
                '- Deploy: `uv run ansible-playbook ansible/deploy.yml --tags "sonarr"`',
            ),
        ),
        green=(Subject("a.md", '- Deploy: `./scripts/deploy.sh --tags "sonarr"`'),),
        must_find=frozenset(
            f"ansible/roles/{role}/CLAUDE.md"
            for role in ("k8s/sonarr", "k8s/traefik", "containers/wg-easy", "setup/k3s")
        ),
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
        name="workflow-runners-are-pinned",
        reason=(
            "`runs-on: ubuntu-latest` floats: GitHub rolls the alias with no commit here, so a "
            "`language: system` prek hook can change verdict between two runs of one SHA. A "
            "versioned label (`ubuntu-24.04`) moves only through a Renovate PR, which runs the "
            "hooks on the new image before merge (docs/adr/0018)."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lambda s: [
            f"floating runner {label!r}"
            for label in _RUNS_ON.findall(s.text)
            if not _VERSIONED.match(label)
        ],
        red=(
            Subject("a.yml", "jobs:\n  a:\n    runs-on: ubuntu-latest\n"),
            Subject("b.yml", "jobs:\n  b:\n    runs-on: ${{ env.RUNNER }}\n"),
            Subject("c.yml", "jobs:\n  c:\n    runs-on: self-hosted\n"),
        ),
        green=(Subject("a.yml", "jobs:\n  a:\n    runs-on: ubuntu-24.04\n"),),
        must_find=frozenset(
            f".github/workflows/{name}"
            for name in ("ci.yml", "image-smoke.yml", "renovate-config-canary.yml")
        ),
    ),
    Census(
        name="workflows-fetch-through-the-shared-script",
        reason=(
            "The merge-base fetch needs a token in a one-shot `extraheader`, because every "
            "checkout runs with persist-credentials: false. `scripts/dev/pr_changed_files.sh` "
            "holds that fetch once, and image-smoke and the deck_mod job call it even though "
            "they read none of its file lists (#3777). A workflow that inlines the header again "
            "is a copy the next change to the fetch has to find by hand."
        ),
        files=lambda: tracked(".github/workflows/*.yml"),
        offence=lines_matching(re.compile(r"extraheader")),
        red=(
            Subject(
                "a.yml",
                'run: |\n  git -c "http.https://github.com/.extraheader=AUTHORIZATION: x" '
                "fetch origin main\n",
            ),
        ),
        green=(
            Subject(
                "a.yml",
                'run: |\n  "$GITHUB_WORKSPACE/scripts/dev/pr_changed_files.sh"\n',
            ),
        ),
        must_find=frozenset(
            f".github/workflows/{name}" for name in ("ci.yml", "image-smoke.yml")
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
                "ansible/roles/k8s/observability/templates/00-namespace.yaml.j2",
                "ansible/roles/k8s/image-builder/templates/context-configmap.yaml.j2",
                "ansible/roles/k8s/uptime-kuma/templates/status-page-sync-configmap.yaml.j2",
                "ansible/templates/checksum-annotation.yml.j2",
            }
        ),
    ),
    Census(
        name="no-init-py-in-namespace-dirs",
        reason=(
            "`scripts/`, `ansible/tests/` and a role's `tests/` resolve as PEP 420 namespace "
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
            Subject("ansible/roles/k8s/svc/tests/__init__.py", ""),
        ),
        green=(Subject("scripts/lib/yaml_fast.py", ""),),
        min_matches=500,
        must_find=frozenset({"scripts/lib/yaml_fast.py", "ansible/tests/_helpers.py"}),
    ),
    Census(
        name="review-surfaces-carry-no-retired-standing-slug",
        reason=(
            f"The standing-list memory is `{STANDING_SLUG}`. A missing memory is no error the "
            "harness reports, so a skill priming from a retired spelling proceeds unprimed and "
            "the fan-out re-derives settled decisions."
        ),
        files=lambda: tracked(
            *(
                f"{d}/*.{ext}"
                for d in (".claude/skills", ".claude/agents", "evals/cases")
                for ext in ("md", "json")
            )
        ),
        offence=lambda s: [slug for slug in RETIRED_SLUGS if slug in s.text],
        red=(
            Subject("a.md", "prime from `homelab-review-standing-donot-reflag` first"),
        ),
        green=(Subject("a.md", f"prime from `{STANDING_SLUG}` first"),),
        must_find=PRIMED_SURFACES,
    ),
    Census(
        name="agents-carry-no-inline-reflag-list",
        reason=(
            "The accepted designs live once, in "
            "`.claude/skills/homelab-review/accepted-designs.md`, one section per reviewer "
            "domain, and the settled-findings register in `docs/reference/backlog.md`. An "
            'inline "Honor accepted designs (don\'t re-flag): ..." list in an agent drifts '
            "from both. Point at the domain's section instead "
            "(`test_reviewer_agents_carry_no_hand_reflag_list.py` holds the pointers)."
        ),
        files=lambda: tracked(".claude/agents/*.md"),
        offence=lines_matching(INLINE_REFLAG_LIST),
        red=(Subject("a.md", "Honor accepted designs (don't re-flag): the arr stack"),),
        green=(
            Subject("a.md", "Honor accepted designs (don't re-flag). See `## cicd`."),
        ),
        must_find=frozenset(
            f".claude/agents/{name}.md"
            for name in (
                "homelab-container-reviewer",
                "homelab-cicd-reviewer",
                "homelab-backup-observability-reviewer",
            )
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
)

_IDS = [row.name for row in ROWS]


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_holds_on_the_tree(row: Census):
    problems = check(row)
    assert not problems, "\n".join(problems)


@pytest.mark.parametrize("row", ROWS, ids=_IDS)
def test_census_row_flags_its_red_subjects_and_passes_its_green_ones(row: Census):
    assert not proof_problems(row)


def test_the_macro_corpus_skips_the_generated_register():
    """The one exclusion; `must_find` above is the positive control that a doc is still read."""
    assert GENERATED_REGISTER not in _macro_docs()
