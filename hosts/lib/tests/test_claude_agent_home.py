"""The agent's keys and memory seed, written by `claude-agent-home` in a temporary home (H7.40).

hosts/components/claude-code ships `claude-agent-home`, which claude-agent-setup runs as root.
Each test renders daniel-box's package with the agent's home, keys and memory stores moved under
`tmp_path` and the agent set to the user running the test, then runs the script with a `runuser`
stub that drops its user argument. ssh-keygen, sed and cp are the real ones.

Run: uv run pytest hosts/lib/tests/test_claude_agent_home.py
"""

import getpass
import shutil
import subprocess

import pytest
import yaml

from lib.estate import Inventory
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

SCRIPT = "/usr/lib/homelab/claude-agent-home"
# The operator's checkout prefix, with a `.` a regex would read as any character.
STRIP = "/home/op.x/server/"
RUNUSER = '#!/bin/sh\n[ "$1" = -u ] && shift 2\n[ "$1" = -- ] && shift\nexec "$@"\n'


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """The script and the paths it renders for, shared by every test in the module."""
    root = tmp_path_factory.mktemp("render")
    home = root / "home"
    paths = {
        "home": home,
        "signing": home / ".ssh" / "git_signing_ed25519",
        "peer": home / ".ssh" / "peers_ed25519",
        "agent_memory": home / "memory",
        "operator_memory": root / "operator" / "memory",
    }
    host_vars = root / "host_vars"
    shutil.copytree(REPO / "ansible" / "inventory" / "host_vars", host_vars)
    path = host_vars / "daniel-box.yml"
    overrides = {
        "claude_code_agent_user": getpass.getuser(),
        "claude_code_agent_user_home": str(home),
        "claude_code_agent_signing_key": str(paths["signing"]),
        "claude_agent_ssh_key_path": str(paths["peer"]),
        "claude_code_agent_memory_dir": str(paths["agent_memory"]),
        "claude_code_operator_memory_dir": str(paths["operator_memory"]),
        "claude_code_agent_memory_seed_strip": STRIP,
    }
    path.write_text(yaml.safe_dump({**yaml.safe_load(path.read_text()), **overrides}))
    package = host_mode.render_host(
        "daniel-box", inventory=Inventory(host_vars=host_vars)
    )
    script = root / "claude-agent-home"
    script.write_bytes(package.files[SCRIPT][0])
    script.chmod(0o755)
    stubs = root / "stubs"
    stubs.mkdir()
    (stubs / "runuser").write_text(RUNUSER)
    (stubs / "runuser").chmod(0o755)
    return script, stubs, paths


@pytest.fixture
def home(rendered):
    """A fresh home with the directories claude-agent-setup creates before it runs the script."""
    _, _, paths = rendered
    for path in (paths["home"], paths["operator_memory"].parent):
        shutil.rmtree(path, ignore_errors=True)
    for directory in (".ssh", ".config/git", ".local/bin", "memory"):
        (paths["home"] / directory).mkdir(parents=True)
    paths["operator_memory"].mkdir(parents=True)
    return paths


def run(rendered, check: bool = True) -> subprocess.CompletedProcess:
    script, stubs, _ = rendered
    env = {"PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin"}
    result = subprocess.run(
        ["bash", str(script)], env=env, capture_output=True, text=True
    )
    assert not check or result.returncode == 0, result.stderr
    return result


def mode(path) -> int:
    return path.stat().st_mode & 0o777


def test_both_keys_are_generated_with_their_modes_and_signing_is_allowed(
    rendered, home
):
    run(rendered)
    for key in (home["signing"], home["peer"]):
        assert (mode(key), mode(key.with_suffix(".pub"))) == (0o600, 0o644)
    signers = (home["home"] / ".config" / "git" / "allowed_signers").read_text()
    public = home["signing"].with_suffix(".pub").read_text().strip()
    assert signers.endswith(f' namespaces="git" {public}\n')


def test_a_second_run_keeps_the_keys(rendered, home):
    run(rendered)
    first = home["peer"].read_bytes(), home["signing"].read_bytes()
    run(rendered)
    assert (home["peer"].read_bytes(), home["signing"].read_bytes()) == first


def test_the_seed_copies_the_store_and_strips_the_prefix_as_a_literal(rendered, home):
    # "/home/opXx/server/" matches STRIP only if its "." is read as a regex wildcard.
    (home["operator_memory"] / "MEMORY.md").write_text(
        f"- [a]({STRIP}docs/a.md)\n- [b](/home/opXx/server/b.md)\n"
    )
    (home["operator_memory"] / "note.md").write_text(f"see {STRIP}x.py\n")
    run(rendered)
    index = (home["agent_memory"] / "MEMORY.md").read_text()
    assert index == "- [a](docs/a.md)\n- [b](/home/opXx/server/b.md)\n"
    assert (home["agent_memory"] / "note.md").read_text() == "see x.py\n"
    assert mode(home["agent_memory"] / "note.md") == 0o640
    # The operator's store is only read.
    assert STRIP in (home["operator_memory"] / "note.md").read_text()


def test_an_agent_with_its_own_index_gets_no_copy(rendered, home):
    (home["operator_memory"] / "MEMORY.md").write_text("the operator's\n")
    (home["agent_memory"] / "MEMORY.md").write_text("the agent's\n")
    run(rendered)
    assert (home["agent_memory"] / "MEMORY.md").read_text() == "the agent's\n"


def test_a_copy_that_fails_partway_removes_the_index_so_the_next_run_retries(
    rendered, home
):
    (home["operator_memory"] / "MEMORY.md").write_text("the operator's\n")
    # The agent's cp copies everything, then fails.
    cp = home["home"] / ".local" / "bin" / "cp"
    cp.write_text('#!/bin/sh\n/bin/cp "$@"\nexit 1\n')
    cp.chmod(0o755)
    result = run(rendered, check=False)
    assert result.returncode != 0
    assert "the next run copies again" in result.stderr
    assert not (home["agent_memory"] / "MEMORY.md").exists()
