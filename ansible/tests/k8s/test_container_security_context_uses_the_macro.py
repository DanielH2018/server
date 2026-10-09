"""A container-level securityContext comes from the shared macro, at both container depths.

`ansible/templates/security-context.yml.j2` carries the `allowPrivilegeEscalation: false` +
`capabilities.drop: [ALL]` body that ~100 container specs share. A
hand-written copy is where the body drifts: a `drop: [ALL]` that becomes `drop: [NET_RAW]`, a
`readOnlyRootFilesystem` that goes missing on a paste. `test_container_security_context.py`
checks the RENDERED result drops capabilities, so it cannot tell a macro call from a copy that
still happens to match.

One exemption, the macro's own docstring's: a block carrying `runAsUser: 0`. The macro refuses
uid 0 so each root site keeps the comment saying which uid owns the files and why root is
needed, which a macro call has nowhere to put.

The rule is textual. A literal `allowPrivilegeEscalation:` line must sit in a block that also
sets `runAsUser: 0`. The rule reads the line at 12 spaces, the key depth under
`hardened_security_context()`'s 10-space `securityContext:`. It also reads the line at 16
spaces, the key depth under `job_hardened_security_context()`, which a CronJob's `jobTemplate`
needs one level further in (#3721). Before #3721 the deeper depth was exempt, and 12 CronJob
containers had drifted into hand-written copies there.

Run: uv run pytest ansible/tests/k8s/test_container_security_context_uses_the_macro.py
"""

import re

from _helpers import K8S_ROLES

# Pod-template depth, then CronJob `jobTemplate` depth.
LITERAL_KEY = re.compile(r"^( {12}| {16})allowPrivilegeEscalation:", re.MULTILINE)
ROOT = re.compile(r"^( {12}| {16})runAsUser: 0\s*$", re.MULTILINE)

# The macro's two documented root call sites, so the census proves it can still find a block.
KNOWN_ROOT_BLOCKS = frozenset(
    {"code-server/deployment.yaml.j2", "loki-homelab/alloy-daemonset.yaml.j2"}
)


def hand_written_blocks(text: str) -> list[str]:
    """Each literal `allowPrivilegeEscalation:` block that does NOT set uid 0.

    A block is the run of lines around the key indented at least as deep as the key; the
    next line at the `securityContext:` depth or shallower ends it in either direction.
    """
    lines = text.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if not (key := LITERAL_KEY.match(line)):
            continue
        depth = len(key.group(1))
        start = i
        while start > 0 and _inside(lines[start - 1], depth):
            start -= 1
        end = i + 1
        while end < len(lines) and _inside(lines[end], depth):
            end += 1
        block = "\n".join(lines[start:end])
        if not ROOT.search(block):
            offenders.append(block)
    return offenders


def _inside(line: str, depth: int) -> bool:
    return line.startswith(" " * (depth - 1)) or not line.strip()


def test_every_hand_written_block_is_a_root_block():
    seen_root = set()
    offenders = {}
    for template in sorted(K8S_ROLES.glob("*/templates/*.yaml.j2")):
        text = template.read_text()
        rel = f"{template.parent.parent.name}/{template.name}"
        if LITERAL_KEY.search(text) and ROOT.search(text):
            seen_root.add(rel)
        if bad := hand_written_blocks(text):
            offenders[rel] = len(bad)
    assert KNOWN_ROOT_BLOCKS <= seen_root, sorted(seen_root)
    assert offenders == {}, (
        "container securityContext written out instead of hardened_security_context(): "
        f"{offenders}"
    )


_ROOT_BLOCK = """
          securityContext:
            allowPrivilegeEscalation: false
            runAsUser: 0
            capabilities:
              drop: [ALL]
              add: [DAC_READ_SEARCH]
          volumeMounts:
"""

_COPIED_BLOCK = """
          securityContext:
            allowPrivilegeEscalation: false
            runAsNonRoot: true
            capabilities:
              drop: [ALL]
          volumeMounts:
"""


def _at_cronjob_depth(block: str) -> str:
    return block.replace("\n ", "\n     ")


def test_a_copied_non_root_block_is_flagged_at_both_depths_and_root_passes():
    assert hand_written_blocks(_ROOT_BLOCK) == []
    assert hand_written_blocks(_at_cronjob_depth(_ROOT_BLOCK)) == []
    assert len(hand_written_blocks(_COPIED_BLOCK)) == 1
    assert len(hand_written_blocks(_at_cronjob_depth(_COPIED_BLOCK))) == 1
    assert len(hand_written_blocks(_ROOT_BLOCK + _COPIED_BLOCK)) == 1
