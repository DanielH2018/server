"""Rendered-manifest property rows whose subject is a Traefik edge object or its placement.

`test_rendered_properties.py` runs these rows beside its own. A row lives here only to keep that
file under the test-module length cap; the rules for what makes a row are in its docstring.
"""

import re
from functools import cache

from service_tier import tier_entries

from _helpers import ALL_VARS, HOST_VARS, load_yaml
from _k8s_render import k8s_entries, pod_spec
from _property_table import Property

# The guard keys on FIELD NAMES, never on values. The render corpus stubs every secret lookup
# (scripts/lib/render_guard.py, StubUndefined), so a value regex would read green forever while
# checking nothing.

# A key name that names a credential. `crowdsecLapiKeyFile` is the correct form — a path into a
# mounted Secret — so a `File` suffix exempts the key. That exemption is the disarm vector, and
# the row's `red` fixture aims at it.
#
# A bare `key` is deliberately absent: it is the commonest word in a manifest (`key`,
# `secretKey`, `matchLabels` values, every ConfigMap projection) and matching it would flag the
# whole corpus. The cost is that `crowdsecLapiKey` — the name this guard was written for —
# matches only because "lapikey" contains "apikey". A sibling named `bouncerKey` or `sharedKey`
# is NOT caught. Do not "simplify" the alternation without checking that case still fires.
_CREDENTIAL_FIELD = re.compile(
    r"password|passwd|secret|token|apikey|api[-_]?key|credential", re.I
)

# The reference forms: `secret:` is how basicAuth/digestAuth name a Secret, `secretName` is how
# a tls block does, `secretNames` is how a TLSOption's clientAuth names its CA Secrets. All are
# the fix, not the finding.
_SECRET_REFERENCE_KEYS = frozenset({"secret", "secretName", "secretNames"})

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


def _traefik_objects(role: str, tpl: str, doc: dict):
    if str(doc.get("apiVersion", "")).startswith("traefik.io/"):
        yield f"{doc['kind']}/{role}/{doc.get('metadata', {}).get('name', '')}", doc


def _inline_credential_offence(role: str, doc: dict) -> str | None:
    findings = _traefik_credential_findings(doc)
    if not findings:
        return None
    return (
        f"carries a credential inline at {', '.join(findings)}. Put the value in a Secret and "
        "reference it: `basicAuth.secret`, a `...KeyFile` path mounted from a Secret, or a file "
        "provider carried in a Secret (roles/k8s/traefik/templates/livesync-gate-secret.yaml.j2)"
    )


# The members, as Kind/role/name, rather than a count alone: a correct guard finds zero
# offenders, so without them the census could empty (a group-version bump, a role rename) and
# still read green. traefik/crowdsec is the only object with a `plugin` block and the one this
# row was written for. The TLSOptions are named so a lost kind is named too, since the table has
# one floor for the whole group rather than one per kind.
_TRAEFIK_MUST_FIND = frozenset(
    {
        "Middleware/authelia/authelia",
        "Middleware/observability/authelia",
        "Middleware/observability/rate-limit",
        "Middleware/observability/rate-limit-proxied",
        "Middleware/karakeep/csp-karakeep",
        "Middleware/longhorn-ui/authelia",
        "Middleware/longhorn-ui/rate-limit",
        "Middleware/traefik/compress",
        "Middleware/traefik/crowdsec",
        "Middleware/traefik/default-headers",
        "Middleware/traefik/rate-limit",
        "Middleware/traefik/rate-limit-proxied",
        "Middleware/traefik/rate-limit-public-livesync",
        "Middleware/traefik/rate-limit-public-livesync-proxied",
        "TLSOption/traefik/modern",
        "TLSOption/traefik/cloudflare-origin-pull",
        "TLSOption/observability/cloudflare-origin-pull",
    }
)


@cache
def _home_critical_roles() -> frozenset[str]:
    entries = list(k8s_entries().values())
    return frozenset(e["name"] for e in tier_entries(entries, "home-critical"))


@cache
def _primary_node() -> str:
    return load_yaml(ALL_VARS)["k8s_primary_node"]


def _home_critical_workloads(role: str, tpl: str, doc: dict):
    # A DaemonSet runs on every node by design, and a Job or CronJob serves no client.
    if role in _home_critical_roles() and doc.get("kind") in {
        "Deployment",
        "StatefulSet",
    }:
        yield doc["metadata"]["name"], pod_spec(doc)


def _vip_node_offence(role: str, pod: dict) -> str | None:
    pinned = (pod.get("nodeSelector") or {}).get("kubernetes.io/hostname")
    if pinned == _primary_node():
        return None
    return (
        f"pinned to {pinned!r}, not k8s_primary_node {_primary_node()!r}; a preferred "
        "affinity does not count, because a pod rescheduled during a reboot stays put after it"
    )


# Every role whose rendered Service pins an address. jellyfin pins its LAN address in
# `service-lan.yaml.j2`, which the template glob this row replaced never read.
_METALLB_PINNED_ROLES = frozenset({"jellyfin", "mosquitto", "pihole", "traefik"})


def _metallb_annotations(role: str, tpl: str, doc: dict):
    """One subject per metallb annotation, keyed by role, so `must_find` needs an annotation."""
    if doc.get("kind") != "Service":
        return
    annotations = (doc.get("metadata") or {}).get("annotations") or {}
    for key in sorted(annotations):
        if "metallb" in key:
            yield role, {"annotation": key}


def _metallb_prefix_offence(role: str, subject: dict) -> str | None:
    if not subject["annotation"].startswith("metallb.universe.tf/"):
        return None
    return f"{subject['annotation']} uses the deprecated metallb.universe.tf/ prefix; use metallb.io/"


@cache
def _pi_ip() -> str:
    # The Pi's own literal, not k8s_pi_client_ip: that is derived from this through hostvars
    # (#3719), and a derivation that lost the Pi's entry resolves to None without raising.
    return load_yaml(HOST_VARS / "daniel-pi.yml")["server_ip"]


def _pi_push_routes(role: str, tpl: str, doc: dict):
    if tpl == "ingressroute-push.yaml.j2" and doc.get("kind") == "IngressRoute":
        for route in doc["spec"]["routes"]:
            yield doc["metadata"]["name"], route


def _pi_push_offence(role: str, route: dict) -> str | None:
    allowed = f"ClientIP(`{_pi_ip()}/32`)"
    if allowed in route.get("match", ""):
        return None
    return f"match does not admit {allowed}, so the Pi's Alloy cannot push its logs"


EDGE_PROPERTIES = (
    Property(
        name="traefik-crds-carry-no-inline-credential",
        reason=(
            'The readonly ServiceAccount holds `resources: ["*"]` on the traefik.io group '
            "(roles/setup/k3s/templates/readonly-rbac.yaml.j2) while Secrets are withheld. So a "
            "credential written into a Middleware or an IngressRoute is readable by anything "
            "holding the readonly kubeconfig, and the Secret it should have lived in is not. "
            "The row keys on field and header NAMES, never on values, because the render stubs "
            "every secret lookup."
        ),
        select=_traefik_objects,
        offence=_inline_credential_offence,
        # Each red fixture is the inline form of something the live tree does correctly. The
        # first is the crowdsec LAPI key without its `File` suffix: the disarm vector.
        red=("traefik", {"spec": {"plugin": {"bouncer": {"crowdsecLapiKey": "stub"}}}}),
        more_red=(
            ("traefik", {"spec": {"basicAuth": {"users": ["admin:$apr1$stub"]}}}),
            (
                "traefik",
                {
                    "spec": {
                        "headers": {
                            "customRequestHeaders": {"Authorization": "Bearer stub"}
                        }
                    }
                },
            ),
            (
                "traefik",
                {
                    "spec": {
                        "routes": [
                            {
                                "match": "Host(`a.example.com`) && "
                                "Header(`X-Livesync-Token`, `stub`)"
                            }
                        ]
                    }
                },
            ),
        ),
        # The indirections that ARE the fix, plus the ordinary header work every edge
        # middleware does. A row firing on these would be unusable and would get exempted away.
        green=(
            "traefik",
            {
                "spec": {
                    "plugin": {
                        "bouncer": {"crowdsecLapiKeyFile": "/run/crowdsec/lapi_key"}
                    }
                }
            },
        ),
        more_green=(
            ("traefik", {"spec": {"basicAuth": {"secret": "traefik-basic-auth"}}}),
            (
                "traefik",
                {
                    "spec": {
                        "clientAuth": {
                            "secretNames": ["cloudflare-origin-pull-ca"],
                            "clientAuthType": "RequireAndVerifyClientCert",
                        }
                    }
                },
            ),
            (
                "traefik",
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
            ),
            (
                "traefik",
                {
                    "spec": {
                        "routes": [
                            {"match": "Host(`a.example.com`) && PathPrefix(`/api`)"}
                        ]
                    }
                },
            ),
        ),
        # 106 traefik.io objects render: 80 IngressRoutes, 21 Middlewares, 5 TLSOptions.
        min_matches=95,
        must_find=_TRAEFIK_MUST_FIND,
    ),
    Property(
        name="home-critical-workloads-pinned-to-the-vip-node",
        reason=(
            "Traefik, pihole and mosquitto run on the node that announces their MetalLB VIPs, "
            "so the house already goes down with that node. The rest of a home-critical "
            "entry's workloads are pinned there too (#3452): HA and authelia on daniel-server "
            "made a second node whose loss broke SSO and home automation. The selector reads "
            "`tier` on containers_list entries, so a newly tiered role joins without an edit "
            "here. The DECIDED marker in the home-assistant Deployment template has the "
            "trade-off."
        ),
        select=_home_critical_workloads,
        offence=_vip_node_offence,
        red=("home-assistant", {"affinity": {"nodeAffinity": {}}}),
        green=(
            "home-assistant",
            {"nodeSelector": {"kubernetes.io/hostname": "daniel-box"}},
        ),
        more_red=(
            (
                "home-assistant",
                {"nodeSelector": {"kubernetes.io/hostname": "daniel-server"}},
            ),
        ),
        must_find=frozenset(
            {
                "traefik",
                "pihole",
                "pihole-2",
                "mosquitto",
                "authelia",
                "authelia-redis",
                "crowdsec",
                "zigbee2mqtt",
                "home-assistant",
            }
        ),
    ),
    Property(
        name="metallb-service-annotations-use-metallb-io",
        reason=(
            "Service annotations moved to metallb.io/ in MetalLB v0.15, and universe.tf/ is "
            "deprecated. v0.16.0 reads both prefixes (controller/service.go "
            "valueForAnnotation, metallb.io winning) but emits a `deprecatedAnnotation` Warning "
            "Event per Service on every reconcile for the old one. Kubernetes accepts any "
            "annotation key and MetalLB ignores one it does not recognise, so a wrong prefix is "
            "silent: the Service gets an address from the auto-assign pool instead of the pinned "
            "one, and the deploy is green. The row reads the rendered Services (#3209), so a "
            "comment naming the old prefix is not credited as an annotation. "
            "test_k8s_manifests_metallb.py ties the k3s_metallb_version pin to v0.15 or later."
        ),
        select=_metallb_annotations,
        offence=_metallb_prefix_offence,
        red=("traefik", {"annotation": "metallb.universe.tf/loadBalancerIPs"}),
        green=("traefik", {"annotation": "metallb.io/loadBalancerIPs"}),
        must_find=_METALLB_PINNED_ROLES,
    ),
    Property(
        name="pi-push-route-admits-the-pi",
        reason=(
            "loki-homelab's push route allows daniel-pi by source address, built from "
            "k8s_pi_client_ip. That variable derives from the Pi's server_ip through hostvars "
            "(#3719), and a hostvars without the Pi's entry renders `None/32` with every "
            "validator green. The row compares against the Pi's own host_vars literal."
        ),
        select=_pi_push_routes,
        offence=_pi_push_offence,
        red=("loki-homelab", {"match": "Host(`x`) && ClientIP(`None/32`)"}),
        green=("loki-homelab", {"match": f"Host(`x`) && ClientIP(`{_pi_ip()}/32`)"}),
        must_find=frozenset({"loki-homelab-push-monitoring"}),
    ),
)
