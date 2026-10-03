"""Jellyfin's plugin init containers, read out of the RENDERED Deployment.

Every guard over a plugin installer used to split `deployment.yaml.j2`'s text on `- name:
install-...` and `- |`, then assert on lines copied out of the template — `PLUGINS =
Path("/config/data/plugins")`, `{{ jellyfin_k8s_anisync_version }}`. Both halves pinned source
text rather than behaviour: the cut markers broke on a reformat that changed nothing the pod
runs, and a `{{ ... }}` assertion passes on a template whose expression no longer reaches the
container that needs it.

This module hands a guard the script the POD runs: the init container's `command[-1]` out of the
rendered Deployment, plus its module-level constants evaluated. A constant is what an assertion
wants anyway — `PLUGINS == "/config/data/plugins"` is the fact, and the line that spells it is
not.

Derivation is the one property a single render cannot show, because a literal and a templated
expression render identically. `script(name, overrides=...)` renders the role again with a
variable flipped, so a guard proves the pin reaches the container by watching the constant
follow it.
"""

import ast
import re

from _k8s_render import render_role_template, rendered_docs
from lib import yaml_fast

ROLE = "jellyfin"
DEPLOYMENT = "deployment.yaml.j2"

# Every plugin installer the Deployment declares, by container name. Named rather than globbed
# for the reason in .claude/rules/python-layout.md: a census that finds its subjects by pattern
# passes over an empty set the moment they are renamed.
INSTALLERS = (
    "install-ani-sync",
    "install-intro-skipper",
    "install-webhook",
    "install-merge-versions",
    "install-media-cleaner",
)

SWEEP = "sweep-unlisted-plugins"

# Where Jellyfin scans for plugins. Not read from the script — this is the value the scripts are
# checked AGAINST, and taking it from the thing under test would assert nothing.
PLUGIN_ROOT = "/config/data/plugins"


class Unevaluable(Exception):
    """The expression is not one `plugin_constants` knows how to fold to a value."""


def init_containers(overrides: dict | None = None) -> dict[str, dict]:
    """name -> init container, from the rendered jellyfin Deployment.

    With `overrides`, the role is rendered again with those variables laid over the inventory;
    without, the shared tree render is reused.
    """
    if overrides is None:
        docs = [
            doc
            for role, _tpl, doc in rendered_docs()
            if role == ROLE and doc.get("kind") == "Deployment"
        ]
    else:
        docs = [
            doc
            for doc in yaml_fast.safe_load_all(
                render_role_template(ROLE, DEPLOYMENT, overrides)
            )
            if isinstance(doc, dict) and doc.get("kind") == "Deployment"
        ]
    assert docs, "the jellyfin role rendered no Deployment at all"

    containers = {}
    for doc in docs:
        spec = doc["spec"]["template"]["spec"]
        for container in spec.get("initContainers") or []:
            containers[container["name"]] = container
    return containers


def script(name: str, overrides: dict | None = None) -> str:
    """The Python an init container runs, as the pod receives it."""
    containers = init_containers(overrides)
    assert name in containers, (
        f"the rendered jellyfin Deployment declares no {name} init container. "
        f"It has: {sorted(containers)}"
    )
    command = containers[name].get("command") or []
    assert command[:2] == ["python3", "-c"], (
        f"{name} no longer runs its script through `python3 -c`, so there is no script to "
        f"read: {command[:2]}"
    )
    return command[-1]


def _value(node: ast.expr, known: dict[str, object]) -> object:
    """One assignment's right-hand side folded to a value, or `Unevaluable`.

    Four forms cover every constant these installers declare: a literal, a reference to a
    constant above it, string concatenation, and `Path(...)` divided by a name. A `Path` folds to
    its string and `/` to a string join, so `PLUGINS / ("Ani-Sync_" + VERSION)` reads back as the
    directory the installer writes.
    """
    match node:
        case ast.Constant(value=value):
            return value
        case ast.Name(id=name) if name in known:
            return known[name]
        case ast.Tuple(elts=elements) | ast.List(elts=elements):
            return tuple(_value(element, known) for element in elements)
        case ast.BinOp(left=left, op=ast.Add(), right=right):
            return _value(left, known) + _value(right, known)
        case ast.BinOp(left=left, op=ast.Div(), right=right):
            return f"{_value(left, known)}/{_value(right, known)}"
        case ast.Call(func=ast.Name(id="Path"), args=[argument]):
            return _value(argument, known)
    raise Unevaluable(ast.dump(node))


def plugin_constants(installer: str) -> dict[str, object]:
    """The module-level constants of an installer script, by name.

    An assignment this cannot fold — a call, a comprehension — is left out rather than raising:
    the scripts hold plenty of those, and a guard asks only about the handful of constants that
    decide where a plugin lands and which build it is.
    """
    folded: dict[str, object] = {}
    for node in ast.parse(installer).body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [getattr(target, "id", None) for target in node.targets]
        if len(targets) != 1 or targets[0] is None:
            continue
        try:
            folded[targets[0]] = _value(node.value, folded)
        except Unevaluable:
            continue
    return folded


def without_assignment(installer: str, name: str) -> str:
    """The script with its module-level assignment to `name` cut out.

    The red half of a constant assertion, and a perturbation rather than a text pin: the lines
    come from the parse, so nothing here quotes a line of the script.
    """
    lines = installer.splitlines(keepends=True)
    for node in ast.parse(installer).body:
        if isinstance(node, ast.Assign) and name in [
            getattr(target, "id", None) for target in node.targets
        ]:
            del lines[node.lineno - 1 : node.end_lineno]
            return "".join(lines)
    raise AssertionError(f"the script assigns no {name}, so there is nothing to remove")


def compares_against(installer: str, name: str) -> bool:
    """Whether the script compares anything against the constant `name`.

    A pinned checksum that nothing compares is present and inert, which is the state this
    answers for. Asked of the parse, so a reformatted comparison still reads as one.
    """
    return any(
        isinstance(node, ast.Compare)
        and any(
            isinstance(side, ast.Name) and side.id == name
            for side in [node.left, *node.comparators]
        )
        for node in ast.walk(ast.parse(installer))
    )


# Leading dotted version of a string: "10.11.6.-.ani-sync_4.1.0.0.zip" -> "10.11.6",
# "10.11.10ubu2404-ls35" -> "10.11.10".
LEADING_VERSION = re.compile(r"^(\d+(?:\.\d+)*)")


def version_tuple(text: str, what: str) -> tuple[int, ...]:
    """The dotted version `text` starts with, as ints; `what` names `text` in the failure."""
    match = LEADING_VERSION.match(text)
    assert match, f"{what} does not start with a dotted version: {text!r}"
    return tuple(int(part) for part in match.group(1).split("."))


def padded(left: tuple[int, ...], right: tuple[int, ...]) -> tuple[tuple, tuple]:
    """Both tuples zero-padded to the same length.

    Not cosmetic. A plugin declares a four-part targetAbi (`10.11.11.0`) while the image tag
    carries three parts (`10.11.11`), and `(10, 11, 11, 0) <= (10, 11, 11)` is False in Python —
    the longer tuple wins a prefix tie. Comparing them unpadded fails the pin that is correct.
    """
    width = max(len(left), len(right))
    return (
        left + (0,) * (width - len(left)),
        right + (0,) * (width - len(right)),
    )
