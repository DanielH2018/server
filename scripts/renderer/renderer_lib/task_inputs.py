"""The values Ansible computes in role tasks that the renderer must supply itself.

`include_vars` reads the `vars:` a role passes to its `k8s/manifests` include, and
`TASK_COMPUTED` names the values R1's PR 3 derives.

Target slice R1 started this module with the facts a role task reads from live state. The
renderer reads the git tree alone, so until target slice R3 removes those reads, each one
takes a fixed stand-in that both halves of the shadow render share: `shadow_diff.py stubs`
writes them into the stub file next to the secret stubs.

A string stand-in carries the secret stubs' marker, `stub-` plus the fact name with
underscores turned to hyphens. A render holding one is a stub render for that tag, and a
publish census that refuses stub tokens refuses it too. `k8s_built_images` pins to an empty
list, which carries no marker: n8n's `checksum_annotation` renders it as `"unstaged"`. Only a
rule keyed on `readers` can refuse that tag.
"""

import sys as _sys
from pathlib import Path as _Path

_sys.path.insert(0, str(_Path(__file__).resolve().parents[2]))

from dataclasses import dataclass
from pathlib import Path

from lib import yaml_fast


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


# The values a role task computes that a template or a role default reads. R1's PR 3 derives
# each one from the tree: the content tags from image-builder's own expression, the script
# checksums from the staged files, `kuma_declarations` from its render and `janitorr_dry_run`
# from defaults. Until then `cluster.PENDING` refuses every tag that reads one.
TASK_COMPUTED = frozenset(
    {
        "k8s_built_image_tags",
        "monitor_bridge_script_checksum",
        "autofix_bridge_script_checksum",
        "game_stats_terraria_script_checksum",
        "game_stats_valheim_script_checksum",
        "kuma_declarations",
        "janitorr_dry_run",
    }
)

# The modules that include a role, by every name ansible-core accepts.
_INCLUDE_MODULES = (
    "include_role",
    "import_role",
    "ansible.builtin.include_role",
    "ansible.builtin.import_role",
)


def _tasks(nodes: list) -> list[dict]:
    """Every task in a tasks file, the ones inside `block`, `rescue` and `always` included."""
    found = []
    for task in nodes or []:
        if not isinstance(task, dict):
            continue
        found.append(task)
        for key in ("block", "rescue", "always"):
            found.extend(_tasks(task.get(key)))
    return found


def include_vars(role_dir: Path) -> dict:
    """The `manifests_*` vars `role_dir`'s tasks pass to the `k8s/manifests` include that renders.

    That is the include with no `tasks_from`, or `tasks_from: main`. Another entry point, such
    as configarr's `drain.yml` or pihole's `rollout_amend.yml`, renders nothing. The values are
    read as written, so a Jinja value is still a string for the caller to resolve.

    Raises:
        ValueError: The role's tasks hold no such include, or more than one.
    """
    found = []
    for path in sorted((role_dir / "tasks").glob("*.yml")):
        for task in _tasks(yaml_fast.safe_load(path.read_text())):
            for module in _INCLUDE_MODULES:
                args = task.get(module)
                if not isinstance(args, dict) or args.get("name") != "k8s/manifests":
                    continue
                if Path(args.get("tasks_from") or "main").stem != "main":
                    continue
                found.append(
                    {
                        k: v
                        for k, v in (task.get("vars") or {}).items()
                        if k.startswith("manifests_")
                    }
                )
    if len(found) != 1:
        raise ValueError(
            f"{role_dir.name} includes k8s/manifests to render {len(found)} times, not once"
        )
    return found[0]
