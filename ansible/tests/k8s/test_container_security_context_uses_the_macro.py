"""A container-level securityContext at pod-template depth comes from the shared macro.

`ansible/templates/security-context.yml.j2` carries the `allowPrivilegeEscalation: false` +
`capabilities.drop: [ALL]` body that ~100 container specs share; 78 templates import it. A
hand-written copy is where the body drifts: a `drop: [ALL]` that becomes `drop: [NET_RAW]`, a
`readOnlyRootFilesystem` that goes missing on a paste. `test_container_security_context.py`
checks the RENDERED result drops capabilities, so it cannot tell a macro call from a copy that
still happens to match.

Two exemptions, both the macro's own docstring's, both structural rather than named:

- a block carrying `runAsUser: 0`. The macro refuses uid 0 so that
  `test_root_needs_dac_capability.py`, which scans raw text, still sees a root block.
- a block nested deeper than 10 spaces. A CronJob's `jobTemplate` puts its containers one
  level further in, and the macro emits at one fixed depth by design.

So the rule is textual: a literal `allowPrivilegeEscalation:` line at exactly 12 spaces (the
key depth under a 10-space `securityContext:`) must sit in a block that also sets
`runAsUser: 0`. Seven such blocks exist and every one is root; the audit that filed this
counted four exemptions and was wrong about the count, not the rule.

Run: uv run pytest ansible/tests/k8s/test_container_security_context_uses_the_macro.py
"""

import re

from _helpers import K8S_ROLES

LITERAL_KEY = re.compile(r"^ {12}allowPrivilegeEscalation:", re.MULTILINE)
ROOT = re.compile(r"^ {12}runAsUser: 0\s*$", re.MULTILINE)

# The macro's two documented root call sites, so the census proves it can still find a block.
KNOWN_ROOT_BLOCKS = frozenset(
    {"code-server/deployment.yaml.j2", "loki-homelab/alloy-daemonset.yaml.j2"}
)


def hand_written_blocks(text: str) -> list[str]:
    """Each literal 12-space `allowPrivilegeEscalation:` block that does NOT set uid 0.

    A block is the run of 12-space-or-deeper lines around the key; the next line indented
    10 spaces or less ends it in either direction.
    """
    lines = text.splitlines()
    offenders = []
    for i, line in enumerate(lines):
        if not LITERAL_KEY.match(line):
            continue
        start = i
        while start > 0 and _deeper_than_ten(lines[start - 1]):
            start -= 1
        end = i + 1
        while end < len(lines) and _deeper_than_ten(lines[end]):
            end += 1
        block = "\n".join(lines[start:end])
        if not ROOT.search(block):
            offenders.append(block)
    return offenders


def _deeper_than_ten(line: str) -> bool:
    return line.startswith(" " * 11) or not line.strip()


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

_CRONJOB_DEPTH_BLOCK = _COPIED_BLOCK.replace("\n ", "\n     ")


def test_a_copied_non_root_block_is_flagged_and_the_two_exemptions_pass():
    assert hand_written_blocks(_ROOT_BLOCK) == []
    assert hand_written_blocks(_CRONJOB_DEPTH_BLOCK) == []
    assert len(hand_written_blocks(_COPIED_BLOCK)) == 1
    assert len(hand_written_blocks(_ROOT_BLOCK + _COPIED_BLOCK)) == 1
