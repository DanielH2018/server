"""Each host package ships the apt sources its host uses, and only those (H7.24a).

docs/target-architecture/host/h7-delete-ansible.md, old step H7.1. The `apt-docker`,
`apt-azlux` and `apt-github-cli` components replace the checksum and fingerprint tasks of
`docker_install`, `github_cli` and `optimize_pi`, so these tests hold what those tasks held:
the key bytes, and that each source's `Signed-By` names a key the host installs readable by
apt's unprivileged `_apt` user. They also hold that a host does not ship a source it never uses,
because every shipped source is one more index its updater must fetch.
"""

import hashlib
import re

from lib import yaml_fast
from lib.repo_paths import ALL_VARS, HOST_VARS
from renderer.renderer_lib import host as host_mode

SOURCE_DIR = "/etc/apt/sources.list.d/"
# Which hosts use each source, read on 2026-10-24. docker and azlux: only daniel-pi runs the
# docker_install and optimize_pi roles. github-cli: daniel-box runs the github_cli role, and
# daniel-server has `gh` from a hand install (`dpkg-query -W gh` printed 2.103.2 on both).
USES = {
    "docker": {"daniel-pi"},
    "azlux": {"daniel-pi"},
    "github-cli": {"daniel-box", "daniel-server"},
}
# The bytes each key had when its role pinned it. Azlux's role pinned the fingerprint
# 98B824A5 FA7D3A10 FDB225B7 CA548A0A 0312D8E6; this is the sha256 of the file that carries it.
KEY_SHA256 = {
    "/etc/apt/keyrings/docker.asc": "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570",
    "/etc/apt/keyrings/githubcli-archive-keyring.gpg": "6084d5d7bd8e288441e0e94fc6275570895da18e6751f70f057485dc2d1a811b",
    "/usr/share/keyrings/azlux-archive-keyring.gpg": "9767f7060fe0bedf00964813a843c10f5b5d566b5deaf369c18454b8926bf243",
}
HOSTS = ("daniel-box", "daniel-pi", "daniel-server")


def shipped(files: dict[str, tuple[bytes, int]]) -> set[str]:
    """The names of the apt sources a package ships."""
    return {
        path.removeprefix(SOURCE_DIR).removesuffix(".sources")
        for path in files
        if path.startswith(SOURCE_DIR) and path.endswith(".sources")
    }


def misplaced_sources(by_host: dict[str, set[str]]) -> list[str]:
    """Each source a host package ships although its host does not use it."""
    return [
        f"{host} ships the {source} source, which it does not use"
        for host, sources in sorted(by_host.items())
        for source in sorted(
            sources - {s for s, hosts in USES.items() if host in hosts}
        )
    ]


def key_problems(files: dict[str, tuple[bytes, int]]) -> list[str]:
    """Each shipped key whose bytes differ from the pin, and each source whose key is missing or unreadable."""
    problems = [
        f"{path} does not match its pin"
        for path, digest in KEY_SHA256.items()
        if path in files and hashlib.sha256(files[path][0]).hexdigest() != digest
    ]
    for path in sorted(p for p in files if p.startswith(SOURCE_DIR)):
        for key in re.findall(r"^Signed-By: (\S+)$", files[path][0].decode(), re.M):
            if key not in files:
                problems.append(
                    f"{path} names {key}, which the package does not install"
                )
            elif files[key][1] & 0o044 != 0o044:
                problems.append(f"{key} is not readable by the _apt user")
    return problems


def render_files() -> dict[str, dict[str, tuple[bytes, int]]]:
    return {host: host_mode.render_host(host).files for host in HOSTS}


def test_each_real_host_ships_exactly_the_sources_it_uses_with_their_pinned_keys():
    files = render_files()
    assert {host: shipped(f) for host, f in files.items()} == {
        "daniel-pi": {"docker", "azlux"},
        "daniel-box": {"github-cli"},
        "daniel-server": {"github-cli"},
    }
    assert misplaced_sources({host: shipped(f) for host, f in files.items()}) == []
    for host, host_files in files.items():
        assert key_problems(host_files) == [], host
    # The pins are not vacuous: every key is shipped to some host.
    assert set(KEY_SHA256) <= {path for f in files.values() for path in f}


def test_the_use_table_agrees_with_the_inventory():
    group = yaml_fast.safe_load(ALL_VARS.read_text())
    flags = {}
    for host in HOSTS:
        merged = {
            **group,
            **yaml_fast.safe_load((HOST_VARS / f"{host}.yml").read_text()),
        }
        flags[host] = merged
    assert {h for h, v in flags.items() if v["has_docker"]} <= USES["docker"]
    assert {h for h, v in flags.items() if v["has_github_cli"]} <= USES["github-cli"]
    assert group["optimize_pi_host"] in USES["azlux"]


def test_a_source_on_a_host_that_does_not_use_it_is_flagged():
    assert misplaced_sources({"daniel-box": {"github-cli", "docker"}}) == [
        "daniel-box ships the docker source, which it does not use"
    ]
    assert misplaced_sources({"daniel-pi": {"github-cli"}}) == [
        "daniel-pi ships the github-cli source, which it does not use"
    ]


def test_a_flipped_key_byte_an_unreadable_key_and_an_unshipped_key_are_all_flagged():
    files = dict(host_mode.render_host("daniel-pi").files)
    docker = "/etc/apt/keyrings/docker.asc"
    key = bytearray(files[docker][0])
    key[100] ^= 1
    assert key_problems({**files, docker: (bytes(key), 0o644)}) == [
        f"{docker} does not match its pin"
    ]
    assert key_problems({**files, docker: (files[docker][0], 0o640)}) == [
        f"{docker} is not readable by the _apt user"
    ]
    missing = {p: v for p, v in files.items() if p != docker}
    assert key_problems(missing) == [
        f"{SOURCE_DIR}docker.sources names {docker}, which the package does not install"
    ]


def test_no_apt_file_is_a_conffile_so_dpkg_never_prompts_for_an_existing_copy():
    # nfpm marks a conffile with `type: config`. The render writes none.
    for host in HOSTS:
        contents = yaml_fast.safe_load(host_mode.render_host(host).nfpm_spec())[
            "contents"
        ]
        apt = [
            e
            for e in contents
            if e["dst"].startswith(("/etc/apt/", "/usr/share/keyrings/azlux"))
        ]
        assert apt and not [e for e in apt if e.get("type") == "config"], host


def test_no_host_depends_on_a_repos_package_and_daniel_box_gets_nfpm():
    for host in HOSTS:
        assert "homelab-repos" not in host_mode.render_host(host).depends, host
    assert "homelab-tools" in host_mode.render_host("daniel-box").depends


def test_daniel_server_retires_the_hand_installed_github_cli_list():
    legacy = host_mode.render_host("daniel-server").legacy
    assert {"path": "/etc/apt/sources.list.d/github-cli.list"} in legacy
