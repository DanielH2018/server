"""The census row holding every literal path a prek hook's `files` regex names to the tree."""

import tomllib

from _row_table import Census, Subject, tracked

# Stand-ins for `^` and `$` inside an expansion; neither can appear in a tracked path.
_START, _END = "\0", "\1"
# A gate that exists to fire on a path the tree must never track.
_NEVER_TRACKED = frozenset({"__pycache__/"})

_CUT = ("cut", "")


def _parse(regex: str, i: int = 0) -> tuple[list[list[tuple]], int]:
    """The branches of `regex` from `i` up to an unmatched `)`, as (kind, value) atoms.

    A class, `.`, a letter escape and any quantified atom become a `cut`: the expansion
    stops there, so only the literal text before it counts.
    """
    branches: list[list[tuple]] = []
    seq: list[tuple] = []
    while i < len(regex) and regex[i] != ")":
        c = regex[i]
        if c == "|":
            branches.append(seq)
            seq = []
            i += 1
            continue
        if c == "(":
            opener = next(
                o for o in ("(?:", "(?<", "(?", "(") if regex.startswith(o, i)
            )
            inner, i = _parse(regex, i + len(opener) + (opener == "(?<"))
            atom, i = (("group", inner) if opener in ("(", "(?:") else _CUT), i + 1
        elif c == "\\":
            nxt = regex[i + 1]
            atom, i = (_CUT if nxt.isalnum() else ("lit", nxt)), i + 2
        elif c == "[":
            atom, i = _CUT, regex.index("]", i + 2) + 1
        elif c in "^$":
            atom, i = ("lit", _START if c == "^" else _END), i + 1
        else:
            atom, i = (_CUT if c == "." else ("lit", c)), i + 1
        if i < len(regex) and regex[i] in "*+?{":
            atom = _CUT
            i = (regex.index("}", i) if regex[i] == "{" else i) + 1
            i += i < len(regex) and regex[i] in "?+"
        seq.append(atom)
    branches.append(seq)
    return branches, i


def _ways(branches: list[list[tuple]]) -> set[tuple[str, bool]]:
    """(text, cut) for each way through the branches: `cut` once a non-literal atom stops it."""
    found = set()
    for seq in branches:
        ways = {("", False)}
        for kind, value in seq:
            alts = (
                {(value, False)}
                if kind == "lit"
                else _ways(value)
                if kind == "group"
                else {("", True)}
            )
            ways = {
                (text, True) if cut else (text + alt, alt_cut)
                for text, cut in ways
                for alt, alt_cut in ((("", False),) if cut else alts)
            }
        found |= ways
    return found


def literal_paths(regex: str) -> list[tuple[str, bool]]:
    """(path, exact) for each start-anchored literal in `regex`; not exact means a prefix."""
    found = []
    for text, cut in _ways(_parse(regex)[0]):
        if not text.startswith(_START):
            continue
        body = text[1:]
        exact = not cut and body.endswith(_END)
        body = body.removesuffix(_END)
        if body and _START not in body and _END not in body:
            found.append((body, exact))
    return sorted(found)


def _hooks(subject: Subject) -> list[dict]:
    config = tomllib.loads(subject.text)
    return [hook for repo in config["repos"] for hook in repo.get("hooks", [])]


def _unresolved(subject: Subject) -> list[str]:
    paths = tracked()
    names = set(paths)
    return [
        f"hook {hook['id']}: `{path}` matches no tracked "
        + ("file" if exact else "path prefix")
        for hook in _hooks(subject)
        for path, exact in literal_paths(hook.get("files", ""))
        if path not in _NEVER_TRACKED
        and not (path in names if exact else any(p.startswith(path) for p in paths))
    ]


_HOOK = '[[repos]]\nrepo = "local"\n[[repos.hooks]]\nid = "x"\nfiles = "{}"\n'

PREK_FILES_PATHS_RESOLVE = Census(
    name="prek-files-regex-paths-resolve",
    reason=(
        "A literal path in a prek hook's `files` regex that no longer resolves matches "
        "nothing, so the hook stops firing for the file it was written for and no check says "
        "so (#8163). Drop the path, or point it at the file that replaced it."
    ),
    files=lambda: ["prek.toml"],
    offence=_unresolved,
    red=(
        Subject(
            "prek.toml", _HOOK.format("^(pyproject\\\\.toml|ansible/deploy\\\\.yml)$")
        ),
        Subject("prek.toml", _HOOK.format("^scripts/no_such_dir/.*\\\\.py$")),
        Subject("prek.toml", _HOOK.format("(\\\\.py$|^scripts/(lib|nope)/)")),
    ),
    green=(
        Subject("prek.toml", _HOOK.format("^(pyproject\\\\.toml|uv\\\\.lock)$")),
        Subject("prek.toml", _HOOK.format("(\\\\.py$|(^|/)CLAUDE\\\\.md$)")),
        Subject("prek.toml", _HOOK.format("^k8s/roles/[^/]+/templates/")),
        Subject("prek.toml", _HOOK.format("^(?!.*\\\\.md$)(?:no_such_dir/x)$")),
    ),
    count=lambda subject: sum(
        len(literal_paths(hook.get("files", ""))) for hook in _hooks(subject)
    ),
    must_find=frozenset({"prek.toml"}),
)
