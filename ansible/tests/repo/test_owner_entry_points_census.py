"""Every entry point in D2's owner table still reads which tags Ansible may not deploy.

docs/target-architecture/deploy/d2-first-leaf.md, *What each entry point does with a tag that
is not Ansible-owned*, lists each path that could apply or report on a tag Flux applies, and
says how it skips one. Each skip has its own `_is_clean` / `_is_flagged` pair beside the code.
Those pairs pass vacuously when the entry point is renamed or stops calling the reader, because
they import the reader, not the entry point (failure class 4). This census names each entry
point as a member it must find, as `file:symbol`, and follows the calls from it down to one of
the three ownership readers that `ansible/tests/deploy/test_owner_readers_agree.py` holds
equal.

The census runs both ways against the plan: a table row no member names fails, and so does a
member whose row the plan no longer has.

Run: uv run pytest ansible/tests/repo/test_owner_entry_points_census.py
"""

import ast
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

from _deploy_sh_callers_row import PLAN, table_rows
from _helpers import REPO

# `table_rows` returns the header row's first cell with the others.
HEADER = "Entry point"

# The three readers of the `owner` flag. Every chain that reads the tree ends at one of them.
READERS = frozenset({"tag_owners", "ansible_owned", "service_owners"})


@dataclass(frozen=True)
class Link:
    """One step of a chain: the definition `symbol` in `path` calls `reads`.

    `symbol` is a function or class name, found anywhere in the module. The call is a
    `reads(...)` or `<anything>.reads(...)` node, not a bare name: a local variable that
    shares the reader's name would otherwise satisfy the link with the call gone. A link with
    `call=False` needs only a read of the name, for a field default or a constant. A link with
    no `symbol` reads `path` as text and needs `reads` as a literal in it, for a playbook, a
    shell template or an argv string.
    """

    path: str
    symbol: str | None
    reads: str
    call: bool = True


OWNERS = "scripts/deploy_tools/deploy_lib/owners.py"
RUN = "scripts/deploy_tools/deploy_lib/run.py"
RENDER_TARGETS = "scripts/deploy_tools/render_targets.py"
RETIRED = "scripts/diagnostics/probe_lib/releases_retired.py"
ROTATION_TOOLS = "scripts/secrets_mgmt/rotation_tools.py"

FLUX_OWNED = (
    Link(OWNERS, "read_owners", "flux_owned"),
    Link(OWNERS, "flux_owned", "tag_owners"),
)
RECORD_TARGETS = Link(RENDER_TARGETS, "render_record_targets", "tag_owners")
TREE_OWNERS = Link(RETIRED, "tree_owners", "tag_owners")

# Each first cell of the plan's owner table, as written there, to the chain from the entry
# point down to the reader it asks.
ENTRY_POINTS: dict[str, tuple[Link, ...]] = {
    "`deploy.yml` k8s play": (
        Link("ansible/deploy.yml", None, "containers_list | ansible_owned"),
    ),
    "`deploy.sh --tags T`": (
        Link(RUN, "_gated_run", "refuse_flux_owned"),
        Link(OWNERS, "refuse_flux_owned", "read_owners"),
        *FLUX_OWNED,
    ),
    "`deploy.sh --changed`": (Link(RUN, "run_changed", "read_owners"), *FLUX_OWNED),
    "`deploy.sh --tags manifests`": (
        Link(RUN, "expand_shared_roles", "read_owners"),
        *FLUX_OWNED,
    ),
    "`deploy.sh --list-services`": (
        Link(RUN, "_gated_run", "mark_owners"),
        Link(RUN, "_gated_run", "read_owners"),
        *FLUX_OWNED,
    ),
    "GitOps deployer": (
        Link(
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_phases.py",
            "plan_tick",
            "host_vars_owners",
        ),
        Link(
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_io.py",
            "host_vars_owners",
            "service_owners",
        ),
    ),
    "`land.sh`": (
        Link("scripts/deploy_tools/land_lib/deploy.py", "deploy_phase", "split_owned"),
        Link(
            "scripts/deploy_tools/land_lib/promotion.py",
            "split_owned",
            "owners_at_merge",
        ),
        Link(
            "scripts/deploy_tools/land_lib/promotion.py", "owners_at_merge", "owners_at"
        ),
        Link("scripts/deploy_tools/land_lib/land_flux.py", "owners_at", "flux_owned"),
        Link(OWNERS, "flux_owned", "tag_owners"),
    ),
    "`smoke_caller`": (
        Link(
            "scripts/deploy_tools/shared_role_callers.py",
            "smoke_caller",
            "render_record_targets",
        ),
        RECORD_TARGETS,
    ),
    "`render_targets.py` CLI": (
        Link(RENDER_TARGETS, "main", "render_record_targets"),
        RECORD_TARGETS,
    ),
    # `cmd_rotate` calls `tools.owners()`, a field of `RotationTools` whose default is `tree_owners`.
    "`secret_rotation.py --deploy`": (
        Link("scripts/secrets_mgmt/secret_rotation.py", "cmd_rotate", "owners"),
        Link(ROTATION_TOOLS, "RotationTools", "tree_owners", call=False),
        Link(ROTATION_TOOLS, "tree_owners", "tag_owners"),
    ),
    "`probe.py health T`": (
        Link(
            "scripts/diagnostics/probe_lib/health.py", "_release_record", "tree_owners"
        ),
        TREE_OWNERS,
    ),
    "`probe.py releases --stale-only`": (
        Link("scripts/diagnostics/probe_lib/releases.py", "run_releases", "drop_owned"),
        Link(RETIRED, "drop_owned", "tree_owners"),
        TREE_OWNERS,
    ),
    "`probe.py releases` (plain listing)": (
        Link(
            "scripts/diagnostics/probe_lib/releases.py", "run_releases", "tree_owners"
        ),
        TREE_OWNERS,
    ),
    "`manifest-prune-check.sh`": (
        Link(
            "ansible/roles/setup/k3s/templates/manifest-prune-check.sh.j2",
            None,
            "read_flux_kustomizations",
        ),
    ),
    "`live_drift_check.py`": (
        Link(
            "ansible/roles/setup/k3s/files/live_drift_check.py",
            "is_server_side_applied",
            "LAST_APPLIED",
            call=False,
        ),
    ),
    "`gen_role_glance.py`, `service_catalog.py`": (
        Link("scripts/docs/gen_role_glance.py", "stale_k8s_docs", "tag_owners"),
        Link("scripts/docs/service_catalog.py", "build_rows", "tag_owners"),
    ),
    "`deploy_ui`": (
        Link(
            "ansible/roles/setup/deploy_ui/files/deploy_ui.py",
            None,
            '"./scripts/deploy.sh", "--tags"',
        ),
    ),
}

# The rows whose chain ends somewhere other than a tree reader, each with the reason.
NOT_A_TREE_READER = {
    "`manifest-prune-check.sh`": (
        "a host check: it skips an object only while its Kustomization exists live, read "
        "with the read-only kubeconfig, because a label alone outlives a handback"
    ),
    "`live_drift_check.py`": (
        "a host check: `is_server_side_applied` already leaves Flux's objects out, and "
        "`last-applied-configuration` is what marks one that a handback returned to Ansible"
    ),
    "`deploy_ui`": "it runs `deploy.sh --tags`, whose refusal reads `tag_owners`",
}

Source = Callable[[str], str | None]


def _repo_text(rel: str) -> str | None:
    path = REPO / rel
    return path.read_text() if path.is_file() else None


def _name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    return node.attr if isinstance(node, ast.Attribute) else None


def _names_in(tree: ast.Module, symbol: str, call: bool) -> set[str] | None:
    """The names the definition `symbol` reads, or None when none is defined.

    With `call`, only the names it calls: the function of each `ast.Call` in its body.
    """
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
            and node.name == symbol
        ):
            found = (
                (n.func for n in ast.walk(node) if isinstance(n, ast.Call))
                if call
                else ast.walk(node)
            )
            return {name for n in found if (name := _name(n))}
    return None


def _link_problems(link: Link, source: Source) -> list[str]:
    text = source(link.path)
    if text is None:
        return [f"{link.path} is gone"]
    if link.symbol is None:
        return (
            []
            if link.reads in text
            else [f"{link.path} no longer holds `{link.reads}`"]
        )
    names = _names_in(ast.parse(text), link.symbol, link.call)
    if names is None:
        return [f"{link.path} defines no `{link.symbol}`"]
    if link.reads not in names:
        verb = "calls" if link.call else "reads"
        return [f"{link.path}:{link.symbol} no longer {verb} `{link.reads}`"]
    return []


def census_problems(
    plan: str,
    entry_points: Mapping[str, tuple[Link, ...]] = ENTRY_POINTS,
    source: Source = _repo_text,
) -> list[str]:
    """Every way `entry_points` and the plan text `plan` disagree, or [] when they agree."""
    rows = table_rows(plan) - {HEADER}
    found = [
        f"{PLAN}'s owner table has the row {cell}, which no member of ENTRY_POINTS names"
        for cell in sorted(rows - entry_points.keys())
    ]
    found += [
        f"ENTRY_POINTS names {cell}, which is no row of {PLAN}'s owner table"
        for cell in sorted(entry_points.keys() - rows)
    ]
    found += [
        f"NOT_A_TREE_READER names {cell}, which ENTRY_POINTS does not"
        for cell in sorted(NOT_A_TREE_READER.keys() - entry_points.keys())
    ]
    for cell, links in sorted(entry_points.items()):
        found += [
            f"{cell}: {p}" for link in links for p in _link_problems(link, source)
        ]
        last = set(re.findall(r"\w+", links[-1].reads))
        if cell not in NOT_A_TREE_READER and not last & READERS:
            found.append(
                f"{cell}: the chain ends at `{links[-1].reads}`, which is none of "
                f"{sorted(READERS)}"
            )
    return found


def _plan() -> str:
    return (REPO / PLAN).read_text()


def _source_with(rel: str, text: str) -> Source:
    return lambda path: text if path == rel else _repo_text(path)


def test_every_entry_point_in_the_owner_table_reads_ownership_is_clean():
    assert len(table_rows(_plan())) >= 17
    assert census_problems(_plan()) == []


def test_a_renamed_entry_point_is_flagged():
    text = (REPO / RENDER_TARGETS).read_text().replace("def main(", "def cli_main(")
    assert census_problems(_plan(), source=_source_with(RENDER_TARGETS, text)) == [
        f"`render_targets.py` CLI: {RENDER_TARGETS} defines no `main`"
    ]


def test_an_entry_point_that_stops_reading_ownership_is_flagged():
    rel = "scripts/deploy_tools/shared_role_callers.py"
    text = (
        (REPO / rel)
        .read_text()
        .replace("for name in render_record_targets(", "for name in render_targets(")
    )
    assert census_problems(_plan(), source=_source_with(rel, text)) == [
        f"`smoke_caller`: {rel}:smoke_caller no longer calls `render_record_targets`"
    ]


def test_a_playbook_loop_that_drops_the_filter_is_flagged():
    rel = "ansible/deploy.yml"
    text = (
        (REPO / rel)
        .read_text()
        .replace("containers_list | ansible_owned", "containers_list")
    )
    assert census_problems(_plan(), source=_source_with(rel, text)) == [
        f"`deploy.yml` k8s play: {rel} no longer holds `containers_list | ansible_owned`"
    ]


def test_a_plan_row_no_member_names_is_flagged():
    plan = _plan().replace(
        "| `deploy_ui` |", "| `new_entry.py` | Skips T.\n| `deploy_ui` |", 1
    )
    assert census_problems(plan) == [
        f"{PLAN}'s owner table has the row `new_entry.py`, which no member of "
        "ENTRY_POINTS names"
    ]


def test_a_chain_that_stops_short_of_a_reader_is_flagged():
    short = dict(ENTRY_POINTS)
    short["`deploy.sh --changed`"] = (Link(RUN, "run_changed", "read_owners"),)
    assert census_problems(_plan(), short) == [
        "`deploy.sh --changed`: the chain ends at `read_owners`, which is none of "
        f"{sorted(READERS)}"
    ]


def test_an_owner_map_no_longer_read_from_the_tools_is_flagged():
    """A local named `owners` must not stand in for the `tools.owners()` call."""
    rel = "scripts/secrets_mgmt/secret_rotation.py"
    text = (REPO / rel).read_text().replace("owners = tools.owners()", "owners = {}")
    assert census_problems(_plan(), source=_source_with(rel, text)) == [
        f"`secret_rotation.py --deploy`: {rel}:cmd_rotate no longer calls `owners`"
    ]
