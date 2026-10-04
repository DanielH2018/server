"""Two importer edges `narrow_broad.template_importers` must not take at face value.

THE PROBLEM (#3445). A change to `ansible/templates/claim-default.yaml.j2` narrowed to 57 of 60
services and fell back to the whole play, though only freshrss and zigbee2mqtt render it. Two
edges carried the fleet, and both were text the grep matched rather than a render:

  - A name inside a Jinja comment. `pvc.yml.j2`'s header names `claim-default.yaml.j2`, and
    `container-resources.yml.j2`'s names `pvc.yml.j2`, so the scan walked from the claim
    template to every role that sets container resources. `real_mentions` drops a `.j2` hit
    whose only mention sits inside `{# ... #}`.
  - The claim template's one renderer. `k8s/manifests` renders it once per entry of the
    CALLER's `k8s_claims`, so a hit in `manifests` reaches the roles that declare that key, not
    every caller of `manifests`. `rendering_roles` swaps the one for the other.

Both refuse on doubt, like every rule `narrow_broad` applies: a template carrying `{% raw %}`
keeps every hit, and a `k8s_claims` mention anywhere but a role's `defaults/main.yml`, the
renderer's own tasks or a comment raises `CannotNarrow`.

A third rule (#3459) asks whether the changed template renders differently at all.
`comment_only` lexes it at both refs with Jinja's own lexer and compares the token streams
less their comments, so an edit to a `{# ... #}` comment reaches no render and narrows to
nothing. It refuses on doubt too: a lex error, a `#jinja2:` header or a stream that differs
under either `trim_blocks` setting keeps the importer mapping.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[1]))

import re
from collections.abc import Callable, Iterable
from pathlib import Path

from lib.git import git
from lib.narrow_git import CannotNarrow, show_at

_JINJA_COMMENT = re.compile(r"\{#.*?#\}", re.DOTALL)
# A `{# ` inside raw text is literal, and stripping from it could swallow a real import line,
# so a template that holds a raw block keeps its hit.
_JINJA_RAW = re.compile(r"\{%-?\s*raw\s*-?%\}")
_YAML_COMMENT_LINE = re.compile(r"^\s*#.*$", re.MULTILINE)

# The shared template `k8s/manifests` renders per entry of a caller's `k8s_claims`, the key,
# and the role that renders it. `test_narrow_claim_template.py` pins the renderer's use of the
# template to that one gated task.
CLAIM_TEMPLATE = "claim-default.yaml.j2"
CLAIM_KEY = "k8s_claims"
CLAIM_RENDERER = "manifests"
_RENDERER_TASKS = f"ansible/roles/k8s/{CLAIM_RENDERER}/tasks/"
_DECLARATION = re.compile(
    r"^ansible/roles/(?:k8s|containers)/([^/]+)/defaults/main\.ya?ml$"
)


def _outside_comments(text: str | None, word: str) -> bool:
    """Whether `text`, a Jinja template, names `word` anywhere but a Jinja comment."""
    if text is None or _JINJA_RAW.search(text):
        return True
    return word in _JINJA_COMMENT.sub("", text)


def _render_tokens(text: str, trim_blocks: bool) -> list[tuple[str, str]]:
    """`text`'s Jinja token stream, less its comments, with adjacent data joined.

    Line numbers are dropped, since a line added inside a comment shifts every later one.
    Whitespace control on a comment survives the drop: the lexer has already stripped it from
    the neighbouring data tokens.
    """
    import jinja2

    tokens: list[tuple[str, str]] = []
    env = jinja2.Environment(trim_blocks=trim_blocks)
    for _line, kind, value in env.lex(text):
        if kind.startswith("comment"):
            continue
        if kind == "data" and tokens and tokens[-1][0] == "data":
            tokens[-1] = ("data", tokens[-1][1] + value)
        else:
            tokens.append((kind, value))
    return tokens


def comment_only(before: str | None, after: str | None) -> bool:
    """Whether a template changed from `before` to `after` only inside Jinja comments.

    Compared under both `trim_blocks` settings, because the lexer applies it to a comment's
    end as well, and the importers' render settings are not read here. False whenever either
    side is missing, cannot be lexed, or opens with a `#jinja2:` header that overrides them.
    False for identical text too: no edit is not a comment edit, and a caller asking what an
    unchanged template reaches wants its importers.
    """
    if before is None or after is None or before == after:
        return False
    if before.startswith("#jinja2:") or after.startswith("#jinja2:"):
        return False
    try:
        import jinja2
    except ImportError:
        return False
    try:
        return all(
            _render_tokens(before, trim) == _render_tokens(after, trim)
            for trim in (False, True)
        )
    except jinja2.TemplateSyntaxError:
        return False


def real_mentions(hits: Iterable[str], name: str, ref: str, cwd: Path) -> list[str]:
    """`hits` less every `.j2` file that names `name` only inside a Jinja comment.

    A YAML file keeps its hit: a `#` line inside a block scalar is content, not a comment.
    """
    return [
        path
        for path in hits
        if not path.endswith(".j2") or _outside_comments(show_at(ref, path, cwd), name)
    ]


def claim_declarers(ref: str, cwd: Path) -> set[str]:
    """The roles whose `defaults/main.yml` mentions `k8s_claims` at `ref`.

    Raises:
        CannotNarrow: the key is mentioned somewhere it could be SET for a role other than in
            its defaults — an include's `vars:`, a `set_fact`, the inventory, the play.
    """
    r = git(
        "grep",
        "-l",
        "-w",
        "-F",
        "-e",
        CLAIM_KEY,
        ref,
        "--",
        "ansible",
        ":!ansible/tests",
        cwd=cwd,
        check=False,
    )
    if r.returncode > 1:
        raise CannotNarrow(f"`git grep {CLAIM_KEY}` failed: {r.stderr.strip()}")
    declarers: set[str] = set()
    for line in r.stdout.splitlines():
        path = line.split(":", 1)[1]
        declared = _DECLARATION.match(path)
        if declared:
            declarers.add(declared.group(1))
        elif (
            path.endswith(".md")
            or "/tests/" in path
            or path.startswith(_RENDERER_TASKS)
        ):
            continue
        elif not _mentioned_in_code(show_at(ref, path, cwd), path):
            continue
        else:
            raise CannotNarrow(
                f"{CLAIM_KEY} is mentioned in {path}, which could set it"
            )
    return declarers


def _mentioned_in_code(text: str | None, path: str) -> bool:
    """Whether `text` names `k8s_claims` outside its comments; True when unreadable."""
    if text is None:
        return True
    if path.endswith(".j2"):
        return _outside_comments(text, CLAIM_KEY)
    return re.search(rf"\b{CLAIM_KEY}\b", _YAML_COMMENT_LINE.sub("", text)) is not None


def rendering_roles(
    name: str, roles: set[str], ref: str, cwd: Path, explain: Callable[[str], None]
) -> set[str]:
    """`roles`, with the claim template's renderer replaced by the roles it renders it for."""
    if name != CLAIM_TEMPLATE or CLAIM_RENDERER not in roles:
        return set(roles)
    declarers = claim_declarers(ref, cwd)
    explain(
        f"narrow: {name} -> {CLAIM_RENDERER} renders it per {CLAIM_KEY}, declared by "
        f"{','.join(sorted(declarers)) or '(nothing)'}"
    )
    return (roles - {CLAIM_RENDERER}) | declarers
