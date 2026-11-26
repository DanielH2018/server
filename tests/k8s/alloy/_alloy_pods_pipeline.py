"""The rendered Alloy `loki.process "pods"` pipeline's `stage.replace` and level stages, run in Python.

`ship(container, line)` returns a line as Loki stores it, so a redaction test asserts on what
ships rather than restating an expression. `dropped_by(container, line)` names the
`stage.drop` that discards a line, or None when it ships. `pod_level(line)` returns the `level`
the pipeline attaches to a line. The stages are parsed out of the rendered config: a
stage at the top of the block applies to every container, and one inside a
`stage.match` applies where the selector does. Alloy compiles each expression as RE2; Python's
`re` stands in, which is equivalent for the literals, classes, bounded repeats and
non-capturing groups these stages use.
"""

import re
import warnings
from functools import cache

from _k8s_render import rendered_docs

_STRING = r'"((?:[^"\\]|\\.)*)"'
_STRING_LITERAL = re.compile(_STRING)
_SELECTOR_MATCHER = re.compile(r"(\w+)\s*(=~|!~|!=|=)\s*" + _STRING)


@cache
def alloy_config() -> str:
    for role, _tpl, doc in rendered_docs():
        if role == "loki-homelab" and doc.get("kind") == "ConfigMap":
            return doc["data"]["config.alloy"]
    raise AssertionError(
        "loki-homelab renders no ConfigMap carrying a config.alloy key"
    )


def _unquote(river: str) -> str:
    return re.sub(r"\\(.)", r"\1", river)


def _blocks(body: str) -> list[tuple[str, str]]:
    """The (header, inner text) of each block directly inside `body`, in order.

    The header is the line before the brace, such as `loki.process "pods"`. Braces inside a
    string literal or a `//` comment do not count, so a selector such as `{container=\\"x\\"}`
    or a repeat such as `{32,}` never opens a block.
    """
    blocks, depth, start, header = [], 0, 0, ""
    i = 0
    while i < len(body):
        c = body[i]
        if c == '"':
            literal = _STRING_LITERAL.match(body, i)
            assert literal, f"unterminated string at offset {i}"
            i = literal.end()
            continue
        if body.startswith("//", i):
            eol = body.find("\n", i)
            i = len(body) if eol < 0 else eol
            continue
        if c == "{":
            if depth == 0:
                header, start = body[:i].rsplit("\n", 1)[-1].strip(), i + 1
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                blocks.append((header, body[start:i]))
        i += 1
    return blocks


def _attribute(block: str, name: str) -> str:
    m = re.search(rf"^\s*{name}\s*=\s*{_STRING}", block, re.MULTILINE)
    assert m, f"no {name} attribute in {block!r}"
    return _unquote(m.group(1))


def _pods_block() -> str:
    pods = [
        inner
        for name, inner in _blocks(alloy_config())
        if name == 'loki.process "pods"'
    ]
    assert len(pods) == 1, 'expected exactly one loki.process "pods" block'
    return pods[0]


_LINE_STAGES = ("stage.replace", "stage.drop")


@cache
def _line_stages() -> list[tuple[str, str | None, re.Pattern, str]]:
    """Every (kind, selector or None, expression, replacement or drop reason) in config order."""
    stages = []
    for name, inner in _blocks(_pods_block()):
        if name in _LINE_STAGES:
            scoped = [(name, None, inner)]
        elif name == "stage.match":
            selector = _attribute(inner, "selector")
            scoped = [(n, selector, s) for n, s in _blocks(inner) if n in _LINE_STAGES]
        else:
            continue
        for kind, selector, stage in scoped:
            pattern = re.compile(_attribute(stage, "expression"))
            if kind == "stage.drop":
                stages.append(
                    (kind, selector, pattern, _attribute(stage, "drop_counter_reason"))
                )
                continue
            assert pattern.groups == 1, (
                f"{pattern.pattern!r} must have exactly one capture group; Alloy replaces only it"
            )
            stages.append((kind, selector, pattern, _attribute(stage, "replace")))
    return stages


def _replace_stages() -> list[tuple[str | None, re.Pattern, str]]:
    """Every (selector or None, expression, replacement) in config order."""
    return [
        (sel, pat, rep)
        for kind, sel, pat, rep in _line_stages()
        if kind == "stage.replace"
    ]


def _replace(line: str, pattern: re.Pattern, replacement: str) -> str:
    return pattern.sub(
        lambda m: (
            m.group(0)[: m.start(1) - m.start(0)]
            + replacement
            + m.group(0)[m.end(1) - m.start(0) :]
        ),
        line,
    )


def _selector_applies(selector: str | None, labels: dict[str, str]) -> bool:
    if selector is None:
        return True
    for label, op, value in _SELECTOR_MATCHER.findall(selector):
        actual = labels.get(label, "")
        # LogQL's regex matchers are fully anchored.
        hit = re.fullmatch(value, actual) is not None if "~" in op else value == actual
        if hit == op.startswith("!"):
            return False
    return True


def expressions_for(container: str, namespace: str = "homelab") -> list[str]:
    """The expression of each stage that applies to a line from `container`, in config order."""
    labels = {"container": container, "namespace": namespace}
    return [
        pattern.pattern
        for selector, pattern, _ in _replace_stages()
        if _selector_applies(selector, labels)
    ]


def ship(container: str, line: str, namespace: str = "homelab") -> str:
    """The line as Loki stores it: each applicable stage's capture group replaced."""
    labels = {"container": container, "namespace": namespace}
    for selector, pattern, replacement in _replace_stages():
        if _selector_applies(selector, labels):
            line = _replace(line, pattern, replacement)
    return line


def dropped_by(container: str, line: str, namespace: str = "homelab") -> str | None:
    """The `drop_counter_reason` of the stage that discards `line`, or None when it ships.

    Replace and drop stages run in config order, so a drop sees the line as the replaces before
    it left it.
    """
    labels = {"container": container, "namespace": namespace}
    for kind, selector, pattern, value in _line_stages():
        if not _selector_applies(selector, labels):
            continue
        if kind == "stage.drop":
            if pattern.search(line):
                return value
        else:
            line = _replace(line, pattern, value)
    return None


_LINE_FILTER = re.compile(r'^\{namespace=~"\.\+"\} \|~ "(.*)"$')


@cache
def _level_stages() -> list[tuple[re.Pattern, str]]:
    """Every (line filter, level) of a match stage that sets `level`, in config order."""
    stages = []
    for name, inner in _blocks(_pods_block()):
        if name != "stage.match":
            continue
        for n, stage in _blocks(inner):
            if n == "stage.template" and _attribute(stage, "source") == "level":
                line_filter = _LINE_FILTER.match(_attribute(inner, "selector"))
                assert line_filter, (
                    f"level stage selector {inner!r} is not a line filter"
                )
                # `[[]` is RE2's only backslash-free spelling of a literal `[`; Python warns on it.
                with warnings.catch_warnings():
                    warnings.simplefilter("ignore", FutureWarning)
                    pattern = re.compile(line_filter.group(1))
                stages.append((pattern, _attribute(stage, "template")))
    return stages


def stage_keys(kind: str) -> set[str]:
    """The keys the pods pipeline's top-level `kind` stages name, such as `stage.labels`."""
    keys = set()
    for name, inner in _blocks(_pods_block()):
        if name == kind:
            keys |= set(
                re.findall(r"^\s*(\w+)\s*=", inner.split("{", 1)[1], re.MULTILINE)
            )
    return keys


def pod_level(line: str) -> str | None:
    """The `level` the pipeline attaches to `line`: the last matching stage's, else None."""
    level = None
    for pattern, name in _level_stages():
        if pattern.search(line):
            level = name
    return level
