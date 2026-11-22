"""How aide-originals keeps the package copy of each conffile the lines arm makes line edits to.

apt-get and dpkg-deb are stubs: apt-get leaves a .deb in its working directory, and dpkg-deb
prints a tar that holds the conffile as the package ships it.
"""

import io
import tarfile

import pytest
from aide_originals import seed
from lib.proc_testing import fake_bin, path_with

SHIPPED = b"UMASK\t\t022\n"
# dpkg's status file: tool is installed, gone is removed with its conffile left behind.
STATUS = """\
Package: tool
Status: install ok installed
Version: 1:2.0-1
Conffiles:
 /etc/tool.defs 92874ad1d97bf6eb24017b438bdf3a57
 /etc/tool.old 0123456789abcdef0123456789abcdef obsolete
Description: a tool

Package: gone
Status: deinstall ok config-files
Version: 1.0
Conffiles:
 /etc/gone.conf 0123456789abcdef0123456789abcdef
"""


@pytest.fixture
def host(tmp_path, monkeypatch):
    """The stubbed archive, dpkg's status file, and the state directory, as (status, state)."""
    tar = io.BytesIO()
    with tarfile.open(fileobj=tar, mode="w") as archive:
        member = tarfile.TarInfo("./etc/tool.defs")
        member.size = len(SHIPPED)
        archive.addfile(member, io.BytesIO(SHIPPED))
    (tmp_path / "data.tar").write_bytes(tar.getvalue())
    stubs = fake_bin(
        tmp_path / "bin",
        **{
            "apt-get": f'echo "$@" >> {tmp_path / "fetched"}\ntouch tool_2.0-1_all.deb\n',
            "dpkg-deb": f"cat {tmp_path / 'data.tar'}\n",
        },
    )
    monkeypatch.setenv("PATH", path_with(stubs))
    (tmp_path / "status").write_text(STATUS)
    return tmp_path / "status", tmp_path / "state"


def test_the_installed_versions_copy_is_kept_under_its_package_and_version(host):
    status, state = host
    assert seed(state, status, ["/etc/tool.defs"]) == (1, [])
    assert (state / "tool" / "1:2.0-1" / "etc/tool.defs").read_bytes() == SHIPPED
    assert (status.parent / "fetched").read_text() == "download tool=1:2.0-1\n"


def test_a_fetched_copy_whose_md5_is_not_dpkgs_digest_is_not_kept(host):
    status, state = host
    status.write_text(STATUS.replace("92874ad1d97bf6eb24017b438bdf3a57", "0" * 32))
    assert seed(state, status, ["/etc/tool.defs"]) == (
        0,
        ["/etc/tool.defs (its md5 is not the digest dpkg records)"],
    )
    assert not (state / "tool" / "1:2.0-1" / "etc/tool.defs").exists()


def test_a_version_already_kept_is_not_fetched_again(host):
    status, state = host
    kept = state / "tool" / "1:2.0-1" / "etc/tool.defs"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(SHIPPED)
    assert seed(state, status, ["/etc/tool.defs"]) == (0, [])
    assert not (status.parent / "fetched").exists()


def test_a_kept_copy_whose_md5_is_not_dpkgs_digest_is_fetched_again(host):
    status, state = host
    kept = state / "tool" / "1:2.0-1" / "etc/tool.defs"
    kept.parent.mkdir(parents=True)
    kept.write_bytes(b"UMASK\t\t077\n")
    assert seed(state, status, ["/etc/tool.defs"]) == (1, [])
    assert kept.read_bytes() == SHIPPED


def test_a_path_no_installed_package_lists_as_a_conffile_is_skipped(host):
    status, state = host
    paths = ["/etc/tool.old", "/etc/gone.conf", "/etc/postfix/main.cf"]
    assert seed(state, status, paths) == (0, [])
    assert not (status.parent / "fetched").exists()


def test_a_failed_download_is_named_with_apts_last_error_line(host, monkeypatch):
    status, state = host
    failing = fake_bin(
        status.parent / "failing",
        **{
            "apt-get": "echo 'W: a warning' >&2\necho 'E: 404 Not Found' >&2\nexit 100\n"
        },
    )
    monkeypatch.setenv("PATH", path_with(failing))
    assert seed(state, status, ["/etc/tool.defs"]) == (
        0,
        ["/etc/tool.defs (apt-get exited 100: E: 404 Not Found)"],
    )
    assert not (state / "tool").exists()
