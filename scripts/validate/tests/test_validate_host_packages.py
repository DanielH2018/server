"""The host-package checks CI's `host_packages` job runs: render, diff, lints and triggers.

The maintainer-script lint's line cases live beside the scripts it reads, in
`hosts/lib/tests/test_maintainer_scripts.py`. The `.deb` cases here build their packages with
`dpkg-deb --build`, because the pinned nfpm exists only inside the CI job, whose red-proof step
builds `fixtures/host_packages/red/` with it.

Run: uv run pytest scripts/validate/tests/test_validate_host_packages.py
"""

from pathlib import Path

import pytest
from lib import proc_testing
from lib.repo_paths import REPO
from validate import host_packages

RED = Path(__file__).parent / "fixtures" / "host_packages" / "red"


def _deb(tmp_path: Path, scripts: dict[str, str], conffiles: str = "") -> Path:
    """A minimal `.deb` whose control archive carries `scripts` and `conffiles`."""
    root = tmp_path / "pkg"
    control = root / "DEBIAN"
    control.mkdir(parents=True)
    # dpkg-deb refuses a control directory the session umask left group-writable.
    control.chmod(0o755)
    (control / "control").write_text(
        "Package: homelab-fixture\nVersion: 1.0\nArchitecture: all\n"
        "Maintainer: homelab\nDescription: fixture\n"
    )
    (root / "etc").mkdir()
    (root / "etc" / "fixture.conf").write_text("x\n")
    if conffiles:
        (control / "conffiles").write_text(conffiles)
    for name, text in scripts.items():
        proc_testing.write_exec(control / name, text)
    deb = tmp_path / "debs" / "homelab-fixture.deb"
    deb.parent.mkdir()
    proc_testing.run(
        ["dpkg-deb", "--root-owner-group", "--build", str(root), str(deb)], check=True
    )
    return deb


def test_every_shipped_render_is_clean(tmp_path):
    assert host_packages.render(REPO, tmp_path) == 0
    # Named member: a glob that stopped finding the specs would lint nothing and pass.
    assert (tmp_path / "packages" / "homelab-updater" / "nfpm.yaml").is_file()
    assert host_packages.lint([tmp_path]) == []


def test_a_tree_without_hosts_renders_no_package(tmp_path):
    assert host_packages.render(tmp_path, tmp_path / "out") == 0
    assert not (tmp_path / "out").exists()


@pytest.mark.parametrize("kind", ["config|noreplace", "config"])
def test_a_config_type_is_flagged(kind):
    spec = (RED / "nfpm.yaml").read_text().replace("config|noreplace", kind)
    assert host_packages.lint_config_types(spec) == [
        f"/etc/homelab-red.conf: type {kind!r} ships a conffile; "
        "host packages ship plain files only"
    ]


def test_a_deb_with_plain_files_and_a_whitelisted_postinst_is_clean(tmp_path):
    postinst = "#!/bin/sh\nsystemctl daemon-reload || true\nexit 0\n"
    assert host_packages.lint_deb(_deb(tmp_path, {"postinst": postinst})) == []


def test_a_deb_with_a_conffile_and_a_bad_postinst_is_flagged(tmp_path):
    deb = _deb(
        tmp_path,
        {"postinst": (RED / "scripts" / "postinst").read_text()},
        conffiles="/etc/fixture.conf\n",
    )
    assert host_packages.lint_deb(deb) == [
        "conffiles lists /etc/fixture.conf; host packages ship plain files only",
        "postinst:2: 'rm -rf /etc/foo'",
    ]


def test_nothing_to_lint_is_flagged(tmp_path):
    assert host_packages.lint([tmp_path]) == [
        f"[FAIL] {tmp_path}: no nfpm.yaml or .deb to lint"
    ]


def _write(directory: Path, files: dict[str, tuple[str, int]]) -> None:
    for name, (text, mode) in files.items():
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
        path.chmod(mode)


def test_identical_renders_have_no_diff(tmp_path):
    files = {"nfpm.yaml": ("name: a\n", 0o644), "root/etc/a": ("a\n", 0o644)}
    _write(tmp_path / "base" / "packages" / "a", files)
    _write(tmp_path / "head" / "packages" / "a", files)
    assert host_packages.diff(tmp_path / "base", tmp_path / "head") == []


def test_each_file_change_is_listed(tmp_path):
    _write(
        tmp_path / "base" / "packages" / "a",
        {
            "nfpm.yaml": ("name: a\n", 0o644),
            "root/edited": ("old\n", 0o644),
            "root/dropped": ("x\n", 0o644),
            "root/chmodded": ("x\n", 0o644),
        },
    )
    _write(tmp_path / "base" / "packages" / "gone", {"nfpm.yaml": ("name: g\n", 0o644)})
    _write(
        tmp_path / "head" / "packages" / "a",
        {
            "nfpm.yaml": ("name: a\n", 0o644),
            "root/edited": ("new\n", 0o644),
            "root/chmodded": ("x\n", 0o755),
            "root/added": ("x\n", 0o644),
        },
    )
    _write(
        tmp_path / "head" / "hosts" / "daniel-pi", {"nfpm.yaml": ("name: p\n", 0o644)}
    )

    assert host_packages.diff(tmp_path / "base", tmp_path / "head") == [
        "- `hosts/daniel-pi`: new, 1 files",
        "- `packages/a/root/added`: added",
        "- `packages/a/root/chmodded`: mode 0644 -> 0755",
        "- `packages/a/root/dropped`: removed",
        "- `packages/a/root/edited`: content changed",
        "- `packages/gone`: dropped",
    ]


def test_an_enable_link_is_diffed_by_its_target_even_when_it_dangles(tmp_path):
    # H3.9b's shape: the host package links the updater's timer, absent from its own root.
    wants = "root/usr/lib/systemd/system/timers.target.wants"
    for side in ("base", "head"):
        _write(tmp_path / side / "hosts" / "p", {"nfpm.yaml": ("name: p\n", 0o644)})
        (tmp_path / side / "hosts" / "p" / wants).mkdir(parents=True)
    (tmp_path / "base/hosts/p" / wants / "a.timer").symlink_to("../a.timer")
    (tmp_path / "base/hosts/p" / wants / "b.timer").symlink_to("../a.timer")
    (tmp_path / "head/hosts/p" / wants / "b.timer").symlink_to("../b.timer")
    (tmp_path / "head/hosts/p" / wants / "c.timer").symlink_to("../c.timer")

    assert host_packages.diff(tmp_path / "base", tmp_path / "head") == [
        f"- `hosts/p/{wants}/a.timer`: removed",
        f"- `hosts/p/{wants}/b.timer`: content changed",
        f"- `hosts/p/{wants}/c.timer`: added",
    ]


_WORKFLOW = """\
jobs:
  host_packages:
    steps:
      - name: cases  # step 5
        run: host_package_cases.py --work /tmp/cases
  hooks:
    steps:
      - run: prek run
"""


def test_an_edit_to_the_job_steps_is_a_job_change():
    edited = _WORKFLOW.replace("--work /tmp/cases", "--work /tmp/other")
    assert host_packages.job_differs(_WORKFLOW, edited, "host_packages")


@pytest.mark.parametrize(
    "edited",
    [
        _WORKFLOW.replace("# step 5", "# step five"),
        _WORKFLOW.replace("prek run", "prek run --all-files"),
    ],
    ids=["comment-only", "another-job"],
)
def test_a_comment_or_another_job_is_not_a_job_change(edited):
    assert not host_packages.job_differs(_WORKFLOW, edited, "host_packages")


def test_a_base_without_the_workflow_is_a_job_change():
    assert host_packages.job_differs(None, _WORKFLOW, "host_packages")


def test_an_edit_to_any_named_job_is_a_change():
    """CI names the build job and the cases job, and an edit to either runs the cases (#6151)."""
    edited = _WORKFLOW.replace("prek run", "prek run --all-files")
    assert not host_packages.jobs_differ(_WORKFLOW, edited, ["host_packages"])
    assert host_packages.jobs_differ(_WORKFLOW, edited, ["host_packages", "hooks"])


def test_the_same_tree_renders_the_same_fixture(tmp_path):
    """The payload variant's random payload would make every pair of renders differ."""
    assert host_packages.render_fixtures(REPO, tmp_path)
    # Named member: two empty renders would also compare equal.
    dropin = "root/usr/lib/systemd/system/homelab-updater.timer.d/50-homelab.conf"
    assert (tmp_path / "head" / "hosts" / "daniel-pi" / dropin).is_file()
    assert not host_packages.fixture_changed(REPO, REPO)


def test_a_base_without_the_fixture_module_is_a_fixture_change(tmp_path):
    assert host_packages.fixture_changed(tmp_path, REPO)


# ── fetch-tools: the pins of every rendered homelab-tools tree, fetched and verified ─────


def _tools_render(tmp_path: Path, body: bytes) -> Path:
    """A render holding the real homelab-tools trees, their pins repointed at `body`'s digest."""
    import hashlib
    import json

    out = tmp_path / "render"
    assert host_packages.render(REPO, out) == 0
    digest = hashlib.sha256(body).hexdigest()
    pins = sorted(out.rglob("tools.json"))
    for path in pins:
        data = json.loads(path.read_text())
        for pin in data.values():
            pin.update(sha256=digest, url="https://example.invalid/x")
            pin.pop("member", None)
        path.write_text(json.dumps(data))
    # Named member: a render that held no tools tree would fetch nothing and pass.
    assert len(pins) == 2
    return out


class _Body:
    def __init__(self, body: bytes):
        self.body = body

    def __call__(self, request, timeout=None):
        import io

        return io.BytesIO(self.body)


def test_a_pin_whose_download_matches_is_placed_in_every_architecture(tmp_path):
    render = _tools_render(tmp_path, b"binary")
    problems = host_packages.fetch_tools(render, tmp_path / "cache", _Body(b"binary"))
    assert problems == []
    placed = sorted(
        p.relative_to(render).parts[2] for p in render.rglob("usr/lib/homelab/bin/sops")
    )
    assert placed == ["amd64", "arm64"]


def test_a_pin_whose_download_differs_is_flagged_for_every_architecture(tmp_path):
    render = _tools_render(tmp_path, b"binary")
    problems = host_packages.fetch_tools(render, tmp_path / "cache", _Body(b"other"))
    assert len(problems) == 2
    assert all(p.startswith("[FAIL] ") and "the pin names" in p for p in problems)
    assert not list(render.rglob("usr/lib/homelab/bin/sops"))
