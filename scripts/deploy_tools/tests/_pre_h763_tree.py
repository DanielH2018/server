"""The last tree in which `initial_setup` still ran tasks, for the setup-reach cases.

H7.63 emptied `initial_setup`, the one real setup role whose tasks shipped files both ungated
and under per-host gates, and left no live setup role in `initial_setup.yml`. `land_reach`
keeps its reach code until H7.53 deletes the deployer, so the cases that need a real role of
that shape read it from the commit before H7.63 rather than from the checkout. CI clones with
full history, so the commit is always there.
"""

import contextlib
from collections.abc import Generator
from pathlib import Path

from deploy_tools.setup_lib.setup_role_chains import tree_at
from lib.repo_paths import REPO

# The merge base H7.63 branched from: origin/master at "Reword the setup tasks comment that
# called the Kuma move pending".
PRE_H763 = "4f1faa6e2199b0b2ba73c239c0399926511ead14"


@contextlib.contextmanager
def pre_h763_paths() -> Generator[dict[str, Path]]:
    """`setup_file_hosts`' tree keywords, pointed at `PRE_H763`'s `ansible/` tree."""
    with tree_at(PRE_H763, REPO) as root:
        assert root is not None, f"git could not archive {PRE_H763[:9]}'s ansible/ tree"
        ansible = root / "ansible"
        yield {
            "playbook": ansible / "initial_setup.yml",
            "all_vars": ansible / "inventory" / "group_vars" / "all.yml",
            "host_vars_dir": ansible / "inventory" / "host_vars",
            "roles_dir": ansible / "roles" / "setup",
        }
