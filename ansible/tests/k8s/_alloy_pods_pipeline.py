"""The rendered Alloy `loki.process "pods"` pipeline's `stage.replace` stages, run in Python.

`ship(container, line)` returns a line as Loki stores it, so a redaction test asserts on what
ships rather than restating an expression. The stages are parsed out of the rendered config: a
`stage.replace` at the top of the block applies to every container, and one inside a
`stage.match` applies where the selector does. Alloy compiles each expression as RE2; Python's
`re` stands in, which is equivalent for the literals, classes, bounded repeats and
non-capturing groups these stages use.
"""

import re
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


@cache
def _replace_stages() -> list[tuple[str | None, re.Pattern, str]]:
    """Every (selector or None, expression, replacement) in config order."""
    pods = [
        inner
        for name, inner in _blocks(alloy_config())
        if name == 'loki.process "pods"'
    ]
    assert len(pods) == 1, 'expected exactly one loki.process "pods" block'
    stages = []
    for name, inner in _blocks(pods[0]):
        if name == "stage.replace":
            scoped = [(None, inner)]
        elif name == "stage.match":
            selector = _attribute(inner, "selector")
            scoped = [(selector, s) for n, s in _blocks(inner) if n == "stage.replace"]
        else:
            continue
        for selector, stage in scoped:
            pattern = re.compile(_attribute(stage, "expression"))
            assert pattern.groups == 1, (
                f"{pattern.pattern!r} must have exactly one capture group; Alloy replaces only it"
            )
            stages.append((selector, pattern, _attribute(stage, "replace")))
    return stages


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


def ship(container: str, line: str, namespace: str = "homelab") -> str:
    """The line as Loki stores it: each applicable stage's capture group replaced."""
    labels = {"container": container, "namespace": namespace}
    for selector, pattern, replacement in _replace_stages():
        if _selector_applies(selector, labels):
            line = pattern.sub(
                lambda m, r=replacement: (
                    m.group(0)[: m.start(1) - m.start(0)]
                    + r
                    + m.group(0)[m.end(1) - m.start(0) :]
                ),
                line,
            )
    return line
