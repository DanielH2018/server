"""checkout_sync.py over a fixture repository: what it fast-forwards to, and every refusal.

Each test clones a bare fixture origin into a primary checkout under `tmp_path`. GitHub is a stub
whose check-runs come from a dict keyed by SHA, so a test sets each commit's CI by hand. The cases
are H7.51's *Verify-by* (docs/target-architecture/host/h7-delete-ansible.md): a green tip
fast-forwards, red or cancelled CI, a non-fast-forward and an untracked file each leave HEAD where
it was.

Run: uv run pytest hosts/lib/tests/test_checkout_sync.py
"""

import io
import json
import subprocess
import urllib.error
from email.message import Message
from pathlib import Path

import pytest

import checkout_sync as cs
from lib.proc_testing import fake_bin

GREEN = [{"name": "prek", "status": "completed", "conclusion": "success"}]
RED = [{"name": "prek", "status": "completed", "conclusion": "failure"}]
CANCELLED = [{"name": "prek", "status": "completed", "conclusion": "cancelled"}]
IDENTITY = {
    "GIT_AUTHOR_NAME": "t",
    "GIT_AUTHOR_EMAIL": "t@example.invalid",
    "GIT_COMMITTER_NAME": "t",
    "GIT_COMMITTER_EMAIL": "t@example.invalid",
}


@pytest.fixture(autouse=True)
def _own_git(monkeypatch):
    # Under a hook git honours GIT_DIR over cwd, which would point every call at the real repo.
    for name in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE"):
        monkeypatch.delenv(name, raising=False)
    for name, value in IDENTITY.items():
        monkeypatch.setenv(name, value)


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True, timeout=30
    ).stdout.strip()


class Lab:
    """A bare origin, a writer clone that pushes to it, and the primary checkout under test."""

    def __init__(self, root: Path):
        self.origin, self.writer, self.checkout = (
            root / "origin.git",
            root / "writer",
            root / "server",
        )
        git(
            root,
            "init",
            "--quiet",
            "--bare",
            "--initial-branch=master",
            str(self.origin),
        )
        git(root, "clone", "--quiet", str(self.origin), str(self.writer))
        self.base = self.push("base")
        git(root, "clone", "--quiet", str(self.origin), str(self.checkout))
        self.state = root / "state"
        self.textfiles = root / "textfiles"
        self.state.mkdir()
        self.textfiles.mkdir()
        (root / "creds").mkdir()
        (root / "creds" / "github_token").write_text("t0ken\n")
        self.env = {
            "CI_CONTEXTS": "prek",
            "CI_ANCESTOR_WALK_MAX": "10",
            "GITHUB_REPO": "o/n",
            "CREDENTIALS_DIRECTORY": str(root / "creds"),
        }
        self.ci: dict[str, list[dict]] = {}

    def push(self, name: str) -> str:
        (self.writer / name).write_text(name)
        git(self.writer, "add", name)
        git(self.writer, "commit", "--quiet", "-m", name)
        git(self.writer, "push", "--quiet", "origin", "HEAD:master")
        return git(self.writer, "rev-parse", "HEAD")

    def head(self) -> str:
        return git(self.checkout, "rev-parse", "HEAD")

    def sync(self, **env: str) -> cs.CheckoutSync:
        return cs.CheckoutSync(
            self.checkout,
            env={**self.env, **env},
            state_dir=self.state,
            textfile_dir=self.textfiles,
            opener=self.github,
        )

    def github(self, request, timeout=None):
        sha = request.full_url.split("/commits/")[1].split("/")[0]
        if "/check-suites" in request.full_url:
            return io.BytesIO(
                json.dumps({"total_count": 0, "check_suites": []}).encode()
            )
        runs = self.ci.get(sha, [])
        return io.BytesIO(
            json.dumps({"total_count": len(runs), "check_runs": runs}).encode()
        )

    def metric(self, series: str) -> str:
        text = (self.textfiles / cs.PROM).read_text()
        return next(
            line.split()[-1] for line in text.splitlines() if line.startswith(series)
        )


@pytest.fixture
def lab(tmp_path) -> Lab:
    return Lab(tmp_path)


def test_a_green_tip_fast_forwards(lab):
    tip = lab.push("a")
    lab.ci[tip] = GREEN
    assert lab.sync().main() == 0
    assert lab.head() == tip
    assert lab.metric('homelab_check_success{check="checkout-sync"}') == "1"
    assert lab.metric('homelab_checkout_sync_refused{reason="ci"}') == "0"


def test_a_red_tip_merges_its_newest_green_ancestor(lab):
    older = lab.push("a")
    green = lab.push("b")
    tip = lab.push("c")
    lab.ci.update({older: GREEN, green: GREEN, tip: RED})
    assert lab.sync().main() == 0
    assert lab.head() == green


@pytest.mark.parametrize("runs", [RED, CANCELLED], ids=["red", "cancelled"])
def test_a_tip_with_no_green_commit_below_it_leaves_head(lab, runs):
    below = lab.push("a")
    tip = lab.push("b")
    lab.ci.update({below: runs, tip: runs})
    sync = lab.sync()
    assert sync.main() == 0
    assert sync.requests == 2, "both commits were graded"
    assert lab.head() == lab.base


def test_a_non_fast_forward_is_refused(lab):
    tip = lab.push("a")
    lab.ci[tip] = GREEN
    (lab.checkout / "local").write_text("local")
    git(lab.checkout, "add", "local")
    git(lab.checkout, "commit", "--quiet", "-m", "local")
    local = lab.head()
    sync = lab.sync()
    assert sync.main() == 0
    assert lab.head() == local
    assert sync.requests == 0
    assert lab.metric('homelab_checkout_sync_refused{reason="ancestry"}') == "1"


def test_an_untracked_file_is_refused(lab):
    tip = lab.push("a")
    lab.ci[tip] = GREEN
    (lab.checkout / "stray").write_text("stray")
    assert lab.sync().main() == 0
    assert lab.head() == lab.base
    assert lab.metric("homelab_checkout_dirty") == "1"
    assert lab.metric('homelab_check_success{check="checkout-sync"}') == "0"


def test_another_branch_is_refused(lab):
    tip = lab.push("a")
    lab.ci[tip] = GREEN
    git(lab.checkout, "checkout", "--quiet", "-b", "work")
    assert lab.sync().main() == 0
    assert lab.head() == lab.base
    assert lab.metric('homelab_checkout_sync_refused{reason="branch"}') == "1"


@pytest.mark.parametrize(
    ("env", "reason"),
    [
        ({"CI_CONTEXTS": ""}, "ci"),
        ({"CI_ANCESTOR_WALK_MAX": "0"}, "ci"),
        ({"CREDENTIALS_DIRECTORY": "/nonexistent"}, "token"),
    ],
    ids=["no-contexts", "zero-bound", "no-token"],
)
def test_a_disarmed_gate_or_a_missing_token_asks_github_nothing(lab, env, reason):
    tip = lab.push("a")
    lab.ci[tip] = GREEN
    sync = lab.sync(**env)
    assert sync.main() == 0
    assert sync.requests == 0
    assert lab.head() == lab.base
    assert lab.metric(f'homelab_checkout_sync_refused{{reason="{reason}"}}') == "1"


def test_a_missing_credential_never_falls_back_to_the_gh_login(
    lab, tmp_path, monkeypatch
):
    # sys_user's gh is logged in as the repo owner, so a fallback would spend that token.
    ran = tmp_path / "gh-ran"
    bin_dir = fake_bin(tmp_path / "bin", gh=f"touch {ran}\necho operator-token\n")
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    tip = lab.push("a")
    lab.ci[tip] = GREEN
    sync = lab.sync(CREDENTIALS_DIRECTORY="/nonexistent")
    assert sync.main() == 0
    assert not ran.exists(), "the run asked gh for a token"
    assert sync.requests == 0
    assert lab.head() == lab.base
    assert lab.metric('homelab_checkout_sync_refused{reason="token"}') == "1"


def test_a_refused_token_reads_token(lab):
    tip = lab.push("a")
    lab.ci[tip] = GREEN

    def refusing(request, timeout=None):
        exc = urllib.error.HTTPError(
            request.full_url, 401, "Unauthorized", Message(), None
        )
        exc.close()
        raise exc

    sync = lab.sync()
    sync.opener = refusing
    assert sync.main() == 0
    assert lab.head() == lab.base
    assert lab.metric('homelab_checkout_sync_refused{reason="token"}') == "1"


def test_a_walk_past_its_budget_stops_grading(lab):
    green = lab.push("a")
    tip = lab.push("b")
    lab.ci.update({green: GREEN, tip: RED})
    clock = iter([0.0, 0.0, cs.WALK_BUDGET_S + 1.0, cs.WALK_BUDGET_S + 2.0])
    sync = lab.sync()
    sync.now = lambda: next(clock)
    assert sync.main() == 0
    assert sync.requests == 1, "only the tip was graded"
    assert lab.head() == lab.base
