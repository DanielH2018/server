"""One cluster-node bootstrap case leaves k3s unseeded and requires install.sh's download (#7754).

The log lines are the ones the pinned install.sh prints: `info()` echoes `[INFO] ` and its
arguments, `download_binary` names the release binary's URL, and `verify_binary` follows. With
INSTALL_K3S_SKIP_DOWNLOAD set, `download_and_verify` prints its skip line instead.

Run: uv run pytest scripts/validate/tests/test_bootstrap_k3s_download_case.py
"""

from validate.validate_lib import bootstrap_case as case

SCRIPT = case.SCRIPT.read_text()
RELEASE = "https://github.com/k3s-io/k3s/releases/download/" + case.script_value(
    SCRIPT, "K3S_VERSION"
).replace("+", "%2B")
DOWNLOADED = (
    f"{case.ANCHOR_ACCEPTS}\n"
    f"[INFO]  Downloading hash {RELEASE}/sha256sum-amd64.txt\n"
    f"[INFO]  Downloading binary {RELEASE}/k3s\n"
    "[INFO]  Verifying binary download\n"
)
SKIPPED = (
    f"{case.ANCHOR_ACCEPTS}\n"
    "[INFO]  Skipping k3s download and verify\n"
    "[INFO]  Skipping installation of SELinux RPM\n"
)


def _verdict(item: case.Planned, stdout: str) -> list[str]:
    field = "absent"
    return case.output_problems(
        0, stdout, "", item.marker or "", getattr(item, field, None)
    )


def test_one_cluster_node_case_requires_install_sh_s_binary_download(tmp_path):
    green = tmp_path / "bootstrap-kit" / "green"
    green.mkdir(parents=True)
    online_nodes = [
        item
        for item in case.plan(tmp_path, green)
        if item.host in case.UNITS and item.kit is None and item.refusal is None
    ]

    downloading = [
        item
        for item in online_nodes
        if _verdict(item, DOWNLOADED) == [] and _verdict(item, SKIPPED) != []
    ]

    assert len(downloading) == 1, (
        "no single cluster-node --local-build case accepts a log showing install.sh's"
        " binary download and flags one without it: "
        f"{[(item.name, item.marker) for item in online_nodes]}"
    )
