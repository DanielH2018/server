"""vault-mcp's OAuth resource-server side: Authelia's JWT access tokens and RFC 9728 metadata.

claude.ai's custom connector signs in through Authelia's `vault-mcp` OIDC client and sends the
RS256 access token Authelia issues (RFC 9068). `ResourceServer` checks that token and builds
the two documents the connector reads before it signs in: the `WWW-Authenticate` challenge on
a 401, and the protected-resource metadata that names Authelia as the authorization server.
It imports PyJWT only, so the tests run without the MCP SDK.

Typical usage example:

    oauth = ResourceServer.from_env()
    oauth.keys.start()
    if challenge := oauth.challenge(request.headers.get("authorization")):
        ...  # 401 with WWW-Authenticate: challenge
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

log = logging.getLogger(__name__)

# RFC 9068 section 4: a resource server refuses any other `typ`, which keeps an ID token out.
ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})
# Authelia's `vault-mcp` OIDC client. Authelia writes the RFC 9068 `client_id` claim from the
# client that requested the token (authelia.com/provider/oauth2 v0.3.2,
# handler/oauth2/strategy_jwt_profile.go), so another client's token for this audience fails.
CLIENT_ID = "vault-mcp"
SCOPES = ("openid", "offline_access")
CLOCK_SKEW_S = 60
JWKS_TIMEOUT_S = 5
JWKS_REFRESH_S = 300
JWKS_COOLDOWN_S = 30
# Three refresh intervals, so one or two failed fetches do not read as stale.
JWKS_STALE_AFTER_S = 3 * JWKS_REFRESH_S
_ENV = ("VAULT_MCP_RESOURCE_URL", "VAULT_MCP_ISSUER", "VAULT_MCP_JWKS_URL")


@dataclass
class SigningKeys:
    """Authelia's signing keys by `kid`, fetched by a background thread and read without a lock.

    A token check only reads the last fetched keys, so it never waits on Authelia. The thread
    fetches every `JWKS_REFRESH_S`, and early when a token names a `kid` it lacks. It leaves
    `JWKS_COOLDOWN_S` after every fetch, failed or not, before the next. A failed fetch keeps
    the keys it had, so an Authelia outage refuses no token those keys verify.

    Attributes:
        fetch: Returns the published keys by `kid`. Raises when Authelia cannot be reached.
        clock: Monotonic seconds, the time `fresh()` reads.
        sleep: Blocks the refresher thread through each cooldown.
    """

    fetch: Callable[[], Mapping[str, RSAPublicKey]]
    clock: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    _keys: Mapping[str, RSAPublicKey] = field(default_factory=dict)
    _fetched_at: float | None = None
    _wake: threading.Event = field(default_factory=threading.Event)

    def get(self, kid: str) -> RSAPublicKey | None:
        """The key published under `kid`, or None, which also asks the thread to fetch early."""
        found = self._keys.get(kid)
        if found is None:
            self._wake.set()
        return found

    def refresh(self) -> bool:
        """Fetches the keys once and swaps them in; whether the fetch succeeded."""
        try:
            fetched = dict(self.fetch())
        except Exception as err:
            log.warning("cannot fetch Authelia's JWKS: %s", err)
            return False
        self._keys = fetched
        self._fetched_at = self.clock()
        return True

    def fresh(self) -> bool:
        """Whether the last successful fetch is less than `JWKS_STALE_AFTER_S` old."""
        return (
            self._fetched_at is not None
            and self.clock() - self._fetched_at < JWKS_STALE_AFTER_S
        )

    def start(self) -> None:
        """Starts the daemon thread that keeps the keys fetched."""
        threading.Thread(target=self._run, name="jwks-refresh", daemon=True).start()

    def _run(self) -> None:
        while True:
            self._cycle()

    def _cycle(self) -> None:
        self._wake.clear()
        fetched = self.refresh()
        self.sleep(JWKS_COOLDOWN_S)
        if fetched:
            self._wake.wait(JWKS_REFRESH_S - JWKS_COOLDOWN_S)


@dataclass(frozen=True)
class ResourceServer:
    """The connector URL this server answers for, and the issuer whose tokens it accepts.

    Attributes:
        resource: The connector URL as entered on claude.ai. It is the `resource` the metadata
            advertises and the `aud` every token must carry.
        issuer: Authelia's public issuer, the token's `iss` and the metadata's first
            authorization server.
        keys: Authelia's published signing keys. Nothing fetches them until `keys.start()`.
    """

    resource: str
    issuer: str
    keys: SigningKeys

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> ResourceServer:
        """Builds the server from `VAULT_MCP_RESOURCE_URL`, `VAULT_MCP_ISSUER` and `VAULT_MCP_JWKS_URL`.

        Nothing is fetched here, so a pod that starts before it can reach Authelia still
        serves `/health`.

        Raises:
            SystemExit: when any of the three is unset or empty, since no token could pass.
        """
        missing = [name for name in _ENV if not environ.get(name)]
        if missing:
            raise SystemExit(f"{', '.join(missing)} required")
        client = jwt.PyJWKClient(
            environ["VAULT_MCP_JWKS_URL"], cache_jwk_set=False, timeout=JWKS_TIMEOUT_S
        )
        return cls(
            resource=environ["VAULT_MCP_RESOURCE_URL"],
            issuer=environ["VAULT_MCP_ISSUER"],
            keys=SigningKeys(
                lambda: {
                    jwk.key_id: jwk.key
                    for jwk in client.get_signing_keys()
                    if jwk.key_id
                }
            ),
        )

    def token_valid(self, authorization: str | None) -> bool:
        """Whether an `Authorization` header carries a token Authelia issued for this resource.

        The token must be an RS256 access token signed by a key in Authelia's JWKS, issued to
        the `vault-mcp` client, with this resource as an `aud` entry, this issuer as `iss`, and
        `exp`, `nbf` and `iat` within `CLOCK_SKEW_S` of this pod's clock. Any error while
        checking refuses the token, and so does a `kid` the last fetched JWKS lacks.
        """
        token = _bearer_token(authorization)
        if not token:
            return False
        try:
            header = jwt.get_unverified_header(token)
            if header.get("typ") not in ACCESS_TOKEN_TYPES:
                return False
            kid = header.get("kid") or ""
            signing_key = self.keys.get(kid)
            if signing_key is None:
                log.warning(
                    "no Authelia signing key for the token's kid (%s)", _digest(kid)
                )
                return False
            claims = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                leeway=CLOCK_SKEW_S,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWTError:
            return False
        # DECIDED: an error nothing above names refuses the token as a 401, never a 500. The
        # check sits in front of the public route, so a malformed token must not reach a crash.
        except Exception:
            log.exception("token check failed")
            return False
        return claims.get("client_id") == CLIENT_ID

    def challenge(self, authorization: str | None) -> str | None:
        """The `WWW-Authenticate` value of the 401 this request gets, or None when its token is valid.

        The value names where the metadata is served. Per RFC 6750 section 3.1 it carries
        `error="invalid_token"` only when the request sent a bearer token, so a request with
        no credentials, or another scheme, gets the bare challenge.
        """
        if self.token_valid(authorization):
            return None
        parts = urlsplit(self.resource)
        url = f"{parts.scheme}://{parts.netloc}{self.metadata_paths()[0]}"
        error = 'error="invalid_token", ' if _bearer_token(authorization) else ""
        return f'Bearer {error}resource_metadata="{url}"'

    def metadata(self) -> dict[str, object]:
        """The RFC 9728 protected-resource metadata document."""
        return {
            "resource": self.resource,
            "authorization_servers": [self.issuer],
            "scopes_supported": list(SCOPES),
            "bearer_methods_supported": ["header"],
        }

    def metadata_paths(self) -> tuple[str, ...]:
        """The paths that serve `metadata()`: the resource's own suffixed path, then the root one.

        claude.ai follows the challenge's URL, and probes both paths when a 401 names none.
        """
        root = "/.well-known/oauth-protected-resource"
        suffix = urlsplit(self.resource).path.rstrip("/")
        return (root + suffix, root) if suffix else (root,)


def _bearer_token(authorization: str | None) -> str:
    """The token of a `Bearer` `Authorization` header, or "" for no header or another scheme."""
    scheme, _, token = (authorization or "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


def _digest(kid: str) -> str:
    """The length and a sha256 prefix of a token's `kid`, which any caller can write."""
    return f"{len(kid)} chars, sha256 {hashlib.sha256(kid.encode()).hexdigest()[:12]}"
