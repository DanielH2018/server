"""Every deployer `log()` line opens with one subject convention: `<subject>: ` (#4507).

The convention is a single literal token, then a colon and a space: `narrow: applying ...`,
`manual_plane: ...`, `gitops-deploy: ...`. One `grep '<subject>: '` then finds every line about
that subject. The test does not pin WHICH subjects exist, only their shape.

How each call shape is judged:

- A string concatenation (`"a" + f"b"`) is judged by its leftmost operand.
- An f-string that opens with an interpolation (`f"{role}: ..."`) has no literal subject and
  is a violation, because a variable subject cannot be grepped for.
- A call with no literal at all, such as the playbook-output relays `log(line.strip())` in
  `deploy_narrow.py`, is a violation too. The issue covers every `log()` call; a relayed line
  needs the subject prefixed onto it.
- Only the first line of a multi-line message is judged.

`log` is found by its bare name, by an `import ... as` alias of it, and as `deploy_config.log`.
"""

import ast
import re
import subprocess

from lib.repo_paths import REPO

FILES = "ansible/roles/setup/gitops_deploy/files"

SUBJECT = re.compile(r"[a-z0-9_-]+: ")

# Modules the census must reach. Each holds log() calls today, so a scan that stops finding
# them has stopped reading the tree rather than found it clean.
MUST_SCAN = frozenset(
    {
        f"{FILES}/gitops_deploy.py",
        f"{FILES}/deployer_lib/deploy_narrow.py",
        f"{FILES}/deployer_lib/deploy_handlers.py",
    }
)


def _tracked_modules() -> list[str]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", f"{FILES}/*.py"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    ).stdout
    return sorted(p for p in out.split("\0") if p)


def _log_names(tree: ast.Module) -> set[str]:
    names = {"log"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname for a in node.names if a.name == "log" and a.asname}
    return names


def _is_log_call(node: ast.Call, names: set[str]) -> bool:
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in names
    return (
        isinstance(func, ast.Attribute)
        and func.attr == "log"
        and isinstance(func.value, ast.Name)
        and func.value.id == "deploy_config"
    )


def _leading_text(arg: ast.expr) -> str | None:
    """The literal text `arg` opens with, or None when it opens with no literal."""
    while isinstance(arg, ast.BinOp) and isinstance(arg.op, ast.Add):
        arg = arg.left
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    if isinstance(arg, ast.JoinedStr) and arg.values:
        first = arg.values[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def _log_calls() -> list[tuple[str, int, str | None]]:
    calls = []
    for rel in _tracked_modules():
        tree = ast.parse((REPO / rel).read_text(), filename=rel)
        names = _log_names(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and node.args and _is_log_call(node, names):
                calls.append((rel, node.lineno, _leading_text(node.args[0])))
    return calls


def test_every_deployer_log_line_opens_with_one_subject_convention() -> None:
    calls = _log_calls()
    assert len(calls) >= 70, f"found only {len(calls)} log() calls under {FILES}"
    missing = MUST_SCAN - {rel for rel, _, _ in calls}
    assert not missing, f"the census found no log() call in {sorted(missing)}"

    offenders = [
        f"{rel}:{line}: {text.splitlines()[0][:80] if text else '<no literal subject>'!r}"
        for rel, line, text in calls
        if text is None or not SUBJECT.match(text.split("\n", 1)[0])
    ]
    assert not offenders, (
        f"{len(offenders)} of {len(calls)} deployer log() lines do not open with "
        f"`<subject>: `:\n" + "\n".join(offenders)
    )
