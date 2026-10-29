"""The builder script's run: its anchor, its failure exits, its metric and what it stages.

Each test builds a real fixture history in a git repository under `tmp_path` and points the
builder's clone at it. GitHub, the render and nfpm are stubs: the check-runs come from a dict
keyed by SHA, the render writes one `homelab-updater` package around the committed archive
keyring, and nfpm writes an empty `.deb` named the way nfpm names one. The cases are the
*Builder* list of docs/target-architecture/host/h3-pi-pilot.md, *Verification*.

Run: uv run pytest ansible/roles/setup/homelab_builder/tests/test_host_package_builder.py
"""

import io
import itertools
import json
import os
import subprocess
from pathlib import Path
from typing import ClassVar

import pytest

import host_package_builder as hb
from lib.repo_paths import REPO


KEYRING = REPO / "hosts" / "keys" / "homelab-archive-keyring.gpg"
FPR = "83B57416B342DDE1CBA0752D435D139ADFD6F016"
GREEN = [{"name": "prek", "status": "completed", "conclusion": "success"}]
RED = [{"name": "prek", "status": "completed", "conclusion": "failure"}]
PENDING = [{"name": "prek", "status": "in_progress", "conclusion": None}]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


class Origin:
    """A fixture master: each commit has its own committer time, one second after the last."""

    def __init__(self, path: Path):
        self.path = path
        self.clock = 1_700_000_000
        path.mkdir()
        git(path, "init", "--quiet", "--initial-branch=master")

    def commit(self, name: str) -> str:
        self.clock += 1
        (self.path / name).write_text(name)
        git(self.path, "add", name)
        self._env_git("commit", "--quiet", "-m", name)
        return git(self.path, "rev-parse", "HEAD")

    def merge_side_branch(self) -> tuple[str, str]:
        """A merge whose second parent is a side commit; returns (side, merge)."""
        base = git(self.path, "rev-parse", "HEAD")
        git(self.path, "checkout", "--quiet", "-b", "side")
        side = self.commit("side")
        git(self.path, "checkout", "--quiet", "master")
        git(self.path, "reset", "--quiet", "--hard", base)
        self.commit("main-line")
        self.clock += 1
        self._env_git("merge", "--quiet", "--no-ff", "-m", "merge", "side")
        return side, git(self.path, "rev-parse", "HEAD")

    def _env_git(self, *args: str) -> None:
        stamp = f"@{self.clock} +0000"
        # Identity through the environment, never `git config`: under a hook, git honours
        # GIT_DIR over cwd, and a config write would land in the real repository.
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "GIT_AUTHOR_DATE": stamp,
            "GIT_COMMITTER_DATE": stamp,
        }
        subprocess.run(
            ["git", *args],
            cwd=self.path,
            env=env,
            check=True,
            capture_output=True,
            timeout=30,
        )


class FakeGitHub:
    """`urlopen` for the check-runs API, answering from `runs` and counting requests."""

    def __init__(self):
        self.runs: dict[str, list[dict]] = {}
        self.asked: list[str] = []

    def __call__(self, request, timeout=None):
        sha = request.full_url.split("/commits/")[1].split("/")[0]
        self.asked.append(sha)
        runs = self.runs.get(sha, [])
        body = json.dumps({"total_count": len(runs), "check_runs": runs}).encode()
        return _Response(body)


class _Response:
    def __init__(self, body: bytes):
        self.body = io.BytesIO(body)

    def read(self, *args):
        return self.body.read(*args)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FixtureBuilder(hb.Builder):
    """The builder with the render and nfpm replaced; `keyring` is what the render ships."""

    keyring: Path | None = KEYRING
    render_exit = 0
    # True renders the same package at every commit, for a commit that changes no package.
    fixed_render = False

    def render(self, tree: Path, out: Path) -> None:
        if self.render_exit:
            raise hb.Failure("build", f"the render exited {self.render_exit}")
        package = out / "packages" / "homelab-updater"
        (package / "root").mkdir(parents=True)
        (package / "nfpm.yaml").write_text("name: homelab-updater\n")
        # The render follows the tree, so a commit that changes a file changes the package.
        tree_text = "fixed" if self.fixed_render else git(tree, "ls-files")
        (package / "root" / "tree.txt").write_text(tree_text)
        if self.keyring is not None:
            target = out / hb.KEYRING
            target.parent.mkdir(parents=True)
            target.write_bytes(self.keyring.read_bytes())

    built_inodes: ClassVar[set[int]] = set()

    def package(self, directory: Path, target: Path, ver: str) -> None:
        deb = target / f"{directory.name}_{ver}_all.deb"
        deb.write_bytes(b"")
        FixtureBuilder.built_inodes.add(deb.stat().st_ino)


@pytest.fixture
def lab(tmp_path):
    origin = Origin(tmp_path / "origin")
    state = tmp_path / "state"
    (state / "staged").mkdir(parents=True)
    etc = tmp_path / "etc"
    etc.mkdir()
    (etc / "archive-key.fpr").write_text(FPR + "\n")
    github = FakeGitHub()
    return origin, state, etc, github


def config(etc: Path, **overrides: str) -> Path:
    values = {
        "GITHUB_TOKEN": "t0ken",
        "GITHUB_REPO": "owner/name",
        "CI_CONTEXTS": "prek",
        "CI_ANCESTOR_WALK_MAX": "10",
        **overrides,
    }
    path = etc / "config.env"
    path.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    return path


def builder(lab, cls=None, **config_overrides: str) -> FixtureBuilder:
    origin, state, etc, github = lab
    return (cls or FixtureBuilder)(
        config_path=config(etc, **config_overrides),
        fingerprint_path=etc / "archive-key.fpr",
        hold_path=etc / "hold",
        state_dir=state,
        clone_url=str(origin.path),
        opener=github,
    )


def metric(state: Path) -> tuple[str, str, float]:
    """The unbuilt-since series as (reason, sha, value)."""
    for line in (state / hb.PROM).read_text().splitlines():
        if line.startswith("homelab_builder_unbuilt_since_timestamp_seconds{"):
            labels, value = line.split("} ")
            fields = dict(part.split("=") for part in labels.split("{")[1].split(","))
            return fields["reason"].strip('"'), fields["sha"].strip('"'), float(value)
    raise AssertionError("no unbuilt-since series")


def staged(state: Path) -> list[str]:
    return sorted(p.name for p in (state / "staged").iterdir())


def last_built(state: Path) -> str | None:
    return json.loads((state / "state.json").read_text()).get("last_built")


def test_a_missing_token_makes_no_request_and_exits_1(lab, monkeypatch):
    origin, state, _, github = lab
    origin.commit("a")
    # The fallback is `gh auth token`, which on a logged-in host would return a real token.
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: pytest.fail("ran a subprocess")
    )
    first = builder(lab, GITHUB_TOKEN="")
    assert first.main() == 1
    assert builder(lab, GITHUB_TOKEN="").main() == 0, "a repeat pages nothing"
    assert github.asked == []
    assert not (state / "src" / "HEAD").exists(), "fetched before the token check"


def test_the_token_comes_from_the_credential_when_the_settings_carry_none(
    lab, tmp_path, monkeypatch
):
    # LoadCredential=github_token:... puts the token in $CREDENTIALS_DIRECTORY, not in the settings.
    origin, _, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN
    (tmp_path / "github_token").write_text("c0red\n")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    b = builder(lab, GITHUB_TOKEN="")
    assert b.main() == 0
    assert b.token == "c0red"
    assert github.asked, "the run never read the check-runs"


def test_a_green_tip_is_built_and_reads_ok_at_zero(lab):
    origin, state, _, github = lab
    origin.commit("a")
    tip = origin.commit("b")
    github.runs[tip] = GREEN
    assert builder(lab).main() == 0
    count = git(origin.path, "rev-list", "--count", "--first-parent", tip)
    assert staged(state) == ["chosen", f"homelab-updater_0.{count}+g{tip[:12]}_all.deb"]
    assert (state / "staged" / "chosen").read_text() == tip[:12] + "\n"
    assert metric(state) == ("ok", tip[:12], 0.0)
    assert last_built(state) == tip


def test_a_pending_or_red_tip_over_a_green_parent_builds_the_parent(lab):
    origin, state, _, github = lab
    parent = origin.commit("a")
    tip = origin.commit("b")
    github.runs = {parent: GREEN, tip: PENDING}
    assert builder(lab).main() == 0
    assert last_built(state) == parent
    # The parent's version carries the parent's own first-parent count, not the tip's.
    assert any(name.startswith("homelab-updater_0.1+g") for name in staged(state))
    reason, sha, since = metric(state)
    assert (reason, sha) == ("ok", tip[:12])
    assert since == float(git(origin.path, "show", "-s", "--format=%ct", tip))

    github.runs[tip] = RED
    assert builder(lab).main() == 0
    assert last_built(state) == parent, "a red tip is never built"


def test_a_green_second_parent_is_never_chosen(lab):
    origin, state, _, github = lab
    origin.commit("a")
    side, merge = origin.merge_side_branch()
    github.runs = {side: GREEN, merge: RED}
    walked = builder(lab)
    assert walked.main() == 0
    assert side not in github.asked
    assert metric(state)[0] == "no-green"
    assert last_built(state) is None


def test_a_cached_pass_makes_no_second_request(lab):
    # A render that fails leaves the green tip unbuilt, so the second run grades it again.
    origin, _, _, github = lab
    green = origin.commit("a")
    github.runs = {green: GREEN}

    def run() -> None:
        b = builder(lab)
        b.render_exit = 2
        b.main()

    run()
    run()
    assert github.asked.count(green) == 1, "a cached pass is asked again"


def test_a_walk_with_no_green_commit_exits_0_and_reads_no_green(lab):
    origin, state, _, github = lab
    origin.commit("a")
    tip = origin.commit("b")
    github.runs[tip] = PENDING
    assert builder(lab).main() == 0
    assert metric(state)[0] == "no-green"
    assert staged(state) == []


def test_the_walk_stops_at_the_last_built_commit_and_at_the_bound(lab):
    origin, _, _, github = lab
    first = origin.commit("a")
    github.runs[first] = GREEN
    builder(lab).main()
    newer = [origin.commit(f"c{i}") for i in range(4)]
    github.asked.clear()
    assert builder(lab, CI_ANCESTOR_WALK_MAX="2").main() == 0
    assert github.asked == [newer[3], newer[2]], "the bound counts the tip"
    github.asked.clear()
    builder(lab).main()
    assert first not in github.asked, "the walk passed the last built commit"


def test_a_no_green_walk_logs_the_bound_beside_the_unbuilt_count(lab, capsys):
    # One pending commit over the last build once logged "in the 1 newest", which read as a
    # walk bound of 1 (#6511).
    origin, _, _, github = lab
    first = origin.commit("a")
    github.runs[first] = GREEN
    builder(lab).main()
    github.runs[origin.commit("b")] = PENDING
    capsys.readouterr()
    assert builder(lab).main() == 0
    assert (
        f"graded 1 of the 1 commits since the last build {first[:12]}, walk bound 10"
        in capsys.readouterr().out
    )


@pytest.mark.parametrize(
    "overrides", [{"CI_CONTEXTS": ""}, {"CI_ANCESTOR_WALK_MAX": "0"}]
)
def test_a_disarmed_ci_config_asks_nothing_builds_nothing_and_pages_once(
    lab, overrides
):
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN
    assert builder(lab, **overrides).main() == 1
    assert builder(lab, **overrides).main() == 0
    assert github.asked == []
    assert metric(state)[0] == "ci"
    assert staged(state) == []
    assert builder(lab).main() == 0, "the same history with the prek context builds"
    assert last_built(state) == tip


def test_a_tip_that_does_not_descend_from_the_last_built_is_refused(lab):
    origin, state, _, github = lab
    origin.commit("a")
    built = origin.commit("b")
    github.runs[built] = GREEN
    builder(lab).main()
    # A rewritten master: the tip's chain no longer holds the last built commit.
    git(origin.path, "reset", "--quiet", "--hard", "HEAD~1")
    tip = origin.commit("rewritten")
    github.runs[tip] = GREEN
    assert builder(lab).main() == 1
    assert builder(lab).main() == 0, "the same condition pages once"
    reason, sha, since = metric(state)
    assert (reason, sha) == ("ancestry", tip[:12])
    assert since > 0
    assert last_built(state) == built


def test_a_held_builder_fetches_and_builds_nothing(lab):
    origin, state, etc, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN
    (etc / "hold").write_text("")
    assert builder(lab).main() == 0
    assert (state / "src" / "HEAD").is_file()
    assert metric(state)[0] == "hold"
    assert github.asked == []
    assert staged(state) == []


@pytest.mark.parametrize("case", ["absent", "other-fingerprint"])
def test_a_keyring_refusal_stages_nothing_and_pages_once(lab, case):
    origin, state, etc, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN

    def run() -> int:
        b = builder(lab)
        if case == "absent":
            b.keyring = None
        return b.main()

    if case == "other-fingerprint":
        (etc / "archive-key.fpr").write_text("0" * 40 + "\n")
    assert run() == 1
    assert run() == 0
    assert metric(state)[0] == "keyring"
    assert staged(state) == []
    assert last_built(state) is None


def test_versions_built_commit_by_commit_compare_strictly_greater(lab):
    origin, state, _, github = lab
    versions = []
    for i in range(3):
        sha = origin.commit(f"v{i}")
        github.runs[sha] = GREEN
        assert builder(lab).main() == 0
        [deb] = [n for n in staged(state) if n.endswith(".deb")]
        versions.append(deb.split("_")[1])
    for older, newer in itertools.pairwise(versions):
        subprocess.run(
            ["dpkg", "--compare-versions", newer, "gt", older], check=True, timeout=30
        )


class FixedRender(FixtureBuilder):
    fixed_render = True


def test_an_unconsumed_staged_set_is_replaced_at_the_new_commit(lab):
    # The second commit leaves the render unchanged, so only the unconsumed set rule
    # rebuilds the package; without it the old set would sit under a moved `chosen`.
    origin, state, _, github = lab
    first = origin.commit("a")
    github.runs[first] = GREEN
    builder(lab, cls=FixedRender).main()
    second = origin.commit("b")
    github.runs[second] = GREEN
    builder(lab, cls=FixedRender).main()
    assert (state / "staged" / "chosen").read_text() == second[:12] + "\n"
    # One commit per staged set, as H3.5b's signer requires.
    debs = [n for n in staged(state) if n.endswith(".deb")]
    assert len(debs) == 1 and debs[0].endswith(f"+g{second[:12]}_all.deb")


def test_a_commit_that_changes_no_package_stages_nothing_and_advances(lab):
    origin, state, _, github = lab
    first = origin.commit("a")
    github.runs[first] = GREEN
    builder(lab, cls=FixedRender).main()
    # The signer took the set, as H3.5b's does after every run.
    for entry in (state / "staged").iterdir():
        entry.unlink()
    second = origin.commit("b")
    github.runs[second] = GREEN
    assert builder(lab, cls=FixedRender).main() == 0
    assert staged(state) == []
    assert last_built(state) == second
    assert metric(state) == ("ok", second[:12], 0.0)


def test_a_git_error_that_recurs_in_the_metric_write_still_pages_once(lab):
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN

    class BrokenClone(FixtureBuilder):
        def git(self, *args, cwd=None):
            if args[0] in ("rev-list", "show"):
                raise RuntimeError("git rev-list: bad object")
            return super().git(*args, cwd=cwd)

    assert builder(lab, cls=BrokenClone).main() == 1
    assert builder(lab, cls=BrokenClone).main() == 0
    assert (
        json.loads((state / "state.json").read_text())["failure"]["reason"] == "build"
    )


def test_a_render_failure_reads_build_and_pages_once(lab):
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN

    def run() -> int:
        b = builder(lab)
        b.render_exit = 2
        return b.main()

    assert run() == 1
    assert run() == 0
    assert metric(state)[0] == "build"


def test_an_unexpected_os_error_reads_build_and_pages_once(lab):
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN

    class NoNfpm(FixtureBuilder):
        def package(self, directory, target, ver):
            raise FileNotFoundError("nfpm")

    def run() -> int:
        return builder(lab, cls=NoNfpm).main()

    assert run() == 1
    assert run() == 0
    assert metric(state)[0] == "build"


def test_each_staged_deb_is_created_in_staged_not_renamed_into_it(lab):
    # The test user has one group, so the setgid group itself cannot be asserted. A file
    # created in staged/ takes that group, and a renamed one keeps the builder's, so the
    # check is that each staged .deb is a new inode rather than the one nfpm wrote.
    origin, state, _, github = lab
    tip = origin.commit("a")
    github.runs[tip] = GREEN
    FixtureBuilder.built_inodes.clear()
    builder(lab).main()
    [deb] = [p for p in (state / "staged").iterdir() if p.suffix == ".deb"]
    assert FixtureBuilder.built_inodes, "the fixture nfpm built nothing"
    assert deb.stat().st_ino not in FixtureBuilder.built_inodes
