"""`hosts/bootstrap-host.sh --tools-seed` on a fake host, online with a stub curl.

Run: uv run pytest hosts/lib/tests/test_bootstrap_host_seed.py
"""

from pathlib import Path

from _bootstrap_host import NFPM_ARCHIVE, UV_ARCHIVE, FakeHost, sha256


def _online_box_with_a_seed(tmp_path) -> tuple[FakeHost, Path, Path]:
    """A stocked online daniel-box, its empty seed directory, and the file curl logs URLs to."""
    host = FakeHost(tmp_path, hostname="daniel-box")
    host.stock_kit()
    seed = tmp_path / "seed"
    seed.mkdir()
    requests = tmp_path / "curl.requests"
    host._stub(
        "curl",
        'for arg; do [ "$prev" = -o ] && dest=$arg; prev=$arg; url=$arg; done\n'
        f'echo "${{url##*/}}" >> {requests}\n'
        f'cp "{host.kit}/${{url##*/}}" "$dest"',
    )
    return host, seed, requests


def test_a_tools_seed_stands_in_for_each_pin_it_holds_and_the_rest_are_downloaded(
    tmp_path,
):
    host, seed, requests = _online_box_with_a_seed(tmp_path)
    nfpm = (host.kit / NFPM_ARCHIVE).read_bytes()
    (seed / sha256(nfpm)).write_bytes(nfpm)
    (seed / "homelab-tools-pin").write_text("a pin only fetch-tools reads")

    result = host.run(
        "daniel-box",
        "--local-build",
        str(host.clone(tmp_path)),
        "--tools-seed",
        str(seed),
    )

    assert result.returncode == 0, result.stderr
    fetched = requests.read_text().splitlines()
    assert NFPM_ARCHIVE not in fetched
    assert UV_ARCHIVE in fetched
    assert "homelab-tools-pin" in (host.root / "tools-cache.listing").read_text()


def test_a_tools_seed_entry_that_differs_from_its_pin_is_refused(tmp_path):
    host, seed, requests = _online_box_with_a_seed(tmp_path)
    digest = sha256((host.kit / NFPM_ARCHIVE).read_bytes())
    (seed / digest).write_bytes(b"not the pinned nfpm")

    result = host.run(
        "daniel-box",
        "--local-build",
        str(host.clone(tmp_path)),
        "--tools-seed",
        str(seed),
    )

    assert result.returncode == 1
    assert f"{NFPM_ARCHIVE} does not match its pinned sha256" in result.stderr
    assert NFPM_ARCHIVE not in requests.read_text().splitlines()


def test_a_tools_seed_that_is_not_a_directory_is_refused_before_any_step(tmp_path):
    host = FakeHost(tmp_path, hostname="daniel-box")

    result = host.run("daniel-box", "--tools-seed", str(tmp_path / "missing"))

    assert result.returncode == 1
    assert "--tools-seed:" in result.stderr
    assert host.written() == []
