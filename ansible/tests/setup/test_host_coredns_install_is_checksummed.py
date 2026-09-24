"""Guard: the host forwarder's CoreDNS tarball is verified against its pinned sha256.

WHY. `k3s_host_coredns_sha256` is the only authenticity check on a binary that serves every
DNS lookup on daniel-box. Until #2391 nothing in Ansible read it: `unarchive` fetched
`k3s_host_coredns_url` itself, and `unarchive` with a URL source takes no checksum. The pin
was hand-updated on every Renovate bump and checked by nothing on the host.

WHY IT NEEDS A GUARD. Dropping the checksum, or pointing `unarchive` back at the URL, renders,
lints and deploys green. The second shape keeps a checksummed `get_url` in the file while the
extracted binary comes from an unverified fetch, so a reader sees the check and trusts it.
"""

from _helpers import ROLES as _ROLES
from _helpers import load_tasks, leaf_tasks

_NODE_TASKS = _ROLES / "setup/k3s/tasks/node.yml"
_GET_URL = "ansible.builtin.get_url"
_UNARCHIVE = "ansible.builtin.unarchive"
_URL_VAR = "k3s_host_coredns_url"
_SHA_VAR = "k3s_host_coredns_sha256"


def checksum_problems(tasks: list[dict]) -> list[str]:
    """What stops the extracted CoreDNS binary from being the pinned artifact; empty when sound."""
    leaves = leaf_tasks(tasks)
    downloads = [
        t[_GET_URL]
        for t in leaves
        if _URL_VAR in str(t.get(_GET_URL, {}).get("url", ""))
    ]
    extracts = [t[_UNARCHIVE] for t in leaves if _UNARCHIVE in t]
    if not downloads:
        return [f"no get_url downloads {_URL_VAR}"]
    problems = []
    download = downloads[0]
    if _SHA_VAR not in str(download.get("checksum", "")):
        problems.append(f"the get_url of {_URL_VAR} does not check {_SHA_VAR}")
    for extract in extracts:
        src = str(extract.get("src", ""))
        if _URL_VAR in src:
            problems.append(
                f"unarchive fetches {_URL_VAR} itself, bypassing the checksum"
            )
        elif src != str(download.get("dest")):
            problems.append(
                f"unarchive reads {src!r}, not the verified {download.get('dest')!r}"
            )
    if not extracts:
        problems.append("no unarchive extracts the downloaded tarball")
    return problems


def _download(checksum: str | None) -> dict:
    task: dict = {
        "url": "{{ k3s_host_coredns_url }}",
        "dest": "{{ k3s_host_coredns_tarball }}",
    }
    if checksum is not None:
        task["checksum"] = checksum
    return {"name": "download", _GET_URL: task}


def _extract(src: str) -> dict:
    return {"name": "extract", _UNARCHIVE: {"src": src, "remote_src": True}}


def test_the_live_node_tasks_verify_the_tarball() -> None:
    assert checksum_problems(load_tasks(_NODE_TASKS)) == []


def test_a_checksummed_download_feeding_unarchive_is_clean() -> None:
    tasks = [
        _download("sha256:{{ k3s_host_coredns_sha256 }}"),
        _extract("{{ k3s_host_coredns_tarball }}"),
    ]
    assert checksum_problems(tasks) == []


def test_a_download_without_its_checksum_is_flagged() -> None:
    tasks = [_download(None), _extract("{{ k3s_host_coredns_tarball }}")]
    assert checksum_problems(tasks) == [
        f"the get_url of {_URL_VAR} does not check {_SHA_VAR}"
    ]


def test_unarchive_fetching_the_url_itself_is_flagged() -> None:
    """The shape that shipped, plus a checksummed download nothing consumes."""
    tasks = [
        _download("sha256:{{ k3s_host_coredns_sha256 }}"),
        _extract("{{ k3s_host_coredns_url }}"),
    ]
    assert checksum_problems(tasks) == [
        f"unarchive fetches {_URL_VAR} itself, bypassing the checksum"
    ]
