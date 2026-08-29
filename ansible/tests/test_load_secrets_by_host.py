"""The secret-load preamble picks its file per host, and refuses a play that mixes hosts.

`pre_tasks/load_secrets.yml` is imported by every playbook that touches a host, and it loads
under `run_once: true`. That combination is the hazard this guard exists for: `run_once`
resolves `secrets_file` for whichever host Ansible reaches first and then applies that ONE file
to the whole play. Every play in this repo is single-host today, but `hosts:` is
`{{ target | default(hostname) }}` in five of them and `target` takes a group as readily as a
host name — so `-e target=homeservers` is one flag away from a mixed play.

The dangerous direction is not symmetric. A staging host that picked up the production file
would put every production credential in scope on the host whose stated purpose is being broken
(docs/staging-cluster.md, Decision 5). So the preamble asserts the hosts agree rather than
picking a winner, and these tests pin both halves: the file is a variable, and the assert that
makes the variable safe is still there.
"""

import re

import yaml

from _helpers import ALL_VARS, ANSIBLE, HOST_VARS

PREAMBLE = ANSIBLE / "pre_tasks" / "load_secrets.yml"
VARS_DIR = ANSIBLE / "vars"

VAR_NAME = "secrets_file"
PRODUCTION_FILE = "secrets.yml"
STAGING_FILE = "secrets-staging.yml"


def _tasks():
    tasks = yaml.safe_load(PREAMBLE.read_text())
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
        f"here loads production secrets on every host, staging included, which is exactly what "
        f"docs/staging-cluster.md Decision 5 rules out."
    )


def test_a_mixed_play_is_refused_before_anything_is_loaded():
    """Without this, `run_once` silently applies one host's file to hosts that chose another."""
    tasks = _tasks()
    # Selected by CONTENT, not position. The preamble carries a second, unrelated assert (the
    # wrong-machine guard, ansible/tests/test_local_connection_target.py), and this test used to
    # take the first assert in the file — which silently became that one when it landed.
    mine = [
        i
        for i, t in enumerate(tasks)
        if VAR_NAME in str(t.get("ansible.builtin.assert", {}).get("that", ""))
    ]
    assert mine, (
        f"{PREAMBLE} has no assert guarding the load. `community.sops.load_vars` runs "
        f"`run_once: true`, so a play spanning a staging host and a production one loads ONE "
        f"of their files for both. Read this file's docstring before removing the guard."
    )
    load = next(i for i, t in enumerate(tasks) if "community.sops.load_vars" in t)
    assert min(mine) < load, (
        f"the assert in {PREAMBLE} runs AFTER the load, so the wrong secrets are already in "
        f"scope by the time it fires."
    )
    guard = tasks[min(mine)]["ansible.builtin.assert"]["that"]
    assert "ansible_play_hosts_all" in guard and VAR_NAME in guard, (
        f"the assert in {PREAMBLE} is {guard!r}. It has to compare {VAR_NAME} across "
        f"`ansible_play_hosts_all` — checking anything else does not catch a mixed play."
    )
    assert "run_once" in tasks[min(mine)], (
        f"the assert in {PREAMBLE} is not `run_once`, so it re-runs per host. It reads the "
        f"whole play either way; running it once matches the load it guards."
    )


def test_production_is_the_default():
    default = yaml.safe_load(ALL_VARS.read_text())[VAR_NAME]
    assert default == PRODUCTION_FILE, (
        f"{ALL_VARS} defaults {VAR_NAME} to {default!r}, expected {PRODUCTION_FILE!r}. A host "
        f"nobody thought about should get the production file and fail closed on a missing "
        f"key, not run with no secrets at all."
    )


def test_staging_overrides_it():
    stage = yaml.safe_load((HOST_VARS / "daniel-stage.yml").read_text())
    assert stage.get(VAR_NAME) == STAGING_FILE, (
        f"daniel-stage sets {VAR_NAME} to {stage.get(VAR_NAME)!r}, expected {STAGING_FILE!r}. "
        f"Without the override the staging play loads every production credential into scope."
    )


def test_every_file_any_host_names_exists():
    """A typo here fails at decrypt time on the host, which is a long way from the edit."""
    named = {yaml.safe_load(ALL_VARS.read_text())[VAR_NAME]}
    for host_file in HOST_VARS.glob("*.yml"):
        value = (yaml.safe_load(host_file.read_text()) or {}).get(VAR_NAME)
        if value:
            named.add(value)
    missing = sorted(n for n in named if not (VARS_DIR / n).is_file())
    assert not missing, (
        f"{missing} named by {VAR_NAME} but absent from {VARS_DIR}. Check the spelling — "
        f"the path is relative to that directory, not to the playbook."
    )


def test_no_host_shares_a_secrets_file_with_a_host_in_a_different_cluster():
    """daniel-stage's file is its own. If another host adopts it, the isolation is gone."""
    sharers = [
        f.stem
        for f in HOST_VARS.glob("*.yml")
        if (yaml.safe_load(f.read_text()) or {}).get(VAR_NAME) == STAGING_FILE
    ]
    assert sharers == ["daniel-stage"], (
        f"{sharers} all load {STAGING_FILE}, expected only daniel-stage. That file is encrypted "
        f"to daniel-server's key alone and holds generated values — a production host loading "
        f"it would run with fake credentials, not with none."
    )
