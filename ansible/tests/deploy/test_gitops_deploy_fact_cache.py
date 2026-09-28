"""The GitOps deployer's playbooks read a fact cache no worktree writes (#2862).

`ansible.cfg` caches facts per HOST in `~/.cache/ansible/facts`, shared by every checkout on
the machine, and the cache carries `discovered_interpreter_python` — the `.venv` of whichever
checkout gathered last. The 2026-09-03 12:36 broad apply of `initial_setup.yml --tags
renovate_agent` died at fact gathering on a pruned worktree's interpreter that way.
`gitops-deploy.service.j2` points the deployer at a cache of its own through
`ANSIBLE_CACHE_PLUGIN_CONNECTION`.

A misspelled variable name would be ignored without a word, so this asks Ansible itself which
cache directory it resolves under the unit's environment, and asks it once more without that
environment as the control that the answer can differ.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

from _helpers import ALL_VARS, REPO, load_yaml

_UNIT = REPO / "ansible/roles/setup/gitops_deploy/templates/gitops-deploy.service.j2"
_ENV_LINE = re.compile(r"^Environment=(ANSIBLE_[A-Z_]+)=(\S+)$", re.MULTILINE)
_CONNECTION = re.compile(r"^CACHE_PLUGIN_CONNECTION\((.+?)\) = (.+)$", re.MULTILINE)


def _cache_connection(extra_env: dict[str, str]) -> tuple[str, str]:
    """(source, value) of the fact-cache directory Ansible resolves from the repo root."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("ANSIBLE_")}
    env.update(extra_env)
    out = subprocess.run(
        [str(Path(sys.executable).parent / "ansible-config"), "dump", "--only-changed"],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    ).stdout
    match = _CONNECTION.search(out)
    assert match, f"ansible-config printed no CACHE_PLUGIN_CONNECTION:\n{out}"
    return match.group(1), match.group(2)


def test_the_deployer_unit_resolves_a_fact_cache_outside_the_shared_one():
    sys_user = load_yaml(ALL_VARS)["sys_user"]
    unit = _UNIT.read_text().replace("{{ sys_user }}", sys_user)
    unit_env = dict(_ENV_LINE.findall(unit))
    assert "ANSIBLE_CACHE_PLUGIN_CONNECTION" in unit_env, (
        f"{_UNIT.name} no longer gives the deployer its own fact cache"
    )
    private = unit_env["ANSIBLE_CACHE_PLUGIN_CONNECTION"]
    assert private.startswith(f"/home/{sys_user}/"), private
    # monitor-bridge mounts the deployer's state directory into its pod.
    assert not private.startswith("/var/lib/gitops-deploy"), private

    shared_source, shared = _cache_connection({})
    assert shared_source.endswith("ansible.cfg") and shared != private, (
        "the control run resolved the unit's directory without the unit's environment"
    )

    source, resolved = _cache_connection(unit_env)
    assert (source, resolved) == ("env: ANSIBLE_CACHE_PLUGIN_CONNECTION", private)
