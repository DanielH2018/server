"""Home Assistant's `trusted_proxies` must list the pod CIDR plus exactly `cloudflare_ips`.

A Cloudflare-proxied request reaches HA with X-Forwarded-For `client, edge`: Traefik's
`cloudflare-realip` Middleware sets XFF to CF-Connecting-IP, then Traefik appends the Cloudflare
edge it received the connection from. HA walks XFF right-to-left and takes the first entry that
is not a trusted proxy. With the pod CIDR alone it took the edge, so `ip_ban` could ban a
Cloudflare edge and lock out every client routed through it.

`files/configuration.yaml` ships verbatim through `lookup('file')`, so it cannot template
`cloudflare_ips` or `k3s_pod_cidr` from `group_vars/all.yml` and carries both literally. This
guard keeps the file equal to both variables. When monitor-bridge's `cloudflare_ips_drift` tile reports new published ranges
and `all.yml` is updated, this test goes red until HA's list follows. It also checks each entry
is a strict network, because nothing else validates the CIDRs before HA reads them and a
malformed entry can crash-loop HA.
"""

import ipaddress

import yaml
from _helpers import ALL_VARS, REPO, load_yaml

HA_CONFIG = REPO / "ansible/roles/k8s/home-assistant/files/configuration.yaml"

# The k3s pod CIDR, read from group_vars so a CIDR change turns this test red until HA's list
# follows. Traefik reaches HA from a pod IP in it; see the `DECIDED: the whole pod CIDR` comment
# in configuration.yaml for why it is not narrowed to Traefik.
POD_CIDR = load_yaml(ALL_VARS)["k3s_pod_cidr"]


def _ha_trusted_proxies() -> list[str]:
    """`http.trusted_proxies` out of the shipped configuration.yaml.

    The file carries HA tags (`!secret`, `!include`) that `yaml.safe_load` rejects, so unknown
    tags resolve to None. The `http:` block uses none of them.
    """

    class _Loader(yaml.SafeLoader):
        pass

    _Loader.add_multi_constructor("!", lambda loader, suffix, node: None)
    config = yaml.load(HA_CONFIG.read_text(), Loader=_Loader)
    return [str(entry) for entry in config["http"]["trusted_proxies"]]


def trusted_proxy_problems(trusted: list[str], cloudflare: list[str]) -> list[str]:
    """Why `trusted` is not exactly the pod CIDR plus `cloudflare`, as readable lines."""
    problems = []
    for entry in trusted:
        try:
            ipaddress.ip_network(entry, strict=True)
        except ValueError as err:
            problems.append(f"{entry!r} is not a strict network: {err}")
    expected = {POD_CIDR, *cloudflare}
    missing = sorted(expected - set(trusted))
    extra = sorted(set(trusted) - expected)
    if missing:
        problems.append(f"missing from trusted_proxies: {missing}")
    if extra:
        problems.append(
            f"in trusted_proxies but not the pod CIDR or cloudflare_ips: {extra}"
        )
    if len(trusted) != len(set(trusted)):
        problems.append("trusted_proxies lists an entry twice")
    return problems


CF = ["173.245.48.0/20", "2400:cb00::/32"]


def test_pod_cidr_plus_every_cloudflare_range_is_clean():
    assert trusted_proxy_problems([POD_CIDR, *CF], CF) == []


def test_a_missing_cloudflare_range_is_flagged():
    problems = trusted_proxy_problems([POD_CIDR, CF[0]], CF)
    assert problems == [f"missing from trusted_proxies: {[CF[1]]}"]


def test_an_extra_range_is_flagged():
    problems = trusted_proxy_problems([POD_CIDR, *CF, "10.0.0.0/8"], CF)
    assert problems == [
        "in trusted_proxies but not the pod CIDR or cloudflare_ips: ['10.0.0.0/8']"
    ]


def test_a_non_strict_network_is_flagged():
    problems = trusted_proxy_problems(
        [POD_CIDR, *CF, "173.245.48.1/20"], [*CF, "173.245.48.1/20"]
    )
    assert len(problems) == 1
    assert problems[0].startswith("'173.245.48.1/20' is not a strict network")


def test_shipped_trusted_proxies_match_cloudflare_ips():
    cloudflare = load_yaml(ALL_VARS)["cloudflare_ips"]
    trusted = _ha_trusted_proxies()
    # Non-vacuity: an empty or renamed variable would make the equality trivially about nothing.
    assert "173.245.48.0/20" in cloudflare
    assert len(cloudflare) >= 20
    assert trusted_proxy_problems(trusted, cloudflare) == []
