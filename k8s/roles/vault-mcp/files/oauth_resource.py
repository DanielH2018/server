"""vault-mcp's OAuth resource-server side: Authelia's JWT access tokens and RFC 9728 metadata.

claude.ai's custom connector signs in through Authelia's `vault-mcp` OIDC client and sends the
RS256 access token Authelia issues (RFC 9068). `ResourceServer` checks that token and builds
the two documents the connector reads before it signs in: the `WWW-Authenticate` challenge on
a 401, and the protected-resource metadata that names Authelia as the authorization server.
It imports PyJWT only, so the tests run without the MCP SDK.

Typical usage example:

    oauth = ResourceServer.from_env()
    if not oauth.token_valid(request.headers.get("authorization")):
        ...  # 401 with WWW-Authenticate: oauth.challenge()
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

import jwt
from cryptography.hazmat.primitives.asymmetric.rsa import RSAPublicKey

log = logging.getLogger(__name__)

# RFC 9068 section 4: a resource server refuses any other `typ`, which keeps an ID token out.
ACCESS_TOKEN_TYPES = frozenset({"at+jwt", "application/at+jwt"})
SCOPES = ("openid", "offline_access")
_ENV = ("VAULT_MCP_RESOURCE_URL", "VAULT_MCP_ISSUER", "VAULT_MCP_JWKS_URL")


@dataclass(frozen=True)
class ResourceServer:
    """The connector URL this server answers for, and the issuer whose tokens it accepts.

    Attributes:
        resource: The connector URL as entered on claude.ai. It is the `resource` the metadata
            advertises and the `aud` every token must carry.
        issuer: Authelia's public issuer, the token's `iss` and the metadata's first
            authorization server.
        signing_key: Returns the key that verifies a token, looked up by the token's `kid`.
            Raises `jwt.PyJWKClientError` when no published key matches.
    """

    resource: str
    issuer: str
    signing_key: Callable[[str], RSAPublicKey]

    @classmethod
    def from_env(cls, environ: Mapping[str, str] = os.environ) -> ResourceServer:
        """Builds the server from `VAULT_MCP_RESOURCE_URL`, `VAULT_MCP_ISSUER` and `VAULT_MCP_JWKS_URL`.

        The JWKS is fetched on the first token, not here, so a pod that starts before it can
        reach Authelia still serves `/health`.

        Raises:
            SystemExit: when any of the three is unset or empty, since no token could pass.
        """
        missing = [name for name in _ENV if not environ.get(name)]
        if missing:
            raise SystemExit(f"{', '.join(missing)} required")
        keys = jwt.PyJWKClient(environ["VAULT_MCP_JWKS_URL"], timeout=5)
        return cls(
            resource=environ["VAULT_MCP_RESOURCE_URL"],
            issuer=environ["VAULT_MCP_ISSUER"],
            signing_key=lambda token: keys.get_signing_key_from_jwt(token).key,
        )

    def token_valid(self, authorization: str | None) -> bool:
        """Whether an `Authorization` header carries a token Authelia issued for this resource.

        The token must be an RS256 access token signed by a key in Authelia's JWKS, with this
        resource as an `aud` entry, this issuer as `iss`, and an `exp` still in the future.
        """
        scheme, _, token = (authorization or "").partition(" ")
        if scheme.lower() != "bearer" or not token:
            return False
        try:
            if jwt.get_unverified_header(token).get("typ") not in ACCESS_TOKEN_TYPES:
                return False
            jwt.decode(
                token,
                self.signing_key(token),
                algorithms=["RS256"],
                audience=self.resource,
                issuer=self.issuer,
                options={"require": ["exp", "iss", "aud"]},
            )
        except jwt.PyJWKClientError as err:
            log.warning("no Authelia signing key for the token: %s", err)
            return False
        except jwt.PyJWTError:
            return False
        return True

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

    def challenge(self) -> str:
        """The `WWW-Authenticate` value of a 401, naming where the metadata is served."""
        parts = urlsplit(self.resource)
        url = f"{parts.scheme}://{parts.netloc}{self.metadata_paths()[0]}"
        return f'Bearer error="invalid_token", resource_metadata="{url}"'
