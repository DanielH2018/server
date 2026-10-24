"""`homelab-repos` ships the third-party apt sources and keys the roles used to write (H7.24a).

docs/target-architecture/host/h7-delete-ansible.md, old step H7.1. The package replaces the
checksum and fingerprint tasks of `docker_install`, `github_cli` and `optimize_pi`, so these
tests hold what those tasks held: the key bytes, and that each source's `Signed-By` names a
key the package installs readable by apt's unprivileged `_apt` user.
"""

import hashlib
import re

from lib import yaml_fast
from renderer.renderer_lib import host as host_mode

# The paths the three roles wrote, so dpkg takes each over.
SOURCES = {
    "/etc/apt/sources.list.d/docker.sources",
    "/etc/apt/sources.list.d/github-cli.sources",
    "/etc/apt/sources.list.d/azlux.sources",
}
# The bytes each key had when its role pinned it. Azlux's role pinned the fingerprint
# 98B824A5 FA7D3A10 FDB225B7 CA548A0A 0312D8E6; this is the sha256 of the file that carries it.
KEY_SHA256 = {
    "/etc/apt/keyrings/docker.asc": "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570",
    "/etc/apt/keyrings/githubcli-archive-keyring.gpg": "6084d5d7bd8e288441e0e94fc6275570895da18e6751f70f057485dc2d1a811b",
    "/usr/share/keyrings/azlux-archive-keyring.gpg": "9767f7060fe0bedf00964813a843c10f5b5d566b5deaf369c18454b8926bf243",
}


def key_problems(files: dict[str, tuple[bytes, int]]) -> list[str]:
    """Each key whose bytes differ from the pin, and each source whose key is missing or unreadable."""
    problems = [
        f"{path} does not match its pin"
        for path, digest in KEY_SHA256.items()
        if path not in files or hashlib.sha256(files[path][0]).hexdigest() != digest
    ]
    for path in sorted(SOURCES & set(files)):
        for key in re.findall(r"^Signed-By: (\S+)$", files[path][0].decode(), re.M):
            if key not in files:
                problems.append(
                    f"{path} names {key}, which the package does not install"
                )
            elif files[key][1] & 0o044 != 0o044:
                problems.append(f"{key} is not readable by the _apt user")
    return problems


def test_the_real_package_holds_every_key_pin_and_names_only_shipped_keys():
    files = host_mode.render_package("homelab-repos").files
    assert key_problems(files) == []
    assert set(files) == SOURCES | set(KEY_SHA256)


def test_a_flipped_key_byte_and_an_unshipped_key_are_both_flagged():
    files = dict(host_mode.render_package("homelab-repos").files)
    docker = "/etc/apt/keyrings/docker.asc"
    key = bytearray(files[docker][0])
    key[100] ^= 1
    flipped = {**files, docker: (bytes(key), 0o644)}
    assert key_problems(flipped) == [f"{docker} does not match its pin"]
    unreadable = {**files, docker: (files[docker][0], 0o640)}
    assert key_problems(unreadable) == [f"{docker} is not readable by the _apt user"]
    missing = {p: v for p, v in files.items() if p != docker}
    assert key_problems(missing) == [
        f"{docker} does not match its pin",
        f"/etc/apt/sources.list.d/docker.sources names {docker}, which the package does not install",
    ]


def test_no_file_is_a_conffile_so_dpkg_never_prompts_for_an_existing_copy():
    # nfpm marks a conffile with `type: config`. The render writes none.
    package = host_mode.render_package("homelab-repos")
    contents = yaml_fast.safe_load(package.nfpm_spec())["contents"]
    assert {entry["dst"] for entry in contents} == SOURCES | set(KEY_SHA256)
    assert not [entry for entry in contents if entry.get("type") == "config"]


def test_every_host_package_depends_on_the_repos_package():
    for host in ("daniel-box", "daniel-pi", "daniel-server"):
        assert "homelab-repos" in host_mode.render_host(host).depends, host
    # daniel-box runs the builder, so it needs nfpm from homelab-tools.
    assert "homelab-tools" in host_mode.render_host("daniel-box").depends


def test_daniel_server_retires_the_hand_installed_github_cli_list():
    legacy = host_mode.render_host("daniel-server").legacy
    assert {"path": "/etc/apt/sources.list.d/github-cli.list"} in legacy
