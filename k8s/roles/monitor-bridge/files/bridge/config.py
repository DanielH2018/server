"""Env-derived configuration for monitor-bridge — every threshold, URL, credential and window.

One frozen `Config`, built once by `load_config(os.environ)` in check.py's `main()` and passed
down as the first parameter of every check body, gate and net helper. Nothing here is read from
a module global, so nothing here can be patched: a test builds the config it wants with
`load_config({...})` or narrows a fixture with `dataclasses.replace(cfg, X=...)`, and what the
code under test reads is the object it was handed.

A patched module constant is a process-wide global with a test-shaped hole in it; a
parameter is not.

THE FIELDS LIVE IN THREE DOMAIN MODULES, composed here by inheritance so that flat `cfg.X`
access survives: `config_host` (disks, SMART, temperatures, the UPS, the Pi), `config_service`
(n8n, the *arr stack, the drills, Home Assistant) and `config_io` (B2, R2, Loki, the
webhooks).
Each declares a frozen dataclass and a builder taking `load_config`'s parsers as arguments, so
a leaf never imports this facade. What stays HERE is the loop and endpoint fields every domain
needs and the check filter.

BUILDING THIS MUST NOT RAISE. A ValueError on one malformed number would kill the pod during
import, before the heartbeat file existed and before any monitor could be told. `_int`/`_num`
below record the problem, fall back to a placeholder, and hand the whole list to
`main()` as `Config.CONFIG_PROBLEMS`, which prints one operator-readable line per problem and
exits 2.

A KEY THE POD'S ENVIRONMENT RENDERS HAS NO DEFAULT HERE. `templates/config/env.yaml.j2` holds the
value the pod runs with, and the role's `render_secrets` the credentials, so a second default in this package would be a copy the deploy never reads, and
the two would drift apart. A read with no default is required:
an absent key records a problem the same way a malformed one does. A default survives only on a
key the template does not render. `tests/bridge_core/test_config_reads_the_rendered_env.py` holds both
directions.

Constants only. The mutable per-check state (the streak counters and probe caches) is
`bridge.streaks.State`, which a check reads as `src.state`.
"""

from collections.abc import Callable, Mapping
from dataclasses import dataclass

from bridge.config_host import HostConfig, host_config
from bridge.config_io import IoConfig, io_config
from bridge.config_service import ServiceConfig, service_config


@dataclass(frozen=True)
class Config(HostConfig, ServiceConfig, IoConfig):
    """One process's configuration, as read from the environment by `load_config`.

    Frozen, and built once in `main()`. The three bases contribute the domain fields; what is
    declared here is what every domain needs or what has to stay in this file by name. Each
    field is documented at its read — that is where the env var name sits, and
    most of the reasoning is about the default rather than the type.

    EVERY CREDENTIAL-BEARING FIELD IS `field(repr=False)` in its domain module. A dataclass
    generates a `__repr__` over all of its fields, so without that a `print(cfg)`, an f-string
    in a log line, or a traceback rendering locals would put the HA token, both B2 keys, the
    Cloudflare token, the SMTP password, five Discord webhook URLs and five *arr API keys into
    the pod log — which Alloy ships to Loki. `test_repr_hides_every_credential_but_not_
    ordinary_config` pins both halves: the secrets absent, and ordinary config still present.
    That test anchors a sentinel LIST, so it cannot see a new credential added with neither
    marker nor sentinel; `tests/bridge_core/test_config_credential_shape.py` covers that by SHAPE, failing
    on any field named like a credential (TOKEN, KEY, PASSWORD, WEBHOOK, …) that the repr would
    still render.

    Attributes:
      CONFIG_PROBLEMS: one operator-readable line per env value that could not be parsed.
        Empty on a good config. Collecting them instead of raising is what keeps building this
        object safe at any point in startup; `main()` is where the list becomes an exit-2
        report.
    """

    INTERVAL: int
    HEARTBEAT_FILE: str
    PROM_URL: str
    LOKI_URL: str
    CHECKS_ONLY: frozenset[str]
    CHECKS_SKIP: frozenset[str]
    CONFIG_PROBLEMS: tuple[str, ...] = ()


def load_config(env: Mapping[str, str], problems: list[str] | None = None) -> Config:
    """Read `env` into a frozen `Config`. Never raises.

    Args:
      env: The environment to read. `os.environ` in the pod; a plain dict in a test, which is
        how a test states the whole configuration a check runs under instead of patching one
        constant at a time.
      problems: Malformed-value lines already collected elsewhere, carried into
        `Config.CONFIG_PROBLEMS` so `main()` reports them together. `bridge.common
        .CONFIG_PROBLEMS` is what check.py passes: `HTTP_TIMEOUT` is parsed there (it is shared
        with autofix-bridge, which does not ship this module), and without this its malformed
        value would be recorded into a list nothing reads.

    Returns:
      The `Config` every check, gate and net helper is then handed.
    """
    problems = [] if problems is None else list(problems)

    def _env(name: str, default: str | None = None) -> str:
        """The `name` env var. With no `default`, the key is REQUIRED.

        A required key is one `templates/config/env.yaml.j2` or `render_secrets` renders, so the
        template holds its value and this module holds none. Absent, it records a problem and reads
        as "" rather than raising, and `main()` exits 2 naming it.
        """
        if default is not None:
            return env.get(name, default)
        if name not in env:
            problems.append(
                "%s is unset; templates/config/env.yaml.j2 renders it, so this env is not "
                "the one the pod gets" % name
            )
        return env.get(name, "")

    def _parser[T](cast: Callable[[str], T], noun: str) -> Callable[..., T]:
        """A reader of one env var through `cast`, recording a malformed value instead of raising."""

        def read(name: str, default: str | None = None) -> T:
            raw = _env(name, default)
            try:
                return cast(raw)
            except ValueError:
                if name in env:  # an absent required key is already recorded by _env
                    problems.append(
                        "%s=%r is not %s; falling back to %s"
                        % (name, raw, noun, default or 0)
                    )
                return cast(default or "0")

        return read

    _int = _parser(int, "an integer")
    _num = _parser(float, "a number")

    def _env_file(name: str, default: str = "") -> str:
        """A secret from the file named by <name>_FILE if set, else the plain <name> env var.

        Inlined in the container environment, a secret lands in the pod's env, which envFrom
        cannot filter per key. Pointing <name>_FILE at a 0600 mounted file keeps it out of
        every process's environment. Trailing whitespace is stripped so
        a rendered newline can't corrupt the value.

        A read error (the file went missing, or the mount source was auto-created as a
        directory because the host file was absent) falls back to the plain env var rather
        than raising: an unguarded open() would fail the whole config build over one missing
        file, disabling every check instead of just the one whose credential is empty.
        """
        path = env.get(name + "_FILE", "")
        if path:
            try:
                with open(path, encoding="utf-8") as fh:
                    return fh.read().strip()
            except OSError:
                pass
        return _env(name, default)

    def _name_set(value: str) -> frozenset[str]:
        """A comma-separated check-name list as a set, tolerating spaces and empty entries."""
        return frozenset(n for n in value.replace(" ", "").split(",") if n)

    # Read ahead of the Config() call because B2_TRANSPORT_RETRY_S defaults to INTERVAL.
    INTERVAL = _int("INTERVAL")
    PROM_URL = _env("PROMETHEUS_URL").rstrip("/")

    return Config(
        **vars(host_config(_env, _int, _num, _env_file)),
        **vars(service_config(_env, _int, _num, _env_file)),
        **vars(io_config(_env, _int, _num, _env_file, problems)),
        INTERVAL=INTERVAL,
        # Touched after every completed cycle; the healthcheck compares its mtime against
        # ~3xINTERVAL, so autoheal restarts a hung bridge that shows only as stale gauges.
        HEARTBEAT_FILE=_env("HEARTBEAT_FILE", "/tmp/heartbeat"),
        PROM_URL=PROM_URL,
        LOKI_URL=_env("LOKI_URL").rstrip("/"),
        # CHECKS_ONLY enables exactly that set and CHECKS_SKIP drops names; the three reachability
        # gates take the names their monitors push as, and a gate the filter leaves out reads as up.
        # cli.py's main() refuses to start on an unknown name, so a mistyped filter is loud.
        # `check.py --check <name>` overrides CHECKS_ONLY for a hand run.
        CHECKS_ONLY=_name_set(_env("CHECKS_ONLY", "")),
        CHECKS_SKIP=_name_set(_env("CHECKS_SKIP", "")),
        CONFIG_PROBLEMS=tuple(problems),
    )
