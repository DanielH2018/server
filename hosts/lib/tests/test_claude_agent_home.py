"""The agent's home, written by `claude-agent-home` and `claude-agent-setup` in a temporary home (H7.40).

hosts/components/claude-code ships `claude-agent-setup`, which runs as root, and the
`claude-agent-home` it calls. Each test renders daniel-box's package with the agent's home, keys
and memory stores moved under a temporary root and the agent and operator set to the user running
the test, then runs the script with a `runuser` stub that drops its user argument. ssh-keygen, sed,
cp, install and chown are the real ones.

The agent owns its home and can replace a path there with a link at any moment, so root must not
act on such a path by name. The tests hold that two ways: a scan of the rendered text for a root
command aimed at a path in the home, and runs of the setup script over planted links.

Run: uv run pytest hosts/lib/tests/test_claude_agent_home.py
"""

import getpass
import grp
import os
import re
import shutil
import stat
import subprocess

import pytest
import yaml

from lib.estate import Inventory
from lib.repo_paths import REPO
from renderer.renderer_lib import host as host_mode

SCRIPT = "/usr/lib/homelab/claude-agent-home"
SETUP = "/usr/lib/homelab/claude-agent-setup"
# The operator's checkout prefix, with a `.` a regex would read as any character.
STRIP = "/home/op.x/server/"
RUNUSER = '#!/bin/sh\n[ "$1" = -u ] && shift 2\n[ "$1" = -- ] && shift\nexec "$@"\n'


@pytest.fixture(scope="module")
def rendered(tmp_path_factory):
    """The script and the paths it renders for, shared by every test in the module."""
    root = tmp_path_factory.mktemp("render").resolve()
    home = root / "home"
    paths = {
        "home": home,
        "templates": root / "templates",
        "operator_home": root / "operator-home",
        "signing": home / ".ssh" / "git_signing_ed25519",
        "peer": home / ".ssh" / "peers_ed25519",
        "agent_memory": home / ".claude" / "projects" / "-srv-clone" / "memory",
        "operator_memory": root / "operator" / "memory",
    }
    host_vars = root / "host_vars"
    shutil.copytree(REPO / "ansible" / "inventory" / "host_vars", host_vars)
    path = host_vars / "daniel-box.yml"
    overrides = {
        "claude_code_agent_user": getpass.getuser(),
        "sys_user": getpass.getuser(),
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
    paths["raw"] = {name: package.files[name][0].decode() for name in (SETUP, SCRIPT)}
    # The setup script's fixed paths move under the temporary root.
    paths["setup_text"] = (
        paths["raw"][SETUP]
        .replace(
            "templates=/usr/lib/homelab/claude-agent", f"templates={paths['templates']}"
        )
        .replace(SCRIPT, str(script))
        .replace("/home/$operator/", f"{paths['operator_home']}/")
    )
    paths["setup"] = root / "claude-agent-setup"
    paths["setup"].write_text(paths["setup_text"])
    stubs = root / "stubs"
    stubs.mkdir()
    (stubs / "runuser").write_text(RUNUSER)
    for name in ("usermod", "groupadd", "getent", "loginctl", "setfacl"):
        (stubs / name).write_text("#!/bin/sh\nexit 0\n")
    for stub in stubs.iterdir():
        stub.chmod(0o755)
    return script, stubs, paths


@pytest.fixture
def home(rendered):
    """A fresh home with the directories claude-agent-setup creates before it runs the script."""
    _, _, paths = rendered
    for path in (paths["home"], paths["operator_memory"].parent):
        shutil.rmtree(path, ignore_errors=True)
    for directory in (".ssh", ".config/git", ".local/bin"):
        (paths["home"] / directory).mkdir(parents=True)
    paths["agent_memory"].mkdir(parents=True)
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


# Root runs both scripts, and the agent owns every directory below its home. A root command that
# names such a path follows a link the agent put there. These tests scan the rendered text for
# one, and run claude-agent-setup over planted links.
ROOT_WRITES = {
    "chmod",
    "chown",
    "cp",
    "install",
    "ln",
    "mkdir",
    "mv",
    "rm",
    "rsync",
    "sed",
    "setfacl",
    "tee",
    "touch",
}
# Commands that take their destination last; the rest take every path after their first word.
DESTINATION_LAST = {"cp", "install", "ln", "mv", "rsync"}
OPTIONS_WITH_A_VALUE = {"-o", "-g", "-m", "--owner", "--group", "--mode"}
# A variable the scripts bind to a path in the agent's home, or a positional parameter a helper
# receives such a path in.
AGENT_PATH = re.compile(r"\$\{?(home|clone|key|peer_key|agent_memory|shared|[1-9])\b")
# A path reached through a directory the script opened and checked.
OPEN_HANDLE = '"$pinned'


def root_writes_by_name(text: str) -> list[str]:
    """The root commands whose target is a path in the agent's home, reached by its name."""
    found = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith(("#", "{%")):
            continue
        for segment in re.split(r"&&|\|\||[|;]|(?:^|\s)[{}](?:\s|$)", line):
            words = segment.split()
            if not words or words[0] not in ROOT_WRITES:
                continue
            positional = [
                word
                for index, word in enumerate(words[1:])
                if not word.startswith("-")
                and (index == 0 or words[index] not in OPTIONS_WITH_A_VALUE)
            ]
            if words[0] in DESTINATION_LAST:
                targets = words[-1:]
            elif words[0] in {"chmod", "chown", "sed"}:
                targets = positional[1:]
            else:
                targets = positional
            found += [
                f"{line}  <- {target}"
                for target in targets
                if AGENT_PATH.search(target) and not target.startswith(OPEN_HANDLE)
            ]
    return found


# Lines the setup and home scripts held before root stopped naming the agent's paths.
NAMED_PATH_LINES = [
    'dir() { install -d -o "$agent" -g "$operator" -m "$1" "$2"; }',
    'put() { install -o "$agent" -g "$operator" -m "$1" "$templates/$2" "$home/$3"; }',
    'chown "$agent:$operator" "$key" "$key.pub"',
    'chmod 0600 "$key"',
    'rm -f "$home/.config/homelab-ui/credentials.json"',
    'setfacl --default --modify "group:$operator:rX" "$shared"',
    'install -o "$agent" -g "$operator" -m 0600 "/home/$operator/.kube/config" "$home/.kube/config"',
    'if ! { rsync -rLptog --chown="$agent:$operator" "$operator_memory/." "$agent_memory/" &&',
]
# Lines that are safe: the agent acts, or root acts on an open handle or on a path of its own.
SAFE_LINES = [
    'as_agent chmod 0600 "$1"',
    'install -T -o "$1" -g "$2" -m "$3" "$4" "$pinned/${5##*/}"',
    'chown "$agent:$operator" "$pinned"',
    'rm -rf "$staging"',
    'chown -R "$agent" "$staging"',
]


@pytest.mark.parametrize("line", NAMED_PATH_LINES)
def test_the_scan_flags_a_root_command_that_names_a_path_in_the_agents_home(line):
    assert root_writes_by_name(line)


@pytest.mark.parametrize("line", SAFE_LINES)
def test_the_scan_passes_the_agent_acting_and_root_on_a_handle_or_its_own_path(line):
    assert root_writes_by_name(line) == []


@pytest.mark.parametrize("script", [SETUP, SCRIPT])
def test_no_root_command_names_a_path_in_the_agents_home(rendered, script):
    _, _, paths = rendered
    assert root_writes_by_name(paths["raw"][script]) == []


@pytest.fixture
def agent_home(rendered):
    """A fresh home holding only what the users arm leaves, a decoy beside it, and the inputs."""
    _, _, paths = rendered
    try:
        grp.getgrnam(getpass.getuser())
    except KeyError:
        pytest.skip(
            "the setup script names the operator's group by the operator's name"
        )
    root = paths["home"].parent
    decoy = root / "decoy"
    for path in (paths["home"], paths["templates"], paths["operator_home"], decoy):
        shutil.rmtree(path, ignore_errors=True)
    paths["templates"].mkdir()
    for name in (
        "profile",
        "gitconfig",
        "CLAUDE.md",
        "chezmoi-seed.toml",
        "ssh-config",
    ):
        (paths["templates"] / name).write_text(f"{name}\n")
    (paths["operator_home"] / ".kube").mkdir(parents=True)
    (paths["operator_home"] / ".kube" / "config").write_text("kubeconfig\n")
    # The clone and the dotfiles source exist, so the script reaches for no network.
    (paths["home"] / "server" / ".git").mkdir(parents=True)
    (paths["home"] / ".local" / "share" / "chezmoi" / ".git").mkdir(parents=True)
    decoy.mkdir(mode=0o700)
    (decoy / "keep").write_text("keep\n")
    return {**paths, "decoy": decoy}


def run_setup(rendered) -> subprocess.CompletedProcess:
    _, stubs, paths = rendered
    env = {"PATH": f"{stubs}:/usr/local/bin:/usr/bin:/bin"}
    return subprocess.run(
        ["bash", str(paths["setup"])],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
    )


def untouched(decoy) -> bool:
    return (
        stat.S_IMODE(decoy.stat().st_mode) == 0o700
        and sorted(entry.name for entry in decoy.iterdir()) == ["keep"]
        and (decoy / "keep").read_text() == "keep\n"
    )


def test_the_setup_gives_each_path_its_mode_and_a_second_run_changes_nothing(
    rendered, agent_home
):
    home = agent_home["home"]
    for _ in range(2):
        result = run_setup(rendered)
        assert result.returncode == 0, result.stderr
    modes = {
        ".": 0o750,
        ".ssh": 0o750,
        ".local/bin": 0o755,
        ".config/homelab-ui": 0o710,
        ".claude/artifacts": 0o750,
        ".profile": 0o644,
        ".config/chezmoi/chezmoi.toml": 0o600,
        ".kube/config": 0o600,
    }
    assert {name: mode(home / name) for name in modes} == modes
    # The home and the shared directories are setgid, so the operator's group reaches new files.
    for name in (".", ".claude/artifacts"):
        assert (home / name).stat().st_mode & stat.S_ISGID
    assert (home / ".kube" / "config").read_text() == "kubeconfig\n"
    assert (home / ".profile").read_text() == "profile\n"


def plant_link(home, relative, decoy):
    (home / relative).parent.mkdir(parents=True, exist_ok=True)
    os.symlink(decoy, home / relative)


@pytest.mark.parametrize(
    "relative", [".ssh", ".config", ".config/homelab-ui", ".claude/artifacts"]
)
def test_a_link_where_a_directory_belongs_fails_the_run_and_leaves_its_target(
    rendered, agent_home, relative
):
    plant_link(agent_home["home"], relative, agent_home["decoy"])
    result = run_setup(rendered)
    assert result.returncode != 0
    assert "link" in result.stderr
    assert untouched(agent_home["decoy"])


def test_a_fifo_where_a_directory_belongs_fails_the_run_instead_of_blocking(
    rendered, agent_home
):
    fifo = agent_home["home"] / ".config" / "homelab-ui"
    fifo.parent.mkdir(parents=True)
    os.mkfifo(fifo)
    assert run_setup(rendered).returncode != 0


def test_a_link_at_a_file_the_setup_writes_is_replaced_and_its_target_left_alone(
    rendered, agent_home
):
    target = agent_home["decoy"] / "target"
    target.write_text("keep\n")
    os.symlink(target, agent_home["home"] / ".profile")
    result = run_setup(rendered)
    assert result.returncode == 0, result.stderr
    assert target.read_text() == "keep\n"
    assert not (agent_home["home"] / ".profile").is_symlink()
    assert (agent_home["home"] / ".profile").read_text() == "profile\n"


def test_a_link_at_the_login_the_setup_removes_is_removed_and_its_target_left_alone(
    rendered, agent_home
):
    # daniel-box leaves the peer login off, so the setup removes the file.
    login = agent_home["home"] / ".ssh" / "authorized_keys"
    login.parent.mkdir()
    os.symlink(agent_home["decoy"] / "keep", login)
    result = run_setup(rendered)
    assert result.returncode == 0, result.stderr
    assert not login.is_symlink() and not login.exists()
    assert untouched(agent_home["decoy"])
