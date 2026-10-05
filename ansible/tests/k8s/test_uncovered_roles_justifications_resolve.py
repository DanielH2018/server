"""Every file an `_UNCOVERED_ROLES` justification names must still exist.

`test_container_security_context.py` exempts a handful of roles from its corpus, each with a
comment saying why, and the stale check there asserts only that each exempt ROLE still exists.
It cannot see a justification pointing at a file the role no longer has. The exemption stays
correct, so nothing fails, but the reason a reader is handed points at a dead file.

This reads the exemption block as text — a comment is not a value the test module could export
— and checks every `*.j2` and `test_*.py` token in it against the tree. A `.j2` must sit under
some k8s role's `templates/`; a test module must be tracked somewhere in the repo.
"""

import re

from _helpers import K8S_ROLES, REPO
from lib.proc_testing import run

_SOURCE = REPO / "ansible" / "tests" / "k8s" / "test_container_security_context.py"
_BLOCK = re.compile(r"^_UNCOVERED_ROLES = \{\n(.*?)^\}", re.M | re.S)
_FILE_TOKEN = re.compile(r"(?<![\w./-])(?:[\w.-]+\.j2|test_\w+\.py)\b")

# Named members the census must find, so a regex that stops matching fails here rather than
# passing over an empty set (the non-vacuity rule in CLAUDE.md, Python & Tests).
_KNOWN_REFERENCES = frozenset(
    {"build-job.yaml.j2", "test_image_builder_security_context.py"}
)


def exemption_block() -> str:
    match = _BLOCK.search(_SOURCE.read_text())
    assert match, f"no `_UNCOVERED_ROLES = {{...}}` block in {_SOURCE.name}"
    return match.group(1)


def referenced_files(block: str) -> set[str]:
    return set(_FILE_TOKEN.findall(block))


def _tracked_basenames() -> set[str]:
    listed = run(["git", "ls-files", "-z"], cwd=REPO, check=True).stdout
    return {rel.rsplit("/", 1)[-1] for rel in listed.split("\0") if rel}


def unresolved(block: str, template_names: set[str], test_names: set[str]) -> set[str]:
    """The referenced files present in neither the templates nor the tracked test modules."""
    missing = set()
    for name in referenced_files(block):
        pool = test_names if name.endswith(".py") else template_names
        if name not in pool:
            missing.add(name)
    return missing


def test_every_named_file_in_the_exemption_block_exists():
    block = exemption_block()
    found = referenced_files(block)
    assert _KNOWN_REFERENCES <= found, (
        "the file-token census no longer finds "
        f"{sorted(_KNOWN_REFERENCES - found)} — the regex or the block layout changed"
    )
    templates = {p.name for p in K8S_ROLES.glob("*/templates/**/*.j2")}
    tests = {n for n in _tracked_basenames() if n.startswith("test_")}
    missing = unresolved(block, templates, tests)
    assert not missing, (
        "_UNCOVERED_ROLES justifies an exemption with a file that is not in the tree: "
        + ", ".join(sorted(missing))
    )


def test_a_justification_naming_a_dead_file_is_flagged():
    block = (
        "    # seed-pod.yaml.j2 IS a pod spec; test_seed_pod_security_context.py owns it.\n"
        '    "volume-claim",\n'
    )
    assert unresolved(block, {"pvc.yaml.j2"}, {"test_other.py"}) == {
        "seed-pod.yaml.j2",
        "test_seed_pod_security_context.py",
    }


def test_a_justification_naming_live_files_is_clean():
    block = (
        "    # build-job.yaml.j2 carries Unconfined seccomp;\n"
        "    # test_image_builder_security_context.py owns it.\n"
        '    "image-builder",\n'
    )
    assert not unresolved(
        block, {"build-job.yaml.j2"}, {"test_image_builder_security_context.py"}
    )
