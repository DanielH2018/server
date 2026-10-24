"""A small CEL evaluator for the subset the `system-upgrade` health expressions use.

The repo has no cel-go or CEL library, and Flux evaluates `healthCheckExprs` inside
kustomize-controller, so no test here runs Flux's own evaluator. This module evaluates the
operators and functions the three Plan expressions use and raises on anything else, so an
expression that reaches for another CEL feature fails the test instead of passing unread.

Supported: string, integer and boolean literals, `&&`, `||`, `!`, `==`, `!=`, `>`, parentheses, field
selection, `has(a.b)`, `size(x)` and the `exists(var, predicate)` macro. A field that is
absent raises `CelError` (CEL's "no such key") unless it sits inside `has()`, which reads
false. Both `&&` and `||` short-circuit left to right, as CEL's do for a left operand that
decides the result.

Run: uv run pytest ansible/roles/k8s/system-upgrade/tests/test_plan_health_exprs.py
"""

import re

_TOKEN = re.compile(
    r"\s*(?:(?P<str>'[^']*')|(?P<num>\d+)|(?P<op>&&|\|\||!=|==|[!>().,])|(?P<id>[A-Za-z_]\w*))"
)


class CelError(Exception):
    """An expression used a feature outside the supported subset, or read an absent field."""


def _tokens(text: str) -> list[tuple[str, str]]:
    out, pos = [], 0
    text = text.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match:
            raise CelError(f"cannot tokenize {text[pos:]!r}")
        kind = match.lastgroup or ""
        out.append((kind, match.group(kind)))
        pos = match.end()
    return out


class _Parser:
    def __init__(self, text: str):
        self.toks = _tokens(text)
        self.i = 0

    def peek(self) -> tuple[str, str]:
        return self.toks[self.i] if self.i < len(self.toks) else ("end", "")

    def take(self, value: str | None = None) -> tuple[str, str]:
        tok = self.peek()
        if value is not None and tok[1] != value:
            raise CelError(f"expected {value!r}, got {tok[1]!r}")
        self.i += 1
        return tok

    def parse(self):
        node = self.or_()
        if self.peek()[0] != "end":
            raise CelError(f"trailing input at {self.peek()[1]!r}")
        return node

    def or_(self):
        node = self.and_()
        while self.peek()[1] == "||":
            self.take()
            node = ("or", node, self.and_())
        return node

    def and_(self):
        node = self.rel()
        while self.peek()[1] == "&&":
            self.take()
            node = ("and", node, self.rel())
        return node

    def rel(self):
        node = self.unary()
        if self.peek()[1] in ("==", "!=", ">"):
            op = self.take()[1]
            node = (op, node, self.unary())
        return node

    def unary(self):
        if self.peek()[1] == "!":
            self.take()
            return ("not", self.unary())
        return self.postfix()

    def postfix(self):
        node = self.primary()
        while self.peek()[1] == ".":
            self.take()
            name = self.take()[1]
            if self.peek()[1] == "(":
                self.take()
                args = self.args()
                node = ("method", name, node, args)
            else:
                node = ("select", node, name)
        return node

    def args(self) -> list:
        out = []
        while self.peek()[1] != ")":
            out.append(self.or_())
            if self.peek()[1] == ",":
                self.take()
        self.take(")")
        return out

    def primary(self):
        kind, value = self.take()
        if kind == "str":
            return ("lit", value[1:-1])
        if kind == "num":
            return ("lit", int(value))
        if kind == "id":
            if value in ("true", "false"):
                return ("lit", value == "true")
            if self.peek()[1] == "(":
                self.take()
                return ("call", value, self.args())
            return ("var", value)
        if value == "(":
            node = self.or_()
            self.take(")")
            return node
        raise CelError(f"unexpected {value!r}")


def _eval(node, env: dict):
    kind = node[0]
    if kind == "lit":
        return node[1]
    if kind == "var":
        if node[1] not in env:
            raise CelError(f"no such attribute {node[1]!r}")
        return env[node[1]]
    if kind == "select":
        base = _eval(node[1], env)
        if not isinstance(base, dict) or node[2] not in base:
            raise CelError(f"no such key {node[2]!r}")
        return base[node[2]]
    if kind == "not":
        return not _eval(node[1], env)
    if kind == "and":
        return bool(_eval(node[1], env)) and bool(_eval(node[2], env))
    if kind == "or":
        return bool(_eval(node[1], env)) or bool(_eval(node[2], env))
    if kind in ("==", "!=", ">"):
        left, right = _eval(node[1], env), _eval(node[2], env)
        return {"==": left == right, "!=": left != right, ">": left > right}[kind]
    if kind == "call":
        name, args = node[1], node[2]
        if name == "has" and len(args) == 1 and args[0][0] == "select":
            try:
                _eval(args[0], env)
            except CelError:
                return False
            return True
        if name == "size" and len(args) == 1:
            return len(_eval(args[0], env))
        raise CelError(f"unsupported function {name!r}")
    if kind == "method":
        _, name, target, args = node
        if name == "exists" and len(args) == 2 and args[0][0] == "var":
            items = _eval(target, env)
            return any(
                bool(_eval(args[1], {**env, args[0][1]: item})) for item in items
            )
        raise CelError(f"unsupported method {name!r}")
    raise CelError(f"unsupported node {kind!r}")


def evaluate(expression: str, obj: dict) -> bool:
    """Evaluate `expression` with each top-level key of `obj` as a variable.

    Raises:
        CelError: The expression uses a feature this module does not implement, reads a field
            that is absent outside `has()`, or is not a boolean.
    """
    result = _eval(_Parser(expression).parse(), dict(obj))
    if not isinstance(result, bool):
        raise CelError(f"{expression!r} evaluated to {result!r}, not a boolean")
    return result
