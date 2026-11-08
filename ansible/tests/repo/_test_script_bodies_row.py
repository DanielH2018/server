"""The census row that reads the scripts a test writes and runs, which the shipped-script row skips.

`scripts-run-no-shimmed-binary-by-hardcoded-path` reads only scripts outside `tests/`. A test
can still write a script body to disk and run it, and leakguard's `Popen` wrapper sees only that
script, never the `/usr/local/bin/k3s` it execs (#7663). This row reads those bodies. In a test-tree Python
module a body is a string literal that opens with a shebang, follows a shell's `-c` in an argv
literal, or is passed to `.write_text()`. Every shell script or tracked executable under a
`tests/` directory is one body, read whole. A test module's own docstrings and asserts name
shimmed paths as text, so the rest of the module is left out. Each body is read for a
hardcoded shimmed path, a PATH prepend and a PATH reset.
"""

import ast
import re
from functools import cache

from _row_table import Census, Subject, tracked
from _shimmed_path_row import (
    _SCRIPT_SUFFIXES,
    _executables,
    hardcoded_shimmed_paths,
    prepended_paths,
    reset_paths,
)

# Each predicate reads one script body.
_BODY_OFFENCES = (hardcoded_shimmed_paths, prepended_paths, reset_paths)

_SHELLS = frozenset({"bash", "sh", "dash", "zsh"})
_COMMAND_FLAG = re.compile(r"-[a-z]*c")  # -c, -ec, -euc


def _in_tests(rel: str) -> bool:
    return "tests" in rel.split("/")[:-1]


@cache
def _tracked_executables() -> frozenset[str]:
    return _executables()


def _whole_file_script(rel: str) -> bool:
    return (rel.endswith(_SCRIPT_SUFFIXES) and not rel.endswith(".py")) or (
        rel in _tracked_executables()
    )


def _literal_text(node: ast.expr) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            str(part.value) if isinstance(part, ast.Constant) else "{}"
            for part in node.values
        )
    return None


@cache
def _embedded_bodies(rel: str, text: str) -> tuple[tuple[int, str], ...]:
    """The line and text of each script body a test-tree file holds.

    A body is a string literal that opens with a shebang, follows a shell's `-c` in an argv
    literal, or is passed to `.write_text()`. A shell script or executable is one body, whole.
    """
    if _whole_file_script(rel):
        return ((1, text),)
    if not rel.endswith(".py"):
        return ()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return ()
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.expr)]
    # An f-string's literal parts are nodes of their own; read them only through the f-string.
    parts = {
        id(part)
        for node in nodes
        if isinstance(node, ast.JoinedStr)
        for part in node.values
    }
    candidates = {id(node): node for node in _run_or_written(nodes)}
    bodies = []
    for node in nodes:
        if id(node) in parts or (body := _literal_text(node)) is None:
            continue
        if body.lstrip().startswith("#!") or id(node) in candidates:
            bodies.append((node.lineno, body.lstrip()))
    return tuple(bodies)


def _run_or_written(nodes: list[ast.expr]) -> list[ast.expr]:
    """The string after a shell's `-c` in an argv literal, and each `.write_text()` argument."""
    found = []
    for node in nodes:
        if isinstance(node, (ast.List, ast.Tuple)):
            argv = [_literal_text(elt) for elt in node.elts]
            found += [
                node.elts[i + 2]
                for i in range(len(argv) - 2)
                if (argv[i] or "").rsplit("/", 1)[-1] in _SHELLS
                and _COMMAND_FLAG.fullmatch(argv[i + 1] or "")
            ]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "write_text"
            and node.args
        ):
            found.append(node.args[0])
    return found


def script_bodies_in_tests() -> list[str]:
    """Tracked Python modules, shell scripts and executables under a `tests/` directory."""
    return [
        rel
        for rel in tracked("*")
        if _in_tests(rel) and (rel.endswith(".py") or _whole_file_script(rel))
    ]


def offending_bodies(subject: Subject) -> list[str]:
    """Each line of a test-held script body that one of `_BODY_OFFENCES` flags."""
    hits = []
    for start, body in _embedded_bodies(subject.rel, subject.text):
        script = Subject(subject.rel, body)
        for offence in _BODY_OFFENCES:
            hits += [f"script at line {start}, {hit}" for hit in offence(script)]
    return hits


TEST_SCRIPT_BODIES = Census(
    name="test-scripts-run-no-shimmed-binary-by-hardcoded-path",
    reason=(
        "leakguard shims `SHIMMED_BINARIES` for a test's direct child only. A script a test "
        "writes and runs that names `/usr/local/bin/k3s` itself reaches the real binary with "
        "nothing recorded (#7623, #7663). Call the bare name so the test's PATH resolves it to "
        "a stub, or read the path from a variable the test sets. A body that puts a fixed "
        "directory ahead of PATH, or sets PATH to fixed directories, hides or drops the stub "
        "directory the same way (#7676): append to the inherited PATH instead."
    ),
    files=script_bodies_in_tests,
    offence=offending_bodies,
    count=lambda subject: len(_embedded_bodies(subject.rel, subject.text)),
    red=(
        Subject(
            "a/tests/test_a.py",
            'STUB = textwrap.dedent("""\\\n    #!/bin/sh\n    /usr/local/bin/k3s ctr images ls\n""")\n',
        ),
        Subject("b/tests/test_b.py", 'f"#!/bin/bash\\n/usr/bin/curl -fsS {url}\\n"\n'),
        Subject("c/tests/stub.sh", "#!/bin/sh\n/usr/local/bin/kubectl get pods\n"),
        Subject(
            "d/tests/test_d.py",
            'run(["/bin/bash", "-ec", "/usr/local/bin/k3s ctr images ls"])\n',
        ),
        Subject(
            "e/tests/test_e.py",
            'p.write_text("set -e\\n/usr/local/bin/kubectl get pods\\n")\n',
        ),
        Subject(
            "f/tests/test_f.py",
            '"#!/bin/sh\\nexport PATH=\\"/usr/local/bin:${PATH}\\"\\nk3s ctr images ls\\n"\n',
        ),
        Subject("g/tests/stub.sh", "#!/bin/sh\nPATH=/usr/bin:/bin\ncurl -fsS x\n"),
    ),
    green=(
        Subject(
            "a/tests/test_a.py",
            '"""A script that runs `/usr/bin/logger` reaches syslog."""\n'
            'assert cmd == ["/usr/local/bin/k3s", "server"]\n',
        ),
        Subject(
            "b/tests/test_b.py",
            '"#!/bin/sh\\nexec \\"${KUBECTL:-/usr/local/bin/kubectl}\\" get pods\\n"\n',
        ),
        Subject("c/tests/stub.sh", "#!/bin/sh\nk3s kubectl get pods\n"),
        Subject(
            "d/tests/test_d.py", 'assert argv == ["ssh", "-c", "/usr/local/bin/k3s"]\n'
        ),
        Subject(
            "e/tests/test_e.py",
            '"#!/bin/sh\\nexport PATH=\\"${PATH}:/usr/local/bin\\"\\nk3s ctr images ls\\n"\n',
        ),
    ),
    min_matches=1250,
    must_find=frozenset(
        {
            "hosts/lib/tests/container/build-image.sh",
            "scripts/deploy_tools/tests/_deploy_sh_fakes.py",
        }
    ),
    allow={
        "ansible/tests/repo/_test_script_bodies_row.py": (
            "its shebang strings are this row's own red subjects, which the row reads as text"
        ),
        "scripts/validate/tests/test_shell_template_cron_rules.py": (
            "its shebang strings, `K3S_ABS` among them, are fixtures `cron_checks` reads as "
            "text; no test writes one to disk or runs it"
        ),
    },
)
