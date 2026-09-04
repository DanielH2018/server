# ansible/roles/setup/gitops_deploy/files/deploy_config.py
"""The deployer's configuration: how the env file is read, the frozen `Config`, and `log`.

`/etc/gitops-deploy/config.env` is parsed once, when `gitops_deploy` imports, and parsing
cannot fail. `load_config` collects a malformed numeric value into `Config.errors` and keeps
that field's default, so `main()` names the offending key in one line instead of dying in an
import traceback. `Config.validate()` is the half that raises, and it raises `ConfigError`.

`log` lives here because every module prints through it and it depends on nothing else.

This is a leaf: it imports `host_lib` and the standard library, and nothing else from this
role. No test patches a name defined here, so a caller may from-import one — unlike
`deploy_io`, where a from-import would take its own reference and never see the `monkeypatch`.
`test_gitops_deploy_patch_boundary.py` reports it if that ever stops being true. `deploy_io`
also re-exports these names for the suite, which reads them through the module it always has.

Stdlib only: the unit runs under `uv run --no-project` and the host is still on Python 3.12.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field

from host_lib import parse_env_file


def log(msg: str) -> None:
    print(msg, flush=True)


# ── configuration ─────────────────────────────────────────────────────────────────────────────


class ConfigError(Exception):
    """The deployer's config file holds a value it cannot be run with.

    Raised by `Config.validate()`, never by parsing — see `load_config` for why the two are
    separate.
    """


def csv_set(raw: str) -> frozenset[str]:
    return frozenset(s.strip() for s in raw.split(",") if s.strip())


@dataclass(frozen=True)
class Config:
    """One tick's configuration, as read from /etc/gitops-deploy/config.env.

    Frozen: nothing rebinds a value mid-tick, and the CI gate and the k8s denylist both
    disarm by deriving a NEW value rather than by writing one back.

    Attributes:
        errors: one message per key whose value could not be parsed. Empty on a good config.
            Parsing records them instead of raising so that IMPORTING the deployer cannot fail
            — `validate()` is where a bad config becomes a reportable error.
    """

    repo: str = ""
    branch: str = "master"
    hostname: str = "unknown-host"
    discord_webhook: str = ""
    health_timeout_s: int = 300
    run_budget_s: int = 1020
    require_ci: bool = False
    ci_contexts: frozenset[str] = frozenset()
    ci_repo: str = ""
    k8s_autodeploy_enabled: bool = False
    k8s_autodeploy_pilot: frozenset[str] = frozenset()
    k8s_autodeploy_denylist: frozenset[str] = frozenset()
    k8s_autodeploy_max_per_tick: int = 3
    k8s_autodeploy_max_claim_services_per_tick: int = 1
    k8s_deploy_timeout_s: int = 900
    k8s_rollback_timeout_s: int = 1320
    broad_deploy_timeout_s: int = 1800
    staging_gate: bool = False
    staging_gate_blocking: bool = False
    staging_gate_timeout_s: int = 600
    staging_expect_timeout_s: int = 120
    # STAGING_SUBSET is deliberately NOT here. Its literal fallback is parsed out of
    # gitops_deploy.py by scripts/docs/gen_doc_fragments.py, which looks for a
    # `C.get("<KEY>", "<literal>")` call in that file by name — so moving it here would leave
    # the published fragment with no source. It stays a module constant there. The two staging
    # timeouts above used to live there too; gitops_deploy.py keeps a vestigial `C.get(...)`
    # call for each purely so the generator still has one to read — see the comment there.
    errors: tuple[str, ...] = field(default=())

    def validate(self) -> None:
        """Raise `ConfigError` naming every unparseable key, or return.

        Args are none; the errors were collected at parse time.

        Raises:
            ConfigError: at least one key held a value this deployer cannot use. One line
                naming every offending key and what it held — a tick that dies on its config
                has to say which key, and it used to die during import with a bare
                `ValueError: invalid literal for int()` and no key name in it.
        """
        if self.errors:
            raise ConfigError("unusable deployer config: " + "; ".join(self.errors))


def read_config_file(path: str) -> dict[str, str]:
    """The KEY=VALUE pairs in `path`, or {} when the file is absent.

    A missing file used to crash the import, which paged through the unit's OnFailure. It now
    leaves every value at its default and `repo` empty, and `main()` refuses to tick on an
    empty repo: the same page, raised from a function a test can call.
    """
    try:
        return parse_env_file(path)
    except FileNotFoundError:
        return {}


def load_config(env: Mapping[str, str]) -> Config:
    """Parse `env` into a `Config`. Never raises.

    A malformed numeric value is recorded in `Config.errors` and the field keeps its default,
    so importing the deployer cannot fail on a half-written config.env — the failure surfaces
    from `Config.validate()` inside `main()`, where it can be logged and posted rather than
    printed as an import traceback before the heartbeat exists.

    `require_ci` also disarms itself here, loudly, when `REQUIRE_CI=true` but `CI_CONTEXTS` or
    `GITHUB_REPO` is empty (a half-rendered config.env) — done in this one place so `Config` is
    never split from the value a caller reads off it.
    """
    errors: list[str] = []

    def _int(key: str, default: int) -> int:
        raw = env.get(key)
        if raw is None:
            return default
        try:
            return int(raw)
        except ValueError:
            errors.append(f"{key}={raw!r} is not a whole number")
            return default

    def _bool(key: str, default: bool = False) -> bool:
        return env.get(key, str(default).lower()).lower() == "true"

    require_ci = _bool("REQUIRE_CI")
    ci_contexts = csv_set(env.get("CI_CONTEXTS", ""))
    ci_repo = env.get("GITHUB_REPO", "")
    if require_ci and not (ci_contexts and ci_repo):
        # Fail closed the same way the k8s denylist does, but in the opposite direction: an
        # empty context list would make ci_verdict() return `pass` for everything, turning a
        # half-rendered config.env into a silently ungated deployer. Better to disarm loudly.
        log(
            "REQUIRE_CI is set but CI_CONTEXTS/GITHUB_REPO is empty — disabling the CI gate"
        )
        require_ci = False

    return Config(
        repo=env.get("REPO_DIR", ""),
        branch=env.get("BRANCH", "master"),
        hostname=env.get("HOSTNAME", "unknown-host"),
        discord_webhook=env.get("DISCORD_WEBHOOK", ""),
        health_timeout_s=_int("HEALTH_TIMEOUT_S", 300),
        run_budget_s=_int("RUN_BUDGET_S", 1020),
        require_ci=require_ci,
        ci_contexts=ci_contexts,
        ci_repo=ci_repo,
        k8s_autodeploy_enabled=_bool("K8S_AUTODEPLOY_ENABLED"),
        k8s_autodeploy_pilot=csv_set(env.get("K8S_AUTODEPLOY_PILOT", "")),
        k8s_autodeploy_denylist=csv_set(env.get("K8S_AUTODEPLOY_DENYLIST", "")),
        k8s_autodeploy_max_per_tick=_int("K8S_AUTODEPLOY_MAX_PER_TICK", 3),
        k8s_autodeploy_max_claim_services_per_tick=_int(
            "K8S_AUTODEPLOY_MAX_CLAIM_SERVICES_PER_TICK", 1
        ),
        k8s_deploy_timeout_s=_int("K8S_DEPLOY_TIMEOUT_S", 900),
        k8s_rollback_timeout_s=_int("K8S_ROLLBACK_TIMEOUT_S", 1320),
        broad_deploy_timeout_s=_int("BROAD_DEPLOY_TIMEOUT_S", 1800),
        staging_gate=_bool("STAGING_GATE"),
        staging_gate_blocking=_bool("STAGING_GATE_BLOCKING"),
        staging_gate_timeout_s=_int("STAGING_GATE_TIMEOUT_S", 600),
        staging_expect_timeout_s=_int("STAGING_EXPECT_TIMEOUT_S", 120),
        errors=tuple(errors),
    )
