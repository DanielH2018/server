"""The guard a test that spawns a real playbook uses: does ansible resolve a collection here?

A playbook that names `community.general.ufw` fails to parse where no collection resolves, and
that reads as a red test the change under test did not cause. A fan-out unit never loads the
`claude` agent user's login profile, so ANSIBLE_COLLECTIONS_PATH is unset and ansible.cfg falls
back to the operator's checkout, which that user cannot read (#4226).

Probe before the playbook run rather than match "couldn't resolve" in its stderr: a misspelt
module prints the same message, and that must stay red.
"""

import re
import subprocess
from pathlib import Path

INSTALL_HINT = (
    "export ANSIBLE_COLLECTIONS_PATH as the agent profile does (#4108), or run "
    "`ansible-galaxy collection install -r ansible/requirements.yml -p ansible/collections`"
)


def collection_resolves(
    name: str, playbook_bin: str, cwd: Path, env: dict[str, str]
) -> bool:
    """Whether the `ansible-galaxy` beside `playbook_bin` finds collection `name` from `cwd`.

    Args:
        name: the collection, such as `community.general`.
        playbook_bin: the resolved `ansible-playbook` path; its sibling `ansible-galaxy` reads
          the same install and the same ansible.cfg.
        cwd: the directory the playbook runs from. ansible.cfg's first collections path is
          relative to it.
        env: the environment the playbook runs with.
    """
    result = subprocess.run(
        [str(Path(playbook_bin).parent / "ansible-galaxy"), "collection", "list", name],
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )
    # ansible-galaxy exits 0 whether or not it finds the collection, so read its table.
    return re.search(rf"^{re.escape(name)}\s", result.stdout, re.MULTILINE) is not None
