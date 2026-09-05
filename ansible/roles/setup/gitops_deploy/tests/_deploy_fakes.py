"""Fakes for every `DeployTools` boundary, so the deployer's suite patches almost nothing.

`ScriptedTick` is the whole scenario for one `main()` call: what git, ansible-playbook, GitHub,
the staging scripts, the health gate and Discord answer, and what the tick did to them. Set the
attributes before the call, then read `log` (every call, oldest first) afterwards. `build_tools`
turns one into the `DeployTools` `main(tools)` takes.

WHAT IS DELIBERATELY NOT A FIELD. `deploy_io.deploy`, `deploy_k8s` and `deploy_broad` build the
`ansible-playbook` argv the suite asserts on and reach `deploy_io.run` qualified, so replacing
them here would retire the assertion rather than fake the process. The `tick` fixture patches
`deploy_io.run` — one module attribute, the last one — and conftest.py says why threading a
runner into those three is deferred.

`staging_verdict` is a word, not a return value: `run_staging_scripts` maps it to the exit-code
pair `deploy_staging.staging_verdict` reads, so the real `consult_staging` produces the verdict
and every alert, ledger write and block that follows from it.
"""

import pathlib
import subprocess
from datetime import datetime
from zoneinfo import ZoneInfo

from deploy_toolbox import DeployTools

LOCAL = "1" * 40
ORIGIN = "2" * 40
# A fixed, tz-aware clock: `handle_dirty` reads it to pick the throttle slot, so a real
# `datetime.now` would make that branch depend on the hour the suite runs at.
CLOCK = datetime(2026, 9, 1, 12, 0, tzinfo=ZoneInfo("America/Chicago"))
# The (deploy_rc, expect_rc) pair each verdict word comes from. Read `staging_verdict` for the
# branch order; these are the three pairs it maps onto, and SKIPPED is not one of them — that
# word comes from the gate being off or nothing being in scope, never from a script's exit.
STAGING_RCS = {"pass": (0, 0), "rejected": (1, 0), "no_verdict": (2, 2)}


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
        ci: what the CI gate reports for origin.
        paths: what `git diff --name-only local..origin` lists.
        files: `"<ref>:<path>"` to the content `git show` returns for it.
        tree_listing: what `git ls-tree` at origin lists under roles/k8s/.
        diffs: k8s service to the `-U0` diff of its defaults file across the range. Every
            service the image-diff read reaches for must have an entry, `""` included.
        playbook_outcomes: an exception to raise from each playbook run in turn; None runs
            clean, and the list running out means every later run is clean.
        run_error: raised by every `run()` call instead of answering, for the paths that must
            survive a git failure.
        healthy: what the Docker health gate reports, PER SERVICE. `render()` seeds a service
            as healthy, so a test flips one name. A name nobody rendered is vacuously healthy,
            exactly as production is; a RENDERED service with no entry is an AssertionError.
        staging_verdict: the word the staging scripts' exit codes stand for — "pass",
            "rejected" or "no_verdict". "skipped" is not settable here; it is what the real
            `consult_staging` returns when the gate is off or nothing is in scope.
        discord_ok: whether Discord accepts each post.
        log: every call, oldest first, as ("git", argv), ("playbook", argv, kwargs),
            ("staging", services), ("annotation", services) or ("post", content).
        repo: the fake checkout REPO points at; `declare()` and `render()` populate it.
        override_file: the staging override marker, at the path `state_dir` repointed it to.
    """

    def __init__(
        self, repo: pathlib.Path, override_file: pathlib.Path | None = None
    ) -> None:
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
        self.run_error: Exception | None = None
        self.healthy: dict[str, bool] = {}
        self.staging_verdict = "pass"
        self.discord_ok = True
        self.log: list[tuple] = []
        self.repo = repo
        self.override_file = override_file or repo / "staging_override"
        # The DeployTools built from this object; the `tick` fixture fills it in, and every
        # test passes it to main() rather than the fixture injecting it behind their back.
        self.tools: DeployTools | None = None

    # ── the scenario ──────────────────────────────────────────────────────────────────────────
    def declare(self, hostvars: str) -> None:
        """This host's containers_list, as the host_vars text main() reads."""
        path = self.repo / "ansible" / "inventory" / "host_vars" / "test-host.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(hostvars)

    def render(self, service: str) -> None:
        """A rendered compose for `service`, which makes the health gate apply to it here.

        Rendering is also what scripts the health gate's answer for that service — healthy
        unless the test flips `healthy[service]` afterwards.
        """
        path = self.repo / "containers" / service / "docker-compose.yml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("services: {}\n")
        self.healthy.setdefault(service, True)

    @property
    def staging_override(self) -> bool:
        """Whether the operator's one-tick override is armed — the marker file itself.

        A property over the real file, not a flag: `consume_staging_override` spends the
        override by REMOVING it, so reading the file is what proves it was one-shot.
        """
        return self.override_file.exists()

    @staging_override.setter
    def staging_override(self, armed: bool) -> None:
        if armed:
            self.override_file.parent.mkdir(parents=True, exist_ok=True)
            self.override_file.write_text("")
        elif self.override_file.exists():
            self.override_file.unlink()

    # ── what main() sees ──────────────────────────────────────────────────────────────────────
    def run(self, argv: list[str], *, cwd: str | None = None, **kwargs) -> str:
        if self.run_error is not None:
            raise self.run_error
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
            service = argv[-1].split("/")[3]
            # No `.get(..., "")` default. An empty diff is a REAL production answer (the
            # range touched no line of that defaults file), so a silent default would let a
            # test that forgot to script one read as "nothing changed" and pass. Every other
            # `run()` path raises on an unscripted call; this is the one that did not.
            assert service in self.diffs, (
                f"the image-diff read asked for {service!r}, which no test scripted "
                f"(scripted: {sorted(self.diffs)}). Script an empty diff as "
                f"diffs[{service!r}] = '' when that is what the test means."
            )
            return self.diffs[service]
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

    def git_status(self, _repo: str) -> subprocess.CompletedProcess:
        argv = ["git", "status", "--porcelain"]
        self.log.append(("git", argv))
        stdout = " M ansible/roles/k8s/sonarr/defaults/main.yml\n" if self.dirty else ""
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr="")

    def git_fetch(self, _repo: str, branch: str) -> subprocess.CompletedProcess:
        argv = ["git", "fetch", "origin", branch]
        self.log.append(("git", argv))
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    def is_ancestor(self, _repo: str, ancestor: str, descendant: str) -> bool:
        self.log.append(
            ("git", ["git", "merge-base", "--is-ancestor", ancestor, descendant])
        )
        if ancestor == descendant:
            return True
        if (ancestor, descendant) == (self.local, self.origin):
            return self.origin_ahead
        if (ancestor, descendant) == (self.origin, self.local):
            return self.local_ahead
        raise AssertionError(f"unscripted ancestry query: {ancestor} {descendant}")

    def fetch_ci_verdict(self, sha: str) -> str:
        """The scripted verdict, and only for the SHA the gate is supposed to ask about.

        The gate reads origin's verdict; asking about `local` would be the bug, and a fake
        that ignored its argument would answer the same either way.
        """
        assert sha == self.origin, (
            f"the CI gate asked about {sha[:8]}, not origin {self.origin[:8]}"
        )
        return self.ci

    def service_healthy(
        self, repo: str, service: str, _timeout: float, deadline: float | None = None
    ) -> bool:
        """What production reports for `service`, INCLUDING its vacuously-healthy case.

        `deploy_io.service_healthy` is `all(health_ok(c) for c in containers_for(...))`, and
        `containers_for` returns [] for a service whose compose was never rendered on this
        host (dozzle is daniel-pi-only). `all([])` is True, so that service passes the gate
        without a container ever being polled. This fake reproduces that: no rendered compose
        means True, which is what makes the vacuous path testable at all.

        Everything else here is a scripting error rather than an answer. A RENDERED service
        the test never scripted raises, as before. So does a `healthy[svc] = False` for a
        service nobody rendered — production cannot report that, so a test leaning on it
        would be asserting against a state the deployer never sees.
        """
        rendered = (
            pathlib.Path(repo) / "containers" / service / "docker-compose.yml"
        ).is_file()
        if not rendered:
            assert self.healthy.get(service, True), (
                f"the test scripted {service!r} UNHEALTHY but never rendered its compose; "
                f"production gates the containers of a rendered compose and reports a "
                f"service with none as healthy (all([]) is True). Call render({service!r}) "
                f"first if the gate is meant to see it."
            )
            return True
        assert service in self.healthy, (
            f"the health gate asked about {service!r}, which no test scripted "
            f"(scripted: {sorted(self.healthy)})"
        )
        return self.healthy[service]

    def run_staging_scripts(
        self, _repo: str, _sha: str, tags: str, _gate_s: float, _expect_s: float
    ) -> tuple[int, int]:
        self.log.append(("staging", set(tags.split(","))))
        return STAGING_RCS[self.staging_verdict]

    def emit_deploy_annotation(self, services: set[str], _sha: str) -> None:
        self.log.append(("annotation", set(services)))

    def now(self, tz=None) -> datetime:
        return CLOCK if tz is None else CLOCK.astimezone(tz)

    def discord_post(self, _webhook: str, content: str) -> bool:
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


def build_tools(scripted: ScriptedTick) -> DeployTools:
    """The `DeployTools` that answers every boundary from `scripted`."""
    return DeployTools(
        run=scripted.run,
        git_fetch=scripted.git_fetch,
        git_status=scripted.git_status,
        is_ancestor=scripted.is_ancestor,
        fetch_ci_verdict=scripted.fetch_ci_verdict,
        discord_post=scripted.discord_post,
        service_healthy=scripted.service_healthy,
        run_staging_scripts=scripted.run_staging_scripts,
        emit_deploy_annotation=scripted.emit_deploy_annotation,
        now=scripted.now,
    )
