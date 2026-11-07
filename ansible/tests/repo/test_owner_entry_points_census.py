"""Every entry point in D2's owner table still reads which tags Ansible may not deploy.

docs/target-architecture/deploy/d2-first-leaf.md, *What each entry point does with a tag that
is not Ansible-owned*, lists each path that could apply or report on a tag Flux applies, and
says how it skips one. Each skip has its own `_is_clean` / `_is_flagged` pair beside the code.
Those pairs pass vacuously when the entry point is renamed or stops calling the reader, because
they import the reader, not the entry point (failure class 4). This census names each entry
point as a member it must find, as `file:symbol`, and follows the calls from it down to one of
the two ownership readers that `ansible/tests/deploy/test_owner_readers_agree.py` holds
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

# The two readers of the `owner` flag. Every chain that reads the tree ends at one of them.
READERS = frozenset({"tag_owners"})


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
ROTATION_TOOLS = "scripts/secrets_mgmt/rotation_tools.py"

K8S_TAGS = (
    Link(OWNERS, "read_k8s_tags", "declared_k8s_tags"),
    Link(OWNERS, "declared_k8s_tags", "k8s_tags"),
)

# Each first cell of the plan's owner table, as written there, to the chain from the entry
# point down to the reader it asks.
ENTRY_POINTS: dict[str, tuple[Link, ...]] = {
    "`deploy.sh --tags T`": (
        Link(RUN, "_gated_run", "refuse_k8s_tags"),
        Link(OWNERS, "refuse_k8s_tags", "read_k8s_tags"),
        *K8S_TAGS,
    ),
    "`deploy.sh --changed`": (
        Link(RUN, "run_changed", "changed"),
        Link("scripts/deploy_tools/deploy_lib/tags.py", "changed", "service_records"),
    ),
    "`deploy.sh --list-services`": (
        Link(RUN, "_gated_run", "mark_k8s_tags"),
        Link(RUN, "_gated_run", "read_k8s_tags"),
        *K8S_TAGS,
    ),
    "GitOps deployer": (
        Link(
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_phases.py",
            "plan_tick",
            "services_from_changed_paths",
        ),
        Link(
            "ansible/roles/setup/gitops_deploy/files/deployer_lib/deploy_changes.py",
            "services_from_changed_paths",
            "flux",
            call=False,
        ),
    ),
    "`land.sh`": (
        Link(
            "scripts/deploy_tools/land_lib/promotion.py",
            "wait_on_promotion",
            "_flux_owned",
        ),
        Link("scripts/deploy_tools/land_lib/promotion.py", "_flux_owned", "owners_at"),
        Link("scripts/deploy_tools/land_lib/land_flux.py", "owners_at", "flux_owned"),
        Link(OWNERS, "flux_owned", "tag_owners"),
    ),
    # `cmd_rotate` calls `tools.owners()`, a field of `RotationTools` whose default is `tree_owners`.
    "`secret_rotation.py --deploy`": (
        Link("scripts/secrets_mgmt/secret_rotation.py", "cmd_rotate", "owners"),
        Link(ROTATION_TOOLS, "RotationTools", "tree_owners", call=False),
        Link(ROTATION_TOOLS, "tree_owners", "tag_owners"),
    ),
    "`probe.py health T`": (
        Link(
            "scripts/diagnostics/probe_lib/health.py", "_with_alert_plane", "tag_owners"
        ),
    ),
    "`gen_role_glance.py`, `service_catalog.py`": (
        Link("scripts/docs/gen_role_glance.py", "stale_k8s_docs", "tag_owners"),
        Link("scripts/docs/service_catalog.py", "build_rows", "tag_owners"),
    ),
}

# The rows whose chain ends somewhere other than a tree reader, each with the reason.
NOT_A_TREE_READER = {
    "`deploy.sh --tags T`": (
        "it refuses by `platform: k8s`, and the `every-k8s-entry-is-flux-owned` census row "
        "holds `owner` on each such entry"
    ),
    "`deploy.sh --changed`": "it leaves out a tag by the platform of its entry",
    "`deploy.sh --list-services`": "it marks a tag by the platform of its entry",
    "GitOps deployer": "it routes every k8s role path to `cs.flux` with no ownership read",
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
    assert len(table_rows(_plan())) >= 9
    assert census_problems(_plan()) == []


def test_a_renamed_entry_point_is_flagged():
    rel = "scripts/secrets_mgmt/secret_rotation.py"
    text = (REPO / rel).read_text().replace("def cmd_rotate(", "def rotate(")
    assert census_problems(_plan(), source=_source_with(rel, text)) == [
        f"`secret_rotation.py --deploy`: {rel} defines no `cmd_rotate`"
    ]


def test_an_entry_point_that_stops_reading_ownership_is_flagged():
    rel = "scripts/diagnostics/probe_lib/health.py"
    text = (
        (REPO / rel)
        .read_text()
        .replace("tag_owners().get(role), _render", "{}.get(role), _render")
    )
    assert census_problems(_plan(), source=_source_with(rel, text)) == [
        f"`probe.py health T`: {rel}:_with_alert_plane no longer calls `tag_owners`"
    ]


def test_a_plan_row_no_member_names_is_flagged():
    plan = _plan().replace(
        "| `secret_rotation.py --deploy` |",
        "| `new_entry.py` | Skips T.\n| `secret_rotation.py --deploy` |",
        1,
    )
    assert census_problems(plan) == [
        f"{PLAN}'s owner table has the row `new_entry.py`, which no member of "
        "ENTRY_POINTS names"
    ]


def test_a_chain_that_stops_short_of_a_reader_is_flagged():
    short = dict(ENTRY_POINTS)
    short["`land.sh`"] = short["`land.sh`"][:1]
    assert census_problems(_plan(), short) == [
        "`land.sh`: the chain ends at `_flux_owned`, which is none of "
        f"{sorted(READERS)}"
    ]


def test_an_owner_map_no_longer_read_from_the_tools_is_flagged():
    """A local named `owners` must not stand in for the `tools.owners()` call."""
    rel = "scripts/secrets_mgmt/secret_rotation.py"
    text = (REPO / rel).read_text().replace("owners = tools.owners()", "owners = {}")
    assert census_problems(_plan(), source=_source_with(rel, text)) == [
        f"`secret_rotation.py --deploy`: {rel}:cmd_rotate no longer calls `owners`"
    ]
