"""The census row that holds a non-root unit reaching the cluster to setting `KUBECONFIG` (#8155).

k3s's kubectl defaults to `/etc/rancher/k3s/k3s.yaml`, which is root-only. A unit running as
another user with no `KUBECONFIG` reads an empty cluster and exits 0, the trap the cron-era rule
in `cron_checks.py` held until #8125 retired it.

"Reaches the cluster" is a closure over what `ExecStart=` runs. A token resolves through the
component's `component.yml` file map, matched on the installed basename (so a `{{ var }}` prefix
needs no rendering), or names a repo script under `scripts/`. From there the row follows a shell
script's `scripts/` references and a Python module's imports and `scripts/` string constants. The
unit reaches the cluster when anything in the closure runs `kubectl` or starts a Claude Code
session, whose agent reads the cluster with kubectl.
"""

import ast
import re
from functools import cache
from pathlib import Path

from _helpers import REPO
from _row_table import Census, Subject, tracked
from lib import yaml_fast

_CLAUDE_CODE_TEMPLATES = "hosts/components/claude-code/templates/*.service*"

_KUBECONFIG_SET = re.compile(
    r"^\s*(?:export\s+)?KUBECONFIG=|^Environment=.*\bKUBECONFIG="
)
_USER = re.compile(r"^User=(.*)$", re.M)
_EXEC_START = re.compile(r"^ExecStart=(.*)$", re.M)
_TOKEN = re.compile(r"[^\s\"']+")
_REPO_SCRIPT = re.compile(r"\bscripts/[\w./-]+\.(?:py|sh)\b")
_SHELL_KUBECTL = re.compile(r"(?:^|[\s/\"'(])kubectl\b")
# A string constant naming the kubectl binary: `kubectl`, a path to it, or `k3s kubectl`.
_KUBECTL_BINARY = re.compile(r"(?:\S*/)?(?:k3s )?kubectl")

# Every directory a repo module's bare import can resolve against, besides its own.
_IMPORT_ROOTS = (
    REPO / "scripts",
    *sorted(p for p in (REPO / "scripts").iterdir() if p.is_dir()),
    REPO / "hosts" / "lib",
)


def _units() -> list[str]:
    return tracked("hosts/components/*/units/*.service*", _CLAUDE_CODE_TEMPLATES)


def _logical_lines(text: str) -> str:
    return re.sub(r"\\\n", " ", text)


def _runs_as_non_root(text: str) -> bool:
    users = _USER.findall(text)
    return bool(users) and users[-1].strip() not in ("", "root", "0")


@cache
def _installed(component: str) -> dict[str, Path]:
    """The component's file map, as installed basename to the source file it ships."""
    comp_dir = REPO / "hosts" / "components" / component
    spec = yaml_fast.safe_load((comp_dir / "component.yml").read_text()) or {}
    sources = {}
    for entry in spec.get("files") or []:
        if "src" in entry:
            sources[Path(entry["dst"]).name] = comp_dir / entry["src"]
        elif "lib" in entry:
            sources[Path(entry["dst"]).name] = REPO / "hosts" / "lib" / entry["lib"]
    return sources


def _exec_targets(rel: str, text: str) -> list[Path]:
    """The files a unit's `ExecStart=` lines run: installed sources and repo scripts."""
    installed = _installed(rel.split("/")[2])
    targets = []
    for command in _EXEC_START.findall(_logical_lines(text)):
        for token in _TOKEN.findall(command):
            name = token.rsplit("/", 1)[-1]
            if "/" in token and name in installed:
                targets.append(installed[name])
        targets += [REPO / m for m in _REPO_SCRIPT.findall(command)]
    return [t for t in targets if t.is_file()]


def _resolve_import(module: str, here: Path) -> Path | None:
    parts = module.split(".")
    for root in (here.parent, *_IMPORT_ROOTS):
        for candidate in (
            root.joinpath(*parts).with_suffix(".py"),
            root.joinpath(*parts, "__init__.py"),
        ):
            if candidate.is_file():
                return candidate
    return None


@cache
def _python_facts(path: Path, runs: bool) -> tuple[bool, tuple[Path, ...]]:
    """Whether a module runs kubectl or Claude Code itself, and the files it leads to.

    A module the unit runs leads to the repo scripts its constants name, as `build_docs.py`
    runs each generator. An imported module leads only to its imports: `lib.exit_codes` names
    every script in a table and runs none of them.
    """
    tree = ast.parse(path.read_text())
    docstrings = {
        id(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    reaches = False
    leads: list[Path] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            leads += filter(None, (_resolve_import(a.name, path) for a in node.names))
        elif isinstance(node, ast.ImportFrom) and node.module and not node.level:
            reaches |= any(a.name == "kubectl_runner" for a in node.names)
            leads += filter(None, [_resolve_import(node.module, path)])
            leads += filter(
                None,
                (_resolve_import(f"{node.module}.{a.name}", path) for a in node.names),
            )
        elif isinstance(node, ast.Attribute) and node.attr == "kubectl_runner":
            reaches = True
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            value = node.value.strip()
            reaches |= bool(_KUBECTL_BINARY.fullmatch(value)) or (
                runs and value == "claude"
            )
            if runs and _REPO_SCRIPT.fullmatch(value):
                leads.append(REPO / value)
    return reaches, tuple(p for p in leads if p.is_file())


@cache
def _shell_facts(path: Path) -> tuple[bool, tuple[Path, ...]]:
    code = [
        line
        for line in path.read_text().splitlines()
        if not line.lstrip().startswith("#")
    ]
    reaches = any(_SHELL_KUBECTL.search(line) for line in code)
    leads = [REPO / m for line in code for m in _REPO_SCRIPT.findall(line)]
    return reaches, tuple(p for p in leads if p.is_file())


@cache
def _reaches_cluster(start: Path) -> bool:
    """Whether `start`, a file a unit runs, or anything it runs or imports reaches the cluster."""
    seen, todo = {start}, [(start, True)]
    while todo:
        path, runs = todo.pop()
        if path.suffix == ".py":
            reaches, leads = _python_facts(path, runs)
        else:
            reaches, leads = _shell_facts(path)
        if reaches:
            return True
        # A shell script runs what it names; a module imports what it leads to.
        todo += [(p, path.suffix != ".py") for p in leads if p not in seen]
        seen.update(leads)
    return False


def _sets_kubeconfig(text: str, wrappers: list[Path]) -> bool:
    lines = text.splitlines()
    for wrapper in wrappers:
        if wrapper.suffix != ".py":
            lines += wrapper.read_text().splitlines()
    return any(_KUBECONFIG_SET.search(line) for line in lines)


def kubeconfig_unset(subject: Subject) -> list[str]:
    """One hit when a non-root unit's `ExecStart` reaches the cluster without `KUBECONFIG`."""
    text = subject.text
    if not _runs_as_non_root(text):
        return []
    targets = _exec_targets(subject.rel, text)
    starts_claude = any(
        token.rsplit("/", 1)[-1] == "claude"
        for command in _EXEC_START.findall(_logical_lines(text))
        for token in _TOKEN.findall(command)
    )
    if not starts_claude and not any(_reaches_cluster(t) for t in targets):
        return []
    if _sets_kubeconfig(text, targets):
        return []
    return ["runs as a non-root user, reaches the cluster, and sets no KUBECONFIG"]


_DOCS_REFRESH = "hosts/components/docs-refresh/units/homelab-docs-refresh.service.j2"
_B2_BUDGET = (
    "hosts/components/k3s-host-checks/units/homelab-b2-budget-listing.service.j2"
)
_CLAUDE_RC = "hosts/components/claude-code/templates/claude-rc.service.j2"
_LANDER = "hosts/components/claude-code/templates/claude-land@.service.j2"
_LOKI_WITNESS = (
    "hosts/components/loki-route-witness/units/homelab-loki-route-witness.service.j2"
)


def _stripped(rel: str) -> Subject:
    """The unit at `rel` with its `KUBECONFIG` assignment removed, kept at its own path."""
    text = (REPO / rel).read_text()
    text = re.sub(r"^Environment=KUBECONFIG=.*\n", "", text, flags=re.M)
    return Subject(rel, re.sub(r" KUBECONFIG=(?:\{\{.*?\}\}|[^\s{])*", "", text))


UNITS_REACHING_THE_CLUSTER_SET_KUBECONFIG = Census(
    name="non-root-units-reaching-the-cluster-set-kubeconfig",
    reason=(
        "k3s's kubectl defaults to /etc/rancher/k3s/k3s.yaml, which only root reads. A unit "
        "running as another user without KUBECONFIG reads an empty cluster and exits 0, so a "
        "check reports green over nothing. Set Environment=KUBECONFIG= to the user's own "
        "kubeconfig copy, or export it in the wrapper the unit runs."
    ),
    files=_units,
    offence=kubeconfig_unset,
    red=(
        _stripped(_DOCS_REFRESH),
        _stripped(_B2_BUDGET),
        _stripped(_CLAUDE_RC),
    ),
    green=(
        Subject(_B2_BUDGET),
        Subject("hosts/components/claude-code/templates/claude-memory-sync.service.j2"),
    ),
    # 79 units and templates when the row landed (2026-11-12).
    min_matches=60,
    must_find=frozenset(
        {
            "hosts/components/infra-map/units/homelab-infra-map.service.j2",
            _DOCS_REFRESH,
            _B2_BUDGET,
            "hosts/components/k3s-host-checks/units/homelab-b2-deletion-accounting.service.j2",
            _CLAUDE_RC,
            "hosts/components/claude-code/templates/claude-bg-resume.service.j2",
            "hosts/components/claude-code/templates/claude-triage@.service.j2",
        }
    ),
    allow={
        _LANDER: (
            "land.py waits on Flux through lib.kubectl, whose find_kubeconfig falls back to the "
            "kubeconfig under HOME, and the unit sets HOME. The unit is on the lander's "
            "approval list, so adding KUBECONFIG waits on the operator (#8207)"
        ),
        _LOKI_WITNESS: (
            "probe.py loki-labels reads Loki over HTTP and runs no kubectl. The row counts it "
            "because probe.py imports every subcommand, some of which reach lib.kubectl"
        ),
    },
)
