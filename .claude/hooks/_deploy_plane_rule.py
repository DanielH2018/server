#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by block-footguns.py for its eighth rule
"""The `block-footguns` rule that keeps agents off the Flux deploy plane's writes.

Three verdicts, one per direction a write moves the cluster:

- `tag_move_problem` denies any command that moves an OCI tag. A moved tag is what Flux
  applies, and the promote workflow's `tag` job alone moves `prod`.
- `deploy_plane_variable_ask` asks the operator before a write that releases or hides
  something. Clearing `DEPLOY_HOLD` lets the next `promote` run move `prod`, and a hand-written
  `PROD_DIGEST` hides a moved `prod` from the deploy-pipeline exporter. The operator approves
  the prompt instead of typing the command (contracts.md, C2).
- Setting `DEPLOY_HOLD` to a reason gets no verdict here. It stops promotion, which is the
  safe direction, so an agent may set a hold without asking.

It lives beside `block-footguns.py` rather than in it to keep that file under the module
length cap. `TAG_MOVES` is exported because the hook's binary gate reads it.
"""

from _hook_common import invokes, strip_shell_keywords

HOLD_VARIABLE = "DEPLOY_HOLD"
DIGEST_VARIABLE = "PROD_DIGEST"
# The repository variables the `promote` workflow reads and writes. GitHub stores variable
# names in upper case and matches them without regard to case, so a lower-case spelling is the
# same variable.
DEPLOY_PLANE_VARIABLES = frozenset({HOLD_VARIABLE, DIGEST_VARIABLE})
# The values every hold reader treats as no hold. `promote_lib/reset.py` strips the value and
# reads an empty one as none; the case fold makes the ask err towards asking.
_CLEAR_VALUES = frozenset({"", "none"})
# The `gh api` flags that add a request field. Any of them turns gh's default method from GET
# to POST.
_GH_API_FIELD_FLAGS = ("-f", "-F", "--field", "--raw-field", "--input")
# The commands that move an OCI tag, each as `invokes` matches it.
TAG_MOVES = (("flux", "tag", "artifact"), ("crane", "tag"), ("oras", "tag"))


def _gh_api_method(words: list[str]) -> str:
    """The HTTP method a `gh api` argv sends, upper-cased, as gh resolves it.

    An explicit `-X`/`--method` wins. Without one gh sends POST when the call carries a field,
    and GET otherwise.
    """
    for i, word in enumerate(words):
        if word in ("-X", "--method") and i + 1 < len(words):
            return words[i + 1].upper()
        if word.startswith("--method="):
            return word.partition("=")[2].upper()
        if word.startswith("-X") and len(word) > 2:
            return word[2:].upper()
    has_field = any(
        word in _GH_API_FIELD_FLAGS
        or _attached_field(word) is not None
        or word.startswith("--input=")
        for word in words
    )
    return "POST" if has_field else "GET"


def _attached_field(word: str) -> str | None:
    """The `key=value` a field flag carries in the same word, or None.

    gh's flag parser takes a value attached to the flag as well as one in the next word:
    `-fname=X`, `-Fname=X`, `--field=name=X` and `--raw-field=name=X` all add the field
    `name=X`.
    """
    for prefix in ("--field=", "--raw-field="):
        if word.startswith(prefix):
            return word[len(prefix) :]
    if word[:2] in ("-f", "-F") and len(word) > 2:
        return word[2:]
    return None


def _api_variable(words: list[str]) -> str | None:
    """The deploy-plane variable a `gh api` argv names, or None.

    Two shapes: the variable's own path (`.../actions/variables/DEPLOY_HOLD`, which PATCH and
    DELETE take), and the collection path with a `name=` field (`.../actions/variables -f
    name=DEPLOY_HOLD`, which POST takes to create one). The field may be its own word or
    attached to its flag.
    """
    for word in words:
        upper = word.upper()
        field = (_attached_field(word) or word).upper()
        for name in DEPLOY_PLANE_VARIABLES:
            if f"/VARIABLES/{name}" in upper or field == f"NAME={name}":
                return name
    return None


def _set_body(words: list[str]) -> str | None:
    """The literal value a `gh variable set` argv writes, or None when it cannot be read.

    gh takes the value from `-b`/`--body`, attached (`-bX`, `--body=X`) or as the next word,
    and reads stdin when neither is given. An env file (`-f`/`--env-file`) or a value that
    still holds a shell expansion is unreadable too, so all three return None.
    """
    body = None
    for i, word in enumerate(words):
        if word in ("-f", "--env-file") or word.startswith("--env-file="):
            return None
        if word in ("-b", "--body") and i + 1 < len(words):
            body = words[i + 1]
        elif word.startswith("--body="):
            body = word.partition("=")[2]
        elif word.startswith("-b") and len(word) > 2:
            body = word[2:]
    if body is None or "$" in body or "`" in body:
        return None
    return body


def tag_move_problem(stage: list[str]) -> str | None:
    """A command that moves an OCI tag (`flux tag artifact`, `crane tag`, `oras tag`), or None.

    A `flux push artifact` straight to `:prod` passes this textual check. The boundary is that
    no agent token carries `write:packages` (`docs/flux-deploy-plane.md`); this rule is the
    second layer.
    """
    words = strip_shell_keywords(stage)
    for prefix in TAG_MOVES:
        if invokes(words, prefix):
            return (
                f"`{' '.join(prefix)}` moves an OCI tag. The cluster follows the `prod` tag of "
                "homelab-manifests, and only the promote workflow's `tag` job moves it, after "
                "`promote.py decide` picks the commit. To stop promotion, set DEPLOY_HOLD: "
                "`gh variable set DEPLOY_HOLD --body <reason> -R DanielH2018/server`."
            )
    return None


def _variable_write(words: list[str]) -> tuple[str, str | None] | None:
    """The deploy-plane variable a stage writes and the literal value, or None for no write.

    A delete writes the empty value, which every hold reader treats as no hold. The value is
    None for a `gh api` write and for a `gh variable set` body this check cannot read.
    """
    is_set = invokes(words, ("gh", "variable", "set"))
    if is_set or invokes(words, ("gh", "variable", "delete")):
        name = next(
            (w.upper() for w in words[1:] if w.upper() in DEPLOY_PLANE_VARIABLES), None
        )
        if name is None:
            return None
        return name, _set_body(words) if is_set else ""
    if invokes(words, ("gh", "api")) and _gh_api_method(words) not in ("GET", "HEAD"):
        name = _api_variable(words)
        return (name, None) if name else None
    return None


def deploy_plane_variable_ask(stage: list[str]) -> str | None:
    """Why the operator must approve this deploy-plane variable write, or None.

    Asks before any `PROD_DIGEST` write, and before a `DEPLOY_HOLD` write that clears the
    hold or whose value this check cannot read: `--body none`, an empty body, a delete, a
    `gh api` write, stdin, an env file or a shell expansion. A `gh variable set DEPLOY_HOLD`
    with a literal reason returns None, as do reads.

    A textual guard breaks on an indirection: a variable name held in a shell variable, an
    env file that names the variable, and the web UI all pass it.
    """
    found = _variable_write(strip_shell_keywords(stage))
    if found is None:
        return None
    name, value = found
    if name == DIGEST_VARIABLE:
        return (
            "PROD_DIGEST is written by the promote workflow's `tag` job alone, and a hand "
            "write hides a moved `prod` from the deploy-pipeline exporter. Approve it only to "
            "repair the variable to the digest `prod` names."
        )
    if value is not None and value.strip().lower() not in _CLEAR_VALUES:
        return None
    what = (
        "clears DEPLOY_HOLD"
        if value is not None
        else "may clear DEPLOY_HOLD (this check cannot read the value it writes)"
    )
    return (
        f"This command {what}, which lets the next `promote` run move `prod` and erases the "
        "SHA and names the hold recorded (contracts.md C2). Approve it once the held commit "
        "is fixed or reverted: `gh variable get DEPLOY_HOLD -R DanielH2018/server` shows the "
        "hold, and `probe.py landing` its state."
    )
