#!/usr/bin/env python3
# gen-hooks: library
#   reason: imported by block-footguns.py for its eighth rule
"""The `block-footguns` rule that keeps agents off the Flux deploy plane's writes.

`deploy_plane_write_problem` denies a write to the repository variable `DEPLOY_HOLD` or
`PROD_DIGEST`, and any command that moves an OCI tag. Each succeeds when typed: a cleared hold
lets the next `promote` run move the cluster's `prod` tag, a hand-written `PROD_DIGEST` hides a
moved `prod` from the deploy-pipeline exporter, and a moved tag is what Flux applies. Clearing
a hold is operator-only (docs/target-architecture/contracts.md, C2), and the promote workflow's
`tag` job alone moves `prod`.

It lives beside `block-footguns.py` rather than in it to keep that file under the module
length cap. `TAG_MOVES` is exported because the hook's binary gate reads it.
"""

from _hook_common import invokes, strip_shell_keywords

# The repository variables the `promote` workflow reads and writes. GitHub stores variable
# names in upper case and matches them without regard to case, so a lower-case spelling is the
# same variable.
DEPLOY_PLANE_VARIABLES = frozenset({"DEPLOY_HOLD", "PROD_DIGEST"})
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
        or any(word.startswith(f"{flag}=") for flag in _GH_API_FIELD_FLAGS[2:])
        for word in words
    )
    return "POST" if has_field else "GET"


def _api_variable(words: list[str]) -> str | None:
    """The deploy-plane variable a `gh api` argv names, or None.

    Two shapes: the variable's own path (`.../actions/variables/DEPLOY_HOLD`, which PATCH and
    DELETE take), and the collection path with a `name=` field (`.../actions/variables -f
    name=DEPLOY_HOLD`, which POST takes to create one).
    """
    for word in words:
        upper = word.upper()
        for name in DEPLOY_PLANE_VARIABLES:
            if f"/VARIABLES/{name}" in upper or upper == f"NAME={name}":
                return name
    return None


def deploy_plane_write_problem(stage: list[str]) -> str | None:
    """A write to `DEPLOY_HOLD` or `PROD_DIGEST`, or a command that moves an OCI tag.

    Denies `gh variable set` and `gh variable delete` on either variable, a `gh api` call with
    a write method on either, `flux tag artifact`, `crane tag` and `oras tag`. A read
    (`gh variable get`, a GET `gh api`) stays clean.

    A textual guard breaks on an indirection: `gh variable set -f vars.env`, a variable name
    held in a shell variable, a `flux push artifact` straight to `:prod`, and the web UI all
    pass it. The boundary is that no agent token carries `write:packages`
    (`docs/flux-deploy-plane.md`); this rule is the second layer.
    """
    words = strip_shell_keywords(stage)
    for prefix in TAG_MOVES:
        if invokes(words, prefix):
            return (
                f"`{' '.join(prefix)}` moves an OCI tag. The cluster follows the `prod` tag of "
                "homelab-manifests, and only the promote workflow's `tag` job moves it, after "
                "`promote.py decide` picks the commit. To stop promotion, ask the operator to "
                "set DEPLOY_HOLD."
            )
    name = None
    if invokes(words, ("gh", "variable", "set")) or invokes(
        words, ("gh", "variable", "delete")
    ):
        name = next(
            (w.upper() for w in words[1:] if w.upper() in DEPLOY_PLANE_VARIABLES), None
        )
    elif invokes(words, ("gh", "api")) and _gh_api_method(words) not in ("GET", "HEAD"):
        name = _api_variable(words)
    if name is None:
        return None
    return (
        f"Writing the repository variable {name} is operator-only. DEPLOY_HOLD stops "
        "promotion and clearing it is the operator's call (contracts.md C2). PROD_DIGEST is "
        "written by the promote workflow's `tag` job alone, and a hand write hides a moved "
        f"`prod` from the exporter. Reading it stays allowed: `gh variable get {name} -R "
        "DanielH2018/server`. Ask the operator to make the change."
    )
