"""Shared fixtures for the tests on gitops_deploy.py.

gitops_deploy.py reads its config from the path in GITOPS_DEPLOY_CONFIG at import. This
file points that at the canned `config.env` beside it BEFORE any test module imports the
deployer, so the suite imports the same module in CI and on a host, and never opens the
host's /etc copy (0600, it carries the Discord webhook). The `gitops_deploy` fixture is
that import; `state_dir` repoints every /var/lib/gitops-deploy marker at tmp_path.

The AST fixtures below remain for the guards that pin a function's shape at the source
(test_staging_gate_is_advisory.py, test_gitops_deploy_timeout_budgets.py); `tick` runs
main() itself against a scripted checkout (test_gitops_deploy_main_branches.py).

Fixtures rather than importable functions: `from conftest import x` resolves to whichever
conftest.py sys.path reached first once the whole repo suite runs, and this repo has three.
pytest resolves a fixture by directory, so it cannot collide. tests/ sits outside files/,
so nothing here is in the role's ship list and nothing here reaches a host.
"""

import ast
import os
import pathlib
import subprocess
from collections.abc import Callable
from types import ModuleType

import pytest

GITOPS_SRC = pathlib.Path(__file__).resolve().parents[1] / "files" / "gitops_deploy.py"
STATE_PREFIX = "/var/lib/gitops-deploy/"

# At import, not in a fixture: a test module's own `import gitops_deploy` runs at collection,
# before any fixture. pytest imports a directory's conftest.py ahead of its test modules.
os.environ["GITOPS_DEPLOY_CONFIG"] = str(pathlib.Path(__file__).with_name("config.env"))


@pytest.fixture(scope="session")
def gitops_deploy() -> ModuleType:
    """The deployer module, imported against the canned config."""
    import gitops_deploy

    assert gitops_deploy.REPO == "/tmp/gitops-test-repo", (
        f"gitops_deploy imported against {gitops_deploy.CONFIG_PATH}, not the canned config"
    )
    return gitops_deploy


@pytest.fixture
def state_dir(
    gitops_deploy: ModuleType, monkeypatch, tmp_path: pathlib.Path
) -> pathlib.Path:
    """tmp_path, with every module constant naming a /var/lib/gitops-deploy path repointed
    into it under the same basename, so a test reads what a tick wrote (`last_run`,
    `pending_alerts.json`, the per-channel `*_alerted_sha` markers) without touching the host."""
    markers = {
        name: value
        for name, value in vars(gitops_deploy).items()
        if isinstance(value, str) and value.startswith(STATE_PREFIX)
    }
    assert "LAST_RUN" in markers and "PENDING_ALERTS_FILE" in markers
    for name, value in markers.items():
        monkeypatch.setattr(
            gitops_deploy, name, str(tmp_path / value.removeprefix(STATE_PREFIX))
        )
    return tmp_path


@pytest.fixture(scope="session")
def gitops_src() -> pathlib.Path:
    """The path of gitops_deploy.py, for the two guards that read its raw text."""
    return GITOPS_SRC


@pytest.fixture(scope="session")
def gitops_tree() -> ast.Module:
    return ast.parse(GITOPS_SRC.read_text())


@pytest.fixture(scope="session")
def gitops_fn(
    gitops_tree: ast.Module,
) -> Callable[[str, ast.AST | None], ast.FunctionDef]:
    """`gitops_fn("main")` is main()'s FunctionDef; a missing name fails, never returns None.

    `tree` overrides the parsed module for a rejecting half that parses the pre-fix shape
    of a function and asserts the check still flags it.
    """

    def _fn(name: str, tree: ast.AST | None = None) -> ast.FunctionDef:
        fn = next(
            (
                n
                for n in ast.walk(tree if tree is not None else gitops_tree)
                if isinstance(n, ast.FunctionDef) and n.name == name
            ),
            None,
        )
        assert fn is not None, f"{name}() not found in gitops_deploy.py"
        return fn

    return _fn


@pytest.fixture(scope="session")
def ast_calls() -> Callable[[ast.AST, str], bool]:
    """Whether `node` contains a call to `fn_name`, as a bare name or an attribute."""

    def _calls(node: ast.AST, fn_name: str) -> bool:
        return any(
            isinstance(c, ast.Call)
            and (
                (isinstance(c.func, ast.Name) and c.func.id == fn_name)
                or (isinstance(c.func, ast.Attribute) and c.func.attr == fn_name)
            )
            for c in ast.walk(node)
        )

    return _calls


LOCAL = "1" * 40
ORIGIN = "2" * 40


class ScriptedTick:
    """What one main() call sees from git, ansible-playbook, GitHub and Discord, and what it
    did to them.

    The scenario is set on the attributes before `main()` runs: the two HEADs and how they
    relate, whether the tree is dirty, the CI verdict, the paths origin adds, the file
    contents and diffs git would show, the outcome of each playbook run in order, whether the
    health gate passes and whether Discord accepts a post. `log` then holds every call in the
    order main() made it, so a test asserts ordering (hold before reset, staging before merge)
    by reading it, not the source.

    Attributes:
        local: the SHA the checkout is on; `head` follows it through merges and resets.
        origin: the SHA origin/master resolves to.
        origin_ahead: whether origin descends from local (the ordinary push).
        local_ahead: whether local descends from origin (an unpushed local commit).
        dirty: whether `git status --porcelain` reports anything.
        ci: what fetch_ci_verdict() reports for origin.
        paths: what `git diff --name-only local..origin` lists.
        files: `"<ref>:<path>"` to the content `git show` returns for it.
        tree_listing: what `git ls-tree` at origin lists under roles/k8s/.
        diffs: k8s service to the `-U0` diff of its defaults file across the range.
        playbook_outcomes: an exception to raise from each playbook run in turn; None runs
            clean, and the list running out means every later run is clean.
        healthy: what the Docker health gate reports for every service.
        discord_ok: whether Discord accepts each post.
        log: every call, oldest first, as ("git", argv), ("playbook", argv, kwargs),
            ("staging", services), ("annotation", services) or ("post", content).
        repo: the fake checkout REPO points at; `declare()` and `render()` populate it.
    """

    def __init__(self, repo: pathlib.Path) -> None:
        self.local = LOCAL
        self.origin = ORIGIN
        self.head = LOCAL
        self.origin_ahead = True
        self.local_ahead = False
        self.dirty = False
        self.ci = "pass"
        self.paths: list[str] = []
        self.files: dict[str, str] = {}
        self.tree_listing = ""
        self.diffs: dict[str, str] = {}
        self.playbook_outcomes: list[Exception | None] = []
        self.healthy = True
        self.discord_ok = True
        self.log: list[tuple] = []
        self.repo = repo

    # ── the scenario ──────────────────────────────────────────────────────────────────────────
    def declare(self, hostvars: str) -> None:
        """This host's containers_list, as the host_vars text main() reads."""
        path = self.repo / "ansible" / "inventory" / "host_vars" / "test-host.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hostvars)

    def render(self, service: str) -> None:
        """A rendered compose for `service`, which makes the health gate apply to it here."""
        path = self.repo / "containers" / service / "docker-compose.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("services: {}\n")

    # ── what main() sees ──────────────────────────────────────────────────────────────────────
    def run(self, argv: list[str], **kwargs) -> str:
        if argv[0] == "git":
            self.log.append(("git", argv))
            return self._git(argv)
        if argv[:4] == ["uv", "run", "--frozen", "ansible-playbook"]:
            self.log.append(("playbook", argv, kwargs))
            outcome = self.playbook_outcomes.pop(0) if self.playbook_outcomes else None
            if outcome is not None:
                raise outcome
            return ""
        raise AssertionError(f"unscripted command: {argv}")

    def _git(self, argv: list[str]) -> str:
        sub = argv[1]
        if sub == "rev-parse":
            return self.origin if argv[2].startswith("origin/") else self.head
        if sub == "diff" and argv[2] == "--name-only":
            return "\n".join(self.paths)
        if sub == "diff" and argv[2] == "-U0":
            return self.diffs.get(argv[-1].split("/")[3], "")
        if sub == "show":
            if argv[2] not in self.files:
                raise RuntimeError(
                    f"git show {argv[2]} -> 128 / fatal: path not scripted"
                )
            return self.files[argv[2]]
        if sub == "ls-tree":
            return self.tree_listing
        if sub in ("merge", "reset"):
            self.head = argv[-1]
            return ""
        raise AssertionError(f"unscripted git call: {argv}")

    def subprocess_run(self, argv: list[str], **_kwargs) -> subprocess.CompletedProcess:
        self.log.append(("git", argv))
        if argv[:2] == ["git", "status"]:
            stdout = (
                " M ansible/roles/k8s/sonarr/defaults/main.yml\n" if self.dirty else ""
            )
            return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")
        if argv[:2] == ["git", "fetch"]:
            return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
        if argv[:3] == ["git", "merge-base", "--is-ancestor"]:
            return subprocess.CompletedProcess(
                argv, 0 if self._is_ancestor(argv[3], argv[4]) else 1
            )
        raise AssertionError(f"unscripted subprocess call: {argv}")

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        if ancestor == descendant:
            return True
        if (ancestor, descendant) == (self.local, self.origin):
            return self.origin_ahead
        if (ancestor, descendant) == (self.origin, self.local):
            return self.local_ahead
        raise AssertionError(f"unscripted ancestry query: {ancestor} {descendant}")

    def discord(self, content: str) -> bool:
        self.log.append(("post", content))
        return self.discord_ok

    # ── what main() did ───────────────────────────────────────────────────────────────────────
    @property
    def git(self) -> list[list[str]]:
        return [entry[1] for entry in self.log if entry[0] == "git"]

    @property
    def merges(self) -> list[str]:
        """The target of every `git merge --ff-only`, in order."""
        return [argv[-1] for argv in self.git if argv[1:3] == ["merge", "--ff-only"]]

    @property
    def playbooks(self) -> list[list[str]]:
        return [entry[1] for entry in self.log if entry[0] == "playbook"]

    @property
    def posts(self) -> list[str]:
        return [entry[1] for entry in self.log if entry[0] == "post"]

    def index(self, kind: str, *needle: str) -> int:
        """Position in `log` of the first `kind` entry whose argv contains every `needle`;
        fails when there is none."""
        for i, entry in enumerate(self.log):
            if entry[0] == kind and all(n in entry[1] for n in needle):
                return i
        raise AssertionError(f"no {kind} call containing {needle} in {self.log}")


@pytest.fixture
def tick(gitops_deploy: ModuleType, monkeypatch, state_dir, tmp_path) -> ScriptedTick:
    """main() against a scripted checkout:

    git, ansible-playbook, the CI verdict, the health gate, the staging gate and Discord all answer
    from the ScriptedTick, and the state files live under `state_dir`. Nothing reaches a shell or
    the network.
    """
    repo = tmp_path / "repo"
    repo.mkdir()
    scripted = ScriptedTick(repo)
    monkeypatch.setattr(gitops_deploy, "REPO", str(repo))
    monkeypatch.setattr(gitops_deploy, "run", scripted.run)
    monkeypatch.setattr(subprocess, "run", scripted.subprocess_run)
    monkeypatch.setattr(gitops_deploy, "discord", scripted.discord)
    monkeypatch.setattr(gitops_deploy, "fetch_ci_verdict", lambda _sha: scripted.ci)
    monkeypatch.setattr(
        gitops_deploy, "service_healthy", lambda _svc, deadline=None: scripted.healthy
    )
    monkeypatch.setattr(
        gitops_deploy,
        "consult_staging",
        lambda services, _origin: scripted.log.append(("staging", set(services))),
    )
    monkeypatch.setattr(
        gitops_deploy,
        "emit_deploy_annotation",
        lambda services, _sha: scripted.log.append(("annotation", set(services))),
    )
    return scripted
