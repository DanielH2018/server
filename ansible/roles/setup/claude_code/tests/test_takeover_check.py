"""The D5 takeover check: a root wrapper that may only dry-run, behind one sudoers line (#5494).

The wrapper runs here as an ordinary user against fake kubectl, flux and uv binaries, which
record what they were called with. Nothing reaches a cluster. The root-only branch is exercised
by running a copy whose `id -u` answers 0.

Run: uv run pytest ansible/roles/setup/claude_code/tests/test_takeover_check.py
"""

import os
import pwd
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
from _helpers import ANSIBLE
from lib.proc_testing import path_with, run as run_proc, write_exec

ROLE = ANSIBLE / "roles" / "setup" / "claude_code"
SCRIPT = ROLE / "files" / "homelab-takeover-check.sh"
WRAPPER = "/usr/local/sbin/homelab-takeover-check"
USER = pwd.getpwuid(os.getuid()).pw_name

FAKE_KUBECTL = """#!/usr/bin/bash
{
  echo "argv: $*"
  echo "env: KUBECTL_EXTERNAL_DIFF=${KUBECTL_EXTERNAL_DIFF-unset} KUBECONFIG=$KUBECONFIG PATH=$PATH"
  echo "umask: $(umask)"
} >> "$FAKE_LOG"
case "$1" in
  diff) cat "$FAKE_DIFF_OUT" ; exit "${FAKE_DIFF_RC:-1}" ;;
  get | apply) echo '{"kind":"List","items":[]}' ;;
esac
"""
FAKE_FLUX = """#!/usr/bin/bash
echo "flux argv: $*" >> "$FAKE_LOG"
echo "► Kustomization/flux-system/demo created"
exit 1
"""
FAKE_UV = """#!/usr/bin/bash
echo "uv argv: $* as $(id -un) in $PWD" >> "@LOG@"
echo "census output"
"""
# A recorded `kubectl diff` of a Secret and a ConfigMap whose script embeds a credential.
SECRET_DIFF = """\
diff -u -N /tmp/LIVE-1/v1.Secret.demo.creds /tmp/MERGED-1/v1.Secret.demo.creds
--- /tmp/LIVE-1/v1.Secret.demo.creds\t2026-10-17 10:00:00
+++ /tmp/MERGED-1/v1.Secret.demo.creds\t2026-10-17 10:00:01
@@ -1,6 +1,8 @@
 apiVersion: v1
 data:
+  password: sentinel-one
-  oldkey: sentinel-two
+  api_key: "sentinel-three"
 kind: Secret
+  echo sentinel-four
"""
SECRET_VALUES = [
    "sentinel-one",
    "sentinel-two",
    "sentinel-three",
    "sentinel-four",
]


@pytest.fixture
def home_dir():
    """A scratch directory under the real home, which is where the wrapper allows paths."""
    home = Path(os.path.realpath(Path.home()))
    if not os.access(home, os.W_OK):
        pytest.skip("home is not writable")
    root = Path(tempfile.mkdtemp(prefix="takeover-check-test-", dir=home))
    yield root
    shutil.rmtree(root, ignore_errors=True)


class Rig:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.render = root / "render"
        self.src = root / "src"
        self.bin = root / "bin"
        self.state = root / "state"
        self.log = root / "calls.log"
        self.diff_out = root / "diff.out"
        self.render.mkdir()
        self.src.mkdir()
        self.bin.mkdir()
        self.log.write_text("")
        self.diff_out.write_text("")
        for name, body in (
            ("kubectl", FAKE_KUBECTL),
            ("flux", FAKE_FLUX),
            ("uv", FAKE_UV),
        ):
            write_exec(self.bin / name, body.replace("@LOG@", str(self.log)))
        (root / "kubeconfig").write_text("")

    def tag(
        self, tag: str, files: dict[str, str] | None = None, flux: str = ""
    ) -> Path:
        directory = self.render / "cluster" / tag
        directory.mkdir(parents=True)
        for name, text in (files or {"cm.yaml": "kind: ConfigMap\n"}).items():
            (directory / name).write_text(text)
        if flux:
            (self.render / "flux").mkdir(exist_ok=True)
            (self.render / "flux" / f"{tag}.yaml").write_text(flux)
        return directory

    def env(self, **extra: str) -> dict[str, str]:
        env = {
            "PATH": os.environ["PATH"],
            "SUDO_USER": USER,
            "HOMELAB_TC_KUBECTL": str(self.bin / "kubectl"),
            "HOMELAB_TC_FLUX": str(self.bin / "flux"),
            "HOMELAB_TC_UV": str(self.bin / "uv"),
            "HOMELAB_TC_STATE": str(self.state),
            "HOMELAB_TC_KUBECONFIG": str(self.root / "kubeconfig"),
            "FAKE_LOG": str(self.log),
            "FAKE_DIFF_OUT": str(self.diff_out),
        }
        env.update(extra)
        return env

    def run(
        self,
        *,
        label: str = "5494",
        render: str | None = None,
        src: str | None = None,
        tags: str = "demo",
        script: Path = SCRIPT,
        env: dict[str, str] | None = None,
        raw: list[str] | None = None,
    ) -> subprocess.CompletedProcess:
        args = raw
        if args is None:
            args = [
                "--label", label,
                "--render", render if render is not None else str(self.render),
                "--src", src if src is not None else str(self.src),
                "--tags", tags,
            ]  # fmt: skip
        return run_proc(
            ["bash", str(script), *args],
            env=env if env is not None else self.env(),
        )

    def calls(self) -> str:
        return self.log.read_text()


@pytest.fixture
def rig(home_dir) -> Rig:
    return Rig(home_dir)


def assert_refused(result: subprocess.CompletedProcess, rig: Rig) -> None:
    assert result.returncode == 2, result.stdout + result.stderr
    assert result.stderr.startswith("refused:")
    assert rig.calls() == ""


def assert_accepted(result: subprocess.CompletedProcess) -> None:
    assert result.returncode == 0, result.stdout + result.stderr


@pytest.mark.parametrize(
    "label", ["", ".", "..", "a b", "a/b", "../x", "$(id)", "x" * 65, "a;b"]
)
def test_a_bad_label_is_refused(rig, label) -> None:
    assert_refused(rig.run(label=label), rig)


@pytest.mark.parametrize("label", ["5494", "pr-12.3_a", "x" * 64, "-dash"])
def test_a_good_label_is_accepted(rig, label) -> None:
    assert_accepted(rig.run(label=label))
    assert (rig.state / label).is_dir()


@pytest.mark.parametrize(
    "tags",
    ["", "A", "a_b", "-a", "a,,b", "a,", ",a", "a b", "../x", "a/b", "a" * 64, "a;id"],
)
def test_a_bad_tag_is_refused(rig, tags) -> None:
    assert_refused(rig.run(tags=tags), rig)


@pytest.mark.parametrize("tags", ["a", "a-snapshot,b2", "0abc", "a" * 63])
def test_a_good_tag_is_accepted(rig, tags) -> None:
    assert_accepted(rig.run(tags=tags))


def test_a_path_outside_the_home_is_refused(rig) -> None:
    outside = Path(tempfile.mkdtemp(prefix="takeover-check-outside-"))
    try:
        assert_refused(rig.run(render=str(outside)), rig)
        assert_refused(rig.run(src=str(outside)), rig)
        assert_refused(rig.run(render=str(rig.render / "missing")), rig)
    finally:
        shutil.rmtree(outside)


def test_a_path_inside_the_home_is_accepted(rig) -> None:
    assert_accepted(rig.run(render=str(rig.render / ".." / "render")))


def test_a_symlink_that_escapes_the_home_is_refused(rig) -> None:
    outside = Path(tempfile.mkdtemp(prefix="takeover-check-outside-"))
    try:
        (rig.root / "escape").symlink_to(outside)
        assert_refused(rig.run(render=str(rig.root / "escape")), rig)
        assert_refused(rig.run(src=str(rig.root / "escape")), rig)
    finally:
        shutil.rmtree(outside)


def test_a_symlink_that_stays_in_the_home_is_accepted(rig) -> None:
    (rig.root / "link").symlink_to(rig.render)
    assert_accepted(rig.run(render=str(rig.root / "link")))


@pytest.mark.parametrize(
    "raw",
    [
        [
            "--label",
            "1",
            "--render",
            "R",
            "--src",
            "S",
            "--tags",
            "a",
            "--dry-run=none",
        ],
        [
            "--label",
            "1",
            "--render",
            "R",
            "--src",
            "S",
            "--tags",
            "a",
            "--kubeconfig",
            "x",
        ],
        ["--label", "1", "--render", "R", "--src", "S", "--tags", "a", "-o", "yaml"],
        ["--label", "1", "--label", "2", "--render", "R", "--src", "S", "--tags", "a"],
        ["--label", "1", "--render", "R", "--src", "S"],
        ["--label", "1", "--render", "R", "--src", "S", "--tags"],
        ["delete", "pods"],
        [],
    ],
)
def test_an_extra_or_missing_flag_is_refused(rig, raw) -> None:
    raw = [
        str(rig.render) if a == "R" else str(rig.src) if a == "S" else a for a in raw
    ]
    assert_refused(rig.run(raw=raw), rig)


def test_exactly_the_four_flags_are_accepted(rig) -> None:
    raw = [
        "--tags",
        "demo",
        "--src",
        str(rig.src),
        "--render",
        str(rig.render),
        "--label",
        "1",
    ]
    assert_accepted(rig.run(raw=raw))


@pytest.mark.parametrize("user", ["", "root"])
def test_a_missing_or_root_sudo_user_is_refused(rig, user) -> None:
    assert_refused(rig.run(env=rig.env(SUDO_USER=user)), rig)


def test_a_sudo_user_with_a_home_is_accepted(rig) -> None:
    assert_accepted(rig.run(env=rig.env(SUDO_USER=USER)))


def test_a_symlink_in_the_render_is_refused(rig) -> None:
    directory = rig.tag("demo")
    (directory / "leak.yaml").symlink_to("/etc/passwd")
    result = rig.run()
    assert "refused, cluster/demo holds a symlink" in result.stdout
    assert rig.calls() == ""
    assert result.returncode == 1


def test_a_plain_render_is_accepted(rig) -> None:
    rig.tag("demo")
    rig.diff_out.write_text("")
    result = rig.run(env=rig.env(FAKE_DIFF_RC="0"))
    assert_accepted(result)
    assert "kubectl diff exit=0" in result.stdout


def test_a_remote_kustomization_base_is_refused(rig) -> None:
    rig.tag(
        "demo",
        {"kustomization.yaml": "resources:\n  - https://example.invalid/x?ref=main\n"},
    )
    assert "remote base" in rig.run().stdout
    assert rig.calls() == ""


def test_a_local_kustomization_base_is_accepted(rig) -> None:
    rig.tag(
        "demo",
        {
            "kustomization.yaml": "resources:\n  - cm.yaml\n",
            "cm.yaml": "kind: ConfigMap\n",
        },
    )
    assert "remote base" not in rig.run().stdout
    assert "argv: diff" in rig.calls()


def test_kubectl_runs_with_the_hard_coded_dry_run_flags_and_a_fixed_environment(
    rig,
) -> None:
    rig.tag("demo")
    result = rig.run(
        env=rig.env(
            KUBECTL_EXTERNAL_DIFF="/tmp/evil",
            KUBECONFIG="/tmp/evil",
            PATH=path_with("/tmp/evil"),
        )
    )
    assert_accepted(result)
    calls = rig.calls()
    work = rig.state / "5494" / "demo.path"
    assert (
        f"argv: diff --server-side --field-manager=kustomize-controller --force-conflicts -k {work}"
        in calls
    )
    assert "KUBECTL_EXTERNAL_DIFF=unset" in calls
    assert f"KUBECONFIG={rig.root / 'kubeconfig'} " in calls
    assert "PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" in calls
    assert "umask: 0077" in calls
    assert (rig.state / "5494").stat().st_mode & 0o777 == 0o700


def test_flux_runs_with_the_hard_coded_flags_on_a_copy(rig) -> None:
    rig.tag(
        "demo",
        flux="metadata:\n  name: demo\nspec:\n  postBuild: {}\n---\nmetadata:\n  name: demo-snapshot\n",
    )
    result = rig.run()
    assert_accepted(result)  # exit 1 from flux means changes, not failure
    work = rig.state / "5494" / "demo.path"
    own = rig.state / "5494" / "demo.kustomization.yaml"
    assert (
        f"flux argv: --kubeconfig {rig.root / 'kubeconfig'} diff kustomization demo "
        f"--path {work} --kustomization-file {own} "
        "--ignore-not-found --strict-substitute --progress-bar=false"
    ) in rig.calls()
    assert "demo-snapshot" not in own.read_text()
    assert "Kustomization/flux-system/demo created" in result.stdout


def test_the_filter_strips_values_from_a_secret_diff(rig) -> None:
    rig.tag("demo")
    rig.diff_out.write_text(SECRET_DIFF)
    result = rig.run()
    assert_accepted(result)
    for value in SECRET_VALUES:
        assert value not in result.stdout
    assert "echo " not in result.stdout
    assert "object: v1.Secret.demo.creds" in result.stdout
    assert "+  password:" in result.stdout
    # The full diff stays in the root-only directory, for the operator.
    assert SECRET_VALUES[0] in (rig.state / "5494" / "demo.diff").read_text()


def test_an_rbac_file_runs_the_census_as_the_user_and_a_secret_file_skips_it(
    rig,
) -> None:
    rig.tag(
        "demo",
        {
            "role.yaml": "kind: Role\nmetadata:\n  name: r\n",
            "mixed.yaml": "kind: Role\nmetadata:\n  name: r\n---\nkind: Secret\nmetadata:\n  name: s\n",
        },
    )
    result = rig.run(env=rig.env(FAKE_DIFF_RC="0"))
    assert_accepted(result)
    assert "RBAC takeover census over role.yaml" in result.stdout
    assert (
        "RBAC census over mixed.yaml skipped, the file also holds a Secret"
        in result.stdout
    )
    calls = rig.calls()
    assert calls.count("argv: get -f") == 1
    assert (
        "argv: apply --server-side --dry-run=server --force-conflicts --field-manager=kustomize-controller -f"
        in calls
    )
    assert "uv argv: run --quiet python scripts/dev/takeover_census.py" in calls
    assert f"in {rig.src}" in calls
    # The census inputs do not outlive the run.
    assert not list(rig.state.glob(".census.*"))


def test_root_ignores_every_override(rig, tmp_path) -> None:
    text = SCRIPT.read_text()
    default_k3s = "/usr/local/bin/k3s"
    assert default_k3s in text and "/usr/bin/id -u" in text
    fake_id = write_exec(tmp_path / "id", "#!/usr/bin/bash\necho 0\n")
    root_copy = tmp_path / "as-root.sh"
    root_copy.write_text(
        text.replace("/usr/bin/id -u", f"{fake_id} -u")
        .replace(default_k3s, "/nonexistent/k3s")
        .replace("/var/lib/homelab-takeover-check", str(tmp_path / "default-state"))
    )
    result = rig.run(script=root_copy)
    assert result.returncode == 3, result.stdout + result.stderr
    assert "missing binary: /nonexistent/k3s" in result.stderr
    assert rig.calls() == ""
    assert not rig.state.exists()
    # The same copy, claiming a non-root uid, honours the override: the gate is what differed.
    write_exec(fake_id, "#!/usr/bin/bash\necho 1000\n")
    assert_accepted(rig.run(script=root_copy))


def test_the_script_names_only_root_owned_binaries_and_never_the_users_flux() -> None:
    text = SCRIPT.read_text()
    assert "/usr/local/lib/homelab-takeover-check/flux" in text
    assert ".local/bin" not in text.replace('PATH="$home/.local/bin', "")
    assert not [x for x in text.splitlines() if x.lstrip().startswith("sudo ")]


def test_the_census_runs_as_the_user_through_runuser() -> None:
    text = SCRIPT.read_text()
    assert 'as_user() { runuser -u "$SUDO_USER" -- "$@"; }' in text
    assert text.count("as_user env -i") == 1
    assert "takeover_census.py" in text.split("as_user env -i")[1].split(")")[0]
