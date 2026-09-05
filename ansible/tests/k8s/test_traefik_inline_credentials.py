"""No rendered traefik.io CRD may carry a credential the readonly ServiceAccount can read.

The readonly ServiceAccount holds `resources: ["*"]` on the traefik.io group
(`roles/setup/k3s/templates/readonly-rbac.yaml.j2`) while Secrets are withheld. So a credential
written into a Middleware or an IngressRoute is readable by anything holding the readonly
kubeconfig, and the Secret it should have lived in is not. Before this module the rule existed
only as prose in three templates.
"""

import re

from _k8s_render import rendered_docs

# The guard keys on FIELD NAMES, never on values. The render corpus stubs every secret lookup
# (scripts/lib/render_guard.py, StubUndefined), so a value regex would read green forever while
# checking nothing.

# A key name that names a credential. `crowdsecLapiKeyFile` is the correct form — a path into a
# mounted Secret — so a `File` suffix exempts the key. That exemption is the disarm vector, and
# test_traefik_credential_guard_rejects_inline_credential_fixtures aims its first fixture at it.
_CREDENTIAL_FIELD = re.compile(
    r"password|passwd|secret|token|apikey|api[-_]?key|credential", re.I
)

# The reference forms: `secret:` is how basicAuth/digestAuth name a Secret, `secretName` is how
# a tls block does. Both are the fix, not the finding.
_SECRET_REFERENCE_KEYS = frozenset({"secret", "secretName"})

# Maps whose KEYS are HTTP header names, where an auth header carries its credential inline.
_HEADER_MAPS = frozenset({"customRequestHeaders", "customResponseHeaders"})
_AUTH_HEADER = re.compile(
    r"^(authorization|proxy-authorization|cookie)$|[-_](token|key|secret|password|auth)$",
    re.I,
)

# `Header(`/`HeaderRegexp(` in a route match. The header NAME is a literal even where the value
# it compares against renders as a stub, so this rule stays name-keyed. The two file-provider
# routers in livesync-gate-secret.yaml.j2 keep exactly this shape inside a Secret; moving one
# into an IngressRoute CRD is the regression this catches.
_MATCH_HEADER = re.compile(r"Header(?:Regexp)?\(\s*`([^`]*)`", re.I)


def _traefik_credential_findings(doc: dict) -> list[str]:
    """Dotted paths inside a traefik.io object's spec that carry a credential inline."""
    findings: list[str] = []

    def walk(node, path: str) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                here = f"{path}.{key}"
                if (
                    key in ("basicAuth", "digestAuth")
                    and isinstance(value, dict)
                    and "users" in value
                ):
                    findings.append(f"{here}.users")
                if key in _HEADER_MAPS and isinstance(value, dict):
                    findings.extend(
                        f"{here}.{hdr}"
                        for hdr in value
                        if _AUTH_HEADER.search(str(hdr))
                    )
                if key == "match" and isinstance(value, str):
                    findings.extend(
                        f"{here} Header(`{hdr}`)"
                        for hdr in _MATCH_HEADER.findall(value)
                        if _AUTH_HEADER.search(hdr)
                    )
                if (
                    _CREDENTIAL_FIELD.search(key)
                    and not key.endswith("File")
                    and key not in _SECRET_REFERENCE_KEYS
                ):
                    findings.append(here)
                walk(value, here)
        elif isinstance(node, list):
            for index, item in enumerate(node):
                walk(item, f"{path}[{index}]")

    walk(doc.get("spec"), "spec")
    return findings


# The denominator, as role/name pairs rather than a count: a correct guard finds zero offenders
# today, so without this the census could empty (a group-version bump, a role rename) and still
# read green. Subset, not equality — a new middleware must not fail this.
_REQUIRED_TRAEFIK_MIDDLEWARES = frozenset(
    {
        "authelia/authelia",
        "claude-otel/authelia",
        "claude-otel/rate-limit",
        "karakeep/csp-karakeep",
        "longhorn-ui/authelia",
        "longhorn-ui/rate-limit",
        "traefik/compress",
        "traefik/crowdsec",
        "traefik/default-headers",
        "traefik/rate-limit",
        "traefik/rate-limit-public-livesync",
    }
)
# Floors for the other two kinds in the group — 47 IngressRoutes and 3 TLSOptions render today.
_TRAEFIK_KIND_FLOORS = {"IngressRoute": 40, "TLSOption": 3}


def test_no_traefik_crd_carries_an_inline_credential():
    """No rendered traefik.io object may carry a credential the readonly SA can read."""
    offenders: list[str] = []
    middlewares: set[str] = set()
    kinds: dict[str, int] = {}
    for role, tpl, doc in rendered_docs():
        if not str(doc.get("apiVersion", "")).startswith("traefik.io/"):
            continue
        kind = doc.get("kind", "")
        kinds[kind] = kinds.get(kind, 0) + 1
        name = doc.get("metadata", {}).get("name", "")
        if kind == "Middleware":
            middlewares.add(f"{role}/{name}")
        offenders += [
            f"{role}/{tpl} {kind}/{name} {found}"
            for found in _traefik_credential_findings(doc)
        ]

    missing = _REQUIRED_TRAEFIK_MIDDLEWARES - middlewares
    assert not missing, (
        f"the traefik.io census lost {sorted(missing)}, so this guard now checks less than it "
        "did. traefik/crowdsec is the only object with a `plugin` block and the one this rule "
        "was written for. If you disabled crowdsec or renamed a middleware deliberately, drop "
        "the name from _REQUIRED_TRAEFIK_MIDDLEWARES in the same PR and say why."
    )
    for kind, floor in _TRAEFIK_KIND_FLOORS.items():
        assert kinds.get(kind, 0) >= floor, (
            f"only {kinds.get(kind, 0)} {kind}(s) rendered, below the floor of {floor} — the "
            "census is narrower than the traefik.io group this guard claims to cover."
        )
    assert not offenders, (
        "traefik.io object(s) carrying a credential inline, where the readonly ServiceAccount "
        f"can read it: {sorted(offenders)}. Put the value in a Secret and reference it: "
        "`basicAuth.secret`, a `...KeyFile` path mounted from a Secret, or a file provider "
        "carried in a Secret (roles/k8s/traefik/templates/livesync-gate-secret.yaml.j2)."
    )


def test_traefik_credential_guard_rejects_inline_credential_fixtures():
    """Red-proof, one fixture per rule. Each is the inline form of something the live tree does
    correctly: the crowdsec LAPI key without its `File` suffix, a basicAuth `users:` list, an
    Authorization header set inline, and a route matching on a token header.
    """
    fixtures = [
        {"spec": {"plugin": {"bouncer": {"crowdsecLapiKey": "stub"}}}},
        {"spec": {"basicAuth": {"users": ["admin:$apr1$stub"]}}},
        {
            "spec": {
                "headers": {"customRequestHeaders": {"Authorization": "Bearer stub"}}
            }
        },
        {
            "spec": {
                "routes": [
                    {
                        "match": "Host(`a.example.com`) && Header(`X-Livesync-Token`, `stub`)"
                    }
                ]
            }
        },
    ]
    for fixture in fixtures:
        assert _traefik_credential_findings(fixture), (
            f"guard accepted {fixture!r}, which carries a credential inline in a CRD the "
            "readonly ServiceAccount can read"
        )


def test_traefik_credential_guard_accepts_the_reference_forms():
    """Accept: the indirections that ARE the fix, plus the ordinary header work every edge
    middleware does. A guard firing on these would be unusable and would get exempted away.
    """
    fixtures = [
        {
            "spec": {
                "plugin": {"bouncer": {"crowdsecLapiKeyFile": "/run/crowdsec/lapi_key"}}
            }
        },
        {"spec": {"basicAuth": {"secret": "traefik-basic-auth"}}},
        {
            "spec": {
                "headers": {
                    "customRequestHeaders": {"X-Forwarded-Proto": "https"},
                    "customResponseHeaders": {
                        "Content-Security-Policy": "default-src 'self'"
                    },
                }
            }
        },
        {
            "spec": {
                "routes": [{"match": "Host(`a.example.com`) && PathPrefix(`/api`)"}]
            }
        },
    ]
    for fixture in fixtures:
        assert not _traefik_credential_findings(fixture), (
            f"guard rejected {fixture!r}, which references a Secret rather than inlining a value"
        )
