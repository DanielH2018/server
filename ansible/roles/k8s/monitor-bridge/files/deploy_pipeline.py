"""The deploy-pipeline exporter: five gauges on the `promote` workflow's state, for Prometheus.

`promote` keeps its state in GitHub and GHCR, where nothing pages: the repository variables
DEPLOY_HOLD and PROD_DIGEST, master's green CI runs, and the `prod` tag. Each cycle `collect`
reads that state, and `Exposition` serves the result to observability's `deploy-pipeline`
scrape job, whose rule group pages on it:

- `homelab_deploy_hold_active` reads 1 while DEPLOY_HOLD reads anything but `none`.
- `homelab_deploy_hold_age_seconds` is the time since DEPLOY_HOLD was last written while a hold
  stands, and 0 when none does.
- `homelab_deploy_unpromoted_age_seconds` is the time since the oldest green master commit that
  `prod`'s commit does not contain went green, and 0 when there is none.
- `homelab_deploy_prod_digest_match` reads 1 when `prod`'s digest in GHCR equals PROD_DIGEST.
  While PROD_DIGEST reads `none` and no `prod` tag exists, nothing has moved `prod`, so it
  reads 1 then too.
- `homelab_deploy_reboot_window_active` reads 1 inside the weekly reboot's no-promotion window,
  where `promote.py decide` defers every promotion, so PromotionStalled stays quiet there. It
  reads no remote state, only the `WEEKLY_REBOOT_*` schedule.

This is the one family the bridge serves rather than pushes to Kuma. It reads no Prometheus,
so no reachability gate applies to it, and it runs after every check has pushed.

Per cycle it sends three GETs to api.github.com (the variables, master's green CI runs, one
compare) and two to ghcr.io (an anonymous pull token, the `prod` manifest). It sends no other
method, so it changes nothing even with a token that could. A read that fails, and a variable
that does not exist, export no series for the gauges they feed rather than a 0, and
DeployPipelineExporterAbsent pages a gauge that stays missing.

Design: docs/target-architecture/deploy/d2-first-leaf.md, *The deploy-pipeline exporter and its
alerts*.
"""

import hashlib
import json
import threading
import urllib.error
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bridge.common
from bridge.config import Config
from bridge.sources import Sources
from bridge.types import JsonValue, as_object, as_object_list

# The names `promote` uses, restated because the pod ships only this role's files/ and cannot
# import scripts/deploy_tools/forge.py. tests/test_deploy_pipeline.py holds each one to forge's.
REPOSITORY = "DanielH2018/server"
ARTIFACT = "danielh2018/homelab-manifests"
REGISTRY = "ghcr.io"
HOLD_VARIABLE = "DEPLOY_HOLD"
DIGEST_VARIABLE = "PROD_DIGEST"
# `flux push artifact --revision master@sha1:<sha>` writes the commit under this annotation.
REVISION_ANNOTATION = "org.opencontainers.image.revision"
REVISION_PREFIX = "master@sha1:"
MANIFEST_ACCEPT = ", ".join(
    (
        "application/vnd.oci.image.manifest.v1+json",
        "application/vnd.oci.image.index.v1+json",
        "application/vnd.docker.distribution.manifest.v2+json",
    )
)
API = "https://api.github.com/repos/" + REPOSITORY
# `promote` runs on each completed push run of the CI workflow on master, so a green master
# commit is the head of one of these runs.
CI_RUNS = (
    API + "/actions/workflows/ci.yml/runs"
    "?branch=master&event=push&status=success&exclude_pull_requests=true&per_page=%d"
)
# Each run in the listing is about 13.5 kB of JSON. Measured from daniel-box on 2026-10-15, 20
# runs answered in 0.75-1.92s and 50 in 3.2-6.7s, against HTTP_TIMEOUT's 10s. While more than 20
# green commits wait, `prod`'s own run falls off the page and the gauge reads the oldest listed
# run's age, which is younger than the real one. PromotionStalled fires at 2 hours, and 20
# merges in 2 hours has not happened here.
RUNS_PER_PAGE = 20
NO_HOLD = "none"
# `chained_timeout.VERIFY_CAP_S`, the ceiling of every target's verify timeout, restated for the
# same reason as the names above. The reboot window opens one target's timeout before the
# reboot, and the exporter holds no target, so it reports the widest window any target has, as
# `land-wait` does. tests/test_deploy_pipeline_reboot_window.py holds it to chained_timeout's.
VERIFY_CAP_S = 330 * 60

GAUGES = (
    (
        "homelab_deploy_hold_active",
        "1 while the repository variable DEPLOY_HOLD holds promotion.",
    ),
    (
        "homelab_deploy_hold_age_seconds",
        "Seconds since DEPLOY_HOLD was last written while a hold stands, else 0.",
    ),
    (
        "homelab_deploy_unpromoted_age_seconds",
        "Seconds since the oldest green master commit that prod lacks went green, else 0.",
    ),
    (
        "homelab_deploy_prod_digest_match",
        "1 when the prod tag's digest in GHCR equals PROD_DIGEST.",
    ),
    (
        "homelab_deploy_reboot_window_active",
        "1 inside the weekly reboot's no-promotion window, where promote defers.",
    ),
)


def _api_headers(cfg: Config) -> dict[str, str]:
    return {
        "Authorization": "Bearer " + cfg.DEPLOY_PIPELINE_GITHUB_TOKEN,
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


def _time(stamp: JsonValue) -> float:
    """A GitHub API timestamp (`2026-10-15T14:31:06Z`) as seconds since the epoch."""
    return datetime.fromisoformat(str(stamp)).timestamp()


def read_variables(cfg: Config, src: Sources) -> dict[str, dict]:
    """The repository's Actions variables by name, from one GET."""
    body = as_object(
        src.get_json(API + "/actions/variables", _api_headers(cfg)), "variables"
    )
    return {
        str(v["name"]): v
        for v in as_object_list(body.get("variables"), "variables.variables")
    }


def read_prod(src: Sources) -> tuple[str, str] | None:
    """`prod`'s `(digest, commit)` from GHCR, or None when the tag does not exist.

    The digest is the sha256 of the manifest bytes the registry serves, which is what
    `Docker-Content-Digest` reports. The commit comes from the revision annotation.

    Raises:
        ValueError: the manifest carries no `master@sha1:` revision.
    """
    token = as_object(
        src.get_json(
            "https://%s/token?scope=repository:%s:pull&service=%s"
            % (REGISTRY, ARTIFACT, REGISTRY)
        ),
        "GHCR token",
    )["token"]
    try:
        body = src.get_bytes(
            "https://%s/v2/%s/manifests/prod" % (REGISTRY, ARTIFACT),
            {"Authorization": "Bearer %s" % token, "Accept": MANIFEST_ACCEPT},
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    manifest = as_object(json.loads(body), "prod manifest")
    annotations = manifest.get("annotations") or {}
    revision = str(as_object(annotations, "annotations").get(REVISION_ANNOTATION, ""))
    if not revision.startswith(REVISION_PREFIX):
        raise ValueError("prod's manifest has no %s revision" % REVISION_PREFIX)
    return "sha256:" + hashlib.sha256(body).hexdigest(), revision[
        len(REVISION_PREFIX) :
    ]


def prod_contains(cfg: Config, src: Sources, commit: str, prod_commit: str) -> bool:
    """Whether `prod_commit` contains `commit`, from one compare call.

    The compare runs `commit...prod_commit`. While `prod` lacks the commit, the usual case when
    this is asked, `prod` reads `behind` or `diverged` and the response lists no commits and no
    files: 13 kB in 0.5s, measured on 2026-10-15. The other orientation lists every commit and
    file `prod` lacks, 820 kB for 30 commits.
    """
    body = as_object(
        src.get_json(
            "%s/compare/%s...%s?per_page=1" % (API, commit, prod_commit),
            _api_headers(cfg),
        ),
        "compare",
    )
    return body.get("status") in ("ahead", "identical")


def unpromoted_age(
    cfg: Config, src: Sources, prod_commit: str | None, now: float
) -> float:
    """Seconds since the oldest green master commit that `prod` lacks went green, or 0.

    Master is linear, so ordering the green runs by `created_at`, the push time, orders their
    commits. The candidates are the runs pushed after `prod`'s own, or every listed run when
    `prod`'s commit is not on the page. The compare call then checks the oldest candidate
    against `prod`, so a page that misses `prod`'s run cannot report a commit `prod` already
    holds. The age runs from `updated_at`, when the run went green and the commit became
    promotable, rather than from the commit date.
    """
    body = as_object(src.get_json(CI_RUNS % RUNS_PER_PAGE, _api_headers(cfg)), "runs")
    green: dict[str, tuple[float, float]] = {}
    for run in as_object_list(body.get("workflow_runs"), "runs.workflow_runs"):
        sha = str(run["head_sha"])
        pushed, greened = _time(run["created_at"]), _time(run["updated_at"])
        if sha not in green or pushed < green[sha][0]:
            green[sha] = (pushed, greened)
    if prod_commit in green:
        candidates = {s: t for s, t in green.items() if t[0] > green[prod_commit][0]}
    else:
        candidates = green
    if not candidates:
        return 0.0
    oldest = min(candidates, key=lambda s: candidates[s][0])
    if prod_commit is not None and prod_contains(cfg, src, oldest, prod_commit):
        return 0.0
    return max(0.0, now - candidates[oldest][1])


def in_reboot_window(cfg: Config, now: float) -> bool:
    """Whether `now` falls inside the weekly reboot's no-promotion window for the widest target.

    The same window `scripts/deploy_tools/promote_lib/reboot_window.py:window` derives, which
    the pod cannot import: it opens `VERIFY_CAP_S` before the reboot and closes, exclusive, when
    M2's mute ends, so the scheduled `promote` run at the end promotes rather than defers. The
    schedule is UTC, as `reboot_window.read_schedule` requires, and the pod's `TZ` is not, so
    the clock is read in UTC explicitly. tests/test_deploy_pipeline_reboot_window.py sweeps a
    week against `reboot_window.window` to hold the two equal.
    """
    clock = datetime.fromtimestamp(now, UTC)
    # datetime counts weekdays from Monday, cron from Sunday.
    cron_day = (clock.weekday() + 1) % 7
    back = timedelta(days=(cron_day - cfg.WEEKLY_REBOOT_WEEKDAY) % 7)
    fire = (clock - back).replace(
        hour=cfg.WEEKLY_REBOOT_HOUR,
        minute=cfg.WEEKLY_REBOOT_MINUTE,
        second=0,
        microsecond=0,
    )
    for week in (-1, 0, 1):
        reboot = fire + timedelta(
            weeks=week, minutes=cfg.WEEKLY_REBOOT_SHUTDOWN_DELAY_MIN
        )
        start = reboot - timedelta(seconds=VERIFY_CAP_S)
        end = reboot + timedelta(minutes=cfg.WEEKLY_REBOOT_MUTE_RECOVERY_MIN)
        if start <= clock < end:
            return True
    return False


def collect(
    cfg: Config, src: Sources, now: float
) -> tuple[dict[str, float], list[str]]:
    """One cycle's gauges, and a line for each read that failed.

    A gauge is missing from the result when its read failed or its variable does not exist.
    With DEPLOY_PIPELINE_GITHUB_TOKEN empty the family reads nothing and every gauge is missing.

    Args:
      cfg: The configuration holding the token.
      src: Sends each GET.
      now: Seconds since the epoch, that the ages are measured to.

    Returns:
      `(gauges, problems)`: the value of each gauge read, keyed by metric name, and one
      operator-readable line per failed read.
    """
    if not cfg.DEPLOY_PIPELINE_GITHUB_TOKEN:
        return {}, ["DEPLOY_PIPELINE_GITHUB_TOKEN is empty, so nothing is read"]
    # Read from the schedule alone, so a failed GitHub read never drops it.
    gauges: dict[str, float] = {
        "homelab_deploy_reboot_window_active": 1.0
        if in_reboot_window(cfg, now)
        else 0.0
    }
    problems: list[str] = []
    variables = None
    try:
        variables = read_variables(cfg, src)
    except Exception as e:
        problems.append("variables: %s" % e)
    if variables is not None and HOLD_VARIABLE in variables:
        hold = variables[HOLD_VARIABLE]
        active = str(hold.get("value", "")).strip() != NO_HOLD
        gauges["homelab_deploy_hold_active"] = 1.0 if active else 0.0
        gauges["homelab_deploy_hold_age_seconds"] = (
            max(0.0, now - _time(hold["updated_at"])) if active else 0.0
        )
    elif variables is not None:
        problems.append("the variable %s does not exist" % HOLD_VARIABLE)
    prod: tuple[str, str] | None = None
    prod_read = False
    try:
        prod = read_prod(src)
        prod_read = True
    except Exception as e:
        problems.append("prod: %s" % e)
    if prod_read and variables is not None and DIGEST_VARIABLE in variables:
        recorded = str(variables[DIGEST_VARIABLE].get("value", "")).strip()
        live = NO_HOLD if prod is None else prod[0]
        gauges["homelab_deploy_prod_digest_match"] = 1.0 if recorded == live else 0.0
    elif variables is not None and DIGEST_VARIABLE not in variables:
        problems.append("the variable %s does not exist" % DIGEST_VARIABLE)
    if prod_read:
        try:
            gauges["homelab_deploy_unpromoted_age_seconds"] = unpromoted_age(
                cfg, src, None if prod is None else prod[1], now
            )
        except Exception as e:
            problems.append("unpromoted: %s" % e)
    return gauges, problems


def render(gauges: Mapping[str, float]) -> bytes:
    """The Prometheus text exposition of `gauges`, one series each, in `GAUGES` order."""
    lines = []
    for name, help_text in GAUGES:
        if name in gauges:
            lines += [
                "# HELP %s %s" % (name, help_text),
                "# TYPE %s gauge" % name,
                "%s %s" % (name, repr(float(gauges[name]))),
            ]
    return ("\n".join(lines) + "\n").encode() if lines else b""


class Exposition:
    """The body the scrape endpoint serves: the last cycle's gauges, and nothing else.

    No process or runtime families: the series budget prices this job at the five gauges plus
    Prometheus's five automatic series. A thread serving `serve`'s listener reads the body while
    the loop's thread replaces it, so both go through one lock.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._body = b""

    def publish(self, gauges: Mapping[str, float]) -> None:
        """Replace the served body with `gauges`."""
        body = render(gauges)
        with self._lock:
            self._body = body

    def body(self) -> bytes:
        """The body a scrape receives."""
        with self._lock:
            return self._body

    def serve(self, port: int) -> ThreadingHTTPServer:
        """Serve `GET /metrics` on `port`, on all addresses, from a daemon thread.

        Returns:
          The server, already serving. The pod never stops it; a test calls `shutdown()`.

        Raises:
          OSError: the port could not be bound.
        """
        exposition = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.split("?", 1)[0] != "/metrics":
                    self.send_error(404)
                    return
                body = exposition.body()
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; version=0.0.4")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                # A scrape a minute would put a line a minute into Loki for nothing.
                pass

        server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        return server


def refresh(cfg: Config, src: Sources, exposition: Exposition, now: float) -> None:
    """Collect one cycle's gauges, publish them, and log one line. Never raises.

    The loop calls this after every check has pushed, so a slow GitHub delays no Kuma push.
    """
    try:
        gauges, problems = collect(cfg, src, now)
    except Exception as e:
        gauges, problems = {}, ["collect: %s" % e]
    exposition.publish(gauges)
    bridge.common.log(
        "deploy-pipeline: %s%s"
        % (
            " ".join("%s=%g" % (n, v) for n, v in sorted(gauges.items()))
            or "no series",
            "".join("; " + p for p in problems),
        )
    )
