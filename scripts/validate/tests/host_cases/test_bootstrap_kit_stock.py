"""The bootstrap cases' kit downloads, the `--tools-seed` directory and the offline container's apt
retries, without a container.

Run: uv run pytest scripts/validate/tests/host_cases/test_bootstrap_kit_stock.py
"""

import email.message
import hashlib
import io
import subprocess
import urllib.error
from types import SimpleNamespace
from typing import ClassVar

import pytest
from lib import proc_testing
from validate.validate_lib import bootstrap_case as case
from validate.validate_lib.host_container import CaseFailed, Container


class _Body(io.BytesIO):
    headers: ClassVar[dict[str, str]] = {}


def test_a_kit_download_retries_a_504_and_lands_at_its_name(tmp_path):
    body = b"binary"
    outcomes = [
        urllib.error.HTTPError("u", 504, "Gateway", email.message.Message(), None),
        _Body(body),
    ]

    def opener(request, timeout=None):
        outcome = outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    dest = tmp_path / "nfpm.tar.gz"
    case._download(
        "https://example.invalid/nfpm.tar.gz",
        hashlib.sha256(body).hexdigest(),
        dest,
        tmp_path / "cache",
        opener,
        sleep=lambda _: None,
    )
    assert dest.read_bytes() == body


def test_the_seed_carries_the_kit_s_k3s_and_checksum_list_beside_its_tools_cache(
    tmp_path,
):
    kit = tmp_path / "kit"
    (kit / "tools-cache").mkdir(parents=True)
    (kit / "tools-cache" / ("a" * 64)).write_bytes(b"nfpm")
    (kit / "k3s").write_text("stand-in")
    (kit / "sha256sum-amd64.txt").write_text(f"{'b' * 64}  k3s\n")
    stale = tmp_path / "seed" / "stale"
    stale.parent.mkdir()
    stale.write_text("left from an earlier run")

    seed = case.stock_seed(kit, tmp_path / "seed")

    assert sorted(p.name for p in seed.iterdir()) == [
        "a" * 64,
        "k3s",
        "sha256sum-amd64.txt",
    ]
    assert (seed / "k3s").read_text() == "stand-in"
    assert not (kit / "tools-cache" / "k3s").exists()


def test_a_kit_seeded_from_the_job_s_cache_reads_a_pin_with_no_request(tmp_path):
    body = b"binary"
    digest = hashlib.sha256(body).hexdigest()
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / digest).write_bytes(body)
    kit = tmp_path / "kit"
    kit.mkdir()
    cases = SimpleNamespace(tools_cache=seed, work=tmp_path / "work")

    def refuse_a_request(request, timeout=None):
        raise AssertionError(f"requested {request.full_url}")

    dest = kit / "nfpm.tar.gz"
    case._download(
        "https://example.invalid/nfpm.tar.gz",
        digest,
        dest,
        case.seed_kit(cases, kit),
        refuse_a_request,
    )
    assert dest.read_bytes() == body
    assert (kit / "tools-cache" / digest).read_bytes() == body


def test_a_kit_with_no_job_cache_downloads_into_its_own_directory(tmp_path):
    cases = SimpleNamespace(tools_cache=None, work=tmp_path / "work")

    assert case.seed_kit(cases, tmp_path) == tmp_path / "work/bootstrap-kit-downloads"
    assert not (tmp_path / "tools-cache").exists()


def test_a_kit_download_differing_from_its_pin_is_flagged(tmp_path):
    dest = tmp_path / "nfpm.tar.gz"
    with pytest.raises(CaseFailed, match="the pin names"):
        case._download(
            "https://example.invalid/nfpm.tar.gz",
            hashlib.sha256(b"binary").hexdigest(),
            dest,
            tmp_path / "cache",
            lambda request, timeout=None: _Body(b"other"),
        )
    assert not dest.exists()


def test_the_kit_s_uv_sync_skips_the_lint_group(tmp_path):
    clone = tmp_path / "clone"
    (clone / "estate").mkdir(parents=True)
    (clone / "estate/vars.yml").write_text('host_python_version: "3.15.2"\n')
    calls = []

    case.stock_uv_cache(
        SimpleNamespace(work=tmp_path / "work"),
        clone,
        tmp_path / "kit",
        "uv",
        run=lambda argv, **_: calls.append(argv),
    )

    sync = next(argv for argv in calls if argv[1] == "sync")
    assert sync[sync.index("--no-group") + 1] == "lint"


def test_only_an_offline_run_turns_off_apt_s_retries(monkeypatch, tmp_path):
    written = {}

    def fake_ok(self, command):
        written.setdefault(self.name, []).append(command)
        return ""

    monkeypatch.setattr(Container, "boot", lambda self, python: None)
    monkeypatch.setattr(Container, "ok", fake_ok)
    monkeypatch.setattr(
        Container,
        "sh",
        lambda self, command, timeout=None: subprocess.CompletedProcess(
            command, 0, "", ""
        ),
    )
    cases = SimpleNamespace(image="img", work=tmp_path, python="3.12")
    kit = tmp_path / "kits" / "kit"

    case.Bootstrap(cases, "daniel-box", tmp_path, kit, "offline").run()
    case.Bootstrap(cases, "daniel-box", tmp_path, None, "box").run()

    online = written["homelab-host-test-bootstrap-box"]
    assert not [c for c in online if case.NO_RETRIES in c]
    offline = written["homelab-host-test-bootstrap-offline"]
    (write,) = [c for c in offline if case.NO_RETRIES in c]
    conf = tmp_path / "no-retries.conf"
    proc_testing.run(write.replace(case.NO_RETRIES, str(conf)), shell=True, check=True)
    parsed = proc_testing.run(
        ["apt-config", "-c", str(conf), "shell", "R", "Acquire::Retries"], check=True
    )
    assert parsed.stdout.strip() == "R='0'"
