"""The values Ansible computes in role tasks that the renderer must supply itself.

Target slice R1 starts this module with the facts a role task reads from live state. The
renderer reads the git tree alone, so until target slice R3 removes those reads, each one
takes a fixed stand-in that both halves of the shadow render share: `shadow_diff.py stubs`
writes them into the stub file next to the secret stubs.

A string stand-in carries the secret stubs' marker, `stub-` plus the fact name with
underscores turned to hyphens. A render holding one is a stub render for that tag, and a
publish census that refuses stub tokens refuses it too. `k8s_built_images` pins to an empty
list, which carries no marker: n8n's `checksum_annotation` renders it as `"unstaged"`. Only a
rule keyed on `readers` can refuse that tag.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class PinnedFact:
    """One live-read fact's stand-in.

    Attributes:
        value: What the fact holds in a stub render.
        readers: The deploy tags whose templates read the fact.
    """

    value: object
    readers: frozenset[str]


PINNED_LIVE_FACTS = {
    # Read from the live users Secret, or hashed by a `kubectl run` pod.
    "authelia_password_hash": PinnedFact(
        "stub-authelia-password-hash", frozenset({"authelia"})
    ),
    "authelia_claude_password_hash": PinnedFact(
        "stub-authelia-claude-password-hash", frozenset({"authelia"})
    ),
    "authelia_agent_password_hash": PinnedFact(
        "stub-authelia-agent-password-hash", frozenset({"authelia"})
    ),
    # Slurped from daniel-pi's SSH host key over Ansible's own connection.
    "pi_peer_backup_known_hosts_line": PinnedFact(
        "stub-pi-peer-backup-known-hosts-line", frozenset({"pi-peer-backup"})
    ),
    # The registry digests image-builder records after each build, a list of name and digest.
    "k8s_built_images": PinnedFact([], frozenset({"n8n"})),
}
