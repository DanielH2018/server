"""The secret-load preamble picks its file per host, and refuses a play that mixes hosts.

`pre_tasks/load_secrets.yml` is imported by every playbook that touches a host, and it loads
under `run_once: true`. That combination is the hazard this guard exists for: `run_once`
resolves `secrets_file` for whichever host Ansible reaches first and then applies that ONE file
to the whole play. Every play in this repo is single-host today, but `hosts:` is
`{{ target | default(hostname) }}` in five of them and `target` takes a group as readily as a
host name — so `-e target=homeservers` is one flag away from a mixed play.

The dangerous direction is not symmetric. A non-production host that picked up the production
file would put every production credential in scope on the host whose stated purpose is being
broken. So the preamble asserts the hosts agree rather than picking a winner, and these tests
pin both halves: the file is a variable, and the assert that makes the variable safe is still
there.

Every host reads `secrets.yml`, so no host exercises the override. The guard stays because
adding a host with its own file is meant to be a var rather than a code change, and because the
assert is what makes that safe.
"""

import re

from lib import yaml_fast

from _helpers import ALL_VARS, ANSIBLE, HOST_VARS

PREAMBLE = ANSIBLE / "pre_tasks" / "load_secrets.yml"
VARS_DIR = ANSIBLE / "vars"

VAR_NAME = "secrets_file"
PRODUCTION_FILE = "secrets.yml"


def _tasks():
    tasks = yaml_fast.safe_load(PREAMBLE.read_text())
    assert tasks, f"{PREAMBLE} parsed to no tasks — check the loader, not the playbook."
    return tasks


def _task_with(key):
    found = [t for t in _tasks() if key in t]
    assert found, (
        f"no task in {PREAMBLE} uses {key}. Has the preamble been restructured?"
    )
    return found[0]


def test_the_load_names_a_variable_and_not_a_literal_file():
    loaded = _task_with("community.sops.load_vars")["community.sops.load_vars"]["file"]
    assert re.fullmatch(r"\{\{\s*%s\s*\}\}" % VAR_NAME, loaded.strip()), (
        f"{PREAMBLE} loads {loaded!r}. It must be `{{{{ {VAR_NAME} }}}}` — a literal filename "
        f"here loads production secrets on every host, including any host added later whose "
        f"whole purpose is not to hold them."
    )


def test_a_mixed_play_is_refused_before_anything_is_loaded():
    """Without this, `run_once` silently applies one host's file to hosts that chose another."""
    tasks = _tasks()
    # Selected by CONTENT, not position. The preamble carries a second, unrelated assert (the
    # wrong-machine guard, ansible/tests/deploy/test_local_connection_target.py), and taking the
    # first assert in the file would silently select that one.
    mine = [
        i
        for i, t in enumerate(tasks)
        if VAR_NAME in str(t.get("ansible.builtin.assert", {}).get("that", ""))
    ]
    assert mine, (
        f"{PREAMBLE} has no assert guarding the load. `community.sops.load_vars` runs "
        f"`run_once: true`, so a play spanning hosts that named different files loads ONE of "
        f"them for both. Read this file's docstring before removing the guard."
    )
    # Selecting by content narrowed the subject; it did not make it unique. A second assert
    # mentioning `secrets_file` would land here silently and this test would go on checking
    # whichever came first. Say out loud that there is one.
    assert len(mine) == 1, (
        f"{PREAMBLE} has {len(mine)} asserts mentioning {VAR_NAME}, at task indices {mine}. "
        f"This test checks exactly one of them, so a second one is unchecked. Decide which is "
        f"the mixed-play guard and split this test rather than letting it pick."
    )
    load = next(i for i, t in enumerate(tasks) if "community.sops.load_vars" in t)
    assert mine[0] < load, (
        f"the assert in {PREAMBLE} runs AFTER the load, so the wrong secrets are already in "
        f"scope by the time it fires."
    )
    guard = tasks[mine[0]]["ansible.builtin.assert"]["that"]
    assert "ansible_play_hosts_all" in guard and VAR_NAME in guard, (
        f"the assert in {PREAMBLE} is {guard!r}. It has to compare {VAR_NAME} across "
        f"`ansible_play_hosts_all` — checking anything else does not catch a mixed play."
    )
    assert "run_once" in tasks[mine[0]], (
        f"the assert in {PREAMBLE} is not `run_once`, so it re-runs per host. It reads the "
        f"whole play either way; running it once matches the load it guards."
    )


def test_production_is_the_default():
    default = yaml_fast.safe_load(ALL_VARS.read_text())[VAR_NAME]
    assert default == PRODUCTION_FILE, (
        f"{ALL_VARS} defaults {VAR_NAME} to {default!r}, expected {PRODUCTION_FILE!r}. A host "
        f"nobody thought about should get the production file and fail closed on a missing "
        f"key, not run with no secrets at all."
    )


def test_no_host_overrides_it_today():
    """The census half. No host sets this, so a value appearing here means a second secrets
    file arrived with it —
    and `test_every_file_any_host_names_exists` below is what then has to find that file."""
    overriding = {
        f.stem: value
        for f in sorted(HOST_VARS.glob("*.yml"))
        if (value := (yaml_fast.safe_load(f.read_text()) or {}).get(VAR_NAME))
    }
    assert not overriding, (
        f"{overriding} override {VAR_NAME}. That is supported, but the mixed-play assert in "
        f"{PREAMBLE} is now load-bearing rather than latent — re-read this file's docstring."
    )


def test_every_file_any_host_names_exists():
    """A typo here fails at decrypt time on the host, which is a long way from the edit."""
    named = {yaml_fast.safe_load(ALL_VARS.read_text())[VAR_NAME]}
    for host_file in HOST_VARS.glob("*.yml"):
        value = (yaml_fast.safe_load(host_file.read_text()) or {}).get(VAR_NAME)
        if value:
            named.add(value)
    missing = sorted(n for n in named if not (VARS_DIR / n).is_file())
    assert not missing, (
        f"{missing} named by {VAR_NAME} but absent from {VARS_DIR}. Check the spelling — "
        f"the path is relative to that directory, not to the playbook."
    )


def test_only_the_production_secrets_file_is_tracked():
    """The other half of the census above: one file in vars/ means one file to reason about.

    A second secrets file is the point at which the per-host variable stops being latent. A file
    encrypted to one host's key alone and holding generated values would make a production host
    that adopted it run with fake credentials rather than with none.
    """
    tracked = sorted(p.name for p in VARS_DIR.glob("secrets*.y*ml"))
    assert tracked == [PRODUCTION_FILE], (
        f"{VARS_DIR} holds {tracked}, expected only [{PRODUCTION_FILE!r}]. A second secrets "
        f"file needs a host that names it, a .sops.yaml rule above the production one, and a "
        f"re-read of this file's docstring."
    )
