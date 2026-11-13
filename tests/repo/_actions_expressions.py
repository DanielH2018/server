"""A small GitHub Actions expression evaluator, and the runner's `$GITHUB_OUTPUT` parser.

`test_host_package_cases_run_on_their_inputs.py` walks the `host_packages` job's steps with
these: `condition` decides a step's `if:`, `interpolate` resolves `${{ }}` in its `env:` and
`run:`, and `read_outputs` turns what a step wrote into `steps.<id>.outputs`. The evaluator
covers literals, context paths, `! == != && ||`, parentheses and the status functions, and
raises on any form outside that subset rather than reading it as false.
"""

import re


_TOKEN = re.compile(
    r"\s*(?:(?P<str>'(?:[^']|'')*')|(?P<num>\d+(?:\.\d+)?)"
    r"|(?P<op>==|!=|&&|\|\||!|\(|\)|,)|(?P<name>[A-Za-z_][A-Za-z0-9_.\-*]*))"
)
_STATUS = {"always": True, "success": True, "failure": False, "cancelled": False}
_STRING_FUNCTIONS = {
    "contains": lambda haystack, needle: str(needle).lower() in str(haystack).lower(),
    "startsWith": lambda text, prefix: (
        str(text).lower().startswith(str(prefix).lower())
    ),
    "endsWith": lambda text, suffix: str(text).lower().endswith(str(suffix).lower()),
}


def _tokens(text: str) -> list[tuple[str, str]]:
    tokens, pos = [], 0
    text = text.strip()
    while pos < len(text):
        match = _TOKEN.match(text, pos)
        if not match or match.end() == pos:
            raise ValueError(f"cannot tokenise {text[pos:]!r} in {text!r}")
        kind = match.lastgroup
        assert kind is not None
        tokens.append((kind, match.group(kind)))
        pos = match.end()
    return tokens


def _truthy(value: object) -> bool:
    return value not in (None, False, "", 0)


def evaluate(expression: str, context: dict) -> object:
    """Evaluate a GitHub expression against `context`; raise on any form outside the subset."""
    tokens = _tokens(expression)
    pos = 0

    def peek() -> tuple[str, str] | None:
        return tokens[pos] if pos < len(tokens) else None

    def take() -> tuple[str, str]:
        nonlocal pos
        pos += 1
        return tokens[pos - 1]

    def lookup(path: str) -> object:
        root, *rest = path.split(".")
        if root not in context:
            raise ValueError(f"unknown context {root!r} in {expression!r}")
        value: object = context[root]
        for key in rest:
            value = value.get(key, "") if isinstance(value, dict) else ""
        return value

    def primary() -> object:
        kind, text = take()
        if kind == "op" and text == "!":
            return not _truthy(primary())
        if kind == "op" and text == "(":
            value = disjunction()
            assert take() == ("op", ")"), expression
            return value
        if kind == "str":
            return text[1:-1].replace("''", "'")
        if kind == "num":
            return float(text)
        if kind == "name":
            if text in ("true", "false"):
                return text == "true"
            if text == "null":
                return None
            if peek() == ("op", "("):
                take()
                args = []
                while peek() != ("op", ")"):
                    args.append(disjunction())
                    if peek() == ("op", ","):
                        take()
                take()
                if text in _STATUS and not args:
                    return _STATUS[text]
                if text in _STRING_FUNCTIONS and len(args) == 2:
                    return _STRING_FUNCTIONS[text](*args)
                raise ValueError(f"unsupported function {text}() in {expression!r}")
            return lookup(text)
        raise ValueError(f"unexpected {text!r} in {expression!r}")

    def comparison() -> object:
        left = primary()
        while peek() in (("op", "=="), ("op", "!=")):
            _, op = take()
            right = primary()
            same = str(left).lower() == str(right).lower()
            left = same if op == "==" else not same
        return left

    def conjunction() -> object:
        left = comparison()
        while peek() == ("op", "&&"):
            take()
            right = comparison()
            left = right if _truthy(left) else left
        return left

    def disjunction() -> object:
        left = conjunction()
        while peek() == ("op", "||"):
            take()
            right = conjunction()
            left = left if _truthy(left) else right
        return left

    value = disjunction()
    if pos != len(tokens):
        raise ValueError(f"trailing tokens in {expression!r}")
    return value


def condition(step: dict, context: dict) -> bool:
    if "if" not in step:
        return True
    text = str(step["if"]).strip()
    if text.startswith("${{") and text.endswith("}}"):
        text = text[3:-2]
    return _truthy(evaluate(text, context))


def interpolate(value: object, context: dict) -> str:
    def one(match: re.Match[str]) -> str:
        result = evaluate(match.group(1), context)
        if isinstance(result, bool):
            return "true" if result else "false"
        return "" if result is None else str(result)

    return re.sub(r"\$\{\{(.*?)\}\}", one, str(value))


def read_outputs(text: str) -> dict[str, str]:
    """`$GITHUB_OUTPUT` as the runner parses it: `k=v` lines and `k<<DELIM` blocks."""
    outputs: dict[str, str] = {}
    lines = iter(text.splitlines())
    for line in lines:
        if "<<" in line and ("=" not in line or line.index("<<") < line.index("=")):
            key, delimiter = line.split("<<", 1)
            body = []
            for inner in lines:
                if inner == delimiter:
                    break
                body.append(inner)
            outputs[key] = "\n".join(body)
        elif "=" in line:
            key, value = line.split("=", 1)
            outputs[key] = value
    return outputs
