"""The forge adapter: every GitHub and GHCR call that `promote.py` makes.

`promote.py` decides which published master commit the cluster's `prod` tag moves to. Every
fact it reads from outside the checkout comes through this module:

- `Registry`, in `forge_registry.py` and re-exported here, speaks the OCI distribution API to ghcr.io: the token request, the manifest
  fetch and the manifest `HEAD`, and a bundle's files read out of its layer, for `decide`'s
  pulls and `land-wait`. It lists no tags, so a run's request count does not grow with the
  number of published versions.
- `GitHub` reads a repository variable through `gh variable get`, and a commit's check-runs,
  commit statuses and pull requests through the REST API. It writes one variable, DEPLOY_HOLD,
  through `gh variable set`, for `decide`'s forward-only gate hold. For `promote.py prune` it also lists the
  artifact's package versions and deletes one, through the Packages API.
- `Cosign` runs `cosign verify` under the identity the cluster's OCIRepository checks.
- `Discord` posts `promote.py hold`'s messages to the deployer's channel, through the webhook
  in the `DEPLOY_DISCORD_WEBHOOK` repository secret. It never prints the URL.

Each read returns a value, returns None for "absent", or raises `ForgeError`. What absent and
error MEAN for a promotion is `promote.py`'s business, not this module's. A forge move
rewrites this module and leaves `promote.py` alone.

Every HTTP request goes through a `Transport`, a callable taking the method, the URL and the
headers: `urllib_transport`, or the tests' fake, which models a registry and counts requests.

The plan is `docs/target-architecture/deploy/d1-flux-installed.md`, *Decisions*,
*Every GitHub and GHCR call goes through one forge adapter*.
"""

import subprocess
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime

from forge_transport import (
    TIMEOUT_S,
    ForgeError,
    Response as Response,
    Transport,
    _api_message,
    _json,
    _updated,
    urllib_post as urllib_post,
    urllib_transport,
)
from forge_transport import DISCORD_MAX as DISCORD_MAX, Discord as Discord
from forge_registry import (
    ARTIFACT,
    MANIFEST_ACCEPT as MANIFEST_ACCEPT,
    REGISTRY,
    Registry as Registry,
)


REPOSITORY = "DanielH2018/server"
# The repository variable that holds promotion: `none`, or why it is held (contract C2).
HOLD_VARIABLE = "DEPLOY_HOLD"

# The artifact as the Packages API names it: a container package owned by the user account,
# not by the repository, so its path starts at /users.
PACKAGE_VERSIONS_URL = "https://api.github.com/users/DanielH2018/packages/container/homelab-manifests/versions"
# The API's largest page. A listing longer than VERSION_PAGES_MAX pages is an error rather than
# a truncation, because `prune` reads a digest missing from the listing as deleted.
VERSIONS_PER_PAGE = 100
VERSION_PAGES_MAX = 300
# A commit's statuses, 100 to a page: one page hid 13 of 113 and hung verify (#5974).
STATUS_PAGE_SIZE, STATUS_PAGES_MAX = 100, 20
# A commit's check-runs, 100 to a page, the same shape as its statuses (#5977).
CHECK_RUN_PAGE_SIZE, CHECK_RUN_PAGES_MAX = 100, 20
# Seconds between two deletes. GitHub's REST best practices ask for at least one second between
# mutating requests, and a burst trips the secondary rate limit as a 403 that reads like a
# permission error.
DELETE_PAUSE_S = 1.0

# The keyless identity the `promote` workflow signs under, and the one the OCIRepository
# `homelab` checks. A signature from any other workflow or ref does not verify.
SIGNER_IDENTITY = "https://github.com/DanielH2018/server/.github/workflows/promote.yml@refs/heads/master"
SIGNER_ISSUER = "https://token.actions.githubusercontent.com"

# cosign verify fetches the signature, then reaches Sigstore's transparency log.
COSIGN_TIMEOUT_S = 120


class GitHub:
    """One GitHub repository: a variable through `gh`, check-runs and packages through REST."""

    def __init__(
        self,
        transport: Transport,
        token: str | None,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        repository: str = REPOSITORY,
        versions_per_page: int = VERSIONS_PER_PAGE,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Holds the two ways in.

        Args:
            transport: sends each REST request.
            token: the bearer token for REST calls. The variable read does not use it,
                because `gh` reads `GH_TOKEN` from its own environment.
            run: starts the `gh` subprocess.
            repository: the `owner/name` slug.
            versions_per_page: the page size of the package listing.
            sleep: waits DELETE_PAUSE_S before each delete.
        """
        self._transport = transport
        self._token = token
        self._run = run
        self.repository = repository
        self._versions_per_page = versions_per_page
        self._sleep = sleep

    def variable(self, name: str) -> str | None:
        """The value of repository variable `name`, or None when the variable does not exist.

        The value comes back as `gh` prints it, less its one trailing newline.

        Raises:
            ForgeError: `gh` failed for any other reason, or did not finish.
        """
        argv = ["gh", "variable", "get", name, "-R", self.repository]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"gh variable get {name}: {err}") from err
        if proc.returncode == 0:
            return proc.stdout.removesuffix("\n")
        # cli/cli pkg/cmd/variable/get maps the API's 404 to this message.
        if f"variable {name} was not found" in proc.stderr:
            return None
        raise ForgeError(
            f"gh variable get {name} exited {proc.returncode}: {proc.stderr.strip()}"
        )

    def check_runs(self, sha: str) -> list[dict]:
        """Every check-run GitHub holds for commit `sha`, page after page.

        One page holds 100 runs, and a commit can carry more, so a single read would hide a
        failing or pending run on page 2 (#5977). The endpoint reports `total_count`, so a read
        that collects fewer runs than that is refused rather than returned as complete. A
        re-run can appear twice across pages; the caller reduces runs by name.

        Raises:
            ForgeError: any status but 200 on any page, a body without a `check_runs` list or
                an integer `total_count`, fewer runs than `total_count`, more than
                CHECK_RUN_PAGES_MAX pages, or no answer.
        """
        runs: list[dict] = []
        for page in range(1, CHECK_RUN_PAGES_MAX + 1):
            url = (
                f"https://api.github.com/repos/{self.repository}/commits/{sha}/check-runs"
                f"?per_page={CHECK_RUN_PAGE_SIZE}&page={page}"
            )
            response = self._transport("GET", url, self._rest_headers())
            if response.status != 200:
                raise ForgeError(
                    f"check-runs for {sha}: HTTP {response.status}"
                    f"{_api_message(response)} (page {page})"
                )
            body = _json(response, f"check-runs for {sha}")
            found = body.get("check_runs") if isinstance(body, dict) else None
            total = body.get("total_count") if isinstance(body, dict) else None
            if not isinstance(found, list):
                raise ForgeError(
                    f"check-runs for {sha}: the body has no check_runs list"
                )
            if not isinstance(total, int) or isinstance(total, bool):
                raise ForgeError(f"check-runs for {sha}: the body has no total_count")
            runs.extend(found)
            if len(found) < CHECK_RUN_PAGE_SIZE or len(runs) >= total:
                # A run created between two page reads can push the count past total_count;
                # only a shortfall means a page went missing.
                if len(runs) < total:
                    raise ForgeError(
                        f"check-runs for {sha}: read {len(runs)} of {total} runs"
                    )
                return runs
        raise ForgeError(f"check-runs for {sha}: more than {CHECK_RUN_PAGES_MAX} pages")

    def runs(self, workflow: str, status: str) -> list[dict]:
        """The newest 100 runs of `workflow`, a file name such as `promote.yml`, with `status`.

        Raises:
            ForgeError: any status but 200, a body without a `workflow_runs` list, or no answer.
        """
        url = (
            f"https://api.github.com/repos/{self.repository}/actions/workflows/{workflow}"
            f"/runs?status={status}&per_page=100"
        )
        response = self._transport("GET", url, self._rest_headers())
        if response.status != 200:
            raise ForgeError(f"{workflow} runs: HTTP {response.status}")
        runs = _json(response, f"{workflow} runs")
        runs = runs.get("workflow_runs") if isinstance(runs, dict) else None
        if not isinstance(runs, list):
            raise ForgeError(f"{workflow} runs: the body has no workflow_runs list")
        return runs

    def statuses(self, sha: str, since: datetime | None = None) -> dict[str, str]:
        """The newest commit status per context on commit `sha`, as `{context: state}`.

        Reads GitHub's combined status, which keeps the newest status of each context, page
        after page: one page holds 100 contexts, and a commit can carry more (#5974). A
        state is `pending`, `success`, `failure` or `error`. A context with no status is
        absent from the map, which the caller must never read as success. With `since`, a
        status whose `updated_at` precedes it, or is unreadable, is absent too.

        Raises:
            ForgeError: any status but 200 on any page, a body without a `statuses` list,
                more than STATUS_PAGES_MAX pages, or no answer.
        """
        return {
            entry["context"]: entry["state"]
            for entry in self._combined_statuses(sha)
            if isinstance(entry.get("state"), str)
            and (since is None or _updated(entry) >= since)
        }

    def status(self, sha: str, context: str) -> tuple[str, datetime] | None:
        """The newest status of `context` on commit `sha`, as `(state, updated_at)`.

        Returns:
            None when the commit carries no status of that context. An `updated_at` that is
            missing or malformed reads as the earliest time, so an age bound refuses it.

        Raises:
            ForgeError: as `statuses` does.
        """
        for entry in self._combined_statuses(sha):
            if entry["context"] == context:
                return str(entry.get("state")), _updated(entry)
        return None

    def _combined_statuses(self, sha: str) -> list[dict]:
        """Each context's newest status on `sha`, from every page up to the first short one."""
        seen: dict[str, dict] = {}
        for page in range(1, STATUS_PAGES_MAX + 1):
            url = (
                f"https://api.github.com/repos/{self.repository}/commits/{sha}/status"
                f"?per_page={STATUS_PAGE_SIZE}&page={page}"
            )
            response = self._transport("GET", url, self._rest_headers())
            if response.status != 200:
                raise ForgeError(
                    f"statuses for {sha}: HTTP {response.status}"
                    f"{_api_message(response)} (page {page})"
                )
            body = _json(response, f"statuses for {sha}")
            found = body.get("statuses") if isinstance(body, dict) else None
            if not isinstance(found, list):
                raise ForgeError(f"statuses for {sha}: the body has no statuses list")
            for entry in found:
                k = entry.get("context") if isinstance(entry, dict) else None
                if isinstance(k, str) and _updated(entry) >= _updated(seen.get(k, {})):
                    seen[k] = entry
            if len(found) < STATUS_PAGE_SIZE:
                return list(seen.values())
        raise ForgeError(f"statuses for {sha}: more than {STATUS_PAGES_MAX} pages")

    def commit_pulls(self, sha: str) -> list[dict]:
        """The pull requests GitHub associates with commit `sha`, up to a page of 100.

        Raises:
            ForgeError: any status but 200, a body that is not a JSON list, or no answer.
        """
        url = (
            f"https://api.github.com/repos/{self.repository}/commits/{sha}/pulls"
            "?per_page=100"
        )
        response = self._transport("GET", url, self._rest_headers())
        if response.status != 200:
            raise ForgeError(
                f"pull requests for {sha}: HTTP {response.status}{_api_message(response)}"
            )
        body = _json(response, f"pull requests for {sha}")
        if not isinstance(body, list):
            raise ForgeError(f"pull requests for {sha}: the body is not a list")
        return body

    def set_variable(self, name: str, value: str) -> None:
        """Set repository variable `name` to `value` through `gh`, which reads `GH_TOKEN`.

        Raises:
            ForgeError: `gh` failed, or did not finish.
        """
        argv = ["gh", "variable", "set", name, "--body", value, "-R", self.repository]
        try:
            proc = self._run(argv, capture_output=True, text=True, timeout=TIMEOUT_S)
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"gh variable set {name}: {err}") from err
        if proc.returncode != 0:
            raise ForgeError(
                f"gh variable set {name} exited {proc.returncode}: {proc.stderr.strip()}"
            )

    def package_versions(self) -> list[dict]:
        """Every version of the artifact's package, as the Packages API returns each one.

        Reads page after page until one comes back empty, so the list is the whole listing or
        an error, never a prefix of it.

        Raises:
            ForgeError: any status but 200 on any page, a page that is not a JSON list, more
                than VERSION_PAGES_MAX pages, or no answer.
        """
        versions = []
        for page in range(1, VERSION_PAGES_MAX + 1):
            url = (
                f"{PACKAGE_VERSIONS_URL}?per_page={self._versions_per_page}&page={page}"
            )
            response = self._transport("GET", url, self._rest_headers())
            if response.status != 200:
                raise ForgeError(
                    f"package versions page {page}: HTTP {response.status}"
                    f"{_api_message(response)}"
                )
            body = _json(response, f"package versions page {page}")
            if not isinstance(body, list):
                raise ForgeError(
                    f"package versions page {page}: the body is not a list"
                )
            if not body:
                return versions
            versions.extend(body)
        raise ForgeError(
            f"package versions: more than {VERSION_PAGES_MAX} pages of "
            f"{self._versions_per_page}"
        )

    def delete_package_version(self, version_id: int) -> bool:
        """Delete one version of the artifact's package.

        Returns:
            True when the version was deleted, False when the API no longer holds it.

        Raises:
            ForgeError: any status but 204 or 404, or no answer. A token without the
                package's Admin role gets a 403 here.
        """
        url = f"{PACKAGE_VERSIONS_URL}/{version_id}"
        self._sleep(DELETE_PAUSE_S)
        response = self._transport("DELETE", url, self._rest_headers())
        if response.status == 204:
            return True
        if response.status == 404:
            return False
        raise ForgeError(
            f"delete package version {version_id}: HTTP {response.status}"
            f"{_api_message(response)}"
        )

    def _rest_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers


class Cosign:
    """Runs `cosign verify` against the artifact's signer identity."""

    def __init__(
        self,
        run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
        repository: str = ARTIFACT,
    ) -> None:
        self._run = run
        self.repository = repository

    def verify(self, digest: str) -> None:
        """Verify the signature on the artifact version `digest` names.

        Raises:
            ForgeError: cosign exited non-zero or did not finish. A missing signature, a
                wrong identity and an unreachable Sigstore all land here, so the caller
                cannot mistake an outage for an unsigned version.
        """
        argv = [
            "cosign",
            "verify",
            "--certificate-identity",
            SIGNER_IDENTITY,
            "--certificate-oidc-issuer",
            SIGNER_ISSUER,
            f"{REGISTRY}/{self.repository}@{digest}",
        ]
        try:
            proc = self._run(
                argv, capture_output=True, text=True, timeout=COSIGN_TIMEOUT_S
            )
        except (OSError, subprocess.TimeoutExpired) as err:
            raise ForgeError(f"cosign verify {digest}: {err}") from err
        if proc.returncode != 0:
            raise ForgeError(
                f"cosign verify {digest} exited {proc.returncode}: {proc.stderr.strip()}"
            )


@dataclass(frozen=True)
class Forge:
    """The three clients `promote.py` reads through.

    Attributes:
        registry: the GHCR client for the artifact's repository.
        github: the repository's variable and check-runs reader.
        cosign: the signature check.
    """

    registry: Registry
    github: GitHub
    cosign: Cosign


def connect(
    env: Mapping[str, str],
    transport: Transport = urllib_transport,
    run: Callable[..., subprocess.CompletedProcess] = subprocess.run,
) -> Forge:
    """The live clients, with `GITHUB_TOKEN` from `env` for the registry and REST calls.

    `gh` reads `GH_TOKEN` from the environment it inherits, which the workflow sets to
    `PROMOTE_TOKEN` on the step that runs `promote.py decide`.
    """
    token = env.get("GITHUB_TOKEN") or None
    return Forge(
        registry=Registry(transport, token),
        github=GitHub(transport, token, run=run),
        cosign=Cosign(run=run),
    )
