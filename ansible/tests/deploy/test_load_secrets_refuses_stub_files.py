"""The preamble refuses a secret stub file outside a k8s dry run, and one that misses a key.

A stub file (`scripts/renderer/shadow_diff.py stubs`, target slice R1) holds a placeholder for
every secret key. Passed to a run that applies anything, it would apply those placeholders as
credentials. `ansible/pre_tasks/load_secrets.yml` holds two asserts against that, ahead of the
SOPS load, and every playbook that reads a secret imports the file.

These tests copy the two asserts out of the preamble verbatim and run them in a real
`ansible-playbook` against a fixture `vars/secrets.yml`, so the expression that decides is the
one in the file, evaluated under ansible-core's own boolean-conditional rule. The rest of the
preamble decrypts SOPS and cannot run here. Each fixture host is one case, so one play decides
them all. Every refusal has an accepted twin, because an assert that fires on everything and one
that fires on nothing look the same from the passing side alone.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from lib import yaml_fast
from lib.git_testing import scrubbed_env
from lib.repo_paths import HOST_VARS
from renderer import shadow_diff
from secret_stubs import homelab_secret_stub

from _helpers import ANSIBLE, REPO

PREAMBLE = ANSIBLE / "pre_tasks" / "load_secrets.yml"
DEPLOY = ANSIBLE / "deploy.yml"

OUTSIDE = "Refuse a secret stub file outside a k8s dry run"
INCOMPLETE = "Refuse a stub file that does not stub every key"

# Key names only, as in the real file: SOPS leaves names in plaintext. `become_password` is in
# NEVER_STUB, so no stub file carries it and the asserts must not ask for it.
_SECRETS = """\
alpha_token: ENC[AES256_GCM,data:AAAA,type:str]
domain: ENC[AES256_GCM,data:BBBB,type:str]
google_assistant_service_account: ENC[AES256_GCM,data:CCCC,type:str]
become_password: ENC[AES256_GCM,data:DDDD,type:str]
sops:
    version: 3.9.0
"""
_KEYS = ("alpha_token", "domain", "google_assistant_service_account")
_STUBS = {key: homelab_secret_stub(key) for key in _KEYS}
_SHADOW = {"k8s_dry_run": True, "load_secrets_stubs_allowed": True}


def _preamble_tasks() -> list[dict]:
    return yaml_fast.safe_load(PREAMBLE.read_text())


def _stub_asserts() -> list[dict]:
    """The two stub asserts, as written in the preamble, in file order."""
    found = [t for t in _preamble_tasks() if t.get("name") in (OUTSIDE, INCOMPLETE)]
    assert [t["name"] for t in found] == [OUTSIDE, INCOMPLETE], (
        f"expected the asserts {OUTSIDE!r} then {INCOMPLETE!r} in {PREAMBLE}. If one was "
        f"renamed, update this test rather than deleting it."
    )
    return found


def _run(
    tmp_path: Path, hosts: dict[str, dict], extra_vars: str = ""
) -> dict[str, dict]:
    """Run the two asserts once per fixture host and return each host's task results by name.

    `playbook_dir` is `tmp_path`, so the asserts read the fixture `vars/secrets.yml`.
    """
    (tmp_path / "vars").mkdir()
    (tmp_path / "vars" / "secrets.yml").write_text(_SECRETS)
    # ansible-core has no JSON stdout callback of its own: `ansible.builtin.json` redirects to
    # `ansible.posix.json`, which CI's env does not install. So each assert registers its
    # result and lets the play continue, and a final task writes both results to a file per
    # host, which this function reads back.
    registers = {OUTSIDE: "outside_result", INCOMPLETE: "incomplete_result"}
    asserts = [
        {**task, "register": registers[task["name"]], "ignore_errors": True}
        for task in _stub_asserts()
    ]
    results_by_name = ", ".join(
        f"{json.dumps(name)}: {var}" for name, var in registers.items()
    )
    record = {
        "name": "Record each assert's result",
        "ansible.builtin.copy": {
            "dest": "{{ playbook_dir }}/results/{{ inventory_hostname }}.json",
            "content": "{{ {" + results_by_name + "} | to_json }}",
        },
    }
    (tmp_path / "results").mkdir()
    play = [{"hosts": "all", "gather_facts": False, "tasks": [*asserts, record]}]
    (tmp_path / "play.yml").write_text(yaml.safe_dump(play, sort_keys=False))
    inventory = {
        "all": {
            "hosts": {
                name: {"ansible_connection": "local", **hostvars}
                for name, hostvars in hosts.items()
            }
        }
    }
    (tmp_path / "hosts.yml").write_text(yaml.safe_dump(inventory))
    command = ["ansible-playbook", "play.yml", "-i", "hosts.yml"]
    if extra_vars:
        (tmp_path / "stubs.yml").write_text(extra_vars)
        command += ["-e", "@stubs.yml"]

    env = scrubbed_env()
    env["ANSIBLE_FILTER_PLUGINS"] = str(ANSIBLE / "filter_plugins")
    env["ANSIBLE_PYTHON_INTERPRETER"] = sys.executable
    env["ANSIBLE_CONFIG"] = str(tmp_path / "none.cfg")
    env["ANSIBLE_NOCOLOR"] = "1"
    result = subprocess.run(
        command,
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if result.returncode != 0:
        pytest.fail(
            f"ansible-playbook exited {result.returncode}.\n{result.stdout}\n{result.stderr}"
        )
    return {
        name: json.loads((tmp_path / "results" / f"{name}.json").read_text())
        for name in hosts
    }


def _verdict(results: dict[str, dict]) -> str:
    """`passed`, or the name of the first assert that failed."""
    for name in (OUTSIDE, INCOMPLETE):
        outcome = results.get(name)
        assert outcome is not None, f"{name!r} never ran: {results}"
        if outcome.get("failed"):
            return name
    return "passed"


pytestmark = pytest.mark.skipif(
    shutil.which("ansible-playbook") is None, reason="ansible-playbook not on PATH"
)


def test_each_stub_run_case_is_refused_or_accepted(tmp_path: Path) -> None:
    hosts = {
        # The red-proof the plan asks for: a real run, before the SOPS load, defines no key.
        "no_stub_file": {},
        # deploy.yml's second play on a real run: the SOPS load of the first play left every
        # key holding its real value, and none of them is a stub.
        "real_values_loaded": {key: f"real-{key}" for key in _KEYS},
        "shadow_dry_run": {**_STUBS, "homelab_secret_stubs": True, **_SHADOW},
        "not_a_dry_run": {
            **_STUBS,
            "homelab_secret_stubs": True,
            "load_secrets_stubs_allowed": True,
        },
        "not_allowed": {**_STUBS, "homelab_secret_stubs": True, "k8s_dry_run": True},
        "sentinel_stripped": {**_STUBS, "load_secrets_stubs_allowed": True},
        # Allowed and a dry run, yet refused: sonarr's API reads skip on the sentinel, so
        # without it they would send stub keys to a live Sonarr.
        "sentinel_stripped_in_a_dry_run": {**_STUBS, **_SHADOW},
        "one_key_missing": {
            **{k: v for k, v in _STUBS.items() if k != "alpha_token"},
            "homelab_secret_stubs": True,
            **_SHADOW,
        },
        "sentinel_without_a_file": {"homelab_secret_stubs": True, **_SHADOW},
    }
    results = _run(tmp_path, hosts)
    assert {host: _verdict(r) for host, r in results.items()} == {
        "no_stub_file": "passed",
        "real_values_loaded": "passed",
        "shadow_dry_run": "passed",
        "not_a_dry_run": OUTSIDE,
        "not_allowed": OUTSIDE,
        "sentinel_stripped": OUTSIDE,
        "sentinel_stripped_in_a_dry_run": OUTSIDE,
        "one_key_missing": INCOMPLETE,
        "sentinel_without_a_file": INCOMPLETE,
    }
    missing = results["one_key_missing"][INCOMPLETE]["msg"]
    assert "alpha_token" in missing and "domain" not in missing, missing
    unstubbed = results["sentinel_without_a_file"][INCOMPLETE]["msg"]
    assert all(key in unstubbed for key in _KEYS), unstubbed
    assert "become_password" not in unstubbed, (
        "the assert asks for become_password, which NEVER_STUB keeps out of every stub file"
    )


def test_the_real_stub_file_passes_only_where_it_is_allowed(tmp_path: Path) -> None:
    """The file `shadow_diff.py stubs` writes, passed as `-e @FILE` the way the operator does."""
    names = tmp_path / "secret-names.yml"
    names.write_text(_SECRETS)
    results = _run(
        tmp_path,
        {
            "box_dry_run": _SHADOW,
            "pi_dry_run": {"k8s_dry_run": True, "load_secrets_stubs_allowed": False},
        },
        extra_vars=shadow_diff.stub_file_text(names),
    )
    assert _verdict(results["box_dry_run"]) == "passed"
    assert _verdict(results["pi_dry_run"]) == OUTSIDE


def test_the_asserts_precede_the_sops_load_and_always_run() -> None:
    names = [t.get("name") for t in _preamble_tasks()]
    assert names.index(INCOMPLETE) == names.index(OUTSIDE) + 1
    assert names.index(INCOMPLETE) < names.index("Load encrypted secrets"), (
        "a stub assert runs after the SOPS load, where it compares real values and every task "
        "the refusal exists to stop may already have run"
    )
    for task in _stub_asserts():
        assert task.get("tags") == "always", (
            f"{task['name']!r} is skipped by a --tags run"
        )
        assert task.get("become") is False, (
            f"{task['name']!r} would escalate before the become password"
        )
        assert not task.get("run_once"), f"{task['name']!r} must decide per host"


def _deploy_allowed_expressions() -> list[object]:
    return [
        play.get("vars", {}).get("load_secrets_stubs_allowed")
        for play in yaml_fast.safe_load(DEPLOY.read_text())
    ]


def test_only_deploy_yml_allows_a_stub_file() -> None:
    """A stub file passed to any other importer stops at its preamble, dry run or not."""
    setters = sorted(
        str(path.relative_to(REPO))
        for path in ANSIBLE.rglob("*.yml")
        if "load_secrets_stubs_allowed" in path.read_text()
        and path not in (DEPLOY, PREAMBLE)
    )
    assert setters == [], (
        f"only deploy.yml may set load_secrets_stubs_allowed: {setters}"
    )
    docker_play, k8s_play = _deploy_allowed_expressions()
    assert k8s_play is True
    assert (
        isinstance(docker_play, str) and "filter_by_platform('docker')" in docker_play
    )


def test_the_docker_play_allows_a_stub_file_only_on_a_host_with_no_docker_entry() -> (
    None
):
    """Evaluated against the real inventory: daniel-box passes, daniel-pi is refused."""
    from ansible.parsing.dataloader import DataLoader
    from ansible.template import Templar, trust_as_template
    from toposort import filter_by_platform

    docker_play = _deploy_allowed_expressions()[0]
    assert isinstance(docker_play, str)
    verdicts = {}
    for host in ("daniel-box", "daniel-pi"):
        hostvars = yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text())
        templar = Templar(
            loader=DataLoader(),
            variables={"containers_list": hostvars["containers_list"]},
        )
        templar.environment.filters["filter_by_platform"] = filter_by_platform
        verdicts[host] = templar.template(trust_as_template(docker_play))
    assert verdicts == {"daniel-box": True, "daniel-pi": False}
